# omarchy-talks.settings

Voice settings for Omarchy Talks, packaged as a third-party Omarchy shell
(Quickshell) panel plugin. It is a small summoned flyout, separate from the
transient playback card (`omarchy-talks.controls`), which it neither imports
nor affects.

The panel is presentation only. The backend owns voice discovery, VoiceBox
profiles, and persistence; this plugin reads and writes that state exclusively
through the stable `omarchy-talks` CLI. It never calls VoiceBox, never touches
the config file, and never signals players. A failure here cannot affect
speech.

## Layout

```text
plugins/omarchy-talks.settings/
├── manifest.json   # id omarchy-talks.settings, kinds ["panel"], no keepLoaded
├── Settings.qml    # the panel
└── README.md
```

The directory is self-contained and can be extracted into its own repository.

## Opening it

From the Omarchy menu: **Setup → Omarchy Talks**, or open the menu and type
`talks`. No terminal is involved. The row is installed as a user menu
extension in `~/.config/omarchy/extensions/omarchy-menu.jsonc`:

```jsonc
"setup.omarchy-talks": {"icon":"󰔊","label":"Omarchy Talks","description":"Choose the reader voice","action":"omarchy-shell shell summon omarchy-talks.settings"},
```

Equivalent direct calls:

```bash
omarchy-shell shell summon omarchy-talks.settings
omarchy menu summon setup.omarchy-talks
```

## Backend contract consumed

| Purpose | Command |
| --- | --- |
| Voice catalogue | `omarchy-talks voices --json` |
| Change voice | `omarchy-talks voice set <voice_id> --json` |
| Note that a change lands on the next read | `omarchy-talks status --json` |

`voices --json` returns `{"engine":"kokoro","voices":[{voice_id, name,
language, gender, active}]}`. `voice_id` is the stable identifier used for
selection; `name` is what the row displays. The panel shows no profile IDs,
config paths, or other VoiceBox internals.

The manifest deliberately omits `keepLoaded`, so every summon mounts a fresh
instance that loads the catalogue from the backend. There is no remembered UI
selection to go stale.

## Behaviour

- **Selection is backend-confirmed.** Clicking or pressing Enter runs
  `voice set` and then reloads the catalogue; the check mark moves only when
  the backend reports the new voice as active.
- **Failure keeps the previous selection.** The CLI's own message is shown in
  the card and the user can simply pick again.
- **A playing read is never disturbed.** If a read is active when the voice
  changes, the card says `Applies to the next read`.
- **States:** loading, empty, no filter match, unavailable, and error each
  render in place of or below the list.

## Interaction

| Input | Effect |
| --- | --- |
| Type any character | Filter by name, id, language, or gender |
| Backspace | Edit the filter |
| Escape | Clear the filter, or close when there is none |
| Up / Down, PageUp / PageDown, Home / End | Move the cursor (first press lands on the current voice) |
| Enter / Space | Select the voice under the cursor |
| Click a row | Select that voice |
| Click outside the card | Close |

The panel takes keyboard focus only while it is open, and takes none when
closed.

## Theme

No colour, size, radius, border, or font is defined here. The panel uses
`Color.menu.*` (the shell's role for summoned centred cards), `Color.urgent`
for errors, `Style.spacing.*`, `Style.space()`, `Style.font.*`,
`Style.cornerRadius`, `Style.gapsOut`, and `Border.surfaceSpec("menu", …)`,
together with the shared `BorderSurface`, `PanelSeparator`, and
`PanelSectionHeader` components.

## Development install

```bash
ln -sfn "$PWD/plugins/omarchy-talks.settings" ~/.config/omarchy/plugins/omarchy-talks.settings
omarchy-shell shell rescanPlugins
omarchy-plugin-enable omarchy-talks.settings
```

Validate the repository path, not the linked path
(`omarchy-plugin-validate` rejects symlinks):

```bash
omarchy-plugin-validate plugins/omarchy-talks.settings
```

The shell's file watcher does not follow the symlink, so after editing
`Settings.qml` run `omarchy-shell shell rescanPlugins`.

Inspection helpers:

```bash
omarchy-shell shell call omarchy-talks.settings ping ""
omarchy-shell shell call omarchy-talks.settings state ""
journalctl --user -t omarchy-shell -f
```

## Disable / uninstall

```bash
omarchy-plugin-disable omarchy-talks.settings
rm ~/.config/omarchy/plugins/omarchy-talks.settings
omarchy-shell shell rescanPlugins
```

Then remove the `setup.omarchy-talks` entry from
`~/.config/omarchy/extensions/omarchy-menu.jsonc`. Nothing under
`$OMARCHY_PATH` is touched, and speech works with or without this plugin.
