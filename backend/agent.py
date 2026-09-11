"""Agent 循环：LLM 和 MCP 工具之间的来回对话。

一次提问的完整流程（这就是所谓「agent loop」）：

    用户: "2019 年有多少户参与调查？"
      ↓
    LLM: 我不知道表在哪，先调 search_metadata("household")
      ↓  (我们把请求转发给 MCP Server，把结果塞回对话)
    LLM: 找到 cses_data.final_HH_CSES，调 describe_table 看列
      ↓
    LLM: 列名清楚了，调 run_query 执行 SQL
      ↓
    LLM: 拿到数字，用中文（或英文）回答用户

循环可能转好几轮，所以设了 max_tool_rounds 上限防止无限打转。
每一步都通过 SSE 实时推给前端。

关于事件里的 *_key 字段：
  推给前端的进度不写死中文，只给一个 key 加上参数，由前端按当前
  界面语言渲染。这样切英文时，连「返回 3 行」这类提示也是英文的。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import APIStatusError, AsyncOpenAI

from . import config_store
from .mcp_bridge import bridge

# 给 LLM 的语言指令。这是「给客户看英文」这个需求的关键——
# 界面切英文的同时，模型的回答也要是英文。
LANGUAGE_RULE = {
    "zh": "用简体中文回答。",
    "en": "Answer in English. Do not use Chinese in your reply, "
          "even if the database comments or the question contain Chinese.",
}

SYSTEM_PROMPT = """\
你是一位社会经济调查微观数据的分析助手，通过工具访问一个只读的 PostgreSQL 数据库。

{mcp_instructions}

回答要求：
- {language_rule}
- 先动手查，不要猜。任何具体数字都必须来自 run_query 的真实结果，
  绝对不允许凭印象编造数字。
- 报告结果时，说明数据来自哪张表、口径是什么（哪一年、哪个人群、是否加权）。
- 如果查出来的结果可疑（比如平均年龄 98 岁），很可能是把缺失值编码当数值算了，
  回头看列注释确认，不要把明显错误的结果当答案交出去。
- 如果搜不到相关数据，直接说没找到，并说明你搜过哪些关键词，
  不要用看似合理的推测填补空白。
