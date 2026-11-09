#!/usr/bin/env python3
"""SKU 定价算术脚本 —— 只算，不猜，不定价（定价决策由人）。

  # 平台预设（推荐）：费率来自 scripts/platform_fees.json
  python sku_pricing.py calc --platform shopee_br --cost 935 --price 2199
  python sku_pricing.py calc --platform mercadolivre_br --cost 935 --price 2199
  python sku_pricing.py calc --platform amazon_br --cost 935 --price 2199
  python sku_pricing.py calc --platform tiktok_br --cost 935 --price 2199
  python sku_pricing.py --list-platforms

  # 手工口径（兼容旧用法，与预设等价的自定义费率）
  python sku_pricing.py calc --cost 935 --price 2199 --commission 14 --return 2 \
      --ad 5 --tax 0 --target-margin 32
  python sku_pricing.py matrix --file examples/sample-skus.csv --platform shopee_br

公式（含巴西税费项与平台按件固定费）：
  广告前毛利 = 售价 − 到岸成本 − 售价×(佣金%+退货%+税%+平台附加%) − 按件固定费
  净利率     = (广告前毛利 − 售价×广告%) ÷ 售价
  盈亏平衡价 / 目标价：费率随价格分档时用不动点迭代求（12 轮收敛）
"""
import argparse
import csv
import json
import sys
from pathlib import Path

PLATFORMS_FILE = Path(__file__).with_name("platform_fees.json")

# 未指定 --platform 时保持的历史默认（= 旧版行为，fixed fee 为 0）
LEGACY = dict(commission=14.0, return_rate=2.0, ad=5.0, tax=0.0,
              extra_pct=0.0, target_margin=32.0)


