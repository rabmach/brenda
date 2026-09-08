#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""brenda serve — the decision layer: a localhost-only helper that turns
scanned drives into a live, button-driven dashboard.

One page, always open: every scan run in the data home, newest first, each
collection with its compare numbers (already-have / variant / new) against
everything else brenda has ever indexed. Leave the browser open, keep
scanning drives — new runs appear on their own (the page re-checks every
few seconds). Actions need the drive mounted and take the scenic route:
plan (dry-run preview) -> confirm -> apply -> undo (or purge after review).

Safety model:
  - binds 127.0.0.1 on a random port; a random token gates every URL, so a
    random web page cannot poke the server (no cross-origin surprises)
  - mutations are POST-only, dry-run-planned first, confirmed in the browser
  - all file changes go through actions.py (quarantine-not-delete, undo
    manifests, journaled)
  - one mutation at a time; concurrent requests get a busy page

Requirements: Python 3.8+, standard library only.
"""

import argparse
import datetime
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import actions
import frm

# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.token = secrets.token_hex(16)
        self.url = None
        self.busy = threading.Lock()
        # live background-job status, shown as a banner on the dashboard:
        # stage None | "scan" | "compare"; label is the human line.
        self.status = {"stage": None, "label": "", "started": "",
                       "last": "", "last_at": ""}
        self.target = os.path.expanduser(
            f"~/Music/imported-{datetime.date.today():%Y%m%d}")
        state_file = os.path.join(frm.data_home(), "serve.state.json")
        self._state_file = state_file
        try:
            with open(state_file, encoding="utf-8") as f:
                d = json.load(f)
            self.target = d.get("target", self.target)
        except (OSError, json.JSONDecodeError):
            pass

    def job_start(self, stage, label):
        self.status.update(stage=stage, label=label,
                           started=datetime.datetime.now().strftime("%H:%M:%S"))

    def job_label(self, label):
        self.status["label"] = label

    def job_end(self, msg):
        self.status.update(stage=None, label="",
                           last=msg,
                           last_at=datetime.datetime.now().strftime("%H:%M:%S"))

    def save(self):
        """Persist target + live URL (chmod 600 — the URL carries the token)."""
        try:
            with open(self._state_file, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"target": self.target, "url": self.url}, f)
            os.chmod(self._state_file, 0o600)
        except OSError:
            pass

    def set_target(self, t):
        self.target = t
        self.save()


def _saved_url():
    """URL of a running server, if one was persisted."""
    try:
        with open(os.path.join(frm.data_home(), "serve.state.json"),
                  encoding="utf-8") as f:
            return json.load(f).get("url")
    except (OSError, json.JSONDecodeError):
        return None


STATE = None      # set in serve()
SERVER = None


def _runs():
    """All scan runs, newest first: {dir, id, meta, mounted, compare}."""
    runs_root = os.path.join(frm.data_home(), "runs")
    if not os.path.isdir(runs_root):
        return []
    out = []
    for name in sorted(os.listdir(runs_root), reverse=True):
        d = os.path.join(runs_root, name)
        rp = os.path.join(d, "report.json")
        if not os.path.isfile(rp):
            continue
        try:
            with open(rp, encoding="utf-8") as f:
                report = json.load(f)
        except json.JSONDecodeError:
            continue
        cp = os.path.join(d, "compare.json")
        comp = None
        if os.path.isfile(cp):
            try:
                with open(cp, encoding="utf-8") as f:
                    comp = json.load(f)
            except json.JSONDecodeError:
                comp = None
        fresh = False
        if comp:
            fresh = os.path.getmtime(cp) >= os.path.getmtime(rp)
        out.append({"id": name, "dir": d, "report": report,
                    "root": report["meta"]["root"],
                    "time": report["meta"]["time"],
                    "mounted": os.path.isdir(report["meta"]["root"]),
                    "compare": comp, "fresh": fresh})
    return out


def _collections(run):
    out = []
    for d in run["report"]["dirs"]:
        if d.get("kind") not in ("collection", "android", "ipod"):
            continue
        out.append({"path": d["path"], "short": frm.loc(d["path"], run["root"]),
                    "kind": d["kind"], "note": d.get("kind_note", ""),
                    "audio": d.get("audio_files", 0),
                    "bytes": d.get("audio_bytes", 0),
                    "other": d.get("other", 0)})
    return out


def _collection_events():
    """collection path -> lines that say what already happened to it, so the
    page itself announces 'something happened here' without a re-scan."""
    ev = {}
    for a in actions.list_actions(50):
        when = (a.get("applied") or a.get("created", ""))[5:16]
        coll = a.get("collection")
        root = a.get("root") or ""
        kind = a["kind"]
        if a["status"] == "planned":
            if coll:
                ev.setdefault(coll, []).append(
                    f"PENDING — a plan is waiting for your apply/cancel in "
                    f"'Your decisions': {_describe_plan(a, plain=True)}")
            continue
        if a["status"] != "applied":
            continue
        if kind == "merge":
            if coll:
                ev.setdefault(coll, []).append(
                    f"merged away {when} — this music no longer lives here "
                    "(re-scan the drive to refresh the listing)")
            pri = a.get("primary")
            if pri:
                ev.setdefault(pri, []).append(
                    f"received a merge {when} (+{a['counts'].get('merged', 0)} "
                    "files moved in)")
        elif kind == "quarantine" and coll:
            ev.setdefault(coll, []).append(
                f"quarantined off the drive {when} — the directory now lives "
                "in brenda quarantine on your LOCAL disk (undo moves it back, "
                "purge is separate)")
        elif kind == "import" and coll:
            mode = "moved" if a.get("mode") == "move" else "copied"
            ev.setdefault(coll, []).append(
                f"{mode} {a['counts'].get('tracks', 0)} track(s) to "
                f"{a.get('target', '?')} {when}")
        elif kind == "delete" and coll:
            ev.setdefault(coll, []).append(
                f"deleted {when} — permanently removed from the drive "
                "(every music file was verified to exist elsewhere first; "
                "no undo)")
    return ev


def _report_inner(run_dir):
    """Inner HTML of a run's report.html (body content, no <html> shell) so
    the dashboard can embed it — one merged page instead of two."""
    rp = os.path.join(run_dir, "report.html")
    if not os.path.isfile(rp):
        return None
    try:
        with open(rp, encoding="utf-8", errors="replace") as f:
            html = f.read()
    except OSError:
        return None
    start = html.find('<div class="wrap">')
    if start < 0:
        return None
    start += len('<div class="wrap">')
    end = html.rfind("</div></body>")
    if end <= start:
        return None
    return html[start:end]


# --------------------------------------------------------------------------
# background scan + compare
# --------------------------------------------------------------------------

def _ensure_compare(run_dir, against=None):
    """(Re)build compare.json for a run. against=None diffs against every
    indexed root; a root list = scan-to-scan compare against those only."""
    import compare
    idx_file = compare.index_path()
    idx = compare._load_index(idx_file)
    roots = [os.path.expanduser("~")]
    for r in idx["roots"]:
        if r not in roots and os.path.isdir(r):
            roots.append(r)
    for r in (against or []):
        if r not in roots:
            roots.append(r)          # not indexed yet: one-time build now
    index, _stats = compare.build_index(roots, idx_file, 4, quiet=True)
    run = compare.load_run(run_dir)
    results = compare.compare_run(run, index, only_roots=against)
    t = results["totals"]
    compare.render_json(results, run, index, os.path.join(run_dir, "compare.json"))
    compare.render_markdown(results, run, index, os.path.join(run_dir, "compare.md"))
    compare.render_html(results, run, index, os.path.join(run_dir, "compare.html"))
    against_txt = (", ".join(os.path.basename(r) for r in results["against"])
                   if against else "everything indexed")
    return (f"compare vs {against_txt}: {t['run_files']:,} on drive -> "
            f"{t['exact_files']:,} exact / {t['variant_files']:,} variant / "
            f"{t['new_files']:,} new")


def _scan_bg(mount):
    """Scan one drive in this (background) thread, then compare it.
    STATE.status carries the live stage so the dashboard banner shows what
    is happening (page refreshes every 5 s)."""
    drive_name = os.path.basename(mount.rstrip(os.sep)) or "root"
    def prog(done, total):
        STATE.job_label(f"scanning {drive_name} — hashing {done:,}/{total:,} files")
    cfg = {"dedup": True, "songmatch": True, "workers": 4, "quiet": True,
           "min_audio": frm.MIN_AUDIO_DEFAULT, "progress": prog}
    try:
        STATE.job_start("scan", f"scanning {drive_name} — walking music directories")
        data = frm.analyze(mount, cfg)
        if data["meta"]["n_music_dirs"] == 0:
            return f"scan of {drive_name} found nothing reportable"
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        outdir = os.path.join(frm.data_home(), "runs",
                              f"{stamp}-{drive_name}")
        os.makedirs(outdir, exist_ok=True)
        frm.export_run(data, mount, outdir)
        latest = os.path.join(frm.data_home(), "latest")
        if os.path.islink(latest) or os.path.exists(latest):
            os.remove(latest)
        os.symlink(outdir, latest)
        STATE.job_start("compare",
                        f"scan of {drive_name} done — comparing it against "
                        "your local music")
        try:
            return _ensure_compare(outdir)
        except Exception as e:                      # noqa: BLE001
            return f"scan ok ({outdir}) but compare failed: {e}"
    finally:
        STATE.status["stage"] = None


# --------------------------------------------------------------------------
# html helpers
# --------------------------------------------------------------------------

def _page(title, body, refresh=0):
    script = ""
    if refresh:
        # JS refresh instead of <meta http-equiv=refresh>. Polite rules:
        # reload ONLY while parked near the top of the page — scrolled down
        # into the report means reading, so the page leaves you alone (no
        # yank back to the top). Also waits while typing/choosing, 15 s
        # after any click, and never fires while a dialog is open. The
        # scroll position is saved before a reload and restored after, so
        # even a top-of-page reload does not move you.
        url = f"/{STATE.token}/" if STATE else "/"
        script = (""
                  '<script>'
                  'try{var y=sessionStorage.getItem("bY");'
                  'if(y!==null){window.scrollTo(0,+y);sessionStorage.removeItem("bY");}}catch(e){}'
                  'document.addEventListener("click",function(){window.bX=Date.now()},true);'
                  'document.addEventListener("change",function(){window.bX=Date.now()},true);'
                  'setTimeout(function tick(){'
                  'var a=document.activeElement;'
                  'var busy=a&&(a.tagName==="SELECT"||a.tagName==="INPUT"||a.tagName==="TEXTAREA");'
                  'var recent=window.bX&&(Date.now()-window.bX<15000);'
                  'var reading=window.scrollY>120;'
                  'if(!busy&&!recent&&!reading){'
                  'try{sessionStorage.setItem("bY",String(window.scrollY))}catch(e){}'
                  f'location.replace("{url}");}}'
                  'setTimeout(tick,5000);'
                  '},5000);'
                  '</script>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">{script}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{frm.esc(title)}</title>
<style>{frm.CSS}
.bar{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0}}
.bar input[type=text]{{flex:1;min-width:260px;background:var(--card2);
color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:14px}}
button,select{{background:var(--card2);color:var(--txt);
border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:13px;cursor:pointer}}
button:hover{{border-color:var(--acc)}}
button.warn{{background:#4a2323;border-color:#7a3030}}
button.go{{background:#1d3a4a;border-color:#2b6a8f}}
form{{display:inline;margin:0}}
.badge-ok{{background:#2e5e3a;color:#c9f0d3}}
.badge-off{{background:#5e4a2e;color:#f0dfc9}}
.mnt{{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:700;
text-transform:uppercase;letter-spacing:.03em}}
.act{{display:inline-flex;align-items:center;gap:8px;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:6px 10px}}
.act .mini{{font-size:12px;color:var(--dim);white-space:nowrap}}
.act label.mini{{display:inline-flex;align-items:center;gap:4px;cursor:pointer}}
.evline{{background:var(--card2);border-left:3px solid var(--acc);border-radius:6px;
padding:6px 10px;margin:4px 0;font-size:13px;color:var(--txt)}}
h2 small{{font-weight:400}}
details{{margin:6px 0}}
summary{{cursor:pointer;color:var(--dim)}}
.flash{{background:#1d3a4a;border:1px solid #2b6a8f;border-radius:8px;
padding:10px 14px;margin:10px 0}}
</style></head><body><div class="wrap">{body}
<footer>brenda serve — localhost only, token-gated. refresh: {frm.esc(datetime.datetime.now().strftime("%H:%M:%S"))}</footer>
</div></body></html>"""


