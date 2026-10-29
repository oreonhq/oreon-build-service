# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deterministic, preview-first repair plans from RPM build logs."""
from __future__ import annotations

import difflib
import hashlib
import re
import tarfile
import time
from pathlib import Path
from typing import Any


def missing_dependencies(log: str) -> list[str]:
    found = re.findall(r"(?:No matching package to install:|No match for argument:)\s*['\"]?([^'\"\n]+?)(?:['\"]|$)", log, re.M)
    for line in log.splitlines():
        match = re.search(r'nothing provides (?:requested )?(.+)', line)
        if match:
            found.append(re.split(r' needed by ', match[1], maxsplit=1)[0].strip())
    return sorted(set(found))


def missing_artifacts(log):
    """Extract names supplied by the failed tool, without guessing RPM names."""
    artifacts = set()
    features = set(re.findall(r'ERROR: ([\w-]+) requested but not found', log))
    if features:
        # Configure logs contain deliberately failing portability probes. Only
        # inspect the failed requested feature's recorded check.
        for feature in features:
            for header in re.findall(r'check_cpp_condition ' + re.escape(feature) + r' (\S+)', log):
                artifacts.add(('header', header))
        return sorted(artifacts)
    for module in re.findall(r"(?:ModuleNotFoundError|ImportError): No module named ['\"]([^'\"]+)", log):
        artifacts.add(('python', module))
    for module in re.findall(r'Could not import extension ([\w.]+)', log):
        artifacts.add(('python', module))
    for header in re.findall(r"fatal error: [‘'\"]?([A-Za-z0-9_./+-]+)[’'\"]?: (?:No such file or directory|file not found)", log):
        artifacts.add(('header', header))
    for tool in re.findall(r"(?:^|: )([A-Za-z0-9_+./-]+): (?:command )?not found", log, re.M):
        artifacts.add(('executable', tool))
    for tool in re.findall(r"Could not find the compiler specified in the environment variable \w+:\s*\n([^\n]+)", log):
        artifacts.add(('executable', tool.strip().rstrip('.')))
    for tool in re.findall(r"(?:Exception: )?([A-Za-z0-9_+-]+) must be installed to build from source", log):
        artifacts.add(('executable', tool.lower()))
    for tool in re.findall(r"FileNotFoundError:.*No such file or directory: ['\"]([^'\"]+)", log):
        if '/' not in tool or tool.startswith('/usr/bin/'):
            artifacts.add(('executable', tool))
    return sorted(artifacts)


def build_requirements(log: str, resolve_artifact=None, unresolved=None) -> list[tuple[str, str]]:
    out = []
    for module in re.findall(r"Can't locate ([A-Za-z0-9_/]+)\.pm in @INC", log):
        out.append(('perl(' + module.replace('/', '::') + ')', 'Perl could not load ' + module))
    for name in re.findall(r"(?:Run-time|Build-time) dependency ([A-Za-z0-9_.+-]+) found: NO \(tried pkgconfig", log):
        out.append(('pkgconfig(' + name + ')', 'Missing pkg-config dependency ' + name))
    for name in re.findall(r"Package '([A-Za-z0-9_.+-]+)', required by .*?, not found", log):
        out.append(('pkgconfig(' + name + ')', 'pkg-config could not find ' + name))
    for kind, name in missing_artifacts(log):
        if resolve_artifact:
            dependency = resolve_artifact(kind, name)
        elif kind == 'executable':
            dependency = name if name.startswith('/') else '/usr/bin/' + name
        else:
            dependency = resolve_artifact(kind, name) if resolve_artifact else None
        if dependency:
            epoch = '0'
            if isinstance(dependency, tuple):
                dependency, epoch = dependency
            if kind == 'header':
                checks = sorted(set(re.findall(r'check_cpp_condition \w+ ' + re.escape(name) + r' (.+)', log)), reverse=True)
                for check in checks:
                    bound = re.search(r'>=\s*(0x[0-9a-fA-F]+)', check)
                    fields = re.findall(r'\b\w*VERSION_\w+\s*<<\s*(\d+)', check)
                    if bound and len(fields) >= 3:
                        value = int(bound[1], 16)
                        shifts = sorted({int(shift) for shift in fields}, reverse=True)
                        if shifts[-1] != 0:
                            shifts.append(0)
                        parts = [(value >> shift) & ((1 << ((shifts[i-1] - shift) if i else 16)) - 1)
                                 for i, shift in enumerate(shifts)]
                        while len(parts) > 2 and parts[-1] == 0:
                            parts.pop()
                        dependency += ' >= ' + (epoch + ':' if epoch != '0' else '') + '.'.join(map(str, parts))
                        break
            evidence = ('Python could not import ' if kind == 'python' else 'Missing ' + kind + ' ') + name
            out.append((dependency, evidence))
        elif unresolved is not None:
            unresolved.append('No unique repository provider for ' + kind + ' ' + name)
    return sorted(set(out))


def failure_diagnostics(log):
    """Retain concrete error lines instead of silently dropping unknown failures."""
    hits = []
    pattern = re.compile(r"fatal error:|(?<![-\w])error:|ERROR:.*(?:dependency|Subproject|wrap)|"
                         r"Source[0-9]+ hash mismatch|checksum doesn.t match|Hunk .*FAILED|FAILED .*|"
                         r"cp: cannot stat|sed: can't read|"
                         r"(?:^|:\s)cd: .*: No such file or directory|"
                         r"File not found:|Directory not found:|missing local files:|"
                         r"(?:ModuleNotFoundError|ImportError|FileNotFoundError|ValueError|Exception):|"
                         r"invalid option|No file to patch|hunk ignored|UNEXPECTED FAIL:|"
                         r"(?:^|/)t/.*\(Wstat:|Bad plan\.|Tests out of sequence|"
                         r"Could NOT find|undefined reference|Curl error|Connection refused|ERROR: .*requested but not found", re.I)
    for raw in log.splitlines():
        line = raw.strip()
        if not pattern.search(line):
            continue
        if any(x in line for x in ('CFLAGS=', 'CXXFLAGS=', 'FFLAGS=', 'FCFLAGS=',
                                  'ERROR: Exception(', 'ERROR: Command failed', 'Bad exit status',
                                  'warning: build failed')):
            continue
        line = re.sub(r'/builddir/build/BUILD/[^/]+/', '', line)
        if line and line not in hits:
            hits.append(line)
    # Configure probes routinely fail. the explicit configure failure wins.
    configure = [x for x in hits if re.search(r'ERROR: .*requested but not found', x)]
    if configure:
        return configure
    if len(hits) > 10:
        return hits[:5] + [f'... {len(hits) - 10} repeated or additional error lines omitted ...'] + hits[-5:]
    return hits


