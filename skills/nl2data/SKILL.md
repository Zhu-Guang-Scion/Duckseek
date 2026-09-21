---
name: nl2data
description: 用户想对已注册的 Excel/Access/Parquet 数据集做自然语言查询时使用（如"3 月黄车总订单量""按行政区汇总小费"）。nl2data 经业务口径词典与只读护栏返回可核验答案，宿主通过 MCP 工具 nl2data_status / nl2data_list_tables / nl2data_ask 驱动全流程。
---

# nl2data：用自然语言查已注册的表格数据

工具面只有三个，全部只读：`nl2data_status` / `nl2data_list_tables` / `nl2data_ask`。
本文件是调用契约；注册宿主的配置见 [README.md](README.md)，凭证模板见
[config-template.sh](config-template.sh)。

## 1. 快速判断：何时用 nl2data

用户问题是对**已注册数据集**的统计/查询 → 优先 nl2data，**不要自己拼 SQL 直连
DuckDB 或读 Parquet**：nl2data 侧有业务口径词典（glossary，例如"小费率"隐含
`payment_type = 1` 过滤）与只读护栏，绕开会丢口径一致性并失去安全防线。

不属于 nl2data 的问题（数据未注册、要改写数据、纯闲聊）→ 如实说明，不强行调用。

## 2. 标准工作流（顺序即契约）

### Step a — 先调 `nl2data_status` 诊断

返回字段：`env`（六变量 presence 布尔）、`missing_env`（缺失变量名，永远不含值）、
`llm_ready` / `embedding_ready`、`catalog{exists, sources, tables}`、
`index{built, built_at}`、`ready`。

- `missing_env` 非空 → 用 §3 固定话术向用户索要，配好后再续。
- `catalog.exists=false` 或 `tables=0` → 数据未注册，按 §5 速查表第一条处置。
- `index.built=false` → 让用户在数据仓库根目录按序执行 CLI 三步：

  ```
  uv run nl2data profile --all
  uv run nl2data cards build --all
  uv run nl2data index build     # 需要先配好 EMB_* 三个环境变量
  ```

- `ready=true`（或仅缺 EMB_*，BM25 降级仍可问，但建议补齐以获最佳召回）→ 进入 Step b。

### Step b — 问题涉及表结构时，调 `nl2data_list_tables`

按 source 分组返回 `{table, rows}`；`rows=null` 表示该表尚未画像。

### Step c — 调 `nl2data_ask` 提问

参数只有一个自然语言问句（把必要的上下文并进问句里）：

- 成功：`{answer, sql, row_count, elapsed_ms, source_tables, embedding_degraded}`；
- `needs_clarification=true` → 把 `question` 反问**原样**转给用户，拿到答复后发起
  **一次新的 ask**（把澄清并入问句，见纪律 3）；
- `{error}` → 按 §5 速查表处置。

呈现纪律：`embedding_degraded=true` 时必须告知用户"本次检索为 BM25 降级，
建议补齐 EMB_* 以恢复最佳召回"——降级披露由服务端响应携带，不依赖宿主自觉。

### Step d — 呈现

答案（answer 的 markdown 表格/画像）+ 实际执行的 SQL（独立代码块）+
行数/耗时/检索表。最后提醒用户：**SQL 可人工核验**——nl2data 的承诺是
"答案永远附带实际执行的 SQL"。

## 3. 缺配置时的固定话术与一键模板

发现 `missing_env` 非空时，对用户说（占位符按实际替换）：

> nl2data 需要以下环境变量才能工作，当前缺少：{missing_env}。
> 请把模板填好后提供，或填入 MCP 宿主的 env 配置后重启会话：
>
> ```
> export LLM_BASE_URL="<LLM 网关地址>"
> export LLM_API_KEY="<LLM 密钥>"
> export LLM_MODEL="<模型名>"
> export EMB_BASE_URL="<embedding 网关地址>"
> export EMB_API_KEY="<embedding 密钥>"
> export EMB_MODEL="<embedding 模型名>"
> ```
>
> 这些值只进入服务器环境变量，我不会把它们写入任何文件。

注意：环境变量在服务器**启动时**读取。用户提供值后，需把它们放进宿主
mcpServers 配置的 `env` 块并重启会话/服务器，再调 `nl2data_status` 确认
`missing_env` 为空后才继续。完整模板（含示例注释）见
[config-template.sh](config-template.sh)。

## 4. 纪律（逐条遵守）

1. **绝不把 API key 写入任何文件或对话持久化处**；只在向用户索要时出现。
2. **不向 nl2data 传密钥类参数**——它只读环境变量；工具参数里永远只有自然语言问句。
3. **一次一问**；追问是新的一次 ask（把澄清或追加条件并入新问句）。
4. **拿不准口径时如实说"词典里未定义"**，不编造业务含义。

## 5. 故障速查表

| 现象 | 一句话处置 |
|---|---|
| `index.built=false` | 让用户在仓库根目录按序跑 Step a 的 CLI 三步（index build 前先配好 EMB_*）。 |
| `list_tables` 里查不到某表 | 数据未注册：让用户用 CLI 摄取（`uv run nl2data ingest excel\|access\|parquet <文件>`）后重跑三步，再 `list_tables` 确认。 |
| ask 返回 `error` 含"超时" | 查询过大：建议用户缩小时间范围/分组粒度后重新 ask；连续超时再检查 `exec.timeout_seconds` 配置。 |
| ask 返回 `error` 含"护栏拒绝" | 生成 SQL 不合规且自动重试仍失败：换一种问法重新 ask，不要试图绕过。 |
| ask 返回 `error` 含"LLM 调用失败" | 凭证或网络问题：回到 Step a 复查 `missing_env`，让用户更新宿主 env 配置并重启。 |
