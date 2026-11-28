# Changelog

## 4.0.2

### Fixes

- **Parallel `bv` commands no longer crash on Windows now and then.** When one `bv`
  call deleted its lock file, another call could get `PermissionError` instead of
  `FileExistsError` and stopped with a traceback. `bv` now waits and tries again,
  as it does for a busy lock.

## 4.0.1

### Fixes

- **A review is no longer counted for a commit made later in the same second.**
  `bv` and the hooks wrote times in whole seconds, so a reviewer stop and the next
  commit could get the same time. `bv stage final start` and `bv finish` then took
  the old review as a review of the new code. Times now have milliseconds.
  Older `state.json` files with whole seconds still load.

## 4.0.0

Live runs on Claude Code 2.1.332 showed that the run could stall or close dishonestly.
This release fixes how `bv` reads agent answers and makes stage order and repair limits strict.
It also closes the escape hatches that the model could open by itself:
the user's consent was only a line in the phase rules, and now the hooks and `bv` check it.

### Breaking changes

- **Stage order is enforced.** `bv stage <id> start` refuses to jump over a stage
  that has not run. Only `manifest` and `briefing` can be skipped.
  A scripted run that started `build` right after `spec` must now start `plan` first.
- **Repair rounds are capped for the whole run.** `bv tickets` refuses a third `F` ticket
  in the same stage (code review or final). Re-entering the stage does not reset the count.
  Only `bv reopen` (a new brief) starts over.
  When the limit is reached, close the run honestly with `bv finish --not-accepted`.
  One more round is possible only when the user asks for it: `bv tickets --extra-round "<reason>"`,
  once per stage, not in mode `full`. The reason goes into the report.
- **An audit finding is accepted only on the reviewer's word.** `bv audit --accept <id>` refuses
  unless the last reviewer answer in the journal that names the id is a `DROP` line.
  The spot review format has a `DROP` line for this.

### Security

- **Escape hatches need the user.** The PreToolUse hook asks Claude Code to show the user these calls for approval:
  `bv set guard=off`, `bv init --force`, `--use-real-env`, `bv tickets --extra-round`,
  and in modes semi/interview also `bv set … --reason` and `bv commit … --fewer-tests`.
  In mode `full` nobody is asked: `--use-real-env` and `--extra-round` are refused,
  `--reason` and `--fewer-tests` pass and go into the report. A subagent cannot run any of them.

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
- When the journal has no answer from a checker, the error now points to `bv doctor`.

### Documentation

- Phase guides `6-review.md` and `7-final.md` describe the new repair limit and the extra round.
- `SKILL.md`, `3-spec.md`, `5-build.md`, `agents/bv-reviewer.md` and `README.md` describe
  which calls the user approves and how an audit finding is accepted.
- Updated workflow diagram (`docs/workflow.png`) and `software-architector-workflow.html`.

### Tests

13 new tests: in `tests/test_bv.py` handback answers, empty second stop, stage jumps,
step back, the repair limit and the extra round; in `tests/test_guard.py` the escape hatches;
in `tests/test_audit.py` acceptance on the reviewer's `DROP`.
`tests/test_delivery.py` follows the new stage order.

### Upgrade

```
claude plugin update software-architector
```

Restart Claude Code after the update. Sessions that are already open
still run hooks from the old plugin copy.
