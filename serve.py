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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import actions
import frm

# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.token = secrets.token_hex(16)
        self.busy = threading.Lock()
        self.target = os.path.expanduser(
            f"~/Music/imported-{datetime.date.today():%Y%m%d}")
        state_file = os.path.join(frm.data_home(), "serve.state.json")
        try:
            with open(state_file) as f:
                self.target = json.load(f).get("target", self.target)
        except (OSError, json.JSONDecodeError):
            pass
        self._state_file = state_file

    def set_target(self, t):
        self.target = t
        try:
            with open(self._state_file, "w") as f:
                json.dump({"target": t}, f)
        except OSError:
            pass


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
            with open(rp) as f:
                report = json.load(f)
        except json.JSONDecodeError:
            continue
        cp = os.path.join(d, "compare.json")
        comp = None
        if os.path.isfile(cp):
            try:
                with open(cp) as f:
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
                    "bytes": d.get("audio_bytes", 0)})
    return out


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
    """Scan one drive in this (background) thread, then compare it."""
    cfg = {"dedup": True, "songmatch": True, "workers": 4, "quiet": True,
           "min_audio": frm.MIN_AUDIO_DEFAULT}
    data = frm.analyze(mount, cfg)
    if data["meta"]["n_music_dirs"] == 0:
        return "scan found nothing reportable"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(frm.data_home(), "runs",
                          f"{stamp}-{os.path.basename(mount.rstrip(os.sep)) or 'root'}")
    os.makedirs(outdir, exist_ok=True)
    frm.export_run(data, mount, outdir)
    latest = os.path.join(frm.data_home(), "latest")
    if os.path.islink(latest) or os.path.exists(latest):
        os.remove(latest)
    os.symlink(outdir, latest)
    try:
        return _ensure_compare(outdir)
    except Exception as e:                          # noqa: BLE001
        return f"scan ok ({outdir}) but compare failed: {e}"


# --------------------------------------------------------------------------
# html helpers
# --------------------------------------------------------------------------

