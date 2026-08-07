"""Generate the repository-safe Miaoshou MY live-check artifact."""

from __future__ import annotations

import asyncio

from live_checks.miaoshou import run_miaoshou_shop_check
from live_checks.writer import atomic_write_report
from shared.safe_paths import PROJECT_ROOT

ARTIFACT_PATH = PROJECT_ROOT / "docs" / "live-checks" / "miaoshou-shop-list-my.json"


async def _main() -> None:
    report = await run_miaoshou_shop_check()
    atomic_write_report(report, ARTIFACT_PATH)
    print(report.error_category or report.status)


if __name__ == "__main__":
    asyncio.run(_main())