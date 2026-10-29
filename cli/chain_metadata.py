# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read source dependency metadata without running upstream build scripts."""
from __future__ import annotations

import ast
import configparser
import email.parser
import hashlib
import fnmatch
import glob
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import contextlib
import tarfile
import tempfile
import tokenize
import tomllib
from urllib.parse import urlsplit
import zipfile
import xml.etree.ElementTree as ET

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from packaging.specifiers import SpecifierSet


class SourceMetadataError(RuntimeError):
    pass


def _present_patch_sections(text, source, strip):
    parts = re.split(r'(?m)^(?=diff --git |--- (?!-))', text)
    head = parts[0] if parts and not parts[0].startswith(('diff --git ', '--- ')) else ''
    out = []
    pending = ''
    for part in parts[1:] if head else parts:
        if part.startswith('diff --git ') and '\n--- ' not in part:
            pending = part
            continue
        section = pending + part
        pending = ''
        match = re.search(r'(?m)^\+\+\+\s+([^\t\n ]+)', section)
        if not match:
            continue
        old = re.search(r'(?m)^---\s+([^\t\n ]+)', section)
        name = match[1]
        if name == '/dev/null' and old:
            name = old[1]
        if match[1] != '/dev/null' and old and old[1] == '/dev/null':
            out.append(section)
            continue
        relative = PurePosixPath(*PurePosixPath(name).parts[strip:]) if len(PurePosixPath(name).parts) > strip else None
        if relative is not None and ((source / relative).exists()
                                     or re.search(r'(?m)^@@ -0,0 ', section)):
            out.append(section)
    return ''.join(out)


def _read_python_source(path):
    with tokenize.open(path) as stream:
        return stream.read()


def texlive_file_owners(database):
    """Map TeX Live filenames to package names from the repository tlpdb."""
    owners = {}
    package = None
    for line in database.splitlines():
        if line.startswith('name '):
            package = line[5:].strip()
        elif package and line[:1].isspace():
            entry = line.strip().split(None, 1)[0]
            path = entry.removeprefix('RELOC/')
            if '/tex/' not in '/' + path or not path.endswith('.sty'):
                continue
            owners.setdefault(PurePosixPath(path).name, set()).add(package)
    return owners


def maven_root_provides(source):
    pom = source / 'pom.xml'
    if not pom.is_file():
        return []
    try:
        root = ET.parse(pom).getroot()
    except ET.ParseError as exc:
        raise SourceMetadataError('Invalid root pom.xml') from exc
    ns = {'p': 'http://maven.apache.org/POM/4.0.0'}
    def value(path):
        node = root.find(path, ns)
        if node is None:
            node = root.find(path.replace('p:', ''))
        return node.text.strip() if node is not None and node.text else None
    group = value('p:groupId') or value('p:parent/p:groupId')
    artifact = value('p:artifactId')
    packaging = value('p:packaging') or 'jar'
    if not group or not artifact or any('${' in part for part in (group, artifact, packaging)):
        raise SourceMetadataError('Cannot resolve root Maven coordinates')
    return ['mvn(' + group + ':' + artifact + ')',
            'mvn(' + group + ':' + artifact + ':' + packaging + ':)']


def maven_module_provides(source, expanded):
    disabled = set(re.findall(r'(?m)^%pom_disable_module\s+(\S+)', expanded))
    uninstalled = set(re.findall(r'(?m)^%mvn_package\s+:([^\s*{}]+)\s+__noinstall', expanded))
    jdk_versions = {int(version) for version in re.findall(r'maven-local-openjdk(\d+)', expanded)}
    ns = {'p': 'http://maven.apache.org/POM/4.0.0'}
    visited = set()
    found = {}
    def visit(directory):
        pom = directory / 'pom.xml'
        if pom in visited or not pom.is_file():
            return
        visited.add(pom)
        try:
            root = ET.parse(pom).getroot()
        except ET.ParseError as exc:
            raise SourceMetadataError('Invalid Maven module POM: ' + str(pom)) from exc
        def value(path):
            node = root.find(path, ns)
            if node is None:
                node = root.find(path.replace('p:', ''))
            return node.text.strip() if node is not None and node.text else None
        group = value('p:groupId') or value('p:parent/p:groupId')
        artifact = value('p:artifactId')
        packaging = value('p:packaging') or 'jar'
        version = value('p:version') or value('p:parent/p:version')
        if group and artifact and not any('${' in part for part in (group, artifact, packaging)) and artifact not in uninstalled:
            capabilities = [f'mvn({group}:{artifact})', f'mvn({group}:{artifact}:{packaging}:)']
            if version and '${' not in version:
                capabilities.append(f'mvn({group}:{artifact}:{version})')
            found[artifact] = tuple(capabilities)
        modules = [*root.findall('p:modules/p:module', ns), *root.findall('modules/module')]
        for profile in [*root.findall('p:profiles/p:profile', ns), *root.findall('profiles/profile')]:
            activation = profile.find('p:activation/p:jdk', ns)
            if activation is None:
                activation = profile.find('activation/jdk')
            expression = activation.text.strip() if activation is not None and activation.text else ''
            bounds = re.fullmatch(r'(\[|\()(\d+)(?:\.(\d+))?,(\d+(?:\.\d+)?)?(\)|\])', expression)
            if not bounds or not jdk_versions:
                continue
            lower = int(bounds[2]) if int(bounds[2]) != 1 or not bounds[3] else int(bounds[3])
            upper = int(bounds[4].split('.')[0]) if bounds[4] else None
            if any((version > lower or version == lower and bounds[1] == '[')
                   and (upper is None or version < upper or version == upper and bounds[5] == ']')
                   for version in jdk_versions):
                modules.extend([*profile.findall('p:modules/p:module', ns), *profile.findall('modules/module')])
        for module in modules:
            if module.text and module.text.strip() not in disabled:
                child = (directory / module.text.strip()).resolve()
                if child.is_relative_to(source.resolve()):
                    visit(child)
    visit(source)
    for filename in re.findall(r'(?m)^%mvn_artifact\s+([^\s]+\.(?:xml|pom))\s+', expanded):
        path = (source / filename).resolve()
        if not path.is_relative_to(source.resolve()):
            raise SourceMetadataError('Declared Maven artifact POM leaves source: ' + filename)
        if path.is_file():
            try:
                root = ET.parse(path).getroot()
            except ET.ParseError as exc:
                raise SourceMetadataError('Invalid Maven artifact POM: ' + filename) from exc
        elif filename.startswith('build/') and (source / 'build.xml').is_file():
            try:
                build = ET.parse(source / 'build.xml').getroot()
            except ET.ParseError as exc:
                raise SourceMetadataError('Invalid Ant build.xml') from exc
            templates = [copy for copy in build.iter('copy')
                         if copy.get('filtering') == 'true' and copy.get('tofile', '').endswith('.pom')
                         and (source / copy.get('file', '')).is_file()]
            if len(templates) != 1:
                continue
            template = templates[0]
            content = (source / template.get('file')).read_text()
            properties = {item.get('name'): item.get('value') for item in build.iter('property')
                          if item.get('name') and item.get('value') and '${' not in item.get('value')}
            for item in template.findall('filterset/filter'):
                token, replacement = item.get('token'), item.get('value')
                if token and replacement:
                    for key, value in properties.items():
                        replacement = replacement.replace('${' + key + '}', value)
                    content = content.replace('@' + token + '@', replacement)
            try:
                root = ET.fromstring(content)
            except ET.ParseError as exc:
                raise SourceMetadataError('Invalid generated Maven artifact template: ' + filename) from exc
        else:
            continue
        def value(name):
            node = root.find('p:' + name, ns)
            if node is None:
                node = root.find(name)
            return node.text.strip() if node is not None and node.text else None
        group, artifact = value('groupId'), value('artifactId')
        if not group:
            parent = root.find('p:parent/p:groupId', ns)
            if parent is None:
                parent = root.find('parent/groupId')
            group = parent.text.strip() if parent is not None and parent.text else None
        packaging = value('packaging') or 'jar'
        version = value('version')
        if not group or not artifact or any('${' in part for part in (group, artifact, packaging)):
            continue
        capabilities = [f'mvn({group}:{artifact})', f'mvn({group}:{artifact}:{packaging}:)']
        if version and '${' not in version:
            capabilities.append(f'mvn({group}:{artifact}:{version})')
        found[artifact] = tuple(capabilities)
    for line in expanded.replace('\\\n', ' ').splitlines():
        if not line.strip().startswith('%mvn_alias '):
            continue
        try:
            aliases = shlex.split(line.strip())[1:]
        except ValueError:
            continue
        if len(aliases) < 2 or ':' not in aliases[0]:
            continue
        old_group, old_artifact = aliases[0].split(':', 1)
        expression = re.escape(old_artifact).replace(re.escape('{*}'), '(.*)')
        for artifact, capabilities in list(found.items()):
            original = re.fullmatch(r'mvn\(([^:]+):[^)]+\)', capabilities[0])
            if not original or (old_group and old_group != original[1]):
                continue
            match = re.fullmatch(expression, artifact) if old_artifact else (re.fullmatch('.*', artifact) if len(found) == 1 else None)
            if not match:
                continue
            extras = []
            for target in aliases[1:]:
                if ':' not in target:
                    continue
                group, alias = target.split(':', 1)
                group = group or original[1]
                alias = alias.replace('@1', match[1] if match.lastindex else '')
                if re.fullmatch(r'[A-Za-z0-9_.-]+', group) and re.fullmatch(r'[A-Za-z0-9_.-]+', alias):
                    extras.append(f'mvn({group}:{alias})')
            found[artifact] = (*capabilities, *extras)
    for line in expanded.splitlines():
        match = re.match(r'^%mvn_compat_version\s+(\S+)\s+(.+)$', line)
        if not match:
            continue
        for artifact, capabilities in list(found.items()):
            coordinate = re.fullmatch(r'mvn\(([^:]+):[^)]+\)', capabilities[0])
            if coordinate and (match[1] == ':' or match[1] == f'{coordinate[1]}:{artifact}'):
                found[artifact] = (*capabilities, *(f'mvn({coordinate[1]}:{artifact}:{version})'
                    for version in match[2].split() if re.fullmatch(r'[A-Za-z0-9_.+-]+', version)))
    return found


def maven_declared_poms(cli, expanded, binaries, directory, filelists):
    provided = {binary['name']: set() for binary in binaries}
    for url in re.findall(r'(?mi)^Source[1-9][0-9]*\s*:\s*(\S+\.pom)\s*$', expanded):
        if not url.startswith('https://'):
            continue
        candidate = Path(urlsplit(url).path).parent.parent.name
        stem = re.sub(r'-jdk\d+on$', '', candidate)
        matching = [binary for binary in binaries if
                    filelists.get(binary['name'], set()) & {'.mfiles-' + candidate, '.mfiles-' + stem}]
        if not matching:
            continue
        target = directory / hashlib.sha256(url.encode()).hexdigest()
        if not cli._fetch_source_archive(cli._genbr_http(), url, target):
            raise SourceMetadataError('Could not download declared Maven POM: ' + url)
        try:
            root = ET.parse(target).getroot()
        except ET.ParseError as exc:
            raise SourceMetadataError('Invalid declared Maven POM: ' + url) from exc
        ns = {'p': 'http://maven.apache.org/POM/4.0.0'}
        def value(path):
            node = root.find(path, ns)
            if node is None:
                node = root.find(path.replace('p:', ''))
            return node.text.strip() if node is not None and node.text else None
        group = value('p:groupId') or value('p:parent/p:groupId')
        artifact = value('p:artifactId')
        packaging = value('p:packaging') or 'jar'
        if not group or not artifact or any('${' in part for part in (group, artifact, packaging)):
            raise SourceMetadataError('Cannot resolve declared Maven POM: ' + url)
        for binary in matching:
            if artifact != candidate:
                raise SourceMetadataError('Declared Maven POM does not match its package: ' + url)
            provided[binary['name']].update((f'mvn({group}:{artifact})',
                                             f'mvn({group}:{artifact}:{packaging}:)'))
            for line in expanded.replace('\\\n', ' ').splitlines():
                if not line.strip().startswith('%mvn_alias '):
                    continue
                try:
                    aliases = shlex.split(line.strip())[1:]
                except ValueError:
                    continue
                if len(aliases) < 2:
                    continue
                variable = re.search(r'\$([A-Za-z_][A-Za-z0-9_]*)', aliases[0])
                if not variable:
                    continue
                def substitute(value):
                    return value.replace('$' + variable[1], stem)
                if substitute(aliases[0]) not in (':' + candidate, group + ':' + candidate):
                    continue
                for alias in aliases[1:]:
                    resolved = substitute(alias)
                    if re.fullmatch(r'[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+', resolved):
                        provided[binary['name']].add('mvn(' + resolved + ')')
    return provided


def python_requirement(value, environment, extras=(), pkgversion="3"):
    requirement = Requirement(value)
    if requirement.url:
        raise SourceMetadataError('Direct URL dependency needs an RPM provider: ' + value)
    if requirement.marker and not any(requirement.marker.evaluate({**environment, 'extra': extra})
                                      for extra in ('', *extras)):
        return []
    location = Path('/usr/lib/rpm/redhat/pyproject_convert.py')
    if not location.is_file():
        raise SourceMetadataError('Install pyproject-rpm-macros to convert Python version constraints.')
    spec = importlib.util.spec_from_file_location('orbscli_pyproject_convert', location)
    converter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(converter)
    name = canonicalize_name(requirement.name)
    result = []
    for dist in (name, *(name + '[' + canonicalize_name(e) + ']' for e in sorted(requirement.extras))):
        cap = 'python' + pkgversion + 'dist(' + dist + ')'
        bounds = [converter.convert(cap, s.operator, s.version)
                  for s in sorted(requirement.specifier, key=str)]
        result.append(cap if not bounds else bounds[0] if len(bounds) == 1 else '(' + ' with '.join(bounds) + ')')
    return result


def _stub_cargo_targets(folder):
    import tomllib
    for manifest in [folder / 'Cargo.toml', *folder.glob('*/Cargo.toml')]:
        if not manifest.is_file():
            continue
        try:
            data = tomllib.loads(manifest.read_text(encoding='utf-8', errors='replace'))
        except (ValueError, OSError):
            continue
        base = manifest.parent
        wanted = []
        lib = data.get('lib')
        if isinstance(lib, dict) and lib.get('path'):
            wanted.append(lib['path'])
        for kind in ('bin', 'example', 'test', 'bench'):
            for entry in data.get(kind, []) or []:
                if isinstance(entry, dict):
                    if entry.get('path'):
                        wanted.append(entry['path'])
                    elif entry.get('name') and kind == 'bin':
                        wanted.append('src/bin/%s.rs' % entry['name'])
        if 'package' in data and not wanted and not (base / 'src/lib.rs').is_file() and not (base / 'src/main.rs').is_file():
            wanted.append('src/lib.rs')
        build = data.get('package', {}).get('build') if isinstance(data.get('package'), dict) else None
        if isinstance(build, str):
            wanted.append(build)
        for item in wanted:
            target = (base / item).resolve()
            if target.is_relative_to(folder.resolve()) and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('')


