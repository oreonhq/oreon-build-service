from __future__ import annotations

from typing import Any

from oreon_build.cli.orbscli import get_api_url, get_token, http_json, require_auth, require_maintainer, resolve_release_id


def _auth() -> tuple[str, str]:
    token = require_auth(get_token())
    require_maintainer(token)
    return get_api_url(), token


def list_releases() -> list[dict[str, Any]]:
    api, token = _auth()
    out = http_json("GET", api, "/api/releases", token=token, params={"limit": 1000, "offset": 0})
    return list(out.get("items") or [])


def list_side_tags(release_id: int) -> list[dict[str, Any]]:
    api, token = _auth()
    out = http_json(
        "GET",
        api,
        "/api/side-tags",
        token=token,
        params={"release_id": release_id, "limit": 200, "offset": 0},
    )
    return list(out.get("items") or [])


def create_side_tag(release_id: int, name: str) -> dict[str, Any]:
    api, token = _auth()
    return http_json(
        "POST",
        api,
        "/api/side-tags",
        token=token,
        json_body={"release_id": release_id, "name": name},
    )


def trigger_discovery(
    *,
    packages: list[str],
    release_id: int,
    side_tag_id: int,
    arches: list[str],
    distgit_url: str,
    distgit_branch: str,
    mock_environment_id: int | None = None,
) -> dict[str, Any]:
    api, token = _auth()
    body: dict[str, Any] = {
        "packages": packages,
        "release_id": release_id,
        "side_tag_id": side_tag_id,
        "architectures": arches,
        "distgit_url": distgit_url,
        "distgit_branch": distgit_branch,
    }
    if mock_environment_id is not None:
        body["preferred_mock_environment_id"] = mock_environment_id
    return http_json("POST", api, "/api/builds/discovery-trigger", token=token, json_body=body, timeout=300.0)


def trigger_chain(
    *,
    chain: str,
    release_id: int,
    side_tag_id: int,
    arches: list[str],
    distgit_url: str,
    distgit_branch: str,
    mock_environment_id: int | None = None,
) -> dict[str, Any]:
    api, token = _auth()
    body: dict[str, Any] = {
        "chain": chain,
        "release_id": release_id,
        "side_tag_id": side_tag_id,
        "architectures": arches,
        "distgit_url": distgit_url,
        "distgit_branch": distgit_branch,
        "maintenance": True,
    }
    if mock_environment_id is not None:
        body["preferred_mock_environment_id"] = mock_environment_id
    return http_json("POST", api, "/api/builds/chain-trigger", token=token, json_body=body, timeout=300.0)


def plan_status(plan_id: int) -> dict[str, Any]:
    api, token = _auth()
    return http_json("GET", api, "/api/builds/chain/%s/status" % plan_id, token=token)


def discovery_status(plan_id: int) -> dict[str, Any]:
    api, token = _auth()
    return http_json("GET", api, "/api/builds/discovery/%s/status" % plan_id, token=token)


def bootstrap_job(job_id: int) -> dict[str, Any]:
    api, token = _auth()
    return http_json("POST", api, "/api/builds/jobs/%s/bootstrap" % job_id, token=token)


def active_maintenance() -> list[dict[str, Any]]:
    api, token = _auth()
    try:
        out = http_json("GET", api, "/api/builds/maintenance/active", token=token)
    except Exception:
        return []
    if isinstance(out, list):
        return out
    return list(out.get("items") or [])


def cancel_plan(plan_id: int) -> dict[str, Any]:
    api, token = _auth()
    return http_json("POST", api, "/api/builds/chain/%s/cancel" % plan_id, token=token)
