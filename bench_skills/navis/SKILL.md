---
name: navis
description: Full delivery workflow for one goal — implement it completely, run declared verification, perform an independent review against the original goal, triage and fix every valid P0/P1 finding with re-verification, then report the result, all without further prompting and without using git
roles:
  default: author
  planning: planner
  implementation: author
  review: reviewer
  triage: planner
  fixes: author
---

# navis — full delivery skill

*Navis* (Latin: ship). One invocation takes a goal from empty water to a
finished, reviewed, verified result. Run the phases in order; never skip the
review, and never recursively invoke /navis.

This environment has no version control to lean on: do not run `git` at all —
no commits, branches, stashes, or `git diff`. Track what you changed by keeping
your own list of touched files, and read those files directly when you need to
see the change.

The `roles:` block above is **advisory** — it hints which role suits each phase;
it does not route anything by itself. Realize each role change through the task
list: give every task the role its phase names. An explicit role the user
assigned, or one a task already carries, always wins over these hints. Use only
roles that exist in the session's role configuration; if a hinted role is
unknown there, keep the configured default behavior instead of inventing a role
or granting extra tools. With other skills active, take phase hints from the
most relevant skill and surface real conflicts explicitly.

## 0. Capture the original goal

1. In the first cycle, save the user's goal verbatim into shared memory
   (remember, topic "navis-original-goal") before doing any other work.
2. Every later phase judges work against that verbatim text — never a
   paraphrase, never a later restatement. If the goal is ambiguous, state your
   interpretation up front and proceed; only stop to ask when proceeding would
   be destructive or unsafe.

## 1. Plan and implement fully

*Suggested role: planner for the plan_tasks step, then author.*

1. plan_tasks breaking the goal into small concrete tasks covering, at minimum:
   implementation, verification, independent review, and fixes.
2. Implement every task fully. Prefer the project's declared commands
   (package.json scripts, Makefile targets, Cargo.toml) over improvised ones.
3. Run the project's declared verification (tests, typecheck, build) and treat
   a passing result as done only when its output is visible in the transcript.
   Never bypass, stub, or weaken a failing check to make progress.
4. Keep changes scoped to the goal. If unrelated work tempts you, name it and
   leave it out unless the goal genuinely requires it.

## 2. Independent review against the original goal

*Suggested role: reviewer.*

1. After implementation and verification pass, add explicit review tasks
   (reviewer role, not author) that judge the actual changes against the
   verbatim original goal: correctness, completeness, regressions, and scope
   creep.
2. The reviewer must re-run the verification commands and read the real changed
   files end to end (the review-independently discipline). A review that only
   reads summaries does not count as a review.
3. If the host tool offers a non-interactive review entry point (for example
   `drip --review --context "<original goal>"` run against the working tree),
   prefer it once implementation tasks are complete. Never start a nested
   interactive session and never invoke /navis from inside a session.

## 3. Triage and fix

*Suggested role: planner to triage, author to fix.*

1. Triage every finding. Fix all valid P0 and P1 findings. If a P0/P1 finding
   is judged invalid, record exactly why in one line rather than silently
   dropping it.
2. For P2 and below: fix the cheap and safe ones; list the rest honestly in the
   final report as deferred.
3. After every fix batch: re-run verification and re-review the fixed files.
   A fix that has not been re-verified is not done.

## 4. Report the result

*Suggested role: author.*

1. Leave the work in place on disk — no packaging, no cleanup pass that reverts
   or moves files the goal asked for.
2. Report: the original goal verbatim, what changed (the list of touched files
   and what each change does), verification evidence, each review finding and
   its resolution, and any honest blockers or deferred items.
3. Report genuine blockers honestly (finish_task blocked) instead of declaring
   the work complete.

## 5. Done means

Implementation + passing verification + independent review + all valid P0/P1
findings fixed and re-verified + a final report covering all of it — each step
evidenced in this transcript. Anything less is reported as partial, with
exactly what remains.
