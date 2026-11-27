#!/usr/bin/env python3
"""Software-Architector core: run state, manifest, tickets, gates, commits, dashboard server.

State lives in <project>/.software-architector/state.json and only this script writes it; the agents' journal
(.software-architector/live/) is written by hooks, see events.py.
One command = one event. Output: one summary line plus `!` warnings; errors exit 1.
"""
import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402
import events  # noqa: E402
import guard  # noqa: E402

guard.utf8_stdio()

STAGES = [("preflight", "Preflight"), ("manifest", "Requirements"), ("briefing", "Briefing"),
          ("spec", "Specification"), ("plan", "Plan"), ("build", "Build"),
          ("review", "Code review"), ("final", "Acceptance")]
STAGE_IDS = [s for s, _ in STAGES]
MODES = ("full", "semi", "interview")
DEPTHS = ("strict", "normal", "deep")
REQ_STATUSES = ("open", "in-spec", "in-ticket", "done", "placeholder", "deferred", "dropped")
ROW_ID = re.compile(r"^[RGASD]\d{2,}(?:i|\.\d+)?$")
REQ_REF = re.compile(r"\b[RGASD]\d{2,}(?:i|\.\d+)?\b")
REQ_REPORT = re.compile(r"^\s*([RGASD]\d{2,}(?:i|\.\d+)?)\s+(done|placeholder)\b", re.I)
UNDELIVERED = ("open", "in-spec", "in-ticket")
LISTS = ("assumptions", "placeholders", "emptyEnv", "concerns", "additions", "report")
SETTABLE = ("title", "mode", "depth", "tier", "setupCmd", "checkCmd", "e2eCmd", "testsPattern", "checkTimeout",
            "guard", "resume")
# what decides «green»: once the build starts, a change is a recorded decision, never a quiet `checkCmd=exit 0`
FROZEN = ("setupCmd", "checkCmd", "e2eCmd", "testsPattern")
GATES = ("G1", "G2", "G3", "G4", "G5")
# git's empty tree: the base of a stage begun before any commit. shortcut: SHA-1 repos only, a SHA-256 repo lists "—"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SKIPPABLE_STAGES = ("manifest", "briefing")  # every later stage is what makes the result trustworthy
CHECK_TIMEOUT = 540  # seconds per command: a foreground Bash tool call is cut at 600
TEST_COUNTS = [re.compile(p, re.M) for p in (
    r"^Ran (\d+) tests? in ",  # unittest
    r"\b(\d+) passed(?:, [^\n]*?)? in [\d.]+ ?s",  # pytest
    r"^\s*Tests:\s+(?:\d+ \w+, )*(\d+) passed",  # jest
    r"^\s*Tests\s+(?:\d+ \w+ \| )*(\d+) passed",  # vitest
    r"\b(\d+) passing\b",  # mocha
    r"^test result: ok\. (\d+) passed",  # cargo
)]
# the environment of setupCmd/checkCmd/e2eCmd: an allowlist, so a session's API keys never reach a project command
ENV_KEEP = {"PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "USER",
            "USERNAME", "LOGNAME", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "JAVA_HOME",
            "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME", "VIRTUAL_ENV", "NVM_DIR"}
NO_TESTS = re.compile(r"\bno tests? (?:found|ran)\b", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SCHEMA = 3
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOK_EVENTS = ("pre-tool", "post-tool", "tool-failed", "agent-start", "agent-stop", "session-start", "prompt", "stop")
CONTRACT_KEYS = ("STATUS", "FILES", "TESTS", "REDPROOF", "INTERFACES", "REQUIREMENTS", "CONCERNS", "BLOCKERS")
CONTRACT_STATUSES = ("DONE", "DONE_WITH_CONCERNS", "BLOCKED", "NEEDS_CONTEXT")
MAX_PARALLEL, MAX_REPAIRS, MAX_RETRIES = 3, 2, 2
IN_FLIGHT = ("in-progress", "repair")
PLAN_PATHS = [".software-architector", ".gitignore", "AGENTS.md", "CLAUDE.md", ".env.example"]
FINAL_PATHS = [".software-architector", ".gitignore", "AGENTS.md", "CLAUDE.md", ".env.example",
               "docs/architecture.md", "docs/adr", "CONTEXT.md", "GLOSSARY.md"]
# leftovers of a check: `bv commit` runs it and then stages the whole zone
GITIGNORE_LINES = [".env", ".env.*", "!.env.example", "node_modules/", "__pycache__/", ".venv/", ".DS_Store"]
FINAL_SUBJECT = "Software-Architector: build delivered"
NOT_ACCEPTED_SUBJECT = "Software-Architector: build closed without acceptance"


class Fail(Exception):
    pass


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def find_root(start=None):
    p = Path(start or os.getcwd()).resolve()
    for d in (p, *p.parents):
        if (d / ".software-architector" / "state.json").exists():
            return d
    return p


def init_root():
    """A git repository nested inside a project with a run gets its own run: `init --force` must never archive the parent's."""
    found = find_root()
    top = git(Path.cwd(), "rev-parse", "--show-toplevel", check=False)
    if top:
        top = Path(top).resolve()
        if top != found and found in top.parents:
            return top
    return found


def state_path(root):
    return root / ".software-architector" / "state.json"


def load(root):
    p = state_path(root)
    if not p.exists():
        raise Fail("no run started: .software-architector/state.json is missing — run `bv init` first")
    try:
        st = guard.read_state(p)
    except json.JSONDecodeError as e:
        raise Fail(f"broken .software-architector/state.json: line {e.lineno}: {e.msg}")
    except ValueError as e:
        raise Fail(f"broken .software-architector/state.json: {e}")
    if st.get("schemaVersion") != SCHEMA:
        raise Fail(f"state.json: schema {st.get('schemaVersion')}, plugin expects {SCHEMA} — update the plugin or finish the run with the old version")
    return st


def try_load(root):
    try:
        return load(root)
    except Fail:
        return None


def save(root, st):
    st["updatedAt"] = now()
    st["rev"] = st.get("rev", 0) + 1
    st["log"] = st["log"][-200:]
    p = state_path(root)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), "utf-8")
    # On Windows a reader (dashboard, a hook) blocks the replace for a moment; a crash here would lose a commit's bookkeeping.
    for attempt in range(100):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.05)
    raise Fail("state.json is held by another process for over 5 seconds: write failed, repeat the command")


