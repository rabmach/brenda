#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""frm - Find Redundant Music directories on a drive.

Read-only analysis of any mount point / path for directories named "Music".
It inventories every Music dir found, classifies it (real collection vs
app/OS boilerplate), measures its structure, finds byte-identical duplicates
(MD5), detects whole-directory mirror copies, and matches the *same songs*
across collections even when the format differs (FLAC vs MP3 vs OGG).

Prints a pretty HTML report plus Markdown and JSON. Everything is saved
under brenda's data home (override with --outdir). The scanned drive is
NEVER written to: this tool only reads directory entries and opens audio
files inside Music directories (for content hashing). Nothing else on the
drive is read.

Requirements: Python 3.8+ (standard library only) on any amd64 Debian system.
No root, no pip installs.

Usage:
    frm.py [DIRECTORY] [options]

    If DIRECTORY is omitted, an external drive is auto-detected
    (mounted under /media, /run/media or /mnt). Use --list-drives to see
    what it finds.

Options:
    --list-drives       list candidate drives and exit
    --no-dedup          skip content hashing (byte-level dedup). Faster; the
                        report then omits the byte-duplication section
    --no-songmatch      skip format-agnostic song matching
    --min-audio N       minimum audio files for a Music dir to count as a
                        real collection (default 3)
    --outdir DIR        write outputs to DIR instead of the brenda data home
    --workers N         parallel hash workers (default: CPU count, capped at 8)
    --quiet             less chatter on stderr
    --open              open the HTML report when done (xdg-open)
    --self-test         run the built-in sanity checks against a temp tree and exit
    --version           print version and exit
    -h, --help          this help

