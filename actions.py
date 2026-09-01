#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""brenda actions — the ONLY part of brenda that modifies your files.

Everything runs through plan objects: building a plan is a dry run (it
produces a JSON manifest of copy/move/remove operations, changes nothing),
applying executes it, undo reverses it from the manifest, purge permanently
deletes what an applied quarantine action moved into brenda's quarantine
(after you had the chance to go look at the files yourself).

Rules enforced here, not by the caller:
  - sources must be collections of the referenced scan run (no arbitrary paths)
  - copies land only in the user-chosen import target
  - moves land only inside the brenda quarantine tree
  - nothing is ever deleted outside the quarantine tree (purge, user-driven)
  - every op is journaled to actions.log; every action keeps its manifest

Plan kinds:
  import      copy the run's "new to you" files into a local target dir
  quarantine  move a whole redundant collection into quarantine (reviewable)
  merge       merge a copy/twin collection into a primary: byte-identical
              files -> quarantine; unique files -> move into the primary
              (collision-renamed); emptied dirs -> removed (undo recreates)
  dedupe      within one run: for each md5 keep the first instance (collection
              order), quarantine the rest — the reviewable generic strategy

Requirements: Python 3.8+, standard library only.
"""

import datetime
import json
import os
import secrets
import shutil
import sys

import frm

VALID_KINDS = ("import", "quarantine", "merge", "dedupe")


# --------------------------------------------------------------------------
# paths / journal
# --------------------------------------------------------------------------

def data_home():
    return frm.data_home()


def actions_dir():
    return os.path.join(data_home(), "actions")


def quarantine_root():
    return os.path.join(data_home(), "quarantine")


def log_path():
    return os.path.join(data_home(), "actions.log")


def _journal(event, plan):
    entry = {"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "event": event, "id": plan["id"], "kind": plan["kind"],
             "run": plan["run"], "collection": plan.get("collection"),
             "counts": plan["counts"]}
    os.makedirs(os.path.dirname(log_path()), exist_ok=True)
    with open(log_path(), "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _new_id():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" \
        + secrets.token_hex(3)


def _save(plan):
    os.makedirs(actions_dir(), exist_ok=True)
    p = os.path.join(actions_dir(), plan["id"] + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(plan, f, indent=1, sort_keys=True)
    os.replace(tmp, p)
    return p


def load_plan(action_id):
    p = os.path.join(actions_dir(), action_id + ".json")
    if not os.path.isfile(p):
        raise KeyError(f"no such action: {action_id}")
    with open(p) as f:
        return json.load(f)


def list_actions(limit=20):
    if not os.path.isdir(actions_dir()):
        return []
    out = []
    for fn in sorted(os.listdir(actions_dir()), reverse=True):
        if fn.endswith(".json"):
            try:
                out.append(load_plan(fn[:-5]))
            except (json.JSONDecodeError, KeyError):
                pass
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# scope guards
# --------------------------------------------------------------------------

def _within(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def _assert_collection(run, collection_path):
    for d in run["dirs"]:
        if d["path"] == collection_path:
            return d
    raise ValueError(f"not a collection of this run: {collection_path}")


def _load_run(rundir):
    """Re-load a run's dirs + hashes (small subset of compare.load_run)."""
    rp = os.path.join(rundir, "report.json")
    if not os.path.isfile(rp):
        raise SystemExit(f"not a brenda/frm run (no report.json): {rundir}")
    with open(rp) as f:
        report = json.load(f)
    hashes = {}
    hp = os.path.join(rundir, "hashes.tsv")
    if os.path.isfile(hp):
        with open(hp) as f:
            for line in f:
                if "\t" in line:
                    h, p = line.rstrip("\n").split("\t", 1)
                    hashes[p] = h
    return report, hashes


def _file_list(rundir, collection_path, root):
    rel = frm.dir_short(collection_path, root).replace(os.sep, "__")
    fl = os.path.join(rundir, f"files-{rel}.txt")
    if os.path.isfile(fl):
        with open(fl) as f:
            return [x for x in f.read().splitlines() if x]
    return []


