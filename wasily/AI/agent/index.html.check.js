
// iframe（IDE 弹层）内运行时隐藏模拟窗口栏
if (window.self !== window.top) { document.body.classList.add('in-frame'); }

/* ---------- 助手名称 / 积分入口（对齐 Trae） ---------- */
function agentDisplayName(){
  try { var n = localStorage.getItem('wasily_agent_name'); if (n && n.trim()) return n.trim(); } catch(e){}
  return 'wasily';
}
function applyAgentName(n){
  n = String(n || '').trim() || 'wasily';
  try { localStorage.setItem('wasily_agent_name', n); } catch(e){}
  var nav = document.getElementById('agentNavName'); if (nav) nav.textContent = n;
  var asst = document.getElementById('asstName'); if (asst) asst.textContent = n;
}
function openSettingsPage(section){
  var url = '/settings' + (section ? ('#' + section) : '');
  try {
    if (typeof wbApi !== 'undefined' && wbApi && wbApi.openPage) { wbApi.openPage(url, '设置'); return; }
  } catch(e){}
  window.open(url, '_blank');
}
function openNameDialog(){
  var m = document.getElementById('nameModal'), inp = document.getElementById('nameInput');
  if (!m || !inp) return;
  inp.value = agentDisplayName();
  document.getElementById('nameCount').textContent = inp.value.length;
  m.classList.add('show');
  m.style.display = 'flex';
  setTimeout(function(){ inp.focus(); inp.select(); }, 30);
}
function closeNameDialog(){
  var m = document.getElementById('nameModal');
  if (m) { m.classList.remove('show'); m.style.display = 'none'; }
}
async function saveAgentName(){
  var inp = document.getElementById('nameInput');
  var n = ((inp && inp.value) || '').trim();
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
  } catch(e){}
}
(function initAgentName(){
  applyAgentName(agentDisplayName());
  var nav = document.getElementById('agentNavName');
  if (nav) { nav.style.cursor = 'pointer'; nav.title = '双击修改名称'; nav.ondblclick = openNameDialog; }
  var inp = document.getElementById('nameInput');
  if (inp) {
    inp.addEventListener('input', function(){
      document.getElementById('nameCount').textContent = inp.value.length;
    });
    inp.addEventListener('keydown', function(e){
      if (e.key === 'Enter') { e.preventDefault(); saveAgentName(); }
      if (e.key === 'Escape') closeNameDialog();
    });
  }
  var m = document.getElementById('nameModal');
  if (m) m.addEventListener('click', function(e){ if (e.target === m) closeNameDialog(); });
  if (!localStorage.getItem('wasily_agent_name')) {
    fetch('/agent/assistant').then(function(r){ return r.json(); }).then(function(d){
      if (d && d.name) applyAgentName(d.name);
    }).catch(function(){});
  }
})();
let CATALOG = [], USER_TOOLS = [], MCP = [], SKILLS = [], CAPS = [], CAPS_USER = [],
    curChip = '全部', curTab = 'plugins';
const AGENT_PARAMS = new URLSearchParams(location.search);
const AGENT_WB_ID = String(AGENT_PARAMS.get('workbench_id') || '').trim();
const AGENT_WB_MODE = AGENT_PARAMS.get('workbench') === '1' && !!AGENT_WB_ID;

function agentChatUrl(sessionId, prompt, isNew){
  let url = '/chat?embed=agent&ipc=1';
  // 主 agent 窗口只走干净的单会话 embed，不触发对话端的工作台标签栏/独立对话面板；
  // workbench_id 仍透传，让服务端把工具输出/撤销快照归到同一个工作台。
  if(AGENT_WB_ID) url += '&workbench_id=' + encodeURIComponent(AGENT_WB_ID);
  if(sessionId) url += '&session=' + encodeURIComponent(sessionId);
  if(prompt) url += '&prompt=' + encodeURIComponent(prompt);
  if(isNew) url += '&new=1';
  return url;
}

/* 对话独立成窗：pywebview 外壳里由 Python 侧再开一个窗口，
   浏览器预览时退化为 window.open（同名窗口会复用并导航到新地址）。 */
const CHAT_WIN_NAME = 'wasily_chat';
let chatWin = null;

function openChatWindow(session, prompt, isNew){
  if(window.pywebview && window.pywebview.api && window.pywebview.api.open_chat){
    window.pywebview.api.open_chat(session || '', prompt || '', isNew ? '1' : '');
    return null;
  }
  try{
    chatWin = window.open(agentChatUrl(session, prompt, isNew), CHAT_WIN_NAME,
                          'width=560,height=900');
  }catch(e){ chatWin = null; }
  return chatWin;
}

/* ===== 跨进程通信：主窗口 ↔ 内嵌面板 / 独立窗口 =====
   两个窗口各是独立进程，localStorage 不互通，统一走后端内存队列。
   发命令用 ipcSend，收状态用 ipcPump（对话端每 3 秒上报一次）。 */
let curChatSession = '';

function ipcSend(to, action, payload){
  return fetch('/agent/ipc', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({client: 'agent', to: to, action: action, payload: payload || {}})
  }).then(r=>r.json()).catch(()=>null);
}

async function ipcPump(){
  try{
    const d = await (await fetch('/agent/ipc?client=agent', {cache: 'no-store'})).json();
    const peers = d.peers || {};
    // 可能同时存在内嵌面板与独立窗口，取最近上报的那个作为当前对话
    let pick = null;
    Object.keys(peers).forEach(k=>{
      if(!k.startsWith('chat')) return;
      if(!pick || peers[k].last > pick.last) pick = peers[k];
    });
    const st = pick && pick.state ? pick.state : null;
    const title = document.getElementById('chatTitle');
    if(!title) return;
    if(st){
      curChatSession = st.session_id || '';
      title.textContent = st.title || (st.session_id ? '对话 ' + String(st.session_id).slice(-6) : '新对话');
      title.title = st.session_id || '';
    }else if(!pick){
      title.textContent = '对话未连接';
    }
  }catch(e){}
}
setInterval(ipcPump, 2000);

function esc(s){
  return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* 统一图标入口：所有动态图标使用相同 viewBox、描边、线帽和颜色。 */
const ICON_PATHS = {
  file:'<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h6M9 17h5"/>',
  report:'<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 17v-3M12 17v-6M15 17v-4"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="M21 21l-4-4"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  tool:'<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/>',
  plugin:'<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
  monitor:'<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
  chat:'<path d="M20 15a3 3 0 0 1-3 3H9l-5 3v-6a3 3 0 0 1-1-2V7a3 3 0 0 1 3-3h11a3 3 0 0 1 3 3z"/>',
  globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  edit:'<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L8 19l-5 1 1-5z"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  close:'<path d="M6 6l12 12M18 6L6 18"/>',
  download:'<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>'
};
function icon(name){
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">${ICON_PATHS[name] || ICON_PATHS.file}</svg>`;
}
function pluginIcon(c){
  const names = {'网络':'globe','效率':'clock','创意':'edit','系统':'tool','服务':'plugin'};
  return icon(names[c && c.cat] || 'plugin');
}
function templateIcon(t){
  const name = t && t.icon;
  // 内置模板用 emoji 作图标，只有不是图标键名时才按文字渲染
  if (name && !ICON_PATHS[name] && String(name).length <= 8) return esc(String(name));
  return icon(ICON_PATHS[name] ? name : 'file');
}

function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 2200);
}

function toggleGrp(id){ document.getElementById(id).classList.toggle('closed'); }

/* 每日签到：一天只能领一次，用 localStorage 记住当天日期 */
function claimPoints(btn){
  const today = new Date().toISOString().slice(0,10);
  if (localStorage.getItem('wasily_pts_day') === today){
    toast('今天已经签到过了，明天再来');
    return;
  }
  localStorage.setItem('wasily_pts_day', today);
  btn.textContent = '今天已签到 +100';
  btn.classList.add('done');
  toast('签到成功，获得 100 积分');
}
(function restorePts(){
  const btn = document.getElementById('ptsBtn');
  if (btn && localStorage.getItem('wasily_pts_day') === new Date().toISOString().slice(0,10)){
    btn.textContent = '今天已签到 +100';
    btn.classList.add('done');
  }
})();

function openChat(prompt){
  go('chat');
  ipcSend('chat', 'prefill', {text: prompt || ''})
    .then(()=>ipcSend('chat', 'focus', {}));
}

function openProj(name){ openChat('继续处理项目：' + name); }

/* 主窗口与对话端之间没有共享存储，历史切换一律通过后端 IPC 下发，
   内嵌面板和独立窗口都会收到同一条命令并各自切换。 */
function closeHistPop(){ Array.prototype.forEach.call(document.querySelectorAll('.hist-pop'), e=>e.remove()); }

async function chatAction(id){
  if(id !== 'histBtn') return;
  if(document.querySelector('.hist-pop')){ closeHistPop(); return; }
  let list = [];
  try{
    const d = await (await fetch('/v1/agent/sessions?limit=20', {cache:'no-store'})).json();
    list = d.sessions || [];
  }catch(e){}
  const pop = document.createElement('div');
  pop.className = 'hist-pop';
  // 弹层贴着右侧图标栏的历史按钮展开，避免固定写死坐标后被工作台宽度带偏
  const anchor = document.querySelector('.agent-rail .rb[title="历史对话"]');
  const ar = anchor ? anchor.getBoundingClientRect() : null;
  if(ar){
    pop.style.right = Math.round(window.innerWidth - ar.left + 8) + 'px';
    pop.style.top = Math.round(ar.top) + 'px';
  }else{
    pop.style.right = '52px';
    pop.style.top = '56px';
  }
  pop.innerHTML = list.length ? list.map(s=>`
    <div class="hi" data-sid="${esc(s.session_id)}">
      ${esc(s.title || '空会话')}
      <div class="hm">${esc(s.source || 'web')} · ${s.count || 0} 条 · ${s.updated ? new Date(s.updated * 1000).toLocaleString('zh-CN') : ''}</div>
    </div>`).join('') : '<div class="he">还没有历史对话</div>';
  pop.addEventListener('click', ev=>{
    const it = ev.target.closest('.hi');
    if(!it) return;
    ipcSend('chat', 'set_session', {session_id: it.dataset.sid})
      .then(()=>ipcSend('chat', 'focus', {}));
    closeHistPop();
    toast('已切换到该会话');
  });
  document.body.appendChild(pop);
  setTimeout(()=>{
    document.addEventListener('click', function once(ev){
      if(!pop.contains(ev.target)){ closeHistPop(); document.removeEventListener('click', once); }
    });
  }, 0);
}

function newTask(){
  go('chat');
  ipcSend('chat', 'clear', {}).then(()=>ipcSend('chat', 'focus', {}));
}

/* 弹出：把当前对话放到独立窗口，适合边看编辑器边聊 */
function popoutChat(){ openChatWindow(curChatSession || ''); }

/* 站内页面打开：
   - 独立 Agent 窗口（Electron）：window.open 新开窗口
   - IDE webview 内：window.open 会被宿主拦截，改走 18753 桥在 IDE 里开原生标签
   两条路都失败时退化为当前页跳转，保证按钮不会“点了没反应”。 */
function openLocalPage(path, title){
  const url = 'http://127.0.0.1:8000' + path;
  // Electron 工作台：交给主进程开受控窗口并置顶，避免 window.open 被拦或开在屏幕外
  const wbApi = window.workbenchAPI;
  if (wbApi && wbApi.isElectron && typeof wbApi.openPage === 'function') {
    wbApi.openPage(path, title || 'wasily');
    return;
  }
  let embedded = false;
  try {
    embedded = (typeof acquireVsCodeApi === 'function') || (window.self !== window.top);
  } catch(e) { embedded = true; }
  if(embedded){
    fetch('http://127.0.0.1:18753/open_url?url=' + encodeURIComponent(url) +
          '&title=' + encodeURIComponent(title || 'wasily'))
      .catch(function(){ try { location.href = url; } catch(e) {} });
    return;
  }
  let w = null;
  try { w = window.open(url, '_blank'); } catch(e) { w = null; }
  if(!w){ try { location.href = url; } catch(e) {} }
}

/* ===== Trae 式顶栏与右侧胶囊的交互 ===== */
function toggleWb(){ document.body.classList.toggle('wb-hidden'); }
/* 顶栏面板图标：收起 / 展开侧栏（对齐 Trae 的标题栏切换） */
function toggleSide(){ document.body.classList.toggle('side-collapsed'); }
function toggleSideSearch(){
  document.body.classList.remove('side-collapsed');
  const box = document.getElementById('sideSearch');
  const input = document.getElementById('sideSearchInput');
  const on = box.classList.toggle('on');
  if(on){ input.focus(); }
  else { input.value = ''; filterSide(''); }
}
function filterSide(q){
  q = (q || '').trim().toLowerCase();
  document.querySelectorAll('#side .grp .ti').forEach(el=>{
    el.style.display = (!q || el.textContent.toLowerCase().indexOf(q) >= 0) ? '' : 'none';
  });
}

/* 在 IDE 里打开当前对话：走 18753 桥，未连接则提示 */
function openInIde(){
  fetch('http://127.0.0.1:18753/open_url?url=' +
        encodeURIComponent('http://127.0.0.1:8000/chat') +
        '&title=' + encodeURIComponent('wasily 对话'))
    .then(()=>toast('已在 IDE 中打开'))
    .catch(()=>toast('IDE 未连接'));
}

/* 通用小菜单：align=left 左对齐，right 右对齐 */
function popMenu(anchor, items, align){
  closeMenu();
  const pop = document.createElement('div');
  pop.className = 'menu-pop';
  items.forEach(d=>{
    if(d.sep){
      const s = document.createElement('div');
      s.className = 'mp-sep';
      pop.appendChild(s);
      return;
    }
    const it = document.createElement('div');
    it.className = 'mp-item';
    it.innerHTML = '<span>' + d.t + '</span>';
    it.onclick = () => { closeMenu(); try { d.fn && d.fn(); } catch(e) {} };
    pop.appendChild(it);
  });
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth || 180;
  pop.style.left = (align === 'right' ? Math.max(8, Math.round(r.right - w)) : Math.round(r.left)) + 'px';
  pop.style.top = Math.round(r.bottom + 6) + 'px';
  menuPop = pop;
}

function chatMore(anchor){
  popMenu(anchor, [
    { t:'新对话', fn:newTask },
    { t:'清空当前对话', fn:()=>ipcSend('chat','clear',{}) },
    { t:'复制最近回复', fn:()=>ipcSend('chat','copy_last',{}) },
    { sep:1 },
    { t:'在独立窗口打开', fn:popoutChat }
  ], 'left');
}

function railMore(anchor){
  popMenu(anchor, [
    { t:'插件市场', fn:()=>go('plugins') },
    { t:'自动化', fn:()=>go('automations') },
    { t:'模板库', fn:()=>go('templates') },
    { t:'我的助理', fn:()=>go('assistant') }
  ], 'right');
}

