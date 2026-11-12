/*
 * Light / Dark / Auto theme. Loaded in <head> (not deferred) so the right
 * colours apply before the page paints, with no flash.
 *
 * The choice is stored in localStorage as "smt-theme". Auto follows the
 * device. Every failure (no localStorage, no matchMedia, no JS) falls back
 * to the light theme, which is the page's built-in default.
 *
 * Pages can listen for the "smt:themechange" event to redraw charts.
 * Any element with [data-theme-picker] becomes a sun/moon menu button.
 */
(function () {
  var KEY = "smt-theme";
  var CHOICES = ["light", "dark", "auto"];
  var LABELS = { light: "Light", dark: "Dark", auto: "Auto (device)" };
  var THEME_COLOR = { light: "#10264A", dark: "#0A1020" };

  function read() {
    try {
      var v = window.localStorage.getItem(KEY);
      return CHOICES.indexOf(v) >= 0 ? v : "auto";
    } catch (e) { return "auto"; }
  }
  function write(v) {
    try { window.localStorage.setItem(KEY, v); } catch (e) { /* private mode etc. */ }
  }

  var mq = null;
  try { mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null; } catch (e) { mq = null; }
  var choice = read();

  function resolved() {
    if (choice === "auto") return mq && mq.matches ? "dark" : "light";
    return choice;
  }

  var ICONS = {
    light: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/></svg>',
    dark: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>',
    auto: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/></svg>',
  };

  function apply(notify) {
    var t = resolved();
    var root = document.documentElement;
    root.setAttribute("data-theme", t);
    try { root.style.colorScheme = t; } catch (e) { /* old browsers */ }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", THEME_COLOR[t]);
    if (notify) {
      try { window.dispatchEvent(new CustomEvent("smt:themechange", { detail: { theme: t, choice: choice } })); } catch (e) { /* no CustomEvent */ }
    }
    syncPickers();
  }

  function set(v) {
    if (CHOICES.indexOf(v) < 0) return;
    choice = v;
    write(v);
    apply(true);
  }

  if (mq) {
    var onDeviceChange = function () { if (choice === "auto") apply(true); };
    if (mq.addEventListener) mq.addEventListener("change", onDeviceChange);
    else if (mq.addListener) mq.addListener(onDeviceChange);
  }

  // ----- The picker: an icon button that opens Light / Dark / Auto
  var pickers = [];

  function syncPickers() {
    for (var i = 0; i < pickers.length; i++) {
      var p = pickers[i];
      p.button.innerHTML = ICONS[choice];
      p.button.setAttribute("aria-label", "Colour theme: " + LABELS[choice] + ". Change theme");
      p.button.title = "Theme: " + LABELS[choice];
      for (var j = 0; j < p.items.length; j++) {
        var on = p.items[j].getAttribute("data-value") === choice;
        p.items[j].setAttribute("aria-checked", on ? "true" : "false");
      }
    }
  }

  function closeAll(except) {
    for (var i = 0; i < pickers.length; i++) {
      if (pickers[i] !== except) {
        pickers[i].menu.hidden = true;
        pickers[i].button.setAttribute("aria-expanded", "false");
      }
    }
  }

  function mount(host) {
    if (host.getAttribute("data-mounted")) return;
    host.setAttribute("data-mounted", "1");
    host.classList.add("theme-picker");
    var id = "themeMenu" + pickers.length;
    host.innerHTML =
      '<button type="button" class="theme-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="' + id + '"></button>' +
      '<div class="theme-menu" role="menu" id="' + id + '" aria-label="Colour theme" hidden>' +
      CHOICES.map(function (c) {
        return '<button type="button" role="menuitemradio" aria-checked="false" data-value="' + c + '">' +
          ICONS[c] + '<span>' + LABELS[c] + '</span></button>';
      }).join("") +
      "</div>";
    var p = { host: host, button: host.querySelector(".theme-btn"), menu: host.querySelector(".theme-menu"), items: host.querySelectorAll("[role=menuitemradio]") };
    pickers.push(p);

    function open() {
      closeAll(p);
      p.menu.hidden = false;
      p.button.setAttribute("aria-expanded", "true");
      var checked = p.menu.querySelector('[aria-checked="true"]') || p.items[0];
      if (checked) checked.focus();
    }
    function close(focusButton) {
      p.menu.hidden = true;
      p.button.setAttribute("aria-expanded", "false");
      if (focusButton) p.button.focus();
    }
    p.button.addEventListener("click", function (e) {
      e.stopPropagation();
      if (p.menu.hidden) open(); else close(false);
    });
    for (var i = 0; i < p.items.length; i++) {
      p.items[i].addEventListener("click", function (e) {
        e.stopPropagation();
        set(this.getAttribute("data-value"));
        close(true);
      });
    }
    p.menu.addEventListener("keydown", function (e) {
      var list = Array.prototype.slice.call(p.items);
      var idx = list.indexOf(document.activeElement);
      if (e.key === "Escape") { e.preventDefault(); close(true); }
      else if (e.key === "ArrowDown") { e.preventDefault(); list[(idx + 1) % list.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); list[(idx - 1 + list.length) % list.length].focus(); }
      else if (e.key === "Tab") { close(false); }
    });
    syncPickers();
  }

  function mountAll() {
    var hosts = document.querySelectorAll("[data-theme-picker]");
    for (var i = 0; i < hosts.length; i++) mount(hosts[i]);
  }

  document.addEventListener("click", function () { closeAll(null); });

  apply(false);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mountAll);
  else mountAll();

  window.SMT = window.SMT || {};
  window.SMT.theme = {
    get: function () { return { choice: choice, theme: resolved() }; },
    set: set,
  };
})();
