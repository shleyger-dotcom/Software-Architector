"""bv audit: junk code and security holes found mechanically, and a delivery that cannot step over them."""
import json
import os
import subprocess
import unittest
from unittest import mock

from test_bv import Base, bv
from test_delivery import APP, BLIND_OK, Delivery

audit = bv.audit
TOKEN = "ghp_" + "a" * 36  # built at run time: a literal token in this file would be a secret in the plugin itself


class ScanBase(Base):
    def scan(self, files, run=None, commands=()):
        """Write `files` ({path: text or bytes}) and scan them; `run` = the files the run changed (default: all)."""
        for rel, text in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                p.write_bytes(text)
            else:
                p.write_text(text, "utf-8")
        return audit.scan(self.root, list(files), list(files) if run is None else run, commands)

    def of(self, findings, rule):
        return [f for f in findings if f["rule"] == rule]

    def security(self, findings):
        return [f for f in findings if f["kind"] == "security"]


class SecretTest(ScanBase):
    def test_a_secret_anywhere_in_the_tree_is_a_blocker_and_its_value_is_masked(self):
        fs = self.scan({"old/settings.py": f"TOKEN = '{TOKEN}'\n", "src/app.py": "x = 1\n"}, run=["src/app.py"])
        hit = self.of(fs, "secret")
        self.assertEqual(len(hit), 1)
        f = hit[0]
        self.assertEqual((f["sev"], f["kind"], f["file"], f["line"]), ("blocker", "security", "old/settings.py", 1))
        self.assertIn("ghp_***", f["msg"])
        self.assertNotIn("aaaa", f["msg"])
        self.assertRegex(f["id"], r"^s-[0-9a-f]{8}$")

    def test_a_variable_name_or_a_local_url_is_not_a_secret(self):
        fs = self.scan({"src/app.py": "TOKEN = os.environ['TOKEN']\nURL = 'postgres://u:pw@localhost/db'\n"})
        self.assertEqual(self.of(fs, "secret"), [])

    def test_tracked_keys_and_env_files_are_blockers(self):
        keys = [".env", ".env.production", "deploy/server.pem", "certs/tls.key", "id_rsa", "store.p12", "app.keystore"]
        fs = self.scan({n: "x\n" for n in keys + [".env.example", "id_rsa.pub", "keys.py"]}, run=[])
        hit = self.of(fs, "key-file")
        self.assertEqual(sorted(f["file"] for f in hit), sorted(keys))
        self.assertEqual({f["sev"] for f in hit}, {"blocker"})


