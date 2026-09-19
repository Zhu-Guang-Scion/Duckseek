# 里程碑 1「数据能进来」说明与交接

> 状态:🟢 已完成(V1 总验收通过,2026-09-18)。
> 目标、契约与决策的最高事实来源是仓库根目录的 [goals.md](../goals.md)。

## 1. 交付概览

| 任务 | 内容 | 模块 |
|---|---|---|
| T1 | uv/ruff/pytest/Apache-2.0/CLI 骨架 | `pyproject.toml`、`nl2data/` |
| T2 | 多 sheet Excel → Parquet + DuckDB 视图 + catalog 血缘 | `ingest/excel.py` |
| T3 | Access(.mdb/.accdb)→ Parquet,契约与 T2 一致 | `ingest/access.py` |
| T4 | 聚合 SQL 数据画像 → `data/catalog/profiles/<table>.json` | `catalog/profiler.py` |

公共契约层(主智能体维护,供 T2/T3 复用):`ingest/common.py`(管线)、
`catalog/naming.py`(`clean_name`,经 `ingest/common.py` 再导出)、
`catalog/store.py`(catalog.yaml 读写)、`nl2data/config.py`(YAML 配置)。

## 2. CLI 命令面(里程碑 1)

```
nl2data --version
nl2data ingest excel <file> [--sheet NAME] [--name ALIAS]
nl2data ingest access <file> [--table NAME]
nl2data ingest parquet <file> [--name ALIAS]
nl2data profile (--table NAME | --all) [--force]
```

所有命令支持 `--config PATH`(或环境变量 `NL2DATA_CONFIG`)覆盖配置文件。

## 3. 给里程碑 2(M-Schema 卡片 + 检索)的契约交接

### catalog.yaml(goals.md §5.2,读写走 `catalog.store.CatalogStore`)

```yaml
sources:
- name: <clean source slug>        # clean_name(文件名 stem 或 --name 别名)
  type: excel | access
  path: <原始文件路径,原样记录>
  ingested_at: <UTC ISO8601>
  tables:
  - name: <clean 表名,即 DuckDB 视图名,全仓库唯一>
    original_name: <原 sheet 名 / Access 表名>
    parquet: <相对 data 根的 Parquet 路径>
    rows: <行数>
    columns:
    - name: <clean 列名>
      original_name: <原列名>       # 原名↔安全名双向映射就在这里
```

### profile JSON(T4 任务书契约,生成方 `catalog.profiler`)

`data/catalog/profiles/<table_name>.json`:

```json
{
  "table": "...", "original_name": "...", "source": "...", "rows": 0,
  "profiled_at": "ISO8601", "sampled": true,
  "columns": [{"name": "...", "original_name": "...", "dtype": "...",
               "null_rate": 0.0, "distinct_count": 0,
               "enum_values": ["..."], "sample_values": ["..."],
               "min": 0, "max": 0,
               "quantiles": {"p25": 0.0, "p50": 0.0, "p75": 0.0},
               "avg_len": 0.0, "error": "..."}]
}
```

键出现规则:`sampled` 仅大表(行数 > `profile.sampled_over_rows`,默认 500 万,
10% bernoulli 抽样)出现;`enum_values` 仅 distinct ≤ `enum_max_distinct`(默认 50)
的 VARCHAR/BOOLEAN 列出现;`min/max` 仅数值/时间列且存在非空值;
`quantiles` 仅数值列;`avg_len` 仅 VARCHAR 列;`error` 为单列降级标记,
出现时该列其余统计可能缺失。

### 命名规则(goals.md §5.4)

`clean_name(raw, existing=None, *, max_length=63)`(实现在 `catalog/naming.py`,
经 `ingest/common.py` 再导出):仅 `[a-z0-9_]`、不以数字开头、≤63 字符、中文转
pypinyin 拼音、无法转换字符下划线化、同音冲突加 `_N`;传入 `existing` 时为分配器
语义(返回的名字自动登记)。反查原名一律走 catalog.yaml,不猜测。

### 产物布局(goals.md §5.5)

- Parquet:`data/parquet/<source_slug>/<table_name>.parquet`
- DuckDB:`data/warehouse.duckdb`,表以 `CREATE OR REPLACE VIEW` 注册
  (视图即表名,`SELECT * FROM read_parquet(...)`)
- 画像:`data/catalog/profiles/<table_name>.json`(增量:Parquet mtime 更新才重算)

## 4. 实现要点与行为说明