def setup_metadata(source, environment=None):
    """Interpret declarative setup() values. Never execute setup.py."""
    result = {}
    path = source / 'setup.cfg'
    if path.is_file():
        config = configparser.ConfigParser(interpolation=None)
        config.read(path)
        for key in ('name', 'version'):
            if config.has_option('metadata', key):
                result[key] = config.get('metadata', key)
        for key in ('install_requires', 'setup_requires'):
            if config.has_option('options', key):
                result[key] = [line.strip() for line in config.get('options', key).splitlines() if line.strip()]
        if config.has_section('options.extras_require'):
            result['extras_require'] = {k: [line.strip() for line in v.splitlines() if line.strip()]
                                      for k, v in config.items('options.extras_require')}
        version = result.get('version', '')
        if version.startswith('attr:'):
            dotted = version.removeprefix('attr:').strip().split('.')
            attribute = dotted.pop()
            if not dotted or not all(part.isidentifier() for part in [*dotted, attribute]):
                raise SourceMetadataError('Invalid setuptools version attribute: ' + version)
            module = Path(*dotted)
            modules = [base / module.with_suffix('.py') for base in (source, source / 'src', source / 'lib')]
            modules += [base / module / '__init__.py' for base in (source, source / 'src', source / 'lib')]
            modules = [path for path in modules if path.is_file()]
            if len(modules) != 1:
                raise SourceMetadataError('Cannot locate setuptools version attribute: ' + version)
            declarations = []
            for node in ast.parse(_read_python_source(modules[0])).body:
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == attribute for target in node.targets):
                    declarations.append(ast.literal_eval(node.value))
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == attribute:
                    declarations.append(ast.literal_eval(node.value))
            if len(declarations) != 1 or not isinstance(declarations[0], str):
                raise SourceMetadataError('Setuptools version attribute is not a unique string literal: ' + version)
            result['version'] = declarations[0]
    path = source / 'setup.py'
    if not path.is_file():
        return result
    tree = ast.parse(_read_python_source(path))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    def conditional_operation(node):
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While)):
                return True
            parent = parents.get(parent)
        return False
    variables = {}
    evaluated_assignments = set()
    evaluated_mutations = set()
    environment = environment or default_environment()
    python_version = tuple(int(part) for part in environment.get('python_full_version', environment['python_version']).split('.')[:3])
    def condition(node):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not condition(node.operand)
        if isinstance(node, ast.Name) and isinstance(variables.get(node.id), bool):
            return variables[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'startswith' and len(node.args) == 1):
            receiver = value(node.func.value)
            argument = value(node.args[0])
            if isinstance(receiver, str) and isinstance(argument, str):
                return receiver.startswith(argument)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [condition(item) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
            left = node.left
            if (isinstance(left, ast.Attribute) and isinstance(left.value, ast.Name)
                    and left.value.id == 'sys' and left.attr == 'version_info'
                    and isinstance(node.comparators[0], (ast.Tuple, ast.List))
                    and all(isinstance(item, ast.Constant) and isinstance(item.value, int)
                            for item in node.comparators[0].elts)):
                actual, expected = python_version, tuple(item.value for item in node.comparators[0].elts)
                op = node.ops[0]
                for kind, answer in ((ast.GtE, actual >= expected), (ast.Gt, actual > expected),
                                     (ast.LtE, actual <= expected), (ast.Lt, actual < expected),
                                     (ast.Eq, actual == expected), (ast.NotEq, actual != expected)):
                    if isinstance(op, kind):
                        return answer
            if (isinstance(left, ast.Subscript) and isinstance(left.value, ast.Attribute)
                    and isinstance(left.value.value, ast.Name) and left.value.value.id == 'sys'
                    and left.value.attr == 'version_info' and isinstance(left.slice, ast.Constant)
                    and isinstance(left.slice.value, int) and 0 <= left.slice.value < len(python_version)
                    and isinstance(node.comparators[0], ast.Constant) and isinstance(node.comparators[0].value, int)):
                actual, expected = python_version[left.slice.value], node.comparators[0].value
                op = node.ops[0]
                for kind, answer in ((ast.GtE, actual >= expected), (ast.Gt, actual > expected),
                                     (ast.LtE, actual <= expected), (ast.Lt, actual < expected),
                                     (ast.Eq, actual == expected), (ast.NotEq, actual != expected)):
                    if isinstance(op, kind):
                        return answer
            if (isinstance(left, ast.Attribute) and isinstance(left.value, ast.Attribute)
                    and isinstance(left.value.value, ast.Name) and left.value.value.id == 'sys'
                    and left.value.attr == 'version_info'
                    and left.attr in ('major', 'minor', 'micro')
                    and isinstance(node.comparators[0], ast.Constant) and isinstance(node.comparators[0].value, int)):
                actual, expected = {'major': python_version[0], 'minor': python_version[1],
                                    'micro': python_version[2]}[left.attr], node.comparators[0].value
                op = node.ops[0]
                for kind, answer in ((ast.GtE, actual >= expected), (ast.Gt, actual > expected),
                                     (ast.LtE, actual <= expected), (ast.Lt, actual < expected),
                                     (ast.Eq, actual == expected), (ast.NotEq, actual != expected)):
                    if isinstance(op, kind):
                        return answer
        raise SourceMetadataError('Non-declarative setup.py condition: ' + ast.unparse(node))
    def imported_literal(module, name, seen=()):
        if module in seen or not module.is_file() or not module.resolve().is_relative_to(source.resolve()):
            raise SourceMetadataError('Cannot resolve setup.py import: ' + str(module))
        for declaration in ast.parse(_read_python_source(module)).body:
            if isinstance(declaration, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in declaration.targets):
                return ast.literal_eval(declaration.value)
            if isinstance(declaration, ast.ImportFrom) and declaration.level == 1:
                for alias in declaration.names:
                    if alias.asname == name or alias.asname is None and alias.name == name:
                        target = module.parent / (declaration.module or '').replace('.', '/')
                        return imported_literal(target.with_suffix('.py'), alias.name, (*seen, module))
        raise SourceMetadataError('Cannot resolve setup.py import: ' + name)
    class SourceFile:
        def __init__(self, name):
            path = (source / name).resolve()
            if not path.is_relative_to(source.resolve()) or not path.is_file():
                raise SourceMetadataError('Invalid setup metadata input: ' + name)
            self.text = path.read_text()
        def __iter__(self):
            return iter(self.text.splitlines(keepends=True))
    dependency_keys = {'install_requires', 'setup_requires', 'extras_require'}
    def non_dependency_mapping(name):
        assignments = [operation for operation in ast.walk(tree) if isinstance(operation, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == name for target in operation.targets)]
        return bool(assignments) and all(conditional_operation(operation)
            and isinstance(operation.value, ast.Dict)
            and all(isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and key.value not in dependency_keys for key in operation.value.keys)
            for operation in assignments)
    def value(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in variables:
                return variables[node.id]
            if node.id == '__file__':
                return str(source / 'setup.py')
            if non_dependency_mapping(node.id):
                return {}
        if isinstance(node, ast.Subscript):
            try:
                return value(node.value)[value(node.slice)]
            except (KeyError, IndexError, TypeError) as error:
                raise SourceMetadataError('Unresolved setup.py source value: ' + ast.unparse(node)) from error
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return [value(n) for n in node.elts]
        if isinstance(node, ast.ListComp) and len(node.generators) == 1:
            generator = node.generators[0]
            if isinstance(generator.target, ast.Name) and not generator.is_async:
                name = generator.target.id
                existed, previous = name in variables, variables.get(name)
                result = []
                try:
                    for item in value(generator.iter):
                        variables[name] = item
                        if all(condition(test) for test in generator.ifs):
                            result.append(value(node.elt))
                finally:
                    if existed:
                        variables[name] = previous
                    else:
                        variables.pop(name, None)
                return result
        if isinstance(node, ast.Dict):
            result = {}
            for key, item in zip(node.keys, node.values):
                if key is None:
                    try:
                        result.update(value(item))
                    except (SourceMetadataError, TypeError):
                        continue
                else:
                    try:
                        result[value(key)] = value(item)
                    except (SourceMetadataError, TypeError):
                        continue
            return result
        if isinstance(node, ast.Compare):
            return condition(node)
        if isinstance(node, ast.IfExp):
            return value(node.body if condition(node.test) else node.orelse)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return value(node.left) + value(node.right)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and ast.unparse(node.func) == 'os.path.join':
                parts = [value(argument) for argument in node.args]
                if parts and all(isinstance(part, str) for part in parts):
                    return os.path.join(*parts)
            if isinstance(node.func, ast.Attribute) and ast.unparse(node.func) == 'os.path.dirname' and len(node.args) == 1:
                path = value(node.args[0])
                if isinstance(path, str):
                    return os.path.dirname(path)
            if isinstance(node.func, ast.Attribute) and ast.unparse(node.func) == 'os.path.abspath' and len(node.args) == 1:
                path = value(node.args[0])
                if isinstance(path, str):
                    candidate = Path(path)
                    if not candidate.is_absolute():
                        candidate = source / candidate
                    return str(candidate.resolve())
            if isinstance(node.func, ast.Name) and node.func.id in {
                    item.name for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}:
                return source_requirements(node)
            if isinstance(node.func, ast.Name) and node.func.id == 'open' and 1 <= len(node.args) <= 2:
                if len(node.args) == 2 and not (isinstance(node.args[1], ast.Constant) and node.args[1].value == 'r'):
                    raise SourceMetadataError('Unsupported setup.py file mode: ' + ast.unparse(node))
                return SourceFile(value(node.args[0]))
            if isinstance(node.func, ast.Name) and node.func.id == 'load' and len(node.args) == 1:
                file = value(node.args[0])
                if isinstance(file, SourceFile):
                    return json.loads(file.text)
            if isinstance(node.func, ast.Name) and node.func.id == 'str' and len(node.args) == 1:
                return str(value(node.args[0]))
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == 're' and node.func.attr == 'search':
                    return re.search(*(value(argument) for argument in node.args))
                receiver = value(node.func.value)
                if isinstance(receiver, SourceFile) and node.func.attr in ('read', 'readlines') and not node.args:
                    return receiver.text if node.func.attr == 'read' else receiver.text.splitlines(keepends=True)
                if isinstance(receiver, re.Match) and node.func.attr == 'group':
                    return receiver.group(*(value(argument) for argument in node.args))
                if isinstance(receiver, str) and node.func.attr in ('splitlines', 'strip') and not node.args:
                    return getattr(receiver, node.func.attr)()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'dict':
            result = {}
            for keyword in node.keywords:
                if keyword.arg is None:
                    try:
                        result.update(value(keyword.value))
                    except (SourceMetadataError, TypeError):
                        continue
                else:
                    try:
                        result[keyword.arg] = value(keyword.value)
                    except (SourceMetadataError, TypeError):
                        continue
            return result
        raise SourceMetadataError('Non-declarative setup.py dependency expression: ' + ast.unparse(node))
    def source_version(node):
        if isinstance(node, ast.Name):
            assignments = [assignment.value for assignment in tree.body if isinstance(assignment, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == node.id
                                   for target in assignment.targets)]
            if len(assignments) == 1:
                node = assignments[0]
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            raise SourceMetadataError('Cannot resolve dynamic setup.py version: ' + ast.unparse(node))
        functions = [item for item in tree.body
                     if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == node.func.id]
        if len(functions) != 1:
            raise SourceMetadataError('Cannot identify setup.py version function: ' + node.func.id)
        paths = {call.args[0].value for call in ast.walk(functions[0])
                 if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'open'
                 and len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
                 and isinstance(call.args[0].value, str)}
        attributes = {node.value.attr for node in ast.walk(functions[0])
                      if isinstance(node, ast.Return) and isinstance(node.value, ast.Attribute)}
        for imported in ast.walk(functions[0]):
            if isinstance(imported, ast.Import):
                paths.update(alias.name.replace('.', '/') + '.py' for alias in imported.names
                             if alias.name.split('.')[0] in {node.value.value.id for node in ast.walk(functions[0])
                                                             if isinstance(node, ast.Return)
                                                              and isinstance(node.value, ast.Attribute)
                                                              and isinstance(node.value.value, ast.Name)})
        versions = set()
        for filename in paths:
            path = (source / filename).resolve()
            if not path.is_relative_to(source.resolve()) or not path.is_file():
                continue
            try:
                module = ast.parse(_read_python_source(path))
            except (OSError, SyntaxError):
                continue
            for declaration in ast.walk(module):
                if isinstance(declaration, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id in {'version', '__version__', *attributes}
                        for target in declaration.targets):
                    try:
                        literal = ast.literal_eval(declaration.value)
                    except (ValueError, TypeError, SyntaxError):
                        continue
                    if isinstance(literal, str):
                        versions.add(literal)
        if len(versions) != 1:
            raise SourceMetadataError('Cannot identify one literal version in setup.py source inputs: ' + node.func.id)
        return versions.pop()
    def source_requirements(node):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            raise SourceMetadataError('Cannot resolve setup.py requirement helper: ' + ast.unparse(node))
        functions = [item for item in tree.body
                     if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == node.func.id]
        if len(functions) != 1:
            raise SourceMetadataError('Cannot identify setup.py requirement helper: ' + node.func.id)
        function = functions[0]
        parameters = {argument.arg: value_arg.value for argument, value_arg in
                      zip(function.args.args, node.args)
                      if isinstance(value_arg, ast.Constant) and isinstance(value_arg.value, str)}
        files = set()
        for call in ast.walk(function):
            if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
                    or call.func.id != 'open' or len(call.args) != 1):
                continue
            filename = call.args[0]
            if isinstance(filename, ast.Constant) and isinstance(filename.value, str):
                files.add(filename.value)
            elif isinstance(filename, ast.Name) and filename.id in parameters:
                files.add(parameters[filename.id])
        collectors, iterators, skips, returns = set(), set(), set(), set()
        for declaration in ast.walk(function):
            if isinstance(declaration, ast.For) and isinstance(declaration.target, ast.Name):
                iterator = declaration.iter
                readlines = (isinstance(iterator, ast.Call) and isinstance(iterator.func, ast.Attribute)
                             and iterator.func.attr == 'readlines' and not iterator.args)
                splitlines = (isinstance(iterator, ast.Call) and isinstance(iterator.func, ast.Attribute)
                              and iterator.func.attr == 'splitlines' and not iterator.args
                              and isinstance(iterator.func.value, ast.Call)
                              and isinstance(iterator.func.value.func, ast.Attribute)
                              and iterator.func.value.func.attr == 'read' and not iterator.func.value.args)
                if readlines or splitlines:
                    iterators.add(declaration.target.id)
                    for operation in ast.walk(declaration):
                        if (isinstance(operation, ast.Call) and isinstance(operation.func, ast.Attribute)
                                and operation.func.attr == 'append' and isinstance(operation.func.value, ast.Name)
                                and len(operation.args) == 1 and isinstance(operation.args[0], ast.Name)
                                and operation.args[0].id == declaration.target.id):
                            collectors.add(operation.func.value.id)
            if isinstance(declaration, ast.If) and any(
                    isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute)
                    and part.func.attr == 'startswith' and len(part.args) == 1
                    and isinstance(part.args[0], ast.Constant) and part.args[0].value == '#'
                    for part in ast.walk(declaration.test)):
                if any(isinstance(part, ast.Continue) for part in declaration.body):
                    skips.update(part.id for part in ast.walk(declaration.test) if isinstance(part, ast.Name))
                elif (isinstance(declaration.test, ast.UnaryOp) and isinstance(declaration.test.op, ast.Not)
                      and any(isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute)
                              and part.func.attr == 'append'
                              for stmt in declaration.body for part in ast.walk(stmt))):
                    skips.update(iterators)
            if isinstance(declaration, ast.Return) and isinstance(declaration.value, ast.Name):
                returns.add(declaration.value.id)
        if (len(files) != 1 or len(iterators) != 1 or len(collectors) != 1
                or collectors != returns or len(skips) != 1 or skips != iterators):
            raise SourceMetadataError('Unsupported setup.py requirement helper: ' + node.func.id)
        filename = files.pop()
        path = (source / filename).resolve()
        if not path.is_relative_to(source.resolve()) or not path.is_file():
            raise SourceMetadataError('Cannot resolve setup.py requirements file: ' + filename)
        return [line for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    def declarations(nodes, conditional=False):
        for node in nodes:
            if isinstance(node, ast.If):
                try:
                    take_body = condition(node.test)
                    chosen, skipped = (node.body, node.orelse) if take_body else (node.orelse, node.body)
                    for branch in skipped:
                        for operation in ast.walk(branch):
                            if (isinstance(operation, ast.Call) and isinstance(operation.func, ast.Attribute)
                                    and operation.func.attr == 'append' and isinstance(operation.func.value, ast.Name)):
                                evaluated_mutations.add(id(operation))
                    declarations(chosen, True)
                except SourceMetadataError:
                    pass
                continue
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id == 'exec'
                    and len(node.value.args) == 2 and isinstance(node.value.args[1], ast.Name)):
                try:
                    text = value(node.value.args[0])
                    namespace = variables.get(node.value.args[1].id)
                    if not isinstance(text, str) or not isinstance(namespace, dict):
                        continue
                    # setup.py commonly execs a local, declared version module into a
                    # dictionary. Read only literal assignments; never execute source.
                    for declaration in ast.parse(text).body:
                        targets = declaration.targets if isinstance(declaration, ast.Assign) else (
                            [declaration.target] if isinstance(declaration, ast.AnnAssign) else [])
                        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                            continue
                        expression = declaration.value
                        try:
                            namespace[targets[0].id] = ast.literal_eval(expression)
                        except (ValueError, TypeError, SyntaxError):
                            continue
                    evaluated_mutations.add(id(node.value))
                except (SourceMetadataError, SyntaxError):
                    pass
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == 'append'
                    and isinstance(node.value.func.value, ast.Name) and len(node.value.args) == 1):
                receiver = variables.get(node.value.func.value.id)
                if conditional and isinstance(receiver, list):
                    try:
                        receiver.append(value(node.value.args[0]))
                        evaluated_mutations.add(id(node.value))
                    except SourceMetadataError:
                        pass
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                module = source / node.module.replace('.', '/')
                module = module / '__init__.py' if module.is_dir() else module.with_suffix('.py')
                for alias in node.names:
                    try:
                        variables[alias.asname or alias.name] = imported_literal(module, alias.name)
                    except (SourceMetadataError, ValueError, SyntaxError):
                        pass
            if isinstance(node, ast.With):
                try:
                    for item in node.items:
                        if isinstance(item.optional_vars, ast.Name):
                            variables[item.optional_vars.id] = value(item.context_expr)
                    declarations(node.body, conditional)
                except SourceMetadataError:
                    pass
            elif isinstance(node, ast.Assign):
                try:
                    evaluated = value(node.value)
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            variables[target.id] = evaluated
                            evaluated_assignments.add(id(node))
                except SourceMetadataError:
                    pass
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                declarations(node.body, conditional)
    declarations(tree.body)
    def safe_mapping_changes(name, seen=()):
        if name in seen:
            return False
        for operation in ast.walk(tree):
            if isinstance(operation, ast.Assign):
                for target in operation.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == name:
                        if not isinstance(target.slice, ast.Constant) or not isinstance(target.slice.value, str) or target.slice.value in dependency_keys:
                            return False
            if isinstance(operation, ast.Call) and isinstance(operation.func, ast.Attribute) and isinstance(operation.func.value, ast.Name) and operation.func.value.id == name:
                if operation.func.attr in ('get', 'items', 'keys', 'values', 'copy'):
                    continue
                if operation.func.attr == 'setdefault' and operation.args and isinstance(operation.args[0], ast.Constant) and isinstance(operation.args[0].value, str) and operation.args[0].value not in dependency_keys:
                    continue
                if operation.func.attr == 'update' and len(operation.args) == 1 and isinstance(operation.args[0], ast.Name) and safe_mapping_changes(operation.args[0].id, (*seen, name)):
                    continue
                return False
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == 'setup')
                                          or (isinstance(node.func, ast.Attribute) and node.func.attr == 'setup')):
            for keyword in node.keywords:
                if keyword.arg in ('install_requires', 'setup_requires', 'extras_require') or keyword.arg is None:
                    references = {item.id for item in ast.walk(keyword.value) if isinstance(item, ast.Name)}
                    if keyword.arg is None:
                        for name in tuple(references):
                            if non_dependency_mapping(name):
                                references.remove(name)
                    for operation in ast.walk(tree):
                        if isinstance(operation, ast.Assign) and id(operation) not in evaluated_assignments:
                            for target in operation.targets:
                                if isinstance(target, ast.Name) and target.id in references:
                                    raise SourceMetadataError('Conditional or computed setup dependency assignment: ' + target.id)
                                if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id in references:
                                    if not isinstance(target.slice, ast.Constant) or target.slice.value in dependency_keys:
                                        raise SourceMetadataError('Mutated setup dependency mapping: ' + target.value.id)
                        if isinstance(operation, ast.Call) and isinstance(operation.func, ast.Attribute) and isinstance(operation.func.value, ast.Name) and operation.func.value.id in references:
                            if operation.func.attr in ('append', 'extend', 'insert', 'update', 'setdefault', 'pop', 'remove', 'clear'):
                                if id(operation) in evaluated_mutations:
                                    continue
                                if (operation.func.attr == 'append' and len(operation.args) == 1
                                        and conditional_operation(operation)):
                                    try:
                                        if isinstance(value(operation.args[0]), str):
                                            continue
                                    except SourceMetadataError:
                                        pass
                                if not safe_mapping_changes(operation.func.value.id):
                                    raise SourceMetadataError('Mutated setup dependency declaration: ' + operation.func.value.id)
                if keyword.arg in ('name', 'version', 'install_requires', 'setup_requires', 'extras_require', 'use_scm_version'):
                    if keyword.arg in ('name', 'version') and (source / 'PKG-INFO').is_file():
                        continue
                    try:
                        result[keyword.arg] = value(keyword.value)
                    except SourceMetadataError:
                        if keyword.arg != 'version':
                            raise
                        try:
                            result[keyword.arg] = source_version(keyword.value)
                        except SourceMetadataError:
                            result.pop('version', None)
                elif keyword.arg is None:
                    result.update(value(keyword.value))
    return result