class SastTest(ScanBase):
    PY_HITS = [("eval(user_input)", "eval", "high"), ("exec(code)", "exec", "high"),
               ("os.system('ls ' + d)", "os-system", "high"), ("subprocess.run(cmd, shell=True)", "shell-true", "high"),
               ("data = pickle.loads(blob)", "pickle", "high"), ("cfg = yaml.load(f)", "yaml-load", "high"),
               ("requests.get(url, verify=False)", "tls-off", "high"), ("h = hashlib.md5(data)", "weak-hash", "medium"),
               ("app.run(debug=True)", "debug-on", "medium"),
               ('cur.execute(f"SELECT * FROM t WHERE id = {uid}")', "sql-concat", "high"),
               ('cur.execute("SELECT * FROM t WHERE id = " + uid)', "sql-concat", "high"),
               ('cur.execute("SELECT * FROM t WHERE id = %s" % uid)', "sql-concat", "high"),
               ("os.chmod(path, 0o777)", "chmod-777", "medium"), ("app.run(host='0.0.0.0')", "bind-all", "medium"),
               ("CORS_ORIGINS = ['*']", "cors-any", "medium")]
    PY_CLEAN = ["ast.literal_eval(text)", "model.eval()", "cfg = yaml.load(f, Loader=yaml.SafeLoader)",
                "cfg = yaml.safe_load(f)", "subprocess.run(['ls', d])", "cur.execute('SELECT * FROM t WHERE id = ?', (uid,))",
                "hashlib.md5(data, usedforsecurity=False)", "requests.get(url, verify=True)", "# never call eval(x) here",
                "app.run(debug=False)", "host = '127.0.0.1'", "LOCAL_HOSTS = ('localhost', '0.0.0.0')"]
    JS_HITS = [("eval(input)", "eval"), ("const f = new Function('a', body)", "new-function"),
               ("el.innerHTML = html", "inner-html"), ("<div dangerouslySetInnerHTML={{__html: x}} />", "dangerous-html"),
               ("document.write(x)", "document-write"), ("exec(`ls ${dir}`)", "exec-template"),
               ("https.request({rejectUnauthorized: false})", "tls-off"),
               ("process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0'", "tls-off"),
               ("res.setHeader('Access-Control-Allow-Origin', '*')", "cors-any"), ("app.use(cors({origin: '*'}))", "cors-any"),
               ("const token = Math.random().toString(36)", "weak-random"),
               ("db.query(`SELECT * FROM t WHERE id = ${id}`)", "sql-template"),
               ("server.listen(3000, '0.0.0.0')", "bind-all")]
    JS_CLEAN = ["el.textContent = html", "const n = Math.random() * 6", "db.query('SELECT * FROM t WHERE id = $1', [id])",
                "execFile('ls', [dir])", "// do not use eval(x)", "https.request({rejectUnauthorized: true})",
                "const m = /a/.exec(s)", "app.use(cors({origin: 'https://example.com'}))"]

    def test_dangerous_python_calls_are_found(self):
        for line, rule, sev in self.PY_HITS:
            with self.subTest(line):
                hit = self.of(self.scan({"src/m.py": line + "\n"}), rule)
                self.assertEqual([(f["sev"], f["kind"], f["line"]) for f in hit], [(sev, "security", 1)])

    def test_safe_python_forms_are_not_findings(self):
        for line in self.PY_CLEAN:
            with self.subTest(line):
                self.assertEqual(self.security(self.scan({"src/m.py": line + "\n"})), [])

    def test_dangerous_javascript_calls_are_found(self):
        for line, rule in self.JS_HITS:
            with self.subTest(line):
                self.assertEqual(len(self.of(self.scan({"src/m.ts": line + "\n"}), rule)), 1)

    def test_safe_javascript_forms_are_not_findings(self):
        for line in self.JS_CLEAN:
            with self.subTest(line):
                self.assertEqual(self.security(self.scan({"src/m.js": line + "\n"})), [])

    def test_shell_scripts_are_read_too(self):
        self.assertEqual(len(self.of(self.scan({"deploy.sh": "chmod 777 /srv/app\n"}), "chmod-777")), 1)

    def test_code_rules_see_only_the_runs_files_secrets_see_the_whole_tree(self):
        fs = self.scan({"old.py": f"eval(x)\nKEY = '{TOKEN}'\n", "src/new.py": "x = 1\n"}, run=["src/new.py"])
        self.assertEqual(self.of(fs, "eval"), [])
        self.assertEqual(len(self.of(fs, "secret")), 1)

    def test_binary_and_big_files_are_skipped(self):
        fs = self.scan({"src/blob.py": f"eval(x)\nKEY = '{TOKEN}'\n\x00\x01".encode(),
                        "src/big.py": "eval(x)\n" + "#" * (audit.MAX_BYTES + 1)})
        self.assertEqual(self.security(fs), [])

    def test_a_secret_on_a_flagged_line_is_masked_in_the_message(self):
        fs = self.scan({"src/m.py": f"eval('{TOKEN}')\n"})
        self.assertTrue(fs)
        self.assertFalse([f for f in fs if TOKEN in f["msg"]])


