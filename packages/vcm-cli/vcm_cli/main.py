"""``vcm`` command: argparse front-end over the VCM libraries."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vcm_ai import analyze, get_provider, to_markdown
from vcm_ai.catalog import (
    PROJECT_CATALOG_DIR,
    USER_CATALOG,
    Catalog,
    read_file,
)
from vcm_core import NothingToCommit, Repository, VcmError
from vcm_diff import diff_revisions, render_text
from vcm_graph import build_graph, project_config
from vcm_graph.backends.appwrite_backend import health as appwrite_health
from vcm_graph.backends.appwrite_backend import provision as appwrite_provision
from vcm_parsers import design_from_revision
from vcm_trace import (
    affected_by_component,
    affected_by_revision,
    list_devices,
    passport,
    register_device,
)
from vcm_validation import check_bom_text, compare_validation, validate_revision
from vcm_workflow import (
    add_comment,
    approve,
    close_pr,
    create_release,
    explain,
    get_pr,
    list_prs,
    open_pr,
    plan_rollback,
    render_plan,
    render_pr,
    render_timeline,
    timeline,
    verify_release,
)


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _repo(args) -> Repository:
    return Repository.discover(args.repo or ".")


def _pair(args) -> tuple[str, str]:
    return args.a or "HEAD~1", args.b or "HEAD"


def _issues_text(res: dict) -> str:
    lines = [f"{i['severity'].upper():7} {i['code']}: {i['message']}" for i in res["issues"]]
    lines.append(f"status: {res['status']}  errors: {res['counts']['error']}  warnings: {res['counts']['warning']}")
    if res.get("kicad_cli"):
        k = res["kicad_cli"]
        lines.append(f"kicad-cli: {k['status']}" + (f" ({k['reason']})" if k.get("reason") else ""))
    return "\n".join(lines)


# ---- command handlers (each returns an exit code) -----------------------------------
def cmd_init(args) -> int:
    repo = Repository.init(args.path, branch=args.branch)
    print(f"Initialized empty VCM repository in {repo.dir}")
    return 0


def cmd_commit(args) -> int:
    repo = _repo(args)
    try:
        rev = repo.commit(args.message, author=args.author)
    except NothingToCommit as e:
        print(f"nothing to commit: {e}")
        return 1
    if args.tag:
        repo.tag(args.tag, rev.id)
    cats = {}
    for a in rev.artifacts.values():
        cats[a.category] = cats.get(a.category, 0) + 1
    print(f"[{rev.branch} {rev.id[:10]}] {rev.message}")
    print("  artifacts: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))
    return 0


def cmd_status(args) -> int:
    st = _repo(args).status()
    if args.json:
        _dump(st)
    else:
        for k, paths in st.items():
            for p in paths:
                print(f"{k:9} {p}")
        if not any(st.values()):
            print("clean")
    return 0


def cmd_log(args) -> int:
    repo = _repo(args)
    revs = repo.log("HEAD", args.n)
    tags = repo.tags()
    if args.json:
        _dump([{**r.summary(), "tags": sorted(t for t, i in tags.items() if i == r.id)} for r in revs])
        return 0
    for r in revs:
        t = ",".join(sorted(t for t, i in tags.items() if i == r.id))
        print(f"{r.id[:10]}  {r.timestamp}  {r.author:10} {r.message}" + (f"  ({t})" if t else ""))
    return 0


def cmd_branch(args) -> int:
    repo = _repo(args)
    if args.name:
        repo.branch(args.name, args.start)
    else:
        for b in repo.branches():
            print(("* " if b == repo.current_branch else "  ") + b)
    return 0


def cmd_tag(args) -> int:
    repo = _repo(args)
    if args.name:
        repo.tag(args.name, args.rev)
    else:
        for t, rid in repo.tags().items():
            print(f"{t} {rid[:10]}")
    return 0


def cmd_checkout(args) -> int:
    rev = _repo(args).checkout(args.rev, force=args.force)
    print(f"checked out {rev.id[:10]}")
    return 0


def cmd_diff(args) -> int:
    a, b = _pair(args)
    d = diff_revisions(_repo(args), a, b)
    _dump(d) if args.json else print(render_text(d))
    return 0


def cmd_impact(args) -> int:
    a, b = _pair(args)
    rep = analyze(_repo(args), a, b, provider=get_provider(args.llm))
    if args.json:
        _dump(rep)
    else:
        print(to_markdown(rep), end="")
    return 0


def cmd_bom_validate(args) -> int:
    if args.path:
        issues = check_bom_text(Path(args.path).read_text(), args.path)
    else:
        repo = _repo(args)
        rev = repo.get_revision(args.rev)
        issues = []
        for p, art in rev.artifacts.items():
            if art.category == "electronics" and p.lower().endswith(".csv"):
                issues += check_bom_text(repo.store.read(art.hash).decode(), p)
    errors = sum(i["severity"] == "error" for i in issues)
    res = {"status": "fail" if errors else "pass",
           "counts": {"error": errors, "warning": len(issues) - errors}, "issues": issues}
    _dump(res) if args.json else print(_issues_text(res))
    return 1 if errors else 0


def cmd_validate(args) -> int:
    repo = _repo(args)
    res = validate_revision(repo, args.rev, run_kicad=args.kicad, attach=not args.no_attach)
    if args.compare:
        cmp = compare_validation(repo, args.compare, args.rev)
        _dump({"result": res, "comparison": cmp}) if args.json else (
            print(_issues_text(res)),
            print(f"vs {args.compare}: {len(cmp['new_issues'])} new, {len(cmp['resolved_issues'])} resolved"
                  f"{' (REGRESSION)' if cmp['regression'] else ''}"))
    else:
        _dump(res) if args.json else print(_issues_text(res))
    return 0 if res["status"] == "pass" else 1


def cmd_release_create(args) -> int:
    m = create_release(_repo(args), args.name, args.rev, out=args.out, run_kicad=args.kicad)
    print(f"release {m['name']} -> revision {m['revision'][:10]}  validation {m['validation']['status']}")
    print(f"  {len(m['artifacts'])} artifacts pinned, manifest sha256 {m['manifest_sha256'][:16]}")
    return 0


def cmd_release_verify(args) -> int:
    res = verify_release(_repo(args), args.release, worktree=args.worktree)
    if args.json:
        _dump(res)
    else:
        for c in res["problems"]:
            print(f"FAIL {c['check']}: {c['detail']}")
        print(f"release {res['release']}: {'OK' if res['ok'] else 'VERIFICATION FAILED'} "
              f"({len(res['checks'])} checks)")
    return 0 if res["ok"] else 1


def cmd_rollback(args) -> int:
    p = plan_rollback(_repo(args), args.target, args.current)
    _dump(p) if args.json else print(render_plan(p))
    return 0


def cmd_device_register(args) -> int:
    d = register_device(_repo(args), args.serial, args.release, args.batch, args.notes)
    print(f"registered {d['serial']} (release {d['release']}, revision {d['revision'][:10]})")
    print(f"  QR payload: {d['qr_payload']}\n  QR SVG:     {d['qr_svg']}")
    return 0


def cmd_device_list(args) -> int:
    for d in list_devices(_repo(args)):
        print(f"{d['serial']}  release {d['release']}  rev {d['revision'][:10]}  batch {d['batch'] or '-'}")
    return 0


def cmd_device_passport(args) -> int:
    p = passport(_repo(args), args.serial)
    p["qr"] = {"payload": p["qr"]["payload"]} if args.no_svg else p["qr"]
    _dump(p)
    return 0


def cmd_device_affected(args) -> int:
    repo = _repo(args)
    if args.component:
        devs = affected_by_component(repo, args.component, args.value)
        if args.json:
            _dump(devs)
        else:
            for d in devs:
                print(f"{d['serial']}  {d['component']}={d['value']} ({d['mpn']})  release {d['release']}")
            print(f"{len(devs)} device(s) include {args.component}")
    elif args.revision:
        res = affected_by_revision(repo, args.revision)
        if args.json:
            _dump(res)
        else:
            print(f"changes introduced by {res['revision'][:10]}: {', '.join(res['changed_components'])}")
            for d in res["devices"]:
                print(f"{d['serial']}  release {d['release']}  rev {d['revision'][:10]}")
            print(f"{len(res['devices'])} device(s) built from this revision or later")
    else:
        print("specify --component or --revision", file=sys.stderr)
        return 2
    return 0


def cmd_pr(args) -> int:
    repo = _repo(args)
    if args.pr_cmd == "open":
        pr = open_pr(repo, args.base, args.head, args.title, args.author)
        print(f"opened PR #{pr['id']}")
        print(render_pr(pr))
    elif args.pr_cmd == "list":
        for p in list_prs(repo):
            print(f"#{p['id']} [{p['state']}] {p['title']}  {p['base']}..{p['head']}  approvals {p['approvals']}")
    elif args.pr_cmd == "show":
        pr = get_pr(repo, args.id)
        _dump(pr) if args.json else print(render_pr(pr))
    elif args.pr_cmd == "comment":
        print(render_pr(add_comment(repo, args.id, args.author, args.text)))
    elif args.pr_cmd == "approve":
        print(render_pr(approve(repo, args.id, args.by)))
    elif args.pr_cmd == "close":
        print(render_pr(close_pr(repo, args.id)))
    return 0


def cmd_timeline(args) -> int:
    items = timeline(_repo(args))
    _dump(items) if args.json else print(render_timeline(items))
    return 0


def cmd_explain(args) -> int:
    ex = explain(_repo(args), args.a, args.b)
    _dump(ex) if args.json else print(ex["text"])
    return 0


def cmd_catalog(args) -> int:
    if args.catalog_cmd == "import":
        parts = read_file(args.file)          # validates before anything is written
        target = USER_CATALOG if args.scope == "user" else Path.cwd() / PROJECT_CATALOG_DIR
        target.mkdir(parents=True, exist_ok=True)
        dest = target / (Path(args.file).stem + ".json")
        dest.write_text(json.dumps({"parts": [
            {k: v for k, v in part.items() if k != "_origin"} for part in parts]},
            indent=1, sort_keys=False) + "\n")
        print(f"Imported {len(parts)} parts into {dest}")
        return 0

    catalog = Catalog.load(project_dir=Path.cwd())
    if args.catalog_cmd == "list":
        rows = [p for p in catalog.parts if not args.type or p.get("type") == args.type]
        if args.json:
            _dump([{k: v for k, v in p.items() if k != "sources"} for p in rows])
            return 0
        print(f"{len(rows)} parts ({catalog.describe()['reviewed']} datasheet-reviewed)")
        for p in sorted(rows, key=lambda p: (p.get("type", ""), p["match"][0])):
            flag = "reviewed" if p.get("reviewed") else "UNREVIEWED"
            print(f"  {p['match'][0]:22} {p.get('type', ''):9} {flag:11} {p.get('source', '')}")
        return 0

    if args.catalog_cmd == "show":
        part = catalog.find(args.mpn, args.mpn)
        if part is None:
            print(f"{args.mpn}: not in the catalog ({len(catalog.parts)} parts). "
                  f"No electrical rules would be applied to it.", file=sys.stderr)
            return 1
        _dump(part)
        return 0

    # coverage
    repo = _repo(args)
    revision = repo.get_revision(args.rev)
    design = design_from_revision(repo, revision)
    cov = catalog.coverage(design, declared=set(project_config(repo, revision.id)
                                                .get("external_loads", {})))
    cov.update(catalog.describe())
    if args.json:
        _dump(cov)
        return 0
    print(f"Catalog coverage for {args.rev}: {len(cov['known'])}/{cov['considered']} "
          f"components ({cov['ratio']:.0%})")
    print(f"  known:    {', '.join(cov['known']) or 'none'}")
    print(f"  UNKNOWN:  {', '.join(cov['unknown']) or 'none'}")
    print(f"  passives: {', '.join(cov['passive']) or 'none'}")
    print(f"  declared: {', '.join(cov['declared']) or 'none'}  (load stated in vcm.json)")
    print(f"  catalog:  {cov['parts']} parts, {cov['reviewed']} datasheet-reviewed")
    if cov["unknown"]:
        print(f"\nNo electrical rules run on the unknown parts above. Add them to "
              f"{PROJECT_CATALOG_DIR}/ or ~/.vcm/catalog/ and re-run.")
    return 0


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def cmd_serve(args) -> int:
    """Run the REST API and dashboard. Fails closed: a token is required unless --insecure."""
    try:
        import uvicorn

        from vcm_api import create_app
        from vcm_api.auth import AuthConfigError
    except ImportError:
        raise VcmError("serving needs the API extras: pip install 'vcm[api]'") from None
    if args.insecure and args.host not in LOOPBACK:
        raise VcmError("--insecure is only allowed on a loopback host (127.0.0.1)")
    try:
        app = create_app(args.repo or ".", insecure=args.insecure)
    except AuthConfigError as e:
        raise VcmError(str(e)) from None
    if args.insecure:
        print("vcm: WARNING: authentication disabled (--insecure); loopback only", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_graph(args) -> int:
    if args.graph_cmd == "provision":
        done = appwrite_provision()
        _dump(done) if args.json else print(
            f"Appwrite schema ready: {len(done['created'])} created, "
            f"{len(done['existed'])} already present")
        return 0
    if args.graph_cmd == "health":
        report = appwrite_health()
        _dump(report)
        return 0 if report["ok"] else 1

    repo = _repo(args)
    backend = getattr(args, "backend", None)
    if args.graph_cmd == "push":
        g = build_graph(repo, args.rev, backend=backend or "appwrite")
        st = g.stats()
        _dump(st) if args.json else print(
            f"Pushed {args.rev} ({g.rev[:10]}) to {st['backend']}: "
            f"{sum(st['nodes'].values())} nodes, {sum(st['edges'].values())} edges")
        return 0
    g = build_graph(repo, args.rev, backend=backend)
    if args.graph_cmd == "stats":
        _dump(g.stats())
        return 0
    res = g.affected_by(args.component, args.depth)
    if args.json:
        _dump(res)
        return 0
    print(f"Changing {args.component} at {res['revision'][:10]} may affect:")
    print(f"  nets: {', '.join(res['nets'])}")
    for c in res["components"]:
        print(f"  component {c['ref']} (hop {c['hop']}) via {', '.join(c['via_nets'])}")
    for k in ("firmware", "tests"):
        for x in res[k]:
            print(f"  {k[:-1] if k == 'tests' else k} {x['node'].split(':', 1)[1]} (via {', '.join(v.split(':')[1] for v in x['via'])})")
    print(f"  footprints: {', '.join(f.split(':', 1)[1] for f in res['footprints'])}")
    print(f"  devices built from this revision: {', '.join(d.split(':')[1] for d in res['devices']) or 'none'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vcm", description="Version control and intelligence for hardware")
    p.add_argument("-C", "--repo", help="repository path (default: discover from cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_=None, json_flag=False):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        if json_flag:
            sp.add_argument("--json", action="store_true")
        return sp

    s = add("init", cmd_init, "create a repository")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--branch", default="main")
    s = add("commit", cmd_commit, "snapshot the working tree")
    s.add_argument("-m", "--message", required=True)
    s.add_argument("--author")
    s.add_argument("--tag")
    add("status", cmd_status, "working tree changes", True)
    s = add("log", cmd_log, "revision history", True)
    s.add_argument("-n", type=int)
    s = add("branch", cmd_branch, "list or create branches")
    s.add_argument("name", nargs="?")
    s.add_argument("--start", default="HEAD")
    s = add("tag", cmd_tag, "list or create tags")
    s.add_argument("name", nargs="?")
    s.add_argument("rev", nargs="?", default="HEAD")
    s = add("checkout", cmd_checkout, "restore a revision into the working tree")
    s.add_argument("rev")
    s.add_argument("--force", action="store_true")
    for name, fn, h in (("diff", cmd_diff, "semantic hardware diff"),
                        ("impact", cmd_impact, "impact analysis report")):
        s = add(name, fn, h, True)
        s.add_argument("a", nargs="?")
        s.add_argument("b", nargs="?")
        if name == "impact":
            s.add_argument("--llm", default="none", help="LLM provider name (default none: offline)")
    s = add("explain", cmd_explain, "explain the change between two revisions", True)
    s.add_argument("a")
    s.add_argument("b")

    bom = sub.add_parser("bom", help="BOM tools").add_subparsers(dest="bom_cmd", required=True)
    s = bom.add_parser("validate", help="validate BOM CSV(s)")
    s.set_defaults(fn=cmd_bom_validate)
    s.add_argument("path", nargs="?")
    s.add_argument("--rev", default="HEAD")
    s.add_argument("--json", action="store_true")

    s = add("validate", cmd_validate, "run validation and attach evidence to a revision", True)
    s.add_argument("--rev", default="HEAD")
    s.add_argument("--kicad", action="store_true", help="also run kicad-cli DRC/ERC if installed")
    s.add_argument("--no-attach", action="store_true")
    s.add_argument("--compare", metavar="REV", help="compare with another revision")

    rel = sub.add_parser("release", help="release manifests").add_subparsers(dest="rel_cmd", required=True)
    s = rel.add_parser("create")
    s.set_defaults(fn=cmd_release_create)
    s.add_argument("name")
    s.add_argument("--rev", default="HEAD")
    s.add_argument("--out", help="also write the manifest to this .json/.yaml file")
    s.add_argument("--kicad", action="store_true")
    s = rel.add_parser("verify")
    s.set_defaults(fn=cmd_release_verify)
    s.add_argument("release", help="release name or manifest file")
    s.add_argument("--worktree", action="store_true")
    s.add_argument("--json", action="store_true")

    rb = sub.add_parser("rollback", help="rollback planning").add_subparsers(dest="rb_cmd", required=True)
    s = rb.add_parser("plan")
    s.set_defaults(fn=cmd_rollback)
    s.add_argument("target")
    s.add_argument("--from", dest="current", default="HEAD")
    s.add_argument("--json", action="store_true")

    dev = sub.add_parser("device", help="physical traceability").add_subparsers(dest="dev_cmd", required=True)
    s = dev.add_parser("register")
    s.set_defaults(fn=cmd_device_register)
    s.add_argument("serial")
    s.add_argument("--release", required=True)
    s.add_argument("--batch", default="")
    s.add_argument("--notes", default="")
    dev.add_parser("list").set_defaults(fn=cmd_device_list)
    s = dev.add_parser("passport")
    s.set_defaults(fn=cmd_device_passport)
    s.add_argument("serial")
    s.add_argument("--no-svg", action="store_true", help="omit the QR SVG from the output")
    s = dev.add_parser("affected")
    s.set_defaults(fn=cmd_device_affected)
    s.add_argument("--component")
    s.add_argument("--value", help="only devices whose component has this value or MPN")
    s.add_argument("--revision")
    s.add_argument("--json", action="store_true")

    pr = sub.add_parser("pr", help="hardware pull requests").add_subparsers(dest="pr_cmd", required=True)
    s = pr.add_parser("open")
    s.set_defaults(fn=cmd_pr)
    s.add_argument("--base", required=True)
    s.add_argument("--head", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--author", default="unknown")
    pr.add_parser("list").set_defaults(fn=cmd_pr)
    s = pr.add_parser("show")
    s.set_defaults(fn=cmd_pr)
    s.add_argument("id", type=int)
    s.add_argument("--json", action="store_true")
    s = pr.add_parser("comment")
    s.set_defaults(fn=cmd_pr)
    s.add_argument("id", type=int)
    s.add_argument("text")
    s.add_argument("--author", default="unknown")
    s = pr.add_parser("approve")
    s.set_defaults(fn=cmd_pr)
    s.add_argument("id", type=int)
    s.add_argument("--by", required=True)
    s = pr.add_parser("close")
    s.set_defaults(fn=cmd_pr)
    s.add_argument("id", type=int)

    add("timeline", cmd_timeline, "hardware time machine", True)

    c = sub.add_parser("catalog", help="component knowledge base behind rule-based findings")
    c = c.add_subparsers(dest="catalog_cmd", required=True)
    s = c.add_parser("list", help="every part the catalog knows")
    s.set_defaults(fn=cmd_catalog)
    s.add_argument("--type", help="filter by part type, e.g. ldo, buck, mcu")
    s.add_argument("--json", action="store_true")
    s = c.add_parser("show", help="catalog entry for one part number")
    s.set_defaults(fn=cmd_catalog)
    s.add_argument("mpn")
    s = c.add_parser("coverage", help="which components of a revision the catalog can reason about")
    s.set_defaults(fn=cmd_catalog)
    s.add_argument("--rev", default="HEAD")
    s.add_argument("--json", action="store_true")
    s = c.add_parser("import", help="add parts from a JSON or CSV file")
    s.set_defaults(fn=cmd_catalog)
    s.add_argument("file")
    s.add_argument("--scope", choices=["user", "project"], default="project",
                   help="user: ~/.vcm/catalog; project: ./vcm-catalog (committed with the design)")
    s = sub.add_parser("serve", help="run the REST API and dashboard (bearer token required)")
    s.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--insecure", action="store_true",
                   help="no authentication; refused on non-loopback hosts")
    s.set_defaults(fn=cmd_serve)
    g = sub.add_parser("graph", help="engineering dependency graph").add_subparsers(dest="graph_cmd", required=True)
    backend_help = "graph store: 'sqlite' (local, default) or 'appwrite' (shared TablesDB)"
    s = g.add_parser("query", help="what is affected by changing a component")
    s.set_defaults(fn=cmd_graph)
    s.add_argument("--component", required=True)
    s.add_argument("--rev", default="HEAD")
    s.add_argument("--depth", type=int, default=1)
    s.add_argument("--backend", choices=["sqlite", "appwrite"], help=backend_help)
    s.add_argument("--json", action="store_true")
    s = g.add_parser("stats")
    s.set_defaults(fn=cmd_graph)
    s.add_argument("--rev", default="HEAD")
    s.add_argument("--backend", choices=["sqlite", "appwrite"], help=backend_help)
    s = g.add_parser("push", help="build a revision into the shared Appwrite TablesDB store")
    s.set_defaults(fn=cmd_graph)
    s.add_argument("--rev", default="HEAD")
    s.add_argument("--backend", choices=["sqlite", "appwrite"], help=backend_help)
    s.add_argument("--json", action="store_true")
    s = g.add_parser("provision", help="create the Appwrite database, tables and indexes")
    s.set_defaults(fn=cmd_graph)
    s.add_argument("--json", action="store_true")
    s = g.add_parser("health", help="check the configured Appwrite store")
    s.set_defaults(fn=cmd_graph)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (VcmError, KeyError) as e:
        print(f"vcm: error: {e.args[0] if isinstance(e, KeyError) else e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