function go(v){
  if(v === '/chat') v = 'chat';
  const navKey = v;
  if(v === 'procs') v = 'runtime';
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
  const el = document.getElementById('pane-'+v);
  if(el) el.classList.add('on');
  document.querySelectorAll('#side .ni').forEach(n=>n.classList.toggle('on', n.dataset.v===navKey));
  if(navKey==='runtime'){ rtTab('mcp'); loadRuntime(); }
  if(navKey==='procs'){ rtTab('proc'); loadRuntime(); }
  if(v==='agents') loadAgentsPage();
  if(v==='plugins') closePlugin();
  if(v==='assistant') loadAssistant();
  if(v==='automations') loadAutomations();
  if(v==='templates') loadTemplates();
  if(v==='outputs') loadOutputs();
  if(v==='sites') loadSites();
  if(v==='chat') loadDiff();
  if(v==='kb') kbRefresh();
  history.replaceState(null,'','#'+v);
}

/* ===== 知识库页：直连本地知识库接口 /v1/kb/*（同源 8000，完全离线） ===== */
const KB_KIND_NAME = {text:'文本', pdf:'PDF', docx:'Word', xlsx:'Excel', pptx:'PPT',
                      image:'图片', doc:'Word', xls:'Excel', ppt:'PPT'};
let kbPollTimer = 0;

function kbSetStatus(text, color){
  const s = document.getElementById('kbStatus');
  if(s){ s.textContent = text; s.style.color = color || '#8f959e'; }
}
function kbSize(n){
  n = Number(n) || 0;
  if(n < 1024) return n + 'B';
  if(n < 1048576) return (n/1024).toFixed(1) + 'KB';
  if(n < 1073741824) return (n/1048576).toFixed(1) + 'MB';
  return (n/1073741824).toFixed(2) + 'GB';
}
async function kbFetch(path){
  try{
    const r = await fetch(path, { cache:'no-store' });
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  }catch(e){
    return { ok:false, error:String(e) };
  }
}

async function kbRefresh(){
  const d = await kbFetch('/v1/kb/status');
  const conn = document.getElementById('kbConn');
  if(!d.ok){
    kbSetStatus('服务不可用','#e05b5b');
    if(conn) conn.textContent = '知识库接口不可达：' + (d.error || '');
    return;
  }
  const ix = d.index || {};
  const vm = (d.vision && d.vision.model) || {};
  kbSetStatus(ix.running ? '索引中' : '就绪', ix.running ? '#e0b45b' : '#4ec97f');
  const parts = ['已索引 ' + (ix.indexed_files || 0) + ' 个文件',
                 '分块 ' + (ix.chunks || 0),
                 '库 ' + (ix.db_mb || 0) + 'MB',
                 '图片描述模型' + (vm.ready ? '已就绪' : '未下载')];
  if(conn) conn.textContent = parts.join(' · ');

  const scanBtn = document.getElementById('kbScanBtn');
  const stopBtn = document.getElementById('kbStopBtn');
  if(scanBtn) scanBtn.style.display = ix.running ? 'none' : '';
  if(stopBtn) stopBtn.style.display = ix.running ? '' : 'none';
  const prog = document.getElementById('kbProg');
  if(prog){
    if(ix.running){
      prog.style.display = '';
      prog.textContent = '阶段 ' + (ix.phase || '-') + '｜已发现 ' + (ix.found || 0) +
        '｜已索引 ' + (ix.indexed || 0) + '｜图片 ' + (ix.images || 0) +
        '｜错误 ' + (ix.errors || 0) + '｜' + (ix.current || '');
    }else{
      prog.style.display = 'none';
    }
  }
  if(!kbPollTimer){
    kbPollTimer = setInterval(function(){
      const pane = document.getElementById('pane-kb');
      if(!pane || !pane.classList.contains('on')){ clearInterval(kbPollTimer); kbPollTimer = 0; return; }
      kbRefresh();
    }, 3000);
  }
  kbLoadDocs();
}

async function kbLoadDocs(){
  const box = document.getElementById('kbDocs');
  if(!box) return;
  const kind = (document.getElementById('kbKind') || {}).value || 'all';
  const d = await kbFetch('/v1/kb/recent?limit=12' +
    (kind && kind !== 'all' ? '&kind=' + encodeURIComponent(kind) : ''));
  if(!d.ok){
    box.innerHTML = '<div class="card"><div class="cb"><div class="name">读取失败</div><div class="desc">' +
      (d.error || '') + '</div></div></div>';
    return;
  }
  const files = d.files || [];
  if(!files.length){
    box.innerHTML = '<div class="card"><div class="cb"><div class="name">还没有索引内容</div>' +
      '<div class="desc">点「开始全盘索引」，系统会只读扫描 C: 与 D: 盘并建立索引。</div></div></div>';
    return;
  }
  box.innerHTML = files.map(function(f){
    return '<div class="card"><div class="cb">' +
      '<div class="name">' + esc(f.name) + '　<span style="font-size:11px;color:#8f959e">' +
      (KB_KIND_NAME[f.kind] || f.kind) + ' · ' + kbSize(f.size) + '</span></div>' +
      '<div class="desc" style="word-break:break-all;font-family:Consolas,monospace;font-size:11px">' +
      esc(f.path) + '</div>' +
      (f.caption ? '<div class="desc" style="margin-top:4px">' + esc(f.caption) + '</div>' : '') +
      '</div></div>';
  }).join('');
}

async function kbSearch(){
  const q = (document.getElementById('kbQuery').value || '').trim();
  if(!q) return;
  const kind = (document.getElementById('kbKind') || {}).value || 'all';
  const box = document.getElementById('kbDocs');
  box.innerHTML = '<div class="card"><div class="cb"><div class="name">检索中…</div></div></div>';
  const d = await kbFetch('/v1/kb/search?q=' + encodeURIComponent(q) +
    '&kind=' + encodeURIComponent(kind) + '&limit=12');
  if(!d.ok){
    box.innerHTML = '<div class="card"><div class="cb"><div class="name">检索失败</div><div class="desc">' +
      esc(d.error || '') + '</div></div></div>';
    return;
  }
  const t = document.getElementById('kbDocsTitle');
  if(t) t.textContent = '检索结果 · 命中 ' + d.count + ' 个文件 · ' +
    d.took_ms + 'ms · ' + (d.mode === 'hybrid' ? '向量+全文' : d.mode);
  const list = d.results || [];
  if(!list.length){
    box.innerHTML = '<div class="card"><div class="cb"><div class="name">没有匹配</div>' +
      '<div class="desc">换个关键词，或先建立索引。</div></div></div>';
    return;
  }
  box.innerHTML = list.map(function(it){
    const snips = (it.snippets || []).map(function(s){
      return '<div class="desc" style="margin-top:6px;padding-left:8px;border-left:2px solid #3f5a46">' +
        esc(s.text) + '</div>';
    }).join('');
    return '<div class="card"><div class="cb">' +
      '<div class="name">' + esc(it.name) + '　<span style="font-size:11px;color:#8f959e">' +
      (KB_KIND_NAME[it.kind] || it.kind) + ' · 相关度 ' + it.score + '</span></div>' +
      '<div class="desc" style="word-break:break-all;font-family:Consolas,monospace;font-size:11px">' +
      esc(it.path) + '</div>' + snips + '</div></div>';
  }).join('');
}

