/* 跨页命令同步面板：Agent 窗口与 IDE 面板共用同一份实现。
 *
 * 数据源只有一条 SSE（/v1/commands/stream），事件自带全局递增 id：
 * 先拿到当前序号再订阅，断线由 EventSource 带 Last-Event-ID 自动续传，
 * 因此两个页面看到的运行状态、行号与顺序完全一致。
 */
(function () {
  if (window.__WASILY_CMD_DOCK_READY__) return;
  window.__WASILY_CMD_DOCK_READY__ = 1;

  var BASE = window.__WASILY_CMD_BASE__ || location.origin;
  var PAGE = location.pathname.indexOf('/agent') === 0 ? 'agent' : 'ide';
  var MAX_LINES = 400;
  var STATUS = {
    running: '运行中', exited: '已完成', failed: '失败',
    aborted: '已中断', timeout: '超时'
  };

  var runs = Object.create(null);
  var order = [];
  var lastSeq = 0;
  var es = null;

  /* ---------- 样式 ---------- */
  var css = ''
    + '.wcmd{position:fixed;right:14px;bottom:14px;width:430px;max-width:calc(100vw - 28px);'
    + 'z-index:9998;background:#15171c;color:#d7dbe0;border:1px solid #2b2f38;'
    + 'border-radius:10px;box-shadow:0 12px 32px rgba(0,0,0,.45);font:12px/1.5 system-ui,sans-serif;'
    + 'display:flex;flex-direction:column;overflow:hidden}'
    + '.wcmd.min .wcmdBody{display:none}'
    + '.wcmdHead{display:flex;align-items:center;gap:8px;padding:8px 10px;'
    + 'background:#1b1e24;cursor:pointer;user-select:none}'
    + '.wcmdDot{width:7px;height:7px;border-radius:50%;background:#6b7280}'
    + '.wcmdDot.on{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.16)}'
    + '.wcmdTitle{font-weight:600;font-size:12px;letter-spacing:.2px}'
    + '.wcmdMeta{margin-left:auto;color:#8b93a1;font-size:11px}'
    + '.wcmdBody{max-height:46vh;overflow:auto;padding:6px}'
    + '.wcmdItem{border:1px solid #262a32;border-radius:8px;margin-bottom:6px;overflow:hidden}'
    + '.wcmdRow{display:flex;align-items:center;gap:6px;padding:6px 8px;background:#191c22}'
    + '.wcmdCmd{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'
    + 'font:11px/1.4 ui-monospace,Consolas,monospace;color:#c9d1d9}'
    + '.wcmdBadge{font-size:10px;padding:1px 6px;border-radius:999px;background:#2b2f38;color:#9aa4b2}'
    + '.wcmdBadge.running{background:rgba(34,197,94,.16);color:#4ade80}'
    + '.wcmdBadge.failed,.wcmdBadge.timeout{background:rgba(248,113,113,.16);color:#f87171}'
    + '.wcmdBadge.aborted{background:rgba(251,191,36,.16);color:#fbbf24}'
    + '.wcmdBadge.exited{background:rgba(96,165,250,.16);color:#60a5fa}'
    + '.wcmdStop{border:1px solid #3a3f4a;background:#242832;color:#e5e7eb;border-radius:6px;'
    + 'font-size:10px;padding:2px 7px;cursor:pointer}'
    + '.wcmdStop:hover{background:#3a3f4a}'
    + '.wcmdOut{margin:0;padding:6px 8px;max-height:170px;overflow:auto;background:#101216;'
    + 'font:11px/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-all}'
    + '.wcmdOut .e{color:#f87171}'
    + '.wcmdOut .p{color:#4b5563}'
    + '.wcmdFoot{display:flex;gap:6px;padding:6px;border-top:1px solid #262a32}'
    + '.wcmdFoot input{flex:1;background:#101216;border:1px solid #2b2f38;color:#e5e7eb;'
    + 'border-radius:6px;padding:5px 8px;font:11px ui-monospace,Consolas,monospace;outline:none}'
    + '.wcmdFoot input:focus{border-color:#3b82f6}'
    + '.wcmdFoot button{border:1px solid #2b4c86;background:#1d4ed8;color:#fff;border-radius:6px;'
    + 'padding:5px 10px;font-size:11px;cursor:pointer}'
    + '.wcmdEmpty{color:#6b7280;font-size:11px;padding:10px;text-align:center}';

  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  /* ---------- DOM ---------- */
  var root = document.createElement('div');
  root.className = 'wcmd min';
  root.innerHTML = ''
    + '<div class="wcmdHead">'
    + '  <span class="wcmdDot"></span>'
    + '  <span class="wcmdTitle">跨页命令同步</span>'
    + '  <span class="wcmdMeta">0 条</span>'
    + '</div>'
    + '<div class="wcmdBody"><div class="wcmdList">'
    + '  <div class="wcmdEmpty">暂无命令</div>'
    + '</div></div>'
    + '<div class="wcmdFoot">'
    + '  <input type="text" placeholder="任一页面执行，两页同步显示（Enter）">'
    + '  <button type="button">运行</button>'
    + '</div>';

  function mount() {
    if (!document.body) return;
    document.body.appendChild(root);
  }
  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);

  var headEl = root.querySelector('.wcmdHead');
  var dotEl = root.querySelector('.wcmdDot');
  var metaEl = root.querySelector('.wcmdMeta');
  var listEl = root.querySelector('.wcmdList');
  var inputEl = root.querySelector('.wcmdFoot input');
  var runBtn = root.querySelector('.wcmdFoot button');

  headEl.addEventListener('click', function () {
    root.classList.toggle('min');
  });

  /* ---------- 渲染 ---------- */
  function connectState() {
    var running = order.filter(function (id) { return runs[id].run.status === 'running'; });
    dotEl.classList.toggle('on', running.length > 0);
    metaEl.textContent = order.length + ' 条' + (running.length ? ' · 运行中 ' + running.length : '');
  }

  function ensureItem(run) {
    var rec = runs[run.id];
    if (!rec || !rec.el) {
      var empty = listEl.querySelector('.wcmdEmpty');
      if (empty) empty.remove();
      var el = document.createElement('div');
      el.className = 'wcmdItem';
      el.innerHTML = ''
        + '<div class="wcmdRow">'
        + '  <span class="wcmdBadge"></span>'
        + '  <code class="wcmdCmd"></code>'
        + '  <span class="wcmdBadge src"></span>'
        + '  <span class="wcmdTime" style="color:#6b7280;font-size:10px"></span>'
        + '  <button class="wcmdStop" type="button">中断</button>'
        + '</div>'
        + '<pre class="wcmdOut"></pre>';
      el.querySelector('.wcmdStop').addEventListener('click', function (ev) {
        ev.stopPropagation();
        abort(run.id);
      });
      el.querySelector('.wcmdCmd').title = run.command || '';
      listEl.insertBefore(el, listEl.firstChild);
      if (!rec) { rec = runs[run.id] = { run: run, lines: [], el: null }; }
      rec.el = el;
      rec.out = el.querySelector('.wcmdOut');
      rec.time = el.querySelector('.wcmdTime');
      rec.badge = el.querySelector('.wcmdBadge');
      rec.src = el.querySelector('.src');
      rec.stop = el.querySelector('.wcmdStop');
    }
    var r = rec.run;
    r.status = run.status;
    r.exit_code = run.exit_code;
    r.ended_at = run.ended_at;
    r.line_count = run.line_count;
    r.started_at = run.started_at;
    if (run.command) r.command = run.command;
    if (run.source) r.source = run.source;
    rec.el.querySelector('.wcmdCmd').textContent = r.command || '';
    rec.badge.textContent = STATUS[r.status] || r.status;
    rec.badge.className = 'wcmdBadge ' + r.status;
    rec.src.textContent = r.source === 'ide' ? 'IDE' : (r.source === 'agent' ? 'Agent' : (r.source || ''));
    rec.stop.style.display = r.status === 'running' ? '' : 'none';
    updateTime(rec);
    if (order.indexOf(run.id) < 0) order.unshift(run.id);
    if (order.length > 20) {
      var drop = order.pop();
      var dead = runs[drop];
      if (dead && dead.el) dead.el.remove();
      delete runs[drop];
    }
    connectState();
    return rec;
  }

  function updateTime(rec) {
    if (!rec || !rec.time) return;
    var r = rec.run;
    var end = r.ended_at || (Date.now() / 1000);
    var sec = Math.max(0, end - (r.started_at || end));
    rec.time.textContent = sec < 60 ? sec.toFixed(1) + 's'
      : Math.floor(sec / 60) + 'm' + Math.round(sec % 60) + 's';
  }

  function appendLines(rec, lines) {
    if (!rec || !rec.out || !lines.length) return;
    var near = rec.out.scrollTop + rec.out.clientHeight >= rec.out.scrollHeight - 24;
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i];
      var span = document.createElement('span');
      if (ln.stream === 'stderr') span.className = 'e';
      span.textContent = (ln.stream === 'stderr' ? '! ' : '') + ln.text + '\n';
      rec.out.appendChild(span);
      rec.lines.push(ln);
    }
    while (rec.lines.length > MAX_LINES) {
      rec.lines.shift();
      if (rec.out.firstChild) rec.out.removeChild(rec.out.firstChild);
    }
    if (near) rec.out.scrollTop = rec.out.scrollHeight;
  }

  function upsert(run, withSnapshotLines) {
    var rec = ensureItem(run);
    if (withSnapshotLines) {
      // 页面刚打开：把已有输出一次性补齐，之后只追增量
      fetch(BASE + '/v1/commands/detail?id=' + encodeURIComponent(run.id) + '&after=0')
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.lines) appendLines(rec, d.lines);
        })
        .catch(function () { });
    }
    return rec;
  }

  /* ---------- 事件 ---------- */
  function onEvent(ev) {
    if (!ev || !ev.id || ev.id <= lastSeq) return;
    lastSeq = ev.id;
    if (ev.type === 'line' && ev.run_id) {
      var rec = runs[ev.run_id] ? ensureItem(ev.run) : upsert(ev.run, false);
      if (ev.run) {
        rec.run.status = ev.run.status;
        rec.badge.textContent = STATUS[ev.run.status] || ev.run.status;
        rec.badge.className = 'wcmdBadge ' + ev.run.status;
        rec.stop.style.display = ev.run.status === 'running' ? '' : 'none';
      }
      appendLines(rec, [ev.line]);
      updateTime(rec);
      connectState();
      return;
    }
    if (ev.run) upsert(ev.run, false);
  }

  function connect() {
    if (es) { try { es.close(); } catch (e) { } }
    es = new EventSource(BASE + '/v1/commands/stream?after=' + lastSeq);
    es.onmessage = function (e) {
      try { onEvent(JSON.parse(e.data)); } catch (err) { }
    };
    es.onopen = function () { dotEl.classList.add('on'); };
    es.onerror = function () { /* EventSource 自带重连，带 Last-Event-ID 续传 */ };
  }

  /* ---------- 操作 ---------- */
  function post(path, body) {
    return fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }

  function abort(id) {
    post('/v1/commands/abort', { id: id, by: PAGE }).catch(function () { });
  }

  function runCommand() {
    var cmd = String(inputEl.value || '').trim();
    if (!cmd) return;
    inputEl.value = '';
    post('/v1/commands/start', { command: cmd, source: PAGE, owner: PAGE })
      .then(function (d) {
        if (d && d.run) { upsert(d.run, true); root.classList.remove('min'); }
      })
      .catch(function () { });
  }

  runBtn.addEventListener('click', runCommand);
  inputEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); runCommand(); }
  });

  /* ---------- 启动：先取快照，再从当前序号订阅 ---------- */
  fetch(BASE + '/v1/commands?limit=20')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      (d.commands || []).forEach(function (run) { upsert(run, true); });
      lastSeq = (d.stats && d.stats.seq) || 0;
      connect();
    })
    .catch(connect);

  setInterval(function () {
    for (var i = 0; i < order.length; i++) {
      var rec = runs[order[i]];
      if (rec && rec.run.status === 'running') updateTime(rec);
    }
  }, 500);
})();
