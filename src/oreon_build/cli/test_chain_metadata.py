# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import io
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from packaging.markers import default_environment
from cli import chain_metadata as m, orbscli as o


class MetadataTests(unittest.TestCase):
    def test_combined_python_and_cargo_buildrequires_are_evaluated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'python-demo'
            package.mkdir()
            spec = package / 'python-demo.spec'
            spec.write_text('Name: python-demo\n')
            source = root / 'source'
            source.mkdir()
            (source / 'Cargo.toml').write_text('[package]\nname="demo"\n')
            expanded = ('%generate_buildrequires\n'
                        '/usr/bin/python3 /usr/lib/rpm/redhat/pyproject_buildrequires.py --python3_pkgversion 3\n'
                        '%generate_buildrequires\n'
                        '/usr/bin/cargo2rpm --path Cargo.toml buildrequires\n'
                        '%build\n')
            project_environment = {**default_environment(), 'python_version': '3.14',
                                   'python_full_version': '3.14.0'}
            project = {'environment': project_environment, 'pkgversion': '3', 'name': 'demo',
                       'version': '1', 'requirements': [], 'extras': {}}
            cargo_result = type('Result', (), {'returncode': 0, 'stdout': 'crate(foo) >= 1\n', 'stderr': ''})()
            with patch.object(m, 'source_tree', return_value=source), \
                 patch.object(m, 'python_metadata', return_value=(['python3dist(bar)'], project)), \
                 patch.object(m.shutil, 'which', return_value='/usr/bin/cargo2rpm'), \
                 patch.object(m.subprocess, 'run', return_value=cargo_result):
                requirements = m.evaluate(o, spec, 'x86_64', expanded, [], '3.14.0')
            self.assertEqual(requirements, ['crate(foo) >= 1', 'python3dist(bar)'])

    def test_go_vendor_license_buildrequires_are_evaluated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'go-demo'
            package.mkdir()
            spec = package / 'go-demo.spec'
            config = package / 'go-vendor-tools.toml'
            config.write_text('[licensing]\ndetector = "trivy"\n')
            source = root / 'source'
            source.mkdir()
            expanded = ('%generate_buildrequires\n'
                        'go_vendor_license --config go-vendor-tools.toml generate_buildrequires\n'
                        '%build\n')
            result = type('Result', (), {'returncode': 0, 'stdout': 'trivy\n', 'stderr': ''})()
            seen = {}
            def fake_run(command, cwd=None, capture_output=False, text=False, timeout=None):
                seen['command'] = command
                seen['cwd'] = cwd
                return result
            with patch.object(m, 'source_tree', return_value=source), \
                 patch.object(m.subprocess, 'run', fake_run):
                requirements = m.evaluate(o, spec, 'x86_64', expanded, [])
            self.assertEqual(requirements, ['trivy'])
            self.assertEqual(seen['command'],
                             ['go_vendor_license', '--config', str(config), 'generate_buildrequires'])
            self.assertEqual(seen['cwd'], source)

    def test_go_vendor_license_rejects_unknown_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'go-demo'
            package.mkdir()
            spec = package / 'go-demo.spec'
            source = root / 'source'
            source.mkdir()
            expanded = ('%generate_buildrequires\n'
                        'go_vendor_license generate_buildrequires\n'
                        '%build\n')
            result = type('Result', (), {'returncode': 0, 'stdout': 'trivy; rm -rf /\n', 'stderr': ''})()
            with patch.object(m, 'source_tree', return_value=source), \
                 patch.object(m.subprocess, 'run', lambda *args, **kw: result):
                with self.assertRaisesRegex(m.SourceMetadataError, 'unsupported dependency'):
                    m.evaluate(o, spec, 'x86_64', expanded, [])

    def test_prep_sed_accepts_combined_flags(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            package = root / 'demo'
            package.mkdir()
            archive = package / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                for name, data in (('demo/requirements.txt', b'foo\n'),):
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    stream.addfile(member, io.BytesIO(data))
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            spec = package / 'demo.spec'
            spec.write_text('%global source0_hash ' + checksum + '\n')
            work = root / 'work'
            work.mkdir()
            source = m.source_tree(o, spec,
                'Source0: https://example.invalid/source.tar.gz\n'
                '%prep\nsed -ri "s/foo/bar/" requirements.txt\n%build\n', work,
                build_metadata_only=True)
            self.assertEqual((source / 'requirements.txt').read_text(), 'bar\n')

    def test_generator_workdir_follows_spec_cd_before_cargo_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / 'agent' / 'rs' / 'htmlparser'
            work.mkdir(parents=True)
            body = 'cd agent/rs/htmlparser\n/usr/bin/cargo2rpm --path Cargo.toml buildrequires\ncd ../../..'
            self.assertEqual(m._generator_workdir(
                root, body, r'^\s*(?:/usr/bin/)?cargo2rpm\s+'), work)

    def test_generator_workdir_rejects_escape_from_source_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'work').mkdir()
            body = 'cd work\ncd ../../..\n/usr/bin/cargo2rpm buildrequires'
            with self.assertRaises(m.SourceMetadataError):
                m._generator_workdir(root, body, r'^\s*(?:/usr/bin/)?cargo2rpm\s+')

    def test_python_generator_tracks_pushd_before_buildrequires(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'python').mkdir()
            self.assertEqual(m.python_generator_source(
                root, 'pushd python >/dev/null\nexport SETUPTOOLS_SCM_VERSION_WRITE_TO_PREFIX="python"\n'),
                root / 'python')

    def test_r_buildrequires_reads_package_dependencies_and_r_base_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'R-demo'
            package.mkdir()
            (root / 'R').mkdir()
            (root / 'R' / 'R.spec').write_text('%add_submodule stats %{version}\n%add_submodule utils %{version}\n')
            archive = package / 'source.tar.gz'
            content = b'Package: demo\nDepends: R (>= 4.0), stats\nImports: jsonlite, utils\nLinkingTo: Rcpp\n'
            with tarfile.open(archive, 'w:gz') as tar:
                member = tarfile.TarInfo('demo/DESCRIPTION')
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
            spec = package / 'R-demo.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() +
                            '\nName: R-demo\nSource0: source.tar.gz\n%generate_buildrequires\n%R_buildrequires\n')
            self.assertEqual(m.evaluate(o, spec, 'x86_64', spec.read_text(), []),
                             ['R-jsonlite', 'R-Rcpp'])

    def test_maven_root_coordinates_come_from_pom(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'pom.xml').write_text('<project xmlns="http://maven.apache.org/POM/4.0.0">'
                '<parent><groupId>org.apache</groupId></parent>'
                '<artifactId>apache</artifactId><packaging>pom</packaging></project>')
            self.assertEqual(m.maven_root_provides(source),
                             ['mvn(org.apache:apache)', 'mvn(org.apache:apache:pom:)'])

    def test_maven_modules_follow_declared_modules_and_disables(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'pom.xml').write_text('<project xmlns="http://maven.apache.org/POM/4.0.0">'
                '<groupId>example.org</groupId><artifactId>parent</artifactId><packaging>pom</packaging>'
                '<modules><module>core</module><module>disabled</module></modules></project>')
            for name in ('core', 'disabled'):
                child = source / name
                child.mkdir()
                (child / 'pom.xml').write_text('<project xmlns="http://maven.apache.org/POM/4.0.0">'
                    '<parent><groupId>example.org</groupId></parent><artifactId>' + name +
                    '</artifactId></project>')
            modules = m.maven_module_provides(source,
                "%pom_disable_module disabled\n%mvn_alias 'example.org:core' 'legacy.example:old-core'\n"
                '%mvn_compat_version : 2.0\n')
            self.assertIn('core', modules)
            self.assertNotIn('disabled', modules)
            self.assertIn('mvn(legacy.example:old-core)', modules['core'])
            self.assertIn('mvn(example.org:core:2.0)', modules['core'])

    def test_maven_coordinate_specific_compat_version(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'pom.xml').write_text('<project xmlns="http://maven.apache.org/POM/4.0.0">'
                '<groupId>org.example</groupId><artifactId>jansi</artifactId>'
                '<version>1.18</version></project>')
            modules = m.maven_module_provides(source,
                '%mvn_compat_version org.example:jansi 1.18 1\n')
            self.assertIn('mvn(org.example:jansi:1)', modules['jansi'])

    def test_ant_filtered_pom_is_read_from_verified_template(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'maven').mkdir()
            (source / 'maven/maven.pom').write_text('<project><groupId>org.example</groupId>'
                '<artifactId>@artifactID@</artifactId></project>')
            (source / 'build.xml').write_text('<project><property name="mavenartifact" value="example"/>'
                '<target name="maven"><copy filtering="true" file="maven/maven.pom" '
                'tofile="build/maven/example.pom"><filterset>'
                '<filter token="artifactID" value="${mavenartifact}"/>'
                '</filterset></copy></target></project>')
            modules = m.maven_module_provides(source,
                '%mvn_artifact build/maven/example.pom build/example.jar\n')
            self.assertIn('mvn(org.example:example)', modules['example'])

    def test_maven_jdk_profile_modules_match_declared_build_jdk(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'pom.xml').write_text('<project xmlns="http://maven.apache.org/POM/4.0.0">'
                '<groupId>org.example</groupId><artifactId>parent</artifactId>'
                '<profiles><profile><activation><jdk>[1.8,)</jdk></activation>'
                '<modules><module>assertj3</module><module>disabled</module></modules>'
                '</profile></profiles></project>')
            for name in ('assertj3', 'disabled'):
                child = source / name
                child.mkdir()
                (child / 'pom.xml').write_text('<project xmlns="http://maven.apache.org/POM/4.0.0">'
                    '<parent><groupId>org.example</groupId></parent>'
                    '<artifactId>' + name + '</artifactId></project>')
            expanded = 'BuildRequires: maven-local-openjdk25\n%pom_disable_module disabled\n'
            modules = m.maven_module_provides(source, expanded)
            self.assertIn('mvn(org.example:assertj3)', modules['assertj3'])
            self.assertNotIn('disabled', modules)
            self.assertNotIn('assertj3', m.maven_module_provides(source, ''))

    def test_python_generator_uses_verified_subdirectory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'lib389'
            project.mkdir(parents=True)
            self.assertEqual(m.python_generator_source(root, 'cd src/lib389\n'), project)
            with self.assertRaisesRegex(m.SourceMetadataError, 'leaves the verified source'):
                m.python_generator_source(root, 'cd ../elsewhere\n')

    def test_prep_alias_parser_ignores_unrelated_shell_syntax(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'yt-dlp'
            source.mkdir()
            aliases = m.python_prep_aliases(source, '%prep\nsed -e \\\n%generate_buildrequires\n')
            self.assertEqual(aliases, {source.name: source})

    def test_python_generator_follows_safe_prep_directory_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'h5py-3.16.0'
            source.mkdir()
            aliases = m.python_prep_aliases(source,
                '%prep\nmv h5py-3.16.0 serial\ncp -al serial openmpi\n%generate_buildrequires\ncd openmpi\n')
            self.assertEqual(m.python_generator_source(source, 'cd openmpi\n', aliases), source)

    def test_python_generator_resolves_source_alias_with_subdirectory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cryptlib-3.4.9'
            bindings = source / 'bindings'
            bindings.mkdir(parents=True)
            aliases = {source.name: source}
            self.assertEqual(m.python_generator_source(
                source, 'cd cryptlib-3.4.9/bindings\n', aliases), bindings)

    @unittest.skipUnless(shutil.which('golist') and shutil.which('go'), 'Go dependency tools required')
    def test_verified_go_generator_reads_imports_without_running_source(self):
        with tempfile.TemporaryDirectory(prefix='orbscli-go-test-') as directory:
            root = Path(directory)
            package = root / 'demo'
            package.mkdir()
            archive = package / 'source.tar.gz'
            content = b'package demo\nimport _ "golang.org/x/sys/unix"\n'
            with tarfile.open(archive, 'w:gz') as tar:
                member = tarfile.TarInfo('demo-1/demo.go')
                member.size = len(content)
                tar.addfile(member, io.BytesIO(content))
            spec = package / 'demo.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() +
                            '\nName: demo\nSource0: source.tar.gz\n%generate_buildrequires\n'
                            "golist --imported --package-path example.org/demo --template 'golang({{.}})\\n' --skip-self\n")
            requirements = m.evaluate(o, spec, 'x86_64', spec.read_text(), [])
            self.assertEqual(requirements, ['golang(golang.org/x/sys/unix)'])

    def test_poetry_metadata_preserves_caret_bounds_and_optional_extras(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text('[build-system]\nrequires=["poetry-core>=1"]\nbuild-backend="poetry.core.masonry.api"\n[tool.poetry]\nname="rich"\nversion="15.0.0"\n[tool.poetry.dependencies]\npython=">=3.9"\npygments="^2.13.0"\nipywidgets={version=">=7.5.1,<9", optional=true}\n[tool.poetry.extras]\njupyter=["ipywidgets"]\n')
            reqs, project = m.python_metadata(source, '', default_environment())
            self.assertEqual(project['version'], '15.0.0')
            self.assertTrue(any('pygments' in r and '>= 2.13' in r and '< 3~~' in r for r in reqs))
            self.assertFalse(any('ipywidgets' in r for r in reqs))
            reqs, _ = m.python_metadata(source, '-x jupyter', default_environment())
            self.assertTrue(any('ipywidgets' in r and '< 9~~' in r for r in reqs))
            self.assertEqual(m.poetry_constraint('^0.2.3'), '>=0.2.3,<0.3')
            self.assertEqual(m.poetry_constraint('^0.0.3'), '>=0.0.3,<0.0.4')
            self.assertEqual(m.poetry_constraint('~1.2.3'), '>=1.2.3,<1.3')

    def test_pep621_project_without_build_system_uses_legacy_setuptools_backend(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text(
                '[project]\nname="demo"\nversion="1.0"\ndependencies=[]\n')
            requirements, project = m.python_metadata(source, '', default_environment())
            self.assertIn('python3dist(setuptools) >= 40.8', requirements)
            self.assertEqual(project['name'], 'demo')

    def test_pep621_project_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text(
                '\ufeff[project]\nname="demo"\nversion="1.0"\ndependencies=[]\n')
            requirements, project = m.python_metadata(source, '', default_environment())
            self.assertEqual(project['name'], 'demo')
            self.assertEqual(project['version'], '1.0')

    def test_verified_source_cache_rechecks_content_before_use(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / 'fixture.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('demo/README')
                member.size = 4
                stream.addfile(member, io.BytesIO(b'demo'))
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            url = 'https://example.invalid/demo.tar.gz'
            cache = root / 'cache'
            cache.mkdir()
            cached = cache / (hashlib.sha256(url.encode()).hexdigest() + '-' + checksum)
            archive.replace(cached)
            package = root / 'package'
            package.mkdir()
            spec = package / 'demo.spec'
            spec.write_text('%global source0_hash ' + checksum + '\n')
            work = root / 'first'
            work.mkdir()
            source = m.source_tree(o, spec, 'Source0: ' + url, work, verified_cache=cache)
            self.assertEqual((source / 'README').read_text(), 'demo')
            cached.write_bytes(b'corrupted')
            with self.assertRaisesRegex(m.SourceMetadataError, 'does not match'):
                m.source_tree(o, spec, 'Source0: ' + url, root, verified_cache=cache)

    def test_build_metadata_source_tree_skips_unrelated_archive_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            package = root / 'demo'
            package.mkdir()
            archive = package / 'source.tar.xz'
            with tarfile.open(archive, 'w:xz') as stream:
                for name, data in (('demo/CMakeLists.txt', b'project(demo)\n'),
                                   ('demo/setup.py', b'setup(name="demo")\n'),
                                   ('demo/grpc.gyp', b'{ }\n'), ('demo/Rakefile', b'task :default\n'),
                                   ('demo/configure', b'old\n'), ('demo/data.bin', b'payload')):
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    stream.addfile(member, io.BytesIO(data))
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            spec = package / 'demo.spec'
            spec.write_text('%global source0_hash ' + checksum + '\nPatch0: configure.patch\n')
            (package / 'configure.patch').write_text(
                'diff -uNr demo.old/configure demo/configure\n--- demo.old/configure\n+++ demo/configure\n@@ -1 +1 @@\n-old\n+new\n')
            work = root / 'work'
            work.mkdir()
            source = m.source_tree(o, spec,
                'Source0: https://example.invalid/source.tar.xz\nPatch0: configure.patch\n'
                '%prep\nsed -r -i -e "s/x/y/" setup.py grpc.gyp Rakefile\n%build\n', work,
                build_metadata_only=True)
            self.assertTrue((source / 'CMakeLists.txt').is_file())
            self.assertTrue((source / 'setup.py').is_file())
            self.assertTrue((source / 'grpc.gyp').is_file())
            self.assertTrue((source / 'Rakefile').is_file())
            self.assertEqual((source / 'configure').read_text(), 'old\n')
            self.assertFalse((source / 'data.bin').exists())

    def test_source_tree_uses_patch_specific_autopatch_strip_level(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            package = root / 'demo'
            package.mkdir()
            archive = package / 'source.tar.gz'
            content = b'AC_ARG_ENABLE(build-format-warnings,\n  [wf_opt=-Wno-format])\n'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('demo/gcc/configure.ac')
                member.size = len(content)
                stream.addfile(member, io.BytesIO(content))
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            spec = package / 'demo.spec'
            spec.write_text('%global source0_hash ' + checksum + '\nPatch1: fix.patch\n'
                            '%prep\n%autopatch -p0 -m 1 -M 1\n%build\n')
            (package / 'fix.patch').write_text(
                '--- gcc/configure.ac\n+++ gcc/configure.ac\n'
                '@@ -1,2 +1,2 @@\n AC_ARG_ENABLE(build-format-warnings,\n-  [wf_opt=-Wno-format])\n+  [wf_opt="-Wno-format -Wno-format-security"])\n')
            work = root / 'work'
            work.mkdir()
            source = m.source_tree(o, spec,
                'Source0: https://example.invalid/source.tar.gz\nPatch1: fix.patch\n'
                '%prep\n%autopatch -p0 -m 1 -M 1\n%build\n', work,
                libtool_builds=True)
            self.assertIn(b'-Wno-format-security', (source / 'gcc/configure.ac').read_bytes())

    def test_build_metadata_source_tree_keeps_patch_files_with_different_archive_roots(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            package = root / 'demo'
            package.mkdir()
            archive = package / 'source.tar.xz'
            files = {
                'binutils-2.47/bfd/Makefile.am': b'old\n',
                'binutils-2.47/bfd/Makefile.in': b'old\n',
                'binutils-2.47/unused/data': b'payload',
            }
            with tarfile.open(archive, 'w:xz') as stream:
                for name, data in files.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    stream.addfile(member, io.BytesIO(data))
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            spec = package / 'demo.spec'
            spec.write_text('%global source0_hash ' + checksum + '\nPatch1: fix.patch\n')
            (package / 'fix.patch').write_text(
                '--- binutils.orig/bfd/Makefile.am\n+++ binutils-2.38/bfd/Makefile.am\n'
                '@@ -1 +1 @@\n-old\n+new\n'
                '--- binutils.orig/bfd/Makefile.in\n+++ binutils-2.38/bfd/Makefile.in\n'
                '@@ -1 +1 @@\n-old\n+new\n')
            work = root / 'work'
            work.mkdir()
            source = m.source_tree(o, spec,
                'Source0: https://example.invalid/source.tar.xz\nPatch1: fix.patch', work,
                build_metadata_only=True, libtool_builds=True)
            self.assertEqual((source / 'bfd/Makefile.am').read_text(), 'new\n')
            self.assertEqual((source / 'bfd/Makefile.in').read_text(), 'new\n')
            self.assertFalse((source / 'unused/data').exists())

    def test_source_zero_accepts_unindexed_source_tag(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / 'fixture.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('NetworkManager/README')
                member.size = 2
                stream.addfile(member, io.BytesIO(b'ok'))
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
            url = 'https://example.invalid/NetworkManager.tar.gz'
            cache = root / 'cache'
            cache.mkdir()
            archive.replace(cache / (hashlib.sha256(url.encode()).hexdigest() + '-' + checksum))
            package = root / 'NetworkManager'
            package.mkdir()
            spec = package / 'NetworkManager.spec'
            spec.write_text('%global source0_hash ' + checksum + '\n')
            work = root / 'work'
            work.mkdir()
            source = m.source_tree(o, spec, 'Source: ' + url, work, verified_cache=cache)
            self.assertEqual((source / 'README').read_text(), 'ok')

    def test_setuptools_attribute_version_is_read_without_import(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'src/demo').mkdir(parents=True)
            (source / 'src/demo/__init__.py').write_text('raise RuntimeError("must not import")\n__version__ = "1.0.0.beta2"\n')
            (source / 'setup.cfg').write_text('[metadata]\nname=demo\nversion=attr:src.demo.__version__\n')
            self.assertEqual(m.setup_metadata(source)['version'], '1.0.0.beta2')

    def test_cmake_soname_scan_ignores_unrelated_escaped_quote_calls(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'CMakeLists.txt').write_text(
                'set_target_properties(foo PROPERTIES SOVERSION 3)\n'
                'set_property(TARGET \\${lib} PROPERTY IMPORTED_LOCATION_\\${C} '
                '        \\\"\\${lib_dir}/\\${c}/\\${lib_prefix}\\${lib}\\${lib_suffix}\\\")\n')
            self.assertEqual(m.cmake_shared_library_sonames(source, {'libfoo'}),
                             {'libfoo': 'libfoo.so.3'})

    def test_libtool_soname_resolves_versioned_library_glob_from_source(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'GUILE-VERSION').write_text('GUILE_EFFECTIVE_VERSION=2.2\n'
                'LIBGUILE_INTERFACE_CURRENT=5\nLIBGUILE_INTERFACE_REVISION=2\n'
                'LIBGUILE_INTERFACE_AGE=4\n')
            library = source / 'libguile'
            library.mkdir()
            (library / 'Makefile.am').write_text(
                'lib_LTLIBRARIES = libguile-@GUILE_EFFECTIVE_VERSION@.la\n'
                'version_info = @LIBGUILE_INTERFACE_CURRENT@:@LIBGUILE_INTERFACE_REVISION@:@LIBGUILE_INTERFACE_AGE@\n'
                'libguile_@GUILE_EFFECTIVE_VERSION@_la_LDFLAGS = -version-info $(version_info)\n')
            self.assertEqual(m.libtool_shared_library_sonames(source, {'libguile*'}),
                             {'libguile-2.2': 'libguile-2.2.so.1'})

    def test_automake_soname_reads_custom_install_links(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            elf = source / 'libelf'
            elf.mkdir()
            (elf / 'Makefile.am').write_text(
                'VERSION = 1\n'
                'install: install-am libelf.so\n'
                '\tln -fs libelf-$(PACKAGE_VERSION).so $(DESTDIR)$(libdir)/libelf.so.$(VERSION)\n')
            debuginfod = source / 'debuginfod'
            debuginfod.mkdir()
            (debuginfod / 'Makefile.am').write_text(
                'install: install-am libdebuginfod.so\n'
                '\tln -fs libdebuginfod-$(PACKAGE_VERSION).so $(DESTDIR)$(libdir)/$(LIBDEBUGINFOD_SONAME)\n')
            (source / 'configure.ac').write_text('LIBDEBUGINFOD_SONAME=libdebuginfod.so.1\n')
            self.assertEqual(m.libtool_shared_library_sonames(source, {'libelf', 'libdebuginfod'}),
                             {'libelf': 'libelf.so.1', 'libdebuginfod': 'libdebuginfod.so.1'})

    def test_libtool_soname_metadata_tolerates_latin1_source_comments(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'configure.ac').write_bytes(b'# Copyright \xa9\nLIBDEMO_SONAME=libdemo.so.2\n')
            (source / 'Makefile.am').write_bytes(
                b'# Copyright \xa9\ninstall: install-am libdemo.so\n'
                b'\tln -fs libdemo.so $(DESTDIR)$(libdir)/$(LIBDEMO_SONAME)\n')
            self.assertEqual(m.libtool_shared_library_sonames(source, {'libdemo'}),
                             {'libdemo': 'libdemo.so.2'})

    def test_makefile_soname_resolves_version_from_source_rules(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'VERSION').write_text('1.13.0\n')
            (source / 'Make.inc').write_text(
                'JULIA_VERSION := $(shell cat $(JULIAHOME)/VERSION)\n'
                'JULIA_MAJOR_VERSION := $(shell echo $(JULIA_VERSION) | cut -d\'-\' -f 1 | cut -d\'.\' -f 1)\n'
                'JULIA_MINOR_VERSION := $(shell echo $(JULIA_VERSION) | cut -d\'-\' -f 1 | cut -d\'.\' -f 2)\n'
                'SOMAJOR := $(JULIA_MAJOR_VERSION).$(JULIA_MINOR_VERSION)\n'
                'SOMAJOR := $(SOMAJOR)_$(SYMBOL_VERSION_SUFFIX)\n'
                'JL_MAJOR_SHLIB_EXT := $(SHLIB_EXT).$(SOMAJOR)\n')
            source.mkdir(exist_ok=True)
            (source / 'cli').mkdir()
            (source / 'cli/Makefile').write_text(
                '$(CC) $(VERSIONSCRIPT) $(call SONAME_FLAGS,libjulia.$(JL_MAJOR_SHLIB_EXT))\n')
            self.assertEqual(m.libtool_shared_library_sonames(source, {'libjulia'}),
                             {'libjulia': 'libjulia.so.1.13'})

    def test_explicit_vcs_version_matches_build_environment(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text('[build-system]\nrequires=["hatchling", "hatch-vcs"]\n[project]\nname="demo"\ndynamic=["version"]\n[tool.hatch.version]\nsource="vcs"\n')
            _, project = m.python_metadata(source, '', default_environment(), scm_version='0.0.5')
            self.assertEqual(project['version'], '0.0.5')
            (source / 'pyproject.toml').unlink()
            (source / 'setup.py').write_text('setup(name="demo", use_scm_version=True, setup_requires=["setuptools_scm"])\n')
            _, project = m.python_metadata(source, '', default_environment(), scm_version='2.11.0')
            self.assertEqual(project['version'], '2.11.0')

    def test_setup_metadata_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text(
                '\ufeff#!/usr/bin/env python\nsetup(name="demo", version="1.2")\n')
            self.assertEqual(m.setup_metadata(source)['name'], 'demo')
            self.assertEqual(m.setup_metadata(source)['version'], '1.2')

    def test_dynamic_python_version_comes_from_declared_configure_template(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text('[project]\nname="gpg"\ndynamic=["version"]\n'
                '[build-system]\nrequires=["setuptools", "swig"]\nbuild-backend="setuptools.build_meta"\n')
            (source / 'VERSION').write_text('2.0.0\ncommit-id\n')
            (source / 'setup.py.in').write_text('version = "@VERSION@"\n')
            _, project = m.python_metadata(source, '', default_environment())
            self.assertEqual(project['version'], '2.0.0')

    def test_setuptools_dynamic_file_and_version_are_read_from_source(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'lib/demo').mkdir(parents=True)
            (source / 'lib/demo/release.py').write_text('__version__ = "2.4"\n')
            (source / 'requirements.txt').write_text('# comment\nfoo >= 2  # comment\nbar\n')
            (source / 'pyproject.toml').write_text('[build-system]\nrequires=["setuptools"]\n'
                '[project]\nname="demo"\ndynamic=["version", "dependencies"]\n'
                '[tool.setuptools.dynamic]\nversion={attr="demo.release.__version__"}\n'
                'dependencies={file="requirements.txt"}\n')
            _, project = m.python_metadata(source, '', default_environment())
            self.assertEqual(project['version'], '2.4')
            self.assertEqual(project['requirements'], ['foo >= 2', 'bar'])

    def test_setuptools_dynamic_version_follows_literal_alias(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'yamllint').mkdir()
            (source / 'yamllint/__init__.py').write_text("APP_VERSION = '1.38.0'\n__version__ = APP_VERSION\n")
            (source / 'pyproject.toml').write_text('[build-system]\nrequires=["setuptools"]\n'
                '[project]\nname="yamllint"\ndynamic=["version"]\n'
                '[tool.setuptools.dynamic]\nversion={attr="yamllint.__version__"}\n')
            _, project = m.python_metadata(source, '', default_environment())
            self.assertEqual(project['version'], '1.38.0')
    def test_patched_declarations_override_stale_sdist_dependencies(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text('[build-system]\nrequires=["flit_core"]\n[project]\nname="demo"\nversion="2"\ndependencies=["example>=2"]\n')
            (source / 'PKG-INFO').write_text('Name: demo\nVersion: 1\nRequires-Dist: example<2\n')
            reqs, project = m.python_metadata(source, '', default_environment())
            self.assertIn('python3dist(example) >= 2', reqs)
            self.assertNotIn('python3dist(example) < 2~~', reqs)
            self.assertEqual(project['version'], '2')

    def test_versioned_interpreter_uses_repository_markers(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text('[build-system]\nrequires=["flit_core"]\n[project]\nname="demo"\nversion="1"\ndependencies=[\'old; python_version < "3.13"\']\n')
            environment = {**default_environment(), '_repository_python_versions': {'3': '3.14.0', '3.12': '3.12.9'}}
            reqs, _ = m.python_metadata(source, '--python3_pkgversion 3.12', environment)
            self.assertIn('python3.12dist(old)', reqs)
            with self.assertRaisesRegex(m.SourceMetadataError, 'no target python3.11'):
                m.python_metadata(source, '--python3_pkgversion 3.11', environment)

    def test_python_bounds_extras_and_target_markers(self):
        environment = {**default_environment(), 'platform_machine': 'aarch64', 'python_version': '3.14', 'python_full_version': '3.14.0'}
        reqs = m.python_requirement('sphinx[test]>=7,<9', environment)
        self.assertEqual(len(reqs), 2)
        self.assertIn(' with ', reqs[0])
        self.assertIn('python3dist(sphinx[test])', reqs[1])
        self.assertEqual(m.python_requirement('arm-tool; platform_machine == "x86_64"', environment), [])
        self.assertEqual(m.python_requirement('arm-tool; platform_machine == "aarch64"', environment), ['python3dist(arm-tool)'])

    def test_setup_is_read_without_executing_it(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('raise RuntimeError("MUST NOT RUN")\nfrom setuptools import setup\ndeps=["sphinx>=7"]\nsetup(name="example",version="1",install_requires=deps,extras_require={"test":["pytest<9"]})\n')
            reqs, project = m.python_metadata(source, '-x test', default_environment())
            self.assertIn('python3dist(sphinx) >= 7', reqs)
            self.assertIn('python3dist(pytest) < 9~~', reqs)
            self.assertEqual(project['name'], 'example')

    def test_setup_metadata_reads_json_and_requirement_file_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / '_version.json').write_text('{"package_name":"ldap3","version":"2.9"}')
            (source / 'requirements.txt').write_text('pyasn1>=0.4.6\n')
            (source / 'setup.py').write_text(
                'from json import load\n'
                'version_dict = load(open("_version.json", "r"))\n'
                'package_name = str(version_dict["package_name"])\n'
                'version = str(version_dict["version"])\n'
                'setup(name=package_name, version=version, '
                'install_requires=[item.strip() for item in open("requirements.txt").readlines()])\n')
            data = m.setup_metadata(source)
            self.assertEqual(data['name'], 'ldap3')
            self.assertEqual(data['version'], '2.9')
            self.assertEqual(data['install_requires'], ['pyasn1>=0.4.6'])

    def test_setup_metadata_iterates_declared_requirement_file_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'requirements.txt').write_text('requests>=2.32\nurllib3<3\n')
            (source / 'setup.py').write_text(
                'dependencies = [line.strip() for line in open("requirements.txt")]\n'
                'setup(name="demo", version="1.0", install_requires=dependencies)\n')
            metadata = m.setup_metadata(source)
            self.assertEqual(metadata['install_requires'], ['requests>=2.32', 'urllib3<3'])

    def test_python_extra_names_follow_normalization_rules(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.cfg').write_text('[metadata]\nname=demo\nversion=1\n[options.extras_require]\ncode_style=\n pytest\n')
            reqs, project = m.python_metadata(source, '-x code-style', default_environment())
            self.assertIn('python3dist(pytest)', reqs)
            self.assertEqual(project['extras']['code-style'], ['pytest'])

    def test_hatch_requirements_txt_hook_resolves_dynamic_project_extras(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'requirements').mkdir()
            (source / 'requirements/base.txt').write_text('dnspython>=2.7,<3\n')
            (source / 'requirements/aws.txt').write_text('pymongo-auth-aws>=1.3,<2\n')
            (source / 'pyproject.toml').write_text(
                '[build-system]\nrequires=["hatchling", "hatch-requirements-txt"]\n'
                'build-backend="hatchling.build"\n[project]\nname="demo"\n'
                'dynamic=["version", "dependencies", "optional-dependencies"]\n'
                '[tool.hatch.metadata.hooks.requirements_txt]\nfiles=["requirements/base.txt"]\n'
                '[tool.hatch.metadata.hooks.requirements_txt.optional-dependencies]\n'
                'aws=["requirements/aws.txt"]\n')
            requirements, project = m.python_metadata(source, '-x aws', default_environment())
            self.assertIn('(python3dist(dnspython) < 3~~ with python3dist(dnspython) >= 2.7)', requirements)
            self.assertIn('(python3dist(pymongo-auth-aws) < 2~~ with python3dist(pymongo-auth-aws) >= 1.3)', requirements)
            self.assertEqual(project['extras']['aws'], ['pymongo-auth-aws>=1.3,<2'])

    def test_no_guess_for_unknown_dependency_expression(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('setup(name="example",install_requires=get_dependencies())')
            with self.assertRaises(m.SourceMetadataError):
                m.setup_metadata(source)

    def test_dynamic_python_spec_reads_verified_source_and_emits_provides(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            package = root / 'python-demo'
            package.mkdir()
            blob = b'[build-system]\nrequires=["flit_core>=3.4,<4"]\nbuild-backend="flit_core.buildapi"\n[project]\nname="demo"\nversion="1.2"\ndependencies=["sphinx[test]>=7,<9"]\n[project.optional-dependencies]\ntest=["pytest>=8"]\n'
            archive = package / 'demo.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('demo-1.2/pyproject.toml')
                member.size = len(blob)
                stream.addfile(member, io.BytesIO(blob))
            spec = package / 'python-demo.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() + '\n')
            expanded = 'Source0: demo.tar.gz\n%generate_buildrequires\n/usr/bin/python3 /usr/lib/rpm/redhat/pyproject_buildrequires.py -x test\n%build\n'
            binary = {'name': 'python3-demo', 'provides': [], 'requires': []}
            reqs = m.evaluate(o, spec, 'aarch64', expanded, [binary], '3.14.0')
            self.assertIn('python3dist(pytest) >= 8', reqs)
            self.assertIn('python3dist(demo) = 1.2', binary['provides'])
            self.assertIn('python3.14dist(demo) = 1.2', binary['provides'])
            self.assertIn('python(abi) = 3.14', binary['requires'])
            self.assertTrue(any('sphinx[test]' in r for r in reqs))
            generated = expanded.replace('%generate_buildrequires\n',
                '%generate_buildrequires -p\nexport SETUPTOOLS_SCM_PRETEND_VERSION="1.2"\n%build -p\n%conf\n%generate_buildrequires\n')
            self.assertEqual(m.evaluate(o, spec, 'aarch64', generated, [], '3.14.0'), reqs)
            renamed = {'name': 'python3-winrm', 'provides': [], 'requires': []}
            renamed_source = blob.replace(b'name="demo"', b'name="pywinrm"')
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('demo-1.2/pyproject.toml')
                member.size = len(renamed_source)
                stream.addfile(member, io.BytesIO(renamed_source))
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() + '\n')
            owned = expanded + '%files -n python3-winrm -f /tmp/package-pyproject-files\n'
            m.evaluate(o, spec, 'aarch64', owned, [renamed], '3.14.0')
            self.assertIn('python3dist(pywinrm) = 1.2', renamed['provides'])
            spec.write_text('%global source0_hash ' + '0' * 64 + '\n')
            with self.assertRaisesRegex(m.SourceMetadataError, 'does not match'):
                m.evaluate(o, spec, 'aarch64', expanded, [binary], '3.14.0')

    def test_python_source_project_maps_to_binary_by_declared_dist_provide(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text(
                '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
                '[project]\nname="libpass"\nversion="1.9.3"\n')
            binary = {'name': 'python3-passlib',
                      'provides': ['python3dist(passlib) = 1.9.3', 'python3dist(libpass) = 1.9.3'],
                      'requires': []}
            m.add_python_source_provides(source, '', [binary], default_environment(), '%pyproject_install\n')
            self.assertIn('python3dist(libpass) = 1.9.3', binary['provides'])

    def test_python_source_project_prefers_exact_binary_name_over_shared_variant_provides(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text(
                '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
                '[project]\nname="h5py"\nversion="3.16.0"\n')
            binaries = [
                {'name': 'python3-h5py', 'provides': ['python3dist(h5py) = 3.16.0'], 'requires': []},
                {'name': 'python3-h5py-openmpi', 'provides': ['python3dist(h5py) = 3.16.0'], 'requires': []},
                {'name': 'python3-h5py-mpich', 'provides': ['python3dist(h5py) = 3.16.0'], 'requires': []},
            ]
            m.add_python_source_provides(source, '', binaries, default_environment(), '%pyproject_install\n')
            self.assertIn('python3dist(h5py) = 3.16', binaries[0]['provides'])
            self.assertNotIn('python3dist(h5py) = 3.16', binaries[1]['provides'])
            self.assertNotIn('python3dist(h5py) = 3.16', binaries[2]['provides'])

    def test_python_source_project_does_not_guess_renamed_binary_without_dist_provide(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text(
                '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
                '[project]\nname="libpass"\nversion="1.9.3"\n')
            binary = {'name': 'python3-passlib', 'provides': [], 'requires': []}
            with self.assertRaisesRegex(m.SourceMetadataError, 'Cannot uniquely match'):
                m.add_python_source_provides(source, '', [binary], default_environment(), '%pyproject_install\n')

    def test_python_source_project_uses_unique_binary_owning_python_payload(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text(
                '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
                '[project]\nname="yamllint"\nversion="1.38.0"\n')
            binary = {'name': 'yamllint',
                      'provides': ['/usr/lib/python3.14/site-packages/yamllint'], 'requires': []}
            m.add_python_source_provides(source, '', [binary], default_environment(), '%pyproject_install\n')
            self.assertIn('python3dist(yamllint) = 1.38', binary['provides'])

    def test_tox_extras_and_declared_groups(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'pyproject.toml').write_text('[build-system]\nrequires=["flit_core"]\nbuild-backend="flit_core.buildapi"\n[project]\nname="demo"\nversion="1"\n[project.optional-dependencies]\ntest=["pytest>=8"]\n[dependency-groups]\nbase=["coverage<8"]\ndocs=[{include-group="base"},"sphinx>=7"]\n')
            (source / 'tox.ini').write_text('[testenv]\nextras=test\ndeps=\n pytest-timeout\n py313: old-only\n py314: current-only\n')
            environment = {**default_environment(), 'python_version': '3.14'}
            reqs, _ = m.python_metadata(source, '-t -g docs', environment, 'py314')
            self.assertIn('python3dist(pytest) >= 8', reqs)
            self.assertIn('python3dist(current-only)', reqs)
            self.assertNotIn('python3dist(old-only)', reqs)
            self.assertIn('python3dist(coverage) < 8~~', reqs)
            self.assertIn('python3dist(tox-current-env) >= 0.0.16', reqs)

    def test_prep_dependency_file_transformations_are_applied(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            package = root / 'pkg'
            package.mkdir()
            blob = b'pytest~=8.3\ncoverage==7\n'
            archive = package / 'source.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('demo/requirements.txt')
                member.size = len(blob)
                stream.addfile(member, io.BytesIO(blob))
            spec = package / 'demo.spec'
            spec.write_text('Name: demo\n')
            expanded = "Source0: source.tar.gz\n%prep\nsed -e '/pytest~=/s/~=8\\.3//' -e 's|==.*||' -e '/coverage/d' requirements.txt > generated.txt\n%generate_buildrequires\necho example\n"
            directory = root / 'work'
            directory.mkdir()
            tree = m.source_tree(o, spec, expanded, directory)
            self.assertEqual((tree / 'generated.txt').read_text(), 'pytest\n')

    def test_source_tree_applies_addressed_sed_hold_restore_to_setup_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            package = root / 'koji'
            package.mkdir()
            blob = b'packages = [\n    "koji",\n    "util/koji",\n    "koji_cli_plugins",\n]\n'
            archive = package / 'koji.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('koji-1.36.1/setup.py')
                member.size = len(blob)
                stream.addfile(member, io.BytesIO(blob))
            spec = package / 'koji.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() + '\n')
            expanded = ('Source0: koji.tar.gz\n%prep\n%autosetup -n koji-1.36.1\n'
                        "sed -e '/util\\/koji/g' -e '/koji_cli_plugins/g' -i setup.py\n"
                        '%generate_buildrequires\n')
            work = root / 'work'
            work.mkdir()
            tree = m.source_tree(o, spec, expanded, work)
            setup = (tree / 'setup.py').read_text()
            self.assertIn('"koji",', setup)
            self.assertNotIn('util/koji', setup)
            self.assertNotIn('koji_cli_plugins', setup)

    def test_source_tree_applies_numeric_line_deletion_to_dependency_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            package = root / 'ansible-lint'
            package.mkdir()
            blob = b'[project]\ndependencies = [\n  "ansible-core>=2.16",\n  "black>=25",\n]\n'
            archive = package / 'ansible-lint.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                member = tarfile.TarInfo('ansible-lint-1.0/pyproject.toml')
                member.size = len(blob)
                stream.addfile(member, io.BytesIO(blob))
            spec = package / 'ansible-lint.spec'
            spec.write_text('%global source0_hash ' + hashlib.sha256(archive.read_bytes()).hexdigest() + '\n')
            expanded = ("Source0: ansible-lint.tar.gz\n%prep\n%autosetup -n ansible-lint-1.0\n"
                        "sed -i '3d' pyproject.toml\n%generate_buildrequires\n")
            work = root / 'work'
            work.mkdir()
            tree = m.source_tree(o, spec, expanded, work)
            text = (tree / 'pyproject.toml').read_text()
            self.assertNotIn('ansible-core', text)
            self.assertIn('black>=25', text)

    def test_setup_version_is_read_from_its_declared_module(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'version.py').write_text('__version__ = "1.3.7"\n')
            (source / 'setup.py').write_text('''import re
with open("version.py") as f:
 data=f.read()
 match=re.search(r'__version__ = "([^"]+)"', data)
 VERSION=match.group(1)
setup(name="demo",version=VERSION,extras_require={"test":["pytest"]})
''')
            data = m.setup_metadata(source)
            self.assertEqual(data['version'], '1.3.7')
            self.assertEqual(data['extras_require'], {'test': ['pytest']})

    def test_setup_exec_reads_only_literal_metadata_from_declared_file(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'googleapiclient').mkdir()
            (source / 'googleapiclient/version.py').write_text(
                '__version__ = "2.192.0"\nraise RuntimeError("must not execute")\n')
            (source / 'setup.py').write_text('''import os
package_root = os.path.abspath(os.path.dirname(__file__))
version = {}
with open(os.path.join(package_root, "googleapiclient/version.py")) as fp:
    exec(fp.read(), version)
setup(name="google-api-python-client", version=version["__version__"],
      install_requires=["httplib2>=0.19.0,<1.0.0", "google-auth>=1.32.0,<3.0.0"])
''')
            data = m.setup_metadata(source)
            self.assertEqual(data['version'], '2.192.0')
            self.assertEqual(data['install_requires'], ['httplib2>=0.19.0,<1.0.0',
                                                        'google-auth>=1.32.0,<3.0.0'])

    def test_setup_cfg_version_attribute_uses_src_package_layout(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'src/flake8').mkdir(parents=True)
            (source / 'src/flake8/__init__.py').write_text('__version__ = "7.3.0"\n')
            (source / 'setup.cfg').write_text('[metadata]\nname=flake8\nversion=attr: flake8.__version__\n'
                                               '[options]\npackage_dir =\n    =src\n')
            self.assertEqual(m.setup_metadata(source)['version'], '7.3.0')

    def test_setup_version_follows_local_imports_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'demo').mkdir()
            (source / 'demo/__init__.py').write_text('from .version import __version__\n')
            (source / 'demo/version.py').write_text('__version__ = "2.3"\nraise RuntimeError("must not execute")\n')
            (source / 'setup.py').write_text('def build():\n from demo import __version__ as version\n setup(name="demo", version=version)\n')
            self.assertEqual(m.setup_metadata(source)['version'], '2.3')

    def test_setup_version_follows_imported_module_attribute_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'version.py').write_text('strictversion = "1.3.2"\nraise RuntimeError("must not execute")\n')
            (source / 'setup.py').write_text('def get_version():\n import version\n return version.strictversion\n'
                                             '__version__ = get_version()\nsetup(name="demo", version=__version__)\n')
            self.assertEqual(m.setup_metadata(source)['version'], '1.3.2')

    def test_unresolvable_dynamic_version_does_not_hide_declared_dependencies(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('def get_version():\n return generated.version\n'
                                             'setup(name="demo", version=get_version(), install_requires=["numpy"])\n')
            metadata = m.setup_metadata(source)
            self.assertNotIn('version', metadata)
            self.assertEqual(metadata['install_requires'], ['numpy'])

    def test_mutated_setup_dependencies_are_not_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('deps=[]\ndeps.append("required-package")\nsetup(name="demo",install_requires=deps)\n')
            with self.assertRaisesRegex(m.SourceMetadataError, 'Mutated setup dependency'):
                m.setup_metadata(source)

    def test_conditional_setup_dependency_append_does_not_affect_other_build_variants(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('requirements = ["numpy"]\n'
                                             'if platform_enabled():\n requirements.append("mpi4py >= 4")\n'
                                             'setup(name="demo", version="1", install_requires=requirements)\n')
            self.assertEqual(m.setup_metadata(source)['install_requires'], ['numpy'])

    def test_conditional_setup_keyword_mapping_without_dependency_fields(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text(
                'try:\n import setuptools\nexcept ImportError:\n'
                ' extra = {"scripts": ["bin/demo"]}\nelse:\n'
                ' extra = {"entry_points": {"console_scripts": ["demo = pkg:main"]}}\n'
                'setup(name="demo", version="1", **extra)\n')
            self.assertEqual(m.setup_metadata(source)['version'], '1')

    def test_setup_static_python_version_dependencies(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('import sys\nrequires = []\n'
                'if sys.version_info[0] >= 3 and sys.version_info[1] >= 8:\n requires.append("distro")\n'
                'if sys.version_info[0] >= 3 and sys.version_info[1] >= 13:\n requires.append("passlib")\n'
                'setup(name="demo", version="1", install_requires=requires)\n')
            env = default_environment()
            env['python_full_version'] = '3.12.0'
            self.assertEqual(m.setup_metadata(source, env)['install_requires'], ['distro'])
            env['python_full_version'] = '3.13.0'
            self.assertEqual(m.setup_metadata(source, env)['install_requires'], ['distro', 'passlib'])

    def test_setup_extra_accepts_single_dependency_string(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('from setuptools import setup\n'
                'setup(name="demo", version="1", extras_require={"arrays": "numpy"})\n')
            _, project = m.python_metadata(source, '-x arrays', default_environment())
            self.assertEqual(project['extras'], {'arrays': ['numpy']})

    def test_setup_reads_filtered_requirements_file_without_execution(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'requirements.txt').write_text('# comment\npytest>=8\n')
            (source / 'setup.py').write_text('from setuptools import setup\n'
                'with open("requirements.txt") as requirements:\n'
                ' INSTALL_REQUIRES = [line.strip() for line in requirements.readlines() if not line.startswith("#")]\n'
                'setup(name="demo", version="1", install_requires=INSTALL_REQUIRES)\n')
            self.assertEqual(m.setup_metadata(source)['install_requires'], ['pytest>=8'])

    def test_setup_dependency_uses_tuple_python_version_condition(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('import sys\n'
                'OLD_PYTHON = sys.version_info < (2, 7)\n'
                'setup(name="demo", version="1", install_requires=["argparse"] if OLD_PYTHON else [])\n')
            env = default_environment()
            env['python_full_version'] = '3.14.0'
            self.assertEqual(m.setup_metadata(source, env)['install_requires'], [])

    def test_python_binary_provides_are_read_from_verified_project_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('from setuptools import setup\nsetup(name="numexpr", version="2.14.2")\n')
            binaries = [{'name': 'python3-numexpr', 'version': '2.14.2', 'provides': []},
                        {'name': 'python3-numexpr+docs', 'version': '2.14.2', 'provides': []}]
            m.add_python_source_provides(source, '', binaries, default_environment(), '%py3_install\n')
            self.assertIn('python3dist(numexpr) = 2.14.2', binaries[0]['provides'])
            self.assertEqual(binaries[1]['provides'], [])

    def test_setup_ignores_only_proven_non_dependency_mapping_keys(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)
            (source / 'setup.py').write_text('options = {}\noptions["package_data"] = {"demo": ["*.txt"]}\nmore = {}\nmore["zip_safe"] = False\noptions.update(more)\nsetup(name="demo", version="1", **options)\n')
            self.assertEqual(m.setup_metadata(source)['name'], 'demo')
            (source / 'setup.py').write_text('options = {}\noptions["install_requires"] = ["hidden"]\nsetup(name="demo", version="1", **options)\n')
            with self.assertRaisesRegex(m.SourceMetadataError, 'Mutated setup dependency'):
                m.setup_metadata(source)

    def test_download_failure_has_no_alternate_or_retry(self):
        with tempfile.TemporaryDirectory() as d:
            spec = Path(d) / 'demo.spec'
            spec.write_text('Name: demo\n')
            with patch.object(o, '_fetch_source_archive', return_value=None) as fetch:
                with self.assertRaises(m.SourceMetadataError):
                    m.source_tree(o, spec, 'Source0: https://github.com/a/b/archive/v1.tar.gz\n', Path(d))
            self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
