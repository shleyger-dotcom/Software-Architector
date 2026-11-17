"""Software-Architector audit: a mechanical pass for junk code and security holes, input for the branch review.

Heuristic, not a SAST: line patterns, no data flow across files; bv-reviewer drops false positives with a reason.
Dead code and dangerous calls: only the files the run changed. Secrets and tracked key files: the whole tree.
Dependency scanners run only when installed; one that did not run is "not checked", never "clean".
Pure functions: reading project files is the only I/O, besides the scanner processes of run_tools.
"""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import guard

MAX_BYTES = 1_000_000  # bigger files are data or generated code, not something the run wrote by hand
TOOL_TIMEOUT = 120
SEVERITIES = ("blocker", "high", "medium", "low")
BLOCKING = ("blocker", "high")
PY = {".py"}
JS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}
GENERAL = PY | JS | {".sh", ".bash", ".ps1", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".json",
                     "dockerfile", "makefile", "procfile"}
WORD = re.compile(r"[A-Za-z_$][\w$]*")

# (rule, severity, languages, pattern, also in comment lines)
RULES = [(r, s, langs, re.compile(p), c) for r, s, langs, p, c in [
    ("eval", "high", PY | JS, r"(?<![\w.$])eval\s*\(", False),
    ("exec", "high", PY, r"(?<![\w.])exec\s*\(", False),
    ("os-system", "high", PY, r"\bos\.system\s*\(", False),
    ("shell-true", "high", PY, r"\bshell\s*=\s*True\b", False),
    ("pickle", "high", PY, r"\b(?:c?pickle|dill)\.loads?\s*\(", False),
    ("yaml-load", "high", PY, r"\byaml\.load\s*\((?![^)]*Loader\s*=\s*(?:yaml\.)?C?(?:Safe|Base)Loader)", False),
    ("tls-off", "high", PY, r"\bverify\s*=\s*False\b", False),
    ("weak-hash", "medium", PY, r"\bhashlib\.(?:md5|sha1)\s*\((?![^)]*usedforsecurity\s*=\s*False)", False),
    ("debug-on", "medium", PY, r"(?i)\bdebug\s*=\s*True\b", False),
    ("sql-concat", "high", PY, r"\.execute(?:many)?\s*\(\s*(?:[rb]?f['\"]|(['\"])[^'\"]*\1\s*(?:%|\+|\.format\b))", False),
    ("new-function", "high", JS, r"\bnew\s+Function\s*\(", False),
    ("inner-html", "high", JS, r"\.(?:inner|outer)HTML\s*\+?=(?!=)", False),
    ("dangerous-html", "high", JS, r"\bdangerouslySetInnerHTML\b", False),
    ("document-write", "high", JS, r"\bdocument\.write(?:ln)?\s*\(", False),
    ("exec-template", "high", JS, r"\bexec(?:Sync)?\s*\(\s*`[^`]*\$\{", False),
    ("tls-off", "high", JS, r"\brejectUnauthorized\s*:\s*false\b|\bNODE_TLS_REJECT_UNAUTHORIZED\b\s*=\s*['\"]?0", False),
    ("weak-random", "high", JS, r"(?i)(?=.*\b\w*(?:token|secret|passw|nonce|salt|otp)\w*\b).*\bMath\.random\s*\(", False),
    ("sql-template", "high", JS, r"\.(?:query|execute|raw)\s*\(\s*`[^`]*\$\{", False),
    ("cors-any", "medium", PY | JS,
     r"(?i)access-control-allow-origin['\"]?\s*[:,=]\s*['\"]\*|\b\w*origins?['\"]?\s*[:=]\s*\[?\s*['\"]\*['\"]", False),
    ("chmod-777", "medium", GENERAL, r"\bchmod\b.*?\b(?:0o)?777\b", False),
    ("bind-all", "medium", GENERAL, r"(?i)\b(?:host|bind|listen|addr\w*)\b.*?\b0\.0\.0\.0\b", False),
    ("debug-leftover", "medium", PY, r"\bbreakpoint\s*\(\s*\)|\bpdb\.set_trace\s*\(", False),
    ("debug-leftover", "medium", JS, r"\bconsole\.(?:log|debug)\s*\(|^\s*debugger\s*;?\s*$", False),
    ("stub", "low", PY | JS, r"(?:#|//|/\*|\*|<!--)\s*(?:TODO|FIXME|XXX)\b", True),
    ("stub", "medium", PY, r"\braise\s+NotImplementedError\b", False),
]]
DEAD_RULES = {"debug-leftover", "stub"}
MESSAGES = {"eval": "eval runs text as code", "exec": "exec runs text as code", "os-system": "os.system runs a shell",
            "shell-true": "shell=True: the command line goes through a shell", "pickle": "unpickling runs code from the data",
            "yaml-load": "yaml.load without SafeLoader builds any object", "tls-off": "TLS certificate check is off",
            "weak-hash": "MD5/SHA-1 are broken for security use", "debug-on": "debug mode is on",
            "sql-concat": "SQL built from a string, not parameters", "new-function": "new Function runs text as code",
            "inner-html": "innerHTML from a value: XSS", "dangerous-html": "dangerouslySetInnerHTML: XSS",
            "document-write": "document.write: XSS", "exec-template": "a shell command built from a template string",
            "weak-random": "Math.random is not a secure source for a secret", "sql-template": "SQL built from a template string",
            "cors-any": "CORS allows any origin", "chmod-777": "mode 777: anyone may write",
            "bind-all": "listens on all interfaces (0.0.0.0)", "debug-leftover": "debug leftover", "stub": "stub"}

