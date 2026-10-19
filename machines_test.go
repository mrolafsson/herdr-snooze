package main

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
)

type machineTestWorld struct {
	data    loadedMsg
	snoozes []target
	wakes   int
	focuses int
	opens   int
}

func (w *machineTestWorld) load() loadedMsg {
	st := emptyState()
	for k, v := range w.data.st.Panes {
		copy := *v
		st.Panes[k] = &copy
	}
	for k, v := range w.data.st.Workspaces {
		copy := *v
		st.Workspaces[k] = &copy
	}
	for k, v := range w.data.st.Woke {
		copy := *v
		st.Woke[k] = &copy
	}
	return loadedMsg{st: st, panes: append([]pane(nil), w.data.panes...), spaces: append([]workspaceInfo(nil), w.data.spaces...)}
}
func (w *machineTestWorld) snooze(t target, _ time.Time, _ string, _ config) error {
	w.snoozes = append(w.snoozes, t)
	return nil
}
func (w *machineTestWorld) wake(config, []string, []string, bool) error { w.wakes++; return nil }
func (w *machineTestWorld) focus(string) error                          { w.focuses++; return nil }
func (w *machineTestWorld) open(popupRef) error                         { w.opens++; return nil }
func (*machineTestWorld) siblings() map[string]popupRef                 { return nil }

type machineCall struct {
	profile machineProfile
	req     remoteRequest
}
type machineTestTransport struct {
	mu           sync.Mutex
	saved        []machineProfile
	snapshots    map[string]*remoteSnapshot
	unavailable  map[string]bool
	calls        []machineCall
	connects     []string
	gate         <-chan struct{}
	started      chan string
	active, peak int
}

func (f *machineTestTransport) profiles() ([]machineProfile, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]machineProfile(nil), f.saved...), nil
}
func (f *machineTestTransport) connect(p machineProfile) (remoteInfo, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.connects = append(f.connects, p.ID)
	if f.unavailable[p.ID] {
		return remoteInfo{}, errors.New("offline")
	}
	return testRemoteInfo(), nil
}
func (f *machineTestTransport) call(p machineProfile, _ remoteInfo, req remoteRequest) (remoteReply, error) {
	f.mu.Lock()
	f.calls = append(f.calls, machineCall{p, req})
	if req.Method != "snapshot" {
		f.mu.Unlock()
		return remoteReply{}, nil
	}
	f.active++
	f.peak = max(f.peak, f.active)
	gate, started := f.gate, f.started
	f.mu.Unlock()
	if started != nil {
		started <- p.ID
	}
	if gate != nil {
		<-gate
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.active--
	if f.unavailable[p.ID] {
		return remoteReply{}, errors.New("offline")
	}
	return remoteReply{Snapshot: f.snapshots[p.ID]}, nil
}
func (f *machineTestTransport) mutations() []machineCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	var calls []machineCall
	for _, c := range f.calls {
		if c.req.Method != "snapshot" {
			calls = append(calls, c)
		}
	}
	return calls
}
func testRemoteInfo() remoteInfo {
	return remoteInfo{remoteProtocol, "/srv/snooze/bin/herdr-snooze", "/srv/herdr/herdr.sock", "/srv/config", "/srv/state", "/usr/bin/herdr"}
}
func testMachine(id string) machineProfile {
	return machineProfile{ID: id, Label: "Machine " + id, Target: "host-" + id, Session: "default", Enabled: true}
}
func testSnapshot(id string) *remoteSnapshot {
	p := mkPane("w1:p1", nil)
	p.TerminalID = "terminal-" + id
	p.Agent = "pi"
	return &remoteSnapshot{Session: "session-" + id, State: emptyState(), Panes: []pane{p}, Spaces: []workspaceInfo{{WorkspaceID: "w1", Label: "Project"}}}
}
func testMachineWorld() (*machineWorld, *machineTestWorld, *machineTestTransport) {
	local := &machineTestWorld{data: loadedMsg{st: emptyState(), panes: []pane{mkPane("w1:p1", nil)}, spaces: []workspaceInfo{{WorkspaceID: "w1", Label: "Local"}}}}
	remote := &machineTestTransport{saved: []machineProfile{testMachine("a"), testMachine("b")}, snapshots: map[string]*remoteSnapshot{"a": testSnapshot("a"), "b": testSnapshot("b")}, unavailable: map[string]bool{}}
	return newMachineWorld(local, remote), local, remote
}
func refreshMachineTest(w *machineWorld) { w.running.Add(1); w.refresh() }
func machinePane(t *testing.T, data loadedMsg, machine string) pane {
	t.Helper()
	for _, p := range data.panes {
		ref, remote, _ := parseRemoteRef(p.PaneID)
		if remote && ref.Machine == machine {
			return p
		}
	}
	t.Fatal("missing machine", machine)
	return pane{}
}

