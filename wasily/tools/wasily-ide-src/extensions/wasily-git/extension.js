'use strict';
/*
 * Wasily Git：IDE 内置的 Git 客户端。
 *
 * 设计约束：
 * - 不在扩展里执行任何 git 命令，也不直接读写 .git；所有仓库操作都走 Wasily 后端
 *   （Dulwich 内核），这样「无系统 git」的机器也能完整工作。
 * - 差异对比用 vscode.diff + 自定义虚拟文档，HEAD / 索引内容按需即时取回，
 *   不落地临时文件，避免污染工作区。
 * - 自迭代闭环（需求 → 隔离工作区 → 校验 → 提交 → 推送 → 审计）也在这里暴露，
 *   让 IDE 内部就能完成一轮完整迭代。
 */

const vscode = require('vscode');
const http = require('http');
const https = require('https');
const path = require('path');
const fs = require('fs');

const SCHEME = 'wasily-git';

let output = null;
let scm = null;
let groupMerge = null;
let groupStaged = null;
let groupChanges = null;
let statusState = { root: '', status: null };
let historyView = null;
let iterationsView = null;
let refreshing = null;
let refreshTimer = null;

// ---------- 基础 ----------

function workspaceRoot() {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || !folders.length) return '';
  return folders[0].uri.fsPath;
}

function serverUrl() {
  const cfg = vscode.workspace.getConfiguration('wasily-git');
  let url = String(cfg.get('serverUrl') || '').trim();
  if (!url) {
    url = String(vscode.workspace.getConfiguration('wasily-ai').get('serverUrl') || '').trim();
  }
  return (url || 'http://127.0.0.1:8000').replace(/\/+$/, '');
}

