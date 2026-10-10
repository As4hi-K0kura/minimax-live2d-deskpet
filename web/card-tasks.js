/* 多任务轮播区块：宠物窗卡片与独立卡片窗口共用的渲染模块。
 *
 * payload.tasks 长度 > 1 时接管 .card-body 里的轮播区（自动追加）；
 * 否则整块隐藏，单任务版式不受影响。暴露 window.CardTasks.render(rootEl, payload)。
 *
 * 设计要点（见 openspec: pet-parallel-task-card/design.md）：
 * - 轮换计时独立于内容刷新：payload 每秒都变（elapsed 在走），只有
 *   「用户点击圆点」和「自动轮换」会重置计时，普通内容刷新不动它。
 * - 任务集合变化时索引越界就回到 0，不出现空白卡片。
 */

(() => {
  const ROTATE_MS = 5000;

  const STATE_COLORS = {
    thinking: "#7aa2ff",
    working: "#39c07a",
    waiting: "#ffb340",
    review: "#b58cff",
    syncing: "#4fc3d9",
    blocked: "#ff6b6b",
    error: "#ff4d4d",
    done: "#ffd166",
    interrupted: "#9b9b9b",
    idle: "#7f8c9b",
  };

  let container = null;   // .card-tasks 根元素
  let els = null;         // 内部子元素引用
  let tasks = [];
  let index = 0;
  let timer = null;
  let lastKey = "";
  let lastCount = -1;

  function humanTime(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    if (sec < 60) return `${sec} 秒`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m} 分 ${sec % 60} 秒`;
    return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
  }

  function keyOf(list) {
    return JSON.stringify(
      list.map((t) => [t.threadId, t.state, t.label, t.title, t.activity, t.tools, t.tokens, t.elapsed])
    );
  }

  function ensureDom(root) {
    if (container && root.contains(container)) return;
    container = root.ownerDocument.createElement("div");
    container.className = "card-tasks";
    container.hidden = true;
    container.innerHTML = [
      '<div class="ct-head">',
      '  <span class="ct-dot"></span>',
      '  <span class="ct-state"></span>',
      '  <span class="ct-spacer"></span>',
      '  <span class="ct-count"></span>',
      "</div>",
      '<div class="ct-title"></div>',
      '<div class="ct-row"><span class="k">已用时</span><span class="v ct-elapsed"></span>',
      '  <span class="k">工具调用</span><span class="v ct-tools"></span></div>',
      '<div class="ct-row ct-minor"><span class="k">上下文</span><span class="v ct-tokens"></span></div>',
      '<div class="ct-activity ct-activity"></div>',
      '<div class="ct-dots"></div>',
    ].join("");
    root.appendChild(container);
    els = {
      dot: container.querySelector(".ct-dot"),
      state: container.querySelector(".ct-state"),
      count: container.querySelector(".ct-count"),
      title: container.querySelector(".ct-title"),
      elapsed: container.querySelector(".ct-elapsed"),
      tools: container.querySelector(".ct-tools"),
      tokens: container.querySelector(".ct-tokens"),
      activity: container.querySelector(".ct-activity"),
      dots: container.querySelector(".ct-dots"),
    };
  }

  function renderDetail() {
    const t = tasks[index];
    if (!t) return;
    const color = STATE_COLORS[t.state] || STATE_COLORS.idle;
    els.dot.style.background = color;
    els.state.textContent = t.label || "执行中";
    els.state.style.color = color;
    els.count.textContent = `${index + 1}/${tasks.length}`;
    els.title.textContent = t.title || "（无标题任务）";
    els.elapsed.textContent = humanTime(t.elapsed);
    els.tools.textContent = t.tools == null ? "—" : String(t.tools);
    els.tokens.textContent = t.tokens || "—";
    els.activity.textContent = t.activity || "";
  }

  function renderDots() {
    els.dots.innerHTML = "";
    tasks.forEach((t, i) => {
      const dot = container.ownerDocument.createElement("button");
      dot.className = "ct-dot-btn" + (i === index ? " active" : "");
      dot.title = t.title || `任务 ${i + 1}`;
      dot.setAttribute("aria-label", `切换到任务 ${i + 1}`);
      dot.addEventListener("click", () => show(i));
      els.dots.appendChild(dot);
    });
  }

  function resetTimer() {
    clearTimeout(timer);
    if (tasks.length > 1) {
      timer = setTimeout(() => show(index + 1), ROTATE_MS);
    }
  }

  function show(i) {
    if (!tasks.length) return;
    index = ((i % tasks.length) + tasks.length) % tasks.length;
    renderDetail();
    renderDots();
    resetTimer();   // 手动切换后计时重新开始
  }

  function render(root, payload) {
    if (!root) return;
    const list = Array.isArray(payload && payload.tasks) ? payload.tasks : [];

    if (list.length < 2) {
      // 单任务/空闲：退回单任务版式，清掉轮播状态
      tasks = [];
      lastKey = "";
      lastCount = -1;
      clearTimeout(timer);
      if (container) container.hidden = true;
      return;
    }

    ensureDom(root);
    const key = keyOf(list);
    const countChanged = list.length !== lastCount;
    tasks = list;
    if (index >= tasks.length) index = 0;
    if (key !== lastKey) {
      lastKey = key;
      renderDetail();
    }
    if (countChanged) {
      renderDots();
      lastCount = tasks.length;
    }
    container.hidden = false;
    if (!timer) resetTimer();
  }

  function dispose() {
    clearTimeout(timer);
    timer = null;
    if (container && container.parentNode) container.parentNode.removeChild(container);
    container = null;
    els = null;
    tasks = [];
    index = 0;
    lastKey = "";
    lastCount = -1;
  }

  window.CardTasks = { render, dispose, ROTATE_MS };
})();
