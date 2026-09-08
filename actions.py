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
import hashlib
import json
import os
import secrets
import shutil
import sys

import frm
from grooves import format_rank

VALID_KINDS = ("import", "quarantine", "merge", "dedupe", "delete",
               "variants")

# cover art + playlists complete an artist/album dir and go along for the
# ride when its music is merged/moved/imported. Everything else (zips, logs,
# executables, …) is JUNK: brenda never moves it and never deletes it — the
# containing dir simply survives until you deal with it yourself.
IMG_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}


def _rides(path):
    e = os.path.splitext(path)[1][1:].lower()
    return e in IMG_EXT or e in frm.PLAYLIST_EXT


def _is_audio(path):
    return os.path.splitext(path)[1][1:].lower() in frm.AUDIO_EXT


def _md5_file(path):
    """md5 of a small-ish file (images, playlists). None on error."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _live_files(root):
    out = []
    for base, _ds, fs in os.walk(root, followlinks=False):
        for f in fs:
            out.append(os.path.join(base, f))
    return out


def _collision_free(dst, tag="merged", planned=None):
    """First dst name that is free. planned = dst paths already assigned by
    OTHER OPS IN THE SAME PLAN (the drive does not know about them yet —
    without this, two same-named sources both get ' (merged 1)')."""
    def taken(p):
        return os.path.exists(p) or (planned is not None and p in planned)
    base, ext = os.path.splitext(dst)
    i = 1
    while taken(dst):
        dst = f"{base} ({tag} {i}){ext}"
        i += 1
    return dst


def _subtree_all_audio_new(base, new, live_audio):
    audio = [f for f in live_audio if f.startswith(base + os.sep)]
    return audio, bool(audio) and all(f in new for f in audio)


def _subtree_junk(base, live):
    """Files brenda will not move: neither audio, nor art/playlists."""
    return [f for f in live
            if f.startswith(base + os.sep)
            and not _is_audio(f) and not _rides(f)]


def _song_key(fp):
    """Normalized (artist-folder|track) key — frm's filename heuristic,
    format-blind. None when either half normalizes to nothing (never match
    on an empty half — too risky)."""
    a = frm.norm_name(os.path.basename(os.path.dirname(fp)))
    t = frm.norm_name(os.path.basename(fp))
    if not a or not t:
        return None
    return a + "|" + t


_PROPS_CACHE = {}


def _read_props(fp):
    """(tag richness, bitrate) via mutagen — headers only, no audio decode.
    Cached per (path, size, mtime). (0, 0) when unreadable or mutagen is
    absent; ranking then falls back to format + size."""
    try:
        st = os.lstat(fp)
        key = (fp, st.st_size, st.st_mtime)
    except OSError:
        return 0, 0
    if key in _PROPS_CACHE:
        return _PROPS_CACHE[key]
    tag_score = 0
    bitrate = 0
    try:
        import mutagen
        m = mutagen.File(fp, easy=True)
        if m is not None:
            if m.tags:
                for vals in m.tags.values():
                    if isinstance(vals, list) and any(str(v).strip()
                                                      for v in vals):
                        tag_score += 1
            info = getattr(m, "info", None)
            bitrate = int(getattr(info, "bitrate", 0) or 0)
    except Exception:                        # noqa: BLE001 — any parse trouble
        pass
    _PROPS_CACHE[key] = (tag_score, bitrate)
    return tag_score, bitrate


def _quality(fp):
    """(format class, tag richness, bitrate-or-size, size): lossless beats
    lossy, then the better-tagged file, then the higher bitrate (lossy) or
    the bigger file (lossless / no mutagen). No audio is decoded — headers
    and tags only."""
    try:
        size = os.lstat(fp).st_size
    except OSError:
        size = 0
    rank = format_rank(fp)
    tag_score, bitrate = _read_props(fp)
    effective = bitrate if bitrate > 0 else size
    return (rank, tag_score, effective, size)


def _audio_live(root):
    return sorted(f for f in _live_files(root) if _is_audio(f))


def _group_songs(files, hashes):
    """Group same-song files: byte-identical content (md5 from the run's
    hashes.tsv) always groups together, and so does the same normalized
    song key (artist folder + title, format-blind). Returns deterministic
    list of groups (lists of paths)."""
    parent = {f: f for f in files}

    def find(f):
        while parent[f] != f:
            parent[f] = parent[parent[f]]
            f = parent[f]
        return f

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_key, by_md5 = {}, {}
    for f in files:
        k = _song_key(f)
        if k:
            if k in by_key:
                union(by_key[k], f)
            else:
                by_key[k] = f
        h = hashes.get(f)
        if h:
            if h in by_md5:
                union(by_md5[h], f)
            else:
                by_md5[h] = f
    groups = {}
    for f in files:
        groups.setdefault(find(f), []).append(f)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def _keep_best(files):
    """The one file to keep from a same-song group: lossless first, then
    better-tagged, then higher bitrate / bigger, then path order."""
    return sorted(files, key=lambda f: (-_quality(f)[0], -_quality(f)[1],
                                        -_quality(f)[2], f))[0]


def _clean_name(name):
    """Filename without brenda's collision tag — final collections should
    never carry '(merged 1)' plumbing in their names."""
    return frm.MERGE_TAG.sub("", name).strip() or name


def _dirs_left_empty(root, removed_files):
    """Dirs under root that hold NOTHING after removed_files are gone
    (no remaining files, no surviving subdir) — deepest-first list."""
    remaining = {}
    for f in _live_files(root):
        if f in removed_files:
            continue
        remaining[os.path.dirname(f)] = remaining.get(os.path.dirname(f), 0) + 1
    all_dirs = []
    for base, _ds, _fs in os.walk(root, topdown=False, followlinks=False):
        if base != root:
            all_dirs.append(base)
    alive = {d for d in all_dirs if remaining.get(d)}
    changed = True
    while changed:
        changed = False
        for d in all_dirs:
            if d in alive:
                continue
            if any(s.startswith(d + os.sep) and s in alive for s in all_dirs):
                alive.add(d)
                changed = True
    return sorted((d for d in all_dirs if d not in alive), reverse=True)


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
    with open(log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _new_id():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" \
        + secrets.token_hex(3)


def _save(plan):
    os.makedirs(actions_dir(), exist_ok=True)
    p = os.path.join(actions_dir(), plan["id"] + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(plan, f, indent=1, sort_keys=True)
    os.replace(tmp, p)
    return p


def load_plan(action_id):
    p = os.path.join(actions_dir(), action_id + ".json")
    if not os.path.isfile(p):
        raise KeyError(f"no such action: {action_id}")
    with open(p, encoding="utf-8") as f:
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
    with open(rp, encoding="utf-8") as f:
        report = json.load(f)
    hashes = {}
    hp = os.path.join(rundir, "hashes.tsv")
    if os.path.isfile(hp):
        with open(hp, encoding="utf-8") as f:
            for line in f:
                if "\t" in line:
                    h, p = line.rstrip("\r\n").split("\t", 1)
                    hashes[p] = h
    return report, hashes


def _file_list(rundir, collection_path, root):
    rel = frm.dir_short(collection_path, root).replace(os.sep, "__")
    fl = os.path.join(rundir, f"files-{rel}.txt")
    if os.path.isfile(fl):
        with open(fl, encoding="utf-8") as f:
            return [x for x in f.read().splitlines() if x]
    return []


# --------------------------------------------------------------------------
# plan builders (all dry runs — they only compute, never touch)
# --------------------------------------------------------------------------

def plan_import(run_dir, collection_path, target, move=False):
    """Bring the run's 'new to you' files of one collection into the target
    dir, preserving the structure under the collection. Copy by default;
    move=True relocates instead (undo puts it back).

    Granularity is directory-first: an artist dir that is entirely new to
    you and free of junk goes over WHOLE (music, cover art, playlists) —
    else an album dir that is entirely new and junk-free — else file-level:
    the new tracks plus the cover art and playlists of the dirs involved.
    Art/playlists already at the target are skipped. Junk (zips etc.) is
    never copied or moved."""
    report, _ = _load_run(run_dir)
    coll = _assert_collection(report, collection_path)
    root = report["meta"]["root"]
    new = set(_new_files_from_compare(run_dir, collection_path))
    if not new:
        raise ValueError("no 'new to you' files recorded for this collection "
                         "(run brenda compare first, and make sure this "
                         "collection still has new files)")
    target = os.path.abspath(os.path.expanduser(target))
    if not os.path.isdir(collection_path):
        raise ValueError(f"collection directory not found (drive mounted?): "
                         f"{collection_path}")
    op_kind = "move" if move else "copy"
    dir_kind = "move_dir" if move else "copy_dir"

    audio_all = [f for f in _file_list(run_dir, collection_path, root)
                 if os.path.isfile(f)]
    live = _live_files(collection_path)
    live_audio = {f for f in live if _is_audio(f)}
    audio_all = sorted(live_audio | {f for f in audio_all if os.path.isfile(f)})

    ops = []
    n_tracks = n_dirs = 0
    handled = set()                    # audio files covered by whole-dir ops
    moved_roots = []                   # dirs gone wholesale
    planned = set()                    # dsts assigned by THIS plan

    artists = sorted(d for d in os.listdir(collection_path)
                     if os.path.isdir(os.path.join(collection_path, d)))
    for a in artists:
        adir = os.path.join(collection_path, a)
        audio_a, all_new = _subtree_all_audio_new(adir, new, live_audio)
        junk_a = _subtree_junk(adir, live)
        dst_a = os.path.join(target, a)
        if all_new and not junk_a \
                and not os.path.exists(dst_a) and dst_a not in planned:
            ops.append({"op": dir_kind, "src": adir, "dst": dst_a,
                        "why": "whole artist dir is new to you"})
            planned.add(dst_a)
            handled.update(audio_a)
            n_tracks += len(audio_a)
            n_dirs += 1
            moved_roots.append(adir)
            continue
        for alb in sorted(d for d in os.listdir(adir)
                          if os.path.isdir(os.path.join(adir, d))):
            aldir = os.path.join(adir, alb)
            audio_al, all_new = _subtree_all_audio_new(aldir, new, live_audio)
            junk_al = _subtree_junk(aldir, live)
            dst_al = os.path.join(target, a, alb)
            if all_new and not junk_al \
                    and not os.path.exists(dst_al) and dst_al not in planned:
                ops.append({"op": dir_kind, "src": aldir, "dst": dst_al,
                            "why": "whole album dir is new to you"})
                planned.add(dst_al)
                handled.update(audio_al)
                n_tracks += len(audio_al)
                n_dirs += 1
                moved_roots.append(aldir)

    def in_moved(f):
        return any(f.startswith(r + os.sep) for r in moved_roots)

    leftovers = sorted(f for f in audio_all if f in new and f not in handled)
    ride_dirs = sorted({os.path.dirname(f) for f in leftovers})
    for d in ride_dirs:
        for f in _live_files(d):
            if not _rides(f) or in_moved(f):
                continue
            rel = os.path.relpath(f, collection_path)
            dst = os.path.join(target, rel)
            if os.path.exists(dst) or dst in planned:
                continue                 # art/playlist already at target
            ops.append({"op": op_kind, "src": f, "dst": dst,
                        "why": "cover art / playlist goes along"})
            planned.add(dst)
    for f in leftovers:
        rel = os.path.relpath(f, collection_path)
        # imports land under clean names — merge tags never travel home
        dst = _collision_free(os.path.join(
            target, os.path.dirname(rel), _clean_name(os.path.basename(f))),
            "imported", planned=planned)
        ops.append({"op": op_kind, "src": f, "dst": dst,
                    "why": "new to you"})
        planned.add(dst)
        n_tracks += 1

    # move-mode hygiene: dirs the move empties get removed (junk keeps its
    # dir alive, by design); copy-mode never touches the source tree
    if move:
        moved = {o["src"] for o in ops
                 if o["op"] in ("move", "move_dir")}

        def in_moved_dir(f):
            return any(f == r or f.startswith(r + os.sep)
                       for r in moved_roots)

        for d in _dirs_left_empty(collection_path, moved):
            if in_moved_dir(d):
                continue                  # whole-dir moves take theirs away
            ops.append({"op": "rmdir", "src": d,
                        "why": "emptied by move-import"})

    junk_n = len([f for f in live if not _is_audio(f) and not _rides(f)])
    plan = {"id": _new_id(), "kind": "import", "run": run_dir,
            "collection": collection_path, "target": target,
            "mode": "move" if move else "copy", "root": root, "ops": ops,
            "counts": {"tracks": n_tracks, "whole_dirs": n_dirs},
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "planned", "notes": []}
    if not os.path.isdir(target):
        plan["notes"].append(f"target will be created: {target}")
    if junk_n:
        plan["notes"].append(f"{junk_n} junk file(s) (zips etc.) in this "
                             "collection are NOT included — brenda never "
                             "moves or deletes those")
    _check_sources_exist(plan)
    _journal("planned", plan)
    _save(plan)
    return plan


def _new_files_from_compare(run_dir, collection_path):
    """The 'new to you' file list for one collection. Prefers the
    collection-only overlay (coll-compare.json) when it matches - THE CARD
    THE USER SAW IS THE LIST THE ACTION USES - and falls back to the run's
    whole-drive compare.json (whose 'new' may differ if other drives were
    in its index)."""
    ov_path = os.path.join(run_dir, "coll-compare.json")
    if os.path.isfile(ov_path):
        try:
            with open(ov_path, encoding="utf-8") as f:
                ov = json.load(f)
            if ov.get("collection") == collection_path:
                for e in ov.get("per_collection", []):
                    if e.get("path") == collection_path:
                        return e.get("new", [])
        except (OSError, json.JSONDecodeError):
            pass
    cp = os.path.join(run_dir, "compare.json")
    if not os.path.isfile(cp):
        raise ValueError(f"no compare results in {run_dir} - run "
                         "compare first")
    with open(cp, encoding="utf-8") as f:
        c = json.load(f)
    for e in c["per_collection"]:
        if e["path"] == collection_path:
            return e["new"]
    raise ValueError(f"collection not in the compare results: {collection_path}")
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


def plan_variants(run_dir, collection_path):
    """Within ONE collection: group audio by normalized song key (artist
    folder + title, format-blind) and keep exactly ONE version of each —
    lossless beats lossy, then the bigger file (grooves.format_rank + size;
    no audio content is read). Every other version — byte-twins included —
    moves into quarantine. Each quarantined variant records its keeper file,
    so purge is an instant isfile() check: a variant may only be purged
    while the kept version still exists. Cover art, playlists and junk are
    never touched. Reversible with undo."""
    report, _hashes = _load_run(run_dir)
    coll = _assert_collection(report, collection_path)
    root = report["meta"]["root"]
    if not os.path.isdir(collection_path):
        raise ValueError(f"collection directory not found (drive mounted?): "
                         f"{collection_path}")
    audio = _audio_live(collection_path)
    q_root = os.path.join(quarantine_root(), "variants", _new_id() + os.sep)
    planned = set()
    ops = []
    n_songs = 0
    freed = 0
    for grp in _group_songs(audio, _hashes):
        if len(grp) < 2:
            continue
        n_songs += 1
        keeper = _keep_best(grp)
        losers = [f for f in grp if f != keeper]
        # the winner must not carry merge-tag plumbing into the final
        # collection: after the losers vacate, it takes the clean name
        base = os.path.basename(keeper)
        clean_base = _clean_name(base)
        rename_dst = None
        if clean_base != base:
            cand = os.path.join(os.path.dirname(keeper), clean_base)
            if cand not in planned and (not os.path.exists(cand)
                                        or cand in set(losers)):
                rename_dst = cand
                planned.add(rename_dst)
        keeper_final = rename_dst or keeper
        for worse in losers:
            rel = os.path.relpath(worse, collection_path)
            dst = _collision_free(os.path.join(q_root, rel), planned=planned)
            planned.add(dst)
            try:
                freed += os.lstat(worse).st_size
            except OSError:
                pass
            ops.append({"op": "move", "src": worse, "dst": dst,
                        "keeper": keeper_final,
                        "why": f"same song, lesser version — kept "
                               f"{os.path.basename(keeper_final)}"})
        if rename_dst:
            ops.append({"op": "move", "src": keeper, "dst": rename_dst,
                        "why": "clean name — merge tag stripped"})
    moved = {o["src"] for o in ops if o.get("keeper")}
    dirs = _dirs_left_empty(collection_path, moved)
    for d in dirs:
        ops.append({"op": "rmdir", "src": d, "why": "emptied by variants"})
    if not ops:
        raise ValueError("no multi-version songs in this collection — every "
                         "song exists exactly once already")
    plan = {"id": _new_id(), "kind": "variants", "run": run_dir,
            "collection": collection_path, "root": root, "ops": ops,
            "counts": {"songs": n_songs, "quarantined": len(ops) - len(dirs),
                       "rmdir": len(dirs), "bytes_freed": freed},
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "planned",
            "notes": ["one copy per song: lossless > opus/ogg > m4a/aac > "
                      "mp3, then tag richness, then bitrate/size (tag "
                      "headers only — no audio decoded); matching is "
                      "filename-based (artist folder + title, format-blind) "
                      "— undo if a call is wrong; winners with '(merged N)' "
                      "names are renamed clean"]}
    _journal("planned", plan)
    _save(plan)
    return plan


def plan_delete(run_dir, collection_path):
    """Delete a whole collection directory from the drive — the explicit,
    instant end for a verified-redundant dump (a normal filesystem delete,
    not a slow secure-erase). Guard: EVERY music file in the collection must
    still have a byte-identical copy somewhere outside it (checked against
    every run's hashes.tsv and the local index, live existence) — otherwise
    the plan is refused and nothing is touched. Non-music files (cover art,
    playlists, zips) go too — that is the point of an explicit delete — and
    the confirm page names the counts. Permanent: no undo, journaled."""
    report, hashes = _load_run(run_dir)
    coll = _assert_collection(report, collection_path)
    root = report["meta"]["root"]
    if not os.path.isdir(collection_path):
        raise ValueError(f"collection directory not found (drive mounted?): "
                         f"{collection_path}")
    live = _live_files(collection_path)
    audio = [f for f in live if _is_audio(f)]
    non_music = len(live) - len(audio)
    md5s = _indexed_md5_map(exclude=collection_path)
    # this run's own hashes are the authority on what was here — merge them
    # in (the collection's own paths excluded: it cannot vouch for itself)
    excl_real = os.path.realpath(collection_path)
    for p, h in hashes.items():
        real = os.path.realpath(p)
        if _within(real, excl_real) or _within(real, quarantine_root()):
            continue
        md5s.setdefault(h, set()).add(real)
    guards = []
    problems = []
    for f in audio:
        h = hashes.get(f) or _md5_file(f)
        if not h:
            problems.append(f"{f}: unreadable, md5 unknown")
            continue
        guards.append({"path": f, "md5": h})
        if not any(os.path.isfile(p) for p in md5s.get(h, ())):
            problems.append(f"{f} ({h[:8]}…): no surviving byte-identical "
                            "copy outside this collection")
    if problems:
        raise ValueError(
            f"delete refused — {len(problems)} music file(s) in this "
            f"collection have no surviving copy elsewhere (first: "
            f"{problems[0]}). This directory is NOT safely deletable. "
            "Nothing was touched.")
    total = 0
    for f in live:
        try:
            total += os.lstat(f).st_size
        except OSError:
            pass
    plan = {"id": _new_id(), "kind": "delete", "run": run_dir,
            "collection": collection_path, "root": root,
            "ops": [{"op": "delete_dir", "src": collection_path,
                     "why": "verified redundant — every music file exists "
                            "elsewhere"}],
            "guards": guards,
            "counts": {"audio_files": len(audio), "non_music": non_music,
                       "bytes": total},
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "planned",
            "notes": [f"permanent: {len(audio)} music + {non_music} "
                      "non-music file(s) (art/playlists/zips) will be gone "
                      "— there is no undo"]}
    _journal("planned", plan)
    _save(plan)
    return plan


def _delete_guard(plan, hashes):
    """Re-verify at apply time: every music file the plan promises is
    redundant must still have a surviving copy. Raises before ANY deletion."""
    src = plan["ops"][0]["src"]
    if not os.path.isdir(src):
        raise ValueError("collection directory is already gone — nothing "
                         "to delete")
    md5s = _indexed_md5_map(exclude=src)
    excl_real = os.path.realpath(src)
    for p, h in hashes.items():
        real = os.path.realpath(p)
        if _within(real, excl_real) or _within(real, quarantine_root()):
            continue
        md5s.setdefault(h, set()).add(real)
    problems = []
    for g in plan.get("guards", []):
        if not os.path.isfile(g["path"]):
            continue                        # vanished already — fine
        if not any(os.path.isfile(p) for p in md5s.get(g["md5"], ())):
            problems.append(g["path"])
    if problems:
        raise ValueError(
            f"delete aborted at apply time — {len(problems)} music file(s) "
            f"lost their surviving copy since the plan was made (first: "
            f"{problems[0]}). Nothing was deleted.")


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
            with open(rp, encoding="utf-8") as f:
                report = json.load(f)
        except json.JSONDecodeError:
            continue
        if any(d.get("path") == collection_path for d in report["dirs"]):
            return os.path.join(runs_root, name)
    return None


def plan_merge(run_dir, primary_path, copy_path):
    """Merge the copy/twin collection into the primary. Decisions, in order:
      - byte-identical to a primary file -> quarantine (twin)
      - same SONG as a primary file, different bytes (filename heuristic,
        format-blind) -> variant: the better version is kept (lossless
        beats lossy, then bigger); a better incoming file swaps in while
        the primary's copy moves to quarantine, a worse incoming file is
        quarantined outright
      - duplicates of the same song inside the copy collection itself ->
        keep the best, quarantine the rest
      - otherwise unique -> move into the primary (collision-renamed)
    Whole artist/album dirs still move in one piece — but only when the
    subtree holds no primary variants either. Art/playlists ride along,
    junk never moves. The primary may live on ANOTHER drive. Requires
    hashes.tsv. Reversible with undo."""
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
    pr_root = root
    if any(d["path"] == primary_path for d in report["dirs"]):
        primary_files = [f for f in _file_list(run_dir, primary_path, root)
                         if os.path.isfile(f)]
        primary_md5 = {hashes[f] for f in primary_files if f in hashes}
    else:
        pr_run_dir = _find_run_of(primary_path)
        if not pr_run_dir:
            raise ValueError(f"primary is not a collection of any known run: "
                             f"{primary_path}")
        pr_report, pr_hashes = _load_run(pr_run_dir)
        pr_root = pr_report["meta"]["root"]
        primary_files = [f for f in _file_list(pr_run_dir, primary_path, pr_root)
                         if os.path.isfile(f)]
        primary_md5 = {pr_hashes[f] for f in primary_files if f in pr_hashes}

    for d in (primary_path, copy_path):
        if not os.path.isdir(d):
            raise ValueError(f"directory not found (drive mounted?): {d}")

    # what songs does the primary hold RIGHT NOW (live — it may have
    # received files from earlier merges since the scan)
    primary_by_key = {}
    for f in _audio_live(primary_path):
        k = _song_key(f)
        if k:
            primary_by_key.setdefault(k, []).append(f)

    # scan lists can be stale (files deleted/renamed since the scan) —
    # plans are made against the live drive only
    copy_files = [f for f in _file_list(run_dir, copy_path, root)
                  if os.path.isfile(f)]
    q_root = os.path.join(quarantine_root(), "merges", _new_id() + os.sep)
    live = _live_files(copy_path)
    live_audio = {f for f in live if _is_audio(f)}
    twins = {f for f in copy_files if hashes.get(f) and hashes[f] in primary_md5}
    extra_audio = sorted(f for f in live_audio if f not in set(copy_files))
    ride = [f for f in live if _rides(f)]
    junk = [f for f in live if not _is_audio(f) and not _rides(f)]

    def has_primary_variant(base):
        """True when any song in the subtree exists in the primary as a
        different-bytes version (whole-dir moves must avoid those)."""
        for f in live_audio:
            if f.startswith(base + os.sep) and f not in twins:
                k = _song_key(f)
                if k and k in primary_by_key:
                    return True
        return False

    def internally_varied(base):
        """True when the subtree itself holds 2+ versions of a song —
        a whole-dir move would smuggle the duplicates into the primary."""
        audio_b = [f for f in live_audio if f.startswith(base + os.sep)]
        return any(len(g) > 1 for g in _group_songs(audio_b, hashes))

    ops = []
    n_q = n_m = n_dirs = n_swap = n_vq = 0
    moved_roots = []                   # artist/album dirs gone wholesale
    planned = set()                    # dsts assigned by THIS plan — other
                                       # ops must not collide with them

    def in_moved(f):
        return any(f == r or f.startswith(r + os.sep) for r in moved_roots)

    # whole-dir merges first: an artist (or album) dir that is entirely
    # unique to this collection and junk-free moves into the primary in
    # one piece — cover art and playlists ride along.
    artists = sorted(d for d in os.listdir(copy_path)
                     if os.path.isdir(os.path.join(copy_path, d)))
    for a in artists:
        adir = os.path.join(copy_path, a)
        audio_a = [f for f in live_audio if f.startswith(adir + os.sep)]
        junk_a = _subtree_junk(adir, live)
        if audio_a and not any(f in twins for f in audio_a) and not junk_a \
                and not has_primary_variant(adir) \
                and not internally_varied(adir) \
                and not os.path.exists(os.path.join(primary_path, a)) \
                and os.path.join(primary_path, a) not in planned:
            ops.append({"op": "move_dir", "src": adir,
                        "dst": os.path.join(primary_path, a),
                        "why": "whole artist dir is unique to this collection"})
            planned.add(ops[-1]["dst"])
            moved_roots.append(adir)
            n_m += len(audio_a)
            n_dirs += 1
            continue
        for alb in sorted(d for d in os.listdir(adir)
                          if os.path.isdir(os.path.join(adir, d))):
            aldir = os.path.join(adir, alb)
            audio_al = [f for f in live_audio if f.startswith(aldir + os.sep)]
            junk_al = _subtree_junk(aldir, live)
            if audio_al and not any(f in twins for f in audio_al) and not junk_al \
                    and not has_primary_variant(aldir) \
                    and not internally_varied(aldir) \
                    and not os.path.exists(os.path.join(primary_path, a, alb)) \
                    and os.path.join(primary_path, a, alb) not in planned:
                ops.append({"op": "move_dir", "src": aldir,
                            "dst": os.path.join(primary_path, a, alb),
                            "why": "whole album dir is unique to this collection"})
                planned.add(ops[-1]["dst"])
                moved_roots.append(aldir)
                n_m += len(audio_al)
                n_dirs += 1

    # file-level: group the incoming audio by song (md5 + song-key union),
    # then decide per group (byte-twins -> quarantine; variants -> best
    # wins; unique -> in)
    incoming = [f for f in copy_files + extra_audio if not in_moved(f)]

    def twin_of_primary(src):
        h = hashes.get(src)
        return bool(h and h in primary_md5)

    for grp in _group_songs(incoming, hashes):
        # byte-twins go straight to quarantine, unaffected by variant logic
        grp_twins = [f for f in grp if twin_of_primary(f)]
        rest = [f for f in grp if f not in grp_twins]
        for src in grp_twins:
            rel = os.path.relpath(src, copy_path)
            dst = os.path.join(q_root, rel)
            ops.append({"op": "move", "src": src, "dst": dst,
                        "why": "byte-identical copy exists in primary"})
            planned.add(dst)
            n_q += 1
        if not rest:
            continue
        best_src = _keep_best(rest) if len(rest) > 1 else rest[0]
        k = _song_key(best_src)
        # best of the group vs the primary's version of the same song
        keeper_dst = None
        if k and k in primary_by_key:
            p_best = _keep_best(primary_by_key[k])
            if _quality(best_src) > _quality(p_best):
                # swap: the primary's copy is quarantined FIRST, then the
                # better version takes its (clean) slot — merge tags never
                # survive into the final collection
                rel = os.path.relpath(best_src, copy_path)
                dst_dir = os.path.join(primary_path, os.path.dirname(rel))
                dst = os.path.join(dst_dir, _clean_name(os.path.basename(
                    best_src)))
                if dst in planned:
                    dst = _collision_free(dst, planned=planned)
                qdst = _collision_free(os.path.join(q_root, "replaced",
                                                    os.path.basename(p_best)),
                                       planned=planned)
                ops.append({"op": "move", "src": p_best, "dst": qdst,
                            "keeper": dst,
                            "if_src": best_src,
                            "why": "replaced by a better version of the "
                                   "same song"})
                planned.add(qdst)
                n_q += 1
                ops.append({"op": "move", "src": best_src, "dst": dst,
                            "why": f"better version of a song the primary "
                                   f"has ({os.path.basename(p_best)})"})
                planned.add(dst)
                keeper_dst = dst
                n_m += 1
                n_swap += 1
            else:
                qdst = _collision_free(os.path.join(q_root, "variants",
                                                    os.path.basename(best_src)),
                                       planned=planned)
                ops.append({"op": "move", "src": best_src, "dst": qdst,
                            "keeper": p_best,
                            "why": f"worse variant — the primary already "
                                   f"has this song ({os.path.basename(p_best)})"})
                planned.add(qdst)
                n_q += 1
                n_vq += 1
                keeper_dst = p_best
        else:
            rel = os.path.relpath(best_src, copy_path)
            dst = _collision_free(os.path.join(primary_path, rel),
                                  planned=planned)
            ops.append({"op": "move", "src": best_src, "dst": dst,
                        "why": "unique to this collection"})
            planned.add(dst)
            keeper_dst = dst
            n_m += 1
        # the group's lesser versions never reach the primary
        for worse in rest:
            if worse == best_src:
                continue
            rel = os.path.relpath(worse, copy_path)
            qdst = _collision_free(os.path.join(q_root, "variants", rel),
                                   planned=planned)
            ops.append({"op": "move", "src": worse, "dst": qdst,
                        "keeper": keeper_dst,
                        "why": "duplicate version of a song in this "
                               "collection itself"})
            planned.add(qdst)
            n_q += 1
            n_vq += 1

    # cover art + playlists go along (junk never does)
    for src in ride:
        if in_moved(src):
            continue
        rel = os.path.relpath(src, copy_path)
        dst = os.path.join(primary_path, rel)
        if os.path.exists(dst) or dst in planned:
            h_src, h_dst = _md5_file(src), _md5_file(dst)
            if h_src and h_dst and h_src == h_dst:
                dst = os.path.join(q_root, rel)
                ops.append({"op": "move", "src": src, "dst": dst,
                            "why": "byte-identical art/playlist in primary"})
                planned.add(dst)
                n_q += 1
                continue
            dst = _collision_free(dst, planned=planned)
        ops.append({"op": "move", "src": src, "dst": dst,
                    "why": "cover art / playlist goes along"})
        planned.add(dst)
        n_m += 1

    # emptied dirs get removed deepest-first — but only dirs that will
    # actually empty (junk keeps its dir alive; undo recreates removed ones)
    junk_set = set(junk)
    all_dirs = []
    for base, ds, _fs in os.walk(copy_path, topdown=False, followlinks=False):
        if base != copy_path:
            all_dirs.append(base)
    alive = {d for d in all_dirs
             if any(os.path.join(d, f) in junk_set
                    for f in os.listdir(d))}
    changed = True
    while changed:
        changed = False
        for d in list(alive):
            p = os.path.dirname(d)
            if _within(p, copy_path) and p not in alive:
                alive.add(p)
                changed = True
    dirs = [d for d in all_dirs
            if d not in alive and not in_moved(d)]
    for d in sorted(dirs, reverse=True):
        ops.append({"op": "rmdir", "src": d, "why": "emptied by merge"})
    if copy_path not in alive:
        ops.append({"op": "rmdir", "src": copy_path, "why": "emptied by merge"})
    notes = [f"primary lives in another run: {pr_run_dir}"] \
        if pr_run_dir != run_dir else []
    if n_swap or n_vq:
        notes.append("variant matching is filename-based (artist folder + "
                     "title, format-blind): better versions replaced worse "
                     "ones — undo if a call is wrong")
    if junk:
        notes.append(f"{len(junk)} junk file(s) (zips etc.) stay put — "
                     "brenda never moves or deletes those; their dirs "
                     "survive the merge on purpose")
    plan = {"id": _new_id(), "kind": "merge", "run": run_dir,
            "collection": copy_path, "primary": primary_path,
            "primary_root": pr_root, "root": root,
            "ops": ops,
            "counts": {"quarantined": n_q, "merged": n_m,
                       "variants_kept": n_swap,
                       "variants_quarantined": n_vq,
                       "whole_dirs": n_dirs,
                       "rmdir": len(dirs) + (0 if copy_path in alive else 1)},
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

def apply_plan(plan, progress=None, resume=False):
    """Execute the ops in order. Missing sources are skipped (noted).

    Speed: consecutive plain file ops (copy/move with no swap guards) run
    through a small worker pool - parallel I/O overlaps the source drive's
    seek latency, which is what made big imports glacial. Order-sensitive
    ops (dir moves, keeper/swap pairs, rmdirs) stay sequential. It is safe
    to interrupt (Ctrl+C/restart) mid-apply: done ops stay done, the rest
    re-apply on the next go.

    progress(done, total) reports advancement for live banners.
    resume=True admits a plan already marked 'applying' (a background apply
    re-entering itself)."""
    if plan["status"] == "applying":
        if not resume:
            raise ValueError(f"action {plan['id']} is already applying — "
                             "watch the banner")
    elif plan["status"] != "planned":
        raise ValueError(f"action {plan['id']} is {plan['status']}, "
                         "only planned actions can be applied")
    if plan["kind"] == "delete":
        _report, hashes = _load_run(plan["run"])
        _delete_guard(plan, hashes)      # raises before anything is touched
    done = skipped = 0
    errors = []
    renamed = 0
    ops = plan["ops"]
    total = len(ops)

    def run_one(op):
        """Execute one pool-able op; returns (state, err)."""
        kind = op["op"]
        src = op.get("src")
        dst = op.get("dst")
        if kind == "move":
            if not os.path.exists(src):
                return "skipped", None
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                old = dst
                dst = _collision_free(dst)
                op["dst"] = dst
                for other in ops:
                    if other.get("keeper") == old:
                        other["keeper"] = dst
                return "renamed", None
            shutil.move(src, dst)
            return "done", None
        if kind == "copy":
            if not os.path.isfile(src):
                return "skipped", None
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                raise FileExistsError(f"refusing to overwrite: {dst}")
            shutil.copy2(src, dst, follow_symlinks=False)
            return "done", None
        return "inline", None

    def drain(batch, out):
        """Run a batch of plain ops in parallel; journal the results."""
        nonlocal done, skipped, renamed
        if not batch:
            return
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(run_one, op): op for op in batch}
            for fut, op in futs.items():
                state, err = fut.result()
                if state == "skipped":
                    skipped += 1
                elif state == "renamed":
                    renamed += 1
                    done += 1
                elif err:
                    out.append(f"{op['op']} {op.get('src')}: {err}")
                else:
                    done += 1
        batch.clear()
        if progress:
            progress(done, total)

    batch = []
    for op in ops:
        kind = op["op"]
        if kind in ("copy", "move") and not op.get("if_src") \
                and not op.get("if_dst") and not op.get("keeper"):
            batch.append(op)
            if len(batch) >= 24:
                drain(batch, errors)
            continue
        drain(batch, errors)               # drain before any special op
        src = op.get("src")
        dst = op.get("dst")
        try:
            if kind == "move":
                # paired swap: proceed only if the replacement still exists
                # at either end of its own move (it may not have landed —
                # drive changed since the plan — then keep the replaced file)
                if op.get("if_src") or op.get("if_dst"):
                    if not any(os.path.isfile(p) for p in
                               (op.get("if_src"), op.get("if_dst")) if p):
                        skipped += 1
                        continue
                if not os.path.exists(src):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    # safety net: the drive changed since the plan (another
                    # op, another session). Rename instead of losing the op —
                    # the manifest's dst is updated so undo follows it, and
                    # any keeper pointing at the old name follows too.
                    old = dst
                    dst = _collision_free(dst)
                    op["dst"] = dst
                    for other in ops:
                        if other.get("keeper") == old:
                            other["keeper"] = dst
                    renamed += 1
                shutil.move(src, dst)
                done += 1
            elif kind == "copy_dir":
                if not os.path.isdir(src):
                    skipped += 1
                    continue
                if os.path.exists(dst):
                    raise FileExistsError(f"refusing to overwrite: {dst}")
                shutil.copytree(src, dst, symlinks=True)
                done += 1
            elif kind == "move_dir":
                if not os.path.isdir(src):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    old = dst
                    dst = _collision_free(dst)
                    op["dst"] = dst
                    for other in ops:
                        if other.get("keeper") == old:
                            other["keeper"] = dst
                    renamed += 1
                shutil.move(src, dst)
                done += 1
            elif kind == "delete_dir":
                if not os.path.isdir(src):
                    skipped += 1
                    continue
                for _base, _ds, fs in os.walk(src):
                    done += len(fs)          # receipt: every file that goes
                shutil.rmtree(src)
            elif kind == "rmdir":
                # only remove if truly empty right now (the moves above
                # should have emptied it)
                if os.path.isdir(src) and not os.listdir(src):
                    os.rmdir(src)
                # a non-empty rmdir is not an error — notes below
            else:
                raise ValueError(f"unknown op kind: {kind}")
            if progress:
                progress(done, total)
        except Exception as e:                      # noqa: BLE001 — journal it
            errors.append(f"{kind} {src}: {e}")
    drain(batch, errors)                           # tail batch
    plan["status"] = "applied"
    plan["applied"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan["result"] = {"done": done, "skipped": skipped, "errors": errors[:50]}
    if renamed:
        plan["notes"].append(f"{renamed} destination(s) renamed on collision "
                             "at apply time — manifest updated, undo follows")
    if skipped:
        plan["notes"].append(f"{skipped} op(s) skipped — source missing")
    if errors:
        plan["notes"].append(f"{len(errors)} op(s) FAILED — see result.errors")
    _journal("applied", plan)
    _save(plan)
    return plan


def discard(action_id):
    """Throw away a planned action (the confirm never happened). Nothing has
    been touched on disk, so there is nothing to reverse — the entry stays
    in the journal for the record."""
    plan = load_plan(action_id)
    if plan["status"] != "planned":
        raise ValueError(f"action {action_id} is {plan['status']} — only "
                         "planned actions can be discarded")
    plan["status"] = "discarded"
    plan["discarded"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _journal("discarded", plan)
    _save(plan)
    return plan


def close(action_id):
    """Finalize an applied import: the user is happy, the moves stand as
    they are. Drops the undo button - after close there is no way back
    (the journal keeps the record). No data changes."""
    plan = load_plan(action_id)
    if plan["status"] != "applied":
        raise ValueError(f"action {action_id} is {plan['status']} — only "
                         "applied actions can be closed")
    if plan["kind"] != "import":
        raise ValueError("close is for import actions - quarantine/merge/"
                         "dedupe actions have undo and purge instead")
    plan["status"] = "closed"
    plan["closed"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _journal("closed", plan)
    _save(plan)
    return plan


def undo(action_id):
    """Reverse an applied action using its manifest (reverse op order)."""
    plan = load_plan(action_id)
    if plan["status"] == "undone":
        return plan
    if plan["kind"] == "delete":
        raise ValueError("delete is permanent by design — brenda kept no "
                         "copy to restore")
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
            elif kind == "copy_dir":
                if os.path.isdir(dst):
                    shutil.rmtree(dst)              # brenda-created tree
                    _prune_empty_dirs(os.path.dirname(dst),
                                      stop=plan.get("target") or "/")
            elif kind in ("move", "move_dir"):
                if os.path.exists(dst) and not os.path.exists(src):
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.move(dst, src)
                elif os.path.exists(dst) and os.path.isdir(src) \
                        and not os.listdir(src):
                    os.rmdir(src)               # undo rmdir's empty placeholder
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


def _indexed_md5_map(exclude=None):
    """md5 -> {realpath} across every run's hashes.tsv plus the local index.
    Paths under the quarantine tree are excluded — quarantined copies must
    never vouch for each other; only real survivors count. With exclude=DIR,
    paths under DIR are excluded too (a collection cannot vouch for itself,
    e.g. when it is the one about to be deleted)."""
    q_root = quarantine_root()
    excl_real = os.path.realpath(exclude) if exclude else None
    md5s = {}

    def add(path, h):
        if not path or not h:
            return
        try:
            real = os.path.realpath(path)
        except OSError:
            return
        if _within(real, q_root):
            return
        if excl_real and _within(real, excl_real):
            return
        md5s.setdefault(h, set()).add(real)

    runs_root = os.path.join(data_home(), "runs")
    if os.path.isdir(runs_root):
        for name in os.listdir(runs_root):
            hp = os.path.join(runs_root, name, "hashes.tsv")
            if not os.path.isfile(hp):
                continue
            try:
                with open(hp, encoding="utf-8") as f:
                    for line in f:
                        if "\t" in line:
                            h, p = line.rstrip("\r\n").split("\t", 1)
                            add(p, h)
            except OSError:
                continue
    idx_file = os.path.join(data_home(), "index", "local.json")
    try:
        with open(idx_file, encoding="utf-8") as f:
            idx = json.load(f)
        for sec in idx.get("roots", {}).values():
            for fp, rec in sec.get("files", {}).items():
                add(fp, rec.get("md5"))
    except (OSError, json.JSONDecodeError):
        pass
    return md5s


def _purge_problems(plan, hashes):
    """AUDIO files in quarantine for this plan whose byte-identical content
    no longer exists anywhere outside the quarantine tree. Empty list = the
    music is safe to purge. Only audio is checked — purge never deletes
    anything else, so nothing else needs a survivor."""
    q_root = quarantine_root()
    md5s = _indexed_md5_map()
    # the plan's own run is the most authoritative source (it hashed exactly
    # these files) — merge it in, quarantine paths excluded as everywhere else
    for p, h in hashes.items():
        real = os.path.realpath(p)
        if not _within(real, q_root):
            md5s.setdefault(h, set()).add(real)
    problems = []
    for op in plan["ops"]:
        if op["op"] not in ("move", "move_dir"):
            continue
        dst = op["dst"]
        if not _within(os.path.realpath(dst), q_root):
            continue
        if op["op"] == "move":
            keeper = op.get("keeper")
            if keeper:
                # variant ops carry their keeper: the purge is instant and
                # allowed only while the kept version still exists
                if os.path.isfile(keeper):
                    continue
                problems.append(f"{op['src']}: the kept version is gone "
                                f"({keeper}) — purge refused")
                continue
            files = [(dst, op["src"])]
        else:
            files = []
            for base, _ds, fs in os.walk(dst):
                for f in fs:
                    fq = os.path.join(base, f)
                    rel = os.path.relpath(fq, dst)
                    files.append((fq, os.path.join(op["src"], rel)))
        for fq, orig in files:
            if not os.path.isfile(fq) or not _is_audio(fq):
                continue                 # gone already, or not music
            h = hashes.get(orig)
            if not h:
                problems.append(f"{orig}: no md5 recorded in the run's "
                                "hashes.tsv — cannot verify a survivor")
                continue
            if not any(os.path.isfile(p) for p in md5s.get(h, ())):
                problems.append(f"{orig} (md5 {h[:8]}…): no surviving "
                                "byte-identical copy outside quarantine")
    return problems


def purge(action_id):
    """PERMANENTLY delete the quarantined MUSIC of an applied quarantine /
    merge / dedupe action. Only possible after apply (you had your review);
    undo is no longer possible afterwards.

    Two protections:
      - every quarantined audio file must still have a surviving
        byte-identical copy somewhere outside quarantine (checked against
        every run's hashes.tsv and the local index, with a real existence
        test on disk) — otherwise the purge is refused, nothing deleted
      - non-music files (cover art, playlists, zips…) are NEVER deleted:
        they stay in quarantine for you to review or keep forever
    """
    plan = load_plan(action_id)
    if plan["status"] != "applied":
        raise ValueError(f"action {action_id} is {plan['status']} — purge "
                         "needs an applied action")
    if plan["kind"] == "import":
        raise ValueError("import actions copy; purge would delete imported "
                         "music — undo instead")
    if plan["kind"] == "delete":
        raise ValueError("delete actions removed the directory directly — "
                         "nothing sits in quarantine to purge")
    _report, hashes = _load_run(plan["run"])
    problems = _purge_problems(plan, hashes)
    if problems:
        raise ValueError(
            f"purge refused — {len(problems)} quarantined file(s) have no "
            f"surviving copy outside quarantine (first: {problems[0]}). "
            "Nothing was deleted. undo this action, or restore the missing "
            "copies first. (If the surviving copies live on a drive, make "
            "sure it is mounted.)")
    removed = 0
    left = 0
    roots = set()
    for op in plan["ops"]:
        if op["op"] in ("move", "move_dir"):
            q = os.path.realpath(op["dst"])
            if _within(q, quarantine_root()):
                roots.add(q if os.path.isdir(q) else os.path.dirname(q))
        elif op["op"] == "copy":
            continue
    for r in sorted(roots):
        if not os.path.isdir(r):
            continue
        for base, _ds, fs in os.walk(r, topdown=False):
            for f in fs:
                fp = os.path.join(base, f)
                if _is_audio(fp):
                    os.unlink(fp)                 # music: verified redundant
                    removed += 1
                else:
                    left += 1                     # art/junk: never deleted
            try:
                if not os.listdir(base):
                    os.rmdir(base)                # only truly-empty dirs go
            except OSError:
                pass
    _prune_empty_dirs(os.path.dirname(next(iter(roots))) if roots
                      else quarantine_root(), quarantine_root())
    plan["status"] = "purged"
    plan["result"]["purged_files"] = removed
    plan["result"]["left_in_quarantine"] = left
    if left:
        plan["notes"].append(f"purge deleted only music: {left} non-music "
                             "file(s) (art/playlists/junk) remain in "
                             "quarantine for your review")
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
        with open(os.path.join(b, "Alpha", "cover.jpg"), "wb") as f:
            f.write(b"COVER-B-ALPHA")
        with open(os.path.join(b, "Alpha", "playlist.m3u"), "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        with open(os.path.join(b, "Alpha", "junk.zip"), "wb") as f:
            f.write(b"ZIP-JUNK-STAYS-PUT")
        with open(os.path.join(b, "Beta", "03 - Three.mp3"), "wb") as f:
            f.write(c3)                       # unique
        with open(os.path.join(b, "Beta", "05 - Five.mp3"), "wb") as f:
            f.write(b"BETA-EXTRA")            # keeps B over the min-audio rail
        with open(os.path.join(b, "Beta", "roadtrip.m3u"), "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")

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
        with open(os.path.join(rundir, "compare.json"), "w", encoding="utf-8") as f:
            json.dump({"per_collection": [{"path": b, "new": [newfile],
                                           "new_files": 1}]}, f)
        # fabricate the collection-only overlay the card was showing: it
        # says BOTH Beta tracks are new (the whole-drive compare.json said
        # only 03 - that mismatch is exactly the bug a real import caught)
        with open(os.path.join(rundir, "coll-compare.json"), "w") as f:
            json.dump({"collection": b, "meta": {"index_roots": ["/home/x"]},
                       "per_collection": [{"path": b, "new": [newfile,
                        os.path.join(b, "Beta", "05 - Five.mp3")]}],
                       "totals": {}}, f)
        plan_imp = plan_import(rundir, b, target)
        check("import plan: overlay new-list wins over the stale whole-drive "
              "compare (Beta becomes all-new -> whole-dir move)",
              plan_imp["counts"] == {"tracks": 2, "whole_dirs": 1})
        check("import dry: nothing at target", not os.path.exists(target))
        apply_plan(plan_imp)
        check("import applied: file at target preserving structure",
              os.path.isfile(os.path.join(target, "Beta", "03 - Three.mp3")))
        check("import applied: second overlay-new file moved too",
              os.path.isfile(os.path.join(target, "Beta", "05 - Five.mp3")))
        check("import applied: playlist went along",
              os.path.isfile(os.path.join(target, "Beta", "roadtrip.m3u")))
        undo(plan_imp["id"])
        check("import undo removed the copy (and empty dirs)",
              not os.path.exists(os.path.join(target, "Beta")))

        # --- merge: B into A -----------------------------------------------
        plan = plan_merge(rundir, a, b)
        check("merge plan: 1 quarantined + 4 merged + 1 whole dir, no rmdir "
              "(junk zip keeps its dir alive)",
              plan["counts"] == {"quarantined": 1, "merged": 4,
                                 "variants_kept": 0,
                                 "variants_quarantined": 0,
                                 "whole_dirs": 1, "rmdir": 0})
        check("plan is dry (b untouched)",
              os.path.isfile(os.path.join(b, "Alpha", "01 - One.mp3")))
        apply_plan(plan)
        check("applied: twin file gone from B",
              not os.path.exists(os.path.join(b, "Alpha", "01 - One.mp3")))
        check("applied: unique file moved into A/Beta",
              os.path.isfile(os.path.join(a, "Beta", "03 - Three.mp3")))
        check("applied: cover art went along",
              os.path.isfile(os.path.join(a, "Alpha", "cover.jpg")))
        check("applied: playlist went along",
              os.path.isfile(os.path.join(a, "Alpha", "playlist.m3u")))
        check("applied: whole album dir carried its playlist",
              os.path.isfile(os.path.join(a, "Beta", "roadtrip.m3u")))
        check("applied: junk zip NEVER moved",
              os.path.isfile(os.path.join(b, "Alpha", "junk.zip")))
        check("applied: B/Beta gone (whole dir moved)",
              not os.path.exists(os.path.join(b, "Beta")))
        check("applied: B/Alpha survives holding only the junk",
              os.path.isdir(os.path.join(b, "Alpha"))
              and os.listdir(os.path.join(b, "Alpha")) == ["junk.zip"])
        check("quarantine holds the twin",
              any("One.mp3" in f for _r, _d, fs in
                  os.walk(quarantine_root()) for f in fs))

        # --- undo merge -----------------------------------------------------
        undo(plan["id"])
        check("undo: B restored",
              os.path.isfile(os.path.join(b, "Alpha", "01 - One.mp3"))
              and os.path.isfile(os.path.join(b, "Beta", "03 - Three.mp3")))
        check("undo: art and playlists returned",
              os.path.isfile(os.path.join(b, "Alpha", "cover.jpg"))
              and os.path.isfile(os.path.join(b, "Beta", "roadtrip.m3u")))
        check("undo: A/Beta file returned",
              not os.path.exists(os.path.join(a, "Beta", "03 - Three.mp3")))
        check("undo: A originals intact",
              os.path.isfile(os.path.join(a, "Alpha", "01 - One.mp3")))

        # --- re-apply then purge ---------------------------------------------
        plan2 = plan_merge(rundir, a, b)
        apply_plan(plan2)
        qop = next(o for o in plan2["ops"]
                   if o["dst"].startswith(quarantine_root()))
        purge(plan2["id"])
        check("purge: quarantine merge tree gone",
              not os.path.exists(os.path.dirname(qop["dst"])))
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

        # --- purge guard: unique content with no survivor must refuse -------
        c3 = os.path.join(drive, "solo3", "Music")
        os.makedirs(os.path.join(c3, "Zulu"))
        for n, b in (("01 - Only.mp3", b"ONLY-HERE-X"),
                     ("02 - Only.mp3", b"ONLY-HERE-Y"),
                     ("03 - Only.mp3", b"ONLY-HERE-Z")):
            with open(os.path.join(c3, "Zulu", n), "wb") as f:
                f.write(b)
        data = frm.analyze(drive, cfg)
        rundir3 = os.path.join(tmp, "run3")
        os.makedirs(rundir3)
        frm.export_run(data, drive, rundir3)

        # whole-dir import: c3/Zulu is entirely new -> one copy_dir op
        target2 = os.path.join(tmp, "local2")
        with open(os.path.join(rundir3, "compare.json"), "w", encoding="utf-8") as f:
            json.dump({"per_collection": [{"path": c3, "new": [
                os.path.join(c3, "Zulu", "01 - Only.mp3"),
                os.path.join(c3, "Zulu", "02 - Only.mp3"),
                os.path.join(c3, "Zulu", "03 - Only.mp3")],
                "new_files": 3}]}, f)
        plan6 = plan_import(rundir3, c3, target2)
        check("import: whole dir when everything in it is new",
              plan6["counts"] == {"tracks": 3, "whole_dirs": 1})
        apply_plan(plan6)
        check("whole-dir import landed complete",
              os.path.isfile(os.path.join(target2, "Zulu", "01 - Only.mp3")))
        undo(plan6["id"])
        check("whole-dir import undo removed the tree (target dir itself "
              "correctly remains, empty)",
              os.path.isdir(target2)
              and not os.path.exists(os.path.join(target2, "Zulu")))

        # --- move-mode import: emptied dirs get removed ---------------------
        target5 = os.path.join(tmp, "local5")
        plan7 = plan_import(rundir3, c3, target5, move=True)
        apply_plan(plan7)
        check("move-import: music gone from the source collection",
              not os.path.exists(os.path.join(c3, "Zulu")))
        check("move-import: no files left behind in the source tree",
              sum(len(fs) for _b, _d, fs in os.walk(c3)) == 0)
        refused = False
        try:
            undo(plan7["id"])
        except ValueError:
            refused = True
        check("move-import: undo still works before close", not refused)
        undo(plan7["id"])                   # c3 restored for later tests

        # --- close: zero out an applied import (moves final, no undo) ------
        c8b = os.path.join(drive, "eightb", "Music")
        os.makedirs(os.path.join(c8b, "Duo"))
        open(os.path.join(c8b, "Duo", "Hit2.flac"), "wb").write(b"F2" * 300)
        open(os.path.join(c8b, "Duo", "Hit2.mp3"), "wb").write(b"M2")
        open(os.path.join(c8b, "Duo", "X2.mp3"), "wb").write(b"Y2")
        data = frm.analyze(drive, cfg)
        rundir8b = os.path.join(tmp, "run8b")
        os.makedirs(rundir8b)
        frm.export_run(data, drive, rundir8b)
        with open(os.path.join(rundir8b, "compare.json"), "w") as f:
            json.dump({"per_collection": [{"path": c8b, "new": [
                os.path.join(c8b, "Duo", "Hit2.flac"),
                os.path.join(c8b, "Duo", "Hit2.mp3"),
                os.path.join(c8b, "Duo", "X2.mp3")], "new_files": 3}]}, f)
        target6 = os.path.join(tmp, "local6")
        plan8 = plan_import(rundir8b, c8b, target6, move=True)
        apply_plan(plan8)
        check("move-import on fresh tree: source emptied",
              sum(len(fs) for _b, _d, fs in os.walk(c8b)) == 0)
        plan8 = close(plan8["id"])
        check("close: applied import filed as done",
              plan8["status"] == "closed")
        refused = False
        try:
            undo(plan8["id"])
        except ValueError:
            refused = True
        check("close: no undo after closing (the moves stand)", refused)

        plan5 = plan_quarantine(rundir3, c3)
        apply_plan(plan5)
        refused = False
        try:
            purge(plan5["id"])
        except ValueError:
            refused = True
        check("purge refused: quarantined file has no surviving twin",
              refused)
        check("refused purge left the quarantine intact (nothing deleted)",
              os.path.isfile(os.path.join(plan5["ops"][0]["dst"], "Zulu",
                                          "01 - Only.mp3")))
        undo(plan5["id"])

        # --- delete: unique content refuses, verified-redundant goes --------
        refused = False
        try:
            plan_delete(rundir3, c3)          # all content unique -> refuse
        except ValueError:
            refused = True
        check("delete refused: unique music, no survivors elsewhere", refused)
        check("refused delete left the collection intact",
              os.path.isfile(os.path.join(c3, "Zulu", "01 - Only.mp3")))
        c4 = os.path.join(drive, "dup4", "Music")
        os.makedirs(os.path.join(c4, "Alpha"))
        for n in ("01 - One.mp3", "02 - Two.mp3", "04 - Four.mp3"):
            shutil.copy(os.path.join(a, "Alpha", n),
                        os.path.join(c4, "Alpha", n))
        with open(os.path.join(c4, "Alpha", "cover.jpg"), "wb") as f:
            f.write(b"COVER-C4")
        data = frm.analyze(drive, cfg)
        rundir4 = os.path.join(tmp, "run4")
        os.makedirs(rundir4)
        frm.export_run(data, drive, rundir4)
        plan7 = plan_delete(rundir4, c4)      # twins of A -> safe to delete
        check("delete plan counts music + non-music",
              plan7["counts"]["audio_files"] == 3
              and plan7["counts"]["non_music"] == 1)
        apply_plan(plan7)
        check("applied delete: directory gone",
              not os.path.exists(c4))
        check("applied delete: originals elsewhere untouched",
              os.path.isfile(os.path.join(a, "Alpha", "01 - One.mp3")))
        refused = False
        try:
            undo(plan7["id"])
        except ValueError:
            refused = True
        check("delete has no undo", refused)

        # --- collision within ONE plan: the real-world shape ---------------
        # primary has Song.mp3; the source has Song.mp3 AND a pre-existing
        # 'Song (merged 1).mp3' (leftover from an earlier merge history).
        # Op1 claims the free (merged 1) slot; op2's rename must skip it.
        c5 = os.path.join(drive, "twofive", "Music")
        os.makedirs(os.path.join(c5, "Unknown"))
        os.makedirs(os.path.join(a, "Unknown"))
        open(os.path.join(c5, "Unknown", "Song.mp3"), "wb").write(b"V1" * 50)
        open(os.path.join(c5, "Unknown", "Song (merged 1).mp3"),
             "wb").write(b"V1-OLD-MERGED")
        open(os.path.join(c5, "Unknown", "Third.mp3"), "wb").write(b"T3")
        open(os.path.join(a, "Unknown", "Song.mp3"), "wb").write(b"PRI")
        data = frm.analyze(drive, cfg)
        rundir5 = os.path.join(tmp, "run5")
        os.makedirs(rundir5)
        frm.export_run(data, drive, rundir5)
        plan8 = plan_merge(rundir5, a, c5)
        check("merge-tag file groups with its plain sibling (one variant "
              "swap, one group-worse quarantine)",
              plan8["counts"]["variants_kept"] == 1
              and plan8["counts"]["variants_quarantined"] == 1)
        apply_plan(plan8)
        landed = set(os.listdir(os.path.join(a, "Unknown")))
        check("better version took the CLEAN slot, no merge tags kept",
              landed == {"Song.mp3", "Third.mp3"}
              and open(os.path.join(a, "Unknown", "Song.mp3"),
                       "rb").read() == b"V1" * 50)
        undo(plan8["id"])
        check("undo follows the swapped/renamed destinations",
              os.path.isfile(os.path.join(c5, "Unknown", "Song.mp3"))
              and os.path.isfile(os.path.join(c5, "Unknown",
                                              "Song (merged 1).mp3"))
              and os.path.isfile(os.path.join(a, "Unknown", "Song.mp3")))

        # --- apply-time collision: drive changed between plan and apply -----
        plan9 = plan_merge(rundir5, a, c5)
        # sneak a file into a planned destination after planning — the
        # apply-time safety net must rename around it, never overwrite
        open(os.path.join(a, "Unknown", "Third.mp3"), "wb").write(b"SNEAK")
        apply_plan(plan9)
        check("apply-time collisions renamed instead of erroring",
              plan9["result"]["errors"] == []
              and any("renamed on collision" in n for n in plan9["notes"]))
        keepers_ok = all(os.path.isfile(o["keeper"]) for o in plan9["ops"]
                         if o.get("keeper") and o["op"] == "move"
                         and o["dst"].startswith(quarantine_root())
                         and os.path.isfile(o["dst"]))
        check("keepers point at files that really exist after apply",
              keepers_ok)
        undo(plan9["id"])
        sneak_ok = open(os.path.join(a, "Unknown", "Third.mp3"),
                        "rb").read() == b"SNEAK"
        check("undo does not touch the pre-existing sneaky file", sneak_ok)

        # --- variants: keep best per song (one copy per song) ---------------
        c6 = os.path.join(drive, "sixsix", "Music")
        os.makedirs(os.path.join(c6, "Artist"))
        # spelling-variant artist dir: Linux (case-sensitive fs) gets a
        # sibling dir that normalizes to the same song key; Windows
        # (case-insensitive fs) cannot have both - the variant lives in
        # Artist there, same grouping either way
        win = sys.platform == "win32"
        artdir = os.path.join(c6, "Artist" if win else "artIst")
        os.makedirs(artdir, exist_ok=True)   # on win32 artdir == Artist
        os.makedirs(os.path.join(c6, "Duo"))
        os.makedirs(os.path.join(c6, "Solo"))
        open(os.path.join(c6, "Artist", "Song.flac"), "wb").write(b"FL" * 500)
        open(os.path.join(c6, "Artist", "Song.mp3"), "wb").write(b"MP3")
        open(os.path.join(artdir, "Song.ogg"), "wb").write(b"OGG")
        open(os.path.join(c6, "Duo", "Twin.mp3"), "wb").write(b"TT")
        open(os.path.join(c6, "Duo", "Twin (1).mp3"), "wb").write(b"TT")
        open(os.path.join(c6, "Solo", "Only.mp3"), "wb").write(b"ONLY")
        open(os.path.join(c6, "Artist", "cover.jpg"), "wb").write(b"ART6")
        data = frm.analyze(drive, cfg)
        rundir6 = os.path.join(tmp, "run6")
        os.makedirs(rundir6)
        frm.export_run(data, drive, rundir6)
        planV = plan_variants(rundir6, c6)
        check("variants: 2 multi-version songs found",
              planV["counts"]["songs"] == 2)
        check("variants: 3 lesser versions quarantined "
              "(flac kept; twin pair collapsed)",
              planV["counts"]["quarantined"] == 3)
        check("variants: keeper recorded on every op",
              all(o.get("keeper") for o in planV["ops"]
                  if o["op"] == "move"))
        apply_plan(planV)
        check("variants applied: flac kept, art untouched, solo untouched",
              os.path.isfile(os.path.join(c6, "Artist", "Song.flac"))
              and os.path.isfile(os.path.join(c6, "Artist", "cover.jpg"))
              and os.path.isfile(os.path.join(c6, "Solo", "Only.mp3")))
        twins_left = [f for f in os.listdir(os.path.join(c6, "Duo"))
                      if f.endswith(".mp3")]
        check("variants applied: one twin survives, lesser versions gone",
              not os.path.exists(os.path.join(c6, "Artist", "Song.mp3"))
              and not os.path.exists(os.path.join(artdir, "Song.ogg"))
              and twins_left == ["Twin (1).mp3"])
        pv = purge(planV["id"])               # keepers exist -> allowed
        check("variants purge: verified-by-keeper deletion worked",
              pv["result"].get("purged_files") == 3)

        # keeper gone -> purge refuses, and undo still works
        c8 = os.path.join(drive, "eight", "Music")
        os.makedirs(os.path.join(c8, "Duo"))
        open(os.path.join(c8, "Duo", "Hit.flac"), "wb").write(b"FL8" * 400)
        open(os.path.join(c8, "Duo", "Hit.mp3"), "wb").write(b"M8")
        open(os.path.join(c8, "Duo", "Extra8.mp3"), "wb").write(b"E8")
        data = frm.analyze(drive, cfg)
        rundir8 = os.path.join(tmp, "run8")
        os.makedirs(rundir8)
        frm.export_run(data, drive, rundir8)
        planV2 = plan_variants(rundir8, c8)
        apply_plan(planV2)
        keeper_path = planV2["ops"][0]["keeper"]
        os.remove(keeper_path)                # keeper vanishes -> refuse
        refused = False
        try:
            purge(planV2["id"])
        except ValueError:
            refused = True
        check("variants purge refused when the keeper is gone", refused)
        undo(planV2["id"])
        check("variants undo still restores after a refused purge",
              os.path.isfile(os.path.join(c8, "Duo", "Hit.mp3")))

        # --- variant-aware merge: better swaps in, worse quarantines --------
        c7 = os.path.join(drive, "seven", "Music")
        os.makedirs(os.path.join(c7, "Alpha"))
        open(os.path.join(c7, "Alpha", "Voice.flac"), "wb").write(b"FLAC" * 400)
        open(os.path.join(c7, "Alpha", "Voice.ogg"), "wb").write(b"OGGV")
        open(os.path.join(c7, "Alpha", "Extra7.mp3"), "wb").write(b"EXTRA7")
        open(os.path.join(a, "Alpha", "Voice.mp3"), "wb").write(b"PRIV")
        data = frm.analyze(drive, cfg)
        rundir7 = os.path.join(tmp, "run7")
        os.makedirs(rundir7)
        frm.export_run(data, drive, rundir7)
        plan10 = plan_merge(rundir7, a, c7)
        check("merge variants: 1 swapped in, 1 lesser quarantined",
              plan10["counts"]["variants_kept"] == 1
              and plan10["counts"]["variants_quarantined"] == 1)
        apply_plan(plan10)
        check("merge applied: flac won the slot in the primary",
              os.path.isfile(os.path.join(a, "Alpha", "Voice.flac")))
        check("merge applied: primary's mp3 parked in quarantine",
              any("Voice.mp3" in f for _r, _d, fs in
                  os.walk(quarantine_root()) for f in fs))
        check("merge applied: incoming ogg never reached the primary",
              not os.path.exists(os.path.join(a, "Alpha", "Voice.ogg")))
        undo(plan10["id"])
        check("merge undo: mp3 back in primary, flac back in copy",
              os.path.isfile(os.path.join(a, "Alpha", "Voice.mp3"))
              and os.path.isfile(os.path.join(c7, "Alpha", "Voice.flac")))
        # incoming loses: same format, smaller file -> primary untouched
        os.remove(os.path.join(c7, "Alpha", "Voice.flac"))
        os.remove(os.path.join(c7, "Alpha", "Voice.ogg"))
        open(os.path.join(c7, "Alpha", "Voice.mp3"), "wb").write(b"V")
        plan11 = plan_merge(rundir7, a, c7)
        check("merge variants: incoming loses, primary keeps its file",
              plan11["counts"]["variants_kept"] == 0
              and plan11["counts"]["variants_quarantined"] == 1)
        apply_plan(plan11)
        check("merge applied: primary's bigger mp3 untouched",
              open(os.path.join(a, "Alpha", "Voice.mp3"), "rb").read()
              == b"PRIV")
        undo(plan11["id"])

        check("actions.log exists and has entries",
              os.path.isfile(log_path())
              and sum(1 for _ in open(log_path(), encoding="utf-8")) >= 8)
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
