// block 1

const _qs = new URLSearchParams(location.search);
const agentEmbed = _qs.get('embed') === 'agent';
const WB_MODE = _qs.get('workbench') === '1';
const WB_ID = String(_qs.get('workbench_id') || '').trim();
const WB_WINDOW_ID = String(_qs.get('window_id') || '').trim();
const WB_IDE = _qs.get('embed') === 'ide';
if (agentEmbed) document.body.classList.add('agent-embed');
if (WB_MODE) document.body.classList.add('workbench');
if (WB_MODE && WB_IDE) document.body.classList.add('ide-workbench');
const chat = document.getElementById('chat');
const input = document.getElementById('input');
if (agentEmbed) input.placeholder = '想做什么事，直接跟我说';
// 模板库「使用模板」跳转过来时预填提示词（agent/index.html 写入 wasily_prefill）
try {
  const _prefill = localStorage.getItem('wasily_prefill');
  if (_prefill) {
    localStorage.removeItem('wasily_prefill');
    input.value = _prefill;
    setTimeout(() => { input.focus(); input.setSelectionRange(_prefill.length, _prefill.length); }, 0);
  }
} catch (e) {}
const sendBtn = document.getElementById('sendBtn');
const micBtn = document.getElementById('micBtn');
const container = document.querySelector('.container');

// IDE ↔ Agent 联动：/chat?session=ide:<id> 直接进入编辑器里那场对话，继续聊
const sessionParam = _qs.get('session') || '';
// 独立对话窗口：主窗口用 ?prompt= 预填提示词、?new=1 要求清空上一轮
const promptParam = _qs.get('prompt') || '';
const newChatParam = _qs.get('new') === '1';
if (promptParam) {
  input.value = promptParam;
  setTimeout(() => { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }, 0);
}
if (newChatParam) {
  setTimeout(() => {
    if (typeof clearChat === 'function') clearChat();
    try { input.value = ''; input.dispatchEvent(new Event('input')); } catch (e) {}
  }, 0);
}

/* ============ 跨进程通信：Agent 主窗口 ↔ 对话面板/独立窗口 ============ */
// 两个 pywebview 进程各有独立 WebView2 存储，localStorage 不互通，
// 统一走后端内存队列：主窗口 POST 下发命令，这里轮询取走并回传当前状态。
const IPC_MODE = _qs.get('ipc') === '1' || agentEmbed;
// 每个页面实例用独立 client 名：同时开着内嵌面板与独立窗口时，各自维护游标，
// 后端按游标广播，不会出现一条命令只被其中一个窗口消费。
const IPC_CLIENT = ((window.self !== window.top) ? 'chat_embed_' : 'chat_') +
  Math.random().toString(36).slice(2, 8);

function ipcApply(cmd) {
  const p = cmd.payload || {};
  if (cmd.action === 'set_session' && p.session_id) {
    switchToSession(p.session_id);
  } else if (cmd.action === 'clear') {
    clearChat();
    // 主窗口的「新对话」语义是重新开始，草稿也一并清掉
    input.value = '';
    input.dispatchEvent(new Event('input'));
  } else if (cmd.action === 'prefill') {
    input.value = p.text || '';
    input.dispatchEvent(new Event('input'));
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch (e) {}
  } else if (cmd.action === 'focus') {
    try { input.focus(); } catch (e) {}
  } else if (cmd.action === 'copy_last') {
    const list = chat.querySelectorAll('.msg.ai .md');
    const last = list[list.length - 1];
    const t = last ? last.innerText.trim() : '';
    if (t) {
      navigator.clipboard.writeText(t).then(() => toast('已复制最近回复')).catch(() => {});
    } else {
      toast('还没有可复制的回复');
    }
  }
}

async function ipcPoll() {
  try {
    const r = await fetch('/agent/ipc?client=' + IPC_CLIENT, { cache: 'no-store' });
    const d = await r.json();
    (d.commands || []).forEach(ipcApply);
  } catch (e) {}
}

async function ipcReport() {
  try {
    const lastUser = document.querySelector('#chat .msg.user .bubble');
    await fetch('/agent/ipc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client: IPC_CLIENT,
        action: 'state',
        payload: {
          session_id: sessionId,
          title: lastUser ? lastUser.textContent.slice(0, 40) : '',
          busy: !!lockGenerating
        }
      })
    });
  } catch (e) {}
}

if (IPC_MODE) {
  setInterval(ipcPoll, 900);
  setInterval(ipcReport, 3000);
  setTimeout(ipcReport, 600);
}

// 独立窗口顶部身份条：有账号信息就用真实名称与套餐
(async function fillIdChip() {
  const chip = document.getElementById('idChip');
  if (!chip) return;
  try {
    const d = await (await fetch('/account/info', { cache: 'no-store' })).json();
    const name = String(d.name || '').trim() || '本地';
    const plan = String(d.plan || '').trim() || '免费';
    chip.innerHTML = '<b></b><span class="plan"></span>';
    chip.querySelector('b').textContent = name;
    chip.querySelector('.plan').textContent = plan;
  } catch (e) {}
})();

const fromIde = _qs.get('from') === 'ide' || sessionParam.indexOf('ide:') === 0;
// 会话 id 持久化：刷新/重开窗口后仍能接上服务端同一场会话，断点续传才有效
let sessionId = sessionParam || (function () {
  try {
    const saved = localStorage.getItem('wasily_chat_sid');
    if (saved) return saved;
  } catch (e) {}
  const sid = 'session_' + Math.random().toString(36).substr(2, 9);
  try { localStorage.setItem('wasily_chat_sid', sid); } catch (e) {}
  return sid;
})();
const MAX_DOM_MSGS = 60;   // 对话页最多保留60条DOM（更早的移除，防止长对话越来越卡）

/* ============ 任务提示面板 + 任务状态条（独立模块，先注册键盘监听） ============ */
const taskStrip = new TaskHint.Strip(document.getElementById('taskStrip'));
const taskHintPanel = new TaskHint.Panel({
  input: input,
  host: document.getElementById('inputArea'),
  getHistory: () => Array.prototype.map.call(
    document.querySelectorAll('#chat .msg.user .bubble'),
    el => el.textContent).slice(-6),
  onFill: () => { /* 填充后焦点由模块保持，不自动发送 */ }
});
window.__taskHint = taskHintPanel;   // 供其他逻辑查询面板开关态

/* ============ 轻提示 ============ */
const toastEl = document.getElementById('toast');
let toastTimer = null;
function toast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 1800);
}

/* ============ 本地存储：对话持久化（key: wasily_chat_history） ============ */
const LS_KEY = 'wasily_chat_history';
const LS_MAX = 200;        // 最多持久化200条

function loadStore() {
  try { return JSON.parse(localStorage.getItem(LS_KEY)) || []; }
  catch (e) { return []; }
}
function saveStore(arr) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(arr));
  } catch (e) {
    // 超容量：先剥掉最旧消息里的图片，再不行就丢弃最旧消息
    for (const m of arr) { if (m.images && m.images.length) { m.images = []; break; } }
    try { localStorage.setItem(LS_KEY, JSON.stringify(arr)); }
    catch (e2) { arr.shift(); saveStore(arr); }
  }
}
function persistMsg(role, content, images, extra) {
  if (WB_MODE) return;
  const arr = loadStore();
  const item = { role: role, content: content || '', images: images || [], ts: Date.now() };
  // 思考时间线随消息一起存，重开页面后仍能还原「任务耗时 / 已思考」折叠行
  if (extra && extra.steps && extra.steps.length) item.steps = extra.steps;
  if (extra && extra.elapsed) item.elapsed = extra.elapsed;
  arr.push(item);
  while (arr.length > LS_MAX) arr.shift();
  saveStore(arr);
}

/* ============ 本地存储：输入历史（↑ 键翻阅 + 面板搜索） ============ */
const IH_KEY = 'wasily_input_history';
let inputHist = [];
try { inputHist = JSON.parse(localStorage.getItem(IH_KEY)) || []; } catch (e) {}
let histIdx = -1;          // -1 表示不在翻阅状态
let histDraft = '';        // 翻阅前的草稿

function pushInputHist(text) {
  if (!text) return;
  if (inputHist[inputHist.length - 1] !== text) {
    inputHist.push(text);
    while (inputHist.length > 100) inputHist.shift();
    try { localStorage.setItem(IH_KEY, JSON.stringify(inputHist)); } catch (e) {}
  }
}

/* ============ TTS 设置（音色/情感持久化） ============ */
// Agent 默认使用吴晓欣克隆音色；用户明确关闭后，刷新仍保持关闭。
const ttsOnKey = agentEmbed ? 'wasily_agent_tts_on' : 'wasily_tts_on';
const savedTtsOn = localStorage.getItem(ttsOnKey);
let ttsEnabled = savedTtsOn === null ? agentEmbed : savedTtsOn === '1';
let ttsVoice = agentEmbed ? 'wxx_clone' : (localStorage.getItem('wasily_tts_voice') || 'wxx_clone');
let ttsEmotion = localStorage.getItem('wasily_tts_emotion') || 'normal';

const ttsPanel = document.getElementById('ttsPanel');
const ttsOn = document.getElementById('ttsOn');
const voiceSel = document.getElementById('voiceSel');
const emotionSel = document.getElementById('emotionSel');
ttsOn.checked = ttsEnabled;

fetch('/voices').then(r => r.json()).then(d => {
  const vs = d.voices || {}, es = d.emotions || {};
  voiceSel.innerHTML = Object.entries(vs).map(([k, v]) =>
    `<option value="${k}" ${k === ttsVoice ? 'selected' : ''}>${v.name}</option>`).join('');
  emotionSel.innerHTML = Object.entries(es).map(([k, v]) =>
    `<option value="${k}" ${k === ttsEmotion ? 'selected' : ''}>${v.label}</option>`).join('');
  voiceSel.insertAdjacentHTML('afterbegin', '<option value="">默认</option>');
}).catch(() => {});

voiceSel.onchange = () => { ttsVoice = voiceSel.value; localStorage.setItem('wasily_tts_voice', ttsVoice); };
emotionSel.onchange = () => { ttsEmotion = emotionSel.value; localStorage.setItem('wasily_tts_emotion', ttsEmotion); };
ttsOn.onchange = () => {
  ttsEnabled = ttsOn.checked;
  localStorage.setItem(ttsOnKey, ttsEnabled ? '1' : '0');
  if (!ttsEnabled) stopSpeak();
  syncTtsToggle();
};
// Agent 内嵌页没有右上角菜单，直接显示语音开关；独立聊天仍由更多菜单进入。
const ttsToggleBtn = document.getElementById('ttsToggle');
ttsToggleBtn.style.display = agentEmbed ? '' : 'none';
function syncTtsToggle() {
  ttsToggleBtn.innerHTML = ttsEnabled
    ? '<i data-icon="volume-3" class="ico"></i>'
    : '<i data-icon="volume-off" class="ico"></i>';
  ttsToggleBtn.style.opacity = ttsEnabled ? '1' : '.55';
  ttsToggleBtn.title = (agentEmbed ? '吴晓欣语音回复：' : '语音回复：') + (ttsEnabled ? '已开启' : '已关闭');
  ttsToggleBtn.setAttribute('aria-pressed', ttsEnabled ? 'true' : 'false');
}
ttsToggleBtn.addEventListener('click', () => {
  ttsEnabled = !ttsEnabled;
  localStorage.setItem(ttsOnKey, ttsEnabled ? '1' : '0');
  ttsOn.checked = ttsEnabled;
  if (!ttsEnabled) stopSpeak();
  syncTtsToggle();
  toast(ttsEnabled ? '吴晓欣语音回复已开启' : '语音回复已关闭');
});
syncTtsToggle();

/* ============ 「⋯」下拉菜单 ============ */
const menuBtn = document.getElementById('menuBtn');
const menuPanel = document.getElementById('menuPanel');

menuBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  ttsPanel.classList.remove('show');
  menuPanel.classList.toggle('show');
});
document.addEventListener('click', (e) => {
  if (!menuPanel.contains(e.target) && e.target !== menuBtn) menuPanel.classList.remove('show');
  if (!ttsPanel.contains(e.target)) ttsPanel.classList.remove('show');
});
menuPanel.addEventListener('click', (e) => {
  const actBtn = e.target.closest ? e.target.closest('button[data-act]') : null;
  if (!actBtn || !menuPanel.contains(actBtn)) return;
  const act = actBtn.dataset.act;
  menuPanel.classList.remove('show');
  if (act === 'history') openHistoryDrawer();
  else if (act === 'inputs') openInputHistPanel();
  else if (act === 'tts') ttsPanel.classList.toggle('show');
  else if (act === 'tts-quick') ttsToggleBtn.click();
  else if (act === 'fs') toggleFullscreen();
  else if (act === 'video') window.open('/video_call', '_blank', 'width=960,height=1080');
  else if (act === 'settings') openSidePanel('/settings', '系统设置');
  else if (act === 'kb') window.open('/kb', '_blank');
  else if (act === 'agent') window.open('/agent', '_blank', 'width=1180,height=760');
  else if (act === 'group') openSidePanel('/group', '多 AI 圆桌');
  else if (act === 'selfcheck') runSelfCheck();
  else if (act === 'help') openDocsCenter();
  else if (act === 'export') exportChat();
  else if (act === 'clear') clearChat();
});

/* ============ AI 自检：另一个审阅员 AI 检查上一条回答 ============ */
let lastAiReply = '';
let lastUserMsg = '';
let selfChecking = false;
async function runSelfCheck(){
  if (selfChecking) { toast('自检进行中，请稍候'); return; }
  if (!lastAiReply) { toast('还没有可检查的 AI 回答'); return; }
  selfChecking = true;
  addMsg('ai', '🔍 自检中：审阅员 AI 正在检查上一条回答…', { selfcheck: true });
  try {
    const r = await fetch('/selfcheck', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: lastAiReply, question: lastUserMsg })
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '自检失败');
    const head = '【AI 自检】' + (d.verdict || '') + (d.score != null ? ' · 置信度 ' + d.score : '');
    addMsg('ai', head + '\n\n' + d.review, { selfcheck: true });
  } catch (e) {
    addMsg('ai', '⚠️ 自检失败：' + e.message, { selfcheck: true });
  } finally {
    selfChecking = false;
  }
}

/* 帮助：打开项目文档中心里的 wasily AI 项目文档 */
function openDocsCenter(){
  const url = '/docs?project=wasily';
  const w = window.open(url, '_blank');
  if (w) w.opener = null;
}

/* ============ 右侧浮动面板：视频通话 / 系统设置（iframe，左缘拖拽调宽） ============ */
const sidePanel = document.getElementById('sidePanel');
const sidePanelFrame = document.getElementById('sidePanelFrame');
const sidePanelTitle = document.getElementById('sidePanelTitle');
function openSidePanel(url, title) {
  sidePanelTitle.textContent = title;
  if (sidePanelFrame.dataset.src !== url) {   // 首次或切换页面时才重新加载
    sidePanelFrame.src = url;
    sidePanelFrame.dataset.src = url;
  }
  sidePanel.classList.add('show');
}
function closeSidePanel() { sidePanel.classList.remove('show'); }
document.getElementById('sidePanelClose').addEventListener('click', closeSidePanel);
document.addEventListener('click', (e) => {
  if (!sidePanel.classList.contains('show')) return;
  if (sidePanel.contains(e.target)) return;
  if (e.target.closest && e.target.closest('#menuPanel, #menuBtn, #creditChip')) return;   // 忽略本次打开菜单/设置面板的点击
  closeSidePanel();
});
/* 左缘拖拽调宽 */
document.getElementById('sidePanelResize').addEventListener('pointerdown', (e) => {
  e.preventDefault();
  const grip = e.currentTarget;
  grip.classList.add('dragging');
  const startX = e.clientX, startW = sidePanel.offsetWidth;
  const mv = (ev) => {
    const w = Math.min(Math.max(startW + (startX - ev.clientX), 360),
      Math.round(window.innerWidth * 0.92));
    sidePanel.style.width = w + 'px';
  };
  const up = () => {
    grip.classList.remove('dragging');
    document.removeEventListener('pointermove', mv);
    document.removeEventListener('pointerup', up);
  };
  document.addEventListener('pointermove', mv);
  document.addEventListener('pointerup', up);
});

/* ============ 顶栏快捷图标（TRAE 风格：新建 / 历史 / 搜索 / 导出） ============ */
document.getElementById('newChatBtn').addEventListener('click', () => { clearChat(); });
document.getElementById('histBtn').addEventListener('click', () => { openHistoryDrawer(); });
document.getElementById('searchBtn').addEventListener('click', () => { openInputHistPanel(); });
document.getElementById('shareBtn').addEventListener('click', () => { exportChat(); });

/* ============ 窗口控制按钮（pywebview 优先，缺失则前端模拟） ============ */
function pyApi(method) {
  // 调 pywebview js_api；返回是否成功调用
  try {
    if (window.pywebview && window.pywebview.api &&
        typeof window.pywebview.api[method] === 'function') {
      window.pywebview.api[method]();
      return true;
    }
  } catch (e) {}
  return false;
}
const minPill = document.getElementById('minPill');
document.getElementById('winClose').onclick = () => {
  if (!pyApi('quit')) window.close();   // 模拟：浏览器环境直接关标签
};
const winMaxBtn = document.getElementById('winMax');
winMaxBtn.onclick = () => {
  if (!pyApi('toggle_max')) {           // 模拟：容器铺满窗口
    document.body.classList.toggle('win-max');
    winMaxBtn.innerHTML = document.body.classList.contains('win-max')
      ? '<i data-icon="arrows-minimize" class="ico"></i>'
      : '<i data-icon="square" class="ico" style="font-size:11px"></i>';
  }
};
document.getElementById('winMin').onclick = () => {
  if (!pyApi('toggle_min')) {           // 模拟：收起为恢复胶囊
    container.style.display = 'none';
    minPill.classList.add('show');
  }
};
minPill.onclick = () => {
  container.style.display = 'flex';
  minPill.classList.remove('show');
};

/* ============ 全屏模式（F11，入口在右上角「更多」菜单） ============ */
function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else document.documentElement.requestFullscreen().catch(() => {
    // 全屏 API 不可用（如部分 pywebview 内核）：退化为铺满窗口
    document.body.classList.toggle('win-max');
  });
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'F11') { e.preventDefault(); toggleFullscreen(); }
  if (e.key === 'Escape') {
    document.getElementById('lightbox').classList.remove('show');
    document.getElementById('ihOverlay').classList.remove('show');
    closeSidePanel();
  }
});

/* ============ 大脑显示区：开关 / 下边缘拖拽调高度 / ⤢弹出桌面纯大脑 ============ */
const brainOn = document.getElementById('brainOn');
const brainView = document.getElementById('brainView');
const brainFrame = document.getElementById('brainFrame');
const brainResize = document.getElementById('brainResize');
const brainFloatBtn = document.getElementById('brainFloatBtn');

const BRAIN_H_MIN = 120;
function brainMaxH() {
  // 上限：60% 窗高 与 窗高-200 预留 取小，但不低于最小值
  return Math.max(BRAIN_H_MIN, Math.min(Math.round(window.innerHeight * 0.6),
    Math.round(window.innerHeight) - 200));
}
let savedBrainH = parseInt(localStorage.getItem('wasily_brain_h') || '220', 10);
function applyBrainHeight(h) {
  h = Math.max(BRAIN_H_MIN, Math.min(brainMaxH(), Math.round(h)));
  brainView.style.height = h + 'px';
  return h;
}
function applyBrainSwitch() {
  const on = brainOn.checked;
  brainView.classList.toggle('show', on);
  brainResize.style.display = on ? '' : 'none';
  if (on) {
    if (!brainFrame.src) brainFrame.src = brainFrame.dataset.src;  // 懒加载
    applyBrainHeight(savedBrainH);
  }
}
brainOn.checked = localStorage.getItem('brain_enabled') !== '0';   // 默认展开大脑
brainOn.onchange = () => {
  localStorage.setItem('brain_enabled', brainOn.checked ? '1' : '0');
  applyBrainSwitch();
};

/* ---- 大脑区下边缘：上下拖动调整高度（document 级监听，触摸/鼠标均可） ---- */
let brainDrag = null;
function onBrainMove(e) {
  if (!brainDrag) return;
  brainDrag.h = applyBrainHeight(brainDrag.h + (e.clientY - brainDrag.y));
  brainDrag.y = e.clientY;
}
function endBrainDrag() {
  if (!brainDrag) return;
  savedBrainH = brainView.clientHeight;
  localStorage.setItem('wasily_brain_h', String(savedBrainH));
  brainDrag = null;
  brainResize.classList.remove('dragging');
  document.body.style.userSelect = '';
  brainFrame.style.pointerEvents = '';   // 恢复 iframe 交互
  document.removeEventListener('pointermove', onBrainMove, true);
  document.removeEventListener('pointerup', endBrainDrag, true);
  document.removeEventListener('pointercancel', endBrainDrag, true);
}
brainResize.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  brainDrag = { y: e.clientY, h: brainView.clientHeight };
  brainResize.classList.add('dragging');
  document.body.style.userSelect = 'none';
  // 关键：指针一进入 iframe（大脑区）pointermove 就会被其吞掉，导致高度不再实时跟随；
  // 拖动期间禁用 iframe 指针事件，事件全程留在父文档，高度实时生效
  brainFrame.style.pointerEvents = 'none';
  try { brainResize.setPointerCapture(e.pointerId); } catch (err) {}
  // document 级捕获：即使指针移出把手也能连续拖动
  document.addEventListener('pointermove', onBrainMove, true);
  document.addEventListener('pointerup', endBrainDrag, true);
  document.addEventListener('pointercancel', endBrainDrag, true);
});
window.addEventListener('resize', () => { if (brainOn.checked) applyBrainHeight(savedBrainH); });

/* ---- ⤢ 弹出桌面：固定位置的纯大脑无边框悬浮窗 ---- */
let floatSpawning = false;
function spawnBrainFloat() {
  if (floatSpawning) return;
  floatSpawning = true;
  toast('正在弹出大脑到桌面…');
  fetch('/spawn_brain', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        toast('大脑已悬浮到桌面（纯大脑 · 固定位置）');
        brainOn.checked = false;   // 弹出后收起内嵌大脑
        localStorage.setItem('brain_enabled', '0');
        applyBrainSwitch();
      } else {
        toast('弹出失败：' + (d.error || '未知错误'));
      }
    })
    .catch(() => toast('弹出失败：服务不可用'))
    .finally(() => { floatSpawning = false; });
}
brainFloatBtn.addEventListener('click', spawnBrainFloat);

applyBrainSwitch();

