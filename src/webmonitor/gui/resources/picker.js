/*
 * Element picker injected into every page loaded by the inspector.
 *
 * Behaviour, deliberately close to a browser's own element inspector:
 *   - hovering outlines the element under the cursor and shows a tag/size badge
 *   - clicking locks that element in and reports a CSS selector to Python
 *   - Escape cancels, ArrowUp walks to the parent, ArrowDown back to the child
 *
 * The hard part is not the highlighting, it is generating a selector that still
 * matches tomorrow. See buildSelector() for the stability heuristics.
 */
(function () {
  "use strict";

  if (window.__wmPicker) {
    return; // Already injected into this frame.
  }

  var OVERLAY_ID = "__wm_picker_overlay";
  var BADGE_ID = "__wm_picker_badge";

  // --- Class names that are generated, stateful, or otherwise not stable ----
  // Matching any of these means the class is a poor selector ingredient.
  var UNSTABLE_CLASS = [
    /^(is|has|js|ng|v|data)[-_]/i,      // state and framework hooks
    /^(active|open|show|hidden|selected|current|focus|hover|loading|visible)$/i,
    /[0-9a-f]{6,}/i,                     // hashed CSS-module suffixes
    /^[a-z]+_[a-z0-9]{5,}$/i,            // styled-components / emotion
    /^css-[a-z0-9]+$/i,                  // emotion
    /^sc-[a-z0-9]+$/i,                   // styled-components
    /^_[a-zA-Z0-9]{5,}$/,                // CSS modules
    /\d{3,}/                             // long digit runs
  ];

  // IDs that are clearly generated per page load.
  var UNSTABLE_ID = [
    /^[0-9]/,                            // not a valid bare CSS ident anyway
    /^(ember|react|radix|headlessui|mui|aria)[-:]?/i,
    /^:r[0-9a-z]+:?$/i,                  // React useId
    /[0-9a-f]{8,}/i,
    /\d{4,}/
  ];

  function isStableClass(name) {
    if (!name || name.length > 40) return false;
    for (var i = 0; i < UNSTABLE_CLASS.length; i++) {
      if (UNSTABLE_CLASS[i].test(name)) return false;
    }
    return true;
  }

  function isStableId(id) {
    if (!id || id.length > 50) return false;
    for (var i = 0; i < UNSTABLE_ID.length; i++) {
      if (UNSTABLE_ID[i].test(id)) return false;
    }
    return true;
  }

  function cssEscape(value) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
    return String(value).replace(/([^\w-])/g, "\\$1");
  }

  function stableClasses(el) {
    if (!el.classList || !el.classList.length) return [];
    var out = [];
    for (var i = 0; i < el.classList.length && out.length < 3; i++) {
      var name = el.classList[i];
      if (isStableClass(name)) out.push(name);
    }
    return out;
  }

  /* Preferred attributes, in descending order of how deliberate they are.
     A `data-testid` was put there on purpose and rarely churns. */
  var GOOD_ATTRS = ["data-testid", "data-test", "data-qa", "data-id", "itemprop", "name", "role"];

  function attributeSelector(el) {
    for (var i = 0; i < GOOD_ATTRS.length; i++) {
      var attr = GOOD_ATTRS[i];
      var value = el.getAttribute && el.getAttribute(attr);
      if (value && value.length < 50 && isStableClass(value.replace(/[^a-z0-9_-]/gi, ""))) {
        return "[" + attr + '="' + value.replace(/"/g, '\\"') + '"]';
      }
    }
    return null;
  }

  function isUnique(selector, root) {
    try {
      return (root || document).querySelectorAll(selector).length === 1;
    } catch (e) {
      return false;
    }
  }

  /* One path segment for an element: tag, plus whatever cheap qualifiers make
     it distinguishable from its siblings. */
  function segmentFor(el) {
    var tag = el.tagName.toLowerCase();

    var attr = attributeSelector(el);
    if (attr) return tag + attr;

    var classes = stableClasses(el);
    var segment = tag + (classes.length ? "." + classes.map(cssEscape).join(".") : "");

    // Disambiguate against siblings that would match the same segment.
    var parent = el.parentElement;
    if (parent) {
      var twins = [];
      for (var i = 0; i < parent.children.length; i++) {
        var child = parent.children[i];
        if (child.tagName !== el.tagName) continue;
        var childClasses = stableClasses(child);
        if (classes.length === 0 || sameClasses(childClasses, classes)) twins.push(child);
      }
      if (twins.length > 1) {
        segment += ":nth-of-type(" + (indexOfType(el) + 1) + ")";
      }
    }
    return segment;
  }

  function sameClasses(a, b) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  function indexOfType(el) {
    var index = 0;
    var sibling = el.previousElementSibling;
    while (sibling) {
      if (sibling.tagName === el.tagName) index++;
      sibling = sibling.previousElementSibling;
    }
    return index;
  }

  /*
   * Build a CSS selector for `el`.
   *
   * Strategy, cheapest and most stable first:
   *   1. A hand-written id      -> `#main-content`
   *   2. A deliberate attribute -> `[data-testid="cart"]`
   *   3. A path of tag+class segments, extended toward the root only until it
   *      is unique. Anchoring on an id ancestor stops the walk early and keeps
   *      the selector short, which also makes it robust to layout changes
   *      higher up the tree.
   */
  function buildSelector(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el === document.body) return "body";
    if (el === document.documentElement) return "html";

    if (el.id && isStableId(el.id)) {
      var byId = "#" + cssEscape(el.id);
      if (isUnique(byId)) return byId;
    }

    var attr = attributeSelector(el);
    if (attr && isUnique(attr)) return attr;

    var parts = [];
    var node = el;
    var depth = 0;

    while (node && node.nodeType === 1 && depth < 8) {
      if (node === document.body) {
        parts.unshift("body");
        break;
      }

      // An ancestor with a good id is the best possible anchor: prepend it and
      // stop, rather than walking all the way to <body>.
      if (node !== el && node.id && isStableId(node.id) && isUnique("#" + cssEscape(node.id))) {
        parts.unshift("#" + cssEscape(node.id));
        break;
      }

      parts.unshift(segmentFor(node));

      var candidate = parts.join(" > ");
      if (isUnique(candidate)) return candidate;

      node = node.parentElement;
      depth++;
    }

    var selector = parts.join(" > ");
    // Last resort: if still ambiguous, pin the exact position in the match set.
    if (!isUnique(selector)) {
      try {
        var matches = document.querySelectorAll(selector);
        for (var i = 0; i < matches.length; i++) {
          if (matches[i] === el) {
            return selector + ":nth-of-type(" + (indexOfType(el) + 1) + ")";
          }
        }
      } catch (e) { /* fall through */ }
    }
    return selector;
  }

  // ------------------------------------------------------------------
  // Overlay
  // ------------------------------------------------------------------
  function ensureOverlay() {
    var overlay = document.getElementById(OVERLAY_ID);
    if (overlay) return overlay;

    overlay = document.createElement("div");
    overlay.id = OVERLAY_ID;
    overlay.style.cssText = [
      "position:fixed", "pointer-events:none", "z-index:2147483646",
      "background:rgba(59,130,246,0.22)",
      "border:2px solid #3b82f6",
      "border-radius:3px",
      "box-shadow:0 0 0 1px rgba(255,255,255,.35), 0 4px 18px rgba(0,0,0,.35)",
      "transition:all .04s linear", "display:none"
    ].join(";");

    var badge = document.createElement("div");
    badge.id = BADGE_ID;
    badge.style.cssText = [
      "position:fixed", "pointer-events:none", "z-index:2147483647",
      "background:#3b82f6", "color:#fff",
      "font:600 11px/1.5 ui-monospace,Consolas,monospace",
      "padding:3px 8px", "border-radius:5px", "white-space:nowrap",
      "box-shadow:0 2px 10px rgba(0,0,0,.4)", "display:none"
    ].join(";");

    (document.body || document.documentElement).appendChild(overlay);
    (document.body || document.documentElement).appendChild(badge);
    return overlay;
  }

  function paint(el) {
    var overlay = ensureOverlay();
    var badge = document.getElementById(BADGE_ID);
    if (!el || !el.getBoundingClientRect) {
      overlay.style.display = "none";
      badge.style.display = "none";
      return;
    }

    var rect = el.getBoundingClientRect();
    overlay.style.display = "block";
    overlay.style.left = rect.left + "px";
    overlay.style.top = rect.top + "px";
    overlay.style.width = rect.width + "px";
    overlay.style.height = rect.height + "px";

    var classes = stableClasses(el);
    badge.textContent =
      el.tagName.toLowerCase() +
      (el.id && isStableId(el.id) ? "#" + el.id : "") +
      (classes.length ? "." + classes.join(".") : "") +
      "  " + Math.round(rect.width) + "x" + Math.round(rect.height);

    badge.style.display = "block";
    // Prefer above the element; flip below when there is no room.
    var top = rect.top - 24;
    badge.style.top = (top < 4 ? rect.bottom + 6 : top) + "px";
    badge.style.left = Math.max(4, rect.left) + "px";
  }

  function clearOverlay() {
    var overlay = document.getElementById(OVERLAY_ID);
    var badge = document.getElementById(BADGE_ID);
    if (overlay) overlay.remove();
    if (badge) badge.remove();
  }

  // ------------------------------------------------------------------
  // Picker state machine
  // ------------------------------------------------------------------
  var picker = {
    active: false,
    current: null,

    send: function (method, payload) {
      if (window.__wmBridge && typeof window.__wmBridge[method] === "function") {
        window.__wmBridge[method](payload);
      }
    },

    onMove: function (event) {
      if (!picker.active) return;
      var el = event.target;
      if (!el || el.id === OVERLAY_ID || el.id === BADGE_ID) return;
      if (el === picker.current) return;
      picker.current = el;
      paint(el);
      picker.send("hoverChanged", buildSelector(el));
    },

    onClick: function (event) {
      if (!picker.active) return;
      event.preventDefault();
      event.stopPropagation();

      /* Trust the click target, not the last hovered element. They are the same
         during normal mouse use, but they diverge whenever a hover event was
         missed or arrived stale - and then `current` would silently pick some
         ancestor the user never aimed at. The Enter-key path synthesises an
         event whose target IS `current`, so it still works. */
      var el = event.target;
      if (!el || el.nodeType !== 1 || el.id === OVERLAY_ID || el.id === BADGE_ID) {
        el = picker.current;
      }
      if (!el) return;

      /* Remove our own overlay and badge before reading text. They are appended
         to <body>, so picking <body> or <html> would otherwise capture the
         badge's own label as part of the page content. */
      clearOverlay();

      var selector = buildSelector(el);
      var count = 0;
      try { count = document.querySelectorAll(selector).length; } catch (e) { count = 0; }

      picker.send("elementPicked", JSON.stringify({
        selector: selector,
        text: (el.innerText || el.textContent || "").trim().slice(0, 1200),
        html: (el.outerHTML || "").slice(0, 4000),
        tag: el.tagName.toLowerCase(),
        matches: count
      }));
      picker.stop();
    },

    onKey: function (event) {
      if (!picker.active) return;

      if (event.key === "Escape") {
        event.preventDefault();
        picker.send("pickCancelled", "");
        picker.stop();
        return;
      }
      // Walk the tree without moving the mouse - the usual way to grab a
      // container when the cursor can only reach its child.
      if (event.key === "ArrowUp" && picker.current && picker.current.parentElement) {
        event.preventDefault();
        picker.current = picker.current.parentElement;
        paint(picker.current);
        picker.send("hoverChanged", buildSelector(picker.current));
      }
      if (event.key === "ArrowDown" && picker.current && picker.current.children.length) {
        event.preventDefault();
        picker.current = picker.current.children[0];
        paint(picker.current);
        picker.send("hoverChanged", buildSelector(picker.current));
      }
      if (event.key === "Enter" && picker.current) {
        event.preventDefault();
        picker.onClick({
          target: picker.current,
          preventDefault: function () {},
          stopPropagation: function () {}
        });
      }
    },

    start: function () {
      if (picker.active) return;
      picker.active = true;
      picker.current = null;
      ensureOverlay();
      document.addEventListener("mousemove", picker.onMove, true);
      document.addEventListener("click", picker.onClick, true);
      document.addEventListener("keydown", picker.onKey, true);
      document.body.style.cursor = "crosshair";
    },

    stop: function () {
      picker.active = false;
      picker.current = null;
      document.removeEventListener("mousemove", picker.onMove, true);
      document.removeEventListener("click", picker.onClick, true);
      document.removeEventListener("keydown", picker.onKey, true);
      if (document.body) document.body.style.cursor = "";
      clearOverlay();
    },

    /* Highlight an existing selector so the user can confirm a saved target
       still points where they think it does. */
    preview: function (selector) {
      clearOverlay();
      if (!selector) return 0;
      var nodes;
      try { nodes = document.querySelectorAll(selector); } catch (e) { return -1; }
      if (nodes.length) paint(nodes[0]);
      return nodes.length;
    }
  };

  window.__wmPicker = picker;

  // Keep the outline glued to the element while the page moves under it.
  window.addEventListener("scroll", function () {
    if (picker.active && picker.current) paint(picker.current);
  }, true);
  window.addEventListener("resize", function () {
    if (picker.active && picker.current) paint(picker.current);
  }, true);
})();
