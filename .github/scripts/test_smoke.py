"""Run the Agent command-line tools against the repository's fixtures."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


AGENTS = Path(__file__).resolve().parents[2] / "Agents"


class AgentSmokeTests(unittest.TestCase):
    def run_cli(self, agent, script, *args, expected_exit=0):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", f"scripts/{script}", *args],
            cwd=AGENTS / agent,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            expected_exit,
            f"{agent}/{script}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result.stdout

    def test_market_research(self):
        agent = "01_Market_Research_Agent"
        with tempfile.TemporaryDirectory() as directory:
            normalized = Path(directory) / "normalized.json"
            metrics = Path(directory) / "metrics"
            self.run_cli(
                agent, "shaduola_parser.py", "samples/sample_patinete_eletrico.csv",
                "-o", str(normalized),
            )
            self.run_cli(
                agent, "market_metrics.py", str(normalized), "--bins", "10",
                "--config-fields", "功率,承重", "--format", "json", "-o", str(metrics),
            )
            report = json.loads(metrics.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["meta"]["row_count_kept"], 50)
        self.assertEqual(len(report["price_bands"]["valleys"]), 2)
        self.assertEqual(len(report["price_bands"]["suggested_bands"]), 3)

    def test_product_strategy(self):
        report = json.loads(self.run_cli(
            "02_Product_Strategy_Agent", "sku_pricing.py", "matrix",
            "--file", "examples/sample-skus.csv", "--platform", "shopee_br", "--json",
        ))
        self.assertEqual(len(report["skus"]), 4)
        main_sku = next(sku for sku in report["skus"] if sku["sku"] == "主推款")
        self.assertGreater(main_sku["coupon_headroom"], 0)
        self.assertLess(main_sku["break_even_price"], main_sku["price"])

    def test_listing_creative(self):
        agent = "03_Listing_Creative_Agent"
        title = "Carregador Portátil Power Bank 20000mAh 45W"
        lint = json.loads(self.run_cli(
            agent, "title_lint.py", title, "--platform", "shopee", "--json",
        ))
        self.assertTrue(lint["ok"])
        self.assertEqual(lint["chars"], len(title))

        report = json.loads(self.run_cli(
            agent, "spec_consistency.py", "--spec", "examples/spec_table.csv",
            "--title", title, "--json",
        ))
        self.assertEqual(report["numbers_in_text"], 2)
        self.assertEqual(report["untraceable"], [])

    def test_product_image(self):
        agent = "04_Product_Image_Agent"
        passing = json.loads(self.run_cli(
            agent, "check_main_image.py", "tests/ml_pass.png",
            "--platform", "mercadolivre", "--json",
        ))
        self.assertTrue(passing["ok"])

        failing = json.loads(self.run_cli(
            agent, "check_main_image.py", "tests/amazon_fail.png",
            "--platform", "amazon", "--json", expected_exit=1,
        ))
        self.assertFalse(failing["ok"])
        self.assertIn(["FAIL", "coverage"], [check[:2] for check in failing["checks"]])

    def test_product_video(self):
        report = json.loads(self.run_cli(
            "05_Product_Video_Agent", "video_timing.py",
            "--script", "examples/narration_airfryer.txt", "--target", "15", "--format", "json",
        ))
        self.assertEqual(report["meta"]["total_seconds"], 26.0)
        self.assertFalse(report["target_check"]["pass"])

    def test_advertising_optimization(self):
        report = json.loads(self.run_cli(
            "06_Advertising_Optimization_Agent", "ad_metrics.py", "diagnose",
            "--file", "examples/sample-ad-report.csv", "--price", "113.40",
            "--product-cost", "55", "--platform", "shopee_br", "--json",
        ))
        self.assertEqual(report["total"]["campaigns"], 4)
        self.assertAlmostEqual(report["total"]["roas"], 1.84, places=2)
        self.assertGreater(report["cost_model"]["break_even_roas"], report["total"]["roas"])

    def test_review_analysis(self):
        report = json.loads(self.run_cli(
            "07_Review_Analysis_Agent", "review_stats.py",
            "--file", "examples/sample-reviews.csv", "--points", "examples/sample-selling-points.csv",
            "--format", "json",
        ))
        self.assertEqual(report["reviews"]["total"], 128)
        self.assertEqual(report["reviews"]["fixable_count"], 11)
        self.assertEqual(report["selling_points"]["rows"][0]["score"], 114)


if __name__ == "__main__":
    unittest.main(verbosity=2)
