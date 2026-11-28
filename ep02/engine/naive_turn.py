"""The first try, kept to show what breaks: let the engine use the permission system series 1 already had.

It loads the rules from the project's .agent/permissions.json (and the user's), loads the project's hooks, and decides
for itself. Mode accept-edits, so that it can change code. Nobody is there to answer "ask", so "ask" means no."""
import json
import os
import sys

import agent
import turn


def main():
    job = json.load(sys.stdin)
    out = os.fdopen(os.dup(1), "w")          # the result goes here; anything else that prints goes to stderr
    os.dup2(2, 1)
    agent.ROOT = os.path.realpath(os.getcwd())
    try:
        result = turn.one_turn(turn.client(), job, agent.Permissions.load(mode="accept-edits"), agent.Hooks.load())
    except Exception as e:
        result = {"error": "%s: %s" % (type(e).__name__, str(e)[:300])}
    json.dump(result, out)
    out.close()


if __name__ == "__main__":
    main()
