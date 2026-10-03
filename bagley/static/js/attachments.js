// Attached files: uploaded into the workspace so Bagley's file tools can read them.

import { toast } from "./ui.js";
import { $, el, formatBytes, icon } from "./util.js";

const MAX_BYTES = 5_000_000;

async function looksLikeText(file) {
  const head = new Uint8Array(await file.slice(0, 4096).arrayBuffer());
  return !head.includes(0);
}

export class Attachments {
  constructor({ onChange }) {
    this.items = [];
    this.onChange = onChange;
    this.root = $("#attachments");
  }

  get uploading() {
    return this.items.some((i) => i.status === "uploading");
  }

  get ready() {
    return this.items.filter((i) => i.status === "ready");
  }

  async add(files) {
    for (const file of files) {
      if (file.size > MAX_BYTES) {
        toast(`${file.name} is larger than 5 MB.`, { type: "error" });
        continue;
      }
      if (!(await looksLikeText(file))) {
        toast(`${file.name} isn't a text file. Bagley can only read text for now.`, { type: "error" });
        continue;
      }
      const item = { id: crypto.randomUUID?.() || String(Math.random()), name: file.name, size: file.size, status: "uploading" };
      this.items.push(item);
      this.render();
      try {
        const res = await fetch(`/api/workspace/uploads/${encodeURIComponent(file.name)}`, { method: "PUT", body: file });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Upload failed (${res.status})`);
        item.path = (await res.json()).path;
        item.status = "ready";
      } catch (err) {
        item.status = "failed";
        toast(`Couldn't attach ${file.name}: ${err.message}`, { type: "error" });
      }
      this.render();
    }
  }

  remove(id) {
    this.items = this.items.filter((i) => i.id !== id);
    this.render();
  }

  clear() {
    this.items = [];
    this.render();
  }

  /** Line appended to the message so the model knows where the files are. */
  note() {
    const paths = this.ready.map((i) => i.path);
    return paths.length ? `\n\n📎 Attached: ${paths.join(", ")}` : "";
  }

  render() {
    this.root.hidden = this.items.length === 0;
    this.root.replaceChildren(...this.items.map((item) => el("span", { class: `chip ${item.status}`, title: item.path || item.name },
      item.status === "uploading" ? el("span", { class: "spinner" }) : icon(item.status === "failed" ? "triangle-alert" : "file-text", "icon-sm"),
      el("span", { class: "name", text: item.name }),
      el("span", { class: "size", text: formatBytes(item.size) }),
      el("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Remove ${item.name}`, onclick: () => this.remove(item.id) }, icon("x", "icon-xs")),
    )));
    this.onChange?.();
  }

  /** Accept drops anywhere in `zone`, highlighting `target` while dragging. */
  bindDrop(zone, target) {
    let depth = 0;
    const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    zone.addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth++;
      target.classList.add("dragging");
    });
    zone.addEventListener("dragover", (e) => hasFiles(e) && e.preventDefault());
    zone.addEventListener("dragleave", () => {
      depth = Math.max(0, depth - 1);
      if (!depth) target.classList.remove("dragging");
    });
    zone.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      target.classList.remove("dragging");
      this.add([...e.dataTransfer.files]);
    });
  }
}
