/* ============================================================
 * wasily 任务提示模块 task_hint.js
 *  - 独立 DOM 容器 / 独立生命周期，挂载即生效，destroy 彻底移除
 *  - 触发逻辑对齐 Trae 原生：≥1 字符 + 停顿 300ms 防抖；IME 组合期不触发
 *  - 匹配三维数据：当前输入 / 历史对话 / 项目状态（工具集 + 缺工具）
 *  - 本地目录瞬时匹配(<5ms) + 服务端异步富化(代际令牌丢弃过期结果)
 *  - 键盘 ↑↓ Enter Tab Esc / 鼠标悬停 / 点击(mousedown 保焦点)
 *  - 两种填充：complete=光标处替换片段词；task=整段替换
 * 纯函数挂在 TaskHint._fns 上，供单元测试直接调用
 * ============================================================ */
(function (global) {
  'use strict';

  /* ---------- 可调常量（与原生阈值对齐） ---------- */
  const CFG = {
    MIN_CHARS: 1,          // 输入字符触发阈值
    DEBOUNCE_MS: 300,      // 输入停顿阈值
    SERVER_MS: 250,        // 服务端富化防抖（本地先出，不阻塞）
    MAX_ITEMS: 6,
    SCORE_MIN: 18,
    PANEL_Z: 200,
    DONE_TTL_MS: 8000      // “任务已完成”停留时长
  };

  /* ---------- 本地任务目录（图标全部来自 icons.js 线性库） ---------- */
  const CATALOG = [
    { id: 'code-dev', icon: 'sparkles', type: 'complete',
      title: '帮你编写代码、调试 Bug、优化性能等开发工作，交付生产级代码产物。',
      desc: '开发任务 · 代码生成 / 调试 / 重构',
      kw: ['写代码', '编写', '代码', '开发', '调试', 'bug', '修复', '优化', '重构', '函数', '脚本', 'python', 'html', 'css', 'js', '报错'],
      fill: '帮你编写代码、调试 Bug、优化性能等开发工作，交付生产级代码产物。' },
    { id: 'plan-exec', icon: 'robot', type: 'task',
      title: '自主规划并执行多步任务',
      desc: '由 Agent 拆解步骤、调用工具并汇报结果',
      kw: ['帮我', '计划', '规划', '执行', '自动', '任务', 'agent', '安排', '处理'],
      fill: '请自主规划步骤并执行以下任务，完成后汇报每一步的结果：' },
    { id: 'open-url', icon: 'world', type: 'complete',
      title: '打开浏览器并访问指定网址',
      desc: '应用控制 · open_app + 浏览器',
      kw: ['打开', '浏览器', '网址', '访问', '网页', '网站', 'url', 'open'],
      fill: '打开浏览器并访问：' },
    { id: 'file-ops', icon: 'folder', type: 'task',
      title: '文件操作（列目录 / 读取 / 创建 / 修改）',
      desc: '文件系统工具：list_dir / read_file / write_file',
      kw: ['文件', '目录', '文件夹', '读取', '创建', '删除', '复制', '移动', 'd盘', '路径'],
      fill: '请操作文件：' },
    { id: 'screenshot', icon: 'screenshot', type: 'task',
      title: '截屏并保存到点餐项目目录',
      desc: '系统控制 · screenshot',
      kw: ['截屏', '截图', '屏幕', 'screenshot', '画面'],
      fill: '截屏并保存到 D:\\煮饭仔点餐\\项目文件 目录。' },
    { id: 'lamp', icon: 'lamp-2', type: 'task',
      title: '控制智能灯（开关 / 亮度 / 色温）',
      desc: '设备控制 · Yeelight 局域网',
      kw: ['灯', '开灯', '关灯', '亮度', '色温', 'yeelight', '设备', '台灯'],
      fill: '控制智能灯：' },
    { id: 'video-call', icon: 'video', type: 'task',
      title: '发起视频通话（吴晓欣数字人）',
      desc: '打开视频通话页面，支持 720P/1080P',
      kw: ['视频', '通话', '打电话', '见面', '数字人', '吴笑欣'],
      fill: '打开视频通话页面并发起呼叫。' },
    { id: 'tts-set', icon: 'volume', type: 'task',
      title: '语音朗读与音色设置',
      desc: 'edge-tts 晓晓音色 / 情绪 / 语速',
      kw: ['朗读', '语音', 'tts', '音色', '声音', '说话', '读出声'],
      fill: '打开语音朗读设置，调整音色与情绪。' },
    { id: 'train', icon: 'brain', type: 'task',
      title: '教 Agent 学习新技能（训练记忆）',
      desc: '进入训练模式，学会后自动记住',
      kw: ['学习', '训练', '教会', '记住', '记忆', '新技能', 'train'],
      fill: '进入训练模式：我要教你学会：' },
    { id: 'history', icon: 'history', type: 'task',
      title: '查看 / 导出历史对话',
      desc: '服务器历史记录或导出 JSON 备份',
      kw: ['历史', '记录', '之前', '上次', '导出', '备份', '聊天记录'],
      fill: '查看历史对话记录。' },
    { id: 'pos', icon: 'chef-hat', type: 'task',
      title: '点餐系统：菜单 / 订单 / 桌台管理',
      desc: '煮饭仔点餐业务相关操作',
      kw: ['点餐', '菜单', '订单', '桌台', '收银', '菜品', '餐厅', '煮饭仔'],
      fill: '处理点餐系统相关需求：' },
    { id: 'settings', icon: 'settings', type: 'task',
      title: '系统设置（资源策略 / 性能模式 / GPU）',
      desc: '资源分级、GPU 卸载、休眠策略',
      kw: ['设置', '性能', 'gpu', '显存', '休眠', '资源', '模式', '配置'],
      fill: '打开系统设置，调整：' },
    { id: 'install-tool', icon: 'plug', type: 'task', tag: 'install',
      title: '让 Agent 自动安装所需工具',
      desc: '对话即可安装新能力，无需重启',
      kw: ['安装', '工具', '插件', '扩展', '能力', '缺少', '没有工具', '装个'],
      fill: '请帮我安装一个可以完成「       」任务的工具，安装后立即试用。' }
  ];

  /* ================= 纯函数区（可单测） ================= */
  const fns = {
    norm(s) { return String(s == null ? '' : s).toLowerCase().replace(/\s+/g, ''); },

    /* 子序列匹配度 0~30 */
    fuzzy(q, t) {
      if (!q) return 0;
      let i = 0;
      for (let k = 0; k < t.length && i < q.length; k++) if (t[k] === q[i]) i++;
      return i === q.length ? Math.round(30 * q.length / Math.max(t.length, 1)) : 0;
    },

    /* 单条评分：输入(前缀/关键词/模糊) + 历史 + 工具集 */
    score(item, q, history, tools) {
      q = fns.norm(q);
      if (!q) return 0;
      const title = fns.norm(item.title);
      let s = 0;
      if (title.startsWith(q)) s += 100;
      else if (title.includes(q)) s += 55;
      else s += fns.fuzzy(q, title);
      (item.kw || []).forEach(k => { if (q.includes(fns.norm(k))) s += 22; });
      if (history) {
        const h = fns.norm(Array.isArray(history) ? history.join(' ') : history);
        if (h && (item.kw || []).some(k => h.includes(fns.norm(k)))) s += 6;
      }
      if (tools && tools.length) {
        const map = { lamp: ['device_control'], 'video-call': ['video'], screenshot: ['screenshot'],
          'file-ops': ['list_dir', 'read_file', 'write_file'], 'open-url': ['open_app'],
          tts: ['tts'], history: ['history'] };
        const need = map[item.id];
        if (need && need.some(n => tools.some(t => String(t).toLowerCase().includes(n)))) s += 4;
      }
      return s;
    },

    /* 本地匹配，返回排序后的数组（不产生副作用） */
    matchAll(q, history, tools, extra) {
      const arr = CATALOG.map(it => ({ it, w: fns.score(it, q, history, tools) }))
        .filter(x => x.w >= CFG.SCORE_MIN)
        .map(x => Object.assign({ weight: x.w }, x.it));
      (extra || []).forEach(sv => {   // 服务端动态项（缺工具引导等）
        if (!arr.some(a => a.id === sv.id)) arr.push(Object.assign({ weight: sv.weight || 30 }, sv));
        else {
          const hit = arr.find(a => a.id === sv.id);
          hit.weight += sv.weight || 10;
          if (sv.tag) hit.tag = sv.tag;
          if (sv.desc) hit.desc = sv.desc;
        }
      });
      return arr.sort((a, b) => b.weight - a.weight).slice(0, CFG.MAX_ITEMS);
    },

    /* 高亮命中片段：标题按查询字符子序列包 <b> */
    highlight(title, q) {
      q = String(q || '');
      if (!q) return esc(title);
      let i = 0; let out = '';
      for (const ch of title) {
        if (i < q.length && ch.toLowerCase() === q[i].toLowerCase()) { out += '<b>' + esc(ch) + '</b>'; i++; }
        else out += esc(ch);
      }
      return out;
    },

    /* 决定填充区间：
       complete → 替换光标前的连续片段词（追加到光标位置语义）
       task     → 替换全部已输入内容
       返回 {start,end,text} */
    decideFill(item, value, selStart, selEnd) {
      value = String(value || '');
      if (typeof selStart !== 'number') selStart = value.length;
      if (typeof selEnd !== 'number') selEnd = selStart;
      if (item.type === 'complete') {
        const m = /[^\s，。、；：,.!?！？]+$/.exec(value.slice(0, selStart));
        const start = m ? selStart - m[0].length : selStart;
        return { start, end: selEnd, text: item.fill };
      }
      return { start: 0, end: value.length, text: item.fill };
    }
  };

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function ico(name) {
    const svgs = global.ICON_SVGS || {};
    return '<i data-icon="' + esc(name) + '" class="ico">' + (svgs[name] || '') + '</i>';
  }

  /* ================= 面板控制器 ================= */
  function TaskHintPanel(opt) {
    this.input = opt.input;
    this.host = opt.host || this.input.parentElement;
    this.getHistory = opt.getHistory || function () { return []; };
    this.onFill = opt.onFill || function () {};
    this.fetchUrl = opt.fetchUrl || '/task_hints';
    this.items = [];
    this.active = -1;
    this.open_ = false;
    this.gen = 0;                 // 代际令牌，丢弃过期的异步结果
    this.debTimer = null;
    this.srvTimer = null;
    this.tools = [];
    this._build();
    this._bind();
    this._loadTools();
  }

  TaskHintPanel.prototype._build = function () {
    const cssPos = getComputedStyle(this.host).position;
    if (cssPos === 'static') this.host.style.position = 'relative';
    const el = document.createElement('div');
    el.id = 'thPanel';
    el.innerHTML =
      '<div class="th-head"><span class="th-head-ico">' + ico('sparkles') +
      '</span><span class="th-src">任务建议</span><span class="th-count"></span>' +
      '<span style="margin-left:auto"></span><kbd>↑↓</kbd><kbd>Enter</kbd><kbd>Esc</kbd></div>' +
      '<div class="th-list"></div>' +
      '<div class="th-loading"><span class="th-spinner"></span>正在结合上下文匹配…</div>';
    this.host.appendChild(el);
    this.el = el;
    this.list = el.querySelector('.th-list');
    this.countEl = el.querySelector('.th-count');
  };

  TaskHintPanel.prototype._bind = function () {
    const inp = this.input;
    this._onInput = () => {
      if (this._composing) return;
      clearTimeout(this.debTimer);
      this.debTimer = setTimeout(() => this.refresh(), CFG.DEBOUNCE_MS);
    };
    this._onFocus = () => { if (inp.value.length >= CFG.MIN_CHARS) this._onInput(); };
    this._onBlur = () => { setTimeout(() => this.hide(), 120); };   // 留时间给 mousedown
    this._onCompStart = () => { this._composing = true; };
    this._onCompEnd = () => { this._composing = false; this._onInput(); };
    this._onKey = (e) => {
      if (!this.open_) {
        // 关闭态：仅 ↓ 主动唤起面板；↑ 保留给宿主（历史翻阅/多行编辑）
        if (e.key === 'ArrowDown' && inp.value.length >= CFG.MIN_CHARS) {
          this.refresh(true);
          e.preventDefault(); e.stopImmediatePropagation();
        }
        return;
      }
      // 面板展开期间完全接管方向键/回车/Tab/Esc
      if (e.key === 'ArrowDown') { this.move(1); e.preventDefault(); e.stopImmediatePropagation(); }
      else if (e.key === 'ArrowUp') { this.move(-1); e.preventDefault(); e.stopImmediatePropagation(); }
      else if (e.key === 'Enter' || e.key === 'Tab') { this.accept(); e.preventDefault(); e.stopImmediatePropagation(); }
      else if (e.key === 'Escape') { this.hide(); e.preventDefault(); e.stopImmediatePropagation(); }
    };
    inp.addEventListener('input', this._onInput);
    inp.addEventListener('focus', this._onFocus);
    inp.addEventListener('blur', this._onBlur);
    inp.addEventListener('keydown', this._onKey);
    inp.addEventListener('compositionstart', this._onCompStart);
    inp.addEventListener('compositionend', this._onCompEnd);
    // 点击宿主其他区域时关闭
    this._onDocDown = (e) => { if (this.open_ && !this.el.contains(e.target) && e.target !== inp) this.hide(); };
    document.addEventListener('mousedown', this._onDocDown, true);
  };

  TaskHintPanel.prototype._loadTools = function () {
    fetch('/tools').then(r => r.json()).then(d => {
      // /tools 返回 {工具名: {description, parameters}}；兼容数组形式
      this.tools = Array.isArray(d) ? d.map(t => t.name || t)
        : (d && d.tools) ? d.tools.map(t => t.name || t) : Object.keys(d || {});
    }).catch(() => {});
  };

  /* 刷新匹配：本地同步出结果 → 异步服务端富化 */
  TaskHintPanel.prototype.refresh = function (immediate) {
    const q = this.input.value;
    if (q.length < CFG.MIN_CHARS) { this.hide(); return; }
    const gen = ++this.gen;
    const local = fns.matchAll(q, this.getHistory(), this.tools, null);
    this._render(local, q);
    if (!immediate) {
      clearTimeout(this.srvTimer);
      this.srvTimer = setTimeout(() => this._enrich(q, gen), CFG.SERVER_MS);
    }
  };

  TaskHintPanel.prototype._enrich = function (q, gen) {
    const hist = this.getHistory().slice(-2);
    this.el.classList.add('th-loading');
    fetch(this.fetchUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q: q, history: hist })
    }).then(r => r.json()).then(d => {
      if (gen !== this.gen) return;                  // 过期结果丢弃
      this.el.classList.remove('th-loading');
      const merged = fns.matchAll(q, this.getHistory(), this.tools, d.items || []);
      this._render(merged, q);
    }).catch(() => {
      if (gen === this.gen) this.el.classList.remove('th-loading');
    });
  };

  TaskHintPanel.prototype._render = function (items, q) {
    this.items = items;
    if (!items.length) { this.hide(); return; }
    this.list.innerHTML = items.map((it, i) =>
      '<div class="th-item' + (i === 0 ? ' th-active' : '') + '" data-i="' + i + '" data-id="' + esc(it.id) + '">' +
      '<span class="th-ico">' + ico(it.icon || 'sparkles') + '</span>' +
      '<span class="th-txt"><span class="th-title">' + fns.highlight(it.title, q) + '</span>' +
      (it.desc ? '<span class="th-desc">' + esc(it.desc) + '</span>' : '') + '</span>' +
      (it.tag === 'install' ? '<span class="th-tag install">装工具</span>'
        : it.tag === 'tool' ? '<span class="th-tag tool">工具</span>'
        : it.tag ? '<span class="th-tag">' + esc(it.tag) + '</span>' : '') +
      '</div>').join('');
    this.active = 0;
    this.countEl.textContent = items.length + ' 项';
    this._bindItems();
    this.show();
    if (global.renderIcons) global.renderIcons(this.list);
  };

  TaskHintPanel.prototype._bindItems = function () {
    const nodes = this.list.querySelectorAll('.th-item');
    Array.prototype.forEach.call(nodes, nd => {
      // mousedown + preventDefault：填充时输入框不丢焦点
      nd.addEventListener('mousedown', (e) => { e.preventDefault(); this.active = +nd.dataset.i; this.accept(); });
      nd.addEventListener('mouseenter', () => this.setActive(+nd.dataset.i));
    });
  };

  TaskHintPanel.prototype.setActive = function (i) {
    this.active = (i + this.items.length) % this.items.length;
    const nodes = this.list.querySelectorAll('.th-item');
    Array.prototype.forEach.call(nodes, (nd, k) => nd.classList.toggle('th-active', k === this.active));
  };
  TaskHintPanel.prototype.move = function (dir) {
    if (!this.items.length) return;
    this.setActive(this.active + dir);
    const node = this.list.children[this.active];
    if (node) node.scrollIntoView({ block: 'nearest' });
  };

  TaskHintPanel.prototype.accept = function () {
    const it = this.items[this.active];
    if (!it) return;
    const inp = this.input;
    const pos = fns.decideFill(it, inp.value, inp.selectionStart, inp.selectionEnd);
    const before = inp.value.slice(0, pos.start);
    const after = inp.value.slice(pos.end);
    inp.value = before + pos.text + after;
    const caret = before.length + pos.text.length;
    inp.focus();
    inp.setSelectionRange(caret, caret);
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    this.hide();
    this.onFill(pos.text, it);
  };

  TaskHintPanel.prototype.show = function () { this.open_ = true; this.el.classList.add('th-show'); };
  TaskHintPanel.prototype.hide = function () {
    this.open_ = false;
    this.el.classList.remove('th-show', 'th-loading');
    this.active = -1;
  };
  TaskHintPanel.prototype.isOpen = function () { return this.open_; };

  TaskHintPanel.prototype.destroy = function () {
    clearTimeout(this.debTimer);
    clearTimeout(this.srvTimer);
    ['input', 'focus', 'blur', 'keydown', 'compositionstart', 'compositionend'].forEach(ev =>
      this.input.removeEventListener(ev, this['_on' + ({ input: 'Input', focus: 'Focus', blur: 'Blur', keydown: 'Key', compositionstart: 'CompStart', compositionend: 'CompEnd' }[ev])]));
    document.removeEventListener('mousedown', this._onDocDown, true);
    if (this.el && this.el.parentNode) this.el.parentNode.removeChild(this.el);
  };

  /* ================= 任务状态条（思考中/任务已完成，可折叠） ================= */
  function TaskStrip(el) {
    this.el = el;
    this.wasBusy = false;
    this.steps = [];
    this.timer = null;
    el.addEventListener('click', () => el.classList.toggle('expanded'));
  }
  TaskStrip.prototype.set = function (st) {
    const busy = st && st.state === 'thinking';
    if (busy) {
      this.wasBusy = true;
      clearTimeout(this.timer);
      const phase = st.phase || 'decode';
      const tool = st.tool ? ' · ' + st.tool : '';
      const line = { t: Date.now(), text: phase + tool };
      if (!this.steps.some(s => s.text === line.text)) this.steps.push(line);
      if (this.steps.length > 6) this.steps.shift();
      this._render('busy', '思考中 · ' + phase + tool);
    } else if (this.wasBusy) {
      this.wasBusy = false;
      this.steps.push({ t: Date.now(), text: '完成' });
      this._render('done', '任务已完成！');
      clearTimeout(this.timer);
      this.timer = setTimeout(() => { this.el.classList.remove('show', 'expanded'); }, CFG.DONE_TTL_MS);
    }
  };
  TaskStrip.prototype._render = function (mode, title) {
    const icon = mode === 'busy'
      ? '<span class="ts-spinner"></span>'
      : '<span class="ts-ico">' + (global.ICON_SVGS ? '<i data-icon="circle-check" class="ico"></i>' : '✓') + '</span>';
    const detail = this.steps.map(s =>
      '<div class="ts-step"><span class="ts-dot">›</span>' + esc(s.text) + '</div>').join('');
    this.el.className = 'show' + (mode === 'busy' ? ' busy' : '');
    this.el.innerHTML = icon + '<span>' + esc(title) + '</span>' +
      '<span class="ts-chevron"><i data-icon="arrows-minimize" class="ico" style="transform:rotate(90deg)"></i></span>' +
      '<div class="ts-detail">' + detail + '</div>';
  };

  global.TaskHint = {
    Panel: TaskHintPanel,
    Strip: TaskStrip,
    config: CFG,
    _fns: fns,
    _catalog: CATALOG,
    version: '1.0'
  };
})(window);
