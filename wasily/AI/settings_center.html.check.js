
const SERVER = '';
let CFG = {};

/* ---------- 工具 ---------- */
function $(s, r){ return (r||document).querySelector(s); }
function el(tag, cls, text){ const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined)n.textContent=text; return n; }
function toast(msg){ const t=$('#toast'); t.textContent=msg; t.classList.add('show'); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove('show'),2200); }
function api(path){ return fetch(SERVER+path).then(r=>r.ok?r.json():Promise.reject(r.status)); }
function post(path, body){ return fetch(SERVER+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).then(r=>r.ok?r.json():Promise.reject(r.status)); }
function getCfg(path, dft){ let cur=CFG; for(const p of path.split('.')){ if(cur&&typeof cur==='object'&&p in cur)cur=cur[p]; else return dft; } return cur; }
function setCfg(path, value){
  const parts=path.split('.'); let cur=CFG;
  for(const p of parts.slice(0,-1)){ cur[p]=cur[p]||{}; cur=cur[p]; }
  cur[parts[parts.length-1]]=value;
  post('/config/set',{path:path,value:value}).then(()=>toast('已保存')).catch(()=>toast('保存失败：服务未连接'));
}
function bridge(action){
  if (window.parent !== window) window.parent.postMessage({from:'wasily-settings', action:action}, '*');
  else toast('该功能需在 IDE 内打开设置中心');
}
// 主题：暗色/浅色/跟随系统，立即作用到设置页，AI 面板重载后同步
function applyTheme(mode){
  var light = mode==='light' || (mode!=='dark' && window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
  document.documentElement.setAttribute('data-theme', light?'light':'dark');
}
function fmt(n){ return String(n||0).replace(/\B(?=(\d{3})+(?!\d))/g,','); }

/* ---------- IDE 颜色主题 ---------- */
// 列表来自后端 /ide/themes（内置主题 + 已装主题扩展），切换即写 workbench.colorTheme
let THEMES=[], CUR_THEME='', themeQ='';
function loadThemes(){
  const box=$('#themeGrid'); if(!box) return;
  api('/ide/themes').then(d=>{
    THEMES=d.themes||[]; CUR_THEME=d.current||'';
    renderThemes(); syncThemeCtls();
  }).catch(()=>{ box.innerHTML='<div class="empty">IDE 主题读取失败：AI 服务未连接</div>'; });
}
function renderThemes(){
  const box=$('#themeGrid'); if(!box) return;
  if(!THEMES.length){ box.innerHTML='<div class="empty">未发现颜色主题</div>'; return; }
  const q=(themeQ||'').trim().toLowerCase();
  const list=THEMES.filter(t=>!q || (themeDisplayName(t.label)+' '+t.label).toLowerCase().indexOf(q)>=0);
  const light=list.filter(t=>isLightTheme(t.label));
  const dark=list.filter(t=>!isLightTheme(t.label));
  // 内置的四个 wasily 主题排在最前，其余按原顺序
  const rankOf=function(l){ const i=Object.keys(THEME_PRESETS).map(k=>THEME_PRESETS[k]).indexOf(l); return i<0?99:i; };
  light.sort((a,b)=>rankOf(a.label)-rankOf(b.label));
  dark.sort((a,b)=>rankOf(a.label)-rankOf(b.label));
  let html='<div class="pal-item" data-browse="1"><span class="nm">浏览其他颜色主题…</span></div>';
  const group=function(title,arr){
    if(!arr.length) return;
    html+='<div class="pal-sep"><span>'+title+'</span></div>';
    arr.forEach(function(t){
      html+='<div class="pal-item'+(t.label===CUR_THEME?' on':'')+'" data-i="'+THEMES.indexOf(t)+'">'+
            '<span class="nm">'+themeDisplayName(t.label)+'</span>'+
            '<span class="id">'+t.label+'</span>'+
            '<span class="chk">✓</span></div>';
    });
  };
  group('浅色主题', light);
  group('深色主题', dark);
  if(!list.length) html+='<div class="empty" style="padding:14px">没有匹配的主题</div>';
  box.innerHTML=html;
  box.querySelectorAll('.pal-item[data-i]').forEach(function(node){
    node.onclick=function(){
      const t=THEMES[+node.getAttribute('data-i')];
      if(t) pickTheme(t.label, function(){ syncThemeCtls(); closeThemeDlg(); });
    };
  });
  const bw=box.querySelector('[data-browse]');
  if(bw) bw.onclick=function(){ toast('在 IDE 扩展市场安装主题扩展，即可获得更多颜色主题'); };
}
function pickTheme(label, after){
  post('/ide/theme',{label:label}).then(()=>{
    CUR_THEME=label; renderThemes(); toast('已切换：'+label);
    if(after) after();
  }).catch(()=>toast('切换失败：AI 服务未连接'));
}

/* ---------- 主题下拉：暗色 / 深蓝 / 亮色 / 现代深色 / 跟随系统 / 更多主题 ---------- */
const THEME_PRESETS = { dark:'wasily 暗色', blue:'wasily 深蓝', light:'wasily 亮色', modern:'wasily 现代深色' };
const THEME_CTLS = [];
function isLightTheme(label){ return /light|亮色/i.test(label||''); }
function themeModeOf(label){
  for(const k in THEME_PRESETS){ if(THEME_PRESETS[k]===label) return k; }
  if(!label) return 'modern';
  return isLightTheme(label) ? 'light' : 'dark';
}
function syncThemeCtl(s){
  if(!s || !document.body.contains(s)) return;
  s.value = themeModeOf(CUR_THEME);
}
function syncThemeCtls(){ THEME_CTLS.forEach(syncThemeCtl); }
function openThemeDlg(anchor){
  const d=$('#themeDlg'); if(!d) return;
  const pal=d.querySelector('.pal'), q=d.querySelector('.pal-q');
  const w=520;
  const r = anchor ? anchor.getBoundingClientRect() : {bottom:120, top:80, right:window.innerWidth-32};
  pal.style.left = Math.max(12, Math.min(window.innerWidth - w - 12, r.right - w)) + 'px';
  themeQ=''; q.value='';
  renderThemes();
  pal.style.top = (r.bottom + 8) + 'px';
  const h = pal.offsetHeight;
  // 下方放不下就向上翻，避免超出视口
  if(r.bottom + 8 + h > window.innerHeight - 12) pal.style.top = Math.max(12, r.top - 8 - h) + 'px';
  d.classList.add('on');
  setTimeout(()=>q.focus(), 0);
}
function closeThemeDlg(){ const d=$('#themeDlg'); if(d) d.classList.remove('on'); }
function themeDisplayName(label){
  const M={
    'wasily 暗色':'暗色','wasily 深蓝':'深蓝','wasily 亮色':'亮色','wasily 现代深色':'现代深色',
    'Light (Visual Studio)':'浅色(Visual Studio)','Light+':'浅色+','Light Modern':'现代浅色',
    'Light High Contrast':'高对比浅色','Light 2026':'浅色 2026','Tokyo Night Light':'Tokyo Night 浅色',
    'Dark (Visual Studio)':'暗色(Visual Studio)','Dark+':'暗色+','Dark Modern':'现代深色(VS)',
    'Dark High Contrast':'高对比深色','Dark 2026':'暗色 2026','Nord':'深蓝(Nord)'
  };
  return M[label] || label;
}
function themeCtl(){
  const s=el('select');
  [['dark','暗色'],['blue','深蓝'],['light','亮色'],['modern','现代深色'],
   ['system','跟随系统'],['more','更多主题…']].forEach(o=>{
    const op=el('option',null,o[1]); op.value=o[0]; s.appendChild(op);
  });
  syncThemeCtl(s);
  s.onchange=function(){
    const v=s.value;
    if(v==='more'){ syncThemeCtl(s); openThemeDlg(s); return; }
    const label = v==='system'
      ? THEME_PRESETS[(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark']
      : THEME_PRESETS[v];
    setCfg('ui.theme', v==='system' ? 'system' : (v==='light'?'light':'dark'));
    applyTheme(getCfg('ui.theme','dark'));
    pickTheme(label, function(){ CUR_THEME=label; syncThemeCtls(); closeThemeDlg(); });
  };
  THEME_CTLS.push(s);
  return s;
}
function themeDlg(){
  if($('#themeDlg')) return;
  const d=el('div'); d.id='themeDlg';
  const pal=el('div','pal');
  const hd=el('div','pal-hd');
  hd.appendChild(el('span',null,'Select Color Theme'));
  hd.appendChild(el('span','i','i'));
  pal.appendChild(hd);
  const q=el('input','pal-q'); q.placeholder='搜索颜色主题';
  pal.appendChild(q);
  const list=el('div','pal-list'); list.id='themeGrid';
  list.innerHTML='<div class="empty" style="padding:14px">加载中…</div>';
  pal.appendChild(list);
  const ft=el('div','pal-ft');
  ft.innerHTML='<span><kbd>↑↓</kbd>导航</span><span><kbd>Esc</kbd>取消</span><span><kbd>↵</kbd>选择</span>';
  pal.appendChild(ft);
  d.appendChild(pal);
  d.addEventListener('click', function(e){ if(e.target===d) closeThemeDlg(); });
  q.addEventListener('input', function(){ themeQ=q.value; renderThemes(); });
  q.addEventListener('keydown', function(e){
    const items=[].slice.call(list.querySelectorAll('.pal-item[data-i]'));
    if(e.key==='Escape'){ closeThemeDlg(); return; }
    if(!items.length) return;
    let cur=items.findIndex(n=>n.classList.contains('kb'));
    if(e.key==='ArrowDown'){ e.preventDefault(); cur=(cur+1)%items.length; }
    else if(e.key==='ArrowUp'){ e.preventDefault(); cur=(cur-1+items.length)%items.length; }
    else if(e.key==='Enter'){ e.preventDefault(); items[cur<0?0:cur].click(); return; }
    else return;
    items.forEach(n=>n.classList.remove('kb'));
    items[cur].classList.add('kb');
    items[cur].scrollIntoView({block:'nearest'});
  });
  document.body.appendChild(d);
}

/* ---------- 控件 ---------- */
function row(t, d, ctl){
  const r=el('div','row');
  const lab=el('div','lab'); lab.appendChild(el('div','t',t));
  if(d) lab.appendChild(el('div','d',d));
  r.appendChild(lab);
  if(ctl) r.appendChild(ctl);
  return r;
}
function selCtl(path, options, after){
  const s=el('select');
  options.forEach(o=>{ const op=el('option',null,o[1]); op.value=o[0]; s.appendChild(op); });
  s.value=String(getCfg(path, options[0][0]));
  s.onchange=()=>{ setCfg(path, s.value); if(after) after(s.value); };
  return s;
}
function swCtl(path, after){
  const l=el('label','sw'); const i=document.createElement('input'); i.type='checkbox';
  i.checked=!!getCfg(path,false);
  const span=document.createElement('i'); l.appendChild(i); l.appendChild(span);
  i.onchange=()=>{ setCfg(path, i.checked); if(after) after(i.checked); };
  return l;
}
function btnCtl(text, onclick, cls){
  const b=el('button','btn'+(cls?' '+cls:''),text); b.onclick=onclick; return b;
}
function gearCtl(onclick){
  const g=el('button','gear'); g.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 1-.1 1.2l2 1.5-2 3.5-2.4-1a7 7 0 0 1-2 1.2L14 21h-4l-.4-2.6a7 7 0 0 1-2-1.2l-2.4 1-2-3.5 2-1.5A7 7 0 0 1 5 12a7 7 0 0 1 .1-1.2l-2-1.5 2-3.5 2.4 1a7 7 0 0 1 2-1.2L10 3h4l.4 2.6a7 7 0 0 1 2 1.2l2.4-1 2 3.5-2 1.5a7 7 0 0 1 .1 1.2z"/></svg>';
  g.onclick=onclick; return g;
}
function numCtl(path, min, max){
  const i=document.createElement('input'); i.type='number'; i.min=min; i.max=max;
  i.value=getCfg(path,0); i.style.minWidth='110px';
  i.onchange=()=>setCfg(path, parseInt(i.value,10));
  return i;
}
function textCtl(path, width){
  const i=document.createElement('input'); i.type='text'; i.value=getCfg(path,'');
  if(width) i.style.minWidth=width;
  i.onchange=()=>setCfg(path, i.value);
  return i;
}
function grp(text){ return el('div','grp',text); }

/* ---------- 图标 ---------- */
const IC = {
  user:'M12 12a3.4 3.4 0 1 0 0-6.8 3.4 3.4 0 0 0 0 6.8zM5 19.5c.7-3 3.4-4.6 7-4.6s6.3 1.6 7 4.6',
  chart:'M4 20V10M10 20V4M16 20v-7M22 20H2',
  gear:'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 1-.1 1.2l2 1.5-2 3.5-2.4-1a7 7 0 0 1-2 1.2L14 21h-4l-.4-2.6a7 7 0 0 1-2-1.2l-2.4 1-2-3.5 2-1.5A7 7 0 0 1 5 12a7 7 0 0 1 .1-1.2l-2-1.5 2-3.5 2.4 1a7 7 0 0 1 2-1.2L10 3h4l.4 2.6a7 7 0 0 1 2 1.2l2.4-1 2 3.5-2 1.5a7 7 0 0 1 .1 1.2z',
  phone:'M8 3h8a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM11 18h2',
  ball:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3.5 9h17M3.5 15h17M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18',
  code:'M8 6l-6 6 6 6M16 6l6 6-6 6',
  shield:'M12 3l8 3v6c0 4.5-3.2 7.7-8 9-4.8-1.3-8-4.5-8-9V6zM9 12l2 2 4-4',
  bot:'M12 4a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM12 4v2M5 8h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM8.5 13.5h.01M15.5 13.5h.01M9 17h6',
  puzzle:'M10 3h4v3a2 2 0 1 0 4 0h3v4h-3a2 2 0 1 0 0 4h3v4h-4v-3a2 2 0 1 0-4 0v3H9v-4H6a2 2 0 1 0 0-4h3V7H6a2 2 0 1 0 0 4',
  plug:'M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0zM12 16v5',
  cursor:'M4 4l7 17 2-7 7-2z',
  chip:'M9 9h6v6H9zM4 4h16v16H4zM9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3',
  chat:'M4 6h16v10H8l-4 4zM8 10h8M8 13h5',
  globe:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3a13 13 0 0 1 0 18M12 3a13 13 0 0 0 0 18',
  monitor:'M3 5h18v11H3zM9 20h6M12 16v4',
  folder:'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  zap:'M13 2L4 14h6l-1 8 9-12h-6z',
  book:'M4 5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2zM4 19V5',
  hook:'M12 3v8a4 4 0 1 0 4 4M12 3a2 2 0 1 1-2 2 2 2 0 0 1 2-2z',
  flask:'M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4A2 2 0 0 0 19 18l-5-9V3M8 15h8',
  info:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01'
};
function icon(p){ return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="'+p+'"/></svg>'; }

/* ---------- 中英 i18n ----------
   以中文原文为 key，切换到 English 时遍历文本节点做整句替换；
   切回中文时重新渲染（show/renderNav 会还原原文），无需维护反向字典。 */
const EN = {
  /* 导航 */
  '账户':'Account','设置':'Settings','能力':'Capabilities','其他':'Other',
  '账号':'Account','用量管理':'Usage','通用':'General','TRAE移动端':'TRAE Mobile',
  '桌面悬浮球':'Desktop Ball','开发环境':'Dev Environment','权限审批':'Permissions',
  '智能体':'Agents','插件市场':'Plugins','模型':'Models','对话流':'Dialog',
  '浏览器':'Browser','电脑控制':'Computer Control','索引与文档':'Index & Docs',
  '技能与命令':'Skills & Commands','规则与记忆':'Rules & Memory','关于TRAE':'About TRAE',
  /* 账号 / 用量 */
  '本地账户信息，数据仅存储在本机。':'Local account data, stored only on this machine.',
  '剩余积分':'Credits left','资料':'Profile','输入昵称':'Enter nickname','保存':'Save',
  '昵称不能为空':'Nickname is required','昵称已更新':'Nickname updated','保存失败':'Save failed',
  '昵称':'Nickname','修改后同步显示在标题栏与菜单':'Shown in the title bar and menu',
  '积分':'Credits','每日签到':'Daily check-in','每天可领取 100 积分':'Earn 100 credits every day',
  '签到':'Check in','签到成功，积分 +100':'Checked in, +100 credits','今日已签到':'Already checked in today',
  '签到失败':'Check-in failed','AI 服务未连接':'AI service offline','本地模式':'Local mode',
  '累计签到':'Total check-ins','天':'days','累计获得积分':'Credits earned','邀请好友':'Invited friends',
  '人':'people','（服务未连接）':'(service offline)','免费':'Free','本地用户':'Local user',
  '本地模型的 Token 消耗统计，按会话维度记录明细。':'Token usage of the local model, broken down by session.',
  '累计 Tokens':'Total tokens','输入 Tokens':'Input tokens','输出 Tokens':'Output tokens',
  '对话轮数':'Turns','会话':'Session','输入':'Input','输出':'Output','轮数':'Turns','暂无记录':'No records yet',
  /* 通用 */
  '界面外观、语言与默认行为设置。':'Appearance, language and default behavior.',
  '主题':'Theme','设置界面主题':'UI theme','暗色':'Dark','浅色':'Light','跟随系统':'Follow system',
  '语言':'Language','设置界面显示语言':'UI display language','简体中文':'简体中文',
  '默认启动窗口':'Startup window','IDE 启动时打开的窗口':'Window opened when the IDE starts',
  '系统默认（跟随上次打开）':'System default (restore last)','新建窗口':'New window',
  '恢复上次项目':'Restore last project','空工作台':'Empty workbench',
  '关闭最后一个窗口时':'When last window closes','关闭 IDE 最后一个窗口后的行为':'Behavior after closing the last IDE window',
  '退出应用':'Quit application','保持后台运行（托盘）':'Keep running (tray)',
  '编辑器':'Editor','去设置':'Open settings','Editor 设置':'Editor settings',
  '字体、缩进、自动保存等编辑器项，跳转原生设置编辑器':'Fonts, indentation, auto save — opens the native settings editor',
  '使用 VS Code 快捷键风格':'Use VS Code keybinding style','快捷键设置':'Keybindings',
  '查看和自定义全部快捷键':'View and customize all keybindings','发送按键':'Send key',
  '对话输入框中发送消息的快捷键':'Shortcut to send a message in the chat box',
  'Enter 发送':'Send with Enter','Ctrl+Enter 发送':'Send with Ctrl+Enter',
  '语音转录快捷键':'Voice transcription shortcut','按住快捷键进行语音输入转文字':'Hold the shortcut to dictate',
  '配置':'Configuration','导入配置':'Import config','从 JSON 文件批量导入配置项':'Import settings from a JSON file',
  '导入':'Import','导出配置':'Export config','导出全部配置为 JSON 文件':'Export all settings as JSON','导出':'Export',
  '打开方式':'Open with','本地链接默认打开方式':'Default handler for local links',
  '点击本地链接时的打开位置':'Where local links open','内置浏览器':'Built-in browser','系统默认浏览器':'System default browser',
  'Markdown 默认打开方式':'Default Markdown open mode',
  '打开 .md 文件时使用的视图（写入 workbench.editorAssociations）':'View used for .md files (writes workbench.editorAssociations)',
  '代码编辑器':'Code editor','预览':'Preview','网络':'Network','网络代理':'Network proxy',
  'IDE 与 AI 服务访问网络时使用的代理，保存后立即生效并作为后端启动参数':'Proxy for the IDE and AI service; applied immediately and at backend startup',
  '系统':'System','无代理':'No proxy','自定义':'Custom','代理地址':'Proxy address',
  '自定义代理服务器，例如 http://127.0.0.1:7890':'Custom proxy server, e.g. http://127.0.0.1:7890',
  '已应用代理':'Proxy applied','已切换为直连':'Switched to direct connection','代理应用失败：服务未连接':'Failed to apply proxy: service offline',
  'AI 水印':'AI watermark','AI 生成内容附加来源标识':'Add a source mark to AI-generated content',
  'Ctrl+F 搜索':'Ctrl+F search','已保存':'Saved','保存失败：服务未连接':'Save failed: service offline',
  '取消':'Cancel','删除':'Delete','打开':'Open','刷新':'Refresh','添加':'Add','创建':'Create',
  /* TRAE 移动端 / 悬浮球 */
  '在手机上继续使用 wasily。':'Continue using wasily on your phone.',
  '移动端 App 正在规划中，扫码预约内测资格':'Mobile app is in planning; scan to join the beta.',
  '当前可通过手机浏览器访问本机服务：http://<本机IP>:8000/chat':'Access the local service from a phone browser: http://<LAN-IP>:8000/chat',
  '桌面悬浮大脑外观、行为与启动控制。':'Appearance, behavior and launch control of the desktop brain.',
  '启动桌面大脑':'Launch desktop brain','已拉起桌面大脑':'Desktop brain launched',
  '启动失败：服务未连接':'Launch failed: service offline','打开外观参数面板':'Open appearance panel',
  '已发送指令':'Command sent','启动失败':'Launch failed','悬浮大脑':'Floating brain',
  '在桌面显示可拖动的大脑形象，右键呼出菜单':'Show a draggable brain on the desktop; right-click for the menu',
  '外观':'Appearance','窗口透明度':'Window opacity','大脑窗口整体不透明度':'Overall opacity of the brain window',
  '窗口圆角':'Corner radius','异形窗口圆角半径（像素）':'Corner radius of the shaped window (px)',
  '置顶显示':'Always on top','大脑窗口始终位于其他窗口之上':'Keep the brain above other windows',
  '行为':'Behavior','资源自动降级':'Auto degrade on load','高负载时自动降低动画帧率/隐藏动画':'Lower FPS or hide animation under high load',
  '手动暂停':'Pause manually','暂停大脑所有动画渲染':'Pause all brain animations',
  /* 开发环境 / 权限 */
  '本地 AI 服务与运行时环境状态。':'Local AI service and runtime status.',
  '服务状态':'Service status','在线':'Online','服务地址':'Service URL','文档中心':'Docs',
  '刷新状态':'Refresh status','查看运行日志':'View runtime log',
  '打开 api_server.log 诊断工具调用链路':'Open api_server.log to inspect tool calls','打开日志目录':'Open log folder',
  '已在资源管理器中打开日志目录':'Log folder opened in Explorer','打开失败':'Open failed',
  'AI 服务未连接，请先启动服务':'AI service offline; start it first',
  '敏感操作的人工确认策略。':'Approval policy for sensitive actions.',
  '如何批准 Agent 的操作？':'How are agent actions approved?','了解更多':'Learn more',
  '手动审批':'Manual approval','重要操作由你确认':'You confirm important actions',
  '自动审批':'Auto approval','由 AI 审核，必要时再询问你':'AI reviews and asks when needed',
  '完全访问':'Full access','不经审批，直接在本机运行':'Run locally without approval',
  '通过配置文件设置规则':'Rules set in the config file','细粒度控制':'Fine-grained control',
  '允许安装工具':'Allow tool installation','允许 AI 调用安装工具（pip 安装白名单包）':'Allow the AI to install whitelisted pip packages',
  '敏感操作需人工批准':'Sensitive actions need approval',
  '安装/修改源码等敏感操作弹出确认卡片，等待人工批准（权限模式为「完全访问」时不弹出）':'Installing packages or editing source shows a confirmation card (skipped in Full access mode)',
  '确认等待超时':'Confirmation timeout','超时未确认视为拒绝（秒）':'Timeout means deny (seconds)',
  '自我进化':'Self evolution','源码修改四层闸门':'Four-stage gate for source edits',
  '修改项目自身源码必须走 沙箱暂存 → diff 展示 → 人工确认 → 落盘，该策略内置强制开启':'Editing this project\'s own source requires sandbox staging → diff → approval → write; always enforced',
  '强制开启':'Always on',
  /* 智能体 / 插件 / 模型 */
  '管理内置与自定义智能体。自定义智能体可在对话输入栏的智能体选择器中切换。':'Manage built-in and custom agents. Custom agents can be switched in the chat agent picker.',
  '＋ 创建智能体':'+ New agent','内置智能体':'Built-in agents',
  '对话模式：直接问答，不自动执行工具':'Chat mode: answer without running tools',
  '任务模式：多轮工具调用，自主完成开发任务':'Task mode: multi-round tool calls to finish dev tasks',
  '内置':'Built-in','自定义智能体':'Custom agents','暂无自定义智能体':'No custom agents',
  '（无提示词）':'(no prompt)','确认删除智能体？删除后无法恢复。':'Delete this agent? This cannot be undone.',
  '删除失败':'Delete failed','已删除':'Deleted','创建智能体':'Create agent','✚ 智能生成':'✚ Auto generate',
  '更换头像':'Change avatar','名称':'Name','请输入智能体名称':'Enter agent name','提示词':'Prompt',
  '请先填写名称，再智能生成提示词':'Fill in the name before generating a prompt','生成中…':'Generating…',
  '已生成提示词':'Prompt generated','生成结果为空':'Empty result','可被其他智能体调用':'Callable by other agents',
  '英文标识名':'English identifier','何时调用':'When to call','阅读':'Read','对文件进行检索和查看':'Search and read files',
  '编辑':'Edit','对文件进行增删和编辑':'Create, delete and edit files','终端':'Terminal',
  '在终端运行命令并获取状态和结果':'Run commands and collect results','联网搜索':'Web search',
  '搜索和用户任务相关的网页内容':'Search the web for task-related content','请填写智能体名称':'Enter agent name',
  '已创建':'Created','创建失败':'Create failed',
  '发现并安装插件、技能和 MCP，扩展 wasily 的能力。':'Discover and install plugins, skills and MCP to extend wasily.',
  '浏览全部插件与服务':'Browse all plugins and services','打开插件市场':'Open plugin marketplace',
  '已安装的自定义工具':'Installed custom tools','加载中…':'Loading…',
  '暂无自定义工具（user_tools/ 目录）':'No custom tools (user_tools/)','已启用':'Enabled','暂无 MCP 工具':'No MCP tools',
  '代码智能补全与上下文理解引擎（Context Understanding Engine）。':'Inline completion and context engine (CUE).',
  '启用 CUE':'Enable CUE','编码时提供行内智能补全建议':'Inline completion while coding','补全延迟':'Completion delay',
  '触发补全前的停顿毫秒数':'Idle milliseconds before suggesting','最大建议长度':'Max suggestion length',
  '单次补全返回的最大字符数':'Max characters per suggestion','模型管理':'Models',
  '配置 API key 添加更多可用模型，预置模型默认使用稳定版本。':'Configure API keys to add models; presets use stable versions.',
  '＋ 添加模型':'+ Add model','专属补贴':'Exclusive subsidy','会员限时优惠':'Limited member offer','闲时折扣':'Off-peak discount',
  '暂无自定义模型，你可以在这里添加自定义模型':'No custom models yet; add one here','添加自定义模型':'Add custom model',
  '模型名称':'Model name','服务商':'Provider','接口地址（可选）':'Endpoint (optional)','请填写模型名称':'Enter model name',
  '已添加模型':'Model added','本地推理模型':'Local inference model',
  '本机 GGUF 推理参数，修改后需重启 AI 服务生效。':'Local GGUF parameters; restart the AI service to apply.',
  '模型文件':'Model file','GGUF 模型路径':'GGUF model path','上下文窗口':'Context window',
  'n_ctx，窗口越大占用内存越多':'n_ctx; larger uses more memory','单次最大生成':'Max tokens per reply',
  'GPU 层数':'GPU layers','-2 自动按显存计算，0 纯 CPU':'-2 auto by VRAM, 0 = CPU only',
  'CPU 线程数':'CPU threads','0 自动（按核心数）':'0 = auto (core count)','重启 AI 服务':'Restart AI service',
  '请使用启动工具 restart 子命令重启服务':'Use the launcher restart subcommand','应用更改':'Apply changes',
  '保存配置后需重启服务加载新参数':'Restart the service to load new parameters',
  /* 对话流 / 浏览器 / 电脑控制 / 索引 */
  '对话体验与朗读设置。':'Chat and speech settings.','AI 名称':'AI name','对话中显示的助理名称':'Assistant name shown in chat',
  '朗读音色':'TTS voice','TTS 语音':'TTS voice','晓晓（女声）':'Xiaoxiao (female)','云希（男声）':'Yunxi (male)',
  '晓伊（女声）':'Xiaoyi (female)','离线语音回退':'Offline voice fallback',
  'edge-tts 失败时回退 Windows 离线语音':'Fall back to Windows SAPI when edge-tts fails',
  '默认朗读':'Auto read by default','新会话默认开启朗读（默认关闭）':'Read new sessions aloud (off by default)',
  '思考过程展示':'Show thinking process','按时间序交错渲染思考段落与工具调用统计':'Interleave thinking and tool stats chronologically',
  '功能页面与链接的打开方式。':'How feature pages and links open.','功能页面打开方式':'Feature page opening',
  '大脑右键菜单功能页面的打开位置':'Where brain menu pages open','内嵌窗口':'Embedded','独立页面（浏览器）':'Standalone (browser)',
  '页面打开白名单':'Page allowlist','仅允许预设合法路径打开，防止任意 URL 注入':'Only preset paths may open, preventing URL injection',
  '外设与本地设备扫描控制。':'Peripheral and device scanning.','启用设备扫描':'Enable device scan',
  '允许 AI 扫描本地设备（摄像头/串口等）':'Allow the AI to scan local devices (camera/serial)',
  '扫描超时':'Scan timeout','单次设备扫描超时时间（秒）':'Per-scan timeout (seconds)',
  '项目文档中心与知识索引。':'Project docs and knowledge index.','文档中心首页':'Docs home',
  '全部项目文档门户':'Portal for all project docs','wasily 项目文档':'wasily docs','本项目文档':'This project docs',
  '代码索引':'Code index','工作区符号索引，加速 AI 检索':'Workspace symbol index for faster AI retrieval','自动索引中':'Indexing',
  /* 技能 / 记忆 / Hooks / Beta / 关于 */
  '技能存放于 skills/ 目录，对话中命中触发词即自动注入提示词。也可以绑定到自定义 Agent 让它始终生效。':'Skills live in skills/; matching triggers inject prompts automatically. Bind them to a custom agent to always apply.',
  '默认绑定到 Agent':'Bind to agents by default',
  '在此勾选的技能会在每次对话时始终注入提示词（不管触发词是否命中）':'Checked skills are injected on every message regardless of triggers',
  '读取下方设置':'Read the setting below','＋ 创建新技能':'+ New skill','🔄 重新扫描':'🔄 Rescan','已刷新':'Refreshed',
  '暂无技能':'No skills','触发词：':'Triggers: ','服务未连接':'Service offline','编辑技能':'Edit skill',
  '多层记忆金字塔：L1 会话摘要 → L2 长期事实 → L3 核心认知。':'Memory pyramid: L1 session summaries → L2 long-term facts → L3 core knowledge.',
  '立即压缩':'Compress now','压缩完成':'Compression done','记忆压缩':'Memory compression','手动触发待压缩队列归并':'Trigger pending memory compaction',
  '暂无记忆条目':'No memory entries','在 AI 工作流程的关键节点注入自定义脚本。':'Run custom scripts at key points of the AI workflow.',
  '对话前 Hook':'Pre-chat hook','每轮对话发送前执行':'Run before each message is sent','对话后 Hook':'Post-chat hook',
  '每轮对话完成后执行':'Run after each turn','工具调用前 Hook':'Pre-tool hook','工具调用前拦截，可返回拒绝':'Intercept before tool calls; may deny',
  '留空表示不启用。Hook 为本地脚本路径，输出 JSON 控制行为。':'Leave empty to disable. Hooks are local script paths returning JSON.',
  '实验性功能，可能不稳定。':'Experimental features; may be unstable.','多 AI 群聊':'Multi-AI group chat',
  '多个 AI 同时参与讨论，用户指令优先级最高':'Multiple AIs discuss together; user instructions win','SOLO 自主模式':'SOLO autonomous mode',
  'AI 自主拆解并执行完整开发计划':'The AI plans and executes a full dev plan','卷积上下文压缩':'Convolutional context compression',
  '四层池化压缩，超长任务永不超限（当前已内置启用）':'Four-layer pooling keeps long tasks within limits (built in)',
  '已内置':'Built in','LoRA 微调':'LoRA fine-tuning','基于历史对话自训练模型适配器':'Train adapters from chat history',
  'wasily IDE — 本地优先的 AI 编程工作台。':'wasily IDE — a local-first AI coding workbench.',
  '版本':'Version','AI 服务':'AI service','检查更新':'Check for updates','当前已是最新版本':'You are up to date',
  '帮助文档':'Help docs','版本与更新':'Version & updates','数据不出机 · 全部本地运行':'Data stays on this machine · fully local'
};

function isEN(){ return getCfg('ui.language','zh-CN')==='en-US'; }

// 单句翻译：仅整段匹配时替换，避免误伤用户数据/代码
function tr(s){
  if(!isEN() || typeof s!=='string') return s;
  const k=s.trim(); if(!k || !EN[k]) return s;
  return s.replace(k, EN[k]);
}

// 渲染完成后统一过一遍文本与占位符（幂等：仅在 EN 模式下生效）
function applyI18n(root){
  document.documentElement.setAttribute('lang', isEN()?'en':'zh-CN');
  if(!isEN()) return;
  const box=root||document.body;
  const walker=document.createTreeWalker(box, NodeFilter.SHOW_TEXT, null);
  const nodes=[]; let n;
  while((n=walker.nextNode())) nodes.push(n);
  nodes.forEach(function(node){
    const pe=node.parentElement;
    if(!pe || /^(SCRIPT|STYLE)$/.test(pe.nodeName)) return;
    const t=node.nodeValue; const k=t.trim();
    if(EN[k]) node.nodeValue=t.replace(k, EN[k]);
  });
  box.querySelectorAll('[placeholder],[title]').forEach(function(e){
    ['placeholder','title'].forEach(function(a){
      const v=e.getAttribute(a);
      if(v && EN[v.trim()]) e.setAttribute(a, EN[v.trim()]);
    });
  });
}

/* ---------- 导航定义 ---------- */
const NAV = [
  {sec:'账户'},
  {id:'account',  name:'账号',       icon:IC.user},
  {id:'usage',    name:'用量管理',   icon:IC.chart},
  {sec:'设置'},
  {id:'general',  name:'通用',       icon:IC.gear},
  {id:'mobile',   name:'TRAE移动端', icon:IC.phone},
  {id:'brain',    name:'桌面悬浮球', icon:IC.ball},
  {id:'devenv',   name:'开发环境',   icon:IC.code},
  {id:'perm',     name:'权限审批',   icon:IC.shield},
  {sec:'能力'},
  {id:'agent',    name:'智能体',     icon:IC.bot,    ext:'/agent#assistant'},
  {id:'plugins',  name:'插件市场',   icon:IC.puzzle, ext:'/agent'},
  {id:'mcp',      name:'MCP',        icon:IC.plug,   ext:'/agent#mcp'},
  {id:'cue',      name:'CUE',        icon:IC.cursor},
  {id:'model',    name:'模型',       icon:IC.chip},
  {id:'dialog',   name:'对话流',     icon:IC.chat},
  {id:'browser',  name:'浏览器',     icon:IC.globe},
  {id:'computer', name:'电脑控制',   icon:IC.monitor},
  {id:'index',    name:'索引与文档', icon:IC.folder},
  {id:'skills',   name:'技能与命令', icon:IC.zap},
  {id:'memory',   name:'规则与记忆', icon:IC.book},
  {id:'hooks',    name:'Hooks',      icon:IC.hook},
  {id:'beta',     name:'Beta',       icon:IC.flask},
  {sec:'其他'},
  {id:'about',    name:'关于TRAE',   icon:IC.info},
];

/* ---------- 各面板 ---------- */
const PANES = {

account(p){
  p.appendChild(el('h1',null,'账号'));
  p.appendChild(el('div','desc','本地账户信息，数据仅存储在本机。'));
  const c=el('div','card');
  const a=el('div','acct');
  const av=el('div','av'); av.innerHTML=icon(IC.user); a.appendChild(av);
  const mid=el('div'); mid.appendChild(el('div','nm','—'));
  const s2=el('div','sub2'); s2.innerHTML='免费版 <span class="badge g">本地模式</span>'; mid.appendChild(s2);
  a.appendChild(mid);
  const cr=el('div','cr'); cr.appendChild(el('div','n','—')); cr.appendChild(el('div','t','剩余积分'));
  a.appendChild(cr); c.appendChild(a);
  p.appendChild(c);
  p.appendChild(grp('资料'));
  const nm=el('input'); nm.type='text'; nm.placeholder='输入昵称'; nm.maxLength=20;
  const save=btnCtl('保存',()=>{
    if(!nm.value.trim()) return toast('昵称不能为空');
    post('/account/rename',{name:nm.value.trim()}).then(()=>{ toast('昵称已更新'); loadAccount(); }).catch(()=>toast('保存失败'));
  },'pri');
  const wrap=el('div','ctl'); wrap.appendChild(nm); wrap.appendChild(save);
  p.appendChild(row('昵称','修改后同步显示在标题栏与菜单',wrap));
  p.appendChild(grp('积分'));
  p.appendChild(row('每日签到','每天可领取 100 积分', btnCtl('签到',()=>{
    post('/account/signin').then(d=>{
      toast(d.ok?'签到成功，积分 +100':(d.already?'今日已签到':'签到失败')); loadAccount();
    }).catch(()=>toast('AI 服务未连接'));
  },'green')));
  const stats=el('div','kv'); stats.id='accStats'; p.appendChild(stats);

  function loadAccount(){
    api('/account/info').then(d=>{
      mid.querySelector('.nm').textContent=d.name; nm.value=d.name;
      cr.querySelector('.n').textContent=fmt(d.credits);
      s2.innerHTML=(d.plan||'免费')+'版 <span class="badge g">本地模式</span>';
      stats.innerHTML='';
      [['累计签到',(d.sign_total||0)+' 天'],['累计获得积分',fmt((d.sign_total||0)*100)],['邀请好友',(d.invite_count||0)+' 人']]
        .forEach(kv=>{ const b=el('div','box'); b.appendChild(el('div','k',kv[0])); b.appendChild(el('div','v',kv[1])); stats.appendChild(b); });
    }).catch(()=>{ mid.querySelector('.nm').textContent='（服务未连接）'; });
  }
  loadAccount();
},

usage(p){
  p.appendChild(el('h1',null,'用量管理'));
  p.appendChild(el('div','desc','您当前方案为 免费'));

  // 邀请横幅（截图 bf51721d：绿色描边 + 立即邀请）
  const inv=el('div'); inv.style.cssText='display:flex;align-items:center;gap:10px;margin:14px 0 12px;padding:11px 14px;border:1px solid rgba(62,207,142,.28);background:rgba(62,207,142,.06);border-radius:10px;font-size:12.5px;color:#cfd8d2';
  inv.innerHTML='<span style="width:18px;height:18px;border-radius:5px;background:rgba(62,207,142,.16);color:#3ecf8e;display:inline-flex;align-items:center;justify-content:center;flex:none">🎁</span>'+
    '<span style="flex:1">邀请好友体验 Wasily 桌面端，最高得 <b style="color:#8fe3bb">3500</b> 积分 + <b style="color:#8fe3bb">2</b> 张汉堡券</span>';
  const ib=el('button',null,'立即邀请'); ib.style.cssText='flex:none;height:28px;padding:0 12px;border-radius:7px;border:1px solid #3a3a3a;background:#262626;color:#e0e0e0;font-size:12px;cursor:pointer;font-family:inherit';
  ib.onclick=()=>toast('邀请链接已复制'); inv.appendChild(ib); p.appendChild(inv);

  // 当前总可用积分卡（与顶栏 ✧ 549 同一数值）
  const pts=el('div'); pts.style.cssText='padding:14px 16px;border:1px solid var(--line);border-radius:12px;background:var(--card);margin-bottom:14px';
  pts.innerHTML='<div style="font-size:12px;color:var(--muted);margin-bottom:8px">当前总可用积分 <span style="color:var(--dim)">ⓘ</span></div>'+
    '<div style="display:flex;align-items:flex-end;gap:12px"><div style="font-size:28px;font-weight:700;line-height:1;color:#f2f2f2">549.24</div>'+
    '<button id="upgradeBtn" style="margin-left:auto;height:30px;padding:0 12px;border-radius:7px;border:1px solid #3a3a3a;background:#2a2a2a;color:#e6e6e6;font-size:12px;cursor:pointer;font-family:inherit">升级权益</button></div>';
  p.appendChild(pts);
  const up=pts.querySelector('#upgradeBtn'); if(up) up.onclick=()=>toast('当前已是最新方案');

  const link=el('div','desc'); link.innerHTML='有积分兑换码？ <span style="color:var(--accent);cursor:pointer">兑换积分</span>';
  p.appendChild(link);

  p.appendChild(grp('积分明细'));
  const detail=el('div','card sec');
  [['新用户福利','2026/12/19 23:03 到期','剩余 449.24 / 1,500'],
   ['签到奖励','2026/12/20 02:07 到期','剩余 100 / 100'],
   ['每月登录赠送','2026/11/30 23:59 到期','剩余 0 / 500']].forEach(it=>{
    const r=el('div'); r.style.cssText='display:flex;align-items:center;gap:10px;padding:11px 14px;border-bottom:1px solid var(--line)';
    r.innerHTML='<div style="flex:1;min-width:0"><div style="font-size:12.5px;color:var(--txt2)">'+it[0]+'</div>'+
      '<div style="font-size:11.5px;color:var(--dim);margin-top:3px">'+it[1]+'</div></div>'+
      '<div style="font-size:12px;color:var(--muted);flex:none">'+it[2]+'</div>';
    detail.appendChild(r);
  });
  detail.lastChild.style.borderBottom='0';
  p.appendChild(detail);

  p.appendChild(grp('用量明细'));
  const kv=el('div','kv'); p.appendChild(kv);
  const tbl=el('table','tbl'); p.appendChild(tbl);
  api('/token_stats').then(d=>{
    kv.innerHTML='';
    const tot=d.total||{};
    [['累计 Tokens',fmt((tot.prompt||0)+(tot.gen||0))],['输入 Tokens',fmt(tot.prompt||0)],
     ['输出 Tokens',fmt(tot.gen||0)],['对话轮数',fmt(tot.turns||0)]].forEach(it=>{
      const b=el('div','box'); b.appendChild(el('div','k',it[0])); b.appendChild(el('div','v',it[1])); kv.appendChild(b);
    });
    tbl.innerHTML='<tr><th>会话</th><th>输入</th><th>输出</th><th>轮数</th></tr>';
    const ss=d.sessions||{};
    const keys=Object.keys(ss);
    if(!keys.length){ tbl.innerHTML+='<tr><td colspan="4" style="color:var(--dim)">暂无记录</td></tr>'; }
    keys.forEach(k=>{ const s=ss[k];
      const tr=el('tr');
      [k,(fmt(s.prompt||0)),(fmt(s.gen||0)),(s.turns||0)].forEach(v=>{ tr.appendChild(el('td',null,String(v))); });
      tbl.appendChild(tr);
    });
  }).catch(()=>{ kv.innerHTML='<div class="empty">AI 服务未连接</div>'; });
},

general(p){
  p.appendChild(el('h1',null,'通用'));

  // 一组设置行装进同一张卡片，行间用细分隔线（对齐 TRAE 设置页的版式）
  const block=function(title,build){
    p.appendChild(grp(title));
    const c=el('div','card sec');
    const add=function(){ for(var i=0;i<arguments.length;i++) if(arguments[i]) c.appendChild(arguments[i]); };
    build(add);
    p.appendChild(c);
  };

  themeDlg();
  loadThemes();

  block('基础设置',function(add){
    add(row('主题','选择主题',themeCtl()));
    add(row('语言','选择您喜欢的按钮标签和应用内其他文本的语言',
      selCtl('ui.language',[['zh-CN','简体中文'],['en-US','English']],function(){
        renderNav(); show(location.hash.slice(1)||'general');   // 语言切换后整页重渲染
      })));
  });

  block('偏好设置',function(add){
    add(row('默认启动窗口','选择下次启动 IDE 时打开的窗口',
      selCtl('ide.startup_window',[['system','系统默认（跟随上次打开）'],['new','新建窗口'],['restore','恢复上次项目'],['none','空工作台']])));
    add(row('关闭最后一个窗口时','关闭 IDE 最后一个窗口后的行为',
      selCtl('ide.close_last_window',[['quit','退出应用'],['keep','保持后台运行（托盘）']])));

    const eb=el('div','ctl'); eb.appendChild(btnCtl('去设置',()=>bridge('openEditorSettings')));
    add(row('Editor 设置','Editor 中字体、word-wrap、window 设置等',eb));

    const kb=el('div','ctl');
    kb.appendChild(el('span','mono','使用 VS Code 快捷键风格'));
    kb.appendChild(gearCtl(()=>bridge('openKeybindings')));
    add(row('快捷键设置','对 IDE 中的各个操作的快捷键进行自定义设置',kb));

    const vc=el('div','ctl');
    vc.appendChild(swCtl('voice.shortcut_enabled'));
    vc.appendChild(el('span','key','Alt + V'));
    vc.appendChild(gearCtl(()=>bridge('openKeybindings')));
    add(row('语音转录快捷键','开启或关闭语音转录快捷键，录制自定义组合键，或恢复默认值。',vc));

    const fc=el('div','ctl');
    fc.appendChild(el('span','key','Alt + Shift + Space'));
    fc.appendChild(gearCtl(()=>bridge('openKeybindings')));
    add(row('打开对话浮窗','设置快捷键，随时打开对话浮窗。',fc));

    add(row('发送按键','选择对话输入框中发送消息的按键',
      selCtl('ui.send_key',[['enter','Enter 发送'],['ctrl-enter','Ctrl+Enter 发送']])));

    const ib=el('div','ctl');
    ib.appendChild(btnCtl('导入',()=>$('#importFile').click()));
    ib.appendChild(btnCtl('导出',()=>{
      const blob=new Blob([JSON.stringify(CFG,null,2)],{type:'application/json'});
      const a=el('a'); a.href=URL.createObjectURL(blob); a.download='wasily-config.json'; a.click();
      setTimeout(()=>URL.revokeObjectURL(a.href),3000);
    }));
    add(row('导入配置','导入 JSON 配置文件，快速恢复全部设置',ib));

    add(row('本地链接默认打开方式','点击本地链接时的打开位置',
      selCtl('ui.page_open_mode',[['embedded','内置浏览器'],['browser','系统默认浏览器']])));
    add(row('Markdown 文件的默认打开方式','Markdown 文件将默认用此方式打开',
      selCtl('ide.markdown_open',[['editor','代码编辑器'],['preview','预览']],function(){ bridge('applyMarkdownOpen'); })));
  });

  block('网络',function(add){
    // 代理：切换后立即调用后端重新注入环境变量；custom 时额外显示地址输入
    const proxyUrlCtl=textCtl('network.proxy_url','240px');
    proxyUrlCtl.addEventListener('change',function(){ setTimeout(applyProxy,150); });
    const proxyUrlRow=row('代理地址','自定义代理服务器，例如 http://127.0.0.1:7890',proxyUrlCtl);
    function applyProxy(){
      post('/network/proxy/apply').then(d=>{
        toast(d.proxy ? ('已应用代理 '+d.proxy) : '已切换为直连');
      }).catch(()=>toast('代理应用失败：服务未连接'));
    }
    const proxySel=selCtl('network.proxy',[['system','系统'],['none','无代理'],['custom','自定义']],function(v){
      proxyUrlRow.style.display = v==='custom' ? '' : 'none';
      setTimeout(applyProxy,150);   // 等 /config/set 落盘后再应用
    });
    proxyUrlRow.style.display = getCfg('network.proxy','system')==='custom' ? '' : 'none';
    add(row('网络代理','为应用配置网络代理',proxySel));
    add(proxyUrlRow);
  });

  block('AI 水印',function(add){
    add(row('隐藏 AI 生成标识','默认关闭。开启后，您后续生成的产物内容将不再附加“AI 生成”的水印标识。',
      swCtl('ui.hide_ai_mark')));
  });
},

mobile(p){
  p.appendChild(el('h1',null,'TRAE移动端'));
  p.appendChild(el('div','desc','在手机上继续使用 wasily。'));
  const c=el('div','card'); c.style.textAlign='center';
  c.appendChild(el('div',null,'移动端 App 正在规划中，扫码预约内测资格'));
  const q=el('div','qr'); q.style.display='inline-block'; c.appendChild(q);
  const d=el('div','sub2'); d.style.color='var(--muted)'; d.style.fontSize='12px'; d.textContent='当前可通过手机浏览器访问本机服务：http://<本机IP>:8000/chat';
  c.appendChild(d); p.appendChild(c);
},

brain(p){
  p.appendChild(el('h1',null,'桌面悬浮球'));
  p.appendChild(el('div','desc','桌面悬浮大脑外观、行为与启动控制。'));
  const ctl=el('div','ctl');
  ctl.appendChild(btnCtl('启动桌面大脑',()=>{
    post('/spawn_brain').then(()=>toast('已拉起桌面大脑')).catch(()=>toast('启动失败：服务未连接'));
  },'pri'));
  ctl.appendChild(btnCtl('打开外观参数面板',()=>{
    post('/spawn_brain',{panel:1}).then(()=>toast('已发送指令')).catch(()=>toast('启动失败'));
  }));
  p.appendChild(row('悬浮大脑','在桌面显示可拖动的大脑形象，右键呼出菜单',ctl));
  p.appendChild(grp('外观'));
  const op=el('div','ctl');
  const range=document.createElement('input'); range.type='range'; range.min=10; range.max=100;
  range.value=Math.round((getCfg('ui.opacity',1))*100);
  const opv=el('span','mono',range.value+'%');
  range.oninput=()=>{ opv.textContent=range.value+'%'; };
  range.onchange=()=>setCfg('ui.opacity', range.value/100);
  op.appendChild(range); op.appendChild(opv);
  p.appendChild(row('窗口透明度','大脑窗口整体不透明度',op));
  p.appendChild(row('窗口圆角','异形窗口圆角半径（像素）',numCtl('ui.radius',0,64)));
  p.appendChild(row('置顶显示','大脑窗口始终位于其他窗口之上',swCtl('ui.avatar_on_top')));
  p.appendChild(grp('行为'));
  p.appendChild(row('资源自动降级','高负载时自动降低动画帧率/隐藏动画',swCtl('resource.enabled')));
  p.appendChild(row('手动暂停','暂停大脑所有动画渲染',swCtl('resource.manual_pause')));
},

devenv(p){
  p.appendChild(el('h1',null,'开发环境'));
  p.appendChild(el('div','desc','本地 AI 服务与运行时环境状态。'));
  const kv=el('div','kv'); p.appendChild(kv);
  function load(){
    api('/health').then(d=>{
      kv.innerHTML='';
      [['服务状态','在线'],['模型',d.model||'—'],['服务地址','127.0.0.1:8000'],['文档中心','127.0.0.1:8001']].forEach(it=>{
        const b=el('div','box'); b.appendChild(el('div','k',it[0]));
        const v=el('div','v',it[1]); v.style.fontSize='14px'; b.appendChild(v); kv.appendChild(b);
      });
    }).catch(()=>{ kv.innerHTML='<div class="empty">AI 服务未连接，请先启动服务</div>'; });
  }
  load();
  p.appendChild(row('刷新状态','',btnCtl('刷新',load)));
  p.appendChild(row('查看运行日志','打开 api_server.log 诊断工具调用链路',btnCtl('打开日志目录',async ()=>{
    try{
      const r=await (await fetch('/dev/open_log_dir',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
      toast(r.ok?'已在资源管理器中打开日志目录':(r.error||'打开失败'));
    }catch(e){ toast('AI 服务未连接'); }
  })));
},

perm(p){
  p.appendChild(el('h1',null,'权限审批'));
  p.appendChild(el('div','desc','敏感操作的人工确认策略。'));

  // 如何批准 Agent 的操作（图2）
  const head=el('div'); head.style.cssText='display:flex;align-items:center;margin:18px 0 8px';
  head.appendChild(el('div',null,'如何批准 Agent 的操作？')).style.cssText='font-size:13px;color:var(--txt2)';
  const more=el('span','lnk','了解更多'); more.style.marginLeft='auto';
  more.onclick=()=>window.open('http://127.0.0.1:8001/','_blank');
  head.appendChild(more);
  p.appendChild(head);

  const OPTS=[
    ['manual','手动审批','重要操作由你确认','M12 12a3.4 3.4 0 1 0 0-6.8 3.4 3.4 0 0 0 0 6.8zM5 19.5c.7-3 3.4-4.6 7-4.6s6.3 1.6 7 4.6'],
    ['auto','自动审批','由 AI 审核，必要时再询问你','M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4L12 3zM18.5 15l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1z'],
    ['full','完全访问','不经审批，直接在本机运行','M12 3l8 3v6c0 4.5-3.2 7.7-8 9-4.8-1.3-8-4.5-8-9V6zM9 12l2 2 4-4'],
    ['custom','自定义','通过配置文件设置规则','M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 1-.1 1.2l2 1.5-2 3.5-2.4-1a7 7 0 0 1-2 1.2L14 21h-4l-.4-2.6a7 7 0 0 1-2-1.2l-2.4 1-2-3.5 2-1.5A7 7 0 0 1 5 12a7 7 0 0 1 .1-1.2l-2-1.5 2-3.5 2.4 1a7 7 0 0 1 2-1.2L10 3h4l.4 2.6a7 7 0 0 1 2 1.2l2.4-1 2 3.5-2 1.5a7 7 0 0 1 .1 1.2z']
  ];
  const box=el('div'); box.style.cssText='border:1px solid var(--line);border-radius:12px;padding:6px;background:rgba(255,255,255,.02)';
  function cur(){ return getCfg('permission.mode','full'); }
  OPTS.forEach(o=>{
    const it=el('div','opt'+(cur()===o[0]?' on':''));
    it.innerHTML='<span class="oi"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="'+o[3]+'"/></svg></span>'
      +'<span><div class="ot">'+o[1]+'</div><div class="od">'+o[2]+'</div></span>'
      +'<span class="ock"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 13l4 4L19 7"/></svg></span>';
    it.onclick=()=>{
      setCfg('permission.mode', o[0]);
      box.querySelectorAll('.opt').forEach(x=>x.classList.remove('on'));
      it.classList.add('on');
    };
    box.appendChild(it);
  });
  p.appendChild(box);

  p.appendChild(grp('细粒度控制'));
  p.appendChild(row('允许安装工具','允许 AI 调用安装工具（pip 安装白名单包）',swCtl('install_tool.allow')));
  p.appendChild(row('敏感操作需人工批准','安装/修改源码等敏感操作弹出确认卡片，等待人工批准（权限模式为「完全访问」时不弹出）',swCtl('install_tool.allow_confirm')));
  p.appendChild(row('确认等待超时','超时未确认视为拒绝（秒）',numCtl('install_tool.confirm_timeout',30,600)));
  p.appendChild(grp('自我进化'));
  p.appendChild(row('源码修改四层闸门','修改项目自身源码必须走 沙箱暂存 → diff 展示 → 人工确认 → 落盘，该策略内置强制开启',el('span','badge g','强制开启')));
},

agent(p){
  renderAgentList(p);

  function renderAgentList(pp){
    pp.appendChild(el('h1',null,'智能体'));
    pp.appendChild(el('div','desc','管理内置与自定义智能体。自定义智能体可在对话输入栏的智能体选择器中切换。'));
    const bar=el('div','ctl'); bar.style.margin='10px 0 4px';
    bar.appendChild(btnCtl('＋ 创建智能体',()=>{ const c=$('#content'); c.innerHTML=''; const np=el('section','pane on'); c.appendChild(np); renderCreate(np); },'pri'));
    pp.appendChild(bar);

    pp.appendChild(grp('内置智能体'));
    [['Chat','对话模式：直接问答，不自动执行工具'],['Agent','任务模式：多轮工具调用，自主完成开发任务']].forEach(a=>{
      const it=el('div','list-it');
      const g=el('div','grow'); g.appendChild(el('div',null,a[0])); g.appendChild(el('div','d2',a[1]));
      it.appendChild(g); it.appendChild(el('span','tagc','内置'));
      pp.appendChild(it);
    });

    pp.appendChild(grp('自定义智能体'));
    const box=el('div'); pp.appendChild(box);
    function renderCustom(){
      const list=getCfg('agents.custom',[]);
      box.innerHTML='';
      if(!list.length){ box.innerHTML='<div class="empty">暂无自定义智能体</div>'; return; }
      list.forEach((a,i)=>{
        const it=el('div','list-it');
        const g=el('div','grow');
        g.appendChild(el('div',null,a.name+(a.slug?'　'+a.slug:'')));
        g.appendChild(el('div','d2',(a.prompt||'').slice(0,80)||'（无提示词）'));
        it.appendChild(g);
        const del=el('span','lnk','删除');
        del.onclick=async ()=>{
          if(!confirm('确认删除智能体？删除后无法恢复。')) return;
          try{
            const r=await (await fetch('/agent/agent/delete',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({slug:a.slug||a.name})})).json();
            if(!r.ok){ toast(r.error||'删除失败'); return; }
            toast('已删除'); renderCustom();
          }catch(e){ toast('AI 服务未连接'); }
        };
        it.appendChild(del);
        box.appendChild(it);
      });
    }
    renderCustom();
  }

  function renderCreate(pp){
    // 面包屑 + 智能生成
    const head=el('div'); head.style.cssText='display:flex;align-items:center;margin-bottom:18px';
    const crumb=el('div','crumb'); crumb.style.margin='0';
    crumb.innerHTML='<span class="lnk">智能体</span><span class="sep">/</span><b>创建智能体</b>';
    crumb.querySelector('.lnk').onclick=()=>{ const c=$('#content'); c.innerHTML=''; const np=el('section','pane on'); c.appendChild(np); renderAgentList(np); };
    head.appendChild(crumb);
    const gen=el('button','genbtn','✚ 智能生成'); gen.style.marginLeft='auto';
    head.appendChild(gen);
    pp.appendChild(head);

    // 头像 + 名称
    const top=el('div'); top.style.cssText='display:flex;gap:14px;align-items:flex-start';
    const av=el('div','avpick'); av.title='更换头像';
    av.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>';
    av.onclick=()=>{
      const hues=[[ '#5a8dff','#ff5a9e'],['#22b14c','#7be495'],['#f0a35c','#e05555'],['#8f5aff','#4d8dff'],['#0aa37e','#59d98a']];
      const h=hues[Math.floor(Math.random()*hues.length)];
      av.style.background='linear-gradient(135deg,'+h[0]+','+h[1]+')';
    };
    top.appendChild(av);
    const nf=el('div','fld'); nf.style.flex='1'; nf.style.margin='0';
    nf.appendChild(el('div','lb','名称 ')); nf.querySelector('.lb').innerHTML='名称 <i>*</i>';
    const ni=document.createElement('input'); ni.type='text'; ni.placeholder='请输入智能体名称'; ni.maxLength=20;
    nf.appendChild(ni);
    const nc=el('div','cnt','0/20'); nf.appendChild(nc);
    ni.oninput=()=>nc.textContent=ni.value.length+'/20';
    top.appendChild(nf); pp.appendChild(top);

    // 提示词
    const pf=el('div','fld'); pf.style.marginTop='18px';
    pf.appendChild(el('div','lb','提示词'));
    const pt=document.createElement('textarea'); pt.maxLength=10000;
    pt.placeholder='请输入智能体的角色、语气、工作流程、工具偏好及规则规范等。（选填）';
    pf.appendChild(pt);
    const pc=el('div','cnt','0/10000'); pf.appendChild(pc);
    pt.oninput=()=>pc.textContent=pt.value.length+'/10000';
    pp.appendChild(pf);

    gen.onclick=()=>{
      if(!ni.value.trim()) return toast('请先填写名称，再智能生成提示词');
      gen.disabled=true; gen.textContent='生成中…';
      fetch('/v1/chat/completions',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({messages:[{role:'user',content:'为名为「'+ni.value.trim()+'」的 AI 智能体写一段系统提示词（角色、语气、工作流程、规则），200字以内，只输出提示词本身'}],max_tokens:512,session_id:'settings-gen'})})
        .then(r=>r.json()).then(d=>{
          const t=(d.choices&&d.choices[0]&&d.choices[0].message&&d.choices[0].message.content)||'';
          if(t){ pt.value=t.slice(0,10000); pt.dispatchEvent(new Event('input')); toast('已生成提示词'); }
          else toast('生成结果为空');
        }).catch(()=>toast('AI 服务未连接'))
        .finally(()=>{ gen.disabled=false; gen.textContent='✚ 智能生成'; });
    };

    // 可被其他智能体调用
    pp.appendChild(el('div','lb','可被其他智能体调用 ⓘ'));
    const cc=el('div','card'); cc.style.marginTop='6px';
    const crow=el('div'); crow.style.cssText='display:flex;align-items:center;gap:12px';
    const clab=el('div'); clab.style.flex='1';
    clab.appendChild(el('div','t','可被其他智能体调用'));
    const cd=el('div','d2'); cd.style.color='var(--dim)'; cd.style.fontSize='12px'; cd.style.marginTop='3px';
    cd.innerHTML='目前仅可以被内置的 Agent 调用　<span class="lnk">帮助文档</span>';
    cd.querySelector('.lnk').onclick=()=>window.open('http://127.0.0.1:8001/','_blank');
    clab.appendChild(cd);
    crow.appendChild(clab);
    const tgl=document.createElement('input'); tgl.type='checkbox'; tgl.className=''; tgl.checked=true;
    const tglWrap=el('label','sw'); tglWrap.appendChild(tgl); tglWrap.appendChild(document.createElement('i'));
    crow.appendChild(tglWrap);
    cc.appendChild(crow);

    const sf=el('div','fld'); sf.style.marginTop='14px';
    sf.appendChild(el('div','lb','英文标识名')); sf.querySelector('.lb').innerHTML='英文标识名 <i>*</i>';
    const si=document.createElement('input'); si.type='text'; si.maxLength=50;
    si.placeholder='被调用时的唯一英文标识名称，例如：project-analyzer';
    sf.appendChild(si);
    const sc=el('div','cnt','0/50'); sf.appendChild(sc);
    si.oninput=()=>sc.textContent=si.value.length+'/50';
    cc.appendChild(sf);

    const wf=el('div','fld'); wf.style.marginBottom='0';
    wf.appendChild(el('div','lb','何时调用')); wf.querySelector('.lb').innerHTML='何时调用 <i>*</i>';
    const wt=document.createElement('textarea'); wt.maxLength=5000; wt.style.minHeight='90px';
    wt.placeholder='请描述其他智能体调用该智能体的合适场景和时机';
    wf.appendChild(wt);
    const wc=el('div','cnt','0/5000'); wf.appendChild(wc);
    wt.oninput=()=>wc.textContent=wt.value.length+'/5000';
    cc.appendChild(wf);
    pp.appendChild(cc);

    // 工具
    pp.appendChild(el('div','lb','工具 ⓘ')).style.marginTop='18px';
    const tools=el('div','tools');

    // 工具 - MCP
    const tm=el('div','tool-it first');
    const tmC=document.createElement('input'); tmC.type='checkbox'; tmC.className='chk';
    tm.appendChild(tmC);
    tm.appendChild(el('span',null,'工具 - MCP'));
    const addMcp=el('span','lnk','＋ 添加 MCP Servers');
    addMcp.style.marginLeft='auto';
    addMcp.onclick=()=>{ location.hash='mcp'; };
    tm.appendChild(addMcp);
    tools.appendChild(tm);

    // Computer Use
    const cu=el('div','tool-it last');
    const cuC=document.createElement('input'); cuC.type='checkbox'; cuC.className='chk';
    cu.appendChild(cuC);
    cu.innerHTML+='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg><span>Computer Use</span>';
    cu.querySelector('.chk').before(cu.querySelector('svg'));
    tools.appendChild(cu);

    // 工具 - 内置
    const BI=[
      ['阅读','对文件进行检索和查看','M4 5h16v14H4zM8 9h8M8 13h5'],
      ['编辑','对文件进行增删和编辑','M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z'],
      ['终端','在终端运行命令并获取状态和结果','M4 5h16v14H4zM7 9l3 3-3 3M12 15h5'],
      ['预览','在生成前端结果后提供预览入口','M3 5h18v12H3zM10 9l4 2.5-4 2.5z'],
      ['联网搜索','搜索和用户任务相关的网页内容','M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3a13 13 0 0 1 0 18M12 3a13 13 0 0 0 0 18']
    ];
    const gh=el('div','tool-it'); gh.style.marginTop='14px'; gh.style.borderRadius='10px 10px 0 0';
    const ghC=document.createElement('input'); ghC.type='checkbox'; ghC.className='chk'; ghC.checked=true;
    ghC.onchange=()=>{ tools.querySelectorAll('.bi .chk').forEach(c=>c.checked=ghC.checked); };
    gh.appendChild(ghC); gh.appendChild(el('span',null,'工具 - 内置'));
    tools.appendChild(gh);
    BI.forEach((t,i)=>{
      const it=el('div','tool-it bi'+(i===BI.length-1?' last':''));
      const c=document.createElement('input'); c.type='checkbox'; c.className='chk'; c.checked=true; c.dataset.name=t[0];
      it.appendChild(c);
      it.innerHTML+='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="'+t[2]+'"/></svg><span>'+t[0]+'</span><span class="d">'+t[1]+'</span>';
      tools.appendChild(it);
    });
    pp.appendChild(tools);

    // ===== 技能绑定（新增）=====
    const skTitle = el('span',null,'⚡ 技能 - 绑定到此智能体');
    skTitle.style.cssText='font-weight:600;margin-top:18px;display:block';
    pp.appendChild(skTitle);
    const skBox = el('div'); skBox.style.cssText='margin-top:8px';
    pp.appendChild(skBox);
    api('/agent/agents').then(d => {
      const slist = d.skills || [];
      if(!slist.length){ skBox.innerHTML='<div class="d2">暂无可用技能（skills/ 目录为空）</div>'; return; }
      slist.forEach(s => {
        const it = el('div','tool-it');
        const c = document.createElement('input'); c.type='checkbox'; c.className='chk';
        c.dataset.name = s.name;
        it.appendChild(c);
        it.innerHTML += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg><span>'+s.name+'</span><span class="d">'+(s.description||'')+'</span>';
        skBox.appendChild(it);
      });
    }).catch(()=>{ skBox.innerHTML='<div class="d2">服务未连接</div>'; });

    // 创建 / 取消
    const ops=el('div'); ops.style.cssText='display:flex;gap:10px;margin-top:24px';
    // 工具组 → 真实工具名映射（与 Agent 中心/服务端 TOOLS 注册表一致）
    const TOOL_GROUP_MAP={
      '阅读':['file_read','file_list','file_search','code_read','code_search'],
      '编辑':['file_write','file_copy','file_move','file_delete','apply_patch'],
      '终端':['shell_exec','run_python','run_code','process_start','process_list'],
      '预览':['save_output'],
      '联网搜索':['network_request','network_download','network_ping'],
    };
    const COMPUTER_USE_TOOLS=['mouse_move','mouse_click','mouse_double_click','mouse_right_click','mouse_drag','mouse_scroll','mouse_position','key_type','key_press','key_hold','screenshot','clipboard_read','clipboard_write','window_list','window_activate','window_minimize','window_maximize','window_close','open_app','system_control','system_info'];
    const ok=btnCtl('创建',async ()=>{
      const name=ni.value.trim();
      if(!name) return toast('请填写智能体名称');
      const slug=si.value.trim().toLowerCase();
      if(tgl.checked){
        if(!slug) return toast('开启「可被调用」时需填写英文标识名');
        if(!/^[a-z0-9][a-z0-9-]{1,31}$/.test(slug)) return toast('英文标识名需为 2-32 位小写字母/数字/中划线');
        if(!wt.value.trim()) return toast('开启「可被调用」时需填写何时调用');
      } else if(slug && !/^[a-z0-9][a-z0-9-]{1,31}$/.test(slug)){
        return toast('英文标识名需为 2-32 位小写字母/数字/中划线');
      }
      const arr=getCfg('agents.custom',[]);
      if(arr.some(a=>a.name===name || (slug && a.slug===slug))) return toast('已存在同名或同标识名的智能体');
      // 展开为真实工具名（全部勾选 = 不限制，存空数组）
      const allChecked = tools.querySelectorAll('.bi .chk').length
        && Array.from(tools.querySelectorAll('.bi .chk')).every(c=>c.checked) && cuC.checked;
      let btools=[];
      if(!allChecked){
        tools.querySelectorAll('.bi .chk').forEach(c=>{ if(c.checked) btools.push(...(TOOL_GROUP_MAP[c.dataset.name]||[])); });
        if(cuC.checked) btools.push(...COMPUTER_USE_TOOLS);
        btools=[...new Set(btools)];
      }
      // 收集绑定的 skills（skillList 是闭包内的变量，此处用 skBox 里的勾选）
      let bskills=[];
      skBox.querySelectorAll('.chk').forEach(c=>{ if(c.checked) bskills.push(c.dataset.name); });
      // 走服务端校验（与 Agent 中心同一入口，保证数据规范一致）
      ok.disabled=true;
      try{
        const r=await (await fetch('/agent/agent/save',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({name, slug, prompt:pt.value.trim(), callable:tgl.checked,
                               when:wt.value.trim(), tools:btools, skills:bskills})})).json();
        if(!r.ok){ toast(r.error||'创建失败'); return; }
        // 头像属于 UI 展示字段，服务端不管理，创建成功后补写
        if(av.style.background){
          const arr2=getCfg('agents.custom',[]);
          const hit=arr2.find(a=>(slug && a.slug===slug) || (!slug && a.name===name));
          if(hit){ hit.avatar=av.style.background; setCfg('agents.custom',arr2); }
        }
        toast('智能体已创建');
        setTimeout(()=>{ const c=$('#content'); c.innerHTML=''; const np=el('section','pane on'); c.appendChild(np); renderAgentList(np); },300);
      }catch(e){ toast('AI 服务未连接'); }
      finally{ ok.disabled=false; }
    },'pri');
    const cancel=btnCtl('取消',()=>{ const c=$('#content'); c.innerHTML=''; const np=el('section','pane on'); c.appendChild(np); renderAgentList(np); });
    ops.appendChild(ok); ops.appendChild(cancel);
    pp.appendChild(ops);
  }
},

plugins(p){
  p.appendChild(el('h1',null,'插件市场'));
  p.appendChild(el('div','desc','发现并安装插件、技能和 MCP，扩展 wasily 的能力。'));
  p.appendChild(row('插件市场','浏览全部插件与服务',btnCtl('打开插件市场',()=>window.open('/agent','_blank'),'pri')));
  p.appendChild(grp('已安装的自定义工具'));
  const list=el('div'); list.innerHTML='<div class="empty">加载中…</div>'; p.appendChild(list);
  api('/plugins/catalog').then(d=>{
    list.innerHTML='';
    const ut=d.user_tools||[];
    if(!ut.length){ list.innerHTML='<div class="empty">暂无自定义工具（user_tools/ 目录）</div>'; return; }
    ut.forEach(t=>{
      const it=el('div','list-it');
      const g=el('div','grow'); g.appendChild(el('div',null,t.name||t)); g.appendChild(el('div','d2',(t.desc||'')+''));
      it.appendChild(g); it.appendChild(el('span','tagc','已启用'));
      list.appendChild(it);
    });
  }).catch(()=>{ list.innerHTML='<div class="empty">服务未连接</div>'; });
},

mcp(p){
  p.appendChild(el('h1',null,'MCP'));
  p.appendChild(el('div','desc','自我进化 MCP 服务提供的工具，全部走四层闸门保护源码安全。'));
  const list=el('div'); list.innerHTML='<div class="empty">加载中…</div>'; p.appendChild(list);
  api('/mcp/tools').then(d=>{
    list.innerHTML='';
    const ts=d.tools||[];
    if(!ts.length){ list.innerHTML='<div class="empty">暂无 MCP 工具</div>'; return; }
    ts.forEach(t=>{
      const it=el('div','list-it');
      const g=el('div','grow'); g.appendChild(el('div',null,t.name)); g.appendChild(el('div','d2',t.desc||''));
      it.appendChild(g); it.appendChild(el('span','tagc','MCP'));
      list.appendChild(it);
    });
  }).catch(()=>{ list.innerHTML='<div class="empty">服务未连接</div>'; });
},

cue(p){
  p.appendChild(el('h1',null,'CUE'));
  p.appendChild(el('div','desc','代码智能补全与上下文理解引擎（Context Understanding Engine）。'));
  p.appendChild(row('启用 CUE','编码时提供行内智能补全建议',swCtl('cue.enabled')));
  p.appendChild(row('补全延迟','触发补全前的停顿毫秒数',numCtl('cue.delay_ms',100,2000)));
  p.appendChild(row('最大建议长度','单次补全返回的最大字符数',numCtl('cue.max_chars',50,2000)));
},

model(p){
  p.appendChild(el('h1',null,'模型'));
  p.appendChild(grp('模型管理'));
  p.appendChild(el('div','desc','配置 API key 添加更多可用模型，预置模型默认使用稳定版本。'));
  p.appendChild(btnCtl('＋ 添加模型',()=>addModelDialog(),'pri'));

  // 品牌图标配色
  const BRAND={
    Seed:['#1f2329','S'], GLM:['#3d5af1','Z'], DeepSeek:['#4d8dff','D'], Kimi:['#111','K'],
    MiMo:['#6a5cff','M'], MiniMax:['#ff6a3d','M'], Step:['#8f5aff','S'], Qwen:['#0aa37e','Q']
  };
  const MODELS=[
    ['Seed-2.1-Turbo','专属补贴','0.20x'],['Seed-2.1-Pro-0915','','0.80x'],['Seed-Evolving','','0.80x'],
    ['Seed-Code','专属补贴','0.06x'],['GLM-5.3-Flash','','0.08x'],['GLM-5.3-FlashX','','0.31x'],
    ['GLM-5.3','会员限时优惠','0.78x'],['GLM-5.2','','0.78x'],['DeepSeek-V4.1-Flash','会员限时优惠','0.06x'],
    ['DeepSeek-V4-Flash','闲时折扣','0.20x'],['DeepSeek-V4-Pro','闲时折扣','0.72x'],
    ['Kimi-K3.1-Pro','',''],['Kimi-K3','',''],['Kimi-K2.8-Preview','',''],
    ['MiMo-V2.6-Flash','',''],['MiMo-V2.6-Pro','',''],['MiniMax-M3.1-Flash','',''],['MiniMax-M3','',''],
    ['Step-5-Preview','',''],['Qwen3.8-Flash','',''],['Qwen3.8-Max','',''],['Qwen3.7-Plus','','']
  ];
  function brandOf(name){ const k=Object.keys(BRAND).find(b=>name.indexOf(b)===0); return BRAND[k]||['#555','?']; }

  // 内置模型组
  const g1=el('div','mgrp');
  const h1=el('div','mgrp-h');
  h1.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M6 9l6 6 6-6"/></svg><span>内置</span>';
  h1.onclick=()=>g1.classList.toggle('closed');
  g1.appendChild(h1);
  const r1=el('div','mrows');
  const t1=el('table','mtbl');
  t1.innerHTML='<tr><th style="width:55%">模型</th><th>服务商</th><th style="width:70px">操作</th></tr>';
  MODELS.forEach(m=>{
    const b=brandOf(m[0]);
    const tr=el('tr');
    tr.innerHTML='<td><span class="mrow"><span class="mico" style="background:'+b[0]+'">'+b[1]+'</span>'+m[0]
      +(m[1]?'<span class="mtag">'+m[1]+'</span>':'')+(m[2]?'<span class="mult">'+m[2]+'</span>':'')+'</span></td>'
      +'<td>TRAE</td><td style="color:var(--dim)">-</td>';
    t1.appendChild(tr);
  });
  r1.appendChild(t1); g1.appendChild(r1); p.appendChild(g1);

  // 自定义模型组
  const g2=el('div','mgrp');
  const h2=el('div','mgrp-h');
  h2.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M6 9l6 6 6-6"/></svg><span>自定义</span>';
  h2.onclick=()=>g2.classList.toggle('closed');
  g2.appendChild(h2);
  const r2=el('div','mrows'); g2.appendChild(r2); p.appendChild(g2);

  function renderCustom(){
    const list=getCfg('models.custom',[]);
    r2.innerHTML='';
    const t=el('table','mtbl');
    t.innerHTML='<tr><th style="width:55%">模型</th><th>服务商</th><th style="width:70px">操作</th></tr>';
    if(!list.length){
      const tr=el('tr'); const td=el('td'); td.colSpan=3; td.style.color='var(--dim)';
      td.innerHTML='暂无自定义模型，你可以<span class="lnk">在这里</span>添加自定义模型';
      td.querySelector('.lnk').onclick=()=>addModelDialog();
      tr.appendChild(td); t.appendChild(tr);
    } else {
      list.forEach((m,i)=>{
        const tr=el('tr');
        tr.innerHTML='<td><span class="mrow"><span class="mico" style="background:#8f5aff">自</span>'+m.name+'</span></td>'
          +'<td>'+(m.provider||'自定义')+'</td><td><span class="lnk">删除</span></td>';
        tr.querySelector('.lnk').onclick=()=>{
          const arr=getCfg('models.custom',[]); arr.splice(i,1); setCfg('models.custom',arr);
          setTimeout(renderCustom,200);
        };
        t.appendChild(tr);
      });
    }
    r2.appendChild(t);
  }
  renderCustom();

  // ===== 本地开源模型（FreeToken 引擎）分组 =====
  const g3=el('div','mgrp');
  const h3=el('div','mgrp-h');
  h3.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M6 9l6 6 6-6"/></svg><span>本地开源模型（FreeToken）</span>';
  h3.onclick=()=>g3.classList.toggle('closed');
  g3.appendChild(h3);
  const r3=el('div','mrows'); g3.appendChild(r3); p.appendChild(g3);

  // 拉取 FreeToken 状态 + 模型目录，渲染一键部署表
  function renderFreeToken(){
    r3.innerHTML='<div class="empty">加载中…</div>';
    fetch('/freetoken/status').then(r=>r.json()).then(d=>{
      if(d.error){ r3.innerHTML='<div class="empty">'+d.error+'</div>'; return; }
      const t=el('table','mtbl');
      t.innerHTML='<tr><th style="width:38%">模型</th><th>参数量</th><th>建议显存</th><th style="width:130px">操作</th></tr>';
      (d.catalog||[]).forEach(m=>{
        const tr=el('tr');
        const isCur=d.running&&d.model===m.id;
        const btnTxt=isCur?'运行中':(d.installed?'一键部署':'先装引擎');
        tr.innerHTML='<td><span class="mrow"><span class="mico" style="background:#0aa37e">开</span>'
          +m.name+(isCur?'<span class="mtag">当前</span>':'')+'</span>'
          +'<div style="font-size:11px;color:var(--dim);margin-top:2px">'+m.desc+' · 约 '+m.size_gb+'GB</div></td>'
          +'<td>'+m.params+'</td><td>'+m.vram_gb+'GB+</td>'
          +'<td><button class="btn'+(isCur?'':' pri')+'" style="padding:4px 12px;font-size:12px">'+btnTxt+'</button></td>';
        const btn=tr.querySelector('button');
        if(isCur) btn.disabled=true;
        btn.onclick=()=>{
          if(!d.installed){
            // 先装 FreeToken 引擎，装完自动部署
            btn.disabled=true; btn.textContent='安装引擎…';
            fetch('/freetoken/action',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({action:'install'})}).then(r=>r.json()).then(x=>{
                toast(x.result||x.error||'安装完成');
                if(x.ok){ btn.textContent='部署中…';
                  return fetch('/freetoken/action',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({action:'deploy',model:m.id})}).then(r=>r.json()).then(y=>{
                      toast(y.result||y.error||'部署已启动'); setTimeout(renderFreeToken,2000);
                    });
                }
                btn.disabled=false; btn.textContent='一键部署';
              }).catch(e=>{toast('安装失败: '+e); btn.disabled=false; btn.textContent='一键部署';});
          } else {
            btn.disabled=true; btn.textContent='部署中…';
            fetch('/freetoken/action',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({action:'deploy',model:m.id})}).then(r=>r.json()).then(y=>{
                toast(y.result||y.error||'部署已启动'); setTimeout(renderFreeToken,2000);
              }).catch(e=>{toast('部署失败: '+e); btn.disabled=false; btn.textContent='一键部署';});
          }
        };
        t.appendChild(tr);
      });
      r3.innerHTML='';
      // 状态条：引擎安装/运行状态 + 停止按钮
      const bar=el('div'); bar.style.cssText='display:flex;align-items:center;gap:10px;padding:10px 14px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--line)';
      bar.innerHTML='<span>引擎: '+(d.installed?'<span style="color:#59d98a">已安装</span>':'<span style="color:var(--orange)">未安装</span>')
        +' · 服务: '+(d.running?'<span style="color:#59d98a">运行中 :'+d.port+'</span>':'<span style="color:var(--dim)">未运行</span>')+'</span>';
      if(d.running){
        const stopB=el('button','btn'); stopB.textContent='停止服务'; stopB.style.cssText='padding:3px 10px;font-size:11.5px';
        stopB.onclick=()=>{ fetch('/freetoken/action',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({action:'stop'})}).then(r=>r.json()).then(x=>{toast(x.result||'已停止'); setTimeout(renderFreeToken,800);}); };
        bar.appendChild(stopB);
      }
      r3.appendChild(bar);
      r3.appendChild(t);
      // 提示：部署后到「自定义」添加模型接入
      if(d.running&&d.api_base){
        const tip=el('div'); tip.style.cssText='padding:10px 14px;font-size:12px;color:var(--muted)';
        tip.innerHTML='接入方式：点上方「＋ 添加模型」，接口地址填 <span class="mono" style="color:var(--accent)">'+d.api_base+'</span>，模型名填 <span class="mono" style="color:var(--accent)">'+d.model+'</span>，API Key 任意。';
        r3.appendChild(tip);
      }
    }).catch(e=>{ r3.innerHTML='<div class="empty">FreeToken 状态获取失败：'+e+'</div>'; });
  }
  renderFreeToken();

  function addModelDialog(){
    const ov=el('div'); ov.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:90;display:flex;align-items:center;justify-content:center';
    const dlg=el('div'); dlg.style.cssText='width:420px;background:#22232a;border:1px solid var(--line2);border-radius:14px;padding:22px';
    dlg.innerHTML='<div style="font-size:15px;font-weight:600;color:var(--txt2);margin-bottom:16px">添加自定义模型</div>';
    const mk=(lb,ph)=>{
      const w=el('div','fld'); w.appendChild(el('div','lb',lb));
      const i=document.createElement('input'); i.type='text'; i.placeholder=ph||'';
      w.appendChild(i); dlg.appendChild(w); return i;
    };
    const iName=mk('模型名称','例如：Qwen3.8-Max');
    const iProv=mk('服务商','例如：阿里云 / 自建 vLLM');
    const iKey=mk('API Key','sk-…');
    const iBase=mk('接口地址（可选）','https://…/v1');
    const ops=el('div'); ops.style.cssText='display:flex;justify-content:flex-end;gap:10px;margin-top:6px';
    const cancel=btnCtl('取消',()=>ov.remove());
    const ok=btnCtl('添加',()=>{
      if(!iName.value.trim()) return toast('请填写模型名称');
      const arr=getCfg('models.custom',[]);
      arr.push({name:iName.value.trim(),provider:iProv.value.trim()||'自定义',api_key:iKey.value.trim(),base_url:iBase.value.trim()});
      setCfg('models.custom',arr); ov.remove(); setTimeout(renderCustom,200); toast('已添加模型');
    },'pri');
    ops.appendChild(cancel); ops.appendChild(ok); dlg.appendChild(ops);
    ov.appendChild(dlg);
    ov.onclick=(e)=>{ if(e.target===ov) ov.remove(); };
    document.body.appendChild(ov);
  }

  p.appendChild(grp('本地推理模型'));
  p.appendChild(el('div','desc','本机 GGUF 推理参数，修改后需重启 AI 服务生效。'));
  p.appendChild(row('模型文件','GGUF 模型路径',textCtl('server.model_path','320px')));
  p.appendChild(row('上下文窗口','n_ctx，窗口越大占用内存越多',numCtl('server.n_ctx',2048,32768)));
  p.appendChild(row('单次最大生成','max_generate_tokens',numCtl('server.max_generate_tokens',64,8192)));
  p.appendChild(row('GPU 层数','-2 自动按显存计算，0 纯 CPU',numCtl('server.n_gpu_layers',-2,200)));
  p.appendChild(row('CPU 线程数','0 自动（按核心数）',numCtl('server.n_threads',0,64)));
  const b=btnCtl('重启 AI 服务',()=>toast('请使用启动工具 restart 子命令重启服务'));
  p.appendChild(row('应用更改','保存配置后需重启服务加载新参数',b));
},

