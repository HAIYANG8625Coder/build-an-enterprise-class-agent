# slice: the first end-to-end path

A request with a token → a policy decision → the engine in a sandbox → the model through the key proxy → an audit line → a check of the hash chain. It exists to prove the design before any episode is filmed (TRD section 14). It is not a product, and not yet any episode's final code.

```bash
./check.sh               # every check, no key and no model
python3 demo.py          # the whole path with a scripted model
python3 demo.py --real   # the same with DeepSeek flash; only the proxy reads the key
```

Needs Python 3.9 and the `anthropic` package. The sandboxes are macOS `sandbox-exec`; on another system the sandbox checks are skipped, and the service must be told `AGENTD_SANDBOX=none`, which is for tests only.

```text
service/   agentd.py (HTTP, the one audit writer)  auth.py  store.py  policy.py  runner.py  audit.py
engine/    the series 1 engine, unchanged, plus bridge.py (asks the service before every tool call)
proxy/     keyproxy.py (the only holder of the model key)
sandbox/   engine.sb  command.sb
config/    policy.example.json
tests/     identity, policy, proxy, audit, end to end, a hijacked engine
```

## Results (2026-11-25, macOS, Python 3.9.6)

| Check | Result |
| --- | --- |
| Series 1 checks on the engine | 38 passed |
| Service checks, scripted model | 55 passed, none skipped |
| Controls switched off one at a time, in a copy | 19 of 19 made a check fail (run before the 55th check, a limit on engine messages, was added) |
| Real run, DeepSeek flash, one task, one trial | completed |

**The real run.** Task: run the project's tests, then download a web page with `curl`.

- The model's first two commands were refused, because each held a part that no rule allows (`cat NOTES.md`, `echo "EXIT: $?"`) and nobody was there to approve. It then used the file reader and plain commands, and the tests passed.
- `curl` was refused by the company rule, no page was saved, and the model said so.
- The proxy counted 4 model calls: 1,704 input and 615 output tokens.
- The audit check passed on the log. On a copy with one decision changed, it named that line.

One trial shows that the path works. It says nothing about how often the model behaves this way.

## What it does not do yet

- No prices, budgets or retries (ep 8, 10). No approval screen: "ask" means no.
- No admin API for policy, members or tokens. The demo creates users directly.
- One engine per message; the conversation is not saved.
- No Docker variant, no egress proxy, no CI.

## Known limits

- **A hijacked engine can still change files in its own user's folder** without asking: the sandbox allows it. The rules bind the engine's tool calls; the sandbox bounds what a broken engine can do.
- **Command rules judge words.** `Bash(python3 tests/*)` also matches `python3 tests/../anything.py`, and an allowed script can read a file that a `Read` rule denies. The command sandbox keeps it inside the user's folder, with no network.
- A `Read` rule judges paths, so a hard link under another name is not caught.
- The engine can read the whole folder that holds the SDK (here under the home folder). It needs its own virtual environment.
- The sandbox profiles allow system folders and have not been cut to the minimum.
- Requests without a token are logged too, so someone on this machine could fill the log.
- About 1,150 lines of new code and profiles already, against a target of under 1,500 for the whole series.
