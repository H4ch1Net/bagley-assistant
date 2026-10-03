// The conversation thread: renders history, streams live runs, and owns message actions.

import { avatarGlyph } from "./avatar.js";
import { renderMarkdown, setMarkdown } from "./markdown.js";
import { state, toolIcon, toolInfo, toolSummary } from "./state.js";
import { announce, toast } from "./ui.js";
import { $, clockTime, copyText, el, formatDuration, icon } from "./util.js";

const thread = () => $("#thread");
const list = () => $("#messages");
const rawText = new WeakMap(); // turn element -> markdown text (for copy / speak)

// Grouping ---------------------------------------------------------------------------------------

/** Turn stored messages into display turns: user turns and assistant turns made of parts. */
export function groupTurns(messages) {
  const results = new Map(messages.filter((m) => m.role === "tool").map((m) => [m.tool_call_id, m]));
  const turns = [];
  let current = null;
  for (const m of messages) {
    if (m.role === "user") {
      turns.push({ kind: "user", message: m });
      current = null;
      continue;
    }
    if (m.role !== "assistant") continue;
    if (!current) {
      current = { kind: "assistant", parts: [], messages: [] };
      turns.push(current);
    }
    current.messages.push(m);
    if (m.reasoning) current.parts.push({ type: "reasoning", text: m.reasoning });
    if (m.content) current.parts.push({ type: "text", text: m.content, interrupted: m.meta?.interrupted });
    for (const call of m.tool_calls || []) current.parts.push({ type: "tool", call, result: results.get(call.id) });
  }
  return turns;
}

// Building blocks --------------------------------------------------------------------------------

function parseArgs(raw) {
  if (typeof raw !== "string") return raw || {};
  try {
    return JSON.parse(raw);
  } catch {
    return { _raw: raw };
  }
}

function prettyResult(text) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

function proseBlock(markdown, streaming = false) {
  const node = el("div", { class: `prose${streaming ? " streaming" : ""}` });
  setMarkdown(node, markdown);
  return node;
}

function reasoningBlock(text, { live = false, seconds } = {}) {
  const label = el("span", { class: "label", text: live ? "Thinking…" : seconds ? `Thought for ${seconds}s` : "Reasoning" });
  const body = el("div", { class: "reasoning-text", text });
  const details = el("details", { class: `reasoning${live ? " live" : ""}` },
    el("summary", {}, icon("chevron-right", "icon-xs chev"), icon("brain", "icon-sm"), label),
    body,
  );
  return { details, label, body };
}

/** A collapsible card for one tool call. Returns helpers to update it as the call progresses. */
export function toolCard({ id, name, args, state: initial = "running", result, duration, summaryTemplate, category }) {
  const info = toolInfo(name);
  const statusEl = el("span", { class: "tool-status" });
  const resultPre = el("pre", { text: result ? prettyResult(result) : "" });
  const resultWrap = el("div", { hidden: !result }, el("h4", { text: "Result" }), resultPre);
  const head = el("button", { class: "tool-head", type: "button", "aria-expanded": "false" },
    el("span", { class: "tool-icon" }, icon(toolIcon(name, category || info?.category), "icon-sm")),
    el("span", { class: "tool-summary", text: toolSummary(name, args, summaryTemplate) }),
    statusEl,
    icon("chevron-right", "icon-xs chev"),
  );
  const detail = el("div", { class: "tool-detail" },
    el("h4", { text: `Arguments · ${name}` }),
    el("pre", { text: JSON.stringify(args, null, 2) }),
    resultWrap,
  );
  const card = el("div", { class: "tool-card", dataset: { id, state: initial } }, head, detail);
  head.addEventListener("click", () => {
    const open = card.classList.toggle("open");
    head.setAttribute("aria-expanded", String(open));
  });

  const api = {
    card,
    set(next, { result: text, duration: ms } = {}) {
      card.dataset.state = next;
      statusEl.replaceChildren();
      if (next === "running") statusEl.append(el("span", { class: "spinner" }));
      else if (next === "approval") statusEl.append(el("span", { text: "waiting" }));
      else if (next === "ok") statusEl.append(icon("check", "icon-sm"), el("span", { text: ms !== undefined ? formatDuration(ms) : "" }));
      else if (next === "denied") statusEl.append(el("span", { text: "declined" }));
      else if (next === "cancelled") statusEl.append(el("span", { text: "cancelled" }));
      else statusEl.append(icon("circle-x", "icon-sm"), el("span", { text: "failed" }));
      if (text !== undefined) {
        resultPre.textContent = prettyResult(text);
        resultWrap.hidden = false;
      }
    },
  };
  api.set(initial, { result, duration });
  return api;
}

