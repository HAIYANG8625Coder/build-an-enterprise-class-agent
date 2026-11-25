#!/bin/sh
# ./check.sh          checks with no key and no model (any machine with Python 3.9 and the anthropic package)
# ./check.sh probes   also try the sandboxes for real (macOS, Docker running, alpine:3.20 already pulled)
set -e
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
echo "== series 1 checks on the copied engine"
# not a pipe: "| tail" would hide a failure
out=$(python3 engine/test_repl.py </dev/null) || { echo "$out"; exit 1; }
echo "$out" | tail -1
echo "== fake model and fake clock"
python3 tests/test_fakes.py </dev/null
if [ "$1" = "probes" ]; then
  echo "== gaps in the series 1 engine"
  python3 tests/probes/engine_gaps.py </dev/null
  echo "== sandbox-exec"
  python3 tests/probes/seatbelt_probe.py </dev/null
  echo "== docker"
  python3 tests/probes/docker_probe.py </dev/null
fi
echo "all passed"
