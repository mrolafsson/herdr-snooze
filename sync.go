package main

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

// sendTokenOps sends the ops and returns the ones herdr refused.
func sendTokenOps(ops []tokenOp) []tokenOp {
	var failed []tokenOp
	for _, op := range ops {
		method, key := "pane.report_metadata", "pane_id"
		if strings.HasPrefix(op.Kind, "ws_") {
			method, key = "workspace.report_metadata", "workspace_id"
		}
		params := map[string]any{key: op.Target, "source": source()}
		switch op.Kind {
		case opReport, opWsReport:
			params["tokens"] = map[string]any{token: op.Value, mark: "1"}
			params["ttl_ms"] = op.TTL
		case opWokeReport:
			tokens := map[string]any{wokeToken: op.Value}
			if op.Badge != "" {
				tokens[wokeBadge] = op.Badge
				tokens[token] = op.Badge
			}
			params["tokens"] = tokens
			params["ttl_ms"] = op.TTL
		case opWokeClear:
			tokens := map[string]any{wokeToken: nil, wokeBadge: nil}
			if op.Badge != "keep" {
				tokens[token] = nil
			}
			params["tokens"] = tokens
		default:
			params["tokens"] = map[string]any{token: nil, mark: nil}
		}
		if err := herdrCall(method, params, nil); err != nil {
			// A pane closing mid-sync is not worth aborting for.
			logf("token op failed: %s %s: %v", op.Kind, op.Target, err)
			failed = append(failed, op)
		}
	}
	return failed
}

type syncOpts struct {
	// force: a restart. Every token is re-sent, and the snoozed list ends.
	force bool
	// strict (recovery): fail if herdr refused to clear any token, rather
	// than letting the caller take it as done.
	strict bool
}

func focusedPane(panes []pane, agentsOnly bool) string {
	for _, p := range panes {
		if p.Focused && (p.Agent != "" || !agentsOnly) {
			return p.PaneID
		}
	}
	return ""
}

// syncState reconciles the state with herdr: tokens, the timer and the
// agent view. It returns the panes it saw.
func syncState(st *state, cfg config, opts syncOpts) ([]pane, error) {
	panes, err := listPanes()
	if err != nil {
		return nil, err
	}
	now := nowMS()
	wakeOnChange(st, panes, cfg.WakeBlocked)
	ops := reconcile(st, panes, now, opts.force, cfg.Badge)
	ops = append(ops, trackWoke(st, panes, now, opts.force, cfg.WokeBadge)...)
	failed := sendTokenOps(ops)
	clearsFailed := 0
	for _, op := range failed {
		switch op.Kind {
		case opReport:
			// Not seen in pane.list, so the next pass resends it anyway;
			// marked so a pass with the token present still does.
			if e := st.Panes[op.Target]; e != nil {
				e.Dirty = true
			}
		case opWsReport:
			// Workspace tokens can't be read back, so nothing else would
			// notice.
			if e := st.Workspaces[op.Target]; e != nil {
				e.Dirty = true
			}
		case opWokeReport, opWokeClear:
			// Absent from the next pane.list or still there: resent then.
		default:
			clearsFailed++
		}
	}
	if opts.strict && clearsFailed > 0 {
		return nil, &herdrError{"recovery_incomplete", fmt.Sprintf("herdr refused to clear %d token(s)", len(failed))}
	}
	// Only an *agent* pane counts as "went back to work": a shell, or the
	// picker popup itself taking focus, is not that.
	if cfg.AutoReturn && settleShowing(st, focusedPane(panes, true)) {
		logf("left the snoozed list: focus moved to an active agent")
	}
	if opts.force {
		// A restart. The sleeper may be gone, and nobody wants herdr to come
		// back up listing only snoozed agents.
		st.Timer = nil
		st.View.Showing, st.View.FromPane = "", ""
	}
	armTimer(st, now, spawnTimer)

	owner, err := probeView()
	if err != nil {
		return nil, err
	}
	agents := map[string]bool{}
	blocked := 0
	for _, p := range panes {
		if p.Agent == "" {
			continue
		}
		agents[p.PaneID] = true
		if _, snoozed := st.Panes[p.PaneID]; p.AgentStatus == "blocked" && !snoozed {
			blocked++
		}
	}
	count := 0
	for pid := range st.Panes {
		if agents[pid] {
			count++
		}
	}
	// Whose "off" flag to honour: the plugin holding the view right now, and
	// only while we hold it ourselves, the one we borrowed it from.
	offOwner := st.View.Inherited
	if owner.Active && owner.Source != source() {
		offOwner = &inheritedView{Source: owner.Source, Label: owner.Label}
	}
	before := st.View
	action := planView(st, viewPlan{
		owner: owner, count: count, blocked: blocked, mode: panelMode(),
		ownerOff: ownerTurnedOff(offOwner), cfg: cfg,
	})
	if action != nil {
		if err := herdrCall(action.Method, action.Params, nil); err != nil {
			// planView already recorded the view as changed. If this was the
			// last clear, that record would read "idle", the file would be
			// deleted, hooks would stop looking — and herdr would keep our
			// filter, hiding agents, with nothing left to retry. Keep what
			// was true before, so the next hook tries again.
			st.View = before
			if st.View.empty() {
				st.View = recoverView()
			}
			return nil, err
		}
	}
	if len(ops) > 0 || action != nil {
		what := "view unchanged"
		if action != nil {
			what = "view " + strings.TrimPrefix(action.Method, "agent.view.")
		}
		logf("sync: %d token op(s); %s", len(ops), what)
	}
	return panes, nil
}