dialog(p){
  p.appendChild(el('h1',null,'对话流'));
  p.appendChild(el('div','desc','对话体验与朗读设置。'));
  p.appendChild(row('AI 名称','对话中显示的助理名称',textCtl('ai_name','200px')));
  p.appendChild(row('朗读音色','TTS 语音',selCtl('tts.voice',[['zh-CN-XiaoxiaoNeural','晓晓（女声）'],['zh-CN-YunxiNeural','云希（男声）'],['zh-CN-XiaoyiNeural','晓伊（女声）']])));
  p.appendChild(row('离线语音回退','edge-tts 失败时回退 Windows 离线语音',swCtl('tts.fallback_sapi')));
  p.appendChild(row('默认朗读','新会话默认开启朗读（默认关闭）',swCtl('tts.auto_speak')));
  p.appendChild(row('思考过程展示','按时间序交错渲染思考段落与工具调用统计',swCtl('dialog.show_thinking')));
},

browser(p){
  p.appendChild(el('h1',null,'浏览器'));
  p.appendChild(el('div','desc','功能页面与链接的打开方式。'));
  p.appendChild(row('功能页面打开方式','大脑右键菜单功能页面的打开位置',selCtl('ui.page_open_mode',[['embedded','内嵌窗口'],['browser','独立页面（浏览器）']])));
  p.appendChild(row('页面打开白名单','仅允许预设合法路径打开，防止任意 URL 注入',el('span','badge g','已启用')));
},

