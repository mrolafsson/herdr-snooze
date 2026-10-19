# herdr-snooze

**Keep a long list of agents down to the ones you're working with.**

Agents wait a lot: on CI, on a review, on a teammate, on tomorrow morning.
Every one of them stays in herdr's Agents panel the whole time, between you
and the agents that need you now. herdr-snooze is a
[herdr](https://herdr.dev) plugin that hides an agent, or a whole space, from
the Agents panel until a time you pick, and brings it back by itself when the
time is up.

<img src="docs/images/picker.png" alt="The snooze popup on an agent: 15 minutes to a week, each with the time it would wake, and Custom for your own" width="440">

- **One key, both ways.** On an agent that's in the way, it asks how long
  and hides it; on a snoozed one, it wakes it.
- **For as long as you like.** 15 minutes, an hour, *tomorrow 9am*, a week,
  or anything you type, read as you type it.
- **Or until something happens.** Until the agent's done working, or until
  its pull request is merged; at the latest, the next morning.
- **A whole space at once**, including agents that start there later.
- **It comes back on time**, with nothing of the plugin running: herdr
  itself drops what hides it at the deadline.
- **One list of everything.** What's snoozed and until when, and every agent
  that isn't: wake one, change when it wakes, or snooze another from the
  same popup. Enabled saved SSH machines appear there too, labelled by machine.
- **Or look in the panel.** One key flips the Agents panel to only what's
  snoozed, and back.
- **Nothing stuck stays hidden.** A snoozed agent that comes to need you
  wakes by itself.
- **It tells you.** When an agent wakes by itself, a toast says which one,
  and why: *fix flaky tests is awake: It's done*. It's then at the top of
  the list, under *Just woke*, and wears a ⏰ in the Agents panel, until you
  go to it.
- **Works with Recap.** Snoozed agents have a tab of their own there;
  `ctrl+s` snoozes the agent you're on, and `ctrl+w` wakes it.
- **It fits in.** Follows herdr's theme, works with the mouse, and gets
  along with plugins that sort the Agents panel. Linux and macOS.

## Works with the other herdr plugins

This is one of five plugins that are useful alone and better together.
Install [herdr-recap](https://github.com/mrolafsson/herdr-recap), [herdr-github](https://github.com/mrolafsson/herdr-github), [herdr-linear](https://github.com/mrolafsson/herdr-linear) or [herdr-file-transfer](https://github.com/mrolafsson/herdr-file-transfer) alongside it and they find each other:
there is nothing to configure.

| Plugin | What it adds to the others |
| --- | --- |
| [herdr-recap](https://github.com/mrolafsson/herdr-recap) | Every agent in one list. An agent's pull request and its Linear issue are links there: a click, or `ctrl+g` / `ctrl+l`, opens it in the plugin it belongs to. Snoozed agents have a tab of their own: `ctrl+s` snoozes an agent, in Snooze's popup, and `ctrl+w` wakes one. |
| [herdr-github](https://github.com/mrolafsson/herdr-github) | Tags every space and agent with its branch's pull request (`#482 ✓ approved`), which Recap shows and groups its agents by. Opens straight on an agent's PR when Recap asks. |
| [herdr-linear](https://github.com/mrolafsson/herdr-linear) | Tags every space and agent with the Linear issue its branch is named for (`ENG-142 In Progress`), which Recap shows. Opens straight on that issue when Recap asks. |
| **[herdr-snooze](https://github.com/mrolafsson/herdr-snooze)** (this one) | Hides an agent, or a whole space, from herdr's Agents panel until a time you pick, or until the agent's done or its pull request is merged (from herdr-github's labels). Tags each snoozed agent with when it wakes (`💤 17:30`, `💤 till done`), and one that just woke by itself with when and why, which Recap shows. Opens straight on an agent when Recap asks, and opens Recap on one with `r`. |
| [herdr-file-transfer](https://github.com/mrolafsson/herdr-file-transfer) | Files between the machine you're at and the server herdr runs on. Chooses the files a new Linear issue attaches (`ctrl+f` in herdr-linear's form): any on the server, or one brought over from your Mac, or from an iPad with Taildrop. |

- **Auto-discovery.** Each plugin asks herdr which others are installed and
  enabled. A link only appears when the plugin behind it is there; without
  it the same text is shown, as text, and its key is left out.
- **Clickable, and there and back.** herdr shows one popup at a time, so
  following a link turns the open popup into the other plugin's, in place and
  without a flicker, and `esc` there brings you back to where you were.
- **Shared look.** A title, a branch, a pull request and a key are
  drawn the same in all five, in herdr's theme colours.

See [With Recap](#with-recap) for the keys and the convention behind it.

**Contents:** [Works with the other plugins](#works-with-the-other-herdr-plugins) ·
[A long list, under control](#a-long-list-under-control) · [Requirements](#requirements) ·
[Install](#install) · [Use](#use) · [With Recap](#with-recap) ·
[How it behaves](#how-it-behaves) · [Configuration](#configuration) ·
[Uninstall](#uninstall) · [Troubleshooting](#troubleshooting) ·
[How it works](#how-it-works) · [Development](#development)

## A long list, under control

Ten agents is a normal afternoon. Two need you, three are working, and the
other five are waiting on something that isn't you: a CI run, a review, the
API team, the morning. Those five sit in the Agents panel all the same,
between you and the two that need you, and every time you look you read past
them again.

This is how they stay out of the way:

- **Snooze what's waiting, for as long as it waits.** The agent that opened a
  pull request: until the review's likely back, *3 hours*. The one waiting on
  the API team: *tomorrow 9am*. It's gone from the panel, and when the time is
  up it's back, without you having to remember it.
- **Or until it's worth looking at.** The agent running a long refactor:
  *until done*, and it's back, finished, at the top of the panel. The one
  whose pull request is in review: *until merged*, and it's back once it is,
  to clean up its worktree. These are short waits, so if it hasn't happened
  by the next morning, it's back then anyway.
- **Snooze a space you're not on today.** The docs space can sit for three
  days. Its agents go with it, and so does any agent started there meanwhile.
- **Keep the panel for what needs you.** What's left is what you're working
  with: the agents that are working, and the ones that need you, first.
- **Look at what's snoozed when you choose to.** The list shows everything
  that's snoozed and when each wakes; a key in the panel flips it to only the
  snoozed ones. Waking one early, or giving it longer, is one key either way.
- **Nothing that needs you stays hidden.** A snoozed agent that gets stuck
  wakes there and then, and herdr notifies you as it always does. Snoozing
  hides; it doesn't pause or mute.

With [Recap](https://github.com/mrolafsson/herdr-recap) installed, snoozed
agents have a tab of their own there, so coming back to the list starts with
the ones you didn't put away. `ctrl+s` snoozes the agent you're reading
about, and on the Snoozed tab `ctrl+w` wakes one.

## Requirements

- **herdr 0.9.0** or later.
- **Linux** or **macOS**, on arm64 or x86-64.
- For saved machines: **herdr 0.9.5** or later on the popup's host, this
  Snooze build installed and enabled on each remote server, and working
  noninteractive OpenSSH access. Local-only use does not need SSH.

## Install

```sh
herdr plugin install mrolafsson/herdr-snooze
```

The build step compiles it with Go if you have it, or downloads the release
binary for your platform and checks its SHA-256.

Then bind the keys you want in `~/.config/herdr/config.toml`. Nothing in a
herdr manifest can add key bindings for you, so **without this step the
plugin is installed but you have no way to reach it** except the command
palette and the command line.

```toml
[[keys.command]]
key = "prefix+shift+z"
type = "plugin_action"
command = "herdr-snooze.agent"
description = "snooze or wake agent"

[[keys.command]]
key = "prefix+alt+z"
type = "plugin_action"
command = "herdr-snooze.manage"
description = "snooze: every agent"

[[keys.command]]
key = "prefix+ctrl+z"
type = "plugin_action"
command = "herdr-snooze.toggle"
description = "agents panel: priority -> grouped -> snoozed"
```

On macOS, `alt` needs Option to send Alt in your terminal (Ghostty:
`macos-option-as-alt = true`).

Then `herdr server reload-config`, and **start a new herdr client** (detach
and run `herdr` again): a client that was already open keeps the key
bindings it started with. Pick any keys you like; these are free in herdr's
default bindings.

`Snooze: demo (fictional agents)` in the command palette shows the list on
made-up agents: safe to try, safe to screenshot.

### From a local checkout

```sh
git clone https://github.com/mrolafsson/herdr-snooze
herdr plugin link ./herdr-snooze
```

`herdr plugin link` runs the build step (`scripts/build.sh`) the first time;
after editing the code, `go build -o bin/herdr-snooze .`.

## Use

| Action | Does |
| --- | --- |
| `herdr-snooze.agent` | One key, both ways: on an awake agent it asks how long and hides it; on a snoozed agent it wakes it, no popup. |
| `herdr-snooze.workspace` | Same, for every agent in the current space, including agents that start there later. |
| `herdr-snooze.manage` | The list of every agent: what's snoozed and until when, then everything awake. |
| `herdr-snooze.toggle` | Step the Agents panel through `priority` order, `grouped` order and the list of only what is snoozed, round again. |
| `herdr-snooze.order` | Switch the Agents panel between `priority` and `grouped` order only, skipping the snoozed list. |
| `herdr-snooze.wake` | Wake the focused pane's agent; never snoozes. For scripts, or a key that should only ever wake. |
| `herdr-snooze.wake-all` | Wake everything. |
| `herdr-snooze.demo` | The list on fictional agents. |

Run any of them from a key binding, the command palette, or the command line:

```sh
herdr plugin action invoke herdr-snooze.agent
```

### Choosing how long

Your snooze key on an agent opens the popup at the top: how long, each with
the time the agent would wake. Press a number, or move with `↑` `↓` and press
`enter`; a click picks one too. `esc` cancels; `tab` goes to the list
instead.

`c` takes anything, and says when that is as you type:

<img src="docs/images/custom.png" alt="Typing tomorrow 2pm, read as Wednesday at 14:00, 23 hours 55 minutes from now" width="440">

| You type | Means |
| --- | --- |
| `45m`, `2h`, `3d`, `1w` | that long from now |
| `1h30m`, `2 hours` | combined and spelled-out units work |
| `90` | a bare number is minutes |
| `17:30`, `9am`, `9:15 pm` | the next time the clock says that |
| `tomorrow`, `tomorrow 14:00` | tomorrow at 9:00, or at the time given |

A preset counts from the moment you pick it, not from when the popup opened.

Below Custom, the popup offers what this agent can be snoozed **until**:

| Key | Until | Offered |
| --- | --- | --- |
| `d` | **done**: it stops working (it's done, idle, or stuck) | while it's working |
| `m` | **merged**: its pull request is merged, or closed | while it has one open, from [herdr-github](https://github.com/mrolafsson/herdr-github)'s labels |

These are short waits, so each has a cap: if it hasn't happened by 9:00 the
next morning, the agent's back then anyway (the row says *or Wed 09:00*).
The sidebar's `$snoozed` and the list say *till done* or *till merged*
instead of a time. Done is seen the moment the agent's status changes. A
merge is seen at the next tick: herdr has no event for another plugin's
label changing, so that's your next focus change, or within five minutes.
From the command line: `herdr-snooze snooze <pane> done` (or `merged`).

### The list

`herdr-snooze.manage` (*Snooze: every agent* in the command palette) opens
every agent in one list.

![The list on the demo's agents: one that just woke, with when and why; a snoozed space with its two agents under it; three agents snoozed on their own, with when each wakes; then the three that are awake](docs/images/list.png)

**Snoozed** comes first: each snoozed space with the agents it hides
under it, then the agents snoozed on their own, the soonest to wake first,
each with how long it has left and when it wakes. **Awake** is every other
agent, in herdr's order, with its state.

| Key | Does |
| --- | --- |
| `↑` `↓`, `j` `k` | move |
| `pgup` / `pgdn`, `home` / `end` | page, first / last |
| `enter` | go to that agent and close (on a space, its first agent) |
| `s` | snooze it: an awake agent, for as long as you choose; a snoozed agent or space, for a new time |
| `w` | wake a snoozed agent or space now |
| `S` | snooze that agent's whole space |
| `r` | open that agent in [Recap](#with-recap), when it's installed; `esc` there comes back here |
| `W` | wake everything |
| `esc` | close |

Waking and snoozing are separate keys, so neither does the other by
mistake. Snoozing or waking from the list keeps the list open, so you can go
down it.
An agent woken out of a snoozed space stays awake while the space sleeps on.

The keys along the bottom are pills, and buttons: the one under the pointer
underlines, and a click presses it. Hovering a row selects it, and the wheel
scrolls.

### Saved SSH machines

The popup includes enabled profiles from `herdr machine list --json` on the
machine running the plugin. Each profile targets its saved Herdr session;
profiles saved on those remote hosts are not followed recursively.
Remote rows include the machine label beside the workspace. `s`, `w`, `S`
and `W` work on them as they do locally. Wake all covers Local and the
successfully loaded machines shown in this popup, not unreachable hosts.

The owning server keeps its snooze records, timer and Agents-panel view.
Closing the popup or disconnecting the laptop does not stop remote wakeups.
Matching pane IDs on different machines are kept separate. A disabled,
removed or retargeted profile, changed server session, or replaced terminal
is rejected instead of falling back to Local.

Local agents load immediately while remote reads run in the background,
at most four at once. Remote snapshots refresh about every ten seconds.
An offline host or missing/older Snooze installation produces a message at
the bottom; it does not stop local snoozing. Nothing is installed, updated
or restarted automatically. A failed write is not retried, because it may
have reached the server before the connection broke. Refresh the list to
check the result before choosing the action again.

Remote navigation is separate: Herdr's public plugin API does not switch
the client's selected machine. Enter on a remote row explains which
machine to select in Herdr. Recap links are only shown for local rows,
so an identically numbered local agent cannot be opened by mistake.

Connections use Herdr's saved-machine CLI to find the remote plugin and its
actual socket, config and state paths. The request then goes to that
installed binary over noninteractive OpenSSH, as JSON on stdin. Paths are
shell-quoted; host-key checking stays strict. Normal SSH config and the
SSH agent apply, but this helper does not reuse Herdr's private managed
MFA connection. If authentication exists only in that connection, make
ordinary noninteractive SSH access available first; Snooze never opens an
authentication prompt or handles keys.

Set `"machines": false` in `config.json` to keep the popup local-only.
This also avoids saved-machine discovery on older Herdr versions.

### In the Agents panel

While anything is snoozed, the Agents panel label shows the badge and a count
after herdr's own `grouped` or `priority` label, such as `priority 💤 2`, and
the panel keeps that order. When another plugin labels the panel, its label
takes that place instead, such as `active 💤 2`.

herdr doesn't let its label be clicked to switch order while anything is
snoozed, so `herdr-snooze.toggle` steps through both orders on its way round
to the snoozed list: `priority`, `grouped`, snoozed only, and `priority` again.
`herdr-snooze.order` switches between the two orders alone, if you want that on
a key of its own. The switch lasts only until the last snooze ends: herdr keeps
its own setting, and a plugin can't change it. While it's switched away from
herdr's own, the label shows a `*`, such as `grouped* 💤 2`. While another
plugin orders the panel, or `"sort"` is set in `config.json`, the order is not
ours to switch: `order` does nothing, and the toggle goes straight between
that panel and the snoozed list.

On the snoozed side the label reads `💤 snoozed only · 2`, so the two can't
be confused. To wake one from there, focus it and press the snooze key again.

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

**Just woke.** An agent that wakes by itself (its time is up, or what it
was snoozed until happened) goes at the top of the list, under *Just woke*,
with when and why: `woke 16:32 · it's done`. `enter` goes to it and `s`
snoozes it again. It leaves that list once you go to it, snooze it again, or
after 4 hours. Meanwhile it wears a ⏰ where `"$snoozed"` shows its 💤,
so a row that shows one shows the other, with nothing to change:

```
  ● web-app  ⏰
    Fix the login bug
```

The ⏰ is also on its own in a `woke` token, for `"$woke"` somewhere else in
a row (next to `state_icon`, it reads as the agent's icon).
## With Recap

Snooze finds [herdr-recap](https://github.com/mrolafsson/herdr-recap) by
itself when it's installed and enabled: nothing to set up.

![Recap's Snoozed tab on its demo: one agent, snoozed until Tuesday 09:00, with ^w wake and ^s snooze along the bottom](docs/images/recap-snoozed.png)

- **In Recap**, snoozed agents have a tab of their own, *💤 Snoozed*, with
  when each wakes. `ctrl+s` on any agent opens Snooze's popup on it: the
  durations for an awake agent; for a snoozed one, the durations to change
  when it wakes, and `w` to wake it now. Once it's done, or on `esc`, you're
  back in Recap on the same agent. `ctrl+w` on a snoozed agent wakes it
  straight away, with no popup: Recap runs `herdr-snooze wake <pane>` from
  this plugin's folder.
- **In Snooze's list**, `r` opens the selected agent in Recap, to see where
  it got to; `esc` there brings the list back on the same agent.

herdr shows one popup at a time, so the popup that's open becomes the other
plugin's, in place (it keeps the size it was opened with). Without Recap the
key is left out.

The convention is small, and any plugin can use it. A popup is opened with
`HERDR_POPUP_RETURN` in its environment: the popup to open again when it's
closed, as JSON in the shape of herdr's `plugin.pane.open`
(`{"plugin_id", "entrypoint", "width", "height", "env"}`). Snooze's popup
(entrypoint `picker`) opens on an agent when `HERDR_SNOOZE_PANE` names its
pane, and on the list with `HERDR_SNOOZE_SCREEN=list`. Opened that way, it
goes back once it has snoozed or woken the agent, and on `esc`.

Every plugin can also read what Snooze puts on a snoozed pane: the
`herdr_snooze` token (only Snooze writes it; its presence means snoozed) and
the `snoozed` token, the badge and when it wakes (`💤  17:30`).

## How it behaves

**It hides; it does not pause or mute.** The pane keeps running and the agent
keeps working. herdr's notifications and the space's status rollup still fire:
the Agents panel is the only thing that changes (sidebar rows, mouse targets,
`focus_agent` numbering, next/previous agent).

**It comes back on time.** herdr itself expires the marker that hides the agent,
so the agent reappears at the deadline even if nothing of this plugin is
running. The label and the panel catch up within about a second.

**It comes back when it needs you.** A snoozed agent that gets stuck (waiting
on a permission, a question, an answer) wakes as soon as herdr says so,
whatever it was snoozed until. Only the change counts: an agent snoozed while
it was already waiting on you was snoozed knowing that, and stays snoozed
until it moves on and gets stuck again. Set `"wake_when_blocked": false` to
leave it hidden.

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
Durations are real elapsed time: `2h` is two hours even across a daylight-saving
change, while a clock time like `9am` stays 9am.

**A snooze belongs to the agent you snoozed.** If that agent exits and another
starts in the same pane, the new one isn't hidden (in a snoozed space it is:
the space covers whatever runs in it). The same agent keeps its snooze through
`/clear`, a resume or a compaction. Moving a pane to another space or tab keeps
its snooze. If the popup was left open while its pane moved, closed or got a
new agent, it says so instead of snoozing the wrong thing.

A new agent is recognised from what herdr reports: its `pane.agent_detected`
event, sent when an agent's process exits (so a quick restart of the same
agent is seen too; `/clear` and the like are not exits) and when a pane's agent
comes, goes or changes kind, or the pane shown without an agent in between.
herdr also shows a pane without its agent when the agent hands the terminal to
another program (an editor it opened, say) for more than a few seconds, and
reports an exit when the shell gets the terminal back (an agent suspended with
Ctrl-Z, say); that agent then loses its snooze and reappears. So does an agent
whose custom herdr integration reports a release while it keeps running.

A new agent of the same kind keeps the old snooze, until it ends or you wake
it, when snooze doesn't hear the old one leave: when the plugin misses that
event (it was disabled at that moment, or a burst of events outran herdr's
limit of 32 plugin commands at once) and the new agent arrives within ten
seconds of the snooze (an arrival that soon is taken for the snoozed agent's
own), or when something swaps one agent for another before herdr samples the
pane in between, so it never sees the first one exit.

One gap is left: a pane moved to another space and herdr stopped within the
same instant, before snooze's hook for the move ran, comes back after the
restart without its snooze, so it shows again.

**Sharing the Agents panel.** herdr has one agent view, and the last plugin to
set it wins. While something is snoozed, snooze keeps its view in place,
checking at least every five minutes, since herdr gives plugins no event when
another one takes it over. A plugin you switch to directly can therefore lose
the panel to snooze within a few minutes; see
[Alongside plugins that sort the Agents panel](#alongside-plugins-that-sort-the-agents-panel).

**The 💤 is not a button.** herdr offers plugins no hook for clicking the panel
label, and its right-click menus are built-in only (as of 0.9.1), so the panel
side of this is reached by key, palette or command line; the list is the
place to click.

## Configuration

Optional. Create `config.json` in the plugin's config directory:

```sh
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
| `woke_badge` | `"⏰"` | Shown in `$snoozed` (and `$woke`) on an agent that just woke by itself. Any text; `""` for none. |
| `notify` | `false` | Show a herdr notification when something is snoozed. |
| `auto_return` | `true` | Leave the snoozed list automatically, as described above. |
| `wake_when_blocked` | `true` | Wake a snoozed agent when it comes to need you. |
| `notify_wake` | `true` | Show a herdr notification when an agent wakes by itself (not when you wake it). |
| `theme` | `""` | `"dark"` or `"light"` to say what your terminal is, instead of asking it. See [Colours](#colours). |
| `machines` | `true` | Include enabled saved SSH machines in the popup; `false` keeps all reads and actions local. Remote hosts need this Snooze build and ordinary noninteractive SSH access. |
| `views` | — | Teach snooze another plugin's panel order. See below. |
| `sort` | — | Always use this `agent.view.set` sort while something is snoozed. |

Changes apply to the next action; no reload needed. A value of the wrong type
falls back to its default, with a note in `herdr plugin log list --plugin
herdr-snooze`, rather than breaking snooze. See
[`config.example.json`](config.example.json).

### Colours

Colours come from herdr's theme, drawn as in the other herdr plugins (one
shared file, `look.go`): the title in the theme's brightest text, where and
when dim, an agent as herdr's sidebar marks it (`◉` needs you, `◔` working,
`●` done, `✓` idle), and the keys as pills coloured by what they do: green
goes somewhere, peach changes something, red wakes everything, grey leaves.
What's snoozed, and when it wakes, is blue, like the 💤.

A light theme on a dark terminal (or the other way round) would put dark
text on dark, so then the popup keeps the plugins' own colours. herdr's
`terminal` theme is spread over your terminal's 16 colours, and a colour you
set yourself in `[theme.custom]` wins.

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

```sh
herdr plugin action invoke herdr-snooze.wake-all
herdr plugin uninstall herdr-snooze
```

Then, by hand:

- Remove the `[[keys.command]]` blocks that reference `herdr-snooze.*` from
  `config.toml`, and `"$snoozed"` from your sidebar rows if you added it,
  then `herdr server reload-config`.
- herdr leaves the plugin's config and state directories behind, and snooze's
  small session-ID file next to each herdr session's socket:

  ```sh
  rm -r ~/.config/herdr/plugins/config/herdr-snooze ~/.local/state/herdr/plugins/herdr-snooze
  rm -f ~/.config/herdr/.herdr-snooze-session ~/.config/herdr/sessions/*/.herdr-snooze-session*
  ```

  If you ran herdr on a socket of your own (`HERDR_SOCKET_PATH`), the file is
  next to that socket, named after it: for `/tmp/team.sock`, remove
  `/tmp/.herdr-snooze-session-team.sock`.

If you uninstall while agents are still snoozed, nothing breaks: herdr drops the
plugin's panel filter immediately, so every agent is visible again, and the
leftover `snoozed` tokens expire on their own within 24 hours. A plugin that
orders the panel may need its order re-applied (for herdr-radar, run its
`view-flip` action twice).

## Troubleshooting

Plugin output is captured rather than printed, so this is where errors surface:

```sh
herdr plugin log list --plugin herdr-snooze --limit 10
```

**The key does nothing.** Check the binding's `command` is
`herdr-snooze.<action>`, that you ran `herdr server reload-config`, and that
the client was started after that. `herdr plugin action invoke
herdr-snooze.agent` tests the action without the key.

**"This pane has no agent".** Snoozing hides agents; a shell has nothing to hide.

**An agent is hidden and I can't find it.** `herdr-snooze.manage` lists
everything that's snoozed; `herdr-snooze.wake-all` brings everything back.

**The panel lost its grouping or order.** Another plugin's view was replaced and
not restored: see *Alongside plugins that sort the Agents panel*. Waking
everything hands the view back.

**The 💤 or ⏰ shows as a box, or not at all.** They're emoji, and your
terminal draws them from an emoji font: without one (common on a minimal
server, or over a remote connection from a machine without one), you get
a box (□) or a blank, and with some fonts the rest of the row shifts by a
column. Install an emoji font (Noto Color Emoji, on Linux:
`fonts-noto-color-emoji`), or use plain text instead in `config.json`:

```json
{ "badge": "(-_-)zz", "woke_badge": "(O_O)" }
```

The popup, the toasts and the list say it in words anyway. Recap's tab
icons are its own, and fixed: see its README.

**The install failed.** Without Go, the build step downloads the release for
your platform from GitHub; with no network, or a platform other than Linux or
macOS on arm64 or x86-64, install [Go](https://go.dev/dl) and install again.

To see what snooze has recorded:

```sh
~/.config/herdr/plugins/herdr-snooze/bin/herdr-snooze list
```

(or `bin/herdr-snooze list` in a checkout).

## How it works

A snoozed pane carries two metadata tokens: `snoozed`, the badge text you can
show in the sidebar (`"$snoozed"`), and `herdr_snooze`, which only snooze
writes. The Agents panel view is filtered to `not(exists herdr_snooze)`, so
another plugin that happens to use a `snoozed` token can't hide or un-hide
anything. The tokens are reported with a TTL, so herdr drops them at the
deadline.

An agent on the *Just woke* list carries the ⏰ in `snoozed` (without
`herdr_snooze`: it's not hidden) and in `woke`, and `snooze_woke`, when and
why (`16:32 · it's done`), which Recap reads. They too have a TTL, of
what's left of the 4 hours.

A small state file, `snoozed.json` in the plugin's state directory, holds the
real deadlines, and which agent each one is for. `tick`, a startup hook plus
a few event hooks, reconciles what a TTL alone cannot: herdr caps a token's
TTL at 24h, tokens and the view do not survive a server restart, another plugin
can replace the view, and a pane that moves gets a new ID (it keeps its
terminal, which is how snooze finds it again). herdr runs hooks concurrently and
in bursts, so a tick that finds another one running leaves a note and exits, and
the running one goes again until no note is left. Every tick reads everything
from herdr, so it doesn't matter which hook's tick does the work.

The state file exists only while something is snoozed, and a hook that finds
none anywhere returns before doing anything else: with nothing snoozed, each
event costs one short process.

Between events, one detached `sleep` runs a tick at the next moment something
needs doing, and at least every five minutes while anything is snoozed. It
sleeps, ticks once and is gone; it is not a daemon. Its process ID is kept, so
if it dies (killed, or the machine slept) the next event arms a new one, and
one replaced by an earlier deadline is stopped. If its own tick fails (herdr
busy or restarting), it arms a retry five minutes out, for as long as herdr's
socket is there.

Each herdr session has its own state, in `sessions/<key>/` under the plugin's
state directory, since pane IDs only mean something inside one session. The key
comes from the session's socket and a random ID snooze keeps next to it
(`.herdr-snooze-session` in the session's directory; for a socket not named
`herdr.sock`, a file named after the socket). A server restart keeps that file
and so the session's snoozes. Deleting a named session deletes its directory
with the file, so a new session with the same name (whose panes herdr numbers
from `w1:p1` again) never picks up the old one's snoozes. State left by a
session that no longer exists is removed.

The state file is only removed once herdr has confirmed our view is gone: if
that last call fails, the file stays and the next event tries again, rather
than leaving the panel filtered with nothing left to undo it. A state file that
can't be read (corrupt, or edited by hand) is treated as "don't know", not
"nothing snoozed": the next event un-hides everything snooze may have hidden
and then removes the file.

Token names aren't owned in herdr, so snooze only ever clears a `snoozed` token
whose value it could have written (it starts with snooze's badge); another
plugin's token of the same name is left alone. With an empty `badge` in
config.json there's nothing to tell them apart by.

**Upgrading from 0.2**, the Python version: the state file and the session
keys are the same, so what's snoozed stays snoozed. Upgrading from 0.1.0,
which kept one file for every session, un-hides once in every running session
whatever 0.1.0 may have left hidden there; snooze those agents again. If a
session can't be reached, that's retried later, backing off from 30 seconds
up to an hour. Servers started with their own `HERDR_SOCKET_PATH` aren't in
herdr's session list and aren't reached: if agents stay hidden there, run
**Wake all snoozed agents** in that session.

## Development

```sh
go test -race ./...
go run . picker --demo       # the popup on fictional agents, in any terminal
herdr plugin link .          # try it in herdr
sh scripts/screenshots.sh    # docs/images from the demo (needs freeze)

go run . snooze w1:p1 30s    # also: workspace:w1
go run . toggle
go run . wake --all
go run . list
```

`SNOOZE_STATE_DIR` points the CLI at a scratch state directory, so experiments
don't touch real snoozes. `docs/images/recap-snoozed.png` is herdr-recap's:
its `scripts/screenshots.sh` writes it as `docs/images/snoozed.png`, copied
here. `themes_herdr.go` is generated from herdr's palettes
by herdr-recap's `scripts/gen-themes.py`, and `look.go` is the file the herdr
plugins share: change it in all four.

### Releasing

Bump `version` in `herdr-plugin.toml`, then tag it: `git tag v0.3.0 && git
push --tags`. The release workflow checks the tag matches the manifest, runs
the tests and publishes the binaries `scripts/build.sh` downloads when Go
isn't installed.

## License

[MIT](LICENSE)
