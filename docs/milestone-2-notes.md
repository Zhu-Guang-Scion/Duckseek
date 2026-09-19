# 里程碑 2 说明与基线(T5 卡片 / T6 词典 / T7 检索)

> 状态:🟢 已完成(2026-09-19,V2 终验通过;M3 起点基线 Recall@3=1.000)。
> 最高事实来源:[goals.md](../goals.md)。

## 1. 交付概览

| 任务 | 模块 | 状态 |
|---|---|---|
| T5 M-Schema 表卡片 | `catalog/cards.py` + `catalog/table_notes.py` + `retrieval/tokens.py` | 🟢(审查通过) |
| T6 业务术语词典 | `catalog/glossary.yaml`(用户维护)+ `catalog/glossary.py` | 🟢(审查通过;G3 词条录入悬置 1 处待人类决策) |
| T7 混合检索 | `retrieval/{embedding,bm25,index,retrieve}.py` + `eval/{golden,recall}.py` + CLI | 主体完成 |

## 2. T7 架构与决策记录

- **向量通道**:OpenAI 兼容 embedding(决策 8:EMB_BASE_URL/EMB_API_KEY/EMB_MODEL
  环境变量,密钥不落盘);bge-m3(1024 维,已归一化)实测通过;批 ≤32,429/5xx/超时
  指数退避重试,401 直接失败;磁盘缓存 key=sha256(model+text),位于
  `data/index/embedding_cache/`。
- **LanceDB 钉版 0.38.x**:0.39 起无 Windows wheel(仅 linux/macOS);0.38 全平台
  有轮子且含 merge_insert 单事务 API。表 `data/index/lance/cards`,显式
  float32[1024] schema,小规模不建 ANN(暴力精确检索)。
- **原子性**:先全部 embedding 成功 → 单次 merge_insert → delete 移除;embedding
  失败旧索引保持不动(专项测试锁定"半更新红线")。
- **BM25 分词决策:选 jieba(0.42.1),不用字符二元组**——依赖轻、中文语义切分好、
  无词典维护成本;预处理 lower→lcut→滤纯符号 token。bigram 的 IDF 区分度下降与
  索引膨胀是放弃原因(调研结论存档于任务记录)。
- **融合**:RRF(k=60,权重 bm25/vector 各 0.5,走配置);**术语直通**:问题含
  术语/同义词(子串、casefold)时对应表强制进候选头部;**预算组装**:按融合分
  贪心装卡片 Markdown,首张保底,超预算记 `dropped_tables`(带原因)。
- **降级**:EMB_* 缺失或 API 失败 → BM25 单通道 + warning(BM25 永远可用);
  零卡片 → 明确指引"先 ingest + profile + cards build"。
- **红线遵守**:索引对象仅卡片 Markdown(本身就是 schema 摘要)与术语;明细数据
  行不进索引;全链路无 LLM 文本生成。

## 3. 给里程碑 3(SQL 生成)的接口契约

```python
from retrieval.retrieve import retrieve, RetrievalResult
result = retrieve(question, cfg, k=5, token_budget=2000)
result.items          # [RetrievedItem(table, score, vector_rank, bm25_rank,
                      #             via_term, card_json)]
result.prompt_block   # 拼装好的卡片 Markdown,直接进提示词
result.total_tokens   # prompt_block 的估算 token 数
result.dropped_tables # [{table, reason}]
```

CLI:`nl2data index build` / `nl2data retrieve "<问题>" [-k N] [--budget N]
[--explain]` / `nl2data eval recall [--golden FILE]`。

## 4. 检索基线(2026-09-18,golden 20 条真实用例)

环境:NYC 三表(yellow/green/taxi_zones)+ 干扰表(excel demo 2 张 +
large_orders),bge-m3 向量通道 + jieba BM25 混合。

| 通道 | Recall@1 | Recall@3 | Recall@5 | MRR |
|---|---|---|---|---|
| **bm25+vector(默认)** | 0.492 | **0.842** | **1.000** | 0.967 |
| bm25-only(降级) | 0.492 | 0.758 | 0.892 | 0.975 |