def _flash(msg):
    return f'<div class="flash">{msg}</div>' if msg else ""


def _opts(pairs, selected=None):
    out = []
    for value, label in pairs:
        sel = " selected" if value == selected else ""
        out.append(f'<option value="{frm.esc(value)}"{sel}>{frm.esc(label)}</option>')
    return "".join(out)


def _coll_compare(run, coll_path):
    """(exact, variant, new) for one collection out of the run's compare."""
    if run["compare"]:
        for e in run["compare"]["per_collection"]:
            if e["path"] == coll_path:
                return (e.get("exact_files", 0), e.get("variant_files", 0),
                        e.get("new_files", 0))
    return None


def _action_forms(run, c, new_n, coll_pairs):
    """The three decisions per collection + open folder, as one row of
    forms. Absolute token URLs, so the identical HTML works both on the
    dashboard and injected into the served report page. Every control sits
    in a labeled pill, so nothing has to be guessed."""
    t = STATE.token
    rid = run["id"]
    alive = bool(run["mounted"] and os.path.isdir(c["path"]))
    disabled = "" if alive else "disabled "
    tgt = frm.esc(STATE.target)
    short = frm.esc(c["short"])
    nums = _coll_compare(run, c["path"])
    if new_n > 0:
        copy_btn = (f'<button {disabled}class="go" type="submit">'
                    f'copy {new_n} new to local</button>')
    elif nums is None:
        copy_btn = ('<button disabled type="button" title="no compare '
                    'results yet — run compare (dropdown up top per run) '
                    'to learn what is new">compare first — what is new is '
                    'unknown</button>')
    else:
        copy_btn = (f'<button disabled type="button" title="every music file '
                    f'here has a byte-identical copy elsewhere in your '
                    f'indexed music — nothing to copy. If the whole '
                    f'directory is redundant, use delete: {short}">'
                    f'all {c["audio"]:,} tracks already indexed</button>')
    return f"""
 <span class="act"><form method="post" action="/{t}/plan/import" onsubmit="return confirm('Plan import of {new_n} new file(s) into {tgt}? Dry run — nothing moves yet.')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  {copy_btn}
  <label class="mini"><input type="checkbox" name="move" value="1"> move instead</label></form>
  <small class="dim">&rarr; {tgt}</small></span>
 <span class="act"><small class="dim">merge <code>{short}</code> into:</small>
  <form method="post" action="/{t}/plan/merge" onsubmit="return confirm('Plan merge of THIS collection into the collection picked in the dropdown? Byte-identical files go to quarantine, unique files + cover art + playlists move into the primary. Junk (zips) never moves. Nothing moves yet — next you get a confirm page with an apply button.')">
  <input type="hidden" name="copy_run" value="{frm.esc(rid)}">
  <input type="hidden" name="copy" value="{frm.esc(c['path'])}">
  <select name="primary">{''.join(f'<option value="{frm.esc(p)}">{frm.esc(l)}</option>' for p, l in coll_pairs if p != c["path"])}</select>
  <button {disabled}type="submit">merge &rarr;</button></form></span>
 <span class="act"><form method="post" action="/{t}/plan/quarantine" onsubmit="return confirm('Plan quarantine of this collection? The directory MOVES off the drive into brenda quarantine on your LOCAL disk — reviewable, undo moves it back, nothing is deleted.')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <button {disabled}class="warn" type="submit">quarantine &rarr; local review folder</button></form></span>
 <span class="act"><form method="post" action="/{t}/plan/variants" onsubmit="return confirm('Plan variant cleanup of THIS collection? One copy per song: lossless beats lossy, then the bigger file. Extra versions move to quarantine — undoable, and purge only works while the kept version exists.')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <button {disabled}type="submit">variants: keep best per song</button></form></span>
 <span class="act"><form method="post" action="/{t}/plan/delete" onsubmit="return confirm('Plan DELETE of {short}? brenda first verifies every music file here still exists elsewhere; non-music files (art/playlists/zips) go too. PERMANENT — no undo.')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <button {disabled}class="warn" type="submit">delete: {short}</button></form></span>
 <span class="act"><a style="text-decoration:none" href="/{t}/open?path={urllib.parse.quote(c['path'])}"><button {disabled}type="button">open folder: {short}</button></a></span>"""


