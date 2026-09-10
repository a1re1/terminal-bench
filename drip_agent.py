"""Harbor agent that runs drip (github.com/a1re1/drip) inside the task container.

Model routing is drip's own, not Harbor's: install() uploads ~/.drip/config.json,
whose role_profiles put the planner role on gpt-6-astra (OpenRouter) and the
author and reviewer roles on z-ai/glm-5.3-flash. Harbor's --model flag is
advisory here and is recorded in the trajectory only.

The binary is cross-compiled on the host, not built in the container:
    cd ~/src/drip
    PATH="$HOME/.cargo/bin:$PATH" cargo zigbuild --release \
        --target x86_64-unknown-linux-musl
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

DRIP_SRC = Path.home() / "src" / "drip"
# The container's architecture picks the binary, not the host's: Modal sandboxes
# are x86_64, while --env docker on Apple Silicon is aarch64. install() probes
# `uname -m` so one agent serves both without a flag.
MUSL_TARGETS = {
    "x86_64": "x86_64-unknown-linux-musl",
    "amd64": "x86_64-unknown-linux-musl",
    "aarch64": "aarch64-unknown-linux-musl",
    "arm64": "aarch64-unknown-linux-musl",
}


def _local_binary(triple: str) -> Path:
    return DRIP_SRC / "target" / triple / "release" / "drip"
# A benchmark-owned config, versioned with this agent, so provider and role
# tweaks here never disturb ~/.drip/config.json (and vice versa).
LOCAL_CONFIG = Path(__file__).resolve().parent / "bench_config.json"

REMOTE_BINARY = "/usr/local/bin/drip"
# $DRIP_HOME overrides ~/.drip (core/home.rs:68), so the config lands at a fixed
# absolute path instead of one that depends on the container's agent user.
REMOTE_HOME = "/opt/drip"
REMOTE_CONFIG = f"{REMOTE_HOME}/config.json"
# drip discovers its user skill library at <home>/skills (core/home.rs:83,204).
# Sourced from the repo, not ~/.drip/skills: the host pool is shipping-oriented
# (navis ends in "commit, push, open a draft PR"), which is wasted effort in a
# task container that usually has no git remote.
REMOTE_SKILLS = f"{REMOTE_HOME}/skills"
LOCAL_SKILLS = Path(__file__).resolve().parent / "bench_skills"
# Activated on every run via --skill, which joins the SKILL.md to the system
# prompt (drip --help:109). install() fails fast if one isn't in the pool.
DEFAULT_SKILLS = ("navis", "verify-before-done", "review-independently")

# drip prints one of these per model call, e.g.
#   [  1] inference openai/gpt-6-astra — 3896 prompt (3241 cached, 0 written), 119 completion in 3669ms
USAGE_RE = re.compile(
    r"inference\s+(?P<model>\S+)\s+\u2014\s+"
    r"(?P<prompt>\d+) prompt \((?P<cached>\d+) cached, (?P<written>\d+) written\), "
    r"(?P<completion>\d+) completion"
)

# USD per token, from OpenRouter (`/api/v1/models`, fields prompt / completion /
# input_cache_read) as of 2026-09-06. Refresh with:
#   curl -s https://openrouter.ai/api/v1/models | python3 -c "..."
# A model absent here still reports tokens; only cost_usd is left unset.
PRICING = {
    "openai/gpt-6-astra": {"prompt": 1e-5, "completion": 5e-5, "cache_read": 1e-6},
    "z-ai/glm-5.3-flash": {
        "prompt": 7.5e-8,
        "completion": 2.5e-7,
        "cache_read": 1.5e-8,
    },
}
LOG_FILE = "/logs/agent/drip.txt"

# rustls bundles webpki roots, so no ca-certificates package is needed (and
# harbor only allows curl/bash/git/tmux/ripgrep/xz as system deps).
# Which credentials to forward is derived from bench_config.json rather than
# hardcoded, so changing a profile's provider does not silently fail to pass its
# key. Only profiles actually in use are considered, so unrelated provider keys
# in the env file stay on the host. drip merges its env.vars file over the
# process environment (core/inference.rs:88-98), so exec env is enough.


class DripAgent(BaseInstalledAgent):
    """Runs one headless drip goal per task: `drip "<instruction>"`."""

    @staticmethod
    @override
    def name() -> str:
        return "drip"

    @override
    def get_version_command(self) -> str | None:
        return f"DRIP_HOME={REMOTE_HOME} {REMOTE_BINARY} --version"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().removeprefix("drip").strip()

    def _forwarded_env(self) -> dict[str, str]:
        env = {"DRIP_HOME": REMOTE_HOME}
        missing = []
        for key in self.required_env_keys():
            value = self._get_env(key)
            if value:
                env[key] = value
            else:
                missing.append(key)
        if missing:
            raise ValueError(
                f"drip needs {', '.join(missing)} in the environment harbor runs in; "
                "its model profiles resolve keys through env: references."
            )
        return env

    async def _container_arch(self, environment: BaseEnvironment) -> str:
        """`uname -m` inside the task container, which decides the musl target."""
        result = await self.exec_as_root(environment, command="uname -m")
        machine = getattr(result, "stdout", None) or ""
        if not machine.strip():
            raise RuntimeError(
                "Could not read `uname -m` from the container; cannot pick a drip binary."
            )
        return machine

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        machine = (await self._container_arch(environment)).strip()
        triple = MUSL_TARGETS.get(machine)
        if triple is None:
            raise RuntimeError(
                f"Container reports `uname -m` = {machine!r}, which has no musl "
                f"target mapping. Known: {sorted(set(MUSL_TARGETS))}."
            )
        local_binary = _local_binary(triple)
        if not local_binary.exists():
            raise FileNotFoundError(
                f"No {machine} linux binary at {local_binary}. Cross-compile it first:\n"
                f"  cd {DRIP_SRC} && PATH=\"$HOME/.cargo/bin:$PATH\" cargo zigbuild "
                f"--release --target {triple}"
            )
        if not LOCAL_CONFIG.exists():
            raise FileNotFoundError(
                f"No benchmark drip config at {LOCAL_CONFIG}. Seed one by\n"
                "copying ~/.drip/config.json there, minus its credential keys."
            )

        await self.ensure_system_dependencies(environment, ("git",))

        await self._upload_agent_owned_file(environment, local_binary, REMOTE_BINARY)
        await self.exec_as_root(
            environment,
            command=f"chmod 755 {shlex.quote(REMOTE_BINARY)} && mkdir -p {shlex.quote(REMOTE_HOME)}",
        )

        await self._upload_config_text(
            environment,
            content=self._sanitized_config(),
            remote_path=REMOTE_CONFIG,
            filename="config.json",
        )
        # _upload_config_text chmods 600 and chowns the file to the agent user,
        # but root still owns the directory. drip writes under $DRIP_HOME at
        # runtime — the session index at projects/<slug>/sessions/ — so 755 on a
        # root-owned home makes it traversable and still unwritable, and drip
        # panics: "create index parent dirs: PermissionDenied"
        # (core/sessions.rs:123).
        #
        # default_user is None whenever the image declares no USER, i.e. the
        # agent already runs as root and owns REMOTE_HOME. Interpolating that
        # None into the command yields `chown None`, which exits 1 and fails
        # install on every root image — the common case — so only the handful of
        # images with a real USER get the chown. _upload_skills guards the same
        # way.
        chown = ""
        if environment.default_user is not None:
            chown = f"chown {shlex.quote(str(environment.default_user))} {shlex.quote(REMOTE_HOME)} && "
        await self.exec_as_root(
            environment,
            command=f"{chown}chmod 755 {shlex.quote(REMOTE_HOME)}",
        )

        await self._upload_skills(environment)

        # Fail install (not mid-task) if the cross-compiled binary can't run on
        # the container's arch or the uploaded config won't parse.
        await self.exec_as_agent(
            environment,
            command=f"DRIP_HOME={shlex.quote(REMOTE_HOME)} {REMOTE_BINARY} --version",
        )

    @classmethod
    def _role_routing(cls) -> dict[str, str]:
        """role name -> profile id, as the uploaded config defines it."""
        try:
            settings = json.loads(cls._sanitized_config())["settings"]
            return {
                role["name"]: role.get("model")
                for role in json.loads(settings["runtime.role_profiles"])
                if role.get("model")
            }
        except Exception:
            return {}

    @classmethod
    def _config_model_name(cls) -> str | None:
        """`provider/modelA+modelB` for harbor's model column.

        drip routes per role, so naming only the base profile hides the
        planner — which is a small fraction of calls but a large fraction of
        spend, and the whole point of the routing. Every model that a role can
        reach is named here, joined by "+", with the base profile first.

        harbor splits this on the first "/" only (agents/base.py:154-155), so
        the leading segment is the provider and everything after it is the
        model label. Exact per-model counts stay in context.metadata.
        """
        try:
            config = json.loads(cls._sanitized_config())
            settings = config["settings"]
            profiles = {
                p["id"]: p for p in json.loads(settings["runtime.model_profiles"])
            }
            active = settings.get("runtime.active_profile_id")

            reachable = [active] if active in profiles else []
            for role in json.loads(settings.get("runtime.role_profiles", "[]")):
                pid = role.get("model")
                if pid in profiles and pid not in reachable:
                    reachable.append(pid)
            if not reachable:
                return None

            providers = {profiles[p]["provider"] for p in reachable}
            if len(providers) != 1:
                # A mixed-provider run has no single provider to report; fall
                # back to the base profile rather than mislabel the column.
                base = profiles.get(active)
                return f"{base['provider']}/{base['model']}" if base else None

            names = "+".join(profiles[p]["model"] for p in reachable)
            return f"{providers.pop()}/{names}"
        except Exception:
            return None
        return None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Only fill the gap; an explicit --model always wins.
        if not self.model_name:
            self.model_name = self._config_model_name()
            self._init_model_info()

    @staticmethod
    def _setting(config: dict, key: str):
        """Read a settings value, tolerating both of drip's on-disk shapes.

        drip normalizes settings to native JSON, but hand-edited configs and
        older writes store the same values as JSON strings.
        """
        value = config.get("settings", {}).get(key)
        return json.loads(value) if isinstance(value, str) and value[:1] in "[{" else value

    @classmethod
    def required_env_keys(cls) -> tuple[str, ...]:
        """env: names referenced by the profiles this config actually uses."""
        config = json.loads(LOCAL_CONFIG.read_text())
        profiles = cls._setting(config, "runtime.model_profiles") or []

        wanted = {
            config.get("settings", {}).get("runtime.active_profile_id"),
            config.get("settings", {}).get("runtime.active_tool_profile_id"),
        }
        for role in cls._setting(config, "runtime.role_profiles") or []:
            wanted.add(role.get("model"))

        keys = []
        for profile in profiles:
            if profile.get("id") not in wanted:
                continue
            ref = profile.get("apiKeyRef", "")
            if ref.startswith("env:") and ref[4:] not in keys:
                keys.append(ref[4:])
        return tuple(keys)

    @staticmethod
    def _sanitized_config() -> str:
        """The bench config with credential-bearing settings removed.

        Keys belong in the environment (see FORWARDED_KEYS), never in a file
        that ships to every task container.
        """
        config = json.loads(LOCAL_CONFIG.read_text())
        settings = config.get("settings", {})
        for key in ("runtime.cerebras_api_key", "credentials.stored_api_keys"):
            settings.pop(key, None)

        # drip WRITES these settings as native JSON but only READS roles from
        # JSON strings (cli/roles.rs:500-510 takes &str), so an array-shaped
        # config silently loads no roles at all — every role falls back to the
        # base model with no error. Normalize so bench_config.json stays
        # readable in either shape.
        config["settings"] = {
            k: v if isinstance(v, str) else json.dumps(v) for k, v in settings.items()
        }
        return json.dumps(config, indent=2)

    async def _upload_skills(self, environment: BaseEnvironment) -> None:
        """Place the benchmark skill pool at $DRIP_HOME/skills, if any exists."""
        if not LOCAL_SKILLS.is_dir():
            return
        skills = [d for d in LOCAL_SKILLS.iterdir() if (d / "SKILL.md").is_file()]
        if not skills:
            return

        missing = [s for s in DEFAULT_SKILLS if s not in {d.name for d in skills}]
        if missing:
            raise FileNotFoundError(
                f"DEFAULT_SKILLS names {missing}, absent from {LOCAL_SKILLS}. "
                "Each needs its own directory containing a SKILL.md."
            )

        await environment.upload_dir(LOCAL_SKILLS, REMOTE_SKILLS)
        # upload_dir copies as root; drip reads skills as the agent user.
        if environment.default_user is not None:
            user = shlex.quote(str(environment.default_user))
            await self.exec_as_root(
                environment,
                command=f"chown -R {user} {shlex.quote(REMOTE_SKILLS)}",
            )

        # upload_dir goes through the Modal SDK and leaves no line in trial.log,
        # so a silently-empty skill pool would be invisible. Assert instead that
        # drip itself discovers everything we intend to activate.
        for skill in DEFAULT_SKILLS:
            await self.exec_as_agent(
                environment,
                # Written to a file rather than piped: `grep -q` exits on the
                # first match and closes the pipe, and drip then panics with
                # "failed printing to stdout: Broken pipe" (exit 101). Rare
                # serially, common under concurrency.
                command=(
                    f"DRIP_HOME={shlex.quote(REMOTE_HOME)} {REMOTE_BINARY} --skills "
                    f"> /tmp/drip-skills.txt && "
                    f"grep -q {shlex.quote('^' + skill + ' ')} /tmp/drip-skills.txt"
                ),
            )

    async def _read_log(self, environment: BaseEnvironment) -> str:
        """Best-effort read of the tee'd transcript; never masks the real error."""
        try:
            result = await self.exec_as_agent(
                environment, command=f"cat {LOG_FILE} 2>/dev/null || true"
            )
            return getattr(result, "stdout", None) or ""
        except Exception:
            return ""

    _run_output: str = ""

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """Report token usage and cost, parsed from drip's own inference lines.

        Without this, harbor has nothing to aggregate and every token/cost
        column in the job table renders as "-".
        """
        prompt = cached = completion = 0
        cost = 0.0
        priced = True
        by_model: dict[str, dict[str, int | float]] = {}

        for match in USAGE_RE.finditer(self._run_output):
            call_prompt = int(match["prompt"])
            call_cached = int(match["cached"])
            call_completion = int(match["completion"])
            prompt += call_prompt
            cached += call_cached
            completion += call_completion

            per = by_model.setdefault(
                match["model"],
                {"calls": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0.0},
            )
            per["calls"] += 1
            per["input"] += call_prompt
            per["cached"] += call_cached
            per["output"] += call_completion

            rates = PRICING.get(match["model"])
            if rates is None:
                priced = False
                continue
            # `prompt` is the full input count and `cached` the part served from
            # cache, so the uncached remainder is what bills at the full rate.
            call_cost = (call_prompt - call_cached) * rates["prompt"]
            call_cost += call_cached * rates["cache_read"]
            call_cost += call_completion * rates["completion"]
            cost += call_cost
            per["cost_usd"] = round(float(per["cost_usd"]) + call_cost, 6)

        if not prompt and not completion:
            return

        context.n_input_tokens = prompt
        context.n_cache_tokens = cached
        context.n_output_tokens = completion
        if priced:
            context.cost_usd = round(cost, 6)

        # drip's per-role routing is invisible in a single model column, so keep
        # the real split (which model did the planning vs the implementing).
        context.metadata = {
            **(context.metadata or {}),
            "usage_by_model": by_model,
            "roles": self._role_routing(),
            "skills": list(DEFAULT_SKILLS),
        }

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        goal = shlex.quote(instruction)
        skill_flags = "".join(f" --skill {shlex.quote(s)}" for s in DEFAULT_SKILLS)
        try:
            result = await self.exec_as_agent(
                environment,
                command=(
                    "set -o pipefail; "
                    "mkdir -p /logs/agent; "
                    f"{REMOTE_BINARY} {goal}{skill_flags} 2>&1 | stdbuf -oL tee {LOG_FILE}"
                ),
                env=self._forwarded_env(),
            )
            self._run_output = getattr(result, "stdout", None) or ""
        except Exception:
            # A non-zero exit raises, and the exception carries only a truncated
            # stdout — so recover usage from the tee'd log before re-raising,
            # otherwise every failed trial reports no tokens or cost.
            self._run_output = await self._read_log(environment)
            raise

        if not self._run_output:
            self._run_output = await self._read_log(environment)