分节 Recall@3(混合):一 0.875 / 二 0.875 / 三 0.933 / 四 0.792 / 五 0.667;
未满 R@3 共 7 条。**R@5=1.000:全部期望表都在 Top-5 视野内**——默认 k=5 下
里程碑 3 的素材完整性有保障;R@3 缺口属名次竞争,非漏检。

## 5. 未过 case 逐条归因(7 条)

| 归因类别 | case | 证据与假设 |
|---|---|---|
| A. 干扰表噪声(demo 数据混入卡片池) | 信用卡支付;布鲁克林绿车;布鲁克林上客曼哈顿下客;机场接送绿车;现金最短行政区;曼哈顿有效绿车;正常费率晚高峰 | 7 条中 6 条的 got_top3 含 `ding_dan_ming_xi` 或 `large_orders`(excel 演示表与 6M demo 表,与"订单/金额"高频词词面强相关)。这不是检索器缺陷而是库内噪声——真实用户库无此混入。**缓解**:清理 demo 源后重跑,预期 R@3 显著上升 |
| B. taxi_zones 跨语言词面缺口 + 术语覆盖不足 | 布鲁克林绿车;布鲁克林上客;机场接送;曼哈顿有效绿车;正常费率晚高峰(共 5 条缺 taxi_zones) | 卡片 Borough/Zone 值全英文("Manhattan"),BM25 对中文"曼哈顿"零词面命中;向量通道部分补偿(R@5 时 taxi_zones 均在视野)。"行政区"术语直通仅当问题含"区域/borough/zone"原词,"哪个区"不触发。**缓解**:glossary 增补 borough 中文同义词(人工维护是设计决策,待人类补充词条) |
| C. 编码列无业务标签 | 信用卡支付;现金最短行政区 | payment_type 为 BIGINT 编码(1/2/...),卡片无"信用卡=1"语义,两 case 仅靠泛词命中黄/绿表。**缓解**:glossary 增补支付方式术语(payment_type=1 信用卡等) |

## 6. 后续(已入 goals.md §7)

- 清理 demo 干扰源后重跑 golden 基线,量化噪声贡献
- G3 词条修复决策(正常费率→ratecodeid)后,glossary 增补 borough/支付方式同义词
- lancedb 0.39+ 若恢复 Windows wheel,评估升级

## 7. 术语增益 A/B 测量(2026-09-18,G3)

A 组 = T7 基线(hybrid,当时盘上第一版 9 条术语经结构层加载参与直通——严格说
非"绝对无术语",特此注明);B 组 = G3 19 条术语增益版(原 9 条安全名修订 +
区域中文名 6 条 + 编码列业务标签 4 条),同库同参数重跑 golden 20 条。

### 总体

| 指标 | A 组(基线) | B 组(术语增益) | 增益 |
|---|---|---|---|
| Recall@1 | 0.492 | 0.492 | — |
| **Recall@3** | **0.842** | **0.933** | **+9.1pp** |
| Recall@5 | 1.000 | 1.000 | — |
| MRR | 0.967 | 0.975 | +0.8pp |
| 未满 R@3 case 数 | 7 | 3 | -4 |

### 分节 Recall@3

| 分节 | A | B | 增益 |
|---|---|---|---|
| 一、单表精准区分层 | 0.875 | 0.875 | — |
| 二、单事实表+维度表 | 0.875 | **1.000** | +12.5pp |
| 三、双事实表联合对比 | 0.933 | **1.000** | +6.7pp |
| 四、干扰型跨表层 | 0.792 | 0.917 | +12.5pp |
| 五、隐含业务规则+跨表 | 0.667 | 0.833 | +16.6pp |

增益主源:区域中文名词条(曼哈顿/布鲁克林/肯尼迪机场等)修复 taxi_zones
跨语言漏召(归因 B),编码列标签(信用卡/现金支付)修复词面缺口(归因 C)。

### 剩余 3 条缺口归因更新(替换 §5 中已修复条目)

| case | 缺失 | 归因(更新) |
|---|---|---|
| 信用卡支付的订单占比 | green_tripdata | 支付类词条仅挂 yellow_tripdata(payment_type 实为双表共有,人类词典只配了 yellow);green 靠通道排名被 demo 表挤出 top3。**缓解**:为 green_tripdata 增补支付词条(人工维护词典) |
| 支付类型为现金…最短行政区 | green_tripdata | 同上(同一根因) |
| 正常费率下…哪个区的绿车 | taxi_zones | 问题含"正常费率"+"绿车"两术语已直通 yellow/green;"哪个区"的"区"不构成"区域"的子串,术语未触发,taxi_zones 靠通道被 demo 表挤出。**缓解**:同义词补"区"字或清理 demo 干扰源(§7 待办) |