def _collection_block(run, c, coll_pairs, show_nums=True, events=None):
    """One collection: path + what-it-is + numbers + the action pills, plus
    lines announcing what already happened to this collection."""
    nums = _coll_compare(run, c["path"])
    new_n = nums[2] if nums else 0
    if nums and show_nums:
        comp = ("<th class='num' style='background:none'>have</th>"
                "<th class='num' style='background:none'>variant</th>"
                "<th class='num' style='background:none'>new</th>"
                f"<td class='num'>{nums[0]:,}</td>"
                f"<td class='num'>{nums[1]:,}</td>"
                f"<td class='num'><b>{nums[2]:,}</b></td>")
    elif show_nums:
        comp = ("<th class='num' style='background:none'>have</th>"
                "<th class='num' style='background:none'>variant</th>"
                "<th class='num' style='background:none'>new</th>"
                "<td class='num' colspan='3' class='dim'>—</td>")
    else:
        comp = ""
    other = c.get("other", 0)
    other_txt = (f"<td class='dim' title='zips, docs, unknown files — brenda "
                 f"never moves or deletes these'>{other:,} non-music "
                 "(stay put)</td>") if other else ""
    head = (f"<table><tr><td><code>{frm.esc(c['short'])}</code></td>"
            f"<td class='dim'>{frm.esc(c['note'])}</td>"
            f"<td class='num'>{c['audio']:,} audio</td>"
            f"<td class='num'>{frm.fmt_bytes(c['bytes'])}</td>"
            f"{other_txt}{comp}")
    if not show_nums:
        head += f"<td class='num'><b>{new_n:,}</b> new to you</td>"
    head += "</tr></table>"
    ev_html = ""
    if run["mounted"] and not os.path.isdir(c["path"]):
        ev_html += ('<div class="evline">directory no longer on disk — '
                    'deleted, moved or quarantined since this scan. '
                    'Re-scan the drive to refresh the listing.</div>')
    for line in (events or {}).get(c["path"], []):
        ev_html += f'<div class="evline">{frm.esc(line)}</div>'
    return (head + ev_html
            + f'<div class="bar">{_action_forms(run, c, new_n, coll_pairs)}</div>')


# --------------------------------------------------------------------------
# the actions ledger — human rows for "what did I click and what now?"
# --------------------------------------------------------------------------

