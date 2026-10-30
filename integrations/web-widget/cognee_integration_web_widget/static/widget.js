/*
 * cognee web chat widget — embeddable snippet.
 *
 * Drop one tag on any page:
 *   <script src="https://your-host/widget.js"
 *           data-site-id="acme" data-api="https://your-host"></script>
 *
 * It opens an assistant side panel styled after the docs.cognee.ai assistant,
 * talks to the cognee-backed backend, renders answers as sanitised markdown,
 * shows citations, and supports forgetting a conversation + opt-out. No build
 * step, no framework: the backend serves this file with marked and DOMPurify
 * prepended (see server.widget_js), so one <script> tag is still the whole
 * embed.
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

  // The bundle hands the libraries over in a local, never on window: a host
  // page may ship its own marked or DOMPurify, and ours must not replace them.
  var libs = typeof __cognee_libs !== "undefined" ? __cognee_libs : {};
  var marked = libs.marked;
  var DOMPurify = libs.DOMPurify;

  // Stable per-browser ids so a returning visitor keeps their conversation.
  // Two different lifetimes, so two different stores.
  //
  // The visitor is the person, and localStorage is shared across their tabs and
  // survives a close - which is what identifying someone across visits means.
  //
  // The conversation is this window. localStorage held it too, and localStorage
  // is per origin: every tab on the site continued one endless conversation,
  // and a second window joined whatever the first was saying. sessionStorage is
  // per tab, survives a reload, and dies with the tab.
  function id(store, key, prefix) {
    var v = null;
    try {
      v = store.getItem(key);
    } catch (e) {
      /* storage blocked: fall through to a per-page id */
    }
    if (!v) {
      v = prefix + "-" + Math.random().toString(36).slice(2, 10);
      try {
        store.setItem(key, v);
      } catch (e) {
        /* nothing to do; the id lives as long as this page does */
      }
    }
    return v;
  }
  var visitorId = id(localStorage, "cognee_visitor_id", "visitor");

  // The chats of this tab. Each has its own conversation id, so cognee keeps a
  // separate session per topic. The list and the transcripts live in
  // sessionStorage: they follow the visitor across pages and reloads and are
  // gone when the tab closes. Nothing is listed back from the server, so one
  // visitor can never see another's chats.
  var CHATS_KEY = "cognee_chats";
  var MAX_CHATS = 20;
  function newChat() {
    return {
      id: "conv-" + Math.random().toString(36).slice(2, 10),
      title: "",
      created: Date.now(),
      messages: [],
    };
  }
  var chats = null;
  try {
    chats = JSON.parse(sessionStorage.getItem(CHATS_KEY) || "null");
  } catch (e) {
    /* unreadable or blocked: start afresh */
  }
  if (!chats || !Array.isArray(chats.list) || !chats.list.length) {
    var first = newChat();
    try {
      // A tab from before chats were kept carries its one conversation id;
      // reuse it so that conversation's session simply continues.
      var legacy = sessionStorage.getItem("cognee_conversation_id");
      if (legacy) first.id = legacy;
      sessionStorage.removeItem("cognee_conversation_id");
    } catch (e) {
      /* nothing to carry over */
    }
    chats = { active: first.id, list: [first] };
  }
  function saveChats() {
    try {
      sessionStorage.setItem(CHATS_KEY, JSON.stringify(chats));
    } catch (e) {
      /* full or blocked: the chats last as long as this page does */
    }
  }
  function chatById(cid) {
    for (var i = 0; i < chats.list.length; i++) if (chats.list[i].id === cid) return chats.list[i];
    return null;
  }
  function activeChat() {
    return chatById(chats.active) || chats.list[0];
  }
  saveChats();
  try {
    // Left over from when this was shared across every tab. Removing it stops
    // an old tab resurrecting the endless conversation.
    localStorage.removeItem("cognee_conversation_id");
  } catch (e) {
    /* nothing to clean up */
  }
  var optIn = localStorage.getItem("cognee_opt_in") !== "0";

  // Colours and type are taken from the docs.cognee.ai assistant (Mintlify),
  // measured in both themes, so the widget reads as the same assistant. Every
  // rule that paints a background also sets a colour: the host's text colour
  // must never be inherited onto our own surfaces.
  var css =
    ".cognee-w{--bg:#fff;--fg:#413f46;--strong:#19171e;--muted:#8a8790;--line:#ebe9ef;--bubble:#f1eef5;--bubble-fg:#19171e;" +
    "--link:#101828;--code-bg:#f6f5f8;--code-fg:#101828;--pre-bg:#fff;--pre-line:#e3e1e8;--pre-fg:#0d0a11;--btn:#19171e;--btn-fg:#fff;" +
    "--in-line:#dcdae0;--in-focus:#a9a6ae;--hover:rgba(0,0,0,.05);--shadow:0 8px 28px rgba(0,0,0,.12);" +
    "font:14px/24px Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;color:var(--fg);-webkit-font-smoothing:antialiased}" +
    ".cognee-w[data-theme=dark]{--bg:#0d0d0f;--fg:#a19fa5;--strong:#e1dee5;--muted:#6f6c75;--line:#28252c;--bubble:rgba(255,255,255,.05);--bubble-fg:#e1dee5;" +
    "--link:#f5f3fa;--code-bg:rgba(255,255,255,.05);--code-fg:#e1dee5;--pre-bg:#0b0c0e;--pre-line:rgba(255,255,255,.14);--pre-fg:#c9c6cd;--btn:#2e2c32;--btn-fg:#f5f3fa;" +
    "--in-line:rgba(255,255,255,.14);--in-focus:#d8d8d8;--hover:rgba(255,255,255,.07);--shadow:0 8px 28px rgba(0,0,0,.5)}" +
    ".cognee-w *{box-sizing:border-box}.cognee-w [hidden]{display:none!important}" +
    ".cognee-launch{position:fixed;right:20px;bottom:20px;z-index:2147483000;display:inline-flex;align-items:center;gap:8px;padding:9px 16px;" +
    "border:1px solid var(--in-line);border-radius:999px;background:var(--bg);color:var(--strong);font-family:inherit;font-size:14px;font-weight:500;line-height:20px;cursor:pointer;box-shadow:var(--shadow)}" +
    ".cognee-launch:hover{border-color:var(--in-focus)}" +
    ".cognee-panel{position:fixed;top:0;right:0;bottom:0;z-index:2147483001;width:clamp(360px,40vw,576px);max-width:100vw;display:none;flex-direction:column;" +
    "background:var(--bg);color:var(--fg);border-left:1px solid var(--line);box-shadow:var(--shadow)}" +
    ".cognee-panel.open{display:flex}" +
    "@media (max-width:640px){.cognee-panel{width:100vw;border-left:0}}" +
    ".cognee-head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px 12px}" +
    ".cognee-title{display:flex;align-items:center;gap:8px;color:var(--strong);font-size:16px;line-height:24px}" +
    ".cognee-acts{display:flex;align-items:center;gap:2px}" +
    ".cognee-icon{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border:0;border-radius:6px;background:transparent;color:var(--muted);cursor:pointer;text-decoration:none}" +
    ".cognee-icon:hover{background:var(--hover);color:var(--strong)}" +
    ".cognee-icon svg,.cognee-title svg,.cognee-launch svg{width:16px;height:16px}" +
    ".cognee-menu{position:absolute;top:52px;left:12px;right:12px;z-index:2;max-height:60%;overflow-y:auto;padding:6px;" +
    "background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}" +
    ".cognee-menu-h{padding:6px 10px;font-size:12px;line-height:16px;color:var(--muted)}" +
    ".cognee-chat{display:flex;align-items:baseline;justify-content:space-between;gap:12px;width:100%;padding:8px 10px;border:0;border-radius:8px;" +
    "background:transparent;color:var(--fg);font-family:inherit;font-size:13px;line-height:18px;text-align:left;cursor:pointer}" +
    ".cognee-chat:hover{background:var(--hover)}.cognee-chat[aria-current=true]{color:var(--strong);font-weight:600}" +
    ".cognee-chat span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
    ".cognee-chat span:last-child{flex:none;font-size:12px;font-weight:400;color:var(--muted)}" +
    ".cognee-note{padding:0 16px 8px;text-align:center;font-size:12px;line-height:16px;color:var(--muted)}" +
    ".cognee-log{flex:1;min-height:0;overflow-y:auto;padding:4px 16px 16px;overflow-wrap:anywhere}" +
    ".cognee-user{display:flex;justify-content:flex-end;margin:12px 0}" +
    ".cognee-user div{max-width:85%;padding:8px 12px;border-radius:16px;background:var(--bubble);color:var(--bubble-fg);font-size:14px;line-height:20px;white-space:pre-wrap}" +
    ".cognee-bot{margin:12px 0;color:var(--fg)}" +
    ".cognee-md>:first-child{margin-top:0}.cognee-md>:last-child{margin-bottom:0}" +
    ".cognee-md p,.cognee-md ul,.cognee-md ol,.cognee-md blockquote,.cognee-md table,.cognee-md .cognee-code{margin:0 0 16px}" +
    ".cognee-md h1,.cognee-md h2,.cognee-md h3,.cognee-md h4,.cognee-md h5,.cognee-md h6{margin:0 0 12px;font-size:14px;line-height:24px;font-weight:600;color:var(--fg)}" +
    ".cognee-md strong,.cognee-md b{font-weight:600}" +
    ".cognee-md ul,.cognee-md ol{padding-left:20px}.cognee-md li{margin:4px 0}.cognee-md li>p{margin:0}" +
    ".cognee-md a{color:var(--link);font-weight:600;text-decoration:underline;text-underline-offset:3px;text-decoration-thickness:1px}" +
    ".cognee-md code{font:500 12px/18px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code-bg);color:var(--code-fg);border-radius:6px;padding:2px 6px}" +
    ".cognee-code{position:relative;border:1px solid var(--pre-line);border-radius:12px;background:var(--pre-bg)}" +
    ".cognee-code pre{margin:0;padding:16px 44px 16px 16px;overflow-x:auto;background:transparent}" +
    ".cognee-code pre code{font:400 13px/20px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:none;color:var(--pre-fg);padding:0;border-radius:0;white-space:pre}" +
    ".cognee-copy{position:absolute;top:8px;right:8px}" +
    ".cognee-md blockquote{padding-left:12px;border-left:2px solid var(--line)}" +
    ".cognee-md table{display:block;overflow-x:auto;border-collapse:collapse}" +
    ".cognee-md th,.cognee-md td{padding:6px 10px;border:1px solid var(--line);text-align:left}" +
    ".cognee-md hr{border:0;border-top:1px solid var(--line);margin:16px 0}" +
    ".cognee-cites{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:-4px 0 16px}" +
    ".cognee-cites span{font-size:12px;line-height:20px;color:var(--muted)}" +
    ".cognee-cite{display:inline-block;max-width:100%;padding:2px 10px;border:1px solid var(--line);border-radius:999px;font-size:12px;line-height:20px;" +
    "color:var(--strong);text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
    "a.cognee-cite:hover{border-color:var(--in-focus)}" +
    ".cognee-dots{display:inline-flex;gap:4px;padding:8px 0}.cognee-dots i{width:5px;height:5px;border-radius:50%;background:var(--muted);animation:cognee-b 1.2s infinite}" +
    ".cognee-dots i:nth-child(2){animation-delay:.2s}.cognee-dots i:nth-child(3){animation-delay:.4s}" +
    "@keyframes cognee-b{0%,80%,100%{opacity:.25}40%{opacity:1}}" +
    ".cognee-foot{padding:8px 12px 12px}" +
    ".cognee-in{display:flex;flex-direction:column;gap:6px;padding:10px 10px 10px 14px;border:1px solid var(--in-line);border-radius:16px;background:var(--bg)}" +
    ".cognee-tools{display:flex;justify-content:flex-end}" +
    ".cognee-in:focus-within{border-color:var(--in-focus)}" +
    ".cognee-in textarea{width:100%;min-height:20px;max-height:160px;resize:none;border:0;outline:0;padding:4px 0;background:transparent;color:var(--strong);font-family:inherit;font-size:14px;line-height:20px}" +
    ".cognee-in textarea::placeholder{color:var(--muted)}" +
    ".cognee-send{flex:none;display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border:0;border-radius:50%;background:var(--btn);color:var(--btn-fg);cursor:pointer}" +
    ".cognee-send:disabled{opacity:.4;cursor:default}.cognee-send svg{width:14px;height:14px}" +
    ".cognee-opt{display:flex;align-items:center;gap:6px;padding:8px 4px 0;font-size:12px;line-height:16px;color:var(--muted);cursor:pointer}" +
    ".cognee-contact{display:flex;flex-direction:column;gap:8px;margin:0 0 16px;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--fg)}" +
    ".cognee-contact-h{color:var(--strong);font-size:13px;line-height:18px;font-weight:600}" +
    ".cognee-contact input,.cognee-contact textarea{width:100%;padding:6px 10px;border:1px solid var(--in-line);border-radius:8px;outline:0;background:var(--bg);color:var(--strong);font-family:inherit;font-size:13px;line-height:20px}" +
    ".cognee-contact textarea{min-height:64px;resize:vertical}" +
    ".cognee-contact input:focus,.cognee-contact textarea:focus{border-color:var(--in-focus)}" +
    ".cognee-contact button{align-self:flex-start;padding:6px 14px;border:0;border-radius:999px;background:var(--btn);color:var(--btn-fg);font-family:inherit;font-size:13px;line-height:20px;cursor:pointer}" +
    ".cognee-contact button:disabled{opacity:.5;cursor:default}" +
    ".cognee-contact-s{font-size:12px;line-height:16px;color:var(--muted)}";
  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  // Lucide-style outline icons, drawn inline so the widget needs no assets.
  function svg(paths) {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + "</svg>"
    );
  }
  var ICON = {
    sparkle: svg('<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 3v4M17 5h4"/>'),
    trash: svg('<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>'),
    close: svg('<path d="M18 6 6 18M6 6l12 12"/>'),
    gear: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 2.9-1.2V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
    copy: svg('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
    check: svg('<path d="M20 6 9 17l-5-5"/>'),
    send: svg('<path d="M12 19V5M5 12l7-7 7 7"/>'),
    chats: svg('<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>'),
    compose: svg('<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z"/>'),
  };

  var root = document.createElement("div");
  root.className = "cognee-w";
  root.innerHTML =
    '<button class="cognee-launch" id="cognee-launch" type="button">' + ICON.sparkle + "Ask AI</button>" +
    '<div class="cognee-panel" id="cognee-panel" role="dialog" aria-label="Assistant">' +
    '  <div class="cognee-head">' +
    '    <div class="cognee-title">' + ICON.sparkle + "Assistant</div>" +
    '    <div class="cognee-acts">' +
    '      <button class="cognee-icon" id="cognee-chats" type="button" title="Chats in this tab" aria-label="Chats in this tab" aria-expanded="false">' + ICON.chats + "</button>" +
    '      <button class="cognee-icon" id="cognee-new" type="button" title="New chat" aria-label="New chat">' + ICON.compose + "</button>" +
    '      <a class="cognee-icon" id="cognee-dash" target="_blank" rel="noopener" title="Widget dashboard" aria-label="Widget dashboard" hidden>' + ICON.gear + "</a>" +
    '      <button class="cognee-icon" id="cognee-forget" type="button" title="Forget and remove this chat" aria-label="Forget and remove this chat">' + ICON.trash + "</button>" +
    '      <button class="cognee-icon" id="cognee-close" type="button" title="Close" aria-label="Close">' + ICON.close + "</button>" +
    "    </div></div>" +
    '  <div class="cognee-menu" id="cognee-menu" hidden></div>' +
    '  <div class="cognee-note">Responses are generated using AI and may contain mistakes.</div>' +
    '  <div class="cognee-log" id="cognee-log" aria-live="polite"></div>' +
    '  <div class="cognee-foot">' +
    '    <div class="cognee-in">' +
    '      <textarea id="cognee-input" rows="1" placeholder="Ask a question..." aria-label="Ask a question"></textarea>' +
    '      <div class="cognee-tools"><button class="cognee-send" id="cognee-send" type="button" title="Send" aria-label="Send">' + ICON.send + "</button></div>" +
    "    </div>" +
    '    <label class="cognee-opt"><input type="checkbox" id="cognee-optin"> Remember this chat</label>' +
    "  </div>" +
    "</div>";
  document.body.appendChild(root);

  var panel = root.querySelector("#cognee-panel");
  var log = root.querySelector("#cognee-log");
  var input = root.querySelector("#cognee-input");
  var sendBtn = root.querySelector("#cognee-send");
  var launcher = root.querySelector("#cognee-launch");

  // Follow the host page's theme: Mintlify marks dark mode with a "dark" class
  // on <html> (other sites use data-theme); with neither, the OS preference.
  var darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function applyTheme() {
    var h = document.documentElement;
    var t = h.getAttribute("data-theme");
    var dark = h.classList.contains("dark") || t === "dark";
    if (!dark && !h.classList.contains("light") && t !== "light" && darkQuery) dark = darkQuery.matches;
    root.setAttribute("data-theme", dark ? "dark" : "light");
  }
  applyTheme();
  new MutationObserver(applyTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class", "data-theme"],
  });
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener("change", applyTheme);

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
      dash.hidden = false;
    }
  } catch (e) {
    /* storage blocked - the gear simply stays hidden */
  }

  var optinBox = root.querySelector("#cognee-optin");
  optinBox.checked = optIn;
  optinBox.onchange = function () {
    optIn = optinBox.checked;
    localStorage.setItem("cognee_opt_in", optIn ? "1" : "0");
  };

  function open(v) {
    panel.classList.toggle("open", v);
    launcher.hidden = v;
    launcher.setAttribute("aria-expanded", v ? "true" : "false");
    if (v) input.focus();
    else launcher.focus();
  }
  launcher.setAttribute("aria-expanded", "false");
  launcher.onclick = function () {
    open(true);
  };
  root.querySelector("#cognee-close").onclick = function () {
    open(false);
  };
  panel.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (!menu.hidden) closeMenu();
    else open(false);
  });

  // -- rendering ---------------------------------------------------------

  // Answers are LLM output that a visitor's question can steer, so the HTML
  // marked produces is sanitised before it touches the page: an allowlist of
  // formatting tags, no images or embeds, no event handlers, and only http(s),
  // mailto and relative links. User messages and citations never go through
  // here - they are set as text.
  var ALLOWED_TAGS = [
    "p", "br", "strong", "b", "em", "i", "del", "s", "code", "pre", "blockquote",
    "ul", "ol", "li", "a", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td", "hr",
  ];
  if (DOMPurify && DOMPurify.addHook) {
    DOMPurify.addHook("afterSanitizeAttributes", function (node) {
      if (node.tagName === "A") {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
      if (node.hasAttribute("class") && !/^language-[\w+-]+$/.test(node.getAttribute("class"))) {
        node.removeAttribute("class");
      }
    });
  }
  function renderMarkdown(text) {
    var wrap = document.createElement("div");
    wrap.className = "cognee-md";
    if (!marked || !DOMPurify) {
      // Served without the bundle: fall back to the old safe plain text.
      wrap.style.whiteSpace = "pre-wrap";
      wrap.textContent = text;
      return wrap;
    }
    var html = marked.parse(text, { gfm: true, breaks: false, async: false });
    wrap.innerHTML = DOMPurify.sanitize(html, {
      ALLOWED_TAGS: ALLOWED_TAGS,
      ALLOWED_ATTR: ["href", "title", "start", "class", "colspan", "rowspan", "align"],
      ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i,
    });
    // Code blocks get the docs' bordered frame and a copy button.
    Array.prototype.forEach.call(wrap.querySelectorAll("pre"), function (pre) {
      var frame = document.createElement("div");
      frame.className = "cognee-code";
      pre.parentNode.insertBefore(frame, pre);
      frame.appendChild(pre);
      var copy = document.createElement("button");
      copy.type = "button";
      copy.className = "cognee-icon cognee-copy";
      copy.title = "Copy";
      copy.setAttribute("aria-label", "Copy code");
      copy.innerHTML = ICON.copy;
      copy.onclick = function () {
        if (!navigator.clipboard) return;
        navigator.clipboard.writeText(pre.textContent).then(function () {
          copy.innerHTML = ICON.check;
          setTimeout(function () {
            copy.innerHTML = ICON.copy;
          }, 1500);
        });
      };
      frame.appendChild(copy);
    });
    return wrap;
  }

  function scrollDown() {
    log.scrollTop = log.scrollHeight;
  }
  function addUser(text) {
    var row = document.createElement("div");
    row.className = "cognee-user";
    var bubble = document.createElement("div");
    bubble.textContent = text;
    row.appendChild(bubble);
    log.appendChild(row);
    scrollDown();
  }
  function addBot(text) {
    var row = document.createElement("div");
    row.className = "cognee-bot";
    row.appendChild(renderMarkdown(text));
    log.appendChild(row);
    scrollDown();
    return row;
  }
  function addThinking() {
    var row = document.createElement("div");
    row.className = "cognee-bot";
    row.setAttribute("aria-label", "Thinking");
    row.innerHTML = '<span class="cognee-dots"><i></i><i></i><i></i></span>';
    log.appendChild(row);
    scrollDown();
    return row;
  }
  var pendingChat = null;
  // Shown when the backend refuses a request over its rate limit without saying more.
  var LIMITED = "You've asked a lot of questions in a short time. Please try again later.";
  function renderChat() {
    var chat = activeChat();
    log.textContent = "";
    chat.messages.forEach(function (m) {
      if (m.role === "user") {
        addUser(m.text);
      } else {
        var row = addBot(m.text);
        if (m.notice) row.style.color = "var(--muted)";
        addCitations(m.citations);
        addCode(m.code);
        if (m.contact) addContact(chat, m);
      }
    });
    if (pendingChat === chat.id) addThinking();
  }
  function closeMenu() {
    menu.hidden = true;
    chatsBtn.setAttribute("aria-expanded", "false");
  }
  function renderMenu() {
    menu.textContent = "";
    var head = document.createElement("div");
    head.className = "cognee-menu-h";
    head.textContent = "Chats in this tab \u00b7 cleared when you close it";
    menu.appendChild(head);
    chats.list
      .slice()
      .sort(function (a, b) {
        return b.created - a.created;
      })
      .forEach(function (chat) {
        var item = document.createElement("button");
        item.type = "button";
        item.className = "cognee-chat";
        if (chat.id === chats.active) item.setAttribute("aria-current", "true");
        var title = document.createElement("span");
        title.textContent = chat.title || "New chat";
        var meta = document.createElement("span");
        var asked = chat.messages.filter(function (m) {
          return m.role === "user";
        }).length;
        meta.textContent = asked === 1 ? "1 question" : asked + " questions";
        item.appendChild(title);
        item.appendChild(meta);
        item.onclick = function () {
          chats.active = chat.id;
          saveChats();
          closeMenu();
          renderChat();
          input.focus();
        };
        menu.appendChild(item);
      });
  }
  function addCitations(cites) {
    if (!cites || !cites.length) return;
    var wrap = document.createElement("div");
    wrap.className = "cognee-cites";
    var label = document.createElement("span");
    label.textContent = "Sources";
    wrap.appendChild(label);
    cites.slice(0, 4).forEach(function (c) {
      // Prefer the readable page title over the flattened document name, and
      // link it when the backend could resolve a published URL.
      var text = c.title || c.document || "";
      if (!text) return;
      // An absolute url wins (the backend was told the docs live elsewhere);
      // otherwise resolve the page path against DOCS_BASE.
      var href = c.url || (c.path ? DOCS_BASE.replace(/\/+$/, "") + "/" + c.path : null);
      var node;
      if (href && /^https?:\/\//i.test(href)) {
        node = document.createElement("a");
        node.href = href;
        node.target = "_blank";
        node.rel = "noopener noreferrer";
      } else {
        node = document.createElement("span");
      }
      node.className = "cognee-cite";
      node.textContent = text;
      if (c.snippet) node.title = c.snippet;
      wrap.appendChild(node);
    });
    log.appendChild(wrap);
    scrollDown();
  }

  // Code from the ingested repository that the message named (a class, a
  // method, a file), as the backend resolved it. Linked when the backend knows
  // where the repository can be browsed; the full symbol is the tooltip.
  function addCode(refs) {
    if (!refs || !refs.length) return;
    var wrap = document.createElement("div");
    wrap.className = "cognee-cites";
    var label = document.createElement("span");
    label.textContent = "Related code";
    wrap.appendChild(label);
    refs.slice(0, 4).forEach(function (r) {
      if (!r.name) return;
      var node;
      if (r.url && /^https?:\/\//i.test(r.url)) {
        node = document.createElement("a");
        node.href = r.url;
        node.target = "_blank";
        node.rel = "noopener noreferrer";
      } else {
        node = document.createElement("span");
      }
      node.className = "cognee-cite";
      var where = r.file ? r.file.split("/").pop() + (r.line ? ":" + r.line : "") : "";
      node.textContent = r.kind === "CodeFileReference" || !where ? r.name : r.name + " · " + where;
      node.title = r.symbol || r.name;
      wrap.appendChild(node);
    });
    log.appendChild(wrap);
    scrollDown();
  }

  // The hand-off to the team, offered under an answer the assistant could not
  // give reliably. The visitor's note and this chat's transcript go to the
  // backend, which emails them; m.contact records "open" or "sent" so a
  // re-render shows the same state.
  function addContact(chat, m) {
    var box = document.createElement("div");
    box.className = "cognee-contact";
    var head = document.createElement("div");
    head.className = "cognee-contact-h";
    var status = document.createElement("div");
    status.className = "cognee-contact-s";
    if (m.contact === "sent") {
      head.textContent = "Sent to the team";
      status.textContent = "They'll reply to " + (m.contactEmail || "your email") + ".";
      box.appendChild(head);
      box.appendChild(status);
      log.appendChild(box);
      scrollDown();
      return;
    }
    head.textContent = "Ask the team";
    var email = document.createElement("input");
    email.type = "email";
    email.placeholder = "Your email";
    email.setAttribute("aria-label", "Your email");
    var note = document.createElement("textarea");
    note.placeholder = "Anything to add? This chat is attached.";
    note.setAttribute("aria-label", "Message to the team");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Send";
    btn.onclick = async function () {
      var address = email.value.trim();
      if (!address) {
        status.textContent = "Please enter your email so the team can reply.";
        email.focus();
        return;
      }
      btn.disabled = true;
      status.textContent = "Sending…";
      var failed = "Sorry, the message couldn't be sent. Please try again later.";
      try {
        var res = await fetch(API + "/api/contact", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            conversation_id: chat.id,
            visitor_id: visitorId,
            site_id: SITE_ID,
            email: address,
            message: note.value,
            page_url: window.location.href,
            transcript: chat.messages.map(function (x) {
              return { role: x.role === "user" ? "user" : "assistant", text: x.text };
            }),
          }),
        });
        if (res.ok) {
          m.contact = "sent";
          m.contactEmail = address;
          saveChats();
          if (chats.active === chat.id) renderChat();
          return;
        }
        var data = {};
        try {
          data = await res.json();
        } catch (e) {
          /* no body: the generic message below */
        }
        status.textContent = data.message || (res.status === 429 ? LIMITED : failed);
      } catch (e) {
        status.textContent = failed;
      }
      btn.disabled = false;
    };
    box.appendChild(head);
    box.appendChild(email);
    box.appendChild(note);
    box.appendChild(btn);
    box.appendChild(status);
    log.appendChild(box);
    scrollDown();
  }

  // -- talking to the backend --------------------------------------------

  function grow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }
  input.addEventListener("input", grow);

  var busy = false;
  async function send() {
    var text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    input.value = "";
    grow();
    var chat = activeChat();
    chat.messages.push({ role: "user", text: text });
    if (!chat.title) chat.title = text.length > 60 ? text.slice(0, 57) + "\u2026" : text;
    saveChats();
    addUser(text);
    pendingChat = chat.id;
    addThinking();
    var reply;
    try {
      var res = await fetch(API + "/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          conversation_id: chat.id,
          visitor_id: visitorId,
          site_id: SITE_ID,
          opt_in: optIn,
        }),
      });
      var data = await res.json();
      if (res.status === 429) {
        // Over a rate limit: the backend says which and when to try again.
        reply = { role: "bot", text: data.message || LIMITED, citations: [], notice: true };
      } else if (!res.ok) {
        reply = { role: "bot", text: "Sorry — I couldn't reach memory right now.", citations: [] };
      } else {
        reply = { role: "bot", text: data.answer || "…", citations: data.citations || [], code: data.code || [] };
        if (data.contact_form) reply.contact = "open";
      }
    } catch (e) {
      reply = { role: "bot", text: "Sorry — I couldn't reach memory right now.", citations: [] };
    } finally {
      pendingChat = null;
      if (chatById(chat.id)) {
        chat.messages.push(reply);
        saveChats();
      }
      if (chats.active === chat.id) renderChat();
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  sendBtn.onclick = send;
  input.addEventListener("keydown", function (e) {
    // Enter sends, Shift+Enter breaks the line - as in the docs assistant.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });

  var menu = root.querySelector("#cognee-menu");
  var chatsBtn = root.querySelector("#cognee-chats");
  chatsBtn.onclick = function () {
    if (menu.hidden) {
      renderMenu();
      menu.hidden = false;
      chatsBtn.setAttribute("aria-expanded", "true");
    } else {
      closeMenu();
    }
  };
  panel.addEventListener("click", function (e) {
    if (!menu.hidden && !menu.contains(e.target) && !chatsBtn.contains(e.target)) closeMenu();
  });
  root.querySelector("#cognee-new").onclick = function () {
    closeMenu();
    // One empty chat at most: reuse it rather than piling up blank ones.
    var empty = chats.list.filter(function (c) {
      return !c.messages.length;
    })[0];
    var chat = empty || newChat();
    if (!empty) {
      chats.list.push(chat);
      // Oldest first out; their sessions stay in cognee, only the tab forgets them.
      while (chats.list.length > MAX_CHATS) chats.list.shift();
    }
    chats.active = chat.id;
    saveChats();
    renderChat();
    input.focus();
  };

  root.querySelector("#cognee-forget").onclick = async function () {
    closeMenu();
    var chat = activeChat();
    var limited = null;
    try {
      var res = await fetch(API + "/api/forget", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: chat.id,
          visitor_id: visitorId,
          site_id: SITE_ID,
        }),
      });
      if (res.status === 429) {
        limited = LIMITED; // kept even if the body cannot be read
        limited = (await res.json()).message || LIMITED;
      }
    } catch (e) {
      /* nothing on screen to keep either way */
    }
    if (limited) {
      // Nothing was forgotten, so the chat stays where it is.
      var notice = addBot(limited);
      notice.style.color = "var(--muted)";
      return;
    }
    chats.list = chats.list.filter(function (c) {
      return c.id !== chat.id;
    });
    if (!chats.list.length) chats.list.push(newChat());
    chats.active = chats.list[chats.list.length - 1].id;
    saveChats();
    renderChat();
    // Said once, not stored: the chat it refers to no longer exists.
    var note = addBot("Done — I've forgotten that conversation.");
    note.style.color = "var(--muted)";
  };

  renderChat();
})();
