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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Oreon Build Worker: polls controller, runs mock builds, uploads artifacts to R2.

DistGit support: accepts a Source(kind="distgit", url="<repo>.git#branch:spec_relpath") and builds via:
  mock --buildsrpm (spec + sources tree) -> SRPM
  mock --rebuild SRPM -> binary RPMs
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

from oreon_build.config import get_settings
from oreon_build.services.r2 import get_r2_client, log_r2_key, repo_rpms_key, src_r2_key
from oreon_build.services.signing import sign_rpm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _get_arch() -> str:
    m = platform.machine()
    if m in ("x86_64", "AMD64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m


def _upload_file_to_r2(r2_key: str, path: Path, content_type: str | None = None) -> None:
    r2 = get_r2_client()
    with open(path, "rb") as f:
        r2.upload_fileobj(r2_key, f, content_type=content_type)
    logger.info("Uploaded %s -> %s", path.name, r2_key)


def _upload_bytes_to_r2(r2_key: str, data: bytes, content_type: str | None = None) -> None:
    r2 = get_r2_client()
    r2.put_object(r2_key, data, content_type=content_type)


def _run(cmd: list[str], timeout_s: int, cwd: str | None = None) -> tuple[int, str]:
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd,
            env={**os.environ, "LANG": "C.UTF-8"},
        )
        out = (res.stdout or "") + (res.stderr or "")
        return res.returncode, out
    except FileNotFoundError:
        return 127, f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"Timed out running: {' '.join(cmd)}"


def _tail_lines(text: str, max_lines: int) -> str:
    if max_lines <= 0:
        return text
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _mock_logs_from_result_dir(result_dir: Path, max_lines: int) -> list[str]:
    """
    mock usually writes the detailed build output to files like build.log/root.log.
    We read those files (if present) instead of relying only on wrapper stdout/stderr.
    """
    # Order matters because the GUI shows only the tail of the uploaded log.
    # We want the most failure-relevant logs (build/root) to appear last.
    wanted_names = ["state.log", "config.log", "build.log", "root.log"]
    parts: list[str] = []
    used_paths: set[str] = set()

    for name in wanted_names:
        matches = sorted(result_dir.rglob(name))
        if not matches:
            continue
        # Prefer the "last" match (deeper nested setups can exist)
        p = matches[-1]
        p_key = str(p)
        if p_key in used_paths:
            continue
        used_paths.add(p_key)

        txt = _read_text_file(p)
        if not txt.strip():
            continue
        rel = ""
        try:
            rel = str(p.relative_to(result_dir))
        except Exception:
            rel = p.name

        txt = _tail_lines(txt, max_lines)
        parts.append(f"== mock log: {name} ({rel}) ==\n{txt.strip()}")

    # Fallback: if none of the known logs exist, grab up to a few *.log files.
    if not parts:
        generic = sorted(result_dir.rglob("*.log"))[:5]
        for p in generic:
            txt = _read_text_file(p)
            if not txt.strip():
                continue
            rel = ""
            try:
                rel = str(p.relative_to(result_dir))
            except Exception:
                rel = p.name
            txt = _tail_lines(txt, max_lines)
            parts.append(f"== mock log: {p.name} ({rel}) ==\n{txt.strip()}")

    return parts


def _parse_distgit_url(distgit_url: str) -> tuple[str, str | None, str | None]:
    repo = distgit_url
    branch = None
    spec_rel = None
    if "#" in distgit_url:
        repo, frag = distgit_url.split("#", 1)
        if ":" in frag:
            branch, spec_rel = frag.split(":", 1)
        else:
            branch = frag or None
    return repo, branch, spec_rel


def _clone_repo(repo: str, branch: str | None, dst: Path) -> tuple[bool, str]:
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["-b", branch]
    cmd += [repo, str(dst)]
    rc, out = _run(cmd, timeout_s=600)
    return rc == 0, out


def _maybe_git_lfs_pull(repo_dir: Path) -> tuple[int, str]:
    """Fetch Git LFS objects when .gitattributes uses filter=lfs (shallow clone may miss blobs)."""
    ga = repo_dir / ".gitattributes"
    try:
        if ga.is_file() and "filter=lfs" in ga.read_text(encoding="utf-8", errors="replace"):
            return _run(["git", "-C", str(repo_dir), "lfs", "pull"], timeout_s=1200)
    except OSError:
        pass
    return 0, ""


