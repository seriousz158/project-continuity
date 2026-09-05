"""Structured progress document and typed ID-based changes (standard library only)."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime

SCHEMA = "project-continuity/v2"
MAX_BYTES = 65536
DATA_START = "<!-- project-continuity:data -->\n```json\n"
DATA_END = "\n```\n<!-- project-continuity:/data -->"
VIEW_START = "<!-- project-continuity:view -->\n"
VIEW_END = "\n<!-- project-continuity:/view -->"
COLLECTIONS = ("tasks", "blockers", "evidence", "decisions")


class Invalid(ValueError):
    """Input violates the progress contract; messages contain no input values."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Invalid("non-finite JSON number")))
    except (ValueError, RecursionError) as exc:
        raise Invalid("invalid JSON input") from exc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def text(value, label, empty=False):
    require(isinstance(value, str) and (empty or bool(value.strip())), label + " must be text")
    require(len(value) <= 4096 and not any(ord(c) < 32 and c not in "\n\t" for c in value)
            and "<!-- project-continuity:" not in value and "\x7f" not in value,
            label + " contains unsupported content")


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value),
            "invalid identifier")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "timestamp needs timezone")
    except (AttributeError, ValueError) as exc:
        raise Invalid("invalid timestamp") from exc


def empty_state(name="Project"):
    return {"project": {"name": name, "goal": "", "status": "active", "current_task": None,
                        "next_step": "", "outcomes": {}},
            **{key: [] for key in COLLECTIONS}, "extensions": {}, "operations": []}


def keyed(rows, label):
    require(isinstance(rows, list), label + " must be a list")
    out = {}
    for row in rows:
        require(isinstance(row, dict), label + " entry must be an object")
        identifier(row.get("id"))
        require(row["id"] not in out, "duplicate identifier")
        out[row["id"]] = row
    return out


