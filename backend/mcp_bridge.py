"""MCP 桥接层：后端如何跟 MCP Server 对话。

后端在这里扮演 MCP「客户端」的角色，和 Claude Desktop 做的事情完全一样：
  1. 启动 mcp_server 子进程
  2. 问它「你有哪些工具」
  3. 把工具清单翻译成 LLM 认得的 function calling 格式
  4. LLM 决定调哪个工具时，转发给 MCP Server 执行

第 3 步是关键：工具描述是自动翻译的，
所以你以后往 mcp_server/server.py 里加新工具，网页会自动获得新能力，
前后端代码一行都不用改。
"""

from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, TextContent


class MCPBridge:
    """维护一个长期存活的 MCP 会话。"""

    def __init__(self) -> None:
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self.tools: list[Any] = []
        self.instructions: str = ""

    async def start(self, database_url: str) -> None:
        """启动 MCP Server 子进程并握手。"""
        await self.stop()

        params = StdioServerParameters(
            command=sys.executable,          # 用当前虚拟环境的 python
            args=["-m", "mcp_server.server"],
            env={"MDA_DATABASE_URL": database_url},
        )

        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(Client(params))

        listed = await self._client.list_tools()
        self.tools = list(listed.tools)
        self.instructions = self._client.instructions or ""

    async def stop(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass  # 子进程可能已经退出了，忽略
        self._stack = None
        self._client = None
        self.tools = []

    @property
    def ready(self) -> bool:
        return self._client is not None

    def openai_tools(self) -> list[dict[str, Any]]:
        """把 MCP 工具清单翻译成 OpenAI function calling 的格式。

        MCP 的 input_schema 本身就是 JSON Schema，主要就是套一层壳，
        但必须先过一遍 sanitize_schema()——原因见那个函数的说明。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": sanitize_schema(
                        tool.input_schema or {"type": "object", "properties": {}}
                    ),
                },
            }
            for tool in self.tools
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """执行一个工具，返回给 LLM 看的文本结果。"""
        if self._client is None:
            return json.dumps({"error": "MCP Server 未连接"}, ensure_ascii=False)

        try:
            result: CallToolResult = await self._client.call_tool(name, arguments)
        except Exception as exc:
            # 不要抛出去——把错误交给 LLM，它往往能自己改对再试一次
            return json.dumps(
                {"error": f"调用工具 {name} 失败: {type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )

        # 优先用结构化结果，它比纯文本更紧凑
        if result.structured_content is not None:
            payload = _drop_nulls(result.structured_content)
            return json.dumps(payload, ensure_ascii=False, default=str)

        texts = [c.text for c in result.content if isinstance(c, TextContent)]
        return "\n".join(texts) if texts else "(工具没有返回内容)"


# Gemini 不接受的 JSON Schema 关键字。DeepSeek 比较宽松，Gemini 会直接报
# 400 Invalid JSON payload，所以统一按更严格的那一家来清洗。
_SCHEMA_NOISE = ("title", "default", "$schema", "additionalProperties", "examples")


def sanitize_schema(schema: Any) -> Any:
    """把 Pydantic 生成的 JSON Schema 改造成两家 LLM 都认的形式。

    Pydantic 对 `str | None` 会生成
        {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}
    DeepSeek 能吃下，但 Gemini 的 function calling 不支持 anyOf，会直接 400。
    这里做三件事：
      1. anyOf/oneOf 里如果只是「某类型 + null」，压平成那个类型本身
      2. 删掉 title / default 等 Gemini 不认的关键字
      3. 递归处理嵌套的 properties 和 items
    可选参数不写进 required，LLM 自然知道可以不传，所以压平不丢信息。
    """
    if isinstance(schema, list):
        return [sanitize_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema

    result = {k: v for k, v in schema.items() if k not in _SCHEMA_NOISE}

    for combiner in ("anyOf", "oneOf"):
        if combiner in result:
            branches = [
                b for b in result.pop(combiner)
                if not (isinstance(b, dict) and b.get("type") == "null")
            ]
            if len(branches) == 1:
                # 只剩一个分支：把它的内容并进当前层
                merged = sanitize_schema(branches[0])
                if isinstance(merged, dict):
                    result = {**merged, **result}
            elif branches:
                # 真的是多类型联合，保留下来（本项目暂时没有这种情况）
                result[combiner] = [sanitize_schema(b) for b in branches]

    if isinstance(result.get("properties"), dict):
        result["properties"] = {
            name: sanitize_schema(sub) for name, sub in result["properties"].items()
        }
    if "items" in result:
        result["items"] = sanitize_schema(result["items"])

    return result


def _drop_nulls(value: Any) -> Any:
    """删掉值为 None 的字段，省 token。

    describe_table 一张 86 列的表会返回大量 "default": null,
    这些对 LLM 毫无信息量，但要占掉几千 token。
    """
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


# 全进程共用一个桥
bridge = MCPBridge()
