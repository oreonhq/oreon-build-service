"""GCP Compute Engine helpers for burst worker VM lifecycle management.

Uses the `gcloud` CLI via subprocess so authentication is handled by
Application Default Credentials on the host (no extra Python deps needed).
All operations are fire-and-forget; failures are logged but never propagate
to the caller so a GCP error never breaks the build queue.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _resolve_project_and_zone(project: str | None, zone: str | None) -> tuple[str | None, str]:
    from oreon_build.config import get_settings

    s = get_settings()
    resolved_project = project or s.gcp_project_id
    resolved_zone = zone or s.gcp_zone
    return resolved_project, resolved_zone


async def _run_gcloud(*args: str) -> bool:
    """Run a gcloud command. Returns True on success, False on error."""
    cmd = ["gcloud", *args, "--quiet"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            return True
        logger.warning(
            "gcloud %s exited %s: %s",
            " ".join(args),
            proc.returncode,
            (stderr or b"").decode(errors="replace").strip(),
        )
        return False
    except FileNotFoundError:
        logger.warning("gcloud CLI not found; cannot manage burst worker VM")
        return False
    except asyncio.TimeoutError:
        logger.warning("gcloud %s timed out after 120s", " ".join(args))
        return False
    except Exception as exc:
        logger.warning("gcloud %s failed: %s", " ".join(args), exc)
        return False


async def start_instance(instance_name: str, *, zone: str | None = None, project: str | None = None) -> None:
    """Start a GCP Compute Engine instance. Fire-and-forget; never raises."""
    resolved_project, resolved_zone = _resolve_project_and_zone(project, zone)
    logger.info("burst-vm: starting GCP instance %s (zone=%s project=%s)", instance_name, resolved_zone, resolved_project)
    cmd_args = ["compute", "instances", "start", instance_name, f"--zone={resolved_zone}"]
    if resolved_project:
        cmd_args.append(f"--project={resolved_project}")
    ok = await _run_gcloud(*cmd_args)
    if ok:
        logger.info("burst-vm: instance %s started successfully", instance_name)


async def stop_instance(instance_name: str, *, zone: str | None = None, project: str | None = None) -> None:
    """Stop a GCP Compute Engine instance. Fire-and-forget; never raises."""
    resolved_project, resolved_zone = _resolve_project_and_zone(project, zone)
    logger.info("burst-vm: stopping GCP instance %s (zone=%s project=%s)", instance_name, resolved_zone, resolved_project)
    cmd_args = ["compute", "instances", "stop", instance_name, f"--zone={resolved_zone}"]
    if resolved_project:
        cmd_args.append(f"--project={resolved_project}")
    ok = await _run_gcloud(*cmd_args)
    if ok:
        logger.info("burst-vm: instance %s stopped successfully", instance_name)
