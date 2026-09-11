"""配置存储：API Key 加密落盘。

存放位置：~/.mda_db_mcp/
  secret.key   —— 加密用的密钥，权限 600（只有你本人能读）
  config.json  —— 配置本体，其中 api_key 字段是加密后的密文

为什么不直接明文存？
  API Key 等于你的信用卡。明文放在项目目录里，很容易被 git commit 出去，
  或者被别的程序读到。这里做两件事：
    1. 存到项目外的用户目录（不会被 git 碰到）
    2. 用 Fernet 对称加密（拿到 config.json 也解不出来，还得有 secret.key）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

CONFIG_DIR = Path(os.environ.get("MDA_CONFIG_DIR", Path.home() / ".mda_db_mcp"))
KEY_FILE = CONFIG_DIR / "secret.key"
CONFIG_FILE = CONFIG_DIR / "config.json"

# 支持的 LLM 供应商。两家都提供「OpenAI 兼容接口」，
# 所以我们用同一套 openai SDK 代码，只换 base_url 就能切换。
#
# 关于模型名：两家的模型 ID 换得很快（DeepSeek 从 deepseek-chat 换成
# deepseek-flash，Gemini 从 2.5 一路到 3.8），所以网页上的模型是「可自由输入」
# 的，下面这个列表只是下拉建议，填别的也行。
#
# 关于 reasoning_effort：这是 OpenAI 兼容层的标准参数，两家都支持，
# 但取值范围不同，而且各家会把它映射到自己的原生参数
# （Gemini 3.x → thinking_level，Gemini 2.5 → thinking_budget，
#  DeepSeek → thinking 模式档位）。用这个标准参数的好处是，
# 各家改原生参数名时我们不用跟着改。
PROVIDERS: dict[str, Any] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "default_model": "deepseek-flash",
        "key_url": "https://platform.deepseek.com/api_keys",
        "efforts": ["", "none", "low", "high", "max"],
        "effort_note_zh": "DeepSeek 默认 high。选 none 关闭思考——回答更快更便宜，"
                          "简单查询够用。注意：思考模式下不支持指名工具调用，"
                          "本项目用的是自动模式，不受影响。",
        "effort_note_en": "DeepSeek defaults to high. Choose none to disable thinking — "
                          "faster and cheaper, fine for simple lookups. Note: named tool "
                          "choice is unsupported while thinking, but this app uses auto mode.",
        "extra_body_example": '{"thinking": {"type": "disabled"}}',
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models": [
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ],
        "default_model": "gemini-3.8-flash",
        "key_url": "https://aistudio.google.com/apikey",
        "efforts": ["", "none", "minimal", "low", "medium", "high"],
        "effort_note_zh": "none 只有 Gemini 2.5 Flash 系列支持；2.5 Pro 和 3.x "
                          "系列不能关闭思考，传 none 会报错。",
        "effort_note_en": "none works only on the Gemini 2.5 Flash line; 2.5 Pro and the "
                          "3.x models cannot disable thinking and will reject it.",
        "extra_body_example":
            '{"google": {"thinking_config": {"thinking_level": "low", '
            '"include_thoughts": true}}}',
    },
}

DEFAULTS: dict[str, Any] = {
    "provider": "deepseek",
    "model": "deepseek-flash",
    "api_key": "",
    "database_url": "postgresql://mda_viewer@localhost:5432/mda",
    "max_tool_rounds": 12,
    # 界面语言，同时决定 LLM 用哪种语言回答（给客户演示时切 en）
    "language": "zh",
    # 推理档位。空字符串 = 不传这个参数，用供应商自己的默认值
    "reasoning_effort": "",
    # 单次回复的输出 token 上限。思考 token 也算在输出里，所以这就是
    # 「限制 thinking token」最直接有效的手段。0 = 不限制
    "max_output_tokens": 0,
    # 高级：直接透传给 API 的原生参数（JSON）。留空则不传。
    # 用来对付 reasoning_effort 表达不了的情况，比如指定确切的 thinking_budget
    "extra_body": "",
    # 是否在界面上显示模型的思考过程
    "show_thinking": True,
}


class ConfigInvalid(Exception):
    """提交的配置有问题，不该写盘。"""


def _fernet() -> Fernet:
    """取出（或首次生成）加密密钥。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        # 先建成 600 权限的空文件，再写内容，避免中间有一瞬间是可读的
        KEY_FILE.touch(mode=0o600)
        KEY_FILE.write_bytes(Fernet.generate_key())
    os.chmod(KEY_FILE, 0o600)
    return Fernet(KEY_FILE.read_bytes())


