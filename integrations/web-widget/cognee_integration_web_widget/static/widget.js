/*
 * cognee web chat widget — embeddable snippet.
 *
 * Drop one tag on any page:
 *   <script src="https://your-host/widget.js"
 *           data-site-id="acme" data-api="https://your-host"></script>
 *
 * It renders a floating chat bubble, talks to the cognee-backed backend,
 * shows inline citations, and supports a /forget command + opt-out. No
 * build step, no framework.
 */
(function () {
  var script = document.currentScript;
  var API = (script && script.getAttribute("data-api")) || window.location.origin;
  var SITE_ID = (script && script.getAttribute("data-site-id")) || "demo";
  // Cited pages are resolved against the site the widget is embedded on, so
  // the same backend serves a local preview and a deployed site and each
  // links to itself. Override with data-docs-base when the docs are hosted
  // somewhere other than the page carrying the widget.
  var DOCS_BASE = (script && script.getAttribute("data-docs-base")) || window.location.origin;

  // Stable per-browser ids so a returning visitor keeps their conversation.
  function id(key, prefix) {
    var v = localStorage.getItem(key);
    if (!v) {
      v = prefix + "-" + Math.random().toString(36).slice(2, 10);
      localStorage.setItem(key, v);
    }
    return v;
  }
  var visitorId = id("cognee_visitor_id", "visitor");
  var conversationId = id("cognee_conversation_id", "conv");
  var optIn = localStorage.getItem("cognee_opt_in") !== "0";

  // The widget is embedded on sites whose themes we do not control, so every
  // rule that paints a background must also set a colour: inheriting the
  // host's text colour onto our own white panels renders the answer
  // invisible on any dark-themed site. The colour is set once on the box and
  // inherited; the few elements that want something else override it below.
  var css =
    ".cognee-w{position:fixed;bottom:20px;right:20px;width:360px;max-width:92vw;display:flex;flex-direction:column;align-items:flex-end;gap:10px;font:14px/1.5 system-ui,sans-serif;z-index:2147483000}" +
    ".cognee-box{display:none;width:100%;flex-direction:column;background:#fff;color:#111827;border:1px solid #e5e7eb;border-radius:12px;box-shadow:0 12px 32px rgba(0,0,0,.18);overflow:hidden}" +
    ".cognee-box.open{display:flex}" +
    ".cognee-head{background:#111827;color:#fff;padding:10px 14px;display:flex;justify-content:space-between;align-items:center}" +
    ".cognee-head b{font-weight:600}" +
    ".cognee-log{padding:12px;height:340px;overflow-y:auto;background:#f9fafb}" +
    ".cognee-msg{margin:6px 0;padding:8px 10px;border-radius:10px;max-width:85%;white-space:pre-wrap;word-wrap:break-word}" +
    ".cognee-user{background:#2563eb;color:#fff;margin-left:auto}" +
    ".cognee-bot{background:#fff;border:1px solid #e5e7eb}" +
    ".cognee-cites{margin:4px 0 10px;font-size:12px;color:#6b7280}" +
    ".cognee-cite{border-left:3px solid #d1d5db;padding:2px 8px;margin:3px 0}" +
    ".cognee-cite a{color:#2563eb;text-decoration:none}" +
    ".cognee-cite a:hover{text-decoration:underline}" +
    ".cognee-in{display:flex;border-top:1px solid #e5e7eb}" +
    ".cognee-in input{flex:1;border:0;padding:11px;outline:none;background:#fff;color:inherit}" +
    ".cognee-in button{border:0;background:#2563eb;color:#fff;padding:0 16px;cursor:pointer}" +
    ".cognee-bar{padding:6px 12px;font-size:12px;color:#6b7280;display:flex;justify-content:space-between;background:#fff;border-top:1px solid #f3f4f6}" +
    ".cognee-bar a{color:#2563eb;cursor:pointer;text-decoration:none}" +
    ".cognee-gear{margin-left:10px}" +
    ".cognee-launch{border:0;background:#111827;color:#fff;border-radius:24px;padding:12px 18px;cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.2)}";
  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var root = document.createElement("div");
  root.className = "cognee-w";
  root.innerHTML =
    '<div class="cognee-box" id="cognee-box">' +
    '  <div class="cognee-head"><b>Ask our docs</b>' +
    '    <span style="cursor:pointer" id="cognee-close">×</span></div>' +
    '  <div class="cognee-log" id="cognee-log"></div>' +
    '  <div class="cognee-bar">' +
    '    <label><input type="checkbox" id="cognee-optin"> Remember this chat</label>' +
    '    <span><a id="cognee-forget">Forget me</a>' +
    '    <a class="cognee-gear" id="cognee-dash" target="_blank" rel="noopener" hidden>⚙</a></span></div>' +
    '  <div class="cognee-in">' +
    '    <input id="cognee-input" placeholder="Ask a question…" autocomplete="off"/>' +
    '    <button id="cognee-send">Send</button></div>' +
    "</div>" +
    '<button class="cognee-launch" id="cognee-launch">💬 Chat</button>';
  document.body.appendChild(root);

  var box = root.querySelector("#cognee-box");
  var log = root.querySelector("#cognee-log");
  var input = root.querySelector("#cognee-input");
  // Operator dashboard link. Deliberately not shown to visitors: the token is
  // never served to the page, so the gear appears only in a browser that was
  // handed one out-of-band via ?cognee_dashboard_token=... (stored once, then
  // stripped from the URL). The backend gates /dashboard on the same token, so
  // this is a convenience, never the access control.
  try {
    var qp = new URLSearchParams(window.location.search);
    var handed = qp.get("cognee_dashboard_token");
    if (handed) {
      localStorage.setItem("cognee_dashboard_token", handed);
      qp.delete("cognee_dashboard_token");
      var clean = window.location.pathname + (qp.toString() ? "?" + qp : "") + window.location.hash;
      window.history.replaceState({}, "", clean);
    }
    var dashToken = localStorage.getItem("cognee_dashboard_token");
    if (dashToken) {
      var dash = root.querySelector("#cognee-dash");
      dash.href = API + "/dashboard?token=" + encodeURIComponent(dashToken);
      dash.title = "Widget dashboard";
      dash.hidden = false;
    }
  } catch (e) {
    /* storage blocked - the gear simply stays hidden */
  }

  var optinBox = root.querySelector("#cognee-optin");
  optinBox.checked = optIn;

  function open(v) {
    box.classList.toggle("open", v);
    var l = root.querySelector("#cognee-launch");
    if (l) l.setAttribute("aria-expanded", v ? "true" : "false");
  }
  var launcher = root.querySelector("#cognee-launch");
  launcher.setAttribute("aria-expanded", "false");
  launcher.onclick = function () {
    // Toggle: the launcher stays visible while the panel is open, so a second
    // click on it should close what the first click opened.
    var nowOpen = !box.classList.contains("open");
    open(nowOpen);
    if (nowOpen) input.focus();
  };
  root.querySelector("#cognee-close").onclick = function () {
    open(false);
  };
  optinBox.onchange = function () {
    optIn = optinBox.checked;
    localStorage.setItem("cognee_opt_in", optIn ? "1" : "0");
  };

  function el(cls, text) {
    var d = document.createElement("div");
    d.className = cls;
    d.textContent = text;
    return d;
  }
  function addMsg(role, text) {
    log.appendChild(el("cognee-msg cognee-" + role, text));
    log.scrollTop = log.scrollHeight;
  }
  function addCitations(cites) {
    if (!cites || !cites.length) return;
    var wrap = document.createElement("div");
    wrap.className = "cognee-cites";
    wrap.appendChild(el("", "Sources:"));
    cites.slice(0, 4).forEach(function (c) {
      // Prefer the readable page title over the flattened document name, and
      // link it when the backend could resolve a published URL.
      var label = c.title || c.document || "";
      if (c.snippet) label = c.snippet + (label ? "  (" + label + ")" : "");
      if (!label) return;

      // An absolute url wins (the backend was told the docs live elsewhere);
      // otherwise resolve the page path against DOCS_BASE.
      var href = c.url || (c.path ? DOCS_BASE.replace(/\/+$/, "") + "/" + c.path : null);

      var node = el("cognee-cite", "");
      if (href) {
        var a = document.createElement("a");
        a.href = href;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = label;
        node.appendChild(a);
      } else {
        node.textContent = label;
      }
      wrap.appendChild(node);
    });
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  async function send() {
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    addMsg("user", text);
    try {
      var res = await fetch(API + "/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          conversation_id: conversationId,
          visitor_id: visitorId,
          site_id: SITE_ID,
          opt_in: optIn,
        }),
      });
      var data = await res.json();
      addMsg("bot", data.answer || "…");
      addCitations(data.citations);
    } catch (e) {
      addMsg("bot", "Sorry — I couldn't reach memory right now.");
    }
  }
  root.querySelector("#cognee-send").onclick = send;
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") send();
  });

  root.querySelector("#cognee-forget").onclick = async function () {
    await fetch(API + "/api/forget", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation_id: conversationId,
        visitor_id: visitorId,
        site_id: SITE_ID,
      }),
    });
    log.innerHTML = "";
    addMsg("bot", "Done — I've forgotten this conversation.");
  };
})();
