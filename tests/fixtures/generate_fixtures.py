"""nl2data Excel/Access 测试夹具生成脚本。

- 固定随机种子 random.Random(42),输出可复现;
- 可重复运行,每次直接覆盖旧文件;
- 100k 行大文件使用 openpyxl write_only 模式;
- 末尾打印每个文件的路径与行列数。
"""

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import openpyxl

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FIXTURE_DIR = Path(__file__).resolve().parent
ACCESS_DIR = FIXTURE_DIR / "access"
FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
ACCESS_DIR.mkdir(parents=True, exist_ok=True)

rng = random.Random(42)
results = []  # [(路径, 说明), ...]


def verify_xlsx(path: Path, read_only: bool = False) -> str:
    """重新打开 xlsx,以读取端视角报告各 sheet 行列数(含合并区信息)。"""
    wb = openpyxl.load_workbook(path, read_only=read_only)
    parts = [f"「{ws.title}」{ws.max_row}行x{ws.max_column}列" for ws in wb.worksheets]
    extra = ""
    if not read_only:
        merged = [
            f"{ws.title}!{m}"
            for ws in wb.worksheets
            for m in ws.merged_cells.ranges
        ]
        if merged:
            extra = "; 合并区=" + ",".join(merged)
    wb.close()
    return ",".join(parts) + extra


def record(path: Path, write_desc: str, read_only: bool = False) -> None:
    """记录一个夹具的写入说明与读取端验证结果。"""
    size = path.stat().st_size
    read_desc = verify_xlsx(path, read_only)
    results.append((str(path), f"写入: {write_desc} | 读取端验证: {read_desc} | {size} bytes"))


# ---------------------------------------------------------------- 1. 基础双 sheet
path = FIXTURE_DIR / "excel_basic.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "订单明细"
ws.append(["订单ID", "客户姓名", "金额", "下单日期", "是否VIP"])
names = ["张伟", "王芳", "李娜", "刘强", "陈静", "杨洋", "赵敏", "黄磊", "周杰", "吴霞",
         "徐磊", "孙丽", "马超", "朱婷", "胡军", "郭靖", "林萍", "何雨", "高飞", "罗成"]
start = date(2026, 1, 1)
for i in range(1, 21):
    ws.append([
        i,                                                       # 订单ID int 1..20
        None if i == 5 else names[i - 1],                        # 第 5 行客户姓名留空
        None if i == 8 else round(rng.uniform(1.0, 9999.99), 2),  # 第 8 行金额留空
        start + timedelta(days=i - 1),                           # 下单日期 2026-01-01 起递增
        bool(i % 2),                                             # 是否VIP True/False 交替
    ])
ws2 = wb.create_sheet("客户")
ws2.append(["客户ID", "姓名", "城市", "注册日期"])
cities = ["北京", "上海", "广州"]
for i in range(1, 9):
    ws2.append([i, names[i - 1], cities[(i - 1) % 3], start - timedelta(days=i * 5)])
wb.save(path)
record(path, "订单明细=21行x5列(含表头), 客户=9行x4列(含表头)")

# ------------------------------------------------- 2. 同音(拼音)表名冲突: 订单 vs 訂單
path = FIXTURE_DIR / "excel_pinyin_conflict.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "订单"
ws.append(["单号", "数量"])
for i in range(1, 4):
    ws.append([i, i * 10])
ws2 = wb.create_sheet("訂單")
ws2.append(["单号", "数量"])
for i in range(101, 104):
    ws2.append([i, i - 100])
wb.save(path)
record(path, "订单=4行x2列, 訂單=4行x2列(各含表头,均3行数据)")

# ---------------------------------------------------------------- 3. 完全空白 sheet
path = FIXTURE_DIR / "excel_empty_sheet.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "数据A"
ws.append(["编号", "名称"])
for i in range(1, 6):
    ws.append([i, f"名称{i}"])
wb.create_sheet("空表")  # 不写入任何内容,完全空白
wb.save(path)
record(path, "数据A=6行x2列(含表头), 空表=完全空白")

# ---------------------------------------------------------------- 4. 纵向合并单元格
path = FIXTURE_DIR / "excel_merged.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "销售区域"
ws.append(["区域", "省份", "销售额"])
provinces = ["江苏", "浙江", "上海", "安徽", "广东", "河北"]
for i, prov in enumerate(provinces):
    region = None
    if i == 0:
        region = "华东"   # 合并区左上角(A2)持有值,A3:A5 为空
    elif i == 4:
        region = "华南"
    elif i == 5:
        region = "华北"
    ws.append([region, prov, round(rng.uniform(1000.0, 99999.0), 2)])
ws.merge_cells("A2:A5")
wb.save(path)
record(path, "销售区域=7行x3列(含表头,6行数据)")

