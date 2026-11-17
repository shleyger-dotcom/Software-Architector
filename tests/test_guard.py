import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import guard  # noqa: E402

# the tests write to "$TEMP/…": Windows sets TEMP, Linux and macOS do not, and an unset one resolves under the cwd
os.environ.setdefault("TEMP", tempfile.gettempdir())

GH = "ghp_" + "a" * 36  # built at runtime so this file itself holds no token


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".software-architector").mkdir()
        flying = {"id": "01", "status": "in-progress", "zone": ["src/", "game.js", "tests/"]}
        (self.root / ".software-architector" / "state.json").write_text(json.dumps(
            {"finishedAt": None, "tickets": [flying]}), "utf-8")
        self.st = guard.active_state(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, tool, agent=None, **ti):
        data = {"tool_name": tool, "tool_input": ti, "cwd": str(self.root)}
        if agent:
            data.update(agent_id="a1", agent_type=f"software-architector:{agent}")
        return guard.decide(data, self.root, self.st)

    def p(self, rel):
        return str(self.root / rel)


class GuardTest(GuardBase):
    def test_orchestrator_cannot_write_code(self):
        self.assertIn("does not write project code", self.decide("Write", file_path=self.p("src/app.py"), content="x"))
        self.assertIsNone(self.decide("Write", file_path=self.p(".software-architector/runs/x/spec.md"), content="x"))
        self.assertIsNone(self.decide("Edit", file_path=self.p("AGENTS.md"), new_string="x"))
        self.assertIsNone(self.decide("Write", file_path=str(Path(tempfile.gettempdir()) / "n.md"), content="x"))
        self.assertIsNone(self.decide("Write", "bv-executor", file_path=self.p("src/app.py"), content="x"))

    def test_secret_in_written_text(self):
        self.assertIn("secret", self.decide("Write", "bv-executor", file_path=self.p("src/c.py"), content=GH))
        self.assertNotIn("secret", self.decide("Write", "bv-executor", file_path=self.p(".env"), content=GH))  # zone, not secret
        self.assertIn("secret", self.decide("Write", "bv-executor", file_path=self.p(".env.example"), content=GH))
        self.assertIsNone(self.decide("Write", "bv-executor", file_path=self.p("src/docker.env.md"),
                                      content="postgres://app:app@localhost:5432/db"))
        self.assertIn("URL", self.decide("Write", "bv-executor", file_path=self.p("x.py"),
                                         content="postgres://app:S3cretPw@prod.example.com/db"))

    def test_dangerous_git_and_rm(self):
        for cmd in ("git push origin main", "git reset --hard HEAD", "git clean -fd", "git stash",
                    "git checkout -- src/a.py", "git restore src/a.py", "git rebase -i HEAD~2",
                    "git commit --amend -m x", "git commit --no-verify -m x", "git add -A",
                    "git add .", "rm -rf /", "rm -rf ~/x", "rm -r ../other", "cat .env", "type .env.local"):
            self.assertIsNotNone(self.decide("Bash", "bv-executor", command=cmd), cmd)
        for cmd in ("git add -A -- src/a/", "git restore --staged x", "rm -rf node_modules", "cat .env.example",
                    "git status && git log --oneline", "npm test", "git checkout -b feature"):
            self.assertIsNone(self.decide("Bash", "bv-executor", command=cmd), cmd)

    def test_reviewer_is_read_only(self):
        for cmd in ("echo x > src/a.py", "sed -i s/a/b/ f", "git commit -m x", "git apply p.diff", "rm f",
                    "Set-Content f x", "tee out.txt"):
            self.assertIn("only reads", self.decide("Bash", "bv-reviewer", command=cmd), cmd)
        for cmd in ("git show abc123", "git diff base..HEAD", "npm test 2>&1 | tail -30", "pytest > /dev/null",
                    "git log --oneline -5", "git branch --show-current"):
            self.assertIsNone(self.decide("Bash", "bv-reviewer", command=cmd), cmd)
        self.assertIsNone(self.decide("Bash", "bv-executor", command="echo x > src/a.py"))

    def test_a_write_command_is_a_token_not_a_part_of_a_path_or_an_option(self):
        for cmd in ("cat src/rm.js", "git diff base..HEAD -- src/patch/", "git show --patch abc123",
                    "git log --format=%cp -1", "npm run test:touch"):
            self.assertIsNone(self.decide("Bash", "bv-reviewer", command=cmd), cmd)
        for cmd in ("/bin/rm f", "ls | xargs rm", "find . -name x -exec rm {} +", "touch .gitkeep", "cp a b; ls"):
            self.assertIn("only reads", self.decide("Bash", "bv-reviewer", command=cmd), cmd)

    def test_a_subagent_cannot_commit(self):
        for cmd in ("git add src/a.py && git commit -m wip", 'bash -c "git commit -m x"'):
            self.assertIn("bv commit", self.decide("Bash", "bv-executor", command=cmd), cmd)
        for cmd in ('git log --grep "git commit"', "git status"):
            self.assertIsNone(self.decide("Bash", "bv-executor", command=cmd), cmd)

    def test_only_a_persons_message_is_held_back_for_a_secret(self):
        def prompt(**data):
            out = io.StringIO()
            payload = json.dumps({"cwd": str(self.root), "prompt": f"key {GH}", **data})
            with mock.patch("sys.stdin", io.StringIO(payload)), redirect_stdout(out), redirect_stderr(io.StringIO()):
                guard.run_hook("prompt")
            return out.getvalue()
        self.assertIn('"block"', prompt())
        self.assertIn('"block"', prompt(source="user"))
        self.assertEqual(prompt(source="system"), "")  # an agent's result: blocking it would stall the run

    def test_launcher_skips_python_only_when_the_project_has_no_run(self):
        """bin/bv leaves a hook before Python starts when the project has no run; it must never do so while one is open."""
        import bv
        argv = bv.shell_argv(self.root, '"$BV_BIN" hook pre-tool')
        if not argv:
            self.skipTest("no bash")
        launcher = (Path(__file__).resolve().parents[1] / "bin" / "bv").as_posix()

        def hook(project):
            payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(project)}
            env = {**os.environ, "BV_BIN": launcher, "CLAUDE_PROJECT_DIR": str(project)}
            return subprocess.run(argv, cwd=project, env=env, capture_output=True,
                                  input=json.dumps(payload).encode("utf-8")).returncode
        deep = self.root / "src" / "deep"
        deep.mkdir(parents=True)
        self.assertEqual(hook(self.root), 2)
        self.assertEqual(hook(deep), 2)  # the run is found above the folder
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(hook(Path(empty).resolve()), 0)

    def test_reviewer_may_run_checks(self):
        for cmd in ('grep -n -i "touch\\|pointer" game.js index.html',
                    "node -e \"const s=L.createGame(1); if (s.x>0 && s.y<24) console.log(1)\"",
                    "node --check game.js && node --test test/ 2>&1 | tail -15; cat logic.js"):
            self.assertIsNone(self.decide("Bash", "bv-reviewer", command=cmd), cmd)
        for cmd in ("node -e \"require('fs').writeFileSync('a.js','x')\"", "python -c \"open('a','w').write('x')\"",
                    "cat > \"$TEMP/dbg.js\" <<'E'\nx\nE", "echo \"a\" >> notes.txt"):
            self.assertIn("only reads", self.decide("Bash", "bv-reviewer", command=cmd), cmd)

    def test_orchestrator_cannot_write_through_shell(self):
        for cmd in ("git add game.js && git commit -q -m x", "sed -i s/a/b/ game.js", "echo x > game.js",
                    "python - <<'E'\ng=open('game.js','w')\nE", "tee src/a.py", "echo x >> test/a.test.js"):
            self.assertIn("does not write project code", self.decide("Bash", command=cmd), cmd)
        for cmd in ("git status --short", "git diff base..HEAD", "bv commit 01 --tests 11/0", "npm test 2>&1 | tail -30",
                    "echo x > .software-architector/runs/r/n.txt", "echo x > AGENTS.md", "echo x > \"$TEMP/dbg.js\"",
                    "cat a > /dev/null", "grep -n \"a>b\" game.js"):
            self.assertIsNone(self.decide("Bash", command=cmd), cmd)
        self.assertIsNone(self.decide("Bash", "bv-executor", command="echo x > game.js"))

    def test_data_in_bv_commands_is_not_a_write(self):
        contract = "STATUS: DONE\nTESTS: node --test → 3/0\nINTERFACES: writeFile(path) => ok; open(f, 'w')\n"
        for cmd in (f"bv contract 01 --from - <<'EOF'\n{contract}EOF",
                    "bv add concerns \"python writes through open(f, 'w')\"",
                    f"\"C:/plugin/bin/bv\" gate G2 <<'EOF'\n{contract}EOF",
                    f"@'\n{contract}'@ | bv contract 01 --from -"):
            self.assertIsNone(self.decide("Bash", command=cmd), cmd)

    def test_data_in_heredoc_is_not_a_command(self):
        page = "cat > src/notes.md <<'EOF'\ndo not run `git commit` or `git push` yourself, nor git add .\nEOF"
        self.assertIsNone(self.decide("Bash", "bv-executor", command=page))
        self.assertIsNotNone(self.decide("Bash", "bv-executor", command="python - <<'E'\nos.system('git push')\nE"))
        self.assertIsNotNone(self.decide("Bash", "bv-executor", command="bash <<'E'\ngit push\nE"))
        self.assertIsNotNone(self.decide("Bash", "bv-executor", command="cat > a <<'E'\nx\nE\ngit push"))

    def test_escaped_quotes_stay_inside_the_string(self):
        self.assertIsNone(self.decide("Bash", "bv-reviewer", command='node -e "console.log(\\"a>b\\")"'))
        self.assertIn("only reads", self.decide("Bash", "bv-reviewer", command='node -e "1" > out.txt'))

    def test_shell_write_message_has_the_way_out(self):
        self.assertIn("guard=off", self.decide("Bash", command="echo x > game.js"))

    def test_session_start_loads_the_orchestrator(self):
        (self.root / ".software-architector" / "state.json").write_text(json.dumps(
            {"schemaVersion": 3, "finishedAt": None, "title": "t", "dir": "runs/x--wip", "mode": "semi", "depth": "normal",
             "stages": [], "gates": {}, "tickets": []}), "utf-8")
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(self.root)}))), redirect_stdout(out):
            self.assertEqual(guard.run_hook("session-start"), 0)
        text = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        skill = Path(__file__).resolve().parents[1] / "skills" / "build" / "SKILL.md"
        self.assertIn(skill.as_posix(), text)
        self.assertTrue(skill.exists())

    def test_real_process_reads_stdin_as_utf8(self):
        # a real process: the pipe encoding of the console is what broke this, an in-process test cannot see it
        project = self.root / "Jeu-é"
        (project / ".software-architector").mkdir(parents=True)
        (project / ".software-architector" / "state.json").write_text(json.dumps(
            {"schemaVersion": 3, "finishedAt": None, "dir": "runs/x--wip"}), "utf-8")
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": str(project)}
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "CLAUDE_PROJECT_DIR")}
        script = Path(__file__).resolve().parents[1] / "scripts" / "bv.py"
        r = subprocess.run([sys.executable, str(script), "hook", "pre-tool"], cwd=project, env=env, capture_output=True,
                           input=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(r.returncode, 2, r.stderr.decode("utf-8", "replace"))

    def test_env_read(self):
        self.assertIn(".env", self.decide("Read", file_path=self.p(".env")))
        self.assertIsNone(self.decide("Read", file_path=self.p(".env.example")))

    def test_git_global_options_and_env_search(self):
        for cmd in ("git -C . push", "git -c user.name=x push origin main", "git --no-pager stash"):
            self.assertIsNotNone(self.decide("Bash", "bv-executor", command=cmd), cmd)
        self.assertIsNone(self.decide("Bash", "bv-executor", command="git -C sub status"))
        self.assertIsNotNone(self.decide("Grep", pattern="KEY", path=self.p(".env")))
        self.assertIsNotNone(self.decide("Grep", pattern="KEY", glob=".env.local"))
        self.assertIsNotNone(self.decide("Glob", pattern="**/.env"))
        self.assertIsNone(self.decide("Grep", pattern="KEY", path=self.p("src")))
        self.assertIsNone(self.decide("Grep", pattern="KEY", glob=".env.example"))

    def test_nested_shell_is_checked_for_writes(self):
        for cmd in ('bash -c "echo x > src/a.py"', "sh -c 'sed -i s/a/b/ f'", 'eval "rm f"', "bash <<'E'\necho x > f\nE",
                    'pwsh -NoProfile -Command "Set-Content f x"', "bash -lc \"bash -c 'echo x > f'\""):
            self.assertIn("only reads", self.decide("Bash", "bv-reviewer", command=cmd), cmd)
        for cmd in ('bash -c "npm test 2>&1 | tail -30"', "bash -lc 'git log --oneline -5'", "bash <<'E'\ngit diff\nE"):
            self.assertIsNone(self.decide("Bash", "bv-reviewer", command=cmd), cmd)
        for cmd in ('bash -c "echo x > game.js"', "bash <<'E'\necho x > game.js\nE"):
            self.assertIn("does not write project code", self.decide("Bash", command=cmd), cmd)
        for cmd in ('bash -c "git status --short"', "bv add concerns \"the reviewer tried bash -c 'echo x > f'\"",
                    'bash -c "echo x > .software-architector/runs/r/n.txt"'):
            self.assertIsNone(self.decide("Bash", command=cmd), cmd)

    def test_researcher_writes_only_research_md(self):
        run = ".software-architector/runs/r/"
        self.assertIsNone(self.decide("Write", "bv-research", file_path=self.p(run + "research.md"), content="x"))
        for tool, rel in (("Write", "src/app.py"), ("Edit", "AGENTS.md"), ("Write", run + "spec.md")):
            self.assertIn("research.md", self.decide(tool, "bv-research", file_path=self.p(rel), content="x", new_string="x"))
        self.assertIsNone(self.decide("Write", "bv-executor", file_path=self.p("src/app.py"), content="x"))

    def test_scout_writes_only_notes_md_and_its_shell_only_reads(self):
        """L8: the scout's doc promised one file, but only the researcher was held to it."""
        run = ".software-architector/runs/r/"
        self.assertIsNone(self.decide("Write", "bv-scout", file_path=self.p(run + "notes.md"), content="x"))
        for rel in ("src/app.py", run + "spec.md", "notes.txt"):
            self.assertIn("notes.md", self.decide("Write", "bv-scout", file_path=self.p(rel), content="x"), rel)
        for cmd in ("echo x > notes.md", "npm install", "git checkout -b x", "touch a"):
            self.assertIn("only reads", self.decide("Bash", "bv-scout", command=cmd), cmd)
        for cmd in ("git log --oneline -20", "ls -la src", "python --version", "npm test 2>&1 | tail -5"):
            self.assertIsNone(self.decide("Bash", "bv-scout", command=cmd), cmd)

    def test_secret_never_leaves_through_the_web(self):
        self.assertIn("secret", self.decide("WebSearch", "bv-research", query=f"why does {GH} fail"))
        self.assertIn("secret", self.decide("WebFetch", "bv-research", url=f"https://api.example.com/?token={GH}", prompt="x"))
        self.assertIn("secret", self.decide("WebFetch", url="https://docs.example.com", prompt=f"use {GH}"))
        self.assertIn("secret", self.decide("WebFetch", url="https://bob:S3cretPw@prod.example.com/x", prompt=""))
        self.assertIsNone(self.decide("WebSearch", "bv-research", query="python-telegram-bot latest version"))
        self.assertIsNone(self.decide("WebFetch", "bv-research", url="https://pypi.org/project/httpx/", prompt="version"))

    def test_ci_has_least_privilege_and_pinned_tools(self):
        """L11: CI read with default rights and installed whatever Claude Code version was newest."""
        ci = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "test.yml").read_text("utf-8")
        self.assertRegex(ci, r"(?m)^permissions:\n  contents: read$")
        self.assertRegex(ci, r'CLAUDE_CODE_VERSION: "\d+\.\d+\.\d+"')
        self.assertIn("@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}", ci)
        self.assertNotRegex(ci, r"claude-code(?!@)\s")
        self.assertIn("python -m compileall", ci)
        self.assertRegex(ci, r"uses: actions/[\w-]+@v\d+")

    def test_web_tools_reach_the_guard(self):
        hooks = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text("utf-8"))
        matcher = hooks["hooks"]["PreToolUse"][0]["matcher"].split("|")
        self.assertIn("WebSearch", matcher)
        self.assertIn("WebFetch", matcher)

    def test_isolated_checkers(self):
        brief = self.p(".software-architector/runs/r/brief-2026-10-28.md")
        self.assertIsNone(self.decide("Read", "bv-blind", file_path=brief))
        self.assertIsNotNone(self.decide("Read", "bv-blind", file_path=self.p(".software-architector/runs/r/spec.md")))
        self.assertIsNone(self.decide("Read", "bv-coverage", file_path=self.p(".software-architector/runs/r/spec.md")))
        self.assertIsNotNone(self.decide("Read", "bv-coverage", file_path=self.p(".software-architector/runs/r/manifest.md")))
        self.assertIsNotNone(self.decide("Bash", "bv-blind", command="ls .software-architector/runs"))
        self.assertIsNotNone(self.decide("Grep", "bv-blind", pattern="R01", path=self.p(".software-architector")))
        self.assertIn("clone", self.decide("Read", "bv-blind", file_path=self.p("src/app.py")))  # M6: the clone, not the folder
        self.assertIsNone(self.decide("Read", "bv-coverage", file_path=self.p("src/app.py")))

    def test_inactive_run_is_ignored(self):
        (self.root / ".software-architector" / "state.json").write_text(json.dumps({"finishedAt": "2026-10-28"}), "utf-8")
        self.assertIsNone(guard.active_state(self.root))
        (self.root / ".software-architector" / "state.json").write_text(json.dumps({"finishedAt": None, "guard": "off"}), "utf-8")
        self.assertIsNone(guard.active_state(self.root))

    def test_find_secret_patterns(self):
        self.assertEqual(guard.find_secret(GH), "GitHub token")
        self.assertEqual(guard.find_secret("key=" + "AKIA" + "ABCDEFGHIJKLMNOP"), "AWS key")
        self.assertIsNone(guard.find_secret("task-runner and sk-short"))

    def test_mask_secrets(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
        masked = guard.mask_secrets(f"t={GH} {pem} postgres://app:S3cretPw@prod.example.com/db "
                                    "postgres://app:app@localhost/db")
        self.assertIsNone(guard.find_secret(masked))
        self.assertNotIn("MIIEow", masked)
        self.assertIn("[GitHub token hidden]", masked)
        self.assertIn("app:[password hidden]@prod.example.com", masked)
        self.assertIn("app:app@localhost", masked)
        self.assertEqual(guard.mask_secrets("npm test"), "npm test")


class RunRecordsTest(GuardBase):
    """H1: state.json and the journal are proof; only bv and the hooks write them, never a tool call."""

    def test_no_agent_writes_the_state_or_the_journal_through_a_tool(self):
        for rel in (".software-architector/state.json", ".software-architector/state.tmp",
                    ".software-architector/live/r/agents/main.jsonl", ".Software-Architector/State.json"):
            for agent in (None, "bv-executor", "bv-memory"):
                self.assertIn("bv and the hooks write", self.decide("Write", agent, file_path=self.p(rel), content="{}"), (rel, agent))
                self.assertIn("bv and the hooks write", self.decide("Edit", agent, file_path=self.p(rel), new_string="x"), (rel, agent))

    def test_no_agent_writes_them_through_the_shell(self):
        for cmd in ("echo {} > .software-architector/state.json", "cp forged.json .software-architector/state.json",
                    "echo '{}' >> .software-architector/live/r/agents/a1.jsonl",
                    "python -c \"open('.software-architector/state.json','w').write('{}')\"",
                    "Set-Content .software-architector\\state.json x", 'bash -c "echo x > .software-architector/live/x"'):
            for agent in (None, "bv-executor"):
                self.assertIn("bv and the hooks write", self.decide("Bash", agent, command=cmd), (cmd, agent))

    def test_reading_them_and_writing_the_run_folder_stays_possible(self):
        self.assertIsNone(self.decide("Bash", "bv-executor", command="cat .software-architector/state.json"))
        self.assertIsNone(self.decide("Write", file_path=self.p(".software-architector/runs/r/spec.md"), content="x"))
        self.assertIsNone(self.decide("Bash", command="echo x > .software-architector/runs/r/n.txt"))
        self.assertIsNone(self.decide("Read", file_path=self.p(".software-architector/live/r/agents/main.jsonl")))


class BypassTest(GuardBase):
    """H3: every shell spelling found by the audit; each is a regression test."""

    def denied(self, agent, cmds, part=None):
        for cmd in cmds:
            why = self.decide("Bash", agent, command=cmd)
            self.assertIsNotNone(why, cmd)
            if part:
                self.assertIn(part, why, cmd)

    def allowed(self, agent, cmds):
        for cmd in cmds:
            self.assertIsNone(self.decide("Bash", agent, command=cmd), cmd)

    def test_orchestrator_file_writes_in_any_shell_form(self):
        self.denied(None, ("cp a.js game.js", "mv a.js game.js", "rm game.js", "New-Item game.js", "Copy-Item a.js game.js",
                           'cmd /c "echo x > game.js"', "cmd.exe /C \"echo x > game.js\"",
                           "[IO.File]::WriteAllText('game.js', 'x')", "[System.IO.File]::Delete('game.js')",
                           '"cp" a.js game.js', "CP a.js game.js", "& 'Copy-Item' a.js game.js", "touch game.js"),
                    "does not write project code")
        self.allowed(None, ("cat README.md", "git status --short", "ls src", "bv status"))

    def test_orchestrator_cannot_hand_bv_a_writing_check(self):
        self.denied(None, ('bv set checkCmd="echo x > game.js"', "bv set 'e2eCmd=sed -i s/a/b/ game.js'",
                           'bv set "setupCmd=cp a.js game.js"'), "bv set")
        self.denied(None, ('bv set checkCmd="git push"', 'bv set checkCmd="cat .env"'))
        self.allowed(None, ('bv set checkCmd="npm test"', "bv set checkCmd='python -m pytest -q' e2eCmd='node e2e.js'",
                            "bv set title=x"))

    def test_publication_in_any_spelling(self):
        self.denied("bv-executor", ('"git" push', "g'i't push origin", "git.exe push", "GIT PUSH",
                                    '"C:/Program Files/Git/cmd/git.exe" push', "git -c alias.p=push p",
                                    "git config alias.p push", "git -C . --no-pager push", "git --git-dir=.git push",
                                    "npm publish", "pnpm publish --access public", "gh repo create x --source . --push",
                                    "git send-pack origin main"))
        self.allowed("bv-executor", ("npm run publish-docs", "git log --oneline -3", "gh --version"))

    def test_history_rewrite_and_file_reverts(self):
        self.denied("bv-executor", ("git reset --soft HEAD~1", "git reset --hard", "git reset HEAD~1", "git reset abc1234",
                                    "git checkout HEAD~1 -- src/a.py", "git checkout HEAD~1 src/a.py", "git checkout src/a.py",
                                    "git checkout .", "git checkout -f main", "git restore --staged --worktree x",
                                    "git update-ref refs/heads/main HEAD~1"))
        self.allowed("bv-executor", ("git reset -q -- src/a.py", "git reset HEAD src/a.py", "git checkout main",
                                     "git checkout -b feature/x", "git restore --staged src/a.py"))

    def test_env_is_not_read_by_any_tool(self):
        self.denied("bv-executor", ("grep KEY .env", "python -c \"print(open('.env').read())\"", "cat ./.env",
                                    "sed -n 1p .env.local", "Select-String KEY .env", "cp .env /tmp/x", "source .env",
                                    "awk 1 < .env", 'cat ".env"'), ".env")
        self.allowed("bv-executor", ("cat .env.example", "grep -n DATABASE_URL .env.example", "echo $NODE_ENV"))

    def test_reviewer_writes_through_interpreters_and_installs(self):
        self.denied("bv-reviewer", ("python -c \"open('a.txt', mode='w').write('x')\"", "npm install", "npm i left-pad",
                                    "pip install requests", "node -e \"require('fs').unlinkSync('a')\"",
                                    "python -c \"import pathlib; pathlib.Path('a').open('w')\"",
                                    "python -c \"import os; os.remove('a')\""), "only reads")
        self.allowed("bv-reviewer", ("cat README.md", "npm test", "python -c \"print(open('a').read())\""))

    def test_the_run_folder_cannot_be_removed(self):
        for agent in (None, "bv-executor"):
            self.denied(agent, ("rm -rf .software-architector", "rm -rf ./.software-architector/",
                                "Remove-Item -Recurse -Force .software-architector", "mv .software-architector old"))

    def test_executor_writes_only_its_zone(self):
        for rel in ("other/x.py", "AGENTS.md", ".software-architector/runs/r/spec.md"):
            self.assertIsNotNone(self.decide("Write", "bv-executor", file_path=self.p(rel), content="x"), rel)
        self.denied("bv-executor", ("echo x > other/a.py", "cp src/a.py lib/a.py", "touch README.md",
                                    "cd lib && echo x > a.py", "mkdir .software-architector/x",
                                    "python -c \"open('src/a.py','w')\""))
        self.allowed("bv-executor", ("echo x > src/a.py", "cp a.txt src/b.txt", "touch tests/t.py", "rm -rf node_modules",
                                     "cd src && echo x > a.py", "npm install left-pad"))
        for rel in ("package-lock.json", "src/x/.env.example", ".env.example"):
            self.assertIsNone(self.decide("Write", "bv-executor", file_path=self.p(rel), content="x"), rel)
        self.assertIsNone(self.decide("Write", "bv-executor", file_path=str(Path(tempfile.gettempdir()) / "n.md"), content="x"))

    def test_executor_zone_is_its_own_ticket_when_the_journal_names_it(self):
        st = {"finishedAt": None, "dir": "runs/r--wip", "tickets": [
            {"id": "01", "status": "in-progress", "zone": ["src/"]}, {"id": "02", "status": "in-progress", "zone": ["lib/"]}]}
        agents = self.root / ".software-architector" / "live" / "r" / "agents"
        agents.mkdir(parents=True)
        (agents / "main.jsonl").write_text(json.dumps(
            {"k": "tool", "tool": "Agent", "s": "software-architector:bv-executor: Ticket 02: library", "spawned": "a1"}) + "\n",
            "utf-8")
        data = {"tool_name": "Write", "tool_input": {"file_path": self.p("src/a.py"), "content": "x"}, "agent_id": "a1",
                "agent_type": "software-architector:bv-executor"}
        self.assertIn("lib/", guard.decide(data, self.root, st))
        data["tool_input"]["file_path"] = self.p("lib/a.py")
        self.assertIsNone(guard.decide(data, self.root, st))

    def test_git_add_of_the_whole_tree_in_any_form(self):
        self.denied("bv-executor", ("git add -A -- .", "git add :/", "git add -u", "git add ':(top)'", "git add --all",
                                    "GIT ADD ."), "git add")
        self.allowed("bv-executor", ("git add -A -- src/a/", "git add src/a.py"))


class OtherToolsTest(GuardBase):
    """L10: MCP write tools, agents from outside the run, and a guard that crashes or hangs."""

    def call(self, tool, ti, **extra):
        return guard.decide({"tool_name": tool, "tool_input": ti, "cwd": str(self.root), **extra}, self.root, self.st)

    def test_the_hook_sees_agents_and_mcp_tools(self):
        hooks = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text("utf-8"))
        matcher = re.compile(hooks["hooks"]["PreToolUse"][0]["matcher"])
        for tool in ("Agent", "Task", "mcp__fs__write_file", "mcp__claude_ai_Notion__notion-create-pages", "Write"):
            self.assertTrue(matcher.fullmatch(tool), tool)

    def test_mcp_tools_that_change_things_are_refused(self):
        for tool in ("mcp__fs__write_file", "mcp__claude_ai_Notion__notion-create-pages", "mcp__gh__createPullRequest",
                     "mcp__db__execute_sql", "mcp__ide__executeCode"):
            self.assertIn("the user decides", self.call(tool, {"x": 1}), tool)
            self.assertIsNotNone(self.call(tool, {"x": 1}, agent_id="a1", agent_type="software-architector:bv-executor"))
        for tool in ("mcp__fs__read_file", "mcp__claude_ai_Notion__notion-search", "mcp__x__get_settings",
                     "mcp__gh__listIssues", "mcp__docs__query"):
            self.assertIsNone(self.call(tool, {"q": "python"}), tool)
        self.assertIn("secret", self.call("mcp__docs__query", {"q": GH}))

    def test_the_orchestrator_launches_only_the_runs_agents(self):
        for kind in ("general-purpose", "Explore", "claude", ""):
            self.assertIn("software-architector:bv-", self.call("Agent", {"subagent_type": kind, "prompt": "x"}), kind)
        self.assertIsNone(self.call("Agent", {"subagent_type": "software-architector:bv-executor", "prompt": "x"}))
        self.assertIsNone(self.call("Task", {"subagent_type": "software-architector:bv-reviewer", "prompt": "x"}))

    def test_an_agent_from_outside_the_run_only_reads(self):
        outsider = {"agent_id": "g1", "agent_type": "general-purpose"}
        self.assertIn("not from the run", self.call("Write", {"file_path": self.p("src/a.py"), "content": "x"}, **outsider))
        self.assertIn("not from the run", self.call("Bash", {"command": "echo x > src/a.py"}, **outsider))
        self.assertIsNone(self.call("Bash", {"command": "git log --oneline -3"}, **outsider))
        self.assertIsNone(self.call("Read", {"file_path": self.p("src/a.py")}, **outsider))

    def test_a_crashing_or_hanging_guard_fails_closed_for_writes(self):
        def hook(tool, ti):
            payload = json.dumps({"tool_name": tool, "tool_input": ti, "cwd": str(self.root)})
            with mock.patch("sys.stdin", io.StringIO(payload)), redirect_stderr(io.StringIO()) as err:
                return guard.run_hook("pre-tool"), err.getvalue()
        with mock.patch.object(guard, "decide", side_effect=RuntimeError("bug")):
            code, err = hook("Write", {"file_path": self.p("x.md"), "content": "x"})
            self.assertEqual(code, 2)
            self.assertIn("crashed", err)
            self.assertEqual(hook("Read", {"file_path": self.p("x.md")})[0], 0)
        with mock.patch.object(guard, "HOOK_BUDGET", 0.2), \
                mock.patch.object(guard, "decide", side_effect=lambda *a: time.sleep(1)):
            code, err = hook("Bash", {"command": "ls"})
            self.assertEqual(code, 2)
            self.assertIn("timed out", err)
            self.assertEqual(hook("Grep", {"pattern": "x"})[0], 0)


