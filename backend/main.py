"""FastAPI 后端：网页和 MCP Server / LLM 之间的中间层。

启动：
    uv run uvicorn backend.main:app --reload --port 8000
然后浏览器打开 http://localhost:8000

对外接口：
    GET  /                  网页本体
    GET  /api/config        读配置（不含 Key 本体）
    POST /api/config        存配置
    POST /api/config/test   测试 LLM Key 是否可用
    DELETE /api/config/key  删除已存的 Key
    GET  /api/status        MCP / 数据库连接状态
    POST /api/chat          提问，SSE 流式返回
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import agent, config_store, history
from .mcp_bridge import bridge

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时拉起 MCP Server，关闭时收拾干净。"""
    config = config_store.load()
    try:
        await bridge.start(config["database_url"])
        print(f"✓ MCP Server 已连接，可用工具 {len(bridge.tools)} 个")
    except Exception as exc:
        # 连不上也要让网页起来，否则用户没法进设置页去改配置
        print(f"✗ MCP Server 启动失败：{exc}\n  网页仍可访问，请到设置页检查数据库地址。")
    yield
    await bridge.stop()


app = FastAPI(title="MDA 数据库问答", lifespan=lifespan)


# ---------------------------------------------------------------- 请求体模型


class ConfigUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    # 空字符串表示「不改动已存的 Key」，不是「清空」（清空请用 DELETE 接口）
    api_key: str | None = None
    database_url: str | None = None
    max_tool_rounds: int | None = Field(default=None, ge=1, le=30)
    language: str | None = None
    # 推理控制。effort 允许空字符串（= 用供应商默认值），
    # 所以这里不能用 None 当「没传」的标记，得靠 exclude_unset 区分。
    reasoning_effort: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=0, le=400_000)
    extra_body: str | None = None
    show_thinking: bool | None = None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    # 传了 conversation_id 就接着那个会话聊，上下文从数据库取；
    # 不传就新建一个会话。
    conversation_id: str | None = None
    language: str = "zh"


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------- 配置接口


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return config_store.public_view()


@app.post("/api/config")
async def post_config(update: ConfigUpdate) -> dict[str, Any]:
    # exclude_unset 而不是 exclude_none：reasoning_effort 的空字符串是有意义的
    # 取值（表示「用供应商默认档位」），必须能传过来
    provided = update.model_dump(exclude_unset=True)
    provided = {k: v for k, v in provided.items() if v is not None}

    if provided.get("provider") and provided["provider"] not in config_store.PROVIDERS:
        raise HTTPException(400, f"不认识的供应商：{provided['provider']}")

    old_db_url = config_store.load()["database_url"]
    try:
        public = config_store.save(provided)
    except config_store.ConfigInvalid as exc:
        raise HTTPException(400, str(exc)) from exc

    # 数据库地址改了，要重启 MCP Server 才生效
    new_db_url = config_store.load()["database_url"]
    if new_db_url != old_db_url:
        try:
            await bridge.start(new_db_url)
            public["mcp_restarted"] = True
        except Exception as exc:
            public["mcp_error"] = f"新的数据库地址连接失败：{exc}"

    return public


@app.post("/api/config/test")
async def test_config(update: ConfigUpdate) -> dict[str, Any]:
    """测试 LLM 连接。

    如果表单里填了新 Key 就用新 Key 测（这样能先测再存），
    没填就用已保存的。
    """
    config = config_store.load()
    provided = update.model_dump(exclude_unset=True)
    # 表单里填了什么就用什么测，这样能「先测再存」，
    # 推理参数也一起测——否则你存下一个模型不接受的 effort 才发现就晚了
    for field in ("api_key", "provider", "model", "reasoning_effort",
                  "max_output_tokens", "extra_body"):
        if provided.get(field) is not None:
            config[field] = provided[field]

    try:
        return await agent.check_credentials(config)
    except agent.ConfigError as exc:
        return {"ok": False, "message": str(exc)}


@app.delete("/api/config/key")
async def delete_key() -> dict[str, Any]:
    return config_store.clear_api_key()


# ---------------------------------------------------------------- 状态接口


@app.get("/api/status")
async def status() -> dict[str, Any]:
    config = config_store.load()
    result: dict[str, Any] = {
        "mcp_connected": bridge.ready,
        "tools": [
            {"name": t.name, "description": (t.description or "")[:160]}
            for t in bridge.tools
        ],
        "database_url": config["database_url"],
        "api_key_set": bool(config["api_key"]),
        "provider": config["provider"],
        "model": config["model"],
        "reasoning_effort": config.get("reasoning_effort", ""),
        "max_output_tokens": config.get("max_output_tokens", 0),
        "language": config.get("language", "zh"),
    }

    if bridge.ready:
        raw = await bridge.call("list_schemas", {})
        try:
            schemas = json.loads(raw).get("schemas", [])
            result["schema_count"] = len(schemas)
            result["table_count"] = sum(s.get("table_count", 0) for s in schemas)
        except (json.JSONDecodeError, AttributeError):
            result["db_error"] = raw[:300]

    return result