/* ============ 轻量 Markdown 解析器（离线自研，无外部依赖） ============ */
function mdEsc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* 简单语法高亮：注释 / 字符串 / 关键字 / 数字 */
function mdHighlight(code) {
  const re = /(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\\n]|\\.)*`)|\b(const|let|var|function|return|if|else|elif|for|while|class|import|from|def|async|await|try|except|finally|with|as|pass|break|continue|new|this|self|None|True|False|null|true|false|undefined|print|len|range|str|int|float|list|dict|set|tuple|in|is|not|and|or|lambda|yield|raise|static|void|long|double|char|bool|struct|enum|switch|case|default|typeof|instanceof|delete|export|extends|super|catch|throw|package|namespace|using|fn|mut|impl|pub|match|loop|go|defer|select|interface|map)\b|(\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)/g;
  return mdEsc(code).replace(re, (m, com, str, kw, num) => {
    if (com) return '<span class="tk-c">' + m + '</span>';
    if (str) return '<span class="tk-s">' + m + '</span>';
    if (kw)  return '<span class="tk-k">' + m + '</span>';
    if (num) return '<span class="tk-n">' + m + '</span>';
    return m;
  });
}

function mdSafeUrl(url) {
  // 清理 URL，阻断 javascript: 等危险协议
  url = url.replace(/["'<>\s\\]/g, '');
  if (/^\s*javascript:/i.test(url) || /^\s*vbscript:/i.test(url)) return '';
  return url;
}

function mdRender(src) {
  if (!src) return '';
  src = String(src).replace(/\r\n?/g, '\n');

  // 1) 提取围栏代码块 ```lang ... ``` 为占位符（带语言头 + 折叠，>12 行默认收起）
  const blocks = [];
  src = src.replace(/```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g, (m, lang, code) => {
    lang = (lang || '').trim();
    // 协议块（tool_call/tasks/plan 等）不是正文代码：一律不渲染成代码卡，
    // 否则模型漏写 JSON 的空 ```tool 块会显示为「TOOL 1 行」的空白卡片
    if (/^(tool|tool_call|tasks|plan|task|task_done)$/i.test(lang)) return '';
    const codeText = code.replace(/\n+$/, '');
    if (!codeText.trim()) return '';   // 空代码块不渲染「N 行」空卡
    const lines = codeText.split('\n').length;
    const folded = lines > 12 ? ' folded' : '';
    blocks.push('<pre class="codeblock' + folded + '" data-lines="' + lines + '">' +
      '<div class="code-bar"><span class="code-lang">' + (mdEsc(lang) || 'code') + '</span>' +
      '<span class="code-meta">' + lines + ' 行</span>' +
      '<button type="button" class="code-fold-btn" tabindex="-1">' +
      '<i data-icon="arrows-minimize" class="ico cb-ico"></i><span class="cb-txt">' +
      (lines > 12 ? '展开' : '收起') + '</span></button></div>' +
      '<code>' + mdHighlight(codeText) + '</code></pre>');
    return '\n\u0000' + (blocks.length - 1) + '\u0000\n';
  });

  // 2) 转义剩余 HTML
  src = mdEsc(src);

  // 3) 提取行内代码 `code` 为占位符
  const inls = [];
  src = src.replace(/`([^`\n]+)`/g, (m, c) => {
    inls.push('<code class="ic">' + c + '</code>');
    return '\u0001' + (inls.length - 1) + '\u0001';
  });

  // 4) 行内元素：图片、链接、粗体、斜体、删除线
  function inline(t) {
    t = t.replace(/!\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g, (m, alt, url) => {
      url = mdSafeUrl(url);
      if (!url) return alt;
      return '<img class="md-img" src="' + url + '" alt="' + alt + '" loading="lazy">';
    });
    t = t.replace(/\[([^\]]+)\]\(([^)\s]+)[^)]*\)/g, (m, txt, url) => {
      url = mdSafeUrl(url);
      if (!url) return txt;
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + txt + '</a>';
    });
    t = t.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/__([^_]+)__/g, '<b>$1</b>');
    t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>')
         .replace(/(^|[^_])_([^_\n]+)_/g, '$1<i>$2</i>');
    t = t.replace(/~~([^~]+)~~/g, '<s>$1</s>');
    return t;
  }

  // 5) 块级解析：标题 / 分割线 / 引用 / 列表 / 段落
  const lines = src.split('\n');
  let html = '', i = 0;
  const listStack = [];
  function closeLists(minIndent) {
    while (listStack.length && listStack[listStack.length - 1].indent >= minIndent) {
      html += '</' + listStack.pop().type + '>';
    }
  }
  const BLOCK_START = /^\s*(\u0000\d+\u0000|#{1,6}\s|[-*+]\s|\d+[.)]\s|>|&gt;)/;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { closeLists(0); i++; continue; }

    // 代码块占位符（独占一行）
    let m = line.match(/^\s*\u0000(\d+)\u0000\s*$/);
    if (m) { closeLists(0); html += '\u0000' + m[1] + '\u0000'; i++; continue; }

    // 标题 #-######
    m = line.match(/^(#{1,6})\s+(.*)$/);
    if (m) { closeLists(0); const lv = m[1].length;
      html += '<h' + lv + '>' + inline(m[2].trim()) + '</h' + lv + '>'; i++; continue; }

    // 分割线
    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { closeLists(0); html += '<hr>'; i++; continue; }

    // 引用块（连续 > 行；注意 > 已被转义为 &gt;）
    const QUOTE_RE = /^\s*(?:>|&gt;)\s?/;
    if (QUOTE_RE.test(line)) {
      closeLists(0);
      const qs = [];
      while (i < lines.length && QUOTE_RE.test(lines[i])) {
        qs.push(lines[i].replace(QUOTE_RE, '')); i++;
      }
      html += '<blockquote>' + qs.map(q => inline(q)).join('<br>') + '</blockquote>';
      continue;
    }

    // 列表项（支持按缩进嵌套，有序/无序混合）
    m = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (m) {
      const indent = m[1].replace(/\t/g, '  ').length;
      const type = /^\d/.test(m[2]) ? 'ol' : 'ul';
      if (!listStack.length || indent > listStack[listStack.length - 1].indent) {
        html += '<' + type + '>'; listStack.push({ type: type, indent: indent });
      } else {
        closeLists(indent + 1);
        if (listStack.length && listStack[listStack.length - 1].indent === indent
            && listStack[listStack.length - 1].type !== type) {
          html += '</' + listStack.pop().type + '><' + type + '>';
          listStack.push({ type: type, indent: indent });
        }
        if (!listStack.length) { html += '<' + type + '>'; listStack.push({ type: type, indent: indent }); }
      }
      html += '<li>' + inline(m[3].trim()) + '</li>';
      i++; continue;
    }

    // 普通段落：收集到空行或下一个块级元素为止
    closeLists(0);
    const ps = [line]; i++;
    while (i < lines.length && lines[i].trim()
           && !BLOCK_START.test(lines[i])
           && !/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(lines[i])) {
      ps.push(lines[i]); i++;
    }
    const inner = ps.map(p => inline(p.trim())).join('<br>');
    // 首行缩进仅加给纯文本段落（含图片的段落不缩进）
    const cls = /<\s*img/.test(inner) ? '' : ' class="ti"';
    html += '<p' + cls + '>' + inner + '</p>';
  }
  closeLists(0);

  // 6) 还原占位符
  html = html.replace(/\u0000(\d+)\u0000/g, (m, n) => blocks[+n] || '');
  html = html.replace(/\u0001(\d+)\u0001/g, (m, n) => inls[+n] || '');
  return html;
}

/* Markdown → 纯文本（供 TTS 朗读） */
function mdToPlain(t) {
  return String(t)
    .replace(/```[\s\S]*?(?:```|$)/g, '。代码略。')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '。图片。')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[#>*`_~|-]/g, '')
    .replace(/\s+/g, ' ').trim();
}

/* ============ AI 头像（纯图标，形象切换功能已随 avatar 模块下线） ============ */
function aiAvatarContent() {
  return '<i data-icon="brain" class="ico" style="font-size:15px;color:#c9f7dc"></i>';
}

/* ============ 图片附件（选择 / 粘贴，base64 内嵌显示） ============ */
const attachBtn = document.getElementById('attachBtn');
const fileInput = document.getElementById('fileInput');
const attachBar = document.getElementById('attachBar');
let pendingImgs = [];   // 待发送的 base64 图片
let pendingDocs = [];   // 待发送的文本文档 { name, size, text }

/* 文本类文档扩展名白名单（代码/配置/数据标记语言） */
const DOC_EXTS = ['txt','md','markdown','json','js','ts','jsx','tsx','py','html','htm','css',
  'csv','tsv','log','yaml','yml','xml','ini','conf','env','bat','cmd','ps1','sh',
  'c','h','cpp','hpp','cs','java','go','rs','rb','php','sql','r','swift','kt','vue','toml','svg'];
function fmtSize(n) {
  return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
}
/* 任务耗时展示：小于 1 分钟显示秒，超过显示「分 + 秒」 */
function fmtDur(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  // 对齐 Trae 的耗时写法：1m 7s / 43s
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm' + (s % 60 ? ' ' + (s % 60) + 's' : '');
  return Math.floor(m / 60) + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '');
}

/* 递归收集拖入的文件（含文件夹）：用 webkitGetAsEntry 逐层展开目录，
   全部扁平化后再回调 handleFiles。不支持该 API 时回退到 dataTransfer.files */
function collectDroppedFiles(dataTransfer, done) {
  const items = dataTransfer && dataTransfer.items;
  const entries = [];
  if (items && items.length) {
    for (const it of items) {
      if (it.kind === 'file') {
        const en = it.webkitGetAsEntry && it.webkitGetAsEntry();
        if (en) entries.push(en);
      }
    }
  }
  if (!entries.length) { done(Array.from((dataTransfer && dataTransfer.files) || [])); return; }
  const out = [];
  let pending = entries.length;
  function finish() { if (--pending <= 0) done(out); }
  function readEntry(entry) {
    if (entry.isFile) {
      entry.file(f => { out.push(f); finish(); }, () => finish());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const readBatch = () => reader.readEntries(batch => {
        if (!batch.length) { finish(); return; }
        batch.forEach(sub => { pending++; readEntry(sub); });
        readBatch();               // readEntries 分批返回，需循环读到空
      }, () => finish());
      readBatch();
    } else {
      finish();
    }
  }
  entries.forEach(readEntry);
}

/* 文件统一入口：图片 → base64 附件；文本类文档 → 读取内容随消息发给模型 */
function isTextLike(f) {
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  return !f.type || (f.type && (f.type.startsWith('text/') || f.type === 'application/json'
    || f.type === 'application/javascript' || f.type === 'application/xml'))
    || DOC_EXTS.includes(ext);
}
function handleFiles(files) {
  let imgs = 0, docs = 0, skipped = 0;
  Array.from(files || []).forEach(f => {
    if (!f) return;
    if (f.type && f.type.startsWith('image/')) { readImageFile(f); imgs++; return; }
    if (isTextLike(f)) { readDocFile(f); docs++; return; }
    skipped++;
  });
  if (imgs) toast('已添加 ' + imgs + ' 张图片');
  if (docs) toast('已添加 ' + docs + ' 个文档');
  if (skipped) toast(skipped + ' 项已跳过（支持图片与文本类文档）');
}
function readDocFile(file) {
  if (file.size > 2 * 1024 * 1024) { toast('「' + file.name + '」超过 2MB，已跳过'); return; }
  const reader = new FileReader();
  reader.onload = () => {
    let t = String(reader.result || '');
    if (t.length > 200000) t = t.slice(0, 200000) + '\n…（内容过长已截断）';
    addPendingDoc({ name: file.name, size: file.size, text: t });
  };
  reader.readAsText(file);
}
function addPendingDoc(doc) {
  pendingDocs.push(doc);
  renderAttachBar();
}

attachBtn.onclick = () => fileInput.click();

/* ============ 输入框 @ 文件引用 / / 命令菜单（Trae 式） ============
   @：列出工作区文件，选中后把「@相对路径」写进输入框并读取正文挂为附件；
   /：行首命令菜单（新对话 / 清空 / 引用文件 / 帮助）。 */
const refPop = document.createElement('div');
refPop.className = 'slash-pop';
document.body.appendChild(refPop);
let refMode = '', refItems = [], refIdx = 0, refSpan = null, refTimer = null;

function hideRefPop() {
  refPop.style.display = 'none';
  refMode = ''; refItems = []; refSpan = null;
}
function refPlace() {
  const r = input.getBoundingClientRect();
  refPop.style.left = Math.round(r.left + 10) + 'px';
  refPop.style.width = Math.min(560, Math.max(260, Math.round(r.width - 20))) + 'px';
  refPop.style.top = Math.max(8, Math.round(r.top - refPop.offsetHeight - 8)) + 'px';
}
function renderRefPop() {
  refPop.innerHTML = '';
  refItems.forEach((it, i) => {
    const d = document.createElement('div');
    d.className = 'sp-item' + (i === refIdx ? ' on' : '');
    d.innerHTML = '<span class="sp-nm">' + mdEsc(it.label) + '</span>' +
      (it.desc ? '<span class="sp-ds">' + mdEsc(it.desc) + '</span>' : '');
    d.onmousedown = e => { e.preventDefault(); chooseRef(i); };
    refPop.appendChild(d);
  });
  if (!refItems.length) { hideRefPop(); return; }
  refPop.style.display = 'block';
  refPlace();
  const cur = refPop.querySelector('.sp-item.on');
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
}
function refMatch() {
  const v = input.value;
  const pos = input.selectionStart == null ? v.length : input.selectionStart;
  const before = v.slice(0, pos);
  let m = before.match(/(^|\n)\/([^\s\/@]*)$/);
  if (m) return { mode: 'cmd', q: m[2], start: pos - m[2].length - 1, end: pos };
  m = before.match(/(^|\s)@([^\s@]*)$/);
  if (m) return { mode: 'file', q: m[2], start: pos - m[2].length - 1, end: pos };
  return null;
}
function insertAtCursor(t) {
  const s = input.selectionStart == null ? input.value.length : input.selectionStart;
  const ePos = input.selectionEnd == null ? s : input.selectionEnd;
  input.value = input.value.slice(0, s) + t + input.value.slice(ePos);
  input.setSelectionRange(s + t.length, s + t.length);
  input.dispatchEvent(new Event('input'));
  syncRefPop();
}
const REF_CMDS = [
  { label: '/new', desc: '开始新对话', run: () => clearChat() },
  { label: '/clear', desc: '清空当前对话', run: () => clearChat() },
  { label: '/file', desc: '引用工作区文件（等同输入 @）', run: () => insertAtCursor('@') },
  { label: '/help', desc: '查看可用命令', run: () => toast('可用命令：/new 新对话、/clear 清空、/file 引用文件') }
];
function syncRefPop() {
  if (WB_MODE) { hideRefPop(); return; }
  const mt = refMatch();
  if (!mt) { hideRefPop(); return; }
  refMode = mt.mode;
  refSpan = { start: mt.start, end: mt.end };
  if (refMode === 'cmd') {
    const q = mt.q.toLowerCase();
    refItems = REF_CMDS.filter(c => !q || c.label.slice(1).startsWith(q))
      .map(c => ({ label: c.label, desc: c.desc, cmd: c }));
    refIdx = 0;
    renderRefPop();
    return;
  }
  clearTimeout(refTimer);
  refTimer = setTimeout(async () => {
    try {
      const r = await fetch('/v1/ws/files?q=' + encodeURIComponent(mt.q), { cache: 'no-store' });
      const d = await r.json();
      if (refMode !== 'file') return;
      refItems = (d.files || []).map(f =>
        ({ label: f.name, desc: f.rel, path: f.path, size: f.size }));
      refIdx = 0;
      renderRefPop();
    } catch (e) { hideRefPop(); }
  }, 220);
}
async function chooseRef(i) {
  const it = refItems[i];
  if (!it) return;
  const span = refSpan || { start: input.value.length, end: input.value.length };
  if (it.cmd) {
    input.value = input.value.slice(0, span.start) + input.value.slice(span.end);
    input.setSelectionRange(span.start, span.start);
    input.dispatchEvent(new Event('input'));
    hideRefPop();
    it.cmd.run();
    return;
  }
  const ins = '@' + it.desc + ' ';
  input.value = input.value.slice(0, span.start) + ins + input.value.slice(span.end);
  input.setSelectionRange(span.start + ins.length, span.start + ins.length);
  input.dispatchEvent(new Event('input'));
  hideRefPop();
  try {
    const r = await fetch('/v1/ws/file?path=' + encodeURIComponent(it.path), { cache: 'no-store' });
    const d = await r.json();
    if (d && d.ok) {
      addPendingDoc({ name: d.name || it.label, size: d.size || it.size || 0, text: d.text || '' });
      toast('已引用 ' + (d.name || it.label));
    } else {
      toast((d && d.error) || '引用失败');
    }
  } catch (e) { toast('引用失败：' + e.message); }
}
window.addEventListener('keydown', e => {
  if (!refMode) return;
  if (e.key === 'ArrowDown') {
    refIdx = Math.min(refIdx + 1, Math.max(0, refItems.length - 1));
    renderRefPop(); e.preventDefault(); e.stopPropagation();
  } else if (e.key === 'ArrowUp') {
    refIdx = Math.max(refIdx - 1, 0);
    renderRefPop(); e.preventDefault(); e.stopPropagation();
  } else if ((e.key === 'Enter' || e.key === 'Tab') && refItems.length) {
    chooseRef(refIdx); e.preventDefault(); e.stopPropagation();
  } else if (e.key === 'Escape') {
    hideRefPop(); e.preventDefault(); e.stopPropagation();
  }
}, true);
input.addEventListener('input', syncRefPop);
input.addEventListener('blur', () => setTimeout(hideRefPop, 120));
window.addEventListener('scroll', () => { if (refMode) refPlace(); }, true);

/* ============ composer 工具行：橙色盾牌保护 / @智能体 / Auto 模式 ============ */
const shieldBtn = document.getElementById('shieldBtn');
let shieldOn = localStorage.getItem('wasily_shield') !== '0';   // 默认开启
function syncShield() {
  shieldBtn.classList.toggle('off', !shieldOn);
  shieldBtn.innerHTML = '<i data-icon="' + (shieldOn ? 'shield-check' : 'shield') + '" class="ico"></i>';
}
shieldBtn.onclick = () => {
  shieldOn = !shieldOn;
  localStorage.setItem('wasily_shield', shieldOn ? '1' : '0');
  syncShield();
  toast(shieldOn ? '工具执行保护已开启' : '工具执行保护已关闭');
};
syncShield();

/* 「完全访问 / 受保护」权限胶囊：与盾牌开关同一份状态，两处入口保持一致 */
const accessChip = document.getElementById('accessChip');
const accessPop = document.getElementById('accessPop');
function syncAccess() {
  const name = document.getElementById('accessChipName');
  if (name) name.textContent = shieldOn ? '受保护' : '完全访问';
  if (accessPop) accessPop.querySelectorAll('button').forEach(b =>
    b.classList.toggle('on', (b.dataset.access === 'safe') === shieldOn));
}
if (accessChip && accessPop) {
  accessChip.addEventListener('click', (e) => {
    e.stopPropagation();
    accessPop.classList.toggle('show');
  });
  accessPop.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-access]');
    if (!b) return;
    const wantSafe = b.dataset.access === 'safe';
    if (wantSafe !== shieldOn) shieldBtn.onclick();
    syncAccess();
    accessPop.classList.remove('show');
  });
  document.addEventListener('click', (e) => {
    if (!accessPop.contains(e.target) && !accessChip.contains(e.target)) accessPop.classList.remove('show');
  });
}
syncAccess();

const agentChip = document.getElementById('agentChip');
const agentPop = document.getElementById('agentPop');
const modeChip = document.getElementById('modeChip');
const modePop = document.getElementById('modePop');
function bindChipPop(chip, pop) {
  chip.addEventListener('click', (e) => {
    e.stopPropagation();
    const otherOpen = agentPop.classList.contains('show') || modePop.classList.contains('show');
    agentPop.classList.remove('show');
    modePop.classList.remove('show');
    if (!otherOpen || !pop.classList.contains('show')) pop.classList.toggle('show');
  });
  document.addEventListener('click', (e) => {
    if (!pop.contains(e.target) && !chip.contains(e.target)) pop.classList.remove('show');
  });
}
bindChipPop(agentChip, agentPop);
bindChipPop(modeChip, modePop);
agentPop.addEventListener('click', (e) => {
  const b = e.target.closest('button[data-agent]');
  if (!b) return;
  agentPop.querySelectorAll('button').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  const label = b.textContent.replace('✓', '').trim();
  document.getElementById('agentChipName').textContent = b.dataset.agent;
  localStorage.setItem('wasily_agent', b.dataset.agent);
  agentPop.classList.remove('show');
  toast('已切换智能体：' + label);
});
let replyMode = localStorage.getItem('wasily_reply_mode') || 'auto';
(function restoreAgent() {
  const saved = localStorage.getItem('wasily_agent');
  if (!saved) return;
  const b = agentPop.querySelector('button[data-agent="' + saved + '"]');
  if (b) {
    agentPop.querySelectorAll('button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    document.getElementById('agentChipName').textContent = saved;
  }
})();
function syncModePop() {
  modePop.querySelectorAll('button').forEach(x =>
    x.classList.toggle('on', x.dataset.mode === replyMode));
  const labelMap = { auto: 'Auto', chat: '对话', code: '编码' };
  document.getElementById('modeChipName').textContent = labelMap[replyMode] || 'Auto';
}
modePop.addEventListener('click', (e) => {
  const b = e.target.closest('button[data-mode]');
  if (!b) return;
  replyMode = b.dataset.mode;
  localStorage.setItem('wasily_reply_mode', replyMode);
  syncModePop();
  modePop.classList.remove('show');
});
syncModePop();
fileInput.onchange = () => {
  handleFiles(fileInput.files);
  fileInput.value = '';
};
function readImageFile(file) {
  if (!file || !file.type.startsWith('image/')) return;
  const reader = new FileReader();
  reader.onload = () => downscaleImage(reader.result, addPendingImg);
  reader.readAsDataURL(file);
}
/* 大图压缩到最长边1024px，避免 localStorage 爆容量 */
function downscaleImage(dataUrl, cb) {
  const img = new Image();
  img.onload = () => {
    const MAX = 1024;
    if (img.width <= MAX && img.height <= MAX && dataUrl.length < 400 * 1024) return cb(dataUrl);
    const scale = Math.min(1, MAX / Math.max(img.width, img.height));
    const cv = document.createElement('canvas');
    cv.width = Math.round(img.width * scale);
    cv.height = Math.round(img.height * scale);
    cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
    cb(cv.toDataURL('image/jpeg', 0.85));
  };
  img.onerror = () => cb(dataUrl);
  img.src = dataUrl;
}
function addPendingImg(dataUrl) {
  pendingImgs.push(dataUrl);
  renderAttachBar();
}
function renderAttachBar() {
  attachBar.innerHTML = '';
  attachBar.classList.toggle('has', pendingImgs.length > 0 || pendingDocs.length > 0);
  pendingImgs.forEach((url, idx) => {
    const chip = document.createElement('div');
    chip.className = 'att-chip';
    chip.innerHTML = '<img src="' + url + '"><button class="att-x" title="移除"><i data-icon="x" class="ico"></i></button>';
    chip.querySelector('img').onclick = () => openLightbox(url);
    chip.querySelector('.att-x').onclick = () => {
      pendingImgs.splice(idx, 1);
      renderAttachBar();
    };
    attachBar.appendChild(chip);
  });
  pendingDocs.forEach((doc, idx) => {
    const chip = document.createElement('div');
    chip.className = 'att-doc';
    chip.title = doc.name + '（' + fmtSize(doc.size) + '）';
    chip.innerHTML = '<span class="doc-ico"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 1.5h5.5L13 5v9.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1z"/><path d="M9.5 1.5V5H13"/></svg></span>'
      + '<span class="doc-name">' + doc.name + '</span>'
      + '<button class="att-x" title="移除">✕</button>';
    chip.querySelector('.att-x').onclick = () => {
      pendingDocs.splice(idx, 1);
      renderAttachBar();
    };
    attachBar.appendChild(chip);
  });
}
/* 输入框粘贴：图片/复制的文件 → 附件流程；纯文本不受影响 */
input.addEventListener('paste', (e) => {
  const cd = e.clipboardData || {};
  const files = [];
  for (const it of (cd.items || [])) {
    if (it.kind === 'file') {
      const f = it.getAsFile();
      if (f && (f.type || f.size)) files.push(f);
    }
  }
  if (files.length) {
    e.preventDefault();               // 有文件时阻止默认插入，统一走附件流程
    handleFiles(files);
  }
});

/* ============ 整窗拖放（TRAE 同款）：图片/文档拖入窗口即添加附件；
   提示框只显示在输入框内部（#composerDrop），拖放接收范围仍为整窗 ============ */
const composerDrop = document.getElementById('composerDrop');
let dragDepth = 0;
addEventListener('dragenter', (e) => {
  const types = Array.from((e.dataTransfer && e.dataTransfer.types) || []);
  if (!types.includes('Files')) return;
  e.preventDefault();
  dragDepth++;
  composerDrop.classList.add('show');
});
addEventListener('dragover', (e) => { e.preventDefault(); });   // 允许 drop
addEventListener('dragleave', (e) => {
  e.preventDefault();
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) composerDrop.classList.remove('show');
});
addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  composerDrop.classList.remove('show');
  collectDroppedFiles(e.dataTransfer, handleFiles);
});

