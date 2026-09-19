# few-shot 示例目录(T9)

每个 `*.md` 文件为一个示例,按文件名排序取前 `sqlgen.few_shot_count` 个注入。
建议格式(单文件,含一对问答):

```markdown
## 示例

问题:2026年3月黄色出租车的总订单量是多少?

提供的表:(由系统注入,示例中省略)

输出:
{"sql": "SELECT count(*) AS total_orders FROM yellow_tripdata"}
```

当前目录为空(占位);积累真实失败案例后逐条沉淀。
