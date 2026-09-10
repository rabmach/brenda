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
import time
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
        # token persists across server restarts (saved with the target) so
        # a running browser tab survives a reboot of the dashboard — no
        # more minting URLs that orphan your tabs into 404s after restart
        self.token = secrets.token_hex(16)
        self.port = None
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
            if d.get("token"):
                self.token = d["token"]
            if d.get("port"):
                self.port = int(d["port"])
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
        """Persist target + the live URL INCLUDING the token and port
        (chmod 600 — the URL carries the token). The token + port persist
        across restarts so browser tabs keep working."""
        try:
            with open(self._state_file, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"target": self.target, "url": self.url,
                           "token": self.token, "port": self.port}, f)
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
button:active{{transform:translateY(1px);filter:brightness(1.3)}}
.bar{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0}}
.bar input[type=text]{{flex:1;min-width:260px;background:var(--card2);
color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:14px}}
button,select{{background:var(--card2);color:var(--txt);
border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:13px;cursor:pointer}}
button:hover{{border-color:var(--acc)}}
button:active{{transform:translateY(1px);filter:brightness(1.3)}}
button.warn{{background:#4a2323;border-color:#7a3030}}
button.go{{background:#1d3a4a;border-color:#2b6a8f}}
form{{display:inline;margin:0}}
.badge-ok{{background:#2e5e3a;color:#c9f0d3}}
.badge-off{{background:#5e4a2e;color:#f0dfc9}}
.mnt{{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:700;
text-transform:uppercase;letter-spacing:.03em}}
.acts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px;margin:8px 0}}
.collbox{{background:var(--card2);border:1px solid var(--line);border-radius:12px;
padding:12px;margin:16px 0}}
.collbox table{{background:transparent}}
.collbox th{{background:transparent;font-size:13px}}
.collbox .act,.collbox .evline{{background:var(--card)}}
table.coll{{font-size:16px;line-height:1.45}}
table.coll th{{font-size:13px;letter-spacing:.04em}}
table.coll td code{{font-size:15px}}
table.coll td,table.coll th{{padding:9px 12px}}

.act{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 8px}}
.act form{{display:flex;flex-wrap:wrap;gap:5px;align-items:center}}
.act form button{{flex:1 1 auto;white-space:nowrap;font-size:12px;padding:7px 8px}}
.act select{{flex:1 1 170px;min-width:0;font-size:12px}}
.act .mini{{font-size:11px;color:var(--dim);white-space:nowrap}}
.act label.mini{{display:inline-flex;align-items:center;gap:4px;cursor:pointer}}
.evline{{background:var(--card2);border-left:3px solid var(--acc);border-radius:6px;
padding:6px 10px;margin:4px 0;font-size:13px;color:var(--txt)}}
h2 small{{font-weight:400}}
details{{margin:6px 0}}
summary{{cursor:pointer;color:var(--dim)}}
.flash{{background:#1d3a4a;border:1px solid #2b6a8f;border-radius:8px;
padding:10px 14px;margin:10px 0}}
details.oldrun>summary{{color:var(--dim);font-size:14px;padding:6px 0}}
details.oldrun{{border:1px solid var(--line);border-radius:8px;padding:4px 10px;
background:var(--card2);opacity:.75}}
</style></head><body><div class="wrap">{body}
<script>
document.addEventListener("submit",function(e){{
  var b=e.target.querySelector('button[type=submit]');
  if(!b)return;
  var t=b.textContent;
  setTimeout(function(){{b.disabled=true;b.textContent="working...";}},0);
  setTimeout(function(){{b.disabled=false;b.textContent=t;}},4000);
}},false);
</script>
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
    """The decision grid for one collection: five uniform cells — copy to
    local, merge into, quarantine, variants, delete. Absolute token URLs so
    the HTML also works inside the inlined report. open-folder lives on the
    collection name (a link, not another button)."""
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
 <div class="act"><form method="post" action="/{t}/plan/import" onsubmit="return confirm('Plan import of {new_n} new file(s) into {tgt}? Dry run — nothing moves yet.')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  {copy_btn}
  <label class="mini"><input type="checkbox" name="move" value="1"> move instead</label>
  <small class="dim">&rarr; {tgt}</small></form></div>
 <div class="act"><form method="post" action="/{t}/collcompare" onsubmit="return confirm('Collection-only compare: fast — uses scan-time hashes and the cached index, and updates just this card.')">
  <small class="dim">this collection vs:</small>
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <select name="against"><option value="home">the local home dir</option><option value="">everything indexed</option></select>
  <button type="submit">compare</button></form></div>
 <div class="act"><form method="post" action="/{t}/plan/merge" onsubmit="return confirm('Plan merge of THIS collection into the collection picked in the dropdown? Byte-identical files go to quarantine, unique files + cover art + playlists move into the primary. Junk (zips) never moves. Nothing moves yet — next you get a confirm page with an apply button.')">
  <small class="dim">merge <code>{short}</code> into:</small>
  <input type="hidden" name="copy_run" value="{frm.esc(rid)}">
  <input type="hidden" name="copy" value="{frm.esc(c['path'])}">
  <select name="primary">{''.join(f'<option value="{frm.esc(p)}">{frm.esc(l)}</option>' for p, l in coll_pairs if p != c["path"])}</select>
  <button {disabled}type="submit">merge &rarr;</button></form></div>
 <div class="act"><form method="post" action="/{t}/plan/quarantine" onsubmit="return confirm('Plan quarantine of this collection? The directory MOVES off the drive into brenda quarantine on your LOCAL disk — reviewable, undo moves it back, nothing is deleted.')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <button {disabled}class="warn" type="submit">quarantine &rarr; review folder</button></form></div>
 <div class="act"><form method="post" action="/{t}/plan/variants" onsubmit="return confirm('Plan variant cleanup of THIS collection? One copy per song: lossless beats lossy, then the bigger file. Extra versions move to quarantine — undoable, and purge only works while the kept version exists.')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <button {disabled}type="submit">variants — keep best per song</button></form></div>
 <div class="act"><form method="post" action="/{t}/plan/delete" onsubmit="return confirm('Plan DELETE of {short}? brenda first verifies every music file here still exists elsewhere; non-music files (art/playlists/zips) go too. PERMANENT — no undo.')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <button {disabled}class="warn" type="submit">delete: {short} — verified</button></form></div>"""


def _collection_block(run, c, coll_pairs, show_nums=True, events=None,
                      overlay=None):
    """One collection: a proper two-row table (header row + values row) so
    the compare numbers render as a real table instead of an inline soup,
    plus the action pills and event lines announcing what already happened
    to this collection. A per-collection overlay (coll-compare.json) wins
    over the run-wide numbers for this card, with a line explaining it."""
    nums = _coll_compare(run, c["path"])
    new_n = nums[2] if nums else 0
    overlay_line = ""
    if overlay and overlay.get("collection") == c["path"]:
        entry = (overlay.get("per_collection") or [{}])[0]
        nums = (entry.get("exact_files", 0), entry.get("variant_files", 0),
                entry.get("new_files", 0))
        new_n = nums[2]
        roots = ", ".join("~" if os.path.expanduser("~") == rr else rr
                          for rr in overlay.get("meta", {})
                          .get("index_roots", []))
        overlay_line = (f'<div class="evline">numbers = collection-only '
                        f'compare vs <b>{frm.esc(roots or "nothing")}</b> at '
                        f'{frm.esc(overlay.get("time", "?"))} — have '
                        f'{nums[0]:,} / variant {nums[1]:,} / new '
                        f'{nums[2]:,}; copy, move, variants and merge on '
                        'this card act on THIS list</div>')
    head = ('<table class="coll"><tr>'
            "<th>collection</th><th>what it is</th>"
            "<th class='num'>audio</th><th class='num'>size</th>"
            "<th class='num' title='zips, docs, unknown files — brenda never "
            "moves or deletes these'>non-music</th>"
            "<th class='num' title='byte-identical copies found in the "
            "compared index (see the run&apos;s compare vs line)'>have</th>"
            "<th class='num' title='same song, different bytes "
            "(format/encode) in the compared index'>variant</th>"
            "<th class='num' title='nothing like it exists in the compared "
            "index'>new</th></tr>"
            f"<tr><td><code>{frm.esc(c['short'])}</code> "
            f"<a class='dim' style='text-decoration:none' title='open this "
            f"folder' href='/{STATE.token}/open?path="
            f"{urllib.parse.quote(c['path'])}'>open &nearr;</a></td>"
            f"<td class='dim'>{frm.esc(c['note'])}</td>"
            f"<td class='num'>{c['audio']:,}</td>"
            f"<td class='num'>{frm.fmt_bytes(c['bytes'])}</td>"
            f"<td class='num'>{c.get('other', 0):,}</td>")
    if nums:
        head += (f"<td class='num'>{nums[0]:,}</td>"
                 f"<td class='num'>{nums[1]:,}</td>"
                 f"<td class='num'><b>{nums[2]:,}</b></td>")
    else:
        head += "<td class='num' colspan='3' class='dim'>— (run compare)</td>"
    if not show_nums:
        head += f"<td class='num'><b>{new_n:,}</b></td>"
    head += "</tr></table>"
    ev_html = ""
    if run["mounted"] and not os.path.isdir(c["path"]):
        ev_html += ('<div class="evline">directory no longer on disk — '
                    'deleted, moved or quarantined since this scan. '
                    'Re-scan the drive to refresh the listing.</div>')
    for line in (events or {}).get(c["path"], []):
        ev_html += f'<div class="evline">{frm.esc(line)}</div>'
    return ('<div class="collbox">' + head + overlay_line + ev_html
            + f'<div class="acts">{_action_forms(run, c, new_n, coll_pairs)}</div>'
            + '</div>')


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


def _variants_groups(plan):
    """For a variants plan: group its quarantine ops by keeper, yielding
    (keeper, [removed...]) pairs sorted by keeper path — the plain-words
    answer to 'what is brenda keeping?'."""
    groups = {}
    order = []
    for op in plan.get("ops", []):
        k = op.get("keeper")
        if not k:
            continue
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(op)
    return [(k, groups[k]) for k in order]


def _keeper_line(plan, keeper, removed_ops):
    """One human row: KEEP this file — FULL PATH disambiguation (the same
    song may live under two different artist folders) — with its measured
    quality, and the full paths of the lesser versions that would be
    quarantined. All file probing is defensive — a missing file (drive
    unplugged, moved since) renders as an honest note, never a crash."""
    root = plan.get("root") or ""
    gone = not os.path.isfile(keeper)
    q = actions._quality(keeper)
    kinds = {5: "lossless", 4: "ogg", 3: "m4a/aac", 2: "mp3", 1: ""}
    fmt = kinds.get(q[0], "?")
    cohesion = (f"its folder holds {q[2]} songs, " if q[2] else
                "single-track folder, ")
    art = "cover art ✓, " if q[3] else ""
    tags = f"{q[4]} tags" if q[4] else "no tags"
    size_txt = ""
    if gone:
        status = ("<small class='warn'>KEEPER FILE GONE — drive unmounted "
                  "or file moved since this plan was made</small>")
    else:
        status = ""
        bits = f"{fmt} · {q[1]:,} {'kbit/s' if q[0] not in (5,) and q[1] > 0 and q[1] < 1024000 or q[0] in (2,3,4) and q[1] > 8000 else 'bytes'}"
        size_txt = (f" · {frm.fmt_bytes(q[5])}") if q[0] != 5 else ""
        status = (f"<small class='dim'>({bits}{size_txt} · {cohesion}"
                  f"{art}{tags})</small>")
        status = f"<small class='dim'>({bits}{size_txt} · {art}{tags})</small>"
    removed = "".join(
        f"<li><code>{frm.esc(os.path.abspath(o.get('src', '?')))}</code>"
        f"</li>" for o in removed_ops)
    rel = frm.esc(os.path.abspath(keeper))
    return (f"<div class='evline'><b>KEEP</b> <code>{rel}</code> {status}"
            f"<ul style='padding-left:14px;margin:4px 0'>{removed}</ul></div>")


def _variants_plan_html(plan, limit=10):
    """Grouped keeper-first rendering of a variants plan."""
    groups = _variants_groups(plan)
    if not groups:
        return ""
    shown = []
    for k, group in groups[:limit]:
        try:
            shown.append(_keeper_line(plan, k, group))
        except Exception as e:              # noqa: BLE001 — render must live
            shown.append(f"<div class='evline'>keeper "
                         f"<code>{frm.esc(os.path.abspath(k))}</code> — "
                         f"current state: {frm.esc(str(e))}</div>")
    more = ""
    if len(groups) > limit:
        extra_songs = sum(len(g) for _k, g in groups[limit:])
        more = (f"<p class='sub'>&#8230; and {len(groups) - limit} more "
                f"song(s) with their extra versions"
                f"{f' ({extra_songs} files)' if extra_songs else ''} in the "
                "same shape.</p>")
    return "".join(shown) + more


def _plan_details(plan):
    """Collapsible first ops, so a plan can be re-inspected later ('what was
    I about to do again?'). Variants plans render keeper-first: what brenda
    is keeping, and the lesser versions it would quarantine."""
    ops = plan.get("ops", [])
    vh = _variants_plan_html(plan, limit=4)
    if plan["status"] not in ("planned", "applying"):
        # finished business: a compact line, not live file inspections —
        # keeper files on unmounted/purged drives must never flash
        # gone-notes here
        return ("<details><summary class='dim'>carried out — "
                f"{frm.esc(plan['status'])} (details in the journal)</summary>"
                f"<ul style='padding-left:18px'>"
                f"<li class='dim'>{frm.esc(_describe_plan(plan))}</li></ul>"
                f"</details>")
    vh = _variants_plan_html(plan, limit=4)
    if vh:
        return (f"<details><summary class='dim'>see the plan — keepers and "
                f"removes</summary>{vh}</details>")
    if plan["status"] in ("planned", "applying", "applied"):
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
    return (f"<ul style='padding-left:18px'>"
            f"<li class='dim'>{frm.esc(_describe_plan(plan))}</li></ul>")


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
            if a["kind"] == "import":
                btns += (f" <form method='post' action='close'>"
                         f"<input type='hidden' name='id' value='{a['id']}'>"
                         f"<button type='submit' title='the moves are final - drops the undo button from the ledger (the journal keeps the record)'>"
                         f"done — keep it</button></form>")
            if a["kind"] in ("quarantine", "merge", "dedupe", "variants"):
                tgt = frm.esc(_purge_label(a))
                btns += (f" <form method='post' action='purge' "
                         f"onsubmit=\"return confirm('PURGE: permanently delete the QUARANTINED copies of {tgt} "
                         "from ~/.local/share/brenda/quarantine. "
                         "The collection on the drive is NOT touched - it keeps the kept versions. "
                         "brenda verified every file still has a surviving copy elsewhere. "
                         "This CANNOT be undone - undo only works before the purge.')\">"
                         f"<input type='hidden' name='id' value='{a['id']}'>"
                         f"<button class='warn' type='submit' title='deletes the parked copies in ~/.local/share/brenda/quarantine - the drive collection is not touched'>"
                         f"purge quarantine: {tgt}</button></form>")
        elif st == "applying":
            badge = ('<span class="mnt badge-ok">applying now — progress on '
                     'the banner</span>')
            btns = ""
        elif st == "closed":
            badge = '<span class="mnt badge-ok">done — closed</span>'
            btns = ""
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
</div>""")
    parts.append(
        '<p class="sub">Per collection the numbers mean: <b>have</b> = '
        "byte-identical copies found in the compared index · <b>variant</b> "
        "= same song, different bytes · <b>new</b> = nothing like it in the "
        "compared index. What was compared is shown per run ('compare vs') "
        "— by default the home music plus every drive ever indexed, so a "
        "song on another backup drive counts as have. For what-is-in-"
        "~/Music-only numbers, run compare against the local home dir. The "
        'three decisions: <b>copy new to local</b> = bring the missing '
        'tracks home · <b>merge into →</b> = collapse a duplicate into the '
        'keep-copy (identical files → quarantine, unique files move in) · '
        '<b>quarantine</b> = whole collection off the drive, reviewable. '
        'Everything goes plan → confirm → apply → undo.</p>')

    parts.append(_actions_section())

    events = _collection_events()
    first_report_shown = False
    worked_on = {a.get("run") for a in actions.list_actions(50)
                 if a.get("status") == "applied"}
    seen_roots = set()
    for r in runs:
        rid = r["id"]
        mnt = ('<span class="mnt badge-ok">mounted</span>' if r["mounted"]
               else '<span class="mnt badge-off">unplugged</span>')
        run_parts = []
        run_parts.append(f"<h2>{frm.esc(os.path.basename(r['root']))}"
                         f" <small class='dim'>{frm.esc(r['root'])} · scanned {frm.esc(r['time'])}</small> {mnt}"
                         f" <small><a href='report/{frm.esc(rid)}'>full report</a></small></h2>")
        if r["compare"]:
            roots = r["compare"].get("meta", {}).get("index_roots", [])
            pretty = ", ".join(
                "~" if os.path.expanduser("~") == rr else rr for rr in roots)
            run_parts.append(f'<p class="sub">the compare numbers below are '
                             f'against: <b>{frm.esc(pretty or "everything indexed")}'
                             f'</b> — use "compare to …" above to change that</p>')
        if not r["compare"] or not r["fresh"]:
            against_opts = ['<option value="">everything indexed</option>',
                            '<option value="__home__">the local home dir</option>']
            for other in runs:
                if other["id"] != rid:
                    against_opts.append(
                        f'<option value="{frm.esc(other["root"])}">'
                        f'{frm.esc(os.path.basename(other["root"]))} scan</option>')
            run_parts.append(f"""
<form method="post" action="compare">
<p class="sub">no compare results yet (or older than the report).
{'<span class="dim"> — drive can stay unplugged: cached indexes carry over</span>' if not r["mounted"] else ''}
<input type="hidden" name="run" value="{frm.esc(rid)}">
<select name="against">{''.join(against_opts)}</select>
<button class="go" type="submit">compare to …</button></p></form>""")
        ov = None
        ovpath = os.path.join(r["dir"], "coll-compare.json")
        if os.path.isfile(ovpath):
            try:
                with open(ovpath, encoding="utf-8") as f:
                    ov = json.load(f)
            except (OSError, json.JSONDecodeError):
                ov = None
        for c in _collections(r):
            run_parts.append(_collection_block(r, c, coll_pairs, events=events,
                                               overlay=ov))
        inner = _report_inner(r["dir"])
        if inner:
            open_tag = " open" if not first_report_shown else ""
            first_report_shown = True
            run_parts.append(f"<details{open_tag}>"
                             f"<summary>report — every Music directory found on "
                             f"this drive (full detail, fold away)</summary>"
                             f"{inner}</details>")

        # fold runs you've acted on, and runs superseded by a newer scan of
        # the same drive — loading the page shows what is CURRENT
        superseded = r["root"] in seen_roots
        seen_roots.add(r["root"])
        if r["dir"] in worked_on or superseded:
            colls = _collections(r)
            n_audio = sum(c["audio"] for c in colls)
            why = ("actions were performed on its collections"
                   if r["dir"] in worked_on else
                   f"superseded by a newer scan of {frm.esc(os.path.basename(r['root']))}")
            summary = (f"<b>{frm.esc(os.path.basename(r['root']))}</b>"
                       f" <small class='dim'>· scanned {frm.esc(r['time'])} · "
                       f"{len(colls)} collection(s), {n_audio:,} audio at "
                       f"scan time · {why}</small> {mnt}")
            parts.append(f"<details class='oldrun'><summary>{summary}"
                         f"</summary>{''.join(run_parts)}</details>")
        else:
            parts.extend(run_parts)

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

    # the kill switch lives at the very bottom, far from daily buttons
    parts.append(f"""
<h2 style="border-bottom:none">Maintenance</h2>
<form method="post" action="stop" onsubmit="return confirm('Stop the brenda server? (the button starts it again)')">
 <button class="warn" type="submit">stop server</button>
 <small class="dim">stops the dashboard — restart with: brenda serve</small></form>""")

    return _page("brenda dashboard", "\n".join(parts), refresh=5)


def confirm_page(plan):
    """Dry-run preview: the plan, the counts, the ops (variants plans show
    keeper-first: what brenda is KEEPING and what it would quarantine),
    confirm/cancel."""
    ops = plan["ops"]
    vh = _variants_plan_html(plan, limit=14)
    if vh:
        listing = (f"<h4 style='margin-top:14px'>what brenda is "
                   f"KEEPING</h4>{vh}"
                   "<p class='sub'>each KEEP lists the lesser versions that "
                   "would move to quarantine.</p>")
    else:
        shown = ops[:8]
        rows = "".join(
            f"<li><code>{frm.esc(o['op'])}</code> "
            f"<code>{frm.esc(o.get('src', ''))}</code><br>"
            f"<span class='dim'>&rarr; <code>{frm.esc(o.get('dst', o.get('why', '')))}</code></span></li>"
            for o in shown)
        more = f"<p class='dim'>… and {len(ops) - len(shown)} more</p>" if len(ops) > len(shown) else ""
        listing = f"<ul style='padding-left:18px'>{rows}</ul>{more}"
    notes = "".join(f"<p class='warn'>{frm.esc(n)}</p>" for n in plan.get("notes", []))
    body = f"""
<h1>Confirm: {frm.esc(plan['kind'])} <small class="dim">{frm.esc(plan['id'])}</small></h1>
<p class="sub">{frm.esc(json.dumps(plan['counts']))} · run {frm.esc(os.path.basename(plan['run']))}</p>
{notes}
{listing}
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
            try:
                self._send(dashboard(q.get("msg", "")))
            except Exception as e:               # noqa: BLE001 — never blank
                self._send(_page(
                    "brenda — hiccup",
                    f"<h1>the dashboard hiccuped</h1>"
                    f"<p class='sub'>{frm.esc(str(e))}</p>"
                    "<p class='sub'>nothing was touched — restart the "
                    "server and the page comes back.</p>"), code=200)
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
                    if mount.startswith("\\\\.\\PHYSICALDRIVE"):
                        self._redirect("that disk holds a Linux filesystem - "
                                       "Windows cannot read it directly; "
                                       "run: brenda.cmd wslmount (elevated)")
                    else:
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
            elif route == "collcompare":
                if STATE.status.get("stage"):
                    self._redirect("a background job is already running "
                                   "(see the banner at the top) — one at a "
                                   "time")
                    return
                run_dir = os.path.join(dh, "runs", form["run"])
                coll = form["collection"]
                threading.Thread(target=_bg_coll_compare,
                                 args=(run_dir, coll,
                                       form.get("against") or None),
                                 daemon=True).start()
                self._redirect(f"collection-only compare of {coll} — watch "
                               "the banner; the card updates when it lands")
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
                probe = actions.load_plan(form["id"])
                if probe["status"] == "applying":
                    self._redirect("that action is already applying — "
                                   "watch the banner")
                    return
                if len(probe.get("ops", [])) > 60:
                    # big plan: run it in the background with live progress
                    probe["status"] = "applying"
                    actions._save(probe)
                    threading.Thread(target=_bg_apply,
                                     args=(form["id"],), daemon=True).start()
                    self._redirect(f"applying {len(probe['ops'])} ops in the "
                                   "background — the banner counts them "
                                   "down; the page keeps refreshing")
                    return
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
                # after an import/move, the card's numbers are stale - the
                # moved files are home now. Re-run the collection-only
                # compare in the background so New drops by what moved.
                if plan["kind"] == "import" and plan.get("run") \
                        and plan.get("collection"):
                    rd = plan["run"]
                    if os.path.isfile(os.path.join(rd, "coll-compare.json")):
                        threading.Thread(
                            target=_bg_coll_compare,
                            args=(rd, plan["collection"], "home"),
                            daemon=True).start()
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
            elif route == "close":
                plan = actions.close(form["id"])
                self._redirect(f"<b>Closed:</b> {_describe_plan(plan)} — "
                               "the moves are final; the ledger entry is "
                               "filed as done")
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

def _ensure_coll_compare(run_dir, collection, against=None):
    """Per-collection compare: fast — the drive side uses scan-time hashes,
    the local side the cached index. Writes a per-collection overlay
    (coll-compare.json) so one card's numbers can be collection-specific
    without re-running the whole drive compare."""
    import compare
    import datetime
    idx_file = compare.index_path()
    idx = compare._load_index(idx_file)
    roots = [os.path.expanduser("~")]
    if against != "home":
        for r in idx["roots"]:
            if r not in roots and os.path.isdir(r):
                roots.append(r)
    run = compare.load_run(run_dir)
    index, _stats = compare.build_index(roots, idx_file, 4, quiet=True)
    # only_roots scopes the diff to the roots built for THIS compare —
    # otherwise stale sections in the index (old drives) sneak back in
    results = compare.compare_run(run, index, only_roots=roots,
                                  collection=collection)
    t = results["totals"]
    overlay = {"collection": collection,
               "meta": {"index_roots": sorted(results.get("against", []))},
               "per_collection": results["per_collection"],
               "totals": t,
               "time": datetime.datetime.now().strftime("%H:%M:%S")}
    with open(os.path.join(run_dir, "coll-compare.json"), "w",
              encoding="utf-8") as f:
        json.dump(overlay, f, indent=1)
    pretty = ", ".join("~" if os.path.expanduser("~") == rr else rr
                       for rr in overlay["meta"]["index_roots"]) or "nothing"
    return (f"collection-only compare vs {pretty}: {t['run_files']:,} files "
            f"-> {t['exact_files']:,} have / {t['variant_files']:,} variant "
            f"/ {t['new_files']:,} new")


def _bg_coll_compare(run_dir, collection, against):
    try:
        STATE.job_start("compare", f"collection-only compare of {collection}")
        msg = _ensure_coll_compare(run_dir, collection, against)
        STATE.job_end(msg)
        STATE.status["fail"] = None
        print(f"serve: {msg}", file=sys.stderr)
    except Exception as e:                           # noqa: BLE001
        STATE.job_end(f"collection compare FAILED: {e}")
        STATE.status["fail"] = f"collection compare FAILED: {e}"
        print(f"serve: collection compare FAILED: {e}", file=sys.stderr)


def _bg_apply(action_id):
    """Background apply for big plans: holds the mutation lock, reports
    per-op progress on the banner, fires the notification when done."""
    got = False
    for _ in range(50):                    # wait out the POST's busy hold
        if STATE.busy.acquire(blocking=False):
            got = True
            break
        time.sleep(0.2)
    if not got:
        print("serve: apply could not start - busy", file=sys.stderr)
        return
    try:
        plan = actions.load_plan(action_id)
        total = len(plan.get("ops", []))
        STATE.job_start("apply", f"applying {action_id}: 0/{total} ops")
        plan = actions.apply_plan(
            plan, progress=lambda done, _t: STATE.job_label(
                f"applying {action_id}: {done:,}/{total:,} ops"),
            resume=True)
        res = plan.get("result", {})
        msg = (f"applied {plan['id']}: {res.get('done', 0):,} op(s) done, "
               f"{res.get('skipped', 0):,} skipped"
               + (f", {len(res.get('errors', []))} FAILED" if res.get("errors")
                  else ""))
        STATE.job_end(msg)
        if res.get("errors"):
            STATE.status["fail"] = (f"{len(res['errors'])} op(s) FAILED — "
                                    f"detail in brenda\\actions/"
                                    f"{plan['id']}.json" if sys.platform
                                    .startswith("win") else
                                    f"{len(res['errors'])} op(s) FAILED — "
                                    f"see ~/.local/share/brenda/actions/"
                                    f"{plan['id']}.json")
        else:
            STATE.status["fail"] = None
        _notify(msg, title=f"brenda — APPLIED {plan['kind'].upper()}")
        print(f"serve: {msg}", file=sys.stderr)
        if plan["kind"] == "import" and plan.get("run") \
                and plan.get("collection") \
                and os.path.isfile(os.path.join(plan["run"],
                                                "coll-compare.json")):
            threading.Thread(target=_bg_coll_compare,
                             args=(plan["run"], plan["collection"], "home"),
                             daemon=True).start()
    except Exception as e:                           # noqa: BLE001
        STATE.job_end(f"apply FAILED: {e}")
        STATE.status["fail"] = f"apply FAILED: {e}"
        _notify(f"apply FAILED: {e}", title="brenda — apply FAILED")
        print(f"serve: apply FAILED: {e}", file=sys.stderr)
    finally:
        STATE.busy.release()


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
    # same port across restarts when it's free — the saved URL keeps working
    # in every browser tab, forever
    port = STATE.port or free
    busy_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        busy_probe.bind(("127.0.0.1", port))
    except OSError:
        port = free                    # taken (another serve?) — fall back
    finally:
        busy_probe.close()
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

        # every card form posts with its hidden run/collection fields: a
        # missing field is KeyError 'run' - the bug the button-grid rewrite
        # planted and a real variants click caught
        code, html = post("/plan/variants",
                          {"run": "testdrive", "collection": coll})
        check("variants route intact (no missing-field error)",
              "FAILED: 'run'" not in html
              and "no multi-version songs" in html)
        code, html = post("/plan/quarantine",
                          {"run": "testdrive", "collection": coll})
        check("quarantine route intact", "FAILED: 'run'" not in html)
        acts = actions.list_actions(1)
        if acts and acts[0]["status"] == "planned":
            post("/cancel", {"id": acts[0]["id"]})
        code, html = post("/plan/delete",
                          {"run": "testdrive", "collection": coll})
        check("delete route intact", "FAILED: 'run'" not in html)

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
