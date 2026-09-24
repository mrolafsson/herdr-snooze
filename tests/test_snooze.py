import contextlib
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import snooze  # noqa: E402

NOON = datetime(2026, 9, 20, 12, 0, 0)  # a Sunday
HOUR = 3600 * 1000
RADAR = "plugin:hhdebb.herdr-radar"


def live_token(value="z"):
    """The tokens a pane snooze has hidden carries (0.2+): display + MARK."""
    return {"snoozed": value, "herdr_snooze": "1"}


def pane(pid, workspace="w1", agent="claude", tokens=None):
    # herdr always sends terminal_id; it stays with the pane when it moves.
    return {"pane_id": pid, "terminal_id": "t-" + pid, "workspace_id": workspace, "agent": agent, "tokens": tokens or {}}


def moved(p, pid, workspace):
    """The same pane after a move: a new ID (and maybe space), the same terminal."""
    p.update({"pane_id": pid, "workspace_id": workspace})
    return p


def state(panes=None, workspaces=None, view=None):
    return {"v": 1, "panes": panes or {}, "workspaces": workspaces or {}, "view": view or {}, "timer": None}


class ParseUntil(unittest.TestCase):
    def after(self, text):
        return (snooze.parse_until(text, NOON) - NOON).total_seconds()

    def test_units(self):
        self.assertEqual(self.after("15m"), 900)
        self.assertEqual(self.after("1h"), 3600)
        self.assertEqual(self.after("1d"), 86400)
        self.assertEqual(self.after("1w"), 604800)
        self.assertEqual(self.after("2 hours"), 7200)
        self.assertEqual(self.after("1.5h"), 5400)

    def test_combined_and_bare_minutes(self):
        self.assertEqual(self.after("1h30m"), 5400)
        self.assertEqual(self.after("1h 30m"), 5400)
        self.assertEqual(self.after("90"), 5400)

    def test_clock_rolls_to_tomorrow_when_passed(self):
        self.assertEqual(snooze.parse_until("17:30", NOON), datetime(2026, 9, 20, 17, 30))
        self.assertEqual(snooze.parse_until("9am", NOON), datetime(2026, 9, 21, 9, 0))
        self.assertEqual(snooze.parse_until("9:15 pm", NOON), datetime(2026, 9, 20, 21, 15))
        self.assertEqual(snooze.parse_until("12am", NOON), datetime(2026, 9, 21, 0, 0))

    def test_tomorrow(self):
        self.assertEqual(snooze.parse_until("tomorrow", NOON), datetime(2026, 9, 21, 9, 0))
        self.assertEqual(snooze.parse_until("tomorrow 14:00", NOON), datetime(2026, 9, 21, 14, 0))

    def test_rejects_garbage(self):
        for text in ("", "soon", "5x", "0m", "25:00", "13pm", "9999w", "h"):
            with self.assertRaises(ValueError, msg=text):
                snooze.parse_until(text, NOON)

    def test_durations_are_elapsed_time_across_daylight_saving(self):
        # v0.2: "2h" across a DST change was 1h or 3h of real time.
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Madrid"
        snooze.time.tzset()
        try:
            for start in (datetime(2026, 3, 29, 1, 30), datetime(2026, 10, 25, 1, 30)):  # spring, autumn
                with self.subTest(start=start):
                    until = snooze.parse_until("2h", start)
                    self.assertEqual(until.timestamp() - start.timestamp(), 7200)
            # a clock time stays a clock time
            self.assertEqual(snooze.parse_until("9am", datetime(2026, 3, 29, 1, 30)).hour, 9)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            snooze.time.tzset()


class Formatting(unittest.TestCase):
    def test_badged(self):
        # an emoji badge covers one space in herdr's sidebar, so it gets two
        self.assertEqual(snooze.badged("💤", "1"), "💤  1")
        self.assertEqual(snooze.badged("z", "1"), "z 1")
        self.assertEqual(snooze.badged("", "1"), "1")
        self.assertEqual(snooze.badged("zz💤", "17:30"), "zz💤  17:30")

    def test_describe(self):
        self.assertEqual(snooze.describe("15m"), "15 minutes")
        self.assertEqual(snooze.describe("1h"), "1 hour")
        self.assertEqual(snooze.describe("1h30m"), "1 hour 30 minutes")
        self.assertEqual(snooze.describe("1w"), "1 week")
        self.assertEqual(snooze.describe("17:30"), "until 17:30")

    def test_format_until(self):
        self.assertEqual(snooze.format_until(datetime(2026, 9, 20, 17, 5), NOON), "17:05")
        self.assertEqual(snooze.format_until(datetime(2026, 9, 22, 9, 0), NOON), "Tue 09:00")
        self.assertEqual(snooze.format_until(datetime(2026, 10, 4, 9, 0), NOON), "4 Oct 09:00")

    def test_format_left(self):
        self.assertEqual(snooze.format_left(45 * 1000), "45s")
        self.assertEqual(snooze.format_left(12 * 60 * 1000), "12m")
        self.assertEqual(snooze.format_left(2 * HOUR + 14 * 60 * 1000), "2h 14m")
        self.assertEqual(snooze.format_left(76 * HOUR), "3d 4h")
        self.assertEqual(snooze.format_left(-5), "0s")

    def test_format_left_rounds_up(self):
        self.assertEqual(snooze.format_left(3 * 60 * 1000 - 800), "3m")  # just set: not "2m"
        self.assertEqual(snooze.format_left(60 * 60 * 1000 - 1), "1h")
        self.assertEqual(snooze.format_left(24 * HOUR - 1), "1d")
        self.assertEqual(snooze.format_left(1), "1s")


