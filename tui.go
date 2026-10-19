package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
)

// The popup has three screens: the list of every agent, snoozed ones first;
// the durations to snooze one for; and a line to type your own.
type screen int

const (
	screenList screen = iota
	screenPick
	screenCustom
)

// choice is one configured duration.
type choice struct {
	spec, label string
}

// rowKind is what a line of the list is.
type rowKind int

const (
	rowSpace  rowKind = iota // a snoozed space
	rowPane                  // an agent, snoozed or not
	rowHeader                // "Just woke", "Snoozed", "Awake": not selectable
)

type row struct {
	kind    rowKind
	id      string // pane or workspace ID; for a header, its title
	title   string
	where   string
	status  string
	snoozed bool
	until   int64      // ms, while snoozed
	via     string     // a snoozed pane's: viaPane or viaWorkspace
	when    string     // a snoozed pane's condition (whenDone, whenMerged), if any
	woke    *wokeEntry // on the "Just woke" list
	count   int        // a header's rows, a space's agents
	p       pane
	space   string // its workspace's ID
}

func (r row) key() string { return strconv.Itoa(int(r.kind)) + ":" + r.id }

type loadedMsg struct {
	st       *state
	panes    []pane
	spaces   []workspaceInfo
	err      error
	warnings []string
}

// doneMsg is a snooze or a wake that finished. note is what to say.
type doneMsg struct {
	note string
	err  error
}

type clockMsg struct{}

type focusedMsg struct{ err error }

// linksMsg is the other plugins' popups, for the keys that open them.
type linksMsg map[string]popupRef

type model struct {
	cfg    config
	world  world
	screen screen
	// openOn is the agent the popup was opened on, by another plugin or
	// coming back from one: until it's loaded, then "".
	openOn string
	// back is the popup this one was opened from, to go back to.
	back  *popupRef
	links map[string]popupRef
	// fromList: the durations were opened from the list, and go back there.
	fromList bool
	target   target
	choices  []choice
	pick     int
	input    textinput.Model
	tried    bool // enter was pressed on what's typed

	rows        []row
	cursor      int
	offset      int
	loaded      bool
	st          *state
	labels      map[string]string // workspace labels by ID
	panes       []pane
	busy        bool
	flash       string
	err         string
	machineNote string
	outcome     string // what was done, for the log, when the popup closes on it

	width, height  int
	mouseX, mouseY int
}

func newModel(cfg config, w world, t target, list bool, openOn string, back *popupRef) model {
	in := textinput.New()
	in.Prompt = "› "
	in.Placeholder = "45m, 2h, 3d, 17:30, tomorrow 9am"
	in.CharLimit = 40
	m := model{cfg: cfg, world: w, target: t, input: in, st: emptyState(), labels: map[string]string{},
		openOn: openOn, back: back}
	for _, spec := range cfg.Durations {
		if len(m.choices) == 9 {
			break
		}
		if _, err := parseUntil(spec, timeNow()); err == nil {
			m.choices = append(m.choices, choice{spec, describe(spec)})
		}
	}
	if list {
		m.screen = screenList
	} else {
		m.screen = screenPick
	}
	return m
}

func (m model) Init() tea.Cmd {
	w := m.world
	return tea.Batch(m.load(), clock(), func() tea.Msg { return linksMsg(w.siblings()) })
}

func clock() tea.Cmd {
	return tea.Tick(time.Second, func(time.Time) tea.Msg { return clockMsg{} })
}

func (m model) load() tea.Cmd {
	w := m.world
	return func() tea.Msg { return w.load() }
}

// ── rows ──────────────────────────────────────────────────────────────────────

