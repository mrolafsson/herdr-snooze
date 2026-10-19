// herdr-snooze: hide an agent (or every agent in a space) from herdr's
// Agents panel until a deadline.
//
// How hiding works: a snoozed pane carries a metadata token, and the Agents
// view is filtered to leave out panes that have it. The token is reported
// with a TTL, so herdr itself drops it at the deadline and the agent
// reappears on time with no process of ours running.
//
// What `tick` is for: the token TTL is capped at 24h, tokens and the view
// die with the server, and herdr has ONE agent view that any caller can
// replace. `tick` (the startup hook and a few event hooks) reconciles the
// state file against all three. It returns at once when nothing is snoozed.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"
)

const usage = `herdr-snooze — hide agents from herdr's Agents panel until a deadline

  action agent|workspace     what the herdr actions run: snooze (a popup asks
                             how long) or, if it's snoozed, wake
  action wake                wake the focused pane's agent, no popup
  action manage              the popup listing what's snoozed, and every agent
  action demo                that popup on fictional agents
  picker [--demo]            the popup itself; --demo (or HERDR_SNOOZE_DEMO=1)
                             shows fictional agents, safe to screenshot
  tick [--force|--detected|--timer]
                             reconcile tokens and the agent view (the hooks)
  toggle                     Agents panel: priority -> grouped -> only snoozed
  order                      Agents panel: priority <-> grouped, while snoozed
  snooze <pane_id|workspace:ID> <duration|done|merged>
  wake --all | <pane_id|workspace:ID>...
  list                       what's snoozed, as text
  handoff JSON               (internal) open another plugin's popup once this
                             one has closed
`

func main() {
	code, err := run(os.Args[1:])
	if err != nil {
		var he *herdrError
		if errors.As(err, &he) {
			fmt.Fprintln(os.Stderr, "snooze: herdr call failed:", err)
		} else {
			fmt.Fprintln(os.Stderr, "snooze:", err)
		}
		if code == 0 {
			code = 1
		}
	}
	os.Exit(code)
}

func run(args []string) (int, error) {
	if len(args) == 0 {
		fmt.Print(usage)
		return 2, nil
	}
	command, args := args[0], args[1:]
	switch {
	case command == "remote-info" && len(args) == 0:
		info, err := connectionInfo()
		if err != nil {
			return 1, err
		}
		return 0, json.NewEncoder(os.Stdout).Encode(info)
	case command == "rpc" && len(args) == 0:
		return 0, serveRemote(os.Stdin, os.Stdout)
	case command == "action" && len(args) > 0:
		return runAction(args[0])
	case command == "picker":
		demo := os.Getenv(envDemo) == "1" || (len(args) > 0 && args[0] == "--demo")
		return 0, runPicker(demo)
	case command == "handoff" && len(args) == 1:
		return 0, runHandoff(args[0])
	case command == "tick":
		return 0, tick(args)
	case command == "toggle":
		return 0, toggle()
	case command == "order":
		return 0, flipOrder()
	case command == "snooze" && len(args) == 2:
		kind, target := "pane", args[0]
		if id, ok := strings.CutPrefix(args[0], "workspace:"); ok {
			kind, target = "workspace", id
		}
		// "done" or "merged": until that happens, at the latest tomorrow.
		when, until, err := "", time.Time{}, error(nil)
		if args[1] == whenDone || args[1] == whenMerged {
			when, until = args[1], conditionCap(timeNow())
		} else if until, err = parseUntil(args[1], timeNow()); err != nil {
			return 2, err
		}
		covered, err := snooze(kind, target, until, loadConfig(), nil, when)
		if err != nil {
			return 1, err
		}
		logf("snoozed %s %s (%d pane(s)) until %s", kind, target, covered, until.Format("2006-01-02 15:04:05"))
		if covered == 0 {
			return 1, nil
		}
		return 0, nil
	case command == "wake" && len(args) > 0:
		var panes, spaces []string
		all := false
		for _, a := range args {
			switch {
			case a == "--all":
				all = true
			case strings.HasPrefix(a, "workspace:"):
				spaces = append(spaces, strings.TrimPrefix(a, "workspace:"))
			default:
				panes = append(panes, a)
			}
		}
		woken, err := wake(loadConfig(), panes, spaces, all)
		if err != nil {
			return 1, err
		}
		logf("woke %d pane(s)", woken)
		return 0, nil
	case command == "list":
		printList()
		return 0, nil
	}
	fmt.Print(usage)
	return 2, nil
}

func printList() {
	st, now := readState(), nowMS()
	ids := sortedKeys(st.Workspaces)
	for _, wid := range ids {
		s := st.Workspaces[wid]
		fmt.Printf("workspace:%s\t%s left\tuntil %s\n", wid, formatLeft(s.Until-now), fromMS(s.Until).Format("2006-01-02 15:04"))
	}
	pids := make([]string, 0, len(st.Panes))
	for pid := range st.Panes {
		pids = append(pids, pid)
	}
	sort.Strings(pids)
	for _, pid := range pids {
		e := st.Panes[pid]
		fmt.Printf("%s\t%s left\tuntil %s\t(%s)\n", pid, formatLeft(e.Until-now), fromMS(e.Until).Format("2006-01-02 15:04"), e.Via)
	}
	if st.idle() {
		fmt.Println("nothing snoozed")
	}
}
