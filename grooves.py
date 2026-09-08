#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""brenda dig — build a genre + BPM filtered playlist from a brenda/frm scan.

Inputs (all produced by a scan run):
  <data home>/latest/report.json   the real collections (mirrors excluded)
  <data home>/latest/hashes.tsv    md5 per hashed audio file (dedup)

Pipeline:
  1. refresh the local index (--local roots, default: home) and collect
     genre-matched local tracks — local copies get priority
  2. walk each real collection on the drive, same genre filter
  3. dedup: byte-identical -> the local path wins (playlist survives the
     drive being unplugged); same song different bytes -> the better
     format wins (FLAC twin replaces a local MP3)
  4. BPM from the md5-keyed cache; compute the misses with aubio
  5. keep tracks inside [--bpm-min, --bpm-max], cap at --max in
     candidate order (local first), write an .m3u named
     <genres>_<bpm-range>_<total-duration>_<timestamp>.m3u

BPM results are cached in <data home>/bpm_cache.tsv keyed by MD5, not path —
so moving, renaming or merging files never invalidates the cache. A one-time
migrator (`brenda migrate-cache`) converts the old path-keyed groovin cache
by joining it against the hashes.tsv files of existing runs.

Requirements: python3-mutagen + python3-aubio (+ ffmpeg backend) — installed
by `install.sh --extras`. Everything else in brenda runs without them.
"""

import argparse
import datetime
import hashlib
import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor

AUDIO_EXT = {
    "mp3", "flac", "ogg", "oga", "opus", "m4a", "m4b", "m4p", "aac",
    "wav", "wv", "wma", "ape", "mpc", "aiff", "aif", "au", "ra", "rm",
    "ac3", "dts", "amr", "mid", "midi",
}


def _deps():
    """mutagen is needed unconditionally (tag reading). aubio is needed only
    for BPM cache misses — checked lazily in detect_bpm."""
    try:
        import mutagen  # noqa: F401
    except ImportError:
        raise SystemExit(
            "brenda dig needs: python3-mutagen"
            "\nDebian:   sudo apt install python3-mutagen"
            "\nFedora:   sudo dnf install python3-mutagen"
            "\nArch:     sudo pacman -S python-mutagen")


# --------------------------------------------------------------------------
# tags (all mutagen use is lazy — import happens in _deps)
# --------------------------------------------------------------------------

def tag_probe(path):
    """One mutagen pass: (genres, dedup_key). The key is tag-based
    (artist|title) with a filename-based fallback (frm's normalization,
    namespaced 'f|') so untagged drive dumps still dedup. Returns
    key=None only when even the filename is empty after normalization."""
    import mutagen
    try:
        m = mutagen.File(path, easy=True)
    except Exception:
        m = None
    if m is None:
        return [], None
    g = [x.strip().lower() for x in (m.get("genre") or []) if x and x.strip()]
    t = m.tags or {}
    try:
        a = (t.get("artist") or [""])[0].strip().lower()
        ti = (t.get("title") or [""])[0].strip().lower()
    except Exception:
        a = ti = ""
    if a and ti:
        return g, "t|" + a + "|" + ti
    import frm
    artist = frm.norm_name(os.path.basename(os.path.dirname(path)))
    track = frm.norm_name(os.path.basename(path))
    if not artist and not track:
        return g, None
    return g, "f|" + artist + "|" + track


def track_duration(path):
    import mutagen
    try:
        m = mutagen.File(path)
        if m is not None:
            return float(getattr(m.info, "length", 0.0))
    except Exception:
        pass
    return 0.0


def total_duration(paths):
    return sum(track_duration(p) for p in paths)


def fmt_duration(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def fmt_bpm_range(lo, hi):
    def num(x):
        return str(int(x)) if float(x).is_integer() else str(x)
    return f"{num(lo)}-{num(hi)}"


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def format_rank(path):
    """Quality proxy for song-key ties: lossless beats lossy so a drive FLAC
    can replace an already-listed local MP3 twin (and vice versa when the
    local copy is the better one)."""
    e = os.path.splitext(path)[1][1:].lower()
    return {"flac": 5, "wav": 5, "aiff": 5, "aif": 5, "wv": 5,
            "opus": 4, "ogg": 4, "oga": 4, "m4a": 3, "aac": 3,
            "mp3": 2}.get(e, 1)


def dedup_candidates(entries):
    """entries: [(path, md5, key, genres)] in priority order (local roots
    first, then drive collections). Returns the kept list, same shape.

    Rules:
      - same md5 (byte-identical): the earlier entry wins — priority is
        local-first, so playlists reference stable local paths
      - same key (same song, different bytes): the higher format_rank wins;
        tie goes to the earlier entry
      - md5=None entries (unreadable) never collide on bytes
    """
    kept = []
    by_md5 = {}
    by_key = {}
    for path, md5, key, genres in entries:
        if md5 and md5 in by_md5:
            continue
        if key is not None and key in by_key:
            prev_path, prev_md5 = by_key[key]
            if format_rank(path) > format_rank(prev_path):
                kept = [k for k in kept if k[0] != prev_path]
                if prev_md5:
                    by_md5.pop(prev_md5, None)
                by_key[key] = (path, md5)
                if md5:
                    by_md5[md5] = path
                kept.append((path, md5, key, genres))
            continue
        if md5:
            by_md5[md5] = path
        if key is not None:
            by_key[key] = (path, md5)
        kept.append((path, md5, key, genres))
    return kept


def display(fp):
    """Path for human reading — home prefix shortened, nothing personal."""
    home = os.path.expanduser("~")
    return "~" + fp[len(home):] if fp.startswith(home + os.sep) else fp


# --------------------------------------------------------------------------
# bpm
# --------------------------------------------------------------------------

def _silence_fd2():
    devnull = os.open(os.devnull, os.O_RDWR)
    saved = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    return saved


def detect_bpm(path):
    try:
        import aubio
    except ImportError:
        raise SystemExit(
            f"brenda dig needs python3-aubio to compute BPM for tracks not "
            f"yet in the cache (e.g. {display(path)})"
            "\nDebian:   sudo apt install python3-aubio ffmpeg"
            "\nFedora:   sudo dnf install python3-aubio ffmpeg"
            "\nArch:     sudo pacman -S python-aubio ffmpeg")
    saved = _silence_fd2()
    try:
        hop = 512
        src = aubio.source(path, 0, hop)
        sr = src.samplerate
        tempo = aubio.tempo("default", 1024, hop, sr)
        bpm = 0.0
        while True:
            samples, read = src()
            if tempo(samples):
                bpm = tempo.get_bpm()
            if read < hop:
                break
        return path, round(bpm, 1) if bpm > 0 else None
    except Exception:
        return path, None
    finally:
        os.dup2(saved, 2)
        os.close(saved)


# --------------------------------------------------------------------------
# md5-keyed bpm cache
# --------------------------------------------------------------------------

def load_cache(path):
    cache = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) == 2:
                    try:
                        cache[parts[0]] = float(parts[1])
                    except ValueError:
                        cache[parts[0]] = None
    return cache


def save_cache(path, cache):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for h in sorted(cache):
            f.write(f"{h}\t{cache[h]}\n")
    os.replace(tmp, path)


def migrate_path_cache(old_cache_path, run_dirs, new_cache_path):
    """Convert a path-keyed bpm cache (old groovin format) to md5-keyed,
    joining paths against the hashes.tsv of the given run dirs. Returns
    (migrated, dropped_unmatched, dropped_failed, collisions)."""
    path_md5 = {}
    for rd in run_dirs:
        hp = os.path.join(rd, "hashes.tsv")
        if not os.path.isfile(hp):
            continue
        with open(hp, encoding="utf-8") as f:
            for line in f:
                if "\t" not in line:
                    continue
                h, p = line.rstrip("\r\n").split("\t", 1)
                path_md5[p] = h

    old = {}
    if os.path.isfile(old_cache_path):
        with open(old_cache_path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\r\n").split("\t")
                if len(parts) == 2:
                    try:
                        old[parts[0]] = float(parts[1])
                    except ValueError:
                        old[parts[0]] = None   # recorded failure

    new = load_cache(new_cache_path)
    migrated = dropped_unmatched = dropped_failed = collisions = 0
    for p, v in old.items():
        if v is None:
            dropped_failed += 1          # known-bad; a fresh dig may retry it
            continue
        h = path_md5.get(p)
        if not h:
            dropped_unmatched += 1       # path unknown to any surviving run
            continue
        if h in new:
            collisions += 1              # already present — keep first value
            continue
        new[h] = v
        migrated += 1
    if migrated or collisions:
        save_cache(new_cache_path, new)
    return migrated, dropped_unmatched, dropped_failed, collisions


# --------------------------------------------------------------------------
# run loading
# --------------------------------------------------------------------------

def load_collections(report_json):
    with open(report_json, encoding="utf-8") as f:
        r = json.load(f)
    return [
        d["path"] for d in r["dirs"]
        if d.get("kind") in ("collection", "android")
    ]


def load_md5_map(hashes_tsv):
    m = {}
    with open(hashes_tsv, encoding="utf-8") as f:
        for line in f:
            if "\t" not in line:
                continue
            h, path = line.rstrip("\r\n").split("\t", 1)
            m[path] = h
    return m


def latest_run(data_home):
    latest = os.path.join(data_home, "latest")
    if os.path.isdir(latest):
        return os.path.realpath(latest)
    runs = os.path.join(data_home, "runs")
    if os.path.isdir(runs):
        allr = sorted(d for d in os.listdir(runs)
                      if os.path.isdir(os.path.join(runs, d)))
        if allr:
            return os.path.join(runs, allr[-1])
    raise SystemExit("no runs found — scan something first (brenda scan)")


# --------------------------------------------------------------------------
# main pipeline
# --------------------------------------------------------------------------

def dig(args):
    _deps()
    import compare
    genres = {g.strip().lower() for g in args.genres.split(",")}
    t0 = time.time()

    # entries: (path, md5, key, genres) — local roots first (priority), then
    # the drive's collections, so dedup prefers local paths and playlists
    # survive the drive being unplugged
    entries = []

    if not args.no_local:
        idx_file = compare.index_path()
        index, stats = compare.build_index(args.local, idx_file, args.workers)
        print(f"local index: {stats['files']:,} files "
              f"({stats['cache_hits']:,} cached, {stats['hashed']:,} hashed)",
              file=sys.stderr)
        n_local = 0
        for fp, rec in compare.all_files(index):
            g, key = tag_probe(fp)
            if not any(any(t in gg for gg in g) for t in genres):
                continue
            entries.append((fp, rec.get("md5") or file_md5(fp), key, g))
            n_local += 1
        print(f"local candidates: {n_local}", file=sys.stderr)

    print(f"collections: {args.report}", file=sys.stderr)
    collections = load_collections(args.report)
    print(f"  {len(collections)} real collections", file=sys.stderr)

    md5_map = load_md5_map(args.hashes)
    print(f"  md5 map: {len(md5_map)} paths", file=sys.stderr)

    n_drive = 0
    for coll in collections:
        for dp, _dirs, fns in os.walk(coll, followlinks=False):
            for fn in fns:
                if fn.lower().rsplit(".", 1)[-1] not in AUDIO_EXT:
                    continue
                fp = os.path.join(dp, fn)
                g, key = tag_probe(fp)
                if not any(any(t in gg for gg in g) for t in genres):
                    continue
                h = md5_map.get(fp)
                if h is None:
                    h = file_md5(fp)
                entries.append((fp, h, key, g))
                n_drive += 1
    print(f"drive candidates: {n_drive}", file=sys.stderr)

    candidates = dedup_candidates(entries)
    print(f"genre-matched, deduped: {len(candidates)} "
          f"({time.time()-t0:.0f}s)", file=sys.stderr)

    for tg in sorted(genres):
        n = sum(1 for _fp, _h, _k, g in candidates
                if any(tg in gg for gg in g))
        print(f"  {tg}: {n}", file=sys.stderr)

    cache = load_cache(args.cache)
    found = [(fp, h) for fp, h, _k, _g in candidates
             if h in cache and cache[h] is not None
             and args.bpm_min <= cache[h] <= args.bpm_max]
    rng = random.Random(args.seed)
    todo = [(fp, h) for fp, h, _k, _g in candidates if h not in cache]
    rng.shuffle(todo)

    print(f"need bpm: {len(todo)}  cached-in-range: {len(found)}  "
          f"cached total: {len(cache)}", file=sys.stderr)

    def accept(fp, h, b):
        if h is not None:
            cache[h] = b
        if b is not None and args.bpm_min <= b <= args.bpm_max:
            found.append((fp, h))

    if args.workers <= 1 or len(todo) < 8:
        for i, (fp, h) in enumerate(todo, 1):
            _p, b = detect_bpm(fp)
            accept(fp, h, b)
            if i % 50 == 0:
                print(f"  bpm {i}/{len(todo)}  in-range {len(found)}",
                      file=sys.stderr)
            if len(found) >= args.max:
                break
    else:
        done = 0
        while done < len(todo):
            batch = todo[done:done + args.workers * 8]
            hmap = dict(batch)
            with ProcessPoolExecutor(max_workers=args.workers) as ex:
                for fp, b in ex.map(detect_bpm, [fp for fp, _h in batch],
                                    chunksize=4):
                    accept(fp, hmap[fp], b)
            done += len(batch)
            print(f"  bpm {done}/{len(todo)}  in-range {len(found)}",
                  file=sys.stderr)
            if len(found) >= args.max:
                break

    save_cache(args.cache, cache)

    # the cap respects candidate order (local first), not compute order
    order = {fp: i for i, (fp, _h, _k, _g) in enumerate(candidates)}
    found.sort(key=lambda x: order.get(x[0], 1 << 30))
    keep = [fp for fp, _h in found[:args.max]]
    by_genre = {fp: [gg for gg in g if any(t in gg for t in genres)]
                for fp, _h, _k, g in candidates}
    md5_of = {fp: h for fp, h, _k, _g in candidates}

    print(f"\nin-range ({args.bpm_min}-{args.bpm_max}): {len(found)}  "
          f"writing {len(keep)} tracks", file=sys.stderr)
    for fp in keep:
        g = by_genre.get(fp, [])
        b = cache.get(md5_of.get(fp))
        print(f"  {b} bpm  {','.join(g):25s} {display(fp)}", file=sys.stderr)

    os.makedirs(args.out, exist_ok=True)
    dur = total_duration(keep)
    genres_str = "-".join(sorted(genres))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = (f"{genres_str}_{fmt_bpm_range(args.bpm_min, args.bpm_max)}_"
            f"{fmt_duration(dur)}_{stamp}.m3u")
    out = os.path.join(args.out, name)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for fp in keep:
            f.write(f"#EXTINF:{track_duration(fp):.0f},"
                    f"{os.path.basename(fp)}\n{fp}\n")

    print(f"\nwrote {out}  ({len(keep)} tracks, {fmt_duration(dur)} total, "
          f"{time.time()-t0:.0f}s elapsed)", file=sys.stderr)
    return 0


def run_cli(argv=None):
    import frm
    dh = frm.data_home()
    ap = argparse.ArgumentParser(
        prog="brenda dig",
        description="Genre + BPM playlist from the latest scan "
                    "(read-only; writes only the .m3u and the BPM cache).")
    ap.add_argument("--report", default=os.path.join(dh, "latest", "report.json"),
                    help="report.json of a run (default: latest)")
    ap.add_argument("--hashes", default=None,
                    help="hashes.tsv (default: next to --report)")
    ap.add_argument("--out", default=os.path.join(dh, "playlists"),
                    help="output directory (default: %(default)s)")
    ap.add_argument("--cache", default=os.path.join(dh, "bpm_cache.tsv"),
                    help="md5-keyed BPM cache (default: %(default)s)")
    ap.add_argument("--local", action="append", default=None, metavar="ROOT",
                    help="local root(s) whose music joins the playlist; "
                         "local copies win dedup over drive copies "
                         "(default: your home dir; repeatable)")
    ap.add_argument("--no-local", action="store_true",
                    help="drive-only playlist (pre-1.0 behavior)")
    ap.add_argument("--genres", default="soul,rock,jazz,world")
    ap.add_argument("--bpm-min", type=float, default=85.0)
    ap.add_argument("--bpm-max", type=float, default=90.0)
    ap.add_argument("--max", type=int, default=100)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    if args.hashes is None:
        args.hashes = os.path.join(os.path.dirname(args.report), "hashes.tsv")
    if args.local is None:
        args.local = [os.path.expanduser("~")]
    if not os.path.isfile(args.report):
        raise SystemExit(f"no report at {args.report} — scan something first")
    if not os.path.isfile(args.hashes):
        raise SystemExit(f"no hashes.tsv at {args.hashes} — re-scan without "
                         "--no-dedup for dedup support")
    return dig(args)


def self_test():
    """Self-test for the md5-keyed cache, migration, and dedup logic
    (no audio deps)."""
    import tempfile
    import shutil
    print("brenda dig self-test (cache + migration + dedup)")
    tmp = tempfile.mkdtemp(prefix="brenda-dig-test-")
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'ok  ' if cond else 'FAIL'}] {name}")
        if not cond:
            ok = False

    try:
        cache_file = os.path.join(tmp, "bpm_cache.tsv")
        save_cache(cache_file, {"aaa": 120.5, "bbb": None, "ccc": 90.0})
        c = load_cache(cache_file)
        check("cache round-trip", c["aaa"] == 120.5 and c["bbb"] is None
              and c["ccc"] == 90.0)

        # old path-keyed cache + a run's hashes.tsv
        old = os.path.join(tmp, "old_cache.tsv")
        with open(old, "w", encoding="utf-8", newline="\n") as f:
            f.write("/old/path/song1.mp3\t120.5\n")     # will map via hashes
            f.write("/old/path/song2.mp3\t95.0\n")      # unknown path -> drop
            f.write("/old/path/junk.mp3\tNone\n")       # failure -> drop
        rundir = os.path.join(tmp, "runs", "x-drive")
        os.makedirs(rundir)
        with open(os.path.join(rundir, "hashes.tsv"), "w", encoding="utf-8") as f:
            f.write("deadbeef\t/old/path/song1.mp3\n")
        new_cache = os.path.join(tmp, "new", "bpm_cache.tsv")
        m, du, df, col = migrate_path_cache(old, [rundir], new_cache)
        check("1 migrated", m == 1)
        check("1 unmatched dropped", du == 1)
        check("1 failed dropped", df == 1)
        c2 = load_cache(new_cache)
        check("md5 key present with value", c2.get("deadbeef") == 120.5)
        check("no junk keys", len(c2) == 1)

        # collision: existing entry wins, no overwrite
        with open(old, "w", encoding="utf-8", newline="\n") as f:
            f.write("/old/path/song1.mp3\t140.0\n")
        m2, _du2, _df2, col2 = migrate_path_cache(old, [rundir], new_cache)
        check("collision counted, no overwrite", m2 == 0 and col2 == 1
              and load_cache(new_cache)["deadbeef"] == 120.5)

        # --- dedup_candidates ---------------------------------------------
        # local-first: byte-identical drive copy loses to the local entry
        d = dedup_candidates([
            ("/home/x/Music/A/a.flac", "h1", "t|a|one", ["rock"]),
            ("/media/drive/Music/A/a.flac", "h1", "t|a|one", ["rock"]),
        ])
        check("md5 dup: local wins", len(d) == 1
              and d[0][0] == "/home/x/Music/A/a.flac")

        # same song different bytes: better format replaces worse, and the
        # replaced md5 is unbound (a later file with that md5 may join)
        d = dedup_candidates([
            ("/home/x/Music/A/a.mp3", "h1", "t|a|one", ["rock"]),
            ("/media/drive/Music/A/a.flac", "h2", "t|a|one", ["rock"]),
            ("/home/x/Music/B/b.mp3", "h1", "t|b|two", ["rock"]),
        ])
        check("format rank: flac replaces local mp3",
              len(d) == 2 and d[0][0] == "/media/drive/Music/A/a.flac"
              and any(x[0] == "/home/x/Music/B/b.mp3" for x in d))

        # tie (same rank): first (local) wins
        d = dedup_candidates([
            ("/home/x/Music/A/a.mp3", "h1", "t|a|one", ["rock"]),
            ("/media/drive/Music/A/a.mp3", "h2", "t|a|one", ["rock"]),
        ])
        check("rank tie: earlier (local) wins", len(d) == 1
              and d[0][0] == "/home/x/Music/A/a.mp3")

        # md5=None never collides on bytes; None key never collides on song
        # (two entries sharing only a tag key with unknown bytes still dedup
        # by the tie rule — same song, keep the first)
        d = dedup_candidates([
            ("/x/one.mp3", None, "t|a|one", []),
            ("/x/two.mp3", None, "t|a|two", []),
            ("/x/three.mp3", "h9", None, []),
            ("/x/four.mp3", None, None, []),
        ])
        check("None md5/key never collide", len(d) == 4)
        d = dedup_candidates([
            ("/x/one.mp3", None, "t|a|one", []),
            ("/x/two.mp3", None, "t|a|one", []),
        ])
        check("same song unknown bytes: tie keeps first", len(d) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_cli())
