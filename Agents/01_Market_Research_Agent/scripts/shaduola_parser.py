#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shaduola_parser.py — 把类目导出数据（CSV / XLSX）标准化为 JSON

用途：解决「数据怎么进得来」这一步。Agent 只吃标准化后的 JSON，不吃各平台
      五花八门的导出格式。

依赖：仅标准库。XLSX 支持需要 openpyxl（可选，缺失时会给提示）。

用法：
    python shaduola_parser.py <输入文件> [-o 输出.json] [--mapping 映射.json]
    python shaduola_parser.py data.csv -o out.json
    python shaduola_parser.py data.csv --profile     # 只打印识别到的列，不转换
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from typing import Any

# [本地补丁 · 非上游代码] Windows 控制台默认 GBK(CP936) 时，本脚本输出的 emoji/中文
# 会抛 UnicodeEncodeError 而中断。强制标准输出走 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 列名映射：键 = 标准字段名，值 = 可能出现的列名（不区分大小写，去空格后匹配）
# ---------------------------------------------------------------------------
DEFAULT_MAPPING: dict[str, list[str]] = {
    "title":        ["商品名称", "商品名", "标题", "产品名称", "product name", "title", "nome do produto"],
    "price":        ["价格", "售价", "均价", "商品价格", "price", "preço", "preco"],
    "monthly_sales": ["月销量", "月销", "30天销量", "近30天销量", "monthly sales", "vendas mensais"],
    "monthly_revenue": ["月销售额", "月销售额(元)", "销售额", "月gmv", "revenue", "faturamento"],
    "total_sales":  ["总销量", "累计销量", "total sales", "vendas totais"],
    "seller":       ["卖家", "店铺", "店铺名称", "卖家名称", "seller", "loja", "vendedor"],
    "brand":        ["品牌", "品牌名", "brand", "marca"],
    "rating":       ["评分", "商品评分", "rating", "avaliação", "avaliacao"],
    "review_count": ["评价数", "评论数", "评价数量", "review count", "avaliações"],
    "listing_age_days": ["上架天数", "上架时间", "已上架", "listing age"],
    "url":          ["商品链接", "链接", "url", "link"],
    "category":     ["类目", "分类", "category", "categoria"],
}

# 标准化输出中的数值字段
NUMERIC_FIELDS = [
    "price", "monthly_sales", "monthly_revenue", "total_sales",
    "rating", "review_count", "listing_age_days",
]

