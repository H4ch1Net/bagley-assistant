// ctOS lists for native pickers. A <select>'s list and an <input list>'s suggestions open in
// the operating system's colours (on Kali, Firefox draws them in the GTK theme's blue). These
// open a launcher-style list instead: chrome border, raised row with a chrome left edge.
//
// The native elements stay in charge of their values, focus and form behaviour; only their
// popups are replaced. Everything is delegated from the document, so lists built later work too.
// Touch screens keep the native select picker, which suits them better.

import { el } from "./util.js";

const coarse = matchMedia("(pointer: coarse)");
let current = null; // { anchor, popup, items, index, choose }

const isSelect = (node) => node instanceof HTMLSelectElement && !node.multiple && node.size <= 1;
const isSuggest = (node) => node instanceof HTMLInputElement && (node.dataset.suggest || node.hasAttribute("list"));

function close() {
  if (!current) return;
  current.popup.remove();
  current.anchor.removeAttribute("aria-expanded");
  current = null;
}

function place(popup, anchor) {
  const rect = anchor.getBoundingClientRect();
  const below = innerHeight - rect.bottom - 8;
  const above = rect.top - 8;
  const up = below < 160 && above > below;
  if (rect.bottom < 0 || rect.top > innerHeight) return false; // Scrolled out of sight.
  popup.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - rect.width - 8))}px`;
  popup.style.minWidth = `${rect.width}px`;
  popup.style.maxWidth = `${Math.max(rect.width, Math.min(480, innerWidth - 16))}px`;
  popup.style.maxHeight = `${Math.max(120, Math.min(320, up ? above : below))}px`;
  popup.style.top = up ? "" : `${rect.bottom + 2}px`;
  popup.style.bottom = up ? `${innerHeight - rect.top + 2}px` : "";
  return true;
}

function activate(index, { scroll = true } = {}) {
  if (!current) return;
  const items = current.items.filter((i) => !i.disabled);
  if (!items.length) return;
  current.items.forEach((i) => i.node.classList.remove("active"));
  current.index = Math.max(0, Math.min(index, current.items.length - 1));
  const item = current.items[current.index];
  item.node.classList.add("active");
  current.anchor.setAttribute("aria-activedescendant", item.node.id);
  if (scroll) {
    const { popup } = current;
    const top = item.node.offsetTop;
    if (top < popup.scrollTop) popup.scrollTop = top;
    else if (top + item.node.offsetHeight > popup.scrollTop + popup.clientHeight) popup.scrollTop = top + item.node.offsetHeight - popup.clientHeight;
  }
}

function step(delta) {
  if (!current) return;
  const { items } = current;
  let i = current.index;
  for (let n = 0; n < items.length; n++) {
    i = Math.max(0, Math.min(items.length - 1, i + delta));
    if (!items[i].disabled) break;
  }
  activate(i);
}

/** Show ``entries`` ({label, value, selected, disabled, group}) under ``anchor``. */
function show(anchor, entries, choose) {
  close();
  if (!entries.length) return;
  const popup = el("div", { class: "picker", role: "listbox", id: "picker-list" });
  const items = [];
  let group = null;
  entries.forEach((entry, n) => {
    if (entry.group && entry.group !== group) {
      group = entry.group;
      popup.append(el("div", { class: "picker-group", role: "presentation", text: group }));
    }
    const node = el("div", {
      class: "picker-item",
      role: "option",
      id: `picker-item-${n}`,
      "aria-selected": entry.selected ? "true" : "false",
      "aria-disabled": entry.disabled ? "true" : null,
      title: entry.label.length > 40 ? entry.label : null,
      text: entry.label,
    });
    const item = { ...entry, node };
    node.addEventListener("mousedown", (e) => e.preventDefault()); // Keep focus on the field.
    node.addEventListener("click", () => !entry.disabled && pick(item));
    node.addEventListener("mousemove", () => current && current.items[current.index] !== item && !entry.disabled && activate(items.indexOf(item), { scroll: false }));
    items.push(item);
    popup.append(node);
  });
  // Inside a modal dialog the list has to live in the dialog to be drawn above it.
  (anchor.closest("dialog[open]") || document.body).append(popup);
  place(popup, anchor);
  anchor.setAttribute("aria-expanded", "true");
  anchor.setAttribute("aria-controls", "picker-list");
  current = { anchor, popup, items, index: 0, choose };
  const selected = items.findIndex((i) => i.selected && !i.disabled);
  activate(selected >= 0 ? selected : items.findIndex((i) => !i.disabled));
}

function pick(item) {
  const { anchor, choose } = current;
  close();
  choose(item);
  anchor.dispatchEvent(new Event("input", { bubbles: true }));
  anchor.dispatchEvent(new Event("change", { bubbles: true }));
}

// Selects ----------------------------------------------------------------------------------------

function openSelect(select) {
  const entries = [...select.options].map((o) => ({
    label: o.label || o.text,
    value: o.value,
    selected: o.selected,
    disabled: o.disabled || o.parentElement?.disabled,
    group: o.parentElement instanceof HTMLOptGroupElement ? o.parentElement.label : null,
    option: o,
  }));
  show(select, entries, (item) => {
    select.selectedIndex = [...select.options].indexOf(item.option);
  });
}

// Suggestions (<input list>) ---------------------------------------------------------------------

/** Take over an input's datalist so the browser never draws its own dropdown. */
function adopt(input) {
  if (input.hasAttribute("list")) {
    input.dataset.suggest = input.getAttribute("list");
    input.removeAttribute("list");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("aria-autocomplete", "list");
  }
}

function openSuggest(input, { all = false } = {}) {
  const list = document.getElementById(input.dataset.suggest);
  if (!list) return close();
  const needle = all ? "" : input.value.trim().toLowerCase();
  const entries = [...list.options]
    .map((o) => ({ value: o.value, label: o.label && o.label !== o.value ? `${o.value}  ${o.label}` : o.value }))
    .filter((o) => o.value && (!needle || o.label.toLowerCase().includes(needle)) && o.value !== input.value);
  if (!entries.length) return close();
  show(input, entries.slice(0, 50), (item) => {
    input.value = item.value;
  });
  if (!all) {
    // While typing, nothing is picked until the user arrows to it, so Enter keeps its meaning.
    current.index = -1;
    current.items.forEach((i) => i.node.classList.remove("active"));
  }
}

// Events -----------------------------------------------------------------------------------------

document.addEventListener("mousedown", (e) => {
  const target = e.target;
  if (current && !current.popup.contains(target) && target !== current.anchor) close();
  if (isSelect(target) && target.matches(".select") && !coarse.matches && !target.disabled) {
    e.preventDefault(); // No native list.
    target.focus();
    if (current?.anchor === target) close();
    else openSelect(target);
  } else if (target instanceof HTMLInputElement && isSuggest(target)) {
    adopt(target);
    if (document.activeElement === target) {
      if (current?.anchor === target) close();
      else openSuggest(target, { all: true });
    }
  }
}, true);

document.addEventListener("focusin", (e) => {
  if (e.target instanceof HTMLInputElement && e.target.hasAttribute("list")) adopt(e.target);
});

document.addEventListener("focusout", (e) => {
  if (current && e.target === current.anchor) close();
});

document.addEventListener("input", (e) => {
  const target = e.target;
  if (target instanceof HTMLInputElement && target.dataset.suggest && e.isTrusted) openSuggest(target);
});

document.addEventListener("keydown", (e) => {
  const target = e.target;
  if (current && target === current.anchor) {
    const key = e.key;
    const choosing = current.index >= 0 && current.items[current.index];
    if (key === "ArrowDown" || key === "ArrowUp") step(key === "ArrowDown" ? 1 : -1);
    else if (key === "PageDown" || key === "PageUp") step(key === "PageDown" ? 8 : -8);
    else if (key === "Home" && isSelect(target)) activate(0);
    else if (key === "End" && isSelect(target)) activate(current.items.length - 1);
    else if (key === "Escape") close();
    else if ((key === "Enter" || (key === " " && isSelect(target))) && choosing) pick(choosing);
    else if (key === "Tab") return close();
    else if (isSelect(target) && key.length === 1 && !e.ctrlKey && !e.metaKey) {
      const from = current.index + 1;
      const order = [...current.items.slice(from), ...current.items.slice(0, from)];
      const hit = order.find((i) => !i.disabled && i.label.toLowerCase().startsWith(key.toLowerCase()));
      if (hit) activate(current.items.indexOf(hit));
    } else return;
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  if (isSelect(target) && target.matches(".select") && !coarse.matches) {
    const opens = e.key === " " || e.key === "Enter" || e.key === "F4" || (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp"));
    if (opens) {
      e.preventDefault();
      e.stopPropagation();
      openSelect(target);
    }
  } else if (target instanceof HTMLInputElement && isSuggest(target) && e.key === "ArrowDown") {
    adopt(target);
    e.preventDefault();
    openSuggest(target, { all: !target.value });
    if (current && current.index < 0) step(1);
  }
}, true);

addEventListener("resize", close);
// Follow the field when something behind the list scrolls; close once it is out of sight.
document.addEventListener("scroll", (e) => {
  if (current && !current.popup.contains(e.target) && !place(current.popup, current.anchor)) close();
}, true);