async function kbStartScan(){
  const d = await fetch('/v1/kb/scan', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:'{}'})
    .then(function(r){ return r.json(); }).catch(function(e){ return {ok:false, error:String(e)}; });
  if(!d.ok){ alert('启动失败：' + (d.error || '')); return; }
  kbRefresh();
}
async function kbStopScan(){
  await fetch('/v1/kb/stop', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .catch(function(){});
  kbRefresh();
}

function switchTab(t){
  curTab = t;
  document.querySelectorAll('#pane-plugins .tab').forEach(e=>e.classList.toggle('on', e.dataset.t===t));
  document.getElementById('tab-plugins').style.display = t==='plugins'?'':'none';
  document.getElementById('tab-skills').style.display = t==='skills'?'':'none';
  document.getElementById('tab-mcp').style.display = t==='mcp'?'':'none';
  document.getElementById('tab-caps').style.display = t==='caps'?'':'none';
  document.querySelector('#pane-plugins .sbox').style.display = t==='caps'?'none':'';
  if(t==='skills') loadSkills();
  if(t==='mcp') loadMcpTools();
  if(t==='caps') loadCaps();
}

/* ---------------- 插件市场 ---------------- */
const CAT_META = {
  '系统':  {title:'研发工具', illu:'linear-gradient(135deg,#e8f0ff 0%,#f5f9ff 55%,#ffffff 100%)'},
  '服务':  {title:'研发工具', illu:'linear-gradient(135deg,#e8f0ff 0%,#f5f9ff 55%,#ffffff 100%)'},
  '创意':  {title:'内容创作', illu:'linear-gradient(135deg,#ffe9ec 0%,#fff6f3 55%,#ffffff 100%)'},
  '网络':  {title:'数据分析', illu:'linear-gradient(135deg,#e6f7f0 0%,#f2fbf7 55%,#ffffff 100%)'}
};
const CHIP_ORDER = ['全部','推荐','开发工具','效率提升','界面设计','调研分析','内容创作','其他'];
const CHIP_MAP = { '系统':'开发工具', '服务':'开发工具', '创意':'内容创作', '网络':'调研分析' };

function chipOf(c){ return CHIP_MAP[c.cat] || '其他'; }

/* ---------------- 插件详情 ---------------- */
// key 与 plugin_store.CATALOG 对应；文案描述插件的真实行为（数据源 / 参数 / 限制）
const PLUGIN_INFO = {
  weather: {
    detail:'调用 wttr.in 免费接口获取任意城市的实时天气与当日概况，返回中文天气、温度、体感温度与湿度，不需要任何 API Key。查询时把城市名传给 city 参数，英文名（如 Shenzhen）命中率更高；网络不通会直接报错，不会返回空结果。',
    spec:{name:'weather', desc:'city → 中文天气 / 温度 / 体感 / 湿度'},
    examples:[
      {title:'查一下深圳现在的天气', prompt:'用天气插件查一下深圳现在的天气'},
      {title:'北京今天需要带伞吗', prompt:'用天气插件看看北京今天会不会下雨，需要带伞吗'}]},
  qrcode_maker: {
    detail:'把文本或链接生成 300×300 的二维码图片，并调用系统默认浏览器打开。适合快速分享网址、WiFi 密码或长文本；图片由 api.qrserver.com 生成，不会落盘，需要长期保存请另存为。',
    spec:{name:'qrcode_maker', desc:'text → 二维码 URL 并在浏览器打开'},
    examples:[
      {title:'把官网生成二维码', prompt:'把 https://wasily.ai 生成一个二维码'},
      {title:'文字转二维码', prompt:'把这段话做成二维码：Wasily 本地智能助手'}]},
  todo_list: {
    detail:'本地待办清单：add 添加、list 列出、done 完成、del 删除，数据保存在 user_tools/todo_db.json，重启后不丢。done / del 的 text 传编号（从 1 开始），编号无效时返回提示而不做改动。',
    spec:{name:'todo_list', desc:'action=add/list/done/del，text=事项或编号'},
    examples:[
      {title:'加一条待办', prompt:'把「给客户回邮件」加到待办'},
      {title:'看看今天要做什么', prompt:'列出我的待办事项'}]},
  wallpaper: {
    detail:'从必应每日图库下载当天高清壁纸，保存到用户图片目录并立即设为桌面背景，同时返回图片版权信息。只取当日第一张；需要联网，Windows 下通过 SystemParametersInfo 生效。',
    spec:{name:'wallpaper', desc:'下载必应每日图 → 设为桌面壁纸'},
    examples:[
      {title:'换一张每日壁纸', prompt:'换一张必应每日壁纸'},
      {title:'下载并设为桌面', prompt:'下载今天的必应壁纸并设为桌面背景'}]},
  sys_clean: {
    detail:'清理系统临时目录中的文件与文件夹，并尝试清空回收站，最后返回释放的空间大小。只处理临时目录内容，不动用户文档；回收站依赖 winshell，未安装时自动跳过。',
    spec:{name:'sys_clean', desc:'清理临时目录 + 尝试清空回收站，返回释放空间'},
    examples:[
      {title:'清理临时文件', prompt:'清理一下系统临时文件'},
      {title:'先看看能释放多少', prompt:'清理临时目录，并告诉我释放了多少空间'}]},
  currency: {
    detail:'调用 frankfurter.app 免费汇率接口做实时换算，支持主流货币（USD / CNY / EUR / JPY 等），返回换算结果与汇率日期。amount 为金额，src / dst 为三字母货币代码，数据为欧洲央行工作日汇率。',
    spec:{name:'currency', desc:'amount / src / dst → 换算结果 + 汇率日期'},
    examples:[
      {title:'美元换人民币', prompt:'100 美元等于多少人民币'},
      {title:'欧元换日元', prompt:'把 500 欧元换成日元'}]},
  web_snap: {
    detail:'抓取指定网页的 HTML 源码，保存到桌面 snap_<时间戳>.html 并自动打开，方便离线留档。只保存原始 HTML，脚本与图片仍引用线上地址；需要登录的页面抓不到正文，会返回 URL 错误。',
    spec:{name:'web_snap', desc:'url → 保存 HTML 到桌面并打开'},
    examples:[
      {title:'保存网页到本地', prompt:'保存这个网页到本地：https://example.com'},
      {title:'把文章存成 HTML', prompt:'把这个新闻页面抓取下来存成 HTML 打开'}]},
  file_zip: {
    detail:'把指定目录打包成 zip 压缩包，默认输出到同级目录（目录名.zip），也可以用 out 参数指定输出路径。会递归打包整个目录；体积越大耗时越长，path 不是有效目录时直接返回提示。',
    spec:{name:'file_zip', desc:'path / out → 递归打包为 zip'},
    examples:[
      {title:'打包资料目录', prompt:'把 D:\\资料 打包成 zip'},
      {title:'压缩项目目录', prompt:'压缩 D:\\项目\\demo 这个目录'}]},
  php_server: {
    detail:'用 PHP 内置服务器（php -S）把任意目录起成本地站点，支持 start / stop / status，pid 记录在 user_tools/php_server.pid。需要本机已安装 php 并加入 PATH，端口默认 8000。',
    spec:{name:'php_server', desc:'action=start/stop/status，dir=站点目录，port=端口'},
    examples:[
      {title:'用 PHP 起站点', prompt:'用 PHP 在 8080 端口起一个站点，目录是 D:\\site'},
      {title:'停掉 PHP 服务', prompt:'把刚才启动的 PHP 服务停掉'}]},
  npm_runner: {
    detail:'在指定项目目录执行 npm 命令（install / run dev / run build / test 等），返回退出码与输出末尾，便于直接排查前端工程问题。命令在 cmd 中执行，超时 10 分钟。',
    spec:{name:'npm_runner', desc:'dir / script → npm 退出码 + 输出末尾'},
    examples:[
      {title:'安装依赖', prompt:'在 D:\\proj 目录里执行 npm install'},
      {title:'构建项目', prompt:'在 D:\\proj 里帮我跑一下 npm run build'}]},
  python_server: {
    detail:'用 python -m http.server 把任意目录变成本地静态站点，启动后自动打开浏览器，默认 8080 端口。适合快速预览构建产物；关闭窗口或结束进程即停止服务。',
    spec:{name:'python_server', desc:'dir / port → 启动静态服务并打开浏览器'},
    examples:[
      {title:'预览构建产物', prompt:'把 D:\\dist 起成 8080 的本地网站'},
      {title:'临时静态站点', prompt:'起一个静态文件服务，目录是 D:\\dist，端口 8081'}]}
};
let CUR_PLUGIN = '';

function openPlugin(key){
  CUR_PLUGIN = key;
  document.getElementById('marketBody').style.display = 'none';
  document.getElementById('pluginDetail').style.display = 'block';
  renderDetail();
  document.getElementById('pane-plugins').scrollTop = 0;
}

function closePlugin(){
  CUR_PLUGIN = '';
  document.getElementById('pluginDetail').style.display = 'none';
  document.getElementById('marketBody').style.display = 'block';
  document.getElementById('pane-plugins').scrollTop = 0;
}

function renderDetail(){
  const c = CATALOG.find(x=>x.key===CUR_PLUGIN);
  const info = PLUGIN_INFO[CUR_PLUGIN] || {};
  if(!c){ closePlugin(); return; }
  const spec = info.spec || {name:c.key, desc:c.desc};
  const exs = info.examples || [];
  document.getElementById('pluginDetail').innerHTML = `
    <div class="dback" onclick="closePlugin()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M15 5l-7 7 7 7"/></svg>返回应用市场
    </div>
    <div class="dhead">
      <div class="dico">${pluginIcon(c)}</div>
      <div class="dinfo">
        <h2>${esc(c.name)}</h2>
        <div class="by">by wasily · 本机运行 · ${esc(chipOf(c))}</div>
      </div>
      <div class="dacts">
        ${c.installed ? `<div class="switch ${c.enabled?'on':''}" onclick="togglePlugin('${c.key}')"
             title="${c.enabled?'已启用，点击停用':'已停用，点击启用'}"><i></i></div>
           <button class="btn pri" onclick="tryPlugin(0)">
             <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12a8 8 0 1 1-8-8"/><path d="M21 5v5h-5"/></svg>立即体验</button>`
         : `<button class="btn pri" onclick="install('${c.key}',this)">
             <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>添加插件</button>`}
      </div>
    </div>
    <div class="ddesc">${esc(info.detail || c.desc)}</div>
    <div class="dsec">使用示例</div>
    <div class="exrow">${exs.length ? exs.map((e,i)=>`
      <div class="excard" onclick="tryPlugin(${i})">
        <div class="thumb">${pluginIcon(c)}</div>
        <div class="cap"><b>${esc(c.name)}</b>${esc(e.title)}</div>
      </div>`).join('') : '<div class="empty">暂无使用示例</div>'}</div>
    <div class="dsec">技能<span class="n">1</span></div>
    <div class="drow">
      <div class="ri"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9h10M7 13h6"/></svg></div>
      <div class="rm"><div class="rn">${esc(spec.name)}</div><div class="rd">${esc(spec.desc)}</div></div>
      <div class="switch ${c.enabled?'on':''}" onclick="togglePlugin('${c.key}')"></div>
    </div>
    <div class="dsec">MCP 服务器<span class="n">0</span></div>
    <div class="drow mute">该插件不依赖 MCP 服务器，安装后直接在对话中调用。</div>`;
}

async function togglePlugin(key){
  const c = CATALOG.find(x=>x.key===key);
  if(!c) return;
  const url = c.enabled ? '/plugin/uninstall' : '/plugin/install';
  const r = await (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key})})).json();
  toast(r.message || (r.ok?'已更新':'操作失败'));
  loadPlugins();
}

function tryPlugin(i){
  const c = CATALOG.find(x=>x.key===CUR_PLUGIN);
  if(!c) return;
  if(!c.enabled){ toast('请先添加并启用「' + c.name + '」'); return; }
  const ex = ((PLUGIN_INFO[CUR_PLUGIN]||{}).examples||[])[i];
  openChat(ex ? ex.prompt : ('用' + c.name + '插件帮我处理一下'));
}

function addPlugin(){
  toast('在对话里描述你想要的插件，wasily 会生成并热加载');
  openChat('帮我写一个自定义插件（user_tools），功能是：');
}

async function loadPlugins(){
  const d = await (await fetch('/plugins/catalog')).json();
  CATALOG = d.catalog; USER_TOOLS = d.user_tools || [];

  /* 三个分类大卡：研发工具 / 内容创作 / 数据分析 */
  const groups = {};
  for(const c of CATALOG){
    const meta = CAT_META[c.cat] || {title:'数据分析'};
    (groups[meta.title] = groups[meta.title] || {meta:meta, items:[]}).items.push(c);
  }
  const order = ['研发工具','内容创作','数据分析'];
  document.getElementById('catsRow').innerHTML = order.map(name=>{
    const g = groups[name];
    const rows = g ? g.items.slice(0,3).map(x=>
      `<div class="cr" onclick="event.stopPropagation();curChip='${chipOf(x)}';render()"><i>${pluginIcon(x)}</i>${x.name}</div>`).join('') : '';
    return `<div class="cat" onclick="curChip='${name==='研发工具'?'开发工具':(name==='内容创作'?'内容创作':'调研分析')}';render()">
      <div class="ct"><h3>${name}</h3>${rows}</div>
      <div class="illu">${icon(name==='研发工具'?'tool':(name==='内容创作'?'edit':'report'))}</div>
    </div>`;
  }).join('');

  /* 筛选胶囊：固定顺序，附带实际存在的其他分类 */
  const chips = CHIP_ORDER.slice();
  document.getElementById('chips').innerHTML = chips.map(c=>
    `<div class="chip ${c===curChip?'on':''}" onclick="curChip='${c}';render()">${c}</div>`).join('');
  render();
  if(CUR_PLUGIN) renderDetail();
}

function matchChip(c){
  if(curChip==='全部' || curChip==='推荐') return true;
  return chipOf(c) === curChip;
}

function render(){
  const kw = (document.getElementById('kw').value || '').trim();
  const list = CATALOG.filter(c => matchChip(c) &&
    (!kw || c.name.includes(kw) || (c.desc||'').includes(kw)));
  document.getElementById('sect-name').textContent =
    kw ? `搜索"${kw}" · ${list.length} 个结果` : (curChip==='全部'||curChip==='推荐' ? '推荐' : curChip);
  document.getElementById('grid').innerHTML = list.map(c=>`
    <div class="card" onclick="openPlugin('${c.key}')">
      <div class="ico">${pluginIcon(c)}</div>
      <div class="cb">
        <div class="name">${c.name}${c.installed?'<span class="tag-ok">已安装</span>':''}</div>
        <div class="desc">${c.desc||''}</div>
      </div>
      <div class="ops">
        ${c.installed
          ? `<button class="btn" onclick="event.stopPropagation();go('/chat')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 12a8 8 0 1 1 2.3 5.6L4 18l.5-2.5"/><path d="M9 12l2 2 4-4"/></svg>使用</button>
             <button class="btn ghost" onclick="event.stopPropagation();uninstall('${c.key}','${c.name}')">停用</button>`
          : `<button class="btn ghost" onclick="event.stopPropagation();install('${c.key}',this)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>安装</button>`}
      </div>
    </div>`).join('') || '<div class="empty">没有匹配的插件</div>';
  document.getElementById('ugrid').innerHTML = USER_TOOLS.length ? USER_TOOLS.map(u=>`
    <div class="card">
      <div class="ico">${icon('tool')}</div>
      <div class="cb">
        <div class="name">${u.tool||u.file}${u.enabled?'<span class="tag-ok">已启用</span>':'<span class="tag-off">已停用</span>'}</div>
        <div class="desc">${u.file}${u.error?' · 错误: '+u.error:''} · ${u.enabled?'已在对话中可用，可直接点名调用。':'已停用（_ 前缀），改名恢复。'}</div>
      </div>
      <div class="ops">${u.enabled?`<button class="btn ghost" onclick="uninstall('${u.file}','${u.file}')">停用</button>`:''}</div>
    </div>`).join('') : '<div class="empty">暂无自定义工具，可在对话中让 wasily 为自己写工具。</div>';
}

async function install(key, btn){
  btn.disabled = true; btn.textContent = '安装中…';
  const r = await (await fetch('/plugin/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})})).json();
  toast(r.message || (r.ok?'安装成功':'安装失败'));
  loadPlugins();
}
async function uninstall(key, name){
  if(!confirm(`确定停用「${name}」？（文件保留，可随时恢复）`)) return;
  const r = await (await fetch('/plugin/uninstall',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})})).json();
  toast(r.message || '已停用');
  loadPlugins();
}

async function loadSkills(){
  const [sk, mem] = await Promise.all([
    fetch('/agent/skills').then(r=>r.json()).catch(()=>({skills:[]})),
    fetch('/skills').then(r=>r.json()).catch(()=>({text:''}))
  ]);
  SKILLS = sk.skills || [];
  document.getElementById('skgrid').innerHTML = SKILLS.length ? SKILLS.map(s=>`
    <div class="card">
      <div class="ico">${icon('target')}</div>
      <div class="cb">
        <div class="name">${s.name}</div>
        <div class="desc">${s.description||''}<br>触发词：${(s.triggers||[]).join(' / ') || '（无）'}</div>
      </div>
      <div class="ops"><button class="btn" onclick="go('/chat')">去对话使用</button></div>
    </div>`).join('') : '<div class="empty">skills/ 目录下暂无技能文件</div>';
  document.getElementById('skillsBody').textContent = mem.text || '暂无已学习规则';
}

// 插件页的「MCP」标签：列工具清单；运行中心的 loadMcp 是另一套（服务/进程视图）
async function loadMcpTools(){
  if(!MCP.length) MCP = (await (await fetch('/mcp/tools')).json()).tools || [];
  document.getElementById('mgrid').innerHTML = MCP.map(t=>`
    <div class="card">
      <div class="ico">${icon('tool')}</div>
      <div class="cb">
        <div class="name">${t.name}</div>
        <div class="desc">${t.desc||''}</div>
      </div>
      <div class="ops"><button class="btn" onclick="go('/chat')">在对话中使用</button></div>
    </div>`).join('');
}

/* ---------------- IDE 能力（与对话/IDE 共用 tools.TOOLS 注册表，可真实执行） ---------------- */
const IDE_BRIDGE = 'http://127.0.0.1:18753';

// IDE ↔ Agent 联动：展示编辑器当前现场（文件/行号/选区），并提供回到 IDE 的快捷动作
// 先读服务端快照（同源、不产生网络错误），确认 IDE 在线后才直连 18753 桥，避免控制台刷 ERR_CONNECTION_REFUSED
async function loadIdeLive(){
  const box = document.getElementById('ideLive');
  if(!box) return;
  let d = null;
  try {
    const r2 = await fetch('/v1/ide/context', { cache:'no-store' });
    if(r2.ok) d = await r2.json();
  } catch(e){ /* 服务未启动 */ }
  // 不再直连 IDE 桥(18753)：桥只在 IDE 运行时存在，直连会在控制台刷 ERR_CONNECTION_REFUSED。
  // 服务端快照由插件心跳（30s）+ 编辑事件实时更新，直接用它即可。
  if(!d || !d.online){
    box.innerHTML = `<div class="rowitem"><div class="sq">${icon('monitor')}</div><div class="grow">
      <div class="t">IDE 未连接</div>
      <div class="d">启动 wasily IDE 并打开文件后，这里会显示当前编辑现场</div></div></div>`;
  } else if(!d.file){
    window.__ideCtx = d;
    box.innerHTML = `<div class="rowitem"><div class="sq">${icon('monitor')}</div>
      <div class="grow">
        <div class="t"><span class="tag-ok">在线</span> 已连接 wasily IDE</div>
        <div class="d">${esc(d.workspace || '工作区')} · 打开任意文件后会显示当前编辑现场</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="openIdePanel()">打开 IDE 面板</button>
      </div></div>`;
  } else {
    window.__ideCtx = d;
    const name = String(d.file).split(/[\\/]/).pop();
    const ln = d.line;
    const line = (ln && ln.length === 2) ? ('L' + ln[0] + (ln[1] !== ln[0] ? '-' + ln[1] : '')) : '';
    box.innerHTML = `<div class="rowitem"><div class="sq">${icon('monitor')}</div>
      <div class="grow">
        <div class="t">${d.online ? '<span class="tag-ok">在线</span>' : '<span class="tag-off">离线</span>'}
          ${esc(name)} <span style="color:#8f959e;font-weight:400;margin-left:4px">${line}</span></div>
        <div class="d">${esc(d.file)}</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="openIdeFile()">打开当前文件</button>
        <button class="btn" onclick="openIdePanel()">打开 IDE 面板</button>
      </div></div>`;
  }
  // 最近的 IDE 会话：只切换内嵌对话，保留独立 Agent 窗口的导航外壳
  try {
    const rs = await fetch('/v1/agent/sessions?prefix=ide:&limit=5', { cache:'no-store' });
    const js = await rs.json();
    (js.sessions || []).filter(s => s.source === 'ide').slice(0, 3).forEach(s => {
      const row = document.createElement('div');
      row.className = 'rowitem';
      row.style.cursor = 'pointer';
      const t = s.updated ? new Date(s.updated * 1000).toLocaleString('zh-CN') : '';
      row.innerHTML = `<div class="sq">${icon('chat')}</div><div class="grow">
        <div class="t">${esc(s.title || '空会话')}</div>
        <div class="d">${t} · ${s.count} 条</div></div>
        <div style="color:#4daafc;font-size:12px">接着聊 →</div>`;
      row.onclick = () => {
        go('chat');
        ipcSend('chat', 'set_session', {session_id: s.session_id})
          .then(()=>ipcSend('chat', 'focus', {}));
      };
      box.appendChild(row);
    });
  } catch(e){ /* 忽略 */ }
}

async function openIdeFile(){
  const d = window.__ideCtx;
  if(!d || !d.file){ toast('IDE 未连接'); return; }
  try {
    await fetch(IDE_BRIDGE + '/open_file', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ path: d.file, line: (d.line && d.line[0]) || 0 }) });
    toast('已在 IDE 中打开');
  } catch(e){ toast('IDE 未连接：' + e.message); }
}

async function openIdePanel(){
  try {
    await fetch(IDE_BRIDGE + '/command', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ command: 'wasily-ai.openPanel' }) });
    toast('已唤出 IDE 对话面板');
  } catch(e){ toast('IDE 未连接：' + e.message); }
}

async function loadCaps(){
  loadIdeLive();
  // 每 15 秒自愈刷新：插件心跳 30s 一次，页面打开时可能还没收到首次上报，稍后会自动变「已连接」
  if(!window.__ideLiveTimer){
    window.__ideLiveTimer = setInterval(function(){
      if(document.visibilityState === 'visible') loadIdeLive();
    }, 15000);
  }
  const sum = document.getElementById('capsSum'), body = document.getElementById('capsBody');
  let d;
  try { d = await (await fetch('/agent/capabilities')).json(); }
  catch(e){ sum.textContent = '能力清单加载失败：' + e.message; return; }
  CAPS = d.tools || []; CAPS_USER = d.user_tools || [];
  sum.textContent = `共 ${d.total||CAPS.length} 项本机能力，与对话窗口 / IDE 面板共用同一套工具注册表。`
    + `点「运行」填入 JSON 参数即可当场真实执行。`;
  const groups = {};
  for(const t of CAPS) (groups[t.group] = groups[t.group] || []).push(t);
  let html = Object.keys(groups).map(g=>`
    <div class="sect" style="margin-top:22px">${esc(g)} · ${groups[g].length} 项</div>
    <div class="grid">${groups[g].map(capCard).join('')}</div>`).join('');
  if(CAPS_USER.length){
    html += `<div class="sect" style="margin-top:26px">已安装自定义工具 · ${CAPS_USER.length} 项</div>
      <div class="list">${CAPS_USER.map(u=>`
        <div class="rowitem"><div class="sq">${icon('plugin')}</div>
          <div class="grow"><div class="t">${esc(u.tool||u.file)}
            ${u.enabled?'<span class="tag-ok">已启用</span>':'<span class="tag-off">已停用</span>'}</div>
            <div class="d">${esc(u.file)}${u.error?' · 错误: '+esc(u.error):''}</div></div>
        </div>`).join('')}</div>`;
  }
  body.innerHTML = html;
}

function capCard(c){
  const ps = Object.keys(c.params || {});
  const id = 'cap-' + c.name;
  return `<div class="card capc">
    <div class="ico">${icon('tool')}</div>
    <div class="cb">
      <div class="name">${esc(c.name)}</div>
      <div class="desc">${esc(c.desc)}</div>
      <div class="pv">${ps.length ? ps.map(p=>
        `<span class="pt" title="${esc(c.params[p])}">${esc(p)}</span>`).join('')
        : '<span class="pt">无参数</span>'}</div>
      <div class="capEdit" id="ed-${id}" style="display:none">
        <textarea class="capArgs" id="args-${id}" spellcheck="false"
          title="JSON 参数，如 {&quot;path&quot;: &quot;.&quot;}"></textarea>
        <div class="capOps">
          <button class="btn pri" onclick="runCap('${esc(c.name)}')">执行</button>
          <button class="btn ghost" onclick="toggleCap('${esc(c.name)}')">收起</button>
        </div>
        <pre class="capRes" id="res-${id}"></pre>
      </div>
    </div>
    <div class="ops"><button class="btn" onclick="toggleCap('${esc(c.name)}')">运行</button></div>
  </div>`;
}

function toggleCap(n){
  const ed = document.getElementById('ed-cap-' + n);
  if(!ed) return;
  const opening = ed.style.display === 'none';
  ed.style.display = opening ? '' : 'none';
  if(opening){
    const box = document.getElementById('args-cap-' + n);
    if(!box.value) box.value = '{}';
    const c = CAPS.find(x=>x.name===n);
    const hint = c && Object.keys(c.params||{}).length
      ? '参数：' + Object.entries(c.params).map(([k,v])=>`${k} — ${v}`).join('；')
      : '该工具无需参数，直接执行即可';
    box.title = hint;
    box.focus();
  }
}

async function runCap(n){
  const box = document.getElementById('args-cap-' + n);
  const out = document.getElementById('res-cap-' + n);
  if(!box || !out) return;
  let args = {};
  const txt = (box.value || '').trim();
  if(txt){
    try { args = JSON.parse(txt); }
    catch(e){
      out.textContent = '参数不是合法 JSON：' + e.message;
      out.classList.add('bad'); out.style.display = 'block'; return;
    }
  }
  if(args === null || typeof args !== 'object' || Array.isArray(args)){
    out.textContent = '参数必须是 JSON 对象，例如 {"path": "."}';
    out.classList.add('bad'); out.style.display = 'block'; return;
  }
  out.classList.remove('bad');
  out.style.display = 'block'; out.textContent = '执行中…';
  try{
    const r = await (await fetch('/agent/tool_run', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name:n, args})
    })).json();
    out.textContent = r.ok ? (r.result || '(无输出)') : ('执行失败：' + (r.error || '未知错误'));
    out.classList.toggle('bad', !r.ok);
    toast(r.ok ? `已执行 ${n}` : `${n} 执行失败`);
  }catch(e){
    out.textContent = '请求失败：' + e.message;
    out.classList.add('bad');
  }
}

/* ---------------- 我的助理 ---------------- */
async function loadAssistant(){
  const d = await (await fetch('/agent/assistant')).json();
  const layers = d.memory && d.memory.layers || {};
  document.getElementById('asstKv').innerHTML = `
    <div class="box"><div class="k">助理名称</div><div class="v" style="font-size:15px;display:flex;align-items:center;gap:8px"><span id="asstName">${d.name||'wasily'}</span><button class="btn" style="height:24px;padding:0 8px;font-size:11.5px" onclick="openNameDialog()">编辑</button></div></div>
    <div class="box"><div class="k">模型</div><div class="v" style="font-size:14px">${(d.model||'').split(/[\\\\/]/).pop()||'-'}</div></div>
    <div class="box"><div class="k">上下文窗口</div><div class="v">${d.n_ctx||'-'}<small>tokens</small></div></div>
    <div class="box"><div class="k">可用工具</div><div class="v">${d.tools||0}<small>个（含 ${d.user_tools||0} 个自定义）</small></div></div>
    <div class="box"><div class="k">已装技能</div><div class="v">${d.skills||0}<small>个</small></div></div>
    <div class="box"><div class="k">长期记忆</div><div class="v">${(layers.total!=null?layers.total:0)}<small>条（L1-L3 分层）</small></div></div>
    <div class="box"><div class="k">运行方式</div><div class="v" style="font-size:14px">${d.runtime||'本地 CPU 推理'}</div></div>
    <div class="box"><div class="k">当前窗口占用</div><div class="v">${d.context_pct!=null?d.context_pct.toFixed(1):'-'}<small>%</small></div></div>`;
  const sk = await (await fetch('/agent/skills')).json();
  document.getElementById('asstSkills').innerHTML = (sk.skills||[]).map(s=>`
    <div class="rowitem"><div class="sq">${icon('target')}</div>
      <div class="grow"><div class="t">${s.name}</div><div class="d">${s.description||''} · 触发词：${(s.triggers||[]).join(' / ')}</div></div></div>
  `).join('') || '<div class="empty">暂无技能</div>';
}

/* ---------------- 智能体 + 审批模式 ---------------- */
const PERM_MODES = [
  {k:'full',   t:'完全访问', d:'所有工具调用自动批准，不再询问。适合完全信任的本地任务。'},
  {k:'auto',   t:'自动审批', d:'仅高危操作（删除、管理员命令、注册表等）需要确认，其余自动放行。'},
  {k:'manual', t:'手动审批', d:'除只读工具外，每次调用都需人工批准。'},
  {k:'custom', t:'自定义',   d:'按自定义规则表逐工具指定 allow / confirm / auto。'},
];
let AGENTS_DATA = [], AGENT_TOOLS = [], PERM_MODE = 'auto', editingSlug = null;

async function loadAgentsPage(){
  loadPerm();
  loadAgentList();
}

async function loadPerm(){
  try {
    const d = await (await fetch('/agent/permission')).json();
    PERM_MODE = d.mode || 'auto';
    renderPermModes(d.rules || {});
  } catch(e) { /* 服务未就绪时静默 */ }
}

function renderPermModes(rules){
  document.getElementById('permModes').innerHTML = PERM_MODES.map(m=>`
    <div class="permCard ${PERM_MODE===m.k?'on':''}" onclick="setPermMode('${m.k}')">
      <div class="pt"><span class="rd"></span>${m.t}</div>
      <div class="pd">${m.d}</div>
    </div>`).join('');
  const box = document.getElementById('permRulesBox');
  box.style.display = PERM_MODE==='custom' ? '' : 'none';
  if (PERM_MODE==='custom') {
    document.getElementById('permRules').value =
      Object.entries(rules).map(([k,v])=>`${k}=${v}`).join('\n');
  }
}

async function setPermMode(k){
  const r = await (await fetch('/agent/permission/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:k})})).json();
  if(r.ok){ PERM_MODE = k; toast('审批模式已切换'); loadPerm(); }
  else toast(r.error||'切换失败');
}

async function savePermRules(){
  const lines = document.getElementById('permRules').value.split('\n');
  const rules = {};
  for(const ln of lines){
    const m = ln.match(/^\s*([A-Za-z0-9_*\-]+)\s*=\s*(allow|confirm|auto)\s*$/);
    if(m) rules[m[1]] = m[2];
  }
  const r = await (await fetch('/agent/permission/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:'custom', rules})})).json();
  toast(r.ok ? '规则已保存' : (r.error||'保存失败'));
}

async function loadAgentList(){
  const d = await (await fetch('/agent/agents')).json();
  AGENTS_DATA = d.agents || [];
  AGENT_TOOLS = d.tools || [];
  document.getElementById('agentList').innerHTML = AGENTS_DATA.length ? AGENTS_DATA.map(a=>`
    <div class="rowitem">
      <div class="grow">
        <div class="t">${esc(a.name)} <span style="color:#8f959e;font-weight:400">@${esc(a.slug)}</span>
          ${a.callable?'<span class="tag-ok">可被调用</span>':''}</div>
        <div class="d">${esc(a.when||'未填写调用时机')} · 工具 ${a.tools&&a.tools.length?a.tools.length+' 个':'全部继承'}</div>
        <div class="d" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:640px">${esc((a.prompt||'').slice(0,80))}</div>
      </div>
      <div class="ops">
        <button class="btn" onclick="editAgent('${esc(a.slug)}')">编辑</button>
        <button class="btn" onclick="deleteAgent('${esc(a.slug)}','${esc(a.name)}')">删除</button>
      </div>
    </div>`).join('') : '<div class="empty">还没有自定义智能体，点击右上角「创建智能体」开始</div>';
}

function renderToolGrid(selected){
  const sel = new Set(selected || []);
  document.getElementById('agTools').innerHTML = AGENT_TOOLS.map(t=>
    `<label><input type="checkbox" value="${esc(t)}" ${sel.has(t)?'checked':''} onchange="agToolsCount()">${esc(t)}</label>`
  ).join('');
  agToolsCount();
}
function agToolsCount(){
  const n = document.querySelectorAll('#agTools input:checked').length;
  document.getElementById('agToolsCount').textContent = n ? `已选 ${n} 个` : '未勾选 = 继承全部工具';
}
function agToolsAll(on){
  document.querySelectorAll('#agTools input').forEach(c=>c.checked=on);
  agToolsCount();
}

function openAgentForm(a){
  editingSlug = a ? a.slug : null;
  document.getElementById('agentFormTitle').textContent = a ? ('编辑智能体 @' + a.slug) : '创建智能体';
  document.getElementById('agName').value = a ? a.name : '';
  document.getElementById('agSlug').value = a ? a.slug : '';
  document.getElementById('agSlug').disabled = !!a;
  document.getElementById('agWhen').value = a ? (a.when||'') : '';
  document.getElementById('agPrompt').value = a ? (a.prompt||'') : '';
  document.getElementById('agCallable').checked = a ? !!a.callable : false;
  renderToolGrid(a ? (a.tools||[]) : []);
  document.getElementById('agentFormWrap').style.display = '';
  document.getElementById('agentFormWrap').scrollIntoView({behavior:'smooth', block:'start'});
}
function closeAgentForm(){ document.getElementById('agentFormWrap').style.display = 'none'; editingSlug = null; }

function editAgent(slug){
  const a = AGENTS_DATA.find(x=>x.slug===slug);
  if(a) openAgentForm(a);
}

async function saveAgent(){
  const payload = {
    name: document.getElementById('agName').value.trim(),
    slug: document.getElementById('agSlug').value.trim().toLowerCase(),
    when: document.getElementById('agWhen').value.trim(),
    prompt: document.getElementById('agPrompt').value.trim(),
    callable: document.getElementById('agCallable').checked,
    tools: Array.from(document.querySelectorAll('#agTools input:checked')).map(c=>c.value),
  };
  if(!payload.name || !payload.slug || !payload.prompt){ toast('名称、英文标识名、提示词为必填'); return; }
  const r = await (await fetch('/agent/agent/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)})).json();
  if(r.ok){ toast('已保存智能体'); closeAgentForm(); loadAgentList(); }
  else toast(r.error||'保存失败');
}

async function deleteAgent(slug, name){
  if(!confirm(`确认删除智能体「${name}」？此操作不可恢复。`)) return;
  const r = await (await fetch('/agent/agent/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({slug})})).json();
  if(r.ok){ toast('已删除'); loadAgentList(); }
  else toast(r.error||'删除失败');
}

/* ---------------- 自动化 ---------------- */
async function loadAutomations(){
  const d = await (await fetch('/agent/automations')).json();
  const list = d.automations || [];
  document.getElementById('auList').innerHTML = list.length ? list.map(a=>`
    <div class="rowitem">
      <div class="grow">
        <div class="t">${a.name}${a.enabled?'<span class="tag-ok">运行中</span>':'<span class="tag-off">已暂停</span>'}</div>
        <div class="d">每 ${a.interval_min} 分钟 · ${a.prompt}</div>
        <div class="d">上次执行：${a.last_run||'从未'}${a.last_result?' · '+a.last_result:''}</div>
      </div>
      <button class="btn" onclick="runAutomation('${a.id}')">立即执行</button>
      <button class="btn" onclick="toggleAutomation('${a.id}',${!a.enabled})">${a.enabled?'暂停':'启用'}</button>
      <button class="btn" onclick="deleteAutomation('${a.id}','${a.name}')">删除</button>
    </div>`).join('') : '<div class="empty">还没有自动化任务</div>';
}
async function saveAutomation(){
  const name = document.getElementById('auName').value.trim();
  const prompt = document.getElementById('auPrompt').value.trim();
  const interval_min = parseInt(document.getElementById('auMin').value || '60', 10);
  const enabled = document.getElementById('auOn').checked;
  if(!name || !prompt){ toast('请填写任务名称和提示词'); return; }
  const r = await (await fetch('/agent/automation/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name, prompt, interval_min, enabled})})).json();
  toast(r.ok ? '已创建自动化' : (r.message||'创建失败'));
  if(r.ok){ document.getElementById('auName').value=''; document.getElementById('auPrompt').value=''; loadAutomations(); }
}
async function runAutomation(id){
  toast('已触发，后台执行中…');
  const r = await (await fetch('/agent/automation/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id})})).json();
  toast(r.message || (r.ok?'已开始执行':'触发失败'));
  setTimeout(loadAutomations, 1500);
}
async function toggleAutomation(id, enabled){
  await fetch('/agent/automation/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id, enabled})});
  loadAutomations();
}
async function deleteAutomation(id, name){
  if(!confirm(`删除自动化「${name}」？`)) return;
  await fetch('/agent/automation/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id})});
  loadAutomations();
}

/* ---------------- 模板库 ---------------- */
let TPLS = [], CUR_TPL_CAT = '全部', EDIT_TPL_ID = '', TPL_Q = '';

async function loadTemplates(){
  const d = await (await fetch('/agent/templates')).json();
  TPLS = d.templates || [];
  renderTplChips();
  renderTplGrid();
}
function tplCats(){
  const set = new Set(['全部']);
  TPLS.forEach(t => { if(t.category) set.add(t.category); });
  return Array.from(set);
}
function renderTplChips(){
  document.getElementById('tplChips').innerHTML = tplCats().map(c =>
    `<div class="chip${c===CUR_TPL_CAT?' on':''}" onclick="pickTplCat(${JSON.stringify(c).replace(/"/g,'&quot;')})">${esc(c)}</div>`
  ).join('');
}
function pickTplCat(c){ CUR_TPL_CAT = c; renderTplChips(); renderTplGrid(); }
function tplMatch(t){
  if(CUR_TPL_CAT !== '全部' && (t.category||'') !== CUR_TPL_CAT) return false;
  if(!TPL_Q) return true;
  return [t.name, t.desc, t.category, (t.tags||[]).join(' ')]
    .join(' ').toLowerCase().indexOf(TPL_Q) >= 0;
}
function onTplSearch(v){ TPL_Q = String(v||'').trim().toLowerCase(); renderTplGrid(); }
const TPL_ACCENTS = ['#3b82f6','#8b5cf6','#10b981','#f59e0b','#ef4444','#06b6d4','#ec4899','#84cc16'];
// 按名称哈希取一个稳定的主题色，同一模板每次渲染颜色一致
function tplAccent(t){
  const s = (t.name||'') + (t.category||'');
  let h = 0;
  for (let i=0;i<s.length;i++) h = (h*31 + s.charCodeAt(i)) % 9973;
  return TPL_ACCENTS[h % TPL_ACCENTS.length];
}
// 缩略图：用元数据拼一张「文档纸」，高度不同形成瀑布流感
const TPL_THUMB_H = [268, 312, 286, 330, 254, 296];
function tplThumbHtml(t, i){
  const accent = tplAccent(t);
  const h = TPL_THUMB_H[i % TPL_THUMB_H.length];
  const lines = [96, 82, 90, 68, 88, 74, 58].map(w => `<i style="width:${w}%"></i>`).join('');
  return `<div class="tpl-thumb" style="height:${h}px">
    <div class="tpl-paper">
      <div class="tp-head"><span class="tp-dot" style="background:${accent}"></span>
        <span class="tp-t">${esc(t.name)}</span></div>
      <div class="tp-sub">${esc(t.desc||'')}</div>
      <div class="tp-chips">${(t.tags||[]).slice(0,3).map(x=>`<span>${esc(x)}</span>`).join('')}</div>
      <div class="tp-lines">${lines}</div>
      <div class="tp-block" style="background:${accent}14;border-color:${accent}33"></div>
    </div>
  </div>`;
}
function renderTplGrid(){
  const list = TPLS.filter(tplMatch);
  document.getElementById('tplGrid').innerHTML = list.map((t,i)=>`
    <div class="tpl-card" onclick="openTplDetail(${TPLS.indexOf(t)})">
      ${tplThumbHtml(t, i)}
      <div class="tpl-meta">
        <div class="tpl-name">${esc(t.name)}</div>
        <div class="tpl-foot">
          <span class="tpl-kind">&lt;/&gt; <b>${esc(t.category||'通用')}</b></span>
          <span class="tpl-star" title="收藏" onclick="event.stopPropagation()">☆</span>
        </div>
      </div>
    </div>`).join('') || `<div class="empty">${TPL_Q?'没有匹配的模板':'暂无模板'}</div>`;
}

