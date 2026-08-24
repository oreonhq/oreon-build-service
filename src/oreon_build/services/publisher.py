from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import json
import xml.etree.ElementTree as ET
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import List

from oreon_build.config import get_settings
from oreon_build.services.r2 import R2Client, get_r2_client
from oreon_build.services.signing import sign_repomd

logger = logging.getLogger(__name__)


def _purge_repodata_cache(r2_prefix: str) -> None:
    settings = get_settings()
    zone_id = (getattr(settings, "cloudflare_zone_id", None) or os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip()
    token = (getattr(settings, "cloudflare_api_token", None) or os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    public_url = (getattr(settings, "r2_public_url", None) or "").strip().rstrip("/")
    prefix = r2_prefix.strip("/")
    if not zone_id or not token or not public_url or not prefix:
        return
    urls = [
        f"{public_url}/{prefix}/repodata/*",
        f"{public_url}/{prefix}/repodata/repomd.xml",
        f"{public_url}/{prefix}/repodata/repomd.xml.asc",
    ]
    body = json.dumps({"files": urls}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("success"):
            raise RuntimeError(result.get("errors") or "Cloudflare cache purge failed")
        logger.info("Purged Cloudflare repodata cache for %s", prefix)
    except Exception:
        logger.exception("Cloudflare repodata cache purge failed for %s", prefix)


def _purge_replaced_rpm_cache(r2_prefix: str, filenames: list[str] | None) -> None:
    settings = get_settings()
    zone_id = (getattr(settings, "cloudflare_zone_id", None) or os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip()
    token = (getattr(settings, "cloudflare_api_token", None) or os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
    public_url = (getattr(settings, "r2_public_url", None) or "").strip().rstrip("/")
    names = sorted({name for name in (filenames or []) if name and name.lower().endswith(".rpm")})
    if not zone_id or not token or not public_url or not names:
        return
    urls = [f"{public_url}/{r2_prefix.strip('/')}/RPMS/{name}" for name in names]
    body = json.dumps({"files": urls}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("success"):
            raise RuntimeError(result.get("errors") or "Cloudflare cache purge failed")
        logger.info("Purged Cloudflare cache for %d replaced RPMs under %s", len(urls), r2_prefix)
    except Exception:
        logger.exception("Cloudflare RPM cache purge failed for %s", r2_prefix)


_BUILDSYS_GROUP_PREFER: list[str] = [
    "bash",
    "coreutils",
    "cpio",
    "diffutils",
    "findutils",
    "gawk",
    "gcc",
    "gcc-c++",
    "grep",
    "gzip",
    "make",
    "patch",
    "redhat-rpm-config",
    "rpm-build",
    "sed",
    "tar",
    "unzip",
    "util-linux",
    "which",
    "xz",
]

def _rpm_name_from_filename(filename: str) -> str | None:
    base = filename.strip()
    if not base.endswith(".rpm") or base.endswith(".src.rpm"):
        return None
    base = base[:-4]
    parts = base.rsplit("-", 2)
    if len(parts) != 3:
        return None
    return parts[0] or None

def _discover_available_binary_names(rpms_dir: Path) -> list[str]:
    out: set[str] = set()
    for f in rpms_dir.iterdir():
        if not f.is_file():
            continue
        n = _rpm_name_from_filename(f.name)
        if n:
            out.add(n)
    return sorted(out)

def _fedora_comps_repo_path() -> Path:
    raw = (os.environ.get("OREON_FEDORA_COMPS_REPO_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "vendor" / "fedora-comps"

def _select_local_fedora_comps_template(repo_path: Path) -> Path | None:
    explicit = (os.environ.get("OREON_FEDORA_COMPS_TEMPLATE_FILE") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        logger.warning("OREON_FEDORA_COMPS_TEMPLATE_FILE does not exist: %s", p)
    best_num = -1
    best_path: Path | None = None
    for base in (repo_path, repo_path / "f"):
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if not p.is_file():
                continue
            m = re.fullmatch(r"comps-f(\d+)\.xml\.in", p.name)
            if not m:
                continue
            n = int(m.group(1))
            if n > best_num:
                best_num = n
                best_path = p
    return best_path

def _load_fedora_comps_template_text() -> str | None:
    repo_path = _fedora_comps_repo_path()
    template_path = _select_local_fedora_comps_template(repo_path)
    if not template_path:
        logger.warning(
            "Fedora comps local clone not found or missing templates. Expected clone at: %s",
            repo_path,
        )
        return None
    try:
        body = template_path.read_text(encoding="utf-8")
        if "<comps" not in body:
            raise RuntimeError("local comps template is not valid XML")
        return body
    except Exception as exc:
        logger.warning("Could not read local Fedora comps template path=%s: %s", template_path, exc)
        return None

def _buildsys_group_xml(selected: list[str]) -> str:
    lines: list[str] = [
        "  <group>",
        "    <id>buildsys-build</id>",
        "    <name>buildsys-build</name>",
        "    <description>Buildroot package set for mock SRPM/RPM builds</description>",
        "    <default>false</default>",
        "    <uservisible>false</uservisible>",
        "    <packagelist>",
    ]
    for pkg in selected:
        safe = re.sub(r"[^A-Za-z0-9+_.-]", "", pkg)
        if not safe:
            continue
        lines.append(f'      <packagereq type="mandatory">{safe}</packagereq>')
    lines += [
        "    </packagelist>",
        "  </group>",
        "",
    ]
    return "\n".join(lines)

def _write_buildsys_comps(repo_dir: Path) -> Path | None:
    rpms_dir = repo_dir / "RPMS"
    if not rpms_dir.is_dir():
        return None
    available = _discover_available_binary_names(rpms_dir)
    if not available:
        return None

    selected = [n for n in _BUILDSYS_GROUP_PREFER if n in set(available)]
    if not selected:
        selected = available[: min(8, len(available))]

    template = _load_fedora_comps_template_text()
    if template and "<comps" in template:
        lines = [template.rstrip(), ""]
        if "<id>buildsys-build</id>" not in template:
            group = _buildsys_group_xml(selected)
            if "</comps>" in template:
                lines = [template.replace("</comps>", group + "</comps>").rstrip(), ""]
            else:
                lines = [template.rstrip(), "<comps>", group, "</comps>", ""]
    else:
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<comps>",
            _buildsys_group_xml(selected).rstrip(),
            "</comps>",
            "",
        ]
    out = repo_dir / ".oreon-comps.xml"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out

def _progress_stdout_enabled() -> bool:
    v = (os.environ.get("OREON_REPO_PROGRESS_STDOUT") or "").strip().lower()
    return v in ("1", "true", "yes", "on")

def _progress(msg: str) -> None:
    if _progress_stdout_enabled():
        print(f"PROGRESS {msg}", flush=True)

class RepodataDiskQuotaError(RuntimeError):
    pass

def _repo_temp_dir_label(tmp_parent: str | None) -> str:
    if tmp_parent:
        return tmp_parent
    return os.environ.get("TMPDIR") or "/tmp"

def _is_disk_quota_or_no_space(err: OSError) -> bool:
    en = getattr(err, "errno", None)
    if en is None:
        return False
    if en == getattr(errno, "ENOSPC", 28):
        return True
    return en == getattr(errno, "EDQUOT", 122)

def _ensure_dir_parent(path: Path) -> str | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if path.is_dir():
        return str(path)
    return None

def _repo_temp_dir_parent() -> str | None:
    raw = (os.environ.get("OREON_REPO_TMPDIR") or "").strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    try:
        s = get_settings()
        cfg = (getattr(s, "oreon_repo_tmpdir", None) or "").strip()
        if cfg and cfg not in candidates:
            candidates.append(cfg)
    except Exception:
        pass
    for c in candidates:
        p = Path(c).expanduser()
        resolved = _ensure_dir_parent(p)
        if resolved:
            return resolved
    try:
        home_default = Path.home() / ".oreon" / "repo-tmp"
        resolved = _ensure_dir_parent(home_default)
        if resolved:
            return resolved
    except OSError:
        pass
    return None

def _stage_container_for_prefix(r2_prefix: str) -> Path:
    parent = _repo_temp_dir_parent()
    if not parent:
        parent = str(Path.home() / ".oreon" / "repo-tmp")
    h = hashlib.sha256(r2_prefix.strip().encode()).hexdigest()[:24]
    return Path(parent) / "stages" / h

def _persistent_repo_root(r2_prefix: str) -> Path:
    return _stage_container_for_prefix(r2_prefix) / "repo"

def _ensure_stage_sidecar(container: Path, r2_prefix: str) -> None:
    p = container / ".r2_prefix.txt"
    if p.exists():
        return
    try:
        p.write_text(r2_prefix.strip() + "\n", encoding="utf-8")
    except OSError:
        pass

@contextmanager
def prefix_publish_lock(r2_prefix: str) -> object:
    parent = _repo_temp_dir_parent()
    if not parent:
        parent = str(Path.home() / ".oreon" / "repo-tmp")
    lock_dir = Path(parent) / "publish-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(r2_prefix.strip().encode()).hexdigest()[:24]
    lock_path = lock_dir / f"{h}.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

@contextmanager
def rpm_publish_locks(r2_prefix: str, filenames: list[str] | set[str]) -> object:
    names = sorted({Path(str(name)).name for name in filenames if str(name).strip()})
    with ExitStack() as stack:
        for name in names:
            stack.enter_context(prefix_publish_lock(f"{r2_prefix}/RPM/{name}"))
        yield

@contextmanager
def _repo_stage_lock(container: Path) -> object:
    container.mkdir(parents=True, exist_ok=True)
    lock_path = container / ".oreon-repodata.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

@contextmanager
def _global_repogen_lock() -> object:
    parent = _repo_temp_dir_parent()
    if not parent:
        parent = str(Path.home() / ".oreon" / "repo-tmp")
    lock_dir = Path(parent)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".oreon-global-repogen.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

_CREATEREPO_C_MAX_WORKERS = 100

def _createrepo_cpu_count() -> int:
    return max(1, os.cpu_count() or 1)

def _createrepo_workers_hard_cap() -> int:
    raw = (os.environ.get("OREON_CREATEREPO_WORKERS_HARD_CAP") or "").strip()
    if raw:
        try:
            return max(1, min(int(raw), _CREATEREPO_C_MAX_WORKERS))
        except ValueError:
            pass
    return _CREATEREPO_C_MAX_WORKERS

def _createrepo_workers_default() -> int:
    cpus = _createrepo_cpu_count()
    return min(max(50, cpus * 4), _createrepo_workers_hard_cap())

def _createrepo_workers() -> int:
    cap = _createrepo_workers_hard_cap()
    raw = (os.environ.get("OREON_CREATEREPO_WORKERS") or "").strip()
    if raw:
        try:
            w = int(raw)
        except ValueError:
            w = _createrepo_workers_default()
        else:
            if w > _CREATEREPO_C_MAX_WORKERS:
                logger.warning(
                    "OREON_CREATEREPO_WORKERS=%s exceeds createrepo_c max %s, using %s",
                    w,
                    _CREATEREPO_C_MAX_WORKERS,
                    cap,
                )
    else:
        w = _createrepo_workers_default()
    return max(1, min(w, cap))

def _createrepo_fallback_workers(primary: int) -> int:
    raw = (os.environ.get("OREON_CREATEREPO_FALLBACK_WORKERS") or "").strip()
    if raw:
        try:
            fb = int(raw)
        except ValueError:
            fb = max(8, primary // 4)
    else:
        fb = max(8, primary // 4)
    cap = min(_createrepo_workers_hard_cap(), max(8, primary // 2))
    fb = max(1, min(fb, cap))
    if fb >= primary:
        fb = max(1, primary // 2 or 1)
    return fb

def _createrepo_timeout_sec() -> int:
    raw = (os.environ.get("OREON_CREATEREPO_TIMEOUT_SEC") or "1800").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 1800
    return max(60, min(n, 8 * 3600))

def _r2_etag_strong_content_md5(etag: str) -> bool:
    t = (etag or "").strip()
    if not t or "-" in t:
        return False
    if len(t) != 32:
        return False
    t = t.lower()
    for c in t:
        if c not in "0123456789abcdef":
            return False
    return True

def _local_rpm_bytes_match_r2_etag(path: Path, etag: str) -> bool:
    if not _r2_etag_strong_content_md5(etag) or not path.is_file() or path.stat().st_size == 0:
        return False
    m = hashlib.md5()
    m.update(path.read_bytes())
    return m.hexdigest() == etag.lower()

def _etag_cache_path(container: Path) -> Path:
    return container / ".etag-cache.json"

def _load_etag_cache(container: Path) -> dict[str, str]:
    import json

    p = _etag_cache_path(container)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}

def _save_etag_cache(container: Path, cache: dict[str, str]) -> None:
    import json

    p = _etag_cache_path(container)
    try:
        tmp = p.parent / (p.name + ".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(p)
    except Exception as exc:
        logger.debug("Could not save etag cache to %s: %s", p, exc)

def _sync_rpms_dir_from_r2(
    client: R2Client,
    rpms_prefix: str,
    rpms_dir: Path,
    *,
    container: Path | None = None,
) -> int:
    import json as _json

    rpms_dir.mkdir(parents=True, exist_ok=True)
    objs = client.list_objects(rpms_prefix)
    wanted: dict[str, tuple[str, int, str]] = {}
    for o in objs:
        key = o.get("key")
        if not key:
            continue
        base = key.split("/")[-1]
        if not base.endswith(".rpm"):
            continue
        sz = int(o.get("size") or 0)
        etag = (o.get("etag") or "").strip()
        if base in wanted:
            prev_key, _, _ = wanted[base]
            logger.warning(
                "Duplicate RPM basename in R2 listing, last wins: base=%s was_key=%s now_key=%s",
                base,
                prev_key,
                key,
            )
        wanted[base] = (key, sz, etag)

    etag_cache: dict[str, str] = _load_etag_cache(container) if container else {}
    cache_dirty = False

    for f in list(rpms_dir.iterdir()):
        if f.is_file() and f.suffix == ".rpm" and f.name not in wanted:
            try:
                f.unlink()
            except OSError as e:
                logger.warning("Could not remove stale local RPM %s: %s", f, e)
            etag_cache.pop(f.name, None)
            cache_dirty = True

    fetched = 0
    for fname, (key, sz, etag) in wanted.items():
        dest = rpms_dir / fname
        need_fetch = True
        if sz > 0 and dest.is_file() and dest.stat().st_size == sz:
            if etag and _r2_etag_strong_content_md5(etag):
                cached_etag = etag_cache.get(fname)
                if cached_etag == etag:
                    need_fetch = False
                elif _local_rpm_bytes_match_r2_etag(dest, etag):
                    need_fetch = False
                    etag_cache[fname] = etag
                    cache_dirty = True
            # no strong etag: never trust size alone. R2 can replace bytes at same size.

        if not need_fetch:
            continue
        data = client.get_object(key)
        if not data:
            raise RuntimeError(f"missing or empty RPM object at {key!r}, aborting repodata update")
        _write_staged_rpm(dest, data)
        if dest.stat().st_size != len(data):
            raise RuntimeError(
                f"local RPM write size mismatch for {dest}: got {dest.stat().st_size} expected {len(data)}"
            )
        fetched += 1
        if etag and _r2_etag_strong_content_md5(etag):
            etag_cache[fname] = etag
            cache_dirty = True

    if cache_dirty and container:
        _save_etag_cache(container, etag_cache)

    if fetched:
        logger.info("Fetched %d new/changed RPMs from R2 under %s", fetched, rpms_prefix)

    return len(wanted), fetched


def _repair_corrupt_local_rpms(
    client: R2Client,
    rpms_prefix: str,
    rpms_dir: Path,
) -> int:
    if not rpms_dir.is_dir():
        return 0
    repaired = 0
    removed = 0
    for path in list(rpms_dir.glob("*.rpm")):
        try:
            sz = path.stat().st_size
        except OSError:
            continue
        bad = sz <= 0
        if not bad:
            continue
        key = f"{rpms_prefix}{path.name}"
        data = client.get_object(key)
        if data:
            _write_staged_rpm(path, data)
            if path.stat().st_size == len(data) and path.stat().st_size > 0:
                repaired += 1
                logger.warning("Re-fetched corrupt local RPM from R2: %s (%s bytes)", path.name, len(data))
                continue
        try:
            path.unlink()
            removed += 1
            logger.warning("Removed unreadable local RPM %s (size=%s)", path.name, sz)
        except OSError as e:
            logger.warning("Could not remove corrupt local RPM %s: %s", path, e)
    if repaired or removed:
        logger.info(
            "Local RPM repair under %s repaired=%s removed=%s",
            rpms_dir,
            repaired,
            removed,
        )
    return repaired + removed

def _list_repodata_keys(client: R2Client, r2_prefix: str) -> list[str]:
    repodata_prefix = f"{r2_prefix}/repodata/"
    objs = client.list_objects(repodata_prefix)
    keys: list[str] = []
    for o in objs:
        k = o.get("key")
        if k:
            keys.append(k)
    return keys

def _cleanup_old_repodata(
    client: R2Client, r2_prefix: str, keep_filenames: set[str]
) -> None:
    old_keys = _list_repodata_keys(client, r2_prefix)
    if not old_keys:
        return

    to_delete: list[str] = []
    for k in old_keys:
        filename = k.split("/")[-1]
        if filename not in keep_filenames:
            to_delete.append(k)

    if not to_delete:
        return

    for k in to_delete:
        client.delete_object(k)

    logger.info(
        "Purged %d stale repodata objects from %s",
        len(to_delete),
        f"{r2_prefix}/repodata/",
    )

def _createrepo_cache_dir(container: Path) -> Path:
    d = container / ".createrepo-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _clear_createrepo_cache(container: Path) -> None:
    d = container / ".createrepo-cache"
    if d.exists():
        try:
            shutil.rmtree(d)
        except OSError as e:
            logger.warning("Could not clear createrepo cache %s: %s", d, e)

def _sync_repodata_from_r2(client: R2Client, r2_prefix: str, repodata_dir: Path) -> int:
    repodata_dir.mkdir(parents=True, exist_ok=True)
    keys = _list_repodata_keys(client, r2_prefix)
    written = 0
    for key in keys:
        fname = key.rsplit("/", 1)[-1]
        if not fname:
            continue
        data = client.get_object(key)
        if not data:
            continue
        (repodata_dir / fname).write_bytes(data)
        written += 1
    return written


def _drop_local_rpms_not_in_wanted(
    rpms_dir: Path,
    wanted: set[str],
    *,
    container: Path | None = None,
) -> list[str]:
    """Remove local stage RPMs that are gone from R2.

    Prune deletes R2 objects but warm-stage publishes used to skip a full
    mirror sync, so createrepo --update kept advertising the dead NEVRAs.
    """
    if not rpms_dir.is_dir():
        return []
    dropped: list[str] = []
    for path in list(rpms_dir.glob("*.rpm")):
        if path.name in wanted:
            continue
        try:
            path.unlink()
            dropped.append(path.name)
        except OSError as e:
            logger.warning("Could not remove stale local RPM %s: %s", path, e)
    if dropped:
        _invalidate_etag_cache_entries(container, set(dropped))
        logger.info(
            "Dropped %s local RPMs absent from R2 under %s (sample=%s)",
            len(dropped),
            rpms_dir,
            dropped[:8],
        )
    return dropped


def _invalidate_etag_cache_entries(container: Path | None, filenames: set[str]) -> None:
    if not container or not filenames:
        return
    cache = _load_etag_cache(container)
    dirty = False
    for fname in filenames:
        if fname in cache:
            cache.pop(fname, None)
            dirty = True
    if dirty:
        _save_etag_cache(container, cache)


def _refresh_staged_rpms_from_r2(
    client: R2Client,
    rpms_prefix: str,
    rpms_dir: Path,
    staged: list[str],
    *,
    container: Path | None = None,
) -> None:
    """Pull staged RPM basenames from R2 onto the local createrepo stage.

    Always re-fetch. Side-tag promote / identical-NEVRA rebuilds overwrite the same
    key on R2 (often same Content-Length after resign). Size-skip left createrepo
    hashing stale locals while clients downloaded the new R2 bytes → checksum mismatch.
    """
    rpms_dir.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for fname in staged:
        key = f"{rpms_prefix}{fname}"
        local = rpms_dir / fname
        data = client.get_object(key)
        if not data:
            raise RuntimeError(f"staged RPM missing on R2: {key}")
        _write_staged_rpm(local, data)
        fetched += 1
    logger.info(
        "staged RPM refresh fetched %s file(s) under %s",
        fetched,
        rpms_prefix,
    )
    _invalidate_etag_cache_entries(container, set(staged))


def _ensure_local_repodata_baseline(
    client: R2Client,
    r2_prefix: str,
    repo_root: Path,
    *,
    min_packages: int,
) -> None:
    if _repodata_looks_valid(repo_root, min_packages=min_packages):
        return
    logger.warning(
        "Local repodata missing or truncated at %s, pulling repodata baseline from R2",
        r2_prefix,
    )
    _sync_repodata_from_r2(client, r2_prefix, repo_root / "repodata")
    if not _repodata_looks_valid(repo_root, min_packages=min_packages):
        raise RuntimeError(
            f"Cannot run incremental repodata update for {r2_prefix}: "
            "no valid repodata on local stage or R2. Run an explicit full regen."
        )


def _write_staged_rpm(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    path.write_bytes(data)
    os.utime(path, None)


def _run_createrepo(
    repo_dir: Path,
    *,
    container: Path | None = None,
    force_full: bool = False,
) -> None:
    def _cleanup_tmp_repodata() -> None:
        tmp_repodata = repo_dir / ".repodata"
        if tmp_repodata.exists():
            try:
                shutil.rmtree(tmp_repodata)
                logger.warning("Removed stale createrepo temp dir %s", tmp_repodata)
            except OSError as e:
                logger.warning("Could not remove stale createrepo temp dir %s: %s", tmp_repodata, e)

    if force_full and container is not None:
        _clear_createrepo_cache(container)
        repodata = repo_dir / "repodata"
        if repodata.exists():
            try:
                shutil.rmtree(repodata)
            except OSError as e:
                logger.warning("Could not remove repodata before full regen %s: %s", repodata, e)

    def _run_once(workers: int) -> None:
        timeout_s = _createrepo_timeout_sec()
        comps_file = _write_buildsys_comps(repo_dir)
        use_update = (
            not force_full
            and container is not None
            and (repo_dir / "repodata" / "repomd.xml").exists()
        )
        logger.info(
            "createrepo_c starting repo_root=%s workers=%s update=%s force_full=%s",
            repo_dir,
            workers,
            use_update,
            force_full,
        )
        cmd = [
            "createrepo_c",
            str(repo_dir),
            f"--workers={workers}",
        ]
        if use_update:
            cmd.append("--update")
        if container is not None:
            try:
                cache_dir = _createrepo_cache_dir(container)
                cmd += ["--cachedir", str(cache_dir)]
            except OSError:
                pass
        if comps_file is not None and comps_file.exists():
            cmd += ["--groupfile", str(comps_file)]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"createrepo_c timed out after {timeout_s}s for repo_root={repo_dir} workers={workers}"
            ) from e
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        if stderr:
            for line in stderr.splitlines()[-40:]:
                logger.warning("createrepo_c: %s", line)
        if stdout:
            for line in stdout.splitlines()[-20:]:
                logger.info("createrepo_c: %s", line)
        repomd = repo_dir / "repodata" / "repomd.xml"
        if proc.returncode != 0:
            if proc.returncode == 2 and repomd.exists() and repomd.stat().st_size > 0:
                logger.warning(
                    "createrepo_c exited %s with invalid-package warnings but wrote %s; continuing",
                    proc.returncode,
                    repomd,
                )
            else:
                detail = stderr or stdout or f"exit {proc.returncode}"
                raise RuntimeError(
                    f"createrepo_c failed repo_root={repo_dir} workers={workers}: {detail}"
                )
        if not repomd.exists():
            raise RuntimeError(f"createrepo_c finished but {repomd} is missing")
        logger.info("createrepo_c finished repo_root=%s workers=%s update=%s", repo_dir, workers, use_update)

    _cleanup_tmp_repodata()
    w = _createrepo_workers()
    try:
        _run_once(w)
    except Exception:
        fb = _createrepo_fallback_workers(w)
        if fb == w:
            raise
        logger.exception(
            "createrepo_c failed with workers=%s, retrying once with workers=%s after cleanup",
            w,
            fb,
        )
        _cleanup_tmp_repodata()
        _run_once(fb)

def _upload_repodata_dir_ordered(client: R2Client, r2_prefix: str, repodata_dir: Path) -> None:
    files = [f for f in repodata_dir.iterdir() if f.is_file()]
    by_name = {f.name: f for f in files}
    tail: list[str] = []
    if "repomd.xml" in by_name:
        tail.append("repomd.xml")
    if "repomd.xml.asc" in by_name:
        tail.append("repomd.xml.asc")
    head = sorted(n for n in by_name if n not in tail)
    for name in head + tail:
        f = by_name[name]
        r2_key = f"{r2_prefix}/repodata/{name}"
        client.put_object(
            r2_key,
            f.read_bytes(),
            cache_control="no-store, no-cache, max-age=0, must-revalidate",
        )

def _count_primary_packages(repodata_dir: Path) -> int:
    if not repodata_dir.is_dir():
        return 0
    primary_file: Path | None = None
    for f in repodata_dir.iterdir():
        if f.is_file() and "primary" in f.name and f.stat().st_size > 256:
            primary_file = f
            break
    if primary_file is None:
        return 0
    raw = primary_file.read_bytes()
    try:
        if primary_file.name.endswith(".zst"):
            with tempfile.TemporaryDirectory() as td:
                zst = Path(td) / primary_file.name
                zst.write_bytes(raw)
                out = Path(td) / "primary.xml"
                subprocess.run(
                    ["zstd", "-d", "-f", str(zst), "-o", str(out)],
                    check=True,
                    capture_output=True,
                )
                proot = ET.parse(out).getroot()
        elif primary_file.name.endswith(".gz"):
            import gzip

            proot = ET.fromstring(gzip.decompress(raw))
        else:
            proot = ET.fromstring(raw)
    except Exception as exc:
        logger.warning("Could not read primary metadata %s: %s", primary_file, exc)
        return 0
    return len(proot.findall("{http://linux.duke.edu/metadata/common}package"))


def _validate_repodata_sane(repo_root: Path, *, min_packages: int) -> None:
    repodata_dir = repo_root / "repodata"
    repomd = repodata_dir / "repomd.xml"
    if not repomd.is_file():
        raise RuntimeError(f"createrepo produced no repomd.xml under {repodata_dir}")
    pkgs = _count_primary_packages(repodata_dir)
    floor = max(1, int(min_packages * 0.9))
    if pkgs < floor:
        raise RuntimeError(
            f"repodata sanity check failed: primary has {pkgs} packages, expected at least {floor} "
            f"(rpm_count={min_packages}). Refusing to upload truncated metadata."
        )


def _repodata_looks_valid(repo_root: Path, *, min_packages: int = 100) -> bool:
    repodata = repo_root / "repodata"
    if not (repodata / "repomd.xml").is_file():
        return False
    return _count_primary_packages(repodata) >= max(1, int(min_packages * 0.9))


def update_repodata_at_prefix(
    r2_prefix: str,
    r2_client: R2Client | None = None,
    *,
    force_full: bool = False,
    staged_binaries: list[str] | None = None,
) -> bool:
    client = r2_client or get_r2_client()
    rpms_prefix = f"{r2_prefix}/RPMS/"
    objs = client.list_objects(rpms_prefix)
    rpm_keys = [
        o["key"]
        for o in objs
        if o.get("key") and o["key"].split("/")[-1].lower().endswith(".rpm")
    ]
    if not rpm_keys:
        logger.warning("No RPMs at %s, skipping createrepo", rpms_prefix)
        return False
    _progress(f"{r2_prefix} listed {len(rpm_keys)} RPM keys from R2")
    tmp_parent = _repo_temp_dir_parent()
    container = _stage_container_for_prefix(r2_prefix)
    _ensure_stage_sidecar(container, r2_prefix)
    repo_root = _persistent_repo_root(r2_prefix)
    rpms_dir = repo_root / "RPMS"
    try:
        with _repo_stage_lock(container):
            staged = [b for b in (staged_binaries or []) if b and b.endswith(".rpm")]
            has_local_rpms = any(rpms_dir.glob("*.rpm")) if rpms_dir.is_dir() else False
            local_rpm_count = sum(1 for _ in rpms_dir.glob("*.rpm")) if has_local_rpms else 0
            local_mirror_ready = has_local_rpms and local_rpm_count >= max(
                100, int(len(rpm_keys) * 0.9)
            )
            rpm_count = local_rpm_count or len(rpm_keys)

            if staged:
                wanted = {k.split("/")[-1] for k in rpm_keys}
                if not local_mirror_ready:
                    _progress(f"{r2_prefix} syncing RPM mirror for staged publish")
                    rpm_count, _synced = _sync_rpms_dir_from_r2(
                        client, rpms_prefix, rpms_dir, container=container
                    )
                else:
                    # prune deletes R2 only. drop dead locals or --update keeps ghosts.
                    _drop_local_rpms_not_in_wanted(
                        rpms_dir, wanted, container=container
                    )
                    rpm_count = sum(1 for _ in rpms_dir.glob("*.rpm")) if rpms_dir.is_dir() else 0
                try:
                    _ensure_local_repodata_baseline(
                        client, r2_prefix, repo_root, min_packages=len(rpm_keys)
                    )
                    staged_force_full = False
                except RuntimeError as e:
                    logger.warning(
                        "%s; falling back to full createrepo for %s",
                        e,
                        r2_prefix,
                    )
                    staged_force_full = True
                if staged_force_full:
                    logger.info(
                        "Full createrepo for %s after mirror sync local_rpms=%s (skipping per-RPM staged refresh of %s files)",
                        r2_prefix,
                        rpm_count,
                        len(staged),
                    )
                else:
                    _refresh_staged_rpms_from_r2(
                        client, rpms_prefix, rpms_dir, staged, container=container
                    )
                    logger.info(
                        "Staged repodata update %s staged=%s local_rpms=%s",
                        r2_prefix,
                        len(staged),
                        rpm_count,
                    )
                _repair_corrupt_local_rpms(client, rpms_prefix, rpms_dir)
                # only wipe cache on full regen; --update needs it for speed
                if staged_force_full and container is not None:
                    _clear_createrepo_cache(container)
                _run_createrepo(repo_root, container=container, force_full=staged_force_full)
            elif force_full:
                # warm local mirrors can still be stale vs R2 after same-NEVRA
                # overwrites. etag sync is cheap when unchanged; skipping sync made
                # "full regen" re-hash bad locals and re-publish broken checksums.
                _progress(f"{r2_prefix} syncing RPMs to local stage for full regen")
                rpm_count, fetched = _sync_rpms_dir_from_r2(
                    client, rpms_prefix, rpms_dir, container=container
                )
                logger.info(
                    "Full repodata regen %s rpms=%s fetched=%s",
                    r2_prefix,
                    rpm_count,
                    fetched,
                )
                _repair_corrupt_local_rpms(client, rpms_prefix, rpms_dir)
                if container is not None:
                    _clear_createrepo_cache(container)
                _run_createrepo(repo_root, container=container, force_full=True)
            else:
                _progress(f"{r2_prefix} syncing RPMs to local stage (may take a while)")
                rpm_count, fetched = _sync_rpms_dir_from_r2(
                    client, rpms_prefix, rpms_dir, container=container
                )
                _progress(f"{r2_prefix} synced {rpm_count} RPMs ({fetched} fetched), running createrepo_c")
                if fetched == 0:
                    repodata_local = _repodata_looks_valid(repo_root, min_packages=len(rpm_keys))
                    if repodata_local:
                        logger.info("No RPM changes at %s, skipping createrepo_c", r2_prefix)
                        return True
                _repair_corrupt_local_rpms(client, rpms_prefix, rpms_dir)
                _run_createrepo(repo_root, container=container, force_full=False)

            _progress(f"{r2_prefix} createrepo_c done, uploading repodata to R2")
            repodata_dir = repo_root / "repodata"
            if repodata_dir.exists():
                _validate_repodata_sane(repo_root, min_packages=rpm_count)
                new_filenames = {f.name for f in repodata_dir.iterdir() if f.is_file()}

                repomd = repodata_dir / "repomd.xml"
                if repomd.exists():
                    sign_repomd(repomd)

                _upload_repodata_dir_ordered(client, r2_prefix, repodata_dir)
                _purge_repodata_cache(r2_prefix)
                _purge_replaced_rpm_cache(r2_prefix, staged_binaries)

                _cleanup_old_repodata(client, r2_prefix, new_filenames)
                logger.info(
                    "Updated repodata at R2 prefix %s (%s RPMs, primary=%s packages)",
                    r2_prefix,
                    rpm_count,
                    _count_primary_packages(repodata_dir),
                )
                return True
            logger.error("createrepo_c produced no repodata under %s", repo_root)
            return False
    except OSError as e:
        if _is_disk_quota_or_no_space(e):
            label = _repo_temp_dir_label(tmp_parent)
            msg = (
                f"disk quota or no space while composing repo metadata under {label}. "
                "Set OREON_REPO_TMPDIR or oreon_repo_tmpdir to a larger path (default is ~/.oreon/repo-tmp)."
            )
            logger.error(msg)
            raise RepodataDiskQuotaError(msg) from e
        raise

def publish_rpms_to_r2(
    r2_prefix: str,
    rpm_keys: List[str],
    r2_client: R2Client | None = None,
) -> None:
    client = r2_client or get_r2_client()
    tmp_parent = _repo_temp_dir_parent()
    try:
        with tempfile.TemporaryDirectory(prefix="oreon-repo-", dir=tmp_parent) as tmp:
            repo_root = Path(tmp) / "repo"
            rpms_dir = repo_root / "RPMS"
            rpms_dir.mkdir(parents=True)
            prefix = r2_prefix.rstrip("/")
            for key in rpm_keys:
                data = client.get_object(key)
                if not data:
                    raise RuntimeError(f"missing or empty RPM object at {key!r}, aborting publish")
                fname = key.split("/")[-1]
                dest = rpms_dir / fname
                if dest.exists():
                    logger.warning(
                        "Duplicate RPM basename in publish list, overwriting %s (key %s)",
                        fname,
                        key,
                    )
                dest.write_bytes(data)
                canonical = f"{prefix}/RPMS/{fname}"
                if key != canonical:
                    client.put_object(canonical, data)
            _run_createrepo(repo_root)
            repodata_dir = repo_root / "repodata"
            if repodata_dir.exists():
                new_filenames = {f.name for f in repodata_dir.iterdir()}
                _upload_repodata_dir_ordered(client, r2_prefix, repodata_dir)

                _cleanup_old_repodata(client, r2_prefix, new_filenames)
    except OSError as e:
        if _is_disk_quota_or_no_space(e):
            label = _repo_temp_dir_label(tmp_parent)
            msg = (
                f"disk quota or no space while publishing repo under {label}. "
                "Set OREON_REPO_TMPDIR or oreon_repo_tmpdir to a larger path (default is ~/.oreon/repo-tmp)."
            )
            logger.error(msg)
            raise RepodataDiskQuotaError(msg) from e
        raise
    logger.info("Published repo to R2 prefix %s", r2_prefix)
