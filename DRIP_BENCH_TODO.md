# drip × terminal-bench — outstanding work

Handoff for a fresh session. The benchmark harness is **built and verified**;
what follows is the unfinished work, most-urgent first.

Repo: `~/src/terminal-bench` (worktree `.worktrees/ca56d57b`, branch `manila`).
drip source: `~/src/drip` (public: `github.com/a1re1/drip`).

---

## 1. RESOLVED — the "roles vanish" bug was a stale binary, not a drip defect

**Superseded.** An earlier version of this file claimed drip silently drops all
roles when it rewrites its config to native-JSON arrays. That was wrong, and it
should not be actioned.

The real variable was **binary age**. `~/.cargo/bin/drip` was rebuilt at 09:08 on
2026-09-06; every failing observation predates it, and the failing container run
used a musl binary built 2026-09-05 18:01. Re-tested afterwards, the identical
array-shaped config yields `[role: planner]` on every loop.

Current source handles both shapes and does **not** fail silently: role issues
are printed to stderr as `roles: {issue}` (`src/cli/entry.rs:767-769`), and decode
failure and unknown-profile are distinct paths (`src/cli/roles.rs:713-718`) —
malformed blobs empty the roles and raise issues, while an unknown profile keeps
the role with its route unset.

Two things follow:

- **Keep the musl binary current.** This was a stale-artifact failure, and it
  cost a 26-minute benchmark run that looked valid. `run_sweep.sh` now blocks the
  sweep if any `.rs` file is newer than the binary.
- `DripAgent._sanitized_config()` still normalizes settings to JSON strings
  before upload. It is now belt-and-braces rather than a fix — harmless, and it
  keeps `bench_config.json` readable as arrays. Remove it only if you want one
  less moving part.

## 2. `reasoningEffort` is silently ignored for GPT-6

`supports_openai_reasoning_effort` (`src/core/inference.rs:135-145`) gates on the
`gpt-5.4` family only:

```rust
(provider == "openai"     && model.starts_with("gpt-5.4"))
 || (provider == "openrouter" && model.starts_with("openai/gpt-5.4"))
 || provider == "codex"
```

The `gpt-6-astra` profile in `bench_config.json` carries
`"reasoningEffort": "high"`, which is dropped on the floor. The leaderboard's
astra entries are ranked by effort (`high` / `xhigh` / `max`), so the benchmark
is currently measuring the default lane while the config claims otherwise.

Widen the gate to the gpt-6 family, or drop the key and document that effort is
not expressible over OpenRouter.

---

## 3. Harbor's agent timeout did not fire

`layout-config-recreation2` declares `timeout_sec = 28800.0` (8h) in its
`task.toml`. A run started 2026-09-05 18:06 was still `n_running_trials: 1` at
07:28 the next morning — **13h22m**, over five hours past its own deadline —
with `finished_at: null` and an empty `agent/` directory. Killing the local
process left the Modal container alive; it had to be stopped separately with
`modal app stop -y <app-id>` (the bare form aborts without a TTY).

Before any large sweep, establish whether this is a harbor bug or a
Modal-environment interaction. A single stuck trial in a 66-task run can hold
the job open and bill a container indefinitely, and `harbor run` gives no
progress output until the job ends.

---

## 4. Before the full sweep

- **Cost.** Leaderboard leaders spend $2.3k–$6.2k per sweep. 66 tasks × ~120
  GLM calls is real money; decide a budget first.
- **Rebuild the binary after any drip change.** The container runs a
  cross-compiled artifact, not your local build — a stale binary fails silently:
  ```sh
  cd ~/src/drip && PATH="$HOME/.cargo/bin:$PATH" \
    cargo zigbuild --release --target x86_64-unknown-linux-musl
  ```
  Homebrew's `rustc` shadows rustup on PATH and lacks the musl target, hence the
  `PATH` prefix.
- **Use `--env-file bench.env`**, not `~/.drip/env.vars` — the latter forwards
  every provider credential you own into all 66 containers.

---

## Already done (do not redo)

Custom harbor agent at `drip_agent.py`, verified end-to-end on Modal:

- Uploads a cross-compiled musl binary to `/usr/local/bin/drip` and
  `bench_config.json` to `/opt/drip/config.json` via `$DRIP_HOME`.
- `bench_config.json`, `bench_skills/`, and `bench.env` (git-ignored, mode 600)
  keep the benchmark fully separate from `~/.drip/*`.
- Strips `runtime.cerebras_api_key` and `credentials.stored_api_keys` before
  upload; forwards only the keys the active profiles reference, derived from the
  config rather than hardcoded.
- Activates `navis` on every run via `--skill`, asserting at install time that
  drip actually discovers it (`upload_dir` goes through the Modal SDK and leaves
  no line in `trial.log`, so a failed upload would otherwise be invisible).

Last good run (`jobs/2026-09-06__09-08-11`): roles `[planner ×3, author ×16,
reviewer ×9]`, routing `gpt-6-astra ×6` / `glm-5.3-flash ×121`, navis active
(26 mentions, plus its `P0/P1` and `Triage` vocabulary). Reward 0.0 — a genuine
task failure on a 30-minute budget against 1.5h of expert time, not a
configuration problem.

Run command:

```sh
cd ~/src/terminal-bench/.worktrees/ca56d57b
PYTHONPATH=$PWD harbor run -d terminal-bench/terminal-bench@4.0.0 \
  --agent drip_agent:DripAgent --env modal -n 100 --env-file bench.env
```

Single task: add `-i terminal-bench/<task-name>` (the org prefix is required).
Skip inference entirely with `--install-only`.