function request(method, pathname, options) {
  const opts = options || {};
  const timeout = opts.timeout || 60000;
  return new Promise((resolve, reject) => {
    let url;
    try {
      url = new URL(serverUrl() + pathname);
    } catch (e) {
      reject(new Error('后端地址不合法: ' + serverUrl()));
      return;
    }
    if (opts.query) {
      Object.keys(opts.query).forEach((key) => {
        const val = opts.query[key];
        if (val !== undefined && val !== null && val !== '') url.searchParams.set(key, String(val));
      });
    }
    const payload = opts.body ? Buffer.from(JSON.stringify(opts.body), 'utf8') : null;
    const headers = { 'Content-Type': 'application/json' };
    if (payload) headers['Content-Length'] = String(payload.length);
    const lib = url.protocol === 'https:' ? https : http;
    const req = lib.request(url, { method: method, headers: headers }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        let data;
        try {
          data = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
        } catch (e) {
          data = { ok: false, error: '后端返回的不是 JSON' };
        }
        if (res.statusCode >= 400 || data.ok === false) {
          const err = new Error(data.error || ('HTTP ' + res.statusCode));
          err.code = data.code || ('http_' + res.statusCode);
          err.data = data;
          reject(err);
          return;
        }
        resolve(data);
      });
    });
    req.setTimeout(timeout, () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

function log(line) {
  if (output) output.appendLine(line);
}

async function guard(label, fn) {
  try {
    return await fn();
  } catch (err) {
    const msg = String((err && err.message) || err);
    const code = (err && err.code) || '';
    log('[error] ' + label + ' ' + code + ' ' + msg);
    if (/ECONNREFUSED|socket hang up|请求超时/i.test(msg)) {
      vscode.window.showErrorMessage('连接不上 Wasily 后端（' + serverUrl() + '），请先启动服务。');
    } else if (code === 'dirty_tree') {
      vscode.window.showWarningMessage('工作区有未提交修改，请先提交或暂存再切换分支。');
    } else if (code === 'need_confirm') {
      vscode.window.showWarningMessage(msg);
    } else {
      vscode.window.showErrorMessage(msg);
    }
    return null;
  }
}

function virtualUri(root, rel, side, stamp) {
  return vscode.Uri.from({
    scheme: SCHEME,
    path: '/' + String(rel).replace(/\\/g, '/'),
    query: 'root=' + encodeURIComponent(root) + '&side=' + side + '&v=' + (stamp || Date.now())
  });
}

function relOf(uri) {
  const root = workspaceRoot();
  return path.relative(root, uri.fsPath).replace(/\\/g, '/');
}

// ---------- 状态渲染 ----------

function makeState(rel, kind, code) {
  const root = workspaceRoot();
  const abs = path.join(root, rel);
  const exists = fs.existsSync(abs);
  let icon = 'diff-modified';
  let strike = false;
  if (kind === 'changes' && !exists) {
    icon = 'diff-removed';
    strike = true;
  } else if (code === 'A') {
    icon = 'diff-added';
  } else if (code === 'D') {
    icon = 'diff-removed';
    strike = true;
  } else if (code === 'M') {
    icon = 'diff-modified';
  }
  const item = {
    resourceUri: vscode.Uri.file(abs),
    decorations: {
      tooltip: rel + (strike ? '（已删除）' : ''),
      strikeThrough: strike,
      iconPath: new vscode.ThemeIcon(icon),
      faded: kind === 'changes' && !exists
    },
    contextValue: kind,
    command: {
      command: 'wasily-git.openDiff',
      title: '查看逐行差异',
      arguments: [{ rel: rel, group: kind }]
    },
    wasily: { rel: rel, group: kind }
  };
  return item;
}

function setGroup(group, states) {
  if (!group) return;
  group.resourceStates = states;
  try {
    group.hideWhenEmpty = true;
  } catch (e) {
    // 旧版本没有这个属性，忽略即可
  }
}

function render() {
  const st = statusState.status;
  if (!st || !scm) return;
  const conflicts = Array.isArray(st.conflicts) ? st.conflicts : [];
  const staged = Array.isArray(st.staged) ? st.staged : [];
  const changed = Array.isArray(st.changed) ? st.changed : [];
  const untracked = Array.isArray(st.untracked) ? st.untracked : [];
  setGroup(groupMerge, conflicts.map((p) => makeState(p, 'merge')));
  setGroup(groupStaged, staged.map((row) => makeState(row.path, 'staged', row.code)));
  const rest = Array.from(new Set(changed.concat(untracked))).sort();
  setGroup(groupChanges, rest.map((p) => makeState(p, 'changes')));
  const branch = st.branch || '（游离 HEAD）';
  const head = st.head ? st.head.slice(0, 8) : '无提交';
  scm.inputBox.placeholder = branch + ' · ' + head + ' — 提交信息：feat(scope): 描述';
  vscode.commands.executeCommand('setContext', 'wasilyGit.branch', branch);
  vscode.commands.executeCommand('setContext', 'wasilyGit.clean', !!st.clean);
  scm.count = conflicts.length + staged.length + rest.length;
}

async function refresh(force) {
  const root = workspaceRoot();
  if (!root) return null;
  if (refreshing && !force) return refreshing;
  refreshing = (async () => {
    const st = await request('GET', '/v1/git/status', {
      query: { path: root, fresh: force ? 1 : 0 },
      timeout: 180000
    });
    statusState = { root: root, status: st };
    render();
    return st;
  })();
  try {
    return await refreshing;
  } finally {
    refreshing = null;
  }
}

function scheduleRefresh(delay) {
  if (!vscode.workspace.getConfiguration('wasily-git').get('autoRefresh')) return;
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    guard('auto-refresh', () => refresh(false));
  }, delay || 1200);
}

// ---------- 差异视图 ----------

