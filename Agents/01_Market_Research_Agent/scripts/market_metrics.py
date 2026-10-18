#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_metrics.py — 类目数据的指标计算层

设计原则：**能算的别让模型猜。**
集中度、占比、分箱、交叉表都是确定性的计算，交给脚本；
Agent 只负责解读"这些数字意味着什么、下一步做什么"。

依赖：仅标准库。

用法：
    python market_metrics.py <normalized.json> [--bins 10] [--top 10]
                             [--config-fields 功率,承重] [--format md|json]
    python market_metrics.py data.normalized.json --config-fields "potencia,peso_max"
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from typing import Any

# [本地补丁 · 非上游代码] Windows 控制台默认 GBK(CP936) 时，本脚本输出的 emoji/中文
# 会抛 UnicodeEncodeError 而中断。强制标准输出走 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _get(item: dict, key: str) -> Any:
    return item.get(key)


def _safe_div(a: float, b: float) -> float:
    return (a / b) if b else 0.0


def _fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


# ---------------------------------------------------------------------------
# 1. 市场容量
# ---------------------------------------------------------------------------
def market_capacity(items: list[dict]) -> dict:
    prices = [i["price"] for i in items if i.get("price")]
    sales = [i["monthly_sales"] for i in items if i.get("monthly_sales") is not None]
    revs = [i["monthly_revenue"] for i in items if i.get("monthly_revenue")]

    total_sales = sum(sales)
    total_rev = sum(revs)
    return {
        "item_count": len(items),
        "total_monthly_sales": total_sales,
        "total_monthly_revenue": round(total_rev, 2),
        "avg_price": round(statistics.mean(prices), 2) if prices else 0,
        "median_price": round(statistics.median(prices), 2) if prices else 0,
        "price_min": round(min(prices), 2) if prices else 0,
        "price_max": round(max(prices), 2) if prices else 0,
        "avg_monthly_sales_per_item": round(_safe_div(total_sales, len(items)), 1),
        "implied_avg_price": round(_safe_div(total_rev, total_sales), 2),
        "rev_share_top10": 0.0,  # 由 concentration 回填
    }


# ---------------------------------------------------------------------------
# 2. 集中度（品牌 / 卖家）
# ---------------------------------------------------------------------------
def _concentration(items: list[dict], key: str, top_n: int = 10) -> dict:
    """按 key 聚合销售额与销量，算 CR-n 与 HHI。"""
    agg: dict[str, dict[str, float]] = {}
    for i in items:
        name = (i.get(key) or "").strip() or "（未填写）"
        slot = agg.setdefault(name, {"revenue": 0.0, "sales": 0.0, "items": 0})
        slot["revenue"] += i.get("monthly_revenue") or 0.0
        slot["sales"] += i.get("monthly_sales") or 0.0
        slot["items"] += 1

    total_rev = sum(v["revenue"] for v in agg.values())
    total_sales = sum(v["sales"] for v in agg.values())
    total_items = sum(v["items"] for v in agg.values())

    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["revenue"])
    cr = _safe_div(sum(v["revenue"] for _, v in ranked[:top_n]), total_rev)

    # HHI：各主体销售额占比平方和 × 10000（0–10000，越高越集中）
    hhi = round(sum((_safe_div(v["revenue"], total_rev) ** 2) for _, v in ranked) * 10000, 1)

    # ⚠️ 主体数 ≤ top_n 时 CR-n 恒为 100%，无参考意义 → 给 CR3 与明确提示
    cr_note = None
    cr3 = round(_safe_div(sum(v["revenue"] for _, v in ranked[:3]), total_rev), 4)
    if len(agg) <= top_n:
        cr_note = (
            f"主体数（{len(agg)}）≤ n（{top_n}），CR{top_n} 恒为 100%，无参考意义。"
            f"请改用 CR3（{cr3 * 100:.1f}%）与 HHI（{hhi}）判断集中度。"
        )

    top_list = [
        {
            "rank": idx + 1,
            "name": name,
            "revenue": round(v["revenue"], 2),
            "revenue_share": round(_safe_div(v["revenue"], total_rev), 4),
            "sales": int(v["sales"]),
            "sales_share": round(_safe_div(v["sales"], total_sales), 4),
            "item_count": int(v["items"]),
            "item_share": round(_safe_div(v["items"], total_items), 4),
        }
        for idx, (name, v) in enumerate(ranked[:top_n])
    ]

    return {
        "subject_count": len(agg),
        f"cr{top_n}": round(cr, 4),
        f"cr{top_n}_by_sales": round(
            _safe_div(sum(v["sales"] for _, v in ranked[:top_n]), total_sales), 4
        ),
        "cr3": cr3,
        "hhi": hhi,
        "cr_note": cr_note,
        "top_list": top_list,
    }