function turnFoot(actions, meta) {
  const foot = el("div", { class: "turn-foot" });
  for (const a of actions) {
    foot.append(el("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": a.label, title: a.label, onclick: a.run }, icon(a.icon, "icon-sm")));
  }
  if (meta) foot.append(el("span", { class: "turn-meta", text: meta }));
  return foot;
}

function metaLine(meta = {}) {
  const bits = [];
  if (meta.model) bits.push(meta.model);
  if (meta.tokens_per_second) bits.push(`${Math.round(meta.tokens_per_second)} tok/s`);
  if (meta.duration_ms) bits.push(formatDuration(meta.duration_ms));
  return bits.join(" · ");
}

// Chat -------------------------------------------------------------------------------------------

export class Chat {
  constructor({ socket, avatars, voice, setStatus, onRunBegin, onRunEnd }) {
    this.socket = socket;
    this.avatars = avatars;
    this.voice = voice;
    this.setStatus = setStatus;
    this.onRunBegin = onRunBegin;
    this.onRunEnd = onRunEnd;
    this.live = null;
    this.stick = true;
    this.renderQueued = false;

    thread().addEventListener("scroll", () => {
      const t = thread();
      this.stick = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
      $("#jump").hidden = this.stick || list().hidden;
    }, { passive: true });
    $("#jump").addEventListener("click", () => this.scrollToBottom(true));
    list().addEventListener("click", (e) => {
      const btn = e.target.closest("[data-copy-code]");
      if (!btn) return;
      const code = btn.closest(".code-block")?.querySelector("code")?.textContent || "";
      copyText(code).then(() => this.flashCopied(btn));
    });

    const on = (type, fn) => socket.on(type, (ev) => fn.call(this, ev));
    on("run.start", this.onRunStart);
    on("status", this.onStatus);
    on("reasoning.delta", this.onReasoning);
    on("text.delta", this.onText);
    on("message", this.onMessage);
    on("tool.start", this.onToolStart);
    on("approval.request", this.onApproval);
    on("approval.result", this.onApprovalResult);
    on("tool.end", this.onToolEnd);
    on("notice", this.onNotice);
    on("error", this.onError);
    on("run.end", this.onRunEndEvent);
  }

  // Rendering history ------------------------------------------------------------------------

  showEmpty(empty) {
    $("#empty").hidden = !empty;
    list().hidden = empty;
    if (empty) $("#jump").hidden = true;
  }

  render(messages) {
    // A run still streaming into this conversation keeps its live node; history covers the rest.
    const reattach = this.live && this.live.conversationId === state.activeId;
    if (reattach) {
      const lastUser = messages.map((m) => m.role).lastIndexOf("user");
      messages = messages.slice(0, lastUser + 1);
    }
    const turns = groupTurns(messages);
    const nodes = turns.map((turn) => (turn.kind === "user" ? this.userTurn(turn.message) : this.assistantTurn(turn)));
    if (reattach) nodes.push(this.live.node);
    list().replaceChildren(...nodes);
    this.markLast();
    this.showEmpty(nodes.length === 0);
    requestAnimationFrame(() => this.scrollToBottom(false));
  }

