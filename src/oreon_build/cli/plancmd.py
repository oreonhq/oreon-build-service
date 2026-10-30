# Copyright (C) 2026 Oreon HQ
# SPDX-License-Identifier: GPL-3.0-or-later

"""orbscli plan commands. Everything here is deterministic, no AI."""
from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import click

try:
    from . import planner as P
    from . import scan as S
except ImportError:
    import planner as P
    import scan as S

CACHE = Path.home() / ".cache" / "orbscli-plan"


def gate_expand(tool, arches, path, new_text, wanted):
    folder = Path(path).parent
    work = Path(tempfile.mkdtemp(prefix="orbscli-bake-"))
    try:
        target = work / folder.name
        target.mkdir()
        for item in folder.iterdir():
            if item.is_file() and item.name != folder.name + ".spec":
                (target / item.name).symlink_to(item)
        (target / (folder.name + ".spec")).write_text(new_text, encoding="utf-8")
        got = {}
        for arch_name in arches:
            lines = tool.run(arch_name, target / (folder.name + ".spec"), ["-q", "--buildrequires"])
            got[arch_name] = {line.strip() for line in lines.splitlines() if line.strip()}
        for arch_name, items in wanted.items():
            if got[arch_name] != items:
                extra = sorted(got[arch_name] - items)[:5]
                absent = sorted(items - got[arch_name])[:5]
                return False, "expansion check %s: extra %s missing %s" % (arch_name, extra, absent)
        return True, ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _arches(raw):
    out = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not out:
        raise click.UsageError("need at least one arch")
    return out


class Context:
    def __init__(self, cli, release, spec_root, repo_base, arches, side_tag, jobs, refresh=False, quiet=False):
        self.cli = cli
        self.release = release
        self.arches = arches
        self.jobs = jobs
        self.quiet = quiet
        repo_root = Path(__file__).resolve().parent.parent
        self.root = Path(spec_root).expanduser().resolve() if spec_root else repo_root / "fedora-rpms" / release
        if not self.root.is_dir():
            raise click.ClickException("Spec root is not a directory: " + str(self.root))
        tags = ["testing"]
        if side_tag:
            tags += cli._side_tag_repo_dirs(side_tag)
        self.urls = {arch: [cli._repo_url(repo_base, release, tag, arch) for tag in tags] for arch in arches}
        self.say("Loading repository metadata")
        jobs_list = [(arch, url) for arch, urls in self.urls.items() for url in urls]
        with ThreadPoolExecutor(max_workers=min(4, len(jobs_list))) as pool:
            loaded = list(pool.map(lambda item: P.load_repo(item[1], CACHE, refresh), jobs_list))
        by_arch = defaultdict(list)
        for (arch, _url), repo in zip(jobs_list, loaded):
            by_arch[arch].append(repo)
        self.repos = {arch: P.merge_repos(items) for arch, items in by_arch.items()}
        self.tmp = tempfile.TemporaryDirectory(prefix="orbscli-plan-")
        self.tool = P.Tooling(cli, self.root, cli._distro_macro_loads(self.root, Path(self.tmp.name)), CACHE)
        self.names = P.list_spec_names(self.root)
        self.say("Scanning %d specs" % len(self.names))
        light = S.scan_all(self.root, self.names, arches, jobs, self.tool.loads)
        self.tool.macro_index = S.macro_index(self.root, light)
        self.tool.brs_of = lambda name: light[name].brs.get("x86_64", ()) if name in light else ()
        self.specs = S.SpecStore(self.tool, light, arches)
        self.groups = P.Groups(self.root)
        self.group_of = self.groups.mapping(self.specs)

    def say(self, message):
        if not self.quiet:
            click.echo(message, err=True)

    def planner(self, dynamic=True):
        return P.Planner(self.tool, self.specs, self.repos, self.arches, dynamic, self.jobs, self.say)


def common(func):
    options = [
        click.option("--release", "release", default="oreon-11-rp1", show_default=True),
        click.option("--spec-root", type=click.Path(file_okay=False, path_type=Path)),
        click.option("--repo-base", envvar="ORBSCLI_REPO_BASE", default="https://repo-us.oreonhq.com"),
        click.option("--arch", "arch", default="x86_64,aarch64", show_default=True),
        click.option("--side-tag", "side_tag"),
        click.option("--jobs", type=int, default=8, show_default=True),
        click.option("--refresh", is_flag=True, help="Ignore cached repository metadata."),
    ]
    for option in reversed(options):
        func = option(func)
    return func


def selection(func):
    options = [
        click.option("--packages", "packages_blob", help="Names separated by spaces, commas or newlines. - reads stdin."),
        click.option("--packages-file", type=click.Path(exists=True, dir_okay=False, path_type=Path)),
        click.option("--group", "groups", multiple=True, help="Only this group. Can repeat."),
        click.option("--failed-chain", "chain_id", type=click.IntRange(min=1), help="Failed and later packages of a chain."),
        click.option("--pushed", is_flag=True, help="Committed specs changed against origin/HEAD."),
    ]
    for option in reversed(options):
        func = option(func)
    return func


