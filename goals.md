# nl2data 项目目标文件（goals.md）

> 本文件是项目的**最高事实来源**。任何会话开始、任何上下文压缩恢复后，第一件事是阅读本文件。
> 当历史对话、临时讨论、记忆与本文件冲突时，**以本文件为准**。
> 修改规则见文末「§8 变更规则」。

---

## §1 项目愿景

让个人与团队用自然语言直接查询大体量（十万至百万行级）Excel/Access 表格数据，得到**可信、可核验**的答案。

## §2 总体目标（North Star）

一个开源（Apache-2.0）的 CLI 优先工具，链路为：

```
文件 → Parquet → DuckDB → LLM 生成 SQL → 静态校验 → 沙箱执行 → 结果压缩 → 自然语言解读
```

- 答案**永远附带实际执行的 SQL**，可人工核验
- 原始大结果集不进入 LLM 上下文，只给统计画像 + 抽样
- 代码从第一天起按开源标准编写（配置外置、英文注释、测试齐全）

### 明确不做（Non-goals）

- **不做本地模型适配**（Ollama 等）——LLM 仅通过 API 调用（见 §3 决策 3）
- MVP 阶段不做：多轮对话记忆、图表可视化、用户系统、Web UI
- 不支持：.xlsb / .numbers / 加密的 .accdb；Access 只读不写

## §3 锁定的架构决策（ADR 摘要）

| # | 决策 | 内容 | 状态 |
|---|------|------|------|
| 1 | 查询引擎 | DuckDB（嵌入式列存），仓库文件 `data/warehouse.duckdb`，表以 VIEW 方式注册 | 锁定 |
| 2 | 存储格式 | Parquet，落盘 `data/parquet/<source_slug>/<table_name>.parquet` | 锁定 |
| 3 | **LLM 接入** | **仅 OpenAI 兼容 API**：环境变量 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 配置，可切换 DeepSeek / Qwen / Kimi 等服务商；**不引入任何本地模型依赖，也不为其预留抽象层** | 锁定 |
| 4 | 语言与依赖 | Python ≥ 3.11，uv 管理依赖（pyproject.toml），禁止 requirements.txt | 锁定 |
| 5 | SQL 安全 | sqlglot 静态校验（只读、表白名单、列存在性）+ 只读连接 + 强制 LIMIT + 超时 | 锁定 |
| 6 | 交互形态 | CLI 优先（Typer + Rich）；Web UI / MCP 推迟到阶段三 | 锁定 |
| 7 | 测试与质量 | pytest（覆盖率 ≥85%）+ ruff 零告警，是每个任务的完成前提 | 锁定 |
| 8 | Embedding 接入 | 仅 OpenAI 兼容 API：环境变量 EMB_BASE_URL / EMB_API_KEY / EMB_MODEL 配置；embedding 结果落本地磁盘缓存避免重复调用；不引入本地 embedding 模型 | 锁定 |
| 9 | 对外封装 | 仅经 MCP tools + SKILL.md；tools 面最小化（status / list_tables / ask），密钥只走环境变量，任何 tool 参数与返回值不得携带密钥值 | 锁定 |

> 注：早期讨论中的"Provider 抽象层 + Ollama 可切换"方案已被决策 3 取代，不再有效。

## §4 阶段目标与验收

### 阶段一：个人 MVP

**里程碑 1：数据能进来** —— 状态：🟢 完成（2026-09-18，V1 总验收通过）
- T1 项目骨架（uv / ruff / pytest / Apache-2.0 / CLI 占位）—— 状态：🟢
- T2 Excel 接入（多 sheet → Parquet + DuckDB 视图 + catalog.yaml 血缘）—— 状态：🟢
- T3 Access 接入（mdbtools → Parquet，契约与 T2 一致，复用 ingest/common.py）—— 状态：🟢
- T4 数据画像（profiler.py → profile JSON，聚合 SQL 实现，禁逐列扫表）—— 状态：🟢
- 完成判据：全新环境重建后 `ingest excel → ingest access → profile --all` 全链路跑通，V1 审查全项通过 ✅（Access 真实链路因本机无 mdbtools 按豁免以 mock 单测 + 后缀拒绝实测覆盖，详见 docs/milestone-1-notes.md）

