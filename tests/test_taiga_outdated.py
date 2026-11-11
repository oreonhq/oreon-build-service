# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

from oreon_build.services.taiga_outdated import issue_body, issue_subject, match_assignees, parse_groups


SAMPLE = """
# comment
[python]
summary: stuff
maintainer: Brandon Lester, ada
packages: python3, python3-wheel

[misc]
maintainer: nobody
packages: curl
"""


def test_parse_groups_maps_packages_to_maintainers() -> None:
    groups = parse_groups(SAMPLE)
    assert groups["python3"] == ["Brandon Lester", "ada"]
    assert groups["curl"] == ["nobody"]


def test_match_assignees_uses_username_or_full_name() -> None:
    users = [
        {"id": 4, "username": "ada", "full_name": "Ada Lovelace"},
        {"id": 9, "username": "blester", "full_name": "Brandon Lester"},
        {"id": 1, "username": "other", "full_name": "Other Person"},
    ]
    assert match_assignees(["Brandon Lester", "ada"], users) == [4, 9]
    assert match_assignees(["missing"], users) == []


def test_issue_text_is_short() -> None:
    assert issue_subject("mesa") == "mesa is outdated"
    body = issue_body("mesa", "24.1.0", "24.2.8")
    assert "we still have 24.1.0" in body
    assert body.endswith("latest upstream\n24.2.8\n")