def add_build_requirement(spec: str, dependency: str) -> str:
    if any(re.search(r'(?<!\S)' + re.escape(dependency) + r'(?=\s|$)', line.partition(':')[2].split('#', 1)[0])
           for line in spec.splitlines() if line.startswith('BuildRequires:')):
        return spec
    # Insert at the start of the preamble, outside any condition or subpackage.
    match = re.search(r'(?m)^Name\s*:', spec)
    if not match:
        return spec
    return spec[:match.start()] + 'BuildRequires:  ' + dependency + '\n' + spec[match.start():]


def exact_unpackaged(spec: str, log: str, o: Any, expanded_spec: str | None = None) -> tuple[str, list[str], list[str]]:
    actions, manual = [], []
    spans = o._files_section_spans(spec)
    # A single package has an unambiguous owner. Subpackages require review.
    paths = [p for p in o._fastfix_unpackaged_files(log)
             if not p.startswith(('/usr/lib/debug/', '/usr/lib/.build-id/', '/usr/src/debug/'))]
    for path in paths:
        if any(c in path for c in '%*?[]'):
            manual.append('Untrusted or glob-like installed path requires review: ' + path)
            continue
        entry = o._files_macroize_abs(path, spec)
        if o._files_entry_already_present(spec, entry):
            continue
        owner = 0 if len(spans) == 1 else None
        expanded = expanded_spec or spec
        expanded_spans = o._files_section_spans(expanded)
        if owner is None and len(expanded_spans) == len(spans):
            import os
            def role(value):
                if re.search(r'\.so\.\d', value):
                    return 'runtime-library'
                if re.search(r'\.(?:so|a)$|/pkgconfig/|/cmake/|/include/|%\{_includedir\}', value):
                    return 'development'
                if '/bin/' in value or '%{_bindir}' in value:
                    return 'executable'
                return 'data'
            target_role = role(path)
            scores = []
            for _, start, end in expanded_spans:
                anchors = re.findall(r'(?m)^\s*(?:%\w+\s+)*(/[^\s]+|%\{[^}]+\}[^\s]*)', expanded[start:end])
                same_role = [anchor for anchor in anchors if role(anchor) == target_role]
                # Compare basenames as well as directories. Runtime SONAMEs
                # and linker files belong to distinct packages even in libdir.
                base = Path(path).name
                prefix = max((len(os.path.commonprefix([base, Path(anchor).name]))
                              for anchor in same_role), default=0)
                depth = max((len(Path(os.path.commonpath([path, anchor])).parts)
                             for anchor in anchors if anchor.startswith('/')), default=0)
                scores.append((bool(same_role), prefix, depth))
            best = max(scores, default=(False, 0, 0))
            if scores.count(best) == 1 and (best[0] or best[2] >= 4):
                owner = scores.index(best)
        if owner is None:
            # Files outside an existing subpackage split belong to the main
            # package. Subpackages own matched roles and path families above.
            source_name = re.search(r'(?m)^Name:[ \t]*(\S+)', spec)
            main_names = {'%{name}', source_name[1] if source_name else ''}
            main = []
            for i, (header, _, _) in enumerate(o._files_section_spans(spec)):
                flags = header[0].split()[1:]
                if '-n' in flags:
                    index = flags.index('-n')
                    is_main = index + 1 < len(flags) and flags[index + 1] in main_names
                else:
                    positional = []
                    skip = False
                    for flag in flags:
                        if skip:
                            skip = False
                        elif flag in ('-f', '-l'):
                            skip = True
                        elif not flag.startswith('-'):
                            positional.append(flag)
                    is_main = not positional
                if is_main:
                    main.append(i)
            if len(main) == 1:
                owner = main[0]
            else:
                spec = spec.rstrip() + '\n\n%files\n'
                owner = len(o._files_section_spans(spec)) - 1
        end = o._files_section_spans(spec)[owner][2]
        spec = spec[:end].rstrip('\n') + '\n' + entry + ('\n\n' if spec[end:] else '\n') + spec[end:]
        actions.append('Package installed file ' + entry)
    return spec, actions, manual


