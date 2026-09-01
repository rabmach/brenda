#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""brenda compare — what's on a scanned drive that you already have (or don't) locally?

Takes a frm/brenda scan run (report.json + hashes.tsv + files-*.txt) and diffs
it against a persistent index of the LOCAL machine's music.

Matching is two-tier, reusing frm's own machinery:
  exact     same MD5 as a local file (byte-identical copy)
  variant   same normalized artist+track key, different bytes/format
            (FLAC-vs-MP3 twins, re-encodes)
  new       neither — a genuine import candidate

The local index lives in <data home>/index/local.json and caches MD5s by
(path, size, mtime): unchanged local files are never re-read after the first
build. Only audio files inside Music directories are ever opened.

Outputs compare.{html,md,json} written INTO the run directory, next to the
report. Read-only: nothing on the drive or in the local collections is
touched.

Requirements: Python 3.8+ (standard library only). No root, no pip installs.
"""

import argparse
import datetime
import json
import os
import sys

import frm


# --------------------------------------------------------------------------
# local index
# --------------------------------------------------------------------------

def index_path(data_home=None):
    dh = data_home or frm.data_home()
    return os.path.join(dh, "index", "local.json")


def _load_index(path):
    if not os.path.isfile(path):
        return {"version": 2, "built": None, "roots": {}}
    with open(path) as f:
        idx = json.load(f)
    if idx.get("version") == 1:
        # v1 (briefly shipped): flat files dict + root summaries. Partition
        # the files into per-root sections; anything that matches no known
        # root is dropped (it re-hashes on the next refresh of a covering
        # root — cheap, and only ever applied to tiny early indexes).
        sections = {}
        known = sorted(idx.get("roots", {}), key=len, reverse=True)
        for fp, rec in idx.get("files", {}).items():
            for r in known:
                if fp == r or fp.startswith(r + os.sep):
                    sections.setdefault(r, {"files": {}})
                    sections[r]["files"][fp] = rec
                    break
        idx = {"version": 2, "built": idx.get("built"), "roots": sections}
    idx.setdefault("version", 2)
    idx.setdefault("roots", {})
    return idx


def _save_index(path, idx):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(idx, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def all_files(index):
    """Yield (path, rec) across every indexed root, in stored root order.
    Sections are keyed by realpath; insertion order = build order, which is
    the priority order dig uses (earlier roots win ties)."""
    for _root, sec in index.get("roots", {}).items():
        yield from sec.get("files", {}).items()


def index_count(index):
    return sum(len(s.get("files", {})) for s in index.get("roots", {}).values())


def _audio_under_music_dirs(root):
    """All audio files inside every Music dir found under root (frm's
    discovery rules, so dev/projects trees are pruned the same way)."""
    files = []
    music_dirs, _errs = frm.discover_music_dirs(root)
    if root.rstrip(os.sep).lower().endswith(os.sep + "music"):
        music_dirs.append(root.rstrip(os.sep))
    for md in sorted(set(music_dirs)):
        for base, _dirs, names in os.walk(md, followlinks=False):
            for n in names:
                if n.lower().rsplit(".", 1)[-1] in frm.AUDIO_EXT:
                    files.append(os.path.join(base, n))
    return files


def song_key(fp):
    """Normalized (artist-folder, track) key — frm's exact heuristic."""
    artist = os.path.basename(os.path.dirname(fp))
    return frm.norm_name(artist) + "|" + frm.norm_name(os.path.basename(fp))


