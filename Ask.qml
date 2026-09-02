pragma ComponentBehavior: Bound

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
  property var messages: []
  property string errorText: ""
  property string agentName: ""
  property string agentStatus: "Detecting default agent…"
  property string pendingRequest: ""
  property string processErrorBuffer: ""
  property string pendingExternalUrl: ""
  property string pendingExternalHost: ""
  property bool receivedAskRecord: false
  property bool discardAskResult: false
  property int lastExitCode: 0

  readonly property int maxUserCharacters: 4000
  readonly property int maxContextCharacters: 24000
  readonly property int maxAssistantCharacters: 131072
  readonly property int maxTranscriptCharacters: 192000
  readonly property int maxErrorCharacters: 4096
  readonly property int maxProtocolCharacters: 530000
  readonly property int maxLinkCharacters: 2048

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string helperScript: {
    var url = Qt.resolvedUrl("quick_ask_helper.py").toString()
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
  readonly property bool showingResult: root.busy || root.messages.length > 0 || root.errorText !== ""
  property int cardHeight: {
    var compact = contentMargin * 2 + headerHeight + Style.font.caption + Style.spacing.sm
    if (!root.showingResult)
      return Math.min(compact, panel.height - Style.gapsOut * 2)
    return Math.min(Style.space(520), panel.height - Style.gapsOut * 2)
  }
  readonly property string hint: root.agentName
    ? ("Enter to ask · Ctrl+N new · Esc to close · " + root.agentName + " default settings")
    : root.agentStatus

  function boundedText(value, maximum) {
    var text = String(value || "")
    if (text.length <= maximum)
      return text
    var suffix = "\n\n[Output truncated by Quick Ask]"
    return text.slice(0, Math.max(0, maximum - suffix.length)) + suffix
  }

  function cleanPlainText(value, maximum) {
    return root.boundedText(String(value || "")
      .replace(/\x1B(?:\[[0-?]*[ -\/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))/g, "")
      .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "")
      .trim(), maximum)
  }

  function sanitizeMarkdown(value) {
    var text = root.cleanPlainText(value, root.maxAssistantCharacters)
    // Qt Markdown can render embedded HTML and images. Quick Ask intentionally
    // supports only text formatting and links, whose activation is gated below.
    text = text.replace(/&/g, "&amp;")
    text = text.replace(/</g, "&lt;").replace(/>/g, "&gt;")
    text = text.replace(/!\[/g, "&#33;[")
    return text
  }

  function open(payloadJson) {
    root.opened = true
    root.refreshAgent()
    Qt.callLater(function() {
      if (queryField)
        queryField.forceActiveFocus()
    })
  }

  function close() {
    if (root.busy) {
      root.discardAskResult = true
      root.cancelAsk("Request cancelled when Quick Ask closed.")
    }
    root.pendingExternalUrl = ""
    root.pendingExternalHost = ""
    root.pendingRequest = ""
    root.messages = []
    root.errorText = ""
    root.opened = false
  }

  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "damianpoole.ask")
  }

  function toggle() {
    if (root.opened)
      root.dismiss()
    else
      root.open("{}")
  }

  function scrollToLatest() {
    Qt.callLater(function() {
      if (answerScroll)
        answerScroll.contentY = Math.max(0, answerScroll.contentHeight - answerScroll.height)
    })
  }

  function appendMessage(role, content) {
    var next = root.messages.slice()
    next.push({ role: String(role), content: String(content) })

    var retained = []
    var used = 0
    for (var i = next.length - 1; i >= 0; i--) {
      var size = String(next[i].content || "").length
      if (retained.length > 0 && used + size > root.maxTranscriptCharacters)
        break
      retained.unshift(next[i])
      used += size
    }
    root.messages = retained
    root.scrollToLatest()
  }

  function buildAgentPrompt(latestMessage) {
    if (root.messages.length === 0)
      return latestMessage

    var context = []
    var used = latestMessage.length
    for (var i = root.messages.length - 1; i >= 0; i--) {
      var message = root.messages[i]
      var size = String(message.content || "").length + 64
      if (context.length > 0 && used + size > root.maxContextCharacters)
        break
      context.unshift({ role: message.role, content: message.content })
      used += size
    }

    return [
      "Continue the Quick Ask conversation below.",
      "Use the history as context and answer only the latest user message.",
      "Conversation history (JSON):\n" + JSON.stringify(context),
      "Latest user message:\n" + latestMessage
    ].join("\n\n")
  }

  function startNewConversation() {
    if (root.busy)
      return
    root.messages = []
    root.errorText = ""
    root.pendingExternalUrl = ""
    root.pendingExternalHost = ""
    if (queryField)
      queryField.text = ""
    Qt.callLater(function() { if (queryField) queryField.forceActiveFocus() })
  }

  function refreshAgent() {
    if (agentProc.running)
      return
    root.agentStatus = "Detecting default agent…"
    agentProc.running = true
  }

  function applyAgentRecord(data) {
    try {
      var parsed = JSON.parse(String(data || "{}"))
      if (parsed.ok && parsed.agent) {
        root.agentName = root.cleanPlainText(parsed.agent, 32)
        root.agentStatus = ""
      } else {
        root.agentName = ""
        root.agentStatus = root.cleanPlainText(parsed.error, root.maxErrorCharacters)
      }
    } catch (error) {
      root.agentName = ""
      root.agentStatus = "Could not read the default-agent response."
    }
  }

  function submit() {
    var userMessage = (queryField ? queryField.text : "").trim()
    if (!userMessage || root.busy || askProc.running)
      return

    var prompt = root.buildAgentPrompt(userMessage)
    root.pendingRequest = JSON.stringify({ prompt: prompt })
    prompt = ""
    root.errorText = ""
    root.processErrorBuffer = ""
    root.pendingExternalUrl = ""
    root.pendingExternalHost = ""
    root.receivedAskRecord = false
    root.discardAskResult = false
    root.busy = true
    queryField.text = ""
    root.appendMessage("user", userMessage)
    userMessage = ""
    askProc.running = true
    askWatchdog.restart()
  }

  function applyAskRecord(data) {
    if (String(data || "").length > root.maxProtocolCharacters) {
      root.cancelAsk("Agent response exceeded the Quick Ask protocol limit.")
      return
    }
    if (root.discardAskResult) {
      root.receivedAskRecord = true
      root.discardAskResult = false
      root.busy = false
      askWatchdog.stop()
      forceKill.stop()
      return
    }
    try {
      var parsed = JSON.parse(String(data || "{}"))
      root.receivedAskRecord = true
      root.busy = false
      askWatchdog.stop()
      forceKill.stop()
      if (parsed.ok && parsed.answer) {
        root.agentName = root.cleanPlainText(parsed.agent, 32)
        root.errorText = ""
        root.appendMessage("assistant", root.sanitizeMarkdown(parsed.answer))
        Qt.callLater(function() { if (queryField) queryField.forceActiveFocus() })
      } else {
        root.errorText = root.cleanPlainText(parsed.error, root.maxErrorCharacters)
          || "The agent request failed."
        root.scrollToLatest()
      }
    } catch (error) {
      root.receivedAskRecord = true
      root.busy = false
      root.errorText = "Could not read the bounded agent response."
      root.scrollToLatest()
    }
  }

  function appendProcessError(data) {
    if (root.processErrorBuffer.length >= root.maxErrorCharacters)
      return
    root.processErrorBuffer = root.cleanPlainText(
      root.processErrorBuffer + String(data || ""), root.maxErrorCharacters)
  }

  function cancelAsk(message) {
    if (!root.busy)
      return
    root.errorText = root.cleanPlainText(message, root.maxErrorCharacters)
    if (askProc.running) {
      askProc.signal(15)
      forceKill.restart()
    } else {
      root.busy = false
    }
  }

  function latestAnswer() {
    for (var i = root.messages.length - 1; i >= 0; i--) {
      if (root.messages[i].role === "assistant")
        return String(root.messages[i].content || "")
    }
    return ""
  }

  function copyAnswer() {
    var answer = root.latestAnswer()
    if (answer)
      Quickshell.clipboardText = answer
  }

  function requestOpenLink(link) {
    var candidate = String(link || "")
    if (!candidate || candidate.length > root.maxLinkCharacters
        || /[\u0000-\u0020\u007F]/.test(candidate)) {
      root.errorText = "Blocked an invalid or overlong external link."
      return
    }
    var match = candidate.match(/^(https?):\/\/([^\/?#]+)(?:[\/?#].*)?$/i)
    if (!match || match[2].indexOf("@") >= 0) {
      root.errorText = "Quick Ask only opens credential-free HTTP(S) links."
      return
    }
    var authority = match[2]
    var validAuthority = /^\[[0-9a-f:.]+\](?::[0-9]{1,5})?$/i.test(authority)
      || /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$/i.test(authority)
    if (!validAuthority || authority.indexOf("..") >= 0) {
      root.errorText = "Blocked a malformed external link authority."
      return
    }
    var port = authority.match(/:(\d+)$/)
    if (port && Number(port[1]) > 65535) {
      root.errorText = "Blocked an external link with an invalid port."
      return
    }
    root.pendingExternalUrl = candidate
    root.pendingExternalHost = authority
    root.scrollToLatest()
  }

  function confirmOpenLink() {
    var candidate = root.pendingExternalUrl
    root.pendingExternalUrl = ""
    root.pendingExternalHost = ""
    if (candidate)
      Qt.openUrlExternally(candidate)
  }

  Process {
    id: agentProc
    running: false
    command: ["python3", root.helperScript, "detect"]
    stdinEnabled: false

    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function(data) {
        if (String(data || "").length <= root.maxErrorCharacters)
          root.applyAgentRecord(data)
        else
          root.agentStatus = "Default-agent response exceeded its limit."
      }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) {
        if (!root.agentName)
          root.agentStatus = root.cleanPlainText(data, root.maxErrorCharacters)
      }
    }
  }

  Process {
    id: askProc
    running: false
    command: ["python3", root.helperScript, "ask"]
    workingDirectory: root.workDir
    stdinEnabled: true

    stdout: SplitParser {
      splitMarker: "\n"
      onRead: function(data) { root.applyAskRecord(data) }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(data) { root.appendProcessError(data) }
    }

    onStarted: {
      var request = root.pendingRequest
      root.pendingRequest = ""
      askProc.write(request + "\n")
      request = ""
    }

    onExited: function(exitCode) {
      root.lastExitCode = exitCode
      askWatchdog.stop()
      forceKill.stop()
      finishFallback.restart()
    }
  }

  Timer {
    id: askWatchdog
    interval: 125000
    repeat: false
    onTriggered: root.cancelAsk("Agent exceeded the Quick Ask deadline.")
  }

  Timer {
    id: forceKill
    interval: 2500
    repeat: false
    onTriggered: {
      if (askProc.running)
        askProc.signal(9)
    }
  }

  Timer {
    id: finishFallback
    interval: 250
    repeat: false
    onTriggered: {
      if (!root.busy || root.receivedAskRecord)
        return
      root.busy = false
      if (root.discardAskResult) {
        root.discardAskResult = false
        root.processErrorBuffer = ""
        return
      }
      root.errorText = root.processErrorBuffer
        || (root.lastExitCode === 0 ? "Agent returned no response." : "Agent bridge exited " + root.lastExitCode + ".")
      root.scrollToLatest()
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
        onClicked: if (queryField) queryField.forceActiveFocus()
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
            maximumLength: root.maxUserCharacters
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            foreground: root.foreground
            placeholderText: root.messages.length > 0 ? "Reply…" : "Ask a question…"
            onAccepted: root.submit()
            Keys.onReturnPressed: function(event) { root.submit(); event.accepted = true }
            Keys.onEnterPressed: function(event) { root.submit(); event.accepted = true }

            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              } else if (event.key === Qt.Key_N && (event.modifiers & Qt.ControlModifier)) {
                root.startNewConversation()
                event.accepted = true
              } else if (event.key === Qt.Key_C && (event.modifiers & Qt.ControlModifier)
                         && !(event.modifiers & Qt.ShiftModifier)) {
                if (!queryField.selectedText && root.latestAnswer()) {
                  root.copyAnswer()
                  event.accepted = true
                }
              }
            }
          }

          Button {
            Layout.preferredHeight: root.headerHeight
            text: "New"
            foreground: root.foreground
            bordered: true
            focusable: true
            enabled: !root.busy && root.messages.length > 0
            onClicked: root.startNewConversation()
          }
        }

        Text {
          Layout.fillWidth: true
          visible: !root.showingResult
          textFormat: Text.PlainText
          text: root.hint
          color: root.foreground
          opacity: 0.5
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }

        Flickable {
          id: answerScroll
          Layout.fillWidth: true
          Layout.fillHeight: true
          visible: root.showingResult
          clip: true
          contentWidth: width
          contentHeight: answerColumn.implicitHeight
          boundsBehavior: Flickable.StopAtBounds
          interactive: contentHeight > height

          Column {
            id: answerColumn
            width: answerScroll.width
            spacing: Style.spacing.md

            Repeater {
              model: root.messages

              Item {
                id: messageItem
                required property var modelData
                width: answerColumn.width
                height: messageBubble.height

                Rectangle {
                  id: messageBubble
                  width: Math.round(parent.width * 0.78)
                  height: messageText.implicitHeight + Style.spacing.controlPaddingY * 2
                  radius: root.cornerRadius
                  color: messageItem.modelData.role === "user"
                    ? Style.selectedFillFor(root.foreground, Color.accent)
                    : Style.hoverFillFor(root.foreground, Color.accent)
                  anchors.right: messageItem.modelData.role === "user" ? parent.right : undefined
                  anchors.left: messageItem.modelData.role === "user" ? undefined : parent.left

                  Text {
                    id: messageText
                    width: parent.width - Style.spacing.controlPaddingX * 2
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.verticalCenter: parent.verticalCenter
                    text: messageItem.modelData.content
                    textFormat: messageItem.modelData.role === "assistant" ? Text.MarkdownText : Text.PlainText
                    wrapMode: Text.Wrap
                    color: root.foreground
                    linkColor: Color.accent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    horizontalAlignment: messageItem.modelData.role === "user" ? Text.AlignRight : Text.AlignLeft
                    onLinkActivated: function(link) { root.requestOpenLink(link) }
                  }
                }
              }
            }

            Rectangle {
              width: parent.width
              height: askingText.implicitHeight + Style.spacing.controlPaddingY * 2
              radius: root.cornerRadius
              color: Style.hoverFillFor(root.foreground, Color.accent)
              visible: root.busy

              Text {
                id: askingText
                width: parent.width - Style.spacing.controlPaddingX * 2
                anchors.centerIn: parent
                textFormat: Text.PlainText
                text: root.agentName ? ("Asking " + root.agentName + "…") : "Asking…"
                color: root.foreground
                opacity: 0.72
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }
            }

            Text {
              width: parent.width
              visible: root.errorText !== ""
              textFormat: Text.PlainText
              text: root.errorText
              wrapMode: Text.Wrap
              color: Color.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            BorderSurface {
              width: parent.width
              visible: root.pendingExternalUrl !== ""
              implicitHeight: linkPrompt.implicitHeight + Style.spacing.controlPaddingY * 2
              radius: root.cornerRadius
              color: Style.hoverFillFor(root.foreground, Color.accent)
              borderSpec: Border.flat(root.border, 1)

              ColumnLayout {
                id: linkPrompt
                width: parent.width - Style.spacing.controlPaddingX * 2
                anchors.centerIn: parent
                spacing: Style.spacing.sm

                Text {
                  Layout.fillWidth: true
                  textFormat: Text.PlainText
                  text: "Open this external HTTP(S) destination?\n" + root.pendingExternalHost
                    + "\n" + root.pendingExternalUrl
                  wrapMode: Text.WrapAnywhere
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                RowLayout {
                  Layout.alignment: Qt.AlignRight
                  Button {
                    text: "Cancel"
                    foreground: root.foreground
                    bordered: true
                    onClicked: {
                      root.pendingExternalUrl = ""
                      root.pendingExternalHost = ""
                    }
                  }
                  Button {
                    text: "Open"
                    foreground: root.foreground
                    bordered: true
                    onClicked: root.confirmOpenLink()
                  }
                }
              }
            }

            Text {
              width: parent.width
              visible: root.messages.length > 0 && !root.busy && root.errorText === ""
              textFormat: Text.PlainText
              text: "Reply above · Ctrl+N starts a new conversation · Ctrl+C copies the latest answer"
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

  Component.onDestruction: {
    if (askProc.running)
      askProc.signal(15)
    root.pendingRequest = ""
    root.messages = []
  }
}