def plan_package(package: str, spec_path: Path, logs: list[Path], o: Any, source_archive: Path | None = None, registry_checksum: str | None = None, patch_payloads: dict[str, str] | None = None, repo_checksum_verified: bool = False, resolve_artifact=None, expanded_spec=None) -> dict[str, Any]:
    original = spec_path.read_text()
    spec = original
    log = '\n'.join(p.read_text(errors='replace') for p in logs)
    actions = []
    source_manual = []
    spec, phase_actions = phase_repairs(spec, log)
    actions.extend(phase_actions)
    version = o._spec_literal_version(original)
    srpms = re.findall(r'/(?:SRPMS|srpm_result)/([^/\s]+)\.src\.rpm', log)
    current = version[0] if version else None
    stale = bool(current and srpms and any(name.startswith(package + '-') and
                 not name.startswith(package + '-' + current + '-') for name in srpms))
    if stale:
        return {
            'package': package, 'spec': str(spec_path),
            'before_sha256': hashlib.sha256(original.encode()).hexdigest(),
            'after_sha256': hashlib.sha256(original.encode()).hexdigest(),
            'logs': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in logs],
            'actions': [], 'missing_dependencies': missing_dependencies(log),
            'manual': ['Logs describe a different source version. Retry the current spec before applying repairs'],
            'diff': '', 'updated_spec': original, 'verification': 'Stale logs',
        }
    source_actions = []
    guard = re.search(r'(?m)^%global\s+source0_hash\s+([0-9a-f]{64})\s*$', spec)
    checksum_repaired = bool(source_archive is not None and guard
                             and o._fastfix_source_hash_failure(log)
                             and tarfile.is_tarfile(source_archive)
                             and hashlib.sha256(source_archive.read_bytes()).hexdigest() == guard[1])
    if (source_archive is not None and registry_checksum and re.fullmatch(r'[0-9a-f]{64}', registry_checksum)
            and o._fastfix_source_hash_failure(log)
            and tarfile.is_tarfile(source_archive)
            and hashlib.sha256(source_archive.read_bytes()).hexdigest() == registry_checksum):
        revised = re.sub(r'(?m)^(%global\s+source0_hash\s+)[0-9a-f]{64}\s*$',
                         lambda m: m[1] + registry_checksum, spec, count=1)
        checksum_repaired = True
        if revised != spec:
            spec = revised
            checksum_repaired = True
            actions.append({'action': 'Correct the crate checksum using the published crates.io checksum',
                            'evidence': 'Registry checksum agrees with downloaded source bytes'})
    patch_review = []
    if source_archive is not None:
        spec, source_actions, source_manual = verified_source_repairs(spec, log, source_archive)
        actions.extend(source_actions)
        spec, redundant_actions = redundant_patch_repairs(spec, log, source_archive, patch_payloads or {}, patch_review)
        actions.extend(redundant_actions)
    patch_changes = []
    build_already = False
    if source_archive is not None:
        patch_changes, patch_actions = macro_patch_repairs(spec_path, log, source_archive)
        actions.extend(patch_actions)
    if source_archive is not None:
        spec, build_actions, build_changes, build_already = verified_build_repairs(spec_path, spec, log, source_archive)
        actions.extend(build_actions)
        patch_changes.extend(build_changes)
    dependency_review = []
    recognised = build_requirements(log, resolve_artifact, dependency_review)
    for dependency, evidence in recognised:
        updated = add_build_requirement(spec, dependency)
        if updated != spec:
            actions.append({'action': 'Add BuildRequires: ' + dependency, 'evidence': evidence})
            spec = updated
        if evidence.startswith('Python could not import ') and 'Failed to import:' in log:
            packages = list(re.finditer(r'(?m)^%package\s+-n\s+python\S+[^\n]*\n', spec))
            if len(packages) == 1:
                start = packages[0].end()
                end = re.search(r'(?m)^%', spec[start:])
                header = spec[start:start + end.start()] if end else spec[start:]
                if not re.search(r'(?m)^Requires:\s*' + re.escape(dependency) + r'(?:\s|$)', header):
                    spec = spec[:start] + 'Requires:       ' + dependency + '\n' + spec[start:]
                    actions.append({'action': 'Add a runtime dependency: ' + dependency,
                                    'evidence': 'Packaged module import failed in the import check'})
    spec, packaged, manual = exact_unpackaged(spec, log, o, expanded_spec)
    actions.extend({'action': a, 'evidence': 'RPM installed-but-unpackaged list'} for a in packaged)
    manual.extend(source_manual)
    manual.extend(patch_review)
    manual.extend(dependency_review)
    dependencies = missing_dependencies(log)
    installed_paths = [p for p in o._fastfix_unpackaged_files(log)
                       if not p.startswith(('/usr/lib/debug/', '/usr/lib/.build-id/', '/usr/src/debug/'))]
    source_verified = bool(source_archive is not None and not source_manual and guard
                           and hashlib.sha256(source_archive.read_bytes()).hexdigest() == guard[1])
    doc_already = False
    if source_verified and 'cp: cannot stat' in log:
        with tarfile.open(source_archive) as tar:
            names = {m.name.split('/', 1)[-1] for m in tar.getmembers() if m.isfile()}
        missing_docs = {Path(path).name for path in re.findall(r"cp: cannot stat '([^']+)': No such file or directory", log)}
        doc_tokens = {token for line in spec.splitlines() if line.startswith(('%doc ', '%license '))
                      for token in line.split('#', 1)[0].split()[1:]}
        doc_already = bool(missing_docs) and all(old not in names and old not in doc_tokens
            and (not resource_candidates(old, names)
                 or any(candidate in doc_tokens for candidate in resource_candidates(old, names))) for old in missing_docs)
        missing_paths = re.findall(r'(?m)File not found:\s*(.+)$', log)
        doc_already = doc_already and all(('/usr/share/doc/' in path or '/usr/share/licenses/' in path) and Path(path).name in missing_docs for path in missing_paths)
    manifest_already = False
    if source_verified:
        with tarfile.open(source_archive) as tar:
            manifests = {m.name.split('/', 1)[-1]: tar.extractfile(m).read()
                         for m in tar if m.isfile() and m.size < 2_000_000 and m.name.endswith('/known_good.json')}
        required_revisions = manifest_api_requirements(spec, log, manifests)
        manifest_already = bool(required_revisions) and all(
            re.search(r'(?m)^BuildRequires:[ \t]+' + re.escape(requirement) + r'[ \t]*$', spec)
            for requirement in required_revisions)
    patch_verified = bool(source_archive is not None and patch_payloads and not patch_review and not source_manual)
    phase_already = (
        manifest_already
        or (incomplete_pytest_output(log) and re.search(r'(?m)^%pytest -q -r fE --tb=short', spec))
        or (o._fastfix_patch_failure(log) and patch_verified)
        or ('Extracting debug info from 0 files' in log and 'debugsourcefiles.list' in log
         and re.search(r'(?m)^BuildArch:\s+noarch\s*$', spec))
        or ('Unknown tag: BuildSystem:' in log and '%pyproject_wheel' in spec
            and '%global orbs_pyproject_files_options' in spec and 'BuildSystem:' not in spec)
        or (source_verified and 'build backend cannot provide build metadata' in log
            and re.search(r'(?m)^%pyproject_buildrequires\s+-p(?:\s|$)', spec))
        or (source_verified and "ipsec algparse: invalid option -- 't'" in log
            and '/ipsec/algparse --tp' in spec and '/ipsec/algparse --ta' in spec)
        or (source_verified and 'No file to patch.' in log and not re.search(r'(?m)^Patch\d*:', spec))
        or (source_verified and re.search(r'error:.*(?:not a member of|no member named).*spv', log)
            and 'BuildRequires:  spirv-headers-commit(' in spec))
    already_applied = not actions and bool(recognised or installed_paths or build_already or checksum_repaired or phase_already or doc_already or repo_checksum_verified) and not dependencies and not manual
    if not actions and not dependencies and not already_applied and not manual:
        if 'ERROR: Command failed:' in log and not failure_diagnostics(log):
            manual.append('No failure detail was recognized in the downloaded worker log. Inspect its full output.')
        else:
            manual.append('No automatic repair is available for this failure.')
    # Keep every unhandled phase visible, even if one rule matched.
    if 'checksum doesn\'t match' in log and not repo_checksum_verified:
        manual.append('Repository RPM checksum differs from its metadata. Repair or refresh the repository before retrying.')
    if ('File not found:' in log or 'Directory not found:' in log) and not source_actions and not doc_already:
        manual.append('Missing packaged paths need source or install-manifest verification')
    patch_checked = source_archive is not None and bool(patch_payloads)
    if (o._fastfix_patch_failure(log) and not patch_checked) or (o._fastfix_source_hash_failure(log) and not checksum_repaired):
        manual.append('Patch or source checksum failure needs review. The checksum guard was kept.')
    return {
        'package': package, 'spec': str(spec_path),
        'before_sha256': hashlib.sha256(original.encode()).hexdigest(),
        'after_sha256': hashlib.sha256(spec.encode()).hexdigest(),
        'logs': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in logs],
        'actions': actions, 'recognised_failures': [{'dependency': d, 'evidence': e} for d, e in recognised],
        'missing_dependencies': dependencies, 'manual': manual,
        'diagnostics': [line for line in failure_diagnostics(log)
                        if not (checksum_repaired and 'Source0 hash mismatch' in line)
                        and not (repo_checksum_verified and "checksum doesn't match" in line)],
        'repo_checksum_verified': repo_checksum_verified,
        'source_requirements': sorted({dependency for dependency, _ in recognised if ' >= ' in dependency} |
                                     set(re.findall(r'(?m)^BuildRequires:\s+([A-Za-z0-9+_.-]+-commit\([0-9a-f]{40}\))\s*$', spec)
                                         if re.search(r'error:.*(?:not a member of|no member named)', log) else [])),
        'already_applied': already_applied and not manual,
        'diff': ''.join(difflib.unified_diff(original.splitlines(True), spec.splitlines(True),
                                          fromfile=str(spec_path), tofile=str(spec_path))),
        'updated_spec': spec, 'patch_changes': patch_changes,
        'verification': 'Not rebuilt. retry after publishing reviewed specs and prerequisites',
    }


