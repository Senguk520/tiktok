"""
Miaoshou ERP (JCOP Open Platform) - Query Authorized Shop List

Usage:
    python shop_list.py list --platform tiktok --site US
    python shop_list.py list --platform ozon --site OZON
    python shop_list.py list --platform shopee --site MY --page 1 --size 50
    python shop_list.py list-all
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load Miaoshou Open Platform credentials from local config or environment."""
    base_dir = Path(__file__).resolve().parent.parent
    config_path = base_dir / "resources" / "config.json"
    config = {}

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

    env_app_key = os.getenv("MIAOSHOU_APP_KEY")
    env_app_secret = os.getenv("MIAOSHOU_APP_SECRET")
    env_base_url = os.getenv("MIAOSHOU_BASE_URL")

    if env_app_key:
        config["app_key"] = env_app_key
    if env_app_secret:
        config["app_secret"] = env_app_secret
    if env_base_url:
        config["base_url"] = env_base_url

    config.setdefault("base_url", "https://openapi-erp.91miaoshou.com")
    config.setdefault("timeout", 30)

    app_key = str(config.get("app_key", "")).strip()
    app_secret = str(config.get("app_secret", "")).strip()
    placeholder_values = {"your_app_key_here", "your_app_secret_here", ""}
    if app_key in placeholder_values or app_secret in placeholder_values:
        print("ERROR: Miaoshou Open Platform credentials are not configured.")
        print(f"Create {config_path} from resources/config.json.example and fill app_key/app_secret,")
        print("or set MIAOSHOU_APP_KEY and MIAOSHOU_APP_SECRET in the environment.")
        print("Use base_url: https://openapi-erp.91miaoshou.com")
        sys.exit(1)

    return config

# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def generate_sign(app_secret: str, path: str, timestamp: int, app_key: str, body_json: str = "") -> str:
    """Generate HmacSHA256 signature for JCOP Open Platform."""
    content = f"{app_secret}{path}{timestamp}{app_key}{body_json}{app_secret}"
    return hmac.new(
        app_secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Common platforms to scan when using list-all.
# Keep API platform-code casing here; some codes such as pddkjChoice are case-sensitive.
PLATFORM_ALIASES = {
    "tk": "tiktok",
    "tt": "tiktok",
    "tiktok": "tiktok",
    "tiktokglobal": "tiktokGlobal",
    "tiktok_global": "tiktokGlobal",
    "ozon": "ozon",
    "temu": "pddkj",
    "pddkj": "pddkj",
    "temu-full": "pddkj",
    "temu-full-service": "pddkj",
    "temu-choice": "pddkjChoice",
    "temu-semi": "pddkjChoice",
    "pddkjchoice": "pddkjChoice",
    "shopee": "shopee",
    "shopeeglobal": "shopeeGlobal",
    "shopee_global": "shopeeGlobal",
    "mercadolibre": "mercadolibre",
    "meli": "mercadolibre",
    "ml": "mercadolibre",
}

PLATFORM_SITES = {
    "tiktok": ["ID", "VN", "TH", "MY", "PH", "BR", "MX", "ES", "FR", "GB", "US", "DE", "IT", "JP"],
    "tiktokGlobal": ["TIKTOKGLOBAL", "TIKTOKGLOBALUS", "TIKTOKGLOBALEU"],
    "ozon": ["OZON"],
    "pddkj": ["PDDKJ"],
    "pddkjChoice": ["PDDKJCHOICE"],
    "shopee": ["MY", "TH", "VN", "PH", "SG", "ID", "TW", "BR", "MX", "CL", "CO", "PL", "ES", "FR", "AR"],
    "shopeeGlobal": ["SHOPEEGLOBAL"],
    "mercadolibre": ["CBT", "UP"],
}

COMMON_PLATFORMS = list(PLATFORM_SITES.keys())

# All site selectors are explicit. In particular, the shop-list endpoint uses
# the literal Ozon selector OZON; RU can return success with an empty shop list.
STRICT_SITE_PLATFORMS = set(PLATFORM_SITES)

PLATFORM_DISPLAY_NAMES = {
    "tiktok": "TikTok Shop",
    "tiktokGlobal": "TikTok Shop Global",
    "ozon": "Ozon",
    "pddkj": "Temu Full Service",
    "pddkjChoice": "Temu Semi-managed",
    "shopee": "Shopee",
    "shopeeGlobal": "Shopee Global",
    "mercadolibre": "MercadoLibre",
}


def normalize_platform(platform: str) -> str:
    key = platform.strip()
    return PLATFORM_ALIASES.get(key.lower(), key)


def validate_site(platform: str, site: str) -> str:
    """Validate and normalize a required site code before making a request."""
    normalized_site = site.strip().upper()
    if not normalized_site:
        raise ValueError("site is required; an empty site does not mean all sites")

    allowed_sites = PLATFORM_SITES.get(platform)
    if platform in STRICT_SITE_PLATFORMS and allowed_sites and normalized_site not in allowed_sites:
        allowed = ", ".join(allowed_sites)
        raise ValueError(f"unsupported site '{site}' for platform '{platform}'; expected one of: {allowed}")
    return normalized_site


def truthy_flag(value) -> bool:
    return value in {1, "1", True, "true", "TRUE", "Y", "y", "yes", "YES"}


def enrich_shop_flags(shop: dict) -> dict:
    """Attach normalized CB/CNSC fields used by downstream publish skills."""
    is_cb = truthy_flag(shop.get("isCb") if "isCb" in shop else shop.get("CB"))
    is_cnsc = truthy_flag(shop.get("isCnsc") if "isCnsc" in shop else shop.get("CNSC"))
    shop["CB"] = "Y" if is_cb else "N"
    shop["CNSC"] = "Y" if is_cnsc else "N"
    shop["shopType"] = "global_child" if is_cb and is_cnsc else "local"
    return shop


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class ShopListClient:
    def __init__(self, config: dict):
        self.base_url = config["base_url"].rstrip("/")
        self.app_key = config["app_key"]
        self.app_secret = config["app_secret"]
        self.timeout = config.get("timeout", 30)

    def _post(self, path: str, body: dict, silent: bool = False) -> dict:
        """Send a signed POST request to the API."""
        if requests is None:
            raise RuntimeError("'requests' package is required for API calls; install it with: pip install requests")

        timestamp = int(time.time())
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        sign = generate_sign(self.app_secret, path, timestamp, self.app_key, body_json)

        headers = {
            "Content-Type": "application/json",
            "x-app-key": self.app_key,
            "x-timestamp": str(timestamp),
            "x-sign": sign,
        }

        url = f"{self.base_url}{path}"

        resp = requests.post(url, headers=headers, data=body_json.encode("utf-8"), timeout=self.timeout)
        resp.raise_for_status()

        if not resp.text or not resp.text.strip():
            if not silent:
                print(f"\nAPI Error: empty response (HTTP {resp.status_code})")
                print("Possible causes: VPN disconnected / JCOP service unavailable / IP not whitelisted")
            return {"result": "fail", "code": "emptyResponse", "message": "Empty response"}

        result = resp.json()

        if result.get("result") != "success" and not silent:
            print(f"\nAPI Error: [{result.get('code')}] {result.get('message', '')}")

        return result

    def get_shop_list(
        self,
        platform: str,
        site: str,
        page: int = 1,
        size: int = 100,
        silent: bool = False,
    ) -> dict:
        """Query authorized shop list by platform and site."""
        site = validate_site(platform, site)
        if page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= size <= 100:
            raise ValueError("size must be between 1 and 100")

        path = "/open/v1/product/shop/shop/get_shop_list"
        body: Dict[str, Any] = {
            "platform": platform,
            "site": site,
        }
        if page:
            body["pageNo"] = page
        if size:
            body["pageSize"] = size
        return self._post(path, body, silent=silent)


# ---------------------------------------------------------------------------
# Output Helpers
# ---------------------------------------------------------------------------

def print_shop_list(data: dict, platform: str):
    """Pretty-print shop list for a single platform."""
    shop_list = data.get("data", {}).get("shopList", [])
    display_name = PLATFORM_DISPLAY_NAMES.get(platform, platform)

    if not shop_list:
        print(f"  {display_name}: (no shops)")
        return []

    print(f"\n{'='*90}")
    print(f"  {display_name} - {len(shop_list)} shop(s)")
    print(f"{'='*90}")
    print(f"  {'#':<4} {'Shop ID':<12} {'Shop Name':<25} {'Site':<8} {'Status':<10} {'CB':<4} {'CNSC':<5} {'Expire':<20}")
    print(f"  {'-'*4} {'-'*12} {'-'*25} {'-'*8} {'-'*10} {'-'*4} {'-'*5} {'-'*20}")

    for i, shop in enumerate(shop_list, 1):
        shop_id = shop.get("shopId", "-")
        shop_nick = shop.get("shopNick", "-") or "-"
        site = shop.get("site", "-") or "-"
        status = shop.get("status", "-")
        is_cb = "Y" if shop.get("isCb") == 1 else "N"
        is_cnsc = "Y" if shop.get("isCnsc") == 1 else "N"
        expire = shop.get("gmtExpire", "-") or "-"

        # Truncate long shop names
        if len(shop_nick) > 24:
            shop_nick = shop_nick[:21] + "..."

        print(f"  {i:<4} {shop_id:<12} {shop_nick:<25} {site:<8} {status:<10} {is_cb:<4} {is_cnsc:<5} {expire:<20}")

    return shop_list


def print_summary(all_shops: Dict[str, list]):
    """Print summary across all platforms."""
    total = sum(len(shops) for shops in all_shops.values())
    active_platforms = {p: s for p, s in all_shops.items() if s}

    print(f"\n{'='*90}")
    print(f"  Summary: {total} shop(s) across {len(active_platforms)} platform(s)")
    print(f"{'='*90}")

    if not active_platforms:
        print("  No authorized shops found on any platform.")
        return

    for platform, shops in active_platforms.items():
        display_name = PLATFORM_DISPLAY_NAMES.get(platform, platform)
        normal_count = sum(1 for s in shops if s.get("status") == "normal")
        print(f"  {display_name:<20} {len(shops)} shop(s) ({normal_count} normal)")


def print_failures(failures: List[dict]):
    """Print failed platform/site queries so partial discovery is never silent."""
    if not failures:
        return

    print(f"\n  Warning: {len(failures)} platform/site query failure(s):")
    for failure in failures:
        print(
            f"  - {failure['platform']}/{failure['site']} page {failure['page']}: "
            f"[{failure['code']}] {failure['message']}"
        )


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

def cmd_list(client: ShopListClient, args):
    """Query shop list for a specific platform."""
    platform = normalize_platform(args.platform)
    site = validate_site(platform, args.site)
    page = args.page
    size = args.size

    result = client.get_shop_list(platform=platform, site=site, page=page, size=size)

    if result.get("result") == "success":
        for shop in result.get("data", {}).get("shopList", []):
            enrich_shop_flags(shop)
        shops = print_shop_list(result, platform)
        print(f"\n  Total: {len(shops)} shop(s)")
    else:
        print(f"\n  Failed to query shops for platform '{platform}'")

    return result


def cmd_list_all(client: ShopListClient, args):
    """Query shop list across all common platforms by iterating platform+site combos."""
    all_shops: Dict[str, list] = {}
    failures: List[dict] = []
    seen_shop_keys = set()

    requested_platform = getattr(args, "platform", None)
    platforms = [normalize_platform(requested_platform)] if requested_platform else COMMON_PLATFORMS
    for platform in platforms:
        sites = PLATFORM_SITES[platform]
        platform_shops = []

        for site in sites:
            page = 1
            seen_page_fingerprints = set()
            while True:
                try:
                    result = client.get_shop_list(platform=platform, site=site, page=page, size=100, silent=True)
                except Exception as exc:
                    failures.append({
                        "platform": platform,
                        "site": site,
                        "page": page,
                        "code": type(exc).__name__,
                        "message": str(exc),
                    })
                    break

                if result.get("result") == "success":
                    shop_list = result.get("data", {}).get("shopList", [])
                    page_fingerprint = tuple(
                        (shop.get("shopId"), shop.get("site"), shop.get("shopNick"))
                        for shop in shop_list
                    )
                    if shop_list and page_fingerprint in seen_page_fingerprints:
                        failures.append({
                            "platform": platform,
                            "site": site,
                            "page": page,
                            "code": "repeatedPage",
                            "message": "API repeated a previous page; pagination stopped to prevent an infinite loop",
                        })
                        break
                    seen_page_fingerprints.add(page_fingerprint)

                    for shop in shop_list:
                        enrich_shop_flags(shop)
                        sid = shop.get("shopId")
                        shop_site = shop.get("site") or site
                        shop_key = (platform, shop_site, sid)
                        if sid is None:
                            shop_key = (platform, shop_site, shop.get("shopNick"), shop.get("parentShopId"))
                        if shop_key not in seen_shop_keys:
                            seen_shop_keys.add(shop_key)
                            platform_shops.append(shop)

                    if len(shop_list) < 100:
                        break
                    page += 1
                else:
                    failures.append({
                        "platform": platform,
                        "site": site,
                        "page": page,
                        "code": result.get("code", "apiError"),
                        "message": result.get("message", "API query failed"),
                    })
                    break

        if platform_shops:
            all_shops[platform] = platform_shops
            # Build a fake result for printing
            print_shop_list({"data": {"shopList": platform_shops}}, platform)

    print_summary(all_shops)
    print_failures(failures)

    return {"shops": all_shops, "failures": failures}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Miaoshou ERP - Query Authorized Shop List",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Query TikTok shops in US site
  %(prog)s list --platform tiktok --site US

  # Query Shopee shops with pagination
  %(prog)s list --platform shopee --site MY --page 1 --size 50

  # Query Ozon shops (the required selector is OZON, not RU)
  %(prog)s list --platform ozon --site OZON

  # Query MercadoLibre shops
  %(prog)s list --platform mercadolibre --site CBT

  # Scan all common platforms
  %(prog)s list-all
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # list sub-command
    list_parser = subparsers.add_parser("list", help="Query shops for a specific platform")
    list_parser.add_argument("--platform", type=str, required=True,
                             help="Platform code (tiktok, tiktokGlobal, ozon, pddkj, pddkjChoice, shopee, shopeeGlobal, mercadolibre, etc.)")
    list_parser.add_argument("--site", type=str, required=True,
                             help="Required site code (for example US, GB, or OZON). Ozon requires OZON, not RU. Use list-all for broad discovery.")
    list_parser.add_argument("--page", type=int, default=1, help="Page number (default: 1)")
    list_parser.add_argument("--size", type=int, default=100, help="Page size (default: 100)")

    # list-all sub-command
    subparsers.add_parser("list-all", help="Scan all common platforms for authorized shops")
    platform_all = subparsers.add_parser("list-platform-all", help="Scan every documented site for one platform")
    platform_all.add_argument("--platform", required=True)

    # --raw flag
    parser.add_argument("--raw", action="store_true", help="Output raw JSON")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config()
    client = ShopListClient(config)

    command_map = {
        "list": cmd_list,
        "list-all": cmd_list_all,
        "list-platform-all": cmd_list_all,
    }

    data = command_map[args.command](client, args)

    if args.raw:
        print("\n--- RAW JSON ---")
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