func TestMachineSnapshotsNeverCollideOrModifyLocalState(t *testing.T) {
	w, local, transport := testMachineWorld()
	remote := transport.snapshots["a"]
	remote.State.Panes["w1:p1"] = &paneEntry{Until: nowMS() + hourMS, WorkspaceID: "w1", Via: viaWorkspace}
	remote.State.Workspaces["w1"] = &spaceEntry{Until: nowMS() + hourMS}
	transport.snapshots["b"].State.Woke["w1:p1"] = &wokeEntry{At: nowMS()}
	refreshMachineTest(w)
	data := w.load()
	if len(data.panes) != 3 {
		t.Fatal(data.panes)
	}
	a, b := machinePane(t, data, "a"), machinePane(t, data, "b")
	if a.PaneID == b.PaneID || a.PaneID == "w1:p1" || a.WorkspaceID == b.WorkspaceID {
		t.Fatal("IDs collided", a, b)
	}
	if data.st.Panes[a.PaneID] == nil || data.st.Workspaces[a.WorkspaceID] == nil || data.st.Woke[b.PaneID] == nil {
		t.Fatal("state not namespaced", data.st)
	}
	if len(local.data.st.Panes) != 0 || len(local.data.panes) != 1 {
		t.Fatal("remote state contaminated local world")
	}
	if remote.State.Panes["w1:p1"].WorkspaceID != "w1" || remote.Panes[0].PaneID != "w1:p1" {
		t.Fatal("source snapshot was modified")
	}
	labels := map[string]string{}
	for _, s := range data.spaces {
		labels[s.WorkspaceID] = s.Label
	}
	rows := buildRows(data.st, data.panes, labels)
	found := false
	for _, r := range rows {
		if r.id == a.PaneID {
			found = strings.Contains(r.where, "Machine a")
		}
	}
	if !found {
		t.Fatal("machine label missing", rows)
	}
	if len(w.load().panes) != 3 {
		t.Fatal("repeated loads duplicated remote rows")
	}
}

func TestRemoteSnoozeAndWakeReturnToOwningMachine(t *testing.T) {
	w, local, transport := testMachineWorld()
	refreshMachineTest(w)
	p := machinePane(t, w.load(), "b")
	if err := w.snooze(paneTarget(p, "Project"), noon.Add(time.Hour), whenDone, defaultConfig()); err != nil {
		t.Fatal(err)
	}
	if err := w.wake(defaultConfig(), []string{p.PaneID}, nil, false); err != nil {
		t.Fatal(err)
	}
	calls := transport.mutations()
	if len(calls) != 2 || len(local.snoozes) != 0 || local.wakes != 0 {
		t.Fatal(calls, local)
	}
	for _, c := range calls {
		if c.profile.ID != "b" || c.req.Session != "session-b" || c.req.Refs[0].ID != "w1:p1" || c.req.Refs[0].Terminal != "terminal-b" {
			t.Fatal(c)
		}
	}
	if calls[0].req.Target.ID != "w1:p1" || calls[0].req.When != whenDone {
		t.Fatal(calls[0])
	}
	if err := w.snooze(target{Kind: "pane", ID: "w1:p1"}, noon, "", defaultConfig()); err != nil {
		t.Fatal(err)
	}
	if len(local.snoozes) != 1 {
		t.Fatal("local route lost")
	}
	w.stop(func() {})
}

func TestRemoteSpaceWakeAndWakeAllHaveExplicitScopes(t *testing.T) {
	w, local, transport := testMachineWorld()
	refreshMachineTest(w)
	p := machinePane(t, w.load(), "a")
	if err := w.wake(defaultConfig(), nil, []string{p.WorkspaceID}, false); err != nil {
		t.Fatal(err)
	}
	calls := transport.mutations()
	if len(calls) != 1 || calls[0].req.Refs[0].ID != "w1" || calls[0].req.Refs[0].Terminal != "" || local.wakes != 0 {
		t.Fatal(calls)
	}
	if err := w.wake(defaultConfig(), nil, nil, true); err != nil {
		t.Fatal(err)
	}
	calls = transport.mutations()
	if len(calls) != 3 || local.wakes != 1 {
		t.Fatal(calls, local.wakes)
	}
	for _, c := range calls[1:] {
		if !c.req.All || len(c.req.Refs) != 0 {
			t.Fatal(c)
		}
	}
	w.stop(func() {})
}

func TestChangedDisabledOrRemovedMachineCannotReceiveWrites(t *testing.T) {
	for _, change := range []string{"disabled", "removed", "retargeted", "session"} {
		t.Run(change, func(t *testing.T) {
			w, local, transport := testMachineWorld()
			refreshMachineTest(w)
			p := machinePane(t, w.load(), "a")
			switch change {
			case "disabled":
				transport.saved[0].Enabled = false
			case "removed":
				transport.saved = transport.saved[1:]
			case "retargeted":
				transport.saved[0].Target = "other-host"
			case "session":
				transport.saved[0].Session = "other-session"
			}
			if err := w.snooze(paneTarget(p, ""), noon, "", defaultConfig()); err == nil {
				t.Fatal("changed machine accepted")
			}
			if len(transport.mutations()) != 0 || len(local.snoozes) != 0 {
				t.Fatal("write escaped validation")
			}
		})
	}
}