def lock_path(root, name="state"):
    # normcase: on Windows `C:\Proj` and `c:\proj` are one project and must share one lock
    key = hashlib.md5(os.path.normcase(str(root)).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"software-architector-{key}{'' if name == 'state' else '-' + name}.lock"


def pid_alive(pid):
    """Whether a process with this pid runs now; True when unsure, so a live owner's lock is never taken away."""
    if pid == os.getpid():
        return True
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return not ok or code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def lock_is_stale(lock, content, stale):
    """The owner is dead, or the lock is far older than any honest holder (a reused pid on Windows looks alive)."""
    age = time.time() - lock.stat().st_mtime
    pid = int(content.split()[0]) if content.split() and content.split()[0].isdigit() else None
    if pid is None:
        return age > max(stale, 5)  # empty: the owner is still writing its pid, or crashed before it did
    return not pid_alive(pid) or age > stale * 5


def break_lock(lock, content):
    """Remove a stale lock without a race: under a breaker lock, and only if it still holds the same owner."""
    brk = lock.with_name(lock.name + ".break")
    try:
        os.close(os.open(brk, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        with contextlib.suppress(OSError):
            if time.time() - brk.stat().st_mtime > 30:  # a breaker that died in these few lines
                brk.unlink()
        return
    try:
        if lock.read_text("utf-8") == content:
            lock.unlink()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            brk.unlink()


@contextlib.contextmanager
def locked(root, wait=15, stale=60, name="state", busy="state.json is held by another bv call — repeat the command"):
    """One state-changing `bv` at a time: parallel executors call bv commit/contract too. The lock lives in the temp dir so `git add .software-architector` never picks it up."""
    lock = lock_path(root, name)
    mine = f"{os.getpid()} {time.time()} {os.urandom(4).hex()}"
    end = time.time() + wait
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(mine)
            break
        except FileExistsError:
            try:
                content = lock.read_text("utf-8")
                if lock_is_stale(lock, content, stale):  # a crashed bv left it
                    break_lock(lock, content)
                    continue
            except OSError:
                pass  # released or replaced meanwhile: wait and try again
            if time.time() > end:
                raise Fail(busy)
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if lock.read_text("utf-8") == mine:  # never remove a lock that is no longer ours
                lock.unlink()
        except OSError:
            pass


def log(st, kind, **kw):
    st["log"].append({"t": now(), "k": kind, **kw})


def run_dir(root, st):
    return root / ".software-architector" / st["dir"]


def run_name(st):
    return Path(st["dir"]).name.removesuffix("--wip")


def git(root, *args, check=True):
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        raise Fail(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


# ---------- manifest ----------

def split_row(line):
    s = line.strip()
    if not s.startswith("|"):
        return None
    parts = re.split(r"(?<!\\)\|", s)[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        parts = parts[:-1]
    return [c.strip() for c in parts]


def clean_id(cell):
    return cell.strip("`* ")


def base_id(rid):
    return re.sub(r"(i|\.\d+)$", "", rid)


def manifest_path(root, st):
    return run_dir(root, st) / "manifest.md"


def read_manifest(path):
    if not path.exists():
        return []
    rows, seen = [], set()
    for n, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        cells = split_row(line)
        if not cells or not ROW_ID.match(clean_id(cells[0])):
            continue
        rid = clean_id(cells[0])
        if len(cells) < 5:
            raise Fail(f"manifest.md:{n}: {rid}: need 5 columns (ID | From the brief | Status | Basis | Where)")
        status = (clean_id(cells[2]).split() or [""])[0].lower()
        if status not in REQ_STATUSES:
            raise Fail(f"manifest.md:{n}: {rid}: unknown status '{cells[2]}', allowed: {', '.join(REQ_STATUSES)}")
        if status == "dropped" and not re.search(r"[«\"“„]", cells[3]):
            raise Fail(f"manifest.md:{n}: {rid}: dropped only with a user quote in 'Basis'")
        if status == "deferred" and not cells[3]:
            raise Fail(f"manifest.md:{n}: {rid}: deferred only with a reason in 'Basis', nothing is deferred silently")
        if rid in seen:
            raise Fail(f"manifest.md:{n}: {rid} is repeated")
        seen.add(rid)
        rows.append({"id": rid, "text": cells[1], "status": status, "why": cells[3], "where": cells[4], "line": n})
    return rows


def update_manifest(path, changes):
    """changes: {id: (status, where_append)} — rewrites only the Status and Where cells."""
    lines = path.read_text("utf-8").splitlines()
    for i, line in enumerate(lines):
        cells = split_row(line)
        if not cells or len(cells) < 5 or clean_id(cells[0]) not in changes:
            continue
        status, add = changes[clean_id(cells[0])]
        cells[2] = status
        if add and add not in cells[4]:
            cells[4] = f"{cells[4]} → {add}" if cells[4] else add
        lines[i] = "| " + " | ".join(cells) + " |"
    path.write_text("\n".join(lines) + "\n", "utf-8")


def live(rows):
    return [r for r in rows if not r["id"].startswith("D")]


def scenarios(rows):
    return [r for r in rows if r["id"].startswith("S") and r["status"] not in ("dropped", "deferred")]


def remember_ids(st, rows):
    st["requirementIds"] = sorted(set(st.get("requirementIds", [])) | {r["id"] for r in rows})


def vanished(st, rows):
    return [f"{i}: row vanished from the manifest; only the user can drop a requirement (dropped with a quote)"
            for i in sorted(set(st.get("requirementIds", [])) - {r["id"] for r in rows})]


def recount(root, st):
    rows = live(read_manifest(manifest_path(root, st)))
    counts = {s: 0 for s in REQ_STATUSES}
    for r in rows:
        counts[r["status"]] += 1
    st["requirements"] = {"total": len(rows), **counts}


def gate_g1(rows, mode):
    bad = [r["id"] for r in live(rows) if r["status"] == "open" and (mode == "full" or not r["why"])]
    if not live(rows):
        return False, "manifest is empty"
    if bad:
        why = "full mode leaves no open row" if mode == "full" else "open without a recorded reason"
        return False, f"{why}: {', '.join(bad)}"
    if not scenarios(rows):
        return False, "no end-to-end scenario: at least one S## row is needed (the main user path from start to end)"
    return True, f"{len(live(rows))} rows"


# ---------- tickets ----------

FIELD = re.compile(r"^\*\*([^*:]+):?\*\*:?\s*(.*)$")
TICKET_KEYS = {"требования": "requirements", "requirements": "requirements",
               "зависит от": "blockedBy", "blocked by": "blockedBy", "depends on": "blockedBy",
               "зона": "zone", "zone": "zone", "волна": "wave", "wave": "wave",
               "ревью": "review", "review": "review", "модель": "model", "model": "model"}


def norm_tid(x):
    return x.zfill(2) if x.isdigit() else x.upper()


def parse_ticket(path):
    raw = re.split(r"[-_.]", path.name, maxsplit=1)[0]
    if not re.fullmatch(r"\d+|[Ff]\d+", raw):
        raise Fail(f"{path.name}: ticket name must start with a number (03-…) or F1-…")
    t = {"id": norm_tid(raw), "title": "", "requirements": [], "blockedBy": [], "zone": [],
         "wave": None, "review": "", "model": "", "file": path.name}
    for line in path.read_text("utf-8").splitlines():
        s = line.strip()
        if s.startswith("# ") and not t["title"]:
            t["title"] = re.sub(r"^[0-9A-Za-z]+\s*[—–-]\s*", "", s[2:].strip())
        m = FIELD.match(s)
        if not m:
            continue
        key, val = TICKET_KEYS.get(m.group(1).strip().lower()), m.group(2).strip()
        if key == "requirements":
            t[key] = REQ_REF.findall(val)
        elif key == "blockedBy":
            t[key] = [norm_tid(x) for x in re.findall(r"\b(?:[Ff]\d+|\d+)\b", val)]
        elif key == "zone":
            t[key] = [z.strip() for z in re.findall(r"`([^`]+)`", val) if z.strip()]
        elif key == "wave":
            w = re.match(r"\d+", val)
            t[key] = int(w.group()) if w else None
        elif key:
            t[key] = val
    return t


def norm_path(z):
    z = z.replace("\\", "/")
    while z.startswith("./"):
        z = z[2:]
    z = z.rstrip("/") + "/"
    return z.lower() if os.name == "nt" else z  # `src/App/` and `src/app/` are one folder on Windows


def zone_problem(z):
    """A zone is what `bv commit` stages: a plain relative path inside the project, never the whole tree or the records."""
    p = z.replace("\\", "/").strip()
    if re.match(r"(?:/|[A-Za-z]:|~)", p):
        return "absolute path"
    if re.search(r"[*?\[\]{}!]", p) or p.startswith(":"):
        return "glob or git magic — only an explicit path"
    parts = p.rstrip("/").split("/")
    while parts and parts[0] == ".":
        parts = parts[1:]
    if not parts or any(x in ("", ".", "..") for x in parts):
        return "`.`, `..` or an empty path part: a zone is a folder or file inside the project"
    if parts[0].lower() in (".software-architector", ".git"):
        return f"{parts[0]} is not a ticket zone"
    return None


def zones_overlap(za, zb):
    return any(x.startswith(y) or y.startswith(x) for x in map(norm_path, za) for y in map(norm_path, zb))


def covers(ticket, rid):
    return rid in ticket["requirements"] or base_id(rid) in ticket["requirements"]


def check_plan(rows, tickets):
    bad, ids, by = [], {r["id"] for r in rows}, {t["id"]: t for t in tickets}
    files = {}
    for t in tickets:  # `01-a.md` and `1-b.md` are both ticket 01: one would silently replace the other
        files.setdefault(t["id"], []).append(t.get("file", "?"))
    bad += [f"ticket {tid} is repeated: {', '.join(names)} — one number, different files"
            for tid, names in files.items() if len(names) > 1]
    for r in live(rows):
        if r["status"] == "open":
            bad.append(f"{r['id']}: open, but the plan is already being written: decide the row (in-spec or deferred with a reason)")
        elif r["status"] in ("in-spec", "in-ticket", "placeholder") and not any(covers(t, r["id"]) for t in tickets):
            bad.append(f"{r['id']}: live requirement is in no ticket")
    for t in tickets:
        n = f"ticket {t['id']}"
        if not t["requirements"]:
            bad.append(f"{n}: no requirements")
        bad += [f"{n}: {r} is not in the manifest" for r in t["requirements"] if r not in ids]
        if not t["zone"]:
            bad.append(f"{n}: no zone")
        bad += [f"{n}: zone `{z}`: {zone_problem(z)}" for z in t["zone"] if zone_problem(z)]
        if not t["wave"]:
            bad.append(f"{n}: no wave")
        for d in t["blockedBy"]:
            if d not in by:
                bad.append(f"{n}: depends on missing {d}")
            elif t["wave"] and by[d]["wave"] and t["wave"] <= by[d]["wave"]:
                bad.append(f"{n}: wave {t['wave']} must be later than wave {by[d]['wave']} of ticket {d}")
    for i, a in enumerate(tickets):
        for b in tickets[i + 1:]:
            if a["wave"] and a["wave"] == b["wave"] and zones_overlap(a["zone"], b["zone"]):
                bad.append(f"tickets {a['id']} and {b['id']}: same wave, overlapping zones")
    return bad


def find_ticket(st, tid):
    tid = norm_tid(tid)
    for t in st["tickets"]:
        if t["id"] == tid:
            return t
    raise Fail(f"no ticket {tid} — run `bv tickets` first")


def landed(st):
    """Tickets others may build on: done, or committed and under review — except a foundation, which must pass it first."""
    return {t["id"] for t in st["tickets"] if t["status"] == "done"
            or t["status"] == "review" and not re.search(r"фундамент|foundation", t.get("review", ""), re.I)}


def ready_tickets(st):
    done = landed(st)
    flying = [t for t in st["tickets"] if t["status"] in IN_FLIGHT]
    out = []
    for t in st["tickets"]:
        if t["status"] == "pending" and all(d in done for d in t["blockedBy"]) \
                and not any(zones_overlap(t["zone"], f["zone"]) for f in flying + out):
            out.append(t)
    return out[:max(0, MAX_PARALLEL - len(flying))]


def close_requirements(root, st):
    path = manifest_path(root, st)
    rows = read_manifest(path)
    placeholders = {p for t in st["tickets"] for p in t.get("placeholders", [])}
    changes = {}
    for r in live(rows):
        ph = r["id"] in placeholders or base_id(r["id"]) in placeholders
        # a later ticket may find that a row already done cannot be proven for real: the weaker word wins
        if r["status"] not in UNDELIVERED and not (r["status"] == "done" and ph):
            continue
        holders = [t for t in st["tickets"] if covers(t, r["id"])]
        if holders and all(t["status"] == "done" for t in holders):
            changes[r["id"]] = ("placeholder" if ph else "done", holders[-1].get("commit", ""))
    if changes:
        update_manifest(path, changes)
    return sorted(changes)


# ---------- commits ----------

def staged_secret(root, paths):
    diff = git(root, "diff", "--cached", "-U0", "--", *paths)
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            hit = guard.find_secret(line)
            if hit:
                return hit
    return None


def commit_paths(root, paths, subject, trailers=()):
    paths = [p for p in dict.fromkeys(paths) if (root / p).exists() or git(root, "ls-files", "--", p, check=False)]
    if not paths:
        raise Fail("nothing to commit: no existing path")
    git(root, "add", "-A", "--", *paths)
    if not git(root, "diff", "--cached", "--name-only", "--", *paths):
        raise Fail(f"no changes in paths: {', '.join(paths)}")
    hit = staged_secret(root, paths)
    if hit:
        git(root, "reset", "-q", "--", *paths)  # unstage only; the working tree is untouched
        raise Fail(f"the diff looks like it holds a secret ({hit}) — replace the value with a variable name, commit not made")
    args = ["commit", "-q", "-m", subject]
    for tr in trailers:
        args += ["--trailer", tr]
    git(root, *args, "--", *paths)
    return git(root, "rev-parse", "--short", "HEAD")


def attempt_commit(root, key):
    """The commit an earlier `bv commit` of the same attempt made, found by its trailer; None if there is none."""
    out = git(root, "log", "-n", "200", "--format=%h%x09%(trailers:key=Software-Architector-Commit,valueonly,separator=%x2C)",
              check=False)
    for line in out.splitlines():
        h, _, value = line.partition("\t")
        if value.strip() == key:
            return h
    return None


def memory_paths(st, paths):
    if st.get("memoryOwner") == "user":
        return [p for p in paths if p not in ("AGENTS.md", "CLAUDE.md")]
    return paths


# ---------- proof: what really ran, not what anyone says ran ----------

def shell_argv(root, cmd):
    """The shell of the agents' Bash tool, so a command means here what it meant when they wrote it."""
    if os.name == "nt":
        bash = Path(git(root, "--exec-path")).parents[2] / "bin" / "bash.exe"  # not PATH: there it may be WSL's
    else:
        bash = shutil.which("bash")
    return [str(bash), "-c", cmd] if bash and Path(bash).exists() else None


def kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


class TreeJob:
    """Windows: a job object holding the check and every process it starts. Closing it kills them all, survivors
    included, and Windows closes it itself when bv dies (the Bash tool's cut), so no orphan stays.
    shortcut: a child started before the assignment (microseconds after the start) escapes; taskkill /T backs it up."""

    def __init__(self, p):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.k32.TerminateJobObject.argtypes = [wintypes.HANDLE, ctypes.c_uint]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.h = self.k32.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not (self.h and self.k32.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info))
                and self.k32.AssignProcessToJobObject(self.h, int(p._handle))):
            self.close()
            raise OSError("job object")

    def close(self):
        if self.h:
            self.k32.TerminateJobObject(self.h, 1)
            self.k32.CloseHandle(self.h)
            self.h = None


def tree_job(p):
    if os.name != "nt":
        return None  # POSIX: the check runs in its own process group, kill_tree takes the whole group
    try:
        return TreeJob(p)
    except (OSError, AttributeError, ValueError):
        return None  # no job (an old or restricted Windows): taskkill /T is all there is


@contextlib.contextmanager
def killed_with_bv(p):
    """bv stopped from outside (the Bash tool's 10-minute cut sends SIGTERM) takes the check's process group with it,
    so no orphan keeps running. shortcut: SIGKILL cannot be caught; on Windows the job object of L5 does this."""
    old = {}

    def stop(signum, frame):
        kill_tree(p)
        raise SystemExit(128 + signum)
    if os.name != "nt":
        with contextlib.suppress(ValueError):  # not the main thread: handlers cannot be set there
            for s in (signal.SIGTERM, signal.SIGHUP):
                old[s] = signal.signal(s, stop)
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def measured_tests(text, pattern=None):
    """Passed tests from the summaries of common runners (and `testsPattern`, group 1); None when nothing is recognized."""
    text = ANSI.sub("", text)
    rxs = TEST_COUNTS + ([re.compile(pattern, re.M)] if pattern else [])
    found = [int(n if isinstance(n, str) else n[0]) for rx in rxs for n in rx.findall(text)]
    if found:
        return sum(found)
    return 0 if NO_TESTS.search(text) else None


def green(r):
    """Exit 0 and a counted, non-empty test run; an install step has no tests to count."""
    return r["exit"] == 0 and (r["step"] == "setup" or r.get("tests") not in (0, None))


def check_env(environ):
    """What a project command gets: the system basics and toolchain homes, never the session's tokens and keys."""
    keep = {k: v for k, v in environ.items() if k.upper() in ENV_KEEP}
    return {**keep, "CI": "1", "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}


def run_cmd(root, st, cwd, name, cmd):
    """Runs a project command for real: its exit code decides. exit None = killed on timeout. Returns (record, output)."""
    events.ensure_live(root)
    d = events.live_dir(root, st) / "checks"
    d.mkdir(parents=True, exist_ok=True)
    log_file = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.txt"
    timeout, argv, start = int(st.get("checkTimeout") or CHECK_TIMEOUT), shell_argv(root, cmd), time.time()
    if argv is None:  # cmd.exe would read bash syntax differently: never run it there silently
        raise Fail("bash not found (on Windows: Git Bash from the git install): check commands are written for bash. "
                   "Install Git for Windows (or bash) and repeat")
    kw = {} if os.name == "nt" else {"start_new_session": True}
    # A file, not a pipe: a process that outlives the command (a dev server) would keep a pipe open forever.
    # CI=1 keeps test runners out of watch mode. PYTHONIOENCODING: on Windows a Python child writes a file in the ANSI
    # code page, and the log is read as UTF-8.
    with open(log_file, "wb") as out:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, env=check_env(os.environ), **kw)
        job = tree_job(p)
        with killed_with_bv(p):
            try:
                code = p.wait(timeout)
            except subprocess.TimeoutExpired:
                kill_tree(p)
                p.wait()
                code = None
            finally:  # a survivor (a dev server the e2e run left) dies with the command, not with the session
                kill_tree(p)
                if job:
                    job.close()
    text = guard.mask_secrets(log_file.read_text("utf-8", errors="replace"))
    try:
        log_file.write_text(text, "utf-8")
    except OSError:  # a survivor still holds the file (Windows): the masked copy is the log; the guard refuses the raw one
        log_file = log_file.with_suffix(".masked.txt")
        log_file.write_text(text, "utf-8")
    return ({"step": name, "cmd": cmd, "exit": code, "at": now(), "ms": int((time.time() - start) * 1000),
             "tests": measured_tests(text, st.get("testsPattern")), "log": log_file.relative_to(root).as_posix()}, text)


def red_reason(r, timeout):
    if r["exit"] is None:
        return f"did not finish in {timeout} s and was stopped"
    if r["exit"]:
        return f"exit code {r['exit']}"
    if r.get("tests") is None:
        return ("test count not recognized in the output, and an uncounted run is not green: "
                "`bv set testsPattern=\"<regex, group 1 — number of passed tests>\"`")
    return "0 tests: an empty set counts as red"


SRC_PATH = re.compile(r"[\w./\\:-]*[\w-]\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|rb|java|kt|cs|php|vue|svelte|c|cc|cpp|h|hpp|"
                      r"swift|dart|scala|ex|exs)\b")


def failing_files(root, text):
    """Project source files a check's output names (tracebacks, `file:line:` errors); libraries outside it are ignored."""
    out = set()
    for m in SRC_PATH.finditer(text):
        p = re.sub(r"^/([A-Za-z])/", r"\1:/", m.group().replace("\\", "/"))
        rel = guard.rel_to(root, Path(p) if Path(p).is_absolute() else root / p)
        if rel and not set(rel.split("/")) & guard.ARTIFACT_DIRS and (root / rel).is_file():
            out.add(rel)
    return sorted(out)


def tail(text, n=30):
    return "\n".join(text.splitlines()[-n:])


def project_check(root, st, a):
    """The check behind a ticket's commit: run here, so a claimed «34/0» cannot stand in for it."""
    tid = norm_tid(a.target)
    r, text = run_cmd(root, st, root, f"commit-{tid}", st["checkCmd"])
    if not green(r):
        others = [t for t in st["tickets"] if t["status"] in IN_FLIGHT and t["id"] != tid]
        files = failing_files(root, text)
        zone = find_ticket(st, tid)["zone"]
        if r["exit"] and files and not any(guard.in_zone(f, zone) for f in files):
            owners = [t["id"] for t in others if any(guard.in_zone(f, t["zone"]) for f in files)]
            raise Fail(f"blocked-by-other-ticket: red only in files outside the zone of ticket {tid} ({', '.join(files[:5])})"
                       + (f", this is the zone of in-flight ticket {', '.join(owners)}" if owners else "")
                       + f". Repair of ticket {tid} is not needed: wait for their return and repeat `bv commit {tid} --tests …`. "
                         f"Log: {r['log']}\n{tail(text)}")
        hint = (f" In flight {', '.join(t['id'] for t in others)}: if the red is in their zone, wait for their return and repeat."
                if others else "")
        raise Fail(f"project check is red ({red_reason(r, st.get('checkTimeout') or CHECK_TIMEOUT)}): commit not made."
                   f"{hint} Fix through `bv ticket {tid} repair`. Log: {r['log']}\n{tail(text)}")
    was = st.get("testsMeasured") or 0
    if r["tests"] is not None and r["tests"] < was and not a.fewer_tests:
        raise Fail(f"test count dropped ({was} → {r['tests']}). Deleting tests to get green is not allowed; "
                   f"if the reason is real: `--fewer-tests \"<reason>\"`, it goes into the report")
    return r


def accepted_findings(st):
    """Audit findings lifted as false positives, from a ticket's audit or the branch one: ids are stable across runs."""
    return {**(st.get("auditAccepted") or {}), **((st.get("audit") or {}).get("accepted") or {})}


def ticket_audit(root, st, t):
    """The code audit behind a ticket's commit, like the check: the mechanical scan of the files this ticket changed.
    Returns (record, blocking findings)."""
    z = t["zone"]
    changed = sorted({f for f in git(root, "diff", "--name-only", "-z", "HEAD", "--", *z, check=False).split("\0")
                      + git(root, "ls-files", "-z", "--others", "--exclude-standard", "--", *z, check=False).split("\0")
                      if f and guard.in_zone(f, z)})
    files = [f for f in git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", check=False).split("\0") if f]
    commands = [st[k] for k in ("setupCmd", "checkCmd", "e2eCmd") if st.get(k)]
    # only what this ticket wrote counts: old code in the zone and other zones are the branch audit's business
    found = [f for f in audit.scan(root, files, changed, commands) if f["file"] in changed]
    accepted = accepted_findings(st)
    rec = {"at": now(), "files": len(changed),
           "findings": [{k: f[k] for k in ("id", "sev", "kind", "rule", "file", "line")} for f in found]}
    return rec, [f for f in found if f["sev"] in audit.BLOCKING and f["id"] not in accepted]


def audit_counts(au):
    return ", ".join(f"{s} {sum(f['sev'] == s for f in au['findings'])}" for s in audit.SEVERITIES)


def audit_refusal(tid, blocking):
    return (f"code audit of ticket {tid} found {len(blocking)} blocking finding(s) in the files it changed: commit not made. "
            f"Fix through `bv ticket {tid} repair`; a false positive only after a reviewer confirmed it: "
            f"`bv audit --accept <id> --note \"<why>\"`, it goes into the report\n"
            + "\n".join(f"! {f['id']} {f['sev']} {f['rule']} {f['file']}" + (f":{f['line']}" if f["line"] else "")
                        + f" — {f['msg']}" for f in blocking))


def last_commit_at(st):
    """When the last ticket commit landed: every check of the current stage must come after it."""
    times = [t["committedAt"] for t in st["tickets"] if t.get("committedAt")]
    return max(times, key=dt.datetime.fromisoformat) if times else None


def agent_stop(root, st, kind, since, ticket=None):
    """The hooks' journal, not the orchestrator's word: the latest stop of an agent of this kind after `since`; with
    `ticket`, only an agent whose Agent call named that ticket («Review of ticket 02»). None when there is none."""
    agents = events.read_agents(root, st)
    spawned = {e["spawned"]: e.get("s", "") for e in agents.get("main", []) if e.get("spawned")}
    start = dt.datetime.fromisoformat(since) if since else None
    best = None
    for name, rows in agents.items():
        if not (any(e.get("type") == kind for e in rows) or kind in spawned.get(name, "")):
            continue
        if ticket and guard.ticket_ref(spawned.get(name, "")) != ticket:
            continue
        for e in rows:
            t = dt.datetime.fromisoformat(e["t"]) if e.get("k") == "agent-stop" else None
            if t and (not start or t >= start) and (best is None or t >= best[0]):
                best = (t, {**e, "agent": name})
    return best and best[1]


def agent_finished_since(root, st, kind, since, ticket=None):
    return agent_stop(root, st, kind, since, ticket) is not None


def remove_clone(tmp):
    """A temp dir this plugin made: git objects and some installs are read-only, so make writable and retry."""
    def retry(func, path, _):
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IWRITE)
            func(path)
    shutil.rmtree(tmp, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))
    if Path(tmp).exists():
        print(f"! temporary clone not removed (busy?): {Path(tmp).as_posix()}")


def snapshot(root, dest):
    """HEAD's files without the run's records and without git history: `git log -p` in the blind clone leaks nothing."""
    data = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=root, capture_output=True)
    if data.returncode:
        raise Fail(f"git archive HEAD: {data.stderr.decode('utf-8', 'replace').strip()}")
    with tarfile.open(fileobj=io.BytesIO(data.stdout)) as tar:
        safe = [m for m in tar.getmembers() if (m.isfile() or m.isdir()) and not m.name.startswith("/")
                and ".." not in m.name.split("/") and m.name.split("/")[0].lower() != ".software-architector"]
        tar.extractall(dest, members=safe, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def drop_blind_clone(st):
    """The previous blind clone, a temp dir this plugin made: removed before a new one and at the landing."""
    old = st.get("blindClone")
    if old and Path(old).name == "project" and Path(old).parent.name.startswith("sa-blind-"):
        remove_clone(Path(old).parent)
    st["blindClone"] = None


def clone_env(root, clone, real):
    """The clone's .env: a stranger has only the committed .env.example (plus CI=1); the real one only when asked for.
    The real file is copied unread. Returns what the clone got: "real", "example" or None."""
    if real and (root / ".env").exists():
        shutil.copyfile(root / ".env", clone / ".env")
        return "real"
    example = clone / ".env.example"
    if example.exists():
        text = example.read_text("utf-8", errors="replace")
        (clone / ".env").write_text(text + ("" if not text or text.endswith("\n") else "\n") + "CI=1\n", "utf-8")
        return "example"
    return None


def note_real_env(st, where):
    st["lists"]["report"].append(f"{where} ran with the user's real .env (--use-real-env), not .env.example")


def cmd_blind_clone(root, st, a):
    g5, head = st["gates"].get("G5") or {}, git(root, "rev-parse", "HEAD")
    if not g5.get("ok") or g5.get("head") != head:
        raise Fail("G5 not passed on the current commit: run `bv verify` first, then the clone for blind acceptance")
    drop_blind_clone(st)
    clone = Path(tempfile.mkdtemp(prefix="sa-blind-")) / "project"
    snapshot(root, clone)
    env = clone_env(root, clone, a.use_real_env)
    if env == "real":
        note_real_env(st, "Blind acceptance")
    st["blindClone"] = str(clone)
    log(st, "blind-clone", head=head, env=env)
    print(f"Clone for blind acceptance (without history and run records): {clone.as_posix()}")


def cmd_verify(root, a):
    """G5: a stranger's checkout. Clone HEAD, drop the run's records, install and run everything; exit codes decide."""
    st = load(root)
    if not st.get("checkCmd"):
        raise Fail("no check command: `bv set checkCmd=…`")
    if scenarios(read_manifest(manifest_path(root, st))) and not st.get("e2eCmd"):
        raise Fail("the manifest has S## scenarios but no end-to-end test command: `bv set e2eCmd=…`")
    head = git(root, "rev-parse", "HEAD")
    tmp = Path(tempfile.mkdtemp(prefix="sa-verify-"))
    clone, steps, red_text, env = tmp / "project", [], "", None
    try:
        git(root, "clone", "-q", str(root), str(clone))
        shutil.rmtree(clone / ".software-architector", ignore_errors=True)
        env = clone_env(root, clone, a.use_real_env)
        for name, key in (("setup", "setupCmd"), ("check", "checkCmd"), ("e2e", "e2eCmd")):
            if st.get(key):
                r, text = run_cmd(root, st, clone, name, st[key])
                steps.append(r)
                if not green(r):
                    red_text = text
                    break
    finally:
        remove_clone(tmp)
    ok = all(green(s) for s in steps)
    with locked(root):
        fresh = load(root)
        fresh["gates"]["G5"] = {"ok": ok, "at": now(), "head": head, "steps": steps, "env": env}
        if env == "real":
            note_real_env(fresh, "Clean clone check (G5)")
        log(fresh, "gate", gate="G5", ok=ok)
        save(root, fresh)
    timing = " · ".join(f"{s['step']} {s['ms'] // 1000} s" for s in steps)
    dirty = git(root, "status", "--porcelain", "--", ".", ":(exclude).software-architector", check=False)
    note = f"\n! not committed, did not reach the clean clone:\n{dirty}" if dirty else ""
    if not ok:
        r = steps[-1]
        raise Fail(f"G5 ✗ clean clone {head[:7]}: `{r['cmd']}` — {red_reason(r, st.get('checkTimeout') or CHECK_TIMEOUT)}. "
                   f"Log: {r['log']}{note}\n{tail(red_text)}")
    print(f"G5 ✓ clean clone {head[:7]}: {timing}{note}")


def cmd_audit(root):
    """The mechanical pass before the branch review: junk code and dangerous calls in the run's files, secrets and
    vulnerable dependencies in the whole tree. audit.md is bv-reviewer's input; blocker/high findings stop `bv finish`."""
    st = load(root)
    head, base = git(root, "rev-parse", "HEAD"), st.get("baseCommit")
    files = [f for f in git(root, "ls-files", "-z").split("\0") if f]
    changed = set(git(root, "diff", "--name-only", "-z", base, "HEAD").split("\0")) if base else set(files)
    commands = [st[k] for k in ("setupCmd", "checkCmd", "e2eCmd") if st.get(k)]
    # the scanners may take minutes and need no state: outside the lock, like `bv verify`
    findings = audit.scan(root, files, [f for f in files if f in changed], commands)
    tools, cves = audit.run_tools(root, files, check_env(os.environ))
    findings = audit.ordered(findings + cves)
    # shortcut: files are read from the working tree, not from HEAD; an uncommitted edit is warned about, not audited apart
    dirty = git(root, "status", "--porcelain", "--untracked-files=no", "--", ".", ":(exclude).software-architector",
                check=False)
    with locked(root):
        fresh = load(root)
        au = {"head": head, "base": base, "at": now(), "tools": tools,
              "accepted": accepted_findings(fresh),  # ids are stable: a decision outlives a re-run
              "findings": [{k: f[k] for k in ("id", "sev", "kind", "rule", "file", "line")} for f in findings]}
        fresh["audit"] = au
        path = run_dir(root, fresh) / "audit.md"
        path.write_text(audit.report(au, findings), "utf-8")
        log(fresh, "audit", head=head[:7], findings=len(findings))
        save(root, fresh)
    out = [f"{audit.summary(au)} · {path.relative_to(root).as_posix()}"]
    out += [f"! {f['id']} {f['sev']} {f['rule']} {f['file']}" + (f":{f['line']}" if f["line"] else "") + f" — {f['msg']}"
            for f in findings if f["sev"] in audit.BLOCKING and f["id"] not in au["accepted"]]
    if dirty:
        out.append(f"! not committed, the audit read these files as they are now:\n{dirty}")
    print("\n".join(out))


def cmd_audit_accept(st, a):
    """A false positive: the block is lifted, and the reason goes into the report verbatim (like --fewer-tests)."""
    au = st.get("audit") or {}
    found = au.get("findings", []) + [x for t in st["tickets"] for x in (t.get("audit") or {}).get("findings", [])]
    f = next((x for x in found if x["id"] == a.accept), None)
    if not f:
        raise Fail(f"no finding {a.accept} in the last audit: the ids are in audit.md or in the refusal of `bv commit` "
                   f"(run `bv audit` first)")
    note = (a.note or "").strip()
    if not note:
        raise Fail(f"--accept {f['id']} needs --note \"<why it is not a defect>\": it goes into the report verbatim")
    st.setdefault("auditAccepted", {})[f["id"]] = note
    if au:
        au.setdefault("accepted", {})[f["id"]] = note
    st["lists"]["report"].append(f"Audit finding {f['id']} ({f['sev']} {f['rule']}, {f['file']}:{f['line']}) accepted: {note}")
    log(st, "audit-accept", id=f["id"], note=note)
    print(f"Audit finding {f['id']}: accepted")


def delivery_problems(root, st):
    """Everything standing between this run and "delivered", from facts: state, manifest, git, journal. Empty = proven."""
    rows = read_manifest(manifest_path(root, st))
    head = git(root, "rev-parse", "HEAD", check=False)
    tickets, gates = st["tickets"], st["gates"]
    bad = [] if tickets else ["no ticket at all: nothing is built"]
    bad += [f"ticket {t['id']} failed: {t.get('note') or 'reason not recorded'}" for t in tickets if t["status"] == "failed"]
    bad += [f"{g} not passed" for g in ("G1", "G2", "G3") if not (gates.get(g) or {}).get("ok")]
    g5, g4 = gates.get("G5"), gates.get("G4")
    if not g5:
        bad.append("no clean clone check: run `bv verify` first")
    elif not g5.get("ok"):
        bad.append("clean clone check is red (G5): fix and repeat `bv verify`")
    elif g5.get("head") != head:
        bad.append("code changed after `bv verify`: repeat the clean clone check")
    if not g4:
        bad.append("no blind acceptance: run `bv gate G4` first")
    elif not g4.get("ok"):
        bad += [f"blind acceptance: {m}" for m in g4.get("mismatches", [])] or ["blind acceptance not passed"]
    elif g4.get("head") != head:
        bad.append("code changed after blind acceptance: repeat it")
    au = st.get("audit")
    if not au:
        bad.append("no audit: run `bv audit` first")
    elif au.get("head") != head:
        bad.append("code changed after the audit: repeat `bv audit`")
    else:
        bad += [f"audit finding {f['id']} ({f['sev']} {f['rule']}, {f['file']}:{f['line']}): fix it, or if it is a false "
                f"positive: `bv audit --accept {f['id']} --note \"<why>\"`, it goes into the report"
                for f in audit.open_blocking(au)]
    if not next(s for s in st["stages"] if s["id"] == "final").get("startedAt"):
        bad.append("the acceptance stage never started: branch code review not confirmed")
    last = last_commit_at(st)
    if last and not agent_finished_since(root, st, "bv-reviewer", last):
        bad.append("code changed after the last code review: run bv-reviewer (repair mode) on the new commit")
    bad += vanished(st, rows)
    for r in live(rows):
        holders = [t for t in tickets if covers(t, r["id"])]
        if r["status"] in UNDELIVERED:
            bad.append(f"{r['id']}: not delivered (status {r['status']})")
        elif r["status"] in ("done", "placeholder") and not (holders and all(t["status"] == "done" and t["commit"] for t in holders)):
            bad.append(f"{r['id']}: marked {r['status']}, but its tickets are not closed")
    return bad


# ---------- dashboard server ----------

def port_base(root):
    return 8700 + zlib.crc32(str(root).lower().encode()) % 100


def probe(port):
    """False — nothing listens; '' — someone else's server; otherwise the project root it serves."""
    try:
        # a refused localhost connect hangs ~2 s on Windows instead of failing fast — keep the timeout short
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
    except OSError:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
            return json.loads(r.read().decode("utf-8")).get("root") or ""
    except Exception:
        return ""


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def url_file(root):
    return root / ".software-architector" / "live" / "dashboard.url"


def url_hint(root):
    """(port, url with the key) the last server of this project wrote; (None, None) when there is none."""
    try:
        url = url_file(root).read_text("utf-8").strip()
    except OSError:
        return None, None
    m = re.match(r"http://localhost:(\d+)/", url)
    return (int(m.group(1)), url) if m else (None, None)


def find_server(root):
    # the port the last server wrote first: a busy base port earlier may have pushed it further than a free one now
    hint, _ = url_hint(root)
    served = probe(hint) if hint else False
    if served and same_path(served, str(root)):
        return hint
    # serve() takes the first free port from the base, so the first free port ends the search
    for port in range(port_base(root), port_base(root) + 20):
        r = probe(port)
        if r is False:
            return None
        if r and same_path(r, str(root)):
            return port
    return None


def dashboard_url(root):
    port = find_server(root)
    if not port:
        return None
    hint, url = url_hint(root)
    return url if hint == port else f"http://localhost:{port}"


def stop_server(root):
    """`bv finish`: the dashboard of a closed run has nothing to show; its key is in the file only bv and hooks write."""
    port, url = url_hint(root)
    if not port or not same_path(probe(port) or "", str(root)):
        return
    with contextlib.suppress(Exception):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/shutdown?" + url.split("?", 1)[1], timeout=2).close()


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a busy port — never use it there.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def make_handler(root, key):
    page = Path(__file__).resolve().parent / "dashboard.html"

    class Handler(BaseHTTPRequestHandler):
        def authorized(self):
            """The run's key: in the address bv printed (`?k=`), then in a cookie the page set; /health is open."""
            given = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("k", [""])[0]
            cookie = re.search(r"(?:^|;\s*)sa_key=([\w-]+)", self.headers.get("Cookie", ""))
            return any(hmac.compare_digest(c, key) for c in (given, cookie.group(1) if cookie else "") if c)

        def do_GET(self):
            # DNS rebinding: a page of another site reaches this port under its own name and would read the journal
            if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("localhost", "127.0.0.1"):
                self.send_error(403)
                return
            path = self.path.split("?")[0]
            if path != "/health" and not self.authorized():
                self.send_error(403, "key required: open the address `bv url` prints")  # HTTP reason: latin-1 only
                return
            cookie = None
            if path == "/shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                body, ctype = b"stopping", "text/plain"
            elif path == "/":
                body, ctype = page.read_bytes(), "text/html; charset=utf-8"
                cookie = f"sa_key={key}; HttpOnly; SameSite=Strict; Path=/"
            elif path == "/state.json":
                p = state_path(find_root(root))
                body, ctype = (p.read_bytes() if p.exists() else b"null"), "application/json; charset=utf-8"
            elif path == "/health":
                body, ctype = json.dumps({"root": str(root)}).encode(), "application/json"
            elif path == "/agents.json" or path.startswith("/out/"):
                r = find_root(root)
                st = try_load(r)
                if path == "/agents.json":
                    data = events.read_agents(r, st) if st else {}
                    body, ctype = json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8"
                else:
                    p = events.out_path(r, st, path[5:]) if st else None
                    if not p:
                        self.send_error(404)
                        return
                    body, ctype = p.read_bytes(), "text/plain; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass  # monitor stdout reaches Claude as notifications — stay silent

    return Handler


def serve(root):
    # one lookup-and-bind at a time: the monitor and `bv serve --detach` starting together made two servers
    with locked(root, name="serve", wait=10, busy="the dashboard is already being started by another bv call"):
        url = dashboard_url(root)
        if url:
            print(f"Software-Architector dashboard is already running: {url}", flush=True)
            return
        key = secrets.token_urlsafe(16)  # per server start: a local page or process without the address reads nothing
        for port in range(port_base(root), port_base(root) + 20):
            try:
                srv = Server(("127.0.0.1", port), make_handler(root, key))
                break
            except OSError:
                continue
        else:
            raise Fail("no free port for the dashboard")
        url = f"http://localhost:{port}/?k={key}"
        # shortcut: the file outlives a killed server; the pane only shows it as a hint, `bv url` is the real check
        (events.ensure_live(root) / "dashboard.url").write_text(url + "\n", "utf-8")
    print(f"Software-Architector dashboard: {url} — in VS Code the run panel opens by itself on the right; "
          f"in a terminal, open the address in a browser", flush=True)
    srv.serve_forever()
    srv.server_close()


def start_server(root):
    """The dashboard's address, starting a server first when none serves this project."""
    url = dashboard_url(root)
    if url:
        return url
    # a server of its own, detached: it outlives this command as the monitor's outlives the skill call,
    # and unlike a background task of the Bash tool it never keeps a `claude -p` session from ending
    kw = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
          else {"start_new_session": True})
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"], cwd=root, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    for _ in range(50):
        time.sleep(0.1)
        url = dashboard_url(root)
        if url:
            return url
    raise Fail("the dashboard did not start: try `bv serve` and read the error")


def web(root):
    url = start_server(root)
    webbrowser.open(url)
    print(url)


# ---------- commands ----------

def new_state(slug, title, mode, depth, rdir, mem_file, mem_owner):
    return {"schemaVersion": 3, "slug": slug, "title": title or slug, "dir": rdir, "mode": mode, "depth": depth,
            "tier": None, "checkCmd": None, "memoryFile": mem_file, "memoryOwner": mem_owner,
            "guard": "on", "resume": None, "baseCommit": None, "startedAt": now(), "updatedAt": now(), "finishedAt": None,
            "result": None,
            "stages": [{"id": s, "label": lbl, "status": "active" if s == "preflight" else "pending",
                        "startedAt": now() if s == "preflight" else None, "finishedAt": None, "note": ""}
                       for s, lbl in STAGES],
            "requirements": {"total": 0}, "tickets": [], "tests": {"passed": 0, "failed": 0},
            "gates": {g: None for g in GATES}, "lists": {k: [] for k in LISTS},
            "log": [], "rev": 0}


def cmd_init(root, a):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", a.slug):
        raise Fail("--slug: lowercase Latin letters, digits and hyphen, up to 41 characters")
    if a.mode not in MODES or a.depth not in DEPTHS:
        raise Fail(f"mode: {'/'.join(MODES)}; depth: {'/'.join(DEPTHS)}")
    old = try_load(root)
    if old is None and state_path(root).exists():
        if not a.force:
            load(root)  # unreadable or foreign-schema state: never overwrite it silently
        kept = state_path(root).with_name(f"state.broken-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copyfile(state_path(root), kept)  # --force starts over, the old record stays for the user to inspect
        print(f"! unreadable state.json saved as .software-architector/{kept.name}")
    if old:
        if not old.get("finishedAt") and not a.force:
            raise Fail(f"there is an unfinished run {old['dir']}: continue it (/software-architector:build without arguments) or init --force")
        arch = run_dir(root, old)
        arch.mkdir(parents=True, exist_ok=True)
        os.replace(state_path(root), arch / "state.json")
    if git(root, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        git(root, "init", "-q")
        print("· git init")
    gi = root / ".gitignore"
    have = gi.read_text("utf-8").splitlines() if gi.exists() else []
    missing = [x for x in GITIGNORE_LINES if x not in have]
    if missing:
        text = gi.read_text("utf-8") if gi.exists() else ""
        gi.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "\n".join(missing) + "\n", "utf-8")
    rdir = f"runs/{dt.date.today().isoformat()}-{a.slug}--wip"
    n = 2
    while (root / ".software-architector" / rdir).exists() or (root / ".software-architector" / rdir.removesuffix("--wip")).exists():
        rdir = f"runs/{dt.date.today().isoformat()}-{a.slug}-{n}--wip"
        n += 1
    (root / ".software-architector" / rdir / "tickets").mkdir(parents=True)
    st = new_state(a.slug, a.title, a.mode, a.depth, rdir, a.memory_file, a.memory_owner)
    st["stages"][0]["head"] = head_or_empty(root)
    log(st, "init", mode=a.mode, depth=a.depth)
    save(root, st)
    print(f"Run started: .software-architector/{rdir} · mode {a.mode}/{a.depth}")
    return st


def cmd_stage(root, st, a):
    if a.id not in STAGE_IDS:
        raise Fail(f"no stage {a.id}: {', '.join(STAGE_IDS)}")
    if a.action in ("skip", "fail") and not a.note:
        raise Fail(f"stage {a.action} needs --note")
    if a.action == "skip" and a.id not in SKIPPABLE_STAGES:
        raise Fail(f"stage {a.id} cannot be skipped: only these can be skipped: {', '.join(SKIPPABLE_STAGES)}")
    stage = next(s for s in st["stages"] if s["id"] == a.id)
    closed = []
    if a.action == "start":
        check_stage_gate(root, st, a.id)
        idx = STAGE_IDS.index(a.id)
        for i, s in enumerate(st["stages"]):
            if s["status"] == "active" and s["id"] != a.id:
                s["status"], s["finishedAt"] = "done", now()
                closed.append(s)
            elif s["status"] == "pending" and i < idx:  # jumped over: show it honestly, not as waiting
                s.update(status="skipped", finishedAt=now(), note=s["note"] or "not marked")
        if stage["status"] != "active":  # re-entered (changes after the review): proof must come from this round
            stage.update(startedAt=now(), finishedAt=None, head=head_or_empty(root))
        stage.update(status="active", note=a.note or stage["note"])
    else:
        stage.update(status={"done": "done", "skip": "skipped", "fail": "failed"}[a.action],
                     finishedAt=now(), note=a.note or stage["note"])
        closed.append(stage)
    log(st, "stage", id=a.id, action=a.action, note=a.note)
    for s in closed:
        print(close_stage(root, st, s))
    print(f"Stage {stage['label']}: {stage['status']}")


def check_stage_gate(root, st, sid):
    if sid == "spec":
        rows = read_manifest(manifest_path(root, st))
        ok, detail = gate_g1(rows, st["mode"])
        st["gates"]["G1"] = {"ok": ok, "at": now(), "detail": detail}
        if not ok:
            save(root, st)
            raise Fail(f"G1 not passed: {detail}")
        remember_ids(st, rows)
    elif sid == "build":
        for g in ("G2", "G3"):
            if not (st["gates"].get(g) or {}).get("ok"):
                raise Fail(f"{g} not passed — the build cannot start")
        if not st.get("checkCmd"):
            raise Fail("no check command: `bv set checkCmd=…` (tests + types + lint), a ticket commit is impossible without it")
        if scenarios(read_manifest(manifest_path(root, st))) and not st.get("e2eCmd"):
            raise Fail("the manifest has S## scenarios but no end-to-end test command: `bv set e2eCmd=…`")
    elif sid in ("review", "final"):
        if not st["tickets"]:
            raise Fail("no ticket at all: run `bv tickets` first")
        left = [t["id"] for t in st["tickets"] if t["status"] not in ("done", "failed")]
        if left:
            raise Fail(f"tickets not closed: {', '.join(left)}")
    if sid == "final":
        # the review stage closes on both gates, each on the current commit: a fix in it re-runs every check
        rv = next(s for s in st["stages"] if s["id"] == "review")
        if not rv.get("startedAt"):
            raise Fail("branch code review first: `bv stage review start`")
        if not agent_finished_since(root, st, "bv-reviewer", rv["startedAt"]):
            raise Fail("branch code review is not confirmed by the journal: run bv-reviewer in branch mode and wait for it")
        last = last_commit_at(st)
        if last and not agent_finished_since(root, st, "bv-reviewer", last):
            raise Fail("code changed after the last code review: run bv-reviewer (repair mode) on the new commit")
        au, head = st.get("audit") or {}, git(root, "rev-parse", "HEAD")
        if not au.get("at") or dt.datetime.fromisoformat(au["at"]) < dt.datetime.fromisoformat(rv["startedAt"]):
            raise Fail("no audit in this review round: run `bv audit`, give audit.md to bv-reviewer, fix or accept its findings")
        if au.get("head") != head:
            raise Fail("code changed after the audit: repeat `bv audit` on the current commit")
        blocking = audit.open_blocking(au)
        if blocking:
            raise Fail("code audit: blocking findings are open: " + ", ".join(f["id"] for f in blocking)
                       + " — fix them in F1, or accept a false positive the reviewer dropped: `bv audit --accept <id> --note …`")
        g5 = st["gates"].get("G5") or {}
        if not g5.get("ok") or g5.get("head") != head:
            raise Fail("no execution result on the current commit: run `bv verify` (clean clone: setup, check, e2e)")


def cmd_set(st, pairs, reason=None):
    building = next(s for s in st["stages"] if s["id"] == "build").get("startedAt")
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep or k not in SETTABLE:
            raise Fail(f"set {p}: allowed {', '.join(SETTABLE)}")
        if k == "mode" and v not in MODES or k == "depth" and v not in DEPTHS or k == "guard" and v not in ("on", "off") \
                or k == "resume" and v not in ("yes", "no") or k == "checkTimeout" and v and not v.isdigit():
            raise Fail(f"set {p}: invalid value")
        if k in ("setupCmd", "checkCmd", "e2eCmd") and v:
            # bv runs this text later with no hook in between, and parts of it may come from research.md (the web)
            why = guard.deny_reason(guard.unnest(v)) or guard.check_rm(v) or guard.check_git_add(v)
            hit = guard.find_secret(v)
            if why or hit:
                raise Fail(f"set {k}: command rejected — {why or f'looks like a secret ({hit})'}")
        if k == "testsPattern" and v:
            try:
                ok = re.compile(v).groups >= 1
            except re.error:
                ok = False
            if not ok:
                raise Fail(f"set {p}: needs a regex with group 1 — the number of passed tests")
        if k in FROZEN and building and st.get(k) != (v or None):
            if not reason:
                raise Fail(f"{k} is frozen since the build started: it decides what is green. If it really must change — "
                           f"`bv set {k}=… --reason \"<reason>\"`, the change goes into the report")
            st["lists"]["report"].append(f"{k} changed after the build started: '{st.get(k) or ''}' → '{v}'. "
                                         f"Reason: {reason}")
        if k == "checkTimeout" and v and int(v) > CHECK_TIMEOUT:
            print(f"! checkTimeout {v} s does not fit in one Bash call (600 s): `bv commit` and `bv verify` "
                  f"run them with run_in_background: true and read their output when they finish")
        if k == "guard" and v == "off":
            st["guardWasOff"] = True  # the journal stops too, so the report has to say so
        st[k] = v or None
        log(st, "set", key=k, value=v)
    print("Saved: " + ", ".join(pairs))


def cmd_tickets(root, st):
    tdir = run_dir(root, st) / "tickets"
    tickets = [parse_ticket(p) for p in sorted(tdir.glob("*.md"))]
    if not tickets:
        raise Fail(f"no ticket files in {tdir}")
    rows = read_manifest(manifest_path(root, st))
    bad = check_plan(rows, tickets) + vanished(st, rows)
    st["gates"]["G3"] = {"ok": not bad, "at": now(), "detail": bad[:20] or f"{len(tickets)} tickets"}
    if bad:
        save(root, st)
        raise Fail("G3 not passed — plan defects:\n" + "\n".join(f"! {b}" for b in bad))
    remember_ids(st, rows)
    old = {t["id"]: t for t in st["tickets"]}
    keep = ("status", "startedAt", "finishedAt", "retries", "repairs", "tests", "commit", "note", "placeholders",
            "contract", "proof", "committedAt")
    st["tickets"] = [{**{"status": "pending", "startedAt": None, "finishedAt": None, "retries": 0, "repairs": 0,
                         "tests": None, "commit": "", "note": "", "placeholders": [], "contract": None},
                      **{k: old[t["id"]][k] for k in keep if t["id"] in old and k in old[t["id"]]}, **t}
                     for t in tickets]
    changes = {}
    for r in live(rows):
        if r["status"] == "in-spec":
            ids = ",".join(t["id"] for t in tickets if covers(t, r["id"]))
            changes[r["id"]] = ("in-ticket", "T" + ids)
    if changes:
        update_manifest(manifest_path(root, st), changes)
    waves = max(t["wave"] for t in tickets)
    log(st, "tickets", count=len(tickets))
    print(f"Plan accepted (G3 ✓): {len(tickets)} tickets, waves: {waves}")


def cmd_ticket(root, st, a):
    if a.action == "fail" and not a.note:
        raise Fail("ticket fail needs --note with a reason")
    for tid in a.ids:
        t = find_ticket(st, tid)
        if a.action == "start":
            if t["status"] != "pending":
                raise Fail(f"ticket {t['id']} is already {t['status']}")
            waiting = [d for d in t["blockedBy"] if d not in landed(st)]
            if waiting:
                raise Fail(f"ticket {t['id']}: dependencies are not committed or a foundation is still in review: {', '.join(waiting)}")
            flying = [x for x in st["tickets"] if x["status"] in IN_FLIGHT]
            if len(flying) >= MAX_PARALLEL:
                raise Fail(f"{MAX_PARALLEL} tickets are already in flight — wait for a return")
            clash = [x["id"] for x in flying if zones_overlap(t["zone"], x["zone"])]
            if clash:
                raise Fail(f"ticket {t['id']}: zone overlaps with in-flight tickets: {', '.join(clash)}")
            t.update(status="in-progress", startedAt=now(), contract=None)
        elif a.action == "repair":
            if t["repairs"] >= MAX_REPAIRS:
                raise Fail(f"ticket {t['id']}: repair limit ({MAX_REPAIRS}) used up — `bv ticket {t['id']} retry` or `fail`")
            t["repairs"] += 1
            t.update(status="repair", contract=None)
        elif a.action == "retry":
            if t["retries"] >= MAX_RETRIES:
                raise Fail(f"ticket {t['id']}: retry limit ({MAX_RETRIES}) used up — `bv ticket {t['id']} fail --note …`")
            t["retries"] += 1
            t.update(status="in-progress", startedAt=now(), contract=None)
        elif a.action == "review":
            if not t["commit"]:
                raise Fail(f"ticket {t['id']} has no commit — nothing to review")
            t["status"] = "review"
        elif a.action == "done":
            if not t["commit"]:
                raise Fail(f"ticket {t['id']} has no commit — use `bv commit {t['id']} --tests P/F`")
            if t["status"] != "review":
                raise Fail(f"ticket {t['id']} has status {t['status']}: `done` closes only a ticket in review, `bv commit` closes the others")
            if not agent_finished_since(root, st, "bv-reviewer", t.get("committedAt"), ticket=t["id"]):
                raise Fail(f"ticket {t['id']}: the journal does not confirm that the bv-reviewer of this ticket finished work after "
                           f"the commit — run it with description 'Review of ticket {t['id']}'")
            t.update(status="done", finishedAt=now())
        elif a.action == "fail":
            t.update(status="failed", finishedAt=now())
        elif a.action == "reset":
            t.update(status="pending", startedAt=None, finishedAt=None, commit="", contract=None, proof=None, committedAt=None)
        if a.note:
            t["note"] = a.note
        log(st, "ticket", id=t["id"], action=a.action, note=a.note)
    closed = close_requirements(root, st)
    print(f"Ticket {', '.join(norm_tid(x) for x in a.ids)}: {a.action}" + (f" · closed {', '.join(closed)}" if closed else ""))


def tests_pair(s):
    m = re.fullmatch(r"(\d+)(?:/(\d+))?", s or "")
    if not m:
        raise Fail(f"--tests: format P/F, for example 34/0 (got '{s}')")
    return {"passed": int(m.group(1)), "failed": int(m.group(2) or 0)}


def downgrades(root, st, t, a):
    """Rows already `done` that this commit would turn into `placeholder`: only for a service nobody may call in a run."""
    ph = set(t.get("placeholders", [])) | set(a.placeholder or [])
    return [r["id"] for r in live(read_manifest(manifest_path(root, st)))
            if r["status"] == "done" and covers(t, r["id"]) and (r["id"] in ph or base_id(r["id"]) in ph)]


def commit_preconditions(root, st, a):
    """Everything a ticket's commit needs that costs nothing to check, so a long project check is never wasted."""
    if not a.tests:
        raise Fail("commit <ticket> needs --tests P/F")
    t = find_ticket(st, a.target)
    if t["status"] not in ("in-progress", "repair", "review"):
        raise Fail(f"ticket {t['id']} has status {t['status']} — nothing to commit")
    tests = tests_pair(a.tests)
    if tests["failed"]:
        raise Fail("no commit on a red check — repair first (bv ticket … repair)")
    if t.get("contract") in ("BLOCKED", "NEEDS_CONTEXT"):
        raise Fail(f"ticket {t['id']}: contract {t['contract']} — nothing to commit, repair first")
    if not t.get("contract"):
        raise Fail(f"ticket {t['id']}: contract not checked — run first: `bv contract {t['id']} --from -`")
    if not tests["passed"]:
        raise Fail("0 passed tests: an empty set counts as red")
    if not st.get("checkCmd"):
        raise Fail("no check command: `bv set checkCmd=…`")
    down = downgrades(root, st, t, a)
    if down and not (a.external or "").strip():
        raise Fail(f"{', '.join(down)} is already done: it can move to placeholder only because of an external service that in "
                   f"the run cannot be called for real: `--external \"<service and what the user will check>\"`, "
                   f"this goes into the report. Otherwise it is a defect: repair, not a stub")
    return t, tests


def cmd_commit(root, st, a):
    if a.target == "plan":
        h = commit_paths(root, memory_paths(st, PLAN_PATHS), "Software-Architector: build plan",
                         [f"Software-Architector-Run: {run_name(st)}"])
        st["baseCommit"] = h
        log(st, "commit", target="plan", commit=h)
        print(f"Plan committed: {h} (baseCommit)")
        return
    t, tests = commit_preconditions(root, st, a)
    for rid in downgrades(root, st, t, a):
        st["lists"]["report"].append(
            f"{rid}: was done, now placeholder — external service: {a.external.strip()}"
            + (". Full mode: the user was not asked, they must confirm and check it by hand" if st["mode"] == "full" else ""))
    if a.proof["tests"] is not None:  # what the runner printed outranks what was claimed
        tests = {"passed": a.proof["tests"], "failed": 0}
        st["testsMeasured"] = a.proof["tests"]
    reqs = ", ".join(t["requirements"])
    subject = f"T{t['id']}: fix after review" if a.repair else f"T{t['id']}: {t['title']}"
    # one attempt = one key: a commit whose bookkeeping was lost (a crash, a busy state.json) is found again by it
    key = f"{run_name(st)}/T{t['id']}/{t.get('startedAt')}/r{t.get('repairs', 0)}"
    h = attempt_commit(root, key)
    if h:
        print(f"! the commit of this attempt already exists ({h}) but was not recorded: recording it, no new commit")
    else:
        h = commit_paths(root, t["zone"] + [".env.example"] + (a.also or []), subject,
                         [f"Software-Architector-Run: {run_name(st)}", f"Requirements: {reqs}",
                          f"Software-Architector-Commit: {key}"])
    t.update(commit=h, tests=tests, proof=a.proof, audit=a.audit, committedAt=now())
    t["placeholders"] = sorted(set(t.get("placeholders", [])) | set(a.placeholder or []))
    st["tests"] = tests  # the project-wide count of the latest check (it runs the whole project), not a sum of tickets
    if a.fewer_tests:
        st["lists"]["report"].append(f"Ticket {t['id']}: test count dropped, reason: {a.fewer_tests}")
    # a reviewed ticket goes back to review after a repair: one re-check, then `bv ticket NN done`
    t["status"] = "review" if t.get("review", "").lower().startswith(("да", "yes")) else "done"
    if t["status"] == "done":
        t["finishedAt"] = now()
    try:
        closed = close_requirements(root, st)
    except Fail as e:  # the commit exists: its record must reach state.json; the manifest catches up on the next bv call
        closed = []
        print(f"! commit recorded, but the manifest is not updated: {e}")
    log(st, "commit", target=t["id"], commit=h)
    print(f"T{t['id']} committed: {h} · {t['status']}" + (f" · closed {', '.join(closed)}" if closed else ""))


def read_arg(src):
    if src in (None, "-"):
        return sys.stdin.read()
    try:
        return Path(src).read_text("utf-8-sig")  # PowerShell's Out-File writes a BOM
    except (OSError, UnicodeDecodeError) as e:
        raise Fail(f"cannot read {src}: {e}. The file must exist and be UTF-8")


def read_json_arg(src):
    return parse_json(read_arg(src))


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)  # tolerate a ```json fence around the block
    try:
        data = json.loads(m.group() if m else text)
    except json.JSONDecodeError as e:
        raise Fail(f"JSON not parsed: {e.msg} (line {e.lineno})")
    if not isinstance(data, dict):
        raise Fail("JSON not parsed: an object was expected")
    return data


def agent_json(root, st, kind, since):
    """A checker's verdict exactly as it returned it: its final answer, which the SubagentStop hook wrote to the journal."""
    e = agent_stop(root, st, kind, since)
    if not e:
        raise Fail(f"the journal does not confirm that {kind} finished work: run it and wait for the result")
    p = events.out_path(root, st, e.get("final", ""))
    try:
        return parse_json(p.read_text("utf-8", errors="replace") if p else e.get("last", ""))
    except Fail as x:
        raise Fail(f"the result of {kind} in the journal is not parsed ({x}): ask it to return only the JSON block and wait")


def parse_contract(text):
    lines = [l.rstrip() for l in text.splitlines() if not l.strip().startswith("```")]
    start = next((i for i, l in enumerate(lines) if l.startswith("STATUS:")), None)
    if start is None:
        raise Fail("the reply has no contract block (STATUS: … lines)")
    lines = lines[start:]
    while lines and not lines[-1].strip():
        lines.pop()
    fields, cur = {}, None
    for l in lines:
        m = re.match(r"([A-Z]+):\s*(.*)$", l)
        if m and m.group(1) in CONTRACT_KEYS:
            cur = m.group(1)
            fields[cur] = m.group(2).strip()
        elif cur:
            fields[cur] += "\n" + l
    bad = [f"no field {k}" for k in CONTRACT_KEYS if k not in fields]
    if len(lines) > 25:
        bad.append(f"{len(lines)} lines instead of ≤ 25")
    status = fields.get("STATUS", "")
    if status not in CONTRACT_STATUSES:
        bad.append(f"STATUS '{status}': allowed {' | '.join(CONTRACT_STATUSES)}")
    tests = None
    if status.startswith("DONE"):
        m = re.search(r"→\s*(\d+)\s*/\s*(\d+)", fields.get("TESTS", ""))
        if not m:
            bad.append("TESTS: no '<command> → <passed>/<failed>'")
        else:
            tests = {"passed": int(m.group(1)), "failed": int(m.group(2))}
            if tests["failed"]:
                bad.append(f"DONE with red tests ({tests['failed']} failed) — this is BLOCKED")
    if status == "DONE" and not redproof_given(fields):
        bad.append("REDPROOF is empty: test-first is not proven. Prove it with a red run or return DONE_WITH_CONCERNS with a reason")
    return status, tests, fields, bad


def redproof_given(fields):
    return fields.get("REDPROOF", "").strip(" —-").lower() not in ("", "none", "нет")


def reported_requirements(text):
    """`R01 done | R01.1 placeholder — why` → {id: state}."""
    return {m.group(1): m.group(2).lower() for m in (REQ_REPORT.match(p) for p in re.split(r"[|,;\n]", text)) if m}


def cmd_contract(root, st, a):
    t = find_ticket(st, a.id)
    status, tests, fields, bad = parse_contract(read_arg(a.src))
    reported = reported_requirements(fields.get("REQUIREMENTS", ""))
    if status.startswith("DONE"):
        # `R01` in the ticket also closes `R01.1`, `R01i`: each of them needs its own word, or it is closed unreported
        rows = [r["id"] for r in live(read_manifest(manifest_path(root, st)))
                if covers(t, r["id"]) and r["status"] not in ("dropped", "deferred")]
        bad += [f"REQUIREMENTS: no report for {r}: done or placeholder"
                for r in dict.fromkeys(t["requirements"] + rows) if r not in reported]
    if bad:
        raise Fail(f"contract of ticket {t['id']} not accepted:\n" + "\n".join(f"! {b}" for b in bad))
    t["contract"] = status
    t["placeholders"] = sorted(set(t.get("placeholders", [])) | {r for r, state in reported.items() if state == "placeholder"})
    log(st, "contract", id=t["id"], status=status)
    warn = "" if redproof_given(fields) or not status.startswith("DONE") else " · ! REDPROOF is empty: test-first is not proven"
    print(f"Contract T{t['id']}: {status}" + (f" · --tests {tests['passed']}/{tests['failed']}" if tests else "") + warn)


def cmd_handoff(root, st, a):
    """One repair package for both routes: a message to the same executor or the prompt of a fresh one."""
    t = find_ticket(st, a.id)
    rdir = run_dir(root, st)
    files = [p for p in sorted((rdir / "tickets").glob("*.md"))  # `3-bot.md` is ticket 03 too, as parse_ticket reads it
             if norm_tid(re.split(r"[-_.]", p.name, maxsplit=1)[0]) == t["id"]]
    out = [f"Ticket {t['id']} — {t['title']}",
           "Ticket file: " + (files[0].relative_to(root).as_posix() if files else "?"),
           "Read yourself: " + ", ".join(p.relative_to(root).as_posix() for p in (rdir / n for n in ("spec.md", "interfaces.md", "notes.md", "research.md"))
                                        if p.exists()),
           "Zone (write only inside it): " + ", ".join(t["zone"]),
           f"Condition: {a.note}"]
    if a.tail:
        tail = sys.stdin.read() if a.tail == "-" else Path(a.tail).read_text("utf-8-sig", errors="replace")
        out += ["Output tail:", guard.mask_secrets("\n".join(tail.splitlines()[-10:]))]
    out.append("Name the cause in one line, then fix. Touch nothing else. Return the contract again.")
    print("\n".join(out))


def cmd_doctor(root):
    st = try_load(root)
    hooks = PLUGIN_ROOT / "hooks" / "hooks.json"
    htext = hooks.read_text("utf-8") if hooks.exists() else ""
    checks = [("git in PATH", shutil.which("git") is not None), ("bin/bv", (PLUGIN_ROOT / "bin" / "bv").exists())]
    if st is None and state_path(root).exists():
        try:
            load(root)
        except Fail as e:
            checks.append((str(e), False))
    checks += [(f"hook {e} registered", f"hook {e}\"" in htext) for e in HOOK_EVENTS]
    if st and not st.get("finishedAt") and st.get("guard") != "off":
        # registered is not firing: in a wrong folder or a broken install the guard would be silently off
        checks.append(("hooks fire: the run journal is not empty", any(events.read_agents(root, st).values())))
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(("✓ " if ok else "✗ ") + n)
    if st:
        print(f"✓ state.json: schema {SCHEMA}")
        if st.get("resume") is None:
            print("! resume is not set: repair runs in a fresh context (`bv set resume=yes` if SendMessage exists)")
    if bad:
        raise Fail("doctor: not OK — " + ", ".join(bad))


def cmd_gate(root, st, a):
    if a.gate == "G4" and a.src is not None:
        raise Fail("bv takes the G4 verdict itself from the hook journal — the bv-blind result as the agent returned it: just `bv gate G4`. "
                   "A verdict that passed through the orchestrator is no longer independent")
    if a.gate == "G2":
        spec = next(s for s in st["stages"] if s["id"] == "spec")
        found = agent_json(root, st, "bv-coverage", spec.get("startedAt")).get("items") or []
        # the findings are the agent's; only the decision on each one is the orchestrator's, matched by the quote
        given = {i.get("quote"): i for i in read_json_arg(a.src).get("items") or [] if isinstance(i, dict)}
        items = [{**i, **{k: v for k, v in given.get(i.get("quote"), {}).items() if k in ("resolution", "row")}}
                 for i in found if isinstance(i, dict)]
        ids = {r["id"] for r in read_manifest(manifest_path(root, st))}
        open_items = [i for i in items if i.get("resolution") not in ("fixed", "deferred", "cut", "attached")
                      or (i.get("resolution") == "attached" and i.get("row") not in ids)]
        ok = not open_items
        st["gates"]["G2"] = {"ok": ok, "at": now(), "found": len(items), "unresolved": len(open_items),
                             "items": items[:50]}
        msg = f"G2 {'✓' if ok else '✗'}: findings {len(items)}, without a resolution {len(open_items)}"
        for i in open_items:
            msg += f"\n! {i.get('quote', '?')}: resolution fixed/deferred/cut, or attached with row, a manifest row"
    else:
        g5, head = st["gates"].get("G5") or {}, git(root, "rev-parse", "HEAD")
        if not g5.get("ok") or g5.get("head") != head:
            raise Fail("G5 not passed on the current commit: run `bv verify` first, then blind acceptance")
        data = agent_json(root, st, "bv-blind", g5["at"])
        rows = {r["id"]: r for r in live(read_manifest(manifest_path(root, st)))}
        verdicts = {v.get("id"): v for v in data.get("requirements", [])}
        run, outcome, mism = data.get("run") or {}, data.get("outcome") or {}, []
        if run.get("ok") is not True:
            mism.append("the project did not start from a clean clone")
        if not run.get("commands"):
            mism.append("the acceptance ran no command")
        if outcome.get("solved") is not True:
            mism.append(f"the user's task is not solved: {outcome.get('why') or 'no outcome verdict'}")
        for rid, r in rows.items():
            v = verdicts.get(rid)
            if r["status"] in UNDELIVERED:
                mism.append(f"{rid}: not delivered (status {r['status']})")
            elif r["status"] in ("done", "placeholder") and not v:
                mism.append(f"{rid}: blind acceptance did not check it")
            elif r["status"] == "done" and v and v.get("verdict") != "implemented":
                mism.append(f"{rid}: done in the manifest, blind acceptance says {v.get('verdict')}")
            elif r["status"] == "placeholder" and v and v.get("verdict") == "no":
                mism.append(f"{rid}: the stub is not visible, blind acceptance says no")
        # a brief line the manifest has no ID for: the blind check quotes it as its id, and it is not lost
        known = {r["id"] for r in read_manifest(manifest_path(root, st))}
        unmapped = [v for v in verdicts.values() if v.get("id") not in known]
        mism += [f"brief line without an ID in the manifest '{v.get('id')}': {v.get('verdict')} — add a G## row and fix"
                 for v in unmapped if v.get("verdict") != "implemented"]
        checked = len(verdicts)
        st["gates"]["G4"] = {"ok": not mism, "at": now(), "head": head, "checked": checked,
                             "matched": checked - sum(1 for m in mism if m[:1] in "RGAS"), "mismatches": mism,
                             "run": run, "outcome": outcome, "unmapped": unmapped}
        msg = f"G4 {'✓' if not mism else '✗'}: checked {checked}, mismatches {len(mism)}"
        for m in mism:
            msg += f"\n! {m}"
    log(st, "gate", gate=a.gate)
    print(msg)


def cmd_add(st, a):
    if a.list not in LISTS:
        raise Fail(f"add: lists {', '.join(LISTS)}")
    lst = st["lists"][a.list]
    new = [x for x in a.items if x not in lst]
    lst += new
    log(st, "add", list=a.list, n=len(new))
    print(f"{a.list}: +{len(new)}")


def verdict_line(st):
    if st.get("accepted") is False:
        return "Build closed WITHOUT acceptance"
    return "Build delivered" + (f", except: {', '.join(st['unproven'])}" if st.get("unproven") else "")


def head_or_empty(root):
    return git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)  # "" in a repo without commits yet


