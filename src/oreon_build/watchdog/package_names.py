# Oreon Build Service
# Copyright (C) 2026 Oreon HQ
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared RPM package name helpers for Watchdog (scanner + HTTP)."""

from __future__ import annotations

import re

DEFAULT_OMIT_SUBPACKAGE_SUFFIXES = "debuginfo,debugsource,doc,help,static"


def parse_omit_subpackage_suffixes(raw: str | None, *, default: str = DEFAULT_OMIT_SUBPACKAGE_SUFFIXES) -> tuple[str, ...]:
    if raw is None:
        raw = default
    s = str(raw).strip()
    if s == "":
        return ()
    return tuple(x.strip().lower() for x in s.split(",") if x.strip())


def rpm_name_is_omitted_subpackage(pkg_name: str, suffixes: tuple[str, ...]) -> bool:
    if not suffixes:
        return False
    ln = pkg_name.lower()
    for suf in suffixes:
        if ln.endswith("-" + suf):
            return True
    return False


def tracked_rpm_not_subpackage_predicate(suffixes: tuple[str, ...]):
    """PostgreSQL case-insensitive regex: name must not end with -suffix."""
    if not suffixes:
        return None
    from sqlalchemy import not_

    from oreon_build.models import TrackedRpm

    inner = "|".join(re.escape(s) for s in suffixes)
    pat = f".*-({inner})$"
    return not_(TrackedRpm.package_name.op("~*")(pat))