// buildRows is the list: what just woke by itself (the latest first), what
// is snoozed (spaces, then agents, the soonest to wake first), then every
// other agent, in herdr's order.
func buildRows(st *state, panes []pane, labels map[string]string) []row {
	live := map[string]pane{}
	for _, p := range panes {
		live[p.PaneID] = p
	}
	where := func(p pane) string {
		var parts []string
		for _, s := range []string{p.Agent, labels[p.WorkspaceID]} {
			if s != "" {
				parts = append(parts, s)
			}
		}
		return strings.Join(parts, " · ")
	}
	snoozedAgent := func(pid string, e *paneEntry, p pane) row {
		return row{kind: rowPane, id: pid, title: paneLabel(p), where: where(p), status: p.AgentStatus,
			snoozed: true, until: e.Until, via: e.Via, when: e.When, p: p, space: p.WorkspaceID}
	}
	byUntil := func(rows []row) {
		sort.SliceStable(rows, func(i, j int) bool {
			if rows[i].until != rows[j].until {
				return rows[i].until < rows[j].until
			}
			return rows[i].id < rows[j].id
		})
	}
	// A snoozed space, with the agents it hides under it; then the agents
	// snoozed on their own, the soonest to wake first.
	var woke, snoozed, own, awake []row
	agents := 0
	for _, wid := range sortedKeys(st.Workspaces) {
		var under []row
		for pid, e := range st.Panes {
			if p, ok := live[pid]; ok && e.WorkspaceID == wid && e.Via == viaWorkspace {
				under = append(under, snoozedAgent(pid, e, p))
			}
		}
		sort.Slice(under, func(i, j int) bool { return under[i].id < under[j].id })
		label := labels[wid]
		if label == "" {
			label = wid
		}
		snoozed = append(snoozed, row{kind: rowSpace, id: wid, title: label, snoozed: true,
			until: st.Workspaces[wid].Until, count: len(under), space: wid})
		snoozed = append(snoozed, under...)
		agents += len(under)
	}
	for pid, e := range st.Panes {
		if p, ok := live[pid]; ok && (e.Via != viaWorkspace || st.Workspaces[e.WorkspaceID] == nil) {
			own = append(own, snoozedAgent(pid, e, p))
		}
	}
	byUntil(own)
	snoozed = append(snoozed, own...)
	agents += len(own)
	for _, p := range panes {
		if p.Agent == "" || st.Panes[p.PaneID] != nil {
			continue
		}
		if e := st.Woke[p.PaneID]; e != nil {
			woke = append(woke, row{kind: rowPane, id: p.PaneID, title: paneLabel(p), where: where(p), status: p.AgentStatus,
				woke: e, p: p, space: p.WorkspaceID})
			continue
		}
		awake = append(awake, row{kind: rowPane, id: p.PaneID, title: paneLabel(p), where: where(p), status: p.AgentStatus, p: p, space: p.WorkspaceID})
	}
	sort.SliceStable(woke, func(i, j int) bool { return woke[i].woke.At > woke[j].woke.At })
	var rows []row
	section := func(title string, count int, these []row) {
		if len(these) == 0 {
			return
		}
		if len(rows) > 0 {
			rows = append(rows, row{kind: rowHeader}) // a blank line between
		}
		rows = append(rows, row{kind: rowHeader, id: title, count: count})
		rows = append(rows, these...)
	}
	section("Just woke", len(woke), woke)
	section("Snoozed", agents, snoozed)
	section("Awake", len(awake), awake)
	return rows
}

func (m *model) setRows(rows []row) {
	keep := ""
	if r := m.selected(); r != nil {
		keep = r.key()
	}
	m.rows = rows
	m.cursor = -1
	for i, r := range rows {
		if r.key() == keep {
			m.cursor = i
		}
	}
	if m.cursor < 0 {
		m.cursor = 0
		m.move(1)
		m.move(-1)
	}
	m.scrollTo()
}

func (m model) selected() *row {
	if m.cursor < 0 || m.cursor >= len(m.rows) || m.rows[m.cursor].kind == rowHeader {
		return nil
	}
	return &m.rows[m.cursor]
}

// move goes delta selectable rows up or down, stopping at the ends.
func (m *model) move(delta int) {
	if len(m.rows) == 0 {
		return
	}
	i := m.cursor
	for step := 0; step != delta; {
		dir := 1
		if delta < 0 {
			dir = -1
		}
		next := i + dir
		for next >= 0 && next < len(m.rows) && m.rows[next].kind == rowHeader {
			next += dir
		}
		if next < 0 || next >= len(m.rows) {
			break
		}
		i = next
		step += dir
	}
	if i >= 0 && i < len(m.rows) && m.rows[i].kind != rowHeader {
		m.cursor = i
	} else if m.rows[m.cursor].kind == rowHeader {
		// Nowhere to go from a header: the first row that isn't one.
		for j, r := range m.rows {
			if r.kind != rowHeader {
				m.cursor = j
				break
			}
		}
	}
	m.scrollTo()
}