# 中文数量单位
_CN_UNITS = {"万": 10_000, "亿": 100_000_000, "k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000}


def _norm_key(s: str) -> str:
    """归一化列名：去空白、全角转半角、小写。"""
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[\s\(\)（）\[\]【】:：]", "", s)
    return s.strip().lower()


def _to_number(raw: Any) -> float | None:
    """
    把脏数据转成数字。

    处理：货币符号（R$ / $ / ¥ / BRL）、千分位（逗号/点/空格）、
          中文数量单位（万 / 亿）、百分比、空值、'-' / 'N/A'。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    s = str(raw).strip()
    if s == "" or s.lower() in {"-", "--", "n/a", "na", "não", "nao", "null", "none", "无", "暂无"}:
        return None

    # 去掉货币符号与文字
    s = re.sub(r"(?i)(R\$|BRL|USD|\$|¥|€|£|元|雷亚尔)", "", s)
    s = s.replace("%", "").strip()

    # 中文/英文数量单位
    mult = 1.0
    for unit, factor in _CN_UNITS.items():
        if s.endswith(unit):
            mult = float(factor)
            s = s[: -len(unit)].strip()
            break

    # 去掉千分位分隔（保留最后一个作为小数点的情况交给下面判断）
    s = s.replace(" ", "")
    if "," in s and "." in s:
        # 两者都有：最后出现的那个是小数点  1.234,56 → 1234.56 / 1,234.56 → 1234.56
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # 只有逗号：1,234 视为千分位；1,5 视为小数点
        head, _, tail = s.rpartition(",")
        s = (head.replace(",", "") + "." + tail) if len(tail) == 1 else s.replace(",", "")

    try:
        return float(s) * mult
    except ValueError:
        return None


def _to_days(raw: Any) -> int | None:
    """上架时间 → 天数。支持 '30天' / '2个月' / '2026-01-15' / '365'。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"n/a", "-", "无"}:
        return None

    m = re.search(r"(\d+(?:\.\d+)?)\s*(天|日)", s)
    if m:
        return int(float(m.group(1)))
    m = re.search(r"(\d+(?:\.\d+)?)\s*个?\s*月", s)
    if m:
        return int(float(m.group(1)) * 30)
    m = re.search(r"(\d+(?:\.\d+)?)\s*年", s)
    if m:
        return int(float(m.group(1)) * 365)
    m = re.match(r"^\d{4}[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        # 粗算：按当年 1 月 1 日起算，避免引入 datetime 时区问题
        return int((int(m.group(1)) - 1) * 30 + int(m.group(2)))
    n = _to_number(s)
    return int(n) if n is not None else None


# ---------------------------------------------------------------------------
# 读取层
# ---------------------------------------------------------------------------
def read_rows(path: str) -> list[dict[str, Any]]:
    """读 CSV / TSV / XLSX 为 list[dict]。自动探测编码与分隔符。"""
    ext = os.path.splitext(path)[1].lower()

    if ext in {".xlsx", ".xlsm", ".xls"}:
        try:
            import openpyxl  # type: ignore
        except ImportError:
            sys.exit(
                "[ERROR] 读取 XLSX 需要 openpyxl。请先安装：\n"
                "        pip install openpyxl\n"
                "        或者把表格另存为 CSV 后重试。"
            )
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = [str(c) if c is not None else "" for c in next(rows_iter)]
        except StopIteration:
            return []
        out = []
        for r in rows_iter:
            if r is None or all(c is None or str(c).strip() == "" for c in r):
                continue
            out.append({header[i]: (r[i] if i < len(r) else None) for i in range(len(header))})
        wb.close()
        return out

    # 文本类：探测编码
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                text = f.read()
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        sys.exit(f"[ERROR] 无法识别文件编码：{path}")

    sample = text[:4096]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.DictReader(text.splitlines(), delimiter=delim)
    return [dict(r) for r in reader]


def build_column_map(header: list[str], custom: dict | None = None) -> dict[str, str]:
    """把标准字段映射到实际列名。返回 {标准字段: 实际列名}。"""
    mapping = dict(DEFAULT_MAPPING)
    if custom:
        for k, v in custom.items():
            mapping[k] = v if isinstance(v, list) else [v]

    norm_header = {_norm_key(h): h for h in header if h is not None}
    resolved: dict[str, str] = {}
    for field, aliases in mapping.items():
        for alias in aliases:
            key = _norm_key(alias)
            if key in norm_header:
                resolved[field] = norm_header[key]
                break
        else:
            # 模糊匹配：别名是列名的子串
            for alias in aliases:
                key = _norm_key(alias)
                hit = next((orig for nk, orig in norm_header.items() if key and key in nk), None)
                if hit:
                    resolved[field] = hit
                    break
    return resolved


# ---------------------------------------------------------------------------
# 转换层
# ---------------------------------------------------------------------------
def normalize(rows: list[dict[str, Any]], colmap: dict[str, str]) -> list[dict[str, Any]]:
    """按映射抽字段 + 清洗数值 + 丢弃无价格无销量的行。"""
    out: list[dict[str, Any]] = []
    dropped = 0

    for r in rows:
        item: dict[str, Any] = {}
        for field, src in colmap.items():
            raw = r.get(src)
            if field in NUMERIC_FIELDS:
                item[field] = (
                    _to_days(raw) if field == "listing_age_days" else _to_number(raw)
                )
            else:
                item[field] = (str(raw).strip() if raw is not None else None) or None

        # 月销售额缺失时用 价格 × 月销量 补算
        if item.get("monthly_revenue") is None and item.get("price") and item.get("monthly_sales"):
            item["monthly_revenue"] = round(item["price"] * item["monthly_sales"], 2)

        # 丢掉完全没有价值信号的行
        if item.get("price") is None and item.get("monthly_sales") is None:
            dropped += 1
            continue
        out.append(item)

    if dropped:
        print(f"[warn] 丢弃 {dropped} 行（价格与月销量均缺失）", file=sys.stderr)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="类目导出数据 → 标准化 JSON")
    ap.add_argument("input", help="输入文件（CSV / TSV / XLSX）")
    ap.add_argument("-o", "--output", help="输出 JSON 路径（默认 <输入名>.normalized.json）")
    ap.add_argument("--mapping", help="自定义列名映射 JSON，形如 {\"price\": [\"我的价格列\"]}")
    ap.add_argument("--profile", action="store_true", help="只打印列识别结果，不转换")
    args = ap.parse_args()

    rows = read_rows(args.input)
    if not rows:
        sys.exit("[ERROR] 没读到数据行。请确认文件非空、且第一行是表头。")

    header = list(rows[0].keys())
    custom = json.load(open(args.mapping, encoding="utf-8")) if args.mapping else None
    colmap = build_column_map(header, custom)

    if args.profile:
        print(f"文件：{args.input}\n数据行数：{len(rows)}\n")
        print("识别到的列：")
        for h in header:
            print(f"  - {h}")
        print("\n字段映射结果：")
        for field in DEFAULT_MAPPING:
            src = colmap.get(field)
            print(f"  {'✅' if src else '❌'}  {field:20} <- {src if src else '（未识别，可用 --mapping 指定）'}")
        missing = [f for f in ("title", "price", "monthly_sales") if f not in colmap]
        if missing:
            print(f"\n[提示] 关键字段未识别：{missing}。建议用 --mapping 手动指定。")
        return

    for required in ("price", "monthly_sales"):
        if required not in colmap:
            sys.exit(
                f"[ERROR] 必需字段 `{required}` 未识别到。\n"
                f"        用 --profile 看识别结果，或 --mapping 指定列名。"
            )

    items = normalize(rows, colmap)
    out_path = args.output or os.path.splitext(args.input)[0] + ".normalized.json"

    payload = {
        "meta": {
            "source_file": os.path.basename(args.input),
            "row_count_raw": len(rows),
            "row_count_kept": len(items),
            "column_map": colmap,
        },
        "items": items,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"✅ 已写出 {out_path}")
    print(f"   保留 {len(items)} / {len(rows)} 行")
    print(f"   识别字段 {len(colmap)} 个：{', '.join(sorted(colmap))}")


if __name__ == "__main__":
    main()
