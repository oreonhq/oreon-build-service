# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cabal
import planner as P


def make_spec(name, brs=(), binaries=None, version="1.0", release="1.or11", arches=("x86_64", "aarch64"),
              flags=(), dynamic=False, chain_deps=()):
    spec = P.Spec(name)
    spec.path = "/nonexistent/%s/%s.spec" % (name, name)
    spec.evr = ("0", version, release)
    spec.arches = list(arches)
    spec.brs = {arch: list(brs) for arch in arches}
    names = binaries if binaries is not None else [name]
    spec.binaries = [{"name": item, "evr": ["0", version, release],
                      "provides": [[item, "=", "%s-%s" % (version, release)]], "files": []} for item in names]
    spec.flags = list(flags)
    spec.dynamic = dynamic
    spec.chain_deps = list(chain_deps)
    return spec


def make_repo(provides=None, sources=None):
    repo = P.Repo()
    for name, entries in (provides or {}).items():
        repo.provides[name] = entries
    for name, evrs in (sources or {}).items():
        repo.sources[name] = set(evrs)
    return repo


class FakeTool:
    cli = None
    root = tempfile.gettempdir()
    cache_dir = Path(tempfile.gettempdir())


class TestPlanner(P.Planner):
    alternates = {}

    def own_requirements(self, spec, extra=(), dynamic=None):
        if extra:
            return {arch: list(self.alternates.get(spec.name, ())) for arch in spec.arches}
        return super().own_requirements(spec, extra, dynamic)


class MemStore(dict):
    @property
    def full(self):
        return self

    def load(self, name):
        return self[name]

    def done(self, name, repos):
        return P.published(self[name], repos)

    def evr(self, name):
        spec = self[name]
        return (spec.evr, getattr(spec, "rpm_name", None) or name, "")


def build(specs, repo=None, alternates=None):
    repos = {"x86_64": repo or make_repo(), "aarch64": repo or make_repo()}
    table = MemStore({spec.name: spec for spec in specs})
    planner = TestPlanner(FakeTool(), table, repos, ["x86_64", "aarch64"], dynamic=False)
    planner.alternates = alternates or {}
    return planner