def _digest_file(path, length):
    digest = hashlib.sha512() if length == 128 else hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def source_tree(cli, spec, expanded, directory, archive_cache=None, verified_cache=None, *,
                go_imports=False, meson_builds=False, libtool_builds=False, build_metadata_only=False):
    source0 = re.search(r'(?mi)^Source(?:0)?\s*:\s*(\S+)', expanded)
    if not source0:
        raise SourceMetadataError('Cannot identify Source0 archive for ' + spec.parent.name)
    url = source0[1]
    guard = re.search(r'(?m)^%global\s+source0_hash\s+([0-9a-f]{64})\s*$', spec.read_text())
    filename = Path(urlsplit(url).fragment.lstrip('/') or urlsplit(url).path).name
    archive = spec.parent / filename
    if not archive.is_file():
        if guard and verified_cache is not None:
            verified_cache.mkdir(parents=True, exist_ok=True)
            archive = verified_cache / (hashlib.sha256(url.encode()).hexdigest() + '-' + guard[1])
        elif archive_cache is not None:
            archive_cache.mkdir(parents=True, exist_ok=True)
            archive = archive_cache / hashlib.sha256(url.encode()).hexdigest()
        else:
            archive = directory / 'source.archive'
        if not url.startswith(('https://', 'http://')):
            raise SourceMetadataError('Source0 is missing: ' + filename)
        if not archive.is_file():
            with tempfile.NamedTemporaryFile(dir=archive.parent, prefix='.source-', delete=False) as handle:
                download = Path(handle.name)
            try:
                if not cli._fetch_source_archive(cli._genbr_http(), url, download):
                    raise SourceMetadataError('Could not download Source0: ' + url)
                if guard and _digest_file(download, 64) != guard[1]:
                    raise SourceMetadataError('Source0 does not match source0_hash for ' + spec.parent.name)
                download.replace(archive)
            finally:
                download.unlink(missing_ok=True)
    if guard and _digest_file(archive, 64) != guard[1]:
        raise SourceMetadataError('Source0 does not match source0_hash for ' + spec.parent.name)
    unpack = directory / 'source'
    unpack.mkdir()
    slim = None
    if build_metadata_only and guard and verified_cache is not None:
        key = hashlib.sha256()
        key.update(('7' + guard[1] + repr((go_imports, meson_builds, libtool_builds))).encode())
        prep_text = re.search(r'(?ms)^%prep\b.*?(?=^%generate_buildrequires\b|^%build\b|\Z)', expanded)
        key.update((prep_text[0] if prep_text else '').encode())
        for patch_name in sorted(set(re.findall(r'(?mi)^Patch\d*\s*:\s*(\S+)', expanded))):
            patch_file = spec.parent / Path(patch_name).name
            if patch_file.is_file():
                key.update(patch_file.read_bytes())
        slim = verified_cache / 'slim' / (guard[1][:16] + '-' + key.hexdigest()[:24])
    if slim is not None and slim.is_dir():
        shutil.copytree(slim, unpack, dirs_exist_ok=True)
    elif tarfile.is_tarfile(archive):
        decoder = None
        stream_context = tarfile.open(archive, mode='r|*' if build_metadata_only else 'r:*')
        try:
            with stream_context as stream:
                def source_filter(member, target):
                    try:
                        return tarfile.data_filter(member, target)
                    except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
                        return None
                if build_metadata_only:
                    basenames = {'CMakeLists.txt', 'meson.build', 'Makefile', 'Makefile.in', 'Makefile.am', 'Make.inc',
                                 'configure.ac', 'configure.in', 'VERSION', '.version', '.tarball-version',
                                 'old-configure.in', 'GUILE-VERSION', 'pyproject.toml', 'setup.py', 'setup.cfg', 'tox.ini',
                                 'PKG-INFO', 'Cargo.toml', 'Cargo.lock', 'pom.xml', 'DESCRIPTION', 'go.mod', 'go.sum',
                                 'requirements.txt', '__init__.py', '_version.py', 'version.py', '__version__.py',
                                 '__about__.py', '_about.py', 'lib.rs', 'main.rs', 'build.rs'}
                    prep_match = re.search(
                        r'(?ms)^%prep\b[^\n]*\n(.*?)(?=^%(?:build|install|check|clean|files|changelog)\b|\Z)',
                        expanded)
                    prep_inputs = set()
                    if prep_match:
                        prep_text = prep_match[1].replace('\\\n', ' ')
                        for line in prep_text.splitlines():
                            if not re.match(r'^\s*sed\b', line):
                                continue
                            try:
                                arguments = shlex.split(line, comments=True)
                            except ValueError:
                                continue
                            if not any(any(token in argument for token in
                                           ('pyproject', 'setup.', 'Cargo.toml', 'requirements', 'tox.ini'))
                                       for argument in arguments):
                                continue
                            index = 1
                            saw_script = False
                            while index < len(arguments):
                                argument = arguments[index]
                                if argument == '-e':
                                    index += 2
                                    saw_script = True
                                    continue
                                if argument in ('-i', '-r', '-E', '-n') or argument.startswith('-'):
                                    index += 1
                                    continue
                                if not saw_script:
                                    saw_script = True
                                elif re.fullmatch(r'[A-Za-z0-9_./+-]+', argument):
                                    prep_inputs.add(PurePosixPath(argument).as_posix())
                                index += 1
                    patch_paths = set()
                    for patch_name in re.findall(r'(?mi)^Patch\d*\s*:\s*(\S+)', expanded):
                        patch_path = spec.parent / Path(patch_name).name
                        if patch_path.is_file():
                            for target in re.findall(r'(?m)^(?:---|\+\+\+)\s+([^\t\n ]+)',
                                                     patch_path.read_text(errors='replace')):
                                if target != '/dev/null':
                                    components = PurePosixPath(target).parts
                                    # Unified diffs commonly carry an archive root
                                    # plus an .old/.new marker (for example
                                    # package-1.old/path). Source archives use the
                                    # unmarked root, so select by the patch target's
                                    # path below that marker.
                                    relative = list(components)
                                    if relative and (relative[0] in ('a', 'b') or
                                                     relative[0].endswith(('.old', '.new'))):
                                        relative.pop(0)
                                    if len(relative) > 1 and relative[0].endswith(('.old', '.new')):
                                        relative.pop(0)
                                    patch_paths.add(PurePosixPath(*relative).as_posix())
                                    if len(components) > 1:
                                        patch_paths.add(PurePosixPath(*components[1:]).as_posix())
                    for member in stream:
                        member_parts = PurePosixPath(member.name).parts
                        relative_name = PurePosixPath(*member_parts[1:]).as_posix() if len(member_parts) > 1 else member.name
                        member_name = Path(member.name).name
                        if (member_name in basenames or
                                member_name.endswith('.pc.in') or member_name.endswith('.pom') or
                                member_name.endswith('requirements.txt') or member_name.startswith('requirements') and member_name.endswith('.txt') or
                                (go_imports and member_name.endswith('.go')) or
                                relative_name in patch_paths or
                                any(fnmatch.fnmatchcase(relative_name, item) for item in prep_inputs)):
                            stream.extract(member, unpack, filter=source_filter)
                else:
                    stream.extractall(unpack, filter=source_filter)
        except Exception:
            if decoder is not None and decoder.poll() is None:
                decoder.kill()
                decoder.wait()
            raise
        if slim is not None and any(unpack.iterdir()):
            slim.parent.mkdir(parents=True, exist_ok=True)
            if slim.exists():
                shutil.rmtree(slim)
            shutil.copytree(unpack, slim)
    elif zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as stream:
            members = []
            for member in stream.infolist():
                target = (unpack / member.filename).resolve()
                if not target.is_relative_to(unpack.resolve()) or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise SourceMetadataError('Unsafe source archive member')
                member_name = Path(member.filename).name
                if not build_metadata_only or member_name in {
                        'CMakeLists.txt', 'meson.build', 'setup.py', 'setup.cfg', 'pyproject.toml',
                        'Cargo.toml', 'Cargo.lock', 'pom.xml', 'DESCRIPTION', 'PKG-INFO', 'tox.ini',
                        'requirements.txt', 'go.mod', 'go.sum'} or member_name.endswith(('.pom', '.pc.in')):
                    members.append(member)
            if members:
                stream.extractall(unpack, members)
        if slim is not None and any(unpack.iterdir()):
            slim.parent.mkdir(parents=True, exist_ok=True)
            if slim.exists():
                shutil.rmtree(slim)
            shutil.copytree(unpack, slim)
    else:
        raise SourceMetadataError('Source0 is not an archive')
    children = list(unpack.iterdir())
    source = children[0] if len(children) == 1 and children[0].is_dir() else unpack
    raw = spec.read_text()
    source_urls = {number or '0': item_url for number, item_url in re.findall(
        r'(?mi)^Source(\d*)\s*:\s*(\S+)', expanded)}
    source_numbers_by_filename = {
        Path(urlsplit(item_url).path).name: number
        for number, item_url in source_urls.items()
    }
    source_hashes = {number: digest for number, digest in re.findall(
        r'(?m)^%global\s+source(\d+)_hash\s+([0-9a-f]{64}|[0-9a-f]{128})\s*$', raw)}
    prep = re.search(r'(?ms)^%prep\b[^\n]*\n(.*?)(?=^%(?:build|install|check|clean|files|changelog)\b|\Z)', expanded)
    if prep:
        for match in re.finditer(
                r'(?m)^\s*tar\s+--(?:xz|bzip2|gzip)\s+-xf\s+(\S+)'
                r'\s+--directory=([A-Za-z0-9_.+-]+)\s+--strip-components=1\s*$', prep[1]):
            source_argument, target_name = match.groups()
            number = source_numbers_by_filename.get(Path(source_argument).name)
            if number is None or number not in source_hashes:
                continue
            target = (source / target_name).resolve()
            if not target.is_relative_to(source.resolve()):
                raise SourceMetadataError('Secondary source directory leaves the verified source tree')
            target.mkdir(parents=True, exist_ok=True)
            secondary_url = source_urls[number]
            secondary_name = Path(urlsplit(secondary_url).fragment.lstrip('/') or
                                  urlsplit(secondary_url).path).name
            secondary = spec.parent / secondary_name
            expected = source_hashes[number]
            if not secondary.is_file():
                cache = verified_cache if verified_cache is not None else archive_cache
                if cache is None:
                    cache = directory
                cache.mkdir(parents=True, exist_ok=True)
                secondary = cache / (hashlib.sha256(secondary_url.encode()).hexdigest() + '-' + expected)
                if not secondary.is_file():
                    with tempfile.NamedTemporaryFile(dir=cache, prefix='.source-', delete=False) as handle:
                        download = Path(handle.name)
                    try:
                        if not cli._fetch_source_archive(cli._genbr_http(), secondary_url, download):
                            raise SourceMetadataError('Could not download Source' + number + ': ' + secondary_url)
                        digest = _digest_file(download, len(expected))
                        if digest != expected:
                            raise SourceMetadataError('Source' + number + ' hash mismatch for ' + spec.parent.name)
                        download.replace(secondary)
                    finally:
                        download.unlink(missing_ok=True)
            digest = _digest_file(secondary, len(expected)) if not secondary.name.endswith(expected) else expected
            if digest != expected:
                raise SourceMetadataError('Source' + number + ' hash mismatch for ' + spec.parent.name)
            try:
                with tarfile.open(secondary) as stream:
                    def secondary_filter(member, extraction_target):
                        try:
                            return tarfile.data_filter(member, extraction_target)
                        except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
                            return None
                    members = []
                    for member in stream.getmembers():
                        components = PurePosixPath(member.name).parts
                        if len(components) < 2:
                            continue
                        member = member.replace(name=str(PurePosixPath(*components[1:])))
                        safe = secondary_filter(member, target)
                        if safe is not None:
                            members.append(safe)
                    stream.extractall(target, members=members)
            except (OSError, tarfile.TarError) as exc:
                raise SourceMetadataError('Invalid secondary source archive Source' + number) from exc
    patches = [(int(number or 0), item) for number, item in re.findall(
        r'(?mi)^Patch(\d*)\s*:\s*(\S+)', expanded)]
    strip = re.search(r'(?m)^%autosetup[^\n]*\s-p\s*(\d+)', raw)
    default_strip = int(strip[1]) if strip else 1
    prep = re.search(r'(?ms)^%prep\b[^\n]*\n(.*?)(?=^%(?:build|install|check|clean|files|changelog)\b|\Z)', raw)
    autopatch_strips = {}
    if prep:
        for line in prep[1].splitlines():
            if not re.match(r'^\s*%autopatch(?:\s|$)', line):
                continue
            try:
                arguments = shlex.split(line.strip())[1:]
            except ValueError:
                continue
            line_strip = default_strip
            selectors, positional, index = [], [], 0
            while index < len(arguments):
                argument = arguments[index]
                attached = re.fullmatch(r'-(p|m|M)(\d+)', argument)
                if attached:
                    option, value = attached[1], int(attached[2])
                    if option == 'p':
                        line_strip = value
                    else:
                        selectors.append((option, value))
                elif argument in ('-p', '-m', '-M') and index + 1 < len(arguments) and arguments[index + 1].isdigit():
                    index += 1
                    value = int(arguments[index])
                    if argument == '-p':
                        line_strip = value
                    else:
                        selectors.append((argument, value))
                elif argument.isdigit():
                    positional.append(int(argument))
                index += 1
            if selectors:
                lower = next((value for option, value in selectors if option == '-m'), None)
                upper = next((value for option, value in selectors if option == '-M'), None)
                if lower is not None or upper is not None:
                    first = lower if lower is not None else min(number for number, _ in patches)
                    last = upper if upper is not None else max(number for number, _ in patches)
                    positional.extend(range(first, last + 1))
            if not positional:
                positional = [number for number, _ in patches]
            for number in positional:
                autopatch_strips.setdefault(number, set()).add(line_strip)
    for number, item in patches:
        path = spec.parent / Path(item).name
        if not path.is_file():
            if not item.startswith(('https://', 'http://')):
                raise SourceMetadataError('Source patch is missing: ' + item)
            patch_cache = archive_cache / 'patches' if archive_cache is not None else directory / 'patches'
            patch_cache.mkdir(parents=True, exist_ok=True)
            path = patch_cache / hashlib.sha256(item.encode()).hexdigest()
            if not path.is_file() and not cli._fetch_source_archive(cli._genbr_http(), item, path):
                raise SourceMetadataError('Could not download declared patch: ' + item)
        patch_text = path.read_text(errors='replace')
        targets = re.findall(r'(?m)^\+\+\+\s+([^\t\n ]+)', patch_text)
        dependency_files = ('pyproject.toml', 'setup.py', 'setup.cfg', 'Cargo.toml', 'tox.ini', 'PKG-INFO')
        if not any(Path(target).name in dependency_files or 'requirements' in Path(target).name
                   or (go_imports and Path(target).suffix == '.go')
                   or (meson_builds and Path(target).name == 'meson.build')
                   or (libtool_builds and Path(target).name in ('Makefile.am', 'configure.ac', 'configure.in'))
                   for target in targets):
            continue
        for name in re.findall(r'(?m)^(?:---|\+\+\+)\s+([^\t\n ]+)', patch_text):
            if name != '/dev/null' and ('..' in Path(name).parts or name.startswith('/')):
                raise SourceMetadataError('Unsafe patch path: ' + name)
        strip_counts = autopatch_strips.get(number, {default_strip})
        if len(strip_counts) != 1:
            raise SourceMetadataError('Ambiguous %autopatch strip level for Patch' + str(number))
        strip_count = next(iter(strip_counts))
        kept = _present_patch_sections(patch_text, source, strip_count)
        if not kept:
            continue
        command = subprocess.run(['patch', '--batch', '--forward', '-p' + str(strip_count)],
                                 input=kept, cwd=source, capture_output=True, text=True, timeout=60)
        if command.returncode:
            raise SourceMetadataError('Could not apply ' + path.name + ': ' + command.stdout + command.stderr)
    prep = re.search(r'(?ms)^%prep\b(.*?)(?=^%generate_buildrequires\b|^%build\b|\Z)', expanded)
    if prep:
        commands = prep[1].replace('\\\n', ' ')
        for command in commands.splitlines():
            if command.strip().startswith('tomcli set '):
                _apply_tomcli(source, command)
                continue
            if not command.strip().startswith('sed '):
                continue
            if not any(token in command for token in ('pyproject', 'setup.', 'Cargo.toml', 'requirements', 'tox.ini')):
                continue
            arguments = shlex.split(command, comments=True)
            if not any(any(token in argument for token in ('pyproject', 'setup.', 'Cargo.toml', 'requirements', 'tox.ini'))
                       for argument in arguments):
                continue
            output = None
            if '>' in arguments:
                position = arguments.index('>')
                if len(arguments) != position + 2:
                    raise SourceMetadataError('Unsupported dependency-file redirection')
                output = (source / arguments[position + 1]).resolve()
                if not output.is_relative_to(source.resolve()):
                    raise SourceMetadataError('Dependency-file output leaves the source directory')
                arguments = arguments[:position]
            scripts, files, run_arguments = [], [], []
            index = 1
            while index < len(arguments):
                argument = arguments[index]
                if argument == '-e':
                    index += 1
                    scripts.append(arguments[index])
                    run_arguments.extend(arguments[index - 1:index + 1])
                elif argument in ('-i', '-E', '-r'):
                    run_arguments.append(argument)
                    pass
                elif argument.startswith('-') and len(argument) > 2 and argument[1] != '-' and all(char in 'irE' for char in argument[1:]):
                    for char in argument[1:]:
                        run_arguments.append('-' + char)
                elif argument.startswith('-'):
                    raise SourceMetadataError('Unsupported sed option: ' + argument)
                elif not scripts:
                    scripts.append(argument)
                    run_arguments.append(argument)
                else:
                    matches = sorted(glob.glob(str(source / argument)))
                    if not matches:
                        matches = [str(source / argument)]
                    relative_files = []
                    for match in matches:
                        target = Path(match).resolve()
                        if not target.is_relative_to(source.resolve()) or not target.is_file():
                            raise SourceMetadataError('Invalid dependency-file input: ' + argument)
                        relative_files.append(target.relative_to(source.resolve()).as_posix())
                    if not relative_files:
                        raise SourceMetadataError('Invalid dependency-file input: ' + argument)
                    files.extend(relative_files)
                    run_arguments.extend(relative_files)
                index += 1
            # Keep the sed program restricted to content transformations on source files.
            for script in scripts:
                addressed = re.fullmatch(r'/((?:\\.|[^/])*)/(s.+|g)', script)
                expression = addressed[2] if addressed else script
                deletion = re.split(r'(?<!\\)/', expression)
                valid = (len(deletion) == 3 and deletion[0] == '' and deletion[-1] == 'd') or bool(
                    re.fullmatch(r'[1-9][0-9]*d', expression))
                if addressed and addressed[1] and expression == 'g':
                    valid = True
                if expression.startswith('s') and len(expression) > 1 and not expression[1].isalnum():
                    parts = re.split(r'(?<!\\)' + re.escape(expression[1]), expression[1:])
                    valid = len(parts) == 4 and parts[0] == '' and bool(re.fullmatch(r'[gIp0-9]*', parts[-1]))
                if re.fullmatch(r'/(?:\\.|[^/])+/[ai][ ]?[^\n]+', script):
                    valid = True
                if not valid or '\n' in script:
                    raise SourceMetadataError('Unsupported dependency-file sed expression: ' + script)
            run = subprocess.run(['sed', *run_arguments], cwd=source, capture_output=True, text=True, timeout=60)
            if run.returncode:
                raise SourceMetadataError(run.stderr.strip())
            if output:
                output.write_text(run.stdout)
    return source