/* ---------- 模板详情浮层 ---------- */
let CUR_TPL_IDX = -1;
function openTplDetail(i){
  const t = TPLS[i];
  if(!t) return;
  CUR_TPL_IDX = i;
  const accent = tplAccent(t);
  // 提示词按行拆段：以「一、二、」这类编号开头当小标题，其余当正文
  const body = String(t.prompt||'').split('\n').filter(s=>s.trim()).map(line => {
    const s = line.trim();
    const isHead = /^([一二三四五六七八九十百]+[、.]|\d+[、.]\s|#)/.test(s) || s.length <= 22 && /^[一二三四五六七八九十]/.test(s);
    return `<p${isHead?' class="h"':''}>${esc(s)}</p>`;
  }).join('') || '<p>（该模板没有填写提示词）</p>';
  document.getElementById('tplDetailPreview').innerHTML = `
    <div class="tpl-doc">
      <div class="tpl-doc-head" style="border-left-color:${accent}">
        <h2>${esc(t.name)}</h2>
        <p>${esc(t.desc||'')}</p>
      </div>
      ${(t.tags&&t.tags.length)?`<div class="tpl-doc-chips">${t.tags.map(x=>`<span>${esc(x)}</span>`).join('')}</div>`:''}
      <div class="tpl-doc-body">${body}</div>
    </div>`;
  document.getElementById('tplDetailPreview').scrollTop = 0;
  document.getElementById('tplDetailName').textContent = t.name;
  document.getElementById('tplDetailDesc').textContent = t.desc || '暂无介绍';
  const tags = [t.category].concat(t.tags||[]).filter(Boolean);
  document.getElementById('tplDetailTags').innerHTML = tags.map(x=>`<span>${esc(x)}</span>`).join('');
  // 内置模板不允许直接改，编辑/删除入口隐藏
  const eb = document.getElementById('tplDetailEdit');
  eb.style.display = t.custom ? '' : 'none';
  document.getElementById('tplDetailDel').style.display = t.custom ? '' : 'none';
  document.getElementById('tplDetailUse').onclick = () => { closeTplDetail(); useTplAt(i); };
  document.getElementById('tplDetail').classList.add('on');
}
function closeTplDetail(){
  document.getElementById('tplDetail').classList.remove('on');
  CUR_TPL_IDX = -1;
}
function tplDetailEdit(){
  const t = TPLS[CUR_TPL_IDX];
  if(!t) return;
  closeTplDetail();
  openTplEditor(t);
}
function tplDetailDel(){
  const t = TPLS[CUR_TPL_IDX];
  if(!t || !t.custom) return;
  closeTplDetail();
  delTpl(t.id);
}
// Esc 关闭详情浮层
document.addEventListener('keydown', e => {
  if(e.key === 'Escape') closeTplDetail();
});
function tplCopyPrompt(){
  const t = TPLS[CUR_TPL_IDX];
  if(!t) return;
  const done = () => toast('提示词已复制');
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(t.prompt||'').then(done).catch(done);
  }else{
    const ta = document.createElement('textarea');
    ta.value = t.prompt||''; document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); }catch(e){}
    ta.remove(); done();
  }
}
function useTplAt(i){ const t = TPLS[i]; if(t) openChat(t.prompt || ''); }
function useTemplate(prompt){ openChat(prompt); }