def validate(state):
    require(isinstance(state, dict), "state must be an object")
    require(set(state) == {"project", *COLLECTIONS, "extensions", "operations"}, "invalid state fields")
    project = state["project"]
    require(isinstance(project, dict), "project must be an object")
    require(set(project) == {"name", "goal", "status", "current_task", "next_step", "outcomes"}, "invalid project fields")
    text(project["name"], "project name")
    for key in ("goal", "next_step"):
        text(project[key], key, empty=True)
    require(project["status"] in ("active", "paused", "blocked", "complete"), "invalid project status")
    require(isinstance(project["outcomes"], dict) and set(project["outcomes"]) <= {"merge", "release", "deploy", "external"}, "invalid outcomes")
    for value in project["outcomes"].values():
        text(value, "outcome")
    require(isinstance(state["extensions"], dict), "extensions must be an object")
    tasks, blockers, evidence, decisions = (keyed(state[k], k) for k in COLLECTIONS)
    require(project["current_task"] is None or project["current_task"] in tasks, "current task missing")
    for task in tasks.values():
        require(set(task) <= {"id", "title", "status", "owner", "depends_on", "acceptance", "reason", "generation"}, "invalid task fields")
        text(task.get("title"), "title")
        require(task.get("status") in ("todo", "doing", "blocked", "done", "cancelled"), "invalid task status")
        require(task.get("owner") is None or isinstance(task["owner"], str), "invalid owner")
        deps = task.get("depends_on")
        require(isinstance(deps, list) and all(isinstance(x, str) for x in deps), "invalid dependencies")
        require(len(deps) == len(set(deps)) and all(x in tasks for x in deps), "missing or duplicate dependency")
        ac = task.get("acceptance")
        require(isinstance(ac, list) and all(isinstance(x, str) and x.strip() for x in ac), "invalid acceptance")
        require(len(ac) == len(set(ac)), "duplicate acceptance")
        require(type(task.get("generation")) is int and task["generation"] >= 0, "invalid task generation")
        if task["status"] == "cancelled":
            text(task.get("reason"), "cancellation reason")
        if task["status"] in ("doing", "done"):
            require(all(tasks[d]["status"] == "done" for d in deps), "dependency not complete")
    visiting, visited = set(), set()
    def visit(key):
        require(key not in visiting, "dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for dep in tasks[key]["depends_on"]:
            visit(dep)
        visiting.remove(key)
        visited.add(key)
    for key in tasks:
        visit(key)
    for blocker in blockers.values():
        require(set(blocker) <= {"id", "task_id", "description", "status", "resolution"}, "invalid blocker fields")
        require(blocker.get("task_id") in tasks, "blocker task missing")
        text(blocker.get("description"), "blocker description")
        require(blocker.get("status") in ("open", "resolved"), "invalid blocker status")
        if blocker["status"] == "resolved":
            text(blocker.get("resolution"), "blocker resolution")
    for item in evidence.values():
        require(set(item) == {"id", "task_id", "check", "result", "at", "ref", "baseline", "acceptance", "generation"}, "invalid evidence fields")
        require(item["task_id"] in tasks, "evidence task missing")
        for key in ("check", "ref"):
            text(item[key], "evidence " + key)
        timestamp(item["at"])
        require(item["result"] in ("pass", "fail", "not_run"), "invalid evidence result")
        require(isinstance(item["baseline"], dict), "invalid evidence baseline")
        require(type(item["generation"]) is int and 0 <= item["generation"] <= tasks[item["task_id"]]["generation"], "invalid evidence generation")
        require(isinstance(item["acceptance"], list) and all(isinstance(a, str) and a in tasks[item["task_id"]]["acceptance"] for a in item["acceptance"]), "unknown evidence acceptance")
    for task in tasks.values():
        blocked = any(b["task_id"] == task["id"] and b["status"] == "open" for b in blockers.values())
        if task["status"] == "blocked":
            require(blocked, "blocked task needs an open blocker")
        if task["status"] == "done":
            require(not blocked and task["acceptance"], "done task needs acceptance and no open blocker")
            covered = {a for e in evidence.values() if e["task_id"] == task["id"] and e["result"] == "pass" and e["generation"] == task["generation"] for a in e["acceptance"]}
            require(set(task["acceptance"]) <= covered, "done task needs evidence for every acceptance")
    for decision in decisions.values():
        require(set(decision) == {"id", "task_ids", "conclusion", "reason"}, "invalid decision fields")
        require(isinstance(decision["task_ids"], list) and all(isinstance(t, str) and t in tasks for t in decision["task_ids"]), "decision task missing")
        text(decision["conclusion"], "decision conclusion")
        text(decision["reason"], "decision reason")
    if project["status"] == "complete":
        require(bool(tasks) and all(t["status"] in ("done", "cancelled") for t in tasks.values()), "project not complete")
    ops = keyed(state["operations"], "operations")
    for op in ops.values():
        require(set(op) == {"id", "hash", "revision"} and isinstance(op["hash"], str) and re.fullmatch("[0-9a-f]{64}", op["hash"]) and type(op["revision"]) is int and op["revision"] >= 0, "invalid operation receipt")
    return state


def apply(state, patch, baseline):
    """Apply typed partial upserts; no deletions, arbitrary paths or evidence rewrites."""
    require(isinstance(patch, dict) and set(patch) <= {"project", *COLLECTIONS, "extensions"}, "invalid change fields")
    out = copy.deepcopy(state)
    if "project" in patch:
        require(isinstance(patch["project"], dict), "project change must be object")
        out["project"].update(patch["project"])
    if "extensions" in patch:
        require(isinstance(patch["extensions"], dict), "extensions change must be object")
        out["extensions"].update(patch["extensions"])
    for name in COLLECTIONS:
        rows = keyed(out[name], name)
        for key, incoming in keyed(patch.get(name, []), name).items():
            old = rows.get(key)
            if old is not None and name in ("evidence", "decisions"):
                require(incoming == old, "evidence and decisions are immutable; use a new ID")
                continue
            new = copy.deepcopy(old or {})
            new.update(incoming)
            if name == "tasks":
                require("generation" not in incoming, "task generation is managed internally")
                for field, default in {"status": "todo", "owner": None, "depends_on": [], "acceptance": [], "generation": 0}.items():
                    new.setdefault(field, default)
                if old:
                    require(set(old["acceptance"]) <= set(new["acceptance"]), "acceptance conditions cannot be removed")
                    if old["status"] in ("done", "cancelled") and new["status"] != old["status"]:
                        text(incoming.get("reason"), "reopen reason")
                        new["generation"] += 1
            if name == "evidence":
                task = next((t for t in out["tasks"] if t["id"] == new.get("task_id")), None)
                require(task is not None, "evidence task missing")
                require("generation" not in incoming and "baseline" not in incoming, "evidence baseline and generation are managed internally")
                new["baseline"] = copy.deepcopy(baseline)
                new["generation"] = task["generation"]
            rows[key] = new
        out[name] = list(rows.values())
    return validate(out)


def display(value):
    # Display is never parsed as task state. JSON encoding keeps injected headings inert.
    return json.dumps(value, ensure_ascii=False).replace("<", "&lt;").replace(">", "&gt;").replace("`", "&#96;")


def view(state):
    project = state["project"]
    lines = ["## Project", "- Name: " + display(project["name"]), "- Goal: " + display(project["goal"]),
             "- Status: " + project["status"], "- Next: " + display(project["next_step"]), "", "## Tasks"]
    for task in state["tasks"]:
        lines.append(f"- {task['id']} [{task['status']}] " + display(task["title"]))
    lines.extend(["", "## Open blockers"])
    lines.extend("- " + b["id"] + ": " + display(b["description"]) for b in state["blockers"] if b["status"] == "open")
    return "\n".join(lines)


def block(body, start, end):
    require(body.count(start) == 1 and body.count(end) == 1, "missing or duplicate managed section")
    a = body.index(start) + len(start)
    b = body.index(end)
    require(a <= b, "invalid managed section order")
    return a, b


def parse_body(body):
    a, b = block(body, DATA_START, DATA_END)
    c, d = block(body, VIEW_START, VIEW_END)
    require(b + len(DATA_END) <= c - len(VIEW_START) or d + len(VIEW_END) <= a - len(DATA_START), "overlapping managed sections")
    state = validate(loads(body[a:b]))
    return state, body[c:d] == view(state)


def render_body(state, body=None):
    validate(state)
    if body is None:
        body = DATA_START + "{}" + DATA_END + "\n\n" + VIEW_START + "" + VIEW_END + "\n"
    replacements = [(DATA_START, DATA_END, json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)),
                    (VIEW_START, VIEW_END, view(state))]
    for start, end, value in replacements:
        a, b = block(body, start, end)
        body = body[:a] + value + body[b:]
    return body


def summary(state, baseline):
    counts = {status: sum(t["status"] == status for t in state["tasks"]) for status in ("todo", "doing", "blocked", "done", "cancelled")}
    evidence = [{"id": e["id"], "result": e["result"], "verification":
                 "UNVERIFIED" if e["baseline"] != baseline or baseline.get("kind") != "git" or
                 e["generation"] != next(t["generation"] for t in state["tasks"] if t["id"] == e["task_id"])
                 else "recorded"} for e in state["evidence"]]
    return {"project": state["project"], "tasks": state["tasks"], "counts": counts,
            "blockers": [b for b in state["blockers"] if b["status"] == "open"], "evidence": evidence}
