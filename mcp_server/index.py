"""本地元数据索引（SQLite FTS5）。

为什么要有这层？
  「找和教育年限相关的变量」这种查询，直接查数据库的话：
    - dhs_source_variable 有 130 万行，多关键词 ILIKE 要 1.4 秒
    - 只读角色装不了 pg_trgm / pgvector，也建不了索引
    - 没有词干还原：搜 education 找不到 educational
  把去重后的元数据（约 30-40 万行）抽到本地 SQLite FTS5 里之后：
    - 查询 < 10ms
    - porter 词干还原：education / educational / educated 互通
    - 内置 BM25 相关性排序
    - 数据库零负载

代价：索引是快照。新数据入库后要重建（网页设置页有按钮，或调 rebuild 工具）。
所以 search() 会一并返回索引年龄，让 LLM 能提醒你索引旧了。

注意这不是向量检索。「语义相关」靠的是 LLM 把「教育年限」展开成
education / schooling / grade / attainment 等英文同义词再来搜，
语义能力在 LLM 那一侧，这里负责快速准确地检索和排序。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import catalog, db

INDEX_DIR = Path(os.environ.get("MDA_CONFIG_DIR", Path.home() / ".mda_db_mcp"))
INDEX_FILE = INDEX_DIR / "metadata_index.db"

# 索引超过这个天数就在搜索结果里提示该重建了
STALE_DAYS = 30

# FTS5 表。UNINDEXED 的列只存不检索，省空间也免得干扰相关性。
# separators '_' 很关键：变量名是 MD_selected_child_yogurt 这种，
# 不切开的话整串算一个词，搜 child 就找不到。
_SCHEMA = """
CREATE VIRTUAL TABLE vars USING fts5(
    name, label, detail, category, section,
    project     UNINDEXED,
    level       UNINDEXED,
    container   UNINDEXED,
    n_datasets  UNINDEXED,
    has_labels  UNINDEXED,
    tokenize = "porter unicode61 separators '_'"
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# BM25 权重，按列顺序给。label 最重要（简短标签最能说明变量是什么），
# detail 次之（定义/问题原文），name 再次（编码名信息量低）。
_WEIGHTS = (2.0, 10.0, 5.0, 3.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)

_UNIFORM_COLUMNS = (
    "project", "level", "section", "container", "name",
    "label", "detail", "category", "n_datasets", "has_labels",
)


def _coerce(value: Any) -> Any:
    """SQLite 只认 None/int/float/str/bytes，其余一律转字符串。"""
    if value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


# ---------------------------------------------------------------- 建索引


async def build() -> dict[str, Any]:
    """重建索引。

    先写到 .new 文件再原子替换，这样重建过程中的搜索请求
    还能用旧索引，不会读到半成品。
    """
    started = time.time()
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(INDEX_FILE) + ".new")
    tmp.unlink(missing_ok=True)

    access = await catalog.accessible()
    conn = sqlite3.connect(tmp)
    conn.executescript(_SCHEMA)

    per_project: dict[str, dict[str, int]] = {}
    skipped: dict[str, str] = {}
    errors: list[str] = []

    pool = await db.get_pool()

    for key, project in catalog.PROJECTS.items():
        if not access.get(key):
            skipped[key] = "当前数据库角色没有读取权限"
            continue

        per_project[key] = {}
        for level, sql in project.extracts.items():
            try:
                inserted = 0
                async with pool.acquire() as pg:
                    async with pg.transaction(readonly=True):
                        # 用游标分批读：dhs source 那条 SQL 聚合后有 5 万行，
                        # 中间结果更大，一次性拉回来没必要
                        cursor = await pg.cursor(sql)
                        while True:
                            batch = await cursor.fetch(2000)
                            if not batch:
                                break
                            if inserted == 0:
                                # 第一批就把列名和顺序核对一遍。
                                # 插入是按位置做的，所以列顺序错了不会报错，
                                # 只会把 label 塞进 detail 这类静默错位——
                                # 在这里拦住比事后发现搜索结果乱掉容易得多。
                                got = tuple(batch[0].keys())
                                if got != _UNIFORM_COLUMNS:
                                    raise ValueError(
                                        f"{key}.{level} 的 SQL 输出列不对。\n"
                                        f"  期望: {_UNIFORM_COLUMNS}\n"
                                        f"  实际: {got}\n"
                                        f"  （每个输出列都要写别名，否则字面量会变成 ?column?）"
                                    )
                            conn.executemany(
                                f"INSERT INTO vars ({', '.join(_UNIFORM_COLUMNS)})"
                                f" VALUES ({', '.join('?' * len(_UNIFORM_COLUMNS))})",
                                # 按位置取值，不按列名。适配器 SQL 里的列别名容易漏写
                                # （漏了就 KeyError），而位置是由 _UNIFORM_COLUMNS
                                # 的顺序约定死的，不会错。
                                [
                                    tuple(_coerce(r[i])
                                          for i in range(len(_UNIFORM_COLUMNS)))
                                    for r in batch
                                ],
                            )
                            inserted += len(batch)
                        conn.commit()
                per_project[key][level] = inserted
            except Exception as exc:
                # 一个 level 挂了不该让整个索引建不出来——把错误记下来，继续建其余的
                errors.append(f"{key}.{level}: {type(exc).__name__}: {exc}")
                per_project[key][level] = 0

    total = conn.execute("SELECT count(*) FROM vars").fetchone()[0]
    elapsed = round(time.time() - started, 1)

    info = {
        "built_at": time.time(),
        "elapsed_seconds": elapsed,
        "total_rows": total,
        "per_project": per_project,
        "skipped": skipped,
        "errors": errors,
    }
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('info', ?)",
                 (json.dumps(info, ensure_ascii=False),))
    conn.commit()
    conn.execute("PRAGMA optimize")
    conn.close()

    os.chmod(tmp, 0o600)
    os.replace(tmp, INDEX_FILE)      # 原子替换
    return info


