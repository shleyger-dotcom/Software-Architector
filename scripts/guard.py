"""Software-Architector hooks: hard run invariants that prompts alone cannot guarantee.

Active only while a run is open in the project (.software-architector/state.json, finishedAt empty, guard != off),
so the plugin never gets in the way of ordinary sessions. Block = exit 2 with the reason on stderr.
"""
import json
import os
import re
import shlex
import sys
import threading
import time
from pathlib import Path

SECRET_PATTERNS = [
    ("OpenAI/Anthropic key", r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"),
    ("Stripe key", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    ("AWS key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ("Google key", r"\b(?:AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]{20,})"),
    ("Slack token", r"\bxox[abpr]-[0-9A-Za-z-]{10,}"),
    ("Telegram bot token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # any provider: a long hex/base64 value right after a key/token/secret/password name; group 1 is what gets masked
    ("key next to a key/token/secret/password name",
     r"(?i)\b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|auth|access[_-]?key|private[_-]?key|credential)"
     r"[\w.-]*['\"]?\s*[:=]\s*['\"]?(?:Bearer\s+|Basic\s+)?([A-Za-z0-9+/_=-]{32,})"),
]
SECRET_RES = [(n, re.compile(p)) for n, p in SECRET_PATTERNS]
URL_CREDS = re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s:/@]+):([^\s@/]{3,})@([^\s/:]+)")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "db", "postgres", "redis", "mysql")
PLUGIN_DIR = Path(__file__).resolve().parents[1]

# Paths the orchestrator (main thread) may write inside the project.
MAIN_WRITABLE_DIRS = (".software-architector/", "docs/adr/")
MAIN_WRITABLE_FILES = ("AGENTS.md", "CLAUDE.md", ".gitignore", ".env.example", "CONTEXT.md", "GLOSSARY.md",
                       "docs/architecture.md")
# Written only by bv (state) and the hooks (journal), never through a tool: a forged gate or event would pass for proof.
PROTECTED_REL = re.compile(r"\.software-architector/(?:state\.[^/]*|live(?:/|$))", re.I)
PROTECTED_MENTION = re.compile(r"\.software-architector(?:[/\\]+(?:state\.|live\b)|[/\\]*(?![\w./\\-]))", re.I)
PROTECTED_WHY = ("Only bv and the hooks write state.json and the journal in .software-architector/live/: a forged gate or event "
                 "would pass for proof. Change the state with bv commands")
# Isolated checkers: they must judge the product by the brief, not by the run's own records.
ISOLATED = {"bv-blind": ("brief-",), "bv-coverage": ("brief-", "spec.md")}
# It reads the untrusted web: one output file, so text on a page cannot steer it into the project.
SINGLE_FILE = {"bv-research": "research.md", "bv-scout": "notes.md"}

GIT = r"\bgit(?:\.exe)?\s+"  # `git`, `/usr/bin/git`, `C:\…\git.exe`; any case: Windows finds GIT.EXE as well
REV = r"\s(?:(?:HEAD|@|ORIG_HEAD|FETCH_HEAD)[~^@{]|[0-9a-f]{7,40}\b)"  # a revision other than plain HEAD
BASH_DENY = [
    (GIT + r"(?:push|send-pack)\b", "git push is a publication: only the user does it"),
    (GIT + r"[^;&|\n]*?\balias\.", "git alias: an alias can hide a push or a history rewrite"),
    (r"\b(?:npm|pnpm|yarn|bun)(?:\.cmd|\.exe)?\s+publish\b|\b(?:cargo|twine|gem)\s+(?:publish|upload|push)\b|"
     r"\bdocker\s+push\b|\bgh(?:\.exe)?\s+(?:repo\s+create\b[^;&|\n]*--push|release\s+create)\b",
     "publishing a package or a repository: only the user does it"),
    (GIT + r"reset\b[^;&|\n]*(?:--(?:hard|soft|mixed|merge|keep)\b|" + REV + ")",
     "git reset with a revision moves the branch or destroys work (to unstage: git restore --staged <path>)"),
    (GIT + r"clean\b[^;&|\n]*\s-[a-zA-Z]*f", "git clean -f deletes files for good"),
    (GIT + r"stash\b", "git stash hides other changes: do not touch the user's working tree"),
    (GIT + r"(?:checkout|switch)\b[^;&|\n]*\s(?:-f|--force|--discard-changes)\b", "checkout -f discards changes in files"),
    (GIT + r"restore\b(?:(?![^;&|\n]*--staged)|(?=[^;&|\n]*(?:--worktree|\s-W\b|--source)))",
     "git restore discards changes in files"),
    (GIT + r"(?:rebase|filter-branch|filter-repo|update-ref|replace)\b", "git history rewrite"),
    (GIT + r"commit\b[^;&|\n]*--amend\b", "git commit --amend rewrites history"),
    (GIT + r"branch\b[^;&|\n]*\s(?-i:-D)\b", "git branch -D deletes a branch without a merge check"),
    (r"--no-verify\b", "--no-verify skips the repository checks"),
    (r"\b(?:pwsh|powershell)(?:\.exe)?\b[^;&|\n]*\s-(?:e|ec|en\w*)\s", "encoded PowerShell command: its text cannot be checked"),
]
BASH_DENY_RES = [(re.compile(p, re.I), why) for p, why in BASH_DENY]
# a real .env as a word of the command, in any role: `cat .env`, `grep K .env`, `open('.env')`, `cp .env x`, `< .env`
ENV_WORD = re.compile(r"(?:^|[\s/\\=(<:,])\.env(?:\.(?!example\b)[\w.-]+)?(?=$|[\s;&|)>,:])", re.I)

# Read-only agents: Bash stays for `git show` and test runs, but not for changing files or history.
READ_ONLY = ("bv-reviewer", "bv-scout")  # the scout writes notes.md with Write, its Bash only reads
# .NET file calls from PowerShell write without any command name: `[IO.File]::WriteAllText(...)`
DOTNET_WRITE = r"\[(?:System\.)?IO\.(?:File|Directory)\]::(?:Write|Append|Create|Copy|Move|Delete|Replace|Open)\w*"
# a command name is a token of its own: `src/rm.js`, `src/patch/`, `--patch`, `%cp` and `test:touch` are not commands
WRITE_CMD = re.compile(
    r"(?<![\w%:=.-])(?:tee|mv|cp|rm|rmdir|mkdir|touch|truncate|dd|patch|chmod|chown|install|ln|unlink|"
    r"copy|move|del|erase|ren|rd|md|xcopy|robocopy|ni|cpi|mi|ri|rni|clc)(?:\.exe)?(?![\w./-])|"
    r"\b(?:sed\s+-[a-zA-Z]*i|"
    r"git(?:\.exe)?\s+(?:add|commit|apply|am|merge|cherry-pick|checkout|switch|restore|reset|revert|rm|mv|pull|fetch)|"
    r"(?:npm|pnpm|yarn|bun)(?:\.cmd)?\s+(?:install|i|ci|add|update|up|remove|rm|uninstall|link)|"
    r"(?:pip3?|uv|poetry|cargo|go)\s+(?:install|add|get|sync|remove)|"
    r"Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item|Clear-Content)\b|" + DOTNET_WRITE,
    re.I)
# shortcut: a script file run by an interpreter (`python tool.py`) and text built at run time (variables, base64) are
# not parsed for writes; a rail against mistakes, not a sandbox
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n([\s\S]*?)\n\s*\2\b|@'\r?\n[\s\S]*?\n'@|@\"\r?\n[\s\S]*?\n\"@")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\[\s\S])*\"")
# a shell inside a shell: `bash -c "…"`, `pwsh -Command "…"`, `eval "…"`; a quoted string before it is skipped whole
NESTED = re.compile(QUOTED.pattern + r"|(?<![\w.-])(?:eval|Invoke-Expression|iex|cmd(?:\.exe)?\s+/[ck]|"
                    r"(?:bash|sh|zsh|pwsh|powershell)(?:\.exe)?(?:\s+-[\w-]+)*?"
                    r"\s+-(?:[a-z]*c|command))\s+(" + QUOTED.pattern + ")", re.I)