- 涉及总体统计量时使用抽样权重加权，并在回答中说明用了哪个权重列。
"""


class ConfigError(Exception):
    """配置不完整，没法调用 LLM。"""


def build_client(config: dict[str, Any]) -> AsyncOpenAI:
    """DeepSeek 和 Gemini 都提供 OpenAI 兼容接口，所以同一个 SDK 换个地址就行。"""
    if not config.get("api_key"):
        raise ConfigError("还没有配置 API Key，请先到「设置」页面填写。")

    provider = config_store.provider_config(config)
    return AsyncOpenAI(
        api_key=config["api_key"],
        base_url=provider["base_url"],
        timeout=180.0,     # 开了高档思考的模型第一个 token 可能要等很久
        max_retries=1,
    )


def tuning_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """把网页上的推理设置翻译成 API 参数。

    三个旋钮，作用范围不同：

    reasoning_effort   两家通用的标准档位。各家自己映射到原生参数
                       （Gemini 3.x → thinking_level，Gemini 2.5 → thinking_budget，
                        DeepSeek → thinking 模式档位）。用标准参数的好处是
                       各家改原生参数名时我们不用跟着改。
    max_completion_tokens
                       输出 token 上限。**思考 token 也算在输出里**，
                       所以这是限制 thinking 开销最直接的手段。
    extra_body         原生参数直通车。上面两个表达不了的时候用，
                       比如指定确切的 thinking_budget 数值。
    """
    kwargs: dict[str, Any] = {}

    if effort := (config.get("reasoning_effort") or "").strip():
        kwargs["reasoning_effort"] = effort

    if limit := int(config.get("max_output_tokens") or 0):
        kwargs["max_completion_tokens"] = limit

    if raw := (config.get("extra_body") or "").strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                kwargs["extra_body"] = parsed
        except json.JSONDecodeError:
            pass   # 存的时候已经校验过了，这里兜底忽略

    return kwargs


def _delta_reasoning(delta: Any) -> str:
    """取出这一小段思考内容。

    各家字段名不统一，而且都不在 openai SDK 的类型定义里，
    所以从 model_extra 里按几个可能的名字找。
    """
    for field in ("reasoning_content", "reasoning", "thought"):
        value = getattr(delta, field, None)
        if value is None and hasattr(delta, "model_extra"):
            value = (delta.model_extra or {}).get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_usage(usage: Any) -> dict[str, Any]:
    """把 usage 对象抹平成好显示的字典，重点是把思考 token 单独拎出来。"""
    if usage is None:
        return {}

    data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    details = data.get("completion_tokens_details") or {}

    result = {
        "prompt_tokens": data.get("prompt_tokens"),
        "completion_tokens": data.get("completion_tokens"),
        "total_tokens": data.get("total_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "cached_tokens": (data.get("prompt_tokens_details") or {}).get("cached_tokens")
                         or data.get("prompt_cache_hit_tokens"),
    }
    return {k: v for k, v in result.items() if v}


def _merge_usage(total: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """多轮工具调用会产生多次 API 请求，token 要累加。"""
    for key, value in new.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value
    return total


async def check_credentials(config: dict[str, Any]) -> dict[str, Any]:
    """「测试连接」按钮用：发一个最小请求，同时验证推理参数能不能被接受。"""
    client = build_client(config)
    provider = config_store.provider_config(config)
    tuning = tuning_kwargs(config)

    try:
        response = await client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "user", "content": "Reply with the two letters: ok"}],
            **tuning,
        )
    except APIStatusError as exc:
        return {
            "ok": False,
            "message": _explain_api_error(exc, provider["label"], tuning),
            "status": exc.status_code,
        }
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    reply = (response.choices[0].message.content or "").strip()
    usage = _extract_usage(getattr(response, "usage", None))

    told = []
    if "reasoning_effort" in tuning:
        told.append(f"effort={tuning['reasoning_effort']}")
    if "max_completion_tokens" in tuning:
        told.append(f"输出上限={tuning['max_completion_tokens']}")
    if "extra_body" in tuning:
        told.append("高级参数已生效")

    return {
        "ok": True,
        "message": f"{provider['label']} / {config['model']} 连接正常"
                   + (f"（{', '.join(told)}）" if told else "")
                   + f"，模型回复：{reply!r}",
        "usage": usage,
    }


def _explain_api_error(
    exc: APIStatusError, provider_label: str, tuning: dict[str, Any] | None = None
) -> str:
    """把 HTTP 状态码翻译成人话——小白最容易卡在这里。"""
    detail = ""
    try:
        body = exc.response.json()
        detail = body.get("error", {}).get("message", "") or str(body)[:300]
    except Exception:
        detail = (exc.response.text or "")[:300]

    # 先看错误原文再看状态码。原因：Gemini 对「Key 无效」返回的是 400 而不是
    # 401，光看状态码会把用户引到「去改模型名」的错误方向上。
    lowered = detail.lower()
    tuning = tuning or {}

    if "api key" in lowered or "api_key" in lowered:
        hint = f"API Key 无效或已撤销，请到 {provider_label} 控制台确认后重新粘贴。"
    elif "quota" in lowered or "billing" in lowered or "insufficient" in lowered:
        hint = "额度或余额不足，请检查账户。"
    elif any(k in lowered for k in ("thinking", "reasoning", "effort", "thought")):
        # 用户明确设了推理参数，就明确告诉他是这个参数被拒了，
        # 不要偷偷丢掉参数重试——那样他会以为设置生效了。
        hint = ("推理设置被这个模型拒绝了。常见原因：把 reasoning_effort 设成 none，"
                "但 Gemini 2.5 Pro / 3.x 系列不允许关闭思考；"
                "或者 reasoning_effort 和高级参数里的 thinking_level/thinking_budget "
                "同时设置了（两者互斥，只能用一个）。"
                f"当前设置：{tuning}")
    elif "model" in lowered and any(
        k in lowered for k in ("not found", "not exist", "unsupported", "invalid")
    ):
        hint = "模型名不被支持。两家的模型 ID 换得很快，去控制台确认当前可用的名字。"
    elif "max_completion_tokens" in lowered or "max_tokens" in lowered:
        hint = "输出上限设得不合法（太小或超过模型允许的最大值），改一下或设成 0 表示不限制。"
    else:
        hint = {
            401: f"API Key 无效或已撤销，请到 {provider_label} 控制台重新生成。",
            403: "Key 没有访问该模型的权限，或所在地区不支持。",
            404: "模型名写错了，换一个模型试试。",
            429: "请求太频繁，或余额 / 免费额度已用尽。",
            400: "请求被拒绝——可能是模型名不对，或该模型不支持工具调用。",
            500: "供应商服务端故障，稍后重试。",
            503: "供应商服务暂时不可用，稍后重试。",
        }.get(exc.status_code, "")

    return f"HTTP {exc.status_code}：{hint} 原始信息：{detail}"


# 图表规格里必须有的字段。缺了就不是一个能画的图，别推给前端。
_CHART_REQUIRED = ("kind", "points")


def _extract_chart(raw: str) -> dict[str, Any] | None:
    """从工具返回里取出图表规格。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    chart = data.get("chart")
    if not isinstance(chart, dict):
        return None
    if not all(k in chart for k in _CHART_REQUIRED):
        return None
    if not isinstance(chart.get("points"), list) or not chart["points"]:
        return None
    return chart