def _encrypt(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode()).decode()


def _decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        # secret.key 被换掉或 config.json 被改坏了，当作「没配 key」处理
        return ""


def load() -> dict[str, Any]:
    """读出完整配置，api_key 已解密。仅供后端内部使用，绝不返回给前端。"""
    config = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            stored = {}
        stored["api_key"] = _decrypt(stored.get("api_key", ""))
        config.update({k: v for k, v in stored.items() if k in DEFAULTS})

    # 环境变量优先级最高，方便临时覆盖或在服务器上部署
    if env_key := os.environ.get("MDA_LLM_API_KEY"):
        config["api_key"] = env_key
    if env_db := os.environ.get("MDA_DATABASE_URL"):
        config["database_url"] = env_db

    return config


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """更新配置并写盘。只接受 DEFAULTS 里定义过的字段。"""
    config = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            config.update(json.loads(CONFIG_FILE.read_text()))
        except json.JSONDecodeError:
            pass

    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        if key == "api_key":
            # 前端传空字符串表示「不改动现有 key」，不是「清空」
            if value:
                config["api_key"] = _encrypt(value)
        else:
            config[key] = value

    # 换了供应商、但这次没顺手指定模型时，切到新供应商的默认模型。
    # （模型名是自由输入的，所以不做「必须在列表里」的校验，
    #   否则你想用一个刚发布的新模型就用不了了。）
    provider = config.get("provider")
    if provider in PROVIDERS and "provider" in updates and "model" not in updates:
        config["model"] = PROVIDERS[provider]["default_model"]

    # 换供应商时，如果原来的 effort 档位新供应商不认（比如 DeepSeek 的
    # "max" 到了 Gemini 就无效），清空成「用默认值」而不是留个会报错的值
    if provider in PROVIDERS:
        if config.get("reasoning_effort") not in PROVIDERS[provider]["efforts"]:
            config["reasoning_effort"] = ""

    # extra_body 必须是合法 JSON 对象，不然每次提问都会在同一个地方报错
    if config.get("extra_body", "").strip():
        try:
            parsed = json.loads(config["extra_body"])
            if not isinstance(parsed, dict):
                raise ValueError("必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as exc:
            raise ConfigInvalid(f"高级参数不是合法的 JSON 对象：{exc}") from exc

    config["max_output_tokens"] = max(0, int(config.get("max_output_tokens") or 0))
    if config.get("language") not in ("zh", "en"):
        config["language"] = "zh"

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.touch(mode=0o600)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    os.chmod(CONFIG_FILE, 0o600)

    return public_view()


def clear_api_key() -> dict[str, Any]:
    """真正删掉已保存的 Key（save() 里空字符串表示「不改动」，所以要单独一个函数）。"""
    if not CONFIG_FILE.exists():
        return public_view()
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError:
        config = dict(DEFAULTS)
    config["api_key"] = ""
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    return public_view()


def public_view() -> dict[str, Any]:
    """给前端看的版本：不含 Key 本体，只说明「配了没有」和末尾几位。"""
    config = load()
    api_key = config.pop("api_key", "")
    return {
        **config,
        "api_key_set": bool(api_key),
        "api_key_hint": f"{api_key[:6]}……{api_key[-4:]}" if len(api_key) > 12 else "",
        "api_key_from_env": bool(os.environ.get("MDA_LLM_API_KEY")),
        "config_path": str(CONFIG_FILE),
        "providers": PROVIDERS,
    }


def provider_config(config: dict[str, Any]) -> dict[str, Any]:
    return PROVIDERS.get(config.get("provider", ""), PROVIDERS["deepseek"])
