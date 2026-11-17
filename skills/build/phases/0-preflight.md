# Phase 0 — Preflight

Goal: the repo is ready, the run exists, the dashboard address is with the user.

## 1. Look around (read-only)
git state; `CLAUDE.md` / `AGENTS.md`; stack manifests (`package.json`, `pyproject.toml`, …); existing code; `CONTEXT.md` / `GLOSSARY.md` / `docs/adr/`; `.software-architector/PROGRESS.md` and earlier runs in `.software-architector/runs/`.

A dirty working tree: say so in one line and continue. Never stash, reset, clean or commit someone else's changes.

**The run lives in the folder this session was opened in.** Hooks and the panel find the run from the session's folder: in any other folder the guard is silently off (`git push`, reading `.env`, writing code all pass) and the journal stays empty. So never `cd` elsewhere to run `bv init`. If this folder is not the project (the user names another path, or it is an unrelated repository): tell the user in one line "Open the folder <path> in VS Code and run /software-architector:build <idea> there" and stop, creating nothing. Do not ask where to create the project: an empty session folder is the project.

## 2. Project memory file (not a question in any mode)
| On disk | memory file | owner |
|---|---|---|
| a file with `<!-- software-architector:start -->` | that file | software-architector (update between markers) |
| `CLAUDE.md` / `AGENTS.md` without markers | that file (`CLAUDE.md` if both) | user — never write it; a proposal at the end |
| nothing | `AGENTS.md` + `CLAUDE.md` containing one line `@AGENTS.md` | software-architector |

A `CLAUDE.md` that is exactly `@AGENTS.md` is a pointer: judge by `AGENTS.md`. Never create `AGENTS.md` next to the user's own `CLAUDE.md`.

## 3. Start the run
```
bv init --slug <latin-kebab-slug> --title "<short title>" --mode <full|semi|interview> --depth <strict|normal|deep> --memory-file <file> --memory-owner <software-architector|user>
```
`init` runs `git init` if needed, adds `.env`, `.env.*`, `!.env.example`, `node_modules/`, `__pycache__/`, `.venv/`, `.DS_Store` to `.gitignore`, creates `.software-architector/runs/<date>-<slug>--wip/`. Add the other build leftovers of the stack (`dist/`, `target/`, coverage folders) to `.gitignore` yourself: `bv commit` runs the check and then commits the whole zone.

Then `bv doctor`, as a call of its own after `bv init` (git, `bin/bv`, all 8 hooks registered and really firing, state schema). Any `✗` → tell the user in one line and stop: the guard would silently not work, and no gate that needs the journal could ever pass. Then `bv set resume=yes` if you have the SendMessage tool, else `bv set resume=no` — phase 5 reads it to pick the repair route.

If the memory file is ours and new, write the skeleton now, all between `<!-- software-architector:start -->` and `<!-- software-architector:end -->`: title line, "Commands" (only known commands), "How Software-Architector works here" with "Edit without Software-Architector" and "Git history" (see the bv-memory agent's description of those two subsections). "Edit without Software-Architector" starts with the line "While a run is open (`bv status`), edits go only through the orchestrator; edit directly after delivery".

## 4. Carried from the last run
If an earlier run in `.software-architector/runs/` was finished, read `.software-architector/PROGRESS.md` and the commits after the last landing:
`git log --oneline --no-merges <L>..HEAD -- . ":(exclude).software-architector"` where `<L>` is the last commit with subject `Software-Architector: build delivered` or `Software-Architector: build closed without acceptance`.
Use: "Agreed" → project rules in `interfaces.md` (phase 3); "Unfinished" / "Known bugs" → one question in the first briefing round (not taken in `full`); post-landing commits → the scout. The last run closed without acceptance → say so in the start block.

## 5. Start block to the user (3–4 lines)
What I understood the task to be (one line) · mode and depth in plain words · where to watch the run (panel on the right in VS Code, the web address in a terminal — see the skill) · "Starting".

Then `bv stage manifest start` and read `phases/1-manifest.md`.
