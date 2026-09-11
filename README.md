# MDA 数据库问答

给 `mda` 数据库（柬埔寨 CSES、DHS、蒙古 HSES/LFS 等调查微观数据）做的
MCP Server + 网页问答界面。用中文提问，LLM 自动搜索元数据、写 SQL、返回结果。

## 快速开始

```bash
# 1. 装依赖（只需一次）
uv sync

# 2. 启动
uv run uvicorn backend.main:app --reload --port 8000

# 3. 浏览器打开 http://localhost:8000
#    首次使用点「设置」，填入 DeepSeek 或 Gemini 的 API Key
```

变量搜索依赖一个本地索引（已建好，10.6 万条）。**数据库新增调查或变量后，
到设置页点「重建索引」**（约 9 秒），否则搜不到新内容。

申请 Key：
- DeepSeek：https://platform.deepseek.com/api_keys （便宜，中文好）
- Gemini：https://aistudio.google.com/apikey （有免费额度）

模型名在网页上是**自由输入**的，下拉列表只是建议。两家的模型 ID 换得很快
（DeepSeek 已从 `deepseek-chat` 换成 `deepseek-flash`，Gemini 从 2.5 一路到 3.8），
所以别把它写死在代码里，去控制台看当前可用的名字直接填。

## 结构

```
mcp_server/          MCP Server —— 把「查数据库」包装成 14 个标准工具
  db.py              连接池 + 通用 SQL（元数据查询、安全检查）
  catalog.py         调查目录层：四个调查族三种元数据形态，用适配器统一
  index.py           本地 FTS5 变量索引（建索引 + 检索排序）
  variables.py       单变量统计与可视化（缺失值、加权）
  server.py          工具定义，也是给 LLM 的使用说明
backend/             网页后端
  main.py            FastAPI 接口
  config_store.py    API Key 加密存储、供应商与推理参数定义
  history.py         聊天历史（SQLite）
  mcp_bridge.py      后端作为 MCP 客户端，翻译工具格式给 LLM
  agent.py           agent 循环：LLM ↔ 工具的来回对话
web/index.html       前端（单文件，无需构建，中英双语）
test_mcp.py          手动测试 MCP Server
grants.sql           开通蒙古 HSES/LFS 只读权限（需你自己执行）
revoke_grants.sql    撤销上述授权
```

## MCP Server 提供的 14 个工具

### 按调查结构探索

| 工具 | 用途 |
|---|---|
| `list_surveys` | 有哪些调查入库了。区分**已入库可读 / 在库但无权限 / 未入库**三种状态 |
| `list_sections` | 某调查包含哪些 section。返回物理分表和问卷章节两种，附 grain 和连接键 |
| `read_guide` | 读库内自带的 `public._guide` / `_catalog` / `_data_issues` |

### 找变量（核心）

| 工具 | 用途 |
|---|---|
| `find_variables` | **主要工具**。在本地 FTS5 索引里按语义搜变量，< 10ms，带词干还原和相关性排序 |
| `rebuild_metadata_index` | 重建索引。新数据入库或新开通 schema 权限后需要跑一次 |
| `metadata_index_status` | 索引状态：建立时间、条目数、各调查覆盖情况 |

### 看变量和画图

| 工具 | 用途 |
|---|---|
| `variable_stats` | 有多少回答、取值分布（带码值含义）、描述统计、加权结果 |
| `plot_variable` | 画条形图或直方图，图直接显示在网页上 |

### 通用

| 工具 | 用途 |
|---|---|
| `list_schemas` / `list_tables` / `describe_table` | 物理结构 |
| `search_metadata` | 搜表名/列名/注释（精确子串匹配，找变量请用 `find_variables`） |
| `sample_rows` | 抽几行看真实取值 |
| `run_query` | 执行只读 SQL |

## 数据库里有什么

全部 5 个调查族都已开通只读访问（`mda_viewer` 可见 1339 张表）：

