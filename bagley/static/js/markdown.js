// Markdown rendering: marked for parsing, DOMPurify for safety, plus code-block chrome.

import DOMPurify from "../vendor/purify.esm.js";
import { Marked } from "../vendor/marked.esm.js";
import { icon } from "./util.js";

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const marked = new Marked({
  gfm: true,
  breaks: false,
  renderer: {
    code({ text, lang }) {
      const language = (lang || "").split(/\s/)[0];
      return (
        `<div class="code-block"><div class="code-head"><span>${escapeHtml(language || "text")}</span>` +
        `<button class="icon-btn icon-btn-sm" data-copy-code aria-label="Copy code" title="Copy code"></button></div>` +
        `<pre><code>${escapeHtml(text)}</code></pre></div>`
      );
    },
    image({ href, text }) {
      // Never load remote images from model output; offer a link the user can choose to open.
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">Image: ${escapeHtml(text || href)}</a>`;
    },
    link({ href, title, tokens }) {
      const label = this.parser.parseInline(tokens);
      const t = title ? ` title="${escapeHtml(title)}"` : "";
      return `<a href="${escapeHtml(href)}"${t} target="_blank" rel="noopener noreferrer">${label}</a>`;
    },
  },
});

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

// Model output is untrusted (it can echo web pages). No images or media, so nothing is fetched
// from elsewhere, and ids are namespaced so they cannot shadow the app's own elements.
const PURIFY = {
  ADD_ATTR: ["target", "data-copy-code"],
  FORBID_TAGS: ["style", "form", "input", "img", "picture", "video", "audio", "source", "dialog", "iframe", "object", "embed", "svg", "math"],
  FORBID_ATTR: ["style", "srcset"],
  SANITIZE_NAMED_PROPS: true,
};

export function renderMarkdown(text) {
  const html = marked.parse(text || "", { async: false });
  return DOMPurify.sanitize(html, PURIFY);
}

/** Render markdown into `node` and add the controls that sanitizing strips. */
export function setMarkdown(node, text) {
  node.innerHTML = renderMarkdown(text);
  for (const btn of node.querySelectorAll("button[data-copy-code]")) btn.append(icon("copy", "icon-sm"));
}

/** Plain text for speech and copy: drop code blocks and markdown punctuation. */
export function plainText(markdown) {
  return (markdown || "")
    .replace(/```[\s\S]*?```/g, " (code omitted) ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s*[|:-]+\s*$/gm, "")
    .replace(/[*_#>|~]/g, "")
    .replace(/\n{2,}/g, "\n")
    .trim();
}
