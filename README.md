# herdr-snooze

Snooze an agent in [herdr](https://herdr.dev): hide it from the Agents panel for
15 minutes, an hour, a day, a week — and have it come back by itself when the
time is up.

For the agent that is waiting on a deploy, a review, or tomorrow morning, and
that you would rather not keep looking at until then.

```
  Fix the login bug
  claude · web-app

  1  15 minutes           17:26
  2  1 hour               18:11
  3  3 hours              20:11
  4  6 hours              23:11
  5  1 day                Mon 17:11
  6  3 days               Wed 17:11
  7  1 week               27 Sep 17:11
  c  Custom…

  ↑↓ move · enter select · esc cancel
```

- Snooze one agent, or every agent in a space.
- Flip the Agents panel to show only what is snoozed, and back.
- Wake anything early, in one key.
- No daemon. Works alongside panel-sorting plugins such as
  [herdr-radar](https://github.com/hhdebb/herdr-radar).

## Requirements

- herdr 0.9.0 or newer
- Python 3.8+ as `python3` on the `PATH` herdr sees — standard library only,
  nothing to install or build
- Linux or macOS

## Install

```bash
herdr plugin install mrolafsson/herdr-snooze
```

Then bind the keys you want in `~/.config/herdr/config.toml`. Nothing in a herdr
manifest can add key bindings for you, so **without this step the plugin is
installed but you have no way to reach it** except the command line.

```toml
[[keys.command]]
key = "prefix+shift+z"
type = "plugin_action"
command = "herdr-snooze.agent"
description = "snooze or wake agent"

[[keys.command]]
key = "prefix+alt+z"
type = "plugin_action"
command = "herdr-snooze.toggle"
description = "agents panel: snoozed <-> active"
```

```bash
herdr server reload-config
```

Pick any keys you like; both are free in herdr's default bindings. If you
use a command palette plugin, every action below shows up there too.

### Install from a local checkout

```bash
git clone https://github.com/mrolafsson/herdr-snooze
herdr plugin link ./herdr-snooze
```

## Use

| Action | Does |
| --- | --- |
| `herdr-snooze.agent` | One key, both ways: on an active agent it asks how long and hides it; on a snoozed agent it wakes it, no popup. |
| `herdr-snooze.workspace` | Same, for every agent in the current space — including agents that start there later. |
| `herdr-snooze.toggle` | Flip the Agents panel to list only what is snoozed, and back. |
| `herdr-snooze.wake` | Wake the focused pane's agent; never snoozes. For scripts, or a key that should only ever wake. |
| `herdr-snooze.wake-all` | Wake everything. |

Run any of them from a key binding, a command palette, or the command line:

```bash
herdr plugin action invoke herdr-snooze.agent
```

### Choosing how long

Press a number, or move with `↑` `↓` / `j` `k` and press `enter`. `esc` cancels.

`c` takes anything:

| You type | Means |
| --- | --- |
| `45m`, `2h`, `3d`, `1w` | that long from now |
| `1h30m`, `2 hours` | combined and spelled-out units work |
| `90` | a bare number is minutes |
| `17:30`, `9am`, `9:15 pm` | the next time the clock says that |
| `tomorrow`, `tomorrow 14:00` | tomorrow at 9:00, or at the time given |

A preset counts from the moment you pick it, not from when the popup opened.

### Seeing what is snoozed, and waking it

While anything is snoozed, the Agents panel label shows the icon and a count:
`💤 2` (or, when another plugin labels the panel, its label plus the count, such
as `active 💤 2`).

The sidebar is the only list there is: it shows either your active agents or
your snoozed ones, and `herdr-snooze.toggle` picks which. On the snoozed
side the label reads `💤 snoozed only · 2`, so the two cannot be confused.

To wake one, click it there to focus it and press the snooze key again. To give
it a different deadline, press it twice: wake, then snooze.

To see *when* each one wakes, add `"$snoozed"` to your agent rows. Only snoozed
agents carry that token, so it appears in the snoozed list and nowhere else:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "$snoozed"], ["terminal_title_stripped"]]
```

```
  ○ web-app  💤 17:30
    Fix the login bug
```

## How it behaves

**It hides; it does not pause or mute.** The pane keeps running and the agent
keeps working. herdr's notifications and the space's status rollup still fire —
the Agents panel is the only thing that changes (sidebar rows, mouse targets,
`focus_agent` numbering, next/previous agent).

**It comes back on time.** herdr itself expires the marker that hides the agent,
so the agent reappears at the deadline even if nothing of this plugin is
running. The label and the panel catch up within about a second.

**The snoozed list is somewhere you visit, not a mode you get stuck in.** A
panel that only lists snoozed agents would hide the one that just got blocked,
so:

- Blocked agents stay visible in the snoozed list even though they are not
  snoozed, and the label says so: `💤 snoozed only · 2 + 1 blocked`.
- The panel returns to normal by itself when you focus an agent that is not
  snoozed, when you wake the agent you are on, when the last snooze ends, when
  you use another plugin's panel control, or when herdr restarts. A shell or a
  popup taking focus does not count, and neither does the pane you opened the
  list from until you have left it.
- Set `"auto_return": false` if you would rather have a plain toggle.

**Spaces stay in the Spaces list.** herdr has no filter for that list. Snoozing
a space hides its agents and puts a `snoozed` token on the space, which you can
show by adding `"$snoozed"` to a row:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace", "$snoozed"],
  ["branch", "git_status"],
]
```

An agent you snooze or wake on its own inside a snoozed space keeps its own
deadline.

