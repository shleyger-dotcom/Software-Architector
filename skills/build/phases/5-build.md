# Phase 5 — Build

Same in every mode, no human involvement (except rule 4).

## Launching
`bv status` shows "Ready to launch": tickets whose dependencies are committed (a dependency with `Review: yes — foundation` only once its review passed, `done`) and whose zones do not clash with tickets in flight, at most 3 in flight (spot reviewers do not count).

One message = `bv ticket 02 03 start` (Bash first) + one Agent call per ticket, `subagent_type: "software-architector:bv-executor"`, `description: "Ticket 02: <title>"` (the panel files the agent under its ticket by this number, and the guard lets it write only that ticket's zone), `run_in_background: true`, `model: "sonnet"` for "regular" (omit for "strong"). `bv ticket … start` refuses on unmet dependencies, the parallel limit or a zone clash — do not work around it.

The wave is not a barrier: when a ticket returns, first launch the next ready ticket, then process the returned one. Waiting for executors = ending your turn (the background agent wakes you); never spawn agents or loop just to wait.

## Executor prompt — paths, not contents
The ticket file · `spec.md` + the named section headings · `interfaces.md` · `notes.md` · `research.md` (if it exists) · `reference.md` (if it builds a surface) · the check command and the one-test-file command · its zone and the zones of tickets in flight · variable names (never values) · any red the last check showed outside its zone.

## After each ticket — strictly in order
Two tickets returning together are processed one by one, each through the whole list.
1. Check the contract: `bv contract <id> --from -` (paste the block on stdin). It refuses a missing field, > 25 lines, a bad `STATUS`, `DONE` with red tests or without red proof, a requirement of the ticket left unreported (sub-rows `R01.n` of a listed `R01` included); on success it prints the `--tests P/F` for step 5. `bv commit` refuses without an accepted contract and on `BLOCKED` / `NEEDS_CONTEXT`. No block → ask for it. `BLOCKED` / `NEEDS_CONTEXT` / CONCERNS "the plan does not hold" → repair (below). Two `NEEDS_CONTEXT` per run = tickets are sliced too thin.
2. `git status --porcelain`: changes outside the zone (except `.env.example` and zones in flight) → ask the executor or get a reason. A file outside every zone is never committed, and `bv verify` catches what depends on it.
3. Append its `INTERFACES` to `interfaces.md`.
4. `DONE` with new criteria and no new tests in `FILES` → ask.
5. `bv commit <id> --tests <passed>/<failed>` with the Bash tool's `timeout: 600000` (`checkTimeout` above 540: `run_in_background: true` instead) (+ `--also <path>` for lock files the ticket was allowed to change). It **runs the check command itself**: red, killed on timeout, zero tests or a count it cannot read = no commit, and it prints the tail of the output. Then it **runs the code audit itself** on the files this ticket changed (the mechanical scan of `bv audit`): a `blocker`/`high` finding = no commit. Both run on every commit, and one refusal names everything both found, so one repair fixes it all. **Read it.** Its own red or finding → repair; you never fix it yourself. A finding you believe is false: show it to a spot reviewer, and only on its DROP `bv audit --accept <id> --note "<its reason>"`. Red in the zone of a ticket in flight → that ticket's unfinished work: `bv` says `blocked-by-other-ticket` when every file the output names lies outside this ticket's zone; that is no repair: process other returns and repeat this step when the other ticket is back. `bv commit` runs one check at a time (another commit's check running → it waits up to a minute, then asks to repeat). Fewer tests than before → repair, unless the reason is real: `--fewer-tests "<reason>"` goes to the report. Limitation: the check and its test count are project-wide, so the count includes uncommitted tests of tickets in flight; "fewer than before" compares project totals, not this ticket's own tests. On green it commits exactly the zone with trailers and closes requirements whose every ticket is done; the count it read from the runner outranks `--tests`; placeholders come from the contract.
6. If the ticket has `Review: yes`, `bv commit` left it in `review`; a ticket without it whose requirement was not fully met goes there by `bv ticket <id> review` (it needs the ticket's commit). Then launch `software-architector:bv-reviewer` in the background with `description: "Review of ticket <id>"` (only a reviewer whose description names this ticket counts for it), mode spot (ticket, spec sections, `interfaces.md`, TESTS/REDPROOF lines, why it is under review, the commit hash), and move on. Review passes → `bv ticket <id> done`; it refuses until the journal shows the reviewer finished after that commit, and it closes only tickets in `review`.
7. Bookkeeping: placeholders → `bv add placeholders "…"`, new variables → `bv add emptyEnv NAME`, delivered `A##` → `bv add additions "…"`, craft doubts → `bv add concerns "…"`; project memory only for an expensive-to-discover fact (real check command, a trap).
8. One line to the user: "The bot takes requests — 3 of 8 done".

## Repair
| | Shortfall (could, did not) | Failure (tried, could not) |
|---|---|---|
| What | red check, code audit finding, reviewer BLOCKING, criterion met only by the letter | `BLOCKED`, `NEEDS_CONTEXT`, a failed repair |
| Where | **the same executor** by message (SendMessage), context intact | **fresh context on the strong model** |
| What to send | the condition, nothing more | the ticket again + the error + the named failing test + the explicit path |

- Build the message with `bv handoff <id> --note "<condition>" --tail -` (last 10 lines of output on stdin, secrets masked): the ticket path, files to read, zone, condition, the repair line. The same text goes to the executor by SendMessage **or** into the prompt of a fresh one — one package, two routes.
- Route: «Shortfall» by SendMessage only when `resume=yes` (`bv status`/state). `resume=no` or unset, or the message fails → the fresh-context route with the same package. Never rely on the old context being alive.
- `bv ticket <id> repair --note "<condition>"` before sending (limit 2 — then it is a failure). Only reviewer BLOCKING, a red check and a code audit refusal become a repair, verbatim; FINDINGS → `bv add concerns`. A repair is a patch of the named defect, never a rewrite of the ticket: the next `bv commit` runs the whole check and the whole audit again, so a patch that breaks something else is caught there.
- Repair message: the failing test + last 10 lines of output + "State the cause in one line, then fix it. Touch nothing else. Return the contract again."
- After a repair of a reviewed ticket: `bv commit <id> --tests … --repair` ("T03: repair after review") → it returns to `review` for **one** re-check; still not closed → `bv add concerns`, fixed in F1.
- If a ticket in flight touches the zone, the repair waits.
- If the harness cannot continue a subagent: a fresh context with the `bv handoff` package (the harness = Claude Code, which runs the subagents).
- Failure: `bv ticket <id> retry` → fresh context, strong model, with the error. The second retry only **with a changed approach**. Then `bv ticket <id> fail --note "<cause>"`, the cause into "Basis" of its rows, one sentence to the user. Never bypass the blocker or silently narrow the ticket. Wave neighbours keep flying.
- `BLOCKED` on a dependency is not a failure: within an accepted decision → add it to "Implementation decisions" and relaunch allowing exactly that install (manifest and lock files go into the commit via `--also`; nothing else installing in parallel). A new decision (paid service, heavy framework, another stack) → ask the user (semi/interview) or `ASSUMPTION` (full).
- **The build contradicts the plan** — you decide, not the executor: (1) fix the spec section with a note, (2) add a `D##` row, (3) rebuild only what is affected (done tickets stay; pending ones get updated references; a ticket that lost its meaning is removed and its rows go back to `in-spec`; `bv tickets`), (4) one line to the user in any mode. `D##` never removes a requirement (impossible → question / `ASSUMPTION` + placeholder) and is not a route for your own idea (that is `A##`).

## End of build
All tickets `done` or `failed` (name the dependants left waiting), spot reviews finished, their repairs merged. A ticket still in `review` whose spot review passed: `bv ticket <id> done` first (`bv stage review start` refuses while any ticket is not `done`/`failed`).

Then, in the same turn as the last ticket's step 8, without waiting for the user: `bv stage review start` and read `phases/6-review.md`.
