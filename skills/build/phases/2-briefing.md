# Phase 2 — Briefing

If mode is `interview` or depth is `deep`: first run the adversarial pass (below).

## full — self-briefing
`bv stage briefing skip --note "full auto — self-briefing"`. Decide every open row yourself:
- "Basis": `ASSUMPTION — decided for the user: <decision>` and `bv add assumptions "<decision in plain English>"`.
- A paid or account-bound service becomes an adapter: interface + local stub + an empty variable name in `.env.example`. Its rows end as `placeholder`, not `done`: nobody in the run may exercise it for real, so the user checks it once by hand.
- No `open` rows may remain (G1 in `full`): each decided row becomes `in-spec` here, before `bv stage spec start`.
Then go to phase 3.

## semi / interview — rounds of questions
- Up to 4 numbered questions per message; each with your recommended answer, so "yes" accepts all.
- Every question closes a specific manifest row. A question without a row is dropped.
- Facts you look up; decisions you ask. Blocking external things (payment, hosting, accounts, data) go in the first round. Secrets never — ask "which provider / do you have an account".
- `semi`: ask only forks that lead to different products. `interview`: every fork.
- A decision returns to the user if it costs money or locks to a vendor, changes what the user sees, cannot be undone by an edit, or encodes a business rule (prices, deadlines, permissions, data fate). Everything else (error texts, retries, defaults, naming) you decide in any mode.
- "I don't know" → the row stays live, the missing fact goes into "Basis", the code gets a visible placeholder.
- `reference.md` in the run folder: only what the user gave (sites, screenshots, "like in a bank"). Not a requirement; it goes to executors building a surface.

After each round: update "Basis", `dropped` with the quote, new `G##` rows; every statement that cancels/adds/changes something goes verbatim into the brief's `## Additions`.

No questions at all → "No questions — writing the specification" + `bv stage briefing skip --note "no questions"`.

## Changing the task later (any phase)
(1) brief `## Additions`, (2) manifest, (3) plan: a ticket / `deferred` / a report line explaining the impact. A new `G##` travels the whole path: story in the spec → row `in-spec` → ticket file → `bv tickets`.

## Adversarial pass (interview, or depth deep)
Seven questions to the task, not to the person:
1. **Failure** — what does failure look like for the user?
2. **Collision** — which two requirements contradict each other?
3. **Unchecked** — which assumption is everything resting on?
4. **Cost** — what costs money, time or quotas, and who pays?
5. **Condition** — what must be true outside the code (accounts, data, access)?
6. **Second week** — what breaks once there is real data and real users?
7. **Second actor** — who else touches the system (admin, attacker, another service)?

Normal yield is 5–7 findings (fewer than 3 on a real project = going through the motions). Route each: a question (interview) / `ASSUMPTION` (full) / decided into the spec / an `A##` row / "Out of scope". The pass removes nothing: no `dropped`, no silent narrowing.

## Close
G1 is checked by `bv stage spec start` (no `open` without a recorded reason; in `full` none at all). One line to the user, then read `phases/3-spec.md`.