def _describe_plan(plan, plain=False):
    """One plain sentence: what this action does (or did) and to what.
    plain=True gives untagged text for desktop notifications."""

    def B(s):
        return s if plain else f"<b>{frm.esc(s)}</b>"

    def C(s):
        return s if plain else f"<code>{frm.esc(s)}</code>"

    kind = plan["kind"]
    counts = plan.get("counts", {})
    root = plan.get("root") or ""
    coll = plan.get("collection")
    coll_short = frm.loc(coll, root) if coll else ""
    if kind == "import":
        mode = "move to local" if plan.get("mode") == "move" else "copy to local"
        dirs_txt = f" ({counts.get('whole_dirs', 0)} whole dir(s))" \
            if counts.get("whole_dirs") else ""
        return (f"{B(mode)} — {counts.get('tracks', 0):,} new-to-you "
                f"track(s){dirs_txt} from {C(coll_short)} into "
                f"{C(plan.get('target', '?'))}")
    if kind == "quarantine":
        return (f"{B('quarantine')} — move {C(coll_short)} off the drive "
                "into brenda quarantine on your LOCAL disk "
                "(~/.local/share/brenda/quarantine): reviewable, undo moves "
                "it back, nothing is deleted")
    if kind == "merge":
        pr_root = plan.get("primary_root", root)
        primary_short = frm.loc(plan.get("primary", ""), pr_root) \
            if plan.get("primary") else "?"
        swap = counts.get("variants_kept", 0)
        vq = counts.get("variants_quarantined", 0)
        var_txt = ""
        if swap or vq:
            var_txt = (f"; variants: {swap} better version(s) swapped in, "
                       f"{vq} lesser quarantined")
        return (f"{B('merge')} — move the music of {C(coll_short)} into "
                f"{C(primary_short)}: byte-identical files → quarantine, "
                f"unique files → moved in{var_txt}, the emptied dir removed")
    if kind == "delete":
        return (f"{B('delete')} — permanently remove {C(coll_short)} from "
                f"the drive ({counts.get('audio_files', 0):,} music + "
                f"{counts.get('non_music', 0):,} non-music file(s) — every "
                "music file was verified to exist elsewhere first; "
                "no undo)")
    if kind == "variants":
        return (f"{B('variants cleanup')} — keep the best version of each "
                f"song in {C(coll_short)} ({counts.get('songs', 0):,} "
                f"song(s) had 2+ versions; lossless beats lossy, then "
                f"bigger) — {counts.get('quarantined', 0):,} extra "
                "version(s) → quarantine")
    if kind == "dedupe":
        drive = os.path.basename(root.rstrip(os.sep)) or "drive"
        return (f"{B('dedupe')} — in the {C(drive)} scan: keep the first "
                "copy of every byte-identical file, quarantine the rest "
                f"({counts.get('quarantined', 0):,} file(s))")
    return kind if plain else frm.esc(kind)


def _notify(body, title="brenda"):
    """Desktop notification, per OS: notify-send (Linux/WSL), a native
    notification via osascript (macOS), a Wscript popup (Windows — it has
    no builtin toast API reachable from stdlib). Silent no-op when nothing
    works; the flash + Your-decisions table + journal are the real record."""
    try:
        if sys.platform.startswith("win"):
            body_ps = body.replace("'", "''")
            title_ps = title.replace("'", "''")
            ps = (f"$s=New-Object -ComObject Wscript.Shell;"
                  f"$s.Popup('{body_ps}',0,'{title_ps}',64)")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            body_os = body.replace("\\", "").replace('"', "")
            title_os = title.replace("\\", "").replace('"', "")
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{body_os}" '
                              f'with title "{title_os}"'],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        elif frm.in_wsl():
            # WSL2: forward to Windows — powershell.exe is on the WSL PATH
            body_ps = body.replace("'", "''").replace("`", "")
            title_ps = title.replace("'", "''").replace("`", "")
            ps = (f"$s=New-Object -ComObject Wscript.Shell;"
                  f"$s.Popup('{body_ps}',0,'{title_ps}',64)")
            subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["notify-send", "-a", "brenda", title, body],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except (OSError, FileNotFoundError):
        pass


def _purge_label(plan):
    """Short name of whose quarantined files a purge would delete: the
    merged-from / quarantined collection (or the drive, for dedupe)."""
    root = plan.get("root") or ""
    coll = plan.get("collection")
    if coll:
        return frm.loc(coll, root)
    return os.path.basename(root.rstrip(os.sep)) or "drive"


def _plan_details(plan):
    """Collapsible first ops, so a plan can be re-inspected later ('what was
    I about to do again?')."""
    ops = plan.get("ops", [])
    shown = ops[:6]
    lis = "".join(
        f"<li><code>{frm.esc(o['op'])}</code> "
        f"<code>{frm.esc(o.get('src', ''))}</code> &rarr; "
        f"<code>{frm.esc(o.get('dst', o.get('why', '')))}</code></li>"
        for o in shown)
    more = (f"<li class='dim'>… {len(ops) - len(shown)} more</li>"
            if len(ops) > len(shown) else "")
    return (f"<details><summary class='dim'>see the exact plan</summary>"
            f"<ul style='padding-left:18px'>{lis}{more}</ul></details>")


def _actions_section():
    acts = actions.list_actions(12)
    if not acts:
        return ""
    rows = ""
    for a in acts:
        st = a["status"]
        btns = ""
        res = a.get("result", {})
        note = ""
        if res.get("errors"):
            lis = "".join(f"<li>{frm.esc(e)}</li>" for e in res["errors"][:3])
            more = (f"<li class='dim'>… {len(res['errors']) - 3} more</li>"
                    if len(res["errors"]) > 3 else "")
            note = (f"<br><small class='warn'>{len(res['errors'])} op(s) "
                    f"FAILED — detail below and in <code>~/.local/share/"
                    f"brenda/actions/{frm.esc(a['id'])}.json</code>"
                    f"<ul style='padding-left:18px'>{lis}{more}</ul></small>")
        if st == "planned":
            badge = '<span class="mnt badge-off">waiting for you</span>'
            btns = (f"<form method='post' action='apply'>"
                    f"<input type='hidden' name='id' value='{a['id']}'>"
                    f"<button class='go' type='submit'>apply — do it now</button></form> "
                    f"<form method='post' action='cancel'>"
                    f"<input type='hidden' name='id' value='{a['id']}'>"
                    f"<button type='submit'>cancel — I changed my mind</button></form>")
        elif st == "applied":
            if a["kind"] == "delete":
                badge = ('<span class="mnt badge-off">done — permanent, '
                         'no undo</span>')
                btns = ""
            else:
                badge = '<span class="mnt badge-ok">applied — reversible</span>'
                btns = (f"<form method='post' action='undo'>"
                        f"<input type='hidden' name='id' value='{a['id']}'>"
                        f"<button type='submit'>undo — put it all back</button></form>")
            if a["kind"] in ("quarantine", "merge", "dedupe"):
                tgt = frm.esc(_purge_label(a))
                btns += (f" <form method='post' action='purge' "
                         f"onsubmit=\"return confirm('PURGE: permanently delete the quarantined copies of {tgt}? "
                         "brenda has verified every file still has a surviving copy elsewhere. "
                         "This CANNOT be undone — undo only works before the purge.')\">"
                         f"<input type='hidden' name='id' value='{a['id']}'>"
                         f"<button class='warn' type='submit'>purge: {tgt}</button></form>")
        elif st == "undone":
            badge = '<span class="mnt badge-ok">undone</span>'
        elif st == "discarded":
            badge = '<span class="mnt badge-off">discarded</span>'
        else:                                     # purged
            badge = '<span class="mnt badge-off">purged — quarantined files deleted</span>'
        when = frm.esc(a.get("created", "")[5:16])   # MM-DD HH:MM
        rows += (f"<tr><td><small>{when}</small></td>"
                 f"<td>{_describe_plan(a)}<br>{badge} "
                 f"<small class='dim'>{frm.esc(a['id'])}</small>{note} "
                 f"{_plan_details(a)}</td>"
                 f"<td>{btns}</td></tr>")
    return (f"""
<h2>Your decisions</h2>
<p class="sub">Everything you clicked, newest first — nothing is ever done
behind your back: a plan waits here until you apply it (or cancel it), an
applied action stays reversible until you purge it.</p>
<table><tr><th>when</th><th>what</th><th></th></tr>
{rows}</table>""")