/* ---- 大脑 iframe 拖放桥接：iframe 内部的拖放事件不会冒泡到父页，
   导致顶部大脑区域拖放无响应。iframe 与主页面同源，直接在其文档上
   绑定同一套处理，共享 dragDepth 计数与输入框提示框 ---- */
function bindBrainDragBridge() {
  const doc = brainFrame.contentDocument;
  if (!doc) return;
  doc.addEventListener('dragenter', (e) => {
    const types = Array.from((e.dataTransfer && e.dataTransfer.types) || []);
    if (!types.includes('Files')) return;
    e.preventDefault();
    dragDepth++;
    composerDrop.classList.add('show');
  });
  doc.addEventListener('dragover', (e) => { e.preventDefault(); });   // 允许 drop
  doc.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) composerDrop.classList.remove('show');
  });
  doc.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    composerDrop.classList.remove('show');
    collectDroppedFiles(e.dataTransfer, handleFiles);
  });
}
brainFrame.addEventListener('load', bindBrainDragBridge);
if (brainFrame.contentDocument && brainFrame.contentDocument.readyState === 'complete') {
  bindBrainDragBridge();   // iframe 早于本脚本加载完成的兜底
}

/* ============ 图片灯箱 ============ */
const lightbox = document.getElementById('lightbox');
const lbImg = document.getElementById('lbImg');
function openLightbox(url) {
  lbImg.src = url;
  lightbox.classList.add('show');
}
lightbox.onclick = () => lightbox.classList.remove('show');
/* 事件委托：点击消息中的图片放大 */
chat.addEventListener('click', (e) => {
  if (e.target.tagName === 'IMG' && e.target.classList.contains('md-img')) {
    openLightbox(e.target.src);
  }
  // 代码块折叠 / 展开（事件委托，兼容流式追加的内容）
  const foldBtn = e.target.closest && e.target.closest('.code-fold-btn');
  if (foldBtn) {
    const pre = foldBtn.closest('pre.codeblock');
    if (pre) {
      const folded = pre.classList.toggle('folded');
      foldBtn.querySelector('.cb-txt').textContent = folded ? '展开' : '收起';
    }
  }
});

/* ============ 输入框上边缘拖拽调高（小窗/最小化恢复后均可拖，持久化） ============ */
const dragHandle = document.getElementById('dragHandle');
const inputArea = document.getElementById('inputArea');
let dragState = null;

function inputHBounds() {
  // 动态可用高度：容器高 − 头部 − 大脑区 − 输入区其余部分(工具栏/抓手)，再留 24px 聊天缝
  const hdr = document.querySelector('.header') ? document.querySelector('.header').offsetHeight : 44;
  const brain = document.getElementById('brain-slot')
    ? document.getElementById('brain-slot').offsetHeight : 0;
  const other = inputArea.offsetHeight - input.offsetHeight;  // 抓手/工具栏/内边距
  const maxBySpace = container.clientHeight - hdr - brain - other - 24;
  return { minH: 44, maxH: Math.max(60, maxBySpace) };
}
function applyInputH(h) {
  const b = inputHBounds();
  h = Math.max(b.minH, Math.min(b.maxH, Math.round(h)));
  input.style.height = h + 'px';
  return h;
}
function onInputDragMove(e) {
  if (!dragState) return;
  dragState.h = applyInputH(dragState.h + (dragState.y - e.clientY));  // 向上拖变高
  dragState.y = e.clientY;
}
function endInputDrag() {
  if (!dragState) return;
  localStorage.setItem('wasily_input_h', String(input.offsetHeight));
  dragState = null;
  dragHandle.classList.remove('dragging');
  document.body.style.userSelect = '';
  document.removeEventListener('pointermove', onInputDragMove, true);
  document.removeEventListener('pointerup', endInputDrag, true);
  document.removeEventListener('pointercancel', endInputDrag, true);
}
dragHandle.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  dragState = { y: e.clientY, h: input.offsetHeight };
  dragHandle.classList.add('dragging');
  document.body.style.userSelect = 'none';
  // document 级监听：拖出抓手/经过其他元素（含最小化恢复边缘场景）都能连续
  document.addEventListener('pointermove', onInputDragMove, true);
  document.addEventListener('pointerup', endInputDrag, true);
  document.addEventListener('pointercancel', endInputDrag, true);
});
// 恢复上次高度；窗口尺寸变化（含最小化→恢复）后重新钳制到当前可用范围
const savedInputH = parseInt(localStorage.getItem('wasily_input_h') || '0', 10);
if (savedInputH >= 44) applyInputH(savedInputH);
window.addEventListener('resize', () => applyInputH(input.offsetHeight));

/* ============ ↑/↓ 翻阅历史输入 ============ */
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    sendMessage();
    return;
  }
  if (e.key === 'ArrowUp' && inputHist.length) {
    // 光标在第一行时才翻阅历史，避免影响多行编辑
    const before = input.value.slice(0, input.selectionStart);
    if (before.indexOf('\n') === -1) {
      e.preventDefault();
      if (histIdx === -1) { histDraft = input.value; histIdx = inputHist.length - 1; }
      else if (histIdx > 0) histIdx--;
      input.value = inputHist[histIdx] || '';
      input.selectionStart = input.selectionEnd = input.value.length;
    }
  } else if (e.key === 'ArrowDown' && histIdx !== -1) {
    const after = input.value.slice(input.selectionEnd);
    if (after.indexOf('\n') === -1) {
      e.preventDefault();
      histIdx++;
      if (histIdx >= inputHist.length) { histIdx = -1; input.value = histDraft; }
      else input.value = inputHist[histIdx];
      input.selectionStart = input.selectionEnd = input.value.length;
    }
  }
});

/* ============ 历史输入面板（搜索 + 重新填入） ============ */
const ihOverlay = document.getElementById('ihOverlay');
const ihList = document.getElementById('ihList');
const ihSearch = document.getElementById('ihSearch');

function openInputHistPanel() {
  ihOverlay.classList.add('show');
  ihSearch.value = '';
  renderInputHist();
  setTimeout(() => ihSearch.focus(), 50);
}
function renderInputHist() {
  const kw = ihSearch.value.trim().toLowerCase();
  ihList.innerHTML = '';
  const arr = inputHist.map((t, i) => ({ t: t, i: i })).reverse()
    .filter(x => !kw || x.t.toLowerCase().indexOf(kw) !== -1);
  if (!arr.length) {
    ihList.innerHTML = '<div class="ih-empty">' + (kw ? '没有匹配的历史输入' : '暂无历史输入') + '</div>';
    return;
  }
  arr.forEach(x => {
    const item = document.createElement('div');
    item.className = 'ih-item';
    item.innerHTML = '<span class="ih-txt"></span><button class="ih-del" title="删除"><i data-icon="trash" class="ico"></i></button>';
    item.querySelector('.ih-txt').textContent = x.t;
    item.querySelector('.ih-txt').onclick = () => {
      input.value = x.t;
      ihOverlay.classList.remove('show');
      input.focus();
      input.selectionStart = input.selectionEnd = input.value.length;
    };
    item.querySelector('.ih-del').onclick = (e) => {
      e.stopPropagation();
      inputHist.splice(x.i, 1);
      try { localStorage.setItem(IH_KEY, JSON.stringify(inputHist)); } catch (err) {}
      renderInputHist();
    };
    ihList.appendChild(item);
  });
}
ihSearch.addEventListener('input', renderInputHist);
document.getElementById('ihClose').onclick = () => ihOverlay.classList.remove('show');
document.getElementById('ihClear').onclick = () => {
  inputHist = [];
  localStorage.removeItem(IH_KEY);
  renderInputHist();
};
ihOverlay.addEventListener('click', (e) => { if (e.target === ihOverlay) ihOverlay.classList.remove('show'); });

/* ============ 导出 / 清空对话 ============ */
function exportChat() {
  const data = loadStore();
  if (!data.length) { toast('暂无对话可导出'); return; }
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  a.href = URL.createObjectURL(blob);
  a.download = 'wasily_chat_' + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate())
    + '_' + pad(d.getHours()) + pad(d.getMinutes()) + '.json';
  a.click();
  URL.revokeObjectURL(a.href);
  toast('已导出 ' + data.length + ' 条对话');
}
async function clearChat() {
  try {
    await fetch('/clear_session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    });
  } catch (e) {}
  chat.innerHTML = '';
  localStorage.removeItem(LS_KEY);
  // 新对话换新会话 id，避免服务端把旧上下文续进新对话，也让断点续传提示自然失效
  sessionId = 'session_' + Math.random().toString(36).substr(2, 9);
  try { localStorage.setItem('wasily_chat_sid', sessionId); } catch (e) {}
  syncChatEmpty();
  toast('对话已清空');
}

/* ============ 语音输入：MediaRecorder → /stt（faster-whisper 本地识别） ============ */
let mediaRec = null, recChunks = [], recording = false;
const MIC_ICO = '<i data-icon="microphone" class="ico"></i>';
const MIC_STOP_ICO = '<i data-icon="player-stop" class="ico"></i>';
const MIC_WAIT_ICO = '<i data-icon="loader-2" class="ico spin"></i>';

async function toggleRecord() {
  if (recording) {                      // 停止并识别
    recording = false;
    micBtn.classList.remove('recording');
    micBtn.innerHTML = MIC_WAIT_ICO;
    try { mediaRec.stop(); } catch (e) {}
    return;
  }
  // 浏览器 SpeechRecognition 兜底（POST /stt 不可用时）
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    if (SR) return startSpeechRec(SR);
    addMsg('ai', '⚠️ 当前环境不支持语音输入（无 MediaRecorder / SpeechRecognition）');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recChunks = [];
    mediaRec = new MediaRecorder(stream);
    mediaRec.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
    mediaRec.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      micBtn.innerHTML = MIC_WAIT_ICO;
      try {
        const blob = new Blob(recChunks, { type: 'audio/webm' });
        const res = await fetch('/stt?fmt=webm', { method: 'POST', body: blob });
        const j = await res.json();
        if (j.text && j.text.trim()) {
          input.value = j.text.trim();
          toast('识别完成，自动发送');
          sendMessage();          // 语音输入 → 自动发送 → AI 语音回复，形成完整语音对话
        } else if (SR) {
          micBtn.innerHTML = MIC_ICO;
          toast('本地识别无结果，改用浏览器语音识别');
          startSpeechRec(SR);
          return;
        } else {
          addMsg('ai', '⚠️ 没有识别到内容' + (j.error ? '：' + j.error : ''));
        }
      } catch (e) {
        if (SR) {
          micBtn.innerHTML = MIC_ICO;
          toast('本地识别失败，改用浏览器语音识别');
          startSpeechRec(SR);
          return;
        }
        addMsg('ai', '⚠️ 语音识别失败：' + e.message + '\n可对我说"安装 faster-whisper"启用本地识别。');
      }
      micBtn.innerHTML = MIC_ICO;
    };
    mediaRec.start();
    recording = true;
    micBtn.classList.add('recording');
    micBtn.innerHTML = MIC_STOP_ICO;
  } catch (e) {
    const SR2 = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR2) { startSpeechRec(SR2); return; }
    addMsg('ai', '⚠️ 无法访问麦克风：' + e.message);
  }
}
function startSpeechRec(SR) {
  try {
    const rec = new SR();
    rec.lang = 'zh-CN';
    rec.interimResults = false;
    micBtn.classList.add('recording');
    micBtn.innerHTML = MIC_STOP_ICO;
    rec.onresult = (e) => {
      const t = e.results[0] && e.results[0][0] && e.results[0][0].transcript;
      if (t) { input.value = t.trim(); toast('识别完成，自动发送'); sendMessage(); }
      else addMsg('ai', '⚠️ 浏览器语音识别没有结果');
    };
    rec.onerror = (e) => addMsg('ai', '⚠️ 浏览器语音识别失败：' + (e.error || '未知错误'));
    rec.onend = () => { micBtn.classList.remove('recording'); micBtn.innerHTML = MIC_ICO; };
    rec.start();
  } catch (e) {
    micBtn.classList.remove('recording');
    micBtn.innerHTML = MIC_ICO;
    addMsg('ai', '⚠️ 语音输入不可用：' + e.message);
  }
}
micBtn.addEventListener('click', toggleRecord);

/* ============ 工具确认卡片（轮询 /agent_status 发现待批准操作） ============ */
let shownActionId = null;

// 轮询共享退避 + 单飞保护：服务不可达时全局降速，避免请求堆积耗尽连接池
let _pollBackoff = 0, _pollFails = 0;
const _pollInflight = new Set();
function pollOk()  { _pollFails = 0; _pollBackoff = 0; }
function pollFail(){ _pollFails++; _pollBackoff = Math.min(2000 * Math.pow(2, Math.min(_pollFails, 4)), 15000); }

async function pollStatus() {
  let next = 1500;
  if (!_pollInflight.has('status')) {
    _pollInflight.add('status');
    try {
      const res = await fetch('/agent_status');
      const st = await res.json();
      // 自愈：本页没有在生成，但全局状态仍是 thinking 且迟迟不更新（后端异常分支漏复位），
      // 直接当作已完成，避免任务条永久卡在「思考中」，挡住下一轮输入
      if (st && st.state === 'thinking' && !lockGenerating
          && st.since && (Date.now() / 1000 - Number(st.since)) > 150) {
        taskStrip.set({ state: 'idle' });
      } else {
        taskStrip.set(st);
      }
      const pa = st.pending_action;
      if (pa && pa.id !== shownActionId) { shownActionId = pa.id; addConfirmCard(pa); }
      else if (!pa) { shownActionId = null; }
      pollOk();
      fetchTokenStats();
      next = 1500;
    } catch (e) {
      pollFail(); next = _pollBackoff;
    } finally { _pollInflight.delete('status'); }
  }
  setTimeout(pollStatus, _pollBackoff || next);
}

/* ============ Token 用量统计条（GET /token_stats，随 /agent_status 同节奏刷新） ============ */
const tokenBar = document.getElementById('tokenBar');
const tokenDetail = document.getElementById('tokenDetail');
const tokCur = document.getElementById('tokCur');
const tokTotal = document.getElementById('tokTotal');
tokenBar.addEventListener('click', () => {
  tokenBar.classList.toggle('open');
  tokenDetail.classList.toggle('show');
});
function tokFmt(n) {
  n = +n || 0;
  return n >= 10000 ? (n / 1000).toFixed(1) + 'k' : String(n);
}
async function fetchTokenStats() {
  try {
    const res = await fetch('/token_stats');
    if (!res.ok) return;
    const st = await res.json();
    const c = st.current || {}, t = st.total || {};
    tokCur.innerHTML = (c.phase && c.phase !== 'idle')
      ? `<span class="tok-phase">${c.phase}</span> ↑<b>${tokFmt(c.prompt_tokens)}</b> ↓<b>${tokFmt(c.gen_tokens)}</b>`
      : `本轮 ↑<b>${tokFmt(c.prompt_tokens)}</b> ↓<b>${tokFmt(c.gen_tokens)}</b>`;
    tokTotal.innerHTML = `累计 <b>${tokFmt(t.tokens)}</b> · <b>${t.turns || 0}</b> 轮`;
    const ss = st.sessions || [];
    tokenDetail.innerHTML = ss.length
      ? ss.map(s =>
          `<div class="td-row"><span class="td-id">${s.session_id}</span>` +
          `<span><b>${s.turns || 0}</b> 轮</span>` +
          `<span>↑<b>${tokFmt(s.prompt_tokens)}</b> ↓<b>${tokFmt(s.gen_tokens)}</b></span>` +
          `<span class="td-last">${s.last || ''}</span></div>`).join('')
      : '<div class="td-row">暂无分会话数据</div>';
  } catch (e) { /* 接口未就绪时静默，下轮重试 */ }
}

function addConfirmCard(pa) {
  const card = document.createElement('div');
  card.className = 'confirm-card';
  card.innerHTML =
    `<div class="c-title">⚠️ AI 请求执行敏感操作</div>
     <div class="c-desc">工具：<b>${pa.tool}</b>　确认后才会执行，拒绝会通知 AI 改用其他方式。</div>
     <pre>${JSON.stringify(pa.args, null, 2)}</pre>
     <div class="c-btns">
       <button class="allow">✓ 允许执行</button>
       <button class="deny">✗ 拒绝</button>
     </div>
     <div class="c-state" style="display:none"></div>`;
  const [allowBtn, denyBtn] = card.querySelectorAll('button');
  const state = card.querySelector('.c-state');
  const decide = async (allow) => {
    allowBtn.disabled = denyBtn.disabled = true;
    state.style.display = 'block';
    state.textContent = allow ? '已允许，AI 正在执行…' : '已拒绝';
    try {
      await fetch('/tool/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_id: pa.id, allow: allow })
      });
    } catch (e) { state.textContent = '提交失败：' + e.message; }
  };
  allowBtn.onclick = () => decide(true);
  denyBtn.onclick = () => decide(false);
  chat.appendChild(card);
  chat.scrollTop = chat.scrollHeight;
}

/* ============ 工程开发智能体：开发计划卡片 ============
   模型输出 ```plan 计划块 → 服务端登记后随响应下发 plan 对象；
   本函数渲染卡片（目标 + 步骤清单 + 确认/取消）。
   确认后 POST /plan/confirm 并轮询 /plan/status 实时刷新步骤状态，
   计划完结时把服务端总结作为一条 AI 消息收尾。 */
function planEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function addPlanCard(plan) {
  const ICO = { pending: '⏳', running: '🔄', done: '✅', failed: '❌' };
  const people = plan.people || [];
  const card = document.createElement('div');
  card.className = 'plan-card';
  card.innerHTML =
    `<div class="p-title">🧠 任务编排（待确认）</div>
     <div class="p-goal">目标：${planEsc(plan.goal)}</div>
     <div class="p-prog"><div class="p-prog-bar"><i></i></div><span class="p-prog-txt">完成 0/0</span></div>
     <div class="p-people">${people.map(p =>
       `<span class="p-person" title="${planEsc(p.duty || '')}">
          <span class="av">${p.emoji || '👤'}</span>${planEsc(p.name)}
          <span class="du">${planEsc(p.agent || '')}</span>
        </span>`).join('')}</div>
     <div class="p-steps"></div>
     <div class="p-btns">
       <button class="go">✓ 确认执行</button>
       <button class="cancel">✗ 取消</button>
     </div>
     <div class="p-state" style="display:none"></div>`;
  const stepsEl = card.querySelector('.p-steps');
  const titleEl = card.querySelector('.p-title');
  const state = card.querySelector('.p-state');
  const [goBtn, cancelBtn] = card.querySelectorAll('button');

  function renderSteps(steps) {
    stepsEl.innerHTML = steps.map(s => {
      const tags = [];
      if (s.agent) tags.push(`<span class="p-tag agent">${planEsc(s.emoji || '')}${planEsc(s.agent)}</span>`);
      if (s.skill) tags.push(`<span class="p-tag skill">⚡ ${planEsc(s.skill)}</span>`);
      (s.tools_state || []).forEach(t => tags.push(
        `<span class="p-tag tool ${planEsc(t.status)}" title="${planEsc(t.detail || '')}">${planEsc(t.tool)}</span>`));
      if (!s.tools_state && (s.tools || []).length) {
        s.tools.forEach(n => tags.push(`<span class="p-tag tool">${planEsc(n)}</span>`));
      }
      return `<div class="p-step ${s.status}">
         <span class="p-ico">${ICO[s.status] || '⏳'}</span>
         <div>
           <span class="p-kind">${planEsc(s.kind || '任务')}</span><span class="p-name">${s.id}. ${planEsc(s.title)}</span>
           <span class="p-owner">${planEsc(s.agent_name || s.person || '')}</span>
           ${s.detail ? `<div class="p-det">${planEsc(s.detail)}</div>` : ''}
           ${tags.length ? `<div class="p-tags">${tags.join('')}</div>` : ''}
         </div>
       </div>`;
    }).join('');
    // 进度条：完成任务数 / 总任务数
    const total = steps.length;
    const doneN = steps.filter(s => s && s.status === 'done').length;
    const bar = card.querySelector('.p-prog-bar i');
    const ptxt = card.querySelector('.p-prog-txt');
    if (bar) bar.style.width = (total ? Math.round(doneN / total * 100) : 0) + '%';
    if (ptxt) ptxt.textContent = '完成 ' + doneN + '/' + total;
  }
  renderSteps(plan.steps);
  chat.appendChild(card);
  chat.scrollTop = chat.scrollHeight;

  const decide = async (action) => {
    goBtn.disabled = cancelBtn.disabled = true;
    try {
      await fetch('/plan/confirm', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_id: plan.id, action: action })
      });
    } catch (e) { state.textContent = '提交失败：' + e.message; }
  };

  cancelBtn.onclick = async () => {
    await decide('cancel');
    titleEl.textContent = '🧠 任务编排（已取消）';
    state.style.display = 'block';
    state.textContent = '已取消。可继续对话调整需求，让我重新编排任务。';
  };

  goBtn.onclick = async () => {
    await decide('approve');
    titleEl.textContent = '🧠 任务编排（执行中）';
    state.style.display = 'block';
    state.textContent = '已确认，各角色开始按任务分工执行…';
    pollPlan(plan.id);
  };

  async function pollPlan(pid) {
    try {
      const res = await fetch('/plan/status?id=' + pid);
      const p = await res.json();
      if (p && p.steps) renderSteps(p.steps);
      if (p.status === 'done' || p.status === 'failed' || p.status === 'canceled') {
        titleEl.textContent = p.status === 'done' ? '🧠 任务编排（已完成）'
          : p.status === 'failed' ? '🧠 任务编排（有任务失败）' : '🧠 任务编排（已取消）';
        state.textContent = p.status === 'done' ? '全部任务执行完成' : '执行结束，详情见下方总结';
        if (p.summary) addMsg('ai', p.summary);
        return;
      }
    } catch (e) { /* 接口未就绪时下轮重试 */ }
    setTimeout(() => pollPlan(pid), 2000);
  }
}

