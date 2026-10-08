import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import Quickshell.Hyprland

// Ask Bagley from anywhere: a centered panel styled like the ctOS rofi launcher (ctos.rasi).
// Create it once per shell (it holds the `bagley` IPC target), then bind keys in Hyprland to
//   qs ipc call bagley toggle | open | close | see | explain | cancel
//   qs ipc call bagley ask "question"
//
// Keys: Enter asks, Ctrl+Enter asks about the screen (screenshot of the focused window, its
// title, the highlighted text and the clipboard), Up recalls the last question, Escape closes
// (and stops the answer if nothing has arrived yet), Ctrl+C stops it, Ctrl+N starts a new
// conversation, Ctrl+Shift+C copies the reply (or the part selected with the mouse),
// PageUp/PageDown scroll. While an approval waits, Up/Down/Tab pick ALLOW or DENY and Enter
// (with an empty input) answers.
PanelWindow {
    id: overlay

    // ctOS tokens (design/ctos/tokens.json). QML colors put alpha first: #0E0E0Eee is #ee0e0e0e.
    readonly property color panelColor: "#ee0e0e0e"
    readonly property color raised: "#202020"
    readonly property color chrome: "#D9D9D9"
    readonly property color white: "#FFFFFF"
    readonly property color body: "#CACACA"
    readonly property color gray: "#7A7A7A"
    readonly property color ok: "#00FA9A"
    readonly property color danger: "#FC3E38"
    readonly property string fontFamily: "JetBrainsMono Nerd Font"

    // The panel fades in and out over 100ms and nothing else moves. The ctOS bar's
    // CTOS_BAR_ANIMATIONS=reduced (or none) turns the fade off too.
    readonly property bool animate: String(Quickshell.env("CTOS_BAR_ANIMATIONS") || "all").toLowerCase() === "all"
    readonly property bool shown: BagleyService.overlayOpen && !BagleyService.capturing
    readonly property bool pending: BagleyService.pendingId !== ""
    readonly property real maxReply: Math.round(overlay.height * 0.6)
    property int choice: 1 // 0 ALLOW, 1 DENY. DENY is preselected so a stray Enter refuses.

    // A transparent layer over the whole output catches clicks outside the panel (they close
    // it, like rofi). The panel itself sits a fifth of the way down and grows downwards.
    anchors {
        top: true
        bottom: true
        left: true
        right: true
    }
    exclusionMode: ExclusionMode.Ignore
    color: "transparent"
    visible: false
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    WlrLayershell.namespace: "bagley"

    onShownChanged: shown ? overlay.reveal() : overlay.conceal()
    Component.onCompleted: {
        if (shown)
            overlay.reveal();
    }

    function reveal() {
        // Open on the monitor that has focus. The screen is set while hidden so the panel never
        // jumps between monitors while it is open.
        const name = Hyprland.focusedMonitor ? Hyprland.focusedMonitor.name : "";
        for (let i = 0; i < Quickshell.screens.length; i++) {
            if (Quickshell.screens[i].name === name)
                overlay.screen = Quickshell.screens[i];
        }
        overlay.visible = true;
        panel.opacity = 1;
        input.forceActiveFocus();
    }

    function conceal() {
        if (panel.opacity === 0)
            overlay.visible = false;
        else
            panel.opacity = 0; // The window goes away when the fade ends (onOpacityChanged).
    }

    function handleKey(event) {
        const ctrl = (event.modifiers & Qt.ControlModifier) !== 0;
        const shift = (event.modifiers & Qt.ShiftModifier) !== 0;
        const enter = event.key === Qt.Key_Return || event.key === Qt.Key_Enter;
        if (event.key === Qt.Key_Escape) {
            if (BagleyService.busy && !BagleyService.reply)
                BagleyService.cancel();
            BagleyService.closeOverlay();
        } else if (enter && overlay.pending && input.text.trim() === "") {
            BagleyService.approve(overlay.choice === 0 ? "allow" : "deny");
        } else if (enter) {
            if (input.text.trim() === "" && !ctrl)
                return;
            replyView.follow = true;
            BagleyService.ask(input.text, ctrl);
            input.text = "";
        } else if (overlay.pending && (event.key === Qt.Key_Up || event.key === Qt.Key_Down || event.key === Qt.Key_Tab)) {
            overlay.choice = overlay.choice === 0 ? 1 : 0;
        } else if (event.key === Qt.Key_Tab) {
            // Keep the focus in the input.
        } else if (event.key === Qt.Key_Up) {
            if (!BagleyService.lastQuestion)
                return;
            input.text = BagleyService.lastQuestion;
            input.cursorPosition = input.text.length;
        } else if (event.key === Qt.Key_PageUp || event.key === Qt.Key_PageDown) {
            const step = (event.key === Qt.Key_PageUp ? -1 : 1) * replyView.height * 0.8;
            const bottom = Math.max(0, replyView.contentHeight - replyView.height);
            replyView.contentY = Math.max(0, Math.min(bottom, replyView.contentY + step));
        } else if (ctrl && event.key === Qt.Key_N) {
            BagleyService.newConversation();
        } else if (ctrl && shift && event.key === Qt.Key_C) {
            const text = reply.selectedText || BagleyService.reply.trim();
            if (text)
                Quickshell.clipboardText = text;
        } else if (ctrl && event.key === Qt.Key_C && !input.selectedText && BagleyService.busy) {
            BagleyService.cancel();
        } else {
            return; // Typing: the input handles it.
        }
        event.accepted = true;
    }

    function codeColor(code) {
        if (code === "DONE")
            return overlay.ok;
        if (code === "ERROR")
            return overlay.danger;
        if (code === "NO SIGNAL" || code === "ABORT")
            return overlay.gray;
        return overlay.white;
    }

    Connections {
        target: BagleyService
        // A new approval: preselect DENY again.
        function onPendingIdChanged() {
            overlay.choice = 1;
        }
        // A new question: follow the stream again.
        function onReplyChanged() {
            if (BagleyService.reply === "")
                replyView.follow = true;
        }
    }

    // Clicks outside the panel close it.
    MouseArea {
        anchors.fill: parent
        onClicked: BagleyService.closeOverlay()
    }

    Rectangle {
        id: panel

        width: Math.min(560, overlay.width - 32)
        height: column.implicitHeight + 28
        x: Math.round((overlay.width - width) / 2)
        y: Math.round(overlay.height * 0.2)
        color: overlay.panelColor
        border.width: 1
        border.color: overlay.chrome
        opacity: 0

        Behavior on opacity {
            enabled: overlay.animate
            NumberAnimation {
                duration: 100
            }
        }

        onOpacityChanged: {
            if (opacity === 0 && !overlay.shown)
                overlay.visible = false;
        }

        // Clicks inside the panel stay inside.
        MouseArea {
            anchors.fill: parent
        }

        Column {
            id: column

            x: 14
            y: 14
            width: panel.width - 28
            spacing: 12

            // Input bar: 8px by 10px padding, a 1px gray line underneath.
            Item {
                width: column.width
                height: inputRow.implicitHeight + 17

                Row {
                    id: inputRow

                    x: 10
                    y: 8
                    width: parent.width - 20
                    spacing: 10

                    Text {
                        id: prompt
                        text: "BAGLEY"
                        color: overlay.ok
                        font.family: overlay.fontFamily
                        font.pixelSize: 16
                    }

                    Item {
                        width: inputRow.width - prompt.width - inputRow.spacing
                        height: prompt.implicitHeight

                        TextInput {
                            id: input
                            anchors.fill: parent
                            color: overlay.white
                            selectionColor: overlay.chrome
                            selectedTextColor: "#0E0E0E"
                            font.family: overlay.fontFamily
                            font.pixelSize: 16
                            clip: true
                            activeFocusOnTab: false
                            Keys.onPressed: event => overlay.handleKey(event)
                        }

                        Text {
                            visible: input.text.length === 0
                            text: "ASK"
                            color: overlay.gray
                            font.family: overlay.fontFamily
                            font.pixelSize: 16
                        }
                    }
                }

                Rectangle {
                    anchors.bottom: parent.bottom
                    width: parent.width
                    height: 1
                    color: overlay.gray
                }
            }

            // The reply, as plain text. It scrolls once it reaches 60% of the screen height and
            // follows the stream unless scrolled up. Select with the mouse; Ctrl+Shift+C copies.
            Flickable {
                id: replyView

                property bool follow: true
                property bool sticking: false

                // Keep the newest text in view while following.
                function stick() {
                    if (!follow)
                        return;
                    sticking = true;
                    contentY = Math.max(0, contentHeight - height);
                    sticking = false;
                }

                visible: BagleyService.reply.length > 0
                width: column.width
                height: Math.min(contentHeight, overlay.maxReply)
                contentWidth: width
                contentHeight: reply.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                onContentHeightChanged: stick()
                onHeightChanged: stick()
                // Scrolling up stops following; scrolling back to the bottom resumes it.
                onContentYChanged: {
                    if (!sticking)
                        follow = contentY >= contentHeight - height - 4;
                }

                TextEdit {
                    id: reply
                    width: replyView.width
                    text: BagleyService.reply.replace(/\s+$/, "")
                    textFormat: TextEdit.PlainText
                    wrapMode: TextEdit.Wrap
                    readOnly: true
                    selectByMouse: true
                    persistentSelection: true
                    activeFocusOnPress: false // Keep the keyboard in the input.
                    color: overlay.body
                    selectionColor: overlay.chrome
                    selectedTextColor: "#0E0E0E"
                    font.family: overlay.fontFamily
                    font.pixelSize: 14
                }
            }

            Text {
                visible: BagleyService.error !== ""
                width: column.width
                text: "[CRIT] " + BagleyService.error
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: overlay.danger
                font.family: overlay.fontFamily
                font.pixelSize: 14
            }

            Text {
                visible: BagleyService.notice !== "" && BagleyService.error === ""
                width: column.width
                text: "» " + BagleyService.notice
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                color: overlay.gray
                font.family: overlay.fontFamily
                font.pixelSize: 12
            }

            // An approval: what the tool would do, then ALLOW / DENY rows like rofi's list
            // (8px by 10px padding, 2px between rows, the selected row raised with a chrome
            // left border).
            Column {
                id: approval

                visible: overlay.pending
                width: column.width
                spacing: 2

                Text {
                    width: approval.width
                    bottomPadding: 6
                    text: "AWAIT // " + BagleyService.pendingSummary
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: overlay.white
                    font.family: overlay.fontFamily
                    font.pixelSize: 14
                }

                Repeater {
                    model: [
                        {
                            label: "ALLOW",
                            decision: "allow"
                        },
                        {
                            label: "DENY",
                            decision: "deny"
                        }
                    ]

                    delegate: Rectangle {
                        id: row

                        required property var modelData
                        required property int index
                        readonly property bool selected: overlay.choice === index

                        width: approval.width
                        height: label.implicitHeight + 16
                        color: selected ? overlay.raised : "transparent"

                        Rectangle {
                            width: 2
                            height: row.height
                            color: row.selected ? overlay.chrome : "transparent"
                        }

                        Text {
                            id: label
                            x: 10
                            y: 8
                            text: row.modelData.label
                            color: row.selected ? overlay.white : overlay.body
                            font.family: overlay.fontFamily
                            font.pixelSize: 16
                        }

                        MouseArea {
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onEntered: overlay.choice = row.index
                            onClicked: BagleyService.approve(row.modelData.decision)
                        }
                    }
                }
            }

            // » THINK // H4CH1 // qwen3:14b, with the keys that matter right now on the right.
            Item {
                width: column.width
                height: status.implicitHeight

                Row {
                    id: status

                    Text {
                        text: "» "
                        color: overlay.gray
                        font.family: overlay.fontFamily
                        font.pixelSize: 12
                        font.weight: 600
                    }

                    Text {
                        text: BagleyService.lineCode + (BagleyService.lineTool ? " " + BagleyService.lineTool : "")
                        color: overlay.codeColor(BagleyService.lineCode)
                        font.family: overlay.fontFamily
                        font.pixelSize: 12
                        font.weight: 600
                    }

                    Text {
                        text: (BagleyService.lineMachine ? " // " + BagleyService.lineMachine.toUpperCase() : "") + (BagleyService.lineModel ? " // " + BagleyService.lineModel : "")
                        color: overlay.gray
                        font.family: overlay.fontFamily
                        font.pixelSize: 12
                        font.weight: 600
                    }
                }

                Text {
                    anchors.right: parent.right
                    text: overlay.pending ? "ENTER ANSWER" : BagleyService.busy ? "CTRL+C STOP" : "CTRL+ENTER SEE"
                    color: overlay.gray
                    font.family: overlay.fontFamily
                    font.pixelSize: 12
                }
            }
        }
    }

    IpcHandler {
        target: "bagley"

        function toggle(): void {
            BagleyService.toggleOverlay();
        }

        function open(): void {
            BagleyService.openOverlay();
        }

        function close(): void {
            BagleyService.closeOverlay();
        }

        function ask(text: string): void {
            BagleyService.ask(text, false);
            BagleyService.openOverlay();
        }

        // Capture first (the overlay stays hidden until the screenshot is taken), then show.
        function see(): void {
            BagleyService.see();
            BagleyService.openOverlay();
        }

        function explain(): void {
            BagleyService.explain();
            BagleyService.openOverlay();
        }

        function cancel(): void {
            BagleyService.cancel();
        }
    }
}