| 调查 | 规模 | 位置 |
|---|---|---|
| **MICS**（UNICEF 多指标簇调查） | 255 数据集 / 97 国 / MICS2–MICS6（1999–2023） | `public` |
| **DHS**（人口与健康调查） | 112 调查 / 82 国 / 1985–2024 | `dhs_*` |
| **CSES**（柬埔寨社会经济调查） | 10 个波次 / 2004–2021 | `cses_*` |
| **HSES**（蒙古家庭社会经济调查） | 11 个波次 / 2002–2020 / 5.9 GB | `mng_hses_*` |
| **LFS**（蒙古劳动力调查） | 14 个波次 | `mng_lfs_*` |

另有气候数据（`final_CLIMATE_DAILY_ADMIN2` 等）和尼泊尔生活标准调查 2022，都在 `public`。

### 权限

蒙古的 6 个 schema 最初对 `mda_readonly` 没有 USAGE 权限，已通过 `grants.sql`
开通（只授予 SELECT，角色仍是只读）。撤销用 `revoke_grants.sql`。

**开通新 schema 后必须重建索引**，否则搜不到新数据。

### 四个调查族，三种元数据形态

`catalog.py` 用「每个调查一个适配器、输出统一 10 列」的方式抹平差异：

| 调查 | 协调层 | 变量层 | 值标签 | section 来源 |
|---|---|---|---|---|
| MICS | `ind_que_*_MICS`（有 `measure_type` / `canonical_text`） | 同左 | — | `public._catalog` |
| CSES / DHS | `*_canonical_variable` | `*_source_variable` | `*_value_mapping` | 表名正则 |
| HSES | `concept`（有 `module_family`） | `variable` | `value_label` | `module_family` |
| LFS | `canonical_variable` | `mng_lfs_meta.variable` | `mng_lfs_meta.value_label` | 无 |

加新调查（比如将来接别的国家）只需在 `PROJECTS` 里加一条并写好它的
`extracts` SQL，五个工具全部自动生效。

### 两个针对具体数据的处理

**HSES 的协调层有 3 个发布版本**（`hses-alignment-v1` / `v1.1` / `v1.2`），
每个概念在每个版本里各存一行。工具只取最新版本 —— 版本号用 `int[]` 比较
而不是字符串，否则 `v1.10` 会被判定小于 `v1.9`。

**HSES 只索引 concept 层，不索引原始列层**：1881 个原始列名里有 1877 个
已被 concept 覆盖（它的 `aligned_name` 常常就是原始列名），两层都索引会让
每次搜索结果出现两份重复。

**LFS 的协调变量会标注状态**：8 个变量里只有 2 个是 `qualified_core`，
其余 6 个是 `candidate_only`（尚未验证）。工具把状态拼进变量说明里，
避免 LLM 把候选变量当成已验证的推荐出去。

## 「语义搜索」是怎么做的## 「语义搜索」是怎么做的

`find_variables` 不是向量检索。语义能力来自两层配合：

1. **LLM 扩词**：你说「教育年限」，LLM 展开成
   `["education","schooling","grade","attainment","years attended","diploma"]`
2. **本地 FTS5 索引检索排序**：10 万条变量元数据，porter 词干还原 + BM25 排序

所以搜「教育年限」能同时找到 `years_attended_school`（教育年限）和
`highest_education_level`（最终学历）—— 这正是你要的「语义相关」。

为什么不直接查数据库：

| | 实时查库 | 本地 FTS5 索引 |
|---|---|---|
| 耗时 | 1.4 秒 | **< 10ms** |
| 词干还原 | 无 | ✅ education / educational 互通 |
| 相关性排序 | 手写 | ✅ 内置 BM25 |
| 数据库负载 | 每次全表扫 130 万行 | 零 |

装不了 `pg_trgm` / `pgvector`（只读角色没权限），所以索引放在本地
`~/.mda_db_mcp/metadata_index.db`（约 28 MB，建一次 9 秒）。

**代价**：索引是快照。新数据入库后要重建，否则搜不到新变量。
`find_variables` 会返回索引年龄，超过 30 天会附带提醒。

### 排序的实用性调整

除了 BM25 相关性，排序还做了几项调整，依据来自库内文档和数据分布：

