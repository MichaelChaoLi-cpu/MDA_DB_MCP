"""数据库访问层：连接池 + 所有 SQL 查询。

设计要点：
1. 只读。连接用的角色（mda_viewer）在 PostgreSQL 层面就没有写权限，
   我们额外再用只读事务 + 语句超时兜底。
2. 这个库有 1100+ 张表、6 万+ 个列，绝不能把表结构整体喂给 LLM。
   所以核心是 search_metadata()：靠列注释搜索定位，再逐表深入。
"""

from __future__ import annotations

import datetime
import decimal
import ipaddress
import json
import os
import uuid
from typing import Any

import asyncpg

# 默认连接串。密码不写在这里——asyncpg 会自动去 ~/.pgpass 找。
DEFAULT_DSN = "postgresql://mda_viewer@localhost:5432/mda"

# 系统 schema，永远排除
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")

# 单条查询最长执行时间，防止一个笛卡尔积把数据库拖死
STATEMENT_TIMEOUT_MS = 30_000

# run_query 单次最多返回多少行（LLM 读不了几千行，也没必要）
MAX_ROWS = 500


def dsn() -> str:
    """连接串来自环境变量，方便 Claude Desktop / 后端分别配置。"""
    return os.environ.get("MDA_DATABASE_URL", DEFAULT_DSN)


_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """惰性创建连接池，全进程复用。"""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn(),
            min_size=1,
            max_size=4,
            command_timeout=60,
            # 每条连接一建立就锁定成只读，这是第二层保险
            server_settings={"default_transaction_read_only": "on"},
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------- 类型转换


def jsonable(value: Any) -> Any:
    """把 asyncpg 返回的 Python 对象转成能塞进 JSON 的形式。

    数据库里有 date / numeric / uuid / jsonb 等类型，直接 json.dumps 会报错。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        # 用 float 会丢精度，转成字符串最稳妥
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (uuid.UUID, ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} 字节二进制>"
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, asyncpg.Record):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, asyncpg.Range):
        return str(value)
    return str(value)


def rows_to_dicts(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [{k: jsonable(v) for k, v in r.items()} for r in records]


# ---------------------------------------------------------------- 元数据查询

# 只列出当前角色真的有权限读的表。
# 注意：pg_catalog 里的元数据是所有人可读的，所以搜索能搜到 mng_* 这些
# 无权访问的 schema。如果不过滤，LLM 会写出一条注定报权限错误的 SQL。
_ACCESSIBLE = """
    has_schema_privilege(n.nspname, 'USAGE')
    AND has_table_privilege(c.oid, 'SELECT')
"""

SQL_LIST_SCHEMAS = f"""
SELECT n.nspname AS schema,
       count(*) AS table_count,
       pg_size_pretty(sum(pg_total_relation_size(c.oid))) AS total_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname NOT IN {SYSTEM_SCHEMAS}
  AND n.nspname NOT LIKE 'pg_%'
  AND {_ACCESSIBLE}
GROUP BY n.nspname
ORDER BY n.nspname
"""

SQL_LIST_TABLES = f"""
SELECT n.nspname AS schema,
       c.relname AS table,
       CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned table'
                      WHEN 'v' THEN 'view'  WHEN 'm' THEN 'materialized view' END AS kind,
       -- reltuples 是统计信息里的估算行数，比 count(*) 快无数倍
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS approx_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
       obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname = $1
  AND ($2::text IS NULL OR c.relname ILIKE '%' || $2 || '%')
  AND {_ACCESSIBLE}
ORDER BY c.relname
LIMIT $3
"""

SQL_DESCRIBE_COLUMNS = """
SELECT a.attnum AS position,
       a.attname AS column,
       format_type(a.atttypid, a.atttypmod) AS type,
       NOT a.attnotnull AS nullable,
       pg_get_expr(d.adbin, d.adrelid) AS default,
       col_description(a.attrelid, a.attnum) AS comment
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = $1::regclass
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum
"""

SQL_DESCRIBE_CONSTRAINTS = """
SELECT con.conname AS name,
       CASE con.contype WHEN 'p' THEN 'PRIMARY KEY' WHEN 'f' THEN 'FOREIGN KEY'
                        WHEN 'u' THEN 'UNIQUE' WHEN 'c' THEN 'CHECK' END AS type,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
WHERE con.conrelid = $1::regclass
  AND con.contype IN ('p', 'f', 'u')
ORDER BY con.contype, con.conname
"""

SQL_DESCRIBE_INDEXES = """
SELECT indexname AS name, indexdef AS definition
FROM pg_indexes
WHERE schemaname = $1 AND tablename = $2
ORDER BY indexname
"""

SQL_TABLE_INFO = """
SELECT n.nspname AS schema,
       c.relname AS table,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END AS approx_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
       obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = $1::regclass