def changed_files(root, base, head):
    files = git(root, "diff", "--name-status", f"{base}..{head}", "--", ".", ":(exclude).software-architector",
                check=False).splitlines()
    return [f"  {f}" for f in files] if files else ["  —"]


def ticket_line(t):
    tests, au = t.get("tests") or {}, t.get("audit")
    return (f"  T{t['id']} {t['status']} · commit {(t.get('commit') or '—')[:7]} · tests "
            + (f"{tests['passed']}/{tests['failed']}" if tests else "—")
            + f" · repairs {t.get('repairs', 0)} · retries {t.get('retries', 0)}"
            + f" · audit {audit_counts(au) if au else '—'}")


def gate_line(g, v):
    line = f"  {g} {'✓' if v and v.get('ok') else ('✗' if v else '—')}"
    if v and v.get("head"):
        line += f" on {v['head'][:7]}"
    if v and g == "G4":
        line += f" · checked {v.get('checked', 0)} · mismatches {len(v.get('mismatches') or [])}"
    if v and g == "G5":
        line += "".join(f" · {s['step']} exit {s['exit']} · tests {s.get('tests')}" for s in v.get("steps") or [])
    return line


def receipt(root, st, landing):
    """What `bv finish` hands over: files changed since the plan, each ticket's proof and every gate, from git and state."""
    base = st.get("baseCommit")
    out = ["Receipt"]
    if base:
        out.append(f"Files changed since the plan ({base[:7]}..{landing[:7]}):")
        out += changed_files(root, base, landing)
    else:
        out.append("Files changed: unknown, no baseCommit (`bv commit plan` never ran)")
    out.append("Tickets:" if st["tickets"] else "Tickets: —")
    out += [ticket_line(t) for t in st["tickets"]]
    out.append("Gates:")
    out += [gate_line(g, st["gates"].get(g)) for g in GATES]
    out.append(f"  {audit.summary(st['audit'])}" if st.get("audit") else "  no audit")
    return "\n".join(out)