class GenericSecretTest(GuardBase):
    """M8: a key of any provider: a long hex/base64 value next to key/token/secret/password. Values are fake, built here."""

    def test_a_long_value_next_to_a_secret_name_is_found_and_masked(self):
        hexval, b64 = "f" * 40, "Ab9+" * 9
        for text in (f"API_KEY = '{hexval}'", f"password: {b64}", f'"client_secret": "{hexval}"',
                     f"Authorization: Bearer {b64}", f"export SERVICE_TOKEN={hexval}"):
            self.assertIsNotNone(guard.find_secret(text), text.split()[0])
            masked = guard.mask_secrets(text)
            self.assertNotIn(hexval if hexval in text else b64, masked)
            self.assertIsNone(guard.find_secret(masked))
        self.assertTrue(guard.mask_secrets(f"API_KEY={hexval}").startswith("API_KEY=["))

    def test_ordinary_code_and_hashes_are_not_secrets(self):
        for text in ("token = request.args['token']", '"js-tokens": "^4.0.0"', "integrity sha512-" + "a" * 80,
                     "sha256=" + "e" * 64, "API_KEY=", "password = os.environ['DB_PASSWORD']", "secret_len = 32"):
            self.assertIsNone(guard.find_secret(text), text[:30])

    def test_an_unmasked_journal_file_is_not_read(self):
        live = self.root / ".software-architector" / "live" / "r" / "checks"
        live.mkdir(parents=True)
        raw, masked = live / "1-check.txt", live / "1-check.masked.txt"
        raw.write_text("deploy ok\nAPI_KEY=" + "f" * 40 + "\n", "utf-8")
        masked.write_text(guard.mask_secrets(raw.read_text("utf-8")), "utf-8")
        self.assertIn("masked", self.decide("Read", file_path=str(raw)))
        self.assertIsNone(self.decide("Read", file_path=str(masked)))
        self.assertIsNone(self.decide("Read", file_path=str(live / "no-file.txt")))


