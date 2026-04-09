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
Oreon Build Worker: polls controller, runs mock builds, uploads build logs via the controller API.

Built RPMs are posted to `/api/worker/upload-rpm/...`. Optional PE / `*.ko` signing streams NSS from
``GET /api/worker/pesign-bundle/{attempt}`` into RAM, extracts only under ``/dev/shm`` (tmpfs) when
possible, then deletes that tree as soon as signing finishes. The raw tarball is never written to disk.
The controller GPG-signs and writes RPMs to R2.

If mock leaves RPMs under ``--resultdir`` owned by root with restrictive modes, the worker can hit
``PermissionError`` when scanning or uploading artifacts. Use ``OREON_WORKER_CHMOD_MOCK_RESULTDIR``
and/or ``OREON_WORKER_CHMOD_MOCK_RESULTDIR_SUDO`` (see ``deploy/oreon-worker.env.example``).

After each job, the worker runs ``mock --scrub=all`` for that job’s ``--uniqueext``, then removes only
that attempt’s trees under ``/var/lib/mock`` (directory names ending in ``-oreon-<attempt_id>``), the
attempt’s mock cache under ``/var/cache/mock/oreon-<attempt_id>/`` (see ``--config-opts cache_topdir``),
then removes the job tempdir (prefix ``oreon-worker-<attempt_id>-``) so parallel jobs are not touched.
Stale legacy ``oreon-worker-*`` dirs are pruned by age (see deploy docs).

DistGit: Source(kind="distgit", url="<repo>.git#branch:spec_relpath") — mock --buildsrpm then mock --rebuild.

Poll may include ``mock_additional_repos`` (base URLs) from the job mock environment. Each becomes ``mock --addrepo``.
Optional ``OREON_WORKER_EXTRA_MOCK_ADDREPO`` appends comma-separated URLs on the worker.
During long ``mock`` runs, optional ``OREON_WORKER_LIVE_LOG_INTERVAL_SEC`` (default 60, 0 disables)
uploads a tail of ``build.log`` so the web UI can show progress before the build finishes.
By default the worker passes ``mock --define dist .orN`` where ``N`` is the first run of digits in the job
``releasename`` (e.g. ``oreon-11-rp1`` gives ``.or11``) so RPM names use ``orN`` instead of the chroot
default like ``fc45``. See ``OREON_WORKER_RPM_DIST`` and ``OREON_WORKER_SKIP_RPM_DIST_OVERRIDE`` in deploy docs.

Each build thread opens its own ``httpx.Client`` (same auth headers as poll). The poll/heartbeat loop keeps a separate client so nothing shares a sync client with mock (multi-hour jobs were seeing broken pipes from pooled stale connections and from unsafe concurrent use).
Transient ``ReadError`` / broken pipe on controller API calls is retried (see ``OREON_WORKER_HTTP_RETRIES``).
RPM uploads default to presigned PUT to R2 plus a small API ``complete`` call (``OREON_WORKER_RPM_UPLOAD_VIA_PRESIGNED_R2`` defaults on). Build logs default the same way (``OREON_WORKER_BUILD_LOG_VIA_PRESIGNED_R2``) to avoid Cloudflare 524 while the origin ingests huge ``PUT`` bodies. Disable with ``=0`` to use legacy tunnel POSTs.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import logging
import os
import re
import platform
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

import httpx

from oreon_build.services.signing import (
    PesignRepackError,
    PesignRuntimeConfig,
    pesign_rpm_if_configured,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Ignore a second dispatch for the same build_attempt_id (duplicate poll / misbehaving client).
_inflight_attempt_lock = threading.Lock()
_inflight_attempt_ids: set[int] = set()
# Active work-tmpdir paths (same process, multi-thread safety).
# Belt over the attempt-id inflight set: the prune checks this set directly so it never
# deletes a directory that a live thread is still writing to, even under unusual conditions.
_active_work_tmpdir_paths: set[str] = set()

T = TypeVar("T")


def _http_retry_attempts_and_base() -> tuple[int, float]:
    raw_n = (os.environ.get("OREON_WORKER_HTTP_RETRIES") or "5").strip()
    raw_d = (os.environ.get("OREON_WORKER_HTTP_RETRY_BASE_SEC") or "2").strip()
    try:
        n = int(raw_n)
    except ValueError:
        n = 5
    try:
        base = float(raw_d)
    except ValueError:
        base = 2.0
    return max(1, min(n, 12)), max(0.5, min(base, 60.0))


def _is_transient_http_error(exc: BaseException) -> bool:
    transient_types: tuple[type[BaseException], ...] = (
        httpx.ReadError,
        httpx.WriteError,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ConnectTimeout,
    )
    if hasattr(httpx, "PoolTimeout"):
        transient_types += (getattr(httpx, "PoolTimeout"),)
    if isinstance(exc, transient_types):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        if resp is not None:
            sc = resp.status_code
            # Cloudflare 522/524 (origin down / timeout), plus generic gateway overload.
            if sc in (408, 425, 429, 500, 502, 503, 504, 522, 523, 524):
                return True
    if isinstance(exc, OSError) and exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ETIMEDOUT):
        return True
    unwrapped: BaseException | None = exc
    for _ in range(6):
        c = getattr(unwrapped, "__cause__", None)
        if c is None:
            c = getattr(unwrapped, "__context__", None)
        if isinstance(c, OSError) and c.errno in (errno.EPIPE, errno.ECONNRESET):
            return True
        if not isinstance(c, BaseException):
            break
        unwrapped = c
    return False


def _with_http_retries(label: str, fn: Callable[[], T]) -> T:
    """Retry on dead pooled connections (common after multi-hour mock) and similar transport glitches."""
    attempts, base = _http_retry_attempts_and_base()
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i + 1 >= attempts or not _is_transient_http_error(e):
                raise
            delay = min(base * (2**i), 120.0)
            logger.warning(
                "%s: transient HTTP error %s (attempt %s/%s, sleeping %.1fs)",
                label,
                e,
                i + 1,
                attempts,
                delay,
            )
            time.sleep(delay)
    assert last is not None
    raise last


def _controller_base_url() -> str:
    return (
        os.environ.get("CONTROLLER_URL")
        or os.environ.get("OREON_CONTROLLER_URL")
        or "http://localhost:8000"
    ).rstrip("/")


def _build_log_put_timeout() -> httpx.Timeout:
    raw = (os.environ.get("OREON_WORKER_BUILD_LOG_PUT_TIMEOUT_SEC") or "7200").strip()
    try:
        total = float(raw)
    except ValueError:
        total = 7200.0
    total = max(120.0, min(total, 86400.0))
    return httpx.Timeout(total, connect=120.0)


