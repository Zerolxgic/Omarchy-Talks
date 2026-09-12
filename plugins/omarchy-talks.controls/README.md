# omarchy-talks.controls

Transient, theme-native playback controls for Omarchy Talks, packaged as a
third-party Omarchy shell (Quickshell) panel plugin.

The panel is presentation only. The `omarchy-talks` backend owns every
session; this plugin renders what the backend reports and sends control
requests back through the stable CLI. It never signals players, reads session
files, captures the selection, or calls VoiceBox. Speech works unchanged when
the plugin is absent, disabled, or broken.

## Layout

```text
plugins/omarchy-talks.controls/
├── manifest.json   # id omarchy-talks.controls, kinds ["panel"], keepLoaded: true
├── Controls.qml    # the panel
└── README.md
```

The directory is self-contained and can be extracted into its own repository
(`omarchy plugin add <git-url>` expects `manifest.json` at the repository root).

## Contract

Backend → UI, delivered by omarchy-shell (protocol 1):

| Transport | Plugin entry point |
| --- | --- |
| `omarchy-shell shell call omarchy-talks.controls update '<json>'` | `update(payloadJson)` |
| `omarchy-shell shell summon omarchy-talks.controls '<json>'` | `open(payloadJson)` |
| `omarchy-shell shell hide omarchy-talks.controls` | `close()` |

Payloads are the session events (`started` / `state` with `session_id`,
`state`, optional `message`) and notices (`kind: "notice"`) defined in the
Slice 5 contract. A bare summon with `{}` makes the panel resync from
`omarchy-talks status --json`.

UI → backend: `omarchy-talks toggle-pause`, `omarchy-talks stop`, and
`omarchy-talks status --json` (recovery on plugin load or reload).

State mapping:

| Backend state | Panel |
| --- | --- |
| idle | hidden |
| preparing | pulsing indicator, Pause disabled, Stop enabled |
| playing | animated indicator, Pause + Stop enabled |
| paused | frozen indicator, Resume + Stop enabled |
| finished | controls disabled, hides after 700 ms |
| stopped | hides immediately |
| error | message only, hides after 4 s |
| notice | text next to the indicator (or alone when idle), clears after 2 s |

Session identity: only a `started` event (or a recovery status) changes the
current session. State events for any other session are ignored, so a late
`finished`/`stopped`/`error` from a replaced read cannot hide the current one.
Notices never touch the session.

## Development install

The shell discovers third-party plugins in `~/.config/omarchy/plugins/<id>/`
and enables non-bar plugins through `plugins[]` in `~/.config/omarchy/shell.json`.

```bash
ln -sfn "$PWD/plugins/omarchy-talks.controls" ~/.config/omarchy/plugins/omarchy-talks.controls
omarchy-shell shell rescanPlugins
omarchy-plugin-enable omarchy-talks.controls
```

`omarchy-plugin-validate plugins/omarchy-talks.controls` checks the manifest
(run it against the repository path: the validator rejects symlinks, so the
linked path itself will not validate).

The shell's file watcher does not follow the symlink, so after editing
`Controls.qml` run `omarchy-shell shell rescanPlugins` to reload it (this
reloads every plugin, which takes a few seconds; the panel then recovers any
active read from `status --json`).

Inspection helpers:

```bash
omarchy-shell shell call omarchy-talks.controls ping ""
omarchy-shell shell call omarchy-talks.controls state ""
journalctl --user -t omarchy-shell -f
```

## Disable / uninstall

```bash
omarchy-plugin-disable omarchy-talks.controls        # removes it from shell.json plugins[]
rm ~/.config/omarchy/plugins/omarchy-talks.controls   # removes the dev symlink only
omarchy-shell shell rescanPlugins
```

Nothing under `$OMARCHY_PATH` is touched. The backend keeps working without
the panel; its IPC publisher simply logs `summon: unknown plugin` in the shell
journal.

## Theme

No colour, size, radius, border, or font is defined here. The panel uses
`Color.popups.*`, `Color.accent`, `Color.urgent`, `Style.spacing.*`,
`Style.space()`, `Style.font.*`, `Style.cornerRadius`, and
`Border.surfaceSpec("popups", ...)`, together with the shared `BorderSurface`
and `PanelActionButton` components, so it follows whatever theme the shell is
running. It sits bottom-centre with the same offset as the first-party OSD.