class AtomTests(unittest.TestCase):
    def test_evr(self):
        self.assertEqual(P.evr_tuple("2:1.4-3.or11"), ("2", "1.4", "3.or11"))
        self.assertEqual(P.evr_tuple("1.4"), ("0", "1.4", ""))

    def test_compare_ignores_missing_release(self):
        self.assertEqual(P.evr_compare(("0", "1.0", "5"), ("0", "1.0", "")), 0)
        self.assertGreater(P.evr_compare(("0", "1.1", "1"), ("0", "1.0", "")), 0)

    def test_split_atom(self):
        self.assertEqual(P.split_atom("pkgconfig(glib-2.0) >= 2.52"), ("pkgconfig(glib-2.0)", ">=", ("0", "2.52", "")))
        self.assertEqual(P.split_atom("gcc"), ("gcc", None, None))

    def test_repo_versions(self):
        repo = make_repo({"foo": [("=", ("0", "1.5", "1.or11"))], "bare": [(None, None)]})
        self.assertTrue(repo.satisfies("foo", ">=", ("0", "1.4", "")))
        self.assertFalse(repo.satisfies("foo", ">=", ("0", "1.6", "")))
        self.assertTrue(repo.satisfies("bare", ">=", ("0", "9", "")))
        self.assertFalse(repo.satisfies("missing", None, None))

    def test_rich_tokenizer_keeps_parens_in_names(self):
        tokens = P.tokenize_rich("(pkgconfig(foo) >= 1 or (bar if baz))")
        self.assertEqual(tokens[:3], ["(", "pkgconfig(foo)", ">="])
        self.assertIn("baz", tokens)

    def test_rich_or_uses_repo_first(self):
        repo = make_repo({"a": [(None, None)]})
        planner = build([make_spec("x")], repo)
        result = planner.resolver.requirement("(a or b)", "x86_64")
        self.assertTrue(result.repo_ok)
        self.assertFalse(result.unresolved)

    def test_rich_and_needs_both(self):
        repo = make_repo({"a": [(None, None)]})
        planner = build([make_spec("x")], repo)
        result = planner.resolver.requirement("(a and b)", "x86_64")
        self.assertEqual(result.unresolved, {"b"})

    def test_rich_with_needs_single_provider_for_both_clauses(self):
        repo = make_repo({"crate(term/default)": [("=", ("0", "1.2.1", ""))]})
        specs = [make_spec("rust-app", ["(crate(term/default) >= 0.7.0 with crate(term/default) < 0.8.0~)"]),
                 make_spec("rust-term0.7", binaries=["rust-term0.7+default-devel"], version="0.7.0")]
        plan = build(specs, repo).plan(["rust-app", "rust-term0.7"])
        self.assertEqual(plan.chain, "rust-term0.7 : rust-app")

    def test_rich_with_satisfied_by_one_provider(self):
        repo = make_repo({"crate(term/default)": [("=", ("0", "0.7.0", ""))]})
        specs = [make_spec("rust-app", ["(crate(term/default) >= 0.7.0 with crate(term/default) < 0.8.0~)"])]
        plan = build(specs, repo).plan(["rust-app"])
        self.assertEqual(plan.chain, "rust-app")

    def test_rich_with_python_dist_range(self):
        repo = make_repo({"python3dist(cssselect)": [("=", ("0", "2.0", ""))]})
        specs = [make_spec("python-cssselect", binaries=["python3-cssselect"], version="1.5"),
                 make_spec("app", ["(python3dist(cssselect) < 2~~ with python3dist(cssselect) >= 1.2)"])]
        plan = build(specs, repo).plan(["app", "python-cssselect"])
        self.assertEqual(plan.chain, "python-cssselect : app")

    def test_rich_with_crate_default_feature_fallback(self):
        repo = make_repo({"crate(foldhash)": [("=", ("0", "0.2.0", ""))]})
        specs = [make_spec("rust-app", ["(crate(foldhash) >= 0.1.2 with crate(foldhash) < 0.2.0~)"]),
                 make_spec("rust-foldhash0.1", binaries=["rust-foldhash0.1-devel"], version="0.1.5")]
        plan = build(specs, repo).plan(["rust-app", "rust-foldhash0.1"])
        self.assertEqual(plan.chain, "rust-foldhash0.1 : rust-app")


