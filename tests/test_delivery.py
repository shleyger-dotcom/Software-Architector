"""'Delivered' must mean proven. Real projects, real commands, real clones: the honest run is accepted and every shortcut is refused."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from test_bv import Base, CONTRACT, NO_FINDINGS, PASS, bv

PY = f'"{Path(sys.executable).as_posix()}"'
RED = 'exit 1'
APP = "def add(a, b):\n    return a + b\n"
UNIT = "import unittest\nfrom src.app import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n"
E2E = "from src.app import add\nassert add(2, 3) == 5\nprint('1 scenario passed')\n"
SCENARIOS = r"testsPattern=^(\d+) scenario"  # the scenario script is no known runner: its count is read by this pattern
MANIFEST = """| ID | From the brief (verbatim) | Status | Basis | Where |
|---|---|---|---|---|
| R01 | "adds numbers" | in-spec | | |
| S01 | the user adds numbers from start to end | in-spec | | |
"""
BLIND_OK = {"run": {"ok": True, "commands": ["python e2e.py"]}, "outcome": {"solved": True, "why": "numbers add up"},
            "requirements": [{"id": "R01", "verdict": "implemented"}, {"id": "S01", "verdict": "implemented"}]}


class Delivery(Base):
    """A small real project taken through the run by a scripted orchestrator; the commands really execute."""

    def plan(self, manifest=MANIFEST, reqs="R01, S01", review="no", check=None):
        self.init()
        (self.rdir() / "manifest.md").write_text(manifest, "utf-8")
        self.write_ticket("01", reqs=reqs, zone="`src/` · `tests/` · `e2e.py`", wave=1, review=review)
        self.bv("stage", "spec", "start")
        self.agent_ran("bv-coverage", last=NO_FINDINGS)
        self.bv("gate", "G2", "--from", self.json_file({"items": []}))
        self.bv("set", f"checkCmd={check or PY + ' -m unittest discover -s tests'}", f"e2eCmd={PY} e2e.py", SCENARIOS)
        self.bv("tickets")
        self.bv("commit", "plan")
        self.bv("stage", "plan", "start")
        self.bv("stage", "build", "start")

    def write_code(self, app=APP):
        self.touch("src/__init__.py", "")
        self.touch("src/app.py", app)
        self.touch("tests/__init__.py", "")
        self.touch("tests/test_app.py", UNIT)
        self.touch("e2e.py", E2E)

    def returned(self, tid="01", reqs="R01 done | S01 done", tests="1/0"):
        p = self.root / "contract.txt"
        p.write_text(CONTRACT.format(status="DONE", tests=tests, reqs=reqs), "utf-8")
        self.bv("contract", tid, "--from", str(p))

    def build(self):
        self.bv("ticket", "01", "start")
        self.write_code()
        self.returned()
        self.bv("commit", "01", "--tests", "1/0")

    def review_round(self):
        self.bv("stage", "review", "start")
        self.agent_ran("bv-reviewer")
        self.bv("audit")

    def review(self):
        self.review_round()
        self.bv("verify")  # the review stage closes on both gates: execution and audit on the current commit
        self.bv("stage", "final", "start")

    def accepted_path(self):
        self.plan()
        self.build()
        self.review()
        self.bv("verify")
        self.agent_ran("bv-blind", last=json.dumps(BLIND_OK))
        self.bv("gate", "G4")

    def subject(self):
        return subprocess.run(["git", "log", "-1", "--format=%s"], cwd=self.root, capture_output=True, text=True,
                              encoding="utf-8").stdout.strip()


class HonestRunTest(Delivery):
    def test_a_real_project_goes_from_plan_to_accepted(self):
        self.accepted_path()
        st = self.state()
        self.assertEqual([s["step"] for s in st["gates"]["G5"]["steps"]], ["check", "e2e"])
        self.assertTrue(all(s["exit"] == 0 for s in st["gates"]["G5"]["steps"]))
        self.assertTrue(st["gates"]["G4"]["ok"])
        self.assertEqual(bv.find_ticket(st, "01")["proof"]["exit"], 0)
        self.assertIn("Build delivered", self.bv("finish", "--result", "numbers add up"))
        self.assertIs(self.state()["accepted"], True)
        self.assertEqual(self.subject(), bv.FINAL_SUBJECT)

    def test_a_failure_found_at_acceptance_is_repaired_and_then_accepted(self):
        self.plan()
        self.build()
        self.review()
        self.bv("verify")
        bad = {**BLIND_OK, "requirements": [{"id": "R01", "verdict": "partial"}, {"id": "S01", "verdict": "implemented"}]}
        self.agent_ran("bv-blind", last=json.dumps(bad))
        self.bv("gate", "G4")
        self.assertIn("R01", self.fails("finish", "--result", "done"))
        (self.rdir() / "tickets" / "F1-fix.md").write_text(
            "# F1 — Fix\n**Requirements:** R01\n**Blocked by:** —\n**Zone:** `src/`\n**Wave:** 2\n**Review:** no\n", "utf-8")
        self.bv("tickets")
        self.bv("ticket", "F1", "start")
        self.touch("src/app.py", APP + "\n# edited\n")
        self.returned("F1", reqs="R01 done")
        self.bv("commit", "F1", "--tests", "1/0")
        msg = self.fails("finish", "--result", "done")
        self.assertIn("after `bv verify`", msg)
        self.assertIn("code changed after the last code review", msg)  # a patch re-runs every check, review included
        self.agent_ran("bv-reviewer", "Review of ticket F1")
        self.bv("audit")  # a repair round moves HEAD: the audit is repeated
        self.bv("verify")
        self.agent_ran("bv-blind", last=json.dumps(BLIND_OK))
        self.bv("gate", "G4")
        self.bv("finish", "--result", "done")
        self.assertIs(self.state()["accepted"], True)


class CheckIsRunNotClaimedTest(Delivery):
    def test_a_red_project_cannot_commit_whatever_is_claimed(self):
        self.plan(check=RED)
        self.bv("ticket", "01", "start")
        self.write_code()
        self.returned(tests="999/0")
        msg = self.fails("commit", "01", "--tests", "999/0")
        self.assertIn("is red", msg)
        self.assertEqual(bv.find_ticket(self.state(), "01")["commit"], "")

    def test_a_really_failing_test_stops_the_commit(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code(app="def add(a, b):\n    return 0\n")
        self.returned()
        self.assertIn("FAILED", self.fails("commit", "01", "--tests", "1/0"))

    def test_a_hung_check_is_killed_and_counts_as_red(self):
        self.plan(check=f'{PY} -c "import time; time.sleep(60)"')
        self.bv("set", "checkTimeout=1")
        self.bv("ticket", "01", "start")
        self.write_code()
        self.returned()
        start = time.time()
        self.assertIn("did not finish in", self.fails("commit", "01", "--tests", "1/0"))
        self.assertLess(time.time() - start, 30)

    def test_build_needs_a_check_command_and_a_scenario_command(self):
        self.init()
        (self.rdir() / "manifest.md").write_text(MANIFEST, "utf-8")
        self.write_ticket("01", reqs="R01, S01", wave=1)
        self.bv("stage", "spec", "start")
        self.agent_ran("bv-coverage", last=NO_FINDINGS)
        self.bv("gate", "G2", "--from", self.json_file({"items": []}))
        self.bv("tickets")
        self.assertIn("checkCmd", self.fails("stage", "build", "start"))
        self.bv("set", f"checkCmd={PASS}")
        self.assertIn("e2eCmd", self.fails("stage", "build", "start"))

    def test_no_contract_and_zero_tests_are_refused(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code()
        self.assertIn("contract not checked", self.fails("commit", "01", "--tests", "1/0"))
        self.returned(tests="0/0")
        self.assertIn("empty set", self.fails("commit", "01", "--tests", "0/0"))

    def test_green_exit_with_no_tests_inside_is_red(self):
        self.plan(check=f'{PY} -c "print(\'Ran 0 tests in 0.001s\'); print(\'OK\')"')
        self.bv("ticket", "01", "start")
        self.write_code()
        self.returned(tests="5/0")
        self.assertIn("0 tests", self.fails("commit", "01", "--tests", "5/0"))
        self.bv("set", f"checkCmd={PY} -c \"print('No tests found, exiting with code 0')\"", "--reason", "test")
        self.assertIn("0 tests", self.fails("commit", "01", "--tests", "5/0"))
        self.bv("set", f"checkCmd={PASS}", f"e2eCmd={PY} -c \"print('0 passing')\"", "--reason", "test")
        self.bv("commit", "01", "--tests", "5/0")
        self.review_round()
        self.assertIn("0 tests", self.fails("verify"))
        self.assertIn("bv verify", self.fails("stage", "final", "start"))  # a red execution result keeps the stage open

    def test_an_unrecognized_runner_is_red_until_its_count_is_named(self):
        self.plan(check=f'{PY} -c "print(\'checked 4 files, all good\')"')
        self.bv("ticket", "01", "start")
        self.write_code()
        self.returned()
        self.assertIn("not recognized", self.fails("commit", "01", "--tests", "1/0"))
        self.assertIn("group 1", self.fails("set", "testsPattern=checked", "--reason", "test"))
        self.assertIn("group 1", self.fails("set", "testsPattern=(", "--reason", "test"))
        self.bv("set", r"testsPattern=checked (\d+) files", "--reason", "the runner prints its own")
        self.bv("commit", "01", "--tests", "1/0")
        self.assertEqual(bv.find_ticket(self.state(), "01")["tests"], {"passed": 4, "failed": 0})

    def test_what_decides_green_is_frozen_once_the_build_starts(self):
        self.plan()
        for key in ("checkCmd", "e2eCmd", "setupCmd", "testsPattern"):
            self.assertIn("frozen", self.fails("set", f"{key}=exit (0)"), key)
        self.bv("set", f"e2eCmd={PY} e2e.py")  # the same value is no change
        self.bv("set", "checkTimeout=60", "title=x")  # the rest stays free
        self.bv("set", f"checkCmd={PASS}", "--reason", "runner changed")
        st = self.state()
        self.assertEqual(st["checkCmd"], PASS)
        self.assertIn("runner changed", " ".join(st["lists"]["report"]))
        self.assertIn("checkCmd changed after the build started", " ".join(st["lists"]["report"]))

    def test_the_count_is_measured_and_a_lost_test_is_noticed(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code()
        two = UNIT + "\n    def test_add_zero(self):\n        self.assertEqual(add(2, 0), 2)\n"
        self.touch("tests/test_app.py", two)
        self.returned(tests="999/0")
        self.bv("commit", "01", "--tests", "999/0")
        self.assertEqual(bv.find_ticket(self.state(), "01")["tests"], {"passed": 2, "failed": 0})
        (self.rdir() / "tickets" / "F1-fix.md").write_text(
            "# F1 — Fix\n**Requirements:** R01\n**Blocked by:** —\n**Zone:** `tests/`\n**Wave:** 2\n**Review:** no\n", "utf-8")
        self.bv("tickets")
        self.bv("ticket", "F1", "start")
        self.touch("tests/test_app.py", UNIT)
        self.returned("F1", reqs="R01 done", tests="2/0")
        self.assertIn("count dropped (2 → 1)", self.fails("commit", "F1", "--tests", "2/0"))
        self.bv("commit", "F1", "--tests", "2/0", "--fewer-tests", "removed a duplicate")
        self.assertIn("removed a duplicate", " ".join(self.state()["lists"]["report"]))

    def test_counts_of_common_runners_are_read_and_unknown_ones_are_not_guessed(self):
        for out, n in (("Ran 12 tests in 0.5s\n\nOK", 12), ("=== 7 passed, 1 warning in 0.12s ===", 7), ("3 passed in 0.1s", 3),
                       ("Test Suites: 2 passed, 2 total\nTests:       1 skipped, 9 passed, 10 total", 9),
                       ("\x1b[32m Tests \x1b[39m 4 passed (4)", 4), ("  5 passing (20ms)", 5),
                       ("test result: ok. 6 passed; 0 failed", 6), ("Ran 0 tests in 0.000s\n\nOK", 0),
                       ("No tests found, exiting with code 0", 0), ("ok  	example.com/pkg	0.1s", None), ("build passed", None)):
            self.assertEqual(bv.measured_tests(out), n, out)

    def test_contract_must_account_for_every_requirement_and_prove_tests(self):
        self.plan()
        self.bv("ticket", "01", "start")
        p = self.root / "c.txt"
        p.write_text(CONTRACT.format(status="DONE", tests="1/0", reqs="R01 done"), "utf-8")
        self.assertIn("no report for S01", self.fails("contract", "01", "--from", str(p)))
        p.write_text(CONTRACT.format(status="DONE", tests="1/0", reqs="R01 done | S01 done").replace(
            "test_x → expectation did not match", "—"), "utf-8")
        self.assertIn("REDPROOF", self.fails("contract", "01", "--from", str(p)))
        p.write_text(CONTRACT.format(status="DONE", tests="1/0", reqs="R01 done | S01 placeholder — no price"), "utf-8")
        self.bv("contract", "01", "--from", str(p))
        self.assertEqual(bv.find_ticket(self.state(), "01")["placeholders"], ["S01"])


class CleanCloneTest(Delivery):
    def test_works_only_because_of_an_uncommitted_file(self):
        """Green in the working folder, broken for everyone else: only a clean clone can see it."""
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code(app="from helper_outside_zone import ONE\n\n\ndef add(a, b):\n    return a + b + ONE - 1\n")
        self.touch("helper_outside_zone.py", "ONE = 1\n")
        self.returned()
        self.bv("commit", "01", "--tests", "1/0")
        self.review_round()
        msg = self.fails("verify")
        self.assertIn("G5 ✗", msg)
        self.assertIn("helper_outside_zone", msg)
        self.assertFalse(self.state()["gates"]["G5"]["ok"])
        self.assertIn("G5", self.fails("gate", "G4"))
        self.assertIn("clean clone", self.fails("finish", "--result", "done"))

    def test_a_broken_scenario_stops_verify_though_unit_tests_pass(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code()
        self.touch("e2e.py", "raise SystemExit('scenario broken')\n")
        self.returned()
        self.bv("commit", "01", "--tests", "1/0")
        self.review_round()
        self.assertIn("scenario broken", self.fails("verify"))

    def test_the_clone_gets_env_example_and_the_real_env_only_on_request(self):
        """M10: a stranger has no .env of ours; the real one goes into a clone only with --use-real-env."""
        # the name is built at runtime: a literal `.env` in a check command is refused by the guard rules
        check = (f'{PY} -c "import os, sys; n = chr(46) + \'env\'; t = open(n).read() if os.path.exists(n) else \'\'; '
                 f'print(\'Ran 1 test in 0s\'); sys.exit(3 if \'MODE=real\' in t else 0 if \'CI=1\' in t else 4)"')
        self.plan(check=check)
        self.bv("ticket", "01", "start")
        self.write_code()
        self.touch(".env.example", "MODE=\n")
        self.touch(".env", "")  # the working folder: a check here sees an empty .env, not CI=1
        self.returned()
        self.assertIn("exit code 4", self.fails("commit", "01", "--tests", "1/0"))
        (self.root / ".env").unlink()
        self.bv("set", f"checkCmd={check.replace('else 4', 'else 0')}", "--reason", "test")
        self.bv("commit", "01", "--tests", "1/0")
        self.review()
        self.touch(".env", "MODE=real\n")
        self.bv("set", f"checkCmd={check}", "--reason", "test")
        self.bv("verify")
        self.assertEqual(self.state()["gates"]["G5"]["env"], "example")
        self.assertIn("exit code 3", self.fails("verify", "--use-real-env"))
        self.assertIn("real .env", " ".join(self.state()["lists"]["report"]))
        self.bv("verify")
        self.bv("blind-clone")
        clone = Path(self.state()["blindClone"])
        self.assertEqual((clone / ".env").read_text("utf-8"), "MODE=\nCI=1\n")
        self.bv("blind-clone", "--use-real-env")
        self.assertEqual((Path(self.state()["blindClone"]) / ".env").read_text("utf-8"), "MODE=real\n")

    def test_setup_runs_first_and_its_failure_stops_everything(self):
        self.plan()
        self.build()
        self.review()
        self.bv("set", f"setupCmd={RED}", "--reason", "test")
        self.fails("verify")
        self.assertEqual([s["step"] for s in self.state()["gates"]["G5"]["steps"]], ["setup"])

    def test_clone_does_not_carry_the_runs_records(self):
        self.plan(check=f'{PY} -c "import os, sys; sys.exit(os.path.exists(\'.software-architector\'))"')
        self.bv("ticket", "01", "start")
        self.write_code()
        self.returned()
        (self.root / ".software-architector" / "state.json").read_text("utf-8")
        self.fails("commit", "01", "--tests", "1/0")  # the working folder has the records: this check is red there
        self.bv("set", f"checkCmd={PASS}", "--reason", "test")
        self.bv("commit", "01", "--tests", "1/0")
        self.bv("set", f'checkCmd={PY} -c "import os, sys; print(\'Ran 1 test in 0s\'); '
                f'sys.exit(os.path.exists(\'.software-architector\'))"', "--reason", "test")
        self.review()
        self.bv("verify")  # green only if the clone has no records

    def test_acceptance_goes_stale_when_code_moves(self):
        self.accepted_path()
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "one more edit"], cwd=self.root, check=True)
        self.assertIn("after `bv verify`", self.fails("finish", "--result", "done"))


class AcceptanceCannotBeFakedTest(Delivery):
    def to_acceptance(self):
        self.plan()
        self.build()
        self.review()
        self.bv("verify")

    def test_the_blind_clone_has_no_history_and_no_records(self):
        """M6: a `git clone` minus the folder still carried spec and manifest in its history (`git log -p`)."""
        self.plan()
        self.build()
        self.review_round()
        self.assertIn("G5", self.fails("blind-clone"))
        self.bv("verify")
        self.bv("stage", "final", "start")
        self.bv("blind-clone")
        first = Path(self.state()["blindClone"])
        self.assertTrue((first / "src" / "app.py").exists())
        self.assertFalse((first / ".software-architector").exists())
        self.assertFalse((first / ".git").exists())
        self.bv("blind-clone")
        second = Path(self.state()["blindClone"])
        self.assertNotEqual(first, second)
        self.assertFalse(first.parent.exists())
        self.agent_ran("bv-blind", last=json.dumps(BLIND_OK))
        self.bv("gate", "G4")
        self.bv("finish", "--result", "done")
        self.assertFalse(second.parent.exists())

    def test_brief_lines_without_an_id_are_not_lost(self):
        self.to_acceptance()
        extra = {"id": "and export to csv", "verdict": "no", "where": "no such command"}
        self.agent_ran("bv-blind", last=json.dumps({**BLIND_OK, "requirements": BLIND_OK["requirements"] + [extra]}))
        self.assertIn("brief line without an ID in the manifest 'and export to csv'", self.bv("gate", "G4"))
        self.assertFalse(self.state()["gates"]["G4"]["ok"])
        ok = {**extra, "verdict": "implemented"}
        self.agent_ran("bv-blind", last=json.dumps({**BLIND_OK, "requirements": BLIND_OK["requirements"] + [ok]}))
        self.bv("gate", "G4")
        g4 = self.state()["gates"]["G4"]
        self.assertTrue(g4["ok"])
        self.assertEqual(g4["unmapped"][0]["id"], "and export to csv")

    def test_blind_agent_must_really_have_run(self):
        self.to_acceptance()
        self.assertIn("bv-blind", self.fails("gate", "G4"))

    def test_coverage_agent_must_really_have_run(self):
        self.init()
        self.assertIn("bv-coverage", self.fails("gate", "G2", "--from", self.json_file({"items": []})))

    def test_coverage_findings_come_from_the_agent_not_from_the_orchestrator(self):
        """M2: a finding the orchestrator leaves out of its resolutions stays unresolved; one it invents counts for nothing."""
        self.init()
        self.write_manifest()
        found = {"items": [{"kind": "missing", "quote": "report in excel", "what": "no export"}]}
        self.agent_ran("bv-coverage", last="Result:\n```json\n" + json.dumps(found, ensure_ascii=False) + "\n```")
        self.bv("gate", "G2", "--from", self.json_file({"items": []}))
        self.assertFalse(self.state()["gates"]["G2"]["ok"])
        self.bv("gate", "G2", "--from", self.json_file({"items": [{"quote": "other", "resolution": "fixed"}]}))
        self.assertFalse(self.state()["gates"]["G2"]["ok"])
        self.bv("gate", "G2", "--from", self.json_file({"items": [{"quote": "report in excel", "resolution": "fixed"}]}))
        g2 = self.state()["gates"]["G2"]
        self.assertTrue(g2["ok"])
        self.assertEqual((g2["found"], g2["items"][0]["what"]), (1, "no export"))
        self.agent_ran("bv-coverage", last="found a few places, will describe now")
        self.assertIn("not parsed", self.fails("gate", "G2", "--from", self.json_file({"items": []})))

    def test_attached_means_a_real_row_of_the_manifest(self):
        """Seen in a real run: the orchestrator marked findings «attached» with no row behind them."""
        self.init()
        self.write_manifest()
        item = {"kind": "extra", "quote": "export to pdf", "resolution": "attached"}
        self.agent_ran("bv-coverage", last=json.dumps({"items": [{"kind": "extra", "quote": "export to pdf"}]}))
        out = self.bv("gate", "G2", "--from", self.json_file({"items": [item]}))
        self.assertIn("export to pdf", out)
        self.assertFalse(self.state()["gates"]["G2"]["ok"])
        self.bv("gate", "G2", "--from", self.json_file({"items": [{**item, "row": "A99"}]}))
        self.assertFalse(self.state()["gates"]["G2"]["ok"])
        self.bv("gate", "G2", "--from", self.json_file({"items": [{**item, "row": "R01"}]}))
        self.assertTrue(self.state()["gates"]["G2"]["ok"])

    def test_claims_without_commands_outcome_or_verdicts_fail_the_gate(self):
        self.to_acceptance()
        cases = [({**BLIND_OK, "run": {"ok": True, "commands": []}}, "no command"),
                 ({**BLIND_OK, "run": {"ok": False, "commands": ["x"]}}, "did not start"),
                 ({k: v for k, v in BLIND_OK.items() if k != "outcome"}, "user's task"),
                 ({**BLIND_OK, "outcome": {"solved": False, "why": "counts wrong"}}, "counts wrong"),
                 ({**BLIND_OK, "requirements": [{"id": "R01", "verdict": "implemented"}]}, "S01: blind acceptance did not check it")]
        for data, expected in cases:
            self.agent_ran("bv-blind", last=json.dumps(data))
            self.assertIn(expected, self.bv("gate", "G4"))
            self.assertFalse(self.state()["gates"]["G4"]["ok"])
            self.assertIn("cannot deliver", self.fails("finish", "--result", "done"))

    def test_a_placeholder_must_at_least_be_visible(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code()
        self.returned(reqs="R01 placeholder — no price | S01 done")
        self.bv("commit", "01", "--tests", "1/0")
        self.review()
        self.bv("verify")
        ok = {**BLIND_OK, "requirements": [{"id": "R01", "verdict": "partial"}, {"id": "S01", "verdict": "implemented"}]}
        self.agent_ran("bv-blind", last=json.dumps(ok))
        self.bv("gate", "G4")
        self.assertTrue(self.state()["gates"]["G4"]["ok"])
        no = {**BLIND_OK, "requirements": [{"id": "R01", "verdict": "no"}, {"id": "S01", "verdict": "implemented"}]}
        self.agent_ran("bv-blind", last=json.dumps(no))
        self.assertIn("the stub is not visible", self.bv("gate", "G4"))

    def test_what_could_not_be_proven_is_named_in_the_verdict(self):
        """Seen in a real run: nobody may send a real email, so such a project could never be accepted at all."""
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code()
        self.returned(reqs="R01 placeholder — needs a SendGrid key | S01 done")
        self.bv("commit", "01", "--tests", "1/0")
        self.review()
        self.bv("verify")
        ok = {**BLIND_OK, "requirements": [{"id": "R01", "verdict": "partial"}, {"id": "S01", "verdict": "implemented"}]}
        self.agent_ran("bv-blind", last=json.dumps(ok))
        self.bv("gate", "G4")
        self.assertIn("Build delivered, except: R01", self.bv("finish", "--result", "works"))
        st = self.state()
        self.assertIs(st["accepted"], True)
        self.assertEqual(st["unproven"], ["R01"])
        self.assertIn("Build delivered, except: R01", bv.status_text(self.root, st, with_url=False))
        self.bv("reopen")
        self.assertEqual(self.state()["unproven"], [])

    def test_a_row_already_done_can_become_a_placeholder_through_a_ticket(self):
        """Seen in a real run: the executor first said done; the repair ticket said placeholder, and bv ignored it."""
        self.plan()
        self.build()
        self.review()
        self.bv("verify")
        partial = {**BLIND_OK, "requirements": [{"id": "R01", "verdict": "implemented"}, {"id": "S01", "verdict": "partial"}]}
        self.agent_ran("bv-blind", last=json.dumps(partial))
        self.bv("gate", "G4")
        self.assertFalse(self.state()["gates"]["G4"]["ok"])
        (self.rdir() / "tickets" / "F1-fix.md").write_text(
            "# F1 — Manual check\n**Requirements:** S01\n**Blocked by:** —\n**Zone:** `README.md`\n**Wave:** 2\n**Review:** no\n", "utf-8")
        self.bv("tickets")
        self.bv("ticket", "F1", "start")
        self.touch("README.md", "Check it once by hand.\n")
        self.returned("F1", reqs="S01 placeholder — needs the user's key")
        self.assertIn("--external", self.fails("commit", "F1", "--tests", "1/0"))  # M7: done → placeholder needs a reason
        self.assertIn("--external", self.fails("commit", "F1", "--tests", "1/0", "--external", " "))
        self.bv("set", "mode=full")
        self.bv("commit", "F1", "--tests", "1/0", "--external", "SendGrid: the email goes out with the user's key")
        report = " ".join(self.state()["lists"]["report"])
        self.assertIn("S01: was done, now placeholder — external service: SendGrid", report)
        self.assertIn("Full mode", report)
        rows = {r["id"]: r["status"] for r in bv.read_manifest(self.rdir() / "manifest.md")}
        self.assertEqual((rows["R01"], rows["S01"]), ("done", "placeholder"))
        self.agent_ran("bv-reviewer", "Review of ticket F1")
        self.bv("audit")
        self.bv("verify")
        self.agent_ran("bv-blind", last=json.dumps(partial))
        self.bv("gate", "G4")
        self.assertIn("Build delivered, except: S01", self.bv("finish", "--result", "works"))

    def test_a_missing_or_undecodable_verdict_is_a_plain_error(self):
        """Seen in a real run: the agent had not returned its verdict yet and `bv gate G4` died with a traceback."""
        self.to_acceptance()
        self.agent_ran("bv-blind", last="starting the project…")
        self.assertIn("not parsed", self.fails("gate", "G4"))
        self.agent_ran("bv-blind", last='["not an object"]')
        self.assertIn("not parsed", self.fails("gate", "G4"))
        bad = self.root / "cp1251.json"
        bad.write_bytes('{"items": "café"}'.encode("latin-1"))
        self.assertIn("cannot read", self.fails("gate", "G2", "--from", str(bad)))
        self.assertIn("cannot read", self.fails("contract", "01", "--from", str(self.root / "no-such-file.txt")))

    def test_the_blind_verdict_comes_from_the_journal_never_retyped(self):
        """Seen in a real run: the orchestrator retyped the verdict and cut 13 real commands down to 3 summaries (M2)."""
        self.to_acceptance()
        self.agent_ran("bv-blind", last="```json\n" + json.dumps(BLIND_OK) + "\n```")
        forged = {**BLIND_OK, "requirements": [{"id": "R01", "verdict": "implemented"}]}
        for src in (self.json_file(forged), "-"):
            self.assertIn("hook journal", self.fails("gate", "G4", "--from", src))
        self.bv("gate", "G4")
        g4 = self.state()["gates"]["G4"]
        self.assertTrue(g4["ok"])
        self.assertEqual(g4["checked"], 2)


class FinishTellsTheTruthTest(Delivery):
    def test_empty_run_cannot_be_delivered(self):
        self.init()
        self.assertIn("no ticket at all", self.fails("finish", "--result", "all works"))
        self.assertFalse(self.state().get("finishedAt"))

    def test_review_and_final_need_tickets(self):
        self.init()
        self.assertIn("no ticket at all", self.fails("stage", "review", "start"))

    def test_failed_ticket_blocks_delivery_but_the_run_can_close_honestly(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.bv("ticket", "01", "fail", "--note", "no access to the API")
        msg = self.fails("finish", "--result", "done")
        self.assertIn("no access to the API", msg)
        self.assertIn("R01: not delivered", msg)
        self.assertIn("--not-accepted", msg)
        out = self.bv("finish", "--not-accepted", "--result", "stuck on API access")
        self.assertIn("WITHOUT acceptance", out)
        st = self.state()
        self.assertIs(st["accepted"], False)
        self.assertTrue(st["finishedAt"])
        self.assertTrue(any("no access to the API" in p for p in st["problems"]))
        self.assertEqual(self.subject(), bv.NOT_ACCEPTED_SUBJECT)
        self.assertIn("WITHOUT acceptance", bv.status_text(self.root, st, with_url=False))
        self.bv("reopen")
        self.assertIsNone(self.state()["accepted"])

    def test_erased_requirement_is_noticed(self):
        self.accepted_path()
        p = self.rdir() / "manifest.md"
        p.write_text("\n".join(x for x in p.read_text("utf-8").splitlines() if "S01" not in x) + "\n", "utf-8")
        self.assertIn("S01: row vanished", self.fails("finish", "--result", "done"))

    def test_status_written_by_hand_is_noticed(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.bv("ticket", "01", "fail", "--note", "failed")
        p = self.rdir() / "manifest.md"
        p.write_text(p.read_text("utf-8").replace("in-ticket", "done"), "utf-8")
        self.assertIn("its tickets are not closed", self.fails("finish", "--result", "done"))

    def test_open_requirement_stops_the_plan_and_deferred_needs_a_reason(self):
        self.init()
        self.write_manifest(r01="open")
        p = self.rdir() / "manifest.md"
        p.write_text(p.read_text("utf-8").replace("| open | |", "| open | decide later |"), "utf-8")
        self.write_ticket("01", reqs="R01.1, R02, S01", wave=1)
        self.bv("stage", "spec", "start")
        self.assertIn("R01: open", self.fails("tickets"))
        self.write_manifest(r01="deferred")
        self.assertIn("deferred only with a reason", self.fails("tickets"))

    def test_guard_switched_off_goes_to_the_report(self):
        self.accepted_path()
        self.bv("set", "guard=off")
        self.bv("set", "guard=on")
        self.bv("finish", "--result", "done")
        self.assertIn("guard", " ".join(self.state()["lists"]["report"]))

    def test_failed_landing_commit_leaves_the_run_open(self):
        self.accepted_path()
        wip = self.rdir()
        self.touch("AGENTS.md", "TOKEN = '" + "ghp_" + "a" * 36 + "'\n")
        self.assertIn("secret", self.fails("finish", "--result", "done"))
        self.assertTrue(wip.exists())
        self.assertFalse(self.state().get("finishedAt"))
        self.touch("AGENTS.md", "clean\n")
        self.bv("finish", "--result", "done")
        self.assertIn("already finished", self.fails("finish", "--result", "once more"))


class ReceiptTest(Delivery):
    def receipt(self, out):
        self.assertIn("Receipt", out)
        return out.split("Receipt", 1)[1]

    def test_delivery_prints_changed_files_tickets_and_every_gate(self):
        self.plan()
        self.bv("ticket", "01", "start")
        # the bug: the check is red. Its size differs from the fix, or a .pyc of the same second and size would stay valid
        self.write_code(app="def add(a, b):\n    return a - b  # bug\n")
        self.returned()
        self.assertIn("project check is red", self.fails("commit", "01", "--tests", "1/0"))
        self.bv("ticket", "01", "repair", "--note", "add subtracts")
        self.touch("src/app.py", APP)
        self.returned()
        self.bv("commit", "01", "--tests", "1/0")
        self.review()
        self.bv("verify")
        self.agent_ran("bv-blind", last=json.dumps(BLIND_OK))
        self.bv("gate", "G4")
        receipt = self.receipt(self.bv("finish", "--result", "numbers add up"))
        for f in ("src/app.py", "tests/test_app.py", "e2e.py"):
            self.assertIn(f"A\t{f}", receipt)
        self.assertNotIn(".software-architector", receipt)
        self.assertIn("T01 done", receipt)
        self.assertIn("repairs 1", receipt)
        for g in bv.GATES:
            self.assertIn(f"{g} ✓", receipt)
        self.assertIn("check exit 0 · tests 1", receipt)
        self.assertIn("e2e exit 0 · tests 1", receipt)
        self.assertIn("Audit ", receipt)

    def test_closing_without_acceptance_shows_what_is_missing(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.bv("ticket", "01", "fail", "--note", "no access to the API")
        receipt = self.receipt(self.bv("finish", "--not-accepted", "--result", "nothing works"))
        self.assertIn("T01 failed · commit — · tests —", receipt)
        self.assertIn("Files changed since the plan", receipt)
        self.assertIn("  —", receipt)  # the plan commit is the last code commit: nothing built
        self.assertIn("G5 —", receipt)
        self.assertIn("G4 —", receipt)
        self.assertIn("no audit", receipt)

    def test_no_plan_commit_is_said_not_guessed(self):
        st = {"baseCommit": None, "tickets": [], "gates": {g: None for g in bv.GATES}}
        text = bv.receipt(self.root, st, "abc1234")
        self.assertIn("no baseCommit", text)
        self.assertIn("Tickets: —", text)


class BothGatesTest(Delivery):
    """No code stage closes without an execution result and a code audit, and a patch re-runs every check of its stage."""

    def test_a_red_check_and_an_audit_finding_are_reported_together(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code(app="def add(a, b):\n    return eval('a - b')\n")
        self.returned()
        msg = self.fails("commit", "01", "--tests", "1/0")
        self.assertIn("project check is red", msg)
        self.assertIn("code audit of ticket 01", msg)
        self.assertFalse(bv.find_ticket(self.state(), "01")["commit"])

    def test_the_ticket_audit_reads_only_what_the_ticket_changed(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code()
        self.touch("tools/old.py", "eval('1')\n")  # outside the zone: the branch audit's business, not this ticket's
        self.returned()
        self.bv("commit", "01", "--tests", "1/0")
        au = bv.find_ticket(self.state(), "01")["audit"]
        self.assertEqual(au["findings"], [])
        self.assertEqual(au["files"], 5)

    def test_a_fix_in_review_reruns_every_check_of_the_stage(self):
        self.plan()
        self.build()
        self.bv("stage", "review", "start")
        self.agent_ran("bv-reviewer")
        self.bv("audit")
        self.bv("verify")
        (self.rdir() / "tickets" / "F1-fix.md").write_text(
            "# F1 — Fix\n**Requirements:** R01\n**Blocked by:** —\n**Zone:** `src/`\n**Wave:** 2\n**Review:** no\n", "utf-8")
        self.bv("tickets")
        self.bv("ticket", "F1", "start")
        self.touch("src/app.py", APP + "\n# reviewed\n")
        self.returned("F1", reqs="R01 done")
        self.bv("commit", "F1", "--tests", "1/0")
        self.assertIn("after the last code review", self.fails("stage", "final", "start"))
        self.agent_ran("bv-reviewer", "Review of ticket F1")
        self.assertIn("repeat `bv audit`", self.fails("stage", "final", "start"))
        self.bv("audit")
        self.assertIn("bv verify", self.fails("stage", "final", "start"))
        self.bv("verify")
        self.bv("stage", "final", "start")

    def test_an_open_blocking_finding_stops_the_review_stage(self):
        self.plan()
        self.build()
        self.bv("stage", "review", "start")
        self.agent_ran("bv-reviewer")
        self.touch("tools/old.py", "eval('1')\n")
        subprocess.run(["git", "add", "tools/old.py"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "an old tool"], cwd=self.root, check=True)
        self.bv("audit")
        self.bv("verify")
        self.assertIn("blocking findings are open", self.fails("stage", "final", "start"))


class StageReceiptTest(Delivery):
    def test_every_closed_stage_leaves_a_receipt(self):
        self.accepted_path()
        out = self.bv("finish", "--result", "numbers add up")
        self.assertIn("Receipt — stage Acceptance", out)
        receipts = self.state()["receipts"]
        by_stage = {r.split("(")[0].removeprefix("Receipt — stage ").strip(): r for r in receipts}
        # this scripted run jumps over Requirements and Briefing (`skipped`): a stage that never ran leaves no receipt
        self.assertEqual(list(by_stage), ["Preflight", "Specification", "Plan", "Build", "Code review", "Acceptance"])
        self.assertIn("not applicable", by_stage["Specification"])
        build = by_stage["Build"]
        for part in ("A\tsrc/app.py", "T01 done", "audit blocker 0, high 0"):
            self.assertIn(part, build)
        self.assertNotIn("A\tsrc/app.py", by_stage["Code review"])  # nothing new was committed in the review
        self.assertIn("Branch Audit", by_stage["Code review"])
        self.assertIn("G5 ✓", by_stage["Code review"])
        self.assertIn("G4 ✓", by_stage["Acceptance"])
        md = (self.root / ".software-architector" / self.state()["dir"] / "receipts.md").read_text("utf-8")
        self.assertEqual(md.count("```text"), len(receipts))

    def test_a_stage_from_an_older_state_says_its_files_are_unknown(self):
        self.init()
        text = bv.stage_receipt(self.root, self.state(), {"label": "Build", "status": "done", "startedAt": None})
        self.assertIn("unknown", text)
        self.assertIn("not applicable", text)


class ProcessCannotBeSkippedTest(Delivery):
    def test_repair_cannot_be_closed_without_a_new_commit(self):
        self.plan()
        self.build()
        self.bv("ticket", "01", "repair", "--note", "red test")
        self.assertIn("status repair", self.fails("ticket", "01", "done"))

    def test_reviewed_ticket_needs_the_reviewer_to_have_run_after_the_commit(self):
        self.plan(review="yes — money")
        self.build()
        self.assertEqual(bv.find_ticket(self.state(), "01")["status"], "review")
        self.assertIn("bv-reviewer", self.fails("ticket", "01", "done"))
        self.agent_ran("bv-reviewer", "Review of ticket 02")  # M1: a reviewer of another ticket proves nothing here
        self.agent_ran("bv-reviewer", "branch review")
        self.assertIn("Review of ticket 01", self.fails("ticket", "01", "done"))
        self.agent_ran("bv-reviewer", "Review of ticket 01")
        self.bv("ticket", "01", "done")

    def test_branch_review_cannot_be_skipped_or_faked(self):
        self.plan()
        self.build()
        self.assertIn("cannot be skipped", self.fails("stage", "review", "skip", "--note", "no time"))
        self.assertIn("code review", self.fails("stage", "final", "start"))
        self.bv("stage", "review", "start")
        self.assertIn("bv-reviewer", self.fails("stage", "final", "start"))
        self.agent_ran("bv-reviewer")
        self.assertIn("bv audit", self.fails("stage", "final", "start"))
        self.bv("audit")
        self.assertIn("bv verify", self.fails("stage", "final", "start"))
        self.bv("verify")
        self.bv("stage", "final", "start")

    def test_g1_needs_a_scenario(self):
        self.init()
        (self.rdir() / "manifest.md").write_text(MANIFEST.replace("| S01 | the user adds numbers from start to end | in-spec | | |\n", ""), "utf-8")
        self.assertIn("end-to-end scenario", self.fails("stage", "spec", "start"))

    def test_doctor_stops_a_run_whose_hooks_do_not_fire(self):
        self.init()
        self.assertIn("journal", self.fails("doctor"))
        self.agent_ran("bv-scout")
        self.bv("doctor")


class RobustnessTest(Delivery):
    def test_source_files_in_a_legacy_encoding_do_not_crash_the_commit(self):
        self.plan()
        self.bv("ticket", "01", "start")
        self.write_code()
        (self.root / "src" / "legacy.py").write_bytes("# café comment in latin-1\n".encode("latin-1"))
        self.returned()
        self.assertIn("committed", self.bv("commit", "01", "--tests", "1/0"))

    def test_a_reader_holding_state_json_does_not_break_a_write(self):
        """On Windows a reader (dashboard, hook) makes os.replace fail for a moment. An open file does not do that on
        POSIX, so the refusal itself is simulated: the test means the same on every OS (L12)."""
        self.init()
        real, calls = os.replace, []

        def busy_then_free(src, dst):
            calls.append(dst)
            if len(calls) <= 3:
                raise PermissionError("held by a reader")
            return real(src, dst)
        with mock.patch.object(bv.os, "replace", busy_then_free):
            self.bv("set", "title=z")
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.state()["title"], "z")
        with mock.patch.object(bv.os, "replace", side_effect=PermissionError("held")), mock.patch.object(bv.time, "sleep"):
            self.assertIn("held by another", self.fails("set", "title=y"))
        self.assertEqual(self.state()["title"], "z")

    def test_guard_fails_closed_on_a_broken_state(self):
        self.init()
        (self.root / ".software-architector" / "state.json").write_text("{broken", "utf-8")

        def hook(**data):
            return subprocess.run([sys.executable, str(Path(bv.__file__).resolve()), "hook", "pre-tool"], cwd=self.root,
                                  input=json.dumps({"cwd": str(self.root), **data}).encode("utf-8"), capture_output=True)
        r = hook(tool_name="Bash", tool_input={"command": "git push"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("damaged", r.stderr.decode("utf-8"))
        self.assertEqual(hook(tool_name="Write", tool_input={"file_path": "src/a.py", "content": "x"}).returncode, 2)
        self.assertEqual(hook(tool_name="Read", tool_input={"file_path": "src/a.py"}).returncode, 0)
        self.assertEqual(hook(tool_name="Bash", tool_input={"command": "bv init --force --slug x"}).returncode, 0)

    def test_a_broken_state_lets_through_only_the_bv_launcher(self):
        """L6: any command with the word `bv` in it used to pass while the guard was down."""
        launcher = (bv.PLUGIN_ROOT / "bin" / "bv").as_posix()
        broken = {"broken": "broken"}
        for cmd in ("bv status", f'"{launcher}" init --force --slug x', "bv doctor"):
            self.assertIsNone(bv.guard.check_broken({"tool_name": "Bash", "tool_input": {"command": cmd}}, broken), cmd)
        for cmd in ("bv status; rm -rf src", "echo bv > src/a.py", "bv status && git push", "./bv init", "rm -rf bv",
                    "bv status | tee src/x", "/tmp/evil/bin/bv status", "cat $(bv url)"):
            self.assertIsNotNone(bv.guard.check_broken({"tool_name": "Bash", "tool_input": {"command": cmd}}, broken), cmd)

    @unittest.skipUnless(os.name == "nt", "where PATH may hold WSL's bash instead of Git Bash")
    def test_checks_run_in_git_bash_on_windows(self):
        self.init()
        argv = bv.shell_argv(self.root, "exit 0")
        self.assertTrue(argv[0].lower().endswith("bin\\bash.exe"), argv)
        self.assertNotIn("system32", argv[0].lower())


if __name__ == "__main__":
    unittest.main()
