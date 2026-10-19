package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

const (
	hourMS = int64(3600 * 1000)
	radar  = "plugin:hhdebb.herdr-radar"
)

// noon is a Sunday.
var noon = time.Date(2026, 9, 20, 12, 0, 0, 0, time.Local)

// realSpawnTimer is the real sleeper launcher; every other test gets a
// stand-in, so no test ever leaves a sleeper that runs the test binary.
var realSpawnTimer = spawnTimer

func TestMain(m *testing.M) {
	if os.Getenv("SNOOZE_TEST_RPC_HELPER") == "1" {
		// An actual protocol process for the two-server integration test.
		// It must never leave a real sleeper behind.
		spawnTimer = func(int64) (int, error) { return 0, nil }
		if err := serveRemote(os.Stdin, os.Stdout); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	if sock := os.Getenv("SNOOZE_TEST_SESSION_ID"); sock != "" {
		// A helper process for TestTwoFirstRunsAgreeOnOneID.
		fmt.Print(sessionID(sock))
		os.Exit(0)
	}
	// Keep the machine running the tests (maybe inside herdr) out of it: no
	// real socket, state, config, sessions or sleepers.
	home, err := os.MkdirTemp("", "snz-home")
	if err != nil {
		panic(err)
	}
	for _, k := range []string{"HERDR_PLUGIN_ID", "SNOOZE_STATE_DIR", "HERDR_PLUGIN_STATE_DIR", "HERDR_PLUGIN_CONFIG_DIR",
		"HERDR_PLUGIN_CONTEXT_JSON", "HERDR_PLUGIN_EVENT_JSON", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_CONFIG_PATH"} {
		os.Unsetenv(k)
	}
	os.Setenv("XDG_STATE_HOME", filepath.Join(home, "state"))
	os.Setenv("XDG_CONFIG_HOME", filepath.Join(home, "config"))
	os.Setenv("HERDR_SOCKET_PATH", filepath.Join(home, "no-herdr", "herdr.sock"))
	os.Setenv("HERDR_BIN_PATH", "/bin/false")
	spawnTimer = func(int64) (int, error) { return 0, nil }
	code := m.Run()
	os.RemoveAll(home)
	os.Exit(code)
}

// ── panes and states ─────────────────────────────────────────────────────────

func liveToken(value string) map[string]any { return map[string]any{token: value, mark: "1"} }

// mkPane is a pane as herdr lists it: terminal_id always set, and kept when
// the pane moves.
func mkPane(pid string, tokens map[string]any) pane {
	if tokens == nil {
		tokens = map[string]any{}
	}
	return pane{PaneID: pid, TerminalID: "t-" + pid, WorkspaceID: "w1", Agent: "claude", Tokens: tokens}
}

func inSpace(p pane, ws string) pane  { p.WorkspaceID = ws; return p }
func withAgent(p pane, a string) pane { p.Agent = a; return p }

// moved is the same pane after a move: a new ID (and maybe space), the same
// terminal.
func moved(p pane, pid, ws string) pane {
	p.PaneID, p.WorkspaceID = pid, ws
	return p
}

func stateOf(panes map[string]*paneEntry, spaces map[string]*spaceEntry) *state {
	st := emptyState()
	for k, v := range panes {
		st.Panes[k] = v
	}
	for k, v := range spaces {
		st.Workspaces[k] = v
	}
	return st
}

func paneIDs(st *state) []string { return sortedKeys(st.Panes) }

// norm is v as JSON would carry it, for comparing what herdr was sent.
func norm(v any) any {
	data, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	var out any
	_ = json.Unmarshal(data, &out)
	return out
}

func sameJSON(t *testing.T, got, want any, what string) {
	t.Helper()
	if !reflect.DeepEqual(norm(got), norm(want)) {
		g, _ := json.Marshal(got)
		w, _ := json.Marshal(want)
		t.Fatalf("%s:\n got %s\nwant %s", what, g, w)
	}
}

// ── a fake herdr ─────────────────────────────────────────────────────────────

// fakeHerdr is just enough of herdr's socket API: panes and their tokens,
// one agent view, and a record of every call.
type fakeHerdr struct {
	mu         sync.Mutex
	panes      []map[string]any
	view       map[string]any
	viewParams map[string]any
	calls      []string
	params     []map[string]any
	// fail, when set, makes a call fail: a herdr error with code, or with
	// drop the connection closes unanswered (herdr went away).
	fail func(method string, params map[string]any) (code string, drop bool)
	// onCall runs before each call is answered, under the lock.
	onCall func(method string)
	ln     net.Listener
	sock   string
}

func fpane(pid string, extra map[string]any) map[string]any {
	p := map[string]any{"pane_id": pid, "terminal_id": "t-" + pid, "workspace_id": "w1", "agent": "claude",
		"tokens": map[string]any{}}
	for k, v := range extra {
		p[k] = v
	}
	return p
}

func startFake(t *testing.T, sock string, panes []map[string]any, view map[string]any) *fakeHerdr {
	t.Helper()
	if view == nil {
		view = map[string]any{"active": false}
	}
	f := &fakeHerdr{panes: panes, view: view, sock: sock}
	f.listen(t)
	return f
}

func (f *fakeHerdr) listen(t *testing.T) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(f.sock), 0o700); err != nil {
		t.Fatal(err)
	}
	ln, err := net.Listen("unix", f.sock)
	if err != nil {
		t.Fatal(err)
	}
	f.ln = ln
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go f.serve(conn)
		}
	}()
}

