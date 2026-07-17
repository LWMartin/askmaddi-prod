/*
 * beacon.js — Phase 0 distribution measurement (maddi-distribution v2.0).
 * =======================================================================
 * Three jobs, all anonymous:
 *   1. AI-referrer detection: if document.referrer is a known AI engine,
 *      fire {event:'ai_referral', engine, category} once per page load.
 *   2. Outbound clicks: any <a data-out> click fires
 *      {event:'outbound', category, retailer} — the link itself is untouched
 *      (direct affiliate href; no redirect hop, no attribution risk).
 *   3. Subscribe form: posts email + honeypot to /subscribe, renders result.
 *
 * Privacy doctrine (inherits the /ping line): no cookies, no localStorage,
 * no user IDs, no URLs, no query text. Category and retailer come from
 * data-attributes the BUILD wrote — never from user input. The gateway
 * whitelists every value server-side anyway; this script is the polite
 * client of a suspicious server.
 *
 * sendBeacon is used so outbound clicks are never delayed or lost to
 * navigation; fetch(keepalive) is the fallback.
 */
(function () {
  'use strict';

  // Same-origin root-relative, matching app.js (this.gateway = ''):
  // Apache proxies /ping, /subscribe etc. straight to the gateway on :5001.
  var GATEWAY = '';

  // Mirror of gateway analytics_log.AI_ENGINES (minus 'other').
  // Additions must be made in BOTH places.
  var AI_REFERRERS = [
    ['chatgpt', /(^|\.)chatgpt\.com$/],
    ['chatgpt', /(^|\.)chat\.openai\.com$/],
    ['perplexity', /(^|\.)perplexity\.ai$/],
    ['gemini', /(^|\.)gemini\.google\.com$/],
    ['gemini', /(^|\.)bard\.google\.com$/],
    ['copilot', /(^|\.)copilot\.microsoft\.com$/],
    ['claude', /(^|\.)claude\.ai$/]
  ];

  function pageCategory() {
    var b = document.body;
    return (b && b.getAttribute('data-category')) || 'unknown';
  }

  function send(payload) {
    var url = GATEWAY + '/ping';
    var body = JSON.stringify(payload);
    try {
      if (navigator.sendBeacon) {
        // sendBeacon with a Blob keeps Content-Type application/json.
        var blob = new Blob([body], { type: 'application/json' });
        if (navigator.sendBeacon(url, blob)) return;
      }
    } catch (e) { /* fall through */ }
    try {
      fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body,
        keepalive: true
      });
    } catch (e) { /* measurement must never break the page */ }
  }

  // ── 1. AI referral (once per load) ────────────────────────────────────
  function detectAiReferral() {
    var ref = document.referrer;
    if (!ref) return;
    var host;
    try { host = new URL(ref).hostname.toLowerCase(); } catch (e) { return; }
    for (var i = 0; i < AI_REFERRERS.length; i++) {
      if (AI_REFERRERS[i][1].test(host)) {
        send({ event: 'ai_referral', engine: AI_REFERRERS[i][0],
               category: pageCategory() });
        return;
      }
    }
  }

  // ── 2. Outbound clicks (delegated; auxclick covers middle-click) ──────
  function onOutClick(ev) {
    var el = ev.target;
    while (el && el !== document.body) {
      if (el.tagName === 'A' && el.hasAttribute('data-out')) {
        send({
          event: 'outbound',
          category: el.getAttribute('data-category') || pageCategory(),
          retailer: el.getAttribute('data-retailer') || 'other'
        });
        return;   // link proceeds untouched — no preventDefault, ever
      }
      el = el.parentElement;
    }
  }

  // ── 3. Subscribe form ─────────────────────────────────────────────────
  function wireSubscribe() {
    var forms = document.querySelectorAll('form[data-subscribe]');
    Array.prototype.forEach.call(forms, function (form) {
      form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var email = (form.querySelector('input[name="email"]') || {}).value;
        var website = (form.querySelector('input[name="website"]') || {}).value;
        var note = form.querySelector('.subscribe-note');
        fetch(GATEWAY + '/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: email, website: website || '' })
        }).then(function (r) { return r.json().catch(function () { return {}; }); })
          .then(function (data) {
            if (note) {
              note.textContent = data.ok
                ? "You're on the list — new cards weekly."
                : (data.error || 'Something went wrong — please try again.');
              note.className = 'subscribe-note ' + (data.ok ? 'ok' : 'err');
            }
            if (data.ok) form.reset();
          })
          .catch(function () {
            if (note) {
              note.textContent = 'Network hiccup — please try again.';
              note.className = 'subscribe-note err';
            }
          });
      });
    });
  }

  function init() {
    detectAiReferral();
    document.addEventListener('click', onOutClick, true);
    document.addEventListener('auxclick', onOutClick, true);
    wireSubscribe();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
