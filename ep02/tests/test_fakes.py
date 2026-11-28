"""Checks for the fakes, and that the copied engine runs a whole turn on them. Run: python3 tests/test_fakes.py"""
import contextlib
import io
import os
import shutil
import tempfile

from fakes import ENGINE, FakeClock, FakeModel, agent, call, say

ok = 0


def check(name, passed):
    global ok
    print(("ok   " if passed else "FAIL ") + name)
    assert passed, name
    ok += 1


def raises(kind, action):
    try:
        action()
    except kind:
        return True
    return False


with tempfile.TemporaryDirectory() as tmp:
    os.environ["AGENT_HOME"], os.environ["AGENT_MEMORY"] = tmp + "/home", tmp + "/mem"
    shutil.copytree(os.path.join(ENGINE, "project"), tmp + "/p")
    start = os.getcwd()
    os.chdir(tmp + "/p")
    agent.ROOT = os.path.realpath(tmp + "/p")

    model = FakeModel([call("read_file", {"path": "cart.py"}), say("It adds up prices.")])
    with contextlib.redirect_stdout(io.StringIO()):
        messages = agent.run("What does cart.py do?", model, "fake", perms=agent.Permissions())
    result = [b for m in messages if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"]]
    check("a tool call runs and its result goes back to the model",
          len(result) == 1 and result[0]["type"] == "tool_result" and "def " in result[0]["content"])
    check("the engine's decision is logged", any("allow" in str(entry) for entry in agent.run.log))
    check("the second request holds the tool result", "tool_result" in str(model.requests[1]["messages"]))

    model = FakeModel([call("run_bash", {"command": "touch made-by-the-model"}), say("I could not run it.")])
    with contextlib.redirect_stdout(io.StringIO()):
        agent.run("Make a file.", model, "fake", perms=agent.Permissions(deny=["run_bash(touch *)"]),
                  approver=lambda *a: "y")
    check("a denied command does not run", not os.path.exists("made-by-the-model"))
    check("the model is told it was denied", "denies this" in str(model.requests[1]["messages"]))

    model = FakeModel([say("done")])
    with contextlib.redirect_stdout(io.StringIO()):
        agent.run("Say done.", model, "fake", perms=agent.Permissions())
    check("a call past the end of the script fails", raises(AssertionError, lambda: model.create(messages=[])))
    check("an exception in the script is raised",
          raises(ConnectionError, lambda: FakeModel([ConnectionError("down")]).create(messages=[])))
    os.chdir(start)

clock = FakeClock(100)
clock.sleep(300)
check("sleep returns at once and moves the clock", clock.now() == 400)
clock.advance(8 * 3600)
check("advance moves the clock by any amount", clock.now() == 400 + 28800)
check("the clock cannot go backwards", raises(ValueError, lambda: clock.advance(-1)))

print("%d checks passed" % ok)