SHELL_FED = re.compile(r"(?:^|[;&|(])\s*(?:[^\s;&|]*[/\\])?(?:bash|sh|zsh)(?:\.exe)?\b")
# a quoted string is skipped whole (so `grep "touch"` and `node -e "a>b"` are not writes); a bare `>` yields its target
SHELL_TOKEN = re.compile(QUOTED.pattern + r"|(?<![>&<])>>?\s*(?!&)(?:\"([^\"]*)\"|'([^']*)'|([^\s;&|<>]+))")
INTERPRETER = re.compile(r"(?:^|[;&|(\n])\s*(?:[^\s;&|]*[/\\])?(?:node|nodejs|deno|bun|python3?|py|bash|sh|pwsh|powershell)"
                         r"(?:\.exe)?\b")
INLINE_WRITE = re.compile(r"writeFile|appendFile|createWriteStream|rmSync|unlinkSync|renameSync|write_text|write_bytes|"
                          r"shutil\.(?:copy|move|rmtree)|\bopen\([^)]*(?:,|mode\s*=)\s*['\"][wax+]|\.open\(\s*['\"][wax]|"
                          r"\bos\.(?:remove|unlink|rename|replace|makedirs|mkdir|rmdir|system)\b|"
                          r"\bfs\.(?:promises\.)?(?:unlink|rm|rmdir|mkdir|copyFile|cp|rename|truncate)\b")
NULL_SINKS = ("/dev/null", "nul")
HEAD_QUOTED = re.compile(r"((?:^|[;&|(\n])\s*)(" + QUOTED.pattern + ")")
CHECKOUT = re.compile(GIT + r"checkout\b([^;&|\n]*)", re.I)
BV_SET = re.compile(r"\bbv(?:\.cmd)?\s+set\b", re.I)
BV_SET_VALUES = (re.compile(r"(?:^|\s)(?:setupCmd|checkCmd|e2eCmd)=(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s\"']+)"),
                 re.compile(r"([\"'])(?:setupCmd|checkCmd|e2eCmd)=(.*?)\1"))
# what an executor may write outside its zone: dependency manifests it was allowed to change (`--also`) and build leftovers
DEP_FILES = {"package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
             "requirements.txt", "requirements-dev.txt", "pyproject.toml", "poetry.lock", "uv.lock", "pipfile",
             "pipfile.lock", "cargo.toml", "cargo.lock", "go.mod", "go.sum", "gemfile", "gemfile.lock", "composer.json",
             "composer.lock", ".env.example"}
ARTIFACT_DIRS = {"node_modules", "__pycache__", ".venv", "venv", "dist", "build", "coverage", ".pytest_cache",
                 ".mypy_cache", ".ruff_cache", ".next", ".cache", "target", ".tox", ".nyc_output", "htmlcov"}
# write commands whose path arguments are the files they write; cp-like ones write only their last argument
LAST_ARG = {"cp", "install", "ln", "copy", "xcopy", "robocopy", "copy-item", "cpi"}
ALL_ARGS = {"rm", "rmdir", "touch", "mkdir", "tee", "truncate", "del", "erase", "rd", "md", "unlink", "ren", "mv", "move",
            "move-item", "mi", "rename-item", "rni", "new-item", "ni", "remove-item", "ri", "set-content", "sc",
            "add-content", "ac", "out-file", "clear-content", "clc"}