class DeadCodeTest(ScanBase):
    def test_debug_leftovers(self):
        fs = self.scan({"src/a.py": "breakpoint()\nimport pdb; pdb.set_trace()\nlogger.debug(x)\n",
                        "src/b.js": "console.log(x)\ndebugger;\nconsole.debug(y)\nconsole.error(e)\n"})
        hit = self.of(fs, "debug-leftover")
        self.assertEqual(sorted((f["file"], f["line"]) for f in hit),
                         [("src/a.py", 1), ("src/a.py", 2), ("src/b.js", 1), ("src/b.js", 2), ("src/b.js", 3)])
        self.assertTrue(all(f["kind"] == "dead" and f["id"].startswith("d-") for f in hit))

    def test_commented_out_code_needs_three_lines_in_a_row(self):
        fs = self.scan({"src/a.py": "x = 1\n# y = compute()\n# if y:\n#     send(y)\n",
                        "src/b.py": "# y = compute()\n# send(y)\nz = 2\n",
                        "src/c.py": "# This module reads the config.\n# It is called once at start.\n# Nothing else here.\n",
                        "src/d.js": "// const a = 1;\n// foo(a);\n// return a;\n"})
        self.assertEqual(sorted((f["file"], f["line"]) for f in self.of(fs, "commented-code")), [("src/a.py", 2), ("src/d.js", 1)])

    def test_stubs(self):
        fs = self.scan({"src/a.py": "# TODO: handle errors\ndef f():\n    raise NotImplementedError\ntodo_list = []\n",
                        "src/b.js": "const x = 1; // FIXME later\n"})
        self.assertEqual(sorted((f["file"], f["line"]) for f in self.of(fs, "stub")),
                         [("src/a.py", 1), ("src/a.py", 3), ("src/b.js", 1)])

    def test_orphan_files_and_what_is_never_one(self):
        files = {"src/lonely.py": "VALUE = 1\n", "src/used.py": "VALUE = 1\n", "src/main.py": "from src.used import VALUE\n",
                 "src/__init__.py": "", "tests/test_used.py": "import src\n",
                 "tools/script.py": "if __name__ == '__main__':\n    pass\n", "bin/tool-runner.js": "x\n",
                 "lib/boot.js": "y\n", "e2e.py": "z = 1\n", "vite.config.js": "export default {}\n",
                 "package.json": json.dumps({"bin": {"tool": "bin/tool-runner.js"}, "scripts": {"go": "node lib/boot.js"}})}
        fs = self.scan(files, commands=["python e2e.py"])
        orphans = self.of(fs, "orphan-file")
        self.assertEqual([f["file"] for f in orphans], ["src/lonely.py"])
        self.assertEqual(orphans[0]["sev"], "low")

    def test_unused_public_python_names_and_the_exemptions(self):
        lib = ("import functools\n\n\ndef unused_helper():\n    return 1\n\n\ndef used_elsewhere():\n    return 2\n\n\n"
               "@functools.cache\ndef cached():\n    return 3\n\n\ndef main():\n    return 4\n\n\ndef test_like():\n    return 5\n\n\n"
               "def _private():\n    return 6\n\n\nclass Exported:\n    pass\n\n\ndef called_here():\n    return 7\n\n\n"
               "VALUE = called_here()\n__all__ = ['Exported']\n")
        fs = self.scan({"src/lib.py": lib, "src/other.py": "from src.lib import used_elsewhere\n"})
        self.assertEqual([(f["file"], f["line"]) for f in self.of(fs, "unused-export")], [("src/lib.py", 4)])

    def test_unused_javascript_exports(self):
        util = ("export function usedThing() {}\nexport function unusedThing() {}\nexport const ALSO = 1;\n"
                "export default function () {}\nconst a = 1, b = 2;\nexport { a, b as bee };\n")
        fs = self.scan({"src/util.js": util, "src/index.js": "import { usedThing, bee } from './util';\n"})
        self.assertEqual(sorted(f["line"] for f in self.of(fs, "unused-export")), [2, 3, 6])

    def test_unused_dependencies_only_when_the_run_changed_the_manifest(self):
        files = {"package.json": json.dumps({"dependencies": {"express": "^4", "left-pad": "^1"},
                                             "devDependencies": {"jest": "^29"}}, indent=2),
                 "src/server.js": "const express = require('express');\n",
                 "requirements.txt": "pyyaml==6.0\nrequests>=2  # http\n-r base.txt\n", "src/load.py": "import yaml\n"}
        hit = self.of(self.scan(files), "unused-dependency")
        self.assertEqual(sorted((f["file"], f["msg"].split(":")[0]) for f in hit),
                         [("package.json", "left-pad"), ("requirements.txt", "requests")])
        self.assertEqual({f["sev"] for f in hit}, {"medium"})
        self.assertEqual(self.of(self.scan(files, run=["src/server.js", "src/load.py"]), "unused-dependency"), [])


class IdTest(ScanBase):
    def test_ids_survive_line_shifts(self):
        first = self.of(self.scan({"src/m.py": "x = 1\neval(a)\n"}), "eval")
        moved = self.of(self.scan({"src/m.py": "\n\n\nx = 1\n    eval(a)\n"}), "eval")
        self.assertEqual([f["id"] for f in first], [f["id"] for f in moved])
        self.assertEqual((first[0]["line"], moved[0]["line"]), (2, 5))

    def test_identical_lines_get_distinct_ids_and_other_files_other_ids(self):
        fs = self.of(self.scan({"src/m.py": "eval(a)\neval(a)\n", "src/n.py": "eval(a)\n"}), "eval")
        self.assertEqual(len({f["id"] for f in fs}), 3)