def load_platforms() -> dict:
    try:
        with open(PLATFORMS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        sys.exit(f"[ERROR] 读不到平台费率表 {PLATFORMS_FILE.name}：{e}")


def _tier_lookup(price: float, tiers: list, default: float) -> float:
    """tiers: [{"max": 79.99, "fee"/"pct": v}, ..., {"max": null, ...}]，按序首个命中。"""
    for t in tiers or []:
        mx = t.get("max")
        if mx is None or price <= float(mx):
            v = t.get("pct", t.get("fee", default))
            return float(v)
    return default


class FeeModel:
    """费率模型：显式 CLI 参数 > 平台预设 > 历史默认。佣金/固定费可随价格分档。"""

    def __init__(self, preset: dict | None = None, *, commission=None,
                 return_rate=None, ad=None, tax=None, target_margin=None,
                 extra_pct=None, fixed_fee=None):
        self.p = preset or {}
        self.o = dict(commission=commission, return_rate=return_rate, ad=ad,
                      tax=tax, target_margin=target_margin,
                      extra_pct=extra_pct, fixed_fee=fixed_fee)

    def _scalar(self, key: str, preset_key: str) -> float:
        if self.o[key] is not None:
            return float(self.o[key])
        if preset_key in self.p:
            return float(self.p[preset_key])
        return LEGACY[key]

    def commission_pct(self, price: float) -> float:
        if self.o["commission"] is not None:
            return float(self.o["commission"])
        if self.p.get("commission_tiers"):
            return _tier_lookup(price, self.p["commission_tiers"],
                                self.p.get("commission_pct", LEGACY["commission"]))
        return float(self.p.get("commission_pct", LEGACY["commission"]))

    def fixed_fee(self, price: float) -> float:
        if self.o["fixed_fee"] is not None:
            return float(self.o["fixed_fee"])
        if self.p.get("fixed_fee_tiers"):
            return _tier_lookup(price, self.p["fixed_fee_tiers"], 0.0)
        return 0.0

    def return_pct(self) -> float: return self._scalar("return_rate", "return_pct")
    def tax_pct(self) -> float:    return self._scalar("tax", "tax_pct")
    def extra_pct(self) -> float:  return self._scalar("extra_pct", "extra_pct")
    def ad_pct(self) -> float:     return self._scalar("ad", "ad_pct")
    def target_margin(self) -> float: return self._scalar("target_margin", "target_margin_pct")

    def margin(self, price: float, cost: float, ad_pct: float | None = None) -> dict:
        ad = self.ad_pct() if ad_pct is None else ad_pct
        cp = self.commission_pct(price)
        ff = self.fixed_fee(price)
        pct = cp + self.return_pct() + self.tax_pct() + self.extra_pct()
        pre = price - cost - price * pct / 100 - ff
        net = pre - price * ad / 100
        return dict(pre_ad_margin=round(pre, 2),
                    pre_ad_rate=round(pre / price, 4) if price else 0,
                    net_margin=round(net, 2),
                    net_rate=round(net / price, 4) if price else 0,
                    commission_pct_applied=cp,
                    fixed_fee_applied=round(ff, 2))

    def _solve(self, cost: float, ad_pct: float, tm_pct: float) -> float | None:
        """盈亏/目标价：p = (cost + fixed_fee(p)) / (1 − Σpct(p)/100)，不动点迭代。"""
        p = max(cost, 1.0)
        for _ in range(12):
            pct = (self.commission_pct(p) + self.return_pct() + self.tax_pct()
                   + self.extra_pct() + ad_pct + tm_pct) / 100
            denom = 1 - pct
            if denom <= 0:
                return None
            p = (cost + self.fixed_fee(p)) / denom
        return round(p, 2)

    def break_even_price(self, cost: float) -> float | None:
        return self._solve(cost, 0.0, 0.0)

    def target_price(self, cost: float) -> float | None:
        return self._solve(cost, self.ad_pct(), self.target_margin())


def _model_from_args(args, plats: dict) -> FeeModel:
    preset = None
    if getattr(args, "platform", None):
        key = args.platform.strip()
        if key not in plats:
            # 允许不带地区后缀（如 --platform mercadolivre → mercadolivre_br）
            matches = [k for k in plats
                       if not k.startswith("_") and (k == key or k.startswith(key + "_"))]
            if len(matches) == 1:
                key = matches[0]
            else:
                keys = "、".join(k for k in plats if not k.startswith("_"))
                hint = f"（{key!r} 匹配到多个：{'、'.join(matches)}，请写全名）" \
                       if len(matches) > 1 else ""
                sys.exit(f"[INPUT_MISSING] 未知平台 {args.platform!r}。可用：{keys}{hint}")
        preset = plats[key]
    return FeeModel(preset,
                    commission=getattr(args, "commission", None),
                    return_rate=getattr(args, "return_rate", None),
                    ad=getattr(args, "ad", None),
                    tax=getattr(args, "tax", None),
                    target_margin=getattr(args, "target_margin", None),
                    extra_pct=getattr(args, "extra_pct", None),
                    fixed_fee=getattr(args, "fixed_fee", None))


def cmd_calc(args, plats) -> int:
    fm = _model_from_args(args, plats)
    m = fm.margin(args.price, args.cost)
    be = fm.break_even_price(args.cost)
    tp = fm.target_price(args.cost)
    tm = fm.target_margin() / 100
    out = dict(mode="calc", platform=getattr(args, "platform", None) or "custom",
               price=args.price, cost=args.cost, **m,
               extra_pct_applied=fm.extra_pct(),
               break_even_price=be, target_price=tp,
               target_margin=fm.target_margin(),
               coupon_headroom=round((m["net_rate"] - tm) * args.price, 2)
               if m["net_rate"] > tm else 0,
               break_even_roas=round(args.price / m["pre_ad_margin"], 2)
               if m["pre_ad_margin"] > 0 else None)
    if fm.ad_pct() > 0 and m["pre_ad_margin"] > 0:
        out["break_even_roas_at_this_ad"] = round(
            args.price / (args.price * fm.ad_pct() / 100), 2)
    # 平台月费/其它固定成本仅提示（不摊入单件）
    if fm.p.get("monthly_fee"):
        out["platform_monthly_fee"] = fm.p["monthly_fee"]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not getattr(args, "json", False):
        print(f"\n平台: {out['platform']} ｜ 佣金 {m['commission_pct_applied']}% ｜ "
              f"固定费 R$ {m['fixed_fee_applied']:.2f} ｜ 平台附加 {fm.extra_pct()}% ｜ "
              f"退货 {fm.return_pct()}% ｜ 税 {fm.tax_pct()}% ｜ 广告 {fm.ad_pct()}%")
        print(f"净利率 {m['net_rate']:.1%} ｜ 盈亏价 R$ {be:,.2f} ｜ "
              f"目标价 R$ {tp:,.2f} ｜ 券余量 R$ {out['coupon_headroom']:,.2f}")
        for note in fm.p.get("notes", [])[:3]:
            print(f"  ℹ️  {note}")
    return 0


def cmd_matrix(args, plats) -> int:
    try:
        with open(args.file, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        sys.exit(f"[ERROR] 读不了文件：{e}")
    if not rows:
        sys.exit("[ERROR] CSV 里没有数据行")
    header = list(rows[0].keys())
    low = [h.strip().lower() for h in header]

    def col(*cands):
        for c in cands:
            if c in low:
                return header[low.index(c)]
        return None

    c_name = col("角色", "名称", "sku", "role", "name") or header[0]
    c_price = col("售价", "价格", "price")
    c_cost = col("到岸成本", "成本", "cost")
    if not c_price or not c_cost:
        sys.exit(f"[ERROR] 认不出售价/成本列（表头：{header}）")

    fm = _model_from_args(args, plats)
    tm = fm.target_margin() / 100
    results = []
    for r in rows:
        name = (r.get(c_name) or "?").strip()
        price = _num(r.get(c_price), kind="dec")
        cost = _num(r.get(c_cost), kind="dec")
        if price <= 0 or cost <= 0:
            results.append(dict(sku=name, error="价格或成本非正数，跳过"))
            continue
        m = fm.margin(price, cost)
        headroom = round((m["net_rate"] - tm) * price, 2)
        results.append(dict(
            sku=name, price=price, cost=cost,
            pre_ad_margin=m["pre_ad_margin"], net_rate=m["net_rate"],
            commission_pct_applied=m["commission_pct_applied"],
            fixed_fee_applied=m["fixed_fee_applied"],
            vs_target=round(m["net_rate"] - tm, 4),
            verdict=("达标" if m["net_rate"] >= tm else
                     f"低于目标 {(tm - m['net_rate']) * 100:.1f} 个点"),
            coupon_headroom=max(headroom, 0),
            break_even_price=fm.break_even_price(cost)))

    ladder = []
    sorted_skus = sorted([r for r in results if "price" in r], key=lambda x: x["price"])
    for a, b in zip(sorted_skus, sorted_skus[1:]):
        gap = b["price"] - a["price"]
        ladder.append(dict(frm=a["sku"], to=b["sku"], gap=round(gap, 2),
                           gap_pct=round(gap / a["price"], 4)))

    notes = []
    by_net = sorted([r for r in results if "net_rate" in r], key=lambda x: -x["net_rate"])
    if len(by_net) >= 2 and by_net[0]["price"] < by_net[-1]["price"]:
        notes.append("最高净利率不是最高价 SKU —— 若是引流款策略属正常；若非刻意设计，检查高配成本")
    under = [r["sku"] for r in results if "net_rate" in r and r["net_rate"] < tm]
    if under:
        notes.append(f"低于目标毛利 {fm.target_margin():g}% 的 SKU：{('、'.join(under))} —— 确认是否刻意（引流款可低于目标，但须在盈亏价之上）")

    out = dict(mode="matrix", platform=getattr(args, "platform", None) or "custom",
               target_margin=fm.target_margin(), skus=results,
               price_ladder=ladder, notes=notes)
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if not args.json:
        plat_label = (fm.p.get("label") or out["platform"])
        print(f"\n平台: {plat_label} ｜ 目标毛利 {fm.target_margin():g}% ｜ "
              f"佣金/固定费按价格分档 ｜ 广告 {fm.ad_pct()}% ｜ 退货 {fm.return_pct()}% ｜ "
              f"税 {fm.tax_pct()}% ｜ 平台附加 {fm.extra_pct()}%\n")
        print(f"{'SKU':<30}{'售价':>10}{'佣金%':>7}{'固定费':>8}{'净利率':>8}{'对目标':>9}{'券余量':>9}")
        for r in results:
            if "error" in r:
                print(f"{r['sku']:<30}  ⚠️ {r['error']}")
                continue
            mark = "✅" if r["vs_target"] >= 0 else "⚠️ "
            print(f"{r['sku']:<30}{r['price']:>10,.2f}{r['commission_pct_applied']:>7g}"
                  f"{r['fixed_fee_applied']:>8,.2f}{r['net_rate']:>8.1%}"
                  f"{r['vs_target']:>+9.1%}{r['coupon_headroom']:>9,.2f} {mark}")
        print()
        for l in ladder:
            print(f"  阶梯 {l['frm']} → {l['to']}：差 R$ {l['gap']:,.2f}（{l['gap_pct']:.0%}）")
        for n in notes:
            print(f"  ℹ️  {n}")
        for note in fm.p.get("notes", [])[:2]:
            print(f"  📌 {note}")
    return 0


def _fmt_tiers(tiers: list, key: str, suffix: str = "") -> str:
    """把价格分档格式化为「≤79.99:20%/>79.99:14%」形式。

    max=None 表示「无上限」，其下界取前一档的 max；若首档即无上限（例如
    amazon_br 的固定费恒为 0，只有 [{"max": null, "fee": 0}]），则标注为
    "all"，避免对空列表取 [-1] 抛 IndexError。
    """
    parts = []
    prev = None
    for t in tiers or []:
        mx = t.get("max")
        if mx is not None:
            label = f"≤{mx}"
            prev = mx
        else:
            label = f">{prev}" if prev is not None else "all"
        parts.append(f"{label}:{t[key]}{suffix}")
    return "/".join(parts)


def cmd_list_platforms(plats) -> int:
    print(f"{'key':<28}{'平台':<22}{'佣金%':<12}{'固定费':<26}{'附加%':<7}")
    print("-" * 80)
    for k, v in plats.items():
        if k.startswith("_"):
            continue
        ct = v.get("commission_tiers")
        comm = _fmt_tiers(ct, "pct", "%") if ct else f"{v.get('commission_pct', '-')}%"
        fee = _fmt_tiers(v.get("fixed_fee_tiers"), "fee") or "0"
        print(f"{k:<28}{v.get('label', k):<22}{comm:<12}{fee:<26}{v.get('extra_pct', 0):<7g}")
    print("\n费率为编写时（见 _meta.updated）公开口径，发布前必须到各平台卖家中心复核；")
    print("显式参数（--commission 等）永远优先于预设。")
    return 0


def _num(v, default=0.0, kind="dec"):
    if v is None:
        return default
    s = str(v).strip().replace("R$", "").replace("%", "").replace(" ", "")
    if s in ("", "-", "—", "暂无", "N/A", "nan"):
        return default
    neg = s.startswith("-")
    s = s.lstrip("-")
    if kind == "int":
        digits = "".join(ch for ch in s if ch.isdigit())
        return (-1 if neg else 1) * int(digits) if digits else default
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head}.{tail}" if len(tail) <= 2 else head + tail
    elif "." in s:
        head, _, tail = s.rpartition(".")
        s = s if len(tail) <= 2 else head + tail
    try:
        return (-1 if neg else 1) * float(s)
    except ValueError:
        return default


def _add_common(p):
    p.add_argument("--platform", help="平台预设 key（--list-platforms 查看）；其余费率参数未给出时取预设默认")
    p.add_argument("--commission", type=float, default=None, help="平台佣金 %%（覆盖预设）")
    p.add_argument("--return", dest="return_rate", type=float, default=None, help="退货损耗 %%（覆盖预设）")
    p.add_argument("--ad", type=float, default=None, help="广告预算占售价 %%（覆盖预设）")
    p.add_argument("--tax", type=float, default=None, help="综合有效税率 %%（覆盖预设）")
    p.add_argument("--extra-pct", type=float, default=None, help="平台附加 %%（如免运计划；覆盖预设）")
    p.add_argument("--fixed-fee", type=float, default=None, help="按件固定费 R$（覆盖预设阶梯）")
    p.add_argument("--target-margin", type=float, default=None, help="目标净利 %%（默认 32）")


def main():
    plats = load_platforms()
    ap = argparse.ArgumentParser(description="SKU 定价算术（只算不定价）")
    ap.add_argument("--list-platforms", action="store_true", help="列出全部平台费率预设")
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("calc", help="单 SKU：净利率 / 盈亏价 / 目标价 / 优惠券余量")
    _add_common(c)
    c.add_argument("--cost", type=float, required=True, help="到岸成本 R$")
    c.add_argument("--price", type=float, required=True, help="售价 R$")
    c.add_argument("--json", action="store_true", help="仅输出 JSON")

    m = sub.add_parser("matrix", help="多 SKU 矩阵：读 CSV 出净利率表 + 价格阶梯 + 券余量")
    _add_common(m)
    m.add_argument("--file", required=True)
    m.add_argument("--json", action="store_true")

    args = ap.parse_args()
    if args.list_platforms:
        sys.exit(cmd_list_platforms(plats))
    if not args.cmd:
        ap.print_help()
        sys.exit(2)
    sys.exit(cmd_calc(args, plats) if args.cmd == "calc" else cmd_matrix(args, plats))


if __name__ == "__main__":
    main()