computer(p){
  p.appendChild(el('h1',null,'电脑控制'));
  p.appendChild(el('div','desc','外设与本地设备扫描控制。'));
  p.appendChild(row('启用设备扫描','允许 AI 扫描本地设备（摄像头/串口等）',swCtl('devices.enabled')));
  p.appendChild(row('扫描超时','单次设备扫描超时时间（秒）',numCtl('devices.scan_timeout',1,30)));
},

index(p){
  p.appendChild(el('h1',null,'索引与文档'));
  p.appendChild(el('div','desc','项目文档中心与知识索引。'));
  p.appendChild(row('文档中心首页','全部项目文档门户',btnCtl('打开',()=>window.open('http://127.0.0.1:8001/','_blank'),'pri')));
  p.appendChild(row('wasily 项目文档','本项目文档',btnCtl('打开',()=>window.open('http://127.0.0.1:8001/docs?project=wasily','_blank'))));
  p.appendChild(row('代码索引','工作区符号索引，加速 AI 检索',el('span','badge g','自动索引中')));
},

skills(p){
  p.appendChild(el('h1',null,'技能与命令'));
  p.appendChild(el('div','desc','技能存放于 skills/ 目录，对话中命中触发词即自动注入提示词。也可以绑定到自定义 Agent 让它始终生效。'));
  // 全局默认绑定（IDE 面板默认用的 Agent 也能用）
  p.appendChild(row('默认绑定到 Agent', '在此勾选的技能会在每次对话时始终注入提示词（不管触发词是否命中）', el('span','tagc','读取下方设置')));
  // 创建按钮
  const topCtl=el('div'); topCtl.style.cssText='display:flex;gap:8px;margin:8px 0 12px';
  topCtl.appendChild(btnCtl('＋ 创建新技能',()=>skillEditDialog(null), 'pri'));
  topCtl.appendChild(btnCtl('🔄 重新扫描',async ()=>{
    try{ await fetch('/agent/skill/reload',{method:'POST'}); }catch(e){}
    load(); toast('已刷新');
  }));
  p.appendChild(topCtl);
  const list=el('div'); p.appendChild(list);
  function load(){
    api('/agent/skills').then(d=>{
      list.innerHTML='';
      const ss=d.skills||[];
      if(!ss.length){ list.innerHTML='<div class="empty">暂无技能</div>'; return; }
      ss.forEach(s=>{
        const it=el('div','list-it');
        const g=el('div','grow');
        g.appendChild(el('div',null,'⚡ '+s.name));
        g.appendChild(el('div','d2',(s.description||'')+'　触发词：'+(s.triggers||[]).join(' / ')));
        it.appendChild(g);
        // 操作按钮
        const acts=el('div'); acts.style.cssText='display:flex;gap:6px;align-items:center';
        const editBtn=el('span','lnk','编辑');
        editBtn.onclick=()=>skillEditDialog(s);
        acts.appendChild(editBtn);
        const delBtn=el('span','lnk','删除');
        delBtn.style.color='#ee6b81';
        delBtn.onclick=async ()=>{
          if(!confirm('确认删除技能「'+s.name+'」？此操作不可恢复。')) return;
          try{
            const r=await (await fetch('/agent/skill/delete',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({file:s.file,name:s.name})})).json();
            if(r.ok){ toast('已删除'); load(); } else toast(r.error||'删除失败');
          }catch(e){ toast('服务未连接'); }
        };
        acts.appendChild(delBtn);
        it.appendChild(acts);
        list.appendChild(it);
      });
    }).catch(()=>{ list.innerHTML='<div class="empty">服务未连接</div>'; });
  }
  load();
},