// The list's layout: a title, a blank, the rows, then a status line and the
// keys.
const listTop = 2

func (m model) listHeight() int { return max(1, m.height-listTop-2) }

func (m *model) scrollTo() {
	h := m.listHeight()
	top := m.cursor
	// Keep a section's header in sight with its first row.
	if top > 0 && m.rows[top-1].kind == rowHeader {
		top--
	}
	if top < m.offset {
		m.offset = top
	}
	if m.cursor >= m.offset+h {
		m.offset = m.cursor - h + 1
	}
	m.offset = max(0, min(m.offset, len(m.rows)-h))
}

// ── update ────────────────────────────────────────────────────────────────────

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.input.Width = max(10, m.width-8)
		m.scrollTo()
		return m, nil
	case clockMsg:
		// The time left counts down; what's snoozed is looked at again
		// every few seconds (a hook may have woken something).
		if time.Now().Second()%3 == 0 && !m.busy {
			return m, tea.Batch(clock(), m.load())
		}
		return m, clock()
	case loadedMsg:
		m.loaded = true
		if msg.err != nil {
			m.err = msg.err.Error()
			return m, nil
		}
		m.machineNote = machineWarning(msg.warnings)
		m.st, m.panes = msg.st, msg.panes
		m.labels = map[string]string{}
		for _, w := range msg.spaces {
			m.labels[w.WorkspaceID] = w.Label
		}
		m.setRows(buildRows(m.st, m.panes, m.labels))
		if m.openOn != "" {
			m.arrive()
		}
		return m, nil
	case linksMsg:
		m.links = msg
		return m, nil
	case doneMsg:
		m.busy = false
		if msg.err != nil {
			m.err = msg.err.Error()
			return m, nil
		}
		m.err = ""
		if !m.fromList && m.screen != screenList {
			m.outcome = msg.note
			return m.leave()
		}
		m.flash, m.screen = msg.note, screenList
		return m, m.load()
	case focusedMsg:
		if msg.err != nil {
			m.err = msg.err.Error()
			return m, nil
		}
		return m, tea.Quit
	case tea.MouseMsg:
		return m.handleMouse(msg)
	case tea.KeyMsg:
		return m.handleKey(msg)
	}
	return m, nil
}

func (m model) handleKey(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	if k.String() == "ctrl+c" {
		return m, tea.Quit
	}
	if m.busy {
		return m, nil
	}
	switch m.screen {
	case screenCustom:
		return m.customKey(k)
	case screenPick:
		return m.pickKey(k)
	}
	return m.listKey(k)
}

func (m model) listKey(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	m.flash, m.err = "", ""
	switch k.String() {
	case "esc", "q":
		return m.leave()
	case "up", "k", "ctrl+p":
		m.move(-1)
	case "down", "j", "ctrl+n":
		m.move(1)
	case "pgup":
		m.move(-m.listHeight())
	case "pgdown":
		m.move(m.listHeight())
	case "home", "g":
		m.move(-len(m.rows))
	case "end", "G":
		m.move(len(m.rows))
	case "enter":
		return m.goTo()
	case "s":
		// Snooze it: an awake agent for the first time, a snoozed one (or
		// space) for a new time.
		if r := m.selected(); r != nil {
			return m.choose(r, r.kind == rowSpace)
		}
	case "S":
		if r := m.selected(); r != nil && r.space != "" && m.st.Workspaces[r.space] == nil {
			return m.choose(r, true)
		}
	case "w":
		return m.wakeSelected()
	case "W":
		if len(m.st.Panes)+len(m.st.Workspaces) > 0 {
			return m.wakeCmd(nil, nil, true, "Woke everything")
		}
	case "r":
		if r := m.selected(); r != nil && r.kind == rowPane {
			if _, remote, _ := parseRemoteRef(r.id); remote {
				m.err = "Open Recap on the owning machine to read this agent."
				return m, nil
			}
			if to, ok := m.links[recapPlugin]; ok {
				if err := m.world.open(recapFor(to, r.id)); err != nil {
					m.err = err.Error()
					return m, nil
				}
				return m, tea.Quit
			}
		}
	}
	return m, nil
}

// leave closes the popup: back to the one it was opened from, if it was.
func (m model) leave() (tea.Model, tea.Cmd) {
	if m.back != nil {
		if err := m.world.open(*m.back); err != nil {
			logf("couldn't go back to %s: %v", m.back.PluginID, err)
		}
	}
	return m, tea.Quit
}