# --------------------------------------------------------------------------
# plan builders (all dry runs — they only compute, never touch)
# --------------------------------------------------------------------------

def plan_import(run_dir, collection_path, target):
    """Copy the run's 'new to you' files of one collection into target,
    preserving the structure under the collection."""
    report, _ = _load_run(run_dir)
    coll = _assert_collection(report, collection_path)
    root = report["meta"]["root"]
    new = _new_files_from_compare(run_dir, collection_path)
    if not new:
        raise ValueError("no 'new to you' files recorded for this collection "
                         "(run brenda compare first, and make sure this "
                         "collection still has new files)")
    target = os.path.abspath(os.path.expanduser(target))
    ops = []
    for src in new:
        rel = os.path.relpath(src, collection_path)
        ops.append({"op": "copy", "src": src,
                    "dst": os.path.join(target, rel),
                    "why": "new to you"})
    plan = {"id": _new_id(), "kind": "import", "run": run_dir,
            "collection": collection_path, "target": target,
            "root": root, "ops": ops,
            "counts": {"copy": len(ops)},
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "planned", "notes": []}
    if not os.path.isdir(target):
        plan["notes"].append(f"target will be created: {target}")
    _check_sources_exist(plan)
    _journal("planned", plan)
    _save(plan)
    return plan


def _new_files_from_compare(run_dir, collection_path):
    cp = os.path.join(run_dir, "compare.json")
    if not os.path.isfile(cp):
        raise ValueError(f"no compare.json in {run_dir} — run "
                         "brenda compare first")
    with open(cp) as f:
        c = json.load(f)
    for e in c["per_collection"]:
        if e["path"] == collection_path:
            return e["new"]
    raise ValueError(f"collection not in the compare results: {collection_path}")


def plan_quarantine(run_dir, collection_path):
    """Move a whole collection directory into quarantine (reviewable,
    reversible). Use for confirmed-redundant collections (e.g. a perfect
    mirror)."""
    report, _ = _load_run(run_dir)
    coll = _assert_collection(report, collection_path)
    root = report["meta"]["root"]
    if not os.path.isdir(collection_path):
        raise ValueError(f"collection directory not found (drive mounted?): "
                         f"{collection_path}")
    rel = frm.dir_short(collection_path, root)
    ops = [{"op": "move_dir", "src": collection_path,
            "dst": os.path.join(quarantine_root(), "collections", rel),
            "why": coll.get("kind_note", "quarantined collection")}]
    plan = {"id": _new_id(), "kind": "quarantine", "run": run_dir,
            "collection": collection_path, "root": root, "ops": ops,
            "counts": {"move_dir": 1},
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "planned", "notes": []}
    _journal("planned", plan)
    _save(plan)
    return plan


def _find_run_of(collection_path):
    """Find the run dir (data home) whose report lists this collection."""
    runs_root = os.path.join(data_home(), "runs")
    if not os.path.isdir(runs_root):
        return None
    for name in sorted(os.listdir(runs_root), reverse=True):
        rp = os.path.join(runs_root, name, "report.json")
        if not os.path.isfile(rp):
            continue
        try:
            with open(rp) as f:
                report = json.load(f)
        except json.JSONDecodeError:
            continue
        if any(d.get("path") == collection_path for d in report["dirs"]):
            return os.path.join(runs_root, name)
    return None