def _apply_tomcli(source, command):
    import tomllib
    arguments = shlex.split(command, comments=True)
    if len(arguments) >= 6 and arguments[1] == 'set' and arguments[3] == 'arrays' and arguments[4] == 'replace':
        _apply_tomcli_replace(source, arguments, command)
        return
    if len(arguments) >= 4 and arguments[1] in ('set', 'del') and arguments[3] != 'arrays':
        _apply_tomcli_edit(source, arguments)
        return
    if len(arguments) != 7 or arguments[3:5] != ['arrays', 'delitem']:
        raise SourceMetadataError('Unsupported tomcli command: ' + command.strip())
    path = (source / arguments[2]).resolve()
    if not path.is_relative_to(source.resolve()) or not path.is_file():
        raise SourceMetadataError('tomcli target is not in the source tree: ' + arguments[2])
    text = path.read_text()
    node = tomllib.loads(text)
    for part in _toml_key_parts(arguments[5]):
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, list):
        return
    for item in [item for item in node if isinstance(item, str) and re.fullmatch(arguments[6], item)]:
        pattern = re.compile(r'[ \t]*(?:"' + re.escape(item) + r'"|\'' + re.escape(item) + r'\')[ \t]*,?[ \t]*\n?')
        text = pattern.sub('', text, count=1)
    path.write_text(text)


def _toml_key_parts(key):
    parts = [item[1:-1] if item[:1] in '"\'' and item[-1:] == item[:1] else item
             for item in re.findall(r'"[^"]*"|\'[^\']*\'|[^.]+', key)]
    if not parts:
        raise SourceMetadataError('Unsupported tomcli key: ' + key)
    return parts


def _apply_tomcli_replace(source, arguments, command):
    import tomlkit
    if len(arguments) != 8:
        raise SourceMetadataError('Unsupported tomcli command: ' + command.strip())
    path = (source / arguments[2]).resolve()
    if not path.is_relative_to(source.resolve()) or not path.is_file():
        raise SourceMetadataError('tomcli target is not in the source tree: ' + arguments[2])
    document = tomlkit.parse(path.read_text())
    node = document
    for part in _toml_key_parts(arguments[5]):
        node = node.get(part) if hasattr(node, 'get') else None
        if node is None:
            return
    if not isinstance(node, list):
        return
    pattern = re.compile(arguments[6])
    for index, item in enumerate(list(node)):
        if isinstance(item, str) and pattern.fullmatch(item):
            node[index] = arguments[7]
    path.write_text(tomlkit.dumps(document))


