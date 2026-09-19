# 示例 1:时间提取 + 区域维度关联
# 对应 golden 层二/层三的时间+区域组合模式

问题:曼哈顿区早高峰（7-9点）黄色出租车的订单量是多少？

SQL:

```sql
SELECT COUNT(*) AS order_count
FROM yellow_tripdata t
JOIN taxi_zones z ON t.pulocationid = z.locationid
WHERE z.borough = 'Manhattan'
  AND EXTRACT(HOUR FROM t.tpep_pickup_datetime) >= 7
  AND EXTRACT(HOUR FROM t.tpep_pickup_datetime) < 9
```