def after(ts, since):
    return bool(ts) and (not since or dt.datetime.fromisoformat(ts) >= dt.datetime.fromisoformat(since))


def stage_receipt(root, st, stage):
    """The receipt of one stage: files committed in it, and what was run and audited in it."""
    since, base, head = stage.get("startedAt"), stage.get("head"), head_or_empty(root)
    out = [f"Receipt — stage {stage['label']} ({stage['status']}, {since or '?'} → {stage.get('finishedAt') or '?'})"]
    if base is None:
        out.append("Files changed: unknown, the stage started before receipts existed")
    elif not head:
        out.append("Files changed: —")
    else:
        out.append(f"Files changed in this stage ({(base or 'empty')[:7]}..{head[:7]}):")
        out += changed_files(root, base or EMPTY_TREE, head)
    tickets = [t for t in st["tickets"] if after(t.get("committedAt"), since)
               or (t["status"] == "failed" and after(t.get("finishedAt"), since))]
    gates = [g for g in GATES if after((st["gates"].get(g) or {}).get("at"), since)]
    au = st.get("audit") if after((st.get("audit") or {}).get("at"), since) else None
    if not (tickets or au or {"G4", "G5"} & set(gates)):  # G1–G3 are the gates of documents, not of code
        out.append("Execution and code audit: not applicable, this stage built and checked no code")
    if tickets:
        out.append("Tickets (each commit ran the full check and the code audit):")
        out += [ticket_line(t) for t in tickets]
    if gates:
        out.append("Gates:")
        out += [gate_line(g, st["gates"][g]) for g in gates]
    if au:
        out.append(f"Branch {audit.summary(au)}")
    return "\n".join(out)


