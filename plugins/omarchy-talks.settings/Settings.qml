import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Omarchy Talks — voice settings.
//
// Third-party Omarchy shell panel plugin (id omarchy-talks.settings). It is a
// small, summoned settings flyout, entirely separate from the transient
// playback card (omarchy-talks.controls), which it neither imports nor
// affects.
//
// Presentation only. The Omarchy Talks backend owns voice discovery,
// VoiceBox profiles, and persistence; this panel reads and writes that state
// exclusively through the stable CLI:
//
//   omarchy-talks voices --json        → {"engine":"kokoro","voices":[…]}
//                                        each: voice_id, name, language,
//                                        gender, active
//   omarchy-talks voice set <voice_id> --json
//   omarchy-talks status --json        → only to tell the user a change lands
//                                        on the next read while one is playing
//
// It never calls VoiceBox, reads or writes the config file, touches session
// state, or signals players. A failure here cannot affect speech.
//
// Open with:  omarchy-shell shell summon omarchy-talks.settings
//
// Visual language follows the first-party menu: a centred card over a scrim,
// Color.menu.* roles, type-to-filter, j/k cursor, Escape to close. The panel
// is not keepLoaded, so every summon mounts a fresh instance that loads the
// catalogue from the backend rather than showing remembered UI state.
Item {
  id: root

  // Injected by omarchy-shell when the panel loads.
  property var shell: null
  property var manifest: null
  property string omarchyPath: ""

  // Host contract: true while the panel is showing (shell.isPluginOpen).
  property bool opened: false

  readonly property string pluginId: "omarchy-talks.settings"

  // ---- backend-confirmed state -------------------------------------------
  //
  // `voices` is whatever the last successful catalogue load returned, and the
  // selected voice is the one the backend marks active in it. Clicking a row
  // never writes here; only a reload after a completed command does. That is
  // what keeps the check mark showing backend truth rather than intent.
  property var voices: []
  property bool loaded: false
  property bool loading: false
  property string loadError: ""

  // Selection in flight: the voice_id being applied, plus the last failure.
  property string pendingVoiceId: ""
  property string setError: ""
  // Shown only when a read was already playing as the voice changed.
  property bool nextReadNote: false

  // ---- view state ---------------------------------------------------------
  property string filterText: ""
  property int selectedIndex: 0
  property bool cursorActive: false

  readonly property bool busy: pendingVoiceId !== ""
  readonly property string activeVoiceId: {
    for (var i = 0; i < voices.length; i++)
      if (voices[i].active === true) return String(voices[i].voice_id)
    return ""
  }
  readonly property string activeVoiceName: {
    for (var i = 0; i < voices.length; i++)
      if (voices[i].active === true) return String(voices[i].name || voices[i].voice_id)
    return ""
  }

  readonly property var filteredVoices: {
    var needle = filterText.toLowerCase()
    if (needle === "") return voices
    var out = []
    for (var i = 0; i < voices.length; i++) {
      var voice = voices[i]
      var haystack = (String(voice.name || "") + " " + String(voice.voice_id || "") + " " +
                      String(voice.language || "") + " " + String(voice.gender || "")).toLowerCase()
      if (haystack.indexOf(needle) !== -1) out.push(voice)
    }
    return out
  }

  readonly property string statusText: {
    if (busy) return "Selecting " + displayNameFor(pendingVoiceId) + "…"
    if (setError !== "") return setError
    if (loadError !== "") return loadError
    if (loading && !loaded) return "Loading voices…"
    if (nextReadNote) return "Applies to the next read"
    return ""
  }
  readonly property bool statusIsError: !busy && (setError !== "" || loadError !== "")

  // ---- theme --------------------------------------------------------------
  //
  // Menu surface roles: this is a summoned centred card, which is exactly what
  // [menu] in the theme describes. Nothing here defines a colour or size of
  // its own.
  readonly property string fontFamily: Style.font.menuFamily
  readonly property int cornerRadius: Style.cornerRadius
  readonly property int contentMargin: Style.spacing.panelPadding
  readonly property int rowHeight: Math.max(Style.space(38), Style.font.body + Style.spacing.rowPaddingX)
  readonly property int cardWidth: Math.min(Style.space(360), panel.width - Style.gapsOut * 2)
  readonly property int listHeight: Math.min(rowHeight * 8 + Style.spacing.xs * 7,
                                             Math.max(rowHeight, filteredVoices.length * (rowHeight + Style.spacing.xs) - Style.spacing.xs))

  readonly property string checkGlyph: "\u{F012C}"   // nf-md-check

  function displayNameFor(voiceId) {
    for (var i = 0; i < voices.length; i++)
      if (String(voices[i].voice_id) === String(voiceId))
        return String(voices[i].name || voices[i].voice_id)
    return String(voiceId)
  }

  function voiceDetail(voice) {
    if (!voice) return ""
    var parts = []
    if (voice.gender) parts.push(String(voice.gender))
    if (voice.language) parts.push(String(voice.language))
    var suffix = parts.length ? "  ·  " + parts.join(", ") : ""
    return String(voice.voice_id || "") + suffix
  }

  // ---- host API -----------------------------------------------------------

  function open(payloadJson) {
    root.opened = true
    root.filterText = ""
    root.setError = ""
    root.nextReadNote = false
    root.cursorActive = false
    refresh()
    // The window is instantiated hidden, so a content-level `focus: true` is
    // evaluated before the surface maps and Escape would land nowhere.
    Qt.callLater(function() { if (root.opened) keyCatcher.forceActiveFocus() })
  }

  // Host-initiated close (shell hide). The host already knows.
  function close() {
    root.opened = false
    root.filterText = ""
    root.cursorActive = false
    root.nextReadNote = false
  }

  // User-initiated close (Escape, scrim click). Tell the shell so its
  // openPanelIds map stays consistent and the next summon works.
  function dismiss() {
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
    else close()
  }

  // Inspection surface for tests:
  //   omarchy-shell shell call omarchy-talks.settings state ""
  function state() {
    return JSON.stringify({
      opened: opened,
      loaded: loaded,
      loading: loading,
      voice_count: voices.length,
      active_voice_id: activeVoiceId,
      active_voice_name: activeVoiceName,
      filter: filterText,
      filtered_count: filteredVoices.length,
      cursor_active: cursorActive,
      selected_index: selectedIndex,
      selected_voice_id: selectedIndex >= 0 && selectedIndex < filteredVoices.length
        ? String(filteredVoices[selectedIndex].voice_id) : "",
      pending_voice_id: pendingVoiceId,
      set_error: setError,
      load_error: loadError,
      next_read_note: nextReadNote,
      status_text: statusText
    })
  }

  function ping() { return "ok" }

  // ---- backend ------------------------------------------------------------

  function refresh() {
    if (catalogProc.running) return
    root.loading = true
    root.loadError = ""
    catalogProc.running = true
  }

  function applyCatalog(text) {
    root.loading = false
    var payload
    try { payload = JSON.parse(String(text || "").trim()) } catch (e) {
      root.loadError = "Could not read the voice list"
      return
    }
    if (!payload || !Array.isArray(payload.voices)) {
      root.loadError = "Could not read the voice list"
      return
    }
    var list = []
    for (var i = 0; i < payload.voices.length; i++) {
      var voice = payload.voices[i]
      if (voice && typeof voice.voice_id === "string" && voice.voice_id !== "")
        list.push(voice)
    }
    root.voices = list
    root.loaded = true
    root.loadError = list.length === 0 ? "No voices available" : ""
    clampCursor()
  }

  // Selecting a voice: the row is confirmed only by the reload that follows
  // the command. A failure keeps the previous confirmed selection and says
  // why, and the user can simply pick again.
  function selectVoice(voiceId) {
    var id = String(voiceId || "")
    if (id === "" || root.busy || root.loading) return
    if (id === root.activeVoiceId) return
    var known = false
    for (var i = 0; i < voices.length; i++)
      if (String(voices[i].voice_id) === id) { known = true; break }
    if (!known) return
    root.setError = ""
    root.nextReadNote = false
    root.pendingVoiceId = id
    setProc.command = ["omarchy-talks", "voice", "set", id, "--json"]
    setProc.running = true
  }

  function finishSelection(exitCode) {
    var failed = exitCode !== 0
    var message = String(setProc.stderr.text || "").trim()
    root.pendingVoiceId = ""
    if (failed) {
      // The CLI's own message is the actionable one; keep it as written and
      // strip only its program prefix.
      root.setError = message.replace(/^omarchy-talks:\s*/, "") || "Could not change the voice"
    } else {
      // A read that is already playing keeps the voice it started with, so
      // say so rather than letting the user wonder why nothing changed.
      statusProc.running = true
    }
    refresh()
  }

  function noteIfReadActive(text) {
    var status
    try { status = JSON.parse(String(text || "").trim()) } catch (e) { return }
    root.nextReadNote = !!(status && status.active === true)
  }

  Process {
    id: catalogProc
    command: ["omarchy-talks", "voices", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyCatalog(text)
    }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      root.loading = false
      if (exitCode !== 0) {
        root.loaded = root.voices.length > 0
        var message = String(catalogProc.stderr.text || "").trim()
        root.loadError = message.replace(/^omarchy-talks:\s*/, "") || "Voice list unavailable"
      }
    }
  }

  Process {
    id: setProc
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) { root.finishSelection(exitCode) }
  }

  Process {
    id: statusProc
    command: ["omarchy-talks", "status", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.noteIfReadActive(text)
    }
  }

  // ---- cursor -------------------------------------------------------------

  function clampCursor() {
    var count = filteredVoices.length
    if (count === 0) { selectedIndex = 0; return }
    if (selectedIndex >= count) selectedIndex = count - 1
    if (selectedIndex < 0) selectedIndex = 0
  }

  function moveCursor(delta) {
    var count = filteredVoices.length
    if (count === 0) return
    if (!cursorActive) {
      // First keypress lands on the current voice rather than the top of the
      // list, so a change is one or two steps from where the user already is.
      cursorActive = true
      selectedIndex = indexOfActive()
      ensureVisible()
      return
    }
    selectedIndex = Math.max(0, Math.min(count - 1, selectedIndex + delta))
    ensureVisible()
  }

  function indexOfActive() {
    for (var i = 0; i < filteredVoices.length; i++)
      if (String(filteredVoices[i].voice_id) === root.activeVoiceId) return i
    return 0
  }

  function ensureVisible() {
    if (selectedIndex >= 0 && selectedIndex < filteredVoices.length)
      voiceList.positionViewAtIndex(selectedIndex, ListView.Contain)
  }

  function activateCursor() {
    if (!cursorActive || selectedIndex < 0 || selectedIndex >= filteredVoices.length) return
    selectVoice(filteredVoices[selectedIndex].voice_id)
  }

  function setFilter(text) {
    filterText = String(text || "")
    cursorActive = filterText !== ""
    selectedIndex = filterText === "" ? indexOfActive() : 0
    ensureVisible()
  }

  onFilteredVoicesChanged: clampCursor()

  // ---- surface ------------------------------------------------------------

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-talks-settings"
    WlrLayershell.layer: WlrLayer.Overlay
    // A deliberate, focused interaction the user summoned, like the menu:
    // it owns the keyboard while open and releases it on close.
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: Color.menu.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: Math.min(content.implicitHeight + root.contentMargin * 2 + card.borderTop + card.borderBottom,
                       panel.height - Style.gapsOut * 2)
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.verticalCenter: parent.verticalCenter
      color: Color.menu.background
      borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
      radius: root.cornerRadius

      // Swallow clicks so only the scrim dismisses.
      MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }

      Item {
        id: keyCatcher
        anchors.fill: parent
        focus: true

        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) {
            // Escape clears a filter first, then closes — the menu's habit.
            if (root.filterText !== "") root.setFilter("")
            else root.dismiss()
            event.accepted = true
          } else if (event.key === Qt.Key_Down) {
            root.moveCursor(1); event.accepted = true
          } else if (event.key === Qt.Key_Up) {
            root.moveCursor(-1); event.accepted = true
          } else if (event.key === Qt.Key_PageDown) {
            root.moveCursor(6); event.accepted = true
          } else if (event.key === Qt.Key_PageUp) {
            root.moveCursor(-6); event.accepted = true
          } else if (event.key === Qt.Key_Home) {
            root.cursorActive = true; root.selectedIndex = 0; root.ensureVisible(); event.accepted = true
          } else if (event.key === Qt.Key_End) {
            root.cursorActive = true; root.selectedIndex = root.filteredVoices.length - 1
            root.ensureVisible(); event.accepted = true
          } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
            if (root.cursorActive) root.activateCursor()
            else { root.cursorActive = true; root.selectedIndex = root.indexOfActive(); root.ensureVisible() }
            event.accepted = true
          } else if (event.key === Qt.Key_Backspace) {
            if (root.filterText !== "") root.setFilter(root.filterText.slice(0, -1))
            event.accepted = true
          } else if (event.text && event.text.length === 1 && event.text.charCodeAt(0) >= 32 &&
                     event.text.charCodeAt(0) !== 127 &&
                     (event.modifiers === Qt.NoModifier || event.modifiers === Qt.ShiftModifier)) {
            root.setFilter(root.filterText + event.text)
            event.accepted = true
          }
        }

        Column {
          id: content
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.top: parent.top
          anchors.leftMargin: card.borderLeft + root.contentMargin
          anchors.rightMargin: card.borderRight + root.contentMargin
          anchors.topMargin: card.borderTop + root.contentMargin
          spacing: Style.spacing.md

          // Header: what this is, and what is selected right now.
          Item {
            width: parent.width
            height: Math.max(titleText.implicitHeight, filterLabel.implicitHeight)

            Text {
              id: titleText
              textFormat: Text.PlainText
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "Omarchy Talks"
              color: Color.menu.text
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              id: filterLabel
              textFormat: Text.PlainText
              anchors.right: parent.right
              anchors.left: titleText.right
              anchors.leftMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              horizontalAlignment: Text.AlignRight
              text: root.filterText !== "" ? root.filterText
                : (root.activeVoiceName !== "" ? root.activeVoiceName : "")
              color: root.filterText !== "" ? Color.menu.selectedText : Color.menu.text
              opacity: root.filterText !== "" ? 1 : 0.55
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }
          }

          PanelSeparator { foreground: Color.menu.text }

          PanelSectionHeader {
            text: "VOICE"
            foreground: Color.menu.text
            fontFamily: root.fontFamily
          }

          // Voice list. One row per voice from the backend catalogue; the
          // check mark and the bold label both mark the confirmed selection,
          // so it never depends on colour alone.
          ListView {
            id: voiceList
            width: parent.width
            height: root.listHeight
            visible: root.filteredVoices.length > 0
            model: root.filteredVoices
            clip: true
            spacing: Style.spacing.xs
            boundsBehavior: Flickable.StopAtBounds
            currentIndex: root.cursorActive ? root.selectedIndex : -1

            delegate: BorderSurface {
              id: voiceRow
              required property int index
              required property var modelData

              readonly property string voiceId: String(modelData.voice_id || "")
              readonly property bool isActive: voiceId !== "" && voiceId === root.activeVoiceId
              readonly property bool isPending: voiceId !== "" && voiceId === root.pendingVoiceId
              readonly property bool hasCursor: root.cursorActive && index === root.selectedIndex

              width: ListView.view.width
              height: root.rowHeight
              radius: root.cornerRadius
              color: hasCursor ? Color.menu.selectedBackground : "transparent"
              borderSpec: hasCursor
                ? Border.surfaceSpec("menu", "selected-border", Color.menu.selectedBorder, 0)
                : Border.none()
              opacity: root.busy && !isPending ? 0.5 : 1

              Behavior on opacity { NumberAnimation { duration: 100 } }

              Text {
                id: mark
                textFormat: Text.PlainText
                anchors.left: parent.left
                anchors.leftMargin: Style.space(10)
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(20)
                horizontalAlignment: Text.AlignHCenter
                text: voiceRow.isActive ? root.checkGlyph : ""
                color: voiceRow.hasCursor ? Color.menu.selectedText : Color.menu.text
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }

              Text {
                id: nameText
                textFormat: Text.PlainText
                anchors.left: mark.right
                anchors.leftMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                width: Math.max(0, detail.x - x - Style.space(8))
                text: String(voiceRow.modelData.name || voiceRow.voiceId)
                color: voiceRow.hasCursor ? Color.menu.selectedText : Color.menu.text
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: voiceRow.isActive
                elide: Text.ElideRight
              }

              Text {
                id: detail
                textFormat: Text.PlainText
                anchors.right: parent.right
                anchors.rightMargin: Style.space(10)
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(implicitWidth, parent.width * 0.5)
                horizontalAlignment: Text.AlignRight
                text: root.voiceDetail(voiceRow.modelData)
                color: voiceRow.hasCursor ? Color.menu.selectedText : Color.menu.text
                opacity: 0.5
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }

              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: root.busy ? Qt.ArrowCursor : Qt.PointingHandCursor
                onEntered: {
                  root.cursorActive = true
                  root.selectedIndex = voiceRow.index
                }
                onClicked: root.selectVoice(voiceRow.voiceId)
              }
            }
          }

          // Empty states carry the same weight as the list they replace, so
          // the card does not jump when the filter matches nothing.
          Item {
            width: parent.width
            height: root.rowHeight
            visible: root.filteredVoices.length === 0

            Text {
              textFormat: Text.PlainText
              anchors.left: parent.left
              anchors.leftMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              text: root.loading && !root.loaded ? "Loading voices…"
                : (root.filterText !== "" ? "No voice matches " + root.filterText
                                          : "No voices available")
              color: Color.menu.text
              opacity: 0.6
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }
          }

          // One status line, only when it says something: applying a voice,
          // a failure to keep, or the note that a playing read keeps its own.
          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.statusText !== ""
            text: root.statusText
            color: root.statusIsError ? Color.urgent : Color.menu.text
            opacity: root.statusIsError ? 1 : 0.6
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }
        }
      }
    }
  }
}
