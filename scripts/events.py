"""Software-Architector live journal: what every agent of a run does, for the pane and the web dashboard.

.software-architector/live/<run>/agents/<agent>.jsonl — one line per event, one file per agent (parallel executors never
share a file); .software-architector/live/<run>/out/<tool_use_id>.txt — the full input and output of one tool call.
Written by hooks only, never committed (live/.gitignore), secrets masked before anything touches the disk.
"""
import datetime as dt
import json
import re
from pathlib import Path

import guard

OUT_CAP = 1_000_000  # characters kept per call: the pane reads a file of at most 4 MiB
SUMMARY_CAP = 200
AGENT_ID = re.compile(r"[^A-Za-z0-9_.-]")
HANDBACK = "SubagentHandback"  # Claude Code 2.1.332: the tool a subagent ends with; its `message` is the final answer


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def live_dir(root, st):
    return root / ".software-architector" / "live" / Path(st["dir"]).name.removesuffix("--wip")


def agent_name(data):
    return AGENT_ID.sub("_", data.get("agent_id") or "main")


def text_of(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) <= {"stdout", "stderr", "interrupted", "isImage", "returnCodeInterpretation",
                                                    "noOutputExpected", "backgroundTaskId", "exitCode"}:
        parts = [value.get("stdout") or "", value.get("stderr") and "[stderr]\n" + value["stderr"]]
        return "\n".join(p for p in parts if p)
    return json.dumps(value, ensure_ascii=False, indent=2)


def summary(root, tool, ti):
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if tool in ("Bash", "PowerShell"):
        s = ti.get("command", "")
    elif tool in ("Agent", "Task"):
        s = f"{ti.get('subagent_type', '')}: {ti.get('description', '')}".strip(": ")
    elif tool in ("Grep", "Glob"):
        s = ti.get("pattern", "") + (f" · {guard.rel_to(root, path) or path}" if path else "")
    elif path:
        s = guard.rel_to(root, path) or path
    else:
        s = json.dumps(ti, ensure_ascii=False)
    s = " ".join(guard.mask_secrets(s).split())
    return s[:SUMMARY_CAP] + ("…" if len(s) > SUMMARY_CAP else "")


def ensure_live(root):
    d = root / ".software-architector" / "live"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", "utf-8")
    return d


def append(root, st, agent, event):
    ensure_live(root)
    d = live_dir(root, st)
    (d / "agents").mkdir(parents=True, exist_ok=True)
    # shortcut: one short append per event, not locked — fine for one writer per agent file; lock if that changes
    with open(d / "agents" / f"{agent}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now(), **event}, ensure_ascii=False) + "\n")


def write_out(root, st, call_id, ti, output):
    text = guard.mask_secrets(f"── input ──\n{text_of(ti)}\n\n── output ──\n{text_of(output)}")
    if len(text) > OUT_CAP:
        text = text[:OUT_CAP] + f"\n\n… truncated: {len(text) - OUT_CAP} more characters"
    d = live_dir(root, st) / "out"
    d.mkdir(parents=True, exist_ok=True)
    name = AGENT_ID.sub("_", call_id) + ".txt"
    (d / name).write_text(text, "utf-8")
    return name, len(text)


def record(event, data, root, st):
    """One hook event → one journal line. `event`: post-tool, tool-failed, agent-start, agent-stop, deny,
    wait (the main thread waits for the user: its turn ended or it asks a question), resume (the user answered)."""
    agent = agent_name(data)
    if event == "wait":
        qs = (data.get("tool_input") or {}).get("questions") or []
        text = " / ".join(q.get("question", "") for q in qs if isinstance(q, dict)) or text_of(data.get("last_assistant_message"))
        append(root, st, agent, {"k": "wait", "last": guard.mask_secrets(text)[:4000]})
        return
    if event == "resume":
        append(root, st, agent, {"k": "resume"})
        return
    if event in ("agent-start", "agent-stop"):
        e = {"k": event, "type": (data.get("agent_type") or "").split(":")[-1]}
        if event == "agent-stop" and data.get("last_assistant_message"):
            e.update(final_answer(root, st, agent, data["last_assistant_message"]))
        append(root, st, agent, e)
        return
    tool, ti = data.get("tool_name", ""), data.get("tool_input") or {}
    e = {"k": "tool", "tool": tool, "s": summary(root, tool, ti), "ok": event == "post-tool"}
    if event == "deny":
        e.update(k="deny", why=guard.mask_secrets(data.get("reason", "")))
    else:
        output = data.get("tool_response") if event == "post-tool" else data.get("error")
        e["out"], e["chars"] = write_out(root, st, data.get("tool_use_id") or now(), ti, output)
        resp = data.get("tool_response") if isinstance(data.get("tool_response"), dict) else {}
        if tool in ("Agent", "Task") and resp.get("agentId"):  # links the spawned agent to its description
            e.update(spawned=AGENT_ID.sub("_", resp["agentId"]), tokens=resp.get("totalTokens"),
                     ms=resp.get("totalDurationMs"), tools=resp.get("totalToolUseCount"))
        if tool == HANDBACK and event == "post-tool" and ti.get("message"):
            e.update(final_answer(root, st, agent, ti["message"]))
    append(root, st, agent, e)


def final_answer(root, st, agent, message):
    """The whole answer: a checker's verdict is read by bv from here, never retyped by the orchestrator."""
    full = guard.mask_secrets(text_of(message))
    d = live_dir(root, st) / "out"
    d.mkdir(parents=True, exist_ok=True)
    name = f"stop-{agent}-{AGENT_ID.sub('_', now())}.txt"
    (d / name).write_text(full[:OUT_CAP], "utf-8")
    return {"last": full[:4000], "final": name}


def read_agents(root, st):
    """{agent: [events]} for the web dashboard; a broken line is skipped, never fatal."""
    d = live_dir(root, st) / "agents"
    out = {}
    for p in sorted(d.glob("*.jsonl")) if d.exists() else []:
        rows = []
        for line in p.read_text("utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        out[p.stem] = rows
    return out


def out_path(root, st, name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.txt", name or ""):
        return None
    p = live_dir(root, st) / "out" / name
    return p if p.exists() else None