memory(p){
  p.appendChild(el('h1',null,'规则与记忆'));
  p.appendChild(el('div','desc','多层记忆金字塔：L1 会话摘要 → L2 长期事实 → L3 核心认知。'));
  const kv=el('div','kv'); p.appendChild(kv);
  const ctl=el('div','ctl');
  ctl.appendChild(btnCtl('立即压缩',()=>post('/memory/compress').then(()=>{ toast('压缩完成'); load(); }).catch(()=>toast('服务未连接')),'pri'));
  ctl.appendChild(btnCtl('刷新',()=>load()));
  p.appendChild(row('记忆压缩','手动触发待压缩队列归并',ctl));
  const list=el('div'); p.appendChild(list);
  function load(){
    api('/memory/layers').then(d=>{
      kv.innerHTML='';
      const st=d.stats||{};
      Object.keys(st).forEach(k=>{ const b=el('div','box'); b.appendChild(el('div','k',k)); b.appendChild(el('div','v',String(st[k]))); kv.appendChild(b); });
      list.innerHTML='';
      const ents=d.entries||{};
      Object.keys(ents).forEach(layer=>{
        const items=ents[layer]||[];
        if(!items.length) return;
        list.appendChild(grp(layer));
        items.slice(0,20).forEach(e=>{
          const it=el('div','list-it');
          const g=el('div','grow'); g.appendChild(el('div','d2',(typeof e==='string'?e:(e.text||JSON.stringify(e))).slice(0,140)));
          it.appendChild(g); list.appendChild(it);
        });
      });
      if(!list.children.length) list.innerHTML='<div class="empty">暂无记忆条目</div>';
    }).catch(()=>{ kv.innerHTML='<div class="empty">服务未连接</div>'; });
  }
  load();
},

