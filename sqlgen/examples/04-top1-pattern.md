# 示例 4:top-1 收敛("哪个/最高" = GROUP BY + ORDER BY DESC + LIMIT 1)
# 对应"哪个 X 最 Y"模式,答案必须收敛到一行

问题:工作日晚高峰（17-19点）哪个行政区的绿车订单量最高？

SQL:

```sql
SELECT z.borough, COUNT(*) AS order_count
FROM green_tripdata t
JOIN taxi_zones z ON t.pulocationid = z.locationid
WHERE t.ratecodeid = 1
  AND EXTRACT(ISODOW FROM t.lpep_pickup_datetime) < 6
  AND EXTRACT(HOUR FROM t.lpep_pickup_datetime) >= 17
  AND EXTRACT(HOUR FROM t.lpep_pickup_datetime) < 19
GROUP BY z.borough
ORDER BY order_count DESC
LIMIT 1
```