  userTurn(message, { pending = false } = {}) {
    const node = el("article", { class: `turn turn-user${pending ? " pending" : ""}`, dataset: { id: message.id ?? "" } },
      el("div", { class: "bubble", text: message.content }),
    );
    rawText.set(node, message.content);
    node.append(turnFoot([
      { icon: "copy", label: "Copy message", run: (e) => this.copy(node, e.currentTarget) },
      { icon: "pencil", label: "Edit and resend", run: () => this.beginEdit(node) },
    ]));
    return node;
  }

  assistantShell() {
    const body = el("div", { class: "turn-body" });
    const node = el("article", { class: "turn turn-assistant" },
      el("div", { class: "turn-head" }, avatarGlyph(24), el("span", { class: "who", text: "Bagley" })),
      body,
    );
    return { node, body };
  }

  assistantTurn(turn) {
    const { node, body } = this.assistantShell();
    let text = "";
    for (const part of turn.parts) {
      if (part.type === "reasoning") body.append(reasoningBlock(part.text).details);
      else if (part.type === "text") {
        body.append(proseBlock(part.text));
        text += (text ? "\n\n" : "") + part.text;
        if (part.interrupted) body.append(el("div", { class: "notice" }, icon("info", "icon-sm"), el("span", { text: "Stopped before finishing." })));
      } else if (part.type === "tool") {
        const fn = part.call.function;
        const meta = part.result?.meta || {};
        let status = "error";
        if (!part.result || meta.cancelled) status = "cancelled";
        else if (meta.ok) status = "ok";
        else if (/declined/.test(part.result.content)) status = "denied";
        body.append(toolCard({
          id: part.call.id,
          name: fn.name,
          args: parseArgs(fn.arguments),
          state: status,
          result: part.result?.content,
          duration: meta.duration_ms,
        }).card);
      }
    }
    rawText.set(node, text);
    const last = turn.messages[turn.messages.length - 1];
    const first = turn.messages[0];
    node.querySelector(".turn-head").append(el("time", { text: clockTime(first.created_at) }));
    node.append(this.assistantFoot(node, metaLine(last?.meta)));
    return node;
  }

  assistantFoot(node, meta) {
    const actions = [{ icon: "copy", label: "Copy reply", run: (e) => this.copy(node, e.currentTarget) }];
    if (this.voice.canSpeak) actions.push({ icon: "volume-2", label: "Read aloud", run: () => this.speak(rawText.get(node)) });
    actions.push({ icon: "refresh-cw", label: "Regenerate", run: () => this.regenerate() });
    return turnFoot(actions, meta);
  }

  /** Only the last user message can be edited and only the last reply regenerated. */
  markLast() {
    const turns = [...list().children];
    turns.forEach((t) => t.classList.remove("last"));
    const lastUser = turns.filter((t) => t.classList.contains("turn-user")).pop();
    const lastBot = turns.filter((t) => t.classList.contains("turn-assistant")).pop();
    lastUser?.classList.add("last");
    lastBot?.classList.add("last");
    const busy = Boolean(state.run);
    for (const t of turns) {
      const isLast = t.classList.contains("last");
      t.querySelector('[aria-label="Edit and resend"]')?.toggleAttribute("hidden", !isLast || busy);
      t.querySelector('[aria-label="Regenerate"]')?.toggleAttribute("hidden", !isLast || busy || t !== turns[turns.length - 1]);
    }
  }

  // Sending ----------------------------------------------------------------------------------

  send(text) {
    if (!this.socket.send({ type: "chat", text, conversation_id: state.activeId })) {
      toast("Not connected to Bagley's server. Reconnecting…", { type: "error" });
      this.socket.reconnectNow();
      return false;
    }
    this.showEmpty(false);
    const pending = this.userTurn({ content: text }, { pending: true });
    list().append(pending);
    this.pendingUser = pending;
    this.beginRun(state.activeId);
    return true;
  }

