#!/usr/bin/env python3
"""Snooze: hide herdr agents from the Agents panel until a deadline.

How hiding works
    A snoozed pane carries a `snoozed` metadata token, and the Agents view is
    filtered to `not(exists snoozed)`. The token is reported with a TTL, so
    herdr itself drops it at the deadline and the agent reappears on time with
    no process of ours running.

What `tick` is for
    The token TTL is capped at 24h, tokens and the view die with the server,
    and herdr has ONE agent view that any caller can replace. `tick` (startup
    hook + a few event hooks) reconciles the state file against all three. It
    reads one small file and exits when nothing is snoozed.

Python 3.8+, standard library only.
"""

import contextlib
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "herdr-snooze"
SOURCE = "plugin:" + PLUGIN_ID
TOKEN = "snoozed"

MAX_TTL_MS = 86400000  # herdr's cap on a metadata token's ttl_ms
REFRESH_MARGIN_MS = 12 * 3600 * 1000  # re-report long snoozes this far before the token dies
MAX_SNOOZE_S = 366 * 86400

DEFAULT_DURATIONS = ["15m", "1h", "3h", "6h", "1d", "3d", "1w"]
DEFAULT_BADGE = "\U0001F4A4"  # 💤

VIEW_FILTER = {"op": "not", "filter": {"op": "exists", "field": {"token": TOKEN}}}
# The snoozed list also lets blocked agents through: it is the one panel state
# that hides your active agents, and one of them needing you is exactly what
# the panel exists to show.
SNOOZED_LIST_FILTER = {"op": "any", "filters": [
    {"op": "exists", "field": {"token": TOKEN}},
    {"op": "eq", "field": "status", "value": "blocked"},
]}

# herdr has a single agent view and its query is not readable: the only thing
# a caller can learn is the owner's `source` and `label`. So borrowing the view
# from a plugin that sorts the panel means re-stating its sort ourselves, and
# this table is how we know it. Unknown owners lose their sort while something
# is snoozed; teach snooze about one with "views" in config.json
# ({"<source>": {"<label>": [<sort>, …]}}), or pin a sort outright with "sort".
KNOWN_VIEWS = {
    "herdr-agent-inbox": {
        "sorts": {
            "Inbox": [
                {"field": {"token": "rank"}, "order": "asc"},
                {"field": "state_change_seq", "order": "desc"},
            ],
        },
    },
    "plugin:hhdebb.herdr-radar": {
        "sorts": {
            "active": [
                {"field": {"token": "ws_key"}, "order": "desc"},
                {"field": {"token": "tab_key"}, "order": "desc"},
                {"field": {"token": "sort_key"}, "order": "desc"},
            ],
            "recent": [{"field": {"token": "sort_key"}, "order": "desc"}],
        },
        # Radar turns its view off with a source-matched clear, which is a
        # no-op while we hold the view. Its persisted intent is the only trace.
        "off_flag": ("hhdebb.herdr-radar", "agent-view.on", "off"),
    },
}


# ── paths ────────────────────────────────────────────────────────────────────


def _herdr_state_root():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "herdr", "plugins")


def state_dir():
    return (
        os.environ.get("SNOOZE_STATE_DIR")
        or os.environ.get("HERDR_PLUGIN_STATE_DIR")
        or os.path.join(_herdr_state_root(), PLUGIN_ID)
    )


def config_dir():
    if os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        return os.environ["HERDR_PLUGIN_CONFIG_DIR"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "herdr", "plugins", "config", PLUGIN_ID)


def load_config():
    config = {"durations": list(DEFAULT_DURATIONS), "badge": DEFAULT_BADGE, "notify": False, "sort": None, "views": None, "auto_return": True}
    try:
        with open(os.path.join(config_dir(), "config.json"), encoding="utf-8") as handle:
            user = json.load(handle)
        if isinstance(user, dict):
            config.update({k: v for k, v in user.items() if k in config})
    except (OSError, ValueError):
        pass
    if not isinstance(config["durations"], list) or not config["durations"]:
        config["durations"] = list(DEFAULT_DURATIONS)
    return config


# ── herdr socket ─────────────────────────────────────────────────────────────


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code


