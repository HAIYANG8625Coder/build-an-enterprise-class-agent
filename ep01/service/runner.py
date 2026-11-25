"""One process per session: each message starts the engine in a fresh process, in that user's project folder."""
import importlib.util
import json
import os
import subprocess
import sys

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")
TURN_SECONDS = 600


def sdk_folder():
    return os.path.dirname(os.path.dirname(importlib.util.find_spec("anthropic").origin))


class ProcessRunner:
    def __init__(self, model, model_url, model_key):
        self.model, self.model_url, self.model_key = model, model_url, model_key

    def environment(self, user_dir):
        """Built from nothing. Whatever is set in the service's own shell (ANTHROPIC_BASE_URL,
        ANTHROPIC_AUTH_TOKEN, ...) does not reach the engine. The engine still holds the model key: episode 5."""
        return {"PATH": "/usr/bin:/bin", "HOME": user_dir, "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join([ENGINE, sdk_folder()]),
                "AGENT_HOME": os.path.join(user_dir, ".agent"),
                "AGENT_MEMORY": os.path.join(user_dir, ".agent", "memory"),
                "MODEL_URL": self.model_url, "MODEL_KEY": self.model_key}

    def run(self, user_dir, project, session_file, task):
        job = {"model": self.model, "task": task, "session_file": session_file}
        try:
            done = subprocess.run([sys.executable, os.path.join(ENGINE, "turn.py")], input=json.dumps(job),
                                  cwd=project, env=self.environment(user_dir), capture_output=True, text=True,
                                  timeout=TURN_SECONDS)
            return json.loads(done.stdout)
        except subprocess.TimeoutExpired:
            return {"error": "the turn took too long"}
        except ValueError:
            sys.stderr.write(done.stderr[-2000:])
            return {"error": "the engine failed"}