def pick_seeds(cli, ctx, packages_blob, packages_file, groups, chain_id, pushed):
    names = None
    if sum(bool(x) for x in (packages_blob, packages_file, chain_id, pushed)) > 1:
        raise click.UsageError("Choose one of --packages, --packages-file, --failed-chain, --pushed.")
    if packages_file:
        names = cli.parse_package_blob(packages_file.read_text(encoding="utf-8"))
    elif packages_blob:
        names = cli.parse_package_blob(click.get_text_stream("stdin").read() if packages_blob == "-" else packages_blob)
    elif chain_id:
        base, token = cli.get_api_url(), cli.get_token()
        status = cli.http_json("GET", base, "/api/builds/chain/%d/status" % chain_id, token=token,
                               params={"offset": 0}, timeout=30.0)
        jobs = cli._list_failed_chain_jobs(base, token, chain_id)
        names = [job["package_name"] for job in jobs]
        current = int(status.get("current_group") or 0)
        for group in (status.get("groups") or [])[current:]:
            names.extend(group)
        names = list(dict.fromkeys(names))
    elif pushed:
        repo_root = Path(__file__).resolve().parent.parent
        names = cli._chain_seed_names(cli._git_changed_spec_paths(repo_root, ctx.root, True))
    if names is None:
        names = list(ctx.names)
    wanted = set(groups)
    if wanted:
        names = [name for name in names if ctx.group_of.get(name) in wanted]
    return list(dict.fromkeys(names)), wanted


def report_blocked(plan, ctx, limit=0):
    roots = {name: reasons for name, reasons in plan.blocked.items()
             if any(not reason.startswith("blocked prerequisite") for reason in reasons)}
    grouped = defaultdict(list)
    for name, reasons in sorted(roots.items()):
        grouped[tuple(reasons)].append(P.show(name))
    shown = 0
    for reasons, names in sorted(grouped.items(), key=lambda item: -len(item[1])):
        click.echo(", ".join(sorted(set(names))) + ":", err=True)
        for reason in reasons:
            click.echo("  " + reason, err=True)
        shown += 1
        if limit and shown >= limit:
            click.echo("  ... %d more" % (len(grouped) - shown), err=True)
            break
    cascaded = len(plan.blocked) - len(roots)
    if cascaded:
        click.echo("%d more are blocked behind those." % cascaded, err=True)


def write_blockers(path, plan, ctx):
    rows = []
    for name, reasons in sorted(plan.blocked.items()):
        rows.append({"package": P.show(name), "group": ctx.group_of.get(P.show(name), "misc"), "reasons": reasons})
    Path(path).write_text(json.dumps({"blocked": rows, "notes": plan.notes, "warnings": plan.warnings,
                                     "bootstrap_first_build_args": plan.bootstrap},
                                     indent=1) + "\n", encoding="utf-8")