"""

# 搜索的四条路：表名、表注释、列名、列注释。
# 列注释那一路最重要——这个库的列名是 a170201 这种编码，语义全在注释里。
SQL_SEARCH = f"""
WITH hits AS (
    SELECT n.nspname AS schema, c.relname AS table, NULL::text AS column,
           'table_name' AS matched_on,
           obj_description(c.oid, 'pg_class') AS comment, 1 AS rank
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r','p','v','m') AND n.nspname NOT IN {SYSTEM_SCHEMAS}
      AND c.relname ILIKE '%' || $1 || '%' AND {_ACCESSIBLE}

    UNION ALL
    SELECT n.nspname, c.relname, NULL, 'table_comment',
           obj_description(c.oid, 'pg_class'), 2
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r','p','v','m') AND n.nspname NOT IN {SYSTEM_SCHEMAS}
      AND obj_description(c.oid, 'pg_class') ILIKE '%' || $1 || '%' AND {_ACCESSIBLE}

    UNION ALL
    SELECT n.nspname, c.relname, a.attname, 'column_name',
           col_description(c.oid, a.attnum), 3
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m')
      AND n.nspname NOT IN {SYSTEM_SCHEMAS}
      AND a.attname ILIKE '%' || $1 || '%' AND {_ACCESSIBLE}

    UNION ALL
    SELECT n.nspname, c.relname, a.attname, 'column_comment',
           col_description(c.oid, a.attnum), 4
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m')
      AND n.nspname NOT IN {SYSTEM_SCHEMAS}
      AND col_description(c.oid, a.attnum) ILIKE '%' || $1 || '%' AND {_ACCESSIBLE}
)
SELECT schema, "table", "column", matched_on, left(comment, 200) AS comment
FROM hits
WHERE ($2::text IS NULL OR schema = $2)
ORDER BY rank, schema, "table", "column"
LIMIT $3
"""


async def fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
    """执行一条内部元数据查询。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch(sql, *args)
    return rows_to_dicts(records)


async def fetch_one(sql: str, *args: Any) -> dict[str, Any] | None:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


# ---------------------------------------------------------------- 用户 SQL

# 只允许这两个开头。CTE（WITH）要允许，因为复杂分析查询都用它。
ALLOWED_PREFIXES = ("select", "with", "table", "explain", "values")

# 即使在只读事务里也该明确挡掉的关键字（防止 SELECT 里塞函数做写操作）
FORBIDDEN_TOKENS = (
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export",
    "pg_terminate_backend", "pg_cancel_backend", "dblink", "copy ",
)


class QueryRejected(Exception):
    """SQL 没通过安全检查。"""


def check_sql(sql: str) -> str:
    """在把 SQL 交给数据库之前做一轮检查。

    数据库层面已经是只读了，这里挡的是"能读但不该读"的东西，
    比如通过 pg_read_file 读服务器上的文件。
    """
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise QueryRejected("SQL 是空的。")

    lowered = cleaned.lower()

    if not lowered.startswith(ALLOWED_PREFIXES):
        raise QueryRejected(
            f"只允许只读查询（开头必须是 {'/'.join(p.upper() for p in ALLOWED_PREFIXES)}），"
            f"收到的是：{cleaned.split()[0]!r}"
        )

    # 分号后面还有内容 => 多语句，拒绝
    if ";" in cleaned:
        raise QueryRejected("不允许一次执行多条语句，请去掉中间的分号。")

    for token in FORBIDDEN_TOKENS:
        if token in lowered:
            raise QueryRejected(f"SQL 里包含被禁止的函数或语句：{token.strip()!r}")

    return cleaned


async def run_query(sql: str, limit: int = 100) -> dict[str, Any]:
    """执行一条只读查询，返回 {columns, rows, row_count, truncated}。

    rows 是「数组的数组」而不是字典列表——列名只出现一次，
    给 LLM 省下大量 token。
    """
    cleaned = check_sql(sql)
    limit = max(1, min(int(limit), MAX_ROWS))

    pool = await get_pool()
    async with pool.acquire() as conn:
        # readonly=True：万一角色权限被改宽，这里还是只读
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            # 用游标而不是 fetch()：这个库里有几百 MB 的表，
            # 一条不带 LIMIT 的查询如果全量拉回来会直接把内存打满。
            # 游标只从数据库取 limit+1 行，多的那一行用来判断是否被截断。
            cursor = await conn.cursor(cleaned)
            records = await cursor.fetch(limit + 1)

    truncated = len(records) > limit
    records = records[:limit]

    columns = list(records[0].keys()) if records else []
    return {
        "columns": columns,
        "rows": [[jsonable(v) for v in r.values()] for r in records],
        "row_count": len(records),
        "truncated": truncated,
        "note": (
            f"结果超过 {limit} 行已被截断，请加 LIMIT 或用聚合函数缩小范围。"
            if truncated else None
        ),
    }