def _apply_tomcli_edit(source, arguments):
    import tomlkit
    path = (source / arguments[2]).resolve()
    if not path.is_relative_to(source.resolve()) or not path.is_file():
        raise SourceMetadataError('tomcli target is not in the source tree: ' + arguments[2])
    if arguments[1] == 'set' and arguments[3] == 'del' and len(arguments) == 5:
        arguments = [arguments[0], 'del', arguments[2], arguments[4]]
    if arguments[1] == 'del':
        if len(arguments) != 4:
            raise SourceMetadataError('Unsupported tomcli command: ' + ' '.join(arguments))
        key, value = arguments[3], None
    else:
        kind = arguments[3]
        if kind in ('true', 'false') and len(arguments) == 5:
            key, value = arguments[4], kind == 'true'
        elif kind in ('str', 'int', 'float') and len(arguments) == 6:
            key = arguments[4]
            try:
                value = {'str': str, 'int': int, 'float': float}[kind](arguments[5])
            except ValueError as exc:
                raise SourceMetadataError('Unsupported tomcli value: ' + arguments[5]) from exc
        else:
            raise SourceMetadataError('Unsupported tomcli command: ' + ' '.join(arguments))
    document = tomlkit.parse(path.read_text())
    parts = _toml_key_parts(key)
    node = document
    for part in parts[:-1]:
        if arguments[1] == 'del':
            if part not in node:
                return
        elif part not in node:
            node[part] = tomlkit.table()
        node = node[part]
    if arguments[1] == 'del':
        if parts[-1] in node:
            del node[parts[-1]]
    else:
        node[parts[-1]] = value
    path.write_text(tomlkit.dumps(document))


def meson_shared_library_sonames(source, names):
    """Infer only literal Meson library SONAMEs from a verified source tree."""
    def calls(text, function):
        for match in re.finditer(r'(?m)^\s*(?:[A-Za-z_]\w*\s*=\s*)?' + function + r'\s*\(', text):
            start = match.end()
            depth = 1
            quote = None
            escaped = False
            for index in range(start, len(text)):
                char = text[index]
                if quote:
                    if escaped:
                        escaped = False
                    elif char == '\\':
                        escaped = True
                    elif char == quote:
                        quote = None
                elif char in "'\"":
                    quote = char
                elif char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                    if depth == 0:
                        yield text[start:index]
                        break

    def keyword(body, name):
        values = re.findall(r'(?m)^\s*' + name + r'\s*:\s*([^,\n]+)', body)
        return values[0].strip() if len(values) == 1 else None

    root_build = source / 'meson.build'
    if not root_build.is_file():
        return {}
    project_calls = list(calls(root_build.read_text(), 'project'))
    if len(project_calls) != 1:
        return {}
    project_version = keyword(project_calls[0], 'version')
    if project_version is not None:
        literal = re.fullmatch(r"['\"]([0-9]+(?:\.[0-9]+)*)['\"]", project_version)
        project_version = literal[1] if literal else None

    found = {}
    for build_file in source.rglob('meson.build'):
        for function in ('library', 'shared_library'):
            for body in calls(build_file.read_text(), function):
                name_match = re.match(r"\s*['\"]([A-Za-z0-9_+.-]+)['\"]\s*,", body)
                if not name_match or 'lib' + name_match[1] not in names:
                    continue
                soversion = keyword(body, 'soversion')
                version = keyword(body, 'version')
                if soversion is not None:
                    literal = re.fullmatch(r"(?:['\"]([0-9]+)['\"]|([0-9]+))", soversion)
                    major = literal[1] or literal[2] if literal else None
                elif version == 'meson.project_version()':
                    major = project_version.split('.', 1)[0] if project_version else None
                else:
                    literal = re.fullmatch(r"['\"]([0-9]+)(?:\.[0-9]+)*['\"]", version or '')
                    major = literal[1] if literal else None
                if major is not None:
                    found.setdefault('lib' + name_match[1], set()).add(major)
    return {name: name + '.so.' + next(iter(versions))
            for name, versions in found.items() if len(versions) == 1}


def libtool_shared_library_sonames(source, names):
    """Read shared-library SONAMEs from libtool and Automake source rules."""
    import fnmatch
    variables = {}
    soname_variables = {}
    metadata_files = [source / name for name in ('configure.ac', 'configure.in', 'VERSION', '.version', '.tarball-version', 'GUILE-VERSION')]
    metadata_files.extend(source.glob('*VERSION'))
    for path in metadata_files:
        if not path.is_file():
            continue
        text = path.read_bytes().decode('latin-1')
        for key, value in re.findall(r'(?m)^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*["\']?([0-9]+(?:\.[0-9]+)*)["\']?\s*$', text):
            variables[key] = value
        for key, value in re.findall(
                r'(?m)^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*["\']?(lib[A-Za-z0-9_+.-]+\.so\.[0-9]+)["\']?\s*$',
                text):
            soname_variables[key] = value
    found = {}
    for path in source.rglob('Makefile.am'):
        text = path.read_bytes().decode('latin-1')
        expanded = re.sub(r'@([A-Za-z_][A-Za-z_0-9]*)@', lambda match: variables.get(match[1], match[0]), text)
        expanded = re.sub(r'\\\n\s*', ' ', expanded)
        assignments = dict(re.findall(r'(?m)^\s*([A-Za-z_][A-Za-z_0-9.]*)\s*=\s*([^\n]+)', expanded))
        assignments.update(soname_variables)
        libraries = [value for key, value in assignments.items() if key.endswith('LTLIBRARIES')]
        declared = {library for line in libraries for library in re.findall(r'(?<!\S)([A-Za-z0-9_.+-]+)\.la(?:\s|$)', line)}
        for pattern in names:
            for library in declared:
                if not fnmatch.fnmatchcase(library, pattern.removesuffix('.la')):
                    continue
                if not library.startswith('lib'):
                    continue
                target = library.replace('-', '_') + '_la_LDFLAGS'
                line = assignments.get(target) or assignments.get('AM_LDFLAGS')
                if not line:
                    continue
                for _ in range(4):
                    line = re.sub(r'\$\(([A-Za-z_][A-Za-z_0-9]*)\)', lambda match: assignments.get(match[1], match[0]), line)
                match = re.search(r'-version-info\s+([^\s]+)', line)
                if not match:
                    continue
                value = re.sub(r'@([A-Za-z_][A-Za-z_0-9]*)@', lambda item: variables.get(item[1], item[0]), match[1])
                value = re.sub(r'\$\(([A-Za-z_][A-Za-z_0-9]*)\)', lambda item: assignments.get(item[1], item[0]), value)
                parts = value.split(':')
                if len(parts) == 3 and all(part.isdecimal() for part in parts):
                    current, revision, age = map(int, parts)
                    if current >= age:
                        found.setdefault('lib' + library[3:], set()).add(str(current - age))
        for line in expanded.splitlines():
            if 'ln -fs' not in line:
                continue
            substituted = line
            for _ in range(4):
                substituted = re.sub(r'\$\(([A-Za-z_][A-Za-z_0-9]*)\)',
                                     lambda match: assignments.get(match[1], variables.get(match[1], match[0])),
                                     substituted)
            for library, major in re.findall(r'\b(lib[A-Za-z0-9_+.-]+)\.so\.([0-9]+)(?:\s|$)', substituted):
                if any(fnmatch.fnmatchcase(library, pattern.removesuffix('.la').removesuffix('.so.*'))
                       for pattern in names):
                    found.setdefault(library, set()).add(major)
    makefiles = [path for path in source.rglob('*') if path.is_file()
                 and path.name in ('Makefile', 'Makefile.am', 'Make.inc')]
    make_variables = {'SHLIB_EXT': 'so'}
    version_file = source / 'VERSION'
    if version_file.is_file():
        make_variables['JULIA_VERSION'] = version_file.read_text().strip()
    assignments = {}
    for path in makefiles:
        for name, value in re.findall(
                r'(?m)^\s*([A-Za-z_][A-Za-z_0-9]*)\s*:?=\s*([^\n#]+)',
                path.read_text(errors='replace')):
            assignments.setdefault(name, []).append(value.strip())

    def resolve_make_variable(name, seen=()):
        if name in seen:
            return None
        choices = ([make_variables[name]] if name in make_variables
                   else list(reversed(assignments.get(name, ()))))
        for value in choices:
            result = resolve_make_expression(value.strip(), name, seen)
            if result is not None:
                return result
        return None

    def resolve_make_expression(value, name, seen):
        version_read = re.fullmatch(r'\$\(shell\s+cat\s+\$\(JULIAHOME\)/VERSION\)', value)
        if version_read:
            return make_variables.get('JULIA_VERSION')
        cut = re.fullmatch(
            r'\$\(shell\s+echo\s+\$\(([A-Za-z_][A-Za-z_0-9]*)\)'
            r'((?:\s*\|\s*cut\s+-d\'[^\']+\'\s+-f\s*\d+)+)\)', value)
        if cut:
            base = resolve_make_variable(cut[1], (*seen, name))
            if base is None:
                return None
            for delimiter, field in re.findall(r'\|\s*cut\s+-d\'([^\']+)\'\s+-f\s*(\d+)', cut[2]):
                fields = base.split(delimiter)
                if len(fields) < int(field):
                    return None
                base = fields[int(field) - 1]
            return base
        for _ in range(8):
            unresolved = re.search(r'\$\(([A-Za-z_][A-Za-z_0-9]*)\)', value)
            if not unresolved:
                return value.strip()
            replacement = resolve_make_variable(unresolved[1], (*seen, name))
            if replacement is None:
                return None
            value = value[:unresolved.start()] + replacement + value[unresolved.end():]
        return None

    for path in makefiles:
        for line in path.read_text(errors='replace').splitlines():
            for library, variable in re.findall(
                    r'SONAME_FLAGS\s*,\s*(lib[A-Za-z0-9_+.-]+)\.\$\(([A-Za-z_][A-Za-z_0-9]*)\)', line):
                if not any(fnmatch.fnmatchcase(library, pattern.removesuffix('.la').removesuffix('.so.*'))
                           for pattern in names):
                    continue
                suffix = resolve_make_variable(variable)
                match = re.fullmatch(r'so\.([0-9]+(?:\.[0-9]+)?)', suffix or '')
                if match:
                    found.setdefault(library, set()).add(match[1])
    return {library: library + '.so.' + next(iter(majors))
            for library, majors in found.items() if len(majors) == 1}


