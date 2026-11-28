#!/bin/sh
# ./check.sh    checks with no key and no model (Python 3.9 and the anthropic package)
set -e
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
echo "== series 1 checks on the copied engine"
# not a pipe: "| tail" would hide a failure
out=$(python3 engine/test_repl.py </dev/null) || { echo "$out"; exit 1; }
echo "$out" | tail -1
echo "== fake model and fake clock"
python3 tests/test_fakes.py </dev/null
echo "== episode 2: the decision function"
python3 tests/test_policy.py </dev/null
echo "== episode 2: the policy through the service, the first try, what is still open"
python3 tests/test_service.py </dev/null
echo "all passed"
