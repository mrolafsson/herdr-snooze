package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func remoteRig(t *testing.T) (*env, remoteRef) {
	t.Helper()
	e := newEnv(t, []map[string]any{fpane("w1:p1", map[string]any{"agent": "pi", "agent_status": "working"})}, nil)
	return e, remoteRef{Machine: "machine", Session: sessionKey(sessionIdentity("")), ID: "w1:p1", Terminal: "t-w1:p1", Agent: "pi"}
}

func TestRemoteRPCSnapshotSnoozeAndWakeUseOwningStateAndView(t *testing.T) {
	e, ref := remoteRig(t)
	read := handleRemote(remoteRequest{Method: "snapshot"})
	if read.Error != "" || read.Snapshot == nil || read.Snapshot.Session != ref.Session || len(read.Snapshot.Panes) != 1 {
		t.Fatal(read)
	}
	if e.herdr.count("pane.report_metadata") != 0 {
		t.Fatal("snapshot changed metadata")
	}
	req := remoteRequest{Method: "snooze", Session: ref.Session, Target: target{Kind: "pane", ID: ref.ID, Agent: &agentID{Agent: "pi"}}, Refs: []remoteRef{ref}, Until: nowMS() + hourMS, When: whenDone}
	if reply := handleRemote(req); reply.Error != "" {
		t.Fatal(reply.Error)
	}
	st := readState()
	if st.Panes[ref.ID] == nil || st.Panes[ref.ID].When != whenDone || e.herdr.tokens(ref.ID)[mark] != "1" {
		t.Fatal(st)
	}
	if reply := handleRemote(remoteRequest{Method: "wake", Session: ref.Session, Refs: []remoteRef{ref}}); reply.Error != "" {
		t.Fatal(reply.Error)
	}
	if readState().Panes[ref.ID] != nil || e.herdr.tokens(ref.ID)[mark] != nil {
		t.Fatal("wake did not clear owning state")
	}
	for _, method := range e.herdr.callList() {
		if strings.Contains(method, "prompt") || strings.Contains(method, "send") || strings.Contains(method, "start") {
			t.Fatal("RPC controlled an agent", method)
		}
	}
}

func TestRemoteRPCRejectsStaleSessionTerminalAndAgentBeforeMutation(t *testing.T) {
	for _, bad := range []string{"session", "terminal", "agent", "missing-ref", "kind"} {
		t.Run(bad, func(t *testing.T) {
			e, ref := remoteRig(t)
			req := remoteRequest{Method: "snooze", Session: ref.Session, Target: target{Kind: "pane", ID: ref.ID}, Refs: []remoteRef{ref}, Until: nowMS() + hourMS}
			switch bad {
			case "session":
				req.Session = "old-server"
			case "terminal":
				req.Refs[0].Terminal = "replaced-terminal"
			case "agent":
				req.Refs[0].Agent = "codex"
			case "missing-ref":
				req.Refs = nil
			case "kind":
				req.Target.Kind = "workspace"
			}
			if reply := handleRemote(req); reply.Error == "" {
				t.Fatal("stale/invalid request accepted", bad)
			}
			if e.herdr.count("pane.report_metadata") != 0 || e.herdr.count("agent.view.set") != 0 || len(readState().Panes) != 0 {
				t.Fatal("invalid request wrote state")
			}
		})
	}
}

func TestRemoteRPCRejectsStaleWakeAndUnknownCommands(t *testing.T) {
	e, ref := remoteRig(t)
	ref.Terminal = "replaced"
	for _, req := range []remoteRequest{
		{Method: "wake", Session: ref.Session, Refs: []remoteRef{ref}},
		{Method: "wake", Session: ref.Session},
		{Method: "wake", Session: ref.Session, All: true, Refs: []remoteRef{ref}},
		{Method: "agent.prompt", Session: ref.Session},
	} {
		if reply := handleRemote(req); reply.Error == "" {
			t.Fatal(req)
		}
	}
	if e.herdr.count("pane.report_metadata") != 0 {
		t.Fatal("invalid request changed a pane")
	}
}