Exit status: 0 = ok, 1 = usage/error, 2 = nothing usable found.
"""

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import shutil
from concurrent.futures import ProcessPoolExecutor

VERSION = "1.1.0"


def data_home():
    """brenda data home: $BRENDA_DATA_HOME or $XDG_DATA_HOME/brenda."""
    d = os.environ.get("BRENDA_DATA_HOME")
    if d:
        return d
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(xdg, "brenda")

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

AUDIO_EXT = {
    "mp3", "flac", "ogg", "oga", "opus", "m4a", "m4b", "m4p", "aac",
    "wav", "wv", "wma", "ape", "mpc", "aiff", "aif", "au", "ra", "rm",
    "ac3", "dts", "amr", "mid", "midi",
}
PLAYLIST_EXT = {"m3u", "m3u8", "pls", "xspf", "wpl", "zpl", "cue"}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff"}
SIDECAR_EXT = {"xml", "nfo", "log", "md5", "ffp", "sfv", "txt", "db", "db-journal"}
DOC_EXT = {"pdf", "html", "htm", "doc", "docx", "ppt", "pptx", "xls", "xlsx"}

# paths that indicate a Music dir is app/OS boilerplate, not a real collection
WINE_MARK = os.sep + "drive_c" + os.sep + "users" + os.sep + "Public" + os.sep + "Music"
ANDROID_APP_MARK = "com.maxmpz.audioplayer"   # Poweramp
XBMC_MARK = "Thumbnails" + os.sep + "Music"
IPOD_MARK = "iTunes_Control" + os.sep + "Music"

# path components (any depth, case-insensitive) that mark a source-tree
# location: a "Music" dir there is app/web code, not a real collection.
# Pruned during discovery so such trees are never scanned at all.
EXCLUDE_PATH_COMPONENTS = {"development", "projects"}

# a Music dir needs at least this many audio files to be treated as a real
# collection; dirs with fewer (web/LAMP assets, stray single tracks) are
# dropped from the report and from all comparisons.
MIN_AUDIO_DEFAULT = 3

# basenames found at a Music dir top level that are usually not artist folders
UTILITY_DIR_HINTS = ("covers", "playlist", "stream", "jangotunage",
                     "straggler", "cover art", "downloads", "misc")

KIND_BADGE = {
    "collection": "collection",
    "android": "android copy",
    "mirror": "mirror copy",
    "ipod": "ipod dump",
    "nested": "nested album",
    "wine": "wine boilerplate",
    "cache": "cache",
    "empty": "empty",
}
KIND_COLOR = {
    "collection": "#4caf50", "android": "#2196f3", "mirror": "#ff9800",
    "ipod": "#795548", "nested": "#607d8b", "wine": "#9e9e9e",
    "cache": "#9e9e9e", "empty": "#616161",
}

TRACK_LEAD = re.compile(
    r"^(?:(?:cd|disc|disk)\s*\d{1,3}\s*[-_.]?\s*)?\d{1,3}\s*[-_.]\s*", re.I)
TRACK_WORD = re.compile(r"^(?:track)\s*\d{1,3}\s*[-_.]?\s*", re.I)
SEP = re.compile(r"[\s_.\-]+")
NONALNUM = re.compile(r"[^a-z0-9 ]")


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def is_music_dir_name(name: str) -> bool:
    return name.lower() == "music"


def discover_music_dirs(root: str):
    """Walk root (dir entries only, no file contents) and yield every
    directory whose basename is 'Music' (case-insensitive)."""
    found = []
    walk_errors = []

    def onerror(err):
        walk_errors.append(str(err))

    for dirpath, dirnames, _filenames in os.walk(root, onerror=onerror,
                                                 followlinks=False):
        # prune source-tree branches so we never descend into them (saves
        # time and keeps their "Music" dirs out of the results entirely)
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in EXCLUDE_PATH_COMPONENTS]
        for d in dirnames:
            if is_music_dir_name(d):
                p = os.path.join(dirpath, d)
                found.append(p)
    return found, walk_errors


# --------------------------------------------------------------------------
# per-directory scan (metadata only - no content reads)
# --------------------------------------------------------------------------

def scan_music_dir(path):
    """Stat every entry inside a Music dir (no file content opened).
    Returns a dict of structure counts + a file manifest of audio files."""
    s = {
        "files": 0, "dirs": 0, "top_dirs": 0, "top_files": 0,
        "size_bytes": 0, "audio_files": 0, "audio_bytes": 0,
        "playlists": 0, "images": 0, "image_bytes": 0,
        "hidden": 0, "sidecars": 0, "docs": 0, "other": 0,
        "symlinks": 0, "unreadable": 0,
        "other_ext": {}, "other_biggest": [],
        "playlist_names": [], "hidden_names": [], "sidecar_names": [],
        "top_loose": [], "top_dir_names": [],
        "has_android_marker": False, "audio_files_list": [],
    }
    ext = lambda p: os.path.splitext(p)[1][1:].lower() if os.path.splitext(p)[1] else ""

    for base, dirs, files in os.walk(path, followlinks=False):
        depth = base[len(path):].count(os.sep)
        if depth == 0:
            for d in dirs:
                s["top_dirs"] += 1
                s["top_dir_names"].append(d)
            for f in files:
                s["top_files"] += 1
                if len(s["top_loose"]) < 5:
                    s["top_loose"].append(f)
        for d in dirs:
            s["dirs"] += 1
        for f in files:
            fp = os.path.join(base, f)
            s["files"] += 1
            try:
                st = os.lstat(fp)
            except OSError:
                s["unreadable"] += 1
                continue
            if os.path.islink(fp):
                s["symlinks"] += 1
                continue
            if not os.path.isfile(fp):
                continue
            s["size_bytes"] += st.st_size
            e = ext(f)
            if e in AUDIO_EXT:
                s["audio_files"] += 1
                s["audio_bytes"] += st.st_size
                s["audio_files_list"].append(fp)
            elif e in PLAYLIST_EXT:
                s["playlists"] += 1
                if len(s["playlist_names"]) < 6:
                    s["playlist_names"].append(f)
            elif e in IMAGE_EXT:
                s["images"] += 1
                s["image_bytes"] += st.st_size
            elif f.startswith("."):
                s["hidden"] += 1
                if len(s["hidden_names"]) < 6:
                    s["hidden_names"].append(f)
            elif e in SIDECAR_EXT:
                s["sidecars"] += 1
                if len(s["sidecar_names"]) < 6:
                    s["sidecar_names"].append(f)
            elif e in DOC_EXT:
                s["docs"] += 1
            else:
                s["other"] += 1
                s["other_ext"][e or "(none)"] = s["other_ext"].get(e or "(none)", 0) + 1
                if len(s["other_biggest"]) < 4:
                    s["other_biggest"].append((f, st.st_size))

    marker = os.path.join(path, ".thumbnails", ".database_uuid")
    s["has_android_marker"] = os.path.exists(marker)
    s["audio_files_list"].sort()
    return s


def classify(path, s, music_set):
    """Return (kind, note)."""
    p = path
    # path-based markers first (they identify boilerplate even when empty)
    if IPOD_MARK in p:
        return "ipod", "iPod library dump (iTunes_Control hash buckets)"
    if WINE_MARK in p:
        return "wine", "Wine/Windows 'Public Music' boilerplate"
    if ANDROID_APP_MARK in p:
        return "android", "Android app folder (empty cache)"
    if XBMC_MARK in p:
        return "cache", "media-player thumbnail cache"
    if s["audio_files"] == 0 and s["files"] == 0:
        return "empty", "no files at all"
    # nested inside another Music dir?
    parent = os.path.dirname(path)
    while parent and parent != os.path.dirname(parent):
        if parent in music_set:
            return "nested", "lives inside another Music directory"
        parent = os.path.dirname(parent)
    if s["has_android_marker"]:
        return "android", "came off an Android device (has .thumbnails marker)"
    if s["audio_files"] > 0:
        if s["top_dirs"] == 0:
            return "collection", "real music collection (flat — no subdirectories)"
        return "collection", "real music collection"
    return "empty", "no audio files"


# --------------------------------------------------------------------------
# content hashing (audio files only)
# --------------------------------------------------------------------------

def _hash_one(fp):
    try:
        h = hashlib.md5()
        with open(fp, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return fp, h.hexdigest(), None
    except OSError as e:
        return fp, None, str(e)


def hash_audio_files(file_list, workers, quiet):
    """MD5 every audio file. Only audio files inside Music dirs are opened."""
    hashes, errors = {}, {}
    if not file_list:
        return hashes, errors
    if workers <= 1 or len(file_list) < 50:
        done = 0
        for fp in file_list:
            p, h, e = _hash_one(fp)
            if e:
                errors[p] = e
            else:
                hashes[p] = h
            done += 1
            if not quiet and done % 250 == 0:
                print(f"  hashed {done}/{len(file_list)}", file=sys.stderr)
        return hashes, errors
    n = min(workers, 8)
    done = 0
    with ProcessPoolExecutor(max_workers=n) as ex:
        for p, h, e in ex.map(_hash_one, file_list, chunksize=16):
            if e:
                errors[p] = e
            else:
                hashes[p] = h
            done += 1
            if not quiet and done % 500 == 0:
                print(f"  hashed {done}/{len(file_list)}", file=sys.stderr)
    return hashes, errors


# --------------------------------------------------------------------------
# song matching (format-agnostic)
# --------------------------------------------------------------------------

def norm_name(name):
    n = os.path.splitext(name)[0]
    n = TRACK_LEAD.sub("", n)
    n = TRACK_WORD.sub("", n)
    n = n.lower()
    n = SEP.sub(" ", n)
    n = NONALNUM.sub("", n)
    return re.sub(r"\s+", " ", n).strip()


def song_keys_for(path, audio_list):
    """(normalized artist-folder, normalized track) keys for one collection."""
    keys = set()
    for fp in audio_list:
        artist = os.path.basename(os.path.dirname(fp))
        track = os.path.basename(fp)
        keys.add((norm_name(artist), norm_name(track)))
    return keys


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def analyze(root, cfg):
    t0 = datetime.datetime.now()
    music_dirs, walk_errors = discover_music_dirs(root)
    music_set = set(music_dirs)
    if root.rstrip(os.sep).lower().endswith(os.sep + "music") and root.rstrip(os.sep) not in music_set:
        music_set.add(root.rstrip(os.sep))
        music_dirs.append(root.rstrip(os.sep))
    music_dirs = sorted(set(music_dirs))
    n_found = len(music_dirs)

    scan_errors = []
    stats = {}
    for p in music_dirs:
        try:
            stats[p] = scan_music_dir(p)
        except OSError as e:
            stats[p] = None
            scan_errors.append(f"{p}: {e}")

    # classify
    kinds = {}
    for p in music_dirs:
        st = stats[p]
        if st is None:
            kinds[p] = ("empty", "unreadable")
        else:
            kinds[p] = classify(p, st, music_set)

    # which are real collections: they must pass the min-audio rail (a Music
    # dir needs actual music files, not a stray single track), and must not
    # be nested inside another Music dir (nested is covered by its parent's
    # scan, so it must not be double-counted)
    candidates = [
        p for p in music_dirs
        if stats[p] is not None
        and stats[p]["audio_files"] >= cfg["min_audio"]
        and kinds[p][0] in ("collection", "android", "ipod")
    ]

    # --- whole-directory mirror detection (path+size signatures, no content) ---
    def signature(p):
        sig = []
        for base, _dirs, files in os.walk(p, followlinks=False):
            for f in files:
                fp = os.path.join(base, f)
                rel = os.path.relpath(fp, p)
                try:
                    sig.append((rel, os.lstat(fp).st_size))
                except OSError:
                    pass
        return sorted(sig)

    mirror_pairs = []
    sig_cache = {}
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a, b = candidates[i], candidates[j]
            if a not in sig_cache:
                sig_cache[a] = signature(a)
            if b not in sig_cache:
                sig_cache[b] = signature(b)
            if len(sig_cache[a]) == len(sig_cache[b]) and sig_cache[a] == sig_cache[b]:
                mirror_pairs.append((a, b))

    # reclassify copies: in each mirror pair the "primary" is the shallower
    # path (tie -> lexicographically first); the other is a mirror copy
    for a, b in mirror_pairs:
        def _depth(p):
            return len([c for c in p.split(os.sep) if c])
        if (_depth(a), a) < (_depth(b), b):
            primary, copy = a, b
        else:
            primary, copy = b, a
        kinds[copy] = ("mirror",
                       f"exact mirror copy of {dir_short(primary, root)} "
                       "(same file tree + sizes)")

    # hashed/matrix set = all candidates including mirror copies (their 0%
    # unique ratio is informative; content-verification needs their hashes)
    top_collections = candidates

    # --- byte-level dedup (optional) ---
    byte = None
    hash_by_path = {}
    if cfg["dedup"]:
        all_audio = []
        for p in top_collections:
            all_audio.extend(stats[p]["audio_files_list"])
        hashes, hash_errors = hash_audio_files(all_audio, cfg["workers"], cfg["quiet"])
        hash_by_path = hashes
        # group by md5
        by_hash = {}
        for p, h in hashes.items():
            by_hash.setdefault(h, []).append(p)
        n_unique = len(by_hash)
        n_instances = len(hashes)
        # clean dedup ledger: unique_bytes = one copy of each distinct hash,
        # dup_bytes = everything beyond the first copy. Sum == all hashed bytes.
        dup_bytes = 0
        unique_bytes = 0
        for h, paths in by_hash.items():
            try:
                size = os.lstat(paths[0]).st_size
            except OSError:
                size = 0
            unique_bytes += size
            dup_bytes += (len(paths) - 1) * size
        # per-collection unique-at-byte-level
        per_coll = {}
        for p in top_collections:
            own = [fp for fp in stats[p]["audio_files_list"] if fp in hash_by_path]
            unique_files = sum(1 for fp in own if len(by_hash[hash_by_path[fp]]) == 1)
            unique_bytes_c = sum(
                os.lstat(fp).st_size for fp in own if len(by_hash[hash_by_path[fp]]) == 1)
            audio_bytes_c = stats[p]["audio_bytes"]
            per_coll[p] = {
                "audio_files": len(own),
                "audio_bytes": audio_bytes_c,
                "unique_files": unique_files,
                "unique_bytes": unique_bytes_c,
                "unique_ratio": (unique_bytes_c / audio_bytes_c) if audio_bytes_c else 1.0,
            }
        # pairwise byte overlap (files of row that exist in column, by hash)
        pair_bytes = {}
        for a in top_collections:
            for b in top_collections:
                if a == b:
                    continue
                set_b = {hash_by_path[fp] for fp in stats[b]["audio_files_list"] if fp in hash_by_path}
                cnt = sum(1 for fp in stats[a]["audio_files_list"]
                          if fp in hash_by_path and hash_by_path[fp] in set_b)
                pair_bytes[a + "\t" + b] = cnt
        byte = {
            "unique_files": n_unique,
            "instances": n_instances,
            "unique_bytes": unique_bytes,
            "dup_bytes": dup_bytes,
            "hash_errors": len(hash_errors),
            "pairwise_files_in_other": pair_bytes,
            "per_collection": per_coll,
            "hash_by_path": hash_by_path,
        }

    # --- song-level (format-agnostic) ---
    song = None
    if cfg["songmatch"]:
        keys_by_coll = {p: song_keys_for(p, stats[p]["audio_files_list"]) for p in top_collections}
        # global counts
        song_members = {}
        for p, keys in keys_by_coll.items():
            for k in keys:
                song_members.setdefault(k, set()).add(p)
        unique_songs = len(song_members)
        mult = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for k, cols in song_members.items():
            mult[min(len(cols), 5)] = mult.get(min(len(cols), 5), 0) + 1
        per_coll = {}
        for p in top_collections:
            total = len(keys_by_coll[p])
            only_here = sum(1 for k in keys_by_coll[p] if len(song_members[k]) == 1)
            per_coll[p] = {"songs": total, "only_here": only_here}
        matrix = {}
        for a in top_collections:
            matrix[a] = {}
            la = len(keys_by_coll[a])
            for b in top_collections:
                if a == b:
                    matrix[a][b] = None
                    continue
                if la == 0:
                    matrix[a][b] = 0.0
                    continue
                lb = len(keys_by_coll[b])
                shared = len(keys_by_coll[a] & keys_by_coll[b])
                matrix[a][b] = (100.0 * shared / la, 100.0 * shared / lb)
        song = {
            "unique_songs": unique_songs,
            "multiplicity": {k: v for k, v in sorted(mult.items())},
            "per_collection": per_coll,
            "matrix": matrix,
        }

    # twin detection: pairs with ~100% song overlap
    twins = []
    if song:
        cols = top_collections
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, b = cols[i], cols[j]
                if not keys_by_coll[a] or not keys_by_coll[b]:
                    continue
                shared = len(keys_by_coll[a] & keys_by_coll[b])
                if 100.0 * shared / len(keys_by_coll[a]) >= 98.0:
                    twins.append((a, b, 100.0 * shared / len(keys_by_coll[a])))

    # mirror byte-verification (if we hashed)
    if byte:
        for a, b in mirror_pairs:
            ha = {hash_by_path[fp] for fp in stats[a]["audio_files_list"] if fp in hash_by_path}
            hb = {hash_by_path[fp] for fp in stats[b]["audio_files_list"] if fp in hash_by_path}
            if ha and ha == hb:
                byte.setdefault("mirrors_content_verified", []).append((a, b))

    elapsed = (datetime.datetime.now() - t0).total_seconds()

    # report set: only real music dirs. anything that fails the min-audio
    # rail (wine/cache/android-app/empty/stubs) is dropped entirely.
    report_kinds = ("collection", "android", "ipod", "mirror", "nested")
    report_dirs = [
        p for p in music_dirs
        if stats[p] is not None
        and stats[p]["audio_files"] >= cfg["min_audio"]
        and kinds[p][0] in report_kinds
    ]

    dirs_out = []
    for p in report_dirs:
        entry = {"path": p, "kind": kinds[p][0], "kind_note": kinds[p][1]}
        entry.update(stats[p])
        dirs_out.append(entry)

    n_kept = len(report_dirs)
    return {
        "meta": {
            "tool": "frm", "version": VERSION, "root": root,
            "time": t0.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(elapsed, 1),
            "python": sys.version.split()[0],
            "n_music_dirs": n_kept,
            "n_found": n_found,
            "n_skipped": n_found - n_kept,
            "walk_errors": walk_errors[:20],
            "scan_errors": scan_errors[:20],
        },
        "dirs": dirs_out,
        "mirror_pairs": mirror_pairs,
        "twins": twins,
        "byte": byte,
        "song": song,
    }


# --------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------

def fmt_bytes(b):
    g = b / 1e9
    if g >= 1000:
        return f"{g/1000:.2f} TB"
    if g >= 1:
        return f"{g:,.2f} GB"
    m = b / 1e6
    if m >= 1:
        return f"{m:,.1f} MB"
    return f"{b:,} B"


def esc(s):
    return html.escape(str(s), quote=True)


def dir_short(path, root):
    return os.path.relpath(path, root) if os.path.commonpath([root, path]) else path


def loc(path, root):
    """Display form of a collection path: drive name + path relative to the
    scan root. Display ONLY — dir_short() stays the export-naming scheme."""
    drive = os.path.basename(root.rstrip(os.sep)) or "root"
    return f"{drive}: {dir_short(path, root)}"


# ---- JSON ----

def render_json(data, outpath):
    with open(outpath, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    return outpath


# ---- Markdown ----

def md_table(headers, rows):
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render_markdown(data, outpath, root):
    L = []
    m = data["meta"]
    L.append(f"# Music directory index — `{m['root']}`")
    L.append("")
    L.append(f"Generated by **frm v{m['version']}** on {m['time']} "
             f"({m['elapsed_s']} s). Read-only — nothing on the drive was modified.")
    L.append("")
    L.append("## Summary")
    dirs = data["dirs"]
    colls = [d for d in dirs if d["kind"] in ("collection", "android", "ipod")]
    L.append(f"- Real music directories: **{m['n_music_dirs']}** "
             f"({len(colls)} compared)")
    if data["song"]:
        L.append(f"- Distinct songs (format-agnostic): **{data['song']['unique_songs']:,}**")
    if data["byte"]:
        L.append(f"- Byte-unique audio files: **{data['byte']['unique_files']:,}** "
                 f"({data['byte']['instances']:,} instances)")
        L.append(f"- Duplicated audio bytes: **{fmt_bytes(data['byte']['dup_bytes'])}**")
    if data["mirror_pairs"]:
        L.append(f"- Whole-directory mirror copies: **{len(data['mirror_pairs'])}**")
    L.append("")
    if data["mirror_pairs"]:
        L.append("## Whole-directory mirrors (exact copies)")
        L.append("")
        for a, b in data["mirror_pairs"]:
            L.append(f"- `{loc(a, root)}` ≡ `{loc(b, root)}`")
        L.append("")
    if data["twins"]:
        L.append("## Twins (~100% same songs, different format)")
        L.append("")
        for a, b, pct in data["twins"]:
            L.append(f"- `{loc(a, root)}` ⇄ `{loc(b, root)}` "
                     f"({pct:.0f}% same songs — likely the same library "
                     f"re-ripped/transcoded)")
        L.append("")
    L.append("## All Music directories")
    L.append("")
    rows = []
    for d in dirs:
        rows.append([
            f"`{loc(d['path'], root)}`", d["kind"], d["files"], d["dirs"],
            d.get("top_dirs", ""), d["audio_files"], d["playlists"],
            d["hidden"], fmt_bytes(d["size_bytes"]), d["kind_note"],
        ])
    L.append(md_table(
        ["Location", "Kind", "files", "folders", "artist folders",
         "audio", "playlists", "hidden", "size", "note"], rows))
    L.append("")
    if data["byte"]:
        L.append("## Byte-level duplicates")
        L.append("")
        pc = data["byte"]["per_collection"]
        rows = []
        for p in sorted(pc):
            v = pc[p]
            rows.append([f"`{loc(p, root)}`", v["audio_files"],
                         v["audio_bytes"], f"{v['unique_files']:,}",
                         f"{v['unique_ratio']*100:.0f}%"])
        L.append(md_table(["Collection", "audio files", "audio bytes",
                           "byte-unique files", "unique share"], rows))
        L.append("")
    if data["song"]:
        L.append("## Song-level overlap")
        L.append("")
        cols = list(data["song"]["matrix"])
        rows = []
        for a in cols:
            row = [f"`{loc(a, root)}`"]
            for b in cols:
                v = data["song"]["matrix"][a][b]
                row.append(f"{v[0]:.0f}%" if v else "—")
            rows.append(row)
        L.append(md_table(["collection \\ in"] + [loc(c, root) for c in cols], rows))
        L.append("")
        L.append("*(row % of songs also present in column; heuristic, filename-based)*")
        L.append("")
    L.append("---")
    L.append("*Nothing was deleted, moved, or modified. Full data in the JSON file "
             "alongside this report.*")
    with open(outpath, "w") as f:
        f.write("\n".join(L) + "\n")
    return outpath


# ---- HTML ----

CSS = """
:root{--bg:#0f1419;--card:#161d26;--card2:#1b2530;--txt:#dbe4ee;--dim:#8fa3b8;
--line:#26323f;--acc:#4fc3f7}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--txt);
font:15px/1.5 -apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}
.wrap{max-width:1200px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:20px;margin:34px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{margin:0 0 6px;font-size:18px}
h4{margin:12px 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}
.dim{color:var(--dim);font-weight:400;font-size:14px}
.sub{color:var(--dim);margin:0 0 14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi{font-size:24px;font-weight:700;color:var(--acc)}
.kpi small{display:block;font-size:11px;color:var(--dim);font-weight:400;
text-transform:uppercase;letter-spacing:.06em;margin-top:2px}
table{border-collapse:collapse;width:100%;background:var(--card);border-radius:10px;
overflow:hidden;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{background:var(--card2);color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.loc code{color:#a5d6a7;font-size:12px;word-break:break-all}
td.note{color:var(--dim)}
.badge{display:inline-block;padding:1px 8px;border-radius:20px;color:#0b1015;
font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.03em}
code{font-family:'SF Mono',Menlo,Consolas,monospace}
.fmts{display:flex;height:16px;border-radius:8px;overflow:hidden;margin:6px 0 4px;background:#0a0f14}
.fbar{height:100%}
.fmts-legend{color:var(--dim);font-size:12px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 4px 0 8px;vertical-align:middle}
.stats td:first-child{color:var(--dim)}
.stats td:last-child{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.anc{background:var(--card2);border-radius:8px;padding:8px 14px;margin-top:10px}
.anc ul{margin:6px 0;padding-left:18px}
.anc li{margin:4px 0}
.warn{background:#33291a;border:1px solid #6b4f1d;border-radius:8px;padding:10px 14px;
font-size:13px;color:#e8d5a3}
footer{margin-top:30px;color:var(--dim);font-size:12px}
"""


def fmt_bar(ext_counts):
    tot = sum(ext_counts.values()) or 1
    colors = ["#4fc3f7", "#81c784", "#ffb74d", "#ba68c8", "#e57373",
              "#90a4ae", "#ff8a65", "#a1887f", "#4dd0e1", "#7986cb"]
    bar = []
    leg = []
    for i, (e, c) in enumerate(sorted(ext_counts.items(),
                                      key=lambda kv: -kv[1])):
        w = 100 * c / tot
        bar.append(f'<div style="width:{w:.1f}%;background:{colors[i%len(colors)]}" '
                   f'class="fbar" title="{esc(e)}: {c}"></div>')
        leg.append(f'<span class="dot" style="background:{colors[i%len(colors)]}"></span>'
                   f'{esc(e)} {c}')
    return ('<div class="fmts">' + "".join(bar) + '</div>'
            + '<div class="fmts-legend">' + " ".join(leg) + "</div>")


def render_html(data, outpath, root):
    m = data["meta"]
    dirs = data["dirs"]

    kpis = [
        (f"{m['n_music_dirs']}", "real music directories"),
    ]
    colls = [d for d in dirs if d["kind"] in ("collection", "android", "ipod")]
    kpis.append((str(len(colls)), "with real audio"))
    if data["song"]:
        kpis.append((f"{data['song']['unique_songs']:,}", "unique songs (format-agnostic)"))
    if data["byte"]:
        kpis.append((f"{data['byte']['unique_files']:,}",
                     "byte-unique audio files"))
        kpis.append((fmt_bytes(data["byte"]["dup_bytes"]),
                     "duplicated audio bytes"))
        kpis.append((str(len(data["byte"].get("mirrors_content_verified", []))),
                     "mirror pairs content-verified"))
    if data["mirror_pairs"]:
        kpis.append((str(len(data["mirror_pairs"])), "whole-directory mirror copies"))

    rows = ""
    for d in dirs:
        kind = d["kind"]
        rows += (f'<tr><td class="loc" title="{esc(d["path"])}">'
                 f'<code>{esc(loc(d["path"], root))}</code></td>'
                 f'<td><span class="badge" style="background:{KIND_COLOR[kind]}">'
                 f'{KIND_BADGE[kind]}</span></td>'
                 f'<td class="num">{d["dirs"]}</td>'
                 f'<td class="num">{d["top_dirs"]}</td>'
                 f'<td class="num">{d["files"]}</td>'
                 f'<td class="num">{d["audio_files"]}</td>'
                 f'<td class="num">{d["playlists"]}</td>'
                 f'<td class="num">{d["hidden"]}</td>'
                 f'<td class="num">{fmt_bytes(d["size_bytes"])}</td>'
                 f'<td class="note">{esc(d["kind_note"])}</td></tr>')

    mirrors = ""
    if data["mirror_pairs"]:
        mirrors = ('<div class="warn"><b>Whole-directory mirror copies '
                   '(identical file tree + sizes, verified):</b><br>')
        for a, b in data["mirror_pairs"]:
            mirrors += (f'<code>{esc(loc(a, root))}</code> ≡ '
                        f'<code>{esc(loc(b, root))}</code><br>')
        if data["byte"] and "mirrors_content_verified" in data["byte"]:
            mirrors += ("<br>Content hashes of these mirror pairs were compared "
                        "and match exactly.")
        mirrors += "</div>"

    twins_html = ""
    if data["twins"]:
        twins_html = ('<div class="warn" style="background:#12302a;'
                      'border-color:#1f6b56;color:#b7e4d2">'
                      '<b>Twins — ~100% song overlap but not byte-identical '
                      '(same library, different format):</b><br>')
        for a, b, pct in data["twins"]:
            twins_html += (f'<code>{esc(loc(a, root))}</code> ⇄ '
                           f'<code>{esc(loc(b, root))}</code> '
                           f'({pct:.0f}% same songs)<br>')
        twins_html += "</div>"

    cards = ""
    for d in dirs:
        if d["kind"] not in ("collection", "android", "ipod", "mirror"):
            continue
        s = d
        exts = {}
        # build audio ext histogram from file list basenames
        for fp in s["audio_files_list"]:
            e = os.path.splitext(fp)[1][1:].lower()
            exts[e] = exts.get(e, 0) + 1
        bar = fmt_bar(exts) if exts else "<p class='note'>no audio files</p>"
        anc = []
        if s["top_loose"]:
            anc.append("<b>loose files at top level</b> — " +
                       "; ".join(f"<code>{esc(x)}</code>" for x in s["top_loose"]))
        if s["playlist_names"]:
            anc.append("<b>playlists</b> — " +
                       "; ".join(f"<code>{esc(x)}</code>" for x in s["playlist_names"]))
        if s["hidden_names"]:
            anc.append("<b>hidden files</b> — " +
                       "; ".join(f"<code>{esc(x)}</code>" for x in s["hidden_names"]))
        if s["sidecar_names"]:
            anc.append("<b>sidecar files</b> — " +
                       "; ".join(f"<code>{esc(x)}</code>" for x in s["sidecar_names"]))
        if s["other_ext"]:
            top = sorted(s["other_ext"].items(), key=lambda kv: -kv[1])[:5]
            anc.append("<b>other/junk extensions</b> — " +
                       ", ".join(f"{esc(k)} ×{v}" for k, v in top))
        if s["other_biggest"]:
            anc.append("<b>largest non-music files</b> — " +
                       "; ".join(f"<code>{esc(x)}</code> ({fmt_bytes(z)})"
                                 for x, z in s["other_biggest"]))
        if s["has_android_marker"]:
            anc.append("<b>Android origin marker</b> — "
                       "<code>.thumbnails/.database_uuid</code> present")
        anc_html = ("<ul><li>" + "</li><li>".join(anc) + "</li></ul>") if anc else ""
        song_extra = ""
        if data["song"] and d["path"] in data["song"]["per_collection"]:
            v = data["song"]["per_collection"][d["path"]]
            song_extra = (f'<tr><td>distinct songs (filename-based)</td>'
                          f'<td>{v["songs"]:,}</td></tr>'
                          f'<tr><td>songs that exist only in this collection</td>'
                          f'<td>{v["only_here"]:,}</td></tr>')
        byte_extra = ""
        if data["byte"] and d["path"] in data["byte"]["per_collection"]:
            v = data["byte"]["per_collection"][d["path"]]
            byte_extra = (f'<tr><td>byte-unique audio files</td>'
                          f'<td>{v["unique_files"]:,} '
                          f'({v["unique_ratio"]*100:.0f}% of this collection)</td></tr>')
        cards += f"""
    <div class="card">
      <h3>{esc(loc(d["path"], root))}
          <span class="dim">{fmt_bytes(d["size_bytes"])}</span></h3>
      <p class="sub">{esc(d["kind_note"])}</p>
      <table class="stats">
        <tr><td>folders (total dirs)</td><td>{d["dirs"]}</td></tr>
        <tr><td>artist/album folders (top-level dirs)</td><td>{d["top_dirs"]}</td></tr>
        <tr><td>files (total)</td><td>{d["files"]}</td></tr>
        <tr><td>loose files at top level</td><td>{d["top_files"]}</td></tr>
        <tr><td>audio files</td><td>{d["audio_files"]}</td></tr>
        <tr><td>playlists</td><td>{d["playlists"]}</td></tr>
        <tr><td>hidden files</td><td>{d["hidden"]}</td></tr>
        <tr><td>images (cover art)</td><td>{d["images"]}</td></tr>
        {byte_extra}{song_extra}
      </table>
      {bar}
      {anc_html}
    </div>"""

    matrix = ""
    if data["song"]:
        cols = list(data["song"]["matrix"])
        head = "".join(f'<th class="num">{esc(loc(c, root))}</th>' for c in cols)
        body = ""
        for a in cols:
            cells = ""
            for b in cols:
                v = data["song"]["matrix"][a][b]
                cells += f'<td class="num">{v[0]:.0f}%</td>' if v else '<td class="num">—</td>'
            body += (f'<tr><td><b>{esc(loc(a, root))}</b></td>{cells}</tr>')
        matrix = (f'<h2>Song-level overlap matrix</h2>'
                  f'<p class="sub">% of the <i>row</i> collection&rsquo;s songs '
                  f'(normalized artist+title, format-blind) that also exist in '
                  f'the <i>column</i> collection.</p>'
                  f'<table><tr><th></th>{head}</tr>{body}</table>')

    ledger = ""
    if data["byte"] or data["song"]:
        ledger_rows = ""
        for d in dirs:
            if d["kind"] not in ("collection", "android", "ipod", "mirror"):
                continue
            p = d["path"]
            b = data["byte"]["per_collection"].get(p) if data["byte"] else None
            s = data["song"]["per_collection"].get(p) if data["song"] else None
            bc = f'{b["unique_ratio"]*100:.0f}% unique' if b else "—"
            sc = (f'{s["only_here"]:,} unique / {s["songs"]:,}'
                  if s else "—")
            ledger_rows += (f'<tr><td><b>{esc(loc(p, root))}</b></td>'
                            f'<td class="num">{fmt_bytes(d["audio_bytes"])}</td>'
                            f'<td class="num">{bc}</td>'
                            f'<td class="num">{sc}</td></tr>')
        ledger = ('<h2>Redundancy ledger</h2><table><tr>'
                  '<th>Collection</th><th class="num">audio bytes</th>'
                  '<th class="num">byte-level</th>'
                  '<th class="num">song-level (only here / total)</th></tr>'
                  + ledger_rows + '</table>')

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Music directory index — {esc(m["root"])}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>Music directories on <code>{esc(m["root"])}</code></h1>
<p class="sub">Read-only index — nothing modified or deleted &nbsp;·&nbsp;
generated {esc(m["time"])} by frm v{esc(m["version"])} &nbsp;·&nbsp;
analysis took {esc(m["elapsed_s"])} s &nbsp;·&nbsp; python {esc(m["python"])}</p>
<div class="cards">
{''.join(f'<div class="card"><div class="kpi">{v}<small>{esc(l)}</small></div></div>' for v, l in kpis)}
</div>
{mirrors}
{twins_html}
<h2>All Music directories</h2>
<p class="sub">folders = all subdirectories · artist folders = top-level
subdirectories · audio = track files</p>
<table><tr><th>Location (relative to scan root)</th><th>Kind</th>
<th class="num">folders</th><th class="num">artist folders</th>
<th class="num">files</th><th class="num">audio</th>
<th class="num">playlists</th><th class="num">hidden</th>
<th class="num">size</th><th>What it is</th></tr>
{rows}
</table>
{cards}
{matrix}
{ledger}
<h2>Notes on method &amp; caveats</h2>
<ul class="sub" style="padding-left:18px">
<li>Byte-level dedup: MD5 of every audio file <b>inside Music directories</b>
only. Nothing else on the drive is opened.</li>
<li>Song-level matching is a filename heuristic (normalized artist folder +
track title, format-blind). It slightly over- and under-matches, but the
magnitudes are solid.</li>
<li>Mirror pairs are detected by identical (path, size) file trees; when
content hashing ran, those pairs are additionally verified by MD5.</li>
<li>Directories named Music are matched case-insensitively.</li>
<li>Wine / cache / empty / dev-or-projects / single-track stub dirs are
excluded entirely by the min-audio rail and the development/projects
prune; they appear nowhere in this report.</li>
</ul>
<footer>frm {esc(m["version"])} — output in JSON + Markdown next to this file.</footer>
</div></body></html>"""
    with open(outpath, "w") as f:
        f.write(doc)
    return outpath


# --------------------------------------------------------------------------
# drive detection
# --------------------------------------------------------------------------

def list_drives():
    candidates = []
    system_roots = {"/", "/home", "/boot", "/boot/efi", "/efi", "/dev", "/proc",
                    "/sys", "/run", "/tmp", "/var", "/usr", "/snap", "/opt",
                    "/etc", "/lib", "/bin", "/sbin", "/root"}
    virtual_fs = {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup",
                  "cgroup2", "securityfs", "pstore", "efivarfs", "bpf",
                  "autofs", "overlay", "mqueue", "ramfs", "debugfs",
                  "tracefs", "fusectl", "configfs", "rpc_pipefs"}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mp, fs = parts[0], parts[1], parts[2]
                if mp in system_roots or mp.startswith(tuple(r + "/" for r in system_roots)):
                    continue
                if fs in virtual_fs:
                    continue
                candidates.append((dev, mp, fs))
    except OSError:
        pass
    return candidates


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def self_test():
    print("frm self-test")
    tmp = tempfile.mkdtemp(prefix="frm-selftest-")
    ok = True
    try:
        root = os.path.join(tmp, "drive")
        a = os.path.join(root, "userA", "Music")
        b = os.path.join(root, "userB", "Music")      # mirror of userA
        c = os.path.join(root, "userC", "Music")      # shares songs w/ userA, diff format
        wine = os.path.join(root, "userD", ".wine", "drive_c", "users", "Public", "Music")
        android = os.path.join(root, "userE", "Android", "data",
                               "com.maxmpz.audioplayer", "files", "Music")
        nested = os.path.join(c, "Madonna", "Music")   # real nested album (3 songs)
        dev = os.path.join(root, "userF", "development", "app", "Music")   # pruned
        proj = os.path.join(root, "userG", "projects", "game", "Music")    # pruned
        stub = os.path.join(root, "userH", "www", "phpgedview",
                            "modules", "lightbox", "music")                # 1 song, dropped
        empty = os.path.join(root, "userI", "notes", "music")              # empty, dropped
        for d in (a, b, c, wine, android, nested, dev, proj, stub, empty):
            os.makedirs(d)
        # userA: three songs + cover + playlist + hidden file
        os.makedirs(os.path.join(a, "Alpha"))
        data1 = b"DUMMY-FLAC-CONTENT-1"
        data2 = b"DUMMY-MP3-CONTENT-2"
        data3 = b"DUMMY-OGG-CONTENT-3"
        with open(os.path.join(a, "Alpha", "01 - Track One.flac"), "wb") as f:
            f.write(data1)
        with open(os.path.join(a, "Alpha", "02_Track_Two.mp3"), "wb") as f:
            f.write(data2)
        with open(os.path.join(a, "Alpha", "03 - Track Three.ogg"), "wb") as f:
            f.write(data3)
        with open(os.path.join(a, "cover.jpg"), "wb") as f:
            f.write(b"JPG")
        with open(os.path.join(a, "playlist.m3u"), "w") as f:
            f.write("# EXTINF\n")
        with open(os.path.join(a, ".hidden"), "w") as f:
            f.write("x")
        # nested album: three real songs so it stays listed as nested
        data4 = b"DUMMY-MP3-MADAME"
        data5 = b"DUMMY-MP3-PARIS"
        data6 = b"DUMMY-MP3-VOGUE"
        with open(os.path.join(nested, "01 - Madame.mp3"), "wb") as f:
            f.write(data4)
        with open(os.path.join(nested, "02 - Paris.mp3"), "wb") as f:
            f.write(data5)
        with open(os.path.join(nested, "03 - Vogue.mp3"), "wb") as f:
            f.write(data6)
        # userB = exact mirror of userA
        for rel in ("Alpha/01 - Track One.flac", "Alpha/02_Track_Two.mp3",
                    "Alpha/03 - Track Three.ogg", "cover.jpg", "playlist.m3u",
                    ".hidden"):
            os.makedirs(os.path.dirname(os.path.join(b, rel)), exist_ok=True)
            shutil.copy(os.path.join(a, rel), os.path.join(b, rel))
        # userC: shares track one (FLAC) + track two (MP3) with userA, has a
        # third song that is only here
        os.makedirs(os.path.join(c, "Alpha"))
        os.makedirs(os.path.join(c, "Beta"))
        with open(os.path.join(c, "Alpha", "track_one.flac"), "wb") as f:
            f.write(data1)
        with open(os.path.join(c, "Alpha", "Track Two.mp3"), "wb") as f:
            f.write(data2)
        with open(os.path.join(c, "Beta", "Only Here.mp3"), "wb") as f:
            f.write(b"DUMMY-MP3-UNIQUE-C")
        # stub dir: a single web-asset song, no subdirs - must be dropped
        with open(os.path.join(stub, "music.mp3"), "wb") as f:
            f.write(b"DUMMY-WEB-ASSET")
        # dev/proj Music dirs get a real file so pruning (not min-audio) is
        # what keeps them out
        with open(os.path.join(dev, "snd.mp3"), "wb") as f:
            f.write(b"DUMMY-DEV-SOUND")
        with open(os.path.join(proj, "snd.mp3"), "wb") as f:
            f.write(b"DUMMY-PROJ-SOUND")

        cfg = {"dedup": True, "songmatch": True, "workers": 2, "quiet": True,
               "min_audio": MIN_AUDIO_DEFAULT}
        data = analyze(root, cfg)

        def check(name, cond):
            nonlocal ok
            status = "ok  " if cond else "FAIL"
            print(f"  [{status}] {name}")
            if not cond:
                ok = False

        found = [d["path"] for d in data["dirs"]]
        check("report shows only real dirs (4)",
              len(found) == 4 and data["meta"]["n_music_dirs"] == 4)
        check("skipped counts add up",
              data["meta"]["n_skipped"] == data["meta"]["n_found"] - 4)
        check("dev/projects dirs pruned (never scanned)",
              data["meta"]["n_found"] == 8
              and not any("development" in p or "projects" in p for p in found))
        km = {os.path.relpath(d["path"], root): d["kind"] for d in data["dirs"]}
        check("wine boilerplate dropped entirely",
              "userD/.wine/drive_c/users/Public/Music" not in km)
        check("android app folder dropped entirely",
              "userE/Android/data/com.maxmpz.audioplayer/files/Music" not in km)
        check("phpged stub dropped entirely",
              "userH/www/phpgedview/modules/lightbox/music" not in km)
        check("empty dir dropped entirely",
              "userI/notes/music" not in km)
        check("classifies nested album", km["userC/Music/Madonna/Music"] == "nested")
        check("classifies mirror copy", km["userB/Music"] == "mirror")
        check("userA stays a collection", km["userA/Music"] == "collection")
        pairs = {(dir_short(a1, root), dir_short(a2, root)) for a1, a2 in data["mirror_pairs"]}
        check("mirror pair detected", ("userA/Music", "userB/Music") in pairs
              or ("userB/Music", "userA/Music") in pairs)
        check("mirror pair content-verified",
              any(("userA/Music", "userB/Music") == (dir_short(x, root), dir_short(y, root))
                  or ("userB/Music", "userA/Music") == (dir_short(x, root), dir_short(y, root))
                  for x, y in data["byte"].get("mirrors_content_verified", [])))
        check("byte dedup finds 7 unique files (nested songs are unique)",
              data["byte"]["unique_files"] == 7)
        check("byte dedup instance count 12 (mirror copies hashed too)",
              data["byte"]["instances"] == 12)
        check("mirror copy has 0% unique bytes",
              data["byte"]["per_collection"][b]["unique_ratio"] == 0.0)
        keys = data["song"]["unique_songs"]
        check(f"7 unique songs ({keys})", keys == 7)
        # check song matrix: track one shared userA-userC
        mA = data["song"]["matrix"]
        av = mA[os.path.join(root, "userA", "Music")][os.path.join(root, "userC", "Music")]
        check("song 'track one' shared A->C", av is not None and av[0] >= 50)
        # no zero-audio dir is ever treated as a real collection
        check("no zero-audio dir classified as collection",
              all(d["kind"] != "collection" for d in data["dirs"]
                  if d["audio_files"] == 0))
        # render smoke test
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        render_json(data, os.path.join(out, "report.json"))
        render_markdown(data, os.path.join(out, "report.md"), root)
        render_html(data, os.path.join(out, "report.html"), root)
        for fn in ("report.json", "report.md", "report.html"):
            check(f"render {fn}",
                  os.path.exists(os.path.join(out, fn)) and
                  os.path.getsize(os.path.join(out, fn)) > 200)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def export_run(data, root, outdir):
    """Write one scan run: files-*.txt, hashes.tsv, report.{json,md,html}.
    data is the analyze() dict; bulky internals are stripped from the JSON.
    Returns the HTML path. Used by frm.main() and by brenda serve's rescan —
    CLI behavior unchanged."""
    # strip bulky internals before JSON dump; keep per-dir file lists and the
    # hash table as separate plain-text files alongside the report
    data_for_json = json.loads(json.dumps(data))
    if data_for_json["byte"]:
        data_for_json["byte"].pop("hash_by_path", None)
    dirs_export = []
    for d in data_for_json["dirs"]:
        fl = d["audio_files_list"]
        rel = dir_short(d["path"], root).replace(os.sep, "__")
        with open(os.path.join(outdir, f"files-{rel}.txt"), "w") as ff:
            ff.write("\n".join(fl))
        d.pop("audio_files_list", None)
        dirs_export.append(d)
    data_for_json["dirs"] = dirs_export

    if data["byte"]:
        with open(os.path.join(outdir, "hashes.tsv"), "w") as hf:
            for fp, h in sorted(data["byte"]["hash_by_path"].items()):
                hf.write(f"{h}\t{fp}\n")

    render_json(data_for_json, os.path.join(outdir, "report.json"))
    render_markdown(data, os.path.join(outdir, "report.md"), root)
    return render_html(data, os.path.join(outdir, "report.html"), root)


def main():
    ap = argparse.ArgumentParser(
        prog="frm", description=__doc__.splitlines()[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", help="path or mount point to scan "
                    "(default: auto-detect external drive)")
    ap.add_argument("--list-drives", action="store_true",
                    help="list candidate drives and exit")
    ap.add_argument("--no-dedup", action="store_true",
                    help="skip MD5 content hashing")
    ap.add_argument("--no-songmatch", action="store_true",
                    help="skip format-agnostic song matching")
    ap.add_argument("--min-audio", type=int, default=MIN_AUDIO_DEFAULT,
                    help="minimum audio files for a Music dir to count as "
                         "a real collection (default: %(default)s)")
    ap.add_argument("--outdir", default=data_home(),
                    help="output directory (default: brenda data home)")
    ap.add_argument("--workers", type=int, default=min(os.cpu_count() or 2, 8))
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--open", action="store_true",
                    help="open the HTML report with xdg-open")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--version", action="version", version=f"frm {VERSION}")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if args.list_drives:
        drv = list_drives()
        if not drv:
            print("No candidate drives found.")
        for dev, mp, fs in drv:
            print(f"{dev}\t{mp}\t{fs}")
        return 0

    root = args.root
    if root is None:
        drv = list_drives()
        if not drv:
            print("No drive detected. Pass a path: frm.py /media/... ",
                  file=sys.stderr)
            return 1
        if len(drv) > 1:
            print("Multiple drives detected; pick one explicitly:\n",
                  file=sys.stderr)
            for dev, mp, fs in drv:
                print(f"  {mp}  ({fs})", file=sys.stderr)
            return 1
        root = drv[0][1]
        print(f"Auto-detected drive: {root}", file=sys.stderr)

    if not os.path.isdir(root):
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1
    if not os.access(root, os.R_OK | os.X_OK):
        print(f"No read permission on: {root}", file=sys.stderr)
        return 1

    cfg = {
        "dedup": not args.no_dedup,
        "songmatch": not args.no_songmatch,
        "workers": max(1, args.workers),
        "quiet": args.quiet,
        "min_audio": max(1, args.min_audio),
    }

    print(f"frm v{VERSION} — scanning {root}", file=sys.stderr)
    print(f"  dedup={cfg['dedup']} songmatch={cfg['songmatch']} "
          f"min_audio={cfg['min_audio']} workers={cfg['workers']}", file=sys.stderr)

    data = analyze(root, cfg)
    n = data["meta"]["n_music_dirs"]
    if n == 0:
        nf = data["meta"]["n_found"]
        if nf:
            print(f"Found {nf} directories named 'Music' under {root}, but "
                  "none passed the min-audio rail. Nothing to report.",
                  file=sys.stderr)
        else:
            print(f"No directories named 'Music' found under {root}. "
                  "Nothing to report.", file=sys.stderr)
        return 2

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    drive = os.path.basename(root.rstrip(os.sep)) or "root"
    outdir = os.path.join(args.outdir, "runs", f"{stamp}-{drive}")
    os.makedirs(outdir, exist_ok=True)
    html_path = export_run(data, root, outdir)

    # latest symlink
    try:
        latest = os.path.join(args.outdir, "latest")
        if os.path.islink(latest) or os.path.exists(latest):
            os.remove(latest)
        os.symlink(outdir, latest)
    except OSError:
        pass

    print("", file=sys.stderr)
    print(f"Done. {n} Music dirs, {data['meta']['elapsed_s']}s.", file=sys.stderr)
    print(f"Output: {outdir}", file=sys.stderr)
    print(f"  HTML:  {os.path.join(outdir, 'report.html')}", file=sys.stderr)
    print(f"  MD:    {os.path.join(outdir, 'report.md')}", file=sys.stderr)
    print(f"  JSON:  {os.path.join(outdir, 'report.json')}", file=sys.stderr)
    if args.open:
        os.system("xdg-open " + shlex_quote(html_path))
    return 0


def shlex_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    sys.exit(main())
