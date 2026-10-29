# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read DNF5 repoclosure failures without inventing package ownership."""
from __future__ import annotations

from pathlib import Path
import json
import re
import subprocess
import tempfile


class ClosureError(RuntimeError):
    pass


def _dnf_command(cli, urls, arch, release, directory):
    root, repos, cache, logs = (directory / part for part in ('root', 'repos', 'cache', 'logs'))
    for path in (root, repos, cache, logs):
        path.mkdir(parents=True, exist_ok=True)
    rel = re.search(r'oreon-(\d+)', release)
    command = cli._dnf_repoquery_cmd(root, repos, cache, rel[1] if rel else release,
                                     arch, urls[0], 'closure0')
    command.append('--setopt=logdir=' + str(logs))
    for index, url in enumerate(urls[1:], 1):
        command.extend(('--repofrompath=closure%s,%s' % (index, url.rstrip('/')),
                        '--enablerepo=closure%s' % index))
    return command


def _binary_sources(command):
    query = '%{name}\t%{epoch}\t%{version}\t%{release}\t%{arch}\t%{source_name}\t%{repoid}\n'
    result = subprocess.run([*command, 'repoquery', '--available', '--qf', query],
                            capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ClosureError('Repository package lookup failed: ' + (result.stderr or result.stdout).strip()[:500])
    sources = {}
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) != 7 or not all(fields):
            raise ClosureError('Unexpected repository package metadata: ' + line[:150])
        name, epoch, version, release, arch, source, repo = fields
        if epoch in ('0', '(none)'):
            nevra = f'{name}-{version}-{release}.{arch}'
        else:
            nevra = f'{name}-{epoch}:{version}-{release}.{arch}'
        key = repo, nevra
        if key in sources and sources[key] != source:
            raise ClosureError('Conflicting source packages for ' + nevra)
        sources[key] = source
    return sources


def _failures(output, expected_repos):
    try:
        rows = json.loads(output)
    except ValueError as exc:
        raise ClosureError('repoclosure returned invalid JSON') from exc
    if not isinstance(rows, list):
        raise ClosureError('repoclosure returned an unexpected result')
    failures = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('package'), str) or not isinstance(row.get('repo'), str):
            raise ClosureError('repoclosure returned an invalid package record')
        repo, package = row['repo'], row['package']
        requirements = row.get('unresolved_dependencies')
        if repo not in expected_repos or not isinstance(requirements, list) or not requirements or not all(
                isinstance(item, str) and item.strip() for item in requirements):
            raise ClosureError('repoclosure returned an invalid dependency record for ' + package)
        failures.setdefault((repo, package), set()).update(requirements)
    return failures


def discover(cli, urls_by_arch, release, side_only):
    """Return source requirements by architecture from exact broken RPMs."""
    result = {}
    with tempfile.TemporaryDirectory(prefix='orbscli-repoclosure-') as folder:
        for arch, urls in urls_by_arch.items():
            if not urls or (side_only and len(urls) < 2):
                raise ClosureError('Required repository is unavailable for ' + arch)
            command = _dnf_command(cli, urls, arch, release, Path(folder) / arch)
            checked = ['closure%s' % index for index in
                       (range(1, len(urls)) if side_only else range(len(urls)))]
            check = ['--check=' + ','.join(checked)] if side_only else []
            run = subprocess.run([*command, 'repoclosure', '--json', *check],
                                 capture_output=True, text=True, timeout=600)
            if run.returncode not in (0, 1):
                raise ClosureError('repoclosure failed for ' + arch + ': ' + (run.stderr or run.stdout).strip()[:500])
            failures = _failures(run.stdout, set(checked))
            if run.returncode == 1 and not failures:
                raise ClosureError('repoclosure failed without usable dependency results for ' + arch)
            if not failures:
                result[arch] = {}
                continue
            sources = _binary_sources(command)
            by_source = {}
            for key, requirements in failures.items():
                source = sources.get(key)
                if not source:
                    raise ClosureError('Cannot identify the source package for ' + key[1] + ' in ' + key[0])
                by_source.setdefault(source, set()).update(requirements)
            result[arch] = by_source
    return result