PATH_OPTS = re.compile(r"-(?:path|literalpath|filepath|destination)$", re.I)
VALUE_OPTS = re.compile(r"-(?:value|encoding|itemtype|type|name|newname|filter|include|exclude|m|mode|t|target-directory)$",
                        re.I)
SKIP_HEADS = {"sudo", "command", "exec", "time", "nohup", "&", "env"}
MAIN_WRITE_CMD = re.compile(
    r"(?<![\w%:=.-])(?:cp|mv|rm|rmdir|touch|truncate|dd|ln|unlink|copy|move|del|erase|ren|rd|xcopy|robocopy|"
    r"ni|cpi|mi|ri|rni|clc)(?:\.exe)?(?![\w./-])|"
    r"\b(?:tee|sed\s+-[a-zA-Z]*i|Set-Content|Add-Content|Out-File|Clear-Content|New-Item|Copy-Item|Move-Item|"
    r"Remove-Item|Rename-Item|git(?:\.exe)?\s+(?:add|commit|apply|am|merge|cherry-pick|revert|rm|mv))\b|" + DOTNET_WRITE,
    re.I)
MAIN_NO_CODE = ("the Software-Architector orchestrator does not write project code ({}). Give the change to an executor "
                "(subagent software-architector:bv-executor) or, if the user explicitly asks for a manual change, "
                "run `bv set guard=off` and tell the user")


def redirect_targets(cmd):
    """Files a command redirects into (`> f`, `>> f`); heredoc bodies and quoted text are not redirects."""
    found = [m.group(1) or m.group(2) or m.group(3) for m in SHELL_TOKEN.finditer(HEREDOC.sub("", cmd))
             if m.group(0)[0] == ">"]
    return [t for t in found if t and t.lower() not in NULL_SINKS]


def bare(cmd):
    # `"cp" a b` and `& "git" push` run cp and git: a quoted command name stays a name, other quoted text goes
    cmd = HEAD_QUOTED.sub(lambda m: m.group(1) + m.group(2)[1:-1], HEREDOC.sub("", cmd))
    return QUOTED.sub('""', cmd)


def dequote(cmd):
    """`"git" push`, `g'i't push`, PowerShell's g`it and cmd's g^it all run as `git push`: match the text they run."""
    return re.sub(r"[\"'`^]", "", cmd)


def writes(cmd):
    """The command changes files somehow: a write command, a redirect or an interpreter writing inline."""
    b = bare(cmd)
    return bool(WRITE_CMD.search(b) or MAIN_WRITE_CMD.search(b) or redirect_targets(cmd) or inline_write(cmd))


def check_protected(cmd, root):
    if writes(cmd) and PROTECTED_MENTION.search(dequote(cmd)):
        return PROTECTED_WHY
    for t in redirect_targets(cmd):
        p = Path(re.sub(r"^/([A-Za-z])/", r"\1:/", t))
        rel = rel_to(root, p if p.is_absolute() else root / p)
        if rel and PROTECTED_REL.match(rel):
            return PROTECTED_WHY
    return None


def inline_write(cmd):
    return bool(INTERPRETER.search(bare(cmd)) and INLINE_WRITE.search(cmd))


def check_checkout(text):
    """`git checkout <rev> <path>` and `git checkout <path>` overwrite files; switching or creating a branch does not."""
    for m in CHECKOUT.finditer(text):
        args = m.group(1).split()
        if {"-b", "-B", "--orphan"} & set(args):
            continue
        pos = [a for a in args if not a.startswith("-")]
        if "--" in args or len(pos) >= 2 or any(a in (".", "*") or "*" in a or a.startswith(":")
                                                 or re.search(r"\.\w{1,5}$", a) for a in pos):
            return "git checkout <revision> <path> / -- / . replaces files and discards changes"
    return None


def deny_reason(cmd):
    """Irreversible or outward commands, matched as written and as the shell runs them (quotes and escapes removed)."""
    forms = {cmd, dequote(cmd)}
    forms |= {GIT_GLOBAL_OPTS.sub("git ", f) for f in forms}  # `git -C . push` is still a push
    for rx, why in BASH_DENY_RES:
        if any(rx.search(f) for f in forms):
            return why
    for f in forms:
        why = check_checkout(f)
        if why:
            return why
    if not is_bv(cmd) and ENV_WORD.search(dequote(cmd)):
        return "reading .env: secrets are not read, copied or repeated; work with variable names"
    return None


def is_bv(cmd):
    """One call of the plugin's own launcher with nothing chained to it: `bv …` or "<plugin>/bin/bv" …."""
    b = bare(cmd).strip()
    words = b.split(None, 1)
    if not words or re.search(r"[;&|`\n<>]|\$\(", b):
        return False
    quoted = QUOTED.match(HEREDOC.sub("", cmd).strip())  # "C:/My Plugins/bin/bv": a path with spaces is one word
    head = os.path.expandvars(quoted.group()[1:-1] if quoted else words[0]).replace("\\", "/")
    head = re.sub(r"^/([A-Za-z])/", r"\1:/", head)
    if head in ("bv", "bv.cmd"):
        return True
    launcher = PLUGIN_DIR / "bin" / "bv"
    return any(same_path(head, str(p)) for p in (launcher, launcher.with_suffix(".cmd")))


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def fold(p):
    return p.lower() if os.name == "nt" else p


def zone_path(z):
    z = z.replace("\\", "/").strip()
    while z.startswith("./"):
        z = z[2:]
    return z.rstrip("/")


def in_zone(rel, zone):
    r = fold(zone_path(rel))
    return any(r == fold(zone_path(z)) or r.startswith(fold(zone_path(z)) + "/") for z in zone)


