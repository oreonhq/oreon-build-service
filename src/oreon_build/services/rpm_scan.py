from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oreon_build.core.audit import log_audit
from oreon_build.db.session import async_session_maker
from oreon_build.models import Artifact, BuildAttempt, BuildJob, BuildStatus, PublishTask, RpmScanFinding, RpmbuildPlan, RpmbuildPlanStatus
from oreon_build.services.r2 import get_r2_client

logger = logging.getLogger(__name__)

_RULES = Path(__file__).resolve().parent / "scan_rules" / "payload.yar"
_inflight: set[int] = set()
_gate = asyncio.Lock()
_HOLD = {"pending", "quarantined"}
_PASS = {"clean", "disregarded"}


def skip_scan_path(rel: str) -> bool:
    norm = rel.replace("\\", "/").lstrip("./")
    parts = [p for p in norm.split("/") if p and p != "."]
    if any(p in {"tests", "testdata"} for p in parts):
        return True
    if norm.startswith("usr/share/doc/") or "/usr/share/doc/" in f"/{norm}":
        return True
    if norm.endswith(".yar"):
        return True
    return False


def parse_clam_line(line: str, root: Path) -> tuple[str, str] | None:
    text = line.strip()
    if not text.endswith("FOUND"):
        return None
    left, _, sig = text.rpartition(":")
    sig = sig.replace("FOUND", "").strip()
    if not left or not sig:
        return None
    if sig.startswith("PUA.") or sig.startswith("Heuristics."):
        return None
    try:
        rel = str(Path(left.strip()).resolve().relative_to(root.resolve()))
    except ValueError:
        rel = left.strip()
    if skip_scan_path(rel):
        return None
    return rel, sig[:240]


def parse_yr_json(raw: str) -> list[tuple[str, str, str]]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows = data if isinstance(data, list) else data.get("matches") or data.get("rules") or []
    out: list[tuple[str, str, str]] = []
    if isinstance(data, dict) and not isinstance(rows, list):
        return []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("file") or "")
        rules = item.get("rules") or item.get("matches") or []
        if item.get("identifier") and not rules:
            rules = [item]
        for rule in rules if isinstance(rules, list) else []:
            if not isinstance(rule, dict):
                continue
            name = str(rule.get("identifier") or rule.get("rule") or "")
            if not name.startswith("oreon_"):
                continue
            snippet = ""
            matches = rule.get("matches") or rule.get("strings") or []
            if isinstance(matches, list) and matches:
                first = matches[0]
                if isinstance(first, dict):
                    snippet = str(first.get("data") or first.get("string") or first.get("matched") or "")
                else:
                    snippet = str(first)
            out.append((name[:240], path[-500:], snippet[:160]))
    return out


_clamd_lock = threading.Lock()


def _clamd_config() -> Path | None:
    env = (os.environ.get("OREON_CLAMD_CONFIG") or "").strip()
    if env:
        path = Path(env)
        if path.is_file():
            return path
    home = Path.home() / ".config" / "clamd.conf"
    if home.is_file():
        return home
    return None


def _cfg_value(cfg: Path, key: str) -> str | None:
    try:
        lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split(None, 1)
        if len(parts) == 2 and parts[0] == key:
            return parts[1].strip()
    return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _clear_stale_clamd(cfg: Path) -> None:
    pid_path = _cfg_value(cfg, "PidFile")
    if not pid_path:
        return
    path = Path(pid_path)
    if not path.is_file():
        return
    try:
        pid = int(path.read_text().strip() or "0")
    except (OSError, ValueError):
        pid = 0
    if _pid_alive(pid):
        return
    path.unlink(missing_ok=True)
    sock = _cfg_value(cfg, "LocalSocket")
    if sock:
        Path(sock).unlink(missing_ok=True)


def _clamdscan_bin() -> str:
    if _clamd_config() is not None and Path("/usr/bin/clamdscan").is_file():
        return "/usr/bin/clamdscan"
    return "clamdscan"


