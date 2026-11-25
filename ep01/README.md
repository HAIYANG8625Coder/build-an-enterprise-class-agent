# ep01: many users, one agent service

A thin service in front of the series 1 agent: **who is asking** (a token), **where they work** (a folder per user) and **which conversation** (a saved session), with **one process per session**.

It is not safe yet. There is no policy (ep 2), no sandbox (ep 4) and the engine holds the model key (ep 5). Until then the agent may only read.

```bash
./check.sh                                              # no key, no model
AGENTD_HOME=./var MODEL_KEY=... python3 service/agentd.py    # DeepSeek flash by default; loopback only
python3 demos.py crossover naive 1                      # one real trial (reads your key file, see demos.py)
```

```text
service/    agentd.py (HTTP)  auth.py  store.py  runner.py (one process per session)  naive.py (the broken first try)
engine/     the series 1 engine, unchanged, plus turn.py (one message of one session)
tests/      test_service.py  stack.py
demos.py    the real runs; their traces are in demo/, the counts in demo/facts.json
```

## Real runs (2026-11-25, DeepSeek flash, 25 trials, no failed requests)

| Test | Result |
| --- | --- |
| Two users at once, **naive** service (engine imported, one thread each): lin asks for the code name in their own `codename.txt`, sam 0.5 s later | lin got **sam's** code name in **5 of 5** trials (in 2 of them the answer also noted that the folder was not the expected one). sam got their own in 5 of 5 |
| The same, **one process per session** | 10 of 10 answers were the user's own; none saw the other's file |
| lin asks the agent to show a file in sam's folder | In 4 of 5 the model used the file tool, was refused, and stopped. In **1 of 5** it went straight to a shell command with `cat`, which read-only mode allows, **and got sam's file** |
| Service restarted, saved conversation **kept** | The word from the first message came back in 5 of 5, with no tool call |
| Service restarted, saved file **removed** (an empty disk) | 0 of 5. No error. The agent searched the project (5 to 15 tool calls); 3 said they did not know and **2 offered another word from the project** |

**The cause of the first row:** the series 1 engine keeps its project folder in one variable, `agent.ROOT`. With two threads, the later request changes it while the earlier one is still waiting for the model.

**What this does not show:** how often such an overlap happens under real traffic (here it was arranged: 0.5 s apart), or how another model would behave.

## Leftover variables (real `anthropic` SDK 0.125.0, a stub server, no model)

| Left in the service's shell | What happened |
| --- | --- |
| `ANTHROPIC_BASE_URL`, key passed in code | The call, **with the key**, went to the old address |
| the same, key read from `ANTHROPIC_API_KEY` | A leftover `ANTHROPIC_AUTH_TOKEN` was sent along as well |
| `ANTHROPIC_AUTH_TOKEN`, key passed in code | The token was **not** sent |
| any of them, engine started with an environment built from nothing | Nothing leftover reached the call |

---

# Phase 0: foundation

No service yet. This folder holds the series 1 engine, unchanged, two test doubles, two sandbox profiles and the probes that tried them. The [threat model](../THREAT-MODEL.md) says what the later code has to prove.

```text
engine/     agent.py repl.py mcp_client.py tickets_server.py test_repl.py project/   copied from series 1 ep13 (commit 9e93a2e)
tests/      fakes.py (FakeModel, FakeClock)   test_fakes.py
tests/probes/   engine_gaps.py   seatbelt_probe.py   docker_probe.py
sandbox/    engine.sb (the engine)   command.sb (one command)
check.sh    ./check.sh            ./check.sh probes
```

## Results (2026-11-25, macOS, Python 3.9.6, Docker 29.5.2 on Colima, no model and no API key)

| Check | Result |
| --- | --- |
| Series 1 checks on the copied engine | 38 passed |
| Fake model and clock | 10 passed; the "denied command" check fails when its rule is removed |
| The same two sets **inside** `engine.sb` | passed |
| One turn with the real `anthropic` SDK from inside the sandbox, to a stub proxy | passed; the stub received only the session token |
| `seatbelt_probe.py` in total | 21 of 21 |
| `docker_probe.py` | 7 of 7 |

## What we found

1. **A sandbox cannot start another sandbox.** Inside `engine.sb`, `sandbox-exec` exits 71 (`sandbox_apply: Operation not permitted`). The design (TRD section 7) has the engine start `command.sb` itself, so that cannot work. A command the engine starts keeps the engine's profile and **can reach the proxy port**, which breaks the R4 test.
2. **A repository can run commands.** The engine runs the hooks in `.agent/hooks.json` before any permission check, even when all commands are denied.
3. **A repository can loosen the rules.** An allow rule in `.agent/permissions.json` lets a command run without asking.
4. **A read rule does not cover `grep`.** With `read_file(secret.txt)` denied, `grep` still shows the file's lines.
5. **Rule names differ.** The design writes `Bash(curl *)`; the engine's tools are `run_bash`, `read_file`, `grep`, `edit_file`. Written as in the design, a rule matches nothing. There is no separate write tool.
6. **The default Docker network is open to the host.** A container reached a service listening on `127.0.0.1` through `host.docker.internal`, and the internet. On an `--internal` network it reached neither.

## The sandbox profile (TRD open question 1: answered for this Mac)

The real engine starts and passes its checks under `(deny default)`. Beyond the sketch in the TRD it needed or was given: `/bin`, `/private/etc`, `/dev`, `/private/var/select`, `/private/var/db`, `sysctl-read`, the folder holding the SDK, and the metadata of every parent folder. `/Library` is narrowed to `/Library/Developer/CommandLineTools`. `/private/var/folders` is **not** needed when `TMPDIR` points into the tenant folder. The list has not been cut down to the minimum.

The SDK sits under the home folder on this Mac. The engine should get its own virtual environment, so that no path under home is readable.

## Changes to the design (proposed, not yet applied to the PRD or TRD)

| # | Change | Because |
| --- | --- | --- |
| C1 | The runner, outside the sandbox, starts every command. The engine asks over the control channel. Same shape for `sandbox-exec` and Docker | Finding 1 |
| C2 | The engine loads no hooks and no rules from the repository or the user folder. The service decides every tool call | Findings 2 and 3 |
| C3 | One rule name per kind of action: `Read` covers `read_file` and `grep`, `Edit` covers `edit_file`, `Bash` covers `run_bash`. Drop `Write` | Findings 4 and 5 |
| C4 | The proxy keeps an estimate when a call ends without usage, and refuses calls when its store fails | Docs, below |

## Docs read in Phase 0 (code.claude.com, 2026-11-25)

Gateways overview, Claude apps gateway, its spend limits page, its deployment page (security and outage parts) and the `enforcement` and `pricing` parts of its configuration, and plugins for organizations (control and limits parts). Compare in episode 8:

- Claude apps gateway checks the cap on each request and returns `429` once a developer has passed it. Ours reserves the worst case first, which is stricter.
- It bills a stream that the client aborts, from an estimate of about four characters per output token.
- When its database is unreachable, spend enforcement **lets requests through** by default; `fail_closed_on_error: true` changes that. Ours refuses.

## Still open

- The Docker image of the engine: not built. It needs the SDK installed into `python:3.12-slim`, which downloads packages.
- Linux in CI: not tried. Docker on Linux differs from Colima.
- Docker socket, stream format, where the audit anchor lives: to be decided in the vertical slice.
- No real model run yet.