class BlindIsolationTest(GuardBase):
    """M6: the blind check sees the brief and its history-free clone, nothing of the run and nothing of the folder."""

    def setUp(self):
        super().setUp()
        self.clone_tmp = tempfile.TemporaryDirectory(prefix="sa-blind-")
        self.clone = Path(self.clone_tmp.name).resolve() / "project"
        self.clone.mkdir()
        self.st = {**self.st, "blindClone": str(self.clone)}

    def tearDown(self):
        self.clone_tmp.cleanup()
        super().tearDown()

    def test_records_stay_shut_in_any_case_and_from_any_search_root(self):
        for tool, ti in (("Read", {"file_path": self.p(".SOFTWARE-ARCHITECTOR/runs/r/manifest.md")}),
                         ("Glob", {"pattern": "**/*.md"}), ("Grep", {"pattern": "R01", "path": str(self.root)}),
                         ("Glob", {"pattern": "**/spec.md", "path": str(self.root.parent)})):
            for agent in ("bv-blind", "bv-coverage"):
                self.assertIsNotNone(self.decide(tool, agent, **ti), (tool, ti, agent))
        self.assertIsNone(self.decide("Read", "bv-blind", file_path=self.p(".software-architector/runs/r/brief-1.md")))
        self.assertIsNone(self.decide("Grep", "bv-coverage", pattern="x", path=self.p("src")))

    def test_blind_works_in_its_clone_and_writes_only_there(self):
        c = self.clone.as_posix()
        for cmd in (f"cd {c} && npm install && npm test", f"echo x > {c}/out.txt", f"node {c}/app.js > {c}/run.log 2>&1"):
            self.assertIsNone(self.decide("Bash", "bv-blind", command=cmd), cmd)
        self.assertIsNone(self.decide("Grep", "bv-blind", pattern="def", path=c))
        for cmd in (f"cat {self.root.as_posix()}/src/a.py", f"cd {self.root} && npm test", "echo x > /tmp/elsewhere.txt",
                    f"cd {c} && python -c \"open('x','w')\"", f"echo x > {self.root.as_posix()}/src/a.py"):
            self.assertIsNotNone(self.decide("Bash", "bv-blind", command=cmd), cmd)
        self.st = {**self.st, "blindClone": None}
        self.assertIn("bv blind-clone", self.decide("Bash", "bv-blind", command=f"echo x > {c}/out.txt"))


