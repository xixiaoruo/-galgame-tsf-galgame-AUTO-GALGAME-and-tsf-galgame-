/* 自动化AI Galgame 前端逻辑 */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---------- 全局状态 ---------- */
const S = {
  sid: null,
  turn: null,
  protName: "",
  npcs: [],
  tsf: null,           // 状态面板数据
  vn: null,            // 普通模式状态数据
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
  el.textContent = msg;
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
  try {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json" },
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
on("prot-extra", "input", refreshAnchorPreview);

/* ---------- 开始界面：NPC 卡片 ---------- */

function addCharCard(data = {}) {
  const list = $("char-list");
  const card = document.createElement("div");
  card.className = "char-card";
  card.innerHTML = `
    <div class="char-card-head">
      <input class="text-input char-name" placeholder="角色名" maxlength="12" value="${data.name || ""}">
      <button class="btn ghost btn-del">✕ 删除</button>
    </div>
    <div class="char-field">
      <small>外貌描述（供立绘生成：发色发型、瞳色、服装、气质）</small>
      <textarea class="text-input char-appearance" rows="2" maxlength="300">${data.appearance || ""}</textarea>
    </div>
    <div class="char-field">
      <small>性格 / 说话风格</small>
      <textarea class="text-input char-personality" rows="2" maxlength="200">${data.personality || ""}</textarea>
    </div>`;
  card.querySelector(".btn-del").addEventListener("click", () => card.remove());
  list.appendChild(card);
}

function collectNPCs() {
  return [...document.querySelectorAll(".char-card")].map((c) => ({
    name: c.querySelector(".char-name").value.trim(),
    appearance: c.querySelector(".char-appearance").value.trim(),
    personality: c.querySelector(".char-personality").value.trim(),
  })).filter((c) => c.name);
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

$("btn-add-preset").addEventListener("click", () => {
  const text = $("preset-new").value.trim();
  if (!text) { toast("请输入指令内容"); return; }
  setupDirectives.customs.push({ id: "", text, enabled: true });
  $("preset-new").value = "";
  renderPresetCustoms();
});

function collectDirectives() {
  return [
    ...setupDirectives.presets.map((p) => ({ id: p.id, enabled: p.enabled })),
    ...setupDirectives.customs.map((c) => ({ id: "", text: c.text, enabled: true })),
  ];
}

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
  if (!world) { toast("请先填写世界观/剧情设定，或点「随机生成设定」"); return; }
  const payload = {
    mode: S.mode,
    title: $("inp-title").value.trim(),
    world,
    characters: collectNPCs(),
    protagonist: {
      name: $("prot-name").value.trim() || "主角",
      anchor: S.mode === "vn" ? $("vn-anchor").value.trim() : buildAnchor(),
    },
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
    tsf_target: collectTsfTarget(),
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
$("btn-credits-game").addEventListener("click", () => $("modal-credits").classList.remove("hidden"));
$("btn-credits-close").addEventListener("click", () => $("modal-credits").classList.add("hidden"));

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
$("btn-shutdown").addEventListener("click", shutdownGame);
$("btn-shutdown-setup").addEventListener("click", shutdownGame);

/* ---------- 模式切换（TSF / 普通 Galgame） ---------- */

function setSetupMode(mode, silent) {
  S.mode = (mode === "vn") ? "vn" : "tsf";
  $("mode-btn-tsf").classList.toggle("on", S.mode === "tsf");
  $("mode-btn-vn").classList.toggle("on", S.mode === "vn");
  localStorage.setItem("galgame_mode", S.mode);
  // 模式差异：TSF 用锚点构建器（发色/瞳色/服装/气质+预览）；普通模式只需一段外貌描述
  const isVn = S.mode === "vn";
  const vnBox = $("vn-anchor-box");
  if (vnBox) vnBox.classList.toggle("hidden", !isVn);
  const ap = document.querySelector(".anchor-preview");
  if (ap) ap.classList.toggle("hidden", isVn);
  const pg = document.querySelector(".prot-grid");   // class，非 id
  if (pg) pg.style.display = isVn ? "none" : "";
  const pe = $("prot-extra");
  if (pe) pe.style.display = isVn ? "none" : "";
  const pt = $("prot-head-title");
  if (pt) pt.textContent = isVn
    ? "② 主角设定（普通故事的主角）"
    : "② 主角设定（转变将从 TA 开始）";
  // 1.7.33 最终变化样式面板：仅 TSF 模式显示
  const tgtPanel = $("tsf-target-panel");
  if (tgtPanel) tgtPanel.style.display = isVn ? "none" : "";
  if (!silent) {
    toast(isVn
      ? "普通模式：心情 / 好感 / 性敏感 / 高潮度"
      : "TSF 模式：性别转换数值演进（同化/体质/沉溺…）", 2600);
  }
  renderStatEditor();
}
$("mode-btn-tsf").addEventListener("click", () => setSetupMode("tsf"));
$("mode-btn-vn").addEventListener("click", () => setSetupMode("vn"));

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
      <span class="stat-ico">${st.icon || "✦"}</span>
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

/* ---------- 设置弹窗 ---------- */

function openSettings() {
  api("/api/config").then(async ({ config, model_detected, llm_mock }) => {
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
    if ($("cfg-inject-outline")) $("cfg-inject-outline").checked = (config.game || {}).inject_outline !== false;
    $("cfg-vram-mode").value = config.image.vram_mode || "mid";
    if ($("cfg-wai-adapt")) $("cfg-wai-adapt").checked = config.image.wai_adapt !== false;
    if ($("cfg-tsf-explicit")) $("cfg-tsf-explicit").checked = !!config.image.tsf_explicit;
    if ($("cfg-tsf-plan-llm")) $("cfg-tsf-plan-llm").checked = config.image.tsf_plan_llm !== false;
    if ($("cfg-stage-style")) $("cfg-stage-style").value = config.image.stage_style || "auto";
    if ($("cfg-portrait-quality")) $("cfg-portrait-quality").value = config.image.portrait_quality || "mid";
    if ($("cfg-bg-quality")) $("cfg-bg-quality").value = config.image.background_quality || "mid";
    if ($("cfg-cg-quality")) $("cfg-cg-quality").value = config.image.cg_quality || "mid";
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
    loadInjects();
    $("modal-settings").classList.remove("hidden");
  }).catch((e) => toast("读取配置失败：" + e.message));
}

$("btn-open-settings").addEventListener("click", openSettings);
$("btn-settings-game").addEventListener("click", openSettings);
$("btn-settings-close").addEventListener("click", () => $("modal-settings").classList.add("hidden"));
$("btn-settings-exit").addEventListener("click", () => $("modal-settings").classList.add("hidden"));

/* ---------- ComfyUI 连接助手（设置页） ---------- */
let comfyDesktopApps = [];

function toggleComfyRow(provider) {
  const row = $("cfg-img-comfy-row");
  if (row) row.classList.toggle("hidden", provider !== "comfyui");
}

if ($("cfg-img-provider")) {
  $("cfg-img-provider").addEventListener("change", () =>
    toggleComfyRow($("cfg-img-provider").value));
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
      tsf_explicit: $("cfg-tsf-explicit") ? $("cfg-tsf-explicit").checked : false,
      tsf_plan_llm: $("cfg-tsf-plan-llm") ? $("cfg-tsf-plan-llm").checked : true,
      stage_style: $("cfg-stage-style") ? $("cfg-stage-style").value : "auto",
      portrait_quality: $("cfg-portrait-quality").value,
      background_quality: $("cfg-bg-quality").value,
      cg_quality: $("cfg-cg-quality").value,
      pink_rim: true,
    },
    game: {
      portrait_update: $("cfg-portrait-update").value,
      style_mode: $("cfg-style-mode").value,
      inject_outline: $("cfg-inject-outline").checked,
    },
  };
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify({ config }) });
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

$("btn-add-inject").addEventListener("click", () => {
  const text = $("inject-new").value.trim();
  if (!text) { toast("请输入注入文本"); return; }
  injectState.items.push({ id: "", name: "注入", description: "", text, enabled: true });
  $("inject-new").value = "";
  renderInjects();
});

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
  clearInterval(updateTimer);
  updateTimer = setInterval(async () => {
    try {
      const st = await api("/api/update/status");
      renderUpdateStatus(st);
      if (st.status === "done") {
        clearInterval(updateTimer);
        toast(st.msg, 8000);
      } else if (st.status === "error") {
        clearInterval(updateTimer);
      }
    } catch (e) {
      clearInterval(updateTimer);
      hint.textContent = "进度查询中断：" + e.message;
    }
  }, 1200);
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

async function refreshMockBanner() {
  try {
    const st = await api("/api/config");
    const mock = !st.llm_ready || !st.image_ready;
    $("mock-banner").classList.toggle("hidden", !mock);
  } catch { /* 忽略 */ }
}

/* ---------- 进入游戏 ---------- */

function enterGame(state, { instant }) {
  S.sid = state.sid;
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
  // 按模式切换右侧状态面板：TSF 数值面板 / 普通模式心情面板
  const isVn = S.mode === "vn";
  $("tsf-panel").classList.toggle("hidden", isVn);
  $("vn-panel").classList.toggle("hidden", !isVn);
  $("tsf-tab").classList.add("hidden");
  $("vn-tab").classList.add("hidden");
  buildSprites();
  renderStatsPanels();
  if (state.llm_degraded) {
    toast("对话 API 不可用，已切换演示剧情（可在设置中修正 Key）", 5000);
  }
  playTurn(state, { instant });
  startPolling();
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
      <div class="sprite-placeholder">${c.name}<br>${c.kind === "prot" ? "阶段立绘生成中…" : "立绘生成中…"}</div>
      <img alt="${c.name}">
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
if ($("regen-char")) {
  $("regen-char").addEventListener("change", updateRegenInitialRow);
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

$("btn-regen-go").addEventListener("click", confirmRegen);
$("btn-regen-cancel").addEventListener("click", () =>
  $("modal-regen").classList.add("hidden"));

function regenerateSprite(target) {
  if (!S.sid) return;
  openRegenModal(target);
}
$("btn-vn-regen-prot").addEventListener("click", () => openRegenModal("protagonist"));
$("btn-vn-regen").addEventListener("click", () => openRegenModal("all"));

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
      <div class="tsf-row-label"><span class="tsf-ico">${s.icon}</span>${s.name}
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

function renderStatsPanels() {
  if (S.mode === "vn") renderVnPanel();
  else renderTsfPanel();
}

/* ---------- TSF 状态面板（主角：转变数值 + 自己的四项；其他角色：各自四项）---------- */

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
}

/* ---------- 普通模式面板（每个角色各自的心情 / 好感 / 性敏感 / 高潮度）---------- */

function renderVnPanel() {
  renderWhoChips("vn-who");
  const box = $("vn-bars");
  const who = selectedChar();
  if (who) {
    renderStatRows(box, who.states, (S.charPrev || {})[who.name]);
    return;
  }
  // 兼容旧服务端（没有 char_stats 时退回单一数值集）
  renderStatRows(box, (S.vn && S.vn.states) || []);
}

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
$("btn-open-folder").addEventListener("click", openPortraitFolder);
$("btn-vn-open-folder").addEventListener("click", openPortraitFolder);

function forceReloadSprites() {
  S.imgVersion = (S.imgVersion || 0) + 1;
  document.querySelectorAll(".sprite img").forEach((im) => {
    im.dataset.url = "";
    im.classList.remove("show");
  });
  const line = S.turn && S.turn.dialogue[S.lineIdx];
  if (line) updateSprites(line.character, line.emotion);
}

$("btn-regen-prot").addEventListener("click", () => {
  regenerateSprite("protagonist");
});

$("btn-regen-all").addEventListener("click", () => {
  regenerateSprite("all");
});

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
$("btn-recut").addEventListener("click", recutAllNow);
$("btn-vn-recut").addEventListener("click", recutAllNow);

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
$("btn-restore-raw").addEventListener("click", restoreRawNow);
$("btn-vn-restore-raw").addEventListener("click", restoreRawNow);

async function exportToLibrary() {
  try {
    const r = await api(`/api/game/${S.sid}/library`, { method: "POST" });
    toast(`角色库已更新：${r.characters} 位角色 · ${r.portraits} 张立绘`, 3400);
  } catch (e) {
    toast("角色库导出失败：" + e.message);
  }
}
$("btn-library").addEventListener("click", exportToLibrary);
$("btn-vn-library").addEventListener("click", exportToLibrary);

async function exportSprites() {
  try {
    const r = await api(`/api/game/${S.sid}/export`, { method: "POST" });
    toast(`已导出 ${r.files} 张立绘（修图适配目录）`, 3200);
  } catch (e) {
    toast("导出失败：" + e.message);
  }
}
$("btn-export").addEventListener("click", exportSprites);
$("btn-vn-export").addEventListener("click", exportSprites);

/* ---------- 轮次播放 ---------- */

function playTurn(state, { instant }) {
  const prevTsf = S.tsf;
  const prevChars = charSnapshot();   // 更新前各角色数值，供面板算 ▲▼
  S.turn = state;
  S.tsf = state.tsf;
  S.vn = state.vn;
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
    }
  };
  step();
}

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
  $("next-hint").classList.add("hidden");
  const wrap = $("choices-wrap");
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
$("btn-reload-assets").addEventListener("click", reloadAssets);
$("btn-vn-reload-assets").addEventListener("click", reloadAssets);

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
  if (isProt && S.mode === "tsf") {
    // 主角（TSF 模式）：优先当前换装差分 → 阶段立绘 → 回退已有阶段
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
  if (img.dataset.url === url) return;
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
      img.classList.add("show");
      layer.querySelectorAll("img.show").forEach((old) => {
        if (old === img) return;
        old.classList.remove("show");
        setTimeout(() => old.remove(), 1100);
      });
      // 清理从未显示过的旧图（加载中被顶掉），避免元素堆积
      layer.querySelectorAll("img").forEach((stale) => {
        if (stale !== img && !stale.classList.contains("show")) stale.remove();
      });
    };
    layer.appendChild(img);
  }
}

