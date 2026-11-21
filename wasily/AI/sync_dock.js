/* 跨端同步层：Agent 对话页与 IDE 面板共用。
 *
 * 数据源只有一条会话 SSE（/v1/sync/stream），事件自带全局递增 id：
 * - 先拿服务端快照的序号再订阅，避免把历史重复渲染两遍；
 * - 断线由 EventSource 带 Last-Event-ID 自动续传；缓冲淘汰下发 reset 走快照对齐；
 * - 消息按 msg_id 幂等，重试、重新生成、重复上报都不会出现重复气泡；
 * - 本页自己产生的事件（source 相同）直接跳过，渲染仍由原有链路负责。
 */
(function () {
  if (window.__WASILY_SYNC_READY__) return;
  window.__WASILY_SYNC_READY__ = 1;

  var BASE = window.__WASILY_SYNC_BASE__ || location.origin;
  var PAGE = location.pathname.indexOf('/panel_ui') === 0 ? 'ide' : 'agent';
  var GROUP = 'default';
  var SID = readSid();
  var lastSeq = 0;
  var es = null;
  var seen = Object.create(null);      // 已渲染的 msg_id
  var midNodes = Object.create(null);  // msg_id -> DOM 节点（仅 Agent 页）
  var streamEls = Object.create(null); // msg_id -> 远端流式临时节点
  var generating = false;
  var ready = false;
  var lastVerify = 0;

  function readSid() {
    try {
      if (PAGE === 'ide') return localStorage.getItem('wasily_ide_sid') || '';
      return localStorage.getItem('wasily_chat_sid') || '';
    } catch (e) { return ''; }
  }

  function setSid(sid) {
    sid = String(sid || '');
    if (!sid || sid === SID) return;
    SID = sid;
    try { localStorage.setItem(PAGE === 'ide' ? 'wasily_ide_sid' : 'wasily_chat_sid', sid); } catch (e) { }
    // 换会话等于换事件流：清空本地记录并从头对齐，不能沿用上一个会话的序号
    seen = Object.create(null); midNodes = Object.create(null);
    streamEls = Object.create(null); lastSeq = 0;
    reconnect(true);
  }

  /* ---------- 页面消息桥 ---------- */
  window.addEventListener('message', function (ev) {
    var m = ev.data;
    if (!m || !m.type) return;
    if (m.type === 'generating') generating = !!m.value;
    if (m.sid) setSid(m.sid);
  });

  function chatHost() {
    return document.getElementById('chat');
  }

  function nodeOf(el) {
    if (!el) return null;
    return el.closest ? el.closest('.msg') : null;
  }

  function alreadyHas(role, content) {
    var host = chatHost();
    if (!host || !content) return false;
    var cls = role === 'user' ? '.msg.user' : '.msg.ai';
    var list = host.querySelectorAll(cls);
    for (var i = list.length - 1; i >= 0; i--) {
      var text = (list[i].textContent || '').trim();
      if (text === String(content).trim()) return true;
    }
    return false;
  }

  /* ---------- 渲染：Agent 对话页 ---------- */
  function agentUser(m) {
    if (!window.addMsg || alreadyHas('user', m.content)) return;
    var node = nodeOf(window.addMsg('user', m.content, { persist: false, remote: true }));
    if (node && m.mid) { node.setAttribute('data-mid', m.mid); midNodes[m.mid] = node; }
  }

  function agentAssistant(m) {
    if (!window.addMsg) return;
    if (m.mid && midNodes[m.mid]) return;
    if (alreadyHas('assistant', m.content)) { if (m.mid) seen[m.mid] = 1; dropStream(m.mid); return; }
    dropStream(m.mid);   // 定稿接管临时流式气泡
    var steps = m.steps || [];
    var toolInfo = null;
    if (steps.length) {
      toolInfo = { name: 'tool', steps: steps,
        result: steps.map(function (s) {
          return (s.think ? '[思考] ' + s.think
            : '[工具] ' + s.tool + '\n结果: ' + (s.result || ''));
        }).join('\n\n') };
    }
    var node = nodeOf(window.addMsg('ai', m.content || '', {
      persist: false, remote: true, toolInfo: toolInfo,
      ms: m.elapsed ? Math.round(Number(m.elapsed) * 1000) : 0
    }));
    if (node && m.mid) { node.setAttribute('data-mid', m.mid); midNodes[m.mid] = node; }
    if (m.mid) seen[m.mid] = 1;
  }

  function agentDelta(m) {
    if (!chatHost() || !m.mid) return;
    if (midNodes[m.mid]) return;     // 该消息已定稿
    var el = streamEls[m.mid];
    if (!el) {
      el = document.createElement('div');
      el.className = 'msg ai wSyncStream';
      el.setAttribute('data-mid', m.mid);
      var b = document.createElement('div');
      b.className = 'bubble';
      el.appendChild(b);
      chatHost().appendChild(el);
      streamEls[m.mid] = el;
    }
    var bubble = el.firstChild;
    bubble.textContent = (bubble.textContent || '') + (m.text || '');
    var host = chatHost();
    if (host.scrollTop + host.clientHeight >= host.scrollHeight - 60) host.scrollTop = host.scrollHeight;
  }

  function dropStream(mid) {
    var el = mid && streamEls[mid];
    if (el && el.parentNode) el.parentNode.removeChild(el);
    if (mid) delete streamEls[mid];
  }

  /* ---------- 渲染：IDE 面板 ---------- */
  function idePost(obj) {
    try { window.postMessage(obj, '*'); } catch (e) { }
  }

  function ideUser(m) {
    if (generating) return;          // 本端正在生成：不能让对端气泡插进来打乱流
    idePost({ type: 'append', role: 'user', sid: SID, content: m.content || '' });
  }

  function ideAssistant(m) {
    if (m.mid && midNodes[m.mid]) return;
    if (generating) {
      // 本端生成中：对端结果先缓存，结束后由对账补齐
      return;
    }
    idePost({ type: 'append', role: 'assistant', sid: SID, content: m.content || '',
      steps: m.steps || [], ms: m.elapsed ? Math.round(Number(m.elapsed) * 1000) : 0,
      agentRun: m.agent_run || null });
    if (m.mid) { midNodes[m.mid] = 1; seen[m.mid] = 1; }
  }

  function ideDelta(m) {
    if (generating) return;
    idePost({ type: 'delta', sid: SID, text: m.text || '' });
  }

  function ideRebuild(messages) {
    idePost({ type: 'renderMessages', sid: SID, messages: messages || [] });
  }

  /* ---------- 执行状态提示（Agent 页可见 IDE 侧动作） ---------- */
  function note(kind, title, detail, status) {
    var host = chatHost();
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'wSyncNote';
    el.style.cssText = 'margin:8px 0;padding:8px 10px;border:1px solid #2b2f38;border-radius:8px;'
      + 'background:#15171c;color:#9aa4b2;font:12px/1.5 system-ui,sans-serif;white-space:pre-wrap;';
    el.textContent = (title || kind) + (detail ? '\n' + detail : '')
      + (status ? '\n状态：' + status : '');
    host.appendChild(el);
    host.scrollTop = host.scrollHeight;
  }

  /* ---------- 事件分发 ---------- */
  function apply(ev) {
    if (!ev || !ev.type || !ev.id || ev.id <= lastSeq) return;
    lastSeq = ev.id;
    if (ev.type === 'reset') { verify(true); return; }
    if (ev.session_id && ev.session_id !== SID) return;   // 别的会话，不串台
    var fromSelf = (ev.source === PAGE);
    if (ev.type === 'user') {
      if (!SID) return;                  // 尚未确定会话：宁可不渲染，也不串台
      if (!fromSelf) (PAGE === 'ide' ? ideUser : agentUser)(ev);
      return;
    }
    if (ev.type === 'delta') {
      if (!SID) return;
      if (!fromSelf) (PAGE === 'ide' ? ideDelta : agentDelta)(ev);
      return;
    }
    if (ev.type === 'assistant') {
      if (!SID) return;
      if (!fromSelf) (PAGE === 'ide' ? ideAssistant : agentAssistant)(ev);
      return;
    }
    if (ev.type === 'session_clear') {
      if (PAGE === 'agent') {
        var host = chatHost();
        if (host) host.innerHTML = '';
        midNodes = Object.create(null); seen = Object.create(null);
        try { localStorage.removeItem('wasily_chat_history'); } catch (e) { }
      } else {
        ideRebuild([]);
      }
      return;
    }
    if (ev.type === 'stream_reset') {
      if (PAGE === 'agent') dropStream(ev.msg_id);
      else if (!generating) idePost({ type: 'streamReset', sid: SID });
      return;
    }
    if (ev.type === 'report' && PAGE === 'agent') {
      // 只保留有意义的执行回执；纯心跳类上报不刷屏
      if (ev.title || ev.detail) note(ev.kind, ev.title || 'IDE 操作', ev.detail, ev.status);
      return;
    }
  }

  /* ---------- 连接与对齐 ---------- */
  function connect() {
    if (es) { try { es.close(); } catch (e) { } }
    var url = BASE + '/v1/sync/stream?group=' + encodeURIComponent(GROUP)
      + '&session_id=' + encodeURIComponent(SID || '') + '&after=' + lastSeq;
    es = new EventSource(url);
    es.onopen = function () { setOnline(true); };
    es.onmessage = function (e) {
      try { apply(JSON.parse(e.data)); } catch (err) { }
    };
    es.onerror = function () { setOnline(false); };   // EventSource 自带重连并带 Last-Event-ID
  }

  function reconnect(force) {
    if (force) { lastSeq = 0; }
    fetchBase();
  }

  function fetchBase() {
    var url = BASE + '/v1/sync/snapshot?session_id=' + encodeURIComponent(SID || '');
    fetch(url, { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { connect(); return; }
      lastSeq = Number(d.seq || 0);
      // 首次进入：把服务端已有、而本地没有的消息补上，保证两端历史一致
      if (d.messages && d.messages.length) {
        if (PAGE === 'ide') {
          ideRebuild(d.messages);
          d.messages.forEach(function (m) { if (m.mid) seen[m.mid] = 1; });
        } else {
          d.messages.forEach(function (m) {
            if (!m.mid || seen[m.mid]) return;
            if (m.role === 'user') agentUser(m);
            else if (m.role === 'assistant') agentAssistant(m);
          });
        }
      }
      ready = true;
      connect();
    }).catch(function () { connect(); });
  }

  function verify(force) {
    var url = BASE + '/v1/sync/snapshot?session_id=' + encodeURIComponent(SID || '');
    fetch(url, { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) return;
      if (PAGE === 'ide') {
        if (!generating && typeof d.seq === 'number' && d.seq > lastSeq) {
          lastSeq = d.seq;
          ideRebuild(d.messages || []);
          (d.messages || []).forEach(function (m) { if (m.mid) seen[m.mid] = 1; });
        }
        return;
      }
      // Agent 页增量补差：只添加服务端有、本地没有的消息，绝不删除本端内容
      (d.messages || []).forEach(function (m) {
        if (!m.mid || seen[m.mid]) return;
        if (m.role === 'user') agentUser(m);
        else if (m.role === 'assistant') agentAssistant(m);
      });
      if (typeof d.seq === 'number' && d.seq > lastSeq) lastSeq = d.seq;
    }).catch(function () { });
  }

  /* ---------- 执行状态：命令结束后在对话流内提示一次 ----------
     Trae 把命令执行放在对话时间线里，不弹独立面板、也不为「运行中」重复刷屏；
     这里只在命令收尾时补一条，样式与普通消息一致。 */
  function connectCommands() {
    try {
      var cs = new EventSource(BASE + '/v1/commands/stream?after=0');
      var startedAt = Date.now();
      var noted = Object.create(null);
      cs.onmessage = function (e) {
        var ev = null;
        try { ev = JSON.parse(e.data); } catch (err) { return; }
        if (!ev || ev.type === 'line' || !ev.run) return;
        var run = ev.run;
        if (!run || !run.started_at) return;
        if (run.started_at * 1000 < startedAt) return;   // 重放的旧命令不提示
        if (SID && run.session_id && run.session_id !== SID) return;
        if (PAGE !== 'agent' || run.status === 'running') return;  // 只在收尾时提示
        if (noted[run.id]) return;
        noted[run.id] = 1;
        var label = (run.status === 'failed' || run.status === 'timeout')
          ? '命令失败' : (run.status === 'aborted' ? '命令已中断' : '命令已完成');
        note('command', label + ' · ' + (run.command || ''), '', '');
      };
    } catch (e) { }
  }

  /* ---------- 状态只在内部维护 ----------
     为什么不做悬浮徽标：Trae 的同步是隐性的，界面上不该多出「同步 · 在线」这类提示，
     否则会像第三方插件而不是编辑器自身能力。 */
  var SYNC_ONLINE = false;
  function setOnline(on) { SYNC_ONLINE = !!on; }

  window.WasilySync = {
    state: function () {
      return { page: PAGE, sid: SID, seq: lastSeq, online: SYNC_ONLINE, seen: Object.keys(seen).length };
    },
    verify: function () { return verify(true); },
    setSid: setSid
  };

  // 页面切换会话后 localStorage 会变，用轮询兜住（两页都不会主动通知同步层）
  var lastSid = SID;
  setInterval(function () {
    var now = readSid();
    if (now && now !== lastSid) { lastSid = now; setSid(now); }
    if (ready && Date.now() - lastVerify > 20000) { lastVerify = Date.now(); verify(true); }
  }, 2500);

  fetchBase();
  connectCommands();
})();