def _tool_summary(name: str, raw: str) -> tuple[str, dict[str, Any]]:
    """返回 (key, 参数)，由前端按界面语言渲染成文字。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "chars", {"n": len(raw)}

    if not isinstance(data, dict):
        return "chars", {"n": len(str(data))}
    if "error" in data:
        return "toolerror", {"message": str(data["error"])[:300]}

    if name == "run_query":
        return "rows", {
            "n": data.get("row_count", 0),
            "cols": len(data.get("columns", [])),
            "truncated": bool(data.get("truncated")),
        }
    if name == "search_metadata":
        return "hits", {
            "n": data.get("hit_count", 0),
            "tables": [t["table"] for t in (data.get("top_tables") or [])[:3]],
        }
    if name == "describe_table":
        return "cols", {
            "cols": data.get("column_count"),
            "rows": data.get("approx_rows"),
        }
    if name == "list_schemas":
        return "schemas", {"n": len(data.get("schemas", []))}
    if name == "list_tables":
        return "tables", {"n": data.get("table_count", 0)}
    if name == "sample_rows":
        return "sampled", {"n": data.get("row_count", 0)}
    if name == "plot_variable":
        return "plotted", {"n": data.get("point_count", 0),
                           "kind": (data.get("chart") or {}).get("kind", "")}
    if name == "variable_stats":
        return "varstats", {"answered": data.get("answered"),
                            "total": data.get("total_rows"),
                            "rate": data.get("answer_rate")}
    if name == "find_variables":
        return "found", {"n": data.get("match_count", 0),
                         "total": (data.get("facets") or {}).get("total_matches")}
    if name == "list_surveys":
        return "surveys", {"n": len(data.get("loaded", []))}
    if name == "list_sections":
        return "sections", {"n": len(data.get("physical_sections") or [])}
    if name == "read_guide":
        return "guide", {"n": len(data.get("guide_sections") or []),
                         "issues": len(data.get("open_data_issues") or [])}
    return "chars", {"n": len(raw)}


async def stream_answer(
    question: str,
    history: list[dict[str, str]] | None = None,
    language: str = "zh",
) -> AsyncIterator[dict[str, Any]]:
    """回答一个问题，边做边把过程 yield 出来。

    yield 出去的每个 dict 就是一个 SSE 事件，type 字段区分类型：
      status / thinking / tool_call / tool_result / delta / usage / done / error
    """
    config = config_store.load()

    try:
        client = build_client(config)
    except ConfigError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    if not bridge.ready:
        yield {"type": "error", "message": "MCP Server 未连接，请检查数据库配置。"}
        return

    provider = config_store.provider_config(config)
    tools = bridge.openai_tools()
    tuning = tuning_kwargs(config)
    show_thinking = bool(config.get("show_thinking", True))

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                mcp_instructions=bridge.instructions,
                language_rule=LANGUAGE_RULE.get(language, LANGUAGE_RULE["zh"]),
            ),
        }
    ]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    max_rounds = int(config.get("max_tool_rounds", 12))
    total_usage: dict[str, Any] = {}
    # 思考内容要跨轮累积。模型往往在前几轮（决定调什么工具时）想得最多，
    # 最后一轮反而只是把数字念出来。只留最后一轮等于把关键推理丢了。
    all_thinking: list[str] = []
    # 有的兼容层不认 stream_options，第一次被拒之后就不再传
    ask_for_usage = True

    for round_index in range(max_rounds):
        yield {"type": "status", "key": "thinking" if round_index == 0 else "continuing"}

        text_parts: list[str] = []
        think_parts: list[str] = []
        # 流式返回时，一次工具调用的 name 和 arguments 是分成很多小片段来的，
        # 要按 index 攒齐才能用。
        pending: dict[int, dict[str, str]] = {}

        for attempt in range(2):
            request: dict[str, Any] = {
                "model": config["model"],
                "messages": messages,
                "tools": tools,
                "stream": True,
                **tuning,
            }
            if ask_for_usage:
                request["stream_options"] = {"include_usage": True}

            try:
                stream = await client.chat.completions.create(**request)
                async for chunk in stream:
                    if usage := _extract_usage(getattr(chunk, "usage", None)):
                        _merge_usage(total_usage, usage)

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    if reasoning := _delta_reasoning(delta):
                        think_parts.append(reasoning)
                        if show_thinking:
                            yield {"type": "thinking", "text": reasoning}

                    if delta.content:
                        text_parts.append(delta.content)
                        yield {"type": "delta", "text": delta.content}

                    for tc in delta.tool_calls or []:
                        slot = pending.setdefault(
                            tc.index, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
                break

            except APIStatusError as exc:
                # stream_options 是我们自己加的用量统计，不是用户的意图，
                # 被拒就悄悄去掉重试一次。用户显式设的推理参数不这么处理——
                # 那样他会以为设置生效了，必须报错让他知道。
                text = (exc.response.text or "").lower()
                if attempt == 0 and ask_for_usage and "stream_options" in text:
                    ask_for_usage = False
                    continue
                yield {
                    "type": "error",
                    "message": _explain_api_error(exc, provider["label"], tuning),
                    "usage": total_usage,
                }
                return
            except Exception as exc:
                yield {
                    "type": "error",
                    "message": f"调用模型失败：{type(exc).__name__}: {exc}",
                    "usage": total_usage,
                }
                return

        answer_text = "".join(text_parts)
        if think_parts:
            all_thinking.append("".join(think_parts))

        if total_usage:
            yield {"type": "usage", "usage": dict(total_usage)}

        # 没有要调的工具，说明 LLM 已经给出最终答案了
        if not pending:
            yield {
                "type": "done",
                "answer": answer_text,
                # 多轮思考用分隔线拼起来，读的时候能看出是分几次想的
                "thinking": "\n\n———\n\n".join(all_thinking),
                "usage": dict(total_usage),
            }
            return

        calls = [pending[i] for i in sorted(pending)]
        messages.append(
            {
                "role": "assistant",
                "content": answer_text or None,
                "tool_calls": [
                    {
                        "id": c["id"] or f"call_{round_index}_{i}",
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
                    }
                    for i, c in enumerate(calls)
                ],
            }
        )

        for i, call in enumerate(calls):
            name = call["name"]
            try:
                args = json.loads(call["arguments"]) if call["arguments"].strip() else {}
            except json.JSONDecodeError:
                # LLM 偶尔会吐出不合法的 JSON，把错误还给它让它重试
                args = {}
                result = json.dumps(
                    {"error": f"参数不是合法 JSON: {call['arguments'][:200]}"},
                    ensure_ascii=False,
                )
            else:
                yield {"type": "tool_call", "name": name, "arguments": args}
                result = await bridge.call(name, args)

            summary_key, summary_args = _tool_summary(name, result)
            yield {
                "type": "tool_result",
                "name": name,
                "summary_key": summary_key,
                "summary_args": summary_args,
                "detail": result[:4000],
            }

            # 工具返回里带 chart 字段就单独推一个事件，让前端画图。
            # 不靠工具名判断（写死 plot_variable 的话，以后加新的画图工具
            # 还得改这里），而是看返回结构里有没有图表规格。
            if chart := _extract_chart(result):
                yield {"type": "chart", "name": name, "chart": chart}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"] or f"call_{round_index}_{i}",
                    "content": result,
                }
            )

    yield {
        "type": "error",
        "message": f"连续调用工具超过 {max_rounds} 轮仍未得出结论，已停止。"
                   "可以把问题拆得更具体一些再试。",
        "usage": dict(total_usage),
    }