/* ---------- 资源轮询与生成提示 ---------- */

function hasPending() {
  const groups = (S.assets && [S.assets.portraits, S.assets.backgrounds]) || [];
  return groups.some((g) => Object.values(g || {}).some((p) => p.status === "pending"));
}

function updateGenBanner() {
  const pendingPortraits = Object.values((S.assets && S.assets.portraits) || {})
    .some((p) => p.status === "pending");
  $("gen-banner").classList.toggle("hidden", !pendingPortraits);
}

function startPolling() {
  stopPolling();
  S.pollTimer = setInterval(async () => {
    if (!S.sid) { stopPolling(); return; }
    try {
      // 先拉取再决定是否停止：避免「最后一张就绪的资产永远没被刷到」
      const data = await api(`/api/game/${S.sid}/assets`);
      S.assets = data.assets;
      S.bgMap = data.bg_map || S.bgMap;
      const line = S.turn && S.turn.dialogue[S.lineIdx];
      if (line) updateSprites(line.character, line.emotion);
      applyBackground();
      const still = hasPending();
      updateGenBanner();
      if (!still) stopPolling();
    } catch { /* 会话失效等，静默 */ }
  }, 2000);
  updateGenBanner();
}

function stopPolling() {
  clearInterval(S.pollTimer);
  S.pollTimer = null;
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

function cutTarget() { return $("cut-char").value; }

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

$("btn-directives").addEventListener("click", openDirectives);
$("btn-directives-close").addEventListener("click", () =>
  $("modal-directives").classList.add("hidden"));


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

$("btn-command").addEventListener("click", openCommand);
$("btn-command-close").addEventListener("click", () =>
  $("modal-command").classList.add("hidden"));

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

let cgRefreshTimer = null;

function openCgGallery() {
  $("modal-cg").classList.remove("hidden");
  refreshCgList();
  // 生成中的桥段 CG 就绪后刷新展示
  clearInterval(cgRefreshTimer);
  cgRefreshTimer = setInterval(refreshCgList, 2500);
}

function closeCgGallery() {
  clearInterval(cgRefreshTimer);
  cgRefreshTimer = null;
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

/* ---------- 快进 ---------- */

let ffTimer = null;
$("btn-fastforward").addEventListener("click", () => {
  if (ffTimer) {
    clearInterval(ffTimer);
    ffTimer = null;
    $("btn-fastforward").textContent = "⏩ 快进";
    return;
  }
  $("btn-fastforward").textContent = "⏸ 快进中";
  ffTimer = setInterval(() => {
    if (!$("choices-wrap").classList.contains("hidden")) {
      clearInterval(ffTimer);
      ffTimer = null;
      $("btn-fastforward").textContent = "⏩ 快进";
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
  }, 160);
});

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
    const data = await api(`/api/slots`);
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
          S.assets = state.assets;
          S.bgMap = state.bg_map || S.bgMap;
          playTurn(state, { instant: true });
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
    refreshSlotList();
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
        .map((d) => `<div class="history-line">${d.character !== "旁白" ? `<b>${d.character}：</b>` : ""}${d.text}</div>`)
        .join("");
      item.innerHTML = `<div class="history-scene">◆ ${h.scene}</div>${lines}` +
        (h.choice ? `<div class="history-choice">➤ 你选择了：${h.choice}</div>` : "");
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
// Esc：关闭当前打开的弹窗（设置页等；依赖原生 confirm 的还原操作不受影响）
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const open = document.querySelector(".modal:not(.hidden)");
    if (open) open.classList.add("hidden");
  }
});
$("btn-debug-close").addEventListener("click", closeDebug);
$("btn-debug-open").addEventListener("click", (e) => {
  e.preventDefault();
  closeSettingsIfOpen();
  openDebug();
});
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

