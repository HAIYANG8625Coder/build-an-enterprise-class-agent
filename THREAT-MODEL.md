# Threat model (Phase 0 and the first slice, 2026-11-25)

We assume the engine can be turned against us, for example by an instruction hidden in a file it reads. So every control that matters must sit **outside** the engine. "Today" says what has been built in `slice/` and tried. Nothing here has been reviewed by a security specialist.

## What we protect

| Asset | Where it will live |
| --- | --- |
| The model API key | A file only the credential proxy reads |
| The money behind the key | The proxy's budget, one cap per user and day |
| Each tenant's files, memory and sessions | One folder per tenant |
| The audit log, its anchor and the database | Outside every sandbox |
| The host: home folder, SSH keys, other programs | Never inside a sandbox |

## Who or what attacks

A user with a valid token who wants more than their role allows. A prompt injection in a file or web text. A tool description that carries instructions. **A repository's own settings files.** Another tenant. A command the model runs. The engine process itself, once it follows any of these.

## Threats, controls and where we are

| # | Threat | Control | Today |
| --- | --- | --- | --- |
| T1 | The engine reads or writes outside its tenant folder, also through a symlink | OS sandbox around the whole engine | `sandbox-exec`: blocked, also for a hijacked engine. Docker engine image not tried |
| T2 | The engine reaches the internet or another local port | Sandbox network rule: the proxy only | `sandbox-exec`: blocked. Docker `--internal` network: host and internet blocked |
| T3 | A command spends the key through the proxy | Commands run in their own sandbox with no network, **started by the runner** | Built: the runner starts commands in `command.sb`, once per approval, with no token. Blocked from the proxy |
| T4 | The engine reads the API key | The key is only in the proxy; the engine gets a session token | Built: only the proxy reads the key; the engine gets a session token |
| T5 | A repository file loosens the policy or runs a command (rules, hooks) | The engine loads no rules or hooks of its own; the service decides every tool call | Closed in the slice: no hooks loaded, allow rules in local files ignored. Still open in the series 1 engine |
| T6 | A rule is dodged through another tool (`grep` shows a file that `read_file` may not) | One rule name covers every tool that does the same thing | Closed in the slice for `read_file` and `grep`. A command can still read such a file |
| T7 | A command that a pattern rule misses (`curl` inside a script) | No network in the command sandbox; patterns are not the wall | Command sandbox has no network. Patterns can be dodged and are not the wall |
| T8 | A user acts as someone else, or a user changes the policy | Hashed tokens with a role; admin route for policy; revocation | Tokens, roles and revocation built. No admin route for policy yet (ep 2) |
| T9 | Tenant A reads tenant B | The sandbox allows one tenant folder only | Another user's folder is blocked for a hijacked engine. Not tried: two sessions at once |
| T10 | Spend passes the cap: parallel calls, a stopped stream with no usage, a database error | Reserve before each call; keep an estimate when usage never arrives; refuse calls if the store fails | Built: estimate when usage never arrives, refuse when the store fails. Caps not built (ep 8) |
| T11 | Someone edits, deletes or rewrites the audit log | One writer, hash chain, anchor outside the engine's reach | Built: one writer, hash chain, anchor. Edits, removed lines and a rewritten file are found |
| T12 | The engine reports less than it did | Compare its events with what the service and proxy saw; mark its events `source: engine` | Engine events are marked `source: engine`. Comparison not built (ep 7) |
| T13 | The engine has the provider reach the internet for it (server tools, a picture by URL) or floods the log | The proxy accepts only plain requests; a turn has a message limit | Built and tried |

## Not defended

- Whoever owns the host can change the program, the database, the log and the anchor together.
- Data sent to an allowed host is not inspected.
- A bug in the macOS or Linux kernel, in `sandbox-exec` or in Docker. Apple marks `sandbox-exec` deprecated.
- The model provider: it receives the prompts.
- Network attacks between machines: everything listens on the loopback, without TLS.
- A malicious dependency (the `anthropic` SDK and what it installs).
- A hijacked engine changing files inside its own user's folder.
- This is a teaching version. It is no compliance claim and no legal advice.
