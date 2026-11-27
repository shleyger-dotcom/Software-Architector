# Phase 7 — Final

"Delivered" is decided by `bv finish` from facts, and only there. Red at any step below is not a line for the report: it is work.

## 1. Clean clone (G5) — before anything expensive
Phase 6 left G5 green on HEAD; run it again only when the code moved since (`bv gate G4` and `bv finish` say so). `bv verify` with the Bash tool's `timeout: 600000` (longer projects: `run_in_background: true`). It clones HEAD into a temp folder, drops `.software-architector`, runs `setupCmd`, `checkCmd` and `e2eCmd` there and reads the exit codes. No model is involved. It sees only what was committed: a file outside every zone, a dependency installed by hand or a setting that lives only in this folder breaks here, not at the user's. The clone's `.env` is the committed `.env.example` plus `CI=1`, as a stranger would have it. Only if the project cannot start without real values and the user agrees: `bv verify --use-real-env` (and `bv blind-clone --use-real-env`) copies the real `.env` unread; `bv` writes that into the report.

Red → **Repair round** (below).

## 2. Blind acceptance (G4) and ADR — one message
- Before launching: `bv blind-clone` prints the clone path. It is HEAD's files without `.software-architector` and without git history (a raw clone carries the spec and manifest in its commits, and `git log -p` would show them). It replaces the previous blind clone; `bv finish` removes it. The hook lets `bv-blind` write only inside it and keeps it out of the working folder.
- `software-architector:bv-blind`: the clone path (not the project path), all brief files of the run (with "Additions") and the list of live requirement IDs with their quotes, `S##` scenarios first. The hook keeps it out of `.software-architector/`.
- T2+: `software-architector:bv-adr` in the same message: `spec.md`, `manifest.md`, `interfaces.md`, `notes.md`.

Record the blind result: `bv gate G4`, with no input. `bv` reads the verdict itself from the agent's final answer, which the SubagentStop hook wrote to the journal: a verdict that passed through you is no longer independent, so `bv` refuses `--from` and stdin here. It also refuses without a green G5 on this very commit and without the journal showing `bv-blind` finished after it. An answer that is not the JSON block → ask the same agent to return only the block, then `bv gate G4` again. Reading the mismatches:

| Manifest | Blind | Meaning |
|---|---|---|
| done | implemented | agreement |
| done | partial / no | 🔴 drift — the manifest is wrong: **Repair round**, never a manual status edit |
| done | partial, only because a real external service could not be exercised | a ticket `F<n>` that documents the user's manual check, its contract reports the row `placeholder`, and `bv commit F<n> … --external "<service and what the user checks>"`: `bv` refuses the downgrade without it, moves the row itself (never by hand) and writes the reason into the report; in `full` the report also says the user was not asked and must confirm it. Not an external service → it is drift: **Repair round** |
| placeholder | partial | expected — the placeholder is visible, not an invented fact |
| placeholder | no | 🔴 the placeholder is not there: **Repair round** |
| dropped / deferred | no | expected, in the report |
| — | implemented, not from the brief | scope without a parent → report |
| — (a brief line with no ID, quoted as its id) | partial / no | 🔴 a requirement the manifest lost: a `G##` row in `in-spec` and a **Repair round**; `implemented` → kept in the gate as `unmapped`, into the report |
| `outcome.solved: false` | | 🔴 the parts work, the task is not solved: **Repair round** |

A project that cannot be started at all → **Repair round**.

## Repair round
1. The findings verbatim as acceptance criteria of one ticket `F<n>` (next free number), the way phase 6 builds `F1`; `bv tickets` → start → executor → `bv contract` → `bv commit`.
2. Any commit makes G5, G4, the audit and the code review stale, and `bv finish` says so. The repair is a patch of the findings only; the checks are all repeated, from the first: the re-check by `bv-reviewer` (`description: "Review of ticket F<n>"`, which `bv ticket F<n> done` also needs), `bv audit`, then section 1, with a fresh clone for the blind check.
3. **At most 2 rounds.** Still red, or the cause is outside the code (an account, a key, a fact only the user has) → stop repairing and close honestly: section 5 with `--not-accepted`.

## 3. Project memory — after the blind check (it needs verified commands)
`software-architector:bv-memory`: the memory file and owner (from `bv status`/state), `interfaces.md`, the tier, the blind agent's verified commands.

## 4. `.software-architector/PROGRESS.md` — rewritten whole from re-read files, ≤ ~60 lines, marker `<!-- software-architector:progress -->`
"Agreed — do not revisit" (dropped with quotes, lasting preferences from "Additions", confirmed assumptions) · "Unfinished" (deferred, failed tickets, coverage items not built) · "Known bugs" (🔴 from the blind check, unfixed concerns/report) · "Edits without Software-Architector" (empty; ordinary sessions fill it). Empty section = "—".

## 5. Land
`bv finish --result "<one line: what now works>"`. It checks by itself: tickets exist and none failed, G1–G5 green, G4 and G5 made on this very commit, every live requirement delivered and none erased, the final stage reached through the review. Then it closes stages, removes `--wip` and commits "Software-Architector: build delivered" (subject is a contract: the next run and ordinary sessions find the landing by it).

It refuses and lists what is unproven → fix the list. Never edit the state or the manifest to get past it.

Only after 2 repair rounds, or when only the user can unblock it: `bv finish --not-accepted --result "<what really works now>"`, which commits "Software-Architector: build closed without acceptance" and keeps the list. In every mode this is the first thing the user hears.

If it prints "left uncommitted", those files go into the report — never commit them yourself.

If the memory owner is the user: one question "Apply the proposal in <file>?" (not asked in `full`). Yes → append it as a marked block at the end of their file.

## 6. Report — in English, plain words, built from re-read files, never from memory
The first line is the verdict, word for word from `bv`: "Build delivered", "Build delivered, except: <rows>" (say in plain words what those rows are and how the user checks them) or "Build closed WITHOUT acceptance", then one sentence why. Never soften the last two.

Sections, in this order (an empty section is omitted, except the two marked *):
1. **What blocks acceptance** (only when not accepted) ← `problems` from the state, in plain words, with what is needed from the user
2. **Decisions made for you** (first in `full`) ← `ASSUMPTION` rows / `lists.assumptions`
3. **Done** ← the blind verdict, not the manifest's `done`; the `S##` scenarios first
4. **How it was checked** ← the G5 steps with their commands and test counts, one line. Name what was not exercised for real (a stubbed payment, a test account)
5. **What is needed from you** * ← placeholders + empty variables (`lists.placeholders`, `lists.emptyEnv`); if none: "everything is filled in"
6. **What was left out** * ← `deferred` / `dropped` with quotes
7. **What was added on top** ← `lists.additions`
8. **What went off plan** ← `D##` rows and failed tickets
9. **What remains after review** ← `lists.report`, the audit summary (findings left, accepted ones with their notes, tools that were not checked)
10. **Open questions** ← unresolved coverage items
11. **Project memory** ← the proposal, if the file is the user's
12. **How to run** — the verified commands · **Where things are** — 3–5 pointers
13. **Receipt** * ← the `Receipt` block that `bv finish` printed, verbatim in a code block: changed files, tickets with their check and audit, gates, audit. Never retype or shorten it. One line after it: the receipt of every stage is in `receipts.md` of the run folder

Secrets only by name; no diffs, file lists or test names outside the Receipt. The Receipt is the one exception to "never paste logs" in SKILL.md.
