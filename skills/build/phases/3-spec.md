# Phase 3 — Spec

`bv stage spec start` (fails if G1 does not pass — fix the manifest).

The phase does not reopen the interview: unresolved → `placeholder`. Single exception: a real fork into different projects — one question with a recommendation (`full`: decide, record `ASSUMPTION`).

## 1. Scout and research first — one message
**Existing code** → launch subagent `software-architector:bv-scout` with: the repo, the post-landing commit list (phase 0), and the `notes.md` path in the run folder. It writes where things are, conventions, commands, exact names of entities/fields/functions.

**Anything outside the repo that changes over time** (an external API or service, a library to install, a rule of a platform: a store, a messenger, a payment provider) → launch `software-architector:bv-research` in the same message with: today's date, the `research.md` path in the run folder, and the questions, one line each: the thing by name · what the spec must know (current version, the exact calls the task needs, limits and price tier, licence) · the version pinned in the repo's manifests, if any. Questions carry names of technologies only: never the user's words or data. Nothing external (standard library only) → skip it.

`research.md` outranks your memory: versions, package names and call names in "Implementation decisions" come from it. `unconfirmed` there → a line in "Open points". A thing found shut down, paid or under a copyleft licence is a fork: one question with the free alternative recommended (`full`: decide, record `ASSUMPTION`).

## 2. `spec.md`
Small product (T0/T1 expected): Task · Solution (one line per live requirement) · Implementation decisions · Out of scope · Open points.
Otherwise: Task · Solution · Stories (table `# | Label | Story | Acceptance`) · Implementation decisions (each with "why"; name every dependency to install) · Boundaries and seams (→ `interfaces.md`) · Out of scope · Open points.
A product with a network, accounts, files or a database also gets a short `## Security` section: who may call what, which data is sensitive, where trust ends (every input from outside), how secrets are kept. The tickets copy its points into their acceptance criteria; the audit and the reviewer check against it in phase 6.
No "which section covers which requirement" table — that is the manifest's "Where" column.
If `CONTEXT.md` / `GLOSSARY.md` / `docs/adr/` exist, speak their words and call out contradictions aloud.

**Deepening pass** — for each requirement, each dimension becomes an `R##.n` row with its own acceptance, or "not applicable": First run · Empty · Wrong input · Failure · Interruption · Growth · Boundaries · Aftermath. `strict`: only what the requirement cannot work without (errors and empty states — yes). `deep`: all of them.

## 3. `interfaces.md` — before the first ticket
- "Boundaries decided in the specification": per unit — what it owns, what it exposes (names and signatures), what it hides; seams for tests.
- Project rules: stack and versions, how to run one test file, what not to touch, and three commands, each also recorded in `bv`. They run in Git Bash from the project root and must work with nothing installed but what `setupCmd` installs:
  - **install**: `bv set setupCmd="<command>"` (leave it unset if the project installs nothing). It installs from the internet and its package names come from `research.md`, i.e. from web pages, so in **every mode, `full` included** (rule 4): show the user the exact command in one line and set it only after an explicit "yes"; no answer → leave it unset and say so;
  - **one check** (tests + types + lint): `bv set checkCmd="<command>"`; no watch mode, exits by itself;
  - **end-to-end**: `bv set e2eCmd="<command>"` runs the `S##` scenarios against the real product from outside (browser automation for a site, HTTP requests for an API, real invocation for a CLI or a script). Name its tool in "Implementation decisions", so ticket 01 installs it. It may start the product itself and must stop what it started.
  `bv set` refuses a command the guard would block in the shell (`git push`, reading `.env`, `rm -rf` outside the project, …) or one holding a secret; `bv` runs the three with a minimal environment (system paths, `HOME`, `TEMP`, `LANG`, `CI=1`), never the session's keys.
  Build refuses to start without `checkCmd`, and without `e2eCmd` while `S##` rows are live. Commands that need more than 9 minutes: `bv set checkTimeout=<seconds>`; above 540 s one foreground Bash call (600 s) cannot hold them, so from then on run `bv commit` and `bv verify` with `run_in_background: true` and read their output when they finish (if `bv` itself is stopped, it stops the check too).
  A check or scenario run is green only with exit 0 **and** a test count `bv` can read (unittest, pytest, jest, vitest, mocha, cargo). Another runner: `bv set testsPattern="<regex, group 1 = passed count>"`, otherwise every run of it is red. From `bv stage build start` on, `setupCmd`, `checkCmd`, `e2eCmd` and `testsPattern` are frozen: a change needs `--reason "<why>"` and goes into the report.
- Existing code: run the check once now. Already red before this run → no ticket could ever commit, so tell the user in one line and make repairing it ticket 01.
- "Agreed" from the last run verbatim.
Later only you append each finished ticket's `INTERFACES` here (never executors — parallel writers collide). Two tickets declaring the same interface = a plan defect.

## 4. G2 — coverage
1. Own pass: every `open` row → `in-spec` (section name in "Where") or `deferred` (plus an "Out of scope" line).
2. Launch `software-architector:bv-coverage` with exactly: all brief files of the run (oldest first) and `spec.md`. The hook keeps it out of the rest of `.software-architector/`.
3. For each item: missing/half → write the section (`resolution: fixed`) or defer it (`deferred`); extra → attach it to a row (`attached` with `"row": "A01"`, the row must exist; `bv` refuses otherwise; on `strict` `A##` rows are forbidden, so there it is cut) or cut it (`cut`). Only a capability is ever cut: validation and error handling that protect a live requirement stay in the spec whatever the check says.
4. Record: `bv gate G2` with one `resolution` per item, each keyed by the item's exact `quote`, via stdin. `bv` takes the items themselves from the agent's final answer in the hooks' journal, never from you: an item you leave out or whose quote you change stays unresolved.
```
bv gate G2 <<'EOF'
{"items": [{"quote": "…", "resolution": "fixed"}, {"quote": "…", "resolution": "attached", "row": "A01"}]}
EOF
```

## 5. Show
Two lines to the user: what will be built and what will not. Then `bv stage plan start` and read `phases/4-plan.md`.
