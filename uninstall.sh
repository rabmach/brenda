#!/usr/bin/env bash
# brenda uninstaller — reverses install.sh.
#
#   ./uninstall.sh          remove the brenda/frm symlinks (restores any
#                           frm.bak-brenda-* backup)
#   ./uninstall.sh --purge  ALSO delete the brenda data home
#                           (~/.local/share/brenda — runs, index, playlists,
#                           bpm cache). Without --purge, your data survives
#                           and a reinstall picks it up again.
#
# Never touches ~/frm or ~/groovin — originals stay wherever you put them.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BIN="$HOME/bin"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/brenda"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { sed -n '2,11p' "$0"; exit 0; }

echo "brenda uninstaller"

for name in brenda frm; do
    link="$BIN/$name"
    if [ -L "$link" ] && [ "$(readlink -f "$link")" = "$REPO_DIR/brenda" ]; then
        rm "$link"
        echo "  removed $link"
    elif [ -e "$link" ]; then
        echo "  left $link alone (not ours)"
    fi
done

# restore the most recent frm backup, if any
restored=0
for bak in $(ls -1d "$BIN"/frm.bak-brenda-* 2>/dev/null | sort | tail -1 || true); do
    if [ ! -e "$BIN/frm" ]; then
        mv "$bak" "$BIN/frm"
        echo "  restored $bak -> $BIN/frm"
        restored=1
    fi
done
[ "$restored" = 0 ] && echo "  no frm backup to restore"

if [ "$PURGE" = 1 ]; then
    if [ -d "$DATA" ]; then
        rm -rf "$DATA"
        echo "  purged data home: $DATA"
    else
        echo "  no data home at $DATA"
    fi
else
    echo "  data home kept: $DATA (use --purge to delete)"
fi

echo "done. ~/frm and ~/groovin were never touched."
grep -q 'brenda PATH' "$HOME/.bashrc" 2>/dev/null && \
    echo "note: the PATH line in ~/.bashrc was left in place (harmless)."