# --------------------------------------------------------------------------
# the dashboard
# --------------------------------------------------------------------------

def dashboard(msg=""):
    runs = _runs()
    idx_files = 0
    import compare
    idx = compare._load_index(compare.index_path())
    idx_files = compare.index_count(idx)

    scan_opts = _opts([(mp, f"{mp} ({fs})") for _d, mp, fs in frm.list_drives()])
    coll_pairs = []
    for r in runs:
        for c in _collections(r):
            if not os.path.isdir(c["path"]):
                continue      # stale path from an older scan (renamed,
                              # moved, merged away) — never offer dead dirs
            coll_pairs.append((c["path"], c["short"]))
    merge_opts = _opts(coll_pairs)
    target = STATE.target

    parts = [f'<h1>brenda <small class="dim">— {len(runs)} scan run(s), '
             f'{idx_files:,} indexed local files</small></h1>']
    parts.append(_flash(msg))
    st = STATE.status
    if st.get("fail") and not msg.startswith("FAILED"):
        parts.append(f'<div class="flash" style="background:#3a1d1d;'
                     f'border-color:#8a3a3a"><b>Last problem</b> — this line '
                     f'stays until the next successful action: '
                     f'{frm.esc(st["fail"])}</div>')
    if st.get("stage"):
        parts.append(f'<div class="flash"><b>Working:</b> '
                     f'{frm.esc(st.get("label") or st["stage"])} '
                     f'(started {frm.esc(st["started"])}) — this page '
                     'refreshes itself; the run appears below when it lands'
                     '</div>')
    elif st.get("last"):
        parts.append(f'<p class="sub">last background job '
                     f'({frm.esc(st["last_at"])}): '
                     f'{frm.esc(st["last"])}</p>')
    parts.append(f"""
<div class="bar">
 <form method="post" action="scan" onsubmit="return confirm('Scan this drive now?')">
  <select name="mount">{scan_opts or '<option value="">no drives detected</option>'}</select>
  <button class="go" type="submit">scan + compare</button></form>
 <form method="post" action="target">
  <input type="text" name="target" value="{frm.esc(target)}" title="import target directory">
  <button type="submit">set import target</button></form>
 <form method="post" action="stop" onsubmit="return confirm('Stop the brenda server? (the button starts it again)')">
  <button class="warn" type="submit">stop server</button></form>
</div>""")
    parts.append(
        '<p class="sub">Per collection the numbers mean: <b>have</b> = '
        "byte-identical copies you already own · <b>variant</b> = same song, "
        "different format/encode · <b>new</b> = nothing like it in your local "
        'music. The three decisions: <b>copy new to local</b> = bring the '
        'missing tracks home · <b>merge into →</b> = collapse a duplicate '
        'into the keep-copy (identical files → quarantine, unique files move '
        'in) · <b>quarantine</b> = whole collection off the drive, reviewable. '
        'Everything goes plan → confirm → apply → undo.</p>')

    parts.append(_actions_section())

    events = _collection_events()
    first_report_shown = False
    for r in runs:
        rid = r["id"]
        mnt = ('<span class="mnt badge-ok">mounted</span>' if r["mounted"]
               else '<span class="mnt badge-off">unplugged</span>')
        parts.append(f"<h2>{frm.esc(os.path.basename(r['root']))}"
                     f" <small class='dim'>{frm.esc(r['root'])} · scanned {frm.esc(r['time'])}</small> {mnt}"
                     f" <small><a href='report/{frm.esc(rid)}'>full report</a></small></h2>")
        if not r["compare"] or not r["fresh"]:
            against_opts = ['<option value="">everything indexed</option>',
                            '<option value="__home__">the local home dir</option>']
            for other in runs:
                if other["id"] != rid:
                    against_opts.append(
                        f'<option value="{frm.esc(other["root"])}">'
                        f'{frm.esc(os.path.basename(other["root"]))} scan</option>')
            parts.append(f"""
<form method="post" action="compare">
<p class="sub">no compare results yet (or older than the report).
{'<span class="dim"> — drive can stay unplugged: cached indexes carry over</span>' if not r["mounted"] else ''}
<input type="hidden" name="run" value="{frm.esc(rid)}">
<select name="against">{''.join(against_opts)}</select>
<button class="go" type="submit">compare to …</button></p></form>""")
        for c in _collections(r):
            parts.append(_collection_block(r, c, coll_pairs, events=events))
        inner = _report_inner(r["dir"])
        if inner:
            open_tag = " open" if not first_report_shown else ""
            first_report_shown = True
            parts.append(f"<details{open_tag}>"
                         f"<summary>report — every Music directory found on "
                         f"this drive (full detail, fold away)</summary>"
                         f"{inner}</details>")

    dedupe_opts = _opts([(r["id"], os.path.basename(r["root"]) + " — " + r["id"])
                         for r in runs])
    t = STATE.token
    q_btn = ""
    if os.path.isdir(actions.quarantine_root()):
        q_btn = (f' <a style="text-decoration:none" '
                 f'href="/{t}/open?path={urllib.parse.quote(actions.quarantine_root())}">'
                 f'<button type="button">open quarantine (review before purge)'
                 f'</button></a>')
    parts.append(f"""
<div class="bar"><form method="post" action="plan/dedupe">
<select name="run">{dedupe_opts}</select>
<button class="warn" type="submit" onclick="return confirm('Plan dedupe of this run? (later byte-identical copies quarantined)')">plan dedupe</button></form>{q_btn}
</div>""")

    return _page("brenda dashboard", "\n".join(parts), refresh=5)


