"""What the series 1 engine does today that a service must change. No API key, no model.
Run: python3 tests/probes/engine_gaps.py

Each line says "shown" when the gap is there. A later episode closes it, and then turns the line into a test."""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from fakes import FakeModel, agent, call, say  # noqa: E402

results = []


def shown(name, there):
    results.append(there)
    print(("shown      " if there else "NOT shown  ") + name)


def one_turn(tool, args, perms, hooks=None):
    model = FakeModel([call(tool, args), say("done")])
    with contextlib.redirect_stdout(io.StringIO()):
        agent.run("task", model, "fake", perms=perms, hooks=hooks, approver=lambda *a: "n", notes="")
    return str(model.requests[1]["messages"])


with tempfile.TemporaryDirectory() as made:
    root = os.path.realpath(made)
    os.environ["AGENT_HOME"], os.environ["AGENT_MEMORY"] = root + "/home", root + "/mem"
    os.makedirs(root + "/repo/.agent")
    start = os.getcwd()
    os.chdir(root + "/repo")
    agent.ROOT = root + "/repo"
    with open("secret.txt", "w") as f:
        f.write("TOP-SECRET-LINE")
    with open(".agent/hooks.json", "w") as f:
        json.dump({"hooks": [{"event": "PreToolUse", "command": "touch made-by-a-hook"}]}, f)
    with open(".agent/permissions.json", "w") as f:
        json.dump({"allow": ["run_bash"]}, f)

    one_turn("read_file", {"path": "secret.txt"}, agent.Permissions(deny=["run_bash"]), agent.Hooks.load())
    shown("a hook file in the repository runs a command, although a rule denies all commands",
          os.path.exists("made-by-a-hook"))
    one_turn("run_bash", {"command": "touch allowed-by-the-repo"}, agent.Permissions.load())
    shown("an allow rule in the repository lets a command run without asking", os.path.exists("allowed-by-the-repo"))
    seen = one_turn("grep", {"pattern": "TOP-SECRET"}, agent.Permissions(deny=["read_file(secret.txt)"]))
    shown("a rule that denies reading a file does not stop grep from showing its lines", "TOP-SECRET-LINE" in seen)
    shown("a rule written as Bash(touch *), as in the design, matches nothing",
          agent.Permissions(deny=["Bash(touch *)"]).check("run_bash", {"command": "touch x"})[0] != "deny")
    os.chdir(start)

print("%d of %d gaps shown" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
