#!/usr/bin/env bash
# brenda installer — user-level, no sudo needed for the core.
#
#   ./install.sh            core: scan + compare (pure stdlib python3)
#   ./install.sh --extras   also install dig's deps via apt when possible
#                           (python3-mutagen, python3-aubio, ffmpeg)
#
# What it does:
#   - symlinks ~/bin/brenda and ~/bin/frm to this repo's dispatcher
#     (an existing ~/bin/frm is backed up as ~/bin/frm.bak-brenda-<ts>)
#   - creates the data home (XDG_DATA_HOME or ~/.local/share)/brenda/
#   - copies ~/frm/runs/* into the data home (originals untouched) and
#     seeds the 'latest' symlink
#   - migrates ~/groovin/bpm_cache.tsv (path-keyed) to the md5-keyed cache
#     via `brenda migrate-cache` (idempotent, never overwrites)
#   - ~/bin is appended to PATH via ~/.bashrc if missing
#
# Idempotent: re-running is always safe. Uninstall with ./uninstall.sh.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BIN="$HOME/bin"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/brenda"
EXTRAS=0
[ "${1:-}" = "--extras" ] && EXTRAS=1
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { sed -n '2,16p' "$0"; exit 0; }

echo "brenda installer"
echo "  repo:  $REPO_DIR"
echo "  bin:   $BIN"
echo "  data:  $DATA"

# --- preflight -------------------------------------------------------------
command -v python3 >/dev/null || { echo "FATAL: python3 not found"; exit 1; }
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "FATAL: python3 >= 3.8 required"
    exit 1
fi
python3 -m py_compile "$REPO_DIR/brenda" "$REPO_DIR/frm.py" \
    "$REPO_DIR/compare.py" "$REPO_DIR/grooves.py" \
    || { echo "FATAL: repo code failed to compile"; exit 1; }
echo "  python3 OK ($(python3 -c 'import sys; print(sys.version.split()[0])'))"

# --- ~/bin + PATH ----------------------------------------------------------
mkdir -p "$BIN"
case ":$PATH:" in
    *":$BIN:"*) : ;;
    *) touch "$HOME/.bashrc"
       if ! grep -q 'brenda PATH' "$HOME/.bashrc"; then
           cp "$HOME/.bashrc" "$HOME/.bashrc.bak-brenda" 2>/dev/null || true
           printf '\n# brenda PATH\nexport PATH="$HOME/bin:$PATH"\n' >> "$HOME/.bashrc"
           echo "  added $BIN to PATH via ~/.bashrc (backup: ~/.bashrc.bak-brenda)"
           echo "  NOTE: current shells need:  export PATH=\"\$HOME/bin:\$PATH\""
       fi ;;
esac

# --- symlinks (frm compat included) ----------------------------------------
ln -sfn "$REPO_DIR/brenda" "$BIN/brenda"
if [ -e "$BIN/frm" ] || [ -L "$BIN/frm" ]; then
    if [ "$(readlink -f "$BIN/frm")" != "$REPO_DIR/brenda" ]; then
        bak="$BIN/frm.bak-brenda-$(date +%Y%m%d-%H%M%S)"
        mv "$BIN/frm" "$bak"
        echo "  backed up existing frm -> $bak"
    fi
fi
ln -sfn "$REPO_DIR/brenda" "$BIN/frm"
echo "  linked: $BIN/brenda, $BIN/frm -> $REPO_DIR/brenda"

# --- data home + runs migration --------------------------------------------
mkdir -p "$DATA/runs" "$DATA/index" "$DATA/playlists"
copied=0
if [ -d "$HOME/frm/runs" ]; then
    for d in "$HOME/frm/runs"/*; do
        [ -d "$d" ] || continue
        tgt="$DATA/runs/$(basename "$d")"
        if [ ! -e "$tgt" ]; then
            cp -a "$d" "$tgt"
            copied=$((copied + 1))
        fi
    done
    echo "  runs migrated: $copied copied from ~/frm/runs (originals kept)"
fi
if [ ! -e "$DATA/latest" ]; then
    newest="$(find "$DATA/runs" -mindepth 1 -maxdepth 1 -type d | sort | tail -1 || true)"
    if [ -n "$newest" ]; then
        ln -sfn "$newest" "$DATA/latest"
        echo "  latest -> $(basename "$newest")"
    fi
fi

# --- bpm cache migration (path-keyed -> md5-keyed) -------------------------
if [ -f "$HOME/groovin/bpm_cache.tsv" ]; then
    echo "  migrating groovin bpm cache..."
    "$REPO_DIR/brenda" migrate-cache || echo "  (cache migration skipped — run 'brenda migrate-cache' later)"
fi

# --- dig deps (optional) ---------------------------------------------------
if [ "$EXTRAS" = 1 ]; then
    if python3 -c 'import mutagen, aubio' 2>/dev/null; then
        echo "  dig deps already present"
    elif command -v apt-get >/dev/null; then
        echo "  installing dig deps: python3-mutagen python3-aubio ffmpeg"
        if sudo -n true 2>/dev/null; then
            sudo apt-get install -y python3-mutagen python3-aubio ffmpeg \
                || echo "  (apt install failed — dig needs these; core is unaffected)"
        else
            echo "  no passwordless sudo — run this yourself:"
            echo "    sudo apt install python3-mutagen python3-aubio ffmpeg"
        fi
    else
        echo "  no apt-get found — dig deps for other distros:"
        echo "    fedora: sudo dnf install python3-mutagen python3-aubio ffmpeg"
        echo "    arch:   sudo pacman -S python-mutagen python-aubio ffmpeg"
    fi
fi

# --- verify ----------------------------------------------------------------
echo
echo "installed:"
"$BIN/brenda" version
echo "  frm -> $BIN/frm -> $(readlink -f "$BIN/frm")"
echo "      (resolves via PATH as: $(command -v frm || echo 'after shell refresh'))"
echo "  data home: $DATA"
if [ -d "$HOME/.config/george" ]; then
    echo
    echo "george buttons (frm ones likely already exist):"
    echo '  { label = "frm scan",  cmd = "frm --open", term = true },'
    echo '  { label = "frm drives", cmd = "frm --list-drives", term = true },'
    echo '  { label = "compare",   cmd = "brenda compare", term = true },'
    echo '  { label = "dig",       cmd = "brenda dig", term = true },'
fi
echo
echo "try it:  brenda drives && brenda scan --open"
