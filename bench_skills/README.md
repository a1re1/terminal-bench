# Benchmark skill pool

Uploaded to `$DRIP_HOME/skills` (i.e. `/opt/drip/skills`) in each task container
by `drip_agent.py`. Deliberately separate from `~/.drip/skills`, which holds
shipping-oriented skills like `navis` — those end in "commit, push, and open a
draft PR", which is wasted effort in a terminal-bench container that usually has
no git remote and often no repo at all.

Put skills here that help with benchmark tasks themselves. One directory per
skill, each with a `SKILL.md` carrying the usual frontmatter.

Empty (README only) means drip runs with no skill pool, which is the behaviour
the 2026-09-05 runs measured.
