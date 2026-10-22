#!/bin/sh
# Lanceur de Goulot installé (~/.local/bin/goulot) : exécute la version active, jamais la copie de travail.
CURRENT="$HOME/Library/Application Support/Goulot/current"
if [ ! -x "$CURRENT/.venv/bin/python" ]; then
    echo "Goulot n'est pas installé : lancez « ./bin/goulot dev bootstrap » depuis la copie de travail." >&2
    exit 1
fi
PYTHONPATH="$CURRENT" exec "$CURRENT/.venv/bin/python" -P -m goulot "$@"
