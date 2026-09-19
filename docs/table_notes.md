# table_notes.md —— 表说明(人工维护)

<!--
格式:每个小节以 `# 表:<安全表名>` 或 `# 表:<原名>` 开头,
标题后的正文(直到下一个标题)会作为该表的 description 注入表卡片。
-->

# 表:yellow_tripdata
本表为黄色出租车订单;时间列为 tpep_pickup_datetime / tpep_dropoff_datetime(tpep = yellow 前缀)。

# 表:green_tripdata
本表为绿色出租车订单;时间列为 lpep_pickup_datetime / lpep_dropoff_datetime(lpep = green 前缀),不得使用 tpep_* 列名。

# 表:taxi_zones
区域维度表;borough 为行政区(5+2),zone 为片区(260 个);行程表经 pulocationid/dolocationid = locationid 关联本表。
