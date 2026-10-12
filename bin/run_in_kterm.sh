#!/bin/sh

KTERM_BIN="/mnt/us/extensions/kterm/bin/kterm.sh"
PYTHON_BIN="/usr/bin/python3"
FIXCOVERS_SCRIPT="/mnt/us/extensions/fixcovers/bin/fix_covers.py"

if [ ! -x "$KTERM_BIN" ]; then
    echo "KTerm introuvable ou non executable : $KTERM_BIN"
    echo "Appuyez sur Entree pour quitter."
    read _
    exit 1
fi

if [ ! -f "$FIXCOVERS_SCRIPT" ]; then
    echo "Script Fix Covers introuvable : $FIXCOVERS_SCRIPT"
    echo "Appuyez sur Entree pour quitter."
    read _
    exit 1
fi

exec "$KTERM_BIN" -c 0 -k 1 -e "$PYTHON_BIN $FIXCOVERS_SCRIPT"
