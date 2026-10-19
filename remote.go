package main

// A saved machine owns its snooze state, timer and view. The popup only holds
// snapshots; remote IDs must never reach the local state file or socket.
import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const remoteProtocol = 1
const remoteOutputLimit = 8 << 20

// remoteInfo is printed by an action on the owning server. In particular, do
// not guess its socket, XDG directories or installation path from the client.
type remoteInfo struct {
	Protocol int    `json:"protocol"`
	Binary   string `json:"binary"`
	Socket   string `json:"socket"`
	Config   string `json:"config"`
	State    string `json:"state"`
	Herdr    string `json:"herdr"`
}

func connectionInfo() (remoteInfo, error) {
	binary, err := os.Executable()
	if err != nil {
		return remoteInfo{}, err
	}
	info := remoteInfo{remoteProtocol, binary, socketPath(), configDir(), pluginStateDir(), os.Getenv("HERDR_BIN_PATH")}
	return info, info.validate()
}

func (i remoteInfo) validate() error {
	if i.Protocol != remoteProtocol {
		return errors.New("update Snooze on this machine: incompatible remote protocol")
	}
	for _, p := range []string{i.Binary, i.Socket, i.Config, i.State, i.Herdr} {
		if !filepath.IsAbs(p) || strings.ContainsAny(p, "\x00\r\n") {
			return errors.New("Snooze returned an invalid remote path")
		}
	}
	return nil
}

type remoteSnapshot struct {
	Session string          `json:"session"`
	State   *state          `json:"state"`
	Panes   []pane          `json:"panes"`
	Spaces  []workspaceInfo `json:"spaces"`
}

// A ref is immutable evidence from the row the user picked, including the
// server incarnation and terminal. Identical w1:p1 IDs on two hosts are not
// interchangeable, nor is an agent that replaced a previously listed one.
type remoteRef struct {
	Machine  string `json:"machine"`
	Session  string `json:"session"`
	ID       string `json:"id"`
	Terminal string `json:"terminal,omitempty"`
	Agent    string `json:"agent,omitempty"`
}

func (r remoteRef) key() string {
	data, _ := json.Marshal(r)
	return "remote:" + base64.RawURLEncoding.EncodeToString(data)
}

func parseRemoteRef(id string) (remoteRef, bool, error) {
	if !strings.HasPrefix(id, "remote:") {
		return remoteRef{}, false, nil
	}
	var r remoteRef
	data, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(id, "remote:"))
	if err != nil || json.Unmarshal(data, &r) != nil || r.Machine == "" || r.Session == "" || r.ID == "" {
		return r, true, errors.New("invalid remote agent reference")
	}
	return r, true, nil
}

type remoteRequest struct {
	Endpoint remoteInfo  `json:"endpoint"`
	Method   string      `json:"method"`
	Session  string      `json:"session,omitempty"`
	Target   target      `json:"target,omitempty"`
	Refs     []remoteRef `json:"refs,omitempty"`
	Until    int64       `json:"until,omitempty"`
	When     string      `json:"when,omitempty"`
	All      bool        `json:"all,omitempty"`
}

type remoteReply struct {
	Snapshot *remoteSnapshot `json:"snapshot,omitempty"`
	Error    string          `json:"error,omitempty"`
}

// serveRemote runs in a short-lived SSH process, not a Herdr pane. Only this
// host's existing snooze/wake functions change state; no shell or agent input
// is accepted by the protocol. Never recursively enumerate saved machines.
func serveRemote(in io.Reader, out io.Writer) error {
	previousRPCLogging := rpcLogging
	rpcLogging = true
	defer func() { rpcLogging = previousRPCLogging }()
	data, err := io.ReadAll(io.LimitReader(in, remoteOutputLimit+1))
	if err != nil {
		return err
	}
	if len(data) > remoteOutputLimit {
		return errors.New("remote request too large")
	}
	var req remoteRequest
	if err := json.Unmarshal(data, &req); err != nil {
		return err
	}
	if err := req.Endpoint.validate(); err != nil {
		return err
	}
	// These paths came from the selected server's injected plugin environment.
	// Do not inherit an SSH SendEnv value for another session or plugin.
	for k, v := range map[string]string{
		"HERDR_SOCKET_PATH": req.Endpoint.Socket, "HERDR_PLUGIN_CONFIG_DIR": req.Endpoint.Config,
		"HERDR_PLUGIN_STATE_DIR": req.Endpoint.State, "HERDR_BIN_PATH": req.Endpoint.Herdr,
		"HERDR_PLUGIN_ID": "herdr-snooze",
	} {
		if err := os.Setenv(k, v); err != nil {
			return err
		}
	}
	os.Unsetenv("SNOOZE_STATE_DIR")
	os.Unsetenv("HERDR_PLUGIN_EVENT_JSON")
	os.Unsetenv("HERDR_PLUGIN_CONTEXT_JSON")
	return json.NewEncoder(out).Encode(handleRemote(req))
}

