# Phase 1 — Manifest

## 1. Secret filter — before anything is written
Scan the user's text for: `sk-…`, `sk_live_/sk_test_/rk_live_/pk_live_`, `ghp_/gho_/ghs_/github_pat_`, `AKIA/ASIA`, `AIza/ya29.`, `xoxb-/xoxp-`, a Telegram bot token (8–10 digits `:` 35 chars), a JWT `eyJ…`, `scheme://user:pass@host`, `-----BEGIN … PRIVATE KEY-----`, ≥ 32 hex/base64 chars next to key|token|secret|password.
On a hit: the value becomes `[REDACTED:<VAR_NAME>]` everywhere, the name goes into `.env.example` with an empty value, and the user gets one line advising to revoke the key. Never echo the value. (Hooks also block writing it — that is the safety net, not the method.)

## 2. The brief, verbatim
File `brief-<YYYY-MM-DD>.md` in the run folder:
```markdown
# Original task
> Recorded <date>. The text is never edited: it is the reference the result is checked against.

<the user's whole text verbatim, secrets redacted>

## Additions
```
The top never changes. Everything the user says later goes to `## Additions` as `- <date> — "verbatim quote"`. A brief of another day is a new file with that day's date in the same folder.

Both independent checkers read the **brief**, not the manifest: a change that never reached the brief does not exist for them.

## 3. Atomize into `manifest.md`
Fixed column order — `bv` parses it and fails loudly with the line number on a bad row:
```markdown
| ID | From the brief (verbatim) | Status | Basis | Where |
|---|---|---|---|---|
| R01 | "verbatim quote" | open | | |
```
- One row = one thing that is independently true or false. A long brief usually gives 25–50 rows; fewer than 10 means retelling instead of atomizing.
- `|` inside a quote is written `\|`.
- **Scenarios.** `S##` = one whole path of a user from first step to last result, in user words ("opened the bot → left a request → the manager sees it in the report"). Every product has at least one, the main path: G1 fails without it. Each distinct way the product is used gets its own row. A library or a script has them too: "called the function from my code → got the result". They prove the parts work together; an `S##` is removed only the way a requirement is.
- IDs: `S##` scenario · `R##` from the brief (untouchable) · `R##i` implied by the brief (goes to briefing as a question; in `full` → `ASSUMPTION`) · `R##.n` deepening (phase 3) · `G##` the user's later words (untouchable) · `A##` a new capability (needs a parent `R##`; count of `A` ≤ `R`+`G`; forbidden on `strict`) · `D##` a constraint proven by the build (only via repair; not a requirement).
- Statuses: `open` → `in-spec` (you) → `in-ticket` (`bv tickets`) → `done` / `placeholder` (`bv commit`, never by hand); also `deferred` (you; the reason in "Basis" is required, plus a line in the spec's "Out of scope"; goes to the report) and `dropped` (**only the user**, with the quote in "Basis"). A row is never deleted: `bv` remembers every ID and refuses the delivery if one vanishes.

Report in one line: "Split the task into N requirements, M of them need clarifying" — no table in chat.

Then `bv stage briefing start` and read `phases/2-briefing.md`.
