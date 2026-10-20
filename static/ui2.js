/* ============================================================================
   TSF Galgame · UI 2.0（1.8.0）
   ----------------------------------------------------------------------------
   独立的界面编排层（不改变 main.js 的任何既有绑定契约）：
     · 开局向导：左侧五步导航 / 准备度 / 步骤切换 / 摘要
     · 游戏顶栏：分组下拉菜单（点击可用）、绘画状态胶囊
     · 六段演变工坊：调用 /api/game/{sid}/stages*，逐段预览与单段重试
     · 性能与消耗：/api/game/{sid}/perf，节能/缓存开关
     · Ctrl+K 命令面板 / 自动播放 / 沉浸模式 / 键盘快捷键
   依赖 main.js 顶层暴露的全局：S、api、toast、nextLine、openSettings、
   Sched、escapeHtml。本文件在其后加载，二者共享同一全局词法环境。
   ========================================================================== */
(function () {
  "use strict";
  if (window.UI2) return;

  const STEPS = [
    { key: "story", title: "故事世界" },
    { key: "cast", title: "登场角色" },
    { key: "rules", title: "规则与系统" },
    { key: "tone", title: "外观与尺度" },
    { key: "files", title: "台词与档案" },
  ];

  const state = {
    step: "story",
    stageData: null,
    studioOpen: false,
    studioSig: "",
    perf: null,
    auto: false,
    autoTimer: null,
    immersive: false,
    palette: { open: false, items: [], active: 0 },
    lastTurnStageFetch: 0,
    bound: false,
  };

  function el(id) { return document.getElementById(id); }
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function esc(s) { return escapeHtml(String(s == null ? "" : s)); }
  function setText(id, text) { const n = el(id); if (n) n.textContent = text; }
  function clickId(id) {
    const node = el(id);
    if (node && !node.disabled) { node.click(); return true; }
    return false;
  }
  function isGameScreen() {
    const g = el("game-screen");
    return !!(g && g.classList.contains("active"));
  }
  function anyModalOpen() {
    return qsa(".modal").some(function (m) { return !m.classList.contains("hidden"); });
  }
  function openModal(id) { const m = el(id); if (m) m.classList.remove("hidden"); }
  function closeModal(id) { const m = el(id); if (m) m.classList.add("hidden"); }

  /* ---------------- 版本 ---------------- */
  function setVersion() {
    const v = (S && S.version) || "";
    if (!v) return;
    setText("rail-version", "v" + v);
    const tag = el("version-tag");
    if (tag) {
      const t = tag.textContent || "";
      // refreshSetupList 会写成「v<版本> · N 档案」；若它在版本号取回前执行，
      // 会留下「v · N 档案」。这里只替换前缀版本号，保留档案数（1.8.0 实测）。
      if (/档案/.test(t)) tag.textContent = t.replace(/^v[^\s·]*/, "v" + v);
      else tag.textContent = "v" + v;
    }
  }

  /* ---------------- 开局向导 ---------------- */
  function stepIndex(key) {
    for (let i = 0; i < STEPS.length; i++) if (STEPS[i].key === key) return i;
    return 0;
  }
  function setupGoStep(key, opts) {
    const idx = stepIndex(key);
    state.step = STEPS[idx].key;
    qsa(".setup-step").forEach(function (s) {
      s.classList.toggle("active", s.dataset.step === state.step);
    });
    qsa(".setup-nav-item").forEach(function (b) {
      b.classList.toggle("active", b.dataset.step === state.step);
    });
    setText("setup-step-label", "0" + (idx + 1) + " / 0" + STEPS.length + " · " + STEPS[idx].title);
    const prev = el("setup-prev"), next = el("setup-next");
    if (prev) prev.disabled = idx <= 0;
    if (next) next.disabled = idx >= STEPS.length - 1;
    try { localStorage.setItem("galgame_setup_step", state.step); } catch (e) { /* 忽略 */ }
    const main = el("setup-main");
    if (main && !(opts && opts.keepScroll)) {
      try { main.scrollTo({ top: 0, behavior: "smooth" }); } catch (e) { main.scrollTop = 0; }
    }
    updateReadiness();
  }
  function stepBy(delta) {
    const idx = Math.max(0, Math.min(STEPS.length - 1, stepIndex(state.step) + delta));
    setupGoStep(STEPS[idx].key);
  }

  function readSetup() {
    function val(id) { const n = el(id); return n ? String(n.value || "").trim() : ""; }
    const mode = (S && S.mode) ? S.mode : "tsf";
    // 只统计「填了名字」的同伴：开局默认会放一张空卡片，
    // 按卡片数统计会永远显示「同伴 1 人」并虚高准备度（1.8.0 实测）。
    let chars = 0;
    qsa("#char-list .char-card").forEach(function (c) {
      const n = c.querySelector(".char-name");
      if (n && String(n.value || "").trim()) chars++;
    });
    let lore = 0;
    const loreList = el("lore-list");
    if (loreList) lore = loreList.querySelectorAll(".lore-item").length;
    let directives = 0;
    qsa("#preset-grid input[type=checkbox]:checked").forEach(function () { directives++; });
    const sel = el("inp-rating");
    return {
      mode: mode,
      title: val("inp-title"),
      world: val("inp-world"),
      prot: val("prot-name"),
      vnAnchor: val("vn-anchor"),
      outline: val("inp-outline"),
      chars: chars,
      lore: lore,
      directives: directives,
      rating: sel ? sel.value : "all",
      catchphrases: val("inp-catchphrases"),
    };
  }
  function modeCN(mode) {
    return mode === "vn" ? "普通 Galgame"
      : mode === "coc" ? "CoC 跑团"
        : mode === "coc_tsf" ? "CoC×TSF" : "TSF Galgame";
  }
  function updateReadiness() {
    const d = readSetup();
    const isCoc = d.mode === "coc" || d.mode === "coc_tsf";
    let score = 0;
    if (d.world.length >= 4) score += 28;
    if (d.prot) score += 22;
    if (!isCoc || d.vnAnchor) score += 6;
    if (d.title) score += 8;
    if (d.chars > 0) score += 8;
    if (d.lore > 0) score += 8;
    if (d.outline) score += 10;
    if (d.directives > 0) score += 6;
    if (d.rating === "18") score += 4;
    score = Math.max(0, Math.min(100, score));
    setText("rail-pct", score + "%");
    const bar = el("rail-bar");
    if (bar) bar.style.width = score + "%";
    const hint = el("rail-hint");
    if (hint) {
      hint.textContent = !d.prot
        ? (isCoc ? "给调查员起个名字，就可以开始。" : "给主角起个名字，就可以开始。")
        : (!d.world ? "写一句世界观，画面与剧情会更稳。" : "准备就绪——随时可以开始。");
    }
    qsa(".setup-nav-item").forEach(function (b) {
      const k = b.dataset.step;
      let done = false;
      if (k === "story") done = d.world.length >= 4;
      else if (k === "cast") done = !!d.prot;
      else if (k === "rules") done = d.lore > 0 || d.directives > 0;
      else if (k === "tone") done = d.rating === "18";
      else done = !!d.outline;
      b.classList.toggle("done", done);
    });
    const sum = el("setup-summary");
    if (sum) {
      const chips = [
        ["模式", modeCN(d.mode)],
        ["标题", d.title || "未命名物语"],
        ["主角", d.prot || "待填写"],
        ["同伴", d.chars ? d.chars + " 人" : "无"],
        ["分级", d.rating === "18" ? "18+ 全解锁" : d.rating + "+"],
      ];
      sum.innerHTML = chips.map(function (c) {
        return '<span class="sum-chip">' + esc(c[0]) + ' <b>' + esc(c[1]) + '</b></span>';
      }).join("");
    }
    const actionHint = el("setup-action-hint");
    if (actionHint) {
      actionHint.textContent = score >= 70
        ? "准备度 " + score + "%：现在开局即可得到稳定的角色与世界观。"
        : "准备度 " + score + "%：也可以直接开始，AI 会用默认值补全。";
    }
  }
  function onModeChanged() {
    updateReadiness();
    setVersion();
  }

  /* ---------------- 顶栏菜单 ---------------- */
  function closeMenus() {
    qsa(".top-group").forEach(function (g) {
      g.classList.remove("open");
      const t = qs(".top-trigger", g);
      if (t) t.setAttribute("aria-expanded", "false");
    });
  }
  function bindTopMenus() {
    qsa(".top-group").forEach(function (g) {
      const trigger = qs(".top-trigger", g);
      if (!trigger) return;
      trigger.addEventListener("click", function (ev) {
        ev.stopPropagation();
        const wasOpen = g.classList.contains("open");
        closeMenus();
        if (!wasOpen) {
          g.classList.add("open");
          trigger.setAttribute("aria-expanded", "true");
        }
      });
      const menu = qs(".top-menu", g);
      if (menu) menu.addEventListener("click", function () { closeMenus(); });
    });
    document.addEventListener("click", function (ev) {
      if (!ev.target.closest || !ev.target.closest(".top-group")) closeMenus();
    });
  }

  /* ---------------- 六段轨道 ---------------- */
  function renderStageTrack() {
    const track = el("stage-track");
    if (!track) return;
    const tsf = S ? S.tsf : null;
    if (!tsf) return;
    const total = (state.stageData && state.stageData.stage_count) || 6;
    const stage = Number(tsf.stage || 0);
    const pct = Number(tsf.progress || 0);
    setText("stage-track-pct", pct + "%");
    const dots = el("stage-track-dots");
    if (dots) {
      let html = "";
      for (let i = 0; i < total; i++) {
        const cls = i < stage ? "done" : (i === stage ? "current" : "");
        html += '<i class="stage-seg ' + cls + '"></i>';
      }
      dots.innerHTML = html;
    }
    let name = String(tsf.stage_name || "").replace(/^.*?·\s*/, "");
    if (state.stageData && state.stageData.stages && state.stageData.stages[stage]) {
      name = state.stageData.stages[stage].name || name;
    }
    setText("stage-track-name", name || ("阶段 " + (stage + 1)));
  }

  function loadStages(force) {
    if (!S || !S.sid) return Promise.resolve(null);
    if (state.stageData && !force) return Promise.resolve(state.stageData);
    return api("/api/game/" + S.sid + "/stages").then(function (d) {
      state.stageData = d;
      renderStageTrack();
      if (state.studioOpen) renderStudio();
      syncArtStatus();
      return d;
    }).catch(function () { return null; });
  }

  /* ---------------- 六段演变工坊 ---------------- */
  function stageImg(entry) {
    const p = entry && entry.preview;
    if (!p || p.status !== "ready" || !p.file) return "";
    return "/img/" + p.file.split("/").map(encodeURIComponent).join("/");
  }
  function allReady(d) {
    const stages = (d && d.stages) || [];
    return stages.length > 0 && stages.every(function (s) {
      return s.preview && s.preview.status === "ready";
    });
  }
  function tourText(d) {
    const tour = (d && d.tour) || {};
    const st = tour.status || "idle";
    if (allReady(d)) return "全部六段已就绪（可直接查看/复用）";
    if (st === "running") {
      const cur = (tour.current == null || tour.current < 0) ? 1 : tour.current + 1;
      return "生成中 · 第 " + cur + " / " + (tour.total || d.stage_count || 6) + " 段";
    }
    if (st === "done") return "全部六段已就绪（可直接查看/复用）";
    if (st === "cancelled") return "已取消（已完成段保留）";
    if (st === "error") return "中断：" + (tour.error || "生成失败，可重试");
    if (st === "interrupted") return "上次生成被中断，可继续";
    return "等待开始 · 已生成段会自动复用";
  }
  function renderStudio() {
    const d = state.stageData;
    const grid = el("studio-grid");
    if (!grid) return;
    if (!d) {
      grid.innerHTML = '<div class="palette-empty">正在读取段表…</div>';
      return;
    }
    setText("studio-pct", (d.progress || 0) + "%");
    const ring = qs(".studio-ring");
    if (ring) ring.style.setProperty("--p", String(d.progress || 0));
    const sum = el("studio-summary");
    if (sum) {
      const ready = (d.stages || []).filter(function (s) {
        return s.preview && s.preview.status === "ready";
      }).length;
      const pend = (d.stages || []).filter(function (s) {
        return s.preview && s.preview.status === "pending";
      }).length;
      const cur = (d.stages || [])[d.stage] || {};
      sum.innerHTML =
        '<span class="sum-chip">主角 <b>' + esc(d.protagonist || "") + '</b></span>' +
        '<span class="sum-chip">当前 <b>' + esc(cur.name || ("阶段 " + (d.stage + 1))) + '</b></span>' +
        '<span class="sum-chip">已生成 <b>' + ready + ' / ' + (d.stage_count || 6) + '</b></span>' +
        (pend ? '<span class="sum-chip">生成中 <b>' + pend + '</b></span>' : '') +
        '<span class="sum-chip">' + (d.eco ? '节能 <b>开</b>' : '标准画质') + '</span>' +
        '<span class="sum-chip">' + (d.gen_cache ? '缓存 <b>开</b>' : '缓存 <b>关</b>') + '</span>';
    }
    const cards = (d.stages || []).map(function (s) {
      const p = s.preview || {};
      const st = p.status || "";
      const ready = st === "ready";
      const pending = st === "pending";
      const err = st === "error";
      const cls = "stage-card" + (s.current ? " current" : "") + (ready ? " ready" : "")
        + (pending ? " pending" : "") + (err ? " error" : "");
      const img = stageImg(s);
      const stateText = ready ? "已生成" : pending ? "生成中" : err ? "失败" : "未生成";
      // 只有 6 张缩略图：用 eager 而不是 lazy——工坊每次轮询会重建 DOM，
      // lazy 会让截图/打开瞬间出现一整排棋盘格（1.8.0 实测）。
      const empty = ready
        ? '<img loading="eager" decoding="async" src="' + esc(img) + '" alt="' + esc(s.name) + '">'
        : '<div class="stage-empty"><b>' + (pending ? "◌" : "+") + '</b><span>' +
          (pending ? "正在绘制…" : err ? "可重试" : "尚未生成") + '</span></div>';
      return '<article class="' + cls + '" data-stage="' + s.index + '">' +
        '<div class="stage-thumb">' +
        '<span class="stage-num">STAGE 0' + (s.index + 1) + '</span>' +
        '<span class="stage-state">' + stateText + '</span>' + empty +
        '</div>' +
        '<div class="stage-body">' +
        '<div class="stage-name">' + esc(s.name || ("第" + (s.index + 1) + "段")) +
        (s.current ? '<span class="stage-current-badge">当前</span>' : '') + '</div>' +
        '<div class="stage-identity">' + esc(s.identity || "") + '</div>' +
        '<p class="stage-desc">' + esc(s.desc || "") + '</p>' +
        '<div class="stage-meta"><span>代表同化率 ' + (s.progress || 0) + '%</span><span>' +
        (s.min != null ? s.min + "–" + s.max : "") + '</span></div>' +
        '<div class="stage-actions">' +
        '<button type="button" class="btn ghost stage-gen">' + (ready ? "↻ 重画此段" : "▶ 生成此段") + '</button>' +
        '<button type="button" class="btn primary stage-view"' + (ready ? "" : " disabled") + '>查看</button>' +
        '</div></div></article>';
    }).join("");
    // 只在「段状态 / 文件 / 进度」真的变化时才重建卡片：
    // 否则每次 2.4s 轮询都会销毁并重载 6 张缩略图（闪烁 + 重复解码）。
    const sig = JSON.stringify({
      p: d.progress,
      t: [((d.tour || {}).status || ""), ((d.tour || {}).current || -1),
          ((d.tour || {}).error || "")],
      s: (d.stages || []).map(function (s) {
        const p = s.preview || {};
        return [s.index, p.status || "", p.file || "", s.current ? 1 : 0];
      }),
    });
    if (sig !== state.studioSig) {
      state.studioSig = sig;
      grid.innerHTML = cards;
    }
    const tour = d.tour || {};
    const running = tour.status === "running" && !allReady(d);
    const runBtn = el("btn-stages-run");
    if (runBtn) runBtn.disabled = running;
    const cancelBtn = el("btn-stages-cancel");
    if (cancelBtn) cancelBtn.disabled = !running;
    const force = el("studio-force");
    if (force) force.disabled = running;
    setText("studio-eta", tourText(d));
    const bar = el("studio-bar");
    if (bar) {
      const total = d.stage_count || 6;
      const done = (d.stages || []).filter(function (s) {
        return s.preview && s.preview.status === "ready";
      }).length;
      bar.style.width = Math.min(100, Math.round((done / Math.max(1, total)) * 100)) + "%";
    }
    const hint = el("studio-hint");
    if (hint) {
      hint.textContent = (d.stages || []).some(function (s) {
        return s.preview && s.preview.status === "error";
      })
        ? "有段落生成失败：可单段重试，或开启「节能模式」降低显存压力。"
        : "逐段生成会复用已完成的图；强制重画会换种子重新绘制。";
    }
    syncArtStatus();
  }

  function studioPollOnce() {
    if (!state.studioOpen || !S || !S.sid) return Promise.resolve(null);
    return loadStages(true).then(function (d) {
      if (d && d.tour && d.tour.status !== "running" && Sched.isRunning("studio")) {
        Sched.stop("studio");
      }
      return d;
    });
  }
  function startStudioPoll() {
    if (!state.studioOpen) return;
    if (!Sched.isRunning("studio")) {
      Sched.define("studio", 2400, studioPollOnce);
      Sched.start("studio");
    }
    studioPollOnce();
  }
  function tsfMode() {
    return !!S && (S.mode === "tsf" || (!S.mode && true));
  }
  function openStudio() {
    if (!S || !S.sid) { toast("请先开始一局游戏", 2600); return; }
    if (!tsfMode()) {
      toast("六段演变工坊只用于 TSF Galgame 模式（当前模式没有阶段立绘管线）", 4200);
      return;
    }
    state.studioOpen = true;
    openModal("modal-stages");
    renderStudio();
    loadStages(true).then(function () { startStudioPoll(); });
  }
  function closeStudio() {
    state.studioOpen = false;
    closeModal("modal-stages");
    Sched.stop("studio");
  }
  function runStages(indices) {
    if (!S || !S.sid) return;
    const forceEl = el("studio-force");
    const payload = { force: !!(forceEl && forceEl.checked) };
    if (indices && indices.length) payload.indices = indices;
    const btn = el("btn-stages-run");
    if (btn) btn.disabled = true;
    api("/api/game/" + S.sid + "/stages/run", {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: 30000,
    }).then(function (r) {
      if (r && r.started) {
        toast(indices && indices.length
          ? "已开始生成选中段（游戏内引擎逐段绘制）"
          : "已开始生成六段全程——可继续看剧情，完成后回来查看", 5000);
      } else {
        toast("演变任务已在运行中", 2600);
      }
      startStudioPoll();
    }).catch(function (e) {
      toast("启动演变失败：" + e.message);
    }).finally(function () {
      if (btn) btn.disabled = false;
      loadStages(true);
    });
  }
  function cancelStages() {
    if (!S || !S.sid) return;
    api("/api/game/" + S.sid + "/stages/cancel", { method: "POST" })
      .then(function () { toast("已请求取消——已完成段会保留", 3200); startStudioPoll(); })
      .catch(function (e) { toast("取消失败：" + e.message); });
  }
  function viewStage(idx) {
    const d = state.stageData;
    if (!d || !d.stages || !d.stages[idx]) return;
    const s = d.stages[idx];
    const img = stageImg(s);
    if (!img) return;
    const box = el("modal-cg-view");
    if (!box) return;
    const im = el("cg-view-img");
    if (im) im.src = img;
    setText("cg-view-title", (s.name || "") + " · 代表同化率 " + (s.progress || 0) + "%");
    ["btn-cg-set-current", "btn-cg-del-hist", "btn-cg-regen-diff"].forEach(function (id) {
      const n = el(id);
      if (n) n.classList.add("hidden");
    });
    box.classList.remove("hidden");
  }

  /* ---------------- 性能与消耗 ---------------- */
  function fmtSec(v) {
    const n = Number(v || 0);
    if (n >= 120) return (n / 60).toFixed(1) + " 分";
    return n.toFixed(n >= 10 ? 0 : 1) + " 秒";
  }
  function perfTile(label, value, unit, hint) {
    return '<div class="perf-tile"><small>' + esc(label) + '</small><b>' + esc(value) +
      (unit ? '<em>' + esc(unit) + '</em>' : '') + '</b><span>' + esc(hint || "") + '</span></div>';
  }
  function renderPerf(d) {
    const grid = el("perf-grid");
    if (!grid) return;
    const se = d.session || {};
    const rt = d.runtime || {};
    grid.innerHTML =
      perfTile("本局出图", se.images || 0, "张", "完成并落盘的立绘/背景")
      + perfTile("本局失败", se.failed || 0, "次", "失败会自动回退旧图")
      + perfTile("本局平均耗时", d.avg_seconds || 0, "秒", "含排队时间")
      + perfTile("缓存命中", rt.cache_hits || 0, "次", "命中率 " + (rt.cache_hit_rate || 0) + "%")
      + perfTile("本次真实请求", rt.requests || 0, "次", "发往生图服务")
      + perfTile("缓存省下", fmtSec(rt.saved_seconds || 0), "", "按历史平均耗时估算")
      + perfTile("缓存占用", rt.cache_mb || 0, "MB", (rt.cache_entries || 0) + " 个成品")
      + perfTile("六段预演", (se.previews || 0) + " / " + (se.tour_runs || 0), "", "已生成段 / 运行次数");
    const eco = el("perf-eco");
    if (eco) eco.checked = !!d.eco;
    const cache = el("perf-cache");
    if (cache) cache.checked = d.gen_cache !== false;
    const mb = el("perf-cache-mb");
    if (mb) mb.value = d.gen_cache_mb || 384;
    const sd = el("perf-sd");
    if (sd) {
      const s = d.sd || {};
      const gen = s.generating;
      let text = "串行通道：Semaphore(1) · 一次只画一张\n";
      text += gen
        ? "正在生成：" + (gen.label || "未知任务") + "（已 " + fmtSec(gen.running_s) + "）\n"
        : "当前空闲\n";
      const waiting = s.waiting || [];
      text += waiting.length
        ? "排队 " + waiting.length + " 项：" + waiting.map(function (w) {
          return (w.label || w.kind || "任务") + " (" + fmtSec(w.queued_s) + ")";
        }).join(" · ")
        : "排队：无";
      sd.textContent = text;
    }
  }
  function refreshPerf() {
    if (!S || !S.sid) return Promise.resolve(null);
    return api("/api/game/" + S.sid + "/perf").then(function (d) {
      state.perf = d;
      renderPerf(d);
      return d;
    }).catch(function (e) {
      const sd = el("perf-sd");
      if (sd) sd.textContent = "读取失败：" + e.message;
      return null;
    });
  }
  function openPerf() {
    if (!S || !S.sid) { toast("请先开始一局游戏", 2600); return; }
    openModal("modal-perf");
    refreshPerf();
  }
  function savePerf() {
    if (!S || !S.sid) return;
    const eco = !!(el("perf-eco") && el("perf-eco").checked);
    const cache = !!(el("perf-cache") && el("perf-cache").checked);
    const mb = parseInt((el("perf-cache-mb") && el("perf-cache-mb").value) || "384", 10) || 384;
    setText("perf-hint", "保存中…");
    api("/api/config", {
      method: "POST",
      body: JSON.stringify({ config: { image: {
        eco_mode: eco, gen_cache: cache, gen_cache_mb: Math.max(64, Math.min(4096, mb)),
      } } }),
    }).then(function () {
      setText("perf-hint", "✓ 已保存，下一张图立即生效");
      refreshPerf();
      loadStages(true);
      const cfgEco = el("cfg-eco-mode");
      if (cfgEco) cfgEco.checked = eco;
      const cfgCache = el("cfg-gen-cache");
      if (cfgCache) cfgCache.checked = cache;
      const cfgMb = el("cfg-gen-cache-mb");
      if (cfgMb) cfgMb.value = mb;
      toast(eco ? "已开启节能模式：更省电、更稳，但细节略降" : "已切回标准画质", 3600);
    }).catch(function (e) {
      setText("perf-hint", "保存失败：" + e.message);
    });
  }

  /* ---------------- 命令面板 ---------------- */
  function commands() {
    return [
      { id: "studio", icon: "🧬", title: "六段演变工坊", hint: "用游戏内引擎生成完整性转变过程", keys: "", kw: "stage six 演變 六段 转变 工坊", run: openStudio },
      { id: "perf", icon: "📊", title: "性能与消耗", hint: "耗时 / 缓存命中 / 节能模式", keys: "", kw: "perf 性能 消耗 缓存 节能", run: openPerf },
      { id: "outfit", icon: "👗", title: "换装", hint: "为角色更换服装并重画", keys: "", kw: "outfit 换装 服装", run: function () { clickId("btn-outfit"); } },
      { id: "command", icon: "⚡", title: "命令面板", hint: "换装 / 强制行为 / CG", keys: "", kw: "command 命令 行为", run: function () { clickId("btn-command"); } },
      { id: "directives", icon: "✍", title: "指令注入", hint: "写作风格与内容尺度", keys: "", kw: "directive 指令 提示词", run: function () { clickId("btn-directives"); } },
      { id: "aiconsole", icon: "🧠", title: "AI 控制台", hint: "思考 / 请求 / 改对话 / 导演指令", keys: "", kw: "ai 控制台 思考 reasoning 对话 提示词", run: function () { clickId("btn-ai-console"); } },
      { id: "history", icon: "📜", title: "剧情历史", hint: "回看每一幕与选择", keys: "", kw: "history 历史 回看", run: function () { clickId("btn-history"); } },
      { id: "cg", icon: "🎨", title: "CG 回廊", hint: "重绘 / 刷新 / 截场景", keys: "", kw: "cg 回廊 画廊", run: function () { clickId("btn-cg"); } },
      { id: "savecg", icon: "📷", title: "存为 CG", hint: "把当前画面收进回廊", keys: "", kw: "cg 截图 存为", run: function () { clickId("btn-save-cg"); } },
      { id: "scene", icon: "🌄", title: "读取 / 更换场景", hint: "复用背景或重生成", keys: "", kw: "scene 场景 背景", run: function () { clickId("btn-scene-mgr"); } },
      { id: "save", icon: "💾", title: "立即存档", hint: "保存当前进度", keys: "", kw: "save 存档", run: function () { clickId("btn-save"); } },
      { id: "load", icon: "📂", title: "读档", hint: "读取全局存档", keys: "", kw: "load 读档", run: function () { clickId("btn-load"); } },
      { id: "regenprot", icon: "🔄", title: "重画主角立绘", hint: "可填写重画需求", keys: "", kw: "regen 重画 主角 立绘", run: function () { if (!clickId("btn-regen-prot")) clickId("btn-vn-regen-prot"); } },
      { id: "regenall", icon: "🔁", title: "重画全部立绘", hint: "主角 + 所有 NPC", keys: "", kw: "regen 全部 重画", run: function () { if (!clickId("btn-regen-all")) clickId("btn-vn-regen"); } },
      { id: "porthist", icon: "📚", title: "立绘历程", hint: "查看历史版本", keys: "", kw: "portrait 立绘 历史 历程", run: function () { if (!clickId("btn-prot-hist")) clickId("btn-vn-prot-hist"); } },
      { id: "compress", icon: "🧠", title: "压缩上下文", hint: "长局重置 AI 语境", keys: "", kw: "context 上下文 压缩", run: function () { clickId("btn-context-reset"); } },
      { id: "settings", icon: "⚙", title: "设置", hint: "API / 画质 / 线路", keys: "", kw: "settings 设置 配置", run: function () { openSettings(); } },
      { id: "auto", icon: "▶", title: "自动播放", hint: "逐句自动推进", keys: "A", kw: "auto 自动 播放", run: function () { toggleAuto(); } },
      { id: "immersive", icon: "◐", title: "沉浸模式", hint: "隐藏界面专注画面", keys: "H", kw: "immersive 沉浸 隐藏 ui", run: function () { toggleImmersive(); } },
      { id: "palette", icon: "⌘", title: "命令面板", hint: "Ctrl+K 随时呼出", keys: "Ctrl+K", kw: "palette 命令 面板", run: function () { openPalette(); } },
    ];
  }
  function renderPalette() {
    const list = el("palette-list");
    if (!list) return;
    const input = el("palette-input");
    const q = String((input && input.value) || "").trim().toLowerCase();
    const all = commands().filter(function (c) {
      if (!q) return true;
      return (c.title + " " + c.hint + " " + c.kw + " " + c.id).toLowerCase().indexOf(q) >= 0;
    });
    state.palette.items = all;
    if (!all.length) {
      list.innerHTML = '<div class="palette-empty">没有匹配的命令</div>';
      return;
    }
    if (state.palette.active >= all.length) state.palette.active = 0;
    list.innerHTML = all.map(function (c, i) {
      return '<button type="button" class="palette-item' + (i === state.palette.active ? " active" : "") +
        '" data-index="' + i + '"><span class="pi-ico">' + c.icon + '</span>' +
        '<span class="pi-copy"><b>' + esc(c.title) + '</b><small>' + esc(c.hint) + '</small></span>' +
        (c.keys ? '<span class="pi-key">' + esc(c.keys) + '</span>' : '') + '</button>';
    }).join("");
  }
  function openPalette() {
    state.palette.open = true;
    state.palette.active = 0;
    openModal("modal-palette");
    const input = el("palette-input");
    if (input) {
      input.value = "";
      renderPalette();
      setTimeout(function () { try { input.focus(); } catch (e) { /* 忽略 */ } }, 40);
    } else {
      renderPalette();
    }
  }
  function closePalette() {
    state.palette.open = false;
    closeModal("modal-palette");
  }
  function runPaletteIndex(i) {
    const c = state.palette.items[i];
    if (!c) return;
    closePalette();
    setTimeout(function () { try { c.run(); } catch (e) { toast("命令失败：" + e.message); } }, 30);
  }

  /* ---------------- 自动播放 / 沉浸 ---------------- */
  function autoBtn(on) {
    const b = el("btn-auto");
    if (!b) return;
    b.classList.toggle("on", !!on);
    const sp = b.querySelector("span");
    if (sp) sp.textContent = on ? "❚❚" : "▶";
  }
  function cancelAuto() {
    if (state.autoTimer) { clearTimeout(state.autoTimer); state.autoTimer = null; }
  }
  function scheduleAuto() {
    cancelAuto();
    if (!state.auto || !isGameScreen()) return;
    if (anyModalOpen()) return;
    if (S && S.typing) return;
    const choices = el("choices-wrap");
    if (choices && !choices.classList.contains("hidden")) return;
    state.autoTimer = setTimeout(function () {
      state.autoTimer = null;
      if (state.auto && !(S && S.typing) && !anyModalOpen()) nextLine();
    }, 1500);
  }
  function toggleAuto(force) {
    state.auto = (force === undefined) ? !state.auto : !!force;
    autoBtn(state.auto);
    if (state.auto) {
      toast("自动播放已开启（A 键切换）", 2000);
      scheduleAuto();
    } else {
      cancelAuto();
      toast("自动播放已关闭", 1600);
    }
  }
  function toggleImmersive(force) {
    state.immersive = (force === undefined) ? !state.immersive : !!force;
    document.body.classList.toggle("ui-immersive", state.immersive);
    const b = el("btn-hide-ui");
    if (b) b.classList.toggle("on", state.immersive);
    toast(state.immersive ? "沉浸模式：H 键或 Esc 恢复界面" : "界面已恢复", 2000);
  }

  /* ---------------- 绘画状态 ---------------- */
  function pendingCount() {
    if (!S || !S.assets) return 0;
    let n = 0;
    ["portraits", "backgrounds"].forEach(function (g) {
      const box = S.assets[g] || {};
      Object.keys(box).forEach(function (k) { if (box[k] && box[k].status === "pending") n++; });
    });
    return n;
  }
  function syncArtStatus() {
    const box = el("art-status");
    const txt = el("art-status-text");
    if (!box || !txt) return;
    const tour = state.stageData && state.stageData.tour;
    const pend = pendingCount();
    let cls = "art-status", text = "画面就绪";
    if (state.stageData && allReady(state.stageData)) {
      text = "六段已就绪";
    } else if (tour && tour.status === "running") {
      cls += " busy";
      const total = tour.total || 6;
      const cur = (tour.current == null || tour.current < 0) ? 1 : Math.min(total, tour.current + 1);
      text = "演变生成 " + cur + "/" + total;
    } else if (pend > 0) {
      cls += " busy";
      text = "立绘生成中 · " + pend;
    } else if (tour && tour.status === "done") {
      text = "六段已就绪";
    }
    box.className = cls;
    txt.textContent = text;
    box.title = "绘画线路：" + text + (state.stageData ? " · 同化率 " + state.stageData.progress + "%" : "");
  }

  /* ---------------- 生命周期钩子 ---------------- */
  function onEnterGame() {
    state.stageData = null;
    state.studioSig = "";
    state.lastTurnStageFetch = 0;
    setVersion();
    autoBtn(state.auto);
    document.body.classList.toggle("ui-immersive", state.immersive);
    // 六段工坊 / 快捷按钮只在 TSF 模式出现（VN/CoC 没有阶段立绘管线）
    const show = tsfMode();
    ["btn-stage-studio", "btn-stage-fab"].forEach(function (id) {
      const n = el(id);
      if (n) n.classList.toggle("hidden", !show);
    });
    if (show) loadStages(true);
    else syncArtStatus();
  }
  function onTurn() {
    syncArtStatus();
    if (!tsfMode()) return;
    renderStageTrack();
    const now = Date.now();
    if (now - state.lastTurnStageFetch > 4000) {
      state.lastTurnStageFetch = now;
      loadStages(true);
    }
  }
  function onTypingDone() { scheduleAuto(); }

  /* ---------------- 键盘 ---------------- */
  function isEditable(target) {
    if (!target) return false;
    const tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
  }
  function bindKeys() {
    document.addEventListener("keydown", function (ev) {
      const mod = ev.ctrlKey || ev.metaKey;
      if (mod && ev.key && ev.key.toLowerCase() === "k") {
        ev.preventDefault();
        if (state.palette.open) closePalette(); else openPalette();
        return;
      }
      if (ev.key === "Escape") {
        if (state.palette.open) { closePalette(); return; }
        if (state.studioOpen) { closeStudio(); return; }
        closeMenus();
        if (anyModalOpen()) {
          const modals = qsa(".modal").filter(function (m) { return !m.classList.contains("hidden"); });
          if (modals.length) modals[modals.length - 1].classList.add("hidden");
          return;
        }
        if (state.immersive) toggleImmersive(false);
        return;
      }
      if (isEditable(ev.target)) return;
      if (anyModalOpen()) return;
      if (!isGameScreen()) return;
      const tag = (ev.target.tagName || "").toLowerCase();
      if ((ev.key === " " || ev.key === "Enter") && tag !== "button" && tag !== "a") {
        ev.preventDefault();
        nextLine();
        return;
      }
      if (mod && ev.key && ev.key.toLowerCase() === "s") {
        ev.preventDefault();
        if (!clickId("btn-save")) toast("当前界面没有可用的存档按钮", 2200);
        return;
      }
      if (mod || ev.altKey) return;
      const k = (ev.key || "").toLowerCase();
      if (k === "a") { ev.preventDefault(); toggleAuto(); }
      else if (k === "h") { ev.preventDefault(); toggleImmersive(); }
      else if (k === "c") { ev.preventDefault(); if (!clickId("btn-stage-studio")) openStudio(); }
    });
  }

  /* ---------------- 绑定 ---------------- */
  function bind() {
    if (state.bound) return;
    state.bound = true;
    qsa(".setup-nav-item").forEach(function (b) {
      b.addEventListener("click", function () { setupGoStep(b.dataset.step); });
    });
    const prev = el("setup-prev"), next = el("setup-next");
    if (prev) prev.addEventListener("click", function () { stepBy(-1); });
    if (next) next.addEventListener("click", function () { stepBy(1); });
    const setupScreen = el("setup-screen");
    if (setupScreen) {
      setupScreen.addEventListener("input", updateReadiness);
      setupScreen.addEventListener("change", updateReadiness);
    }
    bindTopMenus();
    const map = {
      "btn-stage-studio": openStudio,
      "btn-stage-fab": openStudio,
      "stage-track-open": openStudio,
      "btn-stages-close": closeStudio,
      "btn-stages-run": function () { runStages(null); },
      "btn-stages-cancel": cancelStages,
      "btn-perf": openPerf,
      "btn-studio-perf": openPerf,
      "btn-perf-close": function () { closeModal("modal-perf"); },
      "btn-perf-refresh": refreshPerf,
      "btn-perf-save": savePerf,
      "btn-open-perf": openPerf,
      "btn-palette": openPalette,
      "btn-auto": function () { toggleAuto(); },
      "btn-hide-ui": function () { toggleImmersive(); },
      "btn-credits": function () { openModal("modal-credits"); },
    };
    Object.keys(map).forEach(function (id) {
      const n = el(id);
      if (!n) return;
      n.addEventListener("click", function (ev) {
        if (id === "stage-track-open" || id === "btn-stage-fab") ev.preventDefault();
        try { map[id](); } catch (e) { console.error("[ui2] action failed", id, e); }
      });
    });
    const grid = el("studio-grid");
    if (grid) {
      grid.addEventListener("click", function (ev) {
        const card = ev.target.closest ? ev.target.closest(".stage-card") : null;
        if (!card) return;
        const idx = parseInt(card.dataset.stage, 10);
        if (isNaN(idx)) return;
        if (ev.target.closest(".stage-gen")) { runStages([idx]); return; }
        if (ev.target.closest(".stage-view")) { viewStage(idx); }
      });
    }
    ["modal-stages", "modal-perf", "modal-palette"].forEach(function (id) {
      const m = el(id);
      if (!m) return;
      m.addEventListener("click", function (ev) {
        if (ev.target !== m) return;
        if (id === "modal-stages") closeStudio();
        else if (id === "modal-palette") closePalette();
        else m.classList.add("hidden");
      });
    });
    const pin = el("palette-input");
    if (pin) {
      pin.addEventListener("input", function () { state.palette.active = 0; renderPalette(); });
      pin.addEventListener("keydown", function (ev) {
        if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          ev.preventDefault();
          const max = state.palette.items.length - 1;
          state.palette.active = ev.key === "ArrowDown"
            ? Math.min(max, state.palette.active + 1)
            : Math.max(0, state.palette.active - 1);
          renderPalette();
          const act = qs(".palette-item.active", el("palette-list"));
          if (act && act.scrollIntoView) act.scrollIntoView({ block: "nearest" });
        } else if (ev.key === "Enter") {
          ev.preventDefault();
          runPaletteIndex(state.palette.active);
        }
      });
    }
    const plist = el("palette-list");
    if (plist) {
      plist.addEventListener("click", function (ev) {
        const item = ev.target.closest ? ev.target.closest(".palette-item") : null;
        if (!item) return;
        runPaletteIndex(parseInt(item.dataset.index, 10));
      });
    }
    bindKeys();
    let saved = "story";
    try { saved = localStorage.getItem("galgame_setup_step") || "story"; } catch (e) { /* 忽略 */ }
    setupGoStep(saved, { keepScroll: true });
    setVersion();
    renderStageTrack();
    syncArtStatus();
  }

  /* ---------------- 启动 ---------------- */
  function start() {
    bind();
    // 版本号由 main.js 异步取回；这里补一次（不阻塞、不报错）
    api("/api/version").then(function (v) {
      if (v && v.version) {
        if (S) S.version = v.version;
        setVersion();
      }
    }).catch(function () { /* 忽略 */ });
  }
  window.UI2 = {
    bind: bind,
    onModeChanged: onModeChanged,
    onEnterGame: onEnterGame,
    onTurn: onTurn,
    onTypingDone: onTypingDone,
    cancelAuto: cancelAuto,
    syncArtStatus: syncArtStatus,
    setVersion: setVersion,
    openStudio: openStudio,
    openPerf: openPerf,
    openPalette: openPalette,
    toggleAuto: toggleAuto,
    toggleImmersive: toggleImmersive,
    setupGoStep: setupGoStep,
    updateReadiness: updateReadiness,
    state: state,
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    setTimeout(start, 0);
  }
})();
