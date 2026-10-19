package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
)

var defaultDurations = []string{"15m", "1h", "3h", "6h", "1d", "3d", "1w"}

const defaultBadge = "\U0001F4A4" // 💤

type config struct {
	Durations []string
	Badge     string
	Notify    bool
	// Sort pins the panel's order while anything is snoozed, as herdr's
	// agent.view.set takes it. nil: work it out (see planView).
	Sort []any
	// Views teaches snooze the sort behind another plugin's view:
	// {"<source>": {"<label>": [<sort key>, …]}}.
	Views map[string]map[string][]any
	// AutoReturn ends a visit to the snoozed list once focus lands on an
	// active agent.
	AutoReturn bool
	// Theme: "dark", "light", or empty to ask the terminal.
	Theme string
	// WakeBlocked wakes a snoozed agent when it comes to need you.
	WakeBlocked bool
	// NotifyWake shows a toast when an agent wakes by itself.
	NotifyWake bool
	// WokeBadge is "$woke" on an agent that just woke; "" for none.
	WokeBadge string
	// Machines includes enabled saved SSH machines in the popup. Hooks and
	// timers always remain local to their owning server.
	Machines bool
}

func defaultConfig() config {
	return config{
		Durations:   append([]string(nil), defaultDurations...),
		Badge:       defaultBadge,
		AutoReturn:  true,
		WakeBlocked: true,
		NotifyWake:  true,
		WokeBadge:   defaultWokeBadge,
		Machines:    true,
	}
}

func pluginID() string {
	if id := os.Getenv("HERDR_PLUGIN_ID"); id != "" {
		return id
	}
	return "herdr-snooze"
}

func source() string { return "plugin:" + pluginID() }

func xdgDir(env, fallback string) string {
	if v := os.Getenv(env); v != "" {
		return v
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, fallback)
}

func configDir() string {
	if d := os.Getenv("HERDR_PLUGIN_CONFIG_DIR"); d != "" {
		return d
	}
	return filepath.Join(xdgDir("XDG_CONFIG_HOME", ".config"), "herdr", "plugins", "config", pluginID())
}

// herdrStateRoot is where herdr keeps every plugin's state directory.
func herdrStateRoot() string {
	return filepath.Join(xdgDir("XDG_STATE_HOME", ".local/state"), "herdr", "plugins")
}

// pluginStateDir is the plugin's state directory. herdr gives one per
// plugin, shared by every herdr session on the machine.
func pluginStateDir() string {
	if d := os.Getenv("SNOOZE_STATE_DIR"); d != "" {
		return d
	}
	if d := os.Getenv("HERDR_PLUGIN_STATE_DIR"); d != "" {
		return d
	}
	return filepath.Join(herdrStateRoot(), pluginID())
}

// sortFields is herdr's agent-view sort, as agent.view.set accepts it
// (src/api/schema/agents.rs): anything else and herdr refuses the whole view,
// on every sync.
var sortFields = map[string]bool{
	"workspace_order": true, "tab_order": true, "pane_order": true, "attention": true,
	"status": true, "agent": true, "seen": true, "state_change_seq": true,
}

func sortKeyOK(key any) bool {
	k, ok := key.(map[string]any)
	if !ok {
		return false
	}
	named, token := false, false
	switch f := k["field"].(type) {
	case string:
		named = sortFields[f]
	case map[string]any:
		_, isString := f["token"].(string)
		token = len(f) == 1 && isString
	}
	order, has := k["order"]
	if !has {
		order = "asc"
	}
	return (named || token) && (order == "asc" || order == "desc")
}

func sortOK(v any) ([]any, bool) {
	list, ok := v.([]any)
	if !ok {
		return nil, false
	}
	for _, key := range list {
		if !sortKeyOK(key) {
			return nil, false
		}
	}
	return list, true
}

// loadConfig reads config.json. Each setting has a default and a shape: a
// wrong value falls back to its default, with a note in the plugin log,
// instead of crashing a hook halfway through a change (and every hook
// after it).
func loadConfig() config {
	cfg := defaultConfig()
	data, err := os.ReadFile(filepath.Join(configDir(), "config.json"))
	if errors.Is(err, os.ErrNotExist) {
		return cfg
	}
	if err != nil {
		logf("config.json ignored, using defaults: %v", err)
		return cfg
	}
	var user map[string]any
	if err := json.Unmarshal(data, &user); err != nil {
		var top any
		if json.Unmarshal(data, &top) == nil {
			logf("config.json ignored: it must be a JSON object")
		} else {
			logf("config.json ignored, using defaults: %v", err)
		}
		return cfg
	}
	invalid := func(key string) { logf("config.json: %q has an invalid value; using the default", key) }
	for key, value := range user {
		switch key {
		case "durations":
			list, ok := value.([]any)
			var out []string
			for _, d := range list {
				switch d := d.(type) {
				case string:
					out = append(out, d)
				case float64:
					if d != float64(int64(d)) {
						ok = false
					}
					out = append(out, strconv.FormatInt(int64(d), 10))
				default:
					ok = false
				}
			}
			if ok && len(out) > 0 {
				cfg.Durations = out
			} else {
				invalid(key)
			}
		case "badge":
			if s, ok := value.(string); ok {
				cfg.Badge = s
			} else {
				invalid(key)
			}
		case "woke_badge":
			if s, ok := value.(string); ok {
				cfg.WokeBadge = s
			} else {
				invalid(key)
			}
		case "notify", "auto_return", "wake_when_blocked", "notify_wake", "machines":
			b, ok := value.(bool)
			switch {
			case !ok:
				invalid(key)
			case key == "machines":
				cfg.Machines = b
			case key == "notify":
				cfg.Notify = b
			case key == "auto_return":
				cfg.AutoReturn = b
			case key == "notify_wake":
				cfg.NotifyWake = b
			default:
				cfg.WakeBlocked = b
			}
		case "sort":
			if value == nil {
				continue
			}
			if s, ok := sortOK(value); ok {
				cfg.Sort = s
			} else {
				invalid(key)
			}
		case "views":
			if value == nil {
				continue
			}
			if v, ok := viewsOK(value); ok {
				cfg.Views = v
			} else {
				invalid(key)
			}
		case "theme":
			if s, ok := value.(string); ok && (s == "" || s == "dark" || s == "light") {
				cfg.Theme = s
			} else {
				invalid(key)
			}
		}
	}
	return cfg
}

func viewsOK(value any) (map[string]map[string][]any, bool) {
	owners, ok := value.(map[string]any)
	if !ok {
		return nil, false
	}
	out := map[string]map[string][]any{}
	for owner, labels := range owners {
		byLabel, ok := labels.(map[string]any)
		if !ok {
			return nil, false
		}
		out[owner] = map[string][]any{}
		for label, sort := range byLabel {
			s, ok := sortOK(sort)
			if !ok {
				return nil, false
			}
			out[owner][label] = s
		}
	}
	return out, true
}

// Herdr captures normal plugin logs on stdout. An SSH RPC process reserves
// stdout for its one JSON reply and directs these diagnostics to stderr.
var rpcLogging bool

func logf(format string, args ...any) {
	out := os.Stdout
	if rpcLogging {
		out = os.Stderr
	}
	fmt.Fprintf(out, format+"\n", args...)
}