def call(method, params=None, timeout=5.0):
    """One request per connection: the server answers a line and hangs up."""
    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "herdr", "herdr.sock"
    )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall((json.dumps({"id": "snooze", "method": method, "params": params or {}}) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    reply = json.loads(buf.decode("utf-8"))
    if reply.get("error"):
        raise HerdrError(reply["error"].get("code", "error"), reply["error"].get("message", ""))
    return reply.get("result") or {}


def notify(title, body=None):
    try:
        call("notification.show", {"title": title, "body": body})
    except (OSError, ValueError, HerdrError):
        pass


def log(*parts):
    # Plugin stdout is captured by herdr: `herdr plugin log list --plugin herdr-snooze`.
    print(*parts, flush=True)


# ── time ─────────────────────────────────────────────────────────────────────

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
}
_UNIT_NAMES = [(604800, "week"), (86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")]
_PART = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)")
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def _parse_clock(text):
    """'17:30', '9am', '9:15 pm' -> (hour, minute). A bare number is not a clock."""
    match = _CLOCK.match(text)
    if not match or (match.group(2) is None and match.group(3) is None):
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if match.group(3):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match.group(3) == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_until(text, now=None):
    """Turn '15m', '1h30m', '2 days', '90' (minutes), '17:30', '9am' or
    'tomorrow [9am]' into a deadline. Raises ValueError with a user-facing
    message."""
    now = now or datetime.now()
    text = (text or "").strip().lower()
    if not text:
        raise ValueError("enter a duration like 45m, 2h, 3d or a time like 17:30")

    if text.startswith("tomorrow"):
        clock = _parse_clock(text[len("tomorrow"):].strip() or "9:00")
        if not clock:
            raise ValueError("try 'tomorrow' or 'tomorrow 14:00'")
        day = now + timedelta(days=1)
        return day.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)

    clock = _parse_clock(text)
    if clock:
        until = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return until if until > now else until + timedelta(days=1)

    seconds, pos = 0.0, 0
    while pos < len(text):
        match = _PART.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError("can't read '%s' — try 45m, 2h, 3d, 1w or 17:30" % text)
        unit = match.group(2) or "m"
        if unit not in _UNITS:
            raise ValueError("unknown unit '%s' — use s, m, h, d or w" % unit)
        seconds += float(match.group(1)) * _UNITS[unit]
        pos = match.end()
    if seconds <= 0:
        raise ValueError("duration must be more than zero")
    if seconds > MAX_SNOOZE_S:
        raise ValueError("that's more than a year — close the pane instead?")
    return now + timedelta(seconds=seconds)


def describe(spec):
    """Menu label for a configured duration: '15m' -> '15 minutes'."""
    text = str(spec).strip().lower()
    if text.startswith("tomorrow") or _parse_clock(text):
        return text if text.startswith("tomorrow") else "until " + text
    try:
        total = int((parse_until(text, datetime(2000, 1, 1)) - datetime(2000, 1, 1)).total_seconds())
    except ValueError:
        return text
    parts = []
    for size, name in _UNIT_NAMES:
        count, total = divmod(total, size)
        if count:
            parts.append("%d %s%s" % (count, name, "" if count == 1 else "s"))
    return " ".join(parts)


def format_until(until, now=None):
    now = now or datetime.now()
    if until.date() == now.date():
        return until.strftime("%H:%M")
    if until - now < timedelta(days=6):
        return until.strftime("%a %H:%M")
    return until.strftime("%d %b %H:%M").lstrip("0")


def format_left(ms):
    # Rounds up: a 3-minute snooze reads "3m" the moment it is set, not "2m".
    seconds = max(0, -(-int(ms) // 1000))
    if seconds < 60:
        return "%ds" % seconds
    minutes = -(-seconds // 60)
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours) if hours else "%dd" % days


def badged(badge, text):
    """The badge, a gap, then text. A wide badge (an emoji such as 💤) gets two
    spaces: herdr's sidebar lets it cover the single space after it, so
    "💤 1" shows as "💤1"."""
    if not badge:
        return text
    wide = unicodedata.east_asian_width(badge[-1]) in ("W", "F")
    return badge + ("  " if wide else " ") + text


def now_ms():
    return int(time.time() * 1000)


def to_ms(moment):
    return int(moment.timestamp() * 1000)


def from_ms(ms):
    return datetime.fromtimestamp(ms / 1000.0)


# ── state ────────────────────────────────────────────────────────────────────


def empty_state():
    return {"v": 1, "panes": {}, "workspaces": {}, "view": {}, "timer": None}


def read_state():
    try:
        with open(os.path.join(state_dir(), "snoozed.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return empty_state()
    state = empty_state()
    if isinstance(data, dict):
        # Every hook reads this file; one malformed entry (a hand edit, a
        # half-understood older version) must not make all of them crash.
        for key in ("panes", "workspaces"):
            if isinstance(data.get(key), dict):
                state[key] = {
                    name: entry for name, entry in data[key].items()
                    if isinstance(entry, dict) and isinstance(entry.get("until"), int)
                }
        if isinstance(data.get("view"), dict):
            state["view"] = dict(data["view"])
            if "inherited" in state["view"] and not isinstance(state["view"]["inherited"], dict):
                state["view"]["inherited"] = None
        if isinstance(data.get("timer"), int):
            state["timer"] = data["timer"]
    return state


def is_idle(state):
    return not state["panes"] and not state["workspaces"] and not state["view"]


@contextlib.contextmanager
def locked_state():
    """Hooks can overlap (two events in the same instant), so every
    read-modify-write of the state file happens under one flock."""
    root = state_dir()
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state()
        before = json.dumps(state, sort_keys=True)
        try:
            yield state
        finally:
            # Also on failure: a herdr call can die after tokens were already
            # sent, and a token with no record is an agent hidden for up to a
            # day that no list shows. What we meant to do is the better record;
            # the next tick reconciles whatever did not land.
            path = os.path.join(root, "snoozed.json")
            if is_idle(state):
                # No file is the idle signal run.sh checks before starting Python.
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)
            elif json.dumps(state, sort_keys=True) != before:
                with open(path + ".tmp", "w", encoding="utf-8") as handle:
                    json.dump(state, handle, indent=1, sort_keys=True)
                os.replace(path + ".tmp", path)


# ── reconcile (pure) ─────────────────────────────────────────────────────────


def _token_due(entry, now, present):
    if entry.pop("dirty", False) or not present:
        return True
    token_until = entry.get("token_until", 0)
    return token_until < entry["until"] and token_until - now < REFRESH_MARGIN_MS


def _token_op(kind, target, entry, now, badge):
    ttl = max(1, min(entry["until"] - now, MAX_TTL_MS))
    entry["token_until"] = now + ttl
    value = badged(badge, format_until(from_ms(entry["until"]), from_ms(now)))
    return (kind, target, value, ttl)


def reconcile(state, panes, now, force=False, badge=DEFAULT_BADGE):
    """Bring `state` in line with the live panes and the clock. Mutates
    `state`; returns the token ops the caller must send:
    ("report", pane_id, value, ttl) | ("clear", pane_id) |
    ("ws_report", workspace_id, value, ttl) | ("ws_clear", workspace_id)."""
    ops = []
    live = {pane["pane_id"]: pane for pane in panes}
    live_workspaces = {pane.get("workspace_id") for pane in panes}

    for wid in list(state["workspaces"]):
        space = state["workspaces"][wid]
        if wid not in live_workspaces:
            # No panes left in it. If the space itself is gone the clear just
            # fails quietly; if it is merely empty, this takes its badge off.
            ops.append(("ws_clear", wid))
            del state["workspaces"][wid]
            continue
        if now >= space["until"]:
            ops.append(("ws_clear", wid))
            del state["workspaces"][wid]
            continue
        # Agents in the space join it — including ones that start later (the
        # pane.agent_detected hook lands here) — unless that pane was
        # deliberately woken, or given its own deadline. Shells are left alone:
        # a token on one hides nothing and makes every count lie.
        for pane in panes:
            pid = pane["pane_id"]
            if not pane.get("agent"):
                continue
            if pane.get("workspace_id") == wid and pid not in state["panes"] and pid not in space.get("except", []):
                state["panes"][pid] = {"until": space["until"], "via": "workspace", "workspace_id": wid, "dirty": True}
        # Workspace tokens are not visible from pane.list, so only the clock
        # (or a forced pass after a server restart) can say one needs sending.
        if _token_due(space, now, present=not force):
            ops.append(_token_op("ws_report", wid, space, now, badge))

    for pid in list(state["panes"]):
        entry = state["panes"][pid]
        pane = live.get(pid)
        if pane is None:
            del state["panes"][pid]
            continue
        if now >= entry["until"]:
            # The TTL normally got here first; this covers a late token (herdr's
            # clock pauses while the machine sleeps) and makes wake-ups explicit.
            ops.append(("clear", pid))
            del state["panes"][pid]
            continue
        present = TOKEN in (pane.get("tokens") or {})
        if _token_due(entry, now, present=present and not force):
            ops.append(_token_op("report", pid, entry, now, badge))

    # A token with no record behind it (state file lost, a run that died
    # halfway) is an agent hidden for up to a day that no list shows.
    cleared = {op[1] for op in ops if op[0] == "clear"}
    for pid, pane in live.items():
        if pid not in state["panes"] and pid not in cleared and TOKEN in (pane.get("tokens") or {}):
            ops.append(("clear", pid))
    return ops


def known_sort(owner, views=None):
    """The sort behind another plugin's view, if we know it. `views` is the
    user's config.json table and wins over the built-in one."""
    source, label = (owner or {}).get("source"), (owner or {}).get("label")
    user = (views or {}).get(source)
    if isinstance(user, dict) and isinstance(user.get(label), list):
        return user[label]
    known = KNOWN_VIEWS.get(source)
    return known["sorts"].get(label) if known else None


def owner_turned_off(owner, state_root=None):
    known = KNOWN_VIEWS.get((owner or {}).get("source"))
    if not known or not known.get("off_flag"):
        return False
    plugin, filename, off_value = known["off_flag"]
    try:
        with open(os.path.join(state_root or _herdr_state_root(), plugin, filename), encoding="utf-8") as handle:
            return handle.read().strip() == off_value
    except OSError:
        return False


def settle_showing(state, focused):
    """The snoozed list is somewhere you visit, not a mode to get stuck in: a
    panel that keeps listing only snoozed agents hides the one that just got
    blocked. So it ends by itself once focus lands on a pane that is not
    snoozed — other than the pane you opened it from, which is where focus
    still is while you read the list. Returns True when it switched back."""
    view = state["view"]
    if view.get("showing") != "snoozed" or focused is None:
        return False
    if focused in state["panes"]:
        view["from_pane"] = None  # went to look at one; any other pane now means "done here"
        return False
    if focused == view.get("from_pane"):
        return False
    view.pop("showing")
    view.pop("from_pane", None)
    return True


def plan_view(state, owner, count, sort_override=None, badge=DEFAULT_BADGE, owner_off=False, views=None, blocked=0):
    """Decide what to do with herdr's single agent view. `owner` is the probe
    result ({active, source, label}); `count` is how many *agents* are snoozed
    and `blocked` how many active ones are blocked. Mutates state["view"];
    returns ("set", params), ("clear", params) or None."""
    view = state["view"]
    mine = bool(owner.get("active")) and owner.get("source") == SOURCE
    inherited = view.get("inherited")
    if owner.get("active") and not mine:
        inherited = {"source": owner.get("source"), "label": owner.get("label")}
        # If we had the snoozed list up and it got replaced, someone just
        # worked their own panel control. Re-taking the view is how hiding
        # keeps working; putting the snoozed list back on top would undo what
        # they asked for — and the safe side to land on is normal. (Not when
        # the list was only just asked for and isn't up yet: that is a toggle
        # arriving while another plugin happens to hold the view.)
        try:
            had_list_up = json.loads(view.get("applied") or "{}").get("filter") == SNOOZED_LIST_FILTER
        except ValueError:
            had_list_up = False
        if had_list_up:
            view.pop("showing", None)
            view.pop("from_pane", None)
    elif not owner.get("active"):
        inherited = None
    if owner_off:
        inherited = None

    # `count`, not state["panes"]: a record with no agent behind it (a shell, an
    # agent that exited) hides nothing, and a filter held for it would make the
    # snoozed list an empty panel.
    if count:
        base = (inherited or {}).get("label")
        # `showing` is the toggle: the same panel, flipped to list what is
        # snoozed. The two labels must not be confusable — one means "N are
        # hidden from this list", the other "this list IS the hidden ones".
        showing_snoozed = view.get("showing") == "snoozed"
        if showing_snoozed:
            label = badged(badge, "snoozed only · %d" % count) + (" + %d blocked" % blocked if blocked else "")
        else:
            label = "%s %s" % (base, badged(badge, str(count))) if base else badged(badge, str(count))
        want = {
            "source": SOURCE,
            "label": label.strip(),
            "filter": SNOOZED_LIST_FILTER if showing_snoozed else VIEW_FILTER,
            "sort": sort_override or known_sort(inherited, views) or [],
        }
        fingerprint = json.dumps(want, sort_keys=True)
        state["view"] = {"inherited": inherited, "applied": fingerprint}
        if showing_snoozed:
            state["view"]["showing"] = "snoozed"
            state["view"]["from_pane"] = view.get("from_pane")
        if mine and view.get("applied") == fingerprint:
            return None
        return ("set", want)

    state["view"] = {}
    if not mine:
        return None
    sort = known_sort(inherited, views)
    if inherited and sort is not None:
        # Hand the borrowed view back exactly as we found it, under its
        # owner's source, so that owner's own toggles keep working.
        return ("set", {"source": inherited["source"], "label": inherited.get("label"), "sort": sort})
    return ("clear", {"source": SOURCE})


# ── sync: reconcile + talk to herdr ──────────────────────────────────────────


def send_token_ops(ops):
    """Returns the ops herdr refused."""
    failed = []
    for op in ops:
        kind, target = op[0], op[1]
        try:
            if kind == "report":
                call("pane.report_metadata", {"pane_id": target, "source": SOURCE, "tokens": {TOKEN: op[2]}, "ttl_ms": op[3]})
            elif kind == "clear":
                call("pane.report_metadata", {"pane_id": target, "source": SOURCE, "tokens": {TOKEN: None}})
            elif kind == "ws_report":
                call("workspace.report_metadata", {"workspace_id": target, "source": SOURCE, "tokens": {TOKEN: op[2]}, "ttl_ms": op[3]})
            elif kind == "ws_clear":
                call("workspace.report_metadata", {"workspace_id": target, "source": SOURCE, "tokens": {TOKEN: None}})
        except HerdrError as error:
            log("token op failed:", op, error)  # a pane closing mid-sync is not worth aborting for
            failed.append(op)
    return failed


def probe_view():
    # A clear whose source owns nothing changes nothing and reports the owner.
    return call("agent.view.clear", {"source": SOURCE + ".probe"})


def _spawn_timer(delay_s):
    launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.sh")
    subprocess.Popen(
        ["/bin/sh", "-c", 'sleep "$1" && exec /bin/sh "$2" tick', "snooze-timer", str(delay_s), launcher],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


def arm_timer(state, now, spawn=None):
    """One detached `sleep` that runs a tick just after the next deadline.

    herdr's TTL already un-hides the agent on time; what nothing does on time
    is *us* noticing: the label keeps its count, the view stays borrowed, and
    in the snoozed list the last agent expiring leaves an empty panel — until
    some unrelated event happens to fire a hook. Not a daemon: it sleeps, ticks
    once, and is gone."""
    entries = list(state["panes"].values()) + list(state["workspaces"].values())
    if not entries:
        state["timer"] = None
        return
    # A snooze longer than the 24h TTL cap has to be re-reported before its
    # token dies, or the agent pops back days early on a machine quiet enough
    # that no hook fires (a weekend). So the next wake-up is whichever comes
    # first: a deadline, or a token entering its refresh margin.
    soonest = min(
        entry["until"] if entry.get("token_until", entry["until"]) >= entry["until"]
        else min(entry["until"], entry["token_until"] - REFRESH_MARGIN_MS + 1000)
        for entry in entries
    )
    if state.get("timer") == soonest:
        return
    try:
        (spawn or _spawn_timer)(max(1, (soonest - now) // 1000 + 1))
        state["timer"] = soonest
    except OSError as error:
        log("timer not armed:", error)  # hooks still catch up, just later


def focused_pane(panes, agents_only=False):
    return next(
        (pane["pane_id"] for pane in panes if pane.get("focused") and (pane.get("agent") or not agents_only)), None
    )


def sync(state, config, force=False):
    panes = call("pane.list").get("panes", [])
    now = now_ms()
    ops = reconcile(state, panes, now, force=force, badge=config["badge"])
    for op in send_token_ops(ops):
        # Workspace tokens can't be read back, so nothing else would notice.
        entry = state["workspaces" if op[0].startswith("ws_") else "panes"].get(op[1])
        if entry is not None and op[0] in ("report", "ws_report"):
            entry["dirty"] = True
    # Only an *agent* pane counts as "went back to work": a shell, or the
    # picker popup itself taking focus, is not that.
    if config["auto_return"] and settle_showing(state, focused_pane(panes, agents_only=True)):
        log("left the snoozed list: focus moved to an active agent")
    if force:
        # A restart. The sleeper may be gone, and nobody wants herdr to come
        # back up listing only snoozed agents.
        state["timer"] = None
        state["view"].pop("showing", None)
        state["view"].pop("from_pane", None)
    arm_timer(state, now)

    owner = probe_view()
    agents = {pane["pane_id"] for pane in panes if pane.get("agent")}
    count = len([pid for pid in state["panes"] if pid in agents])
    blocked = len([p for p in panes if p.get("agent") and p.get("agent_status") == "blocked" and p["pane_id"] not in state["panes"]])
    # Whose "off" flag to honour: the plugin holding the view right now, and
    # only while we hold it ourselves, the one we borrowed it from.
    foreign = owner.get("active") and owner.get("source") != SOURCE
    action = plan_view(
        state, owner, count,
        sort_override=config["sort"], badge=config["badge"], views=config["views"], blocked=blocked,
        owner_off=owner_turned_off(owner if foreign else state["view"].get("inherited")),
    )
    if action:
        call("agent.view.set" if action[0] == "set" else "agent.view.clear", action[1])
    if ops or action:
        log("sync:", len(ops), "token op(s);", "view " + action[0] if action else "view unchanged")
    return panes


def snooze(kind, target, until, config):
    """kind is 'pane' or 'workspace'. Returns how many panes are now covered."""
    until_ms = to_ms(until)
    with locked_state() as state:
        if kind == "workspace":
            state["workspaces"][target] = {"until": until_ms, "dirty": True}
            for entry in state["panes"].values():
                if entry.get("workspace_id") == target:
                    entry.update({"until": until_ms, "via": "workspace", "dirty": True})
        else:
            state["panes"][target] = {"until": until_ms, "via": "pane", "dirty": True}
            # Its own deadline now outranks its space's: without this, a shorter
            # snooze ends, the agent reappears, and the space re-adopts it a
            # tick later for the rest of the space's (longer) snooze.
            for space in state["workspaces"].values():
                if target not in space.setdefault("except", []):
                    space["except"].append(target)
        panes = sync(state, config)
        for pane in panes:  # remember where a pane lives, so a later workspace snooze/wake finds it
            if pane["pane_id"] in state["panes"]:
                state["panes"][pane["pane_id"]]["workspace_id"] = pane.get("workspace_id")
        if kind == "workspace":
            return len([e for e in state["panes"].values() if e.get("workspace_id") == target])
        return 1 if target in state["panes"] else 0


def wake(config, pane_ids=(), workspace_ids=(), everything=False):
    with locked_state() as state:
        ops = []
        spaces = list(state["workspaces"]) if everything else [w for w in workspace_ids if w in state["workspaces"]]
        for wid in spaces:
            del state["workspaces"][wid]
            ops.append(("ws_clear", wid))
        for pid in list(state["panes"]):
            entry = state["panes"][pid]
            in_woken_space = entry.get("via") == "workspace" and entry.get("workspace_id") in spaces
            if not (everything or pid in pane_ids or in_woken_space):
                continue
            del state["panes"][pid]
            ops.append(("clear", pid))
            space = state["workspaces"].get(entry.get("workspace_id"))
            if space is not None and pid not in space.setdefault("except", []):
                space["except"].append(pid)  # woken early out of a still-snoozed space: don't re-adopt it
        send_token_ops(ops)
        sync(state, config)
        return len([op for op in ops if op[0] == "clear"])


# ── TUI (the popup) ──────────────────────────────────────────────────────────

BOLD, DIM, REVERSE, RESET = "\x1b[1m", "\x1b[2m", "\x1b[7m", "\x1b[0m"
_KEYS = {
    b"\x1b[A": "up", b"\x1bOA": "up", b"k": "up", b"\x10": "up",
    b"\x1b[B": "down", b"\x1bOB": "down", b"j": "down", b"\x0e": "down",
    b"\r": "enter", b"\n": "enter",
    b"\x1b": "esc", b"\x03": "esc", b"q": "esc",
    b"\x7f": "backspace", b"\x08": "backspace",
}


def decode_key(data):
    """One key's bytes -> (name, raw bytes). Anything unnamed is 'char'."""
    return ("eof", b"") if not data else (_KEYS.get(data, "char"), data)


_SEQUENCES = sorted((seq for seq in _KEYS if len(seq) > 1), key=len, reverse=True)


def split_keys(data):
    """One read can carry several keys (a held arrow, a paste): split it, so
    `↓↓` moves twice instead of being one unknown blob that moves nothing."""
    keys = []
    while data:
        known = next((seq for seq in _SEQUENCES if data.startswith(seq)), None)
        if known:
            size = len(known)
        elif data[:1] == b"\x1b" and len(data) > 1:
            size = len(data)  # an escape sequence we don't know: swallow it whole, never half
        else:
            lead = data[0]
            size = 1 if lead < 0xC0 else 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4  # one UTF-8 character
        keys.append(data[:size])
        data = data[size:]
    return keys


class RawTerminal:
    def __enter__(self):
        import termios
        import tty

        self.fd = sys.stdin.fileno()
        self.pending = []
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        try:
            self.rows = os.get_terminal_size(self.fd).lines
        except OSError:
            self.rows = 24
        sys.stdout.write("\x1b[?25l")
        return self

    def __exit__(self, *exc):
        import termios

        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        sys.stdout.write("\x1b[?25h\x1b[2J\x1b[H")
        sys.stdout.flush()

    def key(self):
        if not self.pending:
            data = os.read(self.fd, 64)
            if data == b"\x1b":
                # A lone ESC is either the Escape key or the first byte of an
                # arrow whose rest is still in flight. Waiting a moment to find
                # out beats closing the popup on a ↓.
                import select

                if select.select([self.fd], [], [], 0.05)[0]:
                    data += os.read(self.fd, 64)
            self.pending = split_keys(data) or [b""]
        return decode_key(self.pending.pop(0))

    def draw(self, lines):
        sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(lines))
        sys.stdout.flush()


def clip(text, width):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def menu(term, header, items, footer, selected=0):
    """items: (hotkey, label, hint). Returns the chosen index, or None."""
    while True:
        # Keep the selection on screen when there are more rows than the popup
        # has lines; the blank lines above and below double as "more" markers.
        room = max(3, getattr(term, "rows", 100) - len(header) - 4)
        top = 0 if len(items) <= room else min(max(0, selected - room // 2), len(items) - room)
        above, below = top, max(0, len(items) - top - room)
        lines = [""] + header + ["  " + DIM + "↑ %d more" % above + RESET if above else ""]
        for index, (hotkey, label, hint) in list(enumerate(items))[top:top + room]:
            row = " %s  %-20s " % (hotkey or " ", clip(label, 20))
            lines.append(" " + REVERSE + row + hint + " " + RESET if index == selected else " " + row + DIM + hint + RESET)
        lines += ["  " + DIM + "↓ %d more" % below + RESET if below else "", "  " + DIM + footer + RESET]
        term.draw(lines)

        name, raw = term.key()
        if name in ("esc", "eof"):
            return None
        if name == "up":
            selected = (selected - 1) % len(items)
        elif name == "down":
            selected = (selected + 1) % len(items)
        elif name == "enter":
            return selected
        else:
            typed = raw.decode("utf-8", "ignore").lower()
            for index, item in enumerate(items):
                if typed and typed == item[0]:
                    return index


def prompt(term, header, question):
    """One-line input. Returns a datetime, or None on esc."""
    text, error = "", ""
    while True:
        term.draw(
            [""] + header + ["", "  " + question, "", "  " + BOLD + "› " + RESET + text + "▏", "",
             "  " + (error or DIM + "45m · 2h · 3d · 1w · 17:30 · tomorrow 9am" + RESET), "",
             "  " + DIM + "enter confirm · esc back" + RESET]
        )
        name, raw = term.key()
        if name in ("eof",) or raw in (b"\x1b", b"\x03"):
            return None
        if name == "enter":
            try:
                return parse_until(text)
            except ValueError as problem:
                error = clip(str(problem), 44)
        elif name == "backspace":
            text, error = text[:-1], ""
        else:
            typed = raw.decode("utf-8", "ignore")
            if typed.isprintable():
                text, error = text + typed, ""


def picker_snooze(term, config):
    kind = os.environ.get("SNOOZE_KIND", "pane")
    target = os.environ.get("SNOOZE_TARGET", "")
    header = ["  " + BOLD + clip(os.environ.get("SNOOZE_LABEL") or target, 42) + RESET]
    if os.environ.get("SNOOZE_DETAIL"):
        header.append("  " + DIM + clip(os.environ["SNOOZE_DETAIL"], 42) + RESET)

    now = datetime.now()
    choices = []
    for spec in config["durations"][:9]:
        try:
            choices.append((describe(spec), str(spec), parse_until(str(spec), now)))
        except ValueError:
            continue
    items = [(str(i + 1), label, format_until(until, now)) for i, (label, _spec, until) in enumerate(choices)]
    items.append(("c", "Custom…", ""))

    index = 0
    while True:
        index = menu(term, header, items, "↑↓ move · enter select · esc cancel", selected=index)
        if index is None:
            return None
        if index < len(choices):
            # Re-read the clock: a popup left open for 20 minutes must not turn
            # "15 minutes" into a deadline that has already passed.
            until = parse_until(choices[index][1])
        else:
            until = prompt(term, header, "Snooze for how long, or until when?")
        if until is None:
            continue
        covered = snooze(kind, target, until, config)
        return "snoozed %s %s (%d pane(s)) until %s" % (kind, target, covered, until.strftime("%Y-%m-%d %H:%M"))


def run_picker():
    config = load_config()
    with RawTerminal() as term:
        try:
            outcome = picker_snooze(term, config)
        except (OSError, ValueError, HerdrError) as problem:
            # The popup closes with the process, so an error that only went to
            # the log would look like the picker silently doing nothing.
            log("picker failed:", problem)
            term.draw(["", "  " + BOLD + "Snooze didn't work" + RESET, "", "  " + clip(str(problem), 44), "",
                       "  " + DIM + "press any key" + RESET])
            term.key()
            return 1
    if outcome:
        log(outcome)
        if config["notify"] and outcome.startswith("snoozed"):
            notify("Snoozed", outcome.split(" until ")[-1])
    return 0


# ── actions (what herdr invokes) ─────────────────────────────────────────────


def pane_label(pane):
    return pane.get("title") or pane.get("terminal_title_stripped") or pane.get("terminal_title") or pane.get("agent") or pane["pane_id"]


def open_picker(env, rows):
    params = {
        "plugin_id": PLUGIN_ID, "entrypoint": "picker", "placement": "popup", "focus": True,
        "width": 50, "height": rows, "env": env,
    }
    for attempt in range(4):
        try:
            call("plugin.pane.open", params)
            return 0
        except HerdrError as error:
            # The menu this action was picked from may still be closing.
            if error.code != "ui_busy" or attempt == 3:
                notify("Snooze", "Couldn't open the picker: %s" % error)
                log("open failed:", error)
                return 1
            time.sleep(0.15)
    return 1


def invocation_context():
    try:
        context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        context = {}
    return context if isinstance(context, dict) else {}


def run_action(name):
    context = invocation_context()
    log("action", name, "via", context.get("invocation_source"), "pane", context.get("focused_pane_id"), "workspace", context.get("workspace_id"))
    config = load_config()
    # durations + Custom + chrome, and never less than the Custom prompt needs
    height = max(min(len(config["durations"]), 9) + 10, 13)

    if name == "wake":
        # The one-step way out, for the snoozed list: no popup to get through.
        pane_id = context.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
        if pane_id not in read_state()["panes"]:
            notify("Snooze", "This agent isn't snoozed.")
            return 0
        wake(config, pane_ids=[pane_id])
        return 0

    if name == "workspace":
        wid = context.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
        if wid in read_state()["workspaces"]:
            wake(config, workspace_ids=[wid])  # the same action wakes it: one key, both ways
            return 0
        spaces = {w["workspace_id"]: w for w in call("workspace.list").get("workspaces", [])}
        if wid not in spaces:
            notify("Snooze", "No space to snooze here.")
            return 1
        agents = [p for p in call("pane.list").get("panes", []) if p.get("workspace_id") == wid and p.get("agent")]
        detail = "space · %d agent%s will be hidden" % (len(agents), "" if len(agents) == 1 else "s")
        env = {"SNOOZE_KIND": "workspace", "SNOOZE_TARGET": wid, "SNOOZE_LABEL": spaces[wid].get("label") or wid, "SNOOZE_DETAIL": detail}
        return open_picker(env, height)

    pane_id = context.get("focused_pane_id") or os.environ.get("HERDR_PANE_ID")
    if pane_id in read_state()["panes"]:
        wake(config, pane_ids=[pane_id])  # the same key wakes it: one key, both ways
        return 0
    pane = next((p for p in call("pane.list").get("panes", []) if p["pane_id"] == pane_id), None)
    if pane is None:
        notify("Snooze", "No pane to snooze here.")
        return 1
    if not pane.get("agent"):
        notify("Snooze", "This pane has no agent, so there is nothing to hide.")
        return 1
    detail = " · ".join(part for part in (pane.get("agent"), context.get("workspace_label")) if part)
    env = {"SNOOZE_KIND": "pane", "SNOOZE_TARGET": pane_id, "SNOOZE_LABEL": pane_label(pane), "SNOOZE_DETAIL": detail}
    return open_picker(env, height)


# ── CLI ──────────────────────────────────────────────────────────────────────

USAGE = """usage: snooze.py <command>
  action agent|workspace          what the herdr actions run (opens the duration picker)
  action wake                     wake the focused pane's agent, no popup
  picker                          the popup itself
  tick [--force]                  reconcile tokens and the agent view
  toggle                          Agents panel: only snoozed <-> active
  snooze <pane_id|workspace:ID> <duration>
  wake --all | <pane_id|workspace:ID>...
  list"""


def main(argv):
    if not argv:
        print(USAGE)
        return 2
    command, args = argv[0], argv[1:]

    if command == "action" and args:
        return run_action(args[0])
    if command == "picker":
        return run_picker()

    if command == "tick":
        if is_idle(read_state()):
            # Nothing to do. If a file is there anyway (every entry in it was
            # malformed and dropped), remove it: while it exists, run.sh starts
            # Python on every focus change, forever.
            if os.path.exists(os.path.join(state_dir(), "snoozed.json")):
                with locked_state():
                    pass
            return 0
        with locked_state() as state:
            sync(state, load_config(), force="--force" in args)
        return 0

    if command == "toggle":
        config = load_config()
        with locked_state() as state:
            entering = state["view"].get("showing") != "snoozed"
            if entering and state["panes"]:
                state["view"]["showing"] = "snoozed"
                state["view"]["from_pane"] = invocation_context().get("focused_pane_id") or focused_pane(
                    call("pane.list").get("panes", [])
                )
            else:
                state["view"].pop("showing", None)
                state["view"].pop("from_pane", None)
            if state["panes"]:
                sync(state, config)
            showing = state["view"].get("showing") == "snoozed"
            if entering and not showing:
                # Asked for the snoozed list and didn't get it: nothing was
                # snoozed, or the last deadline had passed and this very sync
                # cleared it. A key press that does nothing needs a reason.
                notify("Snooze", "Nothing is snoozed.")
            log("toggle: showing", "snoozed" if showing else "active")
        return 0

    if command == "snooze" and len(args) == 2:
        kind, target = ("workspace", args[0].split(":", 1)[1]) if args[0].startswith("workspace:") else ("pane", args[0])
        until = parse_until(args[1])
        covered = snooze(kind, target, until, load_config())
        log("snoozed %s %s (%d pane(s)) until %s" % (kind, target, covered, until.strftime("%Y-%m-%d %H:%M:%S")))
        return 0 if covered else 1

    if command == "wake" and args:
        everything = "--all" in args
        targets = [a for a in args if a != "--all"]
        woken = wake(
            load_config(),
            pane_ids=[t for t in targets if not t.startswith("workspace:")],
            workspace_ids=[t.split(":", 1)[1] for t in targets if t.startswith("workspace:")],
            everything=everything,
        )
        log("woke %d pane(s)" % woken)
        return 0

    if command == "list":
        state, now = read_state(), now_ms()
        for wid, space in sorted(state["workspaces"].items()):
            print("workspace:%s\t%s left\tuntil %s" % (wid, format_left(space["until"] - now), from_ms(space["until"]).strftime("%Y-%m-%d %H:%M")))
        for pid, entry in sorted(state["panes"].items()):
            print("%s\t%s left\tuntil %s\t(%s)" % (pid, format_left(entry["until"] - now), from_ms(entry["until"]).strftime("%Y-%m-%d %H:%M"), entry.get("via")))
        if is_idle(state):
            print("nothing snoozed")
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ValueError as problem:
        print("snooze:", problem, file=sys.stderr)
        sys.exit(2)
    except (OSError, HerdrError) as problem:
        print("snooze: herdr call failed:", problem, file=sys.stderr)
        sys.exit(1)
