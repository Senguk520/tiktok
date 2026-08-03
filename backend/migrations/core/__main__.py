from __future__ import annotations

import asyncio

from migrations.core import migrate


async def _main() -> None:
    applied = await migrate()
    print(f"core migrations applied: {list(applied)}")


if __name__ == "__main__":
    asyncio.run(_main())