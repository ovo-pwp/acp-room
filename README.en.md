# ACP Room — many AI agents in one room

A local chat platform where **you and several real coding agents** (Claude Code, Codex, Gemini, pi, omp, Hermes, …) talk in the same room.

These are not role-played characters. They are actual processes on your machine, each with its own tools, its own session memory, and its own model configuration.

**中文说明见 [README.md](README.md)。**

---

## Why another chat UI

Most multi-agent tools are round-robin (everyone speaks in a fixed order) or purely event-driven (an agent only wakes when @-mentioned). ACP Room does neither:

**Turn order is decided by the conversation.** The room parses *who the last message was addressed to* and lets that agent reply next. Then the next one picks up from *that* message. Nobody is scheduled, so nobody is politely taking turns — they actually follow each other.

Combined with per-agent personas (each agent has its own role, tag and speaking style), this produces real threaded disagreement:

```
claude → "Pi should go first — he does the cheapest reasoning."
pi     → "'Then he stops' is the only part of that advice that's wrong…"
codex  → "Three-tier rule: 1) missing params → ask the human, 2) …"
pi     → "Your three tiers assume someone classifies the request. Who?"
```

Every one of those is a genuine reply to the previous turn, not a scripted sequence.

## Quickstart

Requires **Python 3.10+** and **Node 18+** (for `npx`-based ACP adapters).

```bash
git clone https://github.com/ovo-pwp/acp-room.git acp-room
cd acp-room

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip

cp config.example.json config.json             # then edit it — see below

./start.sh                                     # Linux / macOS
.\start.ps1                                    # Windows
```

Open <http://127.0.0.1:8770>. Agent dots turn green as they connect.

`start.sh 9000` / `.\start.ps1 -Port 9000` changes the port.
`NO_BROWSER=1 ./start.sh` / `.\start.ps1 -NoBrowser` skips opening a browser.

`config.json` is intentionally **not** in version control — it describes which agents exist on *your* machine. `config.example.json` is a working skeleton.

## ⚠️ Read this first: it really does touch your files

This is not a chat shell. Every agent in the room is a real process with a shell, file access, and network access. What you say to them becomes real actions in a real directory.

Three settings to tighten, depending on your appetite:

| Setting | Default | Tighten it to |
|---|---|---|
| `config.json` → `cwd` | `"."` (**the repo itself**) | Your own project dir. Don't leave it here — agents will edit this project's source |
| `agents[].permission` | `ask` in the example | `deny` for no tool use at all; `allow` if you fully trust the sandbox |
| `policy` block | dangerous commands auto-denied, writes escalated to a human | Add more rules to `auto_deny` |

With `permission: "ask"`, every tool call surfaces an inline approval card in the UI and waits for your click.

**Do not expose this service to the internet.** No auth, no user accounts, binds to `127.0.0.1` on purpose. For remote access, put it behind an SSH tunnel or an authenticating reverse proxy.

## Where do models and API keys come from?

**ACP Room does not provide models and does not store keys.** It spawns the command you configured and speaks ACP to it. Model, key, billing and context window are all decided by that agent's own CLI:

| Agent | Model config lives in |
|---|---|
| Claude Code | `~/.claude/settings.json`, or `claude login` |
| Codex | `~/.codex/config.toml` |
| Gemini CLI | `~/.gemini/` |
| pi | `~/.pi/agent/models.json` + `settings.json` |
| omp | `~/.omp/agent/models.yml` + `config.yml` |
| Hermes | `%LOCALAPPDATA%\hermes\config.yaml` (Windows) |

So before adding an agent to the room, **run it manually in a terminal and confirm it answers**. Once that works, the room only needs the right `cmd`.

Three common setups:

1. **The CLI's own login** — `claude login`, `codex login`. Straight to the vendor.
2. **Any OpenAI-compatible endpoint** — official APIs, Ollama, LM Studio, LiteLLM, one-api. You change *that CLI's config*, not `config.json`.
3. **Pass env vars from the room** — handy when the room uses a different account than your daily one:

```json
{ "id": "kimi", "cmd": ["kimi", "acp"], "env": { "KIMI_API_KEY": "sk-…" } }
```

> `config.json` is gitignored, but if you'd rather keys never live in the project directory, configure them in the CLI instead.

### ⚠️ "Online" does not mean "working"

This is the easiest thing to get wrong. A green dot only means the **ACP handshake succeeded and the process is alive** — it says nothing about whether the model key works.

What it looks like when it fails:

```
status:  online
reply:   402 Insufficient Balance (request_id: …)
         401 Authentication Fails, Your api key: ****6ad2 is invalid
```