func (f *fakeHerdr) stop() { f.ln.Close() }

func (f *fakeHerdr) serve(conn net.Conn) {
	defer conn.Close()
	line, err := bufio.NewReader(conn).ReadBytes('\n')
	if err != nil {
		return
	}
	var req struct {
		ID     string         `json:"id"`
		Method string         `json:"method"`
		Params map[string]any `json:"params"`
	}
	if json.Unmarshal(line, &req) != nil {
		return
	}
	f.mu.Lock()
	f.calls = append(f.calls, req.Method)
	f.params = append(f.params, req.Params)
	if f.onCall != nil {
		f.onCall(req.Method)
	}
	var result any
	var code string
	drop := false
	if f.fail != nil {
		code, drop = f.fail(req.Method, req.Params)
	}
	if code == "" && !drop {
		result, code = f.answer(req.Method, req.Params)
	}
	f.mu.Unlock()
	if drop {
		return
	}
	reply := map[string]any{"id": req.ID}
	if code != "" {
		reply["error"] = map[string]any{"code": code, "message": "refused"}
	} else {
		reply["result"] = result
	}
	data, _ := json.Marshal(reply)
	conn.Write(append(data, '\n'))
}

func (f *fakeHerdr) answer(method string, params map[string]any) (any, string) {
	switch method {
	case "pane.list":
		return map[string]any{"panes": norm(f.panes)}, ""
	case "workspace.list":
		return map[string]any{"workspaces": []any{
			map[string]any{"workspace_id": "w1", "label": "One"},
			map[string]any{"workspace_id": "w2", "label": "Two"},
		}}, ""
	case "pane.report_metadata":
		for _, p := range f.panes {
			if p["pane_id"] != params["pane_id"] {
				continue
			}
			tokens := p["tokens"].(map[string]any)
			for name, value := range params["tokens"].(map[string]any) {
				if value == nil {
					delete(tokens, name)
				} else {
					tokens[name] = value
				}
			}
			return map[string]any{}, ""
		}
		return nil, "pane_not_found"
	case "agent.view.set":
		f.view = map[string]any{"active": true, "source": params["source"], "label": params["label"]}
		f.viewParams = params
		return f.view, ""
	case "agent.view.clear":
		if f.view["source"] == params["source"] {
			f.view, f.viewParams = map[string]any{"active": false}, nil
		}
		return f.view, ""
	}
	return map[string]any{}, ""
}

// edit changes what the fake holds, under its lock.
func (f *fakeHerdr) edit(fn func()) {
	f.mu.Lock()
	defer f.mu.Unlock()
	fn()
}

func (f *fakeHerdr) pane(pid string) map[string]any {
	for _, p := range f.panes {
		if p["pane_id"] == pid {
			return p
		}
	}
	return nil
}

func (f *fakeHerdr) tokens(pid string) map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := map[string]any{}
	for k, v := range f.pane(pid)["tokens"].(map[string]any) {
		out[k] = v
	}
	return out
}

func (f *fakeHerdr) focus(pid string) {
	f.edit(func() {
		for _, p := range f.panes {
			p["focused"] = p["pane_id"] == pid
		}
	})
}

func (f *fakeHerdr) currentView() map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.view
}

func (f *fakeHerdr) lastViewParams() map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.viewParams
}

func (f *fakeHerdr) callList() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *fakeHerdr) clearCalls() { f.edit(func() { f.calls, f.params = nil, nil }) }

func (f *fakeHerdr) count(method string) int {
	n := 0
	for _, c := range f.callList() {
		if c == method {
			n++
		}
	}
	return n
}

// notifications are the toasts shown, as "title: body".
func (f *fakeHerdr) notifications() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for i, c := range f.calls {
		if c == "notification.show" {
			body, _ := f.params[i]["body"].(string)
			out = append(out, fmt.Sprintf("%v: %s", f.params[i]["title"], body))
		}
	}
	return out
}

func (f *fakeHerdr) paramsOf(method string) []map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []map[string]any
	for i, c := range f.calls {
		if c == method {
			out = append(out, f.params[i])
		}
	}
	return out
}

// ── sleepers ─────────────────────────────────────────────────────────────────