# ---------------------------------------------------------------- 查索引


def exists() -> bool:
    return INDEX_FILE.exists()


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{INDEX_FILE}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def info() -> dict[str, Any]:
    """索引的状态：什么时候建的、多少行、每个调查各多少。"""
    if not exists():
        return {"exists": False, "hint": "还没建索引，调 rebuild_metadata_index 建一次。"}
    try:
        with _open() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='info'").fetchone()
        data = json.loads(row["value"]) if row else {}
    except Exception as exc:
        return {"exists": True, "error": f"索引读取失败：{exc}"}

    age_days = (time.time() - data.get("built_at", 0)) / 86400
    return {
        "exists": True,
        "age_days": round(age_days, 1),
        "stale": age_days > STALE_DAYS,
        "size_mb": round(INDEX_FILE.stat().st_size / 1e6, 1),
        **data,
    }


# FTS5 的查询语法里这些字符有特殊含义，关键词里出现会导致语法错误
_FTS_UNSAFE = re.compile(r'["\'(){}\[\]^*:+\-~,]')


def _fts_query(keywords: list[str]) -> str:
    """把关键词列表拼成 FTS5 的 MATCH 表达式。

    每个词用双引号包起来当短语，OR 连接。BM25 会自动让「命中更多、
    命中更罕见词」的行排在前面，所以不需要我们自己数命中数。
    """
    terms = []
    for kw in keywords:
        cleaned = _FTS_UNSAFE.sub(" ", str(kw)).strip()
        if cleaned:
            terms.append('"' + cleaned.replace('"', "") + '"')
    return " OR ".join(terms)


def search(
    keywords: list[str],
    project: str | None = None,
    section: str | None = None,
    level: str | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    """在索引里搜变量。"""
    if not exists():
        return {
            "error": "元数据索引还没建立。",
            "hint": "先调 rebuild_metadata_index（约需 1-2 分钟），或在网页设置页点「重建元数据索引」。",
        }

    query = _fts_query(keywords)
    if not query:
        return {"error": "关键词是空的（或只包含 FTS5 保留字符）。"}

    where = ["vars MATCH ?"]
    params: list[Any] = [query]
    if project:
        where.append("project = ?")
        params.append(project)
    if section:
        where.append("section = ?")
        params.append(section)
    if level:
        where.append("level = ?")
        params.append(level)

    # 排序 = BM25 相关性 + 几项「实用性」调整（负数往前排）。
    # 这些调整不是拍脑袋的，依据是库内文档和数据分布：
    #
    #  level          canonical 有正式定义和语义分类，比 source 的裸标签有用
    #  CP_ 前缀       public._guide 的 cp_prefix 章节明确说：跨数据集分析优先用
    #                 CP_ 列（它们是清洗过的版本），所以往前排
    #  survey_specific_response
    #                 DHS 里有 777 个这种「单一调查专用」变量，名字里带
    #                 国家和年份（JO_2023_woman_education_recode），
    #                 关键词一命中就挤掉真正可跨国比较的协调变量，往后压
    #  n_datasets     覆盖的数据集越多，越可能是使用者想要的通用变量
    sql = f"""
        SELECT project, level, section, container, name, label, detail,
               category, n_datasets, has_labels,
               bm25(vars, {', '.join(str(w) for w in _WEIGHTS)}) AS score
        FROM vars
        WHERE {' AND '.join(where)}
        ORDER BY score
                 + CASE level WHEN 'canonical' THEN -2.0
                              WHEN 'question'  THEN -1.0
                              ELSE 0 END
                 + CASE WHEN substr(name, 1, 3) = 'CP_' THEN -1.5 ELSE 0 END
                 + CASE WHEN category = 'survey_specific_response' THEN 3.0
                        ELSE 0 END
                 - CASE WHEN n_datasets > 100 THEN 1.0
                        WHEN n_datasets > 20  THEN 0.5
                        ELSE 0 END
        LIMIT ?
    """
    params.append(int(limit))

    try:
        with _open() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError as exc:
        return {"error": f"索引查询失败：{exc}", "query": query}

    meta = info()
    return {
        "matches": rows,
        "match_count": len(rows),
        "fts_query": query,
        "index_age_days": meta.get("age_days"),
        "index_stale": meta.get("stale"),
    }


def facets(keywords: list[str], project: str | None = None) -> dict[str, Any]:
    """命中结果按调查 / section / 语义分类的分布，用来告诉使用者「该往哪看」。"""
    if not exists():
        return {}
    query = _fts_query(keywords)
    if not query:
        return {}

    where = ["vars MATCH ?"]
    params: list[Any] = [query]
    if project:
        where.append("project = ?")
        params.append(project)
    clause = " AND ".join(where)

    out: dict[str, Any] = {}
    try:
        with _open() as conn:
            out["total_matches"] = conn.execute(
                f"SELECT count(*) FROM vars WHERE {clause}", params
            ).fetchone()[0]
            for facet in ("project", "section", "category", "level"):
                rows = conn.execute(
                    f"SELECT {facet} AS k, count(*) AS n FROM vars"
                    f" WHERE {clause} AND {facet} <> ''"
                    f" GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
                    params,
                ).fetchall()
                out[facet] = [{"value": r["k"], "matches": r["n"]} for r in rows]
    except sqlite3.OperationalError:
        return {}
    return out