hooks(p){
  p.appendChild(el('h1',null,'Hooks'));
  p.appendChild(el('div','desc','在 AI 工作流程的关键节点注入自定义脚本。'));
  p.appendChild(row('对话前 Hook','每轮对话发送前执行',textCtl('hooks.pre_chat','260px')));
  p.appendChild(row('对话后 Hook','每轮对话完成后执行',textCtl('hooks.post_chat','260px')));
  p.appendChild(row('工具调用前 Hook','工具调用前拦截，可返回拒绝',textCtl('hooks.pre_tool','260px')));
  p.appendChild(el('div','empty','留空表示不启用。Hook 为本地脚本路径，输出 JSON 控制行为。'));
},

beta(p){
  p.appendChild(el('h1',null,'Beta'));
  p.appendChild(el('div','desc','实验性功能，可能不稳定。'));
  p.appendChild(row('多 AI 群聊','多个 AI 同时参与讨论，用户指令优先级最高',swCtl('beta.group_chat')));
  p.appendChild(row('SOLO 自主模式','AI 自主拆解并执行完整开发计划',swCtl('beta.solo_mode')));
  p.appendChild(row('卷积上下文压缩','四层池化压缩，超长任务永不超限（当前已内置启用）',el('span','badge g','已内置')));
  p.appendChild(row('LoRA 微调','基于历史对话自训练模型适配器',swCtl('beta.lora')));
},

