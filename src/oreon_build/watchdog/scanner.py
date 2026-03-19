# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
import httpx
from sqlalchemy import not_, select

from oreon_build.config import get_settings
from oreon_build.db.session import async_session_maker
from oreon_build.models import (
    Artifact,
    BuildAttempt,
    BuildJob,
    CveMatch,
    CveMatchRelease,
    Release,
    TrackedRpm,
)
from oreon_build.services.discord import send_security_discord_embed

logger = logging.getLogger(__name__)

_RPM_FILENAME_RE = re.compile(
    r"^(?P<name>.+)-(?P<version>[^-]+)-(?P<release>[^-]+)\.(?P<arch>[^.]+)\.rpm$"
)


def parse_rpm_filename(filename: str) -> tuple[str, str, str, str] | None:
    """
    Parse an RPM filename into (name, version, release, arch).
    Excludes source RPMs (which end with .src.rpm) upstream.
    """
    if not filename.endswith(".rpm"):
        return None
    if filename.endswith(".src.rpm"):
        return None
    m = _RPM_FILENAME_RE.match(filename)
    if not m:
        return None
    return m.group("name"), m.group("version"), m.group("release"), m.group("arch")


async def _osv_query_rpm(package_name: str, version_string: str) -> list[dict]:
    """
    Query OSV for an RPM package + version string.
    Returns OSV vuln objects (filtered to those with CVE IDs by caller).
    """
    osv_url = "https://api.osv.dev/v1/query"
    payload = {
        "package": {"name": package_name, "ecosystem": "RPM"},
        "version": version_string,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(osv_url, json=payload, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json() or {}
        vulns = data.get("vulns") or []
        return vulns


def _recommended_fix_from_osv(vuln: dict) -> str:
    fixed = None
    for a in vuln.get("affected") or []:
        for r in a.get("ranges") or []:
            for ev in r.get("events") or []:
                if isinstance(ev, dict) and ev.get("fixed"):
                    fixed = ev.get("fixed")
                    break
            if fixed:
                break
        if fixed:
            break
    if fixed:
        return (
            f"Option 1: backport/patch to keep your current RPM version; "
            f"Option 2: bump this RPM to {fixed} (or later)."
        )
    return "Option 1: backport/patch to keep your current RPM version; Option 2: bump to a fixed version (if available)."


def _osv_reports_version_affected(vuln: dict, version_string: str) -> bool:
    """
    OSV query is supposed to return only advisories affecting the queried version.
    To be safe (and satisfy the "ignore unaffected versions" requirement), we also check
    for explicit `affected[].versions` membership when OSV provides it.
    """
    affected_entries = vuln.get("affected") or []
    for a in affected_entries:
        versions = a.get("versions") or []
        if versions:
            return version_string in versions
    return True


async def scan_cves_once() -> None:
    """
    Scan the currently published RPMs in R2 (inferred from Artifacts in DB) and persist only
    CVEs that OSV reports as affecting the exact RPM version.
    """
    settings = get_settings()
    cooldown = timedelta(hours=max(1, int(settings.cve_scan_cooldown_hours or 24)))
    max_per_run = int(getattr(settings, "cve_scan_max_per_run", 50) or 50)
    now = datetime.now(timezone.utc)

    async with async_session_maker() as db:
        # Gather RPMs present in the binary repo layout.
        q = (
            select(Artifact.filename, BuildJob.release_id)
            .join(BuildAttempt, Artifact.build_attempt_id == BuildAttempt.id)
            .join(BuildJob, BuildAttempt.build_job_id == BuildJob.id)
            .where(
                Artifact.kind == "rpm",
                Artifact.r2_key.like("%/RPMS/%"),
                Artifact.filename.like("%.rpm"),
                not_(Artifact.filename.like("%.src.rpm")),
            )
        )
        rows = (await db.execute(q)).all()

        tracked_to_releases: dict[tuple[str, str, str], set[int]] = {}
        for filename, release_id in rows:
            parsed = parse_rpm_filename(filename)
            if not parsed:
                continue
            name, ver, rel, _arch = parsed
            key = (name, ver, rel)
            tracked_to_releases.setdefault(key, set()).add(int(release_id))

        if not tracked_to_releases:
            logger.info("No RPMs found in repo layout for CVE scanning.")
            return

        all_release_ids: set[int] = set()
        for s in tracked_to_releases.values():
            all_release_ids.update(s)

        release_name_map: dict[int, str] = {}
        if all_release_ids:
            release_rows = await db.execute(
                select(Release.id, Release.releasename).where(Release.id.in_(all_release_ids))
            )
            release_name_map = {rid: name for rid, name in release_rows.all()}

        # Load existing tracked RPMs for the relevant package names.
        package_names = list({k[0] for k in tracked_to_releases.keys()})
        if not package_names:
            return
        existing_rows = await db.execute(
            select(TrackedRpm).where(TrackedRpm.package_name.in_(package_names))
        )
        existing = { (r.package_name, r.rpm_version, r.rpm_release): r for r in existing_rows.scalars().all() }

        due: list[TrackedRpm] = []
        new_tracked: list[TrackedRpm] = []

        for (pkg_name, rpm_ver, rpm_rel), release_ids in tracked_to_releases.items():
            tr = existing.get((pkg_name, rpm_ver, rpm_rel))
            if not tr:
                tr = TrackedRpm(package_name=pkg_name, rpm_version=rpm_ver, rpm_release=rpm_rel)
                new_tracked.append(tr)
                due.append(tr)
                continue
            if tr.last_checked_at is None or (now - tr.last_checked_at) >= cooldown:
                due.append(tr)

        if new_tracked:
            db.add_all(new_tracked)
            await db.flush()

        if not due:
            logger.info("No tracked RPMs are due for OSV scanning.")
            return

        # Limit due set to protect OSV rate limits.
        due = due[:max_per_run]

        tracked_key_to_releases: dict[int, set[int]] = {}
        for tr in due:
            tracked_to_rel = tracked_to_releases.get((tr.package_name, tr.rpm_version, tr.rpm_release), set())
            tracked_key_to_releases[int(tr.id)] = tracked_to_rel

        # Mark last_checked_at before querying OSV to avoid re-entrancy.
        for tr in due:
            tr.last_checked_at = now
        await db.flush()

        newly_inserted: list[tuple[CveMatch, str, str, list[int], list[str]]] = []

        for tr in due:
            version_string = tr.version_string
            try:
                vulns = await _osv_query_rpm(tr.package_name, version_string)
            except Exception as e:
                logger.warning("OSV query failed for %s=%s: %s", tr.package_name, version_string, e)
                continue

            # Filter to CVEs only, and only those explicitly matching our exact queried version when OSV provides version lists.
            cve_objs = [
                v
                for v in vulns
                if (v.get("id") or "").startswith("CVE-") and _osv_reports_version_affected(v, version_string)
            ]
            if not cve_objs:
                continue

            cve_ids = [v["id"] for v in cve_objs if v.get("id")]

            existing_matches_q = await db.execute(
                select(CveMatch.id, CveMatch.cve_id).where(
                    CveMatch.tracked_rpm_id == tr.id,
                    CveMatch.cve_id.in_(cve_ids),
                )
            )
            existing_match_id_by_cve: dict[str, int] = {
                cve_id: mid for mid, cve_id in existing_matches_q.all()
            }
            existing_match_ids = list(existing_match_id_by_cve.values())

            existing_release_pairs: set[tuple[int, int]] = set()
            if existing_match_ids:
                existing_rels_q = await db.execute(
                    select(CveMatchRelease.cve_match_id, CveMatchRelease.release_id).where(
                        CveMatchRelease.cve_match_id.in_(existing_match_ids)
                    )
                )
                existing_release_pairs = {(mid, rid) for mid, rid in existing_rels_q.all()}

            for vuln in cve_objs:
                cve_id = vuln.get("id")
                if not cve_id:
                    continue

                # Upstream URL: first reference URL.
                upstream_url = None
                refs = vuln.get("references") or []
                if refs and isinstance(refs, list):
                    for r in refs:
                        if isinstance(r, dict) and r.get("url"):
                            upstream_url = r["url"]
                            break
                recommended_fix = _recommended_fix_from_osv(vuln)

                match_id = existing_match_id_by_cve.get(cve_id)
                is_new = match_id is None
                if is_new:
                    match = CveMatch(
                        tracked_rpm_id=tr.id,
                        cve_id=cve_id,
                        osv_vulnerability_id=vuln.get("id"),
                        upstream_url=upstream_url,
                        summary=vuln.get("summary") or vuln.get("details"),
                        recommended_fix=recommended_fix,
                        severity=vuln.get("severity"),
                    )
                    db.add(match)
                    await db.flush()
                    match_id = match.id
                else:
                    match = None  # only used for discord notification on new matches

                release_ids = tracked_key_to_releases.get(int(tr.id), set())
                for rid in sorted(release_ids):
                    rid_int = int(rid)
                    if (int(match_id), rid_int) in existing_release_pairs:
                        continue
                    db.add(CveMatchRelease(cve_match_id=int(match_id), release_id=rid_int))
                    existing_release_pairs.add((int(match_id), rid_int))

                release_names = [release_name_map.get(int(rid), str(rid)) for rid in sorted(release_ids)]
                if is_new and match is not None:
                    newly_inserted.append(
                        (match, tr.package_name, version_string, list(release_ids), release_names)
                    )

        await db.commit()

    # Discord notifications (outside DB transaction)
    for match, pkg_name, version_string, _release_ids, release_names in newly_inserted:
        try:
            releases_text = ", ".join(release_names) if release_names else "-"
            pkg_text = f"{pkg_name}-{version_string}"
            fields = [
                ("CVE", match.cve_id),
                ("RPM", pkg_text),
                ("Upstream", match.upstream_url or "-"),
                ("Fix", match.recommended_fix or "-"),
                ("Affected Oreon releases", releases_text),
            ]
            await send_security_discord_embed(
                title=f"security.cve / {match.cve_id}",
                description=match.summary or "",
                fields=fields,
            )
        except Exception:
            logger.exception("Failed to send security Discord notification for %s", match.cve_id)