def _side_tag_built_names(cli, ctx, side_tag, names, jobs):
    """Names with success builds for every arch via this side tag.

    Only drops when the newest side-tag success is newer than the last
    commit touching the spec dir, and the dir has no uncommitted changes.
    Otherwise the build predates the current spec and a rebuild is needed.
    """
    if not side_tag or not names:
        return set()
    token = cli.get_token()
    api_base = cli.get_api_url()

    def arch_hits(name):
        try:
            data = cli.http_json("GET", api_base, "/api/builds", token=token,
                                 params={"search": name, "status": "success", "limit": 1000})
        except Exception:
            return None
        hits = set()
        newest = ""
        for job in data.get("items", []):
            if job.get("package_name") != name or job.get("side_tag_name") != side_tag:
                continue
            hits.add(job.get("architecture"))
            created = job.get("created_at") or ""
            if created > newest:
                newest = created
        return hits, newest

    def git_state(name):
        try:
            dirty = subprocess.run(["git", "-C", str(ctx.root), "status", "--porcelain", "--", name],
                                   capture_output=True, text=True, timeout=30).stdout.strip()
            out = subprocess.run(["git", "-C", str(ctx.root), "log", "-1", "--format=%ct", "--", name],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            return bool(dirty), int(out) if out else None
        except Exception:
            return None, None

    done = set()
    with ThreadPoolExecutor(max_workers=max(1, min(8, jobs or 8))) as pool:
        for name, (hits, newest) in zip(names, pool.map(arch_hits, names)):
            if hits is None:
                continue
            if set(ctx.arches) > hits:
                continue
            dirty, commit_ts = git_state(name)
            if dirty:
                continue
            if commit_ts is None:
                continue
            try:
                build_ts = int(datetime.fromisoformat(newest.replace("Z", "+00:00")).timestamp())
            except Exception:
                continue
            if build_ts > commit_ts:
                done.add(name)
    return done


def register(cli_group, cli):
    @cli_group.group("plan")
    def grp_plan():
        """Fast planning, preflight and status. Split by group for a team."""

    def chain_command(**kw):
        ctx = Context(cli, kw["release"], kw["spec_root"], kw["repo_base"], _arches(kw["arch"]),
                      kw["side_tag"], kw["jobs"], kw["refresh"])
        seeds, wanted = pick_seeds(cli, ctx, kw["packages_blob"], kw["packages_file"], kw["groups"],
                                   kw["chain_id"], kw["pushed"])
        ctx.say("Planning %d packages" % len(seeds))
        plan = ctx.planner(dynamic=not kw["no_dynamic"]).plan(seeds)
        layers = plan.layers
        needs = {}
        if wanted:
            layers, needs = P.split_by_group(plan, ctx.group_of, wanted, kw["with_prereqs"])
        if kw.get("drop_side_tag_built"):
            dropped = _side_tag_built_names(cli, ctx, kw["side_tag"], plan.names(), kw["jobs"])
            if dropped:
                layers = [[n for n in layer if n not in dropped] for layer in layers]
                layers = [layer for layer in layers if layer]
                ctx.say("Dropping %d already built via side tag %s: %s" % (
                    len(dropped), kw["side_tag"], ", ".join(sorted(dropped))))
        text = P.chain_text(layers)
        if kw["split_dir"]:
            folder = kw["split_dir"]
            folder.mkdir(parents=True, exist_ok=True)
            by_group = sorted(set(ctx.group_of.get(name) for name in plan.names()))
            index = []
            for group in by_group:
                glayers, gneeds = P.split_by_group(plan, ctx.group_of, {group}, kw["with_prereqs"])
                if not glayers:
                    continue
                (folder / (group + ".chain")).write_text(P.chain_text(glayers) + "\n", encoding="utf-8")
                count = sum(len(layer) for layer in glayers)
                index.append("%s\t%d packages\t%d layers\tneeds %s" % (
                    group, count, len(glayers),
                    ", ".join("%s(%d)" % (g, len(v)) for g, v in sorted(gneeds.items())) or "-"))
            (folder / "INDEX.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
            click.echo("\n".join(index), err=True)
        if kw["blockers"]:
            write_blockers(kw["blockers"], plan, ctx)
        if plan.blocked:
            click.echo("Not in the chain, %d packages:" % len(plan.blocked), err=True)
            report_blocked(plan, ctx, limit=0 if kw["verbose"] else 25)
        for name, note in sorted(plan.notes.items()):
            click.echo("%s %s" % (name, note), err=True)
        for name, items in sorted(plan.warnings.items()):
            if name in plan.names():
                click.echo("%s %s" % (name, "; ".join(items)), err=True)
        if needs:
            for group, names in sorted(needs.items()):
                click.echo("Needs group %s first for: %s" % (group, ", ".join(names[:12]) + (" ..." if len(names) > 12 else "")),
                           err=True)
        if not text:
            raise click.ClickException("Nothing to build.")
        if kw["output"]:
            kw["output"].write_text(text + "\n", encoding="utf-8")
        if plan.f44:
            f44_names = set(plan.f44)
            f44_layers = [[name for name in layer if name in f44_names]
                          for layer in layers]
            f44_layers = [layer for layer in f44_layers if layer]
            f44_text = P.chain_text(f44_layers)
            stuck = sorted({arch for arches in plan.f44.values() for arch in arches})
            click.echo("%d packages cannot build on the or11 mock for %s yet, build this chain first with the fedora-44 mock, publish, then submit the or11 chain:" % (
                len(f44_names), ", ".join(stuck)), err=True)
            click.echo(f44_text, err=True)
            if kw["output"]:
                f44_path = kw["output"].with_name(kw["output"].stem + "-fedora-44.txt")
                f44_path.write_text(f44_text + "\n", encoding="utf-8")
                click.echo("wrote %s" % f44_path, err=True)
        click.echo(text)
        click.echo("%d layers, %d builds, %d held back." % (len(layers), sum(len(layer) for layer in layers),
                                                           len(plan.blocked)), err=True)
        if plan.blocked and kw["strict"]:
            sys.exit(2)

    def chain_options(func):
        for option in reversed([
            click.option("--output", type=click.Path(dir_okay=False, path_type=Path)),
            click.option("--split-dir", type=click.Path(file_okay=False, path_type=Path),
                         help="Write one chain per group and an INDEX.txt."),
            click.option("--blockers", type=click.Path(dir_okay=False, path_type=Path), help="JSON list of held back packages."),
            click.option("--with-prereqs", is_flag=True, help="Pull needed packages from other groups into the group chain."),
            click.option("--no-dynamic", is_flag=True, help="Skip generated BuildRequires (pyproject, cargo, go, R)."),
            click.option("--drop-side-tag-built", is_flag=True,
                         help="Leave out packages that already built ok on every arch via the side tag."),
            click.option("--strict", is_flag=True, help="Exit 2 when anything is held back."),
            click.option("--verbose", is_flag=True),
        ]):
            func = option(func)
        return selection(common(func))

    @grp_plan.command("chain")
    @chain_options
    def cmd_chain(**kw):
        """Ordered chain for everything the repo does not have at the spec version.

        Packages that cannot build yet are left out and listed with the reason.
        The chain only contains packages whose build requirements are in the repo
        or built earlier in the same chain.
        """
        chain_command(**kw)

    @cli_group.command("generate-chain")
    @chain_options
    def cmd_generate_chain(**kw):
        """Same as plan chain."""
        chain_command(**kw)

    cli.cmd_plan_chain = cmd_chain
    cli.cmd_generate_chain = cmd_generate_chain

    @grp_plan.command("status")
    @common
    @click.option("--target-repo", "targets", multiple=True,
                  help="Repo URL with the versions you want to reach. x86_64 metadata is enough.")
    @click.option("--closure", is_flag=True, help="Also run repoclosure on the repo.")
    def cmd_status(release, spec_root, repo_base, arch, side_tag, jobs, refresh, targets, closure):
        """Where every package stands, by group. """
        ctx = Context(cli, release, spec_root, repo_base, _arches(arch), side_tag, jobs, refresh)
        rows = defaultdict(Counter)
        seen_first = ctx.arches[0]
        ctx.say("Reading versions")
        ctx.specs.prefetch(ctx.names, jobs)
        for name in ctx.names:
            group = ctx.group_of[name]
            evr, rpm_name, error = ctx.specs.evr(name)
            if error:
                state = "spec error"
            elif ctx.specs.done(name, ctx.repos):
                state = "current"
            else:
                have = ctx.repos[seen_first].sources.get(rpm_name)
                if not have:
                    state = "unbuilt new"
                else:
                    order = max((P.evr_compare(tuple(evr), item) for item in have), default=1)
                    state = "unbuilt bump" if order > 0 else "older than repo"
            rows[group][state] += 1
            rows["TOTAL"][state] += 1
        states = ["current", "unbuilt bump", "unbuilt new", "older than repo", "spec error"]
        click.echo("%-14s %8s" % ("group", "specs") + "".join(" %15s" % s for s in states))
        for group in sorted(rows, key=lambda g: (g == "TOTAL", g)):
            counts = rows[group]
            click.echo("%-14s %8d" % (group, sum(counts.values())) + "".join(" %15d" % counts[s] for s in states))
        if targets:
            behind = Counter()
            names = set()
            missing = []
            for url in targets:
                repo = P.load_repo(url, CACHE, refresh)
                for source, evrs in repo.sources.items():
                    names.add(source)
                    if source not in ctx.specs:
                        missing.append(source)
                        continue
                    have = ctx.specs.evr(source)[0]
                    if have and any(P.evr_compare((e[0], e[1], ""), (have[0], have[1], "")) > 0 for e in evrs):
                        behind[ctx.group_of[source]] += 1
            click.echo("")
            click.echo("Spec version behind the target, by group:")
            for group, count in behind.most_common():
                click.echo("  %-14s %d" % (group, count))
            click.echo("In the target but with no spec here: %d" % len(set(missing)))
        if closure:
            _closure_summary(cli, ctx)

    def _closure_summary(cli, ctx):
        try:
            from . import repoclosure as closure
        except ImportError:
            import repoclosure as closure
        urls = {arch: ctx.urls[arch][:1] for arch in ctx.arches}
        found = closure.discover(cli, urls, ctx.release, side_only=False)
        fam = Counter()
        sources = defaultdict(set)
        for arch, by_source in found.items():
            for source, reqs in by_source.items():
                for req in reqs:
                    key = re.sub(r"\(.*", "", re.sub(r"^lib(HS|Qt6).*", r"lib\1", req))
                    if req.startswith("ocamlx("):
                        key = "ocaml abi"
                    elif "libHS" in req:
                        key = "ghc abi"
                    elif "PRIVATE_API" in req:
                        key = "qt private api"
                    fam[key] += 1
                    sources[key].add(source)
        click.echo("")
        click.echo("Broken in the repo, by cause:")
        for key, count in fam.most_common(15):
            groups = Counter(ctx.group_of.get(source, "misc") for source in sources[key])
            click.echo("  %-20s %5d deps, %4d sources  %s" % (
                key, count, len(sources[key]), ", ".join("%s %d" % item for item in groups.most_common(3))))

    @grp_plan.command("groups")
    @common
    @click.option("--list", "list_group", help="Print the package names in this group.")
    def cmd_groups(release, spec_root, repo_base, arch, side_tag, jobs, refresh, list_group):
        """List groups with package counts."""
        ctx = Context(cli, release, spec_root, repo_base, _arches(arch), side_tag, jobs, refresh, quiet=True)
        if list_group:
            click.echo("\n".join(sorted(name for name, group in ctx.group_of.items() if group == list_group)))
            return
        counts = Counter(ctx.group_of.values())
        for group, count in sorted(counts.items()):
            click.echo("%-14s %5d" % (group, count))

    @grp_plan.command("check")
    @common
    @selection
    @click.option("--stages", default="parse,deps,files,sources", show_default=True,
                  help="parse, deps, files, sources, prep (prep runs rpmbuild -bp, slow).")
    @click.option("--all", "include_current", is_flag=True, help="Also check packages already built at this version.")
    @click.option("--report", type=click.Path(dir_okay=False, path_type=Path), help="JSON report.")
    @click.option("--no-dynamic", is_flag=True)
    def cmd_check(release, spec_root, repo_base, arch, side_tag, jobs, refresh, packages_blob, packages_file, groups,
                  chain_id, pushed, stages, include_current, report, no_dynamic):
        """Find broken specs before any chain runs. No mock, no builds.

        parse   rpmspec reads it
        deps    every BuildRequires is in the repo or in a spec that will build first
        files   local sources and patches exist next to the spec
        sources Source URLs download and Source0 matches source0_hash
        prep    rpmbuild -bp applies the patches
        """
        chosen = {item.strip() for item in stages.split(",") if item.strip()}
        ctx = Context(cli, release, spec_root, repo_base, _arches(arch), side_tag, jobs, refresh)
        seeds, _ = pick_seeds(cli, ctx, packages_blob, packages_file, groups, chain_id, pushed)
        ctx.specs.prefetch([name for name in seeds if name in ctx.specs], jobs)
        todo = [name for name in seeds if name in ctx.specs and (include_current or not ctx.specs.done(name, ctx.repos))]
        ctx.specs.load_many(todo, jobs)
        ctx.specs.deepen_many(todo, jobs)
        issues = defaultdict(list)
        if "parse" in chosen:
            for name in todo:
                if ctx.specs[name].error:
                    issues[name].append(("parse", ctx.specs[name].error))
        if "files" in chosen:
            for name in todo:
                spec = ctx.specs[name]
                if spec.error:
                    continue
                folder = Path(spec.path).parent
                for tag, value in [*spec.sources, *spec.patches]:
                    if re.match(r"^[a-z]+://", value):
                        continue
                    if not (folder / Path(value).name).is_file():
                        issues[name].append(("files", "%s %s is not next to the spec" % (tag, Path(value).name)))
        if "deps" in chosen:
            ctx.say("Checking dependencies")
            planner = ctx.planner(dynamic=not no_dynamic)
            pool = ThreadPoolExecutor(max_workers=jobs)
            futures = {}
            if not no_dynamic:
                for name in todo:
                    spec = ctx.specs[name]
                    if spec.dynamic and not spec.error:
                        futures[name] = pool.submit(planner.dynamic_all, spec)
            try:
                for count, name in enumerate(todo, 1):
                    spec = ctx.specs[name]
                    if spec.error:
                        continue
                    extra = {}
                    if name in futures:
                        extra, errors, warned = futures[name].result()
                        for message in errors:
                            issues[name].append(("generated", "generated BuildRequires failed: " + message))
                        for message in warned:
                            issues[name].append(("warning", message))
                    planner.resolver.refresh(spec)
                    owners, missing = planner.resolve_spec(spec, planner.own_requirements(spec, dynamic=extra))
                    for item in sorted(missing):
                        hints = planner.resolver.suggest(item)
                        text = "nothing provides " + item
                        if hints:
                            text += " (spec with a matching name: " + ", ".join(hints) + ")"
                        issues[name].append(("deps", text))
                    if count % 100 == 0:
                        ctx.say("  %d of %d" % (count, len(todo)))
            finally:
                pool.shutdown(wait=True)
        if "sources" in chosen:
            ctx.say("Checking sources")
            for name, found in _check_sources(cli, ctx, todo, jobs).items():
                issues[name].extend(found)
        if "prep" in chosen:
            ctx.say("Applying patches")
            for name, found in _check_prep(cli, ctx, [n for n in todo if n not in issues], jobs).items():
                issues[name].extend(found)
        classes = Counter(kind for found in issues.values() for kind, _ in found)
        by_group = defaultdict(Counter)
        for name, found in issues.items():
            for kind in {kind for kind, _ in found}:
                by_group[ctx.group_of.get(name, "misc")][kind] += 1
        click.echo("%d packages checked, %d have problems" % (len(todo), len(issues)))
        for kind, count in classes.most_common():
            click.echo("  %-10s %d" % (kind, count))
        click.echo("")
        for group in sorted(by_group):
            click.echo("%-14s %s" % (group, "  ".join("%s %d" % item for item in by_group[group].most_common())))
        click.echo("")
        for name in sorted(issues):
            for kind, message in issues[name]:
                click.echo("%s [%s] %s" % (name, kind, message))
        if report:
            report.write_text(json.dumps({name: [{"stage": kind, "message": message} for kind, message in found]
                                          for name, found in sorted(issues.items())}, indent=1) + "\n", encoding="utf-8")
        if issues:
            sys.exit(1)

    @grp_plan.command("bake")
    @common
    @selection
    @click.option("--apply", is_flag=True, help="Write the static BuildRequires into the specs.")
    @click.option("--report", type=click.Path(dir_okay=False, path_type=Path),
                  help="JSON file with one record per package.")
    def cmd_bake(release, spec_root, repo_base, arch, side_tag, jobs, refresh,
                 packages_blob, packages_file, groups, chain_id, pushed, apply, report):
        """Bake generated BuildRequires into plain static spec lines.

        Removes %generate_buildrequires and writes the evaluated dep list as
        normal BuildRequires lines, gated on an expansion check per spec.
        """
        section_re = re.compile(r"(?m)^%(?:description|package|prep|build|install|check|files|changelog|conf|clean)\b")
        br_re = re.compile(r"(?m)^BuildRequires:\s*(.+?)\s*$")
        marker = "# generated BuildRequires, refresh with: orbscli plan bake"
        ctx = Context(cli, release, spec_root, repo_base, _arches(arch), side_tag, jobs, refresh)
        seeds, _ = pick_seeds(cli, ctx, packages_blob, packages_file, groups, chain_id, pushed)
        todo = [name for name in seeds if name in ctx.specs]
        ctx.specs.prefetch(todo, jobs)
        ctx.specs.load_many(todo, jobs)
        ctx.specs.deepen_many(todo, jobs)
        planner = ctx.planner(dynamic=True)
        records = {}
        manual = []
        left_alone = []

        def drop_empty_conditionals(text):
            lines = text.splitlines(keepends=True)
            changed = True
            while changed:
                changed = False
                for index in range(len(lines)):
                    if index >= len(lines) or not re.match(r"^%if\b", lines[index]):
                        continue
                    depth = 0
                    for scan in range(index + 1, len(lines)):
                        if re.match(r"^%if\b", lines[scan]):
                            depth += 1
                        elif re.match(r"^%endif\b", lines[scan]):
                            if depth == 0:
                                if not "".join(lines[index + 1:scan]).strip():
                                    del lines[index:scan + 1]
                                    changed = True
                                    break
                                depth = 0
                            else:
                                depth -= 1
                    if changed:
                        break
            return "".join(lines)

        def remove_gen_sections(text):
            lines = text.splitlines(keepends=True)
            out = []
            index = 0
            while index < len(lines):
                if re.match(r"^%generate_buildrequires\b", lines[index]):
                    index += 1
                    depth = 0
                    while index < len(lines):
                        line = lines[index]
                        if re.match(r"^%if\b", line):
                            depth += 1
                        elif re.match(r"^%endif\b", line):
                            if depth == 0:
                                break
                            depth -= 1
                        elif depth == 0 and re.match(
                                r"^%(?:build|install|check|clean|files|changelog|conf|prep|description|package)\b", line):
                            break
                        index += 1
                    continue
                out.append(lines[index])
                index += 1
            return "".join(out)

        def rewrite(text, blocks):
            cleaned = remove_gen_sections(text)
            found = section_re.search(cleaned)
            if not found:
                return None
            insert = found.start()
            added = marker + "\n" + "\n".join(blocks) + "\n"
            return cleaned[:insert] + added + "\n" + cleaned[insert:]

        def gate(path, new_text, wanted):
            return gate_expand(ctx.tool, planner.arches, path, new_text, wanted)

        def one(name):
            spec = ctx.specs[name]
            record = {"baked": 0, "skipped": None}
            if spec.error:
                record["skipped"] = "spec error: " + spec.error
                return name, record
            path = Path(spec.path)
            raw = path.read_text(encoding="utf-8")
            if not spec.dynamic:
                record["skipped"] = "not dynamic"
                return name, record
            active = False
            conditional_raw = False
            try:
                for arch_name in planner.arches:
                    expanded = ctx.tool.expanded(arch_name, path)
                    if re.search(r"(?m)^%generate_buildrequires\b", expanded):
                        active = True
            except Exception as exc:
                record["skipped"] = "expand error: " + str(exc).strip().splitlines()[-1][:150]
                return name, record
            depth = 0
            for line in raw.splitlines():
                if re.match(r"^%if\b", line):
                    depth += 1
                elif re.match(r"^%endif\b", line):
                    depth -= 1
                elif re.match(r"^%generate_buildrequires\b", line) and depth > 0:
                    conditional_raw = True
            if not active:
                record["skipped"] = "conditional generator inactive by default, left as is"
                left_alone.append(name)
                return name, record
            found, errors, warnings = planner.dynamic_all(spec)
            if errors:
                record["skipped"] = "evaluate failed: " + "; ".join(errors)[:200]
                return name, record
            if warnings:
                record["skipped"] = "evaluate partial: " + "; ".join(warnings)[:200]
                return name, record
            by_arch = {arch_name: sorted(set(items)) for arch_name, items in found.items()}
            common = sorted(set.intersection(*[set(v) for v in by_arch.values()])) if by_arch else []
            only = {arch_name: sorted(set(v) - set(common)) for arch_name, v in by_arch.items()}
            blocks = ["BuildRequires: " + line for line in common]
            for arch_name, items in only.items():
                if items:
                    blocks.append("%ifarch " + arch_name)
                    blocks += ["BuildRequires: " + line for line in items]
                    blocks.append("%endif")
            static = {match.strip() for match in br_re.findall(raw)}
            blocks = [block for block in blocks
                      if not block.startswith("BuildRequires: ")
                      or block[len("BuildRequires: "):] not in static]
            wanted = {arch_name: set(spec.brs.get(arch_name, ())) | set(by_arch.get(arch_name, ()))
                      for arch_name in planner.arches}
            new_text = rewrite(raw, blocks)
            if new_text is None:
                record["skipped"] = "no section anchor"
                return name, record
            if conditional_raw:
                new_text = drop_empty_conditionals(new_text)
                if re.search(r"(?m)^%generate_buildrequires\b", new_text):
                    record["skipped"] = "conditional generator could not be removed cleanly"
                    manual.append(name)
                    return name, record
            try:
                ok, message = gate(path, new_text, wanted)
            except Exception as exc:
                record["skipped"] = "gate error: " + str(exc).strip().splitlines()[-1][:150]
                return name, record
            if not ok:
                record["skipped"] = message
                return name, record
            record["baked"] = len([b for b in blocks if b.startswith("BuildRequires: ")])
            record["text"] = new_text
            return name, record

        done = 0
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for name, record in pool.map(one, todo):
                done += 1
                if done % 25 == 0:
                    ctx.say("  %d of %d" % (done, len(todo)))
                records[name] = record
                if record.get("skipped"):
                    continue
                if apply and record.get("text"):
                    Path(ctx.specs[name].path).write_text(record.pop("text"), encoding="utf-8")
                elif not apply:
                    record.pop("text", None)
        baked_count = sum(1 for record in records.values() if record.get("baked"))
        skipped_count = sum(1 for record in records.values()
                            if record.get("skipped") and "not dynamic" not in record["skipped"])
        click.echo("%d packages, %d baked, %d skipped, %d manual, %d left alone" % (
            len(todo), baked_count, skipped_count, len(manual), len(left_alone)))
        if left_alone:
            click.echo("left alone (conditional generator inactive by default): %s" % ", ".join(left_alone))
        if manual:
            click.echo("manual (generator inside %%if): %s" % ", ".join(manual))
        for name, record in sorted(records.items()):
            if record.get("skipped"):
                click.echo("%s %s" % (name, record["skipped"]))
        if report:
            report.write_text(json.dumps(records, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    @grp_plan.command("derich")
    @common
    @selection
    @click.option("--apply", is_flag=True, help="Rewrite the resolved specs.")
    @click.option("--report", type=click.Path(dir_okay=False, path_type=Path))
    def cmd_derich(release, spec_root, repo_base, arch, side_tag, jobs, refresh,
                   packages_blob, packages_file, groups, chain_id, pushed, apply, report):
        """Resolve simple (X if COND) BuildRequires into plain lines.

        Keeps version ranges, or-choices and macro conditions rich. Gated on
        an expansion check per spec.
        """
        rich_re = re.compile(r"(?m)^BuildRequires:\s*\(\s*(\S+)\s+(if|unless)\s+(\S+)\s*\)\s*$")
        plain_re = re.compile(r"(?m)^BuildRequires:\s*(\S+)\s*$")
        ctx = Context(cli, release, spec_root, repo_base, _arches(arch), side_tag, jobs, refresh)
        seeds, _ = pick_seeds(cli, ctx, packages_blob, packages_file, groups, chain_id, pushed)
        todo = [name for name in seeds if name in ctx.specs]
        ctx.specs.prefetch(todo, jobs)
        ctx.specs.load_many(todo, jobs)
        planner = ctx.planner(dynamic=True)

        def cond_truth(text, arch_name):
            cname, cop, cwant = P.split_atom(text)
            repo = planner.repos[arch_name]
            entries = [entry for entry in repo.provides.get(cname, ()) if entry[2] is not None]
            versioned = [entry for entry in entries if entry[1] is not None]
            if not versioned:
                return bool(entries) if cop in ("=", None) else False
            best = max(versioned, key=functools.cmp_to_key(lambda a, b: P.evr_compare(a[1], b[1])))
            if not best[0]:
                return False
            return P.op_holds(P.evr_compare(best[1], cwant), cop)

        records = {}

        def one(name):
            spec = ctx.specs[name]
            record = {"resolved": 0, "dropped": 0, "kept": 0, "skipped": None}
            if spec.error:
                record["skipped"] = "spec error: " + spec.error
                return name, record
            path = Path(spec.path)
            raw = path.read_text(encoding="utf-8")
            plain_existing = {match.group(1) for match in plain_re.finditer(raw)}
            wanted = {arch_name: set(spec.brs.get(arch_name, ())) for arch_name in planner.arches}
            changes = []
            for match in rich_re.finditer(raw):
                branch, keyword, cond = match.groups()
                line = match.group(0)
                if any(word in branch or word in cond
                       for word in ("(", "with", "or", "and", "unless", "else", "if", "%")):
                    record["kept"] += 1
                    continue
                truth = {arch_name: cond_truth(cond, arch_name) for arch_name in planner.arches}
                if keyword == "unless":
                    truth = {arch_name: not value for arch_name, value in truth.items()}
                if len(set(truth.values())) > 1:
                    record["resolved"] += 1
                    on_arches = " ".join(a for a, v in truth.items() if v)
                    for arch_name, value in truth.items():
                        (wanted[arch_name].add if value else wanted[arch_name].discard)(branch)
                    changes.append((line, "%%ifarch %s\nBuildRequires: %s\n%%endif" % (on_arches, branch)))
                elif truth[planner.arches[0]]:
                    if branch in plain_existing:
                        record["dropped"] += 1
                        changes.append((line, ""))
                    else:
                        record["resolved"] += 1
                        changes.append((line, "BuildRequires: " + branch))
                else:
                    record["dropped"] += 1
                    changes.append((line, ""))
                for arch_name, value in truth.items():
                    (wanted[arch_name].add if value else wanted[arch_name].discard)(branch)
            if not changes:
                record["skipped"] = "nothing to resolve"
                return name, record
            new_text = raw
            for old_line, new_line in changes:
                new_text = new_text.replace(old_line, new_line, 1)
            if new_text == raw:
                record["skipped"] = "rewrite produced no change"
                return name, record
            ok, message = gate_expand(ctx.tool, planner.arches, path, new_text, wanted)
            if not ok:
                record["skipped"] = message
                return name, record
            record["text"] = new_text
            return name, record

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for name, record in pool.map(one, todo):
                records[name] = record
                if record.get("skipped"):
                    continue
                if apply and record.get("text"):
                    Path(ctx.specs[name].path).write_text(record.pop("text"), encoding="utf-8")
                elif not apply:
                    record.pop("text", None)
        resolved = sum(record.get("resolved", 0) for record in records.values())
        dropped = sum(record.get("dropped", 0) for record in records.values())
        kept = sum(record.get("kept", 0) for record in records.values())
        click.echo("%d packages, %d resolved, %d dropped, %d kept rich" % (len(todo), resolved, dropped, kept))
        for name, record in sorted(records.items()):
            if record.get("skipped"):
                click.echo("%s %s" % (name, record["skipped"]))
        if report:
            report.write_text(json.dumps(records, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    @grp_plan.command("bootstrap")
    @common
    @selection
    @click.option("--write", is_flag=True, help="Put %oreon_bootstrap at the top of the specs.")
    def cmd_bootstrap(release, spec_root, repo_base, arch, side_tag, jobs, refresh, packages_blob, packages_file, groups,
                      chain_id, pushed, write):
        """Show or write the self switching bootstrap block for packages in cycles."""
        ctx = Context(cli, release, spec_root, repo_base, _arches(arch), side_tag, jobs, refresh)
        seeds, _ = pick_seeds(cli, ctx, packages_blob, packages_file, groups, chain_id, pushed)
        plan = ctx.planner(dynamic=True).plan(seeds)
        for name, args in sorted(plan.bootstrap.items()):
            have = P.has_switch(ctx.specs[name])
            click.echo("%s: %s%s" % (name, " ".join(args), " (has switch)" if have else ""))
            if write:
                P.write_switch(ctx.specs[name])

def _check_sources(cli, ctx, names, jobs):
    cache = CACHE / "sources" / "check"
    cache.mkdir(parents=True, exist_ok=True)
    found = defaultdict(list)

    def one(name):
        spec = ctx.specs[name]
        out = []
        if spec.error:
            return name, out
        for tag, value in spec.sources:
            if not re.match(r"^https?://", value):
                continue
            wanted = spec.hashes.get(re.sub(r"\D", "", tag) or "0")
            key = cache / (__import__("hashlib").sha256(value.encode()).hexdigest() + ("-" + wanted if wanted else ""))
            if not key.is_file():
                temp = key.with_suffix(".part")
                try:
                    got = cli._fetch_source_archive(cli._genbr_http(), value, temp)
                except Exception as exc:
                    out.append(("sources", "%s %s did not download: %s" % (tag, value, str(exc)[:100])))
                    temp.unlink(missing_ok=True)
                    continue
                if not got:
                    out.append(("sources", "%s %s did not download" % (tag, value)))
                    temp.unlink(missing_ok=True)
                    continue
                digest = got[1]
                if wanted and digest != wanted:
                    out.append(("sources", "%s %s hash is %s, spec says %s" % (tag, value, digest[:12], wanted[:12])))
                    temp.unlink(missing_ok=True)
                    continue
                temp.replace(key)
            elif not wanted and tag == "Source0":
                pass
            if tag == "Source0" and not wanted:
                out.append(("sources", "Source0 has no source0_hash"))
        return name, out

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for name, out in pool.map(one, names):
            if out:
                found[name] = out
    return found


def _check_prep(cli, ctx, names, jobs):
    cache = CACHE / "sources" / "check"
    found = {}

    def one(name):
        spec = ctx.specs[name]
        folder = Path(spec.path).parent
        with tempfile.TemporaryDirectory(prefix="orbscli-prep-") as work:
            work = Path(work)
            sources = work / "SOURCES"
            sources.mkdir()
            for item in folder.iterdir():
                if item.is_file():
                    (sources / item.name).symlink_to(item)
            for tag, value in spec.sources:
                if re.match(r"^https?://", value):
                    wanted = spec.hashes.get(re.sub(r"\D", "", tag) or "0")
                    key = cache / (__import__("hashlib").sha256(value.encode()).hexdigest() + ("-" + wanted if wanted else ""))
                    if key.is_file():
                        target = sources / Path(value.split("#")[-1] if "#/" in value else value).name
                        if not target.exists():
                            target.symlink_to(key)
            command = ["rpmbuild", "-bp", "--nodeps", "--define", "_topdir " + str(work),
                       "--define", "_sourcedir " + str(sources), "--define", "_builddir " + str(work / "BUILD"),
                       "--define", "dist .or11", *cli._rpmspec_distro_args(), spec.path]
            for load in ctx.tool.loads:
                command += ["--load", load]
            try:
                run = subprocess.run(command, capture_output=True, text=True, timeout=900)
            except subprocess.TimeoutExpired:
                return name, [("prep", "timed out")]
            if run.returncode:
                lines = [line for line in (run.stderr + run.stdout).splitlines() if line.strip()]
                return name, [("prep", " | ".join(lines[-3:])[:300])]
        return name, []

    with ThreadPoolExecutor(max_workers=max(1, jobs // 2)) as pool:
        for name, out in pool.map(one, names):
            if out:
                found[name] = out
    return found