function registerContentProvider(context) {
  const provider = {
    onDidChange: new vscode.EventEmitter().event,
    async provideTextDocumentContent(uri) {
      const params = new URLSearchParams(uri.query);
      const root = params.get('root') || workspaceRoot();
      const side = params.get('side') || 'head';
      const rel = uri.path.replace(/^\//, '');
      try {
        const res = await request('GET', '/v1/git/content', {
          query: { path: root, rel: rel, side: side }
        });
        if (res.binary) return '（二进制文件，无法按文本对比；请直接暂存/提交）';
        return res.text || '';
      } catch (e) {
        if (e.code === 'not_found') return '';
        throw e;
      }
    }
  };
  context.subscriptions.push(
    vscode.workspace.registerTextDocumentContentProvider(SCHEME, provider)
  );
}

async function openDiff(arg) {
  const target = arg && arg.rel ? arg : (arg && arg.wasily ? arg.wasily : null);
  if (!target) return;
  const rel = target.rel;
  const group = target.group || 'changes';
  const root = workspaceRoot();
  const stamp = Date.now();
  const leftSide = group === 'staged' || group === 'merge' ? 'head' : 'index';
  const rightSide = group === 'staged' || group === 'merge' ? 'index' : 'worktree';
  const left = virtualUri(root, rel, leftSide, stamp);
  const right = rightSide === 'worktree'
    ? vscode.Uri.file(path.join(root, rel))
    : virtualUri(root, rel, rightSide, stamp);
  const title = rel + '  (' + leftSide + ' ↔ ' + rightSide + ')';
  await vscode.commands.executeCommand('vscode.diff', left, right, title);
}

// ---------- 暂存与提交 ----------

async function stage(arg) {
  const targets = toTargets(arg);
  if (!targets.length) return;
  await request('POST', '/v1/git/stage', {
    body: { path: workspaceRoot(), files: targets.map((x) => x.rel) }
  });
  await refresh(true);
}

async function unstage(arg) {
  const targets = toTargets(arg);
  if (!targets.length) return;
  await request('POST', '/v1/git/unstage', {
    body: { path: workspaceRoot(), files: targets.map((x) => x.rel) }
  });
  await refresh(true);
}

function toTargets(arg) {
  if (!arg) return [];
  if (Array.isArray(arg)) return arg.map((x) => (x.wasily || x)).filter((x) => x && x.rel);
  const one = arg.wasily || arg;
  if (one && one.rel) return [one];
  return [];
}

async function stageAll() {
  const st = statusState.status || await refresh(true);
  if (!st) return;
  const files = Array.from(new Set((st.changed || []).concat(st.untracked || [])));
  if (!files.length) {
    vscode.window.showInformationMessage('没有需要暂存的修改。');
    return;
  }
  await request('POST', '/v1/git/stage', { body: { path: workspaceRoot(), files: files } });
  await refresh(true);
}

async function unstageAll() {
  const st = statusState.status || await refresh(true);
  if (!st) return;
  const files = (st.staged || []).map((row) => row.path);
  if (!files.length) return;
  await request('POST', '/v1/git/unstage', { body: { path: workspaceRoot(), files: files } });
  await refresh(true);
}

const COMMIT_RE = /^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^()\r\n]{1,32}\))?!: \S.+$/;

async function commitStaged() {
  const st = statusState.status || await refresh(true);
  if (!st) return;
  const message = String(scm.inputBox.value || '').trim();
  if (!message) {
    vscode.window.showWarningMessage('提交信息不能为空。');
    return;
  }
  if (!COMMIT_RE.test(message.split('\n')[0].trim())) {
    vscode.window.showWarningMessage('提交信息需符合 Conventional Commits，例如 feat(git): 增加部分暂存。');
    return;
  }
  const res = await request('POST', '/v1/git/commit', {
    body: { path: workspaceRoot(), message: message, expected_head: st.head || '' }
  });
  scm.inputBox.value = '';
  log('[commit] ' + res.commit + ' ' + message.split('\n')[0]);
  await refresh(true);
  vscode.window.setStatusBarMessage('已提交 ' + String(res.commit).slice(0, 8), 3000);
}

function firstLine(text) {
  const row = String(text || '').split('\n').find((line) => line.trim().length);
  return row ? row.slice(0, 80) : '';
}

