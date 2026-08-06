#!/usr/bin/env python3
"""Check local Miaoshou ERP OpenAPI configuration without submitting products."""

from __future__ import annotations

import argparse
import json
import sys

from miaoshou_openapi import ConfigError, json_body, load_config, sign_headers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.json. Defaults to resources/config.json.")
    parser.add_argument(
        "--allow-placeholder",
        action="store_true",
        help="Only validate JSON shape; do not require real app credentials.",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        if not args.allow_placeholder:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 4
        print("Configuration shape check only: placeholders or missing credentials are allowed.")
        return 0

    sample_body = json_body({"collectLinks": ["https://detail.1688.com/offer/123456789.html"]})
    headers = sign_headers(
        config,
        "/open/v1/product/common_collect_box/common_collect_box/fetch_item",
        sample_body,
    )
    safe_report = {
        "base_url": config.base_url,
        "timeout": config.timeout,
        "has_app_key": bool(config.app_key),
        "has_app_secret": bool(config.app_secret),
        "has_account_id": bool(config.account_id),
        "has_authorization": bool(config.authorization),
        "has_cookie": bool(config.cookie),
        "signed_header_names": sorted(headers.keys()),
    }
    print(json.dumps(safe_report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
