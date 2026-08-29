import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  property bool opened: false
  property bool busy: false
  property string query: ""
  property string answer: ""
  property string errorText: ""
  property string agentName: ""
  property bool settingsOpen: false
  property bool settingsBusy: false
  property string settingsStatus: ""
  property string configuredModel: ""
  property string configuredReasoning: ""
  property int askSeq: 0
  property int lastExitCode: 0

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string askScript: {
    var url = Qt.resolvedUrl("ask.sh").toString()
    if (url.indexOf("file://") === 0)
      return decodeURIComponent(url.slice(7))
    return url
  }
  readonly property string workDir: home + "/Work"

  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color border: Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  property color scrim: Color.menu.scrim
  readonly property int cornerRadius: Style.cornerRadius
  property string fontFamily: Style.font.menuFamily
  property int contentMargin: Style.spacing.panelPadding
  property int headerHeight: Math.max(Style.space(44), Style.font.title + Style.spacing.controlPaddingY * 2)
  property int contentSpacing: Style.spacing.md
  property int cardWidth: Math.min(Style.space(720), panel.width - Style.gapsOut * 2)
  readonly property bool showingResult: root.busy || root.answer !== "" || root.errorText !== ""
  readonly property bool showingDetail: root.settingsOpen || root.showingResult
  property int cardHeight: {
    var compact = contentMargin * 2 + headerHeight + Style.font.caption + Style.spacing.sm
    if (!root.showingDetail)
      return Math.min(compact, panel.height - Style.gapsOut * 2)
    return Math.min(Style.space(520), panel.height - Style.gapsOut * 2)
  }
  readonly property string hint: root.agentName
    ? ("Enter to ask · Esc to close · " + root.agentName + " · "
      + (root.configuredModel || "agent default"))
    : "Enter to ask · Esc to close · default Omarchy agent"

  function open(payloadJson) {
    root.opened = true
    root.settingsOpen = false
    if (!agentProc.running)
      agentProc.running = true
    Qt.callLater(function() {
      if (queryField)
        queryField.forceActiveFocus()
    })
  }

  function close() {
    root.opened = false
  }

  function dismiss() {
    root.opened = false
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "damianpoole.ask")
  }

  function toggle() {
    if (root.opened)
      root.dismiss()
    else
      root.open("{}")
  }

  function cleanText(text) {
    return String(text || "").replace(/\x1B\[[0-9;]*[A-Za-z]/g, "").trim()
  }

  function applySettings(text) {
    try {
      var parsed = JSON.parse(String(text || "{}"))
      root.configuredModel = String(parsed.model || "")
      root.configuredReasoning = String(parsed.reasoning || "")
      if (modelField)
        modelField.text = root.configuredModel
      root.settingsStatus = ""
    } catch (error) {
      root.settingsStatus = "Could not read settings: " + error
    }
  }

  function loadSettings() {
    if (!root.agentName || settingsLoadProc.running)
      return
    settingsLoadProc.command = ["bash", root.askScript, "--get-settings", root.agentName]
    settingsLoadProc.running = true
  }

  function showSettings() {
    root.settingsOpen = true
    root.settingsStatus = ""
    root.loadSettings()
    Qt.callLater(function() { if (modelField) modelField.forceActiveFocus() })
  }

  function hideSettings() {
    root.settingsOpen = false
    root.settingsStatus = ""
    Qt.callLater(function() { if (queryField) queryField.forceActiveFocus() })
  }

  function saveSettings(useAgentDefault) {
    if (!root.agentName || settingsSaveProc.running)
      return
    var model = useAgentDefault ? "" : String(modelField.text || "").trim()
    var reasoning = useAgentDefault ? "" : root.configuredReasoning
    root.settingsBusy = true
    root.settingsStatus = "Saving…"
    settingsSaveProc.command = [
      "bash", root.askScript, "--set-settings",
      root.agentName, model, reasoning
    ]
    settingsSaveProc.running = true
  }

  function submit() {
    var prompt = (queryField ? queryField.text : root.query).trim()
    if (!prompt)
      return
    if (root.busy && prompt === root.query)
      return

    root.query = prompt
    root.answer = ""
    root.errorText = ""
    root.busy = true
    root.askSeq += 1
    var seq = root.askSeq

    askProc.workingDirectory = root.workDir
    askProc.command = ["bash", root.askScript, prompt]
    if (askProc.running)
      askProc.running = false

    Qt.callLater(function() {
      if (seq !== root.askSeq)
        return
      askProc.running = true
    })
  }

  function applyFinished(exitCode, stdoutText, stderrText) {
    root.busy = false
    var fromOut = root.cleanText(stdoutText)
    var fromErr = root.cleanText(stderrText)
    if (exitCode === 0 && fromOut) {
      root.answer = fromOut
      root.errorText = ""
      return
    }
    root.answer = fromOut
    root.errorText = fromErr || ("Agent exited " + exitCode)
  }

  function copyAnswer() {
    var text = root.answer
    if (!text)
      return
    Quickshell.execDetached(["bash", "-c", "printf %s " + Util.shellQuote(text) + " | wl-copy"])
  }

  Process {
    id: agentProc
    running: false
    command: ["omarchy-default-agent"]
    stdinEnabled: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.agentName = String(text || "").trim()
        root.loadSettings()
      }
    }
  }

  Process {
    id: settingsLoadProc
    running: false
    command: []
    stdinEnabled: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applySettings(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = root.cleanText(text)
        if (message) root.settingsStatus = message
      }
    }
  }

  Process {
    id: settingsSaveProc
    running: false
    command: []
    stdinEnabled: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.applySettings(text)
        root.settingsBusy = false
        root.settingsStatus = "Saved for " + root.agentName
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = root.cleanText(text)
        if (message) {
          root.settingsBusy = false
          root.settingsStatus = message
        }
      }
    }
  }

  Process {
    id: askProc
    running: false
    command: []
    stdinEnabled: false

    stdout: StdioCollector {
      id: askStdout
      waitForEnd: true
      onStreamFinished: root.applyFinished(root.lastExitCode, text, askStderr.text)
    }
    stderr: StdioCollector {
      id: askStderr
      waitForEnd: true
    }

    onExited: function(exitCode, exitStatus) {
      root.lastExitCode = exitCode
      finishFallback.restart()
    }
  }

  Timer {
    id: finishFallback
    interval: 400
    repeat: false
    onTriggered: {
      if (!root.busy)
        return
      root.applyFinished(root.lastExitCode, askStdout.text, askStderr.text)
    }
  }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-ask"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: root.cardHeight
      radius: root.cornerRadius
      anchors.horizontalCenter: parent.horizontalCenter
      y: Math.round(panel.height * 0.12)
      color: root.background
      borderSpec: root.borderSpec
      padding: root.contentMargin

      MouseArea {
        anchors.fill: parent
        onClicked: {
          if (root.settingsOpen && modelField)
            modelField.forceActiveFocus()
          else if (queryField)
            queryField.forceActiveFocus()
        }
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: card.contentTopInset
        anchors.rightMargin: card.contentRightInset
        anchors.bottomMargin: card.contentBottomInset
        anchors.leftMargin: card.contentLeftInset
        spacing: root.contentSpacing

        RowLayout {
          Layout.fillWidth: true
          Layout.minimumHeight: root.headerHeight
          Layout.preferredHeight: root.headerHeight
          Layout.maximumHeight: root.headerHeight
          spacing: Style.spacing.sm

          TextField {
            id: queryField
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !root.settingsOpen
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            foreground: root.foreground
            placeholderText: "Ask a question…"
            onAccepted: root.submit()
            Keys.onReturnPressed: function(event) { root.submit(); event.accepted = true }
            Keys.onEnterPressed: function(event) { root.submit(); event.accepted = true }

            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              } else if (event.key === Qt.Key_Comma && (event.modifiers & Qt.ControlModifier)) {
                root.showSettings()
                event.accepted = true
              } else if (event.key === Qt.Key_C && (event.modifiers & Qt.ControlModifier) && !(event.modifiers & Qt.ShiftModifier)) {
                if (!queryField.selectedText && root.answer) {
                  root.copyAnswer()
                  event.accepted = true
                }
              }
            }
          }

          Text {
            Layout.fillWidth: true
            visible: root.settingsOpen
            text: "Quick Ask settings"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            verticalAlignment: Text.AlignVCenter
          }

          Button {
            Layout.preferredHeight: root.headerHeight
            text: root.settingsOpen ? "Back" : "Settings"
            foreground: root.foreground
            bordered: true
            focusable: true
            onClicked: root.settingsOpen ? root.hideSettings() : root.showSettings()
          }
        }

        ColumnLayout {
          Layout.fillWidth: true
          Layout.fillHeight: true
          visible: root.settingsOpen
          spacing: Style.spacing.md

          Text {
            Layout.fillWidth: true
            text: root.agentName ? ("Default Omarchy agent: " + root.agentName) : "Detecting default agent…"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          Text {
            Layout.fillWidth: true
            text: "Model"
            color: root.foreground
            opacity: 0.72
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          TextField {
            id: modelField
            Layout.fillWidth: true
            foreground: root.foreground
            placeholderText: "Agent default (leave blank)"
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                root.hideSettings()
                event.accepted = true
              }
            }
          }

          Text {
            Layout.fillWidth: true
            text: "Leave blank to inherit the agent's own configured model. Custom model IDs are passed directly to the agent CLI."
            wrapMode: Text.Wrap
            color: root.foreground
            opacity: 0.5
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Button {
            visible: root.agentName === "codex"
            text: "Use Luna / low"
            foreground: root.foreground
            bordered: true
            onClicked: {
              modelField.text = "gpt-5.6-luna"
              root.configuredReasoning = "low"
            }
          }

          Text {
            Layout.fillWidth: true
            visible: root.agentName === "codex"
            text: "Reasoning"
            color: root.foreground
            opacity: 0.72
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          RowLayout {
            Layout.fillWidth: true
            visible: root.agentName === "codex"
            spacing: Style.spacing.xs

            Repeater {
              model: [
                { label: "Default", value: "" },
                { label: "Minimal", value: "minimal" },
                { label: "Low", value: "low" },
                { label: "Medium", value: "medium" },
                { label: "High", value: "high" }
              ]

              Button {
                required property var modelData
                text: modelData.label
                foreground: root.foreground
                bordered: true
                selected: root.configuredReasoning === modelData.value
                onClicked: root.configuredReasoning = modelData.value
              }
            }
          }

          Text {
            Layout.fillWidth: true
            visible: root.settingsStatus !== ""
            text: root.settingsStatus
            color: root.settingsStatus.indexOf("Saved") === 0 ? root.foreground : Color.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Item { Layout.fillHeight: true }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.sm

            Button {
              text: "Use agent default"
              foreground: root.foreground
              bordered: true
              enabled: !root.settingsBusy && root.agentName !== ""
              onClicked: root.saveSettings(true)
            }

            Item { Layout.fillWidth: true }

            Button {
              text: root.settingsBusy ? "Saving…" : "Save"
              foreground: root.foreground
              bordered: true
              enabled: !root.settingsBusy && root.agentName !== ""
              onClicked: root.saveSettings(false)
            }
          }
        }

        Text {
          Layout.fillWidth: true
          visible: !root.settingsOpen && !root.showingResult
          text: root.hint
          color: root.foreground
          opacity: 0.5
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Text {
          Layout.fillWidth: true
          visible: !root.settingsOpen && root.busy
          text: root.agentName ? ("Asking " + root.agentName + "…") : "Asking…"
          color: root.foreground
          opacity: 0.72
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Flickable {
          id: answerScroll
          Layout.fillWidth: true
          Layout.fillHeight: true
          visible: !root.settingsOpen && root.showingResult
          clip: true
          contentWidth: width
          contentHeight: answerColumn.implicitHeight
          boundsBehavior: Flickable.StopAtBounds
          interactive: contentHeight > height

          Column {
            id: answerColumn
            width: answerScroll.width
            spacing: Style.spacing.sm

            Text {
              width: parent.width
              visible: root.errorText !== ""
              text: root.errorText
              wrapMode: Text.Wrap
              color: Color.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Text {
              width: parent.width
              visible: root.answer !== ""
              text: root.answer
              textFormat: Text.MarkdownText
              wrapMode: Text.Wrap
              color: root.foreground
              linkColor: Color.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              onLinkActivated: function(link) { Qt.openUrlExternally(link) }
            }

            Text {
              width: parent.width
              visible: root.answer !== "" && !root.busy
              text: "Ctrl+C copies the answer"
              color: root.foreground
              opacity: 0.5
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }
}