def confirm_page(plan):
    """Dry-run preview: the plan, the counts, the first ops, confirm/cancel."""
    ops = plan["ops"]
    shown = ops[:8]
    rows = "".join(
        f"<li><code>{frm.esc(o['op'])}</code> "
        f"<code>{frm.esc(o.get('src', ''))}</code><br>"
        f"<span class='dim'>&rarr; <code>{frm.esc(o.get('dst', o.get('why', '')))}</code></span></li>"
        for o in shown)
    more = f"<p class='dim'>… and {len(ops) - len(shown)} more</p>" if len(ops) > len(shown) else ""
    notes = "".join(f"<p class='warn'>{frm.esc(n)}</p>" for n in plan.get("notes", []))
    body = f"""
<h1>Confirm: {frm.esc(plan['kind'])} <small class="dim">{frm.esc(plan['id'])}</small></h1>
<p class="sub">{frm.esc(json.dumps(plan['counts']))} · run {frm.esc(os.path.basename(plan['run']))}</p>
{notes}
<ul style="padding-left:18px">{rows}</ul>{more}
<div class="bar">
 <form method="post" action="apply"><input type="hidden" name="id" value="{frm.esc(plan['id'])}">
  <button class="go" type="submit">apply — {frm.esc(json.dumps(plan['counts']))}</button></form>
 <a href="."><button type="button">cancel</button></a>
</div>
<p class="sub">Applying is journaled and reversible (undo). Purge — permanent
deletion of quarantined files — is a separate, post-review step.</p>"""
    return _page("confirm — brenda", body)