def apply_plan(plan: dict[str, Any]) -> None:
    path = Path(plan['spec'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != plan['before_sha256']:
        raise ValueError('Spec changed after planning: ' + str(path))
    for patch in plan.get('patch_changes', []):
        if (hashlib.sha256(Path(patch['path']).read_bytes()).hexdigest() if Path(patch['path']).exists() else None) != patch['before_sha256']:
            raise ValueError('Patch changed after planning: ' + patch['path'])
    if plan['before_sha256'] != plan['after_sha256']:
        path.write_text(plan['updated_spec'])
    for patch in plan.get('patch_changes', []):
        Path(patch['path']).write_text(patch['updated'])


def incomplete_pytest_output(log):
    """Detect an interrupted test-output section before a worker failure."""
    if 'ERROR: Command failed:' not in log:
        return False
    for section in re.split(r'(?m)^##[^\n]*\n', log):
        if not re.search(r'Executing\(%check\)|test session starts', section):
            continue
        if not re.search(r'(?m)^.*\bPASSED\s+\S+::', section):
            continue
        if re.search(r'(?m)^=+.*(?:passed|failed|error|interrupted).*\bin\s+\d', section):
            continue
        last = section.rstrip().splitlines()[-1]
        if re.match(r'(?:\[gw\d+\].*|tests?/).*', last):
            return True
    return False


def phase_repairs(spec, log):
    """Repairs established by the build output without downloading sources."""
    actions = []
    if incomplete_pytest_output(log):
        def compact(match):
            command = match[0]
            revised = re.sub(r'(?<!\S)(?:--verbose|-v+)(?!\S)[ \t]*', '', command)
            if revised == command:
                return command
            return revised.replace('%pytest', '%pytest -q -r fE --tb=short', 1)
        revised = re.sub(r'(?m)^%pytest[^\n]*', compact, spec)
        if revised != spec:
            spec = revised
            actions.append({'action': 'Keep pytest output compact while preserving failure details',
                            'evidence': 'Verbose test output ends mid-test before the worker failure, without a pytest result'})
    if ('Empty %files file' in log and '/debugsourcefiles.list' in log
            and 'Extracting debug info from 0 files' in log
            and re.search(r'filename=\S+-py[23][\w.]*-none-any\.whl\b', log)):
        if not re.search(r'(?m)^BuildArch:', spec):
            spec = re.sub(r'(?m)^(Name\s*:)', 'BuildArch:      noarch\n\\1', spec, count=1)
            actions.append({'action': 'Build the pure Python wheel as noarch',
                            'evidence': 'Architecture independent wheel and zero ELF debug files'})
    if ('Unknown tag: BuildSystem:' in log and re.search(r'(?m)^BuildSystem:\s+pyproject\s*$', spec)
            and '%generate_buildrequires\n' not in spec and '%install\n' not in spec):
        options = re.findall(r'(?m)^BuildOption\(([^)]+)\):\s*(.*)$', spec)
        if all(section in ('generate_buildrequires', 'install') for section, _ in options):
            spec = re.sub(r'(?m)^BuildSystem:\s+pyproject\s*\n', 'BuildRequires:  python3-devel\n', spec)
            # Preserve conditional options at their original position as macros.
            spec = re.sub(r'(?m)^BuildOption\(generate_buildrequires\):\s*(.*)$', r'%global orbs_pyproject_requires_options \1', spec)
            spec = re.sub(r'(?m)^BuildOption\(install\):\s*(.*)$', r'%global orbs_pyproject_files_options \1', spec)
            spec = spec.replace('%build -a\n', '%build\n%pyproject_wheel\n')
            spec = spec.replace('%conf\n', '')
            pos = re.search(r'(?m)^%check\b|^%files\b', spec)
            if pos and '%generate_buildrequires\n' not in spec and '%install\n' not in spec:
                spec = spec[:pos.start()] + ('%generate_buildrequires\n%pyproject_buildrequires %{?orbs_pyproject_requires_options}\n\n'
                       '%install\n%pyproject_install\n%pyproject_save_files %{?orbs_pyproject_files_options}\n\n') + spec[pos.start():]
                actions.append({'action': 'Expand pyproject BuildSystem into explicit RPM sections',
                                'evidence': 'The source preparation RPM rejects BuildSystem'})
    if 'Unknown tag: BuildSystem:' in log and ('%global orbs_pyproject_files_options' in spec
                                             or any('Expand pyproject BuildSystem' in action['action'] for action in actions)):
        boundary = r'(?=^%(?:prep|conf|build|install|generate_buildrequires|check|files|changelog|package|description)\b|\Z)'
        for phase, default in [('build', '%pyproject_wheel'), ('generate_buildrequires', '%pyproject_buildrequires %{?orbs_pyproject_requires_options}')]:
            pattern = re.compile(r'^%' + phase + r'[^\n]*\n(.*?)' + boundary, re.M | re.S)
            sections = list(pattern.finditer(spec))
            bodies = [section[1].rstrip() for section in sections]
            if not any(default.split()[0] in body for body in bodies):
                bodies.append(default)
            replacement = '%' + phase + '\n' + '\n'.join(bodies) + '\n\n'
            revised = spec
            if sections:
                for section in reversed(sections[1:]):
                    revised = revised[:section.start()] + revised[section.end():]
                first = sections[0]
                revised = revised[:first.start()] + replacement + revised[first.end():]
            else:
                pos = re.search(r'(?m)^%install\b|^%check\b|^%files\b', revised)
                if pos:
                    revised = revised[:pos.start()] + replacement + revised[pos.start():]
            if revised != spec:
                spec = revised
                actions.append({'action': 'Preserve all ' + phase + ' steps when expanding BuildSystem',
                                'evidence': 'Prepend or implicit steps need an explicit default command'})
    return spec, actions


def patch_postimage_present(parts, root, strip=1):
    """Prove every changed block, allowing formatting drift outside literals."""
    def tokens(value):
        return re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_][A-Za-z_0-9]*|\+\+|--|->|::|&&|\|\||<<|>>|==|!=|<=|>=|[+*/%&|^=-]=|[^\s]', value)
    def contains(haystack, needle):
        return bool(needle) and any(haystack[i:i + len(needle)] == needle
                                  for i in range(len(haystack) - len(needle) + 1))
    changed = False
    for part in parts:
        match = re.search(r'(?m)^\+\+\+ ([^\s]+)', part)
        if not match:
            return False
        relative = '/'.join(match[1].split('/')[strip:])
        path = root / relative
        if not path.is_file():
            return False
        source = tokens(path.read_text(errors='replace'))
        hunks = re.split(r'(?m)^@@[^\n]*\n', part)[1:]
        if not hunks:
            return False
        for hunk in hunks:
            removed, added = [], []
            def check():
                before, after = tokens('\n'.join(removed)), tokens('\n'.join(added))
                return contains(source, after) and (not before or not contains(source, before))
            for line in hunk.splitlines() + [' ']:
                if line.startswith('-') and not line.startswith('-- '):
                    removed.append(line[1:])
                elif line.startswith('+'):
                    added.append(line[1:])
                else:
                    if removed or added:
                        if not check():
                            return False
                        changed = True
                        removed, added = [], []
    return changed


def redundant_patch_repairs(spec, log, archive, payloads, review=None):
    """Remove obsolete patches only when all existing hunks are already applied."""
    import subprocess
    import tempfile
    if not re.search(r'No file to patch|hunks? FAILED|hunks? ignored|Reversed .*patch', log, re.I):
        return spec, []
    guard = re.search(r'(?m)^%global\s+source0_hash\s+([0-9a-f]{64}|none)\s*$', spec)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not guard or guard[1] not in ('none', digest):
        return spec, []
    actions = []
    if review is not None:
        for declaration in re.findall(r'(?m)^Patch\d*:\s*(\S+)', spec):
            if declaration not in payloads:
                review.append('Review patch ' + declaration + ': patch contents unavailable')
    with tempfile.TemporaryDirectory(prefix='orbscli-patch-check-') as folder:
        with tarfile.open(archive) as tar:
            tar.extractall(folder, filter='data')
        roots = list(Path(folder).iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            return spec, []
        root = roots[0]
        import shutil
        work = Path(folder) / '.patch-work'
        shutil.copytree(root, work, symlinks=True)
        strip_match = re.search(r'(?m)^%(?:autosetup|autopatch|patch\d*)[^\n]*?\s-p\s*(\d+)', spec)
        strip = int(strip_match[1]) if strip_match else 1
        for declaration, payload in payloads.items():
            parts = re.split(r'(?m)(?=^diff --git )', payload) if re.search(r'(?m)^diff --git ', payload) else re.split(r'(?m)(?=^--- )', payload)
            existing = []
            safe = True
            for part in parts:
                if not part.startswith(('diff --git ', '--- ')):
                    continue
                match = re.search(r'(?m)^\+\+\+ ([^\s]+)', part)
                if not match or '..' in Path(match[1]).parts or Path(match[1]).is_absolute():
                    safe = False
                    break
                relative = '/'.join(match[1].split('/')[strip:])
                path = root / relative
                if path.is_file():
                    existing.append(part.split('\nFrom ', 1)[0])
                else:
                    safe = False
                    break
            if not safe or not any(part.startswith(('diff --git ', '--- ')) for part in parts):
                if review is not None:
                    review.append('Review patch ' + declaration + ': cannot verify every changed file')
                continue
            if existing:
                checked = subprocess.run(['patch', '--dry-run', '--batch', '--reverse', '--force', '--fuzz=0', '-p' + str(strip)],
                                         input=''.join(existing), cwd=root, text=True, capture_output=True)
                if checked.returncode and not patch_postimage_present(existing, root, strip):
                    # Preserve patch order. Later patches may depend on an
                    # earlier patch which still needs to be applied.
                    forward = subprocess.run(['patch', '--dry-run', '--batch', '--forward', '--fuzz=0', '-p' + str(strip)],
                                             input=payload, cwd=work, text=True, capture_output=True)
                    if forward.returncode == 0:
                        subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p' + str(strip)],
                                       input=payload, cwd=work, text=True, capture_output=True, check=True)
                    elif review is not None:
                        review.append('Review patch ' + declaration + ': does not apply and is not proven upstreamed')
                    continue
            revised = re.sub(r'(?m)^Patch\d*:\s*' + re.escape(declaration) + r'[ \t]*\n', '', spec)
            if revised != spec:
                spec = revised
                actions.append({'action': 'Remove obsolete patch ' + declaration.rsplit('/', 1)[-1],
                                'evidence': 'Every changed block is already present in the verified upstream source'})
    if actions and guard[1] == 'none':
        spec = re.sub(r'(?m)^(%global[ \t]+source0_hash[ \t]+)none[ \t]*$', lambda m: m[1] + digest, spec, count=1)
    return spec, actions


def manifest_api_requirements(spec, log, contents):
    """Match compiler diagnostic source paths to pinned upstream dependencies."""
    import json
    if not re.search(r'error:.*(?:not a member of|no member named)', log):
        return []
    paths = re.findall(r'(?m)^([^\s:]+):\d+:\d+: error:', log)
    normalize = lambda value: re.sub(r'[^a-z0-9]', '', value.lower())
    declared = re.findall(r'(?m)^BuildRequires:\s+([^\s<>=]+)', spec)
    result = []
    for name, payload in contents.items():
        if not name.endswith('known_good.json'):
            continue
        for repo in json.loads(payload).get('repos', []):
            dep, commit = repo.get('name', ''), repo.get('commit', '')
            if not re.fullmatch(r'[0-9a-f]{40}', commit):
                continue
            if not any(normalize(dep) == normalize(package.removesuffix('-devel')) for package in declared):
                continue
            if any(normalize(dep) in normalize(Path(path).name) for path in paths):
                result.append(dep.lower() + '-commit(' + commit + ')')
    return sorted(set(result))


def resource_candidates(old, names):
    stems = {old.casefold()}
    if old.casefold() in ('license', 'licence'):
        stems.update(('license', 'licence'))
    return sorted(name for name in names if '/' not in name
                  and name.casefold() in {stem + suffix for stem in stems for suffix in ('', '.md', '.rst', '.txt')})


def verified_source_repairs(spec: str, log: str, archive: Path) -> tuple[str, list[dict[str, str]], list[str]]:
    """Rename packaged docs only when the exact guarded source proves the name."""
    import tarfile
    guard = re.search(r'(?m)^%global\s+source0_hash\s+([0-9a-f]{64}|none)\s*$', spec)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not guard or (guard[1] != 'none' and digest != guard[1]):
        return spec, [], ['Source archive does not match source0_hash. No source-based repairs were applied.']
    try:
        with tarfile.open(archive) as tar:
            # No extraction or execution of upstream files.
            members = [m for m in tar.getmembers() if m.isfile()]
            names = {m.name.split('/', 1)[1] for m in members if '/' in m.name}
            contents = {m.name.split('/', 1)[1]: tar.extractfile(m).read()
                        for m in members if '/' in m.name and m.size < 2_000_000
                        and m.name.endswith(('/pyproject.toml', '/programs/algparse/algparse.c', '/scripts/known_good.json'))}
    except (tarfile.TarError, OSError):
        return spec, [], ['Verified source could not be read as a tar archive. Documentation was not changed.']
    actions = []
    for dependency in manifest_api_requirements(spec, log, contents):
        revised = add_build_requirement(spec, dependency)
        if revised != spec:
            spec = revised
            actions.append({'action': 'Require upstream dependency revision ' + dependency,
                            'evidence': 'Missing API in a dependency-specific source file and verified upstream revision manifest'})
    if re.search(r'error:.*(?:not a member of|no member named).*spv', log) and 'scripts/known_good.json' in contents:
        import json
        repos = json.loads(contents['scripts/known_good.json']).get('repos', [])
        commits = [r.get('commit') for r in repos if r.get('name') == 'SPIRV-Headers']
        if len(commits) == 1 and re.fullmatch(r'[0-9a-f]{40}', commits[0] or ''):
            dependency = 'spirv-headers-commit(' + commits[0] + ')'
            revised = add_build_requirement(spec, dependency)
            if revised != spec:
                spec = revised
                actions.append({'action': 'Require the SPIR-V headers revision used by upstream',
                                'evidence': 'Missing SPIR-V enum and verified upstream known_good.json revision'})
    if 'build backend cannot provide build metadata' in log and 'pyproject.toml' in contents:
        import tomllib
        project = tomllib.loads(contents['pyproject.toml'].decode()).get('project', {})
        if project and not set(project.get('dynamic', ())) & {'dependencies', 'optional-dependencies'}:
            revised = re.sub(r'(?m)^(%pyproject_buildrequires)(?![^\n]*\s-p(?:\s|$))([^\n]*)$', r'\1 -p\2', spec)
            if revised != spec:
                spec = revised
                actions.append({'action': 'Read declared Python dependencies directly from pyproject.toml',
                                'evidence': 'Backend cannot prepare metadata and source declares static project dependencies'})
    algparse = contents.get('programs/algparse/algparse.c', b'')
    if "ipsec algparse: invalid option -- 't'" in log and b'OPT("tp")' in algparse and b'OPT("ta")' in algparse:
        revised = re.sub(r'(?m)^(.*?/ipsec/algparse )-(t[pa])(?=\s|$)', r'\1--\2', spec)
        if revised != spec:
            spec = revised
            actions.append({'action': 'Use current algparse test options --tp and --ta',
                            'evidence': 'Source option table defines the long test options'})
    missing = set(re.findall(r"cp: cannot stat '([^']+)': No such file or directory", log))
    for path in sorted(missing):
        old = Path(path).name
        if old in names:
            continue
        candidates = resource_candidates(old, names)
        if len(candidates) > 1:
            continue
        # The failed source copy plus the verified archive prove the doc is absent.
        new = candidates[0] if candidates else ''
        # Restrict changes to actual %doc lines. descriptions and comments stay intact.
        lines = spec.splitlines(True)
        changed = False
        for i, line in enumerate(lines):
            if line.startswith(('%doc ', '%license ')):
                # A missing license is never removed. It must have an actual,
                # uniquely identified replacement in the verified archive.
                if line.startswith('%license ') and not new:
                    continue
                code, separator, comment = line.partition('#')
                revised = re.sub(r'(?<!\S)' + re.escape(old) + r'(?!\S)', new, code)
                if revised != code:
                    if revised.strip() == '%doc':
                        lines[i] = separator + comment if separator else ''
                    elif separator:
                        lines[i] = revised.rstrip() + ' ' + separator + comment
                    else:
                        lines[i] = revised.rstrip() + ('\n' if line.endswith('\n') else '')
                    changed = True
        if changed:
            spec = ''.join(lines)
            actions.append({'action': ('Rename packaged doc ' + old + ' to ' + new) if new else
                            ('Remove missing documentation ' + old),
                            'evidence': 'Missing-file log plus SHA256-verified Source0 member'})
    if actions and guard[1] == 'none':
        spec = spec[:guard.start(1)] + digest + spec[guard.end(1):]
    return spec, actions, []


def friendly_summary(chain_id, plans, *, apply, log_dir=None, job_count=None):
    """Show each actionable result once, with a short next step."""
    import shlex
    changed = [p for p in plans if p.get('actions')]
    blocked = [p for p in plans if p.get('missing_dependencies') or p.get('source_requirements')]
    existing = [p for p in plans if p.get('already_applied')]
    unresolved = [p for p in plans if p.get('manual') or
                  not (p.get('actions') or p.get('missing_dependencies') or p.get('already_applied'))]
    count = sum(len(p['actions']) for p in changed)
    heading = f'Chain {chain_id}: {len(plans)} failed package{"s" if len(plans) != 1 else ""}'
    if job_count is not None and job_count != len(plans):
        heading += f' ({job_count} jobs)'
    lines = [heading]
    if changed:
        lines += ['', f'{"Applied" if apply else "Proposed"} {count} spec edit{"s" if count != 1 else ""}:']
    for plan in changed:
        lines.append('  ' + plan['package'])
        file_actions = [action for action in plan['actions'] if action['action'].startswith('Package installed file ')]
        if len(file_actions) > 3:
            lines.append('    - Include ' + str(len(file_actions)) + ' installed files in their owning package')
        for action in plan['actions']:
            if len(file_actions) > 3 and action in file_actions:
                continue
            text = action['action'].replace('Add BuildRequires: ', 'Add a build dependency: ').replace(
                'Package installed file ', 'Include the installed file: ')
            lines.append('    - ' + text)
    if existing:
        lines += ['', 'Edits already present locally:']
        lines.extend('  ' + p['package'] for p in existing)
    cleared_checksums = [p['package'] for p in plans if p.get('repo_checksum_verified')]
    if cleared_checksums:
        lines += ['', 'Repository RPM checksums now match current metadata:']
        lines.extend('  ' + package for package in cleared_checksums)
    if blocked:
        lines += ['', 'Missing prerequisites:']
    for plan in blocked:
        suggestions = {x['requirement']: x.get('candidate_sources', []) for x in plan.get('prerequisites', [])}
        lines.append('  ' + plan['package'])
        for req in plan['missing_dependencies'] + plan.get('source_requirements', []):
            candidates = suggestions.get(req, [])
            lines.append(('    - Missing ' if req in plan['missing_dependencies'] else '    - Requires source revision ') + req)
            lines.append('      Candidate source: ' + ', '.join(candidates) if candidates else '      No local provider found')
    if unresolved:
        lines += ['', 'Still blocked:']
    for plan in unresolved:
        notes = plan.get('manual') or ['The dependency is already listed in the spec. these logs do not prove a successful build.']
        notes = [n.replace('Failure needs review. no supported repair rule matched',
                           'The automatic rules cannot resolve this failure.') for n in notes]
        lines.append('  ' + plan['package'])
        for note in notes:
            provider = re.fullmatch(r'No unique repository provider for (python|header|executable) (.+)', note)
            if provider:
                kind = {'python': 'Python module', 'header': 'header', 'executable': 'command'}[provider[1]]
                lines.append(f'    Cannot identify a repository package for {kind} {provider[2]}.')
                lines.append('    Check that the provider is published, then add its BuildRequires.')
            else:
                lines.append('    ' + note)
        diagnostics = plan.get('diagnostics', [])
        if diagnostics and not any('No unique repository provider for ' in n for n in notes):
            lines.append('    Log: ' + diagnostics[0])
    if not (changed or existing or blocked or unresolved or cleared_checksums):
        lines += ['', 'No local change was needed. Retry the failed builds.']
    if changed and not apply:
        command = 'python cli/orbscli.py repair-chain ' + str(chain_id)
        if log_dir is not None:
            command += ' --log-dir ' + shlex.quote(str(log_dir))
        lines += ['', 'Apply the proposed edits:', '  ' + command + ' --apply']
    if blocked:
        sources = sorted({source for plan in blocked for choice in plan.get('prerequisites', [])
                          if len(choice.get('candidate_sources', [])) == 1
                          for source in choice['candidate_sources']})
        if sources:
            lines += ['', 'Check prerequisite build order:',
                '  python cli/orbscli.py plan chain --release oreon-11-rp1 --packages ' +
                         shlex.quote(' '.join(sources))]
    if changed and apply:
        lines += ['', 'Publish the edited specs, then retry their builds.']
    if existing:
        lines += ['', 'Publish the existing edits, then retry those builds.']
    return '\n'.join(lines).replace(';', '.')


def macro_patch_repairs(spec_path, log, archive):
    """Complete a local macro rename when a compiler identifies a missed C file."""
    import tarfile
    spec = spec_path.read_text()
    guard = re.search(r'(?m)^%global\s+source0_hash\s+([0-9a-f]{64})\s*$', spec)
    if not guard or hashlib.sha256(archive.read_bytes()).hexdigest() != guard[1]:
        return [], []
    failures = re.findall(r'([\w./-]+\.c):\d+:\d+:\s*error:\s*[‘\'](\w+)[’\'] undeclared', log)
    if not failures:
        return [], []
    changes, actions = [], []
    for name in re.findall(r'(?m)^Patch\d*:\s*([^\s]+)', spec):
        if '/' in name or '%' in name:
            continue
        patch_path = spec_path.parent / name
        if not patch_path.is_file():
            continue
        original = patch_path.read_text()
        removed = re.findall(r'^-#\s*define\s+(\w+)\b', original, re.M)
        added = re.findall(r'^\+#\s*define\s+(\w+)\b', original, re.M)
        pairs = set(zip(removed, added)) if len(removed) == len(added) else set()
        updated = original
        with tarfile.open(archive) as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            for filename, old in sorted(set(failures)):
                replacements = {new for before, new in pairs if before == old and new != old}
                matches = [m for m in members if m.name.split('/', 1)[-1] == filename]
                if not matches:
                    matches = [m for m in members if Path(m.name).name == Path(filename).name]
                    if len(matches) == 1:
                        filename = matches[0].name.split('/', 1)[-1]
                if len(replacements) != 1 or len(matches) != 1:
                    continue
                if '--- a/' + filename + '\n' in updated:
                    continue
                new = replacements.pop()
                content = tar.extractfile(matches[0]).read().decode()
                # Preserve strings and comments. Only replace C identifier tokens.
                pattern = r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\b' + re.escape(old) + r'\b'
                rewritten = re.sub(pattern, lambda m: new if m.group() == old else m.group(), content, flags=re.S)
                if rewritten == content:
                    continue
                updated += ''.join(difflib.unified_diff(content.splitlines(True), rewritten.splitlines(True),
                                                        fromfile='a/' + filename, tofile='b/' + filename))
                actions.append({'action': 'Complete macro rename in ' + filename + ' (' + old + ' to ' + new + ')',
                                'evidence': 'Compiler error, existing rename patch and verified Source0'})
        if updated != original:
            changes.append({'path': str(patch_path), 'before_sha256': hashlib.sha256(original.encode()).hexdigest(),
                            'updated': updated})
    return changes, actions


def needs_macro_source(spec_path, log):
    missing = set(re.findall(r'error:\s*[‘\'](\w+)[’\'] undeclared', log))
    if not missing:
        return False
    for name in re.findall(r'(?m)^Patch\d*:\s*([^\s]+)', spec_path.read_text()):
        if '/' in name or '%' in name:
            continue
        patch = spec_path.parent / name
        if patch.is_file() and missing.intersection(re.findall(r'^-#\s*define\s+(\w+)\b', patch.read_text(), re.M)):
            return True
    return False


def verified_build_repairs(spec_path, spec, log, archive):
    """Apply narrow source transformations supported by a specific compiler error."""
    import tarfile
    guard = re.search(r'(?m)^%global\s+source0_hash\s+([0-9a-f]{64})\s*$', spec)
    if not guard or not tarfile.is_tarfile(archive) or hashlib.sha256(archive.read_bytes()).hexdigest() != guard[1]:
        return spec, [], [], False
    edits = []
    bundle_already = False
    with tarfile.open(archive) as tar:
        members = {m.name.split('/', 1)[1]: m for m in tar.getmembers() if m.isfile() and '/' in m.name}
        if ('t/11_TCP_ipv4.t' in log and 't/12_TCP_ipv6.t' in log and 'Failed' in log
                and 'lib/Net/Server/IP.pm' in members):
            filename = 'lib/Net/Server/IP.pm'
            content = tar.extractfile(members[filename]).read().decode()
            if 'Simple ephemeral sanity pre-check' in content and '$fake_danger' in content:
                updated = content.replace('LocalAddr=>"[::]", Listen=>1',
                                          'LocalAddr=>"[::]", Family=>AF_INET6, Listen=>1')
                updated = updated.replace('LocalAddr=>"127.0.0.1", Listen=>1',
                                          'LocalAddr=>"127.0.0.1", Family=>AF_INET, Listen=>1')
                if updated != content:
                    edits.append((filename, content, updated))
        for filename, member in members.items():
            if not filename.endswith(('.c', '.cpp')):
                continue
            if Path(filename).name not in log:
                continue
            content = tar.extractfile(member).read().decode(errors='replace')
            updated = content
            if filename.endswith('.cpp') and 'no matching function for call' in log and ('find(' in log or 'find(' in content):
                if 'std::find(' in content and '#include <algorithm>' not in content:
                    updated = re.sub(r'(?m)^(#include .*)$', '#include <algorithm>\n\\1', content, count=1)
            if '[-Werror=stringop-truncation]' in log:
                old = "if (dstlen <= nreal) {          /* prefix won't fit */\n\t\t/* fill_and_terminate */\n\t\tstrncpy(dst, prefix, dstlen-1);\n\t\tdst[dstlen-1] = '\\0';"
                if old in content:
                    updated = content.replace(old, old.replace('strncpy(', 'memcpy('))
            if updated != content:
                edits.append((filename, content, updated))
    with tarfile.open(archive) as tar:
        for method, package, filename in re.findall(r'Can.t locate object method "(\w+)" via package "([\w:]+)" at ([\w./-]+\.t) line', log):
            tests = [m for m in tar.getmembers() if m.isfile() and m.name.split('/', 1)[-1] == filename]
            modules = [m for m in tar.getmembers() if m.isfile() and m.name.endswith('/lib/' + package.replace('::', '/') + '.pm')]
            if len(tests) != 1 or len(modules) != 1:
                continue
            module = tar.extractfile(modules[0]).read().decode()
            content = tar.extractfile(tests[0]).read().decode()
            if not re.search(r'(?m)^sub\s+' + re.escape(method) + r'\b', module):
                continue
            if re.search(r'\b(?:use|require)\s+' + re.escape(package) + r'\b', content):
                continue
            updated = re.sub(r'(?m)^(.*' + re.escape(package + '->' + method) + r'.*)$',
                             'require ' + package + ';\n' + r'\1', content, count=1)
            if updated != content:
                edits.append((filename, content, updated))
    actions, auxiliary = [], []
    if edits:
        existing = re.findall(r'(?m)^Patch\d*:\s*((?:[0-9]+|orbscli-build-fixes)\.patch)\s*$', spec)
        name = next((name for name in reversed(existing) if (spec_path.parent / name).is_file()), None)
        if name is None:
            name = str(int(time.time())) + '.patch'
            if (spec_path.parent / name).exists():
                name = str(time.time_ns()) + '.patch'
        path = spec_path.parent / name
        previous = path.read_text() if path.exists() else ''
        patch = previous
        for filename, before, after in edits:
            delta = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               fromfile='a/'+filename, tofile='b/'+filename))
            if delta not in patch:
                if '--- a/'+filename+'\n' in patch:
                    continue
                patch += delta
                actions.append({'action': ('Fix socket address family probes in ' if filename == 'lib/Net/Server/IP.pm' else 'Load the required test module in ' if filename.endswith('.t') else 'Repair compiler failure in ') + filename,
                                'evidence': 'Exact diagnostic and SHA256-verified source'})
        if patch != previous:
            if not re.search(r'(?m)^Patch\d*:\s*'+re.escape(name)+r'\s*$', spec):
                number = max([int(n or '0') for n in re.findall(r'(?m)^Patch(\d*):', spec)] + [0]) + 1
                if re.search(r'(?m)^%(?:autosetup|autopatch).*\s-p1(?:\s|$)', spec):
                    pass
                elif re.search(r'(?m)^%setup[^\n]*$', spec):
                    spec = re.sub(r'(?m)^(%setup[^\n]*)$', r'\1\n%patch ' + str(number) + ' -p1', spec, count=1)
                else:
                    return spec, [], [], False
                spec = re.sub(r'(?m)^Name\s*:', 'Patch'+str(number)+': '+name+'\nName:', spec, count=1)
            auxiliary.append({'path':str(path), 'before_sha256':hashlib.sha256(previous.encode()).hexdigest() if path.exists() else None,
                              'updated':patch})
    # Tests that inspect a removed bundled module test that bundle, not this package.
    if 'Cannot open inc/Devel/CheckLib.pm:' in log and re.search(r'(?m)^rm -r inc\s*$', spec):
        removals=[]
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                filename=member.name.split('/',1)[-1]
                if filename not in ('t/checklib_findcc.t','t/checklib_tmpdir.t'):
                    continue
                content=tar.extractfile(member).read().decode()
                if 'inc' in content and 'CheckLib.pm' in content and 'my $source' in content:
                    removals.append(filename)
        command='rm -f '+' '.join(sorted(removals))
        bundle_already = bool(removals and command in spec)
        if removals and command not in spec:
            spec=re.sub(r'(?m)^(rm -r inc)\s*$', r'\1\n# These tests inspect the bundled module removed above.\n'+command, spec, count=1)
            actions.append({'action':'Remove tests of the deleted bundled CheckLib source. Keep XML parser tests.',
                            'evidence':'Missing bundled source, unbundling command and verified test contents'})
    return spec, actions, auxiliary, not actions and (bool(edits) or bundle_already)