def build_index(roots, index_file, workers, quiet=False, force=False):
    """Walk roots, reuse cached MD5s for unchanged (size, mtime) files,
    hash the rest. The index is per-root (schema v2): sections for roots not
    in this build are carried forward untouched, so switching --local choices
    never triggers re-hash storms. Files deleted from a root are pruned when
    that root is next walked. Returns (index, stats_dict)."""
    idx = _load_index(index_file)
    sections = idx["roots"]

    todo = []             # (root, fp, size, mtime)
    keep = {}             # root -> {fp: rec}  (rebuilt per walked root)
    cache_hits = 0
    ordered_roots = []
    for root in roots:
        root = os.path.realpath(root)
        if root in ordered_roots:
            continue
        ordered_roots.append(root)
        old_files = sections.get(root, {}).get("files", {})
        sec_keep = {}
        seen = set()
        for fp in _audio_under_music_dirs(root):
            if fp in seen:
                continue
            seen.add(fp)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if not force:
                cached = old_files.get(fp)
                if cached and cached.get("size") == st.st_size \
                        and cached.get("mtime") == st.st_mtime \
                        and cached.get("md5"):
                    sec_keep[fp] = cached
                    cache_hits += 1
                    continue
            todo.append((root, fp, st.st_size, st.st_mtime))
        keep[root] = sec_keep

    if not quiet:
        print(f"index: {cache_hits} unchanged (cache hit), "
              f"{len(todo)} to hash", file=sys.stderr)
    hashes, errors = frm.hash_audio_files([t[1] for t in todo], workers, quiet)
    hashed = 0
    for root, fp, size, mtime in todo:
        h = hashes.get(fp)
        if h:
            hashed += 1
            keep[root][fp] = {"size": size, "mtime": mtime, "md5": h,
                              "key": song_key(fp)}
        else:
            if not quiet and fp in errors:
                print(f"  index: unreadable {fp}: {errors[fp]}", file=sys.stderr)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # stable section order: prior order preserved (walked roots updated in
    # place, untouched sections carried forward), new roots append at the end
    new_sections = {}
    for r, s in sections.items():
        new_sections[r] = {"built": now, "files": keep[r]} if r in keep else s
    for root in ordered_roots:
        if root not in new_sections:
            new_sections[root] = {"built": now, "files": keep[root]}
    idx = {"version": 2, "built": now, "roots": new_sections}
    _save_index(index_file, idx)
    stats = {"cache_hits": cache_hits,
             "hashed": hashed,
             "hash_errors": len(errors),
             "files": index_count(idx)}
    return idx, stats


# --------------------------------------------------------------------------
# run loading
# --------------------------------------------------------------------------

COMPARE_KINDS = ("collection", "android", "ipod")


def load_run(rundir):
    """Load a scan run: collections with their audio file lists + md5s."""
    rp = os.path.join(rundir, "report.json")
    if not os.path.isfile(rp):
        raise SystemExit(f"not a brenda/frm run (no report.json): {rundir}")
    with open(rp) as f:
        report = json.load(f)
    root = report["meta"]["root"]

    colls = []
    for d in report["dirs"]:
        if d.get("kind") not in COMPARE_KINDS:
            continue
        rel = frm.dir_short(d["path"], root).replace(os.sep, "__")
        fl = os.path.join(rundir, f"files-{rel}.txt")
        if os.path.isfile(fl):
            with open(fl) as f2:
                files = [x for x in f2.read().splitlines() if x]
        else:               # fresh run export always writes these; be safe
            files = d.get("audio_files_list", [])
        colls.append({"path": d["path"], "kind": d["kind"],
                      "short": frm.dir_short(d["path"], root), "files": files})

    hashes = {}
    hp = os.path.join(rundir, "hashes.tsv")
    have_hashes = os.path.isfile(hp)
    if have_hashes:
        with open(hp) as f:
            for line in f:
                if "\t" in line:
                    h, p = line.rstrip("\n").split("\t", 1)
                    hashes[p] = h
    return {"dir": rundir, "root": root, "collections": colls,
            "hashes": hashes, "hashed": have_hashes}


# --------------------------------------------------------------------------
# the diff
# --------------------------------------------------------------------------

