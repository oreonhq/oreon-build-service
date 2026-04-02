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

"""Normalize CPU architecture strings so worker enrollment matches API trigger requests."""
from __future__ import annotations


def normalize_machine_arch(raw: str | None) -> str:
    """
    Map common aliases to RPM/mock style names used in BuildTarget and the UI.

    Empty or missing values default to x86_64 (same as worker poll fallback).
    """
    s = (raw or "").strip().lower()
    if not s:
        return "x86_64"
    if s in ("amd64", "x64"):
        return "x86_64"
    if s == "arm64":
        return "aarch64"
    return s


def sql_arch_match_values(raw: str | None) -> frozenset[str]:
    """
    ``build_targets.architecture`` values that should match this worker label.

    Used by worker poll so ``arm64`` workers still claim ``aarch64`` jobs.
    """
    s = (raw or "").strip().lower()
    if not s:
        return frozenset()
    canon = normalize_machine_arch(raw)
    out = {canon, s}
    if canon == "aarch64":
        out.add("arm64")
    if canon == "x86_64":
        out.add("amd64")
        out.add("x64")
    return frozenset(out)
