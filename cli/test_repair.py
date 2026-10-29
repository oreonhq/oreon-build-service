# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import tempfile
import unittest
from pathlib import Path
from click.testing import CliRunner
from cli import orbscli as o, repair


class RepairTests(unittest.TestCase):
    def test_prep_sed_failure_is_reported(self):
        log = ("sed: can't read pyproject.toml: No such file or directory\n"
               "error: Bad exit status from /var/tmp/rpm-tmp.example (%prep)\n")
        self.assertEqual(repair.failure_diagnostics(log),
                         ["sed: can't read pyproject.toml: No such file or directory"])

    def test_prep_cd_failure_is_reported(self):
        line = ('/var/tmp/rpm-tmp.example: line 42: cd: '
                'python-sphinx-inline-tabs-2025.12.21.14: No such file or directory')
        log = (line + '\nRPM build errors:\n'
               'error: Bad exit status from /var/tmp/rpm-tmp.example (%prep)\n'
               'ERROR: Command failed:\n')
        self.assertEqual(repair.failure_diagnostics(log), [line])

    def test_missing_local_source_is_reported(self):
        line = 'missing local files: furo-2025.12.19-vendor.tar.xz'
        self.assertEqual(repair.failure_diagnostics(line + '\nERROR: Command failed:\n'), [line])

    def test_incomplete_pytest_output_repairs_logging_without_changing_tests(self):
        log = ('## Binary RPM build\nExecuting(%check): shell\n'
               '[gw0] [ 19%] PASSED tests/unit/test_any.py::test_case\n'
               'tests/unit/test_any.py::test_par\n\n## Worker summary\nERROR: Command failed:\n')
        spec = ("Name: unrelated\n%check\n%pytest --verbose --numprocesses=auto --maxprocesses=4 "
                "-k 'not test_which' tests/unit tests/functional\n%files\n")
        updated, actions = repair.phase_repairs(spec, log)
        self.assertTrue(repair.incomplete_pytest_output(log))
        self.assertEqual(len(actions), 1)
        self.assertIn('-q -r fE --tb=short', updated)
        self.assertIn("--numprocesses=auto --maxprocesses=4 -k 'not test_which' tests/unit tests/functional", updated)
        self.assertNotIn('--verbose', updated)
        self.assertEqual(repair.phase_repairs(updated, log), (updated, []))

    def test_complete_pytest_result_is_not_a_logging_repair(self):
        log = ('## Binary RPM build\nExecuting(%check): shell\n'
               '[gw0] [ 100%] PASSED tests/unit/test_any.py::test_case\n'
               '================= 1 passed in 0.1s =================\n'
               'tests/unpackaged/file\n\n## Worker summary\nERROR: Command failed:\n')
        spec = 'Name: unrelated\n%check\n%pytest --verbose tests/unit\n'
        self.assertFalse(repair.incomplete_pytest_output(log))
        self.assertEqual(repair.phase_repairs(spec, log), (spec, []))

    def test_arbitrary_compiler_uses_the_reported_executable(self):
        log = 'Could not find the compiler specified in the environment variable CXX:\ncustom-tool++.\n'
        self.assertEqual(repair.build_requirements(log), [('/usr/bin/custom-tool++', 'Missing executable custom-tool++')])

    def test_configure_constraint_comes_from_the_failed_check_and_preserves_epoch(self):
        log = ('ERROR: feature requested but not found\n'
               'fatal error: windows.h: No such file or directory\n'
               'check_cpp_condition feature Vendor/Version.h (VENDOR_VERSION_MAJOR << 48 | VENDOR_VERSION_MINOR << 32 | VENDOR_VERSION_RELEASE << 16 | VENDOR_VERSION_BUILD_NUM) >= 0x2000300040000\n')
        self.assertEqual(repair.build_requirements(log + log, lambda kind, name: ('arbitrary-sdk-devel', '7')),
                         [('arbitrary-sdk-devel >= 7:2.3.4', 'Missing header Vendor/Version.h')])

    def test_owner_is_selected_from_expanded_paths_not_package_names(self):
        spec = 'Name: unrelated\n%files tests\n%{_libexecdir}/unrelated\n%files\n%{_libdir}/vendor/modules/\n'
        expanded = 'Name: unrelated\n%files tests\n/usr/libexec/unrelated\n%files\n/usr/lib64/vendor/modules/\n'
        log = 'Installed (but unpackaged) file(s) found:\n/usr/lib64/vendor/data/new.dat\n'
        revised, actions, manual = repair.exact_unpackaged(spec, log, o, expanded)
        main = revised.split('%files\n', 1)[1]
        self.assertIn('%{_libdir}/vendor/data/new.dat', main)
        self.assertEqual(len(actions), 1)
        self.assertFalse(manual)

    def test_implicit_build_and_prepend_steps_are_all_preserved(self):
        spec = ('Name: arbitrary\nBuildSystem: pyproject\nBuildOption(install): -l arbitrary\n'
                '%prep\n%autosetup\n%generate_buildrequires -p\nexport BUILD_VERSION=1\n'
                '%build -p\nexport BUILD_VERSION=1\n%check\n%pyproject_check_import\n')
        revised, _ = repair.phase_repairs(spec, 'Unknown tag: BuildSystem: pyproject')
        self.assertEqual(revised.count('%build\n'), 1)
        self.assertEqual(revised.count('%generate_buildrequires\n'), 1)
        self.assertIn('%build\nexport BUILD_VERSION=1\n%pyproject_wheel', revised)
        self.assertIn('%generate_buildrequires\nexport BUILD_VERSION=1\n%pyproject_buildrequires', revised)
        self.assertEqual(repair.phase_repairs(revised, 'Unknown tag: BuildSystem: pyproject')[0], revised)

    def test_chain113_dependency_diagnostics(self):
        log = "Exception: CMake must be installed to build from source.\nModuleNotFoundError: No module named 'docutils'"
        self.assertEqual({dep for dep, _ in repair.build_requirements(log, lambda kind, name: '/usr/bin/cmake' if kind == 'executable' else 'python3dist(docutils)')}, {'/usr/bin/cmake', 'python3dist(docutils)'})
        self.assertIn('Exception: CMake must be installed to build from source.', repair.failure_diagnostics(log))
        self.assertIn('ipsec algparse: invalid option -- \'t\'', repair.failure_diagnostics("ipsec algparse: invalid option -- 't'"))

    def test_import_check_dependency_is_required_at_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec = root / 'demo.spec'
            spec.write_text('Name: demo\nVersion: 1\n%package -n python3-demo\nSummary: Demo\n%description -n python3-demo\nDemo\n')
            log = root / 'demo.log'
            log.write_text("ModuleNotFoundError: No module named 'docutils'\nFailed to import: demo")
            plan = repair.plan_package('demo', spec, [log], o, resolve_artifact=lambda kind, name: 'python3dist(docutils)')
            self.assertIn('BuildRequires:  python3dist(docutils)', plan['updated_spec'])
            self.assertIn('%package -n python3-demo\nRequires:       python3dist(docutils)', plan['updated_spec'])
            repair.apply_plan(plan)
            self.assertTrue(repair.plan_package('demo', spec, [log], o, resolve_artifact=lambda kind, name: 'python3dist(docutils)')['already_applied'])

    def test_noarch_requires_pure_wheel_and_no_elf_evidence(self):
        spec = 'Name: demo\nVersion: 1\n'
        failure = 'error: Empty %files file /build/demo/debugsourcefiles.list\nExtracting debug info from 0 files\n'
        self.assertEqual(repair.phase_repairs(spec, failure)[0], spec)
        revised, actions = repair.phase_repairs(spec, failure + 'Created wheel: filename=demo-1-py3-none-any.whl')
        self.assertIn('BuildArch:      noarch', revised)
        self.assertEqual(len(actions), 1)
        self.assertEqual(repair.phase_repairs(revised, failure)[0], revised)

    def test_buildsystem_expansion_preserves_conditional_options(self):
        spec = ('Name: demo\nBuildSystem: pyproject\n%if %{without bootstrap}\n'
                'BuildOption(generate_buildrequires): -x docs\n%endif\nBuildOption(install): -l demo\n'
                '%prep\n%autosetup\n%conf\nsed -i s/old/new/ docs/conf.py\n'
                '%build -a\n%if %{without bootstrap}\nsphinx-build docs html\n%endif\n%check\n%pyproject_check_import\n')
        revised, actions = repair.phase_repairs(spec, 'Unknown tag: BuildSystem: pyproject')
        self.assertGreaterEqual(len(actions), 1)
        self.assertNotIn('BuildSystem:', revised)
        self.assertNotIn('BuildOption(', revised)
        self.assertNotIn('%conf\n', revised)
        self.assertIn('%build\n%pyproject_wheel\n', revised)
        self.assertIn('%if %{without bootstrap}\n%global orbs_pyproject_requires_options -x docs\n%endif', revised)
        self.assertIn('%pyproject_save_files %{?orbs_pyproject_files_options}', revised)

    def test_static_metadata_repair_does_not_disable_runtime_dependencies(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'source.tar'
            content = b'[project]\nname="demo"\nversion="1"\ndependencies=["packaging"]\n'
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo('demo-1/pyproject.toml')
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
            spec = '%global source0_hash none\nName: demo\n%generate_buildrequires\n%pyproject_buildrequires\n'
            revised, actions, manual = repair.verified_source_repairs(spec, 'build backend cannot provide build metadata', archive)
            self.assertIn('%pyproject_buildrequires -p', revised)
            self.assertNotIn('-R', revised)
            self.assertIn(hashlib.sha256(archive.read_bytes()).hexdigest(), revised)
            self.assertFalse(manual)
            self.assertEqual(len(actions), 1)

    def test_obsolete_patch_requires_all_existing_hunks_already_applied(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'source.tar'
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo('demo-1/demo.py')
                member.size = 4
                tar.addfile(member, io.BytesIO(b'new\n'))
            spec = '%global source0_hash none\nName: demo\nPatch0: demo.patch\n'
            applied = 'diff --git a/demo.py b/demo.py\n--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n-old\n+new\n'
            absent = 'diff --git a/tests/snapshot b/tests/snapshot\n--- a/tests/snapshot\n+++ b/tests/snapshot\n@@ -1 +1 @@\n-old\n+new\n'
            revised, actions = repair.redundant_patch_repairs(spec, 'No file to patch.', archive, {'demo.patch': applied})
            self.assertNotIn('Patch0:', revised)
            self.assertEqual(len(actions), 1)
            review = []
            self.assertEqual(repair.redundant_patch_repairs(spec, '1 hunk FAILED', archive, {'demo.patch': applied + absent}, review)[0], spec)
            self.assertTrue(review)
            pending = applied.replace('-old\n+new', '-new\n+future')
            self.assertEqual(repair.redundant_patch_repairs(spec, 'No file to patch.', archive, {'demo.patch': pending + absent})[0], spec)

    def test_patch_checks_failed_hunks_and_preserves_literal_changes(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'source.tar'
            source = b'if (!items.empty() &&\n    items[0] == value) {\n  use("a b");\n}\n'
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo('demo-1/demo.cpp')
                member.size = len(source)
                tar.addfile(member, io.BytesIO(source))
            spec = '%global source0_hash none\nName: unrelated\nPatch: demo.patch\n'
            patch = ('diff --git a/demo.cpp b/demo.cpp\n--- a/demo.cpp\n+++ b/demo.cpp\n'
                     '@@ -1 +1 @@\n-if (items[0] == value) {\n+if (!items.empty() && items[0] == value) {\n')
            review = []
            revised, actions = repair.redundant_patch_repairs(spec, '1 hunk FAILED', archive, {'demo.patch': patch}, review)
            self.assertNotIn('Patch:', revised)
            self.assertFalse(review)
            changed_literal = patch + '@@ -2 +2 @@\n-  use("a b");\n+  use("ab");\n'
            revised, actions = repair.redundant_patch_repairs(spec, '1 hunk FAILED', archive, {'demo.patch': changed_literal}, review)
            self.assertEqual(revised, spec)
            self.assertTrue(review)

    def test_manifest_dependency_detection_uses_arbitrary_names(self):
        import json
        commit = 'a' * 40
        spec = 'BuildRequires: vendor-engine-devel\n'
        log = "src/vendor_engine_utils.cpp:3:4: error: class has no member named OtherAPI\n"
        data = {'scripts/known_good.json': json.dumps({'repos': [{'name': 'Vendor-Engine', 'commit': commit}]}).encode()}
        self.assertEqual(repair.manifest_api_requirements(spec, log, data), ['vendor-engine-commit(' + commit + ')'])
        self.assertFalse(repair.manifest_api_requirements(spec, log.replace('vendor_engine_utils', 'unrelated'), data))

    def test_new_file_family_defaults_to_main_package(self):
        spec = 'Name: unrelated\n%files devel\n/usr/include/vendor.h\n%files\n/usr/share/vendor/data\n'
        updated, actions, manual = repair.exact_unpackaged(spec, 'Installed (but unpackaged) file(s) found:\n/opt/new/file\n', o)
        self.assertIn('/opt/new/file', updated.split('%files\n')[1])
        self.assertFalse(manual)

    def test_socket_probe_fix_preserves_entire_suite(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'source.tar'
            content = ('# Simple ephemeral sanity pre-check\nmy $fake_danger;\n'
                       '$pkg->new(LocalAddr=>"[::]", Listen=>1);\n'
                       '$pkg->new(LocalAddr=>"127.0.0.1", Listen=>1);\n')
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo('demo-1/lib/Net/Server/IP.pm')
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content.encode()))
            spec = root / 'demo.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() + '\nName: demo\nVersion: 1\n%prep\n%autosetup -p1\n%check\nmake test\n')
            log = root / 'demo.log'
            log.write_text('t/11_TCP_ipv4.t Failed\nt/12_TCP_ipv6.t Failed')
            plan = repair.plan_package('demo', spec, [log], o, archive)
            patch = plan['patch_changes'][0]['updated']
            self.assertIn('Family=>AF_INET6', patch)
            self.assertIn('Family=>AF_INET', patch)
            self.assertIn('%check\nmake test', plan['updated_spec'])
            self.assertNotIn('skip', patch)
            repair.apply_plan(plan)
            self.assertTrue(repair.plan_package('demo', spec, [log], o, archive)['already_applied'])

    def test_repository_checksum_uses_current_metadata_and_actual_bytes(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import hawkey
        payload = b'current rpm bytes'
        package = SimpleNamespace(name='demo', version='1', release='1.or11', arch='noarch',
                                  chksum=(hawkey.CHKSUM_SHA256, hashlib.sha256(payload).digest()),
                                  remote_location=lambda: 'https://example.test/demo-1-1.or11.noarch.rpm')
        class Query:
            def filter(self, **kwargs):
                return [package]
        class Solver:
            def __init__(self, *args):
                self.base = SimpleNamespace(sack=SimpleNamespace(query=Query))
            def close(self):
                pass
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def raise_for_status(self): pass
            def iter_bytes(self): yield payload
        class Client(Response):
            def __init__(self, *args, **kwargs): pass
            def stream(self, *args): return Response()
        log = "https://example.test/release/bulk/x86_64/\n[MIRROR] demo-1-1.or11.noarch.rpm: checksum doesn't match\n"
        with patch.object(o, '_DnfSolver', Solver), patch.object(o.httpx, 'Client', Client):
            self.assertTrue(o._repair_repository_checksums(log))
            package.chksum = (hawkey.CHKSUM_SHA256, b'0' * 32)
            self.assertFalse(o._repair_repository_checksums(log))

    def test_clang_libraries_do_not_satisfy_compiler_requirement(self):
        spec = 'Name: demo\nBuildRequires: clang-libs\n'
        updated = repair.add_build_requirement(spec, 'clang')
        self.assertIn('BuildRequires:  clang\n', updated)
        self.assertEqual(repair.add_build_requirement(updated, 'clang'), updated)

    def test_verified_cpp_fix_uses_existing_autopatch_once(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'source.tar'
            content = b'#include <vector>\nvoid f() { std::find(v.begin(), v.end(), 1); }\n'
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo('demo-1/source.cpp'); member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
            spec = root / 'demo.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() +
                            '\nName: demo\nVersion: 1\n%prep\n%setup -q\n%autopatch -p1\n')
            log = root / 'demo-x86_64-job1.log'
            log.write_text("source.cpp:2:1: error: no matching function for call to 'find(v.begin(), v.end(), 1)'")
            plan = repair.plan_package('demo', spec, [log], o, archive)
            self.assertTrue(plan['patch_changes'])
            self.assertRegex(Path(plan['patch_changes'][0]['path']).name, r'^\d+\.patch$')
            self.assertNotIn('%patch ', plan['updated_spec'])
            self.assertIn('+#include <algorithm>', plan['patch_changes'][0]['updated'])
            repair.apply_plan(plan)
            repeated = repair.plan_package('demo', spec, [log], o, archive)
            self.assertFalse(repeated['actions'])
            self.assertTrue(repeated['already_applied'])

    def test_checksum_requires_independent_registry_agreement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            spec = root / 'demo.spec'
            spec.write_text('%global source0_hash ' + '0'*64 + '\nName: demo\nVersion: 1\n')
            import tarfile
            source = root / 'source.tar'
            with tarfile.open(source, 'w'):
                pass
            log = root / 'demo-x86_64-job1.log'
            log.write_text('oreon: Source0 hash mismatch\nerror: Bad exit status from /tmp/failure (%prep)')
            untrusted = repair.plan_package('demo', spec, [log], o, source)
            self.assertEqual(untrusted['updated_spec'], spec.read_text())
            trusted = repair.plan_package('demo', spec, [log], o, source,
                                          hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertTrue(any('published crates.io checksum' in x['action'] for x in trusted['actions']))
            self.assertNotIn('Patch or source checksum failure needs review.', trusted['manual'])

    def test_prep_failure_does_not_automatically_mean_failed_patch(self):
        self.assertFalse(o._fastfix_patch_failure('error: Bad exit status from /tmp/a (%prep)'))
        self.assertTrue(o._fastfix_patch_failure('Hunk #1 FAILED at 12.'))

    def test_verified_missing_doc_removed_and_existing_edits_not_unresolved(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'source.tar'
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo('demo-1/Changes')
                member.size = 1
                tar.addfile(member, io.BytesIO(b'x'))
            spec = root / 'demo.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() +
                            '\nName: demo\nVersion: 1\n%files\n%doc Changes README\n')
            log = root / 'demo-x86_64-job1.log'
            log.write_text("cp: cannot stat '/build/demo-1/README': No such file or directory\nFile not found: /BUILDROOT/usr/share/doc/demo/README")
            plan = repair.plan_package('demo', spec, [log], o, archive)
            self.assertEqual(len(plan['actions']), 1)
            self.assertNotIn('README', plan['updated_spec'])
            self.assertFalse(plan['manual'])
            spec.write_text('Name: demo\nVersion: 1\nBuildRequires: perl(JSON::XS)\n%files\n/usr/bin/demo\n')
            log.write_text("Can't locate JSON/XS.pm in @INC")
            plan = repair.plan_package('demo', spec, [log], o)
            self.assertTrue(plan['already_applied'])
            output = repair.friendly_summary(112, [plan], apply=True)
            self.assertIn('Edits already present locally', output)
            self.assertNotIn(';', output)

    def test_macro_patch_completion_changes_only_missed_code(self):
        import io, tarfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'source.tar'
            content = b'/* head stays in this comment */\nvoid f() { head[0] = 1; }\n'
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo('demo-1/missed.c')
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
            spec = root / 'demo.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() +
                            '\nName: demo\nVersion: 1\nPatch0: rename.patch\n')
            patch = root / 'rename.patch'
            patch.write_text('--- a/header.h\n+++ b/header.h\n@@ -1 +1 @@\n-#define head array\n+#define renamed_head array\n')
            log = root / 'demo-x86_64-job1.log'
            log.write_text("missed.c:2:12: error: 'head' undeclared (first use in this function)")
            plan = repair.plan_package('demo', spec, [log], o, archive)
            self.assertEqual(len(plan['patch_changes']), 1)
            self.assertIn('renamed_head[0] = 1', plan['patch_changes'][0]['updated'])
            self.assertIn('head stays in this comment', plan['patch_changes'][0]['updated'])
            repair.apply_plan(plan)
            repeated = repair.plan_package('demo', spec, [log], o, archive)
            self.assertFalse(repeated['patch_changes'])

    def test_terminal_only_command_cleans_downloads(self):
        from unittest.mock import patch
        runner = CliRunner()
        with runner.isolated_filesystem():
            root = Path('specs'); (root / 'demo').mkdir(parents=True)
            (root / 'demo/demo.spec').write_text('Name: demo\nVersion: 1\n%files\n/usr/bin/demo\n')
            downloaded = []
            def fetch(chain, folder, **kwargs):
                downloaded.append(Path(folder))
                (Path(folder) / 'demo-x86_64-job1.log').write_text("Can't locate JSON/XS.pm in @INC")
            with patch.object(o, '_download_failed_plan_logs', side_effect=fetch), patch.object(o, '_provide_index', return_value={}):
                result = runner.invoke(o.cli, ['repair-chain', '112', '--spec-root', str(root)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn('Add a build dependency: perl(JSON::XS)', result.output)
            self.assertFalse(downloaded[0].exists())
            self.assertEqual([x.name for x in Path('.').iterdir()], ['specs'])
            self.assertNotIn('--out', result.output)
            self.assertNotIn('report.json', result.output)
            self.assertNotIn('BuildRequires:', (root / 'demo/demo.spec').read_text())

    def test_more_dependency_signatures_and_real_diagnostics(self):
        log = "Run-time dependency glfw3 found: NO (tried pkgconfig and cmake)\nfatal error: zlib.h: No such file or directory\nModuleNotFoundError: No module named 'yaml'\n/bin/sh: glslc: command not found\nfoo.c:4: error: invalid conversion\nerror: Bad exit status from /tmp/test (%build)"
        deps = {x for x, _ in repair.build_requirements(log, lambda kind, name: {'header': 'zlib-devel', 'python': 'python3dist(pyyaml)', 'executable': '/usr/bin/glslc'}[kind])}
        self.assertEqual(deps, {'pkgconfig(glfw3)', 'zlib-devel', 'python3dist(pyyaml)', '/usr/bin/glslc'})
        diagnostics = repair.failure_diagnostics(log)
        self.assertIn('foo.c:4: error: invalid conversion', diagnostics)
        self.assertFalse(any('Bad exit status' in x for x in diagnostics))

    def test_friendly_summary_explains_applied_changes_and_blockers(self):
        plans = [
            {'package': 'demo', 'actions': [{'action': 'Add BuildRequires: perl(JSON::XS)'}]},
            {'package': 'waiting', 'missing_dependencies': ['perl(Example)'], 'prerequisites': [
                {'requirement': 'perl(Example)', 'candidate_sources': ['perl-Example']}]},
            {'package': 'unknown', 'manual': ['No automatic repair is available for this failure.']},
        ]
        text = repair.friendly_summary(112, plans, apply=True, log_dir=Path('logs'))
        self.assertIn('Applied 1 spec edit:', text)
        self.assertIn('Add a build dependency: perl(JSON::XS)', text)
        self.assertIn('Candidate source: perl-Example', text)
        self.assertIn('No automatic repair is available for this failure.', text)
        self.assertIn('Still blocked:', text)
        self.assertNotIn('repair(s)', text)
        preview = repair.friendly_summary(112, plans, apply=False, log_dir=Path('my logs'))
        self.assertIn('Proposed 1 spec edit:', preview)
        self.assertIn("--log-dir 'my logs'", preview)

    def test_summary_avoids_empty_sections_and_repeated_test_failures(self):
        plans = [{'package': 'python-pytest-regressions', 'actions': [],
                  'missing_dependencies': [],
                  'manual': ['No unique repository provider for python matplotlib'],
                  'diagnostics': ["ModuleNotFoundError: No module named 'matplotlib'",
                                  'FAILED tests/test_image_regression.py::test_image_regression[pil]']}]
        text = repair.friendly_summary(116, plans, apply=True, job_count=2)
        self.assertIn('Chain 116: 1 failed package (2 jobs)', text)
        self.assertIn('Cannot identify a repository package for Python module matplotlib.', text)
        self.assertIn('Check that the provider is published, then add its BuildRequires.', text)
        self.assertNotIn('None detected', text)
        self.assertNotIn('No new edits', text)
        self.assertNotIn('ModuleNotFoundError', text)
        self.assertNotIn('FAILED tests/', text)

    def test_unavailable_packages_are_prerequisites(self):
        log = "No matching package to install: 'python3dist(furo)'\nProblem: nothing provides requested (crate(syn/default) >= 3 with crate(syn/default) < 4)\n"
        self.assertEqual(len(repair.missing_dependencies(log)), 2)
        self.assertEqual(repair.build_requirements(log), [])

    def test_perl_and_sphinx_modules_are_build_requirements(self):
        log = "Can't locate File/ShareDir/Install.pm in @INC\nCould not import extension autoapi.extension (exception: No module named 'autoapi')"
        self.assertEqual({d for d, _ in repair.build_requirements(log, lambda kind, name: 'python3dist(sphinx-autoapi)')},
                         {'perl(File::ShareDir::Install)', 'python3dist(sphinx-autoapi)'})

    def test_exact_files_never_collapse_to_shared_directory(self):
        spec = 'Name: demo\nVersion: 1\n%files\n%{_bindir}/demo\n%changelog\n'
        log = 'Installed (but unpackaged) file(s) found:\n/usr/bin/a\n/usr/bin/b\n/usr/lib/debug/usr/bin/a.debug\n'
        updated, actions, manual = repair.exact_unpackaged(spec, log, o)
        self.assertIn('%{_bindir}/a\n', updated)
        self.assertIn('%{_bindir}/b\n', updated)
        self.assertNotIn('%{_bindir}/\n', updated)
        self.assertNotIn('a.debug', updated)
        self.assertEqual(len(actions), 2)
        self.assertEqual(manual, [])

    def test_executable_owner_follows_existing_executables(self):
        spec = 'Name: demo\n%files\n/usr/bin/demo\n%files devel\n/usr/include/demo.h\n'
        updated, actions, manual = repair.exact_unpackaged(spec,
            'Installed (but unpackaged) file(s) found:\n/usr/bin/new\n', o)
        self.assertIn('%{_bindir}/new', updated.split('%files devel')[0])
        self.assertEqual(len(actions), 1)
        self.assertFalse(manual)

    def test_runtime_and_linker_files_have_distinct_owners(self):
        spec = ('Name: unrelated\n%files\n/usr/lib64/libvendor.so.0*\n'
                '%files devel\n/usr/lib64/libvendor.so\n/usr/bin/vendor-tool\n')
        log = ('Installed (but unpackaged) file(s) found:\n/usr/lib64/libvendor-gpu.so.1\n'
               '/usr/lib64/libvendor-gpu.so\n/usr/bin/vendor-gpu\n')
        updated, actions, manual = repair.exact_unpackaged(spec, log, o)
        runtime, devel = updated.split('%files devel')
        self.assertIn('libvendor-gpu.so.1', runtime)
        self.assertIn('libvendor-gpu.so\n', devel)
        self.assertIn('vendor-gpu\n', devel)
        self.assertEqual(len(actions), 3)
        self.assertFalse(manual)

    def test_preview_apply_idempotence_and_changed_input_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); pkg = root/'demo';pkg.mkdir()
            spec = pkg/'demo.spec';spec.write_text('Name: demo\nVersion: 1\n%description\nDemo\n%files\n/usr/bin/demo\n')
            logs = root/'logs';logs.mkdir();log=logs/'demo-x86_64-job1.log'
            log.write_text("Can't locate Test/More.pm in @INC\n")
            before=spec.read_text();plan=repair.plan_package('demo',spec,[log],o)
            self.assertEqual(spec.read_text(),before)
            repair.apply_plan(plan)
            self.assertIn('BuildRequires:  perl(Test::More)',spec.read_text())
            self.assertEqual(repair.plan_package('demo',spec,[log],o)['diff'],'')
            with self.assertRaises(ValueError):repair.apply_plan(plan)

    def test_command_preview_never_changes_specs(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);pkg=root/'specs/demo';pkg.mkdir(parents=True)
            spec=pkg/'demo.spec';spec.write_text('Name: demo\nVersion: 1\n%description\nDemo\n')
            logs=root/'logs';logs.mkdir();(logs/'demo-x86_64-job1.log').write_text("Can't locate Test/More.pm in @INC")
            result=CliRunner().invoke(o.cli,['repair-chain','111','--log-dir',str(logs),'--spec-root',str(root/'specs')])
            self.assertEqual(result.exit_code,0,result.output)
            self.assertNotIn('BuildRequires:',spec.read_text())
            self.assertEqual({x.name for x in root.iterdir()}, {'specs', 'logs'})
            self.assertIn('Add a build dependency: perl(Test::More)', result.output)

    def test_stale_logs_never_edit_a_new_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);spec=root/'demo.spec';spec.write_text('Name: demo\nVersion: 2\n%files\n/usr/bin/demo\n')
            log=root/'demo-x86_64-job1.log';log.write_text("Wrote: /builddir/build/SRPMS/demo-1-1.or11.src.rpm\nCan't locate Test/More.pm in @INC\n")
            plan=repair.plan_package('demo',spec,[log],o)
            self.assertEqual(plan['diff'],'')
            self.assertIn('different source version',plan['manual'][0])

    def test_document_rename_requires_verified_source(self):
        import io,tarfile
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'demo.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                entry=tarfile.TarInfo('demo-1/README.md');entry.size=4
                tar.addfile(entry,io.BytesIO(b'docs'))
            spec='%global source0_hash '+hashlib.sha256(archive.read_bytes()).hexdigest()+'\nName: demo\nVersion: 1\n# README\n%files\n%doc README # README\n'
            log="cp: cannot stat '/build/demo-1/README': No such file or directory"
            updated,actions,manual=repair.verified_source_repairs(spec,log,archive)
            self.assertIn('%doc README.md # README',updated)
            self.assertIn('# README\n',updated)
            self.assertEqual(len(actions),1)
            bad=spec.replace(hashlib.sha256(archive.read_bytes()).hexdigest(),'0'*64)
            self.assertEqual(repair.verified_source_repairs(bad,log,archive)[0],bad)
            self.assertTrue(repair.verified_source_repairs(bad,log,archive)[2])

    def test_log_paths_cannot_inject_rpm_macros(self):
        spec='Name: demo\n%files\n/usr/bin/demo\n'
        updated,actions,manual=repair.exact_unpackaged(spec,'Installed (but unpackaged) file(s) found:\n/usr/bin/%(evil)\n',o)
        self.assertEqual(updated,spec)
        self.assertFalse(actions)
        self.assertTrue(manual)

    def test_cargo_generator_keeps_upper_limits_and_test_dependencies(self):
        blob='[package]\nname="demo"\nversion="1.0.0"\n[dependencies]\nalpha="1"\n[dev-dependencies]\nbeta="2"\n'
        result=o._cargo_generated_brs(blob,'%bcond check 1\n%cargo_generate_buildrequires\n')
        self.assertIsNotNone(result)
        self.assertTrue(any('crate(alpha/default)' in item and '< 2.0.0~' in item for item in result))
        self.assertTrue(any('crate(beta/default)' in item and '< 3.0.0~' in item for item in result))
        self.assertEqual(o._cargo_generated_brs('[package]\nname="demo"\nversion="1.0.0"\n'),[])


    def test_translator_merge_keeps_the_original_spec(self):
        old = (
            "%global source0_hash abc\n"
            "%global crate hmac\n"
            "Name: rust-hmac\n"
            "Version: 0.12.0\n"
            "Release: 3%{?dist}\n"
            "License: MIT\n"
            "Patch0: oreon.patch\n"
            "BuildRequires: cargo-rpm-macros\n"
            "%description\n"
            "kept\n"
            "%prep\n"
            "%autosetup -p1\n"
            "echo local-fix\n"
            "%files devel\n"
            "%{crate_instdir}/\n"
            "%changelog\n"
            "* old entry\n"
        )
        new = (
            "Name: rust-hmac\n"
            "Version: 0.13.0\n"
            "Release: 1%{?dist}\n"
            "License: MIT OR Apache-2.0\n"
            "Source: %{crates_source}\n"
            "BuildRequires: cargo-rpm-macros\n"
            "BuildRequires: crate(digest)\n"
            "%description\n"
            "generated\n"
            "%prep\n"
            "%cargo_prep\n"
            "%files\n"
            "gone\n"
            "%changelog\n"
            "* generated entry\n"
        )
        merged = o._lang2rpm_merge(old, new)
        self.assertIn("Version: 0.13.0", merged)
        self.assertIn("Release: 1%{?dist}", merged)
        self.assertIn("License: MIT OR Apache-2.0", merged)
        self.assertIn("Patch0: oreon.patch", merged)
        self.assertIn("echo local-fix", merged)
        self.assertIn("%{crate_instdir}/", merged)
        self.assertIn("* old entry", merged)
        self.assertNotIn("generated", merged)
        self.assertIn("BuildRequires: crate(digest)", merged)
        self.assertIn("%global source0_hash none", merged)


if __name__ == '__main__':unittest.main()