function openTplEditor(t){
  EDIT_TPL_ID = (t && t.id && !String(t.id).startsWith('b')) ? t.id : '';
  document.getElementById('tplEditorTitle').textContent = EDIT_TPL_ID ? '编辑模板' : '新建模板';
  document.getElementById('tplIcon').value = ICON_PATHS[t && t.icon] ? t.icon : 'file';
  document.getElementById('tplName').value = (t && t.name) || '';
  document.getElementById('tplDesc').value = (t && t.desc) || '';
  document.getElementById('tplCat').value = (t && t.category) || '';
  document.getElementById('tplTags').value = (t && (t.tags||[]).join(',')) || '';
  document.getElementById('tplPrompt').value = (t && t.prompt) || '';
  document.getElementById('tplHint').textContent = t && t.id && String(t.id).startsWith('b')
    ? '内置模板编辑后保存为新的自定义模板' : '';
  const box = document.getElementById('tplEditor');
  box.classList.add('on');
  box.scrollIntoView({block:'center', behavior:'smooth'});
}
function closeTplEditor(){ document.getElementById('tplEditor').classList.remove('on'); }
function editTplAt(i){ openTplEditor(TPLS[i]); }

async function saveTpl(){
  const body = {
    id: EDIT_TPL_ID,
    icon: document.getElementById('tplIcon').value || 'file',
    name: document.getElementById('tplName').value.trim(),
    desc: document.getElementById('tplDesc').value.trim(),
    category: document.getElementById('tplCat').value.trim(),
    tags: document.getElementById('tplTags').value.split(',').map(s=>s.trim()).filter(Boolean),
    prompt: document.getElementById('tplPrompt').value.trim()
  };
  if(!body.name || !body.prompt){ document.getElementById('tplHint').textContent = '名称和提示词必填'; return; }
  const r = await (await fetch('/agent/templates/save',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(!r.ok){ document.getElementById('tplHint').textContent = r.error || '保存失败'; return; }
  toast('模板已保存');
  closeTplEditor();
  loadTemplates();
}
async function delTpl(id){
  if(!confirm('删除这个自定义模板？')) return;
  const r = await (await fetch('/agent/templates/delete',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})).json();
  toast(r.ok ? '已删除' : (r.error || '删除失败'));
  loadTemplates();
}

/* 拖入文件 / 网页生成模板 */
function tplDragOver(e){ e.preventDefault(); document.getElementById('tplDrop').classList.add('over'); }
function tplDragLeave(){ document.getElementById('tplDrop').classList.remove('over'); }
function tplDrop(e){
  e.preventDefault(); tplDragLeave();
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if(f) return tplFromFile(f);
  const txt = (e.dataTransfer && e.dataTransfer.getData('text')) || '';
  if(/^https?:\/\//.test(txt.trim())){ document.getElementById('tplUrl').value = txt.trim(); return tplFromUrl(); }
  toast('请拖入 PDF / Word / 文本文件，或网页链接');
}
function tplPickFile(inp){ const f = inp.files && inp.files[0]; if(f) tplFromFile(f); inp.value = ''; }
function tplFromFile(f){
  return new Promise(resolve=>{
    const reader = new FileReader();
    reader.onload = async ()=>{
      const b64 = String(reader.result || '').split(',')[1] || '';
      await tplImport({name: f.name.replace(/\.[^.]+$/, ''), filename: f.name, content_b64: b64});
      resolve();
    };
    reader.onerror = ()=>{ toast('文件读取失败'); resolve(); };
    reader.readAsDataURL(f);
  });
}
async function tplFromUrl(){
  const url = document.getElementById('tplUrl').value.trim();
  if(!url) return toast('请先填写网页地址');
  await tplImport({url});
}
async function tplImport(payload){
  const main = document.querySelector('#tplDrop .tpl-drop-main');
  const old = main.innerHTML;
  main.textContent = '正在解析并生成模板…';
  try{
    const r = await (await fetch('/agent/templates/import',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})).json();
    if(!r.ok){ toast(r.error || '解析失败'); return; }
    openTplEditor({icon:r.icon, name:r.name, desc:r.desc, category:'导入', prompt:r.prompt, id:''});
    document.getElementById('tplHint').textContent =
      '已提取 ' + (r.outline||[]).length + ' 个章节 / ' + (r.chars||0) + ' 字，确认后保存';
  }catch(e){ toast('解析失败：' + e.message); }
  finally{ main.innerHTML = old; }
}

/* ---------------- 我的产物：本地 / 云端 / 飞书 ---------------- */
let OUT_ITEMS = [], OUT_SRC = 'local', OUT_KIND = '全部';
const OUT_KINDS = ['全部', '文档', '图片', '视频', '代码', '其他'];
const KIND_EXT = {
  文档: ['pdf','doc','docx','xls','xlsx','ppt','pptx','md','txt','csv'],
  图片: ['png','jpg','jpeg','gif','webp','svg','bmp'],
  视频: ['mp4','mov','webm','mkv','avi'],
  代码: ['js','ts','py','html','css','json','jsx','tsx','java','go','rs','sh']
};

function outKind(name){
  const ext = String(name || '').split('.').pop().toLowerCase();
  for(const k of Object.keys(KIND_EXT)){ if(KIND_EXT[k].indexOf(ext) >= 0) return k; }
  return '其他';
}
function outIsImg(name){
  return KIND_EXT['图片'].indexOf(String(name || '').split('.').pop().toLowerCase()) >= 0;
}

function outTab(src){
  OUT_SRC = src;
  OUT_KIND = '全部';
  document.querySelectorAll('#outTabs .tab').forEach(t=>t.classList.toggle('on', t.dataset.o===src));
  renderOutputs();
}

function outChip(kind){ OUT_KIND = kind; renderOutputs(); }

function renderOutputs(){
  const grid = document.getElementById('outGrid');
  document.getElementById('outChips').innerHTML = OUT_KINDS
    .map(k=>`<div class="chip ${k===OUT_KIND?'on':''}" onclick="outChip('${k}')">${k}</div>`).join('');

  // 云端/飞书没有凭据来源，只做接入引导，不伪造条目
  if(OUT_SRC !== 'local'){
    const txt = OUT_SRC === 'cloud'
      ? ['云端存储未连接', '在设置里填入对象存储地址与密钥后，上传到云端的产物会显示在这里。', '打开设置 → 存储']
      : ['飞书未连接', '授权飞书账号后，云文档、多维表格与妙记里的产物会汇总到这里。', '去授权飞书 → 账号'];
    grid.innerHTML = `<div class="out-empty"><b>${txt[0]}</b>${txt[1]}<br>${txt[2]}</div>`;
    return;
  }

  const items = OUT_ITEMS.filter(f=>OUT_KIND === '全部' || outKind(f.name) === OUT_KIND);
  if(!items.length){
    grid.innerHTML = '<div class="out-empty"><b>还没有产物</b>让 wasily 做点东西，或把文件放进 agent/outputs/ 后再刷新。</div>';
    return;
  }
  grid.innerHTML = items.map(f=>`
    <div class="out-card" title="${f.name}"
         onclick="window.open('/agent/output?path='+encodeURIComponent('${f.path}'),'_blank')">
      <div class="out-thumb">${outIsImg(f.name)
        ? `<img src="/agent/output?path=${encodeURIComponent(f.path)}" loading="lazy" alt="">`
        : icon(outKind(f.name) === '代码' ? 'edit' : 'file')}</div>
      <div class="out-meta">
        <div class="out-name">${f.name}</div>
        <div class="out-sub"><span>${f.size_kb} KB</span><span>${f.mtime || ''}</span>
          <span class="out-kind">${outKind(f.name)}</span></div>
      </div>
    </div>`).join('');
}

async function loadOutputs(){
  try{
    const d = await (await fetch('/agent/outputs', {cache: 'no-store'})).json();
    OUT_ITEMS = d.outputs || [];
  }catch(e){ OUT_ITEMS = []; }
  renderOutputs();
}

/* ---------------- 我的站点：本地端口站点 + cloudflared 公网穿透 ---------------- */
let SITES = [], SITE_VIEW = 'grid', SITES_TUNNEL = false, SITES_LAN = '';
let SITE_DETAIL = null, DEPLOY_TARGET = null;
const escHtml = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function setSiteView(v){
  SITE_VIEW = v === 'list' ? 'list' : 'grid';
  document.getElementById('vtGrid').classList.toggle('on', SITE_VIEW === 'grid');
  document.getElementById('vtList').classList.toggle('on', SITE_VIEW === 'list');
  renderSites();
}

function fmtSiteDate(ts){
  if(!ts) return '';
  const d = new Date(ts * 1000), p = n => String(n).padStart(2, '0');
  return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function siteUrl(s){ return s.url_lan || s.url_local || ''; }

function openSite(id){
  const s = SITES.find(x => x.id === id);
  const u = s && siteUrl(s);
  if(u) window.open(u, '_blank'); else toast('站点未运行');
}

async function loadSites(){
  try{
    const d = await (await fetch('/v1/sites', {cache:'no-store'})).json();
    SITES = d.sites || [];
    SITES_TUNNEL = !!d.tunnel_ready;
    SITES_LAN = d.lan_ip || '';
  }catch(e){ SITES = []; }
  renderSites();
}

function renderSites(){
  const grid = document.getElementById('siteGrid');
  if(!grid) return;
  const q = (document.getElementById('siteSearch').value || '').trim().toLowerCase();
  const st = document.getElementById('siteStatus').value;
  const list = SITES.filter(s =>
    (!q || (s.name || '').toLowerCase().indexOf(q) >= 0 || (siteUrl(s) || '').indexOf(q) >= 0) &&
    (!st || s.status === st));
  grid.classList.toggle('list', SITE_VIEW === 'list');
  if(!list.length){
    grid.innerHTML = SITES.length
      ? '<div class="sites-empty"><b>没有匹配的站点</b>换个关键词或状态再试试。</div>'
      : '<div class="sites-empty"><b>还没有站点</b>点右上角「新建部署」，和 AI 说一句话就能生成网站并上线。</div>';
    return;
  }
  grid.innerHTML = list.map(s => {
    const running = s.status === 'running';
    const url = siteUrl(s);
    return '<div class="site-card" onclick="showSiteDetail(\'' + s.id + '\')">' +
      '<div class="sc-thumb">' +
        (running && url ? '<iframe src="' + escHtml(url) + '" loading="lazy" scrolling="no"></iframe>' : '') +
        '<span class="sc-badge"><i></i>' + escHtml(s.name) + '</span>' +
        '<div class="sc-hover">' +
          '<button class="sc-ico" title="打开站点" onclick="event.stopPropagation();window.open(\'' + url + '\')">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">' +
            '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.5"/>' +
            '<path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg></button>' +
          '<button class="sc-copy" onclick="event.stopPropagation();copyText(\'' + url + '\')">复制 URL</button>' +
        '</div>' +
      '</div>' +
      '<div class="sc-body">' +
        '<div class="sc-title"><b>' + escHtml(s.name) + '</b>' +
          '<span class="sc-state' + (running ? '' : ' off') + '"><i></i>' +
          (running ? '运行中' : '已停止') + '</span></div>' +
        '<div class="sc-sub"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">' +
          '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18"/></svg>' +
          (s.visibility === 'private' ? '仅自己可见' : '所有人可见') +
          ' · ' + fmtSiteDate(s.created) + (s.public_url ? ' · 公网' : '') + '</div>' +
        '<button class="sc-more" onclick="event.stopPropagation();siteMenu(event,\'' + s.id + '\')">···</button>' +
      '</div></div>';
  }).join('');
}

function openDeploy(){
  DEPLOY_TARGET = null;
  document.getElementById('dpIpHint').textContent =
    SITES_LAN ? ('http://' + SITES_LAN + ':端口/') : '本机 IP:端口';
  document.getElementById('deployMask').classList.add('on');
  setTimeout(() => document.getElementById('dpName').focus(), 50);
}
function closeDeploy(){ document.getElementById('deployMask').classList.remove('on'); }

/* 新建部署 = 一键部署技能：跳到对话页并挂上技能，由 AI 生成网页后自动部署 */
function openOneClickDeploy(){
  go('chat');
  const f = document.getElementById('chatFrame');
  const fire = () => {
    try { f.contentWindow.startOneClickDeploy('帮我创建一个网站并部署。'); }
    catch(e){ setTimeout(fire, 400); }
  };
  setTimeout(fire, 300);
}

async function deploySite(){
  const name = document.getElementById('dpName').value.trim();
  const path = document.getElementById('dpPath').value.trim();
  const vis = document.getElementById('dpVis').value;
  if(!path){ toast('请填写网站目录'); return; }
  const btn = document.getElementById('dpBtn');
  btn.disabled = true; btn.textContent = DEPLOY_TARGET ? '部署中…' : '部署中…';
  try{
    let r;
    if(DEPLOY_TARGET){
      // 已有站点「发起新部署」：保留站点和访问地址，只更新内容
      r = await (await fetch('/v1/sites/action', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'redeploy', id:DEPLOY_TARGET, path:path})
      })).json();
    } else {
      r = await (await fetch('/v1/sites/deploy', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({name:name, path:path, visibility:vis})
      })).json();
    }
    if(r.ok){
      closeDeploy();
      toast('部署成功');
      if(DEPLOY_TARGET && SITE_DETAIL){
        await showSiteDetail(SITE_DETAIL.id);
      } else {
        document.getElementById('dpName').value = '';
        document.getElementById('dpPath').value = '';
        loadSites();
      }
      DEPLOY_TARGET = null;
    } else {
      toast(r.error || '部署失败');
    }
  }catch(e){ toast('部署失败：' + e.message); }
  btn.disabled = false; btn.textContent = '部署';
}

