/* Wasily 独立网站前端：无框架，按页按需加载，避免首页为后台接口付出成本。 */
(function () {
  'use strict';

  var PAGES = ['home', 'workbench', 'kb', 'settings', 'changelog', 'about'];
  var current = 'home';
  var overview = null;

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtBytes(n) {
    n = Number(n || 0);
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }

  function fmtTime(ts) {
    if (!ts) return '-';
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return '-';
    var p = function (v) { return v < 10 ? '0' + v : '' + v; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function toast(msg, isErr) {
    var el = $('#toast');
    el.textContent = msg;
    el.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(el._timer);
    el._timer = setTimeout(function () { el.className = 'toast'; }, 2600);
  }

  function api(path, options) {
    var opts = options || {};
    var init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (resp) {
      return resp.text().then(function (text) {
        var data;
        try { data = text ? JSON.parse(text) : {}; }
        catch (e) { throw new Error('接口返回非 JSON：' + text.slice(0, 120)); }
        if (!resp.ok) throw new Error(data.error || ('请求失败 ' + resp.status));
        return data;
      });
    });
  }

  /* ---------- 路由 ---------- */
  function route() {
    var hash = (location.hash || '#/home').replace(/^#\/?/, '');
    var name = PAGES.indexOf(hash) >= 0 ? hash : 'home';
    if (name === current && !document.body.dataset.booted) { /* 首次仍渲染 */ }
    current = name;
    $$('.page').forEach(function (page) {
      page.hidden = page.getAttribute('data-page') !== name;
    });
    $$('#nav a').forEach(function (a) {
      a.classList.toggle('active', a.getAttribute('data-nav') === name);
    });
    document.title = (name === 'home' ? 'Wasily · 面向任务的 AI 开发工作台'
      : name === 'workbench' ? '工作台 · Wasily'
      : name === 'kb' ? '知识库 · Wasily'
      : name === 'settings' ? '设置 · Wasily'
      : name === 'changelog' ? '更新日志 · Wasily' : '关于 · Wasily');
    window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
    if (name === 'workbench') { loadSessions(); loadRuntime(); }
    if (name === 'kb') { loadKbStats(); loadKbRecent(); }
    if (name === 'settings') {
      loadLogInfo(); loadUpdateStatus(); renderEnv(); loadManifest(); loadOptimizeSettings();
    }
  }

  /* ---------- 概览 ---------- */
  function loadOverview() {
    return api('/v1/site/overview').then(function (data) {
      overview = data;
      $('#top-version').textContent = 'v' + (data.version || '-');
      $('#st-model').textContent = '模型 · ' + ((data.model && data.model.name) || '未配置');
      $('#st-kb').textContent = '知识库 · ' + fmtBytes(data.kb && data.kb.index_size);
      var ideAlive = data.services && data.services.ide_bridge;
      $('#st-ide').innerHTML = '<i class="dot' + (ideAlive ? '' : ' warn') + '"></i>' +
        (ideAlive ? 'IDE 已连接' : 'IDE 未连接');
      $('#about-version').textContent = data.version || '-';
      $('#about-build').textContent = data.build || '-';
      $('#about-channel').textContent = (data.update && data.update.channel) || '-';
      $('#about-logs').textContent = (data.logs && data.logs.root) || '-';
      $('#m-model').textContent = (data.model && (data.model.name || data.model.id)) || '-';
      $('#m-ide').textContent = ideAlive ? '在线' : '离线';
      renderEnv();
    }).catch(function (err) {
      toast('后端信息读取失败：' + err.message, true);
    });
  }

  function renderEnv() {
    var box = $('#env-list');
    if (!box || !overview) return;
    var rows = [];
    (overview.mcp || []).forEach(function (s) {
      rows.push('<div class="row-item"><div class="row-main"><div class="row-title">' +
        esc(s.name) + '</div><div class="row-sub">MCP 服务 · ' + esc(s.transport) + '</div></div>' +
        '<span class="tag ' + (s.enabled ? 'ok' : '') + '">' + (s.enabled ? '已启用' : '已停用') +
        '</span></div>');
    });
    var svc = overview.services || {};
    [['后端 API', svc.api], ['IDE 桥 18753', svc.ide_bridge], ['远程 MCP 9000', svc.remote_mcp]]
      .forEach(function (pair) {
        rows.push('<div class="row-item"><div class="row-main"><div class="row-title">' +
          esc(pair[0]) + '</div><div class="row-sub">本机端口</div></div>' +
          '<span class="tag ' + (pair[1] ? 'ok' : 'err') + '">' + (pair[1] ? '可用' : '未启动') +
          '</span></div>');
      });
    box.innerHTML = rows.join('') || '<div class="empty">暂无可用服务</div>';
  }

  /* ---------- 工作台 ---------- */
  function loadSessions() {
    api('/v1/agent/sessions?limit=30').then(function (data) {
      var list = data.sessions || [];
      $('#m-sessions').textContent = list.length;
      $('#m-ide').textContent = data.ide_online ? '在线' : '离线';
      var box = $('#session-list');
      if (!list.length) {
        box.innerHTML = '<div class="empty">还没有会话，点击右上角「新建任务」开始</div>';
        return;
      }
      box.innerHTML = list.map(function (s) {
        return '<a class="row-item" href="/chat?session=' + encodeURIComponent(s.session_id) +
          '" target="_blank" rel="noopener">' +
          '<div class="row-main"><div class="row-title">' + esc(s.title || '空会话') + '</div>' +
          '<div class="row-sub">' + s.count + ' 条消息 · ' + fmtTime(s.updated) + '</div></div>' +
          '<span class="tag ' + (s.source === 'ide' ? 'ide' : '') + '">' +
          (s.source === 'ide' ? 'IDE' : '网页') + '</span>' +
          '<span class="tag">' + esc(s.session_id).slice(0, 16) + '</span></a>';
      }).join('');
    }).catch(function (err) {
      $('#session-list').innerHTML = '<div class="empty">读取失败：' + esc(err.message) + '</div>';
    });
  }

  function loadRuntime() {
    api('/v1/agent/runtime').then(function (data) {
      var rows = data.sessions || [];
      var busy = rows.filter(function (r) { return r.busy; }).length;
      $('#m-running').textContent = busy;
      var box = $('#runtime-list');
      if (!rows.length) {
        box.innerHTML = '<div class="empty">当前没有运行中的任务</div>';
        return;
      }
      box.innerHTML = rows.map(function (r) {
        var state = r.busy ? '运行中' : (r.state || '空闲');
        return '<div class="row-item"><div class="row-main"><div class="row-title">' +
          esc(r.session_id) + '</div><div class="row-sub">阶段 ' + esc(r.phase || '-') +
          (r.tool ? ' · 工具 ' + esc(r.tool) : '') + ' · ' + fmtTime(r.updated) + '</div></div>' +
          '<span class="tag ' + (r.busy ? 'busy' : '') + '">' + esc(state) + '</span></div>';
      }).join('');
    }).catch(function (err) {
      $('#runtime-list').innerHTML = '<div class="empty">读取失败：' + esc(err.message) + '</div>';
    });
  }

  /* ---------- 知识库 ---------- */
  function loadKbStats() {
    api('/v1/kb/stats').then(function (data) {
      var s = data.stats || {};
      $('#k-files').textContent = s.indexed_files || 0;
      $('#k-chunks').textContent = s.chunks || 0;
      $('#k-size').textContent = (s.db_mb || 0) + ' MB';
      var pending = (s.by_status && s.by_status.pending) || 0;
      $('#k-status').textContent = pending ? (pending + ' 待索引') : '已同步';
    }).catch(function () {
      $('#k-status').textContent = '不可用';
    });
  }

  function loadKbRecent() {
    api('/v1/kb/recent?limit=12').then(function (data) {
      var rows = data.files || [];
      var box = $('#kb-recent');
      if (!rows.length) {
        box.innerHTML = '<div class="empty">知识库还没有内容，打开工作台扫描本地文件</div>';
        return;
      }
      box.innerHTML = rows.map(function (f) {
        var target = f.kind === 'image'
          ? '/v1/kb/file?path=' + encodeURIComponent(f.path)
          : '/v1/kb/file?path=' + encodeURIComponent(f.path);
        return '<a class="row-item" href="' + target + '" target="_blank" rel="noopener">' +
          '<div class="row-main"><div class="row-title">' + esc(f.name) + '</div>' +
          '<div class="row-sub">' + esc(f.kind || '') + ' · ' + fmtBytes(f.size) +
          ' · ' + fmtTime(f.mtime) + '</div></div><span class="tag">' + esc(f.ext || '') +
          '</span></a>';
      }).join('');
    }).catch(function (err) {
      $('#kb-recent').innerHTML = '<div class="empty">读取失败：' + esc(err.message) + '</div>';
    });
  }

  function searchKb(q) {
    if (!q) return;
    var box = $('#kb-results');
    box.innerHTML = '<div class="empty">检索中…</div>';
    api('/v1/kb/search?q=' + encodeURIComponent(q) + '&limit=12').then(function (data) {
      var rows = data.results || [];
      if (!rows.length) {
        box.innerHTML = '<div class="empty">没有匹配结果，换个说法试试</div>';
        return;
      }
      box.innerHTML = rows.map(function (r) {
        var snippet = (r.snippets && r.snippets[0] && r.snippets[0].text) || r.caption || '';
        return '<a class="row-item" href="' + (r.open || r.raw || '#') +
          '" target="_blank" rel="noopener"><div class="row-main"><div class="row-title">' +
          esc(r.name) + '</div><div class="row-sub">' + esc(snippet).slice(0, 160) +
          '</div></div><span class="tag">' + (r.kind || '') + '</span></a>';
      }).join('');
    }).catch(function (err) {
      box.innerHTML = '<div class="empty">检索失败：' + esc(err.message) + '</div>';
    });
  }

  /* ---------- 设置：日志 ---------- */
  function loadLogInfo() {
    api('/v1/logs/info').then(function (data) {
      $('#log-root').textContent = data.root;
      $('#log-session').textContent = data.session_dir;
      $('#log-default').textContent = data.default_root;
      $('#log-path').value = data.custom ? data.root : '';
      $('#log-keep').value = data.keep_days;
      var box = $('#log-files');
      var files = data.files || [];
      if (!files.length) {
        box.innerHTML = '<div class="empty">本次会话暂无日志文件</div>';
        return;
      }
      box.innerHTML = files.map(function (f) {
        return '<button class="row-item" data-log="' + esc(f.name) + '">' +
          '<div class="row-main"><div class="row-title">' + esc(f.name) + '</div>' +
          '<div class="row-sub">' + fmtBytes(f.size) + ' · ' + fmtTime(f.mtime) +
          '</div></div><span class="tag">查看</span></button>';
      }).join('');
    }).catch(function (err) {
      toast('日志信息读取失败：' + err.message, true);
    });
  }

  function viewLog(name) {
    var pre = $('#log-tail');
    pre.textContent = '读取中…';
    api('/v1/logs/tail?name=' + encodeURIComponent(name) + '&lines=300')
      .then(function (data) { pre.textContent = data.text || '（空文件）'; })
      .catch(function (err) { pre.textContent = '读取失败：' + err.message; });
  }

  /* ---------- 设置：更新 ---------- */
  function loadUpdateStatus() {
    api('/v1/update/status').then(function (data) {
      var cur = data.current || {};
      $('#upd-current').textContent = cur.version || '-';
      $('#upd-last').textContent = data.last_check ? fmtTime(data.last_check) : '尚未检查';
      $('#upd-channel-hint').textContent = data.channel || 'stable';
      $('#upd-pending').textContent = data.pending ? ('v' + data.pending.version + ' 待重启') : '无';
      $('#upd-auto').checked = true;
    }).catch(function () { /* 静默 */ });
  }

  function loadManifest() {
    api('/v1/settings/all').then(function (data) {
      var s = data.settings || {};
      $('#upd-manifest').value = s['update.manifest_url'] || '';
      $('#upd-auto').checked = s['update.auto_check'] !== false;
    }).catch(function () { /* 静默 */ });
  }

  /* ---------- 设置：输入优化 / 检索路由 ---------- */
  function loadOptimizeSettings() {
    api('/v1/settings/all').then(function (data) {
      var s = data.settings || {};
      $('#opt-mode').value = s['prompt_optimize.mode'] || 'basic';
      $('#opt-template').value = s['prompt_optimize.template'] || '';
      $('#web-auto').checked = s['web.auto_search'] !== false;
      $('#web-engine').value = s['web.engine'] || 'bing';
      $('#router-auto').checked = s['task_router.auto'] !== false;
      $('#router-preflight').checked = s['task_router.preflight_notice'] !== false;
    }).catch(function (err) {
      toast('读取设置失败：' + err.message, true);
    });
  }

  function saveSetting(key, value, okMsg) {
    return api('/v1/settings/set', { method: 'POST', body: { key: key, value: value } })
      .then(function () { toast(okMsg || '已保存'); })
      .catch(function (err) { toast('保存失败：' + err.message, true); });
  }

  function testOptimize() {
    var pre = $('#opt-preview');
    pre.textContent = '生成中…';
    api('/v1/prompt/optimize', {
      method: 'POST',
      body: { text: '把这个项目里没用的临时文件整理一下，并告诉我删了什么', context: '' }
    }).then(function (data) {
      pre.textContent = data.text || '（无结果）';
    }).catch(function (err) {
      pre.textContent = '失败：' + err.message;
    });
  }

  function assessTraining() {
    var box = $('#train-assess');
    box.className = 'update-result';
    box.textContent = '正在探测 CPU / 内存 / 显卡 / 磁盘…';
    api('/v1/train/assess', { method: 'POST', body: {} }).then(function (d) {
      var env = d.env || {};
      var inc = d.incremental || {};
      var rt = d.realtime || {};
      var gpuText = (env.gpus && env.gpus.length)
        ? env.gpus.map(function (g) { return g.name + ' · ' + g.vram_gb + ' GB'; }).join('；')
        : '未检测到 NVIDIA 显卡';
      var cls = (d.recommended === 'incremental') ? 'ok' : 'new';
      box.className = 'update-result ' + cls;
      box.innerHTML =
        '<strong>' + esc(d.summary || '') + '</strong>' +
        '<span class="notes">实时微调：' + esc(rt.supported ? '可行' : '不可行') +
        ' — ' + esc(rt.reason || '') + '\n' +
        '增量训练：' + esc(inc.level || '') + ' — ' + esc(inc.detail || '') + '\n' +
        '环境：CPU ' + esc(env.cpu_count || 0) + ' 核 · 内存 ' + esc(env.ram_gb || 0) +
        ' GB · 显卡 ' + esc(gpuText) + ' · 磁盘可用 ' + esc(env.disk_free_gb || 0) + ' GB\n' +
        '现有 adapter：' + esc((inc.current && inc.current.existing_adapters) || 0) +
        ' 个 · 训练样本：' + esc((inc.current && inc.current.dataset_samples) || 0) + ' 条</span>' +
        '<span class="notes">推荐路径：' + esc(
          d.recommended === 'incremental'
            ? '离线 QLoRA 增量训练，训练后热挂载 adapter'
            : (d.immediate && d.immediate.name) || '记忆与知识注入') + '</span>' +
        '<span class="notes">' + (d.plan || []).map(function (p) {
          return esc(p.step + '. ' + p.title + '：' + p.detail);
        }).join('\n') + '</span>';
    }).catch(function (err) {
      box.className = 'update-result err';
      box.textContent = '评估失败：' + err.message;
    });
  }

  function checkUpdate() {
    var btn = $('#btn-upd-check');
    var box = $('#upd-result');
    btn.disabled = true;
    box.className = 'update-result';
    box.textContent = '正在检查…';
    api('/v1/update/check', { method: 'POST', body: { force: true } }).then(function (data) {
      if (data.has_update) {
        box.className = 'update-result new';
        box.innerHTML = '发现新版本 v' + esc(data.latest) + '（当前 v' + esc(data.current) + '）' +
          (data.notes ? '<span class="notes">' + esc(data.notes) + '</span>' : '') +
          '<div class="actions"><button class="btn btn-primary btn-sm" id="btn-upd-download">下载并暂存</button></div>';
        var dl = $('#btn-upd-download');
        if (dl) dl.addEventListener('click', function () { downloadUpdate(data); });
      } else if (data.ok === false) {
        box.className = 'update-result err';
        box.textContent = data.message || '检查失败';
      } else {
        box.className = 'update-result ok';
        box.textContent = data.message || '当前没有可用的更新';
      }
      loadUpdateStatus();
    }).catch(function (err) {
      box.className = 'update-result err';
      box.textContent = '检查失败：' + err.message;
    }).then(function () { btn.disabled = false; });
  }

  function downloadUpdate(data) {
    var box = $('#upd-result');
    box.className = 'update-result';
    box.textContent = '正在下载并校验安装包…';
    api('/v1/update/download', { method: 'POST', body: { package: data.package || {} } })
      .then(function (res) {
        box.textContent = '下载完成，正在解压到暂存区…';
        return api('/v1/update/apply', {
          method: 'POST', body: { path: res.path, version: data.latest }
        });
      })
      .then(function () {
        box.className = 'update-result ok';
        box.textContent = '新版本已暂存，重启应用后完成切换。';
        loadUpdateStatus();
      })
      .catch(function (err) {
        box.className = 'update-result err';
        box.textContent = '更新失败：' + err.message;
      });
  }

  /* ---------- 模式 ---------- */
  function setMode(mode) {
    var target = mode === 'solo' ? 'solo' : 'ask';
    api('/ide_mode', { method: 'POST', body: { mode: target } }).then(function (data) {
      var label = data.mode === 'solo' ? 'SOLO' : (data.mode === 'plan' ? 'PLAN' : 'IDE');
      $('#mode-state').textContent = '当前：' + label;
      toast('已切换到 ' + label + ' 模式');
    }).catch(function (err) { toast('切换失败：' + err.message, true); });
  }

  function loadMode() {
    api('/ide_mode').then(function (data) {
      var label = data.mode === 'solo' ? 'SOLO' : (data.mode === 'plan' ? 'PLAN' : 'IDE');
      var el = $('#mode-state');
      if (el) el.textContent = '当前：' + label;
    }).catch(function () { /* 静默 */ });
  }

  /* ---------- 事件绑定 ---------- */
  function bind() {
    window.addEventListener('hashchange', route);

    $$('[data-mode]').forEach(function (btn) {
      btn.addEventListener('click', function () { setMode(btn.getAttribute('data-mode')); });
    });

    $('#btn-refresh-sessions').addEventListener('click', loadSessions);
    $('#btn-kb-recent').addEventListener('click', loadKbRecent);

    $('#kb-form').addEventListener('submit', function (e) {
      e.preventDefault();
      searchKb($('#kb-q').value.trim());
    });

    $('#btn-log-save').addEventListener('click', function () {
      var path = $('#log-path').value.trim();
      api('/v1/settings/set', { method: 'POST', body: { key: 'logging.path', value: path } })
        .then(function (data) {
          toast(path ? '日志目录已切换' : '已恢复默认目录');
          loadLogInfo();
          if (overview) loadOverview();
        })
        .catch(function (err) { toast('保存失败：' + err.message, true); });
    });

    $('#btn-log-reset').addEventListener('click', function () {
      $('#log-path').value = '';
      api('/v1/settings/set', { method: 'POST', body: { key: 'logging.path', value: '' } })
        .then(function () { toast('已恢复默认目录'); loadLogInfo(); })
        .catch(function (err) { toast('操作失败：' + err.message, true); });
    });

    $('#btn-log-open').addEventListener('click', function () {
      api('/v1/logs/open', { method: 'POST', body: {} })
        .then(function () { toast('已在资源管理器中打开'); })
        .catch(function (err) { toast('打开失败：' + err.message, true); });
    });

    $('#btn-log-purge').addEventListener('click', function () {
      var days = Number($('#log-keep').value || 0);
      api('/v1/settings/set', { method: 'POST', body: { key: 'logging.keep_days', value: days } })
        .catch(function () {})
        .then(function () {
          return api('/v1/logs/purge', { method: 'POST', body: { days: days } });
        })
        .then(function (data) { toast('已清理 ' + (data.removed || 0) + ' 个历史目录'); })
        .catch(function (err) { toast('清理失败：' + err.message, true); });
    });

    $('#log-files').addEventListener('click', function (e) {
      var btn = e.target.closest('[data-log]');
      if (btn) viewLog(btn.getAttribute('data-log'));
    });

    $('#btn-upd-check').addEventListener('click', checkUpdate);

    $('#btn-upd-save').addEventListener('click', function () {
      var url = $('#upd-manifest').value.trim();
      api('/v1/settings/set', { method: 'POST', body: { key: 'update.manifest_url', value: url } })
        .then(function () { toast('更新地址已保存'); })
        .catch(function (err) { toast('保存失败：' + err.message, true); });
    });

    $('#btn-opt-save').addEventListener('click', function () {
      saveSetting('prompt_optimize.mode', $('#opt-mode').value, '优化模式已保存');
      saveSetting('prompt_optimize.template', $('#opt-template').value, '提示词模板已保存');
    });
    $('#btn-opt-test').addEventListener('click', testOptimize);
    $('#btn-web-save').addEventListener('click', function () {
      saveSetting('web.auto_search', $('#web-auto').checked, '检索设置已保存');
      saveSetting('web.engine', $('#web-engine').value);
    });
    $('#btn-router-save').addEventListener('click', function () {
      saveSetting('task_router.auto', $('#router-auto').checked, '路由设置已保存');
      saveSetting('task_router.preflight_notice', $('#router-preflight').checked);
    });
    $('#btn-train-assess').addEventListener('click', assessTraining);

    $('#upd-auto').addEventListener('change', function () {
      api('/v1/settings/set', {
        method: 'POST', body: { key: 'update.auto_check', value: $('#upd-auto').checked }
      }).then(function () { toast('已保存'); })
        .catch(function (err) { toast('保存失败：' + err.message, true); });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.body.dataset.booted = '1';
    bind();
    route();
    loadOverview();
    loadMode();
    if (current === 'home') {
      // 首页只做概览，避免一次性打满接口
    }
  });
})();