  regenerate() {
    if (state.run || !state.activeId) return;
    if (!this.socket.send({ type: "chat", mode: "regenerate", conversation_id: state.activeId })) {
      toast("Not connected to Bagley's server.", { type: "error" });
      return;
    }
    const turns = [...list().children];
    const lastUser = turns.filter((t) => t.classList.contains("turn-user")).pop();
    turns.slice(turns.indexOf(lastUser) + 1).forEach((t) => t.remove());
    this.beginRun(state.activeId);
  }

  beginEdit(node) {
    if (state.run) return;
    const original = rawText.get(node);
    const area = el("textarea", { class: "textarea", rows: 3, "aria-label": "Edit message" });
    area.value = original;
    const cancel = () => {
      box.replaceWith(bubble);
      node.querySelector(".turn-foot").hidden = false;
    };
    const save = () => {
      if (state.run) {
        toast("Wait for the current reply to finish, or stop it first.");
        return;
      }
      const text = area.value.trim();
      if (!text || text === original) return cancel();
      if (!this.socket.send({ type: "chat", mode: "edit", text, conversation_id: state.activeId })) {
        toast("Not connected to Bagley's server.", { type: "error" });
        return;
      }
      bubble.textContent = text;
      rawText.set(node, text);
      box.replaceWith(bubble);
      node.querySelector(".turn-foot").hidden = false;
      const turns = [...list().children];
      turns.slice(turns.indexOf(node) + 1).forEach((t) => t.remove());
      this.beginRun(state.activeId);
    };
    area.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save(); }
      if (e.key === "Escape") { e.stopPropagation(); cancel(); }
    });
    const box = el("div", { class: "bubble-edit" }, area,
      el("div", { class: "row" },
        el("button", { class: "btn btn-sm", type: "button", text: "Cancel", onclick: cancel }),
        el("button", { class: "btn btn-sm btn-primary", type: "button", text: "Save & send", onclick: save }),
      ),
    );
    const bubble = node.querySelector(".bubble");
    bubble.replaceWith(box);
    node.querySelector(".turn-foot").hidden = true;
    area.focus();
    area.setSelectionRange(area.value.length, area.value.length);
  }

  stop() {
    if (state.run) this.socket.send({ type: "cancel" });
  }

  // Live run ---------------------------------------------------------------------------------

  beginRun(conversationId) {
    this.voice.stop();
    state.run = { conversationId, status: "thinking" };
    const { node, body } = this.assistantShell();
    node.classList.add("live");
    this.live = { conversationId, node, body, segment: null, cards: new Map(), text: "", started: performance.now() };
    list().append(node);
    this.stick = true;
    this.scrollToBottom(false);
    this.setStatus("thinking");
    this.markLast();
    this.onRunBegin?.();
  }

  isVisible() {
    return this.live && this.live.conversationId === state.activeId;
  }

  onRunStart(ev) {
    if (!this.live) this.beginRun(ev.conversation_id);
    this.live.conversationId = ev.conversation_id;
    if (state.run) state.run.conversationId = ev.conversation_id;
    if (ev.user_message && this.pendingUser) {
      this.pendingUser.classList.remove("pending");
      this.pendingUser.dataset.id = ev.user_message.id;
      this.pendingUser = null;
    }
  }

  onStatus(ev) {
    if (!state.run) return;
    state.run.status = ev.state;
    let label;
    if (ev.state === "tool") {
      const card = [...(this.live?.cards.values() || [])].pop();
      label = card?.card.querySelector(".tool-summary")?.textContent;
    }
    this.setStatus(ev.state, label);
  }

  closeSegment() {
    const seg = this.live?.segment;
    if (!seg) return;
    if (seg.type === "text") {
      seg.el.classList.remove("streaming");
      setMarkdown(seg.el, seg.raw);
    } else if (seg.type === "reasoning") {
      const secs = Math.max(1, Math.round((performance.now() - seg.started) / 1000));
      seg.block.details.classList.remove("live");
      seg.block.label.textContent = `Thought for ${secs}s`;
      if (!seg.userToggled) seg.block.details.open = false;
    }
    this.live.segment = null;
  }

  onReasoning(ev) {
    if (!this.live) return;
    let seg = this.live.segment;
    if (seg?.type !== "reasoning") {
      this.closeSegment();
      const block = reasoningBlock("", { live: true });
      block.details.open = true;
      seg = { type: "reasoning", block, raw: "", started: performance.now(), userToggled: false };
      block.details.querySelector("summary").addEventListener("click", () => (seg.userToggled = true));
      this.live.body.append(block.details);
      this.live.segment = seg;
    }
    seg.raw += ev.text;
    seg.block.body.textContent = seg.raw;
    seg.block.body.scrollTop = seg.block.body.scrollHeight;
    this.avatars.pulse(0.15);
    this.follow();
  }

  onText(ev) {
    if (!this.live) return;
    let seg = this.live.segment;
    if (seg?.type !== "text") {
      this.closeSegment();
      seg = { type: "text", el: proseBlock("", true), raw: "" };
      this.live.body.append(seg.el);
      this.live.segment = seg;
    }
    seg.raw += ev.text;
    this.live.text += ev.text;
    this.avatars.pulse(0.35);
    if (!this.renderQueued) {
      this.renderQueued = true;
      requestAnimationFrame(() => {
        this.renderQueued = false;
        const current = this.live?.segment;
        if (current?.type === "text") setMarkdown(current.el, current.raw);
        this.follow();
      });
    }
  }

  onMessage(ev) {
    if (!this.live) return;
    this.closeSegment();
    this.live.lastMeta = ev.message.meta;
    if (this.live.text && ev.message.content && !this.live.text.endsWith("\n\n")) this.live.text += "\n\n";
  }

  onToolStart(ev) {
    if (!this.live) return;
    this.closeSegment();
    const card = toolCard({
      id: ev.call.id,
      name: ev.call.name,
      args: ev.call.arguments,
      state: "running",
      summaryTemplate: ev.summary,
      category: ev.category,
    });
    this.live.cards.set(ev.call.id, card);
    this.live.body.append(card.card);
    this.follow();
  }

  onApproval(ev) {
    const card = this.live?.cards.get(ev.call.id);
    if (!card) return;
    card.set("approval");
    const summary = card.card.querySelector(".tool-summary").textContent;
    const decide = (decision) => {
      this.socket.send({ type: "approval", id: ev.call.id, decision });
      box.querySelectorAll("button").forEach((b) => (b.disabled = true));
    };
    const allow = el("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => decide("allow") }, icon("check", "icon-sm"), "Allow");
    const box = el("div", { class: "approval", role: "group", "aria-label": "Approval needed" },
      el("p", {}, el("strong", { text: "Allow this action? " }), `Bagley wants to ${summary.charAt(0).toLowerCase()}${summary.slice(1)}.`),
      el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Deny", onclick: () => decide("deny") }),
      el("button", { class: "btn btn-sm", type: "button", text: `Always allow ${ev.call.name}`, title: "Until you reload the page", onclick: () => decide("always") }),
      allow,
      el("div", { class: "approval-args" }, el("pre", { text: JSON.stringify(ev.call.arguments, null, 2) })),
    );
    card.card.append(box);
    card.approval = box;
    this.follow();
    // Only take focus when the user isn't typing somewhere, so a keystroke can't approve.
    const active = document.activeElement;
    if (this.stick && (!active || active === document.body || thread().contains(active))) allow.focus({ preventScroll: true });
    announce(`Bagley needs your approval to ${summary}.`);
  }

  onApprovalResult(ev) {
    const card = this.live?.cards.get(ev.id);
    if (!card) return;
    card.approval?.remove();
    card.set(ev.allowed ? "running" : "denied");
  }

  onToolEnd(ev) {
    const card = this.live?.cards.get(ev.id);
    if (!card) return;
    if (card.card.dataset.state === "denied") {
      card.set("denied", { result: ev.result });
      return;
    }
    card.set(ev.ok ? "ok" : "error", { result: ev.result, duration: ev.duration_ms });
  }

  onNotice(ev) {
    if (!this.live) {
      toast(ev.message);
      return;
    }
    this.closeSegment();
    this.live.body.append(el("div", { class: "notice" }, icon("info", "icon-sm"), el("span", { text: ev.message })));
    this.follow();
  }

  onError(ev) {
    if (!this.live || !state.run) {
      toast(ev.message, { type: "error" });
      return;
    }
    this.closeSegment();
    const hint = el("div", { class: "hint" });
    if (ev.hint) hint.innerHTML = renderMarkdown(ev.hint).replace(/^<p>|<\/p>\s*$/g, "");
    this.live.body.append(el("div", { class: "error-card", role: "alert" }, icon("triangle-alert"),
      el("div", { class: "body" }, el("strong", { text: ev.message }), ev.hint ? hint : null,
        el("button", { class: "btn btn-sm", type: "button", onclick: () => this.regenerate() }, icon("refresh-cw", "icon-sm"), "Try again"),
      ),
    ));
    this.live.failed = true;
    this.follow();
  }

  onRunEndEvent(ev) {
    const live = this.live;
    this.closeSegment();
    state.run = null;
    this.live = null;
    if (live) {
      live.node.classList.remove("live");
      for (const card of live.cards.values()) {
        if (["running", "approval"].includes(card.card.dataset.state)) {
          card.approval?.remove();
          card.set("cancelled");
        }
      }
      if (!live.body.children.length) {
        live.body.append(el("div", { class: "notice" }, icon("info", "icon-sm"), el("span", { text: ev.stopped ? "Stopped." : "No reply." })));
      }
      rawText.set(live.node, live.text);
      const meta = metaLine({ ...(live.lastMeta || {}), duration_ms: ev.stats?.duration_ms });
      live.node.querySelector(".turn-head").append(el("time", { text: clockTime(Date.now() / 1000) }));
      live.node.append(this.assistantFoot(live.node, meta));
      if (this.pendingUser) {
        this.pendingUser.classList.remove("pending");
        this.pendingUser = null;
      }
    }
    this.markLast();
    state.lastStats = ev.stats;
    const failed = live?.failed;
    this.onRunEnd?.(ev, { failed });
    if (!failed && !ev.stopped && live?.text) {
      announce("Bagley replied.");
      if (state.ui.speak) this.speak(live.text);
    }
  }

  // Helpers ----------------------------------------------------------------------------------

  follow() {
    if (this.stick && this.isVisible()) this.scrollToBottom(false);
  }

  scrollToBottom(smooth) {
    const t = thread();
    t.scrollTo({ top: t.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    this.stick = true;
    $("#jump").hidden = true;
  }

  async copy(node, button) {
    if (await copyText(rawText.get(node) || "")) this.flashCopied(button);
  }

  flashCopied(button) {
    const use = button.querySelector("use");
    if (!use) return;
    const prev = use.getAttribute("href");
    use.setAttribute("href", "/static/icons.svg#i-check");
    setTimeout(() => use.setAttribute("href", prev), 1200);
  }

  speak(markdown) {
    if (!this.voice.canSpeak || !markdown) return;
    this.voice.speak(markdown, {
      onStart: () => this.setStatus("speaking"),
      onWord: () => this.avatars.pulse(0.8),
      onEnd: () => !state.run && this.setStatus("idle"),
    });
  }

  lastReplyText() {
    const last = [...list().querySelectorAll(".turn-assistant")].pop();
    return last ? rawText.get(last) || "" : "";
  }
}
