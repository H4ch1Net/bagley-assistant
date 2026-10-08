import QtQuick
import Quickshell

// Bagley in the ctOS bar, built like the bar's SimpleSegment: a CornerFrame holding a 19px mini
// avatar, the state code (IDLE, THINK, EXEC web_search, AWAIT...) and the machine answering.
// Left click toggles the overlay, right click opens the web UI.
//
// The avatar is the "mini" detail of bagley/static/js/avatar.js: the ctOS diamond hub (outline
// plus a filled inner diamond) and two square satellite nodes joined to it. The hub is ctOS gray,
// green on DONE, red on ERROR, blinks white on AWAIT and dims on NO SIGNAL.
Item {
    id: root

    readonly property string code: BagleyService.code
    readonly property bool offline: code === "NO SIGNAL"
    readonly property bool awaiting: code === "AWAIT"
    readonly property string tool: BagleyService.tool.length > 16 ? BagleyService.tool.slice(0, 15) + "…" : BagleyService.tool
    readonly property color hubColor: {
        if (code === "DONE")
            return "#00FA9A";
        if (code === "ERROR")
            return "#FC3E38";
        if (awaiting)
            return "#FFFFFF";
        if (offline)
            return "#7A7A7A";
        return "#D9D9D9";
    }
    property bool blinkOn: true

    height: parent ? parent.height : 37
    width: frame.width + 6

    // AWAIT blinks the hub; nothing else in the segment moves.
    Timer {
        interval: 500
        repeat: true
        running: root.awaiting
        onTriggered: root.blinkOn = !root.blinkOn
        onRunningChanged: root.blinkOn = true
    }

    onHubColorChanged: avatar.requestPaint()
    onBlinkOnChanged: avatar.requestPaint()

    CornerFrame {
        id: frame

        anchors.centerIn: parent
        height: parent.height - 6

        Row {
            spacing: 5

            Canvas {
                id: avatar

                width: 19
                height: 19
                anchors.verticalCenter: parent.verticalCenter

                onPaint: {
                    const ctx = getContext("2d");
                    ctx.reset();
                    const dim = root.offline ? 0.6 : 1;
                    const hubAlpha = root.awaiting && !root.blinkOn ? 0.25 : 1;
                    const hub = {
                        x: 11.5,
                        y: 9.5,
                        r: 5
                    };
                    // Satellites sit where the avatar's first two seeds put them: left and
                    // slightly low, and up to the right.
                    const satellites = [
                        {
                            x: 2.5,
                            y: 13.5
                        },
                        {
                            x: 16.5,
                            y: 2.5
                        }
                    ];
                    ctx.lineWidth = 1;

                    // Spine edges, from the hub's outline to each satellite.
                    ctx.strokeStyle = root.hubColor;
                    ctx.globalAlpha = 0.55 * dim * hubAlpha;
                    for (const s of satellites) {
                        const dx = s.x - hub.x;
                        const dy = s.y - hub.y;
                        const t = hub.r / (Math.abs(dx) + Math.abs(dy));
                        ctx.beginPath();
                        ctx.moveTo(hub.x + dx * t, hub.y + dy * t);
                        ctx.lineTo(s.x, s.y);
                        ctx.stroke();
                    }

                    // Satellites: small squares in the tracker's neutral gray.
                    ctx.globalAlpha = 0.85 * dim;
                    ctx.fillStyle = root.offline ? "#7A7A7A" : "#D9D9D9";
                    for (const s of satellites)
                        ctx.fillRect(s.x - 1.5, s.y - 1.5, 3, 3);

                    // The hub: the ctOS diamond, an outline with a filled inner diamond.
                    ctx.globalAlpha = dim * hubAlpha;
                    ctx.strokeStyle = root.hubColor;
                    ctx.fillStyle = root.hubColor;
                    ctx.beginPath();
                    ctx.moveTo(hub.x, hub.y - hub.r);
                    ctx.lineTo(hub.x + hub.r, hub.y);
                    ctx.lineTo(hub.x, hub.y + hub.r);
                    ctx.lineTo(hub.x - hub.r, hub.y);
                    ctx.closePath();
                    ctx.stroke();
                    const inner = hub.r * 0.45;
                    ctx.beginPath();
                    ctx.moveTo(hub.x, hub.y - inner);
                    ctx.lineTo(hub.x + inner, hub.y);
                    ctx.lineTo(hub.x, hub.y + inner);
                    ctx.lineTo(hub.x - inner, hub.y);
                    ctx.closePath();
                    ctx.fill();
                }
            }

            // The state code, coloured only on DONE, ERROR and NO SIGNAL (bar text is 16/500).
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.code + (root.code === "EXEC" && root.tool ? " " + root.tool : "")
                color: root.code === "DONE" ? "#00FA9A" : root.code === "ERROR" ? "#FC3E38" : root.offline ? "#7A7A7A" : "#FFFFFF"
                font.family: "JetBrainsMono Nerd Font"
                font.pixelSize: 16
                font.weight: 500
            }

            Text {
                visible: text !== ""
                anchors.verticalCenter: parent.verticalCenter
                text: BagleyService.machine.toUpperCase()
                color: "#7A7A7A"
                font.family: "JetBrainsMono Nerd Font"
                font.pixelSize: 16
                font.weight: 500
            }
        }
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        cursorShape: Qt.PointingHandCursor
        onClicked: mouse => {
            if (mouse.button === Qt.RightButton)
                Quickshell.execDetached(["xdg-open", BagleyService.webUrl]);
            else
                BagleyService.toggleOverlay();
        }
    }
}
