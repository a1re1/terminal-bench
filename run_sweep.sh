#!/usr/bin/env bash
# Full terminal-bench sweep with drip. Preflight checks, a hard wall-clock cap,
# and an orphan sweep afterwards.
#
#   ./run_sweep.sh                    # full 66-task sweep on local docker
#   ./run_sweep.sh --dry-run          # preflight only, launches nothing
#   N_CONCURRENT=8 ./run_sweep.sh     # override concurrency
#   HARBOR_ENV=modal ./run_sweep.sh   # run on Modal instead of this machine
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

DRIP_SRC="${DRIP_SRC:-$HOME/src/drip}"
# Local docker containers match the host's arch; Modal sandboxes are always
# x86_64. drip_agent.py probes `uname -m` and picks the same way — this only
# decides which binary preflight insists on.
HARBOR_ENV="${HARBOR_ENV:-docker}"
if [[ "$HARBOR_ENV" == "docker" ]]; then
  case "$(uname -m)" in
    arm64|aarch64) TRIPLE="aarch64-unknown-linux-musl" ;;
    *)             TRIPLE="x86_64-unknown-linux-musl" ;;
  esac
else
  TRIPLE="x86_64-unknown-linux-musl"
fi
BINARY="$DRIP_SRC/target/$TRIPLE/release/drip"
# Local runs are bounded by this machine, not by a billing account.
if [[ "$HARBOR_ENV" == "docker" ]]; then
  N_CONCURRENT="${N_CONCURRENT:-4}"
else
  N_CONCURRENT="${N_CONCURRENT:-20}"
fi
# Harbor's per-task agent timeout has been observed not to fire (a trial ran
# 13h22m against an 8h limit), so cap the whole job from outside.
WALL_CLOCK="${WALL_CLOCK:-14h}"
DATASET="terminal-bench/terminal-bench@4.0.0"

fail() { printf '  FAIL  %s\n' "$1" >&2; FAILED=1; }
ok()   { printf '  ok    %s\n' "$1"; }

echo "Preflight"
FAILED=0

if [[ ! -x "$BINARY" ]]; then
  fail "no linux binary at $BINARY — run: cd $DRIP_SRC && PATH=\"\$HOME/.cargo/bin:\$PATH\" cargo zigbuild --release --target $TRIPLE"
else
  newest_src=$(find "$DRIP_SRC/src" -name '*.rs' -newer "$BINARY" -print -quit 2>/dev/null)
  if [[ -n "$newest_src" ]]; then
    fail "binary is STALE (older than $newest_src) — containers would run an old drip; rebuild first"
  else
    ok "binary current ($(date -r "$BINARY" '+%Y-%m-%d %H:%M'))"
  fi
fi

if [[ ! -f bench.env ]]; then
  fail "bench.env missing"
elif ! grep -q '^OPENROUTER_API_KEY=.\+' bench.env; then
  fail "OPENROUTER_API_KEY is empty in bench.env"
else
  ok "bench.env has OPENROUTER_API_KEY"
fi

# The agent normalizes and sanitizes at upload; surface a broken config here
# rather than 66 containers deep.
if PYTHONPATH="$PWD" python_out=$(~/.local/share/uv/tools/harbor/bin/python -c "
import json, sys, drip_agent as m
try:
    c = json.loads(m.DripAgent._sanitized_config())
    s = c['settings']
    if not all(isinstance(v, str) for v in s.values()):
        raise ValueError('settings not normalized to strings')
    roles = json.loads(s['runtime.role_profiles'])
    ids = {p['id'] for p in json.loads(s['runtime.model_profiles'])}
    bad = [r['name'] for r in roles if r.get('model') and r['model'] not in ids]
    if bad:
        raise ValueError(f'roles naming a missing profile, which would silently fall back to the base model: {bad}')
    print(', '.join(f\"{r['name']}={r.get('model')}\" for r in roles))
except Exception as exc:
    print(exc, file=sys.stderr); sys.exit(1)
" 2>&1); then
  ok "roles resolve: $python_out"
else
  fail "config check: $python_out"
fi

for skill in $(PYTHONPATH="$PWD" ~/.local/share/uv/tools/harbor/bin/python -c "
import drip_agent; print(' '.join(drip_agent.DEFAULT_SKILLS))" 2>/dev/null); do
  [[ -f "bench_skills/$skill/SKILL.md" ]] && ok "skill present: $skill" || fail "DEFAULT_SKILLS names '$skill' but bench_skills/$skill/SKILL.md is missing"
done

(( FAILED )) && { echo; echo "Preflight failed — nothing launched."; exit 1; }
echo "Preflight passed."

[[ "${1:-}" == "--dry-run" ]] && { echo "(dry run — stopping here)"; exit 0; }

echo
echo "Launching: 66 tasks on ${HARBOR_ENV}, ${N_CONCURRENT} concurrent, hard cap ${WALL_CLOCK}"

# Killing harbor does not stop Modal sandboxes; they keep billing. Two sweeps
# have been abandoned with dozens of containers still running, so stop them on
# every exit path, not just the clean one. Local docker containers are torn
# down by harbor itself and cost nothing if they linger, so this is a no-op
# there.
stop_orphans() {
  [[ "$HARBOR_ENV" == "modal" ]] || return 0
  echo
  echo "Checking for orphaned Modal apps..."
  local orphans
  orphans=$(modal app list 2>/dev/null | grep -E "__harbor__" | grep -viE "stopped|ephemeral" || true)
  if [[ -z "$orphans" ]]; then
    echo "  none running."
    return
  fi
  echo "$orphans"
  # Bare `modal app stop` aborts without a TTY; -y is required here.
  echo "$orphans" | grep -oE 'ap-[A-Za-z0-9]+' | while read -r app; do
    printf '  stopping %s ... ' "$app"
    modal app stop -y "$app" >/dev/null 2>&1 && echo ok || echo FAILED
  done
}

on_signal() {
  echo >&2
  echo "!! Interrupted — killing harbor and stopping Modal apps." >&2
  [[ -n "${harbor_pid:-}" ]] && kill "$harbor_pid" 2>/dev/null
  wait "$harbor_pid" 2>/dev/null
  stop_orphans
  exit 130
}
trap on_signal INT TERM

timeout "$WALL_CLOCK" env PYTHONPATH="$PWD" harbor run \
  -d "$DATASET" \
  --agent drip_agent:DripAgent \
  --env "$HARBOR_ENV" \
  --n-concurrent "$N_CONCURRENT" \
  --env-file bench.env &
harbor_pid=$!
wait "$harbor_pid"
status=$?
trap - INT TERM
(( status == 124 )) && echo "!! Hit the ${WALL_CLOCK} cap — job killed from outside." >&2

stop_orphans
exit $status