/* ---------- 主界面：我的存档（读取/删除，跨会话） ---------- */

async function refreshSetupSlotList() {
  const list = $("slot-list-setup");
  if (!list) return;
  try {
    const data = await api("/api/slots");
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
window.__libImports = [];

function rememberLibImport(roleName, name, dir) {
  if (!$("libpick-reuse").checked) return;
  const arr = window.__libImports;
  if (arr.some((x) => x.name === name && x.role === roleName && x.dir === dir)) return;
  arr.push({ name, role: roleName, dir: dir || "" });
}

async function openLibPicker(target) {
  libPickTarget = target;
  $("libpick-hint").textContent = target === "prot"
    ? "选中后自动填入主角设定（外貌并入「外貌补充」，保证立绘一致）"
    : "选中后自动新增一张角色卡；库内已有立绘将直接复用";
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
      card.innerHTML = `
        <img src="/api/library/${encodeURIComponent(c.name)}/preview?dir=${encodeURIComponent(c.dir || "")}" alt="" loading="lazy">
        <div class="cg-caption">${escapeHtml(c.name)}
          ${c.dir && c.dir !== c.name ? `<span class="cg-type">${escapeHtml(c.dir)}</span>` : ""}
          <span class="cg-type">${c.role === "主角" ? "主角" : "NPC"}</span>
          <span class="cg-type">${(c.variants || []).length} 立绘</span>
          ${c.source_game ? `<span class="cg-type">来自「${escapeHtml(c.source_game)}」</span>` : ""}
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
            addCharCard({ name: c.name, appearance: c.appearance || c.anchor || "",
                          personality: c.personality || "" });
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
on("btn-lib-pick-prot", "click", () => openLibPicker("prot"));
on("btn-lib-pick-npc", "click", () => openLibPicker("npc"));
on("btn-libpick-close", "click", () => $("modal-libpick").classList.add("hidden"));

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
        window.open(`/api/archives/${encodeURIComponent(a.file)}/download`, "_blank");
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

/* ---------- 启动：恢复上次会话 ---------- */
(async function init() {
  if (localStorage.getItem("galgame_mode")) setSetupMode(localStorage.getItem("galgame_mode"), true);
  refreshAnchorPreview();
  addCharCard();
  refreshMockBanner();
  loadPresetGrid();
  applyStoryFromUrl();
  ensureSetupList();
  refreshSetupSlotList();
  // 版本与更新状态：一眼看出是否运行最新版、有无待装更新
  try {
    const v = await api("/api/version");
    S.version = v.version;
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
