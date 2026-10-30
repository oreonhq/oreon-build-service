# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import re
import subprocess
import sys
from pathlib import Path

import requests

SPECS = Path(__file__).resolve().parent.parent / "fedora-rpms" / "oreon-11-rp1"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) orespec-fetch"}

fails = [l.strip() for l in Path.home().joinpath(".cache/orespec-src/retrofit-done.failed.txt").read_text().splitlines() if l.strip()]
done = set(Path.home().joinpath(".cache/orespec-src/retrofit-done.txt").read_text().split())

for name in fails:
    if name in done:
        print(f"{name}\talready-done")
        continue
    pdir = SPECS / name
    cands = list(pdir.glob("*.spec"))
    if len(cands) != 1:
        print(f"{name}\tno-spec ({len(cands)})")
        continue
    spec = cands[0]
    r = subprocess.run(["rpmspec", "-P", str(spec)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
        print(f"{name}\tparse\t{err[:120]}")
        continue
    srcs = re.findall(r"(?mi)^Source\d*:\s+(\S+)", r.stdout)
    status = "expand-ok-no-source"
    for url in srcs:
        if not url.startswith("http"):
            continue
        if "%{" in url or "%(" in url:
            print(f"{name}\tmacro-url\t{url[:100]}")
            status = "macro-url"
            break
        try:
            resp = requests.head(url, headers=UA, timeout=30, allow_redirects=True)
            if resp.status_code in (403, 404, 410):
                resp2 = requests.get(url, headers=UA, timeout=30, allow_redirects=True, stream=True)
                code = resp2.status_code
                resp2.close()
            else:
                code = resp.status_code
            if code in (403, 404, 410):
                print(f"{name}\tdead-url\t{code} {url[:100]}")
                status = "dead-url"
                break
            status = f"http-{code}"
        except requests.RequestException as e:
            print(f"{name}\tnet\t{type(e).__name__} {url[:80]}")
            status = "net"
            break
    if status in ("expand-ok-no-source",) or status.startswith("http-2"):
        print(f"{name}\t{status}")
