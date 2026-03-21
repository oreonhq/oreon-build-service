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
Oreon Build Worker: polls controller, runs mock builds, uploads build logs to R2.

Built RPMs are POSTed to the controller (`/api/worker/upload-rpm/...`); the controller GPG-signs
and writes them to R2. No signing key or `rpm-sign` is required on workers.

DistGit: Source(kind="distgit", url="<repo>.git#branch:spec_relpath") — mock --buildsrpm then mock --rebuild.
"""

from __future__ import annotations

import logging
import os
import re
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
from oreon_build.services.r2 import get_r2_client, log_r2_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _get_arch() -> str:
    m = platform.machine()
    if m in ("x86_64", "AMD64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m


def _upload_bytes_to_r2(r2_key: str, data: bytes, content_type: str | None = None) -> None:
    r2 = get_r2_client()
    r2.put_object(r2_key, data, content_type=content_type)


def _upload_rpm_to_controller(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    rpm_path: Path,
) -> dict:
    """POST RPM bytes to controller; controller signs and stores in R2. Returns artifact metadata dict."""
    upload_timeout = httpx.Timeout(7200.0, connect=120.0)
    with open(rpm_path, "rb") as fp:
        files = {"file": (rpm_path.name, fp, "application/x-rpm")}
        r = session.post(
            f"{controller_url}/api/worker/upload-rpm/{attempt_id}",
            files=files,
            timeout=upload_timeout,
        )
    r.raise_for_status()
    return r.json()


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


# Exit code when mock is killed because the user cancelled the build (worker convention)
_RC_MOCK_CANCELLED = 130


def _run_with_abort(
    cmd: list[str],
    timeout_s: int,
    abort_event: threading.Event | None,
    cwd: str | None = None,
) -> tuple[int, str]:
    """Like _run but kills the child process when abort_event is set (user cancelled build)."""
    if not abort_event:
        return _run(cmd, timeout_s, cwd)
    env = {**os.environ, "LANG": "C.UTF-8"}
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError:
        return 127, f"Command not found: {cmd[0]}"
    start = time.monotonic()
    while True:
        if abort_event.is_set():
            proc.kill()
            try:
                proc.wait(timeout=60)
            except Exception:
                pass
            out = ""
            try:
                stdout, stderr = proc.communicate(timeout=10)
                out = (stdout or "") + (stderr or "")
            except Exception:
                pass
            return _RC_MOCK_CANCELLED, out + "\n== Build cancelled by user (mock killed) =="

        ret = proc.poll()
        if ret is not None:
            stdout, stderr = proc.communicate()
            return ret, (stdout or "") + (stderr or "")

        if time.monotonic() - start > timeout_s:
            proc.kill()
            try:
                proc.wait(timeout=30)
            except Exception:
                pass
            return 124, f"Timed out running: {' '.join(cmd)}"
        time.sleep(0.5)


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


def _read_mock_log_tail(result_dir: Path, log_basename: str, max_lines: int) -> str:
    """Read the newest ``log_basename`` under result_dir (e.g. build.log) and return a tailed body."""
    matches = sorted(result_dir.rglob(log_basename))
    if not matches:
        return ""
    txt = _read_text_file(matches[-1])
    if not txt.strip():
        return ""
    return _tail_lines(txt, max_lines).strip()


# mock's Python logging: "DEBUG util.py:461:  message" (note: space after DEBUG, not "DEBUG:")
_RE_MOCK_PY_DEBUG = re.compile(r"^\s*DEBUG\s+(\S+\.py):(\d+):\s*(.*)$")
_RE_MOCK_PY_INFO = re.compile(r"^\s*INFO\s+(\S+\.py):(\d+):\s*(.*)$")
_RE_MOCK_PY_WARNING = re.compile(r"^\s*WARNING\s+(\S+\.py):(\d+):\s*(.*)$")

# Substrings of mock/dnf housekeeping lines (drop whole line or DEBUG-wrapped message)
_MOCK_INFRA_SNIPPETS = (
    "child environment:",
    "executing command:",
    "child return code was:",
    "kill orphans in chroot",
    "removing intree",
    "umount",
    "unmounting",
)

# Lines in build.log that usually indicate failure (for tail-first UIs)
_RE_BUILD_ERROR_HINT = re.compile(
    r"(?i)(\berror:\b|\bfatal\b|\*\*\*\s|BUILD FAILED|RPM build errors|recipe failed|"
    r"undefined reference|incorrect format|no such file|cannot find|command not found|"
    r"make\[\d+\]: \*\*\*|error TS|error: |error \d+|traceback|\bFAILED\b)"
)


def _sanitize_mock_log_line(line: str) -> str | None:
    """
    Normalize or drop a single log line. Mock emits ``DEBUG util.py:N: ...``; we keep only the
    message after the colon when it is not pure infrastructure noise. Returns None to omit the line.
    """
    raw = line.rstrip("\n")
    stripped = raw.strip()
    if not stripped:
        return None

    low = stripped.lower()
    for snip in _MOCK_INFRA_SNIPPETS:
        if snip in low:
            return None

    if stripped.startswith("INFO:") or stripped.startswith("DEBUG:"):
        return None

    for rx in (_RE_MOCK_PY_DEBUG, _RE_MOCK_PY_INFO, _RE_MOCK_PY_WARNING):
        m = rx.match(raw)
        if not m:
            continue
        msg = (m.group(3) or "").strip()
        if not msg:
            return None
        mlow = msg.lower()
        for snip in _MOCK_INFRA_SNIPPETS:
            if snip in mlow:
                return None
        # Most INFO/WARNING from mock's util.py is noise; keep if it looks actionable
        if rx is _RE_MOCK_PY_INFO or rx is _RE_MOCK_PY_WARNING:
            if not _RE_BUILD_ERROR_HINT.search(msg):
                return None
        return msg

    return stripped


def _sanitize_mock_output(text: str) -> str:
    """Strip mock Python DEBUG/INFO noise, dedupe consecutive identical lines."""
    out: list[str] = []
    for line in text.splitlines():
        cleaned = _sanitize_mock_log_line(line)
        if cleaned is None:
            continue
        out.append(cleaned)
    deduped: list[str] = []
    prev: str | None = None
    for L in out:
        if L == prev:
            continue
        deduped.append(L)
        prev = L
    return "\n".join(deduped).strip()


def _extract_build_error_highlights(text: str, max_lines: int = 80) -> str:
    """Pull likely error lines from a full (sanitized) build log for tail-first web UIs."""
    seen: set[str] = set()
    hits: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if len(s) < 4:
            continue
        if not _RE_BUILD_ERROR_HINT.search(s):
            continue
        if s in seen:
            continue
        seen.add(s)
        hits.append(s)
        if len(hits) >= max_lines:
            break
    return "\n".join(hits).strip()


def _format_build_log_display(
    result_dir: Path,
    *,
    max_tail_lines: int,
    failed_or_cancelled: bool,
    max_read_bytes: int = 5 * 1024 * 1024,
) -> str:
    """
    Prefer **build.log** (rpmbuild). On failure/cancel, scan a capped full file for error lines
    and append ``## Detected errors`` so dashboards that only show the log *tail* still see causes.
    """
    matches = sorted(result_dir.rglob("build.log"))
    if not matches:
        return ""
    path = matches[-1]
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if len(raw) > max_read_bytes:
        raw = raw[:max_read_bytes]
    full = raw.decode("utf-8", errors="replace")
    if not full.strip():
        return ""

    sanitized_full = _sanitize_mock_output(full)
    tail = _tail_lines(sanitized_full, max_tail_lines).strip()

    if not failed_or_cancelled:
        return tail

    highlights = _extract_build_error_highlights(sanitized_full)
    if highlights and highlights not in tail:
        return f"{tail}\n\n## Detected errors (from full build.log)\n{highlights}"
    return tail


def _mock_stage_log_sections(
    result_dir: Path,
    *,
    stage_title: str,
    mock_rc_ok: bool,
    cancelled: bool,
    wrapper_output: str,
    max_build_log_lines: int,
    chroot_tail_lines: int = 120,
) -> list[str]:
    """
    Build user-facing log sections from a mock ``--resultdir``.

    Primary content is **build.log** (rpmbuild output inside the chroot). We omit state.log,
    config.log, and full root.log to avoid chroot/dnf noise. On failure before rpmbuild runs,
    a short tail of root.log is included only when build.log is empty.
    """
    sections: list[str] = []
    failed_or_cancelled = (not mock_rc_ok) or cancelled
    build_txt = _format_build_log_display(
        result_dir,
        max_tail_lines=max_build_log_lines,
        failed_or_cancelled=failed_or_cancelled,
    )
    wrapper_clean = _sanitize_mock_output(wrapper_output)

    if build_txt:
        sections.append(f"## {stage_title}\n{build_txt}")

    if cancelled:
        if wrapper_clean:
            sections.append(f"## {stage_title} — cancelled\n{wrapper_clean}")
        return sections

    if not mock_rc_ok:
        if not build_txt and chroot_tail_lines > 0:
            root_raw = _read_mock_log_tail(result_dir, "root.log", chroot_tail_lines)
            root_tail = _sanitize_mock_output(root_raw) if root_raw else ""
            if root_tail:
                sections.append(
                    f"## {stage_title} — setup failed before rpmbuild (root.log tail)\n{root_tail}"
                )
        if wrapper_clean:
            sections.append(f"## {stage_title} — mock\n{wrapper_clean}")
    elif not build_txt and wrapper_clean:
        sections.append(f"## {stage_title}\n{wrapper_clean}")

    return sections


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
    cmd = ["git", "clone", "--quiet", "--depth", "1"]
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
            return _run(["git", "-C", str(repo_dir), "lfs", "pull", "-q"], timeout_s=1200)
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

    shown = copied[:8]
    suffix = " …" if len(copied) > 8 else ""
    parts = [f"Local Source/Patch: {len(copied)} file(s){(': ' + ', '.join(shown) + suffix) if copied else ''}"]
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
    head = ", ".join(names[:6])
    more = f" (+{len(names) - 6} more)" if len(names) > 6 else ""
    log_parts = [f"URL sources via spectool: {len(names)} file(s){(': ' + head + more) if names else ''}"]

    ok_local, local_log = _copy_local_spec_sources_listed_by_spectool(spec_path, staging_dir)
    log_parts.append(local_log)
    if not ok_local:
        return False, "\n".join(log_parts)

    return True, "\n".join(log_parts)


def _mock_build_srpm_from_spec(
    mock_config: str,
    spec_path: Path,
    sources_dir: Path,
    result_dir: Path,
    abort_event: threading.Event | None = None,
    mock_unique_ext: str | None = None,
) -> tuple[bool, Path | None, str, bool]:
    """
    Build SRPM from spec/sources using mock (so dependencies are handled in chroot).
    Produces *.src.rpm in result_dir.
    Returns (ok, srpm_path, output, cancelled).

    ``mock_unique_ext`` must be set per concurrent build on this host (e.g. attempt id);
    otherwise mock shares one chroot per ``-r`` config and parallel jobs fail with
    "Build root is locked by another process."
    """
    cmd: list[str] = ["mock", "-r", mock_config]
    if mock_unique_ext:
        cmd += ["--uniqueext", mock_unique_ext]
    cmd += [
        "--buildsrpm",
        "--spec",
        str(spec_path),
        "--sources",
        str(sources_dir),
        "--resultdir",
        str(result_dir),
    ]
    rc, out = _run_with_abort(cmd, timeout_s=3600, abort_event=abort_event)
    if rc == _RC_MOCK_CANCELLED:
        return False, None, out, True
    if rc != 0:
        return False, None, out, False
    srpms = sorted(result_dir.glob("*.src.rpm"))
    if not srpms:
        # mock sometimes nests outputs; fall back to recursive search
        srpms = sorted(result_dir.rglob("*.src.rpm"))
    if not srpms:
        return False, None, out + "\nNo .src.rpm produced by mock --buildsrpm.", False
    return True, srpms[0], out, False


def _mock_rebuild_srpm(
    mock_config: str,
    srpm_path: Path,
    result_dir: Path,
    abort_event: threading.Event | None = None,
    mock_unique_ext: str | None = None,
) -> tuple[bool, str, bool]:
    """Returns (ok, output, cancelled). See :func:`_mock_build_srpm_from_spec` for ``mock_unique_ext``."""
    cmd: list[str] = ["mock", "-r", mock_config]
    if mock_unique_ext:
        cmd += ["--uniqueext", mock_unique_ext]
    cmd += ["--rebuild", str(srpm_path), "--resultdir", str(result_dir)]
    rc, out = _run_with_abort(cmd, timeout_s=7200, abort_event=abort_event)
    if rc == _RC_MOCK_CANCELLED:
        return False, out, True
    return rc == 0, out, False


def _process_job(controller_url: str, session: httpx.Client, job: dict) -> None:
    done_event = threading.Event()
    abort_event = threading.Event()
    try:
        attempt_id = job["build_attempt_id"]

        def poll_cancel() -> None:
            while not done_event.is_set():
                try:
                    r = session.get(
                        f"{controller_url}/api/worker/cancel-check/{attempt_id}",
                        timeout=15.0,
                    )
                    if r.status_code == 200 and r.json().get("cancel_requested"):
                        abort_event.set()
                        return
                except Exception:
                    pass
                time.sleep(5)

        threading.Thread(target=poll_cancel, daemon=True, name="oreon-cancel-poller").start()

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

        # One chroot lock per mock "-r" config; parallel threads need distinct roots.
        mock_unique_ext = f"oreon-{attempt_id}"

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
                if git_out.strip():
                    build_log_parts.append("## Git clone\n" + git_out.strip())
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
                    build_log_parts.append(f"## Git LFS (rc={lfs_rc})\n{lfs_out.strip()}")
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
                build_log_parts.append("## Source prep\n" + src_log)
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
                ok, srpm_path, srpm_out, srpm_cancelled = _mock_build_srpm_from_spec(
                    mock_config=mock_config,
                    spec_path=spec_path,
                    sources_dir=sources_staging,
                    result_dir=srpm_result_dir,
                    abort_event=abort_event,
                    mock_unique_ext=mock_unique_ext,
                )
                build_log_parts.extend(
                    _mock_stage_log_sections(
                        srpm_result_dir,
                        stage_title="SRPM build (mock --buildsrpm)",
                        mock_rc_ok=ok,
                        cancelled=srpm_cancelled,
                        wrapper_output=srpm_out,
                        max_build_log_lines=log_max_lines,
                    )
                )
                if srpm_cancelled:
                    msg = "\n\n".join(build_log_parts).strip()
                    _upload_bytes_to_r2(log_key, msg.encode("utf-8"), content_type="text/plain")
                    session.post(
                        f"{controller_url}/api/worker/result/{attempt_id}",
                        json={
                            "status": "cancelled",
                            "log_r2_key": log_key,
                            "error_message": "Build cancelled by user",
                            "artifacts": [],
                        },
                    )
                    return
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
            ok, rebuild_out, rebuild_cancelled = _mock_rebuild_srpm(
                mock_config=mock_config,
                srpm_path=srpm_path,
                result_dir=result_dir,
                abort_event=abort_event,
                mock_unique_ext=mock_unique_ext,
            )
            build_log_parts.extend(
                _mock_stage_log_sections(
                    result_dir,
                    stage_title="Binary RPM build (mock --rebuild)",
                    mock_rc_ok=ok,
                    cancelled=rebuild_cancelled,
                    wrapper_output=rebuild_out,
                    max_build_log_lines=log_max_lines,
                )
            )
            if rebuild_cancelled:
                full_log = "\n\n".join([p for p in build_log_parts if p]).strip() + "\n"
                _upload_bytes_to_r2(log_key, full_log.encode("utf-8"), content_type="text/plain")
                session.post(
                    f"{controller_url}/api/worker/result/{attempt_id}",
                    json={
                        "status": "cancelled",
                        "log_r2_key": log_key,
                        "error_message": "Build cancelled by user",
                        "artifacts": [],
                    },
                )
                return
            full_log = "\n\n".join([p for p in build_log_parts if p]).strip() + "\n"

            _upload_bytes_to_r2(log_key, full_log.encode("utf-8"), content_type="text/plain")

            artifacts_payload = []
            for f in sorted(result_dir.rglob("*.rpm")):
                if not f.is_file():
                    continue
                fname = f.name
                try:
                    meta = _upload_rpm_to_controller(session, controller_url, attempt_id, f)
                except Exception as upload_err:
                    err_msg = f"Failed to upload RPM to controller ({fname}): {upload_err}"
                    logger.exception("%s", err_msg)
                    fail_log = full_log + "\n\n== " + err_msg + "\n"
                    _upload_bytes_to_r2(log_key, fail_log.encode("utf-8"), content_type="text/plain")
                    session.post(
                        f"{controller_url}/api/worker/result/{attempt_id}",
                        json={
                            "status": "failed",
                            "log_r2_key": log_key,
                            "error_message": fail_log[:2000],
                            "artifacts": artifacts_payload,
                        },
                    )
                    return
                artifacts_payload.append(
                    {
                        "kind": "rpm",
                        "filename": meta.get("filename") or fname,
                        "r2_key": meta["r2_key"],
                        "signed": bool(meta.get("signed")),
                        "size_bytes": meta.get("size_bytes"),
                        "checksum_sha256": meta.get("checksum_sha256"),
                    }
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
    finally:
        done_event.set()


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
