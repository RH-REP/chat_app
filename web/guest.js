// chat_app 相手のページ。待つ側の chat.py が同じ Wi-Fi に出すページで、合言葉と名前を入れてつなぐ。
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const log = $("log");
  const PING_MS = 5000;  // 待つ側は、これが 20 秒届かないと注意を出し、30 秒で切れたとみなす
  let host = "";
  let es = null;
  let pinger = 0;
  let ended = false;

  function show(view) {
    for (const id of ["login", "closed", "chat"]) $(id).hidden = id !== view;
  }
  async function post(path, body) {
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify(body || {}), credentials: "same-origin" });
      const data = await r.json().catch(() => ({}));
      return { ok: r.ok, status: r.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: { error: "待つ側に届きません" } };
    }
  }
  const store = {
    get(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 使えないときは覚えない */ } },
  };

  // ---- 発言の欄 ----
  function add(el) {
    const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.appendChild(el);
    if (stick) log.scrollTop = log.scrollHeight;
  }
  function hhmm(t) {
    const d = new Date((t || Date.now() / 1000) * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function addMessage(ev) {
    const d = document.createElement("div");
    d.className = "msg" + (ev.mine ? " me" : "");
    const s = document.createElement("small");
    s.textContent = ev.mine ? hhmm(ev.time) : hhmm(ev.time) + " " + ev.name;
    d.appendChild(s);
    d.appendChild(document.createTextNode(ev.text));
    add(d);
  }
  function addNotice(text, kind) {
    const d = document.createElement("div");
    d.className = "note " + (kind || "info");
    d.textContent = (kind === "error" ? "✕ " : kind === "ok" ? "✓ " : "● ") + text;
    add(d);
  }
  function addFile(ev) {
    const d = document.createElement("div");
    d.className = "file";
    d.textContent = host + " から " + ev.name + "（" + ev.size_h + "）";
    const a = document.createElement("a");
    a.href = "/api/file/" + encodeURIComponent(ev.id);
    a.textContent = "保存";
    a.setAttribute("download", ev.name);
    d.appendChild(a);
    add(d);
  }
  function setStatus(text, cls) {
    $("status").className = "st " + (cls || "");
    $("status").textContent = text;
  }

  function end(text) {
    if (ended) return;
    ended = true;
    clearInterval(pinger);
    if (es) es.close();
    addNotice(text);
    setStatus("✕ " + text, "lost");
    for (const id of ["text", "fileBtn", "sendBtn"]) $(id).disabled = true;
    $("leaveBtn").hidden = true;
  }

  function handle(ev) {
    switch (ev.type) {
      case "message": return addMessage(ev);
      case "notice": return addNotice(ev.text, ev.kind);
      case "incoming": return addNotice(host + " が " + ev.name + "（" + ev.size_h + "）を送っています");
      case "file": return addFile(ev);
      case "end": return end(ev.text);
    }
  }

  // ---- チャット ----
  function startChat() {
    show("chat");
    $("who").textContent = host + " と話しています";
    $("leaveBtn").hidden = false;
    setStatus("● " + host + " とつながっています", "ok");
    es = new EventSource("/api/events");
    es.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (e) { console.error(e); } };
    es.onerror = () => { if (!ended) setStatus("◐ 待つ側とのつながりを探しています …", "quiet"); };
    es.onopen = () => { if (!ended) setStatus("● " + host + " とつながっています", "ok"); };
    const ping = async () => {
      const r = await post("/api/ping");
      if (r.status === 403 || (r.ok && r.data.open === false)) end("チャットは終わりました");
    };
    ping();
    pinger = setInterval(ping, PING_MS);
    $("text").focus();
  }

  $("inForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const t = $("text");
    const text = t.value;
    if (!text.trim() || ended) return;
    t.value = "";
    const r = await post("/api/send", { text });
    if (!r.ok) { addNotice("送れません: " + (r.data.error || r.status), "error"); t.value = text; }
  });

  $("leaveBtn").addEventListener("click", () => { post("/api/leave"); end("チャットを抜けました"); });
  $("fileBtn").addEventListener("click", () => $("file").click());
  $("file").addEventListener("change", () => {
    const f = $("file").files[0];
    $("file").value = "";
    if (!f) return;
    const x = new XMLHttpRequest();
    x.open("POST", "/api/upload?name=" + encodeURIComponent(f.name));
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      $("prog").hidden = false;
      $("progText").textContent = "送信中 " + f.name + " " + Math.floor(e.loaded * 100 / e.total) + "%";
      $("progBar").style.width = (e.loaded * 100 / e.total) + "%";
    };
    x.onload = () => {
      $("prog").hidden = true;
      if (x.status >= 300) {
        let d = {};
        try { d = JSON.parse(x.responseText); } catch (err) { /* 空 */ }
        addNotice("送れません: " + f.name + ": " + (d.error || x.status), "error");
      }
    };
    x.onerror = () => { $("prog").hidden = true; addNotice("送れません: 待つ側に届きません", "error"); };
    x.send(f);
  });

  // ---- つなぐ ----
  $("login").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = $("code").value.trim();
    const name = $("name").value.trim();
    if (!code) { $("err").textContent = "合言葉を入れてください"; return $("code").focus(); }
    if (!name) { $("err").textContent = "名前を入れてください"; return $("name").focus(); }
    $("go").disabled = true;
    $("err").textContent = "";
    const r = await post("/api/login", { code, name });
    $("go").disabled = false;
    if (r.ok) {
      store.set("chat_app.name", name);
      host = r.data.host || "相手";
      return startChat();
    }
    if (r.status === 409 || r.status === 410) return closed(r.data.error);
    $("err").textContent = r.data.error || "つなげませんでした";
    const field = r.data.field === "name" ? $("name") : $("code");
    if (r.data.field !== "name") $("code").value = "";
    field.focus();
  });

  function closed(text) {
    show("closed");
    $("closedText").textContent = text + "。待つ側の人に、もう一度待ってもらってください";
  }

  async function boot() {
    let st;
    try {
      st = await (await fetch("/api/state", { credentials: "same-origin" })).json();
    } catch (e) {
      return closed("待つ側に届きません");
    }
    if (st.joined) { host = st.host; return startChat(); }
    if (!st.open) return closed("もう相手が決まったか、待つのをやめています");
    show("login");
    $("name").value = store.get("chat_app.name");
    $("code").focus();
  }
  boot();
})();