/* ============ AI 提示词小按钮（✨ 已移到工具栏，逻辑不变） ============ */
const sparkBtn = document.getElementById('sparkBtn');
let sparkBusy = false;

sparkBtn.onclick = async () => {
  if (sparkBusy) return;
  const draft = input.value.trim();
  // 收集最近6条对话作为上下文
  const ctx = [];
  document.querySelectorAll('#chat .msg').forEach(el => {
    const who = el.classList.contains('user') ? '用户' : 'AI';
    const t = el.textContent.replace(/\s+/g, ' ').trim();
    if (t) ctx.push(who + ': ' + t.slice(0, 200));
  });
  sparkBusy = true;
  sparkBtn.classList.add('busy');
  try {
    // 默认走规则优化的基础版（毫秒级）；高级模式可在设置中切换为本地模型改写
    const res = await fetch('/v1/prompt/optimize', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: draft, context: ctx.slice(-6).join('\n') })
    });
    const data = await res.json();
    const out = (data.text || '').trim();
    if (out) {
      input.value = out;
      sparkBtn.classList.add('done');
      setTimeout(() => sparkBtn.classList.remove('done'), 1500);
      if (data.fallback) toast('模型改写不可用，已使用基础优化');
    } else if (data.error) {
      toast('优化失败：' + data.error);
    }
  } catch (e) {
    sparkBtn.innerHTML = '<i data-icon="alert-triangle" class="ico"></i>';
    setTimeout(() => { sparkBtn.innerHTML = '<i data-icon="sparkles" class="ico"></i>'; }, 1200);
  }
  sparkBusy = false;
  sparkBtn.classList.remove('busy');
  input.focus();
};

/* ============ 历史对话抽屉（服务端 /history/list） ============ */
const drawer = document.getElementById('historyDrawer');
const historyList = document.getElementById('historyList');
let hOffset = 0;

function openHistoryDrawer() {
  drawer.classList.add('show');
  hOffset = 0;
  historyList.innerHTML = '<div class="h-empty">加载中…</div>';
  loadHistory(true);
  loadIdeSessions();   // 同时列出 IDE 里的对话，点一下即可跨端接着聊
}
document.getElementById('hClose').onclick = () => drawer.classList.remove('show');
document.getElementById('hRefresh').onclick = () => {
  hOffset = 0; loadHistory(true); loadIdeSessions();
};
let searchTimer = null;
document.getElementById('hSearch').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { hOffset = 0; loadHistory(true); }, 350);
});

async function loadHistory(reset) {
  const kw = document.getElementById('hSearch').value.trim();
  const q = `limit=40&offset=${hOffset}` + (kw ? `&keyword=${encodeURIComponent(kw)}` : '');
  try {
    const res = await fetch('/history/list?' + q);
    const data = await res.json();
    if (reset) historyList.innerHTML = '';
    const empty = historyList.querySelector('.h-empty');
    if (empty) empty.remove();
    if (!data.records || data.records.length === 0) {
      historyList.innerHTML = `<div class="h-empty">${hOffset ? '没有更多了' : '暂无记录'}</div>`;
      return;
    }
    for (const r of data.records) {
      const item = document.createElement('div');
      item.className = 'h-item';
      const timeStr = new Date((r.ts || 0) * 1000).toLocaleString('zh-CN');
      const type = r.type === 'chat' ? (r.role === 'user' ? '用户' : 'AI') : (r.type || '记录');
      item.innerHTML =
        `<div class="h-meta"><span class="tag">${type}</span><span>${timeStr}</span><span>${r.session_id || ''}</span></div>
         <div class="h-body"></div>
         <button class="h-del" title="删除"><i data-icon="trash" class="ico"></i></button>`;
      item.querySelector('.h-body').textContent = (r.content || '').slice(0, 220);
      item.querySelector('.h-del').onclick = async (e) => {
        e.stopPropagation();
        await fetch('/history/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids: [r.id] })
        });
        item.remove();
      };
      historyList.appendChild(item);
    }
    hOffset += data.records.length;
    const more = historyList.querySelector('.h-more');
    if (more) more.remove();
    if (hOffset < (data.total || 0)) {
      const btn = document.createElement('button');
      btn.className = 'h-more';
      btn.textContent = `加载更多（${hOffset}/${data.total}）`;
      btn.onclick = () => loadHistory(false);
      historyList.appendChild(btn);
    }
  } catch (e) {
    historyList.innerHTML = '<div class="h-empty">加载失败: ' + e.message + '</div>';
  }
}

/* ============ 消息渲染（AI 消息走 Markdown + Agent 执行过程风格，用户消息保留 pre-wrap + 图片） ============ */
/* 工具分类：文件读取类 / 命令执行类 / 记忆类 */
function isFileTool(t) { return /file|read|list|search|dir|folder/i.test(t) && !/shell|exec|python|cmd|command|run/i.test(t); }
function isCmdTool(t)  { t = String(t || ''); return !/run_web_page/i.test(t) && /shell|exec|python|cmd|command|run/i.test(t); }
function isMemTool(t)  { return /memory|memo|remember/i.test(t); }

/* 从 steps 提取本次引用过的文件路径（去重、保序，最多 12 条） */
function collectRefFiles(steps) {
  const out = [], seen = {};
  (steps || []).forEach(s => {
    if (!s || !s.tool || isCmdTool(s.tool) || /^run_web_page$/i.test(s.tool)) return;
    const a = s.args || {};
    let p = String(a.path || a.file || a.filepath || a.dir || '').trim();
    if (!p || !/[\\/]/.test(p)) return;   // 跳过纯关键词/命令
    if (seen[p]) return;
    seen[p] = 1; out.push(p);
  });
  return out.slice(0, 12);
}

/* 点击参考内容 → 请求 IDE 桥打开该文件；IDE 未连接时退化为复制路径 */
async function openFileInIde(p) {
  try {
    const r = await fetch(IDE_BRIDGE + '/open_file', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: p, line: 0 })
    });
    if (r.ok) { toast('已在 IDE 中打开'); return; }
  } catch (e) {}
  try { await navigator.clipboard.writeText(p); toast('IDE 未连接，已复制路径'); }
  catch (e) { toast('IDE 未连接'); }
}

/* ===== 思考过程时间线（对齐 Trae：竖线节点 + 逐条步骤） ===== */
function tfBase(p) { return String(p || '').split(/[\\/]/).filter(Boolean).pop() || ''; }
function tfShort(s, n) {
  s = String(s || '').replace(/\s+/g, ' ').trim();
  return s.length > n ? s.slice(0, n) + '…' : s;
}
function tfStepInfo(s) {
  const a = s.args || {};
  const res = String(s.result || '');
  const miss = /未找到|没有找到|no match|not found/i.test(res) && res.length < 80;
  if (s.think) return { ico: 'think', label: '思考', det: s.think };
  const t = s.tool || '';
  // 打开网页是确定性动作，不是终端命令：展示成「打开网页 xxx」+ 结果摘要
  if (/^run_web_page$/i.test(t)) {
    let det = res, extra = [];
    try {
      const o = JSON.parse(res);
      if (o && typeof o === 'object') {
        if (o.message) extra.push(String(o.message));
        if (o.port) extra.push('端口 ' + o.port);
        if (o.file && !o.message) extra.push('文件 ' + o.file);
        if (o.error) extra.push('失败：' + o.error);
        det = extra.join('\n') || res;
      }
    } catch (e) {}
    return { ico: 'file', label: '打开网页 ' + (tfBase(a.path || a.url || '') || ''), det: det };
  }
  if (isCmdTool(t)) {
    return { ico: 'cmd', label: '命令已执行 ' + tfShort(a.command || a.code || '', 46), det: res };
  }
  if (/search|grep|find/i.test(t)) {
    const m = res.match(/(\d+)\s*(行|条|处)/);
    const q = tfShort(a.query || a.pattern || a.keyword || '', 30);
    return {
      ico: 'search', tag: miss ? '未找到结果' : '',
      label: (m && !miss) ? ('找到 ' + m[1] + ' 行 ' + q) : ('搜索 ' + q),
      det: res
    };
  }
  if (/write|edit|copy|move|delete|apply/i.test(t)) {
    return { ico: 'edit', label: '已修改 ' + (tfBase(a.path || a.dst || '') || t), det: res };
  }
  if (isFileTool(t)) {
    return {
      ico: 'file',
      label: '读取 ' + (tfBase(a.path || a.dir || '') || tfShort(a.query || '', 24) || t),
      det: (a.path || a.dir || '') + (res ? '\n' + res : '')
    };
  }
  return { ico: 'tool', label: t, det: JSON.stringify(a || {}) + (res ? '\n' + res : '') };
}
/* 时间线工具图标：与 Trae 一致的内联线性图标（思考/终端/搜索/编辑/文件/通用） */
const TF_ICONS = {
  think: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg>',
  cmd: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5 7l5 5l-5 5M13 17h6"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z"/></svg>',
  file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  tool: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/></svg>'
};
function tfMakeRow(info) {
  const row = document.createElement('div');
  row.className = 'tf-row';
  const line = document.createElement('div');
  line.className = 'tf-line';
  line.innerHTML = '<span class="tf-ico">' + (TF_ICONS[info.ico] || info.ico) + '</span>' +
    '<span class="tf-label">' + mdEsc(info.label) + '</span>' +
    (info.tag ? '<span class="tf-tag">' + mdEsc(info.tag) + '</span>' : '') +
    '<span class="tf-arr">›</span>';
  const det = document.createElement('div');
  det.className = 'tf-det';
  det.textContent = info.det || '';
  if (!info.det) line.querySelector('.tf-arr').style.display = 'none';
  line.onclick = () => { row.classList.toggle('open'); };
  row.appendChild(line);
  row.appendChild(det);
  return row;
}
/* 统计一行：与 Trae 截图一致的「已读取 N 个文件，搜索 N 个网页，执行 N 条命令」 */
function thinkStatsText(steps) {
  const refFiles = collectRefFiles(steps || []);
  const cmd = (steps || []).filter(s => s && s.tool && isCmdTool(s.tool)).length;
  const web = (steps || []).filter(s => s && /^run_web_page$/i.test(s.tool || '')).length;
  const search = (steps || []).filter(s => s && /search|grep|find/i.test(s.tool || '')).length;
  const edit = (steps || []).filter(s => s && FC_EDIT_RE.test(s.tool || '')).length;
  const parts = [];
  if (edit) parts.push('已编辑 ' + edit + ' 个文件');
  if (refFiles.length) parts.push('已读取 ' + refFiles.length + ' 个文件');
  if (search) parts.push('搜索 ' + search + ' 次文件');
  if (web) parts.push('搜索 ' + web + ' 个网页');
  if (cmd) parts.push('执行 ' + cmd + ' 条命令');
  return parts.join('，');
}

/* 最终消息：Trae 式思考卡（折叠行 + 粗体结论 + 灰色统计 + 提示卡）+ 可展开步骤时间线 */
function renderThinkFlow(wrap, steps, ms) {
  steps = steps || [];
  const hasTool = steps.some(s => s && s.tool);
  const box = document.createElement('div');
  box.className = 'think-flow';
  const head = document.createElement('div');
  head.className = 'tf-head';
  head.innerHTML = '<span>思考过程</span>' +
    (ms ? '<span class="tf-dur" style="color:#6f6f6f">' + fmtDur(ms) + '</span>' : '') +
    '<span class="tf-arr">›</span>';
  head.onclick = () => box.classList.toggle('open');
  box.appendChild(head);

  // 结论区（折叠时仍可见）：think 文本逐条作为粗体结论
  const concl = steps.filter(s => s && s.think).map(s => String(s.think).trim()).filter(Boolean);
  if (!concl.length && steps.length) {
    concl.push('我先查看相关文件与运行环境，确认现状后再动手。');
  }
  concl.forEach(t => {
    const p = document.createElement('div');
    p.className = 'tk-concl';
    p.textContent = t;
    box.appendChild(p);
  });
  const statTxt = thinkStatsText(steps);
  if (statTxt) {
    const st = document.createElement('div');
    st.className = 'tk-stat';
    st.innerHTML = mdEsc(statTxt) + ' <span class="arr">›</span>';
    box.appendChild(st);
  }
  if (hasTool) {
    const tip = document.createElement('div');
    tip.className = 'tk-tip';
    tip.innerHTML = '<span class="ic">' + TF_ICONS.think + '</span><span>整理资料结论时，可让我标明每条结论对应的原文位置，方便回查。</span>';
    box.appendChild(tip);
  }

  const body = document.createElement('div');
  body.className = 'tf-body';
  steps.forEach(s => {
    if (!s) return;
    if (!s.think && !s.tool && !s.result) return;   // 旧会话里的空占位步骤不展示
    body.appendChild(tfMakeRow(tfStepInfo(s)));
  });
  if (!body.children.length) body.innerHTML = '<div class="tf-empty">本轮没有可展示的步骤</div>';
  box.appendChild(body);
  wrap.appendChild(box);
  return box;
}

/* 完成后推荐：Trae 胶囊（优先用后端 next_steps，缺失时按工具结果启发式生成） */
function buildNextSteps(content, steps) {
  steps = steps || [];
  const txt = String(content || '');
  const out = [];
  if (/部署|deploy|上线|发布/i.test(txt + JSON.stringify((steps || []).map(s => s.tool || '')))) {
    out.push('好的，部署这个项目 →');
  }
  if (steps.some(s => s && FC_EDIT_RE.test(s.tool || ''))) out.push('看一下这次改了哪些文件 →');
  if (/生成|页面|网站|HTML/i.test(txt)) out.push('继续完善这个页面 →');
  if (!out.length) out.push('继续下一步 →');
  out.push('重新检查一遍结果 →');
  return out.slice(0, 3);
}
function renderNextSteps(wrap, list) {
  if (!list || !list.length) return null;
  const box = document.createElement('div');
  box.className = 'rec-list';
  list.forEach(t => {
    const b = document.createElement('button');
    b.className = 'rec-pill';
    b.innerHTML = '<span>' + mdEsc(t.replace(/\s*→\s*$/, '')) + '</span><span class="rp-arr">→</span>';
    b.onclick = () => {
      try {
        if (typeof input !== 'undefined' && input) {
          input.value = t.replace(/\s*→\s*$/, '');
          input.dispatchEvent(new Event('input'));
          if (typeof send === 'function') send();
        }
      } catch (e) {}
    };
    box.appendChild(b);
  });
  wrap.appendChild(box);
  return box;
}

/* ===== 文件更改卡片（Trae：N 个文件已更改 / +增 -删 / 展开列表 / 打开） ===== */
const FC_EDIT_RE = /file_write|file_edit|file_append|apply_patch|write_file|edit_file|file_delete|file_copy|file_move/i;
function collectFileChanges(steps) {
  const map = new Map();
  (steps || []).forEach(s => {
    if (!s || !s.tool || !FC_EDIT_RE.test(s.tool)) return;
    const a = s.args || {};
    const p = a.path || a.file || a.dst || a.target || '';
    if (!p) return;
    const key = String(p).replace(/\\/g, '/');
    const addText = a.content || a.text || a.new_content || a.new_string || '';
    const delText = a.old_string || a.old_content || a.old || '';
    let add = addText ? String(addText).split('\n').length : 0;
    let del = delText ? String(delText).split('\n').length : 0;
    if (s.tool === 'file_delete') { add = 0; del = del || 1; }
    const cur = map.get(key) || { path: p, add: 0, del: 0 };
    cur.add += add; cur.del += del;
    map.set(key, cur);
  });
  return Array.from(map.values());
}
function fcRel(p) {
  const parts = String(p).replace(/\\/g, '/').split('/').filter(Boolean);
  return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : parts.join('/');
}
function renderFileChanges(wrap, changes) { wrap.appendChild(buildFileCard(changes)); }
function buildFileCard(changes) {
  const totalAdd = changes.reduce((n, c) => n + c.add, 0);
  const totalDel = changes.reduce((n, c) => n + c.del, 0);
  const card = document.createElement('div');
  card.className = 'fc-card';
  const head = document.createElement('div');
  head.className = 'fc-head';
  head.innerHTML = '<span class="fc-ico">' + TF_ICONS.file + '</span>' +
    '<span class="fc-title">' + changes.length + ' 个文件已更改</span>' +
    '<span class="fc-cv">⌄</span>' +
    '<span class="fc-stat">' + (totalAdd ? '<i class="add">+' + totalAdd + '</i>' : '') +
      (totalDel ? '<i class="del">-' + totalDel + '</i>' : '') + '</span>' +
    '<span class="fc-open" title="在 IDE 中打开"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></span>';
  head.onclick = e => {
    if (e.target.closest('.fc-open')) {
      openFileInIde(changes[0].path);
      return;
    }
    card.classList.toggle('open');
  };
  const list = document.createElement('div');
  list.className = 'fc-list';
  changes.forEach(c => {
    const row = document.createElement('div');
    row.className = 'fc-row';
    row.title = c.path;
    row.innerHTML = '<span class="fc-ico">' + TF_ICONS.file + '</span>' +
      '<span class="fc-name">' + mdEsc(fcRel(c.path)) + '</span>' +
      '<span class="fc-stat">' + (c.add ? '<i class="add">+' + c.add + '</i>' : '') +
        (c.del ? '<i class="del">-' + c.del + '</i>' : '') + '</span>';
    row.onclick = () => openFileInIde(c.path);
    list.appendChild(row);
  });
  card.appendChild(head);
  card.appendChild(list);
  return card;
}

/* Agent 风格工具执行区：摘要行(可展开明细) + 命令块 + 完成状态行 */
function renderAgentTools(wrap, steps) {
  const fileSteps = steps.filter(s => isFileTool(s.tool || ''));
  const cmdSteps  = steps.filter(s => isCmdTool(s.tool || ''));
  const memSteps  = steps.filter(s => isMemTool(s.tool || ''));
  const otherSteps = steps.filter(s => !isFileTool(s.tool || '') && !isCmdTool(s.tool || '') && !isMemTool(s.tool || ''));

  const parts = [];
  if (fileSteps.length)  parts.push('已读取 ' + fileSteps.length + ' 个文件');
  if (cmdSteps.length)   parts.push('执行 ' + cmdSteps.length + ' 条命令');
  if (memSteps.length)   parts.push('已记忆 ' + memSteps.length + ' 条');
  if (otherSteps.length) parts.push('调用 ' + otherSteps.length + ' 个工具');

  const FILE_ICON = '<svg width="12" height="12" viewBox="0 0 16 16"><path fill="#4daafc" d="M3 1h7l3 3v11H3z"/><path fill="#1e1e1e" d="M10 1v3h3z"/></svg>';

  const tools = document.createElement('div');
  tools.className = 'agent-tools';
  const sum = document.createElement('div');
  sum.className = 'agent-tools-sum';
  sum.innerHTML = '<span class="fico">' + FILE_ICON + '</span><span>' +
    mdEsc(parts.join('，')) + '</span> <span class="arr">▸</span>';
  const detail = document.createElement('div');
  detail.className = 'agent-tools-detail';
  detail.innerHTML = steps.map(s => {
    const a = s.args || {};
    let arg = a.path || a.file || a.command || a.code || a.query || a.keyword || JSON.stringify(a);
    arg = String(arg || '');
    if (arg.length > 120) arg = arg.slice(0, 120) + '…';
    return '<div class="td-row"><span class="td-tool">' + mdEsc(s.tool || 'tool') + '</span>' +
      (arg ? ' <span class="td-arg">' + mdEsc(arg) + '</span>' : '') + '</div>';
  }).join('');
  sum.onclick = () => {
    tools.classList.toggle('open');
    sum.querySelector('.arr').textContent = tools.classList.contains('open') ? '▾' : '▸';
  };
  tools.appendChild(sum);
  tools.appendChild(detail);
  wrap.appendChild(tools);

  // 每条 shell/python 调用一个深色命令块
  cmdSteps.forEach(s => {
    const a = s.args || {};
    const cmd = String(a.command || a.code || JSON.stringify(a));
    const blk = document.createElement('div');
    blk.className = 'cmd-block';
    blk.innerHTML = '<div class="cmd-head"><i data-icon="player-play" class="ico" style="font-size:12px;margin-right:5px"></i>命令已执行</div><code>$ ' + mdEsc(cmd) + '</code>';
    wrap.appendChild(blk);
  });

  // 底部完成状态行
  const done = document.createElement('div');
  done.className = 'agent-done';
  done.innerHTML = '<span class="ok">✓</span> 任务已完成！';
  wrap.appendChild(done);
}

/* TRAE 风格：AI 消息下方 5 个操作（复制 / 重新生成 / 赞 / 踩 / 更多） */
function buildMsgActions(aiMsg, rawText) {
  const bar = document.createElement('div');
  bar.className = 'msg-actions';
  const mk = (icon, title, fn) => {
    const b = document.createElement('button');
    b.title = title;
    b.innerHTML = '<i data-icon="' + icon + '" class="ico"></i>';
    b.addEventListener('click', fn);
    return b;
  };
  const copyBtn = mk('copy', '复制', async () => {
    const txt = mdToPlain(rawText || '');
    try { await navigator.clipboard.writeText(txt); }
    catch (e) {
      const ta = document.createElement('textarea');
      ta.value = txt; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (_) {}
      ta.remove();
    }
    copyBtn.classList.add('picked');
    setTimeout(() => copyBtn.classList.remove('picked'), 1000);
    toast('已复制');
  });
  const regenBtn = mk('repeat', '重新生成', () => {
    if (lockByOther) { toast('另一个对话窗口正在使用输入'); return; }
    let prev = aiMsg.previousElementSibling;
    while (prev && !(prev.classList && prev.classList.contains('user'))) {
      prev = prev.previousElementSibling;
    }
    if (!prev) { toast('没有可重新生成的提问'); return; }
    const bubbleEl = prev.querySelector('.bubble');
    const txt = bubbleEl ? bubbleEl.textContent.trim() : '';
    if (!txt) { toast('没有可重新生成的提问'); return; }
    aiMsg.remove();
    regenerateReply(txt);
  });
  const upBtn = mk('thumb-up', '赞（这条回答很好，加入训练集）', () => {
    upBtn.classList.add('picked');
    downBtn.classList.remove('down-picked');
    const q = lastUserMsg || '';
    const a = mdToPlain(rawText || '');
    fetch('/lora/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, original: a, corrected: '', vote: 'up' })
    }).catch(() => {});
    toast('已点赞，这条回答已加入训练样本');
  });
  const downBtn = mk('thumb-down', '踩（回答不好，可附上修正）', () => {
    downBtn.classList.add('down-picked');
    upBtn.classList.remove('picked');
    const corrected = prompt('这条回答哪里不对？请输入你期望的回答（留空则只记录踩）：');
    const q = lastUserMsg || '';
    const a = mdToPlain(rawText || '');
    fetch('/lora/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, original: a, corrected: corrected || '', vote: 'down' })
    }).catch(() => {});
    toast(corrected ? '已记录修正，下次训练会用你的版本' : '已记录踩');
  });
  /* 「⋯」：朗读、在历史记录中打开、标记分支收进二级菜单 */
  const moreBtn = document.createElement('button');
  moreBtn.title = '更多';
  moreBtn.className = 'msg-more-btn';
  moreBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><g fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></g></svg>';
  moreBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    openMsgMore(moreBtn, [
      { icon: 'volume', label: '朗读', fn: () => {
          const txt = mdToPlain(rawText || '');
          if (!txt) { toast('没有可朗读的内容'); return; }
          speak(txt);
          toast('正在朗读…');
        } },
      { icon: 'external-link', label: '在历史记录中打开', fn: openHistoryDrawer },
      { icon: 'git-branch', label: '标记分支', fn: () => { toast('已标记分支'); } }
    ]);
  });
  bar.append(copyBtn, regenBtn, upBtn, downBtn, moreBtn);
  return bar;
}

