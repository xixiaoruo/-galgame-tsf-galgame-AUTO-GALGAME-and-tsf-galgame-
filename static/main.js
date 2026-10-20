/* 自动化AI Galgame 前端逻辑 */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------- 本地 token（B-10 / t39）----------
   服务端启动时随机生成本轮 token（不写死在源码里），写操作（POST/DELETE）必须带上。
   同源页面由服务端下发 SameSite=Strict cookie 自动携带；这里再显式取一次并用
   X-Local-Token 头发送，作为双保险（cookie 被浏览器策略挡掉时仍可用）。
   取不到就返回空串——请求照发，由服务端按缺 token 拒绝并给出可读 detail。 */
let TSF_TOKEN = "";
let TSF_TOKEN_PROMISE = null;
async function ensureToken() {
  if (TSF_TOKEN) return TSF_TOKEN;
  if (!TSF_TOKEN_PROMISE) {
    TSF_TOKEN_PROMISE = fetch("/api/local-token", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { TSF_TOKEN = (d && d.token) || ""; return TSF_TOKEN; })
      .catch(() => "")
      .finally(() => { TSF_TOKEN_PROMISE = null; });
  }
  return TSF_TOKEN_PROMISE;
}

/* ---------- 全局状态 ---------- */
const S = {
  sid: null,
  turn: null,
  protName: "",
  npcs: [],
  tsf: null,           // 状态面板数据
  vn: null,            // 普通模式状态数据
  coc: null,           // CoC 模式状态数据（P18-A：DOOM / 双血条 / 属性 / 标签）
  cocPresets: [],      // 1.9.19：CoC 剧本预设清单
  shop: null,          // 1.9.19：当前固定商店视图
  shopId: "",          // 1.9.19：当前选中的商店 id
  aiData: null,        // 1.9.20：AI 控制台（请求 / 思考 / 原始输出 / 对话 / 导演指令）
  aiEntryId: null,     // 1.9.20：当前查看的调用记录
  aiTab: "think",      // 1.9.20：think / edit / direct
  aiDirective: "",     // 1.9.20：当前导演指令
  charStats: null,     // 每个角色一套数值（两种模式共用）
  statWho: null,       // 状态面板当前查看的角色
  charPrev: {},        // 上一幕各角色数值（算 ▲▼）
  tsfPrev: null,       // 上一幕主角转变数值
  mode: "tsf",         // tsf / vn
  version: "",         // 服务端版本号
  assets: null,
  bgMap: {},
  lineIdx: 0,
  typing: false,
  typeTimer: null,
  pollTimer: null,
  currentBgId: null,
  mock: { llm: true, image: true },
  title: "",
};

/* ---------- 工具 ---------- */

function on(id, ev, fn) {
  const el = $(id);
  if (el) el.addEventListener(ev, fn);
}

// 全局错误兜底：以 toast 呈现，而不是无声的白屏/死页
window.addEventListener("error", (e) => {
  const el = $("toast");
  if (el) {
    el.textContent = "脚本出错：" + (e.message || "未知错误") + "（请强制刷新 Ctrl+F5）";
    el.classList.remove("hidden");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add("hidden"), 6000);
  }
});

function toast(msg, ms = 3200) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  // 1.8.0：按语义标记语气（成功/错误），UI2 皮肤据此换图标与描边
  const text = String(msg || "");
  el.dataset.tone = /失败|错误|出错|无法|不可用|拒绝/.test(text)
    ? "error"
    : (/已|完成|就绪|保存成功|✓/.test(text) ? "ok" : "info");
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), ms);
}