def compare_run(run, index):
    local_md5 = {}
    local_keys = {}
    for fp, rec in all_files(index):
        h = rec.get("md5")
        if h:
            local_md5.setdefault(h, []).append(fp)
        if rec.get("key"):
            local_keys.setdefault(rec["key"], []).append(fp)

    per_coll = []
    totals = {"exact_files": 0, "exact_bytes": 0, "variant_files": 0,
              "new_files": 0, "new_bytes": 0, "run_files": 0}
    new_master = []
    for coll in run["collections"]:
        entry = {"path": coll["path"], "short": coll["short"],
                 "kind": coll["kind"], "run_files": 0,
                 "exact_files": 0, "exact_bytes": 0,
                 "variant_files": 0, "variant": [],
                 "new_files": 0, "new_bytes": 0, "new": []}
        for fp in coll["files"]:
            entry["run_files"] += 1
            h = run["hashes"].get(fp)
            key = song_key(fp)
            if h and h in local_md5:
                entry["exact_files"] += 1
                try:
                    entry["exact_bytes"] += os.lstat(fp).st_size
                except OSError:
                    pass
            elif key in local_keys:
                entry["variant_files"] += 1
                if len(entry["variant"]) < 400:
                    entry["variant"].append(fp)
            else:
                entry["new_files"] += 1
                try:
                    entry["new_bytes"] += os.lstat(fp).st_size
                except OSError:
                    entry["new_bytes"] = None   # drive likely unmounted
                if len(entry["new"]) < 400:
                    entry["new"].append(fp)
                new_master.append(fp)
        per_coll.append(entry)
        for k in ("run_files", "exact_files", "variant_files", "new_files"):
            totals[k] += entry[k]
        totals["exact_bytes"] += entry["exact_bytes"]
        if entry["new_bytes"] is not None:
            totals["new_bytes"] += entry["new_bytes"]
        else:
            entry["new_bytes"] = 0
    return {"per_collection": per_coll, "totals": totals, "new_master": new_master}


# --------------------------------------------------------------------------
# rendering (reuses frm's CSS + helpers, so the pages look identical)
# --------------------------------------------------------------------------

