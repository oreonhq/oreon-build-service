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
from datetime import datetime, timezone

from sqlalchemy import delete, not_, select

from oreon_build.config import get_settings
from oreon_build.db.session import async_session_maker
from oreon_build.models import (
    Artifact,
    BuildAttempt,
    BuildJob,
    CveMatch,
    Package,
    TrackedRpm,
)
from oreon_build.watchdog.package_names import (
    parse_omit_subpackage_suffixes,
    rpm_name_is_omitted_subpackage,
)

logger = logging.getLogger(__name__)

_RPM_FILENAME_RE = re.compile(
    r"^(?P<name>.+)-(?P<version>[^-]+)-(?P<release>[^-]+)\.(?P<arch>[^.]+)\.rpm$"
)


def parse_rpm_filename(filename: str) -> tuple[str, str, str, str] | None:
    if not filename.endswith(".rpm"):
        return None
    if filename.endswith(".src.rpm"):
        return None
    m = _RPM_FILENAME_RE.match(filename)
    if not m:
        return None
    return m.group("name"), m.group("version"), m.group("release"), m.group("arch")


async def scan_cves_once() -> None:
    """
    Refresh ``tracked_rpms`` from build artifacts only (same DB as the controller).

    External CVE feeds (OSV, NVD, R2 walks) are not used. Security advisories are
    created manually via the Watchdog API or UI.
    """
    settings = get_settings()
    sub_suffixes = parse_omit_subpackage_suffixes(str(settings.cve_scan_omit_subpackage_suffixes or ""))
    now = datetime.now(timezone.utc)

    async with async_session_maker() as db:
        pkg_rows = (
            await db.execute(
                select(BuildJob.release_id, Package.name).join(Package, BuildJob.package_id == Package.id)
            )
        ).all()
        source_names_by_release: dict[int, set[str]] = {}
        for rid, pkg_name in pkg_rows:
            pn = str(pkg_name or "").strip()
            if not pn:
                continue
            source_names_by_release.setdefault(int(rid), set()).add(pn)

        q = (
            select(Artifact.filename, BuildJob.release_id, Package.name)
            .join(BuildAttempt, Artifact.build_attempt_id == BuildAttempt.id)
            .join(BuildJob, BuildAttempt.build_job_id == BuildJob.id)
            .join(Package, BuildJob.package_id == Package.id)
            .where(
                Artifact.kind == "rpm",
                Artifact.filename.like("%.rpm"),
                not_(Artifact.filename.like("%.src.rpm")),
            )
        )
        rows = (await db.execute(q)).all()

        tracked_to_releases: dict[tuple[str, str, str], set[int]] = {}
        for filename, release_id, source_pkg_name in rows:
            parsed = parse_rpm_filename(filename)
            if not parsed:
                continue
            name, ver, rel, _arch = parsed
            if name != str(source_pkg_name or "").strip():
                continue
            if rpm_name_is_omitted_subpackage(name, sub_suffixes):
                continue
            key = (name, ver, rel)
            tracked_to_releases.setdefault(key, set()).add(int(release_id))

        if not tracked_to_releases:
            logger.info("No RPM artifacts found for Watchdog inventory sync.")
            return

        logger.info("Watchdog inventory sync gathered %s distinct RPM identities from artifacts", len(tracked_to_releases))

        source_name_set: set[str] = set()
        for names in source_names_by_release.values():
            source_name_set.update(names)
        if source_name_set:
            orphan_subq = (
                select(CveMatch.id).where(CveMatch.tracked_rpm_id == TrackedRpm.id).exists()
            )
            stale_rows = await db.execute(
                select(TrackedRpm.id, TrackedRpm.package_name).where(~orphan_subq)
            )
            stale_ids: list[int] = []
            for tr_id, pkg_name in stale_rows.all():
                pn = str(pkg_name or "")
                if rpm_name_is_omitted_subpackage(pn, sub_suffixes) or pn not in source_name_set:
                    stale_ids.append(int(tr_id))
            if stale_ids:
                await db.execute(delete(TrackedRpm).where(TrackedRpm.id.in_(stale_ids)))
                await db.flush()
                logger.info("Pruned %s stale tracked RPM rows (subpackages/non-source names)", len(stale_ids))

        distinct_pkg_names = list({k[0] for k in tracked_to_releases.keys()})
        if not distinct_pkg_names:
            return

        existing_rows = await db.execute(
            select(TrackedRpm).where(TrackedRpm.package_name.in_(distinct_pkg_names))
        )
        existing = {
            (r.package_name, r.rpm_version, r.rpm_release): r for r in existing_rows.scalars().all()
        }

        new_rows: list[TrackedRpm] = []
        for (pkg_name, rpm_ver, rpm_rel), _release_ids in tracked_to_releases.items():
            if rpm_name_is_omitted_subpackage(pkg_name, sub_suffixes):
                continue
            tr = existing.get((pkg_name, rpm_ver, rpm_rel))
            if tr:
                tr.last_checked_at = now
            else:
                tr = TrackedRpm(
                    package_name=pkg_name,
                    rpm_version=rpm_ver,
                    rpm_release=rpm_rel,
                    last_checked_at=now,
                )
                new_rows.append(tr)
                existing[(pkg_name, rpm_ver, rpm_rel)] = tr

        if new_rows:
            db.add_all(new_rows)
        await db.commit()
        if new_rows:
            logger.info("Watchdog inventory sync added %s new tracked RPM rows", len(new_rows))
