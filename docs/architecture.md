# nl2data 架构文档

> 定位:本文是 **对外叙事**(未来开源 README 的母体);仓库根目录的
> [goals.md](../goals.md) 是**工程内部事实来源**。两者冲突时以 goals.md
> 为准,并应同步修正本文。

**一句话简介**:nl2data 是一个开源(Apache-2.0)的 CLI 工具,让你用自然语言
直接查询大体量(十万至百万行级)Excel/Access/Parquet 数据——答案永远附带
实际执行的 SQL,可人工核验。

## 1. 架构

```
文件(.xlsx/.xlsm/.mdb/.accdb/.parquet)
   │  ingest(零拷贝 Parquet 注册 / 转换落盘)
   ▼
data/parquet/<source>/<table>.parquet ──► data/warehouse.duckdb(表以 VIEW 注册)
   │                                            │
   │  profile(聚合 SQL 画像)                   │  cards(M-Schema 表卡片)
   ▼                                            ▼
data/catalog/profiles/*.json ──────► data/catalog/cards(_md)/*.json(md)
   │                                            │
   │                                            ▼
   │                     index(LanceDB 向量 + BM25,术语直通)
   │                                            │
   └──────────────────► retrieve(Top-K 表卡片,token 预算贪心)
                                                ▼
                                    generate(LLM → SQL,诚实反问)
                                                ▼
                                    guard(sqlglot 静态护栏)
                                                ▼
                                    run(只读沙箱执行 + 结果压缩)
                                                ▼
                                    interpret(第二次 LLM 解读)
                                                ▼
                                    答案 + SQL + 行数/耗时/token + 明细位置
```

原始数据文件不进入 LLM 上下文;模型仅看到 schema 卡片与(必要时压缩的)查询结果——大结果集(>20 行)只给统计画像与样本,小结果集(≤20 行)以内联样本透传。

## 2. 核心设计决策(摘要,权威清单见 goals.md §3/§5)

| # | 决策 | 要点 |
|---|---|---|
| 1 | 查询引擎 | DuckDB 嵌入式列存,表以 VIEW 注册 |
| 2 | 存储 | Parquet,`data/parquet/<source>/<table>.parquet` |
| 3 | LLM 接入 | 仅 OpenAI 兼容 API(`LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`),禁本地模型 |
| 4 | 工程底座 | Python ≥3.11 + uv,禁 requirements.txt |
| 5 | SQL 安全 | sqlglot 静态校验 + 只读连接 + 强制 LIMIT + 超时 |
| 6 | 交互形态 | CLI 优先(Typer + Rich),Web/MCP 推迟阶段三 |
| 7 | 质量门 | pytest 覆盖 ≥85% + ruff 零告警,是每个任务的完成前提 |
| 8 | Embedding | 仅 OpenAI 兼容 API(`EMB_BASE_URL/EMB_API_KEY/EMB_MODEL`)+ 磁盘缓存,不引入本地 embedding 模型 |

跨模块契约(变更即破坏性变更):`catalog.yaml` 血缘、profile JSON、
`clean_name()` 命名规则(拼音/`[a-z0-9_]`/≤63/冲突`_N`)、产物位置——
详见 goals.md §5。

## 3. 快速开始(约 30 分钟)

### 环境要求

