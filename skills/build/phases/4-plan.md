# Phase 4 — Plan

## 1. Size by volume
One executor ≈ 3 000 changed lines, tests included. Estimate in one line ("~8K lines: bot, admin panel, reports") and `bv set tier=T2`.

| Volume | Tickets | Tier |
|---|---|---|
| ≤ ~3K | 1 (no waves, no spot review) | T0 |
| ~3–6K | 2 | T1 |
| ~6–18K | 3–6 | T2 |
| ~18–36K | 7–12 | T3 |
| more | forbidden — split into two runs, tell the user |

A list of edits to existing code is usually T0.

## 2. Slicing
- Each ticket is narrow but complete: a path through every layer (data / logic / interface / tests).
- The shell / schema / shared primitives = ticket 01 alone. Ticket 01 installs **all** dependencies named in "Implementation decisions".
- **Scenarios**: an `S##` row goes into the ticket that completes the path; if the path crosses several tickets, into one ticket of the last wave that depends on them all and writes the end-to-end tests (zone: their own folder). On T0 it belongs to the only ticket. Criterion: `e2eCmd` runs that scenario from first step to last, and it is red on the starting commit.
- Correction pass (mandatory): merge neighbours on the same files; merge thin tickets into their natural parent; merge chains where A alone shows nothing. Payback test: merge until together < ~3K. Neighbour test: < 3 criteria and the same files = a checklist item, not a ticket.
- **Wide change** (a shared thing many zones use) — three green steps: **Expand** (new beside old, old still works; `Review: yes — foundation`, `strong`) → **Migrate** (one ticket per zone, one wave; criterion "in `<zone>` no references to `<old>` remain") → **Contract** (remove the old; deleting user data = `Review: yes — data deletion`). All tickets of the chain carry the same requirement.

## 3. Waves
`wave = 1 + max(wave of dependencies)`, none = 1. Then inside a wave separate by zones: overlapping paths → the later ticket moves to the next wave. Waves are discovered, not designed; a wave of one is normal.

## 4. Ticket files — `tickets/NN-<slug>.md`
```markdown
# 03 — Title
**Requirements:** R01, R01.1, A01
**Blocked by:** 01, 02
**Zone:** `src/bot/` · `tests/bot/`
**Wave:** 2
**Review:** no
**Model:** regular
## What must work
## From the brief, verbatim
## Spec sections
## Acceptance criteria
- [ ] each checkable, and false on the starting commit
```
Field names are a contract with `bv`. Dependencies: numbers or "—". Zone: paths in backticks. No code and no file paths in the body except schemas/state machines.
- `Review: yes` only for: the foundation (ticket 01 if others build on it; Expand) and own code for login/passwords/sessions/2FA, keys, money, deleting/migrating data. Format `yes — foundation` / `yes — <risk>`.
- `Model: strong` for every `Review: yes` ticket and for a hard algorithm / unfamiliar integration; everything else `regular`.
- Tickets of only-`A##` rows fly after tickets with `R##`.

## 5. Publish (G3) and commit the plan
`bv tickets` — checks the plan: ticket numbers unique (`01-a.md` and `1-b.md` are both 01), no `open` rows left, no rows vanished, every live requirement in a ticket, every ticket has existing requirements, a zone and a wave, dependencies exist and come in earlier waves, zones of one wave do not overlap. A zone is a plain relative path inside the project (a folder or a file): no `.`/`..`, absolute paths, masks (`*`, `?`, `[]`), git magic (`:(top)`), `.git` or `.software-architector`; on Windows `src/App/` and `src/app/` are one zone. Each `!` line is a plan defect: fix the files and repeat.

Then `bv commit plan` — the first commit of the run ("Software-Architector: build plan"), records `baseCommit`, refuses if a secret is staged.

## 6. Show
- `semi` / `interview`: one line per ticket — what the user will be able to do — and "Showing the plan and starting. Say 'stop' if something is wrong."
- `full`: the same as a notification, no waiting.

Then `bv stage build start` (fails without G2 and G3) and read `phases/5-build.md`.
