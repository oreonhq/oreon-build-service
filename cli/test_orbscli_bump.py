# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from cli.orbscli import (
    _fetch_source_archive,
    _generated_buildrequires,
    _refresh_source_hash_guards,
    _spec_source_macros,
    bump_spec_to_version,
)


class SpecBumpTests(unittest.TestCase):
    def test_batched_nvchecker_uses_detected_upstream_provider(self):
        from cli.orbscli import _nvchecker_run_batched

        entries = {"demo": {"source": "regex", "url": "https://example.test/", "regex": r"(.*)"}}
        with patch("cli.orbscli._anitya_index_cached", return_value={"demo": "9.9.9"}), \
             patch("cli.orbscli._nvchecker_run", return_value={"demo": "2.0.0"}) as run:
            result = _nvchecker_run_batched(entries)
        self.assertEqual(result, {"demo": "2.0.0"})
        run.assert_called_once_with(entries)

    def test_batched_nvchecker_falls_back_to_index_when_upstream_has_no_version(self):
        from cli.orbscli import _nvchecker_run_batched

        entries = {"demo": {"source": "regex", "url": "https://example.test/", "regex": r"(.*)"}}
        with patch("cli.orbscli._anitya_index_cached", return_value={"demo": "2.0.0"}), \
             patch("cli.orbscli._nvchecker_run", return_value={}) as run:
            result = _nvchecker_run_batched(entries)
        self.assertEqual(result, {"demo": "2.0.0"})
        run.assert_called_once_with(entries)

    def test_batched_nvchecker_uses_index_only_without_detected_provider(self):
        from cli.orbscli import _nvchecker_run_batched

        entries = {"demo": {"source": "anitya-index"}}
        with patch("cli.orbscli._anitya_index_cached", return_value={"demo": "2.0.0"}), \
             patch("cli.orbscli._nvchecker_run") as run:
            result = _nvchecker_run_batched(entries)
        self.assertEqual(result, {"demo": "2.0.0"})
        run.assert_not_called()

    def test_github_source_failure_can_resolve_to_latest_published_release(self):
        from cli.orbscli import _github_latest_release_version, _github_stable_release_for_spec

        def redirect(request):
            if request.url.path.endswith("/releases/latest"):
                return httpx.Response(302, headers={"Location": "/canonical/snapd/releases/tag/2.77.1"}, request=request)
            return httpx.Response(200, request=request)

        transport = httpx.MockTransport(redirect)
        client = httpx.Client(transport=transport, follow_redirects=True)
        try:
            with patch("cli.orbscli.httpx.Client", return_value=client):
                version = _github_latest_release_version(
                    "https://github.com/canonical/snapd/releases/download/2.78/snapd_2.78.vendor.tar.xz"
                )
        finally:
            client.close()
        self.assertEqual(version, "2.77.1")
        self.assertIsNone(_github_latest_release_version("https://example.test/x/y/releases/download/2.0/source.tar.gz"))

    def test_hyphenated_github_tag_uses_latest_stable_release(self):
        from cli.orbscli import _github_stable_release_for_spec

        def redirect(request):
            if request.url.path.endswith("/releases/latest"):
                return httpx.Response(302, headers={"Location": "/owner/demo/releases/tag/1.2.0"}, request=request)
            return httpx.Response(200, request=request)

        client = httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True)
        spec = "Name: demo\nVersion: 1.0\nSource0: https://github.com/owner/demo/archive/%{version}.tar.gz\n"
        try:
            with patch("cli.orbscli.httpx.Client", return_value=client):
                result = _github_stable_release_for_spec("demo", spec, "1.3.0-rc1")
        finally:
            client.close()
        self.assertEqual(result, "1.2.0")

    def test_gnome_archive_series_limits_versions_to_its_source_series(self):
        from cli.orbscli import _best_regex_version, _nvchecker_guess_entry

        spec = "Name: gtk3\nVersion: 3.24.41\nSource0: https://download.gnome.org/sources/gtk/3.24/gtk-%{version}.tar.xz\n"
        entry = _nvchecker_guess_entry("gtk3", spec)
        self.assertEqual(entry["regex"], r'"(3\.24(?:\.\d+)*)"')
        self.assertEqual(_best_regex_version('"3.24.51" "4.24.0"', entry["regex"]), "3.24.51")

    def test_gnome_source_series_tracks_a_version_bump(self):
        spec = "Name: pango\nVersion: 1.58.2\nSource0: https://download.gnome.org/sources/pango/1.57/pango-%{version}.tar.xz\n"
        updated = bump_spec_to_version(spec, "1.90.0")[0]
        self.assertIn("Source0: https://download.gnome.org/sources/pango/1.90/pango-%{version}.tar.xz", updated)

    def test_numeric_padding_is_not_a_new_upstream_release(self):
        from cli.orbscli import _spec_bump_compute

        spec = 'Name: pycparser\nVersion: 3.0\nRelease: 1\n'
        status, reason, old, latest = _spec_bump_compute(
            'python-pycparser', Path('python-pycparser.spec'), spec, '3.00')
        self.assertEqual((status, reason, old, latest), ('skip', 'current', '3.0', '3.00'))

    def test_upstream_hyphen_version_is_not_written_to_rpm_version(self):
        from cli.orbscli import _spec_bump_compute

        spec = "Name: demo\nVersion: 1.2.3\nRelease: 1\n"
        self.assertIsNone(bump_spec_to_version(spec, "1.2.4-rc1"))
        status, reason, _, _ = _spec_bump_compute("demo", Path("demo.spec"), spec, "1.2.4-rc1")
        self.assertEqual(status, "fail")
        self.assertIn("RPM-safe", reason)

    def test_only_owned_version_and_remote_source_change(self):
        spec = """%global unrelated 1.2.3
%global major 1.2
# old release 1.2.3
Name: demo
Version: 1.2.3
Release: 8
Source0: https://example.test/tag/v1.2.3/demo-v1.2.3.tar.gz # 1.2.3
Source1: demo-1.2.3.patch
Obsoletes: other < 1.2.3
Requires: other = 1.2.3
%description
Previously 1.2.3.
"""
        result = bump_spec_to_version(spec, "1.2.4")
        self.assertIsNotNone(result)
        updated = result[0]
        self.assertIn("Version: 1.2.4", updated)
        self.assertIn("tag/v1.2.4/demo-v1.2.4.tar.gz # 1.2.3", updated)
        for line in ("%global unrelated 1.2.3", "%global major 1.2", "# old release 1.2.3",
                     "Source1: demo-1.2.3.patch", "Obsoletes: other < 1.2.3",
                     "Requires: other = 1.2.3", "Previously 1.2.3."):
            self.assertIn(line, updated)

    def test_version_owner_macro(self):
        spec = "%global pkgver 1.2.3\n%global abi 1.2.3\nVersion: %{pkgver}\nRelease: 2\n"
        updated = bump_spec_to_version(spec, "1.2.4")[0]
        self.assertIn("%global pkgver 1.2.4", updated)
        self.assertIn("%global abi 1.2.3", updated)

    def test_html_is_not_an_archive_or_signature(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<!DOCTYPE html><html>login</html>"))
        with httpx.Client(transport=transport) as client:
            for name in ("source.tar.bz2", "source.zip", "source.asc"):
                self.assertIsNone(_fetch_source_archive(client, "https://example.test/" + name, None))

    def test_signature_source_gets_hash_guard(self):
        data = b"-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n"
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=data))
        spec = "Name: demo\nVersion: 1\nSource1: https://example.test/demo.asc\n"
        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=transport) as client:
            result, error = _refresh_source_hash_guards(client, spec, _spec_source_macros(spec, "1", "demo"), Path(directory))
        self.assertIsNone(error)
        self.assertIn("%global source1_hash " + hashlib.sha256(data).hexdigest(), result)

    def test_real_tarball_is_accepted(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            data = b"hello"
            info = tarfile.TarInfo("hello.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        payload = buffer.getvalue()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
        with httpx.Client(transport=transport) as client:
            got = _fetch_source_archive(client, "https://example.test/source.tar.gz", None)
        self.assertIsNotNone(got)
        path, digest = got
        self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
        path.unlink()

    def test_crates_download_urls_have_distinct_cached_metadata(self):
        def crate(name, dependency):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                data = (f'[package]\nname = "{name}"\nversion = "1.0.0"\n'
                        f'[dependencies]\n{dependency} = "1"\n'
                        f'[build-dependencies]\n{dependency}-build = "2"\n').encode()
                info = tarfile.TarInfo(f"{name}-1.0.0/Cargo.toml")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            return buffer.getvalue()

        archives = {"one": crate("one", "alpha"), "two": crate("two", "beta")}
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=archives[request.url.path.split("/")[1]])
        )
        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=transport) as client:
            home = Path(directory)
            specs = []
            for name in archives:
                folder = home / name
                folder.mkdir()
                spec = folder / f"{name}.spec"
                spec.write_text("Name: " + name + "\nVersion: 1.0.0\n%generate_buildrequires\n%cargo_generate_buildrequires\n")
                specs.append(spec)
            with patch.object(Path, "home", return_value=home), \
                 patch("cli.orbscli._GENBR_CLIENT", client), \
                 patch("cli.orbscli._rpmspec_http_sources", side_effect=lambda spec, load: [
                     f"https://example.test/{spec.parent.name}/download#/{spec.parent.name}-1.0.0.crate"
                 ]):
                first = _generated_buildrequires(specs[0])
                second = _generated_buildrequires(specs[1])
        self.assertTrue(any("crate(alpha/default) >= 1.0.0" in item and "< 2.0.0~" in item for item in first))
        self.assertTrue(any("crate(alpha-build/default) >= 2.0.0" in item for item in first))
        self.assertTrue(any("crate(beta/default) >= 1.0.0" in item for item in second))
        self.assertNotIn("crate(alpha/default) >= 1", second)


if __name__ == "__main__":
    unittest.main()