def resolve(base, t):
    """A path as the shell would open it; None when a variable in it is unknown here."""
    t = os.path.expanduser(os.path.expandvars(re.sub(r"^/([A-Za-z])/", r"\1:/", t)))
    if re.search(r"\$|%\w+%", t):
        return None
    p = Path(t)
    return p if p.is_absolute() else base / p


def write_targets(cmd, root):
    """Paths a command writes: redirect targets and path arguments of plain write commands, `cd` followed.
    shortcut: arguments only, not stdin (`xargs rm`) or a script's own writes; a rail against mistakes, not a parser."""
    out, base = [], root
    for seg in re.split(r"&&|\|\||[;&|\n]", HEREDOC.sub("", cmd)):
        out += [resolve(base, t) or Path(t) for t in redirect_targets(seg)]  # unknown `$X/f` stays as written
        try:
            words = shlex.split(seg.replace("\\", "/"))
        except ValueError:
            words = seg.split()
        while words and (words[0] in SKIP_HEADS or re.match(r"\w+=", words[0])):
            words = words[1:]
        if not words:
            continue
        name = re.sub(r"\.(?:exe|cmd)$", "", words[0].rsplit("/", 1)[-1].lower())
        args = []
        for w in words[1:]:
            if re.search(r"[<>]", w):
                break
            args.append(w)
        if name == "cd" and args:
            base = resolve(base, args[0]) or base
            continue
        paths, pos, skip = [], [], False
        for i, w in enumerate(args):
            if skip:
                skip = False
            elif PATH_OPTS.match(w) and i + 1 < len(args):
                paths.append(args[i + 1])
                skip = True
            elif VALUE_OPTS.match(w):
                skip = True
            elif w.startswith("-") or w.startswith("/") and name in ("copy", "move", "del", "erase", "xcopy", "robocopy"):
                continue
            elif name == "dd":
                paths += [w[3:]] if w.startswith("of=") else []
            else:
                pos.append(w)
        if name in ("sed",) and re.search(r"\s-[a-zA-Z]*i", seg):
            paths += pos[1:]
        elif name == "git" and pos[:1] in (["rm"], ["mv"]):
            paths += pos[1:]
        elif name in LAST_ARG:
            paths += [] if paths else pos[-1:] if len(pos) > 1 else []
        elif name in ALL_ARGS:
            paths += pos[:1] if name in ("set-content", "sc", "add-content", "ac", "out-file") else pos
        out += [resolve(base, p) or Path(p) for p in paths]
    return out


def agent_ticket(root, st, agent_id):
    """The ticket an agent works on: the number in the description of the Agent call that spawned it (journal)."""
    if not agent_id or not st.get("dir"):
        return None
    import events  # lazy: only executor writes need the journal
    path = events.live_dir(root, st) / "agents" / "main.jsonl"
    want = events.AGENT_ID.sub("_", agent_id)
    try:
        lines = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"spawned"' in line:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("spawned") == want:
                return ticket_ref(e.get("s"))
    return None


TICKET_REF = re.compile(r"(?:task\w*|ticket\w*)\s*#?\s*(F?\d+)\b|\bT(F?\d+)\b", re.I)


def ticket_ref(text):
    """`Ticket 02: bot`, `Review of task 2`, `T02` → "02"; `F1` → "F1"."""
    m = TICKET_REF.search(text or "")
    if not m:
        return None
    x = m.group(1) or m.group(2)
    return x.zfill(2) if x.isdigit() else x.upper()


def check_executor(root, st, data, paths):
    """An executor writes its ticket's zone (plus .env.example, dependency manifests, build leftovers), never the run's records."""
    zone = None
    for p in paths:
        rel = rel_to(root, p)
        if rel is None:
            continue  # outside the project: temp files; the plugin and ~/.claude are refused elsewhere
        low = rel.lower()
        if low == ".software-architector" or low.startswith(".software-architector/"):
            return "an executor does not write in .software-architector/: the orchestrator and bv keep the run records"
        if low.rsplit("/", 1)[-1] in DEP_FILES or ARTIFACT_DIRS & set(low.split("/")):
            continue
        if zone is None:
            flying = [t for t in st.get("tickets") or [] if t.get("status") in ("in-progress", "repair")]
            tid = agent_ticket(root, st, data.get("agent_id"))
            mine = [t for t in flying if t.get("id") == tid] if tid else flying
            zone = [z for t in mine for z in t.get("zone") or []]
        if not in_zone(rel, zone):
            return (f"an executor writes only in the zone of its ticket ({', '.join(zone) or 'no ticket in flight'}) "
                    f"and in .env.example; {rel} is outside it. If a change outside the zone is needed, return BLOCKED with the reason")
    return None


OUTSIDE_WHY = ("during a run, writing to the plugin itself and to Claude Code settings and memory (~/.claude/settings*.json, "
               "~/.claude/CLAUDE.md, the project's .claude/settings*.json) is forbidden: that is how the guard hooks get removed")
CLAUDE_FILE = re.compile(r"(?:^|[/\\])\.claude[/\\](?:settings[^/\\]*\.json|claude\.md)$", re.I)


def is_under(p, top):
    try:
        p.relative_to(top)
        return True
    except ValueError:
        return False


def outside_protected(path, root):
    """The plugin, Claude Code's settings and the user's global memory; an unresolvable path is refused too."""
    try:
        p = Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return True
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    tops = [PLUGIN_DIR] + ([Path(env_root).resolve()] if env_root else [])
    if any(is_under(p, t) for t in tops):
        return True
    if not CLAUDE_FILE.search(str(p)):
        return False
    rel = rel_to(root, p) if root else None
    return rel is not None and rel.lower().startswith(".claude/") or same_path(str(p.parent), str(Path.home() / ".claude"))