def cmake_shared_library_sonames(source, names):
    """Read explicit CMake target SONAME versions from the source tree."""
    def calls(text, function):
        for match in re.finditer(r'(?m)^\s*' + re.escape(function) + r'\s*\(', text):
            start, depth, quote, escaped = match.end(), 1, None, False
            for index in range(start, len(text)):
                char = text[index]
                if quote:
                    if escaped:
                        escaped = False
                    elif char == '\\':
                        escaped = True
                    elif char == quote:
                        quote = None
                elif char in "'\"":
                    quote = char
                elif char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                    if depth == 0:
                        yield text[start:index]
                        break

    files = list(source.rglob('CMakeLists.txt'))
    variables = {}
    prefixes = {}
    for path in files:
        text = path.read_text()
        for body in calls(text, 'set'):
            match = re.fullmatch(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s+["\']?([0-9]+)["\']?\s*', body)
            if match:
                variables[match[1]] = match[2]
        for body in calls(text, 'ecm_setup_version'):
            prefix = re.search(r'\bVARIABLE_PREFIX\s+([A-Za-z_][A-Za-z0-9_]*)', body)
            major = re.search(r'\bSOVERSION\s+([0-9]+)\b', body)
            if prefix and major:
                prefixes[prefix[1] + '_SOVERSION'] = major[1]
    variables.update(prefixes)
    found = {}
    for path in files:
        text = path.read_text()
        for function in ('set_target_properties', 'set_property'):
            for body in calls(text, function):
                try:
                    tokens = shlex.split(body.replace('\n', ' '), comments=True)
                except ValueError:
                    # CMake permits escaped quotes in unquoted arguments. Such
                    # calls are not useful for the literal SONAME properties
                    # handled below, and must not invalidate the rest of the
                    # source tree's metadata.
                    continue
                try:
                    if function == 'set_target_properties':
                        target = tokens[0]
                        properties = tokens[tokens.index('PROPERTIES') + 1:]
                        values = dict(zip(properties[::2], properties[1::2]))
                    else:
                        if tokens[:1] != ['TARGET']:
                            continue
                        target = tokens[1]
                        prop = tokens.index('PROPERTY')
                        values = {tokens[prop + 1]: tokens[prop + 2]}
                except (IndexError, ValueError):
                    continue
                if 'lib' + target not in names or 'SOVERSION' not in values:
                    continue
                major = values['SOVERSION']
                variable = re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', major)
                if variable:
                    major = variables.get(variable[1], '')
                if re.fullmatch(r'[0-9]+', major):
                    found.setdefault('lib' + target, set()).add(major)
    return {name: name + '.so.' + next(iter(versions))
            for name, versions in found.items() if len(versions) == 1}


def add_tex_source_provider(cli, spec, expanded, binaries, capability, archive_cache, target_binary_names=None):
    match = re.fullmatch(r'tex\(([A-Za-z0-9_+.-]+\.sty)\)', capability)
    if not match:
        return
    filename = match[1]
    source_name_match = re.search(r'(?mi)^Name\s*:\s*(\S+)', expanded)
    source_name = source_name_match[1] if source_name_match else spec.parent.name
    ownership = {}
    for section in re.finditer(r'(?ms)^%files([^\n]*)\n(.*?)(?=^%(?:files|changelog|pre|post|preun|postun|pretrans|posttrans)\b|\Z)', expanded):
        flags = section[1].split()
        if '-n' in flags:
            binary_name = flags[flags.index('-n') + 1]
        elif flags and not flags[0].startswith('-'):
            binary_name = source_name + '-' + flags[0]
        else:
            binary_name = source_name
        ownership[binary_name] = set(re.findall(r'(?m)^\S*(/tex/[^\s]+/)\s*$', section[2]))
    source_entries = []
    for item in re.finditer(r'(?mi)^Source(\d*)\s*:\s*(\S+)', expanded):
        number = item[1] or '0'
        url = item[2]
        if url.startswith('https://') and urlsplit(url).path.endswith('.tar.xz'):
            source_entries.append((number, url, Path(urlsplit(url).path).name.removesuffix('.tar.xz')))
    hashes = {number: digest for number, digest in re.findall(
        r'(?m)^%global\s+source(\d+)_hash\s+([0-9a-f]{64}|[0-9a-f]{128})\s*$', spec.read_text())}
    binaries_by_name = {item['name']: item for item in binaries}
    expected = 'texlive-' + filename.removesuffix('.sty')
    binary_names = []
    for name in binaries_by_name:
        if (not name.startswith('texlive-') or name not in ownership
                or (target_binary_names is not None and name not in target_binary_names)):
            continue
        # TeX Live source archives are split by TDS directory. Match the
        # archive to the declared /tex/<tree>/<package>/ ownership path.
        stem = name.removeprefix('texlive-')
        tds_paths = [directory.split('/tex/', 1)[1].strip('/').split('/')
                     for directory in ownership[name] if '/tex/' in directory]
        path_components = {component for path in tds_paths for component in path}
        top_level_trees = {path[0] for path in tds_paths if path}
        if (name == expected or (target_binary_names is not None and name in target_binary_names)
                or filename.removesuffix('.sty') in path_components
                or stem in top_level_trees):
            binary_names.append(name)
    binary_names.sort(key=lambda name: (name != expected, name))
    archive_cache = archive_cache or spec.parent
    archive_cache.mkdir(parents=True, exist_ok=True)
    verified_owners = set()
    for binary_name in binary_names:
        directories = ownership[binary_name]
        stem = binary_name.removeprefix('texlive-')
        urls = [(number, url) for number, url, archive_stem in source_entries if archive_stem == stem]
        if not urls:
            continue
        for number, url in urls:
            expected_hash = hashes.get(number)
            if expected_hash is None:
                continue
            archive = archive_cache / (hashlib.sha256(url.encode()).hexdigest() + '.tar.xz')
            if not archive.is_file():
                import httpx
                with httpx.Client(timeout=120.0, headers={'User-Agent': 'curl/8.15.0'}) as client:
                    if not cli._fetch_source_archive(client, url, archive):
                        raise SourceMetadataError('Could not download TeX source archive for ' + capability)
            digest = hashlib.sha512(archive.read_bytes()).hexdigest() if len(expected_hash) == 128 else hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest != expected_hash:
                raise SourceMetadataError('TeX source hash mismatch for ' + binary_name)
            try:
                if shutil.which('xz'):
                    decoder = subprocess.Popen(['xz', '-dc', str(archive)], stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE)
                    with tarfile.open(fileobj=decoder.stdout, mode='r|') as stream:
                        owners = [member.name for member in stream
                                  if member.isfile() and member.name.endswith('/' + filename)]
                    decoder.stdout.close()
                    stderr = decoder.stderr.read()
                    decoder.stderr.close()
                    if decoder.wait():
                        raise SourceMetadataError('Could not decompress TeX source archive: ' +
                                                  stderr.decode(errors='replace'))
                else:
                    with tarfile.open(archive, 'r|xz') as stream:
                        owners = [member.name for member in stream
                                  if member.isfile() and member.name.endswith('/' + filename)]
            except (OSError, tarfile.TarError) as exc:
                raise SourceMetadataError('Invalid TeX source archive for ' + capability) from exc
            def belongs_to_declared_directory(owner):
                normalized = owner.lstrip('./')
                tex_root = normalized.find('/tex/')
                if tex_root >= 0:
                    normalized = normalized[tex_root + 1:]
                if not normalized.startswith('tex/'):
                    return False
                return any(
                    normalized.startswith('tex/' + directory.split('/tex/', 1)[1])
                    for directory in directories if '/tex/' in directory
                )

            if any(belongs_to_declared_directory(owner) for owner in owners):
                verified_owners.add(binary_name)
    if len(verified_owners) > 1:
        raise SourceMetadataError('Multiple TeX source packages own ' + filename)
    for binary_name in verified_owners:
        binaries_by_name[binary_name]['provides'].append(capability)


def tox_metadata(source, environments):
    config = configparser.ConfigParser(interpolation=None)
    path = source / 'tox.ini'
    if not path.is_file():
        raise SourceMetadataError('Tox generator requested but tox.ini is missing')
    config.read(path)
    dependencies, extras = ['tox-current-env >= 0.0.16'], []
    if config.has_option('tox', 'minversion'):
        dependencies.append('tox >= ' + config.get('tox', 'minversion'))
    if config.has_option('tox', 'requires'):
        dependencies.extend(config.get('tox', 'requires').splitlines())
    for environment in environments:
        section = 'testenv:' + environment
        factors = set(environment.split('-'))
        def read(option, section=section, seen=()):
            key = section, option
            if key in seen:
                raise SourceMetadataError('Cyclic tox configuration reference')
            selected = section if config.has_option(section, option) else 'testenv'
            text = config.get(selected, option) if config.has_option(selected, option) else ''
            lines = []
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                reference = re.fullmatch(r'\{\[([^]]+)\]([^}]+)\}', line)
                if reference:
                    lines.extend(read(reference[2], reference[1], (*seen, key)))
                    continue
                condition = re.match(r'^([!A-Za-z0-9_,.-]+):\s+(.*)', line)
                if condition:
                    alternatives = condition[1].split(',')
                    if not any(all((factor[1:] not in factors) if factor.startswith('!') else factor in factors
                                   for factor in alternative.split('-')) for alternative in alternatives):
                        continue
                    line = condition[2]
                line = line.replace('{toxinidir}/', '').replace('{toxinidir}', '.')
                if '{' in line or '}' in line:
                    raise SourceMetadataError('Unresolved tox dependency substitution: ' + line)
                lines.append(line)
            return lines
        dependencies.extend(read('deps'))
        extras.extend(extra.strip() for line in read('extras') for extra in line.split(','))
    def expand_requirements(items, seen=()):
        expanded = []
        for item in items:
            line = item.strip()
            if not line or line.startswith('#'):
                continue
            include = re.fullmatch(r'(?:-r|--requirement)(?:\s+|=)(\S+)', line)
            if line.startswith('-r') and include is None:
                include = re.fullmatch(r'-r(\S+)', line)
            if include:
                filename = include[1]
                path = (source / filename).resolve()
                if not path.is_relative_to(source.resolve()) or not path.is_file():
                    raise SourceMetadataError('Missing tox requirements file: ' + filename)
                if path in seen:
                    raise SourceMetadataError('Cyclic tox requirements file: ' + filename)
                lines = []
                for raw in path.read_text().splitlines():
                    raw = raw.split(' #', 1)[0].strip()
                    if raw and not raw.startswith('#'):
                        lines.append(raw)
                expanded.extend(expand_requirements(lines, (*seen, path)))
                continue
            if line.startswith('-'):
                raise SourceMetadataError('Unsupported tox requirement directive: ' + line)
            expanded.append(line)
        return expanded
    return expand_requirements(dependencies), list(dict.fromkeys(extras))


def dependency_group(data, name, seen=()):
    if name in seen:
        raise SourceMetadataError('Dependency group cycle: ' + name)
    groups = data.get('dependency-groups', {})
    if name not in groups:
        raise SourceMetadataError('Missing dependency group: ' + name)
    result = []
    for entry in groups[name]:
        if isinstance(entry, str):
            result.append(entry)
        elif isinstance(entry, dict) and set(entry) == {'include-group'}:
            result.extend(dependency_group(data, entry['include-group'], (*seen, name)))
        else:
            raise SourceMetadataError('Invalid dependency group entry: ' + str(entry))
    return result


def poetry_constraint(value):
    if not isinstance(value, str) or not value.strip():
        raise SourceMetadataError('Poetry dependency has no version constraint')
    value = value.strip()
    if value == '*':
        return ''
    if value.startswith('^') or value.startswith('~') and not value.startswith('~='):
        version = Version(value[1:])
        release = list(version.release)
        position = (next((i for i, part in enumerate(release) if part), len(release) - 1)
                    if value[0] == '^' else min(1, len(release) - 1))
        upper = release[:position + 1]
        upper[-1] += 1
        return '>=' + str(version) + ',<' + '.'.join(map(str, upper))
    if value[0].isdigit():
        value = '==' + value
    SpecifierSet(value)
    return value


def poetry_project(poetry, environment):
    required, declared = [], {}
    for name, declaration in poetry.get('dependencies', {}).items():
        if name == 'python':
            continue
        options = {'version': declaration} if isinstance(declaration, str) else declaration
        if not isinstance(options, dict) or set(options) - {'version', 'optional', 'extras', 'markers', 'python', 'platform'}:
            raise SourceMetadataError('Unsupported Poetry dependency declaration: ' + name)
        if 'python' in options and not SpecifierSet(poetry_constraint(options['python'])).contains(environment['python_full_version']):
            continue
        if 'platform' in options and options['platform'] != environment['sys_platform']:
            continue
        dependency = name
        if options.get('extras'):
            dependency += '[' + ','.join(options['extras']) + ']'
        dependency += poetry_constraint(options.get('version', '*'))
        if options.get('markers'):
            dependency += '; ' + options['markers']
        Requirement(dependency)
        declared[canonicalize_name(name)] = dependency
        if not options.get('optional'):
            required.append(dependency)
    optional = {}
    for extra, names in poetry.get('extras', {}).items():
        missing = [name for name in names if canonicalize_name(name) not in declared]
        if missing:
            raise SourceMetadataError('Poetry extra refers to undeclared dependencies: ' + ', '.join(missing))
        optional[extra] = [declared[canonicalize_name(name)] for name in names]
    return {'name': poetry.get('name'), 'version': poetry.get('version'),
            'dependencies': required, 'optional-dependencies': optional}


def python_metadata(source, command, environment, toxenv=None, scm_version=None):
    words = shlex.split(command)
    if words and words[-1] == '>&2':
        words.pop()
    script_directory = Path('/usr/lib/rpm/redhat')
    script = script_directory / 'pyproject_buildrequires.py'
    if not script.is_file():
        raise SourceMetadataError('Install pyproject-rpm-macros to parse generator arguments')
    module_spec = importlib.util.spec_from_file_location('orbscli_pyproject_buildrequires', script)
    module = importlib.util.module_from_spec(module_spec)
    sys.path.insert(0, str(script_directory))
    try:
        module_spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    arguments = words if '--output' in words else [*words, '--output', '/dev/null']
    errors = __import__('io').StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            options = module.argparser().parse_args(arguments)
    except SystemExit as exc:
        raise SourceMetadataError(errors.getvalue().strip()) from exc
    if options.config_settings:
        raise SourceMetadataError('Backend config settings need declared dependency metadata: ' + str(options.config_settings))
    pkgversion = options.python3_pkgversion
    environment = dict(environment)
    versions = environment.pop('_repository_python_versions', None)
    if versions is not None:
        version = versions.get(pkgversion)
        if version is None:
            raise SourceMetadataError('Repository has no target python' + pkgversion + ' interpreter')
        environment.update(python_version='.'.join(version.split('.')[:2]), python_full_version=version)
    elif pkgversion != '3' and environment['python_version'] != pkgversion:
        raise SourceMetadataError('Target Python version does not match python' + pkgversion)
    extras = [canonicalize_name(extra) for group in (options.extras or []) for extra in group.split(',')]
    project_file = source / 'pyproject.toml'
    data = tomllib.loads(project_file.read_text(encoding='utf-8-sig')) if project_file.is_file() else {}
    project = data.get('project') or {}
    if not project and data.get('tool', {}).get('poetry') and data.get('build-system', {}).get('build-backend') == 'poetry.core.masonry.api':
        project = poetry_project(data['tool']['poetry'], environment)
    hatch_requirements = (data.get('tool', {}).get('hatch', {}).get('metadata', {}).get('hooks', {})
                          .get('requirements_txt', {}))
    if isinstance(hatch_requirements, dict):
        def read_hatch_requirement_files(files):
            if isinstance(files, str):
                files = [files]
            if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
                raise SourceMetadataError('Hatch requirements hook files must be a list of paths')
            declared = []
            for filename in files:
                path = (source / filename).resolve()
                if not path.is_relative_to(source.resolve()) or not path.is_file():
                    raise SourceMetadataError('Missing or unsafe Hatch requirements file: ' + filename)
                for raw_line in path.read_text().splitlines():
                    line = raw_line.split(' #', 1)[0].strip()
                    if not line or line.startswith('#') or line.startswith('--'):
                        continue
                    if ' --' in line:
                        line = line.split(' --', 1)[0].rstrip()
                    try:
                        Requirement(line)
                    except InvalidRequirement as exc:
                        raise SourceMetadataError('Unsupported Hatch requirement: ' + raw_line) from exc
                    declared.append(line)
            return declared

        dynamic_fields = set(project.get('dynamic', ()))
        if 'dependencies' in dynamic_fields and 'dependencies' not in project:
            project['dependencies'] = read_hatch_requirement_files(hatch_requirements.get('files', []))
        if 'optional-dependencies' in dynamic_fields and 'optional-dependencies' not in project:
            optional_files = hatch_requirements.get('optional-dependencies', {})
            if not isinstance(optional_files, dict):
                raise SourceMetadataError('Hatch optional dependency files must be a mapping')
            project['optional-dependencies'] = {
                extra: read_hatch_requirement_files(files)
                for extra, files in optional_files.items()
            }
    setuptools_dynamic = data.get('tool', {}).get('setuptools', {}).get('dynamic', {})
    if 'dependencies' in project.get('dynamic', ()) and 'dependencies' in setuptools_dynamic:
        files = setuptools_dynamic['dependencies'].get('file')
        files = [files] if isinstance(files, str) else files
        if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files):
            raise SourceMetadataError('Invalid setuptools dynamic dependency files')
        declared = []
        for item in files:
            path = (source / item).resolve()
            if not path.is_relative_to(source.resolve()) or not path.is_file():
                raise SourceMetadataError('Invalid setuptools dynamic dependency file: ' + item)
            for line in path.read_text().splitlines():
                line = re.split(r'\s+#', line.strip(), maxsplit=1)[0].strip()
                if line and not line.startswith('#'):
                    Requirement(line)
                    declared.append(line)
        project['dependencies'] = declared
    if 'version' in project.get('dynamic', ()) and 'version' in setuptools_dynamic:
        attr = setuptools_dynamic['version'].get('attr')
        if not isinstance(attr, str) or not all(part.isidentifier() for part in attr.split('.')) or '.' not in attr:
            raise SourceMetadataError('Invalid setuptools dynamic version attribute')
        *module, attribute = attr.split('.')
        relative = Path(*module)
        setuptools_table = data.get('tool', {}).get('setuptools', {})
        extra_bases = []
        package_dir = setuptools_table.get('package-dir', {})
        if isinstance(package_dir, dict):
            extra_bases += [source / value for key, value in package_dir.items()
                            if isinstance(value, str) and key in ('', module[0])]
        where = setuptools_table.get('packages', {}).get('find', {}).get('where', []) if isinstance(
            setuptools_table.get('packages'), dict) else []
        extra_bases += [source / item for item in where if isinstance(item, str)]
        bases = list(dict.fromkeys([source, source / 'src', source / 'lib', *extra_bases]))
        bases = [base for base in bases if base.resolve().is_relative_to(source.resolve())]
        candidates = [base / relative.with_suffix('.py') for base in bases]
        candidates += [base / relative / '__init__.py' for base in bases]
        candidates = [path for path in dict.fromkeys(candidates) if path.is_file()]
        if len(candidates) != 1:
            raise SourceMetadataError('Cannot locate setuptools dynamic version attribute: ' + attr)
        module_tree = ast.parse(_read_python_source(candidates[0]))
        constants, values = {}, []
        for declaration in module_tree.body:
            if not isinstance(declaration, ast.Assign):
                continue
            try:
                literal = _safe_constant(declaration.value, constants)
                resolved = True
            except (ValueError, TypeError, SyntaxError, KeyError, IndexError):
                literal, resolved = None, False
            for target in declaration.targets:
                if not isinstance(target, ast.Name):
                    continue
                if resolved:
                    constants[target.id] = literal
                else:
                    constants.pop(target.id, None)
                if target.id == attribute:
                    values.append(literal if resolved else None)
        if len(values) != 1 or not isinstance(values[0], str):
            raise SourceMetadataError('Setuptools dynamic version is not a unique string literal: ' + attr)
        project['version'] = values[0]
    metadata_path = source / 'PKG-INFO'
    metadata_message = (email.parser.Parser().parsestr(metadata_path.read_text())
                       if metadata_path.is_file() else None)
    metadata_dynamic = {item.lower() for item in metadata_message.get_all('Dynamic', [])} if metadata_message else set()
    resolved_source_metadata = bool(metadata_message and metadata_message.get('Name')
                                    and metadata_message.get('Version')
                                    and 'requires-dist' not in metadata_dynamic
                                    and 'dependencies' not in project
                                    and 'optional-dependencies' not in project
                                    and not (project and set(project.get('dynamic', ()))
                                             & {'dependencies', 'optional-dependencies'}))
    setup = (setup_metadata(source, environment)
             if (not project or set(project.get('dynamic', ())) & {'dependencies', 'optional-dependencies'})
             and not resolved_source_metadata else {})
    build = data.get('build-system')
    if build is None:
        # This is the PEP 517 legacy setuptools mode used by the RPM generator.
        build = {'requires': ['setuptools >= 40.8.0'], 'build-backend': 'setuptools.build_meta:__legacy__'}
    if 'requires' not in build:
        raise SourceMetadataError('Python build-system has no requires declaration')
    if not isinstance(build['requires'], list) or not all(isinstance(item, str) for item in build['requires']):
        raise SourceMetadataError('Python build-system requires must be a list of dependency strings')
    dependencies = list(build['requires']) if options.use_build_system else []
    setup_requires = setup.get('setup_requires', ())
    if isinstance(setup_requires, str):
        setup_requires = [line.strip() for line in setup_requires.splitlines() if line.strip() and not line.strip().startswith('#')]
    dependencies += list(setup_requires)
    for groups in options.dependency_groups or []:
        for group in groups.split(','):
            dependencies.extend(dependency_group(data, group))
    if options.tox or options.toxenv:
        environments = [environment for group in (options.toxenv or []) for environment in group.split(',')]
        if not environments:
            if not toxenv:
                raise SourceMetadataError('Tox environment is not defined by the expanded spec')
            environments = toxenv.split(',')
        tox_dependencies, tox_extras = tox_metadata(source, environments)
        dependencies.extend(tox_dependencies)
        extras.extend(canonicalize_name(extra) for extra in tox_extras)
    runtime = options.runtime and options.use_build_system
    requirements = project.get('dependencies', setup.get('install_requires', ()))
    optional = project.get('optional-dependencies', setup.get('extras_require', {}))
    if resolved_source_metadata:
        requirements = metadata_message.get_all('Requires-Dist', [])
        optional = {canonicalize_name(extra): []
                    for extra in metadata_message.get_all('Provides-Extra', [])}
    if isinstance(requirements, str):
        requirements = [line.strip() for line in requirements.splitlines() if line.strip() and not line.strip().startswith('#')]
    if isinstance(optional, dict):
        optional = {extra: [items] if isinstance(items, str) else items
                    for extra, items in optional.items()}
    if not isinstance(requirements, (list, tuple)) or not all(isinstance(item, str) for item in requirements):
        raise SourceMetadataError('Python dependencies must be a list of dependency strings')
    if not isinstance(optional, dict) or any(not isinstance(items, (list, tuple)) or not all(isinstance(item, str) for item in items) for items in optional.values()):
        raise SourceMetadataError('Python extras must contain lists of dependency strings')
    optional = {canonicalize_name(extra): dependencies for extra, dependencies in optional.items()}
    if runtime:
        dependencies.extend(requirements)
        for extra in extras:
            if extra not in optional:
                provided = {canonicalize_name(item) for item in (metadata_message.get_all('Provides-Extra', []) if metadata_message else [])}
                if extra not in provided:
                    raise SourceMetadataError('Requested Python extra has no metadata: ' + extra)
                for line in metadata_message.get_all('Requires-Dist', []):
                    marker = re.search(r"extra\s*==\s*['\"]([^'\"]+)['\"]", line)
                    if marker and canonicalize_name(marker[1]) == extra:
                        dependencies.append(line)
                continue
            dependencies.extend(optional[extra])
    # Source distributions publish fully resolved metadata, including dependencies
    # which setup.cfg stores indirectly. Prefer it when available.
    resolved_dependencies = 'install_requires' in setup or 'dependencies' in project
    if metadata_message:
        message = metadata_message
        if runtime and 'dependencies' not in project and 'install_requires' not in setup and message.get_all('Requires-Dist') and 'requires-dist' not in {item.lower() for item in message.get_all('Dynamic', [])}:
            requirements = message.get_all('Requires-Dist')
            dependencies.extend(requirements)
            resolved_dependencies = True
        name = project.get('name') or setup.get('name') or message.get('Name')
        version = project.get('version') or setup.get('version') or message.get('Version')
    else:
        name, version = project.get('name', setup.get('name')), project.get('version', setup.get('version'))
    if runtime and 'dependencies' in project.get('dynamic', ()) and not resolved_dependencies:
        raise SourceMetadataError('Dynamic Python dependencies have no resolved source metadata')
    if not name:
        raise SourceMetadataError('Python source has no project identity')
    if scm_version and (setup.get('use_scm_version') or 'version' in project.get('dynamic', ()) and (
            data.get('tool', {}).get('hatch', {}).get('version', {}).get('source') == 'vcs'
            or 'setuptools_scm' in data.get('tool', {}))):
        version = scm_version
    if version is None and 'version' in project.get('dynamic', ()):
        version_file = source / 'VERSION'
        templates = [source / name for name in ('setup.py.in', 'version.py.in')]
        if version_file.is_file() and any(template.is_file() and '@VERSION@' in template.read_text()
                                           for template in templates):
            declared = version_file.read_text().splitlines()[0].strip()
            try:
                Version(declared)
            except InvalidVersion:
                pass
            else:
                version = declared
    if version is None and 'version' in project.get('dynamic', ()) and (str(build.get('build-backend', '')).startswith('flit_core.') or build.get('build-backend') == 'sphinx_theme_builder'):
        module = data.get('tool', {}).get('flit', {}).get('module', {}).get('name', name.replace('-', '_'))
        relative = Path(*module.split('.'))
        candidates = [source / relative / '__init__.py', source / relative.with_suffix('.py'),
                      source / 'src' / relative / '__init__.py', source / 'src' / relative.with_suffix('.py')]
        candidates = [candidate for candidate in candidates if candidate.is_file()]
        if len(candidates) != 1:
            raise SourceMetadataError('Cannot identify the declared Flit version module')
        for node in ast.parse(_read_python_source(candidates[0])).body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets):
                version = ast.literal_eval(node.value)
                break
        if version is None:
            raise SourceMetadataError('Flit version is not a literal source declaration')
    result = ['pyproject-rpm-macros', 'python' + pkgversion + '-devel', 'python' + pkgversion + 'dist(packaging)', 'python' + pkgversion + 'dist(pip) >= 19']
    if project_file.is_file():
        result.append('(python' + pkgversion + 'dist(tomli) if python' + pkgversion + '-devel < 3.11)')
    else:
        result.append('python' + pkgversion + 'dist(setuptools) >= 40.8')
    for dependency in dependencies:
        if str(dependency).strip():
            result.extend(python_requirement(str(dependency), environment, extras, pkgversion))
    # Requirement files are positional arguments after the generator's options.
    for filename in options.requirement_files:
        word = str(filename)
        if word:
            path = source / filename
            if not path.resolve().is_relative_to(source.resolve()) or not path.is_file():
                raise SourceMetadataError('Missing requirements file: ' + word)
            for line in path.read_text().splitlines():
                line = line.split(' #', 1)[0].strip()
                if line and not line.startswith('#'):
                    if line.startswith('-'):
                        raise SourceMetadataError('Unsupported requirements-file directive: ' + line)
                    result.extend(python_requirement(line, environment, extras, pkgversion))
    return list(dict.fromkeys(result)), {'name': canonicalize_name(name), 'version': version,
                                          'requirements': requirements, 'extras': optional, 'pkgversion': pkgversion,
                                          'environment': environment}


