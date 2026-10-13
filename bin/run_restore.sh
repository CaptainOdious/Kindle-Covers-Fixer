#!/bin/sh

KTERM_BIN="/mnt/us/extensions/kterm/bin/kterm"
PYTHON_BIN="/mnt/us/python3/python/bin/python3"
FIXCOVERS_SCRIPT="/mnt/us/extensions/fix_covers/bin/restore_covers.py"

if [ ! -x "$KTERM_BIN" ]; then
    echo "KTerm not found or not executable: $KTERM_BIN"
    echo "Press Enter to exit."
    read _
    exit 1
fi

if [ ! -f "$FIXCOVERS_SCRIPT" ]; then
    echo "Fix Covers script not found: $FIXCOVERS_SCRIPT"
    echo "Press Enter to exit."
    read _
    exit 1
fi

exec "$KTERM_BIN" -c 0 -k 1 -e "$PYTHON_BIN $FIXCOVERS_SCRIPT"