class OrderTests(unittest.TestCase):
    def test_provider_first(self):
        specs = [make_spec("app", ["libfoo-devel"]), make_spec("libfoo", binaries=["libfoo", "libfoo-devel"])]
        plan = build(specs).plan(["app", "libfoo"])
        self.assertEqual(plan.chain, "libfoo : app")

    def test_repo_satisfied_has_no_edge(self):
        repo = make_repo({"libfoo-devel": [("=", ("0", "1.0", "1.or11"))]},
                         sources={"libfoo": {("0", "1.0", "1.or11")}})
        specs = [make_spec("app", ["libfoo-devel >= 1.0"]), make_spec("libfoo", binaries=["libfoo", "libfoo-devel"])]
        plan = build(specs, repo).plan(["app", "libfoo"])
        self.assertEqual(plan.chain, "app")

    def test_repo_satisfied_but_spec_unpublished_makes_edge(self):
        repo = make_repo({"libfoo-devel": [("=", ("0", "1.0", "1.or11"))]})
        specs = [make_spec("app", ["libfoo-devel >= 1.0"], chain_deps=["libfoo"]),
                 make_spec("libfoo", binaries=["libfoo", "libfoo-devel"], version="1.1")]
        plan = build(specs, repo).plan(["app", "libfoo"])
        self.assertEqual(plan.chain, "libfoo : app")

    def test_chain_deps_edge_dropped_when_dep_not_rebuilt(self):
        repo = make_repo({"libfoo-devel": [("=", ("0", "1.0", "1.or11"))]},
                         sources={"libfoo": {("0", "1.1", "1.or11")}})
        specs = [make_spec("app", ["libfoo-devel >= 1.0"], chain_deps=["libfoo"]),
                 make_spec("libfoo", binaries=["libfoo", "libfoo-devel"], version="1.1")]
        plan = build(specs, repo).plan(["app", "libfoo"])
        self.assertEqual(plan.chain, "app")

    def test_version_too_old_in_repo_makes_edge(self):
        repo = make_repo({"libfoo-devel": [("=", ("0", "1.0", "1.or11"))]})
        specs = [make_spec("app", ["libfoo-devel >= 2.0"]),
                 make_spec("libfoo", binaries=["libfoo", "libfoo-devel"], version="2.0")]
        plan = build(specs, repo).plan(["app", "libfoo"])
        self.assertEqual(plan.chain, "libfoo : app")

    def test_seed_pulls_in_provider(self):
        specs = [make_spec("app", ["libfoo-devel"]), make_spec("libfoo", binaries=["libfoo", "libfoo-devel"])]
        plan = build(specs).plan(["app"])
        self.assertEqual(plan.chain, "libfoo : app")

    def test_published_current_is_skipped(self):
        specs = [make_spec("libfoo")]
        repo = make_repo({}, {"libfoo": [("0", "1.0", "1.or11")]})
        plan = build(specs, repo).plan(["libfoo"])
        self.assertEqual(plan.chain, "")
        self.assertIn("libfoo", plan.done)

    def test_spec_newer_than_published_build_rebuilds(self):
        specs = [make_spec("libfoo")]
        repo = make_repo({}, {"libfoo": [("0", "1.0", "1.or11")]})
        planner = build(specs, repo)
        for arch in ("x86_64", "aarch64"):
            planner.repos[arch].build_times[("libfoo", ("0", "1.0", "1.or11"))] = 100
        planner.commit_ts = lambda name: 200
        plan = planner.plan(["libfoo"])
        self.assertEqual(plan.chain, "libfoo")
        self.assertNotIn("libfoo", plan.done)
        self.assertIn("spec changed after the last published build", plan.notes["libfoo"])

    def test_spec_older_than_published_build_stays_done(self):
        specs = [make_spec("libfoo")]
        repo = make_repo({}, {"libfoo": [("0", "1.0", "1.or11")]})
        planner = build(specs, repo)
        for arch in ("x86_64", "aarch64"):
            planner.repos[arch].build_times[("libfoo", ("0", "1.0", "1.or11"))] = 200
        planner.commit_ts = lambda name: 100
        plan = planner.plan(["libfoo"])
        self.assertEqual(plan.chain, "")
        self.assertIn("libfoo", plan.done)

    def test_published_provider_missing_provide_is_rebuilt(self):
        repo = make_repo({"crate(termlib)": [("=", ("0", "1.0", "1.or11"))]},
                         {"rust-termlib": [("0", "1.0", "1.or11")]})
        specs = [make_spec("app", ["(crate(termlib/default) >= 1.0 with crate(termlib/default) < 2.0~)"]),
                 make_spec("rust-termlib", binaries=["rust-termlib-devel", "rust-termlib+default-devel"])]
        plan = build(specs, repo).plan(["app", "rust-termlib"])
        self.assertEqual(plan.chain, "rust-termlib : app")
        self.assertNotIn("rust-termlib", plan.done)
        self.assertIn("lacks provides the current spec generates", plan.notes["rust-termlib"])

    def test_underscore_versioned_crate_matches_base_name(self):
        specs = [make_spec("rust-hash32_0.3", binaries=["rust-hash32_0.3-devel", "rust-hash32_0.3+default-devel"],
                           version="0.3.1")]
        planner = build(specs)
        planner.resolver.refresh(specs[0])
        result = planner.resolver.requirement("(crate(hash32/default) >= 0.3.0 with crate(hash32/default) < 0.4.0~)", "x86_64")
        self.assertEqual(result.owners, {"rust-hash32_0.3"})

    def test_unresolved_blocks_and_cascades(self):
        specs = [make_spec("app", ["libfoo-devel"]), make_spec("top", ["app"], binaries=["top"])]
        specs[0].binaries[0]["name"] = "app"
        plan = build(specs).plan(["app", "top"])
        self.assertEqual(plan.chain, "")
        self.assertIn("app", plan.blocked)
        self.assertTrue(any("nothing provides libfoo-devel" in reason for reason in plan.blocked["app"]))
        self.assertTrue(plan.blocked["top"][0].startswith("blocked prerequisite"))

    def test_blocked_package_does_not_hold_back_others(self):
        specs = [make_spec("good"), make_spec("bad", ["nothing-here"])]
        plan = build(specs).plan(["good", "bad"])
        self.assertEqual(plan.chain, "good")
        self.assertIn("bad", plan.blocked)

    def test_python_dist_provider_by_binary_name(self):
        specs = [make_spec("python-foo", binaries=["python3-foo"]), make_spec("app", ["python3dist(foo) >= 1.0"])]
        plan = build(specs).plan(["app", "python-foo"])
        self.assertEqual(plan.chain, "python-foo : app")

    def test_crate_provider_with_compat_name(self):
        specs = [make_spec("rust-hashbrown0.15", binaries=["rust-hashbrown0.15-devel"], version="0.15.5"),
                 make_spec("rust-app", ["crate(hashbrown) >= 0.15"])]
        plan = build(specs).plan(["rust-app", "rust-hashbrown0.15"])
        self.assertEqual(plan.chain, "rust-hashbrown0.15 : rust-app")

    def test_perl_module_provider(self):
        specs = [make_spec("perl-Foo-Bar", binaries=["perl-Foo-Bar"]), make_spec("perl-Baz", ["perl(Foo::Bar::Deep)"])]
        plan = build(specs).plan(["perl-Baz", "perl-Foo-Bar"])
        self.assertEqual(plan.chain, "perl-Foo-Bar : perl-Baz")

    def test_spec_error_is_reported(self):
        broken = P.Spec("broken")
        broken.error = "line 4: Illegal char"
        plan = build([broken, make_spec("fine")]).plan(["broken", "fine"])
        self.assertEqual(plan.chain, "fine")
        self.assertIn("spec error", plan.blocked["broken"][0])


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.switch = P.has_switch
        P.has_switch = lambda spec: True

    def tearDown(self):
        P.has_switch = self.switch

    def test_bootstrap_without_switch_is_blocked(self):
        P.has_switch = lambda spec: False
        specs = [make_spec("a", ["b"], flags=["bootstrap"]), make_spec("b", ["a"])]
        plan = build(specs, alternates={"a": []}).plan(["a", "b"])
        self.assertIn("oreon_bootstrap", plan.blocked["a"][0])

    def test_bootstrap_lists_package_twice(self):
        specs = [make_spec("a", ["b"], flags=["bootstrap"]), make_spec("b", ["a"])]
        planner = build(specs, alternates={"a": []})
        plan = planner.plan(["a", "b"])
        self.assertEqual(plan.chain, "a : b : a")
        self.assertIn("a", plan.notes)
        self.assertEqual(plan.bootstrap["a"], ["--with", "bootstrap"])

    def test_cycle_without_option_is_blocked_not_dropped_silently(self):
        specs = [make_spec("a", ["b"]), make_spec("b", ["a"])]
        plan = build(specs).plan(["a", "b"])
        self.assertEqual(plan.chain, "")
        self.assertIn("dependency cycle", plan.blocked["a"][0])

    def test_bootstrap_does_not_work_if_cycle_stays(self):
        specs = [make_spec("a", ["b"], flags=["bootstrap"]), make_spec("b", ["a"])]
        planner = build(specs, alternates={"a": ["b"]})
        plan = planner.plan(["a", "b"])
        self.assertEqual(plan.chain, "")

    def test_two_package_bootstrap_ring(self):
        specs = [make_spec("a", ["b"], flags=["perl_bootstrap"]), make_spec("b", ["c"], flags=["perl_bootstrap"]),
                 make_spec("c", ["a"])]
        planner = build(specs, alternates={"a": [], "b": []})
        plan = planner.plan(["a", "b", "c"])
        names = [name for layer in plan.chain.split(" : ") for name in layer.split()]
        self.assertTrue(len(plan.blocked) == 0)
        self.assertEqual(sorted(set(names)), ["a", "b", "c"])
        self.assertGreaterEqual(len(names), 4)

    def test_tarjan(self):
        parts = P.tarjan({"a": {"b"}, "b": {"a"}, "c": {"a"}, "d": set()})
        self.assertIn(["a", "b"], parts)
        self.assertEqual(len(parts), 3)