func TestRemoteRPCWorkspaceTargetsAreValidated(t *testing.T) {
	e, ref := remoteRig(t)
	ref.ID, ref.Terminal, ref.Agent = "w1", "", ""
	req := remoteRequest{Method: "snooze", Session: ref.Session, Target: target{Kind: "workspace", ID: ref.ID}, Refs: []remoteRef{ref}, Until: nowMS() + hourMS}
	if reply := handleRemote(req); reply.Error != "" {
		t.Fatal(reply.Error)
	}
	if readState().Workspaces["w1"] == nil {
		t.Fatal("space snooze missing")
	}
	if reply := handleRemote(remoteRequest{Method: "wake", Session: ref.Session, Refs: []remoteRef{ref}}); reply.Error != "" {
		t.Fatal(reply.Error)
	}
	if readState().Workspaces["w1"] != nil {
		t.Fatal("space did not wake")
	}
	ref.ID = "missing"
	if reply := handleRemote(remoteRequest{Method: "wake", Session: ref.Session, Refs: []remoteRef{ref}}); reply.Error == "" {
		t.Fatal("missing workspace accepted")
	}
	_ = e
}

func TestRemoteServerUsesReportedPathsNotInheritedClientContext(t *testing.T) {
	e, _ := remoteRig(t)
	info, err := connectionInfo()
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"HERDR_PLUGIN_STATE_DIR", "HERDR_PLUGIN_ID", "SNOOZE_STATE_DIR", "HERDR_PLUGIN_EVENT_JSON", "HERDR_PLUGIN_CONTEXT_JSON", "HERDR_SOCKET_PATH", "HERDR_PLUGIN_CONFIG_DIR", "HERDR_BIN_PATH"} {
		t.Setenv(key, os.Getenv(key))
	}
	t.Setenv("HERDR_SOCKET_PATH", filepath.Join(e.dir, "wrong.sock"))
	t.Setenv("HERDR_PLUGIN_CONTEXT_JSON", `{"focused_pane_id":"another-agent"}`)
	req, _ := json.Marshal(remoteRequest{Endpoint: info, Method: "snapshot"})
	var out bytes.Buffer
	if err := serveRemote(bytes.NewReader(req), &out); err != nil {
		t.Fatal(err)
	}
	var reply remoteReply
	if err := json.Unmarshal(out.Bytes(), &reply); err != nil || reply.Error != "" || len(reply.Snapshot.Panes) != 1 {
		t.Fatal(out.String(), err)
	}
	if os.Getenv("HERDR_PLUGIN_CONTEXT_JSON") != "" || socketPath() != info.Socket || pluginStateDir() != info.State {
		t.Fatal("wrong server environment")
	}
}

func TestRemoteProtocolAndPathsFailClosed(t *testing.T) {
	for _, change := range []string{"protocol", "relative-binary", "newline-socket", "empty-state"} {
		info := testRemoteInfo()
		switch change {
		case "protocol":
			info.Protocol++
		case "relative-binary":
			info.Binary = "snooze"
		case "newline-socket":
			info.Socket = "/tmp/a\nscript"
		case "empty-state":
			info.State = ""
		}
		if info.validate() == nil {
			t.Fatal(change)
		}
		data, _ := json.Marshal(remoteRequest{Endpoint: info, Method: "snapshot"})
		if serveRemote(bytes.NewReader(data), &bytes.Buffer{}) == nil {
			t.Fatal(change, "reached server")
		}
	}
	if serveRemote(strings.NewReader("not JSON"), &bytes.Buffer{}) == nil {
		t.Fatal("bad JSON accepted")
	}
}

func TestRemoteHandshakeCorrelatesTheExactActionLog(t *testing.T) {
	dir := t.TempDir()
	script := filepath.Join(dir, "herdr")
	info := testRemoteInfo()
	raw, _ := json.Marshal(info)
	logs, _ := json.Marshal(map[string]any{"result": map[string]any{"logs": []any{
		map[string]any{"log_id": "unrelated", "status": "succeeded", "stdout": "not connection info"},
		map[string]any{"log_id": "wanted", "status": "succeeded", "stdout": string(raw)},
	}}})
	body := "#!/bin/sh\nif [ \"$4\" = action ]; then printf '%s\\n' '{\"result\":{\"log\":{\"log_id\":\"wanted\"}}}'; else printf '%s\\n' " + shellQuote(string(logs)) + "; fi\n"
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HERDR_BIN_PATH", script)
	got, err := (sshTransport{}).connect(testMachine("test"))
	if err != nil || got != info {
		t.Fatal(got, err)
	}
}