- `canonical`（协调变量，有定义和语义分类）排在 `source`（原始列裸标签）前
- **`CP_` 前缀的列往前排** —— `_guide` 的 `cp_prefix` 章节明确说跨数据集分析要优先用它们
- `survey_specific_response` 类往后压 —— DHS 里有 777 个这种单一调查专用变量
  （`JO_2023_woman_education_recode`），关键词一命中就会挤掉真正可跨国比较的变量
- 覆盖数据集多的变量往前排

## 库内自带的文档（重要）

数据库的 `public` schema 里有三张维护者手写的文档表，比任何从 `pg_catalog`
推断出来的信息都权威。`read_guide` 工具把它们读出来给 LLM：

| 表 | 内容 |
|---|---|
| `_guide` | 7 章使用指南：连接约定、编码约定、协调变量清单、`CP_` 前缀规则 |
| `_catalog` | 每张表的 grain、连接键、说明、caveats、行数、数据集数 |
| `_data_issues` | 已知数据问题（当前 1 个未解决、63 个已修） |

里面有几条推断不出来但极其关键的规则：

- **`CP_` 前缀 = carefully processed**，跨数据集分析优先用这些列
- 原始变量的缺失哨兵是 7/9、97/98/99、9997-9999；`*_harmonized` / `*_years` 已置 NULL
- 性别 1=男 2=女；是/否通常 1=是 2=否，**但 MICS2 时代（1999–2001）常是 0=否 1=是**
- `final_WM_MICS` 的户标识列是 `hh_number`，不是 `household_number`
- 连接键在部分表是 TEXT、部分是 DOUBLE PRECISION，跨表连接要先转类型
- 键只在同一 `dataset_name` 内唯一，连接必须带上 `dataset_name`
- 跨国分析用 `CP_country` / `CP_country_code`，不要解析 `dataset_name`

MCP Server 的工具说明已经把这些写给 LLM，并要求它涉及 MICS 或跨数据集比较时先读指南。

## 缺失值与加权

这是调查数据分析最容易出错的两点，工具层面都做了处理：

**缺失值编码**：98="don't know"、99="missing" 这类编码在元数据里是机器可读的
（`value_labels` JSONB），所以不靠猜 96–99，而是读元数据判断。
读不到时会明确说「无法判断」而不是假装没有。

**抽样权重**：`variable_stats` 和 `plot_variable` 会自动发现权重列
（`household_weight`、`person_weight` 等）并在返回里列出；传 `weight` 参数即加权。
未加权时会提示「这是样本数不是总体估计」。

实测差异：CSES 教育年限未加权均值 6.33，加权后 6.20。

## 界面语言

右上角按钮切换中文 / English。切换的不只是界面文案——**模型的回答语言也会跟着换**，
所以给客户演示时切成 English，整个界面和答案都是英文的。

语言选择存在浏览器 localStorage 里，同时同步到后端配置。
进度提示（「正在思考…」「返回 120 行 × 8 列」）也是按当前语言渲染的：
后端只发 `summary_key` + 参数，文案由前端拼，所以加语种不用改后端。

## Markdown 渲染

LLM 的回答按 Markdown 渲染：表格、标题、列表、代码块、粗体、链接。
给客户演示时表格能正常显示，不是一堆 `|---|---|`。

**不引 marked / DOMPurify**，自己写了一个约 150 行的渲染器，原因：

1. 保持单文件零依赖、离线可用 —— CDN 挂了不会让页面变空白
2. 安全模型简单到能一眼看完：**先把整段文本转义，之后渲染器生成的标签
   就是页面上唯一的 HTML**，LLM 输出和数据库内容都无法注入

占位符用 `<F0>` 这种形式：转义之后文本里不可能再出现 `<`（都成了 `&lt;`），
所以用户或 LLM 无论写什么都撞不上占位符。

### 故意不支持 `_斜体_` 和 `__粗体__`

这个库的变量名全是下划线。支持下划线强调会把回答毁掉：

```
years_attended_school      →  years<em>attended</em>school
q0611__2020_000cb8d1       →  q0611<strong>2020</strong>000cb8d1
```

LLM 写强调基本都用 `*`，所以这个取舍没有实际损失。

### 链接白名单

只放行 `http://`、`https://`、`mailto:` 和站内相对路径。
`javascript:`、`data:`、`vbscript:` 以及 `//evil.com`（协议相对 URL，会跳外站）
都拒绝，原样显示成文本。