**里程碑 2：LLM 能理解库结构** —— 状态：🟢 完成（2026-09-19，V2 终验：M3 起点基线 Recall@3=1.000 满分）
- M-Schema 风格表卡片生成、glossary.yaml 术语词典、LanceDB + BM25 混合检索（Top-K 带 token 预算）
- T5 M-Schema 表卡片（catalog/cards.py + table_notes.py → data/catalog/cards[/cards_md]）—— 状态：🟢
- T6 术语词典 glossary（加载/校验/查询 + CLI check/list）—— 状态：🟢
- T7 检索（LanceDB + BM25 混合，Top-K 带 token 预算；卡片与术语为索引对象）—— 状态：🟢（审查通过）
- G2 golden 评测集（eval/recall_golden.yaml 20 条 + 校验器）—— 状态：🟢
- G3 业务术语词典（19 条 + 术语增益 A/B 测量）—— 状态：🟢
- 完成判据：对任意问题能召回正确的表集合（用里程碑 4 的评测集衡量）

**里程碑 3：问数闭环** —— 状态：🟢 完成（2026-09-19 人类裁定：19/20 验收通过）
- LLM API 客户端（按 §3 决策 3）、SQL 生成提示词、sqlglot 护栏、DuckDB 沙箱执行、结果压缩、CLI 问答循环（答案附 SQL）
- T8 LLM 客户端（llm/chat.py）—— 🟢；T9 SQL 生成（sqlgen/，模板+few-shot 经人类审定）—— 🟢；T10 护栏（guard/，红队双轮零穿透）—— 🟢；T11 沙箱执行（exec/runner.py）—— 🟢；T12 问答闭环+审计（nl2data/qa.py+audit.py+CLI ask）—— 🟢
- 完成判据：对自己的真实数据提问，连续 20 问无破坏性操作、答案均可核验（记录模板见 docs/milestone-3-notes.md §8，待人类执行）
  2026-09-19 验收：19/20。安全底线与可核验性 20/20 全过；#12（小费率业务口径）失败，归因语义层 schema 缺口（词条 filter 按表局部适用 vs 指标口径全局适用的歧义），转 M4 以指标级 metric_filter 修复，方案已列入 §7。

**里程碑 4：敢迭代** —— 状态：🟢 完成（2026-09-19 V4 终验：受控实验证明回归 runner 敏感且可复现）
- golden.yaml 评测集（≥10 条真实问答）+ 回归 runner + docs/architecture.md
- 完成判据：改提示词后一键回归，指标可对比



**里程碑 5：MCP + Skill 封装** —— 状态：🟡 进行中（独立小里程碑，不改变阶段划分；两项已完成，待人类通读 SKILL.md 后收官）
- MCP Server（tools：status / list_tables / ask）—— 🟢（T17，mcp 2.2 stdio）；SKILL.md 封装 —— 🟢（T18，skills/nl2data/ 三件套）
- 完成判据：AI 宿主可显式调用 nl2data 完成接入到问答全流程；缺失配置时由宿主 LLM 向用户索要；密钥零出现在 tool 参数/返回值（专项测试）
### 阶段二：准确率工程（MVP 验收后启动）
多候选 SQL + 选择器、实体索引、Python 分析沙箱、查询缓存。

### 阶段三：团队开源（阶段二稳定后启动）
Web UI + MCP Server、多用户最小集（只读强制、审计日志）、docker-compose、中英文文档。

## §5 不可妥协的契约（Invariants）

以下契约是跨模块依赖，**变更即破坏性变更**，必须人类批准并同步修改本文件与相关测试：

1. **目录结构**：`ingest/ catalog/ retrieval/ sqlgen/ guard/ exec/ eval/ tests/ docs/ data/`
2. **catalog.yaml**：`sources[] → {name, type, path, ingested_at, tables[] → {name, original_name, parquet, rows, columns[] → {name, original_name}}}`
3. **profile JSON**：`data/catalog/profiles/<table>.json`，字段见 T4 任务书（table/rows/columns[]/{name,dtype,null_rate,distinct_count,enum_values?,sample_values,min/max?,quantiles?,avg_len?}）
4. **命名规则**：`clean_name()` —— 仅 `[a-z0-9_]`、不以数字开头、≤63 字符、中文转拼音、冲突加 `_N`、原名双向映射入 catalog
5. **产物位置**：Parquet → `data/parquet/`；DuckDB → `data/warehouse.duckdb`；画像 → `data/catalog/profiles/`

## §6 对 Coding 智能体的工作规则（漂移防护）

1. 每次会话开始、每次上下文压缩恢复后：先读本文件，向人类**复述当前里程碑目标与验收标准**，确认后再动手
2. 只做当前里程碑范围内的任务；发现跨里程碑需求，记入 §7 待办池，不顺手实现
3. 每完成一个任务：更新其状态（⚪→🟡→🟢）；每完成一个里程碑：更新状态并写 §9 变更日志
4. **禁止擅自修改 §2 总体目标、§3 决策、§5 契约**；确需变更时停止相关工作，向人类说明理由并请示
5. 上下文压缩/会话重启后，不得凭记忆恢复需求，一律以本文件 + 代码现状为准
6. 每个任务完成 = 实现 + 测试 + ruff 通过 + 本文件状态更新，四者齐备才算完成