COMMENT = re.compile(r"^\s*(?:#|//|/\*|\*|<!--)")
PY_COMMENT = re.compile(r"^\s*#(?![!])\s?(.*)$")
JS_COMMENT = re.compile(r"^\s*//\s?(.*)$")
CODE_LIKE = re.compile(
    r"(?:(?:def|class)\s+\w+.*:|(?:if|elif|else|for|while|with|try|except|finally)\b.*:|"
    r"import\s+[\w.]+.*|from\s+[\w.]+\s+import\s+\w.*|(?:const|let|var|function)\s+[\w$]+.*|return\b.*[;)\]]|"
    r"[\w.$\[\]'\"]+\s*[+\-*/]?=(?!=)\s*\S.*|[\w.$]+\(.*\)\s*;?|[{}()\[\]]+\s*;?|.*;)\s*")
TEST_FILE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)(?:test_[^/]*|conftest\.py)$|_test\.\w+$|\.(?:test|spec)\.\w+$")
ENTRY_NAMES = {"main", "index", "app", "cli", "server", "manage", "setup", "wsgi", "asgi", "__main__", "run"}
CONFIG_NAME = re.compile(r"^\.|(?:^|[._-])(?:config|conf|settings|rc)(?:[._-]|$)", re.I)
PY_DEF = re.compile(r"^(?:async\s+)?def\s+([A-Za-z]\w*)|^class\s+([A-Za-z]\w*)", re.M)
JS_EXPORT = re.compile(r"^\s*export\s+(?:async\s+)?(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
                       re.M)
JS_EXPORT_LIST = re.compile(r"^\s*export\s*\{([^}]*)\}", re.M)
# a distribution whose import name differs from its own; shortcut: a short table, add pairs as projects need them
PY_ALIASES = {"pyyaml": "yaml", "pillow": "PIL", "beautifulsoup4": "bs4", "scikit-learn": "sklearn",
              "python-dotenv": "dotenv", "opencv-python": "cv2", "python-dateutil": "dateutil",
              "psycopg2-binary": "psycopg2", "python-telegram-bot": "telegram", "protobuf": "google"}
PY_TOOLS = {"pytest", "black", "ruff", "mypy", "flake8", "pylint", "isort", "coverage", "tox", "pre-commit", "gunicorn",
            "uvicorn", "pip", "setuptools", "wheel", "pip-audit"}  # run as commands, never imported