def check_outside(paths, root):
    for p in paths:
        if outside_protected(p, root) or CLAUDE_FILE.search(str(p)) and "$" in str(p):
            return OUTSIDE_WHY
    return None


def mentions_root(text, root):
    """The project folder named in a tool input, as C:/x, C:\\x or Git Bash's /c/x, any case."""
    p = root.as_posix().lower()
    forms = {p} | ({"/" + p[0] + p[2:]} if re.match(r"[a-z]:/", p) else set())
    t = text.lower().replace("\\\\", "/").replace("\\", "/")
    return any(re.search(re.escape(f) + r"(?![\w.-])", t) for f in forms)


def check_isolated(agent, tool, ti, root, cwd):
    """Independent checkers judge by the brief and the product, never by the run's records (any case, any glob)."""
    blob = json.dumps(ti, ensure_ascii=False).replace("\\\\", "/")
    brief = tool == "Read" and Path(str(ti.get("file_path", ""))).name.startswith(ISOLATED[agent])
    closed = f"{agent}: .software-architector/ is closed to an independent check; judge by the brief and the product"
    if brief:
        return None
    if ".software-architector" in blob.lower():
        return closed
    if tool in ("Grep", "Glob"):
        start = Path(ti.get("path") or cwd)
        start = start if start.is_absolute() else cwd / start
        if is_under(root, start.resolve()):  # the project root or above it: the search walks into the records
            return closed + "; search in a folder below the root" + (" (in your own clone)" if agent == "bv-blind" else "")
    if agent == "bv-blind" and (mentions_root(blob, root) or tool in ("Read", "Grep", "Glob")
                                and rel_to(root, ti.get("file_path") or ti.get("path") or cwd) is not None):
        return "bv-blind works only in the clone made by `bv blind-clone`, not in the project working folder"
    return None


def check_blind(st, targets, cmd):
    """bv-blind installs and runs the product in its clone; it writes nowhere else."""
    clone = st.get("blindClone")
    outside = [p for p in targets if not (clone and is_under(Path(p).resolve(), Path(clone).resolve()))]
    if outside or inline_write(cmd):
        return (f"bv-blind writes only inside its own clone ({clone or 'there is no clone: `bv blind-clone`'}); "
                f"{outside[0] if outside else 'a write from an interpreter'} is outside it")
    return None


def check_bv_set(cmd, root):
    """`bv set checkCmd="…"` hands the text to bv to run later: the same rails apply to it now."""
    if not BV_SET.search(dequote(cmd)):
        return None
    values = [m.group(1) for m in BV_SET_VALUES[0].finditer(cmd)] + [m.group(2) for m in BV_SET_VALUES[1].finditer(cmd)]
    for v in values:
        if v[:1] in "\"'" and v[-1:] == v[:1]:
            v = v[1:-1].replace('\\"', '"')
        v = unnest(v)
        why = deny_reason(v) or check_main_bash(v, root) or check_protected(v, root)
        if why:
            return f"command for bv set: {why}"
    return None


def without_data(cmd):
    """A heredoc body is data (file text, a contract) unless it feeds an interpreter: there it is code."""
    def keep(m):
        line = cmd[cmd.rfind("\n", 0, m.start()) + 1:m.start()]
        return m.group(0) if INTERPRETER.search(line) else ""
    return HEREDOC.sub(keep, cmd)


def unnest(cmd, depth=3):
    """`bash -c "…"`, `eval "…"` and `bash <<E … E` run their text as commands: append it, so the checks see inside."""
    inner = [q[1:-1].replace('\\"', '"') if q[0] == '"' else q[1:-1]
             for q in (m.group(1) for m in NESTED.finditer(cmd)) if q]
    for m in HEREDOC.finditer(cmd):
        if m.group(3) and SHELL_FED.search(cmd[cmd.rfind("\n", 0, m.start()) + 1:m.start()]):
            inner.append(m.group(3))
    if not inner or depth == 0:
        return cmd
    return cmd + "\n" + unnest("\n".join(inner), depth - 1)


def check_read_only(cmd):
    if WRITE_CMD.search(bare(cmd)) or redirect_targets(cmd) or inline_write(cmd):
        return "this agent (reviewer, scout) only reads: Bash without file writes and without git changes (git show/diff/log, test runs, node -e)"
    return None


def check_agent_commit(cmd):
    """A commit of a subagent's own leaves `bv commit` nothing to commit: the ticket could never be closed."""
    if re.search(r"\bgit\s+commit\b", bare(cmd)):
        return ("only the orchestrator commits: `bv commit` runs the check itself and commits the ticket zone. "
                "Do not commit, return the contract")
    return None


def check_main_bash(cmd, root):
    """The orchestrator keeps the books; project code is written by executors (rule 5), also through the shell."""
    why = MAIN_NO_CODE.format("through the shell") + "; `bv commit` commits a ticket"
    if MAIN_WRITE_CMD.search(bare(cmd)) or inline_write(cmd):
        return why
    for t in redirect_targets(cmd):
        t = re.sub(r"^/([A-Za-z])/", r"\1:/", t)
        if t.startswith(("$", "~", "%")):
            continue  # $TEMP and the like: outside the project
        p = Path(t)
        rel = rel_to(root, p if p.is_absolute() else root / p)
        if rel is not None and not (rel.startswith(MAIN_WRITABLE_DIRS) or rel in MAIN_WRITABLE_FILES):
            return why
    return None


def find_secret(text):
    if not text:
        return None
    for name, rx in SECRET_RES:
        if rx.search(text):
            return name
    for m in URL_CREDS.finditer(text):
        user, pw, host = m.groups()
        if host.lower() not in LOCAL_HOSTS and not pw.startswith(("$", "{", "<", "%")):
            return "password in a URL"
    return None