func TestSSHTransportQuotesOnlyExecutableAndSendsRequestOnStdin(t *testing.T) {
	dir := t.TempDir()
	payload := filepath.Join(dir, "request.json")
	args := filepath.Join(dir, "args")
	binary := filepath.Join(dir, "snooze' ; touch PWNED ; '")
	body := "#!/bin/sh\ncat > " + shellQuote(payload) + "\nprintf '%s\\n' '{}'\n"
	if err := os.WriteFile(binary, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	ssh := filepath.Join(dir, "ssh")
	body = "#!/bin/sh\nprintf '%s\\n' \"$@\" > " + shellQuote(args) + "\nfor command do :; done\ncd " + shellQuote(dir) + "\nexec /bin/sh -c \"$command\"\n"
	if err := os.WriteFile(ssh, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	info := testRemoteInfo()
	info.Binary = binary
	_, err := (sshTransport{}).call(testMachine("one"), info, remoteRequest{Method: "wake", Session: "session", All: true})
	if err != nil {
		t.Fatal(err)
	}
	if exists(filepath.Join(dir, "PWNED")) {
		t.Fatal("remote path became shell syntax")
	}
	data, err := os.ReadFile(payload)
	if err != nil {
		t.Fatal(err)
	}
	var req remoteRequest
	if json.Unmarshal(data, &req) != nil || req.Method != "wake" || req.Endpoint != info {
		t.Fatal(string(data))
	}
	argv, _ := os.ReadFile(args)
	for _, flag := range []string{"BatchMode=yes", "ConnectTimeout=5", "ServerAliveInterval=5", "--\nhost-one\n"} {
		if !strings.Contains(string(argv), flag) {
			t.Fatal("missing safe SSH option", flag, string(argv))
		}
	}
}

func TestAmbiguousSSHFailureNeverReplaysMutation(t *testing.T) {
	dir := t.TempDir()
	count := filepath.Join(dir, "attempts")
	script := filepath.Join(dir, "ssh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\necho x >> "+shellQuote(count)+"\nexit 255\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	_, err := (sshTransport{}).call(testMachine("one"), testRemoteInfo(), remoteRequest{Method: "wake", All: true})
	if err == nil || !strings.Contains(err.Error(), "not retried") {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(count)
	if string(data) != "x\n" {
		t.Fatal("operation was replayed", string(data))
	}
}

func TestRemoteCommandCancellationAndOutputBounds(t *testing.T) {
	start := time.Now()
	_, err := remoteCommand(context.Background(), 30*time.Millisecond, nil, "/bin/sh", "-c", "sleep 20 & wait")
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 2*time.Second {
		t.Fatal(err, time.Since(start))
	}
	var b cappedOutput
	if _, err := b.Write(make([]byte, remoteOutputLimit+1)); err == nil || b.Len() != 0 {
		t.Fatal("output limit ignored")
	}
}

func TestMachinesCanBeDisabledInConfig(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HERDR_PLUGIN_CONFIG_DIR", dir)
	if err := os.WriteFile(filepath.Join(dir, "config.json"), []byte(`{"machines":false}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if loadConfig().Machines {
		t.Fatal("machines=false ignored")
	}
	if !defaultConfig().Machines {
		t.Fatal("saved machines are not included by default")
	}
}

type rpcIntegrationTransport struct {
	sshTransport
	profile machineProfile
	info    remoteInfo
}

func (t rpcIntegrationTransport) profiles() ([]machineProfile, error) {
	return []machineProfile{t.profile}, nil
}
func (t rpcIntegrationTransport) connect(machineProfile) (remoteInfo, error) { return t.info, nil }

func TestTwoServerIntegrationKeepsIdenticalPaneIDsSeparate(t *testing.T) {
	local := newEnv(t, []map[string]any{fpane("w1:p1", map[string]any{"agent": "pi"})}, nil)
	remoteSocket := filepath.Join(local.dir, "remote-server", "herdr.sock")
	remote := startFake(t, remoteSocket, []map[string]any{fpane("w1:p1", map[string]any{"agent": "pi", "terminal_id": "remote-terminal"})}, nil)
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	info := remoteInfo{remoteProtocol, binary, remoteSocket, configDir(), filepath.Join(local.dir, "remote-state"), os.Getenv("HERDR_BIN_PATH")}
	ssh := filepath.Join(local.dir, "ssh")
	if err := os.WriteFile(ssh, []byte("#!/bin/sh\nfor command do :; done\nexec /bin/sh -c \"$command\"\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", local.dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("SNOOZE_TEST_RPC_HELPER", "1")
	transport := rpcIntegrationTransport{profile: testMachine("remote"), info: info}
	w := newMachineWorld(liveWorld{}, transport)
	defer w.stop(func() {})
	refreshMachineTest(w)
	loaded := w.load()
	if len(loaded.panes) != 2 {
		t.Fatal(loaded.warnings, loaded.panes)
	}
	p := machinePane(t, loaded, "remote")
	if err := w.snooze(paneTarget(p, "Project"), timeNow().Add(time.Hour), "", defaultConfig()); err != nil {
		t.Fatal(err)
	}
	if remote.tokens("w1:p1")[mark] != "1" {
		t.Fatal("remote agent was not snoozed")
	}
	if local.herdr.tokens("w1:p1")[mark] != nil || len(readState().Panes) != 0 {
		t.Fatal("remote operation touched local agent/state")
	}
	refreshMachineTest(w)
	if err := w.wake(defaultConfig(), []string{p.PaneID}, nil, false); err != nil {
		t.Fatal(err)
	}
	if remote.tokens("w1:p1")[mark] != nil || local.herdr.tokens("w1:p1")[mark] != nil {
		t.Fatal("wrong wake result")
	}
	if local.herdr.count("pane.report_metadata") != 0 || local.herdr.count("agent.view.set") != 0 {
		t.Fatal("remote request changed local server")
	}
}

func TestRemoteWakeRechecksIdentityInsideStateLock(t *testing.T) {
	e, ref := remoteRig(t)
	if _, err := snooze("pane", ref.ID, timeNow().Add(time.Hour), defaultConfig(), &agentID{Agent: ref.Agent}, ""); err != nil {
		t.Fatal(err)
	}
	e.herdr.clearCalls()
	reads := 0
	e.herdr.onCall = func(method string) {
		if method == "pane.list" {
			reads++
			if reads == 2 {
				p := e.herdr.pane(ref.ID)
				p["terminal_id"] = "replacement-terminal"
				p["tokens"].(map[string]any)[mark] = "replacement-token"
			}
		}
	}
	reply := handleRemote(remoteRequest{Method: "wake", Session: ref.Session, Refs: []remoteRef{ref}})
	if reply.Error == "" || !strings.Contains(reply.Error, "changed") {
		t.Fatal("replacement was accepted", reply)
	}
	if e.herdr.count("pane.report_metadata") != 0 || e.herdr.tokens(ref.ID)[mark] != "replacement-token" {
		t.Fatal("stale request cleared replacement tokens")
	}
	if readState().Panes[ref.ID] == nil {
		t.Fatal("stale request cleared state")
	}
}

func TestRemoteHandshakeWaitsForSlowActionAndCancelsPromptly(t *testing.T) {
	for _, cancelled := range []bool{false, true} {
		t.Run(fmtBool(cancelled), func(t *testing.T) {
			dir := t.TempDir()
			script := filepath.Join(dir, "herdr")
			counter := filepath.Join(dir, "polls")
			info := testRemoteInfo()
			raw, _ := json.Marshal(info)
			running := `{"result":{"logs":[{"log_id":"wanted","status":"running"}]}}`
			finished, _ := json.Marshal(map[string]any{"result": map[string]any{"logs": []any{map[string]any{"log_id": "wanted", "status": "succeeded", "stdout": string(raw)}}}})
			threshold := 6
			if cancelled {
				threshold = 100
			}
			body := "#!/bin/sh\nif [ \"$4\" = action ]; then printf '%s\\n' '{\"result\":{\"log\":{\"log_id\":\"wanted\"}}}'; exit; fi\n"
			body += "echo x >> " + shellQuote(counter) + "\nif [ \"$(wc -l < " + shellQuote(counter) + ")\" -le " + strconv.Itoa(threshold) + " ]; then printf '%s\\n' " + shellQuote(running) + "; else printf '%s\\n' " + shellQuote(string(finished)) + "; fi\n"
			if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
				t.Fatal(err)
			}
			t.Setenv("HERDR_BIN_PATH", script)
			ctx := context.Background()
			cancel := func() {}
			if cancelled {
				ctx, cancel = context.WithTimeout(ctx, 150*time.Millisecond)
			}
			defer cancel()
			started := time.Now()
			got, err := (sshTransport{ctx: ctx}).connect(testMachine("test"))
			if cancelled {
				if !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > time.Second {
					t.Fatal(err, time.Since(started))
				}
			} else if err != nil || got != info || time.Since(started) < 500*time.Millisecond {
				t.Fatal(got, err, time.Since(started))
			}
		})
	}
}

func fmtBool(b bool) string {
	if b {
		return "cancelled"
	}
	return "slow-success"
}
