from __future__ import annotations

import hashlib
import shlex
import threading
from pathlib import Path

import httpx


def run_iso_job(controller_url: str, worker_headers: dict[str, str], job: dict) -> None:
    from oreon_build.worker.main import _clone_repo, _mock_cmd_base, _run, _tail_lines

    iso_id = int(job["iso_build_id"])
    session = httpx.Client(timeout=60.0, headers=worker_headers)
    abort = threading.Event()
    logs: list[str] = []

    def note(text: str) -> None:
        if not text or not text.strip():
            return
        logs.append(text.strip())
        try:
            session.post(
                f"{controller_url}/api/worker/iso/{iso_id}/log",
                json={"log": "\n\n".join(logs)[-200000:]},
                timeout=30.0,
            )
        except Exception:
            pass

    def cancelled() -> bool:
        try:
            r = session.get(f"{controller_url}/api/worker/iso/{iso_id}/cancel-check", timeout=20.0)
            return r.status_code == 200 and bool(r.json().get("cancel_requested"))
        except Exception:
            return False

    def finish(status: str, error: str | None = None, sha256: str | None = None, size_bytes: int | None = None) -> None:
        body = {
            "status": status,
            "log": "\n\n".join(logs)[-1_500_000:],
            "error_message": error,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "r2_key": job.get("iso_name") if status == "success" else None,
        }
        try:
            session.post(f"{controller_url}/api/worker/iso/{iso_id}/result", json=body, timeout=120.0)
        except Exception:
            pass
        session.close()

    try:
        note(f"starting iso build {job.get('iso_name')} on {job.get('mock_config')}")
        if cancelled():
            finish("cancelled", "Cancelled before start")
            return
        repo = str(job.get("kickstart_repo") or "").strip()
        branch = str(job.get("kickstart_branch") or "main").strip() or "main"
        folder = str(job["release_folder"])
        ks_name = str(job["kickstart"])
        iso_name = str(job["iso_name"])
        mock_config = str(job["mock_config"])
        work = Path(job.get("work_dir") or f"/tmp/oreon-iso-{iso_id}")
        if work.exists():
            import shutil

            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        git_dir = work / "kickstarts"
        ok, git_out = _clone_repo(repo, branch, git_dir)
        note("## git\n" + git_out)
        if not ok:
            finish("failed", "kickstart git clone failed")
            return
        root = git_dir / folder
        ks_path = root / ks_name
        templates = root / "lorax-templates"
        if not ks_path.is_file():
            finish("failed", f"kickstart missing: {folder}/{ks_name}")
            return
        if not templates.is_dir():
            finish("failed", f"lorax-templates missing in {folder}")
            return
        base = _mock_cmd_base(mock_config, f"iso-{iso_id}", [], None, attempt_id=iso_id)
        rc, out = _run(base + ["--init"], timeout_s=3600)
        note("## mock init\n" + out)
        if rc != 0:
            finish("failed", "mock init failed")
            return
        install = ["lorax", "lorax-lmc-novirt", "pykickstart", "dbus"]
        if job.get("macboot"):
            install.append("hfsplus-tools")
        rc, out = _run(base + ["--install"] + install, timeout_s=3600)
        note("## mock install\n" + out)
        if rc != 0:
            finish("failed", "mock install of lorax failed")
            return
        rc, out = _run(base + ["--copyin", str(ks_path), "/tmp/oreon.ks"], timeout_s=600)
        note("## copyin ks\n" + out)
        if rc != 0:
            finish("failed", "copyin kickstart failed")
            return
        rc, out = _run(base + ["--copyin", str(templates), "/tmp/lorax-templates"], timeout_s=600)
        note("## copyin templates\n" + out)
        if rc != 0:
            finish("failed", "copyin lorax-templates failed")
            return
        if cancelled():
            finish("cancelled", "Cancelled")
            return
        lmc = [
            "livemedia-creator",
            "--ks",
            "/tmp/oreon.ks",
            "--no-virt",
            "--resultdir",
            "/var/lmc",
            "--project",
            str(job.get("project") or "Oreon"),
            "--make-iso",
            "--volid",
            str(job["volid"]),
            "--iso-only",
            "--iso-name",
            iso_name,
            "--releasever",
            str(job["releasever"]),
            "--lorax-templates",
            "/tmp/lorax-templates",
        ]
        if job.get("macboot"):
            lmc.append("--macboot")
        else:
            lmc.append("--nomacboot")
        inner = " ".join(shlex.quote(x) for x in lmc)
        script = (
            "mkdir -p /run/dbus && "
            "if [ ! -S /run/dbus/system_bus_socket ]; then dbus-daemon --system --fork; fi && "
            + inner
        )
        rc, out = _run(base + ["--chroot", "--", "bash", "-c", script], timeout_s=21600)
        note("## livemedia-creator\n" + _tail_lines(out, 4000))
        if rc != 0:
            extra = []
            for src in (
                "/var/log/anaconda/anaconda.log",
                "/var/log/anaconda/packaging.log",
            ):
                dest = work / Path(src).name
                _run(base + ["--copyout", src, str(dest)], timeout_s=120)
                if dest.is_file():
                    extra.append("## " + src + "\n" + dest.read_text(errors="replace")[-60000:])
            if extra:
                note("\n".join(extra))
            finish("failed", "livemedia-creator failed")
            return
        dest = work / iso_name
        rc, out = _run(base + ["--copyout", f"/var/lmc/{iso_name}", str(dest)], timeout_s=3600)
        note("## copyout\n" + out)
        if rc != 0 or not dest.is_file():
            finish("failed", "ISO was not produced")
            return
        digest = hashlib.sha256()
        size = 0
        with dest.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        sha = digest.hexdigest()
        presign = session.post(f"{controller_url}/api/worker/iso/{iso_id}/presign", timeout=60.0)
        if presign.status_code != 200:
            note(presign.text)
            finish("failed", "could not get ISO upload url", sha256=sha, size_bytes=size)
            return
        upload_url = presign.json().get("url")
        with dest.open("rb") as fh:
            put = httpx.put(
                upload_url,
                content=fh,
                headers={"Content-Type": "application/octet-stream", "If-None-Match": "*"},
                timeout=httpx.Timeout(60.0, read=7200.0, write=7200.0),
            )
        note(f"## upload\nstatus={put.status_code}")
        if put.status_code not in (200, 201, 204):
            finish("failed", f"ISO upload failed ({put.status_code})", sha256=sha, size_bytes=size)
            return
        try:
            _run(base + ["--scrub", "all"], timeout_s=1800)
        except Exception:
            pass
        finish("success", sha256=sha, size_bytes=size)
    except Exception as exc:
        note(str(exc))
        finish("failed", str(exc))