REQ_FILE = re.compile(r"requirements[\w.-]*\.txt$", re.I)
DIST_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SEV_MAP = {"critical": "blocker", "high": "high", "moderate": "medium", "medium": "medium", "low": "low", "info": "low"}


def lang(rel):
    p = Path(rel)
    return p.suffix.lower() or p.name.lower()


def is_records(rel):
    return rel.split("/", 1)[0].lower() == ".software-architector"


def is_artifact(rel):
    return bool({x.lower() for x in rel.split("/")[:-1]} & guard.ARTIFACT_DIRS)


def is_key_file(name):
    low = name.lower()
    return (guard.is_env_file(name) or low.endswith((".pem", ".key", ".p12", ".keystore"))
            or bool(re.match(r"id_(?:rsa|dsa|ecdsa|ed25519)\b", low)) and not low.endswith(".pub"))


def read_text(root, rel):
    """A project file as text; None for a missing, binary or oversized one."""
    p = Path(root) / rel
    try:
        if not p.is_file() or p.stat().st_size > MAX_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    return None if b"\0" in data[:8192] else data.decode("utf-8", errors="replace")


def mask(value):
    return value[:4] + "***"


def secret_hit(line):
    """(pattern name, masked value) of a secret on this line, the same patterns the guard uses; None when clean."""
    name = guard.find_secret(line)
    if not name:
        return None
    for n, rx in guard.SECRET_RES:
        m = rx.search(line)
        if m:
            return n, mask(m.group(1) if rx.groups else m.group(0))
    for m in guard.URL_CREDS.finditer(line):  # find_secret said yes: the first non-local password
        user, pw, host = m.groups()
        if host.lower() not in guard.LOCAL_HOSTS and not pw.startswith(("$", "{", "<", "%")):
            return name, mask(pw)
    return name, "***"


def ordered(findings):
    return sorted(findings, key=lambda f: (SEVERITIES.index(f["sev"]), f["file"], f["line"]))


class Findings:
    """Collects findings; the id hashes rule, file and the line's text, so it stays when lines above move."""

    def __init__(self):
        self.items, self.seen = [], {}

    def add(self, kind, rule, sev, rel, line, text, msg):
        key = f"{rule}|{rel}|{' '.join(text.split())}"
        n = self.seen[key] = self.seen.get(key, 0) + 1
        key += f"|{n}" if n > 1 else ""  # the same line twice in one file: two findings, two ids
        fid = ("s-" if kind == "security" else "d-") + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
        self.items.append({"id": fid, "kind": kind, "rule": rule, "sev": sev, "file": rel, "line": line, "msg": msg})


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def line_text(text, n):
    lines = text.splitlines()
    return lines[n - 1] if 0 < n <= len(lines) else ""


def scan_lines(out, rel, text):
    ext = lang(rel)
    rules = [r for r in RULES if ext in r[2]]
    for n, line in enumerate(text.splitlines(), 1):
        comment = bool(COMMENT.match(line))
        for rule, sev, _, rx, in_comments in rules:
            if (in_comments or not comment) and rx.search(line):
                snippet = guard.mask_secrets(" ".join(line.split()))[:120]
                out.add("dead" if rule in DEAD_RULES else "security", rule, sev, rel, n, line, f"{MESSAGES[rule]}: {snippet}")


def scan_commented_code(out, rel, text):
    rx = PY_COMMENT if lang(rel) in PY else JS_COMMENT
    run = []
    for n, line in enumerate(text.splitlines() + [""], 1):
        m = rx.match(line)
        body = m.group(1).strip() if m else ""
        if m and body and not re.search(r"noqa|type:|pragma|-\*-", body) and CODE_LIKE.fullmatch(body):
            run.append((n, line))
            continue
        if len(run) >= 3:
            out.add("dead", "commented-code", "low", rel, run[0][0], run[0][1], f"{len(run)} lines of commented-out code")
        run = []