class OutsideTheProjectTest(GuardBase):
    """H4: a path outside the project is not a free pass: the plugin and Claude Code's own files stay shut."""

    def test_plugin_and_claude_settings_are_never_written(self):
        plugin = Path(guard.__file__).resolve().parents[1]
        home = Path.home() / ".claude"
        for path in (plugin / "scripts" / "guard.py", plugin / "hooks" / "hooks.json", home / "settings.json",
                     home / "settings.local.json", home / "CLAUDE.md", self.root / ".claude" / "settings.local.json"):
            for agent in (None, "bv-executor", "bv-memory"):
                self.assertIn("plugin", self.decide("Write", agent, file_path=str(path), content="x"), (path, agent))
        with tempfile.TemporaryDirectory() as other, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": other}):
            self.assertIn("plugin", self.decide("Edit", "bv-executor", file_path=str(Path(other) / "x.py"), new_string="x"))

    def test_the_shell_cannot_reach_them_either(self):
        for cmd in ("echo x > ~/.claude/settings.json", 'cp a.json "$HOME/.claude/settings.local.json"',
                    "tee ~/.claude/CLAUDE.md", "echo x > $NOPE_UNSET_VAR/.claude/settings.json"):
            for agent in (None, "bv-executor"):
                self.assertIn("plugin", self.decide("Bash", agent, command=cmd), (cmd, agent))

    def test_an_unresolvable_path_fails_closed(self):
        self.assertIsNotNone(self.decide("Write", "bv-executor", file_path="src/\0bad.py", content="x"))
        self.assertIsNotNone(self.decide("Write", "bv-executor", content="x"))

    def test_temp_files_and_memory_notes_outside_the_project_stay_writable(self):
        tmp = str(Path(tempfile.gettempdir()) / "n.md")
        self.assertIsNone(self.decide("Write", file_path=tmp, content="x"))
        self.assertIsNone(self.decide("Write", "bv-executor", file_path=tmp, content="x"))
        self.assertIsNone(self.decide("Write", file_path=str(Path.home() / ".claude" / "projects" / "p" / "memory" / "m.md"),
                                      content="x"))
        self.assertIsNone(self.decide("Bash", "bv-executor", command='echo x > "$TEMP/dbg.txt"'))


if __name__ == "__main__":
    unittest.main()