def close_stage(root, st, stage):
    """A closed stage leaves its receipt in the state and in receipts.md of the run folder."""
    text = stage_receipt(root, st, stage)
    st.setdefault("receipts", []).append(text)
    path = run_dir(root, st) / "receipts.md"
    path.write_text("# Stage receipts\n\n" + "\n\n".join(f"```text\n{r}\n```" for r in st["receipts"]) + "\n", "utf-8")
    return text


def cmd_finish(root, st, a):
    if st.get("finishedAt"):
        raise Fail("the run is already finished")
    left = [t["id"] for t in st["tickets"] if t["status"] not in ("done", "failed")]
    if left:
        raise Fail(f"tickets not closed: {', '.join(left)}")
    problems = delivery_problems(root, st)
    if problems and not a.not_accepted:
        raise Fail("cannot deliver, the work is not proven:\n" + "\n".join(f"! {p}" for p in problems) +
                   "\nFix it (ticket → `bv verify` → blind acceptance). If attempts are used up or the user is needed: "
                   "`bv finish --not-accepted --result <what exists>`, and tell the user first that the build is NOT accepted")
    accepted, before = not problems, copy.deepcopy(st)
    unproven = [r["id"] for r in live(read_manifest(manifest_path(root, st))) if r["status"] == "placeholder"]
    if st.get("guardWasOff"):
        st["lists"]["report"].append("During the run the guard was switched off (guard=off): the journal and the bans did not work in that time.")
    if st.get("audit"):  # the accepted findings are in the report already, from `bv audit --accept`
        st["lists"]["report"].append(audit.summary(st["audit"]))
    last_stage = []
    for s in st["stages"]:
        if s["status"] in ("active", "pending"):
            s.update(status="done" if s["status"] == "active" else "skipped", finishedAt=now())
            if s["status"] == "done":
                last_stage.append(close_stage(root, st, s))
    old = run_dir(root, st)
    st["dir"] = st["dir"].removesuffix("--wip")
    os.replace(old, run_dir(root, st))
    drop_blind_clone(st)
    st.update(finishedAt=now(), result=a.result, accepted=accepted, problems=problems, unproven=unproven)
    log(st, "finish", result=a.result, accepted=accepted)
    save(root, st)
    try:
        h = commit_paths(root, memory_paths(st, FINAL_PATHS), FINAL_SUBJECT if accepted else NOT_ACCEPTED_SUBJECT,
                         [f"Software-Architector-Run: {run_name(st)}"])
    except Fail:  # no landing commit, no landing: the run stays open and the command can be repeated
        os.replace(run_dir(root, st), old)
        save(root, before)
        raise
    if accepted:
        print(f"{verdict_line(st)}: {h}")
    else:
        print(f"Build closed WITHOUT acceptance: {h}\n" + "\n".join(f"! {p}" for p in problems))
    for text in last_stage:
        print(text)
    print(receipt(root, st, h))
    rest = git(root, "status", "--porcelain", check=False)
    if rest:
        print("! left uncommitted (into the report):\n" + rest)
    stop_server(root)


