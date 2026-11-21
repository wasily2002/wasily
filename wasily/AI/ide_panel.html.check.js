
(function () {
  // 无 IDE 扩展时（浏览器直接打开）acquireVsCodeApi 不存在，自动降级为「直连后端」模式
  var HAS_API = (typeof acquireVsCodeApi === 'function');
  var vscode = HAS_API ? acquireVsCodeApi() : null;
  // 扩展消息通道（绑定 this，供 post() 统一调用；无 API 时为 null）
  var vsPost = null;
  if (vscode) { try { vsPost = vscode.postMessage.bind(vscode); } catch (e) { vsPost = null; } }
  var SERVER = '__SERVER__';
  // 直连模式下占位符未被扩展替换，回落为当前页面同源地址（页面本身由 /panel_ui 下发）
  if (!HAS_API && SERVER === '__SERVER__') SERVER = location.origin;

  var chatEl = document.getElementById('chat');
  var inputEl = document.getElementById('input');
  // 空状态快捷入口：点一下把开头写进输入框
  chatEl.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('#empty .wChips button') : null;
    if (!b) return;
    inputEl.value = b.getAttribute('data-p') || '';
    inputEl.focus();
    inputEl.dispatchEvent(new Event('input'));
  });
  var sendBtn = document.getElementById('sendBtn');
  var btnEnhance = document.getElementById('btnEnhance');
  var stopBtn = document.getElementById('btnStop');
  var refChip = document.getElementById('refChip');
  var modeSel = document.getElementById('modeSel');
  var permSel = document.getElementById('permSel');
  var sessPanel = document.getElementById('sessPanel');
  var sessList = document.getElementById('sessList');
  var toastEl = document.getElementById('toast');
  var portView = document.getElementById('portView');
  var portTb = document.getElementById('portTb');
  var currentSid = null;
  var generating = false;
  var toolSteps = [];
  var pendingChangesEl = null;
  var turnStart = 0;

  /* ---------- 多窗口模型：标签 = 窗口，每窗口独立 windowId + 独立会话 sid ---------- */
  var windows = [];               // [{ windowId, sid, title }]
  var activeWindowId = null;
  var winTabList = document.getElementById('winTabList');
  function winBySid(sid) {
    if (!sid) return null;
    for (var i = 0; i < windows.length; i++) if (windows[i].sid === sid) return windows[i];
    return null;
  }
  function winById(wid) {
    for (var i = 0; i < windows.length; i++) if (windows[i].windowId === wid) return windows[i];
    return null;
  }
  function activeWin() { return winById(activeWindowId); }
  function winTitle(w) {
    var t = (w && w.title) || '新窗口';
    return t.length > 14 ? t.slice(0, 14) + '…' : t;
  }
  function renderTabs() {
    if (!winTabList) return;
    winTabList.innerHTML = windows.map(function (w) {
      return '<div class="winTab' + (w.windowId === activeWindowId ? ' on' : '') + '" data-wid="' + esc(w.windowId) + '">' +
        '<span class="wt">' + esc(winTitle(w)) + '</span>' +
        '<button class="wx" data-close="' + esc(w.windowId) + '" title="关闭窗口">✕</button></div>';
    }).join('');
    // 只有一个窗口时不显示标签栏，和 TRAE 一样保持单行头部
    var wt = document.getElementById('winTabs');
    if (wt) wt.style.display = windows.length > 1 ? '' : 'none';
  }
  // 新建窗口：面板侧先生成 sid，扩展按该 id 建会话，避免「先建窗口再等扩展回 id」的空窗期
  function newWindow() {
    var ts = Date.now();
    var wid = 'w' + ts;
    var sid = 's' + ts + '-' + Math.random().toString(36).slice(2, 6);
    windows.push({ windowId: wid, sid: sid, title: '新窗口' });
    activeWindowId = wid;
    currentSid = sid;
    renderTabs();
    post({ type: 'newSession', windowId: wid, sessionId: sid });
    post({ type: 'registerWindow', windowId: wid, sessionId: sid });
    post({ type: 'setActiveWindow', windowId: wid });
    return wid;
  }
  // 切换窗口：先通知扩展当前窗口（上下文按窗口存），再切到该窗口绑定的会话
  function activateWindow(wid, silent) {
    var w = winById(wid);
    if (!w) return;
    if (activeWindowId === wid && !silent) return;
    activeWindowId = wid;
    renderTabs();
    if (!silent) {
      post({ type: 'setActiveWindow', windowId: wid });
      if (w.sid) post({ type: 'switchSession', id: w.sid, windowId: wid });
    }
  }
  // 关闭窗口：有进行中任务时先提示，确认后停止并关闭；其余窗口不受影响
  function closeWindow(wid) {
    var w = winById(wid);
    if (!w) return;
    var busy = generating && currentSid && w.sid === currentSid;
    if (busy && !confirm('该窗口还有进行中的任务，关闭后该窗口的对话将中断。确定关闭？')) return;
    if (busy) post({ type: 'stop' });
    var idx = -1;
    for (var i = 0; i < windows.length; i++) if (windows[i].windowId === wid) { idx = i; break; }
    if (idx < 0) return;
    windows.splice(idx, 1);
    if (windows.length === 0) { newWindow(); }
    else if (activeWindowId === wid) {
      var next = windows[Math.max(0, idx - 1)];
      activeWindowId = next.windowId;
      currentSid = next.sid;
      renderTabs();
      post({ type: 'setActiveWindow', windowId: next.windowId });
      post({ type: 'switchSession', id: next.sid, windowId: next.windowId });
    } else {
      renderTabs();
    }
    if (w.sid) post({ type: 'deleteSession', id: w.sid, windowId: wid });
  }

  // 新增功能的 DOM 引用与状态
  var brainOn = document.getElementById('brainOn');
  var brainView = document.getElementById('brainView');
  var brainFrame = document.getElementById('brainFrame');
  var brainResize = document.getElementById('brainResize');
  var brainPop = document.getElementById('brainPop');
  var btnBrainEl = document.getElementById('btnBrain');
  var tokenBar = document.getElementById('tokenBar');
  var tokenDetail = document.getElementById('tokenDetail');
  var tokCur = document.getElementById('tokCur');
  var tokTotal = document.getElementById('tokTotal');
  var lightboxEl = document.getElementById('lightbox');
  var lbImg = document.getElementById('lbImg');
  var ttsPanel = document.getElementById('ttsPanel');
  var ttsOn = document.getElementById('ttsOn');
  var voiceSel = document.getElementById('voiceSel');
  var emotionSel = document.getElementById('emotionSel');
  var attachBar = document.getElementById('attachBar');
  var fileInput = document.getElementById('fileInput');
  var dragHandle = document.getElementById('dragHandle');
  var pendingImgs = [];   // 待发送图片 {id,name,size,kind,status,progress,path,thumb,error}
  var pendingDocs = [];   // 待发送文档 {id,name,size,kind,status,progress,path,error}
  var pendingContextFiles = [];   // IDE 拖入的已落盘文件，沿用上下文发送链路，仅移到输入框内展示
  var ttsEnabled = false;
  var ttsVoice = '';
  var ttsEmotion = 'normal';
  var curSpeakAudio = null;
  var tokenTimer = 0;
  var lastAiText = '';   // 上一条 AI 回复文本（用于朗读/自检）

  function toast(text) {
    toastEl.textContent = text;
    toastEl.style.display = 'block';
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { toastEl.style.display = 'none'; }, 2400);
  }

  /* ---------- 任务结束提示（窗口内 toast；点击回到对应会话并高亮结果） ---------- */
  var taskToastEl = document.getElementById('taskToast');
  var taskToastState = { sid: '', at: 0 };
  var lastAiWrap = null;

  function showTaskToast(msg) {
    if (!taskToastEl) return;
    var kind = msg.state === 'error' ? 'err' : (msg.state === 'confirm' ? 'warn' : 'ok');
    var stateText = kind === 'err' ? '任务失败' : (kind === 'warn' ? '需要你确认' : '任务完成');
    taskToastEl.className = 'show' + (kind === 'err' ? ' err' : (kind === 'warn' ? ' warn' : ''));
    taskToastEl.querySelector('.ttState').textContent =
      stateText + (msg.title ? ' · ' + msg.title : '');
    taskToastEl.querySelector('.ttMs').textContent =
      (typeof msg.ms === 'number' && msg.ms > 0) ? fmtMs(msg.ms) : '';
    taskToastEl.querySelector('.ttBody').textContent = msg.summary || '';
    taskToastState = { sid: msg.sid || '', at: Date.now() };
    clearTimeout(taskToastEl._t);
    taskToastEl._t = setTimeout(function () { taskToastEl.className = ''; }, 6000);
  }

  function highlightLastAi() {
    var el = (lastAiWrap && lastAiWrap.parentNode) ? lastAiWrap : null;
    if (!el) {
      var all = chatEl.querySelectorAll('.msg.ai');
      el = all.length ? all[all.length - 1] : null;
    }
    if (!el) return;
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
    if (el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    setTimeout(function () { el.classList.remove('flash'); }, 2200);
  }

  if (taskToastEl) taskToastEl.addEventListener('click', function () {
    taskToastEl.className = '';
    if (taskToastState.sid && currentSid && taskToastState.sid !== currentSid) {
      post({ type: 'switchSession', id: taskToastState.sid });
    }
    highlightLastAi();
  });

  /* ---------- 空返回 / 失败卡片：明确原因 + 可执行重试 ---------- */
  function addRetryCard(msg) {
    clearEmpty();
    streamEnd();
    // 失败收尾与完成态对齐：思考块表头并入耗时行，避免「已思考 X」和「任务耗时 Y」两行并存
    var live = document.getElementById('procWrap') || (chatEl.querySelector('.procWrap.live'));
    if (live) {
      live.classList.remove('live');
      live.removeAttribute('id');
      var th = live.querySelector('.tbHead');
      if (th && th.parentNode) th.parentNode.removeChild(th);
      live.classList.add('fold');
      ensureProcTally(live, (msg && msg.ms) || (genStartedAt ? Date.now() - genStartedAt : 0), []);
    }
    var card = document.createElement('div');
    card.className = 'retryCard';
    var detail = '';
    var d = msg && msg.detail;
    if (d && typeof d === 'object') {
      detail = '请求 ' + (d.request_id || '-') +
        ' · 模式 ' + (d.mode || '-') +
        ' · 工具轮次 ' + (d.tool_rounds == null ? '-' : d.tool_rounds) +
        ' · finish_reason ' + (d.finish_reason || '-') +
        ' · tokens ' + (d.tokens == null ? '-' : d.tokens);
    }
    card.innerHTML = '<div class="rcReason">' + esc(msg && msg.reason ? msg.reason : '模型未返回内容') + '</div>' +
      (detail ? '<div class="rcDetail">' + esc(detail) + '</div>' : '') +
      '<button class="rcBtn">重试</button>';
    card.querySelector('.rcBtn').addEventListener('click', function () {
      card.remove();
      post({ type: 'retryLast' });
    });
    chatEl.appendChild(card);
    scrollBottom();
  }

  // 统一的消息发送出口：无扩展（直连模式）时静默丢弃，保证任何调用点都不会抛错
  function post(m) {
    if (!vsPost) return;
    try { vsPost(m); } catch (e) { /* 扩展通道异常时忽略 */ }
  }
  // 依赖扩展的功能（文件改动确认 / 打开 diff / 导出等）：无 API 时统一提示，避免静默失败
  function needIde() {
    if (HAS_API) return false;
    toast('该功能需在 Wasily IDE 面板中使用');
    return true;
  }
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  // 代码块在 esc(text) 之后才提取，缓存里存的是转义文本；这里还原成原始代码，
  // 保证展示不再二次转义、复制出去也不带 &lt; / &amp;
  function unesc(s) {
    return String(s).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  }
  // ===== 轻量语法着色（本地实现，不依赖外部库）=====
  var HL_KW = {
    js: 'break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new return super switch this throw try typeof var void while with yield async await of static get set true false null undefined interface type enum implements public private protected readonly namespace declare abstract as from'.split(' '),
    py: 'False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case'.split(' '),
    json: ['true', 'false', 'null']
  };
  function hlFamily(lang) {
    var l = String(lang || '').toLowerCase();
    if (/^(js|javascript|jsx|ts|typescript|tsx|mjs|cjs)$/.test(l)) return 'js';
    if (/^(py|python)$/.test(l)) return 'py';
    if (l === 'json') return 'json';
    if (/^(html|htm|xml|svg)$/.test(l)) return 'html';
    if (/^(css|scss|less)$/.test(l)) return 'css';
    return '';
  }
  function hlTag(tag) {
    if (tag.indexOf('<!--') === 0) return '<span class="tk-c">' + esc(tag) + '</span>';
    var out = '', i = 0, re = /<\/?[A-Za-z][\w:-]*|[A-Za-z_:][\w:.-]*(?=\s*=)|"[^"]*"|'[^']*'/g, m;
    while ((m = re.exec(tag))) {
      out += esc(tag.slice(i, m.index));
      var tk = m[0];
      var cls = /<\/?/.test(tk) ? 'tk-t' : (/^["']/.test(tk) ? 'tk-s' : 'tk-f');
      out += '<span class="' + cls + '">' + esc(tk) + '</span>';
      i = re.lastIndex;
    }
    return out + esc(tag.slice(i));
  }
  function hlHtmlLine(line) {
    var out = '', i = 0, re = /<!--[\s\S]*?-->|<\/?[A-Za-z][^>]*>/g, m;
    while ((m = re.exec(line))) {
      out += esc(line.slice(i, m.index)) + hlTag(m[0]);
      i = re.lastIndex;
    }
    return out + esc(line.slice(i));
  }
  // 返回单行高亮后的 HTML；未知语言退化为纯转义文本
  function hlLine(line, lang) {
    var fam = hlFamily(lang);
    if (!fam) return esc(line);
    if (fam === 'html') return hlHtmlLine(line);
    var commentSrc = fam === 'py' ? '#[^\\n]*'
      : (fam === 'json' ? '(?!)' : '\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/');
    var stringSrc = fam === 'py'
      ? '"(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\''
      : (fam === 'json' ? '"(?:\\\\.|[^"\\\\])*"'
        : '"(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\'|`(?:\\\\.|[^`\\\\])*`');
    var idSrc = fam === 'css' ? '@[A-Za-z-]+|[A-Za-z_-][\\w-]*' : '[A-Za-z_$][\\w$]*';
    var re = new RegExp('(' + commentSrc + ')|(' + stringSrc + ')|(\\b\\d+(?:\\.\\d+)?\\b)|(' + idSrc + ')|([\\s\\S])', 'g');
    var out = '', m;
    while ((m = re.exec(line))) {
      if (m[1]) out += '<span class="tk-c">' + esc(m[1]) + '</span>';
      else if (m[2]) out += '<span class="tk-s">' + esc(m[2]) + '</span>';
      else if (m[3]) out += '<span class="tk-n">' + esc(m[3]) + '</span>';
      else if (m[4]) {
        var w = m[4], after = line.slice(re.lastIndex);
        var kw = fam === 'json' ? /^(true|false|null)$/.test(w) : (HL_KW[fam] || []).indexOf(w) >= 0;
        if (kw) out += '<span class="tk-k">' + w + '</span>';
        else if (fam === 'css' && /^\s*:/.test(after)) out += '<span class="tk-f">' + w + '</span>';
        else if (fam !== 'css' && /^\s*\(/.test(after)) out += '<span class="tk-f">' + w + '</span>';
        else out += esc(w);
      } else out += esc(m[5]);
    }
    return out;
  }
  /* ===== 滚动跟随：用户上翻则停止自动滚动，绝不把页面拽回底部 ===== */
  var userAtBottom = true;
  var quietRender = false;   // 历史分段补渲染期间禁止自动滚动/朗读
  var backBottomBtn = document.getElementById('backBottom');
  function scrollBottom(force) {
    if (quietRender) return;
    if (force === true) {
      userAtBottom = true;
      chatEl.scrollTop = chatEl.scrollHeight;
      updateBackBottom();
      return;
    }
    if (userAtBottom) chatEl.scrollTop = chatEl.scrollHeight;
  }
  function updateBackBottom() {
    if (!backBottomBtn) return;
    var show = !userAtBottom && chatEl.scrollHeight > chatEl.clientHeight + 80;
    backBottomBtn.style.display = show ? 'inline-flex' : 'none';
    if (show) {
      var comp = document.getElementById('composer');
      backBottomBtn.style.bottom = ((comp ? comp.offsetHeight : 120) + 12) + 'px';
    }
  }
  chatEl.addEventListener('scroll', function () {
    var gap = chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight;
    userAtBottom = gap < 40;
    updateBackBottom();
    // 接近顶部时增量补渲染更早的历史消息（分段窗口）
    if (chatEl.scrollTop < 60) loadOlderHistory();
  });
  if (backBottomBtn) {
    backBottomBtn.addEventListener('click', function () { scrollBottom(true); });
  }
  /* ===== 思考时间线事件立即渲染；耗时行只保留折叠入口 ===== */
  function queueOrRun(fn) { fn(); }
  /* ===== IDE 错误定位列表 ===== */
  var diagItems = [];
  function renderDiagnostics(items) {
    diagItems = items || [];
    var bar = document.getElementById('ideSyncBar');
    var chip = document.getElementById('diagChip');
    var list = document.getElementById('diagList');
    if (!bar || !chip || !list) return;
    if (!diagItems.length) {
      bar.style.display = 'none';
      chip.style.display = 'none';
      list.style.display = 'none';
      list.innerHTML = '';
      return;
    }
    bar.style.display = 'flex';
    chip.style.display = 'inline-block';
    chip.textContent = '⛔ ' + diagItems.length + ' 个错误';
    list.innerHTML = diagItems.map(function (d, i) {
      return '<div class="diagRow" data-i="' + i + '">' +
        '<span class="dl">' + esc(baseName(d.file)) + ':' + d.line + '</span>' +
        '<span class="dm">' + esc(d.message) + '</span></div>';
    }).join('');
  }
  (function () {
    var chip = document.getElementById('diagChip');
    var list = document.getElementById('diagList');
    if (chip) chip.addEventListener('click', function () {
      list.style.display = list.style.display === 'none' ? 'block' : 'none';
    });
    if (list) list.addEventListener('click', function (e) {
      var row = e.target.closest ? e.target.closest('.diagRow') : null;
      if (!row) return;
      var d = diagItems[Number(row.getAttribute('data-i'))];
      if (d) post({ type: 'gotoError', file: d.file, line: d.line, col: d.col });
    });
  })();
  function clearEmpty() {
    var e = document.getElementById('empty');
    if (e && e.parentNode) e.parentNode.removeChild(e);
  }

  /* ---------- markdown（轻量，支持 GFM 表格/有序列表/分割线/引用块） ---------- */
  /* 提醒块图标（三种语气） */
  var NT_ICONS = {
    info: '<span class="ntIco"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 8.1h.01"/><path d="M12 11.4v4.5"/></svg></span>',
    warning: '<span class="ntIco"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.6 3.2 19.2h17.6z"/><path d="M12 9.2v4.1"/><path d="M12 16.4h.01"/></svg></span>',
    error: '<span class="ntIco"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M9.3 9.3l5.4 5.4"/><path d="M14.7 9.3l-5.4 5.4"/></svg></span>'
  };
  var NT_LABELS = { info: '提示', warning: '警告', error: '错误' };
  // 进度块：多步骤清单（进行中 / 完成 / 失败 三态 + 步骤计数）
  function renderProgressBlock(raw) {
    var lines = String(raw || '').split('\n').filter(function (l) { return l.trim(); });
    var steps = [], done = 0, fail = 0;
    lines.forEach(function (line) {
      var m = line.match(/^\s*(?:[-*]\s*)?(?:\[([^\]]*)\]\s*|(\d+)[.、]\s*)?(.*)$/);
      var mark = m && m[1] ? m[1].trim().toLowerCase() : '';
      var text = String((m ? m[3] : line) || '').trim();
      if (!text) return;
      var st = 'idle';
      if (/^(x|✓|√|done|完成|已完成)$/.test(mark)) st = 'done';
      else if (/^(!|×|fail|失败|error)$/.test(mark)) st = 'fail';
      else if (/^(>|•|run|进行中|doing)$/.test(mark)) st = 'run';
      if (!mark) {
        // 无标记时从行首状态词推断，如「已完成：读取目录」
        if (/^(已完成|完成)[：:]\s*/.test(text)) { st = 'done'; text = text.replace(/^(已完成|完成)[：:]\s*/, ''); }
        else if (/^(失败|出错)[：:]\s*/.test(text)) { st = 'fail'; text = text.replace(/^(失败|出错)[：:]\s*/, ''); }
        else if (/^(进行中|执行中|正在)[：:]\s*/.test(text)) { st = 'run'; text = text.replace(/^(进行中|执行中|正在)[：:]\s*/, ''); }
      }
      if (st === 'done') done++;
      if (st === 'fail') fail++;
      steps.push({ st: st, text: text });
    });
    if (!steps.length) return '';
    var total = steps.length;
    var pct = total ? Math.round(done * 100 / total) : 0;
    var body = steps.map(function (s) {
      var ico = s.st === 'done' ? '✓' : (s.st === 'fail' ? '✕' : (s.st === 'run' ? '<i></i>' : '·'));
      return '<div class="pgStep ' + s.st + '"><span class="pgSt">' + ico + '</span>' +
        '<span class="pgTx">' + esc(s.text) + '</span></div>';
    }).join('');
    return '<div class="progressBlock"><div class="pgHead"><span class="pgTag">进度</span>' +
      '<span class="pgCount">完成 ' + done + '/' + total +
      (fail ? '<span class="bad">' + fail + ' 失败</span>' : '') + '</span></div>' +
      '<div class="pgBar"><i style="width:' + pct + '%"></i></div>' +
      '<div class="pgBody">' + body + '</div></div>';
  }
  function renderMarkdown(text) {
    var s = esc(text || '');
    var blocks = [];
    s = s.replace(/```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```/g, function (_, lang, code) {
      // 全局唯一 id：同一条聊天里多次渲染时，旧消息的复制按钮不能被新消息覆盖
      var id = 'cb' + (window.__cbSeq = (window.__cbSeq || 0) + 1);
      blocks.push({ id: id, lang: lang || 'code', code: unesc(code.replace(/\n$/, '')) });
      return '\u0000' + id + '\u0000';
    });
    // GFM 表格：连续以 | 开头的行，第二行必须是分隔行（|---|---|）
    var notices = [];
    // 提醒块：> [!info] / [!warning] / [!error]（可带标题）+ 后续 > 行
    s = s.replace(/(?:^|\n)&gt;[ \t]?\[!(info|warning|error|success)\][ \t]*(.*?)\n((?:&gt;[ \t]?[^\n]*\n?)*)/gi, function (_, tone, title, body) {
      var t = String(tone).toLowerCase();
      if (t === 'success') t = 'info';
      if (!NT_ICONS[t]) t = 'info';
      var bodyTxt = String(body || '').replace(/^&gt;[ \t]?/gm, '').replace(/\n$/, '');
      bodyTxt = bodyTxt.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/`([^`]+)`/g, '<code class="inl">$1</code>').replace(/\n/g, '<br>');
      var head = (title || '').trim() || NT_LABELS[t];
      var idx = notices.length;
      notices.push('<div class="notice ' + t + '"><div class="ntHead">' + NT_ICONS[t] +
        '<span>' + esc(head) + '</span></div><div class="ntBody">' + bodyTxt + '</div></div>');
      return '\u0002' + idx + '\u0002';
    });
    s = s.replace(/(^|\n)((?:\|[^\n]*\|[ \t]*(?:\n|$))+)/g, function (_, pre, block) {
      var rows = block.replace(/\n$/, '').split('\n').map(function (r) { return r.trim(); });
      if (rows.length < 2 || !/^\|[\s:|-]+\|$/.test(rows[1] || '')) return _;
      var cells = function (r) {
        var c = r.replace(/^\|/, '').replace(/\|$/, '').split('|');
        return c.map(function (x) { return x.trim(); });
      };
      var html = '<table class="mdt"><thead><tr>';
      cells(rows[0]).forEach(function (c) { html += '<th>' + c + '</th>'; });
      html += '</tr></thead><tbody>';
      for (var i = 2; i < rows.length; i++) {
        html += '<tr>';
        cells(rows[i]).forEach(function (c) { html += '<td>' + c + '</td>'; });
        html += '</tr>';
      }
      html += '</tbody></table>';
      return pre + '\u0001' + html + '\u0001';
    });
    s = s.replace(/^### (.*)$/gm, '<h3>$1</h3>')
         .replace(/^## (.*)$/gm, '<h2>$1</h2>')
         .replace(/^# (.*)$/gm, '<h3>$1</h3>');
    s = s.replace(/^[ \t]*(-{3,}|\*{3,})[ \t]*$/gm, '<hr>');
    s = s.replace(/^&gt;[ \t]?(.*)$/gm, '<blockquote>$1</blockquote>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
    s = s.replace(/\`([^\`]+)\`/g, '<code class="inl">$1</code>');
    // 图片：![alt](url) → <img class="md-img">（点击可放大，见灯箱）
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img class="md-img" src="$2" alt="$1">');
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // 无序列表（先处理，li 行不会再被有序列表规则命中）
    s = s.replace(/^\s*[-*·]\s+(.*)$/gm, '<li>$1</li>');
    s = s.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>');
    // 有序列表（oli 占位，包装成 <ol> 后再转回 <li>，避免被 <ul> 规则二次包裹）
    s = s.replace(/^\s*\d+\.\s+(.*)$/gm, '<oli>$1</oli>');
    s = s.replace(/(<oli>[\s\S]*?<\/oli>)(?!\s*<oli>)/g, '<ol>$1</ol>');
    s = s.replace(/<oli>/g, '<li>').replace(/<\/oli>/g, '</li>');
    s = s.replace(/\n{2,}/g, '</p><p>');
    s = '<p>' + s + '</p>';
    // 表格占位还原（避免被 <p> 包裹）
    s = s.replace(/\u0001([\s\S]*?)\u0001/g, function (_, h) { return '</p>' + h + '<p>'; });
    // 提醒块占位还原（同样脱离 <p> 包裹）
    s = s.replace(/\u0002(\d+)\u0002/g, function (_, i) { return '</p>' + (notices[Number(i)] || '') + '<p>'; });
    s = s.replace(/<p>\s*<\/p>/g, '');
    s = s.replace(/\u0000(cb\d+)\u0000/g, function (_, id) {
      var b = null;
      for (var i = 0; i < blocks.length; i++) { if (blocks[i].id === id) b = blocks[i]; }
      if (!b) return '';
      var raw = String(b.code || '');
      // 进度块：```progress / plan / steps / tasks
      if (/^(progress|plan|steps|tasks)$/i.test(b.lang || '')) return renderProgressBlock(raw);
      var isShell = /^(bash|sh|shell|powershell|cmd|bat)$/i.test(b.lang || '');
      // 终端输出块：语言显式标注，或首行是命令提示符（$ / PS>）且不是常见源码语言
      var firstLine = raw.split('\n')[0] || '';
      var isTerm = /^(terminal|console|shell-session|ansi|output|log)$/i.test(b.lang || '') ||
        (/^\$[ \t]/.test(firstLine) && !/^(js|javascript|ts|typescript|py|python|html|css|json|java|go|rs)/i.test(b.lang || ''));
      var btns = '<button data-diff="' + id + '" title="整文件 diff 预览">Diff</button>' +
        '<button data-hunkdiff="' + id + '" title="逐块选择要应用的改动">逐块</button>' +
        '<button data-apply="' + id + '" class="primary">应用</button>' +
        '<button data-copy="' + id + '">复制</button>' +
        '<button data-insert="' + id + '">插入</button>';
      if (isShell) btns = '<button data-run="' + id + '" class="primary">Run</button>' + btns;
      if (isTerm) {
        // 终端样式：命令行走 termCmd（绿色提示符），其余走 termLine；不带行号
        var termBody = '';
        raw.split('\n').forEach(function (line) {
          if (/^\$[ \t]/.test(line)) {
            termBody += '<div class="termCmd"><span class="pr">$</span>' + esc(line.replace(/^\$[ \t]/, '')) + '</div>';
          } else if (/^(PS>|>)[ \t]/.test(line)) {
            termBody += '<div class="termCmd"><span class="pr">›</span>' + esc(line.replace(/^(PS>|>)[ \t]/, '')) + '</div>';
          } else {
            termBody += '<div class="termLine">' + esc(line) + '</div>';
          }
        });
        return '<div class="term"><div class="codeHead"><span class="termTag">' + esc(b.lang || 'terminal') + '</span>' +
          '<span class="spacer"></span><button data-copy="' + id + '">复制</button></div>' +
          '<div class="termBody">' + termBody + '</div></div>';
      }
      // 普通代码块：行号 + 等宽对齐；超过 20 行折叠，保留「展开」按钮
      var lines = raw.split('\n');
      var total = lines.length;
      var lnW = 'calc(' + String(total).length + 'ch + 16px)';
      var folded = total > 20;
      var body = '';
      for (var li = 0; li < total; li++) {
        if (folded && li === 20) body += '<div class="foldMore">';
        body += '<span class="cl"><span class="ln" style="width:' + lnW + '">' + (li + 1) + '</span>' +
          '<span class="lc">' + hlLine(lines[li], b.lang) + '</span></span>';
      }
      if (folded) body += '</div>';
      var foldBtn = folded ? '<button data-fold="' + id + '" data-total="' + total + '">展开 ' + (total - 20) + ' 行</button>' : '';
      return '<div class="code"><div class="codeHead"><span class="langTag">' + esc(b.lang) + '</span>' +
        '<span class="spacer"></span>' + foldBtn + btns +
        '</div><div class="codeBody">' + body + '</div></div>';
    });
    window.__codeCache = window.__codeCache || {};
    blocks.forEach(function (b) { window.__codeCache[b.id] = b.code; });
    return s;
  }

  chatEl.addEventListener('click', function (e) {
    var t = e.target;
    // 图片点击放大（灯箱）
    if (t && t.tagName === 'IMG' && t.classList.contains('md-img')) {
      openLightbox(t.getAttribute('src') || t.src);
      return;
    }
    if (!t || t.tagName !== 'BUTTON') return;
    // 代码块折叠：只切换前端显示，不影响缓存里的原始代码
    var foldId = t.getAttribute('data-fold');
    if (foldId) {
      var box = t.closest ? t.closest('.code') : null;
      if (box) {
        box.classList.toggle('open');
        var opened = box.classList.contains('open');
        var total = Number(t.getAttribute('data-total') || 0);
        t.textContent = opened ? '收起' : ('展开 ' + Math.max(0, total - 20) + ' 行');
      }
      return;
    }
    var cid = t.getAttribute('data-copy');
    var iid = t.getAttribute('data-insert');
    var aid = t.getAttribute('data-apply');
    var rid = t.getAttribute('data-run');
    var did = t.getAttribute('data-diff');
    var hid = t.getAttribute('data-hunkdiff');
    var spk = t.getAttribute('data-speak');
    var code = null;
    if (cid || iid || aid || rid || did || hid) code = (window.__codeCache || {})[cid || iid || aid || rid || did || hid];
    // 运行/应用/插入等代码操作依赖扩展，直连模式下统一提示
    if ((rid || did || hid || aid || iid) && needIde()) return;
    if (rid) {
      post({ type: 'runCommand', code: code || '' });
    } else if (did) {
      post({ type: 'diffCode', code: code || '' });
    } else if (hid) {
      post({ type: 'hunkDiff', code: code || '' });
    } else if (aid) {
      post({ type: 'applyCode', code: code || '' });
    } else if (cid) {
      navigator.clipboard.writeText(code || '').then(function () { toast('已复制'); },
        function () { post({ type: 'copy', code: code || '' }); });
    } else if (iid) {
      post({ type: 'insert', code: code || '' });
    } else if (spk !== null) {
      speak(mdToPlain(t.getAttribute('data-speak') || lastAiText));
    }
  });

  /* ---------- 图片灯箱 ---------- */
  function openLightbox(url) {
    if (!url) return;
    lbImg.src = url;
    lightboxEl.classList.add('show');
  }
  lightboxEl.addEventListener('click', function () { lightboxEl.classList.remove('show'); });

  /* ---------- Markdown 转纯文本（用于 TTS 朗读） ---------- */
  function mdToPlain(text) {
    if (!text) return '';
    return String(text)
      .replace(/```[\s\S]*?```/g, '（代码块）')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '（图片）')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/^#{1,6}\s+/gm, '')
      .replace(/^\s*[-*·]\s+/gm, '')
      .replace(/^\s*\d+\.\s+/gm, '')
      .replace(/\|/g, ' ')
      .replace(/[>*_~`]/g, '')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  /* ---------- TTS 语音合成 ---------- */
  function stopSpeak() {
    if (curSpeakAudio) {
      try { curSpeakAudio.pause(); curSpeakAudio.removeAttribute('src'); } catch (_) {}
      curSpeakAudio = null;
    }
  }
  function speak(text) {
    if (!text) return;
    stopSpeak();
    var shortText = text.substring(0, 200);
    var url = SERVER + '/tts?text=' + encodeURIComponent(shortText) + '&emotion=' + encodeURIComponent(ttsEmotion);
    if (ttsVoice) url += '&voice=' + encodeURIComponent(ttsVoice);
    var audio = new Audio(url);
    curSpeakAudio = audio;
    audio.onended = audio.onerror = function () { if (curSpeakAudio === audio) curSpeakAudio = null; };
    audio.play().catch(function () { toast('语音播放失败'); });
  }
  // 加载音色 / 情感列表
  api('/voices').then(function (r) {
    if (!r.ok || !r.data) return;
    var vs = r.data.voices || {}, es = r.data.emotions || {};
    var vHtml = '<option value="">默认</option>';
    Object.keys(vs).forEach(function (k) {
      vHtml += '<option value="' + esc(k) + '"' + (k === ttsVoice ? ' selected' : '') + '>' + esc(vs[k].name) + '</option>';
    });
    voiceSel.innerHTML = vHtml;
    var eHtml = '';
    Object.keys(es).forEach(function (k) {
      eHtml += '<option value="' + esc(k) + '"' + (k === ttsEmotion ? ' selected' : '') + '>' + esc(es[k].label) + '</option>';
    });
    emotionSel.innerHTML = eHtml;
  });
  voiceSel.addEventListener('change', function () { ttsVoice = voiceSel.value; });
  emotionSel.addEventListener('change', function () { ttsEmotion = emotionSel.value; });
  ttsOn.addEventListener('change', function () {
    ttsEnabled = ttsOn.checked;
    toast(ttsEnabled ? '语音回复已开启' : '语音回复已关闭');
  });
  document.getElementById('btnTts').addEventListener('click', function (e) {
    e.stopPropagation();
    ttsPanel.classList.toggle('show');
  });
  document.addEventListener('click', function (e) {
    if (!ttsPanel.contains(e.target) && e.target.id !== 'btnTts') ttsPanel.classList.remove('show');
  });

  /* ---------- 大脑可视化区：开关 / 下边缘拖拽调高度 ---------- */
  var BRAIN_H_MIN = 120;
  function brainMaxH() {
    // 浮层内拖动：高度上限取浮层可用空间，避免撑出面板
    if (brainPop && brainPop.classList.contains('show')) {
      var avail = brainPop.clientHeight - 70;   // 扣除浮层头与 token 条
      return Math.max(BRAIN_H_MIN, Math.min(Math.round(window.innerHeight * 0.5), avail));
    }
    return Math.max(BRAIN_H_MIN, Math.min(Math.round(window.innerHeight * 0.6),
      Math.round(window.innerHeight) - 240));
  }
  var savedBrainH = 180;
  function applyBrainHeight(h) {
    h = Math.max(BRAIN_H_MIN, Math.min(brainMaxH(), Math.round(h)));
    brainView.style.height = h + 'px';
    return h;
  }
  function applyBrainSwitch() {
    var on = brainOn.checked;
    brainView.classList.toggle('show', on);
    brainResize.style.display = on ? '' : 'none';
    if (on) {
      if (!brainFrame.src) brainFrame.src = brainFrame.dataset.src;
      applyBrainHeight(savedBrainH);
    }
  }
  brainFrame.dataset.src = SERVER + '/brain?embed=1';
  brainOn.checked = false;   // 面板默认收起，节省空间
  brainOn.addEventListener('change', applyBrainSwitch);
  var brainDrag = null;
  function onBrainMove(e) {
    if (!brainDrag) return;
    brainDrag.h = applyBrainHeight(brainDrag.h + (e.clientY - brainDrag.y));
    brainDrag.y = e.clientY;
  }
  function endBrainDrag() {
    if (!brainDrag) return;
    savedBrainH = brainView.clientHeight;
    brainDrag = null;
    brainResize.classList.remove('dragging');
    document.body.style.userSelect = '';
    brainFrame.style.pointerEvents = '';
    document.removeEventListener('pointermove', onBrainMove, true);
    document.removeEventListener('pointerup', endBrainDrag, true);
    document.removeEventListener('pointercancel', endBrainDrag, true);
  }
  brainResize.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    brainDrag = { y: e.clientY, h: brainView.clientHeight };
    brainResize.classList.add('dragging');
    document.body.style.userSelect = 'none';
    brainFrame.style.pointerEvents = 'none';
    try { brainResize.setPointerCapture(e.pointerId); } catch (_) {}
    document.addEventListener('pointermove', onBrainMove, true);
    document.addEventListener('pointerup', endBrainDrag, true);
    document.addEventListener('pointercancel', endBrainDrag, true);
  });
  window.addEventListener('resize', function () { if (brainOn.checked) applyBrainHeight(savedBrainH); });
  applyBrainSwitch();

  /* ---------- 大脑 / Token 浮层：顶栏图标唤起，首次打开才加载 iframe ---------- */
  function toggleBrainPop(show) {
    var on = (show === undefined) ? !brainPop.classList.contains('show') : !!show;
    brainPop.classList.toggle('show', on);
    btnBrainEl.classList.toggle('on', on);
    if (on) {
      if (!brainOn.checked) brainOn.checked = true;   // 首次打开才启用，iframe 在此刻懒加载
      applyBrainSwitch();
      fetchTokenStats();   // 打开浮层刷新一次用量
    }
  }
  btnBrainEl.addEventListener('click', function (e) {
    e.stopPropagation();
    toggleBrainPop();
  });
  document.getElementById('brainPopClose').addEventListener('click', function () { toggleBrainPop(false); });
  document.addEventListener('click', function (e) {
    // 点到浮层外、图标外则收起（iframe 内点击不会冒泡到本面板）
    if (!brainPop.classList.contains('show')) return;
    if (brainPop.contains(e.target) || btnBrainEl.contains(e.target)) return;
    toggleBrainPop(false);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && brainPop.classList.contains('show')) toggleBrainPop(false);
  });

  /* ---------- Token 用量统计条 ---------- */
  tokenBar.addEventListener('click', function () {
    tokenBar.classList.toggle('open');
    tokenDetail.classList.toggle('show');
  });
  function tokFmt(n) {
    n = +n || 0;
    return n >= 10000 ? (n / 1000).toFixed(1) + 'k' : String(n);
  }
  var lastTokSample = { t: 0, g: 0 };      // 上一次采样（算 tok/s）
  function fetchTokenStats() {
    api('/token_stats').then(function (r) {
      if (!r.ok || !r.data) return;
      var st = r.data;
      var c = st.current || {}, t = st.total || {};
      var generating = !!(c.phase && c.phase !== 'idle');
      // 生成速率 tok/s：相邻两次采样的 gen_tokens 差值 / 间隔秒数
      var now = Date.now() / 1000, tps = 0;
      if (generating && lastTokSample.t > 0 && now > lastTokSample.t) {
        tps = Math.max(0, ((c.gen_tokens || 0) - lastTokSample.g) / (now - lastTokSample.t));
      }
      lastTokSample = { t: now, g: c.gen_tokens || 0 };
      tokCur.innerHTML = generating
        ? '<span class="tok-phase">' + esc(c.phase) + '</span> ↑<b>' + tokFmt(c.prompt_tokens) + '</b> ↓<b>' + tokFmt(c.gen_tokens) + '</b>' +
          (tps > 0.05 ? ' <span style="color:var(--blue)">' + tps.toFixed(1) + ' tok/s</span>' : '')
        : '本轮 ↑<b>' + tokFmt(c.prompt_tokens) + '</b> ↓<b>' + tokFmt(c.gen_tokens) + '</b>';
      tokTotal.innerHTML = '累计 <b>' + tokFmt(t.tokens) + '</b> · <b>' + (t.turns || 0) + '</b> 轮';
      // ---- 展开区：上下文占用条 + 每轮消耗趋势图 + 分会话统计 ----
      var html = '';
      var limit = st.context_limit || 0;
      if (limit > 0) {
        var pct = Math.min(100, (c.prompt_tokens || 0) / limit * 100);
        var cls = pct > 80 ? 'crit' : pct > 60 ? 'warn' : '';
        html += '<div class="ctx-wrap" title="当前上下文占用（prompt_tokens / 窗口上限）">' +
          '<span style="flex:none;font-size:10px">上下文</span>' +
          '<div class="ctx-track"><div class="ctx-fill ' + cls + '" style="width:' + pct.toFixed(1) + '%"></div></div>' +
          '<span class="ctx-pct ' + cls + '">' + pct.toFixed(0) + '% · ' + tokFmt(c.prompt_tokens) + '/' + tokFmt(limit) + '</span></div>';
      }
      var rt = st.recent_turns || [];
      if (rt.length) {
        var maxV = 1;
        rt.forEach(function (x) { maxV = Math.max(maxV, (x.p || 0), (x.g || 0)); });
        html += '<div class="trend-wrap"><div class="trend-cap">最近 ' + rt.length + ' 轮消耗（↑输入 ↓生成）</div>' +
          '<div class="trend">' + rt.map(function (x) {
            var hp = Math.max(2, (x.p || 0) / maxV * 100);
            var hg = Math.max(x.g ? 2 : 0, (x.g || 0) / maxV * 100);
            return '<div class="col" title="' + esc(x.t || '') + ' ↑' + (x.p || 0) + ' ↓' + (x.g || 0) + '">' +
              '<div class="b up" style="height:' + hp.toFixed(1) + '%"></div>' +
              (x.g ? '<div class="b dn" style="height:' + hg.toFixed(1) + '%"></div>' : '') +
              '</div>';
          }).join('') + '</div></div>';
      }
      var ss = st.sessions || [];
      html += ss.length
        ? ss.map(function (s) {
            return '<div class="td-row"><span class="td-id">' + esc(s.session_id) + '</span>' +
              '<span><b>' + (s.turns || 0) + '</b> 轮</span>' +
              '<span>↑<b>' + tokFmt(s.prompt_tokens) + '</b> ↓<b>' + tokFmt(s.gen_tokens) + '</b></span>' +
              '<span class="td-last">' + esc(s.last || '') + '</span></div>';
          }).join('')
        : '<div class="td-row">暂无分会话数据</div>';
      tokenDetail.innerHTML = html;
    });
  }
  function startTokenPoll(fast) {
    stopTokenPoll();
    fetchTokenStats();
    tokenTimer = setInterval(fetchTokenStats, fast ? 1200 : 4000);   // 生成期间实时刷新本轮 token
  }
  function stopTokenPoll() {
    if (tokenTimer) { clearInterval(tokenTimer); tokenTimer = 0; }
  }

  /* ---------- 文件附加与拖拽 ---------- */
  var DOC_EXTS = ['txt','md','markdown','json','js','ts','jsx','tsx','py','html','htm','css',
    'csv','tsv','log','yaml','yml','xml','ini','conf','env','bat','cmd','ps1','sh',
    'c','h','cpp','hpp','cs','java','go','rs','rb','php','sql','r','swift','kt','vue','toml','svg'];
  function fmtSize(n) {
    return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
  }
  function collectDroppedFiles(dataTransfer, done) {
    var items = dataTransfer && dataTransfer.items;
    var entries = [];
    if (items && items.length) {
      for (var i = 0; i < items.length; i++) {
        if (items[i].kind === 'file') {
          var en = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
          if (en) entries.push(en);
        }
      }
    }
    if (!entries.length) { done(Array.from((dataTransfer && dataTransfer.files) || [])); return; }
    var out = [];
    var pending = entries.length;
    function finish() { if (--pending <= 0) done(out); }
    function readEntry(entry) {
      if (entry.isFile) {
        entry.file(function (f) { out.push(f); finish(); }, function () { finish(); });
      } else if (entry.isDirectory) {
        var reader = entry.createReader();
        var readBatch = function () { reader.readEntries(function (batch) {
          if (!batch.length) { finish(); return; }
          batch.forEach(function (sub) { pending++; readEntry(sub); });
          readBatch();
        }, function () { finish(); }); };
        readBatch();
      } else { finish(); }
    }
    entries.forEach(readEntry);
  }
  function isTextLike(f) {
    var ext = (f.name.split('.').pop() || '').toLowerCase();
    return !f.type || (f.type && (f.type.startsWith('text/') || f.type === 'application/json'
      || f.type === 'application/javascript' || f.type === 'application/xml'))
      || DOC_EXTS.indexOf(ext) >= 0;
  }
  // 附件统一走 /v1/ide/attach 落盘，消息里只带路径引用（模型用 read_* 读取，不猜内容）
  var MAX_ATTACH_BYTES = 25 * 1024 * 1024;
  var IMG_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tif', 'tiff', 'ico'];
  var PDF_EXTS = ['pdf'];
  var OFFICE_EXTS = ['docx', 'xlsx', 'pptx', 'doc', 'xls', 'ppt'];
  function attachKindOf(f) {
    var ext = (String(f.name || '').split('.').pop() || '').toLowerCase();
    if ((f.type && f.type.indexOf('image/') === 0) || IMG_EXTS.indexOf(ext) >= 0) return 'image';
    if (PDF_EXTS.indexOf(ext) >= 0) return 'pdf';
    if (OFFICE_EXTS.indexOf(ext) >= 0) return 'office';
    if (isTextLike(f) || DOC_EXTS.indexOf(ext) >= 0) return 'text';
    return '';
  }
  function kindLabel(kind) {
    return kind === 'image' ? '图片' : kind === 'pdf' ? 'PDF'
      : kind === 'office' ? 'Office 文档' : kind === 'text' ? '文本' : (kind || '文件');
  }
  function handleFiles(files) {
    var added = 0, skipped = [];
    Array.from(files || []).forEach(function (f) {
      if (!f) return;
      var kind = attachKindOf(f);
      if (!kind) { skipped.push(f.name || '未命名文件'); return; }
      if (f.size > MAX_ATTACH_BYTES) {
        toast('「' + f.name + '」' + fmtSize(f.size) + ' 超过 25MB 上限，已跳过');
        return;
      }
      addAttachment(f, kind);
      added++;
    });
    if (skipped.length) {
      toast('格式不支持：' + skipped.join('、') + '（支持图片 / PDF / Word / Excel / PPT / 文本代码）');
    } else if (added) {
      toast('正在上传 ' + added + ' 个附件…');
    }
  }
  var attachSeq = 0;
  function addAttachment(file, kind) {
    var item = {
      id: 'att' + (++attachSeq), kind: kind, name: file.name, size: file.size,
      status: 'uploading', progress: 0, path: '', thumb: '', error: ''
    };
    if (kind === 'image') pendingImgs.push(item); else pendingDocs.push(item);
    renderAttachBar();
    var reader = new FileReader();
    reader.onload = function () {
      var dataUrl = String(reader.result || '');
      if (kind === 'image') {
        downscaleImage(dataUrl, function (thumb) { item.thumb = thumb; renderAttachBar(); });
      }
      uploadAttachment(file, item, dataUrl);
    };
    reader.onerror = function () {
      item.status = 'error'; item.error = '读取文件失败';
      renderAttachBar();
      toast('附件失败：' + item.name + ' — 读取文件失败');
    };
    reader.readAsDataURL(file);
  }
  function uploadAttachment(file, item, dataUrl) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', SERVER + '/v1/ide/attach', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.timeout = 120000;
    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable && ev.total) {
        item.progress = Math.min(99, Math.round(ev.loaded * 100 / ev.total));
        renderAttachBar();
      }
    };
    xhr.onload = function () {
      var j = null;
      try { j = JSON.parse(xhr.responseText || '{}'); } catch (e2) { j = null; }
      var att = j && j.attachment;
      if (xhr.status >= 200 && xhr.status < 300 && j && j.ok && att && att.path) {
        item.path = att.path;
        item.name = att.name || item.name;
        item.size = att.bytes || item.size;
        if (att.kind === 'image') item.kind = 'image';
        item.status = 'ready';
        item.progress = 100;
        renderAttachBar();
        toast('附件已就绪：' + item.name + '（' + kindLabel(item.kind) + '，' + fmtSize(item.size) + '）');
      } else {
        item.status = 'error';
        item.error = (j && (j.error || (j.error && j.error.message))) || ('上传失败 HTTP ' + xhr.status);
        renderAttachBar();
        toast('附件失败：' + item.name + ' — ' + item.error);
      }
    };
    xhr.onerror = function () {
      item.status = 'error'; item.error = '无法连接本地服务';
      renderAttachBar();
      toast('附件失败：' + item.name + ' — 无法连接本地服务');
    };
    xhr.ontimeout = function () {
      item.status = 'error'; item.error = '上传超时';
      renderAttachBar();
      toast('附件失败：' + item.name + ' — 上传超时');
    };
    xhr.send(JSON.stringify({ name: file.name, data: dataUrl }));
  }
  function downscaleImage(dataUrl, cb) {
    var img = new Image();
    img.onload = function () {
      var MAX = 1024;
      if (img.width <= MAX && img.height <= MAX && dataUrl.length < 400 * 1024) return cb(dataUrl);
      var scale = Math.min(1, MAX / Math.max(img.width, img.height));
      var cv = document.createElement('canvas');
      cv.width = Math.round(img.width * scale);
      cv.height = Math.round(img.height * scale);
      cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
      cb(cv.toDataURL('image/jpeg', 0.85));
    };
    img.onerror = function () { cb(dataUrl); };
    img.src = dataUrl;
  }
  function appendAttachRemove(chip, item) {
    var x = document.createElement('button');
    x.className = 'att-x';
    x.title = '移除';
    x.textContent = '✕';
    x.addEventListener('click', function () {
      if (item.contextPending) {
        post({ type: 'removePending', path: item.path });
        return;
      }
      var i = pendingImgs.indexOf(item); if (i >= 0) pendingImgs.splice(i, 1);
      var j2 = pendingDocs.indexOf(item); if (j2 >= 0) pendingDocs.splice(j2, 1);
      renderAttachBar();
    });
    chip.appendChild(x);
  }
  function fileExtLabel(item) {
    var m = /\.([a-z0-9]+)$/i.exec(String(item.name || ''));
    if (m) return m[1].toUpperCase();
    return item.kind === 'image' ? 'IMG' : item.kind === 'office' ? 'DOC'
      : item.kind === 'pdf' ? 'PDF' : item.kind === 'text' ? 'TEXT' : 'FILE';
  }
  function fileTypeBadge(item) {
    var ext = fileExtLabel(item).toLowerCase();
    var word = ext === 'doc' || ext === 'docx';
    var excel = ext === 'xls' || ext === 'xlsx' || ext === 'csv';
    var ppt = ext === 'ppt' || ext === 'pptx';
    var cls = word ? 'word' : excel ? 'excel' : ppt ? 'ppt' : ext === 'pdf' ? 'pdf' : 'file';
    var label = word ? 'W' : excel ? 'X' : ppt ? 'P' : ext === 'pdf' ? 'PDF' : fileExtLabel(item).slice(0, 3);
    return '<span class="doc-ico ' + cls + '">' + esc(label) + '</span>';
  }
  function renderAttachBar() {
    attachBar.innerHTML = '';
    var all = pendingImgs.concat(pendingDocs, pendingContextFiles);
    attachBar.classList.toggle('has', all.length > 0);
    all.forEach(function (item) {
      var stateCls = item.status === 'error' ? 'err' : (item.status === 'uploading' ? 'up' : 'ok');
      var tip = item.name + '（' + kindLabel(item.kind) + '，' + fmtSize(item.size) + '）'
        + (item.status === 'error' ? '\n失败：' + item.error
          : item.status === 'uploading' ? '\n上传中 ' + item.progress + '%'
            : '\n' + (item.path || ''));
      if (item.kind === 'image' && !item.contextPending) {
        var chip = document.createElement('div');
        chip.className = 'att-chip ' + stateCls;
        chip.title = tip;
        var img = document.createElement('img');
        img.src = item.thumb || 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
        img.addEventListener('click', function () { if (item.thumb) openLightbox(item.thumb); });
        chip.appendChild(img);
        if (item.status !== 'ready') {
          var mask = document.createElement('div');
          mask.className = 'att-mask';
          mask.textContent = item.status === 'error' ? '失败' : item.progress + '%';
          chip.appendChild(mask);
        }
        appendAttachRemove(chip, item);
        attachBar.appendChild(chip);
        return;
      }
      var docChip = document.createElement('div');
      docChip.className = 'att-doc ' + stateCls;
      docChip.title = tip;
      var meta = item.status === 'error' ? '失败'
        : item.status === 'uploading' ? (item.progress + '%') : fileExtLabel(item);
      docChip.innerHTML = fileTypeBadge(item)
        + '<span class="doc-body"><span class="doc-name">' + esc(item.name) + '</span>'
        + '<span class="att-meta">' + esc(meta) + '</span></span>';
      appendAttachRemove(docChip, item);
      attachBar.appendChild(docChip);
    });
  }
  fileInput.addEventListener('change', function () {
    handleFiles(fileInput.files);
    fileInput.value = '';
  });
  // 输入框粘贴：图片与文件统一走附件上传（落盘后以路径引用进消息）
  inputEl.addEventListener('paste', function (e) {
    var cd = e.clipboardData || {};
    var files = [];
    var items = cd.items || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === 'file') {
        var f = items[i].getAsFile();
        if (f && (f.type || f.size)) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      handleFiles(files);
      return;
    }
    // 单独粘贴网址时直接变成网页附件，避免长链接挤占输入框
    var pasted = '';
    try { pasted = String(cd.getData('text/plain') || '').trim(); } catch (e2) { }
    if (/^https?:\/\/[^\s]+$/i.test(pasted)) {
      e.preventDefault();
      addWebLink(pasted).catch(function (err) {
        toast('网页链接添加失败：' + (err.message || String(err)));
      });
    }
  });
  // 整窗拖放
  var dragDepth = 0;
  var composerBox = document.getElementById('composerBox');
  function setComposerDragHover(active) {
    if (composerBox) composerBox.classList.toggle('drag-hover', !!active);
  }
  function reportDragDebug(stage, e) {
    try {
      var dt = e && e.dataTransfer;
      var types = Array.from((dt && dt.types) || []).map(function (t) { return String(t); });
      var items = Array.from((dt && dt.items) || []).map(function (it) {
        return { kind: it.kind || '', type: it.type || '' };
      });
      var values = {};
      if (stage === 'drop') types.forEach(function (type) {
        try { values[type] = String(dt.getData(type) || '').slice(0, 4000); } catch (e2) { }
      });
      post({ type: 'dragDebug', stage: stage, types: types, items: items,
        fileCount: dt && dt.files ? dt.files.length : 0, values: values });
    } catch (e2) { }
  }
  window.addEventListener('dragenter', function (e) {
    reportDragDebug('dragenter', e);
    if (!isConversationDrag(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    dragDepth++;
    setComposerDragHover(true);
  }, true);
  window.addEventListener('dragover', function (e) {
    if (!isConversationDrag(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  }, true);
  window.addEventListener('dragleave', function (e) {
    if (!isConversationDrag(e.dataTransfer)) return;
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) setComposerDragHover(false);
  }, true);
  // 从 IDE 资源管理器 / 编辑器标签拖入：dataTransfer 里没有文件，而是 URI 列表，
  // 交给扩展宿主按路径读取（与图片、文档同一套待附加上下文芯片）
  function extractExplorerPaths(dt) {
    if (!dt) return [];
    var uris = [];
    // VS Code 不同版本的资源管理器负载层级不同，递归取 URI，不能只读第一层 uri 字段
    function collect(value) {
      if (!value) return;
      if (typeof value === 'string') { uris.push(value); return; }
      if (Array.isArray(value)) { value.forEach(collect); return; }
      if (typeof value === 'object') Object.keys(value).forEach(function (k) { collect(value[k]); });
    }
    function grab(raw) {
      if (!raw) return;
      var text = String(raw).trim();
      if (!text) return;
      try { collect(JSON.parse(text)); return; } catch (e2) { /* 非 JSON，按行解析 */ }
      text.split(/\r?\n/).forEach(function (line) {
        line = line.trim();
        if (line && line.indexOf('#') !== 0) uris.push(line);
      });
    }
    Array.from(dt.types || []).forEach(function (type) {
      try { grab(dt.getData(type)); } catch (e2) { }
    });
    var paths = [];
    uris.forEach(function (u) {
      u = String(u || '').trim().replace(/^["']|["']$/g, '');
      var p = u;
      if (u.indexOf('file://') === 0) {
        try { p = decodeURIComponent(u.replace(/^file:\/\/\/?/, '')); }
        catch (e2) { p = u.replace(/^file:\/\/\/?/, ''); }
      }
      if (/^\/[a-zA-Z]:/.test(p)) p = p.slice(1); // /D:/x → D:/x
      // 某些 Electron 拖放直接给 Windows 路径，不带 file://
      if (!/^[a-zA-Z]:[\\/]/.test(p) && !/^\\\\/.test(p)) return;
      if (paths.indexOf(p) < 0) paths.push(p);
    });
    return paths;
  }
  function isConversationDrag(dt) {
    var types = Array.from((dt && dt.types) || []);
    return types.some(function (t) {
      var type = String(t).toLowerCase();
      return type === 'files' || type === 'codefiles' || type === 'text/uri-list'
        || type === 'resourceurls' || type.indexOf('application/vnd.code.') === 0;
    });
  }
  function attachDroppedPaths(paths) {
    if (paths.length) {
      toast('正在读取拖入的 ' + paths.length + ' 项…');
      post({ type: 'dropPaths', paths: paths });
    } else {
      toast('未识别拖入内容，请改用 + 上传附件');
    }
  }
  window.addEventListener('drop', function (e) {
    reportDragDebug('drop', e);
    var dt = e.dataTransfer;
    if (!isConversationDrag(dt)) return;   // 普通文本拖入输入框保持原生行为
    e.preventDefault();
    e.stopPropagation();
    dragDepth = 0;
    setComposerDragHover(false);
    // getData 只在 drop 事件同步执行期间可靠，先取出 IDE 资源路径再异步读取文件
    var paths = extractExplorerPaths(dt);
    if (dt && ((dt.files && dt.files.length) || (dt.items && dt.items.length &&
        Array.from(dt.items).some(function (it) { return it.kind === 'file'; })))) {
      collectDroppedFiles(dt, function (files) {
        // IDE 资源拖放可能只有 file 类型的空壳条目，没有 File/Entry，不能静默返回
        if (files && files.length) handleFiles(files);
        else attachDroppedPaths(paths);
      });
      return;
    }
    attachDroppedPaths(paths);
  }, true);

  /* ---------- 输入框上边缘拖拽调高 ---------- */
  var dragState = null;
  function applyInputH(h) {
    var maxH = Math.max(60, Math.round(window.innerHeight * 0.5));
    h = Math.max(42, Math.min(maxH, Math.round(h)));
    inputEl.style.height = h + 'px';
    return h;
  }
  function onInputDragMove(e) {
    if (!dragState) return;
    dragState.h = applyInputH(dragState.h + (dragState.y - e.clientY));
    dragState.y = e.clientY;
  }
  function endInputDrag() {
    if (!dragState) return;
    dragState = null;
    dragHandle.classList.remove('dragging');
    document.body.style.userSelect = '';
    document.removeEventListener('pointermove', onInputDragMove, true);
    document.removeEventListener('pointerup', endInputDrag, true);
    document.removeEventListener('pointercancel', endInputDrag, true);
  }
  dragHandle.addEventListener('pointerdown', function (e) {
    e.preventDefault();
    dragState = { y: e.clientY, h: inputEl.offsetHeight };
    dragHandle.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.addEventListener('pointermove', onInputDragMove, true);
    document.addEventListener('pointerup', endInputDrag, true);
    document.addEventListener('pointercancel', endInputDrag, true);
  });

  /* ---------- 敏感操作确认卡片 ---------- */
  var shownActionId = null;
  function addConfirmCard(pa) {
    if (!pa || pa.id === shownActionId) return;
    shownActionId = pa.id;
    var card = document.createElement('div');
    card.className = 'confirm-card';
    card.innerHTML =
      '<div class="c-title">⚠️ AI 请求执行敏感操作</div>' +
      '<div class="c-desc">工具：<b>' + esc(pa.tool) + '</b>　确认后才会执行，拒绝会通知 AI 改用其他方式。</div>' +
      '<pre>' + esc(JSON.stringify(pa.args, null, 2)) + '</pre>' +
      '<div class="c-btns"><button class="allow">✓ 允许执行</button><button class="deny">✗ 拒绝</button></div>' +
      '<div class="c-state" style="display:none"></div>';
    var btns = card.querySelectorAll('button');
    var allowBtn = btns[0], denyBtn = btns[1];
    var state = card.querySelector('.c-state');
    function decide(allow) {
      allowBtn.disabled = denyBtn.disabled = true;
      state.style.display = 'block';
      state.textContent = allow ? '已允许，AI 正在执行…' : '已拒绝';
      api('/tool/approve', 'POST', { action_id: pa.id, allow: allow }, 10000).then(function (r) {
        if (!r.ok) state.textContent = '提交失败';
      });
    }
    allowBtn.addEventListener('click', function () { decide(true); });
    denyBtn.addEventListener('click', function () { decide(false); });
    chatEl.appendChild(card);
    scrollBottom();
  }

  /* ---------- 导出对话 ---------- */
  function exportChat() {
    if (needIde()) return;   // 导出需扩展落盘
    var msgs = [];
    var rows = chatEl.querySelectorAll('.msg');
    if (!rows.length) { toast('暂无对话可导出'); return; }
    rows.forEach(function (m) {
      if (m.classList.contains('user')) {
        var ub = m.querySelector('.ububble');
        msgs.push({ role: 'user', content: ub ? ub.textContent : '' });
      } else {
        var md = m.querySelector('.md');
        msgs.push({ role: 'assistant', content: md ? md.textContent : '' });
      }
    });
    var blob = new Blob([JSON.stringify(msgs, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    var d = new Date();
    var pad = function (n) { return String(n).padStart(2, '0'); };
    try {
      a.href = URL.createObjectURL(blob);
      a.download = 'wasily_chat_' + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate())
        + '_' + pad(d.getHours()) + pad(d.getMinutes()) + '.json';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      toast('已导出 ' + msgs.length + ' 条对话');
    } catch (e) {
      navigator.clipboard.writeText(JSON.stringify(msgs, null, 2)).then(function () {
        toast('已复制对话 JSON 到剪贴板');
      }, function () { toast('导出失败：' + e.message); });
    }
  }

  // ===== 输入框上方状态栏（TRAE 式：工作进度 + 轮换提示，空闲时隐藏） =====
  var stBar = document.createElement('div');
  stBar.id = 'stBar';
  stBar.style.display = 'none';
  stBar.innerHTML = '<div class="stLine"><span class="spin"></span><span class="stTxt" id="stTxt">正在思考</span></div>' +
    '<div class="stTip" id="stTip"></div>';
  // 插到 composer 前；composer 现在在 #mainCol 内，用它的父节点插入
  var composerEl = document.getElementById('composer');
  var composerParent = composerEl ? composerEl.parentNode : document.body;
  composerParent.insertBefore(stBar, composerEl);

  var ST_TIPS = [
    '排查模块问题时，在输入框用 @ 引用文件夹，让 AI 聚焦范围。',
    '从脚手架开始，别让 AI 从零搭。',
    '描述问题时带上报错原文和复现步骤，定位更快。',
    '大改动前先让 AI 给出修改计划，确认后再动手。',
    '在文件上右键可把它加入对话上下文。'
  ];
  var stTipIdx = 0, stTipTimer = 0;
  function stBarShow(text) {
    var t = document.getElementById('stTxt');
    if (t && text) t.textContent = text;
    stBar.style.display = 'block';
    if (!stTipTimer) {
      var tip = document.getElementById('stTip');
      if (tip) tip.textContent = ST_TIPS[stTipIdx % ST_TIPS.length];
      stTipTimer = setInterval(function () {
        stTipIdx++;
        var tp = document.getElementById('stTip');
        if (tp) tp.textContent = ST_TIPS[stTipIdx % ST_TIPS.length];
      }, 8000);
    }
  }
  function stBarSet(text) {
    var t = document.getElementById('stTxt');
    if (t) t.textContent = text;
  }
  function stBarHide() {
    stBar.style.display = 'none';
    if (stTipTimer) { clearInterval(stTipTimer); stTipTimer = 0; }
  }

  var AV_AI = '<span class="av"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M8 10.5l2.6 2.4L8 15.3M13.6 15.3H16"/></svg></span>';
  var AV_USER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="3.4"/><path d="M5.6 20c.7-3.4 3.3-5.2 6.4-5.2s5.7 1.8 6.4 5.2"/></svg>';

  /* ---------- 消息渲染 ---------- */
  function addUser(text, ref) {
    clearEmpty();
    var wrap = document.createElement('div');
    wrap.className = 'msg user';
    var b = document.createElement('div');
    b.className = 'ububble';
    b.textContent = (ref ? '📎 ' + ref + '\n' : '') + text;
    wrap.appendChild(b);
    // 用户头像固定在气泡右侧
    var ua = document.createElement('div');
    ua.className = 'uav';
    ua.innerHTML = AV_USER;
    wrap.appendChild(ua);
    chatEl.appendChild(wrap);
    scrollBottom();
  }

  var genStartedAt = 0;   // 本轮生成开始时间：用于计算 Trae 式耗时行

  function agentName() {
    try { var n = localStorage.getItem('wasily_agent_name'); if (n && n.trim()) return n.trim(); } catch (e) {}
    return 'wasily';
  }
  function buildNextSteps(text, steps) {
    var t = String(text || ''), tools = (steps || []).map(function (s) { return (s && s.tool) || ''; }).join(' ');
    var out = [];
    if (/部署|deploy|上线|发布/i.test(t + tools)) out.push('好的，部署这个项目 →');
    if (/file_edit|file_write|apply_patch|edit_file/i.test(tools)) out.push('看一下这次改了哪些文件 →');
    if (/生成|页面|网站|HTML/i.test(t)) out.push('继续完善这个页面 →');
    if (!out.length) out.push('继续下一步 →');
    out.push('重新检查一遍结果 →');
    return out.slice(0, 3);
  }
  function renderRecList(list) {
    if (!list || !list.length) return null;
    var box = document.createElement('div');
    box.className = 'rec-list';
    list.forEach(function (t) {
      var b = document.createElement('button');
      b.className = 'rec-pill';
      b.innerHTML = '<span>' + esc(t.replace(/\s*→\s*$/, '')) + '</span><span class="rp-arr">→</span>';
      b.onclick = function () {
        try {
          if (inputEl) {
            inputEl.value = t.replace(/\s*→\s*$/, '');
            inputEl.dispatchEvent(new Event('input'));
            send();
          }
        } catch (e) {}
      };
      box.appendChild(b);
    });
    return box;
  }

  function addAssistant(text, ms, steps) {
    // 服务端没回耗时就按本轮开始时间兜底，保证每条回答都有 Trae 式「任务耗时/已思考」行
    if (!(typeof ms === 'number' && ms > 0) && genStartedAt) ms = Date.now() - genStartedAt;
    clearEmpty();
    stopThinkClock();
    thinkStartTs = 0;
    var wrap = document.createElement('div');
    wrap.className = 'msg ai';
    var head = document.createElement('div');
    head.className = 'aiHead';
    head.innerHTML = AV_AI + '<span class="nm">' + esc(agentName()) + '</span>';
    wrap.appendChild(head);
    var body = document.createElement('div');
    body.className = 'md';
    body.innerHTML = renderMarkdown(text);
    wrap.appendChild(body);
    // 检测助手回复中的文档路径（.docx/.pdf/.md/.pptx/.xlsx），渲染成可下载/打开的卡片
    (function () {
      var docRe = /([A-Za-z]:[\\/][^\s<>"'（）()]+\.(?:docx|doc|pdf|md|pptx|xlsx))/gi;
      var found = {};
      var m;
      while ((m = docRe.exec(text || '')) !== null) {
        var fp = m[1].replace(/[，。、；：]+$/, '');   // 去尾部中文标点
        if (!found[fp]) found[fp] = true;
      }
      var paths = Object.keys(found);
      if (!paths.length) return;
      paths.forEach(function (fp) {
        var fn = fp.split(/[\\/]/).pop();
        var ext = fn.split('.').pop().toLowerCase();
        var iconColor = ext === 'pdf' ? '#e05252' : (ext === 'md' ? '#8b8f9a' : '#4d8dff');
        var iconText = ext === 'pdf' ? 'PDF' : (ext === 'md' ? 'MD' : (ext === 'pptx' ? 'PPT' : (ext === 'xlsx' ? 'XLS' : 'W')));
        var card = document.createElement('div');
        card.className = 'docCard';
        card.innerHTML =
          '<div class="docIco" style="background:' + iconColor + '">' + iconText + '</div>' +
          '<div class="docInfo"><div class="docName">' + esc(fn) + '</div>' +
          '<div class="docPath">' + esc(fp) + '</div></div>' +
          '<div class="docActs">' +
          '<button class="docBtn" data-act="download" title="下载到本地">下载</button>' +
          '<button class="docBtn" data-act="open" title="在 IDE 中打开">打开</button>' +
          '</div>';
        card.querySelector('[data-act=download]').onclick = function () {
          var a = document.createElement('a');
          a.href = SERVER + '/file/download?path=' + encodeURIComponent(fp);
          a.download = fn;
          document.body.appendChild(a); a.click(); a.remove();
        };
        card.querySelector('[data-act=open]').onclick = function () {
          post({ type: 'openFile', path: fp });
        };
        wrap.appendChild(card);
      });
    })();
    var acts = document.createElement('div');
    acts.className = 'aiActions';
    acts.innerHTML =
      '<button data-copyall="1" title="复制"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6.2A1.7 1.7 0 0 0 13.8 4.5H6.2A1.7 1.7 0 0 0 4.5 6.2v7.6a1.7 1.7 0 0 0 1.7 1.7H8.5"/></svg></button>' +
      '<button data-speak="' + esc(text || '').replace(/"/g, '&quot;') + '" title="朗读"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg></button>' +
      '<button data-check="1" title="AI 自检"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5 19 6v6.2c0 4.2-3 6.8-7 8.3-4-1.5-7-4.1-7-8.3V6z"/><path d="M9.2 12.2l2 2 3.6-3.9"/></svg></button>' +
      '<button data-good="1" title="回答有帮助"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M7 20V10.5l4.4-6.2c.6-.9 2-.5 2 .6V9h4.4c1.2 0 2 1.1 1.8 2.2l-1.2 6.6A2.2 2.2 0 0 1 16.2 20z"/><path d="M7 10.5H4.2V20H7"/></svg></button>' +
      '<button data-bad="1" title="回答有问题"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M17 4v9.5L12.6 19.7c-.6.9-2 .5-2-.6V15H6.2c-1.2 0-2-1.1-1.8-2.2l1.2-6.6A2.2 2.2 0 0 1 7.8 4z"/><path d="M17 13.5h2.8V4H17"/></svg></button>';
    // 过程容器：从对话根移入当前消息内，摘要行常显，点摘要行展开明细
    var procWrap = document.getElementById('procWrap');
    var hasSteps = !!(steps && steps.length);
    var meta = null;
    if (!hasSteps && !procWrap && typeof ms === 'number' && ms > 0) {
      // 纯对话也要有 Trae 式可折叠耗时行（点击展开/收起），不再用不可点的纯文本
      procWrap = document.createElement('div');
      procWrap.className = 'procWrap';
      wrap.insertBefore(procWrap, wrap.firstChild);
    }
    // 把生成期间的 procWrap 挪进本条消息；没有则用 steps 现场构造
    if (procWrap) {
      procWrap.removeAttribute('id');   // 释放 id，避免下一轮 addThinking 的 getElementById 命中旧节点
      procWrap.classList.remove('live');   // 结束后保留折叠摘要行，明细由摘要行控制
      // 思考块先摘出来：它承载预分析的真实推理，steps 里不一定有，不能被一起清掉
      var liveThink = procWrap.querySelector('.thinkBlock');
      if (liveThink && liveThink.parentNode) liveThink.parentNode.removeChild(liveThink);
      // 计划卡同样保留，并跟随折叠区一起收起
      var livePlan = procWrap.querySelector('.stagePlan');
      if (livePlan && livePlan.parentNode) livePlan.parentNode.removeChild(livePlan);
      // 清空生成期的实时内容，用服务端返回的 steps 重渲染（摘要行 + 思考/工具序列）
      var olds = procWrap.querySelectorAll('.procFlow, .procTally, #thinkingRow');
      for (var oi = 0; oi < olds.length; oi++) olds[oi].remove();
      if (hasSteps) renderProcSteps(steps, procWrap);
      if (liveThink || livePlan) {
        var nf = procWrap.querySelector('.procFlow');
        if (!nf) { nf = document.createElement('div'); nf.className = 'procFlow'; procWrap.insertBefore(nf, procWrap.firstChild); }
        // 顺序对齐 Trae：思考正文在上，计划/步骤在下
        if (liveThink) {
          // 思考块自带「已思考 N 秒」表头，会和下方汇总行重复；收进折叠区后只保留正文
          var _th = liveThink.querySelector('.tbHead');
          if (_th && _th.parentNode) _th.parentNode.removeChild(_th);
          nf.insertBefore(liveThink, nf.firstChild);
        }
        if (livePlan) nf.appendChild(livePlan);
      }
      ensureProcTally(procWrap, ms, steps);
      // 摘要上方保留最后一段思考原文（Trae 式）
      var ph = procWrap.querySelector('.procPhrase');
      if (ph) {
        var lastThink = '';
        for (var si = 0; si < (steps || []).length; si++) {
          if (steps[si] && steps[si].think) lastThink = String(steps[si].think);
        }
        if (lastThink) {
          ph.textContent = lastThink.length > 140 ? lastThink.slice(0, 140) + '…' : lastThink;
        } else {
          ph.remove();
        }
      }
      procWrap.classList.add('fold');
      // body 此时还未挂到 wrap，只能用 appendChild；用 insertBefore(procWrap, body) 会抛 NotFoundError
      wrap.appendChild(procWrap);
    } else if (hasSteps) {
      // 无过程容器（如历史回放）：现场建一个折叠的
      procWrap = document.createElement('div');
      procWrap.className = 'procWrap';
      renderProcSteps(steps, procWrap);
      ensureProcTally(procWrap, ms, steps);
      wrap.appendChild(procWrap);
    }
    // 预分析产出的计划卡若还挂在对话根上，收进本条消息：思考 → 计划 → 正文
    for (var pi = 0; pi < chatEl.children.length; pi++) {
      var pc = chatEl.children[pi];
      if (pc && pc.classList && pc.classList.contains('stagePlan')) {
        wrap.appendChild(pc);
        break;
      }
    }
    wrap.appendChild(body);
    var recs = renderRecList(buildNextSteps(text, steps));
    if (recs) wrap.appendChild(recs);
    wrap.appendChild(acts);
    chatEl.appendChild(wrap);
    genStartedAt = 0;
    lastAiWrap = wrap;
    acts.addEventListener('click', function (e) {
      e.stopPropagation();
      var btn = e.target.closest ? e.target.closest('button') : null;
      if (!btn) return;
      if (btn.getAttribute('data-copyall')) {
        navigator.clipboard.writeText(text || '');
        toast('已复制');
      } else if (btn.getAttribute('data-speak') !== null) {
        speak(mdToPlain(text));
      } else if (btn.getAttribute('data-check')) {
        if (!needIde()) post({ type: 'selfcheck' });
      } else {
        toast(btn.getAttribute('data-good') ? '已记录：回答有帮助' : '已记录：回答有问题');
      }
    });
    lastAiText = text || '';
    if (ttsEnabled && text && !quietRender) speak(mdToPlain(text));
    scrollBottom();
    return wrap;
  }

  function fmtMs(ms) {
    var s = Math.round(ms / 1000);
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + (s % 60) + 's';
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
  }

  // 过程容器必须有可点击的摘要行：有工具→「任务耗时 Ns」，纯思考→「已思考 Ns」
  function ensureProcTally(host, msVal, steps) {
    if (!host) return;
    var hasTool = false;
    for (var i = 0; i < (steps || []).length; i++) {
      if (steps[i] && steps[i].tool) { hasTool = true; break; }
    }
    var dur = (typeof msVal === 'number' && msVal > 0) ? fmtMs(msVal) : '';
    var tal = host.querySelector('.procTally');
    if (!tal) {
      tal = document.createElement('div');
      tal.className = 'procTally';
      tal.innerHTML = '<span class="txt">思考过程' + (dur ? ' ' + dur : '') +
                      '</span><span class="cv">›</span>';
      tal.addEventListener('click', function () { host.classList.toggle('fold'); });
      host.insertBefore(tal, host.firstChild);
    } else if (dur) {
      var tx = tal.querySelector('.txt');
      // Trae 摘要行固定叫「思考过程」，时长跟在后面
      if (tx) tx.textContent = '思考过程 ' + dur;
    }
    // Trae 提示卡：有工具执行时给一条可回查的建议
    if (hasTool && !host.querySelector('.tk-tip')) {
      var tip = document.createElement('div');
      tip.className = 'tk-tip';
      tip.innerHTML = '<span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg></span>' +
        '<span>整理资料结论时，可让我标明每条结论对应的原文位置，方便回查。</span>';
      host.insertBefore(tip, tal.nextSibling);
    }
    host.classList.add('fold');
  }

  /* ---------- Trae 式思考块：thinking 事件流（与正文 delta 分开） ---------- */
  function thinkBlockPush(text) {
    if (!text) return;
    var flow = document.getElementById('liveFlow');
    if (!flow) return;
    var blk = flow.querySelector('.thinkBlock');
    if (!blk) {
      var pw = document.getElementById('procWrap');
      if (pw) {
        pw.classList.add('has-thinking');
        pw.classList.remove('fold');
      }
      blk = document.createElement('div');
      blk.className = 'thinkBlock stream';
      blk.__t0 = Date.now();
      blk.innerHTML =
        '<div class="tbHead"><span class="dotg"></span><span class="lbl">思考中</span>' +
        '<span class="st">0s</span><span class="cv">⌄</span></div>' +
        '<div class="tbBody"></div>';
      blk.querySelector('.tbHead').addEventListener('click', function () {
        blk.classList.toggle('fold');
      });
      flow.insertBefore(blk, flow.firstChild);   // 思考块在最上方，先于工具/计划
    }
    var body = blk.querySelector('.tbBody');
    body.textContent += text;
    // Trae 式头部：思考中 + 实时秒数（不再显示字数）
    var sec = Math.max(0, Math.round((Date.now() - (blk.__t0 || Date.now())) / 1000));
    var st = blk.querySelector('.tbHead .st');
    if (st) st.textContent = sec + 's';
    body.scrollTop = body.scrollHeight;
    // 思考原文只在折叠块内显示，不再额外在上方铺一行灰色摘要（避免同一段推理出现两遍）
    scrollBottom();
  }

  function thinkBlockDone() {
    var blk = document.querySelector('#liveFlow .thinkBlock');
    if (!blk) return;
    blk.classList.remove('stream');
    var dot = blk.querySelector('.tbHead .dotg');
    if (dot) dot.remove();
    var sec = Math.max(1, Math.round((Date.now() - (blk.__t0 || Date.now())) / 1000));
    var lb = blk.querySelector('.tbHead .lbl');
    if (lb) lb.textContent = '已思考 ' + sec + ' 秒';
    var st = blk.querySelector('.tbHead .st');
    if (st) st.remove();
    // 思考结束即收起，点击可展开全文（Trae 行为）
    blk.classList.add('fold');
    // 顶部摘要行同步为「已思考 N 秒」，避免还停留在“正在思考”
    var t = document.getElementById('procTally');
    if (t && !tallyParts(liveSteps).length) {
      var tx = t.querySelector('.txt');
      if (tx) tx.textContent = '已思考 ' + sec + ' 秒';
    }
    var ph = document.getElementById('procPhrase');
    if (ph) ph.textContent = '';
  }

  // 历史回放：把预分析思考渲染成与流式一致的「思考过程」块
  function makeStageThinkBlock(text) {
    var blk = document.createElement('div');
    blk.className = 'thinkBlock';
    blk.innerHTML = '<div class="tbHead"><span class="lbl">思考过程</span>' +
      '<span class="st">分析</span><span class="cv">⌄</span></div><div class="tbBody"></div>';
    blk.querySelector('.tbBody').textContent = String(text || '');
    blk.querySelector('.tbHead').addEventListener('click', function () {
      blk.classList.toggle('fold');
    });
    return blk;
  }

  /* ---------- 结构化计划卡（预分析：分析 / 步骤 / 复杂度） ---------- */
  var STAGE_STATE_TXT = { pending: '待执行', running: '进行中', done: '已完成', failed: '失败' };
  var STAGE_COMP_TXT = { simple: '简单', medium: '中等', complex: '复杂' };

  function addStagePlanCard(plan, parent) {
    if (!plan || !plan.steps || !plan.steps.length) return;
    var pid = String(plan.id || '');
    if (pid && chatEl.querySelector('.stagePlan[data-stage-id="' + pid + '"]')) return;
    clearEmpty();
    var card = document.createElement('div');
    card.className = 'stagePlan';
    card.setAttribute('data-stage-id', pid);
    var comp = String(plan.complexity || 'medium');
    card.innerHTML =
      '<div class="spHead"><span class="spTitle">分析 · 计划</span>' +
        '<span class="spBadge ' + esc(comp) + '">' + esc(STAGE_COMP_TXT[comp] || comp) + '</span>' +
        '<span class="spCount">' + plan.steps.length + ' 个步骤</span></div>' +
      '<div class="spGoal"><b>目标：</b>' + esc(plan.goal || '') + '</div>' +
      (plan.analysis ? '<div class="spAnalysis">' + esc(plan.analysis) + '</div>' : '') +
      '<div class="spSteps"></div>' +
      (plan.source && plan.source !== 'model'
        ? '<div class="spNote">未按格式拿到步骤 JSON，以下步骤由真实分析文本派生。</div>' : '');

    var stepsEl = card.querySelector('.spSteps');
    var stateTxt = STAGE_STATE_TXT;

    function renderSteps(steps) {
      stepsEl.innerHTML = (steps || []).map(function (s) {
        var stt = String(s.status || 'pending');
        var dep = (s.depends_on || []).length ? '依赖步骤 ' + s.depends_on.join(', ') : '';
        return '<div class="spStep ' + esc(stt) + '" data-step-id="' + esc(s.id) + '">' +
          '<div class="spTop"><span class="spNo">' + esc(s.id) + '</span>' +
            '<span class="spT">' + esc(s.title || '') + '</span>' +
            '<span class="spSt ' + esc(stt) + '">' + esc(stateTxt[stt] || stt) + '</span></div>' +
          (s.goal ? '<div class="spMeta"><b>目标：</b>' + esc(s.goal) + '</div>' : '') +
          (s.scope ? '<div class="spMeta"><b>范围：</b>' + esc(s.scope) + '</div>' : '') +
          (s.expected ? '<div class="spMeta"><b>产出：</b>' + esc(s.expected) + '</div>' : '') +
          (dep ? '<div class="spMeta">' + esc(dep) + '</div>' : '') +
          (s.result ? '<div class="spMeta"><b>结果：</b>' + esc(String(s.result).slice(0, 200)) + '</div>' : '') +
          ((s.skill) ? '<div class="spTags">' +
            '<span class="spTag skill">⚡ ' + esc(s.skill) + '</span></div>' : '') +
        '</div>';
      }).join('');
    }
    card.__renderSteps = renderSteps;
    renderSteps(plan.steps);
    (parent || chatEl).appendChild(card);
    scrollBottom();
    return card;
  }

  // 供编排执行阶段实时更新步骤状态（待执行/进行中/完成/失败）
  function updateStagePlanStep(planId, stepId, state, resultText) {
    var card = planId
      ? chatEl.querySelector('.stagePlan[data-stage-id="' + planId + '"]')
      : chatEl.querySelector('.stagePlan');
    if (!card) return;
    var row = card.querySelector('.spStep[data-step-id="' + stepId + '"]');
    if (!row) return;
    row.className = 'spStep ' + esc(state);
    var stEl = row.querySelector('.spSt');
    if (stEl) {
      stEl.className = 'spSt ' + esc(state);
      stEl.textContent = STAGE_STATE_TXT[state] || state;
    }
    if (resultText) {
      var meta = row.querySelector('.spResult');
      if (!meta) {
        meta = document.createElement('div');
        meta.className = 'spMeta spResult';
        row.appendChild(meta);
      }
      meta.innerHTML = '<b>结果：</b>' + esc(String(resultText).slice(0, 200));
    }
    scrollBottom();
  }

  /* ---------- 折叠对话：可展开折叠条（新增块类型，不参与既有消息渲染分支） ---------- */
  var foldStateBySid = {};   // sid -> {folds:[], thresholds, folded_through}
  var foldBarsById = {};     // 当前视图里的折叠条 node
  var foldFetchSeq = 0;

  function foldSec(title, items, fmt) {
    if (!items || !items.length) return '';
    var lis = '';
    for (var i = 0; i < items.length; i++) lis += '<li>' + fmt(items[i]) + '</li>';
    return '<div class="fbSec"><b>' + title + '</b><ul>' + lis + '</ul></div>';
  }

  function foldSummaryHTML(sum) {
    sum = sum || {};
    var out = '';
    out += foldSec('已确认的决定', sum.decisions, function (s) { return esc(s); });
    out += foldSec('改动过的文件', sum.files, function (f) {
      var act = (f && f.action) || '涉及';
      return esc((f && f.path) || '') + '<span class="fbAct">' + esc(act) + '</span>';
    });
    out += foldSec('未完成事项', sum.pending, function (s) { return esc(s); });
    out += foldSec('关键结论', sum.conclusions, function (s) { return esc(s); });
    if (!out) out = '<div class="fbSec" style="color:var(--sub2)">（摘要为空）</div>';
    return '<div class="fbSummary">' + out + '</div>';
  }

  function foldOriginalsHTML(fold) {
    var items = (fold && fold.items) || [];
    var html = '';
    for (var i = 0; i < items.length; i++) {
      var it = items[i] || {};
      var who = it.role === 'user' ? '用户' : (it.role === 'assistant' ? '助手' : (it.role || ''));
      html += '<div class="foItem"><b>' + esc(who) + '：</b>' + esc(it.content || '') + '</div>';
    }
    return html || '<div class="foItem" style="color:var(--sub2)">（原文未保留）</div>';
  }

  function makeFoldBar(fold) {
    fold = fold || {};
    var bar = document.createElement('div');
    bar.className = 'foldBar fold';
    bar.setAttribute('data-fold-id', String(fold.id || ''));
    var sum = fold.summary || {};
    var tags = '';
    if (sum.decisions && sum.decisions.length) tags += '<span class="fbTag">决定 ' + sum.decisions.length + '</span>';
    if (sum.files && sum.files.length) tags += '<span class="fbTag">文件 ' + sum.files.length + '</span>';
    if (sum.pending && sum.pending.length) tags += '<span class="fbTag">待办 ' + sum.pending.length + '</span>';
    var count = fold.turn_count || 0;
    bar.innerHTML =
      '<div class="fbHead"><span class="fbIcon">▤</span>' +
      '<span class="fbLabel">已折叠 ' + Math.max(1, count) + ' 轮对话 · 摘要</span>' +
      '<span class="fbTags">' + tags + '</span><span class="cv">⌄</span></div>' +
      '<div class="fbBody">' + foldSummaryHTML(sum) +
      '<div class="fbOrig"><div class="fbOrigTitle">原始消息（仅供查看，已不再注入模型上下文）</div>' +
      foldOriginalsHTML(fold) + '</div>' +
      '<div class="fbNote">展开只用于查看；模型侧仍使用上面的摘要，不会把整段原文重新塞回上下文。</div>' +
      '</div>';
    var head = bar.querySelector('.fbHead');
    if (head) head.addEventListener('click', function () { bar.classList.toggle('fold'); });
    return bar;
  }

  function insertFoldBar(bar) {
    var lastBar = null;
    for (var i = 0; i < chatEl.children.length; i++) {
      var c = chatEl.children[i];
      if (c && c.classList && c.classList.contains('foldBar')) lastBar = c;
    }
    var atBottom = chatEl.scrollTop + chatEl.clientHeight >= chatEl.scrollHeight - 40;
    var prevTop = chatEl.scrollTop;
    var anchor = null;
    if (lastBar) {
      anchor = lastBar.nextSibling;
    } else {
      var firstMsg = chatEl.querySelector('.msg');
      if (firstMsg) anchor = firstMsg;
      else {
        var tip = document.getElementById('historyTip');
        anchor = tip ? tip.nextSibling : chatEl.firstChild;
      }
    }
    chatEl.insertBefore(bar, anchor);
    var h = Math.round(bar.getBoundingClientRect ? bar.getBoundingClientRect().height : 0) || 0;
    if (!atBottom && h) chatEl.scrollTop = prevTop + h;   // 不打乱正在看的位置
  }

  function hideFoldedNodes(folds) {
    if (!folds || !folds.length) return;
    var nodes = chatEl.querySelectorAll('.msg');
    for (var i = nodes.length - 1; i >= 0; i--) {
      var n = nodes[i];
      if (typeof n.__mi !== 'number') continue;
      for (var f = 0; f < folds.length; f++) {
        var rg = (folds[f] && folds[f].range) || [];
        if (n.__mi >= rg[0] && n.__mi <= rg[1]) { n.remove(); break; }
      }
    }
  }

  function applyFoldState(sid, state) {
    if (!sid) return;
    foldStateBySid[sid] = state || { folds: [] };
    if (sid !== currentSid) return;
    var folds = (state && state.folds) || [];
    for (var fid in foldBarsById) {
      if (!Object.prototype.hasOwnProperty.call(foldBarsById, fid)) continue;
      var exists = false;
      for (var i = 0; i < folds.length; i++) {
        if (String(folds[i].id) === fid) { exists = true; break; }
      }
      if (!exists) { try { foldBarsById[fid].remove(); } catch (e) {} delete foldBarsById[fid]; }
    }
    folds.slice().sort(function (a, b) {
      return (((a || {}).range || [0])[0]) - (((b || {}).range || [0])[0]);
    }).forEach(function (fold) {
      var id = String((fold && fold.id) || '');
      if (!id || foldBarsById[id]) return;
      var bar = makeFoldBar(fold);
      foldBarsById[id] = bar;
      insertFoldBar(bar);
    });
    hideFoldedNodes(folds);
  }

  function handleFoldEvent(fold) {
    if (!fold) return;
    var sid = fold.sid || currentSid;
    if (currentSid && sid !== currentSid) return;
    var st = foldStateBySid[sid] || { folds: [] };
    var has = false;
    for (var i = 0; i < (st.folds || []).length; i++) {
      if (String(st.folds[i].id) === String(fold.id)) { has = true; break; }
    }
    if (!has) st.folds = (st.folds || []).concat([fold]);
    foldStateBySid[sid] = st;
    if (sid !== currentSid) return;
    var id = String(fold.id || '');
    if (id && !foldBarsById[id]) {
      var bar = makeFoldBar(fold);
      foldBarsById[id] = bar;
      insertFoldBar(bar);
    }
    hideFoldedNodes([fold]);
  }

  function fetchFoldState(sid) {
    if (!sid || !window.fetch) return;
    var seq = ++foldFetchSeq;
    fetch(SERVER + '/v1/session/fold?sid=' + encodeURIComponent(sid))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || seq !== foldFetchSeq || sid !== currentSid) return;
        applyFoldState(sid, d);
      })
      .catch(function () {});
  }

  /* ---------- 任务编排卡片（人物分工 + 任务模块 + agent/skill + 工具准备） ---------- */
  var ORCH_ICO = { pending: '⏳', running: '🔄', done: '✅', failed: '❌' };

  function addOrchCard(plan) {
    if (!plan || !plan.steps) return;
    clearEmpty();
    var card = document.createElement('div');
    card.className = 'orch-card';
    card.setAttribute('data-plan-id', plan.id || '');
    var people = plan.people || [];
    card.innerHTML =
      '<div class="oc-title"><span class="oc-label">🧠 任务编排（待确认）</span>' +
      '<span class="oc-count">' + people.length + ' 位角色 · ' + plan.steps.length + ' 个任务</span></div>' +
      '<div class="oc-goal">目标：' + esc(plan.goal || '') + '</div>' +
      '<div class="oc-people">' + people.map(function (p) {
        return '<span class="oc-person" title="' + esc(p.duty || '') + '">' +
          (p.emoji || '👤') + esc(p.name || '') +
          '<span class="du">' + esc(p.agent || '') + '</span></span>';
      }).join('') + '</div>' +
      '<div class="oc-steps"></div>' +
      '<div class="oc-btns"><button class="go">✓ 确认执行</button>' +
      '<button class="cancel">✗ 取消</button></div>' +
      '<div class="oc-state" style="display:none"></div>';

    var stepsEl = card.querySelector('.oc-steps');
    var labelEl = card.querySelector('.oc-label');
    var stateEl = card.querySelector('.oc-state');
    var btns = card.querySelectorAll('.oc-btns button');
    var goBtn = btns[0], cancelBtn = btns[1];

    function renderSteps(steps) {
      stepsEl.innerHTML = steps.map(function (s) {
        var tags = [];
        if (s.agent) tags.push('<span class="oc-tag agent">' + (s.emoji || '') + esc(s.agent) + '</span>');
        if (s.skill) tags.push('<span class="oc-tag skill">⚡ ' + esc(s.skill) + '</span>');
        (s.tools_state || []).forEach(function (t) {
          tags.push('<span class="oc-tag ' + esc(t.status) + '" title="' + esc(t.detail || '') + '">' +
            esc(t.tool) + '</span>');
        });
        if (!s.tools_state && (s.tools || []).length) {
          s.tools.forEach(function (n) { tags.push('<span class="oc-tag">' + esc(n) + '</span>'); });
        }
        return '<div class="oc-step ' + esc(s.status) + '">' +
          '<span class="oi">' + (ORCH_ICO[s.status] || '⏳') + '</span>' +
          '<div style="flex:1;min-width:0">' +
            '<span class="okind">' + esc(s.kind || '任务') + '</span>' +
            '<span class="ot">' + s.id + '. ' + esc(s.title) + '</span>' +
            '<span style="color:var(--sub2);font-size:10px;margin-left:5px">' +
              esc(s.agent_name || s.person || '') + '</span>' +
            (s.detail ? '<div class="odet">' + esc(s.detail) + '</div>' : '') +
            (tags.length ? '<div class="oc-tags">' + tags.join('') + '</div>' : '') +
          '</div></div>';
      }).join('');
    }

    renderSteps(plan.steps);
    chatEl.appendChild(card);
    scrollBottom();

    function decide(action) {
      goBtn.disabled = cancelBtn.disabled = true;
      return api('/plan/confirm', 'POST', { plan_id: plan.id, action: action }, 10000);
    }

    cancelBtn.onclick = function () {
      decide('cancel').then(function () {
        labelEl.textContent = '🧠 任务编排（已取消）';
        stateEl.style.display = 'block';
        stateEl.textContent = '已取消，可继续对话调整需求后重新编排。';
      });
    };

    goBtn.onclick = function () {
      decide('approve').then(function () {
        labelEl.textContent = '🧠 任务编排（执行中）';
        stateEl.style.display = 'block';
        stateEl.textContent = '已确认，各角色开始按分工执行…';
        // 联动：标记活跃 plan + 确保 procWrap 存在 + 启动 toolEvPoll
        setActivePlan(plan.id);
        if (!document.getElementById('procWrap')) {
          addThinking();
        }
        if (!generating) { generating = true; startToolEvPoll(); }
        poll(plan.id);
      });
    };

    function poll(pid) {
      api('/plan/status?id=' + encodeURIComponent(pid), 'GET', null, 10000).then(function (r) {
        var p = r && r.data;
        if (p && p.steps) renderSteps(p.steps);
        if (p && (p.status === 'done' || p.status === 'failed' || p.status === 'canceled')) {
          labelEl.textContent = p.status === 'done' ? '🧠 任务编排（已完成）'
            : p.status === 'failed' ? '🧠 任务编排（有任务失败）' : '🧠 任务编排（已取消）';
          stateEl.textContent = p.status === 'done' ? '全部任务执行完成' : '执行结束，详情见下方总结';
          if (p.summary) addAssistant(p.summary, 0);
          // plan 执行结束 → 停止 toolEvPoll + 清理活跃 plan 标记
          stopToolEvPoll();
          setActivePlan(null);
          generating = false;
          stopThinkClock();
          return;
        }
        setTimeout(function () { poll(pid); }, 2000);
      });
    }
  }

  var thinkLines = [];

  // 思考过程：Trae 式思考块，思考时展开、开始回答后折叠成摘要行
  function addThinking() {
    clearEmpty();
    var wrap = document.createElement('div');
    wrap.className = 'procWrap live';   // 先展开明细，回答开始流式时自动折叠
    wrap.id = 'procWrap';
    var phrase = document.createElement('div');
    phrase.className = 'procPhrase';
    phrase.id = 'procPhrase';
    var tally = document.createElement('div');
    tally.className = 'procTally';
    tally.id = 'procTally';
    tally.innerHTML = '<span class="txt">思考中</span><span class="cv">›</span>';
    tally.addEventListener('click', function () {
      wrap.classList.toggle('fold');
    });
    var flow = document.createElement('div');
    flow.className = 'procFlow';
    flow.id = 'liveFlow';
    wrap.appendChild(phrase);
    wrap.appendChild(tally);
    wrap.appendChild(flow);
    chatEl.appendChild(wrap);
    thinkLines = [];
    liveSteps = [];
    startThinkClock();
    scrollBottom();
    return wrap;
  }

  /* ---------- 流式回答：逐字上屏（工具调用轮自动撤回） ---------- */
  var liveWrap = null, liveBody = null, liveText = '', liveQueue = '', liveTimer = 0;

  function streamStart() {
    if (liveWrap) return;
    clearEmpty();
    // 有真实思考内容时保持展开，避免正文开始输出后只剩「正在思考」
    var pw = document.getElementById('procWrap');
    if (pw && !pw.classList.contains('has-thinking')) pw.classList.add('fold');
    liveWrap = document.createElement('div');
    liveWrap.className = 'msg ai';
    var head = document.createElement('div');
    head.className = 'aiHead';
    head.innerHTML = AV_AI + '<span class="nm">' + esc(agentName()) + '</span>';
    var tip = document.createElement('div');
    tip.className = 'genTip';
    tip.innerHTML = '<span class="gd"></span>思考中…';
    liveBody = document.createElement('div');
    liveBody.className = 'md';
    liveWrap.appendChild(head);
    liveWrap.appendChild(tip);
    liveWrap.appendChild(liveBody);
    chatEl.appendChild(liveWrap);
    scrollBottom();
  }

  // 增量文字入队，50ms 批量渲染一次，避免逐 token 重排卡顿
  function streamPush(text) {
    if (!text) return;
    streamStart();
    liveQueue += text;
    if (liveTimer) return;
    liveTimer = setTimeout(function () {
      liveTimer = 0;
      if (!liveBody) return;
      liveText += liveQueue;
      liveQueue = '';
      // 上屏前剥掉工具块（完整或流式中的半截），避免 ```tool 围栏/半截 JSON 被当成回答显示
      var _shown = liveText
        .replace(/```tool(?:_call)?[\s\S]*?```/g, '')
        .replace(/```tool(?:_call)?[\s\S]*$/g, '');
      liveBody.innerHTML = renderMarkdown(_shown);
      scrollBottom();
    }, 50);
  }

  // 本轮其实是工具调用：把已吐出的文字转为「思考」行后清空气泡
  function streamReset() {
    // 剥离工具块残留（完整或半截），只把真正的思考文字转入思考行
    var t = liveText
      .replace(/```tool(?:_call)?[\s\S]*?```/g, '')
      .replace(/```tool(?:_call)?[\s\S]*$/g, '')
      .trim();
    if (liveTimer) { clearTimeout(liveTimer); liveTimer = 0; }
    liveText = ''; liveQueue = '';
    if (liveBody) liveBody.innerHTML = '';
    if (t) thinkStep(t.length > 200 ? t.slice(0, 200) + '…' : t);
  }

  function streamEnd() {
    if (liveTimer) { clearTimeout(liveTimer); liveTimer = 0; }
    liveText = ''; liveQueue = ''; liveBody = null;
    if (liveWrap && liveWrap.parentNode) liveWrap.parentNode.removeChild(liveWrap);
    liveWrap = null;
  }

  /* ===== Trae 式实时时间线 ===== */
  var liveSteps = [];   // 镜像：{think} | {tool, args, result, ok, done}
  var thinkStartTs = 0, thinkTimer = 0;   // 生成计时：让摘要行每秒走动，避免看着像卡死

  function startThinkClock() {
    stopThinkClock();
    thinkStartTs = Date.now();
    thinkTimer = setInterval(function () { updateTally(); }, 1000);
  }

  function stopThinkClock() {
    if (thinkTimer) { clearInterval(thinkTimer); thinkTimer = 0; }
  }

  // 收起当前思考块：改标题为「思考」并折叠
  function sealThink() {
    var flow = document.getElementById('liveFlow');
    if (!flow) return;
    var rows = flow.querySelectorAll('.procStep.think.active');
    for (var i = 0; i < rows.length; i++) {
      rows[i].classList.remove('active', 'open');
      rows[i].dataset.done = '1';
      var lb = rows[i].querySelector('.lbl');
      if (lb) lb.textContent = '思考';
      var runs = rows[i].querySelectorAll('.tLine.run');
      for (var j = 0; j < runs.length; j++) {
        runs[j].classList.remove('run');
        var d = runs[j].querySelector('.dot');
        if (d) d.textContent = '✓';
      }
    }
  }

  function thinkStep(text, running, key) { queueOrRun(function () { _thinkStep(text, running, key); }); }
  function _thinkStep(text, running, key) {
    var flow = document.getElementById('liveFlow');
    if (!flow) return;
    var k = key || text;
    // 摘要行上方始终显示最新一段思考原文（Trae 式）
    var ph = document.getElementById('procPhrase');
    if (ph) ph.textContent = text.length > 160 ? text.slice(0, 160) + '…' : text;
    for (var i = 0; i < thinkLines.length; i++) {
      if (thinkLines[i].key === k) {
        if (!running) {   // 同步已完成状态
          thinkLines[i].el.classList.remove('run');
          var d = thinkLines[i].el.querySelector('.dot');
          if (d) d.textContent = '✓';
        }
        return;
      }
    }
    // 连续思考合并进同一圆角框（Trae：工具调用之间各成一块）
    var rows = flow.querySelectorAll('.procStep.think');
    var last = rows.length ? rows[rows.length - 1] : null;
    if (last && last.classList.contains('active')) {
      var bx = last.querySelector('.tx');
      if (bx) {
        var ln = document.createElement('div');
        ln.className = 'tLine' + (running ? ' run' : '');
        ln.innerHTML = '<span class="dot">' + (running ? '◐' : '✓') + '</span><span>' + esc(text) + '</span>';
        bx.appendChild(ln);
        thinkLines.push({ key: k, el: ln });
        scrollBottom();
        return;
      }
    }
    // 新思考块（默认展开，标题「思考中」）
    var pw = document.getElementById('procWrap');
    if (pw) {
      pw.classList.add('has-thinking');
      pw.classList.remove('fold');
    }
    sealThink();
    var row = document.createElement('div');
    row.className = 'procStep think active open';
    row.innerHTML =
      '<div class="procStepHead">' + PROC_ICONS.think + '<span class="lbl">思考中</span>' +
      '<span class="cv">⌄</span></div>' +
      '<div class="procStepBody"><div class="tx"></div></div>';
    row.querySelector('.procStepHead').addEventListener('click', function () {
      row.classList.toggle('open');
    });
    var tx = row.querySelector('.tx');
    var ln2 = document.createElement('div');
    ln2.className = 'tLine' + (running ? ' run' : '');
    ln2.innerHTML = '<span class="dot">' + (running ? '◐' : '✓') + '</span><span>' + esc(text) + '</span>';
    tx.appendChild(ln2);
    flow.appendChild(row);
    thinkLines.push({ key: k, el: ln2 });
    scrollBottom();
  }

  // 历史对话压缩：分隔线 + 折叠统计（Trae 式「已编辑 3 个文件，读取 14 个文件…」）
  function tlCompress(ev) { queueOrRun(function () { _tlCompress(ev); }); }
  function _tlCompress(ev) {
    var flow = document.getElementById('liveFlow');
    if (!flow) {
      // 非生成态（手动 /compact）：在对话末尾单独挂一个容器
      var wrap = document.createElement('div');
      wrap.className = 'procWrap standalone';
      flow = document.createElement('div');
      flow.className = 'procFlow';
      wrap.appendChild(flow);
      chatEl.appendChild(wrap);
    }
    var div = document.createElement('div');
    div.className = 'histSplit';
    div.innerHTML = '<span class="hsLine"></span><span class="hsTxt">历史对话已被压缩</span>' +
      '<span class="hsLine"></span>';
    flow.appendChild(div);
    var st = ev.stats || {};
    var parts = [];
    if (st.edit) parts.push('已编辑 ' + st.edit + ' 个文件');
    if (st.read) parts.push('读取 ' + st.read + ' 个文件');
    if (st.search) parts.push('搜索 ' + st.search + ' 次文件');
    if (st.list) parts.push('浏览 ' + st.list + ' 个目录');
    if (st.cmd) parts.push('执行 ' + st.cmd + ' 条命令');
    var row = document.createElement('div');
    row.className = 'procSumRow';
    row.innerHTML = '<span class="txt">' + esc(parts.length ? parts.join('，') : '较早对话已折叠') +
      '</span><span class="cv">›</span>';
    var detail = document.createElement('div');
    detail.className = 'procSumDetail';
    detail.textContent = (ev.manual ? '手动压缩' : '自动压缩') +
      (ev.folded ? ' · 折叠 ' + ev.folded + ' 条' : '') +
      (ev.turns ? ' · 折叠 ' + ev.turns + ' 轮' : '') +
      ' · 约 ' + (ev.before || 0) + ' → ' + (ev.after || 0) + ' tokens';
    row.addEventListener('click', function () {
      row.classList.toggle('open');
      detail.classList.toggle('show');
    });
    flow.appendChild(row);
    flow.appendChild(detail);
    scrollBottom();
  }

  // 工具行开始（applyPhase 与 tool_events 轮询共用，按工具名去重）
  function tlToolStart(tool, args, confirm) { queueOrRun(function () { _tlToolStart(tool, args, confirm); }); }
  function _tlToolStart(tool, args, confirm) {
    var flow = document.getElementById('liveFlow');
    if (!flow || !tool) return;
    sealThink();
    var pend = flow.querySelectorAll('.procStep.tool.pending');
    var row = null;
    for (var i = 0; i < pend.length; i++) {
      if (pend[i].dataset.tool === String(tool)) { row = pend[i]; break; }
    }
    if (row) {   // 复用未完成行：补参数并刷新标题
      if (args) {
        var pre = row.querySelector('.args pre');
        if (pre && (!pre.textContent || pre.textContent === '{}')) pre.textContent = JSON.stringify(args, null, 2);
        var pl0 = procRunningLabel(tool, args);
        var lb0 = row.querySelector('.lbl'), cm0 = row.querySelector('.cmd');
        if (lb0) lb0.textContent = pl0.label;
        if (cm0) cm0.textContent = pl0.detail || '';
      }
      if (confirm) {
        var stc = row.querySelector('.st');
        if (stc) { stc.textContent = '待确认'; stc.className = 'st run'; }
      }
      scrollBottom();
      return;
    }
    var a = args || {};
    var kind0 = procKindOf(tool);
    var isLeaf = (kind0 === 'read' || kind0 === 'search' || kind0 === 'list');
    var pl = procRunningLabel(tool, a);
    row = document.createElement('div');
    row.className = 'procStep tool pending' + (isLeaf ? ' leaf' : ' open');
    row.dataset.tool = String(tool);
    row.innerHTML =
      '<div class="procStepHead">' + PROC_ICONS[pl.ico] +
      '<span class="lbl">' + esc(pl.label) + '</span>' +
      (pl.detail ? '<span class="cmd">' + esc(pl.detail) + '</span>' : '') +
      '<span class="st run">' + (confirm ? '待确认' : '执行中…') + '</span>' +
      (isLeaf ? '' : '<span class="cv">⌄</span>') + '</div>' +
      (isLeaf ? '' :
      '<div class="procStepBody">' +
        '<div class="args">参数：<pre style="margin:2px 0 0;white-space:pre-wrap;word-break:break-word;font-size:10.5px">' + esc(JSON.stringify(a, null, 2)) + '</pre></div>' +
        '<div class="resultTxt"></div>' +
      '</div>');
    if (!isLeaf) {
      row.querySelector('.procStepHead').addEventListener('click', function () {
        row.classList.toggle('open');
      });
    }
    flow.appendChild(row);
    liveSteps.push({ tool: tool, args: a, result: '', done: false });
    updateTally();
    scrollBottom();
  }

  // 工具行完成：写结果、更新统计，行保持一行（点击可回看参数与结果）
  function tlToolDone(result, ok, dur) { queueOrRun(function () { _tlToolDone(result, ok, dur); }); }
  function _tlToolDone(result, ok, dur) {
    var flow = document.getElementById('liveFlow');
    if (!flow) return;
    var pend = flow.querySelectorAll('.procStep.tool.pending');
    var row = pend.length ? pend[pend.length - 1] : null;
    if (!row) return;
    row.classList.remove('pending');
    // 进行中文案 → 完成态文案（TRAE 式：读取中 → 已读取；搜索中 → 找到 N 行）
    var lb0 = row.querySelector('.lbl');
    if (lb0) lb0.textContent = procRowLabel(row.dataset.tool, {}, result).label;
    var st = row.querySelector('.st');
    if (st) {
      st.textContent = (ok === false ? '✗ ' : '✓ ') + (dur != null ? dur + 's' : '完成');
      st.className = 'st ' + (ok === false ? 'err' : 'ok');
    }
    var txt = String(result || '');
    var res = row.querySelector('.resultTxt');
    if (res && txt) res.textContent = txt.length > 1200 ? txt.slice(0, 1200) + '\n…（已截断）' : txt;
    if (ok === false) {
      row.classList.add('open');   // 失败自动展开，便于看到原因
    } else {
      row.classList.remove('open');
    }
    for (var i = liveSteps.length - 1; i >= 0; i--) {
      if (liveSteps[i].tool === row.dataset.tool && !liveSteps[i].done) {
        liveSteps[i].done = true; liveSteps[i].result = txt; liveSteps[i].ok = ok !== false;
        break;
      }
    }
    sealThink();
    updateTally();
    scrollBottom();
  }

  // 实时统计头：Trae 式「已编辑 N 个文件，读取 N 个文件，搜索 N 次…」
  function tallyParts(list) {
    var cnt = { edit: 0, read: 0, search: 0, list: 0, cmd: 0, browser: 0, other: 0 };
    (list || []).forEach(function (s) {
      if (s && s.tool) { var k = procKindOf(s.tool); if (k) cnt[k]++; }
    });
    var parts = [];
    if (cnt.edit) parts.push('已编辑 ' + cnt.edit + ' 个文件');
    if (cnt.read) parts.push('已读取 ' + cnt.read + ' 个文件');
    if (cnt.search) parts.push('搜索 ' + cnt.search + ' 次文件');
    if (cnt.list) parts.push('浏览 ' + cnt.list + ' 个目录');
    if (cnt.cmd) parts.push('执行 ' + cnt.cmd + ' 条命令');
    if (cnt.browser) parts.push('操作浏览器 ' + cnt.browser + ' 次');
    if (cnt.other) parts.push('调用 ' + cnt.other + ' 次工具');
    return parts;
  }
  function updateTally() {
    var t = document.getElementById('procTally');
    if (!t) return;
    var parts = tallyParts(liveSteps);
    var running = '';
    for (var i = liveSteps.length - 1; i >= 0; i--) {
      if (liveSteps[i] && liveSteps[i].tool && !liveSteps[i].done) {
        running = liveActionOf(liveSteps[i].tool);
        break;
      }
    }
    var txt = parts.length ? parts.join('，') : '正在思考';
    if (running) txt += '，' + running + '…';
    else if (!parts.length) txt += '…';
    // 每秒走动：Trae 式「思考 12s」，让用户知道还在推进而不是卡死
    if (thinkStartTs) {
      var sec = Math.floor((Date.now() - thinkStartTs) / 1000);
      if (sec >= 2) {
        var dur = sec < 60 ? sec + 's' : Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
        txt += ' · ' + dur;
      }
    }
    var txtEl = t.querySelector('.txt');
    if (txtEl) txtEl.textContent = txt;
  }

  // 未完成工具 → Trae 式进行中文案
  function liveActionOf(tool) {
    var k = procKindOf(tool);
    if (k === 'read') return '正在读取文件';
    if (k === 'search') return '正在搜索文件';
    if (k === 'list') return '正在查看目录';
    if (k === 'edit') return '正在编辑文件';
    if (k === 'cmd') return '正在执行命令';
    if (k === 'browser') return '正在操作浏览器';
    return '正在调用工具';
  }

  // 工具名 → 分类（对齐 Trae：按类别聚合，而非逐个工具罗列）
  var PROC_TOOL_KINDS = {
    read:   ['file_read', 'code_read', 'self_read', 'read_file', 'read_files', 'file_read_many'],
    search: ['file_search', 'code_search', 'grep', 'search_files'],
    list:   ['file_list', 'self_list', 'list_files'],
    edit:   ['file_write', 'apply_patch', 'self_write', 'self_edit', 'file_copy', 'file_move', 'file_delete', 'write_file', 'edit_file', 'ide_edit_file'],
    cmd:    ['shell_exec', 'shell_admin', 'run_python', 'process_start', 'run_command', 'execute_command', 'ide_run_terminal'],
    browser:['screenshot', 'browser', 'browser_use', 'open_url']
  };
  function procKindOf(name) {
    var n = String(name || '').toLowerCase();
    if (!n) return null;
    for (var k in PROC_TOOL_KINDS) {
      if (PROC_TOOL_KINDS[k].indexOf(n) >= 0) return k;
    }
    return 'other';
  }
  function baseName(p) {
    var s = String(p || '').replace(/\\/g, '/');
    var parts = s.split('/');
    return parts[parts.length - 1] || s;
  }
  // 步骤行图标（14px 线性图标，风格贴近 Trae）
  var PROC_ICONS = {
    think:   '<span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.2"/><path d="M12 11v4.5"/><circle cx="12" cy="7.8" r="1" fill="currentColor" stroke="none"/></svg></span>',
    read:    '<span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="12" r="4.4"/><circle cx="15" cy="12" r="4.4"/></svg></span>',
    search:  '<span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="12" r="4.4"/><circle cx="15" cy="12" r="4.4"/></svg></span>',
    list:    '<span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="12" r="4.4"/><circle cx="15" cy="12" r="4.4"/></svg></span>',
    edit:    '<span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13.6 5.4l4.9 4.9M5 19l1-4.2L15.2 5.6a1.7 1.7 0 0 1 2.4 0l.8.8a1.7 1.7 0 0 1 0 2.4L9.2 18z"/></svg></span>',
    cmd:     '<span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M7.5 10l2.6 2.2-2.6 2.2M12.8 14.6h3.8"/></svg></span>',
    browser: '<span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.2" y="5" width="17.6" height="12.2" rx="2"/><path d="M9 20.8h6"/></svg></span>',
    other:   '<span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14.8 6.2a3.8 3.8 0 0 0 5 5l-8.4 8.4a2.2 2.2 0 0 1-3.1-3.1z"/></svg></span>'
  };
  // 工具调用中的行标签（TRAE 式：命令执行中 / 读取中 / 搜索中…）
  function procRunningLabel(tool, a) {
    var kind = procKindOf(tool);
    a = a || {};
    var path = a.path || a.file || a.filename || a.target || a.dst || '';
    if (kind === 'read') return { ico: 'read', label: '读取中', detail: baseName(path) || String(tool) };
    if (kind === 'list') return { ico: 'list', label: '读取目录中', detail: String(a.path || path || '.').replace(/\\/g, '/') };
    if (kind === 'edit') return { ico: 'edit', label: '编辑中', detail: baseName(path) || String(tool) };
    if (kind === 'search') return { ico: 'search', label: '搜索中', detail: String(a.query || a.pattern || '') };
    if (kind === 'cmd') {
      var cmd = String(a.command || a.cmd || a.code || '').split('\n')[0];
      if (cmd.length > 60) cmd = cmd.slice(0, 60) + '…';
      return { ico: 'cmd', label: '命令执行中', detail: cmd };
    }
    if (kind === 'browser') return { ico: 'browser', label: '正在操作浏览器', detail: '' };
    return { ico: 'other', label: '调用中', detail: String(tool) };
  }
  // 单个工具步骤 → Trae 式标题（如「已读取 ide_panel.html」「已编辑 chat.html」）
  function procRowLabel(tool, args, result) {
    var kind = procKindOf(tool);
    var a = args || {};
    var path = a.path || a.file || a.filename || a.target || a.dst || '';
    if (kind === 'read') return { ico: 'read', label: '已读取', detail: baseName(path) || String(tool) };
    if (kind === 'list') return { ico: 'list', label: '已读取目录', detail: String(a.path || path || '.').replace(/\\/g, '/') };
    if (kind === 'edit') return { ico: 'edit', label: '已编辑', detail: baseName(path) || String(tool) };
    if (kind === 'search') {
      // Trae 式：主文案是「已搜索 <条件>」，第二行才是结果（未找到结果 / 找到 N 行）
      var q = String(a.query || a.pattern || a.directory || '');
      var hits = String(result || '').split('\n').filter(function (x) { return x.trim(); }).length;
      if (/未找到|没有找到|No results|not found|0 个/i.test(result || '') || !hits) {
        return { ico: 'search', label: '已搜索', detail: q, sub: '未找到结果' };
      }
      return { ico: 'search', label: '已搜索', detail: q, sub: '找到 ' + hits + ' 行' };
    }
    if (kind === 'cmd') {
      var cmd = a.command || a.cmd || a.code || '';
      cmd = String(cmd).split('\n')[0];
      if (cmd.length > 60) cmd = cmd.slice(0, 60) + '…';
      return { ico: 'cmd', label: '命令已执行', detail: cmd };
    }
    if (kind === 'browser') return { ico: 'browser', label: 'Browser Use Agent 已操作浏览器', detail: '' };
    if (tool === 'auto_verify') {
      var failed = /验证失败/.test(result || '');
      return { ico: 'search', label: failed ? '自动验证未通过' : '自动验证通过',
               detail: String(result || '').replace(/^验证(通过|失败)：/, '').slice(0, 80) };
    }
    return { ico: 'other', label: '已调用', detail: String(tool) };
  }

  // 把服务端 steps（think/tool 交替）渲染为 Trae 式「思考过程」流：
  // 摘要行（思考过程 · 已读取 N 个文件…）+ 相邻同类工具分组（已读取 N 个文件›）+ ① 思考块（全文可展开）
  var THINK_NUMS = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩'];
  function renderProcSteps(steps, host) {
    if (!steps || !steps.length) return null;
    var thinkN = 0, toolN = 0;
    var flow = document.createElement('div');
    flow.className = 'procFlow';

    function makeThinkBlock(text, idx) {
      var st = document.createElement('div');
      st.className = 'procStep think';
      st.innerHTML =
        '<div class="procStepHead"><span class="tno">' + (THINK_NUMS[idx] || (idx + 1)) + '</span>' +
        '<span class="lbl">思考</span><span class="cv">⌄</span></div>' +
        '<div class="procStepBody"><div class="tx">' + esc(text) + '</div></div>';
      st.querySelector('.procStepHead').addEventListener('click', function () {
        st.classList.toggle('open');
      });
      return st;
    }
    function makeSkillBlock(name) {
      var st = document.createElement('div');
      st.className = 'procStep think';
      st.innerHTML =
        '<div class="procStepHead"><span class="lbl">命中技能</span><span class="cv">⌄</span></div>' +
        '<div class="procStepBody"><div class="tx">已调用技能：' + esc(name) + '</div></div>';
      st.querySelector('.procStepHead').addEventListener('click', function () {
        st.classList.toggle('open');
      });
      return st;
    }
    // 工具结果失败判定（shell 非零退出码会在末尾追加 [exit code: N]）
    function stepFailed(r) {
      var t = String(r || '');
      return /^(错误|执行失败|执行错误|读取错误|写入错误|删除错误|移动错误|复制错误|搜索错误|列目录错误|保存产物失败|文件不存在|路径不存在|Error|error|失败)/.test(t) ||
        /\[exit code:\s*-?\d+\]/.test(t) || /(^|\n)\[stderr\]/.test(t);
    }
    var FT_ICO_DIR = '<span class="ftIco"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5h6l2 2.4h9v8.6a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5z"/></svg></span>';
    var FT_ICO_FILE = '<span class="ftIco"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3.5H6.5A1.5 1.5 0 0 0 5 5v14a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 19V8.5z"/><path d="M14 3.5V8.5H19"/></svg></span>';
    function langOfPath(p) {
      var ext = String(p || '').toLowerCase().split('.').pop();
      var MAP = { js: 'js', jsx: 'jsx', mjs: 'js', cjs: 'js', ts: 'ts', tsx: 'tsx', py: 'python', json: 'json',
        html: 'html', htm: 'html', xml: 'xml', svg: 'svg', css: 'css', scss: 'scss', less: 'less',
        md: 'markdown', sh: 'bash', ps1: 'powershell', bat: 'bat', cmd: 'bat', yml: 'yaml', yaml: 'yaml' };
      return MAP[ext] || '';
    }
    // 目录结构 → 文件树块（支持 [DIR]/[FILE] 列表与 ├─ 树状文本两种结果）
    function makeTreeEl(result) {
      var lines = String(result || '').split('\n');
      var rows = [], root = '', listMode = 0;
      for (var i = 0; i < lines.length; i++) {
        var m = lines[i].match(/^\[(DIR|FILE)\]\s+(.*?)(?:\s+\(([^)]*)\))?$/);
        if (m) { listMode++; rows.push({ dir: m[1] === 'DIR', name: m[2], size: m[3] || '', depth: 0 }); }
      }
      if (!listMode) {
        // 树状文本：连接符个数即层级
        lines.forEach(function (raw) {
          if (!raw.trim()) return;
          if (/^目录[:：]/.test(raw.trim())) { root = raw.replace(/^目录[:：]\s*/, '').replace(/（共[\s\S]*$/, '').trim(); return; }
          var prefix = (raw.match(/^[\s│├└─┬┼┐┘|`+-]*/) || [''])[0];
          var name = raw.slice(prefix.length).trim();
          if (!name) return;
          var depth = (prefix.match(/[│├└]/g) || []).length;
          rows.push({ dir: /\/$/.test(name), name: name.replace(/\/$/, ''), size: '', depth: depth });
        });
      } else {
        for (var j = 0; j < lines.length; j++) {
          var dm = lines[j].match(/^目录[:：]\s*(.*)$/);
          if (dm) { root = dm[1].replace(/（共[\s\S]*$/, '').trim(); break; }
        }
      }
      var shown = rows.slice(0, 300);
      var body = shown.map(function (r) {
        return '<div class="ftRow' + (r.dir ? ' dir' : '') + '" style="padding-left:' + (10 + r.depth * 14) + 'px">' +
          (r.dir ? FT_ICO_DIR : FT_ICO_FILE) + '<span class="ftName">' + esc(r.name) + '</span>' +
          (r.size ? '<span class="ftSize">' + esc(r.size) + '</span>' : '') + '</div>';
      }).join('');
      if (rows.length > 300) body += '<div class="ftNote">…（共 ' + rows.length + ' 项，仅显示前 300 项）</div>';
      var wrap = document.createElement('div');
      wrap.className = 'fileTree';
      wrap.innerHTML = '<div class="ftHead">' + FT_ICO_DIR +
        '<span class="ftName">' + esc(root || '目录') + '</span>' +
        '<span class="ftSize">' + rows.length + ' 项</span></div>' +
        '<div class="ftBody">' + (body || '<div class="ftNote">（空目录）</div>') + '</div>';
      return wrap;
    }
    // 文件内容 → 预览卡片（路径标题 + 行号 + 按扩展名着色）
    function makeFilePrevEl(path, result) {
      var text = String(result || '');
      // 去掉路径纠正提示行
      if (/^\[路径纠正\]/.test(text)) {
        var nl = text.indexOf('\n');
        text = nl >= 0 ? text.slice(nl + 1) : '';
      }
      var truncated = false;
      var lines = text.split('\n');
      if (lines.length > 160) { lines = lines.slice(0, 160); truncated = true; }
      text = lines.join('\n');
      if (text.length > 6000) { text = text.slice(0, 6000); lines = text.split('\n'); truncated = true; }
      var lang = langOfPath(path);
      var lnW = 'calc(' + String(lines.length).length + 'ch + 16px)';
      var body = '';
      for (var i = 0; i < lines.length; i++) {
        body += '<span class="cl"><span class="ln" style="width:' + lnW + '">' + (i + 1) + '</span>' +
          '<span class="lc">' + hlLine(lines[i], lang) + '</span></span>';
      }
      var wrap = document.createElement('div');
      wrap.className = 'filePrev';
      wrap.innerHTML = '<div class="fpHead">' + FT_ICO_FILE +
        '<span class="fpPath" title="' + esc(path) + '">' + esc(path || '文件') + '</span>' +
        '<span class="fpMeta">' + lines.length + ' 行' + (lang ? ' · ' + esc(lang) : '') + '</span></div>' +
        '<div class="codeBody">' + body + '</div>' +
        (truncated ? '<div class="fpNote">…（预览已截断，完整内容请在编辑器中查看）</div>' : '');
      return wrap;
    }
    function makeDetailEl(s) {
      var toolArgs = s.args || {};
      var argsStr = '';
      try { argsStr = JSON.stringify(toolArgs, null, 2); } catch (e) { argsStr = String(toolArgs); }
      var result = String(s.result || '');
      var isFail = stepFailed(result);
      var kind = procKindOf(s.tool);
      var d = document.createElement('div');
      d.className = 'grpDetail';
      // 命令类工具：用终端样式展示命令 + 输出，和普通代码块区分
      if (kind === 'cmd') {
        var cmd = String(toolArgs.command || toolArgs.cmd || toolArgs.code || toolArgs.script || '').trim();
        var shown = result.length > 1500 ? result.slice(0, 1500) + '\n…（已截断，原始 ' + result.length + ' 字符）' : result;
        d.innerHTML =
          '<div class="term ' + (isFail ? 'fail' : 'ok') + '">' +
          '<div class="codeHead"><span class="termTag">终端</span><span class="spacer"></span>' +
          '<span class="termState' + (isFail ? ' fail' : '') + '">' + (isFail ? '失败' : '成功') + '</span></div>' +
          '<div class="termBody">' +
          (cmd ? '<div class="termCmd"><span class="pr">$</span>' + esc(cmd) + '</div>' : '') +
          '<div class="termLine">' + esc(shown) + '</div>' +
          '</div></div>';
        return d;
      }
      // 目录结构 → 文件树；文件内容 → 预览卡片（失败结果仍走原始文本）
      if (!isFail && kind === 'list' && result.trim()) return makeTreeEl(result);
      if (!isFail && kind === 'read' && result.trim()) {
        return makeFilePrevEl(String(toolArgs.path || toolArgs.file || toolArgs.filename || toolArgs.target || ''), result);
      }
      d.innerHTML =
        '<pre>' + esc(argsStr) + '</pre>' +
        '<pre class="' + (isFail ? 'resFail' : 'resOk') + '">' +
        esc(result.length > 1500 ? result.slice(0, 1500) + '\n…（已截断，原始 ' + result.length + ' 字符）' : result) +
        '</pre>';
      return d;
    }
    // 单个工具 → 一行（对齐 Trae：命令/读取/搜索/编辑逐条列出，点行展开参数与结果）
    function makeToolRow(s) {
      toolN++;
      var result = String(s.result || '');
      var isFail = stepFailed(result);
      var pl = procRowLabel(s.tool, s.args || {}, result);
      var row = document.createElement('div');
      row.className = 'procStep tool' + (isFail ? ' fail' : '');
      row.innerHTML =
        '<div class="procStepHead">' + PROC_ICONS[pl.ico] +
        '<span class="lbl">' + esc(pl.label) + '</span>' +
        (pl.detail ? '<span class="cmd">' + esc(pl.detail) + '</span>' : '') +
        '<span class="cv">⌄</span></div>' +
        (pl.sub ? '<div class="procSub">' + esc(pl.sub) + '</div>' : '') +
        '<div class="procStepBody"></div>';
      row.querySelector('.procStepBody').appendChild(makeDetailEl(s));
      row.querySelector('.procStepHead').addEventListener('click', function () {
        row.classList.toggle('open');
      });
      if (isFail) row.classList.add('open');
      return row;
    }

    var thinkIdx = 0;
    steps.forEach(function (s) {
      if (s.think) {
        thinkN++;
        // 预分析的真实推理：渲染成与流式一致的「思考过程」块
        flow.appendChild(s.stage === 'analysis' ? makeStageThinkBlock(s.think)
                                                : makeThinkBlock(s.think, thinkIdx - 1));
      } else if (s.skill) {
        flow.appendChild(makeSkillBlock(String(s.skill)));
      } else if (s.tool) {
        flow.appendChild(makeToolRow(s));
      }
    });
    if (!thinkN && !toolN) return null;

    if (host) {
      host.classList.add('fold');
      host.appendChild(flow);
      // 摘要行：Trae 式「已编辑 N 个文件，读取 N 个文件…」，点击折叠/展开整个流
      var parts = tallyParts(steps);
      var tal = document.createElement('div');
      tal.className = 'procTally';
      tal.innerHTML = '<span class="txt">' + esc(parts.length ? parts.join('，') : '思考过程') +
        '</span><span class="cv">›</span>';
      tal.addEventListener('click', function () { host.classList.toggle('fold'); });
      host.insertBefore(tal, flow);
    }
    return { thinkN: thinkN, toolN: toolN };
  }

  function renderPorts(rows) {
    if (!rows.length) {
      portTb.innerHTML = '<tr><td colspan="4" style="color:#8b8f9a">没有可探测的端口</td></tr>';
      return;
    }
    portTb.innerHTML = rows.map(function (r) {
      return '<tr class="row" data-port="' + r.port + '">' +
        '<td>' + r.port + '</td>' +
        '<td>' + esc(r.label || '') + '</td>' +
        '<td class="' + (r.online ? 'auto' : 'off') + '">' + (r.online ? '自动转发' : '—') + '</td>' +
        '<td class="' + (r.online ? 'on' : 'off') + '">' + (r.online ? '● 运行中' : '○ 未启动') + '</td>' +
        '</tr>';
    }).join('');
  }

  function addChanges(files) {
    if (pendingChangesEl && pendingChangesEl.parentNode) {
      pendingChangesEl.parentNode.removeChild(pendingChangesEl);
      pendingChangesEl = null;
    }
    if (!files || !files.length) return;
    var add = 0, del = 0;
    files.forEach(function (f) { add += f.add || 0; del += f.del || 0; });
    var card = document.createElement('div');
    card.className = 'changes';
    var head = document.createElement('div');
    head.className = 'changesHead';
    head.innerHTML =
      '<span class="docIco"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3.5H7.2A1.7 1.7 0 0 0 5.5 5.2v13.6a1.7 1.7 0 0 0 1.7 1.7h9.6a1.7 1.7 0 0 0 1.7-1.7V8z"/><path d="M14 3.5V8h5.5M8.8 13h6.4M8.8 16.4h6.4"/></svg></span>' +
      '<span class="ct">' + files.length + ' 个文件已更改</span><span class="cv">⌃</span>' +
      '<span class="spacer"></span>' +
      '<span class="plus">+' + add + '</span> <span class="minus">-' + del + '</span>' +
      '<span class="vsep"></span>' +
      '<button class="expand" title="在源代码管理中查看"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 5h5v5M19 5l-7.5 7.5"/><path d="M18 14.5v3.8a1.7 1.7 0 0 1-1.7 1.7H5.7A1.7 1.7 0 0 1 4 18.3V7.7A1.7 1.7 0 0 1 5.7 6h3.8"/></svg></button>';
    card.appendChild(head);
    var bodyEl = document.createElement('div');
    bodyEl.className = 'changesBody';
    files.forEach(function (f) {
      var name = String(f.path).split(/[\\/]/).pop();
      var dir = String(f.path).replace(/[\\/][^\\/]+$/, '');
      var row = document.createElement('div');
      row.className = 'fileRow';
      row.innerHTML = '<span class="fp" title="' + esc(f.path) + '">' + esc(name) +
        ' <span class="st">' + esc(dir) + '</span></span>' +
        '<span class="plus">+' + (f.add || 0) + '</span>' +
        '<span class="minus">-' + (f.del || 0) + '</span>' +
        '<button class="fo" data-open="' + esc(f.path) + '">查看</button>' +
        '<button class="fo" data-accept="' + esc(f.path) + '">接受</button>' +
        '<button class="fo" data-reject="' + esc(f.path) + '">拒绝</button>';
      bodyEl.appendChild(row);
    });
    var foot = document.createElement('div');
    foot.className = 'changesFoot';
    foot.innerHTML = '<button class="acc" data-all="accept">全部接受</button>' +
      '<button data-all="reject">拒绝全部</button>';
    bodyEl.appendChild(foot);
    card.appendChild(bodyEl);
    head.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('.expand')) {
        if (!needIde()) post({ type: 'openScm' });
        return;
      }
      card.classList.toggle('open');
      var cv = head.querySelector('.cv');
      if (cv) cv.textContent = card.classList.contains('open') ? '⌄' : '⌃';
    });
    card.addEventListener('click', function (e) {
      var b = e.target;
      if (!b || b.tagName !== 'BUTTON') return;
      if (needIde()) return;   // 改动确认/打开 diff 依赖扩展
      var p = b.getAttribute('data-open');
      var a = b.getAttribute('data-accept');
      var r = b.getAttribute('data-reject');
      var all = b.getAttribute('data-all');
      if (p) post({ type: 'openDiff', path: p });
      else if (a) post({ type: 'acceptChanges', paths: [a] });
      else if (r) post({ type: 'rejectChanges', paths: [r] });
      else if (all === 'accept') post({ type: 'acceptChanges', paths: files.map(function (f) { return f.path; }) });
      else if (all === 'reject') post({ type: 'rejectChanges', paths: files.map(function (f) { return f.path; }) });
    });
    chatEl.appendChild(card);
    pendingChangesEl = card;
    scrollBottom();
  }

  /* ---------- 已附加上下文 chips ---------- */
  function renderPending(value) {
    refChip.innerHTML = '';
    var items = (value && value.items) ? value.items
      : (value && value.name ? [{ name: value.name }] : []);
    // 拖入/附加的实体文件按 Trae 形态放进输入框；普通引用仍留在原上下文条
    var attachedItems = items.filter(function (it) { return it && it.attached && !it.dir && it.path; });
    pendingContextFiles = attachedItems.map(function (it) {
      return {
        kind: it.kind || attachKindOf({ name: it.name || '', type: '' }) || 'text',
        name: it.name || baseName(it.path || ''), size: Number(it.bytes || it.size || 0),
        status: 'ready', progress: 100, path: it.path, thumb: it.thumb || '',
        error: '', contextPending: true, truncated: !!it.truncated
      };
    });
    items = items.filter(function (it) { return attachedItems.indexOf(it) < 0; });
    renderAttachBar();
    if (!items.length) { refChip.style.display = 'none'; return; }
    refChip.style.display = 'flex';
    items.forEach(function (it) {
      var chip = document.createElement('span');
      chip.className = 'ctxChip';
      var n = document.createElement('span');
      n.className = 'n';
      n.textContent = (it.dir ? '📁 ' : '📄 ') + (it.name || '');
      n.title = it.path || '';
      chip.appendChild(n);
      if (it.truncated) {
        var cut = document.createElement('span');
        cut.className = 'cut';
        cut.textContent = '已截断';
        chip.appendChild(cut);
      }
      if (!it.dir) {
        var opt = document.createElement('span');
        opt.className = 'optAct';
        opt.textContent = '优化';
        opt.title = 'AI 优化此文件（润色/结构/格式/性能）';
        opt.addEventListener('click', function (ev) {
          ev.stopPropagation();
          var MODES = [
            ['polish', '内容润色', '修正错别字与语病，让表达更自然流畅，保持原意与篇幅基本不变'],
            ['structure', '逻辑结构', '梳理内容逻辑，调整段落/章节顺序，补全缺失的过渡，使结构更清晰'],
            ['format', '格式规范', '统一缩进/命名/标点/排版风格，符合该文件类型的官方格式规范'],
            ['perf', '性能优化', '分析并优化性能瓶颈（算法复杂度、重复计算、资源释放），保持功能行为不变']
          ];
          var html = '<div class="popHead">优化 ' + esc(baseName(it.path || it.name || '')) + '</div>';
          MODES.forEach(function (m) {
            html += '<div class="popIt" data-optmode="' + m[0] + '" data-optpath="' + esc(it.path || '') + '">' +
              svg(IC.spark) + '<span>' + m[1] + '</span></div>';
          });
          showSub(chip, html);
        });
        chip.appendChild(opt);
      }
      var x = document.createElement('button');
      x.textContent = '✕';
      x.title = '移除';
      x.addEventListener('click', function () {
        post({ type: 'removePending', path: it.path });
      });
      chip.appendChild(x);
      refChip.appendChild(chip);
    });
  }

  /* ---------- 校对员 AI ---------- */
  var reviewMap = {};
  function textKey(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return 'r' + h;
  }

  function addReviewCard(text, rv, anchorIdx) {
    clearEmpty();
    var card = document.createElement('div');
    card.className = 'revCard';
    var head = document.createElement('div');
    head.className = 'revHead';
    head.innerHTML = '<span class="rvName">校对员 AI</span><span class="rvCnt">' +
      (rv.issues.length ? '发现 ' + rv.issues.length + ' 处可改进' : '未发现错别字') +
      '</span><span class="spacer"></span>';
    if (rv.issues.length) {
      var btn = document.createElement('button');
      btn.textContent = '采用修正';
      btn.addEventListener('click', function () {
        inputEl.value = rv.corrected || text;
        inputEl.dispatchEvent(new Event('input', { bubbles: true }));
        inputEl.focus();
        toast('已填入修正后的表述');
      });
      head.appendChild(btn);
    }
    var body = document.createElement('div');
    body.className = 'revBody';
    rv.issues.forEach(function (it) {
      var line = document.createElement('div');
      line.className = 'revLine';
      line.innerHTML = '· ' + esc(it.type || '表达') + '：<b>' + esc(it.from || '') +
        '</b> → <b class="to">' + esc(it.to || '') + '</b>' +
        (it.note ? ' <span>' + esc(it.note) + '</span>' : '');
      body.appendChild(line);
    });
    if (rv.intent) {
      var intent = document.createElement('div');
      intent.className = 'revIntent';
      intent.textContent = '理解意图：' + rv.intent;
      body.appendChild(intent);
    }
    card.appendChild(head); card.appendChild(body);
    // 回填到对应那条用户消息下方（模型被占用时校对结果可能晚于回答返回）
    var anchor = null;
    if (anchorIdx) {
      var users = chatEl.querySelectorAll('.msg.user');
      if (users.length >= anchorIdx) anchor = users[anchorIdx - 1];
    }
    if (anchor && anchor.parentNode === chatEl) {
      chatEl.insertBefore(card, anchor.nextSibling);
    } else {
      chatEl.appendChild(card);
    }
    scrollBottom();
    return card;
  }

  // 发送前把这条消息交给独立的校对员 AI 过一遍（异步，不阻塞发送）
  function proofread(text, anchorIdx) {
    // 本地模型逐条串行推理，排在回答之后时可能较久，超时放宽
    api('/proofread', 'POST', { text: text }, 300000).then(function (res) {
      if (!res.ok || !res.data || !res.data.ok) return;
      var d = res.data;
      var rv = { issues: d.issues || [], corrected: d.corrected || text, intent: d.intent || '' };
      if (!rv.issues.length && !rv.intent) return;
      reviewMap[textKey(text)] = rv;
      addReviewCard(text, rv, anchorIdx);
    });
  }

  /* ---------- 中断断点卡片（对齐 Trae：中断处可一键继续） ---------- */
  function addInterruptCard(it) {
    if (!it || !it.text) return;
    if (chatEl.querySelector('[data-interrupt]')) return;   // 已有一张就不重复渲染
    var txt = String(it.text);
    var cut = txt.length > 120 ? txt.slice(0, 120) + '…' : txt;
    var tm = '';
    try {
      var d = new Date(it.ts || Date.now());
      tm = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
    } catch (e) { tm = ''; }
    var wrap = document.createElement('div');
    wrap.className = 'msg ai';
    wrap.setAttribute('data-interrupt', '1');
    wrap.innerHTML =
      '<div style="border-left:3px solid #dfa758;background:rgba(223,167,88,.07);border-radius:6px;padding:9px 11px;margin:2px 0 8px;">' +
        '<div style="display:flex;align-items:center;gap:6px;font-size:12px;color:#e6c79a;font-weight:600;">' +
          '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="#dfa758" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 2.6 17.4A1.7 1.7 0 0 0 4.1 20h15.8a1.7 1.7 0 0 0 1.5-2.6L13.7 3.9a1.7 1.7 0 0 0-3.4 0z"/><path d="M12 9v4.2"/><path d="M12 16.4h.01"/></svg>' +
          '上次任务中断在这里' +
          (tm ? '<span style="margin-left:auto;font-weight:400;font-size:10.5px;color:#8b8d95;">' + tm + '</span>' : '') +
        '</div>' +
        '<div style="font-size:12px;color:#b9bdc7;margin:6px 0 9px;white-space:pre-wrap;word-break:break-word;">' + esc(cut) + '</div>' +
        '<div style="display:flex;gap:6px;">' +
          '<button data-continue="1" style="border:1px solid #2f9e56;background:#2f9e56;color:#fff;border-radius:6px;font-size:11.5px;padding:3px 12px;cursor:pointer;">继续</button>' +
          '<button data-dismiss="1" style="border:1px solid #2e2f36;background:transparent;color:#b9bdc7;border-radius:6px;font-size:11.5px;padding:3px 10px;cursor:pointer;">忽略</button>' +
        '</div>' +
      '</div>';
    wrap.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('button') : null;
      if (!btn) return;
      if (btn.getAttribute('data-continue')) {
        wrap.remove();
        // 断档续算：直接重跑上一轮请求，不再插入一条“继续上次未完成的任务”用户消息
        // （Trae 行为：续跑不刷屏，历史里也不会层层叠加）
        post({ type: 'retryLast' });
      } else if (btn.getAttribute('data-dismiss')) {
        wrap.remove();
        post({ type: 'dismissInterrupted' });
      }
    });
    chatEl.appendChild(wrap);
    scrollBottom();
  }

  /* ===== 历史消息分段渲染：只渲染最近 HISTORY_WINDOW 条，上滑增量补更早的 ===== */
  var HISTORY_WINDOW = 40;   // 首屏渲染条数
  var HISTORY_PAGE = 30;     // 每次上滑补充条数
  var historyMsgs = null, historyFrom = 0, loadingOlder = false;

  function renderOneMessage(m, idx) {
    if (!m) return;
    if (m.role === 'user') {
      // 服务端会话里含「工具执行结果」的内部消息，恢复展示时跳过，避免刷屏
      if (String(m.content || '').indexOf('工具执行结果：') === 0) return;
      // 旧版续跑会插入「继续上次未完成的任务：…」用户消息，历史回放时隐藏，避免成片刷屏
      if (/^继续上次未完成的任务[：:]/.test(String(m.display || m.content || ''))) return;
      addUser(m.display || m.content, m.ref);
      var uNode = chatEl.lastElementChild;
      if (uNode && typeof idx === 'number') uNode.__mi = idx;   // 供折叠条按消息下标隐藏原文
      var rv = reviewMap[textKey(m.display || m.content || '')];
      if (rv) addReviewCard(m.display || '', rv);
    } else {
      // 历史消息：带上持久化的过程步骤与耗时，重启后仍能看到「思考→执行」明细
      var hw = addAssistant(m.content, m.ms, m.steps);
      if (hw && typeof idx === 'number') hw.__mi = idx;
      // 计划卡随消息持久化：重启/切换会话后仍能看到「分析 → 计划」，位置在思考之后、正文之前
      if (m.planStage) {
        var hcard = addStagePlanCard(m.planStage, hw);
        var hbody = hw.querySelector('.md');
        if (hcard && hbody) hw.insertBefore(hcard, hbody);
      }
    }
  }
  function setHistoryTip(from) {
    var tip = document.getElementById('historyTip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'historyTip';
      tip.className = 'historyTip';
      chatEl.insertBefore(tip, chatEl.firstChild);
    }
    tip.textContent = from > 0 ? ('上滑加载更早消息（还有 ' + from + ' 条）') : '已加载全部消息';
  }
  function loadOlderHistory() {
    if (loadingOlder || !historyMsgs || historyFrom <= 0) return;
    loadingOlder = true;
    var tip = document.getElementById('historyTip');
    if (tip) tip.textContent = '正在加载更早消息…';
    var prevH = chatEl.scrollHeight, prevTop = chatEl.scrollTop;
    var from = Math.max(0, historyFrom - HISTORY_PAGE);
    quietRender = true;
    // marker 放在「更早消息」应插入的位置：顶部提示之后、当前首条消息之前
    var marker = document.createElement('div');
    marker.style.display = 'none';
    chatEl.insertBefore(marker, tip ? tip.nextSibling : chatEl.firstChild);
    var prevLast = chatEl.lastChild;   // 记下补渲染前最后一个节点，新增节点都排它之后
    var covered = {};
    var st = foldStateBySid[currentSid] || {};
    for (var fi = 0; fi < (st.folds || []).length; fi++) {
      var rg = (st.folds[fi] && st.folds[fi].range) || [];
      for (var ci = rg[0]; ci <= rg[1]; ci++) covered[ci] = true;
    }
    for (var i = from; i < historyFrom; i++) {
      if (!covered[i]) renderOneMessage(historyMsgs[i], i);
    }
    // addUser/addAssistant 只会 append，把 prevLast 之后新增的节点整体移到 marker 前面
    var node = prevLast ? prevLast.nextSibling : chatEl.firstChild;
    while (node) {
      var next = node.nextSibling;
      chatEl.insertBefore(node, marker);
      node = next;
    }
    marker.remove();
    quietRender = false;
    historyFrom = from;
    // 补渲染后保持视口位置不跳动
    chatEl.scrollTop = prevTop + (chatEl.scrollHeight - prevH);
    setHistoryTip(from);
    loadingOlder = false;
  }
  function renderAll(sid, messages, interrupted) {
    currentSid = sid;
    chatEl.innerHTML = '';
    foldBarsById = {};   // 视图重建，折叠条引用同步清空
    historyMsgs = messages || null;
    historyFrom = 0;
    if (!messages || !messages.length) {
      chatEl.innerHTML = '<div class="empty" id="empty"><div class="wTitle">Build With wasily</div><div class="wChips"><button data-p="帮我开发一个应用：">应用开发</button><button data-p="帮我理解这个项目：">项目理解</button><button data-p="帮我构思一个游戏创意：">游戏创意</button><button data-p="帮我写一个工具脚本：">工具脚本</button></div></div>';
      if (interrupted) addInterruptCard(interrupted);
      fetchFoldState(sid);
      return;
    }
    // 折叠条置顶：缓存里的折叠状态先渲染（最早的折叠在上），其余消息按需跳过
    var cached = foldStateBySid[sid];
    var covered = {};
    if (cached && cached.folds && cached.folds.length) {
      applyFoldState(sid, cached);
      for (var fi = 0; fi < cached.folds.length; fi++) {
        var rg = (cached.folds[fi] && cached.folds[fi].range) || [];
        for (var ci = rg[0]; ci <= rg[1]; ci++) covered[ci] = true;
      }
    }
    // 大历史只渲染尾部窗口，上滑时再按页补更早的消息
    var total = messages.length;
    var start = total > HISTORY_WINDOW ? total - HISTORY_WINDOW : 0;
    for (var i = start; i < total; i++) {
      if (!covered[i]) renderOneMessage(messages[i], i);
    }
    historyFrom = start;
    if (start > 0) setHistoryTip(start);
    if (interrupted) addInterruptCard(interrupted);
    scrollBottom(true);   // 会话整体重渲染后定位到底部
    fetchFoldState(sid);  // 异步拉取最新折叠状态（重启后恢复 / 其它窗口新增）
  }

  /* ---------- 生成状态 ---------- */
  var SVG_ARROW = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M6.2 10.8 12 5l5.8 5.8"/></svg>';
  var SVG_SQUARE = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="1.6"/></svg>';
  function setGenerating(on) {
    generating = on;
    // 发送键变形：生成中=方块（可点击中断），空闲=箭头（发送）。不再禁用，也不再显示独立停止按钮
    sendBtn.disabled = false;
    sendBtn.innerHTML = on ? SVG_SQUARE : SVG_ARROW;
    sendBtn.title = on ? '中断生成' : '发送（Enter 发送，Shift+Enter 换行）';
    sendBtn.classList.toggle('stopping', on);
    stopBtn.style.display = 'none';
    if (on) stBarShow('正在思考');
    if (!on) {
      stBarHide();
      // 时间线收尾：思考块折叠归位，未完成的工具行标为完成（正常 done 走 addAssistant 重渲染，此处兜底 stop/异常路径）
      sealThink();
      var flow = document.getElementById('liveFlow');
      if (flow) {
        var pend = flow.querySelectorAll('.procStep.tool.pending');
        for (var i = 0; i < pend.length; i++) {
          pend[i].classList.remove('pending');
          var st = pend[i].querySelector('.st');
          if (st) { st.textContent = '✓ 完成'; st.className = 'st ok'; }
        }
      }
      for (var j = 0; j < liveSteps.length; j++) {
        if (liveSteps[j] && !liveSteps[j].done) liveSteps[j].done = true;
      }
      var elapsed = thinkStartTs ? Date.now() - thinkStartTs : 0;
      updateTally();
      stopThinkClock();
      thinkStartTs = 0;
      // 兜底：本轮若没有最终消息接管（后端报错/空结果），把「正在思考」收成耗时条，别一直挂着
      var pw = document.getElementById('procWrap');
      if (pw) {
        pw.classList.add('fold');
        var pht = pw.querySelector('.procPhrase');
        if (pht) pht.textContent = '';
        var tl = pw.querySelector('.procTally .txt');
        if (tl) tl.textContent = elapsed > 0 ? '任务耗时 ' + fmtMs(elapsed) : '已完成';
      }
      toolSteps = [];
    }
  }

  function applyPhase(phase, tool) {
    if (tool) tlToolStart(tool, null);
    // 阶段变化只更新状态短语，不生成假勾选行（真实步骤由工具事件流逐条上屏）
    var label = '';
    if (phase === 'memory') label = '检索长期记忆…';
    else if (phase === 'decode') label = '正在组织回答并生成输出…';
    else if (phase === 'tool') label = '正在规划下一步…';
    else if (phase === 'think') label = '正在思考…';
    if (!label) return;
    var pw = document.getElementById('procWrap');
    var hasThinking = !!(pw && pw.classList.contains('has-thinking'));
    var ph = document.getElementById('procPhrase');
    if (ph && !hasThinking) ph.textContent = label;
    if (!hasThinking) stBarSet(label);
  }

  /* ---------- 实时工具事件流（TRAE 式：逐次调用上屏，含参数与耗时） ---------- */
  var toolEvTimer = 0, toolEvSince = 0, toolEvGenStart = 0;

  function startToolEvPoll() {
    stopToolEvPoll();
    toolEvGenStart = Date.now() / 1000 - 2;
    toolEvPoll();
  }
  function stopToolEvPoll() {
    if (toolEvTimer) { clearTimeout(toolEvTimer); toolEvTimer = 0; }
  }
  function toolEvPoll() {
    api('/tool_events?since=' + toolEvSince, 'GET', null, 6000).then(function (r) {
      if (r && r.ok && r.data && r.data.events) {
        r.data.events.forEach(function (ev) {
          if ((ev.id || 0) > toolEvSince) toolEvSince = ev.id;
          var sid = ev.sid || '';
          // 接收当前会话事件（IDE 会话 id 现为 ide:<面板会话>）+ plan 执行事件（plan_ 前缀）
          var isNormalSession = (sid === currentSid || sid === ('ide:' + currentSid) ||
            sid === 'vscodium' || sid === 'vscodium-solo');
          var isPlanEvent = sid.indexOf('plan_') === 0;
          if ((isNormalSession || isPlanEvent) && (ev.ts || 0) >= toolEvGenStart) {
            liveToolEvent(ev);
          }
        });
      }
      if (generating) toolEvTimer = setTimeout(toolEvPoll, 800);
    });
  }

  function liveToolEvent(ev) {
    // 历史对话压缩：画分隔线 + 折叠统计行（Trae 式）
    if (ev.type === 'history_compress') {
      tlCompress(ev);
      return;
    }
    // plan 级事件：plan_start / plan_step / plan_end
    if (ev.type === 'plan_start') {
      // plan 开始 → 确保 procWrap 存在（如果用户直接点确认，可能没有 think 事件先触发）
      if (!document.getElementById('procWrap')) {
        addThinking();
      }
      thinkStep('🧠 开发计划启动：' + (ev.goal || ''), false);
      return;
    }
    if (ev.type === 'plan_step') {
      // 步骤状态变更 → 更新 orch-card（如果还在）+ procWrap 追加提示行
      updateOrchStep(ev.step, ev.state);
      var labelMap = { running: '执行中', done: '已完成', failed: '失败' };
      var colorMap = { running: '#dfa758', done: '#59d98a', failed: '#ee6b81' };
      thinkStep('　↳ 步骤 ' + ev.step + '：' + (ev.title || '') + ' → ' +
        '<span style="color:' + colorMap[ev.state] + '">' + (labelMap[ev.state] || ev.state) + '</span>', false);
      return;
    }
    if (ev.type === 'plan_end') {
      thinkStep('✅ 开发计划 ' + ev.status + ' — 全部步骤执行完毕', false);
      return;
    }
    if (ev.type === 'think') {
      if (ev.text) thinkStep(ev.text.length > 160 ? ev.text.slice(0, 160) + '…' : ev.text, false);
      return;
    }
    if (ev.type === 'tool_call') {
      var a = {};
      try { a = JSON.parse(ev.args || '{}'); } catch (e) { a = {}; }
      tlToolStart(ev.tool, a, ev.confirm);
    } else if (ev.type === 'tool_result') {
      tlToolDone(ev.result, ev.ok !== false, ev.dur);
    } else if (ev.type === 'subagent_start') {
      // 子智能体在独立上下文里跑，主对话只留一行摘要
      thinkStep('🤖 子智能体 ' + (ev.agent || '') + ' 启动' +
        (ev.task ? '：' + String(ev.task).slice(0, 60) + '…' : '') + '（独立上下文）', false);
    } else if (ev.type === 'subagent_end') {
      thinkStep('🤖 子智能体 ' + (ev.agent || '') + (ev.ok === false ? ' 未完成' : ' 完成') +
        (ev.tools != null ? ' · ' + ev.tools + ' 个工具' : '') +
        (ev.dur != null ? ' · ' + ev.dur + 's' : '') +
        (ev.error ? ' · ' + String(ev.error).slice(0, 60) : ''), false);
    } else if (ev.type === 'verify') {
      tlVerify(ev);
    } else if (ev.type === 'tool_install') {
      if (ev.state === 'start') {
        thinkStep('　📦 准备依赖：' + (ev.pkg || '') + '…', false);
      } else if (ev.state === 'done') {
        thinkStep('　📦 已安装：' + (ev.pkg || ''), false);
      } else if (ev.state === 'failed') {
        thinkStep('　⚠️ 依赖安装失败：' + (ev.pkg || ''), false);
      }
    }
  }

  // 通过 data-plan-id 属性定位当前活跃的 orch-card（可能有多个历史 card）
  var _activePlanId = null;
  function setActivePlan(id) { _activePlanId = id; }
  function updateOrchStep(stepId, state) {
    // 找到当前活跃的 orch-card
    var card = null;
    if (_activePlanId) {
      card = document.querySelector('.orch-card[data-plan-id="' + _activePlanId + '"]');
    }
    if (!card) {
      // 找不到就取页面上最后一个 orch-card（大概率就是当前的）
      var cards = document.querySelectorAll('.orch-card');
      if (cards && cards.length) card = cards[cards.length - 1];
    }
    if (!card) return;
    // 找到对应的步骤行：oc-step 里第一个 .oi 子元素的文本是序号
    var steps = card.querySelectorAll('.oc-step');
    for (var i = 0; i < steps.length; i++) {
      var oi = steps[i].querySelector('.oi');
      if (!oi) continue;
      // oi 的文本格式是 emoji，前面有 s.id. 编号，用 .ot 里的数字匹配
      var ot = steps[i].querySelector('.ot');
      if (!ot) continue;
      var m = ot.textContent.match(/^(\d+)\./);
      if (m && Number(m[1]) === Number(stepId)) {
        // 移除旧状态 class，添加新状态
        steps[i].classList.remove('pending', 'running', 'done', 'failed');
        steps[i].classList.add(state || 'pending');
        break;
      }
    }
  }

  // 自动验证行：task_done 前服务端校验本轮产物（开始→完成两段更新同一行）
  function tlVerify(ev) { queueOrRun(function () { _tlVerify(ev); }); }
  function _tlVerify(ev) {
    var flow = document.getElementById('liveFlow');
    if (!flow) return;
    sealThink();
    var row = flow.querySelector('.procStep.tool.verify.pending');
    if (ev.status === 'start') {
      if (row) return;                       // 已有进行中的验证行
      row = document.createElement('div');
      row.className = 'procStep tool verify pending';
      row.dataset.tool = 'auto_verify';
      row.innerHTML =
        '<div class="procStepHead">' + (PROC_ICONS.search || PROC_ICONS.other) +
        '<span class="lbl">自动验证</span>' +
        '<span class="cmd">校验本轮产物…</span>' +
        '<span class="st run">验证中…</span></div>';
      flow.appendChild(row);
      liveSteps.push({ tool: 'auto_verify', args: {}, result: '', done: false });
      updateTally();
      scrollBottom();
      return;
    }
    // status === 'done'
    if (!row) {                              // 没收到 start（中途打开面板）：补一行已完成态
      row = document.createElement('div');
      row.className = 'procStep tool verify';
      row.dataset.tool = 'auto_verify';
      row.innerHTML =
        '<div class="procStepHead">' + (PROC_ICONS.search || PROC_ICONS.other) +
        '<span class="lbl">自动验证</span><span class="cmd"></span><span class="st"></span></div>';
      flow.appendChild(row);
      liveSteps.push({ tool: 'auto_verify', args: {}, result: '', done: false });
    }
    row.classList.remove('pending');
    var st = row.querySelector('.st');
    var ok = ev.ok !== false;
    if (st) {
      st.textContent = (ok ? '✓ ' : '✗ ') + (ev.dur != null ? ev.dur + 's' : '完成');
      st.className = 'st ' + (ok ? 'ok' : 'err');
    }
    var cmd = row.querySelector('.cmd');
    if (cmd && ev.text) cmd.textContent = String(ev.text).slice(0, 90);
    updateTally();
    scrollBottom();
  }

  /* ---------- Ask / Plan / Agent 三模式 ---------- */
  var MODE_LABEL = { ask: 'Ask', plan: 'Plan', solo: 'Agent' };
  var MODE_TOAST = {
    ask: '已切到 Ask · 问答模式：只读分析、答疑，不修改文件',
    plan: '已切到 Plan · 规划模式：先出执行计划，确认或切 Agent 后才动手',
    solo: '已切到 Agent · 执行模式：自主规划、多轮工具执行、可修改文件'
  };
  function applyMode(m, quiet) {
    m = (m === 'solo' || m === 'agent') ? 'solo' : (m === 'plan' ? 'plan' : 'ask');
    curMode = m;
    modeSel.value = (m === 'solo') ? 'solo' : 'ide';
    // 内置智能体胶囊跟随模式；自定义智能体保留自己的名字
    if (curAgent === 'chat' || curAgent === 'agent') {
      curAgent = (m === 'solo') ? 'agent' : 'chat';
      curAgentPrompt = '';
      agentLabel.textContent = MODE_LABEL[m];
    }
    if (!quiet) toast(MODE_TOAST[m]);
  }
  function setMode(m) {
    applyMode(m);
    post({ type: 'setMode', value: curMode });
  }

  /* ---------- 会话列表 ---------- */
  function renderSessList(sessions, activeId) {
    var w = activeWin();
    var hl = (w && w.sid) ? w.sid : activeId;
    currentSid = hl;
    // 用会话名刷新窗口标签标题（会话首条消息决定名字）
    (sessions || []).forEach(function (s) {
      var ww = winBySid(s.id);
      if (ww) ww.title = s.name || ww.title;
    });
    renderTabs();
    sessList.innerHTML = (sessions || []).map(function (s) {
      return '<div class="sessRow' + (s.id === hl ? ' on' : '') + '" data-sid="' + s.id + '">' +
        '<span class="sn">' + esc(s.name) + '</span>' +
        '<button class="del" data-del="' + s.id + '" title="删除">✕</button></div>';
    }).join('') || '<div class="empty" style="padding:12px">暂无历史会话</div>';
    document.getElementById('sessCount').textContent = (sessions || []).length + ' 条';
    // 同步填充侧边栏会话列表
    var sbListEl = document.getElementById('sbList');
    if (sbListEl) {
      sbListEl.innerHTML = (sessions || []).map(function (s) {
        return '<div class="sessRow' + (s.id === hl ? ' on' : '') + '" data-sid="' + s.id + '">' +
          '<span class="sn">' + esc(s.name) + '</span>' +
          '<button class="del" data-del="' + s.id + '" title="删除">✕</button></div>';
      }).join('') || '<div class="empty">暂无历史会话</div>';
    }
    var sbCountEl = document.getElementById('sbCount');
    if (sbCountEl) sbCountEl.textContent = (sessions || []).length + ' 条';
  }
  sessList.addEventListener('click', function (e) {
    var del = e.target.getAttribute && e.target.getAttribute('data-del');
    if (del) {
      e.stopPropagation();
      // 删除一个被窗口占用的会话，同时关闭对应窗口（否则窗口会指向已删除的会话）
      var w = winBySid(del);
      if (w) { closeWindow(w.windowId); return; }
      post({ type: 'deleteSession', id: del });
      return;
    }
    var row = e.target.closest ? e.target.closest('.sessRow') : null;
    if (!row) return;
    var sid = row.getAttribute('data-sid');
    var aw = activeWin();
    if (aw) aw.sid = sid;   // 当前窗口改绑到选中的历史会话
    currentSid = sid;
    renderTabs();
    post({ type: 'switchSession', id: sid, windowId: activeWindowId });
    sessPanel.classList.remove('show');
  });
  function toggleSessPanel() { sessPanel.classList.toggle('show'); }

  /* ---------- 侧边栏（会话列表）：折叠/展开、拖拽调宽、状态持久化 ---------- */
  var SB_KEY = 'wasily.idePanel.sidebar';
  var sideBar = document.getElementById('sideBar');
  var sideBarResize = document.getElementById('sideBarResize');
  var sbList = document.getElementById('sbList');

  function sbSave() {
    try {
      var collapsed = document.body.classList.contains('sb-collapsed');
      localStorage.setItem(SB_KEY, JSON.stringify({ w: parseInt(sideBar.style.width, 10) || 240, collapsed: collapsed }));
    } catch (e) {}
  }
  function sbRestore() {
    // 默认收起，和 TRAE 一致：面板打开就是整宽的对话区，需要时再点历史图标展开
    document.body.classList.add('sb-collapsed');
    try {
      var raw = localStorage.getItem(SB_KEY);
      if (raw) {
        var st = JSON.parse(raw);
        if (st.w) { sideBar.style.width = Math.max(160, Math.min(420, st.w)) + 'px'; }
      }
    } catch (e) {}
    document.body.style.setProperty('--sb-w', '0px');
  }
  function toggleSidebar() {
    document.body.classList.toggle('sb-collapsed');
    var collapsed = document.body.classList.contains('sb-collapsed');
    // 折叠时把宽度记忆下来，展开时恢复
    if (collapsed) {
      sideBar.dataset.lastW = sideBar.style.width || '240px';
    } else {
      sideBar.style.width = sideBar.dataset.lastW || sideBar.style.width || '240px';
    }
    document.body.style.setProperty('--sb-w', collapsed ? '0px' : (parseInt(sideBar.style.width, 10) || 240) + 'px');
    var hb = document.getElementById('btnHist');
    if (hb) hb.classList.toggle('on', !collapsed);
    sbSave();
  }
  // 拖拽调宽：在主文档监听 mousemove/mouseup，避免指针移到 iframe 上时丢失事件
  var sbDragging = false, sbStartX = 0, sbStartW = 0;
  if (sideBarResize) {
    sideBarResize.addEventListener('mousedown', function (e) {
      if (e.button !== 0) return;
      if (document.body.classList.contains('sb-collapsed')) return;
      e.preventDefault();
      sbDragging = true;
      sbStartX = e.clientX;
      sbStartW = sideBar.getBoundingClientRect().width;
      sideBarResize.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });
  }
  document.addEventListener('mousemove', function (e) {
    if (!sbDragging) return;
    var w = Math.round(Math.max(160, Math.min(420, sbStartW + (e.clientX - sbStartX))));
    sideBar.style.width = w + 'px';
    document.body.style.setProperty('--sb-w', w + 'px');
  });
  document.addEventListener('mouseup', function () {
    if (!sbDragging) return;
    sbDragging = false;
    sideBarResize.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    sbSave();
  });

  // 侧边栏会话点击：与浮层历史列表同逻辑，但不关闭浮层
  if (sbList) {
    sbList.addEventListener('click', function (e) {
      var del = e.target.getAttribute && e.target.getAttribute('data-del');
      if (del) {
        e.stopPropagation();
        var w = winBySid(del);
        if (w) { closeWindow(w.windowId); return; }
        post({ type: 'deleteSession', id: del });
        return;
      }
      var row = e.target.closest ? e.target.closest('.sessRow') : null;
      if (!row) return;
      var sid = row.getAttribute('data-sid');
      var aw = activeWin();
      if (aw) aw.sid = sid;
      currentSid = sid;
      renderTabs();
      post({ type: 'switchSession', id: sid, windowId: activeWindowId });
    });
  }
  var sbNewBtn = document.getElementById('sbNew');
  if (sbNewBtn) sbNewBtn.addEventListener('click', function () { newWindow(); });

  // 初始化：还原状态 + 绑定顶栏历史按钮切换侧边栏
  sbRestore();
  renderTabs();
  (function () {
    var hb = document.getElementById('btnHist');
    if (hb) hb.classList.toggle('on', !document.body.classList.contains('sb-collapsed'));
  })();

  /* ---------- 内嵌页面（Agent 中心 / 多 AI 圆桌） ---------- */
  var frameView = document.getElementById('frameView');
  var frameFrame = document.getElementById('frameFrame');
  var frameTitle = document.getElementById('frameTitle');
  var frameUrl = '';
  function openFrame(url, title) {
    frameTitle.textContent = title || '';
    frameUrl = url;
    if (frameFrame.dataset.src !== url) {
      frameFrame.src = url;
      frameFrame.dataset.src = url;
    }
    frameView.classList.add('show');
  }
  document.getElementById('frameOpen').addEventListener('click', function () {
    if (frameUrl) post({ type: 'openExternal', url: frameUrl });
  });
  document.getElementById('frameClose').addEventListener('click', function () {
    frameView.classList.remove('show');
  });

  /* ---------- Slash Commands (/xxx) + @ 文件引用 ---------- */
  var SLASH_CMDS = [
    { cmd: '/explain', desc: '解释选中的代码',  inject: '/explain ' },
    { cmd: '/inline-edit', desc: '改这段代码',  inject: '/inline-edit ' },
    { cmd: '/fix',     desc: '修复选中代码的 Bug', inject: '/fix ' },
    { cmd: '/comment', desc: '加注释',     inject: '/comment ' },
    { cmd: '/test',    desc: '生成测试',   inject: '/test ' },
    { cmd: '/refactor',desc: '重构代码',   inject: '/refactor ' },
    { cmd: '/refactor-global', desc: '跨文件全局重构', inject: '/refactor-global ' },
    { cmd: '/git-review', desc: '审查 git 变更',  inject: '/git-review ' },
    { cmd: '/generate',desc: '生成新代码', inject: '/generate ' },
    { cmd: '/run',     desc: '运行命令',   inject: '/run ' },
    { cmd: '/compact', desc: '压缩当前上下文', inject: '/compact' }
  ];
  var pickerEl = document.getElementById('picker');
  var pickerMode = null;    // 'slash' | 'at' | 'hash' | 'hashfile' | 'hashfolder' | null
  var pickerActiveIdx = 0;
  var pickerItems = [];
  var filesCache = [];      // @ 文件引用缓存
  var filesCacheAt = 0;
  var filesPending = false;

  /* ---------- # 快捷上下文（对齐 Trae 的六类资源） ---------- */
  var HASH_CATS = [
    { kind: 'code',      key: '#Code',       desc: '当前选中的代码片段' },
    { kind: 'file',      key: '#File',       desc: '完整文件（从工作区选择）' },
    { kind: 'folder',    key: '#Folder',     desc: '文件夹（递归加入）' },
    { kind: 'workspace', key: '#Workspace',  desc: '整个工作区概览' },
    { kind: 'web',       key: '#Web',        desc: '外部文档（后面粘贴 URL）' },
    { kind: 'past',      key: '#Past Chats', desc: '历史对话要点' }
  ];
  var hashCodeSel = null;   // 最近一次从编辑器取到的选区内容

  function pickerShow(items, mode) {
    pickerMode = mode;
    pickerItems = items;
    pickerActiveIdx = 0;
    pickerEl.innerHTML = items.map(function (it, i) {
      var cls = 'pi' + (i === 0 ? ' active' : '') + '" data-i="' + i + '">';
      if (mode === 'slash') {
        return '<div class="' + cls +
          '<span class="pi-ic">' + esc(it.cmd) + '</span>' +
          '<span class="pi-desc">' + esc(it.desc) + '</span></div>';
      }
      if (mode === 'hash') {
        return '<div class="' + cls +
          '<span class="pi-ic" style="color:#8bc98f">#</span>' +
          '<span class="pi-nm">' + esc(it.key) + '</span>' +
          '<span class="pi-desc">' + esc(it.desc) + '</span></div>';
      }
      var parts = (it.path || it.folder || '').split('/');
      var icon = mode === 'hashfolder' ? '📁' : '📄';
      return '<div class="' + cls +
        '<span class="pi-ic" style="color:#8bc98f">' + icon + '</span>' +
        '<span class="pi-nm">' + esc(parts[parts.length - 1] || it.path || it.folder) + '</span>' +
        '<span class="pi-path">' + esc(it.path || it.folder || '') + '</span></div>';
    }).join('');
    pickerEl.style.display = items.length ? 'block' : 'none';
  }
  function pickerHide() { pickerEl.style.display = 'none'; pickerMode = null; }

  // 在 / @ # 触发点插入文本，替换掉还没选完的关键词
  function insertAtCaret(token) {
    var val = inputEl.value;
    var before = val.substring(0, inputEl.selectionStart);
    var after = val.substring(inputEl.selectionEnd);
    var start = Math.max(before.lastIndexOf('/'), before.lastIndexOf('@'), before.lastIndexOf('#'));
    if (start < 0) start = before.length;
    var prefix = before.substring(0, start);
    var inserted = token + (/\s$/.test(token) ? '' : ' ');
    inputEl.value = prefix + inserted + after;
    inputEl.selectionStart = inputEl.selectionEnd = (prefix + inserted).length;
    inputEl.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // 从工作区文件路径反推目录列表（供 #Folder 选择）
  function workspaceFolders() {
    var dirs = {};
    filesCache.forEach(function (f) {
      var p = String(f.path || '').replace(/\\/g, '/');
      var seg = p.split('/');
      for (var i = 1; i < seg.length; i++) dirs[seg.slice(0, i).join('/')] = true;
    });
    return Object.keys(dirs).slice(0, 60).map(function (d) { return { folder: d }; });
  }

  function hashItemFilterAndShow(kw, mode) {
    var k = String(kw || '').toLowerCase();
    if (mode === 'hashfolder') {
      var all = workspaceFolders();
      pickerShow(all.filter(function (x) { return !k || x.folder.toLowerCase().indexOf(k) >= 0; }), 'hashfolder');
    } else {
      pickerShow(filesCache.filter(function (f) {
        return !k || String(f.path || '').toLowerCase().indexOf(k) >= 0;
      }).slice(0, 30), 'hashfile');
    }
  }

  function pickerSelect(idx) {
    var it = pickerItems[idx];
    if (!it) return;
    if (pickerMode === 'slash') {
      insertAtCaret(it.inject || (it.cmd + ' '));
      pickerHide();
      return;
    }
    if (pickerMode === 'at') {
      insertAtCaret('@' + (it.path || ''));
      pickerHide();
      return;
    }
    if (pickerMode === 'hash') {
      if (it.kind === 'file') {
        requestFiles();
        insertAtCaret('#File ');
        hashItemFilterAndShow('', 'hashfile');
        if (!filesCache.length) toast('正在读取工作区文件…');
        return;
      }
      if (it.kind === 'folder') {
        requestFiles();
        insertAtCaret('#Folder ');
        hashItemFilterAndShow('', 'hashfolder');
        return;
      }
      if (it.kind === 'code') {
        // 选区内容要扩展那边提供：发请求 → 'selection' 回传后存入 hashCodeSel
        hashCodeSel = null;
        post({ type: 'getSelection' });
        insertAtCaret('#Code');
        pickerHide();
        toast('已引用当前选中代码');
        return;
      }
      insertAtCaret(it.key);
      pickerHide();
      return;
    }
    if (pickerMode === 'hashfile') {
      insertAtCaret('#File ' + (it.path || ''));
      pickerHide();
      return;
    }
    if (pickerMode === 'hashfolder') {
      insertAtCaret('#Folder ' + (it.folder || ''));
      pickerHide();
    }
  }

  function pickerFilterAndShow(q) {
    if (!q || q === '/') { pickerShow(SLASH_CMDS, 'slash'); return; }
    var kw = q.slice(1).toLowerCase();
    var filtered = SLASH_CMDS.filter(function (c) {
      return c.cmd.toLowerCase().indexOf(kw) >= 0 || c.desc.toLowerCase().indexOf(kw) >= 0;
    });
    pickerShow(filtered, 'slash');
  }

  function atPickerFilterAndShow(q) {
    // 取 @ 后面的关键词
    var kw = (q || '').toLowerCase();
    var filtered = filesCache.filter(function (f) {
      return (!kw) || (f.path || '').toLowerCase().indexOf(kw) >= 0;
    });
    pickerShow(filtered.slice(0, 30), 'at');
  }

  // 从扩展取工作区文件列表（有缓存）
  function requestFiles() {
    if (filesPending) return;
    filesPending = true;
    if (Date.now() - filesCacheAt < 60000 && filesCache.length) { filesPending = false; return; }
    filesCacheAt = Date.now();
    // 先拿当前打开的文件 + 最近会话里的 pending 文件（快速路径）
    post({ type: 'findFiles' });
  }

  inputEl.addEventListener('input', function () {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(140, Math.max(42, inputEl.scrollHeight)) + 'px';
    // 检测 slash / @ picker 触发
    var val = inputEl.value;
    var caret = inputEl.selectionStart;
    var before = val.substring(0, caret);
    // 二级列表：#File / #Folder 后面继续输关键词做过滤
    var hf = before.match(/(?:^|\s)(#File|#Folder)\s([^\s#]*)$/);
    if (hf) {
      var isFolder = hf[1] === '#Folder';
      if (isFolder) requestFiles();
      hashItemFilterAndShow(hf[2], isFolder ? 'hashfolder' : 'hashfile');
      return;
    }
    var m = before.match(/([@\/#][^\s@\/#]*)$/);
    if (m) {
      var prefix = m[0];
      if (prefix.startsWith('/')) {
        pickerFilterAndShow(prefix);
      } else if (prefix.startsWith('@')) {
        requestFiles();
        atPickerFilterAndShow(prefix.slice(1));
      } else {
        // # 快捷上下文：一级菜单（六类资源）
        var kw = prefix.slice(1).toLowerCase();
        pickerShow(HASH_CATS.filter(function (c) {
          return !kw || c.key.toLowerCase().indexOf('#' + kw) >= 0 || c.desc.indexOf(kw) >= 0;
        }), 'hash');
      }
    } else if (pickerMode) {
      pickerHide();
    }
  });
  inputEl.addEventListener('keydown', function (e) {
    if (pickerMode && pickerItems.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); pickerActiveIdx = Math.min(pickerItems.length - 1, pickerActiveIdx + 1); pickerEl.querySelectorAll('.pi').forEach(function (el, i) { el.classList.toggle('active', i === pickerActiveIdx); }); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); pickerActiveIdx = Math.max(0, pickerActiveIdx - 1); pickerEl.querySelectorAll('.pi').forEach(function (el, i) { el.classList.toggle('active', i === pickerActiveIdx); }); }
      else if (e.key === 'Enter') { e.preventDefault(); pickerSelect(pickerActiveIdx); return; }
      else if (e.key === 'Escape') { e.preventDefault(); pickerHide(); return; }
    }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); return; }
    // Enter 发送、Shift+Enter 换行（输入法组词中不拦截）
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      // 设置中心把发送键改成 Ctrl+Enter 时，单独 Enter 只换行
      if (String(window.__wasilySendKey || 'enter') === 'ctrl-enter') return;
      e.preventDefault();
      send();
    }
  });
  pickerEl.addEventListener('click', function (e) {
    var el = e.target.closest('.pi');
    if (!el) return;
    var i = Number(el.getAttribute('data-i'));
    if (isNaN(i)) return;
    pickerSelect(i);
  });
  document.addEventListener('click', function (e) {
    if (pickerEl && !pickerEl.contains(e.target) && e.target !== inputEl) pickerHide();
  });

  /* ---------- 粘贴图片已并入统一附件上传流程（见输入框 paste 监听） ---------- */

  /* ---------- 直连模式发送（无扩展）：本地渲染 + SSE 逐字 + 直连后端 ---------- */
  async function sendDirect(text, imgs, docs, refs) {
    // 附件已由 /v1/ide/attach 落盘：消息里只放路径引用，模型用 read_* 自己读
    var atts = (imgs || []).concat(docs || []);
    var ref = atts.length ? atts.map(function (a) { return a.name; }).join('、') : null;
    var prefix = '';
    if (atts.length) {
      prefix = '用户附件（已落盘到本地，共 ' + atts.length + ' 个）：\n'
        + atts.map(function (a) {
          return '- 附件：' + a.path + '（' + kindLabel(a.kind) + '，' + fmtSize(a.size) + '）';
        }).join('\n')
        + '\n请先调用对应工具读取附件内容再回答，不要猜测内容：\n'
        + '  图片 → read_image(path)；PDF → read_pdf(path)；Word/Excel/PPT → read_office(path)；文本/代码 → file_read(path)。\n\n';
    }
    var prompt = (prefix + text).trim();

    addUser(text, ref);
    generating = true;
    setGenerating(true);
    addThinking();   // 思考块先展开；首个 delta 到达时由 streamPush 自动折叠

    var doneEv = null, stagePlanEv = null;
    // 单条 SSE 事件：data: {...}
    function onSse(line) {
      if (!line || line.indexOf('data:') !== 0) return;
      var payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') return;
      var ev;
      try { ev = JSON.parse(payload); } catch (e) { return; }
      if (!ev) return;
      if (ev.ev === 'thinking') thinkBlockPush(ev.t || '');      // 思考流：独立通道
      else if (ev.ev === 'thinking_done') thinkBlockDone();
      else if (ev.ev === 'stage_plan') { stagePlanEv = ev.plan; addStagePlanCard(ev.plan); }
      else if (ev.ev === 'step_start') updateStagePlanStep(ev.plan_id, ev.step_id, 'running');
      else if (ev.ev === 'step_done') updateStagePlanStep(ev.plan_id, ev.step_id, ev.status || 'done', ev.result || '');
      else if (ev.ev === 'delta') streamPush(ev.t || '');
      else if (ev.ev === 'reset') streamReset();
      else if (ev.ev === 'fold') handleFoldEvent(ev.fold);       // 折叠对话：顶部折叠条
      else if (ev.ev === 'done') doneEv = ev;
      // 其他事件（step/phase 等）忽略
    }

    try {
      var res = await fetch(SERVER + '/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [{ role: 'user', content: prompt }],
          session_id: 'browser-preview',
          window_id: activeWindowId || 'default',
          max_tokens: 2048,
          max_tool_rounds: curMode === 'solo' ? 0 : (curMode === 'plan' ? 3 : 2),
          mode: curMode,
          orchestrate: true,   // Plan/Agent 模式启用「思考流 + 计划预分析」
          refs: Array.isArray(refs) ? refs : [],
          agent: (curAgent && curAgent !== 'chat' && curAgent !== 'agent') ? curAgent : '',
          agent_prompt: curAgentPrompt || '',
          stream: true
        })
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      if (!res.body || !res.body.getReader) throw new Error('当前浏览器不支持流式读取');

      var reader = res.body.getReader();
      var dec = new TextDecoder();
      var buf = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var parts = buf.split('\n\n');
        buf = parts.pop();
        for (var i = 0; i < parts.length; i++) onSse(parts[i].trim());
      }
      if (buf.trim()) onSse(buf.trim());

      streamEnd();
      // 用 done 事件的完整正文 + steps 渲染最终回复（带「已读取 N 个文件…」摘要与文件卡片）
      addAssistant((doneEv && doneEv.content) || '', turnStart ? Date.now() - turnStart : 0,
        (doneEv && doneEv.steps) || []);
      // 兜底：计划卡没在流里到达时，用 done 里的 stage_plan 渲染（已有则按 id 去重）
      if (!stagePlanEv && doneEv && doneEv.stage_plan) addStagePlanCard(doneEv.stage_plan);
      // 直连模式的完成通知（与扩展侧 taskDone 同款窗口内 toast，点击高亮结果）
      showTaskToast({
        state: 'ok', sid: currentSid,
        summary: String((doneEv && doneEv.content) || '').replace(/\s+/g, ' ').slice(0, 60),
        ms: turnStart ? Date.now() - turnStart : 0
      });
    } catch (err) {
      streamEnd();
      addAssistant('生成失败: ' + ((err && err.message) || String(err)), 0, []);
      showTaskToast({ state: 'error', sid: currentSid, summary: String((err && err.message) || '') });
    } finally {
      generating = false;
      setGenerating(false);
      turnStart = 0;
      fetchTokenStats();
    }
  }

  // 把输入框里的 # 标记解析成结构化引用（后端统一展开成参考上下文）
  function parseHashRefs(text) {
    var refs = [];
    if (!text) return refs;
    var m;
    var reFile = /#File\s+([^\s#]+)/g;
    while ((m = reFile.exec(text)) && refs.length < 8) refs.push({ kind: 'file', value: m[1] });
    var reFolder = /#Folder\s+([^\s#]+)/g;
    while ((m = reFolder.exec(text)) && refs.length < 8) refs.push({ kind: 'folder', value: m[1] });
    var reWeb = /#Web\s+(https?:\/\/[^\s#]+)/g;
    while ((m = reWeb.exec(text)) && refs.length < 8) refs.push({ kind: 'web', value: m[1] });
    var mp = text.match(/#Past Chats\s*([^\n#]*)/);
    if (mp && refs.length < 8) refs.push({ kind: 'past', value: (mp[1] || '').trim() });
    if (/#Workspace/.test(text) && refs.length < 8) refs.push({ kind: 'workspace', value: '' });
    if (/#Code/.test(text) && refs.length < 8 && hashCodeSel && hashCodeSel.text) {
      refs.push({ kind: 'code', value: hashCodeSel.file || '', text: hashCodeSel.text });
    }
    return refs;
  }

  function send() {
    var text = inputEl.value.trim();
    if (generating) return;
    // /compact：手动压缩上下文（不发给模型，直接折叠较早对话）
    if (/^\/compact(\s|$)/i.test(text)) {
      inputEl.value = '';
      inputEl.style.height = '42px';
      updateEnhanceBtn();
      if (HAS_API) {
        post({ type: 'compact' });
      } else {
        toast('正在压缩上下文…');
        api('/v1/context/compact', 'POST', { session_id: 'browser-preview' }, 15000).then(function (r) {
          var d = (r && r.ok && r.data) || null;
          if (!d) { toast('压缩失败：服务不可达'); return; }
          toast('上下文已压缩：约 ' + d.before_tokens + ' → ' + d.after_tokens +
            ' tokens（折叠 ' + d.folded + ' 条）');
        });
      }
      return;
    }
    // 续跑后清掉旧的中断卡片，避免残留在对话末尾
    var ics = chatEl.querySelectorAll('[data-interrupt]');
    for (var ii = 0; ii < ics.length; ii++) ics[ii].remove();
    // 附件：必须全部落盘成功才允许发送；失败项跳过并提示
    var allAtts = pendingImgs.concat(pendingDocs);
    var uploading = allAtts.filter(function (a) { return a.status === 'uploading'; });
    if (uploading.length) { toast('还有 ' + uploading.length + ' 个附件正在上传，请稍候…'); return; }
    var ready = allAtts.filter(function (a) { return a.status === 'ready' && a.path; });
    var failedCnt = allAtts.length - ready.length;
    if (failedCnt) toast('已跳过 ' + failedCnt + ' 个上传失败的附件');
    var imgs = ready.filter(function (a) { return a.kind === 'image'; });
    var docs = ready.filter(function (a) { return a.kind !== 'image'; });
    if (!text && !ready.length) return;
    var refs = parseHashRefs(text).concat((inputEl.__refs || []).map(function (r) {
      return { kind: 'file', value: r.path };
    }));
    turnStart = Date.now();
    userAtBottom = true;   // 发送新消息：恢复跟随滚动，回到最新位置
    var msg = {
      type: 'send', text: text, mode: permSel.value,
      agent: curAgent, len: 'medium',
      windowId: activeWindowId || 'default', sessionId: currentSid || ''
    };
    if (curAgentPrompt) msg.agentPrompt = curAgentPrompt;
    if (imgs.length) msg.images = imgs.map(function (a) {
      return { path: a.path, name: a.name, size: a.size, kind: a.kind };
    });
    if (docs.length) msg.docs = docs.map(function (a) {
      return { path: a.path, name: a.name, size: a.size, kind: a.kind };
    });
    if (refs.length) msg.refs = refs;
    post(msg);   // 有扩展：消息由扩展回推渲染（无扩展时 post 为空操作）
    if (!HAS_API) sendDirect(text, imgs, docs, refs);   // 直连模式：本地渲染 + 直接请求后端
    inputEl.value = '';
    inputEl.style.height = '42px';
    pendingImgs = [];
    pendingDocs = [];
    inputEl.__refs = [];
    hashCodeSel = null;
    renderAttachBar();
    updateEnhanceBtn();
    if (text) proofread(text, chatEl.querySelectorAll('.msg.user').length + 1);   // 校对员 AI 异步复核（不阻塞发送）
  }
  // 发送/中断二合一：生成中显示方块，点击中断；空闲显示箭头，点击发送
  sendBtn.addEventListener('click', function () {
    if (generating) {
      post({ type: 'stop', sessionId: currentSid });
      // 直连模式（无扩展）：除了本地 AbortController，还要直连后端设置取消标记
      if (window.__directAbort) { try { window.__directAbort.abort(); } catch (e) {} }
      if (currentSid) {
        fetch(SERVER + '/v1/agent/cancel', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: currentSid })
        }).catch(function () {});
      }
      setGenerating(false);
      return;
    }
    send();
  });

  /* ---------- ⭐ 智能增强 Prompt（对齐 Trae 的 Prompt Enhancer） ---------- */
  var enhanceBusy = false;
  var enhancePrev = null;      // 增强前的原文，供 Ctrl+Z 撤销

  function autoGrowInput() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(140, Math.max(42, inputEl.scrollHeight)) + 'px';
  }

  function updateEnhanceBtn() {
    if (!btnEnhance) return;
    btnEnhance.disabled = (!inputEl.value.trim() || generating || enhanceBusy);
  }

  async function enhancePrompt() {
    if (enhanceBusy || generating) return;
    var text = inputEl.value.trim();
    if (!text) return;
    enhanceBusy = true;
    btnEnhance.classList.add('on');
    updateEnhanceBtn();
    var code = (hashCodeSel && hashCodeSel.text) || '';
    try {
      var r = await fetch(SERVER + '/v1/prompt/enhance', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, code: code, mode: code ? 'code' : 'smart' })
      });
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
      enhancePrev = inputEl.value;
      inputEl.value = '';
      var reader = r.body.getReader();
      var dec = new TextDecoder('utf-8');
      var buf = '';
      var acc = '';
      var errMsg = '';
      while (true) {
        var rd = await reader.read();
        if (rd.done) break;
        buf += dec.decode(rd.value, { stream: true });
        var parts = buf.split('\n\n');
        buf = parts.pop();             // 最后一段可能没写完，留到下一轮
        for (var bi = 0; bi < parts.length; bi++) {
          var line = parts[bi].trim();
          if (line.indexOf('data:') !== 0) continue;
          var ev;
          try { ev = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
          if (ev.error) { errMsg = ev.error; continue; }
          if (ev.delta) {
            acc += ev.delta;
            inputEl.value = acc;
            inputEl.scrollTop = inputEl.scrollHeight;
            autoGrowInput();
          }
          if (ev.done && ev.text) {
            acc = ev.text;
            inputEl.value = acc;
            autoGrowInput();
          }
        }
      }
      if (errMsg) throw new Error(errMsg);
      if (acc) toast('已智能增强（Ctrl+Z 撤销）');
      else toast('增强没有返回内容');
    } catch (e) {
      if (enhancePrev !== null && !inputEl.value.trim()) inputEl.value = enhancePrev;
      toast('增强失败：' + (e && e.message ? e.message : e));
    }
    enhanceBusy = false;
    btnEnhance.classList.remove('on');
    updateEnhanceBtn();
  }

  if (btnEnhance) btnEnhance.addEventListener('click', enhancePrompt);

  document.addEventListener('keydown', function (e) {
    var focused = document.activeElement === inputEl;
    if (focused && (e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'E' || e.key === 'e')) {
      e.preventDefault();
      enhancePrompt();
      return;
    }
    // 增强结果还没改动时，Ctrl+Z 直接还原原文
    if (focused && (e.ctrlKey || e.metaKey) && (e.key === 'z' || e.key === 'Z') && !e.shiftKey && enhancePrev !== null) {
      if (inputEl.value === enhancePrev) return;   // 已经是原文，交给浏览器默认撤销
      e.preventDefault();
      inputEl.value = enhancePrev;
      enhancePrev = null;
      autoGrowInput();
      updateEnhanceBtn();
    }
  });
  inputEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
  });

  /* ---------- 语音转录：按住 Alt+V 说话，松开转文字（设置中心「语音转录快捷键」） ---------- */
  var voice = { enabled: true, busy: false, rec: null, chunks: [], startedAt: 0, cancel: false };
  fetch(SERVER + '/config/get?path=' + encodeURIComponent('voice.shortcut_enabled'), { cache: 'no-store' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { if (d && d.value === false) voice.enabled = false; })
    .catch(function () { });

  function voiceInsert(text) {
    if (!text) return;
    var s = inputEl.selectionStart == null ? inputEl.value.length : inputEl.selectionStart;
    var e2 = inputEl.selectionEnd == null ? s : inputEl.selectionEnd;
    inputEl.value = inputEl.value.slice(0, s) + text + inputEl.value.slice(e2);
    inputEl.selectionStart = inputEl.selectionEnd = s + text.length;
    inputEl.dispatchEvent(new Event('input'));
    inputEl.focus();
  }

  function startVoice() {
    if (!voice.enabled || voice.busy || voice.rec) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
      toast('当前环境不支持麦克风录音'); return;
    }
    voice.busy = true;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      voice.chunks = [];
      var mime = '';
      ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg'].forEach(function (m) {
        if (!mime && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) mime = m;
      });
      var rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      voice.rec = rec;
      rec.ondataavailable = function (ev) { if (ev.data && ev.data.size) voice.chunks.push(ev.data); };
      rec.onstop = function () {
        try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (err) { }
        voice.rec = null;
        var cancelled = voice.cancel; voice.cancel = false;
        if (cancelled || Date.now() - voice.startedAt < 300) { voice.busy = false; return; }
        var blob = new Blob(voice.chunks, { type: 'audio/webm' });
        voice.chunks = [];
        toast('正在识别语音…');
        // 直连后端 /stt（CSP 已放行 127.0.0.1:8000）
        fetch(SERVER + '/stt?fmt=webm', { method: 'POST', body: blob, signal: AbortSignal.timeout(60000) })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d && d.text) { voiceInsert(d.text); toast('已插入语音转写'); }
            else toast('识别失败：' + ((d && d.error) || '无结果'));
          })
          .catch(function () { toast('识别失败：AI 服务未连接'); })
          .then(function () { voice.busy = false; });
      };
      rec.start();
      voice.startedAt = Date.now();
      toast('正在录音…松开 Alt+V 结束');
    }).catch(function (err) {
      voice.busy = false;
      toast('无法访问麦克风：' + ((err && err.name) || err));
    });
  }

  document.addEventListener('keydown', function (e) {
    if (e.altKey && (e.key === 'v' || e.key === 'V')) {
      e.preventDefault();
      if (!e.repeat) startVoice();
    }
  });
  document.addEventListener('keyup', function (e) {
    if ((e.key === 'v' || e.key === 'V' || e.key === 'Alt') && voice.rec) {
      voice.cancel = false;
      try { voice.rec.stop(); } catch (err) { }
    }
  });
  window.addEventListener('blur', function () {
    if (voice.rec) { voice.cancel = false; try { voice.rec.stop(); } catch (err) { } }
  });

  inputEl.addEventListener('input', function () {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(140, Math.max(42, inputEl.scrollHeight)) + 'px';
    updateEnhanceBtn();
  });
  document.getElementById('btnStop').addEventListener('click', function () {
    post({ type: 'stop', sessionId: currentSid });
  });

  /* ---------- 顶栏按钮 ---------- */
  document.getElementById('btnNew').addEventListener('click', function () { newWindow(); });
  document.getElementById('btnNewTab').addEventListener('click', function () { newWindow(); });
  winTabList.addEventListener('click', function (e) {
    var closeId = e.target.getAttribute && e.target.getAttribute('data-close');
    if (closeId) { e.stopPropagation(); closeWindow(closeId); return; }
    var tab = e.target.closest ? e.target.closest('.winTab') : null;
    if (!tab) return;
    activateWindow(tab.getAttribute('data-wid'));
  });
  document.getElementById('btnHist').addEventListener('click', function (e) { e.stopPropagation(); toggleSidebar(); });
  document.getElementById('btnScm').addEventListener('click', function () { if (!needIde()) post({ type: 'openScm' }); });
  document.getElementById('btnPorts').addEventListener('click', function () {
    portView.classList.add('show');
    portTb.innerHTML = '<tr><td colspan="4" style="color:#8b8f9a">探测中…</td></tr>';
    post({ type: 'ports' });
  });
  document.getElementById('portRefresh').addEventListener('click', function () {
    portTb.innerHTML = '<tr><td colspan="4" style="color:#8b8f9a">探测中…</td></tr>';
    post({ type: 'ports' });
  });
  document.getElementById('portClose').addEventListener('click', function () {
    portView.classList.remove('show');
  });
  portTb.addEventListener('click', function (e) {
    var tr = e.target.closest('tr.row');
    if (!tr) return;
    post({ type: 'openPort', port: Number(tr.getAttribute('data-port')) });
  });
  document.getElementById('btnWorkbench').addEventListener('click', async function () {
    var btn = this;
    btn.disabled = true;
    try {
      var res = await fetch(SERVER + '/v1/workbench/bind', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ window_id: activeWindowId || '' })
      });
      var data = await res.json();
      if (!res.ok || !data.ok || !data.workbench_id) {
        throw new Error(data.error || ('HTTP ' + res.status));
      }
      var url = SERVER + '/chat?embed=ide&workbench=1&workbench_id=' +
        encodeURIComponent(data.workbench_id) + '&window_id=' + encodeURIComponent(activeWindowId || '');
      openFrame(url, '多对话工作台 · 独立 / 讨论组');
    } catch (e) {
      toast('无法打开多对话工作台：' + e.message);
    } finally {
      btn.disabled = false;
    }
  });
  document.getElementById('btnAgent').addEventListener('click', function () {
    // 独立 App 窗口打开，不嵌入聊天面板
    post({ type: 'openAgentCenter', tab: '' });
  });
  document.getElementById('btnGroup').addEventListener('click', function () {
    openFrame(SERVER + '/group', '多 AI 圆桌（你的发言最高优先级）');
  });
  document.getElementById('btnSelf').addEventListener('click', function () {
    if (needIde()) return;
    toast('正在自检上一条回答…');
    post({ type: 'selfcheck' });
  });
  document.getElementById('btnClose').addEventListener('click', function () { post({ type: 'closePanel' }); });
  document.getElementById('btnShare').addEventListener('click', function () { exportChat(); });
  document.getElementById('btnAttach').addEventListener('click', function (e) {
    e.stopPropagation();
    openAttachMenu();
  });
  document.getElementById('btnAt').addEventListener('click', function () {
    var v = inputEl.value;
    inputEl.value = v + (v && !/\s$/.test(v) ? ' ' : '') + '@wasily ';
    inputEl.focus();
  });
  // 语音输入：优先 MediaRecorder → /stt（本地识别），失败回退浏览器 Web Speech
  var mediaRec = null, recChunks = [], recording = false;
  var btnMicEl = document.getElementById('btnMic');
  function startSpeechRec(SR) {
    var rec = new SR();
    rec.lang = 'zh-CN';
    rec.interimResults = true;
    rec.onresult = function (e) {
      var t = '';
      for (var i = e.resultIndex; i < e.results.length; i++) t += e.results[i][0].transcript;
      inputEl.value = (inputEl.value + ' ' + t).trim();
      inputEl.dispatchEvent(new Event('input', { bubbles: true }));
    };
    rec.onerror = function () { toast('语音识别失败'); };
    rec.onend = function () { btnMicEl.classList.remove('on'); };
    btnMicEl.classList.add('on');
    rec.start();
  }
  btnMicEl.addEventListener('click', async function () {
    if (recording) {
      recording = false;
      btnMicEl.classList.remove('on');
      try { mediaRec.stop(); } catch (_) {}
      return;
    }
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      if (SR) return startSpeechRec(SR);
      toast('当前环境不支持语音输入');
      return;
    }
    try {
      var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recChunks = [];
      mediaRec = new MediaRecorder(stream);
      mediaRec.ondataavailable = function (e) { if (e.data.size) recChunks.push(e.data); };
      mediaRec.onstop = async function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        try {
          var blob = new Blob(recChunks, { type: 'audio/webm' });
          var res = await fetch(SERVER + '/stt?fmt=webm', { method: 'POST', body: blob });
          var j = await res.json();
          if (j.text && j.text.trim()) {
            inputEl.value = j.text.trim();
            inputEl.dispatchEvent(new Event('input', { bubbles: true }));
            toast('识别完成');
          } else if (SR) {
            toast('本地识别无结果，改用浏览器语音识别');
            startSpeechRec(SR);
          } else {
            toast('没有识别到内容');
          }
        } catch (e) {
          if (SR) {
            toast('本地识别失败，改用浏览器语音识别');
            startSpeechRec(SR);
          } else {
            toast('语音识别失败：' + e.message);
          }
        }
      };
      mediaRec.start();
      recording = true;
      btnMicEl.classList.add('on');
    } catch (e) {
      if (SR) { startSpeechRec(SR); return; }
      toast('无法启动麦克风：' + e.message);
    }
  });
  modeSel.addEventListener('change', function () {
    post({ type: 'setMode', value: modeSel.value });
  });
  document.addEventListener('click', function (e) {
    if (!sessPanel.classList.contains('show')) return;
    if (sessPanel.contains(e.target) || e.target.id === 'btnHist') return;
    sessPanel.classList.remove('show');
  });

  /* ---------- 来自扩展的消息 ---------- */
  window.addEventListener('message', function (ev) {
    var msg = ev.data;
    if (!msg || !msg.type) return;
    switch (msg.type) {
      case 'filesList':
        // @ 文件引用 picker 的工作区文件列表（来自扩展 findFiles）
        filesCache = Array.isArray(msg.files) ? msg.files : [];
        filesPending = false;
        // 如果 picker 当前正开着 at 模式，刷新显示
        if (pickerMode === 'at' && pickerItems && pickerItems.length) {
          atPickerFilterAndShow(pickerItems._kw || '');
        }
        break;
      case 'externalDrag':
        setComposerDragHover(!!msg.active);
        break;
      case 'selection':
        // #Code 引用：扩展回传的当前选区
        hashCodeSel = { text: msg.text || '', file: msg.file || '', language: msg.language || '' };
        break;
      case 'renderMessages': {
        // 扩展推来的会话消息：先定位到绑定该 sid 的窗口，再渲染，避免切窗口时串台
        var w = winBySid(msg.sid);
        if (!w && windows.length) {
          var aw = activeWin();
          if (aw && !aw.sid) { w = aw; aw.sid = msg.sid || aw.sid; renderTabs(); }
        }
        if (!w && !windows.length && msg.sid) {
          w = { windowId: 'w' + Date.now(), sid: msg.sid, title: '窗口 1' };
          windows.push(w);
          activeWindowId = w.windowId;
          renderTabs();
          post({ type: 'registerWindow', windowId: w.windowId, sessionId: w.sid });
        }
        if (w && w.windowId !== activeWindowId) { activeWindowId = w.windowId; renderTabs(); }
        renderAll(msg.sid, msg.messages, msg.interrupted);
        break;
      }
      case 'append':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        streamEnd();   // 流式气泡由最终完整消息接管
        if (msg.role === 'user') addUser(msg.content, msg.ref);
        else {
          var ams = (typeof msg.ms === 'number' && msg.ms > 0)
            ? msg.ms : (turnStart ? Date.now() - turnStart : 0);
          addAssistant(msg.content, ams, msg.steps);
          turnStart = 0;
        }
        break;
      case 'interrupted':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        addInterruptCard(msg.interrupted);
        break;
      case 'delta':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        streamPush(msg.text || '');
        break;
      case 'thinking':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        thinkBlockPush(msg.text || '');
        break;
      case 'thinkingDone':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        thinkBlockDone();
        break;
      case 'planStage':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        // 计划卡收进实时思考区（折叠后整块收起），不再以独立大卡插在消息流里
        addStagePlanCard(msg.plan, document.getElementById('liveFlow') || undefined);
        break;
      case 'stepStart':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        updateStagePlanStep(msg.planId, msg.stepId, 'running');
        break;
      case 'stepDone':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        updateStagePlanStep(msg.planId, msg.stepId, msg.status || 'done', msg.result || '');
        break;
      case 'foldRecord':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        handleFoldEvent(msg.fold || null);
        break;
      case 'foldState':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        applyFoldState(msg.sid, msg);
        break;
      case 'streamReset':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        streamReset();
        break;
      case 'plan':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        addOrchCard(msg.plan);
        break;
      case 'sessions': {
        // 首次收到会话列表：用扩展的 activeId 建第一个窗口标签
        if (!windows.length && msg.activeId) {
          var nw = { windowId: 'w' + Date.now(), sid: msg.activeId, title: '窗口 1' };
          windows.push(nw);
          activeWindowId = nw.windowId;
          currentSid = msg.activeId;
          renderTabs();
          post({ type: 'registerWindow', windowId: nw.windowId, sessionId: nw.sid });
          post({ type: 'setActiveWindow', windowId: nw.windowId });
        } else if (msg.windowId) {
          // 扩展按窗口建会话后的回执：把 sid 绑到对应标签（兼容未预生成 sid 的调用）
          var bw = winById(msg.windowId);
          if (bw && !bw.sid) { bw.sid = msg.activeId; currentSid = bw.sid; renderTabs(); }
        }
        renderSessList(msg.sessions, msg.activeId);
        break;
      }
      case 'mode':
        applyMode(msg.value, true);
        break;
      case 'phase':
        applyPhase(msg.phase, msg.tool);
        break;
      case 'generating':
        setGenerating(!!msg.value);
        if (msg.value) { genStartedAt = Date.now(); addThinking(); startToolEvPoll(); startTokenPoll(true); }
        else { streamEnd(); stopToolEvPoll(); stopTokenPoll(); fetchTokenStats(); }   // 停止/失败/完成：清掉未收尾的流式气泡
        break;
      case 'status':
        // 在线状态统一由 IDE 底部状态栏展示，面板内不再重复提示
        break;
      case 'ask':
        // 外部（IDE 命令 / 桥）送入提示词：等价于用户输入后回车
        if (msg.text && !generating) {
          inputEl.value = String(msg.text);
          inputEl.dispatchEvent(new Event('input'));
          send();
        }
        break;
      case 'pendingContext':
        renderPending(msg.value);
        break;
      case 'changes':
        addChanges(msg.files);
        break;
      case 'confirmAction':
      case 'pendingAction':
        // 后端 needs_confirm：展示敏感操作确认卡，允许/拒绝后 POST /tool/approve
        if (msg.action) addConfirmCard(msg.action);
        break;
      case 'ports':
        renderPorts(msg.rows || []);
        break;
      case 'selfchecking':
        if (msg.value) toast('正在自检上一条回答…');
        break;
      case 'notify':
        toast(msg.message || '');
        break;
      case 'compact_done':
        // 手动 /compact 完成：在对话末尾画压缩分隔线 + 折叠统计
        tlCompress(msg);
        break;
      case 'taskDone':
        // 任务结束（完成 / 失败 / 需要确认）：窗口内 toast，点击回到对应会话并高亮结果
        showTaskToast(msg);
        break;
      case 'highlight':
        // 等会话切换后的 DOM 渲染完成再滚动高亮，避免滚到上一个会话
        setTimeout(highlightLastAi, 260);
        break;
      case 'taskError':
        if (msg.sid && currentSid && msg.sid !== currentSid) break;
        addRetryCard(msg);
        break;
      case 'apiResp':
        var pcb = apiPending[msg.reqId];
        if (pcb) { delete apiPending[msg.reqId]; pcb({ ok: !!msg.ok, data: msg.data }); }
        break;
      case 'openFrame':
        openFrame(msg.url, msg.title || '');
        break;
      case 'showPorts':
        document.getElementById('portView').classList.add('show');
        break;
      case 'closeFrame':
        frameView.classList.remove('show');
        break;
      case 'diagnostics':
        renderDiagnostics(msg.items);
        break;
      case 'runResult': {
        var rc = msg.exitCode;
        var rcmd = String(msg.command || '').split('\n')[0];
        if (rcmd.length > 50) rcmd = rcmd.slice(0, 50) + '…';
        toast(rc === 0 ? ('✓ 运行完成：' + rcmd) : ('✗ 运行结束，退出码 ' + (rc == null ? '?' : rc) + '：' + rcmd));
        break;
      }
      case 'debugHint': {
        // P3-④ 断点命中提示卡片：stack + AI 分析（复用 renderMarkdown 渲染代码块）
        var card = document.createElement('div');
        card.className = 'debug-hint';
        var html = '<div style="background:#1e2230;border:1px solid #3a3f4f;border-radius:8px;padding:12px;margin:10px 0;">';
        html += '<div style="color:#ff9f7a;font-weight:600;font-size:13px;margin-bottom:6px;">🛑 断点命中 wasily AI 分析</div>';
        if (msg.filePath) html += '<div style="color:#8b8f9a;font-size:11px;margin-bottom:6px;">file: ' + esc(msg.filePath) + '</div>';
        if (msg.frames) html += '<pre style="color:#a7b0c2;background:#0f1118;padding:8px;border-radius:4px;font-size:11.5px;white-space:pre-wrap;margin:0 0 8px 0;">' + esc(msg.frames) + '</pre>';
        if (msg.content) {
          html += '<div style="border-top:1px solid #2a2e3e;padding-top:8px;margin-top:8px;">';
          html += '<div style="color:#8fbaff;font-size:12px;font-weight:600;margin-bottom:4px;">AI 建议</div>';
          html += '<div style="font-size:12px;line-height:1.55;">' + renderMarkdown(msg.content) + '</div>';
          html += '</div>';
        }
        html += '</div>';
        card.innerHTML = html;
        chatEl.appendChild(card);
        chatEl.scrollTop = chatEl.scrollHeight;
        break;
      }
    }
  });

  /* ==================== 输入栏弹层（权限 / 智能体 / 模型 / + 菜单） ==================== */
  var popEl = document.getElementById('pop');
  var popSubEl = document.getElementById('popSub');
  var permPill = document.getElementById('permPill');
  var agentPill = document.getElementById('agentPill');
  var modelPill = document.getElementById('modelPill');
  var permLabel = document.getElementById('permLabel');
  var agentLabel = document.getElementById('agentLabel');
  var modelLabel = document.getElementById('modelLabel');
  var btnAttach = document.getElementById('btnAttach');

  var curAgent = 'chat';         // 'chat' | 'agent' | 自定义智能体标识
  var curAgentPrompt = '';
  var curMode = 'solo';          // 'ask' | 'plan' | 'solo'（Agent，默认）
  var curPerm = 'full';          // manual | auto | full | custom
  var curModel = 'Auto';
  var cfgAgents = [];            // agents.custom
  var cfgModels = [];            // models.custom

  /* ---- 接口代理（WebView CSP 禁 fetch，经扩展中转） ---- */
  var apiSeq = 0, apiPending = {};
  function api(path, method, body, timeoutMs) {
    // 直连模式（无扩展）：直接 fetch 后端，返回结构与原扩展中转保持一致 { ok, data }
    if (!HAS_API) {
      return fetch(SERVER + path, {
        method: method || 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeoutMs || 8000)
      }).then(function (r) { return r.json(); }).then(function (d) {
        return { ok: true, data: d };
      }, function () { return { ok: false, data: null }; });
    }
    return new Promise(function (resolve) {
      var id = 'a' + (++apiSeq);
      apiPending[id] = resolve;
      post({ type: 'api', reqId: id, path: path, method: method || 'GET', body: body });
      setTimeout(function () {
        if (apiPending[id]) { delete apiPending[id]; resolve({ ok: false, data: null }); }
      }, timeoutMs || 8000);
    });
  }

  /* ---- 弹层定位与关闭 ---- */
  var popAnchor = null;
  var popOpen = false;                  // 显式状态，避免依赖 style.display 字符串比较
  function hideSub() { popSubEl.style.display = 'none'; }
  function hidePop() {
    popEl.style.display = 'none';
    hideSub();
    popAnchor = null;
    popOpen = false;
  }
  function showPop(anchor, html, force) {
    // toggle 收回：同一 anchor 且已打开（显式状态 + DOM 双重确认）
    if (!force && popAnchor === anchor && popOpen && popEl.style.display === 'block') {
      hidePop(); return;
    }
    // 切换 anchor：先彻底清掉旧的
    if (popAnchor && popAnchor !== anchor) hidePop();
    popEl.innerHTML = html;
    popEl.classList.toggle('wide', html.indexOf('vdForm') >= 0 || html.indexOf('undoList') >= 0);
    popEl.style.display = 'block';
    popEl.style.visibility = 'hidden';
    var r = anchor.getBoundingClientRect();
    var w = popEl.offsetWidth, h = popEl.offsetHeight;
    var left = Math.max(4, Math.min(r.left, window.innerWidth - w - 6));
    var bottom = window.innerHeight - r.top + 6;
    if (bottom + h > window.innerHeight - 4) bottom = window.innerHeight - h - 4;
    popEl.style.left = left + 'px';
    popEl.style.top = 'auto';
    popEl.style.bottom = bottom + 'px';
    popEl.style.visibility = 'visible';
    popAnchor = anchor;
    popOpen = true;
    hideSub();
  }
  function showSub(itemEl, html) {
    popSubEl.innerHTML = html;
    popSubEl.style.display = 'block';
    popSubEl.style.visibility = 'hidden';
    var r = itemEl.getBoundingClientRect();
    var w = popSubEl.offsetWidth, h = popSubEl.offsetHeight;
    var left = r.right + 4;
    if (left + w > window.innerWidth - 4) left = r.left - w - 4;
    var top = Math.min(r.top, window.innerHeight - h - 6);
    popSubEl.style.left = Math.max(4, left) + 'px';
    popSubEl.style.top = Math.max(4, top) + 'px';
    popSubEl.style.bottom = 'auto';
    popSubEl.style.visibility = 'visible';
  }
  document.addEventListener('click', function (e) {
    if (!popOpen) return;
    if (popEl.contains(e.target) || popSubEl.contains(e.target)) return;
    if (popAnchor && popAnchor.contains(e.target)) return;
    hidePop();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && popOpen) hidePop(); });

  /* ---- 图标与条目模板 ---- */
  function svg(p) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>'; }
  var IC = {
    at: '<circle cx="12" cy="12" r="3.4"/><path d="M15.4 12v1.6a2.6 2.6 0 0 0 5.2 0V12a8.6 8.6 0 1 0-3.4 6.8"/>',
    slash: '<path d="M14.5 4l-5 16"/>',
    upload: '<path d="M12 16V5M6.8 10.2 12 5l5.2 5.2"/><path d="M4.5 19.5h15"/>',
    cmd: '<path d="M5 7l5 5-5 5M12 17h7"/>',
    plug: '<path d="M9 7V4M15 7V4M7 7h10v4a5 5 0 0 1-10 0z"/><path d="M12 16v4"/>',
    skill: '<path d="M12 3l2.2 5.6L20 9.3l-4.2 3.9 1.2 5.8L12 16l-5 3 1.2-5.8L4 9.3l5.8-.7z"/>',
    doc: '<path d="M14 3.5H7.2A1.7 1.7 0 0 0 5.5 5.2v13.6a1.7 1.7 0 0 0 1.7 1.7h9.6a1.7 1.7 0 0 0 1.7-1.7V8z"/><path d="M14 3.5V8h5.5M9 13h6M9 16.4h6"/>',
    book: '<circle cx="9" cy="8.5" r="3"/><path d="M3.8 19.2c.6-3 2.6-4.7 5.2-4.7s4.6 1.7 5.2 4.7"/><path d="M16 6.2a2.9 2.9 0 0 1 0 5.6M17.4 14.8c2 .4 3.3 2 3.7 4.4"/>',
    arr: '<path d="M9 6l6 6-6 6"/>',
    check: '<path d="M5 12.5l4.2 4.2L19 7"/>',
    chat: '<path d="M4 6h16v10H8l-4 4zM8 10h8M8 13h5"/>',
    bot: '<rect x="5" y="8" width="14" height="10" rx="2.5"/><path d="M12 8V4.5M8.5 4.5h7"/><circle cx="9.3" cy="12.5" r=".9" fill="currentColor"/><circle cx="14.7" cy="12.5" r=".9" fill="currentColor"/>',
    spark: '<path d="M12 4l1.8 4.6L18.5 10l-4.7 1.4L12 16l-1.8-4.6L5.5 10l4.7-1.4z"/><path d="M18.2 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    lock: '<rect x="5.5" y="10.5" width="13" height="9" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
    zap: '<path d="M13 3L5 13.5h5L10.5 21 19 9.5h-5.5z"/>',
    shield: '<path d="M12 3l8 3v6c0 4.5-3.2 7.7-8 9-4.8-1.3-8-4.5-8-9V6zM9 12l2 2 4-4"/>',
    tune: '<path d="M5 8h9M18 8h1M5 16h3M12 16h7"/><circle cx="16" cy="8" r="2"/><circle cx="10" cy="16" r="2"/>'
  };
  function popIt(icon, text, attrs) {
    return '<div class="popIt" ' + (attrs || '') + '>' + svg(icon) + '<span>' + esc(text) + '</span></div>';
  }
  function subIt(icon, text, key) {
    return '<div class="popIt" data-sub="' + key + '">' + svg(icon) + '<span>' + esc(text) + '</span>' +
      svg(IC.arr).replace('<svg', '<svg class="arr"') + '</div>';
  }

  /* ---- 权限审批 ---- */
  var PERM_OPTS = [
    { k: 'manual', t: '手动审批', d: '所有 Agent 操作都需要你手动批准', i: IC.lock },
    { k: 'auto', t: '自动审批', d: '自动批准低风险操作，敏感操作需确认', i: IC.zap },
    { k: 'full', t: '完全访问', d: '自动批准所有 Agent 操作', i: IC.shield },
    { k: 'custom', t: '自定义', d: '按操作类型自定义审批规则', i: IC.tune }
  ];
  function applyPerm(k, save) {
    var opt = null;
    for (var i = 0; i < PERM_OPTS.length; i++) if (PERM_OPTS[i].k === k) opt = PERM_OPTS[i];
    if (!opt) opt = PERM_OPTS[2];
    curPerm = opt.k;
    permLabel.textContent = opt.t;
    permPill.classList.toggle('full', opt.k === 'full');
    permSel.value = (opt.k === 'full') ? 'full' : 'confirm';
    if (save) api('/config/set', 'POST', { path: 'permission.mode', value: opt.k });
  }
  function openPermMenu() {
    var html = '<div class="popHead">如何批准 Agent 的操作？<span class="lnk" data-a="permMore">了解更多</span></div>';
    PERM_OPTS.forEach(function (o) {
      html += '<div class="popOpt' + (o.k === curPerm ? ' on' : '') + '" data-perm="' + o.k + '">' +
        '<span class="oi">' + svg(o.i) + '</span>' +
        '<span><div class="ot">' + o.t + '</div><div class="od">' + o.d + '</div></span>' +
        '<span class="ock">' + svg(IC.check) + '</span></div>';
    });
    showPop(permPill, html);
  }

  /* ---- 智能体选择 ---- */
  function pickAgent(k, custom) {
    if (custom) {
      curAgent = custom.slug || custom.name;
      curAgentPrompt = custom.prompt || '';
      agentLabel.textContent = custom.name;
      setMode('solo');
    } else if (k === 'chat') {
      curAgent = 'chat';
      curAgentPrompt = '';
      agentLabel.textContent = 'Ask';
      setMode('ask');
    } else {
      curAgent = 'agent';
      curAgentPrompt = '';
      agentLabel.textContent = 'Agent';
      setMode('solo');
    }
  }
  function refreshAgents() {
    return api('/config/get').then(function (r) {
      if (r.ok && r.data && r.data.agents && Array.isArray(r.data.agents.custom)) {
        cfgAgents = r.data.agents.custom;
      }
    }).catch(function () { });
  }
  function openAgentMenu() {
    // 先用现有数据渲染；后台拉最新列表，若菜单仍开着则原位重绘
    var mine = popAnchor;
    refreshAgents().then(function () {
      if (mine === agentPill && popAnchor === agentPill && popOpen) {
        popEl.innerHTML = agentMenuHtml();
      }
    });
    var html = agentMenuHtml();
    showPop(agentPill, html);
  }
  function agentMenuHtml() {
    function modeOpt(k, icon, title, desc) {
      return '<div class="popOpt' + (curMode === k ? ' on' : '') + '" data-mode="' + k + '">' +
        '<span class="oi">' + svg(icon) + '</span>' +
        '<span><div class="ot">' + title + '</div><div class="od">' + desc + '</div></span>' +
        '<span class="ock">' + svg(IC.check) + '</span></div>';
    }
    var html = '<div class="popHead">模式</div>';
    html += modeOpt('ask', IC.chat, 'Ask', '问答答疑：只读分析，不修改文件');
    html += modeOpt('plan', IC.doc, 'Plan', '先规划：输出计划，确认后才执行');
    html += modeOpt('solo', IC.bot, 'Agent', '自主执行：可修改文件、运行命令');
    if (cfgAgents.length) {
      html += '<div class="popSep"></div><div class="popHead">自定义智能体</div>';
      cfgAgents.forEach(function (a, idx) {
        html += '<div class="popOpt' + (curAgent === (a.slug || a.name) ? ' on' : '') + '" data-agc="' + idx + '">' +
          '<span class="oi">' + svg(IC.bot) + '</span>' +
          '<span><div class="ot">' + esc(a.name) + '</div>' +
          (a.when ? '<div class="od">' + esc(a.when) + '</div>' : '') + '</span>' +
          '<span class="ock">' + svg(IC.check) + '</span></div>';
      });
    }
    html += '<div class="popSep"></div>' +
      '<div class="popOpt" data-a="openAgentChat">' +
      '<span class="oi">' + svg(IC.bot) + '</span>' +
      '<span><div class="ot">Agent 对话</div><div class="od">带着当前文件/选区去独立 Agent 页继续</div></span></div>';
    html += '<div class="popFoot" data-a="createAgent">＋ 创建智能体</div>';
    return html;
  }

  /* ---- 模型选择 ---- */
  var BRAND = { Seed: ['#1f2329', 'S'], GLM: ['#3d5af1', 'Z'], DeepSeek: ['#4d8dff', 'D'] };
  var MODELS = [
    { n: 'Seed-2.1-Turbo', b: 'Seed', tag: '专属补贴', m: '0.20x' },
    { n: 'Seed-2.1-Pro-0915', b: 'Seed', m: '0.80x' },
    { n: 'Seed-Evolving', b: 'Seed', m: '0.80x' },
    { n: 'Seed-Code', b: 'Seed', tag: '专属补贴', m: '0.06x' },
    { n: 'GLM-5.3-Flash', b: 'GLM', m: '0.08x' },
    { n: 'GLM-5.3-FlashX', b: 'GLM', m: '0.31x' },
    { n: 'GLM-5.3', b: 'GLM', tag: '会员限时优惠', m: '0.78x' },
    { n: 'GLM-5.2', b: 'GLM', m: '0.78x' },
    { n: 'DeepSeek-V4.1-Flash', b: 'DeepSeek', tag: '会员限时优惠', m: '0.06x' },
    { n: 'DeepSeek-V4-Flash', b: 'DeepSeek', tag: '闲时折扣', m: '0.20x' },
    { n: 'DeepSeek-V4-Pro', b: 'DeepSeek', tag: '闲时折扣', m: '0.72x' }
  ];
  function pickModel(n, save) {
    curModel = n;
    modelLabel.textContent = (n === 'Auto') ? 'Auto' : n;
    if (save) api('/config/set', 'POST', { path: 'ui.chat_model', value: n });
  }
  function modelRow(name, brand, tag, mult) {
    var on = (curModel === name);
    var icoHtml;
    if (brand === 'Auto') {
      icoHtml = '<span class="mico" style="background:#2f7d4f">' + svg(IC.spark) + '</span>';
    } else {
      var c = BRAND[brand] || ['#555', 'C'];
      icoHtml = '<span class="mico" style="background:' + c[0] + '">' + c[1] + '</span>';
    }
    return '<div class="popM' + (on ? ' on' : '') + '" data-m="' + esc(name) + '">' +
      icoHtml + '<span>' + esc(name) + '</span>' +
      (tag ? '<span class="mtag">' + esc(tag) + '</span>' : '') +
      (on ? '<span class="ock">' + svg(IC.check) + '</span>' : '<span class="mult">' + esc(mult || '') + '</span>') +
      '</div>';
  }
  function openModelMenu() {
    var html = '<div class="popHead">内置模型</div>';
    html += modelRow('Auto', 'Auto', '', '');
    MODELS.forEach(function (m) { html += modelRow(m.n, m.b, m.tag, m.m); });
    if (cfgModels.length) {
      html += '<div class="popSep"></div><div class="popHead">自定义模型</div>';
      cfgModels.forEach(function (m) { html += modelRow(m.name || m.n, '', '', ''); });
    }
    html += '<div class="popFoot" data-a="addModel">＋ 添加模型</div>';
    showPop(modelPill, html);
  }

  /* ---- “+” 菜单与子菜单 ---- */
  var QUICK_ITEMS = [
    ['explain', '解释代码'], ['addComments', '添加注释'],
    ['fixBug', '修复 Bug'], ['genTests', '生成测试']
  ];
  function openAttachMenu() {
    var html =
      popIt(IC.at, '使用 @ 引用上下文', 'data-a="at"') +
      popIt(IC.slash, '使用 / 调用命令和技能', 'data-a="slash"') +
      popIt(IC.upload, '上传附件', 'data-a="upload"') +
      popIt(IC.link || IC.at, '添加网页链接', 'data-a="weblink"') +
      '<div class="popSep"></div>' +
      subIt(IC.cmd, '命令', 'cmd') +
      subIt(IC.plug, '插件', 'plugin') +
      subIt(IC.skill, '技能', 'skill') +
      subIt(IC.doc, '飞书文档', 'fsdoc') +
      subIt(IC.book, '飞书通讯录', 'fscontact') +
      '<div class="popSep"></div>' +
      popIt(IC.spark, 'AI 视频生成', 'data-a="video"') +
      popIt(IC.doc, '文档编写', 'data-a="doc"') +
      popIt(IC.cmd, '操作历史', 'data-a="undo"');
    showPop(btnAttach, html);
  }
  /* ---- 网页链接附件：后端已落盘，面板只保存路径引用，不再走 base64 上传 ---- */
  function addWebLink(url) {
    var item = {
      id: 'att' + (++attachSeq), kind: 'text', name: url, size: 0,
      status: 'uploading', progress: 0, path: '', thumb: '', error: '', url: url
    };
    pendingDocs.push(item);
    renderAttachBar();
    return fetch(SERVER + '/web/fetch_attach', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: url })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok || !d.path) {
        throw new Error((d.error && d.error.message) || d.error || '抓取失败');
      }
      item.name = d.filename || ('webpage_' + Date.now() + '.md');
      item.size = Number(d.bytes) || String(d.content || '').length;
      item.path = d.path;
      item.url = d.url || url;
      item.status = 'ready';
      item.progress = 100;
      renderAttachBar();
      toast('网页已添加：' + item.name);
      return item;
    }).catch(function (e) {
      item.status = 'error';
      item.error = e.message || String(e);
      renderAttachBar();
      throw e;
    });
  }
  function weblinkOpen() {
    var html = '<div class="popHead">添加网页链接</div>' +
      '<div style="padding:12px 14px">' +
      '<input id="wlUrl" type="text" placeholder="粘贴网页地址，如 https://example.com/article" ' +
      'style="width:100%;min-width:320px;background:var(--card);border:1px solid var(--line2);color:var(--txt);border-radius:7px;padding:8px 12px;font-size:13px;outline:none">' +
      '<div style="font-size:11.5px;color:var(--dim);margin-top:8px">抓取网页正文转为 Markdown，作为对话附件。AI 可直接阅读并参考内容。</div>' +
      '<div id="wlStatus" style="font-size:12px;margin-top:8px"></div>' +
      '</div>' +
      '<div class="popFoot"><button id="wlFetch" class="primary">抓取并添加</button></div>';
    showPop(btnAttach, html, true);
    var urlEl = document.getElementById('wlUrl');
    urlEl.focus();
    document.getElementById('wlFetch').onclick = function () {
      var url = (urlEl.value || '').trim();
      if (!url) { toast('请输入网页地址'); return; }
      if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
      var st = document.getElementById('wlStatus');
      st.innerHTML = '<span style="color:var(--accent)">正在抓取…</span>';
      this.disabled = true;
      addWebLink(url).then(function () {
        hidePop();
      }).catch(function (e) {
        st.innerHTML = '<span style="color:var(--red)">' + esc(e.message || String(e)) + '</span>';
      }).finally(function () {
        var btn = document.getElementById('wlFetch');
        if (btn) btn.disabled = false;
      });
    };
    // 回车触发抓取
    urlEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); document.getElementById('wlFetch').click(); }
    });
  }

  /* ---- AI 视频生成 ---- */
  var VIDEO_DEFAULTS = { resolution: '1280x720', fps: 30, samplerate: 44100, style: '电影感' };
  var VIDEO_STYLES = ['电影感', '清新', '复古', '科技', '原片'];
  var videoState = null;    // { project, title, params:{...}, segs:[{material,text,seconds}] }
  var videoPoll = 0;        // 渲染进度轮询 timer
  function videoOpen() {
    if (!videoState) videoReset();
    videoRenderForm();
  }
  function videoReset() {
    videoState = { project: 'v' + Date.now().toString(36), title: '',
      params: { resolution: VIDEO_DEFAULTS.resolution, fps: VIDEO_DEFAULTS.fps,
                samplerate: VIDEO_DEFAULTS.samplerate, style: VIDEO_DEFAULTS.style },
      segs: [] };
  }
  function videoSyncForm() {
    /* 重渲染前把表单当前值收进 videoState.tmp，避免丢失用户已填内容 */
    var g = function (id) { var el = document.getElementById(id); return el ? el.value : undefined; };
    var t = g('vdTitle');
    if (t === undefined) return;
    videoState.title = t;
    videoState.tmp = {
      res: g('vdRes'), fps: Number(g('vdFps')) || 0, sr: Number(g('vdSr')) || 0,
      mat: g('vdMat'), txt: g('vdTxt'), sec: g('vdSec')
    };
    if (videoState.tmp.res) videoState.params.resolution = videoState.tmp.res;
    if (videoState.tmp.fps) videoState.params.fps = videoState.tmp.fps;
    if (videoState.tmp.sr) videoState.params.samplerate = videoState.tmp.sr;
  }
  function videoRenderForm(msgHtml) {
    var p = videoState.params;
    var tmp = videoState.tmp || {};
    var optRes = ['854x480', '1280x720', '1920x1080', '2560x1440'];
    var html = '<div class="popHead">AI 视频生成 · 工程 ' + esc(videoState.project) + '</div>';
    html += '<div class="vdForm">' +
      '<label>标题</div><input id="vdTitle" value="' + esc(videoState.title) + '" placeholder="视频标题（可选）">' +
      '<label>分辨率</label><select id="vdRes">' +
        optRes.map(function (r) { return '<option' + (r === p.resolution ? ' selected' : '') + '>' + r + '</option>'; }).join('') + '</select>' +
      '<label>帧率</label><select id="vdFps">' +
        [24, 25, 30, 60].map(function (f) { return '<option' + (f === p.fps ? ' selected' : '') + '>' + f + '</option>'; }).join('') + '</select>' +
      '<label>采样率</label><select id="vdSr">' +
        [44100, 48000].map(function (s) { return '<option' + (s === p.samplerate ? ' selected' : '') + '>' + s + '</option>'; }).join('') + '</select>' +
      '<label>风格</label><div class="vdStyles">' +
        VIDEO_STYLES.map(function (s) {
          return '<span class="vdSt' + (s === p.style ? ' on' : '') + '" data-st="' + s + '">' + s + '</span>';
        }).join('') + '</div>' +
      '<div class="vdSegHead">素材片段（' + videoState.segs.length + '）</div>' +
      '<div class="vdSegs">' +
        (videoState.segs.length ? videoState.segs.map(function (sg, i) {
          return '<div class="vdSeg"><span class="n">#' + i + '</span><span class="m" title="' +
            esc(sg.material) + '">' + esc(baseName(sg.material)) + '</span><span class="s">' +
            sg.seconds + 's</span><span class="x" data-delseg="' + i + '">×</span></div>';
        }).join('') : '<div class="vdNone">还没有片段，在下方添加本地素材</div>') +
      '</div>' +
      '<div class="vdAdd"><input id="vdMat" placeholder="素材路径：本地图片/视频绝对路径" value="' + esc(tmp.mat || '') + '">' +
        '<input id="vdTxt" placeholder="字幕文案（可选）" value="' + esc(tmp.txt || '') + '">' +
        '<input id="vdSec" type="number" min="0.5" step="0.5" value="' + esc(tmp.sec || '5') + '" title="片段时长（秒）">' +
        '<button id="vdAddBtn" class="primary">＋ 添加片段</button></div>' +
      (msgHtml || '') +
      '</div>';
    html += '<div class="popFoot vdFoot">' +
      '<button id="vdReset" title="恢复默认参数模板">恢复默认</button>' +
      '<button id="vdRender" class="primary"' + (videoState.segs.length ? '' : ' disabled') + '>开始渲染</button></div>';
    showPop(btnAttach, html, true);
  }
  function videoBusy(t) {
    videoRenderForm('<div class="vdBusy"><span class="spinDot"></span>' + esc(t) + '</div>');
    var btn = document.getElementById('vdRender');
    if (btn) btn.disabled = true;
  }
  function videoStartRender() {
    var vs = videoState;
    videoBusy('正在创建工程…');
    api('/tool/run', 'POST', { name: 'video_new', args: {
      project: vs.project, title: vs.title || vs.project,
      resolution: vs.params.resolution, fps: vs.params.fps,
      samplerate: vs.params.samplerate, style: vs.params.style } }, 30000)
    .then(function (r) {
      if (!r.ok || !r.data || !r.data.result || r.data.result.indexOf('已创建') < 0) {
        videoRenderForm('<div class="vdErr">✕ 创建工程失败：' + esc((r.data && r.data.result) || '服务无响应') + '</div>');
        return null;
      }
      videoBusy('正在导入 ' + vs.segs.length + ' 个素材片段…');
      var chain = Promise.resolve();
      vs.segs.forEach(function (sg) {
        chain = chain.then(function () {
          return api('/tool/run', 'POST', { name: 'video_add_segment', args: {
            project: vs.project, material_path: sg.material, text: sg.text, seconds: sg.seconds } }, 30000);
        });
      });
      return chain;
    }).then(function (last) {
      if (last === null) return;
      videoBusy('分片渲染中…长视频逐段渲染，可随时关闭弹层');
      videoPollStatus();
      api('/tool/run', 'POST', { name: 'video_render', args: { project: vs.project } }, 600000)
        .then(function (r) {
          clearInterval(videoPoll); videoPoll = 0;
          var txt = (r.ok && r.data && r.data.result) || '服务无响应';
          if (txt.indexOf('渲染完成') >= 0) {
            var m = txt.match(/outputs[\\\/]([^\n]+\.mp4)/i);
            var rel = m ? m[1].replace(/\\/g, '/') : '';
            videoRenderForm('<div class="vdOk">✓ 渲染完成</div>' +
              (txt.match(/共 .+/m) ? '<div class="vdInfo">' + esc(txt.match(/共 .+/m)[0]) + '</div>' : '') +
              (rel ? '<video class="vdPlay" controls src="' + SERVER + '/outputs/' + encodeURIComponent(rel) + '"></video>' +
                '<div class="vdExp"><a href="' + SERVER + '/agent/output?path=' + encodeURIComponent(rel) + '" target="_blank">导出视频</a>' +
                '<a href="' + SERVER + '/outputs/video/' + encodeURIComponent(videoState.project) + '/project.json" target="_blank">导出项目文件</a></div>' : ''));
          } else {
            videoRenderForm('<div class="vdErr">✕ ' + esc(txt).replace(/\n/g, '<br>') + '</div>' +
              '<div class="vdInfo">可修复后再次点击「开始渲染」，已完成片段自动跳过</div>');
          }
        });
    });
  }
  function videoPollStatus() {
    clearInterval(videoPoll);
    videoPoll = setInterval(function () {
      if (!popOpen || popAnchor !== btnAttach) { clearInterval(videoPoll); return; }
      api('/tool/run', 'POST', { name: 'video_status', args: { project: videoState.project } }, 8000)
        .then(function (r) {
          var t = (r.ok && r.data && r.data.result) || '';
          var m = t.match(/进度: (\w+) (\d+)\/(\d+)（([\d.]+)%）(.*)/);
          var busy = popEl.querySelector('.vdBusy');
          if (m && busy) busy.innerHTML = '<span class="spinDot"></span>分片渲染中 ' + m[2] + '/' + m[3] +
            '（' + m[4] + '%）<div class="vdBar"><i style="width:' + m[4] + '%"></i></div>' + esc(m[5] || '');
        });
    }, 1500);
  }
  /* ---- 操作历史（撤销/回退） ---- */
  function undoOpen() {
    showPop(btnAttach, '<div class="popHead">操作历史（最近 100 步）</div><div class="vdBusy"><span class="spinDot"></span>加载中…</div>', true);
    api('/undo/list', 'GET', null, 10000).then(function (r) {
      var list = (r.ok && r.data && r.data.entries) || [];
      var html = '<div class="popHead">操作历史（最近 100 步，最新在上）</div>';
      if (!list.length) {
        html += '<div class="vdNone" style="padding:10px 12px">暂无可撤销的操作</div>';
      } else {
        html += '<div class="undoList">' + list.map(function (e) {
          var ok = e.snap || !e.existed;
          return '<div class="undoRow">' +
            '<span class="uI">#' + e.i + '</span>' +
            '<span class="uT">' + esc(e.time || '') + '</span>' +
            '<span class="uK">' + esc(e.tool) + '</span>' +
            '<span class="uP" title="' + esc(e.path) + '">' + esc(baseName(e.path)) + '</span>' +
            (ok ? '<span class="uA" data-undo1="' + e.i + '" data-p="' + esc(e.path) + '">撤销此步</span>' +
                  '<span class="uA" data-undoto="' + e.i + '">回退到此</span>'
                : '<span class="uNo">不可撤销</span>') +
            '</div>';
        }).join('') + '</div>';
      }
      html += '<div class="popFoot vdFoot"><span class="vdInfo">撤销此步=只还原该步；回退到此=撤销比它新的所有步</span></div>';
      showPop(btnAttach, html, true);
    });
  }
  function undoAct(kind, idx, path, rowEl) {
    if (rowEl) rowEl.style.opacity = '.45';
    var url = kind === 'step' ? '/undo/step' : '/undo/to';
    var body = kind === 'step' ? { path: path || '' } : { index: idx };
    api(url, 'POST', body, 30000).then(function (r) {
      var msg = (r.ok && r.data && r.data.result) || '服务无响应';
      toast(msg.split('\n')[0]);
      undoOpen();
    });
  }
  /* ---- 文档编写 ---- */
  function docOpen() {
    var html = '<div class="popHead">文档编写（Word / PPT / Markdown）</div>' +
      '<div class="popIt" data-doc="word">' + svg(IC.doc) + '<span>Word 文档（.docx）</span></div>' +
      '<div class="popIt" data-doc="ppt">' + svg(IC.doc) + '<span>PPT 幻灯片（.pptx）</span></div>' +
      '<div class="popIt" data-doc="markdown">' + svg(IC.doc) + '<span>Markdown（.md）</span></div>' +
      '<div class="popFoot vdFoot"><span class="vdInfo">选择格式后告诉 AI 主题即可自动排版生成</span></div>';
    showPop(btnAttach, html, true);
  }
  function insertAtCursor(text) {
    var s = inputEl.selectionStart || 0, e2 = inputEl.selectionEnd || 0;
    var v = inputEl.value;
    inputEl.value = v.slice(0, s) + text + v.slice(e2);
    var p = s + text.length;
    inputEl.setSelectionRange(p, p);
    inputEl.focus();
    inputEl.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function openSub(key, itemEl) {
    if (key === 'cmd') {
      var html = '';
      QUICK_ITEMS.forEach(function (q) {
        html += '<div class="popIt" data-q="' + q[0] + '">' + svg(IC.cmd) + '<span>' + q[1] + '</span></div>';
      });
      showSub(itemEl, html);
    } else if (key === 'plugin') {
      showSub(itemEl, '<div class="popHead">加载中…</div>');
      api('/plugins/catalog').then(function (r) {
        var tools = (r.ok && r.data && r.data.user_tools) || [];
        tools = tools.filter(function (t) { return t.enabled; });
        var html = tools.length ? '' : '<div class="popHead">暂无已安装插件</div>';
        tools.forEach(function (t) {
          var nm = t.tool || String(t.file || '').replace(/\.py$/, '');
          html += '<div class="popIt" data-ins="/' + esc(nm) + ' ">' + svg(IC.plug) + '<span>' + esc(nm) + '</span></div>';
        });
        if (popSubEl.style.display === 'block') showSub(itemEl, html);
      });
    } else if (key === 'skill') {
      showSub(itemEl, '<div class="popHead">加载中…</div>');
      api('/agent/skills').then(function (r) {
        var sk = (r.ok && r.data && r.data.skills) || [];
        var html = sk.length ? '' : '<div class="popHead">暂无可用技能</div>';
        sk.forEach(function (s) {
          var trig = (s.triggers && s.triggers[0]) || s.name;
          html += '<div class="popIt" data-ins="' + esc(trig) + ' ">' + svg(IC.skill) +
            '<span>' + esc(s.name) + '</span><span class="sub">' + esc(trig) + '</span></div>';
        });
        if (popSubEl.style.display === 'block') showSub(itemEl, html);
      });
    } else {
      showSub(itemEl, '<div class="popHead">未连接飞书</div>' +
        '<div class="popIt" style="cursor:default;color:#63656d"><span>请在设置中心完成飞书授权</span></div>');
    }
  }

  /* ---- 弹层点击委托 ---- */
  popEl.addEventListener('click', function (e) {
    var el = e.target.closest ? e.target.closest('.popIt,.popOpt,.popM,.popFoot,.lnk,.uA,.vdSt,.vdSeg .x,button') : null;
    if (!el || !popEl.contains(el)) return;
    /* 视频表单控件 */
    if (el.classList.contains('vdSt')) {
      videoSyncForm();
      videoState.params.style = el.getAttribute('data-st'); videoRenderForm(); return;
    }
    var delseg = el.getAttribute('data-delseg');
    if (delseg !== null && delseg !== undefined && delseg !== '') {
      videoSyncForm();
      videoState.segs.splice(Number(delseg), 1); videoRenderForm(); return;
    }
    if (el.id === 'vdAddBtn') {
      var mat = (document.getElementById('vdMat').value || '').trim();
      if (!mat) { toast('请填写素材路径'); return; }
      videoSyncForm();
      videoState.segs.push({ material: mat,
        text: (videoState.tmp.txt || '').trim(),
        seconds: Math.max(0.5, Number(videoState.tmp.sec) || 5) });
      videoState.tmp.mat = ''; videoState.tmp.txt = '';
      videoRenderForm(); return;
    }
    if (el.id === 'vdReset') {
      videoSyncForm();
      videoState.params = { resolution: VIDEO_DEFAULTS.resolution, fps: VIDEO_DEFAULTS.fps,
        samplerate: VIDEO_DEFAULTS.samplerate, style: VIDEO_DEFAULTS.style };
      videoRenderForm(); toast('已恢复默认参数模板'); return;
    }
    if (el.id === 'vdRender') {
      videoSyncForm();
      videoStartRender(); return;
    }
    /* 操作历史 */
    var u1 = el.getAttribute('data-undo1');
    if (u1) { undoAct('step', Number(u1), el.getAttribute('data-p') || '', el.parentElement); return; }
    var ut = el.getAttribute('data-undoto');
    if (ut) { undoAct('to', Number(ut), '', el.parentElement); return; }
    /* 文档编写：选格式 → 输入框放引导语 */
    var doc = el.getAttribute('data-doc');
    if (doc) {
      hidePop();
      var hint = { word: '帮我写一份 Word 文档：', ppt: '帮我做一份 PPT：', markdown: '帮我写一份 Markdown 文档：' }[doc];
      insertAtCursor(hint || '帮我写文档：');
      return;
    }
    var sub = el.getAttribute('data-sub');
    if (sub) { openSub(sub, el); return; }
    var a = el.getAttribute('data-a');
    if (a === 'at') { hidePop(); insertAtCursor('@wasily '); return; }
    if (a === 'slash') { hidePop(); insertAtCursor('/'); return; }
    if (a === 'upload') { hidePop(); fileInput.click(); return; }
    if (a === 'weblink') { weblinkOpen(); return; }
    if (a === 'video') { videoOpen(); return; }
    if (a === 'doc') { docOpen(); return; }
    if (a === 'undo') { undoOpen(); return; }
    if (a === 'permMore') { hidePop(); post({ type: 'openSettings', tab: 'perm' }); return; }
    if (a === 'createAgent') { hidePop(); post({ type: 'openAgentCenter', tab: 'agents' }); return; }
    if (a === 'openAgentChat') { hidePop(); post({ type: 'openAgentChat' }); return; }
    if (a === 'addModel') { hidePop(); post({ type: 'openSettings', tab: 'model' }); return; }
    var pm = el.getAttribute('data-perm');
    if (pm) { applyPerm(pm, true); hidePop(); return; }
    var ag = el.getAttribute('data-ag');
    if (ag) { pickAgent(ag); hidePop(); return; }
    var md = el.getAttribute('data-mode');
    if (md) { setMode(md); hidePop(); return; }
    var agc = el.getAttribute('data-agc');
    if (agc !== null && agc !== undefined && agc !== '') { pickAgent(null, cfgAgents[Number(agc)]); hidePop(); return; }
    var mm = el.getAttribute('data-m');
    if (mm) { pickModel(mm, true); hidePop(); return; }
  });
  popSubEl.addEventListener('click', function (e) {
    var el = e.target.closest ? e.target.closest('.popIt') : null;
    if (!el || !popSubEl.contains(el)) return;
    var q = el.getAttribute('data-q');
    if (q) { hidePop(); post({ type: 'quick', kind: q }); return; }
    var ins = el.getAttribute('data-ins');
    if (ins) { hidePop(); insertAtCursor(ins); return; }
    var optmode = el.getAttribute('data-optmode');
    if (optmode) {
      var optpath = el.getAttribute('data-optpath') || '';
      hidePop();
      var OPT_PROMPTS = {
        polish: '内容润色：修正错别字与语病，让表达更自然流畅，保持原意与篇幅基本不变',
        structure: '逻辑结构调整：梳理内容逻辑，调整段落/章节顺序，补全缺失的过渡，使结构更清晰',
        format: '格式标准化：统一缩进/命名/标点/排版风格，符合该文件类型的官方格式规范',
        perf: '性能优化：分析并优化性能瓶颈（算法复杂度、重复计算、资源释放），保持功能行为不变'
      };
      inputEl.value = '请优化文件 ' + (optpath || '（当前附件）') + '。优化模式——' +
        (OPT_PROMPTS[optmode] || OPT_PROMPTS.polish) +
        '。请先用 file_read 读取文件，优化后用 file_write 写回原路径（可随时在「操作历史」中撤销）。';
      send();
      return;
    }
  });

  permPill.addEventListener('click', function (e) { e.stopPropagation(); openPermMenu(); });
  agentPill.addEventListener('click', function (e) { e.stopPropagation(); openAgentMenu(); });
  modelPill.addEventListener('click', function (e) { e.stopPropagation(); openModelMenu(); });

  /* ---- 初始化：读取配置应用 权限/模型/自定义智能体 ---- */
  // 直连模式：空态文案后追加一行模式提示（沿用现有 .empty 样式）
  if (!HAS_API) {
    var emptyEl = document.getElementById('empty');
    if (emptyEl) emptyEl.innerHTML += '<br>（浏览器直连模式：文件改动确认需在 IDE 面板中使用）';
    // 直连模式没有扩展的 workspaceState，刷新后从服务端持久化会话恢复上次对话与断点
    api('/session/state?sid=vscodium', 'GET', null, 8000).then(function (r) {
      if (!r || !r.ok || !r.data) return;
      var d = r.data;
      if ((d.messages && d.messages.length) || d.interrupted) {
        renderAll(d.sid || 'vscodium', d.messages || [], d.interrupted || null);
      }
    });
  }
  startTokenPoll();   // 面板打开即拉取一次 Token 统计，并持续轮询（否则在未对话前一直显示 "--"）
  api('/config/get').then(function (r) {
    var cfg = (r.ok && r.data) || {};
    try {
      applyPerm((cfg.permission && cfg.permission.mode) || 'full', false);
      if (cfg.ui && cfg.ui.chat_model) pickModel(cfg.ui.chat_model, false);
      if (cfg.agents && Array.isArray(cfg.agents.custom)) cfgAgents = cfg.agents.custom;
      if (cfg.models && Array.isArray(cfg.models.custom)) cfgModels = cfg.models.custom;
    } catch (e) { /* 配置异常时保持默认 */ }
  });

  post({ type: 'ready' });
})();

;

(function () {
  var KEY = 'wasily.panel.settings.v1';
  var DEF = { theme: 'dark', font: '', size: 13, line: '1.6', gap: 'normal', mono: "Consolas, 'Courier New', monospace", hideMark: false };
  var S = Object.assign({}, DEF);
  try { Object.assign(S, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) { }
  var $ = function (id) { return document.getElementById(id); };

  function apply() {
    var root = document.documentElement;
    if (S.theme === 'ide') delete document.body.dataset.panelTheme;
    else document.body.dataset.panelTheme = S.theme;
    root.style.setProperty('--fs', S.size + 'px');
    root.style.setProperty('--lh', S.line);
    root.style.setProperty('--mono', S.mono);
    document.body.style.fontFamily = S.font ? (S.font + ', "Microsoft YaHei", sans-serif') : '';
    document.body.dataset.gap = S.gap;
    document.body.classList.toggle('hideAiMark', !!S.hideMark);
    document.querySelectorAll('pre, code, .lv-tool .lv-name').forEach(function (el) {
      el.style.fontFamily = S.mono;
    });
  }

  function sync() {
    $('stTheme').value = S.theme;
    $('stFont').value = S.font;
    $('stSize').value = S.size;
    $('stSizeVal').textContent = S.size;
    $('stLine').value = S.line;
    $('stGap').value = S.gap;
    $('stMono').value = S.mono;
    $('stMark').checked = !!S.hideMark;
  }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) { }
    apply();
  }

  $('btnSettings').addEventListener('click', function () {
    sync();
    $('setPage').classList.add('on');
  });
  // 积分芯片：打开设置中心「用量管理」（与 Agent 顶栏入口同一页）
  var _creditBtn = $('btnCredit');
  if (_creditBtn) _creditBtn.addEventListener('click', function () {
    post({ type: 'openSettings', tab: 'usage' });
  });
  $('setClose').addEventListener('click', function () { $('setPage').classList.remove('on'); });
  document.querySelectorAll('.setNavItem').forEach(function (b) {
    b.addEventListener('click', function () {
      document.querySelectorAll('.setNavItem').forEach(function (x) { x.classList.remove('on'); });
      document.querySelectorAll('.setSec').forEach(function (x) { x.classList.remove('on'); });
      b.classList.add('on');
      $('sec-' + b.dataset.sec).classList.add('on');
    });
  });
  $('stTheme').addEventListener('change', function () {
    S.theme = this.value; save();
    // 同时写入设置中心的通用配置，两个入口保持一致
    fetch('http://127.0.0.1:8000/config/set', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: 'ui.theme', value: this.value === 'ide' ? 'system' : this.value })
    }).catch(function () { });
  });
  $('stFont').addEventListener('change', function () { S.font = this.value; save(); });
  $('stSize').addEventListener('input', function () { S.size = parseFloat(this.value); $('stSizeVal').textContent = S.size; save(); });
  $('stLine').addEventListener('change', function () { S.line = this.value; save(); });
  $('stGap').addEventListener('change', function () { S.gap = this.value; save(); });
  $('stMono').addEventListener('change', function () { S.mono = this.value; save(); });
  $('stMark').addEventListener('change', function () { S.hideMark = this.checked; save(); });

  apply();
  // 读取设置中心「通用」页保存的配置，让主题/AI 水印/发送按键真正生效
  function applyCfgUi(ui) {
    if (!ui) return;
    var t = ui.theme;
    if (t === 'light') { document.body.dataset.panelTheme = 'light'; S.theme = 'light'; }
    else if (t === 'dark') { document.body.dataset.panelTheme = 'dark'; S.theme = 'dark'; }
    else if (t === 'system') { delete document.body.dataset.panelTheme; S.theme = 'ide'; }
    if (ui.ai_watermark === false) document.body.classList.add('hideAiMark');
    else document.body.classList.remove('hideAiMark');
    window.__wasilySendKey = ui.send_key || 'enter';
  }
  // 首屏由后端内联注入，避免闪烁；独立打开页面时再兜底拉一次
  applyCfgUi(window.__WASILY_UI);
  if (!window.__WASILY_UI) {
    fetch('http://127.0.0.1:8000/config/get', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (cfg) {
        var c = cfg && (cfg.value || cfg);
        applyCfgUi(c && c.ui);
        sync();
      })
      .catch(function () { });
  } else {
    sync();
  }
  // 支持 #settings 深链：便于从外部直接打开设置页
  if (location.hash === '#settings') {
    sync();
    $('setPage').classList.add('on');
  }
})();
