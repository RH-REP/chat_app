// 画面の !○（改善のコメント）。どの画面からでも「画面 ID ＋ コメント ＋（選べば）画面の写真 ＋ 記録」を GitHub の新しい Issue にする。
// 正本は issue_hub/feedback/feedback.js。各 app はこのファイルをそのまま写して使う（直すときは正本を直して写し直す）。
// 元は paper_reader の assets/feedback.js（ES モジュール）。file:// で開く単体 HTML でも読めるよう、普通の <script> にしてある。
//
// 使い方:
//   <script src="feedback.js"></script>
//   <script>
//     AppFeedback.install({
//       app: "vowel_chart", version: "2026-10-22", repo: "OWNER/REPO",
//       screenshot: "off",               // 写真の欄の初期状態: "on"（付ける）/ "off"（付けない。選べば付く）/ "never"（欄を出さない）
//       isPublic: true,                  // 公開 repo なら、送った中身が公開になることを画面に書く
//       log: true,                       // false で記録（画面のエラー・getLog）の欄を出さない（個人のデータが混ざりうる app）
//       getContext: () => ({ summary: "今の状態を1行で", extra: "…" }),   // Issue に書く今の状態（無ければ省く）
//       getLog: () => "…",               // 画面側の記録（無ければ省く）
//     });
//   </script>
// 送り方（既定）: 書き込み済みの「新しい Issue」画面を GitHub で開く。写真は端末に保存し、本人が GitHub で添付して送る
//   （鍵を app に置かない）。サーバーで送る app は send: async (payload) => ({ number, url }) を渡す。
// 送り先は 2 つ（2026-11-09）: 「GitHub の Issue」と「対話のセッション（issue_hub）」。対話は、手元で動く issue_hub の画面
//   （hub: 既定 http://127.0.0.1:8934）にコメントを渡し、issue_hub がその app の対話のセッションに届けて返事を待つ。
//   対話の欄は、手元で開いた画面（127.0.0.1・localhost・file://）にだけ出す（公開のサイトでは出さない）。talk: false で消す。
//   repo が無い app（GitHub で管理していない）は、見た目はそのままで「GitHub の Issue」を押せなくする。
//   onlyLocal: true なら、手元で開いたときだけ「!」を出す（GitHub Pages などで公開しているサイトの訪問者には見せない）。
// 画面 ID: 要素に data-screen="main.list" を付ける。見えているもののうち data-screen-layer の大きいもの（同じなら DOM の深いもの）を
//   今の画面とする。どこにも無ければ、ページの名前（index・pairs など）を画面 ID にする。
(function () {
  "use strict";
  const ERR_KEY = "feedback.errors";

  function rememberError(msg) {
    try {
      const a = JSON.parse(localStorage.getItem(ERR_KEY) || "[]");
      a.push(`${new Date().toISOString()} ${String(msg).slice(0, 300)}`);
      localStorage.setItem(ERR_KEY, JSON.stringify(a.slice(-30)));
    } catch (e) { /* 保存できなくても続ける */ }
  }
  function errorLog() {
    try { return JSON.parse(localStorage.getItem(ERR_KEY) || "[]").join("\n"); } catch (e) { return ""; }
  }

  // ---- 今の画面 ID ----
  function visible(el) {
    if (el.closest("[hidden]")) return false;
    if (el.checkVisibility) return el.checkVisibility();
    const st = getComputedStyle(el);
    return st.display !== "none" && st.visibility !== "hidden" && el.getClientRects().length > 0;
  }
  function pageName() {
    const last = decodeURIComponent(location.pathname.split("/").pop() || "");
    return (last.replace(/\.html?$/i, "") || "index");
  }
  function currentScreen(root) {
    root = root || document;
    let best = null, bestLayer = -Infinity, bestDepth = -1;
    for (const el of root.querySelectorAll("[data-screen]")) {
      if (el.id === "fbDlg") continue;
      if (el.tagName === "DIALOG" && !el.open) continue;
      if (!visible(el)) continue;
      const layer = Number(el.dataset.screenLayer || 0);
      let depth = 0;
      for (let p = el; p; p = p.parentElement) depth++;
      if (layer > bestLayer || (layer === bestLayer && depth > bestDepth)) { best = el; bestLayer = layer; bestDepth = depth; }
    }
    return best ? best.dataset.screen : pageName();
  }

  // ---- 画面の写真（見えている範囲の HTML を SVG の foreignObject に描き直して PNG にする。許可のダイアログは出ない）----
  async function toDataURL(url) {
    const blob = await (await fetch(url)).blob();
    return new Promise((ok) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = () => ok(url); r.readAsDataURL(blob); });
  }
  async function captureScreen(opt) {
    const exclude = (opt && opt.exclude) || [], maxWidth = (opt && opt.maxWidth) || 1400;
    try {
      const W = innerWidth, H = innerHeight;
      let css = "";
      for (const sh of document.styleSheets) { try { for (const r of sh.cssRules) css += r.cssText + "\n"; } catch (e) { /* 別の生成元 */ } }
      const src = document.body, clone = src.cloneNode(true);
      const a = [src, ...src.querySelectorAll("*")], b = [clone, ...clone.querySelectorAll("*")];
      const jobs = [];
      for (let i = 0; i < a.length; i++) {
        const s = a[i], d = b[i];
        if (!d) continue;
        if (s.tagName === "INPUT") { if (s.type === "checkbox" || s.type === "radio") { if (s.checked) d.setAttribute("checked", ""); } else d.setAttribute("value", s.value); }
        else if (s.tagName === "TEXTAREA") d.textContent = s.value;
        else if (s.tagName === "SELECT") { const o = d.options && d.options[s.selectedIndex]; if (o) o.setAttribute("selected", ""); }
        else if (s.tagName === "CANVAS") { try { const im = document.createElement("img"); im.src = s.toDataURL(); im.style.cssText = s.style.cssText; im.width = s.clientWidth; im.height = s.clientHeight; d.replaceWith(im); } catch (e) { /* 汚れた canvas */ } }
        else if (s.tagName === "IMG" && s.currentSrc && visible(s)) jobs.push(toDataURL(s.currentSrc).then((u) => d.setAttribute("src", u)).catch(() => d.removeAttribute("src")));
        else if (s.tagName === "IMG") d.removeAttribute("src");
        if (s.scrollTop > 0 && s !== src) {
          const sh = document.createElement("div");
          sh.style.cssText = `margin-top:-${s.scrollTop}px;height:0`;
          d.prepend(sh);
        }
        if (s.tagName === "DIALOG" && s.open) d.setAttribute("open", "");
      }
      for (const sel of exclude) clone.querySelectorAll(sel).forEach((x) => x.remove());
      clone.querySelectorAll("script,audio,video,iframe").forEach((x) => x.remove());
      await Promise.all(jobs);
      clone.style.position = "relative";
      clone.style.top = `-${scrollY}px`;
      clone.style.margin = "0";
      const html = document.documentElement;
      const bg = getComputedStyle(document.body).backgroundColor;
      const body = new XMLSerializer().serializeToString(clone);
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}"><foreignObject x="0" y="0" width="${W}" height="${H}">` +
        `<div xmlns="http://www.w3.org/1999/xhtml" class="${html.className}" ${html.dataset.theme ? `data-theme="${html.dataset.theme}"` : ""} ` +
        `style="width:${W}px;height:${H}px;overflow:hidden;background:${bg}"><style>${css.replace(/<\/style/gi, "")}</style>${body}</div></foreignObject></svg>`;
      const img = new Image();
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
      await img.decode();
      const scale = Math.min(maxWidth / W, Math.min(2, devicePixelRatio || 1));
      const c = document.createElement("canvas");
      c.width = Math.round(W * scale);
      c.height = Math.round(H * scale);
      const ctx = c.getContext("2d");
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, c.width, c.height);
      ctx.scale(scale, scale);
      ctx.drawImage(img, 0, 0);
      return c.toDataURL("image/png");
    } catch (e) {
      rememberError(`画面の写真を撮れなかった: ${(e && e.message) || e}`);
      return null;
    }
  }

  // ---- Issue の題名と本文（paper_reader の feedback.py・feedback.js と同じ形）----
  function issueTitle(r) {
    const first = (r.comment || "").trim().split("\n")[0] || "（コメントなし）";
    return `[${r.screen}] ${first.slice(0, 70)}`;
  }
  function issueBody(r) {
    const c = r.context || {};
    const L = [`**画面**: \`${r.screen}\``, `**app**: ${r.app} ・ 版 ${r.version} ・ ${c.device || ""}`, `**時刻**: ${r.at || new Date().toISOString()}`];
    if (c.paper) L.push(`**論文・記事**: ${c.paper}`);
    if (c.sentence) L.push(`**今の文**: > ${c.sentence}`);
    if (c.summary) L.push(`**今の状態**: ${c.summary}`);
    if (c.extra) L.push(`**そのときの状態**: ${c.extra}`);
    L.push("", "### コメント", "", (r.comment || "").trim() || "（なし）");
    if (r.imageNote) L.push("", "### 画面の写真", "", r.imageNote);
    if (r.log) L.push("", "<details><summary>システムの記録（末尾）</summary>", "", "```", r.log.trimEnd(), "```", "</details>");
    L.push("", "<!-- 画面の !○ から送った -->");
    return L.join("\n");
  }
  // 書き込み済みの「新しい Issue」画面の URL。長すぎると開けないので、記録 → 今の文の順に削る
  function newIssueUrl(repo, r, maxLen) {
    maxLen = maxLen || 7000;
    const build = (x) => `https://github.com/${repo}/issues/new?labels=feedback&title=${encodeURIComponent(issueTitle(x))}&body=${encodeURIComponent(issueBody(x))}`;
    let x = Object.assign({}, r);
    let url = build(x);
    while (url.length > maxLen && x.log) { x = Object.assign({}, x, { log: x.log.length > 400 ? "…" + x.log.slice(Math.floor(x.log.length / 3)) : "" }); url = build(x); }
    if (url.length > maxLen && x.context && x.context.sentence) { x = Object.assign({}, x, { context: Object.assign({}, x.context, { sentence: x.context.sentence.slice(0, 120) + "…" }) }); url = build(x); }
    return url;
  }

  // 既定の送り方: GitHub の画面を開き、写真は端末に保存（押した操作の中で開く。後回しにするとポップアップとして止められる）
  function openGitHub(opts) {
    return async function (payload) {
      const r = Object.assign({}, payload, { log: payload.client_log,
        imageNote: payload.screenshot ? `（端末に保存した写真 ${opts.app}_screen_*.png を、ここに添付してください）` : "" });
      window.open(newIssueUrl(opts.repo, r), "_blank", "noopener");
      if (payload.screenshot) {
        const a = document.createElement("a");
        const d = new Date(), z = (n) => String(n).padStart(2, "0");
        a.href = payload.screenshot;
        a.download = `${opts.app}_screen_${d.getMonth() + 1}${z(d.getDate())}_${z(d.getHours())}${z(d.getMinutes())}.png`;
        document.body.appendChild(a);
        a.click();
        a.remove();
      }
      return { message: payload.screenshot ? "GitHub を開き、写真を保存しました。写真を添付して「Create」で送ってください" : "GitHub を開きました。「Create」で送ってください" };
    };
  }

  const CSS = `
.fb-btn{position:fixed;top:10px;right:10px;z-index:2147483000;width:26px;height:26px;border-radius:50%;padding:0;cursor:pointer;
  font:700 14px/24px -apple-system,BlinkMacSystemFont,sans-serif;text-align:center;border:1px solid rgba(127,127,127,.55);
  background:rgba(255,255,255,.85);color:#c0392b;opacity:.55;box-shadow:0 1px 3px rgba(0,0,0,.2)}
.fb-btn:hover{opacity:1}
.fb-btn.fb-in-dialog{position:absolute;top:8px;right:8px}
.fb-dlg{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;padding:16px}
.fb-box{background:#fff;color:#222;border:1px solid #ccc;border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.25);padding:16px 18px;
  width:min(560px,100%);max-height:calc(100vh - 32px);overflow:auto;display:flex;flex-direction:column;gap:8px;
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;text-align:left}
.fb-box>*{flex-shrink:0}
.fb-head b{font-size:16px}.fb-muted{color:#666;font-size:12.5px}
.fb-warn{color:#a15c00;font-size:12.5px}
.fb-meta{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}
.fb-text{width:100%;box-sizing:border-box;font:14px/1.5 -apple-system,sans-serif;padding:8px;border:1px solid #ccc;border-radius:8px;background:#fafafa;color:#222}
.fb-row{display:flex;gap:6px;align-items:flex-start}.fb-row input{flex:none;margin:3px 0 0}
.fb-dest{flex-wrap:wrap;gap:4px 12px;align-items:center}.fb-dest label{display:inline-flex;gap:4px;align-items:center}
.fb-dest input:disabled+*,.fb-dest label:has(input:disabled){opacity:.45}
.fb-ctxv{margin:-4px 0 2px 22px}
.fb-img{max-width:100%;max-height:240px;border:1px solid #ccc;border-radius:6px}
.fb-logv pre{max-height:160px;overflow:auto;font-size:11px;background:#f4f4f4;padding:6px;border-radius:6px;white-space:pre-wrap}
.fb-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.fb-actions button{font:14px -apple-system,sans-serif;padding:5px 12px;border-radius:7px;border:1px solid #bbb;background:#f2f2f2;color:#222;cursor:pointer}
.fb-actions .fb-send{background:#2563eb;border-color:#2563eb;color:#fff}
.fb-actions .fb-send:disabled{opacity:.5}
@media (prefers-color-scheme: dark){
  .fb-btn{background:rgba(40,40,40,.85);color:#ff8a7a}
  .fb-box{background:#1f1f1f;color:#eee;border-color:#444}.fb-muted{color:#aaa}
  .fb-text{background:#151515;color:#eee;border-color:#444}.fb-logv pre{background:#151515}
  .fb-actions button{background:#2a2a2a;color:#eee;border-color:#555}
}`;

  // ---- 対話のセッションに送る（issue_hub の画面へ）----
  function localPage() {
    return location.protocol === "file:" || ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
  }
  function sendTalk(opts) {
    return async function (payload) {
      const hub = (opts.hub || "http://127.0.0.1:8934").replace(/\/$/, "");
      let r;
      try {
        r = await fetch(hub + "/api/talk-inbox", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ app: opts.app, screen: payload.screen, comment: payload.comment, version: payload.version,
                                 at: payload.at, context: payload.context, log: payload.client_log, image: payload.screenshot }) });
      } catch (e) {
        throw new Error(`issue_hub の画面（${hub}）に届かない。issue_hub.app を開いてから送ってください`);
      }
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || `issue_hub が受け付けなかった（${r.status}）`);
      return { message: d.message || "対話のセッションに送りました。返事は issue_hub の画面で見られます" };
    };
  }

  function deviceText() {
    const ua = navigator.userAgent;
    const kind = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Mac/.test(ua) ? "Mac" : "ブラウザ";
    return `${kind}・${innerWidth}×${innerHeight}`;
  }

  function install(opts) {
    opts = Object.assign({ screenshot: "off", log: true, version: "", sendLabel: null, talk: true, hub: "http://127.0.0.1:8934", onlyLocal: false }, opts);
    if (opts.onlyLocal && !localPage()) return;
    if (!opts.repo && !opts.send && !(opts.talk && localPage())) throw new Error("AppFeedback.install: repo か、手元の画面での対話（talk）が要る");
    if (!opts.send && opts.repo) { opts.send = openGitHub(opts); opts.sendLabel = opts.sendLabel || "GitHub を開く"; }
    opts.sendTalk = opts.talk && localPage() ? sendTalk(opts) : null;
    addEventListener("error", (e) => rememberError(`${e.message} @ ${(e.filename || "").split("/").pop()}:${e.lineno}`));
    addEventListener("unhandledrejection", (e) => rememberError(`未処理の失敗: ${(e.reason && e.reason.message) || e.reason}`));
    const go = () => {
      if (!document.getElementById("fbStyle")) {
        const st = document.createElement("style");
        st.id = "fbStyle";
        st.textContent = CSS;
        document.head.appendChild(st);
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "fb-btn";
      btn.id = "fbBtn";
      btn.textContent = "!";
      btn.title = "この画面について、改善のコメントを送る";
      btn.setAttribute("aria-label", btn.title);
      btn.addEventListener("mousedown", (e) => e.preventDefault());
      btn.addEventListener("click", (e) => { e.stopPropagation(); open(opts); });
      document.body.appendChild(btn);
      // 開くと後ろが押せなくなるダイアログ（showModal）にも、それぞれ !○ を置く
      for (const d of document.querySelectorAll("dialog")) {
        const b2 = btn.cloneNode(true);
        b2.id = "";
        b2.classList.add("fb-in-dialog");
        b2.addEventListener("mousedown", (e) => e.preventDefault());
        b2.addEventListener("click", (e) => { e.stopPropagation(); open(opts); });
        d.appendChild(b2);
      }
    };
    if (document.body) go(); else document.addEventListener("DOMContentLoaded", go);
  }

  async function open(opts) {
    if (document.getElementById("fbDlg")) return;
    const screen = currentScreen();
    const ctx = (() => { try { return (opts.getContext && opts.getContext()) || {}; } catch (e) { return {}; } })();
    ctx.device = ctx.device || deviceText();
    const allowShot = opts.screenshot !== "never";
    let shot = null;
    const SHOT_EXCLUDE = ["#fbBtn", "#fbDlg", ".fb-in-dialog", "#fbStyle"];
    if (opts.screenshot === "on") {                   // "off" のときは、チェックを入れたときに撮る（付けない写真は撮らない）
      const btns = document.querySelectorAll(".fb-btn");
      btns.forEach((b) => { b.style.visibility = "hidden"; });
      shot = await captureScreen({ exclude: SHOT_EXCLUDE });
      btns.forEach((b) => { b.style.visibility = ""; });
    }
    const log = opts.log === false ? "" : [(() => { try { return (opts.getLog && opts.getLog()) || ""; } catch (e) { return ""; } })(),
                 errorLog() && `---- 画面のエラー ----\n${errorLog()}`].filter(Boolean).join("\n");
    const ctxText = [ctx.summary, ctx.paper, ctx.sentence && `「${ctx.sentence.slice(0, 60)}…」`, ctx.extra].filter(Boolean).join(" ・ ");

    const dlg = document.createElement("div");
    dlg.id = "fbDlg";
    dlg.className = "fb-dlg";
    dlg.innerHTML = `
      <div class="fb-box" role="dialog" aria-label="改善のコメント">
        <div class="fb-head"><b>改善のコメント</b> <span class="fb-muted fb-where"></span></div>
        <div class="fb-row fb-dest"><span class="fb-muted">送り先:</span>
          <label><input type="radio" name="fbDest" value="github" class="fb-d-gh"> GitHub の Issue</label>
          <label class="fb-d-talkrow"><input type="radio" name="fbDest" value="talk" class="fb-d-talk"> 対話のセッション（issue_hub）</label></div>
        <div class="fb-warn fb-public" hidden>この repo は公開です。送った中身（コメント・写真・記録）は誰でも読めます。</div>
        <div class="fb-meta"><div>画面: <code class="fb-screen"></code></div><div class="fb-muted fb-ver"></div></div>
        <textarea class="fb-text" rows="5" placeholder="どこを、どう直したいか（例: ボタンが小さい、ここで止まった）"></textarea>
        <label class="fb-row fb-ctxrow"><input type="checkbox" class="fb-ctx" checked> <span>今の状態を付ける</span></label>
        <div class="fb-muted fb-ctxv"></div>
        <label class="fb-row fb-shotrow"><input type="checkbox" class="fb-shot"> <span>画面の写真を付ける</span></label>
        <div class="fb-shotbox"><img class="fb-img" alt="画面の写真"><div class="fb-muted fb-noshot" hidden>このブラウザでは写真を撮れませんでした。⌘⇧4 などで撮って、ここに貼り付けられます（⌘V）。</div></div>
        <label class="fb-row fb-logrow"><input type="checkbox" class="fb-log" checked> <span>画面の記録（エラーなど）の末尾を付ける</span></label>
        <details class="fb-logv"><summary class="fb-muted">付ける記録を見る</summary><pre></pre></details>
        <div class="fb-actions"><button type="button" class="fb-send"></button> <button type="button" class="fb-cancel">やめる</button> <span class="fb-msg"></span></div>
      </div>`;
    (document.querySelector("dialog[open]") || document.body).appendChild(dlg);
    const $q = (s) => dlg.querySelector(s);
    // 送り先: GitHub の Issue か、対話のセッション（issue_hub）。repo が無ければ GitHub は押せない（見た目は変えない）
    const ghOk = !!opts.send, talkOk = !!opts.sendTalk;
    const ghR = $q(".fb-d-gh"), talkR = $q(".fb-d-talk");
    ghR.disabled = !ghOk;
    if (!ghOk) ghR.parentElement.title = "この app は GitHub で管理していない";
    if (!talkOk) $q(".fb-d-talkrow").hidden = true;
    let dest = (() => { try { return localStorage.getItem("feedback.dest"); } catch (e) { return null; } })();
    if (dest === "talk" && !talkOk) dest = null;
    if (dest === "github" && !ghOk) dest = null;
    dest = dest || (ghOk ? "github" : "talk");
    const paintDest = () => {
      ghR.checked = dest === "github"; talkR.checked = dest === "talk";
      $q(".fb-where").textContent = dest === "github" ? `GitHub の新しい Issue（${opts.repo || ""}）にします`
        : `${opts.app} の対話のセッションに送り、issue_hub で返事を待ちます`;
      $q(".fb-public").hidden = !(dest === "github" && opts.isPublic);
      $q(".fb-send").textContent = dest === "github" ? (opts.sendLabel || "GitHub に送る") : "対話に送る";
    };
    for (const r of [ghR, talkR]) r.onchange = () => {
      dest = r.value;
      try { localStorage.setItem("feedback.dest", dest); } catch (e) { /* 覚えられなくても続ける */ }
      paintDest();
    };
    if (!ghOk && !talkOk) $q(".fb-dest").hidden = true;
    $q(".fb-screen").textContent = screen;
    $q(".fb-ver").textContent = `${opts.app} ・ 版 ${opts.version}`;
    paintDest();
    if (ctxText) $q(".fb-ctxv").textContent = ctxText; else { $q(".fb-ctxrow").hidden = true; $q(".fb-ctxv").hidden = true; }
    $q(".fb-logv pre").textContent = log || "（記録は無し）";
    if (opts.log === false) { $q(".fb-logrow").hidden = true; $q(".fb-logv").hidden = true; $q(".fb-log").checked = false; }
    let image = shot;
    const shotBox = $q(".fb-shotbox"), shotChk = $q(".fb-shot");
    shotChk.checked = opts.screenshot === "on";
    const showImg = () => {
      shotBox.hidden = !shotChk.checked;
      $q(".fb-img").hidden = !image; $q(".fb-noshot").hidden = !!image; if (image) $q(".fb-img").src = image;
    };
    let shooting = false;
    const onShotChange = async () => {
      if (shotChk.checked && !image && !shooting) {     // 写真はダイアログごと除いて描くので、窓は閉じなくてよい
        shooting = true;
        image = await captureScreen({ exclude: SHOT_EXCLUDE });
        shooting = false;
      }
      showImg();
    };
    if (!allowShot) { $q(".fb-shotrow").hidden = true; shotBox.hidden = true; } else { shotChk.onchange = onShotChange; showImg(); }
    if (allowShot) dlg.addEventListener("paste", (e) => {
      const f = [...((e.clipboardData && e.clipboardData.files) || [])].find((x) => x.type.startsWith("image/"));
      if (!f) return;
      e.preventDefault();
      const r = new FileReader();
      r.onload = () => { const im = new Image(); im.onload = () => { const c = document.createElement("canvas"); c.width = im.width; c.height = im.height; c.getContext("2d").drawImage(im, 0, 0); image = c.toDataURL("image/png"); shotChk.checked = true; showImg(); }; im.src = r.result; };
      r.readAsDataURL(f);
    });
    const close = () => dlg.remove();
    $q(".fb-cancel").onclick = close;
    dlg.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") close(); }, true);
    dlg.addEventListener("keyup", (e) => e.stopPropagation(), true);
    dlg.addEventListener("click", (e) => { e.stopPropagation(); if (e.target === dlg) close(); });
    dlg.addEventListener("pointerdown", (e) => e.stopPropagation());
    $q(".fb-send").onclick = async () => {
      const comment = $q(".fb-text").value.trim();
      if (!comment) { $q(".fb-msg").textContent = "コメントを書いてください"; $q(".fb-text").focus(); return; }
      const payload = { screen, comment, app: opts.app, version: opts.version, at: new Date().toISOString(),
                        context: $q(".fb-ctx").checked ? ctx : { device: ctx.device },
                        screenshot: allowShot && shotChk.checked ? image : null,
                        client_log: $q(".fb-log").checked ? log : "", include_log: $q(".fb-log").checked };
      $q(".fb-send").disabled = true;
      $q(".fb-msg").textContent = "送っています…";
      try {
        const r = await (dest === "talk" ? opts.sendTalk : opts.send)(payload);
        $q(".fb-msg").textContent = "";
        if (r && r.url) {
          const a = document.createElement("a");
          a.href = r.url; a.target = "_blank"; a.rel = "noopener";
          a.textContent = r.number ? `#${r.number}` : "GitHub で開く";
          $q(".fb-msg").append("送りました: ", a);
        } else $q(".fb-msg").textContent = (r && r.message) || "送りました";
        $q(".fb-cancel").textContent = "閉じる";
      } catch (e) {
        $q(".fb-msg").textContent = `送れませんでした: ${e.message}`;
        $q(".fb-send").disabled = false;
      }
    };
    setTimeout(() => $q(".fb-text").focus(), 0);
  }

  window.AppFeedback = { install, open, currentScreen, captureScreen, issueTitle, issueBody, newIssueUrl, errorLog };
})();