// arrive puts the popup on the agent it was opened on, once the agents are
// loaded: selected in the list, or with its durations.
func (m *model) arrive() {
	pid := m.openOn
	m.openOn = ""
	var r *row
	for i := range m.rows {
		if m.rows[i].kind == rowPane && m.rows[i].id == pid {
			r = &m.rows[i]
			if m.screen == screenList {
				m.cursor = i
				m.scrollTo()
				return
			}
		}
	}
	if m.screen == screenList {
		return
	}
	if r == nil {
		m.screen, m.err = screenList, "That agent isn't there any more."
		return
	}
	m.target = paneTarget(r.p, m.labels[r.p.WorkspaceID])
	if r.snoozed {
		m.target.Detail += " · now until " + formatUntil(fromMS(r.until), timeNow())
		m.target.Snoozed = true
	}
}

func (m model) spaceLabel(wid string) string {
	if l := m.labels[wid]; l != "" {
		return l
	}
	return wid
}

// goTo focuses the selected agent (a space's first) and closes the popup.
func (m model) goTo() (tea.Model, tea.Cmd) {
	r := m.selected()
	if r == nil {
		return m, nil
	}
	pid := r.id
	if r.kind == rowSpace {
		pid = ""
		for _, p := range m.panes {
			if p.WorkspaceID == r.id && p.Agent != "" {
				pid = p.PaneID
				break
			}
		}
		if pid == "" {
			m.flash = "No agent in that space to go to."
			return m, nil
		}
	}
	w := m.world
	return m, func() tea.Msg { return focusedMsg{w.focus(pid)} }
}

// wakeSelected wakes the selected space or agent, if it's snoozed.
func (m model) wakeSelected() (tea.Model, tea.Cmd) {
	r := m.selected()
	switch {
	case r == nil || !r.snoozed:
		return m, nil
	case r.kind == rowSpace:
		return m.wakeCmd(nil, []string{r.id}, false, "Woke "+r.title)
	}
	return m.wakeCmd([]string{r.id}, nil, false, "Woke "+shorten(r.title, 40))
}

// choose opens the durations for the row: its agent, or (space) its space.
func (m model) choose(r *row, space bool) (tea.Model, tea.Cmd) {
	if space {
		w := workspaceInfo{WorkspaceID: r.space, Label: m.labels[r.space]}
		m.target = spaceTarget(w, m.panes)
	} else {
		m.target = paneTarget(r.p, m.labels[r.p.WorkspaceID])
	}
	if r.snoozed {
		m.target.Detail += " · now until " + formatUntil(fromMS(r.until), timeNow())
		m.target.Snoozed = true
	}
	m.screen, m.fromList, m.pick, m.flash, m.err = screenPick, true, 0, "", ""
	return m, nil
}

func (m model) wakeCmd(panes, spaces []string, all bool, note string) (tea.Model, tea.Cmd) {
	m.busy = true
	cfg, w := m.cfg, m.world
	return m, func() tea.Msg { return doneMsg{note: note, err: w.wake(cfg, panes, spaces, all)} }
}

// condRow is a row after Custom…: a condition this agent can be snoozed
// until.
type condRow struct{ when, key, label string }

// conditions are the rows after Custom…: until done for an agent that's
// working, until merged for one with a pull request open. Not for a space.
func (m model) conditions() []condRow {
	if m.target.Kind != "pane" {
		return nil
	}
	var rows []condRow
	if m.target.Status == "working" {
		rows = append(rows, condRow{whenDone, "d", "until done"})
	}
	if m.target.PR != "" {
		rows = append(rows, condRow{whenMerged, "m", "until " + m.target.PR + " is merged"})
	}
	return rows
}

// pickRows is how many rows the durations take: the presets, Custom…, and
// the conditions.
func (m model) pickRows() int { return len(m.choices) + 1 + len(m.conditions()) }