### 流式渲染节流

一次回答有上百个 token，每来一个就重排表格既抖又费。
所以流式期间 120ms 渲染一次，收到 `done` 时立即定稿。
原文存在元素的 `dataset` 上，定时器回调取最新值，不会渲染到过期内容。

### 错误信息不走 Markdown

报错文字是我们自己生成的，里面常带 SQL 报错原文（含 `*`、`_`、`|` 等字符），
按纯文本显示更不容易看错。

## 聊天历史

自动保存，存在 `~/.mda_db_mcp/history.db`（SQLite）。

**为什么不存在 mda 数据库里**：mda 是只读的（角色 `mda_viewer` 属于 `mda_readonly`
组），写不进去；而且调查数据库不该混进应用自己的数据。

每条助手消息除了正文，还存下：
- **工具调用过程**（包括完整 SQL）—— 打开历史会话时会原样重现，给客户演示时可以回放
- **思考过程** —— 模型的推理内容
- **token 用量** —— 用来核对推理档位的实际开销

侧边栏操作：单击打开、**双击标题重命名**（自动生成的标题多半不适合给客户看）、
悬停点 × 删除。设置页有「清空全部历史」。

历史库和 API Key 一样是 600 权限，包括 SQLite 的 `-wal` / `-shm` 旁支文件
（它们装着同样的内容，只锁主库等于没锁）。

## 推理与 Token 控制

设置页的「推理与 Token 控制」有三个旋钮，作用范围不同：

| 控件 | 作用 | 什么时候用 |
|---|---|---|
| 推理档位 `reasoning_effort` | 两家通用的标准档位 | 日常调节的首选 |
| 输出 token 上限 | 输出总量上限 | **限制 thinking 开销最直接的手段** |
| 高级：原生参数 (JSON) | 直接透传给 API | 上面两个表达不了时 |

**为什么用 `reasoning_effort` 而不是各家的原生参数**：各家把它映射到自己的原生
参数（Gemini 3.x → `thinking_level`，Gemini 2.5 → `thinking_budget`，
DeepSeek → thinking 模式档位），原生参数名一直在变，标准参数不用跟着改。

各家可选档位：

- **DeepSeek**：`none` / `low` / `high` / `max`，默认 `high`。
  选 `none` 关闭思考，回答更快更便宜，简单查询够用。
- **Gemini**：`none` / `minimal` / `low` / `medium` / `high`。
  `none` 只有 2.5 Flash 系列支持，**2.5 Pro 和 3.x 系列不能关闭思考**，传了会报错。

**关于输出上限**：思考 token 算在输出里，所以这是限制 thinking 花费最有效的办法。
但设太小会让回答被截断在半句话——建议至少留 4096。

**关于高级参数**：只在标准档位不够用时使用，比如要指定确切的预算：

```json
{"google": {"thinking_config": {"thinking_level": "low", "include_thoughts": true}}}
```

注意 `reasoning_effort` 和 `thinking_level` / `thinking_budget` **互斥**，
同时设置会报 400。

**参数被拒时不会静默降级**：如果你设的档位这个模型不支持，界面会明确报错并告诉你
是哪个设置的问题——不会偷偷丢掉参数重试，那样你会以为设置生效了。

**看实际花了多少**：每条回答下面会显示 `token：输入 8900 · 思考 100 · 输出 190`，
多轮工具调用是累加的。调完档位看这一行就知道有没有效果。

**DeepSeek 的一个坑**：思考模式下不支持指名工具调用（`required` 或指定某个工具），
会返回 400。本项目用的是自动模式（`auto`），不受影响，但你要是改代码去指定工具，
记得先关思考。

## 安全设计

三层防护，任一层单独失效都不会造成数据损坏：

1. **数据库层**：连接角色 `mda_viewer` 只属于 `mda_readonly` 组，
   PostgreSQL 直接拒绝任何写操作，也访问不到 `mng_*` 那几个受限 schema。
2. **连接层**：连接池设 `default_transaction_read_only=on`，
   每条查询再包一层 `readonly=True` 事务 + 30 秒超时。
