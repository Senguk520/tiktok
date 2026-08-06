#!/usr/bin/env python3
"""Preview or submit source product links to Miaoshou ERP common collect box."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from miaoshou_openapi import ApiError, ConfigError, classify_api_code, load_config, post_json


FETCH_ITEM_PATH = "/open/v1/product/common_collect_box/common_collect_box/fetch_item"
TRAILING_PUNCTUATION = " \t\r\n,.;:!?)]}>'\"，。；：！？）】》、"
URL_PATTERN = re.compile(r"https?://[^\s<>()\"'，。；：！？【】]+", re.IGNORECASE)
PRODUCT_QUERY_KEYS = {"id", "goods_id", "offerid", "item_id", "product_id", "sku"}
PAGE_SEARCH_QUERY_KEYS = {"q", "keyword", "searchtext"}
TRACKING_QUERY_KEYS = {"spm"}


@dataclass(frozen=True)
class LinkCheck:
    url: str
    ok: bool
    domain: str | None
    reason: str


def extract_urls(chunks: Iterable[str]) -> list[str]:
    urls: list[str] = []
    for chunk in chunks:
        for match in URL_PATTERN.findall(chunk):
            urls.append(clean_url(match))
    return urls


def clean_url(url: str) -> str:
    return url.strip().rstrip(TRAILING_PUNCTUATION)


def dedupe_preserve_order(urls: Iterable[str]) -> tuple[list[str], list[str]]:
    seen: set[str] = set()
    unique: list[str] = []
    duplicates: list[str] = []
    for raw_url in urls:
        url = clean_url(raw_url)
        if not url:
            continue
        key = url.casefold()
        if key in seen:
            duplicates.append(url)
            continue
        seen.add(key)
        unique.append(url)
    return unique, duplicates


def check_link(url: str) -> LinkCheck:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    query = _normalize_query(parse_qs(parsed.query))

    if parsed.scheme not in {"http", "https"} or not domain:
        return LinkCheck(url, False, None, "not an absolute http(s) URL")

    if _looks_like_non_product_page(domain, path, query):
        return LinkCheck(url, False, domain, "looks like a search, category, store, or home page")

    if "1688.com" in domain:
        if re.search(r"/offer/\d+\.html$", path) or path.startswith("/product/") or query.get("offerid"):
            return LinkCheck(url, True, domain, "1688 product detail URL")
        return LinkCheck(url, False, domain, "1688 URL is not a recognized product detail page")

    if "aliexpress." in domain:
        if re.search(r"/item/[^/]+\.html$", path):
            return LinkCheck(url, True, domain, "AliExpress product detail URL")
        return LinkCheck(url, False, domain, "AliExpress URL is not a recognized item page")

    if "taobao.com" in domain:
        if path.endswith("/item.htm") and query.get("id"):
            return LinkCheck(url, True, domain, "Taobao product detail URL")
        return LinkCheck(url, False, domain, "Taobao URL is missing item id")

    if "tmall.com" in domain:
        if path.endswith("/item.htm") and query.get("id"):
            return LinkCheck(url, True, domain, "Tmall product detail URL")
        return LinkCheck(url, False, domain, "Tmall URL is missing item id")

    if "yangkeduo.com" in domain or "pinduoduo.com" in domain:
        if path.endswith("/product.html") and query.get("goods_id"):
            return LinkCheck(url, True, domain, "Pinduoduo product detail URL")
        return LinkCheck(url, False, domain, "Pinduoduo URL is missing goods_id")

    if _generic_detail_candidate(path, query):
        return LinkCheck(url, True, domain, "generic product detail candidate")

    return LinkCheck(url, False, domain, "unrecognized or ambiguous source URL")


def _looks_like_non_product_page(domain: str, path: str, query: dict[str, list[str]]) -> bool:
    if path in {"", "/"}:
        return True
    non_product_terms = (
        "search",
        "category",
        "categories",
        "shop",
        "store",
        "seller",
        "supplier",
        "company",
        "offer_search",
    )
    if any(term in path for term in non_product_terms):
        return True
    if any(key in PRODUCT_QUERY_KEYS for key in query):
        return False
    if any(key in PAGE_SEARCH_QUERY_KEYS for key in query):
        return True
    if any(key in TRACKING_QUERY_KEYS for key in query):
        return False
    return False


def _generic_detail_candidate(path: str, query: dict[str, list[str]]) -> bool:
    if any(key in query for key in PRODUCT_QUERY_KEYS):
        return True
    return bool(re.search(r"/(item|product|goods|offer|detail)[-/]?[a-z0-9_]*", path))


def _normalize_query(query: dict[str, list[str]]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for key, values in query.items():
        normalized.setdefault(key.lower(), []).extend(values)
    return normalized


def build_plan(urls: Iterable[str]) -> dict[str, object]:
    url_list = list(urls)
    unique_urls, duplicates = dedupe_preserve_order(url_list)
    checks = [check_link(url) for url in unique_urls]
    valid = [check for check in checks if check.ok]
    invalid = [check for check in checks if not check.ok]
    domains = sorted({check.domain for check in valid if check.domain})
    return {
        "input_count": len(url_list),
        "submit_count": len(valid),
        "domains": domains,
        "valid_urls": [check.url for check in valid],
        "invalid_urls": [check.__dict__ for check in invalid],
        "duplicates": duplicates,
    }


def collect_inputs(args: argparse.Namespace) -> list[str]:
    chunks: list[str] = []
    chunks.extend(args.url or [])
    if args.text:
        chunks.append(args.text)
    if args.input:
        chunks.append(Path(args.input).read_text(encoding="utf-8"))
    if args.stdin:
        chunks.append(sys.stdin.read())
    urls = extract_urls(chunks)
    if not urls:
        urls = [clean_url(url) for url in (args.url or [])]
    return urls


def print_plan(plan: dict[str, object], json_output: bool) -> None:
    if json_output:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return

    print("采集计划预览")
    print(f"- 可提交链接数: {plan['submit_count']}")
    print(f"- 来源域名: {', '.join(plan['domains']) if plan['domains'] else '(none)'}")
    duplicate_count = len(plan["duplicates"])  # type: ignore[arg-type]
    invalid_count = len(plan["invalid_urls"])  # type: ignore[arg-type]
    if duplicate_count:
        print(f"- 已去重链接数: {duplicate_count}")
    if invalid_count:
        print(f"- 无效/模糊链接数: {invalid_count}")
        for item in plan["invalid_urls"]:  # type: ignore[union-attr]
            print(f"  - {item['url']} ({item['reason']})")


def preview(args: argparse.Namespace) -> int:
    plan = build_plan(collect_inputs(args))
    print_plan(plan, args.json)
    return 0 if plan["submit_count"] else 2


def fetch(args: argparse.Namespace) -> int:
    plan = build_plan(collect_inputs(args))
    valid_urls = plan["valid_urls"]
    invalid_urls = plan["invalid_urls"]

    if invalid_urls and not args.ignore_invalid:
        print_plan(plan, args.json)
        print("Refusing to submit while invalid or ambiguous URLs are present.", file=sys.stderr)
        return 2

    if not valid_urls:
        print_plan(plan, args.json)
        print("No valid product detail URLs to submit.", file=sys.stderr)
        return 2

    if not args.confirm:
        print_plan(plan, args.json)
        print("Add --confirm after the user explicitly confirms collection.", file=sys.stderr)
        return 3

    try:
        config = load_config(args.config)
        response = post_json(config, FETCH_ITEM_PATH, {"collectLinks": valid_urls})
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 4
    except ApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 5

    result = {
        "submitted_count": len(valid_urls),
        "api_response": response,
        "auth_hint": classify_api_code(str(response.get("code"))),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if str(response.get("code")) == "0" or response.get("result") == "success" else 6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_input_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--url", action="append", help="Product detail URL. Repeat as needed.")
        command.add_argument("--text", help="Free text containing one or more URLs.")
        command.add_argument("--input", help="UTF-8 text file containing URLs.")
        command.add_argument("--stdin", action="store_true", help="Read URLs from stdin.")
        command.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    preview_parser = subparsers.add_parser("preview", help="Validate, de-duplicate, and summarize URLs.")
    add_input_options(preview_parser)
    preview_parser.set_defaults(func=preview)

    fetch_parser = subparsers.add_parser("fetch", help="Submit confirmed URLs to the common collect box.")
    add_input_options(fetch_parser)
    fetch_parser.add_argument("--config", help="Path to config.json. Defaults to resources/config.json.")
    fetch_parser.add_argument("--confirm", action="store_true", help="Required after explicit user confirmation.")
    fetch_parser.add_argument("--ignore-invalid", action="store_true", help="Submit valid URLs and skip invalid ones.")
    fetch_parser.set_defaults(func=fetch)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