async function api(path, opts = {}) {
  // 1.7.23：前端 90s 超时兜底——服务端即使卡住（LLM 慢/资产生成排队），
  // 加载层也会收场并给出错误提示，绝不让页面无限「加载中」
  // 1.7.34：opts.timeoutMs 可覆盖超时（二周目=普通开局+开局后置任务，放宽）
  const { timeoutMs = 90000, ...rest } = opts || {};
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  // B-10（t39）：写操作需要本轮本地 token。服务启动时随机生成，同源读取；
  // 同源页面也会被服务端下发 SameSite=Strict cookie（双保险，任一条即可通过）。
  const method = String((rest.method || "GET")).toUpperCase();
  const headers = { "Content-Type": "application/json", ...(rest.headers || {}) };
  if (method !== "GET" && method !== "HEAD") {
    const tok = await ensureToken();
    if (tok) headers["X-Local-Token"] = tok;
  }
  try {
    const resp = await fetch(path, {
      // 存档/读档/资产状态都是动态数据：QtWebEngine 与部分浏览器会对 GET
      // 做启发式缓存，导致「刚存的档在列表里看不到」。统一禁用 HTTP 缓存。
      cache: "no-store",
      headers,
      signal: ctl.signal,
      ...rest,
    });
    let data = null;
    try { data = await resp.json(); } catch { /* 空响应 */ }
    if (!resp.ok) {
      throw new Error((data && data.detail) || `请求失败（${resp.status}）`);
    }
    return data;
  } catch (e) {
    if (e && e.name === "AbortError") {
      throw new Error(`请求超时（${Math.round(timeoutMs / 1000)} 秒），服务可能正忙，请稍后重试`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/* ---------- 开始界面：主角锚点 ---------- */

function buildAnchor() {
  const hair = $("prot-hair").value;
  const eyes = $("prot-eyes").value;
  const outfit = $("prot-outfit").value;
  const vibe = $("prot-vibe").value;
  const extra = $("prot-extra").value.trim();
  return [hair, eyes, outfit, vibe, extra].filter(Boolean).join("，");
}

function refreshAnchorPreview() {
  $("anchor-text").textContent = buildAnchor() || "（未设置）";
}
["prot-hair", "prot-eyes", "prot-outfit", "prot-vibe"].forEach((id) =>
  on(id, "change", refreshAnchorPreview));
// —— UI 绑定段 bindUiPart01（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart01() {
  on("prot-extra", "input", refreshAnchorPreview);
}

/* ---------- 开始界面：NPC 卡片（1.9.0：身份 / 背景 / AI 谱写 / 随机） ---------- */

const CHAR_CARD_LIMIT = 4;

function traitsText(raw) {
  const arr = Array.isArray(raw) ? raw : String(raw || "").split(/[,，、;；/]+/);
  return arr.map((x) => String(x).trim()).filter(Boolean).join("、");
}

function addCharCard(data = {}) {
  const list = $("char-list");
  if (!list) return null;
  if (list.querySelectorAll(".char-card").length >= CHAR_CARD_LIMIT) {
    toast(`最多 ${CHAR_CARD_LIMIT} 位其他角色（可在游戏内由 AI 继续引入）`);
    return null;
  }
  const card = document.createElement("div");
  card.className = "char-card";
  card.innerHTML = `
    <div class="char-card-head">
      <input class="text-input char-name" placeholder="角色名" maxlength="12" value="${escapeHtml(data.name || "")}">
      <input class="text-input char-role" placeholder="身份 / 与主角的关系（如：同班同学）" maxlength="20" value="${escapeHtml(data.role || "")}">
      <span class="char-tools">
        <button class="btn ghost mini btn-char-ai" type="button" title="用 AI 根据世界观、外貌与性格自动谱写角色背景">✨ 背景</button>
        <button class="btn ghost mini btn-char-tune" type="button" title="用 AI 微调这张角色卡（不会改写固定字段）">✨ 微调</button>
        <button class="btn ghost mini btn-char-lib" type="button" title="从角色库选一张卡填入这张角色卡（保留立绘复用）">📚</button>
        <button class="btn ghost mini btn-char-random" type="button" title="随机生成一位角色（本地，不消耗 API）">🎲</button>
        <button class="btn ghost mini btn-del" type="button">✕</button>
      </span>
    </div>
    <div class="char-grid">
      <div class="char-field">
        <small>外貌描述（发色发型、瞳色、服装、气质）</small>
        <textarea class="text-input char-appearance" rows="2" maxlength="300">${escapeHtml(data.appearance || "")}</textarea>
      </div>
      <div class="char-field">
        <small>性格 / 说话风格</small>
        <textarea class="text-input char-personality" rows="2" maxlength="200">${escapeHtml(data.personality || "")}</textarea>
      </div>
    </div>
    <div class="char-profile">
      <label>年龄<input class="text-input char-age" type="number" min="18" max="80" placeholder="22" value="${data.age || ""}"></label>
      <label>身高(cm)<input class="text-input char-height" type="number" min="130" max="220" placeholder="164" value="${data.height_cm || ""}"></label>
      <label>体型<input class="text-input char-body" maxlength="30" placeholder="纤细高挑 / 娇小柔软" value="${escapeHtml(data.body_type || "")}"></label>
      <label>三围<input class="text-input char-measure" maxlength="40" placeholder="B86 / W58 / H88" value="${escapeHtml(data.measurements || "")}"></label>
      <label>生日<input class="text-input char-birthday" maxlength="20" placeholder="5月12日" value="${escapeHtml(data.birthday || "")}"></label>
    </div>
    <div class="char-grid">
      <div class="char-field">
        <small>性格标签（逗号分隔，最多 8 个）</small>
        <input class="text-input char-traits" maxlength="120" placeholder="慢热、嘴硬心软、观察力强" value="${escapeHtml(traitsText(data.traits))}">
      </div>
      <div class="char-field">
        <small>说话风格</small>
        <input class="text-input char-speech" maxlength="100" placeholder="语速偏慢，措辞礼貌" value="${escapeHtml(data.speech_style || "")}">
      </div>
    </div>
    <div class="char-grid">
      <div class="char-field"><small>喜欢</small><input class="text-input char-likes" maxlength="100" placeholder="旧书、雨天、热牛奶" value="${escapeHtml(data.likes || "")}"></div>
      <div class="char-field"><small>厌恶</small><input class="text-input char-dislikes" maxlength="100" placeholder="吵闹的人群、被催促" value="${escapeHtml(data.dislikes || "")}"></div>
    </div>
    <label class="char-lock">
      <input type="checkbox" class="char-lock-personality" ${data.personality_locked ? "checked" : ""}>
      🔒 固定性格与说话风格（AI 微调 / 后续剧情不会改写）
    </label>
    <label class="char-lock">
      <input type="checkbox" class="char-lock-profile">
      🔒 固定详细人设（年龄 / 身高 / 体型 / 三围 / 生日）
    </label>
    <div class="char-field">
      <small>角色背景（出身 / 与主角的因缘 / 可被剧情利用的钩子）
        <span class="char-bg-status">${data.background ? "✓ 已有背景" : "可点「✨ 背景」自动谱写"}</span>
      </small>
      <textarea class="text-input char-background" rows="3" maxlength="400">${escapeHtml(data.background || "")}</textarea>
    </div>`;
  card.querySelector(".btn-del").addEventListener("click", () => {
    card.remove();
    updateCharCount();
    if (window.UI2 && UI2.updateReadiness) UI2.updateReadiness();
  });
  card.querySelector(".btn-char-ai").addEventListener("click", () => aiCharBackground(card));
  card.querySelector(".btn-char-tune").addEventListener("click", () => openFineTune(card));
  card.querySelector(".btn-char-random").addEventListener("click", () => randomizeCharCard(card));
  card.querySelector(".btn-char-lib").addEventListener("click", () => {
    if (typeof openLibPicker === "function") openLibPicker("card", card);
    else toast("角色库尚未就绪");
  });
  card.querySelector(".char-background").addEventListener("input", () => {
    const st = card.querySelector(".char-bg-status");
    st.textContent = card.querySelector(".char-background").value.trim()
      ? "✓ 已有背景" : "可点「✨ 背景」自动谱写";
  });
  list.appendChild(card);
  updateCharCount();
  if (window.UI2 && UI2.updateReadiness) UI2.updateReadiness();
  return card;
}

function updateCharCount() {
  const list = $("char-list");
  if (!list) return;
  const n = list.querySelectorAll(".char-card").length;
  const btn = $("btn-add-char");
  if (btn) {
    btn.disabled = n >= CHAR_CARD_LIMIT;
    btn.textContent = n >= CHAR_CARD_LIMIT ? `已满 ${CHAR_CARD_LIMIT} 位` : "＋ 添加角色";
  }
}

function charTraits(card) {
  return String(card.querySelector(".char-traits")?.value || "")
    .split(/[,，、;；/]+/).map((x) => x.trim()).filter(Boolean).slice(0, 8);
}

function charInt(card, sel) {
  const n = parseInt(card.querySelector(sel)?.value || "0", 10);
  return Number.isFinite(n) ? n : 0;
}

function collectNPCs() {
  return [...document.querySelectorAll(".char-card")].map((c) => {
    const plock = !!c.querySelector(".char-lock-personality")?.checked;
    const flock = !!c.querySelector(".char-lock-profile")?.checked;
    const locks = [];
    if (plock) locks.push("personality", "traits", "speech_style");
    if (flock) locks.push("age", "height_cm", "body_type", "measurements", "birthday");
    return {
      name: c.querySelector(".char-name").value.trim(),
      role: c.querySelector(".char-role") ? c.querySelector(".char-role").value.trim() : "",
      appearance: c.querySelector(".char-appearance").value.trim(),
      personality: c.querySelector(".char-personality").value.trim(),
      traits: charTraits(c),
      age: charInt(c, ".char-age"),
      height_cm: charInt(c, ".char-height"),
      body_type: c.querySelector(".char-body")?.value.trim() || "",
      measurements: c.querySelector(".char-measure")?.value.trim() || "",
      birthday: c.querySelector(".char-birthday")?.value.trim() || "",
      speech_style: c.querySelector(".char-speech")?.value.trim() || "",
      likes: c.querySelector(".char-likes")?.value.trim() || "",
      dislikes: c.querySelector(".char-dislikes")?.value.trim() || "",
      personality_locked: plock,
      locked_fields: locks,
      background: c.querySelector(".char-background") ? c.querySelector(".char-background").value.trim() : "",
    };
  }).filter((c) => c.name).slice(0, CHAR_CARD_LIMIT);
}

function validateCharCards(protagonist) {
  const chars = collectNPCs();
  const seen = new Set();
  const pname = String(protagonist || "").trim();
  for (const c of chars) {
    if (c.name === pname) {
      toast(`角色名「${c.name}」与主角同名，请改一个名字`);
      return false;
    }
    if (seen.has(c.name)) {
      toast(`角色名「${c.name}」重复了，请改名或删掉一张卡`);
      return false;
    }
    seen.add(c.name);
  }
  return true;
}

function charCardContext() {
  return {
    title: $("inp-title") ? $("inp-title").value.trim() : "",
    world: $("inp-world") ? $("inp-world").value.trim() : "",
    protagonist: $("prot-name") ? ($("prot-name").value.trim() || "主角") : "主角",
  };
}

function fillCharCard(card, data) {
  if (!card || !data) return;
  const set = (sel, val) => {
    const el = card.querySelector(sel);
    if (el) el.value = val || "";
  };
  set(".char-name", data.name);
  set(".char-role", data.role);
  set(".char-appearance", data.appearance);
  set(".char-personality", data.personality);
  set(".char-background", data.background);
  set(".char-traits", traitsText(data.traits));
  set(".char-age", data.age || "");
  set(".char-height", data.height_cm || "");
  set(".char-body", data.body_type);
  set(".char-measure", data.measurements);
  set(".char-birthday", data.birthday);
  set(".char-speech", data.speech_style);
  set(".char-likes", data.likes);
  set(".char-dislikes", data.dislikes);
  const lock = card.querySelector(".char-lock-personality");
  if (lock) lock.checked = !!data.personality_locked;
  const flock = card.querySelector(".char-lock-profile");
  if (flock) {
    const locks = new Set(data.locked_fields || []);
    flock.checked = ["age", "height_cm", "body_type", "measurements", "birthday"]
      .every((k) => locks.has(k));
  }
  const st = card.querySelector(".char-bg-status");
  if (st) st.textContent = data.background ? "✓ 已有背景" : "可点「✨ 背景」自动谱写";
  if (window.UI2 && UI2.updateReadiness) UI2.updateReadiness();
}

async function aiCharBackground(card) {
  const name = card.querySelector(".char-name").value.trim();
  if (!name) { toast("先给角色起个名字"); return; }
  const btn = card.querySelector(".btn-char-ai");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "谱写中…";
  try {
    const ctx = charCardContext();
    const data = await api("/api/characters/background", {
      method: "POST",
      body: JSON.stringify({
        name,
        role: card.querySelector(".char-role").value.trim(),
        appearance: card.querySelector(".char-appearance").value.trim(),
        personality: card.querySelector(".char-personality").value.trim(),
        world: ctx.world, title: ctx.title, protagonist: ctx.protagonist,
        existing: collectNPCs().map((c) => c.name).filter((n) => n !== name),
      }),
    });
    card.querySelector(".char-background").value = data.background || "";
    card.querySelector(".char-bg-status").textContent =
      data.source === "llm" ? "✓ AI 谱写" : "✓ 本地谱写（AI 不可用）";
    toast(`已为「${name}」谱写背景`);
  } catch (e) {
    toast("自动谱写失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function randomizeCharCard(card) {
  const btn = card.querySelector(".btn-char-random");
  btn.disabled = true;
  try {
    const ctx = charCardContext();
    const avoid = collectNPCs().map((c) => c.name).concat([ctx.protagonist]);
    const data = await api("/api/characters/random", {
      method: "POST",
      body: JSON.stringify({ world: ctx.world, avoid_names: avoid }),
    });
    fillCharCard(card, data);
    toast(`🎲 随机角色「${data.name}」已填入`);
  } catch (e) {
    toast("随机角色失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ---------- 角色卡：AI 自主微调 ---------- */

let fineTuneCard = null;
const FINETUNE_SUGGESTS = [
  "身高改成 168cm", "体型更纤细", "气质更成熟", "语气更温柔",
  "性格更主动", "加一个「怕生」标签", "说话更简短", "背景更神秘",
];

function openFineTune(card) {
  if (!card) return;
  fineTuneCard = card;
  const name = card.querySelector(".char-name")?.value.trim() || "未命名角色";
  $("finetune-target").textContent = `目标：${name} · 只改未固定字段`;
  $("finetune-note").value = "";
  $("finetune-hint").textContent = "";
  const box = $("finetune-chips");
  box.innerHTML = "";
  FINETUNE_SUGGESTS.forEach((text) => {
    const b = document.createElement("button");
    b.className = "btn ghost action-btn";
    b.type = "button";
    b.textContent = text;
    b.addEventListener("click", () => {
      const n = $("finetune-note");
      n.value = (n.value ? n.value + "；" : "") + text;
    });
    box.appendChild(b);
  });
  $("modal-finetune").classList.remove("hidden");
  setTimeout(() => $("finetune-note").focus(), 30);
}

async function applyFineTune() {
  if (!fineTuneCard) return;
  const instruction = $("finetune-note").value.trim();
  if (!instruction) { toast("请写一条微调要求"); return; }
  const name = fineTuneCard.querySelector(".char-name")?.value.trim() || "";
  const cardData = collectNPCs().find((c) => c.name === name);
  if (!cardData) { toast("角色卡内容为空"); return; }
  const btn = $("btn-finetune-go");
  btn.disabled = true;
  btn.textContent = "微调中…";
  $("finetune-hint").textContent = "AI 正在保留固定字段并改写其余内容…";
  try {
    const ctx = charCardContext();
    const r = await api("/api/characters/fine-tune", {
      method: "POST", timeoutMs: 60000,
      body: JSON.stringify({
        card: cardData, instruction,
        world: ctx.world, title: ctx.title, protagonist: ctx.protagonist,
      }),
    });
    if (!r.character) throw new Error("返回角色卡为空");
    fillCharCard(fineTuneCard, r.character);
    $("finetune-hint").textContent = r.source === "llm"
      ? "✓ AI 微调完成（固定字段已保留）"
      : "✓ 已应用本地微调（AI 不可用）";
    toast(`已微调「${r.character.name || cardData.name}」`);
    setTimeout(() => $("modal-finetune").classList.add("hidden"), 450);
  } catch (e) {
    $("finetune-hint").textContent = "微调失败：" + e.message;
    toast("微调失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "✨ 应用微调";
  }
}

function bindFineTuneUi() {
  const close = () => $("modal-finetune").classList.add("hidden");
  $("btn-finetune-cancel").addEventListener("click", close);
  $("btn-finetune-go").addEventListener("click", applyFineTune);
  $("modal-finetune").addEventListener("click", (e) => {
    if (e.target === $("modal-finetune")) close();
  });
}

/* ---------- 随机数 / 骰子（含 CoC 检定） ---------- */

const rngHistory = [];

function rngPush(html, label) {
  rngHistory.unshift({ html, label: label || "" });
  if (rngHistory.length > 20) rngHistory.length = 20;
  const box = $("rng-history");
  if (!box) return;
  box.innerHTML = rngHistory.map((x) =>
    `<div class="rng-history-row">${x.html}</div>`).join("");
}

function rngShowResult(html) {
  const el = $("rng-result");
  if (el) el.innerHTML = html;
}

function openRng() {
  const cocMode = S.mode === "coc" || S.mode === "coc_tsf";
  const tab = $("rng-tab-coc");
  if (tab) tab.classList.toggle("hidden", !cocMode);
  if (!cocMode) switchRngTab("dice");
  $("rng-hint").textContent = "";
  $("rng-coc-hint").textContent = "";
  $("modal-rng").classList.remove("hidden");
}

function switchRngTab(which) {
  const coc = which === "coc";
  $("rng-dice-pane").classList.toggle("hidden", coc);
  $("rng-coc-pane").classList.toggle("hidden", !coc);
  $("rng-tab-dice").className = "btn mini " + (coc ? "ghost" : "primary");
  const cocMode = S.mode === "coc" || S.mode === "coc_tsf";
  $("rng-tab-coc").className = "btn mini " + (coc ? "primary" : "ghost")
    + (cocMode ? "" : " hidden");
}

async function rollRng() {
  const preset = $("rng-preset").value;
  const notation = preset || $("rng-custom").value.trim() || "1d100";
  const label = $("rng-label").value.trim();
  const btn = $("btn-rng-roll");
  btn.disabled = true;
  try {
    const r = await api("/api/rng/roll", {
      method: "POST",
      body: JSON.stringify({ notation, label }),
    });
    const rolls = (r.rolls || []).join(" + ");
    const mod = r.modifier ? ` ${r.modifier > 0 ? "+" : ""}${r.modifier}` : "";
    const html = `<b>🎲 ${escapeHtml(r.notation)}</b> → <b>${r.total}</b>`
      + `<span class="rng-detail">（${escapeHtml(rolls)}${mod}${label ? " · " + escapeHtml(label) : ""}）</span>`;
    rngShowResult(html);
    rngPush(html, label);
    $("rng-hint").textContent = "✓ 已掷骰";
  } catch (e) {
    $("rng-hint").textContent = "掷骰失败：" + e.message;
    toast("掷骰失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function rollCocCheck() {
  if (!S.sid) { toast("先进入 CoC 对局"); return; }
  const btn = $("btn-rng-coc");
  btn.disabled = true;
  try {
    const r = await api("/api/rng/coc-check", {
      method: "POST",
      body: JSON.stringify({
        sid: S.sid, stat: $("rng-coc-stat").value,
        threat: $("rng-coc-threat").value,
      }),
    });
    const ok = r.success ? "✅ 成功" : "❌ 失败";
    const html = `<b>${ok}</b> · ${escapeHtml(r.stat)} ${r.stat_value}`
      + ` vs d12 ${r.roll} <span class="rng-detail">（${escapeHtml(r.reason || "")}）</span>`;
    rngShowResult(html);
    rngPush(html, "CoC 检定");
    $("rng-coc-hint").textContent = ok;
  } catch (e) {
    $("rng-coc-hint").textContent = "检定失败：" + e.message;
    toast("检定失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

function bindRngUi() {
  $("btn-rng").addEventListener("click", openRng);
  $("btn-rng-close").addEventListener("click", () => $("modal-rng").classList.add("hidden"));
  $("modal-rng").addEventListener("click", (e) => {
    if (e.target === $("modal-rng")) $("modal-rng").classList.add("hidden");
  });
  $("rng-tab-dice").addEventListener("click", () => switchRngTab("dice"));
  $("rng-tab-coc").addEventListener("click", () => switchRngTab("coc"));
  $("rng-preset").addEventListener("change", () => {
    const custom = $("rng-custom");
    if ($("rng-preset").value) custom.value = "";
    custom.disabled = !!$("rng-preset").value;
  });
  $("btn-rng-roll").addEventListener("click", rollRng);
  $("btn-rng-coc").addEventListener("click", rollCocCheck);
  $("rng-custom").disabled = !!$("rng-preset").value;
}

/* ---------- 开始界面：故事书 ---------- */

function addLoreEntry(data = {}) {
  const list = $("lore-list");
  const item = document.createElement("div");
  item.className = "lore-item";
  item.innerHTML = `
    <div class="lore-head">
      <input class="text-input lore-keys" placeholder="触发关键词（逗号分隔，留空+非常驻=不注入）" value="${data.keys || ""}">
      <label class="lore-always"><input type="checkbox" class="lore-always-cb" ${data.always ? "checked" : ""}>常驻</label>
      <button class="btn ghost btn-del">✕</button>
    </div>
    <textarea class="text-input lore-content" rows="2" placeholder="设定内容：世界规则、伏笔、人物秘密…">${data.content || ""}</textarea>`;
  item.querySelector(".btn-del").addEventListener("click", () => item.remove());
  list.appendChild(item);
}

function collectLorebook() {
  return [...document.querySelectorAll(".lore-item")].map((el) => ({
    keys: el.querySelector(".lore-keys").value.trim(),
    content: el.querySelector(".lore-content").value.trim(),
    always: el.querySelector(".lore-always-cb").checked,
  })).filter((e) => e.content);
}
// —— UI 绑定段 bindUiPart02（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart02() {

  /* ---------- 开始界面：随机设定 / 大纲 ---------- */

  $("btn-add-char").addEventListener("click", () => addCharCard());
  $("btn-add-lore").addEventListener("click", () => addLoreEntry());

  $("btn-random").addEventListener("click", async () => {
    const btn = $("btn-random");
    btn.disabled = true;
    btn.textContent = "生成中……";
    try {
      const data = await api(`/api/setup/random?mode=${S.mode}`, { method: "POST" });
      $("inp-title").value = data.title || "";
      $("inp-world").value = data.world || "";
      $("char-list").innerHTML = "";
      (data.characters || []).forEach((c) => addCharCard(c));
      if (data.outline) $("inp-outline").value = data.outline;
    } catch (e) {
      toast("随机生成失败：" + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "🎲 随机生成设定";
    }
  });

  $("btn-clear-outline").addEventListener("click", () => {
    $("inp-outline").value = "";
    toast("大纲已清空（剧情将自由发挥）");
  });

  bindFineTuneUi();
  bindRngUi();

  $("btn-outline").addEventListener("click", async () => {
    const btn = $("btn-outline");
    const world = $("inp-world").value.trim();
    if (!world) { toast("请先填写世界观，再生成大纲"); return; }
    btn.disabled = true;
    btn.textContent = "大纲生成中……";
    try {
      const data = await api("/api/outline", {
        method: "POST",
        body: JSON.stringify({
          world,
          protagonist: $("prot-name").value.trim() || "主角",
          characters: collectNPCs(),
        }),
      });
      $("inp-outline").value = data.outline || "";
      if (data.title && !$("inp-title").value.trim()) $("inp-title").value = data.title;
      toast("大纲已生成，可自由编辑后开局");
    } catch (e) {
      toast("大纲生成失败：" + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "🤖 用 AI 生成大纲";
    }
  });
}

/* ---------- 开始界面：AI 指令注入（文风/限度） ---------- */

const setupDirectives = { presets: [], customs: [] };

async function loadPresetGrid() {
  try {
    const data = await api("/api/directives/presets");
    setupDirectives.presets = data.presets.map((p) => ({ ...p, enabled: false }));
  } catch {
    setupDirectives.presets = [];
  }
  renderPresetGrid();
}

function renderPresetGrid() {
  const grid = $("preset-grid");
  grid.innerHTML = "";
  const groups = {};
  setupDirectives.presets.forEach((p) => {
    (groups[p.group || "其他"] = groups[p.group || "其他"] || []).push(p);
  });
  Object.entries(groups).forEach(([g, items]) => {
    const head = document.createElement("div");
    head.className = "preset-group-name";
    head.textContent = g;
    grid.appendChild(head);
    items.forEach((p) => {
      const chip = document.createElement("label");
      chip.className = "preset-chip" + (p.enabled ? " on" : "");
      chip.innerHTML = `<input type="checkbox" ${p.enabled ? "checked" : ""}>${escapeHtml(p.text)}`;
      chip.querySelector("input").addEventListener("change", (e) => {
        p.enabled = e.target.checked;
        chip.classList.toggle("on", e.target.checked);
      });
      grid.appendChild(chip);
    });
  });
  renderPresetCustoms();
}

function renderPresetCustoms() {
  const box = $("preset-custom-list");
  box.innerHTML = "";
  setupDirectives.customs.forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "directive-row on";
    row.innerHTML = `
      <input type="checkbox" checked>
      <span class="directive-text">${escapeHtml(c.text)} <em class="directive-custom">自定义</em></span>
      <button class="btn mini ghost">✕</button>`;
    row.querySelector("button").addEventListener("click", () => {
      setupDirectives.customs.splice(i, 1);
      renderPresetCustoms();
    });
    box.appendChild(row);
  });
}
// —— UI 绑定段 bindUiPart03（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart03() {

  $("btn-add-preset").addEventListener("click", () => {
    const text = $("preset-new").value.trim();
    if (!text) { toast("请输入指令内容"); return; }
    setupDirectives.customs.push({ id: "", text, enabled: true });
    $("preset-new").value = "";
    renderPresetCustoms();
  });
}

function collectDirectives() {
  return [
    ...setupDirectives.presets.map((p) => ({ id: p.id, enabled: p.enabled })),
    ...setupDirectives.customs.map((c) => ({ id: "", text: c.text, enabled: true })),
  ];
}

/* t73：**自定义剧本**（可选）。玩家在 `#inp-scenario` 里粘一份 JSON，
   就能真正决定 CoC 系模式的地点/谜团/古神/事件卡（而不只是叙述措辞）。

   形状见 `app/coc/scenario.py`；这里只做"是不是合法 JSON 对象"的**前端初筛**，
   真正的 schema 校验（threat 合法性、缺 id 等）由后端 `scenario.normalize` 负责 ——
   后端会回 400 并带上具体原因，前端**不重复实现**一套校验（两套必然漂移）。
   留空 ⇒ 返回 {} ⇒ 后端按"无剧本"处理（零影响）。 */
function collectScenario() {
  const el = $("inp-scenario");
  if (!el) return {};
  const raw = String(el.value || "").trim();
  if (!raw) return {};
  try {
    const obj = JSON.parse(raw);
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
      toast("剧本必须是 JSON 对象（{ … }）", 4000);
      return {};
    }
    return obj;
  } catch (e) {
    toast("剧本 JSON 解析失败：" + String(e.message || e).slice(0, 80), 5000);
    return {};
  }
}
// —— UI 绑定段 bindUiPart04（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart04() {

  $("inp-rating").addEventListener("change", () => {
    const r = $("inp-rating");
    $("inp-r18").disabled = r.value !== "18";
    if (r.value !== "18") $("inp-r18").checked = false;
    $("inp-forced").disabled = r.value !== "18" || !$("inp-r18").checked;
    if (r.value !== "18" || !$("inp-r18").checked) $("inp-forced").checked = false;
  });
  $("inp-r18").addEventListener("change", () => {
    $("inp-forced").disabled = !$("inp-r18").checked;
    if (!$("inp-r18").checked) $("inp-forced").checked = false;
  });

  /* ---------- 开始游戏 ---------- */

  $("btn-start").addEventListener("click", async () => {
    const world = $("inp-world").value.trim();
    // P18-A：CoC 模式**不需要**世界观（它的题材由古神/角色/难度决定），
    // 且**不带头像管线**（no portrait / anchor），所以这条校验对 coc 跳过。
    // C×T4（t56）：coc_tsf 与 coc 同规则（世界观可留空）。
    const isCocMode = S.mode === "coc" || S.mode === "coc_tsf";
    if (!isCocMode && !world) {
      toast("请先填写世界观/剧情设定，或点「随机生成设定」"); return;
    }
    const pname = $("prot-name").value.trim() || "主角";
    if (!isCocMode && !validateCharCards(pname)) return;
    const cocSel = (id, dflt) => { const el = $(id); return el ? el.value : dflt; };
    const payload = {
      mode: S.mode,
      title: $("inp-title").value.trim(),
      // t73：**不要把玩家填的世界观丢掉**。原来写成 `isCocMode ? (world || "盐川町") : world`，
      // 效果是 CoC 系模式下"填了也白填"（引擎当时也确实不读它，是双重失效）。
      // 现在引擎会把 world/outline/lorebook/directives 组装成"纯文本剧本"并注入 prompt，
      // 所以这里**原样透传**；留空时才交给后端回落（后端 `_scenario_of` 只在有内容时才建剧本）。
      world: world,
      // t73：结构化剧本（可选）。玩家/作者可传入 {locations,mysteries,old_gods,cards,...}
      // 来真正决定内容；留空 = 用随包内容（零影响）。
      scenario: collectScenario(),
      scenario_id: isCocMode ? collectCocScenarioId() : "",
      characters: isCocMode ? [] : collectNPCs(),
      protagonist: isCocMode
        ? { name: cocSel("coc-character", "Kirie"), anchor: "" }
        : {
            name: pname,
            anchor: S.mode === "vn" ? $("vn-anchor").value.trim() : buildAnchor(),
          },
      // CoC 开局参数（服务端 app/coc/session.py::start 读取）
      difficulty: cocSel("coc-difficulty", "Initiate"),
      death_mode: cocSel("coc-death-mode", "last_stand"),
      old_god: cocSel("coc-old-god", "Goizo"),
      character: cocSel("coc-character", "Kirie"),
      outline: $("inp-outline").value.trim(),
      lorebook: collectLorebook(),
      directives: collectDirectives(),
      catchphrases: ($("inp-catchphrases").value || "").split(String.fromCharCode(10))
        .map(s => s.trim()).filter(Boolean).slice(0, 5),
      content_rating: $("inp-rating").value,
      r18_enabled: $("inp-r18").checked,
      allow_forced: $("inp-forced").checked && $("inp-r18").checked
        && $("inp-rating").value === "18",
      library_imports: window.__libImports || [],
      stage_count: Number(S.stageCount) || 0,
      tsf_target: collectTsfTarget(),
      // 1.9.0：制服字段此前只存进设定档案、**没进开局 payload**，导致
      // 玩家填的本局制服在真正开局时从未生效（只剩场景自动映射）。
      uniform_en: $("uniform-en") ? $("uniform-en").value.trim() : "",
      uniform_neg: $("uniform-neg") ? $("uniform-neg").value.trim() : "",
      uniform_name: ($("uniform-en") && ($("uniform-en").value.trim()
        || ($("uniform-neg") && $("uniform-neg").value.trim())))
        ? ($("uniform-name") ? $("uniform-name").textContent.trim() : "") : "",
    };
    const btn = $("btn-start");
    btn.disabled = true;
    btn.textContent = "正在开场……";
    try {
      const state = await api("/api/game/start", {
        method: "POST", body: JSON.stringify(payload),
      });
      localStorage.setItem("galgame_sid", state.sid);
      enterGame(state, { instant: false });
    } catch (e) {
      toast("开局失败：" + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "▶ 开始游戏";
    }
  });

  $("btn-credits").addEventListener("click", () => $("modal-credits").classList.remove("hidden"));
  // B-22（t37）：底部 footer 那个「🍰 制作名单」按钮原先与上面的按钮**共用 id**
  // （index.html 里 id="btn-credits" 出现两次），$() 只会拿到第一个，导致它点不动。
  // 现已给它唯一 id="btn-credits-foot" 并在此绑定。
  $("btn-credits-foot").addEventListener("click", () => $("modal-credits").classList.remove("hidden"));
  $("btn-credits-game").addEventListener("click", () => $("modal-credits").classList.remove("hidden"));
  $("btn-credits-close").addEventListener("click", () => $("modal-credits").classList.add("hidden"));
}

/* ---------- 关闭游戏（左上角）：停止服务并关闭窗口 ---------- */

async function shutdownGame() {
  if (!confirm("关闭游戏？所有窗口与服务都会停止（进度已自动保存）。")) return;
  let sdNote = "";
  try {
    const r = await fetch("/api/shutdown", { method: "POST" }).then((x) => x.json());
    sdNote = r && r.sd_running
      ? "（绘画服务仍在运行，不受影响）" : "";
  } catch { /* 服务已停 */ }
  window.close();
  setTimeout(() => {
    document.body.innerHTML =
      '<div style="color:#8a87a8;text-align:center;padding:60px;font-size:15px">' +
      '游戏已关闭' + sdNote + '，可以关闭本标签页了。</div>';
  }, 600);
}
// —— UI 绑定段 bindUiPart05（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart05() {
  $("btn-shutdown").addEventListener("click", shutdownGame);
  $("btn-shutdown-setup").addEventListener("click", shutdownGame);
}

/* ---------- 模式切换（TSF / 普通 Galgame / CoC 跑团） ---------- */

function setSetupMode(mode, silent) {
  // P18-A（t51）：三模式；C×T4（t56）：新增第四模式 coc_tsf。
  // 未知值一律回退 tsf（与旧行为一致，不新增异常路径）。
  S.mode = (mode === "vn" || mode === "coc" || mode === "coc_tsf") ? mode : "tsf";
  $("mode-btn-tsf").classList.toggle("on", S.mode === "tsf");
  $("mode-btn-vn").classList.toggle("on", S.mode === "vn");
  const cocBtn = $("mode-btn-coc");
  if (cocBtn) cocBtn.classList.toggle("on", S.mode === "coc");
  const cocTsfBtn = $("mode-btn-coc-tsf");
  if (cocTsfBtn) cocTsfBtn.classList.toggle("on", S.mode === "coc_tsf");
  localStorage.setItem("galgame_mode", S.mode);
  // 模式差异：TSF 用锚点构建器（发色/瞳色/服装/气质+预览）；普通模式只需一段外貌描述；
  // CoC / CoC×TSF 用调查员设定（难度/死法/古神/角色），与两者的开局表单都不同。
  const isVn = S.mode === "vn";
  const isCoc = S.mode === "coc" || S.mode === "coc_tsf";
  const isCocTsf = S.mode === "coc_tsf";
  // 1.9.20：模式简介固定在模式按钮下方；CoC 介绍不再只藏在设置框提示里。
  const intro = $("mode-intro");
  if (intro) {
    intro.textContent = {
      tsf: "TSF Galgame：普通剧情 + 性别同化率 / 六段转变立绘。",
      vn: "普通 Galgame：日常情感与关系推进；不推进 TSF 转变。",
      coc: "CoC 跑团：DOOM / STA / REA / SAN、d12 检定、固定商店与剧本预设；纯文字，不生成立绘。",
      coc_tsf: "CoC×TSF：CoC 调查跑团 + TSF 转变进度与阶段立绘。",
    }[S.mode] || "";
  }
  const vnBox = $("vn-anchor-box");
  if (vnBox) vnBox.classList.toggle("hidden", !isVn);
  const cocBox = $("coc-setup-box");
  if (cocBox) cocBox.classList.toggle("hidden", !isCoc);
  // C×T4：同一个设置框两种 CoC 模式共用，只换提示文案（避免复制整块 → 不会新增 id 冲突面）
  const cocHint = $("coc-setup-hint");
  if (cocHint) {
    cocHint.textContent = isCocTsf
      ? "调查员设定（CoC×TSF：CoC 玩法 + TSF 转变进度与阶段立绘，需生成立绘）"
      : "调查员设定（CoC 模式：数值由难度表决定，不生成立绘）";
  }
  const ap = document.querySelector(".anchor-preview");
  if (ap) ap.classList.toggle("hidden", isVn || isCoc);
  const pg = document.querySelector(".prot-grid");   // class，非 id
  if (pg) pg.style.display = (isVn || isCoc) ? "none" : "";
  const pe = $("prot-extra");
  if (pe) pe.style.display = (isVn || isCoc) ? "none" : "";
  const pt = $("prot-head-title");
  if (pt) pt.textContent = isVn
    ? "② 主角设定（普通故事的主角）"
    : (isCocTsf
      ? "② 调查员设定（CoC×TSF 模式：带转变进度与阶段立绘）"
      : (isCoc ? "② 调查员设定（CoC 模式不带头像管线）" : "② 主角设定（转变将从 TA 开始）"));
  // 1.7.33 最终变化样式面板：仅 TSF 模式显示
  const tgtPanel = $("tsf-target-panel");
  if (tgtPanel) tgtPanel.style.display = (isVn || isCoc) ? "none" : "";
  if (!silent) {
    toast(isVn
      ? "普通模式：心情 / 好感 / 性敏感 / 高潮度"
      : (isCocTsf
        ? "CoC×TSF：CoC 跑团 + TSF 转变进度/阶段立绘（第四个独立模式）"
        : (isCoc
          ? "CoC 跑团：DOOM / STA / REA / SAN、物品与商店、剧本预设（第三模式）"
          : "TSF 模式：性别转换数值演进（同化/体质/沉溺…）")), 2600);
  }
  renderStatEditor();
  // P18-A：CoC 的开局下拉（古神/调查员）按需懒加载一次
  if (isCoc) populateCocSelects();
  // 1.8.0：UI2 同步模式摘要 / 版本（未加载时静默跳过）
  if (window.UI2) UI2.onModeChanged();
}

/** P18-A（t51）：把 CoC 的可选古神/调查员填进下拉（只填一次）。 */
async function populateCocSelects() {
  const og = $("coc-old-god");
  const ch = $("coc-character");
  if (!og || !ch || og.dataset.filled) return;
  try {
    const cfgv = await api("/api/game/coc/config");
    (cfgv.old_gods || []).forEach((id) => {
      const o = document.createElement("option");
      o.value = id; o.textContent = id; og.appendChild(o);
    });
    (cfgv.characters || []).forEach((id) => {
      const o = document.createElement("option");
      o.value = id; o.textContent = id; ch.appendChild(o);
    });
    // 1.9.19：剧本预设（default 已在 HTML 里，其余按服务端清单填）
    S.cocPresets = Array.isArray(cfgv.scenarios) ? cfgv.scenarios : [];
    const sp = $("coc-scenario-preset");
    if (sp) {
      const seen = new Set(Array.from(sp.options).map((o) => o.value));
      S.cocPresets.forEach((sc) => {
        if (!sc || !sc.id || seen.has(sc.id)) return;
        const o = document.createElement("option");
        o.value = sc.id;
        o.textContent = (sc.title || sc.id)
          + (sc.era ? ` · ${sc.era}` : "");
        sp.appendChild(o);
      });
      sp.addEventListener("change", updateCocScenarioDesc);
    }
    updateCocScenarioDesc();
    og.dataset.filled = "1";
  } catch (e) {
    og.dataset.filled = "1";   // 失败不重试，保持默认值可用
  }
}

/* 1.9.19：剧本预设的说明文字（不含 Keeper-only 内容，只给题材/时长/人数）。 */
function updateCocScenarioDesc() {
  const sp = $("coc-scenario-preset");
  const box = $("coc-scenario-desc");
  if (!sp || !box) return;
  const id = sp.value || "default";
  const sc = (S.cocPresets || []).find((x) => x.id === id);
  if (!sc) {
    box.textContent = "选择剧本会替换地点、谜团、古神、事件卡与叙述基调；数值规则不变。";
    return;
  }
  const bits = [sc.summary, sc.tone, sc.players,
    (sc.locations ? `${sc.locations} 个地点` : ""),
    (sc.mystery_len ? `单章 ${sc.mystery_len} 次调查` : "")].filter(Boolean);
  box.textContent = bits.join(" · ") || "数值规则不变。";
}

/* 1.9.19：开局时把选中的剧本预设 id 带给服务端；自定义 JSON 会覆盖预设同名段。 */
function collectCocScenarioId() {
  const el = $("coc-scenario-preset");
  return el ? String(el.value || "default") : "default";
}
// —— UI 绑定段 bindUiPart06（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart06() {
  $("mode-btn-tsf").addEventListener("click", () => setSetupMode("tsf"));
  $("mode-btn-vn").addEventListener("click", () => setSetupMode("vn"));
  // P18-A（t51）：新增绑定写在既有绑定函数内（**不新增顶层零缩进绑定**，
  // 保持 check_frontend_ids.py --max-toplevel 138 的棘轮不上升）。
  // 用存在性判断，兼容未同步更新 index.html 的旧副本。
  const cocBtn = $("mode-btn-coc");
  if (cocBtn) cocBtn.addEventListener("click", () => setSetupMode("coc"));
  // C×T4（t56）：第四模式按钮（同样写在既有绑定函数内，不新增顶层零缩进绑定）
  const cocTsfBtn = $("mode-btn-coc-tsf");
  if (cocTsfBtn) cocTsfBtn.addEventListener("click", () => setSetupMode("coc_tsf"));
  // P18-A（t51）：CoC 存档按钮（写在既有绑定函数内，不新增顶层零缩进绑定）
  const cocSave = $("btn-coc-save-slot");
  if (cocSave) {
    cocSave.addEventListener("click", async () => {
      if (!S.sid) return;
      try {
        const r = await api(`/api/game/${S.sid}/slots`, {
          method: "POST",
          body: JSON.stringify({ name: $("game-title").textContent || "CoC 存档" }),
        });
        toast(`已存档：${r.slot_id || r.name || "OK"}`, 2600);
      } catch (e) {
        toast("存档失败：" + e.message);
      }
    });
  }
}

/* ---------- 状态栏自定义（开局前编辑） ---------- */

const DEFAULT_STATS = {
  tsf: [
    { key: "progress", name: "性别同化率", icon: "♀", start: 0 },
    { key: "physique", name: "体质特征", icon: "◈", start: 0 },
    { key: "acuity", name: "感官适应度", icon: "✦", start: 20 },
    { key: "genital", name: "性征转化度", icon: "◉", start: 0 },
    { key: "habit", name: "洗脑程度", icon: "⚯", start: 0 },
    { key: "immersion", name: "堕落度", icon: "◍", start: 0 },
  ],
  vn: [
    { key: "mood", name: "心情", icon: "♪", start: 45 },
    { key: "affection", name: "好感", icon: "❣", start: 20 },
    { key: "arousal", name: "性敏感", icon: "❀", start: 0 },
    { key: "climax", name: "高潮度", icon: "◉", start: 0 },
  ],
};

function cloneStats(list) { return (list || []).map((s) => ({ ...s })); }

function collectStatConfig() {
  const list = (S.statCfg && S.statCfg[S.mode]) || [];
  return list.filter((s) => (s.name || "").trim())
    .map((s) => ({ key: s.key || "", name: (s.name || "").trim(), start: s.start ?? 0 }));
}

function renderStatEditor() {
  const box = $("stat-editor-list");
  if (!box) return;
  if (!S.statCfg) S.statCfg = {};
  const mode = S.mode;
  if (!Array.isArray(S.statCfg[mode])) S.statCfg[mode] = cloneStats(DEFAULT_STATS[mode]);
  const list = S.statCfg[mode];
  box.innerHTML = "";
  list.forEach((st, i) => {
    const locked = mode === "tsf" && st.key === "progress";
    const row = document.createElement("div");
    row.className = "stat-row";
    row.innerHTML = `
      <span class="stat-ico">${escapeHtml(st.icon || "✦")}</span>
      <input class="text-input stat-name" placeholder="状态名" maxlength="10" value="${escapeHtml(st.name || "")}">
      <label class="stat-start-label">初始值
        <input class="text-input stat-start" type="number" min="0" max="100" value="${st.start ?? 0}">
      </label>
      <button class="btn mini ghost btn-stat-del" ${locked ? "disabled" : ""}
        title="${locked ? "阶段演化核心，不可删除" : "删除该状态"}">✕</button>`;
    row.querySelector(".stat-name").addEventListener("input", (e) => { st.name = e.target.value; });
    row.querySelector(".stat-start").addEventListener("change", (e) => {
      st.start = Math.max(0, Math.min(100, parseInt(e.target.value, 10) || 0));
      e.target.value = st.start;
    });
    const del = row.querySelector(".btn-stat-del");
    if (!locked) del.addEventListener("click", () => {
      S.statCfg[mode].splice(i, 1);
      renderStatEditor();
    });
    box.appendChild(row);
  });
}
// —— UI 绑定段 bindUiPart07（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart07() {

  $("btn-stat-add").addEventListener("click", () => {
    if (!S.statCfg || !Array.isArray(S.statCfg[S.mode])) renderStatEditor();
    S.statCfg[S.mode].push({ key: "", name: "", icon: "✦", start: 0 });
    renderStatEditor();
  });
  $("btn-stat-reset").addEventListener("click", () => {
    if (!S.statCfg) S.statCfg = {};
    S.statCfg[S.mode] = cloneStats(DEFAULT_STATS[S.mode]);
    renderStatEditor();
  });
}

/* ---------- 内容分级（游戏内，下一幕生效） ---------- */

function applyRatingUI(rating, r18, intensity, forced) {
  const sel = $("inp-rating-game");
  if (!sel) return;
  sel.value = rating || "all";
  const cb = $("inp-r18-game");
  cb.disabled = sel.value !== "18";
  cb.checked = !!r18 && sel.value === "18";
  const fc = $("cfg-forced");
  if (fc) {
    fc.disabled = sel.value !== "18" || !cb.checked;
    fc.checked = !!forced && sel.value === "18" && cb.checked;
  }
}
// —— UI 绑定段 bindUiPart08（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart08() {

  // 联动：切到 18+ 才允许勾选 R18（与开局面板行为一致）
  $("inp-rating-game").addEventListener("change", () => {
    const sel = $("inp-rating-game");
    const cb = $("inp-r18-game");
    cb.disabled = sel.value !== "18";
    if (sel.value !== "18") cb.checked = false;
    const fc = $("cfg-forced");
    fc.disabled = sel.value !== "18" || !cb.checked;
    if (sel.value !== "18" || !cb.checked) fc.checked = false;
    $("inp-r18-game").dispatchEvent(new Event("change"));
  });
  $("inp-r18-game").addEventListener("change", () => {
    const fc = $("cfg-forced");
    fc.disabled = !$("inp-r18-game").checked;
    if (!$("inp-r18-game").checked) fc.checked = false;
  });
}

/* ---------- 设置弹窗 ---------- */

/* F1-U1（t65）：画风预设的联动提示。
   数据来源全部是后端（`GET /api/config` 的 style_presets/style_preset/style_custom）：
   **哪个预设要提示开像素开关，由该预设的 `hint.pixel_art` 决定**，前端不硬编码 id。
   ⚠️ 这里**只显示提示**：绝不写 `image.pixel_art`（那是另一个开关，t61 明确"只提示不自动开"）。 */
function updateStyleHint() {
  const box = $("cfg-style-hint");
  const sel = $("cfg-style-preset");
  if (!box || !sel) return;
  const pid = sel.value || "none";
  const list = (S.stylePresets && Array.isArray(S.stylePresets)) ? S.stylePresets : [];
  const cur = list.find((p) => p && p.id === pid) || null;
  const parts = [];
  if (pid !== "none") {
    parts.push("已选画风会在「下一幕 / 重新生成立绘」时生效；已有立绘不会自动改变。");
  }
  if (cur && cur.hint && cur.hint.pixel_art) {
    parts.push("像素风提示：本预设只改提示词；真正的块状像素化由下面的「像素化立绘」开关"
      + "（image.pixel_art）控制，两者一起开才有完整像素观感。");
  }
  box.textContent = parts.join(" ");
  box.classList.toggle("hidden", parts.length === 0);
  updatePixelHint();
}

/* t68：**像素化开关**（image.pixel_art）的联动提示。
   它不用 style_presets 的 hint —— 那个 hint 是"这个预设建议开像素"，与本开关自身的状态
   是两回事。这里只在"选了像素风预设但没开像素化"时提醒，避免用户漏开半边。 */
function updatePixelHint() {
  const box = $("cfg-pixel-hint");
  const cb = $("cfg-pixel-art");
  const sel = $("cfg-style-preset");
  if (!box || !cb) return;
  const on = !!cb.checked;
  const pid = (sel && sel.value) || "none";
  const parts = [];
  if (on) {
    parts.push("已开启：生成图会被量化成硬色块，并由 image-rendering: pixelated 放大成真像素。");
  }
  if (!on && pid === "pixel_art") {
    parts.push("你选了「像素风」预设但没开这个开关 —— 那只改了提示词，图仍是平滑的。"
      + "要真正的像素块请勾选它。");
  }
  box.textContent = parts.join(" ");
  box.classList.toggle("hidden", parts.length === 0);
}

function openSettings() {
  // 先开弹窗，再异步填值：SD/Comfy 探测再慢也不会让按钮「点下去几秒没反应」。
  $("modal-settings").classList.remove("hidden");
  $("settings-hint").textContent = "读取配置…";
  // 配置还没填完时禁止保存，避免用户手快用空字段覆盖 config.json。
  const saveBtn = $("btn-settings-save");
  if (saveBtn) saveBtn.disabled = true;
  api("/api/config").then(async ({ config, model_detected, llm_mock,
                                  style_presets, style_preset, style_custom,
                                  style_custom_max }) => {
    $("cfg-llm-url").value = config.llm.base_url || "";
    // Key 不回显（掩码会误导"已配置"）：留空 + 状态提示
    $("cfg-llm-key").value = "";
    const st = $("cfg-llm-status");
    st.textContent = llm_mock
      ? "⚠ 当前为演示模式：未配置有效的对话 API（剧情使用内置内容）"
      : "✓ 对话 API 已配置（真实剧情）";
    $("cfg-llm-model").value = config.llm.model || "";
    $("cfg-img-provider").value = (config.image.provider || "openai");
    $("cfg-img-url").value = config.image.base_url || "";
    $("cfg-img-key").value = config.image.api_key || "";
    $("cfg-img-model").value = config.image.model || "";
    if ($("cfg-img-comfy-ckpt")) $("cfg-img-comfy-ckpt").value = config.image.comfy_checkpoint || "";
    toggleComfyRow(config.image.provider || "openai");
    $("cfg-img-size").value = config.image.size || "";
    if ($("cfg-img-hires")) $("cfg-img-hires").checked = !!config.image.hi_res;
    $("cfg-img-neg").value = config.image.negative_prompt || "";
    $("cfg-cutout-mode").value = config.image.cutout_mode || "ai";
    $("cfg-portrait-update").value = (config.game || {}).portrait_update || "stage";
    $("cfg-style-mode").value = (config.game || {}).style_mode || "default";
    // F1-U1（t65）：画风预设下拉 —— 选项/当前值/上限**全部来自后端**（前端零硬编码）
    const styleSel = $("cfg-style-preset");
    if (styleSel) {
      S.stylePresets = Array.isArray(style_presets) ? style_presets : [];
      styleSel.innerHTML = "";
      S.stylePresets.forEach((p) => {
        if (!p || !p.id) return;
        const o = document.createElement("option");
        o.value = String(p.id);
        o.textContent = String(p.name || p.id);
        if (p.hint && p.hint.pixel_art) o.dataset.pixelHint = "1";
        styleSel.appendChild(o);
      });
      if (!S.stylePresets.length) {     // 后端异常时也要可用：至少保留 none
        const o = document.createElement("option");
        o.value = "none"; o.textContent = "无风格（默认，与旧行为一致）";
        styleSel.appendChild(o);
      }
      styleSel.value = String(style_preset || "none");
      if (!styleSel.value) styleSel.value = "none";
    }
    const customEl = $("cfg-style-custom");
    if (customEl) {
      customEl.value = String(style_custom || "");
      if (style_custom_max) customEl.maxLength = Number(style_custom_max) || 300;
    }
    updateStyleHint();
    if ($("cfg-inject-outline")) $("cfg-inject-outline").checked = (config.game || {}).inject_outline !== false;
    $("cfg-vram-mode").value = config.image.vram_mode || "mid";
    if ($("cfg-wai-adapt")) $("cfg-wai-adapt").checked = config.image.wai_adapt !== false;
    // t68：像素化开关（image.pixel_art，默认关 ⇒ 严格 !!）
    if ($("cfg-pixel-art")) $("cfg-pixel-art").checked = !!config.image.pixel_art;
    if ($("cfg-tsf-explicit")) $("cfg-tsf-explicit").checked = !!config.image.tsf_explicit;
    if ($("cfg-tsf-plan-llm")) $("cfg-tsf-plan-llm").checked = config.image.tsf_plan_llm !== false;
    if ($("cfg-stage-style")) $("cfg-stage-style").value = config.image.stage_style || "auto";
    if ($("cfg-portrait-quality")) $("cfg-portrait-quality").value = config.image.portrait_quality || "mid";
    if ($("cfg-bg-quality")) $("cfg-bg-quality").value = config.image.background_quality || "mid";
    if ($("cfg-cg-quality")) $("cfg-cg-quality").value = config.image.cg_quality || "mid";
    // 1.8.0 绘画线路优化：成品缓存 / 节能 / 缓存上限
    if ($("cfg-gen-cache")) $("cfg-gen-cache").checked = config.image.gen_cache !== false;
    if ($("cfg-eco-mode")) $("cfg-eco-mode").checked = !!config.image.eco_mode;
    if ($("cfg-gen-cache-mb")) $("cfg-gen-cache-mb").value = config.image.gen_cache_mb || 384;
    const mi = $("cfg-model-info");
    if (model_detected) {
      mi.innerHTML = "🧠 检测到模型：" + escapeHtml(
        (model_detected.checkpoint || "").split(" [")[0])
        + " → 适配预设：" + escapeHtml(model_detected.preset_name)
        + "（" + model_detected.steps + " 步 / CFG " + model_detected.cfg
        + " / " + escapeHtml(model_detected.sampler)
        + (model_detected.hr_fix ? " / HR修复×" + model_detected.hr_scale : "")
        + "）";
    } else {
      mi.textContent = "（无法检测 WebUI 模型，使用默认参数）";
    }
    if (S.sid) {
      try {
        const st = await api(`/api/game/${S.sid}`);
        applyRatingUI(st.content_rating, st.r18_enabled, st.adult_intensity,
                      st.allow_forced);
        if ($("cfg-adult-intensity")) $("cfg-adult-intensity").value = st.adult_intensity || "成熟";
      } catch { /* 无会话则保持默认 */ }
    }
    $("settings-hint").textContent = "";
    if (saveBtn) saveBtn.disabled = false;
    loadInjects();
  }).catch((e) => {
    $("settings-hint").textContent = "读取配置失败：" + e.message;
    // 读不到配置就保持禁用保存：避免用旧/空字段覆盖 config.json
    if (saveBtn) saveBtn.disabled = true;
    toast("读取配置失败：" + e.message);
  });
}
// —— UI 绑定段 bindUiPart09（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart09() {

  $("btn-open-settings").addEventListener("click", openSettings);
  $("btn-settings-game").addEventListener("click", openSettings);
  $("btn-settings-close").addEventListener("click", () => $("modal-settings").classList.add("hidden"));
  $("btn-settings-exit").addEventListener("click", () => $("modal-settings").classList.add("hidden"));
}

/* ---------- ComfyUI 连接助手（设置页） ---------- */
let comfyDesktopApps = [];

function toggleComfyRow(provider) {
  const row = $("cfg-img-comfy-row");
  if (row) row.classList.toggle("hidden", provider !== "comfyui");
}
// —— UI 绑定段 bindUiPart10（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart10() {

  if ($("cfg-img-provider")) {
    $("cfg-img-provider").addEventListener("change", () =>
      toggleComfyRow($("cfg-img-provider").value));
  }
  // F1-U1（t65）：画风下拉的联动提示（只提示，不改任何开关）
  if ($("cfg-style-preset")) {
    $("cfg-style-preset").addEventListener("change", updateStyleHint);
  }
  // t68：像素化开关的联动提示（用户手动勾选/取消时刷新文案）
  if ($("cfg-pixel-art")) {
    $("cfg-pixel-art").addEventListener("change", updatePixelHint);
  }

  $("btn-comfy-scan").addEventListener("click", async () => {
    const st = $("comfy-status");
    st.textContent = "🔍 探测本机 ComfyUI 服务与桌面端…";
    try {
      const r = await api("/api/image/comfy/scan", { method: "POST" });
      comfyDesktopApps = r.desktop_apps || [];
      const svc = r.services || [];
      $("btn-comfy-launch").classList.toggle("hidden", !comfyDesktopApps.length);
      if (!svc.length) {
        st.textContent = comfyDesktopApps.length
          ? "未发现在线服务；已检测到桌面端程序，点「🚀 启动桌面端程序」后重新探测"
          : "未发现本机 ComfyUI 服务（请确认桌面端已启动，默认端口 8188）";
        return;
      }
      $("btn-comfy-launch").classList.add("hidden");
      const pick = svc[0];
      $("cfg-img-url").value = pick.url;
      let chk = { checkpoints: [], version: pick.version || "" };
      try {
        chk = await api("/api/image/comfy/check", {
          method: "POST", body: JSON.stringify({ base_url: pick.url }),
        });
      } catch (e2) { /* 读不到模型列表也不阻塞 */ }
      const sel = $("cfg-img-comfy-ckpt");
      const cur = sel.value;
      sel.innerHTML = '<option value="">（自动-第一个可用模型）</option>';
      (chk.checkpoints || []).forEach((n) => {
        const o = document.createElement("option");
        o.value = n;
        o.textContent = n;
        sel.appendChild(o);
      });
      if (cur) sel.value = cur;
      st.textContent = `✅ 已连接 ${pick.url} · ComfyUI ${chk.version || "?"}`
        + ` · 可用模型 ${(chk.checkpoints || []).length} 个 · 记得点「保存」`;
    } catch (e) {
      st.textContent = "连接失败：" + e.message;
    }
  });

  $("btn-comfy-launch").addEventListener("click", async () => {
    if (!comfyDesktopApps.length) return;
    try {
      await api("/api/image/comfy/launch", {
        method: "POST", body: JSON.stringify({ path: comfyDesktopApps[0] }),
      });
      $("comfy-status").textContent = "已启动桌面端程序，稍候几秒后再点「🔌 自动探测并连接桌面端」";
    } catch (e) {
      $("comfy-status").textContent = "启动失败：" + e.message;
    }
  });

  $("btn-settings-save").addEventListener("click", async () => {
    const config = {
      llm: {
        base_url: $("cfg-llm-url").value.trim(),
        api_key: $("cfg-llm-key").value.trim(),
        model: $("cfg-llm-model").value.trim(),
      },
      image: {
        provider: $("cfg-img-provider").value,
        edge_clean: true,
        edge_shrink: 1,
        sync_webui: true,
        auto_sdxl_styles: true,
        base_url: $("cfg-img-url").value.trim(),
        api_key: $("cfg-img-key").value.trim(),
        model: $("cfg-img-model").value.trim(),
        comfy_checkpoint: $("cfg-img-comfy-ckpt").value.trim(),
        size: $("cfg-img-size").value.trim(),
        hi_res: $("cfg-img-hires") ? $("cfg-img-hires").checked : false,
        negative_prompt: $("cfg-img-neg").value.trim(),
        cutout_mode: $("cfg-cutout-mode").value,
        vram_mode: $("cfg-vram-mode").value,
        wai_adapt: $("cfg-wai-adapt") ? $("cfg-wai-adapt").checked : true,
        // t68：像素化立绘（image.pixel_art，默认关）
        pixel_art: $("cfg-pixel-art") ? $("cfg-pixel-art").checked : false,
        tsf_explicit: $("cfg-tsf-explicit") ? $("cfg-tsf-explicit").checked : false,
        tsf_plan_llm: $("cfg-tsf-plan-llm") ? $("cfg-tsf-plan-llm").checked : true,
        stage_style: $("cfg-stage-style") ? $("cfg-stage-style").value : "auto",
        portrait_quality: $("cfg-portrait-quality").value,
        background_quality: $("cfg-bg-quality").value,
        cg_quality: $("cfg-cg-quality").value,
        pink_rim: true,
        // 1.8.0 线路优化（缺失控件时保留默认，不覆盖后端既有值）
        gen_cache: $("cfg-gen-cache") ? $("cfg-gen-cache").checked : true,
        eco_mode: $("cfg-eco-mode") ? $("cfg-eco-mode").checked : false,
        gen_cache_mb: $("cfg-gen-cache-mb")
          ? Math.max(64, Math.min(4096, parseInt($("cfg-gen-cache-mb").value, 10) || 384))
          : 384,
      },
      game: {
        portrait_update: $("cfg-portrait-update").value,
        style_mode: $("cfg-style-mode").value,
        inject_outline: $("cfg-inject-outline").checked,
      },
    };
    // F1-U1（t65）：画风两项（仅当控件存在时才随包发出 —— 兼容未同步更新 index.html 的旧副本）
    if ($("cfg-style-preset")) config.image.style_preset = $("cfg-style-preset").value;
    if ($("cfg-style-custom")) config.image.style_custom = $("cfg-style-custom").value.trim();
    try {
      const saved = await api("/api/config", { method: "POST", body: JSON.stringify({ config }) });
      // 后端会把 style_custom 清洗/截断后回传：写回控件，避免"界面显示 300+ 字但其实只存了 300"
      if (saved && $("cfg-style-custom") && typeof saved.style_custom === "string") {
        $("cfg-style-custom").value = saved.style_custom;
      }
      if (S.sid && $("inp-rating-game")) {
        try {
          await api(`/api/game/${S.sid}/policy`, {
            method: "POST",
            body: JSON.stringify({
              content_rating: $("inp-rating-game").value,
              r18_enabled: $("inp-r18-game").checked,
              adult_intensity: $("cfg-adult-intensity").value,
              allow_forced: $("cfg-forced").checked,
            }),
          });
          $("settings-hint").textContent = "✓ 已保存，分级政策下一幕生效";
        } catch (e) {
          $("settings-hint").textContent = "配置保存成功，分级保存失败：" + e.message;
        }
      } else {
        $("settings-hint").textContent = "✓ 已保存，立即生效";
      }
      refreshMockBanner();
    } catch (e) {
      toast("保存失败：" + e.message);
    }
  });
}

/* ---------- API 前置注入 ---------- */

const injectState = { items: [] };

async function loadInjects() {
  try {
    const d = await api("/api/injects");
    injectState.items = d.injects || [];
    renderInjects();
  } catch { injectState.items = []; renderInjects(); }
}

function renderInjects() {
  const box = $("inject-list");
  if (!box) return;
  box.innerHTML = "";
  if (!injectState.items.length) {
    box.innerHTML = '<p class="hint-line">暂无注入条目，添加后勾选启用</p>';
    return;
  }
  injectState.items.forEach((it, i) => {
    const row = document.createElement("div");
    row.className = "directive-row" + (it.enabled ? " on" : "");
    row.innerHTML = `
      <input type="checkbox" ${it.enabled ? "checked" : ""} title="启用（保存后生效）">
      <textarea class="text-input inject-text" rows="2" maxlength="500">${escapeHtml(it.text)}</textarea>
      <button class="btn mini ghost inject-del" title="删除">✕</button>`;
    const cb = row.querySelector("input");
    const ta = row.querySelector("textarea");
    cb.addEventListener("change", () => {
      it.enabled = cb.checked;
      row.classList.toggle("on", cb.checked);
    });
    ta.addEventListener("input", () => { it.text = ta.value; });
    row.querySelector(".inject-del").addEventListener("click", (e) => {
      e.preventDefault();
      injectState.items.splice(i, 1);
      renderInjects();
    });
    box.appendChild(row);
  });
}
// —— UI 绑定段 bindUiPart11（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart11() {

  $("btn-add-inject").addEventListener("click", () => {
    const text = $("inject-new").value.trim();
    if (!text) { toast("请输入注入文本"); return; }
    injectState.items.push({ id: "", name: "注入", description: "", text, enabled: true });
    $("inject-new").value = "";
    renderInjects();
  });
}

/* ---------- 从 GitHub 更新：检查 / 下载整合包 ---------- */

let updateTimer = null;

function renderUpdateStatus(st) {
  const hint = $("update-hint");
  const wrap = $("update-bar-wrap");
  const bar = $("update-bar");
  if (!st) return;
  if (st.progress > 0 && st.status !== "done") {
    wrap.classList.remove("hidden");
    bar.style.width = st.progress + "%";
  } else if (st.status === "done") {
    wrap.classList.remove("hidden");
    bar.style.width = "100%";
  } else {
    wrap.classList.add("hidden");
    bar.style.width = "0%";
  }
  if (hint) hint.textContent = st.msg || "";
}
// —— UI 绑定段 bindUiPart12（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart12() {

  $("btn-update-check").addEventListener("click", async () => {
    const hint = $("update-hint");
    hint.textContent = "正在查询 GitHub…";
    $("btn-update-download").classList.add("hidden");
    $("update-notes").classList.add("hidden");
    try {
      const d = await api("/api/update/check");
      if (d.error) { hint.textContent = d.error; return; }
      const cur = d.current ? `v${d.current}` : "未知";
      if (d.has_update) {
        hint.textContent = `发现新版本 ${cur} → v${d.latest}`
          + (d.proxy ? "（下载将走系统代理）" : "");
        $("btn-update-download").classList.remove("hidden");
      } else {
        hint.textContent = `已是最新版本（${cur}）`;
      }
      if (d.notes) {
        const box = $("update-notes");
        box.textContent = d.notes;
        box.classList.remove("hidden");
      }
    } catch (e) {
      hint.textContent = "检查失败：" + e.message;
    }
  });

  $("btn-update-download").addEventListener("click", async () => {
    const hint = $("update-hint");
    $("btn-update-download").classList.add("hidden");
    try {
      await api("/api/update/download", { method: "POST" });
    } catch (e) {
      hint.textContent = "启动下载失败：" + e.message;
      return;
    }
    // t38：收敛到单一调度器（原来这里是独立 setInterval，靠 clearInterval 自清）
    Sched.define("updateStatus", 1200, async () => {
      try {
        const st = await api("/api/update/status");
        renderUpdateStatus(st);
        if (st.status === "done") {
          Sched.stop("updateStatus");
          toast(st.msg, 8000);
        } else if (st.status === "error") {
          Sched.stop("updateStatus");
        }
      } catch (e) {
        Sched.stop("updateStatus");
        hint.textContent = "进度查询中断：" + e.message;
      }
    });
    Sched.stop("updateStatus");
    Sched.start("updateStatus");
  });

  $("btn-injects-save").addEventListener("click", async () => {
    try {
      const current = (await api("/api/injects")).injects || [];
      const keepIds = new Set(injectState.items.map((x) => x.id).filter(Boolean));
      for (const old of current) {
        if (old.id && !keepIds.has(old.id) && !String(old.id).startsWith("builtin_")) {
          await api(`/api/injects/${old.id}`, { method: "DELETE" });
        }
      }
      for (const it of injectState.items) {
        await api("/api/injects", { method: "POST", body: JSON.stringify(it) });
      }
      await loadInjects();
      $("injects-hint").textContent = "✓ 已保存：启用中的注入将在下一轮剧情生效";
    } catch (e) {
      toast("注入保存失败：" + e.message);
    }
  });
}

async function refreshMockBanner() {
  try {
    const st = await api("/api/config");
    const mock = !st.llm_ready || !st.image_ready;
    $("mock-banner").classList.toggle("hidden", !mock);
  } catch { /* 忽略 */ }
}

/* ---------- 进入游戏 ---------- */

function enterGame(state, { instant }) {
  // t38 · ② 切局/读档的统一清理点。
  // 所有"采纳一份新对局状态"的路径都经过 enterGame ——新开局、scene-set（换场景/
  // 重绘背景）、slots/*/load（读档）—— 所以清理放在这里**一次覆盖全部**，而不是
  // 在每个调用点各补一行（那种写法迟早会漏一个）。
  // 收敛前 ffTimer（快进）完全没有切局清理：切局后它会在新局面上继续 nextLine()。
  // 停掉之后由各路径原有的 startPolling() 按需重新启动（读档后页面照常刷新）。
  Sched.stopAll();
  S.sid = state.sid;
  // 读档/切局必须把活动 sid 一起落 localStorage。旧版游戏内「读档」只
  // 调 playTurn，S.sid 仍指向旧对局，下一幕会写到旧档（表现=存档乱跳、
  // 读档后继续推剧情又回到另一局）。这里统一覆盖所有进入对局的路径。
  try {
    if (state.sid) localStorage.setItem("galgame_sid", state.sid);
  } catch { /* localStorage 不可用时忽略 */ }
  S.title = state.title;
  S.mode = state.mode || "tsf";
  S.mock = state.mock || { llm: true, image: true };
  S.assets = state.assets;
  S.bgMap = state.bg_map || {};
  S.protName = state.protagonist;
  S.npcs = state.npcs || [];
  S.present = state.present || state.npcs || [];
  S.outfits = state.outfits || {};
  S.r18 = !!(state.r18_enabled && state.content_rating === "18");
  S.forced = !!state.allow_forced && S.r18;
  S.imgVersion = (S.imgVersion || 0) + 1;
  S.tsf = state.tsf || null;
  S.vn = state.vn || null;
  S.coc = state.coc || null;
  S.cocTsf = state.coc_tsf || null;      // C×T4（t56）：第四模式的 TSF 视图（服务端 t54 接口取值）
  S.aiDirective = state.ai_directive || "";   // 1.9.20
  S.charStats = state.char_stats || null;
  S.charPrev = {};
  S.tsfPrev = null;
  S.statWho = state.protagonist || null;   // 新局默认看主角
  if (state.stat_config) {
    if (!S.statCfg) S.statCfg = {};
    S.statCfg.session = state.stat_config;
  }
  $("game-title").textContent = state.title;
  $("mock-badge").classList.toggle("hidden", !(S.mock.llm && S.mock.image));
  $("setup-screen").classList.remove("active");
  $("game-screen").classList.add("active");
  // 按模式切换右侧状态面板：TSF 数值面板 / 普通模式心情面板 / CoC 跑团面板（P18-A）
  // C×T4（t56）：coc_tsf 复用 CoC 面板（面板内多一段 TSF 进度）
  const isVn = S.mode === "vn";
  const isCoc = S.mode === "coc" || S.mode === "coc_tsf";
  $("tsf-panel").classList.toggle("hidden", isVn || isCoc);
  $("vn-panel").classList.toggle("hidden", !isVn);
  const cocPanel = $("coc-panel");
  if (cocPanel) cocPanel.classList.toggle("hidden", !isCoc);
  $("tsf-tab").classList.add("hidden");
  $("vn-tab").classList.add("hidden");
  buildSprites();
  renderStatsPanels();
  if (state.llm_degraded) {
    toast("对话 API 不可用，已切换演示剧情（可在设置中修正 Key）", 5000);
  }
  playTurn(state, { instant });
  startPolling();
  // 1.8.0：进入对局后刷新六段轨道 / 绘画状态 / 版本
  if (window.UI2) UI2.onEnterGame();
}

/* 站位表：按同屏角色数分配横向站位（主角第一位，NPC 依次）。
   5 人时内缩避免两端立绘被视口边缘裁切；crowd 模式见 buildSprites。 */
const STAGE_POSITIONS = {
  1: [50],
  2: [30, 70],
  3: [20, 50, 80],
  4: [14, 38, 62, 86],
  5: [12, 31, 50, 69, 88],
};

function buildSprites(presentList) {
  const layer = $("sprite-layer");
  layer.innerHTML = "";
  const present = presentList || S.present || S.npcs;
  const cast = [{ name: S.protName, kind: "prot" },
                ...present.filter(n => S.npcs.includes(n))
                  .map((n) => ({ name: n, kind: "npc" }))];
  const positions = STAGE_POSITIONS[cast.length] || STAGE_POSITIONS[4];
  const crowd = cast.length >= 5;
    cast.forEach((c, i) => {
    const div = document.createElement("div");
    div.className = "sprite" + (crowd ? " crowd" : "");
    div.dataset.name = c.name;
    div.dataset.kind = c.kind;
    div.style.left = `${positions[Math.min(i, positions.length - 1)]}%`;
    div.innerHTML = `
      <div class="sprite-placeholder">${escapeHtml(c.name)}<br>${c.kind === "prot" ? "阶段立绘生成中…" : "立绘生成中…"}</div>
      <img alt="${escapeHtml(c.name)}">
      <div class="sprite-badge hidden"></div>`;
    // 悬停出现「重新生成」徽章：不满意/畸形时只重画该角色
    const badge = document.createElement("button");
    badge.className = "sprite-regen";
    badge.title = `重新生成 ${c.name} 的立绘`;
    badge.textContent = "🔄";
    badge.addEventListener("click", (e) => {
      e.stopPropagation();
      regenerateSprite(c.kind === "prot" ? "protagonist" : c.name);
    });
    div.appendChild(badge);
    layer.appendChild(div);
  });
}

/* ---------- 重画立绘（弹窗：目标 + 需求描述） ---------- */

const REGEN_SUGGESTS = [
  "更女性化的面容", "更精致的头部细节", "去掉眼镜", "要开心的表情",
  "头发更长", "更年轻的样子", "眼神更有神", "更贴近参考气质",
];

let regenTarget = "protagonist";
let regenStartNoNote = false;

function openRegenModal(target, startNoNote) {
  regenTarget = target || "protagonist";
  regenStartNoNote = !!startNoNote;
  const sel = $("regen-char");
  sel.innerHTML = "";
  const add = (val, txt) => {
    const o = document.createElement("option");
    o.value = val; o.textContent = txt;
    sel.appendChild(o);
    return o;
  };
  add("protagonist", S.protName + "（主角）");
  add("all", "全部角色");
  (S.npcs || []).forEach((n) => add(n, n));
  sel.value = regenTarget === "all" ? "all"
    : (S.npcs || []).includes(regenTarget) ? regenTarget
    : regenTarget === "protagonist" ? "protagonist" : (S.npcs || [])[0] || "protagonist";
  if (regenTarget !== "all" && !(S.npcs || []).includes(regenTarget)
      && regenTarget !== "protagonist") regenTarget = sel.value;
  $("regen-note").value = "";
  $("regen-hint").textContent = regenStartNoNote ? "" : "重画使用新缓存，旧立绘将被替换";
  if ($("regen-use-initial")) $("regen-use-initial").checked = false;
  renderRegenChips();
  updateRegenInitialRow();
  $("modal-regen").classList.remove("hidden");
}

/* 「参考主角同化率」选项：仅 TSF 模式 & 目标是主角时显示 */
function updateRegenInitialRow() {
  const row = $("regen-initial-row");
  if (!row) return;
  const show = S.mode === "tsf" && $("regen-char").value === "protagonist";
  row.classList.toggle("hidden", !show);
}
// —— UI 绑定段 bindUiPart13（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart13() {
  if ($("regen-char")) {
    $("regen-char").addEventListener("change", updateRegenInitialRow);
  }

}
function renderRegenChips() {
  const box = $("regen-chips");
  box.innerHTML = "";
  REGEN_SUGGESTS.forEach((t) => {
    const b = document.createElement("button");
    b.className = "btn ghost action-btn";
    b.textContent = t;
    b.addEventListener("click", () => {
      const ta = $("regen-note");
      ta.value = ta.value ? ta.value + "，" + t : t;
    });
    box.appendChild(b);
  });
}

function confirmRegen() {
  const target = $("regen-char").value;
  const note = $("regen-note").value.trim();
  const useInitial = $("regen-use-initial") && $("regen-use-initial").checked;
  $("modal-regen").classList.add("hidden");
  doRegenerate(target, note, regenStartNoNote, useInitial);
}

async function doRegenerate(target, note, silent, useInitial) {
  if (!S.sid) return;
  try {
    const r = await api(`/api/game/${S.sid}/regenerate`, {
      method: "POST",
      body: JSON.stringify({ target, note, use_initial: !!useInitial }),
    });
    toast(`正在重新生成：${r.regenerating}${note ? "（已按需求）" : ""}`
      + (useInitial ? "（参考初始立绘·按同化率）" : ""), 3000);
    S.imgVersion = (S.imgVersion || 0) + 1;
    startPolling();
  } catch (e) {
    toast("重新生成失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart14（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart14() {

  $("btn-regen-go").addEventListener("click", confirmRegen);
  $("btn-regen-cancel").addEventListener("click", () =>
    $("modal-regen").classList.add("hidden"));
}

function regenerateSprite(target) {
  if (!S.sid) return;
  openRegenModal(target);
}
// —— UI 绑定段 bindUiPart15（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart15() {
  $("btn-vn-regen-prot").addEventListener("click", () => openRegenModal("protagonist"));
  $("btn-vn-regen").addEventListener("click", () => openRegenModal("all"));
}

/* ---------- 状态面板：每个出场角色一套数值，可切换查看 ---------- */

function charList() {
  return (S.charStats && S.charStats.characters) || [];
}

// 各角色当前数值快照，用于下一幕计算 ▲▼
function charSnapshot() {
  const snap = {};
  charList().forEach((c) => {
    snap[c.name] = Object.fromEntries(c.states.map((s) => [s.key, s.value]));
  });
  return snap;
}

// 当前选中的角色；名字失效（换局/主角改名）时回落到主角
function selectedChar() {
  const list = charList();
  if (!list.length) return null;
  let c = list.find((x) => x.name === S.statWho);
  if (!c) {
    c = list.find((x) => x.is_protagonist) || list[0];
    S.statWho = c.name;
  }
  return c;
}

function renderWhoChips(containerId) {
  const box = $(containerId);
  if (!box) return;
  const list = charList();
  box.innerHTML = "";
  if (list.length < 2) {          // 只有主角时不显示切换条
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  list.forEach((c) => {
    const b = document.createElement("button");
    b.className = "who-chip" + (c.name === S.statWho ? " on" : "");
    b.textContent = (c.is_protagonist ? "★ " : "") + c.name;
    b.title = c.is_protagonist ? "主角（另有转变数值）" : "该角色自己的数值";
    b.addEventListener("click", () => {
      S.statWho = c.name;
      renderStatsPanels();
    });
    box.appendChild(b);
  });
}

// 渲染一组数值行（沿用 tsf-row 样式）；prev 为上一幕的值，用于 ▲▼ 标注
function renderStatRows(box, states, prev, append) {
  if (!box) return;
  if (!append) box.innerHTML = "";
  (states || []).forEach((s) => {
    const before = (prev || {})[s.key];
    const delta = before === undefined ? 0 : s.value - before;
    const row = document.createElement("div");
    row.className = "tsf-row";
    row.innerHTML = `
      <div class="tsf-row-label"><span class="tsf-ico">${escapeHtml(s.icon)}</span>${escapeHtml(s.name)}
        <span class="tsf-delta ${delta > 0 ? "up" : delta < 0 ? "down" : ""}">
          ${delta > 0 ? "▲" + delta : delta < 0 ? "▼" + Math.abs(delta) : ""}</span>
        <span class="tsf-val">${s.value}</span>
      </div>
      <div class="tsf-bar"><div class="tsf-bar-fill" style="width:${s.value}%"></div></div>`;
    if (delta !== 0) row.classList.add("tsf-changed");
    row.title = s.hint;
    box.appendChild(row);
  });
}

function renderBond(boxId, who) {
  const box = $(boxId);
  if (!box) return;
  if (!who || !who.bond) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  const b = who.bond;
  const note = who.emotion_note && who.emotion_note.text
    ? `<div class="bond-note" title="最近一次情感变化">💭 ${escapeHtml(who.emotion_note.text)}</div>`
    : "";
  const next = (b.next_at === null || b.next_at === undefined)
    ? "已达最高阶段"
    : `距下一阶段 ${Math.max(0, b.next_at - b.score)} 点好感`;
  box.classList.remove("hidden");
  box.innerHTML = `
    <div class="bond-head">
      <span class="bond-stage">❣ 关系 · ${escapeHtml(b.label)}</span>
      <span class="bond-hint">${escapeHtml(b.hint)}</span>
    </div>
    <div class="bond-meta">好感 ${b.score}/100 · ${escapeHtml(next)}</div>
    ${note}`;
}

function renderStatsPanels() {
  if (S.mode === "vn") renderVnPanel();
  else if (S.mode === "coc" || S.mode === "coc_tsf") renderCocPanel();   // C×T4（t56）
  else renderTsfPanel();
}

/* ---------- CoC 面板（DOOM 仪表 / 双血条 / 五项属性 / 标签计数）—— P18-A（t51） ----------
   数据全部来自 /api/game/{sid} 的 `coc` 字段（服务端 app/coc/session.py 的 panel）。
   注意：这里**只渲染数值 HUD**；检定属性名在结算前不展示（信息遮蔽）。 */

function renderCocPanel() {
  const c = S.coc;
  if (!c) return;
  const doomBox = $("coc-doom");
  if (doomBox) {
    const pct = Math.max(0, Math.min(100, Number(c.doom) || 0));
    doomBox.innerHTML =
      `<div class="tsf-bar"><div class="tsf-bar-fill" style="width:${pct}%"></div></div>` +
      `<div class="model-info">${c.doom_line || ""}</div>`;
  }
  const bars = $("coc-bars");
  if (bars) {
    bars.innerHTML = "";
    const rows = [
      { name: "体力 STA", value: c.sta, max: c.sta_max },
      { name: "意志 REA", value: c.rea, max: c.rea_max },
    ];
    rows.forEach((r) => {
      const max = Math.max(1, Number(r.max) || 1);
      const pct = Math.max(0, Math.min(100, (Number(r.value) / max) * 100));
      const row = document.createElement("div");
      row.className = "tsf-row";
      row.innerHTML =
        `<div class="tsf-row-head"><span>${r.name}</span><b>${r.value} / ${r.max}</b></div>` +
        `<div class="tsf-bar"><div class="tsf-bar-fill" style="width:${pct}%"></div></div>`;
      bars.appendChild(row);
    });
  }
  renderCocSan(c);
  const stats = $("coc-stats");
  if (stats && c.stats) {
    // t72：原来只输出一行裸值（`属性：STR 5 DEX 5 …`），夹在 DOOM 与标签之间几乎看不见，
    // 玩家会以为"这模式没有属性"。改成**带中文名的条目**（自身角色卡不属信息遮蔽 ——
    // 遮蔽②指的是"不预告下次检定考哪个属性"，见 COC-DESIGN §2.10；禁止属性名的是
    // **LLM 的 hint 文本**，§4.3）。
    const NAMES = { STR: "力量", DEX: "敏捷", PER: "感知", KNW: "知识", CHA: "魅力" };
    stats.innerHTML = `<div class="coc-stat-head">属性</div>` + Object.keys(c.stats)
      .map((k) => {
        const v = c.stats[k];
        const nm = NAMES[k] || k;
        return `<span class="coc-stat"><b>${escapeHtml(nm)}</b>`
          + `<i>${escapeHtml(k)}</i><em>${escapeHtml(String(v))}</em></span>`;
      }).join("");
  }
  const tags = $("coc-tags");
  if (tags && c.tag_counts) {
    tags.innerHTML = `状态标签：伤势 ${c.tag_counts.injury}　诅咒 ${c.tag_counts.curse}　` +
      `城镇 ${c.tag_counts.town}（${c.severity || "normal"}）`;
  }
  const extra = $("coc-extra");
  if (extra && Array.isArray(c.extra)) {
    extra.innerHTML = c.extra.map((d) => `${d.name} ${d.value}`).join("　");
  }
  renderCocTsfBox();
  renderCocInventory();
}

/* ---------- 1.9.19：SAN 理智面板 ----------
   与 REA 分开显示：REA 是意志耐久，SAN 是经典 CoC 的理智值（0–99）。
   状态文案与 bout 全部来自服务端 `coc.san_state`，前端不自己判定阈值。 */
function renderCocSan(c) {
  const box = $("coc-san-box");
  if (!box) return;
  if (c.san === undefined || c.san === null) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const san = Number(c.san) || 0;
  const cap = Number(c.san_max) || 99;
  const pct = Math.max(0, Math.min(100, (san / Math.max(1, cap)) * 100));
  const val = $("coc-san-val");
  if (val) val.textContent = `${san} / ${cap}`;
  const bar = $("coc-san-bar");
  if (bar) bar.style.width = `${pct}%`;
  const st = $("coc-san-state");
  if (st) {
    const s = c.san_state || {};
    st.textContent = s.label || "SAN 稳定";
    st.className = "model-info" + (s.state === "bout" || s.state === "fragile"
      ? " coc-san-danger" : "");
  }
}

/* 1.9.19：一次 SAN 检定。结果由服务端定稿；前端只显示并刷新整段 coc 面板。 */
async function cocSanCheck() {
  if (!S.sid) { toast("还没有进入对局"); return; }
  const btn = $("btn-coc-san-check");
  const hint = $("coc-san-msg");
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/game/${S.sid}/coc/san-check`, {
      method: "POST",
      body: JSON.stringify({
        loss: ($("coc-san-loss") || {}).value || "0/1d6",
        difficulty: ($("coc-san-diff") || {}).value || "regular",
        reason: "manual",
      }),
    });
    if (r && r.coc) {
      S.coc = r.coc.coc || r.coc;
      S.cocTsf = r.coc.coc_tsf || S.cocTsf;
    }
    renderCocPanel();
    const text = r.text || `1D100=${r.roll} / 目标 ${r.target}`;
    if (hint) hint.textContent = text;
    toast(r.success ? "SAN 检定成功" : "SAN 检定失败", 2600);
  } catch (e) {
    if (hint) hint.textContent = "SAN 检定失败：" + e.message;
    toast("SAN 检定失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------- C×T4（t56）：CoC×TSF 的进度块 ----------
   数值 100% 来自服务端（`app/coc/tsf.py` 的 `mechanical_effects`，即 t54 的接口）：
   前端**不重算**阶段，只做显示。没有 `coc_tsf` 字段（纯 coc 局）时整块隐藏。 */

function renderCocTsfBox() {
  const box = $("coc-tsf-box");
  if (!box) return;
  const view = S.cocTsf;
  if (!view) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const prog = Math.max(0, Math.min(100, Number(view.progress) || 0));
  const pct = $("coc-tsf-pct");
  if (pct) pct.textContent = `${prog}%`;
  const bar = $("coc-tsf-bar");
  if (bar) bar.style.width = `${prog}%`;
  const stageBox = $("coc-tsf-stage");
  if (stageBox) {
    const nm = escapeHtml(String(view.stage_name || ""));
    const idt = escapeHtml(String(view.identity || ""));
    const sym = escapeHtml(String(view.symptom || ""));
    stageBox.innerHTML = `阶段 ${(Number(view.stage) || 0) + 1}：${nm}` +
      (idt ? `（${idt}）` : "") + (sym ? `<br>症状：${sym}` : "");
  }
  const hint = $("coc-tsf-sprite-hint");
  if (hint) {
    const ports = (S.assets && S.assets.portraits) || {};
    const ready = Object.values(ports).filter((p) => p.status === "ready").length;
    const pending = Object.values(ports).filter((p) => p.status === "pending").length;
    hint.textContent = ready > 0
      ? `立绘：${ready} 张已就绪`
      : (pending > 0 ? "立绘生成中…（无图也不会中断对局）" : "立绘未生成（无图不报错）");
  }
}

/* ---------- U2（t66）：物品栏渲染 ----------
   数据源：/api/game/{sid} 的 `coc.inventory`（t63 暴露；契约见 COC-STATS-ITEMS.md §9）。
   本函数**只渲染**、不推断：物品的 kind / stat / equip_slot / usable 全部来自后端，
   前端不维护第二份映射表（唯一真源在 data/coc/items.json）。

   为什么"走 DEX/STR"必须显式显示：T↔检查的联动入口就是武器属性 ——
   玩家不看到"这把刀走 DEX"，就无法理解"换武器 ⇒ 换检定属性"（t62 的联动）。 */

const COC_KIND_CN = {
  weapon: "武器", equipment: "装备", consumable: "消耗品", usable: "可用",
  unknown: "未登记",
};

function cocKindCN(kind) {
  return COC_KIND_CN[kind] || String(kind || "");
}

/* 物品的一句人话摘要：武器给"走 DEX"，消耗品给治疗目标，装备给可装备槽位。 */
function cocItemHint(it) {
  const parts = [cocKindCN(it.kind)];
  if (it.kind === "weapon" && it.stat) parts.push(`走 ${it.stat}`);
  if (it.equip_slot) {
    const slots = (S.coc && S.coc.inventory && S.coc.inventory.slots) || {};
    parts.push(`可装备到 ${slots[it.equip_slot] || it.equip_slot}`);
  }
  if (it.hand === "two") parts.push("双手");
  if (it.cures === "minor") parts.push("治轻伤");
  if (it.cures === "major") parts.push("治重伤");
  return parts.join(" · ");
}

function renderCocInventory() {
  const box = $("coc-inventory");
  if (!box) return;
  const inv = (S.coc && S.coc.inventory) || null;
  if (!inv) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");

  const count = $("coc-inv-count");
  if (count) {
    count.textContent = String((inv.counts && inv.counts.backpack) || 0);
  }

  // ---- 三个装备槽（契约 id：coc-inv-equip-R / -C / -B）----
  const slots = inv.slots || {};
  const eq = inv.equipped || {};
  const byId = {};
  (inv.backpack || []).forEach((x) => { byId[x.id] = x; });
  const order = ["R", "C", "B"].filter((k) => k in slots)
    .concat(Object.keys(slots).filter((k) => ["R", "C", "B"].indexOf(k) < 0));
  order.forEach((k) => {
    const box2 = $("coc-inv-equip-" + k);
    if (!box2) return;
    const cur = eq[k];
    const label = escapeHtml(String(slots[k] || k));
    if (!cur) { box2.innerHTML = `${label}：<i>空</i>`; return; }
    const meta = byId[cur.id] || cur;
    const stat = meta.stat ? `（走 ${escapeHtml(String(meta.stat))}）` : "";
    const iid = escapeHtml(String(cur.id));
    // 槽位里的卸下按钮用 `coc-inv-unequip-slot-<K>`（不与背包里按物品 id 命名的
    // 契约按钮 `coc-inv-unequip-<itemId>` 撞 id —— 闸门会查重复 id）
    box2.innerHTML = `${label}：<b>${escapeHtml(String(cur.name || cur.id))}</b>${stat}`
      + ` <button id="coc-inv-unequip-slot-${escapeHtml(k)}"`
      + ` class="btn mini ghost coc-inv-act" data-action="unequip"`
      + ` data-slot="${escapeHtml(k)}">卸下</button>`;
  });

  // ---- 当前武器（判定属性的一眼可见入口） ----
  const w = $("coc-inv-weapon");
  if (w) {
    w.textContent = inv.weapon
      ? `⚔ 当前武器：${inv.weapon.name}（${inv.weapon.stat} 检定）`
      : "⚔ 当前未装备武器";
  }

  // ---- t71（TSF×CoC 融合）：身体变化对物品/装备的影响（衣物档 + 属性修正） ----
  const tsfBox = $("coc-inv-tsf");
  if (tsfBox) {
    // 数据可能在 coc.inventory.tsf_mech，也可能在 coc.panel.tsf_mech（两处都读，取有值的）
    const mech = (inv && inv.tsf_mech)
      || ((S.coc && S.coc.panel && S.coc.panel.tsf_mech) || null);
    if (!mech || !mech.tier) {
      tsfBox.textContent = "";
      tsfBox.classList.add("hidden");
    } else {
      const sh = mech.shifts || {};
      const parts = [];
      Object.keys(sh).forEach((k) => {
        const v = Number(sh[k]) || 0;
        if (v !== 0) parts.push(`${k} ${v > 0 ? "+" : ""}${v}`);
      });
      const wd = mech.wardrobe || {};
      let txt = `🧬 转变第 ${mech.tier} 档`;
      if (wd.label) txt += ` · ${wd.label}`;
      if (parts.length) txt += `　（属性 ${parts.join("　")}）`;
      const pen = Number(mech.use_penalty) || 0;
      if (pen > 0) txt += `　使用类操作 +${pen}`;
      tsfBox.textContent = txt;
      tsfBox.classList.remove("hidden");
    }
  }

  // ---- 背包条目 ----
  const list = $("coc-inv-list");
  if (list) {
    const bag = inv.backpack || [];
    if (!bag.length) {
      // t72：空背包要说清"为什么空"与"东西从哪来"，否则玩家会以为这模式根本没物品系统。
      list.innerHTML = `<div class="model-info coc-inv-empty">`
        + `背包是空的 —— 调查地点、触发事件卡、搜刮与购买都会把物品放进这里。`
        + `拿到后可直接装备（武器走 R 槽、护符走 C 槽、衣物走 B 槽）。`
        + `</div>`;
    } else {
      list.innerHTML = bag.map((it) => {
        const name = escapeHtml(String(it.name || it.id));
        const hint = escapeHtml(cocItemHint(it));
        const eq = it.equipped ? " · 已装备" : "";
        const iid = escapeHtml(String(it.id));
        // t71（TSF×CoC 融合）：身体变化会让旧装备**不合身/握不住**。
        // 后端已把 `wearable` / `block_reason` / `use_penalty` 放进每件物品的视图里，
        // 这里要把它们变成玩家看得懂的东西 —— 否则"为什么装不上"只能靠猜。
        const blocked = it.wearable === false && it.block_reason;
        const reasonTxt = blocked ? cocReasonCN(it.block_reason) : "";
        const pen = Number(it.use_penalty) || 0;
        // 不可装备 ⇒ **不渲染装备按钮**（避免玩家点了才发现被拒）；
        // 已装备的仍给"卸下"（卸下永远允许）。
        let btn = "";
        if (it.equipped && it.equip_slot) {
          btn = `<button id="coc-inv-unequip-${iid}" class="btn mini ghost coc-inv-act"`
            + ` data-action="unequip" data-slot="${escapeHtml(String(it.equip_slot))}">卸下</button>`;
        } else if (it.equip_slot && !blocked) {
          btn = `<button id="coc-inv-equip-${iid}" class="btn mini ghost coc-inv-act"`
            + ` data-action="equip" data-item="${iid}">装备</button>`;
        }
        // 1.9.19：可用品（绷带/镇静剂/日记…）在前端就能直接使用；具体效果由服务端结算
        if (it.usable) {
          btn += `<button id="coc-inv-use-${iid}" class="btn mini ghost coc-inv-act"`
            + ` data-action="use" data-item="${iid}">使用</button>`;
        }
        const tag = blocked
          ? `<small class="coc-inv-blocked">⚠ ${escapeHtml(reasonTxt)}</small>`
          : (pen > 0
            ? `<small class="coc-inv-penalty">使用需额外耗时 +${pen}</small>`
            : "");
        return `<div class="coc-inv-item${blocked ? " coc-inv-unfit" : ""}"><span>${name}</span>`
          + `<small class="model-info">${hint}${eq}</small>${tag}${btn}</div>`;
      }).join("");
    }
  }

  // ---- 仓库（结构已在视图里；当前没有任何动作会往里放东西，如实显示） ----
  const sto = $("coc-inv-storage");
  if (sto) {
    const st = inv.storage || [];
    sto.innerHTML = st.length
      ? `<div class="model-info">仓库：${st.map((x) => escapeHtml(String(x.name || x.id))).join("　")}</div>`
      : "";
  }
}

/* 装备 / 卸下：走**受保护端点** POST /api/game/{sid}/coc/equip。
   为什么必须走端点而不是直接改内存里的 S.coc：
     · `equip`/`unequip` 是 `sanitize.PLAYER_ACTION_KEYS` 里的**玩家动作**，
       只有服务端经 `Run.apply`（唯一写点）落账才会持久化并跑不变式；
     · 前端自己改一份视图 = 造出第二条真相，读档就露。
   失败时**必须展示后端给的 reason**（"为什么点不动"要可读，不能静默）。 */
const COC_REASON_CN = {
  not_in_backpack: "不在背包里",
  not_equippable: "这件物品不能装备",
  unregistered: "未登记的物品",
  slot_empty: "该槽位本来就空着",
  unknown_action: "未知操作",
  no_effect: "没有发生变化",
  ok: "已生效",
  // t71（TSF×CoC 融合）：转变导致的不可用，原因要说清是"身体变了"而不是"物品坏了"
  tsf_slot_unfit: "身体变化后已不合身，无法继续穿戴（需要更合身的衣物）",
  tsf_hand_unfit: "身体变化后已无法有效使用（体格与握持半径变了）",
  // 1.9.19：物品使用
  not_usable: "这件物品不能直接使用",
  not_usable_equipment: "装备类物品请用「装备」按钮",
  no_matching_injury: "没有对应类型的伤势",
  no_effect: "现在派不上用场",
  use_failed: "使用没有生效（物品仍在背包里）",
  // 1.9.19：固定商店
  unknown_shop: "未知商店",
  not_in_stock: "这件商品已经不在货架上",
  insufficient_funds: "资金不足",
  grant_failed: "物品没有进入背包",
  take_failed: "物品没有从背包移除",
  already_owned: "背包里已经有这件物品",
};

function cocReasonCN(reason) {
  // 端点失败时 `reason` 的形态是 `<action>:<arg>(<code>)`（多个用 `;` 连接，来自引擎的
  // `dropped`），例如 `equip:NoSuchThing(not_in_backpack)`、`unequip:C(slot_empty)`；
  // 引擎内层函数则给**裸 code**（`not_in_backpack`）。两种都要能翻译，否则玩家会看到
  // 原始英文码 —— 这是实测发现的一处真缺陷（窄匹配）。故：先取最后一对括号里的内容当 code。
  const raw = String(reason || "").trim();
  if (!raw) return "操作未成功";
  const codeOf = (one) => {
    const s = String(one || "").trim();
    const m = s.match(/\(([^()]*)\)\s*$/);
    return m ? m[1].trim() : s;
  };
  const tr = (c) => {
    const code = codeOf(c);
    if (COC_REASON_CN[code]) return COC_REASON_CN[code];
    if (code.startsWith("stat_below:")) {
      return "属性不足（" + code.slice("stat_below:".length) + "）";
    }
    if (code.startsWith("weapon_")) return "需要先装备武器";
    if (code.indexOf(":") > 0) {           // 异常形态 `Name:msg` → 只显示消息体
      const tail = code.slice(code.indexOf(":") + 1).trim();
      if (tail) return tail;
    }
    return code || "操作未成功";
  };
  const parts = raw.split(";").map(tr).filter(Boolean);
  return parts.length ? parts.join("；") : "操作未成功";
}

function cocInvMsg(text) {
  const el = $("coc-inv-msg");
  if (el) el.textContent = String(text || "");
}

async function cocInventoryAct(action, payload, btn) {
  if (!S.sid) { toast("还没有进入对局"); return; }
  if (btn) btn.disabled = true;
  // 动作**前**的视图：用于反查"换装顶掉的是谁"（后端若没回 replaced 时的兜底）
  const invBefore = (S.coc && S.coc.inventory) || {};
  const eqBefore = invBefore.equipped || {};
  const itemId = (payload && payload.item) || "";
  const metaBefore = ((invBefore.backpack || []).find((x) => x.id === itemId)) || null;
  const slotKey = (payload && payload.slot) || (metaBefore && metaBefore.equip_slot) || "";
  const wasEquipped = slotKey ? (eqBefore[slotKey] || null) : null;
  try {
    const r = await api(`/api/game/${S.sid}/coc/equip`, {
      method: "POST",
      body: JSON.stringify(Object.assign({ action }, payload || {})),
    });
    // 回填视图：**顶层 `inventory`** 是端点从 `equip_item` 直接带出来的**新鲜**视图，
    // 是唯一权威来源；`coc.inventory` 是整段 coc 面板里的同一份数据
    // （两个出口都应可用，但以顶层为准 —— 实测顶层一定是最新的）。
    if (r && r.inventory) {
      S.coc = Object.assign({}, S.coc || {}, { inventory: r.inventory });
    } else if (r && r.coc && r.coc.inventory) {
      S.coc = r.coc;
    }
    renderCocInventory();
    if (r && r.ok) {
      const what = action === "equip" ? "已装备" : "已卸下";
      // 契约 §9.2：换装时后端会给 `replaced`。**优先用它**；若这条路径没带
      // （历史版本 API 层未透传，t66 已在 server.py 补上），就从"动作前的视图"反查。
      let replacedFrom = "";
      let replaced = (r && r.replaced === undefined) ? null : (r && r.replaced);
      if (replaced) replacedFrom = "后端字段";
      const nowId = (r && r.item) || itemId;
      if (!replaced && wasEquipped && wasEquipped.id !== nowId) {
        replaced = wasEquipped.name || wasEquipped.id;
        replacedFrom = "视图反查";
      }
      const who = (r && r.item) || payload.item || payload.slot || "";
      const msg = action === "equip" && replaced
        ? `${what}：${who}（替换下 ${replaced}）`
        : `${what}：${who}`;
      toast(msg);
      cocInvMsg("✓ " + msg + (replaced ? ` · 来源=${replacedFrom}` : ""));
    } else {
      const reason = (r && r.reason) || "";
      const msg = `操作未生效：${cocReasonCN(reason)}${reason ? `（${reason}）` : ""}`;
      toast(msg);
      cocInvMsg("✗ " + msg);
    }
    document.dispatchEvent(new CustomEvent(
      action === "equip" ? "coc:inventory-equip" : "coc:inventory-unequip",
      { detail: { item: (r && r.item) || payload.item || "",
                  slot: (r && r.slot) || payload.slot || "",
                  ok: !!(r && r.ok),
                  reason: (r && r.reason) || "" } }));
    document.dispatchEvent(new CustomEvent("coc:inventory-refresh",
      { detail: { inventory: (S.coc && S.coc.inventory) || null } }));
  } catch (e) {
    // 网络/404/403 等也要有可读反馈（不能静默无反应）
    toast("操作失败：" + e.message);
    cocInvMsg("✗ 操作失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* 1.9.19：使用背包物品（绷带/医疗包/镇静剂/日记…）。服务端负责消耗与效果。 */
async function cocUseItem(itemId, btn) {
  if (!S.sid) { toast("还没有进入对局"); return; }
  if (!itemId) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/game/${S.sid}/coc/use-item`, {
      method: "POST",
      body: JSON.stringify({ item: itemId }),
    });
    if (r && r.coc) {
      S.coc = r.coc.coc || r.coc;
      S.cocTsf = r.coc.coc_tsf || S.cocTsf;
    } else if (r && r.inventory && S.coc) {
      S.coc = Object.assign({}, S.coc, { inventory: r.inventory });
    }
    renderCocPanel();
    if (r && r.ok) {
      toast(r.text || "已使用");
      cocInvMsg("✓ " + (r.text || "已使用"));
    } else {
      const why = cocReasonCN(r.reason || "");
      toast("使用未生效：" + why);
      cocInvMsg("✗ " + (r.text || why || "使用未生效"));
    }
  } catch (e) {
    toast("使用失败：" + e.message);
    cocInvMsg("✗ 使用失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderTsfPanel() {
  const tsfData = S.tsf;
  const who = selectedChar();
  const prev = S.charPrev || {};
  renderWhoChips("tsf-who");

  if (!tsfData) {                 // 状态未就绪（如重载中）时至少显示选中角色
    renderStatRows($("tsf-bars"), who && who.states, who ? prev[who.name] : {});
    return;
  }
  // 主角立绘徽章：同化率 X%（特殊标记）
  const prog = (tsfData.stats || []).find((s) => s.key === "progress");
  document.querySelectorAll(".sprite-badge").forEach((b) => {
    const sprite = b.closest(".sprite");
    if (sprite && sprite.dataset.kind === "prot") {
      b.textContent = `同化率 ${prog ? prog.value : 0}%`;
      b.classList.toggle("hidden", prog === undefined);
    }
  });
  const isProt = !who || who.is_protagonist;
  $("tsf-identity").classList.toggle("hidden", !isProt);
  $("tsf-stage-desc").classList.toggle("hidden", !isProt);
  const bars = $("tsf-bars");
  if (!isProt) {                  // 其他角色：只显示 TA 自己的一套数值
    renderStatRows(bars, who.states, prev[who.name]);
    renderBond("tsf-bond", who);
    return;
  }
  $("tsf-identity-text").textContent = tsfData.identity;
  $("tsf-stage-name").textContent = tsfData.stage_name;
  $("tsf-stage-desc").textContent = tsfData.stage_desc;
  const prevTsf = S.tsfPrev
    ? Object.fromEntries((S.tsfPrev.stats || []).map((s) => [s.key, s.value]))
    : {};
  renderStatRows(bars, tsfData.stats, prevTsf);
  // 主角自己的情绪 / 好感 / 身体数值，接在转变数值之后
  if (who && who.states && who.states.length) {
    const hr = document.createElement("div");
    hr.className = "tsf-subhead";
    hr.textContent = "情绪 · 好感 · 身体";
    bars.appendChild(hr);
    renderStatRows(bars, who.states, prev[who.name], true);
  }
  renderBond("tsf-bond", who);
}

/* ---------- 普通模式面板（每个角色各自的心情 / 好感 / 性敏感 / 高潮度）---------- */

function renderVnPanel() {
  renderWhoChips("vn-who");
  const box = $("vn-bars");
  const who = selectedChar();
  if (who) {
    renderStatRows(box, who.states, (S.charPrev || {})[who.name]);
    renderBond("vn-bond", who);
    return;
  }
  // 兼容旧服务端（没有 char_stats 时退回单一数值集）
  renderStatRows(box, (S.vn && S.vn.states) || []);
  renderBond("vn-bond", null);
}
// —— UI 绑定段 bindUiPart16（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart16() {

  $("vn-collapse").addEventListener("click", () => {
    $("vn-panel").classList.add("hidden");
    $("vn-tab").classList.remove("hidden");
  });
  $("vn-tab").addEventListener("click", () => {
    $("vn-tab").classList.add("hidden");
    $("vn-panel").classList.remove("hidden");
  });
  $("btn-vn-regen").addEventListener("click", () => regenerateSprite("all"));

  $("tsf-collapse").addEventListener("click", () => {
    $("tsf-panel").classList.add("hidden");
    $("tsf-tab").classList.remove("hidden");
  });
  $("tsf-tab").addEventListener("click", () => {
    $("tsf-tab").classList.add("hidden");
    $("tsf-panel").classList.remove("hidden");
  });
}

async function openPortraitFolder() {
  try {
    const body = S.sid ? JSON.stringify({ sid: S.sid }) : "{}";
    const r = await api("/api/open-folder", {
      method: "POST", body,
    });
    toast("已打开立绘文件夹：" + r.path, 2600);
  } catch (e) {
    toast("打开失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart17（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart17() {
  $("btn-open-folder").addEventListener("click", openPortraitFolder);
  $("btn-vn-open-folder").addEventListener("click", openPortraitFolder);
}

function forceReloadSprites() {
  S.imgVersion = (S.imgVersion || 0) + 1;
  document.querySelectorAll(".sprite img").forEach((im) => {
    im.dataset.url = "";
    im.classList.remove("show");
  });
  const line = S.turn && S.turn.dialogue[S.lineIdx];
  if (line) updateSprites(line.character, line.emotion);
}
// —— UI 绑定段 bindUiPart18（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart18() {

  $("btn-regen-prot").addEventListener("click", () => {
    regenerateSprite("protagonist");
  });

  $("btn-regen-all").addEventListener("click", () => {
    regenerateSprite("all");
  });
}

/* ---------- 主角「转变目标设定」（1.7.16）：体型/头发/发色可配置 ---------- */
function openTsfTarget() {
  if (!S.sid) { toast("请先进入对局"); return; }
  const t = S.tsfTarget || {};
  const hair = $("tsf-t-hair");
  const build = $("tsf-t-build");
  if (t.hair && hair) hair.value = t.hair;
  if (t.build && build) build.value = t.build;
  $("tsf-t-haircolor").value = t.hair_color || "";
  $("tsf-t-aura").value = t.aura || "";
  $("tsf-t-keepcolor").checked = t.keep_hair_color !== false;
  $("tsf-target-hint").textContent = "";
  $("modal-tsf-target").classList.remove("hidden");
}
// —— UI 绑定段 bindUiPart19（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart19() {
  $("btn-tsf-target").addEventListener("click", openTsfTarget);
  $("btn-tsf-target-close").addEventListener("click", () =>
    $("modal-tsf-target").classList.add("hidden"));
  $("btn-tsf-target-save").addEventListener("click", async () => {
    const btn = $("btn-tsf-target-save");
    btn.disabled = true;
    try {
      const body = {
        hair: $("tsf-t-hair").value,
        build: $("tsf-t-build").value,
        hair_color: $("tsf-t-haircolor").value.trim(),
        aura: $("tsf-t-aura").value.trim(),
        keep_hair_color: $("tsf-t-keepcolor").checked,
      };
      const r = await api(`/api/game/${S.sid}/tsf-target`, {
        method: "POST", body: JSON.stringify(body),
      });
      S.tsfTarget = r.target || {};
      toast("转变目标已保存，正在按新目标重画主角立绘…", 3200);
      $("modal-tsf-target").classList.add("hidden");
      S.imgVersion = (S.imgVersion || 0) + 1;
      startPolling();   // 轮询直到新立绘 ready/error
    } catch (e) {
      toast("保存失败：" + e.message);
    } finally {
      btn.disabled = false;
    }
  });
}

async function recutAllNow() {
  try {
    const state = await api(`/api/game/${S.sid}/recut`, { method: "POST" });
    S.assets = state.assets;
    toast("已按当前抠图标准重新抠图全部立绘");
    forceReloadSprites();
  } catch (e) {
    toast("重新抠图失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart20（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart20() {
  $("btn-recut").addEventListener("click", recutAllNow);
  $("btn-vn-recut").addEventListener("click", recutAllNow);
}

async function restoreRawNow() {
  try {
    const state = await api(`/api/game/${S.sid}/restore-raw`, { method: "POST" });
    S.assets = state.assets;
    toast("已还原为去除背景之前的原始立绘");
    forceReloadSprites();
  } catch (e) {
    toast("还原失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart21（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart21() {
  $("btn-restore-raw").addEventListener("click", restoreRawNow);
  $("btn-vn-restore-raw").addEventListener("click", restoreRawNow);
}

async function exportToLibrary() {
  try {
    const r = await api(`/api/game/${S.sid}/library`, { method: "POST" });
    toast(`角色库已更新：${r.characters} 位角色 · ${r.portraits} 张立绘`, 3400);
  } catch (e) {
    toast("角色库导出失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart22（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart22() {
  $("btn-library").addEventListener("click", exportToLibrary);
  $("btn-vn-library").addEventListener("click", exportToLibrary);
}

async function exportSprites() {
  try {
    const r = await api(`/api/game/${S.sid}/export`, { method: "POST" });
    toast(`已导出 ${r.files} 张立绘（修图适配目录）`, 3200);
  } catch (e) {
    toast("导出失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart23（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart23() {
  $("btn-export").addEventListener("click", exportSprites);
  $("btn-vn-export").addEventListener("click", exportSprites);
}

/* ---------- 轮次播放 ---------- */

function playTurn(state, { instant }) {
  const prevTsf = S.tsf;
  const prevChars = charSnapshot();   // 更新前各角色数值，供面板算 ▲▼
  S.turn = state;
  S.tsf = state.tsf;
  S.vn = state.vn;
  S.coc = state.coc || null;
  S.aiDirective = state.ai_directive || S.aiDirective || "";
  // C×T4（t56）：每轮都用服务端新值刷新 TSF 视图（缺字段时保留旧值，避免面板闪空）
  if (state.coc_tsf) S.cocTsf = state.coc_tsf;
  if (state.assets) S.assets = state.assets;
  // 关键：推进/发言/读档后立即同步背景映射，否则第一帧永远取旧 map，
  // 新幕背景要等轮询才能出现（旧档/已就绪资产无轮询 → 永远不显示）
  S.bgMap = state.bg_map || S.bgMap;
  // 「立刻使用此立绘」的固定显示：立绘历程里固定后，每幕都保持该版本
  S.pinned = state.pinned || S.pinned || {};
  // 主角「转变目标设定」（体型/头发/发色等，保存即重画）
  S.tsfTarget = state.tsf_target || S.tsfTarget || {};
  S.lineIdx = 0;
  $("choices-wrap").classList.add("hidden");
  $("choices-wrap").innerHTML = "";
  const tag = $("scene-tag");
  tag.textContent = `第 ${state.turn_no} 幕 · ${state.scene}`;
  tag.classList.add("show");
  S.tsfPrev = prevTsf;
  if (state.char_stats) {
    S.charPrev = prevChars;
    S.charStats = state.char_stats;
  }
  // 1.9.0：关系里程碑只在正常推进时提示；读档/静默刷新不刷屏
  if (!instant && Array.isArray(state.bond_up)) {
    state.bond_up.slice(0, 3).forEach((ev) => {
      toast(`💗 与「${ev.name}」的关系进入「${ev.label}」`, 3800);
    });
  }
  renderStatsPanels();
  // 角色池随轮次更新（AI 引入的新角色立绘才能显示）
  if (state.npcs) S.npcs = state.npcs;
  // 每一幕都严格按「开场名单」重建立绘层：名单有谁就显示谁、
  // 名单为空就一个不显示（旁白/独处轮不会残留上一幕角色）
  S.present = (state.present || []);
  buildSprites();
  applyBackground();
  // CG 桥段：全屏展示 CG 图、临时隐藏所有角色立绘；CG 结束时恢复角色
  const cgActive = !!state.cg_active;
  const cgPrev = !!S.cgActive;
  S.cgActive = cgActive;
  const cgLayer = document.getElementById("cg-layer");
  if (cgLayer) {
    if (cgActive) {
      cgLayer.classList.remove("hidden");
      const cgKey = state.cg_key || "";
      if (cgLayer.dataset.key !== cgKey) {
        cgLayer.innerHTML = "";
        const img = document.createElement("img");
        img.src = state.cg_url || "";
        img.onload = () => img.classList.add("show");
        cgLayer.appendChild(img);
        const title = document.createElement("div");
        title.className = "cg-title";
        title.textContent = state.cg_title || "CG";
        cgLayer.appendChild(title);
        cgLayer.dataset.key = cgKey;
      }
      document.getElementById("sprite-layer").style.visibility = "hidden";
    } else {
      cgLayer.classList.add("hidden");
      document.getElementById("sprite-layer").style.visibility = "";
      if (cgPrev) {
        // CG 结束：重建角色立绘显示
        buildSprites();
      }
    }
  }
  if (!instant && state.stage_up) showStageUp(state.tsf);
  updateCatchphrases(state.catchphrases || []);
  showDialogLine(instant);
  // 有生成中的资源（换装立绘/新背景等）时保持轮询；全部就绪后可暂停
  if (hasPending()) startPolling();
  // 桥段 CG 自动收藏（cg_saved 防重；再次进入同幕不会重复入库）
  if (state.cg_active && !state.cg_saved) {
    saveCg("bridge", "");
  }
  // 1.8.0：六段轨道与顶栏「绘画状态」随每幕刷新
  if (window.UI2) UI2.onTurn();
}

function showDialogLine(instant) {
  const line = S.turn.dialogue[S.lineIdx];
  if (!line) { showChoices(); return; }
  const nameTag = $("name-tag");
  const isNarration = line.character === "旁白";
  nameTag.classList.remove("hidden");
  nameTag.classList.toggle("narration", isNarration);
  nameTag.textContent = line.character;
  updateSprites(line.character, line.emotion);
  typeText($("dialog-text"), line.text, instant);
}

function typeText(el, text, instant) {
  clearTimeout(S.typeTimer);
  S.typing = true;
  $("next-hint").classList.add("hidden");
  if (instant) {
    el.textContent = text;
    S.typing = false;
    $("next-hint").classList.remove("hidden");
    if (window.UI2) UI2.onTypingDone();
    return;
  }
  el.textContent = "";
  let i = 0;
  const step = () => {
    if (i < text.length) {
      el.textContent += text[i++];
      const ch = text[i - 1];
      const pause = "。，！？…—".includes(ch) ? 160 : 30;
      S.typeTimer = setTimeout(step, pause);
    } else {
      S.typing = false;
      $("next-hint").classList.remove("hidden");
      if (window.UI2) UI2.onTypingDone();
    }
  };
  step();
}
// —— UI 绑定段 bindUiPart24（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart24() {

  $("dialog-box").addEventListener("click", () => {
    if (S.typing) {
      clearTimeout(S.typeTimer);
      const line = S.turn.dialogue[S.lineIdx];
      $("dialog-text").textContent = line.text;
      S.typing = false;
      $("next-hint").classList.remove("hidden");
    } else {
      nextLine();
    }
  });
}

function nextLine() {
  if (!$("choices-wrap").classList.contains("hidden")) return;
  S.lineIdx++;
  if (S.lineIdx < S.turn.dialogue.length) {
    showDialogLine(false);
  } else {
    showChoices();
  }
}

function updateCatchphrases(list) {
  S.catchphrases = list;
  const bar = $("catchphrase-bar");
  bar.innerHTML = "";
  list.forEach((text) => {
    const btn = document.createElement("button");
    btn.className = "catchphrase-btn";
    btn.textContent = "💬 " + text;
    btn.addEventListener("click", () => speakPhrase(text));
    bar.appendChild(btn);
  });
  bar.classList.toggle("hidden", !list.length);
}

async function speakPhrase(text) {
  if (!S.sid) return;
  $("choices-wrap").classList.add("hidden");
  $("loading-overlay").classList.remove("hidden");
  $("loading-text").textContent = "正在推进剧情……";
  try {
    const state = await api(`/api/game/${S.sid}/speak`, {
      method: "POST", body: JSON.stringify({ text }),
    });
    S.assets = state.assets;
    S.bgMap = state.bg_map || S.bgMap;
    $("loading-overlay").classList.add("hidden");
    playTurn(state, { instant: false });
  } catch (e) {
    $("loading-overlay").classList.add("hidden");
    toast("发言失败：" + e.message);
    $("choices-wrap").classList.remove("hidden");
  }
}

function showChoices() {
  if (window.UI2) UI2.cancelAuto();   // 出现选项：自动播放必须停（否则会跳过选择）
  $("next-hint").classList.add("hidden");
  const wrap = $("choices-wrap");
  // 1.9.1：选项拖动防误触。鼠标从 A 拖到 B 时，浏览器默认会把整排文字
  // 选成选区（看起来所有选项都亮），并且 mouseup 不再触发 click。
  // 这里只做「拖动中抑制 hover 尾迹」；文本选择由 CSS user-select:none 关掉。
  if (!wrap.dataset.dragBound) {
    wrap.dataset.dragBound = "1";
    const clearHot = () => {
      wrap.querySelectorAll(".choice-btn.hot").forEach((b) => b.classList.remove("hot"));
    };
    const endDrag = () => {
      wrap.classList.remove("dragging");
      clearHot();
    };
    // QtWebEngine 的 :hover 在部分机器上会「粘住」上一条选项（移开也不消失），
    // 因此不再依赖 CSS :hover，而用 JS 的 .hot 类做单点高亮，pointerleave 必清。
    wrap.addEventListener("pointermove", (e) => {
      const btn = e.target && e.target.closest
        ? e.target.closest(".choice-btn") : null;
      wrap.querySelectorAll(".choice-btn").forEach((b) => {
        b.classList.toggle("hot", b === btn);
      });
    });
    wrap.addEventListener("pointerdown", (e) => {
      const btn = e.target && e.target.closest
        ? e.target.closest(".choice-btn") : null;
      if (btn) {
        wrap.classList.add("dragging");
        wrap.querySelectorAll(".choice-btn").forEach((b) => {
          b.classList.toggle("hot", b === btn);
        });
      }
    });
    wrap.addEventListener("pointerup", endDrag);
    wrap.addEventListener("pointercancel", endDrag);
    wrap.addEventListener("pointerleave", endDrag);
    wrap.addEventListener("dragstart", (e) => e.preventDefault());
  }
  wrap.innerHTML = "";
  S.turn.choices.forEach((c, idx) => {
    const btn = document.createElement("button");
    btn.className = "choice-btn";
    btn.textContent = c.text + (c.bias ? `  ⟪${biasName(c.bias)}⟫` : "");
    btn.addEventListener("click", () => choose(idx));
    wrap.appendChild(btn);
  });
  // 最底部：玩家可自主编辑的选项（输入任意内容并发言）
  const row = document.createElement("div");
  row.className = "custom-choice-row";
  row.innerHTML = `
    <input id="custom-choice-input" class="text-input" placeholder="✏ 或输入你想说的话…" maxlength="200">
    <button id="custom-choice-send" class="btn primary">说</button>`;
  wrap.appendChild(row);
  const input = row.querySelector("#custom-choice-input");
  row.querySelector("#custom-choice-send").addEventListener("click", () => {
    const text = input.value.trim();
    if (!text) { toast("先输入想说的话"); return; }
    $("choices-wrap").classList.add("hidden");
    speakPhrase(text);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      row.querySelector("#custom-choice-send").click();
    }
  });
  wrap.classList.remove("hidden");
}

function biasName(key) {
  const map = {
    progress: "同化", physique: "体质", acuity: "感官",
    habit: "习惯", immersion: "沉溺",
    mood: "心情", affection: "好感", arousal: "性敏感", climax: "高潮度",
  };
  const lists = [];
  if (S.statCfg) {
    const has = (list) => Array.isArray(list) && list.some((c) => c && c.key === key);
    if (S.statCfg.session) lists.push(S.statCfg.session);
    const merged = (S.statCfg.tsf || []).concat(S.statCfg.vn || []);
    if (!has(S.statCfg.session) && has(merged)) lists.push(merged);
  }
  const find = list => Array.isArray(list) ? list.find(c => c && c.key === key) : null;
  for (const list of lists) {
    const f = find(list);
    if (f && f.name) return f.name;
  }
  return map[key] || key;
}

async function choose(idx) {
  $("choices-wrap").classList.add("hidden");
  $("loading-overlay").classList.remove("hidden");
  $("loading-text").textContent = "正在书写剧情……";
  try {
    const state = await api(`/api/game/${S.sid}/advance`, {
      method: "POST", body: JSON.stringify({ choice_index: idx }),
    });
    S.assets = state.assets;
    S.bgMap = state.bg_map || S.bgMap;
    $("loading-overlay").classList.add("hidden");
    playTurn(state, { instant: false });
  } catch (e) {
    $("loading-overlay").classList.add("hidden");
    toast("推进失败：" + e.message);
    $("choices-wrap").classList.remove("hidden");
  }
}

/* ---------- 阶段跃迁演出 ---------- */

function showStageUp(tsfData) {
  const ov = $("stageup-overlay");
  $("stageup-text").textContent = `✧ ${tsfData.stage_name} ✧`;
  ov.classList.remove("hidden");
  ov.classList.add("active");
  setTimeout(() => {
    ov.classList.remove("active");
    ov.classList.add("hidden");
  }, 2200);
}

let phistEntries = [];
let phistPage = 1;
const PHIST_PAGE_SIZE = 8;

function renderHistPage(protagonistName) {
  const grid = $("prot-hist-grid");
  grid.innerHTML = "";
  const pages = Math.max(1, Math.ceil(phistEntries.length / PHIST_PAGE_SIZE));
  phistPage = Math.min(Math.max(1, phistPage), pages);
  const slice = phistEntries.slice((phistPage - 1) * PHIST_PAGE_SIZE,
    phistPage * PHIST_PAGE_SIZE);
  $("phist-page").textContent = `第 ${phistPage}/${pages} 页`;
  $("btn-phist-prev").disabled = phistPage <= 1;
  $("btn-phist-next").disabled = phistPage >= pages;
  $("prot-hist-hint").textContent = `共 ${phistEntries.length} 张版本记录（每页 ${PHIST_PAGE_SIZE} 张）`;
  renderPinBar();
  if (!slice.length) {
    grid.innerHTML = '<p class="hint-line">暂无立绘记录（生成或重画后自动记录）</p>';
    return;
  }
  slice.forEach((e) => {
    const prot = (e.label || "").split("·")[0] === protagonistName;
    const card = document.createElement("div");
    card.className = "cg-item" + (e.current ? " cur" : "");
    card.innerHTML = `
      <img src="/img/${encodeURI(e.file)}?v=${Date.now()}" alt="立绘" loading="lazy">
      <div class="cg-caption">${escapeHtml(e.label || "立绘")}
        ${e.progress ? `<span class="cg-type">同化率 ${e.progress}%</span>` : ""}
        ${e.current ? `<span class="cg-type">当前</span>` : `<span class="cg-type">旧版</span>`}
      </div>
      ${e.current ? `<div class="hist-actions">
          <button class="btn mini ghost hist-pincur" data-label="${escapeHtml(e.label || "")}" title="这张标着「当前」但舞台不一定在用——点此直接替换为实际显示">⚡ 替换为此立绘</button>
          <button class="btn mini ghost hist-del" data-label="${escapeHtml(e.label || "")}" data-file="${escapeHtml(e.file || "")}" title="删除这张立绘并按阶段自动重绘（删除后按当前进度/表情重新生成一张）">🗑 删除</button>
        </div>` : `<div class="hist-actions">
          <button class="btn mini ghost hist-use" data-label="${escapeHtml(e.label || "")}" data-file="${escapeHtml(e.file || "")}">⚡ 立刻使用</button>
          <button class="btn mini ghost hist-restore" data-label="${escapeHtml(e.label || "")}" data-file="${escapeHtml(e.file || "")}">↩ 还原</button>
          <button class="btn mini ghost hist-del" data-label="${escapeHtml(e.label || "")}" data-file="${escapeHtml(e.file || "")}" title="删除这张历史立绘（文件删除、不再被调用；当前使用版不可删）">🗑 删除</button>
        </div>`}`;
    card.querySelector("img").addEventListener("click", () => {
      $("cg-view-img").src = `/img/${encodeURI(e.file)}`;
      $("cg-view-title").textContent = (e.label || "立绘")
        + (e.progress ? " · 同化率 " + e.progress + "%" : "")
        + (e.current ? " · 当前" : " · 旧版");
      // 仅对立绘历程里的角色图生效（CG 回廊无标签）
      S.cgView = { label: e.label || "", file: e.file || "", current: !!e.current };
      if ($("btn-cg-regen-diff")) {
        $("btn-cg-regen-diff").classList.toggle("hidden", !S.cgView.label);
      }
      if ($("btn-cg-set-current")) {
        // 1.7.24：标「当前」的立绘同样可替换（可能未真正生效）——有标签即可用
        $("btn-cg-set-current").classList.toggle("hidden", !S.cgView.label);
      }
      // 1.7.39 删除此立绘：新旧版本均可——当前版删除后按阶段自动重绘
      if ($("btn-cg-del-hist")) {
        $("btn-cg-del-hist").classList.toggle("hidden", !S.cgView.label);
      }
      $("modal-cg-view").classList.remove("hidden");
    });
    const rst = card.querySelector(".hist-restore");
    if (rst) rst.addEventListener("click", async () => {
      if (!confirm("把该版本还原为当前立绘？（当前立绘会自动归档保留）")) return;
      try {
        await api(`/api/game/${S.sid}/portrait-restore`, {
          method: "POST",
          body: JSON.stringify({ label: rst.dataset.label, file: rst.dataset.file }),
        });
        toast("已还原立绘");
        await reloadAssets();
        phistPage = 1;
        await openProtHist();
      } catch (e2) { toast("还原失败：" + e2.message); }
    });
    // 「立刻使用此立绘」：还原 + 该角色固定显示此版本（无视当前阶段/表情/服装）
    // 「删除此立绘」（1.7.38/1.7.39）：当前版删除后按阶段自动重绘
    const delCard = card.querySelector(".hist-del");
    if (delCard) delCard.addEventListener("click", async () => {
      if (!confirm(`删除这张立绘？（文件删除、不再被任何调用）
${delCard.dataset.file.startsWith("versions/") || !delCard.closest(".cg-item.cur")
    ? ""
    : "删除的是当前使用版——将按当前阶段/表情自动重绘一张。" }
此操作不可撤销。`)) return;
      try {
        const r = await api(`/api/game/${S.sid}/portrait-delete`, {
          method: "POST",
          body: JSON.stringify({ label: delCard.dataset.label, file: delCard.dataset.file }),
        });
        toast(r.regenerating
          ? `已删除，正在按阶段重绘：${r.regenerating}`
          : `已删除立绘：${r.label}（不再被调用）`, 3200);
        await reloadAssets();
        phistPage = 1;
        await openProtHist();
        if (r.regenerating) startPolling();
      } catch (e) {
        toast("删除失败：" + e.message);
      }
    });
    const use = card.querySelector(".hist-use");
    if (use) use.addEventListener("click", async () => {
      if (!confirm("立刻使用这张立绘？该角色会固定显示此版本，直到重新生成立绘或取消固定。")) return;
      try {
        await api(`/api/game/${S.sid}/portrait-restore`, {
          method: "POST",
          body: JSON.stringify({ label: use.dataset.label, file: use.dataset.file, pin: true }),
        });
        toast("已立刻使用该立绘（固定显示，取消方式见历程顶部）", 3200);
        await reloadAssets();      // 先刷新状态（含 pinned），再重开历程渲染固定栏
        phistPage = 1;
        await openProtHist();
      } catch (e3) { toast("立刻使用失败：" + e3.message); }
    });
    // 1.7.24「替换为此立绘」：标「当前」的版本也能一键设为实际显示（直接固定，
    // 不动文件）——解决「识别为当前但实际未生效」的情形
    const pincur = card.querySelector(".hist-pincur");
    if (pincur) pincur.addEventListener("click", async () => {
      try {
        await api(`/api/game/${S.sid}/portrait-pin`, {
          method: "POST",
          body: JSON.stringify({ label: pincur.dataset.label }),
        });
        toast("已替换为该立绘（固定显示，取消方式见历程顶部）", 3000);
        await reloadAssets();
        phistPage = 1;
        await openProtHist();
      } catch (e4) { toast("替换失败：" + e4.message); }
    });
    grid.appendChild(card);
  });
}

/* 历程顶部「固定立绘」提示 + 取消固定按钮 */
function renderPinBar() {
  const bar = $("prot-hist-pinbar");
  if (!bar) return;
  const pins = S.pinned || {};
  const names = Object.keys(pins).filter((n) => pins[n]);
  if (!names.length) {
    bar.innerHTML = "";
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  bar.innerHTML = names.map((n) => `
    <span class="pin-chip">🧷 ${escapeHtml(n)} 固定为：${escapeHtml(pins[n])}
      <button class="btn mini ghost pin-unpin" data-name="${escapeHtml(n)}">取消固定</button>
    </span>`).join("");
  bar.querySelectorAll(".pin-unpin").forEach((b) => b.addEventListener("click", async () => {
    try {
      await api(`/api/game/${S.sid}/portrait-unpin`, {
        method: "POST", body: JSON.stringify({ name: b.dataset.name }),
      });
      delete S.pinned[b.dataset.name];
      toast("已取消固定，恢复按阶段/表情自动选择");
      phistPage = 1;
      openProtHist();
    } catch (e) { toast("取消固定失败：" + e.message); }
  }));
}

async function openProtHist() {
  try {
    const data = await api(`/api/game/${S.sid}/portrait-history`);
    phistEntries = data.entries || [];
    phistPage = 1;
    renderHistPage(data.protagonist);
    $("modal-prot-hist").classList.remove("hidden");
  } catch (e) {
    toast("读取立绘历程失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart25（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart25() {
  $("btn-prot-hist").addEventListener("click", openProtHist);
  $("btn-vn-prot-hist").addEventListener("click", openProtHist);
  $("btn-prot-hist-close").addEventListener("click", () =>
    $("modal-prot-hist").classList.add("hidden"));
  $("btn-phist-prev").addEventListener("click", () => {
    phistPage -= 1;
    renderHistPage(S.protName);
  });
  $("btn-phist-next").addEventListener("click", () => {
    phistPage += 1;
    renderHistPage(S.protName);
  });
}

async function reloadAssets() {
  if (!S.sid) return;
  try {
    const r = await api(`/api/game/${S.sid}/reload-assets`, { method: "POST" });
    S.imgVersion = (S.imgVersion || 0) + 1;
    const state = await api(`/api/game/${S.sid}`);
    S.assets = state.assets;
    S.bgMap = state.bg_map || S.bgMap;
    playTurn(state, { instant: true });
    toast(r.fixed ? `已重新读取立绘（修复 ${r.fixed} 处引用）` : "立绘引用全部有效，显示已刷新");
  } catch (e) {
    toast("重新读取失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart26（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart26() {
  $("btn-reload-assets").addEventListener("click", reloadAssets);
  $("btn-vn-reload-assets").addEventListener("click", reloadAssets);
}

/* ---------- 立绘与背景 ---------- */

function portraitMap() {
  // assets.portraits: key(hash) -> {label: "名字·表情" / "名字·衣着·表情"(三维差分) / "名字·衣着"(TSF主角换装), file, status}
  const map = {};
  const ports = (S.assets && S.assets.portraits) || {};
  Object.values(ports).forEach((p) => {
    if (p.status === "ready" && p.label) {
      const i1 = p.label.indexOf("·");
      if (i1 <= 0) return;
      const name = p.label.slice(0, i1);
      const tail = p.label.slice(i1 + 1);
      const i2 = tail.lastIndexOf("·");
      if (i2 > 0) {
        map[`${name}|${tail.slice(0, i2)}|${tail.slice(i2 + 1)}`] =
          `/img/${p.file}?v=${S.imgVersion || 0}`;
      } else {
        map[`${name}|${tail}`] = `/img/${p.file}?v=${S.imgVersion || 0}`;
      }
    }
  });
  return map;
}

function updateSprites(speaker, emotion) {
  const urls = portraitMap();
  // 以「精确标签」为键的映射（供立刻使用/固定立绘按 label 直接命中）
  const byLabel = {};
  const ports = (S.assets && S.assets.portraits) || {};
  Object.values(ports).forEach((p) => {
    if (p.status === "ready" && p.label) {
      byLabel[p.label] = `/img/${p.file}?v=${S.imgVersion || 0}`;
    }
  });
  const stage = S.tsf ? S.tsf.stage : 0;
  document.querySelectorAll(".sprite").forEach((div) => {
    const name = div.dataset.name;
    const isProt = div.dataset.kind === "prot";
    const isSpeaker = name === speaker;
    div.classList.toggle("speaking", isSpeaker);
    div.classList.toggle("dimmed", !!speaker && speaker !== "旁白" && !isSpeaker);
    let url;
    // 「立刻使用此立绘」：该角色固定显示指定版本，无视当前阶段/表情/服装
    const pinLabel = (S.pinned || {})[name];
    if (pinLabel && byLabel[pinLabel]) {
      url = byLabel[pinLabel];
    } else {
      url = pickSpriteByState(urls, name, isProt, stage, emotion);
    }
    if (url) setSpriteImage(div, url);
  });
}

function pickSpriteByState(urls, name, isProt, stage, emotion) {
  const outfit = (S.outfits || {})[name];
  let url;
  if (isProt && (S.mode === "tsf" || S.mode === "coc_tsf")) {
    // 主角（TSF / C×T4 的 CoC×TSF）：优先当前换装差分 → 阶段立绘 → 回退已有阶段
    // coc_tsf 的 stage 由**服务端**给出（`state.tsf.stage` ← t54 的接口），前端不重算。
    url = outfit ? urls[`${name}|${outfit}`] : null;
    if (!url) {
      url = urls[`${name}|阶段${stage + 1}`];
      if (!url) {
        for (let s = stage; s >= 0; s--) {
          if (urls[`${name}|阶段${s + 1}`]) { url = urls[`${name}|阶段${s + 1}`]; break; }
        }
      }
    }
  } else {
    // NPC / 普通模式主角：当前衣着下按表情差分调取（换装后每个表情
    // 用自己的衣着版立绘，不再跳回默认衣）；无换装走默认表情集
    if (outfit) {
      url = urls[`${name}|${outfit}|${emotion}`]
        || urls[`${name}|${outfit}|neutral`]
        || urls[`${name}|${outfit}`];
    }
    if (!url) url = urls[`${name}|${emotion}`] || urls[`${name}|neutral`];
  }
  return url;
}

/* 统一立绘显示大小：基准高度为主 + 按身形 ±8% 微调。
   crowd（≥5 人）模式：全部同高且略小，保证同屏整整齐齐。 */
function applySpriteHeight(img) {
  // 大立绘布局由 style.css 控制：头顶完整（距顶约3vh）、腿部由画面底部裁切、
  // 横向站位由 buildSprites 决定——此处不做任何位置/高度计算
  img.style.height = "";
  img.style.width = "auto";
  img.style.objectFit = "cover";
  img.style.objectPosition = "center top";
}

function setSpriteImage(div, url) {
  const img = div.querySelector("img");
  // 同一 URL 已成功显示才跳过；若之前加载失败/没挂上 .show，允许重试，
  // 否则「已生成但舞台没换」会一直卡在旧图。
  if (img.dataset.url === url && img.classList.contains("show")) return;
  img.dataset.url = url;
  img.onload = () => {
    applySpriteHeight(img);
    img.classList.add("show");
    const ph = div.querySelector(".sprite-placeholder");
    if (ph) ph.remove();
  };
  img.src = url;
}

let bgSeq = 0;
function applyBackground() {
  const bgId = S.turn.background_id;
  if (!bgId || bgId === S.currentBgId) return;
  const key = S.bgMap[bgId];
  const entry = key && S.assets.backgrounds[key];
  if (entry && entry.status === "ready") {
    S.currentBgId = bgId;
    const layer = $("bg-layer");
    const seq = ++bgSeq;
    const img = document.createElement("img");
    img.src = `/img/${entry.file}`;
    img.onerror = () => {
      // 加载失败：允许下次轮询重试，并清掉这张空图
      if (seq === bgSeq) S.currentBgId = null;
      img.remove();
    };
    img.onload = () => {
      // 过期任务：切换期间背景又变了，直接丢弃（防止旧背景顶掉新背景）
      if (seq !== bgSeq) { img.remove(); return; }
      // F1（t60）：**交叉淡入** —— 新图淡入期间旧图必须留着；新图完全可见后才移除旧图。
      // 修的是什么（实测定位，不是猜测）：原实现里 `old.classList.remove("show")` 之后，
      // 紧跟的"清理从未显示过的旧图"会把**刚刚被摘掉 .show 的旧图在同一帧 remove()**，
      // 于是那句 `setTimeout(() => old.remove(), 1100)` 形同虚设 —— 旧图立刻消失，
      // 而新图还要 1s 才淡入到 1 ⇒ 这 1s 里 `#bg-layer` 只剩渐变底色透出，
      // 亮图→暗底→亮图 = 用户报的「加载下一幕黑白闪烁」。
      // 采样证据（headless Edge + CDP，rAF 逐帧 + MutationObserver）：
      //   修前 6 次换背景 → 6 个「0 张完全可见背景图」窗口，各 ~950ms，remove 6；
      //   修后同法 → 0 个窗口（详见 FLICKER-PIXEL-FIX.md）。
      img.classList.add("show");
      const outgoing = [...layer.querySelectorAll("img.show")].filter((o) => o !== img);
      outgoing.forEach((old) => {
        old.classList.add("bg-out");   // 标记"待移除"：不让下面的清理提前摘掉它
        let dropped = false;
        const drop = () => { if (!dropped) { dropped = true; old.remove(); } };
        // 新图 opacity→1 的过渡一结束就移除旧图；transitionend 不触发时（标签页隐藏等）超时兜底
        img.addEventListener("transitionend", drop, { once: true });
        setTimeout(drop, 1400);
      });
      // 只清理**从未显示过**的陈旧图（加载中被顶掉的那种）；待移除的旧图保留到淡入完成
      layer.querySelectorAll("img:not(.show):not(.bg-out)").forEach((stale) => {
        if (stale !== img) stale.remove();
      });
    };
    layer.appendChild(img);
  }
}

/* 轮询补 CG：进入该幕时 CG 可能还在生成（cg_url 为空），生成完成后
   playTurn 不会再跑，这里按 /assets 返回的 cg_url 把图补进全屏层。 */
function applyCgFromPoll(data) {
  if (!data || !data.cg_active || !data.cg_url) return;
  const layer = document.getElementById("cg-layer");
  if (!layer) return;
  layer.classList.remove("hidden");
  let img = layer.querySelector("img");
  if (!img) {
    img = document.createElement("img");
    img.onload = () => img.classList.add("show");
    layer.insertBefore(img, layer.firstChild);
  }
  if (img.getAttribute("src") !== data.cg_url) {
    img.classList.remove("show");
    img.src = data.cg_url;
  }
  if (!layer.querySelector(".cg-title")) {
    const title = document.createElement("div");
    title.className = "cg-title";
    title.textContent = data.cg_title || "CG";
    layer.appendChild(title);
  }
  layer.dataset.key = data.cg_key || "";
  S.cgActive = true;
  const sprites = document.getElementById("sprite-layer");
  if (sprites) sprites.style.visibility = "hidden";
}

/* ---------- 资源轮询与生成提示 ---------- */

function hasPending() {
  const groups = (S.assets && [S.assets.portraits, S.assets.backgrounds]) || [];
  return groups.some((g) => Object.values(g || {}).some((p) => p.status === "pending"));
}

function updateGenBanner() {
  const pendingList = Object.values((S.assets && S.assets.portraits) || {})
    .filter((p) => p.status === "pending");
  const banner = $("gen-banner");
  banner.classList.toggle("hidden", !pendingList.length);
  const txt = $("gen-banner-text");
  if (txt && pendingList.length) {
    // 1.8.0：给出「还有几张 + 当前在画谁」的可读进度，而不是一句固定文案
    const label = (pendingList[0] && pendingList[0].label) || "";
    txt.textContent = `🎨 正在绘制 ${pendingList.length} 张立绘${label ? " · " + label : ""}…`;
  }
  if (window.UI2) UI2.syncArtStatus();
}

function startPolling() {
  // t38：单一调度器任务（in-flight 守卫保证同一时刻只有一个资产请求在飞）
  if (!Sched.isRunning("assets")) {
    Sched.define("assets", 2000, async () => {
      if (!S.sid) { stopPolling(); return; }
      try {
        // 先拉取再决定是否停止：避免「最后一张就绪的资产永远没被刷到」
        const data = await api(`/api/game/${S.sid}/assets`);
        S.assets = data.assets;
        S.bgMap = data.bg_map || S.bgMap;
        const line = S.turn && S.turn.dialogue[S.lineIdx];
        if (line) updateSprites(line.character, line.emotion);
        applyBackground();
        applyCgFromPoll(data);
        const still = hasPending();
        updateGenBanner();
        if (window.UI2) UI2.syncArtStatus();
        if (!still) stopPolling();
      } catch { /* 会话失效等，静默 */ }
    });
  }
  Sched.start("assets");
  updateGenBanner();
}

function stopPolling() {
  Sched.stop("assets");
  S.pollTimer = null;   // 兼容既有读取点
}

/* ---------- 抠图中心：按角色重抠 + 多方案对比 ---------- */

function openCutCenter() {
  const sel = $("cut-char");
  sel.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部角色";
  sel.appendChild(all);
  const prot = document.createElement("option");
  prot.value = S.protName;
  prot.textContent = S.protName + "（主角）";
  sel.appendChild(prot);
  (S.npcs || []).forEach((n) => {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  });
  $("cut-hint").textContent = "";
  $("cut-preview-grid").innerHTML = "";
  $("modal-cut").classList.remove("hidden");
}

function cutTarget() {
  return $("cut-char").value;
}
// —— UI 绑定段 bindUiPart27（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart27() {

  $("btn-recut").addEventListener("click", openCutCenter);
  $("btn-vn-recut").addEventListener("click", openCutCenter);
  $("btn-cut-close").addEventListener("click", () => $("modal-cut").classList.add("hidden"));

  $("btn-cut-recut").addEventListener("click", async () => {
    const c = cutTarget();
    try {
      const state = await api(`/api/game/${S.sid}/recut`, {
        method: "POST", body: JSON.stringify({ character: c }),
      });
      S.assets = state.assets;
      forceReloadSprites();
      $("cut-hint").textContent = "✓ 已按当前抠图标准重新抠图";
      toast("重新抠图完成，立绘已更新");
    } catch (e) {
      toast("重新抠图失败：" + e.message);
    }
  });

  $("btn-cut-previews").addEventListener("click", async () => {
    const c = cutTarget();
    if (!c) { toast("请先选择目标角色（预览对比需要指定角色）"); return; }
    $("btn-cut-previews").disabled = true;
    $("cut-hint").textContent = "正在生成四种方案……（AI 智能首次需加载模型，稍等）";
    try {
      const data = await api(`/api/game/${S.sid}/cutpreviews`, {
        method: "POST", body: JSON.stringify({ character: c }),
      });
      const grid = $("cut-preview-grid");
      grid.innerHTML = "";
      data.previews.forEach((p) => {
        const card = document.createElement("div");
        card.className = "cg-item";
        card.innerHTML = `
          <img src="/img/${escapeHtml(p.file)}?v=${Date.now()}" alt="${escapeHtml(p.name)}">
          <div class="cg-caption">${escapeHtml(p.name)}</div>
          <button class="btn mini ghost cut-use">采用此方案</button>`;
        card.querySelector(".cut-use").addEventListener("click", async () => {
          if (!confirm(`确定把「${p.name}」方案应用到 ${c} 的全部立绘？`)) return;
          try {
            await api(`/api/game/${S.sid}/cutapply`, {
              method: "POST",
              body: JSON.stringify({ character: c, mode: p.mode }),
            });
            const state = await api(`/api/game/${S.sid}`);
            S.assets = state.assets;
            forceReloadSprites();
            $("cut-hint").textContent = `✓ 已应用「${p.name}」方案`;
            toast("抠图方案已应用");
          } catch (e) {
            toast("应用失败：" + e.message);
          }
        });
        grid.appendChild(card);
      });
      $("cut-hint").textContent = "对比后点「采用此方案」应用到该角色全部立绘";
    } catch (e) {
      $("cut-hint").textContent = "生成失败：" + e.message;
    } finally {
      $("btn-cut-previews").disabled = false;
    }
  });
}

/* ---------- 指令注入窗口 ---------- */

function renderDirectiveRows(list) {
  const box = $("directive-list");
  box.innerHTML = "";
  const groups = {};
  list.forEach((d) => {
    (groups[d.group || (d.preset ? "其他" : "自定义")] =
      groups[d.group || (d.preset ? "其他" : "自定义")] || []).push(d);
  });
  Object.entries(groups).forEach(([g, items]) => {
    const head = document.createElement("div");
    head.className = "preset-group-name";
    head.textContent = g;
    box.appendChild(head);
    items.forEach((d) => box.appendChild(directiveRow(d, list)));
  });
  updateDirectiveCount(list);
  return list;
}

function directiveRow(d, list) {
  const row = document.createElement("label");
  row.className = "directive-row" + (d.enabled ? " on" : "");
  row.innerHTML = `
    <input type="checkbox" ${d.enabled ? "checked" : ""}>
    <span class="directive-text">${escapeHtml(d.text)}${d.preset ? "" : ' <em class="directive-custom">自定义</em>'}</span>
    ${d.preset ? "" : `<button class="btn mini ghost directive-del" title="删除">✕</button>`}`;
  const cb = row.querySelector("input");
  cb.addEventListener("change", () => {
    d.enabled = cb.checked;
    row.classList.toggle("on", cb.checked);
    updateDirectiveCount(list);
  });
  const del = row.querySelector(".directive-del");
  if (del) del.addEventListener("click", (e) => {
    e.preventDefault();
    const i = list.indexOf(d);
    if (i >= 0) list.splice(i, 1);
    renderDirectiveRows(list);
  });
  return row;
}

function updateDirectiveCount(list) {
  const n = list.filter((d) => d.enabled).length;
  $("directives-hint").textContent = n ? `已选 ${n} 条，保存后下一幕生效` : "";
}

/* ---------- 单一轮询调度器（t38 · 审计 3.1 / M-08） ----------
   收敛前全站有 4 个各自为政的 setInterval 源（updateTimer / S.pollTimer /
   cgRefreshTimer / ffTimer），任一被漏清就会一直发请求（B-21 的机制）。
   现在全站只有一个定时器源：Sched 的 tick。每个任务是"注册一次、显式启停"，
   并带 in-flight 守卫 —— 上一轮没结束时不再发起下一轮，因此**同一任务的在飞
   请求数恒 ≤1**（限速下也成立，这正是验收判据）。 */
const Sched = (function () {
  const TICK_MS = 100;
  const tasks = new Map();
  let handle = null;

  function tick() {
    const now = Date.now();
    tasks.forEach(function (t) {
      if (!t.running || t.inflight) return;      // ← in-flight 守卫：并发 ≤1
      if (now - t.last < t.periodMs) return;
      t.last = now;
      t.inflight = true;
      Promise.resolve()
        .then(function () { return t.fn(); })
        .catch(function (err) { console.error("[sched] task " + t.name + " failed:", err); })
        .finally(function () { t.inflight = false; });
    });
  }
  function ensure() { if (handle === null) handle = setInterval(tick, TICK_MS); }
  function maybeStop() {
    let any = false;
    tasks.forEach(function (t) { if (t.running) any = true; });
    if (handle !== null && !any) { clearInterval(handle); handle = null; }
  }
  return {
    define: function (name, periodMs, fn) {
      tasks.set(name, { name: name, periodMs: periodMs, fn: fn, last: 0, inflight: false, running: false });
      return name;
    },
    start: function (name) {
      const t = tasks.get(name);
      if (!t || t.running) return;               // 幂等：已在跑就不重复起
      t.running = true; t.last = 0; ensure();
    },
    stop: function (name) {
      const t = tasks.get(name);
      if (t) { t.running = false; t.inflight = false; }
      maybeStop();
    },
    stopAll: function () { tasks.forEach(function (t) { t.running = false; t.inflight = false; }); maybeStop(); },
    isRunning: function (name) { const t = tasks.get(name); return !!(t && t.running); },
    isInflight: function (name) { const t = tasks.get(name); return !!(t && t.inflight); },
    running: function () { const r = []; tasks.forEach(function (t) { if (t.running) r.push(t.name); }); return r; },
    debug: function () {
      const out = { tickMs: TICK_MS, timerSourceActive: handle !== null, tasks: [] };
      tasks.forEach(function (t) {
        out.tasks.push({ name: t.name, periodMs: t.periodMs, running: t.running, inflight: t.inflight });
      });
      return out;
    },
  };
})();
window.Sched = Sched;   // 供验收探针读取（只读接口，不参与业务逻辑）

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

async function openDirectives() {
  try {
    const data = await api(`/api/game/${S.sid}/directives`);
    const list = renderDirectiveRows(data.directives);
    $("modal-directives").classList.remove("hidden");
    const save = $("btn-directives-save");
    save.onclick = async () => {
      save.disabled = true;
      try {
        await api(`/api/game/${S.sid}/directives`, {
          method: "POST", body: JSON.stringify({ directives: list }),
        });
        $("modal-directives").classList.add("hidden");
        toast("指令已保存，下一幕生效");
      } catch (e) {
        toast("保存失败：" + e.message);
      } finally {
        save.disabled = false;
      }
    };
    const add = $("btn-add-directive");
    add.onclick = () => {
      const text = $("directive-new").value.trim();
      if (!text) { toast("请输入指令内容"); return; }
      list.push({ id: "", text, enabled: true, preset: false });
      $("directive-new").value = "";
      renderDirectiveRows(list);
    };
  } catch (e) {
    toast("读取指令失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart28（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart28() {

  $("btn-directives").addEventListener("click", openDirectives);
  $("btn-directives-close").addEventListener("click", () =>
    $("modal-directives").classList.add("hidden"));
}


/* ---------- 命令 / 换装 / CG 回廊 ---------- */

const CMD_ACTIONS = [
  { key: "outfit_casual", label: "更衣 · 便服", group: "更衣", adultOnly: false },
  { key: "outfit_underwear", label: "更衣 · 内衣", group: "更衣", adultOnly: true },
  { key: "outfit_nude", label: "更衣 · 裸体", group: "更衣", adultOnly: true },
  { key: "outfit_default", label: "换回默认", group: "更衣", adultOnly: false },
  { key: "kiss", label: "强吻", group: "强制行为", forced: true },
  { key: "hug", label: "强行拥抱", group: "强制行为", forced: true },
  { key: "sex", label: "侵犯", group: "强制行为", forced: true },
  { key: "blowjob", label: "口交", group: "强制行为", forced: true },
];

let cmdTarget = null;   // 命令面板当前目标角色

function castNames() {
  return [{ name: S.protName, isProt: true },
          ...(S.npcs || []).map((n) => ({ name: n, isProt: false }))];
}

function openCommand() {
  cmdTarget = null;
  const row = $("cmd-char-row");
  row.innerHTML = "";
  castNames().forEach((c) => {
    const btn = document.createElement("button");
    btn.className = "btn ghost cmd-char" + (c.isProt ? " prot" : "");
    btn.textContent = c.name;
    btn.addEventListener("click", () => {
      cmdTarget = c.name;
      row.querySelectorAll(".cmd-char").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      renderCommandActions();
    });
    row.appendChild(btn);
  });
  $("cmd-hint").textContent = "";
  $("cmd-actions").innerHTML = '<p class="hint-line">先选择目标角色</p>';
  $("modal-command").classList.remove("hidden");
}

function renderCommandActions() {
  const box = $("cmd-actions");
  box.innerHTML = "";
  if (!cmdTarget) return;
  const groups = {};
  CMD_ACTIONS.forEach((a) => {
    if (a.adultOnly && !S.r18) return;
    if (a.forced && !(S.r18 && S.forced)) return;
    (groups[a.group] = groups[a.group] || []).push(a);
  });
  Object.entries(groups).forEach(([g, items]) => {
    const head = document.createElement("div");
    head.className = "preset-group-name";
    head.textContent = g + "（自动收藏为 CG）";
    box.appendChild(head);
    items.forEach((a) => {
      const btn = document.createElement("button");
      btn.className = "btn ghost action-btn";
      btn.textContent = a.label;
      btn.addEventListener("click", () => runCommand(a));
      box.appendChild(btn);
    });
  });
}

async function runCommand(action) {
  if (!S.sid || !cmdTarget) return;
  $("modal-command").classList.add("hidden");
  $("choices-wrap").classList.add("hidden");
  $("loading-overlay").classList.remove("hidden");
  $("loading-text").textContent = "命令执行中……";
  try {
    const state = await api(`/api/game/${S.sid}/command`, {
      method: "POST",
      body: JSON.stringify({ character: cmdTarget, action: action.key }),
    });
    S.assets = state.assets;
    S.bgMap = state.bg_map || S.bgMap;
    S.outfits = state.outfits || S.outfits;
    $("loading-overlay").classList.add("hidden");
    playTurn(state, { instant: false });
    toast(`⚡ ${action.label}：场面已收藏到 CG 回廊`);
  } catch (e) {
    $("loading-overlay").classList.add("hidden");
    toast("命令失败：" + e.message);
    $("choices-wrap").classList.remove("hidden");
  }
}
// —— UI 绑定段 bindUiPart29（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart29() {

  $("btn-command").addEventListener("click", openCommand);
  $("btn-command-close").addEventListener("click", () =>
    $("modal-command").classList.add("hidden"));
}

/* ---- 换装面板 ---- */

const OUTFIT_PRESETS = [
  { key: "default", label: "默认着装" },
  { key: "casual", label: "便服" },
  { key: "underwear", label: "内衣", adultOnly: true },
  { key: "nude", label: "裸体", adultOnly: true },
];

let pendingOutfit = "";

function outfitDisplayName(v) {
  const p = OUTFIT_PRESETS.find((x) => x.key === v);
  return p ? p.label : v;
}

function updateOutfitHint() {
  const character = $("outfit-char").value;
  const cur = (S.outfits || {})[character];
  $("outfit-hint").textContent = pendingOutfit
    ? `已选定「${outfitDisplayName(pendingOutfit)}」，点「✔ 确定换装」后立即生效`
    : (cur ? `当前着装：${cur}（未改动）` : "当前为初始着装（未改动）");
}

function openOutfit() {
  const sel = $("outfit-char");
  sel.innerHTML = "";
  castNames().forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c.name;
    opt.textContent = c.isProt ? c.name + "（主角）" : c.name;
    sel.appendChild(opt);
  });
  const box = $("outfit-presets");
  box.innerHTML = "";
  pendingOutfit = "";
  $("outfit-preset").value = "";
  OUTFIT_PRESETS.forEach((p) => {
    if (p.adultOnly && !S.r18) return;
    const btn = document.createElement("button");
    btn.className = "btn ghost action-btn outfit-preset-btn";
    btn.textContent = p.label;
    btn.addEventListener("click", () => {
      pendingOutfit = p.key;
      $("outfit-preset").value = "";
      document.querySelectorAll(".outfit-preset-btn").forEach((b) =>
        b.classList.toggle("sel", b === btn));
      updateOutfitHint();
    });
    box.appendChild(btn);
  });
  updateOutfitHint();
  $("modal-outfit").classList.remove("hidden");
}

async function applyOutfit(fixed) {
  if (!S.sid) return;
  const character = $("outfit-char").value;
  const outfit = fixed || $("outfit-preset").value.trim();
  if (!outfit) { toast("请选择服装或输入自定义服饰"); return; }
  $("modal-outfit").classList.add("hidden");
  try {
    const state = await api(`/api/game/${S.sid}/outfit`, {
      method: "POST",
      body: JSON.stringify({ character, outfit }),
    });
    S.assets = state.assets;
    S.bgMap = state.bg_map || S.bgMap;
    S.outfits = state.outfits || S.outfits;
    pendingOutfit = "";
    playTurn(state, { instant: true });
    toast(`👗 ${character} 已更换为「${outfit}」，立绘重新生成中`);
  } catch (e) {
    toast("换装失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart30（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart30() {

  $("btn-outfit").addEventListener("click", openOutfit);
  $("btn-outfit-apply").addEventListener("click", () => {
    const v = pendingOutfit || $("outfit-preset").value.trim();
    if (!v) { toast("请先选择服装或输入自定义服饰"); return; }
    applyOutfit(v);
  });
  $("outfit-preset").addEventListener("input", () => {
    pendingOutfit = $("outfit-preset").value.trim();
    document.querySelectorAll(".outfit-preset-btn").forEach((b) =>
      b.classList.remove("sel"));
    updateOutfitHint();
  });
  $("outfit-char").addEventListener("change", updateOutfitHint);
  $("btn-outfit-close").addEventListener("click", () =>
    $("modal-outfit").classList.add("hidden"));
}

/* ---- CG 回廊 ---- */

async function saveCg(type, note) {
  if (!S.sid) return null;
  try {
    const r = await api(`/api/game/${S.sid}/cg`, {
      method: "POST",
      body: JSON.stringify({ type, note }),
    });
    if (!r.already) toast(`🎨 已收藏：${r.scene || r.note || "CG"}`);
    refreshCgList();
    return r;
  } catch (e) {
    toast("CG 收藏失败：" + e.message);
    return null;
  }
}
// —— UI 绑定段 bindUiPart31（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart31() {

  $("btn-save-cg").addEventListener("click", () => saveCg("manual", ""));
  /* 顶栏「刷新CG」：重新生成当前幕桥段 CG（当前幕无桥段 CG 时给出提示） */
  $("btn-cg-refresh").addEventListener("click", async () => {
    if (!S.sid) return;
    try {
      const r = await api(`/api/game/${S.sid}/cg-regen`, {
        method: "POST",
        body: JSON.stringify({ cg_id: "" }),
      });
      toast(`正在刷新 CG：${r.title}（新图生成中，旧图保留）`, 3200);
      S.imgVersion = (S.imgVersion || 0) + 1;
      startPolling();
    } catch (e) {
      toast("刷新失败：" + e.message);
    }
  });
}

let cgRefreshTimer = null;

function openCgGallery() {
  $("modal-cg").classList.remove("hidden");
  refreshCgList();
  // 生成中的桥段 CG 就绪后刷新展示（t38：收敛到单一调度器）
  Sched.define("cgRefresh", 2500, refreshCgList);
  Sched.start("cgRefresh");
}

function closeCgGallery() {
  Sched.stop("cgRefresh");
  cgRefreshTimer = null;   // 兼容既有读取点
  $("modal-cg").classList.add("hidden");
}

async function refreshCgList() {
  const list = $("cg-list");
  if (!list) return;
  try {
    const data = await api("/api/cg");
    $("cg-hint").textContent = data.items.length
      ? `共 ${data.items.length} 张`
      : "";
    list.innerHTML = "";
    if (!data.items.length) {
      list.innerHTML = '<p class="hint-line">还没有 CG。剧情关键桥段、命令场面会自动收藏，也可点顶栏「📷 存为CG」。</p>';
      return;
    }
    data.items.forEach((it) => {
      const card = document.createElement("div");
      card.className = "cg-item" + (it.ready ? "" : " pending");
      const img = it.ready
        ? `<img src="/cg/${escapeHtml(it.file)}?v=${Date.now()}" alt="CG" loading="lazy">`
        : (it.file_lost
           ? '<div class="cg-pending lost" title="旧版本只登记了引用、没有把图片落盘，缓存已清理，无法找回；删掉这条即可">源图已丢失</div>'
           : '<div class="cg-pending">生成中…</div>');
      card.innerHTML = `
        ${img}
        <div class="cg-caption">${escapeHtml(it.scene || it.note || "CG")}
          <span class="cg-type">${escapeHtml(it.type)}</span></div>
        ${it.type === "bridge" ? `<button class="btn mini ghost cg-regen" title="按已记录的画面描述重新画这张桥段 CG（旧图保留）">🔄 重绘</button>` : ""}
        <button class="btn mini ghost cg-del" title="删除">✕</button>`;
      if (it.ready) {
        card.querySelector("img").addEventListener("click", () => {
          $("cg-view-img").src = `/cg/${escapeHtml(it.file)}?v=${Date.now()}`;
          $("cg-view-title").textContent = it.scene + (it.note ? " · " + it.note : "");
          S.cgView = null;
          if ($("btn-cg-regen-diff")) $("btn-cg-regen-diff").classList.add("hidden");
          if ($("btn-cg-set-current")) $("btn-cg-set-current").classList.add("hidden");
          $("modal-cg-view").classList.remove("hidden");
        });
      }
      const regen = card.querySelector(".cg-regen");
      if (regen) regen.addEventListener("click", async () => {
        try {
          const r = await api(`/api/game/${S.sid}/cg-regen`, {
            method: "POST",
            body: JSON.stringify({ cg_id: it.id }),
          });
          toast(`正在重绘 CG：${r.title}（新图生成中，旧图保留）`, 3200);
          regen.disabled = true;
          if (S.sid) { S.imgVersion = (S.imgVersion || 0) + 1; startPolling(); }
        } catch (e) {
          toast("重绘失败：" + e.message);
        }
      });
      card.querySelector(".cg-del").addEventListener("click", async () => {
        if (!confirm("删除这张 CG？")) return;
        try {
          await api(`/api/cg/${it.id}`, { method: "DELETE" });
          refreshCgList();
        } catch (e) {
          toast("删除失败：" + e.message);
        }
      });
      list.appendChild(card);
    });
  } catch {
    list.innerHTML = '<p class="hint-line">读取 CG 列表失败</p>';
  }
}
// —— UI 绑定段 bindUiPart32（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart32() {

  $("btn-cg").addEventListener("click", openCgGallery);
  $("btn-cg-close").addEventListener("click", closeCgGallery);
  $("btn-cg-view-close").addEventListener("click", () =>
    $("modal-cg-view").classList.add("hidden"));

  /* 立绘历程「删除此立绘」（1.7.38）：移除历史+删除文件，不再被系统调用 */
  $("btn-cg-del-hist").addEventListener("click", async () => {
    const v = S.cgView;
    if (!v || !v.label || !v.file || !S.sid) return;
    if (!confirm(`删除这张立绘？\n\n文件将删除、并从历程移除——之后不会出现在「还原/立刻使用/参考底图」等任何调用中。
  ${v.current ? "删除的是当前使用版——将按当前阶段/表情自动重绘一张。" : ""}
  此操作不可撤销。`)) return;
    try {
      const r = await api(`/api/game/${S.sid}/portrait-delete`, {
        method: "POST",
        body: JSON.stringify({ label: v.label, file: v.file }),
      });
      toast(r.regenerating
        ? `已删除，正在按阶段重绘：${r.regenerating}`
        : `已删除立绘：${r.label}（不再被调用）`, 3200);
      $("modal-cg-view").classList.add("hidden");
      await reloadAssets();
      phistPage = 1;
      await openProtHist();
    } catch (e) {
      toast("删除失败：" + e.message);
    }
  });

  /* 立绘历程「重绘差分」：以当前查看的立绘为参照重画该表情（只变表情） */
  $("btn-cg-regen-diff").addEventListener("click", async () => {
    const label = (S.cgView && S.cgView.label) || "";
    if (!label || !S.sid) return;
    try {
      $("btn-cg-regen-diff").disabled = true;
      const r = await api(`/api/game/${S.sid}/regenerate-label`, {
        method: "POST",
        body: JSON.stringify({ label }),
      });
      toast(`正在重绘差分：${r.regenerating}（只变表情，身材/服装/发型保持）`, 3200);
      $("modal-cg-view").classList.add("hidden");
      S.imgVersion = (S.imgVersion || 0) + 1;
      startPolling();   // 轮询资产直到新立绘 ready/error，保证有效性
    } catch (e) {
      toast("重绘差分失败：" + e.message);
    } finally {
      $("btn-cg-regen-diff").disabled = false;
    }
  });

  /* 立绘历程「替换为此立绘」（1.7.24）：大图上任意版本（含标「当前」但未
     真正生效的）一键设为实际显示——旧版=归档替换+固定；当前版=直接固定
     不动文件；固定直到重新生成立绘或取消固定 */
  $("btn-cg-set-current").addEventListener("click", async () => {
    const v = S.cgView;
    if (!v || !v.label || !S.sid) return;
    try {
      if (v.current) {
        await api(`/api/game/${S.sid}/portrait-pin`, {
          method: "POST",
          body: JSON.stringify({ label: v.label }),
        });
      } else {
        await api(`/api/game/${S.sid}/portrait-restore`, {
          method: "POST",
          body: JSON.stringify({ label: v.label, file: v.file, pin: true }),
        });
      }
      toast("已替换为该立绘（固定显示，取消方式见立绘历程顶部）", 3200);
      $("modal-cg-view").classList.add("hidden");
      await reloadAssets();
      phistPage = 1;
      await openProtHist();   // 刷新历程：固定栏与当前标记同步
    } catch (e) {
      toast("替换为此立绘失败：" + e.message);
    }
  });
}

/* ---------- 读取场景（进入/更换/重生成） ---------- */

async function refreshSceneList() {
  const box = $("scene-mgr-list");
  if (!box) return;
  box.innerHTML = "";
  try {
    const data = await api(`/api/game/${S.sid}/scenes`);
    if (!data.scenes.length) {
      box.innerHTML = '<p class="hint-line">暂无已记录场景（随剧情推进自动记录）</p>';
      return;
    }
    data.scenes.forEach((s) => {
      const row = document.createElement("div");
      row.className = "scene-row";
      row.innerHTML = `<b>${escapeHtml(s.name)}</b> 访问 ${s.visits} 次`
        + `<span class="scene-role">${s.bg_file ? "背景已就绪" : (s.bg_file === "" ? "" : "背景生成中")}</span>`
        + `<button class="btn mini ghost scene-enter">🎯 进入</button>`;
      row.querySelector(".scene-enter").addEventListener("click", () =>
        enterScene(s.name));
      box.appendChild(row);
    });
  } catch (e) {
    box.innerHTML = '<p class="hint-line">读取场景清单失败</p>';
  }
}

async function enterScene(name) {
  if (!name || !S.sid) return;
  try {
    const state = await api(`/api/game/${S.sid}/scene-set`, {
      method: "POST",
      body: JSON.stringify({ name, action: "enter" }),
    });
    S.assets = state.assets;
    S.bgMap = state.bg_map || S.bgMap;
    playTurn(state, { instant: true });
    $("scenes-hint").textContent = `✓ 已进入场景「${name}」`;
    refreshSceneList();
  } catch (e) {
    toast("进入场景失败：" + e.message);
  }
}

function openSceneMgr() {
  if (!S.sid) { toast("请先进入对局"); return; }
  $("scenes-hint").textContent = "";
  $("scene-name").value = "";
  $("scenes-current").textContent =
    `当前场景：${S.turn ? S.turn.scene : "—"}`;
  refreshSceneList();
  $("modal-scenes").classList.remove("hidden");
}
// —— UI 绑定段 bindUiPart33（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart33() {
  $("btn-scene-mgr").addEventListener("click", openSceneMgr);
  $("btn-scenes-close").addEventListener("click", () =>
    $("modal-scenes").classList.add("hidden"));
  $("btn-scenes-close2").addEventListener("click", () =>
    $("modal-scenes").classList.add("hidden"));

  $("btn-scene-enter").addEventListener("click", () => {
    const name = $("scene-name").value.trim();
    if (!name) { toast("先输入场景名"); return; }
    enterScene(name);
  });
  $("scene-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("btn-scene-enter").click();
  });
  $("btn-scene-rerender").addEventListener("click", async () => {
    if (!S.sid) return;
    try {
      const state = await api(`/api/game/${S.sid}/scene-set`, {
        method: "POST",
        body: JSON.stringify({ name: "", action: "rerender" }),
      });
      S.assets = state.assets;
      S.bgMap = state.bg_map || S.bgMap;
      S.imgVersion = (S.imgVersion || 0) + 1;
      playTurn(state, { instant: true });
      $("scenes-hint").textContent = "🔄 正在重新生成当前场景背景……";
      startPolling();
      refreshSceneList();
    } catch (e) {
      toast("重生成失败：" + e.message);
    }
  });
}

/* ---------- 快进 ---------- */

let ffTimer = null;
/* t38：快进收敛到单一调度器，并补上它原本完全缺失的清理路径 ——
   收敛前 ffTimer 只在「再次点按钮」或「出现选项」时停，
   切局 / 读档 / Esc 都不会停它，于是快进会在别的界面上继续 nextLine()。 */
function stopFastForward() {
  Sched.stop("fastForward");
  ffTimer = null;
  $("btn-fastforward").textContent = "⏩ 快进";
}
// —— UI 绑定段 bindUiPart34（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart34() {
  Sched.define("fastForward", 160, () => {
    if (!$("choices-wrap").classList.contains("hidden")) {
      stopFastForward();
      return;
    }
    if (S.typing) {
      clearTimeout(S.typeTimer);
      const line = S.turn.dialogue[S.lineIdx];
      $("dialog-text").textContent = line.text;
      S.typing = false;
      $("next-hint").classList.remove("hidden");
    } else {
      nextLine();
    }
  });
  $("btn-fastforward").addEventListener("click", () => {
    if (Sched.isRunning("fastForward")) { stopFastForward(); return; }
    $("btn-fastforward").textContent = "⏸ 快进中";
    Sched.start("fastForward");
  });
}

/* ---------- 存档 / 读档 ---------- */

async function openSlots() {
  $("slots-hint").textContent = "";
  await refreshSlotList();
  $("modal-slots").classList.remove("hidden");
}

async function refreshSlotList() {
  const list = $("slot-list");
  list.innerHTML = "";
  try {
    const data = await api(`/api/slots?t=${Date.now()}`);
    if (!data.slots.length) {
      list.innerHTML = '<p class="hint-line">暂无存档</p>';
      return;
    }
    data.slots.forEach((s) => {
      const row = document.createElement("div");
      row.className = "slot-row";
      row.innerHTML = `
        <span class="slot-info">${escapeHtml(s.name)} ｜ 第 ${s.turn_no} 幕</span>
        <button class="btn mini ghost slot-load">📂 读取</button>
        <button class="btn mini ghost slot-ngp" title="继承角色与场景，剧情/数值从头开始；前情自动压缩">🔁 二周目</button>
        <button class="btn mini ghost slot-del">✕</button>`;
      row.querySelector(".slot-load").addEventListener("click", async () => {
        try {
          const state = await api(`/api/slots/${s.slot_id}/load`,
            { method: "POST" });
          $("modal-slots").classList.add("hidden");
          // 必须走 enterGame，而不能只 playTurn：后者不会更新 S.sid /
          // localStorage / 轮询与状态面板，下一幕会发到旧 sid（乱跳存档）。
          enterGame(state, { instant: true });
          toast("已读取存档");
        } catch (e) {
          toast("读档失败：" + e.message);
        }
      });
      row.querySelector(".slot-ngp").addEventListener("click", async () => {
        try {
          toast("二周目开启中（继承角色与场景，首次约 1-2 分钟）…");
          const state = await api(`/api/game/newgame-plus`,
            { method: "POST", timeoutMs: 300000,
              body: JSON.stringify({ slot_id: s.slot_id, title: (s.name || "") + "·二周目" }) });
          $("modal-slots").classList.add("hidden");
          localStorage.setItem("galgame_sid", state.sid);
          enterGame(state, { instant: true });
          toast("二周目已开启：角色/场景已继承，前情已压缩");
        } catch (e) {
          toast("二周目失败：" + e.message);
        }
      });
      row.querySelector(".slot-del").addEventListener("click", async () => {
        try {
          await api(`/api/slots/${s.slot_id}`, { method: "DELETE" });
          refreshSlotList();
        } catch (e) {
          toast("删除失败：" + e.message);
        }
      });
      list.appendChild(row);
    });
  } catch (e) {
    list.innerHTML = '<p class="hint-line">读取存档列表失败</p>';
  }
}
// —— UI 绑定段 bindUiPart35（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart35() {

  $("btn-save").addEventListener("click", openSlots);
  $("btn-load").addEventListener("click", openSlots);
  $("btn-context-reset").addEventListener("click", async () => {
    if (!confirm("压缩上下文·重置 AI？\n\n将把本局长剧情压缩为「前情提要」（保留人物关系/当前形态/感情张力/尺度氛围与未了悬念），仅保留最近 6 幕对话，让 AI 回到更开放的写作状态（内容分级政策不变）。")) return;
    const sid = localStorage.getItem("galgame_sid");
    try {
      const r = await api(`/api/game/${sid}/context-reset`,
        { method: "POST", timeoutMs: 120000 });
      toast(`上下文已压缩：摘要 ${r.summary_chars} 字 · 保留最近 ${r.kept_turns} 幕`);
    } catch (e) {
      toast("压缩失败：" + e.message);
    }
  });
  $("btn-slots-close").addEventListener("click", () => $("modal-slots").classList.add("hidden"));
  $("btn-slot-save").addEventListener("click", async () => {
    try {
      const r = await api(`/api/game/${S.sid}/slots`, {
        method: "POST", body: JSON.stringify({ name: $("slot-name").value.trim() }),
      });
      $("slot-name").value = "";
      $("slots-hint").textContent = `✓ 已存档（${r.name}）`;
      await refreshSlotList();
    } catch (e) {
      toast("存档失败：" + e.message);
    }
  });

  /* ---------- 顶栏 ---------- */

  $("btn-history").addEventListener("click", async () => {
    try {
      const data = await api(`/api/game/${S.sid}/history`);
      const list = $("history-list");
      list.innerHTML = "";
      data.history.forEach((h) => {
        const item = document.createElement("div");
        item.className = "history-item";
        const lines = h.dialogue
          .map((d) => `<div class="history-line">${d.character !== "旁白" ? `<b>${escapeHtml(d.character)}：</b>` : ""}${escapeHtml(d.text)}</div>`)
          .join("");
        item.innerHTML = `<div class="history-scene">◆ ${escapeHtml(h.scene)}</div>${lines}` +
          (h.choice ? `<div class="history-choice">➤ 你选择了：${escapeHtml(h.choice)}</div>` : "");
        list.appendChild(item);
      });
      $("modal-history").classList.remove("hidden");
    } catch (e) {
      toast("读取历史失败：" + e.message);
    }
  });
  $("btn-history-close").addEventListener("click", () => $("modal-history").classList.add("hidden"));

  $("btn-exit").addEventListener("click", async () => {
    if (!confirm("结束本局并回到开始界面？（本局进度将删除）")) return;
    stopPolling();
    try { await api(`/api/game/${S.sid}`, { method: "DELETE" }); } catch { /* 忽略 */ }
    localStorage.removeItem("galgame_sid");
    S.sid = null;
    S.turn = null;
    $("game-screen").classList.remove("active");
    $("setup-screen").classList.add("active");
    refreshMockBanner();
  });
}

/* ---------- 启动：恢复上次会话 ---------- */


/* ---------- 启动器剧本直达：?story=<id>&autostart=1 ---------- */

function fillSetupForm(st) {
  if (st.mode && $("mode-btn-vn")) setSetupMode(st.mode, true);
  if (st.stat_config && S.statCfg) {
    S.statCfg[st.mode || S.mode] = st.stat_config.map((s) => ({
      key: s.key || "", name: s.name || "", icon: s.icon || "✦",
      start: s.start ?? 0,
    }));
    renderStatEditor();
  }
  $("inp-title").value = st.title || "";
  $("inp-world").value = st.world || "";
  $("prot-name").value = (st.protagonist || {}).name || "";
  // 主角锚点：拆开填入外貌补充（下拉项保留默认）；普通模式填进简化输入框
  const anchor = (st.protagonist || {}).anchor || "";
  if (anchor) {
    $("prot-extra").value = anchor;
    if ($("vn-anchor")) $("vn-anchor").value = anchor;
  }
  refreshAnchorPreview();
  // 角色卡
  $("char-list").innerHTML = "";
  (st.characters || []).forEach((c) => addCharCard(c));
  if (!(st.characters || []).length) addCharCard();
  // 故事书
  $("lore-list").innerHTML = "";
  (st.lorebook || []).forEach((e) => addLoreEntry(e));
  // 大纲 / 固定话语 / 分级
  $("inp-outline").value = st.outline || "";
  $("inp-catchphrases").value = (st.catchphrases || []).join(String.fromCharCode(10));
  if ($("uniform-en")) $("uniform-en").value = st.uniform_en || "";
  if ($("uniform-neg")) $("uniform-neg").value = st.uniform_neg || "";
  if ($("uniform-name")) {
    $("uniform-name").textContent = st.uniform_name
      || (st.uniform_en ? "自定义制服" : "未启用预设");
  }
  // 1.7.33 主角最终变化样式（开局面板）恢复
  const tsfTarget = st.tsf_target || {};
  if ($("tsf-target-build")) $("tsf-target-build").value = tsfTarget.build || "";
  if ($("tsf-target-chest")) $("tsf-target-chest").value = tsfTarget.chest || "";
  if ($("tsf-target-eye")) $("tsf-target-eye").value = tsfTarget.eye || "";
  if ($("tsf-target-hair-color")) $("tsf-target-hair-color").value =
    tsfTarget.keep_hair_color === false
      ? (tsfTarget.hair_color || "维持原色") : "维持原色";
  if ($("tsf-target-clothes")) $("tsf-target-clothes").value = tsfTarget.clothes || "";
  if ($("tsf-target-aura")) $("tsf-target-aura").value = tsfTarget.aura || "";
  $("inp-rating").value = st.content_rating || "all";
  $("inp-r18").disabled = (st.content_rating || "all") !== "18";
  $("inp-r18").checked = !!(st.r18_enabled) && (st.content_rating || "all") === "18";
  $("inp-forced").disabled = (st.content_rating || "all") !== "18" || !$("inp-r18").checked;
  $("inp-forced").checked = !!(st.allow_forced) && $("inp-r18").checked
    && (st.content_rating || "all") === "18";
  // 指令勾选（按 id 匹配预置）
  const enabled = new Set((st.directives || []).filter((d) => d.enabled).map((d) => d.id));
  setupDirectives.presets.forEach((p) => {
    p.enabled = enabled.has(p.id);
  });
  renderPresetGrid();
}

async function applyStoryFromUrl() {
  const params = new URLSearchParams(location.search);
  const storyId = params.get("story");
  if (!storyId) return;
  // 纵深防御：id 白名单校验（服务端亦有同规则），拒绝路径类/非法输入
  if (!/^[A-Za-z0-9_-]{1,16}$/.test(storyId)) {
    toast("剧本链接无效");
    return;
  }
  try {
    const st = await api(`/api/storylines/${storyId}`);
    fillSetupForm(st);
    if (params.get("autostart") === "1") {
      setTimeout(() => $("btn-start").click(), 1200);
    }
  } catch (e) {
    toast("剧本加载失败：" + e.message);
  }
}



/* ---------- 隐藏 Debug 面板（F10 / ?debug=1）与彩蛋 ---------- */

function openDebug() {
  $("debug-hint").textContent = "";
  $("modal-debug").classList.remove("hidden");
}
function closeDebug() {
  $("modal-debug").classList.add("hidden");
}
window.addEventListener("keydown", (e) => {
  const typing = /INPUT|TEXTAREA|SELECT/.test((e.target && e.target.tagName) || "");
  // F10 在部分浏览器被菜单拦截，故同时支持反引号 ~（输入框内不触发）
  if (e.key === "F10" || (e.key === "`" || e.key === "Backquote") && !typing) {
    e.preventDefault();
    if ($("modal-debug").classList.contains("hidden")) openDebug();
    else closeDebug();
  }
});
// Esc：关闭当前打开的弹窗
//
// t38 根因修复（审计 B-21）：这里原先是 `open.classList.add("hidden")` ——
// **直接改样式，绕过了各弹窗自己的关闭函数**。于是 closeCgGallery()（唯一会
// 执行 Sched.stop("cgRefresh") 的地方）永远不被调用，CG 回廊的 2500ms 轮询
// 在按 Esc 后仍会一直打 /api/cg，直到刷新页面。
// 机制性结论：**凡是"直接隐藏"的关闭路径，都会绕过清理**——所以这里改成
// 查表路由到真正的 close 函数；将来新增带定时器的弹窗，只需往表里加一行。
const MODAL_CLOSERS = {
  "modal-cg": closeCgGallery,
  "modal-debug": closeDebug,
  "modal-settings": closeSettingsIfOpen,
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const open = document.querySelector(".modal:not(.hidden)");
    if (!open) return;
    const closer = MODAL_CLOSERS[open.id];
    if (closer) closer();                  // ← 走真正的关闭路径（含清理）
    else open.classList.add("hidden");     // 没有专属清理的弹窗：保持原行为
  }
});
// —— UI 绑定段 bindUiPart36（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart36() {
  $("btn-debug-close").addEventListener("click", closeDebug);
  $("btn-debug-open").addEventListener("click", (e) => {
    e.preventDefault();
    closeSettingsIfOpen();
    openDebug();
  });
}
function closeSettingsIfOpen() {
  $("modal-settings").classList.add("hidden");
}

async function dbgApi(body) {
  if (!S.sid) { toast("请先进入游戏"); return null; }
  try {
    const state = await api(`/api/game/${S.sid}/debug`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    S.assets = state.assets;
    S.bgMap = state.bg_map || S.bgMap;
    S.outfits = state.outfits || S.outfits;
    playTurn(state, { instant: true });
    $("debug-hint").textContent = "✓ 已执行";
    return state;
  } catch (e) {
    toast("Debug 执行失败：" + e.message);
    return null;
  }
}
// —— UI 绑定段 bindUiPart37（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart37() {

  $("dbg-next-stage").addEventListener("click", () => dbgApi({ action: "next_stage" }));
  $("dbg-stat-add").addEventListener("click", () => dbgApi({ action: "set_stat", stat: "progress", value: 25 }));
  $("dbg-regen").addEventListener("click", () => dbgApi({ action: "regen_all" }));
  $("dbg-stat-set").addEventListener("click", () => dbgApi({
    action: "set_stat",
    stat: $("dbg-stat-key").value.trim() || "progress",
    value: parseInt($("dbg-stat-val").value, 10) || 0,
  }));
  $("dbg-scenes").addEventListener("click", async () => {
    try {
      const data = await api(`/api/game/${S.sid}/scenes`);
      const box = $("debug-scene-list");
      box.innerHTML = "";
      data.scenes.forEach((s) => {
        const row = document.createElement("div");
        row.className = "scene-row";
        row.innerHTML = `<b>${escapeHtml(s.name)}</b> 访问 ${s.visits} 次｜出场：${escapeHtml(s.roster.join("、") || "无")}` +
          (s.bg_file ? `<br><small>固定背景：${escapeHtml(s.bg_file)}</small>` : "");
        box.appendChild(row);
      });
      if (!data.scenes.length) box.innerHTML = '<p class="hint-line">暂无场景记录</p>';
      console.log("场景清单（重访场景将复用其固定背景图）:", data.scenes);
      $("debug-hint").textContent = `已列出 ${data.scenes.length} 个场景`;
    } catch (e) {
      toast("场景清单失败：" + e.message);
    }
  });

  $("dbg-outfit-go").addEventListener("click", () => dbgApi({
    action: "force_outfit",
    character: $("dbg-outfit-char").value.trim(),
    outfit: $("dbg-outfit-name").value.trim(),
  }));
}

/* 彩蛋 */
console.log("%c✨ 自动化AI Galgame %c 隐藏调试入口：游戏内按 F10（或打开 ?debug=1）",
            "color:#b9a8ff;font-size:16px;font-weight:bold", "color:#8a87a8");
let logoClicks = 0;
let logoTimer = null;
document.addEventListener("click", (e) => {
  if (e.target && e.target.className && String(e.target.className).includes("logo")) {
    clearTimeout(logoTimer);
    logoClicks++;
    logoTimer = setTimeout(() => { logoClicks = 0; }, 1200);
    if (logoClicks >= 5 && !logoClicksSeen) {
      logoClicksSeen = true;
      toast("✨ 彩蛋：双击页面任意空白处三下也能唤出调试面板", 4200);
    }
  }
});
let logoClicksSeen = false;
let blankClicks = 0;
let blankTimer = null;
document.addEventListener("click", (e) => {
  if (e.target && String(e.target.tagName) === "BODY" &&
      !S.sid && $("setup-screen") && $("setup-screen").classList.contains("active")) {
    blankClicks++;
    clearTimeout(blankTimer);
    blankTimer = setTimeout(() => { blankClicks = 0; }, 1200);
    if (blankClicks >= 3) { blankClicks = 0; toast("🎩 恭喜发现隐藏入口——游戏中按 F10", 3600); }
  }
});

/* ---------- 启动：恢复上次会话 ---------- */

/* ---------- 设定档案：保存/读取全部开局选项 ---------- */

async function refreshSetupList() {
  const sel = $("setup-load-select");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = '<option value="">（选择档案）</option>';
  try {
    const data = await api("/api/storylines");
    data.storylines.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = `${s.name || s.title}（${s.content_rating} · ${s.mode === "vn" ? "普通" : "TSF"}）`;
      sel.appendChild(opt);
    });
    if (cur) sel.value = cur;
    const tag = $("version-tag");
    if (tag) tag.textContent = `v${S.version || ""} · ${data.storylines.length} 档案`;
  } catch (e) {
    toast("档案列表加载失败：" + e.message);
    throw e;
  }
}

/* 首次进入自动重试一次档案读取：保证"不用刷新就能看到档案" */
async function ensureSetupList() {
  try { await refreshSetupList(); } catch {
    await new Promise((r) => setTimeout(r, 800));
    try { await refreshSetupList(); } catch { /* 保留默认 */ }
  }
}

function collectAllSetup() {
  return {
    mode: S.mode,
    title: $("inp-title").value.trim(),
    world: $("inp-world").value.trim(),
    protagonist: {
      name: $("prot-name").value.trim() || "主角",
      anchor: S.mode === "vn" ? $("vn-anchor").value.trim() : buildAnchor(),
    },
    characters: collectNPCs(),
    outline: $("inp-outline").value.trim(),
    lorebook: collectLorebook(),
    directives: collectDirectives(),
    catchphrases: ($("inp-catchphrases").value || "").split(String.fromCharCode(10))
      .map(s => s.trim()).filter(Boolean).slice(0, 5),
    content_rating: $("inp-rating").value,
    r18_enabled: $("inp-r18").checked,
    allow_forced: $("inp-forced").checked && $("inp-r18").checked
      && $("inp-rating").value === "18",
    stat_config: collectStatConfig(),
    uniform_en: $("uniform-en").value.trim(),
    uniform_neg: $("uniform-neg").value.trim(),
    uniform_name: ($("uniform-en").value.trim() || $("uniform-neg").value.trim())
      ? ($("uniform-name") ? $("uniform-name").textContent.trim() : "") : "",
    stage_count: Number(S.stageCount) || 0,
    tsf_target: collectTsfTarget(),
  };
}

// 1.7.33 开局「男主最终变化样式」采集（与游戏内 🎯 转变目标同结构）
function collectTsfTarget() {
  const gv = (id) => ($(id) ? $(id).value : "");
  const hair = gv("tsf-target-hair-color");
  return {
    build: gv("tsf-target-build"),
    chest: gv("tsf-target-chest"),
    eye: gv("tsf-target-eye"),
    hair_color: hair,
    clothes: gv("tsf-target-clothes"),
    aura: gv("tsf-target-aura"),
    keep_hair_color: hair === "" || hair === "维持原色",
  };
}
// —— UI 绑定段 bindUiPart38（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart38() {

  $("btn-save-setup").addEventListener("click", async () => {
    const body = collectAllSetup();
    if (!body.world) { toast("世界观为空，未保存"); return; }
    const name = $("setup-save-name").value.trim();
    if (!name) { toast("请先给档案起个名字"); return; }
    body.id = "";
    body.name = name;
    try {
      const saved = await api("/api/storylines", { method: "POST", body: JSON.stringify(body) });
      $("setup-save-name").value = "";
      $("setup-load-select").value = saved.id;
      refreshSetupList();
      toast(`设定已保存：${saved.title}`);
    } catch (e) {
      toast("保存失败：" + e.message);
    }
  });

  $("btn-play-setup").addEventListener("click", async () => {
    // 一键开局：读取所选档案并以缓存命中方式进入游戏
    const id = $("setup-load-select").value;
    if (!id) { toast("先选择一个档案"); return; }
    try {
      const st = await api(`/api/storylines/${id}`);
      fillSetupForm(st);
      await new Promise(r => setTimeout(r, 300));
      $("btn-start").click();
    } catch (e) {
      toast("一键开局失败：" + e.message);
    }
  });

  $("btn-load-setup").addEventListener("click", async () => {
    const id = $("setup-load-select").value;
    if (!id) { toast("先选择一个档案"); return; }
    try {
      const st = await api(`/api/storylines/${id}`);
      fillSetupForm(st);
      toast(`已载入设定：${st.title}`);
    } catch (e) {
      toast("读取失败：" + e.message);
    }
  });

  $("btn-delete-setup").addEventListener("click", async () => {
    const id = $("setup-load-select").value;
    if (!id) { toast("先选择一个档案"); return; }
    if (!confirm("删除该设定档案？")) return;
    try {
      await api("/api/storylines/delete", { method: "POST", body: JSON.stringify({ id }) });
      refreshSetupList();
    } catch (e) {
      toast("删除失败：" + e.message);
    }
  });
}

/* ---------- 主界面：我的存档（读取/删除，跨会话） ---------- */

async function refreshSetupSlotList() {
  const list = $("slot-list-setup");
  if (!list) return;
  try {
    const data = await api(`/api/slots?t=${Date.now()}`);
    list.innerHTML = "";
    if (!data.slots.length) {
      list.innerHTML = '<p class="hint-line">暂无存档（游戏内顶栏「💾 存档」可创建）</p>';
      return;
    }
    data.slots.forEach((s) => {
      const row = document.createElement("div");
      row.className = "slot-row";
      row.innerHTML = `
        <span class="slot-info">${escapeHtml(s.name)} ｜ ${escapeHtml(s.title || "")} ｜ 第 ${s.turn_no} 幕</span>
        <button class="btn mini ghost slot-load">📂 读取</button>
        <button class="btn mini ghost slot-ngp" title="继承角色与场景，剧情/数值从头开始；前情自动压缩">🔁 二周目</button>
        <button class="btn mini ghost slot-del">✕</button>`;
      row.querySelector(".slot-load").addEventListener("click", async () => {
        try {
          const state = await api(`/api/slots/${s.slot_id}/load`, { method: "POST" });
          localStorage.setItem("galgame_sid", state.sid);
          enterGame(state, { instant: true });
          toast(`已读取存档：${s.name}`);
        } catch (e) {
          toast("读档失败：" + e.message);
        }
      });
      row.querySelector(".slot-ngp").addEventListener("click", async () => {
        try {
          toast("二周目开启中（继承角色与场景，首次约 1-2 分钟）…");
          const state = await api(`/api/game/newgame-plus`,
            { method: "POST", timeoutMs: 300000,
              body: JSON.stringify({ slot_id: s.slot_id, title: (s.name || "") + "·二周目" }) });
          localStorage.setItem("galgame_sid", state.sid);
          enterGame(state, { instant: true });
          toast("二周目已开启：角色/场景已继承，前情已压缩");
        } catch (e) {
          toast("二周目失败：" + e.message);
        }
      });
      row.querySelector(".slot-del").addEventListener("click", async () => {
        if (!confirm("删除该存档？")) return;
        try {
          await api(`/api/slots/${s.slot_id}`, { method: "DELETE" });
          refreshSetupSlotList();
        } catch (e) {
          toast("删除失败：" + e.message);
        }
      });
      list.appendChild(row);
    });
  } catch (e) {
    list.innerHTML = '<p class="hint-line">读取存档列表失败</p>';
  }
}

/* ---------- 角色库：开局选择/导入复用 ---------- */

let libPickTarget = "prot";
let libPickCard = null;   // 1.9.0：目标为某张角色卡（per-card 📚）时回填这张卡
window.__libImports = [];

function rememberLibImport(roleName, name, dir) {
  if (!$("libpick-reuse").checked) return;
  const arr = window.__libImports;
  if (arr.some((x) => x.name === name && x.role === roleName && x.dir === dir)) return;
  arr.push({ name, role: roleName, dir: dir || "" });
}

async function openLibPicker(target, card = null) {
  libPickTarget = target;
  libPickCard = card || null;
  $("libpick-hint").textContent = target === "prot"
    ? "选中后自动填入主角设定（外貌并入「外貌补充」，保证立绘一致）"
    : (target === "card"
      ? "选中后填入这张角色卡（名字/身份/外貌/性格/背景），已有立绘直接复用"
      : "选中后自动新增一张角色卡；库内已有立绘将直接复用");
  try {
    const data = await api("/api/library");
    const grid = $("libpick-list");
    grid.innerHTML = "";
    const chars = data.characters || [];
    if (!chars.length) {
      grid.innerHTML = '<p class="hint-line">角色库为空：先开一局生成立绘，游戏内点「📚 角色库」即可把角色导出入库。</p>';
    }
    chars.forEach((c) => {
      const card = document.createElement("div");
      card.className = "cg-item libpick-item";
      const thumb = (c.variants || []).length
        ? `<img src="/api/library/${encodeURIComponent(c.name)}/preview?dir=${encodeURIComponent(c.dir || "")}" alt="" loading="lazy">`
        : '<div class="cg-thumb-empty">暂无立绘<br><small>可仅导入设定</small></div>';
      card.innerHTML = `
        ${thumb}
        <div class="cg-caption">${escapeHtml(c.name)}
          ${c.dir && c.dir !== c.name ? `<span class="cg-type">${escapeHtml(c.dir)}</span>` : ""}
          <span class="cg-type">${escapeHtml(c.role || "NPC")}</span>
          <span class="cg-type">${(c.variants || []).length} 立绘</span>
          ${c.background ? '<span class="cg-type ok">有背景</span>'
            : '<span class="cg-type warn">背景待谱写</span>'}
          ${c.source_game ? `<span class="cg-type">来自「${escapeHtml(c.source_game)}」</span>` : ""}
          ${c.background ? `<div class="cg-bg-preview">${escapeHtml(c.background.slice(0, 60))}…</div>` : ""}
        </div>`;
      card.addEventListener("click", async () => {
        try {
          if (libPickTarget === "prot") {
            $("prot-name").value = c.name;
            const a = c.anchor || c.appearance || "";
            if (S.mode === "vn") { $("vn-anchor").value = a; }
            else if (a) { $("prot-extra").value = a; refreshAnchorPreview(); }
            rememberLibImport("主角", c.name, c.dir);
          } else {
            const payload = {
              name: c.name, role: c.role || "",
              appearance: c.appearance || c.anchor || "",
              personality: c.personality || "",
              background: c.background || "",
            };
            if (libPickTarget === "card" && libPickCard) {
              fillCharCard(libPickCard, payload);
            } else {
              addCharCard(payload);
            }
            rememberLibImport("NPC", c.name, c.dir);
          }
          $("modal-libpick").classList.add("hidden");
          toast(`已从角色库选用「${c.name}${c.dir && c.dir !== c.name ? "（" + c.dir + "）" : ""}」`);
        } catch (e2) { toast("填入失败：" + e2.message); }
      });
      grid.appendChild(card);
    });
    $("modal-libpick").classList.remove("hidden");
  } catch (e) {
    toast("读取角色库失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart39（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart39() {
  on("btn-lib-pick-prot", "click", () => openLibPicker("prot"));
  on("btn-lib-pick-npc", "click", () => openLibPicker("npc"));
  on("btn-libpick-close", "click", () => $("modal-libpick").classList.add("hidden"));
  on("btn-libpick-adapt", "click", () => {
    $("modal-libpick").classList.add("hidden");
    openLegacy();
  });
}

/* ---------- 1.9.0：随机制服 / 旧档案找回 ---------- */

let uniformPresets = [];

async function loadUniformPresets() {
  try {
    const data = await api("/api/uniforms");
    uniformPresets = data.presets || [];
    const sel = $("uniform-preset-select");
    if (!sel) return;
    sel.innerHTML = '<option value="">从预设选择…</option>';
    uniformPresets.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = `${p.icon || "👔"} ${p.name} · ${p.note || ""}`;
      sel.appendChild(opt);
    });
  } catch (e) {
    console.warn("[tsf] 制服库加载失败：", e);
  }
}

function applyUniformPreset(p, { toastIt = true } = {}) {
  if (!p) return;
  if ($("uniform-en")) $("uniform-en").value = p.en || "";
  if ($("uniform-neg")) $("uniform-neg").value = p.neg || "";
  if ($("uniform-name")) $("uniform-name").textContent = `${p.icon || "👔"} ${p.name}`;
  if ($("uniform-preset-select")) $("uniform-preset-select").value = p.id || "";
  if (toastIt) toast(`🎲 已选用制服「${p.name}」：${p.note || ""}`, 3200);
}

async function randomUniformSetup() {
  const btn = $("btn-uniform-random");
  if (!btn) return;
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "抽取中…";
  try {
    const scene = [
      $("inp-world") ? $("inp-world").value : "",
      $("inp-outline") ? $("inp-outline").value : "",
      $("inp-title") ? $("inp-title").value : "",
    ].join(" ").slice(0, 600);
    const p = await api("/api/uniforms/random", {
      method: "POST",
      body: JSON.stringify({
        scene,
        exclude: $("uniform-preset-select") && $("uniform-preset-select").value
          ? [$("uniform-preset-select").value] : [],
      }),
    });
    applyUniformPreset(p);
  } catch (e) {
    toast("随机制服失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function clearUniformSetup() {
  if ($("uniform-en")) $("uniform-en").value = "";
  if ($("uniform-neg")) $("uniform-neg").value = "";
  if ($("uniform-name")) $("uniform-name").textContent = "未启用预设";
  if ($("uniform-preset-select")) $("uniform-preset-select").value = "";
}

async function randomOutfit() {
  const btn = $("btn-outfit-random");
  if (!btn) return;
  btn.disabled = true;
  try {
    const p = await api("/api/uniforms/random", {
      method: "POST",
      body: JSON.stringify({ scene: (S.turn && S.turn.scene) || "" }),
    });
    pendingOutfit = p.name;
    $("outfit-preset").value = p.name;
    document.querySelectorAll(".outfit-preset-btn").forEach((b) => b.classList.remove("sel"));
    if ($("outfit-random-note")) {
      $("outfit-random-note").textContent =
        `🎲 ${p.icon || "👔"} ${p.name} · ${p.en}`;
    }
    updateOutfitHint();
    toast(`🎲 抽到「${p.name}」，点「✔ 确定换装」生效`, 3200);
  } catch (e) {
    toast("随机制服失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

let legacyScanData = null;

function renderLegacyScan(data) {
  legacyScanData = data;
  const summary = $("legacy-summary");
  const counts = data.counts || {};
  if (summary) {
    summary.innerHTML =
      `<b>可找回：</b> 设定档案 ${counts.storylines || 0} 份 · ` +
      `旧角色卡 ${counts.roster || 0} 张 · 对局 zip ${counts.archives || 0} 个 ` +
      `<span class="hint">（需要处理 ${counts.need_action || 0} 项）</span>`;
  }
  const list = $("legacy-list");
  if (!list) return;
  list.innerHTML = "";
  const rows = [];
  (data.storylines || []).forEach((s) => {
    rows.push(`<div class="legacy-item">
      <span class="legacy-badge ${s.status === "exists" ? "ok" : "warn"}">${escapeHtml(s.status)}</span>
      <b>${escapeHtml(s.title || s.name || s.file)}</b>
      <small>${escapeHtml(s.source)} · ${s.characters} 角色 · ${escapeHtml(s.note || "")}</small></div>`);
  });
  (data.roster || []).forEach((r) => {
    rows.push(`<div class="legacy-item">
      <span class="legacy-badge warn">旧角色卡</span>
      <b>${escapeHtml(r.name)}</b>
      <small>${escapeHtml(r.dir)} · ${r.variants} 立绘 · 待补：${escapeHtml((r.needs || []).join(" / "))}</small></div>`);
  });
  (data.archives || []).forEach((a) => {
    rows.push(`<div class="legacy-item">
      <span class="legacy-badge ok">zip</span>
      <b>${escapeHtml(a.title || a.file)}</b>
      <small>${escapeHtml(a.source)} · ${escapeHtml(a.file)}</small></div>`);
  });
  list.innerHTML = rows.length ? rows.join("")
    : '<p class="hint-line">没有发现可找回的旧档案（或已全部适配）。</p>';
}

async function scanLegacy() {
  const hint = $("legacy-hint");
  if (hint) hint.textContent = "扫描中…";
  try {
    const data = await api("/api/legacy/scan");
    renderLegacyScan(data);
    if (hint) hint.textContent = "扫描完成";
  } catch (e) {
    if (hint) hint.textContent = "扫描失败：" + e.message;
    toast("旧档案扫描失败：" + e.message);
  }
}

async function openLegacy() {
  $("modal-legacy").classList.remove("hidden");
  await scanLegacy();
}

async function restoreLegacy() {
  const btn = $("btn-legacy-restore");
  const hint = $("legacy-hint");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "找回中…";
  if (hint) hint.textContent = "正在找回并适配，请稍候…";
  try {
    const result = await api("/api/legacy/restore", {
      method: "POST",
      body: JSON.stringify({
        storylines: $("legacy-storylines").checked,
        roster: $("legacy-roster").checked,
        archives: $("legacy-archives").checked,
        generate_backgrounds: $("legacy-bg").checked,
      }),
    });
    const s = result.storylines || {};
    const r = result.roster || {};
    const a = result.archives || {};
    const text = `✓ 设定档案：新建 ${s.created || 0} · 适配 ${s.adapted || 0}` +
      `｜旧角色卡：适配 ${r.adapted || 0}（谱写背景 ${r.backgrounds || 0}）` +
      `｜zip：导入 ${(a.imported || []).length}`;
    if ($("legacy-result")) $("legacy-result").textContent = text;
    if (hint) hint.textContent = "完成";
    toast(text, 5000);
    await scanLegacy();
    if (typeof refreshSetupList === "function") refreshSetupList();
  } catch (e) {
    if (hint) hint.textContent = "找回失败：" + e.message;
    toast("旧档案找回失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* ---------- 1.9.20：AI 控制台 ----------
   数据源：GET /api/game/{sid}/ai/console
   · entries：每次主剧情 LLM 调用的请求 / reasoning_content / 原始输出 / 用量 / 解析结果
   · dialogue：当前幕的对话（可编辑）
   · directive：持续注入的玩家导演指令
   这里不伪造思考：模型没返回 reasoning_content 时，明确显示“未返回”。 */
function aiStatusText(e) {
  if (!e) return "";
  if (e.status === "pending") return "生成中";
  if (e.status === "error") return "失败";
  return "完成";
}

async function openAiConsole() {
  if (!S.sid) { toast("还没有进入对局"); return; }
  $("modal-ai").classList.remove("hidden");
  await refreshAiConsole();
  switchAiTab("think");
}

async function refreshAiConsole() {
  if (!S.sid) return;
  try {
    const r = await api(`/api/game/${S.sid}/ai/console`);
    S.aiData = r;
    const rows = r.entries || [];
    if (!S.aiEntryId || !rows.some((x) => x.id === S.aiEntryId)) {
      S.aiEntryId = rows.length ? rows[0].id : null;
    }
    renderAiEntries();
    renderAiEntryDetail();
    const dir = $("ai-directive");
    if (dir) dir.value = r.directive || "";
    renderAiDialogueEditor(r.dialogue || []);
    const msg = $("ai-msg");
    if (msg) msg.textContent = `${rows.length} 条调用记录 · ${r.mode || ""}`;
  } catch (e) {
    const msg = $("ai-msg");
    if (msg) msg.textContent = "读取 AI 控制台失败：" + e.message;
  }
}

function renderAiEntries() {
  const box = $("ai-entry-list");
  if (!box) return;
  const rows = (S.aiData && S.aiData.entries) || [];
  if (!rows.length) {
    box.innerHTML = `<div class="model-info">还没有调用记录。<br>推进一幕或重新开局后再打开这里。</div>`;
    return;
  }
  box.innerHTML = rows.map((e) => {
    const cls = e.id === S.aiEntryId ? " on" : "";
    const dot = e.status === "error" ? "error" : (e.status === "pending" ? "pending" : "");
    const ms = Number(e.duration_ms) || 0;
    return `<div class="ai-entry${cls}" data-id="${escapeHtml(String(e.id))}">`
      + `<b><i class="ai-dot ${dot}"></i>${escapeHtml(String(e.label || "剧情"))}</b>`
      + `<small>${escapeHtml(String(e.time || ""))} · ${escapeHtml(aiStatusText(e))}`
      + `${ms ? ` · ${(ms / 1000).toFixed(1)}s` : ""}</small>`
      + `<small>${escapeHtml(String(e.model || "—"))}</small></div>`;
  }).join("");
}

function renderAiEntryDetail() {
  const box = $("ai-entry-detail");
  if (!box) return;
  const e = ((S.aiData && S.aiData.entries) || []).find((x) => x.id === S.aiEntryId);
  if (!e) {
    box.textContent = "还没有记录。推进一幕后再打开这里。";
    return;
  }
  const usage = e.usage || {};
  const tokens = [usage.prompt_tokens, usage.completion_tokens, usage.total_tokens]
    .filter((x) => x !== undefined && x !== null).join(" / ");
  const msgs = (e.messages || []).map((m) => {
    const role = String(m.role || "");
    return `<details class="ai-msg-row ${escapeHtml(role)}">`
      + `<summary><b>${escapeHtml(role)}</b> · ${String(m.content || "").length} 字</summary>`
      + `<pre>${escapeHtml(String(m.content || ""))}</pre></details>`;
  }).join("");
  const parse = e.parse
    ? (e.parse.ok ? "JSON 解析成功" : `JSON 解析失败：${e.parse.error || ""}`)
    : "未记录解析状态";
  box.innerHTML = `
    <div><b>${escapeHtml(String(e.label || "剧情"))}</b>
      <span class="hint">${escapeHtml(String(e.time || ""))} · ${escapeHtml(aiStatusText(e))}
      ${e.duration_ms ? `· ${(Number(e.duration_ms) / 1000).toFixed(1)}s` : ""}</span></div>
    <div class="model-info">模型 ${escapeHtml(String(e.model || "—"))}
      · temperature ${e.temperature === null || e.temperature === undefined ? "—" : escapeHtml(String(e.temperature))}
      ${e.mock ? " · 演示模式" : ""}
      ${tokens ? ` · tokens ${escapeHtml(tokens)}` : ""}</div>
    <h4>思考 / reasoning_content</h4>
    ${e.reasoning
      ? `<pre>${escapeHtml(String(e.reasoning))}</pre>`
      : `<div class="model-info">该模型没有返回 reasoning_content；这里不伪造思考，只展示真实请求与输出。</div>`}
    <h4>本次收到的消息（system 可能很长，默认折叠）</h4>
    ${msgs || `<div class="model-info">无消息记录</div>`}
    <h4>原始输出</h4>
    ${e.raw ? `<pre>${escapeHtml(String(e.raw))}</pre>` : `<div class="model-info">无输出</div>`}
    <h4>解析 / 错误</h4>
    <div class="model-info">${escapeHtml(parse)}${e.error ? `<br>错误：${escapeHtml(String(e.error))}` : ""}</div>`;
}

function switchAiTab(tab) {
  const t = ["think", "edit", "direct"].includes(tab) ? tab : "think";
  S.aiTab = t;
  $("ai-pane-think").classList.toggle("hidden", t !== "think");
  $("ai-pane-edit").classList.toggle("hidden", t !== "edit");
  $("ai-pane-direct").classList.toggle("hidden", t !== "direct");
  $("ai-tab-think").className = "btn mini " + (t === "think" ? "primary" : "ghost");
  $("ai-tab-edit").className = "btn mini " + (t === "edit" ? "primary" : "ghost");
  $("ai-tab-direct").className = "btn mini " + (t === "direct" ? "primary" : "ghost");
  if (t === "edit") renderAiDialogueEditor((S.aiData && S.aiData.dialogue) || []);
  if (t === "direct") {
    const dir = $("ai-directive");
    if (dir) dir.value = (S.aiData && S.aiData.directive) || "";
  }
}

function renderAiDialogueEditor(dialogue) {
  const box = $("ai-dialogue-editor");
  if (!box) return;
  const rows = (dialogue || []).slice(0, 6);
  if (!rows.length) rows.push({ character: "旁白", emotion: "neutral", text: "" });
  box.innerHTML = rows.map((d) => `
    <div class="ai-dialogue-row">
      <input class="text-input ai-char" maxlength="20" value="${escapeHtml(String(d.character || "旁白"))}"
        placeholder="角色">
      <input class="text-input ai-emo" maxlength="20" value="${escapeHtml(String(d.emotion || "neutral"))}"
        placeholder="情绪">
      <textarea class="text-input ai-text" maxlength="800"
        placeholder="这一行台词 / 旁白">${escapeHtml(String(d.text || ""))}</textarea>
      <button class="btn mini ghost ai-del" type="button" title="删除这一行">✕</button>
    </div>`).join("");
}

function aiAddDialogueLine() {
  const box = $("ai-dialogue-editor");
  if (!box) return;
  const n = box.querySelectorAll(".ai-dialogue-row").length;
  if (n >= 6) { toast("最多 6 行对话"); return; }
  const row = document.createElement("div");
  row.className = "ai-dialogue-row";
  row.innerHTML = `<input class="text-input ai-char" maxlength="20" value="旁白" placeholder="角色">
    <input class="text-input ai-emo" maxlength="20" value="neutral" placeholder="情绪">
    <textarea class="text-input ai-text" maxlength="800" placeholder="这一行台词 / 旁白"></textarea>
    <button class="btn mini ghost ai-del" type="button" title="删除这一行">✕</button>`;
  box.appendChild(row);
}

function aiCollectDialogue() {
  const out = [];
  document.querySelectorAll("#ai-dialogue-editor .ai-dialogue-row").forEach((row) => {
    const text = row.querySelector(".ai-text").value.trim();
    if (!text) return;
    out.push({
      character: row.querySelector(".ai-char").value.trim() || "旁白",
      emotion: row.querySelector(".ai-emo").value.trim() || "neutral",
      text,
    });
  });
  return out;
}

async function saveAiDialogue() {
  if (!S.sid) return;
  const dialogue = aiCollectDialogue();
  if (!dialogue.length) { toast("至少保留一行非空对话"); return; }
  const btn = $("btn-ai-save-dialogue");
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/game/${S.sid}/ai/edit-dialogue`, {
      method: "POST", body: JSON.stringify({ dialogue }),
    });
    if (r && r.state) {
      S.turn = r.state;
      S.lineIdx = 0;
      $("choices-wrap").classList.add("hidden");
      $("choices-wrap").innerHTML = "";
      S.coc = r.state.coc || S.coc;
      renderStatsPanels();
      showDialogLine(true);
    }
    $("ai-edit-msg").textContent = "✓ 已保存；画面台词已替换。";
    toast("对话已修改", 2600);
    await refreshAiConsole();
    switchAiTab("edit");
  } catch (e) {
    $("ai-edit-msg").textContent = "保存失败：" + e.message;
    toast("保存对话失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function saveAiDirective() {
  if (!S.sid) return;
  const text = $("ai-directive").value.trim();
  try {
    const r = await api(`/api/game/${S.sid}/ai/directive`, {
      method: "POST", body: JSON.stringify({ text }),
    });
    S.aiDirective = (r && r.directive) || "";
    if (S.aiData) S.aiData.directive = S.aiDirective;
    $("ai-direct-msg").textContent = text
      ? "✓ 已保存；下一轮剧情生成开始生效。"
      : "✓ 已清空；下一轮恢复默认风格。";
    toast("导演指令已保存", 2600);
  } catch (e) {
    $("ai-direct-msg").textContent = "保存失败：" + e.message;
    toast("保存导演指令失败：" + e.message);
  }
}

async function clearAiConsole() {
  if (!S.sid) return;
  if (!window.confirm("清空本局的 AI 调用记录？（不影响对话与游戏进度）")) return;
  try {
    await api(`/api/game/${S.sid}/ai/console/clear`, { method: "POST" });
    S.aiEntryId = null;
    await refreshAiConsole();
    renderAiEntryDetail();
    toast("AI 调用记录已清空");
  } catch (e) {
    toast("清空失败：" + e.message);
  }
}

function bindUiPart42() {
  const onSafe = (id, ev, fn) => {
    const el = $(id);
    if (el) el.addEventListener(ev, fn);
  };
  onSafe("btn-uniform-random", "click", randomUniformSetup);
  onSafe("btn-uniform-clear", "click", clearUniformSetup);
  onSafe("uniform-preset-select", "change", () => {
    const id = $("uniform-preset-select").value;
    const p = uniformPresets.find((x) => x.id === id);
    if (p) applyUniformPreset(p, { toastIt: false });
  });
  ["uniform-en", "uniform-neg"].forEach((id) => onSafe(id, "input", () => {
    if ($("uniform-name") && ($("uniform-en").value || $("uniform-neg").value)) {
      $("uniform-name").textContent = "自定义制服";
      if ($("uniform-preset-select")) $("uniform-preset-select").value = "";
    }
  }));
  onSafe("btn-outfit-random", "click", randomOutfit);
  onSafe("btn-legacy", "click", openLegacy);
  onSafe("btn-legacy-scan", "click", scanLegacy);
  onSafe("btn-legacy-restore", "click", restoreLegacy);
  onSafe("btn-legacy-close", "click", () => $("modal-legacy").classList.add("hidden"));

  // 1.9.20：AI 控制台
  onSafe("btn-ai-console", "click", openAiConsole);
  onSafe("btn-ai-close", "click", () => $("modal-ai").classList.add("hidden"));
  onSafe("btn-ai-close2", "click", () => $("modal-ai").classList.add("hidden"));
  onSafe("btn-ai-refresh", "click", refreshAiConsole);
  onSafe("btn-ai-clear", "click", clearAiConsole);
  onSafe("ai-tab-think", "click", () => switchAiTab("think"));
  onSafe("ai-tab-edit", "click", () => switchAiTab("edit"));
  onSafe("ai-tab-direct", "click", () => switchAiTab("direct"));
  onSafe("btn-ai-add-line", "click", aiAddDialogueLine);
  onSafe("btn-ai-save-dialogue", "click", saveAiDialogue);
  onSafe("btn-ai-save-directive", "click", saveAiDirective);
  const aiEntries = $("ai-entry-list");
  if (aiEntries) {
    aiEntries.addEventListener("click", (ev) => {
      const row = ev.target.closest(".ai-entry");
      if (!row) return;
      S.aiEntryId = row.dataset.id || null;
      renderAiEntries();
      renderAiEntryDetail();
    });
  }
  const aiEditor = $("ai-dialogue-editor");
  if (aiEditor) {
    aiEditor.addEventListener("click", (ev) => {
      const del = ev.target.closest(".ai-del");
      if (!del) return;
      const row = del.closest(".ai-dialogue-row");
      if (row) row.remove();
    });
  }

  // 1.9.4：QtWebEngine 的 <select> 原生弹层关闭后可能留下上一项的选中高亮/
  // 残影；同时鼠标点过的按钮会保留 :focus 外环。选中/点击后主动 blur 并让
  // 弹窗轻量重绘一次，清掉焦点与原生弹层缓存。
  document.addEventListener("change", (e) => {
    const sel = e.target;
    if (!sel || sel.tagName !== "SELECT") return;
    try { sel.blur(); } catch { /* 忽略 */ }
    const box = sel.closest(".modal-box");
    if (box) {
      // 不改布局，只触发一次合成重绘；下一帧恢复，避免留下 transform 层。
      const old = box.style.transform;
      box.style.transform = "translateZ(0)";
      requestAnimationFrame(() => { box.style.transform = old; });
    }
  });
  document.addEventListener("click", (e) => {
    if (e.detail <= 0) return;          // 键盘触发不夺焦点
    const btn = e.target.closest && e.target.closest("button");
    if (btn) setTimeout(() => { try { btn.blur(); } catch { /* 忽略 */ } }, 0);
  });
}

/* ---------- 对局归档：导出 / 导入 / 下载 / 删除 ---------- */

function fmtSizeMB(n) {
  return ((n || 0) / 1048576).toFixed(1) + " MB";
}
function fmtClock(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function refreshArchiveList() {
  const list = $("archive-list");
  if (!list) return;
  try {
    const data = await api("/api/archives");
    list.innerHTML = "";
    const items = data.archives || [];
    if (!items.length) {
      list.innerHTML = '<p class="hint-line">暂无对局档案（游戏内点「📦 对局归档 → 导出本局」即可生成 zip）。</p>';
      return;
    }
    items.forEach((a) => {
      const el = document.createElement("div");
      el.className = "slot-item";
      el.innerHTML = `
        <div class="slot-info">
          <b>${escapeHtml(a.title || "未命名对局")}</b>
          <span class="archive-meta">${a.turn_no} 轮 · ${fmtSizeMB(a.size)} · ${escapeHtml(a.protagonist)}
            · ${fmtClock(a.mtime)}</span>
        </div>
        <div class="slot-actions">
          <button class="btn mini ghost btn-arc-load">▶ 继续</button>
          <button class="btn mini ghost btn-arc-dl">⬇ 下载</button>
          <button class="btn mini ghost danger-text btn-arc-del">✕</button>
        </div>`;
      el.querySelector(".btn-arc-load").addEventListener("click", async () => {
        try {
          const r = await api(`/api/archives/${encodeURIComponent(a.file)}/import`, { method: "POST" });
          localStorage.setItem("galgame_sid", r.sid);
          enterGame(r.state, { instant: false });
          $("modal-archives").classList.add("hidden");
          toast(`已导入对局「${a.title}」`);
        } catch (e) { toast("导入失败：" + e.message); }
      });
      el.querySelector(".btn-arc-dl").addEventListener("click", () => {
        // t50：原来此处用「window 打开新标签页」的写法（带空白 target）。
        // 在原生窗口（QtWebEngine 等）里那会**拉起系统浏览器**，与本项目
        // 「原生窗口内嵌、不再开浏览器」的目标直接冲突（server 侧 webbrowser
        // 已实测真的开过标签）。改法：同源下载用带 download 属性的 <a>
        // 在**当前窗口内**触发，不新建窗口、不唤起系统浏览器；并给可见 toast，绝不静默。
        // 注：本注释刻意不写出那两个被机械核对检索的字面量，否则会把计数污染的
        //     门禁变成假阳性（见 tsf-fix/FRONTEND-WINDOWOPEN.md 的枚举范围声明）。
        // 改法：同源下载用带 download 属性的 <a> 在**当前窗口内**触发，
        // 既不新建窗口也不唤起系统浏览器；并给可见 toast，绝不静默。
        const url = `/api/archives/${encodeURIComponent(a.file)}/download`;
        const link = document.createElement("a");
        link.href = url;
        link.download = a.file || "";   // 仅作文件名建议；DOM 属性赋值，非 HTML 注入面
        link.rel = "noopener";
        document.body.appendChild(link);
        link.click();
        link.remove();
        toast(`开始下载档案「${a.title}」——保存到本机下载目录`, 3400);
      });
      el.querySelector(".btn-arc-del").addEventListener("click", async () => {
        if (!confirm(`删除档案「${a.title}」？仅删除 zip 档案，不影响对局数据。`)) return;
        try {
          await api(`/api/archives/${encodeURIComponent(a.file)}`, { method: "DELETE" });
          refreshArchiveList();
        } catch (e) { toast("删除失败：" + e.message); }
      });
      list.appendChild(el);
    });
  } catch (e) {
    toast("档案列表加载失败：" + e.message);
  }
}
// —— UI 绑定段 bindUiPart40（t37：自模块顶层收敛至此，内容逐字未改）——
function bindUiPart40() {

  on("btn-archives-setup", "click", () => { $("modal-archives").classList.remove("hidden"); refreshArchiveList(); });
  on("btn-archives-game", "click", () => { $("modal-archives").classList.remove("hidden"); refreshArchiveList(); });
  on("btn-archives-close", "click", () => $("modal-archives").classList.add("hidden"));

  on("btn-archive-export", "click", async () => {
    if (!S.sid) { toast("进入对局后才能导出本局档案"); return; }
    const btn = $("btn-archive-export");
    btn.disabled = true;
    btn.textContent = "导出中……";
    try {
      const r = await api(`/api/game/${S.sid}/archive`, { method: "POST" });
      toast(`已导出归档：${r.file}（${fmtSizeMB(r.size)}）`, 4200);
      refreshArchiveList();
    } catch (e) {
      toast("导出失败：" + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "📥 导出本局为档案";
    }
  });

  on("btn-archive-import", "click", () => $("archive-file").click());
  on("archive-file", "change", async () => {
    const f = $("archive-file").files[0];
    if (!f) return;
    if (f.size > 500 * 1024 * 1024) { toast("档案超过 500MB，无法导入"); return; }
    try {
      const resp = await fetch("/api/archives/import", { method: "POST", body: f });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || `请求失败（${resp.status}）`);
      localStorage.setItem("galgame_sid", data.sid);
      enterGame(data.state, { instant: false });
      $("modal-archives").classList.add("hidden");
      toast(`对局档案导入成功，继续「${data.state.title || ""}」`);
    } catch (e) {
      toast("导入失败：" + e.message);
    } finally {
      $("archive-file").value = "";
    }
  });
}

/* ---------- 1.9.19：固定商店场景 ----------
   进入/切换/购买/重掷全部走服务端；前端只渲染服务端给的库存与价格，
   不自己算钱、不自己生成商品（避免"前端一份、后端一份"的第二真相）。 */
async function openShopCatalog() {
  if (!S.sid) { toast("还没有进入对局"); return; }
  try {
    const r = await api(`/api/game/${S.sid}/coc/shop`);
    S.shopCatalog = (r && r.shops) || [];
    if (r && r.coc) S.coc = r.coc.coc || S.coc;
    $("modal-shop").classList.remove("hidden");
    renderShopTabs(S.shopCatalog);
    const first = S.shopId || (S.shopCatalog[0] && S.shopCatalog[0].id) || "general_store";
    await openShopTab(first);
  } catch (e) {
    toast("商店打不开：" + e.message);
  }
}

async function openShopTab(shopId) {
  if (!S.sid || !shopId) return;
  const msg = $("shop-msg");
  if (msg) msg.textContent = "正在推门……";
  try {
    const r = await api(`/api/game/${S.sid}/coc/shop/open`, {
      method: "POST",
      body: JSON.stringify({ shop: shopId }),
    });
    S.shop = r.shop || null;
    S.shopId = shopId;
    if (r && r.coc) S.coc = r.coc.coc || S.coc;
    renderShopTabs(S.shopCatalog || []);
    renderShop(r.shop, r.coc);
    if (msg) msg.textContent = "";
  } catch (e) {
    if (msg) msg.textContent = "进店失败：" + e.message;
    toast("进店失败：" + e.message);
  }
}

function renderShopTabs(shops) {
  const box = $("shop-tabs");
  if (!box) return;
  box.innerHTML = (shops || []).map((s) =>
    `<button type="button" class="btn mini ${s.id === S.shopId ? "primary" : "ghost"} shop-tab"`
    + ` data-shop="${escapeHtml(String(s.id))}">`
    + `${escapeHtml(String(s.name || s.id))}</button>`).join("");
}

function renderShop(view, fullState) {
  const desc = $("shop-desc");
  const stock = $("shop-stock");
  const funds = $("shop-funds");
  if (!view) {
    if (desc) desc.textContent = "货架是空的。";
    if (stock) stock.innerHTML = "";
    return;
  }
  if (desc) {
    desc.innerHTML = `<div class="shop-scene">${escapeHtml(String(view.scene || ""))}</div>`
      + `<div class="shop-owner">店主：${escapeHtml(String(view.owner || "—"))}` +
      (view.greeting ? `　“${escapeHtml(String(view.greeting))}”` : "") + `</div>`;
  }
  const bag = (((fullState || {}).coc || {}).inventory || {}).backpack || [];
  const owned = new Set(bag.map((x) => String(x.id)));
  if (funds) funds.textContent = `持有 ${view.funds} FND · 本轮第 ${view.reroll + 1} 次进货`;
  if (stock) {
    stock.innerHTML = (view.stock || []).map((it) => {
      const have = it.owned || owned.has(String(it.id));
      const dis = have ? " disabled" : "";
      const tag = have ? `<small class="shop-owned">已在背包</small>`
        : `<small class="shop-desc">${escapeHtml(String(it.desc || ""))}</small>`;
      return `<div class="shop-item${have ? " shop-item-owned" : ""}">`
        + `<div class="shop-item-head"><b>${escapeHtml(String(it.name || it.id))}</b>`
        + `<em>${Number(it.price) || 0} FND</em></div>`
        + `<div class="shop-meta">${escapeHtml(String(it.rarity || "common"))}`
        + (it.san_restore ? ` · SAN +${escapeHtml(String(it.san_restore))}` : "")
        + (it.cures ? ` · 治${it.cures === "major" ? "重伤" : "轻伤"}` : "") + `</div>`
        + tag
        + `<button class="btn mini primary shop-buy" data-shop="${escapeHtml(String(view.id))}"`
        + ` data-item="${escapeHtml(String(it.id))}"${dis}>购买</button></div>`;
    }).join("") || `<div class="model-info">货架空了。可以重掷，或下次再来。</div>`;
  }
}

async function shopBuy(itemId, btn) {
  if (!S.sid || !S.shop) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/game/${S.sid}/coc/shop/buy`, {
      method: "POST",
      body: JSON.stringify({ shop: S.shop.id, item: itemId }),
    });
    if (r && r.coc) S.coc = r.coc.coc || S.coc;
    if (r && r.shop) S.shop = r.shop;
    renderShop(S.shop, r && r.coc);
    const msg = $("shop-msg");
    if (r && r.ok) {
      if (msg) msg.textContent = `✓ 买到「${r.name || itemId}」，花费 ${r.price} FND。`;
      toast(`已购买：${r.name || itemId}`, 2600);
    } else {
      const why = cocReasonCN(r.reason || "");
      if (msg) msg.textContent = "✗ 购买未生效：" + why;
      toast("购买未生效：" + why);
    }
  } catch (e) {
    toast("购买失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function shopReroll() {
  if (!S.sid || !S.shop) return;
  const btn = $("btn-shop-reroll");
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/game/${S.sid}/coc/shop/reroll`, {
      method: "POST",
      body: JSON.stringify({ shop: S.shop.id }),
    });
    if (r && r.coc) S.coc = r.coc.coc || S.coc;
    if (r && r.shop) S.shop = r.shop;
    renderShop(S.shop, r && r.coc);
    const msg = $("shop-msg");
    if (msg) msg.textContent = "✓ 重新进货（DOOM 已按规则增加）。";
    toast("已重新进货", 2400);
  } catch (e) {
    toast("重掷失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function cocSearchLoot() {
  if (!S.sid) return;
  const btn = $("btn-coc-search");
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/game/${S.sid}/coc/search`, {
      method: "POST",
      body: JSON.stringify({ location: "" }),
    });
    if (r && r.coc) S.coc = r.coc.coc || S.coc;
    renderCocPanel();
    const el = $("coc-loot-msg");
    if (r && r.ok) {
      toast(r.text || (r.found ? `找到 ${r.name}` : "什么也没找到"), 3200);
      if (el) el.textContent = r.text || "";
    } else {
      toast("搜刮失败：" + (r.reason || "未知原因"));
      if (el) el.textContent = "搜刮失败：" + (r.reason || "未知原因");
    }
  } catch (e) {
    toast("搜刮失败：" + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* —— UI 绑定段 bindUiPart41（t66 / U2：物品栏装备与卸下）——
   用**事件委托**挂在容器上：背包条目每次渲染都会重建，逐条 addEventListener
   会随渲染次数泄漏监听器；委托只绑一次，且天然覆盖后加的条目。
   绑定放进 bindUiPartNN（而非模块顶层）是为了不抬高顶层零缩进绑定棘轮。 */
function bindUiPart41() {

  const listBox = $("coc-inv-list");
  if (listBox) {
    listBox.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".coc-inv-act");
      if (!btn) return;
      ev.preventDefault();
      // t66 修（真缺陷）：背包列表里**既有「装备」也有「卸下」按钮**，而这里原先**写死 equip**
      // ⇒ 点"卸下"实际发出的是 {"action":"equip","item":""}（CDP fetch 记录实测：status=200
      // 但 reason=not_in_backpack），界面与后端都不变，玩家看到的是"点了没反应"。
      // 现在按按钮自己声明的 data-action 派发：卸下走 slot（契约 §9 的 coc-inv-unequip-<id>）。
      const action = String(btn.dataset.action || "equip");
      if (action === "use") {
        cocUseItem(btn.dataset.item || "", btn);
        return;
      }
      const act = (action === "unequip") ? "unequip" : "equip";
      cocInventoryAct(act,
        act === "unequip" ? { slot: btn.dataset.slot || "" }
                          : { item: btn.dataset.item || "" },
        btn);
    });
  }

  const slotBox = $("coc-inv-slots");
  if (slotBox) {
    slotBox.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".coc-inv-act");
      if (!btn) return;
      ev.preventDefault();
      cocInventoryAct("unequip", { slot: btn.dataset.slot || "" }, btn);
    });
  }

  // 1.9.19：SAN / 固定商店 / 搜刮
  on("btn-coc-san-check", "click", cocSanCheck);
  on("btn-coc-shop", "click", openShopCatalog);
  on("btn-coc-search", "click", cocSearchLoot);
  on("btn-shop-close", "click", () => $("modal-shop").classList.add("hidden"));
  on("btn-shop-reroll", "click", shopReroll);
  const shopTabs = $("shop-tabs");
  if (shopTabs) {
    shopTabs.addEventListener("click", (ev) => {
      const b = ev.target.closest(".shop-tab");
      if (b) openShopTab(b.dataset.shop || "");
    });
  }
  const shopStock = $("shop-stock");
  if (shopStock) {
    shopStock.addEventListener("click", (ev) => {
      const b = ev.target.closest(".shop-buy");
      if (b && !b.disabled) shopBuy(b.dataset.item || "", b);
    });
  }
}

/* ---------- 启动：恢复上次会话 ---------- */
/* t37：显式 UI 初始化入口。
   原先 124 条 DOM 绑定零缩进散落在模块顶层，任一 id 缺失会让**后续绑定整体失效**
   （脆性设计）；现在收进 bindUiPart01..40，由这里统一调用。
   单段失败不再连带后面失效：捕获后 console.error 并计数，跑完把结果写到
   <html data-tsf-ui-init="ok/total">（无 UI 影响，供自动化验证读取）。 */
function bindUi() {
  const parts = [
    bindUiPart01, bindUiPart02, bindUiPart03, bindUiPart04, bindUiPart05,
    bindUiPart06, bindUiPart07, bindUiPart08, bindUiPart09, bindUiPart10,
    bindUiPart11, bindUiPart12, bindUiPart13, bindUiPart14, bindUiPart15,
    bindUiPart16, bindUiPart17, bindUiPart18, bindUiPart19, bindUiPart20,
    bindUiPart21, bindUiPart22, bindUiPart23, bindUiPart24, bindUiPart25,
    bindUiPart26, bindUiPart27, bindUiPart28, bindUiPart29, bindUiPart30,
    bindUiPart31, bindUiPart32, bindUiPart33, bindUiPart34, bindUiPart35,
    bindUiPart36, bindUiPart37, bindUiPart38, bindUiPart39, bindUiPart40,
    bindUiPart41, bindUiPart42,
  ];
  const failed = [];
  for (const fn of parts) {
    try {
      fn();
    } catch (e) {
      failed.push(fn.name);
      console.error(`[tsf] UI 绑定段 ${fn.name} 失败：`, e);
    }
  }
  const ok = parts.length - failed.length;
  document.documentElement.dataset.tsfUiInit = `${ok}/${parts.length}`;
  console.info(`[tsf] UI 初始化：${ok}/${parts.length} 段绑定完成`
    + (failed.length ? `，失败段：${failed.join(",")}` : ""));
  return { ok, total: parts.length, failed };
}

(async function init() {
  bindUi();
  if (localStorage.getItem("galgame_mode")) setSetupMode(localStorage.getItem("galgame_mode"), true);
  refreshAnchorPreview();
  addCharCard();
  refreshMockBanner();
  loadPresetGrid();
  loadUniformPresets();
  applyStoryFromUrl();
  ensureSetupList();
  refreshSetupSlotList();
  // 版本与更新状态：一眼看出是否运行最新版、有无待装更新
  try {
    const v = await api("/api/version");
    S.version = v.version;
    if (window.UI2) UI2.setVersion();
  } catch { /* 忽略 */ }
  try {
    const u = await api("/api/update-state");
    if (u.update_ready) {
      toast("检测到新版本：请先点「⨯ 关闭游戏」，再把 update\\ 里的 TSF_Galgame.exe 覆盖到程序目录", 8000);
    }
  } catch { /* 忽略 */ }
  if (new URLSearchParams(location.search).get("debug") === "1") {
    setTimeout(openDebug, 900);
  }
  const sid = localStorage.getItem("galgame_sid");
  if (!sid) return;
  try {
    const state = await api(`/api/game/${sid}`);
    enterGame(state, { instant: true });
  } catch {
    localStorage.removeItem("galgame_sid");
  }
})();
