#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_sample.py — 生成一份带"脏数据"的样本类目导出表

用途：让使用者一拿到仓库就能立刻跑通 scripts/ 下的两个脚本，验证环境。
数据是**合成数据**，用于演示格式与清洗能力，不代表任何真实市场。

用法：python make_sample.py [-o ../samples/sample_patinete_eletrico.csv]
"""

import argparse
import csv
import random
import sys
from pathlib import Path

# [本地补丁 · 非上游代码] Windows 控制台默认 GBK(CP936) 时，本脚本输出的 emoji/中文
# 会抛 UnicodeEncodeError 而中断。强制标准输出走 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

random.seed(20261013)

# 品牌：头部 2 个 + 一批白牌（模拟"白牌仍有机会"的市场）
BRANDS = (
    ["MarcaA"] * 6 + ["MarcaB"] * 4 + ["MarcaC"] * 3
    + ["Generic"] * 12 + ["Sem marca"] * 15 + ["Outras"] * 10
)

# 卖家：CR10 由数据自然形成（3 个大卖家 + 一批小卖家）
SELLERS = (
    ["LojaGrande1"] * 8 + ["LojaGrande2"] * 6 + ["LojaGrande3"] * 5
    + [f"LojaPequena{i:02d}" for i in range(1, 32)]
)

POWERS = ["350W", "500W", "500W", "500W", "800W"]      # 500W 为主流
LOADS = ["100kg", "120kg", "150kg", "150kg", "150kg"]  # 150kg 为主流
FOLDABLE = ["Dobrável", "Dobrável", "Dobrável", "Não dobrável"]

# 价格带设计：1500–3200，销量在 1800–2400 集中（给波谷检测留出可识别结构）
PRICE_ANCHORS = [
    (1550, 40), (1700, 90), (1850, 160), (2000, 210), (2150, 230),
    (2300, 190), (2450, 120), (2600, 70), (2800, 40), (3050, 20),
]


def price_for(i: int) -> float:
    lo, _ = PRICE_ANCHORS[i % len(PRICE_ANCHORS)]
    return round(lo + random.uniform(-90, 90), 2)


def sales_for(price: float) -> int:
    # 找到最接近的锚点，取其销量 + 噪声
    best = min(PRICE_ANCHORS, key=lambda a: abs(a[0] - price))
    base = best[1]
    return max(0, int(random.gauss(base, base * 0.45)))


def config_for(price: float) -> tuple[str, str, str]:
    """
    按价格档位分配配置 —— 这一点很关键。

    如果配置与销量独立随机，配置交叉表算出来的"机会配置"就是噪声。
    真实市场里配置与价格强相关（功率高→贵），所以样本也必须这样构造，
    否则演示不出交叉表真正的用途。
    """
    if price < 1900:
        return "350W", "100kg", "10Ah"
    if price < 2400:
        return "500W", "150kg", "12Ah"
    return "800W", "150kg", "12Ah"


def dirty(v) -> str:
    """随机把数值格式弄脏，用来验证解析器的清洗能力。"""
    style = random.choice(["plain", "thousands_dot", "thousands_comma", "currency", "currency_comma_dot"])
    if isinstance(v, float):
        if style == "plain":
            return f"{v:.2f}"
        if style == "currency":
            return f"R$ {v:,.2f}"
        if style == "currency_comma_dot":
            return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{v:.2f}"
    # int
    if style == "thousands_dot":
        return f"{v:,}".replace(",", ".")
    if style == "thousands_comma":
        return f"{v:,}"
    if style == "currency":
        return f"R$ {v:,}".replace(",", ".")
    if style == "plain":
        return str(v)
    return str(v)


def age_dirty(days: int) -> str:
    style = random.choice(["days", "months", "date", "plain"])
    if style == "days":
        return f"{days}天"
    if style == "months":
        return f"{max(1, days // 30)}个月"
    if style == "date":
        return f"2025-{(days % 12) + 1:02d}-15"
    return str(days)


def main() -> None:
    ap = argparse.ArgumentParser()
    default_out = Path(__file__).resolve().parent.parent / "samples" / "sample_patinete_eletrico.csv"
    ap.add_argument("-o", "--output", default=str(default_out))
    ap.add_argument("-n", "--rows", type=int, default=50)
    args = ap.parse_args()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    header = [
        "商品名称", "价格", "月销量", "月销售额", "卖家", "品牌",
        "评分", "评价数", "上架时间", "商品链接",
    ]

    # 机会配置：在峰值价格带里放 4 条"高承重"配置（数量少，看是否有单品效率优势）
    opportunity_slots = {3, 13, 23, 33}

    with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for i in range(args.rows):
            p = price_for(i)
            s = sales_for(p)
            brand = BRANDS[i % len(BRANDS)]
            seller = SELLERS[i % len(SELLERS)]
            power, load, cap = config_for(p)
            fold = FOLDABLE[i % len(FOLDABLE)]

            # 少数条目改成差异化配置（高承重），用来演示"机会配置"判断
            if i in opportunity_slots and 1900 <= p < 2400:
                load = "200kg"

            title = (f"Patinete Elétrico Adulto {power} {load} Bateria {cap} "
                     f"com Banco {fold} para Mobilidade Urbana")
            if brand not in ("Generic", "Sem marca", "Outras"):
                title = f"{brand} {title}"

            rating = round(random.uniform(4.0, 5.0), 1)
            reviews = int(random.gauss(s * 0.6, s * 0.3)) if s else 0
            reviews = max(0, reviews)
            age = random.randint(20, 900)

            w.writerow([
                title,
                dirty(p),
                dirty(s),
                dirty(round(p * s, 2)) if random.random() > 0.25 else "",  # 25% 缺失，测补算
                seller,
                brand,
                f"{rating}",
                dirty(reviews),
                age_dirty(age),
                f"https://example.com/item/{100000 + i}",
            ])

    print(f"✅ 已生成样本：{args.output}（{args.rows} 行，含刻意混入的脏数据格式）")


if __name__ == "__main__":
    main()