## §7 待办池（跨里程碑想法暂存）

- 【阶段二】2026-09-18 大文件加速：pandas ≥2.2 可选 `engine="calamine"`（python-calamine），50 万行级 xlsx 读取显著提速；里程碑 2 前评估。（源自 T2 调研）
- 【阶段二/环境依赖】2026-09-18 Access OLE/附件类型列的检测与跳过：无 mdbtools 真实环境下无法从 CSV 输出识别，列为已知限制；有环境后补。
- 【阶段二/环境依赖】2026-09-18 Access 真实环境集成补测（需 mdbtools + 真实 .mdb/.accdb）。
- 【阶段二】2026-09-19 runner 侧纵深检查（read_only 对 COPY 导出与 read_csv 读取不设防，当前完全依赖 guard 层）：评估 exec/runner 在执行前对 vsql 做表函数/COPY 复检，形成真双层防线（V3-B 红队观察项）。
- 【阶段二】2026-09-19 lancedb 0.38 的 table_names() DeprecationWarning（11 处）：升级 0.39+ 无 Windows wheel，待其恢复 Windows 支持后迁移 list_tables()。
- 【阶段三/开源准备】开源前补英文 README（T16：architecture.md 中文先行，翻译推迟至开源准备期）。
- 【阶段二】M4 提示词引导清单（G8b 风格类失败，不再动比对器）：①宽表输出引导（“分别/各”类双指标问题输出一行两列而非行式）；②NULL 分组如实输出（不把未关联 zone 的组改为数值标签）；③跨表列名纪律（green 用 lpep_*，模型曾把 tpep 用于 green 被护栏拒）。
- M4 任务：glossary schema 支持指标级口径：term 增加 metric_filter 与 applies_to 键；T6 校验器、卡片渲染、system.md 配套；#12 为验证用例，expected_sql 两侧均含 payment_type=1。✅ 已完成（2026-09-19 T14：schema/互斥校验/跨表注入/渲染区分/规则 6 落地，#12 真实链路两侧过滤且数值与 T13 参考值逐位一致）

## §8 变更规则

- §1/§2/§3/§5 的修改：仅人类本人，或人类明确授权后执行，且必须写 §9 变更日志
- §4 状态、§7 待办池：Coding 智能体按 §6 规则例行维护
- 每次修改都在 §9 追加一行，禁止改写历史日志

## §9 变更日志