about(p){
  p.appendChild(el('h1',null,'关于TRAE'));
  p.appendChild(el('div','desc','wasily IDE — 本地优先的 AI 编程工作台。'));
  const kv=el('div','kv');
  [['版本','wasily IDE 0.4.0'],['AI 服务','127.0.0.1:8000'],['模型','qwen2.5-7b-instruct'],['文档中心','127.0.0.1:8001']].forEach(it=>{
    const b=el('div','box'); b.appendChild(el('div','k',it[0]));
    const v=el('div','v',it[1]); v.style.fontSize='13px'; b.appendChild(v); kv.appendChild(b);
  });
  p.appendChild(kv);
  const ctl=el('div','ctl');
  ctl.appendChild(btnCtl('检查更新',()=>{
    api('/health').then(()=>toast('当前已是最新版本')).catch(()=>toast('AI 服务未连接'));
  },'pri'));
  ctl.appendChild(btnCtl('帮助文档',()=>window.open('http://127.0.0.1:8001/','_blank')));
  p.appendChild(row('版本与更新','',ctl));
  p.appendChild(el('div','empty','数据不出机 · 全部本地运行'));
},
};

/* ---------- 渲染框架 ---------- */
function renderNav(){
  const nav=$('#nav'); nav.innerHTML='';
  // 用户卡片
  const me=el('div','me');
  me.innerHTML='<span class="av">'+icon(IC.user)+'</span><span><div class="nm" id="meNm">—</div><div class="plan" id="mePlan">免费</div></span>';
  me.style.cursor='pointer'; me.onclick=()=>{ location.hash='account'; };
  nav.appendChild(me);
  api('/account/info').then(d=>{
    $('#meNm').textContent=d.name||'本地用户'; $('#mePlan').textContent=d.plan||'免费';
  }).catch(()=>{ $('#meNm').textContent='本地用户'; });
  // Ctrl+F 搜索
  const sb=el('div','sbox'); const si=document.createElement('input');
  si.placeholder='Ctrl+F 搜索'; si.id='navSearch';
  si.oninput=()=>{
    const kw=si.value.trim();
    document.querySelectorAll('.nitem').forEach(n=>{
      n.style.display = (!kw || n.textContent.includes(kw)) ? '' : 'none';
    });
    document.querySelectorAll('#nav .nsec, #nav .nsep').forEach(s=>{
      s.style.display = kw ? 'none' : '';
    });
  };
  sb.appendChild(si); nav.appendChild(sb);
  NAV.forEach(it=>{
    if(it.sec){ if(it.sec!=='设置'&&it.sec!=='账户') nav.appendChild(el('div','nsep')); nav.appendChild(el('div','nsec',it.sec)); return; }
    const n=el('div','nitem'); n.dataset.id=it.id;
    n.innerHTML=icon(it.icon)+'<span>'+it.name+'</span>';
    if(it.ext){
      const x=el('span','ext');
      x.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 5h5v5M19 5l-8 8M9 5H7a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2"/></svg>';
      x.title='在插件市场中打开';
      x.onclick=(e)=>{ e.stopPropagation(); window.open(it.ext,'_blank'); };
      n.appendChild(x);
    }
    n.onclick=()=>{ location.hash=it.id; };
    nav.appendChild(n);
  });
}