func (m model) pickKey(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	m.err = ""
	n := m.pickRows()
	switch s := k.String(); s {
	case "esc", "q":
		if m.fromList {
			m.screen = screenList
			return m, nil
		}
		return m.leave()
	case "w":
		if m.target.Snoozed {
			name := shorten(m.target.Label, 40)
			if m.target.Kind == "workspace" {
				return m.wakeCmd(nil, []string{m.target.ID}, false, "Woke "+name)
			}
			return m.wakeCmd([]string{m.target.ID}, nil, false, "Woke "+name)
		}
	case "tab":
		m.screen, m.fromList = screenList, false
		return m, nil
	case "up", "k", "ctrl+p":
		m.pick = (m.pick - 1 + n) % n
	case "down", "j", "ctrl+n":
		m.pick = (m.pick + 1) % n
	case "enter":
		return m.choosePick(m.pick)
	case "c":
		return m.choosePick(len(m.choices))
	case "d", "m":
		for j, c := range m.conditions() {
			if c.key == k.String() {
				return m.choosePick(len(m.choices) + 1 + j)
			}
		}
	default:
		if d, err := strconv.Atoi(s); err == nil && d >= 1 && d <= len(m.choices) {
			return m.choosePick(d - 1)
		}
	}
	return m, nil
}

func (m model) choosePick(i int) (tea.Model, tea.Cmd) {
	m.pick = i
	if c := i - len(m.choices) - 1; c >= 0 && c < len(m.conditions()) {
		return m.snoozeCmd(conditionCap(timeNow()), m.conditions()[c].when)
	}
	if i >= len(m.choices) {
		m.screen, m.tried = screenCustom, false
		m.input.SetValue("")
		return m, m.input.Focus()
	}
	// Read the clock now: a popup left open for 20 minutes must not turn
	// "15 minutes" into a deadline that has already passed.
	until, err := parseUntil(m.choices[i].spec, timeNow())
	if err != nil {
		m.err = err.Error()
		return m, nil
	}
	return m.snoozeCmd(until, "")
}

