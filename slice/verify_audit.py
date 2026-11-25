"""Checks the audit log of a service home. Run: python3 verify_audit.py <AGENTD_HOME>   (exit 1 if it does not check out)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "service"))
from audit import verify  # noqa: E402

if len(sys.argv) != 2:
    sys.exit(__doc__)
home = sys.argv[1]
result = verify(os.path.join(home, "audit"), os.path.join(home, "anchor"))
if result["ok"]:
    print("ok: %d lines, the chain is whole and ends at the anchor" % result["checked"])
    sys.exit(0)
print("BROKEN at line %s: %s" % (result["broken_at"], result["why"]))
sys.exit(1)