3. **SQL 层**：只放行 `SELECT`/`WITH`/`EXPLAIN` 等开头的语句，
   拒绝多语句，拉黑 `pg_read_file`、`dblink`、`COPY` 等函数。

另外：
- 查询结果用游标分页，只从数据库取需要的行数
  （库里有几百 MB 的表，全量拉回来会打满内存）
- 单次最多返回 500 行
- **数据库密码不存在配置里**，走 `~/.pgpass`
- API Key 用 Fernet 加密存 `~/.mda_db_mcp/config.json`，
  密钥文件 `secret.key` 权限 600，都在项目目录之外，不会被 git 提交

## 挂到 Claude Desktop / Claude Code

这个 MCP Server 是标准实现，不止能给自家网页用。

### Claude Code

在项目目录下执行，路径自动解析，不用手填：

```bash
claude mcp add mda-db \
  --env MDA_DATABASE_URL=postgresql://mda_viewer@localhost:5432/mda \
  -- "$(pwd)/.venv/bin/python" -m mcp_server.server
```

### Claude Desktop

配置文件不支持变量展开，所以要填绝对路径。先在项目目录下打印出该填的值：

```bash
echo "command: $(pwd)/.venv/bin/python"
echo "cwd:     $(pwd)"
```

然后编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`
（Windows 在 `%APPDATA%\Claude\`），把上面两个值填进去：

```json
{
  "mcpServers": {
    "mda-db": {
      "command": "<上面打印的 command>",
      "args": ["-m", "mcp_server.server"],
      "cwd": "<上面打印的 cwd>",
      "env": { "MDA_DATABASE_URL": "postgresql://mda_viewer@localhost:5432/mda" }
    }
  }
}
```

`command` 必须指向项目虚拟环境里的 python（`.venv/bin/python`），
不能用系统 python —— 否则找不到 mcp、asyncpg 这些依赖。

## 环境变量（可选，优先级高于网页配置）

| 变量 | 用途 |
|---|---|
| `MDA_DATABASE_URL` | 数据库连接串 |
| `MDA_LLM_API_KEY` | LLM API Key（设了它网页里就改不动） |
| `MDA_CONFIG_DIR` | 配置和历史库目录，默认 `~/.mda_db_mcp` |

## 排查问题

**网页显示「数据库未连接」**
```bash
psql -h localhost -U mda_viewer -d mda -c "select 1"   # 先确认数据库本身能连
uv run python test_mcp.py                               # 再单测 MCP Server
```

**提问报 HTTP 400 / 401**
Key 不对或没额度。设置页点「测试连接」，错误原文会显示出来。

**LLM 说找不到变量**
先确认索引存在：设置页看「元数据索引」，没有就点「重建索引」。
索引在的话，让它换一批英文同义词再搜——元数据是英文的，中文关键词搜不到。

**新入库的数据搜不到**
索引是快照，需要重建：设置页点「重建索引」，或让 LLM 调
`rebuild_metadata_index`。建一次约 9 秒。

**开通了新 schema 权限但还是搜不到**
`grants.sql` 只改数据库权限，索引不会自动更新。执行后必须重建索引。

**某个调查搜出来的结果成对重复**
说明它的协调层和原始列层指向同一批变量（HSES 就是这种情况，已在
`catalog.py` 里去掉了它的 source 层）。新接调查时要检查这一点：
比较协调层的变量名集合和原始列名集合的重叠度。

**回答里的数字看着不对**
调查数据的缺失值常用 96/97/98/99 编码，直接求平均会算错。
展开「查看 SQL」核对它有没有排除这些值、有没有用抽样权重加权。

**回答说到一半就断了**
输出 token 上限设太小了。思考 token 也算在里面，调大或设成 0。

**报错提到 thinking / reasoning / effort**
推理设置这个模型不接受。最常见两种：给 Gemini 2.5 Pro 或 3.x 传了
`reasoning_effort: none`（它们不能关思考）；或者档位和高级参数里的
`thinking_level` 同时设了（两者互斥）。

**切了英文但模型还是回中文**
语言指令是在提问时发出去的，只影响新提问。已有的回答不会重新翻译。