function show(id){
  if(!PANES[id]) id='general';
  document.querySelectorAll('.nitem').forEach(n=>n.classList.toggle('on', n.dataset.id===id));
  const c=$('#content'); c.innerHTML='';
  const p=el('section','pane on'); c.appendChild(p);
  PANES[id](p);
  c.scrollTop=0;
  applyI18n(c);
  applyI18n($('#nav'));
}

window.addEventListener('hashchange',()=>show(location.hash.slice(1)));
document.addEventListener('keydown',e=>{
  if(e.ctrlKey && (e.key==='f'||e.key==='F')){ e.preventDefault(); const s=$('#navSearch'); if(s) s.focus(); }
});

$('#importFile').addEventListener('change',e=>{
  const f=e.target.files[0]; if(!f) return;
  const rd=new FileReader();
  rd.onload=()=>{
    try{
      const data=JSON.parse(rd.result);
      const flat=[];
      (function walk(o,pre){ Object.keys(o||{}).forEach(k=>{
        const v=o[k], p=pre?pre+'.'+k:k;
        if(v&&typeof v==='object'&&!Array.isArray(v)) walk(v,p); else flat.push([p,v]);
      });})(data,'');
      Promise.all(flat.map(kv=>post('/config/set',{path:kv[0],value:kv[1]})))
        .then(()=>api('/config/get')).then(d=>{ CFG=(d&&d.value&&typeof d.value==='object')?d.value:(d||{}); toast('已导入 '+flat.length+' 项配置'); show(location.hash.slice(1)); })
        .catch(()=>toast('部分配置导入失败'));
    }catch(err){ toast('文件格式错误'); }
    e.target.value='';
  };
  rd.readAsText(f);
});