- **读取器(T2)**:优先 DuckDB excel 扩展(`read_xlsx`,core extension,签名自动
  加载,首次使用需联网下载);扩展不可用、表头需下移(`detect_header_row()` 探测到
  标题行/空首行)或读取出错时,回退 `pandas.read_excel(engine="openpyxl")` 并记
  warning。两条路径共用后处理:剔除全空行、剔除无表头的全空列、保留"有表头但数据
  全 NULL"的列并降级为 VARCHAR 记录日志。
- **合并单元格**:只有左上角锚点有值,其余读为 NULL;不自动填充(两条读取路径一致)。
- **公式单元格**:读到缓存值;从未被 Excel 保存过的文件(openpyxl 生成)无缓存,
  读为 NULL。
- **旧版格式**:.xls/.xlsb/.numbers 不支持,`ingest excel` 按扩展名明确拒绝。
- **Access(T3)**:经 mdbtools 子进程(`mdb-tables -1` 枚举,`mdb-export -D/-T`
  固定日期格式导出,退出码 1 视为失败);系统表(MSys*)与临时表防御性跳过;CSV 用
  pyarrow.csv 解析(容忍引号内换行)。runner 可注入,无 mdbtools 环境可完整单测。
- **幂等**:同 slug 重导会先 DROP 旧视图、删除旧 Parquet 再写入,catalog 按
  source name 原位替换并刷新时间戳。

## 5. 已知限制

1. **Access 在原生 Windows 不可用**:mdbtools 无 winget/Chocolatey/Scoop/MSYS2
   包;推荐 WSL(`sudo apt install mdbtools`)或 Linux/macOS。CLI 会给出可操作报错。
2. **加密 .accdb 不支持**(goals.md §2):mdbtools 报错时若 stderr 疑似加密/损坏,
   报错信息会附提示。
3. **OLE/附件列未识别**:CSV 输出中无法与文本区分,按文本接入;待有真实环境后补
   检测(记入 goals.md §7)。
4. **合并单元格不回填**:如需"华东"填满合并区,属数据清洗语义,里程碑 1 不做。
5. **大文件**:pandas 回退路径对 50 万行级 xlsx 较慢(分钟级);`engine="calamine"`
   加速已记入 goals.md §7 待办池。主路径 read_xlsx 为 C++ 流式,数十万行秒级。
6. **日期语义(Access)**:mdb-export 以 `-D/-T` 固定格式导出为字符串,里程碑 1
   不做类型提升,画像 dtype 显示 VARCHAR。
7. **环境备忘**:本机 32 核且内存紧张时,OpenBLAS 可能导入失败;包入口已默认
   `OPENBLAS_NUM_THREADS=1`(可用环境变量覆盖)。

## 6. 质量门(V1 验收实测,2026-09-18)

- `uv sync && uv run pytest`:**78 passed, 1 skipped**(唯一 skip 为 mdbtools 集成
  占位);默认套件不包含 `@pytest.mark.slow` 的 10 万行用例,需 `uv run pytest -m slow`
  另跑(1 passed,实测 ~3s,目标 <60s)
- `uv run ruff check .`:零告警(E/F/I/UP/ANN,line-length 100,豁免 ANN401)
- 覆盖率:`uv run pytest --cov`,总计 92.39%,被测模块最低 85%,全部 ≥ 80%
- 全链路(V1 在 `rm -rf .venv data` 后实测):`uv sync` → `ingest excel` →
  `profile --all` 一次跑通;profile 数值与 DuckDB 直查交叉验证一致;Access 真实链路
  因本机无 mdbtools 按豁免条款以 mock 单测 + 后缀拒绝实测覆盖
- CLI 日志级别默认 WARNING,可用 `NL2DATA_LOG_LEVEL=INFO` 提高(回退、空 sheet
  跳过等 warning/info 可见)

## 7. T-P 原生 Parquet 接入(2026-09-18,人类批准的小范围扩展)

`ingest/parquet.py` 提供 register 模式:`nl2data ingest parquet <file> [--name 别名]`
不复制文件,直接对已有 .parquet 建 DuckDB VIEW(列名清洗为安全名,原名进 catalog
血缘),catalog 登记 `source.type: parquet`,`tables[].parquet` 记录**原始路径**;
幂等语义与 excel/access 一致(重导替换视图与 catalog 条目)。

安全语义:重导清理(`_drop_previous_artifacts`)只删除 `data/parquet/` 管理区内的
文件,**用户外部文件永不删除**(含与其它来源同名替换的场景,有专项测试)。