def journal_secret(path, cap=4_000_000):
    """Masking on read, the fallback when a log could not be rewritten masked (a survivor held it on Windows)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return find_secret(f.read(cap))
    except OSError:
        return None


PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")


def mask_secrets(text):
    """The same patterns as find_secret, replaced by their name: the journal keeps the shape, never the value."""
    if not text:
        return text
    text = PRIVATE_KEY_BLOCK.sub("[private key hidden]", text)
    for name, rx in SECRET_RES:
        # a pattern with a group hides only the value: `API_KEY=[… hidden]` keeps the name readable
        text = rx.sub(lambda m, n=name: m.group(0)[:m.start(1) - m.start(0)] + f"[{n} hidden]" if m.re.groups
                      else f"[{n} hidden]", text)

    def url(m):
        user, pw, host = m.groups()
        if host.lower() in LOCAL_HOSTS or pw.startswith(("$", "{", "<", "%")):
            return m.group(0)
        return m.group(0).replace(f":{pw}@", ":[password hidden]@", 1)
    return URL_CREDS.sub(url, text)


def project_root(data):
    for start in (data.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if not start:
            continue
        p = Path(start).resolve()
        for d in (p, *p.parents):
            if (d / ".software-architector" / "state.json").exists():
                return d
    return None


def read_state(path, tries=40):
    """state.json as a dict. On Windows a reader and a writer's os.replace can refuse each other for a moment: retry."""
    for attempt in range(tries):
        try:
            text = Path(path).read_text("utf-8")
            break
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.05)
    st = json.loads(text)
    if not isinstance(st, dict):
        raise ValueError("a JSON object was expected")
    return st


def active_state(root):
    if root is None:
        return None
    try:
        st = read_state(root / ".software-architector" / "state.json")
    except Exception as e:
        return {"broken": str(e)}  # a run is open and its rails must not vanish silently: run_hook fails closed
    if st.get("finishedAt") or st.get("guard") == "off":
        return None
    return st


def check_broken(data, st):
    """state.json is unreadable: reading and `bv` (to see and repair it) stay possible, changing the project does not."""
    tool = data.get("tool_name", "")
    if tool in ("Read", "Grep", "Glob", "AskUserQuestion"):
        return None
    # only the launcher itself, nothing chained: `bv status; rm -rf src` or `echo bv > x` is not a bv call
    if tool in ("Bash", "PowerShell") and is_bv(without_data((data.get("tool_input") or {}).get("command", ""))):
        return None
    return (f"state.json is damaged ({st['broken']}); the run guard cannot work, so changes are blocked. "
            f"Show this to the user: `bv status` names the error, `bv init --force` starts the run again: the damaged "
            f"file is kept as .software-architector/state.broken-<time>.json, the folders of earlier runs stay")


def rel_to(root, path):
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None  # outside the project: scratchpad, ~/.claude memory, temp clones


def is_env_file(name):
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def written_text(tool, ti):
    if tool == "Write":
        return ti.get("content", "")
    if tool == "Edit":
        return ti.get("new_string", "")
    if tool == "MultiEdit":
        return "\n".join(e.get("new_string", "") for e in ti.get("edits", []))
    if tool == "NotebookEdit":
        return ti.get("new_source", "")
    return ""


def check_rm(cmd):
    for m in re.finditer(r"\brm(?:\.exe)?\s+([^;&|\n]+)", cmd, re.I):
        args = m.group(1).split()
        flags = "".join(a for a in args if a.startswith("-"))
        if "r" not in flags.lower():
            continue
        for t in (a.strip("'\"") for a in args if not a.startswith("-")):
            if t in ("/", "~", "*", ".", "..") or t.startswith(("/", "~", "..", "$HOME")) or re.match(r"[A-Za-z]:", t):
                return f"rm -r outside the project ({t}): delete only inside the project"
    if re.search(r"\b(?:Remove-Item|ri)\b[^;|\n]*-Recurse[^;|\n]*\s(?:[A-Za-z]:|~|\\|/|\.\.)", cmd, re.I):
        return "Remove-Item -Recurse outside the project"
    return None


GIT_GLOBAL_OPTS = re.compile(r"\bgit(?:\.exe)?\s+(?:(?:-c|--git-dir|--work-tree|--namespace|--exec-path|--config-env)"
                             r"(?:\s+|=)\S+\s+|--[\w-]+(?:=\S+)?\s+|-p\s+)+", re.I)
WHOLE_TREE = {".", "./", "*", ":/", "/", ":"}


def check_git_add(cmd):
    """`git add -A`, `git add .`, `git add -A -- .`, `git add :/`: the whole tree, never one ticket's files."""
    for text in dict.fromkeys((cmd, dequote(cmd))):
        for m in re.finditer(GIT + r"add\b([^;&|\n]*)", text, re.I):
            args = m.group(1).split()
            spec = [a for a in args if not a.startswith("-")]
            if (any(a in WHOLE_TREE or a.startswith((":(", ":/")) for a in spec)
                    or not spec and {"-A", "--all", "-u", "--update"} & set(args)):
                return "git add without explicit paths: `bv commit <ticket> --tests P/F` commits a ticket"
    return None


