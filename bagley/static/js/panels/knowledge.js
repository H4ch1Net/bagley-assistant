// Knowledge: indexed folders, search by meaning, and the sources for recaps of your own life
// (Obsidian vaults, code folders).

import { api } from "../api.js";
import { state } from "../state.js";
import { toast } from "../ui.js";
import { debounce, el, icon } from "../util.js";
import { field, header, keepFocus, pullModel, savePrefs, section, stringList, value } from "../settings-kit.js";

let box = null;

export function render(panel, ctx) {
  box = el("div");
  const pathInput = el("input", { class: "input mono", placeholder: "~/Documents/Notes", spellcheck: "false", "aria-label": "Folder path" });
  const add = el("button", { class: "btn", type: "button" }, icon("folder-plus", "icon-sm"), "Add folder");
  const submit = async () => {
    if (!pathInput.value.trim()) return pathInput.focus();
    add.disabled = true;
    try {
      state.knowledge = await api.post("/api/knowledge/folders", { path: pathInput.value.trim() });
      pathInput.value = "";
      toast("Folder added. Indexing…");
      renderStatus(ctx);
    } catch (err) {
      toast(err.message, { type: "error" });
    }
    add.disabled = false;
  };
  add.addEventListener("click", submit);
  pathInput.addEventListener("keydown", (e) => e.key === "Enter" && submit());

  const query = el("input", { class: "input", placeholder: "Try a search, e.g. budget for Q3", "aria-label": "Search the knowledge base" });
  const results = el("div", { class: "kb-results" });
  let searchSeq = 0;
  query.addEventListener("input", debounce(async () => {
    const seq = ++searchSeq;
    const q = query.value.trim();
    if (!q) return results.replaceChildren();
    const hits = await api.get(`/api/knowledge/search?q=${encodeURIComponent(q)}`).catch(() => []);
    if (seq !== searchSeq) return;
    results.replaceChildren(...(hits.length ? hits.map((h) => el("div", { class: "kb-hit" },
      el("div", { class: "name" }, el("span", { class: "mono", text: h.path }), el("span", { class: `badge${h.match === "keyword" ? "" : " badge-accent"}`, text: h.match })),
      el("div", { class: "desc", text: h.text })))
      : [el("div", { class: "list-empty", text: "No matches." })]));
  }, 250));

  const saveList = (key) => async (items) => {
    const ok = await savePrefs({ [key]: items });
    if (ok && key === "vaults") await api.post("/api/knowledge/reindex").catch(() => {});
    return ok;
  };

  header(panel, "Knowledge", "Bagley searches these folders when you ask about your notes and documents. Files are indexed on this computer and never uploaded. Text, Markdown, code, HTML and CSV are supported (PDF too with the pdf extra).");
  panel.append(
    box,
    section("Add a folder",
      el("div", { class: "inline" }, pathInput, add),
      el("div", { class: "help", style: "margin-top:6px", text: "A full path on this computer. Hidden folders and node_modules are skipped." }),
    ),
    section("Search", query, results),
    section("Your life",
      el("p", { class: "help", style: "margin:0 0 10px", text: "Obsidian vaults are indexed like the folders above and read by date, so you can ask \"what was I working on Tuesday?\" or get a weekly recap. Code folders are scanned for git repositories; only your commit history is read, not the code." }),
      el("div", { class: "field-row" },
        el("div", { class: "field" }, el("span", { class: "field-label", text: "Obsidian vaults" }),
          stringList({ items: [...(value("vaults") || [])], placeholder: "~/Documents/Obsidian/Main", onChange: saveList("vaults") })),
        el("div", { class: "field" }, el("span", { class: "field-label", text: "Code folders" }),
          stringList({ items: [...(value("code_folders") || [])], placeholder: "~/dev", onChange: saveList("code_folders") })),
      ),
    ),
  );
  renderStatus(ctx);
}