def plan_merge(run_dir, primary_path, copy_path):
    """Merge the copy/twin collection into the primary: byte-identical files
    -> quarantine; unique files -> move into the primary (collision-renamed);
    emptied directories -> removed. Requires hashes.tsv (content decisions).
    The primary may live on ANOTHER drive — its run is located by its
    collection path and its md5s come from that run's hashes.tsv."""
    report, hashes = _load_run(run_dir)
    c = _assert_collection(report, copy_path)
    root = report["meta"]["root"]
    if not hashes:
        raise ValueError("this run has no hashes.tsv — content decisions "
                         "need hashes; re-scan without --no-dedup")
    if primary_path == copy_path:
        raise ValueError("primary and copy are the same collection")

    # primary may be in this run or (cross-drive merge) in another run
    primary_files = None
    primary_md5 = None
    pr_run_dir = run_dir
    if any(d["path"] == primary_path for d in report["dirs"]):
        primary_files = _file_list(run_dir, primary_path, root)
        primary_md5 = {hashes[f] for f in primary_files if f in hashes}
    else:
        pr_run_dir = _find_run_of(primary_path)
        if not pr_run_dir:
            raise ValueError(f"primary is not a collection of any known run: "
                             f"{primary_path}")
        pr_report, pr_hashes = _load_run(pr_run_dir)
        pr_root = pr_report["meta"]["root"]
        primary_files = _file_list(pr_run_dir, primary_path, pr_root)
        primary_md5 = {pr_hashes[f] for f in primary_files if f in pr_hashes}

    for d in (primary_path, copy_path):
        if not os.path.isdir(d):
            raise ValueError(f"directory not found (drive mounted?): {d}")

    copy_files = _file_list(run_dir, copy_path, root)
    q_root = os.path.join(quarantine_root(), "merges", _new_id() + os.sep)
    ops = []
    n_q = n_m = 0
    for src in copy_files:
        h = hashes.get(src)
        rel = os.path.relpath(src, copy_path)
        if h and h in primary_md5:
            ops.append({"op": "move", "src": src,
                        "dst": os.path.join(q_root, rel),
                        "why": "byte-identical copy exists in primary"})
            n_q += 1
        else:
            dst = os.path.join(primary_path, rel)
            if os.path.exists(dst):          # same name, different bytes
                base, ext = os.path.splitext(dst)
                i = 1
                while os.path.exists(dst):
                    dst = f"{base} (merged {i}){ext}"
                    i += 1
            ops.append({"op": "move", "src": src, "dst": dst,
                        "why": "unique to this collection"})
            n_m += 1
    # emptied dirs get removed deepest-first (undo recreates them)
    dirs = []
    for base, ds, _fs in os.walk(copy_path, topdown=False, followlinks=False):
        if base != copy_path:
            dirs.append(base)
    for d in dirs:
        ops.append({"op": "rmdir", "src": d, "why": "emptied by merge"})
    ops.append({"op": "rmdir", "src": copy_path, "why": "emptied by merge"})
    notes = [f"primary lives in another run: {pr_run_dir}"] \
        if pr_run_dir != run_dir else []
    plan = {"id": _new_id(), "kind": "merge", "run": run_dir,
            "collection": copy_path, "primary": primary_path, "root": root,
            "ops": ops,
            "counts": {"quarantined": n_q, "merged": n_m, "rmdir": len(dirs) + 1},
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "planned", "notes": notes}
    _check_sources_exist(plan)
    _journal("planned", plan)
    _save(plan)
    return plan


def plan_dedupe(run_dir):
    """Within one run: for each md5 keep the first instance (collection
    order), quarantine every later copy. File-level, reviewable, reversible.
    Cross-collection only — mirrors of the same collection inside one run are
    exactly what this targets."""
    report, hashes = _load_run(run_dir)
    root = report["meta"]["root"]
    if not hashes:
        raise ValueError("this run has no hashes.tsv — dedupe needs hashes; "
                         "re-scan without --no-dedup")
    seen = set()
    ops = []
    for d in report["dirs"]:
        if d.get("kind") not in ("collection", "android", "ipod"):
            continue
        for src in _file_list(run_dir, d["path"], root):
            h = hashes.get(src)
            if not h:
                continue
            if h in seen:
                rel = frm.dir_short(d["path"], root)
                ops.append({"op": "move", "src": src,
                            "dst": os.path.join(quarantine_root(), "dedupe",
                                                _new_id() + os.sep, rel,
                                                os.path.relpath(src, d["path"])),
                            "why": f"byte-identical to an earlier copy ({h[:8]})"})
            else:
                seen.add(h)
    if not ops:
        raise ValueError("no byte-identical duplicates within this run")
    plan = {"id": _new_id(), "kind": "dedupe", "run": run_dir,
            "collection": None, "root": root, "ops": ops,
            "counts": {"quarantined": len(ops)},
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "planned", "notes": []}
    _check_sources_exist(plan)
    _journal("planned", plan)
    _save(plan)
    return plan