class Reconcile(unittest.TestCase):
    now = snooze.to_ms(NOON)

    def test_new_snooze_reports_token_with_ttl(self):
        st = state({"p1": {"until": self.now + HOUR, "dirty": True}})
        ops = snooze.reconcile(st, [pane("p1")], self.now, badge="z")
        self.assertEqual(ops, [("report", "p1", "z 13:00", HOUR)])
        self.assertEqual(st["panes"]["p1"]["token_until"], self.now + HOUR)
        self.assertNotIn("dirty", st["panes"]["p1"])

    def test_steady_state_sends_nothing(self):
        st = state({"p1": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        self.assertEqual(snooze.reconcile(st, [pane("p1", tokens=live_token())], self.now), [])

    def test_ttl_is_capped_and_refreshed_before_it_dies(self):
        week = self.now + 7 * 24 * HOUR
        st = state({"p1": {"until": week, "dirty": True}})
        ops = snooze.reconcile(st, [pane("p1")], self.now)
        self.assertEqual(ops[0][3], snooze.MAX_TTL_MS)

        live = [pane("p1", tokens=live_token())]
        self.assertEqual(snooze.reconcile(st, live, self.now + 11 * HOUR), [])  # 13h of token left
        later = self.now + 13 * HOUR  # 11h left: inside the refresh margin
        ops = snooze.reconcile(st, live, later)
        self.assertEqual([op[0] for op in ops], ["report"])
        self.assertEqual(st["panes"]["p1"]["token_until"], later + snooze.MAX_TTL_MS)

    def test_missing_token_is_restored(self):
        # server restart, or the token expired under a >24h snooze
        st = state({"p1": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        self.assertEqual([op[0] for op in snooze.reconcile(st, [pane("p1")], self.now)], ["report"])

    def test_force_re_reports_even_when_present(self):
        st = state({"p1": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        ops = snooze.reconcile(st, [pane("p1", tokens=live_token())], self.now, force=True)
        self.assertEqual([op[0] for op in ops], ["report"])

    def test_expired_is_cleared_and_forgotten(self):
        st = state({"p1": {"until": self.now - 1}})
        self.assertEqual(snooze.reconcile(st, [pane("p1", tokens=live_token())], self.now), [("clear", "p1")])
        self.assertEqual(st["panes"], {})

    def test_closed_pane_is_forgotten_silently(self):
        st = state({"gone": {"until": self.now + HOUR}})
        self.assertEqual(snooze.reconcile(st, [pane("p1")], self.now), [])
        self.assertEqual(st["panes"], {})

    def test_token_without_a_record_is_cleared(self):
        # state file lost, or a run that died after tokens were sent
        st = state({"p1": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        live = [pane("p1", tokens=live_token("z 12:00")), pane("ghost", tokens=live_token("z 12:00")),
                pane("old", tokens={"snoozed": "z 12:00"}), pane("plain")]  # "old": 0.1.x wrote no MARK
        self.assertEqual(snooze.reconcile(st, live, self.now, badge="z"), [("clear", "ghost"), ("clear", "old")])

    def with_session(self, pid, session, agent="claude"):
        p = pane(pid, agent=agent, tokens=live_token())
        p["agent_session"] = {"agent": agent, "kind": "id", "source": "x", "value": session} if session else None
        return p

    def snoozed_claude(self, **extra):
        return state({"p1": dict({"until": self.now + HOUR, "token_until": self.now + HOUR,
                                  "agent_id": {"agent": "claude"}}, **extra)})

    def test_a_new_agent_after_the_old_one_exited_does_not_inherit_the_snooze(self):
        # v0.2: agent A exits (herdr shows the pane with no agent), agent B
        # starts in the same pane; the snooze was A's.
        st = self.snoozed_claude()
        self.assertEqual(snooze.reconcile(st, [pane("p1", agent=None, tokens=live_token())], self.now), [])
        self.assertIn("p1", st["panes"], "the record hides nothing while no agent runs: kept")
        self.assertEqual(snooze.reconcile(st, [self.with_session("p1", "s-b")], self.now), [("clear", "p1")])
        self.assertEqual(st["panes"], {})

    def test_clear_resume_and_compact_keep_the_snooze(self):
        # Round 3: herdr gives the same running agent a new agent_session on
        # /clear, resume and compact; 0.2's first cut took that for a new agent.
        st = self.snoozed_claude()
        for session in ("s-a", "s-after-clear", None, "s-resumed"):
            self.assertEqual(snooze.reconcile(st, [self.with_session("p1", session)], self.now), [], session)
        self.assertIn("p1", st["panes"])

    def test_another_kind_of_agent_is_a_new_agent(self):
        st = self.snoozed_claude()
        self.assertEqual(snooze.reconcile(st, [self.with_session("p1", None, agent="codex")], self.now), [("clear", "p1")])

    def test_a_new_agent_in_a_snoozed_space_stays_hidden(self):
        # The space covers whatever runs in it: no clear-and-re-adopt flicker.
        st = state({"p1": {"until": self.now + HOUR, "token_until": self.now + HOUR, "via": "workspace",
                           "workspace_id": "w1", "agent_id": {"agent": "claude"}}},
                   {"w1": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        snooze.reconcile(st, [pane("p1", agent=None, tokens=live_token())], self.now)
        self.assertEqual(snooze.reconcile(st, [pane("p1", agent="codex", tokens=live_token())], self.now), [])
        self.assertEqual(st["panes"]["p1"]["agent_id"], {"agent": "codex"})
        self.assertNotIn("agent_gone", st["panes"]["p1"])

    def test_a_0_1_record_adopts_its_agent_and_gets_the_marker(self):
        # upgrading: no identity recorded, and the token has no MARK yet
        st = state({"p1": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        p = self.with_session("p1", "s-a")
        p["tokens"] = {"snoozed": "z 12:00"}
        ops = snooze.reconcile(st, [p], self.now)
        self.assertEqual([op[0] for op in ops], ["report"])  # re-reported, now with MARK
        self.assertEqual(st["panes"]["p1"]["agent_id"], {"agent": "claude"})
        self.assertEqual(st["panes"]["p1"]["terminal_id"], "t-p1")

    # ── v0.2: a moved pane gets a new ID and keeps its terminal ──

    def test_a_moved_pane_keeps_its_snooze_whatever_the_hook_order(self):
        # Round 3: a plain tick that saw the move before the move hook ran
        # forgot the record as closed. Now any pass finds it by terminal.
        st = self.snoozed_claude(via="pane", workspace_id="w1", terminal_id="t-p1")
        p = moved(pane("p1", tokens={}), "w2:p1", "w2")  # the token stayed behind
        ops = snooze.reconcile(st, [p], self.now)
        self.assertEqual(list(st["panes"]), ["w2:p1"])
        self.assertEqual([op[:2] for op in ops], [("report", "w2:p1")], "hidden again under its new ID")
        self.assertEqual(st["panes"]["w2:p1"]["workspace_id"], "w2")

    def test_two_quick_moves_in_any_order(self):
        # A → B → C with no pass in between (or the B hook running last):
        # only C is live, and the snooze lands there.
        st = self.snoozed_claude(via="pane", workspace_id="w1", terminal_id="t-p1")
        c = moved(pane("p1"), "C", "w3")
        snooze.reconcile(st, [c], self.now)
        self.assertEqual(list(st["panes"]), ["C"])
        snooze.reconcile(st, [c], self.now)  # the late hooks for A → B and B → C
        self.assertEqual(list(st["panes"]), ["C"])

    def test_leaving_a_snoozed_space_keeps_its_own_deadline(self):
        st = state({"p1": {"until": self.now + HOUR, "via": "workspace", "workspace_id": "w1", "terminal_id": "t-p1"}},
                   {"w1": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        snooze.reconcile(st, [pane("stay"), moved(pane("p1"), "w2:p1", "w2")], self.now)
        self.assertEqual(st["panes"]["w2:p1"]["via"], "pane")

    def test_a_woken_pane_stays_woken_when_it_moves_out_and_back(self):
        # Woken early out of a snoozed space, moved to another space and back:
        # herdr gives it a new ID each time (a move within a space keeps it).
        # A new ID that isn't on the except list would be re-adopted, hidden.
        st = state(workspaces={"w1": {"until": self.now + HOUR, "token_until": self.now + HOUR, "except": ["p1"]}})
        snooze.reconcile(st, [pane("p1"), pane("other", workspace="w2")], self.now)  # learns p1's terminal
        p = pane("p1")
        snooze.reconcile(st, [moved(p, "w2:p1", "w2"), pane("stay")], self.now)
        snooze.reconcile(st, [moved(p, "w1:p1-back", "w1"), pane("stay")], self.now)
        self.assertNotIn("w1:p1-back", st["panes"])
        self.assertEqual(st["workspaces"]["w1"]["except"], ["w1:p1-back"])

    def test_a_restart_gives_terminals_new_ids_and_known_panes_learn_them(self):
        # A cold restart keeps pane IDs but allocates new terminal IDs; a record
        # matched by pane ID picks up the new one, so a later move still works.
        st = self.snoozed_claude(via="pane", workspace_id="w1", terminal_id="t-old")
        p = pane("p1", tokens=live_token())
        p["terminal_id"] = "t-new"
        snooze.reconcile(st, [p], self.now)
        self.assertEqual(st["panes"]["p1"]["terminal_id"], "t-new")
        snooze.reconcile(st, [moved(p, "w2:p1", "w2")], self.now)
        self.assertEqual(list(st["panes"]), ["w2:p1"])

    def test_a_move_then_a_restart_before_any_tick_loses_the_snooze(self):
        # Documented limit: the pane.moved hook re-keys within milliseconds;
        # only a crash in that window leaves neither the old ID nor the old
        # terminal to find. The pane then shows again (never wrongly hidden).
        st = self.snoozed_claude(via="pane", workspace_id="w1", terminal_id="t-p1")
        p = moved(pane("p1"), "w2:p1", "w2")
        p["terminal_id"] = "t-restored"
        self.assertEqual(snooze.reconcile(st, [p], self.now), [])
        self.assertEqual(st["panes"], {})

    # ── v0.2: herdr's pane.agent_detected tells a new process of the same kind ──
    # Its payload as herdr 0.9.1 sends it (api/schema/events.rs, emitted from
    # app/api.rs emit_pane_state_update): on a release `agent` still names the
    # agent that left.

    @staticmethod
    def detected(pid, agent="claude", released=False):
        data = {"pane_id": pid, "workspace_id": "w1"}
        if agent:
            data["agent"] = agent
        if released:
            data.update(released=True, final_status="idle")
        return {"event": "pane.agent_detected", "data": data}

    def test_a_quick_restart_of_the_same_agent_drops_the_snooze(self):
        # Claude A exits and Claude B starts before herdr ever shows the pane
        # without an agent: A's release is what tells.
        st = self.snoozed_claude(since=self.now - HOUR)
        event = snooze.detected_event(self.detected("p1", released=True))
        self.assertTrue(snooze.agent_detected(st, event, self.now))
        self.assertEqual(snooze.reconcile(st, [self.with_session("p1", "s-b")], self.now), [("clear", "p1")])

    def test_a_release_right_after_snoozing_still_counts(self):
        # A replacement seconds after the snooze is still a replacement.
        st = self.snoozed_claude(since=self.now - 2000)
        event = snooze.detected_event(self.detected("p1", released=True))
        self.assertTrue(snooze.agent_detected(st, event, self.now))
        self.assertEqual(snooze.reconcile(st, [self.with_session("p1", "s-b")], self.now), [("clear", "p1")])

    def test_the_agent_leaving_right_after_snoozing_counts(self):
        st = self.snoozed_claude(since=self.now - 2000)
        self.assertTrue(snooze.agent_detected(st, snooze.detected_event(self.detected("p1", agent=None)), self.now))

    def test_a_late_detection_of_the_snoozed_agent_itself_is_ignored(self):
        st = self.snoozed_claude(since=self.now - 2000)
        self.assertFalse(snooze.agent_detected(st, snooze.detected_event(self.detected("p1")), self.now))
        self.assertEqual(snooze.reconcile(st, [self.with_session("p1", "s-a")], self.now), [])

    def test_an_agent_arriving_after_the_grace_drops_the_snooze(self):
        # The editor handoff: herdr cleared `agent`, then took it up again.
        st = self.snoozed_claude(since=self.now - HOUR)
        self.assertTrue(snooze.agent_detected(st, snooze.detected_event(self.detected("p1")), self.now))

    def test_a_release_from_before_the_snooze_is_not_about_it(self):
        # Round 4: A exits, B starts, you snooze B while A's release hook is
        # still waiting on the lock. The hook started before the snooze.
        st = self.snoozed_claude(since=self.now)
        event = snooze.detected_event(self.detected("p1", released=True))
        self.assertFalse(snooze.agent_detected(st, event, self.now - 500))
        self.assertNotIn("agent_gone", st["panes"]["p1"])

    def test_an_arrival_is_dated_by_when_its_hook_started_not_when_it_got_the_lock(self):
        # It started 2s into the snooze and waited out the picker's lock: still
        # the snoozed agent's own detection, however late it is handled.
        st = self.snoozed_claude(since=self.now - HOUR)
        event = snooze.detected_event(self.detected("p1"))
        self.assertFalse(snooze.agent_detected(st, event, self.now - HOUR + 2000))

    def test_detections_elsewhere_change_nothing(self):
        st = self.snoozed_claude(since=self.now - HOUR)
        self.assertFalse(snooze.agent_detected(st, snooze.detected_event(self.detected("p2", released=True)), self.now))
        self.assertNotIn("agent_gone", st["panes"]["p1"])

    def test_the_detected_pane_is_found_in_herdrs_event(self):
        event = snooze.detected_event(self.detected("w1:p3", released=True))
        self.assertEqual((event["pane_id"], event["agent"], event["released"]), ("w1:p3", "claude", True))
        self.assertIsNone(snooze.detected_event({"event": "x"}))
        self.assertIsNone(snooze.detected_event(None))

    def test_a_closed_pane_is_not_mistaken_for_a_moved_one(self):
        st = self.snoozed_claude(terminal_id="t-p1")
        self.assertEqual(snooze.reconcile(st, [pane("p2")], self.now), [])
        self.assertEqual(st["panes"], {})

    def test_another_plugins_token_of_the_same_name_is_left_alone(self):
        # token names aren't owned in herdr: only a value we'd write is ours
        live = [pane("theirs", tokens={"snoozed": "yes"}), pane("ours", tokens={"snoozed": "z 12:00"})]
        self.assertEqual(snooze.reconcile(state(), live, self.now, badge="z"), [("clear", "ours")])

    def test_expiry_clear_is_not_sent_twice(self):
        st = state({"p1": {"until": self.now - 1}})
        ops = snooze.reconcile(st, [pane("p1", tokens=live_token())], self.now)
        self.assertEqual(ops, [("clear", "p1")])

    def test_workspace_adopts_its_panes_including_later_ones(self):
        st = state(workspaces={"w1": {"until": self.now + HOUR, "dirty": True}})
        live = [pane("p1"), pane("shell", agent=None), pane("other", workspace="w2")]
        ops = snooze.reconcile(st, live, self.now)
        self.assertEqual(sorted(op[:2] for op in ops), [("report", "p1"), ("ws_report", "w1")])
        self.assertEqual(st["panes"]["p1"]["via"], "workspace")
        self.assertNotIn("other", st["panes"])
        self.assertNotIn("shell", st["panes"])  # a token on a shell hides nothing and makes counts lie

        live[0]["tokens"] = live_token()
        live.append(pane("p3"))  # an agent started later in the snoozed space
        self.assertEqual([op[:2] for op in snooze.reconcile(st, live, self.now)], [("report", "p3")])
        live[-1]["tokens"] = live_token()
        live[1]["agent"] = "claude"  # an agent detected later in a pane that was a shell
        self.assertEqual([op[:2] for op in snooze.reconcile(st, live, self.now)], [("report", "shell")])

    def test_space_with_no_panes_left_gets_its_badge_cleared(self):
        st = state(workspaces={"empty": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        self.assertEqual(snooze.reconcile(st, [pane("p1")], self.now), [("ws_clear", "empty")])
        self.assertEqual(st["workspaces"], {})

    def test_own_shorter_deadline_inside_a_snoozed_space_is_not_overruled(self):
        # space for 3h, one of its agents for 15 minutes: when the 15 minutes
        # are up it must stay visible, not get re-adopted for the rest of the 3h
        st = state({"p1": {"until": self.now + 900_000, "via": "pane", "token_until": self.now + 900_000}},
                   {"w1": {"until": self.now + 3 * HOUR, "token_until": self.now + 3 * HOUR, "except": ["p1"]}})
        later = self.now + 1_000_000
        self.assertEqual(snooze.reconcile(st, [pane("p1", tokens=live_token())], later), [("clear", "p1")])
        self.assertEqual(snooze.reconcile(st, [pane("p1")], later + 1000), [])
        self.assertNotIn("p1", st["panes"])

    def test_workspace_does_not_readopt_a_pane_woken_early(self):
        st = state(workspaces={"w1": {"until": self.now + HOUR, "token_until": self.now + HOUR, "except": ["p1"]}})
        snooze.reconcile(st, [pane("p1"), pane("p2")], self.now)
        self.assertEqual(list(st["panes"]), ["p2"])

    def test_expired_workspace_clears_its_token_and_panes(self):
        until = self.now - 1
        st = state({"p1": {"until": until, "via": "workspace", "workspace_id": "w1"}}, {"w1": {"until": until}})
        ops = snooze.reconcile(st, [pane("p1")], self.now)
        self.assertEqual(sorted(ops), [("clear", "p1"), ("ws_clear", "w1")])
        self.assertTrue(snooze.is_idle(st))


class PlanView(unittest.TestCase):
    snoozed = {"p1": {"until": 1}}

    def test_takes_a_free_view_and_clears_it_afterwards(self):
        st = state(dict(self.snoozed))
        kind, params = snooze.plan_view(st, {"active": False}, 1, badge="z")
        self.assertEqual((kind, params["source"], params["filter"], params["sort"]), ("set", snooze.SOURCE, snooze.VIEW_FILTER, []))
        self.assertEqual(params["label"], "z 1")

        st["panes"] = {}
        self.assertEqual(snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 0), ("clear", {"source": snooze.SOURCE}))
        self.assertEqual(st["view"], {})

    def test_borrows_radars_sort_and_hands_the_view_back(self):
        st = state(dict(self.snoozed))
        kind, params = snooze.plan_view(st, {"active": True, "source": RADAR, "label": "active"}, 1, badge="z")
        self.assertEqual(params["sort"], snooze.KNOWN_VIEWS[RADAR]["sorts"]["active"])
        self.assertEqual(params["label"], "active z 1")
        self.assertEqual(params["filter"], snooze.VIEW_FILTER)

        st["panes"] = {}
        kind, params = snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 0)
        self.assertEqual(kind, "set")
        self.assertEqual(params, {"source": RADAR, "label": "active", "sort": snooze.KNOWN_VIEWS[RADAR]["sorts"]["active"]})
        self.assertNotIn("filter", params)

    def test_idempotent_while_we_hold_it(self):
        st = state(dict(self.snoozed))
        snooze.plan_view(st, {"active": True, "source": RADAR, "label": "active"}, 1)
        self.assertIsNone(snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 1))
        # the count is part of the label, so a second snooze re-sets the view
        self.assertEqual(snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 2)[0], "set")

    def test_follows_radar_when_it_retakes_the_view_in_another_mode(self):
        st = state(dict(self.snoozed))
        snooze.plan_view(st, {"active": True, "source": RADAR, "label": "active"}, 1)
        kind, params = snooze.plan_view(st, {"active": True, "source": RADAR, "label": "recent"}, 1)
        self.assertEqual((kind, params["sort"]), ("set", snooze.KNOWN_VIEWS[RADAR]["sorts"]["recent"]))

    def test_reapplies_after_the_view_was_lost(self):
        st = state(dict(self.snoozed))
        snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 1)
        self.assertEqual(snooze.plan_view(st, {"active": False}, 1)[0], "set")

    def test_owner_turned_off_drops_the_borrowed_sort(self):
        st = state(dict(self.snoozed))
        snooze.plan_view(st, {"active": True, "source": RADAR, "label": "active"}, 1)
        kind, params = snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 1, owner_off=True)
        self.assertEqual(params["sort"], [])
        st["panes"] = {}
        self.assertEqual(snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 0, owner_off=True)[0], "clear")

    def test_unknown_owner_is_cleared_not_impersonated(self):
        st = state(dict(self.snoozed))
        kind, params = snooze.plan_view(st, {"active": True, "source": "plugin:other", "label": "x"}, 1)
        self.assertEqual(params["sort"], [])
        st["panes"] = {}
        self.assertEqual(snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 0)[0], "clear")

    def test_borrows_agent_inbox_sort_and_hands_it_back(self):
        inbox = {"active": True, "source": "herdr-agent-inbox", "label": "Inbox"}
        want = snooze.KNOWN_VIEWS["herdr-agent-inbox"]["sorts"]["Inbox"]
        st = state(dict(self.snoozed))
        self.assertEqual(snooze.plan_view(st, inbox, 1)[1]["sort"], want)
        st["panes"] = {}
        self.assertEqual(
            snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 0),
            ("set", {"source": "herdr-agent-inbox", "label": "Inbox", "sort": want}),
        )

    def test_config_views_teach_an_unknown_owner(self):
        other = {"active": True, "source": "plugin:other", "label": "x"}
        views = {"plugin:other": {"x": [{"field": "status", "order": "asc"}]}}
        st = state(dict(self.snoozed))
        self.assertEqual(snooze.plan_view(st, other, 1, views=views)[1]["sort"], views["plugin:other"]["x"])
        st["panes"] = {}
        kind, params = snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 0, views=views)
        self.assertEqual((kind, params["source"]), ("set", "plugin:other"))
        # a malformed table is ignored rather than trusted
        self.assertIsNone(snooze.known_sort(other, {"plugin:other": {"x": "nope"}}))

    def test_toggle_shows_only_snoozed_keeping_the_borrowed_sort(self):
        radar = {"active": True, "source": RADAR, "label": "active"}
        mine = {"active": True, "source": snooze.SOURCE}
        st = state(dict(self.snoozed))
        hidden = snooze.plan_view(st, radar, 2, badge="z")[1]
        self.assertEqual((hidden["filter"], hidden["label"]), (snooze.VIEW_FILTER, "active z 2"))

        st["view"]["showing"] = "snoozed"
        kind, shown = snooze.plan_view(st, mine, 2, badge="z")
        self.assertEqual(kind, "set")  # the filter is part of the fingerprint
        self.assertEqual(shown["filter"], snooze.SNOOZED_LIST_FILTER)
        self.assertEqual(shown["label"], "z snoozed only · 2")
        self.assertEqual(shown["sort"], hidden["sort"])
        self.assertEqual(st["view"]["showing"], "snoozed")  # survives the next tick
        self.assertIsNone(snooze.plan_view(st, mine, 2, badge="z"))

        st["view"].pop("showing")
        self.assertEqual(snooze.plan_view(st, mine, 2, badge="z")[1]["filter"], snooze.VIEW_FILTER)

    def test_toggle_ends_with_the_last_snooze(self):
        st = state(dict(self.snoozed))
        snooze.plan_view(st, {"active": True, "source": RADAR, "label": "active"}, 1)
        st["view"]["showing"] = "snoozed"
        snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 1)
        st["panes"] = {}
        kind, params = snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 0)
        self.assertEqual((kind, params["source"]), ("set", RADAR))  # handed back, not left on an empty list
        self.assertNotIn("filter", params)
        self.assertEqual(st["view"], {})

    def test_config_sort_wins(self):
        pinned = [{"field": "status", "order": "asc"}]
        st = state(dict(self.snoozed))
        params = snooze.plan_view(st, {"active": True, "source": RADAR, "label": "active"}, 1, sort_override=pinned)[1]
        self.assertEqual(params["sort"], pinned)

    def test_leaves_someone_elses_view_alone_when_nothing_is_snoozed(self):
        self.assertIsNone(snooze.plan_view(state(), {"active": True, "source": RADAR, "label": "active"}, 0))


class SettleShowing(unittest.TestCase):
    """The snoozed list ends by itself; walk the real sequences."""

    def showing(self, from_pane="work"):
        return state({"zz": {"until": 1}}, view={"showing": "snoozed", "from_pane": from_pane})

    def test_stays_while_you_are_still_where_you_opened_it(self):
        st = self.showing()
        self.assertFalse(snooze.settle_showing(st, "work"))
        self.assertEqual(st["view"]["showing"], "snoozed")

    def test_stays_while_you_look_at_snoozed_agents(self):
        st = self.showing()
        self.assertFalse(snooze.settle_showing(st, "zz"))
        self.assertEqual(st["view"]["showing"], "snoozed")

    def test_returns_when_focus_moves_to_an_active_pane(self):
        st = self.showing()
        self.assertTrue(snooze.settle_showing(st, "elsewhere"))
        self.assertNotIn("showing", st["view"])
        self.assertNotIn("from_pane", st["view"])

    def test_going_back_to_the_origin_after_visiting_a_snoozed_agent_also_returns(self):
        st = self.showing()
        snooze.settle_showing(st, "zz")
        self.assertTrue(snooze.settle_showing(st, "work"))

    def test_waking_the_agent_you_are_on_returns(self):
        st = self.showing()
        snooze.settle_showing(st, "zz")
        st["panes"] = {"other": {"until": 1}}  # zz was woken; others remain
        self.assertTrue(snooze.settle_showing(st, "zz"))

    def test_no_focus_information_changes_nothing(self):
        st = self.showing()
        self.assertFalse(snooze.settle_showing(st, None))
        self.assertFalse(snooze.settle_showing(state({"zz": {"until": 1}}), "elsewhere"))  # not showing at all

    def test_plan_view_keeps_the_origin_across_ticks(self):
        st = self.showing()
        snooze.plan_view(st, {"active": True, "source": snooze.SOURCE}, 1)
        self.assertEqual(st["view"]["from_pane"], "work")
        self.assertEqual(st["view"]["showing"], "snoozed")


class ArmTimer(unittest.TestCase):
    now = snooze.to_ms(NOON)

    SAFETY_S = snooze.SAFETY_MS // 1000

    def setUp(self):
        # A pretend process table: a sleeper is "alive" until we say it died.
        self.alive, self.stopped, self.next_pid = set(), [], [1000]
        for patcher in (mock.patch.object(snooze, "timer_alive", lambda pid: pid in self.alive),
                        mock.patch.object(snooze, "stop_timer", lambda pid: self.stopped.append(pid))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def arm(self, st, now=None):
        delays = []

        def spawn(delay):
            delays.append(delay)
            self.next_pid[0] += 1
            self.alive.add(self.next_pid[0])
            return self.next_pid[0]

        snooze.arm_timer(st, self.now if now is None else now, spawn=spawn)
        return delays

    def test_arms_once_for_the_soonest_deadline(self):
        st = state({"a": {"until": self.now + 90_000}, "b": {"until": self.now + HOUR}})
        self.assertEqual(self.arm(st), [91])  # just after, never before
        self.assertEqual(st["timer"], self.now + 90_000)
        self.assertEqual(self.arm(st), [])  # already armed for it, and the sleeper is alive

    def test_rearms_when_an_earlier_deadline_appears(self):
        st = state({"a": {"until": self.now + 2 * 60_000}})
        self.arm(st)
        first = st["timer_pid"]
        st["panes"]["b"] = {"until": self.now + 60_000}
        self.assertEqual(self.arm(st), [61])
        self.assertIn(first, self.stopped, "the replaced sleeper was left running")

    def test_never_sleeps_longer_than_the_safety_interval(self):
        # v0.2: herdr has no event for another plugin taking the view; a tick
        # at least every few minutes is how snooze notices.
        st = state({"a": {"until": self.now + HOUR}})
        self.assertEqual(self.arm(st), [self.SAFETY_S + 1])

    def test_a_dead_sleeper_is_replaced(self):
        # v0.2: 0.1 trusted a recorded sleeper even after it died, and a long
        # snooze then woke at the 24h TTL instead of its deadline.
        st = state({"a": {"until": self.now + 60_000}})
        self.arm(st)
        self.alive.clear()  # killed, or the machine slept through it
        self.assertEqual(self.arm(st), [61])

    def test_workspaces_count(self):
        st = state(workspaces={"w": {"until": self.now + 30_000}})
        self.assertEqual(self.arm(st), [31])

    def test_long_snooze_wakes_up_in_time_to_refresh_its_token(self):
        # herdr caps a token at 24h. On a machine quiet enough that no hook
        # fires (a weekend), nothing else would re-report it, and a week-long
        # snooze would pop back into the panel after one day.
        week = self.now + 7 * 24 * HOUR
        st = state({"a": {"until": week, "dirty": True}})
        snooze.reconcile(st, [pane("a")], self.now)
        self.arm(st)
        # Safety ticks come every few minutes; follow them to the refresh margin.
        now = self.now
        while now < self.now + 12 * HOUR + 2000:
            now = st["timer"]
            ops = snooze.reconcile(st, [pane("a", tokens=live_token())], now)
            if ops:
                break
            self.arm(st, now)
        self.assertEqual([op[0] for op in ops], ["report"])  # the token is refreshed in time
        self.assertLessEqual(now, self.now + 12 * HOUR + 2000)

    def test_short_snooze_is_not_woken_for_a_refresh_it_does_not_need(self):
        st = state({"a": {"until": self.now + 60_000, "dirty": True}})
        snooze.reconcile(st, [pane("a")], self.now)
        self.assertEqual(self.arm(st), [61])

    def test_real_sleepers_are_recognised_and_only_ours_are_stopped(self):
        import subprocess
        mock.patch.stopall()  # the real helpers, against real processes
        try:
            subprocess.run(["ps", "-o", "command=", "-p", str(os.getpid())], capture_output=True, check=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("ps isn't available here (a sandbox)")
        # The real launcher, so this tests the contract rather than a copy of
        # it. The imposter even borrows our $0 name, but runs something else.
        pid = snooze._spawn_timer(30)
        self.assertIsInstance(pid, int)
        other = subprocess.Popen(["/bin/sh", "-c", "sleep 30 && true", snooze.TIMER_NAME], start_new_session=True)
        try:
            self.assertTrue(snooze.timer_alive(pid))
            self.assertTrue(snooze.is_our_timer(pid))
            self.assertFalse(snooze.is_our_timer(other.pid))
            snooze.stop_timer(other.pid)  # a reused PID: must survive
            snooze.stop_timer(pid)
            _, status = os.waitpid(pid, 0)  # our child: reap it, or it lingers as a zombie
            self.assertTrue(os.WIFSIGNALED(status))
            self.assertIsNone(other.poll(), "stopped someone else's process")
        finally:
            with contextlib.suppress(OSError):
                os.killpg(pid, 9)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
            other.kill()
            other.wait()

    def test_nothing_snoozed_disarms(self):
        st = state()
        st["timer"] = 123
        self.assertEqual(self.arm(st), [])
        self.assertIsNone(st["timer"])

    def test_spawn_failure_is_not_fatal(self):
        def broken(_delay):
            raise OSError("no fork for you")

        st = state({"a": {"until": self.now + 60_000}})
        snooze.arm_timer(st, self.now, spawn=broken)
        self.assertIsNone(st["timer"])  # so the next sync tries again


class FakeHerdr:
    """Just enough of the socket API for sync(): panes, one agent view, tokens."""

    def __init__(self, panes, view=None):
        self.panes = panes
        self.view = view or {"active": False}
        self.view_params = None
        self.calls = []

    def __call__(self, method, params=None, timeout=5.0):
        self.calls.append(method)
        if method == "pane.list":
            return {"panes": self.panes}
        if method == "pane.report_metadata":
            pane = next(p for p in self.panes if p["pane_id"] == params["pane_id"])
            for name, value in params["tokens"].items():
                if value is None:
                    pane["tokens"].pop(name, None)
                else:
                    pane["tokens"][name] = value
            return {}
        if method == "agent.view.set":
            self.view = {"active": True, "source": params["source"], "label": params.get("label")}
            self.view_params = params
            return dict(self.view)
        if method == "agent.view.clear":
            if self.view.get("source") == params.get("source"):
                self.view, self.view_params = {"active": False}, None
            return dict(self.view)
        return {}


class SyncAgainstFakeHerdr(unittest.TestCase):
    """The whole loop, as the hooks run it, without a herdr."""

    config = {"durations": ["1h"], "badge": "z", "notify": False, "sort": None, "views": None, "auto_return": True}

    def setUp(self):
        self.panes = [dict(pane("work"), focused=True), dict(pane("zz"), focused=False), dict(pane("zz2"), focused=False)]
        self.herdr = FakeHerdr(self.panes, {"active": True, "source": RADAR, "label": "active"})
        for patcher in (mock.patch.object(snooze, "call", self.herdr), mock.patch.object(snooze, "_spawn_timer"),
                        mock.patch.object(snooze, "owner_turned_off", return_value=False), mock.patch.object(snooze, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)
        far = snooze.now_ms() + HOUR
        self.state = state({"zz": {"until": far, "dirty": True}, "zz2": {"until": far, "dirty": True}})

    def focus(self, pane_id):
        for entry in self.panes:
            entry["focused"] = entry["pane_id"] == pane_id

    def sync(self):
        snooze.sync(self.state, self.config)
        return self.herdr.view_params

    def test_toggle_visit_and_leave(self):
        hidden = self.sync()
        self.assertEqual((hidden["label"], hidden["filter"]), ("active z 2", snooze.VIEW_FILTER))
        self.assertEqual(self.panes[1]["tokens"], {"snoozed": mock.ANY, "herdr_snooze": "1"})

        self.state["view"].update({"showing": "snoozed", "from_pane": "work"})  # what `toggle` does
        shown = self.sync()
        self.assertEqual((shown["label"], shown["filter"]), ("z snoozed only · 2", snooze.SNOOZED_LIST_FILTER))
        self.assertEqual(shown["sort"], hidden["sort"])  # radar's order in both

        self.assertEqual(self.sync()["filter"], snooze.SNOOZED_LIST_FILTER)  # hooks firing while you read it
        self.focus("zz")
        self.assertEqual(self.sync()["filter"], snooze.SNOOZED_LIST_FILTER)  # looking at a snoozed agent
        self.focus("work")
        back = self.sync()  # walked away: the normal panel returns by itself
        self.assertEqual((back["label"], back["filter"]), ("active z 2", snooze.VIEW_FILTER))
        self.assertNotIn("showing", self.state["view"])

    def test_auto_return_can_be_turned_off(self):
        self.config = dict(self.config, auto_return=False)
        self.sync()
        self.state["view"].update({"showing": "snoozed", "from_pane": "work"})
        self.focus("zz")
        self.sync()
        self.focus("work")
        self.assertEqual(self.sync()["filter"], snooze.SNOOZED_LIST_FILTER)

    def test_last_snooze_ending_in_the_snoozed_list_gives_radar_its_view_back(self):
        self.sync()
        self.state["view"].update({"showing": "snoozed", "from_pane": "work"})
        self.sync()
        for entry in self.state["panes"].values():
            entry["until"] = snooze.now_ms() - 1
        self.sync()
        self.assertEqual(self.herdr.view, {"active": True, "source": RADAR, "label": "active"})
        self.assertNotIn("filter", self.herdr.view_params)
        self.assertTrue(snooze.is_idle(self.state))
        self.assertTrue(all("snoozed" not in p["tokens"] for p in self.panes))

    def test_using_radars_own_key_mid_visit_lands_you_on_the_normal_panel(self):
        self.sync()
        self.state["view"].update({"showing": "snoozed", "from_pane": "work"})
        self.sync()
        self.herdr("agent.view.set", {"source": RADAR, "label": "recent", "sort": []})  # user flipped radar
        again = self.sync()
        # hiding carries on under radar's new order; the snoozed list is not re-imposed
        self.assertEqual((again["label"], again["filter"]), ("recent z 2", snooze.VIEW_FILTER))
        self.assertEqual(again["sort"], snooze.KNOWN_VIEWS[RADAR]["sorts"]["recent"])
        self.assertNotIn("showing", self.state["view"])

    def test_restart_never_comes_back_up_in_the_snoozed_list(self):
        self.sync()
        self.state["view"].update({"showing": "snoozed", "from_pane": "work"})
        self.sync()
        self.herdr.view, self.herdr.view_params = {"active": False}, None  # server restarted: view gone
        for entry in self.panes:
            entry["tokens"].clear()  # and the tokens with it
        snooze.sync(self.state, self.config, force=True)
        self.assertEqual(self.herdr.view_params["filter"], snooze.VIEW_FILTER)
        self.assertNotIn("showing", self.state["view"])
        self.assertTrue(all("snoozed" in p["tokens"] for p in self.panes if p["pane_id"] != "work"))

    def test_shell_or_popup_taking_focus_does_not_end_the_visit(self):
        self.panes.append(dict(pane("shell", agent=None), focused=False))
        self.sync()
        self.state["view"].update({"showing": "snoozed", "from_pane": "work"})
        self.focus("zz")
        self.sync()
        self.focus("shell")
        self.assertEqual(self.sync()["filter"], snooze.SNOOZED_LIST_FILTER)

    def test_blocked_agents_stay_visible_in_the_snoozed_list_and_the_label_says_so(self):
        self.sync()
        self.state["view"].update({"showing": "snoozed", "from_pane": "work"})
        self.assertEqual(self.sync()["label"], "z snoozed only · 2")
        self.panes[0]["agent_status"] = "blocked"  # the active one now needs you
        shown = self.sync()
        self.assertEqual(shown["label"], "z snoozed only · 2 + 1 blocked")
        self.assertIn({"op": "eq", "field": "status", "value": "blocked"}, shown["filter"]["filters"])
        self.panes[1]["agent_status"] = "blocked"  # a snoozed one being blocked is not news
        self.assertEqual(self.sync()["label"], "z snoozed only · 2 + 1 blocked")

    def test_records_with_no_agent_behind_them_hold_no_view(self):
        # a snoozed agent exited: the record stays (it may come back), the filter does not
        for entry in self.panes:
            entry["agent"] = None if entry["pane_id"] != "work" else entry["agent"]
        self.state["view"].update({"showing": "snoozed", "from_pane": "work"})
        snooze.sync(self.state, self.config)
        self.assertEqual(self.herdr.view, {"active": True, "source": RADAR, "label": "active"})  # untouched
        self.assertNotIn("showing", self.state["view"])

    def test_failed_workspace_token_is_retried_next_time(self):
        self.state["workspaces"]["w1"] = {"until": snooze.now_ms() + HOUR, "dirty": True}
        real = self.herdr.__call__

        def flaky(method, params=None, timeout=5.0):
            if method == "workspace.report_metadata":
                raise snooze.HerdrError("busy", "try later")
            return real(method, params, timeout)

        with mock.patch.object(snooze, "call", flaky):
            snooze.sync(self.state, self.config)
        self.assertTrue(self.state["workspaces"]["w1"].get("dirty"))
        snooze.sync(self.state, self.config)
        self.assertIn("workspace.report_metadata", self.herdr.calls)
        self.assertNotIn("dirty", self.state["workspaces"]["w1"])

    def test_radars_off_flag_is_not_applied_to_another_plugin(self):
        self.sync()  # borrowed from radar
        self.herdr("agent.view.set", {"source": "herdr-agent-inbox", "label": "Inbox", "sort": []})
        with mock.patch.object(snooze, "owner_turned_off", side_effect=lambda owner: (owner or {}).get("source") == RADAR):
            snooze.sync(self.state, self.config)
        self.assertEqual(self.herdr.view_params["sort"], snooze.KNOWN_VIEWS["herdr-agent-inbox"]["sorts"]["Inbox"])
        self.assertEqual(self.state["view"]["inherited"]["source"], "herdr-agent-inbox")

    def test_timer_armed_for_the_soonest_deadline(self):
        snooze._spawn_timer.return_value = 4242
        with mock.patch.object(snooze, "timer_alive", lambda pid: pid == 4242):
            self.sync()
            snooze._spawn_timer.assert_called_once()
            self.sync()
            snooze._spawn_timer.assert_called_once()  # not again while that sleeper lives


class Commands(unittest.TestCase):
    """main() as herdr runs it: a real state dir, a fake herdr."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.panes = [dict(pane("work"), focused=True), dict(pane("zz"), focused=False)]
        self.herdr = FakeHerdr(self.panes, {"active": True, "source": RADAR, "label": "active"})
        self.notify = mock.Mock()
        # A herdr session directory of our own, so the session ID file never
        # lands in the real ~/.config/herdr.
        home = os.path.join(self.dir.name, "herdr-a")
        os.makedirs(home)
        sock = os.path.join(home, "herdr.sock")
        env = {"SNOOZE_STATE_DIR": self.dir.name, "HERDR_PLUGIN_CONTEXT_JSON": "", "HERDR_SOCKET_PATH": sock}
        for patcher in (mock.patch.dict(os.environ, env),
                        mock.patch.object(snooze, "running_sessions", return_value=[sock]),
                        mock.patch.object(snooze, "call", self.herdr), mock.patch.object(snooze, "_spawn_timer"),
                        mock.patch.object(snooze, "owner_turned_off", return_value=False),
                        mock.patch.object(snooze, "notify", self.notify), mock.patch.object(snooze, "log"),
                        mock.patch.object(snooze, "load_config", return_value=dict(SyncAgainstFakeHerdr.config))):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.makedirs(snooze.state_dir(), exist_ok=True)
        self.file = os.path.join(snooze.state_dir(), "snoozed.json")

    def snoozed(self, until):
        with open(self.file, "w") as handle:
            handle.write('{"panes": {"zz": {"until": %d, "via": "pane"}}}' % until)

    def action(self, name, pane_id="work", workspace_id="w1"):
        context = '{"focused_pane_id": "%s", "workspace_id": "%s"}' % (pane_id, workspace_id)
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": context}), \
                mock.patch.object(snooze, "open_picker", return_value=0) as picker, \
                mock.patch.object(snooze, "wake", return_value=1) as woke:
            self.assertEqual(snooze.run_action(name), 0)
        return picker, woke

    def test_snooze_key_asks_how_long_on_an_active_agent(self):
        picker, woke = self.action("agent", "work")
        picker.assert_called_once()
        self.assertEqual(picker.call_args[0][0]["SNOOZE_TARGET"], "work")
        woke.assert_not_called()

    def test_same_key_wakes_a_snoozed_agent_without_a_popup(self):
        self.snoozed(snooze.now_ms() + HOUR)
        picker, woke = self.action("agent", "zz")
        picker.assert_not_called()
        self.assertEqual(woke.call_args[1], {"pane_ids": ["zz"]})

    def test_space_action_wakes_a_snoozed_space(self):
        with open(self.file, "w") as handle:
            handle.write('{"workspaces": {"w1": {"until": %d}}}' % (snooze.now_ms() + HOUR))
        picker, woke = self.action("workspace", workspace_id="w1")
        picker.assert_not_called()
        self.assertEqual(woke.call_args[1], {"workspace_ids": ["w1"]})

    # ── v0.2: moved panes, a stale picker, bursts of hooks ──

    def test_a_moved_pane_keeps_its_snooze_even_if_a_plain_tick_sees_it_first(self):
        # Round 3: herdr runs hooks concurrently. A focus tick that reads
        # pane.list after the move, before the move hook, used to forget the
        # record as closed. No hook carries the move now: any tick finds it.
        self.snoozed(snooze.now_ms() + HOUR)
        snooze.main(["tick"])  # learns zz's terminal
        p = moved(self.panes[1], "w2:zz", "w2")
        p["tokens"] = {}  # the token stayed behind
        self.assertEqual(snooze.main(["tick"]), 0)  # a focus tick, no event
        self.assertEqual(list(snooze.read_state()["panes"]), ["w2:zz"])
        self.assertIn(snooze.MARK, p["tokens"], "hidden again under its new ID")

    def pick(self, target, shown):
        snooze.snooze("pane", target, datetime.now() + snooze.timedelta(hours=1), dict(SyncAgainstFakeHerdr.config),
                      expect_agent=shown)

    def test_the_picker_refuses_a_pane_whose_agent_changed(self):
        self.panes[1]["agent"] = "codex"
        with self.assertRaises(ValueError):
            self.pick("zz", {"agent": "claude"})
        with self.assertRaises(ValueError):  # moved or closed since the picker opened
            self.pick("gone", {"agent": "claude"})
        self.panes[1]["agent"] = None
        with self.assertRaises(ValueError):  # exited since
            self.pick("zz", {"agent": "claude"})
        self.assertEqual(snooze.read_state()["panes"], {})

    def test_the_picker_snoozes_the_agent_it_showed(self):
        # a 0.2-first-cut picker's identity (with a session) still matches
        self.panes[1]["agent_session"] = {"agent": "claude", "kind": "id", "source": "x", "value": "s-new"}
        self.pick("zz", {"agent": "claude", "session": "s-old"})
        record = snooze.read_state()["panes"]["zz"]
        self.assertEqual((record["agent_id"], record["terminal_id"]), ({"agent": "claude"}, "t-zz"))

    def counting_ticks(self):
        runs, real = [], snooze._tick_once

        def once(args):
            runs.append(1)
            return real(args)
        return runs, mock.patch.object(snooze, "_tick_once", once)

    def test_a_burst_of_ticks_coalesces(self):
        self.snoozed(snooze.now_ms() + HOUR)
        root = snooze.state_dir()
        with open(os.path.join(root, "tick.lock"), "a") as lock:
            snooze.fcntl.flock(lock, snooze.fcntl.LOCK_EX)  # a tick is running
            self.herdr.calls.clear()
            self.assertEqual(snooze.main(["tick"]), 0)
            self.assertEqual(self.herdr.calls, [], "queued up behind the running tick")
            self.assertTrue(os.path.exists(os.path.join(root, "tick.pending")))
        runs, patch = self.counting_ticks()
        with patch:
            snooze.main(["tick"])  # the pending note from the burst: one more pass
        self.assertEqual(len(runs), 1)
        self.assertFalse(os.path.exists(os.path.join(root, "tick.pending")))

    def test_a_note_left_as_the_runner_unlocks_is_not_stranded(self):
        # Round 3: a tick failing the lock after the runner's last look but
        # before its unlock left a note nobody read. Simulated at the exact
        # boundary: the note appears as the runner lets go of the lock.
        self.snoozed(snooze.now_ms() + HOUR)
        pending = os.path.join(snooze.state_dir(), "tick.pending")
        real_flock, notes = snooze.fcntl.flock, [1]

        def flock(handle, op):
            if op == snooze.fcntl.LOCK_UN and notes:
                notes.pop()
                open(pending, "a").close()
            return real_flock(handle, op)
        runs, patch = self.counting_ticks()
        with patch, mock.patch.object(snooze.fcntl, "flock", flock):
            snooze.main(["tick"])
        self.assertEqual(len(runs), 2, "the late note got its pass")
        self.assertFalse(os.path.exists(pending))

    def test_a_long_burst_is_not_cut_short(self):
        # Round 3: 0.2's first cut stopped after three passes, leaving the
        # rest of a burst to the 5-minute safety tick.
        self.snoozed(snooze.now_ms() + HOUR)
        pending = os.path.join(snooze.state_dir(), "tick.pending")
        runs, real = [], snooze._tick_once

        def once(args):
            runs.append(1)
            if len(runs) < 5:
                open(pending, "a").close()  # another hook arrived during this pass
            return real(args)
        with mock.patch.object(snooze, "_tick_once", once):
            snooze.main(["tick"])
        self.assertEqual(len(runs), 5)
        self.assertFalse(os.path.exists(pending))

    def test_a_tick_that_leaves_a_note_after_the_runner_quit_runs_itself(self):
        # The note's writer tries the lock once more: if the runner already
        # left, nobody else would read the note.
        self.snoozed(snooze.now_ms() + HOUR)
        real_flock, calls = snooze.fcntl.flock, []

        def flock(handle, op):
            calls.append(op)
            if len(calls) == 1:
                raise BlockingIOError  # held at the first try, free by the second
            return real_flock(handle, op)
        runs, patch = self.counting_ticks()
        with patch, mock.patch.object(snooze.fcntl, "flock", flock):
            snooze.main(["tick"])
        self.assertEqual(len(runs), 1)

    def test_a_detected_hook_marks_the_pane_and_is_never_coalesced_away(self):
        # Its event is the only evidence of a same-kind replacement: a note
        # for the running tick would lose it, so it waits its turn instead.
        with open(self.file, "w") as handle:
            handle.write('{"panes": {"zz": {"until": %d, "via": "pane", "since": 0, "agent_id": {"agent": "claude"}}}}'
                         % (snooze.now_ms() + HOUR))
        snooze.main(["tick"])
        self.assertIn(snooze.MARK, self.panes[1]["tokens"])
        event = {"event": "pane.agent_detected",
                 "data": {"pane_id": "zz", "workspace_id": "w1", "agent": "claude", "released": True,
                          "final_status": "idle"}}
        root = snooze.state_dir()
        with open(os.path.join(root, "tick.lock"), "a") as lock, \
                mock.patch.dict(os.environ, {"HERDR_PLUGIN_EVENT_JSON": snooze.json.dumps(event)}):
            snooze.fcntl.flock(lock, snooze.fcntl.LOCK_EX)  # a plain tick is running
            self.assertEqual(snooze.main(["tick", "--detected"]), 0)
        self.assertEqual(snooze.read_state()["panes"], {})
        self.assertNotIn(snooze.MARK, self.panes[1]["tokens"], "the new agent shows")

    def test_a_failed_timer_tick_arms_a_retry(self):
        # Round 4: the sleeper ticks once; if that tick fails and no hook
        # follows, nothing would arm the next and long snoozes lapse.
        self.snoozed(snooze.now_ms() + 2 * 24 * HOUR)
        open(os.environ["HERDR_SOCKET_PATH"], "w").close()  # herdr is up
        broken = mock.Mock(side_effect=OSError("herdr busy"))
        snooze._spawn_timer.return_value = 4343
        with mock.patch.object(snooze, "call", broken):
            for args in (["tick"], ["tick", "--timer"]):
                snooze._spawn_timer.reset_mock()
                with self.assertRaises(OSError):
                    snooze.main(args)
                self.assertEqual(snooze._spawn_timer.called, args == ["tick", "--timer"], args)
            # Recorded as the sleeper: the next tick counts on it instead of
            # arming a second one beside it.
            self.assertEqual(snooze.read_state()["timer_pid"], 4343)
            self.assertGreater(snooze.read_state()["timer"], snooze.now_ms())
            st = snooze.read_state()
            with mock.patch.object(snooze, "timer_alive", lambda pid: pid == 4343):
                snooze._spawn_timer.reset_mock()
                snooze.arm_timer(st, snooze.now_ms())
                snooze._spawn_timer.assert_not_called()
            os.remove(os.environ["HERDR_SOCKET_PATH"])  # herdr gone: don't keep a sleeper going
            snooze._spawn_timer.reset_mock()
            with self.assertRaises(OSError):
                snooze.main(["tick", "--timer"])
            snooze._spawn_timer.assert_not_called()

    def test_a_failed_timer_tick_leaves_a_live_sleeper_armed_meanwhile(self):
        # Round 4: another hook armed a sooner sleeper while this tick failed.
        self.snoozed(snooze.now_ms() + 2 * 24 * HOUR)
        open(os.environ["HERDR_SOCKET_PATH"], "w").close()
        with snooze.locked_state() as st:
            st["timer"], st["timer_pid"] = snooze.now_ms() + 60_000, 777
        broken = mock.Mock(side_effect=OSError("herdr busy"))
        with mock.patch.object(snooze, "call", broken), \
                mock.patch.object(snooze, "timer_alive", lambda pid: True), \
                mock.patch.object(snooze, "is_our_timer", lambda pid: pid == 777):
            snooze._spawn_timer.reset_mock()
            with self.assertRaises(OSError):
                snooze.main(["tick", "--timer"])
            snooze._spawn_timer.assert_not_called()
            self.assertEqual(snooze.read_state()["timer_pid"], 777)
            # This tick's own PID (the sleeper exec'd into it) is replaced.
            with snooze.locked_state() as st:
                st["timer_pid"] = os.getpid()
            snooze._spawn_timer.return_value = 4545
            with self.assertRaises(OSError):
                snooze.main(["tick", "--timer"])
            self.assertEqual(snooze.read_state()["timer_pid"], 4545)

    def test_the_sleeper_ticks_as_a_timer(self):
        self.assertTrue(snooze.TIMER_SCRIPT.endswith("tick --timer"))

    def test_toggle_there_and_back(self):
        self.snoozed(snooze.now_ms() + HOUR)
        self.assertEqual(snooze.main(["toggle"]), 0)
        self.assertEqual(self.herdr.view_params["filter"], snooze.SNOOZED_LIST_FILTER)
        self.assertEqual(snooze.read_state()["view"]["from_pane"], "work")  # where it was opened from
        self.assertEqual(snooze.main(["toggle"]), 0)
        self.assertEqual(self.herdr.view_params["filter"], snooze.VIEW_FILTER)
        self.notify.assert_not_called()

    def test_toggle_with_nothing_snoozed_says_so_and_touches_nothing(self):
        self.assertEqual(snooze.main(["toggle"]), 0)
        self.notify.assert_called_once_with("Snooze", "Nothing is snoozed.")
        self.assertEqual(self.herdr.calls, [])
        self.assertFalse(os.path.exists(self.file))

    def test_toggle_on_a_stale_file_explains_itself(self):
        # the only deadline passed while no hook ran: this sync clears it, so
        # the key press would otherwise do nothing, silently
        self.snoozed(snooze.now_ms() - 1000)
        self.assertEqual(snooze.main(["toggle"]), 0)
        self.notify.assert_called_once_with("Snooze", "Nothing is snoozed.")
        self.assertEqual(self.herdr.view, {"active": True, "source": RADAR, "label": "active"})
        self.assertFalse(os.path.exists(self.file))

    def test_damaged_file_fails_open_then_goes(self):
        # A record we can't read may stand for a live token and our live
        # view. Deleting the file unexamined could leave agents hidden with
        # nothing to un-hide them (0.1.0 did). Instead: clear tokens that
        # have no readable record and our view, then remove the file.
        for broken in ('{"panes": {"zz": {"until": "soon"}}}', "{not json", "[]"):
            with self.subTest(broken=broken):
                self.panes[1]["tokens"][snooze.TOKEN] = "z 12:00"
                self.herdr.view = {"active": True, "source": snooze.SOURCE, "label": "z 1"}
                with open(self.file, "w") as handle:
                    handle.write(broken)
                self.assertEqual(snooze.main(["tick"]), 0)
                self.assertNotIn(snooze.TOKEN, self.panes[1]["tokens"])
                self.assertEqual(self.herdr.view, {"active": False})
                self.assertFalse(os.path.exists(self.file))  # or run.sh starts Python on every hook, forever

    def test_missing_or_wrong_view_counts_as_damaged(self):
        # Round 2: these read as "idle", so the file went without a check.
        for broken in ('{"view": "x"}', '{"v": 1, "panes": {}, "workspaces": {}, "view": null}', '{"v": 1}'):
            with self.subTest(broken=broken):
                self.herdr.view = {"active": True, "source": snooze.SOURCE, "label": "z 1"}
                with open(self.file, "w") as handle:
                    handle.write(broken)
                self.assertEqual(snooze.main(["tick"]), 0)
                self.assertEqual(self.herdr.view, {"active": False}, "our filter left live")
                self.assertFalse(os.path.exists(self.file))

    # ── upgrading from 0.1.0: one state file for every session, at the top ──

    def legacy(self):
        path = os.path.join(self.dir.name, "snoozed.json")
        with open(path, "w") as handle:
            handle.write('{"panes": {"zz": {"until": %d, "via": "pane"}}, "workspaces": {}, "view": {}}' % (snooze.now_ms() + HOUR))
        return path

    def second_session(self, reachable=True):
        """A second running herdr session with its own herdr: quiet (it never
        ticks here), still showing 0.1.0's snoozed-only view, with a 0.1.0
        token on its own w1-numbered pane."""
        home = os.path.join(self.dir.name, "herdr-b")
        os.makedirs(home, exist_ok=True)
        sock = os.path.join(home, "herdr.sock")
        self.herdr_b = FakeHerdr([dict(pane("zz", tokens={snooze.TOKEN: "z 12:00"}), focused=False)],
                                 {"active": True, "source": snooze.SOURCE, "label": "z snoozed only · 1"})
        self.b_reachable = reachable
        mine = snooze.socket_path()

        def route(method, params=None, timeout=5.0):
            if os.path.realpath(snooze.socket_path()) == os.path.realpath(sock):
                if not self.b_reachable:
                    raise OSError("session b not answering")
                return self.herdr_b(method, params, timeout)
            return self.herdr(method, params, timeout)

        for patcher in (mock.patch.object(snooze, "call", route),
                        mock.patch.object(snooze, "running_sessions", return_value=[mine, sock])):
            patcher.start()
            self.addCleanup(patcher.stop)
        return sock

    def test_0_1_0_state_is_adopted_by_no_session_and_fails_open_everywhere(self):
        legacy = self.legacy()
        self.second_session()
        self.panes[1]["tokens"][snooze.TOKEN] = "z 12:00"  # 0.1.0's token, maybe this session's
        self.herdr.view = {"active": True, "source": snooze.SOURCE, "label": "z 1"}
        self.assertEqual(snooze.main(["tick"]), 0)
        # this session: the agent reappears, and "zz" wasn't adopted as ours
        self.assertNotIn(snooze.TOKEN, self.panes[1]["tokens"])
        self.assertEqual(self.herdr.view, {"active": False})
        self.assertEqual(snooze.read_state()["panes"], {})
        # the quiet session, which never ticked, recovered through its own socket
        self.assertEqual(self.herdr_b.view, {"active": False})
        self.assertNotIn(snooze.TOKEN, self.herdr_b.panes[0]["tokens"])
        # everyone recovered: the old file goes, and run.sh stops starting Python
        self.assertFalse(os.path.exists(legacy))

    def past_the_backoff(self):
        path = os.path.join(self.dir.name, "legacy-retry.json")
        with open(path) as handle:
            retry = snooze.json.load(handle)
        retry["at"] = 0
        with open(path, "w") as handle:
            snooze.json.dump(retry, handle)

    def test_0_1_0_file_stays_until_every_running_session_recovered(self):
        legacy = self.legacy()
        self.second_session(reachable=False)
        self.assertEqual(snooze.main(["tick"]), 0)
        self.assertTrue(os.path.exists(legacy), "removed with a session still unrecovered")
        self.herdr.calls.clear()
        self.b_reachable = True
        self.past_the_backoff()
        self.assertEqual(snooze.main(["tick"]), 0)
        self.assertEqual(self.herdr_b.view, {"active": False})
        self.assertFalse(os.path.exists(legacy))
        self.assertNotIn("agent.view.clear", self.herdr.calls, "recovered this session again")

    def test_a_refusing_session_is_retried_with_backoff_not_on_every_hook(self):
        self.legacy()
        self.second_session(reachable=False)
        with mock.patch.object(snooze, "running_sessions", wraps=snooze.running_sessions) as listed:
            snooze.main(["tick"])
            snooze.main(["tick"])  # straight after: backing off
            self.assertEqual(listed.call_count, 1, "every hook paid for another recovery attempt")
        with open(os.path.join(self.dir.name, "legacy-retry.json")) as handle:
            first = snooze.json.load(handle)["delay"]
        self.past_the_backoff()
        snooze.main(["tick"])  # fails again: waits longer
        with open(os.path.join(self.dir.name, "legacy-retry.json")) as handle:
            self.assertEqual(snooze.json.load(handle)["delay"], first * 2)

    def test_only_one_process_recovers_at_a_time(self):
        self.legacy()
        with open(os.path.join(self.dir.name, "legacy.lock"), "a") as lock:
            snooze.fcntl.flock(lock, snooze.fcntl.LOCK_EX)  # another process is at it
            with mock.patch.object(snooze, "running_sessions") as listed:
                self.assertEqual(snooze.main(["tick"]), 0)
            listed.assert_not_called()

    def test_a_refused_token_clear_is_not_counted_as_recovered(self):
        # Round 2c: the clear failed, yet the session was marked done and the
        # 0.1.0 file removed, leaving that agent hidden.
        legacy = self.legacy()
        self.panes[1]["tokens"][snooze.TOKEN] = "z 12:00"
        real = self.herdr.__call__

        def refuses_clears(method, params=None, timeout=5.0):
            if method == "pane.report_metadata" and params["tokens"].get(snooze.TOKEN, "x") is None:
                raise snooze.HerdrError("denied", "not now")
            return real(method, params, timeout)

        with mock.patch.object(snooze, "call", refuses_clears):
            snooze.main(["tick"])
        self.assertTrue(os.path.exists(legacy))
        self.assertFalse(os.path.exists(os.path.join(snooze.state_dir(), "legacy-recovered")))

    def test_0_1_0_file_stays_if_herdr_cant_list_sessions(self):
        legacy = self.legacy()
        with mock.patch.object(snooze, "running_sessions", return_value=None):
            self.assertEqual(snooze.main(["tick"]), 0)
        self.assertTrue(os.path.exists(legacy))  # no proof the others recovered

    def test_a_session_with_its_own_snoozes_keeps_them_through_recovery(self):
        self.snoozed(snooze.now_ms() + HOUR)
        snooze.main(["tick"])  # takes the view for "zz"
        self.legacy()
        self.assertEqual(snooze.main(["tick"]), 0)
        self.assertIn("zz", snooze.read_state()["panes"])
        self.assertEqual(self.herdr.view.get("source"), snooze.SOURCE)

    def test_damaged_file_leaves_another_plugins_view_alone(self):
        with open(self.file, "w") as handle:
            handle.write("{not json")
        self.assertEqual(snooze.main(["tick"]), 0)
        self.assertEqual(self.herdr.view, {"active": True, "source": RADAR, "label": "active"})
        self.assertFalse(os.path.exists(self.file))

    def test_a_crash_while_planning_never_deletes_the_file(self):
        # Round 2c: plan_view had already set the view record to idle when
        # it crashed, so the file went with our filter still live.
        self.snoozed(snooze.now_ms() - 1000)
        self.herdr.view = {"active": True, "source": snooze.SOURCE, "label": "z 1"}
        def clears_then_crashes(state, *args, **kwargs):
            state["view"] = {}  # what plan_view does before it can fail
            raise TypeError("boom")

        with mock.patch.object(snooze, "plan_view", side_effect=clears_then_crashes):
            with self.assertRaises(TypeError):
                snooze.main(["tick"])
        self.assertTrue(os.path.exists(self.file))

    def test_failed_last_clear_keeps_the_file_so_the_next_tick_retries(self):
        # The last snooze has ended; clearing our view is all that's left.
        self.snoozed(snooze.now_ms() - 1000)
        self.herdr.view = {"active": True, "source": snooze.SOURCE, "label": "z 1"}
        real = self.herdr.__call__

        def dies_on_clear(method, params=None, timeout=5.0):
            if method == "agent.view.clear" and params.get("source") == snooze.SOURCE:
                raise OSError("herdr went away mid-call")
            return real(method, params, timeout)

        with mock.patch.object(snooze, "call", dies_on_clear):
            with self.assertRaises(OSError):
                snooze.main(["tick"])
        self.assertTrue(os.path.exists(self.file), "deleted with our filter still live: nothing would retry")
        self.assertEqual(snooze.main(["tick"]), 0)  # herdr is back
        self.assertEqual(self.herdr.view, {"active": False})
        self.assertFalse(os.path.exists(self.file))


class Config(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = mock.patch.object(snooze, "log").start()
        self.addCleanup(mock.patch.stopall)

    def write(self, text):
        with open(os.path.join(self.dir.name, "config.json"), "w") as handle:
            handle.write(text)

    def test_wrong_types_fall_back_to_defaults_with_a_note(self):
        # v0.2: "badge": 7 crashed every hook; a list in "views" too.
        self.write('{"badge": 7, "views": ["x"], "sort": {"a": 1}, "notify": "yes", "durations": [], "auto_return": false}')
        config = snooze.load_config()
        self.assertEqual(config["badge"], snooze.DEFAULT_BADGE)
        self.assertIsNone(config["views"])
        self.assertIsNone(config["sort"])
        self.assertFalse(config["notify"])
        self.assertEqual(config["durations"], snooze.DEFAULT_DURATIONS)
        self.assertFalse(config["auto_return"])  # the valid one is kept
        self.assertGreaterEqual(self.log.call_count, 5)

    def test_sort_keys_must_be_ones_herdr_accepts(self):
        # Round 3: any dict passed, and herdr then refused every agent.view.set.
        good = [{"field": "attention"}, {"field": {"token": "rank"}, "order": "desc"}]
        for bad in ([{}], [{"field": "nope"}], [{"field": "seen", "order": "up"}], [{"field": {"token": 1}}],
                    [{"field": {"token": "a", "extra": 1}}], [{"field": ["seen"]}], ["seen"]):
            with self.subTest(bad=bad):
                self.write(snooze.json.dumps({"sort": bad, "views": {"plugin:x": {"l": bad}}}))
                config = snooze.load_config()
                self.assertIsNone(config["sort"])
                self.assertIsNone(config["views"])
        self.write(snooze.json.dumps({"sort": good, "views": {"plugin:x": {"l": good}}}))
        self.assertEqual(snooze.load_config()["sort"], good)
        for view in snooze.KNOWN_VIEWS.values():  # and ours pass our own check
            self.assertTrue(all(snooze._sort_ok(s) for s in view["sorts"].values()))

    def test_malformed_json_says_so(self):
        self.write("{nope")
        self.assertEqual(snooze.load_config()["badge"], snooze.DEFAULT_BADGE)
        self.assertTrue(self.log.called, "settings silently ignored")

    def test_valid_settings_are_kept(self):
        self.write('{"badge": "zz", "durations": ["5m", 30], "views": {"src": {"label": [{"field": "status", "order": "asc"}]}}}')
        config = snooze.load_config()
        self.assertEqual((config["badge"], config["durations"]), ("zz", ["5m", 30]))
        self.assertIn("src", config["views"])


class Sessions(unittest.TestCase):
    """Pane IDs only mean something inside one herdr session; the plugin's
    state directory is shared by all of them."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = os.path.join(self.dir.name, "state")
        env = {"SNOOZE_STATE_DIR": self.state, "XDG_CONFIG_HOME": os.path.join(self.dir.name, "cfg")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("HERDR_SOCKET_PATH", None)
        self.log = mock.patch.object(snooze, "log").start()
        self.addCleanup(mock.patch.stopall)

    def session(self, name):
        """A herdr session's directory, as herdr makes one, and its socket."""
        home = os.path.join(self.dir.name, "sessions", name)
        os.makedirs(home, exist_ok=True)
        return os.path.join(home, "herdr.sock")

    def in_session(self, sock):
        return mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": sock})

    def snooze_in(self, sock, pane_id="w1:p1"):
        with self.in_session(sock), snooze.locked_state() as st:
            st["panes"][pane_id] = {"until": snooze.now_ms() + HOUR, "via": "pane"}

    def test_every_session_has_its_own_state(self):
        work, play = self.session("work"), self.session("play")
        self.snooze_in(work)
        with self.in_session(work):
            a = snooze.state_dir()
            self.assertIn("w1:p1", snooze.read_state()["panes"])
        with self.in_session(play):
            self.assertNotEqual(snooze.state_dir(), a)
            self.assertEqual(snooze.read_state()["panes"], {})  # play doesn't see work's w1:p1
        self.assertTrue(a.startswith(os.path.join(self.state, "sessions")))
        self.assertTrue(snooze.state_dir().startswith(os.path.join(self.state, "sessions")))  # default too

    def test_a_recreated_session_never_reads_the_old_ones_records(self):
        # herdr numbers a new session's panes from w1:p1 again.
        work = self.session("work")
        self.snooze_in(work)
        shutil.rmtree(os.path.dirname(work))  # herdr session delete work
        work = self.session("work")  # …and a new "work"
        with self.in_session(work):
            self.assertEqual(snooze.read_state()["panes"], {})

    def test_state_of_a_deleted_session_is_pruned(self):
        work, play = self.session("work"), self.session("play")
        self.snooze_in(work)
        self.snooze_in(play)
        with self.in_session(work):
            work_state = snooze.state_dir()
        with self.in_session(play):
            play_state = snooze.state_dir()
        shutil.rmtree(os.path.dirname(work))  # work is deleted
        with self.in_session(play):
            snooze.prune_stale_sessions()
        self.assertFalse(os.path.exists(work_state), "a deleted session's file keeps every hook starting Python")
        self.assertTrue(os.path.exists(os.path.join(play_state, "snoozed.json")))

    def test_a_stopped_or_restarted_session_keeps_its_state(self):
        work, play = self.session("work"), self.session("play")
        self.snooze_in(work)
        with self.in_session(play):
            snooze.prune_stale_sessions()  # work's directory is still there: only its server is down
        with self.in_session(work):
            self.assertIn("w1:p1", snooze.read_state()["panes"])

    def test_identity_doesnt_depend_on_the_file_system(self):
        # Round 2b: inode + birth time could repeat on Linux after a delete.
        # The session ID is random and lives in the session's own directory.
        work = self.session("work")
        with self.in_session(work):
            first = snooze.session_identity()
            self.assertEqual(snooze.session_identity(), first)  # stable while the directory lives
        home = os.path.dirname(work)
        os.remove(os.path.join(home, snooze.SESSION_ID_FILE))  # what deleting the session does to it
        with self.in_session(work):
            self.assertNotEqual(snooze.session_identity()["id"], first["id"])

    def test_a_session_being_set_up_is_never_pruned(self):
        # Round 2b: its directory exists before its identity is written.
        work, play = self.session("work"), self.session("play")
        with self.in_session(work):
            half = snooze.state_dir()
        os.makedirs(half)  # created, identity not written yet
        with self.in_session(play):
            snooze.prune_stale_sessions()
        self.assertTrue(os.path.isdir(half))

    def test_a_busy_stale_session_is_left_for_later(self):
        work, play = self.session("work"), self.session("play")
        self.snooze_in(work)
        with self.in_session(work):
            stale = snooze.state_dir()
        shutil.rmtree(os.path.dirname(work))
        with open(os.path.join(stale, "lock"), "a") as lock:
            snooze.fcntl.flock(lock, snooze.fcntl.LOCK_EX)  # something is writing it
            with self.in_session(play):
                snooze.prune_stale_sessions()
            self.assertTrue(os.path.isdir(stale))
        with self.in_session(play):
            snooze.prune_stale_sessions()
        self.assertFalse(os.path.exists(stale))

    def test_two_first_runs_agree_on_one_id(self):
        import multiprocessing
        sock = self.session("race")
        with multiprocessing.Pool(4) as pool:
            ids = pool.map(snooze.session_id, [sock] * 8)
        self.assertEqual(len(set(ids)), 1, ids)

    def test_herdrs_own_sessions_keep_the_0_1_1_id_file(self):
        # Renaming it would re-key every 0.1.1 session and orphan its state.
        sock = self.session("work")
        self.assertEqual(snooze.session_id_path(sock), os.path.join(os.path.dirname(sock), snooze.SESSION_ID_FILE))

    def test_custom_sockets_sharing_a_directory_dont_share_an_identity(self):
        # v0.2 (r2c): several custom sockets in /tmp shared one ID file.
        shared = os.path.join(self.dir.name, "tmp")
        os.makedirs(shared)
        a, b = os.path.join(shared, "a.sock"), os.path.join(shared, "b.sock")
        self.assertNotEqual(snooze.session_id(a), snooze.session_id(b))
        os.remove(snooze.session_id_path(a))  # a's server is gone; b's identity must stand
        with self.in_session(b):
            b_key = snooze.session_key()
        self.assertEqual(snooze.session_key({"socket": os.path.realpath(b), "id": snooze.read_session_id(b)}), b_key)


class RunSh(unittest.TestCase):
    """run.sh's fast path must find state wherever snooze.py puts it, or every
    tick would exit early, silently, forever."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        # A stand-in "python" that records it was started.
        self.marker = os.path.join(self.dir.name, "started")
        fake = os.path.join(self.dir.name, "fakepy")
        with open(fake, "w") as handle:
            handle.write('#!/bin/sh\ntouch "%s"\n' % self.marker)
        os.chmod(fake, 0o755)
        with open(os.path.join(self.dir.name, "python"), "w") as handle:
            handle.write(fake + "\n")
        self.run_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "run.sh")

    def tick(self):
        import subprocess
        env = dict(os.environ, SNOOZE_STATE_DIR=self.dir.name)
        subprocess.run(["/bin/sh", self.run_sh, "tick"], env=env, check=True)
        started = os.path.exists(self.marker)
        if started:
            os.remove(self.marker)
        return started

    def test_idle_everywhere_starts_nothing(self):
        self.assertFalse(self.tick())

    def test_a_0_1_0_state_file_starts_python(self):
        # Until each session has recovered from it (recover_legacy).
        open(os.path.join(self.dir.name, "snoozed.json"), "w").close()
        self.assertTrue(self.tick())

    def test_another_sessions_state_starts_python(self):
        session = os.path.join(self.dir.name, "sessions", "abc123")
        os.makedirs(session)
        open(os.path.join(session, "snoozed.json"), "w").close()
        self.assertTrue(self.tick())

    def run_with_stand_in_python3(self):
        """Runs `run.sh list` with a stand-in python3 on PATH, whose
        interpreter records that it ran. Returns which one was used."""
        import subprocess
        bin_dir = os.path.join(self.dir.name, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        fresh_marker = os.path.join(self.dir.name, "fresh")
        fresh = os.path.join(bin_dir, "freshpy")
        with open(fresh, "w") as handle:
            handle.write('#!/bin/sh\ntouch "%s"\n' % fresh_marker)
        os.chmod(fresh, 0o755)
        with open(os.path.join(bin_dir, "python3"), "w") as handle:
            handle.write('#!/bin/sh\necho "%s"\n' % fresh)  # what `python3 -c ...sys.executable` prints
        os.chmod(os.path.join(bin_dir, "python3"), 0o755)
        env = dict(os.environ, SNOOZE_STATE_DIR=self.dir.name, PATH=bin_dir + ":/usr/bin:/bin")
        subprocess.run(["/bin/sh", self.run_sh, "list"], env=env, check=True)
        return "cache" if os.path.exists(self.marker) else "fresh" if os.path.exists(fresh_marker) else None

    def test_a_private_interpreter_cache_is_used(self):
        self.assertEqual(self.run_with_stand_in_python3(), "cache")

    def test_an_interpreter_cache_others_could_write_is_ignored(self):
        # v0.2: the cache names the program run.sh executes.
        os.chmod(os.path.join(self.dir.name, "python"), 0o666)
        self.assertEqual(self.run_with_stand_in_python3(), "fresh")

    def test_a_cache_in_a_directory_others_could_write_is_ignored(self):
        os.chmod(self.dir.name, 0o777)
        self.assertEqual(self.run_with_stand_in_python3(), "fresh")


class StateFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        home = os.path.join(self.dir.name, "herdr-a")
        os.makedirs(home)
        env = {"SNOOZE_STATE_DIR": self.dir.name, "HERDR_SOCKET_PATH": os.path.join(home, "herdr.sock")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(snooze.state_dir(), exist_ok=True)
        self.path = os.path.join(snooze.state_dir(), "snoozed.json")

    def test_nested_view_fields_of_the_wrong_type_are_damage(self):
        # Round 2b: {"applied": 7} made planning crash on every hook.
        for view in ({"applied": 7}, {"inherited": "radar"}, {"showing": 1}, {"from_pane": []},
                     {"applied": "recover", "inherited": {"source": RADAR, "label": []}},  # round 2c
                     {"inherited": {"source": 5}}):
            with self.subTest(view=view):
                with open(self.path, "w") as handle:
                    handle.write(snooze.json.dumps({"panes": {}, "workspaces": {}, "view": view}))
                self.assertEqual(snooze.read_state()["view"], snooze.RECOVER)
        owner = {"active": True, "source": RADAR, "label": "active"}
        st = state(view={"applied": 7})  # even if one got through, planning must not crash
        snooze.plan_view(st, owner, 0)

    def test_saved_even_when_the_body_fails(self):
        # tokens may already be on panes when a later herdr call dies
        with self.assertRaises(snooze.HerdrError):
            with snooze.locked_state() as st:
                st["panes"]["p1"] = {"until": 5, "via": "pane"}
                raise snooze.HerdrError("boom", "socket went away")
        self.assertEqual(snooze.read_state()["panes"], {"p1": {"until": 5, "via": "pane"}})

    def test_file_exists_only_while_something_is_snoozed(self):
        with snooze.locked_state() as st:
            st["panes"]["p1"] = {"until": 5}
        self.assertTrue(os.path.exists(self.path))
        with snooze.locked_state() as st:
            st["panes"].clear()
        self.assertFalse(os.path.exists(self.path))  # run.sh's idle signal

    def test_malformed_entries_are_dropped_not_fatal(self):
        with open(self.path, "w") as handle:
            handle.write('{"panes": {"ok": {"until": 9}, "bad": {"til": 9}, "worse": 7}, "workspaces": [], "view": "x", "timer": "soon"}')
        st = snooze.read_state()
        self.assertEqual(st["panes"], {"ok": {"until": 9}})
        # Dropped records may stand for live tokens and our view: the state
        # says so, so the next sync checks herdr before anything is forgotten.
        self.assertEqual((st["workspaces"], st["view"], st["timer"]), ({}, snooze.RECOVER, None))
        with open(self.path, "w") as handle:
            handle.write("not json")
        st = snooze.read_state()
        self.assertFalse(snooze.is_idle(st))  # not "nothing snoozed": we can't tell
        self.assertEqual(st["view"], snooze.RECOVER)


UP, DOWN, ENTER, ESC, BACKSPACE =b"\x1b[A", b"\x1b[B", b"\r", b"\x1b", b"\x7f"
ITEMS = [("1", "15 minutes", "12:15"), ("2", "1 hour", "13:00"), ("c", "Custom…", "")]


class FakeTerminal:
    """Scripted keys in, drawn frames out — the picker without a tty."""

    def __init__(self, *keys):
        self.keys = [key if isinstance(key, bytes) else key.encode() for key in keys]
        self.frames = []

    def key(self):
        return snooze.decode_key(self.keys.pop(0)) if self.keys else ("eof", b"")

    def draw(self, lines):
        self.frames.append("\n".join(lines))


class Keys(unittest.TestCase):
    def test_decode(self):
        self.assertEqual(snooze.decode_key(b"\x1b[A")[0], "up")
        self.assertEqual(snooze.decode_key(b"\x1bOB")[0], "down")  # application cursor mode
        self.assertEqual(snooze.decode_key(b"\r")[0], "enter")
        self.assertEqual(snooze.decode_key(b"\x1b")[0], "esc")
        self.assertEqual(snooze.decode_key(b"\x03")[0], "esc")  # ctrl-c
        self.assertEqual(snooze.decode_key(b"3"), ("char", b"3"))
        self.assertEqual(snooze.decode_key(b""), ("eof", b""))


class SplitKeys(unittest.TestCase):
    def test_held_arrow_is_two_moves_not_one_unknown_blob(self):
        self.assertEqual(snooze.split_keys(b"\x1b[B\x1b[B"), [b"\x1b[B", b"\x1b[B"])
        self.assertEqual(snooze.split_keys(b"\x1bOA\r"), [b"\x1bOA", b"\r"])

    def test_paste_becomes_characters(self):
        self.assertEqual(snooze.split_keys(b"45m"), [b"4", b"5", b"m"])
        self.assertEqual(snooze.split_keys("é1".encode()), ["é".encode(), b"1"])
        self.assertEqual(snooze.split_keys("💤".encode()), ["💤".encode()])

    def test_unknown_escape_sequence_is_swallowed_whole(self):
        self.assertEqual(snooze.split_keys(b"\x1b[1;5C"), [b"\x1b[1;5C"])  # ctrl-right: ignored, not "esc" + junk
        self.assertEqual(snooze.decode_key(b"\x1b[1;5C")[0], "char")
        self.assertEqual(snooze.split_keys(b""), [])


class Menu(unittest.TestCase):
    def test_arrows_wrap_and_enter_selects(self):
        self.assertEqual(snooze.menu(FakeTerminal(DOWN, DOWN, ENTER), [], ITEMS, ""), 2)
        self.assertEqual(snooze.menu(FakeTerminal(UP, ENTER), [], ITEMS, ""), 2)
        self.assertEqual(snooze.menu(FakeTerminal("j", "k", "j", ENTER), [], ITEMS, ""), 1)

    def test_hotkeys_select_immediately(self):
        self.assertEqual(snooze.menu(FakeTerminal("2"), [], ITEMS, ""), 1)
        self.assertEqual(snooze.menu(FakeTerminal("C"), [], ITEMS, ""), 2)
        self.assertEqual(snooze.menu(FakeTerminal("9", "x", "1"), [], ITEMS, ""), 0)  # unknown keys ignored

    def test_cancel(self):
        for key in (ESC, b"q", b"\x03"):
            self.assertIsNone(snooze.menu(FakeTerminal(key), [], ITEMS, ""))
        self.assertIsNone(snooze.menu(FakeTerminal(), [], ITEMS, ""))  # popup closed under us

    def test_rows_without_a_hotkey_cannot_be_picked_by_typing(self):
        items = [("1", "one", ""), ("", "ten", ""), ("", "eleven", "")]
        self.assertIsNone(snooze.menu(FakeTerminal(" ", ESC), [], items, ""))

    def test_long_lists_scroll_with_the_selection(self):
        items = [("", "row %02d" % n, "") for n in range(30)]
        term = FakeTerminal(*([DOWN] * 20), ESC)
        term.rows = 12
        snooze.menu(term, ["  header"], items, "footer")
        for frame in term.frames:
            self.assertLessEqual(len(frame.split("\n")), term.rows)
        first, last = term.frames[0], term.frames[-1]
        self.assertIn("row 00", first)
        self.assertNotIn("row 20", first)
        self.assertIn("more", first)
        selected = [line for line in last.split("\n") if snooze.REVERSE in line]
        self.assertEqual(len(selected), 1)
        self.assertIn("row 20", selected[0])  # still on screen after 20 presses

    def test_draws_the_selection_and_hints(self):
        term = FakeTerminal(DOWN, ESC)
        snooze.menu(term, ["  header"], ITEMS, "footer")
        self.assertIn("header", term.frames[0])
        self.assertIn("footer", term.frames[0])
        selected = [line for line in term.frames[1].split("\n") if snooze.REVERSE in line]
        self.assertEqual(len(selected), 1)
        self.assertIn("1 hour", selected[0])
        self.assertIn("13:00", selected[0])


class Prompt(unittest.TestCase):
    def seconds(self, until):
        return round((until - datetime.now()).total_seconds())

    def test_typed_duration(self):
        self.assertAlmostEqual(self.seconds(snooze.prompt(FakeTerminal("4", "5", "m", ENTER), [], "?")), 2700, delta=2)

    def test_letters_bound_in_the_menu_are_just_text_here(self):
        # 'j', 'k' and 'q' move/cancel in the menu; "1wk" and "2 weeks" need them typed
        self.assertAlmostEqual(self.seconds(snooze.prompt(FakeTerminal("1", "w", "k", ENTER), [], "?")), 604800, delta=2)

    def test_error_then_correction(self):
        term = FakeTerminal("5", "x", ENTER, BACKSPACE, "h", ENTER)
        self.assertAlmostEqual(self.seconds(snooze.prompt(term, [], "?")), 5 * 3600, delta=2)
        self.assertTrue(any("unknown unit" in frame for frame in term.frames))
        self.assertNotIn("unknown unit", term.frames[-1])  # cleared once you edit

    def test_escape_backs_out(self):
        self.assertIsNone(snooze.prompt(FakeTerminal("5", ESC), [], "?"))


class PickerSnooze(unittest.TestCase):
    env = {"SNOOZE_KIND": "pane", "SNOOZE_TARGET": "w1:p1", "SNOOZE_LABEL": "Fix the login bug", "SNOOZE_DETAIL": "claude · web"}
    config = {"durations": ["15m", "1h", "bogus", "1w"], "badge": "z", "notify": False, "sort": None, "views": None,
              "auto_return": True}

    def run_picker(self, *keys, snoozed=None):
        term = FakeTerminal(*keys)
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(snooze, "read_state", return_value=state(snoozed or {})), \
                mock.patch.object(snooze, "snooze", return_value=1) as do_snooze, \
                mock.patch.object(snooze, "wake", return_value=1) as do_wake:
            outcome = snooze.picker_snooze(term, self.config)
        return term, outcome, do_snooze, do_wake

    def test_preset(self):
        term, outcome, do_snooze, _ = self.run_picker("2")
        kind, target, until, _config = do_snooze.call_args[0]
        self.assertEqual((kind, target), ("pane", "w1:p1"))
        self.assertAlmostEqual((until - datetime.now()).total_seconds(), 3600, delta=2)
        self.assertIn("snoozed pane w1:p1", outcome)
        self.assertIn("Fix the login bug", term.frames[0])
        self.assertIn("claude · web", term.frames[0])

    def test_preset_counts_from_when_you_pick_it_not_when_the_popup_opened(self):
        opened = datetime.now() - __import__("datetime").timedelta(minutes=20)
        real_datetime = snooze.datetime

        class Clock(real_datetime):
            calls = 0

            @classmethod
            def now(cls, tz=None):
                cls.calls += 1
                return opened if cls.calls == 1 else real_datetime.now()  # 1st call: popup opening

        with mock.patch.object(snooze, "datetime", Clock):
            _, _, do_snooze, _ = self.run_picker("1")
        left = (do_snooze.call_args[0][2] - real_datetime.now()).total_seconds()
        self.assertAlmostEqual(left, 900, delta=3)  # a full 15 minutes, not minus the 20 it sat open

    def test_bad_config_entry_is_skipped_not_fatal(self):
        term, _, do_snooze, _ = self.run_picker("3")  # 15m, 1h, [bogus dropped], 1w
        self.assertNotIn("bogus", term.frames[0])
        self.assertAlmostEqual((do_snooze.call_args[0][2] - datetime.now()).total_seconds(), 604800, delta=2)

    def test_custom(self):
        _, _, do_snooze, _ = self.run_picker("c", "9", "0", "s", ENTER)
        self.assertAlmostEqual((do_snooze.call_args[0][2] - datetime.now()).total_seconds(), 90, delta=2)

    def test_escape_from_custom_returns_to_the_menu(self):
        _, outcome, do_snooze, _ = self.run_picker("c", "9", ESC, ESC)
        self.assertIsNone(outcome)
        do_snooze.assert_not_called()

    def test_picker_has_no_wake_row(self):
        # waking is the same key on a snoozed agent; the picker only ever snoozes
        term, _, _, _ = self.run_picker(ESC)
        self.assertNotIn("Wake", term.frames[0])

    def test_frames_fit_the_popup(self):
        term, _, _, _ = self.run_picker("c", "x", ENTER, ESC, ESC)
        inner = 50 - 2  # open_picker's width minus the border
        for frame in term.frames:
            for line in frame.split("\n"):
                visible = re.sub(r"\x1b\[[0-9;]*m", "", line)
                self.assertLessEqual(len(visible), inner, repr(visible))


class OwnerOffFlag(unittest.TestCase):
    def test_reads_radars_persisted_intent(self):
        with tempfile.TemporaryDirectory() as root:
            owner = {"source": RADAR, "label": "active"}
            self.assertFalse(snooze.owner_turned_off(owner, root))
            os.makedirs(os.path.join(root, "hhdebb.herdr-radar"))
            flag = os.path.join(root, "hhdebb.herdr-radar", "agent-view.on")
            for value, expected in (("off", True), ("grouped", False)):
                with open(flag, "w") as handle:
                    handle.write(value + "\n")
                self.assertEqual(snooze.owner_turned_off(owner, root), expected)
            self.assertFalse(snooze.owner_turned_off({"source": "plugin:other"}, root))


if __name__ == "__main__":
    unittest.main()