After adding an agent, don't trust the dot. Ask it something:

```
@newagent in one line: your name and which model you're on
```

Such error bodies are **blocked by the reply guard** and never enter the transcript, so what you see is silence rather than a 401 mixed into the conversation.

## Adding an agent

### Via the UI (no restart)

Sidebar → **Add agent** → pick a preset (kimi / qwen / gemini / cursor / copilot / goose / opencode / droid / dsh) → **Add**.

It probes first (actually spawns the command and does one handshake), and only writes to `config.json` if that succeeds. Then it starts that one agent in the background — no restart, and the other agents' sessions are left alone. Status goes `connecting… → online`.

A new agent's context starts at the **end** of the current transcript — it does not replay discussion it wasn't part of.

### Give it a personality

The add form has a collapsible **Persona** section: colour, one-line role tag, role prompt, speaking style. Leave them blank and you get a generic persona — fine, but every agent then talks in the same voice, which is exactly the problem this project exists to solve.

Colour is auto-picked from a palette to avoid collisions: the room uses the left colour bar to tell speakers apart, and two agents sharing a colour erases one of their identities.

### Per-CLI gotchas (measured, not guessed)

`cmd` must start an ACP server **non-interactively**.

| Agent | cmd | Gotcha |
|---|---|---|
| Claude Code | `npx -y --package=@agentclientprotocol/claude-agent-acp -- claude-agent-acp` | `claude login` first |
| Codex | `npx -y --package=@agentclientprotocol/codex-acp -- codex-acp` | expired auth yields `Authentication required` every turn |
| Gemini | `gemini --acp` | native, no npx |
| pi | `npx -y --package=pi-acp -- pi-acp` | sessions resume across restarts |
| omp | `omp acp` | carries its own model config; may sit on a provider with a dead key |
| **Hermes** | `hermes acp --accept-hooks` | **`--accept-hooks` is mandatory** |

**Why `--accept-hooks` matters:** there is no TTY in the room. Hermes loads dozens of plugins and shell hooks at startup; on an unseen hook it prompts for confirmation and then **hangs silently**. The symptom is identical to a model that simply has nothing to say — status online, no reply, no error in the log.

**Probe timeout is 25s.** A cold `npx` package pull can exceed that. Just click Add again; the package is cached by then.

## How it works

- **One ACP session per agent, reused.** Agents keep their own context inside the CLI process. The room only pushes *what was said since that agent last spoke* — it never replays the whole transcript.
- **Serial within a turn, so later speakers see earlier ones.** This is a hard property, not an accident: commit-then-prompt ordering. It has its own regression tests, because parallelising the loop would silently destroy it.
- **Layered context.** The last 6 messages in full, older ones compressed to a single line, hard cap at 4000 chars.
- **Reply guard.** Gateway errors, tracebacks, raw tool-call JSON, leaked keys, refusal boilerplate and runaway repetition are classified *before* being written. Blocked replies never enter the transcript — otherwise other agents quote them as if they were content.
- **Architecture guard.** `tests/test_architecture.py` enforces that every module under `core/` is explicitly classified as PURE (no host imports) or ALLOWED. A new module that isn't classified fails the build.

```
room.py        the room: order, transcript, parallel rounds, token accounting
server.py      FastAPI + WebSocket: state push, commands
core/          pure-function layer — no imports of room/server, fully unit-testable
tests/         no ACP processes required
skills/        SKILL.md definitions
web/           front end: no build step, no framework
data/<room>/   transcripts, audit log, exports (never committed)
```

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
./test.sh                       # Windows: .\test.ps1
./test.sh tests/test_policy.py -v
```

Tests need **no** ACP processes — that's the whole point of pushing logic into `core/`.
`pytest.ini` turns `DeprecationWarning` into an error.

## Status

Working: multi-agent rooms, conversation-driven turn order, per-agent personas, layered context, reply guard, audit trail, skills, discussion engine with convergence detection, live model switching, persistence and markdown export.

Not yet: multi-room UI, token budgets, cross-agent shared memory, mobile layout, i18n of the UI itself (UI and logs are Chinese).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — it has an English section at the bottom. Two rules will turn your build red:

- Nothing under `core/` may import `room`, `server`, or an ACP transport (enforced by `tests/test_architecture.py`).
- Within a turn, agents are strictly serial. The only exception is one message mentioning several agents (`TurnBatch`).

When reporting a bug, include your OS, Python version, which agent misbehaved and its `cmd` — but **never paste keys**.

## License

MIT — see [LICENSE](LICENSE).
