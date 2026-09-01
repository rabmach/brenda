# brenda

**Find redundant music, compare drives, dig playlists.** Read-only triage for
the boxes where music actually lives.

brenda is three tools in one keyboard-friendly command:

| command | what it does |
|---|---|
| `brenda scan [DRIVE]` | finds every `Music` directory on a drive, classifies it
(collection vs android copy vs wine boilerplate vs cache…), hashes the audio,
finds byte-identical duplicates, detects whole-directory mirror copies, and
matches the *same songs* across collections even when the format differs
(FLAC vs MP3). Writes a pretty HTML report (+ Markdown + JSON) and opens it. |
| `brenda compare [RUN]` | diffs a scan run against your local music: what's
**already there** byte-for-byte, what exists as a **variant** (different
format/encode), and what's **new to you** — import candidates. |
| `brenda dig` | builds genre + BPM filtered playlists from the latest scan
**and your local music** (dedup prefers local copies — byte-identical drive
tracks point at your local file, so the playlist survives the drive being
unplugged; a same-song twin in a better format, e.g. drive FLAC vs local MP3,
wins its slot). BPM is cached by content hash, so file moves never invalidate
it, and v1.1's planned import buttons close the loop: import a new track
locally, re-dig, and its playlist path flips from the drive to home. |

The scanner inside brenda is **frm** (Find Redundant Music), and brenda
answers to that name too: everything `frm` did before still works —
`frm --open`, `frm --list-drives`, `frm /media/... --no-dedup`, the lot.

```sh
brenda drives                 # what's plugged in?
brenda scan --open            # scan the (only) external drive, open report
brenda compare                # vs your home music: exact / variant / new
brenda compare --local /media/you/bigdrive   # ...vs the big collection drive
brenda dig --bpm-min 85 --bpm-max 90 --max 100
brenda dig --local /media/you/bigdrive --genres soul --bpm-min 155 --bpm-max 160
```

## Install

```sh
git clone https://github.com/rabmach/brenda.git
cd brenda
./install.sh            # core: scan + compare. stdlib-only python3, no sudo
./install.sh --extras   # also apt-installs dig's deps (mutagen, aubio, ffmpeg)
```

User-level only: symlinks `~/bin/brenda` + `~/bin/frm`, creates the data
home, migrates any existing `~/frm/runs` and `~/groovin/bpm_cache.tsv`
(path-keyed → md5-keyed; your old BPM results survive). Idempotent — re-run
any time.

```sh
./uninstall.sh          # remove symlinks (restores any frm backup)
./uninstall.sh --purge  # also delete the data home
```

## Data home

Everything brenda produces lives in one place
(`XDG_DATA_HOME` or `~/.local/share`)/`brenda/`:

```
runs/<time>-<drive>/   report.{html,md,json} + hashes.tsv + file lists
                       (+ compare.{html,md,json} after a compare)
latest -> newest run   # what 'compare' and 'dig' read by default
index/local.json       # local-music MD5 index (compare's cache)
playlists/             # .m3u files from dig
bpm_cache.tsv          # md5-keyed BPM cache
```

## The read-only rule

Scans and compares **never write to the drive being scanned** — only
directory entries are read, and audio files *inside Music directories* are
opened (for hashing). Nothing is deleted, moved, or renamed by scan, compare
or dig. Files land only in brenda's own data home.

## The frm name

`~/bin/frm` points at the same dispatcher; invoked as `frm` it behaves
byte-for-byte like the original standalone tool. Existing scripts, aliases
and desktop buttons that call `frm` keep working untouched.

## george

If you run
[george](https://github.com/rabmach/george) (keyboard-first urwid
dashboard), these buttons slot right in:

```toml
{ label = "frm scan",   cmd = "frm --open",        term = true },
{ label = "frm drives", cmd = "frm --list-drives", term = true },
{ label = "compare",    cmd = "brenda compare",    term = true },
{ label = "dig",        cmd = "brenda dig",        term = true },
```

`term = true` keeps the scan chatter readable while `--open` pops the report
in your browser.

## Requirements

- Linux, python 3.8+ (standard library only for scan + compare)
- `dig` additionally needs `python3-mutagen` (always) and `python3-aubio` +
  `ffmpeg` (only to compute BPM for tracks not already in the cache — a
  fully-cached dig runs without them). `install.sh --extras` handles Debian;
  other distros' commands are printed on request.
- Any DE or WM — brenda is a terminal tool; the only "GUI" is `xdg-open`
  handing the report to your browser

## Privacy

Local only. No network access, no telemetry, no accounts, no cloud. Your
music collection's inventory stays on your disk.

## License

MIT — see [LICENSE](LICENSE).
