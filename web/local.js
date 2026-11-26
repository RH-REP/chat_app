// chat_app ブラウザの版（127.0.0.1 の手元のページ）。ページ → 手元は fetch / XHR、手元 → ページは Server-Sent Events。
"use strict";
(function () {
  const KEY = new URLSearchParams(location.search).get("k") || "";
  const $ = (id) => document.getElementById(id);
  const log = $("log");
  let phase = "idle";
  let framed = true;
  let ended = false;

  // ---- 手元とのやり取り ----
  async function api(path, body) {
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Chat-Key": KEY },
        body: JSON.stringify(body || {}),
      });
      const data = await r.json().catch(() => ({}));
      return { ok: r.ok, status: r.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: { error: "chat.py に届きません" } };
    }
  }
  async function getState() {
    const r = await fetch("/api/state", { headers: { "X-Chat-Key": KEY } });
    return r.json();
  }

  // ---- 発言の欄 ----
  const atBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  function add(el) {
    const stick = atBottom();
    log.appendChild(el);
    if (stick) log.scrollTop = log.scrollHeight;
  }
  function hhmm(t) {
    const d = new Date((t || Date.now() / 1000) * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function addMessage(mine, name, text, time) {
    const d = document.createElement("div");
    d.className = "msg" + (mine ? " me" : "");
    const s = document.createElement("small");
    s.textContent = mine ? hhmm(time) : hhmm(time) + " " + name;
    d.appendChild(s);
    d.appendChild(document.createTextNode(text));
    add(d);
  }
  function addNotice(text, kind) {
    const d = document.createElement("div");
    d.className = "note " + (kind || "info");
    d.textContent = (kind === "error" ? "✕ " : kind === "ok" ? "✓ " : "● ") + text;
    add(d);
  }
  function addRows(rows) {
    const p = document.createElement("pre");
    p.className = "rows";
    p.textContent = rows.map((r) => r.trim()).join("\n");
    add(p);
  }

  // ---- 状態の行・進み具合・受け取りの確認 ----
  function setStatus(ev) {
    const st = $("status");
    st.className = "st " + (ev.state || "");
    if (ev.state === "lost") st.textContent = "✕ " + ev.name + " との接続が切れました";
    else if (ev.state === "quiet") st.textContent = "◐ " + ev.name + "  返事が " + ev.silent + " 秒ありません …";
    else st.textContent = "● " + ev.name + "（" + ev.ip + "）とつないで " + ev.minutes + " 分";
  }
  function setProgress(text, pct, final) {
    $("prog").hidden = !!final;
    $("progText").textContent = text;
    $("progBar").style.width = Math.max(0, Math.min(100, pct)) + "%";
  }
  function setAsk(ev) {
    $("ask").hidden = !ev;
    if (ev) $("askText").textContent = "ファイルが届きました: " + ev.name + "（" + ev.size_h + "）";
  }

  // ---- 画面の切り替え ----
  function setPhase(st) {
    phase = st.phase;
    const chat = phase === "chat";
    $("connect").hidden = phase === "waiting" || chat;
    $("waiting").hidden = phase !== "waiting";
    $("peerBox").hidden = !chat;
    for (const id of ["text", "fileBtn", "sendBtn"]) $(id).disabled = !chat;
    if (phase === "waiting" && st.wait) {
      $("wIp").textContent = st.wait.ip;
      $("wCode").textContent = st.wait.code;
      $("wUrl").textContent = st.wait.page_url || ("出せません（" + (st.wait.page_error || "使わない設定") + "）");
      $("status").className = "st";
      $("status").textContent = "● 待っています …";
    }
    if (chat && st.peer) {
      framed = st.peer.framed;
      $("pName").textContent = st.peer.name;
      $("pIp").textContent = st.peer.ip;
      $("text").focus();
    }
    if (phase === "idle") {
      setAsk(null);
      $("prog").hidden = true;
      $("status").className = "st";
      $("status").textContent = "つないでいません";  // 抜けた・切れたの理由は発言の欄の知らせに出ている
    }
  }

  function handle(ev) {
    switch (ev.type) {
      case "message": return addMessage(ev.mine, ev.name, ev.text, ev.time);
      case "notice": return addNotice(ev.text, ev.kind);
      case "lines": return addRows(ev.rows);
      case "progress": return setProgress(ev.text, ev.pct, ev.final);
      case "status": return setStatus(ev);
      case "ask": return setAsk(ev);
      case "ask_done": return setAsk(null);
      case "phase": return setPhase(ev);
      case "quit":
        ended = true;
        addNotice(ev.text + "。このページは閉じてかまいません");
        setPhase({ phase: "ended" });
        $("connect").hidden = true;
        for (const b of document.querySelectorAll("button")) if (!b.closest(".fb-box") && b.id !== "fbBtn") b.disabled = true;
        return;
    }
  }

  // ---- つなぐ欄 ----
  function formMessage(text, kind, field) {
    const m = $("formMsg");
    m.textContent = text || "";
    m.className = "msg" + (kind ? " " + kind : "");
    if (field && $(field)) { $(field).focus(); $(field).select(); }
  }
  $("joinForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { ip: $("ip").value.trim(), code: $("code").value.trim(), name: $("name").value.trim() };
    for (const [k, label] of [["ip", "相手の IP"], ["code", "合言葉"], ["name", "自分の名前"]]) {
      if (!body[k]) return formMessage(label + "を入れてください", "error", k);
    }
    formMessage("つないでいます …");
    $("joinBtn").disabled = true;
    const r = await api("/api/join", body);
    $("joinBtn").disabled = false;
    if (!r.ok) {
      if (r.data.field === "code") $("code").value = "";
      return formMessage(r.data.error || "つなげませんでした", "error", r.data.field);
    }
    formMessage("");
  });
  $("hostBtn").addEventListener("click", async () => {
    const name = $("name").value.trim();
    if (!name) return formMessage("自分の名前を入れてください", "error", "name");
    formMessage("待ち受けを始めています …");
    const r = await api("/api/host", { code: $("code").value.trim(), name });
    formMessage(r.ok ? "" : (r.data.error || "待てませんでした"), r.ok ? "" : "error", r.data.field);
  });
  // 合言葉の欄は、待つ側になるときは空でもよい（空ならランダム）
  $("code").placeholder = "4 桁";
  $("cancelBtn").addEventListener("click", () => api("/api/cancel"));
  $("leaveBtn").addEventListener("click", () => api("/api/leave"));
  $("openBtn").addEventListener("click", () => api("/api/open"));
  $("quitBtn").addEventListener("click", () => api("/api/quit"));
  $("askYes").addEventListener("click", () => api("/api/answer", { accept: true }));
  $("askNo").addEventListener("click", () => api("/api/answer", { accept: false }));
  $("autoAccept").addEventListener("change", (e) => api("/api/auto", { on: e.target.checked }));

  // ---- 入力の行 ----
  $("inForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const t = $("text");
    const text = t.value;
    if (!text.trim()) return;
    t.value = "";
    const r = await api("/api/send", { text });
    if (!r.ok) { addNotice("送れません: " + (r.data.error || r.status), "error"); t.value = text; }
  });

  // ---- ファイル（選ぶ・ドラッグ） ----
  // ブラウザはファイルの元の場所を教えないので、中身を 127.0.0.1 へ送り、chat.py が一時ファイルから相手へ送る。
  function uploadOne(file) {
    return new Promise((resolve) => {
      const x = new XMLHttpRequest();
      x.open("POST", "/api/upload?name=" + encodeURIComponent(file.name));
      x.setRequestHeader("X-Chat-Key", KEY);
      x.setRequestHeader("Content-Type", "application/octet-stream");
      x.upload.onprogress = (e) => { if (e.lengthComputable) setProgress("コピーしています " + file.name, e.loaded * 100 / e.total, false); };
      x.onload = () => {
        let d = {};
        try { d = JSON.parse(x.responseText); } catch (e) { /* 空 */ }
        resolve({ ok: x.status < 300, data: d });
      };
      x.onerror = () => resolve({ ok: false, data: { error: "chat.py に届きません" } });
      x.send(file);
    });
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let queue = Promise.resolve();
  function sendFiles(files) {
    if (phase !== "chat") return addNotice("つながってから送ってください", "error");
    if (!framed) return addNotice("相手が古いバージョンなので送れません。相手に chat_app を新しくしてもらってください", "error");
    for (const f of files) {
      queue = queue.then(async () => {
        if (phase !== "chat") return;
        while ((await getState()).sending) await sleep(400);  // 1 件ずつ送る
        const r = await uploadOne(f);
        if (!r.ok) { $("prog").hidden = true; addNotice("送れません: " + f.name + ": " + (r.data.error || ""), "error"); return; }
        await sleep(300);
        while ((await getState()).sending) await sleep(400);
      });
    }
  }
  $("fileBtn").addEventListener("click", () => $("file").click());
  $("file").addEventListener("change", () => { sendFiles([...$("file").files]); $("file").value = ""; });
  // ページ全体で dragover を止める（止めないと、離したときにブラウザがファイルを開いてしまう）
  let depth = 0;
  window.addEventListener("dragenter", (e) => {
    if (![...(e.dataTransfer && e.dataTransfer.types || [])].includes("Files")) return;
    depth++;
    log.classList.add("drag");
    $("drop").hidden = phase !== "chat";
  });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; log.classList.remove("drag"); $("drop").hidden = true; } });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0;
    log.classList.remove("drag");
    $("drop").hidden = true;
    const files = [...(e.dataTransfer && e.dataTransfer.files || [])];
    if (files.length) sendFiles(files);
  });

  // ---- 始める ----
  async function boot() {
    let st;
    try { st = await getState(); } catch (e) { return addNotice("chat.py に届きません", "error"); }
    $("ip").value = st.defaults.ip || "";
    $("name").value = st.defaults.name || "";
    $("code").value = st.defaults.code || "";
    $("autoAccept").checked = !!st.auto_accept;
    setPhase(st);
    if (!$("connect").hidden) ($("ip").value ? ($("name").value ? $("code") : $("name")) : $("ip")).focus();
    const es = new EventSource("/api/events?k=" + encodeURIComponent(KEY) + "&since=0");
    es.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (e) { console.error(e); } };
    es.onerror = () => {
      if (ended) { es.close(); return; }
      $("status").className = "st lost";
      $("status").textContent = "✕ chat.py とのつながりを探しています …";
    };
    es.onopen = () => { if ($("status").textContent.indexOf("chat.py") >= 0) { $("status").className = "st"; $("status").textContent = phase === "chat" ? "●" : "つないでいません"; } };
  }
  boot();

  // ---- 改善のコメント（右上の !）。発言・名前・IP は入れない ----
  if (window.AppFeedback) {
    AppFeedback.install({
      app: "chat_app", version: "2026-11-27b", repo: "RH-REP/chat_app",
      screenshot: "off", isPublic: true, log: false,
      getContext: () => ({ summary: "ブラウザの版・" + phase + (phase === "chat" ? (framed ? "・相手は新しいバージョン" : "・相手は古いバージョン") : "") }),
    });
  }
})();