demo 干扰表(ding_dan_ming_xi/large_orders)仍在 3/3 条缺口中出现——归因 A
(库内噪声)仍是 R@3 的最大剩余因素,清理后预期接近满分。

## 8. M3 起点基线与终版汇总(2026-09-19,V2)

零成本修复:「行政区」synonyms 增补「区」(check 19 条 0 错误)。
demo 干扰源清理(人类批准):excel_basic 与 large_orders 两个演示源全链移除
(DROP 视图、catalog/profile/cards/index 条目、parquet 与 tp_demo 产物文件;
data/taxi/ 真实数据保留)。绿车支付词条按人类指示未补——清理后基线已满分,
决策:无需增补。

### 三条基线(条件不同,各自独立,不可互比)

| 基线 | 库内容 | 术语 | R@1 | R@3 | R@5 | MRR | 缺口 |
|---|---|---|---|---|---|---|---|
| 无术语基线(T7,§4) | 6 表(含 demo) | 第一版 9 条 | 0.492 | 0.842 | 1.000 | 0.967 | 7 |
| 术语增益版(G3,§7) | 6 表(含 demo) | 19 条 | 0.492 | 0.933 | 1.000 | 0.975 | 3 |
| **M3 起点基线(V2)** | **3 表(纯 NYC)** | **19 条+「区」** | **0.492** | **1.000** | **1.000** | **0.975** | **0** |

M3 起点分节 R@3:五个分节全部 1.000;唯一 MRR 瑕疵为分节五 0.833
(单 case 首位排名偏差,不影响素材完整性)。

## 9. 已知限制

1. R@1=0.492:双表用例的第一名常为两表之一(集合召回任务下 R@1 天然受限),
   M3 依赖 R@3/R@5 而非首位。
2. 术语直通候选窗口:来自双通道 top-max(4k,20) 并集,目录超该窗口且术语表
   未被任一通道命中时不出现(当前 3 表库无影响)。
3. lancedb 钉版 0.38(0.39+ 无 Windows wheel);升级条件入 goals.md §7。
4. embedding 单价与限流:SiliconFlow 批 ≤32;磁盘缓存按 sha256(model+text)
   命中,重跑 index build 实测 embedded=0(零网络)。
5. golden 20 条以 2026-03 单月 NYC 切片为语义域;跨数据集泛化待里程碑 4 扩集。

## 10. 给里程碑 3(SQL 生成闭环)的交接

- **素材入口**:`retrieval.retrieve.retrieve(question, cfg, k=5, token_budget=2000)
  -> RetrievalResult`;`prompt_block` 为拼装好的卡片 Markdown(`---` 分隔,
  术语直通表置顶),直接进 system/user 提示词;`items[].card_json` 为结构化
  卡片(含 original_name 原名列,SQL 生成必须用安全名列名,业务含义靠原名)。
- **token 预算配置键**:`retrieval.token_budget`(默认 2000)、`retrieval.top_k`
  (默认 5);估算器 `tokens.estimator`(len_div_4,可替换 tiktoken)。
- **术语直通行为**:问题含术语/同义词(casefold 子串)时对应表强制进候选头部
  (`items[].via_term` 标注);glossary 的 filter/expression 是 DuckDB SQL 片段,
  语义是「隐含条件」,提示词工程可直接引用(如 现常费率 → ratecodeid = 1)。
- **LLM 客户端环境变量(goals.md 决策 3,锁定)**:`LLM_BASE_URL` /
  `LLM_API_KEY` / `LLM_MODEL`(OpenAI 兼容 API;禁止本地模型/抽象层)。
  Embedding 独立走 `EMB_BASE_URL`/`EMB_API_KEY`/`EMB_MODEL`(决策 8)。
- **执行环境**:已注册视图即查询对象(warehouse.duckdb 只读连接);护栏
  (sqlglot 静态校验+只读+LIMIT+超时)按 goals.md 决策 5 属里程碑 3 范围。