async function siteAction(id, action, extra){
  const body = Object.assign({action:action, id:id}, extra || {});
  const r = await (await fetch('/v1/sites/action', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  })).json();
  if(!r.ok){ toast(r.error || '操作失败'); return null; }
  loadSites();
  if(action !== 'delete' && SITE_DETAIL && SITE_DETAIL.id === id) showSiteDetail(id);
  return r;
}

function copyText(t){
  try{ navigator.clipboard.writeText(t); toast('已复制：' + t); }
  catch(e){
    const ta = document.createElement('textarea');
    ta.value = t; document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); toast('已复制：' + t); }catch(_){}
    ta.remove();
  }
}

async function startTunnel(id){
  if(!SITES_TUNNEL){
    toast('正在下载 cloudflared（开源穿透组件）…');
    try{
      const d = await (await fetch('/v1/sites/tunnel/download', {method:'POST'})).json();
      if(!d.ok){ toast(d.error || '下载失败'); return; }
      SITES_TUNNEL = true;
    }catch(e){ toast('下载失败：' + e.message); return; }
  }
  toast('正在建立公网映射，稍候…');
  const r = await siteAction(id, 'tunnel');
  if(r && r.public_url){ copyText(r.public_url); }
}

/* 站点管理里的映射开关 */
function toggleMapping(){
  if(!SITE_DETAIL) return;
  if(SITE_DETAIL.public_url || SITE_DETAIL.mapping) siteAction(SITE_DETAIL.id, 'untunnel');
  else startTunnel(SITE_DETAIL.id);
}

function siteMenu(ev, id){
  const s = SITES.find(x => x.id === id);
  if(!s) return;
  const running = s.status === 'running';
  const items = [
    { t:'打开站点', fn:()=>openSite(id) },
    { t:'复制局域网地址', fn:()=>copyText(siteUrl(s) || '') }
  ];
  if(s.public_url) items.push({ t:'复制公网地址', fn:()=>copyText(s.public_url) });
  else items.push({ t:SITES_TUNNEL ? '开启公网映射' : '安装并开启公网映射', fn:()=>startTunnel(id) });
  if(s.public_url || s.mapping){
    items.push({ t:'关闭公网映射', fn:()=>siteAction(id, 'untunnel') });
  }
  items.push({ sep:1 });
  items.push({ t: running ? '停止站点' : '启动站点',
               fn:()=>siteAction(id, running ? 'stop' : 'start') });
  items.push({ t:'删除站点', fn:async ()=>{
    if(!confirm('确定删除站点「' + s.name + '」？只会停止对外访问，不会删除本地文件。')) return;
    await siteAction(id, 'delete');
  }});
  popMenu(ev.currentTarget, items, 'right');
}

/* ---------------- 站点详情：观测数据 / 站点管理 / 部署历史 / 日志 ---------------- */
let SD_TAB = 'obs';
let SITE_RANGE = 0;   // 0=今天 1=昨天 7=近7天

function _rangeDay(d){
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
         '-' + String(d.getDate()).padStart(2, '0');
}

/* 按所选范围汇总观测数据（今天/昨天取自 daily 快照） */
function rangeStats(s){
  const now = new Date();
  const daily = s.daily || {};
  let label = '', list = [];
  if(SITE_RANGE === 1){
    const y = new Date(now.getTime() - 86400000);
    label = _rangeDay(y) + ' - ' + _rangeDay(y);
    if(daily[_rangeDay(y)]) list.push(daily[_rangeDay(y)]);
  } else if(SITE_RANGE === 7){
    const start = new Date(now.getTime() - 6 * 86400000);
    label = _rangeDay(start) + ' - ' + _rangeDay(now);
    for(let i = 6; i >= 0; i--){
      const k = _rangeDay(new Date(now.getTime() - i * 86400000));
      if(daily[k]) list.push(daily[k]);
    }
  } else {
    label = _rangeDay(now) + ' - ' + _rangeDay(now);
    list = [{hits: s.views || 0, bytes: s.bytes || 0,
             hourly: s.hourly || [], hourly_bytes: s.hourly_bytes || []}];
  }
  const hourly = new Array(24).fill(0), hb = new Array(24).fill(0);
  let hits = 0, bytes = 0;
  list.forEach(d => {
    hits += d.hits || 0;
    bytes += d.bytes || 0;
    (d.hourly || []).forEach((v, i) => hourly[i] += v);
    (d.hourly_bytes || []).forEach((v, i) => hb[i] += v);
  });
  return {hits, bytes, hourly, hourly_bytes: hb, label};
}

function rangeMenu(e){
  e = e || window.event;
  if(e && e.stopPropagation) e.stopPropagation();
  const anchor = e && e.currentTarget ? e.currentTarget : document.getElementById('sdRange');
  popMenu(anchor, [
    { t:'今天', fn:() => { SITE_RANGE = 0; SITE_DETAIL && showSiteDetail(SITE_DETAIL.id); } },
    { t:'昨天', fn:() => { SITE_RANGE = 1; SITE_DETAIL && showSiteDetail(SITE_DETAIL.id); } },
    { t:'近 7 天', fn:() => { SITE_RANGE = 7; SITE_DETAIL && showSiteDetail(SITE_DETAIL.id); } }
  ], 'right');
}