// sleeper is a real process that looks exactly like snooze's sleeper (the
// same command line), but runs /bin/true when its long sleep ends.
type sleeper struct {
	pid  int
	done chan struct{}
}

func startSleeper(t *testing.T) *sleeper {
	t.Helper()
	cmd := exec.Command(timerShell, "-c", timerScript, timerName, "600", "/bin/true")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	s := &sleeper{pid: cmd.Process.Pid, done: make(chan struct{})}
	go func() { cmd.Wait(); close(s.done) }()
	t.Cleanup(func() {
		syscall.Kill(-s.pid, syscall.SIGKILL)
		<-s.done
	})
	return s
}

func (s *sleeper) gone(within time.Duration) bool {
	select {
	case <-s.done:
		return true
	case <-time.After(within):
		return false
	}
}

func (s *sleeper) kill() {
	syscall.Kill(-s.pid, syscall.SIGKILL)
	<-s.done
}

// ── an environment as herdr runs the plugin in ───────────────────────────────

// env is one herdr session ("herdr-a") with a fake herdr, the plugin's own
// state directory, a config with the badge "z", and `herdr session list`
// answering with that one session.
type env struct {
	dir      string
	sock     string
	herdr    *fakeHerdr
	sessions string // the file `herdr session list --json` prints
	listed   string // a file with a line per `herdr session list`
	failList string // a file that, while it exists, makes it fail
}

func newEnv(t *testing.T, panes []map[string]any, view map[string]any) *env {
	t.Helper()
	dir, err := os.MkdirTemp("", "snz")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	e := &env{dir: dir, sock: filepath.Join(dir, "herdr-a", "herdr.sock")}
	e.herdr = startFake(t, e.sock, panes, view)
	cfg := filepath.Join(dir, "cfg")
	os.MkdirAll(cfg, 0o700)
	os.WriteFile(filepath.Join(cfg, "config.json"), []byte(`{"durations": ["1h"], "badge": "z"}`), 0o600)
	e.sessions = filepath.Join(dir, "sessions.json")
	e.listed = filepath.Join(dir, "listed")
	e.failList = filepath.Join(dir, "fail-list")
	e.setSessions(e.sock)
	script := filepath.Join(dir, "herdr")
	os.WriteFile(script, []byte(fmt.Sprintf("#!/bin/sh\necho x >> %q\n[ -e %q ] && exit 1\ncat %q\n", e.listed, e.failList, e.sessions)), 0o700)
	for k, v := range map[string]string{
		"SNOOZE_STATE_DIR": filepath.Join(dir, "state"), "HERDR_SOCKET_PATH": e.sock,
		"XDG_STATE_HOME": filepath.Join(dir, "xdg-state"), "XDG_CONFIG_HOME": filepath.Join(dir, "xdg-config"),
		"HERDR_PLUGIN_CONFIG_DIR": cfg, "HERDR_PLUGIN_CONTEXT_JSON": "", "HERDR_PLUGIN_EVENT_JSON": "",
		"HERDR_BIN_PATH": script,
	} {
		t.Setenv(k, v)
	}
	os.MkdirAll(stateDir(), 0o700)
	return e
}

func (e *env) setSessions(socks ...string) {
	var list []string
	for _, s := range socks {
		list = append(list, fmt.Sprintf(`{"running": true, "socket_path": %q}`, s))
	}
	os.WriteFile(e.sessions, []byte(`{"sessions": [`+strings.Join(list, ", ")+`]}`), 0o600)
}

func (e *env) timesListed() int {
	data, _ := os.ReadFile(e.listed)
	return strings.Count(string(data), "\n")
}

func (e *env) file() string { return stateFile() }

func (e *env) write(t *testing.T, text string) {
	t.Helper()
	if err := os.WriteFile(e.file(), []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
}

// snoozed writes a state file with "zz" snoozed until `until`.
func (e *env) snoozed(t *testing.T, until int64) {
	e.write(t, fmt.Sprintf(`{"panes": {"zz": {"until": %d, "via": "pane"}}}`, until))
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func mustRun(t *testing.T, args ...string) {
	t.Helper()
	if code, err := run(args); code != 0 || err != nil {
		t.Fatalf("%v: code %d, %v", args, code, err)
	}
}

// stubSpawn swaps the sleeper launcher for the test.
func stubSpawn(t *testing.T, f func(int64) (int, error)) {
	t.Helper()
	old := spawnTimer
	spawnTimer = f
	t.Cleanup(func() { spawnTimer = old })
}

// onlyJustWoke: every snooze is over, and what's left is the "Just woke"
// list, which keeps the file until it's gone too.
func onlyJustWoke(st *state) bool {
	return len(st.Woke) > 0 && len(st.Panes) == 0 && len(st.Workspaces) == 0 && st.View.empty()
}