// errStale is a snooze refused because the pane is no longer what the
// picker showed.
var errStale = errors.New("stale target")

// snooze snoozes a pane (kind "pane") or a workspace until a deadline, and
// returns how many panes are now covered.
//
// expect is the agent the picker showed. The popup can stay open a while;
// if that pane has since closed, moved (a new pane ID), or a different agent
// started in it, snoozing now would hide something the person never chose —
// so it's refused instead.
//
// when is a condition to wake on as well (whenDone, whenMerged), with until
// its cap; "" for a plain snooze.
func snooze(kind, target string, until time.Time, cfg config, expect *agentID, when string) (int, error) {
	return snoozeExpected(kind, target, until, cfg, expect, when, "", nil)
}

// Remote rows also pin the terminal identity across the final state-file lock.
func snoozeExpected(kind, target string, until time.Time, cfg config, expect *agentID, when, terminal string, check func() error) (int, error) {
	untilMS := until.UnixMilli()
	covered := 0
	err := withState(func(st *state) error {
		if check != nil {
			if err := check(); err != nil {
				return err
			}
		}
		if kind == "workspace" {
			st.Workspaces[target] = &spaceEntry{Until: untilMS, Dirty: true}
			for _, e := range st.Panes {
				if e.WorkspaceID == target {
					e.Until, e.Via, e.Dirty = untilMS, viaWorkspace, true
				}
			}
		} else {
			record := &paneEntry{Until: untilMS, Via: viaPane, Dirty: true, Since: nowMS(), When: when}
			if expect != nil {
				panes, err := listPanes()
				if err != nil {
					return err
				}
				var found *pane
				for i := range panes {
					if panes[i].PaneID == target {
						found = &panes[i]
					}
				}
				switch {
				case found == nil:
					return fmt.Errorf("%w: that pane moved or closed; open snooze on it again", errStale)
				case terminal != "" && found.TerminalID != terminal:
					return fmt.Errorf("%w: that terminal was replaced; refresh the agent list", errStale)
				case agentIdentity(*found) == nil:
					return fmt.Errorf("%w: the agent there has exited; open snooze on it again", errStale)
				case replaced(&paneEntry{AgentID: expect}, *found):
					return fmt.Errorf("%w: a different agent is running there now; open snooze on it again", errStale)
				}
				record.AgentID = agentIdentity(*found)
				record.TerminalID = found.TerminalID
				record.WorkspaceID = found.WorkspaceID
				record.Status = found.AgentStatus
			}
			st.Panes[target] = record
			// Its own deadline now outranks its space's: without this, a
			// shorter snooze ends, the agent reappears, and the space
			// re-adopts it a tick later for the rest of the space's (longer)
			// snooze.
			for _, space := range st.Workspaces {
				space.addExcept(target)
			}
		}
		panes, err := syncState(st, cfg, syncOpts{})
		for _, p := range panes {
			// Remember where a pane lives, so a later workspace snooze or
			// wake finds it.
			if e := st.Panes[p.PaneID]; e != nil {
				e.WorkspaceID = p.WorkspaceID
			}
		}
		if kind == "workspace" {
			for _, e := range st.Panes {
				if e.WorkspaceID == target {
					covered++
				}
			}
		} else if st.Panes[target] != nil {
			covered = 1
		}
		return err
	})
	return covered, err
}

// wake ends snoozes now: these panes, these workspaces (and the panes they
// covered), or everything. It returns how many panes it woke.
func wake(cfg config, paneIDs, workspaceIDs []string, everything bool) (int, error) {
	return wakeExpected(cfg, paneIDs, workspaceIDs, everything, nil)
}

// check runs under the state lock, before any state or token is cleared.
func wakeExpected(cfg config, paneIDs, workspaceIDs []string, everything bool, check func() error) (int, error) {
	woken := 0
	err := withState(func(st *state) error {
		if check != nil {
			if err := check(); err != nil {
				return err
			}
		}
		var ops []tokenOp
		var spaces []string
		for _, wid := range sortedKeys(st.Workspaces) {
			if everything || contains(workspaceIDs, wid) {
				spaces = append(spaces, wid)
			}
		}
		for _, wid := range spaces {
			delete(st.Workspaces, wid)
			ops = append(ops, tokenOp{Kind: opWsClear, Target: wid})
		}
		for _, pid := range sortedKeys(st.Panes) {
			e := st.Panes[pid]
			inWokenSpace := e.Via == viaWorkspace && contains(spaces, e.WorkspaceID)
			if !(everything || contains(paneIDs, pid) || inWokenSpace) {
				continue
			}
			delete(st.Panes, pid)
			ops = append(ops, tokenOp{Kind: opClear, Target: pid})
			woken++
			if space := st.Workspaces[e.WorkspaceID]; space != nil {
				space.addExcept(pid) // woken early out of a still-snoozed space: don't re-adopt it
			}
		}
		sendTokenOps(ops)
		_, err := syncState(st, cfg, syncOpts{})
		return err
	})
	return woken, err
}