function fmtBytes(n){
  n = Number(n) || 0;
  if(n < 1024) return n + ' B';
  if(n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if(n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

function sdChart(values, kind){
  const W = 700, H = 180, L = 44, R = 8, T = 10, B = 22;
  const iw = W - L - R, ih = H - T - B;
  let maxV, fmt;
  if(kind === 'views'){
    maxV = Math.max(50, Math.max.apply(null, values.concat([0])));
    maxV = Math.ceil(maxV / 50) * 50;
    fmt = v => v >= 1000 ? (v / 1000) + 'k' : String(v);
  } else {
    const mx = Math.max.apply(null, values.concat([0]));
    maxV = Math.max(150 * 1024, mx);
    fmt = fmtBytes;
  }
  let s = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">';
  for(let i = 0; i <= 5; i++){
    const y = T + ih - ih / 5 * i;
    s += '<line x1="' + L + '" y1="' + y + '" x2="' + (W - R) + '" y2="' + y +
         '" stroke="#2b2c26" stroke-width="1"/>';
    s += '<text x="' + (L - 6) + '" y="' + (y + 3) +
         '" fill="#7f8478" font-size="10" text-anchor="end">' + fmt(Math.round(maxV / 5 * i)) + '</text>';
  }
  const pts = values.map((v, i) => {
    const x = L + iw * (i / 23);
    const y = T + ih - ih * Math.min(1, (Number(v) || 0) / maxV);
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  s += '<polyline points="' + pts + '" fill="none" stroke="#00d084" stroke-width="1.4"/>';
  for(let h = 0; h < 24; h += 3){
    const x = L + iw * (h / 23);
    s += '<text x="' + x + '" y="' + (H - 6) +
         '" fill="#7f8478" font-size="10" text-anchor="middle">' + h + '时</text>';
  }
  return s + '</svg>';
}

async function showSiteDetail(id){
  let r;
  try{ r = await (await fetch('/v1/sites/detail?id=' + encodeURIComponent(id),
                              {cache:'no-store'})).json(); }
  catch(e){ toast('加载失败：' + e.message); return; }
  if(!r.ok){ toast(r.error || '加载失败'); return; }
  const s = r.site;
  SITE_DETAIL = s;
  document.getElementById('pane-sites').classList.add('detail-open');
  document.getElementById('sitesDetail').style.display = '';
  const url = s.url_domain || siteUrl(s) || '';
  const rs = rangeStats(s);
  document.getElementById('sdName').textContent = s.name;
  const st = document.getElementById('sdStatus');
  st.className = 'sd-run' + (s.status === 'running' ? '' : ' off');
  st.innerHTML = '<i></i>' + (s.status === 'running' ? '运行中' : '已停止');
  const a1 = document.getElementById('sdUrl');
  a1.textContent = url || '—'; a1.href = url || '#';
  const a2 = document.getElementById('sdUrl2');
  a2.textContent = url || '—'; a2.href = url || '#';
  document.getElementById('sdVis').textContent =
    s.visibility === 'private' ? '私有' : '所有人可见';
  const mapUrl = s.public_url || '';
  const mu = document.getElementById('sdMapUrl');
  mu.textContent = mapUrl || (s.mapping ? '正在建立映射…' : '未开启');
  mu.href = mapUrl || '#';
  mu.style.color = mapUrl ? '' : '#7f8478';
  document.getElementById('sdMapBtn').textContent =
    (mapUrl || s.mapping) ? '关闭映射' : '开启映射';
  document.getElementById('sdViews').textContent = rs.hits;
  document.getElementById('sdBytes').textContent = fmtBytes(rs.bytes);
  document.getElementById('sdDailyLimit').textContent = (s.daily_limit_gb || 10) + ' GB';
  document.getElementById('sdDailyUsed').textContent = '已使用：' + fmtBytes(s.bytes || 0);
  document.getElementById('sdQpsLimit').textContent = (s.qps_limit || 100) + ' QPS';
  document.getElementById('sdQps').textContent = '今日峰值：' + (s.qps_peak || 0) + '次/秒';
  const pauseBtn = document.getElementById('sdPauseBtn');
  if(pauseBtn) pauseBtn.textContent = s.status === 'running' ? '暂停服务' : '恢复服务';
  document.getElementById('sdRange').textContent = rs.label + '  ▾';
  document.getElementById('sdThumb').innerHTML =
    url && s.status === 'running'
      ? '<iframe src="' + escHtml(url) + '" scrolling="no"></iframe>' : '';
  document.getElementById('sdChartViews').innerHTML = sdChart(rs.hourly, 'views');
  document.getElementById('sdChartBytes').innerHTML = sdChart(rs.hourly_bytes, 'bytes');
  const hist = (s.deploys || []).slice().reverse();
  document.getElementById('sdHist').innerHTML = hist.length ? hist.map(d =>
    '<div class="hist-item"><b>' + escHtml(s.name) +
    (d.note === '首次部署' ? '首次部署' : '重新部署') + '</b>' +
    '<span>部署成功 · ' + fmtSiteDate(d.time) + '</span></div>').join('')
    : '<div class="sites-empty">还没有部署记录</div>';
  sdTab(SD_TAB || 'obs');
}

/* ---------------- 运行中心：MCP 管理 / 后台进程 ---------------- */
let RT_MCP_CONFIG = '';
let RT_MCP_PROCS = {};   // server 名（小写） -> 后台进程条目

/* 把 /v1/system/processes 里的 MCP 子进程按 server 名归档，供 MCP 卡片展示 */
function collectMcpProcs(d){
  RT_MCP_PROCS = {};
  if(!d) return;
  (d.groups || []).forEach(g => (g.items || []).forEach(it => {
    if(it.kind !== 'mcp-proc') return;
    const key = String(it.name || '').replace(/^MCP\s*·\s*/, '').trim().toLowerCase();
    if(key) RT_MCP_PROCS[key] = it;
  }));
}

function rtMcpProcLine(s){
  const proc = RT_MCP_PROCS[String(s.name || '').toLowerCase()];
  if(proc){
    return '<div class="rt-proc">后台进程 <b>PID ' + proc.pid + '</b>' +
      (proc.mem ? ' · ' + proc.mem + ' MB' : '') +
      (proc.port ? ' · 端口 ' + proc.port : '') +
      '<span class="lnk" onclick="focusProc(' + proc.pid + ')">在后台进程中查看 →</span></div>';
  }
  // HTTP / SSE 是远程服务，没有本地子进程
  const remote = s.transport === 'http' || s.transport === 'sse' ||
                 s.transport === 'streamable-http';
  return '<div class="rt-proc off">' +
    (remote ? '远程 ' + escHtml(String(s.transport).toUpperCase()) + ' 服务，无本地子进程'
            : '未发现本地进程') +
    (s.connected ? '' : ' · 未连接') + '</div>';
}

/* 从 MCP 卡片跳到后台进程页，并高亮对应行 */
function focusProc(pid){
  go('procs');
  let tries = 0;
  const tick = () => {
    const row = document.querySelector('#rtProcList .rt-row[data-pid="' + pid + '"]');
    if(row){
      row.scrollIntoView({block:'center'});
      row.classList.add('hl');
      setTimeout(() => row.classList.remove('hl'), 2600);
      return;
    }
    if(tries++ < 25) setTimeout(tick, 200);
  };
  setTimeout(tick, 150);
}

/* 从后台进程页跳回 MCP 管理，并高亮对应 server 卡片 */
function focusMcp(name){
  go('runtime');
  rtTab('mcp');
  let tries = 0;
  const tick = () => {
    const card = document.querySelector('#rtMcpList .rt-card[data-server="' + name + '"]');
    if(card){
      card.scrollIntoView({block:'center'});
      card.classList.add('hl');
      setTimeout(() => card.classList.remove('hl'), 2600);
      return;
    }
    if(tries++ < 25) setTimeout(tick, 200);
  };
  setTimeout(tick, 150);
}

function rtTab(name){
  document.querySelectorAll('.rt-tabs button[data-rt]').forEach(b =>
    b.classList.toggle('on', b.dataset.rt === name));
  document.getElementById('rtMcp').style.display = name === 'mcp' ? '' : 'none';
  document.getElementById('rtProc').style.display = name === 'proc' ? '' : 'none';
}

async function loadRuntime(){
  await Promise.all([loadMcp(), loadProcs()]);
}

async function loadMcp(){
  const box = document.getElementById('rtMcpList');
  if(!box) return;
  box.innerHTML = '<div class="rt-meta">加载中…</div>';
  try{
    const [d, p] = await Promise.all([
      fetch('/v1/mcp/servers', {cache:'no-store'}).then(r => r.json()),
      fetch('/v1/system/processes', {cache:'no-store'}).then(r => r.json()).catch(() => null)
    ]);
    if(!d.ok) throw new Error(d.error || '加载失败');
    collectMcpProcs(p);
    RT_MCP_CONFIG = d.config || '';
    const servers = d.servers || [];
    const connected = servers.filter(s => s.connected).length;
    const tools = servers.reduce((n, s) => n + (s.tools || []).length, 0);
    document.getElementById('rtMcpSum').innerHTML =
      '<span class="rt-pill">服务<b>' + connected + '/' + servers.length + '</b></span>' +
      '<span class="rt-pill">工具<b>' + tools + '</b></span>' +
      '<span class="rt-pill">进程<b>' + Object.keys(RT_MCP_PROCS).length + '</b></span>' +
      '<span class="rt-pill">配置<b>mcp.json</b></span>';
    box.innerHTML = servers.map(s => {
      const list = (s.tools || []).map(t => t.name);
      let chips = list.slice(0, 8).map(n => '<span class="rt-chip">' + escHtml(n) + '</span>').join('');
      if(list.length > 8) chips += '<span class="rt-chip">+' + (list.length - 8) + '</span>';
      return '<div class="rt-card" data-server="' + escHtml(s.name) + '">' +
        '<div class="rt-card-h"><span class="rt-dot' + (s.connected ? ' on' : '') + '"></span>' +
          '<b>' + escHtml(s.name) + '</b><span class="rt-tag">' + escHtml(s.transport || '') + '</span>' +
          '<span class="rt-n">' + (s.tools || []).length + ' 个工具</span></div>' +
        '<div class="rt-meta">命名空间 ' + escHtml(s.namespace || '（无）') +
          ' · 超时 ' + (s.timeout || 120) + 's' +
          (s.error ? ' · <span style="color:#f0736a">' + escHtml(s.error) + '</span>' : '') + '</div>' +
        rtMcpProcLine(s) +
        (list.length ? '<div class="rt-chips">' + chips + '</div>' : '') + '</div>';
    }).join('');
  }catch(e){
    box.innerHTML = '<div class="rt-meta" style="color:#f0736a">加载失败：' + escHtml(e.message) + '</div>';
  }
}

async function reloadMcp(){
  toast('正在重载 MCP…');
  try{
    const d = await (await fetch('/v1/mcp/reload', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
    })).json();
    if(!d.ok){ toast(d.error || '重载失败'); return; }
    toast('已重载，MCP 工具 ' + ((d.tools || []).length) + ' 个');
    loadMcp();
  }catch(e){ toast('重载失败：' + e.message); }
}

function openMcpJson(){
  if(!RT_MCP_CONFIG){ toast('请先刷新 MCP 列表'); return; }
  copyText(RT_MCP_CONFIG);
  toast('mcp.json 路径已复制');
}

async function loadProcs(){
  const box = document.getElementById('rtProcList');
  if(!box) return;
  box.innerHTML = '<div class="rt-meta">加载中…</div>';
  try{
    const d = await (await fetch('/v1/system/processes', {cache:'no-store'})).json();
    if(!d.ok) throw new Error(d.error || '加载失败');
    document.getElementById('rtProcSum').innerHTML =
      '<span class="rt-pill">运行中<b>' + d.totals.running + '</b></span>' +
      '<span class="rt-pill">条目<b>' + d.totals.total + '</b></span>';
    box.innerHTML = (d.groups || []).map(g => {
      const rows = g.items.map(it => {
        // MCP 子进程：名字可点回「MCP 管理」对应的 server 卡片
        let nameCell = escHtml(it.name);
        if(it.kind === 'mcp-proc'){
          const srv = String(it.name || '').replace(/^MCP\s*·\s*/, '').trim();
          nameCell = '<span class="plink" data-srv="' + escHtml(srv) +
            '" onclick="focusMcp(this.dataset.srv)">' + escHtml(it.name) + '</span>';
        }
        return '<div class="rt-row" data-pid="' + (it.pid || '') + '"><span>' + nameCell + '</span>' +
        '<span class="dim">' + (it.pid || '—') + '</span>' +
        '<span class="dim">' + (it.port || '—') + '</span>' +
        '<span class="dim" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' +
          escHtml(it.detail || '') + '">' + escHtml(it.detail || '') + '</span>' +
        '<span class="dim">' + (it.mem ? it.mem + ' MB' : '—') + '</span>' +
        '<span class="st' + (it.status === 'running' ? '' : ' off') + '"><i></i>' +
          (it.status === 'running' ? '运行中' : '已停止') + '</span></div>';
      }).join('');
      return '<div class="rt-grp-h">' + escHtml(g.name) + '</div><div class="rt-scroll">' +
        '<div class="rt-row head"><span>名称</span><span>PID</span><span>端口</span>' +
        '<span>说明</span><span>内存</span><span>状态</span></div>' +
        '<div class="rt-rows">' + rows + '</div></div>';
    }).join('');
  }catch(e){
    box.innerHTML = '<div class="rt-meta" style="color:#f0736a">加载失败：' + escHtml(e.message) + '</div>';
  }
}

function closeSiteDetail(){
  document.getElementById('pane-sites').classList.remove('detail-open');
  document.getElementById('sitesDetail').style.display = 'none';
  SITE_DETAIL = null;
  SD_TAB = 'obs';
}

function sdTab(name){
  SD_TAB = name;
  document.getElementById('sdRange').style.display = name === 'obs' ? '' : 'none';
  document.querySelectorAll('.sd-tabs button').forEach(b =>
    b.classList.toggle('on', b.dataset.tab === name));
  ['obs', 'manage', 'history', 'logs'].forEach(t =>
    document.getElementById('sd-' + t).style.display = (t === name ? '' : 'none'));
  if(name === 'logs') loadLogs();
}

function refreshDetail(){ if(SITE_DETAIL) showSiteDetail(SITE_DETAIL.id); }

function visibilityMenu(ev){
  if(!SITE_DETAIL) return;
  const id = SITE_DETAIL.id;
  popMenu(ev.currentTarget, [
    { t:'所有人可见', fn:()=>siteAction(id, 'visibility', {visibility:'public'}) },
    { t:'私有（仅自己）', fn:()=>siteAction(id, 'visibility', {visibility:'private'}) }
  ], 'right');
}

async function pauseSite(){
  if(!SITE_DETAIL) return;
  await siteAction(SITE_DETAIL.id, 'stop');
}

async function deleteSiteFromDetail(){
  if(!SITE_DETAIL) return;
  if(!confirm('确定删除部署「' + SITE_DETAIL.name + '」？只会停止对外访问，不会删除本地文件。')) return;
  await siteAction(SITE_DETAIL.id, 'delete');
  closeSiteDetail();
  loadSites();
}

function openRedeploy(){
  if(!SITE_DETAIL) return;
  const id = SITE_DETAIL.id;
  openDeploy();
  DEPLOY_TARGET = id;
  document.getElementById('dpName').value = SITE_DETAIL.name;
  document.getElementById('dpPath').value = SITE_DETAIL.path || '';
}

/* ---------------- 站点设置：自有域名 / 资源上限 / 启停 / 日志 ---------------- */
let CFG_MODE = '';
function closeCfg(){ document.getElementById('cfgMask').classList.remove('on'); }

function openDomainCfg(){
  if(!SITE_DETAIL) return;
  CFG_MODE = 'domain';
  document.getElementById('cfgTitle').textContent = '更改访问地址';
  document.getElementById('cfgDomainWrap').style.display = '';
  document.getElementById('cfgDailyWrap').style.display = 'none';
  document.getElementById('cfgQpsWrap').style.display = 'none';
  document.getElementById('cfgDomain').value = SITE_DETAIL.custom_domain || '';
  document.getElementById('cfgTip').textContent =
    '当前 IP 地址：' + (SITE_DETAIL.url_lan || '—') +
    '。购买域名后填在这里，并把域名 A 记录解析到本机公网 IP；解析生效前仍可用 IP 访问。';
  document.getElementById('cfgMask').classList.add('on');
}

function openLimitCfg(){
  if(!SITE_DETAIL) return;
  CFG_MODE = 'limits';
  document.getElementById('cfgTitle').textContent = '更改资源上限';
  document.getElementById('cfgDomainWrap').style.display = 'none';
  document.getElementById('cfgDailyWrap').style.display = '';
  document.getElementById('cfgQpsWrap').style.display = '';
  document.getElementById('cfgDaily').value = SITE_DETAIL.daily_limit_gb || 10;
  document.getElementById('cfgQps').value = SITE_DETAIL.qps_limit || 100;
  document.getElementById('cfgTip').textContent =
    '超出单日流量上限或 QPS 上限时，访问会立刻被拒绝（503 / 429）。';
  document.getElementById('cfgMask').classList.add('on');
}

async function saveCfg(){
  if(!SITE_DETAIL) return;
  let r;
  if(CFG_MODE === 'domain'){
    r = await siteAction(SITE_DETAIL.id, 'set_domain',
                         {domain: document.getElementById('cfgDomain').value.trim()});
  } else {
    r = await siteAction(SITE_DETAIL.id, 'set_limits',
                         {daily_gb: document.getElementById('cfgDaily').value,
                          qps: document.getElementById('cfgQps').value});
  }
  if(r) closeCfg();
}

function togglePause(){
  if(!SITE_DETAIL) return;
  siteAction(SITE_DETAIL.id, SITE_DETAIL.status === 'running' ? 'stop' : 'start');
}

async function loadLogs(){
  if(!SITE_DETAIL) return;
  const box = document.getElementById('sdLogs');
  box.innerHTML = '<div class="sd-logs-empty">加载中…</div>';
  try{
    const d = await (await fetch('/v1/sites/logs?id=' +
      encodeURIComponent(SITE_DETAIL.id), {cache:'no-store'})).json();
    const logs = d.logs || [];
    if(!logs.length){
      box.innerHTML = '<div class="sd-logs-empty">暂无访问日志，打开站点后再刷新即可看到。</div>';
      return;
    }
    const p = n => String(n).padStart(2, '0');
    box.innerHTML =
      '<div class="log-row head"><span>时间</span><span>方法</span><span>路径</span><span>状态</span><span>大小</span></div>' +
      logs.map(l => {
        const t = new Date(l.t * 1000);
        return '<div class="log-row"><span>' + p(t.getHours()) + ':' + p(t.getMinutes()) +
          ':' + p(t.getSeconds()) + '</span><span>' + l.m + '</span><span>' +
          escHtml(l.p) + '</span><span class="s' + String(l.s).charAt(0) + '">' +
          l.s + '</span><span>' + fmtBytes(l.b) + '</span></div>';
      }).join('');
  }catch(e){
    box.innerHTML = '<div class="sd-logs-empty">加载失败：' + escHtml(e.message) + '</div>';
  }
}

/* ---------------- 右侧工作台：终端 / 代码变更 ---------------- */
function wbTab(name){
  document.querySelectorAll('.wb-tabs button').forEach(b=>b.classList.toggle('on', b.dataset.wb===name));
  document.getElementById('wbTerm').style.display = name==='term' ? '' : 'none';
  document.getElementById('wbDiff').style.display = name==='diff' ? '' : 'none';
  if(name==='diff') loadDiff();
}

let termBusy = false;
function termPrint(text, cls){
  const out = document.getElementById('termOut');
  const line = document.createElement('div');
  if(cls) line.className = cls;
  line.textContent = text;
  out.appendChild(line);
  out.scrollTop = out.scrollHeight;
}

/* shell_exec 内部走 cmd，直接发 PowerShell 命令会报「不是内部或外部命令」且中文乱码；
   用 -EncodedCommand（UTF-16LE base64）把脚本原样交给 PowerShell，避开引号转义。 */
function psEncoded(cmd){
  const script = "$ProgressPreference='SilentlyContinue';" +
    '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;' + cmd;
  const bytes = [];
  for(let i=0;i<script.length;i++){
    const c = script.charCodeAt(i);
    bytes.push(c & 0xff, (c >> 8) & 0xff);
  }
  let bin = '';
  for(let i=0;i<bytes.length;i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

async function termRun(cmd){
  if(termBusy || !cmd) return;
  termBusy = true;
  termPrint('PS> ' + cmd, 'c');
  try{
    const r = await (await fetch('/agent/tool_run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: 'shell_exec',
        args: {command: 'powershell -NoProfile -EncodedCommand ' + psEncoded(cmd)}})
    })).json();
    termPrint(r.ok === false ? (r.error || '执行失败') : (r.result || '（无输出）'),
              r.ok === false ? 'e' : '');
  }catch(e){
    termPrint('执行失败：' + e.message, 'e');
  }finally{
    termBusy = false;
  }
}

function diffWhen(e){
  if(!e.ts) return e.time || '';
  return new Date(e.ts * 1000).toLocaleDateString('zh-CN') + ' ' + (e.time || '');
}

async function loadDiff(){
  const box = document.getElementById('diffList');
  if(!box) return;
  let entries = [];
  try{
    const d = await (await fetch('/undo/list', {cache: 'no-store'})).json();
    entries = (d.entries || []).slice(0, 40);
  }catch(e){}
  box.innerHTML = entries.length ? entries.map(e=>`
    <div class="ditem">
      <div class="dn">
        <div class="dt" title="${e.path}">${String(e.path || '').split(/[\\/]/).pop()}</div>
        <div class="dm">${diffWhen(e)} · ${e.tool || ''}</div>
      </div>
      <button class="btn" onclick="undoStep('${String(e.path || '').replace(/\\/g, '\\\\')}')">撤销</button>
    </div>`).join('')
    : '<div class="wb-empty">还没有可回滚的改动。wasily 写入文件后，这里会按时间列出快照。</div>';
}

async function undoStep(path){
  try{
    const r = await (await fetch('/undo/step', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path})
    })).json();
    toast(r.result || '已撤销');
  }catch(e){ toast('撤销失败：' + e.message); }
  loadDiff();
}

