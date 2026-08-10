/*
 * share.js — copy-to-clipboard for the per-card share block.
 * ==========================================================
 * Distribution doctrine (maddi-distribution v2.0): humans post, machines only
 * draft. This never auto-posts anywhere — it copies a ready-to-paste block
 * (already carrying the honest "I built this" disclosure) that the BUILD wrote
 * into a JSON payload, and the person pastes it where the question came up.
 *
 * The copy text is the card's own export.reddit / export.discord (built in
 * phantom-ops card_export.build_export_artifacts, stored on the card, rendered
 * verbatim here) — this script chooses which one and writes it to the clipboard.
 * Event-delegated so it costs nothing on cards without a share block.
 */
(function () {
  "use strict";

  function flash(note, msg) {
    if (!note) return;
    note.textContent = msg;
    clearTimeout(note._t);
    note._t = setTimeout(function () { note.textContent = ""; }, 2600);
  }

  function copyText(text, ok, bad) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, bad);
      return;
    }
    // execCommand fallback for older / non-secure contexts.
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy") ? ok() : bad(); }
    catch (_) { bad(); }
    document.body.removeChild(ta);
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-share]");
    if (!btn) return;
    var section = btn.closest(".card-share");
    if (!section) return;
    e.preventDefault();

    var note = section.querySelector("[data-share-note]");
    var payloadEl = section.querySelector("[data-share-payload]");
    var payload = {};
    try { payload = JSON.parse(payloadEl.textContent); } catch (_) {}

    var kind = btn.getAttribute("data-share"); // "reddit" | "discord" | "permalink"
    var text = kind === "permalink" ? payload.permalink : payload[kind];
    if (!text) { flash(note, "Nothing to copy."); return; }

    copyText(
      text,
      function () {
        var what = kind === "permalink" ? "Link copied." :
          "Copied — paste it where the question came up.";
        flash(note, what);
      },
      function () { flash(note, "Copy failed — select the text and copy manually."); }
    );
  });
})();
