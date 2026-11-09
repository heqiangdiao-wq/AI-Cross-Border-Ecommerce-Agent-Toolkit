#!/usr/bin/env python3
"""--list-platforms 的回归测试（仅标准库，无第三方依赖）。

从仓库根目录运行：
    python -m unittest discover -s Agents/02_Product_Strategy_Agent/tests -v

背景：platform_fees.json 里 amazon_br 的 fixed_fee_tiers 只有一档
[{"max": null, "fee": 0}]。旧实现用
    [x['max'] for x in ft if x.get('max')][-1]
反推上一档上界，此时列表为空，抛 IndexError，导致命令整体崩溃。
"""
import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location("sku_pricing", SCRIPTS / "sku_pricing.py")
sku_pricing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sku_pricing)


class FormatTiersTest(unittest.TestCase):

    def test_multi_tier_keeps_previous_bound(self):
        tiers = [{"max": 79.99, "pct": 20}, {"max": None, "pct": 14}]
        self.assertEqual(sku_pricing._fmt_tiers(tiers, "pct", "%"),
                         "≤79.99:20%/>79.99:14%")

    def test_single_open_ended_tier_uses_all(self):
        """回归：首档即无上限时不得抛 IndexError。"""
        tiers = [{"max": None, "fee": 0}]
        self.assertEqual(sku_pricing._fmt_tiers(tiers, "fee"), "all:0")

    def test_empty_tiers(self):
        self.assertEqual(sku_pricing._fmt_tiers([], "fee"), "")
        self.assertEqual(sku_pricing._fmt_tiers(None, "fee"), "")


class ListPlatformsTest(unittest.TestCase):

    def test_real_fee_table_renders_all_platforms(self):
        plats = json.loads((SCRIPTS / "platform_fees.json").read_text(encoding="utf-8"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sku_pricing.cmd_list_platforms(plats)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        for key in ("shopee_br", "mercadolivre_br", "mercadolivre_br_premium",
                    "amazon_br", "tiktok_br"):
            self.assertIn(key, out)
        self.assertIn("all:0", out)  # amazon_br 固定费恒为 0


if __name__ == "__main__":
    unittest.main()