class GroupTests(unittest.TestCase):
    def test_default_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            groups = P.Groups(folder)
            self.assertEqual(groups.of(make_spec("rust-serde")), "rust-crates")
            self.assertEqual(groups.of(make_spec("python-requests")), "python")
            self.assertEqual(groups.of(make_spec("kf6-kio")), "kde")
            self.assertEqual(groups.of(make_spec("whatever")), "misc")

    def test_file_beats_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "python-foo").mkdir()
            (Path(folder) / "python-foo" / "chain.group").write_text("tools\n")
            self.assertEqual(P.Groups(folder).of(make_spec("python-foo")), "tools")

    def test_split_reports_needs(self):
        specs = [make_spec("rust-a", ["libfoo-devel"]), make_spec("libfoo", binaries=["libfoo", "libfoo-devel"])]
        plan = build(specs).plan(["rust-a", "libfoo"])
        group_of = {"rust-a": "rust-crates", "libfoo": "misc"}
        layers, needs = P.split_by_group(plan, group_of, {"rust-crates"})
        self.assertEqual(layers, [["rust-a"]])
        self.assertEqual(needs, {"misc": ["libfoo"]})
        layers, needs = P.split_by_group(plan, group_of, {"rust-crates"}, with_prereqs=True)
        self.assertEqual(P.chain_text(layers), "libfoo : rust-a")