# ---------------------------------------------------------------- 5. 公式无缓存值
path = FIXTURE_DIR / "excel_formula.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "计算"
ws.append(["数值", "两倍"])
for i in range(10):
    ws.append([rng.randint(1, 100), f"=A{i + 2}*2"])  # =A2*2 ... =A11*2
wb.save(path)
record(path, "计算=11行x2列(含表头; 公式无缓存值,读取端为 None)")

# ---------------------------------------------------------------- 6. 刁钻表头
path = FIXTURE_DIR / "excel_tricky_names.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "数据"
long_header = "超长列名" + "测试列名验证截断逻辑" * 8  # 4 + 80 = 84 个汉字(>=80)
ws.append(["1月销量", "金额（元）", long_header, "金额", "金额"])  # 数字开头/全角括号/超长/重复
for i in range(1, 6):
    ws.append([i, i * 100, i, i * 7, i * 13])
wb.save(path)
record(path, "数据=6行x5列(含表头; 超长表头 84 个汉字; 「金额」重复 2 次)")

# ---------------------------------------------------------------- 7. 100k 大文件 (write_only)
path = FIXTURE_DIR / "excel_large_100k.xlsx"
wb = openpyxl.Workbook(write_only=True)
ws = wb.create_sheet("大表")
ws.append(["序号", "类别", "数值", "日期", "文本"])
categories = ["家电", "家具", "食品", "服装", "数码", "美妆", "母婴", "图书", "体育", "其他"]
epoch = date(2025, 1, 1)
for i in range(100000):
    ws.append([
        i,                                   # 序号 0..99999
        categories[i % 10],                  # 类别 10 类循环
        round(rng.random() * 1000.0, 4),     # 固定种子随机 float
        epoch + timedelta(days=i % 365),     # 2025-01-01 + (序号 mod 365) 天
        f"行{i}",                             # 文本
    ])
wb.save(path)
record(path, "大表=100001行x5列(含表头, write_only 模式)", read_only=True)

# ---------------------------------------------------------------- 8. 伪 .xls(扩展名拒绝测试)
path = FIXTURE_DIR / "legacy.xls"
path.write_bytes(b"legacy-xls-not-supported")
size = path.stat().st_size
results.append((str(path), f"写入: 非法 xls 原始字节 24 bytes | {size} bytes"))

# ------------------------------------------------- 8b. 首行标题行 / 空首行(detect_header_row)
path = FIXTURE_DIR / "excel_title_row.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "月报"
ws.append(["2026年1月销售月报"])  # 仅一个非空单元格的标题行,真实表头在第 2 行
ws.append(["订单ID", "金额"])
for i in range(1, 6):
    ws.append([i, i * 10.5])
ws2 = wb.create_sheet("空首行")
ws2.append([None, None])  # 首行全空
ws2.append(["编号", "数量"])
for i in range(1, 4):
    ws2.append([i, i * 2])
path2 = FIXTURE_DIR / "excel_all_null_column.xlsx"
wb2 = openpyxl.Workbook()
ws3 = wb2.active
ws3.title = "备注"
ws3.append(["编号", "备注"])  # 备注列有表头但数据全空 → 全 NULL 列降级 VARCHAR
for i in range(1, 6):
    ws3.append([i, None])
wb2.save(path2)
size2 = path2.stat().st_size
results.append((str(path2), f"写入: 备注=6行x2列(备注列数据全 NULL) | {size2} bytes"))
record(path, "月报=7行x2列(首行标题); 空首行=6行x2列(首行全空)")

# ---------------------------------------------------------------- 9. Access 表名清单
path = ACCESS_DIR / "tables_list.txt"
with open(path, "w", encoding="utf-8", newline="") as f:
    f.write("orders\n订单明细\n")
results.append((str(path), "两行表名: orders / 订单明细 (UTF-8)"))

# ---------------------------------------------------------------- 10. orders 导出 CSV
path = ACCESS_DIR / "orders_export.csv"
with open(path, "w", encoding="utf-8", newline="") as f:  # utf-8 无 BOM
    f.write("订单ID,客户,金额\n")
    f.write("1,张三,100.50\n")
    f.write('2,"李四,先生",200.00\n')  # 客户含逗号,双引号包裹
    f.write("3,王五,99.99\n")
results.append((str(path), "UTF-8 无 BOM; 3 行数据; 第 2 行客户含逗号且用双引号包裹; 金额含小数"))

# ---------------------------------------------------------------- 11. 空导出 CSV
path = ACCESS_DIR / "empty_export.csv"
with open(path, "w", encoding="utf-8", newline="") as f:
    f.write("列A,列B\n")  # 仅表头,无数据行
results.append((str(path), "仅一行表头 列A,列B,无数据行"))

# ---------------------------------------------------------------- 汇总输出
print("=" * 72)
print(f"夹具生成完成,共 {len(results)} 个文件:")
for p, desc in results:
    print(f"[OK] {p}")
    print(f"     {desc}")
print("=" * 72)
