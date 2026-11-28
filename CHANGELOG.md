# Changelog

## Unreleased

The model could open the escape hatches of `bv` by itself: the user's consent was only a line in the phase rules.
Now the hooks and `bv` check it.

### Changes

- **Escape hatches need the user.** The PreToolUse hook asks Claude Code to show the user these calls for approval:
  `bv set guard=off`, `bv init --force`, `--use-real-env`, `bv tickets --extra-round`,
  and in modes semi/interview also `bv set … --reason` and `bv commit … --fewer-tests`.
  In mode `full` nobody is asked: `--use-real-env` and `--extra-round` are refused,
  `--reason` and `--fewer-tests` pass and go into the report. A subagent cannot run any of them.
- **An audit finding is accepted only on the reviewer's word.** `bv audit --accept <id>` refuses
  unless the last reviewer answer in the journal that names the id is a `DROP` line.
  The spot review format has a `DROP` line for this.
- **One more repair round, only on request.** `bv tickets --extra-round "<reason>"` allows a third `F` ticket
  in a stage, once per stage, not in mode `full`. The reason goes into the report.
- When the journal has no answer from a checker, the error now points to `bv doctor`.

## 4.0.0

Live runs on Claude Code 2.1.332 showed that the run could stall or close dishonestly.
This release fixes how `bv` reads agent answers and makes stage order and repair limits strict.

### Breaking changes

- **Stage order is enforced.** `bv stage <id> start` refuses to jump over a stage
  that has not run. Only `manifest` and `briefing` can be skipped.
  A scripted run that started `build` right after `spec` must now start `plan` first.
- **Repair rounds are capped for the whole run.** `bv tickets` refuses a third `F` ticket
  in the same stage (code review or final). Re-entering the stage does not reset the count.
  Only `bv reopen` (a new brief) starts over.
  When the limit is reached, close the run honestly with `bv finish --not-accepted`.

### Fixes

- **Checker verdicts are read on the first attempt.** In Claude Code 2.1.332 a subagent
  ends with a `SubagentHandback` tool call, and `SubagentStop` can arrive without a message.
  The hooks now save the `SubagentHandback` answer to the journal.
  `bv gate G2` and `bv gate G4` take the verdict from it, so the orchestrator
  no longer has to repeat the coverage check or the blind acceptance.
- **The VS Code panel shows the agent's final answer.** An empty second `SubagentStop`
  no longer hides the answer that the agent handed back.
- **A step back to an earlier stage is shown correctly.** The later stage returns to
  "waiting" instead of being marked "done".

### Documentation

- Phase guides `6-review.md` and `7-final.md` describe the new repair limit.
- Updated workflow diagram (`docs/workflow.png`) and `software-architector-workflow.html`.

### Tests

6 new tests in `tests/test_bv.py`: handback answers, empty second stop, stage jumps,
step back, and the repair limit. `tests/test_delivery.py` follows the new stage order.

### Upgrade

```
claude plugin update software-architector
```

Restart Claude Code after the update. Sessions that are already open
still run hooks from the old plugin copy.
