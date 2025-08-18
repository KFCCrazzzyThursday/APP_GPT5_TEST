(() => {
  "use strict";

  // ---- bridge helpers ----
  function getApi() {
    return window.pywebview && window.pywebview.api
      ? window.pywebview.api
      : null;
  }
  async function safeCall(fn, ...args) {
    try {
      if (typeof fn !== "function") return null;
      return await fn(...args);
    } catch (e) {
      console.error(e);
      addBubble("⚠️ " + (e?.message || String(e)), false);
      return null;
    }
  }
  async function callApi(name, ...args) {
    const api = getApi();
    if (!api || typeof api[name] !== "function") return null;
    return await safeCall(api[name].bind(api), ...args);
  }

  // ---- UI helpers ----
  function addBubble(text, me = false) {
    const tl = document.getElementById("timeline");
    const d = document.createElement("div");
    d.className = "bubble " + (me ? "me" : "bot");
    d.textContent = text;
    tl.appendChild(d);
    tl.scrollTop = tl.scrollHeight;
  }
  function log(s) {
    const lb = document.getElementById("logbox");
    lb.textContent += s + "\n";
    lb.scrollTop = lb.scrollHeight;
  }
  function setButtonsEnabled(container, enabled) {
    const el =
      typeof container === "string"
        ? document.getElementById(container)
        : container;
    if (!el) return;
    el.classList.toggle("is-busy", !enabled);
    el.querySelectorAll("button").forEach((b) => (b.disabled = !enabled));
  }
  function setQuickActionsEnabled(enabled) {
    document
      .querySelectorAll(".quick-actions button")
      .forEach((b) => (b.disabled = !enabled));
  }

  // ---- refs
  const storePathEl = document.getElementById("store-path");
  const recentSelect = document.getElementById("recent-stores");
  const dailySizeEl = document.getElementById("daily-size");
  const reviewScoreLimitEl = document.getElementById("review-score-limit");
  const ghMode = document.getElementById("gh-mode");
  const ghGroup = document.getElementById("gh-group");

  const input = document.getElementById("input");
  document.getElementById("btn-send").addEventListener("click", send);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    addBubble(text, true);
    const r = await callApi("send_message", text);
    if (!r) return;
    (r.logs || []).forEach((x) =>
      log(typeof x === "string" ? x : JSON.stringify(x))
    );
    addBubble(r.assistant || "(no reply)", false);
  }

  // ---- store choose / recent switch ----
  document
    .getElementById("btn-choose-store")
    .addEventListener("click", async () => {
      const r = await callApi("open_store_dialog", "open");
      if (r?.ok) {
        storePathEl.textContent = r.path || "(selected)";
        await refreshRecent();
        await refreshStats();
        await tryResume();
      } else if (r?.error) {
        addBubble("⚠️ " + r.error, false);
      }
    });

  document
    .getElementById("btn-switch-store")
    .addEventListener("click", async () => {
      const sel = recentSelect.value;
      if (!sel) return;
      const r = await callApi("switch_store", sel);
      if (r?.ok) {
        storePathEl.textContent = r.path;
        await refreshStats();
        await tryResume();
      } else {
        addBubble("⚠️ " + (r?.error || "切换失败"), false);
      }
    });

  async function refreshRecent() {
    const r = await callApi("list_recent_stores");
    recentSelect.innerHTML = "";
    if (r?.ok) {
      const items = r.items || [];
      items.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p;
        opt.textContent = p;
        recentSelect.appendChild(opt);
      });
      if (r.current) {
        storePathEl.textContent = r.current;
        const found = Array.from(recentSelect.options).find(
          (o) => o.value === r.current
        );
        if (found) found.selected = true;
      }
    }
  }

  // ------------------- planner/session -------------------
  const fc = {
    mode: "idle",
    masterWords: [],
    groupSize: 10,
    groupStart: 0,
    stage: "firstpass",
    activeWords: [],
    activeIndex: 0,
    seenStates: [],
    weakSet: new Set(),
    current: null,
    needVerify: false,
    frontBusy: false,
    backBusy: false,

    resetSeen(n) {
      this.seenStates = Array(n).fill("todo");
    },

    async startDailyPlan(n) {
      const r = await callApi("plan_daily_new", parseInt(n, 10));
      if (!r?.ok || !(r.items || []).length)
        return addBubble(
          "⚠️ No items for daily plan. Select a base first.",
          false
        );
      this._startWithList(
        r.items.map((x) => ({ word: x.word, entry: x.entry || null })),
        "learn"
      );
    },

    async startReviewToday() {
      const r = await callApi("sample_today_all");
      if (!r?.ok || !(r.items || []).length)
        return addBubble("今天还没有学过单词。", false);
      this._startWithList(
        r.items.map((x) => ({ word: x.word, entry: x.entry || null })),
        "review_today"
      );
    },

    async startReviewByScore(limit, learnedOnly = true) {
      const r = await callApi(
        "sample_by_score",
        parseInt(limit, 10),
        learnedOnly
      );
      if (!r?.ok || !(r.items || []).length)
        return addBubble("⚠️ 词库里没有可复习的条目。", false);
      this._startWithList(
        r.items.map((x) => ({ word: x.word, entry: x.entry || null })),
        "review_score"
      );
    },

    _startWithList(list, mode) {
      this.mode = mode;
      this.masterWords = list.slice();
      this.groupStart = 0;
      this._startGroup();
    },

    _startGroup() {
      const totalGroups = Math.max(
        1,
        Math.ceil(this.masterWords.length / this.groupSize)
      );
      const groupIdx = Math.floor(this.groupStart / this.groupSize) + 1;
      ghMode.textContent =
        this.mode === "learn"
          ? "Learning"
          : this.mode === "review_today"
          ? "Review Today"
          : "Review by Score";
      ghGroup.textContent = `Group ${groupIdx}/${totalGroups}`;
      const group = this.masterWords.slice(
        this.groupStart,
        this.groupStart + this.groupSize
      );
      this.stage = "firstpass";
      this.activeWords = group.slice();
      this.activeIndex = 0;
      this.weakSet.clear();
      this.resetSeen(this.activeWords.length);
      this._show(0);
      saveSession();
    },

    _startWeakLoop() {
      if (this.weakSet.size === 0) return this._nextGroup();
      this.stage = "weakloop";
      this.activeWords = Array.from(this.weakSet).map((w) => ({ word: w }));
      this.activeIndex = 0;
      this.resetSeen(this.activeWords.length);
      this._show(0);
      saveSession();
    },

    _nextGroup() {
      this.groupStart += this.groupSize;
      if (this.groupStart >= this.masterWords.length) this._finishPlan();
      else this._startGroup();
    },

    _finishPlan() {
      addBubble("🎯 本轮计划完成。", false);
      document.getElementById("session-banner").classList.add("hidden");
      saveSession(true);
      refreshStats();
    },

    async _show(i) {
      if (!this.activeWords.length) return this._finishPlan();
      this.activeIndex = Math.max(0, Math.min(i, this.activeWords.length - 1));
      this.current = this.activeWords[this.activeIndex];

      document.querySelector(".face-front").classList.remove("hidden");
      document.querySelector(".face-back").classList.add("hidden");
      document.getElementById("fc-word").textContent = this.current.word;
      setQuickActionsEnabled(true);
      this.backBusy = false;

      this._updateProgress();
      await this._ensureCardData(this.current);
      this._prefetchNext(3);

      await callApi(
        "record_signal_tool",
        this.current.word,
        "view_start",
        `${this.mode}:${this.stage}`
      );
      saveSession();
    },

    _flip() {
      document.querySelector(".face-front").classList.toggle("hidden");
      document.querySelector(".face-back").classList.toggle("hidden");
      this._updateBackButtons();
    },

    _updateBackButtons() {
      const showVerify = !!this.needVerify;
      document
        .getElementById("verify-actions")
        .classList.toggle("hidden", !showVerify);
      document
        .getElementById("next-actions")
        .classList.toggle("hidden", showVerify);
    },

    async markRemember(remembered) {
      if (this.frontBusy || this.backBusy) return;
      this.frontBusy = true;
      setQuickActionsEnabled(false);

      this.needVerify = !!remembered;
      const w = this.current;
      await callApi(
        "record_signal_tool",
        w.word,
        remembered ? "start_remember" : "start_forgot",
        `${this.mode}:${this.stage}`
      );
      if (!remembered) this.weakSet.add(w.word);

      await this._ensureCardData(w);
      this._renderBack(w);

      this._flip();
      this.frontBusy = false;
      saveSession();
    },

    _renderBack(w) {
      const ipa = (w.entry?.ipa || w.ipa || "").trim();
      document.getElementById("fc-back-word").textContent = ipa
        ? `${w.word}  /${ipa}/`
        : w.word;
      const m = w.meaning_zh || w.entry?.meaning_zh || "(meaning)";
      document.getElementById("fc-meaning").textContent = m;
      const phrases = w.entry?.phrases || [];
      document.getElementById("fc-phrases").innerHTML = phrases
        .map((p) => `<div>• ${p.phrase || p} — ${p.meaning_zh || ""}</div>`)
        .join("");
      const ex = w.entry?.example || {};
      document.getElementById("fc-example").innerHTML = `<p>${
        ex.en || ""
      }</p><p>${ex.zh || ""}</p>`;
    },

    async confirmCorrect(ok) {
      if (this.backBusy) return;
      this.backBusy = true;
      setButtonsEnabled("verify-actions", false);
      setButtonsEnabled("next-actions", false);

      const w = this.current;
      await callApi(
        "record_signal_tool",
        w.word,
        "verify",
        ok ? "verified_correct" : "verified_wrong"
      );
      this.seenStates[this.activeIndex] = ok ? "ok" : "weak";
      if (ok && this.stage === "weakloop") this.weakSet.delete(w.word);
      if (!ok) this.weakSet.add(w.word);

      // 关键修复：记录“今天学过该词”
      await callApi("note_learn_event", w.word);

      // 同步 SRS 与平均分：正确=1.0，错误=0.0
      await callApi("commit_review", w.word, ok ? 1.0 : 0.0);
      await callApi("update_score", w.word, ok ? 1.0 : 0.0);

      await refreshStats();
      this._updateProgress();
      await this._next();
      this.backBusy = false;
      setButtonsEnabled("verify-actions", true);
      setButtonsEnabled("next-actions", true);
    },

    async nextAfterShow() {
      if (this.backBusy) return;
      this.backBusy = true;
      setButtonsEnabled("verify-actions", false);
      setButtonsEnabled("next-actions", false);

      const w = this.current;
      await callApi("record_signal_tool", w.word, "verify", "verified_wrong");
      this.seenStates[this.activeIndex] = "weak";
      this.weakSet.add(w.word);

      // 同样记录“今天学过该词”（幂等）
      await callApi("note_learn_event", w.word);

      await callApi("commit_review", w.word, 0.0);
      await callApi("update_score", w.word, 0.0);

      await refreshStats();
      this._updateProgress();
      await this._next();
      this.backBusy = false;
      setButtonsEnabled("verify-actions", true);
      setButtonsEnabled("next-actions", true);
    },

    async _next() {
      if (this.activeIndex + 1 < this.activeWords.length)
        return this._show(this.activeIndex + 1);
      if (this.stage === "firstpass") return this._startWeakLoop();
      if (this.weakSet.size > 0) return this._startWeakLoop();
      return this._nextGroup();
    },

    _updateProgress() {
      const total = Math.max(1, this.activeWords.length);
      const idx = this.activeIndex + 1;
      document.getElementById("fc-progress").style.width =
        Math.round((idx * 100) / total) + "%";
      document.getElementById(
        "fc-progress-text"
      ).textContent = `${idx}/${total} (${
        this.stage === "firstpass" ? "Pass 1/1" : "Weak loop"
      })`;

      const el = document.getElementById("heat");
      el.innerHTML = "";
      for (let i = 0; i < this.seenStates.length; i++) {
        const s = this.seenStates[i] || "todo";
        const c = document.createElement("div");
        c.className = "cell " + s;
        el.appendChild(c);
      }
    },

    async _ensureCardData(w) {
      if (w.entry) return;
      const r = await callApi("get_word", w.word);
      if (r && (r.entry || r.item)) w.entry = r.entry || r.item;
    },

    async _prefetchNext(n = 3) {
      for (let j = 1; j <= n; j++) {
        const idx = this.activeIndex + j;
        if (idx < this.activeWords.length)
          await this._ensureCardData(this.activeWords[idx]);
      }
    },
  };

  // ---- persistence ----
  async function saveSession(clear = false) {
    const st = clear
      ? null
      : {
          mode: fc.mode,
          masterWords: fc.masterWords,
          groupSize: fc.groupSize,
          groupStart: fc.groupStart,
          stage: fc.stage,
          activeWords: fc.activeWords,
          activeIndex: fc.activeIndex,
          seenStates: fc.seenStates,
          weakSet: Array.from(fc.weakSet),
        };
    await callApi("save_session_state", st);
  }

  async function tryResume() {
    const r = await callApi("load_session_state");
    if (!r?.ok || !r.state) return;
    const st = r.state;
    fc.mode = st.mode || "idle";
    fc.masterWords = st.masterWords || [];
    fc.groupSize = st.groupSize || 10;
    fc.groupStart = st.groupStart || 0;
    fc.stage = st.stage || "firstpass";
    fc.activeWords = st.activeWords || [];
    fc.activeIndex = st.activeIndex || 0;
    fc.seenStates = st.seenStates || [];
    fc.weakSet = new Set(st.weakSet || []);
    if (fc.activeWords.length) {
      const banner = document.getElementById("session-banner");
      banner.classList.remove("hidden");
      banner.textContent = "Resumed previous plan.";
      fc._show(fc.activeIndex);
      const totalGroups = Math.max(
        1,
        Math.ceil(fc.masterWords.length / fc.groupSize)
      );
      ghMode.textContent = fc.mode;
      ghGroup.textContent = `Group ${
        Math.floor(fc.groupStart / fc.groupSize) + 1
      }/${totalGroups}`;
    }
  }

  // ---- donut chart ----
  function renderDonut(snapshot) {
    const total = snapshot.total || 0;
    const mastered = snapshot.mastered || 0;
    const learned = snapshot.learned || 0;
    const learnedOnly = Math.max(0, learned - mastered);
    const rest = Math.max(0, total - mastered - learnedOnly);

    const CIRC = 2 * Math.PI * 54; // r=54
    const segM = document.getElementById("donut-seg-mastered");
    const segL = document.getElementById("donut-seg-learned");
    const segR = document.getElementById("donut-seg-rest");
    const label = document.getElementById("donut-label");

    const pM = total ? mastered / total : 0;
    const pL = total ? learnedOnly / total : 0;
    const pR = Math.max(0, 1 - pM - pL);

    // dasharray = [visible, hidden]; 旋转 -90° 从正上开始
    segM.setAttribute(
      "stroke-dasharray",
      (CIRC * pM).toFixed(3) + " " + (CIRC * (1 - pM)).toFixed(3)
    );
    segL.style.strokeDashoffset = (-CIRC * pM).toFixed(3);
    segL.setAttribute(
      "stroke-dasharray",
      (CIRC * pL).toFixed(3) + " " + (CIRC * (1 - pL)).toFixed(3)
    );
    segR.style.strokeDashoffset = (-(CIRC * (pM + pL))).toFixed(3);
    segR.setAttribute(
      "stroke-dasharray",
      (CIRC * pR).toFixed(3) + " " + (CIRC * (1 - pR)).toFixed(3)
    );

    const pct = total
      ? Math.round(((mastered + learnedOnly) * 100) / total)
      : 0;
    label.textContent = pct + "%";
  }

  async function refreshStats() {
    const snap = await callApi("progress_snapshot");
    if (!snap) return;
    renderDonut(snap);
  }

  // ---- hooks ----
  document
    .getElementById("btn-start-daily")
    .addEventListener("click", () =>
      fc.startDailyPlan(dailySizeEl.value || 100)
    );

  document
    .getElementById("btn-review-today")
    .addEventListener("click", () => fc.startReviewToday());

  document.getElementById("btn-review-score").addEventListener("click", () => {
    const limit = reviewScoreLimitEl.value || 100;
    const includeUnseen = !!document.getElementById("include-unseen")?.checked;
    // learned_only = !includeUnseen
    fc.startReviewByScore(limit, !includeUnseen);
  });

  document
    .getElementById("btn-remember")
    .addEventListener("click", () => fc.markRemember(true));
  document
    .getElementById("btn-forget")
    .addEventListener("click", () => fc.markRemember(false));
  document
    .getElementById("btn-correct")
    .addEventListener("click", () => fc.confirmCorrect(true));
  document
    .getElementById("btn-wrong")
    .addEventListener("click", () => fc.confirmCorrect(false));
  document
    .getElementById("btn-next")
    .addEventListener("click", () => fc.nextAfterShow());

  // ---- keyboard (idempotent-aware) ----
  document.addEventListener("keydown", (e) => {
    if (e.target && e.target.tagName === "TEXTAREA") return;
    const k = e.key.toLowerCase();
    const back = !document
      .querySelector(".face-back")
      .classList.contains("hidden");
    if (fc.frontBusy || fc.backBusy) return;

    if (k === " ") {
      if (back && !fc.needVerify) return fc.nextAfterShow();
      fc._flip();
      e.preventDefault();
    }
    if (k === "1" || k === "y") {
      return back && fc.needVerify
        ? fc.confirmCorrect(true)
        : fc.markRemember(true);
    }
    if (k === "2" || k === "n") {
      return back && fc.needVerify
        ? fc.confirmCorrect(false)
        : fc.markRemember(false);
    }
    if (k === "arrowright") {
      return back && !fc.needVerify ? fc.nextAfterShow() : fc._next();
    }
  });

  // ---- init ----
  (async function init() {
    const cur = await callApi("get_current_store_path");
    if (cur?.ok && cur.path) storePathEl.textContent = cur.path;
    await refreshRecent();
    await refreshStats();
    await tryResume();
  })();

  // expose
  window.fc = fc;
})();