- 2026-09-18 初版建立。锁定 LLM 接入方式为 OpenAI 兼容 API（决策 3），取代早期"Provider 抽象 + Ollama 可切换"方案。
- 2026-09-18 T1 项目骨架完成：uv + setuptools（含 nl2data CLI 包与顶层模块包）、ruff E/F/I/UP/ANN @ line-length 100 零告警、pytest 3 项冒烟通过、Apache-2.0、`nl2data --version` 可用。任务书验收命令 `python -m nl2data.cli --version` 通过，故 CLI 归入 `nl2data/` 包，业务模块按 §5.1 保持顶层包，两者并存。
- 2026-09-18 T2/T3/T4 实现完成并通过独立审查：T2 Excel（DuckDB excel 扩展优先 + pandas 回退、detect_header_row 表头探测、幂等重导先清理）；T3 Access（mdbtools 子进程 -1/-D/-T、runner 注入、MSys*/临时表过滤、pyarrow.csv、加密提示，集成测试 skipif 占位）；T4 画像（聚合 SQL 单次扫描、§5.3+T4 任务书契约字段、enum≤50/样本≤5、大表 10% 抽样、mtime 增量、单列 error 降级）。CLI：ingest excel/access、profile --table/--all。质量门：ruff 全仓库零告警；pytest 78 passed + 1 预期 skip（slow 单跑通过）；覆盖率 92.39%（最低模块 85%）；全新环境（rm -rf .venv data → uv sync）ingest excel → profile --all 跑通且数值与 DuckDB 直查一致。V1 独立总验收结论：达到完成判据（Access 真实链路因本机无 mdbtools 按豁免条款覆盖）。里程碑 1 置 🟢。
- 2026-09-18 环境备忘：本机 32 核高内存压力下 OpenBLAS 导入可能失败，nl2data/__init__.py 默认 OPENBLAS_NUM_THREADS=1（env 可覆盖）；记录 calamine 大文件加速与 OLE 列检测两项入 §7 待办池。
- 2026-09-18 里程碑 1 验收通过；新增决策 8（Embedding 仅走 OpenAI 兼容 API）；里程碑 2 启动。
- 2026-09-18 T5/T6 完成并通过独立审查：T5 表卡片（catalog/cards.py + table_notes.py，纯机械装配禁 LLM、enum 截断可配置、>100 列 Markdown 折叠、token_estimate 走 retrieval/tokens.py 供 T7 复用、mtime 增量重建、notes 缺失静默降级）；T6 术语词典（catalog/glossary.py，dataclass+手工校验、sqlglot duckdb 方言试解析、悬空引用 difflib 候选纠错、terms_by_table_map fail fast）。主智能体冻结并落地共享契约：glossary schema、卡片 JSON/Markdown 模板、terms_by_table 组装接口、配置扩展（paths.glossary/cards_dir/cards_md_dir/table_notes + cards/tokens 段）；新增依赖 sqlglot；CLI 增 cards build / glossary check|list。质量门：ruff 零告警；pytest 133 passed + 1 预期 skip；覆盖率 94%（最低模块 88%）。
- 2026-09-18 T-P 原生 Parquet 接入（人类批准扩展）：ingest/parquet.py register 模式（零拷贝建 VIEW、source.type=parquet、记录原始路径、幂等清理永不删除用户外部文件）；CLI 增 ingest parquet。6M 行/117MB 实测：ingest 0.72s、profile 1.19s（sampled:true 触发）、cards build 0.40s，进程峰值内存均 ~3MB；同时修复 T4 抽样画像两处缺陷并补专项测试（null_rate 改按抽样基准计算；min/max 剥离抽样改为精确计算，跨次运行稳定）。
- 2026-09-18 NYC 出租车真实数据注册（T-P 扩展执行）：yellow_tripdata_2026_03（3,952,451 行）/green_tripdata_2026_03（44,208 行）零拷贝注册并完成 profile+cards（注册 1.34s/0.73s，profile 2.83s，峰值内存 ~4MB）。偏差如实上报：两表均 <500 万未触发抽样分支（未改阈值）；taxi_zones 数据文件缺失未注册（G2 golden 录入维持停止，待文件）。实测记录见 docs/milestone-1-notes.md §8。
- 2026-09-18 人类三项决策执行 + G2 完成：①三表以恒等表名重注册（yellow_tripdata 3,952,451 行 / green_tripdata 44,208 行 / taxi_zones 265 行，文件以干净名置于 data/taxi/，幂等替换清理旧视图，孤儿产物清理）；②taxi_zones 取自 NYC TLC 官方 taxi_zone_lookup.csv（cloudfront 链接，四列核对一致，转 Parquet 注册）；③用例数量以 20 条为准（"24"系笔误），不做抽样演示、阈值零改动。G2：eval/recall_golden.yaml 录入 20 条（question/note 原样、恒等映射无替换、文件头注明来源/日期/映射说明），schema 校验器独立为 eval/golden.py 供 T7 复用；自检全过（解析/schema/无重复/表存在性），148 passed + 1 skip，ruff 零告警。详见 docs/milestone-1-notes.md §9。
- 2026-09-18 G3 术语词典录入（进行中，停止待人类决策）：data/catalog/glossary.yaml 新建写入人类确认的 9 条 NYC 术语（值逐字保留）；glossary check 报 1 处悬空列——术语「正常费率」maps_to.column 为原始列名 RatecodeID，而注册后安全列名为 ratecodeid（候选建议已给出）。按任务规则停止，未改词条、未跑 cards build，等人类决策。
- 2026-09-18 G3 完成（人类批准安全名决策）：glossary.yaml 录入 19 条全集（原 9 条修订——「正常费率」column: ratecodeid、filter: "ratecodeid = 1"——加区域中文名 6 条、编码列业务标签 4 条），check 19 条 0 错误；三张出租车表卡片 terms 非空（yellow 8 / green 4 / zones 7）；索引与卡片重建后重跑 golden：「术语增益版」Recall@3 0.842→0.933（+9.1pp），分节 二+12.5pp / 三+6.7pp / 四+12.5pp / 五+16.6pp，R@5 恒 1.000，缺口 7→3 条；剩余缺口归因更新（支付词条仅挂 yellow、“区”未命中“区域”子串、demo 干扰表）见 docs/milestone-2-notes.md §7。
- 2026-09-19 V2 里程碑 2 终验完成：「行政区」synonyms 增补「区」（零成本修复）；demo 干扰源清理（excel_basic/large_orders 全链移除，data/taxi 保留）；绿车支付词条经基线数据判定无需增补。三条基线落盘 docs/milestone-2-notes.md §8：无术语 R@3=0.842 → 术语增益 0.933（+9.1pp）→ M3 起点基线（3 表纯 NYC + 19 条术语）R@3=1.000 满分（R@5=1.000，MRR=0.975，五分节全满）。红线复查通过（索引仅卡片/术语、全仓无 LLM 调用、embedding 缓存重跑零网络 embedded=0）；质量门 ruff 零告警、202 passed+1 skip、覆盖 93%。里程碑 2 置 🟢，交接说明见 docs §10（含 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）。
- 2026-09-18 T7 混合检索完成（待审查）：retrieval/embedding.py（OpenAI 兼容 embedding，决策 8 环境变量、批≤32、退避重试、sha256 磁盘缓存）、retrieval/bm25.py（jieba 分词决策，弃 bigram）、retrieval/index.py（LanceDB 钉版 0.38——0.39 起无 Windows wheel；merge_insert 单事务、embedding 失败保留旧索引红线）、retrieval/retrieve.py（RRF k=60/术语直通/token 预算贪心/BM25-only 降级）、eval/recall.py（Recall@1/3/5+MRR+分节，复用 G2 校验器）；CLI 增 index build / retrieve --explain / eval recall。golden 20 条真实基线（bge-m3+jieba 混合）：R@1=0.492 R@3=0.842 R@5=1.000 MRR=0.967（BM25-only 对照 R@3=0.758）；7 条未满 R@3 逐条归因（干扰表噪声/taxi_zones 跨语言词面/编码列无业务标签）见 docs/milestone-2-notes.md。夹具评测 10 用例 R@3=100% 硬门槛达成；质量门 ruff 零告警、196 passed+1 skip、覆盖 92%（最低模块 85%）。T7 独立审查通过（功能/契约/红线/基线全过；修复 cli.py 覆盖不足与降级分支缺口后 202 passed、覆盖 93%、全部模块 ≥85%）。
- 2026-09-19 里程碑 2 终验通过（三基线：0.842 / 0.933 / 1.000）；里程碑 3 启动。
- 2026-09-19 T8/T10 完成并通过验收：T8 llm/chat.py（全仓唯一 LLM 调用模块，json_object 三层降级——调研确认 GLM/DeepSeek/Qwen/Kimi 四家均仅支持 json_object、json_schema 仅作客户端契约；429/5xx 退避、友好错误分类、审计事件+usage 累计、密钥零泄漏专项测试；20 测试覆盖 99%，真实 GLM 链路验证 parsed 正确）。T10 guard/validate.py（九条规则全走 sqlglot 解析树、CTE 作用域防逃逸、LIMIT 注入/封顶改写；55+64 测试覆盖 98%）。红队子智能体 64 对抗样本发现 2 个高危绕过家族（可写 CTE、SELECT..INTO 渲染成 CREATE TABLE）+2 误拒，已全部修复并 XPASS 固化；护栏改写 SQL 经真 warehouse 执行验证。全量门：333 passed+1 skip+1 xfail+7 xpassed，ruff 零告警，覆盖 94%。调研结论与红队细节见 docs/milestone-3-notes.md §2/§4。
- 2026-09-19 T9/T11 完成并通过验收：T9 sqlgen/generate.py（messages 三段组装、few-shot 注入、解析回退链、candidates_tried 由调用方累计；20 测试覆盖 98%；系统模板 sqlgen/prompts/system.md 主智能体起草、运行时读取、待人类审定）。T11 exec/runner.py（ValidatedSQL 签名防绕 + read_only 双重防线实测固化、守护线程超时+interrupt、fetchmany 截断、>50 行画像单趟聚合 SQL、明细 scratch parquet + detail_ref、错误六分类、cleanup_scratch 72h；23 测试覆盖 98%，100k 行压缩实测 176ms）。端到端真实冒烟通过：检索（双术语直通）→ GLM 生成（正确采用术语 filter 语义 JOIN taxi_zones WHERE borough='Manhattan'）→ 护栏（LIMIT 500）→ 沙箱执行（avg_fare=17.8230 与手工交叉验证一致）。全量门：376 passed+1 skip+1 xfail+7 xpassed，ruff 零告警，覆盖 94%。详见 docs/milestone-3-notes.md §5。
- 2026-09-19 T12 问答闭环完成：nl2data/qa.py（retrieve→generate→guard/run 回喂重试≤3→解读二次 LLM 可关可降级）+ nl2data/audit.py（jsonl 每问一条，失败不阻断）+ CLI ask 单发/交互循环（/retry、/show sql、/export csv、/tables）+ audit last；解读模板 sqlgen/prompts/interpret.md 起草（只陈述数据/引用数字/样本声明/禁外推）。修复 llm 包漏出 setuptools 清单。测试 7 用例（qa 88%/audit 91%）；真实冒烟：双表对比问 UNION ALL 正确+解读引数字，模糊问诚实反问。全量门：384 passed+1 skip+1 xfail+7 xpassed，ruff 零告警，覆盖 91%。20 问人工验收待 V3。详见 docs/milestone-3-notes.md §6。
- 2026-09-19 G5 完成：sqlgen/examples/ 注入两个 few-shot 示例（时间+区域关联、编码列+术语口径，语义一字不改）；system.md 补关键措辞级校验（与既有逐字快照双保险）；注入测试与 README 跳过断言通过；真实冒烟：示例 1 问题生成 SQL 与示例结构完全一致（JOIN+EXTRACT HOUR 半开区间），236,138 单/56ms/尝试 1 次。全量 387 passed 无回归，V3 二十问前置就绪。详见 docs/milestone-3-notes.md §7。
- 2026-09-19 V3 里程碑 3 终验（A 审查 + B 红队）完成：A 零阻断（T8-T12 逐条通过，五模块覆盖 99/98/97/98/88+91，红线五项全过：LLM 边界/guard 22 关键词实测零漏拦/read_only 双防线/key 零泄漏/质量门 442 passed 覆盖 93%），4 条建议全部处置（8 关键词固化、交互循环测试、陈旧注释、§7 待办）；B 零穿透（46+ 对抗含提示注入全链路/CTE 写/伪造 ValidatedSQL，47 固化测试，攻击后数据完整）。观察级弱点（COPY 导出/read_csv 读取依赖 guard 单层）记 §7。过程修复一处交互测试死循环（脚本注入器单例化）。20 问人工验收模板备于 docs §8，里程碑 3 状态待其结果。
- 2026-09-19 V3 二十问执行完成（agent 代理，逐条真实询问）：16/20 通过（80%），连续 20 问全过的完成判据未达成。安全底线全 20 问成立（SQL 可核验/解读无编造/零破坏性——表行数前后快照一致，23 事件 108,455 tokens，1 问重试回路生效，1 次网络故障退避重试成功，1 次进程崩溃重跑）。失败 4 问两类归因：模式 A×3（#4/#15/#17 未限定车型时共享字段应双表 UNION，模型单表——检索无责，提示词未覆盖隐含双表）；模式 B×1（#12 小费率未过滤 payment_type=1，glossary description 已载但未采用）。修复建议（补 few-shot 双表示例+system.md 一句规则+词条 filter 升级，均需人类审定）与逐问记录见 docs/milestone-3-notes.md §8。里程碑 3 维持 🟡 待修复重跑判定。
- 2026-09-19 G6 三项审定修复落地（system.md 第 5 条硬约束逐字、few-shot 示例 3（共享字段 UNION ALL，few_shot_count→3）、小费率词条 filter 升级；check/cards/index 重建，442 passed 无回归）；复跑失败 4 问：#4/#15/#17 通过（模式 A 全修复），#12 仍失败（SQL 与修复前逐位相同，payment_type=1 未采用——双表分别场景下模型视词条 filter 为单侧口径而整体放弃，深层归因留 M4）。二十问终局 19/20（95%）；按 G6 指令停止提示词迭代，里程碑 3 维持 🟡 待人类对 #12 处置决策（接受 95% 判定或留待 M4）。详见 docs/milestone-3-notes.md §8 G6。
- 2026-09-19 里程碑 3 收官（人类裁定 19/20 通过为完成）：安全底线（零破坏性/SQL 可核验/解读无编造/诚实反问）与可核验性 20/20 全过；唯一偏差 #12（小费率 payment_type=1 口径）按 §4 偏差注记转 M4（指标级 metric_filter 方案已列 §7）。T8-T12 全 🟢；G6 修复后模式 A（隐含双表）3/3 修复。终版封存 docs/milestone-3-notes.md。
- 2026-09-19 里程碑 3 收官（19/20 裁定，§4 偏差注记在案）；里程碑 4 启动。- 2026-09-19 T13 完成：附录 B 人类起草 20 条 expected_sql 录入（guard 白名单通道列校验 20/20 零拒绝，green 时间列 lpep_* 核对无误）；warehouse 只读生成 expected_result 全部落盘（关键值与二十问交叉一致；#3/#16 NULL 属数据事实照实记录；#12 过滤口径 0.2515/0.2785 为 T14 metric_filter 验证依据）；golden.py 校验器扩展（expected_sql/result/tolerance 三键，缺键向后兼容，11+3 测试）；三层校验全过；全量 445 passed。#19 口径按草案 borough 分区录入、待人类最终拍板（备选 vehicle 分区）。详见 docs/milestone-4-notes.md。
- 2026-09-19 #19 占比口径人类拍板：维持行政区内两车占比（PARTITION BY z.borough），参考值不重生成；golden 三元组冻结，T15 runner 输入就绪。
- 2026-09-19 T14 指标级口径 schema 修复完成：glossary 词条级新键 metric_filter（sqlglot 校验）与 applies_to（表存在性校验，difflib 候选），与表级 filter 互斥（5 个新测试固化含互斥/成对/跨表分组/悬空/坏 SQL）；小费率词条迁移为指标口径并注入黄绿双卡片（terms 8/5）；卡片 Markdown 渲染区分「指标口径(全局适用,跨表生效)」与「表级口径(仅本表)」；system.md 第 6 条逐字落地+快照双短语；check/cards/index 重建，450 passed 无回归。#12 真实验证：生成 SQL 两侧均含 payment_type=1，结果 0.251485…/0.278538… 与 T13 参考值逐位一致，一次尝试通过。§7 对应待办销项。
- 2026-09-19 T15 三层判定 runner 完成：eval/e2e.py（L1 召回/L2 全链执行/L3 语义等价——行多重集合+列序无关+ORDER BY 行序敏感+容差+车辆标签 glossary 同义归一）+ CLI eval e2e + 基线 diff + 13 测试；--no-interpret 强制、单 case 异常不中断、审计复用。敏感度 A/B 实证：回退小费率词条存 A 基线（含 #12 失败）→ 恢复 T14 重跑，diff 7 条变化+3 指标变化，#12 fail→pass 闭环。正式基线 eval/baseline_e2e.json（git 跟踪）：L1=1.000 / L2=1.000 / L3=0.55（剩余 5 条真实语义差为 M4 迭代靶点，见 docs §4）。全量 463 passed 无回归。
- 2026-09-19 G8 完成：A1 查证（#3/#16 双 NULL 比较器本就正确，证据在案）；A2 行政区收缩+新建区域词条（zone 级，#14 行数对齐）；B3 有效订单指标口径词条（#18 修复）；B4 few-shot 示例 4 top-1 模式（#20 修复，#11 轻度外溢摆动）；B5 eval 段 temperature=0（19/20 复现，#19 摆动）；修复 runner 样本截断伪影（L3 从 detail_ref 读全量，补测试）。新基线 L1=1.000/L2=0.95/L3=0.65（未达 0.90 预期）：剩余真语义差四类（#4 单位口径、#7/#8/#19 多列形状、#10 行式、#14 细节）+摆动，三个决策点已列 docs §5 待人类。baseline_e2e.json 因语义资产+伪影修复重新生成（非失守）。全量 465 passed。
- 2026-09-19 G8b 完成：①比对器列超集投影+单位等价（带 unit_equivalent 标注），“缺列 fail”修正为缺列回退形状比对（字面执行致基线崩至 0.40，别名自由是合法变体）；②#11 口径注记逐字入 golden；③eval runs_per_case=3 多数决（confidence 标注，成本 25-30 万 tokens/轮）；④#14 残差定性为 NULL zone 组呈现差（非空串混排，不归一化）。最终基线 L1=1.000/L2=0.95/L3=0.75（15/20，未达 0.90 预期）：#7/#10/#14/#19 风格类已列 §7 提示词引导清单，#11 本轮列名跨表混用被护栏正确拒（模型语义错，历史轮次曾过）。全量 474 passed。baseline_e2e.json 因 G8b 重生成。
- 2026-09-19 G8c 完成：table_notes 三表说明（人类审定逐字，yellow/green 时间列前缀纪律 + zones 维度口径）注入卡片；#11 L2 修复生效（时间列混淆导致的护栏拒绝消除）、L3 仍 fail（top-1 单行 vs golden 双行口径，按任务书不再迭代防过拟合）。最终基线定稿（N=3）：L1=1.000/L2=0.95/L3=0.75，剩余 5 条全为风格/口径类（#7 top-1 外溢、#10/#11 形状、#14 NULL 组、#19 百分比舍入超容差），M4 判定基线冻结。全量 474 passed 无回归。baseline_e2e.json 重生成。
- 2026-09-19 T16 完成：docs/architecture.md（开源 README 母体，七节骨架：简介/架构图/八决策+契约引用/快速开始五个环境变量+全流程命令/三层评测/安全模型双重防线+红队制度/开发指南；中文先行，英文 README 入 §7 待办；goals.md 内部事实源 vs architecture.md 对外叙事的引用关系文内写明）。文中全部命令在全新临时工作区逐条自验通过（ingest excel+parquet→profile→cards→index→glossary check→ask 正确回答→audit；eval 在临时库正确拒绝缺表，主库有效性有 T15/G8 证据）。待人类通读。
- 2026-09-19 V4-A 建议处置：覆盖率门禁 80→85（决策 7 措辞与 pyproject fail_under 同步上调；实测 91.24% 有余量，人类授权顾问裁定）。
- 2026-09-19 V4 收官：A1 三条处置（门禁 80→85 三处同步；M4 文件统一 commit「M4 收官」；章节引用核实为文档链无误）；B 受控实验（改动轮 L3 0.75→0.65 单句措辞即被感知，恢复轮 0.80 与基线差恰 1 case 且为已知摆动项）——完成判据「改提示词后一键回归，指标可对比」实证达成；C 质量门全过（85 新门禁）、里程碑 4 🟢、§7 逐项标注归属、docs/milestone-4-notes.md 封存。阶段一个人 MVP 四大里程碑全部完成。
- 2026-09-19 M5 立项（MCP + Skill 封装）：人类希望 AI 宿主可显式调用 nl2data，调用中由宿主 LLM 向用户索要缺失配置；人类无偏好，顾问裁定批准立项。§3 新增锁定决策 9（仅经 MCP tools + SKILL.md，tools 面最小化 status/list_tables/ask，密钥只走环境变量且不得出现在任何 tool 参数与返回值）。
- 2026-09-19 T17 MCP Server 完成：新模块 mcp_server/（决策 9 最小三工具 status/list_tables/ask，对既有模块只 import；ask 复用 ask_once(no_interpret=True)，解读留给宿主 LLM）；依赖 mcp 2.2.0（FastMCP 已更名 MCPServer，同步工具经 anyio.to_thread 卸载不阻塞事件循环）；CLI 增 `nl2data mcp serve`（stdio）；密钥纪律三处落地（status 只报 presence 布尔与缺失名、ask 异常经 _scrub 六变量值清洗、LlmError 原生脱敏），专用测试断言哨兵值零出现；注入防护按任务书不加新防线——单元级敌意模型（DELETE SQL）被既有护栏三拒、表行数不变，真实注入（"忽略之前的规则"）模型诚实反问只读约束。真实链路验证：stdio 子进程全流程（status ready=true / list_tables 三表行数 / golden #1 答案 3,952,432 与参考值一致）+ slow 测试（内存客户端 golden #1）。质量门：ruff 零告警、488 passed 覆盖 91.41%。mcpServers 配置样例见交付报告；SKILL.md 待后续任务书。
- 2026-09-19 T18 Skill 包完成：skills/nl2data/ 三件套（SKILL.md 主文件 97 行 + config-template.sh 六变量空占位模板（变量名与 §3 决策 3/8 一致，硅基流动公开示例注释，零密钥零内部地址）+ README.md 宿主注册指南（mcpServers JSON 片段即 T17 交付样例落地版，Claude Code/Cursor/zcode 注册位置，env 块显式传六变量的说明））。SKILL.md 按 progressive disclosure：第三人称 frontmatter 触发场景、快速判断（禁绕过口径层直连 DuckDB）、四步工作流（status 诊断含缺配置固定话术与 CLI 三步命令 → list_tables → ask（needs_clarification 原样转达、新问句重发）→ 呈现附 SQL 可核验提醒）、四条纪律逐条成文、故障速查表五行。验收标准机器化为 tests/test_skill_docs.py 8 项（目录齐备/<150 行/工具引用恰为 T17 三件无悬空/四纪律关键词/工作流四步与速查三行/引用文件存在/六变量一致/全包无密钥字面量）。质量门：ruff 零告警、496 passed 覆盖 91.41%。SKILL.md 全文已随交付报告呈人类过目。
- 2026-09-20 生成条件变更（V5-C，人类裁定通过）：联调发现 GLM-5.3-Flash 默认深度思考致 SQL 生成超 50s（宿主 30s 超时必现）；新增 llm.thinking 开关（默认 enabled 向后兼容、未知值 fail fast，仅显式 disabled 时随请求发送 thinking={"type":"disabled"}/GLM 扩展字段，严格 OpenAI 兼容端不受影响），仓库 config.yaml 置 disabled。验证：单次 ask 2m31s → 12.8s，双口径答案一致，497 passed 覆盖 91.35%、ruff 零告警。commit 4834f5d。
