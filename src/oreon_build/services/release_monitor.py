from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select

from oreon_build.core.repo_branch import PRIMARY_REPO_BRANCHES
from oreon_build.core.rpm_requires import rpm_evr_compare
from oreon_build.db.session import async_session_maker
from oreon_build.models import (
    Release,
    ReleaseMonitorOutdated,
    ReleaseMonitorScan,
)
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

ANITYA_URL = "https://release-monitoring.org/api/v2/packages/"
ANITYA_DISTRO = "Fedora"
_ANITYA_CONCURRENCY = 8
_ANITYA_ATTEMPTS = 5
_MAX_PACKAGES = 20000

_running: set[asyncio.Task] = set()


def _parse_srpm_name_version(filename: str) -> tuple[str, str, str] | None:
    """systemd-261.1-2.or11.src.rpm -> (systemd, 261.1, 2.or11)."""
    fn = (filename or "").strip()
    if not fn.endswith(".src.rpm"):
        return None
    stem = fn[: -len(".src.rpm")]
    parts = stem.rsplit("-", 2)
    if len(parts) != 3:
        return None
    name, ver, rel = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not name or not ver:
        return None
    return name, ver, rel


def collect_source_packages(release_name: str, branches: list[str]) -> dict[str, tuple[str, str]]:
    """Newest source pkg per name across branches -> {name: (version, release)}."""
    r2 = get_r2_client()
    best: dict[str, tuple[str, str]] = {}
    for branch in branches:
        prefix = f"{release_name}/{branch}/src/"
        for key in r2.iter_keys_with_prefix(prefix):
            fn = key.rsplit("/", 1)[-1]
            parsed = _parse_srpm_name_version(fn)
            if not parsed:
                continue
            name, ver, rel = parsed
            cur = best.get(name)
            if cur is None:
                best[name] = (ver, rel)
                continue
            if rpm_evr_compare(("0", ver, rel), ("0", cur[0], cur[1])) > 0:
                best[name] = (ver, rel)
            if len(best) >= _MAX_PACKAGES:
                break
    return best


async def _anitya_latest(client: httpx.AsyncClient, name: str) -> dict | None:
    """Latest Fedora-mapped upstream stable for name, or None if untracked."""
    for attempt in range(_ANITYA_ATTEMPTS):
        try:
            r = await client.get(
                ANITYA_URL,
                params={"distribution": ANITYA_DISTRO, "name": name},
            )
            if r.status_code == 429:
                await asyncio.sleep(min(2.0 * (attempt + 1), 15.0))
                continue
            if r.status_code >= 500:
                if attempt + 1 >= _ANITYA_ATTEMPTS:
                    return None
                await asyncio.sleep(min(1.5 * (2**attempt), 20.0))
                continue
            r.raise_for_status()
            data = r.json()
            for item in data.get("items") or []:
                if (item.get("name") or "").strip() == name:
                    ver = (item.get("stable_version") or item.get("version") or "").strip()
                    if not ver:
                        return None
                    return {
                        "latest_version": ver,
                        "project": (item.get("project") or "").strip() or None,
                        "ecosystem": (item.get("ecosystem") or "").strip() or None,
                    }
            return None
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt + 1 >= _ANITYA_ATTEMPTS:
                return None
            await asyncio.sleep(min(1.5 * (2**attempt), 20.0))
    return None


def _is_outdated(current_version: str, latest_version: str) -> bool:
    """True only when upstream is strictly newer (version-only, ignore release)."""
    try:
        return rpm_evr_compare(("0", latest_version, ""), ("0", current_version, "")) > 0
    except Exception:
        return False


async def _bump_progress(scan_id: int, checked: int, matched: int) -> None:
    async with async_session_maker() as db:
        scan = await db.get(ReleaseMonitorScan, scan_id)
        if scan is None:
            return
        scan.checked_packages = checked
        scan.matched_packages = matched
        await db.commit()