# --------------------------------------------------------------------------
# http handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):               # quiet-ish
        sys.stderr.write("serve: " + (fmt % args) + "\n")

    # -- plumbing ----------------------------------------------------------
    def _host_ok(self):
        """Defense in depth: only our own loopback Host header, and for
        POSTs an Origin (browsers send it on cross-origin posts) matching
        our own. A CSRFing web page cannot act without BOTH the token AND
        the right origin — belt to the token's suspenders."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            port = self.server.server_address[1]
            if origin.rstrip("/") != f"http://127.0.0.1:{port}":
                return False
        return True

    def _route(self):
        path = urllib.parse.urlparse(self.path).path
        return path.split("/")

    def _authed(self):
        parts = self._route()
        return len(parts) >= 2 and parts[1] == STATE.token

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

    def _send(self, html, code=200):
        raw = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, msg=""):
        if msg.startswith("<b>"):
            STATE.status["fail"] = None     # a success clears the last problem
        q = f"?msg={urllib.parse.quote(msg)}" if msg else ""
        self.send_response(303)
        self.send_header("Location", f"/{STATE.token}/{q}")
        self.end_headers()

    def _json(self, obj):
        raw = json.dumps(obj, indent=1, sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _guarded(self, fn):
        """Run a mutation under the busy lock; friendly 503 when busy."""
        if not STATE.busy.acquire(blocking=False):
            self._send(_page("brenda busy",
                             "<h1>Busy</h1><p class='sub'>another background "
                             "job is running — the dashboard banner shows "
                             "which. This page refreshes itself.</p>",
                             refresh=6), code=503)
            return
        try:
            fn()
        except Exception as e:                       # noqa: BLE001 — last net
            try:
                self._redirect(f"FAILED: {e}")
            except Exception:                        # noqa: BLE001
                pass
        finally:
            STATE.busy.release()

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            self._send(_page("brenda", "<h1>403</h1>"
                             "<p class='sub'>wrong origin.</p>"), code=403)
            return
        if not self._authed():
            self._send(_page("brenda", "<h1>404</h1><p class='sub'>nope.</p>"),
                       code=404)
            return
        parts = self._route()
        q = {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).items()}
        route = parts[2] if len(parts) > 2 else ""

        if route == "":
            self._send(dashboard(q.get("msg", "")))
        elif route == "actions":
            self._json({"actions": actions.list_actions(50)})
        elif route == "report" and len(parts) > 3:
            self._send_report(parts[3])
        elif route == "open":
            self._open(q.get("path", ""))
        else:
            self._send(_page("brenda", "<h1>404</h1>"), code=404)

    def _send_report(self, run_id):
        run_dir = os.path.join(frm.data_home(), "runs", run_id)
        rp = os.path.join(run_dir, "report.html")
        if not os.path.isfile(rp):
            self._send(_page("brenda", "<h1>no such run</h1>"), code=404)
            return
        with open(rp, encoding="utf-8", errors="replace") as f:
            html = f.read()
        back = (f'<div class="flash">classic report for <b>{frm.esc(run_id)}</b> '
                f'— <a href="../">back to the dashboard</a></div>')
        html = html.replace('<div class="wrap">', '<div class="wrap">' + back, 1)
        self._send(html)

    def _open(self, path):
        path = os.path.realpath(path)
        allowed = [actions.quarantine_root(), STATE.target]
        for r in _runs():
            allowed.append(os.path.realpath(r["root"]))
            for c in _collections(r):
                allowed.append(os.path.realpath(c["path"]))
        if not any(path == a or path.startswith(a + os.sep) for a in allowed
                   if os.path.isdir(a)):
            self._send(_page("brenda", "<h1>refused</h1><p class='sub'>path "
                             "not in scope</p>"), code=403)
            return
        if os.path.isdir(path):
            frm.gui_open(path)
            self._redirect(f"opened {path}")
        else:
            self._send(_page("brenda", "<h1>not a directory</h1>"), code=404)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        if not self._host_ok():
            self._send(_page("brenda", "<h1>403</h1>"
                             "<p class='sub'>wrong origin.</p>"), code=403)
            return
        if not self._authed():
            self._send(_page("brenda", "<h1>404</h1>"), code=404)
            return
        self._guarded(lambda: self._post_route())

    def _post_route(self):
        parts = self._route()
        route = "/".join(parts[2:])
        form = self._body()
        dh = frm.data_home()
        try:
            if route == "target":
                STATE.set_target(form["target"])
                self._redirect(f"import target set: {form['target']}")
            elif route == "scan":
                mount = form.get("mount", "")
                if STATE.status.get("stage"):
                    self._redirect("a background job is already running "
                                   "(see the banner at the top) — one at a "
                                   "time")
                    return
                if not os.path.isdir(mount):
                    self._redirect("no such mount")
                    return
                threading.Thread(target=_bg_with_msg, args=(mount,),
                                 daemon=True).start()
                self._redirect(f"scanning {mount} in the background — "
                               "watch the banner up top; the run appears "
                               "below when it lands")
            elif route == "compare":
                if STATE.status.get("stage"):
                    self._redirect("a background job is already running "
                                   "(see the banner at the top) — one at a "
                                   "time")
                    return
                run_dir = os.path.join(dh, "runs", form["run"])
                if form.get("against") == "__home__":
                    against = [os.path.expanduser("~")]
                elif form.get("against"):
                    against = [form["against"]]
                else:
                    against = None
                threading.Thread(target=_bg_compare,
                                 args=(run_dir, against),
                                 daemon=True).start()
                self._redirect(f"comparing {form['run']} — refresh is coming")
            elif route == "plan/import":
                plan = actions.plan_import(
                    os.path.join(dh, "runs", form["run"]),
                    form["collection"],
                    form.get("target") or STATE.target,
                    move=form.get("move") == "1")
                self._send(confirm_page(plan))
            elif route == "plan/quarantine":
                plan = actions.plan_quarantine(
                    os.path.join(dh, "runs", form["run"]),
                    form["collection"])
                self._send(confirm_page(plan))
            elif route == "plan/merge":
                plan = actions.plan_merge(
                    os.path.join(dh, "runs", form["copy_run"]),
                    form["primary"], form["copy"])
                self._send(confirm_page(plan))
            elif route == "plan/dedupe":
                plan = actions.plan_dedupe(
                    os.path.join(dh, "runs", form["run"]))
                self._send(confirm_page(plan))
            elif route == "plan/variants":
                plan = actions.plan_variants(
                    os.path.join(dh, "runs", form["run"]),
                    form["collection"])
                self._send(confirm_page(plan))
            elif route == "plan/delete":
                plan = actions.plan_delete(
                    os.path.join(dh, "runs", form["run"]),
                    form["collection"])
                self._send(confirm_page(plan))
            elif route == "apply":
                plan = actions.apply_plan(actions.load_plan(form["id"]))
                extra = ""
                extra_plain = ""
                if plan["result"]["errors"]:
                    first = plan["result"]["errors"][0]
                    n_more = len(plan["result"]["errors"]) - 1
                    extra = (f" — <b>{len(plan['result']['errors'])} op(s) "
                             f"FAILED</b>: {frm.esc(first[:180])}"
                             + (f" (+{n_more} more)" if n_more else "")
                             + f" — full detail: <code>~/.local/share/"
                               f"brenda/actions/{frm.esc(plan['id'])}.json</code>")
                    extra_plain = (f" — {len(plan['result']['errors'])} "
                                   f"op(s) FAILED: {first[:200]} (full "
                                   f"detail: ~/.local/share/brenda/actions/"
                                   f"{plan['id']}.json)")
                elif plan["kind"] in ("merge", "quarantine", "dedupe",
                                      "delete"):
                    extra = extra_plain = (" — re-scan the drive and its "
                                           "collections update")
                elif plan["kind"] == "import":
                    extra = f" — look in <code>{frm.esc(plan.get('target', ''))}</code>"
                    extra_plain = f" — look in {plan.get('target', '')}"
                self._redirect(f"<b>Done:</b> {_describe_plan(plan)}{extra}")
                verb = {"merge": "APPLIED MERGE",
                        "quarantine": "APPLIED QUARANTINE",
                        "dedupe": "APPLIED DEDUPE",
                        "import": "APPLIED IMPORT",
                        "variants": "APPLIED VARIANT CLEANUP",
                        "delete": "APPLIED DELETE — PERMANENT"}.get(
                    plan["kind"], "APPLIED")
                perm = ""
                if plan["kind"] == "delete":
                    perm = (" This is permanent: the directory is gone, "
                            "there is no undo.")
                _notify(f"you just did THIS: {_describe_plan(plan, plain=True)}{extra_plain}.\n"
                        + ("Nothing else was deleted. Reversible "
                           "with undo — see the brenda dashboard, Your "
                           "decisions." if plan["kind"] != "delete" else
                           "No undo exists for a delete.") + perm,
                        title=f"brenda — {verb}")
            elif route == "undo":
                plan = actions.undo(form["id"])
                self._redirect(f"<b>Undone:</b> {_describe_plan(plan)} — "
                               "everything is back where it was")
                _notify(f"you just did THIS: UNDO — {_describe_plan(plan, plain=True)}.\n"
                        "Everything is back where it was.",
                        title="brenda — UNDONE")
            elif route == "cancel":
                plan = actions.discard(form["id"])
                self._redirect(f"<b>Cancelled:</b> {_describe_plan(plan)} — "
                               "the plan was discarded, nothing had moved")
                _notify(f"you just did THIS: CANCELLED — {_describe_plan(plan, plain=True)}.\n"
                        "The plan was discarded; nothing had moved.",
                        title="brenda — CANCELLED")
            elif route == "purge":
                plan = actions.purge(form["id"])
                n = plan["result"].get("purged_files", 0)
                left = plan["result"].get("left_in_quarantine", 0)
                left_txt = (f" — {left} non-music file(s) (art/junk) left "
                            "untouched in quarantine" if left else "")
                self._redirect(f"<b>Purged:</b> {_describe_plan(plan)} — "
                               f"{n} quarantined music file(s) permanently "
                               "deleted (every one had a surviving copy "
                               f"elsewhere){left_txt}")
                _notify(f"you just did THIS: PURGE — {_describe_plan(plan, plain=True)}.\n"
                        f"{n} quarantined music file(s) permanently deleted — "
                        "each had a verified surviving copy elsewhere. "
                        "Non-music files (art/junk) were NOT deleted."
                        + (f" {left} remain in quarantine." if left else "")
                        + " This was the irreversible step.",
                        title="brenda — PURGED (permanent)")
            elif route == "stop":
                self._send(_page("brenda", "<h1>stopped</h1>"
                                 "<p class='sub'>you can close this tab.</p>"))
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
            else:
                self._redirect(f"unknown route: {route}")
        except Exception as e:                       # noqa: BLE001 — surface it
            STATE.status["fail"] = str(e)
            print(f"serve: FAILED {route}: {e}", file=sys.stderr)
            self._redirect(f"FAILED: {e}")

def _bg_with_msg(mount):
    try:
        msg = _scan_bg(mount)
        STATE.job_end(msg)
        STATE.status["fail"] = None      # a finished scan clears the problem
        _notify(msg, title="brenda — scan + compare done")
        print(f"serve: {msg}", file=sys.stderr)
    except Exception as e:                           # noqa: BLE001
        STATE.job_end(f"scan FAILED: {e}")
        STATE.status["fail"] = f"scan FAILED: {e}"
        _notify(f"scan FAILED: {e}", title="brenda — scan FAILED")
        print(f"serve: scan FAILED: {e}", file=sys.stderr)


def _bg_compare(run_dir, against=None):
    try:
        STATE.job_start("compare", "comparing — building/refreshing indexes")
        msg = _ensure_compare(run_dir, against)
        STATE.job_end(msg)
        STATE.status["fail"] = None
        _notify(msg, title="brenda — compare done")
        print(f"serve: {msg}", file=sys.stderr)
    except Exception as e:                           # noqa: BLE001
        STATE.job_end(f"compare FAILED: {e}")
        STATE.status["fail"] = f"compare FAILED: {e}"
        _notify(f"compare FAILED: {e}", title="brenda — compare FAILED")
        print(f"serve: compare FAILED: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def serve(port=None, no_open=False):
    global STATE, SERVER
    STATE = State()
    dh = frm.data_home()
    os.makedirs(os.path.join(dh, "runs"), exist_ok=True)
    pidfile = os.path.join(dh, "serve.pid")
    if os.path.isfile(pidfile):
        try:
            pid = int(open(pidfile, encoding="utf-8").read().strip())
            os.kill(pid, 0)
            # already running: open the live dashboard instead of failing
            url = _saved_url()
            if url:
                webbrowser.open(url)
                print(f"brenda serve already running (pid {pid}) — "
                      f"opened {url}", file=sys.stderr)
                return 0
            raise SystemExit(f"brenda serve already running (pid {pid}) — "
                             f"stop it first (dashboard has a stop button, "
                             f"or kill {pid})")
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    port = port or free
    SERVER = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    with open(pidfile, "w", encoding="utf-8", newline="\n") as f:
        f.write(str(os.getpid()))
    url = f"http://127.0.0.1:{port}/{STATE.token}/"
    STATE.url = url
    STATE.save()
    print(f"brenda serve: {url}", file=sys.stderr)
    print("(localhost only; token-gated; ctrl+c stops)", file=sys.stderr)
    if not no_open:
        webbrowser.open(url)
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVER.server_close()
        if os.path.isfile(pidfile):
            os.remove(pidfile)
        print("brenda serve: stopped", file=sys.stderr)
    return 0


def run_cli(argv=None):
    ap = argparse.ArgumentParser(
        prog="brenda serve",
        description="Live multi-drive dashboard with plan/apply/undo actions "
                    "(localhost-only, token-gated).")
    ap.add_argument("--port", type=int, default=None,
                    help="port (default: random free)")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the browser")
    args = ap.parse_args(argv)
    return serve(args.port, args.no_open)


def self_test():
    """e2e: run the real server on a random port against a throwaway data
    home, then drive it with urllib: dashboard renders, plan/apply/undo
    round-trip works, actions JSON is live."""
    import tempfile
    import shutil
    import urllib.request
    import threading
    import time
    print("brenda serve self-test (real server, throwaway data home)")
    tmp = tempfile.mkdtemp(prefix="brenda-serve-test-")
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'ok  ' if cond else 'FAIL'}] {name}")
        if not cond:
            ok = False

    try:
        os.environ["BRENDA_DATA_HOME"] = os.path.join(tmp, "data")
        drive = os.path.join(tmp, "drive")
        music = os.path.join(drive, "backups", "Music")
        os.makedirs(os.path.join(music, "Alpha"))
        for n, c in (("01 - One.mp3", b"ONE"), ("02 - Two.mp3", b"TWO"),
                     ("03 - Three.mp3", b"THREE")):
            with open(os.path.join(music, "Alpha", n), "wb") as f:
                f.write(c)
        cfg = {"dedup": True, "songmatch": True, "workers": 2, "quiet": True,
               "min_audio": 3}
        data = frm.analyze(drive, cfg)
        rundir = os.path.join(tmp, "data", "runs", "testdrive")
        os.makedirs(rundir)
        frm.export_run(data, drive, rundir)

        global STATE, SERVER
        STATE = State()
        SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = SERVER.server_address[1]
        th = threading.Thread(target=SERVER.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{port}/{STATE.token}"

        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return r.status, r.read().decode()

        def post(path, fields):
            data_ = urllib.parse.urlencode(fields).encode()
            req = urllib.request.Request(base + path, data=data_, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()

        code, html = get("/")
        check("dashboard 200", code == 200)
        check("dashboard lists the collection", "backups: Music" in html
              or "backups" in html)
        check("dashboard has import button", "import" in html)
        check("stop button present with zero actions", "stop server" in html)

        code, html = get(f"/report/testdrive")
        check("wrapped report 200", code == 200
              and "back to the dashboard" in html)

        coll = os.path.join(drive, "backups", "Music")
        # fabricate compare.json so plan/import has "new" files to work with
        with open(os.path.join(rundir, "compare.json"), "w", encoding="utf-8") as f:
            json.dump({"per_collection": [{"path": coll,
                                           "new": [os.path.join(coll, "Alpha",
                                                            "01 - One.mp3")],
                                           "new_files": 1}]}, f)
        code, html = post("/plan/import", {"run": "testdrive",
                                           "collection": coll,
                                           "target": os.path.join(tmp, "imp")})
        # plan endpoint returns a confirm page (or redirect on error)
        import actions as A
        acts = A.list_actions(1)
        check("import plan created", acts and acts[0]["kind"] == "import")
        pid = acts[0]["id"]
        code, _ = post("/apply", {"id": pid})
        check("apply 200", code == 200)
        check("file imported", os.path.isfile(
            os.path.join(tmp, "imp", "Alpha", "01 - One.mp3")))
        post("/undo", {"id": pid})
        check("undo removed the import",
              not os.path.exists(os.path.join(tmp, "imp", "Alpha")))

        code, js = get("/actions")
        check("actions JSON live", code == 200
              and '"import"' in js)

        # token gate: wrong token is a 404/401-class rejection
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/wrongtoken/", timeout=5)
            gated = False
        except urllib.error.HTTPError as e:
            gated = e.code in (401, 403, 404)
        check("wrong token rejected", gated)

        # hardening: foreign Host header rejected; cross-origin POST Origin
        # rejected; normal requests still pass
        req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                     headers={"Host": "evil.com"})
        try:
            urllib.request.urlopen(req, timeout=5)
            h_ok = False
        except urllib.error.HTTPError as e:
            h_ok = e.code == 403
        check("foreign Host header rejected", h_ok)
        req = urllib.request.Request(base + "/", data=b"", method="POST",
                                     headers={"Origin": "https://evil.com"})
        try:
            urllib.request.urlopen(req, timeout=5)
            o_ok = False
        except urllib.error.HTTPError as e:
            o_ok = e.code == 403
        check("cross-origin POST rejected", o_ok)
        code, _ = get("/")
        check("normal requests still pass", code == 200)
    finally:
        if SERVER:
            SERVER.shutdown()
        os.environ.pop("BRENDA_DATA_HOME", None)
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_cli())