def cmd_reopen(root, st):
    if not st.get("finishedAt"):
        raise Fail("the run is not finished anyway")
    old = run_dir(root, st)
    st["dir"] += "--wip"
    os.replace(old, run_dir(root, st))
    st.update(finishedAt=None, result=None, accepted=None, problems=[], unproven=[])
    for s in st["stages"]:
        if s["id"] != "preflight":
            s.update(status="active" if s["id"] == "manifest" else "pending", startedAt=None, finishedAt=None)
    st["gates"] = {g: None for g in GATES}
    log(st, "reopen")
    print(f"Run reopened: .software-architector/{st['dir']} — new dated brief, next requirement numbers")


def status_text(root, st, with_url=True):
    stage = next((s for s in st["stages"] if s["status"] == "active"), None)
    g = " ".join(f"{k} {'✓' if v and v.get('ok') else ('✗' if v else '—')}" for k, v in st["gates"].items())
    rq = st.get("requirements") or {}
    tk = st["tickets"]
    done = sum(t["status"] == "done" for t in tk)
    fly = [t["id"] for t in tk if t["status"] in IN_FLIGHT]
    rev = [t["id"] for t in tk if t["status"] == "review"]
    lines = [f"Software-Architector · {st['title']} · mode {st['mode']}/{st['depth']}" + (f" · {st['tier']}" if st.get("tier") else ""),
             f"Folder: .software-architector/{st['dir']}",
             f"{verdict_line(st)}: {st.get('result') or ''}"
             if st.get("finishedAt") else f"Stage: {stage['label']} ({stage['id']})" if stage else "Stage: —",
             f"Gates: {g}"]
    if rq.get("total"):
        lines.append(f"Requirements: {rq['total']} · done {rq.get('done', 0)} · in tickets {rq.get('in-ticket', 0)}"
                     f" · in spec {rq.get('in-spec', 0)} · open {rq.get('open', 0)} · placeholders {rq.get('placeholder', 0)}"
                     f" · deferred {rq.get('deferred', 0)} · dropped {rq.get('dropped', 0)}")
    if tk:
        lines.append(f"Tickets: done {done}/{len(tk)}" + (f" · in flight {', '.join(fly)}" if fly else "")
                     + (f" · in review {', '.join(rev)}" if rev else ""))
        ready = [t["id"] for t in ready_tickets(st)]
        if ready and not st.get("finishedAt"):
            lines.append(f"Ready to start: {', '.join(ready)}")
    if st.get("audit"):
        lines.append(audit.summary(st["audit"]))
        lines += [f"Audit accepted {i}: {note}" for i, note in (st["audit"].get("accepted") or {}).items()]
    if st.get("guard") == "off":
        lines.append("! Software-Architector hooks are off (guard=off)")
    if with_url:
        url = dashboard_url(root)
        lines.append(f"Dashboard: {url}" if url else "Dashboard: not running (`bv serve --detach`)")
    return "\n".join(lines)