class FileTests(unittest.TestCase):
    def test_files_provides(self):
        text = (
            "%files devel\n%{_libdir}/libfoo.so\n/usr/lib64/pkgconfig/foo.pc\n/usr/lib64/cmake/Foo/FooConfig.cmake\n"
            "%dir /usr/include/foo\n%exclude /usr/lib64/skip.a\n%files\n/usr/lib64/libfoo.so.3\n/usr/bin/foo\n"
            "%changelog\n* x\n")
        found = P.files_provides(text, "foo", "x86_64")
        self.assertIn("pkgconfig(foo)", found["foo-devel"])
        self.assertIn("cmake(Foo)", found["foo-devel"])
        self.assertIn("libfoo.so.3()(64bit)", found["foo"])
        self.assertIn("/usr/bin/foo", found["foo"])
        self.assertNotIn("/usr/lib64/skip.a", found["foo-devel"])

    def test_binary_name(self):
        self.assertEqual(P.binary_name("devel", "foo"), "foo-devel")
        self.assertEqual(P.binary_name("-n bar-libs", "foo"), "bar-libs")
        self.assertEqual(P.binary_name("", "foo"), "foo")
        self.assertEqual(P.binary_name("-f files.list", "foo"), "foo")

    def test_arch_applies(self):
        self.assertFalse(P.arch_applies("ExclusiveArch: x86_64\n", "aarch64"))
        self.assertTrue(P.arch_applies("ExclusiveArch: x86_64 aarch64\n", "aarch64"))
        self.assertFalse(P.arch_applies("ExcludeArch: aarch64\n", "aarch64"))

    def test_spec_flags(self):
        flags = P.spec_flags("%bcond_with bootstrap\n%bcond check 1\n%bcond_without perl_X_enables_y_test\n")
        self.assertIn("bootstrap", flags)
        self.assertIn("check", flags)
        self.assertIn("bcond:perl_X_enables_y_test", flags)
        self.assertEqual(P.alt_args(make_spec("x", flags=flags)),
                         ["--without", "check", "--with", "bootstrap", "--without", "perl_X_enables_y_test"])