async function stageHunk(arg) {
  const target = (arg && (arg.wasily || arg)) || null;
  if (!target || !target.rel) return;
  let info;
  try {
    info = await request('GET', '/v1/git/hunks', {
      query: { path: workspaceRoot(), rel: target.rel, base: 'index' }
    });
  } catch (e) {
    if (e.code === 'binary_unsupported') {
      vscode.window.showInformationMessage('二进制文件只能整文件暂存。');
      return;
    }
    throw e;
  }
  if (!info.hunks || !info.hunks.length) {
    vscode.window.showInformationMessage('该文件没有可单独暂存的变更块。');
    return;
  }
  const picks = info.hunks.map((h) => ({
    label: '@@ -' + (h.base_start + 1) + ',' + (h.base_end - h.base_start) +
      ' +' + (h.target_start + 1) + ',' + (h.target_end - h.target_start),
    description: firstLine(h.after),
    detail: (h.before ? '- ' + firstLine(h.before) + '    ' : '') + '+ ' + firstLine(h.after),
    id: h.id
  }));
  const chosen = await vscode.window.showQuickPick(picks, {
    canPickMany: true,
    placeHolder: '选择要暂存的变更块（' + target.rel + '）'
  });
  if (!chosen || !chosen.length) return;
  await request('POST', '/v1/git/stage_hunks', {
    body: { path: workspaceRoot(), file: target.rel, ids: chosen.map((c) => c.id) }
  });
  await refresh(true);
}

// ---------- 分支与远端 ----------

async function switchBranch() {
  const info = await request('GET', '/v1/git/branches', { query: { path: workspaceRoot() } });
  const items = (info.branches || []).map((name) => ({
    label: name,
    description: name === info.current ? '当前分支' : ''
  }));
  const pick = await vscode.window.showQuickPick(items, { placeHolder: '切换分支（需工作区干净）' });
  if (!pick) return;
  await request('POST', '/v1/git/switch', { body: { path: workspaceRoot(), name: pick.label } });
  await refresh(true);
  historyView.reload();
  vscode.window.setStatusBarMessage('已切换到 ' + pick.label, 3000);
}

async function createBranch() {
  const name = await vscode.window.showInputBox({
    prompt: '新分支名（需形如 feature/<名称>）',
    value: 'feature/',
    validateInput: (v) => (/^(feature|feat|fix|chore|docs|refactor|test)\/[A-Za-z0-9._\-/]{1,80}$/.test(v) ? '' : '请使用 feature/<名称> 形式')
  });
  if (!name) return;
  await request('POST', '/v1/git/branch/create', {
    body: { path: workspaceRoot(), name: name }
  });
  await request('POST', '/v1/git/switch', { body: { path: workspaceRoot(), name: name } });
  await refresh(true);
  vscode.window.setStatusBarMessage('已创建并切换到 ' + name, 3000);
}

async function mergeBranch() {
  const info = await request('GET', '/v1/git/branches', { query: { path: workspaceRoot() } });
  const items = (info.branches || [])
    .filter((name) => name !== info.current)
    .map((name) => ({ label: name, description: name.indexOf('feature/') === 0 ? '功能分支' : '' }));
  if (!items.length) {
    vscode.window.showInformationMessage('没有可合并的其他分支。');
    return;
  }
  const pick = await vscode.window.showQuickPick(items, { placeHolder: '把哪个分支合并进 ' + (info.current || '') });
  if (!pick) return;
  const res = await request('POST', '/v1/git/merge', {
    body: { path: workspaceRoot(), branch: pick.label }
  });
  await refresh(true);
  historyView.reload();
  const conflicts = res.conflicts || [];
  if (!conflicts.length) {
    vscode.window.showInformationMessage('已合并 ' + pick.label + ' → ' + (info.current || ''));
    return;
  }
  const action = await vscode.window.showWarningMessage(
    '合并产生 ' + conflicts.length + ' 个冲突，需要逐个解决后暂存并继续。',
    '打开冲突文件', '中止合并');
  if (action === '打开冲突文件') {
    for (const rel of conflicts) {
      await vscode.window.showTextDocument(vscode.Uri.file(path.join(workspaceRoot(), rel)));
    }
  } else if (action === '中止合并') {
    await request('POST', '/v1/git/merge/abort', {
      body: { path: workspaceRoot(), confirm: true }
    });
    await refresh(true);
    vscode.window.showInformationMessage('已中止合并，工作区恢复。');
  }
}