def _build_log_presigned_r2_enabled() -> bool:
    """Default on so huge logs do not POST through Cloudflare Tunnel (524 timeouts)."""
    v = (os.environ.get("OREON_WORKER_BUILD_LOG_VIA_PRESIGNED_R2") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _upload_build_log_via_presigned_r2(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    data: bytes,
) -> str:
    digest = hashlib.sha256(data).hexdigest()
    size_bytes = len(data)
    put_to = _build_log_put_timeout()

    def _presign() -> dict:
        r = session.post(
            f"{controller_url}/api/worker/build-log/presign/{attempt_id}",
            timeout=httpx.Timeout(120.0, connect=60.0),
        )
        r.raise_for_status()
        return r.json()

    pres = _with_http_retries(f"build-log presign attempt_id={attempt_id}", _presign)
    put_url = str(pres["put_url"])
    hdrs = dict(pres.get("headers") or {})
    def _put() -> None:
        pr = session.put(put_url, headers=hdrs, content=data, timeout=put_to)
        pr.raise_for_status()

    _with_http_retries(f"build-log R2 PUT attempt_id={attempt_id}", _put)

    def _complete() -> dict:
        r = session.post(
            f"{controller_url}/api/worker/build-log/complete/{attempt_id}",
            json={"checksum_sha256": digest, "size_bytes": size_bytes},
            timeout=httpx.Timeout(600.0, connect=120.0),
        )
        r.raise_for_status()
        return r.json()

    out = _with_http_retries(f"build-log complete attempt_id={attempt_id}", _complete)
    return str(out.get("log_r2_key") or "")


def _upload_build_log_via_controller(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    data: bytes,
) -> str:
    if _build_log_presigned_r2_enabled():
        return _upload_build_log_via_presigned_r2(session, controller_url, attempt_id, data)

    def _do() -> str:
        r = session.put(
            f"{controller_url}/api/worker/build-log/{attempt_id}",
            content=data,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            timeout=_build_log_put_timeout(),
        )
        r.raise_for_status()
        return str(r.json().get("log_r2_key") or "")

    return _with_http_retries(f"build-log upload attempt_id={attempt_id}", _do)


_OREON_MAX_BUILD_LOG_BYTES = 64 * 1024 * 1024


def _upload_build_log_direct_put(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    data: bytes,
) -> None:
    """
    PUT log through the controller (no presign+complete). Used for periodic live snapshots so we
    do not hash the full object on every tick.
    """
    r = session.put(
        f"{controller_url}/api/worker/build-log/{attempt_id}",
        content=data,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        timeout=_build_log_put_timeout(),
    )
    r.raise_for_status()


def _live_log_interval_sec() -> int:
    raw = (os.environ.get("OREON_WORKER_LIVE_LOG_INTERVAL_SEC") or "60").strip()
    try:
        v = int(raw)
    except ValueError:
        v = 60
    return max(0, min(v, 3600))


def _mock_log_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _read_inprogress_mock_build_log_snippet(result_dir: Path, max_bytes: int = 4 * 1024 * 1024) -> str:
    """
    Prefer the *newest* build.log under resultdir (mock can leave older nested paths).
    During ``mock --rebuild``, dnf often streams to **root.log** while **build.log** still
    shows the finished ``rpmbuild -bs`` block for a long time, so include **root.log** too.
    """
    chunks: list[str] = []

    try:
        build_logs = [p for p in result_dir.rglob("build.log") if p.is_file()]
    except OSError:
        build_logs = []
    if build_logs:
        bl_path = max(build_logs, key=_mock_log_mtime)
        try:
            data = bl_path.read_bytes()
        except OSError:
            data = b""
        rel = ""
        try:
            rel = str(bl_path.relative_to(result_dir))
        except ValueError:
            rel = bl_path.name
        if len(data) > max_bytes:
            data = data[-max_bytes:]
            head = f"## build.log (newest, tail only) — {rel}\n\n[truncated first bytes]\n\n"
        else:
            head = f"## build.log (newest) — {rel}\n\n"
        chunks.append(head + data.decode("utf-8", errors="replace"))

    root_logs: list[Path] = []
    rp = result_dir / "root.log"
    if rp.is_file():
        root_logs.append(rp)
    try:
        root_logs.extend(p for p in result_dir.rglob("root.log") if p.is_file())
    except OSError:
        pass
    root_logs = list({p.resolve(): p for p in root_logs}.values())
    if root_logs:
        rr_path = max(root_logs, key=_mock_log_mtime)
        root_cap = min(max_bytes // 2, 2 * 1024 * 1024)
        try:
            rdata = rr_path.read_bytes()
        except OSError:
            rdata = b""
        if len(rdata) > root_cap:
            rdata = rdata[-root_cap:]
            rhead = f"## root.log tail (dnf / mock wrapper, last {root_cap} bytes)\n\n"
        else:
            rhead = "## root.log (dnf / mock wrapper)\n\n"
        chunks.append(rhead + rdata.decode("utf-8", errors="replace"))

    return "\n\n".join(chunks).strip()


def _start_live_build_log_snapshot_thread(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    result_dir: Path,
    stop_flag: threading.Event,
) -> None:
    interval = _live_log_interval_sec()
    if interval <= 0:
        return

    def runner() -> None:
        while not stop_flag.wait(timeout=interval):
            try:
                snippet = _read_inprogress_mock_build_log_snippet(result_dir)
                if not snippet.strip():
                    continue
                banner = (
                    "## Live log snapshot (mock still running) "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} worker-local\n\n"
                )
                body_bytes = (banner + snippet).encode("utf-8")
                if len(body_bytes) > _OREON_MAX_BUILD_LOG_BYTES:
                    continue

                def _snap() -> None:
                    _upload_build_log_direct_put(session, controller_url, attempt_id, body_bytes)

                _with_http_retries(f"live build-log snapshot attempt_id={attempt_id}", _snap)
            except Exception:
                logger.debug("Live build log snapshot failed (attempt %s)", attempt_id, exc_info=True)

    threading.Thread(target=runner, daemon=True, name=f"oreon-live-log-{attempt_id}").start()


def _download_source_via_controller(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    r2_key: str,
) -> bytes:
    def _do() -> bytes:
        r = session.get(
            f"{controller_url}/api/worker/source-blob/{attempt_id}",
            params={"key": r2_key},
            timeout=httpx.Timeout(7200.0, connect=120.0),
        )
        r.raise_for_status()
        return r.content

    return _with_http_retries(f"source-blob attempt_id={attempt_id}", _do)


def _pesign_bundle_max_bytes() -> int:
    raw = (os.environ.get("OREON_WORKER_PESIGN_BUNDLE_MAX_MIB") or "256").strip()
    try:
        mib = int(raw)
    except ValueError:
        mib = 256
    return max(16, min(mib, 1024)) * 1024 * 1024


def _pesign_extract_parent_dir() -> Path:
    override = (os.environ.get("OREON_WORKER_PESIGN_EXTRACT_DIR") or "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_dir() and os.access(p, os.W_OK):
            return p
        logger.warning("OREON_WORKER_PESIGN_EXTRACT_DIR is missing or not writable, using fallback")
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK):
        return shm
    logger.warning(
        "/dev/shm not available for pesign extract. NSS files may touch disk-backed temp. "
        "Use tmpfs on /dev/shm or set OREON_WORKER_PESIGN_EXTRACT_DIR to a RAM filesystem."
    )
    return Path(tempfile.gettempdir())


def _wipe_pesign_tree(root: Path | None) -> None:
    if root is None:
        return
    try:
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=False)
    except OSError:
        shutil.rmtree(str(root), ignore_errors=True)


@contextmanager
def _pesign_bundle_context(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
) -> Iterator[PesignRuntimeConfig | None]:
    """Yield pesign config while NSS lives only under RAM-backed extract_root, then wipe it."""
    volatile: Path | None = None
    cfg: PesignRuntimeConfig | None = None
    try:
        try:
            cfg, volatile = _with_http_retries(
                f"pesign-bundle attempt_id={attempt_id}",
                lambda: _fetch_pesign_bundle_volatile(session, controller_url, attempt_id),
            )
        except Exception as pe:
            logger.warning("Pesign bundle not applied (%s); PE/ko signing may be skipped", pe)
            cfg = None
        yield cfg
    finally:
        _wipe_pesign_tree(volatile)


