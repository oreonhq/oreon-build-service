# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import tempfile
import unittest
from pathlib import Path

import click

from cli.specadd import (
    inspect_tree,
    lang_kind_from_tree,
    oreonize_translator_spec,
    refuse_lookaside,
    render_spec,
    templated_source,
    version_from_filename,
)


class SpecAddTests(unittest.TestCase):
    def test_meson_keeps_required_deps_and_drops_optional(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "meson.build").write_text(
                "\n".join([
                    "project('demo', 'c', version: '1.2.3', license: 'MIT',",
                    "  meson_version: '>= 0.50')",
                    "library('demo', 'demo.c', dependencies: dependency('zlib'), install: true)",
                    "install_headers('demo.h')",
                    "dependency('openssl', required: false)",
                    "dependency('libcap', required: get_option('extra'))",
                    "",
                ]),
                encoding="utf-8",
            )
            (root / "meson_options.txt").write_text(
                "option('extra', type: 'feature', value: 'auto', description: 'extra')\n",
                encoding="utf-8",
            )
            (root / "COPYING").write_text("Permission is hereby granted, free of charge\n", encoding="utf-8")
            info = inspect_tree(root)
            text = render_spec(
                "demo", "1.2.3",
                "https://example.com/demo-1.2.3.tar.xz",
                "ab" * 32, info, None,
            )
        self.assertIn("# !!!ADD DEPS!!! !!!ADD DEPS!!! !!!ADD DEPS!!!", text)
        self.assertNotIn("pkgconfig(", text)
        self.assertNotIn("openssl", text)
        self.assertIn("License: MIT", text)
        self.assertIn("%oreon_meson", text)
        self.assertNotIn("auto_features", text)
        self.assertNotIn("QA_RPATHS", text)
        self.assertIn("%package devel", text)
        self.assertIn("Source0: https://example.com/demo-%{version}.tar.xz", text)
        self.assertIn("%license COPYING", text)
        self.assertNotIn("%changelog", text)

    def test_autotools_required_module_and_chown_flag(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "configure.ac").write_text(
                "\n".join([
                    "AC_INIT([widget], [4.0], [bugs@example.com], [widget], [https://example.com/widget])",
                    "AC_PROG_CC",
                    "LT_INIT",
                    "PKG_CHECK_MODULES([GLIB], [glib-2.0 >= 2.0])",
                    "PKG_CHECK_MODULES([FOO], [foo], [have_foo=yes], [have_foo=no])",
                    "AC_ARG_ENABLE([makeinstall-chown], [], [], [enable_makeinstall_chown=yes])",
                    "",
                ]),
                encoding="utf-8",
            )
            (root / "Makefile.am").write_text(
                "lib_LTLIBRARIES = libwidget.la\ninclude_HEADERS = widget.h\n",
                encoding="utf-8",
            )
            (root / "COPYING").write_text(
                "GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007\n",
                encoding="utf-8",
            )
            info = inspect_tree(root)
            text = render_spec(
                "widget", "4.0",
                "https://example.com/widget-4.0.tar.xz",
                "cd" * 32, info, None,
            )
        self.assertIn("# !!!ADD DEPS!!! !!!ADD DEPS!!! !!!ADD DEPS!!!", text)
        self.assertNotIn("pkgconfig(", text)
        self.assertIn("--disable-static", text)
        self.assertNotIn("makeinstall-chown", text)
        self.assertIn("License: GPL-3.0-or-later", text)
        self.assertIn("URL: https://example.com/widget", text)
        self.assertIn("%oreon_configure", text)
        self.assertIn("patchelf", text)

    def test_two_libraries_become_a_subpackage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "configure.ac").write_text("AC_INIT([widget],[1.0])\nLT_INIT\n", encoding="utf-8")
            (root / "Makefile.am").write_text(
                "SUBDIRS = src\n",
                encoding="utf-8",
            )
            (root / "src").mkdir()
            (root / "src" / "Makefile.am").write_text(
                "\n".join([
                    "lib_LTLIBRARIES = libwidget.la libextra.la",
                    "bin_PROGRAMS = widget",
                    "include_HEADERS = widget.h extra.h",
                    "",
                ]),
                encoding="utf-8",
            )
            (root / "COPYING").write_text("Permission is hereby granted, free of charge\n", encoding="utf-8")
            info = inspect_tree(root)
            text = render_spec("widget", "1.0", "https://example.com/widget-1.0.tar.xz", "ab" * 32, info, None)
        self.assertIn("%package -n libextra", text)
        self.assertNotIn("%package -n libwidget", text)
        self.assertIn("%files -n libextra", text)
        self.assertIn("Requires: libextra%{?_isa} = %{version}-%{release}", text)
        self.assertIn("%package devel", text)
        self.assertEqual(info["libs"], ["libwidget", "libextra"])

    def test_cmake_subdir_shared_libs_split(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "CMakeLists.txt").write_text(
                "\n".join([
                    "cmake_minimum_required(VERSION 3.16)",
                    'project(demo VERSION 1.0 LANGUAGES C DESCRIPTION "demo tool")',
                    "add_subdirectory(src)",
                    "add_subdirectory(tests)",
                    "",
                ]),
                encoding="utf-8",
            )
            (root / "src").mkdir()
            (root / "src" / "CMakeLists.txt").write_text(
                "\n".join([
                    "add_library(demo SHARED demo.c)",
                    "add_library(extra SHARED extra.c)",
                    "add_library(demo_obj OBJECT obj.c)",
                    "install(TARGETS demo extra)",
                    "",
                ]),
                encoding="utf-8",
            )
            (root / "tests").mkdir()
            (root / "tests" / "CMakeLists.txt").write_text(
                "add_library(should_skip SHARED skip.c)\ninstall(TARGETS should_skip)\n",
                encoding="utf-8",
            )
            info = inspect_tree(root)
            text = render_spec("demo", "1.0", "https://example.com/demo-1.0.tar.gz", "cd" * 32, info, None)
        self.assertEqual(info["libs"], ["demo", "extra"])
        self.assertIn("%package -n libextra", text)
        self.assertNotIn("should_skip", text)
        self.assertNotIn("%package -n libdemo", text)

    def test_internal_and_builtin_deps_are_not_buildrequires(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "meson.build").write_text(
                "\n".join([
                    "project('demo', 'c', version: '1.0')",
                    "dependency('zlib', required: true)",
                    "dependency('threads')",
                    "dependency('dl')",
                    "meson.override_dependency('blkid', declare_dependency())",
                    "find_program('bison')",
                    "find_program('sed')",
                    "find_program('tests/run.sh')",
                    "dependency('slang', required: get_option('slang'))",
                    "subdir('tests')",
                    "",
                ]),
                encoding="utf-8",
            )
            (root / "meson_options.txt").write_text(
                "option('slang', type: 'feature', value: 'disabled', description: 'slang')\n",
                encoding="utf-8",
            )
            (root / "tests").mkdir()
            (root / "tests" / "meson.build").write_text(
                "dependency('gtest', required: true)\n",
                encoding="utf-8",
            )
            (root / "CMakeLists.txt").write_text("", encoding="utf-8")
            info = inspect_tree(root)
            text = render_spec("demo", "1.0", "https://example.com/demo-1.0.tar.xz", "ab" * 32, info, None)
        self.assertIn("# !!!ADD DEPS!!! !!!ADD DEPS!!! !!!ADD DEPS!!!", text)
        self.assertNotIn("pkgconfig(", text)
        self.assertNotIn("BuildRequires: bison", text)
        self.assertNotIn("BuildRequires: sed", text)

    def test_cmake_pkg_modules_ignore_the_result_variable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "CMakeLists.txt").write_text(
                "\n".join([
                    "cmake_minimum_required(VERSION 3.16)",
                    "project(demo VERSION 1.0 LANGUAGES C)",
                    "pkg_check_modules(PC_GLIB REQUIRED glib-2.0>=2.56 IMPORTED_TARGET)",
                    "pkg_search_module(PC_BAR QUIET bar)",
                    "find_package(Threads)",
                    "add_executable(demo demo.c)",
                    "",
                ]),
                encoding="utf-8",
            )
            info = inspect_tree(root)
            text = render_spec("demo", "1.0", "https://example.com/demo-1.0.tar.gz", "cd" * 32, info, None)
        self.assertIn("# !!!ADD DEPS!!! !!!ADD DEPS!!! !!!ADD DEPS!!!", text)
        self.assertNotIn("pkgconfig(", text)
        self.assertNotIn("BuildRequires: Threads", text)

    def test_lang_kind_skips_c_builds(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
            (root / "package.json").write_text("{}\n", encoding="utf-8")
            self.assertIsNone(lang_kind_from_tree(root, "demo", "https://example.com/demo-1.tar.gz"))
            (root / "Cargo.toml").write_text('[package]\nname = "libc"\n', encoding="utf-8")
            self.assertEqual(lang_kind_from_tree(root, "rust-libc", "https://example.com/libc-1.crate"), ("rust", "libc"))

    def test_translator_spec_keeps_oreon_header(self):
        text = oreonize_translator_spec(
            "\n".join([
                "Name: rust-libc",
                "Version: 0.2.169",
                "Source: %{crates_source}",
                "BuildRequires: rust-packaging",
                "%description",
                "libc",
                "%package devel",
                "Requires: cargo",
                "Provides: crate(libc) = 0.2.169",
                "%description devel",
                "devel",
                "%prep",
                "%autosetup -n %{crate}-%{version} -p1",
                "%changelog",
                "* generated",
                "",
            ]),
            "ab" * 32,
            "https://static.crates.io/crates/libc/libc-0.2.169.crate",
            "0.2.169",
        )
        self.assertIn("%global source0_hash " + "ab" * 32, text)
        self.assertIn("BuildRequires: oreon-rpm-macros", text)
        self.assertIn("BuildRequires: rust-packaging", text)
        self.assertIn("Requires: cargo", text)
        self.assertIn("Provides: crate(libc) = 0.2.169", text)
        self.assertIn("%oreon_sha256", text)
        self.assertNotIn("%changelog", text)
        self.assertNotIn("crates_source", text)
        self.assertIn("libc-%{version}.crate", text)

    def test_refuses_lookaside_and_templates_version_once(self):
        with self.assertRaises(click.ClickException):
            refuse_lookaside("https://src.fedoraproject.org/repo/pkgs/foo/foo.tar.xz")
        self.assertEqual(
            templated_source("https://example.com/a-1.2.3.tar.xz", "1.2.3"),
            "https://example.com/a-%{version}.tar.xz",
        )
        self.assertEqual(
            templated_source("https://example.com/1.2.3/a-1.2.3.tar.xz", "1.2.3"),
            "https://example.com/1.2.3/a-1.2.3.tar.xz",
        )
        self.assertEqual(
            version_from_filename("https://github.com/fastfetch-cli/fastfetch/archive/refs/tags/2.70.0.tar.gz"),
            "2.70.0",
        )
        self.assertEqual(version_from_filename("https://example.com/v2.70.0.tar.gz"), "2.70.0")


if __name__ == "__main__":
    unittest.main()