async function cloneRepo() {
  const url = await vscode.window.showInputBox({
    prompt: '仓库地址（https:// 或本地路径）',
    placeHolder: 'https://github.com/<owner>/<repo>.git'
  });
  if (!url) return;
  const target = await vscode.window.showInputBox({
    prompt: '克隆到本地目录（绝对路径）',
    value: path.join(path.dirname(workspaceRoot() || process.cwd()), 'wasily-clone')
  });
  if (!target) return;
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: '正在克隆 ' + url, cancellable: false },
    () => request('POST', '/v1/git/clone', { body: { url: url, target: target }, timeout: 600000 })
  );
  const pick = await vscode.window.showInformationMessage('已克隆到 ' + target, '打开该文件夹');
  if (pick === '打开该文件夹') {
    await vscode.commands.executeCommand('vscode.openFolder', vscode.Uri.file(target), true);
  }
}

async function fetchRemote() {
  const res = await request('POST', '/v1/git/fetch', {
    body: { path: workspaceRoot(), remote: 'origin' },
    timeout: 180000
  });
  const count = Object.keys(res.refs || {}).length;
  vscode.window.showInformationMessage('已获取远端更新（' + count + ' 个引用）。');
}

async function pullRemote() {
  const res = await request('POST', '/v1/git/pull', {
    body: { path: workspaceRoot(), remote: 'origin', ff_only: true },
    timeout: 180000
  });
  await refresh(true);
  historyView.reload();
  vscode.window.showInformationMessage(res.changed ? '已快进到远端最新提交。' : '已经是远端最新状态。');
}

async function pushRemote() {
  const res = await request('POST', '/v1/git/push', {
    body: { path: workspaceRoot(), remote: 'origin' },
    timeout: 180000
  });
  const ok = res.verified !== false;
  vscode.window.showInformationMessage((ok ? '已推送 ' : '推送结果待确认 ') + res.branch + ' → ' + String(res.head || '').slice(0, 8));
  await refresh(true);
}

// ---------- 历史视图 ----------

class HistoryProvider {
  constructor() {
    this.items = [];
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
    this.loading = false;
  }

  async reload() {
    if (this.loading) return;
    this.loading = true;
    try {
      const res = await request('GET', '/v1/git/log', {
        query: { path: workspaceRoot(), limit: 60 }
      });
      this.items = res.entries || [];
    } catch (e) {
      this.items = [];
      log('[history] ' + e.message);
    } finally {
      this.loading = false;
      this.emitter.fire();
    }
  }

  getTreeItem(el) {
    return el;
  }

  async getChildren(el) {
    if (el) return [];
    if (!this.items.length && !this.loading) this.reload();
    return this.items.map((row) => {
      const item = new vscode.TreeItem(String(row.message || '(无信息)').slice(0, 90), vscode.TreeItemCollapsibleState.None);
      item.description = String(row.sha || '').slice(0, 8) + ' · ' + new Date((row.time || 0) * 1000).toLocaleString();
      item.tooltip = String(row.sha || '') + '\n' + (row.author || '');
      item.contextValue = 'commit';
      item.iconPath = new vscode.ThemeIcon('git-commit');
      item.wasily = { sha: row.sha, message: row.message };
      item.command = {
        command: 'wasily-git.showCommit',
        title: '查看提交',
        arguments: [item.wasily]
      };
      return item;
    });
  }
}

async function showCommit(arg) {
  if (!arg || !arg.sha) return;
  const pick = await vscode.window.showQuickPick([
    { label: '复制提交号', id: 'copy' },
    { label: '回滚到该提交（mixed，保留工作区内容）', id: 'reset' },
    { label: '从此提交新建分支', id: 'branch' }
  ], { placeHolder: String(arg.sha).slice(0, 8) + ' ' + String(arg.message || '') });
  if (!pick) return;
  if (pick.id === 'copy') {
    await vscode.env.clipboard.writeText(arg.sha);
    return;
  }
  if (pick.id === 'reset') return resetTo(arg.sha);
  if (pick.id === 'branch') return branchFrom(arg.sha);
}

async function resetTo(sha) {
  const target = typeof sha === 'string' ? sha : (sha && sha.wasily ? sha.wasily.sha : '');
  if (!target) return;
  const confirm = await vscode.window.showWarningMessage(
    '回滚到 ' + target.slice(0, 8) + '？当前分支将移动，工作区文件保留为未暂存修改。',
    { modal: true },
    '确认回滚'
  );
  if (confirm !== '确认回滚') return;
  await request('POST', '/v1/git/reset', {
    body: { path: workspaceRoot(), mode: 'mixed', ref: target, confirm: true }
  });
  await refresh(true);
  historyView.reload();
  vscode.window.showInformationMessage('已回滚到 ' + target.slice(0, 8));
}

