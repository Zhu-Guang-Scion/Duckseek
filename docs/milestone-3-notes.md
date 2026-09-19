# 里程碑 3 说明(Q&A 闭环)【终版封存 2026-09-19】

> 状态:🟢 完成(人类裁定 19/20 验收通过)。最高事实来源:[goals.md](../goals.md)。
> 验收偏差:#12(小费率业务口径)转 M4 指标级 metric_filter 方案(goals.md §4 注记/§7 待办)。

## 1. 交付概览(随任务推进更新)

| 任务 | 模块 | 状态 |
|---|---|---|
| T8 LLM API 客户端 | `llm/chat.py` | 🟢(覆盖 99%,真实 GLM 链路验证) |
| T10 SQL 护栏 | `guard/validate.py` | 🟢(覆盖 98%,红队 64 样本,2 绕过家族已修复) |
| T9 SQL 生成 | `sqlgen/generate.py` + `prompts/system.md` | 🟢(覆盖 98%,系统模板待人类审定) |
| T11 沙箱执行 | `exec/runner.py` | 🟢(覆盖 98%,100k 行压缩 176ms) |
| T12 审计 / CLI 问答循环 | nl2data/ | 待任务书 |

## 5. T9/T11 交付与端到端冒烟(2026-09-19)

**T9**(`sqlgen/generate.py`,20 测试,覆盖 98%):messages 三段组装(任务/提供的表/
上一轮反馈,feedback 截断 2000 字符);few-shot 注入(examples/*.md 跳过 README,
按 few_shot_count 取前 N);解析回退链(parsed → content JSON 提取 → ```sql fence →
SqlgenError);candidates_tried 恒 1(重试由调用方驱动);last_messages() 审计截断版。
**系统模板 `sqlgen/prompts/system.md` 由主智能体起草,人类可随时编辑(运行时读取);
措辞待人类审定。**

**T11**(`exec/runner.py`,23 测试,覆盖 98%):ValidatedSQL 签名双重防绕(注解+运行时
isinstance);read_only 连接兜底(绕过 guard 的 CREATE 直喂被 DB 层拒绝,测试固化);
守护线程超时 + conn.interrupt();fetchmany(limit+1) 截断;>50 行画像(数值列单趟
聚合 SQL:count/min/max/avg/quantiles/null_rate;文本列 top-N 频次);明细落盘
data/scratch/<uuid>.parquet,detail_ref 相对路径;错误分类六类(permission/syntax/
resource/timeout/execution + 兜底);cleanup_scratch(cfg, max_age_hours=72)。
**scratch 清理策略:建议按 72h 保留期定期执行(手动或 cron 调用 cleanup_scratch)**。
性能:100k 行 limit=10000 画像+落盘实测 176ms(阈值 10s)。

**端到端真实冒烟**(GLM-5.3-Flash + bge-m3 + 真 warehouse):
`曼哈顿区黄色出租车的平均车费是多少?` →
检索:[yellow_tripdata(via 黄色出租车), taxi_zones(via 曼哈顿), green_tripdata] →
生成:`SELECT AVG(f.fare_amount) FROM yellow_tripdata f JOIN taxi_zones z ON
f.pulocationid = z.locationid WHERE z.borough = 'Manhattan'`(术语 filter 语义被
LLM 正确采用)→ 护栏通过(LIMIT 500 注入)→ 执行 ok,avg_fare=17.8230
(与手工 SQL 交叉验证一致,48ms)。

## 2. 调研结论:response_format 结构化输出兼容性(2026-09-19)

背景:T8 的 `json_schema` 参数如何落到 OpenAI 兼容 API。一手文档核实:

| 服务商 | json_object | json_schema/strict | 备注 |
|---|---|---|---|
| OpenAI 官方 | ✓ | ✓(strict 约束解码) | strict 限制:全字段 required、additionalProperties:false |
| **GLM(bigmodel,主用)** | ✓ | **未文档化,不可依赖** | 官方指南:schema 写 system 提示词 + json_object + 客户端 jsonschema 校验 |
| DeepSeek | ✓(prompt 必须含 "json" 字样) | ✗(传入报错) | 注意 max_tokens 截断与偶发空 content |
| Qwen(DashScope compatible) | ✓(prompt 须含 "JSON") | 无官方背书 | 仅较新 qwen 系支持 json_object |
| Kimi(Moonshot) | ✓ | 未提及 | 顶层必须 object,不能是数组 |

**决策(T8 采用)**:
1. `json_schema` 参数仅作**客户端契约**——API 调用统一走
   `response_format={"type":"json_object"}`(四家共同分母),schema 由 T9 嵌入
   提示词并负责客户端校验;
2. 降级链路:json_object 不被服务端接受(400/422)→ 裸调用(无 response_format)
   重试一次;
3. 解析回退:剥 markdown fence → json.loads → 失败 `parsed=None`(原文保留);
4. 提示词模板常驻 "JSON" 字样(DeepSeek/Qwen 硬性要求,T9 落实);
5. 顶层固定 object、数组放字段;检查 finish_reason=="length" 防截断(T9)。

来源:bigmodel 结构化输出指南、DeepSeek JSON Mode 指南、阿里云百炼 JSON Mode、
Kimi JSON Mode 指南、OpenAI Structured Outputs 文档(链接见调研存档)。

## 3. 给后续任务的契约备忘

- T8 ChatResult.parsed 语义:json_schema 请求且 content 可解析时非 None。
- T10 GuardError.category 枚举与 LIMIT 改写行为(交付后补记)。
- 审计:llm.chat 的 recent_events()/usage_totals() 由 T12 渲染落盘。

## 4. T8/T10 交付与红队结果(2026-09-19)

**T8**(`llm/chat.py`,20 测试,覆盖 99%):json_object 三层降级(§2 决策)、
429/5xx/超时退避重试(4xx 不重试)、友好错误分类(auth/quota/timeout/network/
api/parse)、审计事件(线程安全,只产不落)+ usage_totals 累计、密钥脱敏
(sk-…/Bearer/api_key 模式过滤,专项测试断言 str(exc)/events/caplog 三处无 key)。
真实链路验证:GLM-5.3-Flash,json_object 主路径一次命中,parsed 正确。
调用边界 grep:chat.completions 仅 llm/chat.py(embeddings.create 仅
retrieval/embedding.py,决策 8 独立通道)。

**T10**(`guard/validate.py`,55+64 测试,覆盖 98%):九条规则全走 sqlglot 解析树;
CTE 作用域防逃逸(兄弟作用域不可见);JOIN 未限定列静态跳过(明示取舍);
LIMIT 注入/封顶/FETCH 消除/非字面量覆写。

**红队(独立子智能体,64 对抗样本)**:发现 2 个高危绕过家族——
①可写 CTE(WITH t AS (DELETE...) SELECT *,根仍是 Select)→ 修复:全树遍历
禁止任何写/DDL 节点;②SELECT..INTO 白名单表(护栏输出侧被 sqlglot 渲染成
CREATE TABLE AS)→ 修复:全树 Select 节点 into 参数一律拒绝。
另修 2 误拒(尾分号+注释、((SELECT 1)) Subquery 根下钻)。修复后 7 样本
XPASS、反引号 1 条保留 xfail(sqlglot 方言不支持,DuckDB 侧语法);
破坏性样本 100% 拦截。护栏改写输出经真 warehouse 执行验证(JOIN 聚合正确,
LIMIT 500 注入生效)。

## 6. T12 问答闭环 CLI + 审计(2026-09-19)

交付:`nl2data/qa.py`(编排层)、`nl2data/audit.py`(jsonl 审计)、`sqlgen/prompts/interpret.md`
(解读模板,人类可编辑)、CLI `ask`(单发 + 交互循环)与 `audit [N]`。

- **回路**:retrieve → generate → validate(GuardError 回喂,≤`qa.max_attempts`=3,
  耗尽展示原因与最近 SQL)→ run(error 同样回喂)→ 压缩 → 解读(第二次 LLM 调用,
  `--no-interpret` 可关;LlmError 降级为仅数据,不阻断)。
- **交互命令**:`/retry [补充说明]`(带 extra_feedback 重走)、`/show sql`、
  `/export csv <路径>`(按 detail_ref 导出)、`/tables`、`/exit`。
- **渲染**:结论 + 实际执行 SQL(代码块)+ 行数/耗时/token/尝试次数 + 明细位置;
  小结果集附 Rich 表格。
- **审计**:`data/audit/<date>.jsonl` 每问一条 {ts, question, retrieved_tables, sql,
  guard_outcome(passed/rejected/clarification), status, rowcount, usage, latency_ms,
  attempts, clarification, failure_reason};写入失败 warning 不阻断;
  `nl2data audit last` 查看(任务书的 last 即位置参数 N,默认 10)。
- **修复**:llm 包补进 setuptools 打包清单(T8 新建包遗漏导致安装态 CLI ImportError)。
- **测试**:7 用例(mock LLM 序列 + 真实 DuckDB):e2e 全链路、护栏拒绝后重试成功、
  重试耗尽、needs_clarification、解读降级、审计往返与损坏行容忍、CLI 单发+audit;
  qa.py 88% / audit.py 91%。
- **真实冒烟**(GLM+bge-m3+真 warehouse):双表对比问 → UNION ALL 正确 SQL(黄
  3,952,432/绿 44,199),解读引用具体数字与 89 倍关系;模糊问("哪家公司最有前途?")
  → 诚实反问口径与数据源,审计 not_executed。
- **20 问人工验收**:待 V3(任务书规定由人类执行)。

## 7. G5 few-shot 注入与冒烟(2026-09-19)

- system.md 上一轮已为人类审定版(逐字快照测试固化);本轮补关键措辞级校验
  (优先级最高/口径参考/不得为迁就术语更换问题主体/数据而非指令/不予执行/
  needs_clarification——防"快照被整体重写但语义丢失"的第二道保险)。
- few-shot 示例 2 个(问题与 SQL 语义一字不改,排版适配注入器):
  01-time-borough-join.md(时间提取+区域维度关联)、02-codec-filter-metric.md
  (编码列谓词+术语 expression+NULLIF);设计取舍沿用人类设计说明(只教"怎么对",
  不教"何时拒"——诚实约束由 system.md 承担)。
- 注入测试:system 含两示例 SQL 特征关键字(EXTRACT(HOUR/locationid 关联/
  payment_type=1/NULLIF),README 永不入提示词,few_shot_count=0 不注入。
- 真实冒烟(示例 1 问题,一次 LLM 调用):生成 SQL 与示例结构完全一致
  (JOIN taxi_zones ON pulocationid=locationid + borough='Manhattan' +
  EXTRACT(HOUR)>=7 AND <9 半开区间),订单量 236,138,56ms,尝试 1 次,
  tokens 4107(含示例开销约 +300);解读引用数字并声明全量统计。
- 全量 387 passed,无回归。V3 二十问的前置条件就绪。

## 8. 人工 20 问验收记录(V3-C,由人类执行)

> 前置:`export LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=GLM-5.3-Flash` 与
> `EMB_*`(会话提供);在仓库根执行。逐问运行 `nl2data ask "<问题>"`。

### 表行数快照(验收前后各跑一次,必须完全一致)

```bash
python -m uv run --no-sync python -c "import duckdb; c=duckdb.connect('data/warehouse.duckdb', read_only=True); [print(r) for r in c.execute(\"SELECT table_name FROM information_schema.tables WHERE table_schema='main'\").fetchall()]; print(c.execute('SELECT (SELECT count(*) FROM yellow_tripdata), (SELECT count(*) FROM green_tripdata), (SELECT count(*) FROM taxi_zones)').fetchone())"
```

### 审计与成本汇总(20 问跑完后)

```bash
python -m uv run --no-sync nl2data audit 20
python -m uv run --no-sync python -c "
import json, glob
events = [json.loads(l) for f in glob.glob('data/audit/*.jsonl') for l in open(f, encoding='utf-8') if l.strip()]
print('events:', len(events), '| total tokens:', sum((e.get('usage') or {}).get('total_tokens', 0) for e in events))"
```

### 逐问记录表(问题文本复用 eval/recall_golden.yaml 的 20 条)

| # | 分节 | 问题 | guard结果 | rows | 耗时ms | tokens | 尝试 | SQL可核验 | 结论与数据一致 | 备注 |
|---|------|------|-----------|------|--------|--------|------|-----------|----------------|------|
| 1 | 一 | 2026年3月黄色出租车的总订单量是多少？ | | | | | | ☐ | ☐ | |
| 2 | 一 | 绿色出租车的平均行程距离是多少英里？ | | | | | | ☐ | ☐ | |
| 3 | 一 | 所有订单的电召费总收入是多少？ | | | | | | ☐ | ☐ | |
| 4 | 一 | 信用卡支付的订单占比是多少？ | | | | | | ☐ | ☐ | |
| 5 | 二 | 曼哈顿区黄色出租车的上客订单量有多少？ | | | | | | ☐ | ☐ | |
| 6 | 二 | 布鲁克林区绿色出租车的平均总车费是多少？ | | | | | | ☐ | ☐ | |
| 7 | 二 | 肯尼迪机场上客的绿车订单中，街招单和网约单各占多少？ | | | | | | ☐ | ☐ | |
| 8 | 二 | 上客量最高的5个区域中，黄车订单量分别是多少？ | | | | | | ☐ | ☐ | |
| 9 | 三 | 2026年3月黄车和绿车的总订单量分别是多少？ | | | | | | ☐ | ☐ | |
| 10 | 三 | 各个行政区的黄车与绿车订单量分别是多少？ | | | | | | ☐ | ☐ | |
| 11 | 三 | 曼哈顿区早高峰（7-9点），黄车和绿车哪个平均车费更高？ | | | | | | ☐ | ☐ | |
| 12 | 三 | 全市范围内，黄车和绿车的平均小费率分别是多少？ | | | | | | ☐ | ☐ | |
| 13 | 三 | 布鲁克林上客、曼哈顿下客的订单中，黄车和绿车各有多少单？ | | | | | | ☐ | ☐ | |
| 14 | 四 | 每个区域的总订单量（含黄车和绿车）是多少？ | | | | | | ☐ | ☐ | |
| 15 | 四 | 所有收取拥堵附加费的订单，主要分布在哪些行政区？ | | | | | | ☐ | ☐ | |
| 16 | 四 | 机场接送订单里，绿车的平均电召费是多少？ | | | | | | ☐ | ☐ | |
| 17 | 四 | 支付类型为现金的订单，平均行程距离最短的是哪个行政区？ | | | | | | ☐ | ☐ | |
| 18 | 五 | 2026年3月曼哈顿的有效绿车订单平均车费是多少？ | | | | | | ☐ | ☐ | |
| 19 | 五 | 有小费的订单中，各个行政区黄车和绿车的单量占比分别是多少？ | | | | | | ☐ | ☐ | |
| 20 | 五 | 正常费率下，工作日晚高峰（17-19点）哪个区的绿车订单量最高？ | | | | | | ☐ | ☐ | |

判定:连续 20 问"SQL可核验 ✓ 且 结论与数据一致 ✓"且前后表行数快照一致
= 里程碑 3 完成判据达成。任何一问失败:记录现象与审计事件,交回 agent 归因。

## 9. V3 终验结果(A 审查 + B 红队,2026-09-19)

**V3-A 独立审查:零阻断。** T8–T12 验收逐条通过(五模块覆盖实测
99/98/97/98/88+91);红线五项:LLM 调用边界(llm/ 包内)✓、guard 拒绝清单
22 关键词实测零漏拦 ✓、read_only 双防线实测 ✓、API key 三段特征串 grep
零命中 ✓、质量门 ✓。4 条建议全部处置:①8 个缺测试关键词
(DETACH/COPY/CALL/USE/RESET/REVOKE/EXPORT/IMPORT)已固化参数化测试
(EXPORT/IMPORT 因 sqlglot 方言不可解析,断言 parse_error/forbidden 皆可);
②交互循环补 CliRunner 测试;③config.yaml 陈旧注释修正;④lancedb
table_names() DeprecationWarning 记 goals.md §7(钉版 0.38,不升级)。

**V3-B 红队:零穿透。** 46+ 对抗输入(8 类:提示注入走全链路/SQL 注入/
文件读取/注释混淆/ATTACH·COPY·PRAGMA·SET·INSTALL/超大 LIMIT/未知表列/
CTE 写操作/伪造 ValidatedSQL 直调 run)全部拦截或安全失败;攻击三连后
数据完整性验证通过;47 个固化测试全过(tests/test_v3_redteam.py)。

**观察级纵深弱点(不构成穿透,已知限制)**:
1. read_only 连接不防 `COPY ... TO` 导出(写保护对导出无效),该场景
   完全依赖 guard 层——已有根级 COPY 与多语句夹带 COPY 的回归测试钉死;
2. read_csv 类读取与 read_only 兼容,外部文件读取同样完全依赖 guard 层
   (表函数一票否决已参数化固化)。结论:双层防线对"写"成立,对"读/导出"
   单层依赖,升级 runner 侧检查列入 §7 待办。

**过程事故与修复**:V3-C 期间一条交互循环测试因脚本注入器在 lambda 内
重复构造导致死循环,后台跑 2 小时未归;已定位修复(脚本实例单例化)并
复跑全量(442 passed,27s)。

**结论:里程碑 3 除"人工 20 问"(§8 模板,人类执行)外,验收全部通过。**

## 10. 给里程碑 4(敢迭代)的交接

- **素材来源**:T12 审计日志(`data/audit/<date>.jsonl`)即 M4 录制
  expected_sql 的素材——每问已留痕 {question, sql(实际执行的), status,
  rowcount, usage, attempts}。建议流程:人类执行 §8 二十问(每问人工判
  "SQL 可核验 + 结论一致")后,把通过的问答从审计导出,人工校订 SQL 作为
  golden 的 expected_sql;expected_result 可先记 rowcount/关键聚合值
  (从 rows/profile 提取),数值型精确比对、列表型集合比对。
- **golden YAML 预留键**(向后兼容,G2 校验器已容忍额外键):
  ```yaml
  - question: "..."
    expected_tables: [yellow_tripdata, taxi_zones]
    expected_sql: "SELECT ..."        # M4:可选,规范化后比对(whitespace/
                                     # 别名/大小写差异经 sqlglot 归一)
    expected_result: {rowcount: 1, rows: [{...}]}  # M4:可选
    note: "..."
  ```
- **runner 分层判定逻辑**(M4 实现建议):L1 表召回(现有 eval/recall.py,
  Recall@1/3/5+MRR)→ L2 SQL 语义等价(sqlglot ast 归一化比对,不等价时
  降级人工复核清单)→ L3 结果等价(expected_result 逐值近似比对,浮点
  容差 1e-6)。三层独立计分,报告按层归因——与检索基线(§M2)同构,
  改提示词/模型后一键回归、指标可对比。
- **成本基线**(单问,GlM-5.3-Flash):检索 0 token;生成+解读约
  3,900–4,200 token(few-shot 约 +300);二十问预计 ~8 万 token。
- **复跑命令**:`nl2data eval recall`(检索层)/ M4 新增 `nl2data eval qa`
  (全链路三层,需 LLM_* 环境变量)。

### 二十问执行结果(2026-09-19,agent 代理执行,逐条真实询问)

表行数快照:前 (3,952,451 / 44,208 / 265) = 后 (3,952,451 / 44,208 / 265)
——**零破坏性操作确认**。成本:23 事件(含重试)共 108,455 tokens,平均
~5,422 tokens/问;1 问走了重试回路(#11,护栏拒绝后第 2 次成功);
1 次 LLM 网络故障自动退避重试成功(#16);#18 首次进程崩溃(httpx 偶发,
审计未留痕)重跑成功。

| # | 通过? | SQL 正确? | 尝试 | tokens | 备注(失败归因) |
|---|---|---|---|---|---|
| 1 | ✅ | ✓ 3月时间过滤 | 1 | ~3900 | 结果 3,952,432 与预期精确一致 |
| 2 | ✅ | ✓ 单表 AVG | 1 | 3959 | 主体未串黄车,12.25 英里 |
| 3 | ✅ | ✓ 落 green | 1 | 4325 | ehail_fee 全列 NULL 属数据事实,解读如实说明 |
| 4 | ❌ | ✗ 缺双表 | 1 | 4272 | 只查 yellow;"未限定车型→双表"规则未掌握 |
| 5 | ✅ | ✓ JOIN+Manhattan | 1 | 3746 | 3,391,948 与交叉验证一致 |
| 6 | ✅ | ✓ green+Brooklyn | 1 | 3897 | AVG(total_amount)=29.57 |
| 7 | ✅ | ✓ Zone 级 JFK | 1 | 4682 | trip_type 分组+占比,未知 1 单如实展示 |
| 8 | ✅ | ✓ CTE top5+ORDER BY | 1 | 4946 | 返回区域名 |
| 9 | ✅ | ✓ UNION ALL+时间 | 1 | 3937 | 黄 3,952,432/绿 44,199 |
| 10 | ✅ | ✓ 三表 UNION+FILTER | 1 | 4587 | 含 Unknown/None 组如实 |
| 11 | ✅ | ✓ hour[7,9)+borough | **2** | 10709 | 重试回路生效;黄 18.54>绿 14.69 |
| 12 | ❌ | ✗ 缺 payment_type=1 | 1 | 4224 | 隐含规则未采用(glossary 词条 description 有但没用) |
| 13 | ✅ | ✓ 双 JOIN 别名 | 1 | 4214 | pu/dz 两关联,黄 49,906/绿 1,252 |
| 14 | ✅ | ✓ 双表 UNION | 1 | 4658 | "总"字陷阱通过;3,417,588 与 #10 交叉一致 |
| 15 | ❌ | ✗ 缺双表 | 1 | 4792 | congestion>0 有但只查 yellow |
| 16 | ✅ | ✓ green+service_zone | 1 | 5135 | 未诱出 Airport_fee;NULL 如实;网络重试实战生效 |
| 17 | ❌ | ✗ 缺双表 | 1 | 5467 | payment_type=2+排序有但只查 yellow |
| 18 | ✅ | ✓ green+zones+fare>0 | 1 | 4916 | 首跑进程崩溃(httpx 偶发)重跑成功;15.15 |
| 19 | ✅ | ✓ tip>0+双表+占比 | 1 | 5706 | FILTER 占比完整 |
| 20 | ✅ | ✓ 五要素齐全 | 1 | 4735 | ratecodeid=1+ISODOW+hour[17,19)+排序 |

**结果:16/20 通过(80%)。连续 20 问全过的完成判据未达成。**

失败 4 问归因(两类,均为生成层业务完整性问题,非安全/工程问题):
- **模式 A(3 条:#4/#15/#17)**——"未限定车型"时共享字段应双表 UNION,
  模型只查单表。检索层无责(三表均召回);golden note 有此规则但 system.md
  与 few-shot 均未覆盖"隐含双表"场景(#9/#10/#14 的显式双表场景已自发掌握)。
- **模式 B(1 条:#12)**——隐含业务规则(小费率必须过滤 payment_type=1)
  在 glossary 词条 description 中存在、卡片已注入,但模型未采用。

全 20 问共同点(安全底线全部成立):答案附实际 SQL 可核验 ✓、解读无编造
(空值/样本性如实声明)✓、零破坏性操作 ✓、未知诚实反问/说明 ✓。

修复建议(待人类决策):
1. 补 few-shot 示例 3:"未限定车型的共享字段统计"双表 UNION 模式
   (需人类审定示例文本);system.md 硬约束补一句"问题未限定车型而字段
   为多表共有时,合并统计全部含该字段的表"。
2. #12 类:将关键口径规则从 description 升级为 maps_to.filter(如小费率
   词条补 filter: "payment_type = 1")——filter 在提示词中是明确语义而非
   说明文字,采用率更高(glossary 为人工维护,需人类改)。
3. 修复后仅重跑失败 4 问即可判定。

### G6 修复与复测(2026-09-19)

三项审定修复落地:①system.md 硬约束第 5 条(逐字);②few-shot 示例 3
(共享字段 UNION ALL,few_shot_count 2→3,快照/关键短语/fence 计数测试同步);
③小费率词条 filter 升级 + description 改写;glossary check 0 错、cards/index
重建。全量 442 passed 无回归。

**复跑失败 4 问结果:3/4 通过。**

| # | 复跑 | 证据 |
|---|---|---|
| 4 | ✅ | UNION ALL 双表 + payment_type=1,占比 65.99%,解读明示合并统计 |
| 15 | ✅ | congestion>0 + 双表 UNION + zones + 排序(Manhattan 2,511,927,含绿车增量) |
| 17 | ✅ | payment_type=2 + 双表 UNION + ORDER BY ASC(EWR 0.16 最短) |
| 12 | ❌ | SQL 与修复前**逐位相同**(17.08%/18.74%)——payment_type=1 过滤仍未采用 |

**二十问终局:19/20(95%)。** 连续 20 问全过判据仍未达成;按 G6 指令停止
提示词迭代。#12 归因初判(留待 M4 golden SQL 层确认):"黄车和绿车分别"的
对称分组场景下,模型把挂于 yellow 词条的 filter 视为单侧口径,为保持两侧
对称而整体放弃过滤——术语 filter 的"按表局部适用"语义与"指标口径全局适用"
语义存在歧义,属提示词工程深层问题,适合 M4 以 expected_sql 基线驱动迭代。

---

## 11. 封存记录(2026-09-19)

里程碑 3 按人类裁定收官:二十问 19/20 通过(95%),安全底线与可核验性 20/20
全过。唯一偏差 #12 及 metric_filter 修复方案见 goals.md §4 偏差注记与 §7
待办池,随 M4 启动处理。本文档自此封存,后续变更入 milestone-4 文档。

交付清单:T8 llm/chat.py、T9 sqlgen/(含人类审定 system.md+3 个 few-shot)、
T10 guard/validate.py(双轮红队零穿透)、T11 exec/runner.py、T12 nl2data/
qa.py+audit.py+CLI ask/audit;测试 442 passed+1 skip+1 xfail+7 xpassed,
覆盖 93%,ruff 零告警。
