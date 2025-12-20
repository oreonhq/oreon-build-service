"""
oreon-buildctl - CLI for Oreon Build Service.
Commands: create-package, trigger-build, list-builds, retry-build, logs, promote, publish, workers, repo-status, doctor.
"""
from __future__ import annotations

import os
import sys

import click

try:
    import httpx
except ImportError:
    httpx = None


def get_api_url() -> str:
    return os.environ.get("OREON_API_URL", "http://localhost:8000")


def get_token() -> str | None:
    return os.environ.get("OREON_TOKEN") or os.environ.get("OREON_ACCESS_TOKEN")


def api_request(method: str, path: str, json: dict | None = None, token: str | None = None) -> dict | list:
    url = get_api_url().rstrip("/") + "/api" + path
    headers = {}
    if token or get_token():
        headers["Authorization"] = "Bearer " + (token or get_token() or "")
    with httpx.Client(timeout=30.0) as client:
        r = client.request(method, url, json=json, headers=headers or None)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return {}


@click.group()
@click.option("--api-url", envvar="OREON_API_URL", default="http://localhost:8000", help="API base URL")
@click.pass_context
def cli(ctx, api_url):
    """Oreon Build Service control (oreon-buildctl)."""
    ctx.ensure_object(dict)
    ctx.obj["API_URL"] = api_url
    if httpx is None:
        click.echo("httpx is required for CLI. pip install httpx", err=True)
        sys.exit(1)


@cli.command("create-package")
@click.option("--name", required=True, help="Package name")
@click.option("--description", default=None)
@click.option("--gitlab-project-id", type=int, default=None)
@click.option("--gitlab-web-url", default=None)
@click.pass_context
def create_package(ctx, name, description, gitlab_project_id, gitlab_web_url):
    """Create a package."""
    token = get_token()
    if not token:
        click.echo("OREON_TOKEN or OREON_ACCESS_TOKEN required for this command", err=True)
        sys.exit(1)
    data = {"name": name}
    if description:
        data["description"] = description
    if gitlab_project_id is not None:
        data["gitlab_project_id"] = gitlab_project_id
    if gitlab_web_url:
        data["gitlab_web_url"] = gitlab_web_url
    out = api_request("POST", "/packages", json=data, token=token)
    click.echo("Created package: id=%s name=%s" % (out.get("id"), out.get("name")))


@cli.command("trigger-build")
@click.option("--package-id", type=int, required=True)
@click.option("--release-id", type=int, required=True)
@click.option("--package-version-id", type=int, default=None)
@click.option("--target-id", type=int, default=None)
@click.option("--priority", type=int, default=0)
@click.pass_context
def trigger_build(ctx, package_id, release_id, package_version_id, target_id, priority):
    """Trigger a build."""
    token = get_token()
    if not token:
        click.echo("OREON_TOKEN required", err=True)
        sys.exit(1)
    data = {"package_id": package_id, "release_id": release_id, "priority": priority}
    if package_version_id is not None:
        data["package_version_id"] = package_version_id
    if target_id is not None:
        data["target_id"] = target_id
    out = api_request("POST", "/builds/trigger", json=data, token=token)
    click.echo("Build triggered: job id=%s" % out.get("id"))


@cli.command("list-builds")
@click.option("--release-id", type=int, default=None)
@click.option("--package-id", type=int, default=None)
@click.option("--status", default=None)
@click.option("--limit", type=int, default=50)
def list_builds(release_id, package_id, status, limit):
    """List build jobs."""
    path = "/builds?limit=%d" % limit
    if release_id is not None:
        path += "&release_id=%d" % release_id
    if package_id is not None:
        path += "&package_id=%d" % package_id
    if status:
        path += "&status=" + status
    out = api_request("GET", path)
    for j in out:
        click.echo("  %s  package=%s release=%s  status=%s  created=%s" % (j["id"], j.get("package_id"), j.get("release_id"), j.get("status"), (j.get("created_at") or "")[:19]))


@cli.command("retry-build")
@click.argument("job_id", type=int)
@click.pass_context
def retry_build(ctx, job_id):
    """Retry a build job."""
    token = get_token()
    if not token:
        click.echo("OREON_TOKEN required", err=True)
        sys.exit(1)
    api_request("POST", "/builds/jobs/%d/retry" % job_id, token=token)
    click.echo("Retry requested for job %s" % job_id)


@cli.command("logs")
@click.argument("attempt_id", type=int)
def logs(attempt_id):
    """Show build log (tail) for an attempt."""
    out = api_request("GET", "/logs/attempts/%d/tail?lines=200" % attempt_id)
    for line in out.get("lines", []):
        click.echo(line)


@cli.command("promote")
@click.option("--release-id", type=int, required=True)
@click.option("--from-channel", required=True)
@click.option("--to-channel", required=True)
@click.option("--package-name", default=None)
@click.option("--build-job-id", type=int, default=None)
@click.pass_context
def promote(ctx, release_id, from_channel, to_channel, package_name, build_job_id):
    """Promote packages between channels."""
    token = get_token()
    if not token:
        click.echo("OREON_TOKEN required", err=True)
        sys.exit(1)
    data = {"release_id": release_id, "from_channel": from_channel, "to_channel": to_channel}
    if package_name:
        data["package_name"] = package_name
    if build_job_id is not None:
        data["build_job_id"] = build_job_id
    out = api_request("POST", "/promotions/promote", json=data, token=token)
    click.echo("Promotion created: id=%s" % out.get("id"))


@cli.command("publish")
@click.option("--release-id", type=int, required=True)
@click.option("--channel", required=True)
@click.option("--architecture", required=True)
@click.pass_context
def publish(ctx, release_id, channel, architecture):
    """Trigger repository publish (compose and upload to R2)."""
    token = get_token()
    if not token:
        click.echo("OREON_TOKEN required", err=True)
        sys.exit(1)
    click.echo("Publish is triggered via API or scheduler; use repo-status to check.")


@cli.command("workers")
def workers():
    """List workers."""
    out = api_request("GET", "/workers")
    for w in out:
        click.echo("  %s  %s  %s  %s" % (w["id"], w.get("name"), w.get("state"), w.get("last_seen_at") or "-"))


@cli.command("repo-status")
def repo_status():
    """Show repository status (R2 layout)."""
    out = api_request("GET", "/repos/status")
    import json
    click.echo(json.dumps(out, indent=2))


@cli.command("doctor")
@click.pass_context
def doctor(ctx):
    """Check connectivity and config."""
    url = get_api_url()
    click.echo("API URL: %s" % url)
    try:
        r = api_request("GET", "/releases")
        click.echo("Releases: %d" % len(r))
    except Exception as e:
        click.echo("Error: %s" % e, err=True)
        sys.exit(1)
    token = get_token()
    click.echo("Token: %s" % ("set" if token else "not set"))
    click.echo("OK")