func (m model) customKey(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.String() {
	case "esc":
		m.screen = screenPick
		m.input.Blur()
		return m, nil
	case "enter":
		until, err := parseUntil(m.input.Value(), timeNow())
		if err != nil {
			m.tried = true
			return m, nil
		}
		return m.snoozeCmd(until, "")
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(k)
	m.tried = false
	return m, cmd
}

// snoozeCmd snoozes the target until then, or (when) until that happens,
// then at the latest.
func (m model) snoozeCmd(until time.Time, when string) (tea.Model, tea.Cmd) {
	m.busy = true
	t, cfg, w := m.target, m.cfg, m.world
	return m, func() tea.Msg {
		if err := w.snooze(t, until, when, cfg); err != nil {
			// A stale target says why in its own words.
			return doneMsg{err: errors.New(strings.TrimPrefix(err.Error(), errStale.Error()+": "))}
		}
		if when != "" {
			return doneMsg{note: fmt.Sprintf("Snoozed %s %s", shorten(t.Label, 30), conditionLabel(when))}
		}
		return doneMsg{note: fmt.Sprintf("Snoozed %s until %s", shorten(t.Label, 30), formatUntil(until, timeNow()))}
	}
}

// ── mouse ─────────────────────────────────────────────────────────────────────

func (m model) handleMouse(ev tea.MouseMsg) (tea.Model, tea.Cmd) {
	m.mouseX, m.mouseY = ev.X, ev.Y
	if m.busy {
		return m, nil
	}
	i, onRow := m.rowAt(ev.Y)
	switch {
	case ev.Action == tea.MouseActionMotion:
		if onRow {
			m.hover(i)
		}
		return m, nil
	case ev.Action != tea.MouseActionPress:
		return m, nil
	case ev.Button == tea.MouseButtonWheelUp:
		return m.handleKey(tea.KeyMsg{Type: tea.KeyUp})
	case ev.Button == tea.MouseButtonWheelDown:
		return m.handleKey(tea.KeyMsg{Type: tea.KeyDown})
	case ev.Button != tea.MouseButtonLeft:
		return m, nil
	}
	if ev.Y == m.height-1 {
		if k := hintAt(m.footer(), ev.X, m.width); k != "" {
			return m.handleKey(keyMsg(k))
		}
		return m, nil
	}
	if !onRow {
		return m, nil
	}
	m.hover(i)
	if m.screen == screenPick {
		return m.choosePick(i)
	}
	return m, nil
}

func (m *model) hover(i int) {
	if m.screen == screenPick {
		m.pick = i
	} else if m.screen == screenList && m.rows[i].kind != rowHeader {
		m.cursor = i
	}
}

// rowAt is the row (or duration) drawn on line y.
func (m model) rowAt(y int) (int, bool) {
	switch m.screen {
	case screenPick:
		top, first, n := m.pickWindow()
		if i := y - top + first; y >= top && i < first+n {
			return i, true
		}
	case screenList:
		if i := y - listTop + m.offset; y >= listTop && y < listTop+m.listHeight() && i < len(m.rows) {
			return i, true
		}
	}
	return 0, false
}

// ── view ──────────────────────────────────────────────────────────────────────

func (m model) View() string {
	if m.width == 0 {
		return ""
	}
	var lines []string
	switch m.screen {
	case screenList:
		lines = m.viewList()
	case screenPick:
		lines = m.viewPick()
	case screenCustom:
		lines = m.viewCustom()
	}
	// The status line and the keys always sit at the bottom.
	body := m.height - 2
	for len(lines) < body {
		lines = append(lines, "")
	}
	lines = append(lines[:body], m.statusLine(), m.renderFooter())
	return screenSafe(strings.Join(lines, "\n"))
}

func (m model) header() []string {
	mark := ""
	if m.target.Kind == "workspace" {
		mark = snoozedStyle().Render("◫") + " "
	} else if m.target.Status != "" || m.target.Agent != nil {
		mark = agentMark(m.target.Status, "") + " "
	}
	out := []string{"", " " + mark + styleTitle.Render(shorten(m.target.Label, m.width-4))}
	if m.target.Detail != "" {
		out = append(out, "   "+styleDim.Render(shorten(m.target.Detail, m.width-4)))
	}
	return append(out, "")
}

// pickWindow is where the durations are drawn: the line the first is on,
// which is first, and how many fit.
func (m model) pickWindow() (top, first, n int) {
	top = len(m.header())
	total := m.pickRows()
	room := max(1, m.height-top-3)
	n = min(total, room)
	first = 0
	if total > room {
		first = min(max(0, m.pick-room/2), total-room)
	}
	return top, first, n
}

func (m model) viewPick() []string {
	lines := m.header()
	_, first, n := m.pickWindow()
	now := timeNow()
	conds := m.conditions()
	for i := first; i < first+n; i++ {
		key, label, right := "c", "Custom…", ""
		switch c := i - len(m.choices) - 1; {
		case i < len(m.choices):
			key, label = strconv.Itoa(i+1), m.choices[i].label
			if until, err := parseUntil(m.choices[i].spec, now); err == nil {
				right = formatUntil(until, now)
			}
		case c >= 0:
			// The cap, said as what happens if it doesn't.
			key, label, right = conds[c].key, conds[c].label, "or "+formatUntil(conditionCap(now), now)
		}
		lines = append(lines, m.choiceLine(key, label, right, i == m.pick))
	}
	return lines
}

func (m model) choiceLine(key, label, right string, selected bool) string {
	w := m.width - 2
	left := " " + styleTree.Render(key) + "  "
	title := styleLead.Render(label)
	if selected {
		title = styleTitle.Render(label)
	}
	line := fitRow(left, title, styleDim.Render(right), w)
	if selected {
		return " " + highlight(line, w)
	}
	return " " + line
}

func (m model) viewCustom() []string {
	lines := m.header()
	lines = append(lines, " "+styleLead.Render("Snooze for how long, or until when?"), "", " "+m.input.View(), "")
	now := timeNow()
	text := m.input.Value()
	until, err := parseUntil(text, now)
	switch {
	case strings.TrimSpace(text) == "":
		lines = append(lines, "   "+styleDim.Render("45m · 2h · 3d · 1w · 17:30 · tomorrow 9am"))
	case err == nil:
		lines = append(lines, "   "+snoozedStyle().Render("until "+formatUntil(until, now))+styleDim.Render(" · "+formatLeft(until.Sub(now).Milliseconds())))
	case m.tried:
		lines = append(lines, "   "+styleErr.Render(shorten(err.Error(), m.width-4)))
	default:
		lines = append(lines, "   "+styleDim.Render("…"))
	}
	return lines
}

func (m model) viewList() []string {
	snoozed := 0
	if m.st != nil {
		for pid := range m.st.Panes {
			for _, p := range m.panes {
				if p.PaneID == pid && p.Agent != "" {
					snoozed++
				}
			}
		}
	}
	title := " " + styleTitle.Render("Snooze")
	if snoozed > 0 {
		s := "s"
		if snoozed == 1 {
			s = ""
		}
		title += styleDim.Render(fmt.Sprintf("  %d agent%s hidden from the Agents panel", snoozed, s))
	}
	lines := []string{title, ""}
	switch {
	case !m.loaded:
		return append(lines, styleDim.Render("  Loading…"))
	case len(m.rows) == 0:
		return append(lines, styleDim.Render("  No agents running."))
	}
	end := min(len(m.rows), m.offset+m.listHeight())
	now := timeNow().UnixMilli()
	for i := m.offset; i < end; i++ {
		lines = append(lines, m.rowLine(m.rows[i], i == m.cursor, now))
	}
	return lines
}

func (m model) rowLine(r row, selected bool, now int64) string {
	w := m.width - 2
	if r.kind == rowHeader && r.id == "" {
		return ""
	}
	if r.kind == rowHeader {
		style := styleTitle
		switch r.id {
		case "Snoozed":
			style = snoozedStyle().Bold(true)
		case "Just woke":
			style = styleOK.Bold(true)
		}
		return " " + style.Render(r.id) + styleDim.Render(" "+strconv.Itoa(r.count))
	}
	var left, title, right string
	switch r.kind {
	case rowSpace:
		left = " " + snoozedStyle().Render("◫") + " "
		s := "s"
		if r.count == 1 {
			s = ""
		}
		title = styleLead.Render("space "+r.title) + styleDim.Render(fmt.Sprintf("  %d agent%s", r.count, s))
	default:
		left = " " + agentMark(r.status, "") + " "
		if r.snoozed && r.via == viaWorkspace {
			left = "   " + left // under its space
		}
		title = styleLead.Render(r.title)
		if selected {
			title = styleTitle.Render(r.title)
		}
		if r.where != "" {
			title += styleDim.Render("  " + r.where)
		}
	}
	switch {
	case r.snoozed && r.via == viaWorkspace:
		right = styleDim.Render("with the space")
	case r.snoozed && r.when != "":
		right = snoozedStyle().Render(conditionLabel(r.when)) + styleDim.Render(" · or "+formatUntil(fromMS(r.until), fromMS(now)))
	case r.snoozed:
		right = snoozedStyle().Render(formatLeft(r.until-now)) + styleDim.Render(" · "+formatUntil(fromMS(r.until), fromMS(now)))
	case r.woke != nil:
		right = styleOK.Render("woke " + formatUntil(fromMS(r.woke.At), fromMS(now)))
		if r.woke.Reason != "" {
			right += styleDim.Render(" · " + r.woke.Reason)
		}
	case r.status == "blocked":
		right = styleBlocked.Render("needs you")
	case r.status != "" && r.status != "unknown":
		right = styleDim.Render(r.status)
	}
	line := fitRow(left, title, right, w)
	if selected {
		return " " + highlight(line, w)
	}
	return " " + line
}

// statusLine is the line above the keys: the last error, or what was done.
func (m model) statusLine() string {
	w := max(10, m.width-2)
	switch {
	case m.busy:
		return " " + styleDim.Render("…")
	case m.err != "":
		return " " + styleErr.Render(shorten(m.err, w))
	case m.flash != "":
		return " " + styleOK.Render(shorten(m.flash, w))
	case m.machineNote != "":
		return " " + styleErr.Render(shorten(m.machineNote, w))
	}
	return ""
}

func (m model) footer() []hint {
	switch m.screen {
	case screenCustom:
		return []hint{{"enter snooze", "enter", hintGo}, {"esc back", "esc", hintQuiet}}
	case screenPick:
		hs := []hint{{"enter snooze", "enter", hintGo}}
		if m.target.Snoozed {
			hs[0].label = "enter change"
			hs = append(hs, hint{"w wake", "w", hintAct})
		}
		hs = append(hs, hint{"c custom", "c", hintView})
		switch {
		case m.fromList || m.back != nil:
			return append(hs, hint{"esc back", "esc", hintQuiet})
		}
		return append(hs, hint{"tab list", "tab", hintView}, hint{"esc cancel", "esc", hintQuiet})
	}
	hs := []hint{{"enter go to", "enter", hintGo}}
	if r := m.selected(); r != nil {
		if r.snoozed {
			hs = append(hs, hint{"w wake", "w", hintAct})
		}
		hs = append(hs, hint{"s snooze", "s", hintAct})
		if r.kind == rowPane && r.space != "" && m.st.Workspaces[r.space] == nil {
			hs = append(hs, hint{"S snooze space", "S", hintAct})
		}
	}
	if r := m.selected(); r != nil && r.kind == rowPane {
		_, remote, _ := parseRemoteRef(r.id)
		if _, ok := m.links[recapPlugin]; ok && !remote {
			hs = append(hs, hint{"r recap", "r", hintView})
		}
	}
	if m.st != nil && len(m.st.Panes)+len(m.st.Workspaces) > 0 {
		hs = append(hs, hint{"W wake all", "W", hintRisk})
	}
	esc := "esc close"
	if m.back != nil {
		esc = "esc back"
	}
	return append(hs, hint{esc, "esc", hintQuiet})
}

func (m model) renderFooter() string {
	hot := ""
	if m.mouseY == m.height-1 {
		hot = hintAt(m.footer(), m.mouseX, m.width)
	}
	return footerLine(m.footer(), hot, m.width)
}

// ── drawing helpers ───────────────────────────────────────────────────────────

func shorten(s string, n int) string {
	if n <= 0 {
		return ""
	}
	if ansi.StringWidth(s) <= n {
		return s
	}
	return strings.TrimRight(ansi.Truncate(s, n-1, ""), " ") + "…"
}

// fitRow lays out a row w cells wide: left, then the title (cut to fit),
// then right at the far end. With too little room, right goes.
func fitRow(left, title, right string, w int) string {
	lw, rw := lipgloss.Width(left), lipgloss.Width(right)
	room := w - lw - rw - 2
	if room < 8 {
		right, rw = "", 0
		room = w - lw - 1
	}
	title = shorten(title, max(1, room))
	pad := max(1, w-lw-lipgloss.Width(title)-rw-1)
	return left + title + strings.Repeat(" ", pad) + right
}

// highlight gives a whole line the selection background, w cells wide. The
// line's own coloured pieces each end in a style reset, which would also end
// the background, so it's switched back on after every one.
func highlight(line string, w int) string {
	on := styleSelected.Render("x")
	on = on[:strings.Index(on, "x")] // the escape that turns the background on
	if on != "" {
		line = strings.ReplaceAll(line, "\x1b[0m", "\x1b[0m"+on)
	}
	return styleSelected.Width(w).Render(line)
}

// ── run ───────────────────────────────────────────────────────────────────────

func runPicker(demo bool) error {
	cfg := loadConfig()
	// Ask the terminal for its background now: once the program owns stdin,
	// the reply would arrive as stray input.
	dark := cfg.Theme == "dark"
	if cfg.Theme == "" {
		dark = lipgloss.HasDarkBackground()
	}
	darkTerminal = dark
	useTheme(pickerTheme(dark))

	// A SIGTERM (a newer open replacing this popup) ends it cleanly,
	// restoring the terminal and cancelling its SSH reads.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var w world = liveWorld{}
	if demo {
		w = newDemoWorld()
	} else {
		defer notePopup()()
		if cfg.Machines {
			remoteCtx, cancel := context.WithCancel(ctx)
			machines := newMachineWorld(w, sshTransport{ctx: remoteCtx})
			defer machines.stop(cancel)
			w = machines
		}
	}

	var back *popupRef
	if p, ok := cameFrom(); ok {
		back = &p
	}
	t := targetFromEnv()
	openOn := os.Getenv(envPane)
	list := os.Getenv(envScreen) == "list" || (t.ID == "" && openOn == "")
	m := newModel(cfg, w, t, list, openOn, back)
	final, err := tea.NewProgram(m, tea.WithAltScreen(), tea.WithMouseAllMotion(), tea.WithContext(ctx)).Run()
	if errors.Is(err, context.Canceled) || errors.Is(err, tea.ErrProgramKilled) {
		return nil
	}
	if err != nil {
		return err
	}
	if fm, ok := final.(model); ok && fm.outcome != "" && cfg.Notify && !demo {
		notify("Snooze", fm.outcome)
	}
	return finishSwap()
}

// snoozedStyle is what's snoozed, and when it wakes: blue, the colour of
// the 💤 on it, as herdr-recap draws its Snoozed tab.
func snoozedStyle() lipgloss.Style { return lipgloss.NewStyle().Foreground(look.Blue) }
