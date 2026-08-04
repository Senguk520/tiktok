from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.db.base import DatabaseSettings, create_engine_and_session_factory
from app.db.models import CollectorImportReceipt, ProductDraft, ShopBinding
from migrations.core import migrate_engine as migrate_core
from shared.safe_paths import PROJECT_ROOT

_SECRET = "collector-process-boundary-secret-at-least-32-bytes"
_ADMIN_SECRET = "collector-process-admin-secret-at-least-32-bytes"
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_COLLECTOR_SERVER = _BACKEND_ROOT / "tests" / "_collector_e2e_server.py"


async def _seed_core(path: Path) -> str:
    engine, factory = create_engine_and_session_factory(
        DatabaseSettings(url=f"sqlite+aiosqlite:///{path.as_posix()}", path=path)
    )
    try:
        await migrate_core(engine)
        async with factory() as session:
            shop = ShopBinding(
                open_id=f"process-owner-{uuid4()}",
                shop_id=f"process-shop-{uuid4()}",
                region="MY",
            )
            session.add(shop)
            await session.commit()
            return shop.id
    finally:
        await engine.dispose()


async def _assert_one_core_import(path: Path) -> None:
    engine, factory = create_engine_and_session_factory(
        DatabaseSettings(url=f"sqlite+aiosqlite:///{path.as_posix()}", path=path)
    )
    try:
        async with factory() as session:
            drafts = await session.scalar(select(func.count()).select_from(ProductDraft))
            receipts = await session.scalar(
                select(func.count()).select_from(CollectorImportReceipt)
            )
        assert drafts == 1
        assert receipts == 1
    finally:
        await engine.dispose()


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _assert_collector_port_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", 8010))
        except OSError as exc:
            raise AssertionError("fixed Collector loopback port 8010 is already in use") from exc


def _start_process(arguments: list[str], environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        arguments,
        cwd=_BACKEND_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )


async def _wait_for_health(
    base_url: str,
    process: subprocess.Popen[str],
    *,
    service: str,
) -> None:
    deadline = time.monotonic() + 15
    async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=1.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                raise AssertionError(
                    f"{service} process exited during startup with {process.returncode}: {output[-1000:]}"
                )
            try:
                response = await client.get("/healthz")
                if response.status_code == 200 and response.json().get("service") == service:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            await _sleep(0.1)
    raise AssertionError(f"{service} process did not become healthy")


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _stop_process(process: subprocess.Popen[str] | None) -> str:
    if process is None:
        return ""
    if process.poll() is None:
        process.terminate()
    try:
        output, _stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _stderr = process.communicate(timeout=5)
    return output


def _remove_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


@pytest.mark.asyncio
async def test_core_and_collector_complete_loopback_flow_in_independent_processes() -> None:
    _assert_collector_port_available()
    run_id = uuid4().hex
    core_path = PROJECT_ROOT / "data" / f"core-process-e2e-{run_id}.sqlite3"
    collector_path = PROJECT_ROOT / "data" / f"collector-process-e2e-{run_id}.sqlite3"
    image_path = PROJECT_ROOT / "temp" / "images" / f"collector-process-e2e-{run_id}.png"
    assert core_path != collector_path
    shop_id = await _seed_core(core_path)
    core_port = _available_port()

    common = os.environ.copy()
    common.pop("DATABASE_URL", None)
    common["COLLECTOR_INTERNAL_HMAC_SECRET"] = _SECRET
    collector_environment = {
        **common,
        "COLLECTOR_DATABASE_PATH": str(collector_path),
        "COLLECTOR_E2E_IMAGE_NAME": image_path.name,
    }
    core_environment = {
        **common,
        "CORE_DATABASE_PATH": str(core_path),
        "ADMIN_BOOTSTRAP_SECRET": _ADMIN_SECRET,
        "ADMIN_SESSION_COOKIE_SECURE": "false",
    }
    collector_process: subprocess.Popen[str] | None = None
    core_process: subprocess.Popen[str] | None = None
    diagnostics: list[str] = []
    flow_completed = False
    try:
        collector_process = _start_process(
            [sys.executable, str(_COLLECTOR_SERVER)],
            collector_environment,
        )
        await _wait_for_health(
            "http://127.0.0.1:8010",
            collector_process,
            service="collector",
        )
        core_process = _start_process(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(core_port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            core_environment,
        )
        core_url = f"http://127.0.0.1:{core_port}"
        await _wait_for_health(core_url, core_process, service="core")

        async with httpx.AsyncClient(
            base_url=core_url,
            trust_env=False,
            timeout=15.0,
        ) as browser:
            intent = {
                "source": "CJ",
                "source_mode": "OFFICIAL_API",
                "source_url": (
                    "https://www.cjdropshipping.com/product/item.html?pid=CJPROCESS1"
                ),
            }
            unauthenticated = await browser.post("/api/collector/jobs", json=intent)
            assert unauthenticated.status_code == 401
            login = await browser.post(
                "/api/session",
                json={"bootstrap_secret": _ADMIN_SECRET},
            )
            assert login.status_code == 201
            csrf = login.json()["csrf_token"]
            missing_csrf = await browser.post("/api/collector/jobs", json=intent)
            assert missing_csrf.status_code == 403
            headers = {"X-CSRF-Token": csrf}
            created = await browser.post("/api/collector/jobs", json=intent, headers=headers)
            assert created.status_code == 201
            job_id = created.json()["job_id"]
            replayed = await browser.post("/api/collector/jobs", json=intent, headers=headers)
            assert replayed.status_code == 200
            assert replayed.json()["job_id"] == job_id
            assert replayed.json()["reused"] is True

            deadline = time.monotonic() + 10
            status: dict[str, object] = {}
            while time.monotonic() < deadline:
                polled = await browser.get(f"/api/collector/jobs/{job_id}")
                assert polled.status_code == 200
                status = polled.json()
                if status["status"] == "SUCCEEDED":
                    break
                await _sleep(0.1)
            assert status["status"] == "SUCCEEDED"
            result_id = str(status["result_id"])

            imported = await browser.post(
                f"/api/collector/shops/{shop_id}/results/{result_id}/import",
                headers=headers,
            )
            assert imported.status_code == 201
            assert imported.json()["created"] is True
            assert imported.json()["collector_acknowledged"] is True
            replayed_import = await browser.post(
                f"/api/collector/shops/{shop_id}/results/{result_id}/import",
                headers=headers,
            )
            assert replayed_import.status_code == 200
            assert replayed_import.json()["created"] is False
            assert replayed_import.json()["collector_acknowledgement_replayed"] is True
            acknowledged = await browser.get(f"/api/collector/jobs/{job_id}")
            assert acknowledged.status_code == 200
            assert acknowledged.json()["imported"] is True
        flow_completed = True
    finally:
        diagnostics.append(_stop_process(core_process))
        diagnostics.append(_stop_process(collector_process))
        if image_path.exists():
            image_path.unlink()
        if not flow_completed:
            _remove_sqlite(core_path)
            _remove_sqlite(collector_path)

    try:
        await _assert_one_core_import(core_path)
    except Exception as exc:
        combined = "\n".join(item[-1000:] for item in diagnostics if item)
        raise AssertionError(f"Core import verification failed. Process output: {combined}") from exc
    finally:
        _remove_sqlite(core_path)
        _remove_sqlite(collector_path)