class Refs:
    """Who mentions what: the words of every tracked text file outside the run's records, plus the run's commands.
    shortcut: a name or file stem mentioned anywhere counts as used, so dead code a common word names is missed;
    resolve real imports when the false negatives matter."""

    def __init__(self, texts, commands):
        self.texts = {r: t for r, t in texts.items() if not is_records(r)}
        self.words = {r: set(WORD.findall(t)) for r, t in self.texts.items()}
        self.extra = "\n".join(commands)
        self.extra_words = set(WORD.findall(self.extra))

    def elsewhere(self, token, own):
        if WORD.fullmatch(token):
            return token in self.extra_words or any(token in w for r, w in self.words.items() if r != own)
        return token in self.extra or any(token in t for r, t in self.texts.items() if r != own)


def exempt_file(rel, text):
    """Files nothing imports by design: entry points, tests, configs, package markers."""
    p = Path(rel)
    return bool(TEST_FILE.search(rel) or p.name == "__init__.py" or p.stem.lower() in ENTRY_NAMES
                or CONFIG_NAME.search(p.name) or text.startswith("#!") or "__main__" in text and "__name__" in text)


def scan_orphan(out, rel, text, refs):
    if not exempt_file(rel, text) and not refs.elsewhere(Path(rel).stem, rel):
        out.add("dead", "orphan-file", "low", rel, 0, "", f"{rel}: no other file refers to it")


def scan_unused_names(out, rel, text, refs):
    if TEST_FILE.search(rel):
        return
    lines = text.splitlines()
    if lang(rel) in PY:
        if Path(rel).name == "__init__.py":
            return
        m = re.search(r"^__all__\s*=\s*[\[(]([^\])]*)", text, re.M)
        public = set(re.findall(r"['\"](\w+)['\"]", m.group(1))) if m else set()
        for d in PY_DEF.finditer(text):
            name, n = d.group(1) or d.group(2), line_of(text, d.start())
            above = next((x.strip() for x in reversed(lines[:n - 1]) if x.strip()), "")
            if name == "main" or name.lower().startswith("test") or name in public or above.startswith("@"):
                continue
            if len(re.findall(rf"\b{re.escape(name)}\b", text)) < 2 and not refs.elsewhere(name, rel):
                out.add("dead", "unused-export", "low", rel, n, lines[n - 1], f"{name}: defined, used nowhere")
        return
    if exempt_file(rel, text):  # an entry file's exports are the package's API for its users
        return
    found = [(e.group(1), e.start()) for e in JS_EXPORT.finditer(text)]
    for e in JS_EXPORT_LIST.finditer(text):
        found += [(part.split(" as ")[-1].strip(), e.start()) for part in e.group(1).split(",") if part.strip()]
    for name, pos in found:
        if name != "default" and not refs.elsewhere(name, rel):
            n = line_of(text, pos)
            out.add("dead", "unused-export", "low", rel, n, f"{lines[n - 1]}|{name}", f"{name}: exported, no other file imports it")


def first_line(text, needle):
    return next((n for n, line in enumerate(text.splitlines(), 1) if needle in line), 0)