Python ≥ 3.11、[uv](https://docs.astral.sh/uv/)、六个环境变量
(LLM 与 embedding 各一套,均为 OpenAI 兼容 API;DeepSeek/Qwen/Kimi/GLM 等
均可;同一厂商提供两者时,EMB_BASE_URL 可与 LLM_BASE_URL 相同):

```bash
export LLM_BASE_URL="https://your-llm-provider/v1"
export LLM_API_KEY="sk-..."
export LLM_MODEL="your-chat-model"
export EMB_BASE_URL="https://your-emb-provider/v1"
export EMB_API_KEY="sk-..."
export EMB_MODEL="your-embedding-model"
```

### 安装与全流程

```bash
git clone <repo> && cd nl2data
uv sync                                   # 创建 .venv 并安装依赖

# 1) 接入数据(三种来源,任选;零拷贝注册或转换落盘)
uv run nl2data ingest excel  path/to/订单.xlsx          # 多 sheet → 每 sheet 一表
uv run nl2data ingest access path/to/legacy.mdb          # 需 mdbtools(Linux/macOS/WSL)
cp path/to/big.parquet data/parquet/my_source/my_table.parquet
uv run nl2data ingest parquet data/parquet/my_source/my_table.parquet
# 表名 = 文件名(clean_name 规则见 goals.md §5)

# 2) 画像 → 卡片 → 检索索引(每步幂等,可重复执行)
uv run nl2data profile --all
uv run nl2data cards build --all
uv run nl2data index build

# 3) 提问(答案附实际执行的 SQL)
uv run nl2data ask "2026年3月黄色出租车的总订单量是多少?"
# 交互模式(支持 /retry [补充] /show sql /export csv <路径> /tables /exit):
uv run nl2data ask
```

中文列名自动转拼音安全名(`订单ID → ding_dan_id`),原名完整保留在
catalog.yaml 双向映射中;Excel 表头行自动探测(标题行/空首行下移)。

### 配置与词典

- 所有路径与阈值在 `config.yaml`(环境变量 `NL2DATA_CONFIG` 可覆盖位置);
  密钥只走环境变量,绝不落盘。
- 业务黑话进 `data/catalog/glossary.yaml`:术语 → 表/列/口径
  (表级 `filter` 仅本表生效;`metric_filter`+`applies_to` 是随指标全局
  生效的口径)。校验:`uv run nl2data glossary check`。
- 表级说明进 `docs/table_notes.md`(`# 表:<表名>` 小节)。

## 4. 评测体系(敢迭代)

三层判定,一条命令回归:

```bash
uv run nl2data eval e2e [--save-baseline]   # golden 20 条 × N=3 多数决
uv run nl2data eval recall                  # 仅召回层
uv run nl2data audit 10                     # 查看最近问答审计
```

- **L1 召回**:retrieve 是否召回期望表(Recall@3);
- **L2 SQL**:全链是否执行成功(反问=失败;护栏拒/执行错=错误;SQL 文本
  不比对——不同写法合法);
- **L3 结果**:与 golden 参考值语义等价(行多重集合、列超集投影、数值
  容差 + ×100 单位等价标注;不做行式/列式形状等价)。

golden 集(`eval/recall_golden.yaml`,20 条真实问答三元组)与基线
(`eval/baseline_e2e.json`,git 跟踪)构成回归锚点:改提示词/换模型后
重跑,与基线 diff 即逐 case 风险清单。比对器有已知边界(风格类差异不计
为回归缺陷,见 docs/milestone-4-notes.md §6/§7)。

## 5. 安全模型

**宁可误拒,不可漏放;拒绝必须给出可读原因。**

1. **上半层(guard)**:`guard/validate.py` 九条规则全走 sqlglot 解析树
   (注释/大小写/嵌套免疫)——语句白名单(仅 SELECT/WITH)、多语句拒绝、
   表白名单(CTE 作用域区分)、表函数一票否决(read_csv 等)、系统函数
   黑名单、列存在性(difflib 候选)、LIMIT 注入 500/封顶 10000。
2. **下半层(沙箱)**:`exec/runner.py` 只接受 `ValidatedSQL` 类型(编译期
   防绕护栏)+ DuckDB `read_only=True`(DB 层拒绝写操作)+ 守护线程超时。
3. **红队制度**:每轮安全相关变更由独立红队子智能体构造对抗样本
   (提示注入/SQL 注入/文件读取/CTE 藏写/伪造入参等,累计 100+ 样本),
   全部拦截后固化进测试套件;历史发现 2 个高危绕过家族(可写 CTE、
   SELECT..INTO)均已修复并回归钉死。
4. **密钥纪律**:API key 不写入任何文件/日志/异常消息(专项测试断言)。
   已知边界:read_only 对 COPY 导出与外部文件读取不设防,该场景依赖
   guard 单层(已有回归测试钉死,纵深复检在待办池)。

## 6. 开发指南

```bash
uv sync                                   # 安装(锁定于 uv.lock)
uv run pytest                             # 快速套件(slow 用例默认剔除)
uv run pytest -m slow                     # 10 万行大文件用例
uv run pytest --cov                       # 覆盖率(总门槛 85%)
uv run ruff check .                       # lint,必须零告警
```

约定:全类型注解;公开函数 docstring;注释英文;所有路径与阈值走
YAML/env,禁止硬编码。模块布局:`ingest/ catalog/ retrieval/ sqlgen/
guard/ exec/ eval/ llm/ nl2data/`(后者为 CLI 与配置)。

**多智能体流程**(本项目实践):调研/夹具/并行实现/独立审查/红队均派
子智能体,任务书内嵌完整契约(子智能体不共享上下文);主智能体负责
契约冻结、集成与"实现—审查—修复"闭环。质量门(测试+ruff+goals.md
状态更新)齐备才算任务完成。

## 7. 路线图与文档

- 阶段二:多候选 SQL + 选择器、实体索引、Python 分析沙箱、查询缓存;
- 阶段三:Web UI + MCP Server、多用户只读与审计、docker-compose;
- 里程碑历史与当前状态见 [goals.md](../goals.md) §4;各里程碑细节见
  `docs/milestone-{1..4}-notes.md`;
- 本文档为中文先行,开源前补英文 README(待办池在案)。