/* ---------------- 左下角用户卡片 ---------------- */
async function loadMe(){
  try{
    const d = await (await fetch('/account/info')).json();
    if(d && d.name) document.getElementById('meName').textContent = d.name;
    if(d && d.plan) document.getElementById('mePlan').textContent = d.plan;
  }catch(e){ document.getElementById('meName').textContent = '本地用户'; }
}

/* 原生窗口优先；浏览器和普通内嵌页仍保留 pywebview/关标签页回退 */
/* ===== 顶部菜单：文件 / 编辑 / 查看 / 帮助 ===== */
let uiZoom = 1;
let menuPop = null;

function chatFrameDoc() {
  try { return document.getElementById('chatFrame').contentDocument; } catch (e) { return null; }
}
function activeEditable() {
  const d = chatFrameDoc();
  const ae = document.activeElement;
  if (d && d.activeElement && /INPUT|TEXTAREA/.test(d.activeElement.tagName)) return d.activeElement;
  if (ae && /INPUT|TEXTAREA/.test(ae.tagName)) return ae;
  return d ? d.getElementById('input') : null;
}
function editCmd(cmd) {
  const el = activeEditable();
  if (el) { try { el.focus(); } catch (e) {} }
  try { document.execCommand(cmd); } catch (e) {}
}
function setUiZoom(v) {
  uiZoom = Math.min(1.6, Math.max(0.7, Math.round(v * 10) / 10));
  document.body.style.zoom = uiZoom;
}
function toggleFull() {
  if (document.fullscreenElement) document.exitFullscreen();
  else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen();
}
function showMenuModal(title, bodyHtml) {
  const mask = document.createElement('div');
  mask.className = 'menu-modal';
  mask.innerHTML = '<div class="mm-card"><div class="mm-h">' + title +
    '<span class="mm-x">×</span></div><div class="mm-b">' + bodyHtml + '</div></div>';
  mask.querySelector('.mm-x').onclick = () => mask.remove();
  mask.onclick = e => { if (e.target === mask) mask.remove(); };
  document.body.appendChild(mask);
}

/* 菜单结构与 Trae 桌面端保持一致；多组快捷键用数组表示 */
const MENU_DEFS = {
  file: [
    { t: '新建 IDE 窗口', k: 'Ctrl+Shift+N', fn: () => window.open(location.href, '_blank', 'width=1280,height=860') },
    { t: '新建任务', k: 'Ctrl+N', fn: () => newTask() },
    { t: '复制最近回复', fn: () => ipcSend('chat', 'copy_last', {}) },
    { sep: 1 },
    { t: '刷新窗口', k: 'F5', fn: () => location.reload() },
    { t: '关闭窗口', fn: () => winCtl('close') }
  ],
  edit: [
    { t: '撤销', k: 'Ctrl+Z', fn: () => editCmd('undo') },
    { t: '恢复', k: 'Ctrl+Y', fn: () => editCmd('redo') },
    { sep: 1 },
    { t: '剪切', k: 'Ctrl+X', fn: () => editCmd('cut') },
    { t: '复制', k: 'Ctrl+C', fn: () => editCmd('copy') },
    { t: '粘贴', k: 'Ctrl+V', fn: () => editCmd('paste') }
  ],
  view: [
    { t: '全屏', k: 'F11', fn: () => toggleFull() },
    { sep: 1 },
    { t: '放大', k: 'Ctrl+=', fn: () => setUiZoom(uiZoom + 0.1) },
    { t: '缩小', k: 'Ctrl+-', fn: () => setUiZoom(uiZoom - 0.1) },
    { t: '重置缩放', k: 'Ctrl+NumPad0', fn: () => setUiZoom(1) }
  ],
  help: [
    { t: '显示更新日志', fn: () => openLocalPage('/changelog', '更新日志') },
    { sep: 1 },
    { t: '切换开发人员工具', k: 'Ctrl+Shift+I', fn: () => openDevTools() },
    { t: '报告问题', k: ['Ctrl+K', 'Ctrl+R'], fn: () => showMenuModal('报告问题',
        '<div>请把问题描述与复现步骤，连同 <b>wasily/AI/logs</b> 下的相关日志一起反馈给开发者。</div>' +
        '<div style="margin-top:6px">后端地址：http://127.0.0.1:8000</div>') },
    { t: '进程浏览器', fn: () => toast('进程浏览器需在桌面端 IDE 内使用') },
    { t: '帮助文档', fn: () => openLocalPage('/docs', '帮助文档') },
    { t: '联系我们', fn: () => showMenuModal('联系我们',
        '<div>wasily 本地助手</div>' +
        '<div>问题反馈请附上后端地址与日志目录：wasily/AI/logs</div>' +
        '<div>后端：http://127.0.0.1:8000</div>') },
    { t: '在文件夹中打开日志', fn: () => toast('日志目录：wasily/AI/logs') },
    { sep: 1 },
    { t: '检查更新...', fn: () => toast('当前已是最新版本') },
    { sep: 1 },
    {
      t: '关于', fn: () => showMenuModal('关于 wasily',
        '<div><b>wasily</b> 本地 AI 助手</div>' +
        '<div>本地模型：llama.cpp + qwen2.5</div>' +
        '<div>后端：http://127.0.0.1:8000</div>' +
        '<div>IDE 桥：http://127.0.0.1:18753</div>')
    }
  ]
};

function openDevTools(){
  try {
    if (window.workbenchAPI && typeof window.workbenchAPI.openDevTools === 'function') {
      window.workbenchAPI.openDevTools(); return;
    }
    if (window.pywebview && window.pywebview.api && typeof window.pywebview.api.open_devtools === 'function') {
      window.pywebview.api.open_devtools(); return;
    }
  } catch (e) {}
  toast('当前窗口不支持开发者工具');
}

/* 把 "Ctrl+Shift+I" 渲染成 Trae 的键帽组合；数组表示多组快捷键 */
function renderKeys(k){
  const groups = Array.isArray(k) ? k : [k];
  return '<span class="mp-keys">' + groups.map(g =>
    '<span class="mp-keygrp">' + String(g).split('+').map(x =>
      '<kbd class="mp-key">' + x + '</kbd>').join('<i class="mp-plus">+</i>') +
    '</span>').join('') + '</span>';
}

function closeMenu() { if (menuPop) { menuPop.remove(); menuPop = null; } }
function openMenu(key, anchor) {
  closeMenu();
  const defs = MENU_DEFS[key];
  if (!defs) return;
  const pop = document.createElement('div');
  pop.className = 'menu-pop';
  pop.dataset.key = key;
  defs.forEach(d => {
    if (d.sep) {
      const s = document.createElement('div');
      s.className = 'mp-sep';
      pop.appendChild(s);
      return;
    }
    const it = document.createElement('div');
    it.className = 'mp-item';
    it.innerHTML = '<span>' + d.t + '</span>' + (d.k ? renderKeys(d.k) : '');
    it.onclick = () => { closeMenu(); try { d.fn && d.fn(); } catch (e) {} };
    pop.appendChild(it);
  });
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = Math.round(r.left) + 'px';
  pop.style.top = Math.round(r.bottom + 4) + 'px';
  menuPop = pop;
}
document.querySelectorAll('#winbar .mi[data-menu]').forEach(el => {
  el.onclick = e => {
    e.stopPropagation();
    const key = el.dataset.menu;
    if (menuPop && menuPop.dataset.key === key) { closeMenu(); return; }
    openMenu(key, el);
  };
});
document.addEventListener('click', closeMenu);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeMenu();
  if (e.ctrlKey && (e.key === 'n' || e.key === 'N')) { e.preventDefault(); newTask(); }
  if (e.ctrlKey && (e.key === '0')) { e.preventDefault(); setUiZoom(1); }
  if (e.key === 'F11') { e.preventDefault(); toggleFull(); }
});

function winCtl(action){
  const wb = window.workbenchAPI;
  if(wb && wb.isElectron){
    if(action === 'minimize') wb.minimize();
    else if(action === 'toggle_max') wb.maximizeOrRestore();
    else if(action === 'close') wb.close();
    return;
  }
  const api = window.pywebview && window.pywebview.api;
  if(api && typeof api[action] === 'function'){ api[action](); return; }
  if(action === 'close') window.close();
}

if(window.workbenchAPI && window.workbenchAPI.isElectron){
  const bar = document.getElementById('winbar');
  bar.addEventListener('dblclick', e=>{
    if(e.target.closest('.wico,.menus,.wctl')) return;
    winCtl('toggle_max');
  });
  window.workbenchAPI.onState(state=>{
    const btn = document.getElementById('winMaxBtn');
    if(btn) btn.title = state === 'max' ? '还原' : '最大化';
  });
}

/* ===== 侧边栏：固定在左侧，只允许拖动调宽 ===== */
(function(){
  const side = document.getElementById('side');
  const rz = document.getElementById('sideResizer');
  const KEY_W = 'wasily.agent.sideWidth';
  const MIN_W = 140, MAX_W = 520;

  const clampW = w => Math.max(MIN_W, Math.min(MAX_W, Math.round(w)));

  function applyWidth(w){
    side.style.width = clampW(w) + 'px';
  }

  const savedW = parseInt(localStorage.getItem(KEY_W) || '', 10);
  if(savedW) applyWidth(savedW);

  // 旧版本存过「停靠右侧」，这里清掉，导航栏固定在左侧
  try{ localStorage.removeItem('wasily.agent.sideDock'); }catch(e){}

  rz.addEventListener('mousedown', function(e){
    if(e.button !== 0) return;
    e.preventDefault();
    const startX = e.clientX;
    const startW = side.getBoundingClientRect().width;
    rz.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    function move(ev){
      const dx = ev.clientX - startX;
      applyWidth(startW + dx);
    }
    function up(){
      rz.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      localStorage.setItem(KEY_W, String(Math.round(side.getBoundingClientRect().width)));
    }
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });

  // 双击手柄恢复默认宽度（清掉自定义值，交回主题样式）
  rz.addEventListener('dblclick', function(){
    side.style.width = '';
    localStorage.removeItem(KEY_W);
  });
})();

/* 工作台宽度：拖拽调宽并记住，双击手柄恢复默认 */
(function(){
  const pane = document.getElementById('wbPane');
  const rz = document.getElementById('wbResizer');
  const KEY = 'wasily.agent.wbWidth';
  const MIN = 240, MAX = 720;
  const clamp = w => Math.max(MIN, Math.min(MAX, Math.round(w)));

  const saved = parseInt(localStorage.getItem(KEY) || '', 10);
  if(saved){ pane.style.width = clamp(saved) + 'px'; }

  rz.addEventListener('mousedown', e=>{
    if(e.button !== 0) return;
    e.preventDefault();
    const startX = e.clientX;
    const startW = pane.getBoundingClientRect().width;
    rz.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    function move(ev){
      const w = clamp(startW - (ev.clientX - startX));
      pane.style.width = w + 'px';
    }
    function up(){
      rz.classList.remove('dragging');
      document.body.style.cursor = '';
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      localStorage.setItem(KEY, String(Math.round(pane.getBoundingClientRect().width)));
    }
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });

  rz.addEventListener('dblclick', ()=>{
    pane.style.width = '';
    localStorage.removeItem(KEY);
  });
})();

/* 终端输入：回车执行；空输入时上箭头取回上一条命令 */
(function(){
  const inp = document.getElementById('termCmd');
  let last = '';
  inp.addEventListener('keydown', e=>{
    if(e.key === 'Enter'){
      const cmd = inp.value.trim();
      if(!cmd) return;
      last = cmd;
      inp.value = '';
      termRun(cmd);
    }else if(e.key === 'ArrowUp' && !inp.value){
      inp.value = last;
    }
  });
})();

/* ===== 空状态联动：对话面板还没有消息时收起右侧工作台，整块卡片只留对话 =====
   iframe 与本页同源，直接读它有没有消息节点，比让对话端额外上报更简单，
   也避免「只是点了输入框、还没发消息」就 prematurely 弹出工作台。 */
(function(){
  const frame = document.getElementById('chatFrame');
  let mo = null;
  function hasMsg(){
    try{
      const d = frame.contentDocument;
      return !!(d && (d.querySelector('#chat .msg') || d.querySelector('#sideDock .sp-msgs .msg')));
    }catch(e){ return true; }   // 读不到面板时按会话态处理，宁可露出工作台也别误收
  }
  function apply(){ document.body.classList.toggle('task-empty', !hasMsg()); }
  function watch(){
    const d = frame.contentDocument;
    if(!d) return;
    if(mo) mo.disconnect();
    mo = new MutationObserver(apply);
    mo.observe(d.body, {childList: true, subtree: true});
    apply();
  }
  frame.addEventListener('load', watch);
  watch();
  // 会话恢复是异步的：首屏暂时没消息不代表空，短时兜底轮询，稳定后交给 MutationObserver
  let n = 0;
  const t = setInterval(()=>{ apply(); if(++n >= 12) clearInterval(t); }, 400);
})();

/* 初始化 */
document.getElementById('chatFrame').src = agentChatUrl();
loadPlugins();
loadMe();
go(location.hash ? location.hash.slice(1) : 'chat');
