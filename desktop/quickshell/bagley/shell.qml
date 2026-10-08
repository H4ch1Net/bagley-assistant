import Quickshell

// Bagley on its own: the overlay and its `bagley` IPC target, for trying it out or for running
// beside a bar you'd rather not edit.
//
//   cp -r desktop/quickshell/bagley ~/.config/quickshell/bagley
//   qs -p ~/.config/quickshell/bagley/shell.qml
//   qs ipc -p ~/.config/quickshell/bagley/shell.qml call bagley toggle
//
// To put Bagley in the ctOS bar instead (one Quickshell instance, the segment in the bar):
//
// 1. Copy this folder next to the bar's bar.qml, so it becomes the module `qs.bagley`:
//      sudo cp -r desktop/quickshell/bagley /opt/ctos/      (or: cp -r ... ~/ctOS/)
//
// 2. In bar.qml, add the import next to the others:
//      import qs.common
//      import qs.common.components
//      import qs.bar.components
//      import qs.bagley                                      // <- add
//
// 3. Inside the root PanelWindow (anywhere, exactly once), create the overlay:
//      PanelWindow {
//          id: root
//          ...
//          BagleyOverlay {}                                  // <- add
//
// 4. In the right-hand Row (the one with Layout.alignment: Qt.AlignRight), put the segment
//    first, followed by a divider, so it reads [BAGLEY][net][user][status]:
//      Row {
//          Layout.fillHeight: true
//          Layout.alignment: Qt.AlignRight
//
//          BagleySegment {}                                  // <- add
//          Divider {}                                        // <- add
//
//          SimpleSegment {
//              id: netSegment
//              ...
//
// 5. Restart the bar and point the Hyprland binds at its instance:
//      qs ipc -p /opt/ctos/bar.qml call bagley toggle
//
// See docs/desktop.md for the binds, the mako snippet and troubleshooting.
ShellRoot {
    BagleyOverlay {}
}
