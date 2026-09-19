# 示例 3:共享字段未限定主体 → 双表 UNION ALL
# 对应"未限定车型 + 共享字段"模式(与 golden #4/#15/#17 同模式,刻意换题面)

问题:所有现金支付的订单总数是多少？

SQL:

```sql
SELECT COUNT(*) AS cash_orders
FROM (
  SELECT payment_type FROM yellow_tripdata WHERE payment_type = 2
  UNION ALL
  SELECT payment_type FROM green_tripdata WHERE payment_type = 2
) t
```