@app.post("/api/mcp/restart")
async def restart_mcp() -> dict[str, Any]:
    config = config_store.load()
    try:
        await bridge.start(config["database_url"])
        return {"ok": True, "tools": len(bridge.tools)}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


# ---------------------------------------------------------------- 元数据索引


@app.get("/api/index")
async def index_status() -> dict[str, Any]:
    """索引状态由 MCP Server 那侧管理，所以走工具调用拿。"""
    if not bridge.ready:
        return {"exists": False, "error": "MCP Server 未连接"}
    raw = await bridge.call("metadata_index_status", {})
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"exists": False, "error": raw[:300]}


@app.post("/api/index/rebuild")
async def index_rebuild() -> dict[str, Any]:
    if not bridge.ready:
        raise HTTPException(503, "MCP Server 未连接")
    # 重建要读几十万行元数据，比默认的工具超时长，这里不设限
    raw = await bridge.call("rebuild_metadata_index", {})
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": raw[:300]}


# ---------------------------------------------------------------- 会话历史


@app.get("/api/conversations")
async def get_conversations() -> dict[str, Any]:
    return {"conversations": history.list_conversations(), "stats": history.stats()}


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str) -> dict[str, Any]:
    conv = history.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    return conv


@app.patch("/api/conversations/{conv_id}")
async def rename_conversation(conv_id: str, body: RenameRequest) -> dict[str, Any]:
    if not history.rename_conversation(conv_id, body.title):
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str) -> dict[str, Any]:
    if not history.delete_conversation(conv_id):
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


@app.delete("/api/conversations")
async def delete_all_conversations() -> dict[str, Any]:
    return {"ok": True, "deleted": history.delete_all()}


# ---------------------------------------------------------------- 聊天接口


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    """SSE 流式返回。每个事件一行 `data: {json}\\n\\n`。

    为什么用 SSE 而不是普通 POST？
    因为一次提问可能要查 4-5 轮数据库，等 30 秒才出结果体验很差。
    SSE 让你实时看到「正在搜索 income」「正在执行 SQL」，
    而且能核对它写的 SQL 对不对。
    """
    language = request.language if request.language in ("zh", "en") else "zh"

    # 上下文从数据库取，不由前端传上来——前端传的可以被篡改，
    # 而且刷新页面后前端内存里的历史就没了，数据库里的还在。
    conv_id = history.ensure_conversation(
        request.conversation_id, title=request.question[:80], language=language
    )
    past = history.llm_history(conv_id)

    history.add_message(conv_id, "user", request.question)

    async def event_stream():
        # 攒起来最后一次性入库：中途 yield 一次写一次会很慢，
        # 而且用户关掉页面时会留下半截记录
        answer, thinking, steps, usage, error = "", "", [], {}, ""

        try:
            yield f'data: {json.dumps({"type": "conversation", "id": conv_id}, ensure_ascii=False)}\n\n'

            async for event in agent.stream_answer(request.question, past, language):
                kind = event["type"]
                if kind == "delta":
                    answer += event["text"]
                elif kind == "thinking":
                    thinking += event["text"]
                elif kind in ("tool_call", "tool_result", "chart"):
                    # chart 也存进 steps：打开历史会话时能把图重新画出来，
                    # 给客户回放演示时这一点很重要
                    steps.append(event)
                elif kind == "usage":
                    usage = event["usage"]
                elif kind == "done":
                    answer = event.get("answer") or answer
                    # done 里的 thinking 是跨轮累积的全量版本，
                    # 比这里逐段攒的多了轮次分隔线，优先用它
                    thinking = event.get("thinking") or thinking
                    usage = event.get("usage") or usage
                elif kind == "error":
                    error = event["message"]
                    usage = event.get("usage") or usage

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        except asyncio.CancelledError:
            # 用户关掉页面了。已经产生的内容照样存下来，别丢
            error = error or "（用户中断）"
            raise
        except Exception as exc:
            error = f"服务端异常：{type(exc).__name__}: {exc}"
            yield f'data: {json.dumps({"type": "error", "message": error}, ensure_ascii=False)}\n\n'
        finally:
            if answer or error or steps:
                history.add_message(
                    conv_id, "assistant", answer,
                    thinking=thinking, steps=steps, usage=usage, error=error,
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 防止反向代理缓冲住流
        },
    )


# ---------------------------------------------------------------- 静态网页


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
