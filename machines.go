package main

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

const machineRefresh = 10 * time.Second
const machineConcurrency = 4

type machineSource struct {
	profile  machineProfile
	info     remoteInfo
	snapshot *remoteSnapshot
	err      error
}

type machineWorld struct {
	local        world
	transport    machineTransport
	mu           sync.Mutex
	sources      map[string]machineSource
	refreshing   bool
	refreshed    time.Time
	discoveryErr error
	closed       bool
	running      sync.WaitGroup
}

func newMachineWorld(local world, transport machineTransport) *machineWorld {
	return &machineWorld{local: local, transport: transport, sources: map[string]machineSource{}}
}

func (w *machineWorld) load() loadedMsg {
	loaded := w.local.load()
	w.mu.Lock()
	if !w.closed && !w.refreshing && time.Since(w.refreshed) >= machineRefresh {
		w.refreshing = true
		w.running.Add(1)
		go w.refresh()
	}
	// Always return local agents immediately. A sleeping SSH host cannot hold
	// the popup open on a loading screen or prevent local snooze/wake.
	if w.discoveryErr != nil {
		loaded.warnings = append(loaded.warnings, "Saved machines: "+w.discoveryErr.Error())
	}
	ids := sortedKeys(w.sources)
	for _, id := range ids {
		source := w.sources[id]
		if source.err != nil {
			loaded.warnings = append(loaded.warnings, clean(source.profile.Label, false)+": "+source.err.Error())
		} else if source.snapshot != nil {
			mergeMachine(&loaded, source.profile, source.snapshot)
		}
	}
	w.mu.Unlock()
	return loaded
}

func (w *machineWorld) refresh() {
	defer w.running.Done()
	defer func() {
		w.mu.Lock()
		w.refreshing, w.refreshed = false, time.Now()
		w.mu.Unlock()
	}()
	profiles, err := w.transport.profiles()
	w.mu.Lock()
	w.discoveryErr = err
	if err != nil {
		w.sources = map[string]machineSource{}
		w.mu.Unlock()
		return
	}
	next := map[string]machineSource{}
	for _, p := range profiles {
		if !p.Enabled || p.ID == "" {
			continue
		}
		source := w.sources[p.ID]
		if source.profile.ID != p.ID || source.profile.Target != p.Target || source.profile.Session != p.Session {
			source = machineSource{}
		}
		source.profile = p
		next[p.ID] = source
	}
	w.sources = next
	jobs := make([]machineSource, 0, len(next))
	for _, source := range next {
		jobs = append(jobs, source)
	}
	w.mu.Unlock()
	var wg sync.WaitGroup
	limit := make(chan struct{}, machineConcurrency)
	for _, source := range jobs {
		wg.Add(1)
		go func(id string, source machineSource) {
			defer wg.Done()
			limit <- struct{}{}
			defer func() { <-limit }()
			info := source.info
			var err error
			if info.Protocol == 0 {
				info, err = w.transport.connect(source.profile)
			}
			var reply remoteReply
			if err == nil {
				reply, err = w.transport.call(source.profile, info, remoteRequest{Method: "snapshot"})
			}
			if err == nil && (reply.Snapshot == nil || reply.Snapshot.Session == "" || reply.Snapshot.State == nil) {
				err = errors.New("invalid remote Snooze snapshot")
			}
			if err == nil {
				sanitize(&reply.Snapshot.Panes)
				for i := range reply.Snapshot.Spaces {
					reply.Snapshot.Spaces[i].Label = clean(reply.Snapshot.Spaces[i].Label, false)
				}
				source.info, source.snapshot, source.err = info, reply.Snapshot, nil
			} else {
				// Do not leave stale, selectable rows for a disconnected host.
				// Rediscovery may be attempted on a later read, never by
				// replaying a possibly committed mutation.
				source.info, source.snapshot, source.err = remoteInfo{}, nil, err
			}
			w.mu.Lock()
			w.sources[id] = source
			w.mu.Unlock()
		}(source.profile.ID, source)
	}
	wg.Wait()
}

// mergeMachine namespaces every reference, including inherited workspace
// snoozes and Just woke rows. No remote state is ever saved on this client.
func mergeMachine(loaded *loadedMsg, profile machineProfile, snap *remoteSnapshot) {
	if loaded.st == nil {
		loaded.st = emptyState()
	}
	refs := map[string]remoteRef{}
	workspace := func(id string) string { return (remoteRef{Machine: profile.ID, Session: snap.Session, ID: id}).key() }
	for _, p := range snap.Panes {
		refs[p.PaneID] = remoteRef{Machine: profile.ID, Session: snap.Session, ID: p.PaneID, Terminal: p.TerminalID, Agent: p.Agent}
		p.PaneID, p.WorkspaceID = refs[p.PaneID].key(), workspace(p.WorkspaceID)
		loaded.panes = append(loaded.panes, p)
	}
	label := clean(profile.Label, false)
	if label == "" {
		label = clean(profile.ID, false)
	}
	for _, space := range snap.Spaces {
		space.WorkspaceID = workspace(space.WorkspaceID)
		if space.Label != "" {
			space.Label = label + " · " + space.Label
		} else {
			space.Label = label
		}
		loaded.spaces = append(loaded.spaces, space)
	}
	for id, e := range snap.State.Panes {
		if ref, ok := refs[id]; ok && e != nil {
			copy := *e
			copy.WorkspaceID = workspace(e.WorkspaceID)
			loaded.st.Panes[ref.key()] = &copy
		}
	}
	for id, e := range snap.State.Workspaces {
		if e != nil {
			copy := *e
			loaded.st.Workspaces[workspace(id)] = &copy
		}
	}
	for id, e := range snap.State.Woke {
		if ref, ok := refs[id]; ok && e != nil {
			copy := *e
			loaded.st.Woke[ref.key()] = &copy
		}
	}
}

