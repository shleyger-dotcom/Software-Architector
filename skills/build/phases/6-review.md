# Phase 6 — Branch review

Always, in every tier and mode. It starts by itself at the end of the build and runs to `bv stage final start` without waiting for the user.

You do not edit project code, not even a one-line fix, and not through the shell (the guard blocks it). Every FIX NOW goes through `F1` and an executor. A hook block is not a reason to stop or to find another route to the same edit: take the path in its message (executor, `bv`) and keep going; ask the user only when the message says so.

0. `bv audit`: the mechanical pre-filter (junk code, security patterns, dependency CVEs). It writes `audit.md` into the run folder and prints a summary. Without it `bv stage final start` refuses.
1. Launch `software-architector:bv-reviewer` (omit `model`: the agent file pins Opus, so the reviewer is never weaker than the executors it checks), mode **branch**: the `baseCommit` (from `bv status` / state), `spec.md`, `interfaces.md`, the deferred concerns from the state, the `audit.md` path. On T3 up to 3 reviewers in parallel, split by groups of zones.
2. It returns `FIX NOW / REPORT / DROP`.
3. All FIX NOW → **one ticket `F1`**: `tickets/F1-branch-fixes.md` with `Blocked by: —`, `Wave:` one after the last, `Zone:` the union of the files involved, `Review: yes — branch repair`, `Model: strong`, the findings verbatim as acceptance criteria, `Requirements:` the requirements they belong to. Every `blocker`/`high` audit finding is FIX NOW or a reviewer's DROP. Then `bv tickets` → `bv ticket F1 start` → one executor → full check → `bv commit F1 --tests …` → one re-check by the reviewer in repair mode (`description: "Review of ticket F1"`) → `bv ticket F1 done`. Whatever is still open after that goes to the report.
After `F1` is committed: `bv audit` again (the audit must describe the final commit). A DROP of a `blocker`/`high` finding: `bv audit --accept <id> --note "<the reviewer's reason>"`; the note goes into the report word for word. Never accept a finding you have not shown the reviewer.
4. REPORT → `bv add report "<plain English sentence>"`.
5. **Execution result**: `bv verify` (clean clone: setup, check, e2e), with the Bash tool's `timeout: 600000` (`checkTimeout` above 540: `run_in_background: true`). Red → a ticket `F<n>` the way `F1` is built, then steps 0–5 again from `bv audit`. At most 2 `F` tickets in this stage for the whole run, `F1` included: `bv tickets` refuses the third, and re-entering the stage gives none back. Then stop repairing and close honestly: `bv finish --not-accepted`.
6. One line to the user: "Code review: 7 findings, 4 fixed, 3 in the report".

No FIX NOW → skip F1. Then, in the same turn, `bv stage final start` and read `phases/7-final.md`. It closes the stage only on both gates, each on the current commit: a code audit (`bv-reviewer` finished after this stage started **and** after the last ticket commit; `bv audit` of HEAD with no open `blocker`/`high`) and an execution result (`bv verify` green on HEAD). Any fix in this stage makes all of them stale: they are repeated, not only the one that failed. This stage cannot be skipped. The review is finished only when `bv stage final start` has run; ending your turn before it, without a question to the user, is a stall.

## Changes the user asked for after the review started
They go through tickets (SKILL.md, «The user asks for a change»). When the last of them is closed, without being asked: stage `final` → `bv stage review start`; `bv audit`, launch the reviewer in mode **branch** with `git diff <the commit before the first change>..HEAD` instead of the `baseCommit`, then steps 2–6 and `bv stage final start`. The blind acceptance is done again from the top of `phases/7-final.md`.
