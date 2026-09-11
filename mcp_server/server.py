"""MDA 数据库 MCP Server。

把「查数据库」包装成 6 个标准工具，任何 MCP 客户端都能调用：
  - 我们自己的网页后端
  - Claude Desktop / Claude Code
  - 别的支持 MCP 的工具

启动方式（stdio 传输，也就是通过标准输入输出通话）：
    uv run python -m mcp_server.server

针对这个库的设计取舍：
  1182 张表、6 万个列，且列名多为 a170201 这类编码，语义全在注释里。
  所以工作流必须是「搜索 → 看结构 → 抽样 → 查询」，
  而不是「把表结构全部读一遍」。工具说明里也明确告诉 LLM 这一点。
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import catalog, db, index, variables

INSTRUCTIONS = """\
这是一个只读的社会经济调查微观数据库，收录多个调查项目的协调（harmonized）微观数据。

# 最重要的一件事：列名没有语义，语义在元数据里

数据表的列名大多是问卷编码（a170201、hv123、b0202、q15_c20），看名字猜不出含义。
真正的语义信息存放在一套「变量协调层」里：变量定义、问卷原文、值标签（码值→含义）。
本 server 的工具就是这套协调层的入口。**不要靠猜表名或列名找数据。**

# 按问题类型选工具

**问「有哪些调查/数据集入库了」** → list_surveys()
   会区分三种状态：已入库可读 / 在库但无权限 / 根本没入库。
   如果某个调查是「无权限」或「未入库」，直接如实告诉使用者，不要含糊其辞，
   更不要用别的调查的数据冒充。

**问「某个调查包含哪些 section / 模块 / 章节」** → list_sections(survey)
   注意本库 section 有两种含义，工具会同时返回：
     physical_sections      数据的分表方式（CSES 的 EC/ED/HH/HO/VL，DHS 的 WM/CH/BR/PR…）
     questionnaire_sections 问卷文档的章节（"SECTION 2. REPRODUCTION"）
   使用者说「section」时通常指前者，但回答时把两者都提一下更有帮助。

**问「有没有 xx 相关的变量」（最常见）** → find_variables(keywords=[...])
   ★ 调用前必须把概念展开成多个英文同义词，这是这个工具能不能用好的关键。
   元数据是英文的，中文关键词搜不到任何东西。而且要覆盖同一概念的不同说法：

     使用者说「教育年限」
       → keywords=["education","schooling","grade","attainment","years attended",
                   "diploma","degree","certificate","literacy"]
       这样才能同时命中 years_attended_school（教育年限）和
       highest_education_level（最终学历）——使用者要的「语义相关」正是这个。

     使用者说「收入」
       → keywords=["income","earnings","wage","salary","revenue","remittance","profit"]
     使用者说「饮用水」
       → keywords=["drinking water","water source","improved water","piped water","well"]
     使用者说「营养不良」
       → keywords=["nutrition","stunting","wasting","underweight","anthropometry",
                   "height for age","weight for age","malnutrition"]

   宁可多给几个同义词也不要少给。搜索有词干还原（education 和 educational 互通），
   所以不用列单复数和词形变化，但要列真正不同的说法。
   第一次搜不到理想结果时，换一批同义词再搜，不要直接说「没有」。

   返回结果里 level 字段的含义（可靠性递减）：
     canonical  协调后的变量，有正式定义和语义分类，最可靠，优先推荐给使用者
     question   问卷原文，用来确认这个变量到底问的是什么
     source     原始数据列，覆盖面最广但只有一个简短标签

**问「这个变量有多少回答 / 取值怎么分布」** → variable_stats(table, column)
   返回 answered（非空回答数）、valid_answers（排除缺失值编码后的有效回答）、
   取值分布（带标签）、数值型的描述统计、以及可用的抽样权重列。

**要「可视化某个变量」** → plot_variable(table, column)
   自动选图型：类别少画条形图，连续数值画直方图。图会直接显示在网页上。

