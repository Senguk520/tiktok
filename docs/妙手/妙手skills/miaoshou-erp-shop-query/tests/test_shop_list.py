import sys
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import shop_list


class FakeClient:
    def __init__(self):
        self.calls = []

    def get_shop_list(self, platform, site, page, size, silent=False):
        self.calls.append((platform, site, page, size))
        if site == "MX":
            return {"result": "fail", "code": "denied", "message": "no permission"}
        return {"result": "success", "data": {"shopList": []}}


class ShopListTests(unittest.TestCase):
    def test_tiktok_site_registry_matches_current_api_reference(self):
        self.assertEqual(shop_list.PLATFORM_SITES["tiktok"], ["ID", "VN", "TH", "MY", "PH", "BR", "MX", "ES", "FR", "GB", "US", "DE", "IT", "JP"])

    def test_ozon_uses_endpoint_selector_instead_of_country_code(self):
        self.assertEqual(shop_list.PLATFORM_SITES["ozon"], ["OZON"])
        self.assertEqual(shop_list.validate_site("ozon", "ozon"), "OZON")
        with self.assertRaisesRegex(ValueError, "expected one of: OZON"):
            shop_list.validate_site("ozon", "RU")

    def test_ozon_platform_scan_queries_only_ozon_selector(self):
        client = FakeClient()
        shop_list.cmd_list_all(client, Namespace(platform="ozon"))
        self.assertEqual(client.calls, [("ozon", "OZON", 1, 100)])

    def test_platform_all_scans_only_requested_platform_and_reports_failures(self):
        client = FakeClient()
        result = shop_list.cmd_list_all(client, Namespace(platform="tiktok"))
        self.assertEqual({call[0] for call in client.calls}, {"tiktok"})
        self.assertEqual({call[1] for call in client.calls}, set(shop_list.PLATFORM_SITES["tiktok"]))
        self.assertTrue(any(item["site"] == "MX" for item in result["failures"]))


if __name__ == "__main__":
    unittest.main()