func checkRemoteRefs(session string, refs []remoteRef) error {
	if session == "" || session != sessionKey(sessionIdentity("")) {
		return errors.New("this Herdr session changed; refresh the agent list")
	}
	panes, err := listPanes()
	if err != nil {
		return err
	}
	for _, ref := range refs {
		if ref.Session != session {
			return errors.New("mixed remote sessions")
		}
		if ref.Terminal == "" && ref.Agent == "" {
			spaces, err := listWorkspaces()
			if err != nil {
				return err
			}
			found := false
			for _, space := range spaces {
				found = found || space.WorkspaceID == ref.ID
			}
			if !found {
				return errors.New("that space is no longer there")
			}
			continue
		}
		found := false
		for _, pane := range panes {
			found = found || (pane.PaneID == ref.ID && pane.TerminalID == ref.Terminal && pane.Agent == ref.Agent && pane.Agent != "")
		}
		if !found {
			return errors.New("that agent changed; refresh the agent list")
		}
	}
	return nil
}

func handleRemote(req remoteRequest) remoteReply {
	fail := func(err error) remoteReply { return remoteReply{Error: err.Error()} }
	if req.Method == "snapshot" {
		loaded := (liveWorld{}).load()
		if loaded.err != nil {
			return fail(loaded.err)
		}
		return remoteReply{Snapshot: &remoteSnapshot{sessionKey(sessionIdentity("")), loaded.st, loaded.panes, loaded.spaces}}
	}
	if req.Method != "snooze" && req.Method != "wake" {
		return fail(errors.New("unknown remote operation"))
	}
	check := func() error { return checkRemoteRefs(req.Session, req.Refs) }
	if err := check(); err != nil {
		return fail(err)
	}
	var err error
	cfg := loadConfig()
	switch req.Method {
	case "snooze":
		if len(req.Refs) != 1 || req.Refs[0].ID != req.Target.ID || (req.Target.Kind != "pane" && req.Target.Kind != "workspace") {
			return fail(errors.New("invalid snooze target"))
		}
		if (req.Target.Kind == "pane") != (req.Refs[0].Terminal != "" && req.Refs[0].Agent != "") {
			return fail(errors.New("invalid snooze target kind"))
		}
		if req.Until <= nowMS() || (req.When != "" && req.When != whenDone && req.When != whenMerged) || (req.Target.Kind == "workspace" && req.When != "") {
			return fail(errors.New("invalid snooze deadline or condition"))
		}
		ref := req.Refs[0]
		var expected *agentID
		if req.Target.Kind == "pane" {
			expected = &agentID{Agent: ref.Agent}
		}
		_, err = snoozeExpected(req.Target.Kind, req.Target.ID, fromMS(req.Until), cfg, expected, req.When, ref.Terminal, check)
	case "wake":
		if req.All && len(req.Refs) != 0 || !req.All && len(req.Refs) == 0 {
			return fail(errors.New("invalid wake target"))
		}
		var pids, wids []string
		for _, ref := range req.Refs {
			if ref.Terminal == "" && ref.Agent == "" {
				wids = append(wids, ref.ID)
			} else {
				pids = append(pids, ref.ID)
			}
		}
		_, err = wakeExpected(cfg, pids, wids, req.All, check)
	}
	if err != nil {
		return fail(err)
	}
	return remoteReply{}
}

type machineProfile struct {
	ID      string `json:"id"`
	Label   string `json:"label"`
	Target  string `json:"target"`
	Session string `json:"session"`
	Enabled bool   `json:"enabled"`
}

type machineTransport interface {
	profiles() ([]machineProfile, error)
	connect(machineProfile) (remoteInfo, error)
	call(machineProfile, remoteInfo, remoteRequest) (remoteReply, error)
}

type sshTransport struct{ ctx context.Context }

func (t sshTransport) context() context.Context {
	if t.ctx != nil {
		return t.ctx
	}
	return context.Background()
}

func herdrBinary() string {
	if bin := os.Getenv("HERDR_BIN_PATH"); bin != "" {
		return bin
	}
	return "herdr"
}