def _copy_local_spec_sources_listed_by_spectool(spec_path: Path, staging_dir: Path) -> tuple[bool, str]:
    """
    Copy Source/Patch files that live next to the spec (distgit layout).

    ``spectool -g`` only downloads *URL* sources. Local ``SourceN: foo`` and
    ``PatchN: bar`` lines must be copied into the mock --sources staging dir
    or rpmbuild fails with "Bad file: .../SOURCES/foo".
    """
    spec_dir = spec_path.parent
    staging_dir.mkdir(parents=True, exist_ok=True)

    rc, out = _run(
        ["spectool", "-l", str(spec_path.name)],
        timeout_s=120,
        cwd=str(spec_dir),
    )
    if rc != 0:
        return False, f"spectool -l failed (rc={rc}): {out[:2000]}"

    copied: list[str] = []
    missing: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        _, _, rest = line.partition(":")
        rest = rest.strip()
        if not rest:
            continue
        low = rest.lower()
        if low.startswith(("http://", "https://", "ftp://")):
            continue

        rel = rest.replace("\\", "/").lstrip("/")
        if not rel or rel.startswith("..") or "/.." in rel:
            continue

        src = spec_dir / rel
        if not src.is_file():
            # Often listed as basename only; try next to spec
            base = Path(rel).name
            alt = spec_dir / base
            if alt.is_file():
                src = alt
                rel = base
            else:
                missing.append(rest)
                continue

        dst = staging_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
        except OSError as e:
            return False, f"copy failed for {rel}: {e}"

    parts = [
        f"local Source/Patch files copied ({len(copied)}): "
        f"{', '.join(copied[:25])}{' ...' if len(copied) > 25 else ''}"
    ]
    if missing:
        parts.append("missing local files: " + ", ".join(missing[:30]))
        return False, "\n".join(parts)

    return True, "\n".join(parts)


def _prepare_mock_sources_with_spectool(spec_path: Path, staging_dir: Path) -> tuple[bool, str]:
    """
    Prepare sources for mock (spectool from rpmdevtools).

    Koji/COPR do the same: sources are prepared before mock. Koji uses lookaside;
    for URL-based Source lines, spectool is the standard tool (Fedora rpmdevtools).
    ``spectool -g`` fetches URL sources only; local distgit files are copied by
    :func:`_copy_local_spec_sources_listed_by_spectool`.
    Returns (ok, log).
    """
    spec_dir = spec_path.parent
    staging_dir.mkdir(parents=True, exist_ok=True)

    rc, out = _run(
        ["spectool", "-g", "-C", str(staging_dir), str(spec_path.name)],
        timeout_s=900,
        cwd=str(spec_dir),
    )

    if rc != 0:
        err = f"spectool -g failed (rc={rc}). Install rpmdevtools (spectool). Output:\n{out}"
        if rc == 127:
            err = (
                "spectool not found. Install rpmdevtools on the worker: sudo dnf install rpmdevtools. "
                " spectool downloads Source URLs and copies local Patch files for mock --buildsrpm."
            )
        return False, err

    files = sorted(staging_dir.rglob("*")) if staging_dir.is_dir() else []
    names = [f.name for f in files if f.is_file()]
    log_parts = [
        f"spectool -g fetched URL sources ({len(names)} files): "
        f"{', '.join(names[:15])}{' ...' if len(names) > 15 else ''}"
    ]

    ok_local, local_log = _copy_local_spec_sources_listed_by_spectool(spec_path, staging_dir)
    log_parts.append(local_log)
    if not ok_local:
        return False, "\n".join(log_parts)

    return True, "\n".join(log_parts)


def _mock_build_srpm_from_spec(mock_config: str, spec_path: Path, sources_dir: Path, result_dir: Path) -> tuple[bool, Path | None, str]:
    """
    Build SRPM from spec/sources using mock (so dependencies are handled in chroot).
    Produces *.src.rpm in result_dir.
    """
    cmd = [
        "mock",
        "-r",
        mock_config,
        "--buildsrpm",
        "--spec",
        str(spec_path),
        "--sources",
        str(sources_dir),
        "--resultdir",
        str(result_dir),
    ]
    rc, out = _run(cmd, timeout_s=3600)
    if rc != 0:
        return False, None, out
    srpms = sorted(result_dir.glob("*.src.rpm"))
    if not srpms:
        # mock sometimes nests outputs; fall back to recursive search
        srpms = sorted(result_dir.rglob("*.src.rpm"))
    if not srpms:
        return False, None, out + "\nNo .src.rpm produced by mock --buildsrpm."
    return True, srpms[0], out


def _mock_rebuild_srpm(mock_config: str, srpm_path: Path, result_dir: Path) -> tuple[bool, str]:
    cmd = ["mock", "-r", mock_config, "--rebuild", str(srpm_path), "--resultdir", str(result_dir)]
    rc, out = _run(cmd, timeout_s=7200)
    return rc == 0, out


