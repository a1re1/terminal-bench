# Running the drip benchmark

Operational runbook. For outstanding work see `DRIP_BENCH_TODO.md`.

Repo: `~/src/terminal-bench` (worktree `.worktrees/ca56d57b`, branch `manila`).
drip source: `~/src/drip` (public: `github.com/a1re1/drip`).

---

## Quick reference

```bash
./run_sweep.sh --dry-run              # preflight only, launches nothing
./run_sweep.sh                        # full 66-task sweep, local docker, 4 concurrent
N_CONCURRENT=8 ./run_sweep.sh         # override concurrency
HARBOR_ENV=modal ./run_sweep.sh       # run on Modal instead of this machine
```

Single task, bypassing the sweep script (note the org prefix on `-i`, without it
the filter matches nothing):

```bash
PYTHONPATH="$PWD" harbor run \
  -d terminal-bench/terminal-bench@4.0.0 \
  -i terminal-bench/foodstuff-beta-activity \
  --agent drip_agent:DripAgent \
  --env docker --n-concurrent 1 --env-file bench.env
```

Add `--install-only` for a ~40s compatibility check that builds the container,
uploads the binary, and asserts skill discovery without running the task.

---

## Two backends

`--env docker` is harbor's **default** and runs containers on this machine.
`--env modal` runs them in Modal sandboxes and costs money per container-hour.

Local is the right default: in the 2026-09-08 sweep the Modal bill hit its cap
while OpenRouter spend was only ~$22. Tokens were never the expensive part.

|                | local docker            | modal                    |
| -------------- | ----------------------- | ------------------------ |
| arch           | host arch (arm64 here)  | always x86_64            |
| GPU tasks      | **cannot run**          | yes, with billing on     |
| concurrency    | 4 default (14 cpu host) | 20 default               |
| cost           | electricity             | per container-hour       |

### Architecture

`drip_agent.py` probes `uname -m` in the container and picks the matching musl
binary, so one agent serves both backends with no flag. Keep both built:

```bash
cd ~/src/drip && PATH="$HOME/.cargo/bin:$PATH" \
  cargo zigbuild --release --target aarch64-unknown-linux-musl   # local docker
cd ~/src/drip && PATH="$HOME/.cargo/bin:$PATH" \
  cargo zigbuild --release --target x86_64-unknown-linux-musl    # modal
```

Task base images are overwhelmingly multi-arch (`ubuntu:24.04`,
`python:3.1x-slim`), so arm64 containers run natively with no qemu penalty.
`vllm/vllm-openai-cpu:v0.21.0` is the one likely amd64-only base image.

---

## Splitting a run: CPU tasks locally, GPU tasks on Modal

4 tasks reference CUDA/nvidia and cannot run locally. Rather than producing two
job directories and hand-merging them, run one job and **resume it against a
different backend**. `harbor job resume` reads the environment from the job
directory's `config.json`, so flipping that field retargets the retry.

```bash
# 1. Full sweep locally. The GPU tasks fail; everything else scores.
./run_sweep.sh

# 2. Retarget the job at Modal.
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("jobs/<JOB_DIR>/config.json")
c = json.loads(p.read_text())
c["environment"] = {"type": "modal"}
p.write_text(json.dumps(c, indent=2))
PY

# 3. Resume only the failed trials. --filter-error-type drops trials with those
#    error types before resuming, so the passing local results are untouched.
harbor job resume -p jobs/<JOB_DIR> -f <ErrorType>

# 4. One coherent job, one job ID, correct aggregate stats.
harbor upload jobs/<JOB_DIR>
```

**Why not stitch two job dirs.** The job-level `result.json` carries derived
aggregates (`n_completed_trials`, `evals`, `reward_stats`, the mean) and
`config.json` records a single `environment`. Merging by hand means editing
derived data and misreporting the environment for half the trials, with no
guarantee the server accepts it.

**Upload is idempotent.** Per `harbor job resume --upload`: if the job was
already partially uploaded, it "fills in the missing trials and finalizes." So
uploading before the GPU half lands is safe — resume and re-upload completes the
same job rather than creating a second one.

> **Untested.** The config-flip mid-job has not been exercised; resume may
> validate the environment against the per-trial locks. Prove it on a 2-task job
> before relying on it for a real sweep.

---

## Setup on a fresh machine

```bash
brew install colima docker docker-compose
mkdir -p ~/.docker/cli-plugins
ln -sf /opt/homebrew/lib/docker/cli-plugins/docker-compose ~/.docker/cli-plugins/
colima start --cpu 8 --memory 32 --disk 100
PATH="$HOME/.cargo/bin:$PATH" rustup target add aarch64-unknown-linux-musl
```

`brew install docker` ships only the CLI — harbor drives `docker compose`, and
without the plugin symlink every trial dies with `docker: unknown command:
docker compose`.

`bench.env` (git-ignored, mode 600) holds `OPENROUTER_API_KEY`. Keys are read
from the harbor process environment and forwarded into the container; they are
never stored in Modal or in `bench_config.json`.

---

## Costs and limits

Check the OpenRouter balance before a sweep — `total_credits - total_usage` is
what actually funds requests, not the key limit or the workspace guardrail:

```bash
set -a; . ./bench.env; set +a
curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  https://openrouter.ai/api/v1/credits
```

Recent per-task cost is $0.25–$2.40, so budget $60–$150 for 66 tasks.

**High concurrency causes 402s.** At `-n 100`, OpenRouter reserves each in-flight
request's estimated max cost; 100 simultaneous planner calls reserved past the
balance and a third of the trials died at cycle 1 with 0 model calls. drip has no
retry on 402 (`model_call.rs` retries only 429 and 5xx), so a single blip kills
the whole run. Keep concurrency modest.

---

## Cleaning up Modal

Killing harbor does **not** stop Modal sandboxes; they keep billing.
`run_sweep.sh` traps INT/TERM and sweeps orphans on every exit path, but that
trap does not fire when the process is killed externally. Check by hand after any
abnormal stop:

```bash
modal app list | grep __harbor__
modal app stop -y <app-id>       # bare `modal app stop` aborts without a TTY
```

---

## Reading results

```
jobs/<JOB_DIR>/
  config.json                    job_name, environment, agents, datasets
  result.json                    id, stats, per-eval aggregates
  <task>__<id>/
    result.json                  reward, agent_result (cost/tokens), exception_info
    agent/drip.txt               the full drip transcript
    verifier/ctrf.json           per-test pass/fail
    verifier/test-stdout.txt     assertion text, expected vs actual
    artifacts/                   the container's working tree
```

Note `find <job> -mindepth 2 -name result.json` when counting completed trials —
the job-level `result.json` exists from launch and will inflate the count.

Reward is all-or-nothing per task: 11/13 passing tests still scores 0.0.
