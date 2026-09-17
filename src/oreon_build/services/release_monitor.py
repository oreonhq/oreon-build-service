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
    Package,
    Release,
    ReleaseMonitorOutdated,
    ReleaseMonitorScan,
)
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

ANITYA_URL = "https://release-monitoring.org/api/v2/packages/"
ANITYA_PROJECTS_URL = "https://release-monitoring.org/api/v2/projects/"
ANITYA_DISTRO = "Fedora"
KERNEL_RELEASES_URL = "https://www.kernel.org/releases.json"
_ANITYA_CONCURRENCY = 24
_ANITYA_ATTEMPTS = 4
_ANITYA_PAGE_SIZE = 250
_ANITYA_INDEX_CONCURRENCY = 16
_MAX_PACKAGES = 20000
_KERNEL_PACKAGES = {
    "kernel",
    "kernel-core",
    "kernel-devel",
    "kernel-headers",
    "kernel-modules",
    "kernel-modules-core",
    "kernel-tools",
    "linux",
}

_running: set[asyncio.Task] = set()
_periodic_task: asyncio.Task | None = None


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


def _strip_version_tag(raw: str) -> str:
    s = (raw or "").strip()
    if s.lower().startswith("v") and len(s) > 1 and s[1].isdigit():
        return s[1:]
    return s


def parse_github_repo(url: str) -> tuple[str, str] | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if "github.com/" not in raw.lower():
        return None
    path = raw.split("github.com/", 1)[1]
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if not owner or not repo:
        return None
    return owner, repo


def kernel_org_stable_version(payload: dict) -> str | None:
    latest = payload.get("latest_stable") if isinstance(payload, dict) else None
    if isinstance(latest, dict):
        ver = _strip_version_tag(str(latest.get("version") or ""))
        if ver:
            return ver
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, list):
        return None
    for item in releases:
        if not isinstance(item, dict):
            continue
        if str(item.get("moniker") or "").strip().lower() != "stable":
            continue
        ver = _strip_version_tag(str(item.get("version") or ""))
        if ver:
            return ver
    return None


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> dict | list | None:
    merged = dict(headers or {})
    for attempt in range(_ANITYA_ATTEMPTS):
        try:
            r = await client.get(url, params=params, headers=merged or None)
            if r.status_code == 429:
                await asyncio.sleep(min(2.0 * (attempt + 1), 15.0))
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 500:
                if attempt + 1 >= _ANITYA_ATTEMPTS:
                    return None
                await asyncio.sleep(min(1.5 * (2**attempt), 20.0))
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, (dict, list)):
                return data
            return None
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt + 1 >= _ANITYA_ATTEMPTS:
                return None
            await asyncio.sleep(min(1.5 * (2**attempt), 20.0))
    return None


def _hit(version: str, project: str | None, ecosystem: str | None) -> dict | None:
    ver = _strip_version_tag(version)
    if not ver:
        return None
    return {
        "latest_version": ver,
        "project": (project or "").strip() or None,
        "ecosystem": (ecosystem or "").strip() or None,
    }


def ingest_anitya_package_items(index: dict[str, dict], items: object) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name or name in index:
            continue
        ver = (item.get("stable_version") or item.get("version") or "").strip()
        hit = _hit(ver, item.get("project"), item.get("ecosystem"))
        if hit:
            index[name] = hit