### 600 万行实测(Windows,6M 行 × 6 列 / 117 MB,data/large_orders.parquet)

| 步骤 | 耗时 | 峰值内存(进程 PeakWorkingSet64) |
|---|---|---|
| ingest parquet(注册,仅读元数据) | 0.72 s | ~3 MB |
| profile --all(>500 万行触发 10% bernoulli 抽样,`sampled: true`) | 1.19 s | ~3 MB |
| cards build --all | 0.40 s | ~3 MB |

说明:峰值内存为 CLI 进程级,数据全程不进 Python(SQL 下推到 DuckDB)。
抽样语义:`rows` 与 min/max 恒为精确值(min/max 独立于抽样聚合,跨次运行稳定),
null_rate/基数/分位数按抽样基准计算。T-P 验收中发现并修复两处画像缺陷并补专项
测试:①"精确行数 × 抽样非空计数"混算导致 null_rate 虚高;②min/max 误走抽样
导致逐次漂移。测量已含 OPENBLAS_NUM_THREADS=1 默认防护。

## 8. NYC 出租车真实数据注册实测(2026-09-18,T-P 扩展)

数据:`测试样本_美国NYC出租车/`(2026-03 月份切片),register 模式零拷贝注册。

| 表(安全名) | 行数 | 注册耗时 | profile 耗时 | cards | 峰值内存 |
|---|---|---|---|---|---|
| yellow_tripdata_2026_03 | 3,952,451 | 1.34 s | 2.83 s(与 green 合计) | ~485 tokens | ~4 MB |
| green_tripdata_2026_03 | 44,208 | 0.73 s | (同上) | ~488 tokens | ~3 MB |

- profile/cards 全链路无报错;画像字段语义与 NYC TLC 数据一致(ehail_fee 全 NULL、
  passenger_count 等列 ~24%/15% 空值为该数据集真实特征)。
- **未触发抽样分支**:两表均低于 sampled_over_rows=500 万阈值(yellow 395 万),
  `sampled: true` 未出现——数据行数与任务书"千万行级"预期不符,按边界约定未改
  阈值,如实上报。抽样分支的行为已由 6M 行 large_orders(§7)与专项单测覆盖。
- **taxi_zones 文件缺失**:目录内仅有 yellow/green 两个 parquet,taxi_zones 未
  注册,等待数据文件。G2 golden 录入仍处停止状态(逻辑名映射:yellow_tripdata
  → yellow_tripdata_2026_03、green_tripdata → green_tripdata_2026_03 已可确定,
  taxi_zones → 无)。

## 9. NYC 数据定稿与 golden 评测集(2026-09-18,人类三项决策执行)

**表名恒等重注册**:yellow/green 以干净文件名(stem 即目标表名)重新注册,
`--name` 与表名一致——最终 catalog 表名与逻辑名完全相同
(yellow_tripdata / green_tripdata / taxi_zones),幂等替换自动清理旧视图,
旧注册的外部路径文件未受影响(管理区外不删);上次注册产生的孤儿
profile/cards 产物已清理。

**taxi_zones 数据来源与核对**:NYC TLC 官网 Trip Record Data 页面的官方
`taxi_zone_lookup.csv`,下载自
`https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv`;核对结果:
表头恰为 LocationID/Borough/Zone/service_zone 四列,265 数据行(官方 265 个
zone),空值 Borough=1、Zone=1、service_zone=2(官方数据本如此);经 pandas
转 Parquet 后注册,画像:borough 7 枚举(含 Unknown)、service_zone 4 枚举
(Airports/Boro Zone/EWR/Yellow Zone)。

**最终三表链路(profile 2.95s / cards 0.39s,峰值内存 ~3MB)**:

| 表(恒等名) | 行数 | cards tokens |
|---|---|---|
| yellow_tripdata | 3,952,451 | ~481 |
| green_tripdata | 44,208 | ~484 |
| taxi_zones | 265 | ~139 |

**G2 golden 评测集**:`eval/recall_golden.yaml` 录入 20 条(数量以 20 为准,
早期任务书"24"为笔误),question/note 原样保留,恒等映射无替换;
schema 校验器独立成 `eval/golden.py`(load_golden / validate_golden_schema /
validate_golden_tables / load_and_validate,供 T7 runner 复用)。
自检:20 条全量、YAML 解析通过、schema 通过(无重复 question)、
expected_tables 全部为 catalog 真实安全名。抽样分支确认不做演示
(large_orders 6M 已覆盖,阈值零改动)。