/* 消息操作条「⋯」二级菜单（单例，点击外部关闭） */
let msgMoreEl = null;
function closeMsgMore() {
  if (msgMoreEl) { msgMoreEl.remove(); msgMoreEl = null; }
  document.removeEventListener('click', closeMsgMore);
}
function openMsgMore(anchor, items) {
  if (msgMoreEl) { closeMsgMore(); return; }
  const pop = document.createElement('div');
  pop.className = 'msg-more-pop';
  items.forEach((it) => {
    const b = document.createElement('button');
    b.innerHTML = '<i data-icon="' + it.icon + '" class="ico"></i><span>' + it.label + '</span>';
    b.addEventListener('click', (ev) => { ev.stopPropagation(); closeMsgMore(); it.fn(); });
    pop.appendChild(b);
  });
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8)) + 'px';
  pop.style.top = (r.bottom + 6) + 'px';
  msgMoreEl = pop;
  setTimeout(() => document.addEventListener('click', closeMsgMore), 0);
}

/* ============ 空状态 / 会话态切换（Trae 新任务页两段式） ============ */
const chatHero = document.getElementById('chatHero');
const heroSlot = document.getElementById('heroSlot');
const hintEl = document.querySelector('.hint');
let chatEmpty = null;

function msgExists() {
  if (document.querySelector('#chat .msg')) return true;
  // 工作台模式消息在侧面板里，只有面板真有消息才算会话态
  if (WB_MODE && document.querySelector('#sideDock .sp-msgs .msg')) return true;
  return false;
}
function setChatEmpty(on) {
  if (on === chatEmpty) return;
  chatEmpty = on;
  document.body.classList.toggle('chat-empty', on);
  if (on) {
    heroSlot.appendChild(inputArea);                      // 输入卡居中
  } else if (hintEl && hintEl.parentNode) {
    hintEl.parentNode.insertBefore(inputArea, hintEl);     // 落回底部（原位置）
  }
}
function syncChatEmpty() { setChatEmpty(!msgExists()); }

function leaveHero() {
  if (!chatEmpty) return;
  // 工作台模式下共用输入卡就是空状态卡，等真正发出消息才切到面板视图
  if (WB_MODE) return;
  // DOM 搬迁会让输入框失焦，搬完再补焦点，否则用户打字会中断
  setChatEmpty(false);
  setTimeout(() => { try { input.focus({ preventScroll: true }); } catch (e) { input.focus(); } }, 0);
}
input.addEventListener('focus', leaveHero);
input.addEventListener('input', () => { if (input.value.trim()) leaveHero(); });
document.getElementById('heroChips').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-preset]');
  if (!b) return;
  input.value = b.dataset.preset;
  input.dispatchEvent(new Event('input'));
  leaveHero();
});

/* 助手显示名：本地设置优先，默认 wasily（可在顶部身份卡里编辑） */
function agentDisplayName() {
  try {
    var n = localStorage.getItem('wasily_agent_name');
    if (n && n.trim()) return n.trim();
  } catch (e) {}
  return 'wasily';
}

/* 打开设置页：内嵌面板优先，独立窗口兜底；section=usage 直达用量管理 */
function openSettingsPage(section) {
  var url = '/settings' + (section ? ('#' + section) : '');
  try {
    if (typeof openSidePanel === 'function') { openSidePanel(url, '设置'); return; }
  } catch (e) {}
  window.open(url, '_blank');
}

function applyAgentName(n) {
  n = String(n || '').trim() || 'wasily';
  try { localStorage.setItem('wasily_agent_name', n); } catch (e) {}
  var b = document.getElementById('agentBrand');
  if (b) b.textContent = n;
  document.querySelectorAll('.agent-name').forEach(function (el) { el.textContent = n; });
}

function openNameDialog() {
  var modal = document.getElementById('nameModal');
  var inp = document.getElementById('nameInput');
  if (!modal || !inp) return;
  inp.value = agentDisplayName();
  document.getElementById('nameCount').textContent = inp.value.length;
  modal.classList.add('show');
  modal.style.display = 'flex';
  setTimeout(function () { inp.focus(); inp.select(); }, 30);
}

function closeNameDialog() {
  var modal = document.getElementById('nameModal');
  if (modal) { modal.classList.remove('show'); modal.style.display = 'none'; }
}

async function saveAgentName() {
  var inp = document.getElementById('nameInput');
  var n = (inp && inp.value || '').trim();
  if (!n) { toast('名称不能为空'); return; }
  if (n.length > 20) n = n.slice(0, 20);
  applyAgentName(n);
  closeNameDialog();
  toast('名称已更新');
  try {
    await fetch('/v1/agent/name', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: n })
    });
  } catch (e) {}
}

(function initAgentIdentity() {
  var brand = document.getElementById('agentBrand');
  var chip = document.getElementById('creditChip');
  var inp = document.getElementById('nameInput');
  if (brand) {
    brand.textContent = agentDisplayName();
    brand.addEventListener('click', openNameDialog);
  }
  if (chip) {
    chip.addEventListener('click', function () { openSettingsPage('usage'); });
  }
  if (inp) {
    inp.addEventListener('input', function () {
      document.getElementById('nameCount').textContent = inp.value.length;
    });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); saveAgentName(); }
      if (e.key === 'Escape') closeNameDialog();
    });
  }
  var cancel = document.getElementById('nameCancel');
  if (cancel) cancel.addEventListener('click', closeNameDialog);
  var save = document.getElementById('nameSave');
  if (save) save.addEventListener('click', saveAgentName);
  var modal = document.getElementById('nameModal');
  if (modal) modal.addEventListener('click', function (e) { if (e.target === modal) closeNameDialog(); });
  // 从后端同步已落盘名称（本地未手动改过时）
  try {
    if (!localStorage.getItem('wasily_agent_name')) {
      fetch('/agent/assistant').then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.name) applyAgentName(d.name);
      }).catch(function () {});
    }
  } catch (e) {}
})();

function addMsg(role, content, opts) {
  opts = opts || {};
  const host = opts.host || chat;
  const toolInfo = opts.toolInfo || null;
  const images = opts.images || null;

  const msg = document.createElement('div');
  msg.className = 'msg ' + role;
  if (host === chat && role === 'ai' && !opts.selfcheck && typeof content === 'string') lastAiReply = content;
  if (host === chat && role === 'user' && typeof content === 'string') lastUserMsg = content;

  let bubble;
  if (role === 'ai') {
    // TRAE Agent 风格：全宽容器，无气泡底色
    const wrap = document.createElement('div');
    wrap.className = 'agent-msg';

    // 标题行：24px 圆形头像 + 绿色 Agent 字样
    const head = document.createElement('div');
    head.className = 'agent-head';
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.innerHTML = aiAvatarContent();
    head.appendChild(avatar);
    const nameEl = document.createElement('span');
    nameEl.className = 'agent-name';
    nameEl.textContent = opts.agentName || agentDisplayName();
    head.appendChild(nameEl);
    wrap.appendChild(head);

    // 思考过程（TRAE 风格：think 段落与累计统计胶囊按时间序交错）
    // 摘要行固定显示「任务耗时 / 已思考」，具体步骤默认折叠，点摘要展开
    const steps = (toolInfo && toolInfo.steps) || [];
    const thinks = steps.filter(s => s && s.think).map(s => s.think);
    const ms = Math.max(0, Number(opts.ms) || 0);

    // 元数据行（Trae）：已读取 N 个文件 · 执行 N 条命令 · 耗时 N 秒
    const refFiles = collectRefFiles(steps);
    const cmdCount = steps.filter(s => s && s.tool && isCmdTool(s.tool)).length;
    const webCount = steps.filter(s => s && /^run_web_page$/i.test(s.tool || '')).length;
    const metaParts = [];
    if (refFiles.length) metaParts.push('已读取 ' + refFiles.length + ' 个文件');
    if (cmdCount) metaParts.push('执行 ' + cmdCount + ' 条命令');
    if (webCount) metaParts.push('打开 ' + webCount + ' 个网页');
    // 时长只出现在下面的「任务耗时 / 已思考」摘要里，这里不重复
    if (metaParts.length) {
      const meta = document.createElement('div');
      meta.className = 'agent-meta';
      meta.textContent = metaParts.join(' · ');
      wrap.appendChild(meta);
    }

    // 参考内容：本次读取/搜索过的文件，点击让 IDE 打开
    if (refFiles.length) {
      const refs = document.createElement('div');
      refs.className = 'agent-refs';
      const rh = document.createElement('div');
      rh.className = 'agent-refs-head';
      rh.innerHTML = '<span>参考内容 ' + refFiles.length + '</span><span class="arr">›</span>';
      rh.onclick = () => { refs.classList.toggle('open'); };
      const rb = document.createElement('div');
      rb.className = 'agent-refs-body';
      rb.innerHTML = refFiles.map(p =>
        '<div class="rf-item" data-path="' + mdEsc(p).replace(/"/g, '&quot;') + '">' +
        '<span class="rf-name">' + mdEsc(String(p).split(/[\\/]/).pop()) + '</span>' +
        '<span class="rf-path">' + mdEsc(p) + '</span></div>').join('');
      rb.addEventListener('click', e => {
        const it = e.target.closest && e.target.closest('.rf-item');
        if (it && it.dataset.path) openFileInIde(it.dataset.path);
      });
      refs.appendChild(rh);
      refs.appendChild(rb);
      wrap.appendChild(refs);
    }

    const fileChanges = collectFileChanges(steps);
    if (fileChanges.length) renderFileChanges(wrap, fileChanges);
    if (steps.length || ms) renderThinkFlow(wrap, steps, ms);

    // 正文：完整 Markdown 渲染
    bubble = document.createElement('div');
    bubble.className = 'bubble';
    const md = document.createElement('div');
    md.className = 'md';
    md.innerHTML = mdRender(content);
    bubble.appendChild(md);
    wrap.appendChild(bubble);

    // 旧版兼容：没有步骤明细但带 result 文本时，仍显示一张工具卡
    if (toolInfo && toolInfo.result && !steps.length) {
      // 兼容：无步骤明细但有旧版 result 文本时仍显示工具卡片
      const card = document.createElement('div');
      card.className = 'tool-card';
      const header = document.createElement('div');
      header.className = 'tool-header';
      header.textContent = '🔧 调用工具: ' + toolInfo.name;
      const body = document.createElement('div');
      body.className = 'tool-content';
      body.textContent = toolInfo.result;
      card.appendChild(header);
      card.appendChild(body);
      bubble.appendChild(card);
    }
    if (content && host === chat) wrap.appendChild(buildMsgActions(msg, content));
    // Trae：助手消息结束后给出可点击的推荐胶囊
    if (content && !opts.selfcheck && opts.persist !== false) {
      renderNextSteps(wrap, opts.nextSteps || buildNextSteps(content, steps));
    }
    msg.appendChild(wrap);
  } else {
    // 用户消息：右侧气泡（图片附件 + 纯文本）
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.innerHTML = '<i data-icon="user" class="ico"></i>';
    bubble = document.createElement('div');
    bubble.className = 'bubble';
    if (images && images.length) {
      images.forEach(url => {
        const im = document.createElement('img');
        im.className = 'md-img';
        im.src = url;
        bubble.appendChild(im);
      });
    }
    if (content) bubble.appendChild(document.createTextNode(content));
    msg.appendChild(bubble);
    msg.appendChild(avatar);
  }
  host.appendChild(msg);
  host.scrollTop = host.scrollHeight;

  // 裁剪旧消息DOM，保持渲染轻量
  while (host.children.length > MAX_DOM_MSGS) host.removeChild(host.firstChild);

  // localStorage 持久化（恢复渲染时不重复写入）
  if (host === chat && opts.persist !== false && (content || (images && images.length))) {
    persistMsg(role, content, images,
      (role === 'ai' && opts.toolInfo && opts.toolInfo.steps && opts.toolInfo.steps.length)
        ? { steps: opts.toolInfo.steps, elapsed: opts.ms } : null);
  }

  if (host === chat && role === 'ai' && ttsEnabled && content && opts.persist !== false) {
    speak(mdToPlain(content));
  }
  syncChatEmpty();
  return bubble;
}

function addTyping() {
  const msg = document.createElement('div');
  msg.className = 'msg ai';
  msg.id = 'typing';
  const wrap = document.createElement('div');
  wrap.className = 'agent-msg';
  const head = document.createElement('div');
  head.className = 'agent-head';
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.innerHTML = aiAvatarContent();
  const nameEl = document.createElement('span');
  nameEl.className = 'agent-name';
  nameEl.textContent = agentDisplayName();
  head.appendChild(avatar);
  head.appendChild(nameEl);
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  wrap.appendChild(head);
  wrap.appendChild(bubble);
  msg.appendChild(wrap);
  chat.appendChild(msg);
  chat.scrollTop = chat.scrollHeight;
}

function removeTyping() {
  const t = document.getElementById('typing');
  if (t) t.remove();
}

let curSpeakAudio = null;
function stopSpeak() {
  if (curSpeakAudio) {
    try { curSpeakAudio.pause(); curSpeakAudio.removeAttribute('src'); } catch (_) {}
    curSpeakAudio = null;
  }
}
function speak(text) {
  if (!text) return;
  stopSpeak();   // 重新朗读前停掉上一段
  const shortText = text.substring(0, 200);
  let url = '/tts?text=' + encodeURIComponent(shortText) + '&emotion=' + encodeURIComponent(ttsEmotion);
  if (ttsVoice) url += '&voice=' + encodeURIComponent(ttsVoice);
  const audio = new Audio(url);
  curSpeakAudio = audio;
  audio.onended = () => { if (curSpeakAudio === audio) curSpeakAudio = null; };
  audio.onerror = () => {
    if (curSpeakAudio === audio) curSpeakAudio = null;
    toast('吴晓欣语音播放失败，请稍后重试');
  };
  audio.play().catch(e => {
    if (curSpeakAudio === audio) curSpeakAudio = null;
    toast(e && e.name === 'NotAllowedError'
      ? '浏览器拦截了自动播放，可点击消息「⋯ → 朗读」'
      : '吴晓欣语音播放失败，请稍后重试');
  });
}

/* ============ 发送消息 ============ */
/* ============ 一键部署技能 ============ */
let ONE_CLICK = null;
const dplEsc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function renderSkillBar(){
  const bar = document.getElementById('skillBar');
  if(!bar) return;
  if(!ONE_CLICK){ bar.classList.remove('has'); bar.innerHTML = ''; return; }
  bar.classList.add('has');
  bar.innerHTML = '<span class="skill-chip">' +
    '<button class="x" title="移除技能" onclick="clearOneClick()">×</button>' +
    '<span class="sk">一键部署</span><span class="tx">' + dplEsc(ONE_CLICK.prompt) + '</span></span>';
}

function startOneClickDeploy(prompt){
  ONE_CLICK = { prompt: prompt || '帮我创建一个网站并部署。' };
  renderSkillBar();
  input.value = '';
  input.dispatchEvent(new Event('input'));
  try { syncChatEmpty(); } catch(e) {}
  input.focus();
}
window.startOneClickDeploy = startOneClickDeploy;
function goSites(){ try { parent.go('sites'); } catch(e) {} }

function clearOneClick(){
  ONE_CLICK = null;
  renderSkillBar();
}

async function runOneClickDeploy(prompt){
  const live = createLiveAgentMsg(chat, 'wasily');
  live.statusEl.textContent = '正在生成网站…';
  try{
    const res = await fetch('/v1/deploy/oneclick', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prompt: prompt})
    });
    const d = await res.json();
    if(!d.ok) throw new Error(d.error || '部署失败');
    const s = d.site || {};
    const url = s.url_lan || s.url_local || '';
    live.statusEl.textContent = '已完成';
    live.textEl.innerHTML =
      '<div class="dpl-ok"><i>✓</i>' + dplEsc(d.site_name) + ' 已创建并部署成功！</div>' +
      '<div class="dpl-sec">📋 部署信息</div>' +
      '<table class="dpl-table"><thead><tr><th>项目</th><th>详情</th></tr></thead><tbody>' +
      '<tr><td>Site 名称</td><td>' + dplEsc(d.site_name) + '</td></tr>' +
      '<tr><td>访问地址</td><td><a href="' + url + '" target="_blank">' + dplEsc(url) + '</a></td></tr>' +
      '<tr><td>Site ID</td><td>' + dplEsc(s.id) + '</td></tr>' +
      '<tr><td>Deployment ID</td><td>' + dplEsc(d.deployment_id) + '</td></tr>' +
      '<tr><td>部署类型</td><td>' + dplEsc(d.deploy_type) + '</td></tr>' +
      '<tr><td>访问权限</td><td>仅自己可见（private）</td></tr>' +
      '<tr><td>当前状态</td><td><span class="online"><i></i>Online</span></td></tr>' +
      '</tbody></table>' +
      '<div class="dpl-sec">🍅 网站内容</div>' +
      '<div style="font-size:13px;color:#cfd3ca">我为你创建了一个专业的' +
      dplEsc(d.site_name) + '，包含以下板块：</div>' +
      '<ul style="margin:8px 0 0 18px;font-size:12.8px;color:#c9cec3;line-height:1.9">' +
      (d.summary || []).map(x => '<li>' + dplEsc(x) + '</li>').join('') + '</ul>' +
      '<div class="dpl-sec">📁 源码位置</div>' +
      '<div style="font-size:12.8px;color:#c9cec3">网站源码保存在 <code>' +
      dplEsc(d.path) + '</code> 目录：</div>' +
      '<ul style="margin:6px 0 0 18px;font-size:12.8px;color:#c9cec3;line-height:1.9">' +
      (d.files || []).map(f => '<li>' + dplEsc(f.name) + ' — 主页面</li>').join('') + '</ul>' +
      '<div class="dpl-files"><span>📄 ' + (d.files || []).length +
      ' 个文件已更改 <span class="add">+' + (((d.files || [])[0] || {}).added || 218) +
      '</span><span class="del">-0</span></span><span>↗</span></div>' +
      '<div class="dpl-site"><div class="thumb">' +
      (url ? '<iframe src="' + dplEsc(url) + '" scrolling="no"></iframe>' : '') + '</div>' +
      '<div><b>' + dplEsc(d.site_name) + '</b><a href="' + url + '" target="_blank">' +
      dplEsc(url) + '</a></div><span class="vis">🔒 仅自己可见</span></div>' +
      '<div class="dpl-acts">' +
      '<button onclick="goSites()">把网站改为公开访问 →</button>' +
      '<button onclick="input.value=\'帮我修改首页的统计数据\';input.focus()">帮我修改首页的统计数据 →</button>' +
      '<button onclick="input.value=\'添加一个产品展示模块\';input.focus()">添加一个产品展示模块 →</button></div>' +
      '<div style="margin-top:10px;font-size:11.5px;color:#7f8478">✓ 由 AI 生成</div>';
  }catch(e){
    live.statusEl.textContent = '失败';
    live.textEl.innerHTML =
      '<div style="color:#f0736a;font-size:13px">部署失败：' + dplEsc(e.message) + '</div>';
  }
  chat.scrollTop = chat.scrollHeight;
}

async function sendMessage() {
  if(ONE_CLICK){
    const p = ONE_CLICK.prompt;
    clearOneClick();
    input.value = '';
    input.dispatchEvent(new Event('input'));
    try { syncChatEmpty(); } catch(e) {}
    addMsg('user', '一键部署 ' + p);
    runOneClickDeploy(p);
    return;
  }
  if (WB_MODE) {
    // 空状态用的是共用居中输入卡，文本走 wbSend 的 preset 入口投递给当前面板
    const txt = input.value.trim();
    const id = WB_ACTIVE_ID || (SP[0] && SP[0].id);
    if (!id) return;
    await wbSend(id, txt || null);
    if (txt) {
      input.value = '';
      input.dispatchEvent(new Event('input'));
      syncChatEmpty();
    }
    return;
  }
  if (lockByOther) return;   // 其他对话窗口持锁：禁止发送
  const text = input.value.trim();
  const imgs = pendingImgs.slice();
  const docs = pendingDocs.slice();
  if (!text && !imgs.length && !docs.length) return;
  if (!lockHeld) await lockAcquire();   // Enter 直达时补抢锁
  if (lockByOther) return;
  lockGenerating = true;
  lockApplyUI();
  input.value = '';
  pendingImgs = [];
  pendingDocs = [];
  renderAttachBar();
  histIdx = -1;
  pushInputHist(text);
  /* 气泡显示：正文 + 文档清单行；模型内容：正文 + 文档全文（文本类文档真实参与对话） */
  const docLines = docs.map(d => '📎 ' + d.name + ' (' + fmtSize(d.size) + ')').join('\n');
  addMsg('user', text + (docLines ? (text ? '\n' : '') + docLines : ''), { images: imgs });
  const docBlock = docs.map(d =>
    '\n\n---\n📎 附件文档《' + d.name + '》内容：\n```\n' + d.text + '\n```').join('');
  await requestAIReply(text + docBlock, imgs, { workbenchId: WB_ID })
    .catch(() => {})
    .finally(() => {
      // 兜底恢复输入：请求抛异常时也要把锁状态收回来，避免永久禁输
      if (!WB_MODE) { lockGenerating = false; lockApplyUI(); }
    });
  input.focus();
}

