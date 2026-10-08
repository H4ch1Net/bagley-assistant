pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io

// Bagley for the ctOS shell. Two things live here:
// - what Bagley is doing everywhere, followed with `bagley status --follow --json` (the bar
//   segment reads it), and
// - the overlay's own turn, streamed by `bagley overlay-ask --json` (or `see` / `explain`), one
//   agent event per line.
// Commands run without a shell: the question is passed as a single argument.
Singleton {
    id: root

    // `bagley` from PATH. Quickshell started by Hyprland may not see ~/.local/bin; then set
    // BAGLEY_BIN to the full path in the environment Quickshell starts with.
    readonly property string bagley: Quickshell.env("BAGLEY_BIN") || "bagley"
    readonly property string webUrl: Quickshell.env("BAGLEY_URL") || "http://127.0.0.1:8765"

    // Activity, as `bagley status --follow --json` reports it ------------------------------------
    property string state: "offline" // idle, thinking, tool, approval, happy, error, offline...
    property string code: "NO SIGNAL" // IDLE, THINK, EXEC, AWAIT, DONE, ERROR, NO SIGNAL...
    property string tool: ""
    property string machine: ""
    property string model: ""
    property int approvals: 0
    property int runs: 0

    // The overlay's turn --------------------------------------------------------------------------
    property bool overlayOpen: false
    property bool capturing: false // The overlay hides while the screen is captured.
    readonly property bool busy: askProc.running || captureDelay.running
    property string reply: ""
    property string runCode: "" // This turn's code; empty before the first question.
    property string runTool: ""
    property string runMachine: ""
    property string runModel: ""
    property string error: ""
    property string notice: ""
    property string pendingId: "" // Approval waiting for an answer, and what it would do.
    property string pendingSummary: ""
    property string conversationId: "" // Follow-ups continue it; newConversation() clears it.
    property string lastQuestion: ""

    // What the overlay's status line shows: this turn once there is one, else the activity.
    readonly property string lineCode: runCode || code
    readonly property string lineTool: runCode ? runTool : tool
    readonly property string lineMachine: (runCode ? runMachine : machine) || machine
    readonly property string lineModel: (runCode ? runModel : model) || model

    // Bookkeeping for the ask process.
    property var pendingCommand: []
    property var queued: null // A turn asked while the last one was still stopping.
    property bool cancelled: false
    property int exitCode: -1
    property string lastStderr: ""

    readonly property var codes: ({
            idle: "IDLE",
            listening: "INPUT",
            thinking: "THINK",
            reasoning: "REASON",
            writing: "TX",
            tool: "EXEC",
            approval: "AWAIT",
            speaking: "VOICE",
            happy: "DONE",
            error: "ERROR",
            offline: "NO SIGNAL"
        })
    readonly property var liveCodes: ["THINK", "REASON", "TX", "EXEC", "AWAIT"]

    // Overlay ------------------------------------------------------------------------------------

    function openOverlay() {
        root.overlayOpen = true;
    }

    function closeOverlay() {
        root.overlayOpen = false;
    }

    function toggleOverlay() {
        root.overlayOpen = !root.overlayOpen;
    }

    // Ask a question. withContext attaches the focused window's screenshot, its title, the
    // highlighted text and the clipboard (`overlay-ask --see`).
    function ask(text, withContext) {
        const question = (text || "").trim();
        if (!question && !withContext)
            return;
        if (question)
            root.lastQuestion = question;
        const command = [root.bagley, "overlay-ask", "--json"];
        if (root.conversationId)
            command.push("--conversation", root.conversationId);
        if (withContext)
            command.push("--see");
        command.push("--");
        if (question)
            command.push(question);
        root.run(command, withContext);
    }

    // "What's on my screen?" with the screenshot, as a new conversation.
    function see() {
        root.run([root.bagley, "see", "--json", "--no-notify"], true);
    }

    // Explain the highlighted text (or the clipboard).
    function explain() {
        root.run([root.bagley, "explain", "--json", "--no-notify"], false);
    }

    // decision: "allow", "deny" or "always". The run reports the result as approval.result.
    function approve(decision) {
        if (!root.pendingId)
            return;
        const command = [root.bagley, "approve", root.pendingId];
        if (decision === "deny")
            command.push("--deny");
        else if (decision === "always")
            command.push("--always");
        Quickshell.execDetached(command);
        root.pendingId = "";
        root.pendingSummary = "";
    }

    // Stop the turn. The CLI is terminated, which drops its stream, and the server stops the run.
    function cancel() {
        root.queued = null;
        if (captureDelay.running) {
            captureDelay.stop();
            root.capturing = false;
            root.runCode = "ABORT";
            return;
        }
        if (askProc.running) {
            root.cancelled = true;
            askProc.running = false; // SIGTERM.
        }
    }

    function newConversation() {
        root.cancel();
        root.reset();
        root.runCode = "";
        root.conversationId = "";
    }

    // Turn bookkeeping ---------------------------------------------------------------------------

    function reset() {
        root.reply = "";
        root.error = "";
        root.notice = "";
        root.runTool = "";
        root.runMachine = "";
        root.runModel = "";
        root.pendingId = "";
        root.pendingSummary = "";
        root.lastStderr = "";
        root.exitCode = -1;
    }

    function run(command, capture) {
        if (askProc.running) {
            // One turn at a time: stop this one, start the next once it has exited.
            root.queued = {
                command: command,
                capture: capture
            };
            root.cancelled = true;
            askProc.running = false;
            return;
        }
        root.reset();
        root.runCode = "THINK";
        root.pendingCommand = command;
        root.capturing = capture;
        if (capture)
            captureDelay.restart(); // Let the overlay fade out first so it isn't in the shot.
        else
            root.launch();
    }

    function launch() {
        askProc.command = root.pendingCommand;
        askProc.running = true;
    }

    function finished() {
        const wasCancelled = root.cancelled;
        root.cancelled = false;
        root.capturing = false;
        root.pendingId = "";
        root.pendingSummary = "";
        if (root.queued) {
            const next = root.queued;
            root.queued = null;
            root.run(next.command, next.capture);
            return;
        }
        if (wasCancelled) {
            root.runCode = "ABORT";
        } else if (root.liveCodes.indexOf(root.runCode) >= 0) {
            // The command ended without run.end: not installed, crashed or killed.
            root.runCode = "ERROR";
            if (!root.error) {
                if (root.exitCode < 0)
                    root.error = `Could not run ${root.bagley}. Is Bagley installed? Set BAGLEY_BIN to its path.`;
                else
                    root.error = root.lastStderr || `bagley exited with code ${root.exitCode}.`;
            }
        }
    }

    // One line of `overlay-ask --json`: an agent event (see bagley/api/desktop.py).
    function handleEvent(line) {
        let event;
        try {
            event = JSON.parse(line);
        } catch (e) {
            return;
        }
        const call = event.call || {};
        switch (event.type) {
        case "context":
            // The screen has been captured; the overlay can come back.
            root.capturing = false;
            if (event.notes && event.notes.length)
                root.notice = event.notes.join(" // ");
            break;
        case "run.start":
            root.conversationId = event.conversation_id || root.conversationId;
            root.runCode = "THINK";
            break;
        case "model":
            root.runMachine = event.machine || "";
            root.runModel = event.model || "";
            break;
        case "status":
            root.runCode = root.codes[event.state] || String(event.state || "").toUpperCase();
            root.runTool = event.tool || "";
            break;
        case "text.delta":
            root.reply += event.text || "";
            break;
        case "message":
            if (root.reply && !root.reply.endsWith("\n"))
                root.reply += "\n\n";
            break;
        case "tool.start":
            root.runCode = "EXEC";
            root.runTool = call.name || "";
            break;
        case "approval.request":
            root.pendingId = call.id || "";
            root.pendingSummary = event.summary || call.name || "";
            root.runCode = "AWAIT";
            break;
        case "approval.result":
            if (event.id === root.pendingId) {
                root.pendingId = "";
                root.pendingSummary = "";
            }
            break;
        case "notice":
            root.notice = event.message || "";
            break;
        case "error":
            root.error = (event.message || "Something went wrong.") + (event.hint ? " " + event.hint : "");
            root.runCode = "ERROR";
            break;
        case "run.end":
            if (event.stats && event.stats.machine)
                root.runMachine = event.stats.machine;
            root.runCode = root.error ? "ERROR" : event.stopped ? "ABORT" : "DONE";
            root.runTool = "";
            break;
        }
    }

    // One line of `bagley status --follow --json`.
    function applyActivity(line) {
        let snap;
        try {
            snap = JSON.parse(line);
        } catch (e) {
            return;
        }
        if (!snap || snap.type === "ping")
            return;
        root.state = snap.state || "offline";
        root.code = snap.code || root.codes[root.state] || "NO SIGNAL";
        root.tool = snap.tool || "";
        root.machine = snap.machine || "";
        root.model = snap.model || "";
        root.approvals = snap.approvals || 0;
        root.runs = snap.runs || 0;
    }

    function setOffline() {
        root.applyActivity('{"state": "offline", "code": "NO SIGNAL"}');
    }

    // Processes ----------------------------------------------------------------------------------

    Process {
        id: follow
        command: [root.bagley, "status", "--follow", "--json"]
        running: true
        stdout: SplitParser {
            onRead: line => {
                followRetry.interval = 5000;
                root.applyActivity(line);
            }
        }
        // `status --follow` reconnects to the server by itself. This covers the command itself
        // ending (not installed yet, killed): show NO SIGNAL and try again, less often each time.
        onRunningChanged: {
            if (!running) {
                root.setOffline();
                followRetry.restart();
            }
        }
    }

    Timer {
        id: followRetry
        interval: 5000
        onTriggered: {
            follow.running = true;
            interval = Math.min(interval * 2, 60000);
        }
    }

    Process {
        id: askProc
        stdout: SplitParser {
            onRead: line => root.handleEvent(line)
        }
        stderr: SplitParser {
            onRead: line => {
                if (line.trim())
                    root.lastStderr = line.trim();
            }
        }
        onExited: (exitCode, exitStatus) => root.exitCode = exitCode
        onRunningChanged: {
            if (!running)
                root.finished();
        }
    }

    Timer {
        id: captureDelay
        interval: 250 // The 100ms fade plus a frame or two.
        onTriggered: root.launch()
    }
}
