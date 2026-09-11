"""启动预检：把最容易卡住的几件事先查一遍。

单独成一个模块而不是塞进启动脚本，因为它需要 import 项目代码
（判断连接串、索引路径、Key 存放位置），写在 shell 里要么重复一遍这些逻辑、
要么靠内嵌 Python 堆 heredoc，两者都不好维护。

用法：
    python -m backend.preflight            只检查
    python -m backend.preflight --build-index   索引不存在时顺便建好

退出码 0 = 可以启动；非 0 = 有阻塞性问题（数据库连不上）。
API Key 没配不算阻塞——网页要能起来才能去设置页填 Key。
"""

from __future__ import annotations

import asyncio
import os
import sys

from . import config_store

# 启动脚本会把自己的文件名传进来。提示文字里不写死脚本名，
# 否则脚本改名之后这里会指向一个不存在的命令。
LAUNCHER = os.environ.get("MDA_LAUNCHER", "./mda_db_mcp_start.sh")

DB_TABLES_SQL = """
SELECT count(*) FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND has_schema_privilege(n.nspname, 'USAGE')
  AND has_table_privilege(c.oid, 'SELECT')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
"""


def ok(msg: str) -> None:
    print(f"  {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def die(msg: str) -> None:
    print(f"\n✗ {msg}", file=sys.stderr)
    sys.exit(1)


async def check_database(dsn: str) -> None:
    import asyncpg

    try:
        conn = await asyncpg.connect(dsn, timeout=8)
    except Exception as exc:
        die(
            f"数据库连不上：{type(exc).__name__}: {exc}\n"
            f"  连接串：{dsn}\n"
            f"  排查：  psql -d mda -c 'select 1'\n"
            f"  密码走 ~/.pgpass，不需要写在连接串里。\n"
            f"  换连接串：MDA_DATABASE_URL=... {LAUNCHER}"
        )
    try:
        who = await conn.fetchval("SELECT current_user")
        tables = await conn.fetchval(DB_TABLES_SQL)
        readonly = await conn.fetchval("SHOW default_transaction_read_only")
    finally:
        await conn.close()

    ok(f"数据库已连接（{who} 可见 {tables} 张表）")
    if readonly != "on":
        # 不阻塞启动：代码里还有只读事务和 SQL 白名单两层防护，
        # 但这说明连接用的角色权限比预期宽，值得提醒。
        warn(f"这个角色不是只读的（default_transaction_read_only={readonly}）。"
             "建议改用只读角色连接。")


async def check_index(build: bool) -> None:
    from mcp_server import db, index

    info = index.info()
    if info.get("exists"):
        note = "（已较旧，建议到设置页重建）" if info.get("stale") else ""
        ok(f"元数据索引：{info.get('total_rows', 0):,} 条，"
           f"{info.get('age_days', '?')} 天前建立{note}")
        skipped = info.get("skipped") or {}
        if skipped:
            warn(f"索引未覆盖：{', '.join(skipped)}（数据库权限不足）")
        return

    if not build:
        warn("元数据索引不存在，变量搜索会不可用。"
             f"启动后到设置页点「重建索引」，或用 {LAUNCHER} 自动建立。")
        return

    ok("元数据索引不存在，正在建立（约 10 秒，变量搜索依赖它）…")
    try:
        built = await index.build()
        ok(f"索引建立完成：{built['total_rows']:,} 条")
        if built.get("errors"):
            warn(f"部分来源失败：{built['errors'][0]}")
    except Exception as exc:
        warn(f"索引建立失败（{type(exc).__name__}: {exc}）。"
             "服务仍可启动，之后到设置页重建。")
    finally:
        await db.close_pool()


def check_api_key() -> None:
    config = config_store.load()
    if config.get("api_key"):
        src = "环境变量" if os.environ.get("MDA_LLM_API_KEY") else "本地配置"
        ok(f"API Key 已配置（{config['provider']} / {config['model']}，来自{src}）")
    else:
        warn("还没配置 API Key —— 启动后在网页「设置」页填入")
        print("      DeepSeek: https://platform.deepseek.com/api_keys")
        print("      Gemini:   https://aistudio.google.com/apikey")


async def main() -> None:
    build = "--build-index" in sys.argv
    config = config_store.load()
    await check_database(config["database_url"])
    await check_index(build)
    check_api_key()


if __name__ == "__main__":
    asyncio.run(main())
