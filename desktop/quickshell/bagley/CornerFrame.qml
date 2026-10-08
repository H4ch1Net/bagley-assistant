//@ pragma Internal
pragma ComponentBehavior: Bound

import QtQuick

// The ctOS bar's CornerFrame (bar/components/CornerFrame.qml): four white L-corners with 7px
// arms, 1px thick, around a row of content that sits 11px in (arm plus a 4px margin) and 5px
// down. A segment centers it in a slot 6px wider than the frame and sets its height to the bar
// height minus 6 (31px). Internal to this folder so it never clashes with the bar's own.
Item {
    id: root

    property color accentColor: "white"
    property int thickness: 1
    property int armLength: 7
    property int horizontalMargin: 4

    default property alias content: container.data

    implicitWidth: container.width + 2 * armLength + 2 * horizontalMargin
    implicitHeight: container.height + 2 * armLength
    width: implicitWidth
    height: implicitHeight

    Row {
        id: container
        x: root.armLength + root.horizontalMargin
        y: root.armLength - 2 // The same visual fix as ctOS.
    }

    // One L: a horizontal and a vertical arm meeting at the top-left, rotated for each corner.
    component Corner: Item {
        width: root.armLength
        height: root.armLength

        Rectangle {
            width: root.armLength
            height: root.thickness
            color: root.accentColor
        }

        Rectangle {
            width: root.thickness
            height: root.armLength
            color: root.accentColor
        }
    }

    Corner {
        anchors.top: parent.top
        anchors.left: parent.left
    }

    Corner {
        anchors.top: parent.top
        anchors.right: parent.right
        rotation: 90
    }

    Corner {
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        rotation: 180
    }

    Corner {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        rotation: 270
    }
}
