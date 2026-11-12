/*
 * Install-as-an-app, automatic updates, offline marker, version labels and
 * the "What's new" card. Plain JavaScript, no build step, no framework.
 *
 * Everything is feature-detected: a browser without service workers, install
 * prompts, MessageChannel or localStorage simply doesn't show that feature.
 *
 * Page hooks (all optional; add the attribute and this file fills it in):
 *   [data-app-version]   "vX.Y.Z", release date on hover
 *   [data-offline-marker] shown only while the device is offline
 *   [data-install-button] shown only when the app can be installed
 *   [data-open-about]     opens the About & updates dialog
 *   #whatsNew             "What's new" card (main page only)
 *
 * State for other scripts (the plain-JS version of a usePwa() hook):
 *   SMT.usePwa()          -> { updateReady, canInstall, installed, online }
 *   SMT.usePwa(callback)  -> calls back now and on every change; returns an unsubscribe function
 */
(function () {
  "use strict";

  var V = self.SMT_VERSION || { APP_VERSION: "0.0.0", APP_RELEASE_DATE: "", RELEASE_NOTES: [] };
  var LAST_SEEN_KEY = "smt-last-version";
  var HOUR = 60 * 60 * 1000;

  var state = { updateReady: false, canInstall: false, installed: false, online: navigator.onLine !== false };
  var installMode = null;      // "prompt" | "ios" | "mac-safari" | null
  var deferredPrompt = null;   // beforeinstallprompt event (Chrome, Edge, Android)
  var registration = null;
  var waitingWorker = null;
  var updateRequested = false; // reload only after the person chose "Update now"
  var reloading = false;
  var barDismissed = false;
  var listeners = [];

  function snapshot() {
    return { updateReady: state.updateReady, canInstall: state.canInstall, installed: state.installed, online: state.online };
  }
  function emit() {
    render();
    listeners.slice().forEach(function (fn) { try { fn(snapshot()); } catch (e) { /* a listener's bug isn't ours */ } });
  }

  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtDate(iso) {
    if (!iso) return "";
    var p = iso.split("-").map(Number);
    try {
      return new Date(Date.UTC(p[0], p[1] - 1, p[2])).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
    } catch (e) { return iso; }
  }

  // ------------------------------------------------------------------
  // Installed? Installable?
  // ------------------------------------------------------------------
  function isStandalone() {
    try {
      if (window.matchMedia && (window.matchMedia("(display-mode: standalone)").matches ||
                                window.matchMedia("(display-mode: window-controls-overlay)").matches)) return true;
    } catch (e) { /* no matchMedia */ }
    return window.navigator.standalone === true; // iPhone / iPad home-screen apps
  }

  function detectManualInstall() {
    var ua = navigator.userAgent || "";
    var touchMac = navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1; // iPadOS reports as a Mac
    if (/iPhone|iPad|iPod/.test(ua) || touchMac) return "ios";
    var isMac = /Macintosh/.test(ua);
    var isSafari = /Safari\//.test(ua) && !/Chrome|Chromium|CriOS|Edg|OPR|Firefox|FxiOS/.test(ua);
    var ver = (ua.match(/Version\/(\d+)/) || [])[1];
    if (isMac && isSafari && ver && Number(ver) >= 17) return "mac-safari"; // "Add to Dock" arrived in Safari 17
    return null;
  }

  function refreshInstallState() {
    state.installed = isStandalone();
    installMode = deferredPrompt ? "prompt" : detectManualInstall();
    state.canInstall = !state.installed && !!installMode;
  }

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault(); // keep the browser's own mini-bar away; we show our button instead
    deferredPrompt = e;
    refreshInstallState();
    emit();
  });
  window.addEventListener("appinstalled", function () {
    deferredPrompt = null;
    state.installed = true;
    state.canInstall = false;
    closeModal();
    emit();
  });
  try {
    window.matchMedia("(display-mode: standalone)").addEventListener("change", function () { refreshInstallState(); emit(); });
  } catch (e) { /* older browsers */ }

  function install() {
    if (installMode === "prompt" && deferredPrompt) {
      var ev = deferredPrompt;
      deferredPrompt = null; // a prompt can only be used once
      ev.prompt();
      var after = function () { refreshInstallState(); emit(); };
      if (ev.userChoice && ev.userChoice.then) ev.userChoice.then(after, after); else after();
      return;
    }
    if (installMode === "ios") {
      openModal("Install on iPhone or iPad",
        '<ol class="smt-steps">' +
          '<li>Tap the <b>Share</b> button <span class="smt-kbd" aria-hidden="true">' + SHARE_ICON + "</span> in Safari's toolbar.</li>" +
          "<li>Scroll down and tap <b>Add to Home Screen</b>.</li>" +
          "<li>Tap <b>Add</b>. The tracker then opens like an app, full screen.</li>" +
        "</ol>");
      return;
    }
    if (installMode === "mac-safari") {
      openModal("Install on your Mac",
        '<ol class="smt-steps">' +
          "<li>In Safari's menu bar, choose <b>File → Add to Dock</b>.</li>" +
          "<li>Click <b>Add</b>. The tracker opens in its own window from the Dock.</li>" +
        "</ol>");
    }
  }
  var SHARE_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M8 7l4-4 4 4"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/></svg>';

  // ------------------------------------------------------------------
  // Online / offline
  // ------------------------------------------------------------------
  window.addEventListener("online", function () { state.online = true; emit(); });
  window.addEventListener("offline", function () { state.online = false; emit(); });

  // ------------------------------------------------------------------
  // Service worker and updates
  // ------------------------------------------------------------------
  var swSupported = !!navigator.serviceWorker && typeof navigator.serviceWorker.register === "function" &&
    (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1");

  function askVersion(worker) {
    return new Promise(function (resolve) {
      if (typeof MessageChannel === "undefined") return resolve(null);
      try {
        var ch = new MessageChannel();
        var timer = setTimeout(function () { resolve(null); }, 3000);
        ch.port1.onmessage = function (e) { clearTimeout(timer); resolve(e.data && e.data.version ? e.data.version : null); };
        worker.postMessage({ type: "GET_VERSION" }, [ch.port2]);
      } catch (e) { resolve(null); }
    });
  }

  function onWorkerWaiting(worker) {
    return askVersion(worker).then(function (version) {
      if (version && version === V.APP_VERSION) {
        // Same version (for example a data-only redeploy): hand over quietly,
        // no bar, no reload. The next page load uses the new worker.
        worker.postMessage({ type: "SKIP_WAITING" });
        return;
      }
      waitingWorker = worker;
      state.updateReady = true;
      barDismissed = false;
      emit();
    });
  }

  function watchInstalling(worker) {
    return new Promise(function (resolve) {
      if (!worker) return resolve();
      if (worker.state === "installed" || worker.state === "activated") return resolve();
      worker.addEventListener("statechange", function () {
        if (worker.state === "installed" || worker.state === "redundant" || worker.state === "activated") resolve();
      });
    }).then(function () {
      if (worker.state === "installed" && navigator.serviceWorker.controller) return onWorkerWaiting(worker);
    });
  }

  function applyUpdate() {
    if (!waitingWorker) { location.reload(); return; }
    updateRequested = true;
    waitingWorker.postMessage({ type: "SKIP_WAITING" });
    // If the browser never reports the switch, reload anyway after a moment.
    setTimeout(function () { if (!reloading) { reloading = true; location.reload(); } }, 4000);
  }

  function checkForUpdates() {
    if (!swSupported) return Promise.resolve("unsupported");
    if (!state.online) return Promise.resolve("offline");
    if (state.updateReady) return Promise.resolve("ready");
    if (!registration) return Promise.resolve("unavailable");
    return registration.update().then(function () {
      var w = registration.installing || registration.waiting;
      if (!w) return "latest";
      return watchInstalling(w).then(function () { return state.updateReady ? "ready" : "latest"; });
    }, function () { return "error"; });
  }

  if (swSupported) {
    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (updateRequested && !reloading) { reloading = true; location.reload(); }
    });
    var start = function () {
      navigator.serviceWorker.register("sw.js", { scope: "./", updateViaCache: "none" }).then(function (reg) {
        registration = reg;
        if (reg.waiting && navigator.serviceWorker.controller) onWorkerWaiting(reg.waiting);
        reg.addEventListener("updatefound", function () { watchInstalling(reg.installing); });
        setInterval(function () { if (state.online) reg.update().catch(function () {}); }, HOUR);
        document.addEventListener("visibilitychange", function () {
          if (document.visibilityState === "visible" && state.online) reg.update().catch(function () {});
        });
      }).catch(function () { /* no offline support this time; the site still works */ });
    };
    if (document.readyState === "complete") start(); else window.addEventListener("load", start);
  }

  // ------------------------------------------------------------------
  // UI: update bar, install buttons, offline marker, version, About, What's new
  // ------------------------------------------------------------------
  var bar = null;
  function renderBar() {
    var show = state.updateReady && !barDismissed;
    if (!bar && !show) return;
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "smt-update-bar";
      bar.setAttribute("role", "status");
      bar.setAttribute("aria-live", "polite");
      bar.innerHTML = '<span>New version ready</span>' +
        '<button type="button" class="smt-update-now">Update now</button>' +
        '<button type="button" class="smt-update-later" aria-label="Not now">×</button>';
      bar.querySelector(".smt-update-now").addEventListener("click", applyUpdate);
      bar.querySelector(".smt-update-later").addEventListener("click", function () { barDismissed = true; emit(); });
      document.body.appendChild(bar);
    }
    bar.hidden = !show;
  }

  function render() {
    if (!document.body) return;
    renderBar();
    each("[data-install-button]", function (el) { el.hidden = !state.canInstall; });
    each("[data-offline-marker]", function (el) { el.hidden = state.online; });
    var ab = document.getElementById("smtAboutStatus");
    if (ab && state.updateReady) ab.textContent = "A new version is ready.";
  }

  function each(sel, fn) {
    var els = document.querySelectorAll(sel);
    for (var i = 0; i < els.length; i++) fn(els[i]);
  }

  // ----- Modal (About, install steps). Built on demand.
  var modal = null, lastFocus = null;
  function openModal(title, bodyHtml) {
    closeModal();
    lastFocus = document.activeElement;
    modal = document.createElement("div");
    modal.className = "smt-modal-backdrop";
    modal.innerHTML =
      '<div class="smt-modal" role="dialog" aria-modal="true" aria-labelledby="smtModalTitle">' +
        '<div class="smt-modal-head"><h2 id="smtModalTitle">' + esc(title) + "</h2>" +
        '<button type="button" class="smt-modal-close" aria-label="Close">×</button></div>' +
        '<div class="smt-modal-body">' + bodyHtml + "</div>" +
      "</div>";
    modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });
    modal.querySelector(".smt-modal-close").addEventListener("click", closeModal);
    document.addEventListener("keydown", onModalKey);
    document.body.appendChild(modal);
    var first = modal.querySelector(".smt-modal-close");
    if (first) first.focus();
    return modal;
  }
  function onModalKey(e) {
    if (!modal) return;
    if (e.key === "Escape") { closeModal(); return; }
    if (e.key === "Tab") { // keep focus inside the dialog
      var f = modal.querySelectorAll("button, a[href], input, select");
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  function closeModal() {
    if (!modal) return;
    document.removeEventListener("keydown", onModalKey);
    modal.remove();
    modal = null;
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) { /* element gone */ } }
  }

  function notesHtml(entries) {
    return entries.map(function (r) {
      return '<div class="smt-note"><h3>v' + esc(r.version) + ' <span>' + esc(fmtDate(r.date)) + "</span></h3><ul>" +
        r.notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") + "</ul></div>";
    }).join("");
  }

  function openAbout() {
    var m = openModal("About Smart Money Tracker",
      '<p class="smt-about-version">Version <b>' + esc(V.APP_VERSION) + "</b>, released " + esc(fmtDate(V.APP_RELEASE_DATE)) + "</p>" +
      '<div class="smt-about-actions">' +
        '<button type="button" class="smt-btn" id="smtCheckUpdates">Check for updates</button>' +
        (state.canInstall ? '<button type="button" class="smt-btn smt-btn-quiet" id="smtAboutInstall">Install app</button>' : "") +
      "</div>" +
      '<p class="smt-about-status" id="smtAboutStatus" role="status" aria-live="polite"></p>' +
      '<div class="smt-about-notes"><h3 class="smt-about-h">Recent changes</h3>' + notesHtml(V.RELEASE_NOTES.slice(0, 3)) + "</div>");
    var status = m.querySelector("#smtAboutStatus");
    m.querySelector("#smtCheckUpdates").addEventListener("click", function () {
      var btn = this;
      btn.disabled = true;
      status.textContent = "Checking…";
      checkForUpdates().then(function (r) {
        btn.disabled = false;
        status.textContent = {
          latest: "You're on the latest version.",
          ready: "A new version is ready. Use the bar at the bottom, or reload the page.",
          offline: "You're offline. Connect to check for updates.",
          unsupported: "This browser doesn't support automatic updates. Reload the page to get the latest version.",
          unavailable: "Updates aren't available on this page yet. Reload the page to get the latest version.",
          error: "Couldn't check right now. Try again in a moment.",
        }[r] || "";
        if (r === "ready") emit();
      });
    });
    var ib = m.querySelector("#smtAboutInstall");
    if (ib) ib.addEventListener("click", install);
  }

  // ----- What's new (shown once per update, never on a first visit)
  function renderWhatsNew() {
    var host = document.getElementById("whatsNew");
    if (!host) return;
    var seen = lsGet(LAST_SEEN_KEY);
    if (!seen) { lsSet(LAST_SEEN_KEY, V.APP_VERSION); return; } // brand-new device: no card
    if (seen === V.APP_VERSION) return;
    lsSet(LAST_SEEN_KEY, V.APP_VERSION); // once per update
    host.innerHTML =
      '<section class="smt-whatsnew" aria-labelledby="smtWhatsNewTitle">' +
        '<button type="button" class="smt-whatsnew-close" aria-label="Close what\'s new">×</button>' +
        '<h2 id="smtWhatsNewTitle">What\'s new in v' + esc(V.APP_VERSION) + "</h2>" +
        notesHtml(V.RELEASE_NOTES.slice(0, 3)) +
      "</section>";
    host.querySelector(".smt-whatsnew-close").addEventListener("click", function () { host.innerHTML = ""; });
  }

  function init() {
    refreshInstallState();
    each("[data-app-version]", function (el) {
      el.textContent = "v" + V.APP_VERSION;
      el.title = "Released " + fmtDate(V.APP_RELEASE_DATE);
    });
    each("[data-install-button]", function (el) { el.addEventListener("click", install); });
    each("[data-open-about]", function (el) { el.addEventListener("click", openAbout); });
    renderWhatsNew();
    emit();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();

  window.SMT = window.SMT || {};
  window.SMT.usePwa = function (callback) {
    if (typeof callback !== "function") return snapshot();
    listeners.push(callback);
    callback(snapshot());
    return function () { listeners = listeners.filter(function (f) { return f !== callback; }); };
  };
  window.SMT.pwa = { install: install, applyUpdate: applyUpdate, checkForUpdates: checkForUpdates, openAbout: openAbout };
})();