# ---------------------------------------------------------------------------
# 3. 价格带分箱 + 波谷检测
# ---------------------------------------------------------------------------
def price_bands(items: list[dict], n_bins: int = 10, valley_min_drop: float = 0.15,
                max_bands: int = 5) -> dict:
    """
    SOP 1.4：横坐标价格区间、纵坐标销量，从波谷到波谷划分价格带。

    实现：等宽分箱 → 统计销量 → **在原始曲线上**检局部最小值（波谷）→ 按波谷切分价格带。

    两个关键参数：
      valley_min_drop  波谷相对两侧邻居的最小跌幅，低于此值视为噪声（默认 0.15 = 15%）
      max_bands        最多保留的价格带数，超出的按跌幅取前几名（默认 5）

    注意：**不在默认流程里做平滑**。3 点滑动平均会把陡峭的局部波谷抹平
    （例如 799 → 618 → 1120 的波谷会被平均掉），反而漏掉真实的价格带边界。
    需要平滑时用 --smooth。
    """
    pts = [(i["price"], i.get("monthly_sales") or 0, i.get("monthly_revenue") or 0)
           for i in items if i.get("price")]
    if not pts:
        return {"bins": [], "valleys": [], "suggested_bands": []}

    prices = sorted(p for p, _, _ in pts)
    lo, hi = prices[0], prices[-1]
    if hi <= lo:
        return {"bins": [], "valleys": [], "suggested_bands": [], "note": "价格无差异，无法分箱"}

    width = (hi - lo) / n_bins
    bins = []
    for k in range(n_bins):
        b_lo = lo + k * width
        b_hi = b_lo + width if k < n_bins - 1 else hi + 1e-9
        sel = [(p, s, r) for p, s, r in pts if b_lo <= p < b_hi]
        bins.append({
            "index": k + 1,
            "price_lo": round(b_lo, 2),
            "price_hi": round(b_hi, 2),
            "item_count": len(sel),
            "total_sales": int(sum(s for _, s, _ in sel)),
            "total_revenue": round(sum(r for _, _, r in sel), 2),
            "avg_sales_per_item": round(_safe_div(sum(s for _, s, _ in sel), len(sel)), 1),
        })

    raw = [b["total_sales"] for b in bins]

    # 波谷检测：局部最小值 + 相对跌幅显著性过滤
    candidates = []
    for i in range(1, len(raw) - 1):
        if raw[i] < raw[i - 1] and raw[i] < raw[i + 1]:
            drops = []
            if raw[i - 1]:
                drops.append((raw[i - 1] - raw[i]) / raw[i - 1])
            if raw[i + 1]:
                drops.append((raw[i + 1] - raw[i]) / raw[i + 1])
            drop = min(drops) if drops else 0.0
            # 右邻居是上升趋势的第一格时，右侧跌幅天然巨大 → 用左侧跌幅作为主判据
            candidates.append({"bin_index": i + 1, "price": round(bins[i]["price_hi"], 2),
                               "drop": round(drop, 4)})

    significant = [c for c in candidates if c["drop"] >= valley_min_drop]
    noise_count = len(candidates) - len(significant)

    # 波谷过多时，按跌幅取最深的 (max_bands - 1) 个
    trimmed = []
    if len(significant) > max_bands - 1:
        ordered = sorted(significant, key=lambda c: -c["drop"])
        keep = {c["bin_index"] for c in ordered[: max_bands - 1]}
        trimmed = [c for c in significant if c["bin_index"] not in keep]
        significant = [c for c in significant if c["bin_index"] in keep]

    significant.sort(key=lambda c: c["bin_index"])

    boundaries = [lo] + [v["price"] for v in significant] + [hi]
    bands = []
    for k in range(len(boundaries) - 1):
        a, b = boundaries[k], boundaries[k + 1]
        sel = [(p, s, r) for p, s, r in pts if a <= p <= b]
        bands.append({
            "band_index": k + 1,
            "price_from": round(a, 2),
            "price_to": round(b, 2),
            "item_count": len(sel),
            "total_sales": int(sum(s for _, s, _ in sel)),
            "total_revenue": round(sum(r for _, _, r in sel), 2),
            "avg_sales_per_item": round(_safe_div(sum(s for _, s, _ in sel), len(sel)), 1),
        })

    return {
        "price_range": [round(lo, 2), round(hi, 2)],
        "bin_width": round(width, 2),
        "bins": bins,
        "valleys": significant,
        "noise_valleys": noise_count,
        "trimmed_valleys": trimmed,
        "valley_min_drop": valley_min_drop,
        "suggested_bands": bands,
    }


