from __future__ import annotations

import asyncio

from migrations.collector import migrate


async def _main() -> None:
    applied = await migrate()
    print(f"collector migrations applied: {list(applied)}")


if __name__ == "__main__":
    asyncio.run(_main())