class FakeRunner:
    def __init__(self, results):
        self.results, self.calls = results, []

    def __call__(self, argv, cwd, env):
        self.calls.append((argv, cwd, env))
        r = self.results[argv[0]]
        if isinstance(r, BaseException):
            raise r
        return r


NPM = {"vulnerabilities": {
    "lodash": {"severity": "critical", "via": [{"title": "Prototype pollution", "source": 1}]},
    "minimist": {"severity": "high", "via": ["lodash"]},
    "debug": {"severity": "low", "via": [{"title": "ReDoS", "source": 2}]}}}
PIP = {"dependencies": [{"name": "jinja2", "version": "2.0", "vulns": [{"id": "PYSEC-1", "fix_versions": ["3.0"]}]},
                        {"name": "ok", "version": "1", "vulns": []}]}
CARGO = {"vulnerabilities": {"found": True, "list": [
    {"advisory": {"id": "RUSTSEC-1", "package": "smallvec", "title": "overflow"}, "package": {"name": "smallvec"}}]}}


class ToolTest(Base):
    def test_npm_audit_maps_critical_to_blocker_and_high_to_high(self):
        run = FakeRunner({"npm": (1, json.dumps(NPM))})
        tools, fs = audit.run_tools(self.root, ["package.json", "package-lock.json"], {"PATH": "p"}, runner=run)
        self.assertEqual(tools, {"npm": "ran"})
        self.assertEqual(run.calls, [(["npm", "audit", "--json"], self.root, {"PATH": "p"})])
        sev = {f["msg"].split(":")[0]: f["sev"] for f in fs}
        self.assertEqual(sev, {"lodash": "blocker", "minimist": "high", "debug": "low"})
        self.assertEqual({(f["rule"], f["kind"], f["file"]) for f in fs}, {("cve", "security", "package-lock.json")})

    def test_pip_audit_and_cargo_audit_without_a_severity_count_as_high(self):
        run = FakeRunner({"pip-audit": (1, json.dumps(PIP)), "cargo": (1, json.dumps(CARGO))})
        tools, fs = audit.run_tools(self.root, ["requirements.txt", "Cargo.lock", "src/a.py"], {}, runner=run)
        self.assertEqual(tools, {"pip-audit": "ran", "cargo-audit": "ran"})
        self.assertIn(["pip-audit", "-f", "json", "-r", "requirements.txt"], [c[0] for c in run.calls])
        self.assertEqual(sorted((f["file"], f["sev"]) for f in fs), [("Cargo.lock", "high"), ("requirements.txt", "high")])
        self.assertTrue(any("PYSEC-1" in f["msg"] for f in fs))

    def test_missing_broken_and_hung_tools_are_not_checked_never_clean(self):
        run = FakeRunner({"npm": FileNotFoundError("npm"), "pip-audit": (0, "not json"),
                          "cargo": subprocess.TimeoutExpired("cargo", 120)})
        tools, fs = audit.run_tools(self.root, ["package-lock.json", "pyproject.toml", "Cargo.lock"], {}, runner=run)
        self.assertEqual(tools, {"npm": "missing", "pip-audit": "error", "cargo-audit": "error"})
        self.assertEqual(fs, [])
        run = FakeRunner({"npm": (1, json.dumps({"error": {"code": "ENOTFOUND"}}))})
        self.assertEqual(audit.run_tools(self.root, ["package-lock.json"], {}, runner=run)[0], {"npm": "error"})

    def test_no_dependency_manifest_runs_no_tool(self):
        run = FakeRunner({})
        self.assertEqual(audit.run_tools(self.root, ["src/a.py", "sub/package-lock.json"], {}, runner=run), ({}, []))
        self.assertEqual(run.calls, [])

    def test_the_real_runner_uses_an_argument_list_no_shell_and_a_timeout(self):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps({"vulnerabilities": {}}), stderr="")
        with mock.patch.object(audit.shutil, "which", return_value="/usr/bin/npm"), \
                mock.patch.object(audit.subprocess, "run", return_value=done) as run:
            self.assertEqual(audit.run_tools(self.root, ["package-lock.json"], {"PATH": "p"}), ({"npm": "ran"}, []))
        args, kw = run.call_args
        self.assertEqual(args[0], ["/usr/bin/npm", "audit", "--json"])
        self.assertFalse(kw.get("shell"))
        self.assertEqual((kw["timeout"], kw["env"], kw["cwd"]), (120, {"PATH": "p"}, self.root))
        with mock.patch.object(audit.shutil, "which", return_value=None), mock.patch.object(audit.subprocess, "run") as run:
            self.assertEqual(audit.run_tools(self.root, ["package-lock.json"], {}), ({"npm": "missing"}, []))
        run.assert_not_called()