**It survives restarts and long waits.** Snoozes are restored after a herdr
restart, and a week-long snooze stays hidden for the week. If the machine sleeps
through a deadline, the agent reappears at your first focus change after waking.

**The 💤 is not a button.** herdr offers plugins no hook for clicking the panel
label, and its right-click menus are built-in only (as of 0.9.1), so everything
here is reached by key, palette or command line.

## Configuration

Optional. Create `config.json` in the plugin's config directory:

```bash
herdr plugin config-dir herdr-snooze
```

```json
{
  "durations": ["15m", "1h", "3h", "6h", "tomorrow 9am", "1d", "3d", "1w"],
  "badge": "💤",
  "notify": false,
  "auto_return": true
}
```

| Key | Default | |
| --- | --- | --- |
| `durations` | `["15m","1h","3h","6h","1d","3d","1w"]` | Rows in the picker, 9 at most. Anything the Custom field accepts. Entries that don't parse are skipped. |
| `badge` | `"💤"` | Shown in the panel label and in the `$snoozed` token. Any text; `""` for none. |
| `notify` | `false` | Show a herdr notification when something is snoozed. |
| `auto_return` | `true` | Leave the snoozed list automatically, as described above. |
| `views` | — | Teach snooze another plugin's panel order. See below. |
| `sort` | — | Always use this `agent.view.set` sort while something is snoozed. |

Changes apply to the next action; no reload needed. See
[`config.example.json`](config.example.json).

### Alongside plugins that sort the Agents panel

herdr has a single Agents panel view, and whichever plugin set it last owns it.
Plugins that order the panel hold it; snooze needs it to filter. So snooze
**borrows** it: while something is snoozed it applies its filter together with
the other plugin's order, and hands the view back when the last snooze ends.

It knows the orders of **herdr-radar** and **herdr-agent-inbox** out of the box.
For any other plugin the panel falls back to herdr's own order while something
is snoozed. To fix that, give snooze the plugin's view source, label and sort:

```json
{
  "views": {
    "plugin:some.other-plugin": {
      "its view label": [{ "field": { "token": "its_sort_token" }, "order": "desc" }]
    }
  }
}
```

While something is snoozed, the other plugin's "turn my order off" control may
not take effect until the last snooze ends.

## Uninstall

Wake everything first, so the panel is handed back cleanly to whichever plugin
orders it:

```bash
herdr plugin action invoke herdr-snooze.wake-all
herdr plugin uninstall herdr-snooze
```

Then, by hand:

- Remove the `[[keys.command]]` blocks that reference `herdr-snooze.*` from
  `config.toml`, and `"$snoozed"` from `ui.sidebar.spaces.rows` if you added it,
  then `herdr server reload-config`.
- herdr leaves the plugin's config and state directories behind:

  ```bash
  rm -r ~/.config/herdr/plugins/config/herdr-snooze ~/.local/state/herdr/plugins/herdr-snooze
  ```

If you uninstall while agents are still snoozed, nothing breaks: herdr drops the
plugin's panel filter immediately, so every agent is visible again, and the
leftover `snoozed` tokens expire on their own within 24 hours. A plugin that
orders the panel may need its order re-applied (for herdr-radar, run its
`view-flip` action twice).

## Troubleshooting

Plugin output is captured rather than printed, so this is where errors surface:

```bash
herdr plugin log list --plugin herdr-snooze --limit 10
```

**The key does nothing.** Check the binding's `command` is `herdr-snooze.<action>` and
that you ran `herdr server reload-config`. `herdr plugin action invoke
herdr-snooze.agent` tests the action without the key.

**"This pane has no agent".** Snoozing hides agents; a shell has nothing to hide.

**An agent is hidden and I can't find it.** `herdr-snooze.toggle` flips the
sidebar to the snoozed agents; `herdr-snooze.wake-all` brings everything back.

**The panel lost its grouping or order.** Another plugin's view was replaced and
not restored — see *Alongside plugins that sort the Agents panel*. Waking
everything hands the view back.

**`python3` not found.** herdr runs plugins with its own `PATH`, which may not
include a version manager's shims. Make `python3` resolvable there, or start
herdr from a shell where it is.

To see what snooze has recorded, from a checkout:

```bash
python3 snooze.py list
```

## How it works

A snoozed pane carries a `snoozed` metadata token, and the Agents panel view is
filtered to `not(exists snoozed)`. The token is reported with a TTL, so herdr
drops it at the deadline.

A small state file, `snoozed.json` in the plugin's state directory, holds the
real deadlines. `tick` — a startup hook plus a few event hooks — reconciles what
a TTL alone cannot: herdr caps a token's TTL at 24h, tokens and the view do not
survive a server restart, and another plugin can replace the view. Each snooze
also arms one detached `sleep` that runs a tick at the next moment something
needs doing. It sleeps, ticks once and is gone; it is not a daemon.

The state file exists only while something is snoozed, and every command goes
through `run.sh`, which exits before starting Python when a hook finds no file
(~8ms per event). It also caches the real interpreter's path, because `python3`
is often a pyenv/asdf shim that adds ~100ms to every start.

## Development

```bash
python3 -m unittest discover -s tests
herdr plugin link "$PWD"        # re-run after editing the manifest; read `warnings`

python3 snooze.py snooze w1:p1 30s      # also: workspace:w1
python3 snooze.py toggle
python3 snooze.py wake --all
```

`SNOOZE_STATE_DIR` points the CLI at a scratch state directory, so experiments
don't touch real snoozes. `herdr plugin link` does not copy files: edits to
`snooze.py` are live immediately.

## License

[MIT](LICENSE)