/* ---- Trae 式实时执行过程：流式正文 + 工具事件逐步渲染 ---- */
function createLiveAgentMsg(host, agentName) {
  host = host || chat;
  const msg = document.createElement('div');
  msg.className = 'msg ai live';   // live 标记：新提问时据此清掉上一轮没收尾的块
  const wrap = document.createElement('div');
  wrap.className = 'agent-msg';
  const head = document.createElement('div');
  head.className = 'agent-head';
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.innerHTML = aiAvatarContent();
  const nameEl = document.createElement('span');
  nameEl.className = 'agent-name';
  nameEl.textContent = agentName || 'wasily';
  const status = document.createElement('span');
  status.className = 'live-status';
  status.textContent = '正在思考…';
  head.appendChild(avatar);
  head.appendChild(nameEl);
  head.appendChild(status);
  wrap.appendChild(head);

  // 思考过程（Trae 卡片：折叠行 + 粗体结论 + 灰色统计，生成中展开步骤）
  const thinkBox = document.createElement('div');
  thinkBox.className = 'think-flow open thinking';   // thinking：生成中显示旋转 ✳，整块收尾时移除
  thinkBox.style.display = 'none';
  const th = document.createElement('div');
  th.className = 'tf-head';
  th.innerHTML = '<span class="tf-spin">' + TF_ICONS.think + '</span>' +
    '<span class="tf-dot"></span><span class="tf-title">思考过程</span><span class="tf-arr">›</span>';
  th.onclick = () => { thinkBox.classList.toggle('open'); };
  const conclEl = document.createElement('div');
  conclEl.className = 'tk-concl';
  conclEl.style.display = 'none';
  const statEl = document.createElement('div');
  statEl.className = 'tk-stat';
  statEl.style.display = 'none';
  const tb = document.createElement('div');
  tb.className = 'tf-body';
  const tipEl = document.createElement('div');
  tipEl.className = 'tk-tip';
  tipEl.style.display = 'none';
  tipEl.innerHTML = '<span class="ic">' + TF_ICONS.think + '</span><span>如果 Bug 难复现，可把出现条件告诉我，让我先写一个复现脚本。</span>';
  thinkBox.appendChild(th);
  thinkBox.appendChild(conclEl);
  thinkBox.appendChild(statEl);
  thinkBox.appendChild(tipEl);
  thinkBox.appendChild(tb);
  wrap.appendChild(thinkBox);

  // 实时工具行直接进时间线（保留字段名，旧调用处不用改）
  const toolRows = tb;

  // 流式正文
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const md = document.createElement('div');
  md.className = 'md';
  md.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  bubble.appendChild(md);
  wrap.appendChild(bubble);

  msg.appendChild(wrap);
  host.appendChild(msg);
  host.scrollTop = host.scrollHeight;
  return { root: msg, host, wrap, thinkBox, thinkBody: tb, toolRows, textEl: md, statusEl: status,
           conclEl, statEl, tipEl, liveSteps: [], lastToolRow: null, lastToolArgs: null,
           fileChanges: [], fileCard: null };
}

function liveSetText(live, t) {
  live.textEl.innerHTML = t ? mdRender(t)
    : '<div class="typing"><span></span><span></span><span></span></div>';
  live.host.scrollTop = live.host.scrollHeight;
}
function liveSetStatus(live, t) {
  live.statusEl.textContent = t || '';
}
function liveHandleEvent(live, ev) {
  if (ev.type === 'think') {
    live.thinkBox.style.display = '';
    const t = String(ev.text || '').trim();
    if (t) {
      live.conclEl.style.display = '';
      live.conclEl.textContent = live.conclEl.textContent ? (live.conclEl.textContent + '\n' + t) : t;
    }
    const row = tfMakeRow({ ico: 'think', label: '思考', det: ev.text || '' });
    live.thinkBody.appendChild(row);
    live.host.scrollTop = live.host.scrollHeight;
  } else if (ev.type === 'tool_call') {
    let arg = '';
    let parsed = {};
    try {
      parsed = JSON.parse(ev.args || '{}');
      arg = String(parsed.path || parsed.command || parsed.code || parsed.url || parsed.app_name || parsed.query || '').slice(0, 60);
    } catch (e) {}
    live.lastToolArgs = parsed;
    live.liveSteps.push({ tool: ev.tool || '', args: parsed });
    if (live.tipEl) live.tipEl.style.display = '';
    if (live.statEl) {
      const st = thinkStatsText(live.liveSteps);
      if (st) {
        live.statEl.style.display = '';
        live.statEl.innerHTML = mdEsc(st + '，正在执行命令') + ' <span class="arr">›</span>';
      }
    }
    // 与最终渲染共用 tfStepInfo：SVG 图标 +「命令已执行 / 已读取 / 搜索」同款文案
    const info = tfStepInfo({ tool: ev.tool || 'tool', args: parsed, result: '' });
    if (!info.label && arg) info.label = (ev.tool || 'tool') + ' ' + arg;
    const row = tfMakeRow(info);
    row.classList.add('running');
    live.thinkBody.appendChild(row);
    live.lastToolRow = row;
    liveSetStatus(live, '正在调用工具：' + (ev.tool || ''));
    live.host.scrollTop = live.host.scrollHeight;
  } else if (ev.type === 'tool_result') {
    if (live.liveSteps.length) live.liveSteps[live.liveSteps.length - 1].result = ev.result || '';
    if (live.statEl) {
      const st = thinkStatsText(live.liveSteps);
      if (st) { live.statEl.style.display = ''; live.statEl.innerHTML = mdEsc(st) + ' <span class="arr">›</span>'; }
    }
    const row = live.lastToolRow;
    if (row) {
      row.classList.remove('running');
      if (ev.ok === false) row.classList.add('err');
      const line = row.querySelector('.tf-line');
      if (line && !line.querySelector('.tf-tag')) {
        const sp = document.createElement('span');
        sp.className = 'tf-tag';
        sp.textContent = (ev.ok === false ? '✗ ' : '✓ ') +
          (ev.dur != null ? ev.dur + 's' : '完成');
        line.insertBefore(sp, line.querySelector('.tf-arr'));
      }
      const det = row.querySelector('.tf-det');
      if (det && ev.result) det.textContent = String(ev.result);
    }
    // 编辑类工具成功后，实时累加并刷新「N 个文件已更改」卡片
    if (FC_EDIT_RE.test(ev.tool || '') && live.lastToolArgs) {
      const a = live.lastToolArgs;
      const p = a.path || a.file || a.dst || a.target || '';
      if (p) {
        const addT = a.content || a.text || a.new_content || a.new_string || '';
        const delT = a.old_string || a.old_content || a.old || '';
        live.fileChanges.push({
          path: p,
          add: addT ? String(addT).split('\n').length : 0,
          del: delT ? String(delT).split('\n').length : (ev.tool === 'file_delete' ? 1 : 0)
        });
        const fresh = buildFileCard(live.fileChanges);
        if (live.fileCard) { live.fileCard.replaceWith(fresh); }
        else { live.wrap.insertBefore(fresh, live.thinkBox); }
        live.fileCard = fresh;
      }
    }
    liveSetStatus(live, '正在思考…');
  } else if (ev.type === 'plan_step') {
    liveSetStatus(live, (ev.state === 'done' ? '✓ 步骤 ' : '▶ 步骤 ') + (ev.step || '') + '：' + (ev.title || ''));
  }
}

/* 发起一次 AI 回复请求（发送 / 重新生成共用；不再新增用户气泡）
   Trae 式实时过程：SSE 流式正文 + 轮询 /tool_events 实时渲染思考/工具步骤，
   结束后用 addMsg 渲染最终成品消息（思考块 + 工具汇总 + 正文）。 */
async function requestAIReply(text, imgs, opts) {
  opts = opts || {};
  const host = opts.host || chat;
  const targetSession = opts.sessionId || sessionId;
  const isolated = !!opts.pane;
  if (!isolated) {
    lockGenerating = true;
    lockApplyUI();
  }

  const live = createLiveAgentMsg(host, opts.agentName);
  // 上一轮若异常中断，live 块会留在页面上；先清掉，避免新一轮步骤并进旧块
  host.querySelectorAll('.msg.ai.live').forEach(n => { if (n !== live.root) n.remove(); });
  let evSince = 0, pollStop = false;
  let accText = '';
  let failure = '';
  const turnStartTs = Date.now() / 1000 - 1;   // 只收本轮事件，历史事件不再重放

  // 事件游标从"当前最大 id"起步：否则 since=0 会把同会话的历史步骤全量重放，
  // 新提问的思考流里混进上一轮的 file_read/memory_* 步骤（表现为内容并到一起）。
  try {
    const probe = await fetch('/tool_events?since=999999999', { cache: 'no-store' });
    const pd = await probe.json();
    evSince = Number(pd.last || 0) || 0;
  } catch (e) {}

  // 每个面板只接收本会话的思考与工具事件，避免多对话互相串台。
  const pollEv = (async () => {
    while (!pollStop) {
      try {
        const r = await fetch('/tool_events?since=' + evSince, { cache: 'no-store' });
        const d = await r.json();
        (d.events || []).forEach(ev => {
          evSince = Math.max(evSince, ev.id || 0);
          if (ev.sid !== targetSession) return;
          if (ev.ts && ev.ts < turnStartTs) return;   // 双保险：丢弃本轮之前的事件
          liveHandleEvent(live, ev);
        });
      } catch (e) {}
      if (!pollStop) await new Promise(r => setTimeout(r, 700));
    }
  })();

  let doneObj = null, streamFailed = false;
  const turnStart = Date.now();
  try {
    const body = {
      model: 'qwen2.5-7b-instruct',
      messages: [{ role: 'user', content: text || '（我发送了 ' + (imgs || []).length + ' 张图片）' }],
      max_tokens: 1024,
      temperature: 0.7,
      mode: opts.mode || replyMode,
      agent: opts.agent || localStorage.getItem('wasily_agent') || 'wasily',
      shield: shieldOn,
      session_id: targetSession,
      max_tool_rounds: 8,
      stream: true
    };
    if (opts.workbenchId) body.workbench_id = opts.workbenchId;
    if (opts.windowId) body.window_id = opts.windowId;
    if (opts.groupId && opts.turnId) {
      body.group_id = opts.groupId;
      body.turn_id = opts.turnId;
    }
    const res = await fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: opts.signal,
      body: JSON.stringify(body)
    });
    if (!res.ok || !res.body) {
      let message = 'HTTP ' + res.status;
      try {
        const d = await res.json();
        if (d.error) message += ' ' + (typeof d.error === 'string' ? d.error : JSON.stringify(d.error));
      } catch (e) {}
      throw new Error(message);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    while (true) {
      const rd = await reader.read();
      if (rd.done) break;
      buf += decoder.decode(rd.value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = raw.split('\n').find(l => l.startsWith('data: '));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }
        if (ev.ev === 'delta') {
          accText += ev.t || '';
          liveSetText(live, accText);
        } else if (ev.ev === 'reset') {
          accText = '';
          liveSetText(live, '');
          liveSetStatus(live, '正在调用工具…');
        } else if (ev.ev === 'done') {
          doneObj = ev;
        }
      }
    }
  } catch (e) {
    streamFailed = true;
    failure = e.message;
    live.root.remove();
    if (!(opts.signal && opts.signal.aborted)) {
      addMsg('ai', '请求失败: ' + e.message, {
        host, persist: isolated ? false : undefined, agentName: opts.agentName
      });
    }
  }

  pollStop = true;
  try { await pollEv; } catch (e) {}

  if (!streamFailed) {
    live.root.remove();
    if (doneObj) {
      const steps = doneObj.steps || [];
      const toolInfo = (doneObj.used_tool || steps.length)
        ? { name: doneObj.tool_name || 'tool',
            steps: steps,
            result: steps.map(s =>
              s.think ? '[思考] ' + s.think
              : '[工具] ' + s.tool + '\n参数: ' + JSON.stringify(s.args || {})
                + '\n结果: ' + (s.result || '')).join('\n\n') || '执行完成' }
        : null;
      addMsg('ai', doneObj.content || '', {
        host, persist: isolated ? false : undefined, toolInfo, agentName: opts.agentName,
        ms: doneObj.elapsed ? Math.round(Number(doneObj.elapsed) * 1000) : (Date.now() - turnStart)
      });
      if (host === chat && doneObj.plan) addPlanCard(doneObj.plan);
    } else if (!(opts.signal && opts.signal.aborted)) {
      addMsg('ai', '⚠️ 连接中断，未收到完整回复，请重试', {
        host, persist: isolated ? false : undefined, agentName: opts.agentName
      });
    }
  }
  if (!isolated) {
    lockGenerating = false;
    lockApplyUI();
  }
  return {
    ok: !streamFailed && !!doneObj && !!(doneObj.content || '').trim(),
    content: doneObj ? (doneObj.content || '') : '',
    done: doneObj,
    error: failure
  };
}

/* 重新生成：沿用上一条用户提问，仅刷新 AI 回复 */
async function regenerateReply(text) {
  if (!lockHeld) await lockAcquire();
  if (lockByOther) return;
  await requestAIReply(text, [], { workbenchId: WB_ID });
  input.focus();
}
sendBtn.addEventListener('click', sendMessage);

/* ============ 对话窗口输入互斥（跨窗口/跨进程，服务端锁协调） ============
   规则：一个窗口聚焦输入或等待回复期间，其他对话窗口（桌面 pywebview /
   系统浏览器页面，前端存储不互通，故经 /input_lock 协调）禁止输入。
   焦点离开且不在等待回复时立即释放；页面关闭也释放；心跳 2s、TTL 6s。 */
const CHAT_CID = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
let lockHeld = false;        // 本窗口持有锁
let lockGenerating = false;  // 等待回复中（失焦/隐藏也不释放）
let lockByOther = false;     // 锁被其他窗口持有 → 本窗口禁输
let lockBeatTimer = null, lockPollTimer = null, lockBlurTimer = null;

const lockBanner = document.createElement('div');
lockBanner.id = 'lockBanner';
lockBanner.textContent = '另一个对话窗口正在使用输入，点此可强制接管';
lockBanner.title = '点击强制接管输入';
lockBanner.style.cursor = 'pointer';
lockBanner.addEventListener('click', async () => {
  if (!lockByOther) return;
  await lockPost('force');           // 后端无视 TTL 直接判给自己
  lockHeld = false;
  if (await lockAcquire()) {
    lockByOther = false; lockApplyUI();
    toast('已接管输入');
    try { input.focus(); } catch (e) {}
  }
});
inputArea.parentNode.insertBefore(lockBanner, inputArea);

function lockApplyUI() {
  input.disabled = lockByOther;
  sendBtn.disabled = lockByOther || lockGenerating;
  micBtn.disabled = lockByOther;
  sparkBtn.disabled = lockByOther;
  lockBanner.classList.toggle('show', lockByOther);
}
async function lockPost(action) {
  if (WB_MODE) return { ok: true };
  try {
    const r = await fetch('/input_lock/' + action, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: CHAT_CID })
    });
    return await r.json();
  } catch (e) { return null; }
}
async function lockAcquire() {
  if (WB_MODE) return true;
  const d = await lockPost('acquire');
  if (!d) return lockHeld;
  lockHeld = !!d.ok;
  if (!lockHeld && d.owner && d.owner !== CHAT_CID) { lockByOther = true; lockApplyUI(); }
  if (lockHeld) {
    lockByOther = false;
    // 抢到锁时清掉「失焦即释放」的待执行定时器：点横幅接管会先让输入框 blur，
    // 300ms 后那个定时器会把刚拿到的锁又释放掉，导致瞬间被别的窗口抢回去
    if (lockBlurTimer) { clearTimeout(lockBlurTimer); lockBlurTimer = null; }
    lockBeatStart();
    lockApplyUI();
  }
  return lockHeld;
}
function lockBeatStart() {
  if (lockBeatTimer) return;
  lockBeatTimer = setTimeout(lockBeatTick, 2000);
}
async function lockBeatTick() {
  if (!lockHeld) return;
  // 失焦/隐藏且没在生成时立即让出：否则一个从不失焦的后台窗口会一直续命占锁，
  // 其它对话窗口永远解锁不了（这是之前「浏览器 IPC 锁」卡死的根因）
  if (!lockGenerating && (!document.hasFocus() || document.visibilityState === 'hidden')) {
    lockRelease();
    return;
  }
  if (!_pollInflight.has('beat')) {
    _pollInflight.add('beat');
    try {
      const d = await lockPost('heartbeat');
      if (d && !d.ok && d.owner && d.owner !== CHAT_CID) {
        lockHeld = false; lockByOther = true;
        clearInterval(lockBeatTimer); lockBeatTimer = null;
        lockApplyUI();
      }
      pollOk();
    } catch (e) { pollFail(); }
    finally { _pollInflight.delete('beat'); }
  }
  if (lockHeld) setTimeout(lockBeatTick, _pollBackoff || 2000);
}
function lockRelease() {
  if (WB_MODE) return;
  if (lockGenerating) return;
  lockHeld = false;
  if (lockBeatTimer) { clearInterval(lockBeatTimer); lockBeatTimer = null; }
  lockReleaseBeacon();
}
// 释放锁改用 sendBeacon：页面失焦/关闭时普通 fetch 会被浏览器中断，控制台报
// net::ERR_ABORTED http://127.0.0.1:8000/input_lock/release；beacon 不参与中断。
function lockReleaseBeacon() {
  if (WB_MODE) return;
  const body = JSON.stringify({ id: CHAT_CID });
  try {
    if (navigator.sendBeacon && navigator.sendBeacon('/input_lock/release',
        new Blob([body], { type: 'application/json' }))) return;
  } catch (e) { /* ignore */ }
  try {
    fetch('/input_lock/release', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: body, keepalive: true
    });
  } catch (e) {}
}
window.addEventListener('pagehide', () => { if (!lockGenerating) lockReleaseBeacon(); });
// 窗口切走即让锁（生成中除外）：跨进程多窗口场景下 blur 比 input.blur 更可靠
window.addEventListener('blur', () => {
  if (!lockGenerating && lockHeld) lockRelease();
});
window.addEventListener('focus', () => {
  if (lockByOther) { lockPoll(); return; }
  try { if (document.activeElement === input && !lockHeld) lockAcquire(); } catch (e) {}
});
input.addEventListener('focus', () => {
  if (lockBlurTimer) { clearTimeout(lockBlurTimer); lockBlurTimer = null; }
  if (!lockHeld && !lockByOther) lockAcquire();
});
input.addEventListener('blur', () => {
  if (lockBlurTimer) clearTimeout(lockBlurTimer);
  // 延时：点击发送按钮的 click 晚于 blur，需等其同步置位 lockGenerating
  lockBlurTimer = setTimeout(() => { if (!lockGenerating) lockRelease(); }, 300);
});
async function lockPoll() {
  if (WB_MODE) return;
  if (!_pollInflight.has('lock')) {
    _pollInflight.add('lock');
    try {
      const r = await fetch('/input_lock?id=' + encodeURIComponent(CHAT_CID), { cache: 'no-store' });
      const d = await r.json();
      if (d.owner === CHAT_CID) {
        if (!lockHeld) { lockHeld = true; lockByOther = false; lockBeatStart(); lockApplyUI(); }
      } else if (d.locked) {
        if (!lockByOther) { lockByOther = true; lockApplyUI(); }
      } else if (!d.owner && !lockHeld && lockByOther) {
        lockByOther = false; lockApplyUI();
      }
      pollOk();
    } catch (e) { pollFail(); }
    finally { _pollInflight.delete('lock'); }
  }
  lockPollTimer = setTimeout(lockPoll, _pollBackoff || 2500);
}
lockPollTimer = setTimeout(lockPoll, 2500);
window.addEventListener('pagehide', lockRelease);
/* 不监听 visibilitychange 释放：独立窗口并存时窗口始终可见；标签页切换隐藏
   不应释放（焦点仍在），锁靠失焦/pagehide/TTL(6s) 回收 */
lockPoll();
lockApplyUI();

/* ============ IDE ↔ Agent 联动 ============ */
const IDE_BRIDGE = 'http://127.0.0.1:18753';
const ideChip = document.getElementById('ideChip');
let ideCtx = null;

// 页头 IDE 现场提示：先读服务端快照（同源、永不报网络错误），只要服务端标记 IDE 在线，
// 再直连 18753 桥取更实时的一份。IDE 关闭时完全不碰桥，控制台不再出现 ERR_CONNECTION_REFUSED。
let ideBridgeDownUntil = 0;
async function refreshIdeChip() {
  let d = null;
  try {
    const r2 = await fetch('/v1/ide/context', { cache: 'no-store' });
    if (r2.ok) d = await r2.json();
  } catch (e) { /* 服务未启动 */ }
  // 不再直连 IDE 桥(18753)：桥只有 IDE 运行时才存在，直连会在控制台刷 ERR_CONNECTION_REFUSED。
  // 服务端 /v1/ide/context 已由插件心跳（30s）+ 编辑事件实时落盘，这里只读它即可。
  ideCtx = d;
  // 始终展示状态：IDE 未连接时也显示「IDE 未连接」，避免用户以为没有联动
  ideChip.style.display = 'flex';
  if (!d || !d.file) {
    ideChip.classList.add('off');
    document.getElementById('ideChipFrom').style.display = 'none';
    document.getElementById('ideChipName').textContent = 'IDE 未连接';
    document.getElementById('ideChipSel').textContent = '';
    ideChip.title = 'IDE 未连接：打开 wasily IDE 后会自动同步当前文件';
    return;
  }
  ideChip.classList.toggle('off', !d.online);
  document.getElementById('ideChipFrom').style.display = fromIde ? '' : 'none';
  document.getElementById('ideChipName').textContent = String(d.file).split(/[\\/]/).pop();
  const ln = d.line;
  document.getElementById('ideChipSel').textContent =
    (ln && ln.length === 2 && ln[0] !== ln[1]) ? 'L' + ln[0] + '-' + ln[1] : '';
  ideChip.title = d.file + (d.selection ? '\n已选中 ' + d.selection.length + ' 字' : '') +
    '\n点击在 IDE 中打开';
}

ideChip.addEventListener('click', async () => {
  if (!ideCtx || !ideCtx.file) { toast('IDE 未连接：请先打开 wasily IDE'); return; }
  try {
    await fetch(IDE_BRIDGE + '/open_file', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: ideCtx.file,
        line: (ideCtx.line && ideCtx.line[0]) || 0
      })
    });
    toast('已在 IDE 中打开');
  } catch (e) {
    toast('IDE 未连接：' + e.message);
  }
});

// 载入服务端会话（IDE 会话与 Web 会话共用一份存储）
async function loadIdeSession(sid) {
  chat.innerHTML = '';
  try {
    const r = await fetch('/v1/agent/session?id=' + encodeURIComponent(sid), { cache: 'no-store' });
    const d = await r.json();
    const msgs = (d.messages || []).filter(m => {
      const c = String(m.content || '');
      if (m.role === 'user' && (c.indexOf('工具执行结果：') === 0 || c.indexOf('⚠️ 系统拦截') === 0))
        return false;
      return m.role === 'user' || m.role === 'assistant';
    });
    if (!msgs.length) {
      chat.innerHTML = '<div class="h-empty">这场对话还没有消息，直接在下方输入即可继续</div>';
      return;
    }
    msgs.forEach(m => addMsg(m.role === 'user' ? 'user' : 'ai', m.content, {
      persist: false,
      // 会话里带了思考步骤就还原时间线，否则只显示正文
      toolInfo: (m.role === 'assistant' && m.steps && m.steps.length) ? { steps: m.steps } : null,
      ms: m.elapsed ? Math.round(Number(m.elapsed) * 1000) : 0
    }));
    chat.scrollTop = chat.scrollHeight;
  } catch (e) {
    chat.innerHTML = '<div class="h-empty">读取会话失败：' + e.message + '</div>';
  }
}