def scan_dependencies(out, rel, text, refs):
    """Runtime dependencies nothing imports; only for a manifest the run changed."""
    name = Path(rel).name
    sources = {r: t for r, t in refs.texts.items() if lang(r) not in (".json", ".lock", ".txt", ".toml")}
    if name == "package.json":
        try:
            data = json.loads(text)
            deps = list((data.get("dependencies") or {}).keys())
            scripts = " ".join(str(v) for v in (data.get("scripts") or {}).values())
        except (ValueError, AttributeError):
            return
        for dep in deps:
            spec = re.compile(rf"['\"`]{re.escape(dep)}(?:/[^'\"`]*)?['\"`]")
            if not any(spec.search(t) for t in sources.values()) \
                    and not re.search(rf"(?<![\w@/-]){re.escape(dep)}(?![\w-])", scripts + "\n" + refs.extra):
                n = first_line(text, f'"{dep}"')
                out.add("dead", "unused-dependency", "medium", rel, n, line_text(text, n), f"{dep}: declared, never imported")
        return
    if REQ_FILE.search(name):
        lines = [(n, line.split("#")[0].strip()) for n, line in enumerate(text.splitlines(), 1)]
        deps = [(n, s) for n, s in lines if s and not s.startswith("-") and "://" not in s]
    elif name == "pyproject.toml":
        # shortcut: [project] dependencies only; Poetry tables and optional groups are not read
        m = re.search(r"^\[project\]\s*$(?:(?!^\[)[\s\S])*?^dependencies\s*=\s*\[([\s\S]*?)\]", text, re.M)
        deps = [(first_line(text, s), s) for s in re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))] if m else []
    else:
        return
    py = [t for r, t in sources.items() if lang(r) in PY]
    for n, s in deps:
        dist = DIST_NAME.match(s)
        if not dist:
            continue
        dist = dist.group().lower()
        mod = PY_ALIASES.get(dist, dist.replace("-", "_").replace(".", "_"))
        imported = re.compile(rf"^\s*(?:import|from)\s+{re.escape(mod)}\b", re.M | re.I)
        if dist not in PY_TOOLS and not any(imported.search(t) for t in py) and dist not in refs.extra.lower():
            out.add("dead", "unused-dependency", "medium", rel, n, line_text(text, n), f"{dist}: declared, never imported")


def scan(root, files, run_files, commands=()):
    """Findings of the tree. `files`: every tracked file; `run_files`: those the run changed (dead code and dangerous
    calls are searched only there); `commands`: the run's setup/check/e2e commands, which refer to files too."""
    out = Findings()
    files = [f for f in files if not is_artifact(f)]
    texts = {}
    for rel in files:
        text = read_text(root, rel)
        if text is not None:
            texts[rel] = text
    for rel in files:
        if is_key_file(Path(rel).name):
            out.add("security", "key-file", "blocker", rel, 0, "", f"{Path(rel).name} is in git: a key or an env file "
                                                                    f"is never committed (untrack it, rotate what it holds)")
        for n, line in enumerate(texts.get(rel, "").splitlines(), 1):
            hit = secret_hit(line)
            if hit:
                out.add("security", "secret", "blocker", rel, n, line, f"{hit[0]}: {hit[1]}")
    refs = Refs(texts, commands)
    for rel in dict.fromkeys(run_files):
        text = texts.get(rel)
        if text is None or is_records(rel) or is_artifact(rel):
            continue
        ext = lang(rel)
        if ext in GENERAL:
            scan_lines(out, rel, text)
        if ext in PY | JS:
            scan_commented_code(out, rel, text)
            scan_orphan(out, rel, text, refs)
            scan_unused_names(out, rel, text, refs)
        scan_dependencies(out, rel, text, refs)
    return ordered(out.items)


# ---------- dependency scanners ----------

def run_tool(argv, cwd, env):
    """One scanner: an argument list, no shell, the given environment, a timeout. FileNotFoundError when not installed."""
    path = next((v for k, v in env.items() if k.upper() == "PATH"), None)
    exe = shutil.which(argv[0], path=path)
    if not exe:
        raise FileNotFoundError(argv[0])
    r = subprocess.run([exe, *argv[1:]], cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=TOOL_TIMEOUT)
    return r.returncode, r.stdout


def parse_npm(data):
    if "error" in data:
        raise ValueError("npm audit reported an error")
    if isinstance(data.get("vulnerabilities"), dict):  # npm 7+
        for pkg, v in data["vulnerabilities"].items():
            via = v.get("via") or []
            titles = [x.get("title") for x in via if isinstance(x, dict) and x.get("title")]
            yield pkg, v.get("severity"), titles[0] if titles else "via " + ", ".join(str(x) for x in via)
    elif isinstance(data.get("advisories"), dict):  # npm 6
        for adv in data["advisories"].values():
            yield adv.get("module_name"), adv.get("severity"), adv.get("title")
    else:
        raise ValueError("no vulnerabilities in the npm audit output")