# ---------- entry ----------

def build_parser():
    p = argparse.ArgumentParser(prog="bv", description="Software-Architector: run bookkeeping")
    sub = p.add_subparsers(dest="cmd")
    i = sub.add_parser("init")
    i.add_argument("--slug", required=True)
    i.add_argument("--title")
    i.add_argument("--mode", default="semi")
    i.add_argument("--depth", default="normal")
    i.add_argument("--memory-file", default="AGENTS.md")
    i.add_argument("--memory-owner", default="software-architector", choices=("software-architector", "user"))
    i.add_argument("--force", action="store_true")
    sub.add_parser("status")
    s = sub.add_parser("stage")
    s.add_argument("id")
    s.add_argument("action", nargs="?", default="start", choices=("start", "done", "skip", "fail"))
    s.add_argument("--note")
    s = sub.add_parser("set")
    s.add_argument("pairs", nargs="+")
    s.add_argument("--reason")
    sub.add_parser("tickets")
    s = sub.add_parser("ticket")
    s.add_argument("ids", nargs="+")
    s.add_argument("--note")
    c = sub.add_parser("commit")
    c.add_argument("target")
    c.add_argument("--tests")
    c.add_argument("--placeholder", action="append")
    c.add_argument("--also", action="append")
    c.add_argument("--repair", action="store_true")
    c.add_argument("--fewer-tests")
    c.add_argument("--external")
    g = sub.add_parser("gate")
    g.add_argument("gate", choices=("G2", "G4"))
    g.add_argument("--from", dest="src")
    s = sub.add_parser("add")
    s.add_argument("list")
    s.add_argument("items", nargs="+")
    s = sub.add_parser("contract")
    s.add_argument("id")
    s.add_argument("--from", dest="src")
    s = sub.add_parser("handoff")
    s.add_argument("id")
    s.add_argument("--note", required=True)
    s.add_argument("--tail")
    sub.add_parser("doctor")
    s = sub.add_parser("audit")
    s.add_argument("--accept", metavar="ID")
    s.add_argument("--note")
    for name in ("verify", "blind-clone"):
        s = sub.add_parser(name)
        s.add_argument("--use-real-env", action="store_true")  # copy the real .env (unread) instead of .env.example
    s = sub.add_parser("finish")
    s.add_argument("--result", required=True)
    s.add_argument("--not-accepted", action="store_true")
    sub.add_parser("reopen")
    s = sub.add_parser("serve")
    s.add_argument("--detach", action="store_true")
    sub.add_parser("url")
    sub.add_parser("web")
    h = sub.add_parser("hook")
    h.add_argument("event", choices=("pre-tool", "post-tool", "tool-failed", "agent-start", "agent-stop",
                                     "session-start", "prompt", "stop"))
    return p