async function branchFrom(sha) {
  const target = typeof sha === 'string' ? sha : (sha && sha.wasily ? sha.wasily.sha : '');
  const name = await vscode.window.showInputBox({
    prompt: '从 ' + String(target).slice(0, 8) + ' 新建分支',
    value: 'feature/',
    validateInput: (v) => (/^(feature|feat|fix|chore|docs|refactor|test)\/[A-Za-z0-9._\-/]{1,80}$/.test(v) ? '' : '请使用 feature/<名称> 形式')
  });
  if (!name) return;
  await request('POST', '/v1/git/branch/create', {
    body: { path: workspaceRoot(), name: name, start: target }
  });
  await refresh(true);
  vscode.window.showInformationMessage('已从 ' + String(target).slice(0, 8) + ' 创建 ' + name);
}

// ---------- 自迭代闭环 ----------

class IterationsProvider {
  constructor() {
    this.items = [];
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
    this.loading = false;
  }

  async reload() {
    if (this.loading) return;
    this.loading = true;
    try {
      const res = await request('GET', '/v1/iterations', {
        query: { path: workspaceRoot(), limit: 40 }
      });
      this.items = res.iterations || [];
    } catch (e) {
      this.items = [];
      log('[iterations] ' + e.message);
    } finally {
      this.loading = false;
      this.emitter.fire();
    }
  }

  getTreeItem(el) {
    return el;
  }

  async getChildren(el) {
    if (el) return [];
    if (!this.items.length && !this.loading) this.reload();
    return this.items.map((row) => {
      const map = {
        prepared: ['circle-outline', '待校验'],
        verified: ['verified', '校验通过'],
        committed: ['archive', '已提交（未推送）'],
        pushed: ['pass', '已推送'],
        aborted: ['circle-slash', '已放弃'],
        failed: ['error', '失败']
      };
      const meta = map[row.state] || ['circle-outline', row.state];
      const item = new vscode.TreeItem(
        String(row.requirement || '(无需求描述)').slice(0, 80),
        vscode.TreeItemCollapsibleState.None
      );
      item.description = meta[1] + ' · ' + String(row.branch || '');
      item.tooltip = '需求：' + (row.requirement || '') +
        '\n分支：' + (row.branch || '') +
        '\n状态：' + row.state +
        '\n提交：' + String(row.commit || '无').slice(0, 8) +
        '\n更新：' + (row.updated || '');
      item.contextValue = 'iteration';
      item.iconPath = new vscode.ThemeIcon(meta[0]);
      item.wasily = { id: row.id, requirement: row.requirement, state: row.state, branch: row.branch };
      item.command = {
        command: 'wasily-git.showIteration',
        title: '查看迭代详情',
        arguments: [item.wasily]
      };
      return item;
    });
  }
}

async function startIteration() {
  const requirement = await vscode.window.showInputBox({
    prompt: '这一轮要改什么？（会写入结构化迭代轨迹）',
    placeHolder: '例如：给 SCM 增加按块暂存'
  });
  if (!requirement) return;
  const tests = await vscode.window.showInputBox({
    prompt: '可选：测试命令（留空则只做语法校验；跳过的检查不算通过）',
    placeHolder: 'python -m unittest discover -s tests -p "test_git_*.py"'
  });
  const res = await request('POST', '/v1/iterations/start', {
    body: { path: workspaceRoot(), requirement: requirement, tests: tests || '' },
    timeout: 180000
  });
  const state = res.iteration;
  iterationsView.reload();
  const pick = await vscode.window.showInformationMessage(
    '已在隔离工作区开始迭代：' + state.branch,
    '在文件管理器中打开'
  );
  if (pick === '在文件管理器中打开') {
    await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(state.worktree));
  }
}

function iterationArg(arg) {
  if (!arg) return '';
  if (typeof arg === 'string') return arg;
  if (arg.wasily && arg.wasily.id) return arg.wasily.id;
  if (arg.id) return arg.id;
  return '';
}

