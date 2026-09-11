"""手动测试 MCP Server：像真正的客户端那样启动它并调用工具。

用法：uv run python test_mcp.py
"""

import asyncio
import json

from mcp import Client, StdioServerParameters


def show(title: str, result) -> None:
    print(f"\n{'=' * 70}\n▶ {title}\n{'=' * 70}")
    if result.is_error:
        print("!! 出错:", result.content)
        return
    data = result.structured_content or {}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    print(text[:1400] + ("\n... (已截断)" if len(text) > 1400 else ""))


async def main() -> None:
    # 和 Claude Desktop 启动 MCP server 的方式完全一样：跑一个子进程，通过 stdio 通话
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "mcp_server.server"],
    )

    async with Client(params) as client:
        tools = await client.list_tools()
        print("MCP Server 连接成功，提供的工具：")
        for t in tools.tools:
            print(f"  - {t.name}")

        show("1) list_schemas", await client.call_tool("list_schemas"))

        show("2) search_metadata('income')",
             await client.call_tool("search_metadata", {"keyword": "income", "limit": 8}))

        show("3) list_tables('cses_data')",
             await client.call_tool("list_tables", {"schema": "cses_data", "limit": 5}))

        show("4) describe_table(cses_data, final_HH_CSES)",
             await client.call_tool("describe_table",
                                    {"schema": "cses_data", "table": "final_HH_CSES"}))

        show("5) run_query 正常查询",
             await client.call_tool("run_query", {
                 "sql": 'SELECT count(*) AS n FROM cses_data."final_HH_CSES"'}))

        show("6) run_query 写操作应被拒绝",
             await client.call_tool("run_query", {
                 "sql": "DROP TABLE cses_data.final_HH_CSES"}))

        show("7) run_query 读文件应被拒绝",
             await client.call_tool("run_query", {
                 "sql": "SELECT pg_read_file('/etc/passwd')"}))


if __name__ == "__main__":
    asyncio.run(main())