class AuditCommandTest(Delivery):
    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True, text=True).stdout.strip()

    def to_review(self, app=APP):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code(app=app)
        self.returned()
        self.bv("commit", "01", "--tests", "1/0")
        self.bv("stage", "review", "start")
        self.agent_ran("bv-reviewer")

    def edit_state(self, change):
        p = self.root / ".software-architector" / "state.json"
        st = json.loads(p.read_text("utf-8"))
        change(st)
        p.write_text(json.dumps(st), "utf-8")

    def finding(self, rule):
        return next(f for f in self.state()["audit"]["findings"] if f["rule"] == rule)

    def test_audit_writes_its_report_and_the_state(self):
        self.to_review(app=APP + "\n\ndef spare():\n    return 0\n")
        out = self.bv("audit")
        au = self.state()["audit"]
        self.assertEqual((au["head"], au["base"], au["tools"], au["accepted"]), (self.head(), self.state()["baseCommit"], {}, {}))
        self.assertTrue(au["at"])
        f = self.finding("unused-export")
        self.assertEqual((f["file"], f["sev"], f["kind"]), ("src/app.py", "low", "dead"))
        report = (self.rdir() / "audit.md").read_text("utf-8")
        self.assertIn(f["id"], report)
        self.assertIn("spare", report)
        self.assertIn("audit.md", out)
        self.assertIn("low 1", out.splitlines()[0])

    def test_a_dangerous_call_blocks_the_ticket_until_fixed_or_accepted_with_a_reason(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code(app="def add(a, b):\n    return eval('a + b')\n")
        self.returned()
        msg = self.fails("commit", "01", "--tests", "1/0")  # the check is green: the code audit alone refuses
        self.assertIn("code audit of ticket 01", msg)
        self.assertIn("eval", msg)
        self.assertNotIn("project check is red", msg)
        fid = bv.find_ticket(self.state(), "01")["audit"]["findings"][0]["id"]  # kept although the commit was refused
        self.assertIn("no finding", self.fails("audit", "--accept", "s-00000000", "--note", "x"))
        self.assertIn("--note", self.fails("audit", "--accept", fid))
        self.assertIn("--note", self.fails("audit", "--accept", fid, "--note", "  "))
        self.agent_ran("bv-reviewer", "Review of ticket 01", last=f"BLOCKING: none\nDROP: eval [{fid}] — a constant")
        self.bv("audit", "--accept", fid, "--note", "the expression is a constant, no user input reaches it")
        self.bv("commit", "01", "--tests", "1/0")
        self.bv("stage", "review", "start")
        self.agent_ran("bv-reviewer")
        self.assertIn("high 1 · medium 0 · low 0 · accepted 1", self.bv("audit"))
        self.assertEqual(self.state()["audit"]["accepted"], {fid: "the expression is a constant, no user input reaches it"})
        self.bv("verify")
        self.bv("stage", "final", "start")  # accepted at the ticket: the branch audit does not block it again
        self.agent_ran("bv-blind", last=json.dumps(BLIND_OK))
        self.bv("gate", "G4")
        self.bv("finish", "--result", "ok")
        st = self.state()
        self.assertIs(st["accepted"], True)
        self.assertIn("the expression is a constant, no user input reaches it", " ".join(st["lists"]["report"]))

    def test_a_finding_is_accepted_only_on_the_reviewers_last_word_drop(self):
        self.to_review(app=APP + "# TODO: negative numbers\n")
        self.bv("audit")
        fid = self.finding("stub")["id"]
        self.assertIn("has not dropped", self.fails("audit", "--accept", fid, "--note", "x"))  # no reviewer named it
        clock = iter(f"2030-01-01T00:00:{s:02d}+00:00" for s in range(60))  # one answer per second, in this order
        with mock.patch.object(bv.events, "now", lambda: next(clock)):
            self.agent_ran("bv-reviewer", last=f"DROP: the TODO [{fid}] — out of scope")
            self.agent_ran("bv-reviewer", last=f"FIX NOW: the TODO [{fid}] — src — handle negatives")
            self.assertIn("FIX NOW", self.fails("audit", "--accept", fid, "--note", "x"))
            self.agent_ran("bv-reviewer", last=f"- **DROP**: the TODO [{fid}] — out of scope")
        self.bv("audit", "--accept", fid, "--note", "negative numbers are out of scope")
        self.assertEqual(self.state()["audit"]["accepted"], {fid: "negative numbers are out of scope"})

    def test_a_secret_committed_before_the_run_is_found_at_delivery(self):
        for args in (["init", "-q"], ["add", "old/settings.py"], ["commit", "-q", "-m", "old work"]):
            if args[0] == "add":
                self.touch("old/settings.py", f"TOKEN = '{TOKEN}'\n")
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        self.to_review()
        out = self.bv("audit")
        self.assertIn("ghp_***", out)
        fid = self.finding("secret")["id"]
        self.assertNotIn(TOKEN, (self.rdir() / "audit.md").read_text("utf-8"))
        self.assertNotIn(TOKEN, (self.root / ".software-architector" / "state.json").read_text("utf-8"))
        problems = bv.delivery_problems(self.root, self.state())
        self.assertTrue(any(fid in p and "blocker" in p for p in problems), problems)

    def test_the_final_stage_needs_an_audit_of_this_review_round(self):
        self.to_review()
        self.assertIn("bv audit", self.fails("stage", "final", "start"))
        self.bv("audit")
        self.edit_state(lambda st: st["audit"].update(at="2000-01-01T00:00:00+00:00"))
        self.assertIn("bv audit", self.fails("stage", "final", "start"))
        self.bv("audit")
        self.bv("verify")
        self.bv("stage", "final", "start")

    def test_code_moved_after_the_audit_or_no_audit_at_all_stops_delivery(self):
        self.accepted_path()
        self.assertEqual([p for p in bv.delivery_problems(self.root, self.state()) if "audit" in p], [])
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "one more edit"], cwd=self.root, check=True)
        self.assertIn("repeat `bv audit`", self.fails("finish", "--result", "done"))
        self.edit_state(lambda st: st.pop("audit"))  # a state written before 3.6.0: still schema 3, still readable
        st = self.state()
        self.assertIn("no audit: run `bv audit` first", bv.delivery_problems(self.root, st))
        self.assertNotIn("Audit", bv.status_text(self.root, st, with_url=False))
        self.bv("status")

    def test_status_and_the_finish_report_name_the_audit_what_was_accepted_and_what_was_not_checked(self):
        self.to_review(app=APP + "# TODO: negative numbers\n")
        tools = {"npm": "missing", "pip-audit": "ran"}
        with mock.patch.object(bv.audit, "run_tools", return_value=(tools, [])) as rt, \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            self.assertIn("not checked: npm", self.bv("audit"))
        self.assertNotIn("ANTHROPIC_API_KEY", rt.call_args[0][2])  # the scanners get check_env, not the session's keys
        fid = self.finding("stub")["id"]
        self.agent_ran("bv-reviewer", last=f"DROP: the TODO [{fid}] — out of scope")
        self.bv("audit", "--accept", fid, "--note", "negative numbers are out of scope")
        text = bv.status_text(self.root, self.state(), with_url=False)
        for part in ("Audit", "not checked: npm", fid, "negative numbers are out of scope"):
            self.assertIn(part, text)
        self.assertNotIn("pip-audit", text)
        self.bv("verify")
        self.bv("stage", "final", "start")
        self.agent_ran("bv-blind", last=json.dumps(BLIND_OK))
        self.bv("gate", "G4")
        self.bv("finish", "--result", "ok")
        report = " ".join(self.state()["lists"]["report"])
        for part in ("Audit", "not checked: npm", fid, "negative numbers are out of scope"):
            self.assertIn(part, report)


if __name__ == "__main__":
    unittest.main()
