# DuckSeek

**一句话简介**：DuckSeek（Python 包名 `nl2data`）是一个开源（Apache-2.0）的 CLI
工具，让你用自然语言直接查询大体量（十万至百万行级）Excel / Access / Parquet
数据——答案永远附带实际执行的 SQL，可人工核验。

## 架构

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

原始数据文件不进入 LLM 上下文：模型仅看到 schema 卡片与（必要时压缩的）查询
结果——大结果集（>20 行）只给统计画像与样本，小结果集（≤20 行）以内联样本透传。

## 快速开始（约 30 分钟）

**环境要求**：Python ≥ 3.11、[uv](https://docs.astral.sh/uv/)、六个环境变量
（LLM 与 embedding 各一套，均为 OpenAI 兼容 API；DeepSeek / Qwen / Kimi / GLM
等均可；同一厂商提供两者时，EMB_BASE_URL 可与 LLM_BASE_URL 相同）：

```bash
export LLM_BASE_URL="https://your-llm-provider/v1"
export LLM_API_KEY="sk-..."
export LLM_MODEL="your-chat-model"
export EMB_BASE_URL="https://your-emb-provider/v1"
export EMB_API_KEY="sk-..."
export EMB_MODEL="your-embedding-model"
```

```bash
git clone <repo> && cd nl2data
uv sync                                   # 创建 .venv 并安装依赖（锁定于 uv.lock）

# 1) 接入数据（三种来源任选；零拷贝注册或转换落盘；表名 = 文件名，clean_name 规则）
uv run nl2data ingest excel  path/to/订单.xlsx            # 多 sheet → 每 sheet 一表
uv run nl2data ingest access path/to/legacy.mdb            # 需 mdbtools（Linux/macOS/WSL）
uv run nl2data ingest parquet samples/nyc-taxi/yellow_tripdata.parquet --name yellow_tripdata

# 2) 画像 → 3) 卡片 → 4) 检索索引（幂等，可重复执行）
uv run nl2data profile --all
uv run nl2data cards build --all
uv run nl2data index build

# 5) 提问（答案附实际执行的 SQL）
uv run nl2data ask "2026年3月黄色出租车的总订单量是多少？"
# 交互模式（/retry [补充] /show sql /export csv <路径> /tables /exit）：
uv run nl2data ask
```

仓库自带 `samples/nyc-taxi/` 三张 NYC 出租车样本表（yellow / green / taxi_zones，
即上例与评测集所用数据）；其中 yellow 行程表 67.9MB，处于 GitHub 单文件
50–100MB 警告带（未超 100MB 硬限制），克隆即得、无需另行下载。中文列名自动
转拼音安全名（`订单ID → ding_dan_id`），原名完整保留在 catalog.yaml 双向映射中。

**配置与词典**：路径与阈值在 `config.yaml`（`NL2DATA_CONFIG` 可覆盖位置），密钥
只走环境变量、绝不落盘；业务黑话进 `data/catalog/glossary.yaml`（术语 → 表/列/
口径，表级 `filter` 仅本表生效，`metric_filter`+`applies_to` 随指标全局生效，
校验 `uv run nl2data glossary check`）；表级说明进 `docs/table_notes.md`。

## MCP + Skill 接入（AI 宿主）

任意 MCP 宿主（Claude Code / Cursor / zcode 等）可显式调用 DuckSeek：
`uv run nl2data mcp serve` 启动 stdio 服务器，注册片段与各宿主配置位置见
[skills/duckseek/README.md](skills/duckseek/README.md)；宿主 LLM 的调用契约
（工作流 / 纪律 / 故障速查）见 [skills/duckseek/SKILL.md](skills/duckseek/SKILL.md)。

```json
{
  "mcpServers": {
    "duckseek": {
      "command": "uv",
      "args": ["--directory", "<本仓库绝对路径>", "run", "nl2data", "mcp", "serve"],
      "env": {
        "LLM_BASE_URL": "<LLM 网关地址>",
        "LLM_API_KEY": "<LLM 密钥>",
        "LLM_MODEL": "<模型名>",
        "EMB_BASE_URL": "<embedding 网关地址>",
        "EMB_API_KEY": "<embedding 密钥>",
        "EMB_MODEL": "<embedding 模型名>"
      }
    }
  }
}
```

注意：`env` 块必须显式携带六个变量——MCP 客户端 stdio 启动默认只透传安全白名单
环境变量；密钥轮换后需重启会话（环境变量启动时读取）。

## 评测体系（敢迭代）

三层判定，一条命令回归：

```bash
uv run nl2data eval e2e [--save-baseline]   # golden 20 条 × N=3 多数决
uv run nl2data eval recall                  # 仅召回层
uv run nl2data audit 10                     # 最近问答审计
```

- **L1 召回**：retrieve 是否召回期望表（Recall@3）；
- **L2 SQL**：全链是否执行成功（反问=失败；护栏拒/执行错=错误；SQL 文本不比对）；
- **L3 结果**：与 golden 参考值语义等价（行多重集合、列超集投影、数值容差 +
  ×100 单位等价标注；不做行式/列式形状等价）。

golden 集（`eval/recall_golden.yaml`，20 条真实问答三元组）与冻结基线
（`eval/baseline_e2e.json`，git 跟踪：**L1=1.000 / L2=0.95 / L3=0.75**）构成回归
锚点：改提示词/换模型后重跑，与基线 diff 即逐 case 风险清单。已知摆动说明：四个
边界/风格类 case（#7 多列形状 / #11 时间列归因 / #16 反问边界 / #19 百分比舍入）
在 N=3 多数决下仍可能双向翻转，thinking 关闭条件下 L3 ∈ [0.75, 0.80] 属正常带；
比对器已知边界（风格类差异不计为回归缺陷）见 docs/milestone-4-notes.md。

## 安全模型摘要

**宁可误拒，不可漏放；拒绝必须给出可读原因。**

1. **护栏层**：九条规则全走 sqlglot 解析树（注释/大小写/嵌套免疫）——语句白名单
   （仅 SELECT/WITH）、表白名单、表函数一票否决、列存在性、LIMIT 注入 500/封顶
   10000；
2. **沙箱层**：只接受护栏签发的 `ValidatedSQL` 类型 + DuckDB `read_only=True` +
   守护线程超时——伪造入参在类型层即被拒；
3. **红队制度**：累计 100+ 对抗样本（提示注入/SQL 注入/文件读取/CTE 藏写等），
   全部拦截后固化进测试套件；
4. **密钥纪律**：API key 不写入任何文件/日志/异常消息/工具参数与返回值
   （专项测试断言）。

## 命名说明

| 面 | 名称 |
|---|---|
| 产品 / 分发版 | DuckSeek |
| Python 包与 CLI 命令 | nl2data（`uv run nl2data …`） |
| MCP 服务器与三工具 | duckseek / duckseek_status / duckseek_list_tables / duckseek_ask |
| Skill | skills/duckseek/（name: duckseek） |

> 包级重命名（nl2data → duckseek，CLI 同步更名并给出迁移说明）列在路线图阶段三；
> 此前宿主面与命令行名并存属预期，SKILL.md 内已注明对应关系。

## 路线图

- **阶段二（准确率工程）**：多候选 SQL + 选择器、实体索引、Python 分析沙箱、查询缓存；
- **阶段三（团队开源）**：Web UI、多用户只读与审计、docker-compose、包级更名
  duckseek（含 CLI 迁移说明）、中英双 README；
- 里程碑历史与当前状态见 [goals.md](goals.md) §4（M1-M5 全部完成）。

## 文档地图

- [goals.md](goals.md) —— 工程内部事实源（目标、锁定决策、契约、里程碑记录）
- [docs/architecture.md](docs/architecture.md) —— 架构与设计决策全文（本 README 母体）
- [docs/milestone-{1..5}-notes.md](docs/) —— 各里程碑验收留档
- [skills/duckseek/](skills/duckseek/) —— Skill 三件套（SKILL.md / 注册指南 / 凭证模板）
- [docs/table_notes.md](docs/table_notes.md) —— 表级说明（注入卡片）
- [eval/recall_golden.yaml](eval/recall_golden.yaml) —— golden 评测集

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