def add_python_source_provides(source, expanded, binaries, environment, spec_text=None):
    if not re.search(r'(?m)^%(?:pyproject_install|py3_install)\b', spec_text or expanded):
        return
    _, project = python_metadata(source, '', environment)
    prefix = 'python' + project['pkgversion'] + '-'
    def advertises_project(binary):
        for provide in binary.get('provides', ()):
            match = re.match(r'^python(?:3(?:\.\d+)?)?dist\(([^)]+)\)', provide)
            if match and canonicalize_name(match[1]) == project['name']:
                return True
        return False

    named_matches = [binary for binary in binaries
                     if binary['name'].startswith(prefix)
                     and '+' not in binary['name'][len(prefix):]
                     and canonicalize_name(binary['name'][len(prefix):]) == project['name']]
    if len(named_matches) == 1:
        matches = named_matches
    else:
        matches = [binary for binary in binaries if advertises_project(binary)]
    if not matches:
        python_payload_owners = [binary for binary in binaries if any(
            re.search(r'/python[0-9.]+/site-packages/', provide)
            for provide in binary.get('provides', ()))]
        if len(python_payload_owners) == 1:
            matches = python_payload_owners
    if len(matches) != 1:
        raise SourceMetadataError('Cannot uniquely match Python project to its RPM binary: ' + project['name'])
    binary = matches[0]
    version = project['version'] or binary['version']
    binary['provides'].extend(python_requirement(project['name'] + ' == ' + version,
                                                 project['environment'],
                                                 pkgversion=project['pkgversion']))


def perl_providers(cli, spec, expanded, binaries, generator, archive_cache=None, verified_cache=None):
    with tempfile.TemporaryDirectory(prefix='orbscli-perl-metadata-') as temporary:
        source = source_tree(cli, spec, expanded, Path(temporary), archive_cache, verified_cache)
        sections = {}
        for section in re.finditer(r'(?ms)^%files([^\n]*)\n(.*?)(?=^%(?:files|changelog|pre|post|preun|postun)\b|\Z)', expanded):
            flags = section[1].split()
            source_name = re.search(r'(?mi)^Name\s*:\s*(\S+)', expanded)[1]
            name = flags[flags.index('-n') + 1] if '-n' in flags else source_name + '-' + flags[0] if flags and not flags[0].startswith('-') else source_name
            patterns = []
            for line in section[2].splitlines():
                if line.lstrip().startswith(('%doc', '%license', '%dir', '%exclude')):
                    continue
                for path in re.findall(r'/[^\s]+/vendor_perl/([^\s]*)', line):
                    patterns.append(path)
            sections[name] = patterns
        for module in source.rglob('*.pm'):
            relative = module.relative_to(source)
            if any(part in {'t', 'xt', 'test', 'tests', 'inc', 'examples', 'eg'} for part in relative.parts[:-1]):
                continue
            parts = list(relative.parts)
            while parts and parts[0] in ('lib', 'src'):
                parts.pop(0)
            installed_path = '/'.join(parts)
            owners = [binary for binary in binaries if any(fnmatch.fnmatch(installed_path, pattern)
                      or installed_path.startswith(pattern.rstrip('/') + '/')
                      for pattern in sections.get(binary['name'], ()))]
            if not owners:
                continue
            run = subprocess.run(['perl', str(generator), str(module)], capture_output=True, text=True, timeout=60)
            if run.returncode:
                raise SourceMetadataError(run.stderr.strip())
            provides = [line.strip() for line in run.stdout.splitlines() if line.strip()]
            for binary in owners:
                binary['provides'] = list(dict.fromkeys([*binary['provides'], *provides]))


def python_generator_source(source, commands, prep_aliases=None):
    directory = source
    stack = []
    aliases = prep_aliases or {}
    for line in commands.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if not words:
            continue
        if words[0] == 'popd':
            if stack:
                directory = stack.pop()
            continue
        if words[0] not in ('cd', 'pushd'):
            continue
        targets = [word for word in words[1:] if word not in ('>', '>>') and
                   not word.startswith(('>', '/dev/'))]
        if len(targets) != 1 or Path(targets[0]).is_absolute() or '$' in targets[0]:
            raise SourceMetadataError('Unsupported Python generator directory: ' + line)
        target = targets[0]
        alias = aliases.get(target)
        if alias is None:
            parts = PurePosixPath(target).parts
            if parts and parts[0] in aliases:
                alias = aliases[parts[0]].joinpath(*parts[1:])
        destination = Path(alias if alias is not None else directory / target).resolve()
        if not destination.is_relative_to(source.resolve()) or not destination.is_dir():
            raise SourceMetadataError('Python generator directory leaves the verified source: ' + line)
        if words[0] == 'pushd':
            stack.append(directory)
        directory = destination
    return directory


def python_prep_aliases(source, expanded):
    prep = re.search(r'(?ms)^%prep\b(.*?)(?=^%(?:generate_buildrequires|build|install|check|clean|files|changelog)\b|\Z)',
                     expanded)
    if not prep:
        return {}
    aliases = {source.name: source}
    for line in prep[1].splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if len(words) == 3 and words[0] == 'mv':
            origin, target = words[1:]
            if origin in aliases and Path(target).name == target and target not in ('.', '..'):
                aliases[target] = aliases[origin]
        elif len(words) == 4 and words[:2] == ['cp', '-al']:
            origin, target = words[2:]
            if origin in aliases and Path(target).name == target and target not in ('.', '..'):
                aliases[target] = aliases[origin]
    return aliases


def _generator_workdir(source, body, command_pattern):
    """Resolve the directory selected by cd commands before a generator."""
    source_root = source.resolve()
    working = source_root
    stack = []
    for line in body.splitlines():
        if re.search(command_pattern, line):
            break
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if words and words[0] == 'popd':
            if stack:
                working = stack.pop()
            continue
        if words and words[0] in ('cd', 'pushd'):
            args = [word for word in words[1:] if not word.startswith(('>', '/dev/', '2>'))]
            if len(args) != 1 or args[0].startswith('/'):
                raise SourceMetadataError('Unsupported dependency generator working directory')
            target = (working / args[0]).resolve()
            if not target.is_relative_to(source_root):
                raise SourceMetadataError('Dependency generator working directory leaves the verified source tree')
            if words[0] == 'pushd':
                stack.append(working)
            working = target
    if not working.is_dir():
        raise SourceMetadataError('Dependency generator working directory is missing from the verified source tree')
    return working