def _fetch_pesign_bundle_volatile(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
) -> tuple[PesignRuntimeConfig | None, Path | None]:
    """
    Stream bundle into RAM (no tarball file on disk). Extract under tmpfs when possible.
    Returns (config, extract_root). Caller must _wipe_pesign_tree(extract_root) in a finally block.
    """
    cap = _pesign_bundle_max_bytes()
    buf = bytearray()
    with session.stream(
        "GET",
        f"{controller_url}/api/worker/pesign-bundle/{attempt_id}",
        timeout=httpx.Timeout(600.0, connect=120.0),
    ) as r:
        if r.status_code == 204:
            return None, None
        r.raise_for_status()
        for chunk in r.iter_bytes(chunk_size=1024 * 1024):
            buf.extend(chunk)
            if len(buf) > cap:
                raise RuntimeError(
                    f"pesign bundle larger than cap ({cap // (1024 * 1024)} MiB); "
                    "raise OREON_WORKER_PESIGN_BUNDLE_MAX_MIB if needed"
                )

    if not buf:
        return None, None

    parent = _pesign_extract_parent_dir()
    extract_root = parent / f"oreon-pesign-{attempt_id}-{secrets.token_hex(8)}"
    extract_root.mkdir(mode=0o700)
    try:
        bio = io.BytesIO(bytes(buf))
        del buf
        with tarfile.open(fileobj=bio, mode="r:gz") as tf:
            try:
                tf.extractall(extract_root, filter="data")
            except TypeError:
                tf.extractall(extract_root)
    except Exception:
        _wipe_pesign_tree(extract_root)
        raise

    manifest_path = extract_root / "manifest.json"
    if not manifest_path.is_file():
        _wipe_pesign_tree(extract_root)
        raise RuntimeError("pesign bundle missing manifest.json")
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    nss_rel = (man.get("nss_subdir") or "nss").strip()
    nss_dir = (extract_root / nss_rel).resolve()
    try:
        nss_dir.relative_to(extract_root.resolve())
    except ValueError:
        _wipe_pesign_tree(extract_root)
        raise RuntimeError("invalid nss path in bundle") from None
    if not nss_dir.is_dir():
        _wipe_pesign_tree(extract_root)
        raise RuntimeError("pesign bundle missing nss directory")
    rx = man.get("pesign_package_name_regex") or r"^(kernel.*|shim.*|fwupd.*|grub2-efi.*)$"
    cfg = PesignRuntimeConfig(
        pesign_nss_dir=str(nss_dir),
        pesign_rules_json=man.get("pesign_rules_json"),
        pesign_certificate_nickname=man.get("pesign_certificate_nickname"),
        pesign_package_name_regex=str(rx),
        pesign_token=man.get("pesign_token"),
        pesign_strict=bool(man.get("pesign_strict")),
        pesign_aarch64_qemu_prefix=man.get("pesign_aarch64_qemu_prefix"),
        pesign_aarch64_guest_shell=man.get("pesign_aarch64_guest_shell"),
    )
    return cfg, extract_root