async function switchToSession(sid) {
  sessionId = sid;
  try { localStorage.setItem('wasily_chat_sid', sid); } catch (e) {}
  await loadIdeSession(sid);
  syncChatEmpty();
  showInterruptedCard();
  if (IPC_MODE) ipcReport();
  toast('已切换到对话：' + sid.replace(/^ide:/, 'IDE · '));
}

// 历史抽屉：列出所有 IDE 会话，点一下即可接着聊
async function loadIdeSessions() {
  const box = document.getElementById('ideSessions');
  const sect = document.getElementById('ideSect');
  try {
    const r = await fetch('/v1/agent/sessions?prefix=ide:&limit=20', { cache: 'no-store' });
    const d = await r.json();
    const list = (d.sessions || []).filter(s => s.source === 'ide');
    box.innerHTML = '';
    if (!list.length) { sect.style.display = 'none'; return; }
    sect.style.display = 'flex';
    list.forEach(s => {
      const item = document.createElement('div');
      item.className = 'h-item';
      const t = s.updated ? new Date(s.updated * 1000).toLocaleString('zh-CN') : '';
      item.innerHTML = '<div class="h-meta"><span class="tag" style="background:#12281a;color:#3fb950">IDE</span>' +
        '<span>' + t + '</span><span>' + s.count + ' 条</span></div><div class="h-body"></div>';
      item.querySelector('.h-body').textContent = s.title || '（空会话）';
      item.onclick = () => {
        drawer.classList.remove('show');
        switchToSession(s.session_id);
      };
      box.appendChild(item);
    });
  } catch (e) {
    sect.style.display = 'none';
  }
}
document.getElementById('ideSectRefresh').onclick = () => loadIdeSessions();

refreshIdeChip();
setInterval(refreshIdeChip, 3000);   // 3s 轮询 IDE 现场，保持 Agent 与编辑器同步
setInterval(refreshIdeChip, 5000);

/* ============ 启动时从 localStorage 恢复最近对话 ============ */
function restoreChat() {
  const all = loadStore();
  all.slice(-50).forEach(m => addMsg(m.role, m.content, {
    images: m.images, persist: false,
    toolInfo: (m.role === 'ai' && m.steps && m.steps.length) ? { steps: m.steps } : null,
    ms: m.elapsed ? Math.round(Number(m.elapsed) * 1000) : 0
  }));
  syncChatEmpty();
  chat.scrollTop = chat.scrollHeight;
}

/* ============ 断点续传：服务端记录的本轮中断任务 → 对话末尾提示卡 ============ */
function addInterruptCard(it) {
  if (!it || !it.text) return;
  if (chat.querySelector('[data-interrupt]')) return;   // 已有一张就不重复渲染
  const txt = String(it.text);
  const cut = txt.length > 120 ? txt.slice(0, 120) + '…' : txt;
  let tm = '';
  try {
    const d = new Date(it.ts || Date.now());
    tm = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  } catch (e) { tm = ''; }
  const card = document.createElement('div');
  card.className = 'interrupt-card';
  card.setAttribute('data-interrupt', '1');
  card.innerHTML =
    '<div class="ic-head"><span class="ic-dot"></span>上次任务中断在这里' +
      (tm ? '<span class="ic-time">' + tm + '</span>' : '') + '</div>' +
    '<div class="ic-text"></div>' +
    '<div class="ic-btns"><button class="ic-go">继续</button>' +
      '<button class="ic-ignore">忽略</button></div>';
  card.querySelector('.ic-text').textContent = cut;
  card.querySelector('.ic-go').onclick = () => {
    card.remove();
    // 剥掉历史叠加的前缀再拼，防止「继续上次未完成的任务：」层层嵌套
    input.value = '继续上次未完成的任务：' + txt.replace(/^(继续上次未完成的任务[：:]\s*)+/, '');
    input.dispatchEvent(new Event('input'));
    sendMessage();
  };
  card.querySelector('.ic-ignore').onclick = () => card.remove();
  chat.appendChild(card);
  chat.scrollTop = chat.scrollHeight;
}

/* 拉取服务端会话状态，有断点就出提示卡（刷新后依然可见） */
async function showInterruptedCard() {
  if (WB_MODE) return;
  try {
    const r = await fetch('/session/state?sid=' + encodeURIComponent(sessionId), { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    if (d && d.interrupted && d.interrupted.text) addInterruptCard(d.interrupted);
  } catch (e) {}
}
// 带 IDE 会话参数进入时，直接展示服务端那场对话，而不是本地缓存
if (WB_MODE) {
  chat.innerHTML = '';
} else if (sessionParam.indexOf('ide:') === 0) {
  loadIdeSession(sessionParam).then(() => {
    if (fromIde) toast('已载入 IDE 会话，可直接接着聊');
  });
} else {
  restoreChat();
}
// 服务端若记录了「上一轮生成中途断开」，在对话末尾补一张续跑卡
if (!WB_MODE) setTimeout(showInterruptedCard, 600);

/* ============ 移动端下拉刷新提示 ============ */
const ptrHint = document.getElementById('ptrHint');
let pullStartY = null, pullDist = 0;

chat.addEventListener('touchstart', (e) => {
  if (chat.scrollTop <= 0 && e.touches.length === 1) {
    pullStartY = e.touches[0].clientY;
    pullDist = 0;
  } else pullStartY = null;
}, { passive: true });
chat.addEventListener('touchmove', (e) => {
  if (pullStartY === null) return;
  pullDist = e.touches[0].clientY - pullStartY;
  if (pullDist > 0 && chat.scrollTop <= 0) {
    const show = Math.min(pullDist * 0.6, 56);
    ptrHint.style.transform = 'translateY(' + (show - 100) + '%)';
    ptrHint.style.transform = 'translateY(' + show + 'px)';
    ptrHint.textContent = pullDist > 70 ? '↻ 松开刷新对话' : '↓ 继续下拉刷新对话';
  }
}, { passive: true });
chat.addEventListener('touchend', () => {
  if (pullStartY === null) return;
  if (pullDist > 70 && chat.scrollTop <= 0) {
    chat.innerHTML = '';
    restoreChat();
    toast('已刷新对话记录');
  }
  pullStartY = null;
  pullDist = 0;
  ptrHint.style.transform = 'translateY(-100%)';
}, { passive: true });

/* ================= 侧边对话 ================= */
const SP_STORE = WB_MODE ? ('wasily_workbench_' + (WB_ID || 'unbound')) : 'wasily_side_chats';
let SP = [];
let SP_VISIBLE = false;
let WB_ACTIVE_ID = null;
let WB_DISCUSSION = false;
let WB_TIMELINE = true;
let WB_GROUP_ID = '';
let WB_GROUP = null;
let WB_NEXT = '';
let WB_ROOT = '';
let WB_SESSIONS = [];
let WB_TURN_STARTING = false;

function spEsc(s){
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]));
}
function spLoad(){
  if(WB_MODE){ wbLayoutLoad(); return; }
  try{
    const d = JSON.parse(localStorage.getItem(SP_STORE) || '{}');
    SP = Array.isArray(d.panes) ? d.panes.map(p => ({
      id: p.id, name: p.name || '侧边对话', msgs: Array.isArray(p.msgs) ? p.msgs : [], busy: false,
      x: typeof p.x === 'number' ? p.x : null,
      y: typeof p.y === 'number' ? p.y : null,
      w: typeof p.w === 'number' ? p.w : null,
      h: typeof p.h === 'number' ? p.h : null
    })) : [];
    SP_VISIBLE = !!d.visible;
  }catch(e){ SP = []; }
  if(!SP.length){
    SP.push({ id:'sp' + Date.now().toString(36), name:'侧边 1', msgs:[], busy:false });
    SP_VISIBLE = false;
  }
}
function spSave(){
  if(WB_MODE){ wbLayoutSave(); return; }
  try{
    localStorage.setItem(SP_STORE, JSON.stringify({
      panes: SP.map(p => ({ id:p.id, name:p.name, msgs:p.msgs,
                            x:p.x, y:p.y, w:p.w, h:p.h })),
      visible: SP_VISIBLE
    }));
  }catch(e){}
}
function spNew(){
  if(WB_MODE){ wbNew(); return; }
  SP.push({ id:'sp' + Date.now().toString(36), name:'侧边 ' + (SP.length + 1), msgs:[], busy:false });
  SP_VISIBLE = true;
  spRender(); spSave();
  const last = SP[SP.length - 1];
  const t = document.querySelector('.sp-input textarea[data-id="' + last.id + '"]');
  if(t) t.focus();
}
function spToggle(){ SP_VISIBLE = !SP_VISIBLE; spRender(); spSave(); }
function spClose(id){
  if(WB_MODE){ wbClose(id); return; }
  SP = SP.filter(p => p.id !== id);
  if(!SP.length) SP_VISIBLE = false;
  spRender(); spSave();
}
function spRename(id){
  if(WB_MODE){ wbRename(id); return; }
  const p = SP.find(x => x.id === id);
  if(!p) return;
  const n = prompt('侧边对话名称', p.name);
  if(n && n.trim()){ p.name = n.trim().slice(0, 20); spRender(); spSave(); }
}
function spMsgsHtml(p){
  if(!p.msgs.length){
    return '<div class="sp-msg sys">侧边对话：在这里追问不会打断主对话；也可以用右上角“发给…”把内容交给其他侧边或主对话。</div>';
  }
  return p.msgs.map(m =>
    '<div class="sp-msg ' + (m.role === 'user' ? 'user' : (m.role === 'sys' ? 'sys' : 'bot')) + '">' +
    spEsc(m.content) + '</div>').join('');
}
/* 浮动窗口几何：首次打开时给一个不重叠的默认位置，之后沿用用户拖出来的位置 */
let SP_Z = 60;
function spDefaultGeom(p){
  if(p.w == null) p.w = Math.max(300, Math.min(430, Math.round(window.innerWidth * 0.32)));
  if(p.h == null) p.h = Math.max(300, Math.min(600, Math.round(window.innerHeight * 0.68)));
  if(p.x == null || p.y == null){
    const i = Math.max(0, SP.indexOf(p));
    const off = (i % 6) * 26;
    p.x = Math.max(12, window.innerWidth - p.w - 74 - off);
    p.y = Math.max(12, 64 + off);
  }
  return p;
}
function spApplyGeom(el, p){
  el.style.left = Math.round(p.x) + 'px';
  el.style.top = Math.round(p.y) + 'px';
  el.style.width = Math.round(p.w) + 'px';
  el.style.height = Math.round(p.h) + 'px';
}
function spRender(){
  if(WB_MODE){ wbRender(); return; }
  const dock = document.getElementById('sideDock');
  const rail = document.getElementById('sideRail');
  if(!dock || !rail) return;
  const drafts = {};
  dock.querySelectorAll('.sp-input textarea').forEach(t => { drafts[t.dataset.id] = t.value; });
  dock.classList.toggle('on', SP_VISIBLE && SP.length > 0);
  dock.innerHTML = SP.map(p => {
    spDefaultGeom(p);
    return `
    <div class="side-pane" data-id="${p.id}" style="left:${Math.round(p.x)}px;top:${Math.round(p.y)}px;width:${Math.round(p.w)}px;height:${Math.round(p.h)}px">
      <div class="sp-head">
        <span class="sp-title" ondblclick="spRename('${p.id}')" title="双击重命名">${spEsc(p.name)}</span>
        <select class="sp-target" title="把这条对话的回复发给谁">
          <option value="">发给…</option>
          ${SP.filter(o => o.id !== p.id).map(o =>
            `<option value="${o.id}">${spEsc(o.name)}</option>`).join('')}
          <option value="main">主对话</option>
        </select>
        <button title="发送给所选对话" onclick="spHandoff('${p.id}',this)">➤</button>
        ${p.busy ? `<button class="sp-stop" title="停止这次生成" onclick="spStop('${p.id}')">■</button>` : ''}
        <button title="关闭" onclick="spClose('${p.id}')">✕</button>
      </div>
      <div class="sp-msgs" id="spMsgs_${p.id}">${spMsgsHtml(p)}</div>
      <div class="sp-input">
        <textarea data-id="${p.id}" placeholder="在这里追问…" onkeydown="spKey(event,'${p.id}')">${spEsc(drafts[p.id] || '')}</textarea>
        <button onclick="spSend('${p.id}')">↑</button>
      </div>
      <div class="sp-resize nw" data-corner="nw" title="拖动调整大小"></div>
      <div class="sp-resize ne" data-corner="ne" title="拖动调整大小"></div>
      <div class="sp-resize sw" data-corner="sw" title="拖动调整大小"></div>
      <div class="sp-resize se" data-corner="se" title="拖动调整大小"></div>
    </div>`;
  }).join('');
  rail.innerHTML = '<button class="sr-btn" title="新开侧边对话" onclick="spNew()">＋</button>' +
    '<button class="sr-btn' + (SP_VISIBLE ? ' on' : '') + '" title="显示/收起侧边对话" onclick="spToggle()">💬</button>' +
    SP.map(p => '<button class="sr-btn' + (SP_VISIBLE ? ' on' : '') + '" title="' +
      spEsc(p.name) + '" onclick="spToggle()">•</button>').join('');
  SP.forEach(p => {
    const box = document.getElementById('spMsgs_' + p.id);
    if(box) box.scrollTop = box.scrollHeight;
  });
}
/* 拖标题栏移动 / 拖四角缩放：事件挂在容器上，面板重渲染后依然有效 */
(function(){
  const dock = document.getElementById('sideDock');
  if(!dock) return;
  let drag = null;

  function onMove(e){
    if(!drag) return;
    const dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if(drag.mode === 'move'){
      drag.p.x = Math.min(Math.max(-drag.w + 100, drag.x + dx), window.innerWidth - 100);
      drag.p.y = Math.min(Math.max(0, drag.y + dy), window.innerHeight - 44);
    }else{
      const minW = 260, minH = 220;
      const c = drag.corner || 'se';
      const fromLeft = c === 'sw' || c === 'nw';   // 拖左/上边时，对边保持不动
      const fromTop = c === 'ne' || c === 'nw';
      let w = fromLeft ? drag.w - dx : drag.w + dx;
      let h = fromTop ? drag.h - dy : drag.h + dy;
      w = Math.min(Math.max(minW, w), window.innerWidth - 8);
      h = Math.min(Math.max(minH, h), window.innerHeight - 8);
      drag.p.w = w;
      drag.p.h = h;
      drag.p.x = Math.min(Math.max(0, fromLeft ? drag.x + drag.w - w : drag.x), window.innerWidth - 60);
      drag.p.y = Math.min(Math.max(0, fromTop ? drag.y + drag.h - h : drag.y), window.innerHeight - 44);
    }
    spApplyGeom(drag.el, drag.p);
  }
  function onUp(){
    if(drag){
      drag.el.classList.remove('dragging');
      document.body.classList.remove('sp-dragging');
      drag = null;
      spSave();
    }
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
  }
  dock.addEventListener('mousedown', function(e){
    if(WB_MODE || e.button !== 0) return;
    const el = e.target.closest('.side-pane');
    if(!el) return;
    const p = SP.find(x => x.id === el.dataset.id);
    if(!p) return;
    el.style.zIndex = ++SP_Z;                       // 点到哪个窗口就置顶
    const handle = e.target.closest('.sp-resize');
    const resizing = !!handle;
    // 标题栏上的按钮/下拉要保留原有点击行为，不能当拖拽
    if(!resizing && e.target.closest('button,select,input,textarea,option')) return;
    if(!resizing && !e.target.closest('.sp-head')) return;
    e.preventDefault();
    const r = el.getBoundingClientRect();
    drag = { el, p, mode: resizing ? 'resize' : 'move',
             corner: handle ? (handle.dataset.corner || 'se') : '',
             sx: e.clientX, sy: e.clientY, x: r.left, y: r.top, w: r.width, h: r.height };
    document.body.classList.add('sp-dragging');
    if(!resizing) el.classList.add('dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // 窗口尺寸变化后把面板拉回可视范围，避免拖到屏幕外找不回来
  window.addEventListener('resize', function(){
    if(WB_MODE) return;
    let changed = false;
    SP.forEach(p => {
      if(p.x == null) return;
      const nx = Math.min(p.x, Math.max(0, window.innerWidth - 120));
      const ny = Math.min(p.y, Math.max(0, window.innerHeight - 60));
      if(nx !== p.x || ny !== p.y){ p.x = nx; p.y = ny; changed = true; }
      const el = dock.querySelector('.side-pane[data-id="' + p.id + '"]');
      if(el) spApplyGeom(el, p);
    });
    if(changed) spSave();
  });
})();

function spPaint(id){
  const p = SP.find(x => x.id === id);
  const box = document.getElementById('spMsgs_' + id);
  if(!p || !box) return;
  box.innerHTML = spMsgsHtml(p);
  box.scrollTop = box.scrollHeight;
}
function spKey(e, id){
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); spSend(id); }
}
async function spSend(id, preset){
  if(WB_MODE){ await wbSend(id, preset); return; }
  const p = SP.find(x => x.id === id);
  if(!p || p.busy) return;
  const ta = document.querySelector('.sp-input textarea[data-id="' + id + '"]');
  const text = String(preset != null ? preset : (ta ? ta.value : '')).trim();
  if(!text) return;
  if(preset == null && ta) ta.value = '';
  p.msgs.push({ role:'user', content:text });
  p.busy = true;
  p.stopped = false;
  p.timedOut = false;
  p.t0 = Date.now();
  const sysMsg = { role:'sys', content:'思考中… 0s' };
  p.msgs.push(sysMsg);
  spRender();
  // 本机只有一个模型实例、且 CPU 推理很慢：必须流式返回，否则长时间看不到任何反馈；
  // 同时把「排队」如实显示出来，并允许随时停止
  const tick = setInterval(() => {
    const pane = SP.find(x => x.id === id);
    if(!pane || !pane.busy) return clearInterval(tick);
    if(!sysMsg) return clearInterval(tick);
    const sec = Math.round((Date.now() - pane.t0) / 1000);
    sysMsg.content = (sec >= 15 ? '模型正忙，排队等待中… ' : '思考中… ') + sec + 's';
    if(pane.msgs.indexOf(sysMsg) >= 0) spPaint(id);
  }, 1000);
  const timer = setTimeout(() => { p.timedOut = true; spStop(id); }, 900000);
  const history = p.msgs.filter(m => m.role !== 'sys')
    .map(m => ({ role: m.role, content: m.content }));
  let acc = '';
  let live = null;      // 流式回复消息对象（首个 delta 到达后创建）
  const dropSys = () => {
    const i = p.msgs.indexOf(sysMsg);
    if(i >= 0) p.msgs.splice(i, 1);
  };
  try{
    const res = await fetch('/v1/chat/completions', {
      method:'POST', headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ messages: history, max_tokens: 512, stream: true,
                             session_id: 'side_' + id })
    });
    if(!res.ok || !res.body) throw new Error('HTTP ' + res.status);
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    while(true){
      const rd = await reader.read();
      if(rd.done) break;
      buf += decoder.decode(rd.value, { stream: true });
      let idx;
      while((idx = buf.indexOf('\n\n')) >= 0){
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = raw.split('\n').find(l => l.startsWith('data: '));
        if(!line) continue;
        let ev;
        try{ ev = JSON.parse(line.slice(6)); }catch(e){ continue; }
        if(ev.ev === 'delta'){
          acc += ev.t || '';
          if(!live){ dropSys(); live = { role:'assistant', content:acc }; p.msgs.push(live); }
          live.content = acc;
          spPaint(id);
        }else if(ev.ev === 'reset'){
          // 本轮转成工具调用：撤回已吐出的文字，继续等下一段
          acc = '';
          if(live){ live.content = '（正在调用工具…）'; spPaint(id); }
        }else if(ev.ev === 'done'){
          if(ev.content) acc = ev.content;
        }
      }
    }
    dropSys();
    if(!acc.trim()){
      p.msgs.push({ role:'sys', content: p.stopped ? (p.timedOut
        ? '等待过久已自动停止（模型正忙，可稍后再问）' : '已停止生成') : '没有拿到回复，请稍后重试' });
    }else if(live){
      live.content = acc.trim();
      if(p.stopped) p.msgs.push({ role:'sys', content:'已停止生成（保留已输出部分）' });
    }
  }catch(e){
    dropSys();
    if(!acc.trim()) p.msgs.push({ role:'sys', content:'请求失败：' + e.message });
    else if(live) live.content = acc.trim();
  }finally{
    clearInterval(tick);
    clearTimeout(timer);
    p.busy = false;
    p.stopped = false;
    p.timedOut = false;
    spRender(); spSave();
  }
}
async function spStop(id){
  if(WB_MODE){ await wbStop(id); return; }
  const p = SP.find(x => x.id === id);
  if(!p || !p.busy) return;
  p.stopped = true;
  const last = p.msgs[p.msgs.length - 1];
  if(last && last.role === 'sys') last.content = '正在停止…';
  spPaint(id);
  try{
    await fetch('/v1/agent/cancel', {
      method:'POST', headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ session_id: 'side_' + id })
    });
  }catch(e){}
}
function spLastReply(id){
  const p = SP.find(x => x.id === id);
  if(!p) return '';
  const last = p.msgs.slice().reverse().find(m => m.role === 'assistant' && m.content);
  return last ? last.content : '';
}
function spHandoff(id, btn){
  if(WB_MODE){ wbHandoff(id, btn); return; }
  const pane = btn.closest('.side-pane');
  const target = pane.querySelector('.sp-target').value;
  if(!target) return toast('先在右侧选择要发给谁');
  const content = spLastReply(id);
  if(!content) return toast('这条侧边对话还没有可发送的回复');
  const p = SP.find(x => x.id === id);
  const payload = '【来自' + (p ? p.name : '侧边对话') + '】\n' + content;
  if(target === 'main'){
    const inp = document.getElementById('input');
    if(inp){
      inp.value = (inp.value ? inp.value + '\n\n' : '') + payload;
      inp.dispatchEvent(new Event('input'));
      inp.focus();
    }
    return toast('已放入主对话输入框');
  }
  const t = SP.find(x => x.id === target);
  if(!t) return;
  toast('已发送到 ' + t.name);
  spSend(target, payload);
}