func (t sshTransport) profiles() ([]machineProfile, error) {
	data, err := remoteCommand(t.context(), 5*time.Second, nil, herdrBinary(), "machine", "list", "--json")
	if err != nil {
		return nil, err
	}
	var profiles []machineProfile
	err = json.Unmarshal(data, &profiles)
	return profiles, err
}

func (t sshTransport) machineCommand(p machineProfile, args ...string) ([]byte, error) {
	return remoteCommand(t.context(), 10*time.Second, nil, herdrBinary(), append([]string{"--machine", p.ID}, args...)...)
}

func (t sshTransport) connect(p machineProfile) (remoteInfo, error) {
	ctx, cancel := context.WithTimeout(t.context(), 15*time.Second)
	defer cancel()
	t.ctx = ctx
	data, err := t.machineCommand(p, "plugin", "action", "invoke", "herdr-snooze.remote-info")
	if err != nil {
		return remoteInfo{}, fmt.Errorf("update/enable Snooze on this machine: %w", err)
	}
	var invoked struct {
		Result struct {
			Log struct {
				ID string `json:"log_id"`
			} `json:"log"`
		} `json:"result"`
	}
	if json.Unmarshal(data, &invoked) != nil || invoked.Result.Log.ID == "" {
		return remoteInfo{}, errors.New("missing remote Snooze action log")
	}
	// Actions return before their process exits. Correlate by log ID, never by
	// whichever hook happened to finish last. Only this read-only handshake
	// is polled; a snooze/wake request is never automatically replayed.
	for {
		data, err = t.machineCommand(p, "plugin", "log", "list", "--plugin", "herdr-snooze", "--limit", "100")
		if err != nil {
			return remoteInfo{}, err
		}
		var logs struct {
			Result struct {
				Logs []struct {
					ID     string `json:"log_id"`
					Status string `json:"status"`
					Stdout string `json:"stdout"`
				} `json:"logs"`
			} `json:"result"`
		}
		if err = json.Unmarshal(data, &logs); err != nil {
			return remoteInfo{}, err
		}
		for _, log := range logs.Result.Logs {
			if log.ID != invoked.Result.Log.ID {
				continue
			}
			if log.Status == "running" {
				break
			}
			if log.Status != "succeeded" {
				return remoteInfo{}, errors.New("remote Snooze connection action failed")
			}
			var info remoteInfo
			if err = json.Unmarshal([]byte(log.Stdout), &info); err != nil {
				return info, err
			}
			return info, info.validate()
		}
		timer := time.NewTimer(100 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return remoteInfo{}, ctx.Err()
		case <-timer.C:
		}
	}
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }

func (t sshTransport) call(p machineProfile, info remoteInfo, req remoteRequest) (remoteReply, error) {
	if err := info.validate(); err != nil {
		return remoteReply{}, err
	}
	if p.Target == "" || strings.HasPrefix(p.Target, "-") || strings.ContainsAny(p.Target, "\x00\r\n") {
		return remoteReply{}, errors.New("invalid saved SSH target")
	}
	req.Endpoint = info
	data, err := json.Marshal(req)
	if err != nil {
		return remoteReply{}, err
	}
	data, err = remoteCommand(t.context(), 10*time.Second, data, "ssh", "-T", "-o", "BatchMode=yes", "-o", "NumberOfPasswordPrompts=0", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectionAttempts=1", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1", "--", p.Target, "exec "+shellQuote(info.Binary)+" rpc")
	if err != nil {
		return remoteReply{}, fmt.Errorf("%w (request was not retried; refresh to check its result)", err)
	}
	var reply remoteReply
	if err = json.Unmarshal(data, &reply); err != nil {
		return reply, err
	}
	if reply.Error != "" {
		return reply, errors.New(clean(reply.Error, false))
	}
	return reply, nil
}

type cappedOutput struct{ bytes.Buffer }

func (b *cappedOutput) Write(p []byte) (int, error) {
	if b.Len()+len(p) > remoteOutputLimit {
		return 0, errors.New("remote output too large")
	}
	return b.Buffer.Write(p)
}

func remoteCommand(parent context.Context, timeout time.Duration, input []byte, bin string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Stdin = bytes.NewReader(input)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = time.Second
	var out, stderr cappedOutput
	cmd.Stdout, cmd.Stderr = &out, &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		message := clean(strings.TrimSpace(stderr.String()), false)
		if len(message) > 300 {
			message = message[:300]
		}
		return nil, fmt.Errorf("%s: %w %s", filepath.Base(bin), err, message)
	}
	return out.Bytes(), nil
}