def _get_arch() -> str:
    m = platform.machine()
    if m in ("x86_64", "AMD64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    return m


def _worker_result_post_timeout() -> httpx.Timeout:
    """
    Controller ``POST /worker/result/...`` can run createrepo and R2 copies for many RPMs; the default
    client read timeout (30s) causes ReadTimeout after the server already committed success. The worker
    then posts a spurious *failed* and overwrites the DB. Override with OREON_WORKER_RESULT_POST_TIMEOUT_SEC.
    """
    raw = os.environ.get("OREON_WORKER_RESULT_POST_TIMEOUT_SEC", "900")
    try:
        total = float(raw)
    except ValueError:
        total = 900.0
    total = max(120.0, min(total, 7200.0))
    connect = min(120.0, total)
    return httpx.Timeout(connect=connect, read=total, write=total, pool=total)


def _post_worker_build_result(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    json_body: dict,
) -> httpx.Response:
    def _do() -> httpx.Response:
        return session.post(
            f"{controller_url}/api/worker/result/{attempt_id}",
            json=json_body,
            timeout=_worker_result_post_timeout(),
        )

    return _with_http_retries(f"worker result attempt_id={attempt_id}", _do)


def _try_post_orphan_failure(
    controller_url: str,
    worker_headers: dict[str, str],
    releasename: str,
    attempt_id: int,
    exc: BaseException,
) -> None:
    """If _process_job dies without returning, still fail the attempt so the UI does not stick on *running*."""
    msg = f"Worker unhandled error during build: {type(exc).__name__}: {exc}"
    log_key = ""
    with httpx.Client(
        timeout=httpx.Timeout(600.0, connect=120.0),
        headers=worker_headers,
    ) as oc:
        try:
            log_key = _upload_build_log_via_controller(
                oc, controller_url, attempt_id, msg.encode("utf-8")
            )
        except Exception:
            logger.exception("Orphan failure: could not upload log for attempt %s", attempt_id)
        try:
            _post_worker_build_result(
                oc,
                controller_url,
                attempt_id,
                {"status": "failed", "log_r2_key": log_key or None, "error_message": msg[:2000]},
            )
        except Exception:
            logger.exception("Orphan failure: could not POST result for attempt %s", attempt_id)


def _rpm_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _rpm_upload_presigned_r2_enabled() -> bool:
    """Default on. Set OREON_WORKER_RPM_UPLOAD_VIA_PRESIGNED_R2=0 to use multipart POST through the controller."""
    v = (os.environ.get("OREON_WORKER_RPM_UPLOAD_VIA_PRESIGNED_R2") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _rpm_complete_post_timeout() -> httpx.Timeout:
    """
    Controller may download a multi GiB object from R2, hash it, and ``rpm --addsign`` while other
    builds run. Default read timeout is higher than the presigned PUT timeout.
    """
    raw = (os.environ.get("OREON_WORKER_RPM_COMPLETE_TIMEOUT_SEC") or "32400").strip()
    try:
        total = float(raw)
    except ValueError:
        total = 32400.0
    total = max(300.0, min(total, 86400.0))
    return httpx.Timeout(total, connect=120.0)


def _rpm_complete_extra_retries() -> int:
    """Extra passes after a non-transient HTTP 400 with a retryable error detail (R2 lag, verify race)."""
    raw = (os.environ.get("OREON_WORKER_RPM_COMPLETE_EXTRA_RETRIES") or "5").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 5
    return max(0, min(n, 15))


def _rpm_complete_400_retryable(detail: str) -> bool:
    if not detail:
        return False
    low = detail.lower()
    if "job cancelled" in low or "invalid checksum" in low:
        return False
    if "filename must end with" in low:
        return False
    return any(
        x in low
        for x in (
            "not in r2",
            "upload verify failed",
            "size mismatch",
            "timed out waiting for rpm finalize lock",
        )
    )


def _upload_rpm_via_presigned_r2(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    rpm_path: Path,
) -> dict:
    fname = rpm_path.name
    size_bytes = rpm_path.stat().st_size
    digest = _rpm_file_sha256(rpm_path)

    put_timeout = httpx.Timeout(7200.0, connect=120.0)
    complete_timeout = _rpm_complete_post_timeout()
    extra = _rpm_complete_extra_retries()
    max_pass = 1 + extra
    cycles = 4  # full re-presign and re-put cycle count for huge SRPMs and R2 eventual consistency
    last_exc: BaseException | None = None
    for cycle in range(cycles):
        def _presign() -> dict:
            r = session.post(
                f"{controller_url}/api/worker/upload-rpm/presign/{attempt_id}",
                json={"filename": fname},
                timeout=httpx.Timeout(120.0, connect=60.0),
            )
            r.raise_for_status()
            return r.json()

        pres = _with_http_retries(
            f"rpm presign attempt_id={attempt_id} {fname} cycle={cycle + 1}/{cycles}",
            _presign,
        )
        put_url = str(pres["put_url"])
        hdrs = dict(pres.get("headers") or {})

        def _put() -> None:
            with rpm_path.open("rb") as fp:
                pr = session.put(put_url, headers=hdrs, content=fp, timeout=put_timeout)
            pr.raise_for_status()

        _with_http_retries(
            f"rpm R2 PUT attempt_id={attempt_id} {fname} cycle={cycle + 1}/{cycles}",
            _put,
        )

        saw_400 = False
        for ci in range(max_pass):
            def _complete() -> dict:
                r = session.post(
                    f"{controller_url}/api/worker/upload-rpm/complete/{attempt_id}",
                    json={
                        "filename": fname,
                        "checksum_sha256": digest,
                        "size_bytes": size_bytes,
                    },
                    timeout=complete_timeout,
                )
                r.raise_for_status()
                return r.json()

            try:
                return _with_http_retries(
                    f"rpm presign-complete attempt_id={attempt_id} {fname} cycle={cycle + 1}/{cycles} pass={ci + 1}/{max_pass}",
                    _complete,
                )
            except httpx.HTTPStatusError as e:
                last_exc = e
                detail = ""
                if e.response is not None and e.response.status_code == 400:
                    saw_400 = True
                    try:
                        body = e.response.json()
                        if isinstance(body, dict) and body.get("detail") is not None:
                            detail = str(body["detail"])
                    except Exception:
                        detail = (e.response.text or "")[:800]
                    if ci + 1 < max_pass and _rpm_complete_400_retryable(detail):
                        delay = min(3.0 * (1.6**ci), 90.0)
                        logger.warning(
                            "rpm complete HTTP 400 retryable for %s (attempt_id=%s cycle=%s/%s): %s, sleeping %.1fs",
                            fname,
                            attempt_id,
                            cycle + 1,
                            cycles,
                            detail[:200],
                            delay,
                        )
                        time.sleep(delay)
                        continue
                raise

        if saw_400 and cycle + 1 < cycles:
            logger.warning(
                "rpm complete kept returning 400 for %s (attempt_id=%s), retrying with fresh presign+PUT cycle %s/%s",
                fname,
                attempt_id,
                cycle + 2,
                cycles,
            )
            continue
        break

    assert last_exc is not None
    raise last_exc


def _upload_rpm_to_controller(
    session: httpx.Client,
    controller_url: str,
    attempt_id: int,
    rpm_path: Path,
) -> dict:
    """Default: presigned R2 PUT + API complete. Optional: multipart POST when presign is disabled."""
    upload_timeout = httpx.Timeout(7200.0, connect=120.0)
    fname = rpm_path.name

    def _multipart() -> dict:
        with open(rpm_path, "rb") as fp:
            files = {"file": (fname, fp, "application/x-rpm")}
            r = session.post(
                f"{controller_url}/api/worker/upload-rpm/{attempt_id}",
                files=files,
                timeout=upload_timeout,
            )
        r.raise_for_status()
        return r.json()

    if _rpm_upload_presigned_r2_enabled():
        return _upload_rpm_via_presigned_r2(session, controller_url, attempt_id, rpm_path)
    try:
        return _with_http_retries(f"rpm upload attempt_id={attempt_id} {fname}", _multipart)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 413:
            logger.warning(
                "Multipart RPM upload returned 413 for %s, retrying via presigned R2 PUT",
                fname,
            )
            return _upload_rpm_via_presigned_r2(session, controller_url, attempt_id, rpm_path)
        raise


def _truthy_env(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _mock_addrepo_baseurls(job: dict) -> list[str]:
    """URLs for ``mock --addrepo`` from poll ``mock_additional_repos`` plus optional worker env."""
    seen: set[str] = set()
    out: list[str] = []
    raw = job.get("mock_additional_repos") or []
    if isinstance(raw, list):
        for u in raw:
            s = str(u).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
    extra = (os.environ.get("OREON_WORKER_EXTRA_MOCK_ADDREPO") or "").strip()
    if extra:
        for part in extra.split(","):
            p = part.strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _rpm_dist_macro_value(releasename: str | None) -> str | None:
    """
    Return the value for rpmbuild ``dist`` (e.g. ``.or11``) so ``Release`` and filenames use ``orN``
    instead of the Fedora mock default (``fc45``). Return None to leave the chroot default.

    Order: if ``OREON_WORKER_SKIP_RPM_DIST_OVERRIDE`` is set, None. Else if ``OREON_WORKER_RPM_DIST`` is
    set, use that (add a leading dot when missing). Else derive ``.or`` + first digit run in
    ``releasename`` (``oreon-11-rp1`` -> ``11``).
    """
    if _truthy_env("OREON_WORKER_SKIP_RPM_DIST_OVERRIDE"):
        return None
    raw = (os.environ.get("OREON_WORKER_RPM_DIST") or "").strip()
    if raw:
        low = raw.lower()
        if low in ("0", "false", "no", "off", "default"):
            return None
        return raw if raw.startswith(".") else f".{raw}"
    if not releasename:
        return None
    m = re.search(r"(\d+)", str(releasename).strip())
    if not m:
        return None
    return f".or{m.group(1)}"


def _mock_shared_host_cache_enabled() -> bool:
    """
    If set, the worker does not pass per-attempt ``cache_topdir`` to mock.

    Default off so parallel jobs do not share ``/var/cache/mock/<root>/dnf_cache`` (mock uses
    ``shared_root_name`` without ``uniqueext`` for that path). Sharing it causes dnf5 races
    (tmpdir / directory iterator errors) under load.
    """
    return _truthy_env("OREON_WORKER_MOCK_SHARED_CACHE")


def _mock_cache_topdir_for_attempt(attempt_id: int) -> str:
    parent = (os.environ.get("OREON_WORKER_MOCK_CACHE_PARENT") or "/var/cache/mock").rstrip("/")
    return f"{parent}/oreon-{attempt_id}"


def _mock_cmd_base(
    mock_config: str,
    mock_unique_ext: str | None,
    addrepo_baseurls: list[str],
    rpm_dist: str | None = None,
    *,
    attempt_id: int | None = None,
) -> list[str]:
    cmd: list[str] = ["mock", "-r", mock_config]
    if mock_unique_ext:
        cmd += ["--uniqueext", mock_unique_ext]
    if attempt_id is not None and not _mock_shared_host_cache_enabled():
        cmd += ["--config-opts", f"cache_topdir={_mock_cache_topdir_for_attempt(attempt_id)}"]
    for url in addrepo_baseurls:
        cmd += ["--addrepo", url]
    if rpm_dist:
        cmd += ["--define", f"dist {rpm_dist}"]
    return cmd


def _make_mock_resultdir_world_readable(result_dir: Path) -> None:
    """
    Best-effort chmod so the worker user can traverse result_dir and read logs + ``*.rpm``.

    mock often drops root-owned subtrees. We must run this **before** any ``result_dir.rglob()``
    (e.g. ``_mock_stage_log_sections`` / ``build.log``), not only before artifact upload.

    Non-sudo ``chmod -R`` runs every time (cheap). For root-only files, set
    ``OREON_WORKER_CHMOD_MOCK_RESULTDIR_SUDO=1`` and sudoers (see deploy/oreon-worker.env.example).
    Set ``OREON_WORKER_SKIP_CHMOD_MOCK_RESULTDIR=1`` to skip the non-sudo chmod (unusual).
    """
    if not result_dir.is_dir():
        return
    if not _truthy_env("OREON_WORKER_SKIP_CHMOD_MOCK_RESULTDIR"):
        rc, out = _run(["chmod", "-R", "a+rX", str(result_dir)], timeout_s=600)
        if rc != 0 and (out or "").strip():
            logger.warning(
                "chmod -R a+rX on mock resultdir rc=%s: %s",
                rc,
                _tail_lines(out, 20),
            )
    if _truthy_env("OREON_WORKER_CHMOD_MOCK_RESULTDIR_SUDO"):
        rc, out = _run(["sudo", "-n", "chmod", "-R", "a+rX", str(result_dir)], timeout_s=600)
        if rc != 0:
            logger.warning(
                "OREON_WORKER_CHMOD_MOCK_RESULTDIR_SUDO: sudo -n chmod failed rc=%s "
                "(add NOPASSWD sudoers for this worker user if needed): %s",
                rc,
                _tail_lines(out, 25),
            )


def _run(cmd: list[str], timeout_s: int | None, cwd: str | None = None) -> tuple[int, str]:
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

# Default mock wall-clock caps (override with OREON_WORKER_MOCK_*_TIMEOUT_SEC). Upper bound for env overrides.
_MOCK_STAGE_TIMEOUT_DEFAULT_SEC = 5 * 24 * 3600  # 5 days
_MOCK_STAGE_TIMEOUT_MAX_SEC = 30 * 24 * 3600  # 30 days
_MOCK_STAGE_TIMEOUT_MIN_SEC = 600


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
            try:
                proc.kill()
            except OSError:
                pass
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
            try:
                proc.kill()
            except OSError:
                pass
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
    try:
        matches = sorted(result_dir.rglob(log_basename))
    except OSError:
        return ""
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
    try:
        matches = sorted(result_dir.rglob("build.log"))
    except OSError:
        return ""
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


def _collect_url_source_basenames_from_spectool_list(spec_path: Path) -> list[str]:
    """
    Return basenames of every URL-based Source/Patch entry in the spec.

    Used to validate that spectool -g actually downloaded each one; spectool can
    silently return rc=0 even when a download fails (e.g. 404 or timeout), and the
    missing file only surfaces as a cryptic "Bad file" error inside mock.
    """
    spec_dir = spec_path.parent
    rc, out = _run(
        ["spectool", "-l", str(spec_path.name)],
        timeout_s=120,
        cwd=str(spec_dir),
    )
    if rc != 0:
        return []

    basenames: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        _, _, rest = line.partition(":")
        rest = rest.strip()
        low = rest.lower()
        if low.startswith(("http://", "https://", "ftp://")):
            basenames.append(Path(rest.rstrip("/")).name)
    return basenames


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

    # Collect expected URL source basenames BEFORE downloading so we can verify after.
    expected_url_basenames = _collect_url_source_basenames_from_spectool_list(spec_path)

    rc, out = _run(
        ["spectool", "-g", "-C", str(staging_dir), str(spec_path.name)],
        timeout_s=None,
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
    names = {f.name for f in files if f.is_file()}
    head = ", ".join(sorted(names)[:6])
    more = f" (+{len(names) - 6} more)" if len(names) > 6 else ""
    log_parts = [f"URL sources via spectool: {len(names)} file(s){(': ' + head + more) if names else ''}"]

    # Verify every expected URL source was actually downloaded. spectool can return rc=0
    # even when a download fails (server 404, timeout, etc.), which causes a confusing
    # "Bad file: /builddir/build/SOURCES/foo" error deep inside mock rather than here.
    missing_downloads = [b for b in expected_url_basenames if b not in names]
    if missing_downloads:
        shown = ", ".join(missing_downloads[:10])
        extra = f" (+{len(missing_downloads) - 10} more)" if len(missing_downloads) > 10 else ""
        log_parts.append(
            f"spectool download failed (rc=0 but files missing): {shown}{extra}\n"
            f"spectool output:\n{out[:3000]}"
        )
        return False, "\n".join(log_parts)

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
    addrepo_baseurls: list[str] | None = None,
    rpm_dist: str | None = None,
    attempt_id: int | None = None,
) -> tuple[bool, Path | None, str, bool]:
    """
    Build SRPM from spec/sources using mock (so dependencies are handled in chroot).
    Produces *.src.rpm in result_dir.
    Returns (ok, srpm_path, output, cancelled).

    ``mock_unique_ext`` must be set per concurrent build on this host (e.g. attempt id);
    otherwise mock shares one chroot per ``-r`` config and parallel jobs fail with
    "Build root is locked by another process."

    Pass ``attempt_id`` so mock gets a dedicated ``cache_topdir`` (unless
    ``OREON_WORKER_MOCK_SHARED_CACHE`` is set). Otherwise dnf metadata and tmpdirs are shared
    across all jobs using the same ``-r`` and dnf5 can fail when builds overlap.
    """
    cmd = _mock_cmd_base(
        mock_config,
        mock_unique_ext,
        addrepo_baseurls or [],
        rpm_dist=rpm_dist,
        attempt_id=attempt_id,
    )
    cmd += [
        "--buildsrpm",
        "--spec",
        str(spec_path),
        "--sources",
        str(sources_dir),
        "--resultdir",
        str(result_dir),
    ]
    rc, out = _run_with_abort(
        cmd, timeout_s=_mock_buildsrpm_timeout_sec(), abort_event=abort_event
    )
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
    addrepo_baseurls: list[str] | None = None,
    rpm_dist: str | None = None,
    attempt_id: int | None = None,
) -> tuple[bool, str, bool]:
    """Returns (ok, output, cancelled). See :func:`_mock_build_srpm_from_spec` for isolation knobs."""
    cmd = _mock_cmd_base(
        mock_config,
        mock_unique_ext,
        addrepo_baseurls or [],
        rpm_dist=rpm_dist,
        attempt_id=attempt_id,
    )
    cmd += ["--rebuild", str(srpm_path), "--resultdir", str(result_dir)]
    rc, out = _run_with_abort(cmd, timeout_s=_mock_rebuild_timeout_sec(), abort_event=abort_event)
    if rc == _RC_MOCK_CANCELLED:
        return False, out, True
    return rc == 0, out, False


def _mock_buildsrpm_timeout_sec() -> int:
    """Seconds before killing ``mock --buildsrpm`` (default 5 days)."""
    raw = os.environ.get(
        "OREON_WORKER_MOCK_BUILDSRPM_TIMEOUT_SEC", str(_MOCK_STAGE_TIMEOUT_DEFAULT_SEC)
    )
    try:
        v = int(raw)
    except ValueError:
        v = _MOCK_STAGE_TIMEOUT_DEFAULT_SEC
    return max(_MOCK_STAGE_TIMEOUT_MIN_SEC, min(v, _MOCK_STAGE_TIMEOUT_MAX_SEC))


def _mock_rebuild_timeout_sec() -> int:
    """Seconds before killing ``mock --rebuild`` (default 5 days)."""
    raw = os.environ.get(
        "OREON_WORKER_MOCK_REBUILD_TIMEOUT_SEC", str(_MOCK_STAGE_TIMEOUT_DEFAULT_SEC)
    )
    try:
        v = int(raw)
    except ValueError:
        v = _MOCK_STAGE_TIMEOUT_DEFAULT_SEC
    return max(_MOCK_STAGE_TIMEOUT_MIN_SEC, min(v, _MOCK_STAGE_TIMEOUT_MAX_SEC))


def _worker_temp_parent_dir() -> str | None:
    """Optional dedicated parent for worker scratch dirs (keeps system /tmp free)."""
    p = (os.environ.get("OREON_WORKER_TMP_PARENT") or "").strip()
    return p or None


def _inflight_marker_paths(attempt_id: int) -> list[Path]:
    """
    Cross-process inflight markers so stale prune cannot delete active attempt dirs when multiple
    worker processes run on the same host.
    """
    roots: list[Path] = [Path(tempfile.gettempdir())]
    extra = _worker_temp_parent_dir()
    if extra:
        roots.append(Path(extra))
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        try:
            key = str(r.resolve())
        except OSError:
            key = str(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r / f".oreon-worker-inflight-{attempt_id}.lock")
    return out


def _mark_inflight_attempt_external(attempt_id: int) -> None:
    for p in _inflight_marker_paths(attempt_id):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not write inflight marker %s (cross-process prune protection disabled for attempt %s): %s",
                p, attempt_id, exc,
            )


def _clear_inflight_attempt_external(attempt_id: int) -> None:
    for p in _inflight_marker_paths(attempt_id):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _mock_scrub_unique_chroot(mock_config: str, mock_unique_ext: str, attempt_id: int | None = None) -> None:
    """
    Delete mock chroot + bootstrap for this job's ``--uniqueext`` (frees space under
    ``/var/lib/mock``). Safe to call even if the build never created a root.
    """
    if not mock_config or not mock_unique_ext:
        return
    timeout = int(os.environ.get("OREON_WORKER_MOCK_SCRUB_TIMEOUT_SEC", "3600"))
    cmd = [
        "mock",
        "-r",
        mock_config,
        "--uniqueext",
        mock_unique_ext,
    ]
    if attempt_id is not None and not _mock_shared_host_cache_enabled():
        cmd += ["--config-opts", f"cache_topdir={_mock_cache_topdir_for_attempt(attempt_id)}"]
    cmd += ["--scrub", "all"]
    rc, out = _run(cmd, timeout_s=max(120, timeout))
    if rc != 0:
        snippet = _tail_lines(out, 40) if out else ""
        logger.warning(
            "mock --scrub=all failed (rc=%s) for -r %s --uniqueext %s: %s",
            rc,
            mock_config,
            mock_unique_ext,
            snippet,
        )


def _rm_rf_var_lib_mock_for_attempt(attempt_id: int) -> None:
    """
    Remove mock top-level roots for this attempt only (parallel-safe: names end with
    ``-oreon-<attempt_id>``, e.g. ``fedora-rawhide-x86_64-oreon-139`` and
    ``fedora-rawhide-x86_64-bootstrap-oreon-139``).
    """
    base = Path("/var/lib/mock")
    if not base.is_dir():
        return
    suffix = f"-oreon-{attempt_id}"
    removed = 0
    try:
        for p in list(base.iterdir()):
            try:
                if p.is_dir() and p.name.endswith(suffix):
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    if removed:
        logger.info("Removed %d mock root(s) under /var/lib/mock for attempt %s", removed, attempt_id)


def _rm_rf_mock_cache_topdir_for_attempt(attempt_id: int) -> None:
    """Remove per-attempt mock ``cache_topdir`` (dnf/yum host cache bind-mounted into the chroot)."""
    if _mock_shared_host_cache_enabled():
        return
    p = Path(_mock_cache_topdir_for_attempt(attempt_id))
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            logger.info("Removed mock cache_topdir %s", p)
    except OSError as e:
        logger.warning("Could not remove mock cache_topdir %s: %s", p, e)


def _rm_rf_worker_tempdirs_for_attempt(attempt_id: int, tmp_parent: str | None) -> None:
    """
    Best-effort ``rm -rf`` of scratch dirs for this attempt only:
    ``oreon-worker-<attempt_id>-…`` under TMPDIR and optional OREON_WORKER_TMP_PARENT.

    Uses ``startswith("oreon-worker-{id}-")`` (not a glob) so attempt **8** never deletes **86**'s dir.
    """
    name_prefix = f"oreon-worker-{attempt_id}-"
    roots: list[Path] = [Path(tempfile.gettempdir())]
    if tmp_parent:
        roots.append(Path(tmp_parent))
    seen: set[str] = set()
    removed = 0
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if not root.is_dir():
            continue
        try:
            for p in root.iterdir():
                try:
                    if p.is_dir() and p.name.startswith(name_prefix):
                        shutil.rmtree(p, ignore_errors=True)
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
    if removed:
        logger.info("Removed %d worker temp dir(s) with prefix %r for attempt %s", removed, name_prefix, attempt_id)


def _cleanup_job_disk(attempt_id: int, mock_config: str, mock_unique_ext: str, work_tmpdir: str | None) -> None:
    """
    After each job: mock scrub (umount), then rm -rf this attempt's trees under /var/lib/mock,
    then this job's work tmpdir and any matching oreon-worker-<id>-* dirs, then stale prune.
    Safe with parallel builds (everything keyed by attempt_id / known work path).
    """
    tmp_parent = _worker_temp_parent_dir()
    # Remove the in-dir sentinel and deregister from the active-path set before any rmtree so that
    # _prune_stale_oreon_worker_tempdirs called at the end of this function does not see our own
    # (now-dead) dir as live while still correctly protecting other parallel jobs' dirs.
    if work_tmpdir:
        try:
            (Path(work_tmpdir) / ".oreon-active").unlink(missing_ok=True)
        except OSError:
            pass
    with _inflight_attempt_lock:
        if work_tmpdir:
            _active_work_tmpdir_paths.discard(work_tmpdir)
    _mock_scrub_unique_chroot(mock_config, mock_unique_ext, attempt_id)
    _rm_rf_var_lib_mock_for_attempt(attempt_id)
    _rm_rf_mock_cache_topdir_for_attempt(attempt_id)
    if work_tmpdir:
        shutil.rmtree(work_tmpdir, ignore_errors=True)
    _rm_rf_worker_tempdirs_for_attempt(attempt_id, tmp_parent)
    _prune_stale_oreon_worker_tempdirs()
    _prune_stale_var_lib_mock_dirs()


def _prune_stale_oreon_worker_tempdirs() -> None:
    """
    Remove ``oreon-worker-*`` directories older than OREON_WORKER_PRUNE_TMP_AGE_SEC (default 300s).
    Set OREON_WORKER_PRUNE_TMP_AGE_SEC=0 to disable. Covers TMPDIR and OREON_WORKER_TMP_PARENT.
    """
    age = int(os.environ.get("OREON_WORKER_PRUNE_TMP_AGE_SEC", "300"))
    if age <= 0:
        return
    now = time.time()
    with _inflight_attempt_lock:
        inflight_ids = set(_inflight_attempt_ids)
        active_paths = set(_active_work_tmpdir_paths)

    def _tempdir_attempt_id(name: str) -> int | None:
        m = re.match(r"^oreon-worker-(\d+)-", name)
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    roots: list[Path] = []
    roots.append(Path(tempfile.gettempdir()))
    extra = _worker_temp_parent_dir()
    if extra:
        roots.append(Path(extra))
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if not root.is_dir():
            continue
        try:
            for p in root.glob("oreon-worker-*"):
                if not p.is_dir():
                    continue
                # Check 1 (same-process, most reliable): attempt-id in in-memory inflight set.
                aid = _tempdir_attempt_id(p.name)
                if aid is not None and aid in inflight_ids:
                    continue
                # Check 2 (same-process, belt): path registered in active tmpdir set.
                try:
                    p_str = str(p.resolve())
                except OSError:
                    p_str = str(p)
                if p_str in active_paths or str(p) in active_paths:
                    continue
                # Check 3 (cross-process): inflight marker file written before thread starts.
                if aid is not None:
                    if any(mp.exists() for mp in _inflight_marker_paths(aid)):
                        continue
                # Check 4 (cross-process fallback): sentinel file inside the dir itself.
                if (p / ".oreon-active").exists():
                    continue
                try:
                    if now - p.stat().st_mtime < age:
                        continue
                except OSError:
                    continue
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _prune_stale_var_lib_mock_dirs() -> None:
    """
    Scrub and remove ``/var/lib/mock/*-oreon-<id>`` and ``/var/cache/mock/oreon-<id>``
    directories left behind by crashed or cancelled builds.

    The per-build cleanup in ``_cleanup_job_disk`` handles the happy path. This sweeper
    catches anything that slipped through (worker killed mid-cleanup, mock crash, etc.).

    Age threshold: OREON_WORKER_PRUNE_MOCK_CHROOT_AGE_SEC (default 3600s / 1 hour).
    Set to 0 to disable.
    """
    age = int(os.environ.get("OREON_WORKER_PRUNE_MOCK_CHROOT_AGE_SEC", "3600"))
    if age <= 0:
        return

    with _inflight_attempt_lock:
        inflight_ids = set(_inflight_attempt_ids)

    now = time.time()

    # Pattern: <anything>-oreon-<digits>  (skip -bootstrap- dirs; scrubbing the main handles them)
    _mock_chroot_re = re.compile(r"^(.+)-oreon-(\d+)$")

    mock_lib_base = Path("/var/lib/mock")
    mock_cache_base = Path("/var/cache/mock")

    if mock_lib_base.is_dir():
        try:
            for p in list(mock_lib_base.iterdir()):
                if not p.is_dir():
                    continue
                # Skip bootstrap dirs - they are cleaned when the main chroot is scrubbed.
                if "-bootstrap-oreon-" in p.name:
                    continue
                m = _mock_chroot_re.match(p.name)
                if not m:
                    continue
                try:
                    attempt_id = int(m.group(2))
                except ValueError:
                    continue
                if attempt_id in inflight_ids:
                    continue
                try:
                    if now - p.stat().st_mtime < age:
                        continue
                except OSError:
                    continue
                mock_config = m.group(1)
                mock_unique_ext = f"oreon-{attempt_id}"
                logger.info(
                    "Pruning stale mock chroot %s (attempt %s, config %s)",
                    p.name,
                    attempt_id,
                    mock_config,
                )
                # Run mock --scrub first to unmount any lingering bind mounts.
                _mock_scrub_unique_chroot(mock_config, mock_unique_ext)
                # Belt-and-suspenders: rm -rf anything still left.
                for leftover in list(mock_lib_base.iterdir()):
                    if leftover.is_dir() and leftover.name.endswith(f"-oreon-{attempt_id}"):
                        shutil.rmtree(leftover, ignore_errors=True)
        except OSError:
            pass

    if mock_cache_base.is_dir():
        try:
            for p in list(mock_cache_base.iterdir()):
                if not p.is_dir() or not p.name.startswith("oreon-"):
                    continue
                try:
                    attempt_id = int(p.name[len("oreon-"):])
                except ValueError:
                    continue
                if attempt_id in inflight_ids:
                    continue
                try:
                    if now - p.stat().st_mtime < age:
                        continue
                except OSError:
                    continue
                logger.info("Pruning stale mock cache_topdir %s (attempt %s)", p, attempt_id)
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _process_job(controller_url: str, worker_headers: dict[str, str], job: dict) -> None:
    done_event = threading.Event()
    abort_event = threading.Event()
    session: httpx.Client | None = None
    try:
        attempt_id = job["build_attempt_id"]
        session = httpx.Client(timeout=30.0, headers=worker_headers)

        def poll_cancel() -> None:
            cancel_client = httpx.Client(timeout=20.0, headers=worker_headers)
            try:
                while not done_event.is_set():
                    try:
                        r = cancel_client.get(
                            f"{controller_url}/api/worker/cancel-check/{attempt_id}",
                            timeout=15.0,
                        )
                        if r.status_code == 200 and r.json().get("cancel_requested"):
                            abort_event.set()
                            return
                    except Exception:
                        pass
                    time.sleep(5)
            finally:
                cancel_client.close()

        threading.Thread(target=poll_cancel, daemon=True, name="oreon-cancel-poller").start()

        releasename = job.get("releasename") or "oreon11"
        branch = job.get("branch") or "dev"
        arch = job.get("architecture") or "x86_64"
        sources = job.get("sources") or []
        mock_config = job.get("mock_config")
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
            log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
            _post_worker_build_result(
                session,
                controller_url,
                attempt_id,
                {"status": "failed", "log_r2_key": log_key, "error_message": msg},
            )
            return

        # One chroot lock per mock "-r" config; parallel threads need distinct roots.
        mock_unique_ext = f"oreon-{attempt_id}"
        scrub_mock_cfg = mock_config
        scrub_mock_ext = mock_unique_ext
        mock_addrepos = _mock_addrepo_baseurls(job)
        if mock_addrepos:
            logger.info("mock --addrepo for attempt %s: %s", attempt_id, mock_addrepos)

        rpm_dist = _rpm_dist_macro_value(job.get("releasename"))
        if rpm_dist:
            logger.info("mock rpmbuild dist for attempt %s: %s", attempt_id, rpm_dist)

        tmp_parent = _worker_temp_parent_dir()
        if tmp_parent:
            try:
                Path(tmp_parent).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning("OREON_WORKER_TMP_PARENT=%s not usable (%s), using default temp dir", tmp_parent, e)
                tmp_parent = None
        tmpdir: str | None = None
        tmp_prefix = f"oreon-worker-{attempt_id}-"
        try:
            tmpdir = (
                tempfile.mkdtemp(prefix=tmp_prefix, dir=tmp_parent)
                if tmp_parent
                else tempfile.mkdtemp(prefix=tmp_prefix)
            )
            # Register path in the same-process active set so _prune_stale_oreon_worker_tempdirs
            # never deletes this directory from a parallel thread's cleanup run.
            with _inflight_attempt_lock:
                _active_work_tmpdir_paths.add(tmpdir)
            # Write a sentinel file inside the dir for cross-process protection (different worker
            # processes on the same host share /tmp but not in-memory state).  The file is removed
            # at the start of _cleanup_job_disk before the rmtree so it does not linger.
            try:
                (Path(tmpdir) / ".oreon-active").write_text(str(os.getpid()), encoding="utf-8")
            except OSError as _e:
                logger.warning("Could not write .oreon-active sentinel in %s: %s", tmpdir, _e)
            tmp = Path(tmpdir)
            build_log_parts: list[str] = []
            log_max_lines = int(os.environ.get("OREON_WORKER_LOG_MAX_LINES", "40000"))

            srpm_path: Path | None = None

            if srpm_r2_key:
                try:
                    srpm_bytes = _download_source_via_controller(
                        session, controller_url, attempt_id, srpm_r2_key
                    )
                except Exception as dl_err:
                    msg = f"SRPM download from controller failed: {dl_err}"
                    log_key = _upload_build_log_via_controller(
                        session, controller_url, attempt_id, msg.encode("utf-8")
                    )
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
                    )
                    return
                if not srpm_bytes:
                    msg = "SRPM empty or not found"
                    log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {"status": "failed", "log_r2_key": log_key, "error_message": msg},
                    )
                    return
                srpm_path = tmp / "source.src.rpm"
                srpm_path.write_bytes(srpm_bytes)
            elif distgit_url:
                repo, branch_name, spec_rel = _parse_distgit_url(distgit_url)
                if not spec_rel:
                    msg = "DistGit URL missing '#<branch>:<spec_relpath>' fragment"
                    log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {"status": "failed", "log_r2_key": log_key, "error_message": msg},
                    )
                    return
                git_dir = tmp / "distgit"
                ok, git_out = _clone_repo(repo, branch_name, git_dir)
                if git_out.strip():
                    build_log_parts.append("## Git clone\n" + git_out.strip())
                if not ok:
                    msg = "\n\n".join(build_log_parts).strip()
                    log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
                    )
                    return
                lfs_rc, lfs_out = _maybe_git_lfs_pull(git_dir)
                if lfs_out.strip():
                    build_log_parts.append(f"## Git LFS (rc={lfs_rc})\n{lfs_out.strip()}")
                spec_path = git_dir / spec_rel
                if not spec_path.is_file():
                    build_log_parts.append(f"Spec not found: {spec_rel}")
                    msg = "\n\n".join(build_log_parts).strip()
                    log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
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
                    log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
                    )
                    return

                srpm_result_dir = tmp / "srpm_result"
                srpm_result_dir.mkdir()
                live_srpm_stop = threading.Event()
                _start_live_build_log_snapshot_thread(
                    session, controller_url, attempt_id, srpm_result_dir, live_srpm_stop
                )
                try:
                    ok, srpm_path, srpm_out, srpm_cancelled = _mock_build_srpm_from_spec(
                        mock_config=mock_config,
                        spec_path=spec_path,
                        sources_dir=sources_staging,
                        result_dir=srpm_result_dir,
                        abort_event=abort_event,
                        mock_unique_ext=mock_unique_ext,
                        addrepo_baseurls=mock_addrepos,
                        rpm_dist=rpm_dist,
                        attempt_id=attempt_id,
                    )
                finally:
                    live_srpm_stop.set()
                _make_mock_resultdir_world_readable(srpm_result_dir)
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
                    log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {
                            "status": "cancelled",
                            "log_r2_key": log_key,
                            "error_message": "Build cancelled by user",
                            "artifacts": [],
                        },
                    )
                    return
                if not ok or not srpm_path:
                    msg = "\n\n".join(build_log_parts).strip()
                    log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {"status": "failed", "log_r2_key": log_key, "error_message": msg[:2000]},
                    )
                    return
            else:
                msg = "No SRPM or DistGit source in job (upload SRPM or configure DistGit spec path)."
                log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, msg.encode("utf-8"))
                _post_worker_build_result(
                    session,
                    controller_url,
                    attempt_id,
                    {"status": "failed", "log_r2_key": log_key, "error_message": msg},
                )
                return

            result_dir = tmp / "result"
            result_dir.mkdir()
            live_rebuild_stop = threading.Event()
            _start_live_build_log_snapshot_thread(
                session, controller_url, attempt_id, result_dir, live_rebuild_stop
            )
            try:
                ok, rebuild_out, rebuild_cancelled = _mock_rebuild_srpm(
                    mock_config=mock_config,
                    srpm_path=srpm_path,
                    result_dir=result_dir,
                    abort_event=abort_event,
                    mock_unique_ext=mock_unique_ext,
                    addrepo_baseurls=mock_addrepos,
                    rpm_dist=rpm_dist,
                    attempt_id=attempt_id,
                )
            finally:
                live_rebuild_stop.set()
            _make_mock_resultdir_world_readable(result_dir)
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
                log_key = _upload_build_log_via_controller(session, controller_url, attempt_id, full_log.encode("utf-8"))
                _post_worker_build_result(
                    session,
                    controller_url,
                    attempt_id,
                    {
                        "status": "cancelled",
                        "log_r2_key": log_key,
                        "error_message": "Build cancelled by user",
                        "artifacts": [],
                    },
                )
                return
            full_log = "\n\n".join([p for p in build_log_parts if p]).strip() + "\n"

            rpm_upload_stage = tmp / "rpm_upload_staging"
            rpm_upload_stage.mkdir(exist_ok=True)

            # Upload RPMs before the big build log so a tunnel timeout on log cannot block any binary,
            # and so alphabetical order (e.g. huge debuginfo before kernel-modules) does not strand later RPMs.
            with _pesign_bundle_context(session, controller_url, attempt_id) as pesign_cfg:
                perm_hint = (
                    "Permission denied while reading mock --resultdir RPMs (tree may still be "
                    "root-owned after chmod). Set OREON_WORKER_CHMOD_MOCK_RESULTDIR_SUDO=1 and NOPASSWD "
                    "sudoers for chmod on worker temp dirs (deploy/oreon-worker.env.example). "
                    "Ensure the worker user is in the 'mock' group."
                )
                artifacts_payload: list[dict] = []
                try:
                    rpm_paths = sorted(result_dir.rglob("*.rpm"))
                except OSError as pe:
                    err_msg = f"{perm_hint}\n\n{type(pe).__name__}: {pe}"
                    logger.exception("%s", err_msg)
                    fail_log = full_log + "\n\n== " + err_msg + "\n"
                    log_key = _upload_build_log_via_controller(
                        session, controller_url, attempt_id, fail_log.encode("utf-8")
                    )
                    _post_worker_build_result(
                        session,
                        controller_url,
                        attempt_id,
                        {
                            "status": "failed",
                            "log_r2_key": log_key,
                            "error_message": fail_log[:2000],
                            "artifacts": [],
                        },
                    )
                    return
                for f in rpm_paths:
                    if not f.is_file():
                        continue
                    fname = f.name
                    try:
                        rpm_upload_stage.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        pass
                    staged = rpm_upload_stage / fname
                    try:
                        f.replace(staged)
                    except OSError as stage_err:
                        if isinstance(stage_err, FileNotFoundError):
                            # If tmp tree was removed unexpectedly, recreate staging once and retry.
                            try:
                                rpm_upload_stage.mkdir(parents=True, exist_ok=True)
                                f.replace(staged)
                            except OSError as stage_err2:
                                err_msg = (
                                    f"Could not move RPM out of mock resultdir for upload ({fname}): "
                                    f"{stage_err2}"
                                )
                                logger.exception("%s", err_msg)
                                fail_log = full_log + "\n\n== " + err_msg + "\n"
                                log_key = _upload_build_log_via_controller(
                                    session, controller_url, attempt_id, fail_log.encode("utf-8")
                                )
                                _post_worker_build_result(
                                    session,
                                    controller_url,
                                    attempt_id,
                                    {
                                        "status": "failed",
                                        "log_r2_key": log_key,
                                        "error_message": fail_log[:2000],
                                        "artifacts": artifacts_payload,
                                    },
                                )
                                return
                            continue
                        err_msg = f"Could not move RPM out of mock resultdir for upload ({fname}): {stage_err}"
                        logger.exception("%s", err_msg)
                        fail_log = full_log + "\n\n== " + err_msg + "\n"
                        log_key = _upload_build_log_via_controller(
                            session, controller_url, attempt_id, fail_log.encode("utf-8")
                        )
                        _post_worker_build_result(
                            session,
                            controller_url,
                            attempt_id,
                            {
                                "status": "failed",
                                "log_r2_key": log_key,
                                "error_message": fail_log[:2000],
                                "artifacts": artifacts_payload,
                            },
                        )
                        return
                    try:
                        if pesign_cfg and not fname.endswith(".src.rpm"):
                            pesign_rpm_if_configured(staged, pesign_cfg)
                    except PesignRepackError as pe:
                        err_msg = f"pesign failed for {fname}: {pe}"
                        logger.exception("%s", err_msg)
                        fail_log = full_log + "\n\n== " + err_msg + "\n"
                        log_key = _upload_build_log_via_controller(
                            session, controller_url, attempt_id, fail_log.encode("utf-8")
                        )
                        _post_worker_build_result(
                            session,
                            controller_url,
                            attempt_id,
                            {
                                "status": "failed",
                                "log_r2_key": log_key,
                                "error_message": fail_log[:2000],
                                "artifacts": artifacts_payload,
                            },
                        )
                        return
                    try:
                        meta = _upload_rpm_to_controller(session, controller_url, attempt_id, staged)
                    except OSError as pe:
                        if isinstance(pe, FileNotFoundError):
                            err_msg = f"RPM disappeared during upload ({fname}): {pe}"
                            logger.exception("%s", err_msg)
                            fail_log = full_log + "\n\n== " + err_msg + "\n"
                            log_key = _upload_build_log_via_controller(
                                session, controller_url, attempt_id, fail_log.encode("utf-8")
                            )
                            _post_worker_build_result(
                                session,
                                controller_url,
                                attempt_id,
                                {
                                    "status": "failed",
                                    "log_r2_key": log_key,
                                    "error_message": fail_log[:2000],
                                    "artifacts": artifacts_payload,
                                },
                            )
                            return
                        if not isinstance(pe, PermissionError):
                            raise
                        err_msg = f"{perm_hint}\n\nWhile uploading {fname}: {type(pe).__name__}: {pe}"
                        logger.exception("%s", err_msg)
                        fail_log = full_log + "\n\n== " + err_msg + "\n"
                        log_key = _upload_build_log_via_controller(
                            session, controller_url, attempt_id, fail_log.encode("utf-8")
                        )
                        _post_worker_build_result(
                            session,
                            controller_url,
                            attempt_id,
                            {
                                "status": "failed",
                                "log_r2_key": log_key,
                                "error_message": fail_log[:2000],
                                "artifacts": artifacts_payload,
                            },
                        )
                        return
                    except Exception as upload_err:
                        err_msg = f"Failed to upload RPM to controller ({fname}): {upload_err}"
                        logger.exception("%s", err_msg)
                        fail_log = full_log + "\n\n== " + err_msg + "\n"
                        log_key = _upload_build_log_via_controller(
                            session, controller_url, attempt_id, fail_log.encode("utf-8")
                        )
                        _post_worker_build_result(
                            session,
                            controller_url,
                            attempt_id,
                            {
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

                log_key = _upload_build_log_via_controller(
                    session, controller_url, attempt_id, full_log.encode("utf-8")
                )

                _post_worker_build_result(
                    session,
                    controller_url,
                    attempt_id,
                    {
                        "status": "success" if ok else "failed",
                        "log_r2_key": log_key,
                        "error_message": None if ok else full_log[:2000],
                        "artifacts": artifacts_payload,
                    },
                )
        finally:
            _cleanup_job_disk(attempt_id, scrub_mock_cfg, scrub_mock_ext, tmpdir)
    except Exception as exc:
        logger.exception("Unhandled error while processing job")
        aid = job.get("build_attempt_id") if isinstance(job, dict) else None
        if aid is not None:
            _try_post_orphan_failure(
                controller_url,
                worker_headers,
                str(job.get("releasename") or "oreon11"),
                int(aid),
                exc,
            )
    finally:
        done_event.set()
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def main() -> None:
    worker_name = os.environ.get("OREON_WORKER_NAME", "worker-1")
    worker_token = os.environ.get("OREON_WORKER_TOKEN")
    if not worker_token:
        logger.error("OREON_WORKER_TOKEN not set")
        sys.exit(1)
    controller_url = _controller_base_url()
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

    # Clean up any stale mock chroot dirs left over from a previous worker crash/restart.
    _prune_stale_var_lib_mock_dirs()

    active: list[threading.Thread] = []
    last_heartbeat = 0.0
    last_mock_prune = 0.0

    while True:
        active = [t for t in active if t.is_alive()]

        now = time.time()
        if now - last_mock_prune > 3600:
            threading.Thread(target=_prune_stale_var_lib_mock_dirs, daemon=True, name="oreon-mock-pruner").start()
            last_mock_prune = now

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
            raw_aid = job.get("build_attempt_id")
            try:
                aid = int(raw_aid) if raw_aid is not None else None
            except (TypeError, ValueError):
                aid = None
            if aid is not None:
                with _inflight_attempt_lock:
                    if aid in _inflight_attempt_ids:
                        logger.warning(
                            "Ignoring duplicate poll job for build_attempt_id=%s (already running locally)",
                            aid,
                        )
                        time.sleep(2)
                        continue
                    _inflight_attempt_ids.add(aid)
                _mark_inflight_attempt_external(aid)

            def _run_job() -> None:
                try:
                    _process_job(controller_url, headers, job)
                finally:
                    if aid is not None:
                        with _inflight_attempt_lock:
                            _inflight_attempt_ids.discard(aid)
                        _clear_inflight_attempt_external(aid)

            t = threading.Thread(target=_run_job, daemon=True)
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