# ---------------------------------------------------------------------------
# 4. 配置组合交叉表
# ---------------------------------------------------------------------------
def _extract_dim(item: dict, field: str) -> str | None:
    """从 title 或字段里抽取配置值。字段不存在时尝试从标题正则抽取。"""
    if item.get(field):
        return str(item[field]).strip()

    title = item.get("title") or ""
    # 数字 + 常见单位
    if re.search(r"(?i)(power|watt|potência|potencia)", field) or field == "功率":
        m = re.search(r"(\d{2,4})\s*(?:w|W|瓦)", title)
        if m:
            return f"{m.group(1)}W"
    if re.search(r"(?i)(weight|peso|承重|carga)", field) or field == "承重":
        m = re.search(r"(\d{2,3})\s*(?:kg|KG|公斤)", title)
        if m:
            return f"{m.group(1)}kg"
    if re.search(r"(?i)(capacit|容量|容量|ah|mah|litro|L\b)", field) or field == "容量":
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:mah|mAh|Ah|ah|L|L\b|litros)", title)
        if m:
            return m.group(0)
    return None


def config_matrix(items: list[dict], fields: list[str]) -> dict:
    """
    SOP 1.5：每个配置维度统计「商品数量 / 数量占比 / 平均销量 / 总销量」
    并给出四维判断：
      - 总销量高 → 需求大
      - 平均销量高 → 单品效率高
      - 数量占比高 → 主流（但竞争可能更激烈）
      - 数量少但平均销量高 → 潜在机会配置
    """
    out: dict[str, Any] = {}
    total_items = len(items)

    for field in fields:
        agg: dict[str, dict[str, float]] = {}
        unparsed = 0
        for i in items:
            val = _extract_dim(i, field)
            if not val:
                unparsed += 1
                continue
            slot = agg.setdefault(val, {"items": 0, "sales": 0.0, "revenue": 0.0})
            slot["items"] += 1
            slot["sales"] += i.get("monthly_sales") or 0
            slot["revenue"] += i.get("monthly_revenue") or 0

        if not agg:
            out[field] = {"error": "该维度未解析出任何值。请把配置写在单独列里，或用 --mapping 指定。",
                          "unparsed": unparsed}
            continue

        total_sales = sum(v["sales"] for v in agg.values())
        rows = []
        for val, v in agg.items():
            rows.append({
                "value": val,
                "item_count": int(v["items"]),
                "item_share": round(_safe_div(v["items"], total_items), 4),
                "total_sales": int(v["sales"]),
                "sales_share": round(_safe_div(v["sales"], total_sales), 4),
                "avg_sales": round(_safe_div(v["sales"], v["items"]), 1),
            })
        rows.sort(key=lambda r: -r["total_sales"])

        n_values = len(rows)
        avg_sales_all = _safe_div(total_sales, sum(r["item_count"] for r in rows))
        # 均分基准：若某取值占 1/n，说明各取值"势均力敌"
        equal_share = 1.0 / n_values if n_values else 1.0

        for r in rows:
            flags = []
            # ① 总销量高 → 市场需求大
            if r["sales_share"] >= 0.30:
                flags.append("总销量高→需求大")
            # ② 单品效率高：平均销量显著高于整体
            if avg_sales_all and r["avg_sales"] > avg_sales_all * 1.2:
                flags.append("单品效率高")
            # ③ 主流配置：数量占比显著高于均分（→ 竞争更激烈）
            if r["item_share"] >= equal_share * 1.5:
                flags.append("主流配置→竞争激烈")
            # ④ 潜在机会配置：数量占比显著低于均分，但单品效率高
            if r["item_share"] <= equal_share * 0.6 and avg_sales_all \
                    and r["avg_sales"] > avg_sales_all * 1.2:
                flags.append("★机会配置（数量少但效率高）")
            # ⑤ 市场大但竞争激烈：总销量高而单品效率低
            if r["sales_share"] >= 0.30 and avg_sales_all and r["avg_sales"] < avg_sales_all:
                flags.append("市场大但竞争激烈")
            r["judgement"] = flags or ["常规"]

        out[field] = {
            "value_count": n_values,
            "unparsed_items": unparsed,
            "avg_sales_all_values": round(avg_sales_all, 1),
            "equal_share_reference": round(equal_share, 4),
            "rows": rows,
        }

    return out


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------
def render_md(payload: dict) -> str:
    cap = payload["market_capacity"]
    L: list[str] = []
    L.append("# 类目指标计算报告\n")
    L.append(f"数据来源：`{payload['meta'].get('source_file', '—')}` ｜ "
             f"样本量：{cap['item_count']} 个商品\n")

    L.append("## 1. 市场容量\n")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 样本商品数 | {cap['item_count']} |")
    L.append(f"| 月总销量 | {cap['total_monthly_sales']:,} |")
    L.append(f"| 月总销售额 | R$ {_fmt_money(cap['total_monthly_revenue'])} |")
    L.append(f"| 均价 | R$ {_fmt_money(cap['avg_price'])} |")
    L.append(f"| 中位价 | R$ {_fmt_money(cap['median_price'])} |")
    L.append(f"| 价格范围 | R$ {_fmt_money(cap['price_min'])} – R$ {_fmt_money(cap['price_max'])} |")
    L.append(f"| 单商品平均月销量 | {cap['avg_monthly_sales_per_item']} |")
    L.append(f"| 隐含均价（GMV÷销量） | R$ {_fmt_money(cap['implied_avg_price'])} |")
    L.append(f"| 头部 10 名销售额占比 | {_fmt_pct(cap.get('rev_share_top10', 0))} |")
    L.append("")

    br = payload.get("brand_concentration")
    if br:
        L.append("## 2. 品牌集中度\n")
        L.append(f"- 品牌数：**{br['subject_count']}**")
        L.append(f"- **CR3（销售额）：{_fmt_pct(br['cr3'])}** ｜ CR10：{_fmt_pct(br['cr10'])}"
                 f" ｜ CR10 按销量：{_fmt_pct(br['cr10_by_sales'])}")
        L.append(f"- HHI：**{br['hhi']}**（0–10000，越高越集中）")
        if br.get("cr_note"):
            L.append(f"\n> ⚠️ {br['cr_note']}")
        L.append("")
        L.append("| # | 品牌 | 销售额 | 销售额占比 | 销量占比 | 商品数 |")
        L.append("|---|---|---|---|---|---|")
        for r in br["top_list"][:10]:
            L.append(f"| {r['rank']} | {r['name']} | R$ {_fmt_money(r['revenue'])} | "
                     f"{_fmt_pct(r['revenue_share'])} | {_fmt_pct(r['sales_share'])} | {r['item_count']} |")
        L.append("")
        L.append("> **判断**（SOP 1.3）：品牌集中度 < 50% → 白牌仍有机会；> 50% → 头部品牌影响强，需谨慎。\n")

    sl = payload.get("seller_concentration")
    if sl:
        L.append("## 3. 卖家集中度\n")
        L.append(f"- 卖家数：**{sl['subject_count']}**")
        L.append(f"- **CR3（销售额）：{_fmt_pct(sl['cr3'])}** ｜ "
                 f"**CR10（销售额）：{_fmt_pct(sl['cr10'])}** ｜ CR10 按销量：{_fmt_pct(sl['cr10_by_sales'])}")
        L.append(f"- HHI：**{sl['hhi']}**")
        if sl.get("cr_note"):
            L.append(f"\n> ⚠️ {sl['cr_note']}")
        L.append("")
        L.append("| # | 卖家 | 销售额占比 | 销量占比 | 商品数 |")
        L.append("|---|---|---|---|---|")
        for r in sl["top_list"][:10]:
            L.append(f"| {r['rank']} | {r['name']} | {_fmt_pct(r['revenue_share'])} | "
                     f"{_fmt_pct(r['sales_share'])} | {r['item_count']} |")
        L.append("")

    pb = payload.get("price_bands")
    if pb and pb.get("bins"):
        L.append("## 4. 价格带分析\n")
        L.append(f"- 价格范围：R$ {_fmt_money(pb['price_range'][0])} – R$ {_fmt_money(pb['price_range'][1])}"
                 f" ｜ 箱宽：R$ {_fmt_money(pb['bin_width'])}")
        L.append("")
        L.append("### 4.1 等宽分箱（销量曲线）\n")
        L.append("| 区间 | 价格段 | 商品数 | 总销量 | 曲线 | 总销售额 | 单商品均销 |")
        L.append("|---|---|---|---|---|---|---|")
        peak = max((b["total_sales"] for b in pb["bins"]), default=0)
        for b in pb["bins"]:
            bar = "█" * int(_safe_div(b["total_sales"], peak) * 20) if peak else ""
            L.append(f"| {b['index']} | R$ {_fmt_money(b['price_lo'])}–{_fmt_money(b['price_hi'])} | "
                     f"{b['item_count']} | {b['total_sales']:,} | {bar} | "
                     f"R$ {_fmt_money(b['total_revenue'])} | {b['avg_sales_per_item']} |")
        L.append("")
        L.append("### 4.2 波谷检测\n")
        if pb.get("valleys"):
            L.append(f"判定阈值：波谷相对两侧邻居的最小跌幅 **≥ {_fmt_pct(pb.get('valley_min_drop', 0))}**"
                     f"（低于此值视为噪声）\n")
            L.append("| 箱序 | 价格边界 | 跌幅 | 作为价格带边界 |")
            L.append("|---|---|---|---|")
            for v in pb["valleys"]:
                L.append(f"| {v['bin_index']} | R$ {_fmt_money(v['price'])} | {_fmt_pct(v['drop'])} | ✅ |")
            L.append("")
            if pb.get("noise_valleys"):
                L.append(f"> 另有 {pb['noise_valleys']} 个跌幅不足阈值的候选波谷被过滤为噪声。\n")
            if pb.get("trimmed_valleys"):
                names = "、".join(f"箱{v['bin_index']}(R$ {_fmt_money(v['price'])})"
                                for v in pb["trimmed_valleys"])
                L.append(f"> 因超出最多价格带数（{pb.get('max_bands', 5)}）被裁掉：{names}。"
                         f"如需保留请调大 `--max-bands`。\n")
        else:
            L.append("未检测到显著波谷。可能原因：样本量偏小（建议 ≥ 50）、"
                     "价格分布过于连续、或销量曲线单调。\n")
        if pb.get("suggested_bands"):
            L.append("### 4.3 建议价格带（按波谷切分）\n")
            L.append("| 价格带 | 区间 | 商品数 | 总销量 | 总销售额 | 单商品均销 |")
            L.append("|---|---|---|---|---|---|")
            for b in pb["suggested_bands"]:
                L.append(f"| #{b['band_index']} | R$ {_fmt_money(b['price_from'])}–{_fmt_money(b['price_to'])} | "
                         f"{b['item_count']} | {b['total_sales']:,} | "
                         f"R$ {_fmt_money(b['total_revenue'])} | {b['avg_sales_per_item']} |")
            L.append("")
            L.append("> ⚠️ 波谷切分只是**数学建议**。SOP 1.4 要求最终判断还要叠加："
                     "公司产品有没有利润、竞品是否特别强。这一步由 Agent 结合成本结构完成。\n")

    cm = payload.get("config_matrix")
    if cm:
        L.append("## 5. 配置组合交叉表\n")
        for field, data in cm.items():
            L.append(f"### 维度：{field}\n")
            if "error" in data:
                L.append(f"⚠️ {data['error']}\n")
                continue
            L.append(f"- 取值数：{data['value_count']} ｜ 整体平均单品销量：{data['avg_sales_all_values']}"
                     f" ｜ 均分参考（1/n）：{_fmt_pct(data.get('equal_share_reference', 0))}")
            if data.get("unparsed_items"):
                L.append(f"- ⚠️ 未能解析该维度的商品：{data['unparsed_items']} 个")
            L.append("")
            L.append("| 配置值 | 商品数 | 数量占比 | 总销量 | 销量占比 | 平均销量 | 判断 |")
            L.append("|---|---|---|---|---|---|---|")
            for r in data["rows"]:
                L.append(f"| {r['value']} | {r['item_count']} | {_fmt_pct(r['item_share'])} | "
                         f"{r['total_sales']:,} | {_fmt_pct(r['sales_share'])} | {r['avg_sales']} | "
                         f"{'；'.join(r['judgement'])} |")
            L.append("")
        L.append("> **判断规则**（SOP 1.5）：总销量高 = 需求大（销量占比 ≥30%）；"
                 "平均销量高 = 单品效率高（高于整体均值 1.2 倍）；"
                 "数量占比高 = 主流但竞争激烈（高于均分 1.5 倍）；"
                 "**数量少但平均销量高 = 潜在机会配置**（低于均分 0.6 倍且效率高，需再确认是否被头部占据）；"
                 "总销量高但平均销量低 = 市场大但竞争激烈。\n")

    return "\n".join(L)


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="类目指标计算（集中度 / 价格带 / 配置交叉表）")
    ap.add_argument("input", help="shaduola_parser.py 产出的 normalized.json")
    ap.add_argument("--bins", type=int, default=10, help="价格分箱数（默认 10）")
    ap.add_argument("--top", type=int, default=10, help="CR-n 与 Top 榜单长度（默认 10）")
    ap.add_argument("--config-fields", default="", help="配置维度字段，逗号分隔，如 功率,承重")
    ap.add_argument("--valley-min-drop", type=float, default=0.15,
                    help="波谷相对两侧邻居的最小跌幅，低于此值视为噪声（默认 0.15）")
    ap.add_argument("--max-bands", type=int, default=5, help="最多保留的价格带数（默认 5）")
    ap.add_argument("--brand-field", default="brand", help="品牌字段名（默认 brand）")
    ap.add_argument("--seller-field", default="seller", help="卖家字段名（默认 seller）")
    ap.add_argument("--format", choices=["md", "json", "both"], default="both")
    ap.add_argument("-o", "--output", help="输出文件前缀（默认 <输入名>.metrics）")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items", [])
    if not items:
        sys.exit("[ERROR] 输入 JSON 里没有 items。请先用 shaduola_parser.py 转换。")

    cap = market_capacity(items)
    brand = _concentration(items, args.brand_field, args.top)
    seller = _concentration(items, args.seller_field, args.top)
    cap["rev_share_top10"] = seller[f"cr{args.top}"]

    pb = price_bands(items, args.bins, args.valley_min_drop, args.max_bands)
    pb["max_bands"] = args.max_bands

    payload: dict[str, Any] = {
        "meta": data.get("meta", {}),
        "market_capacity": cap,
        "brand_concentration": brand,
        "seller_concentration": seller,
        "price_bands": pb,
    }

    fields = [f.strip() for f in args.config_fields.split(",") if f.strip()]
    if fields:
        payload["config_matrix"] = config_matrix(items, fields)

    prefix = args.output or (args.input.rsplit(".", 1)[0] + ".metrics")
    if args.format in ("json", "both"):
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"✅ JSON → {prefix}.json")
    if args.format in ("md", "both"):
        md = render_md(payload)
        with open(prefix + ".md", "w", encoding="utf-8") as f:
            f.write(md)
        print(f"✅ Markdown → {prefix}.md")
        print()
        print(md)

    print("\n" + "=" * 60)
    print("给 Agent 的输入：把上面这份指标报告（JSON 或 MD）连同 "
          "templates/input_category_data.md 一起提交。")
    print("Agent 只负责解读，不负责重算。若数字有疑问，回来改脚本参数。")


if __name__ == "__main__":
    main()