async def _anitya_fedora_package_index(client: httpx.AsyncClient) -> dict[str, dict]:
    index: dict[str, dict] = {}
    first = await _get_json(
        client,
        ANITYA_URL,
        params={"distribution": ANITYA_DISTRO, "page": 1, "items_per_page": _ANITYA_PAGE_SIZE},
    )
    if not isinstance(first, dict):
        return index
    ingest_anitya_package_items(index, first.get("items"))
    total = int(first.get("total_items") or 0)
    last_page = max(1, (total + _ANITYA_PAGE_SIZE - 1) // _ANITYA_PAGE_SIZE)
    if last_page <= 1:
        return index
    sem = asyncio.Semaphore(_ANITYA_INDEX_CONCURRENCY)

    async def fetch_page(page: int) -> None:
        async with sem:
            data = await _get_json(
                client,
                ANITYA_URL,
                params={
                    "distribution": ANITYA_DISTRO,
                    "page": page,
                    "items_per_page": _ANITYA_PAGE_SIZE,
                },
            )
        if isinstance(data, dict):
            ingest_anitya_package_items(index, data.get("items"))

    await asyncio.gather(*(fetch_page(p) for p in range(2, last_page + 1)))
    logger.info("anitya fedora package index size=%s pages=%s", len(index), last_page)
    return index


async def _anitya_latest(client: httpx.AsyncClient, name: str) -> dict | None:
    data = await _get_json(
        client,
        ANITYA_URL,
        params={"distribution": ANITYA_DISTRO, "name": name},
    )
    if not isinstance(data, dict):
        return None
    for item in data.get("items") or []:
        if (item.get("name") or "").strip() != name:
            continue
        ver = (item.get("stable_version") or item.get("version") or "").strip()
        return _hit(ver, item.get("project"), item.get("ecosystem"))
    return None


async def _anitya_project_latest(client: httpx.AsyncClient, name: str) -> dict | None:
    data = await _get_json(client, ANITYA_PROJECTS_URL, params={"name": name})
    if not isinstance(data, dict):
        return None
    for item in data.get("items") or []:
        if (item.get("name") or "").strip() != name:
            continue
        stable = item.get("stable_versions") or []
        ver = ""
        if isinstance(stable, list) and stable:
            ver = str(stable[0] or "").strip()
        if not ver:
            ver = str(item.get("version") or "").strip()
        homepage = str(item.get("homepage") or item.get("ecosystem") or "").strip()
        return _hit(ver, item.get("name"), homepage or "anitya")
    return None


async def _kernel_org_latest(client: httpx.AsyncClient, cache: dict) -> dict | None:
    if "kernel.org" in cache:
        return cache["kernel.org"]
    data = await _get_json(client, KERNEL_RELEASES_URL)
    ver = kernel_org_stable_version(data if isinstance(data, dict) else {})
    hit = _hit(ver or "", "kernel", "https://www.kernel.org/")
    cache["kernel.org"] = hit
    return hit


async def _github_latest(client: httpx.AsyncClient, url: str) -> dict | None:
    parsed = parse_github_repo(url)
    if parsed is None:
        return None
    owner, repo = parsed
    from oreon_build.config import get_settings

    headers = {"Accept": "application/vnd.github+json"}
    token = (get_settings().github_token or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    latest = await _get_json(
        client,
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
        headers=headers,
    )
    if isinstance(latest, dict):
        ver = str(latest.get("tag_name") or latest.get("name") or "").strip()
        hit = _hit(ver, f"{owner}/{repo}", f"https://github.com/{owner}/{repo}")
        if hit:
            return hit
    tags = await _get_json(
        client,
        f"https://api.github.com/repos/{owner}/{repo}/tags",
        params={"per_page": 20},
        headers=headers,
    )
    if not isinstance(tags, list):
        return None
    for item in tags:
        if not isinstance(item, dict):
            continue
        ver = str(item.get("name") or "").strip()
        hit = _hit(ver, f"{owner}/{repo}", f"https://github.com/{owner}/{repo}")
        if hit:
            return hit
    return None


async def lookup_upstream_latest(
    client: httpx.AsyncClient,
    name: str,
    *,
    github_url: str | None = None,
    cache: dict | None = None,
    anitya_index: dict[str, dict] | None = None,
    skip_slow_lookups: bool = False,
) -> dict | None:
    cache = cache if cache is not None else {}
    pkg = (name or "").strip()
    if not pkg:
        return None
    if pkg in _KERNEL_PACKAGES:
        hit = await _kernel_org_latest(client, cache)
        if hit:
            return hit
    if anitya_index is not None:
        hit = anitya_index.get(pkg)
        if hit:
            return hit
    else:
        hit = await _anitya_latest(client, pkg)
        if hit:
            return hit
    if not skip_slow_lookups:
        hit = await _anitya_project_latest(client, pkg)
        if hit:
            return hit
    if github_url:
        return await _github_latest(client, github_url)
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
        github_by_name: dict[str, str] = {}
        async with async_session_maker() as db:
            scan = await db.get(ReleaseMonitorScan, scan_id)
            if scan is not None:
                scan.total_packages = total
                await db.commit()
            if packages:
                names = list(packages.keys())
                rows = (
                    await db.execute(
                        select(Package.name, Package.github_html_url).where(
                            Package.name.in_(names),
                            Package.github_html_url.isnot(None),
                        )
                    )
                ).all()
                for pkg_name, gh in rows:
                    url = (gh or "").strip()
                    if pkg_name and url and pkg_name not in github_by_name:
                        github_by_name[str(pkg_name)] = url

        if total == 0:
            await _finish_scan(scan_id, status="done", error=None)
            return

        outdated: list[dict] = []
        checked = 0
        matched = 0
        upstream_cache: dict = {}
        github_misses: list[tuple[str, str, str]] = []

        headers = {"User-Agent": "oreon-watchdog-release-monitor/1.0"}
        timeout = httpx.Timeout(60.0, connect=30.0)
        limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, follow_redirects=True, limits=limits
        ) as client:
            anitya_index = await _anitya_fedora_package_index(client)
            await _kernel_org_latest(client, upstream_cache)

            for name, (ver, rel) in packages.items():
                info = await lookup_upstream_latest(
                    client,
                    name,
                    cache=upstream_cache,
                    anitya_index=anitya_index,
                    skip_slow_lookups=True,
                )
                checked += 1
                if info is None and github_by_name.get(name):
                    github_misses.append((name, ver, rel))
                    continue
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
                if checked % 250 == 0:
                    await _bump_progress(scan_id, checked, matched)

            sem = asyncio.Semaphore(_ANITYA_CONCURRENCY)

            async def github_worker(name: str, ver: str, rel: str) -> None:
                nonlocal checked, matched
                async with sem:
                    info = await _github_latest(client, github_by_name[name])
                if info is None:
                    return
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

            if github_misses:
                await asyncio.gather(*(github_worker(n, v, r) for n, v, r in github_misses))
            await _bump_progress(scan_id, checked, matched)

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


async def run_automatic_release_monitor_scan() -> None:
    async with async_session_maker() as db:
        active_releases = (
            await db.execute(select(Release).where(Release.is_active.is_(True)).order_by(Release.id))
        ).scalars().all()
        running = (
            await db.execute(select(ReleaseMonitorScan.release_id).where(ReleaseMonitorScan.status == "running"))
        ).scalars().all()
        running_ids = {int(value) for value in running if value is not None}
        scan_ids: list[int] = []
        branches = ",".join(PRIMARY_REPO_BRANCHES)
        for release in active_releases:
            if release.id in running_ids:
                continue
            scan = ReleaseMonitorScan(
                release_id=release.id,
                branches=branches,
                status="running",
            )
            db.add(scan)
            await db.flush()
            scan_ids.append(int(scan.id))
        await db.commit()
    for scan_id in scan_ids:
        schedule_scan(scan_id)


def start_periodic_release_monitor(interval_seconds: int = 18000) -> asyncio.Task:
    global _periodic_task
    if _periodic_task is not None and not _periodic_task.done():
        return _periodic_task

    async def loop() -> None:
        while True:
            try:
                await run_automatic_release_monitor_scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("automatic release monitor scan failed")
            await asyncio.sleep(interval_seconds)

    _periodic_task = asyncio.create_task(loop())
    return _periodic_task


def stop_periodic_release_monitor() -> None:
    global _periodic_task
    if _periodic_task is not None:
        _periodic_task.cancel()
        _periodic_task = None