def _page(title, body, refresh=0):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">{meta}
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
            coll_pairs.append((c["path"], c["short"]))
    merge_opts = _opts(coll_pairs)
    target = STATE.target

    parts = [f'<h1>brenda <small class="dim">— {len(runs)} scan run(s), '
             f'{idx_files:,} indexed local files</small></h1>']
    parts.append(_flash(msg))
    parts.append(f"""
<div class="bar">
 <form method="post" action="scan" onsubmit="return confirm('Scan this drive now?')">
  <select name="mount">{scan_opts or '<option value="">no drives detected</option>'}</select>
  <button class="go" type="submit">scan + compare</button></form>
 <form method="post" action="target">
  <input type="text" name="target" value="{frm.esc(target)}" title="import target directory">
  <button type="submit">set import target</button></form>
</div>""")

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
            comp = ""
            new_n = 0
            if r["compare"]:
                for e in r["compare"]["per_collection"]:
                    if e["path"] == c["path"]:
                        t = e
                        new_n = t.get("new_files", 0)
                        comp = (f"<td class='num'>{t.get('exact_files', 0):,}</td>"
                                f"<td class='num'>{t.get('variant_files', 0):,}</td>"
                                f"<td class='num'><b>{t.get('new_files', 0):,}</b></td>")
                        break
            else:
                comp = "<td class='num' colspan='3' class='dim'>—</td>"
            disabled = "" if r["mounted"] else "disabled "
            parts.append(f"""
<table><tr><td><code>{frm.esc(c['short'])}</code></td>
<td class="num">{c['audio']:,} audio</td><td class="num">{frm.fmt_bytes(c['bytes'])}</td>
<th class="num" style="background:none">have</th>{comp}</tr></table>
<div class="bar">
 <form method="post" action="plan/import" onsubmit="return confirm('Plan import of {new_n} new file(s) into the target dir? (dry run — nothing moves yet)')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <button {disabled}type="submit">import {new_n} new</button></form>
 <form method="post" action="plan/quarantine" onsubmit="return confirm('Plan quarantine of this whole collection? (reversible — moved into brenda quarantine for review)')">
  <input type="hidden" name="run" value="{frm.esc(rid)}">
  <input type="hidden" name="collection" value="{frm.esc(c['path'])}">
  <button {disabled}class="warn" type="submit">quarantine collection</button></form>
 <form method="post" action="plan/merge" onsubmit="return confirm('Plan merge of this collection into the selected primary?')">
  <input type="hidden" name="copy_run" value="{frm.esc(rid)}">
  <input type="hidden" name="copy" value="{frm.esc(c['path'])}">
  <select name="primary">{''.join(f'<option value="{frm.esc(p)}">{frm.esc(l)}</option>' for p, l in coll_pairs if p != c["path"])}</select>
  <button {disabled}type="submit">merge into …</button></form>
 <a class="button" style="text-decoration:none" href="open?path={urllib.parse.quote(c['path'])}"><button {disabled}type="button">open folder</button></a>
</div>""")

    acts = actions.list_actions(12)
    if acts:
        rows = ""
        for a in acts:
            btns = ""
            if a["status"] == "planned":
                btns = (f"<form method='post' action='apply'>"
                        f"<input type='hidden' name='id' value='{a['id']}'>"
                        f"<button class='go' type='submit'>apply</button></form>")
            elif a["status"] == "applied":
                btns = (f"<form method='post' action='undo'>"
                        f"<input type='hidden' name='id' value='{a['id']}'>"
                        f"<button type='submit'>undo</button></form>")
                if a["kind"] != "import":
                    btns += (f" <form method='post' action='purge' "
                             f"onsubmit=\"return confirm('PERMANENTLY delete the quarantined files of {a['id']}? You had your review — this cannot be undone.')\">"
                             f"<input type='hidden' name='id' value='{a['id']}'>"
                             f"<button class='warn' type='submit'>purge</button></form>")
            res = a.get("result", {})
            err = f" ({len(res.get('errors', []))} errors)" if res.get("errors") else ""
            rows += (f"<tr><td><code>{frm.esc(a['id'])}</code></td>"
                     f"<td>{frm.esc(a['kind'])}</td><td>{frm.esc(a['status'])}</td>"
                     f"<td class='num'>{frm.esc(json.dumps(a['counts']))}</td>"
                     f"<td>{btns}{err}</td></tr>")
        parts.append(f"""
<h2>Actions</h2>
<p class="sub">plan → apply → undo (or purge after review). Every step journaled.</p>
<table><tr><th>id</th><th>kind</th><th>status</th><th class="num">ops</th><th></th></tr>
{rows}</table>""")
        parts.append("""
<div class="bar"><form method="post" action="plan/dedupe">
<select name="run">""" + _opts([(r["id"], os.path.basename(r["root"]) + " — " + r["id"]) for r in runs]) + """
</select><button class="warn" type="submit"
 onsubmit2="x" onclick="return confirm('Plan dedupe of this run? (later byte-identical copies quarantined)')">plan dedupe</button></form>
<form method="post" action="stop"><button class="warn" type="submit">stop server</button></form>
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
                             "<h1>Busy</h1><p class='sub'>another action is "
                             "running — the page refreshes itself.</p>",
                             refresh=3), code=503)
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
            subprocess.Popen(["xdg-open", path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self._redirect(f"opened {path}")
        else:
            self._send(_page("brenda", "<h1>not a directory</h1>"), code=404)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
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
                if not os.path.isdir(mount):
                    self._redirect("no such mount")
                    return
                threading.Thread(target=_bg_with_msg, args=(mount,),
                                 daemon=True).start()
                self._redirect(f"scanning {mount} in the background — "
                               "the page will show the new run when it lands")
            elif route == "compare":
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
                    form.get("target") or STATE.target)
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
            elif route == "apply":
                plan = actions.apply_plan(actions.load_plan(form["id"]))
                self._redirect(f"applied {plan['id']}: "
                               f"{frm.esc(json.dumps(plan['counts']))}"
                               + (" — ERRORS, see notes"
                                  if plan["result"]["errors"] else ""))
            elif route == "undo":
                plan = actions.undo(form["id"])
                self._redirect(f"undone {plan['id']}")
            elif route == "purge":
                plan = actions.purge(form["id"])
                self._redirect(f"purged {plan['id']}: "
                               f"{plan['result'].get('purged_files', 0)} files "
                               "deleted from quarantine")
            elif route == "stop":
                self._send(_page("brenda", "<h1>stopped</h1>"
                                 "<p class='sub'>you can close this tab.</p>"))
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
            else:
                self._redirect(f"unknown route: {route}")
        except Exception as e:                       # noqa: BLE001 — surface it
            self._redirect(f"FAILED: {e}")

def _bg_with_msg(mount):
    try:
        msg = _scan_bg(mount)
        print(f"serve: {msg}", file=sys.stderr)
    except Exception as e:                           # noqa: BLE001
        print(f"serve: scan FAILED: {e}", file=sys.stderr)


def _bg_compare(run_dir, against=None):
    try:
        print(f"serve: {_ensure_compare(run_dir, against)}", file=sys.stderr)
    except Exception as e:                           # noqa: BLE001
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
            pid = int(open(pidfile).read().strip())
            os.kill(pid, 0)
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
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    url = f"http://127.0.0.1:{port}/{STATE.token}/"
    print(f"brenda serve: {url}", file=sys.stderr)
    print("(localhost only; token-gated; ctrl+c stops)", file=sys.stderr)
    if not no_open:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        code, html = get(f"/report/testdrive")
        check("wrapped report 200", code == 200
              and "back to the dashboard" in html)

        coll = os.path.join(drive, "backups", "Music")
        # fabricate compare.json so plan/import has "new" files to work with
        with open(os.path.join(rundir, "compare.json"), "w") as f:
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
    finally:
        if SERVER:
            SERVER.shutdown()
        os.environ.pop("BRENDA_DATA_HOME", None)
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_cli())