def _clamd_ping() -> bool:
    cmd = [_clamdscan_bin(), "--ping", "2"]
    cfg = _clamd_config()
    if cfg is not None:
        cmd[1:1] = ["--config-file", str(cfg)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and "PONG" in (proc.stdout or "")


def ensure_clamd() -> str | None:
    if shutil.which("clamdscan") is None or shutil.which("clamd") is None:
        return "clamd missing"
    if _clamd_ping():
        return None
    cfg = _clamd_config()
    if cfg is None:
        return "clamd is not running"
    with _clamd_lock:
        if _clamd_ping():
            return None
        _clear_stale_clamd(cfg)
        try:
            subprocess.Popen(
                ["clamd", "--config-file", str(cfg)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return f"clamd failed to start {exc}"[:240]
        for _ in range(60):
            time.sleep(0.5)
            if _clamd_ping():
                logger.info("clamd started")
                return None
    return "clamd is not running"


def _scan_failure(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        item = line.strip()
        if not item or item.startswith("WARNING:"):
            continue
        lines.append(item)
    for item in lines:
        low = item.lower()
        if "could not connect" in low or "ping timeout" in low:
            return "clamd is not running"
    useful = [item for item in lines if "File tree walk aborted" not in item]
    if useful:
        return useful[-1][:240]
    if lines:
        return lines[-1][:240]
    return "clamdscan failed"


def _tools_missing() -> str | None:
    for name in ("rpm2cpio", "cpio", "rpm", "clamdscan", "yr"):
        if shutil.which(name) is None:
            return f"{name} missing"
    if not _RULES.is_file():
        return "yara rules missing"
    return None


def _download(key: str, dest: Path) -> str | None:
    body = get_r2_client().get_object_stream(key)
    if body is None:
        return "rpm missing in storage"
    try:
        with dest.open("wb") as fh:
            for chunk in body.iter_chunks(1024 * 1024):
                if chunk:
                    fh.write(chunk)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    return None


def _unpack(rpm: Path, dest: Path) -> str | None:
    cpio = subprocess.run(["rpm2cpio", str(rpm)], capture_output=True, timeout=180)
    if cpio.returncode != 0 or not cpio.stdout:
        return "rpm2cpio failed"
    extracted = subprocess.run(
        ["cpio", "-idm"],
        input=cpio.stdout,
        cwd=dest,
        capture_output=True,
        timeout=300,
    )
    if extracted.returncode not in (0, 2):
        return "cpio failed"
    return None


def _run_clamdscan(root: Path) -> subprocess.CompletedProcess[str]:
    cmd = [_clamdscan_bin(), "--fdpass", "--infected", "--no-summary", str(root)]
    cfg = _clamd_config()
    if cfg is not None:
        cmd[1:1] = ["--config-file", str(cfg)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def _clam(root: Path) -> tuple[list[tuple[str, str]], str | None]:
    proc = _run_clamdscan(root)
    blob = f"{proc.stderr or ''}\n{proc.stdout or ''}"
    if proc.returncode == 2 and _scan_failure(blob) == "clamd is not running":
        if ensure_clamd() is None:
            proc = _run_clamdscan(root)
            blob = f"{proc.stderr or ''}\n{proc.stdout or ''}"
    hits: list[tuple[str, str]] = []
    for line in blob.splitlines():
        parsed = parse_clam_line(line, root)
        if parsed:
            hits.append(parsed)
    if proc.returncode == 2 and not hits:
        return [], _scan_failure(blob)
    if proc.returncode not in (0, 1, 2):
        return [], "clamdscan failed"
    return hits, None


def _yara_texts(rpm: Path, root: Path) -> Path | None:
    folder = root / "_oreon_scan_text"
    folder.mkdir(exist_ok=True)
    scripts = subprocess.run(
        ["rpm", "-qp", "--scripts", str(rpm)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    wrote = False
    body = scripts.stdout or ""
    if body.strip():
        (folder / "scripts.txt").write_text(body, encoding="utf-8", errors="replace")
        wrote = True
    for spec in root.rglob("*.spec"):
        if "_oreon_scan_text" in spec.parts:
            continue
        target = folder / spec.name
        target.write_bytes(spec.read_bytes()[:2_000_000])
        wrote = True
    return folder if wrote else None


def _yara(folder: Path) -> tuple[list[tuple[str, str, str]], str | None]:
    proc = subprocess.run(
        ["yr", "scan", "--output-format", "json", str(_RULES), str(folder)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode not in (0, 1) and not (proc.stdout or "").strip():
        err = ((proc.stderr or "yr failed").strip().splitlines() or ["yr failed"])[-1]
        return [], err[:240]
    return parse_yr_json(proc.stdout or ""), None


def scan_rpm_path(rpm: Path) -> tuple[list[dict], str | None]:
    missing = _tools_missing()
    if missing:
        return [], missing
    with tempfile.TemporaryDirectory(prefix="oreon-scan-") as tmp:
        root = Path(tmp)
        err = _unpack(rpm, root)
        if err:
            return [], err
        hits: list[dict] = []
        clam_hits, clam_err = _clam(root)
        if clam_err:
            return [], clam_err
        for rel, sig in clam_hits:
            hits.append({"engine": "clamav", "rule": sig, "file_path": rel[:1000], "evidence": sig[:160]})
        text = _yara_texts(rpm, root)
        if text is not None:
            yra, yerr = _yara(text)
            if yerr:
                return [], yerr
            for name, path, snippet in yra:
                hits.append({
                    "engine": "yara",
                    "rule": name,
                    "file_path": path or "scripts",
                    "evidence": snippet,
                })
        return hits, None


async def _enqueue(db, job_id: int, attempt_id: int) -> None:
    stmt = (
        pg_insert(PublishTask.__table__)
        .values(kind="repo_publish", attempt_id=attempt_id, build_job_id=job_id)
        .on_conflict_do_nothing(index_elements=["attempt_id"])
    )
    await db.execute(stmt)


async def gate_publish(db, job: BuildJob, attempt_id: int) -> None:
    state = (job.scan_state or "").strip()
    if state in _PASS:
        await _enqueue(db, job.id, attempt_id)
        await db.commit()
        return
    if state == "quarantined":
        return
    job.scan_state = "pending"
    await db.commit()
    schedule_rpm_scan(job.id, attempt_id)


def schedule_rpm_scan(job_id: int, attempt_id: int) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(run_job_scan(job_id, attempt_id))


async def run_job_scan(job_id: int, attempt_id: int) -> None:
    async with _gate:
        if job_id in _inflight:
            return
        _inflight.add(job_id)
    try:
        await _scan_job(job_id, attempt_id)
    finally:
        async with _gate:
            _inflight.discard(job_id)


async def _scan_job(job_id: int, attempt_id: int) -> None:
    async with async_session_maker() as db:
        job = await db.get(BuildJob, job_id)
        if job is None or job.status != BuildStatus.SUCCESS:
            return
        if (job.scan_state or "") != "pending":
            return
        arts = (
            await db.execute(
                select(Artifact).where(Artifact.build_attempt_id == attempt_id)
            )
        ).scalars().all()
        rpms = [a for a in arts if (a.filename or "").endswith(".rpm") and a.r2_key]
        if not rpms:
            job.scan_state = "clean"
            await _enqueue(db, job_id, attempt_id)
            await db.commit()
            return
        findings: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="oreon-rpm-") as tmp:
            root = Path(tmp)
            for art in rpms:
                dest = root / Path(art.filename).name
                err = await asyncio.to_thread(_download, art.r2_key, dest)
                if err:
                    if "missing" in err:
                        job.scan_state = "skipped"
                        await _enqueue(db, job_id, attempt_id)
                        await db.execute(
                            update(PublishTask)
                            .where(
                                PublishTask.attempt_id == attempt_id,
                                PublishTask.status.in_(("pending", "queued")),
                            )
                            .values(
                                status="failed",
                                error_message=err[:500],
                                finished_at=datetime.now(timezone.utc),
                            )
                        )
                        await db.commit()
                        logger.warning("rpm scan dropped job %s: %s", job_id, err)
                        return
                    logger.warning("rpm scan held job %s: %s", job_id, err)
                    return
                hits, scan_err = await asyncio.to_thread(scan_rpm_path, dest)
                if scan_err:
                    logger.warning("rpm scan held job %s: %s", job_id, scan_err)
                    return
                findings.extend(hits)
        await db.execute(delete(RpmScanFinding).where(RpmScanFinding.build_job_id == job_id))
        if findings:
            for hit in findings:
                db.add(RpmScanFinding(
                    build_job_id=job_id,
                    attempt_id=attempt_id,
                    engine=hit["engine"],
                    rule=hit["rule"],
                    file_path=hit["file_path"],
                    evidence=hit.get("evidence") or "",
                ))
            job.scan_state = "quarantined"
            await log_audit(
                db,
                action="scan.quarantine",
                resource_type="build_job",
                resource_id=str(job_id),
                details=f"{len(findings)} hit(s) attempt_id={attempt_id}",
                user_agent="scanner",
            )
            await db.commit()
            return
        job.scan_state = "clean"
        await _enqueue(db, job_id, attempt_id)
        await db.commit()


async def retry_pending_scans(db) -> None:
    down = await asyncio.to_thread(ensure_clamd)
    if down:
        logger.warning("rpm scan waiting on clamd: %s", down)
        return
    live = (
        RpmbuildPlanStatus.PENDING,
        RpmbuildPlanStatus.RUNNING,
        RpmbuildPlanStatus.WAITING_SETTLE,
    )
    rows = (
        await db.execute(
            select(BuildJob.id)
            .outerjoin(RpmbuildPlan, RpmbuildPlan.id == BuildJob.rpmbuild_plan_id)
            .where(BuildJob.scan_state == "pending", BuildJob.status == BuildStatus.SUCCESS)
            .order_by(case((RpmbuildPlan.status.in_(live), 0), else_=1), BuildJob.id.desc())
            .limit(4)
        )
    ).scalars().all()
    for job_id in rows:
        attempt_id = await db.scalar(
            select(BuildAttempt.id)
            .where(BuildAttempt.build_job_id == job_id, BuildAttempt.status == BuildStatus.SUCCESS)
            .order_by(BuildAttempt.id.desc())
            .limit(1)
        )
        if attempt_id:
            schedule_rpm_scan(int(job_id), int(attempt_id))