/** Status and folder list; redrawn on index progress without touching the inputs. */
export function renderStatus(ctx) {
  if (!box?.isConnected) return;
  const k = state.knowledge || { folders: [], files: 0, passages: 0 };
  const indexing = k.state === "indexing";
  const semantic = k.embedding_model
    ? el("span", { class: "badge badge-accent", text: `search by meaning · ${k.embedding_model}` })
    : el("span", { class: "badge", text: "keyword search" });
  // Stays enabled while indexing (a second request is ignored) so keyboard focus survives redraws.
  const reindex = el("button", { class: "btn btn-sm", type: "button", "aria-label": "Reindex now", onclick: async () => {
    try {
      await api.post("/api/knowledge/reindex");
      toast("Reindexing…");
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  } }, icon("refresh-cw", "icon-sm"), "Reindex now");

  const embedModels = state.models.filter((m) => /embed|minilm|bge-|e5-|gte-/i.test(m.name));
  const embedSelect = el("select", { class: "select" },
    el("option", { value: "", text: "Automatic" }),
    el("option", { value: "off", text: "Off (keyword search only)" }),
    ...embedModels.map((m) => el("option", { value: m.name, text: m.name })));
  embedSelect.value = value("embedding_model") || "";
  embedSelect.addEventListener("change", async () => {
    if (await savePrefs({ embedding_model: embedSelect.value })) await api.post("/api/knowledge/reindex");
  });
  const canPull = state.health?.provider === "ollama" && !embedModels.length;
  const pullBox = el("div");
  if (canPull) {
    const progress = el("div", { class: "progress", hidden: true }, el("i"));
    const status = el("div", { class: "pull-status" });
    const pull = el("button", { class: "btn btn-sm", type: "button" }, icon("hard-drive-download", "icon-sm"), "Download nomic-embed-text (274 MB)");
    pull.addEventListener("click", async () => {
      pull.disabled = true;
      if (await pullModel("nomic-embed-text", { progress, status })) {
        await ctx.onModelsChanged();
        await api.post("/api/knowledge/reindex");
      } else pull.disabled = false;
    });
    pullBox.append(el("div", { class: "help", style: "margin:4px 0 8px", text: "For search by meaning (not just keywords), download a small local embedding model:" }), pull, progress, status);
  }

  keepFocus(box, () => box.replaceChildren(
    el("div", { class: "section" },
      el("div", { class: "kb-status" },
        indexing ? el("span", { class: "spinner" }) : icon("library", "icon-sm"),
        el("span", { text: indexing ? `Indexing ${k.progress || "…"}` : `${k.files.toLocaleString()} files · ${k.passages.toLocaleString()} passages` }),
        semantic,
        el("span", { class: "grow" }),
        reindex,
      ),
      k.error ? el("div", { class: "help error-text", style: "margin-top:6px", text: k.error }) : null,
      el("div", { class: "list", style: "margin-top:12px" }, ...k.folders.map((f) => el("div", { class: "list-item" },
        el("span", { class: "tool-icon" }, icon("folder-open", "icon-sm")),
        el("div", { class: "grow" },
          el("div", { class: "name" }, f.label, el("span", { class: "badge", text: `${f.files} files` }), f.exists ? null : el("span", { class: "badge badge-danger", text: "missing" })),
          el("div", { class: "desc mono", text: f.path }),
        ),
        f.removable
          ? el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Remove", "aria-label": `Remove ${f.label}`, onclick: async () => {
              state.knowledge = await api.del(`/api/knowledge/folders?path=${encodeURIComponent(f.path)}`).catch((err) => (toast(err.message, { type: "error" }), state.knowledge));
              renderStatus(ctx);
            } }, icon("x", "icon-sm"))
          : el("span", { class: "subtle", style: "font-size:.74rem", text: f.vault ? "vault" : "always included" }),
      ))),
    ),
    el("div", { class: "section" },
      el("div", { class: "section-title", text: "Search by meaning" }),
      field("Embedding model", embedSelect, { help: "Automatic uses an installed embedding model if there is one." }),
      pullBox,
    ),
  ));
}
