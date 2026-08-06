#!/usr/bin/env python3
"""Standard source collection entrypoint with --urls support."""

from __future__ import annotations

import argparse
from argparse import Namespace

from source_import import fetch, preview


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--urls",
        nargs="+",
        required=True,
        help="One or more 1688/AliExpress/source product detail URLs.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--config", help="Path to config.json. Defaults to resources/config.json.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Submit to Miaoshou ERP after explicit user confirmation. Without this, only preview.",
    )
    parser.add_argument(
        "--ignore-invalid",
        action="store_true",
        help="Submit valid URLs and skip invalid ones after the user chooses to skip invalid links.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    common_args = Namespace(
        url=args.urls,
        text=None,
        input=None,
        stdin=False,
        json=args.json,
        config=args.config,
        confirm=args.confirm,
        ignore_invalid=args.ignore_invalid,
    )
    if args.confirm:
        return fetch(common_args)
    return preview(common_args)


if __name__ == "__main__":
    raise SystemExit(main())
