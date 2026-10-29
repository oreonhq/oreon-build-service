# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from oreon_build.services.cargo_target_brs import (
    filter_foreign_target_crate_brs,
    foreign_crate_names_from_metadata,
    target_matches_platform,
)


def test_cfg_unix_matches_linux() -> None:
    assert target_matches_platform("cfg(unix)", "x86_64-unknown-linux-gnu") is True
    assert target_matches_platform("cfg(windows)", "x86_64-unknown-linux-gnu") is False
    assert (
        target_matches_platform('cfg(target_os = "hermit")', "x86_64-unknown-linux-gnu")
        is False
    )
    assert (
        target_matches_platform('cfg(target_os = "linux")', "x86_64-unknown-linux-gnu")
        is True
    )


def test_foreign_names_from_atty_like_metadata() -> None:
    meta = {
        "packages": [
            {
                "dependencies": [
                    {"name": "libc", "target": "cfg(unix)", "rename": None},
                    {
                        "name": "hermit-abi",
                        "target": 'cfg(target_os = "hermit")',
                        "rename": None,
                    },
                    {"name": "winapi", "target": "cfg(windows)", "rename": None},
                    {"name": "serde", "target": None, "rename": None},
                ]
            }
        ]
    }
    drop = foreign_crate_names_from_metadata(meta, ["x86_64-unknown-linux-gnu"])
    assert drop == {"hermit-abi", "winapi"}


def test_filter_brs_drops_only_foreign(tmp_path: Path) -> None:
    manifest = tmp_path / "Cargo.toml"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("", encoding="utf-8")
    manifest.write_text(
        '[package]\nname="x"\nversion="0.1.0"\nedition="2021"\n'
        "[dependencies]\n"
        'serde = "1"\n'
        '[target."cfg(windows)".dependencies]\n'
        'winapi = "0.3"\n'
        '[target."cfg(unix)".dependencies]\n'
        'libc = "0.2"\n',
        encoding="utf-8",
    )
    brs = [
        "cargo-rpm-macros >= 24",
        "(crate(serde/default) >= 1 with crate(serde/default) < 2)",
        "(crate(winapi/default) >= 0.3.0 with crate(winapi/default) < 0.4.0~)",
        "(crate(libc) >= 0.2.0 with crate(libc) < 0.3.0~)",
    ]
    out = filter_foreign_target_crate_brs(
        brs, cargo_tomls=[manifest], arches={"x86_64"}
    )
    joined = "\n".join(out)
    assert "winapi" not in joined
    assert "libc" in joined
    assert "serde" in joined
    assert "cargo-rpm-macros" in joined