def parse_pip(data):
    deps = data.get("dependencies") if isinstance(data, dict) else data  # pip-audit < 2.5 printed a bare list
    for dep in deps:
        for v in dep.get("vulns") or []:
            yield dep.get("name"), None, v.get("id")  # pip-audit gives no severity: counted as high


def parse_cargo(data):
    for v in (data.get("vulnerabilities") or {}).get("list") or []:
        adv = v.get("advisory") or {}
        yield adv.get("package") or (v.get("package") or {}).get("name"), None, f"{adv.get('id')} {adv.get('title') or ''}"


def run_tools(root, files, env, runner=None):
    """The installed scanners for the manifests at the project root: ({tool: ran|missing|error}, findings).
    shortcut: `cargo audit` without the cargo-audit plugin reads as error, not missing; both mean "not checked"."""
    runner = runner or run_tool
    top = {f for f in files if "/" not in f}
    reqs = sorted(f for f in top if REQ_FILE.fullmatch(f))
    plan = []
    if "package-lock.json" in top:
        plan.append(("npm", ["npm", "audit", "--json"], "package-lock.json", parse_npm))
    if reqs or "pyproject.toml" in top:
        args = [x for r in reqs for x in ("-r", r)] or ["."]
        plan.append(("pip-audit", ["pip-audit", "-f", "json", *args], reqs[0] if reqs else "pyproject.toml", parse_pip))
    if "Cargo.lock" in top:
        plan.append(("cargo-audit", ["cargo", "audit", "--json"], "Cargo.lock", parse_cargo))
    tools, out = {}, Findings()
    for name, argv, rel, parse in plan:
        try:
            _, text = runner(argv, root, env)  # a scanner exits non-zero when it finds something: the JSON decides
            found = list(parse(json.loads(text)))
        except FileNotFoundError:
            tools[name] = "missing"
            continue
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError):
            tools[name] = "error"
            continue
        tools[name] = "ran"
        for pkg, sev, adv in found:
            word = str(sev or "unrated").lower()
            out.add("security", "cve", SEV_MAP.get(word, "high"), rel, 0, f"{pkg} {adv}", f"{pkg}: {word} — {adv}")
    return tools, out.items


# ---------- report ----------

def summary(au):
    """One line: counts by severity, accepted, and the scanners that did not run."""
    counts = " · ".join(f"{s} {sum(f['sev'] == s for f in au['findings'])}" for s in SEVERITIES)
    missing = [t for t, s in (au.get("tools") or {}).items() if s != "ran"]
    return (f"Audit {au['head'][:7]}: {counts}" + (f" · accepted {len(au['accepted'])}" if au.get("accepted") else "")
            + (f" · not checked: {', '.join(missing)}" if missing else ""))


def open_blocking(au):
    return [f for f in au["findings"] if f["sev"] in BLOCKING and f["id"] not in (au.get("accepted") or {})]


def report(au, findings):
    """audit.md: every finding for bv-reviewer, worst first."""
    tools = " · ".join(f"{t} {s if s == 'ran' else 'not checked (' + s + ')'}" for t, s in au["tools"].items())
    lines = [f"# Audit {au['head'][:7]}", "",
             f"Base: {au['base'] or '—'} · {au['at']} · heuristic line patterns, not a full SAST: a false positive goes to "
             f"DROP with a reason, or `bv audit --accept <id> --note \"<why>\"`.",
             f"Dependency scanners: {tools or 'no dependency manifest at the root'}", "", summary(au)]
    accepted = au.get("accepted") or {}
    for sev in SEVERITIES:
        group = [f for f in findings if f["sev"] == sev]
        if group:
            lines += ["", f"## {sev}", ""]
            lines += [f"- `{f['id']}` {f['kind']} · {f['rule']} · `{f['file']}" + (f":{f['line']}`" if f["line"] else "`")
                      + f" — {f['msg']}" + (f" · accepted: {accepted[f['id']]}" if f["id"] in accepted else "")
                      for f in group]
    return "\n".join(lines) + "\n"