TICKET_ACTIONS = ("start", "review", "repair", "retry", "done", "fail", "reset")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    a = build_parser().parse_args(argv)
    if a.cmd == "ticket":  # `ticket 02 03 start`: the action is the last positional
        if not a.ids or a.ids[-1] not in TICKET_ACTIONS or len(a.ids) < 2:
            raise Fail(f"ticket <id…> {'|'.join(TICKET_ACTIONS)}")
        a.action, a.ids = a.ids[-1], a.ids[:-1]
    root = init_root() if a.cmd == "init" else find_root()
    if a.cmd is None or a.cmd == "status":
        st = load(root) if state_path(root).exists() else None  # a broken or foreign state must say so, not look like "no run"
        print(status_text(root, st) if st else "No run. Start: /software-architector:build <idea>")
        return 0
    if a.cmd == "hook":
        return guard.run_hook(a.event)
    if a.cmd == "serve":
        if a.detach:
            print(start_server(root))
        else:
            serve(root)
        return 0
    if a.cmd == "url":
        print(dashboard_url(root) or "Dashboard is not running")
        return 0
    if a.cmd == "web":
        web(root)
        return 0
    if a.cmd == "init":
        cmd_init(root, a)
        return 0
    if a.cmd == "doctor":
        cmd_doctor(root)
        return 0
    if a.cmd == "verify":
        cmd_verify(root, a)
        return 0
    if a.cmd == "audit" and not a.accept:
        cmd_audit(root)
        return 0
    if a.cmd == "commit" and a.target != "plan":
        st = load(root)
        commit_preconditions(root, st, a)
        # may take minutes: outside the state lock, so other bv calls do not wait; but one check at a time, since two
        # checks in one working tree trip over each other's caches, builds and ports
        limit = int(st.get("checkTimeout") or CHECK_TIMEOUT)
        with locked(root, wait=30, stale=limit + 120, name="check",  # 540 s check + 30 s wait fit a 600 s Bash call
                    busy="another ticket's check is running: repeat `bv commit` in a minute"):
            # both gates run every time, and both are reported at once: one repair fixes everything they found
            red = None
            try:
                a.proof = project_check(root, load(root), a)
            except Fail as e:
                red = e
            fresh = load(root)
            a.audit, blocking = ticket_audit(root, fresh, find_ticket(fresh, a.target))
            if red or blocking:
                if blocking:  # the ids must outlive the refusal, so `bv audit --accept <id>` can find them
                    with locked(root):
                        fresh = load(root)
                        find_ticket(fresh, a.target)["audit"] = a.audit
                        save(root, fresh)
                parts = [str(red)] if red else []
                if blocking:
                    parts.append(audit_refusal(norm_tid(a.target), blocking))
                raise Fail("\n\n".join(parts))
    with locked(root):
        return _state_cmd(root, a)


def _state_cmd(root, a):
    st = load(root)
    if a.cmd == "handoff":
        cmd_handoff(root, st, a)
        return 0  # read-only: nothing to save
    if a.cmd == "reopen":
        cmd_reopen(root, st)
    elif a.cmd == "finish":
        cmd_finish(root, st, a)
        return 0  # finish saved and committed itself
    elif a.cmd == "stage":
        cmd_stage(root, st, a)
    elif a.cmd == "set":
        cmd_set(st, a.pairs, a.reason)
    elif a.cmd == "tickets":
        cmd_tickets(root, st)
    elif a.cmd == "ticket":
        cmd_ticket(root, st, a)
    elif a.cmd == "commit":
        cmd_commit(root, st, a)
    elif a.cmd == "contract":
        cmd_contract(root, st, a)
    elif a.cmd == "gate":
        cmd_gate(root, st, a)
    elif a.cmd == "add":
        cmd_add(st, a)
    elif a.cmd == "blind-clone":
        cmd_blind_clone(root, st, a)
    elif a.cmd == "audit":
        cmd_audit_accept(st, a)
    try:
        recount(root, st)
    except Fail as e:
        print(f"! {e}")
    save(root, st)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
