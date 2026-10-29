# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Bump a whole upstream release train in one go, like every Plasma package."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click

STACKS = {
    "plasma": {
        "index": "https://download.kde.org/stable/plasma/",
        "url": re.compile(r"https://download\.kde\.org/[^\s#]*/plasma/"),
        "keyserver": "hkps://keyserver.ubuntu.com",
        "keys": ["0AAC775BB6437A8D9AF7A3ACFE0784117FBCE11D"],
    },
}


def _get(url, timeout=120):
    request = urllib.request.Request(url, headers={"User-Agent": "orbscli-stack-bump"})
    return urllib.request.urlopen(request, timeout=timeout).read()


def _exists(url):
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "orbscli-stack-bump"})
    try:
        return urllib.request.urlopen(request, timeout=60).status == 200
    except Exception:
        return False


def latest_version(stack):
    html = _get(STACKS[stack]["index"]).decode("utf-8", "replace")
    found = {tuple(int(x) for x in m.split(".")) for m in re.findall(r'href="(\d+\.\d+\.\d+)/"', html)}
    if not found:
        raise click.ClickException("no versions listed at " + STACKS[stack]["index"])
    return ".".join(str(x) for x in max(found))


def _expanded_sources(spec):
    result = subprocess.run(["spectool", "-l", "-S", str(spec)], capture_output=True, text=True)
    out = {}
    for line in result.stdout.splitlines():
        match = re.match(r"Source(\d+):\s+(\S+)", line)
        if match:
            out[int(match[1])] = match[2]
    return out


def _version(text):
    match = re.search(r"(?m)^Version:\s+(\S+)\s*$", text)
    return match[1] if match else None


def find_members(root, stack):
    pattern = STACKS[stack]["url"]
    members = []
    for spec in sorted(Path(root).glob("*/*.spec")):
        text = spec.read_text(encoding="utf-8", errors="replace")
        if "download.kde.org" not in text or "plasma/" not in text:
            continue
        version = _version(text)
        if not version or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            continue
        sources = _expanded_sources(spec)
        url = sources.get(0, "")
        if pattern.match(url) and ("/plasma/" + version + "/") in url:
            members.append((spec, version, sources))
    return members


def _rewrite(text, old, new, hashes):
    text = re.sub(r"(?m)^(Version:\s+)" + re.escape(old) + r"\s*$", r"\g<1>" + new, text, count=1)
    text = re.sub(r"(?m)^(Release:\s*)\d+(%\{\?dist\}.*)$", r"\g<1>1\2", text, count=1)
    major, minor, bug = new.split(".")
    for name, value in (("maj_ver_kf6", major), ("min_ver_kf6", minor), ("bug_ver_kf6", bug)):
        text = re.sub(r"(?m)^(%global\s+" + name + r"\s+)\S+\s*$", r"\g<1>" + value, text)
    for number, digest in hashes.items():
        text = re.sub(r"(?m)^(%global\s+source" + str(number) + r"_hash\s+)\S+", r"\g<1>" + digest, text, count=1)
    return text


