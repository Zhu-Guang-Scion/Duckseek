# 示例 2:编码字段过滤 + 术语口径计算
# 对应 golden 层一/层三的编码列+指标口径模式

问题:信用卡支付的黄色出租车订单平均小费率是多少？

SQL:

```sql
SELECT AVG(t.tip_amount / NULLIF(t.fare_amount, 0)) AS avg_tip_rate
FROM yellow_tripdata t
WHERE t.payment_type = 1
```