async def run_release_monitor_scan(scan_id: int) -> None:
    try:
        async with async_session_maker() as db:
            scan = await db.get(ReleaseMonitorScan, scan_id)
            if scan is None:
                return
            branches = [b for b in (scan.branches or "").split(",") if b.strip()]
            release = None
            if scan.release_id is not None:
                release = await db.get(Release, scan.release_id)
            release_name = release.releasename if release else None
            release_id = scan.release_id

        if not release_name:
            await _finish_scan(scan_id, status="failed", error="release not found")
            return
        if not branches:
            branches = list(PRIMARY_REPO_BRANCHES)

        packages = await asyncio.to_thread(collect_source_packages, release_name, branches)
        total = len(packages)
        async with async_session_maker() as db:
            scan = await db.get(ReleaseMonitorScan, scan_id)
            if scan is not None:
                scan.total_packages = total
                await db.commit()

        if total == 0:
            await _finish_scan(scan_id, status="done", error=None)
            return

        outdated: list[dict] = []
        sem = asyncio.Semaphore(_ANITYA_CONCURRENCY)
        checked = 0
        matched = 0
        progress_lock = asyncio.Lock()

        headers = {"User-Agent": "oreon-watchdog-release-monitor/1.0"}
        timeout = httpx.Timeout(60.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:

            async def worker(name: str, ver: str, rel: str) -> None:
                nonlocal checked, matched
                async with sem:
                    info = await _anitya_latest(client, name)
                async with progress_lock:
                    checked += 1
                    if info is not None:
                        matched += 1
                        if _is_outdated(ver, info["latest_version"]):
                            outdated.append(
                                {
                                    "package_name": name,
                                    "current_version": f"{ver}-{rel}" if rel else ver,
                                    "latest_version": info["latest_version"],
                                    "project": info.get("project"),
                                    "ecosystem": info.get("ecosystem"),
                                }
                            )
                    if checked % 100 == 0:
                        await _bump_progress(scan_id, checked, matched)

            tasks = [
                asyncio.create_task(worker(name, ver, rel))
                for name, (ver, rel) in packages.items()
            ]
            await asyncio.gather(*tasks)

        async with async_session_maker() as db:
            await db.execute(
                delete(ReleaseMonitorOutdated).where(ReleaseMonitorOutdated.scan_id == scan_id)
            )
            for row in sorted(outdated, key=lambda d: d["package_name"].lower()):
                db.add(
                    ReleaseMonitorOutdated(
                        scan_id=scan_id,
                        release_id=release_id,
                        package_name=row["package_name"],
                        current_version=row["current_version"],
                        latest_version=row["latest_version"],
                        project=row["project"],
                        ecosystem=row["ecosystem"],
                    )
                )
            scan = await db.get(ReleaseMonitorScan, scan_id)
            if scan is not None:
                scan.status = "done"
                scan.checked_packages = checked
                scan.matched_packages = matched
                scan.outdated_count = len(outdated)
                scan.finished_at = datetime.now(timezone.utc)
                scan.error = None
            await db.commit()
        logger.info(
            "release monitor scan %s done: total=%s matched=%s outdated=%s",
            scan_id,
            total,
            matched,
            len(outdated),
        )
    except Exception as exc:
        logger.exception("release monitor scan %s crashed", scan_id)
        await _finish_scan(scan_id, status="failed", error=str(exc)[:4000])


async def _finish_scan(scan_id: int, *, status: str, error: str | None) -> None:
    async with async_session_maker() as db:
        scan = await db.get(ReleaseMonitorScan, scan_id)
        if scan is None:
            return
        scan.status = status
        scan.error = error
        scan.finished_at = datetime.now(timezone.utc)
        await db.commit()


async def mark_stale_scans_failed() -> None:
    """A scan left running by a process restart can never resume, so fail it."""
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(ReleaseMonitorScan).where(ReleaseMonitorScan.status == "running")
            )
        ).scalars().all()
        for scan in rows:
            scan.status = "failed"
            scan.error = "scan interrupted (service restart)"
            scan.finished_at = datetime.now(timezone.utc)
        if rows:
            await db.commit()


def schedule_scan(scan_id: int) -> None:
    task = asyncio.create_task(run_release_monitor_scan(scan_id))
    _running.add(task)
    task.add_done_callback(lambda t: _running.discard(t))