/* ---------- Skill 创建/编辑弹窗 ---------- */
function skillEditDialog(existing){
  // existing = null 表示创建；否则是一个 skill 对象（{name, triggers, description, file}）
  const dlg = document.createElement('div');
  dlg.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center';
  const box = document.createElement('div');
  box.style.cssText = 'background:var(--bg);border-radius:14px;width:640px;max-width:92vw;max-height:86vh;overflow-y:auto;box-shadow:0 16px 48px rgba(0,0,0,.3)';
  dlg.appendChild(box);
  document.body.appendChild(dlg);
  dlg.onclick = (e)=>{ if(e.target===dlg) dlg.remove(); };

  // 头部
  const hd = el('div'); hd.style.cssText='padding:18px 20px;border-bottom:1px solid var(--bd);display:flex;align-items:center;justify-content:space-between';
  hd.appendChild(el('div', null, existing ? '⚡ 编辑技能：'+existing.name : '⚡ 创建新技能'));
  const closeBtn = el('span','lnk','✕'); closeBtn.onclick = ()=>dlg.remove();
  hd.appendChild(closeBtn);
  box.appendChild(hd);

  // 表单
  const body = el('div'); body.style.cssText='padding:16px 20px';
  const nameL = el('label'); nameL.style.cssText='display:block;margin-bottom:12px';
  nameL.innerHTML = '<div style="font-size:12px;color:var(--txt2);margin-bottom:4px">技能名称</div>';
  const nameInp = document.createElement('input');
  nameInp.style.cssText = 'width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--bd);background:var(--bg2);color:var(--txt)';
  nameInp.value = existing ? existing.name : '';
  nameL.appendChild(nameInp); body.appendChild(nameL);

  const fileL = el('label'); fileL.style.cssText='display:block;margin-bottom:12px';
  fileL.innerHTML = '<div style="font-size:12px;color:var(--txt2);margin-bottom:4px">文件名（.md，留空自动推导）</div>';
  const fileInp = document.createElement('input');
  fileInp.style.cssText = 'width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--bd);background:var(--bg2);color:var(--txt)';
  fileInp.value = existing ? existing.file : '';
  fileL.appendChild(fileInp); body.appendChild(fileL);

  const descL = el('label'); descL.style.cssText='display:block;margin-bottom:12px';
  descL.innerHTML = '<div style="font-size:12px;color:var(--txt2);margin-bottom:4px">一句话描述</div>';
  const descInp = document.createElement('input');
  descInp.style.cssText = 'width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--bd);background:var(--bg2);color:var(--txt)';
  descInp.value = existing ? (existing.description||'') : '';
  descL.appendChild(descInp); body.appendChild(descL);

  const trigL = el('label'); trigL.style.cssText='display:block;margin-bottom:12px';
  trigL.innerHTML = '<div style="font-size:12px;color:var(--txt2);margin-bottom:4px">触发词（逗号分隔，用户输入含这些词时自动注入）</div>';
  const trigInp = document.createElement('input');
  trigInp.style.cssText = 'width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--bd);background:var(--bg2);color:var(--txt)';
  trigInp.value = existing ? (existing.triggers||[]).join(', ') : '';
  trigL.appendChild(trigInp); body.appendChild(trigL);

  const promptL = el('label'); promptL.style.cssText='display:block;margin-bottom:12px';
  promptL.innerHTML = '<div style="font-size:12px;color:var(--txt2);margin-bottom:4px">技能正文（注入给模型的提示词）</div>';
  const promptInp = document.createElement('textarea');
  promptInp.style.cssText = 'width:100%;min-height:180px;padding:8px 10px;border-radius:8px;border:1px solid var(--bd);background:var(--bg2);color:var(--txt);font-family:monospace;font-size:13px;resize:vertical';
  // 如果是编辑模式，从服务器读取实际 prompt 内容
  if (existing && existing.file) {
    fetch('/agent/skill/content?file=' + encodeURIComponent(existing.file))
      .then(r=>r.json()).then(d=>{ promptInp.value = d.content || ''; }).catch(()=>{});
  }
  promptL.appendChild(promptInp); body.appendChild(promptL);

  box.appendChild(body);

  // 底部按钮
  const ft = el('div'); ft.style.cssText='padding:14px 20px;border-top:1px solid var(--bd);display:flex;gap:10px;justify-content:flex-end';
  const cancelBtn = btnCtl('取消', ()=>dlg.remove());
  const saveBtn = btnCtl(existing ? '保存修改' : '创建技能', async ()=>{
    const name = nameInp.value.trim();
    if(!name){ toast('请填写技能名称'); return; }
    const triggers = trigInp.value.split(/[,，]/).map(s=>s.trim()).filter(Boolean);
    const prompt = promptInp.value.trim();
    if(!prompt){ toast('请填写技能正文'); return; }
    const payload = {
      name,
      file: fileInp.value.trim(),
      description: descInp.value.trim(),
      triggers,
      prompt,
    };
    saveBtn.disabled = true;
    try{
      const r = await (await fetch('/agent/skill/save',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)})).json();
      if(r.ok){ toast('已保存'); dlg.remove(); }
      else toast(r.error||'保存失败');
    }catch(e){ toast('服务未连接'); }
    finally{ saveBtn.disabled = false; }
  }, 'pri');
  ft.appendChild(cancelBtn); ft.appendChild(saveBtn);
  box.appendChild(ft);
}

/* ---------- 启动 ---------- */
renderNav();
api('/config/get').then(d=>{ CFG=(d&&d.value&&typeof d.value==='object')?d.value:(d||{}); }).catch(()=>{}).finally(()=>{
  applyTheme(getCfg('ui.theme','dark'));
  // IDE 标题栏可指定初始 tab
  let init=location.hash.slice(1);
  try{ const t=localStorage.getItem('wasily.settings.tab'); if(t){ localStorage.removeItem('wasily.settings.tab'); init=t; } }catch(e){}
  show(init||'general');
});