**要自己写 SQL 查数** → run_query(sql)
   写之前先用 describe_table 核对列名。

**涉及 MICS 数据、跨数据集比较、或要连接多张表** → 先调 read_guide()
   数据库自带一份维护者手写的使用指南，里面有推断不出来的关键约定，例如：
     · `CP_` 前缀的列是「carefully processed」清洗版本，跨数据集分析要优先用它
     · 原始变量的缺失哨兵是 7/9、97/98/99、9997-9999；`*_harmonized` / `*_years` 已置 NULL
     · 性别 1=男 2=女；是/否通常 1=是 2=否，但 MICS2 时代（1999-2001）常是 0=否 1=是
     · `final_WM_MICS` 的户标识列是 `hh_number`，不是 `household_number`
     · 连接键在部分表是 TEXT、部分表是 DOUBLE PRECISION，跨表连接要先转换类型
     · 键只在同一个 dataset_name 内唯一，连接时必须带上 dataset_name
     · 跨国分析用 `CP_country` / `CP_country_code`，不要去解析 dataset_name
   它还会返回未解决的数据问题清单——如果分析涉及那些表和变量，回答里必须说明。
   这份文档比本说明和其他工具的推断都权威，冲突时以它为准。

# 三条必须遵守的规则

1. **缺失值编码**。调查数据里 98="don't know"、99="missing" 这类编码是常态，
   当成真实数值算平均会得出荒谬结果（比如平均年龄 98 岁）。
   variable_stats 和 plot_variable 会读元数据自动识别并排除。
   如果工具返回 note_missing 说「元数据里找不到值标签」，
   说明无法确认有没有缺失编码——这时要先看取值分布再下结论，并在回答里说明这个不确定性。

2. **抽样权重**。算总体量（多少人、多少户、占比、均值）必须用抽样权重加权，
   直接 count 或 avg 得到的是样本数而非总体估计。
   工具会告诉你有哪些权重列（household_weight、person_weight 等），
   传 weight 参数即可。回答里要说明用了哪个权重列，或明确说这是未加权的样本数。

3. **不要编造数字**。任何具体数值都必须来自工具的真实返回结果。
   搜不到就说搜不到，并说明搜过哪些关键词。

# SQL 注意事项

- 表名和列名大小写敏感且含大写字母，必须加双引号：
      SELECT * FROM cses_data."final_HH_CSES"
- 数据库是只读的，写操作会失败，不要尝试。
- DHS 的表按国家分开存（final_WM_KH_DHS、final_WM_BD_DHS…），
  跨国分析需要 UNION 多张表，先用 list_tables 确认有哪些国家。
- MICS 相反：所有国家和轮次堆在同一张表里（public."final_HH_MICS" 等），
  靠 dataset_name / CP_country 区分，所以跨国分析不用 UNION，但要注意
  这些表很大（final_HL_MICS 有 1174 万行），务必加 WHERE 和 LIMIT。
- CSES 的 final_* 表在 cses_data，public schema 里有一批同名便捷视图，
  两者数据一样，工具会自动选实体表。
