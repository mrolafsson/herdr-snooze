# Changelog

## 0.5.2 - 2026-10-20

- **herdr-vitals joins the plugins that work together.** The README's
  table has it: how your machines are doing, and in Recap's header, the
  machine's CPU and memory, a click from Vitals.

## 0.5.1 - 2026-10-13

- **herdr-file-transfer joins the plugins that work together.** The README's
  table has it: files between the machine you're at and the server, and the
  files a new Linear issue attaches.

## 0.5.0 - 2026-10-13

- **A toast when an agent wakes by itself**: *fix flaky tests is awake*,
  and why (*It's done*, *It needs you*, *Its pull request is merged*). A
  space, or several agents at once, is one toast. Wakes you ask for aren't
  announced. `"notify_wake": false` turns it off.
- **Just woke.** An agent that woke by itself is at the top of the list,
  with when and why (`woke 16:32 · it's done`), until you go to it, snooze
  it again, or 4 hours have passed. Meanwhile it wears a ⏰ in the Agents
  panel, where `"$snoozed"` shows a 💤 (or put `"$woke"` anywhere in a
  row; `"woke_badge"` changes it). Recap 0.8 gives it a tab, *⏰ Just woke*.

## 0.4.0 - 2026-10-13

- **Snooze until something happens.** Below Custom, the popup offers
  *until done* (`d`) for an agent that's working, and *until #482 is merged*
  (`m`) for one with a pull request open (from herdr-github's labels). Each
  is capped at 9:00 the next morning: if it hasn't happened by then, the
  agent's back anyway. The badge, the list and Recap say *till done* or
  *till merged*. From the command line, `snooze <pane> done` or `merged`.
- **A snoozed agent that comes to need you wakes by itself**, whatever it
  was snoozed until. An agent snoozed while it was already waiting on you
  stays snoozed until it moves on and gets stuck again.
  `"wake_when_blocked": false` keeps it hidden.

## 0.3.0 - 2026-10-12

Rewritten in Go, to look and work like the other herdr plugins, and to work
with them.

- **One list of every agent** (`herdr-snooze.manage`): what's snoozed, each
  snoozed space with the agents it hides under it, and when each wakes; then
  every agent that's awake. `s` snoozes one (or a snoozed one for a new
  time), `w` wakes one, `S` snoozes its whole space, `enter` goes to it, all
  without leaving the list.
- **Works with Recap.** Snoozed agents have a tab of their own there.
  `ctrl+s` opens this popup on the agent you're on; once it's snoozed, or on
  `esc`, you're back in Recap. `ctrl+w` wakes a snoozed one, no popup. From the list, `r` opens an agent in Recap.
  Any plugin can open the popup on an agent with `HERDR_SNOOZE_PANE`, and
  have it come back with `HERDR_POPUP_RETURN`.
- **Follows herdr's theme**, drawn as the other plugins draw: an agent's
  state as the sidebar marks it, the keys as pills along the bottom. Works
  with the mouse.
- **A snoozed agent's own popup can wake it** (`w`) or give it another time,
  instead of waking it first and snoozing it again.
- **A time you type is read as you type it**: *tomorrow 2pm* says *until Wed
  14:00 · 23h 55m* before you press enter.
- **A demo** (`Snooze: demo (fictional agents)`), safe to try and to
  screenshot.
- **No Python needed.** The build step compiles it with Go, or downloads the
  release binary for your platform and checks its SHA-256. With nothing
  snoozed, a hook is one short process.

What's snoozed stays snoozed across the upgrade: the state file and the
session keys are the same. The popup's environment variables are now
`HERDR_SNOOZE_*`. After upgrading a linked checkout, run `herdr plugin link`
again so herdr reads the new manifest.

## 0.2.5

- The toggle steps through priority, grouped and the snoozed list.

## 0.2.4

- herdr's priority/grouped label and order are kept while anything is
  snoozed.

## 0.2.3

- A picker left open on another client is replaced instead of failing with
  `ui_busy`.

## 0.2.0 – 0.2.2

- Durations are elapsed time; snooze keeps a marker token of its own; a
  snooze belongs to the agent it was for; moved panes keep their snooze;
  bursts of hooks coalesce; a sleeper ticks at the next deadline and at
  least every five minutes; config.json is validated.
