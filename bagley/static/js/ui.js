// Toasts, confirm dialog, announcements and popover menus.

import { $, el, icon } from "./util.js";

export function announce(message) {
  const node = $("#announcer");
  node.textContent = "";
  requestAnimationFrame(() => (node.textContent = message));
}

/** A modal dialog makes the rest of the page inert, so toasts live inside it while it's open. */
function placeToasts(box) {
  const dialog = [...document.querySelectorAll("body > dialog[open]")].pop();
  const home = dialog || document.body;
  if (box.parentElement !== home) home.append(box);
}

let toastBox = null;

/** Put the toast stack back where it can be seen, after a dialog redraws or closes. */
export function keepToasts() {
  if (toastBox) placeToasts(toastBox);
}
// "close" doesn't bubble, but capture listeners still see it.
document.addEventListener("close", keepToasts, true);

/**
 * Show a toast. Options: `type` ("info" | "error"), `action` ({label, run}), `duration` in ms.
 * Returns a function that dismisses it.
 */

export function toast(message, { type = "info", action, duration } = {}) {
  // Held by reference: dialogs replace their contents, which can detach the stack.
  toastBox ||= $("#toasts");
  const box = toastBox;
  placeToasts(box);
  const node = el("div", { class: `toast ${type}`, role: type === "error" ? "alert" : "status" },
    icon(type === "error" ? "triangle-alert" : "circle-check"),
    el("span", { text: message }),
  );
  let timer;
  const remove = () => node.remove();
  const close = () => {
    clearTimeout(timer);
    node.classList.add("leaving");
    node.addEventListener("animationend", remove, { once: true });
    setTimeout(remove, 400);
  };
  if (action) {
    node.append(el("button", { type: "button", text: action.label, onclick: () => { action.run(); close(); } }));
  }
  box.append(node);
  while (box.children.length > 3) box.firstElementChild.remove();
  timer = setTimeout(close, duration ?? (action ? 6000 : type === "error" ? 6000 : 2600));
  return close;
}

export function confirmDialog({ title, message, confirm = "Confirm", danger = false }) {
  const dialog = $("#confirm-dialog");
  return new Promise((resolve) => {
    dialog.replaceChildren(
      el("div", { class: "dialog-body" },
        el("h2", { style: "margin:0 0 8px;font-size:17px", text: title }),
        el("p", { text: message }),
      ),
      el("div", { class: "dialog-foot" },
        el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => dialog.close("cancel") }),
        el("button", { class: `btn ${danger ? "btn-danger" : "btn-primary"}`, type: "button", text: confirm, onclick: () => dialog.close("ok") }),
      ),
    );
    keepToasts();
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
    dialog.querySelector(".dialog-foot .btn:last-child").focus();
  });
}

/** Light-dismiss popover menu anchored inside `container`. */
export function openMenu(container, build) {
  closeMenus();
  const menu = el("div", { class: "menu", role: "listbox" });
  build(menu);
  container.append(menu);
  const button = container.querySelector("[aria-expanded]");
  button?.setAttribute("aria-expanded", "true");
  const items = () => [...menu.querySelectorAll(".menu-item:not(:disabled)")];
  (menu.querySelector('[aria-selected="true"]') || items()[0])?.focus();
  const onKey = (e) => {
    const list = items();
    const i = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); list[(i + 1) % list.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); list[(i - 1 + list.length) % list.length]?.focus(); }
    else if (e.key === "Escape") { e.stopPropagation(); close(); button?.focus(); }
  };
  const onDown = (e) => { if (!container.contains(e.target)) close(); };
  function close() {
    menu.remove();
    button?.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", onDown, true);
    menu.removeEventListener("keydown", onKey);
  }
  menu.addEventListener("keydown", onKey);
  setTimeout(() => document.addEventListener("pointerdown", onDown, true));
  menu.close = close;
  return menu;
}

export function closeMenus() {
  document.querySelectorAll(".menu").forEach((m) => m.close?.());
}