"""


mcp = MCPServer(
    name="mda-db",
    title="MDA 调查数据库",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)


def _qualified(schema: str, table: str) -> str:
    """拼出带双引号的完整表名，供 ::regclass 使用。

    这个库的表名含大写字母（final_HH_CSES），不加引号 PostgreSQL 会
    自动转小写导致找不到表。
    """
    return f'"{schema}"."{table}"'


# ---------------------------------------------------------------- 工具 1


@mcp.tool(
    description=(
        "列出数据库里所有可访问的 schema（数据分区），"
        "带每个 schema 的表数量和总体积。探索一个陌生库时先调这个。"
    )
)
async def list_schemas() -> dict[str, Any]:
    schemas = await db.fetch(db.SQL_LIST_SCHEMAS)
    return {
        "schemas": schemas,
        "hint": "命名规律：*_data 是清洗后的数据表，*_analysis 是分析结果，"
                "*_meta 是元数据，*_alignment 是跨年份变量对齐表。",
    }


# ---------------------------------------------------------------- 工具 2


@mcp.tool(
    description=(
        "列出某个 schema 下的表，带表注释、估算行数和体积。"
        "可用 name_contains 按表名过滤。注意有的 schema 有 600 多张表，"
        "建议配合过滤条件使用，或者直接用 search_metadata。"
    )
)
async def list_tables(
    schema: Annotated[str, Field(description="schema 名，例如 cses_data")],
    name_contains: Annotated[
        str | None, Field(description="按表名模糊过滤，不区分大小写")
    ] = None,
    limit: Annotated[int, Field(description="最多返回多少张表", ge=1, le=300)] = 60,
) -> dict[str, Any]:
    tables = await db.fetch(db.SQL_LIST_TABLES, schema, name_contains, limit)
    if not tables:
        return {
            "tables": [],
            "note": f"schema {schema!r} 下没有匹配的表，或者你没有访问权限。"
                    "可以先调 list_schemas 看有哪些可用的 schema。",
        }
    return {
        "schema": schema,
        "table_count": len(tables),
        "tables": tables,
        "note": f"达到 limit={limit} 上限，可能还有更多表。" if len(tables) == limit else None,
    }


# ---------------------------------------------------------------- 工具 3（核心）


@mcp.tool(
    description=(
        "按关键词搜索数据库的物理结构：表名、表注释、列名、列注释。"
        "适用于「我知道大概的表名或列名，帮我定位」这类需求，"
        "或者想看某张表的注释说明。\n"
        "★ 找变量请优先用 find_variables——它搜的是变量协调层"
        "（变量定义、问卷原文、值标签），带词干还原和相关性排序，"
        "对「有没有 xx 相关的变量」这类语义问题效果好得多。"
        "本工具搜的是物理结构，只做精确子串匹配，没有同义词和词干处理。"
    )
)
async def search_metadata(
    keyword: Annotated[
        str, Field(description="搜索关键词，建议用英文单词，如 income / education / age")
    ],
    schema: Annotated[
        str | None, Field(description="限定只在某个 schema 内搜索")
    ] = None,
    limit: Annotated[int, Field(description="最多返回多少条命中", ge=1, le=200)] = 50,
) -> dict[str, Any]:
    hits = await db.fetch(db.SQL_SEARCH, keyword, schema, limit)
    if not hits:
        return {
            "hits": [],
            "note": f"没搜到 {keyword!r}。试试换个更短的英文词、换同义词，"
                    "或者用 list_schemas 先了解库的结构。",
        }

    # 按表聚合一下，让 LLM 一眼看出「哪张表命中最多」——那通常就是目标表
    by_table: dict[str, int] = {}
    for h in hits:
        key = f'{h["schema"]}.{h["table"]}'
        by_table[key] = by_table.get(key, 0) + 1
    top = sorted(by_table.items(), key=lambda kv: -kv[1])[:10]

    return {
        "keyword": keyword,
        "hit_count": len(hits),
        "top_tables": [{"table": t, "matches": n} for t, n in top],
        "hits": hits,
        "note": (
            f"达到 limit={limit} 上限，可能还有更多命中；可加 schema 参数缩小范围。"
            if len(hits) == limit else None
        ),
        "next_step": "选定表之后调 describe_table 看完整列清单。",
    }


# ---------------------------------------------------------------- 工具 4


@mcp.tool(
    description=(
        "查看一张表的完整结构：所有列的名字、类型、是否可空、以及列注释；"
        "外加主键、外键、索引和估算行数。写 SQL 之前必须先调这个确认列名。"
    )
)
async def describe_table(
    schema: Annotated[str, Field(description="schema 名，例如 cses_data")],
    table: Annotated[
        str, Field(description="表名，大小写敏感，例如 final_HH_CSES")
    ],
) -> dict[str, Any]:
    ref = _qualified(schema, table)
    try:
        info = await db.fetch_one(db.SQL_TABLE_INFO, ref)
        columns = await db.fetch(db.SQL_DESCRIBE_COLUMNS, ref)
        constraints = await db.fetch(db.SQL_DESCRIBE_CONSTRAINTS, ref)
    except Exception as exc:
        return {
            "error": f"找不到表 {schema}.{table}，或者你没有权限访问：{exc}",
            "hint": "表名大小写敏感。可以用 search_metadata 或 list_tables 确认准确名字。",
        }

    indexes = await db.fetch(db.SQL_DESCRIBE_INDEXES, schema, table)

    return {
        **(info or {"schema": schema, "table": table}),
        "column_count": len(columns),
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "sql_reference": f'{schema}."{table}"',
        "note": "在 SQL 里引用本表和它的列时，含大写字母的名字必须加双引号。",
    }


# ---------------------------------------------------------------- 工具 5


@mcp.tool(
    description=(
        "从一张表里抽几行真实数据看看。用来确认取值形态——"
        "特别是缺失值编码（96/97/98/99 之类）和字段的实际格式。"
    )
)
async def sample_rows(
    schema: Annotated[str, Field(description="schema 名")],
    table: Annotated[str, Field(description="表名，大小写敏感")],
    columns: Annotated[
        list[str] | None,
        Field(description="只看这几列。表很宽（可能上百列）时强烈建议指定"),
    ] = None,
    limit: Annotated[int, Field(description="取几行", ge=1, le=50)] = 5,
) -> dict[str, Any]:
    if columns:
        # 逐个加双引号，同时挡掉引号注入
        for c in columns:
            if '"' in c:
                return {"error": f"列名 {c!r} 含非法字符。"}
        select_list = ", ".join(f'"{c}"' for c in columns)
    else:
        select_list = "*"

    sql = f'SELECT {select_list} FROM "{schema}"."{table}"'
    try:
        return await db.run_query(sql, limit=limit)
    except Exception as exc:
        return {
            "error": f"抽样失败：{exc}",
            "hint": "确认 schema / 表名 / 列名拼写和大小写；可先调 describe_table。",
        }


# ---------------------------------------------------------------- 工具 6


@mcp.tool(
    description=(
        "执行一条只读 SQL 查询并返回结果。"
        "只接受 SELECT / WITH / EXPLAIN 等只读语句，单次最多返回 500 行，"
        "超时 30 秒。表名列名含大写字母时记得加双引号。"
        "统计总体指标时请使用表里的抽样权重列加权。"
    )
)
async def run_query(
    sql: Annotated[str, Field(description="一条只读 SQL 语句，不要加结尾分号")],
    limit: Annotated[int, Field(description="最多返回多少行", ge=1, le=500)] = 100,
) -> dict[str, Any]:
    try:
        return await db.run_query(sql, limit=limit)
    except db.QueryRejected as exc:
        return {"error": f"查询被安全检查拒绝：{exc}"}
    except Exception as exc:
        # 把数据库的报错原文交回去，LLM 能据此自己改 SQL
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "常见原因：列名大小写没加双引号、表名写错、类型不匹配。"
                    "可以先用 describe_table 核对。",
        }


# ================================================================ 需求 1


@mcp.tool(
    description=(
        "列出数据库里有哪些调查项目（CSES / DHS / HSES / LFS 等），"
        "带调查数量、覆盖国家、年份范围、表数和体积。"
        "会明确区分三种状态：已入库可读 / 数据在库但当前角色无权限 / 根本没入库——"
        "被问到某个调查有没有时，按这个区分如实回答。"
    )
)
async def list_surveys() -> dict[str, Any]:
    return await catalog.overview()


# ================================================================ 需求 2


@mcp.tool(
    description=(
        "列出某个调查包含哪些 section（模块/章节）。"
        "返回两种：physical_sections 是数据的分表方式"
        "（CSES 的 EC/ED/HH/HO/VL，DHS 的 WM/CH/BR/PR 等，带含义说明）；"
        "questionnaire_sections 是问卷文档的章节。两者不是一一对应的。"
    )
)
async def list_sections(
    survey: Annotated[
        str, Field(description="调查代号，如 cses / dhs（先用 list_surveys 查看可用值）")
    ],
) -> dict[str, Any]:
    return await catalog.sections(survey)


# ================================================================ 需求 3（核心）


@mcp.tool(
    description=(
        "【找变量的主要工具】按语义搜索整个数据库的变量元数据"
        "（变量定义、问卷原文、变量标签、值标签）。\n"
        "★ 调用前必须把概念展开成多个英文同义词——元数据是英文的，"
        "中文关键词搜不到任何东西，而且同一概念在不同调查里叫法不同。\n"
        "例：使用者问「有没有教育年限相关的变量」，应传 keywords="
        '["education","schooling","grade","attainment","years attended",'
        '"diploma","degree"]，'
        "这样才能同时命中 years_attended_school 和 highest_education_level。\n"
        "宁可多给同义词也不要少给。搜索自带词干还原，不必列单复数词形。"
        "第一次结果不理想就换一批同义词再搜，不要直接说没有。"
    )
)
async def find_variables(
    keywords: Annotated[
        list[str],
        Field(description="英文同义词列表，3-10 个。必须是英文，可以是词组"),
    ],
    survey: Annotated[
        str | None, Field(description="限定调查：cses / dhs / mng_hses / mng_lfs")
    ] = None,
    section: Annotated[
        str | None, Field(description="限定 section，如 ED / WM / HO")
    ] = None,
    level: Annotated[
        str | None,
        Field(description="限定层级：canonical（协调变量，最可靠）/ question（问卷原文）"
                          "/ source（原始列）"),
    ] = None,
    limit: Annotated[int, Field(description="返回多少条", ge=1, le=100)] = 30,
) -> dict[str, Any]:
    if not keywords:
        return {"error": "keywords 不能为空。请把使用者的概念展开成几个英文同义词。"}
    if isinstance(keywords, str):
        keywords = [keywords]

    result = index.search(keywords, project=survey, section=section,
                          level=level, limit=limit)
    if "error" in result:
        return result

    # 分面统计：告诉使用者命中集中在哪些调查/section/语义分类，便于收窄范围
    result["facets"] = index.facets(keywords, project=survey)

    if not result["matches"]:
        result["hint"] = (
            "没搜到。试试：① 换一批同义词（元数据是英文的）；"
            "② 用更宽的上位词（比如从 'years of schooling' 改成 'education'）；"
            "③ 去掉 survey / section 限定。"
        )
    else:
        result["next_step"] = (
            "选定变量后：variable_stats(table, column) 看有多少回答和取值分布；"
            "plot_variable(table, column) 画图；describe_table 看整表结构。"
            "container 字段是变量所在的表名。"
        )
    if result.get("index_stale"):
        result["warning"] = (
            f"元数据索引已建立 {result['index_age_days']} 天，"
            "如果期间有新数据入库，可能搜不到新变量。可调 rebuild_metadata_index 重建。"
        )
    return result


# ================================================================ 需求 4


@mcp.tool(
    description=(
        "统计一个变量：有多少非空回答、排除缺失值编码后的有效回答、"
        "取值分布（带码值含义）、数值型的描述统计（均值/中位数/标准差）、"
        "以及可用的抽样权重列。"
        "缺失值编码（98=不知道、99=缺失这类）会根据元数据自动识别并单独报告，"
        "不是靠猜 96-99；元数据里查不到值标签时会明确说明无法判断。"
    )
)
async def variable_stats(
    table: Annotated[str, Field(description="表名，如 final_ED_CSES（大小写敏感）")],
    column: Annotated[str, Field(description="列名，如 years_attended_school")],
    schema: Annotated[
        str | None, Field(description="schema 名。省略时自动按表名解析")
    ] = None,
    weight: Annotated[
        str | None,
        Field(description="抽样权重列名。算总体量时应该传，返回值里会列出可用的权重列"),
    ] = None,
) -> dict[str, Any]:
    located = await variables.locate(table, schema)
    if not located["found"]:
        return located
    try:
        return await variables.stats(
            located["schema"], located["table"], column, weight=weight
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}",
                "hint": "列名大小写敏感，可先调 describe_table 核对。"}


# ================================================================ 需求 5


@mcp.tool(
    description=(
        "把一个变量画成图，图会直接显示在网页上。"
        "kind='auto' 时自动选择：类别型画条形图（用码值含义当标签），"
        "连续数值画直方图。缺失值编码默认排除。"
        "传 weight 参数可按抽样权重加权——调查数据展示总体分布时应该加权。"
    )
)
async def plot_variable(
    table: Annotated[str, Field(description="表名，如 final_HO_CSES（大小写敏感）")],
    column: Annotated[str, Field(description="列名")],
    schema: Annotated[
        str | None, Field(description="schema 名。省略时自动按表名解析")
    ] = None,
    kind: Annotated[
        str, Field(description="auto / bar（条形图）/ histogram（直方图）")
    ] = "auto",
    bins: Annotated[int, Field(description="直方图分几组", ge=2, le=60)] = 20,
    top: Annotated[int, Field(description="条形图最多显示几个类别", ge=2, le=40)] = 25,
    weight: Annotated[str | None, Field(description="抽样权重列名")] = None,
    include_missing: Annotated[
        bool, Field(description="是否把缺失值编码也画进去（默认排除）")
    ] = False,
) -> dict[str, Any]:
    located = await variables.locate(table, schema)
    if not located["found"]:
        return located
    try:
        return await variables.plot(
            located["schema"], located["table"], column,
            kind=kind, bins=bins, top=top, weight=weight,
            include_missing=include_missing,
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ================================================================ 库内文档


@mcp.tool(
    description=(
        "读出数据库自带的使用指南（public._guide）、表目录（public._catalog）"
        "和未解决的数据问题（public._data_issues）。\n"
        "★ 这是数据库维护者手写的权威文档，包含跨数据集比较的关键陷阱："
        "哪些列是清洗过的（CP_ 前缀）、缺失值哨兵编码、"
        "不同年代数据集的编码差异、表之间的连接键和坑。\n"
        "涉及 MICS 数据、或者要做跨数据集/跨国比较、或者要连接多张表时，"
        "先调这个工具读一遍。它和其他工具的推断性说明冲突时，以它为准。"
    )
)
async def read_guide(
    section: Annotated[
        str | None,
        Field(description="只读某一章节，如 join_conventions / coding_conventions / "
                          "cp_prefix / harmonized_variables。省略则返回全部"),
    ] = None,
) -> dict[str, Any]:
    return await catalog.guide(section)


# ================================================================ 索引维护


@mcp.tool(
    description=(
        "重建本地元数据索引。数据库里新增了调查/变量之后，"
        "或者刚开通了新 schema 的权限之后，需要重建一次搜索才能找到新内容。"
        "耗时约 10 秒到 2 分钟，取决于数据量。"
    )
)
async def rebuild_metadata_index() -> dict[str, Any]:
    try:
        info = await index.build()
    except Exception as exc:
        return {"error": f"重建失败：{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "total_rows": info["total_rows"],
        "elapsed_seconds": info["elapsed_seconds"],
        "per_project": info["per_project"],
        "skipped": info["skipped"],
        "errors": info["errors"] or None,
    }


@mcp.tool(description="查看本地元数据索引的状态：建立时间、条目数、各调查的覆盖情况。")
async def metadata_index_status() -> dict[str, Any]:
    return index.info()


def main() -> None:
    """以 stdio 方式启动 MCP server。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