// sourceFor rechecks the saved profile before any write. A disabled, removed
// or retargeted profile must not silently turn a selected row into a write
// to another host/session.
func (w *machineWorld) sourceFor(ref remoteRef) (machineSource, error) {
	w.mu.Lock()
	source, ok := w.sources[ref.Machine]
	w.mu.Unlock()
	if !ok || source.err != nil || source.snapshot == nil || source.snapshot.Session != ref.Session {
		return source, errors.New("this machine/session changed; refresh the agent list")
	}
	profiles, err := w.transport.profiles()
	if err != nil {
		return source, err
	}
	for _, p := range profiles {
		if p.ID == ref.Machine && p.Enabled && p.Target == source.profile.Target && p.Session == source.profile.Session {
			return source, nil
		}
	}
	return source, errors.New("saved machine was disabled, removed or changed; refresh the agent list")
}

func (w *machineWorld) snooze(t target, until time.Time, when string, cfg config) error {
	ref, remote, err := parseRemoteRef(t.ID)
	if err != nil {
		return err
	}
	if !remote {
		return w.local.snooze(t, until, when, cfg)
	}
	source, err := w.sourceFor(ref)
	if err != nil {
		return err
	}
	t.ID = ref.ID
	_, err = w.transport.call(source.profile, source.info, remoteRequest{Method: "snooze", Session: ref.Session,
		Target: t, Refs: []remoteRef{ref}, Until: until.UnixMilli(), When: when})
	w.invalidate()
	return err
}

func (w *machineWorld) wake(cfg config, panes, spaces []string, all bool) error {
	var localPanes, localSpaces []string
	groups := map[string][]remoteRef{}
	for kind, ids := range [][]string{panes, spaces} {
		for _, id := range ids {
			ref, remote, err := parseRemoteRef(id)
			if err != nil {
				return err
			}
			if remote {
				groups[ref.Machine] = append(groups[ref.Machine], ref)
			} else if kind == 0 {
				localPanes = append(localPanes, id)
			} else {
				localSpaces = append(localSpaces, id)
			}
		}
	}
	if all {
		w.mu.Lock()
		for id, source := range w.sources {
			if source.err == nil && source.snapshot != nil {
				groups[id] = []remoteRef{{Machine: id, Session: source.snapshot.Session, ID: "all"}}
			}
		}
		w.mu.Unlock()
	}
	// Validate all destinations before starting any partial operation.
	sources := map[string]machineSource{}
	for id, refs := range groups {
		source, err := w.sourceFor(refs[0])
		if err != nil {
			return err
		}
		for _, ref := range refs {
			if ref.Session != refs[0].Session {
				return errors.New("mixed remote sessions")
			}
		}
		sources[id] = source
	}
	var errs []error
	if all || len(localPanes)+len(localSpaces) > 0 {
		if err := w.local.wake(cfg, localPanes, localSpaces, all); err != nil {
			errs = append(errs, err)
		}
	}
	// Wake all means the machines shown in this popup, not any transitive
	// profiles those machines themselves have saved.
	for _, id := range sortedKeys(sources) {
		source := sources[id]
		req := remoteRequest{Method: "wake", Session: groups[id][0].Session, Refs: groups[id], All: all}
		if all {
			req.Refs = nil
		}
		if _, err := w.transport.call(source.profile, source.info, req); err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", clean(source.profile.Label, false), err))
		}
	}
	w.invalidate()
	return errors.Join(errs...)
}

func (w *machineWorld) stop(cancel context.CancelFunc) {
	w.mu.Lock()
	w.closed = true
	w.mu.Unlock()
	cancel()
	w.running.Wait()
}

func (w *machineWorld) invalidate() {
	w.mu.Lock()
	w.refreshed = time.Time{}
	w.mu.Unlock()
}

func (w *machineWorld) focus(id string) error {
	ref, remote, err := parseRemoteRef(id)
	if err != nil {
		return err
	}
	if remote {
		w.mu.Lock()
		source := w.sources[ref.Machine]
		w.mu.Unlock()
		return fmt.Errorf("switch to %s in Herdr to visit this agent; snooze and wake work here", clean(source.profile.Label, false))
	}
	return w.local.focus(id)
}
func (w *machineWorld) open(p popupRef) error         { return w.local.open(p) }
func (w *machineWorld) siblings() map[string]popupRef { return w.local.siblings() }

func machineWarning(warnings []string) string {
	if len(warnings) == 0 {
		return ""
	}
	sort.Strings(warnings)
	return strings.Join(warnings, "; ")
}
