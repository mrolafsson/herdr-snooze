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
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timedelta

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "herdr-snooze"
SOURCE = "plugin:" + PLUGIN_ID
TOKEN = "snoozed"

MAX_TTL_MS = 86400000  # herdr's cap on a metadata token's ttl_ms
REFRESH_MARGIN_MS = 12 * 3600 * 1000  # re-report long snoozes this far before the token dies
MAX_SNOOZE_S = 366 * 86400

DEFAULT_DURATIONS = ["15m", "1h", "3h", "6h", "1d", "3d", "1w"]
DEFAULT_BADGE = "\U0001F4A4"  # 💤

# Token names aren't owned in herdr: any plugin could write `snoozed`. So every
# snoozed pane carries two: `snoozed`, the badge text people show in their
# sidebar ("$snoozed"), and MARK, which is snooze's alone. Filters and "is this
# ours?" go by MARK; `snoozed` is only for display.
MARK = "herdr_snooze"

VIEW_FILTER = {"op": "not", "filter": {"op": "exists", "field": {"token": MARK}}}
# The snoozed list also lets blocked agents through: it is the one panel state
# that hides your active agents, and one of them needing you is exactly what
# the panel exists to show.
SNOOZED_LIST_FILTER = {"op": "any", "filters": [
    {"op": "exists", "field": {"token": MARK}},
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


def plugin_state_dir():
    """The plugin's state directory. herdr gives one per plugin, shared by
    every herdr session on the machine."""
    return (
        os.environ.get("SNOOZE_STATE_DIR")
        or os.environ.get("HERDR_PLUGIN_STATE_DIR")
        or os.path.join(_herdr_state_root(), PLUGIN_ID)
    )


def default_socket():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "herdr", "herdr.sock")


def socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or default_socket()


# A random ID snooze keeps next to the herdr session's socket, naming this
# incarnation of the session.
SESSION_ID_FILE = ".herdr-snooze-session"


def session_id_path(sock):
    """Where a socket's session ID lives. herdr's own sessions each have a
    directory with one herdr.sock in it, and keep the 0.1.1 name. A socket
    named anything else (a custom HERDR_SOCKET_PATH, perhaps one of several in
    /tmp) gets a file of its own, so sockets sharing a directory don't share
    an identity."""
    home, name = os.path.split(sock)
    if name == "herdr.sock":
        return os.path.join(home, SESSION_ID_FILE)
    return os.path.join(home, "%s-%s" % (SESSION_ID_FILE, name))


def read_session_id(sock):
    try:
        with open(session_id_path(sock), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def session_id(sock):
    """The ID of the herdr session listening on `sock`, created on first use.
    A server restart keeps its directory and so the ID (snoozes are meant to
    survive it). Deleting a named session removes its directory, and with it
    the ID: a new session of the same name — whose panes herdr numbers from
    w1:p1 again — gets a new one, so it never reads the old one's records.
    None if the directory can't be written (then only the socket identifies
    the session)."""
    existing = read_session_id(sock)
    if existing:
        return existing
    path = session_id_path(sock)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(uuid.uuid4().hex + "\n")
        try:
            # link, not rename: it fails if another process got there first,
            # and nobody ever reads a half-written file.
            os.link(tmp, path)
        except FileExistsError:
            pass
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)
    return read_session_id(sock)


def session_identity(sock=None):
    """What makes this herdr session this one: its socket and its session ID."""
    sock = os.path.realpath(sock or socket_path())
    return {"socket": sock, "id": session_id(sock)}


def session_key(identity=None):
    """A short key for one herdr session, from session_identity().

    Pane and workspace IDs only mean something inside one herdr session, but
    the plugin's state directory is shared by all of them: without a key per
    session, a second session would read the first one's snoozes, hide its
    own unrelated `w1:p1` or delete the first's records as closed panes."""
    ident = identity or session_identity()
    return hashlib.sha1(json.dumps(ident, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def state_dir():
    """This session's state directory: sessions/<key>/ in the plugin's. run.sh's
    fast path must agree on where state can live: every sessions/*/, plus
    0.1.0's single file at the top (see recover_legacy)."""
    return os.path.join(plugin_state_dir(), "sessions", session_key())


def config_dir():
    if os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        return os.environ["HERDR_PLUGIN_CONFIG_DIR"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "herdr", "plugins", "config", PLUGIN_ID)


# herdr's agent-view sort, as agent.view.set accepts it (src/api/schema/agents.rs):
# anything else and herdr refuses the whole view, on every sync.
SORT_FIELDS = {"workspace_order", "tab_order", "pane_order", "attention", "status", "agent", "seen", "state_change_seq"}


def _sort_key_ok(key):
    field = key.get("field") if isinstance(key, dict) else None
    token = field.get("token") if isinstance(field, dict) and len(field) == 1 else None
    named = isinstance(field, str) and field in SORT_FIELDS
    return (named or isinstance(token, str)) and key.get("order", "asc") in ("asc", "desc")


def _sort_ok(value):
    return isinstance(value, list) and all(_sort_key_ok(key) for key in value)


# Each setting: its default, and what a valid value looks like. A wrong value
# falls back to the default, with a note in the plugin log, instead of
# crashing a hook halfway through a change (and every hook after it).
CONFIG_SCHEMA = {
    "durations": (lambda: list(DEFAULT_DURATIONS),
                  lambda v: isinstance(v, list) and v and all(isinstance(d, (str, int)) for d in v)),
    "badge": (lambda: DEFAULT_BADGE, lambda v: isinstance(v, str)),
    "notify": (lambda: False, lambda v: isinstance(v, bool)),
    "sort": (lambda: None, lambda v: v is None or _sort_ok(v)),
    "views": (lambda: None, lambda v: v is None or (isinstance(v, dict) and all(
        isinstance(labels, dict) and all(_sort_ok(s) for s in labels.values()) for labels in v.values()))),
    "auto_return": (lambda: True, lambda v: isinstance(v, bool)),
}


def load_config():
    config = {key: default() for key, (default, _valid) in CONFIG_SCHEMA.items()}
    path = os.path.join(config_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            user = json.load(handle)
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as problem:
        log("config.json ignored, using defaults:", problem)
        return config
    if not isinstance(user, dict):
        log("config.json ignored: it must be a JSON object")
        return config
    for key, value in user.items():
        if key not in CONFIG_SCHEMA:
            continue
        if CONFIG_SCHEMA[key][1](value):
            config[key] = value
        else:
            log("config.json: %r has an invalid value; using the default" % key)
    return config


# ── herdr socket ─────────────────────────────────────────────────────────────


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code


def call(method, params=None, timeout=5.0):
    """One request per connection: the server answers a line and hangs up."""
    path = socket_path()
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
    # Elapsed time, not wall-clock arithmetic: across a daylight-saving change
    # "2h" must still mean two hours from now (a clock time like 9am above
    # stays a clock time).
    return datetime.fromtimestamp(now.timestamp() + seconds)


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
    return {"v": 1, "panes": {}, "workspaces": {}, "view": {}, "timer": None, "timer_pid": None}


# A view record meaning "we may hold the view, but don't know what we set":
# the next sync then asks herdr and clears it if it's ours.
RECOVER = {"applied": "recover"}

# What each field of the view record may hold (plan_view reads them all).
VIEW_FIELDS = {"applied": str, "inherited": (dict, type(None)), "showing": str, "from_pane": (str, type(None))}
INHERITED_FIELDS = {"source": (str, type(None)), "label": (str, type(None))}


def valid_view(view):
    """Whether a saved view record has only fields of the types plan_view
    reads, all the way down (a list where a label belongs crashed it)."""
    if not all(isinstance(view[k], types) for k, types in VIEW_FIELDS.items() if k in view):
        return False
    inherited = view.get("inherited")
    return inherited is None or all(
        isinstance(inherited[k], types) for k, types in INHERITED_FIELDS.items() if k in inherited
    )


def read_state():
    """The state file, or an empty state if there is none. A file that is
    there but can't be read in full (corrupt, a hand edit, a crash mid-write)
    is *not* taken as "nothing snoozed": tokens and our view may still be live
    in herdr, and dropping them unexamined could leave agents hidden with no
    record left to un-hide them. Such a state carries RECOVER, so the next sync
    fails open: it clears tokens it has no record for and our view."""
    try:
        with open(os.path.join(state_dir(), "snoozed.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return empty_state()
    except (OSError, ValueError):
        state = empty_state()
        state["view"] = dict(RECOVER)
        return state
    state = empty_state()
    # Snooze writes every key, every time, and deletes the file when idle: a
    # file that exists without them (or with the wrong types) is damaged,
    # never a quiet "nothing snoozed".
    damaged = not isinstance(data, dict) or any(not isinstance(data.get(k), dict) for k in ("panes", "workspaces", "view"))
    if isinstance(data, dict):
        # Every hook reads this file; one malformed entry (a hand edit, a
        # half-understood older version) must not make all of them crash.
        for key in ("panes", "workspaces"):
            if isinstance(data.get(key), dict):
                state[key] = {
                    name: entry for name, entry in data[key].items()
                    if isinstance(entry, dict) and isinstance(entry.get("until"), int)
                }
                damaged = damaged or len(state[key]) != len(data[key])
        if isinstance(data.get("view"), dict):
            view = dict(data["view"])
            if valid_view(view):
                state["view"] = view
            else:
                damaged = True  # its fields would crash planning: recover instead
        if isinstance(data.get("timer"), int):
            state["timer"] = data["timer"]
        if isinstance(data.get("timer_pid"), int):
            state["timer_pid"] = data["timer_pid"]
    if damaged and not state["view"]:
        state["view"] = dict(RECOVER)
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
        ident_path = os.path.join(root, "session.json")
        if not os.path.exists(ident_path):
            # Which session this directory belongs to, so prune_stale_sessions
            # can tell when that session no longer exists. Written under the
            # lock (the pruner takes it too), under a name only we use.
            tmp = "%s.%d.tmp" % (ident_path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(session_identity(), handle)
            os.replace(tmp, ident_path)
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


def prune_stale_sessions():
    """Remove the state of herdr sessions that no longer exist.

    A deleted session's directory would otherwise stay forever: its file keeps
    run.sh starting Python in every session's hooks. Its records are safe to
    drop: tokens and views die with the server that held them, and a
    recreated session of the same name is a different one (session_key)."""
    root = os.path.join(plugin_state_dir(), "sessions")
    try:
        keys = os.listdir(root)
    except OSError:
        return
    mine = session_key()
    for key in keys:
        if key == mine:
            continue
        path = os.path.join(root, key)
        try:
            with open(os.path.join(path, "session.json"), encoding="utf-8") as handle:
                ident = json.load(handle)
            sock, recorded = ident["socket"], ident["id"]
        except (OSError, ValueError, KeyError, TypeError):
            # No identity (yet — it may be mid-creation) or can't read it:
            # unknown is never proof the session is gone.
            continue
        home = os.path.dirname(sock)
        if recorded is None:
            continue  # it couldn't write an ID: nothing to compare
        if os.path.isdir(home) and read_session_id(sock) == recorded:
            continue  # alive (running or just stopped)
        # Its directory is gone, or holds another incarnation's ID. Take its
        # lock, so nothing is mid-write; if it's busy, it isn't stale.
        try:
            with open(os.path.join(path, "lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                log("removing state of a herdr session that no longer exists:", key)
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


# 0.1.0 kept one state file for every session, at the top of the plugin's
# state directory. Its records may belong to any session, so none adopts them.


def running_sessions():
    """The sockets of every running herdr session, or None if herdr can't
    say (then nothing may be assumed about the others)."""
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        out = subprocess.run([herdr, "session", "list", "--json"], capture_output=True, text=True, timeout=5, check=True)
        sessions = json.loads(out.stdout).get("sessions", [])
        return [s["socket_path"] for s in sessions if s.get("running") and s.get("socket_path")]
    except (OSError, ValueError, AttributeError, KeyError, TypeError, subprocess.SubprocessError):
        return None


@contextlib.contextmanager
def in_session(sock):
    """Act as the herdr session behind `sock`: its state, its socket."""
    old = os.environ.get("HERDR_SOCKET_PATH")
    os.environ["HERDR_SOCKET_PATH"] = sock
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("HERDR_SOCKET_PATH", None)
        else:
            os.environ["HERDR_SOCKET_PATH"] = old


def recover_legacy(config):
    """After upgrading from 0.1.0: fail open in every running session. For
    each one whose own state is idle, anything of ours still in it (our view,
    a token with no record) came from 0.1.0 — clear it, so agents reappear
    rather than stay hidden by a record no session will read again. Done
    through each session's own socket, by whichever session ticks first, so a
    quiet session isn't left out. A session that isn't running needs nothing:
    its view and tokens died with its server.

    The old file goes only once every running session has recovered, with
    every token clear confirmed; until then (herdr unreachable, a session
    refusing), it stays. One process recovers at a time (the others skip
    straight past), and after a failure the next attempt waits, doubling
    from LEGACY_RETRY_S up to LEGACY_RETRY_MAX_S: a refusing session must not
    turn every hook into seconds of retries. Sessions started with their own
    HERDR_SOCKET_PATH aren't listed by herdr, so aren't reached (see the
    README)."""
    root = plugin_state_dir()
    legacy = os.path.join(root, "snoozed.json")
    if not os.path.exists(legacy):
        return
    retry_path = os.path.join(root, "legacy-retry.json")
    try:
        with open(retry_path, encoding="utf-8") as handle:
            retry = json.load(handle)
        if time.time() < float(retry["at"]):
            return  # backing off after a failure
    except (OSError, ValueError, KeyError, TypeError):
        retry = {}
    with open(os.path.join(root, "legacy.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another process is recovering right now
        complete = _recover_every_session(config)
        if complete:
            for path in (legacy, retry_path):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)
        else:
            delay = min(LEGACY_RETRY_MAX_S, max(LEGACY_RETRY_S, float(retry.get("delay", 0)) * 2))
            with contextlib.suppress(OSError), open(retry_path, "w", encoding="utf-8") as handle:
                json.dump({"at": time.time() + delay, "delay": delay}, handle)


LEGACY_RETRY_S = 30
LEGACY_RETRY_MAX_S = 3600


def _recover_every_session(config):
    """Returns True when every running session is known to be recovered."""
    socks = running_sessions()
    complete = socks is not None
    socks = list(socks or [])
    if os.path.realpath(socket_path()) not in [os.path.realpath(s) for s in socks]:
        socks.append(socket_path())
    for sock in socks:
        with in_session(sock):
            marker = os.path.join(state_dir(), "legacy-recovered")
            if os.path.exists(marker):
                continue
            try:
                with locked_state() as state:
                    if is_idle(state):
                        state["view"] = dict(RECOVER)
                    sync(state, config, strict=True)
                with open(marker, "w", encoding="utf-8") as handle:
                    handle.write("0.1.0 state recovered\n")
                log("recovered from 0.1.0 in", sock)
            except (OSError, ValueError, HerdrError) as problem:
                log("0.1.0 recovery not done yet in", sock, "-", problem)
                complete = False
    return complete


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
    follow_terminals(state, panes, live)

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
                state["panes"][pid] = {
                    "until": space["until"], "via": "workspace", "workspace_id": wid, "dirty": True,
                    "agent_id": agent_identity(pane), "terminal_id": pane.get("terminal_id"), "since": now,
                }
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
        if replaced(entry, pane):
            space = state["workspaces"].get(entry.get("workspace_id"))
            if entry.get("via") == "workspace" and space is not None:
                # A snoozed space covers whatever agent runs in it: keep it
                # hidden, as this agent's own record.
                entry["agent_id"] = agent_identity(pane)
                entry.pop("agent_gone", None)
            else:
                # The agent that was snoozed exited and another started in
                # the same pane: the snooze was for that one, not this.
                ops.append(("clear", pid))
                del state["panes"][pid]
                continue
        present = MARK in (pane.get("tokens") or {})
        if _token_due(entry, now, present=present and not force):
            ops.append(_token_op("report", pid, entry, now, badge))

    # A token with no record behind it (state file lost, a run that died
    # halfway) is an agent hidden for up to a day that no list shows. MARK is
    # ours alone; a `snoozed` token without it is taken for ours only if 0.1.x
    # could have written it (it starts with our badge): another plugin's stays.
    cleared = {op[1] for op in ops if op[0] == "clear"}
    for pid, pane in live.items():
        tokens = pane.get("tokens") or {}
        ours = MARK in tokens or (TOKEN in tokens and looks_ours(tokens[TOKEN], badge))
        if pid not in state["panes"] and pid not in cleared and ours:
            ops.append(("clear", pid))
    return ops


def looks_ours(value, badge):
    """Whether a `snoozed` token without MARK is one snooze 0.1.x wrote:
    _token_op always started it with the badge. With no badge configured
    there's nothing to tell by, so it's taken for ours."""
    return not badge or str(value).startswith(badge)


def follow_terminals(state, panes, live):
    """herdr gives a pane moved to another space or tab a new ID, but it keeps
    its terminal. Re-key what we hold for it by terminal_id, so the snooze (or
    a wake-up exception) follows the pane from whatever pane.list shows,
    whatever order concurrent hooks run in, and however many moves ago.
    Mutates `state`; everything learns its terminal while its pane is live."""
    by_terminal = {pane["terminal_id"]: pane for pane in panes if pane.get("terminal_id")}
    for pid in list(state["panes"]):
        entry = state["panes"][pid]
        pane = live.get(pid)
        if pane is not None:
            if pane.get("terminal_id"):
                entry["terminal_id"] = pane["terminal_id"]
            continue
        pane = by_terminal.get(entry.get("terminal_id"))
        if pane is None:
            continue  # closed: reconcile forgets it
        del state["panes"][pid]
        new = pane["pane_id"]
        if new in state["panes"]:
            continue  # it already has a record of its own
        if entry.get("via") == "workspace" and pane.get("workspace_id") != entry.get("workspace_id"):
            entry["via"] = "pane"  # it left the snoozed space: it keeps its own deadline
        entry["workspace_id"] = pane.get("workspace_id")
        entry["dirty"] = True  # the token may not have come along
        state["panes"][new] = entry
    for space in state["workspaces"].values():
        if not space.get("except"):
            continue
        known = space.get("except_terminals") if isinstance(space.get("except_terminals"), dict) else {}
        kept, terminals = [], {}
        for pid in space["except"]:
            pane = live.get(pid) or by_terminal.get(known.get(pid))
            if pane is None:
                continue  # closed: nothing left to except
            if pane["pane_id"] not in kept:
                kept.append(pane["pane_id"])
            if pane.get("terminal_id"):
                terminals[pane["pane_id"]] = pane["terminal_id"]
        space["except"], space["except_terminals"] = kept, terminals


def agent_identity(pane):
    """Which kind of agent runs in a pane, or None when there's no agent."""
    return {"agent": pane["agent"]} if pane.get("agent") else None


def replaced(entry, pane):
    """Whether the agent in the pane is not the one that was snoozed: another
    kind, or any agent after this pane was seen with none, or after herdr
    reported a new agent process there (agent_detected, below).

    pane.list carries no process identity, and agent_session changes on the
    same agent's /clear, resume and compact, so neither can tell. What herdr
    does give: pane.agent_detected each time it takes up an agent process,
    including a new one of the same kind (and not on /clear and the like);
    and `agent` cleared when the agent exits, or after several missed probes
    while something else holds the foreground, so an agent that hands its
    terminal to an editor for a while loses its snooze and shows again. A
    record from before 0.2 (no identity) adopts the agent it finds."""
    current = agent_identity(pane)
    if current is None:
        entry["agent_gone"] = True  # it hides nothing until an agent returns
        return False
    if entry.get("agent_gone"):
        return True
    recorded = entry.get("agent_id")
    if not isinstance(recorded, dict):
        entry["agent_id"] = current
        return False
    return recorded.get("agent") != current["agent"]


# A detection this soon after a snooze began is the snoozed agent's own,
# delivered late: the picker only offers panes whose agent herdr already had.
DETECTED_GRACE_MS = 10 * 1000


def agent_detected(state, pane_id, now):
    """herdr took up a new agent process in pane_id. If a snooze there predates
    it, the agent it was for is gone: the next reconcile treats the pane as
    replaced. Returns True if a record was marked."""
    entry = state["panes"].get(pane_id)
    if entry is None or now - entry.get("since", 0) < DETECTED_GRACE_MS:
        return False
    entry["agent_gone"] = True
    return True


def detected_pane(event):
    """The pane_id in a pane.agent_detected hook's event JSON, wherever herdr
    nests it, or None."""
    if isinstance(event, dict):
        if isinstance(event.get("pane_id"), str):
            return event["pane_id"]
        for value in event.values():
            found = detected_pane(value)
            if found:
                return found
    return None


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
        except (ValueError, TypeError, AttributeError):
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
                call("pane.report_metadata", {"pane_id": target, "source": SOURCE, "tokens": {TOKEN: op[2], MARK: "1"}, "ttl_ms": op[3]})
            elif kind == "clear":
                call("pane.report_metadata", {"pane_id": target, "source": SOURCE, "tokens": {TOKEN: None, MARK: None}})
            elif kind == "ws_report":
                call("workspace.report_metadata", {"workspace_id": target, "source": SOURCE, "tokens": {TOKEN: op[2], MARK: "1"}, "ttl_ms": op[3]})
            elif kind == "ws_clear":
                call("workspace.report_metadata", {"workspace_id": target, "source": SOURCE, "tokens": {TOKEN: None, MARK: None}})
        except HerdrError as error:
            log("token op failed:", op, error)  # a pane closing mid-sync is not worth aborting for
            failed.append(op)
    return failed


def probe_view():
    # A clear whose source owns nothing changes nothing and reports the owner.
    return call("agent.view.clear", {"source": SOURCE + ".probe"})


TIMER_SCRIPT = 'sleep "$1" && exec /bin/sh "$2" tick --timer'


def _spawn_timer(delay_s):
    """Starts the sleeper and returns its PID (arm_timer keeps it)."""
    launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.sh")
    return subprocess.Popen(
        [TIMER_SHELL, "-c", TIMER_SCRIPT, TIMER_NAME, str(delay_s), launcher],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    ).pid


TIMER_SHELL = "/bin/sh"
TIMER_NAME = "snooze-timer"  # its $0, so a sleeper can be told apart from a reused PID
# At most this long between ticks while anything is snoozed, whatever the
# deadlines: herdr has no event for another plugin taking the agent view, so
# this is how snooze notices (and how a dead sleeper is noticed on a quiet
# machine).
SAFETY_MS = 5 * 60 * 1000


def timer_alive(pid):
    """Whether our sleeper is still there. A signal-0 probe: cheap, but a PID
    can be reused, so killing it goes through is_our_timer first."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # someone else's process has that PID now


def is_our_timer(pid):
    """Whether the process is one of our sleepers: its whole command line, not
    a word in it, before we'd stop its process group."""
    signature = "%s -c %s %s " % (TIMER_SHELL, TIMER_SCRIPT, TIMER_NAME)
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return out.stdout.startswith(signature)
    except (OSError, subprocess.SubprocessError):
        return False


def stop_timer(pid):
    if timer_alive(pid) and is_our_timer(pid):
        with contextlib.suppress(OSError):
            os.killpg(pid, 15)  # its own session/group: the sleep goes with it


def arm_timer(state, now, spawn=None):
    """One detached `sleep` that runs a tick at the next moment something needs
    doing (a deadline, a token due for refresh, or SAFETY_MS at the latest).

    herdr's TTL already un-hides the agent on time; what nothing does on time
    is *us* noticing: the label keeps its count, the view stays borrowed, and
    in the snoozed list the last agent expiring leaves an empty panel. And a
    snooze longer than the TTL cap must be re-reported before its token dies.
    Not a daemon: each sleeper ticks once and is gone. Its PID is kept, so a
    sleeper that died (killed, a machine sleep) is replaced by the next tick
    instead of being trusted until the snooze ends early."""
    entries = list(state["panes"].values()) + list(state["workspaces"].values())
    if not entries:
        stop_timer(state.get("timer_pid"))
        state["timer"], state["timer_pid"] = None, None
        return
    soonest = min(
        entry["until"] if entry.get("token_until", entry["until"]) >= entry["until"]
        else min(entry["until"], entry["token_until"] - REFRESH_MARGIN_MS + 1000)
        for entry in entries
    )
    current, pid = state.get("timer"), state.get("timer_pid")
    if isinstance(current, int) and now < current <= soonest and timer_alive(pid):
        return  # a live sleeper will tick in time, and re-arm then
    wake_at = min(soonest, now + SAFETY_MS)
    try:
        new_pid = (spawn or _spawn_timer)(max(1, (wake_at - now) // 1000 + 1))
    except OSError as error:
        log("timer not armed:", error)  # hooks still catch up, just later
        return
    stop_timer(pid)  # the one it replaces, if it's still sleeping
    state["timer"], state["timer_pid"] = wake_at, new_pid if isinstance(new_pid, int) else None


def focused_pane(panes, agents_only=False):
    return next(
        (pane["pane_id"] for pane in panes if pane.get("focused") and (pane.get("agent") or not agents_only)), None
    )


def sync(state, config, force=False, strict=False):
    """Reconcile state with herdr. strict (recovery) raises if herdr refused
    to clear any token, rather than letting the caller take it as done."""
    panes = call("pane.list").get("panes", [])
    now = now_ms()
    ops = reconcile(state, panes, now, force=force, badge=config["badge"])
    failed = send_token_ops(ops)
    for op in failed:
        # Workspace tokens can't be read back, so nothing else would notice.
        entry = state["workspaces" if op[0].startswith("ws_") else "panes"].get(op[1])
        if entry is not None and op[0] in ("report", "ws_report"):
            entry["dirty"] = True
    if strict and any(op[0] in ("clear", "ws_clear") for op in failed):
        raise HerdrError("recovery_incomplete", "herdr refused to clear %d token(s)" % len(failed))
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
    view_before = copy.deepcopy(state["view"])
    try:
        action = plan_view(
            state, owner, count,
            sort_override=config["sort"], badge=config["badge"], views=config["views"], blocked=blocked,
            owner_off=owner_turned_off(owner if foreign else state["view"].get("inherited")),
        )
    except Exception:
        # Whatever went wrong, never leave the view record half-planned:
        # "idle" would delete the file with our filter maybe still live.
        state["view"] = view_before or dict(RECOVER)
        raise
    if action:
        try:
            call("agent.view.set" if action[0] == "set" else "agent.view.clear", action[1])
        except (OSError, ValueError, HerdrError):
            # plan_view already recorded the view as changed. If this was the
            # last clear, that record would read "idle", the file would be
            # deleted, run.sh would stop starting Python — and herdr would
            # keep our filter, hiding agents, with nothing left to retry.
            # Keep what was true before, so the next hook tries again.
            state["view"] = view_before or dict(RECOVER)
            raise
    if ops or action:
        log("sync:", len(ops), "token op(s);", "view " + action[0] if action else "view unchanged")
    return panes


def snooze(kind, target, until, config, expect_agent=None):
    """kind is 'pane' or 'workspace'. Returns how many panes are now covered.

    expect_agent is the agent the picker showed. The popup can stay open a
    while; if that pane has since closed, moved (a new pane ID), or a
    different agent started in it, snoozing now would hide something the
    person never chose — so it's refused instead."""
    until_ms = to_ms(until)
    with locked_state() as state:
        if kind == "workspace":
            state["workspaces"][target] = {"until": until_ms, "dirty": True}
            for entry in state["panes"].values():
                if entry.get("workspace_id") == target:
                    entry.update({"until": until_ms, "via": "workspace", "dirty": True})
        else:
            record = {"until": until_ms, "via": "pane", "dirty": True, "since": now_ms()}
            if expect_agent is not None:
                pane = next((p for p in call("pane.list").get("panes", []) if p["pane_id"] == target), None)
                if pane is None:
                    raise ValueError("that pane moved or closed; open snooze on it again")
                if agent_identity(pane) is None:
                    raise ValueError("the agent there has exited; open snooze on it again")
                if replaced({"agent_id": expect_agent}, pane):
                    raise ValueError("a different agent is running there now; open snooze on it again")
                record["agent_id"] = agent_identity(pane)
                record["terminal_id"] = pane.get("terminal_id")
            state["panes"][target] = record
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
        expect = None
        if kind == "pane":
            try:
                expect = json.loads(os.environ.get("SNOOZE_AGENT") or "null")
            except ValueError:
                expect = None
        covered = snooze(kind, target, until, config, expect_agent=expect if isinstance(expect, dict) else None)
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
    env = {"SNOOZE_KIND": "pane", "SNOOZE_TARGET": pane_id, "SNOOZE_LABEL": pane_label(pane), "SNOOZE_DETAIL": detail,
           "SNOOZE_AGENT": json.dumps(agent_identity(pane))}
    return open_picker(env, height)


# ── CLI ──────────────────────────────────────────────────────────────────────

USAGE = """usage: snooze.py <command>
  action agent|workspace          what the herdr actions run (opens the duration picker)
  action wake                     wake the focused pane's agent, no popup
  picker                          the popup itself
  tick [--force|--detected|--timer]  reconcile tokens and the agent view
  toggle                          Agents panel: only snoozed <-> active
  snooze <pane_id|workspace:ID> <duration>
  wake --all | <pane_id|workspace:ID>...
  list"""


def _try_lock(handle):
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def tick(args):
    """What every hook runs. Hooks fire in bursts (each focus change, each
    status flip, all at once: herdr runs them concurrently): a tick that
    finds another one running leaves a note and exits, and the running one
    goes again — a burst is a couple of passes, not a queue of processes each
    waiting on the lock and on herdr. Every pass reads everything from herdr,
    so plain ticks carry nothing a later pass would miss. Two run regardless:
    the startup --force (it resets what a restart left behind) and a
    --detected one, whose event (a new agent process in a pane) no later pass
    could see.

    No note is ever stranded: the runner looks again after unlocking, and a
    tick that left a note tries the lock once more, so either the note's
    writer runs or the lock's holder will see the note after unlocking.

    A --timer tick is the detached sleeper's; if it fails (herdr busy or
    restarting), nothing else may come along to arm the next one, so it arms a
    retry itself while herdr's socket is there."""
    try:
        return _tick(args)
    except Exception:
        if "--timer" in args and os.path.exists(socket_path()) and not is_idle(read_state()):
            with contextlib.suppress(OSError):
                _spawn_timer(SAFETY_MS // 1000)
        raise


def _tick(args):
    root = state_dir()
    if "--detected" in args:
        return _tick_once(args, detected=detected_pane(hook_event()))
    if "--force" in args or not os.path.isdir(root):
        return _tick_once(args)
    pending = os.path.join(root, "tick.pending")
    with open(os.path.join(root, "tick.lock"), "a") as lock:
        while True:
            if not _try_lock(lock):
                with open(pending, "a"):
                    pass
                if not _try_lock(lock):
                    return 0  # its holder hasn't unlocked yet: it will see the note
            while True:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(pending)
                _tick_once(args)
                if not os.path.exists(pending):
                    break
            fcntl.flock(lock, fcntl.LOCK_UN)
            if not os.path.exists(pending):
                return 0


def hook_event():
    try:
        return json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "null")
    except ValueError:
        return None


def _tick_once(args, detected=None):
    prune_stale_sessions()
    if os.path.exists(os.path.join(plugin_state_dir(), "snoozed.json")):
        recover_legacy(load_config())
    if is_idle(read_state()):
        # Nothing to do. If a file is there anyway (every entry in it was
        # malformed and dropped), remove it: while it exists, run.sh starts
        # Python on every focus change, forever.
        if os.path.exists(os.path.join(state_dir(), "snoozed.json")):
            with locked_state():
                pass
        return 0
    with locked_state() as state:
        if detected and agent_detected(state, detected, now_ms()):
            log("a new agent process in", detected, "- its snooze was for the one before")
        sync(state, load_config(), force="--force" in args)
    return 0


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
        return tick(args)

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
