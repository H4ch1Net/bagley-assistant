// Study: flashcard decks with spaced repetition, a review session, adding and importing cards.

import { api } from "../api.js";
import { keepToasts, toast } from "../ui.js";
import { $, el, icon } from "../util.js";
import { field, header, section } from "../settings-kit.js";

const GRADES = [["Again", 1, "a"], ["Hard", 3, "h"], ["Good", 4, "g"], ["Easy", 5, "e"]];

export function render(panel, ctx) {
  const decksBox = el("div", { class: "list" });
  const deck = el("input", { class: "input", placeholder: "Deck, e.g. Networking", maxlength: 60, list: "deck-names" });
  const front = el("textarea", { class: "textarea", rows: 2, placeholder: "Front: What does DHCP do?", "aria-label": "Front" });
  const back = el("textarea", { class: "textarea", rows: 2, placeholder: "Back: Hands out IP addresses, gateway and DNS to clients.", "aria-label": "Back" });
  const names = el("datalist", { id: "deck-names" });
  const add = el("button", { class: "btn btn-primary", type: "button" }, icon("plus", "icon-sm"), "Add card");
  add.addEventListener("click", async () => {
    if (!front.value.trim() || !back.value.trim()) return toast("A card needs a front and a back.", { type: "error" });
    try {
      const res = await api.post("/api/flashcards", { deck: deck.value.trim() || "General", cards: [{ front: front.value.trim(), back: back.value.trim() }] });
      toast(res.added ? "Card added" : "That card is already in the deck");
      front.value = back.value = "";
      front.focus();
      draw();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });
  const csv = el("textarea", { class: "textarea mono", rows: 4, placeholder: "front,back,deck\nWhat port does SSH use?,22,Networking", "aria-label": "Cards as CSV" });
  const importBtn = el("button", { class: "btn btn-sm", type: "button" }, icon("upload", "icon-sm"), "Import CSV");
  importBtn.addEventListener("click", async () => {
    if (!csv.value.trim()) return csv.focus();
    try {
      const res = await fetch(`/api/flashcards/import${deck.value.trim() ? `?deck=${encodeURIComponent(deck.value.trim())}` : ""}`, { method: "POST", headers: { "Content-Type": "text/csv" }, body: csv.value });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Import failed");
      toast(`Imported ${data.added} cards${data.duplicates ? ` · ${data.duplicates} already there` : ""}`);
      csv.value = "";
      draw();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });

  const draw = async () => {
    let decks = [];
    try {
      decks = await api.get("/api/flashcards/decks");
    } catch {
      decksBox.replaceChildren(el("div", { class: "list-empty", text: "Flashcards are not available on this server." }));
      return;
    }
    names.replaceChildren(...decks.map((d) => el("option", { value: d.deck })));
    decksBox.replaceChildren(...(decks.length ? decks.map((d) => el("div", { class: "list-item", style: "align-items:center" },
      el("span", { class: "tool-icon" }, icon("graduation-cap", "icon-sm")),
      el("div", { class: "grow" },
        el("div", { class: "name" }, d.deck, d.due ? el("span", { class: "badge badge-accent", text: `${d.due} due` }) : el("span", { class: "badge badge-ok", text: "done for now" }), d.new ? el("span", { class: "badge", text: `${d.new} new` }) : null),
        el("div", { class: "desc", text: `${d.total} card${d.total === 1 ? "" : "s"}` }),
      ),
      el("a", { class: "icon-btn icon-btn-sm", href: `/api/flashcards/export?deck=${encodeURIComponent(d.deck)}`, title: "Export CSV", "aria-label": `Export ${d.deck}` }, icon("download", "icon-sm")),
      el("button", { class: "btn btn-sm btn-primary", type: "button", disabled: !d.due, onclick: () => review(d.deck, draw) }, icon("play", "icon-xs"), "Review"),
    )) : [el("div", { class: "list-empty", text: "No cards yet. Add some here, or in study mode ask Bagley to make flashcards from your notes." })]));
  };

  header(panel, "Study", ["Flashcards with spaced repetition: cards you get wrong come back soon, the ones you know move further out. In study mode Bagley quizzes you, makes cards from your notes and reviews them with you. In the terminal: bagley cards."]);
  panel.append(
    section("Decks", decksBox),
    section("Add a card", field("Deck", deck), names, el("div", { class: "field-row" }, field("Front", front), field("Back", back)), add),
    section("Import", csv, el("div", { class: "inline", style: "margin-top:8px" }, importBtn, el("span", { class: "help", text: "Columns front, back and optionally deck. The deck field above overrides it." }))),
  );
  draw();
}

/** A review session in the tool dialog: front, reveal, grade. Failed cards come back once. */
export async function review(deck, onDone) {
  let cards;
  try {
    cards = await api.get(`/api/flashcards?deck=${encodeURIComponent(deck)}&due=1&limit=50`);
  } catch (err) {
    toast(err.message, { type: "error" });
    return;
  }
  const d = $("#tool-dialog");
  const queue = [...cards];
  const again = new Set();
  let done = 0;
  let card = null;
  let revealed = false;
  const onKey = (e) => {
    if (!d.open || e.target.closest("input, textarea")) return;
    if (!revealed && (e.key === " " || e.key === "Enter")) {
      e.preventDefault();
      reveal();
    } else if (revealed) {
      const grade = GRADES.find((g) => g[2] === e.key.toLowerCase()) || (/^[0-5]$/.test(e.key) ? [null, Number(e.key)] : null);
      if (grade) {
        e.preventDefault();
        submit(grade[1]);
      }
    }
  };
  document.addEventListener("keydown", onKey);
  d.addEventListener("close", () => {
    document.removeEventListener("keydown", onKey);
    onDone?.();
  }, { once: true });

  const draw = () => {
    const body = el("div", { class: "dialog-body" });
    if (!card) {
      body.append(el("div", { class: "card-face cf", text: done ? `DECK CLEAR // ${done} reviewed` : "Nothing due in this deck." }));
    } else {
      body.append(
        el("div", { class: "help", style: "margin-bottom:8px", text: `${deck.toUpperCase()} // ${String(done + 1).padStart(2, "0")} OF ${String(done + queue.length + 1).padStart(2, "0")}${card.new ? " // NEW" : ""}` }),
        el("div", { class: "cf" }, el("div", { class: "card-face", text: card.front }), revealed ? el("div", { class: "card-face back", text: card.back }) : null),
      );
    }
    const foot = el("div", { class: "dialog-foot" });
    if (card && !revealed) foot.append(el("button", { class: "btn btn-primary", type: "button", onclick: reveal }, "Show answer"));
    else if (card) foot.append(...GRADES.map(([label, q, key]) => el("button", { class: `btn${q >= 4 ? " btn-primary" : ""}`, type: "button", title: `Key ${key.toUpperCase()}`, onclick: () => submit(q) }, label)));
    else foot.append(el("button", { class: "btn btn-primary", type: "button", text: "Close", onclick: () => d.close() }));
    d.replaceChildren(
      el("div", { class: "dialog-head" }, el("h2", {}, el("span", { class: "barcode", "aria-hidden": "true" }), "Review"),
        el("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: () => d.close() }, icon("x"))),
      body, foot,
    );
    if (!d.open) d.showModal();
    keepToasts();
    d.querySelector(".dialog-foot .btn")?.focus();
  };
  const next = () => {
    card = queue.shift() || null;
    revealed = false;
    draw();
  };
  function reveal() {
    revealed = true;
    draw();
  }
  async function submit(quality) {
    try {
      await api.post(`/api/flashcards/${card.id}/grade`, { quality });
    } catch (err) {
      toast(err.message, { type: "error" });
      return;
    }
    if (quality < 3 && !again.has(card.id)) {
      again.add(card.id);
      queue.push(card);
    }
    done += 1;
    next();
  }
  next();
}
