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
| arch           | x86_64 (emulated)       | always x86_64            |
| GPU tasks      | **cannot run**          | yes, with billing on     |
| concurrency    | 4 default; 15 is safe   | 20 default               |
| cost           | electricity             | per container-hour       |

Local containers are **x86_64 even on Apple Silicon**: harbor pulls prebuilt
pinned `harborframework/terminal-bench:*` images, which are amd64 only, so
everything runs under Rosetta translation. The host's own arch does not enter
into it — `drip_agent.py` probes `uname -m` inside the container and picks the
musl target from that, so only the `x86_64-unknown-linux-musl` build is actually
used today. Expect setup steps (a 13 MB binary upload) to be several times slower
than native, which is what makes the agent-setup timeout the first thing to blow
under concurrency.

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

> **The config flip needs a second edit.** Confirmed on 2026-09-08: resume
> rebuilds a `TrialConfig` for every task and requires it to be **exactly equal**
> to the `config.json` of each trial that still has a `result.json`
> (`harbor/job.py:348`). Any job-level change — `environment`, a timeout
> multiplier — makes the plan differ from what the surviving trials recorded, and
> resume aborts before starting anything:
>
> ```
> ValueError: Existing trial config does not match planned job config.
> ```
>
> So step 2 must write the same field into every surviving trial's
> `jobs/<JOB_DIR>/<trial>/config.json` as well, not just the job config. Trials
> that resume is about to delete (`-f`) don't matter; only the ones it keeps.
> `n_concurrent_trials` is the exception — it lives on the job and in
> `lock.json`, not in the trial configs (see below).

---

## Sizing a local sweep, and what actually breaks

Every failure below was hit on 2026-09-08. Read this before raising concurrency.

**Concurrency is bounded by host RAM, not by the VM or by task declarations.**
A 63-concurrent sweep was killed by macOS for memory pressure. Measuring the
live containers showed why the obvious diagnosis was wrong:

```
31 containers: 7.9 GB in use / 410 GB of caps granted   → 2% utilization
VM at that moment: 42 of 46 GB free
```

Task-declared `memory_mb` is close to meaningless — several tasks declare 16 GB
and use a few hundred MB, so container caps never bind. What binds is that
colima with `--vm-type vz` reserves its whole allocation from the host up front,
and harbor spawns **one host-side `docker compose` process per concurrent
trial**. A 48 GB VM on a 64 GB Mac left ~16 GB for macOS, the editor, harbor and
63 compose processes; macOS killed the largest tree, which was harbor. The VM is
now 32 GB (`~/.colima/default/colima.yaml`), which cost nothing and doubled the
host reserve. **15 concurrent is the tested ceiling here.**

Watch host free memory, not `docker stats`, when judging headroom.

**Raise the setup timeout whenever you raise concurrency.** The agent-setup
budget is 360s (`harbor/trial/trial.py:93`) and it bounds the binary upload,
which is slow under Rosetta. At 63 concurrent, 31 trials died on it. Both knobs
are job-level and take effect on resume:

```jsonc
"n_concurrent_trials": 15,
"agent_setup_timeout_multiplier": 5.0,      // 360s -> 1800s
"environment_build_timeout_multiplier": 3.0  // env start, per-task base
```

**`n_concurrent_trials` also lives in `lock.json`,** and a mismatch against the
resolved lock is a hard `FileExistsError` (`harbor/job.py:909`). Move the
job-level `lock.json` aside before resuming with a different concurrency.

**Docker's default address pool runs out at 31 networks.** Symptom: `all
predefined address pools have been fully subnetted`. Fixed in
`~/.colima/default/colima.yaml`, giving 256:

```yaml
docker:
  default-address-pools:
    - base: 10.200.0.0/16
      size: 24
```

**A killed run leaves debris that looks like a real failure.** In-flight trials
record `RuntimeError: There is no current event loop in thread 'MainThread'`.
That shares its type with genuine `RuntimeError`s — notably
`live-database-cutover`, which declares `cpus = 16` against a 12-CPU VM on a
14-core host and so can never run locally at any concurrency. `-f RuntimeError`
cannot tell them apart. Match on the message and delete just the debris dirs;
any trial dir without a `result.json` is re-run (`harbor/job.py:265`).

**Two settings that must be right before a sweep, both silent when wrong:**

- `credsStore` in `~/.docker/config.json` — a leftover `"desktop"` from a removed
  Docker Desktop makes every image pull fail with
  `docker-credential-desktop: executable file not found`, failing 100% of trials.
- `exclude_task_names` matching is `fnmatch` against the **org-prefixed** name
  (`harbor/models/job/config.py:147`), so a bare `math-eval-grader` silently
  matches nothing. Always write `terminal-bench/math-eval-grader`.

**A CLI dataset flag replaces the whole dataset block** and nulls out `agents`,
so `-c bench_job.yaml` cannot be combined with `-x` or `-i`. Check any
combination with `--print-config` before trusting it; this is why the GPU
exclusions live in the YAML and `.strip_excludes.py` exists.

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