def _process_job(controller_url: str, session: httpx.Client, job: dict) -> None:
    try:
        attempt_id = job["build_attempt_id"]
        releasename = job.get("releasename") or "oreon11"
        branch = job.get("branch") or "dev"
        arch = job.get("architecture") or "x86_64"
        sources = job.get("sources") or []
        mock_config = job.get("mock_config")
        log_key = log_r2_key(releasename, attempt_id)

        # Locate SRPM in R2 or distgit URL
        srpm_r2_key = None
        distgit_url = None
        for s in sources:
            if (s.get("kind") == "upload_srpm" or (s.get("r2_key") or "").endswith(".src.rpm")) and s.get("r2_key"):
                srpm_r2_key = s["r2_key"]
                break
            if s.get("kind") == "distgit" and s.get("url"):
                distgit_url = s["url"]

        if not mock_config:
            msg = "No mock config for this job (add mock environment to release)."
            _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
            session.post(
                f"{controller_url}/api/worker/result/{attempt_id}",
                json={"status": "failed", "log_r2_key": log_key, "error_message": msg},
            )
            return

        with tempfile.TemporaryDirectory(prefix="oreon-worker-") as tmpdir:
            tmp = Path(tmpdir)
            build_log_parts: list[str] = []
            log_max_lines = int(os.environ.get("OREON_WORKER_LOG_MAX_LINES", "40000"))

            srpm_path: Path | None = None

            if srpm_r2_key:
                r2 = get_r2_client()
                srpm_bytes = r2.get_object(srpm_r2_key)
                if not srpm_bytes:
                    msg = "SRPM not found in R2"
                    _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
                    session.post(
                        f"{controller_url}/api/worker/result/{attempt_id}",
                        json={"status": "failed", "log_r2_key": log_key, "error_message": msg},
                    )
                    return
                srpm_path = tmp / "source.src.rpm"
                srpm_path.write_bytes(srpm_bytes)
            elif distgit_url:
                repo, branch_name, spec_rel = _parse_distgit_url(distgit_url)
                if not spec_rel:
                    msg = "DistGit URL missing '#<branch>:<spec_relpath>' fragment"
                    _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
                    session.post(
                        f"{controller_url}/api/worker/result/{attempt_id}",
                        json={"status": "failed", "log_r2_key": log_key, "error_message": msg},
                    )
                    return
                git_dir = tmp / "distgit"
                ok, git_out = _clone_repo(repo, branch_name, git_dir)
                build_log_parts.append("== git clone ==\n" + git_out.strip())
                if not ok:
                    msg = "\n\n".join(build_log_parts).strip()
                    _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
                    session.post(
                        f"{controller_url}/api/worker/result/{attempt_id}",
                        json={"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
                    )
                    return
                lfs_rc, lfs_out = _maybe_git_lfs_pull(git_dir)
                if lfs_out.strip():
                    build_log_parts.append(
                        f"== git lfs pull (rc={lfs_rc}) ==\n{lfs_out.strip()}"
                    )
                spec_path = git_dir / spec_rel
                if not spec_path.is_file():
                    build_log_parts.append(f"Spec not found: {spec_rel}")
                    msg = "\n\n".join(build_log_parts).strip()
                    _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
                    session.post(
                        f"{controller_url}/api/worker/result/{attempt_id}",
                        json={"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
                    )
                    return

                sources_staging = tmp / "mock_sources_staged"
                src_ok, src_log = _prepare_mock_sources_with_spectool(
                    spec_path=spec_path,
                    staging_dir=sources_staging,
                )
                build_log_parts.append("== distgit sources for mock ==\n" + src_log)
                if not src_ok:
                    msg = "\n\n".join(build_log_parts).strip()
                    _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
                    session.post(
                        f"{controller_url}/api/worker/result/{attempt_id}",
                        json={"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
                    )
                    return

                srpm_result_dir = tmp / "srpm_result"
                srpm_result_dir.mkdir()
                ok, srpm_path, srpm_out = _mock_build_srpm_from_spec(
                    mock_config=mock_config,
                    spec_path=spec_path,
                    sources_dir=sources_staging,
                    result_dir=srpm_result_dir,
                )
                mock_log_parts = _mock_logs_from_result_dir(srpm_result_dir, max_lines=log_max_lines)
                has_build_or_root = any(("== mock log: build.log" in p) or ("== mock log: root.log" in p) for p in mock_log_parts)
                if mock_log_parts:
                    build_log_parts.extend(mock_log_parts)
                    # Some mock setups only emit state.log; if that's the case, wrapper stdout/stderr
                    # often contains the actual rpmbuild error we need.
                    if not ok and not has_build_or_root:
                        build_log_parts.append("== mock --buildsrpm (wrapper output on failure) ==\n" + srpm_out.strip())
                else:
                    # Fallback to wrapper output if mock didn't write log files.
                    build_log_parts.append("== mock --buildsrpm (wrapper output) ==\n" + srpm_out.strip())
                if not ok or not srpm_path:
                    msg = "\n\n".join(build_log_parts).strip()
                    _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
                    session.post(
                        f"{controller_url}/api/worker/result/{attempt_id}",
                        json={"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
                    )
                    return
            else:
                msg = "No SRPM or DistGit source in job (upload SRPM or configure DistGit spec path)."
                _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
                session.post(
                    f"{controller_url}/api/worker/result/{attempt_id}",
                    json={"status": "failed", "log_r2_key": log_key, "error_message": msg},
                )
                return

            result_dir = tmp / "result"
            result_dir.mkdir()
            ok, rebuild_out = _mock_rebuild_srpm(mock_config=mock_config, srpm_path=srpm_path, result_dir=result_dir)
            mock_log_parts = _mock_logs_from_result_dir(result_dir, max_lines=log_max_lines)
            has_build_or_root = any(("== mock log: build.log" in p) or ("== mock log: root.log" in p) for p in mock_log_parts)
            if mock_log_parts:
                build_log_parts.extend(mock_log_parts)
                if not ok and not has_build_or_root:
                    build_log_parts.append("== mock --rebuild (wrapper output on failure) ==\n" + rebuild_out.strip())
            else:
                build_log_parts.append("== mock --rebuild (wrapper output) ==\n" + rebuild_out.strip())
            full_log = "\n\n".join([p for p in build_log_parts if p]).strip() + "\n"

            _upload_bytes_to_r2(log_key, full_log.encode("utf-8"), content_type="text/plain")

            artifacts_payload = []
            for f in sorted(result_dir.rglob("*.rpm")):
                if not f.is_file():
                    continue
                fname = f.name
                signed = sign_rpm(f)
                if fname.endswith(".src.rpm"):
                    art_key = src_r2_key(releasename, branch, fname)
                else:
                    art_key = repo_rpms_key(releasename, branch, arch, fname)
                _upload_file_to_r2(art_key, f)
                artifacts_payload.append(
                    {"kind": "rpm", "filename": fname, "r2_key": art_key, "signed": signed}
                )

            session.post(
                f"{controller_url}/api/worker/result/{attempt_id}",
                json={
                    "status": "success" if ok else "failed",
                    "log_r2_key": log_key,
                    "error_message": None if ok else full_log[:2000],
                    "artifacts": artifacts_payload,
                },
            )
    except Exception:
        logger.exception("Unhandled error while processing job")


def main() -> None:
    settings = get_settings()
    worker_name = os.environ.get("OREON_WORKER_NAME", "worker-1")
    worker_token = os.environ.get("OREON_WORKER_TOKEN")
    if not worker_token:
        logger.error("OREON_WORKER_TOKEN not set")
        sys.exit(1)
    controller_url = settings.controller_url.rstrip("/")
    headers = {"X-Worker-Token": worker_token}
    session = httpx.Client(timeout=30.0, headers=headers)

    raw_max = os.environ.get("OREON_WORKER_MAX_JOBS", "4")
    try:
        max_jobs = int(raw_max)
    except ValueError:
        max_jobs = 4
    if max_jobs < 1:
        max_jobs = 1
    cpu = os.cpu_count() or 2
    hard_cap = max(1, min(8, cpu * 2))
    if max_jobs > hard_cap:
        logger.warning("Clamping OREON_WORKER_MAX_JOBS=%s to hard cap %s", max_jobs, hard_cap)
        max_jobs = hard_cap

    logger.info(
        "Worker %s starting (arch=%s), controller=%s, max_concurrent_jobs=%s",
        worker_name,
        _get_arch(),
        controller_url,
        max_jobs,
    )

    active: list[threading.Thread] = []
    last_heartbeat = 0.0

    while True:
        active = [t for t in active if t.is_alive()]

        now = time.time()
        if now - last_heartbeat > 30:
            try:
                session.post(f"{controller_url}/api/worker/heartbeat", json={})
            except Exception:
                logger.warning("Failed to send heartbeat", exc_info=True)
            last_heartbeat = now

        if len(active) >= max_jobs:
            time.sleep(2)
            continue

        try:
            resp = session.get(f"{controller_url}/api/worker/poll")
            resp.raise_for_status()
            data = resp.json()
            job = data.get("job")
            if not job:
                time.sleep(10)
                continue
            t = threading.Thread(target=_process_job, args=(controller_url, session, job), daemon=True)
            t.start()
            active.append(t)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.error("Invalid worker token")
                sys.exit(1)
            logger.warning("HTTP error during poll: %s", e)
            time.sleep(30)
        except Exception as e:
            logger.exception("Poll error: %s", e)
            time.sleep(30)


if __name__ == "__main__":
    main()