/* ================= Workbench：独立多对话 / 讨论组 ================= */
function wbUid(prefix){
  return prefix + Date.now().toString(36) + Math.random().toString(36).slice(2, 9);
}
function wbSessionId(){
  const tag = (WB_ID || 'workbench').replace(/^wb_/, '').replace(/[^A-Za-z0-9_.-]/g, '');
  return 'wbs_' + tag + '_' + Math.random().toString(36).slice(2, 10);
}
function wbPane(index){
  return {
    id: wbUid('pane_'), name: '对话 ' + (index || (SP.length + 1)),
    sessionId: wbSessionId(), agent: 'wasily', mode: 'solo',
    draft: '', busy: false, msgs: []
  };
}
function wbLayoutLoad(){
  let d = {};
  try{ d = JSON.parse(localStorage.getItem(SP_STORE) || '{}') || {}; }catch(e){}
  SP = (Array.isArray(d.panes) ? d.panes : []).slice(0, 4).map((p, i) => ({
    id: p.id || wbUid('pane_'),
    name: String(p.name || ('对话 ' + (i + 1))).slice(0, 20),
    sessionId: p.sessionId || wbSessionId(),
    agent: ['wasily', 'coder', 'ops'].includes(p.agent) ? p.agent : 'wasily',
    mode: ['ask', 'plan', 'solo'].includes(p.mode) ? p.mode : 'solo',
    draft: String(p.draft || ''), busy: false, msgs: []
  }));
  if(!SP.length) SP.push(wbPane(1));
  WB_ACTIVE_ID = SP.some(p => p.id === d.activeId) ? d.activeId : SP[0].id;
  WB_DISCUSSION = !!d.discussion;
  WB_TIMELINE = d.timeline !== false;
  WB_GROUP_ID = String(d.groupId || '');
  WB_NEXT = String(d.nextSpeaker || '');
  if(sessionParam && !WB_DISCUSSION){
    const p = SP.find(x => x.id === WB_ACTIVE_ID);
    if(p) p.sessionId = sessionParam;
  }
  SP_VISIBLE = true;
}
function wbCaptureDrafts(){
  document.querySelectorAll('#sideDock .sp-input textarea').forEach(t => {
    const p = SP.find(x => x.id === t.dataset.id);
    if(p) p.draft = t.value;
  });
}
function wbLayoutSave(){
  wbCaptureDrafts();
  try{
    localStorage.setItem(SP_STORE, JSON.stringify({
      panes: SP.map(p => ({
        id: p.id, name: p.name, sessionId: p.sessionId,
        agent: p.agent, mode: p.mode, draft: p.draft
      })),
      activeId: WB_ACTIVE_ID,
      discussion: WB_DISCUSSION,
      timeline: WB_TIMELINE,
      groupId: WB_GROUP_ID,
      nextSpeaker: WB_NEXT
    }));
  }catch(e){}
}
async function wbApi(url, options){
  const r = await fetch(url, options || {});
  let d = {};
  try{ d = await r.json(); }catch(e){}
  if(!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}
function wbIsMember(p){
  return !!(WB_GROUP && p && (WB_GROUP.members || []).some(m => m.session_id === p.sessionId));
}
function wbActiveTurn(){
  if(WB_TURN_STARTING) return { state: 'pending', turnId: '' };
  if(!WB_GROUP || !WB_GROUP.activeTurn) return null;
  const t = (WB_GROUP.turns || {})[WB_GROUP.activeTurn];
  return t && ['pending', 'running'].includes(t.state) ? t : null;
}
function wbNew(){
  if(SP.some(p => p.busy)) return toast('请先停止正在生成的面板');
  if(SP.length >= 4) return toast('同一工作台最多打开 4 个对话面板');
  const p = wbPane();
  SP.push(p);
  WB_ACTIVE_ID = p.id;
  wbRender();
  wbLayoutSave();
  const ta = document.querySelector('.sp-input textarea[data-id="' + p.id + '"]');
  if(ta) ta.focus();
}
async function wbClose(id){
  const p = SP.find(x => x.id === id);
  if(!p) return;
  if(p.busy) return toast('请先停止这次生成');
  if(wbIsMember(p)){
    if(wbActiveTurn()) return toast('本轮发言结束后才能退出面板');
    try{
      if(WB_GROUP.members.length > 1){
        const d = await wbApi('/v1/discussions/' + WB_GROUP_ID + '/members', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'remove', session_id: p.sessionId })
        });
        WB_GROUP = d.group;
      }else{
        WB_GROUP = null;
        WB_GROUP_ID = '';
        WB_DISCUSSION = false;
      }
    }catch(e){ return toast('退出讨论组失败：' + e.message); }
  }
  SP = SP.filter(x => x.id !== id);
  if(!SP.length) SP.push(wbPane(1));
  if(!SP.some(x => x.id === WB_ACTIVE_ID)) WB_ACTIVE_ID = SP[0].id;
  wbRender();
  wbLayoutSave();
}
async function wbRename(id){
  const p = SP.find(x => x.id === id);
  if(!p || p.busy) return;
  const n = prompt('对话名称', p.name);
  if(!n || !n.trim()) return;
  p.name = n.trim().slice(0, 20);
  if(wbIsMember(p) && !wbActiveTurn()){
    try{ await wbSyncMember(p); }catch(e){ toast('名称已保存，讨论组同步失败：' + e.message); }
  }
  wbRender();
  wbLayoutSave();
}
function wbActivate(id){
  if(!SP.some(p => p.id === id)) return;
  WB_ACTIVE_ID = id;
  wbRender();
  wbLayoutSave();
}
async function wbSyncMember(p){
  const d = await wbApi('/v1/discussions/' + WB_GROUP_ID + '/members', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      action: 'add',
      member: { session_id: p.sessionId, title: p.name, agent: p.agent }
    })
  });
  WB_GROUP = d.group;
}
async function wbChangeAgent(id, value){
  const p = SP.find(x => x.id === id);
  if(!p || p.busy) return;
  p.agent = value;
  if(wbIsMember(p) && !wbActiveTurn()){
    try{ await wbSyncMember(p); }catch(e){ toast('讨论组成员同步失败：' + e.message); }
  }
  wbRender();
  wbLayoutSave();
}
function wbChangeMode(id, value){
  const p = SP.find(x => x.id === id);
  if(!p || p.busy) return;
  p.mode = value;
  wbLayoutSave();
}
function wbDraft(id, value){
  const p = SP.find(x => x.id === id);
  if(!p) return;
  p.draft = value;
  wbLayoutSave();
}
function wbKey(e, id){
  if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){
    e.preventDefault();
    wbSend(id);
  }
}
async function wbSwitchSession(id, sid){
  const p = SP.find(x => x.id === id);
  if(!p || p.busy || wbIsMember(p)) return;
  p.sessionId = sid === '__new' ? wbSessionId() : sid;
  wbRender();
  wbLayoutSave();
  await wbRefreshSessions();
}
function wbHistoryOptions(p){
  const seen = new Set([p.sessionId]);
  const rows = [{ session_id: p.sessionId, title: '当前会话' }];
  WB_SESSIONS.forEach(s => {
    if(!s.session_id || seen.has(s.session_id)) return;
    seen.add(s.session_id);
    rows.push(s);
  });
  return '<option value="__new">＋ 新会话</option>' + rows.map(s =>
    '<option value="' + spEsc(s.session_id) + '"' +
    (s.session_id === p.sessionId ? ' selected' : '') + '>' +
    spEsc(String(s.title || s.session_id).slice(0, 24)) + '</option>').join('');
}
function wbPaneHtml(p){
  const member = wbIsMember(p);
  const agents = [['wasily', 'wasily'], ['coder', 'coder'], ['ops', 'ops']];
  const modes = [['ask', 'Ask 只读'], ['plan', 'Plan 规划'], ['solo', 'solo 执行']];
  return '<div class="side-pane' + (p.id === WB_ACTIVE_ID ? ' active' : '') +
    '" data-id="' + p.id + '" onclick="WB_ACTIVE_ID=\'' + p.id + '\'">' +
    '<div class="sp-head">' +
      '<span class="sp-title" ondblclick="spRename(\'' + p.id + '\')" title="双击重命名">' + spEsc(p.name) + '</span>' +
      (member ? '<span class="sp-busy-label">讨论组成员</span>' : '') +
      (p.busy ? '<span class="sp-busy-label" id="spBusy_' + p.id + '">正在思考…</span>' : '') +
      (!member ? '<select class="sp-target" title="手动转发最近回复"><option value="">发给…</option>' +
        SP.filter(o => o.id !== p.id).map(o => '<option value="' + o.id + '">' + spEsc(o.name) + '</option>').join('') +
        '</select><button title="发送给所选对话" onclick="spHandoff(\'' + p.id + '\',this)">➤</button>' : '') +
      (WB_DISCUSSION ? '<button title="' + (member ? '退出讨论组' : '加入讨论组') +
        '" onclick="wbToggleMember(\'' + p.id + '\')">' + (member ? '退出组' : '加入组') + '</button>' : '') +
      (p.busy ? '<button class="sp-stop" title="停止这次生成" onclick="spStop(\'' + p.id + '\')">■</button>' : '') +
      '<button title="关闭面板" onclick="spClose(\'' + p.id + '\')">✕</button>' +
      '<div class="sp-config">' +
        '<select title="智能体" ' + (p.busy ? 'disabled' : '') + ' onchange="wbChangeAgent(\'' + p.id + '\',this.value)">' +
          agents.map(a => '<option value="' + a[0] + '"' + (p.agent === a[0] ? ' selected' : '') + '>' + a[1] + '</option>').join('') +
        '</select>' +
        '<select title="Ask/Plan 只读，solo 可修改共享目录" ' + (p.busy ? 'disabled' : '') + ' onchange="wbChangeMode(\'' + p.id + '\',this.value)">' +
          modes.map(m => '<option value="' + m[0] + '"' + (p.mode === m[0] ? ' selected' : '') + '>' + m[1] + '</option>').join('') +
        '</select>' +
        '<select title="历史会话" ' + (p.busy || member ? 'disabled' : '') + ' onchange="wbSwitchSession(\'' + p.id + '\',this.value)">' +
          wbHistoryOptions(p) + '</select>' +
      '</div>' +
    '</div>' +
    '<div class="sp-msgs" id="spMsgs_' + p.id + '" data-pane="' + p.id + '"></div>' +
    '<div class="sp-input">' +
      '<textarea data-id="' + p.id + '" ' + (p.busy ? 'disabled' : '') +
        ' placeholder="' + (member ? '向讨论组发言，由下一位指定成员回复…' : '独立对话，不会接收其他面板内容…') +
        '" oninput="wbDraft(\'' + p.id + '\',this.value)" onkeydown="wbKey(event,\'' + p.id + '\')">' +
        spEsc(p.draft || '') + '</textarea>' +
      '<button title="发送" ' + (p.busy ? 'disabled' : '') + ' onclick="spSend(\'' + p.id + '\')">↑</button>' +
    '</div></div>';
}
function wbRender(){
  const dock = document.getElementById('sideDock');
  if(!dock) return;
  wbCaptureDrafts();
  if(!SP.some(p => p.id === WB_ACTIVE_ID)) WB_ACTIVE_ID = SP.length ? SP[0].id : null;
  const liveHosts = {};
  dock.querySelectorAll('.sp-msgs').forEach(h => {
    const p = SP.find(x => x.id === h.dataset.pane);
    if(p && p.busy) liveHosts[p.id] = h;
  });
  dock.classList.add('on');
  dock.innerHTML = SP.map(wbPaneHtml).join('');
  document.getElementById('wbTabs').innerHTML = SP.map(p =>
    '<button class="' + (p.id === WB_ACTIVE_ID ? 'active' : '') + '" onclick="wbActivate(\'' + p.id + '\')">' +
    spEsc(p.name) + '</button>').join('');
  const modeBtn = document.getElementById('wbModeBtn');
  modeBtn.textContent = WB_DISCUSSION ? '讨论组模式' : '独立模式';
  modeBtn.classList.toggle('on', WB_DISCUSSION);
  modeBtn.disabled = SP.some(p => p.busy);
  const next = document.getElementById('wbNextSpeaker');
  next.style.display = WB_DISCUSSION ? '' : 'none';
  document.getElementById('wbTimelineBtn').style.display = WB_DISCUSSION ? '' : 'none';
  const members = WB_GROUP ? (WB_GROUP.members || []) : [];
  if(!members.some(m => m.session_id === WB_NEXT)) WB_NEXT = members.length ? members[0].session_id : '';
  next.innerHTML = members.map(m => '<option value="' + spEsc(m.session_id) + '">下一位：' +
    spEsc(m.title || m.session_id) + '</option>').join('');
  next.value = WB_NEXT;
  next.disabled = !!wbActiveTurn();
  document.getElementById('wbWorkspace').textContent = WB_ROOT || '正在绑定共享目录…';
  document.getElementById('wbWorkspace').title = WB_ROOT;
  SP.forEach(p => {
    const fresh = document.getElementById('spMsgs_' + p.id);
    if(liveHosts[p.id] && fresh) fresh.replaceWith(liveHosts[p.id]);
    else wbRestorePane(p);
  });
  wbRenderTimeline();
}
async function wbRestorePane(p){
  const host = document.getElementById('spMsgs_' + p.id);
  if(!host || p.busy) return;
  const sid = p.sessionId;
  host.innerHTML = '<div class="sp-msg sys">正在读取会话…</div>';
  try{
    const d = await wbApi('/v1/agent/session?id=' + encodeURIComponent(sid));
    const current = document.getElementById('spMsgs_' + p.id);
    if(p.sessionId !== sid || p.busy || current !== host) return;
    const msgs = (d.messages || []).filter(m => {
      const c = String(m.content || '');
      return (m.role === 'user' || m.role === 'assistant') &&
        !(m.role === 'user' && (c.indexOf('工具执行结果：') === 0 || c.indexOf('⚠️ 系统拦截') === 0));
    });
    host.innerHTML = '';
    if(!msgs.length){
      host.innerHTML = '<div class="sp-msg sys">新会话。Ask/Plan 只读；solo 可修改当前共享目录。</div>';
      return;
    }
    msgs.slice(-MAX_DOM_MSGS).forEach(m => addMsg(m.role === 'user' ? 'user' : 'ai',
      String(m.content || ''), { host, persist: false, agentName: p.name }));
    host.scrollTop = host.scrollHeight;
  }catch(e){
    if(host.isConnected) host.innerHTML = '<div class="sp-msg sys">读取会话失败：' + spEsc(e.message) + '</div>';
  }
}
async function wbRefreshSessions(){
  if(!WB_ID) return;
  const prefix = 'wbs_' + WB_ID.replace(/^wb_/, '') + '_';
  try{
    const d = await wbApi('/v1/agent/sessions?prefix=' + encodeURIComponent(prefix) + '&limit=50');
    WB_SESSIONS = d.sessions || [];
  }catch(e){ WB_SESSIONS = []; }
}
async function wbRefreshGroup(){
  if(!WB_GROUP_ID){ WB_GROUP = null; return; }
  const d = await wbApi('/v1/discussions/' + encodeURIComponent(WB_GROUP_ID));
  WB_GROUP = d;
  wbRenderTimeline();
}
async function wbInit(){
  wbRender();
  if(!WB_ID){
    document.getElementById('sideDock').innerHTML = '<div class="sp-msg sys">缺少 workbench_id，无法绑定共享目录</div>';
    return;
  }
  try{
    const wb = await wbApi('/v1/workbench/bind', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workbench_id: WB_ID, window_id: WB_WINDOW_ID })
    });
    WB_ROOT = wb.workspace_root || '';
    await wbRefreshSessions();
    if(WB_GROUP_ID){
      try{ await wbRefreshGroup(); }
      catch(e){
        WB_GROUP = null;
        WB_GROUP_ID = '';
        WB_DISCUSSION = false;
        toast('原讨论组不可用，已回到独立模式');
      }
    }
    wbRender();
    wbLayoutSave();
  }catch(e){
    document.getElementById('wbWorkspace').textContent = '共享目录绑定失败';
    toast('工作台初始化失败：' + e.message);
    SP.forEach(p => {
      const h = document.getElementById('spMsgs_' + p.id);
      if(h) h.innerHTML = '<div class="sp-msg sys">工作台初始化失败：' + spEsc(e.message) + '</div>';
    });
  }
}
async function wbToggleMode(){
  if(SP.some(p => p.busy) || wbActiveTurn()) return toast('请先结束当前发言');
  if(WB_DISCUSSION){
    WB_DISCUSSION = false;
    wbRender();
    wbLayoutSave();
    return;
  }
  try{
    if(!WB_GROUP_ID){
      const d = await wbApi('/v1/discussions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workbench_id: WB_ID,
          members: SP.map(p => ({ session_id: p.sessionId, title: p.name, agent: p.agent }))
        })
      });
      WB_GROUP_ID = d.group_id;
      WB_GROUP = d.group;
    }else{
      await wbRefreshGroup();
    }
    WB_DISCUSSION = true;
    WB_TIMELINE = true;
    wbRender();
    wbLayoutSave();
  }catch(e){ toast('无法开启讨论组：' + e.message); }
}
function wbToggleTimeline(){
  WB_TIMELINE = !WB_TIMELINE;
  wbRenderTimeline();
  wbLayoutSave();
}
function wbRenderTimeline(){
  const box = document.getElementById('groupTimeline');
  if(!box) return;
  box.classList.toggle('show', WB_DISCUSSION && WB_TIMELINE);
  if(!WB_DISCUSSION || !WB_TIMELINE) return;
  const messages = WB_GROUP ? (WB_GROUP.messages || []) : [];
  box.innerHTML = messages.slice(-80).map(m =>
    '<div class="gt-row"><span class="gt-name">' + spEsc(m.name || (m.role === 'user' ? '用户' : 'AI')) +
    '</span><span class="gt-text">' + spEsc(String(m.content || '').slice(0, 1200)) + '</span></div>').join('') +
    (wbActiveTurn() ? '<div class="gt-state">本轮发言进行中，结束后会停止，等待你指定下一位。</div>' : '') +
    (!messages.length ? '<div class="gt-state">共享记录为空。选择下一位发言者，然后在成员面板输入话题。</div>' : '');
  box.scrollTop = box.scrollHeight;
}
function wbNextSpeakerChanged(){
  WB_NEXT = document.getElementById('wbNextSpeaker').value;
  wbLayoutSave();
}
async function wbToggleMember(id){
  const p = SP.find(x => x.id === id);
  if(!p || p.busy || !WB_GROUP_ID) return;
  if(wbActiveTurn()) return toast('本轮发言结束后才能修改成员');
  try{
    if(wbIsMember(p)){
      const d = await wbApi('/v1/discussions/' + WB_GROUP_ID + '/members', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'remove', session_id: p.sessionId })
      });
      WB_GROUP = d.group;
      if(WB_NEXT === p.sessionId) WB_NEXT = '';
    }else{
      await wbSyncMember(p);
    }
    wbRender();
    wbLayoutSave();
  }catch(e){ toast('修改讨论组成员失败：' + e.message); }
}
async function wbSend(id, preset, forceIndependent){
  const source = SP.find(x => x.id === id);
  if(!source || source.busy) return;
  const ta = document.querySelector('.sp-input textarea[data-id="' + id + '"]');
  const text = String(preset != null ? preset : (ta ? ta.value : source.draft)).trim();
  if(!text) return;
  const grouped = !forceIndependent && WB_DISCUSSION && wbIsMember(source);
  if(grouped && wbActiveTurn()) return toast('请等待当前发言结束');
  let actor = source;
  if(grouped){
    actor = SP.find(x => x.sessionId === WB_NEXT);
    if(!actor || !wbIsMember(actor)) return toast('请先选择下一位发言者');
    if(actor.busy) return toast('下一位发言者正在执行其他任务');
  }
  actor.busy = true;
  actor.stopped = false;
  actor.controller = new AbortController();
  actor.turnId = '';
  if(preset == null){
    source.draft = '';
    if(ta) ta.value = '';
  }
  if(grouped){
    WB_TURN_STARTING = true;
    try{
      const d = await wbApi('/v1/discussions/' + WB_GROUP_ID + '/turn', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          clientTurnId: wbUid('client_'),
          text,
          targetSessionId: actor.sessionId
        })
      });
      WB_GROUP = d.group;
      actor.turnId = d.turn.turnId;
    }catch(e){
      actor.busy = false;
      actor.controller = null;
      WB_TURN_STARTING = false;
      try{ await wbRefreshGroup(); }catch(_){}
      toast('创建讨论轮次失败：' + e.message);
      wbRender();
      return;
    }
    WB_TURN_STARTING = false;
  }
  const actorId = actor.id;
  WB_ACTIVE_ID = actorId;
  wbRender();
  const host = document.getElementById('spMsgs_' + actorId);
  addMsg('user', text, { host, persist: false, agentName: actor.name });
  wbRenderTimeline();
  const started = Date.now();
  const tick = setInterval(() => {
    const label = document.getElementById('spBusy_' + actorId);
    if(label && actor.busy){
      const sec = Math.round((Date.now() - started) / 1000);
      label.textContent = (sec >= 15 ? '模型排队/执行中… ' : '正在思考… ') + sec + 's';
    }
  }, 1000);
  const timer = setTimeout(() => { if(actor.busy) wbStop(actorId); }, 900000);
  try{
    await requestAIReply(text, [], {
      pane: actor,
      host,
      sessionId: actor.sessionId,
      mode: actor.mode,
      agent: actor.agent,
      agentName: actor.name,
      workbenchId: WB_ID,
      windowId: WB_WINDOW_ID,
      groupId: grouped ? WB_GROUP_ID : '',
      turnId: grouped ? actor.turnId : '',
      signal: actor.controller.signal
    });
  }finally{
    clearInterval(tick);
    clearTimeout(timer);
    actor.busy = false;
    actor.controller = null;
    actor.turnId = '';
    if(grouped){
      try{ await wbRefreshGroup(); }catch(e){ toast('共享记录刷新失败：' + e.message); }
    }
    await wbRefreshSessions();
    wbRender();
    wbLayoutSave();
    actor.stopped = false;
  }
}
async function wbStop(id){
  const p = SP.find(x => x.id === id);
  if(!p || !p.busy) return;
  p.stopped = true;
  const label = document.getElementById('spBusy_' + id);
  if(label) label.textContent = '正在停止…';
  const cancel = fetch('/v1/agent/cancel', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: p.sessionId,
      group_id: p.turnId ? WB_GROUP_ID : '',
      turn_id: p.turnId || ''
    })
  }).catch(() => {});
  if(p.controller) p.controller.abort();
  await cancel;
}
function wbHandoff(id, btn){
  const pane = btn.closest('.side-pane');
  const target = pane.querySelector('.sp-target').value;
  if(!target) return toast('先选择要发给哪个对话');
  const host = document.getElementById('spMsgs_' + id);
  const replies = host ? host.querySelectorAll('.msg.ai .md') : [];
  const content = replies.length ? replies[replies.length - 1].textContent.trim() : '';
  if(!content) return toast('这个对话还没有可转发的回复');
  const p = SP.find(x => x.id === id);
  const t = SP.find(x => x.id === target);
  if(!t) return;
  toast('已发送到 ' + t.name);
  wbSend(target, '【来自' + (p ? p.name : '独立对话') + '】\n' + content, true);
}

spLoad();
if(WB_MODE){
  wbInit();
}else{
  spRender();
}
pollStatus();
syncChatEmpty();
// 空状态不自动聚焦：否则会立刻把输入卡从居中位置踢到底部
if(!WB_MODE && msgExists()) input.focus();
