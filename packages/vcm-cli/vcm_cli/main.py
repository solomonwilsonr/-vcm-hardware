"""``vcm`` command: argparse front-end over the VCM libraries."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vcm_ai import analyze, get_provider, to_markdown
from vcm_ai.catalog import (
    NUMERIC_FIELDS,
    PROJECT_CATALOG_DIR,
    USER_CATALOG,
    Catalog,
    CatalogError,
    read_file,
    review_part,
    unverified_fields,
)
from vcm_ai.rules_datacenter import node_meta
from vcm_core import NothingToCommit, Repository, VcmError, now_iso
from vcm_core import remote as rm
from vcm_core import signing as sg
from vcm_core.tokens import ROLES, TokenStore
from vcm_diff import compare_drift, diff_revisions, render_text
from vcm_graph import build_graph, project_config
from vcm_graph.backends.appwrite_backend import health as appwrite_health
from vcm_graph.backends.appwrite_backend import provision as appwrite_provision
from vcm_mcp import approve as approve_proposal
from vcm_mcp import get_proposal, list_proposals, reject, serve
from vcm_mcp.proposals import render as render_proposal
from vcm_parsers import (
    design_from_revision,
    merge_observations,
    parse_nvidia_smi,
    parse_redfish,
    to_node_manifest,
)
from vcm_trace import (
    affected_by_component,
    affected_by_revision,
    define_rack,
    fleet_query,
    import_netbox,
    list_devices,
    list_racks,
    passport,
    place_device,
    plan_rollout,
    rack_power,
    recall,
    register_device,
    sign_device,
)
from vcm_validation import check_bom_text, compare_validation, validate_revision
from vcm_workflow import (
    add_comment,
    approve,
    close_pr,
    create_release,
    evaluate_policy,
    explain,
    get_pr,
    list_prs,
    load_policy,
    open_pr,
    plan_rollback,
    render_plan,
    render_policy,
    render_pr,
    render_timeline,
    sign_release,
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
    if not args.ignore_locks and repo.head_id():
        st = repo.status()
        touched = st["added"] + st["removed"] + st["modified"]
        for name in rm.remotes(repo):
            remote = rm.Remote.of(repo, name)
            blocked = rm.blocking_locks(remote, touched, args.author or rm.identity())
            if blocked:
                for b in blocked:
                    print(f"{b['path']} is locked on {name} by {b['holder']} until "
                          f"{b['expires_at']}" + (f": {b['reason']}" if b.get("reason") else ""),
                          file=sys.stderr)
                print("commit refused; ask the holder, or --ignore-locks to commit locally "
                      "(the push will still be refused)", file=sys.stderr)
                return 1
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


def cmd_policy(args) -> int:
    repo = _repo(args)
    a, b = _pair(args)
    pol = load_policy(repo, b)
    report = analyze(repo, a, b) if pol else None
    res = evaluate_policy(pol, report, "check",
                          observed_base=repo.latest_evidence(repo.resolve(a), "observed") is not None)
    _dump(res) if args.json else print(render_policy(res))
    return 0 if res["ok"] else 1


def cmd_release_create(args) -> int:
    m = create_release(_repo(args), args.name, args.rev, out=args.out, run_kicad=args.kicad,
                       override_policy=args.override_policy)
    if m["policy"].get("override"):
        print(f"WARNING: released despite policy violations ({', '.join(m['policy']['violations'])}); "
              f"override recorded: {m['policy']['override']['reason']}", file=sys.stderr)
    print(f"release {m['name']} -> revision {m['revision'][:10]}  validation {m['validation']['status']}")
    print(f"  {len(m['artifacts'])} artifacts pinned, manifest sha256 {m['manifest_sha256'][:16]}")
    return 0


def cmd_release_sign(args) -> int:
    sig = sign_release(_repo(args), args.name, args.key, args.signer)
    print(f"signed release {args.name} with {sig['key_id']} ({sig['signer']})")
    return 0


def cmd_release_verify(args) -> int:
    res = verify_release(_repo(args), args.release, worktree=args.worktree,
                         require_signature=args.require_signature)
    if args.json:
        _dump(res)
    else:
        for c in res["problems"]:
            print(f"FAIL {c['check']}: {c['detail']}")
        signed = (f"signed ({res['valid_signatures']} valid signature(s))" if res["signed"]
                  else "UNSIGNED: hashes prove consistency, not who vouched for it")
        print(f"release {res['release']}: {'OK' if res['ok'] else 'VERIFICATION FAILED'} "
              f"({len(res['checks'])} checks); {signed}")
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


def cmd_device_sign(args) -> int:
    sig = sign_device(_repo(args), args.serial, args.key, args.signer)
    print(f"signed passport record of {args.serial} with {sig['key_id']} ({sig['signer']})")
    return 0


def cmd_key(args) -> int:
    if args.key_cmd == "gen":
        k = sg.generate_key(args.name)
        print(f"created key {k['name']} ({k['key_id']}) at {k['path']}")
        print(f"  public key: {k['public_key']}")
        if args.trust:
            repo = _repo(args)
            sg.trust_key(repo.root, k["name"], k["public_key"], now_iso(), args.role)
            repo.audit.append("key.trust", k["key_id"], {"name": k["name"]})
            print(f"  trusted in {sg.trust_path(repo.root)} (commit it)")
        return 0
    repo = _repo(args)
    if args.key_cmd == "trust":
        pub = args.public or sg.public_from_secret(sg.load_secret(args.name))
        entry = sg.trust_key(repo.root, args.name, pub, now_iso(), args.role)
        repo.audit.append("key.trust", entry["key_id"], {"name": args.name})
        print(f"trusted {args.name} ({entry['key_id']}) for {', '.join(entry['roles'])}")
    elif args.key_cmd == "revoke":
        sg.revoke_key(repo.root, args.key_id)
        repo.audit.append("key.revoke", args.key_id)
        print(f"revoked {args.key_id}; signatures by it no longer verify")
    else:
        keys = sg.load_trust(repo.root)["keys"]
        for kid, e in sorted(keys.items()):
            print(f"{kid}  {e['name']:16} {','.join(e['roles']):18} added {e['added_at']}")
        if not keys:
            print("no trusted keys")
    return 0


def cmd_token(args) -> int:
    repo = _repo(args)
    store = TokenStore(repo.dir)
    if args.token_cmd == "create":
        token = store.create(args.user, args.role, now_iso())
        repo.audit.append("token.create", args.user, {"role": args.role})
        print(f"token for {args.user} ({args.role}); shown once, store it now:\n{token}")
    elif args.token_cmd == "revoke":
        n = store.revoke(args.token_id)
        repo.audit.append("token.revoke", args.token_id)
        print(f"revoked {n} token(s)")
    else:
        for t in store.list():
            print(f"{t['id']}  {t['user']:16} {t['role']:9} created {t['created_at']}")
    return 0


def cmd_serve(args) -> int:
    try:
        import uvicorn

        from vcm_api import create_app
    except ImportError:
        print("vcm serve needs the API extras: pip install 'vcm-hardware[api]'", file=sys.stderr)
        return 2
    from vcm_api.app import AuthConfigError
    try:
        app = create_app(str(_repo(args).root), insecure=args.insecure)
    except AuthConfigError as e:
        print(f"vcm: error: {e}", file=sys.stderr)
        return 1
    if args.insecure:
        print("WARNING: serving WITHOUT authentication (--insecure)", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _observation(args) -> dict:
    """One merged observation from --observed / --redfish / --nvidia-smi files."""
    parts = []
    for f in args.observed or []:
        parts.append(json.loads(Path(f).read_text()))
    for f in args.redfish or []:
        parts.append(parse_redfish(Path(f).read_text()))
    for f in args.nvidia_smi or []:
        parts.append(parse_nvidia_smi(Path(f).read_text()))
    if not parts:
        raise VcmError("give at least one of --observed, --redfish, --nvidia-smi")
    obs = merge_observations(parts)
    obs["captured_at"] = now_iso()
    return obs


def cmd_inventory(args) -> int:
    if args.inv_cmd == "fetch-redfish":
        import os

        from .redfish_fetch import fetch
        password = os.environ.get(args.password_env, "")
        if not password:
            print(f"set the BMC password in ${args.password_env}", file=sys.stderr)
            return 2
        cap = fetch(args.url, args.user, password, ca_file=args.ca_file,
                    verify_tls=not args.insecure_tls)
        Path(args.out).write_text(json.dumps(cap, indent=1) + "\n")
        print(f"captured {len(cap['resources'])} Redfish resources into {args.out}")
        return 0
    obs = _observation(args)
    if args.inv_cmd == "manifest":
        draft = to_node_manifest(obs)
        text = json.dumps(draft, indent=1) + "\n"
        Path(args.out).write_text(text) if args.out else print(text, end="")
        if args.out:
            print(f"drafted {args.out}: {len(draft['components'])} components. Connections, "
                  "power domains and chassis cooling are not observable: declare them by hand.")
        return 0
    text = json.dumps(obs, indent=1) + "\n"
    Path(args.out).write_text(text) if args.out else print(text, end="")
    if args.out:
        print(f"observed {len(obs['components'])} components from {', '.join(obs['sources'])} "
              f"into {args.out}")
    return 0


def cmd_drift(args) -> int:
    repo = _repo(args)
    obs = _observation(args)
    rev = repo.get_revision(args.rev)
    design = design_from_revision(repo, rev)
    meta = node_meta(design)
    if not meta:
        raise VcmError(f"{args.rev} has no node manifest to compare against")
    res = compare_drift(design, meta, obs)
    if args.attach:
        repo.attach_evidence(rev.id, "observed", {"observation": obs, "drift": res,
                                                  "captured_at": obs.get("captured_at", "")})
        repo.audit.append("observe", rev.id, {"status": res["status"],
                                              "sources": obs["sources"]})
    if args.json:
        _dump(res)
    else:
        print(f"{res['node']} vs {args.rev} ({rev.id[:10]}): {res['status'].upper()} "
              f"[{', '.join(res['sources'])}]")
        for f in res["findings"]:
            print(f"  {f['severity'].upper():8} {f['title']}")
        print(f"  checked: {', '.join(res['checked_types'] + res['checked_firmware']) or 'nothing'}")
        if res["unchecked_types"]:
            print(f"  NOT checked (no source reports them): {', '.join(res['unchecked_types'])}")
        if args.attach:
            print(f"  attached as evidence to {rev.id[:10]}; `vcm impact` will cite it")
    return 1 if res["status"] == "drifted" else 0


def _criteria(args) -> dict:
    return {"type_": args.type, "model": args.model, "mpn": args.mpn,
            "firmware": args.firmware, "rack": args.rack, "release": args.release}


def _units_csv(units: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["serial", "rack", "position", "release", "revision", "components", "firmware"])
    for u in units:
        w.writerow([u["serial"], u["rack"] or "", u["position"] or "", u["release"],
                    u["revision"][:12], " ".join(f"{c['ref']}={c['model']}" for c in u["components"]),
                    " ".join(f"{k}={v}" for k, v in sorted(u["firmware"].items()))])
    return buf.getvalue()


def cmd_fleet(args) -> int:
    repo = _repo(args)
    c = args.fleet_cmd
    if c == "rack":
        if args.rack_cmd == "define":
            r = define_rack(repo, args.name, args.budget, args.cooling, args.site, args.units)
            print(f"rack {r['name']}: budget {r.get('power_budget_w', 'none')} W")
        else:
            for r in list_racks(repo):
                print(f"{r['name']:16} budget {r.get('power_budget_w', '-')!s:>8} W  "
                      f"site {r.get('site') or '-'}")
        return 0
    if c == "place":
        place_device(repo, args.serial, args.rack, args.position)
        print(f"{args.serial} -> rack {args.rack}" + (f" U{args.position}" if args.position else ""))
        return 0
    if c in ("query", "recall"):
        if c == "recall" and not any((args.type, args.model, args.mpn, args.firmware)):
            raise VcmError("a recall needs --model, --mpn, --type or --firmware")
        res = recall(repo, **_criteria(args)) if c == "recall" else fleet_query(repo, **_criteria(args))
        units = res["units"] if c == "recall" else res
        if args.json:
            _dump(res)
        elif args.csv:
            print(_units_csv(units), end="")
        else:
            for u in units:
                comps = ", ".join(f"{x['ref']}={x['model']}" for x in u["components"][:4])
                more = len(u["components"]) - 4
                print(f"{u['serial']:14} rack {u['rack'] or '-':10} release {u['release']:8} "
                      f"{comps}{f' (+{more})' if more > 0 else ''}")
            if c == "recall":
                print(f"{res['count']} unit(s), {res['components']} component(s); by rack: "
                      + ", ".join(f"{k} {len(v)}" for k, v in sorted(res["by_rack"].items())))
            else:
                print(f"{len(units)} unit(s)")
        return 0
    if c == "power":
        res = rack_power(repo, args.upgrade_to, args.release)
        if args.json:
            _dump(res)
        else:
            for r in res["racks"]:
                line = (f"{r['rack']:14} {len(r['units']):3} unit(s)  now {r['now_w']:7.0f} W "
                        f"[{r['now_status']}]")
                if res["upgrade_to"]:
                    line += f"  after {r['after_w']:7.0f} W [{r['after_status']}]"
                print(line + (f"  budget {r['budget_w']:g} W" if r["budget_w"] else ""))
                if r["unknown"]:
                    print(f"{'':14} draw unknown for {', '.join(r['unknown'])}")
            print(res["note"])
        return 1 if res["over_after"] else 0
    if c == "rollout":
        res = plan_rollout(repo, args.to, args.release, args.max_per_rack, args.wave_size,
                           args.allow_critical)
        if args.json:
            _dump(res)
            return 0
        for w in res["waves"]:
            print(f"wave {w['wave']}: " + ", ".join(f"{u['serial']} ({u['rack']})" for u in w["units"]))
        for b in res["blocked"]:
            print(f"BLOCKED  {b['serial']}: {'; '.join(b['critical'][:3])}")
        for d in res["deferred"]:
            print(f"DEFERRED {d['serial']}: {d['reason']}")
        print(f"{res['scheduled']} scheduled in {len(res['waves'])} wave(s), "
              f"{len(res['blocked'])} blocked, {len(res['deferred'])} deferred")
        return 0
    # netbox
    if args.url:
        import os

        from .netbox_fetch import fetch_all
        token = os.environ.get(args.token_env, "")
        if not token:
            print(f"set the NetBox API token in ${args.token_env}", file=sys.stderr)
            return 2
        devices = fetch_all(args.url, "/api/dcim/devices/", token)
        racks = fetch_all(args.url, "/api/dcim/racks/", token)
    elif args.devices:
        devices = json.loads(Path(args.devices).read_text())
        racks = json.loads(Path(args.racks).read_text()) if args.racks else None
    else:
        raise VcmError("give --devices FILE (saved /api/dcim/devices/ JSON) or --url")
    res = import_netbox(repo, devices, racks, args.budget_field)
    if args.json:
        _dump(res)
    else:
        print(f"placed {len(res['placed'])} unit(s); racks created {len(res['racks_created'])}, "
              f"updated {len(res['racks_updated'])}")
        if res["unmatched_in_netbox"]:
            print(f"  {len(res['unmatched_in_netbox'])} NetBox device(s) not registered in VCM: "
                  + ", ".join(d['name'] or d['serial'] for d in res['unmatched_in_netbox'][:10]))
        if res["not_in_netbox"]:
            print(f"  {len(res['not_in_netbox'])} VCM unit(s) not found in NetBox: "
                  + ", ".join(res["not_in_netbox"][:10]))
    return 0


def cmd_remote(args) -> int:
    repo = _repo(args)
    if args.remote_cmd == "add":
        cfg = rm.add_remote(repo, args.name, args.url)
        print(f"remote {args.name} -> {cfg['url']}")
    else:
        for name, cfg in sorted(rm.remotes(repo).items()):
            print(f"{name}\t{cfg['url']}")
    return 0


def _conflicts(res: dict) -> None:
    for t in res.get("tag_conflicts", []):
        print(f"  CONFLICT tag {t}: differs between local and remote; left unchanged")
    for r in res.get("record_conflicts", []):
        print(f"  CONFLICT {r}: differs between local and remote; left unchanged")


def cmd_push(args) -> int:
    res = rm.push(_repo(args), args.remote, args.branch)
    if res["up_to_date"]:
        print(f"{res['remote']}/{res['branch']} already up to date")
    else:
        print(f"pushed {res['branch']} to {res['remote']}: "
              f"{(res['from'] or 'new')[:10]} -> {res['to'][:10]}, {res['objects']} object(s), "
              f"{len(res['tags'])} tag(s), {len(res['records'])} record(s)")
    _conflicts(res)
    return 0


def cmd_pull(args) -> int:
    repo = _repo(args)
    if args.cmd == "fetch":
        res = rm.fetch(repo, args.remote)
        print(f"fetched {res['objects']} object(s), {len(res['tags'])} tag(s), "
              f"{len(res['records'])} record(s) from {args.remote}")
    else:
        res = rm.pull(repo, args.remote, args.branch)
        if res["fast_forward"]:
            print(f"fast-forwarded {res['branch']}: {(res['before'] or 'none')[:10]} -> "
                  f"{res['after'][:10]}")
        else:
            print(f"{res['branch']} is up to date with {args.remote}")
    _conflicts(res)
    return 0


def cmd_lock(args) -> int:
    repo = _repo(args)
    remote = rm.Remote.of(repo, args.remote)
    if args.lock_cmd == "acquire":
        lock = rm.acquire_lock(remote, args.path, args.ttl, args.reason)
        repo.audit.append("lock.acquire", args.path, {"remote": args.remote,
                                                     "expires_at": lock["expires_at"]})
        print(f"locked {args.path} for {lock['holder']} until {lock['expires_at']}")
    elif args.lock_cmd == "release":
        lock = rm.release_lock(remote, args.path, force=args.force)
        repo.audit.append("lock.release" + (".forced" if args.force else ""), args.path,
                          {"remote": args.remote, "holder": lock["holder"]})
        print(f"released {args.path} (was held by {lock['holder']})")
    else:
        locks = rm.list_locks(remote)
        for lk in locks:
            state = "expired" if lk["expired"] else "until " + lk["expires_at"]
            print(f"{lk['path']:40} {lk['holder']:14} {state}"
                  + (f"  {lk['reason']}" if lk.get("reason") else ""))
        if not locks:
            print("no locks")
    return 0


def cmd_mcp(args) -> int:
    return serve(str(_repo(args).root))


def cmd_proposal(args) -> int:
    repo = _repo(args)
    by = getattr(args, "by", None) or os.environ.get("VCM_AUTHOR", "")
    if args.prop_cmd == "list":
        items = list_proposals(repo, args.status)
        if args.json:
            _dump(items)
        for p in items if not args.json else []:
            print(f"{p['id']}  {p['status']:9} {p['action']:16} {p['proposer']}  "
                  + ", ".join(f"{k}={v}" for k, v in sorted(p["args"].items()))[:70])
        if not items and not args.json:
            print("no proposals")
        return 0
    if args.prop_cmd == "show":
        rec = get_proposal(repo, args.id)
        _dump(rec) if args.json else print(render_proposal(rec))
        return 0
    if args.prop_cmd == "approve":
        rec = get_proposal(repo, args.id)
        print(render_proposal(rec))
        if not args.yes:
            if not sys.stdin.isatty():
                print("refusing to approve without confirmation: re-run with --yes after "
                      "reading the above", file=sys.stderr)
                return 1
            if input(f"Approve {args.id} as {by or '?'}? [y/N] ").strip().lower() != "y":
                print("not approved")
                return 1
        rec = approve_proposal(repo, args.id, by)
        print(f"approved {rec['id']} as {by}: {rec['result']}")
        return 0
    rec = reject(repo, args.id, by, args.reason)
    print(f"rejected {rec['id']} as {by}")
    return 0


def cmd_audit(args) -> int:
    log = _repo(args).audit
    if args.audit_cmd == "verify":
        res = log.verify()
        if args.json:
            _dump(res)
        else:
            print(f"audit log intact: {res['events']} events, head {res['head'][:16]}" if res["ok"]
                  else f"AUDIT LOG BROKEN at event {res['broken_at']}: {res['reason']}")
        return 0 if res["ok"] else 1
    evs = log.events()[-args.n:] if args.n else log.events()
    if args.json:
        _dump(evs)
        return 0
    for e in evs:
        print(f"{e['seq']:5} {e['at']} {e['actor']:12} {e['action']:16} {e['subject']}")
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
    if args.catalog_cmd == "review":
        return _catalog_review(args, catalog)
    if args.catalog_cmd == "unreviewed":
        rows = [(p, unverified_fields(p)) for p in catalog.parts
                if not args.type or p.get("type") == args.type]
        rows = [(p, f) for p, f in rows if f]
        for p, fields in sorted(rows, key=lambda x: (x[0].get("type", ""), x[0]["match"][0])):
            print(f"  {p['match'][0]:26} {p.get('type', ''):6} unverified: {', '.join(fields)}")
        print(f"{len(rows)} of {len(catalog.parts)} parts have values no human has verified")
        return 0
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


def _catalog_review(args, catalog: Catalog) -> int:
    part = catalog.find(args.mpn, args.mpn)
    if part is None:
        print(f"{args.mpn}: not in the catalog; import it first", file=sys.stderr)
        return 1
    values = {}
    for item in args.set or []:
        key, _, raw = item.partition("=")
        values[key] = float(raw) if key in NUMERIC_FIELDS else raw
    try:
        reviewed = review_part(part, values, args.source, args.by, now_iso(), args.confirm or [])
    except CatalogError as e:
        print(f"vcm: error: {e}", file=sys.stderr)
        return 1
    target = USER_CATALOG if args.scope == "user" else Path.cwd() / PROJECT_CATALOG_DIR
    target.mkdir(parents=True, exist_ok=True)
    dest = target / "reviews.json"
    existing = json.loads(dest.read_text())["parts"] if dest.is_file() else []
    key = reviewed["match"][0].upper()
    existing = [p for p in existing if p["match"][0].upper() != key] + [reviewed]
    dest.write_text(json.dumps({"parts": existing}, indent=1) + "\n")
    left = unverified_fields(reviewed)
    print(f"recorded review of {reviewed['match'][0]} by {args.by} in {dest}")
    print("  part is now REVIEWED" if reviewed["reviewed"] else
          f"  still unverified: {', '.join(left)}")
    try:
        Repository.discover(Path.cwd()).audit.append(
            "catalog.review", reviewed["match"][0],
            {"fields": sorted([*values, *(args.confirm or [])]), "source": args.source},
            actor=args.by)
    except VcmError:
        pass  # reviewing outside a repository is fine; there is just no audit log to write to
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
    s.add_argument("--ignore-locks", action="store_true",
                   help="commit even if a remote lock is held by someone else")
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
    s = add("policy", cmd_policy, "check a change against the committed vcm-policy.json", True)
    s.add_argument("a", nargs="?")
    s.add_argument("b", nargs="?")
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
    s.add_argument("--override-policy", metavar="REASON",
                   help="release despite vcm-policy.json violations; the reason is recorded")
    s = rel.add_parser("sign", help="sign a release manifest with a trusted Ed25519 key")
    s.set_defaults(fn=cmd_release_sign)
    s.add_argument("name")
    s.add_argument("--key", required=True, help="private key name in ~/.vcm/keys")
    s.add_argument("--signer", help="signer name recorded with the signature (default: key name)")
    s = rel.add_parser("verify")
    s.set_defaults(fn=cmd_release_verify)
    s.add_argument("release", help="release name or manifest file")
    s.add_argument("--worktree", action="store_true")
    s.add_argument("--require-signature", action="store_true",
                   help="fail unless a trusted key has signed the release")
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
    s = dev.add_parser("sign", help="sign a unit's registration record")
    s.set_defaults(fn=cmd_device_sign)
    s.add_argument("serial")
    s.add_argument("--key", required=True)
    s.add_argument("--signer")
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

    k = sub.add_parser("key", help="signing keys and the trust database")
    k = k.add_subparsers(dest="key_cmd", required=True)
    s = k.add_parser("gen", help="create an Ed25519 signing key in ~/.vcm/keys")
    s.set_defaults(fn=cmd_key)
    s.add_argument("name")
    s.add_argument("--trust", action="store_true", help="also add it to this repo's trustdb.json")
    s.add_argument("--role", action="append", choices=["release", "passport"],
                   help="what the key may sign (repeatable; default both)")
    s = k.add_parser("trust", help="add a public key to trustdb.json")
    s.set_defaults(fn=cmd_key)
    s.add_argument("name")
    s.add_argument("--public", help="hex public key (default: derive from the local key)")
    s.add_argument("--role", action="append", choices=["release", "passport"])
    s = k.add_parser("revoke", help="remove a key from trustdb.json")
    s.set_defaults(fn=cmd_key)
    s.add_argument("key_id")
    k.add_parser("list", help="trusted keys").set_defaults(fn=cmd_key)

    def obs_args(sp):
        sp.add_argument("--observed", action="append", help="vcm-observed/1 JSON (repeatable)")
        sp.add_argument("--redfish", action="append", help="Redfish capture JSON (repeatable)")
        sp.add_argument("--nvidia-smi", action="append",
                        help="nvidia-smi --query-gpu CSV or -q -x XML (repeatable)")

    inv = sub.add_parser("inventory", help="read what a machine reports about itself")
    inv = inv.add_subparsers(dest="inv_cmd", required=True)
    s = inv.add_parser("import", help="normalise captures into one vcm-observed/1 file")
    s.set_defaults(fn=cmd_inventory)
    obs_args(s)
    s.add_argument("--out")
    s = inv.add_parser("manifest", help="draft a node manifest from captures")
    s.set_defaults(fn=cmd_inventory)
    obs_args(s)
    s.add_argument("--out")
    s = inv.add_parser("fetch-redfish", help="capture a live BMC over the network (explicit)")
    s.set_defaults(fn=cmd_inventory)
    s.add_argument("url", help="e.g. https://10.0.0.21")
    s.add_argument("--user", required=True)
    s.add_argument("--password-env", default="VCM_BMC_PASSWORD",
                   help="environment variable holding the password")
    s.add_argument("--ca-file", help="CA bundle for the BMC's certificate")
    s.add_argument("--insecure-tls", action="store_true",
                   help="skip certificate verification (self-signed BMCs; prefer --ca-file)")
    s.add_argument("--out", required=True)

    s = add("drift", cmd_drift, "compare a revision's node manifest with the real machine", True)
    obs_args(s)
    s.add_argument("--rev", default="HEAD")
    s.add_argument("--attach", action="store_true",
                   help="store the observation as evidence on the revision")

    fl = sub.add_parser("fleet", help="racks, fleet queries, recalls, rack power, rollouts")
    fl = fl.add_subparsers(dest="fleet_cmd", required=True)
    rk = fl.add_parser("rack", help="define or list racks").add_subparsers(dest="rack_cmd",
                                                                          required=True)
    s = rk.add_parser("define")
    s.set_defaults(fn=cmd_fleet)
    s.add_argument("name")
    s.add_argument("--budget", type=float, help="power budget in W")
    s.add_argument("--cooling", type=float, help="cooling capacity in W")
    s.add_argument("--site", default="")
    s.add_argument("--units", type=int, help="rack height in U")
    rk.add_parser("list").set_defaults(fn=cmd_fleet)
    s = fl.add_parser("place", help="record which rack a unit is installed in")
    s.set_defaults(fn=cmd_fleet)
    s.add_argument("serial")
    s.add_argument("--rack", required=True)
    s.add_argument("--position", type=int)
    for name, help_ in (("query", "units matching parts, firmware, rack or release"),
                        ("recall", "every unit carrying a part or firmware, grouped for action")):
        s = fl.add_parser(name, help=help_)
        s.set_defaults(fn=cmd_fleet)
        s.add_argument("--type", help="component type, e.g. gpu, dimm, psu")
        s.add_argument("--model", help="model substring")
        s.add_argument("--mpn", help="part-number substring")
        s.add_argument("--firmware", action="append", metavar="EXPR",
                       help="e.g. gpu_driver<535.104 or bios=3.4 (repeatable)")
        s.add_argument("--rack")
        s.add_argument("--release")
        s.add_argument("--json", action="store_true")
        s.add_argument("--csv", action="store_true")
    s = fl.add_parser("power", help="declared draw per rack, now and after an upgrade")
    s.set_defaults(fn=cmd_fleet)
    s.add_argument("--upgrade-to", metavar="REV")
    s.add_argument("--release", help="only units currently on this release move")
    s.add_argument("--json", action="store_true")
    s = fl.add_parser("rollout", help="plan a fleet change in rack-safe waves")
    s.set_defaults(fn=cmd_fleet)
    s.add_argument("--to", required=True, metavar="REV")
    s.add_argument("--release", help="only units currently on this release")
    s.add_argument("--max-per-rack", type=int, default=1)
    s.add_argument("--wave-size", type=int, default=10)
    s.add_argument("--allow-critical", action="store_true",
                   help="schedule units even when the change has critical findings")
    s.add_argument("--json", action="store_true")
    s = fl.add_parser("netbox", help="import rack placement from NetBox")
    s.set_defaults(fn=cmd_fleet)
    s.add_argument("--devices", help="saved /api/dcim/devices/ JSON")
    s.add_argument("--racks", help="saved /api/dcim/racks/ JSON")
    s.add_argument("--url", help="NetBox base URL (fetches live; explicit network access)")
    s.add_argument("--token-env", default="NETBOX_TOKEN")
    s.add_argument("--budget-field", default="power_budget_w",
                   help="rack custom field holding the power budget in W")
    s.add_argument("--json", action="store_true")

    r = sub.add_parser("remote", help="shared remotes (a directory on a shared mount)")
    r = r.add_subparsers(dest="remote_cmd", required=True)
    s = r.add_parser("add")
    s.set_defaults(fn=cmd_remote)
    s.add_argument("name")
    s.add_argument("url", help="path or file:// URL of the shared directory")
    r.add_parser("list").set_defaults(fn=cmd_remote)
    s = add("push", cmd_push, "send revisions, tags and records to a remote (fast-forward only)")
    s.add_argument("remote", nargs="?", default="origin")
    s.add_argument("--branch")
    s = add("fetch", cmd_pull, "download revisions, tags and records from a remote")
    s.add_argument("remote", nargs="?", default="origin")
    s = add("pull", cmd_pull, "fetch and fast-forward the current branch")
    s.add_argument("remote", nargs="?", default="origin")
    s.add_argument("--branch")
    lk = sub.add_parser("lock", help="lock files on a remote instead of merging them")
    lk = lk.add_subparsers(dest="lock_cmd", required=True)
    for name in ("acquire", "release", "list"):
        s = lk.add_parser(name)
        s.set_defaults(fn=cmd_lock)
        s.add_argument("--remote", default="origin")
        if name != "list":
            s.add_argument("path", help="repository-relative file path")
        if name == "acquire":
            s.add_argument("--ttl", type=float, default=8.0, help="hours (default 8)")
            s.add_argument("--reason", default="")
        if name == "release":
            s.add_argument("--force", action="store_true", help="break someone else's lock")

    t = sub.add_parser("token", help="API tokens bound to a user and a role")
    t = t.add_subparsers(dest="token_cmd", required=True)
    s = t.add_parser("create")
    s.set_defaults(fn=cmd_token)
    s.add_argument("user")
    s.add_argument("--role", choices=ROLES, default="viewer")
    s = t.add_parser("revoke")
    s.set_defaults(fn=cmd_token)
    s.add_argument("token_id")
    t.add_parser("list").set_defaults(fn=cmd_token)

    s = add("serve", cmd_serve, "serve the REST API and dashboard (authenticated by default)")
    s.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--insecure", action="store_true",
                   help="serve without authentication; only on a trusted network")

    add("mcp", cmd_mcp, "serve VCM to an AI agent over MCP (stdio, offline, read-only plus proposals)")
    pp = sub.add_parser("proposal", help="changes an agent has asked for; a person decides")
    pp = pp.add_subparsers(dest="prop_cmd", required=True)
    s = pp.add_parser("list")
    s.set_defaults(fn=cmd_proposal)
    s.add_argument("--status", choices=["pending", "approved", "rejected"])
    s.add_argument("--json", action="store_true")
    s = pp.add_parser("show")
    s.set_defaults(fn=cmd_proposal)
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s = pp.add_parser("approve", help="perform the proposed action as you")
    s.set_defaults(fn=cmd_proposal)
    s.add_argument("id")
    s.add_argument("--by", help="your name (default: $VCM_AUTHOR)")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s = pp.add_parser("reject")
    s.set_defaults(fn=cmd_proposal)
    s.add_argument("id")
    s.add_argument("--by", help="your name (default: $VCM_AUTHOR)")
    s.add_argument("--reason", default="")

    au = sub.add_parser("audit", help="hash-chained audit log")
    au = au.add_subparsers(dest="audit_cmd", required=True)
    s = au.add_parser("log")
    s.set_defaults(fn=cmd_audit)
    s.add_argument("-n", type=int)
    s.add_argument("--json", action="store_true")
    s = au.add_parser("verify")
    s.set_defaults(fn=cmd_audit)
    s.add_argument("--json", action="store_true")

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
    s = c.add_parser("review", help="record that a human checked part values against a source")
    s.set_defaults(fn=cmd_catalog)
    s.add_argument("mpn")
    s.add_argument("--by", required=True, help="who checked it")
    s.add_argument("--source", required=True, help="citation, e.g. 'datasheet DS-123 rev 2 p.4'")
    s.add_argument("--set", action="append", metavar="FIELD=VALUE",
                   help="correct a value while reviewing it (repeatable)")
    s.add_argument("--confirm", action="append", metavar="FIELD",
                   help="confirm an existing value as correct (repeatable)")
    s.add_argument("--scope", choices=["user", "project"], default="project")
    s = c.add_parser("unreviewed", help="parts whose values no human has verified, by field")
    s.set_defaults(fn=cmd_catalog)
    s.add_argument("--type")
    s = c.add_parser("import", help="add parts from a JSON or CSV file")
    s.set_defaults(fn=cmd_catalog)
    s.add_argument("file")
    s.add_argument("--scope", choices=["user", "project"], default="project",
                   help="user: ~/.vcm/catalog; project: ./vcm-catalog (committed with the design)")
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
