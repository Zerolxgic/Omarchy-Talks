import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Omarchy Talks — transient playback controls.
//
// Third-party Omarchy shell panel plugin (id omarchy-talks.controls). It is
// presentation only: the Omarchy Talks backend owns every session, and this
// panel renders the state the backend reports and hands control requests back
// to the stable `omarchy-talks` CLI. It never signals players, reads session
// files, captures the selection, or talks to VoiceBox.
//
// Backend → UI (protocol 1), delivered by omarchy-shell:
//   shell call   omarchy-talks.controls update '<payload>'   → update(payloadJson)
//   shell summon omarchy-talks.controls '<payload>'          → open(payloadJson)
//   shell hide   omarchy-talks.controls                      → close()
//
//   {"protocol":1,"kind":"session","event":"started"|"state",
//    "session_id":"opaque","state":"preparing|playing|paused|finished|stopped|error",
//    "message":null|"concise text"}
//   {"protocol":1,"kind":"notice","level":"info","message":"No text selected"}
//
// UI → backend: `omarchy-talks toggle-pause`, `omarchy-talks stop`, and
// `omarchy-talks status --json` for recovery after a plugin or shell reload.
//
// Visual language borrows from the first-party OSD (compact bottom-centred
// popup surface, keepLoaded lifecycle) and notifications (full transparent
// layer whose input region is only the visible card). Controls are the shared
// PanelActionButton. Every colour, radius, border, spacing and font comes from
// the shell's Color / Style / Border singletons, so the panel follows the
// active Omarchy theme and carries no palette of its own.
Item {
  id: root

  // Injected by omarchy-shell when the panel loads.
  property var shell: null
  property var manifest: null
  property string omarchyPath: ""

  // Host contract: true while the panel is showing (shell.isPluginOpen).
  property bool opened: false

  readonly property string pluginId: "omarchy-talks.controls"
  readonly property var sessionStates: ["preparing", "playing", "paused", "finished", "stopped", "error"]
  readonly property var activeStates: ["preparing", "playing", "paused"]

  // Minimal mirror of the backend's authoritative session. `sessionId` is the
  // current UI owner; it changes only through a `started` event (or recovery
  // from `status --json`). `sessionState` is "" while nothing is shown.
  property string sessionId: ""
  property string sessionState: ""
  property string sessionMessage: ""

  // Notice channel. Independent of the session: it never touches sessionId,
  // sessionState, or the controls.
  property string noticeText: ""

  // Bumped on every applied payload so a status reply that raced with newer
  // events is discarded rather than rolling the UI back.
  property int payloadSerial: 0
  property int statusSerial: -1

  readonly property bool sessionActive: activeStates.indexOf(sessionState) !== -1
  readonly property bool sessionVisible: sessionState !== ""
  readonly property bool noticeVisible: noticeText !== ""
  readonly property bool preparing: sessionState === "preparing"
  readonly property bool playing: sessionState === "playing"
  readonly property bool paused: sessionState === "paused"
  readonly property bool finished: sessionState === "finished"
  readonly property bool errored: sessionState === "error"
  readonly property bool canPause: playing
  readonly property bool canResume: paused
  readonly property bool canStop: sessionActive

  // Disappearance timing (ms). Finished lingers just long enough to read as
  // completion; stopped disappears at once; an error stays readable.
  readonly property int finishedLinger: 700
  readonly property int stoppedLinger: 0
  readonly property int errorLinger: 4000
  readonly property int noticeLinger: 2000

  // Geometry from shell tokens. The bottom offset is the one the first-party
  // OSD uses, so the card sits in the same peripheral slot as volume and
  // brightness feedback.
  readonly property int pad: Style.spacing.xxl
  readonly property int gap: Style.spacing.controlGap
  readonly property int bottomOffset: Style.space(67)
  readonly property int rowHeight: pauseButton.implicitHeight
  readonly property int indicatorWidth: Style.space(26)
  readonly property int maxTextWidth: Style.space(220)

  readonly property string pauseGlyph: "\u{F03E4}"   // nf-md-pause
  readonly property string playGlyph: "\u{F040A}"    // nf-md-play
  readonly property string stopGlyph: "\u{F04DB}"    // nf-md-stop

  readonly property string visibleText: errored && sessionMessage !== ""
    ? sessionMessage
    : (noticeVisible ? noticeText : "")
  readonly property bool showIndicator: sessionVisible && !errored
  // An error card carries only its message: the session is over, so the
  // controls would only be dimmed clutter next to it.
  readonly property bool showControls: sessionVisible && !errored
  readonly property int textWidth: visibleText === "" ? 0 : Math.min(Math.ceil(textMetrics.advanceWidth), root.maxTextWidth)
  readonly property int contentWidth: {
    var parts = []
    if (showControls) parts.push(pauseButton.implicitWidth)
    if (showIndicator) parts.push(indicatorWidth)
    if (textWidth > 0) parts.push(textWidth)
    if (showControls) parts.push(stopButton.implicitWidth)
    var total = 0
    for (var i = 0; i < parts.length; i++) total += parts[i]
    return total + Math.max(0, parts.length - 1) * gap
  }

  // ------------------------------------------------------------ host API

  function open(payloadJson) { return applyPayload(payloadJson) }
  function update(payloadJson) { return applyPayload(payloadJson) }

  function close() {
    lingerTimer.stop()
    noticeTimer.stop()
    sessionState = ""
    sessionMessage = ""
    noticeText = ""
    opened = false
  }

  // Inspection surface for tests: `omarchy-shell shell call omarchy-talks.controls state ""`.
  function state() {
    return JSON.stringify({
      opened: opened,
      session_id: sessionId,
      state: sessionState === "" ? "idle" : sessionState,
      message: sessionMessage,
      notice: noticeText,
      can_pause: canPause,
      can_resume: canResume,
      can_stop: canStop,
      control_running: controlProc.running,
      status_running: statusProc.running
    })
  }

  function ping() { return "ok" }

  // ------------------------------------------------------------ payloads

  function applyPayload(payloadJson) {
    var payload
    try { payload = JSON.parse(String(payloadJson || "{}")) } catch (e) { return "invalid" }
    if (!payload || typeof payload !== "object") return "invalid"
    // A bare summon carries "{}": resync from the backend instead of guessing.
    if (payload.kind === undefined) { refreshStatus(); return }
    if (payload.protocol !== undefined && Number(payload.protocol) !== 1) return "unsupported-protocol"
    if (payload.kind === "notice") return applyNotice(payload)
    if (payload.kind === "session") return applySession(payload)
    return "invalid"
  }

  function applyNotice(payload) {
    var message = String(payload.message || "").trim()
    if (message === "") return "invalid"
    payloadSerial++
    noticeText = message
    opened = true
    noticeTimer.restart()
  }

  function applySession(payload) {
    var id = String(payload.session_id || "")
    var next = String(payload.state || "")
    var event = String(payload.event || "")
    if (id === "" || sessionStates.indexOf(next) === -1) return "invalid"
    if (event === "started") {
      payloadSerial++
      adoptSession(id, next, payload.message)
      return
    }
    if (event !== "state") return "invalid"
    if (sessionId === "") {
      // Nothing is current, so nothing newer can be displaced. Late terminal
      // events for a session we never saw have nothing to show, except an
      // error, which is worth surfacing.
      if (activeStates.indexOf(next) === -1 && next !== "error") return
      payloadSerial++
      adoptSession(id, next, payload.message)
      return
    }
    if (id !== sessionId) {
      // Only `started` may replace the current owner. Anything else from a
      // different session is stale and must not disturb the current one.
      console.log("omarchy-talks.controls: ignoring stale", next, "for", id, "current", sessionId)
      return
    }
    payloadSerial++
    setSessionState(next, payload.message)
  }

  function adoptSession(id, next, message) {
    sessionId = id
    setSessionState(next, message)
  }

  function setSessionState(next, message) {
    lingerTimer.stop()
    sessionState = next
    sessionMessage = message === undefined || message === null ? "" : String(message).trim()
    if (activeStates.indexOf(next) !== -1) {
      opened = true
      return
    }
    var linger = next === "finished" ? finishedLinger : (next === "error" ? errorLinger : stoppedLinger)
    if (linger <= 0) {
      hideSession()
      return
    }
    opened = true
    lingerTimer.interval = linger
    lingerTimer.restart()
  }

  function hideSession() {
    lingerTimer.stop()
    sessionState = ""
    sessionMessage = ""
    if (!noticeVisible) opened = false
  }

  // ------------------------------------------------------------ recovery

  function refreshStatus() {
    if (statusProc.running) return
    statusSerial = payloadSerial
    statusProc.running = true
  }

  function applyStatus(text) {
    if (statusSerial !== payloadSerial) return   // newer events arrived meanwhile
    var status
    try { status = JSON.parse(String(text || "").trim()) } catch (e) {
      console.warn("omarchy-talks.controls: unreadable status:", text)
      return
    }
    if (!status || typeof status !== "object") return
    if (status.active === true && sessionStates.indexOf(String(status.state)) !== -1) {
      adoptSession(String(status.session_id || ""), String(status.state), status.message)
      return
    }
    if (sessionVisible && sessionActive) hideSession()
  }

  // ------------------------------------------------------------ controls

  function runControl(action) {
    if (action !== "toggle-pause" && action !== "stop") return "invalid"
    if (controlProc.running) return "busy"
    controlProc.action = action
    controlProc.command = ["omarchy-talks", action]
    controlProc.running = true
  }

  Process {
    id: controlProc
    property string action: ""
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode, exitStatus) {
      if (exitCode !== 0)
        console.warn("omarchy-talks.controls:", controlProc.action, "exited", exitCode, String(controlProc.stderr.text || "").trim())
      root.refreshStatus()
    }
  }

  Process {
    id: statusProc
    command: ["omarchy-talks", "status", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(text)
    }
  }

  Timer {
    id: lingerTimer
    interval: root.finishedLinger
    onTriggered: root.hideSession()
  }

  Timer {
    id: noticeTimer
    interval: root.noticeLinger
    onTriggered: {
      root.noticeText = ""
      if (!root.sessionVisible) root.opened = false
    }
  }

  TextMetrics {
    id: textMetrics
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    text: root.visibleText
  }

  Component.onCompleted: root.refreshStatus()

  // ------------------------------------------------------------ surface

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-talks-controls"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    // Only the card accepts pointer input; the rest of the transparent layer
    // stays click-through, as the notification popups do.
    mask: Region { item: card }

    BorderSurface {
      id: card
      width: card.borderLeft + root.pad + root.contentWidth + root.pad + card.borderRight
      height: card.borderTop + root.pad + root.rowHeight + root.pad + card.borderBottom
      anchors.horizontalCenter: parent.horizontalCenter
      anchors.bottom: parent.bottom
      anchors.bottomMargin: root.bottomOffset
      color: Color.popups.background
      borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
      radius: Style.cornerRadius

      Row {
        id: content
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.leftMargin: card.borderLeft + root.pad
        anchors.topMargin: card.borderTop + root.pad
        height: root.rowHeight
        spacing: root.gap

        PanelActionButton {
          id: pauseButton
          visible: root.showControls
          anchors.verticalCenter: parent.verticalCenter
          iconText: root.paused ? root.playGlyph : root.pauseGlyph
          tooltipText: root.paused ? "Resume" : (root.playing ? "Pause" : "Pause (waiting for audio)")
          enabled: root.canPause || root.canResume
          foreground: Color.popups.text
          bordered: true
          onClicked: root.runControl("toggle-pause")
        }

        // Activity indicator. Truthful by construction: it shows that the
        // reader is preparing, speaking, or paused, never a completion
        // percentage, because the backend exposes no playback position.
        Item {
          id: indicator
          visible: root.showIndicator
          width: root.indicatorWidth
          height: parent.height

          readonly property int barWidth: Style.space(3)
          readonly property int barGap: Style.space(3)
          readonly property int minBar: Style.space(4)
          readonly property int maxBar: Math.round(root.rowHeight * 0.7)

          Row {
            anchors.centerIn: parent
            spacing: indicator.barGap

            Repeater {
              model: 3
              delegate: Rectangle {
                id: bar
                required property int index
                // Preparing pulses this rather than `opacity` itself so the
                // paused/normal opacity binding survives the animation.
                property real pulse: 1
                width: indicator.barWidth
                radius: width / 2
                anchors.verticalCenter: parent.verticalCenter
                height: indicator.minBar
                color: root.paused ? Color.popups.text : Color.accent
                opacity: root.paused ? 0.45 : pulse

                // Speaking: staggered rise and fall. Pausing freezes the bars
                // where they are; any other stop settles them.
                SequentialAnimation on height {
                  running: root.playing && root.opened
                  loops: Animation.Infinite
                  PauseAnimation { duration: bar.index * 110 }
                  NumberAnimation { to: indicator.maxBar; duration: 260; easing.type: Easing.InOutSine }
                  NumberAnimation { to: indicator.minBar; duration: 320; easing.type: Easing.InOutSine }
                  PauseAnimation { duration: (2 - bar.index) * 110 }
                  onRunningChanged: if (!running && !root.paused) bar.height = indicator.minBar
                }

                // Preparing: a soft, sequential pulse with no motion in height.
                SequentialAnimation on pulse {
                  running: root.preparing && root.opened
                  loops: Animation.Infinite
                  PauseAnimation { duration: bar.index * 160 }
                  NumberAnimation { to: 0.25; duration: 320; easing.type: Easing.InOutSine }
                  NumberAnimation { to: 1; duration: 320; easing.type: Easing.InOutSine }
                  PauseAnimation { duration: (2 - bar.index) * 160 }
                  onRunningChanged: if (!running) bar.pulse = 1
                }
              }
            }
          }
        }

        Text {
          id: messageText
          textFormat: Text.PlainText
          visible: root.textWidth > 0
          width: root.textWidth
          anchors.verticalCenter: parent.verticalCenter
          text: root.visibleText
          font: textMetrics.font
          color: root.errored && root.sessionMessage !== "" ? Color.urgent : Color.popups.text
          elide: Text.ElideRight
          maximumLineCount: 1
        }

        PanelActionButton {
          id: stopButton
          visible: root.showControls
          anchors.verticalCenter: parent.verticalCenter
          iconText: root.stopGlyph
          tooltipText: "Stop"
          enabled: root.canStop
          foreground: Color.popups.text
          bordered: true
          onClicked: root.runControl("stop")
        }
      }
    }
  }
}