async function verifyIteration(arg) {
  const id = iterationArg(arg);
  if (!id) return;
  const res = await request('POST', '/v1/iterations/verify', {
    body: { path: workspaceRoot(), id: id },
    timeout: 600000
  });
  const checks = res.checks || [];
  output.show(true);
  checks.forEach((c) => log('[' + c.status + '] ' + c.name + (c.detail ? ' — ' + String(c.detail).split('\n')[0] : '')));
  iterationsView.reload();
  if (res.ok) {
    vscode.window.showInformationMessage('校验通过：' + (res.files || []).length + ' 个文件，可提交。');
  } else {
    vscode.window.showErrorMessage('校验未通过：' + (res.reason || '详见输出面板'));
  }
}

async function commitIteration(arg) {
  const id = iterationArg(arg);
  if (!id) return;
  const message = await vscode.window.showInputBox({
    prompt: '迭代提交信息（Conventional Commits）',
    value: 'feat(loop): '
  });
  if (!message) return;
  const res = await request('POST', '/v1/iterations/commit', {
    body: { path: workspaceRoot(), id: id, message: message, push: true },
    timeout: 300000
  });
  const push = res.push || {};
  const note = push.ok ? '并已推送 feature 分支' : ('，但推送未完成：' + (push.error || push.reason || '无远端'));
  vscode.window.showInformationMessage('已提交 ' + String(res.commit).slice(0, 8) + note);
  await refresh(true);
  historyView.reload();
  iterationsView.reload();
}

async function abortIteration(arg) {
  const id = iterationArg(arg);
  if (!id) return;
  const confirm = await vscode.window.showWarningMessage(
    '放弃这轮迭代并删除隔离目录？未提交的修改会丢失。',
    { modal: true },
    '放弃迭代'
  );
  if (confirm !== '放弃迭代') return;
  await request('POST', '/v1/iterations/abort', {
    body: { path: workspaceRoot(), id: id, force: true }
  });
  iterationsView.reload();
  vscode.window.showInformationMessage('已放弃该轮迭代。');
}

async function openIteration(arg) {
  const id = iterationArg(arg);
  if (!id) return;
  const res = await request('GET', '/v1/iterations/detail', {
    query: { path: workspaceRoot(), id: id }
  });
  const worktree = res.iteration && res.iteration.worktree;
  if (!worktree) return;
  await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(worktree));
}

async function showIteration(arg) {
  const id = iterationArg(arg);
  if (!id) return;
  const res = await request('GET', '/v1/iterations/detail', {
    query: { path: workspaceRoot(), id: id }
  });
  const it = res.iteration || {};
  const lines = [
    '迭代 ' + it.id,
    '需求：' + (it.requirement || ''),
    '分支：' + (it.branch || '') + '（基线 ' + (it.base || '') + '）',
    '状态：' + (it.state || ''),
    '隔离目录：' + (it.worktree || ''),
    '提交：' + (it.commit ? String(it.commit).slice(0, 8) + ' ' + (it.message || '') : '无'),
    '推送：' + JSON.stringify(it.push || {}),
    '校验：' + (it.checks || []).map((c) => c.name + '=' + c.status).join(', ')
  ];
  output.show(true);
  log('———— 迭代详情 ————');
  lines.forEach((line) => log(line));
  const pick = await vscode.window.showInformationMessage(
    (it.branch || '') + ' · ' + (it.state || ''),
    '打开隔离目录', '复制目录路径'
  );
  if (pick === '打开隔离目录') await openIteration(arg);
  if (pick === '复制目录路径') await vscode.env.clipboard.writeText(it.worktree || '');
}

async function showAudit() {
  const res = await request('GET', '/v1/git/audit', {
    query: { path: workspaceRoot(), limit: 80 }
  });
  const content = JSON.stringify(res.events || [], null, 2);
  const doc = await vscode.workspace.openTextDocument({ content: content, language: 'json' });
  await vscode.window.showTextDocument(doc, { preview: true });
}

// ---------- 激活 ----------

function registerCommand(context, name, handler) {
  context.subscriptions.push(
    vscode.commands.registerCommand(name, (...args) => guard(name, () => handler.apply(null, args)))
  );
}

