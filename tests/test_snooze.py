import os
import re
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


def pane(pid, workspace="w1", agent="claude", tokens=None):
    return {"pane_id": pid, "workspace_id": workspace, "agent": agent, "tokens": tokens or {}}


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
        self.assertEqual(snooze.reconcile(st, [pane("p1", tokens={"snoozed": "z"})], self.now), [])

    def test_ttl_is_capped_and_refreshed_before_it_dies(self):
        week = self.now + 7 * 24 * HOUR
        st = state({"p1": {"until": week, "dirty": True}})
        ops = snooze.reconcile(st, [pane("p1")], self.now)
        self.assertEqual(ops[0][3], snooze.MAX_TTL_MS)

        live = [pane("p1", tokens={"snoozed": "z"})]
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
        ops = snooze.reconcile(st, [pane("p1", tokens={"snoozed": "z"})], self.now, force=True)
        self.assertEqual([op[0] for op in ops], ["report"])

    def test_expired_is_cleared_and_forgotten(self):
        st = state({"p1": {"until": self.now - 1}})
        self.assertEqual(snooze.reconcile(st, [pane("p1", tokens={"snoozed": "z"})], self.now), [("clear", "p1")])
        self.assertEqual(st["panes"], {})

    def test_closed_pane_is_forgotten_silently(self):
        st = state({"gone": {"until": self.now + HOUR}})
        self.assertEqual(snooze.reconcile(st, [pane("p1")], self.now), [])
        self.assertEqual(st["panes"], {})

    def test_token_without_a_record_is_cleared(self):
        # state file lost, or a run that died after tokens were sent
        st = state({"p1": {"until": self.now + HOUR, "token_until": self.now + HOUR}})
        live = [pane("p1", tokens={"snoozed": "z"}), pane("ghost", tokens={"snoozed": "z"}), pane("plain")]
        self.assertEqual(snooze.reconcile(st, live, self.now), [("clear", "ghost")])

    def test_expiry_clear_is_not_sent_twice(self):
        st = state({"p1": {"until": self.now - 1}})
        ops = snooze.reconcile(st, [pane("p1", tokens={"snoozed": "z"})], self.now)
        self.assertEqual(ops, [("clear", "p1")])

    def test_workspace_adopts_its_panes_including_later_ones(self):
        st = state(workspaces={"w1": {"until": self.now + HOUR, "dirty": True}})
        live = [pane("p1"), pane("shell", agent=None), pane("other", workspace="w2")]
        ops = snooze.reconcile(st, live, self.now)
        self.assertEqual(sorted(op[:2] for op in ops), [("report", "p1"), ("ws_report", "w1")])
        self.assertEqual(st["panes"]["p1"]["via"], "workspace")
        self.assertNotIn("other", st["panes"])
        self.assertNotIn("shell", st["panes"])  # a token on a shell hides nothing and makes counts lie

        live[0]["tokens"] = {"snoozed": "z"}
        live.append(pane("p3"))  # an agent started later in the snoozed space
        self.assertEqual([op[:2] for op in snooze.reconcile(st, live, self.now)], [("report", "p3")])
        live[-1]["tokens"] = {"snoozed": "z"}
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
        self.assertEqual(snooze.reconcile(st, [pane("p1", tokens={"snoozed": "z"})], later), [("clear", "p1")])
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

    def arm(self, st):
        delays = []
        snooze.arm_timer(st, self.now, spawn=delays.append)
        return delays

    def test_arms_once_for_the_soonest_deadline(self):
        st = state({"a": {"until": self.now + 90_000}, "b": {"until": self.now + HOUR}})
        self.assertEqual(self.arm(st), [91])  # just after, never before
        self.assertEqual(st["timer"], self.now + 90_000)
        self.assertEqual(self.arm(st), [])  # already armed for it

    def test_rearms_when_an_earlier_deadline_appears_or_the_soonest_is_woken(self):
        st = state({"a": {"until": self.now + HOUR}})
        self.arm(st)
        st["panes"]["b"] = {"until": self.now + 60_000}
        self.assertEqual(self.arm(st), [61])
        del st["panes"]["b"]
        self.assertEqual(self.arm(st), [3601])

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
        self.assertEqual(self.arm(st), [12 * 3600 + 2])  # as the token enters its refresh margin

        later = self.now + 12 * HOUR + 2000
        ops = snooze.reconcile(st, [pane("a", tokens={"snoozed": "z"})], later)
        self.assertEqual([op[0] for op in ops], ["report"])  # that wake-up does refresh it
        delays = []
        snooze.arm_timer(st, later, spawn=delays.append)
        self.assertEqual(delays, [12 * 3600 + 2])  # and books the next one

    def test_short_snooze_is_not_woken_for_a_refresh_it_does_not_need(self):
        st = state({"a": {"until": self.now + HOUR, "dirty": True}})
        snooze.reconcile(st, [pane("a")], self.now)
        self.assertEqual(self.arm(st), [3601])

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
        self.assertEqual(self.panes[1]["tokens"], {"snoozed": mock.ANY})

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
        self.sync()
        snooze._spawn_timer.assert_called_once()
        self.sync()
        snooze._spawn_timer.assert_called_once()  # not again for the same deadline


class Commands(unittest.TestCase):
    """main() as herdr runs it: a real state dir, a fake herdr."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.panes = [dict(pane("work"), focused=True), dict(pane("zz"), focused=False)]
        self.herdr = FakeHerdr(self.panes, {"active": True, "source": RADAR, "label": "active"})
        self.notify = mock.Mock()
        for patcher in (mock.patch.dict(os.environ, {"SNOOZE_STATE_DIR": self.dir.name, "HERDR_PLUGIN_CONTEXT_JSON": ""}),
                        mock.patch.object(snooze, "call", self.herdr), mock.patch.object(snooze, "_spawn_timer"),
                        mock.patch.object(snooze, "owner_turned_off", return_value=False),
                        mock.patch.object(snooze, "notify", self.notify), mock.patch.object(snooze, "log"),
                        mock.patch.object(snooze, "load_config", return_value=dict(SyncAgainstFakeHerdr.config))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.file = os.path.join(self.dir.name, "snoozed.json")

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

    def test_idle_tick_removes_a_file_that_holds_nothing(self):
        with open(self.file, "w") as handle:
            handle.write('{"panes": {"zz": {"until": "soon"}}}')  # malformed: dropped on read
        self.assertEqual(snooze.main(["tick"]), 0)
        self.assertFalse(os.path.exists(self.file))  # or run.sh starts Python on every hook, forever
        self.assertEqual(self.herdr.calls, [])


class StateFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"SNOOZE_STATE_DIR": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self.dir.name, "snoozed.json")

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
        self.assertEqual((st["workspaces"], st["view"], st["timer"]), ({}, {}, None))
        with open(self.path, "w") as handle:
            handle.write("not json")
        self.assertTrue(snooze.is_idle(snooze.read_state()))


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
