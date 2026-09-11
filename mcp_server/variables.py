"""单个变量的统计与可视化（需求 4、5）。

两件事最容易出错，这里都专门处理：

1. **缺失值编码**。调查数据里 98="don't know"、99="missing" 是常态，
   直接 count 或求平均会得出荒谬结果。这些编码在元数据里是机器可读的
   （source_variable.value_labels 是 JSONB），所以不靠 96-99 这种猜测，
   而是读元数据判断，读不到就老实说读不到。

2. **抽样权重**。调查数据算总体量必须加权。表里有 household_weight /
   person_weight 这类列，这里自动发现并同时给出加权和未加权两个结果。
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import db

# 值标签里出现这些词，说明该码值是「缺失/不适用」而不是真实回答
_MISSING_WORDS = re.compile(
    r"missing|don'?t know|dont know|do not know|no answer|not answer|refus|"
    r"not applicable|n/?a\b|inconsistent|unknown|not stated|no response|"
    r"not determined|undetermined",
    re.I,
)

# 权重列的识别模式
_WEIGHT_HINT = re.compile(r"weight$|^weight|_weight_", re.I)

MAX_CATEGORIES = 40


def _q(schema: str, table: str) -> str:
    """带双引号的完整表名——这个库的表名含大写字母。"""
    return f'"{schema}"."{table}"'


async def locate(table: str, schema: str | None = None) -> dict[str, Any]:
    """按表名找到它在哪个 schema。

    使用者通常只说「final_ED_CSES 的 years_attended_school」，不说 schema，
    所以这里做一次解析。同名表出现在多个 schema 时全部列出来让上层选。
    """
    rows = await db.fetch(
        """
        SELECT n.nspname AS schema, c.relname AS table,
               c.relkind::text AS kind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p','v','m')
          AND ($2::text IS NULL OR n.nspname = $2)
          AND (c.relname = $1 OR lower(c.relname) = lower($1))
          AND has_schema_privilege(n.nspname, 'USAGE')
          AND has_table_privilege(c.oid, 'SELECT')
        -- 真实表排在视图前面：public schema 里放了一批指向 cses_data 的
        -- 同名便捷视图，直接报「有歧义」会把使用者卡住，而两者数据相同，
        -- 选真实表既正确又省一次来回。
        ORDER BY CASE c.relkind WHEN 'r' THEN 0 WHEN 'p' THEN 0 ELSE 1 END,
                 n.nspname
        """,
        table, schema,
    )
    if not rows:
        return {"found": False,
                "error": f"找不到表 {table!r}（或者当前角色无权访问）。"
                         "表名大小写敏感，可先用 find_variables 或 list_tables 确认。"}

    tables = [r for r in rows if r["kind"] in ("r", "p")]
    if len(tables) > 1:
        # 多个真实同名表，这才是真的有歧义，必须让使用者选
        return {"found": False, "ambiguous": [r["schema"] for r in tables],
                "error": f"表名 {table!r} 在多个 schema 里都有同名的实体表"
                         f"（{', '.join(r['schema'] for r in tables)}），请指定 schema 参数。"}

    chosen = rows[0]
    result = {"found": True, "schema": chosen["schema"], "table": chosen["table"]}
    others = [r["schema"] for r in rows[1:]]
    if others:
        result["also_visible_as_view_in"] = others
    return result


async def column_info(schema: str, table: str, column: str) -> dict[str, Any] | None:
    """物理列的类型和注释。"""
    return await db.fetch_one(
        """
        SELECT a.attname AS column,
               format_type(a.atttypid, a.atttypmod) AS type,
               a.atttypid::regtype::text AS base_type,
               NOT a.attnotnull AS nullable,
               col_description(a.attrelid, a.attnum) AS comment
        FROM pg_attribute a
        WHERE a.attrelid = $1::regclass AND a.attnum > 0
          AND NOT a.attisdropped AND a.attname = $2
        """,
        _q(schema, table), column,
    )


async def weight_columns(schema: str, table: str) -> list[str]:
    rows = await db.fetch(
        """
        SELECT a.attname AS column
        FROM pg_attribute a
        WHERE a.attrelid = $1::regclass AND a.attnum > 0 AND NOT a.attisdropped
          AND a.attname ~* 'weight'
        ORDER BY a.attnum
        """,
        _q(schema, table),
    )
    return [r["column"] for r in rows]


# ---------------------------------------------------------------- 值标签

# 每个调查族的值标签放在不同的表里，形状也不同。统一输出 (code, label) 两列。
#
# 元组第二项是参数个数：CSES/DHS 需要 (表名, 列名) 两个，
# 蒙古的表只按列名存所以只要一个。这一点必须显式声明——
# 如果 SQL 里没引用 $1，PostgreSQL 会报「无法推断参数类型」而不是忽略它。
_VALUE_LABEL_SQL: dict[str, tuple[str, int]] = {
    # CSES / DHS：canonical_variable -> variable_mapping -> value_mapping
    "cses": ("""
        SELECT DISTINCT vmap.source_value AS code,
               coalesce(vmap.canonical_label, vmap.canonical_value) AS label
        FROM cses_alignment.cses_canonical_variable cv
        JOIN cses_alignment.cses_variable_mapping vm
             ON vm.canonical_variable_id = cv.canonical_variable_id
        JOIN cses_alignment.cses_value_mapping vmap
             ON vmap.variable_mapping_id = vm.variable_mapping_id
        WHERE cv.target_table = $1 AND cv.canonical_name = $2
    """, 2),
    "dhs": ("""
        SELECT DISTINCT vmap.source_value AS code,
               coalesce(vmap.canonical_label, vmap.canonical_value) AS label
        FROM dhs_alignment.dhs_canonical_variable cv
        JOIN dhs_alignment.dhs_variable_mapping vm
             ON vm.canonical_variable_id = cv.canonical_variable_id
        JOIN dhs_alignment.dhs_value_mapping vmap
             ON vmap.variable_mapping_id = vm.variable_mapping_id
        WHERE cv.target_table = $1 AND cv.canonical_name = $2
    """, 2),
    # 蒙古 HSES：按 column_name 存，但有 3 个发布版本，只取最新的
    # （版本号用 int[] 比较，否则 v1.10 会被判定小于 v1.9）
    "mng_hses": ("""
        SELECT DISTINCT code, coalesce(decoded_label, aligned_label) AS label
        FROM mng_hses_alignment.value_label
        WHERE column_name = $1
          AND release_id = (
              SELECT release_id FROM mng_hses_alignment.value_label
              ORDER BY string_to_array(
                  regexp_replace(release_id, '[^0-9.]', '', 'g'), '.')::int[] DESC
              LIMIT 1)
    """, 1),
    "mng_lfs": ("""
        SELECT DISTINCT code::text AS code, label
        FROM mng_lfs_meta.value_label
        WHERE column_name = $1
    """, 1),
}

# 原始列的 value_labels（JSONB），缺失值编码就在这里
_SOURCE_LABEL_SQL: dict[str, str] = {
    "cses": """
        SELECT value_labels FROM cses_alignment.cses_source_variable
        WHERE variable_name = $1 AND value_labels IS NOT NULL
          AND value_labels::text NOT IN ('null','{}')
        LIMIT 1
    """,
    "dhs": """
        SELECT value_labels FROM dhs_alignment.dhs_source_variable
        WHERE variable_name = $1 AND value_labels IS NOT NULL
          AND value_labels::text NOT IN ('null','{}')
        LIMIT 1
    """,
}


def project_of(schema: str) -> str:
    """从 schema 名推断所属调查族。"""
    if schema.startswith("mng_hses"):
        return "mng_hses"
    if schema.startswith("mng_lfs"):
        return "mng_lfs"
    return schema.split("_", 1)[0]


def _code_variants(code: Any) -> list[str]:
    """一个码值的所有等价写法。

    元数据里的码值格式和物理列取出来的不一定一致：
    mng_hses_alignment.value_label 存的是 '1.0'，而 smallint 列 ::text 是 '1'。
    不归一化的话两边对不上，标签就全丢了（而且不会报错，只是显示「无标签」）。
    """
    s = str(code).strip()
    out = [s]
    if s.endswith(".0"):
        out.append(s[:-2])
    elif s.lstrip("-").isdigit():
        out.append(s + ".0")
    try:                       # '01' 和 '1' 也要能互认
        out.append(str(int(float(s))))
    except (ValueError, OverflowError):
        pass
    return list(dict.fromkeys(out))


async def value_labels(schema: str, table: str, column: str) -> dict[str, Any]:
    """取出某列的码值 -> 含义，并标出哪些码值代表缺失。"""
    project = project_of(schema)
    labels: dict[str, str] = {}
    sources: list[str] = []
    errors: list[str] = []

    if entry := _VALUE_LABEL_SQL.get(project):
        sql, n_params = entry
        args = (table, column)[:n_params] if n_params == 2 else (column,)
        try:
            for r in await db.fetch(sql, *args):
                if r["code"] is not None:
                    for key in _code_variants(r["code"]):
                        labels.setdefault(key, str(r["label"] or ""))
            if labels:
                sources.append("变量协调层（value_mapping）")
        except Exception as exc:
            # 不能静默忽略：查询坏掉会变成「这一列没有值标签」，
            # 于是 96/98/99 这些缺失编码被当成真实取值算进统计里，
            # 产出貌似合理但错误的结果。宁可把错误报出来。
            errors.append(f"{project} 值标签查询失败：{type(exc).__name__}: {exc}")

    if sql := _SOURCE_LABEL_SQL.get(project):
        try:
            row = await db.fetch_one(sql, column)
            if row and row["value_labels"]:
                raw = row["value_labels"]
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    for code, label in parsed.items():
                        for key in _code_variants(code):
                            labels.setdefault(key, str(label))
                    sources.append("原始变量字典（value_labels）")
        except Exception as exc:
            errors.append(f"{project} 原始变量字典查询失败：{type(exc).__name__}: {exc}")

    missing = {c: l for c, l in labels.items() if _MISSING_WORDS.search(l or "")}

    return {
        "labels": labels,
        "missing_codes": sorted(set(missing), key=lambda x: (len(x), x)),
        "missing_labels": missing,
        "label_sources": sources,
        # 说清楚「没找到缺失值编码」和「确认没有缺失值编码」的区别
        "missing_codes_known": bool(labels),
        "errors": errors,
    }


async def definition(schema: str, table: str, column: str) -> dict[str, Any]:
    """变量的定义和语义分类（来自协调层）。"""
    project = project_of(schema)
    if project not in ("cses", "dhs"):
        return {}
    tbl = f"{project}_alignment.{project}_canonical_variable"
    try:
        row = await db.fetch_one(
            f"SELECT canonical_definition AS definition, measure_type AS category,"
            f" status FROM {tbl} WHERE target_table = $1 AND canonical_name = $2",
            table, column,
        )
    except Exception:
        return {}
    return dict(row) if row else {}


# ---------------------------------------------------------------- 需求 4：统计


async def stats(
    schema: str, table: str, column: str, weight: str | None = None
) -> dict[str, Any]:
    """一个变量有多少回答、取值怎么分布。"""
    info = await column_info(schema, table, column)
    if info is None:
        return {"error": f"表 {schema}.{table} 里没有列 {column!r}。"
                         "列名大小写敏感，可先调 describe_table 核对。"}

    ref = _q(schema, table)
    col = f'"{column}"'
    vl = await value_labels(schema, table, column)
    defn = await definition(schema, table, column)
    weights = await weight_columns(schema, table)

    if weight and weight not in weights:
        return {"error": f"{schema}.{table} 里没有权重列 {weight!r}。"
                         f"可用的权重列：{weights or '（没有）'}"}

    # 基础计数
    base = await db.fetch_one(
        f"SELECT count(*) AS total_rows, count({col}) AS non_null,"
        f" count(*) - count({col}) AS nulls,"
        f" count(DISTINCT {col}) AS distinct_values FROM {ref}"
    )

    # 缺失值编码占了多少行。注意这里用元数据判断，不靠猜。
    invalid = 0
    if vl["missing_codes"]:
        row = await db.fetch_one(
            f"SELECT count(*) AS n FROM {ref}"
            f" WHERE {col}::text = ANY($1::text[])",
            vl["missing_codes"],
        )
        invalid = (row or {}).get("n", 0) or 0

    numeric = info["base_type"] in (
        "smallint", "integer", "bigint", "numeric", "real",
        "double precision", "money",
    )

    result: dict[str, Any] = {
        "schema": schema, "table": table, "column": column,
        "type": info["type"],
        "comment": info["comment"],
        **({"definition": defn.get("definition"),
            "category": defn.get("category")} if defn else {}),
        "total_rows": base["total_rows"],
        "answered": base["non_null"],
        "not_answered": base["nulls"],
        "distinct_values": base["distinct_values"],
        "answer_rate": (round(base["non_null"] / base["total_rows"] * 100, 2)
                        if base["total_rows"] else None),
        "available_weight_columns": weights,
    }

    if vl.get("errors"):
        # 查询失败时必须让使用者知道，否则「没有缺失值编码」这个结论是假的
        result["label_lookup_errors"] = vl["errors"]
        result["warning"] = ("值标签查询出错（见 label_lookup_errors），"
                             "因此无法判断缺失值编码，下面的统计量可能把 "
                             "98/99 这类缺失编码当成真实数值算进去了。")
    if vl["labels"]:
        result["value_label_count"] = len(vl["labels"])
        result["label_sources"] = vl["label_sources"]
    if vl["missing_codes"]:
        result["missing_codes"] = vl["missing_labels"]
        result["rows_with_missing_code"] = invalid
        result["valid_answers"] = base["non_null"] - invalid
        result["note_missing"] = (
            f"其中 {invalid} 行是缺失值编码（{', '.join(vl['missing_codes'])}），"
            "不是真实回答。算统计量时应排除。"
        )
    elif not vl["missing_codes_known"]:
        result["note_missing"] = (
            "元数据里找不到这一列的值标签，因此无法判断是否存在缺失值编码"
            "（98=不知道、99=缺失这类）。用它做计算前建议先看取值分布。"
        )

    # 取值分布：类别少就全列出来，类别多只给 Top N
    if base["distinct_values"] and base["distinct_values"] <= MAX_CATEGORIES:
        rows = await db.fetch(
            f"SELECT {col}::text AS code, count(*) AS n FROM {ref}"
            f" WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
        )
    else:
        rows = await db.fetch(
            f"SELECT {col}::text AS code, count(*) AS n FROM {ref}"
            f" WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT {MAX_CATEGORIES}"
        )
        result["distribution_truncated"] = True

    result["distribution"] = [
        {
            "code": r["code"],
            "label": vl["labels"].get(r["code"], ""),
            "count": r["n"],
            "is_missing_code": r["code"] in vl["missing_codes"],
            "pct": round(r["n"] / base["non_null"] * 100, 2) if base["non_null"] else None,
        }
        for r in rows
    ]

    # 数值型再给一组描述统计，排除缺失值编码
    if numeric:
        exclude = ""
        if vl["missing_codes"]:
            exclude = f" AND {col}::text <> ALL($1::text[])"
        args = [vl["missing_codes"]] if vl["missing_codes"] else []
        num = await db.fetch_one(
            f"SELECT min({col}) AS min, max({col}) AS max,"
            f" round(avg({col})::numeric, 4) AS mean,"
            f" percentile_cont(0.5) WITHIN GROUP (ORDER BY {col}) AS median,"
            f" round(stddev_samp({col})::numeric, 4) AS stddev,"
            f" count({col}) AS n"
            f" FROM {ref} WHERE {col} IS NOT NULL{exclude}",
            *args,
        )
        result["numeric_summary"] = dict(num or {})
        result["numeric_summary"]["excluded_missing_codes"] = vl["missing_codes"] or None

    # 加权结果。调查数据算总体量必须加权，所以两个都给，让使用者看到差别。
    if weight:
        w = f'"{weight}"'
        wrow = await db.fetch_one(
            f"SELECT round(sum({w})::numeric, 2) AS weighted_total,"
            f" round(sum(CASE WHEN {col} IS NOT NULL THEN {w} ELSE 0 END)::numeric, 2)"
            f"     AS weighted_answered"
            f" FROM {ref} WHERE {w} IS NOT NULL"
        )
        result["weighted"] = {"weight_column": weight, **dict(wrow or {})}
        if numeric:
            exclude = (f" AND {col}::text <> ALL($1::text[])"
                       if vl["missing_codes"] else "")
            args = [vl["missing_codes"]] if vl["missing_codes"] else []
            wm = await db.fetch_one(
                f"SELECT round((sum({col} * {w}) / nullif(sum({w}), 0))::numeric, 4)"
                f"     AS weighted_mean"
                f" FROM {ref} WHERE {col} IS NOT NULL AND {w} IS NOT NULL{exclude}",
                *args,
            )
            result["weighted"].update(dict(wm or {}))

    return result


# ---------------------------------------------------------------- 需求 5：可视化


async def plot(
    schema: str, table: str, column: str,
    kind: str = "auto",
    bins: int = 20,
    top: int = 25,
    weight: str | None = None,
    include_missing: bool = False,
) -> dict[str, Any]:
    """生成一个图表规格，由网页前端渲染成图。

    返回值里的 chart 字段会被后端识别并单独推给前端，
    所以这里只管算数据，不管画图。
    """
    info = await column_info(schema, table, column)
    if info is None:
        return {"error": f"表 {schema}.{table} 里没有列 {column!r}。"}

    ref = _q(schema, table)
    col = f'"{column}"'
    vl = await value_labels(schema, table, column)
    defn = await definition(schema, table, column)

    weights = await weight_columns(schema, table)
    if weight and weight not in weights:
        return {"error": f"没有权重列 {weight!r}，可用：{weights or '（没有）'}"}
    w = f'"{weight}"' if weight else None

    numeric = info["base_type"] in (
        "smallint", "integer", "bigint", "numeric", "real", "double precision",
    )
    distinct = (await db.fetch_one(
        f"SELECT count(DISTINCT {col}) AS n FROM {ref}"))["n"] or 0

    if kind == "auto":
        kind = "histogram" if (numeric and distinct > top) else "bar"

    # 排除缺失值编码——除非使用者明确要看
    skip = ""
    args: list[Any] = []
    if vl["missing_codes"] and not include_missing:
        skip = f" AND {col}::text <> ALL($1::text[])"
        args = [vl["missing_codes"]]

    measure = f"sum({w})" if w else "count(*)"
    y_label = f"加权计数（{weight}）" if w else "记录数"

    if kind == "bar":
        rows = await db.fetch(
            f"SELECT {col}::text AS code, {measure} AS value FROM {ref}"
            f" WHERE {col} IS NOT NULL{skip}"
            f" GROUP BY 1 ORDER BY 2 DESC LIMIT {int(top)}",
            *args,
        )
        points = [
            {
                "label": vl["labels"].get(r["code"]) or r["code"],
                "code": r["code"],
                "value": float(r["value"] or 0),
            }
            for r in rows
        ]
        # 码值型变量按码值排序更符合阅读习惯（教育程度 1..7 不该按人数乱序）
        if numeric and all(re.fullmatch(r"-?\d+", p["code"] or "") for p in points):
            points.sort(key=lambda p: int(p["code"]))
        chart = {
            "kind": "bar", "points": points,
            "x_label": column, "y_label": y_label,
        }

    elif kind == "histogram":
        if not numeric:
            return {"error": f"{column} 是 {info['type']} 类型，不能画直方图，"
                             "请用 kind='bar'。"}
        rng = await db.fetch_one(
            f"SELECT min({col})::double precision AS lo,"
            f" max({col})::double precision AS hi FROM {ref}"
            f" WHERE {col} IS NOT NULL{skip}", *args)
        lo, hi = (rng or {}).get("lo"), (rng or {}).get("hi")
        if lo is None or hi is None:
            return {"error": "这一列没有任何非空数值，画不出图。"}
        if lo == hi:
            return {"error": f"这一列所有非空值都等于 {lo}，画直方图没有意义。"}

        n_bins = max(2, min(int(bins), 60))
        rows = await db.fetch(
            f"SELECT width_bucket({col}::double precision, $%d, $%d, {n_bins}) AS b,"
            f" {measure} AS value FROM {ref}"
            f" WHERE {col} IS NOT NULL{skip} GROUP BY 1 ORDER BY 1"
            % (len(args) + 1, len(args) + 2),
            *args, lo, hi,
        )
        step = (hi - lo) / n_bins
        counts = {r["b"]: float(r["value"] or 0) for r in rows}
        points = []
        for b in range(1, n_bins + 1):
            left = lo + (b - 1) * step
            right = lo + b * step
            points.append({
                # width_bucket 会把等于 hi 的值放进第 n_bins+1 桶，合并进最后一桶
                "label": f"{left:.3g}–{right:.3g}",
                "code": str(b),
                "value": counts.get(b, 0.0) + (counts.get(n_bins + 1, 0.0)
                                               if b == n_bins else 0.0),
            })
        chart = {
            "kind": "histogram", "points": points,
            "x_label": column, "y_label": y_label,
            "range": {"min": lo, "max": hi, "bins": n_bins},
        }
    else:
        return {"error": f"不支持的图表类型 {kind!r}，可用：auto / bar / histogram。"}

    chart["title"] = f"{schema}.{table} · {column}"
    subtitle_bits = []
    if defn.get("definition"):
        subtitle_bits.append(defn["definition"][:160])
    elif info["comment"]:
        subtitle_bits.append(info["comment"][:160])
    if vl["missing_codes"] and not include_missing:
        subtitle_bits.append(f"已排除缺失值编码 {', '.join(vl['missing_codes'])}")
    if w:
        subtitle_bits.append(f"按 {weight} 加权")
    chart["subtitle"] = " · ".join(subtitle_bits)

    return {
        "chart": chart,
        "point_count": len(chart["points"]),
        "total_plotted": round(sum(p["value"] for p in chart["points"]), 2),
        "excluded_missing_codes": (vl["missing_labels"]
                                   if vl["missing_codes"] and not include_missing
                                   else None),
        "available_weight_columns": weights,
        "hint": ("这是未加权的原始记录数。调查数据算总体量应传 weight 参数加权。"
                 if not w and weights else None),
    }
