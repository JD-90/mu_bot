"""Shared roster: every character of ours, on any machine.

roster.json (committed to the repo, same file on every machine) lists all our characters and the
machine that runs each one. accounts.json stays per machine and only holds the logins it runs.

Everything that decides "is this player one of ours?" (GI defense / team-up, BC strangers, crowd
counting when scouting, chat replies, trade requests) uses friends(), so a character running on
the other machine is never treated as a stranger: never attacked, never counted as crowd, never
answered in chat.

Phase 0 of claude/multi-machine-design.md (no network yet: the machines don't share levels or
positions, only who is ours).
"""
import json
from pathlib import Path

ROSTER_FILE = Path(__file__).with_name("roster.json")


def _load():
    try:
        data = json.loads(ROSTER_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        print(f"roster.json could not be read ({e!r}) -> only this machine's characters count as ours")
        return []
    chars = data.get("characters", []) if isinstance(data, dict) else data
    out = []
    for c in chars or []:
        if isinstance(c, str):
            c = {"name": c}
        name = (c.get("name") or "").strip() if isinstance(c, dict) else ""
        if name:
            out.append({"name": name, "machine": (c.get("machine") or "").strip()})
    return out


CHARACTERS = _load()
NAMES = [c["name"] for c in CHARACTERS]
_LOWER = {n.lower() for n in NAMES}


def friends(local_names):
    """This machine's characters first (same order), then the roster's others (no duplicates).
    The page-side checks compare names exactly (ours.includes(name)), so each remote name is also
    given lower-case and Capitalized, in case roster.json doesn't match the in-game spelling."""
    local = list(local_names or [])
    seen = {n.lower() for n in local}
    out = list(local)
    for n in NAMES:
        if n.lower() in seen:
            continue
        for v in (n, n.lower(), n[:1].upper() + n[1:].lower()):
            if v not in out:
                out.append(v)
    return out


def is_ours(name, local_names=()):
    n = (name or "").strip().lower()
    return bool(n) and (n in _LOWER or n in {x.lower() for x in local_names or ()})


def machine_of(name):
    n = (name or "").strip().lower()
    for c in CHARACTERS:
        if c["name"].lower() == n:
            return c["machine"] or None
    return None


def startup_line(local_names):
    """One log line: what the roster says, and anything that doesn't match accounts.json."""
    if not CHARACTERS:
        return "roster: no roster.json -> only this machine's characters count as ours"
    local = {n.lower() for n in local_names or []}
    remote = [c["name"] + (f" ({c['machine']})" if c["machine"] else "")
              for c in CHARACTERS if c["name"].lower() not in local]
    missing = [n for n in local_names or [] if n.lower() not in _LOWER]
    line = f"roster: {len(CHARACTERS)} characters; on other machines: {', '.join(remote) or 'none'}"
    if missing:
        line += f" | WARNING not in roster.json: {', '.join(missing)}"
    return line