def _check_sources_exist(plan):
    """Dry-run sanity: every copy/move source must exist right now (or the
    plan notes it as missing). Missing sources are kept in the plan but the
    caller sees the count."""
    missing = 0
    for op in plan["ops"]:
        if op["op"] in ("copy", "move", "move_dir") \
                and not os.path.exists(op["src"]):
            missing += 1
    if missing:
        plan["notes"].append(f"{missing} source(s) missing right now "
                             "(drive unmounted?) — those ops will be skipped "
                             "at apply time")


# --------------------------------------------------------------------------
# apply / undo / purge
# --------------------------------------------------------------------------

def apply_plan(plan):
    """Execute the ops in order. Missing sources are skipped (noted). Returns
    the updated plan."""
    if plan["status"] != "planned":
        raise ValueError(f"action {plan['id']} is {plan['status']}, "
                         "only planned actions can be applied")
    done = skipped = 0
    errors = []
    for op in plan["ops"]:
        kind = op["op"]
        src = op.get("src")
        dst = op.get("dst")
        try:
            if kind == "copy":
                if not os.path.isfile(src):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    raise FileExistsError(f"refusing to overwrite: {dst}")
                shutil.copy2(src, dst, follow_symlinks=False)
            elif kind == "move":
                if not os.path.exists(src):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    raise FileExistsError(f"destination exists: {dst}")
                shutil.move(src, dst)
            elif kind == "move_dir":
                if not os.path.isdir(src):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    raise FileExistsError(f"destination exists: {dst}")
                shutil.move(src, dst)
            elif kind == "rmdir":
                # only remove if truly empty right now (the moves above
                # should have emptied it)
                if os.path.isdir(src) and not os.listdir(src):
                    os.rmdir(src)
                # a non-empty rmdir is not an error — notes below
            else:
                raise ValueError(f"unknown op kind: {kind}")
            done += 1
        except Exception as e:                      # noqa: BLE001 — journal it
            errors.append(f"{kind} {src}: {e}")
    plan["status"] = "applied"
    plan["applied"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan["result"] = {"done": done, "skipped": skipped, "errors": errors[:50]}
    if skipped:
        plan["notes"].append(f"{skipped} op(s) skipped — source missing")
    if errors:
        plan["notes"].append(f"{len(errors)} op(s) FAILED — see result.errors")
    _journal("applied", plan)
    _save(plan)
    return plan


def undo(action_id):
    """Reverse an applied action using its manifest (reverse op order)."""
    plan = load_plan(action_id)
    if plan["status"] == "undone":
        return plan
    if plan["status"] != "applied":
        raise ValueError(f"action {action_id} is {plan['status']} — only "
                         "applied actions can be undone")
    done = skipped = 0
    errors = []
    for op in reversed(plan["ops"]):
        kind = op["op"]
        src, dst = op.get("src"), op.get("dst")
        try:
            if kind == "copy":
                if os.path.isfile(dst):
                    os.unlink(dst)                  # brenda-created file
                    _prune_empty_dirs(os.path.dirname(dst),
                                      stop=plan.get("target") or "/")
            elif kind in ("move", "move_dir"):
                if os.path.exists(dst) and not os.path.exists(src):
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.move(dst, src)
                elif os.path.exists(dst) and os.path.exists(src):
                    errors.append(f"undo move: both exist {src} / {dst}")
            elif kind == "rmdir":
                if not os.path.isdir(src):
                    os.makedirs(src, exist_ok=True)
            done += 1
        except Exception as e:                      # noqa: BLE001
            errors.append(f"undo {kind} {src}: {e}")
    plan["status"] = "undone"
    plan["result"]["undo_done"] = done
    plan["result"]["undo_skipped"] = skipped
    if errors:
        plan["notes"].append(f"undo: {len(errors)} problem(s) — "
                             "see result.errors")
    _journal("undone", plan)
    _save(plan)
    return plan


def _prune_empty_dirs(path, stop):
    """Remove now-empty directories up to (not including) stop."""
    path = os.path.realpath(path)
    stop = os.path.realpath(stop)
    while _within(path, stop) and path != stop:
        try:
            if os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
            else:
                break
        except OSError:
            break
        path = os.path.dirname(path)


def purge(action_id):
    """PERMANENTLY delete the quarantined files of an applied quarantine /
    merge / dedupe action. Only possible after apply (you had your review);
    undo is no longer possible afterwards."""
    plan = load_plan(action_id)
    if plan["status"] != "applied":
        raise ValueError(f"action {action_id} is {plan['status']} — purge "
                         "needs an applied action")
    if plan["kind"] == "import":
        raise ValueError("import actions copy; purge would delete imported "
                         "music — undo instead")
    removed = 0
    roots = set()
    for op in plan["ops"]:
        if op["op"] in ("move", "move_dir"):
            q = os.path.realpath(op["dst"])
            if _within(q, quarantine_root()):
                roots.add(q if os.path.isdir(q) else os.path.dirname(q))
        elif op["op"] == "copy":
            continue
    for r in sorted(roots):
        if os.path.isdir(r):
            # count files before removal for the receipt
            for base, _ds, fs in os.walk(r):
                removed += len(fs)
            shutil.rmtree(r)
    _prune_empty_dirs(os.path.dirname(next(iter(roots))) if roots
                      else quarantine_root(), quarantine_root())
    plan["status"] = "purged"
    plan["result"]["purged_files"] = removed
    _journal("purged", plan)
    _save(plan)
    return plan


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def self_test():
    print("brenda actions self-test (plans/apply/undo/purge on temp trees)")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="brenda-actions-test-")
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'ok  ' if cond else 'FAIL'}] {name}")
        if not cond:
            ok = False

    try:
        os.environ["BRENDA_DATA_HOME"] = os.path.join(tmp, "data")
        # recompute paths after the env override
        for name in ("data_home", "actions_dir", "quarantine_root", "log_path"):
            globals()[name].__wrapped__ if hasattr(globals()[name], "__wrapped__") else None
        # (functions read frm.data_home() live, so the override just works)

        # --- a fake drive with two collections: A (primary) and B (twin) ---
        drive = os.path.join(tmp, "drive")
        a = os.path.join(drive, "music", "Music")
        b = os.path.join(drive, "other", "Music")
        os.makedirs(os.path.join(a, "Alpha"))
        os.makedirs(os.path.join(b, "Alpha"))
        os.makedirs(os.path.join(b, "Beta"))
        c1, c2, c3, c4 = (b"TWIN-CONTENT-ONE", b"PRIMARY-ONLY",
                          b"UNIQUE-TO-B", b"PRIMARY-EXTRA")
        with open(os.path.join(a, "Alpha", "01 - One.mp3"), "wb") as f:
            f.write(c1)
        with open(os.path.join(a, "Alpha", "02 - Two.mp3"), "wb") as f:
            f.write(c2)
        with open(os.path.join(a, "Alpha", "04 - Four.mp3"), "wb") as f:
            f.write(c4)
        with open(os.path.join(b, "Alpha", "01 - One.mp3"), "wb") as f:
            f.write(c1)                       # twin of A's
        with open(os.path.join(b, "Beta", "03 - Three.mp3"), "wb") as f:
            f.write(c3)                       # unique
        with open(os.path.join(b, "Beta", "05 - Five.mp3"), "wb") as f:
            f.write(b"BETA-EXTRA")            # keeps B over the min-audio rail

        # fake run via frm.analyze + export_run
        cfg = {"dedup": True, "songmatch": True, "workers": 2, "quiet": True,
               "min_audio": 3}
        data = frm.analyze(drive, cfg)
        rundir = os.path.join(tmp, "run")
        os.makedirs(rundir)
        frm.export_run(data, drive, rundir)

        # --- import (while B is intact) --------------------------------------
        target = os.path.join(tmp, "local")
        newfile = os.path.join(b, "Beta", "03 - Three.mp3")
        # fabricate compare.json marking the B Beta file as new
        with open(os.path.join(rundir, "compare.json"), "w") as f:
            json.dump({"per_collection": [{"path": b, "new": [newfile],
                                           "new_files": 1}]}, f)
        plan_imp = plan_import(rundir, b, target)
        check("import plan has 1 copy op",
              plan_imp["counts"] == {"copy": 1})
        check("import dry: nothing at target", not os.path.exists(target))
        apply_plan(plan_imp)
        check("import applied: file at target preserving structure",
              os.path.isfile(os.path.join(target, "Beta", "03 - Three.mp3")))
        undo(plan_imp["id"])
        check("import undo removed the copy (and empty dirs)",
              not os.path.exists(os.path.join(target, "Beta")))

        # --- merge: B into A -----------------------------------------------
        plan = plan_merge(rundir, a, b)
        check("merge plan: 1 quarantined + 2 merged + 3 rmdir",
              plan["counts"] == {"quarantined": 1, "merged": 2, "rmdir": 3})
        check("plan is dry (b untouched)",
              os.path.isfile(os.path.join(b, "Alpha", "01 - One.mp3")))
        apply_plan(plan)
        check("applied: twin file gone from B",
              not os.path.exists(os.path.join(b, "Alpha", "01 - One.mp3")))
        check("applied: unique file moved into A/Beta",
              os.path.isfile(os.path.join(a, "Beta", "03 - Three.mp3")))
        check("applied: B emptied and removed",
              not os.path.exists(b))
        check("quarantine holds the twin",
              any("One.mp3" in f for _r, _d, fs in
                  os.walk(quarantine_root()) for f in fs))

        # --- undo merge -----------------------------------------------------
        undo(plan["id"])
        check("undo: B restored",
              os.path.isfile(os.path.join(b, "Alpha", "01 - One.mp3"))
              and os.path.isfile(os.path.join(b, "Beta", "03 - Three.mp3")))
        check("undo: A/Beta file returned",
              not os.path.exists(os.path.join(a, "Beta", "03 - Three.mp3")))
        check("undo: A originals intact",
              os.path.isfile(os.path.join(a, "Alpha", "01 - One.mp3")))

        # --- re-apply then purge ---------------------------------------------
        plan2 = plan_merge(rundir, a, b)
        apply_plan(plan2)
        purge(plan2["id"])
        check("purge: quarantine merge tree gone",
              not os.path.exists(os.path.dirname(plan2["ops"][0]["dst"])))
        check("purged action cannot be undone",
              _undo_refuses(plan2["id"]))

        # --- dedupe: copy A's whole Alpha set into a second collection ------
        a2 = os.path.join(drive, "music2", "Music")
        os.makedirs(os.path.join(a2, "Alpha"))
        for n in ("01 - One.mp3", "02 - Two.mp3", "04 - Four.mp3"):
            shutil.copy(os.path.join(a, "Alpha", n),
                        os.path.join(a2, "Alpha", n))
        data = frm.analyze(drive, cfg)
        rundir2 = os.path.join(tmp, "run2")
        os.makedirs(rundir2)
        frm.export_run(data, drive, rundir2)
        plan4 = plan_dedupe(rundir2)
        check("dedupe found the 3 later copies", plan4["counts"] ==
              {"quarantined": 3})
        apply_plan(plan4)
        check("dedupe: later copies quarantined, originals intact",
              not os.path.exists(os.path.join(a2, "Alpha", "01 - One.mp3"))
              and os.path.isfile(os.path.join(a, "Alpha", "01 - One.mp3")))
        undo(plan4["id"])
        check("dedupe undo restores the copies",
              os.path.isfile(os.path.join(a2, "Alpha", "01 - One.mp3")))

        check("actions.log exists and has entries",
              os.path.isfile(log_path())
              and sum(1 for _ in open(log_path())) >= 8)
    finally:
        os.environ.pop("BRENDA_DATA_HOME", None)
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _undo_refuses(action_id):
    try:
        undo(action_id)
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    sys.exit(self_test())