def decide(data, root, st):
    """Return a deny reason or None."""
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    agent = (data.get("agent_type") or "").split(":")[-1]
    main_thread = not data.get("agent_id")

    if agent in ISOLATED:
        why = check_isolated(agent, tool, ti, root, Path(data.get("cwd") or root))
        if why:
            return why

    if tool.startswith("mcp__"):
        why = check_mcp(tool, ti)
        if why:
            return why
    if tool in ("Agent", "Task") and main_thread:
        kind = str(ti.get("subagent_type") or "")
        if not kind.startswith("software-architector:bv-"):
            return (f"during a run the orchestrator starts only software-architector:bv-* agents (not '{kind or 'general-purpose'}'): "
                    f"bv-executor writes code in the ticket zone. If another agent is needed, run `bv set guard=off` with the user's consent")
    if not main_thread and agent not in BV_AGENTS:  # an agent from outside the run: it may look, never change anything
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return f"agent '{agent or '?'}' is not from the run (Software-Architector): during a run it only reads"
        if tool in ("Bash", "PowerShell"):
            why = check_read_only(unnest(without_data(ti.get("command", ""))))
            if why:
                return f"agent '{agent or '?'}' is not from the run: {why}"

    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if not path or outside_protected(path, root):
            return OUTSIDE_WHY if path else "a write without a file path"
        name = Path(path).name
        if agent in SINGLE_FILE and name != SINGLE_FILE[agent]:
            return f"{agent} writes only {SINGLE_FILE[agent]} in the run folder"
        hit = find_secret(written_text(tool, ti))
        if hit and not is_env_file(name):
            return (f"the text to write looks like a secret ({hit}). Secrets are not written to files: "
                    f"leave the environment variable name and an empty value in .env.example")
        rel = rel_to(root, path) if path else None
        if rel and PROTECTED_REL.match(rel):
            return PROTECTED_WHY
        if main_thread and rel is not None and not (rel.startswith(MAIN_WRITABLE_DIRS) or rel in MAIN_WRITABLE_FILES):
            return MAIN_NO_CODE.format(rel)
        if agent == "bv-executor" and path:
            why = check_executor(root, st, data, [Path(path) if Path(path).is_absolute() else root / path])
            if why:
                return why

    if tool == "Read" and is_env_file(Path(str(ti.get("file_path", ""))).name):
        return "reading .env is forbidden: secrets are not read or repeated; work with variable names"
    if tool == "Read" and (rel_to(root, ti.get("file_path") or "") or "").lower().startswith(".software-architector/live/"):
        hit = journal_secret(ti["file_path"])
        if hit:
            return (f"the journal file looks like it holds a secret ({hit}): its masked copy could not be written, so it is not "
                    f"read. Read the masked copy next to it (*.masked.txt)")

    if tool in ("Grep", "Glob"):
        target = f'{ti.get("path", "")} {ti.get("glob", "")} {ti.get("pattern", "") if tool == "Glob" else ""}'
        if any(is_env_file(Path(t.replace("\\", "/")).name) for t in target.split()):
            return "searching .env is forbidden: secrets are not read or repeated; work with variable names"

    if tool in ("WebSearch", "WebFetch"):
        hit = find_secret(" ".join(str(v) for v in ti.values()))
        if hit:
            return (f"the web request looks like it holds a secret ({hit}). Secrets do not leave the machine: "
                    f"search by the technology name, without key values")

    if tool in ("Bash", "PowerShell"):
        raw = unnest(without_data(ti.get("command", "")))
        why = deny_reason(raw)
        if why:
            return f"{why}. If this is really needed, ask the user: the user runs it"
        cmd = GIT_GLOBAL_OPTS.sub("git ", raw)  # `git -C . add -A` is still `git add -A`
        targets = write_targets(cmd, root)
        why = check_protected(cmd, root) or check_outside(targets, root)
        if why:
            return why
        if agent == "bv-blind":
            why = check_blind(st, targets, cmd)
            if why:
                return why
        if agent == "bv-executor":
            why = check_executor(root, st, data, targets) or (
                "an executor writes files with the Write/Edit tools in its zone: a write from an interpreter cannot be checked"
                if inline_write(cmd) else None)
            if why:
                return why
        return (check_git_add(cmd) or check_rm(cmd)
                or (check_read_only(cmd) if agent in READ_ONLY else None)
                or (check_main_bash(cmd, root) or check_bv_set(raw, root) if main_thread else check_agent_commit(cmd)))
    return None


BV_AGENTS = {"bv-executor", "bv-reviewer", "bv-blind", "bv-coverage", "bv-scout", "bv-research", "bv-memory", "bv-adr"}
# verbs of an MCP tool that change something or reach outside: during a run only the user decides those (rule 4)
MCP_WRITE_VERBS = {"write", "edit", "create", "update", "delete", "remove", "move", "rename", "upload", "push", "commit",
                   "insert", "send", "post", "publish", "deploy", "execute", "exec", "run", "merge", "replace", "append",
                   "set", "put", "patch", "add", "save", "apply", "cancel", "restore", "duplicate", "upsert", "drop"}
WRITE_CLASS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell", "Agent", "Task")
HOOK_BUDGET = 10  # seconds: the hook's own timeout is 15, and on a timeout Claude Code lets the tool run
# bv's escape hatches: each weakens a check, so the user lets it through (Claude Code shows them the call), not the model.
# The third field is the rule in mode full, where nobody is asked: ask anyway, deny, or pass (decided and reported)
USER_ONLY = [(re.compile(p, re.I), what, full) for p, what, full in (
    (r"\bguard=off\b", "switches off the run guard and its journal", "ask"),
    (r"\binit\b.*\s--force\b", "abandons the open run and starts a new one", "ask"),
    (r"\s--use-real-env\b", "copies the user's real .env into a clone", "deny"),
    (r"\s--extra-round\b", "opens one more repair round past the limit", "deny"),
    (r"\s--reason\b", "changes a check command frozen since the build started", "pass"),
    (r"\s--fewer-tests\b", "lets the project's test count drop", "pass"),
)]
BV_WORD = re.compile(r"\bbv\b", re.I)