def _safe_constant(node, constants):
    """Evaluate a tiny side-effect free subset of Python constant expressions."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in constants:
            return constants[node.id]
        raise ValueError(node.id)
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_safe_constant(item, constants) for item in node.elts] if isinstance(node, ast.List) else tuple(
            _safe_constant(item, constants) for item in node.elts)
    if isinstance(node, ast.Dict):
        return {_safe_constant(key, constants): _safe_constant(value, constants)
                for key, value in zip(node.keys, node.values) if key is not None}
    if isinstance(node, ast.Set):
        return {_safe_constant(item, constants) for item in node.elts}
    if isinstance(node, ast.Subscript):
        value = _safe_constant(node.value, constants)
        index = node.slice
        if isinstance(index, ast.Slice):
            parts = [None if part is None else _safe_constant(part, constants)
                     for part in (index.lower, index.upper, index.step)]
            return value[slice(*parts)]
        return value[_safe_constant(index, constants)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _safe_constant(node.left, constants), _safe_constant(node.right, constants)
        if isinstance(left, (str, tuple, list)) and type(left) is type(right):
            return left + right
        raise ValueError('unsupported addition')
    if isinstance(node, ast.JoinedStr):
        out = ''
        for part in node.values:
            if isinstance(part, ast.Constant):
                out += str(part.value)
            elif isinstance(part, ast.FormattedValue) and part.format_spec is None and part.conversion == -1:
                out += str(_safe_constant(part.value, constants))
            else:
                raise ValueError('unsupported f-string')
        return out
    if isinstance(node, ast.Call) and not node.keywords:
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr == 'join' and len(node.args) == 1:
            separator = _safe_constant(function.value, constants)
            if isinstance(separator, str):
                return separator.join(_safe_constant(node.args[0], constants))
        if isinstance(function, ast.Name) and function.id == 'map' and len(node.args) == 2 and isinstance(
                node.args[0], ast.Name) and node.args[0].id == 'str':
            return [str(item) for item in _safe_constant(node.args[1], constants)]
        if isinstance(function, ast.Name) and function.id == 'str' and len(node.args) == 1:
            return str(_safe_constant(node.args[0], constants))
    raise ValueError('unsupported expression')


def evaluate(cli, spec, arch, expanded, binaries, python_version=None, archive_cache=None, verified_cache=None):
    sections = list(re.finditer(r'(?ms)^%generate_buildrequires[^\n]*\n(.*?)(?=^%(?:build|install|check|clean|files|changelog|conf|generate_buildrequires)\b|\Z)', expanded))
    if not sections:
        return []
    body = '\n'.join(section[1] for section in sections)
    cargo = re.search(r'(?m)^\s*(?:/usr/bin/)?cargo2rpm\s+([^\n]+)', body)
    python = re.search(r'(?m)^.*?pyproject_buildrequires\.py\s+([^\n]+)', body)
    go = re.search(r'(?m)^.*?\bgolist\s+([^\n]+)', body)
    r_buildrequires = re.search(r'(?m)^\s*%R_buildrequires\s*$', body)
    govl = re.search(r'(?m)^\s*(?:/usr/bin/)?go_vendor_license\s+([^\n]+)', body)
    generators = {
        'cargo': (cargo, r'(?m)^\s*(?:/usr/bin/)?cargo2rpm\s+[^\n]*\n?'),
        'python': (python, r'(?m)^.*?pyproject_buildrequires\.py\s+[^\n]*\n?'),
        'go': (go, r'(?m)^.*?\bgolist\s+[^\n]*\n?'),
        'r': (r_buildrequires, r'(?m)^\s*%R_buildrequires\s*\n?'),
        'go_vendor_license': (govl, r'(?m)^\s*(?:/usr/bin/)?go_vendor_license\s+[^\n]*\n?'),
    }
    active_generators = [name for name, (match, _pattern) in generators.items() if match]
    all_generators = (cargo, python, go, r_buildrequires, govl)
    if sum(bool(generator) for generator in all_generators) > 1:
        if len(active_generators) == sum(bool(generator) for generator in all_generators):
            requirements = []
            for selected in active_generators:
                isolated = expanded
                for name, (_match, pattern) in generators.items():
                    if name != selected:
                        isolated = re.sub(pattern, '', isolated)
                requirements.extend(evaluate(cli, spec, arch, isolated, binaries, python_version,
                                             archive_cache, verified_cache))
            return list(dict.fromkeys(requirements))
        raise SourceMetadataError('Multiple dependency generators need ordered evaluation for ' + spec.parent.name)
    if r_buildrequires:
        with tempfile.TemporaryDirectory(prefix='orbscli-r-metadata-') as temporary:
            source = source_tree(cli, spec, expanded, Path(temporary), archive_cache, verified_cache,
                                 build_metadata_only=True)
            descriptions = sorted(source.rglob('DESCRIPTION'),
                                  key=lambda path: (len(path.relative_to(source).parts), str(path)))
            if not descriptions:
                raise SourceMetadataError('Expected one DESCRIPTION file for R package ' + spec.parent.name)
            depth = len(descriptions[0].relative_to(source).parts)
            descriptions = [path for path in descriptions
                            if len(path.relative_to(source).parts) == depth]
            if len(descriptions) != 1:
                raise SourceMetadataError('Expected one package DESCRIPTION file for R package ' + spec.parent.name)
            fields = {}
            current = None
            for line in descriptions[0].read_text(errors='replace').splitlines():
                if line[:1].isspace() and current:
                    fields[current] += ' ' + line.strip()
                    continue
                key, sep, value = line.partition(':')
                if sep:
                    current = key.strip()
                    fields[current] = value.strip()
            r_spec = spec.parent.parent / 'R' / 'R.spec'
            base_packages = set(re.findall(r'(?m)^%add_submodule\s+([A-Za-z][A-Za-z0-9.]*)',
                                           r_spec.read_text())) if r_spec.is_file() else set()
            requirements = []
            for field in ('Depends', 'Imports', 'LinkingTo'):
                for dependency in fields.get(field, '').split(','):
                    match = re.match(r'\s*([A-Za-z][A-Za-z0-9.]*)', dependency)
                    if match and match[1] != 'R' and match[1] not in base_packages:
                        requirements.append('R-' + match[1])
            return list(dict.fromkeys(requirements))
    if govl and not cargo and not python and not go:
        if 'generate_buildrequires' not in shlex.split(govl[1]):
            raise SourceMetadataError('go_vendor_license without generate_buildrequires for ' + spec.parent.name)
        arguments = shlex.split(govl[1])
        config = None
        for index, value in enumerate(arguments[:-1]):
            if value in ('-c', '--config'):
                config = arguments[index + 1]
        with tempfile.TemporaryDirectory(prefix='orbscli-go-vendor-license-') as temporary:
            source = source_tree(cli, spec, expanded, Path(temporary), archive_cache, verified_cache,
                                 build_metadata_only=True)
            command = ['go_vendor_license']
            if config:
                conf = Path(config)
                conf_path = conf if conf.is_absolute() else spec.parent / conf.name
                if not conf_path.is_file():
                    raise SourceMetadataError('go_vendor_license config not found: ' + config)
                command += ['--config', str(conf_path)]
            command += ['generate_buildrequires']
            run = subprocess.run(command, cwd=source, capture_output=True, text=True, timeout=300)
            if run.returncode:
                raise SourceMetadataError(run.stderr.strip().splitlines()[-1][:200] if run.stderr.strip() else 'go_vendor_license failed')
            requirements = [line.strip() for line in run.stdout.splitlines() if line.strip()]
            if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', item) for item in requirements):
                raise SourceMetadataError('go_vendor_license returned an unsupported dependency for ' + spec.parent.name)
            return list(dict.fromkeys(requirements))
    if not cargo and not python and not go and not govl:
        requirements = []
        for line in body.splitlines():
            words = shlex.split(line, comments=True)
            if not words:
                continue
            if words[0] == 'echo' and len(words) > 1 and not any(word in ('>', '>>', '|', '&&') or '$' in word for word in words[1:]):
                requirements.append(' '.join(words[1:]))
            elif words[0] == 'printf' and len(words) > 2 and words[1] in ('%s\\n', '%s\n'):
                requirements.extend(words[2:])
            else:
                raise SourceMetadataError('Unrecognized dependency generator for ' + spec.parent.name + ': ' + line)
        return requirements
    with tempfile.TemporaryDirectory(prefix='orbscli-source-metadata-') as temporary:
        source = source_tree(cli, spec, expanded, Path(temporary), archive_cache, verified_cache,
                             go_imports=bool(go), build_metadata_only=True)
        if go:
            executable = shutil.which('golist')
            if not executable:
                raise SourceMetadataError('Install golist to resolve Go dependencies.')
            if not re.search(r'(?m)^%global\s+source0_hash\s+[0-9a-f]{64}\s*$', spec.read_text()):
                raise SourceMetadataError('Go dependency discovery requires a verified Source0 checksum')
            arguments = shlex.split(go[1])
            package_paths = [arguments[i + 1] for i, value in enumerate(arguments[:-1])
                             if value == '--package-path']
            package_paths.extend(value.split('=', 1)[1] for value in arguments
                                 if value.startswith('--package-path='))
            if len(package_paths) != 1 or any(part in ('', '.', '..') for part in package_paths[0].split('/')):
                raise SourceMetadataError('Go generator has no safe, unique package path')
            if any(value in (';', '|', '>', '&&') for value in arguments):
                raise SourceMetadataError('Unsupported Go generator command')
            if 'GO111MODULE=on' in go[0]:
                raise SourceMetadataError('Go module downloads are not supported during dependency planning')
            vendor = source / 'vendor'
            if vendor.is_symlink():
                raise SourceMetadataError('Go vendor directory is a symlink')
            if vendor.is_dir():
                shutil.rmtree(vendor)
            workspace = source / '_build'
            link = workspace / 'src' / package_paths[0]
            if link.exists() or link.is_symlink():
                raise SourceMetadataError('Go package path already exists in the verified source')
            if not link.resolve().is_relative_to(source.resolve()):
                raise SourceMetadataError('Go package path leaves the verified source')
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(source, target_is_directory=True)
            environment = dict(os.environ, GOPATH=str(workspace) + ':/usr/share/gocode',
                               GO111MODULE='off', GOPROXY='off', GOSUMDB='off', GOTOOLCHAIN='local')
            run = subprocess.run([executable, *arguments], cwd=link, env=environment,
                                 capture_output=True, text=True, timeout=120)
            if run.returncode:
                raise SourceMetadataError(run.stderr.strip() or run.stdout.strip() or 'golist failed')
            requirements = list(dict.fromkeys(line.strip() for line in run.stdout.splitlines() if line.strip()))
            if any(not re.fullmatch(r'golang\([A-Za-z0-9./_+~-]+\)', item) for item in requirements):
                raise SourceMetadataError('golist returned an unsupported dependency')
            return requirements
        if cargo:
            executable = shutil.which('cargo2rpm')
            if not executable:
                raise SourceMetadataError('Install cargo2rpm to resolve Cargo dependencies.')
            arguments = shlex.split(cargo[1])
            cargo_source = _generator_workdir(source, body, r'^\s*(?:/usr/bin/)?cargo2rpm\s+')
            for index, argument in enumerate(arguments):
                manifest = arguments[index + 1] if argument in ('--path', '-p') and index + 1 < len(arguments) else argument.split('=', 1)[1] if argument.startswith('--path=') else None
                if manifest is not None:
                    path = (cargo_source / manifest).resolve()
                    if not path.is_relative_to(source.resolve()) or not path.is_file():
                        raise SourceMetadataError('Cargo manifest leaves the verified source tree: ' + manifest)
            if any(word in (';', '|', '>', '&&') for word in arguments):
                raise SourceMetadataError('Unsupported Cargo generator command')
            def generate(arguments):
                run = subprocess.run([executable, *arguments], cwd=cargo_source, capture_output=True, text=True, timeout=120)
                if run.returncode:
                    raise SourceMetadataError(run.stderr.strip() or run.stdout.strip())
                return [line.strip() for line in run.stdout.splitlines() if line.strip()]
            _stub_cargo_targets(cargo_source)
            result = generate(arguments)
            for binary in binaries:
                if binary['name'].startswith('rust-') and binary['name'].endswith('-devel'):
                    name = binary['name']
                    for kind in ('provides', 'requires'):
                        binary[kind].extend(generate(['--path', 'Cargo.toml', kind, '--subpackage', '--feature', name]))
            return result
        if python_version is None:
            raise SourceMetadataError('Repository has no target Python version for ' + arch)
        environment = default_environment()
        environment.update(platform_machine=arch, sys_platform='linux', platform_system='Linux', os_name='posix')
        if isinstance(python_version, dict):
            environment['_repository_python_versions'] = python_version
        elif python_version:
            environment.update(python_version='.'.join(python_version.split('.')[:2]), python_full_version=python_version)
        toxenv = re.search(r'RPM_TOXENV=[\"\']?([^\s\"\']+)', body)
        scm = re.search(r"(?m)^\s*export SETUPTOOLS_SCM_PRETEND_VERSION=['\"]([^'\"]+)['\"]\s*$", body)
        python_source = python_generator_source(source, body[:python.start()],
                                                python_prep_aliases(source, expanded))
        generator_directories = re.findall(r'(?m)^\s*cd\s+([^\n]+)$', body[:python.start()])
        generator_variant = None
        if generator_directories:
            variant_words = shlex.split(generator_directories[-1], comments=True)
            if len(variant_words) == 1:
                generator_variant = Path(variant_words[0]).name
        requirements, project = python_metadata(python_source, python[1], environment, toxenv[1] if toxenv else None,
                                                scm[1] if scm else None)
        environment = project['environment']
        prefix = 'python' + project['pkgversion'] + '-'
        if project['version'] is None:
            declared_versions = {binary['version'] for binary in binaries if binary['name'].startswith(prefix)
                                 and canonicalize_name(binary['name'][len(prefix):].split('+', 1)[0]) == project['name']}
            if len(declared_versions) == 1:
                project['version'] = declared_versions.pop()
        if project['version']:
            prefix = 'python' + project['pkgversion'] + '-'
            variant_binaries = ([binary for binary in binaries
                                 if generator_variant and binary['name'].endswith('-' + generator_variant)]
                                if generator_variant else [])
            fallback_owners = [binary['name'] for binary in binaries if binary['name'].startswith(prefix)
                               and re.search(r'(?m)^%files\s+-n\s+' + re.escape(binary['name'])
                                             + r'\s+-f\s+\S*pyproject[-_]files\b', expanded)]
            for binary in binaries:
                name = binary['name']
                if variant_binaries and binary not in variant_binaries:
                    continue
                if not name.startswith(prefix):
                    continue
                package, _, extra = name[len(prefix):].partition('+')
                project_package = (package.removesuffix('-' + generator_variant)
                                   if generator_variant and package.endswith('-' + generator_variant)
                                   else package)
                if canonicalize_name(project_package) != project['name'] and not (len(fallback_owners) == 1 and name == fallback_owners[0]):
                    continue
                dist = project['name'] + ('[' + canonicalize_name(extra) + ']' if extra else '')
                provides = python_requirement(dist + ' == ' + project['version'], environment, pkgversion=project['pkgversion'])
                binary['provides'].extend(provides[-1:] if extra else provides)
                # RPM emits the interpreter-specific capability as well as the
                # default interpreter's major-version alias. Existing repository
                # RPMs require these exact minor-version capabilities.
                minor = environment['python_version']
                if minor != project['pkgversion']:
                    minor_provides = python_requirement(dist + ' == ' + project['version'], environment, pkgversion=minor)
                    binary['provides'].extend(minor_provides[-1:] if extra else minor_provides)
                binary['requires'].append('python(abi) = ' + minor)
                for dependency in project['requirements']:
                    binary['requires'].extend(python_requirement(dependency, environment, (extra,) if extra else (), project['pkgversion']))
                if extra:
                    for dependency in project['extras'].get(extra, ()):
                        binary['requires'].extend(python_requirement(dependency, environment, (extra,), project['pkgversion']))
        return requirements