function activate(context) {
  output = vscode.window.createOutputChannel('Wasily Git');
  context.subscriptions.push(output);
  const root = workspaceRoot();
  if (!root) {
    log('未打开工作区，Wasily Git 不启用。');
    return;
  }

  scm = vscode.scm.createSourceControl('wasily-git', 'Wasily Git', vscode.Uri.file(root));
  groupMerge = scm.createResourceGroup('merge', '合并冲突');
  groupStaged = scm.createResourceGroup('staged', '已暂存的更改');
  groupChanges = scm.createResourceGroup('changes', '更改');
  scm.inputBox.placeholder = '提交信息：feat(scope): 描述';
  scm.acceptInputCommand = { command: 'wasily-git.commitStaged', title: '提交' };
  scm.quickDiffProvider = {
    provideOriginalResource(uri) {
      if (uri.scheme !== 'file') return null;
      const rel = relOf(uri);
      if (!rel || rel.startsWith('..') || rel.indexOf('.git') >= 0) return null;
      return virtualUri(root, rel, 'head');
    }
  };

  registerContentProvider(context);
  historyView = new HistoryProvider();
  iterationsView = new IterationsProvider();
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('wasily-git.history', historyView),
    vscode.window.registerTreeDataProvider('wasily-git.iterations', iterationsView)
  );

  registerCommand(context, 'wasily-git.refresh', async () => {
    await refresh(true);
    historyView.reload();
    iterationsView.reload();
  });
  registerCommand(context, 'wasily-git.stage', stage);
  registerCommand(context, 'wasily-git.unstage', unstage);
  registerCommand(context, 'wasily-git.stageAll', stageAll);
  registerCommand(context, 'wasily-git.unstageAll', unstageAll);
  registerCommand(context, 'wasily-git.openDiff', openDiff);
  registerCommand(context, 'wasily-git.stageHunk', stageHunk);
  registerCommand(context, 'wasily-git.commitStaged', commitStaged);
  registerCommand(context, 'wasily-git.switchBranch', switchBranch);
  registerCommand(context, 'wasily-git.createBranch', createBranch);
  registerCommand(context, 'wasily-git.merge', mergeBranch);
  registerCommand(context, 'wasily-git.clone', cloneRepo);
  registerCommand(context, 'wasily-git.fetch', fetchRemote);
  registerCommand(context, 'wasily-git.pull', pullRemote);
  registerCommand(context, 'wasily-git.push', pushRemote);
  registerCommand(context, 'wasily-git.showHistory', () => historyView.reload());
  registerCommand(context, 'wasily-git.showCommit', showCommit);
  registerCommand(context, 'wasily-git.resetToCommit', resetTo);
  registerCommand(context, 'wasily-git.branchFromCommit', branchFrom);
  registerCommand(context, 'wasily-git.startIteration', startIteration);
  registerCommand(context, 'wasily-git.verifyIteration', verifyIteration);
  registerCommand(context, 'wasily-git.commitIteration', commitIteration);
  registerCommand(context, 'wasily-git.abortIteration', abortIteration);
  registerCommand(context, 'wasily-git.openIteration', openIteration);
  registerCommand(context, 'wasily-git.showIteration', showIteration);
  registerCommand(context, 'wasily-git.showAudit', showAudit);

  const watcher = vscode.workspace.createFileSystemWatcher('**/*');
  const onChange = (uri) => {
    if (!uri || uri.scheme !== 'file') return;
    const fp = uri.fsPath.replace(/\\/g, '/');
    if (/\/node_modules\/|\/\.git\/|__pycache__\/|\/\.git$/.test(fp)) return;
    scheduleRefresh(1500);
  };
  context.subscriptions.push(
    watcher,
    watcher.onDidChange(onChange),
    watcher.onDidCreate(onChange),
    watcher.onDidDelete(onChange),
    vscode.window.onDidChangeWindowState((s) => {
      if (s && s.focused) scheduleRefresh(400);
    })
  );

  guard('initial-refresh', async () => {
    await refresh(false);
    historyView.reload();
    iterationsView.reload();
  });
}

function deactivate() {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = null;
}

module.exports = { activate, deactivate };
