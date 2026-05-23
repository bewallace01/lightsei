// Phase 21.4: customer-side widget loader.
//
// A tiny IIFE (no module system, no build) that customers paste onto
// their own site:
//
//   <script src="https://app.lightsei.com/widget.js"
//           data-workspace="wid_xxxx"
//           async></script>
//
// What it does:
//
//   1. Reads `data-workspace` from its own <script> tag to find the
//      Lightsei widget public id.
//   2. Computes the Lightsei origin from its own src attribute (so a
//      script served from localhost:3000 talks to localhost:3000,
//      and one served from app.lightsei.com talks to app.lightsei.com).
//   3. Injects:
//        - a fixed-position chat-bubble launcher in the bottom-right
//        - a fixed-position iframe (initially hidden) pointing at
//          {origin}/widget/{public_id}
//   4. Toggles iframe visibility when the launcher is clicked.
//   5. Listens for `{type: "lightsei:widget-resize", height}` postMessage
//      from the iframe (the 21.3 page emits these on every reflow) and
//      sizes the iframe to match.
//   6. Exposes `window.Lightsei.{open, close, toggle, mount}` so the
//      customer can wire their own launcher if they don't want the
//      default bubble.
//
// Cross-frame trust model: postMessage origin is verified against the
// computed Lightsei origin. The iframe's CSP / same-origin policy is
// the actual sandbox; this script only manages layout.

(function () {
  "use strict";

  if (typeof window === "undefined") return;
  // Idempotent — re-running the script must not double-mount.
  if (window.__lightsei_widget_loaded__) return;
  window.__lightsei_widget_loaded__ = true;

  // ---------- Bootstrap: find own <script> tag ---------- //

  // document.currentScript is the canonical way to find "the tag that
  // loaded this code." Falls back to scanning script tags for one that
  // matches our src; the fallback covers some script-loader patterns.
  function findOwnScript() {
    if (document.currentScript) return document.currentScript;
    var scripts = document.getElementsByTagName("script");
    for (var i = 0; i < scripts.length; i++) {
      if ((scripts[i].src || "").indexOf("/widget.js") !== -1) {
        return scripts[i];
      }
    }
    return null;
  }

  var ownScript = findOwnScript();
  if (!ownScript) {
    // No way to figure out which workspace to load. Bail silently —
    // graceful degradation per CLAUDE.md.
    return;
  }

  var publicId = ownScript.getAttribute("data-workspace");
  if (!publicId) {
    if (window.console && console.warn) {
      console.warn(
        "Lightsei widget: data-workspace not set on the <script> tag; skipping."
      );
    }
    return;
  }

  // Lightsei origin = the origin the script came from. Strip any
  // trailing path; we just want scheme + host + port.
  var lightseiOrigin = (function () {
    try {
      var u = new URL(ownScript.src, window.location.href);
      return u.origin;
    } catch (e) {
      return null;
    }
  })();
  if (!lightseiOrigin) return;

  var iframeUrl = lightseiOrigin + "/widget/" + encodeURIComponent(publicId);


  // ---------- DOM scaffolding ---------- //

  // All Lightsei UI lives inside a single container so the customer
  // can scope styles or remove it cleanly. position:fixed so it
  // doesn't shift around when the customer's page layout changes.
  var container = null;
  var iframe = null;
  var launcher = null;
  var open = false;

  function mount() {
    if (container) return;

    container = document.createElement("div");
    container.id = "lightsei-widget-root";
    container.style.cssText = [
      "position:fixed",
      "right:20px",
      "bottom:20px",
      "z-index:2147483646", // one below int32 max — high but reserves the top for modals
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
    ].join(";");

    // Iframe (initially hidden).
    iframe = document.createElement("iframe");
    iframe.src = iframeUrl;
    iframe.title = "Chat";
    iframe.allow = "clipboard-write";
    iframe.style.cssText = [
      "display:none",
      "width:380px",
      "height:560px",
      "max-width:calc(100vw - 40px)",
      "max-height:calc(100vh - 100px)",
      "border:1px solid #e5e7eb",
      "border-radius:12px",
      "box-shadow:0 12px 40px rgba(0,0,0,0.18)",
      "background:#fff",
      "margin-bottom:12px"
    ].join(";");

    // Launcher button (the floating chat bubble).
    launcher = document.createElement("button");
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Open chat");
    launcher.style.cssText = [
      "display:block",
      "margin-left:auto",
      "width:56px",
      "height:56px",
      "border-radius:50%",
      "border:none",
      "background:#4f46e5",
      "color:#fff",
      "cursor:pointer",
      "box-shadow:0 6px 20px rgba(79,70,229,0.35)",
      "font-size:24px",
      "line-height:1",
      "transition:transform 0.15s ease",
      "padding:0"
    ].join(";");
    launcher.innerHTML = chatIcon();
    launcher.addEventListener("click", toggle);
    launcher.addEventListener("mouseenter", function () {
      launcher.style.transform = "scale(1.05)";
    });
    launcher.addEventListener("mouseleave", function () {
      launcher.style.transform = "scale(1)";
    });

    container.appendChild(iframe);
    container.appendChild(launcher);

    // Mount after DOM is ready (the script is loaded async; the body
    // might not exist yet if a customer pastes it in <head>).
    if (document.body) {
      document.body.appendChild(container);
    } else {
      document.addEventListener("DOMContentLoaded", function () {
        document.body.appendChild(container);
      });
    }
  }


  // ---------- Launcher SVG ---------- //

  function chatIcon() {
    // Inline SVG so there's no extra request.
    return (
      '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ' +
      'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>' +
      "</svg>"
    );
  }

  function closeIcon() {
    return (
      '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ' +
      'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round">' +
      '<line x1="18" y1="6" x2="6" y2="18"/>' +
      '<line x1="6" y1="6" x2="18" y2="18"/>' +
      "</svg>"
    );
  }


  // ---------- Open / close / toggle ---------- //

  function setOpen(next) {
    if (!iframe || !launcher) return;
    open = !!next;
    iframe.style.display = open ? "block" : "none";
    launcher.setAttribute("aria-label", open ? "Close chat" : "Open chat");
    launcher.innerHTML = open ? closeIcon() : chatIcon();
  }

  function openWidget() { setOpen(true); }
  function closeWidget() { setOpen(false); }
  function toggle() { setOpen(!open); }


  // ---------- iframe sizing via postMessage ---------- //

  // The 21.3 iframe app observes its own body height and posts
  // {type: "lightsei:widget-resize", height} on every reflow. We
  // respect a sensible cap (max-height in CSS handles smaller
  // viewports) so a broken iframe can't make the launcher unreachable.
  window.addEventListener("message", function (event) {
    if (event.origin !== lightseiOrigin) return;
    var data = event.data || {};
    if (data.type === "lightsei:widget-resize" && typeof data.height === "number") {
      if (!iframe) return;
      var capped = Math.max(200, Math.min(800, Math.round(data.height)));
      iframe.style.height = capped + "px";
    }
  });


  // ---------- Public API ---------- //

  window.Lightsei = window.Lightsei || {};
  window.Lightsei.mount = mount;
  window.Lightsei.open = openWidget;
  window.Lightsei.close = closeWidget;
  window.Lightsei.toggle = toggle;


  // ---------- Auto-mount ---------- //

  mount();
})();