def user_only(data, st):
    """("ask" | "deny", reason) for a bv call that opens an escape hatch; None for anything else."""
    if data.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    cmd = dequote(unnest(without_data((data.get("tool_input") or {}).get("command", ""))))
    if not BV_WORD.search(cmd):
        return None
    for rx, what, full in USER_ONLY:
        if not rx.search(cmd):
            continue
        if data.get("agent_id"):
            return "deny", f"this bv call {what}: only the orchestrator runs bv, and only the user allows this"
        if st.get("mode") == "full" and full == "pass":
            continue
        if st.get("mode") == "full" and full == "deny":
            return "deny", (f"this bv call {what}: that needs the user's consent, and mode full asks nobody. "
                            f"Close honestly (`bv finish --not-accepted`) or ask the user to switch the mode")
        return "ask", (f"Software-Architector: this bv call {what}. Allow it only if you asked for it yourself; "
                       f"the run report names it")
    return None


def check_mcp(tool, ti):
    hit = find_secret(json.dumps(ti, ensure_ascii=False))
    if hit:
        return f"the input of the MCP tool looks like it holds a secret ({hit}): secrets do not leave the machine"
    name = tool.split("__", 2)[-1]
    words = set(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower()))
    if words & MCP_WRITE_VERBS:
        return (f"the MCP tool {tool} changes data or acts outside: during a run the user decides this "
                f"(rule 4); only bv-executor writes project code. Ask the user")
    return None


def decide_in_time(data, root, st):
    """decide() within the hook budget; a crash or an overrun denies a write-class tool instead of letting it pass."""
    box = {}

    def work():
        try:
            box["why"] = decide(data, root, st)
        except Exception as e:  # noqa: BLE001 — any bug of the guard must not open the gate
            box["error"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(HOOK_BUDGET)
    if "why" in box:
        return box["why"]
    tool = data.get("tool_name", "")
    if tool in WRITE_CLASS or tool.startswith("mcp__"):
        cause = "timed out" if t.is_alive() else f"crashed: {box.get('error')!r}"
        return f"the guard check {cause}, so the write call was not let through. Retry; if it repeats, run `bv doctor`"
    return None


def journal(event, data, root, st):
    try:
        import events  # lazy: only hooks of an active run write the journal
        events.record(event, data, root, st)
    except Exception as e:  # the journal is a view: a failure there never blocks or breaks the run
        print(f"Software-Architector: the journal was not written: {e}", file=sys.stderr)


def utf8_stdio():
    """cp1251/cp1252 console and pipes on Windows; PowerShell pipes a BOM first."""
    for s, enc, errors in ((sys.stdin, "utf-8-sig", "replace"), (sys.stdout, "utf-8", None), (sys.stderr, "utf-8", None)):
        try:
            s.reconfigure(encoding=enc, errors=errors)
        except Exception:
            pass


def run_hook(event):
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0  # malformed input: never break the session on our side
    root = project_root(data)
    st = active_state(root)
    if st is None:
        return 0
    if st.get("broken"):
        reason = check_broken(data, st) if event == "pre-tool" else None
        if reason:
            print(f"Software-Architector: {reason}", file=sys.stderr)
            return 2
        return 0
    if event == "pre-tool":
        reason = decide_in_time(data, root, st)
        hatch = None if reason else user_only(data, st)
        if hatch and hatch[0] == "ask":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                     "permissionDecisionReason": hatch[1]}}, ensure_ascii=False))
            return 0
        reason = reason or (hatch and hatch[1])
        if reason:
            journal("deny", {**data, "reason": reason}, root, st)
            print(f"Software-Architector: {reason}", file=sys.stderr)
            return 2
        if data.get("tool_name") == "AskUserQuestion" and not data.get("agent_id"):
            journal("wait", data, root, st)
    elif event in ("post-tool", "tool-failed", "agent-start", "agent-stop"):
        journal(event, data, root, st)
    elif event == "stop":
        # paused for its own background work, not for the user; a monitor (the dashboard) never finishes: it is not work
        if not [t for t in data.get("background_tasks") or [] if not (isinstance(t, dict) and t.get("type") == "monitor")]:
            journal("wait", data, root, st)
    elif event == "prompt":
        # only what a person typed is held back: a machine turn (an agent's result) blocked here would stall the run
        hit = find_secret(data.get("prompt", "")) if data.get("source") in (None, "user", "sdk") else None
        if hit:
            print(json.dumps({"decision": "block", "reason": (
                f"Software-Architector: the message looks like it holds a secret ({hit}); it was not sent to the model. "
                f"Send it again without the value: give only the variable name (for example TELEGRAM_TOKEN) "
                f"and put the value into .env yourself. If the key was exposed anywhere, revoke it at the provider.")},
                ensure_ascii=False))
        else:
            journal("resume", data, root, st)
    elif event == "session-start":
        import bv  # lazy: avoids a cycle at import time
        text = bv.status_text(root, st, with_url=False)
        skill = bv.PLUGIN_ROOT / "skills" / "build"
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": (
            "A Software-Architector run is open in this project (plugin software-architector).\n" + text +
            f"\nThis session is its orchestrator, but the phase rules are not loaded yet. For any user request about this "
            f"project (\"continue\", a change, \"commit\", \"do a review\", a question about progress), before anything else read "
            f"{skill.as_posix()}/SKILL.md and follow it: there are no arguments, so use the Resume section; the state is above; the "
            f"phase files are in {skill.as_posix()}/phases/; the bv command is \"{bv.PLUGIN_ROOT.as_posix()}/bin/bv\". "
            f"The user does not need to run /software-architector:build.")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # bin/bv runs hooks here: they fire on every tool call and skip the imports of the CLI
    utf8_stdio()
    sys.exit(run_hook(sys.argv[1] if len(sys.argv) > 1 else ""))