def render_json(results, run, index, outpath):
    out = {
        "meta": {"tool": "brenda", "run": run["dir"], "run_root": run["root"],
                 "run_hashed": run["hashed"],
                 "index_built": index.get("built"),
                 "index_files": index_count(index),
                 "index_roots": sorted(index["roots"]),
                 "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "version": frm.VERSION},
        "totals": results["totals"],
        "per_collection": results["per_collection"],
    }
    with open(outpath, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    return outpath


def render_html(results, run, index, outpath):
    import html as _html
    t = results["totals"]
    kpis = [
        (f"{t['new_files']:,}", "new to you (import candidates)"),
        (f"{t['exact_files']:,}", "already have (byte-identical)"),
        (f"{t['variant_files']:,}", "have as variant (diff. format/encode)"),
        (f"{t['run_files']:,}", "audio files on the drive"),
    ]

    rows = ""
    for e in results["per_collection"]:
        pct = (100.0 * e["new_files"] / e["run_files"]) if e["run_files"] else 0
        rows += (f'<tr><td><code>{_html.escape(e["short"])}</code></td>'
                 f'<td class="num">{e["run_files"]:,}</td>'
                 f'<td class="num">{e["exact_files"]:,}</td>'
                 f'<td class="num">{e["variant_files"]:,}</td>'
                 f'<td class="num"><b>{e["new_files"]:,}</b> '
                 f'<span class="dim">({pct:.0f}%)</span></td>'
                 f'<td class="num">{frm.fmt_bytes(e["new_bytes"])}</td></tr>')

    def file_table(paths, limit_note=""):
        if not paths:
            return "<p class='sub'>none</p>"
        items = "".join(f"<li><code>{_html.escape(p)}</code></li>" for p in paths)
        more = (f"<li class='dim'>… and {limit_note} more</li>" if limit_note else "")
        return f"<ul style='padding-left:18px'>{items}{more}</ul>"

    new_html = ""
    for e in results["per_collection"]:
        if not e["new"]:
            continue
        shown = len(e["new"])
        note = str(e["new_files"] - shown) if e["new_files"] > shown else ""
        new_html += (f"<h3>{_html.escape(e['short'])} — "
                     f"{e['new_files']:,} new</h3>" + file_table(e["new"], note))

    variant_html = ""
    for e in results["per_collection"]:
        if not e["variant"]:
            continue
        shown = len(e["variant"])
        note = str(e["variant_files"] - shown) if e["variant_files"] > shown else ""
        variant_html += (f"<h3>{_html.escape(e['short'])} — "
                         f"{e['variant_files']:,} variants</h3>" +
                         file_table(e["variant"], note))

    warn = ""
    if not run["hashed"]:
        warn = ('<div class="warn"><b>This run was made with --no-dedup, so no '
                'MD5s exist.</b> Exact (byte-identical) matching was impossible; '
                'everything here is song-key based. Re-scan without --no-dedup '
                'for the full two-tier match.</div>')

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compare — {frm.esc(run["root"])} vs local music</title>
<style>{frm.CSS}</style></head><body><div class="wrap">
<h1>Compare: <code>{frm.esc(run["root"])}</code> vs your local music</h1>
<p class="sub">Run: {frm.esc(run["dir"])} &nbsp;·&nbsp; local index:
{index_count(index):,} files from {", ".join(frm.esc(r) for r in sorted(index["roots"])) or "nowhere"}
&nbsp;·&nbsp; <a href="report.html">back to the full report</a></p>
{warn}
<div class="cards">
{''.join(f'<div class="card"><div class="kpi">{v}<small>{frm.esc(l)}</small></div></div>' for v, l in kpis)}
</div>
<h2>Per collection</h2>
<table><tr><th>Collection</th><th class="num">audio</th>
<th class="num">exact</th><th class="num">variant</th>
<th class="num">new</th><th class="num">new size</th></tr>
{rows}
</table>
<h2>New to you</h2>
<p class="sub">No byte match and no normalized artist+track match in your
local index. These are the import candidates (v1.1 adds report buttons that
copy them into a directory of your choice).</p>
{new_html or "<p class='sub'>nothing — you already have everything here, at least as a variant</p>"}
<h2>Have as variant</h2>
<p class="sub">Same normalized artist+track, different bytes — re-encodes,
different format, or re-rips. Judge these by ear/eye before deleting anything.</p>
{variant_html or "<p class='sub'>none</p>"}
<h2>Notes on method</h2>
<ul class="sub" style="padding-left:18px">
<li>Local index: MD5 cached by (path, size, mtime) — unchanged local files are
never re-read.</li>
<li>"Exact" = identical MD5. "Variant" = identical normalized
artist-folder + track-name key (frm's heuristic, format-blind).</li>
<li>Only audio files inside Music directories are compared — the same rule
the scan uses.</li>
<li>Read-only: this page never modified anything.</li>
</ul>
<footer>brenda compare (frm {frm.esc(frm.VERSION)}) — JSON + Markdown next to this file.</footer>
</div></body></html>"""
    with open(outpath, "w") as f:
        f.write(doc)
    return outpath


def render_markdown(results, run, index, outpath):
    t = results["totals"]
    L = [f"# Compare — `{run['root']}` vs local music", ""]
    L.append(f"Local index: {index_count(index):,} files "
             f"({', '.join(sorted(index['roots'])) or 'nowhere'}). "
             f"Run hash-verified: {run['hashed']}.")
    L.append("")
    L.append(f"- New to you: **{t['new_files']:,}** "
             f"({frm.fmt_bytes(t['new_bytes'])})")
    L.append(f"- Already have (byte-identical): **{t['exact_files']:,}**")
    L.append(f"- Have as variant (diff. format/encode): **{t['variant_files']:,}**")
    L.append("")
    L.append("## Per collection")
    L.append("")
    rows = [[f"`{e['short']}`", e["run_files"], e["exact_files"],
             e["variant_files"], e["new_files"]]
            for e in results["per_collection"]]
    L.append(frm.md_table(["Collection", "audio", "exact", "variant", "new"], rows))
    L.append("")
    L.append("## New to you")
    L.append("")
    for e in results["per_collection"]:
        if not e["new"]:
            continue
        L.append(f"### {e['short']} — {e['new_files']:,} new")
        L.append("")
        for p in e["new"][:100]:
            L.append(f"- `{p}`")
        if e["new_files"] > len(e["new"]):
            L.append(f"- … and {e['new_files'] - len(e['new'])} more (see JSON)")
        L.append("")
    L.append("---")
    L.append("*Read-only comparison. Nothing was modified.*")
    with open(outpath, "w") as f:
        f.write("\n".join(L) + "\n")
    return outpath


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run_cli(argv=None):
    ap = argparse.ArgumentParser(
        prog="brenda compare",
        description="Diff a brenda/frm scan run against your local music. "
                    "Read-only.")
    ap.add_argument("run", nargs="?",
                    help="run directory (default: the 'latest' symlink)")
    ap.add_argument("--local", action="append", default=None,
                    metavar="ROOT",
                    help="local root(s) to index (default: your home dir); "
                         "repeatable")
    ap.add_argument("--refresh", action="store_true",
                    help="re-hash all indexed files, ignoring the cache")
    ap.add_argument("--workers", type=int, default=min(os.cpu_count() or 2, 8))
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the HTML result")
    args = ap.parse_args(argv)

    dh = frm.data_home()
    run_dir = args.run or os.path.join(dh, "latest")
    run_dir = os.path.realpath(run_dir)
    if not os.path.isdir(run_dir):
        raise SystemExit(f"no run at {run_dir} — scan something first "
                         f"(brenda scan <drive>)")

    roots = args.local or [os.path.expanduser("~")]
    for r in roots:
        if not os.path.isdir(r):
            raise SystemExit(f"not a directory: {r}")

    idx_file = index_path(dh)
    index, stats = build_index(roots, idx_file, args.workers,
                               quiet=False, force=args.refresh)
    print(f"index: {stats['files']:,} local files "
          f"({stats['cache_hits']:,} cached, {stats['hashed']:,} hashed)",
          file=sys.stderr)

    run = load_run(run_dir)
    results = compare_run(run, index)
    t = results["totals"]
    print(f"compare: {t['run_files']:,} on drive -> "
          f"{t['new_files']:,} new / {t['exact_files']:,} exact / "
          f"{t['variant_files']:,} variant", file=sys.stderr)

    out = render_json(results, run, index, os.path.join(run_dir, "compare.json"))
    render_markdown(results, run, index, os.path.join(run_dir, "compare.md"))
    html_path = render_html(results, run, index, os.path.join(run_dir, "compare.html"))
    print(f"Output: {out}", file=sys.stderr)
    print(f"  HTML:  {html_path}", file=sys.stderr)
    if not args.no_open:
        os.system("xdg-open " + frm.shlex_quote(html_path))
    return 0


def self_test():
    """Self-test for compare: fake local tree + fake run, two-tier match."""
    import tempfile
    import shutil
    import hashlib
    print("brenda compare self-test")
    tmp = tempfile.mkdtemp(prefix="brenda-compare-test-")
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'ok  ' if cond else 'FAIL'}] {name}")
        if not cond:
            ok = False

    try:
        # local tree: ~/Music with two artists
        home = os.path.join(tmp, "home")
        loc = os.path.join(home, "Music")
        os.makedirs(os.path.join(loc, "Alpha"))
        content_a = b"LOCAL-FLAC-CONTENT-A"
        content_b = b"LOCAL-MP3-CONTENT-B"
        with open(os.path.join(loc, "Alpha", "01 - Track One.flac"), "wb") as f:
            f.write(content_a)
        with open(os.path.join(loc, "Alpha", "02 - Track Two.mp3"), "wb") as f:
            f.write(content_b)
        with open(os.path.join(loc, "Alpha", "03 - Local Only.mp3"), "wb") as f:
            f.write(b"LOCAL-UNIQUE-CONTENT")

        # fake run: same Track One (exact), Track Two as different encode
        # (variant), plus two genuinely new tracks
        rundir = os.path.join(tmp, "run", "runs", "x-homishdump")
        os.makedirs(rundir)
        drive = os.path.join(tmp, "drive")
        dmd = os.path.join(drive, "backup", "Music", "Alpha")
        os.makedirs(dmd)
        with open(os.path.join(dmd, "01 - Track One.flac"), "wb") as f:
            f.write(content_a)                       # exact match
        with open(os.path.join(dmd, "02 - Track Two.mp3"), "wb") as f:
            f.write(b"DRIVE-DIFFERENT-ENCODE-B")     # variant
        with open(os.path.join(dmd, "04 - Drive New.flac"), "wb") as f:
            f.write(b"DRIVE-NEW-CONTENT-1")          # new
        nested = os.path.join(drive, "backup", "Music", "Beta")
        os.makedirs(nested)
        with open(os.path.join(nested, "05 - Drive New 2.mp3"), "wb") as f:
            f.write(b"DRIVE-NEW-CONTENT-2")          # new

        # write run files the way frm.main() exports them
        mda = hashlib.md5(content_a).hexdigest()
        report = {"meta": {"root": drive},
                  "dirs": [
                      {"path": os.path.join(drive, "backup", "Music"),
                       "kind": "collection", "audio_files": 4}]}
        with open(os.path.join(rundir, "report.json"), "w") as f:
            json.dump(report, f)
        hashes = {}
        for base, _d, names in os.walk(os.path.join(drive, "backup", "Music")):
            for n in names:
                p = os.path.join(base, n)
                with open(p, "rb") as f:
                    hashes[p] = hashlib.md5(f.read()).hexdigest()
        with open(os.path.join(rundir, "hashes.tsv"), "w") as f:
            for p, h in sorted(hashes.items()):
                f.write(f"{h}\t{p}\n")
        short = frm.dir_short(os.path.join(drive, "backup", "Music"),
                              drive).replace(os.sep, "__")
        with open(os.path.join(rundir, f"files-{short}.txt"), "w") as f:
            f.write("\n".join(sorted(hashes)))

        idx_file = os.path.join(tmp, "data", "index", "local.json")
        index, stats = build_index([home], idx_file, workers=2, quiet=True)
        check("index has 3 local files", stats["files"] == 3)
        check("index hashed 3", stats["hashed"] == 3)

        run = load_run(rundir)
        check("run loaded 1 collection", len(run["collections"]) == 1)
        check("run has 4 hashed files", len(run["hashes"]) == 4)

        results = compare_run(run, index)
        t = results["totals"]
        check(f"exact == 1 (got {t['exact_files']})", t["exact_files"] == 1)
        check(f"variant == 1 (got {t['variant_files']})", t["variant_files"] == 1)
        check(f"new == 2 (got {t['new_files']})", t["new_files"] == 2)
        check("run_files == 4", t["run_files"] == 4)

        # cache hit on rebuild
        index2, stats2 = build_index([home], idx_file, workers=2, quiet=True)
        check("rebuild hits cache", stats2["cache_hits"] == 3
              and stats2["hashed"] == 0)

        # --- schema v2: second root joins; untouched sections survive ------
        home2 = os.path.join(tmp, "home2")
        loc2 = os.path.join(home2, "Music")
        os.makedirs(loc2)
        with open(os.path.join(loc2, "09 - Second Root.flac"), "wb") as f:
            f.write(b"LOCAL-SECOND-ROOT")
        index3, stats3 = build_index([home, home2], idx_file, 2, quiet=True)
        check("two roots indexed (4 files)", stats3["files"] == 4
              and stats3["hashed"] == 1)
        # rebuild ONLY the first root: second's section survives, no re-hash
        index4, stats4 = build_index([home], idx_file, 2, quiet=True)
        check("untouched root survives rebuild",
              stats4["files"] == 4 and stats4["hashed"] == 0
              and stats4["cache_hits"] == 3)
        check("section order preserved (home first)",
              list(index4["roots"]) == [home, home2])
        # delete a local file -> pruned on that root's next walk
        os.remove(os.path.join(loc, "Alpha", "03 - Local Only.mp3"))
        index5, stats5 = build_index([home], idx_file, 2, quiet=True)
        check("deleted file pruned", stats5["files"] == 3)

        # renders don't blow up
        rj = render_json(results, run, index, os.path.join(rundir, "compare.json"))
        rm = render_markdown(results, run, index, os.path.join(rundir, "compare.md"))
        rh = render_html(results, run, index, os.path.join(rundir, "compare.html"))
        for p in (rj, rm, rh):
            check(f"render {os.path.basename(p)}",
                  os.path.getsize(p) > 200)
        with open(rh) as f:
            check("html mentions exact/variant/new",
                  "already have" in f.read())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_cli())