def _verify_signature(home, keyserver, data, sig, allowed):
    folder = Path(tempfile.mkdtemp(prefix="stackbump-"))
    try:
        (folder / "f").write_bytes(data)
        (folder / "f.sig").write_bytes(sig)
        env = dict(os.environ, GNUPGHOME=home)
        result = subprocess.run(["gpg", "--batch", "--keyserver", keyserver, "--auto-key-retrieve", "--verify",
                                 str(folder / "f.sig"), str(folder / "f")],
                                capture_output=True, text=True, env=env)
        if "BAD signature" in result.stderr:
            return "bad"
        if "Good signature" not in result.stderr:
            return "unknown"
        found = re.search(r"Primary key fingerprint: ([0-9A-F ]+)", result.stderr)
        if not found:
            return "unknown"
        fingerprint = re.sub(r"\s", "", found[1]).upper()
        return "good" if fingerprint in allowed else "unpinned"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _prepare(item, target, verify, home, keyserver, keys):
    spec, old, sources = item
    text = spec.read_text(encoding="utf-8")
    renamed = {number: url.replace(old, target) for number, url in sources.items()}
    url0 = renamed[0].split("#")[0]
    if not _exists(url0):
        return spec, old, None, "not in " + target + " upstream", None
    hashes = {}
    sig_state = None
    for number in sorted(renamed):
        if re.search(r"(?m)^%global\s+source" + str(number) + r"_hash\s", text) and "://" in renamed[number]:
            data = _get(renamed[number].split("#")[0])
            if hashlib.sha256(data).hexdigest() != hashlib.sha256(_get(renamed[number].split("#")[0])).hexdigest():
                return spec, old, None, "unstable download for Source" + str(number), None
            hashes[number] = hashlib.sha256(data).hexdigest()
            if number == 0 and verify:
                try:
                    sig = _get(url0 + ".sig", 60)
                except Exception:
                    sig = None
                sig_state = _verify_signature(home, keyserver, data, sig, keys) if sig else "nosig"
    if 0 not in hashes:
        return spec, old, None, "no source0_hash to refresh", None
    if sig_state == "bad":
        return spec, old, None, "bad signature", sig_state
    if sig_state == "unpinned":
        return spec, old, None, "signature from unpinned key", sig_state
    return spec, old, _rewrite(text, old, target, hashes), "", sig_state


def stack_bump(root, stack, target, dry_run, verify, jobs=8):
    if stack not in STACKS:
        raise click.ClickException("unknown stack, known: " + ", ".join(sorted(STACKS)))
    if not shutil.which("spectool"):
        raise click.ClickException("spectool is missing, install rpmdevtools")
    target = target or latest_version(stack)
    click.echo("%s target %s" % (stack, target), err=True)
    members = [item for item in find_members(root, stack) if item[1].split(".")[0] == target.split(".")[0]]
    click.echo("%d specs follow the %s train" % (len(members), stack), err=True)
    home = tempfile.mkdtemp(prefix="stackbump-gpg-")
    keyserver = STACKS[stack]["keyserver"]
    keys = STACKS[stack]["keys"]
    changed, skipped, warned, failed = 0, [], [], []
    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(lambda item: _prepare(item, target, verify, home, keyserver, keys), members))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    for spec, old, text, why, sig in results:
        name = spec.parent.name
        if text is None:
            (failed if why in ("bad signature", "unpinned key", "no source0_hash to refresh") else skipped).append("%s (%s)" % (name, why))
            continue
        if old == target:
            if text != spec.read_text(encoding="utf-8"):
                if not dry_run:
                    spec.write_text(text, encoding="utf-8")
                changed += 1
            continue
        if sig in ("unknown", "nosig"):
            warned.append("%s (signature %s)" % (name, "could not be checked" if sig == "unknown" else "missing"))
        if dry_run:
            click.echo("%s %s -> %s" % (name, old, target))
        else:
            spec.write_text(text, encoding="utf-8")
            try:
                import orespec as _orespec

                for note in _orespec.refresh_spec(spec):
                    if note:
                        click.echo("%s: %s" % (name, note), err=True)
            except Exception as exc:
                click.echo("%s: dep refresh failed: %s" % (name, exc), err=True)
                failed.append("%s (dep refresh)" % name)
            for stale in list(spec.parent.glob("*.tar.*")):
                if target not in stale.name and (old in stale.name or stale.name.endswith(".sig")):
                    stale.unlink()
        changed += 1
    click.echo("%d specs %s." % (changed, "would change" if dry_run else "changed"), err=True)
    for label, items in (("skipped", skipped), ("signature warnings", warned), ("failed", failed)):
        if items:
            click.echo("%s: %s" % (label, ", ".join(items)), err=True)
    if failed:
        raise click.ClickException("some specs were not bumped")