func TestOldServerSnapshotCannotReceiveWrites(t *testing.T) {
	w, _, transport := testMachineWorld()
	refreshMachineTest(w)
	p := machinePane(t, w.load(), "a")
	transport.snapshots["a"] = testSnapshot("a")
	transport.snapshots["a"].Session = "new-server"
	refreshMachineTest(w)
	if err := w.snooze(paneTarget(p, ""), noon, "", defaultConfig()); err == nil {
		t.Fatal("stale session accepted")
	}
	if len(transport.mutations()) != 0 {
		t.Fatal("stale write sent")
	}
}

func TestOfflineAndDisabledMachinesLeaveLocalAgentsUsable(t *testing.T) {
	w, local, transport := testMachineWorld()
	transport.unavailable["a"] = true
	transport.saved[1].Enabled = false
	refreshMachineTest(w)
	data := w.load()
	if len(data.panes) != 1 || len(data.warnings) != 1 || !strings.Contains(data.warnings[0], "Machine a") {
		t.Fatal(data)
	}
	if len(transport.connects) != 1 || transport.connects[0] != "a" {
		t.Fatal("disabled host contacted", transport.connects)
	}
	if err := w.snooze(target{Kind: "pane", ID: "w1:p1"}, noon, "", defaultConfig()); err != nil || len(local.snoozes) != 1 {
		t.Fatal(err)
	}
}

func TestRemoteRefreshDoesNotBlockLocalLoadAndBoundsConcurrency(t *testing.T) {
	w, _, transport := testMachineWorld()
	gate := make(chan struct{})
	transport.gate = gate
	transport.started = make(chan string, 12)
	transport.saved = nil
	for i := range 10 {
		id := fmt.Sprint(i)
		transport.saved = append(transport.saved, testMachine(id))
		transport.snapshots[id] = testSnapshot(id)
	}
	loaded := make(chan loadedMsg, 1)
	go func() { loaded <- w.load() }()
	select {
	case data := <-loaded:
		if len(data.panes) != 1 {
			t.Fatal(data)
		}
	case <-time.After(time.Second):
		t.Fatal("remote host blocked local load")
	}
	for range machineConcurrency {
		select {
		case <-transport.started:
		case <-time.After(time.Second):
			t.Fatal("remote refresh not started")
		}
	}
	close(gate)
	w.stop(func() {})
	transport.mu.Lock()
	defer transport.mu.Unlock()
	if transport.peak > machineConcurrency || transport.peak < 2 {
		t.Fatal("unexpected concurrency", transport.peak)
	}
}

func TestRemoteNavigationNeverTargetsLocalIDOrPlugin(t *testing.T) {
	w, local, _ := testMachineWorld()
	refreshMachineTest(w)
	data := w.load()
	p := machinePane(t, data, "a")
	if err := w.focus(p.PaneID); err == nil || local.focuses != 0 {
		t.Fatal(err, local.focuses)
	}
	m := newModel(defaultConfig(), w, target{}, true, "", nil)
	updated, _ := m.Update(data)
	m = updated.(model)
	for i, r := range m.rows {
		if r.id == p.PaneID {
			m.cursor = i
		}
	}
	m.links = map[string]popupRef{recapPlugin: {PluginID: recapPlugin, Entrypoint: "picker"}}
	updated, _ = m.listKey(keyMsg("r"))
	m = updated.(model)
	if local.opens != 0 || !strings.Contains(m.err, "owning machine") {
		t.Fatal("remote ID handed to local Recap", m.err)
	}
}

func TestMalformedRemoteIDsNeverFallBackToLocal(t *testing.T) {
	w, local, transport := testMachineWorld()
	for _, id := range []string{"remote:", "remote:not-base64", (remoteRef{Machine: "a"}).key()} {
		if w.snooze(target{Kind: "pane", ID: id}, noon, "", defaultConfig()) == nil {
			t.Fatal(id)
		}
		if w.wake(defaultConfig(), []string{id}, nil, false) == nil {
			t.Fatal(id)
		}
	}
	if len(local.snoozes) != 0 || local.wakes != 0 || len(transport.mutations()) != 0 {
		t.Fatal("malformed reference reached a writer")
	}
}

func TestRemoteRefRoundTripPreservesOpaqueIDs(t *testing.T) {
	want := remoteRef{Machine: "id:/", Session: "session:/", ID: "w1:p1:/", Terminal: "term:/", Agent: "pi"}
	got, remote, err := parseRemoteRef(want.key())
	if err != nil || !remote || got != want {
		t.Fatal(got, err)
	}
}
