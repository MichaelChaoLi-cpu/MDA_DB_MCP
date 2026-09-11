"""调查目录层：把「不同形状的元数据」抹平成统一结构。

这个库里四个调查族的元数据层是三种不同形状：

  cses / dhs / mng_lfs   canonical_variable（canonical_name + definition + measure_type）
  mng_hses               concept + variable（concept_id + module_family + aligned_label）
  mng_lfs                另有 mng_lfs_meta.variable 存列级信息

如果每个工具都自己去分辨「这是哪个调查、该查哪张表」，加一个新调查（比如 MICS）
就得改遍所有工具。所以这里让每个调查族提供一组 SQL「适配器」，
统一输出下面这 10 个字段，上层工具只认这 10 个字段：

    project      cses | dhs | mng_hses | mng_lfs
    level        canonical（协调后变量）| question（问卷原文）| source（原始列）
    section      分区：CSES 的 EC/ED/HH…，DHS 的 WM/CH/BR…，HSES 的 module_family
    container    所在表或数据集范围
    name         变量名 / 列名 / 问题编号
    label         简短标签
    detail       完整定义或问题原文
    category     语义分类（measure_type / module_family / 问卷章节）
    n_datasets   有多少个数据集包含它
    has_labels   是否有值标签（码值→含义）

加新调查时：在 PROJECTS 里加一条，写好它的 extracts SQL，其余全部自动生效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import db


@dataclass(frozen=True)
class Project:
    """一个调查族。"""

    key: str
    label: str
    scope: str                      # 覆盖范围，给人看的
    schemas: dict[str, str]         # 角色 -> schema 名
    # 判断「有没有权限」只看这一个 schema：alignment 层没权限的话其他也没意义
    probe_schema: str
    # 调查/波次登记表的查询，返回 n_surveys / n_countries / year_min / year_max / types
    survey_sql: str | None
    # 物理分区（section）的查询，返回 section / tables / approx_rows / description
    section_sql: str | None
    # 问卷章节的查询，返回 section_name / questions
    questionnaire_sql: str | None
    # 索引抽取：level -> SQL（必须输出上面那 10 个字段）
    extracts: dict[str, str] = field(default_factory=dict)
    notes: str = ""


# ---------------------------------------------------------------- CSES

_CSES = Project(
    key="cses",
    label="CSES — Cambodia Socio-Economic Survey",
    scope="柬埔寨，10 个重复横截面波次",
    schemas={"data": "cses_data", "meta": "cses_meta",
             "alignment": "cses_alignment", "analysis": "cses_analysis"},
    probe_schema="cses_alignment",
    survey_sql="""
        SELECT count(*) AS n_surveys,
               count(DISTINCT country_name) AS n_countries,
               min(nominal_survey_year) AS year_min,
               max(nominal_survey_year) AS year_max,
               string_agg(DISTINCT country_name, ', ') AS types
        FROM cses_meta.cses_survey
    """,
    # section 藏在表名里：final_EC_CSES -> EC。表注释是自解释的，直接拿来当描述。
    section_sql="""
        SELECT regexp_replace(c.relname, '^final_|_CSES$', '', 'g') AS section,
               count(*) AS tables,
               max(CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END)
                   AS approx_rows,
               max(obj_description(c.oid, 'pg_class')) AS description
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'cses_data' AND c.relkind = 'r'
          AND c.relname LIKE 'final\\_%'
        GROUP BY 1 ORDER BY 1
    """,
    questionnaire_sql="""
        SELECT section_name, count(*) AS questions
        FROM cses_alignment.cses_question
        WHERE coalesce(section_name, '') <> ''
        GROUP BY 1 ORDER BY 1
    """,
    extracts={
        "canonical": """
            SELECT 'cses' AS project, 'canonical' AS level,
                   regexp_replace(cv.target_table, '^final_|_CSES$', '', 'g') AS section,
                   cv.target_table AS container,
                   cv.canonical_name AS name,
                   cv.canonical_name AS label,
                   cv.canonical_definition AS detail,
                   cv.measure_type AS category,
                   1 AS n_datasets,
                   0 AS has_labels
            FROM cses_alignment.cses_canonical_variable cv
            WHERE cv.status <> 'retired'
        """,
        "question": """
            SELECT 'cses' AS project, 'question' AS level,
                   '' AS section,
                   'cses_alignment.cses_question' AS container,
                   q.question_code AS name,
                   left(q.question_text, 160) AS label,
                   q.question_text AS detail,
                   coalesce(q.section_name, '') AS category,
                   1 AS n_datasets, 0 AS has_labels
            FROM cses_alignment.cses_question q
            WHERE coalesce(q.question_text, '') <> ''
        """,
        "source": """
            SELECT 'cses' AS project, 'source' AS level,
                   '' AS section,
                   'cses_alignment.cses_source_variable' AS container,
                   sv.variable_name AS name,
                   coalesce(sv.variable_label, sv.variable_name) AS label,
                   '' AS detail,
                   '' AS category,
                   count(DISTINCT sv.dataset_id)::int AS n_datasets,
                   max(CASE WHEN sv.value_labels IS NOT NULL
                                 AND sv.value_labels::text NOT IN ('null','{}')
                            THEN 1 ELSE 0 END) AS has_labels
            FROM cses_alignment.cses_source_variable sv
            GROUP BY sv.variable_name, coalesce(sv.variable_label, sv.variable_name)
        """,
    },
)

# ---------------------------------------------------------------- DHS

_DHS = Project(
    key="dhs",
    label="DHS — Demographic and Health Surveys",
    scope="82 个国家 / 1985–2024 / DHS·CONTINUOUS_DHS·MIS·SPECIAL",
    schemas={"data": "dhs_data", "meta": "dhs_meta", "alignment": "dhs_alignment",
             "analysis": "dhs_analysis", "legacy": "dhs_legacy"},
    probe_schema="dhs_alignment",
    survey_sql="""
        SELECT count(*) AS n_surveys,
               count(DISTINCT country) AS n_countries,
               min(survey_year) AS year_min,
               max(survey_year) AS year_max,
               string_agg(DISTINCT survey_type, ', ') AS types
        FROM dhs_meta.dhs_survey
    """,
    # DHS 表名是 final_<SECTION>_<国家码>_DHS，所以 split_part 取第 2 段。
    # DHS 的表注释是模板化的（"Country-scoped DHS BR physical storage"），
    # 说明不了 BR 是什么，所以描述改由 _SECTION_HINTS 提供。
    section_sql="""
        SELECT split_part(c.relname, '_', 2) AS section,
               count(*) AS tables,
               sum(CASE WHEN c.reltuples < 0 THEN 0 ELSE c.reltuples::bigint END)
                   AS approx_rows,
               NULL::text AS description
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'dhs_data' AND c.relkind = 'r'
          AND c.relname LIKE 'final\\_%'
        GROUP BY 1 ORDER BY 2 DESC
    """,
    questionnaire_sql="""
        SELECT section_name, count(*) AS questions
        FROM dhs_alignment.dhs_question
        WHERE coalesce(section_name, '') <> ''
        GROUP BY 1 ORDER BY 2 DESC LIMIT 40
    """,
    extracts={
        "canonical": """
            SELECT 'dhs' AS project, 'canonical' AS level,
                   split_part(cv.target_table, '_', 2) AS section,
                   cv.target_table AS container,
                   cv.canonical_name AS name,
                   cv.canonical_name AS label,
                   cv.canonical_definition AS detail,
                   cv.measure_type AS category,
                   1 AS n_datasets,
                   0 AS has_labels
            FROM dhs_alignment.dhs_canonical_variable cv
            WHERE cv.status <> 'retired'
        """,
        "question": """
            SELECT 'dhs' AS project, 'question' AS level,
                   '' AS section,
                   'dhs_alignment.dhs_question' AS container,
                   q.question_code AS name,
                   left(q.question_text, 160) AS label,
                   max(q.question_text) AS detail,
                   coalesce(q.section_name, '') AS category,
                   count(*)::int AS n_datasets, 0 AS has_labels
            FROM dhs_alignment.dhs_question q
            WHERE coalesce(q.question_text, '') <> ''
            GROUP BY q.question_code, left(q.question_text, 160),
                     coalesce(q.section_name, '')
        """,
        # 130 万行原始列，同一个变量在 730 个数据集里重复出现，
        # 而且有 hv123_01..hv123_11 这种宽表重复槽位。
        # 所以按 label 聚合：一行代表一个概念，名字给个代表值加变体数量。
        "source": """
            SELECT 'dhs' AS project, 'source' AS level,
                   coalesce(max(d.record_type), '') AS section,
                   'dhs_data' AS container,
                   min(sv.variable_name)
                     || CASE WHEN count(DISTINCT sv.variable_name) > 1
                             THEN ' (+' || (count(DISTINCT sv.variable_name) - 1)
                                  || ' 个同名变体)'
                             ELSE '' END AS name,
                   sv.variable_label AS label,
                   '' AS detail,
                   '' AS category,
                   count(DISTINCT sv.dataset_id)::int AS n_datasets,
                   max(CASE WHEN sv.value_labels IS NOT NULL
                                 AND sv.value_labels::text NOT IN ('null','{}')
                            THEN 1 ELSE 0 END) AS has_labels
            FROM dhs_alignment.dhs_source_variable sv
            LEFT JOIN dhs_meta.dhs_dataset d ON d.dataset_id = sv.dataset_id
            WHERE coalesce(sv.variable_label, '') <> ''
            GROUP BY sv.variable_label
        """,
    },
)

# ---------------------------------------------------------------- 蒙古 HSES

# HSES 的协调层有多个发布版本（hses-alignment-v1 / v1.1 / v1.2），
# 每个概念在每个版本里各存一行，内容基本相同。不过滤的话搜索结果会
# 出现三份一样的变量。所以统一只取最新版本。
#
# 版本号排序不能直接按字符串比，那样 v1.10 会小于 v1.9。
# 这里把数字部分抽出来转成 int[] 再比，v1.10 > v1.9 才成立。
def _hses_latest(table: str) -> str:
    return (
        f"(SELECT release_id FROM {table}"
        f" ORDER BY string_to_array("
        f"     regexp_replace(release_id, '[^0-9.]', '', 'g'), '.')::int[] DESC"
        f" LIMIT 1)"
    )


_HSES = Project(
    key="mng_hses",
    label="HSES — Mongolia Household Socio-Economic Survey",
    scope="蒙古，2002–2020 多波次",
    schemas={"data": "mng_hses_data", "meta": "mng_hses_meta",
             "alignment": "mng_hses_alignment", "analysis": "mng_hses_analysis"},
    probe_schema="mng_hses_alignment",
    survey_sql="""
        SELECT count(*) AS n_surveys, 1 AS n_countries,
               NULL::int AS year_min, NULL::int AS year_max,
               'Mongolia' AS types
        FROM mng_hses_meta.wave
    """,
    # HSES 没有 final_<SECTION>_ 这种命名（表名是 y2002_2003_rt006_sav_<hash>），
    # section 的等价物是 concept.module_family
    section_sql=f"""
        SELECT module_family AS section, count(*) AS tables,
               NULL::bigint AS approx_rows, NULL::text AS description
        FROM mng_hses_alignment.concept
        WHERE coalesce(module_family, '') <> ''
          AND release_id = {_hses_latest('mng_hses_alignment.concept')}
        GROUP BY 1 ORDER BY 2 DESC
    """,
    questionnaire_sql=None,
    extracts={
        "canonical": f"""
            SELECT 'mng_hses' AS project, 'canonical' AS level,
                   coalesce(c.module_family, '') AS section,
                   'mng_hses_alignment.concept' AS container,
                   c.aligned_name AS name,
                   coalesce(c.aligned_label, c.aligned_name) AS label,
                   '' AS detail,
                   coalesce(c.module_family, '') AS category,
                   coalesce(jsonb_array_length(c.waves), 1) AS n_datasets,
                   0 AS has_labels
            FROM mng_hses_alignment.concept c
            WHERE c.release_id = {_hses_latest('mng_hses_alignment.concept')}
        """,
    },
    notes=("列名是问卷编码（b0202 这种），语义在 concept.aligned_label 里。"
           "协调层有 3 个发布版本（v1/v1.1/v1.2），工具只取最新版本。"
           "只索引 concept 层、不索引原始列层：1881 个原始列名里有 1877 个"
           "已被 concept 覆盖，两层都索引会让每次搜索结果出现两份重复。"),
)

# ---------------------------------------------------------------- 蒙古 LFS

_LFS = Project(
    key="mng_lfs",
    label="LFS — Mongolia Labour Force Survey",
    scope="蒙古，多波次劳动力调查",
    schemas={"data": "mng_lfs_data", "meta": "mng_lfs_meta",
             "alignment": "mng_lfs_alignment", "analysis": "mng_lfs_analysis"},
    probe_schema="mng_lfs_alignment",
    survey_sql="""
        SELECT count(*) AS n_surveys, 1 AS n_countries,
               NULL::int AS year_min, NULL::int AS year_max,
               'Mongolia' AS types
        FROM mng_lfs_meta.wave
    """,
    section_sql=None,
    questionnaire_sql=None,
    extracts={
        "canonical": """
            SELECT 'mng_lfs' AS project, 'canonical' AS level,
                   '' AS section,
                   'mng_lfs_alignment.canonical_variable' AS container,
                   cv.canonical_name AS name,
                   coalesce(cv.label, cv.canonical_name) AS label,
                   -- status 拼进 detail：LFS 的协调工作还在早期，8 个变量里
                   -- 只有 2 个是 qualified_core，其余是 candidate_only（尚未验证）。
                   -- 不标出来的话，LLM 会把候选变量当成已验证的推荐给使用者。
                   coalesce(cv.definition, '')
                     || CASE WHEN cv.status <> 'qualified_core'
                             THEN ' 〔协调状态：' || cv.status || '，尚未验证，'
                                  || '用于跨波次比较前需自行确认〕'
                             ELSE '' END AS detail,
                   coalesce(cv.unit, '') AS category,
                   1 AS n_datasets, 0 AS has_labels
            FROM mng_lfs_alignment.canonical_variable cv
        """,
        "source": """
            SELECT 'mng_lfs' AS project, 'source' AS level,
                   '' AS section,
                   'mng_lfs_data' AS container,
                   v.column_name AS name,
                   coalesce(max(v.label), v.column_name) AS label,
                   '' AS detail, '' AS category,
                   count(DISTINCT v.dataset_id)::int AS n_datasets,
                   0 AS has_labels
            FROM mng_lfs_meta.variable v
            GROUP BY v.column_name
        """,
    },
)


# ---------------------------------------------------------------- MICS（public schema）

# MICS 的模块。_catalog 表里有 grain / join_keys / caveats，
# 所以 section 说明直接从数据库读，不用我在代码里写死。
_MICS_IND_QUE = ("HH", "HL", "WM", "CH")

# 四个模块的变量溯源表结构相同，UNION 起来。
# canonical_text 是人写的描述，measure_type 是语义分类——正好对上统一结构。
_MICS_UNION = " UNION ALL ".join(
    f"""
    SELECT '{m}' AS module, canonical_varname, dataset_name, column_in_raw_sav,
           column_label_in_english, measure_type, canonical_text
    FROM public."ind_que_{m}_MICS"
    """
    for m in _MICS_IND_QUE
)

_MICS = Project(
    key="mics",
    label="MICS — UNICEF Multiple Indicator Cluster Surveys（协调库）",
    scope="MICS2–MICS6 / 1999–2023 / 约 110 个国家 / 250+ 数据集",
    schemas={"data": "public"},
    probe_schema="public",
    # 数据集数从 ind_que 表数（便宜），国家数从 _geo_dict 数——
    # dataset_name 的命名不统一（下划线/空格混用、还有 "(Roma Settlements)"
    # 这类变体），靠字符串切分猜国家会多算几十个，_geo_dict 是权威映射。
    # 年份范围不实时算：CP_survey_year 在 277 万行的表上没有索引，
    # min/max 要 1.4 秒，不值得为一个概览查询付这个代价（范围已写在 scope 里）。
    survey_sql="""
        SELECT (SELECT count(DISTINCT dataset_name)
                FROM public."ind_que_HH_MICS") AS n_surveys,
               (SELECT count(DISTINCT country_code)
                FROM public."_geo_dict") AS n_countries,
               NULL::int AS year_min, NULL::int AS year_max,
               'MICS2-MICS6（1999-2023）' AS types
    """,
    # 直接读库里自带的 _catalog 表：它有 grain、join_keys、caveats，
    # 比我在代码里维护一份说明可靠得多。
    section_sql="""
        SELECT module AS section,
               1 AS tables,
               row_count AS approx_rows,
               description || CASE WHEN coalesce(caveats,'') <> ''
                                   THEN ' ⚠ 注意：' || caveats ELSE '' END
                   AS description,
               grain, join_keys, n_datasets, table_name
        FROM public."_catalog"
        ORDER BY module
    """,
    questionnaire_sql=None,
    extracts={
        "canonical": f"""
            SELECT 'mics' AS project, 'canonical' AS level,
                   u.module AS section,
                   'final_' || u.module || '_MICS' AS container,
                   u.canonical_varname AS name,
                   coalesce(max(u.canonical_text), u.canonical_varname) AS label,
                   coalesce(max(u.column_label_in_english), '') AS detail,
                   coalesce(max(u.measure_type), '') AS category,
                   count(DISTINCT u.dataset_name)::int AS n_datasets,
                   0 AS has_labels
            FROM ({_MICS_UNION}) u
            GROUP BY u.module, u.canonical_varname
        """,
        "source": f"""
            SELECT 'mics' AS project, 'source' AS level,
                   u.module AS section,
                   'final_' || u.module || '_MICS' AS container,
                   u.column_in_raw_sav AS name,
                   u.column_label_in_english AS label,
                   '' AS detail,
                   coalesce(max(u.measure_type), '') AS category,
                   count(DISTINCT u.dataset_name)::int AS n_datasets,
                   0 AS has_labels
            FROM ({_MICS_UNION}) u
            WHERE coalesce(u.column_label_in_english, '') <> ''
            GROUP BY u.module, u.column_in_raw_sav, u.column_label_in_english
        """,
    },
    notes=("库里自带一份写给 agent 的使用指南（public._guide）和表目录（public._catalog），"
           "分析 MICS 数据前应该先调 read_guide 读一遍——里面有跨数据集比较的关键陷阱，"
           "比如 CP_ 前缀列才是清洗过的版本、MICS2 时代是非题编码相反等。"),
)


PROJECTS: dict[str, Project] = {
    p.key: p for p in (_MICS, _CSES, _DHS, _HSES, _LFS)
}

# 已知但尚未入库的调查。明确列出来，这样被问到时工具能回答「没有」，
# 而不是让 LLM 面对一片空白去编造。
NOT_LOADED: dict[str, str] = {
    # 暂时没有已知但未入库的调查。
    # 有的话在这里列出来，这样被问到时能明确回答「没有」，
    # 而不是让 LLM 面对空结果去编造。
}

# DHS 的 section 含义。DHS 的表注释是模板化的，说明不了 BR/SB/PG 是什么，
# 所以这里给出说明——每一条都是从该 section 表的实际列推断出来的，不是凭印象写的：
#   SB 有 sibling_index、PG 有 pregnancy_index、CP 同时有 woman/man_line_number
#   加 couple_sample_weight、FW 有 fieldworker_id。
_SECTION_HINTS = {
    "dhs": {
        "WM": "女性个人记录（育龄妇女问卷主表）",
        "MN": "男性个人记录",
        "HH": "户级记录（住房、资产、用水卫生等）",
        "PR": "户成员名册（每个家庭成员一行）",
        "BR": "生育史（每个孩子一行）",
        "CH": "儿童健康与营养（五岁以下儿童）",
        "CP": "夫妻配对记录（女性行号 + 男性行号 + 夫妻权重）",
        "SB": "兄弟姐妹史（来自女性问卷的 sibling_index，用于孕产妇死亡率估计）",
        "PG": "妊娠史（pregnancy_index，逐次妊娠一行）",
        "FW": "访员记录（fieldworker_id）",
        "GEO": "抽样社区 / 地理位置",
    },
    "mics": {
        "HH": "户级问卷（用水卫生、资产、财富指数、户主属性、抽样设计）",
        "HL": "户成员名册（全体成员的年龄、性别、与户主关系、教育、孤儿状况）",
        "WM": "15-49 岁受访女性（生育、健康、媒体接触、教育）",
        "CH": "五岁以下儿童（体格测量、患病、照护、喂养）",
        "NLSS": "尼泊尔生活标准调查 2022（独立调查，未纳入 MICS 协调）",
    },
    "cses": {
        "EC": "当前就业（个人层级）",
        "ED": "教育（个人层级）",
        "HH": "户级主表 / 链接骨架",
        "HL": "户成员名册",
        "HO": "住房、用水卫生、能源、产权、居住成本（户级）",
        "VL": "村级人口信息（PSU 层级）",
        "SURVEY_DATE": "访问时间审计（户级）",
    },
}


def section_hint(project: str, section: str) -> str:
    return _SECTION_HINTS.get(project, {}).get(section, "")


async def accessible() -> dict[str, bool]:
    """逐个探测当前角色对每个调查族有没有读取权限。

    pg_catalog 的元数据是所有人可读的，所以「能搜到」不等于「能查」。
    这里用 has_schema_privilege 问数据库要真实答案。
    """
    rows = await db.fetch(
        "SELECT s.key, has_schema_privilege(s.schema_name, 'USAGE') AS ok"
        " FROM (SELECT unnest($1::text[]) AS key,"
        "              unnest($2::text[]) AS schema_name) s",
        list(PROJECTS.keys()),
        [p.probe_schema for p in PROJECTS.values()],
    )
    return {r["key"]: bool(r["ok"]) for r in rows}


async def storage_stats(project: Project) -> dict[str, Any]:
    """一个调查族占了多少张表、多大体积。"""
    row = await db.fetch_one(
        """
        SELECT count(*) AS tables,
               pg_size_pretty(sum(pg_total_relation_size(c.oid))) AS size
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p','v','m')
          AND n.nspname = ANY($1::text[])
          AND has_schema_privilege(n.nspname, 'USAGE')
          AND has_table_privilege(c.oid, 'SELECT')
        """,
        list(project.schemas.values()),
    )
    return row or {"tables": 0, "size": "0 bytes"}


async def overview() -> dict[str, Any]:
    """需求 1：哪些调查入库了。

    三种状态要分清楚，不能混成一句「没有」：
      loaded        已入库且当前角色可读
      no_access     数据在库里，但当前角色没有权限（需要 GRANT）
      not_loaded    数据库里根本没有这个调查
    """
    access = await accessible()
    loaded: list[dict[str, Any]] = []
    no_access: list[dict[str, Any]] = []

    for key, project in PROJECTS.items():
        entry: dict[str, Any] = {
            "survey": key,
            "label": project.label,
            "scope": project.scope,
            "schemas": sorted(project.schemas.values()),
        }
        if not access.get(key):
            entry["status"] = "no_access"
            entry["reason"] = (
                f"{project.probe_schema} 等 schema 对当前数据库角色没有 USAGE 权限。"
                "项目根目录的 grants.sql 可以开通（需要数据库管理员执行）。"
            )
            no_access.append(entry)
            continue

        entry["status"] = "loaded"
        entry.update(await storage_stats(project))
        if project.survey_sql:
            try:
                row = await db.fetch_one(project.survey_sql)
                if row:
                    entry["surveys"] = row.get("n_surveys")
                    entry["countries"] = row.get("n_countries")
                    years = [row.get("year_min"), row.get("year_max")]
                    if all(y is not None for y in years):
                        entry["years"] = f"{years[0]}–{years[1]}"
                    entry["survey_types"] = row.get("types")
            except Exception as exc:
                entry["registry_error"] = str(exc)[:200]
        if project.notes:
            entry["notes"] = project.notes
        loaded.append(entry)

    return {
        "loaded": loaded,
        "no_access": no_access,
        "not_loaded": [{"survey": k, "label": v, "status": "not_loaded"}
                       for k, v in NOT_LOADED.items()],
        "summary": (
            f"已入库且可读：{', '.join(e['survey'] for e in loaded) or '无'}"
            + (f"；在库但无权限：{', '.join(e['survey'] for e in no_access)}"
               if no_access else "")
            + (f"；未入库：{', '.join(NOT_LOADED)}" if NOT_LOADED else "")
        ),
    }


async def sections(survey: str) -> dict[str, Any]:
    """需求 2：某个调查包含哪些 section。

    这个库里 section 有两种含义，两种都返回，因为使用者问的可能是任一种：
      物理分区   数据实际怎么分表（CSES 的 EC/ED/HH，DHS 的 WM/CH/BR）
      问卷章节   问卷本身怎么分节（"SECTION 2. REPRODUCTION"）
    """
    project = PROJECTS.get(survey)
    if project is None:
        return {"error": f"不认识的调查 {survey!r}，可用：{sorted(PROJECTS)}。"
                         "先调 list_surveys 看清单。"}

    access = await accessible()
    if not access.get(survey):
        return {"error": f"{survey} 的 schema 对当前角色没有权限，看不到它的 section。",
                "hint": "项目根目录的 grants.sql 可以开通。"}

    out: dict[str, Any] = {"survey": survey, "label": project.label}

    if project.section_sql:
        rows = await db.fetch(project.section_sql)
        out["physical_sections"] = [
            {
                "section": r["section"],
                "meaning": section_hint(survey, r["section"]) or None,
                "tables": r["tables"],
                "approx_rows": r.get("approx_rows"),
                "description": r.get("description"),
                # 下面几项只有部分调查有（MICS 的 _catalog 表提供），
                # 没有的就不输出，免得塞一堆 null 给 LLM
                **{k: r[k] for k in ("table_name", "grain", "join_keys", "n_datasets")
                   if k in r and r[k] is not None},
            }
            for r in rows if r["section"]
        ]
        out["physical_note"] = (
            "physical_sections 是数据的分表方式。写 SQL 时表名形如 "
            + ("final_<SECTION>_CSES" if survey == "cses"
               else "final_<SECTION>_<国家码>_DHS" if survey == "dhs"
               else "见 list_tables")
        )

    if project.questionnaire_sql:
        rows = await db.fetch(project.questionnaire_sql)
        out["questionnaire_sections"] = [
            {"section_name": r["section_name"], "questions": r["questions"]}
            for r in rows
        ]
        out["questionnaire_note"] = (
            "questionnaire_sections 来自问卷文档，和上面的物理分表不是一一对应的。"
            + ("DHS 的问卷章节有多语言版本（英语/法语等），同一章节会出现多条。"
               if survey == "dhs" else "")
        )

    if "physical_sections" not in out and "questionnaire_sections" not in out:
        out["note"] = f"{survey} 没有可用的 section 划分信息。"

    return out


# ---------------------------------------------------------------- 库内自带文档

# 数据库的 public schema 里自带三张给 agent 用的文档表。
# 这些是维护者手写的，比任何从 pg_catalog 推断出来的东西都权威，
# 所以直接读出来给 LLM，不要用推断去覆盖。
_GUIDE_TABLE = 'public."_guide"'
_CATALOG_TABLE = 'public."_catalog"'
_ISSUES_TABLE = 'public."_data_issues"'


async def guide(section: str | None = None, include_issues: bool = True) -> dict[str, Any]:
    """读出库内自带的使用指南、表目录和已知数据问题。"""
    out: dict[str, Any] = {}

    try:
        if section:
            rows = await db.fetch(
                f"SELECT position, section, content, updated_at FROM {_GUIDE_TABLE}"
                f" WHERE section = $1 OR section ILIKE '%' || $1 || '%'"
                f" ORDER BY position", section)
        else:
            rows = await db.fetch(
                f"SELECT position, section, content, updated_at FROM {_GUIDE_TABLE}"
                f" ORDER BY position")
        out["guide_sections"] = rows
        if section and not rows:
            avail = await db.fetch(
                f"SELECT section FROM {_GUIDE_TABLE} ORDER BY position")
            out["error"] = f"没有名为 {section!r} 的章节。"
            out["available_sections"] = [r["section"] for r in avail]
    except Exception as exc:
        out["guide_error"] = f"读取 {_GUIDE_TABLE} 失败：{exc}"

    if section is None:
        try:
            out["table_catalog"] = await db.fetch(
                f"SELECT table_name, module, grain, join_keys, description, caveats,"
                f" row_count, n_datasets FROM {_CATALOG_TABLE} ORDER BY table_name")
        except Exception as exc:
            out["catalog_error"] = f"读取 {_CATALOG_TABLE} 失败：{exc}"

    if include_issues:
        try:
            out["open_data_issues"] = await db.fetch(
                f"SELECT table_name, variable, severity, description, reported_by"
                f" FROM {_ISSUES_TABLE} WHERE status = 'open'"
                f" ORDER BY severity, table_name")
            counts = await db.fetch(
                f"SELECT status, count(*) AS n FROM {_ISSUES_TABLE} GROUP BY 1")
            out["issue_counts"] = {r["status"]: r["n"] for r in counts}
        except Exception as exc:
            out["issues_error"] = f"读取 {_ISSUES_TABLE} 失败：{exc}"

    out["note"] = (
        "以上内容由数据库维护者手写，是关于这个库的权威说明——"
        "跨数据集比较的陷阱、连接键、编码约定都在里面。"
        "和工具返回的其他推断性信息冲突时，以这里为准。"
    )
    if out.get("open_data_issues"):
        out["warning"] = (
            f"有 {len(out['open_data_issues'])} 个未解决的数据问题。"
            "如果分析涉及到这些表和变量，必须在结论里说明这个已知问题。"
        )
    return out
