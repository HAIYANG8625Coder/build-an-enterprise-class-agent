#!/bin/sh
# Every check, no key and no model. On macOS the sandbox checks run too; elsewhere they are reported as skipped.
set -e
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
echo "== series 1 checks on the engine"
out=$(python3 engine/test_repl.py </dev/null) || { echo "$out"; exit 1; }
echo "$out" | tail -1
echo "== service checks"
python3 -m unittest discover -s tests -v </dev/null