PRIMARY = b"""<?xml version="1.0"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="1">
<package type="rpm"><name>foo-devel</name><arch>x86_64</arch><version epoch="0" ver="1.2" rel="3.or11"/>
<format><rpm:sourcerpm>foo-1.2-3.or11.src.rpm</rpm:sourcerpm>
<rpm:provides><rpm:entry name="foo-devel" flags="EQ" epoch="0" ver="1.2" rel="3.or11"/>
<rpm:entry name="pkgconfig(foo)"/></rpm:provides><file>/usr/bin/foo</file></format></package>
</metadata>"""


class RepoParseTests(unittest.TestCase):
    def test_parse_primary(self):
        repo = P.parse_primary(io.BytesIO(PRIMARY))
        self.assertEqual(repo.sources["foo"], {("0", "1.2", "3.or11")})
        self.assertTrue(repo.satisfies("pkgconfig(foo)", None, None))
        self.assertTrue(repo.satisfies("foo-devel", ">=", ("0", "1.0", "")))
        self.assertTrue(repo.satisfies("/usr/bin/foo", None, None))
        self.assertEqual(repo.binaries["foo-devel"], "foo")

    def test_published(self):
        spec = make_spec("foo", version="1.2", release="3.or11")
        repo = P.parse_primary(io.BytesIO(PRIMARY))
        self.assertTrue(P.published(spec, {"x86_64": repo, "aarch64": repo}))
        self.assertFalse(P.published(make_spec("foo", version="1.3"), {"x86_64": repo, "aarch64": repo}))


class CabalTests(unittest.TestCase):
    def test_constraint_ok(self):
        self.assertFalse(cabal.constraint_ok(">=2.13.1 && <2.19", (2, 19, 0, 0)))
        self.assertTrue(cabal.constraint_ok(">=2.13.1 && <2.19", (2, 14, 3)))
        self.assertTrue(cabal.constraint_ok("==2.11.18", (2, 11, 18)))
        self.assertFalse(cabal.constraint_ok("==2.11.18", (2, 11, 17)))
        self.assertTrue(cabal.constraint_ok("==2.11.*", (2, 11, 18)))
        self.assertFalse(cabal.constraint_ok("==2.11.*", (2, 12, 0)))
        self.assertFalse(cabal.constraint_ok(">= 2.4 && < 2.12", (2, 19, 0, 0)))
        self.assertTrue(cabal.constraint_ok("", (1, 0)))
        self.assertFalse(cabal.constraint_ok("^>=4.16", (5, 0)))

    def test_parse_deps_skips_test_suite(self):
        text = """
library
    build-depends:
        base >=4 && <5
      , QuickCheck >=2.13.1 && <2.19

test-suite t
    type: exitcode-stdio-1.0
    build-depends: hspec

executable e
    build-depends: directory
"""
        deps = cabal.parse_deps(text)
        self.assertEqual(deps["QuickCheck"], ">=2.13.1 && <2.19")
        self.assertNotIn("hspec", deps)
        self.assertEqual(deps["directory"], "")


if __name__ == "__main__":
    unittest.main()
