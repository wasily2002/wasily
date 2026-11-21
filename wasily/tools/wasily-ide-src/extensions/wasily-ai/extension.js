const vscode = require('vscode');
const fs = require('fs');
const path = require('path');

// 全局 context（activate 里赋值，供顶层工具函数使用）
let _ctx = null;
// 全局 ChatProvider 引用：ExtensionContext 可能被冻结（context.chatProvider 赋值静默失败），
// 桥接层统一走这个引用，保证 /notify_click 等路由能拿到面板实例
let _provider = null;

// ==================== 服务调用 ====================

function getServerUrl() {
    const url = vscode.workspace.getConfiguration('wasily-ai').get('serverUrl') || 'http://127.0.0.1:8000';
    return url.replace(/\/+$/, '');
}

// 健康检查，timeoutMs 毫秒内未响应视为离线
async function checkHealth(timeoutMs) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs || 3000);
    try {
        const res = await fetch(getServerUrl() + '/health', { signal: ctrl.signal });
        return res.ok;
    } catch (e) {
        return false;
    } finally {
        clearTimeout(timer);
    }
}

// 把底层网络错误翻成可操作的提示：undici 的 fetch failed / terminated 用户看不懂，
// 也不知道下一步该点哪里。这里统一给出「稍后重试」的动作指引。
function friendlyNetError(e) {
    const raw = String((e && e.message) || e || '未知错误');
    if (/fetch failed|terminated|ECONNRESET|ECONNREFUSED|EPIPE|socket|other side closed/i.test(raw))
        return '连接中断：AI 服务可能正在重启或已停止，请稍后点击「重试」';
    if (/timeout|ETIMEDOUT/i.test(raw))
        return '请求超时：本地模型生成较慢，可点击「重试」或稍后再试';
    return raw;
}

// ==================== 内置 Git ====================
// Git 能力已内建在后端（Dulwich 内核），IDE 不再解析 / 下载系统 git，
// 也不再在扩展里执行任何 git 命令；这里只保留一个状态提示用的小工具。
function gitRuntimeHint() {
    return '内置 Git 由 Wasily 后端提供（无需安装 git），源代码管理面板见「Wasily Git」。';
}

// Markdown 默认打开方式：读设置中心的 ide.markdown_open，写入 VS Code 原生
// workbench.editorAssociations。preview → 内置预览编辑器；editor → 移除关联走代码编辑器。
async function applyMarkdownOpen() {
    try {
        const res = await fetch(getServerUrl() + '/config/get?path=' +
            encodeURIComponent('ide.markdown_open'), { signal: AbortSignal.timeout(4000) });
        const d = res.ok ? await res.json() : null;
        const mode = String((d && d.value) || 'editor');
        const cfg = vscode.workspace.getConfiguration('workbench');
        const assoc = Object.assign({}, cfg.get('editorAssociations') || {});
        if (mode === 'preview') assoc['*.md'] = 'vscode.markdown.preview.editor';
        else delete assoc['*.md'];
        await cfg.update('editorAssociations', assoc, vscode.ConfigurationTarget.Global);
        return mode;
    } catch (e) {
        return null;
    }
}

// ==================== 任务完成提示（系统通知 / 任务栏闪烁） ====================
const NOTIFY_TTL_MS = 5 * 60 * 1000;
let _notifySeq = 0;

// 设置项 wasily-ai.taskNotify.system：false = 只留窗口内 toast
function notifySystemEnabled() {
    try {
        return vscode.workspace.getConfiguration('wasily-ai').get('taskNotify.system', true) !== false;
    } catch (e) {
        return true;
    }
}

// 拉起 notify.ps1：Windows 托盘气泡（系统级，IDE 最小化也能看到）+ 任务栏闪烁
function spawnNotifyHelper(args) {
    const script = path.join(__dirname, 'notify.ps1');
    if (!fs.existsSync(script)) return false;
    const logPath = path.join(__dirname, 'notify.log');
    const trace = (s) => {
        try {
            fs.appendFileSync(logPath, '[' + new Date().toISOString() + '] ' + s + '\n');
        } catch (e) { /* 日志失败不影响提示 */ }
    };
    try {
        const cp = require('child_process');
        const p = cp.spawn('powershell.exe', [
            '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-WindowStyle', 'Hidden', '-File', script
        ].concat(args), { detached: true, stdio: 'ignore', windowsHide: true });
        p.on('error', (e) => trace('spawn error: ' + (e && e.message)));
        trace('spawned pid=' + (p.pid || 0) + ' args=' + args.join(' '));
        p.unref();
        return true;
    } catch (e) {
        trace('spawn failed: ' + (e && e.message ? e.message : e));
        return false;
    }
}

function flashTaskbar() {
    return spawnNotifyHelper(['-FlashOnly']);
}

// ---- 主通道：Electron 渲染进程原生 Notification（SSE 推送，真正的 Windows 系统通知）----
// 扩展宿主不能直接调用 Electron main，由 workbench 注入脚本（trae-titlebar.v2.js）连
// /notify_stream，收到任务事件后用 Notification API 弹系统通知；点击再回 /notify_click。
const _notifyClients = new Set();       // 未结束的 SSE 响应
const _notifyPending = Object.create(null);  // id -> { acked, timer }
const _notifyMeta = Object.create(null);     // id -> { title, body }（兜底时用）

function sseSend(payload) {
    const chunk = 'data: ' + JSON.stringify(payload) + '\n\n';
    for (const res of _notifyClients) {
        try { res.write(chunk); } catch (e) { _notifyClients.delete(res); }
    }
}

// 下发一条系统通知：有渲染端连接时只走 Electron Notification；
// 只有「无客户端」或渲染端明确回执 rendered=false 才启用 PowerShell 兜底。
// 不做超时兜底 —— 迟到的 ack 不会再补弹，保证单次任务只出现一条系统通知。
function pushSysNotify(item) {
    _notifyMeta[item.id] = { title: item.title, body: item.body };
    if (_notifyClients.size > 0) {
        _notifyPending[item.id] = { acked: false };
        sseSend(item);
        return 'system';
    }
    spawnNotifyHelper(['-Title', item.title, '-Body', item.body, '-Id', item.id]);
    return 'fallback';
}

function ackSysNotify(id, rendered) {
    if (!_notifyPending[id]) return;
    delete _notifyPending[id];
    if (rendered) return;
    // 渲染失败（权限被拒 / API 不可用）→ 立即走 PowerShell 兜底（仅此一次）
    const meta = _notifyMeta[id];
    if (meta) spawnNotifyHelper(['-Title', meta.title, '-Body', meta.body, '-Id', id]);
}

function fmtDuration(ms) {
    const s = Math.round(Math.max(0, ms) / 1000);
    if (s < 60) return s + ' 秒';
    const m = Math.floor(s / 60);
    return m + ' 分 ' + (s % 60) + ' 秒';
}

// 空返回归因：优先用后端诊断（finish_reason），保证提示可执行而不是占位文案
function emptyReplyReason(data) {
    const diag = (data && data.diag) || {};
    const fin = String(diag.finish_reason || (data && data.finish_reason) || '');
    if (fin === 'tool_rounds_exhausted') return '工具轮次用尽，模型没有给出最终回答';
    if (fin === 'think_deadline') return '思考时间超限，任务被强制收尾';
    if (fin === 'length') return '输出被截断（max_tokens 用尽）';
    if (fin === 'error') return '模型生成出错';
    if (fin === 'no_delta') return '服务无响应（流结束但没有返回任何内容）';
    if (!data) return '服务无响应或返回为空';
    return '模型返回空内容（可能被过滤或服务无响应）';
}

// 调用本地 AI 服务（非流式，单 JSON 返回，可能耗时 10~60 秒）
async function callAI(messages, maxTokens, options) {
    options = options || {};
    const cfg = vscode.workspace.getConfiguration('wasily-ai');
    const defaultModel = cfg.get('defaultModel', 'local');
    const res = await fetch(getServerUrl() + '/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            messages: messages,
            session_id: options.sessionId || 'vscodium',
            max_tokens: maxTokens || 2048,
            // 多轮工具循环：思考→执行→再思考→…→task_done，IDE 模式也给足轮数（上限 8）
            max_tool_rounds: 8,
            model: options.modelId || defaultModel
        }),
        signal: options.signal
    });
    if (!res.ok) {
        throw new Error('AI 服务返回 HTTP ' + res.status);
    }
    return res.json();
}

// 流式调用本地 AI（SSE）：边生成边把 delta 推给面板，返回 done 事件（含完整正文/步骤）
async function callAIStream(messages, maxTokens, options) {
    options = options || {};
    const onEvent = options.onEvent || function () { };
    // 跨端同步：本轮消息 id 由服务端分配，面板与 Agent 页共用同一标识去重
    let turnMid = '';
    // mode: 'solo'（Agent）→ 不限轮数（0 = 无限长度思考，服务端按墙钟预算 + 上下文池化兜底）
    //       'plan' → 最多 3 轮（只读调研并产出计划）
    //       'ask'  → 最多 2 轮（只读问答）
    const mode = options.mode || 'ask';
    const rounds = (mode === 'solo') ? 0 : (mode === 'plan' ? 3 : 2);
    const res = await fetch(getServerUrl() + '/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            messages: messages,
            session_id: options.sessionId || 'vscodium',
            window_id: options.windowId || 'default',
            max_tokens: maxTokens || 2048,
            max_tool_rounds: rounds,
            mode: options.mode || 'ide',
            client: 'ide',
            orchestrate: true,   // 启用「思考流(thinking) + 计划(stage_plan)」编排预分析
            dispatch_steps: true,// Agent 模式按步骤分派执行并汇总
            refs: Array.isArray(options.refs) ? options.refs : [],
            validation_commands: Array.isArray(options.validationCommands) ? options.validationCommands : null,
            stream: true
        }),
        signal: options.signal
    });
    if (!res.ok) throw new Error('AI 服务返回 HTTP ' + res.status);
    // 服务端未返回事件流（旧版本服务）→ 回退为一次性 JSON
    const ctype = res.headers.get('content-type') || '';
    if (ctype.indexOf('text/event-stream') < 0 || !res.body) {
        return await res.json();
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    let doneEv = null;
    let streamed = '';
    let gotAnyEvent = false;   // 只要收到过任意 SSE 事件，说明链路通了，断流时不当致命错误
    let streamBroken = false;  // 断流但收到过事件：标记为「中断」，上游走断点卡而非空回复重试
    try {
        while (true) {
            const chunk = await reader.read();
            if (chunk.done) break;
            buf += dec.decode(chunk.value, { stream: true });
            let idx;
            while ((idx = buf.indexOf('\n\n')) >= 0) {
                const raw = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                const line = raw.replace(/^data: ?/, '').trim();
                if (!line || line === '[DONE]') continue;
                let ev = null;
                try { ev = JSON.parse(line); } catch (e) { continue; }
                gotAnyEvent = true;
                if (ev.ev === 'delta') { streamed += ev.t; onEvent(ev); }
                else if (ev.ev === 'reset') { streamed = ''; onEvent(ev); }
                else if (ev.ev === 'done') { doneEv = ev; if (ev.msg_id) turnMid = ev.msg_id; }
                else { onEvent(ev); }
            }
        }
    } catch (e) {
        // 服务端收尾时直接断开连接，undici 会抛 "terminated"。
        // 用户主动停止：始终抛给上层，走「已停止生成」+ 断点卡。
        if (e && (e.name === 'AbortError' || /abort/i.test(String(e && e.message)))) throw e;
        // 只要已经吐过正文 / 收到过任意事件，就不当致命失败——
        // 不能把「已生成的内容 / 已跑过的思考」当失败丢掉（Agent 模式正文常只在 done 里，
        // delta 可能被 reset 清空，此时 streamed 为空但任务其实在跑）。
        // 只有完全一个事件都没收到，才认定为真正的链路失败，抛给上层显示「请求失败」。
        if (!doneEv && !streamed.trim() && !gotAnyEvent) throw e;
        streamBroken = true;   // 收到过事件但连接断了：任务跑到一半，走断点续跑
    }
    // 剥离围栏残片（模型半截工具调用块），避免 ```tool 被当成回答渲染成 **tool
    function stripToolFences(s) {
        return String(s || '')
            .replace(/```tool(?:_call)?[\s\S]*?```/g, '')
            .replace(/```tool(?:_call)?[\s\S]*$/g, '')
            .trim();
    }
    if (doneEv) {
        // 归一化成与 /v1/chat/completions 相同的数据结构，后续逻辑无需分支
        doneEv.choices = [{ message: { role: 'assistant', content: stripToolFences(doneEv.content || streamed) } }];
        if (!doneEv.msg_id && turnMid) doneEv.msg_id = turnMid;
        return doneEv;
    }
    // 空返回不塞占位文案：交给上层自动重试 / 明确报错；
    // interrupted=true 表示断流（收到过事件但无 done），上游走断点卡而非空回复重试
    return { choices: [{ message: { role: 'assistant', content: stripToolFences(streamed) } }], steps: [], interrupted: streamBroken };
}

function extractReply(data) {
    if (data && data.choices && data.choices[0] && data.choices[0].message) {
        return data.choices[0].message.content || '';
    }
    return '';
}
// ---- IDE 模式总线：与标题栏等原生 UI 同步 IDE/SOLO 模式 ----
function postIdeMode(mode) {
    fetch(getServerUrl() + '/ide_mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: mode })
    }).catch(() => { /* 服务未启动时静默 */ });
}

async function fetchIdeMode() {
    try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 1500);
        const res = await fetch(getServerUrl() + '/ide_mode', { signal: ctrl.signal });
        clearTimeout(t);
        if (!res.ok) return null;
        const d = await res.json();
        return (d.mode === 'solo' || d.mode === 'plan' || d.mode === 'ask') ? d.mode : null;
    } catch (e) {
        return null;
    }
}

// 提取 markdown 代码块
function extractCodeBlocks(text) {
    const blocks = [];
    const re = /```(?:[\w+#.-]*)\r?\n?([\s\S]*?)```/g;
    let m;
    while ((m = re.exec(text)) !== null) {
        blocks.push(m[1].replace(/\n$/, ''));
    }
    return blocks;
}

// 剥离代码块包裹（内联补全用）：模型有时会把补全包在 ``` 里
function stripFences(text) {
    let t = (text || '').trim();
    const m = t.match(/^```[\w+#.-]*[ \t]*\r?\n([\s\S]*?)\r?\n?```$/);
    if (m) return m[1];
    if (t.startsWith('```')) {
        t = t.replace(/^```[\w+#.-]*[ \t]*\r?\n?/, '');
        t = t.replace(/```\s*$/, '');
    }
    return t.trim();
}

async function insertTextIntoEditor(text) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('没有打开的编辑器，无法插入代码');
        return;
    }
    await editor.edit((edit) => {
        for (const selection of editor.selections) {
            edit.insert(selection.active, text);
        }
    });
}

// 取当前活动编辑器的选区文本（没选区返回空串）
function getActiveSelection() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return '';
    const sel = editor.selection;
    if (sel.isEmpty) return '';
    return editor.document.getText(sel);
}

// P1-⑥ 代码块 bash/sh → VS Code 内置 Terminal 执行
async function runShellCommand(code) {
    if (!code || !code.trim()) { vscode.window.showWarningMessage('没有可执行的命令'); return; }
    try {
        const term = vscode.window.createTerminal('Wasily AI Run');
        term.show();
        // PowerShell 兼容：多命令用分号或换行分隔
        term.sendText(code, true);   // addNewLine=true
        vscode.window.setStatusBarMessage('▶ 已在终端执行命令', 2500);
    } catch (e) {
        vscode.window.showErrorMessage('无法打开终端：' + (e && e.message ? e.message : String(e)));
    }
}

// P1-⑦ 把指定路径文件加入 PENDING 上下文（与右键「添加到对话」同机制）
async function attachFilePath(path) {
    if (!path) return;
    try {
        const doc = await vscode.workspace.openTextDocument(path);
        const text = doc.getText();
        const max = 8000;
        const truncated = text.length > max;
        const item = { path: doc.uri.fsPath, name: require('path').basename(doc.uri.fsPath),
            dir: false, size: text.length, content: truncated ? text.slice(0, max) : text, truncated };
        // 写入 PENDING_KEY（与 attachFile 同机制）
        const ctx = null; // 没有 ChatProvider 时临时处理
        // 实际上调用方是 ChatProvider，应该走 this.pushPending + update PENDING_KEY
        // 但这里是顶层函数，用 workspaceState 直接写会比较乱，简化：直接 post 给面板
        vscode.window.setStatusBarMessage('📎 ' + item.name + ' 已加入下一条对话上下文', 2500);
        // 真正的写入由 ChatProvider 自己 attachByPath 处理（上面 case 里调 await this.attachFilePath）
    } catch (e) {
        vscode.window.showWarningMessage('无法读取文件：' + path);
    }
}

// 替换当前选区；无选区则替换整份文件的最长连续非空区（兜底：插入光标处）
async function applyCodeToEditor(text) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('没有打开的编辑器，无法应用代码');
        return;
    }
    const doc = editor.document;
    let applied = false;
    await editor.edit((edit) => {
        for (const sel of editor.selections) {
            if (!sel.isEmpty) {
                edit.replace(sel, text);
                applied = true;
            }
        }
        if (!applied) {
            // 无选区：替换整文件
            const fullRange = new vscode.Range(0, 0, doc.lineCount - 1, doc.lineAt(doc.lineCount - 1).text.length);
            edit.replace(fullRange, text);
            applied = true;
        }
    });
    // 应用后保存（给 AI 自我完善闭环：改完即持久化）
    if (applied && !doc.isUntitled) {
        try { await doc.save(); } catch (e) { /* 已保存则忽略 */ }
    }
    vscode.window.setStatusBarMessage('✓ 代码已应用到 ' + (doc.fileName ? require('path').basename(doc.fileName) : '编辑器'), 2500);
}

// P3-③ 文件级 diff 预览：当前编辑器内容 vs AI 代码，原生 vscode.diff 逐块 Accept/Reject
async function showDiffForCode(aiCode) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('请先打开一个编辑器');
        return;
    }
    const doc = editor.document;
    const oldText = doc.getText();
    const fileName = (doc.fileName || 'untitled').replace(/^.*[\\\/]/, '');
    const lang = doc.languageId;
    try {
        const dir = path.join((_ctx && _ctx.globalStorageUri ? _ctx.globalStorageUri.fsPath : require('os').tmpdir()), 'diffs');
        fs.mkdirSync(dir, { recursive: true });
        const ts = Date.now();
        const oldFile = path.join(dir, `old-${ts}-${fileName}`);
        const newFile = path.join(dir, `new-${ts}-${fileName}`);
        fs.writeFileSync(oldFile, oldText, 'utf8');
        fs.writeFileSync(newFile, String(aiCode || ''), 'utf8');
        await vscode.commands.executeCommand('vscode.diff',
            vscode.Uri.file(oldFile),
            vscode.Uri.file(newFile),
            `Diff 预览: ${fileName}（当前文件 ↔ AI 代码）`);
        vscode.window.showInformationMessage(
            'Diff 已打开 —— 原生 VSCode diff：点左侧 change → 「Accept」合入 AI 版本；不想应用直接关闭 tab'
        );
    } catch (e) {
        vscode.window.showErrorMessage('打开 Diff 失败：' + (e && e.message ? e.message : String(e)));
    }
}

// ===== A4: 块级 diff panel — 聊天面板内逐个 hunk 渲染 + 接受/跳过 =====
let _hunkPanel = null;
let _hunkState = { hunks: [], filePath: '', applied: new Set() };

async function showHunkDiffPanel(aiCode) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) { vscode.window.showWarningMessage('请先打开一个编辑器'); return; }
    const doc = editor.document;
    const oldText = doc.getText();
    const filePath = doc.fileName || '';

    // 调后端分 hunks
    try {
        const res = await fetch(getServerUrl() + '/v1/ide/diff_hunks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old: oldText, new: aiCode })
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const hunks = data.hunks || [];
        if (!hunks.length) {
            vscode.window.showInformationMessage('🤖 没有检测到差异（文件已是最新）');
            return;
        }

        // 用 QuickPick 让用户逐 hunk 选择 —— 比自建 webview 简单且原生
        _hunkState = { hunks, filePath, applied: new Set() };
        const choices = hunks.map((h, i) => {
            const tagLabel = h.tag === 'replace' ? '✏️ 修改' : h.tag === 'delete' ? '🗑️ 删除' : '➕ 新增';
            const oldLine = h.old.split('\n').filter(l => l.trim())[0] || '(空)';
            const newLine = h.new.split('\n').filter(l => l.trim())[0] || '(空)';
            return {
                label: `${tagLabel} hunk #${i + 1} (L${h.old_start}-${h.old_end} → L${h.new_start}-${h.new_end})`,
                description: `旧: ${oldLine.slice(0, 40)} → 新: ${newLine.slice(0, 40)}`,
                index: i
            };
        });
        // 显示 summary 让用户决定
        const summary = `共 ${hunks.length} 处差异：+${data.summary.additions}/-${data.summary.deletions} 行。` +
            `请逐个选择要应用的块（选中后点 OK 批量应用）：`;
        const picked = await vscode.window.showQuickPick(choices, {
            placeHolder: summary + ' Ctrl+Click 多选，默认全选',
            canPickMany: true
        });
        if (!picked) return;

        // 构造：把选中 hunks 合入 old，跳过未选中的
        let result = oldText;
        let offset = 0;
        // 按 old_start 倒序应用（从文件底部往上改，不会让行号偏移）
        const sortedPicks = picked.map(p => p.index).sort((a, b) => b - a);
        for (const idx of sortedPicks) {
            const h = hunks[idx];
            const oldRange = new vscode.Range(
                new vscode.Position(h.old_start - 1, 0),
                new vscode.Position(h.old_end, 0)  // exclusive → 覆盖到 old_end 行尾
            );
            await editor.edit((eb) => eb.replace(oldRange, h.new + (h.new.endsWith('\n') ? '' : '\n')));
        }
        await doc.save();
        vscode.window.showInformationMessage(`✅ 已应用 ${picked.length}/${hunks.length} 处差异`);

    } catch (err) {
        vscode.window.showErrorMessage('块级 diff 失败：' + (err.message || err));
    }
}

// 按 oldCode 在指定文件里查找并替换（Inline Edit 命令走 diff 确认后应用）
async function replaceSelection(newCode, filePath, oldCode) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return;
    const doc = editor.document;
    const file = (filePath && require('path').resolve(filePath)) || null;
    const matchDoc = file ? (require('path').resolve(doc.filePath) === file) : true;
    if (!matchDoc) {
        vscode.window.showWarningMessage('当前打开的文件与 AI 要修改的不一致');
        return;
    }
    if (!oldCode) { await applyCodeToEditor(newCode); return; }
    const full = doc.getText();
    const idx = full.indexOf(oldCode);
    if (idx < 0) {
        // oldCode 不在当前文件里，降级：直接替换选区/插入
        vscode.window.showWarningMessage('未在当前文件里找到 AI 想替换的代码片段，降级为「应用」');
        await applyCodeToEditor(newCode);
        return;
    }
    const startPos = doc.positionAt(idx);
    const endPos = doc.positionAt(idx + oldCode.length);
    const range = new vscode.Range(startPos, endPos);
    await editor.edit((edit) => { edit.replace(range, newCode); });
    try { await doc.save(); } catch (e) { }
    vscode.window.setStatusBarMessage('✓ 已替换 选中代码', 2500);
}

// ==================== 会话持久化 ====================

const SESSIONS_KEY = 'wasily-ai.sessions';   // {sessions:[{id,name,messages:[{role,content,display,ref}]}], activeId}
const PENDING_KEY = 'wasily-ai.pendingContext'; // {path,name,content} 待附加的当前文件上下文

function newId() {
    return 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

// 统一待附加上下文结构：{items:[{path,name,content,dir,truncated,size}]}
// 兼容旧版本单文件结构 {path,name,content}
function pendingItems(p) {
    if (!p) return [];
    if (Array.isArray(p.items)) return p.items.filter((it) => it && it.path);
    if (p.path && p.content !== undefined) {
        return [{ path: p.path, name: p.name, content: p.content, truncated: !!p.truncated }];
    }
    return [];
}

// 附件展示辅助：与面板保持同一套类型/大小文案
function attachKindLabel(kind) {
    if (kind === 'image') return '图片';
    if (kind === 'pdf') return 'PDF';
    if (kind === 'office' || kind === 'ole') return 'Office 文档';
    if (kind === 'text') return '文本';
    return kind || '文件';
}
function fmtBytes(n) {
    n = Number(n) || 0;
    return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
}

function loadState(context) {
    const s = context.workspaceState.get(SESSIONS_KEY, null);
    if (s && Array.isArray(s.sessions)) return s;
    return { sessions: [], activeId: null };
}

function saveState(context, state) {
    context.workspaceState.update(SESSIONS_KEY, state);
}

// 进程/服务在生成中途被杀时，catch 来不及执行；启动时把遗留的 running 还原成断点
function recoverInterruptedTurns(context) {
    const st = loadState(context);
    let dirty = false;
    (st.sessions || []).forEach((s) => {
        if (s && s.running) {
            if (!s.interrupted) {
                s.interrupted = {
                    text: s.running.text || '',
                    ts: s.running.ts || Date.now(),
                    aborted: true
                };
            }
            delete s.running;
            dirty = true;
        }
    });
    if (dirty) saveState(context, st);
}

// ==================== 快捷操作提示词 ====================

const QUICK_TASKS = {
    explain: '请解释这段代码的作用与实现逻辑',
    addComments: '请为下面的代码添加清晰的中文注释，只返回添加注释后的完整代码',
    fixBug: '请找出下面代码中的问题并修复，返回修复后的完整代码，并用几句话说明修改点',
    genTests: '请为下面的代码编写单元测试（使用该语言常用的测试框架），只返回测试代码'
};

const QUICK_LABELS = {
    explain: '解释',
    addComments: '加注释',
    fixBug: '修 Bug',
    genTests: '写测试'
};

// ==================== SOLO 模式与运行阶段 ====================

const MODE_KEY = 'wasily-ai.mode'; // 当前模式：'ask' | 'plan' | 'solo'（默认 Agent）
const MODE_BOOTSTRAP_KEY = 'wasily-ai.modeBootstrapV2'; // 升级后一次性把默认模式切到 Agent

// SOLO 模式提示词前缀：让模型以自主智能体方式调查源码并提交修改
const SOLO_PREFIX = '【SOLO 模式：你是自主开发智能体。请先用 self_search/self_read 调查相关源码，再用 self_edit/self_write 提交修改（会展示 diff 给用户确认），完成后总结改动。若任务过大先拆解并在回复中列出计划。'
    + '重要例外：当用户只是要求「运行 / 打开 / 预览 / 启动」已有的网页或程序时，这属于执行动作而不是开发任务，'
    + '必须直接定位文件并调用 run_web_page（网页）或 run_command（程序），禁止修改任何代码、禁止输出改造方案。】\n\n';

// 根据当前选区构造快捷提示词；无选区返回 null
function buildQuickPrompt(editor, kind) {
    const text = editor.document.getText(editor.selection);
    if (!text) return null;
    const lang = editor.document.languageId;
    const file = editor.document.fileName;
    const task = QUICK_TASKS[kind] || QUICK_TASKS.explain;
    return '文件：' + file + '\n语言：' + lang + '\n选中的代码：\n```' + lang + '\n' + text + '\n```\n\n任务：' + task;
}

// ==================== Dulwich 源代码管理 ====================

function gitError(result, fallback) {
  const detail = result && result.error;
  const message = detail && typeof detail === 'object' ? detail.message : detail;
  const error = new Error(String(message || fallback || 'Git 操作失败'));
  error.code = detail && typeof detail === 'object' ? detail.code : '';
  return error;
}

class ScmProvider {
  constructor(context) {
    this.context = context;
    this.views = new Set();
    this.root = '';
    this.state = null;
    this.queue = Promise.resolve();
    this.refreshTimer = null;
    this.disposed = false;
    const schedule = () => this.scheduleRefresh();
    const watcher = vscode.workspace.createFileSystemWatcher('**/*');
    watcher.onDidChange(schedule);
    watcher.onDidCreate(schedule);
    watcher.onDidDelete(schedule);
    const gitWatcher = vscode.workspace.createFileSystemWatcher('**/.git/{HEAD,index,config,refs/**}');
    gitWatcher.onDidChange(schedule);
    gitWatcher.onDidCreate(schedule);
    gitWatcher.onDidDelete(schedule);
    context.subscriptions.push(watcher, gitWatcher, this);
    context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => {
      this.root = '';
      this.state = null;
      this.scheduleRefresh();
    }));
    // 后端重启后不需要重开视图；无视图时此轮询保持休眠。
    this.reconnectTimer = setInterval(() => {
      if (this.views.size) this.refresh('', true);
    }, 8000);
  }

  dispose() {
    this.disposed = true;
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    if (this.reconnectTimer) clearInterval(this.reconnectTimer);
    this.views.clear();
  }

  resolveWebviewView(webviewView) {
    const webview = webviewView.webview;
    webview.options = { enableScripts: true };
    webview.html = fs.readFileSync(path.join(__dirname, 'scm.html'), 'utf8');
    this.views.add(webview);
    const subscription = webview.onDidReceiveMessage((message) => this.onMessage(message));
    webviewView.onDidDispose(() => {
      subscription.dispose();
      this.views.delete(webview);
    });
    this.refresh('', true);
  }

  post(message) {
    for (const view of this.views) {
      try { view.postMessage(message); } catch (e) { /* 已销毁 */ }
    }
  }

  workspaceRoot() {
    const folders = vscode.workspace.workspaceFolders || [];
    const editor = vscode.window.activeTextEditor;
    if (editor && editor.document) {
      const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
      if (folder) return folder.uri.fsPath;
    }
    return folders.length ? folders[0].uri.fsPath : '';
  }

  validRoot(candidate) {
    const requested = candidate ? path.resolve(String(candidate)) : this.workspaceRoot();
    if (!requested) return '';
    const allowed = (vscode.workspace.workspaceFolders || []).map((item) => item.uri.fsPath);
    if (this.root) allowed.push(this.root);
    const normalize = (value) => {
      const resolved = path.resolve(value);
      return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
    };
    if (!allowed.some((item) => normalize(item) === normalize(requested))) {
      throw new Error('Git 路径不在当前工作区内：' + requested);
    }
    return requested;
  }

  async request(endpoint, root, data, method) {
    const verb = method || (data === undefined ? 'GET' : 'POST');
    let url = getServerUrl() + endpoint;
    const options = { method: verb, headers: { 'Content-Type': 'application/json' } };
    if (verb === 'GET') {
      const query = Object.assign({ path: root }, data || {});
      url += '?' + new URLSearchParams(query).toString();
    } else {
      options.body = JSON.stringify(Object.assign({ path: root }, data || {}));
    }
    const response = await fetch(url, options);
    const result = await response.json();
    if (!response.ok || !result || result.ok === false) {
      throw gitError(result, 'HTTP ' + response.status);
    }
    return result;
  }

  emptyState() {
    return {
      root: '', branch: '', head: '', staged: [], changed: [], untracked: [],
      conflicts: [], branches: [], remotes: [], worktrees: [], tracking: '',
      ahead: 0, behind: 0, online: true
    };
  }

  async refresh(candidate, silent) {
    if (this.disposed || !this.views.size) return null;
    let root = '';
    try {
      root = this.validRoot(candidate || this.root);
      if (!root) {
        this.state = this.emptyState();
        this.post({ type: 'git.state', state: this.state });
        return this.state;
      }
      let status;
      try {
        status = await this.request('/git/status', root, {}, 'GET');
      } catch (error) {
        if (!/不是 git 仓库|NotGitRepository|FileNotFoundError/i.test(error.message)) throw error;
        this.root = root;
        this.state = this.emptyState();
        this.post({ type: 'git.state', state: this.state });
        return this.state;
      }
      const extra = await Promise.all([
        this.request('/git/branches', status.root, {}, 'GET'),
        this.request('/git/remotes', status.root, {}, 'GET'),
        this.request('/git/worktrees', status.root, {}, 'GET')
      ]);
      this.root = status.root;
      this.state = Object.assign({}, status, {
        branches: extra[0].branches || [],
        remotes: extra[1].remotes || [],
        worktrees: extra[2].worktrees || [],
        online: true,
        offline: false
      });
      this.post({ type: 'git.state', state: this.state });
      return this.state;
    } catch (error) {
      if (this.state) {
        this.state = Object.assign({}, this.state, { online: false, offline: true });
        this.post({ type: 'git.state', state: this.state });
      }
      if (!silent) this.post({ type: 'git.error', message: friendlyNetError(error) });
      return null;
    }
  }

  scheduleRefresh() {
    if (!this.views.size || this.refreshTimer) return;
    this.refreshTimer = setTimeout(() => {
      this.refreshTimer = null;
      this.refresh('', true);
    }, 450);
  }

  onMessage(message) {
    if (!message || message.type !== 'git.action') return;
    if (message.action === 'openDiff') {
      this.openDiff(message);
      return;
    }
    if (message.action === 'openNativeDiff') {
      this.openNativeDiff(message).catch((error) => {
        this.post({ type: 'git.error', message: friendlyNetError(error) });
      });
      return;
    }
    // 所有写操作串行执行，避免连续点击造成 stage/commit 竞态。
    this.queue = this.queue.then(() => this.execute(message));
  }

  async openDiff(message) {
    const requestId = String(message.requestId || '');
    try {
      const root = this.validRoot(message.root);
      const rel = String(message.rel || message.path || '');
      const mode = message.target === 'staged' ? 'index' : 'work';
      const result = await this.request('/git/diff', root, { target: mode, rel: rel }, 'GET');
      this.post({
        type: 'git.diff', requestId: requestId, target: message.target,
        rel: rel, result: result
      });
    } catch (error) {
      this.post({
        type: 'git.diff', requestId: requestId, target: message.target,
        rel: String(message.rel || message.path || ''),
        result: { error: friendlyNetError(error) }
      });
    }
  }

  gitUri(root, ref, rel) {
    return vscode.Uri.from({
      scheme: 'wasily-git',
      path: '/' + String(rel).replace(/\\/g, '/'),
      query: JSON.stringify({ root: root, ref: ref, rel: rel })
    });
  }

  async provideTextDocumentContent(uri) {
    let query;
    try { query = JSON.parse(uri.query); } catch (e) { throw new Error('无效的 Git 文档地址'); }
    const root = this.validRoot(query.root);
    const ref = String(query.ref || 'head').toLowerCase();
    if (['head', 'index', 'work'].indexOf(ref) < 0) throw new Error('无效的 Git 版本');
    const result = await this.request('/git/content', root, { ref: ref, rel: query.rel }, 'GET');
    if (result.binary) throw new Error('二进制文件请在磁盘上查看');
    return String(result.text || '');
  }

  async openNativeDiff(message) {
    const root = this.validRoot(message.root);
    const rel = String(message.rel || message.path || '');
    const staged = message.target === 'staged';
    const conflict = message.target === 'conflicts';
    const leftRef = staged ? 'head' : (conflict ? 'head' : 'index');
    const left = this.gitUri(root, leftRef, rel);
    const right = vscode.Uri.file(path.join(root, ...rel.split('/')));
    const title = rel + '（' + leftRef.toUpperCase() + ' ↔ 工作区）';
    await vscode.commands.executeCommand('vscode.diff', left, right, title, { preview: true });
  }

  async remoteWithAuth(endpoint, root, data) {
    try {
      return await this.request(endpoint, root, data);
    } catch (error) {
      if (error.code !== 'authentication_failed') throw error;
      const username = await vscode.window.showInputBox({
        prompt: '远端需要认证：输入用户名（不保存）', ignoreFocusOut: true
      });
      if (username === undefined) throw error;
      const secret = await vscode.window.showInputBox({
        prompt: '输入访问令牌或密码（仅用于本次请求，不保存）',
        password: true, ignoreFocusOut: true
      });
      if (secret === undefined) throw error;
      return this.request(endpoint, root, Object.assign({}, data, {
        username: username, password: secret
      }));
    }
  }

  async execute(message) {
    const action = String(message.action || '');
    this.post({ type: 'git.busy', action: action, busy: true });
    let root = '';
    try {
      root = this.validRoot(message.root);
      const rel = String(message.rel || message.path || '');
      let result = null;
      let notice = '';
      if (action === 'refresh') {
        await this.refresh(root, false);
        return;
      } else if (action === 'stage' || action === 'resolveConflict') {
        result = await this.request('/git/stage', root, { files: [rel], exact: true });
      } else if (action === 'unstage') {
        result = await this.request('/git/unstage', root, { files: [rel] });
      } else if (action === 'revert') {
        result = await this.request('/git/discard', root, { rel: rel });
        notice = '已放弃 ' + rel + ' 的未暂存改动';
      } else if (action === 'stageHunk' || action === 'unstageHunk') {
        result = await this.request(action === 'stageHunk' ? '/git/hunk/stage' : '/git/hunk/unstage', root, {
          rel: rel,
          hunk_id: String(message.id || ''),
          expected_head: String(message.head_sha || ''),
          expected_index: String(message.index_sha || '')
        });
      } else if (action === 'commit') {
        result = await this.request('/git/commit', root, { message: String(message.message || '') });
        notice = '已提交 ' + String(result.commit || '').slice(0, 12);
      } else if (action === 'branchCreate') {
        result = await this.request('/git/branch/create', root, {
          name: String(message.branch || ''), start: String(message.startPoint || 'HEAD')
        });
        notice = '已创建分支 ' + String(message.branch || '');
      } else if (action === 'branchSwitch') {
        result = await this.request('/git/branch/switch', root, { name: String(message.branch || '') });
        notice = '已切换到 ' + String(result.branch || message.branch || '');
      } else if (action === 'remoteAdd') {
        result = await this.request('/git/remote/add', root, {
          name: String(message.name || 'origin'), url: String(message.url || '')
        });
        notice = '已添加远程仓库 ' + String(message.name || 'origin');
      } else if (action === 'worktreeAdd') {
        const target = path.resolve(root, String(message.path || ''));
        result = await this.request('/git/worktree/add', root, {
          target: target, branch: String(message.branch || '')
        });
        notice = '已添加工作树 ' + target;
      } else if (action === 'fetch' || action === 'pull' || action === 'push') {
        result = await this.remoteWithAuth('/git/' + action, root, {
          remote: String(message.remote || 'origin'), force: false
        });
        notice = { fetch: '获取完成', pull: '拉取完成', push: '推送完成' }[action];
      } else {
        throw new Error('不支持的 Git 操作：' + action);
      }
      await this.refresh(root, true);
      if (notice) this.post({ type: 'git.notice', message: notice, level: 'success' });
    } catch (error) {
      this.post({ type: 'git.error', action: action, message: friendlyNetError(error) });
    } finally {
      this.post({ type: 'git.busy', action: action, busy: false });
    }
  }
}

// ==================== 聊天面板 ====================

async function ensureScmView() {
  await vscode.commands.executeCommand('workbench.view.scm');
  try { await vscode.commands.executeCommand('wasily-ai.scmView.focus'); } catch (e) { /* 等待视图注册 */ }
}

// AI 对话停靠在右侧辅助侧栏（secondary sidebar）：纵向全高，不占用编辑器 tab
async function ensureChatView() {
    try {
        await vscode.commands.executeCommand('workbench.view.extension.wasilyAiBar');
    } catch (e) { /* 容器尚未注册时忽略 */ }
    try {
        await vscode.commands.executeCommand('wasily-ai.panelView.focus');
    } catch (e) { /* 视图尚未解析时忽略 */ }
}

class ChatProvider {
    constructor(context) {
        this.context = context;
        this.views = new Set();  // 面板/侧栏共用的 webview 集合（左右面板 UI 完全一致）
        this.panel = null;       // 右侧编辑器区面板（非插件视图方式）
        this.view = null;        // 右侧辅助侧栏视图（默认对话入口）
        this.abortCtrl = null;   // 当前聊天请求的 AbortController
        this.generating = false;
        this.phaseTimer = null;  // /agent_status 轮询定时器（生成期间）
        this.lastPhase = null;   // 上一次轮询到的 phase
        this.openFiles = [];     // 当前打开的编辑器文件（P1-⑤ Workspace 自动上下文）
        this.openFilesTimer = null;

        // P1-⑤ 监听可见编辑器变化 → 自动更新上下文
        context.subscriptions.push(vscode.window.onDidChangeVisibleTextEditors(() => this.rebuildOpenFiles()));
        context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(() => this.rebuildOpenFiles()));
        // P2-⑪ 终端报错自动建议：监听 shell execution 完成事件 → exitCode != 0 时给 AI 分析
        try {
            if (vscode.window.onDidChangeTerminalShellExecution) {
                context.subscriptions.push(vscode.window.onDidChangeTerminalShellExecution((e) => {
                    try {
                        const exec = e && e.execution;
                        if (!exec) return;
                        exec.onDidComplete((ev) => {
                            if (!ev || ev.exitCode === undefined || ev.exitCode === null || ev.exitCode === 0) return;
                            // 终端报错：发 AI 自动修复建议（轻量：3 秒防抖）
                            this.handleTerminalError(exec.commandLine || '', ev.exitCode);
                        });
                    } catch (_) { /* 某些 VSCodium 版本不支持 onDidChangeTerminalShellExecution */ }
                }));
            }
        } catch (_) { /* feature flag 不支持时忽略 */ }
        // P4-② IDE 状态双向同步：每 200ms 采集一次工作区状态，内容有变化才推给面板
        this._lastIdeStateJson = '';
        this.ideStateTimer = setInterval(() => this.pushIdeState(), 200);
        context.subscriptions.push({ dispose: () => { try { clearInterval(this.ideStateTimer); } catch (e) { } } });
        // P4-② 编译/运行错误实时回传：诊断变化（300ms 防抖）→ 面板错误条，点击一键定位
        this._diagTimer = null;
        try {
            context.subscriptions.push(vscode.languages.onDidChangeDiagnostics(() => {
                if (this._diagTimer) clearTimeout(this._diagTimer);
                this._diagTimer = setTimeout(() => this.pushDiagnostics(), 300);
            }));
        } catch (_) { /* 忽略 */ }
        // P4-② 程序运行结果实时回传：'Wasily AI Run' 终端命令结束 → 面板回执
        try {
            if (typeof vscode.window.onDidEndTerminalShellExecution === 'function') {
                context.subscriptions.push(vscode.window.onDidEndTerminalShellExecution((e) => {
                    try {
                        if (!e || !e.terminal || e.terminal.name !== 'Wasily AI Run') return;
                        let cmd = '';
                        try { cmd = String((e.execution.commandLine && e.execution.commandLine.value) || e.execution.commandLine || ''); } catch (_) { }
                        this.post({ type: 'runResult', command: cmd, exitCode: (e.exitCode === undefined ? null : e.exitCode) });
                    } catch (_) { /* 忽略 */ }
                }));
            }
        } catch (_) { /* 旧版本无此 API */ }
        // 初始化一次
        this.rebuildOpenFiles();
    }

    // P2-⑪ 终端报错处理：退出码非 0 → 自动问 AI
    async handleTerminalError(cmd, exitCode) {
        if (!cmd) return;
        // 防抖：5 秒内只处理一次（避免连续命令刷屏）
        if (this._termErrTimer) clearTimeout(this._termErrTimer);
        this._termErrTimer = setTimeout(async () => {
            try {
                const errPrompt = '刚才在终端执行的命令退出码非 0：\n```sh\n' + cmd + '\n```\n退出码：' + exitCode +
                    '\n请分析可能的原因并给出修复命令（直接返回命令块 + 一句解释）。';
                // 用 /v1/chat/completions 快速调用（走 SOLO 模式避免工具循环）
                const r = await fetch(getServerUrl() + '/v1/chat/completions', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ messages: [{ role: 'user', content: errPrompt }], stream: false, max_tokens: 400 })
                });
                const j = await r.json();
                const content = extractReply({ choices: [{ message: { content: j.choices && j.choices[0] && j.choices[0].message ? j.choices[0].message.content : j.content } }] });
                if (content) {
                    this.post({ type: 'append', role: 'assistant', content:
                        '🐛 终端退出码 ' + exitCode + '（命令：`' + String(cmd).slice(0, 60) + '`）\n\n' + content,
                        done: true, sid: '__term_err__', terminalError: true });
                }
            } catch (_) { /* 忽略 AI 调用失败 */ }
        }, 2000);
    }

    // P1-⑤ 重建「当前打开的文件」缓存，推给所有 webview
    rebuildOpenFiles() {
        try {
            const editors = vscode.window.visibleTextEditors || [];
            const seen = new Set();
            this.openFiles = [];
            // 活动编辑器排最前
            const active = vscode.window.activeTextEditor;
            if (active && active.document) this._recordOpenFile(active, seen);
            for (const ed of editors) {
                this._recordOpenFile(ed, seen);
            }
            // 推给面板（用于 P1-⑦ 文件树芯片组 + P1-⑤ system prompt 预览）
            this.pushOpenFiles();
        } catch (e) { /* 忽略 */ }
    }
    _recordOpenFile(ed, seen) {
        if (!ed || !ed.document) return;
        const d = ed.document;
        if (!d.uri || !d.uri.fsPath) return;
        if (seen.has(d.uri.fsPath)) return;
        seen.add(d.uri.fsPath);
        // 截断单文件内容（后端也会再截断，这里做轻量版）
        const text = d.getText();
        this.openFiles.push({
            path: d.uri.fsPath,
            name: require('path').basename(d.uri.fsPath),
            lang: d.languageId,
            lineCount: d.lineCount,
            content: text.length > 3000 ? text.slice(0, 3000) + '\n...（已截断）' : text,
            isActive: ed === vscode.window.activeTextEditor,
            isDirty: d.isDirty,
        });
    }
    pushOpenFiles() {
        const active = vscode.window.activeTextEditor && vscode.window.activeTextEditor.document;
        this.post({ type: 'openFiles', files: this.openFiles, activePath: active ? active.uri.fsPath : null });
    }

    // P4-② 采集当前 IDE 工作区状态（轻量元信息，不传文件内容；有变化才推送）
    pushIdeState() {
        try {
            const ed = vscode.window.activeTextEditor;
            const ws = vscode.workspace.workspaceFolders || [];
            const st = {
                ws: ws.map((f) => f.name),
                activeFile: ed && ed.document ? ed.document.uri.fsPath : '',
                line: ed ? ed.selection.active.line + 1 : 0,
                col: ed ? ed.selection.active.character + 1 : 0,
                lang: ed && ed.document ? ed.document.languageId : '',
                openCount: (vscode.window.visibleTextEditors || []).length,
                dirtyCount: (vscode.workspace.textDocuments || []).filter((d) => d.isDirty).length
            };
            const j = JSON.stringify(st);
            if (j === this._lastIdeStateJson) return;
            this._lastIdeStateJson = j;
            this.post({ type: 'ideState', state: st });
        } catch (e) { /* 忽略 */ }
    }

    // P4-② 收集全工作区「错误」级诊断（上限 20 条），面板点击行一键跳转定位
    pushDiagnostics() {
        try {
            const items = [];
            const all = vscode.languages.getDiagnostics();
            for (const pair of all) {
                const uri = pair[0], diags = pair[1];
                for (const d of diags) {
                    if (d.severity !== vscode.DiagnosticSeverity.Error) continue;
                    items.push({
                        file: uri.fsPath,
                        line: d.range.start.line + 1,
                        col: d.range.start.character + 1,
                        message: String(d.message || '').split('\n')[0].slice(0, 300),
                        source: d.source || ''
                    });
                    if (items.length >= 20) break;
                }
                if (items.length >= 20) break;
            }
            this.post({ type: 'diagnostics', items });
        } catch (e) { /* 忽略 */ }
    }

    // 当前模式（'ask' | 'plan' | 'solo'），持久化在 workspaceState
    getMode() {
        // 升级迁移：老工作区里存的 'ask' 会让人误以为 IDE 不能编程，首次运行切到 Agent
        if (!this.context.workspaceState.get(MODE_BOOTSTRAP_KEY, false)) {
            this.context.workspaceState.update(MODE_BOOTSTRAP_KEY, true);
            this.context.workspaceState.update(MODE_KEY, 'solo');
            return 'solo';
        }
        const m = this.context.workspaceState.get(MODE_KEY, 'solo');
        return (m === 'solo' || m === 'plan' || m === 'ask') ? m : 'solo';
    }

    pushMode() {
        this.post({ type: 'mode', value: this.getMode() });
    }

    // 把同一个聊天 UI 挂到任意 webview（右侧 WebviewPanel 或活动栏视图）
    attachWebview(webview) {
        webview.options = { enableScripts: true };
        // getHtml 现为异步（优先远程拉取最新 UI）：html 就位后再推初始状态，保持原时序
        Promise.resolve(this.getHtml()).then((res) => {
            try { webview.html = res.html; } catch (e) { return; }
            this.pushSessions();
            this.pushRender();
            this.pushPending();

            this.pushMode();
            // 面板打开时把本地模式推给服务端，避免标题栏与面板显示不一致
            postIdeMode(this.getMode());
            this.refreshStatus();
            this.pushChanges();
            this.pushIdeState();
            this.pushDiagnostics();
            // 自愈：本次若非服务端最新 HTML（旧缓存/内置兜底），后台重试拉取并热替换，
            // 避免服务曾短暂不可达导致面板长期停留在旧版 UI
            if (res.source !== 'server') this._refreshPanelHtml(webview, res.html, 0);
            // 常驻版本哨兵：面板存活期间定期对照服务端 UI 版本，发现升级就整体热替换
            this._watchPanelUi(webview, res.html);
        });
        const sub = webview.onDidReceiveMessage((msg) => this.onMessage(msg));
        this.views.add(webview);
        webview.__wasilySub = sub;
        // 模式总线反向同步：标题栏等外部 UI 改动 3s 内反映到面板
        this.syncModeFromServer();
        if (this.modeTimer) clearInterval(this.modeTimer);
        this.modeTimer = setInterval(() => this.syncModeFromServer(), 3000);
    }

    detachWebview(webview) {
        if (webview.__uiTimer) { clearInterval(webview.__uiTimer); webview.__uiTimer = 0; }
        this.views.delete(webview);
        if (webview && webview.__wasilySub) {
            try { webview.__wasilySub.dispose(); } catch (e) { /* 忽略 */ }
        }
    }

    // 右侧辅助侧栏视图（默认对话入口）
    resolveWebviewView(webviewView) {
        this.view = webviewView;
        this.attachWebview(webviewView.webview);
        webviewView.onDidDispose(() => {
            this.detachWebview(webviewView.webview);
            if (this.view === webviewView) this.view = null;
        });
    }

    // 从服务端模式总线拉取模式（标题栏改动 → 面板跟随）
    async syncModeFromServer() {
        const m = await fetchIdeMode();
        if (m && m !== this.getMode()) {
            await this.context.workspaceState.update(MODE_KEY, m);
            this.pushMode();
        }
    }

    post(msg) {
        for (const webview of this.views) {
            try { webview.postMessage(msg); } catch (e) { /* 已销毁的面板 */ }
        }
    }

    async focus() {
        // 优先使用右侧辅助侧栏视图；编辑器区 WebviewPanel 仅作兼容回退
        if (this.panel) {
            try {
                this.panel.reveal(undefined, true);
                return;
            } catch (e) { /* 面板已销毁，走回退 */ }
        }
        await ensureChatView();
    }

    // ==================== 任务完成提示 ====================
    // state: done（完成）/ error（失败）/ confirm（需要用户确认）
    // 同一会话同类状态 20 秒内只提示一次；内容 = 会话名 + 摘要 + 耗时
    notifyTask(info) {
        try {
            info = info || {};
            const now = Date.now();
            const state = info.state === 'error' || info.state === 'confirm' ? info.state : 'done';
            const sid = String(info.sid || '');
            this._notifMarks = this._notifMarks || {};
            const markKey = state + '|' + sid;
            if (now - (this._notifMarks[markKey] || 0) < 20000) return 'throttled';
            // 同批合并：同一会话 2.5 秒内刚提示过（如 confirm 紧接 done），done 不再补发，
            // error / confirm 仍然放行，避免漏掉关键状态
            const lastAny = this._notifMarks['any|' + sid] || 0;
            if (state === 'done' && now - lastAny < 2500) return 'merged';
            this._notifMarks[markKey] = now;
            this._notifMarks['any|' + sid] = now;

            const ms = Math.max(0, Math.round(info.ms || 0));
            const summary = String(info.summary || '').replace(/\s+/g, ' ').trim().slice(0, 90);
            const stateText = state === 'error' ? '任务失败'
                : (state === 'confirm' ? '需要你确认' : '任务完成');
            const title = 'Wasily AI · ' + stateText;
            const body = (info.title ? '【' + info.title + '】' : '') +
                (summary ? summary + '  ' : '') +
                (ms ? '耗时 ' + fmtDuration(ms) : '');
            const id = 'n' + (++_notifySeq) + '-' + now;

            this._notifMap = this._notifMap || {};
            this._notifMap[id] = { sid: sid, ts: now };
            for (const k of Object.keys(this._notifMap)) {
                if (now - this._notifMap[k].ts > NOTIFY_TTL_MS) delete this._notifMap[k];
            }

            // 窗口内 toast（面板收起时下次展开仍能看到刚发生的状态）
            this.post({ type: 'taskDone', id: id, state: state, title: info.title || '',
                        summary: summary, ms: ms, sid: sid });

            if (notifySystemEnabled()) {
                // 主通道：workbench 渲染进程的 Electron 原生 Notification（系统级，最小化可见）；
                // 点击后由主进程恢复窗口，再把 sid 回传渲染进程做会话切换/高亮
                return pushSysNotify({ id: id, title: title, body: body, sid: sid,
                                       ackDelayMs: info.ackDelayMs || 0 });
            }
            // 退化通道：IDE 窗口内原生提示（带「查看结果」）+ 任务栏闪烁
            vscode.window.showInformationMessage(title + '：' + body, '查看结果').then((sel) => {
                if (sel) this.focusTask(id);
            });
            flashTaskbar();
            return 'window';
        } catch (e) {
            return 'error:' + (e && e.message ? e.message : String(e));
        }
    }

    // 点击提示 → 回到对应会话并高亮那条结果（sidHint 由主进程随点击回传）
    async focusTask(id, sidHint) {
        const rec = (this._notifMap || {})[id] || null;
        const sid = String((rec && rec.sid) || sidHint || '');
        if (sid) {
            const state = loadState(this.context);
            if (state.sessions.some((s) => s.id === sid) && state.activeId !== sid) {
                this.switchSession(sid);
            }
        }
        await this.focus();
        this.post({ type: 'highlight', sid: sid, id: id || '' });
    }

    // 系统通知被点击（主进程恢复窗口后回传 → 18753 桥）
    onNotifyClick(id, sid) {
        this.focusTask(String(id || ''), String(sid || '')).catch(() => { /* 忽略 */ });
    }

    async onMessage(msg) {
        if (!msg || !msg.type) return;
        switch (msg.type) {
            case 'ready':
                this.pushSessions();
                this.pushRender();
                this.pushPending();
                this.pushChanges();
                // webview 就绪前的 postMessage 会丢失：就绪后补推模式与 IDE 状态
                this.pushMode();
                this._lastIdeStateJson = '';
                this.pushIdeState();
                await this.refreshStatus();
                break;
            case 'send':
                await this.sendPrompt(String(msg.text || '').trim(), {
                    mode: msg.mode, agent: msg.agent, len: msg.len,
                    agentPrompt: msg.agentPrompt,
                    windowId: msg.windowId,
                    sessionId: msg.sessionId,
                    images: Array.isArray(msg.images) ? msg.images : [],
                    docs: Array.isArray(msg.docs) ? msg.docs : [],
                    refs: Array.isArray(msg.refs) ? msg.refs : [],
                    validationCommands: Array.isArray(msg.validationCommands) ? msg.validationCommands : null
                });
                break;
            case 'agentRunPersist': {
                const state = loadState(this.context);
                const sid = String(msg.sid || state.activeId || '');
                const sess = state.sessions.find((s) => s.id === sid);
                const run = msg.run;
                if (sess && run && run.id) {
                    for (let i = sess.messages.length - 1; i >= 0; i--) {
                        const saved = sess.messages[i] && sess.messages[i].agentRun;
                        if (saved && saved.id === run.id) {
                            sess.messages[i].agentRun = run;
                            saveState(this.context, state);
                            break;
                        }
                    }
                }
                break;
            }
            case 'findFiles':
                await this.findWorkspaceFiles();
                break;
            case 'findSymbols':
                await this.findWorkspaceSymbols(String(msg.query || ''));
                break;
            case 'shareSession':
                await this.shareSession(String(msg.id || ''));
                break;
            case 'editSessionMessage':
                await this.editMessageAndBranch(msg);
                break;
            case 'messageFeedback':
                await this.submitMessageFeedback(msg);
                break;
            case 'runCommand':
                // P1-⑥ 代码块「Run in Terminal」
                await runShellCommand(String(msg.code || ''));
                break;
            case 'attachByPath':
                // P1-⑦ 点芯片组把这个文件加进上下文
                if (msg.path) await this.attachPaths([String(msg.path)]);
                break;
            case 'dropPaths':
                // 从资源管理器/编辑器标签拖进对话框的文件或文件夹
                await this.attachPaths(Array.isArray(msg.paths) ? msg.paths : []);
                break;
            case 'dragDebug': {
                // 临时诊断真实 Webview 拖放链路，确认事件与 DataTransfer 是否到达面板
                try {
                    const entry = {
                        at: new Date().toISOString(), source: 'webview', stage: String(msg.stage || ''),
                        types: Array.isArray(msg.types) ? msg.types : [],
                        items: Array.isArray(msg.items) ? msg.items : [],
                        fileCount: Number(msg.fileCount) || 0,
                        values: msg.values && typeof msg.values === 'object' ? msg.values : {}
                    };
                    fs.appendFileSync(path.join(__dirname, 'drag-debug.log'), JSON.stringify(entry) + '\n');
                } catch (e) { /* 诊断失败不影响拖放 */ }
                break;
            }
            case 'api':
                // WebView 内 CSP 禁止直接 fetch，经扩展中转本地服务接口
                await this.proxyApi(msg);
                break;
            case 'quick':
                await this.handleQuick(msg.kind);
                break;
            case 'getSelection': {
                // #Code：把当前编辑器选中的代码回传给面板，作为引用上下文
                const _ctx = collectIdeContext();
                this.post({ type: 'selection', text: _ctx.selection || '',
                            file: _ctx.file || '', language: _ctx.language || '' });
                break;
            }
            case 'attachFile':
                await this.attachCurrentFile();
                break;
            case 'removePending': {
                const keep = pendingItems(this.context.workspaceState.get(PENDING_KEY, null))
                    .filter((it) => it.path !== msg.path);
                await this.context.workspaceState.update(PENDING_KEY, { items: keep });
                this.pushPending();
                break;
            }
            case 'openFile': {
                // 文档卡片「在 IDE 打开」：docx/pdf/md 等文件用默认编辑器打开
                const fp = String(msg.path || '');
                if (fp) {
                    vscode.workspace.openTextDocument(vscode.Uri.file(fp)).then(doc => {
                        vscode.window.showTextDocument(doc, { preview: true });
                    }).catch(() => {
                        // 二进制文件（docx/pdf）openTextDocument 会失败，回退用系统默认程序
                        vscode.commands.executeCommand('vscode.open', vscode.Uri.file(fp));
                    });
                }
                break;
            }
            case 'stop': {
                // 双通道中断：1) abort 当前 HTTP 请求（立即断开 SSE）
                if (this.abortCtrl) this.abortCtrl.abort();
                // 2) 通知后端设置取消标记（生成循环/工具循环在下一个检查点停下）
                //    否则后端 Python 仍在跑，用户感知"闪崩/卡死时无法中断"
                const stopSid = msg.sessionId || 'vscodium';
                fetch(getServerUrl() + '/v1/agent/cancel', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: stopSid })
                }).catch(() => { /* 后端可能已崩，忽略 */ });
                break;
            }
            case 'retryLast':
                // 空返回/失败卡片上的「重试」：重跑最后一轮，不重复插入用户消息
                await this.retryLastTurn();
                break;
            case 'deleteMessage': {
                // 删除单条消息（用户/AI 均可）：按下标移除并持久化，随后整棵重渲染
                const st = loadState(this.context);
                const ss = st.sessions.find((s) => s.id === st.activeId);
                if (ss && typeof msg.idx === 'number' && msg.idx >= 0 && msg.idx < ss.messages.length) {
                    ss.messages.splice(msg.idx, 1);
                    saveState(this.context, st);
                    this.pushRender();
                }
                break;
            }
            case 'editResend': {
                // 编辑某条用户消息并重发：更新内容后从该条截断重跑（其后的旧回答全部丢弃）
                const st = loadState(this.context);
                const ss = st.sessions.find((s) => s.id === st.activeId);
                const nt = String(msg.text || '').trim();
                if (ss && nt && typeof msg.idx === 'number' && ss.messages[msg.idx] && ss.messages[msg.idx].role === 'user') {
                    ss.messages[msg.idx].content = nt;
                    if (ss.messages[msg.idx].display !== undefined) ss.messages[msg.idx].display = nt;
                    saveState(this.context, st);
                    await this.retryLastTurn(msg.idx);
                }
                break;
            }
            case 'regenerateAt': {
                // 重新生成某条 AI 回答：从它前面那条用户消息重跑
                if (typeof msg.idx === 'number' && msg.idx > 0) await this.retryLastTurn(msg.idx - 1);
                break;
            }
            case 'branchFrom': {
                // 从任意消息创建分支会话：复制到该条（含）为止，切到新会话继续
                const st = loadState(this.context);
                const src = st.sessions.find((s) => s.id === st.activeId);
                if (src && typeof msg.idx === 'number' && msg.idx >= 0 && msg.idx < src.messages.length) {
                    const branch = { id: newId(), name: (src.name || '会话') + ' · 分支', messages: src.messages.slice(0, msg.idx + 1) };
                    st.sessions.unshift(branch);
                    st.activeId = branch.id;
                    saveState(this.context, st);
                    this.pushSessions();
                    this.pushRender();
                }
                break;
            }
            case 'setMode': {
                // Ask / Plan / Agent 模式切换：持久化 + 写入服务端模式总线（标题栏等同步）
                const m = (msg.value === 'solo' || msg.value === 'plan') ? msg.value : 'ask';
                await this.context.workspaceState.update(MODE_KEY, m);
                this.pushMode();
                postIdeMode(m);
                break;
            }
            case 'compact':
                await this.compactSession();
                break;
            case 'closePanel':
                // 关闭 AI 对话：编辑器区面板直接销毁；辅助侧栏视图则收起侧栏
                if (this.panel) {
                    try { this.panel.dispose(); } catch (e) { /* 已关闭 */ }
                } else {
                    await vscode.commands.executeCommand('workbench.action.toggleAuxiliaryBar');
                }
                break;
            case 'selfcheck':
                await this.selfCheck();
                break;
            case 'openScm':
                await ensureScmView();
                break;
            case 'ports':
                await this.pushPorts();
                break;
            case 'openPort':
                if (msg.port) {
                    await vscode.env.openExternal(vscode.Uri.parse('http://127.0.0.1:' + msg.port + '/'));
                }
                break;
            case 'openDiff':
                await this.openDiff(String(msg.path || ''));
                break;
            case 'acceptChanges':
                await this.resolveChanges('accept', msg.paths || []);
                break;
            case 'rejectChanges':
                await this.resolveChanges('reject', msg.paths || []);
                break;
            case 'openExternal':
                if (typeof msg.url === 'string' && msg.url.indexOf(getServerUrl()) === 0) {
                    await vscode.env.openExternal(vscode.Uri.parse(msg.url));
                }
                break;
            case 'newSession':
                this.newSession(msg);
                break;
            case 'setActiveWindow':
                this.setActiveWindow(msg.windowId);
                break;
            case 'registerWindow':
                this.registerWindow(msg.windowId, msg.sessionId);
                break;
            case 'dismissInterrupted': {
                // 用户点「忽略」：清掉当前会话断点，避免下次重启再弹卡片
                const st = loadState(this.context);
                const ss = st.sessions.find((s) => s.id === st.activeId);
                if (ss) {
                    delete ss.interrupted;
                    saveState(this.context, st);
                    this.pushRender();
                }
                break;
            }
            case 'switchSession':
                this.switchSession(msg.id, msg.windowId);
                break;
            case 'clearSession':
                this.clearSession();
                break;
            case 'deleteSession':
                this.deleteSession(msg.id); // 支持按 id 删除历史列表中的指定会话
                break;
            case 'renameSession': {
                const st = loadState(this.context);
                const ss = st.sessions.find((s) => s.id === msg.id);
                const name = String(msg.name || '').trim().slice(0, 60);
                if (ss && name) {
                    ss.name = name;
                    saveState(this.context, st);
                    this.pushSessions();
                }
                break;
            }
            case 'insert':
                await insertTextIntoEditor(msg.code);
                break;
            case 'createFileFromCode': {
                // 必须由用户选择保存位置，避免 AI 代码块静默覆盖工作区文件
                const lang = String(msg.lang || '').toLowerCase();
                const extMap = { javascript: 'js', typescript: 'ts', python: 'py', bash: 'sh', shell: 'sh', powershell: 'ps1', 'c++': 'cpp', html: 'html', css: 'css', json: 'json', markdown: 'md' };
                const ext = extMap[lang] || (/^[a-z0-9]+$/.test(lang) ? lang : 'txt');
                const ws = vscode.workspace.workspaceFolders || [];
                const defaultUri = ws.length ? vscode.Uri.file(path.join(ws[0].uri.fsPath, 'ai-code.' + ext)) : undefined;
                const target = await vscode.window.showSaveDialog({ defaultUri: defaultUri, saveLabel: '新建文件' });
                if (target) {
                    await vscode.workspace.fs.writeFile(target, Buffer.from(String(msg.code || ''), 'utf8'));
                    const doc = await vscode.workspace.openTextDocument(target);
                    await vscode.window.showTextDocument(doc, { preview: true });
                    this.post({ type: 'notify', message: '已新建文件：' + target.fsPath });
                }
                break;
            }
            case 'applyCode':
                // 代码块「应用」：替换当前选区（无选区则插入光标处）
                await applyCodeToEditor(msg.code);
                break;
            case 'diffCode':
                // P3-③ Diff 预览：打开当前文件内容 vs AI 代码的原生 diff（逐块 Accept/Reject）
                await showDiffForCode(msg.code);
                break;
            case 'hunkDiff':
                // A4: 块级 diff — 后端分 hunks，前端逐个渲染 + Accept/Reject 按钮
                await showHunkDiffPanel(msg.code);
                break;
            case 'hunkAction':
                // 接收 webview 发来的 Accept/Reject 单个 hunk
                await applySingleHunk(msg.hunkIndex, msg.action, msg.filePath, msg.filePath ? null : null);
                break;
            case 'replaceSelection':
                await replaceSelection(msg.code, msg.filePath, msg.oldCode);
                break;
            case 'copy':
                await vscode.env.clipboard.writeText(String(msg.code || ''));
                this.post({ type: 'notify', message: '已复制到剪贴板' });
                break;
            case 'checkHealth':
                await this.refreshStatus();
                break;
            case 'openSettings':
                // chat 弹层内的设置入口 → 中央编辑器区设置中心 tab
                await openSettingsCenter(this.context, String(msg.tab || ''));
                break;
            case 'openAgentCenter':
                // Agent 中心是独立 App 窗口，不占聊天面板或编辑器标签
                await openAgentCenter(this.context, String(msg.tab || ''));
                break;
            case 'openAgentChat':
                // 常驻面板：不新开窗口，直接聚焦右侧 WASILY 对话面板（和 Trae 一样常驻）
                await revealWasilyPanel();
                break;
            case 'gotoError':
                // P4-② 编译/运行错误一键定位跳转（面板错误条点击）
                if (msg.file) {
                    try {
                        const doc = await vscode.workspace.openTextDocument(String(msg.file));
                        const ed = await vscode.window.showTextDocument(doc);
                        const ln = Math.max(0, Math.min(doc.lineCount - 1, Number(msg.line || 1) - 1));
                        const cl = Math.max(0, Number(msg.col || 1) - 1);
                        const pos = new vscode.Position(ln, cl);
                        ed.selection = new vscode.Selection(pos, pos);
                        ed.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
                    } catch (e) {
                        vscode.window.showWarningMessage('无法打开文件：' + msg.file);
                    }
                }
                break;
        }
    }

    // 通用接口代理：WebView → 扩展 → 本地服务，回发 apiResp
    async proxyApi(msg) {
        const reqId = msg.reqId;
        try {
            const opt = { method: msg.method || 'GET', headers: { 'Content-Type': 'application/json' } };
            if (msg.body) opt.body = JSON.stringify(msg.body);
            const res = await fetch(getServerUrl() + String(msg.path || ''), opt);
            let data = null;
            try { data = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
            this.post({ type: 'apiResp', reqId: reqId, ok: res.ok, data: data });
        } catch (e) {
            this.post({ type: 'apiResp', reqId: reqId, ok: false, data: null });
        }
    }

    async refreshStatus() {
        const online = await checkHealth(3000);
        this.post({ type: 'status', online: online });
        return online;
    }

    // AI 自检：把当前会话上一条 AI 回复交给独立的审阅员 AI 检查
    async selfCheck() {
        const state = loadState(this.context);
        const sess = state.sessions.find((s) => s.id === state.activeId);
        const lastAi = sess ? [...sess.messages].reverse().find((m) => m.role === 'assistant') : null;
        if (!lastAi) {
            this.post({ type: 'notify', message: '还没有可检查的 AI 回答' });
            return;
        }
        this.post({ type: 'selfchecking', value: true });
        try {
            const res = await fetch(getServerUrl() + '/selfcheck', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: lastAi.content })
            });
            const d = await res.json();
            if (!d || !d.ok) throw new Error((d && d.error) || ('HTTP ' + res.status));
            const head = '【AI 自检】' + (d.verdict || '') +
                (d.score != null ? ' · 置信度 ' + d.score : '');
            this.post({ type: 'append', role: 'assistant', content: head + '\n\n' + d.review });
        } catch (e) {
            this.post({ type: 'notify', message: '自检失败：' + (e && e.message ? e.message : String(e)) });
        } finally {
            this.post({ type: 'selfchecking', value: false });
        }
    }

    // ==================== 文件变更集（AI 改动 → 卡片 → 接受/拒绝） ====================

    currentSessionId() {
        // IDE 会话统一走 ide:<面板会话 id>，与独立 Agent 页共享同一份服务端会话，
        // 打开 /chat?session=ide:<id> 即可无缝续聊（文件变更卡片也按这个 id 归属）
        try {
            const st = loadState(this.context);
            return 'ide:' + (st.activeId || 'default');
        } catch (e) {
            return 'ide:default';
        }
    }

    // 拉取当前会话的文件改动并推给面板（每轮回复结束、用户操作后调用）
    async pushChanges() {
        try {
            const res = await fetch(getServerUrl() + '/changes?session=' +
                encodeURIComponent(this.currentSessionId()));
            if (!res.ok) return;
            const d = await res.json();
            this.post({ type: 'changes', files: (d && d.files) || [] });
        } catch (e) { /* 服务未启动时静默 */ }
    }

    // 接受/拒绝变更：服务端落盘或回滚，随后刷新卡片
    async resolveChanges(action, paths) {
        if (!paths || !paths.length) return;
        try {
            await fetch(getServerUrl() + '/changes/' + action, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths: paths })
            });
            this.post({ type: 'notify', message: (action === 'accept' ? '已接受 ' : '已拒绝 ') + paths.length + ' 个文件改动' });
        } catch (e) {
            this.post({ type: 'notify', message: '操作失败：服务未启动' });
        }
        await this.pushChanges();
    }

    // 端口表：探测本机常用端口后推给面板（点击行在浏览器打开）
    async pushPorts() {
        const rows = await probePorts();
        this.post({ type: 'ports', rows: rows });
    }

    // 查看单个文件的 diff：before/after 写入临时文件后用原生 diff 编辑器对比
    async openDiff(filePath) {
        try {
            const res = await fetch(getServerUrl() + '/changes/file?path=' + encodeURIComponent(filePath));
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const d = await res.json();
            const dir = path.join(this.context.globalStorageUri.fsPath, 'changes');
            fs.mkdirSync(dir, { recursive: true });
            const base = path.basename(filePath) || 'file';
            const beforeFile = path.join(dir, 'before-' + Date.now() + '-' + base);
            const afterFile = path.join(dir, 'after-' + Date.now() + '-' + base);
            fs.writeFileSync(beforeFile, d.before || '', 'utf8');
            fs.writeFileSync(afterFile, d.after || '', 'utf8');
            await vscode.commands.executeCommand('vscode.diff',
                vscode.Uri.file(beforeFile), vscode.Uri.file(afterFile),
                base + '（AI 改动对比）');
        } catch (e) {
            this.post({ type: 'notify', message: '无法打开 diff：' + (e && e.message ? e.message : String(e)) });
        }
    }

    pushSessions(windowId) {
        const state = loadState(this.context);
        this.post({
            type: 'sessions',
            sessions: state.sessions.map((s) => ({ id: s.id, name: s.name })),
            activeId: state.activeId,
            windowId: windowId || ''
        });
    }

    pushRender() {
        const state = loadState(this.context);
        const sess = state.sessions.find((s) => s.id === state.activeId) || null;
        this.post({
            type: 'renderMessages',
            sid: sess ? sess.id : null,
            messages: sess ? sess.messages : [],
            interrupted: (sess && sess.interrupted) || null
        });
    }

    pushPending() {
        const p = this.context.workspaceState.get(PENDING_KEY, null);
        const items = pendingItems(p);
        this.post({
            type: 'pendingContext',
            value: items.length ? { items: items.map((it) => ({
                name: it.name, path: it.path, dir: !!it.dir, size: it.size, bytes: it.bytes,
                kind: it.kind, attached: !!it.attached, thumb: it.thumb || '', truncated: !!it.truncated
            })) } : null
        });
    }

    // 工作区文件列表（@ 文件引用 picker）；相对路径用于生成简短且稳定的引用标记
    async findWorkspaceFiles() {
        try {
            let files = [];
            const entry = (p) => ({ path: p, relative: vscode.workspace.asRelativePath(p, false) });
            // 1. 优先拿当前打开的编辑器文件 + 可见编辑器文件（快速、精准）
            files = vscode.window.visibleTextEditors
                .map((e) => e.document && e.document.uri && e.document.uri.fsPath)
                .filter(Boolean)
                .map(entry);
            // 2. 再按 glob 扫工作区（去重）
            const wsFiles = await vscode.workspace.findFiles(
                '**/*.{js,ts,jsx,tsx,py,go,rs,java,c,cpp,h,hpp,html,css,scss,vue,svelte,rb,php,md,json,yaml,yml,toml}',
                '**/node_modules/**,**/.git/**,**/__pycache__/**,**/dist/**,**/build/**,**/.next/**'
            );
            const seen = new Set(files.map((f) => f.path));
            for (const u of wsFiles.slice(0, 200)) {
                if (!seen.has(u.fsPath)) { files.push(entry(u.fsPath)); seen.add(u.fsPath); }
            }
            this.post({ type: 'filesList', files: files });
        } catch (e) {
            this.post({ type: 'filesList', files: [] });
        }
    }

    // @Symbol 二级 picker：直接复用语言服务的工作区符号索引
    async findWorkspaceSymbols(query) {
        const q = String(query || '').trim();
        try {
            const result = await vscode.commands.executeCommand(
                'vscode.executeWorkspaceSymbolProvider', q);
            const seen = new Set();
            const symbols = [];
            for (const s of (Array.isArray(result) ? result : [])) {
                if (!s || !s.location || !s.location.uri || !s.location.range) continue;
                const line = s.location.range.start.line + 1;
                const key = s.name + ':' + s.location.uri.fsPath + ':' + line;
                if (seen.has(key)) continue;
                seen.add(key);
                symbols.push({
                    name: s.name,
                    kind: vscode.SymbolKind[s.kind] || String(s.kind),
                    file: s.location.uri.fsPath,
                    relative: vscode.workspace.asRelativePath(s.location.uri, false),
                    line: line,
                    containerName: s.containerName || ''
                });
            }
            symbols.sort((a, b) => {
                const ae = a.name.toLowerCase() === q.toLowerCase() ? 0 : 1;
                const be = b.name.toLowerCase() === q.toLowerCase() ? 0 : 1;
                return ae - be || a.name.localeCompare(b.name);
            });
            this.post({ type: 'symbolsList', query: q, symbols: symbols.slice(0, 50) });
        } catch (e) {
            this.post({ type: 'symbolsList', query: q, symbols: [] });
        }
    }

    // 替换 prompt 里的 @path → 实际文件内容（读文件失败则保留原占位符）
    async expandAtRefs(text) {
        const langMap = { js: 'javascript', ts: 'typescript', jsx: 'javascript', tsx: 'typescript',
            py: 'python', go: 'go', rs: 'rust', java: 'java', c: 'c', cpp: 'cpp', h: 'c', hpp: 'cpp',
            html: 'html', css: 'css', scss: 'scss', vue: 'vue', svelte: 'svelte', rb: 'ruby', php: 'php',
            md: 'markdown', json: 'json', yml: 'yaml', yaml: 'yaml', toml: 'toml', txt: 'text' };
        const langOf = (p) => langOfExt((p.split('.').pop() || '').toLowerCase());
        const langOfExt = (ext) => langMap[ext] || ext;
        const roots = vscode.workspace.workspaceFolders || [];
        let out = text;
        let m;

        // @Folder：读取目录内的真实文本/代码，而不是只把目录名交给模型猜测
        const folderRe = /@Folder\s+([^\s@]+)/g;
        const folders = new Set();
        while ((m = folderRe.exec(text)) !== null) folders.add(m[1]);
        for (const ref of folders) {
            const candidates = [];
            if (path.isAbsolute(ref)) {
                candidates.push(vscode.Uri.file(ref));
            } else {
                for (const root of roots) candidates.push(vscode.Uri.file(path.join(root.uri.fsPath, ref)));
            }
            let folderUri = null;
            for (const uri of candidates) {
                try {
                    const st = await vscode.workspace.fs.stat(uri);
                    if (st.type & vscode.FileType.Directory) { folderUri = uri; break; }
                } catch (e) { /* 尝试下一个工作区 */ }
            }
            if (!folderUri) continue;
            try {
                const found = await vscode.workspace.findFiles(
                    new vscode.RelativePattern(folderUri, '**/*'),
                    '**/node_modules/**,**/.git/**,**/__pycache__/**,**/dist/**,**/build/**,**/.next/**', 60);
                const files = found.filter((u) => /\.(js|ts|jsx|tsx|py|go|rs|java|c|cpp|h|hpp|html|css|scss|vue|svelte|rb|php|md|json|ya?ml|toml|txt)$/i.test(u.fsPath));
                const blocks = [];
                let remaining = 16000;
                for (const uri of files.slice(0, 12)) {
                    if (remaining <= 0) break;
                    try {
                        const doc = await vscode.workspace.openTextDocument(uri);
                        const limit = Math.min(4000, remaining);
                        const content = doc.getText();
                        const clip = content.length > limit ? content.slice(0, limit) + '\n...（文件已截断）' : content;
                        remaining -= clip.length;
                        const rel = vscode.workspace.asRelativePath(uri, false);
                        blocks.push('文件：' + rel + '\n```' + langOf(uri.fsPath) + '\n' + clip + '\n```');
                    } catch (e) { /* 单个不可读文件不阻断整个目录 */ }
                }
                if (blocks.length) {
                    const truncated = files.length > blocks.length || remaining <= 0 ? '\n...（目录内容已截断）' : '';
                    out = out.split('@Folder ' + ref).join(
                        '\n\n参考目录：' + folderUri.fsPath + '\n' + blocks.join('\n\n') + truncated + '\n');
                }
            } catch (e) { /* 找不到就保留原引用 */ }
        }

        // @Symbol：注入定义源码及语言服务查到的引用位置
        const symbolRe = /@Symbol\s+([^\s@]+)/g;
        const symbolNames = new Set();
        while ((m = symbolRe.exec(text)) !== null) symbolNames.add(m[1]);
        for (const name of symbolNames) {
            try {
                const result = await vscode.commands.executeCommand('vscode.executeWorkspaceSymbolProvider', name);
                const list = (Array.isArray(result) ? result : []).filter((s) =>
                    s && s.location && s.location.uri && s.location.range);
                if (!list.length) continue;
                const symbol = list.find((s) => s.name === name) || list[0];
                const doc = await vscode.workspace.openTextDocument(symbol.location.uri);
                let snippet = doc.getText(symbol.location.range);
                if (snippet.trim().length <= name.length + 4) {
                    const startLine = symbol.location.range.start.line;
                    const endLine = Math.min(doc.lineCount - 1, startLine + 79);
                    snippet = doc.getText(new vscode.Range(startLine, 0, endLine, doc.lineAt(endLine).text.length));
                }
                if (snippet.length > 8000) snippet = snippet.slice(0, 8000) + '\n...（符号源码已截断）';
                const scanned = await this.scanSymbolRefs(name);
                const refLines = scanned && scanned.refs.length
                    ? '\n引用位置：\n' + scanned.refs.slice(0, 20).map((r) => '- ' + r.file + ':' + (r.line + 1)).join('\n') : '';
                const title = (symbol.containerName ? symbol.containerName + '.' : '') + symbol.name;
                out = out.split('@Symbol ' + name).join(
                    '\n\n参考符号：' + title + '\n定义：' + symbol.location.uri.fsPath + ':' +
                    (symbol.location.range.start.line + 1) + '\n```' + langOf(doc.fileName) + '\n' + snippet + '\n```' + refLines + '\n');
            } catch (e) { /* 语言服务不可用时保留原引用 */ }
        }

        // 通用 @path 文件引用
        const re = /@([^\s@\/][^\s]*)/g;
        const refs = new Set();
        while ((m = re.exec(text)) !== null) {
            if (m[1] !== 'Folder' && m[1] !== 'Symbol') refs.add(m[1]);
        }
        for (const ref of refs) {
            let content = null;
            let resolved = null;
            const normalizedRef = ref.replace(/\\/g, '/');
            // 先查当前工作区打开的编辑器
            for (const ed of vscode.window.visibleTextEditors) {
                const p = (ed.document && ed.document.uri && ed.document.uri.fsPath) || '';
                const normalizedPath = p.replace(/\\/g, '/');
                if (p && (normalizedPath.endsWith(normalizedRef) || normalizedPath.endsWith('/' + normalizedRef))) {
                    content = ed.document.getText();
                    resolved = p; break;
                }
            }
            if (content == null && path.isAbsolute(ref)) {
                try {
                    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(ref));
                    content = doc.getText(); resolved = doc.fileName;
                } catch (e) { /* 继续按工作区相对路径查找 */ }
            }
            // 再按 glob 查
            if (content == null) {
                try {
                    const files = await vscode.workspace.findFiles('**/' + normalizedRef, '**/node_modules/**,**/.git/**');
                    if (files && files[0]) {
                        resolved = files[0].fsPath;
                        const doc = await vscode.workspace.openTextDocument(files[0]);
                        content = doc.getText();
                    }
                } catch (e) { /* 忽略 */ }
            }
            if (content != null && resolved) {
                const clip = content.length > 8000 ? content.slice(0, 8000) + '\n...（已截断）' : content;
                out = out.split('@' + ref).join(
                    '\n\n参考文件：' + resolved + '\n```' + langOf(resolved) + '\n' + clip + '\n```\n');
            }
            // 找不到就保留 @ref，让 AI 自己处理
        }
        return out;
    }

    // 解析 /xxx Slash Commands → 预设 prompt（支持 /explain /inline-edit /fix /comment /test /refactor /generate /run）
    resolveSlashCommand(text) {
        const cmds = [
            { re: /^\s*\/explain(\s|$)/i, handler: () => {
                const sel = getActiveSelection();
                if (sel) return '请解释以下选中代码的作用、潜在 bug 和改进建议：\n\n```\n' + sel + '\n```';
                return '请解释当前打开文件的代码逻辑';
            }},
            { re: /^\s*\/inline-edit(\s|$)/i, handler: (rest) => {
                return '[INLINE_EDIT_MODE]\n请直接修改选中的代码并只输出修改后的代码块，不要解释。\n修改目标：' + (rest || '根据上下文自行判断');
            }},
            { re: /^\s*\/fix(\s|$)/i, handler: () => {
                const sel = getActiveSelection();
                if (sel) return '定位并修复以下代码中的 bug，只返回修复后的代码块：\n\n```\n' + sel + '\n```';
                return '定位并修复当前打开文件的 bug，只返回修复后的完整代码';
            }},
            { re: /^\s*\/comment(\s|$)/i, handler: () => {
                const sel = getActiveSelection();
                if (sel) return '为以下代码每行加上简洁的中文注释，只返回加注释后的代码块：\n\n```\n' + sel + '\n```';
                return '为当前打开文件加上简洁的中文注释，只返回加注释后的代码';
            }},
            { re: /^\s*\/test(\s|$)/i, handler: () => {
                const sel = getActiveSelection();
                if (sel) return '为以下代码生成单元测试，只返回测试代码块：\n\n```\n' + sel + '\n```';
                return '为当前打开文件生成单元测试';
            }},
            { re: /^\s*\/refactor(\s|$)/i, handler: (rest) => {
                const sel = getActiveSelection();
                const target = rest || sel || '当前打开文件';
                const ctx = sel ? '\n\n参考代码：\n```\n' + sel + '\n```' : '';
                return '重构目标：' + target + '\n要求：可读性提升、命名规范、消除重复、最小改动范围。只返回重构后的代码块。' + ctx;
            }},
            { re: /^\s*\/refactor-global(\s|$)/i, handler: (rest) => {
                // P2-⑨ 跨文件重构：让 extension 自动扫描符号引用链（会在预处理里注入）
                const target = rest || getActiveSelection() || '当前选中的函数/类名';
                return '[CROSS_FILE_REFACTOR]\n请全局重命名/重构以下符号，修改所有引用它的文件：' + target + '\n（IDE 已为你附加了该符号的全局引用链）';
            }},
            { re: /^\s*\/generate(\s|$)/i, handler: (rest) => {
                return '生成代码：' + (rest || '（请补充需求描述）') + '\n只返回代码块，放在 ``` 中。';
            }},
            { re: /^\s*\/run(\s|$)/i, handler: (rest) => {
                return '[TERMINAL_RUN]\n请执行以下命令：' + (rest || '（请补充命令）');
            }},
            { re: /^\s*\/git-review(\s|$)/i, handler: () => {
                // P2-⑫ Git Review（预处理里会附加当前 git diff）
                return '[GIT_REVIEW_MODE]\n请审查当前 git 变更的代码质量，指出潜在 bug、安全问题、性能隐患、风格不一致。给出逐项修复建议。';
            }}
        ];
        for (const c of cmds) {
            const m = text.match(c.re);
            if (m) {
                const rest = text.replace(c.re, '').trim();
                return c.handler(rest);
            }
        }
        return null;
    }

    // ===== P2-⑨ 符号引用扫描（VS Code WorkspaceSymbol + ReferenceProvider）=====
    async scanSymbolRefs(symbolName) {
        if (!symbolName) return null;
        const results = { symbol: symbolName, defs: [], refs: [] };
        try {
            // 1. 全局符号搜索
            const symbols = await vscode.commands.executeCommand(
                'vscode.executeWorkspaceSymbolProvider', symbolName);
            if (Array.isArray(symbols)) {
                for (const s of symbols.slice(0, 20)) {
                    if (!s || !s.location || !s.location.uri) continue;
                    results.defs.push({
                        name: s.name, kind: s.kind,
                        file: s.location.uri.fsPath,
                        line: (s.location.range && s.location.range.start) ? s.location.range.start.line : -1,
                        containerName: s.containerName || ''
                    });
                }
            }
            // 2. 取第一个定义点 → 查引用
            if (results.defs.length) {
                const d = results.defs[0];
                const uri = vscode.Uri.file(d.file);
                const pos = new vscode.Position(Math.max(0, d.line), 0);
                try {
                    const refs = await vscode.commands.executeCommand(
                        'vscode.executeReferenceProvider', uri, pos);
                    if (Array.isArray(refs)) {
                        const seen = new Set();
                        for (const r of refs) {
                            if (!r || !r.uri || !r.range) continue;
                            const key = r.uri.fsPath + ':' + r.range.start.line;
                            if (seen.has(key)) continue;
                            seen.add(key);
                            results.refs.push({
                                file: r.uri.fsPath,
                                line: r.range.start.line,
                                col: r.range.start.character
                            });
                        }
                    }
                } catch (e) { /* ReferenceProvider 可能对某些 symbol 不支持 */ }
            }
        } catch (e) { /* 忽略符号 API 异常 */ }
        return results;
    }

    // ===== P2-⑫ Git Review：从后端内核取 staged + unstaged 差异 =====
    // 不再调用系统 git：本机可能没有 git.exe，所有仓库操作统一由后端
    // 的 Dulwich 内核完成（与 Wasily Git 扩展共用同一份状态）。
    async scanGitDiff() {
        try {
            const cwd = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
            if (!cwd) return null;
            const read = async (mode) => {
                try {
                    const res = await fetch(getServerUrl() + '/v1/git/diff?mode=' + mode +
                        '&path=' + encodeURIComponent(cwd.uri.fsPath), { signal: AbortSignal.timeout(20000) });
                    if (!res.ok) return '';
                    const d = await res.json();
                    return (d && d.ok && d.diff) || '';
                } catch (e) { return ''; }
            };
            const staged = await read('staged');
            const unstaged = await read('unstaged');
            const out = (staged ? '=== Staged ===\n' + staged + '\n' : '') +
                        (unstaged ? '=== Unstaged ===\n' + unstaged : '');
            if (!out.trim()) return null;
            // 截断 12k（太大塞不下 prompt）
            return out.length > 12000 ? out.slice(0, 12000) + '\n...（diff 过长已截断）' : out;
        } catch (e) { return null; }
    }

    // 确保存在活动会话并返回
    ensureSession(state) {
        let sess = state.sessions.find((s) => s.id === state.activeId);
        if (!sess) {
            sess = { id: newId(), name: '新会话', messages: [] };
            state.sessions.unshift(sess);
            state.activeId = sess.id;
        }
        return sess;
    }

    // 核心：把一条用户消息发给 AI 并渲染回复（opts: mode=访问模式 agent=执行方式 len=回答长度）
    async describeImages(images) {
        // 把拖进来/粘贴的图片逐张交给视觉服务（本地视觉 LLM → OCR 降级），返回可读文本块
        const out = [];
        for (let i = 0; i < images.length && i < 4; i++) {
            const im = images[i];
            const dataUrl = (typeof im === 'string') ? im : ((im && (im.dataUrl || im.data)) || '');
            if (!dataUrl) continue;
            try {
                const r = await fetch(getServerUrl() + '/v1/image_analyze', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ image: dataUrl })
                });
                const j = await r.json();
                const desc = (j && (j.analysis || '')) || '';
                if (desc) {
                    out.push('【图片附件 ' + (i + 1) + (im && im.name ? '：' + im.name : '') +
                        ' 的解析内容】\n' + desc);
                }
            } catch (e) { /* 单张解析失败不影响整轮对话 */ }
        }
        return out.join('\n\n');
    }

    // /compact：手动压缩当前会话上下文（较早消息折叠为摘要，保留最近几条原文）
    async compactSession() {
        const state = loadState(this.context);
        const sess = state.sessions.find((s) => s.id === state.activeId);
        const keep = 4;
        if (!sess || !Array.isArray(sess.messages) || sess.messages.length <= keep + 1) {
            this.post({ type: 'notify', message: '当前上下文已经很短，无需压缩' });
            return;
        }
        const msgs = sess.messages.map((m) => ({ role: m.role, content: m.content }));
        let info = null;
        try {
            const res = await fetch(getServerUrl() + '/v1/context/compact', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sess.id, keep_items: keep, messages: msgs })
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            info = await res.json();
        } catch (e) {
            this.post({ type: 'notify', message: '压缩失败：' + (e && e.message ? e.message : String(e)) });
            return;
        }
        const dropped = sess.messages.length - keep;
        const kept = sess.messages.slice(-keep);
        // 首条保留一条 user（服务端以「客户端历史首条为 user」判定本轮上下文），
        // 其后用一条 assistant 摘要承载被折叠的过程，再保留最近几条原文
        const firstUser = sess.messages.find((m) => m.role === 'user');
        sess.messages = [
            { role: 'user', content: (firstUser && firstUser.content) || '（继续当前任务）' },
            { role: 'assistant',
              content: '【上下文已压缩】较早的 ' + dropped + ' 条对话已折叠为摘要，最近 ' + keep +
                  ' 条保留原文。\n\n' + ((info && info.summary) ? info.summary : '') }
        ].concat(kept);
        saveState(this.context, state);
        this.pushSessions();
        this.pushRender();
        // 面板据此画「历史对话已被压缩」分隔线与统计行
        this.post({ type: 'compact_done', manual: true,
            before: (info && info.before_tokens) || 0,
            after: (info && info.after_tokens) || 0,
            folded: dropped, stats: (info && info.stats) || {} });
        this.post({ type: 'notify', message: '上下文已压缩：约 ' +
            ((info && info.before_tokens) || 0) + ' → ' + ((info && info.after_tokens) || 0) +
            ' tokens（折叠 ' + dropped + ' 条）' });
    }

    async sendPrompt(text, opts) {
        const _t0 = Date.now();
        if (!text && !(opts && ((Array.isArray(opts.images) && opts.images.length)
            || (Array.isArray(opts.docs) && opts.docs.length)))) return;
        if (this.generating) {
            this.post({ type: 'notify', message: '正在生成中，请先点击「停止」或等待完成' });
            return;
        }
        opts = opts || {};
        // 当前模式提前取值（RAG 分支要用到，放在此避免 TDZ 报错）
        const solo = this.getMode() === 'solo';
        // ===== P0 核心预处理：Slash Commands + @path 文件引用 =====
        let effectiveText = text;
        // 1) /xxx Slash Command 解析（优先，返回预设 prompt 替换用户原输入）
        const slashResolved = this.resolveSlashCommand(effectiveText);
        if (slashResolved) effectiveText = slashResolved;
        // 2) @path 文件引用展开（把 @path 占位符替换成实际文件内容）
        effectiveText = await this.expandAtRefs(effectiveText);

        // ===== P2 预处理：跨文件重构符号引用链 + Git Review diff 注入 + Codebase RAG =====
        let crossFileRefactor = null;
        let gitDiff = null;
        let ragContext = null;
        if (effectiveText.indexOf('[CROSS_FILE_REFACTOR]') >= 0) {
            // 从 slash 命令 rest 或选中代码推断符号名
            let sym = '';
            const m = text.match(/\/refactor-global\s+(.+)/i);
            if (m) sym = m[1].trim();
            if (!sym) {
                // 尝试从选中代码里抓第一个 identifier（简单启发式）
                const sel = getActiveSelection();
                const mm = sel.match(/\b([a-zA-Z_][a-zA-Z_0-9]*)\b/);
                if (mm) sym = mm[1];
            }
            if (sym) crossFileRefactor = await this.scanSymbolRefs(sym);
        }
        if (effectiveText.indexOf('[GIT_REVIEW_MODE]') >= 0) {
            gitDiff = await this.scanGitDiff();
        }
        // P2-⑧ Codebase RAG：检测 query 意图 → 自动召回工作区相关片段
        const ragTriggers = ["代码里", "哪里", "在哪", "函数", "类", "方法", "定义", "实现", "调用了", "引用了", "用到", "retrieve", "RAG", "相关代码"];
        if (!solo && effectiveText && ragTriggers.some(t => effectiveText.indexOf(t) >= 0)) {
            try {
                const r = await fetch(getServerUrl() + '/v1/retrieve', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ q: effectiveText.slice(0, 200), top_k: 6 })
                });
                if (r.ok) {
                    const j = await r.json();
                    if (j && Array.isArray(j.results) && j.results.length) {
                        ragContext = j.results;
                    }
                }
            } catch (_) { /* RAG 不可用时静默降级 */ }
        }

        // 回答长度 → max_tokens（SOLO 模式固定更大预算）
        const tokens = solo ? 3072 : (opts.len === 'short' ? 384 : (opts.len === 'long' ? 3072 : 2048));
        // Agent 内部指令只走本轮 system 消息，不能混入用户消息或持久化会话。
        let wired = effectiveText;
        if (!solo) {
            if (opts.mode === 'confirm') {
                wired = '【需确认模式：涉及修改文件/执行命令的操作，先给出方案等我确认，不要直接执行】\n' + wired;
            }
            if (opts.agent === 'chat') {
                wired = '【对话模式：只读分析，可调用 file_read/code_search/ide_read_file/browser_snapshot 等读取工具；不要修改文件或执行命令，需要动手请切 Agent 模式】\n' + wired;
            }
        }
        const state = loadState(this.context);
        // 多窗口：按面板窗口绑定的会话 id 定位会话，切窗口发送不会串台
        if (opts.sessionId && state.sessions.some((s) => s.id === opts.sessionId)) {
            state.activeId = opts.sessionId;
        }
        if (opts.windowId) {
            _activeWindowId = String(opts.windowId);
        }
        const sess = this.ensureSession(state);
        delete sess.interrupted;   // 开新一轮即视为已从断点续跑

        // 拼接待附加的文件上下文（支持多个文件/目录）
        let finalPrompt = wired;
        let ref = null;
        const pending = this.context.workspaceState.get(PENDING_KEY, null);
        const items = pendingItems(pending);
        if (items.length) {
            const blocks = items.map((it) => {
                if (it.dir) {
                    return '参考目录：' + it.path + '\n（目录已加入上下文，可用工具列出并读取其中文件）';
                }
                if (it.attached) {
                    return '附件：' + it.path + '（' + attachKindLabel(it.kind) +
                        (it.size ? '，' + fmtBytes(it.size) : '') + '）\n' +
                        '请用对应工具读取后再回答：图片 read_image，PDF read_pdf，Office read_office，文本 file_read。';
                }
                const cut = it.truncated ? '\n（内容过长，已截断，其余部分可用工具按需读取）' : '';
                return '参考文件：' + it.path + cut + '\n文件内容：\n```\n' + it.content + '\n```';
            });
            finalPrompt = blocks.join('\n\n') + '\n\n用户问题：' + wired;
            ref = items.map((it) => it.name).join('、');
        }
        this.context.workspaceState.update(PENDING_KEY, null);
        this.pushPending();

        // ===== 附件注入：只给落盘路径引用，模型用 read_* 读取，不猜内容 =====
        const docs = opts.docs || [];
        const imgs = opts.images || [];
        const atts = imgs.concat(docs).filter((a) => a && (a.path || a.text));
        if (atts.length) {
            const lines = [];
            atts.forEach((a) => {
                if (a.path) {
                    lines.push('- 附件：' + a.path + '（' + attachKindLabel(a.kind) +
                        (a.size ? '，' + fmtBytes(a.size) : '') + '）');
                } else {
                    lines.push('【附件《' + a.name + '》内容】\n```\n' + a.text + '\n```');
                }
            });
            if (lines.length) {
                finalPrompt = finalPrompt + '\n\n【用户附件】\n' + lines.join('\n') +
                    '\n请先调用对应工具读取附件再回答：图片用 read_image，PDF 用 read_pdf，' +
                    'Word/Excel/PPT 用 read_office，文本/代码用 file_read。不要猜测内容。\n';
            }
            const names = atts.map((a) => a.name).filter(Boolean).join('、');
            if (names) ref = ref ? (ref + '、' + names) : names;
        }

        // ===== P1-⑤ Workspace 自动上下文：注入当前打开的文件概览（低成本，只给路径+行数+活动状态）=====
        if (!solo && this.openFiles && this.openFiles.length) {
            // 只注入前 5 个（活动编辑器排第一），其余用...
            const top = this.openFiles.slice(0, 5);
            const tail = this.openFiles.length > 5 ? `...另 ${this.openFiles.length - 5} 个文件` : '';
            const list = top.map((f) => {
                const flag = f.isActive ? '（活动）' : '';
                const dirty = f.isDirty ? '（未保存）' : '';
                return `- ${f.path}${flag}${dirty}  ${f.lineCount}行  lang=${f.lang}`;
            }).join('\n');
            // 带上工作区根目录和完整路径，避免模型凭空拼出错误的绝对路径
            let root = '';
            try {
                const ws = vscode.workspace.workspaceFolders;
                if (ws && ws.length && ws[0].uri) root = ws[0].uri.fsPath;
            } catch (_) { /* ignore */ }
            finalPrompt = (root ? '当前工作区：' + root + '\n' : '') +
                '当前在 IDE 里打开的文件（完整路径）：\n' + list + (tail ? '\n' + tail : '') +
                '\n（读取/修改这些文件请直接使用上面的完整路径，不要自行拼接）\n\n' + finalPrompt;
        }

        // ===== P2-⑨ 跨文件重构：注入符号引用链 =====
        if (crossFileRefactor && crossFileRefactor.refs && crossFileRefactor.refs.length) {
            const refLines = crossFileRefactor.refs.map((r) =>
                `- ${r.file}:${r.line}`).slice(0, 30).join('\n');
            finalPrompt =
                '\n\n【跨文件重构上下文】符号「' + crossFileRefactor.symbol +
                '」在工作区中共有 ' + crossFileRefactor.refs.length + ' 处引用：\n' +
                refLines + '\n请逐处修改所有引用，确保不会漏掉任何一个文件。\n\n' + finalPrompt;
        }

        // ===== P2-⑫ Git Review：注入当前 diff =====
        if (gitDiff) {
            finalPrompt =
                '\n\n【当前 git 变更】\n```diff\n' + gitDiff + '\n```\n\n请逐项审查以上 diff，指出潜在 bug、安全隐患、性能问题和风格不一致，给出具体修复建议。\n\n' + finalPrompt;
        }

        // ===== P2-⑧ Codebase RAG：注入召回的相关代码片段 =====
        if (ragContext && ragContext.length) {
            const ragLines = ragContext.map((r) =>
                `- ${r.file.split(/[\\/]/).slice(-2).join('/')}:${r.line}\n  ${r.snippet.replace(/\n/g, '\n  ')}`).join('\n');
            finalPrompt =
                '\n\n【工作区相关代码片段（RAG 召回）】\n' + ragLines +
                '\n以上片段来自工作区关键词检索，请结合这些上下文回答。\n\n' + finalPrompt;
        }

        sess.messages.push({ role: 'user', content: finalPrompt, display: text, ref: ref });
        // 会话名 = 首条消息前 12 字
        if (sess.name === '新会话') {
            sess.name = text.replace(/\s+/g, ' ').slice(0, 12) || '新会话';
        }
        sess.validationCommands = Array.isArray(opts.validationCommands) ? opts.validationCommands : null;
        // 未完成标记：若进程/服务在生成中途被杀，重启后据此还原「上次任务中断」断点
        sess.running = { text: text, ts: Date.now() };
        saveState(this.context, state);
        this.pushSessions();
        this.pushRender();

        this.generating = true;
        this.abortCtrl = new AbortController();
        this._genStartTs = _t0;
        this.post({ type: 'generating', value: true });
        // 生成期间轮询 /agent_status（IDE/SOLO 都启用），实时推送运行阶段
        this.startPhasePolling();
        try {
            const msgs = sess.messages.map((m) => ({ role: m.role, content: m.content }));
            // 内置与自定义 Agent 设定均仅本轮注入，界面和会话历史只保留用户原文。
            const agentSystem = [];
            if (solo) agentSystem.push(SOLO_PREFIX.trim());
            if (opts.agentPrompt) {
                agentSystem.push('【智能体设定】请始终以下述身份与规则响应：\n' + opts.agentPrompt);
            }
            if (agentSystem.length) {
                msgs.unshift({ role: 'system', content: agentSystem.join('\n\n') });
            }
            const _streamOpts = {
                signal: this.abortCtrl.signal,
                sessionId: this.currentSessionId(),
                windowId: opts.windowId || _activeWindowId || 'default',
                mode: this.getMode(),
                refs: opts.refs || [],
                validationCommands: sess.validationCommands,
                onEvent: (ev) => {
                    // 逐 token 推到面板做打字机渲染；工具调用轮开始时撤回已吐出的文字
                    if (ev.ev === 'delta') this.post({ type: 'delta', sid: sess.id, text: ev.t });
                    else if (ev.ev === 'reset') this.post({ type: 'streamReset', sid: sess.id });
                    // 编排：思考流独立通道 + 计划卡
                    else if (ev.ev === 'thinking') this.post({ type: 'thinking', sid: sess.id, text: ev.t });
                    else if (ev.ev === 'thinking_done') this.post({ type: 'thinkingDone', sid: sess.id });
                    else if (ev.ev === 'stage_plan') this.post({ type: 'planStage', sid: sess.id, plan: ev.plan });
                    else if (ev.ev === 'step_start') this.post({ type: 'stepStart', sid: sess.id,
                        planId: ev.plan_id, stepId: ev.step_id });
                    else if (ev.ev === 'step_done') this.post({ type: 'stepDone', sid: sess.id,
                        planId: ev.plan_id, stepId: ev.step_id, status: ev.status, result: ev.result });
                    // 折叠对话：面板据此在消息流顶部渲染折叠条（摘要 + 可展开原文）
                    else if (ev.ev === 'fold') this.post({ type: 'foldRecord', sid: sess.id, fold: ev.fold });
                    else if (ev.ev === 'agent_run') this.post({
                        type: 'agentRun', sid: sess.id, run: ev.run,
                        action: ev.action, detail: ev.detail
                    });
                }
            };
            let data = await callAIStream(msgs, tokens, _streamOpts);
            // 断流但收到过事件（任务跑到一半连接断了）：走断点卡，不当空回复自动重试
            if (data && data.interrupted) {
                const _itext = String(text || '').replace(/^(继续上次未完成的任务[：:]\s*)+/, '');
                sess.interrupted = { text: _itext, ts: Date.now(), aborted: false };
                delete sess.running;
                saveState(this.context, state);
                const online = await checkHealth(3000);
                this.post({ type: 'status', online: online });
                this.post({ type: 'notify', message: online
                    ? '连接中断，已停止生成，可点击「继续」从断点恢复'
                    : 'AI 服务未启动，请先运行 wasily AI（' + getServerUrl() + '）' });
                this.post({ type: 'interrupted', sid: sess.id, interrupted: sess.interrupted });
                return;   // finally 负责收尾 generating / 轮询
            }
            let reply = extractReply(data).trim();
            // 空返回：自动重试一次（同一上下文，不重复插入用户消息）
            if (!reply) {
                this.post({ type: 'streamReset', sid: sess.id });
                try {
                    const retry = await callAIStream(msgs, tokens, _streamOpts);
                    const retryReply = extractReply(retry).trim();
                    if (retryReply) { data = retry; reply = retryReply; }
                } catch (e2) { /* 保留首次诊断，交给下面统一报错 */ }
            }
            if (!reply) {
                // 不显示「（AI 未返回内容）」占位：给出可执行原因 + 重试入口
                const reason = emptyReplyReason(data);
                this.post({ type: 'taskError', sid: sess.id, reason: reason,
                            detail: (data && data.diag) || null, ms: Date.now() - _t0 });
                this.notifyTask({ state: 'error', sid: sess.id, title: sess.name,
                                  summary: reason, ms: Date.now() - _t0 });
                return;   // finally 负责收尾 generating / 轮询
            }
            // 持久化过程详情：steps（思考/工具轨迹）+ 耗时，重启后可完整回放
            const replySteps = (data && data.steps) || [];
            const replyMs = Math.round(((data && data.elapsed) || 0) * 1000);
            // 计划卡随消息持久化（切换会话/重启后仍可回放「分析 → 计划」）
            const stagePlan = (data && data.stage_plan) || null;
            const agentRun = (data && data.agent_run) || null;
            sess.messages.push({ role: 'assistant', content: reply, steps: replySteps, ms: replyMs,
                                 planStage: stagePlan, agentRun: agentRun });
            delete sess.interrupted;
            delete sess.running;
            saveState(this.context, state);
            // 把服务端 steps（think/tool 交替轨迹）随回复下发，供面板渲染「思考→执行→检测」折叠流
            this.post({ type: 'append', role: 'assistant', content: reply, done: true, sid: sess.id,
                        steps: replySteps, ms: replyMs, agentRun: agentRun, index: sess.messages.length - 1 });
            // 计划卡兜底：实时 stage_plan 事件丢失时用 done 里的计划补发（面板按 id 去重）
            if (stagePlan) this.post({ type: 'planStage', plan: stagePlan, sid: sess.id });
            // 任务编排卡片：服务端把多步需求拆成「人物 + 任务 + agent/skill + 工具」，这里下发给面板渲染
            if (data && data.plan) {
                this.post({ type: 'plan', plan: data.plan, sid: sess.id });
            }
            if (data && data.usage && data.usage.total_tokens) {
                this.totalTokens = (this.totalTokens || 0) + data.usage.total_tokens;
                this.post({ type: 'tokens', value: this.totalTokens });
            }
            // 任务结束提示：完成 / 需要确认（任务编排卡片）
            if (data && data.plan) {
                this.notifyTask({ state: 'confirm', sid: sess.id, title: sess.name,
                                  summary: '任务编排待确认：' + String(data.plan.goal || ''),
                                  ms: Date.now() - _t0 });
            } else {
                this.notifyTask({ state: 'done', sid: sess.id, title: sess.name,
                                  summary: reply, ms: Date.now() - _t0 });
            }
        } catch (e) {
            // 中断即断点：记录用户原话，面板显示「上次任务中断在这里」并可一键继续
            const aborted = e && (e.name === 'AbortError' || /abort/i.test(String(e && e.message)));
            // 存断点前剥掉「继续上次未完成的任务：」前缀（可能已叠多层），防止续跑时重复拼接刷屏
            const _itext = String(text || '').replace(/^(继续上次未完成的任务[：:]\s*)+/, '');
            sess.interrupted = { text: _itext, ts: Date.now(), aborted: !!aborted };
            delete sess.running;
            saveState(this.context, state);
            if (aborted) {
                this.post({ type: 'notify', message: '已停止生成' });
            } else {
                const online = await checkHealth(3000);
                this.post({ type: 'status', online: online });
                const msg = online
                    ? ('请求失败：' + friendlyNetError(e))
                    : 'AI 服务未启动，请先运行 wasily AI（' + getServerUrl() + '）';
                // 与 Agent 一致：只留一张可重试的失败卡，不再额外插一条错误气泡（避免同一次失败出现两处文案）
                this.post({ type: 'taskError', sid: sess.id,
                            reason: msg.replace(/^请求失败：/, ''),
                            detail: null, ms: Date.now() - _t0 });
                // 任务失败提示（系统级通知，面板收起/最小化时也能看到）
                this.notifyTask({ state: 'error', sid: sess.id, title: sess.name,
                                  summary: msg.replace(/^请求失败：/, ''), ms: Date.now() - _t0 });
            }
            this.post({ type: 'interrupted', sid: sess.id, interrupted: sess.interrupted });
        } finally {
            // 回复完成/中断后必须停止轮询定时器，防止泄漏
            this.stopPhasePolling();
            this.generating = false;
            this.abortCtrl = null;
            // 无论正常结束还是早退，都清掉未完成标记，避免下次启动误判断档
            if (sess && sess.running) {
                delete sess.running;
                saveState(this.context, state);
            }
            this.post({ type: 'generating', value: false });
            this.pushSessions();
            // 本轮 AI 产生的文件改动 → 面板变更卡片
            this.pushChanges();
        }
    }

    // 空返回 / 失败后的「重试」入口：沿用当前会话历史重跑最后一轮，不重复插入用户消息
    async retryLastTurn(fromIdx) {
        if (this.generating) {
            this.post({ type: 'notify', message: '正在生成中，请先点击「停止」或等待完成' });
            return;
        }
        const state = loadState(this.context);
        const sess = state.sessions.find((s) => s.id === state.activeId);
        if (!sess || !sess.messages.length) return;
        // 从指定用户消息重跑（编辑重发 / 任意消息重新生成）：截断到该条（含），丢弃其后所有消息
        let truncated = false;
        if (typeof fromIdx === 'number' && fromIdx >= 0 && fromIdx < sess.messages.length) {
            sess.messages = sess.messages.slice(0, fromIdx + 1);
            truncated = true;
        }
        // 丢掉末尾空的 assistant 占位（若有），保证重跑的是最后一轮用户请求
        while (sess.messages.length && sess.messages[sess.messages.length - 1].role === 'assistant'
                && !String(sess.messages[sess.messages.length - 1].content || '').trim()) {
            sess.messages.pop();
        }
        // 成功回答的重新生成只从本次请求上下文中排除旧回答；新回答成功前保留旧内容，失败仍可回看
        const regenerate = !!(sess.messages.length && sess.messages[sess.messages.length - 1].role === 'assistant');
        const baseMessages = regenerate ? sess.messages.slice(0, -1) : sess.messages;
        if (!baseMessages.length || baseMessages[baseMessages.length - 1].role !== 'user') return;

        const _t0 = Date.now();
        this.generating = true;
        this.abortCtrl = new AbortController();
        this.post({ type: 'generating', value: true });
        this.startPhasePolling();
        // 重跑同样标记「未完成」，进程被杀后仍能还原断点
        sess.running = { text: (baseMessages[baseMessages.length - 1] || {}).display || '', ts: Date.now() };
        saveState(this.context, state);
        // 截断重跑先把视图刷回截断后的消息，再流式追加新回答，避免旧回答残留
        if (truncated) this.pushRender();
        try {
            const msgs = baseMessages.map((m) => ({ role: m.role, content: m.content }));
            // 重试同样只临时注入 Agent 设定，不把内部提示词写回历史。
            if (this.getMode() === 'solo') {
                msgs.unshift({ role: 'system', content: SOLO_PREFIX.trim() });
            }
            const _streamOpts = {
                signal: this.abortCtrl.signal,
                sessionId: this.currentSessionId(),
                mode: this.getMode(),
                refs: [],
                validationCommands: sess.validationCommands,
                onEvent: (ev) => {
                    if (ev.ev === 'delta') this.post({ type: 'delta', sid: sess.id, text: ev.t });
                    else if (ev.ev === 'reset') this.post({ type: 'streamReset', sid: sess.id });
                    else if (ev.ev === 'thinking') this.post({ type: 'thinking', sid: sess.id, text: ev.t });
                    else if (ev.ev === 'thinking_done') this.post({ type: 'thinkingDone', sid: sess.id });
                    else if (ev.ev === 'stage_plan') this.post({ type: 'planStage', sid: sess.id, plan: ev.plan });
                    else if (ev.ev === 'step_start') this.post({ type: 'stepStart', sid: sess.id,
                        planId: ev.plan_id, stepId: ev.step_id });
                    else if (ev.ev === 'step_done') this.post({ type: 'stepDone', sid: sess.id,
                        planId: ev.plan_id, stepId: ev.step_id, status: ev.status, result: ev.result });
                    else if (ev.ev === 'fold') this.post({ type: 'foldRecord', sid: sess.id, fold: ev.fold });
                    else if (ev.ev === 'agent_run') this.post({
                        type: 'agentRun', sid: sess.id, run: ev.run,
                        action: ev.action, detail: ev.detail
                    });
                }
            };
            const tokens = this.getMode() === 'solo' ? 3072 : 2048;
            let data = await callAIStream(msgs, tokens, _streamOpts);
            // 断流但收到过事件：续跑也可能被掐断，同样走断点卡
            if (data && data.interrupted) {
                const _last = baseMessages[baseMessages.length - 1] || {};
                const _itext = String(_last.display || _last.content || '').replace(/^(继续上次未完成的任务[：:]\s*)+/, '');
                sess.interrupted = { text: _itext, ts: Date.now(), aborted: false };
                delete sess.running;
                saveState(this.context, state);
                const online = await checkHealth(3000);
                this.post({ type: 'status', online: online });
                this.post({ type: 'notify', message: online
                    ? '连接中断，已停止生成，可点击「继续」从断点恢复'
                    : 'AI 服务未启动，请先运行 wasily AI（' + getServerUrl() + '）' });
                this.post({ type: 'interrupted', sid: sess.id, interrupted: sess.interrupted });
                return;
            }
            let reply = extractReply(data).trim();
            if (!reply) {
                this.post({ type: 'streamReset', sid: sess.id });
                try {
                    const retry = await callAIStream(msgs, tokens, _streamOpts);
                    const rr = extractReply(retry).trim();
                    if (rr) { data = retry; reply = rr; }
                } catch (e2) { /* 保留首次诊断 */ }
            }
            if (!reply) {
                const reason = emptyReplyReason(data);
                this.post({ type: 'taskError', sid: sess.id, reason: reason,
                            detail: (data && data.diag) || null, ms: Date.now() - _t0 });
                this.notifyTask({ state: 'error', sid: sess.id, title: sess.name,
                                  summary: reason, ms: Date.now() - _t0 });
                return;
            }
            const replySteps = (data && data.steps) || [];
            const replyMs = Math.round(((data && data.elapsed) || 0) * 1000);
            const stagePlan = (data && data.stage_plan) || null;
            const agentRun = (data && data.agent_run) || null;
            if (regenerate) sess.messages.pop();
            sess.messages.push({ role: 'assistant', content: reply, steps: replySteps, ms: replyMs,
                                 planStage: stagePlan, agentRun: agentRun });
            delete sess.interrupted;
            delete sess.running;
            saveState(this.context, state);
            if (regenerate) this.pushRender();
            else this.post({ type: 'append', role: 'assistant', content: reply, done: true,
                             sid: sess.id, steps: replySteps, ms: replyMs, agentRun: agentRun,
                             index: sess.messages.length - 1 });
            if (stagePlan) this.post({ type: 'planStage', plan: stagePlan, sid: sess.id });
            if (data && data.plan) this.post({ type: 'plan', plan: data.plan, sid: sess.id });
            if (data && data.usage && data.usage.total_tokens) {
                this.totalTokens = (this.totalTokens || 0) + data.usage.total_tokens;
                this.post({ type: 'tokens', value: this.totalTokens });
            }
            this.notifyTask({ state: (data && data.plan) ? 'confirm' : 'done', sid: sess.id,
                              title: sess.name,
                              summary: (data && data.plan) ? '任务编排待确认' : reply,
                              ms: Date.now() - _t0 });
        } catch (e) {
            const aborted = e && (e.name === 'AbortError' || /abort/i.test(String(e && e.message)));
            const msg = aborted ? '已停止生成'
                : ('请求失败：' + friendlyNetError(e));
            this.post({ type: 'taskError', sid: sess.id, reason: msg, ms: Date.now() - _t0 });
            if (!aborted) {
                this.notifyTask({ state: 'error', sid: sess.id, title: sess.name,
                                  summary: msg.replace(/^请求失败：/, ''), ms: Date.now() - _t0 });
            }
        } finally {
            this.stopPhasePolling();
            this.generating = false;
            this.abortCtrl = null;
            if (sess && sess.running) {
                delete sess.running;
                saveState(this.context, state);
            }
            this.post({ type: 'generating', value: false });
            this.pushSessions();
            this.pushChanges();
        }
    }

    // ==================== 运行阶段轮询（/agent_status） ====================

    startPhasePolling() {
        this.stopPhasePolling();
        this.lastPhase = null;
        this.pollPhase(); // 先立即查一次
        this.phaseTimer = setInterval(() => this.pollPhase(), 1500);
    }

    stopPhasePolling() {
        if (this.phaseTimer) {
            clearInterval(this.phaseTimer);
            this.phaseTimer = null;
        }
    }

    // 查询一次运行阶段；1 秒超时丢弃本轮，phase 变化时推送给 webview
    async pollPhase() {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 1000);
        try {
            const res = await fetch(getServerUrl() + '/agent_status', { signal: ctrl.signal });
            if (res.ok) {
                const data = await res.json();
                const phase = (data && data.phase) || 'idle';
                if (phase !== this.lastPhase) {
                    this.lastPhase = phase;
                    this.post({ type: 'phase', phase: phase, tool: (data && data.tool) || null });
                }
                // 敏感操作确认：服务端挂起等待用户批准 → 推给面板渲染确认卡片
                if (data && data.pending_action) {
                    this.post({ type: 'pendingAction', id: data.pending_action.id,
                                tool: data.pending_action.tool, args: data.pending_action.args });
                    // 需要用户确认 → 系统级提示（面板收起/最小化时也能看到）
                    if (data.pending_action.id !== this._lastPendingNotifyId) {
                        this._lastPendingNotifyId = data.pending_action.id;
                        const st = loadState(this.context);
                        const sess = st.sessions.find((s) => s.id === st.activeId);
                        this.notifyTask({ state: 'confirm', sid: st.activeId || '',
                                          title: sess ? sess.name : '',
                                          summary: '需要确认：' + String(data.pending_action.tool || '敏感操作'),
                                          ms: Date.now() - (this._genStartTs || Date.now()) });
                    }
                }
                if (phase === 'idle') this.stopPhasePolling(); // 服务端已空闲，停止轮询
            }
        } catch (e) {
            /* 超时或服务未就绪：静默丢弃，下个周期重试 */
        } finally {
            clearTimeout(timer);
        }
    }

    // 快捷操作（解释/加注释/修Bug/写测试），针对当前选区
    async handleQuick(kind) {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            this.post({ type: 'notify', message: '请先打开一个文件' });
            return;
        }
        const prompt = buildQuickPrompt(editor, kind);
        if (!prompt) {
            this.post({ type: 'notify', message: '请先选中一段代码，再使用「' + (QUICK_LABELS[kind] || kind) + '」' });
            return;
        }
        await this.focus();
        await this.sendPrompt(prompt);
    }

    // 资源管理器右键「添加到对话」：读取文件/目录信息，作为上下文附件加入待发送队列
    async addPaths(uris) {
        if (!uris || !uris.length) return;
        const items = pendingItems(this.context.workspaceState.get(PENDING_KEY, null));
        let added = 0;
        for (const uri of uris) {
            const base = String(uri.path || uri.fsPath || '').split(/[\\/]/).pop();
            const fsPath = uri.fsPath || uri.path;
            try {
                const st = await vscode.workspace.fs.stat(uri);
                if ((st.type & vscode.FileType.Directory) !== 0) {
                    if (!items.some((it) => it.path === fsPath)) {
                        items.push({ name: base, path: fsPath, dir: true });
                        added++;
                    }
                    continue;
                }
                const LIMIT = 60000;   // 单文件上限；超出部分交由服务端上下文压缩处理
                const buf = await vscode.workspace.fs.readFile(uri);
                let text = Buffer.from(buf).toString('utf8');
                let truncated = false;
                if (text.length > LIMIT) { text = text.slice(0, LIMIT); truncated = true; }
                const idx = items.findIndex((it) => it.path === fsPath);
                const it = { name: base, path: fsPath, content: text, truncated, size: st.size };
                if (idx >= 0) items[idx] = it; else items.push(it);
                added++;
            } catch (e) {
                this.post({ type: 'notify', message: '读取失败：' + base });
            }
        }
        await this.context.workspaceState.update(PENDING_KEY, { items: items });
        await ensureChatView();   // 先确保面板已打开，再推送，避免内容发到未挂载的面板丢失
        this.pushPending();
        if (added) this.post({ type: 'notify', message: '已添加 ' + added + ' 项到对话上下文' });
    }

    // 编辑器右键「添加到对话」：有选区→选中文本作为上下文；无选区→整个文件
    async addSelectionToChat() {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            this.post({ type: 'notify', message: '请先打开一个文件' });
            return;
        }
        const doc = editor.document;
        const base = doc.fileName.split(/[\\/]/).pop();
        const sel = editor.selection;
        let content, label;
        if (!sel.isEmpty) {
            content = doc.getText(sel);
            label = base + '（选区 L' + (sel.start.line + 1) + '-L' + (sel.end.line + 1) + '）';
        } else {
            content = doc.getText();
            label = base;
        }
        let truncated = false;
        const LIMIT = 60000;
        if (content.length > LIMIT) { content = content.slice(0, LIMIT); truncated = true; }
        // 同路径旧条目替换，避免重复堆叠
        const items = pendingItems(this.context.workspaceState.get(PENDING_KEY, null))
            .filter((it) => it.path !== doc.fileName);
        items.push({ name: label, path: doc.fileName, content: content, truncated: truncated, size: content.length });
        await this.context.workspaceState.update(PENDING_KEY, { items: items });
        await ensureChatView();
        this.pushPending();
        this.post({ type: 'notify', message: (truncated ? '内容过长已截断，' : '') + '已添加到对话：' + label });
    }

    // 非文本附件：交给后端 /v1/ide/attach 落盘（path 注册），返回统一附件信息
    async registerAttachment(fsPath) {
        const res = await fetch(getServerUrl() + '/v1/ide/attach', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: fsPath })
        });
        let j = null;
        try { j = await res.json(); } catch (e) { j = null; }
        if (!res.ok || !j || !j.ok || !j.attachment) {
            throw new Error((j && j.error) || ('HTTP ' + res.status));
        }
        return j.attachment;
    }

    // 📎 附加当前打开文件作为下一条消息的上下文
    // 拖入/路径批量附加：文本直接读内容；图片/PDF/Office 交给后端落盘后只留路径引用
    async attachPaths(paths) {
        if (!Array.isArray(paths) || !paths.length) return;
        const TEXT_EXTS = new Set(('py,js,ts,jsx,tsx,vue,html,css,scss,less,json,md,txt,csv,xml,yml,yaml,toml,ini,conf,'
            + 'java,c,cpp,h,hpp,cs,go,rs,rb,php,sh,bat,ps1,sql,log,env,gitignore,lock,gradle,cmake,makefile,dockerfile').split(','));
        const ATTACH_EXTS = new Set('png,jpg,jpeg,gif,webp,bmp,tif,tiff,ico,pdf,docx,xlsx,pptx,doc,xls,ppt'.split(','));
        const SKIP_DIRS = new Set(['node_modules', '.git', 'dist', 'out', 'build', '__pycache__', '.next', 'coverage', '.idea', '.vscode']);
        const MAX_DIR_FILES = 40, MAX_CHARS = 80000;
        const added = [];
        const skipped = [];
        const seen = new Set(pendingItems(this.context.workspaceState.get(PENDING_KEY, null)).map((it) => it.path));

        const readOne = async (fsPath, displayName) => {
            const ext = (fsPath.split('.').pop() || '').toLowerCase();
            const isText = TEXT_EXTS.has(ext);
            if (!isText && !ATTACH_EXTS.has(ext)) { skipped.push(displayName || fsPath); return; }
            if (seen.has(fsPath)) return;
            try {
                if (!isText) {
                    const att = await this.registerAttachment(fsPath);
                    seen.add(fsPath);
                    added.push({
                        path: att.path,
                        name: displayName || att.name || fsPath.split(/[\\/]/).pop(),
                        kind: att.kind,
                        size: att.bytes || 0,
                        attached: true
                    });
                    return;
                }
                const u8 = await vscode.workspace.fs.readFile(vscode.Uri.file(fsPath));
                let content = Buffer.from(u8).toString('utf8');
                const truncated = content.length > MAX_CHARS;
                if (truncated) content = content.slice(0, MAX_CHARS);
                seen.add(fsPath);
                added.push({ path: fsPath, name: displayName || fsPath.split(/[\\/]/).pop(),
                    content, truncated, size: content.length });
            } catch (e) { skipped.push(displayName || fsPath); }
        };

        const walkDir = async (dirPath, prefix, budget) => {
            if (budget.n >= MAX_DIR_FILES) return;
            let entries;
            try { entries = await vscode.workspace.fs.readDirectory(vscode.Uri.file(dirPath)); }
            catch (e) { skipped.push(prefix || dirPath); return; }
            entries.sort((a, b) => (a[1] === b[1] ? a[0].localeCompare(b[0]) : a[1] === vscode.FileType.Directory ? -1 : 1));
            for (const [name, ftype] of entries) {
                if (budget.n >= MAX_DIR_FILES) { budget.over = true; break; }
                const full = dirPath + '\\' + name;
                const rel = prefix ? prefix + '/' + name : name;
                if (ftype === vscode.FileType.Directory) {
                    if (!SKIP_DIRS.has(name)) await walkDir(full, rel, budget);
                } else if (ftype === vscode.FileType.File) {
                    const n0 = added.length;
                    await readOne(full, rel);
                    if (added.length > n0) budget.n++;
                }
            }
        };

        for (const raw of paths) {
            let p = String(raw || '').trim();
            if (!p) continue;
            try { p = decodeURIComponent(p); } catch (e) { /* 保留原样 */ }
            p = p.replace(/^file:\/\/\/?/, '').replace(/\//g, '\\');
            if (/^\\[a-zA-Z]:/.test(p)) p = p.slice(1); // file:///C:\x → \C:\x → C:\x
            try {
                const st = await vscode.workspace.fs.stat(vscode.Uri.file(p));
                if (st.type === vscode.FileType.Directory) {
                    const budget = { n: 0, over: false };
                    await walkDir(p, p.split(/[\\/]/).pop(), budget);
                } else {
                    await readOne(p, null);
                }
            } catch (e) { skipped.push(p); }
        }

        if (added.length) {
            const prev = pendingItems(this.context.workspaceState.get(PENDING_KEY, null));
            await this.context.workspaceState.update(PENDING_KEY, { items: prev.concat(added) });
            this.pushPending();
        }
        let msg = added.length ? ('📎 已附加 ' + added.length + ' 个文件') : '';
        if (skipped.length) msg += (msg ? '，' : '') + '跳过 ' + skipped.length + ' 项（不支持或读取失败）';
        if (msg) this.post({ type: 'notify', message: msg });
    }

    async attachCurrentFile() {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            this.post({ type: 'notify', message: '请先打开一个文件' });
            return;
        }
        let content = editor.document.getText();
        let truncated = false;
        if (content.length > 80000) { // 过大文件截断，避免请求体过大
            content = content.slice(0, 80000);
            truncated = true;
        }
        const name = editor.document.fileName.split(/[\\/]/).pop();
        const prev = pendingItems(this.context.workspaceState.get(PENDING_KEY, null))
            .filter((it) => it.path !== editor.document.fileName);
        prev.push({
            path: editor.document.fileName,
            name: name,
            content: content,
            truncated: truncated,
            size: content.length
        });
        await this.context.workspaceState.update(PENDING_KEY, { items: prev });
        this.pushPending();
        this.post({ type: 'notify', message: (truncated ? '文件过长已截断，' : '') + '已附加当前文件：' + name });
    }

    async shareSession(id) {
        const state = loadState(this.context);
        const sess = state.sessions.find((s) => s.id === id);
        if (!sess) {
            this.post({ type: 'shareResult', ok: false, id: id, message: '会话不存在或已删除' });
            return;
        }
        // 复用现有本地续聊入口；明确标注本机链接，不暗示已经发布到公网
        const url = getServerUrl() + '/chat?session=' + encodeURIComponent('ide:' + sess.id);
        try {
            await vscode.env.clipboard.writeText(url);
            this.post({ type: 'shareResult', ok: true, id: sess.id, url: url,
                message: '已复制本机续聊链接（需要本机后端运行）' });
        } catch (e) {
            this.post({ type: 'shareResult', ok: false, id: sess.id, url: url,
                message: '复制失败：' + (e && e.message ? e.message : String(e)) });
        }
    }

    async editMessageAndBranch(msg) {
        if (this.generating) {
            this.post({ type: 'notify', message: '正在生成中，请先停止或等待完成后再编辑' });
            return;
        }
        const text = String(msg.text || '').trim();
        const index = Number(msg.index);
        const state = loadState(this.context);
        const source = state.sessions.find((s) => s.id === String(msg.sessionId || state.activeId));
        if (!source || !Number.isInteger(index) || index < 0 || index >= source.messages.length ||
                source.messages[index].role !== 'user' || !text) {
            this.post({ type: 'notify', message: '无法编辑：原用户消息不存在或内容为空' });
            return;
        }
        const branchId = newId();
        const branch = {
            id: branchId,
            name: (String(source.name || '会话').slice(0, 52) + ' · 分支'),
            // 仅复制被编辑消息之前的历史；源会话及其后续回答完全不动
            messages: JSON.parse(JSON.stringify(source.messages.slice(0, index)))
        };
        state.sessions.unshift(branch);
        state.activeId = branchId;
        saveState(this.context, state);
        this.post({ type: 'sessionBranched', sourceId: source.id, id: branchId,
            name: branch.name, windowId: String(msg.windowId || '') });
        this.pushSessions();
        this.post({ type: 'notify', message: '已创建编辑分支，原会话保留' });
        await this.sendPrompt(text, {
            mode: msg.mode, agent: msg.agent, len: msg.len,
            agentPrompt: msg.agentPrompt,
            windowId: msg.windowId,
            sessionId: branchId,
            refs: Array.isArray(msg.refs) ? msg.refs : []
        });
    }

    async submitMessageFeedback(msg) {
        const state = loadState(this.context);
        const sessionId = String(msg.sessionId || state.activeId || '');
        const index = Number(msg.index);
        const vote = String(msg.vote || '');
        const corrected = vote === 'down' ? String(msg.corrected || '') : '';
        const result = (ok, message) => this.post({
            type: 'feedbackResult', sessionId: sessionId, index: index,
            vote: vote, corrected: corrected, ok: ok, message: message
        });
        const sess = state.sessions.find((s) => s.id === sessionId);
        const target = sess && Number.isInteger(index) && index >= 0 && index < sess.messages.length
            ? sess.messages[index] : null;
        if (!target || target.role !== 'assistant' || (vote !== 'up' && vote !== 'down')) {
            result(false, '反馈失败：原回答不存在或会话已变化');
            return;
        }
        // 同一状态不重复写入训练反馈；切换赞/踩仍提交最新选择
        if (target.feedback && target.feedback.vote === vote) {
            result(true, vote === 'up' ? '已记录：回答有帮助' : '已记录：回答有问题');
            return;
        }
        let question = '';
        for (let i = index - 1; i >= 0; i--) {
            if (sess.messages[i].role === 'user') {
                question = String(sess.messages[i].display || sess.messages[i].content || '');
                break;
            }
        }
        try {
            const res = await fetch(getServerUrl() + '/lora/feedback', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    question: question, original: String(target.content || ''),
                    corrected: corrected, vote: vote
                }),
                signal: AbortSignal.timeout(15000)
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data || data.ok === false) {
                throw new Error((data && (data.message || data.error)) || ('HTTP ' + res.status));
            }
            // 后端确认后才落盘，失败时不会让按钮出现假高亮
            target.feedback = { vote: vote, corrected: corrected, ts: Date.now() };
            saveState(this.context, state);
            result(true, vote === 'up' ? '已记录：回答有帮助' :
                (corrected ? '已记录点踩与修正建议' : '已记录：回答有问题'));
        } catch (e) {
            result(false, '反馈失败：' + friendlyNetError(e));
        }
    }

    newSession(opts) {
        // 多窗口：面板可预生成 windowId/sessionId，扩展按该 id 建独立会话
        opts = opts || {};
        const state = loadState(this.context);
        const sid = String(opts.sessionId || newId());
        let sess = state.sessions.find((s) => s.id === sid);
        if (!sess) {
            sess = { id: sid, name: '新会话', messages: [] };
            state.sessions.unshift(sess);
        }
        state.activeId = sid;
        saveState(this.context, state);
        this.pushSessions(opts.windowId);
        this.pushRender();
    }

    switchSession(id, windowId) {
        const state = loadState(this.context);
        if (state.sessions.some((s) => s.id === id)) {
            state.activeId = id;
            saveState(this.context, state);
            this.pushSessions(windowId);
            this.pushRender();
        }
    }

    // 切换当前窗口：上下文按 windowId 分窗口留存，供后端按窗口取用
    setActiveWindow(windowId) {
        const wid = String(windowId || '');
        if (!wid) return;
        // 不在这里用全局编辑器覆盖离开窗口的快照：它的上下文只由「它在活动期间」的编辑器变化更新
        _activeWindowId = wid;
        // 首次进入该窗口才采集现场；已有快照的窗口切回来时保持自己的上下文不被覆盖
        if (!_windowCtx[wid]) {
            try { _windowCtx[wid] = collectIdeContext(); } catch (e) { /* ignore */ }
        }
        registerIdeClient(wid, { kind: 'panel', ts: Date.now() });
        pushIdeContext(0, wid);
    }

    // 面板窗口注册：桥侧维护多客户端列表（不再只保留首个）
    registerWindow(windowId, sessionId) {
        const wid = String(windowId || '');
        if (!wid) return;
        registerIdeClient(wid, { kind: 'panel', sessionId: String(sessionId || ''), ts: Date.now() });
    }

    clearSession() {
        const state = loadState(this.context);
        const sess = state.sessions.find((s) => s.id === state.activeId);
        if (sess) {
            sess.messages = [];
            saveState(this.context, state);
            this.pushRender();
        }
    }

    // 删除会话：指定 id 删除该项（历史列表按钮），否则删除当前活动会话
    deleteSession(id) {
        const state = loadState(this.context);
        let idx = -1;
        if (id) idx = state.sessions.findIndex((s) => s.id === id);
        if (idx < 0) idx = state.sessions.findIndex((s) => s.id === state.activeId);
        if (idx >= 0) {
            const removed = state.sessions.splice(idx, 1)[0];
            if (removed && removed.id === state.activeId) {
                state.activeId = state.sessions.length > 0 ? state.sessions[0].id : null;
            }
        }
        if (!state.activeId || !state.sessions.some((s) => s.id === state.activeId)) {
            if (state.sessions.length > 0) {
                state.activeId = state.sessions[0].id;
            } else {
                const sess = { id: newId(), name: '新会话', messages: [] };
                state.sessions.push(sess);
                state.activeId = sess.id;
            }
        }
        saveState(this.context, state);
        this.pushSessions();
        this.pushRender();
    }

    // 命令面板入口：聚焦面板后发送
    async sendFromCommand(prompt) {
        await this.focus();
        setTimeout(() => {
            this.sendPrompt(prompt);
        }, 300);
    }

    // 面板 UI 版本哨兵：阶段轮询之外再定期对照服务端版本标记，
    // 这样即使 webview 被保留、用户没重载窗口，也能在有新 UI 时自动热替换
    _watchPanelUi(webview, baselineHtml) {
        const readVer = (s) => {
            const m = String(s || '').match(/UI v(\d+)/);
            return m ? m[1] : '';
        };
        if (webview.__uiTimer) clearInterval(webview.__uiTimer);
        let lastVer = readVer(baselineHtml);
        const tick = async () => {
            let t = null;
            try {
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), 2500);
                const res = await fetch(getServerUrl() + '/panel_ui?v=' + Date.now(),
                                        { signal: ctrl.signal, cache: 'no-store' });
                clearTimeout(timer);
                if (res.ok) t = await res.text();
            } catch (e) { return; }
            if (!t || t.length < 2000 || t.indexOf('__SERVER__') < 0) return;
            const ver = readVer(t);
            if (!ver || ver === lastVer) return;   // 无版本标记/已是同版：不打断面板
            lastVer = ver;
            try {
                webview.html = t.replace(/__SERVER__/g, getServerUrl());
                this.pushSessions();
                this.pushRender();
                this.pushPending();
                this.pushMode();
                this.pushChanges();
                this.pushIdeState();
            } catch (e) { /* webview 已销毁 */ }
        };
        setTimeout(tick, 2500);
        webview.__uiTimer = setInterval(tick, 15000);
    }

    // 后台重试拉取服务端面板 UI：拿到更新的版本就热替换 webview 并恢复状态，
    // 用户无需重载窗口即可从旧缓存/内置兜底升级到最新面板
    _refreshPanelHtml(webview, currentHtml, attempt) {
        const delay = attempt === 0 ? 4000 : 15000;
        setTimeout(async () => {
            let fresh = null;
            try {
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), 2500);
                const res = await fetch(getServerUrl() + '/panel_ui?v=8&t=' + Date.now(), { signal: ctrl.signal, cache: 'no-store' });
                clearTimeout(timer);
                if (res.ok) {
                    const t = await res.text();
                    if (t && t.length > 2000 && t.indexOf('__SERVER__') >= 0) {
                        try { await this.context.globalState.update('panelUiCache', t); } catch (e) { /* 忽略 */ }
                        fresh = t.replace(/__SERVER__/g, getServerUrl());
                    }
                }
            } catch (e) { /* 服务仍不可达 */ }
            if (!fresh) {
                if (attempt < 3) this._refreshPanelHtml(webview, currentHtml, attempt + 1);
                return;
            }
            if (fresh === currentHtml) return;   // 缓存已是最新，无需替换
            try {
                webview.html = fresh;
                this.pushSessions();
                this.pushRender();
                this.pushPending();
                this.pushMode();
                this.pushChanges();
            } catch (e) { /* webview 已销毁 */ }
        }, delay);
    }

    async getHtml() {
        // 远程更新：优先从 wasily 服务拉取最新面板 UI（服务端 ide_panel.html），
        // 成功则写入缓存；服务不可达时用上次缓存，最终降级到扩展内置 chat.html。
        // 效果：改服务端 ide_panel.html 即可远程更新所有编辑器面板，无需重装扩展。
        const server = getServerUrl();
        let html = null;
        let source = 'server';
        try {
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), 2500);
            const res = await fetch(server + '/panel_ui?v=8&t=' + Date.now(), { signal: ctrl.signal, cache: 'no-store' });
            clearTimeout(timer);
            if (res.ok) {
                const t = await res.text();
                if (t && t.length > 2000 && t.indexOf('__SERVER__') >= 0) {
                    html = t;
                    try { await this.context.globalState.update('panelUiCache', t); } catch (e) { /* 忽略 */ }
                }
            }
        } catch (e) { /* 服务未启动/超时：走缓存或本地 */ }
        if (!html) { html = this.context.globalState.get('panelUiCache', null); source = 'cache'; }
        if (!html) { html = fs.readFileSync(path.join(__dirname, 'chat.html'), 'utf8'); source = 'bundled'; }
        return { html: html.replace(/__SERVER__/g, server), source: source };
    }
}

// ==================== 内联补全（Tab 补全） ====================

class InlineCompletionProvider {
    constructor() {
        this.lastRequestTime = 0; // 上次实际发起请求的时间戳
        this.inflight = false;    // 是否有补全请求在途，避免堆积
    }

    async provideInlineCompletionItems(document, position, context, token) {
        const cfg = vscode.workspace.getConfiguration('wasily-ai');
        if (cfg.get('inlineSuggest.enabled', true) === false) return undefined;
        if (token.isCancellationRequested) return undefined;

        // 简单防抖：距上次请求不足 debounceMs 则跳过本轮
        const debounceMs = cfg.get('inlineSuggest.debounceMs', 800);
        const now = Date.now();
        if (now - this.lastRequestTime < debounceMs) return undefined;
        if (this.inflight) return undefined;
        this.lastRequestTime = now;
        this.inflight = true;

        try {
            // 先快速探测服务，300ms 内未响应直接放弃本轮，绝不阻塞编辑
            const online = await checkHealth(300);
            if (!online || token.isCancellationRequested) return undefined;

            // 光标前 2000 字符 + 后 500 字符作为上下文
            const full = document.getText();
            const offset = document.offsetAt(position);
            const prefix = full.slice(Math.max(0, offset - 2000), offset);
            const suffix = full.slice(offset, offset + 500);

            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), 8000); // 超时 8 秒放弃
            const cancelSub = token.onCancellationRequested(() => ctrl.abort());
            let text = '';
            try {
                // J: 改流式调用 —— SSE 逐 token 接收，等 done 时一次性返回（VSCode InlineCompletion 不支持增量显示）
                const streamed = await new Promise((resolve, reject) => {
                    let buf = '', result = '', done = false;
                    fetch(getServerUrl() + '/v1/chat/completions', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            messages: [
                                {
                                    role: 'system',
                                    content: '你是代码自动补全引擎。根据代码上下文，在 <CURSOR> 位置继续补全代码。只返回需要插入的代码本身，不要任何解释，不要 markdown 代码块标记，不要重复上下文中已有的代码。'
                                },
                                {
                                    role: 'user',
                                    content: '请在 <CURSOR> 处补全代码。\n\n前文：\n' + prefix + '\n\n<CURSOR>\n\n后文：\n' + suffix
                                }
                            ],
                            session_id: 'vscodium-inline',
                            max_tokens: 128,
                            max_tool_rounds: 1,
                            stream: true
                        }),
                        signal: ctrl.signal
                    }).then(async (res) => {
                        if (!res.ok || !res.body) { reject(new Error('HTTP ' + res.status)); return; }
                        const ctype = res.headers.get('content-type') || '';
                        if (ctype.indexOf('text/event-stream') < 0) {
                            const d = await res.json();
                            resolve(extractReply(d));
                            return;
                        }
                        const reader = res.body.getReader();
                        const dec = new TextDecoder('utf-8');
                        while (!done) {
                            const chunk = await reader.read();
                            if (chunk.done) break;
                            buf += dec.decode(chunk.value, { stream: true });
                            let idx;
                            while ((idx = buf.indexOf('\n\n')) >= 0) {
                                const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
                                const line = raw.replace(/^data: ?/, '').trim();
                                if (!line || line === '[DONE]') continue;
                                let ev; try { ev = JSON.parse(line); } catch (e) { continue; }
                                if (ev.ev === 'delta') result += ev.t;
                                else if (ev.ev === 'reset') result = '';
                                else if (ev.ev === 'done') { done = true; if (ev.content) result = ev.content; }
                            }
                        }
                        resolve(result);
                    }).catch(reject);
                });
                text = streamed;
            } finally {
                clearTimeout(timer);
                cancelSub.dispose();
            }

            if (token.isCancellationRequested) return undefined;
            const code = stripFences(text);
            if (!code) return undefined;
            // 避免补全内容与光标前最后一行完全重复
            const prefixLines = prefix.split('\n');
            const lastLine = (prefixLines[prefixLines.length - 1] || '').trim();
            if (lastLine && code.trim() === lastLine) return undefined;

            return [new vscode.InlineCompletionItem(code, new vscode.Range(position, position))];
        } catch (e) {
            return undefined; // 任何错误都静默放弃，不影响编辑
        } finally {
            this.inflight = false;
        }
    }
}

// 按配置注册/注销内联补全 provider
let inlineDisp = null;
function updateInlineProvider() {
    const cfg = vscode.workspace.getConfiguration('wasily-ai');
    const enabled = cfg.get('inlineSuggest.enabled', true);
    if (enabled && !inlineDisp) {
        inlineDisp = vscode.languages.registerInlineCompletionItemProvider('*', new InlineCompletionProvider());
    } else if (!enabled && inlineDisp) {
        inlineDisp.dispose();
        inlineDisp = null;
    }
}

// ==================== 命令 ====================

async function askAboutSelection(context) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('请先打开一个编辑器');
        return;
    }
    const selection = editor.selection;
    const text = editor.document.getText(selection);
    if (!text) {
        vscode.window.showWarningMessage('请先选中一段代码');
        return;
    }
    const lang = editor.document.languageId;
    const fileName = editor.document.fileName;
    const question = await vscode.window.showInputBox({
        prompt: '关于选中的代码，你想问什么？',
        placeHolder: '例如：这段代码有什么问题？请解释它的逻辑',
        value: '请解释这段代码'
    });
    if (question === undefined) return;
    const prompt = '文件：' + fileName + '\n语言：' + lang + '\n选中的代码：\n```' + lang + '\n' + text + '\n```\n\n问题：' + question;
    await context.chatProvider.sendFromCommand(prompt);
}

// 调 /v1/ide/stream：带当前文件+选区+指令，返回流式 done 事件
async function callIdeStream({ mode = 'inline_edit', instruction = '', filePath = '', code = '', selection = '',
    maxTokens = 2048, onDelta }) {
    const res = await fetch(getServerUrl() + '/v1/ide/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, file: filePath, code, selection, messages: [{ role: 'user', content: instruction }],
            max_tokens: maxTokens })
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const ctype = res.headers.get('content-type') || '';
    if (ctype.indexOf('text/event-stream') < 0 || !res.body) {
        // 旧服务降级
        const d = await res.json();
        return { content: extractReply({ choices: [{ message: { content: extractReply(d) } }] }) };
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '', full = '', done = null;
    while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
            const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
            const line = raw.replace(/^data: ?/, '').trim();
            if (!line || line === '[DONE]') continue;
            let ev; try { ev = JSON.parse(line); } catch (e) { continue; }
            if (ev.delta != null) {
                full += ev.delta;
                if (onDelta) onDelta(ev.delta);
            } else if (ev.done) {
                done = ev;
            } else if (ev.error) {
                throw new Error(ev.error);
            }
        }
        if (done) break;
    }
    return { content: full, done };
}

async function editCurrentFile() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('请先打开一个编辑器');
        return;
    }
    const doc = editor.document;
    const hasSelection = !editor.selection.isEmpty;
    const selectionText = hasSelection ? doc.getText(editor.selection) : '';
    const fileText = doc.getText();
    const lang = doc.languageId;
    const fileName = doc.fileName;

    const instruction = await vscode.window.showInputBox({
        prompt: hasSelection ? '告诉 AI 如何修改【选中的代码】（改完自动替换选区）'
            : '告诉 AI 如何修改【整个文件】（改完预览 diff）',
        placeHolder: '例如：给这段加错误处理 / 重构为 class / 改成 async'
    });
    if (!instruction) return;

    await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'Wasily AI 正在生成修改…', cancellable: false },
        async () => {
            let raw;
            try {
                const r = await callIdeStream({
                    mode: 'inline_edit',
                    instruction,
                    filePath: fileName || '',
                    code: hasSelection ? '' : fileText,   // 有选区时不塞全文件（省 token），没选区时塞全文件
                    selection: selectionText,
                    maxTokens: hasSelection ? 1024 : 4096
                });
                raw = r.content || '';
            } catch (e) {
                vscode.window.showErrorMessage('无法连接 Wasily AI 服务（' + getServerUrl() + '）。' + (e && e.message ? ' (' + e.message + ')' : ''));
                return;
            }
            if (!raw) {
                vscode.window.showInformationMessage('AI 没有返回有效的修改内容');
                return;
            }
            const blocks = extractCodeBlocks(raw);
            const newText = blocks.length > 0 ? blocks[0] : raw.trim();
            if (!newText) {
                vscode.window.showInformationMessage('AI 返回内容提取失败');
                return;
            }

            if (hasSelection) {
                // Inline Edit：直接替换选区（安全：替换的是用户明确框选的那一段）
                await editor.edit((edit) => edit.replace(editor.selection, newText));
                try { await doc.save(); } catch (e) { }
                vscode.window.showInformationMessage('✓ 已替换选中代码（Wasily AI Inline Edit）');
            } else {
                // E5: Cursor Auto-apply —— 用户主动触发 Inline Edit 就直接替换整文件（不再走 modal 确认）
                // 尊重 Trae 行为：用户信任 AI 才会点「应用」，不需要二次 confirm 打断
                const fullRange = new vscode.Range(doc.positionAt(0), doc.positionAt(fileText.length));
                await editor.edit((edit) => edit.replace(fullRange, newText));
                try { await doc.save(); } catch (e) { }
                vscode.window.showInformationMessage('✓ Wasily AI 已直接应用修改（Auto-apply 模式）');
                // 顺手打开 diff 让用户 review —— 不阻塞，只是额外参考
                try {
                    const _old = await vscode.workspace.openTextDocument({ language: lang, content: fileText });
                    const _new = await vscode.workspace.openTextDocument({ language: lang, content: newText });
                    vscode.commands.executeCommand('vscode.diff', _old.uri, _new.uri, 'Wasily AI 修改预览（已自动应用）');
                } catch (e) { /* diff 失败不影响主流程 */ }
            }
        }
    );
}

async function insertAtCursor() {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('请先打开一个编辑器');
        return;
    }
    const doc = editor.document;
    const lang = doc.languageId;
    const fileName = doc.fileName;

    const instruction = await vscode.window.showInputBox({
        prompt: '描述要生成的代码，AI 生成后将插入当前光标处',
        placeHolder: '例如：一个防抖函数 / 读取 JSON 文件的辅助函数'
    });
    if (!instruction) return;

    await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'Wasily AI 正在生成代码…', cancellable: false },
        async () => {
            let raw;
            try {
                const r = await callIdeStream({
                    mode: 'generate',
                    instruction,
                    filePath: fileName || '',
                    code: doc.getText(),
                    maxTokens: 2048
                });
                raw = r.content || '';
            } catch (e) {
                vscode.window.showErrorMessage('无法连接 Wasily AI 服务（' + getServerUrl() + '）。' + (e && e.message ? ' (' + e.message + ')' : ''));
                return;
            }
            if (!raw) {
                vscode.window.showInformationMessage('AI 没有返回代码');
                return;
            }
            const blocks = extractCodeBlocks(raw);
            const code = blocks.length > 0 ? blocks[0] : raw.trim();
            await insertTextIntoEditor(code + '\n');
            vscode.window.showInformationMessage('✓ 已在光标处插入 AI 生成的代码');
        }
    );
}

// 新增快捷命令（与面板快捷操作同一实现）
async function quickCommand(context, kind) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('请先打开一个编辑器');
        return;
    }
    const prompt = buildQuickPrompt(editor, kind);
    if (!prompt) {
        vscode.window.showWarningMessage('请先选中一段代码，再使用「' + (QUICK_LABELS[kind] || kind) + '」');
        return;
    }
    await context.chatProvider.sendFromCommand(prompt);
}

// ==================== 端口自动转发（面板内表格） ====================

const PORTS = [
    { port: 8000, label: 'wasily AI 服务' },
    { port: 8001, label: '项目文档中心' },
    { port: 3000, label: '前端预览' }
];

// TCP 连接探测（500ms 超时），不依赖 HTTP 是否返回 200
function checkPort(port) {
    return new Promise((resolve) => {
        const sock = require('net').connect({ host: '127.0.0.1', port: port });
        const done = (ok) => { try { sock.destroy(); } catch (e) { } resolve(ok); };
        sock.setTimeout(500);
        sock.once('connect', () => done(true));
        sock.once('timeout', () => done(false));
        sock.once('error', () => done(false));
    });
}

async function probePorts() {
    const rows = [];
    for (const p of PORTS) {
        rows.push({ port: p.port, label: p.label, online: await checkPort(p.port) });
    }
    return rows;
}

// ==================== 设置中心 ====================

let settingsPanel = null;
let agentCenterPanel = null;
let agentChatPanel = null;
let changelogPanel = null;

// 当前更新日志内容标识：内容/版本更新时递增，用于「更新后显示更新日志」自动弹出
const CHANGELOG_ID = '2026-11-02-v3.4.14';

// 在中央编辑器区打开更新日志（iframe 嵌入 /changelog）
async function openChangelogPanel(context) {
    const src = getServerUrl() + '/changelog';
    if (changelogPanel) {
        try {
            changelogPanel.reveal(undefined, false);
            changelogPanel.webview.html = buildAgentCenterHtml(src);
            return;
        } catch (e) { changelogPanel = null; }
    }
    const col = (vscode.window.activeTextEditor && vscode.window.activeTextEditor.viewColumn)
        || vscode.ViewColumn.One;
    const panel = vscode.window.createWebviewPanel(
        'wasily-ai.changelog',
        '更新日志',
        col,
        { enableScripts: true, retainContextWhenHidden: true }
    );
    changelogPanel = panel;
    panel.onDidDispose(() => { changelogPanel = null; });
    panel.webview.html = buildAgentCenterHtml(src);
}

// 更新后自动弹出更新日志：日志标识变化且用户未关闭该开关时触发
async function maybeShowChangelogOnUpdate(context) {
    try {
        const seen = context.globalState.get('wasily.changelog.seenId', '');
        if (seen === CHANGELOG_ID) return;
        let enabled = true;
        try {
            const res = await fetch(getServerUrl() + '/config/get?path=' +
                encodeURIComponent('ui.show_changelog_on_update'));
            if (res.ok) {
                const d = await res.json();
                if (d && typeof d.value === 'boolean') enabled = d.value;
            }
        } catch (e) { /* 服务未启动时按默认勾选处理 */ }
        await context.globalState.update('wasily.changelog.seenId', CHANGELOG_ID);
        if (enabled) openChangelogPanel(context);
    } catch (e) { /* 忽略 */ }
}

// Agent 中心 webview 外壳（iframe 嵌入 /agent）
function buildAgentCenterHtml(src) {
    return '<!DOCTYPE html><html><head><meta charset="UTF-8">' +
        '<style>html,body{margin:0;height:100%;overflow:hidden;background:#17181c}' +
        'iframe{width:100%;height:100%;border:0;display:block}</style></head><body>' +
        '<iframe src="' + src + '"></iframe></body></html>';
}

// ===== IDE 原生面板：直接渲染服务端网页，不走 iframe / Simple Browser =====
// 之前用 Simple Browser 会出现地址栏（看起来像浏览器）且偶发白屏；这里由插件
// 自己取 HTML、注入 <base> 指向服务端，再用原生 webview 渲染，效果与 Trae 内置面板一致。
const nativePanels = new Map();   // key -> WebviewPanel

function fetchPageText(url) {
    return new Promise((resolve, reject) => {
        const http = require('http');
        const req = http.get(url, (res) => {
            if (res.statusCode !== 200) {
                res.resume();
                reject(new Error('HTTP ' + res.statusCode));
                return;
            }
            let buf = '';
            res.setEncoding('utf8');
            res.on('data', (c) => { buf += c; });
            res.on('end', () => resolve(buf));
        });
        req.on('error', reject);
        req.setTimeout(8000, () => req.destroy(new Error('timeout')));
    });
}

// 页面里的 fetch('/v1/...')、<img src="/x"> 等相对路径靠 <base> 解析到服务端
function injectBase(html, url) {
    let origin = '';
    try { origin = new URL(url).origin + '/'; } catch (e) { return html; }
    const tag = '<base href="' + origin + '">';
    const m = html.search(/<head[^>]*>/i);
    if (m < 0) return tag + html;
    const end = html.indexOf('>', m) + 1;
    return html.slice(0, end) + tag + html.slice(end);
}

async function openNativeWebPanel(url, title, key) {
    const old = nativePanels.get(key);
    if (old) {
        try {
            old.reveal(undefined, false);
            old.webview.html = injectBase(await fetchPageTextRetry(url), url);
            return true;
        } catch (e) {
            nativePanels.delete(key);
        }
    }
    const html = injectBase(await fetchPageTextRetry(url), url);
    const col = (vscode.window.activeTextEditor && vscode.window.activeTextEditor.viewColumn)
        || vscode.ViewColumn.One;
    const panel = vscode.window.createWebviewPanel(
        'wasily-ai.' + key, title, col,
        { enableScripts: true, retainContextWhenHidden: true }
    );
    nativePanels.set(key, panel);
    panel.onDidDispose(() => {
        if (nativePanels.get(key) === panel) nativePanels.delete(key);
    });
    panel.webview.html = html;
    return true;
}

// 在 IDE 内打开本地网页已统一走原生 webview；这里只负责取页面 HTML。
// 取页面时做 3 次重试，避免服务端刚好在重启时抓到空响应而退回空白页。
async function fetchPageTextRetry(url) {
    let lastErr = null;
    for (let i = 0; i < 3; i++) {
        try {
            return await fetchPageText(url);
        } catch (e) {
            lastErr = e;
            await new Promise((r) => setTimeout(r, 600));
        }
    }
    throw lastErr || new Error('fetch failed');
}

// 绑定当前 IDE 现场，由后端拉起真正的 Electron 无边框工作台
async function openAgentCenter(context, tab) {
    try {
        const folders = vscode.workspace.workspaceFolders || [];
        const activeDoc = vscode.window.activeTextEditor && vscode.window.activeTextEditor.document;
        const activeFolder = activeDoc && vscode.workspace.getWorkspaceFolder(activeDoc.uri);
        const folder = activeFolder || folders[0];
        const workspace = folder && folder.uri ? folder.uri.fsPath : '';
        const windowId = _activeWindowId || _ideClientId || 'default';
        const bindRes = await fetch(getServerUrl() + '/v1/workbench/bind', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ window_id: windowId, workspace: workspace })
        });
        if (!bindRes.ok) throw new Error('工作台绑定失败（HTTP ' + bindRes.status + '）');
        const binding = await bindRes.json();
        const workbenchId = binding && typeof binding.workbench_id === 'string'
            ? binding.workbench_id.trim() : '';
        if (!workbenchId) throw new Error('工作台绑定响应缺少 workbench_id');

        const spawnRes = await fetch(getServerUrl() + '/spawn_agent', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ workbench_id: workbenchId })
        });
        if (!spawnRes.ok) throw new Error('原生工作台启动失败（HTTP ' + spawnRes.status + '）');
        const spawned = await spawnRes.json().catch(() => null);
        if (spawned && spawned.ok === false) {
            throw new Error(spawned.error || '原生工作台启动失败');
        }
    } catch (e) {
        await vscode.window.showErrorMessage('打开 Agent 工作台失败：' +
            (e && e.message ? e.message : String(e)));
    }
}

// 浏览器 App 模式：没有标签页和地址栏，但仍保留系统标题栏
function openAgentByBrowser(src) {
    const roots = [
        process.env.PROGRAMFILES,
        process.env['PROGRAMFILES(X86)'],
        process.env.LOCALAPPDATA
    ].filter(Boolean);
    const candidates = [];
    for (const root of roots) {
        // 优先使用系统浏览器，避免本机 Chrome 启动器存在但无法创建窗口
        candidates.push(path.join(root, 'Microsoft', 'Edge', 'Application', 'msedge.exe'));
        candidates.push(path.join(root, 'Google', 'Chrome', 'Application', 'chrome.exe'));
    }
    const browser = candidates.find((p) => fs.existsSync(p));
    if (!browser) {
        vscode.env.openExternal(vscode.Uri.parse(src));
        return;
    }
    try {
        const child = require('child_process').spawn(browser, [
            '--app=' + src,
            '--new-window',
            '--no-first-run',
            '--no-default-browser-check'
        ], { detached: true, stdio: 'ignore', windowsHide: false });
        // 浏览器启动失败时 spawn 只抛 error 事件，不 throw；不接住会表现为「点了没反应」
        child.on('error', () => { fallbackAgentPanel(src); });
        child.unref();
    } catch (e) {
        fallbackAgentPanel(src);
    }
}

// 独立窗口无法创建时退回 IDE 内原生面板，保证点击始终有可见反馈
function fallbackAgentPanel(src) {
    openNativeWebPanel(src, 'Agent 中心', 'agentCenter')
        .catch(() => vscode.env.openExternal(vscode.Uri.parse(src)));
}

// IDE ↔ Agent 联动：把当前 IDE 会话带到独立 Agent 对话页继续聊
// src = /chat?session=ide:<sid>&from=ide，页头会显示「来自 IDE」并可一键打开当前文件
function buildAgentChatHtml(src) {
    return '<!DOCTYPE html><html><head><meta charset="UTF-8">' +
        '<style>html,body{margin:0;height:100%;overflow:hidden;background:#17181c}' +
        'iframe{width:100%;height:100%;border:0;display:block}</style></head><body>' +
        '<iframe src="' + src + '"></iframe></body></html>';
}

// 聚焦右侧常驻的 WASILY 对话面板（与 Trae 的常驻 Agent 面板一致，不再弹新窗口）
async function revealWasilyPanel() {
    try {
        await vscode.commands.executeCommand('workbench.view.extension.wasilyAiBar');
        await vscode.commands.executeCommand('wasily-ai.panelView.focus');
    } catch (e) {
        console.warn('[revealWasilyPanel] 聚焦面板失败：' + (e && e.message));
    }
}

async function openAgentChatPanel(context, sessionId) {
    const sid = String(sessionId || '').trim() || 'ide:default';
    const src = getServerUrl() + '/chat?session=' + encodeURIComponent(sid) + '&from=ide';
    try {
        if (await openNativeWebPanel(src, 'Agent 对话', 'agentChat')) return;
    } catch (e) {
        console.warn('[openAgentChatPanel] 原生面板失败：' + (e && e.message));
    }
    if (agentChatPanel) {
        try {
            agentChatPanel.reveal(undefined, false);
            agentChatPanel.webview.html = buildAgentChatHtml(src);
            return;
        } catch (e) { agentChatPanel = null; }
    }
    const col = (vscode.window.activeTextEditor && vscode.window.activeTextEditor.viewColumn)
        || vscode.ViewColumn.One;
    const panel = vscode.window.createWebviewPanel(
        'wasily-ai.agentChat',
        'Agent 对话',
        col,
        { enableScripts: true, retainContextWhenHidden: true }
    );
    agentChatPanel = panel;
    panel.onDidDispose(() => { agentChatPanel = null; });
    panel.webview.html = buildAgentChatHtml(src);
}

// 设置中心 webview 外壳（iframe 嵌入 /settings_center，桥接原生设置命令）
function buildSettingsHtml(src) {
    return '<!DOCTYPE html><html><head><meta charset="UTF-8">' +
        '<style>html,body{margin:0;height:100%;overflow:hidden;background:#1b1c21}' +
        'iframe{width:100%;height:100%;border:0;display:block}</style></head><body>' +
        '<iframe id="f" src="' + src + '"></iframe>' +
        '<script>const vscode=acquireVsCodeApi();' +
        'window.addEventListener("message",function(e){' +
        'var d=e.data;if(d&&d.from==="wasily-settings"){vscode.postMessage({type:"bridge",action:d.action});}});' +
        '</scr' + 'ipt></body></html>';
}

// 在编辑器区（中间 tab）打开设置中心。
// tab 为空时读取标题栏等写入的 ui.settings_pending_tab 信标（读后即清），
// 避免跨进程 localStorage 传递失效。
async function openSettingsCenter(context, tab) {
    tab = typeof tab === 'string' ? tab : '';
    if (!tab) {
        try {
            const res = await fetch(getServerUrl() + '/config/get?path=' + encodeURIComponent('ui.settings_pending_tab'));
            if (res.ok) {
                const d = await res.json();
                if (d && typeof d.value === 'string' && d.value) {
                    tab = d.value;
                    fetch(getServerUrl() + '/config/set', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: 'ui.settings_pending_tab', value: '' })
                    }).catch(() => { });
                }
            }
        } catch (e) { /* 服务未启动时忽略 */ }
    }
    const src = getServerUrl() + '/settings_center' + (tab ? '#' + tab : '');
    if (settingsPanel) {
        try {
            settingsPanel.reveal(undefined, false);
            if (tab) settingsPanel.webview.html = buildSettingsHtml(src);
            return;
        } catch (e) { settingsPanel = null; }
    }
    // 焦点可能停在右侧 AI 面板（webview），此时 activeTextEditor 为空，
    // 统一回退到第一栏，确保设置中心开在中央编辑器区而不是 AI 标签列
    const col = (vscode.window.activeTextEditor && vscode.window.activeTextEditor.viewColumn)
        || vscode.ViewColumn.One;
    const panel = vscode.window.createWebviewPanel(
        'wasily-ai.settings',
        '设置',
        col,
        { enableScripts: true, retainContextWhenHidden: true }
    );
    settingsPanel = panel;
    panel.onDidDispose(() => { settingsPanel = null; });
    panel.webview.html = buildSettingsHtml(src);
    panel.webview.onDidReceiveMessage((msg) => {
        if (!msg || msg.type !== 'bridge') return;
        if (msg.action === 'openEditorSettings') {
            vscode.commands.executeCommand('workbench.action.openSettings');
        } else if (msg.action === 'openKeybindings') {
            vscode.commands.executeCommand('workbench.action.openGlobalKeybindings');
        } else if (msg.action === 'applyMarkdownOpen') {
            applyMarkdownOpen();
        }
    });
}

// ==================== 激活 ====================

// 在编辑器右侧创建 AI 面板：与编辑器同层、非活动栏插件视图，视觉与 Trae 右侧 AI 面板一致
function createChatPanel(context, provider) {
    // 对话默认停靠右侧辅助侧栏（全高），此处仅负责唤出该视图
    ensureChatView().then(() => { provider.pushMode(); }, () => { /* 忽略 */ });
    return null;
}

// 兼容旧调用点：旧的编辑器区 WebviewPanel 实现（保留但不再默认使用）
function createChatEditorPanel(context, provider) {
    const panel = vscode.window.createWebviewPanel(
        'wasily-ai.panel',
        'Wasily AI',
        { viewColumn: vscode.ViewColumn.Two, preserveFocus: true },
        { enableScripts: true, retainContextWhenHidden: true }
    );
    try {
        panel.iconPath = vscode.Uri.joinPath(context.extensionUri, 'resources', 'icon.svg');
    } catch (e) { /* 图标缺失不影响使用 */ }
    provider.panel = panel;
    const webview = panel.webview;
    provider.attachWebview(webview);
    panel.onDidDispose(() => {
        provider.detachWebview(webview);
        if (provider.panel === panel) provider.panel = null;
    });
    return panel;
}

// ==================== 更新检测与诊断（官方地址探测 + 友好提示 + 镜像替代） ====================
const UPDATE_SITES = {
    official: 'https://github.com/VSCodium/vscodium/releases',
    mirror: 'https://gh-proxy.com/https://github.com/VSCodium/vscodium/releases'
};

function classifyNetworkError(e) {
    const msg = String((e && e.message) || e || '');
    if (/ENOTFOUND|EAI_AGAIN|getaddrinfo/i.test(msg)) return 'DNS 解析失败（域名无法解析，可能被安全策略或 hosts 屏蔽）';
    if (/ECONNREFUSED/.test(msg)) return '连接被拒绝（目标端口未响应）';
    if (/CERT|SSL|TLS|certificate/i.test(msg)) return 'SSL 证书异常（' + msg + '）';
    if (/abort|timeout|ETIMEDOUT|ECONNRESET|terminated|network|fetch/i.test(msg))
        return '连接超时或被重置（当前网络环境无法访问该站点，多为网络阻断所致）';
    return msg || '未知网络错误';
}

async function probeUpdateUrl(url, timeoutMs) {
    const t0 = Date.now();
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs || 6000);
    try {
        const res = await fetch(url, { signal: ctrl.signal, redirect: 'follow' });
        if (res.body && res.body.cancel) res.body.cancel(); // 不读 body，立即释放连接防泄漏
        return { ok: res.status < 500, status: res.status, ms: Date.now() - t0, err: '' };
    } catch (e) {
        return { ok: false, status: 0, ms: Date.now() - t0, err: classifyNetworkError(e) };
    } finally {
        clearTimeout(timer);
    }
}

async function checkForUpdateDiagnostics() {
    const r = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: '正在检查更新（探测官方地址可达性）…', cancellable: false },
        async () => {
            const official = await probeUpdateUrl(UPDATE_SITES.official, 6000);
            const mirror = official.ok ? { ok: true, status: 0, ms: 0, err: '' } : await probeUpdateUrl(UPDATE_SITES.mirror, 6000);
            return { official, mirror };
        });
    const offTxt = r.official.ok ? ('可达 · HTTP ' + r.official.status + ' · ' + r.official.ms + 'ms') : ('不可达 · ' + r.official.err);
    const mirTxt = r.mirror.ok ? ('可达 · HTTP ' + r.mirror.status + ' · ' + r.mirror.ms + 'ms') : ('不可达 · ' + r.mirror.err);
    const picks = [];
    if (r.mirror.ok) {
        picks.push({
            label: '$(cloud-download) 打开下载页（镜像加速）',
            detail: UPDATE_SITES.mirror + ' —— ' + mirTxt,
            action: 'mirror'
        });
    }
    if (r.official.ok) {
        picks.push({ label: '$(cloud-download) 打开官方下载页', detail: UPDATE_SITES.official + ' —— ' + offTxt, action: 'official' });
    }
    picks.push({ label: '$(link) 复制官方下载链接（可用浏览器/下载工具自行打开）', detail: UPDATE_SITES.official, action: 'copy' });
    if (!r.official.ok) {
        picks.push({
            label: '$(info) 官方地址无法访问的原因',
            detail: offTxt + '。替代方案：使用镜像加速下载，或复制链接到可访问外网的设备/浏览器下载。',
            action: 'noop'
        });
    }
    const sel = await vscode.window.showQuickPick(picks, {
        placeHolder: '检查更新 —— 官方 ' + offTxt + '，镜像 ' + mirTxt
    });
    if (!sel) return;
    if (sel.action === 'mirror') await vscode.env.openExternal(vscode.Uri.parse(UPDATE_SITES.mirror));
    else if (sel.action === 'official') await vscode.env.openExternal(vscode.Uri.parse(UPDATE_SITES.official));
    else if (sel.action === 'copy') { await vscode.env.clipboard.writeText(UPDATE_SITES.official); vscode.window.setStatusBarMessage('已复制下载链接', 3000); }
}

// ---------- AI 服务器设置页（离线时点击状态栏进入） ----------
let _serverPanel = null;

function _serverPort() {
    try {
        const u = new URL(getServerUrl());
        return u.port || (u.protocol === 'https:' ? '443' : '80');
    } catch (e) {
        return '8000';
    }
}

function _killAiServer(cb) {
    const port = _serverPort();
    const cmd = process.platform === 'win32'
        ? 'for /f "tokens=5" %a in (\'netstat -ano ^| findstr LISTENING ^| findstr :' + port + '\') do taskkill /PID %a /F'
        : 'lsof -ti tcp:' + port + ' | xargs -r kill -9';
    require('child_process').exec(cmd, { windowsHide: true }, (err, stdout, stderr) => {
        cb(String(stdout || '') + String(stderr || ''));
    });
}

function _startAiServer() {
    const fs = require('fs');
    const cp = require('child_process');
    const bat = path.resolve(__dirname, '../../../../../../启动Wasily.bat');
    if (!fs.existsSync(bat)) return { ok: false, error: '未找到启动脚本：' + bat };
    const child = cp.spawn('cmd.exe', ['/c', bat], { detached: true, stdio: 'ignore', windowsHide: true });
    child.unref();
    return { ok: true, path: bat };
}

function _serverSettingsHtml() {
    return `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);background:var(--vscode-editor-background);padding:24px;max-width:900px}
h1{font-size:18px;font-weight:600;margin:0 0 4px}
.sub{color:var(--vscode-descriptionForeground);font-size:12px;margin-bottom:18px}
.card{background:var(--vscode-editorWidget-background);border:1px solid var(--vscode-widget-border,rgba(128,128,128,.25));border-radius:8px;padding:16px;margin-bottom:14px}
.row{display:flex;align-items:center;gap:10px;margin:7px 0;flex-wrap:wrap}
.dot{width:10px;height:10px;border-radius:50%;background:#d93025;flex:none}
.dot.on{background:#2ea043}
.k{color:var(--vscode-descriptionForeground);width:88px;font-size:12px;flex:none}
.v{font-size:13px;word-break:break-all}
.mono{font-family:var(--vscode-editor-font-family,monospace);font-size:11.5px}
.tag{font-size:11px;padding:2px 8px;border-radius:10px;background:rgba(128,128,128,.16);color:var(--vscode-descriptionForeground)}
.hint{color:var(--vscode-descriptionForeground);font-size:11.5px;line-height:1.7}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:12px 0 4px}
.box{background:rgba(128,128,128,.07);border-radius:7px;padding:10px 12px}
.bk{font-size:11px;color:var(--vscode-descriptionForeground)}
.bv{font-size:14px;font-weight:600;margin-top:4px;word-break:break-all}
.bv small{font-size:11px;font-weight:400;color:var(--vscode-descriptionForeground);margin-left:3px}
button{font-family:inherit;font-size:13px;padding:6px 14px;border-radius:6px;border:1px solid transparent;background:var(--vscode-button-background);color:var(--vscode-button-foreground);cursor:pointer}
button.sec{background:var(--vscode-button-secondaryBackground);color:var(--vscode-button-secondaryForeground)}
button:hover{opacity:.88}
input{flex:1;min-width:240px;padding:6px 10px;border-radius:6px;border:1px solid var(--vscode-input-border,#555);background:var(--vscode-input-background);color:var(--vscode-input-foreground);font-family:inherit;font-size:13px}
#log{height:110px;overflow:auto;font-family:var(--vscode-editor-font-family,monospace);font-size:11.5px;background:var(--vscode-textCodeBlock-background);border-radius:6px;padding:8px;white-space:pre-wrap;color:var(--vscode-descriptionForeground)}
.badge{font-size:10.5px;padding:1px 7px;border-radius:5px;background:rgba(128,128,128,.18);color:var(--vscode-descriptionForeground)}
</style></head><body>
<h1>AI 服务器设置</h1>
<div class="sub">本地 Wasily AI 后端 · 运行状态 / 服务控制 / 模型 / MCP / 工具 / 路径与日志</div>

<div class="card">
  <div class="row"><span id="dot" class="dot"></span><b id="state">检测中…</b>
    <span id="uptime" class="tag">-</span>
    <span style="margin-left:auto"><button id="refresh" class="sec">刷新状态</button></span>
  </div>
  <div class="grid">
    <div class="box"><div class="bk">服务地址</div><div class="bv" id="url" style="font-size:12px">-</div></div>
    <div class="box"><div class="bk">端口</div><div class="bv" id="port">-</div></div>
    <div class="box"><div class="bk">模型</div><div class="bv" id="model" style="font-size:12px">-</div></div>
    <div class="box"><div class="bk">上下文</div><div class="bv" id="ctx">-</div></div>
    <div class="box"><div class="bk">GPU 层</div><div class="bv" id="gpu">-</div></div>
    <div class="box"><div class="bk">MCP</div><div class="bv" id="mcp">-</div></div>
    <div class="box"><div class="bk">工具</div><div class="bv" id="toolSum">-</div></div>
    <div class="box"><div class="bk">Python</div><div class="bv" id="py">-</div></div>
  </div>
  <div class="row"><span class="k">工作区</span><span class="v mono" id="ws">-</span></div>
  <div class="row"><span class="k">模型文件</span><span class="v mono" id="mfile">-</span></div>
  <div class="row"><span class="k">生成参数</span><span class="v" id="gen">-</span></div>
</div>

<div class="card">
  <div class="row"><b>服务控制</b></div>
  <div class="row">
    <button id="start">启动服务</button>
    <button id="restart" class="sec">重启</button>
    <button id="stop" class="sec">停止</button>
    <button id="openPanel" class="sec">打开 AI 面板</button>
    <button id="openCenter" class="sec">完整设置中心</button>
  </div>
  <div class="hint">启动 = 运行「启动Wasily.bat」（幂等，已在运行则直接复用）；重启 = 结束 8000 端口进程后重新启动；停止 = 只结束本机后端进程，不影响 IDE。</div>
</div>

<div class="card">
  <div class="row"><span class="k">服务地址</span><input id="urlInput" placeholder="http://127.0.0.1:8000"></div>
  <div class="row"><button id="save">保存地址</button><span class="hint" style="margin-left:auto">扩展通过该地址访问本地 AI 后端</span></div>
</div>

<div class="card">
  <div class="row"><b>MCP 服务器</b><span class="k" style="width:auto;margin-left:auto" id="mcpSum">-</span></div>
  <div id="mcpList" style="margin-top:6px"><div class="hint">加载中…</div></div>
</div>

<div class="card">
  <div class="row"><b>工具</b><span class="k" style="width:auto;margin-left:auto" id="toolPie">-</span></div>
  <div id="userTools" style="margin-top:6px"><div class="hint">加载中…</div></div>
</div>

<div class="card">
  <div class="row"><b>路径与文件</b></div>
  <div class="row">
    <button id="openBat" class="sec">启动脚本</button>
    <button id="openLog" class="sec">后端日志</button>
    <button id="openLogErr" class="sec">错误日志</button>
  </div>
  <div class="row">
    <button id="openMcp2" class="sec">MCP 配置</button>
    <button id="openConfig" class="sec">config.json</button>
    <button id="openAiDir" class="sec">AI 目录</button>
  </div>
</div>

<div class="card"><div class="row"><b>操作日志</b></div><div id="log">等待操作…</div></div>
<script>
const vscode = acquireVsCodeApi();
const $ = (id) => document.getElementById(id);
function log(t){ const el=$('log'); el.textContent += '\\n' + t; el.scrollTop = el.scrollHeight; }
function fmtUp(s){
  if(!s && s!==0) return '-';
  s = Number(s)||0;
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), ss=s%60;
  return h>0 ? (h+' 小时 '+m+' 分') : (m>0 ? (m+' 分 '+ss+' 秒') : (ss+' 秒'));
}
$('start').onclick=()=>{ log('> 启动服务'); vscode.postMessage({type:'start'}); };
$('stop').onclick=()=>{ log('> 停止服务'); vscode.postMessage({type:'stop'}); };
$('restart').onclick=()=>{ log('> 重启服务'); vscode.postMessage({type:'restart'}); };
$('refresh').onclick=()=>vscode.postMessage({type:'refresh'});
$('save').onclick=()=>vscode.postMessage({type:'saveUrl', value:$('urlInput').value});
$('openBat').onclick=()=>vscode.postMessage({type:'openFile', target:'bat'});
$('openLog').onclick=()=>vscode.postMessage({type:'openFile', target:'log'});
$('openLogErr').onclick=()=>vscode.postMessage({type:'openFile', target:'logerr'});
$('openMcp2').onclick=()=>vscode.postMessage({type:'openFile', target:'mcp'});
$('openConfig').onclick=()=>vscode.postMessage({type:'openFile', target:'config'});
$('openAiDir').onclick=()=>vscode.postMessage({type:'openFile', target:'aidir'});
$('openPanel').onclick=()=>vscode.postMessage({type:'openPanel'});
$('openCenter').onclick=()=>vscode.postMessage({type:'openCenter'});
window.addEventListener('message', (e)=>{
  const m = e.data || {};
  if(m.type==='status'){
    const h = m.health || {};
    $('dot').className = 'dot' + (m.online?' on':'');
    $('state').textContent = m.online ? '运行中' : '未运行（离线）';
    $('uptime').textContent = m.online ? ('已运行 ' + fmtUp(h.uptime)) : '-';
    $('url').textContent = m.url || '-';
    $('port').textContent = h.port || '-';
    $('model').textContent = h.model || (m.online?'(未上报)':'-');
    $('ctx').textContent = h.ctx ? (h.ctx + ' tokens') : '-';
    $('gpu').textContent = (h.gpu_layers===0 || h.gpu_layers) ? String(h.gpu_layers) : '-';
    $('py').textContent = h.python || '-';
    $('ws').textContent = h.workspace || '(未上报)';
    $('mfile').textContent = h.path || h.file || '-';
    if(h.gen){
      $('gen').textContent = 'temperature ' + h.gen.temperature + ' · top_p ' + h.gen.top_p + ' · max_tokens ' + h.gen.max_tokens;
    }
    if(h.mcp){
      $('mcp').innerHTML = h.mcp.connected + ' <small>/ ' + h.mcp.total + '</small>';
    }
    if(h.tools){
      $('toolSum').textContent = h.tools.total + '';
      $('toolPie').textContent = '内置 ' + h.tools.builtin + ' · 用户工具 ' + h.tools.user;
    }
    if(m.mcpList){
      $('mcpSum').textContent = m.mcpList.filter(function(s){return s.connected;}).length + ' / ' + m.mcpList.length + ' 已连接';
      $('mcpList').innerHTML = m.mcpList.length ? m.mcpList.map(function(s){
        return '<div class="row" style="margin:4px 0"><span class="dot' + (s.connected?' on':'') + '"></span>'
          + '<span style="min-width:96px;font-size:13px">' + s.name + '</span>'
          + '<span class="k" style="width:52px">' + (s.transport||'') + '</span>'
          + '<span class="v">' + (s.connected ? s.tools + ' 个工具' : '连接失败') + '</span></div>';
      }).join('') : '<div class="hint">未配置 MCP 服务器</div>';
    }
    if(m.toolsInfo){
      const st = m.toolsInfo.user_state || {};
      const rows = Object.keys(st).map(function(f){
        const v = st[f] || {};
        const ok = v.status === 'ok';
        return '<div class="row" style="margin:3px 0"><span class="dot' + (ok?' on':'') + '"></span>'
          + '<span class="mono" style="min-width:150px">' + f + '</span>'
          + '<span class="v">' + (ok ? ('→ ' + (v.tool||'')) : (v.status === 'skipped' ? '已停用' : ('错误：' + (v.error||'')))) + '</span></div>';
      }).join('');
      $('userTools').innerHTML = (rows || '<div class="hint">暂无用户工具</div>');
    }
    if(!$('urlInput').value) $('urlInput').value = m.url || '';
  } else if(m.type==='log'){ log(m.text); }
});
</script></body></html>`;
}

function openServerSettings(context) {
    if (_serverPanel) { _serverPanel.reveal(vscode.ViewColumn.Active); return; }
    _serverPanel = vscode.window.createWebviewPanel('wasilyServerSettings', 'AI 服务器设置',
        vscode.ViewColumn.Active, { enableScripts: true, retainContextWhenHidden: true });
    _serverPanel.webview.html = _serverSettingsHtml();

    const pushStatus = async () => {
        if (!_serverPanel) return;
        const online = await checkHealth(2500);
        let health = {}, mcpList = [], toolsInfo = null;
        if (online) {
            try {
                health = await (await fetch(getServerUrl() + '/health')).json();
            } catch (e) { /* 忽略 */ }
            try {
                const d = await (await fetch(getServerUrl() + '/v1/mcp/servers')).json();
                mcpList = (d.servers || []).map(s => ({
                    name: s.name, transport: s.transport || '',
                    connected: !!s.connected, tools: (s.tools || []).length
                }));
            } catch (e) { /* 忽略 */ }
            try {
                toolsInfo = await (await fetch(getServerUrl() + '/v1/tools')).json();
            } catch (e) { /* 忽略 */ }
        }
        if (_serverPanel) _serverPanel.webview.postMessage({
            type: 'status', online, url: getServerUrl(), health, mcpList, toolsInfo
        });
    };

    const timer = setInterval(pushStatus, 5000);
    _serverPanel.onDidDispose(() => { clearInterval(timer); _serverPanel = null; });
    pushStatus();

    _serverPanel.webview.onDidReceiveMessage(async (msg) => {
        const post = (t) => { if (_serverPanel) _serverPanel.webview.postMessage({ type: 'log', text: t }); };
        const m = msg || {};
        if (m.type === 'start') {
            const r = _startAiServer();
            post(r.ok ? '已调用启动脚本，正在等待服务就绪…' : r.error);
            setTimeout(pushStatus, 4000);
            setTimeout(pushStatus, 15000);
        } else if (m.type === 'stop') {
            _killAiServer((out) => {
                post('已发送停止指令' + (out ? '：' + out.slice(0, 160) : ''));
                setTimeout(pushStatus, 1500);
            });
        } else if (m.type === 'restart') {
            _killAiServer(() => {
                post('已停止，1.8 秒后重新启动…');
                setTimeout(() => {
                    const r = _startAiServer();
                    post(r.ok ? '已调用启动脚本，等待服务就绪…' : r.error);
                    setTimeout(pushStatus, 4000);
                    setTimeout(pushStatus, 16000);
                }, 1800);
            });
        } else if (m.type === 'refresh') {
            pushStatus();
        } else if (m.type === 'saveUrl') {
            await vscode.workspace.getConfiguration('wasily-ai').update('serverUrl',
                String(m.value || '').trim(), vscode.ConfigurationTarget.Global);
            post('服务地址已保存：' + String(m.value || '').trim());
            pushStatus();
        } else if (m.type === 'openFile') {
            const fs = require('fs');
            const base = path.resolve(__dirname, '../../../../../../');
            const map = {
                bat: path.join(base, '启动Wasily.bat'),
                log: path.join(base, 'AI', 'api_run.log'),
                logerr: path.join(base, 'AI', 'api_err.log'),
                mcp: path.join(base, 'AI', 'mcp.json'),
                config: path.join(base, 'AI', 'config.json'),
                aidir: path.join(base, 'AI')
            };
            const target = map[m.target];
            if (!target) return;
            if (m.target === 'aidir') {
                if (fs.existsSync(target)) vscode.env.openExternal(vscode.Uri.file(target));
                else post('目录不存在：' + target);
            } else if (fs.existsSync(target)) {
                vscode.window.showTextDocument(vscode.Uri.file(target), { preview: false });
            } else {
                post('文件不存在：' + target);
            }
        } else if (m.type === 'openMcp') {
            const fs = require('fs');
            const f = path.resolve(__dirname, '../../../../../../AI/mcp.json');
            if (fs.existsSync(f)) vscode.window.showTextDocument(vscode.Uri.file(f));
            else vscode.window.showWarningMessage('未找到 MCP 配置：' + f);
        } else if (m.type === 'openCenter') {
            const ok = await checkHealth(2500);
            if (!ok) { post('后端离线，无法打开完整设置中心；请先点击「启动服务」。'); return; }
            try {
                await openNativeWebPanel(getServerUrl() + '/settings_center', 'Wasily 完整设置中心', 'settings_center');
                post('已打开完整设置中心：' + getServerUrl() + '/settings_center');
            } catch (e) {
                post('打开失败：' + (e && e.message ? e.message : e));
            }
        } else if (m.type === 'openPanel') {
            vscode.commands.executeCommand('wasily-ai.openPanel');
        }
    });
}

function activate(context) {
    _ctx = context;
    // 先把上次进程被杀留下的「进行中」会话还原为断点，面板打开即可续跑
    try { recoverInterruptedTurns(context); } catch (_) { /* 状态损坏不影响启动 */ }
    const chatProvider = new ChatProvider(context);
    const scmProvider = new ScmProvider(context);

    // 在右侧辅助栏对话面板内打开功能页（Agent 中心 / 圆桌 / 设置等）
    function openInPanel(path, title) {
        const msg = { type: 'openFrame', url: getServerUrl() + path, title: title };
        ensureChatView().then(() => {
            // 视图首次解析时 webview 脚本尚未就绪，重试投递保证送达
            setTimeout(() => chatProvider.post(msg), 400);
            setTimeout(() => chatProvider.post(msg), 1600);
        });
    }

    context.chatProvider = chatProvider;
    _provider = chatProvider;

    // 底部状态栏「wasily 在线/离线」：真实反映 AI 服务连通状态，点击打开右侧 AI 面板
    const agentStatus = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    agentStatus.command = 'wasily-ai.openServerSettings';
    agentStatus.show();

    let statusTimer = null;
    async function refreshAgentStatus() {
        const online = await checkHealth(2500);
        if (online) {
            agentStatus.text = '$(circle-filled) wasily 在线';
            agentStatus.color = new vscode.ThemeColor('charts.green');
            agentStatus.backgroundColor = undefined;
        } else {
            agentStatus.text = '$(circle-outline) wasily 离线';
            agentStatus.color = new vscode.ThemeColor('statusBarItem.warningForeground');
            agentStatus.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        }
        agentStatus.tooltip = 'wasily AI 服务：' + (online ? '在线' : '离线') +
            '\n' + getServerUrl() + '\n点击打开 AI 服务器设置（启动 / 重启 / 配置）';
    }
    refreshAgentStatus();
    statusTimer = setInterval(refreshAgentStatus, 10000);
    // 启动时按配置同步一次 Markdown 默认打开方式（设置页改完也会即时调用）
    // 服务可能尚未就绪，失败时 8 秒后补一次
    applyMarkdownOpen().then((m) => { if (!m) setTimeout(applyMarkdownOpen, 8000); });

    context.subscriptions.push(
        agentStatus,
        { dispose() { if (statusTimer) clearInterval(statusTimer); } },
        // 活动栏视图作回退；右侧辅助侧栏视图为主入口
        (() => {
            try {
                return vscode.window.registerWebviewViewProvider('wasily-ai.panelView', chatProvider);
            } catch (e) {
                return { dispose() { } };
            }
        })(),
        vscode.window.registerWebviewViewProvider('wasily-ai.scmView', scmProvider),
        vscode.workspace.registerTextDocumentContentProvider('wasily-git', scmProvider),
        // 资源管理器右键「添加到对话」：把一个或多个文件/目录加入下一条消息的上下文
        vscode.commands.registerCommand('wasily-ai.addToChat', (uri, uris) => {
            const list = Array.isArray(uris) && uris.length ? uris : (uri ? [uri] : []);
            return chatProvider.addPaths(list);
        }),
        // 编辑器右键「添加到对话」：选中文本或整个文件加入下一条消息的上下文
        vscode.commands.registerCommand('wasily-ai.addSelectionToChat', () => {
            return chatProvider.addSelectionToChat();
        }),
        vscode.commands.registerCommand('wasily-ai.askAboutSelection', () => askAboutSelection(context)),
        vscode.commands.registerCommand('wasily-ai.editCurrentFile', () => editCurrentFile()),
        vscode.commands.registerCommand('wasily-ai.insertAtCursor', () => insertAtCursor()),
        vscode.commands.registerCommand('wasily-ai.explainSelection', () => quickCommand(context, 'explain')),
        vscode.commands.registerCommand('wasily-ai.addComments', () => quickCommand(context, 'addComments')),
        vscode.commands.registerCommand('wasily-ai.fixBug', () => quickCommand(context, 'fixBug')),
        vscode.commands.registerCommand('wasily-ai.genTests', () => quickCommand(context, 'genTests')),
        // 打开/切回右侧 AI 对话（辅助侧栏，纵向全高）
        vscode.commands.registerCommand('wasily-ai.openPanel', () => ensureChatView()),
        // 外部触发一次对话（IDE 命令 / 18753 桥调用）：把提示词送进面板，等同用户输入回车
        vscode.commands.registerCommand('wasily-ai.ask', (text) => {
            if (!text) return;
            ensureChatView();
            setTimeout(() => chatProvider.post({ type: 'ask', text: String(text) }), 700);
        }),
        vscode.commands.registerCommand('wasily-ai.openServerSettings', () => openServerSettings(context)),
        // 视图标题栏动作（与 Trae 单行布局对齐：图标放在原生标题栏）
        vscode.commands.registerCommand('wasily-ai.newSession', () => chatProvider.newSession()),
        vscode.commands.registerCommand('wasily-ai.toggleHistory', () => chatProvider.post({ type: 'toggleHistory' })),
        vscode.commands.registerCommand('wasily-ai.openScm', () => ensureScmView()),
        // 内置 Git 提示（不再下载外部 git；真正的操作在「Wasily Git」扩展里）
        vscode.commands.registerCommand('wasily-ai.ensureGit', async () => {
            vscode.window.showInformationMessage(gitRuntimeHint(), '打开源代码管理')
                .then((pick) => {
                    if (pick === '打开源代码管理') {
                        vscode.commands.executeCommand('workbench.view.scm');
                    }
                });
        }),
        vscode.commands.registerCommand('wasily-ai.togglePorts', () => chatProvider.post({ type: 'togglePorts' })),
        vscode.commands.registerCommand('wasily-ai.openAgent', () => openAgentCenter(context, '')),
        vscode.commands.registerCommand('wasily-ai.openAgentChat', () => {
            return revealWasilyPanel();
        }),
        vscode.commands.registerCommand('wasily-ai.openGroup', () => openInPanel('/group', '多 AI 圆桌（你的发言最高优先级）')),
        vscode.commands.registerCommand('wasily-ai.openSettings', (t) =>
            openSettingsCenter(context, typeof t === 'string' ? t : '')),
        vscode.commands.registerCommand('wasily-ai.openChangelog', () => openChangelogPanel(context)),
        // 检查更新：探测官方/镜像可达性，失败时给出原因与替代方案
        vscode.commands.registerCommand('wasily-ai.checkForUpdates', () => checkForUpdateDiagnostics()),
        vscode.commands.registerCommand('wasily-ai.selfCheck', () => chatProvider.selfCheck()),
        // 中央预览：wasily 应用页面改用插件原生 webview（不再走 simple-browser，避免空白浮窗）
        vscode.commands.registerCommand('wasily-ai.preview', async () => {
            try {
                await openNativeWebPanel(getServerUrl() + '/', 'wasily', 'wasilyApp');
            } catch (e) {
                vscode.window.showWarningMessage('无法打开 wasily 页面：' + (e && e.message));
            }
        }),
        // 端口自动转发：在面板内展示本机端口表（探测 → 点击在浏览器打开）
        vscode.commands.registerCommand('wasily-ai.showPorts', async () => {
            await ensureChatView();
            setTimeout(() => chatProvider.pushPorts(), 600);
            setTimeout(() => chatProvider.post({ type: 'showPorts' }), 700);
        }),
        // 退出时注销内联补全 provider
        { dispose() { if (inlineDisp) { inlineDisp.dispose(); inlineDisp = null; } } },
        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration('wasily-ai.inlineSuggest')) {
                updateInlineProvider();
            }
        })
    );
    updateInlineProvider();

    // 启动即在右侧辅助侧栏展开 AI 对话（等编辑器恢复完布局后再唤出）
    setTimeout(() => {
        ensureChatView().then(undefined, () => { /* 忽略 */ });
    }, 900);

    // 启动后自动展开底部面板：依次激活 问题→终端→调试控制台→输出，
    // 保证四个 Tab 齐备且最终停在「输出」（toggle 类命令只在目标视图非激活时才切换，不会收起面板）
    setTimeout(() => {
        const seq = [
            'workbench.actions.view.problems',
            'workbench.action.terminal.toggleTerminal',
            'workbench.debug.action.toggleRepl',
            'workbench.action.output.toggleOutput'
        ];
        seq.forEach((cmd, i) => setTimeout(() => {
            vscode.commands.executeCommand(cmd).then(undefined, () => { /* 忽略 */ });
        }, i * 260));
    }, 1800);

    // 更新后自动弹出更新日志（延迟执行，避开启动期布局恢复）
    setTimeout(() => { maybeShowChangelogOnUpdate(context); }, 2600);

    startIdeBridge(context);

    // Agent 中心/对话已改为插件原生 webview，旧版本留下的 Simple Browser 标签页
    // 会以「有地址栏但内容空白」的浮动组形式被恢复，这里在启动后清掉它们。
    setTimeout(() => { closeStaleSimpleBrowserTabs(); }, 1200);
}

async function closeStaleSimpleBrowserTabs() {
    try {
        const info = [];
        for (const group of vscode.window.tabGroups.all) {
            for (const tab of group.tabs) {
                const input = tab.input;
                const kind = input && input.viewType ? ('webview:' + input.viewType)
                    : (input && input.uri ? ('uri:' + input.uri.toString()) : String(input));
                info.push(kind);
            }
        }
        const line = '[TABS] groups=' + vscode.window.tabGroups.all.length + ' ' + JSON.stringify(info);
        console.log(line);
        try { require('fs').writeFileSync(__dirname + '/_tabs_debug.txt', line + '\n'); } catch (e2) { /* ignore */ }
    } catch (e) { console.log('[TABS] dump failed: ' + e.message); }
    try {
        for (const group of vscode.window.tabGroups.all) {
            for (const tab of group.tabs) {
                const input = tab.input;
                if (input instanceof vscode.TabInputWebview && /simpleBrowser/i.test(input.viewType || '')) {
                    try { await vscode.window.tabGroups.close(tab, true); } catch (e) { /* ignore */ }
                }
            }
        }
    } catch (e) { /* ignore */ }
}

// ==================== IDE 桥：AI 服务直连 IDE（终端执行 / 文件打开 / 文件编辑） ====================
// 监听 127.0.0.1:18753，由 api_server 的 ide_* 用户工具调用

// IDE ↔ Agent 联动：采集当前编辑器现场（文件/选区/诊断/打开列表）
function collectIdeContext() {
    const out = {
        workspace: '', file: '', language: '', selection: '',
        line: [], open_files: [], diagnostics: []
    };
    try {
        const wf = vscode.workspace.workspaceFolders;
        if (wf && wf.length && wf[0].uri) out.workspace = wf[0].uri.fsPath;
    } catch (e) { /* ignore */ }
    // 焦点可能在输出面板/webview 上：优先取活动编辑器，其次回退到任一可见的磁盘文件编辑器
    let ed = vscode.window.activeTextEditor;
    try {
        if (!ed || !ed.document || !ed.document.uri || ed.document.uri.scheme !== 'file') {
            const vis = vscode.window.visibleTextEditors || [];
            ed = vis.find((v) => v && v.document && v.document.uri && v.document.uri.scheme === 'file') || ed;
        }
    } catch (e) { /* ignore */ }
    if (ed && ed.document) {
        const doc = ed.document;
        try {
            out.file = (doc.uri && doc.uri.scheme === 'file') ? doc.uri.fsPath : String(doc.uri || '');
            out.language = doc.languageId || '';
            const sel = ed.selection;
            if (sel && !sel.isEmpty) out.selection = doc.getText(sel).slice(0, 4000);
            if (sel) out.line = [sel.start.line + 1, sel.end.line + 1];
        } catch (e) { /* ignore */ }
    }
    // 打开的文件（按标签页顺序，最多 10 个）
    try {
        const groups = (vscode.window.tabGroups && vscode.window.tabGroups.all) || [];
        const files = [];
        groups.forEach((g) => (g.tabs || []).forEach((t) => {
            const inp = t.input;
            const uri = inp && inp.uri;
            if (uri && uri.scheme === 'file' && files.indexOf(uri.fsPath) < 0) files.push(uri.fsPath);
        }));
        out.open_files = files.slice(0, 10);
    } catch (e) { /* ignore */ }
    // 当前文件的诊断（最多 10 条）
    try {
        if (out.file) {
            const uri = vscode.Uri.file(out.file);
            const diags = vscode.languages.getDiagnostics(uri) || [];
            out.diagnostics = diags.slice(0, 10).map((d) => {
                const ln = (d.range && d.range.start) ? d.range.start.line + 1 : 0;
                const sev = ['', '错误', '警告', '信息', '提示'][d.severity] || '';
                return sev + ' L' + ln + ': ' + String(d.message || '').slice(0, 140);
            });
        }
    } catch (e) { /* ignore */ }
    return out;
}

// ===== 多窗口上下文：每个窗口一份现场快照 + 桥侧多客户端注册 =====
let _activeWindowId = 'default';
const _windowCtx = {};                 // windowId -> 最近一次现场快照
const _ideClients = new Map();         // windowId -> { kind, sessionId, ts }
const _ideClientId = 'ext-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6);
function registerIdeClient(windowId, info) {
    const wid = String(windowId || '');
    if (!wid) return;
    const prev = _ideClients.get(wid) || {};
    _ideClients.set(wid, Object.assign({}, prev, info || {}, { ts: Date.now() }));
}

let _ideCtxTimer = null;
function pushIdeContext(delay, windowId) {
    if (_ideCtxTimer) clearTimeout(_ideCtxTimer);
    _ideCtxTimer = setTimeout(async () => {
        _ideCtxTimer = null;
        const explicit = !!windowId;
        const wid = String(windowId || _activeWindowId || 'default');
        try {
            // 切回某窗口时用它自己的快照（不拿当前全局编辑器去覆盖它）；
            // 编辑器变化事件（无 windowId）才更新当前窗口的现场。
            const ctx = (explicit && _windowCtx[wid]) ? _windowCtx[wid] : collectIdeContext();
            _windowCtx[wid] = ctx;
            await fetch(getServerUrl() + '/v1/ide/context', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({}, ctx, {
                    window_id: wid, client_id: _ideClientId
                }))
            });
        } catch (e) { /* 服务未启动时静默 */ }
    }, typeof delay === 'number' ? delay : 500);
}

function startIdeContextSync(context) {
    try {
        context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(() => pushIdeContext()));
        context.subscriptions.push(vscode.window.onDidChangeTextEditorSelection(() => pushIdeContext()));
        context.subscriptions.push(vscode.workspace.onDidChangeTextDocument(() => pushIdeContext(800)));
        if (vscode.languages.onDidChangeDiagnostics) {
            context.subscriptions.push(vscode.languages.onDidChangeDiagnostics(() => pushIdeContext(1200)));
        }
        if (vscode.workspace.onDidChangeWorkspaceFolders) {
            context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => pushIdeContext(300)));
        }
    } catch (e) { /* 旧版本 API 缺失时忽略 */ }
    pushIdeContext(300);   // 启动即上报一次，Agent 端立刻能看到现场
    // 心跳：即使没有编辑动作也每 30 秒上报一次，避免服务端 online（90 秒）判离线
    const hb = setInterval(() => { try { pushIdeContext(0); } catch (e) { } }, 30000);
    context.subscriptions.push({ dispose() { clearInterval(hb); } });
}

// 磁盘 ↔ 编辑器同步：外部进程（Agent 的 file_write / 脚本）改写文件时，
// 已打开的文件自动跟进；本地有未保存修改则弹提示条，绝不静默覆盖。
function startDiskSync(context) {
    let timer = null;
    const pending = new Set();
    context.subscriptions.push({ dispose() { if (timer) { clearTimeout(timer); timer = null; } } });

    const handle = async (uri) => {
        try {
            const doc = vscode.workspace.textDocuments.find(
                (d) => d && !d.isUntitled && d.uri && d.uri.fsPath === uri.fsPath);
            if (!doc) return;
            const bytes = await vscode.workspace.fs.readFile(uri);
            const diskText = new TextDecoder('utf-8').decode(bytes);
            if (diskText === doc.getText()) return;   // 已一致（本扩展自己保存的 / 编辑器已自动跟进）→ 不打扰
            if (doc.isDirty) {
                // 有未保存修改：只提示，让用户决定，绝不静默覆盖
                const pick = await vscode.window.showWarningMessage(
                    '文件已在磁盘上更改，是否重新加载？\n' + uri.fsPath,
                    { modal: false }, '重新加载', '保留我的修改');

                if (pick === '重新加载') {
                    try { await vscode.commands.executeCommand('workbench.action.files.revert', doc.uri); } catch (_) { }
                }
                return;
            }
            await vscode.commands.executeCommand('workbench.action.files.revert', doc.uri);
        } catch (e) { /* 文件被删/权限问题忽略 */ }
    };

    try {
        const watcher = vscode.workspace.createFileSystemWatcher('**/*');
        context.subscriptions.push(watcher);
        context.subscriptions.push(watcher.onDidChange((uri) => {
            pending.add(uri.fsPath);
            if (timer) return;
            timer = setTimeout(() => {
                timer = null;
                const uris = [];
                for (const p of pending) { uris.push(vscode.Uri.file(p)); }
                pending.clear();
                uris.forEach((u) => { handle(u); });
            }, 400);
        }));
    } catch (e) { /* 无工作区时忽略 */ }
}

function startIdeBridge(context) {
    const http = require('http');
    const { McpHost } = require('./mcpHost');
    let mcpHost = null;
    let mcpReady = null;
    // MCP 统一由后端 mcp_hub 托管（agent 窗口 / 对话 / IDE 共用同一套连接）；
    // 本地 Host 只在后端不可达时惰性启动，作为兜底。
    const getMcpHost = () => {
        if (!mcpHost) {
            mcpHost = new McpHost();
            mcpReady = mcpHost.start().catch((e) => {
                console.error('[MCP] 本地兜底 Host 启动失败', e);
            });
        }
        return mcpHost;
    };
    const waitMcpHost = async () => {
        const host = getMcpHost();
        if (mcpReady) await mcpReady;
        return host;
    };
    const backendBase = () => {
        try { return getServerUrl(); } catch (e) { return 'http://127.0.0.1:8000'; }
    };
    const backendJson = async (p, opt) => {
        const r = await fetch(backendBase() + p, Object.assign({ cache: 'no-store' }, opt || {}));
        const d = await r.json();
        if (!d || d.ok === false) throw new Error((d && d.error) || '后端 MCP 不可用');
        return d;
    };
    const srv = http.createServer((req, res) => {
        const send = (code, obj) => {
            const body = JSON.stringify(obj);
            // workbench 页面（vscode-file:// 源）跨源调用需要 CORS 头
            res.writeHead(code, {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Content-Type',
                'Access-Control-Allow-Methods': 'GET,POST,OPTIONS'
            });
            res.end(body);
        };
        if (req.method === 'OPTIONS') { send(200, { ok: true }); return; }
        if (req.method === 'GET' && req.url === '/health') { send(200, { ok: true }); return; }
        if (req.method === 'GET' && (req.url === '/mcp/servers' || req.url === '/mcp/tools')) {
            (async () => {
                try {
                    const d = await backendJson('/v1/mcp/servers');
                    if (req.url === '/mcp/servers') {
                        send(200, { ok: true, source: 'backend', servers: d.servers });
                        return;
                    }
                    const tools = [];
                    (d.servers || []).forEach((s) => {
                        const ns = (s.namespace === undefined || s.namespace === null)
                            ? ('mcp__' + s.name + '__') : String(s.namespace);
                        (s.tools || []).forEach((t) => {
                            if (!t || !t.name) return;
                            tools.push({
                                name: ns + t.name, server: s.name, raw: t.name,
                                description: '[MCP:' + s.name + '] ' + (t.description || ''),
                                inputSchema: t.inputSchema || { properties: {} },
                                readonly: !!t.readonly
                            });
                        });
                    });
                    send(200, { ok: true, source: 'backend', tools });
                } catch (e) {
                    // 后端不可达时才退回本地 Host
                    waitMcpHost().then((host) => {
                        if (req.url === '/mcp/servers') send(200, { ok: true, source: 'local', servers: host.status() });
                        else send(200, { ok: true, source: 'local', tools: host.tools() });
                    }).catch((err) => send(503, { ok: false, error: String((err && err.message) || err) }));
                }
            })();
            return;
        }
        if (req.method === 'GET' && req.url === '/notify_stream') {
            // workbench 渲染进程订阅任务完成事件（SSE），拿到后用 Electron Notification 弹系统通知
            res.writeHead(200, {
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*'
            });
            res.write(': connected\n\n');
            _notifyClients.add(res);
            const hb = setInterval(() => {
                try { res.write(': hb\n\n'); } catch (e) { }
            }, 25000);
            req.on('close', () => { clearInterval(hb); _notifyClients.delete(res); });
            return;
        }
        if (req.method === 'GET' && req.url.indexOf('/context') === 0) {
            // IDE ↔ Agent 联动：外部页面实时拉取编辑器现场；?windowId=xx 取该窗口的快照
            let qWid = '';
            try { qWid = new URL(req.url, 'http://127.0.0.1').searchParams.get('windowId') || ''; } catch (e) { }
            const ctx = (qWid && _windowCtx[qWid]) ? _windowCtx[qWid] : collectIdeContext();
            send(200, Object.assign({ ok: true, online: true, windowId: qWid || _activeWindowId }, ctx));
            return;
        }
        if (req.method === 'GET' && req.url.indexOf('/open_url') === 0) {
            // 下行通道：后端 AI 让 IDE 在中央 webview 标签里打开网页
            let target = '', titleText = '';
            try {
                const q = new URL(req.url, 'http://127.0.0.1').searchParams;
                target = q.get('url') || '';
                titleText = q.get('title') || '';
            } catch (e) { /* 忽略 */ }
            if (!/^https?:\/\//i.test(target)) {
                send(400, { ok: false, error: 'url 必须以 http(s):// 开头' });
                return;
            }
            openNativeWebPanel(target, titleText || target, 'aiPage:' + Date.now())
                .then(() => send(200, { ok: true, url: target }))
                .catch((e) => send(500, { ok: false, error: String((e && e.message) || e) }));
            return;
        }
        if (req.method === 'GET' && req.url === '/clients') {
            // 多客户端注册表：不再只保留首个接入方
            const list = [];
            for (const [wid, info] of _ideClients) list.push(Object.assign({ windowId: wid }, info));
            send(200, { ok: true, count: list.length, clients: list, activeWindowId: _activeWindowId });
            return;
        }
        if (req.method === 'GET' && req.url.indexOf('/ui_state') === 0) {
            // 外壳界面状态持久化（如 AI 面板宽度）：globalState 跨会话保留
            let qKey = '';
            try { qKey = new URL(req.url, 'http://127.0.0.1').searchParams.get('key') || ''; } catch (e) { }
            if (!qKey) { send(400, { ok: false, error: '缺少 key' }); return; }
            send(200, { ok: true, value: context.globalState.get('wasily.uiState.' + qKey, null) });
            return;
        }
        if (req.method !== 'POST') { send(405, { ok: false, error: 'method not allowed' }); return; }
        let raw = '';
        req.on('data', (c) => { raw += c; if (raw.length > 2e6) req.destroy(); });
        req.on('end', async () => {
            let body = {};
            try { body = raw ? JSON.parse(raw) : {}; } catch (e) {
                send(400, { ok: false, error: 'bad json' }); return;
            }
            try {
                if (req.url === '/mcp/reload') {
                    try {
                        const d = await backendJson('/v1/mcp/reload',
                            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
                        send(200, { ok: true, source: 'backend', servers: d.servers });
                    } catch (e) {
                        const host = await waitMcpHost();
                        send(200, { ok: true, source: 'local', servers: await host.reload() });
                    }
                    return;
                }
                if (req.url === '/mcp/call') {
                    try {
                        const name = String(body.name || '');
                        const list = await backendJson('/v1/mcp/servers');
                        let server = '', raw = '';
                        (list.servers || []).forEach((s) => {
                            if (server) return;
                            const ns = (s.namespace === undefined || s.namespace === null)
                                ? ('mcp__' + s.name + '__') : String(s.namespace);
                            const hit = (s.tools || []).find((t) => t.name && (ns + t.name === name || t.name === name));
                            if (hit) { server = s.name; raw = hit.name; }
                        });
                        if (!server) throw new Error('未找到工具 ' + name);
                        const d = await backendJson('/v1/mcp/call', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ server, tool: raw, arguments: body.arguments || {} })
                        });
                        const text = typeof d.result === 'string' ? d.result : JSON.stringify(d.result);
                        send(200, { ok: true, source: 'backend', text });
                    } catch (e) {
                        const host = await waitMcpHost();
                        const text = await host.call(String(body.name || ''), body.arguments || {});
                        send(200, { ok: true, source: 'local', text });
                    }
                    return;
                }
                if (req.url === '/register_client') {
                    // 多客户端注册：面板窗口 / 第二个 IDE 实例都可注册，桥按 windowId 分别留存
                    const wid = String(body.windowId || body.clientId || '');
                    if (!wid) { send(400, { ok: false, error: 'windowId 为空' }); return; }
                    registerIdeClient(wid, body);
                    if (body.context && typeof body.context === 'object') _windowCtx[wid] = body.context;
                    send(200, { ok: true, windowId: wid, count: _ideClients.size });
                    return;
                }
                if (req.url === '/notify_ack') {
                    // 渲染进程回执：rendered=false 时立即退化为 PowerShell 弹窗
                    try {
                        fs.appendFileSync(path.join(__dirname, 'notify.log'),
                            '[' + new Date().toISOString() + '] ack id=' + String(body.id || '') +
                            ' rendered=' + (body.rendered !== false) + '\n');
                    } catch (e) { }
                    ackSysNotify(String(body.id || ''), body.rendered !== false);
                    send(200, { ok: true });
                    return;
                }
                if (req.url === '/notify_probe') {
                    // 渲染进程能力探测（Notification 是否可用），写日志便于真机排查
                    try {
                        fs.appendFileSync(path.join(__dirname, 'notify.log'),
                            '[' + new Date().toISOString() + '] probe ' + JSON.stringify(body) + '\n');
                    } catch (e) { }
                    send(200, { ok: true });
                    return;
                }
                if (req.url === '/notify_click') {
                    // 系统通知被点击：主进程恢复窗口后 → 这里 → 切会话并高亮对应结果
                    const id = String(body.id || '');
                    const sid = String(body.sid || '');
                    try {
                        fs.appendFileSync(path.join(__dirname, 'notify.log'),
                            '[' + new Date().toISOString() + '] click id=' + id + ' sid=' + sid + '\n');
                    } catch (e) { }
                    try {
                        const pv = _provider || context.chatProvider;
                        if (pv) pv.onNotifyClick(id, sid);
                    } catch (e) { /* 面板未就绪时忽略 */ }
                    send(200, { ok: true, id: id, sid: sid });
                    return;
                }
                if (req.url === '/notify_selftest') {
                    // 自检：走与真实任务完成完全相同的提示链路（系统通知 / 窗口内 toast）
                    try {
                        const pv = _provider || context.chatProvider;
                        let channel = 'no-provider';
                        if (pv) {
                            const st = (body.state === 'error' || body.state === 'confirm') ? body.state : 'done';
                            channel = pv.notifyTask({
                                state: st, sid: String(body.sid || ('selftest-' + Date.now())),
                                title: '通知自检', summary: '面板收起 + IDE 最小化', ms: 4321,
                                ackDelayMs: Math.max(0, Number(body.ackDelayMs || 0))
                            });
                        }
                        send(200, { ok: true, channel: channel, enabled: notifySystemEnabled(),
                                    clients: _notifyClients.size,
                                    ctxFrozen: Object.isFrozen(context), extPath: __dirname });
                    } catch (e) {
                        send(500, { ok: false, error: String((e && e.message) || e) });
                    }
                    return;
                }
                if (req.url === '/run_terminal') {
                    // 在名为「wasily AI」的集成终端中执行命令（不存在则创建），并捕获输出回传
                    const cmd = String(body.command || '').trim();
                    if (!cmd) { send(400, { ok: false, error: 'command 为空' }); return; }
                    // 镜像到后端命令总线：Agent 窗口与 IDE 面板看到同一条命令的实时状态与逐行输出
                    const mirror = (payload) => fetch(getServerUrl() + '/v1/commands/ingest', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    }).then((r) => r.json()).catch(() => null);
                    const folders = vscode.workspace.workspaceFolders;
                    const mirrorStart = await mirror({
                        action: 'start', command: cmd, source: 'ide', owner: 'ide',
                        cwd: folders && folders[0] ? folders[0].uri.fsPath : ''
                    });
                    const mirrorId = mirrorStart && mirrorStart.run ? mirrorStart.run.id : '';
                    let term = vscode.window.terminals.find((t) => t.name === 'wasily AI');
                    if (!term) term = vscode.window.createTerminal({ name: 'wasily AI' });
                    term.show(false);
                    // shell integration 异步就绪：最多等 3 秒，拿到就能读输出
                    let si = term.shellIntegration;
                    for (let i = 0; i < 30 && !si; i++) {
                        await new Promise((r) => setTimeout(r, 100));
                        si = term.shellIntegration;
                    }
                    if (si && typeof si.executeCommand === 'function') {
                        try {
                            const exec = si.executeCommand(cmd);
                            let out = '';
                            let tail = '';
                            if (exec && typeof exec.read === 'function') {
                                for await (const chunk of exec.read()) {
                                    const text = String(chunk || '');
                                    out += text;
                                    if (mirrorId) {
                                        // 按行切分实时上报；末段不完整行留到下一块拼接
                                        tail += text;
                                        const parts = tail.split(/\r?\n/);
                                        tail = parts.pop();
                                        if (parts.length) {
                                            mirror({ action: 'line', run_id: mirrorId,
                                                     lines: parts.map((t) => ({ stream: 'stdout', text: t })) });
                                        }
                                    }
                                    if (out.length > 20000) break;
                                }
                            }
                            if (mirrorId && tail) {
                                mirror({ action: 'line', run_id: mirrorId,
                                         lines: [{ stream: 'stdout', text: tail }] });
                            }
                            // 去掉 shell integration 的 OSC 633 标记与 ANSI 控制序列
                            out = out
                                .replace(/\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)/g, '')
                                .replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, '')
                                .replace(/\r\n/g, '\n');
                            let code = null;
                            try {
                                const c = await exec.exitCode;
                                code = (c === undefined || c === null) ? null : c;
                            } catch (_) { /* 无退出码 */ }
                            if (mirrorId) mirror({ action: 'finish', run_id: mirrorId, exit_code: code });
                            send(200, {
                                ok: true, output: out.slice(-8000), exitCode: code,
                                detail: '已在终端 wasily AI 执行并捕获输出'
                            });
                            return;
                        } catch (e) { /* shell integration 不可用 → 回退 sendText */ }
                    }
                    if (mirrorId) {
                        // 拿不到 shell integration 就没有输出流，也要让状态收尾，不能一直挂着「运行中」
                        mirror({ action: 'line', run_id: mirrorId,
                                 lines: [{ stream: 'stderr', text: '该终端未启用 shell integration，输出无法捕获' }] });
                        mirror({ action: 'finish', run_id: mirrorId, exit_code: null });
                    }
                    term.sendText(cmd, true);
                    send(200, {
                        ok: true, output: '', exitCode: null,
                        detail: '已发送到终端 wasily AI 执行（该终端未启用 shell integration，输出无法捕获）'
                    });
                    return;
                }
                if (req.url === '/recent') {
                    // 标题栏「项目介绍」菜单：返回最近打开的工作区列表
                    const toWinPath = (p) => {
                        let s = String(p || '').replace(/^\/([a-zA-Z]):/, (m, d) => d.toUpperCase() + ':');
                        s = s.replace(/\//g, '\\');
                        return s.replace(/\\+$/, '');
                    };
                    const out = { workspaces: [], files: [] };
                    try {
                        const r = await vscode.commands.executeCommand('_workbench.getRecentlyOpened');
                        const ws = (r && r.workspaces) || [];
                        out.workspaces = ws.slice(0, 8).map((w) => {
                            const uri = w.folderUri || w.workspace || {};
                            const p = typeof uri === 'object' ? decodeURIComponent(String(uri.path || uri.fsPath || '')) : String(uri);
                            return { label: w.label || p.split(/[\\/]/).pop() || p, path: toWinPath(p), workspace: !w.folderUri };
                        }).filter((w) => w.path);
                        const fl = (r && r.files) || [];
                        out.files = fl.slice(0, 5).map((f) => {
                            const p = typeof f.fileUri === 'object' ? decodeURIComponent(String(f.fileUri.path || '')) : String(f.fileUri);
                            return { label: f.label || p.split(/[\\/]/).pop(), path: toWinPath(p) };
                        }).filter((f) => f.path);
                    } catch (e) { /* 旧版本无该命令时返回空列表 */ }
                    send(200, Object.assign({ ok: true }, out));
                    return;
                }
                if (req.url === '/open_recent') {
                    // 在当前窗口打开最近的工作区/文件夹
                    const p = String(body.path || '');
                    if (!p) { send(400, { ok: false, error: 'path 为空' }); return; }
                    await vscode.commands.executeCommand('vscode.openFolder', vscode.Uri.file(p), { forceNewWindow: false });
                    send(200, { ok: true });
                    return;
                }
                if (req.url === '/open_file') {
                    const p = String(body.path || '');
                    if (!p) { send(400, { ok: false, error: 'path 为空' }); return; }
                    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(p));
                    const ed = await vscode.window.showTextDocument(doc, { preview: false });
                    const line = body.line | 0;
                    if (line > 0) {
                        const ln = Math.max(0, Math.min(doc.lineCount - 1, line - 1));
                        const pos = new vscode.Position(ln, 0);
                        ed.selection = new vscode.Selection(pos, pos);
                        ed.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
                    }
                    send(200, { ok: true, detail: '已打开 ' + p });
                    return;
                }
                if (req.url === '/read_file') {
                    // 读编辑器缓冲区：文件已在编辑器打开时返回含未保存修改的内容
                    const p = String(body.path || '');
                    if (!p) { send(400, { ok: false, error: 'path 为空' }); return; }
                    const uri = vscode.Uri.file(p);
                    const open = (vscode.workspace.textDocuments || []).find(
                        (d) => d.uri && d.uri.fsPath && d.uri.fsPath.toLowerCase() === uri.fsPath.toLowerCase());
                    let text = '';
                    let saved = true;
                    if (open) {
                        text = open.getText();
                        saved = !open.isDirty;
                    } else {
                        try {
                            text = fs.readFileSync(p, 'utf8');
                        } catch (e) {
                            send(404, { ok: false, error: '读取失败: ' + String((e && e.message) || e) });
                            return;
                        }
                    }
                    send(200, { ok: true, path: p, text: text, saved: saved });
                    return;
                }
                if (req.url === '/edit_file') {
                    // old_string 非空 = 替换首个匹配；为空 = 全文替换。编辑后立即保存。
                    const p = String(body.path || '');
                    const oldStr = String(body.old_string || '');
                    const newStr = String(body.new_string || '');
                    if (!p) { send(400, { ok: false, error: 'path 为空' }); return; }
                    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(p));
                    const ed = await vscode.window.showTextDocument(doc, { preview: false });
                    if (oldStr) {
                        const full = doc.getText();
                        const idx = full.indexOf(oldStr);
                        if (idx < 0) { send(404, { ok: false, error: '未找到待替换文本' }); return; }
                        await ed.edit((eb) => eb.replace(
                            new vscode.Range(doc.positionAt(idx), doc.positionAt(idx + oldStr.length)), newStr));
                    } else {
                        await ed.edit((eb) => eb.replace(
                            new vscode.Range(doc.positionAt(0), doc.positionAt(doc.getText().length)), newStr));
                    }
                    await doc.save();
                    send(200, { ok: true, detail: '已编辑并保存 ' + p });
                    return;
                }
                if (req.url === '/open_settings') {
                    // 标题栏/页面直连：在中央编辑器区打开设置中心
                    openSettingsCenter(context, String(body.tab || '')).then(
                        () => send(200, { ok: true, detail: '设置中心已打开' }),
                        (e) => send(500, { ok: false, error: String((e && e.message) || e) }));
                    return;
                }
                if (req.url === '/workbench_drag_hint') {
                    // 外部文件拖动由 Workbench 接收，高亮则由输入框自身绘制以保持边界一致
                    if (!_provider) { send(503, { ok: false, error: 'AI 面板尚未就绪' }); return; }
                    _provider.post({ type: 'externalDrag', active: !!body.active });
                    send(200, { ok: true });
                    return;
                }
                if (req.url === '/workbench_drag') {
                    // Workbench 捕获到、但未进入 Webview 的资源拖放，直接复用扩展附件链路
                    const entry = {
                        at: new Date().toISOString(), source: 'workbench',
                        stage: String(body.stage || ''), types: Array.isArray(body.types) ? body.types : [],
                        fileCount: Number(body.fileCount) || 0,
                        paths: Array.isArray(body.paths) ? body.paths : [],
                        observedPaths: Array.isArray(body.observedPaths) ? body.observedPaths : [],
                        accepted: !!body.accepted,
                        values: body.values && typeof body.values === 'object' ? body.values : {},
                        target: String(body.target || ''), targetId: String(body.targetId || ''),
                        targetClass: String(body.targetClass || ''),
                        clientX: Number(body.clientX), clientY: Number(body.clientY),
                        viewRect: body.viewRect && typeof body.viewRect === 'object' ? body.viewRect : null,
                        hit: body.hit && typeof body.hit === 'object' ? body.hit : null,
                        frames: Array.isArray(body.frames) ? body.frames : [],
                        defaultPrevented: !!body.defaultPrevented
                    };
                    try { fs.appendFileSync(path.join(__dirname, 'drag-debug.log'), JSON.stringify(entry) + '\n'); }
                    catch (e) { /* 诊断失败不影响拖放 */ }
                    if (entry.stage === 'drop' && entry.paths.length) {
                        if (!_provider) { send(503, { ok: false, error: 'AI 面板尚未就绪' }); return; }
                        await _provider.attachPaths(entry.paths);
                    }
                    send(200, { ok: true });
                    return;
                }
                if (req.url === '/ui_state') {
                    // 外壳界面状态持久化（如 AI 面板宽度）：globalState 跨会话保留
                    const stKey = String(body.key || '');
                    if (!stKey) { send(400, { ok: false, error: '缺少 key' }); return; }
                    if (body.value === undefined) {
                        send(200, { ok: true, value: context.globalState.get('wasily.uiState.' + stKey, null) });
                        return;
                    }
                    await context.globalState.update('wasily.uiState.' + stKey, body.value);
                    send(200, { ok: true });
                    return;
                }
                if (req.url === '/command') {
                    // 标题栏按钮直连执行 workbench 命令（白名单，防任意命令注入）
                    const cmd = String(body.command || '');
                    const cmdArgs = Array.isArray(body.args) ? body.args : [];
                    const allow = [
                        'workbench.action.navigateBack',
                        'workbench.action.navigateForward',
                        'workbench.action.toggleSidebarVisibility',
                        'workbench.action.togglePanel',
                        'workbench.action.toggleAuxiliaryBar',
                        'workbench.action.moveFocusedView',
                        'workbench.action.focusAuxiliaryBar',
                        'workbench.action.increaseViewSize',
                        'workbench.action.decreaseViewSize',
                        'workbench.action.maximizeAuxiliaryBar',
                        'workbench.action.restoreAuxiliaryBar',
                        'workbench.action.toggleMaximizedAuxiliaryBar',
                        'wasily-ai.panelView.focus',
                        'workbench.action.closeQuickOpen',
                        'workbench.action.selectTheme',
                        'workbench.action.findInFiles',
                        'workbench.action.quickOpen',
                        'wasily-ai.openPanel',
                        'wasily-ai.openScm',
                        'wasily-ai.ask',
                        'wasily-ai.openAgent',
                        'wasily-ai.openAgentChat',
                        'wasily-ai.preview',
                        'wasily-ai.openGroup',
                        'wasily-ai.openChangelog',
                        'wasily-ai.addToChat',
                        'renameFile', 'deleteFile',
                        'filesExplorer.copy', 'filesExplorer.cut', 'filesExplorer.paste',
                        'copyFilePath', 'copyRelativeFilePath',
                        'revealFileInOS', 'explorer.openToSide',
                        'explorer.newFile', 'explorer.newFolder',
                        'wasily-ai.showPorts',
                        'workbench.action.reloadWindow',
                        'workbench.action.restartExtensionHost',
                        'workbench.action.files.openFolder',
                        'git.clone',
                        'workbench.action.remote.showMenu',
                        'workbench.action.openRecent'
                    ];
                    if (allow.indexOf(cmd) < 0) { send(403, { ok: false, error: 'command not allowed' }); return; }
                    vscode.commands.executeCommand(cmd, ...cmdArgs).then(
                        () => send(200, { ok: true }),
                        (e) => send(500, { ok: false, error: String((e && e.message) || e) }));
                    return;
                }
                if (req.url === '/lsp_definition' || req.url === '/lsp_references'
                    || req.url === '/lsp_symbols' || req.url === '/lsp_rename') {
                    // LSP 语义级能力：全走 VSCode 语言服务通道（executeDefinitionProvider /
                    // executeReferenceProvider / executeDocumentRenameProvider），不靠正则猜符号
                    const p = String(body.path || '');
                    const lineNo = Math.max(1, parseInt(body.line || 1, 10));
                    const colNo = Math.max(0, parseInt(body.column || 1, 10) - 1);
                    const pos = new vscode.Position(lineNo - 1, colNo);
                    const uriFor = async () => {
                        if (p) {
                            const fsP = vscode.Uri.file(p).fsPath;
                            const doc = vscode.workspace.textDocuments.find(
                                (d) => d && d.uri && d.uri.fsPath === fsP);
                            return doc ? doc.uri : vscode.Uri.file(p);
                        }
                        const ed = vscode.window.activeTextEditor;
                        if (!ed) throw new Error('没有活动编辑器，且未传 path');
                        return ed.document.uri;
                    };
                    const norm = (locs) => (locs || []).slice(0, 200).map((l) => {
                        const t = l.targetUri || l.uri || (l.location && l.location.uri);
                        const r = l.targetRange || l.range || (l.location && l.location.range);
                        return {
                            path: t && t.fsPath ? t.fsPath : String(t || ''),
                            line: r && r.start ? r.start.line + 1 : 0,
                            column: r && r.start ? r.start.character + 1 : 0
                        };
                    });
                    try {
                        const uri = await uriFor();
                        if (req.url === '/lsp_symbols') {
                            const q = String(body.query || '');
                            const raw = q
                                ? await vscode.commands.executeCommand('vscode.executeWorkspaceSymbolProvider', q)
                                : await vscode.commands.executeCommand('vscode.executeDocumentSymbolProvider', uri);
                            const flat = [];
                            const walk = (arr) => (arr || []).forEach((s) => {
                                flat.push({
                                    name: s.name, kind: String(s.kind),
                                    line: s.range && s.range.start ? s.range.start.line + 1 : 0,
                                    container: s.containerName || ''
                                });
                                if (s.children) walk(s.children);
                            });
                            walk(raw);
                            send(200, { ok: true, count: flat.length, symbols: flat.slice(0, 300) });
                            return;
                        }
                        if (req.url === '/lsp_definition') {
                            const res = await vscode.commands.executeCommand(
                                'vscode.executeDefinitionProvider', uri, pos);
                            const list = norm(res);
                            send(200, { ok: true, count: list.length, locations: list });
                            return;
                        }
                        if (req.url === '/lsp_references') {
                            const res = await vscode.commands.executeCommand(
                                'vscode.executeReferenceProvider', uri, pos);
                            const list = norm(res);
                            send(200, { ok: true, count: list.length, locations: list });
                            return;
                        }
                        const newName = String(body.new_name || '');
                        if (!newName) { send(400, { ok: false, error: 'new_name 为空' }); return; }
                        const we = await vscode.commands.executeCommand(
                            'vscode.executeDocumentRenameProvider', uri, pos, newName);
                        if (!we) {
                            send(404, {
                                ok: false,
                                error: '当前语言服务不支持重命名（该文件类型未安装语言服务器）'
                            });
                            return;
                        }
                        const applied = await vscode.workspace.applyEdit(we);
                        const files = [];
                        try { we.entries().forEach(([u]) => files.push(u.fsPath)); } catch (e) { }
                        send(applied ? 200 : 500, { ok: applied, applied: applied, files: files });
                        return;
                    } catch (e) {
                        send(500, { ok: false, error: String((e && e.message) || e) });
                        return;
                    }
                }
                send(404, { ok: false, error: 'unknown path' });
            } catch (e) {
                send(500, { ok: false, error: String((e && e.message) || e) });
            }
        });
    });
    srv.on('error', (err) => {
        // 端口被首个实例占用时不再丢弃本窗口：以客户端身份注册到已有桥，上下文继续独立上报
        if (err && err.code === 'EADDRINUSE') {
            const register = () => {
                try {
                    fetch('http://127.0.0.1:18753/register_client', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            windowId: _activeWindowId || _ideClientId, clientId: _ideClientId,
                            kind: 'secondary', context: collectIdeContext()
                        })
                    }).catch(() => { });
                } catch (e) { /* ignore */ }
            };
            register();
            const t = setInterval(register, 15000);
            context.subscriptions.push({ dispose() { clearInterval(t); } });
        }
    });
    srv.listen(18753, '127.0.0.1', () => {
        // 不再主动拉起本地 MCP：统一走后端 mcp_hub，避免同一份 mcp.json 被拉两遍
        // （Playwright 的 profile 目录等互斥资源尤其怕重复启动）。
    });
    context.subscriptions.push({ dispose() {
        try { srv.close(); } catch (e) { }
        if (mcpHost) { try { mcpHost.closeAll(); } catch (e) { } }
    } });
    // IDE ↔ Agent 联动：监听编辑器变化，节流上报当前现场
    startIdeContextSync(context);
    // 磁盘 → 编辑器：外部进程改写已打开文件时自动刷新 / 提示重载
    startDiskSync(context);

    // ===== P3-④ 调试断点集成：停在断点 → AI 自动分析栈帧并给出修复建议 =====
    // 兼容：VSCode 新版移除了 vscode.debug.onDidStop，降级跳过
    if (typeof vscode.debug.onDidStop === 'function') {
        context.subscriptions.push(
            vscode.debug.onDidStop(async (e) => {
            try {
                if (!e || !e.session) return;
                const threadId = e.threadId;
                if (threadId == null) return;
                const stackTrace = await e.session.getStackTrace(threadId);
                if (!stackTrace || !stackTrace.length) return;
                // 取顶 6 帧，带行号 + 文件路径
                const frames = stackTrace.slice(0, 6).map((f, i) => {
                    const uri = f.source && f.source.path ? f.source.path :
                        (f.source && f.source.name ? f.source.name : '');
                    return `  #${i + 1} ${f.functionName || '(anon)'} @ ${uri}:${f.line}`;
                }).join('\n');
                // 取当前帧文件源码片段（如果能找到）
                const top = stackTrace[0];
                let srcSnippet = '';
                if (top.source && top.source.path) {
                    try {
                        const editor = vscode.window.activeTextEditor;
                        if (editor && require('path').resolve(editor.document.filePath) === require('path').resolve(top.source.path)) {
                            const line = (top.line || 1) - 1;
                            const start = Math.max(0, line - 5), end = Math.min(editor.document.lineCount, line + 6);
                            const lines = [];
                            for (let i = start; i < end; i++) {
                                const marker = i === line ? '>>' : '  ';
                                lines.push(`${marker} ${i + 1}: ${editor.document.lineAt(i).text}`);
                            }
                            srcSnippet = lines.join('\n');
                        }
                    } catch (err) { /* 编辑器不匹配就跳过 */ }
                }
                const prompt = `调试器停在断点上。栈帧：\n${frames}\n\n${srcSnippet ? ('当前文件源码（带断点标记）：\n```\n' + srcSnippet + '\n```\n') : ''}请：1) 判断为什么停在这里（是正常断点命中还是异常/错误传播？）2) 如果是 bug 根因，给出具体修复代码块 3) 如果只是普通断点，说明代码此时在做什么。`;
                // 推给 AI 分析 —— 复用 callIdeStream explain 模式，结果自动进 chat 面板
                const activeEditor = vscode.window.activeTextEditor;
                let codeSnippet = '';
                let filePath = '';
                if (activeEditor) {
                    codeSnippet = activeEditor.document.getText();
                    filePath = activeEditor.document.fileName || '';
                }
                const r = await callIdeStream({
                    mode: 'debug_fix', instruction: prompt,
                    filePath: filePath, code: codeSnippet,
                    maxTokens: 1024
                });
                const content = (r && r.content ? r.content : '').trim();
                if (!content) return;
                // 用 notify 推给面板显示（触发 chatPanel 的「AI 回答」展示）
                chatProvider.post({ type: 'debugHint', frames: frames, filePath: top.source ? top.source.path : '', content: content });
                vscode.window.showInformationMessage('🛑 断点命中 —— wasily AI 已分析，查看聊天面板');
            } catch (err) {
                // 调试时 AI 分析失败不影响正常断点体验
                console.error('[wasily] debug hint error:', err);
            }
        })
        );
    }

    // ===== E2: Hover AI 注释 —— 光标悬停符号 → AI 自动解释 =====
    let _hoverDebounce = null;
    context.subscriptions.push(
        vscode.languages.registerHoverProvider(
            [{ scheme: 'file' }, { scheme: 'untitled' }],
            {
                async provideHover(document, position, token) {
                    // 节流：避免每个字符位置都打一次
                    if (_hoverDebounce) clearTimeout(_hoverDebounce);
                    return new Promise((resolve) => {
                        _hoverDebounce = setTimeout(async () => {
                            try {
                                if (token.isCancellationRequested) return resolve(null);
                                const wordRange = document.getWordRangeAtPosition(position);
                                if (!wordRange) return resolve(null);
                                const word = document.getText(wordRange);
                                // 跳过明显不是符号的位置
                                if (!word || word.length < 2 || /^[\d\W_]+$/.test(word)) return resolve(null);

                                const fullText = document.getText();
                                const filePath = document.fileName || '';
                                // 取符号行 + 上下 3 行作为 selection 上下文
                                const line = position.line;
                                const selStart = new vscode.Range(
                                    Math.max(0, line - 3), 0,
                                    Math.min(document.lineCount - 1, line + 3),
                                    document.lineAt(Math.min(document.lineCount - 1, line + 3)).text.length
                                );
                                const selection = document.getText(selStart);

                                const r = await callIdeStream({
                                    mode: 'hover',
                                    file: filePath,
                                    code: fullText,
                                    selection: selection,
                                    maxTokens: 256
                                });
                                if (token.isCancellationRequested) return resolve(null);
                                const content = (r && r.content ? r.content : '').trim();
                                if (!content) return resolve(null);
                                const md = new vscode.MarkdownString(
                                    `**🤖 wasily AI hover: \`${word}\`**\n\n` + content,
                                    true
                                );
                                md.isTrusted = true;
                                resolve(new vscode.Hover(md, wordRange));
                            } catch (err) {
                                resolve(null);
                            }
                        }, 180);  // 180ms debounce
                    });
                }
            }
        )
    );

    // ===== E3: Problems 面板 Quick Fix —— 红线旁边 AI 给 Quick Fix Code Action =====
    context.subscriptions.push(
        vscode.languages.registerCodeActionsProvider(
            [{ scheme: 'file' }, { scheme: 'untitled' }],
            {
                async provideCodeActions(document, range, context, token) {
                    // 只针对 diagnostics 触发（Problems 面板来的）
                    if (!context || !context.diagnostics || !context.diagnostics.length) return [];
                    // 跳过 hint/info，只处理 warning / error
                    const diags = context.diagnostics.filter(d =>
                        d.severity === vscode.DiagnosticSeverity.Error ||
                        d.severity === vscode.DiagnosticSeverity.Warning
                    );
                    if (!diags.length) return [];

                    const fullText = document.getText();
                    const filePath = document.fileName || '';
                    const diagText = diags.map(d => {
                        const sev = d.severity === vscode.DiagnosticSeverity.Error ? 'error' : 'warning';
                        return `[L${d.range.start.line + 1} C${d.range.start.character + 1}] (${sev}) ${d.message}`;
                    }).join('\n');

                    const action = new vscode.CodeAction(
                        `🤖 wasily AI: 修复 ${diags.length} 个问题`,
                        vscode.CodeActionKind.QuickFix
                    );
                    action.isPreferred = true;
                    action.diagnostics = diags;
                    action.command = {
                        title: 'wasily AI Quick Fix',
                        command: 'wasily.quickfix',
                        arguments: [
                            { filePath, code: fullText, diagnostics: diagText,
                              startLine: diags[0].range.start.line + 1 }
                        ]
                    };
                    return [action];
                }
            },
            { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }
        )
    );

    // Quick Fix 命令实现：AI 修复 → diff 预览 → apply
    context.subscriptions.push(
        vscode.commands.registerCommand('wasily.quickfix', async (payload) => {
            if (!payload) return;
            const editor = vscode.window.activeTextEditor;
            if (!editor) return;
            const { filePath, code, diagnostics } = payload;
            try {
                const r = await callIdeStream({
                    mode: 'quickfix',
                    file: filePath,
                    code: code,
                    selection: diagnostics,
                    maxTokens: 2048
                });
                const content = (r && r.content ? r.content : '').trim();
                // 从回复里提取代码块
                const m = content.match(/```[a-zA-Z]*\n([\s\S]*?)```/);
                const fixed = m ? m[1].trim() : content.trim();
                if (!fixed || fixed === code.trim()) {
                    vscode.window.showInformationMessage('🤖 AI 认为当前 diagnostics 无法通过代码修改解决');
                    return;
                }
                // 先 diff 预览，让用户手动 Accept —— 尊重 E5 的 Auto-apply 偏好由用户选择
                await showDiffForCode(fixed);
                vscode.window.showInformationMessage('✅ AI 修复已打开 Diff 预览，点左侧 Accept 即可应用');
            } catch (err) {
                vscode.window.showErrorMessage('AI Quick Fix 失败：' + (err && err.message ? err.message : String(err)));
            }
        })
    );

    // ===== F: Build 失败自动修复命令 =====
    context.subscriptions.push(
        vscode.commands.registerCommand('wasily.buildFix', async () => {
            const editor = vscode.window.activeTextEditor;
            const root = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
            if (!root) { vscode.window.showWarningMessage('请先打开一个项目文件夹'); return; }

            // 让用户选构建命令
            const cmd = await vscode.window.showInputBox({
                prompt: '输入构建命令（npm run build / pip install -e . / cargo build 等）',
                value: 'npm run build',
                placeHolder: '构建命令'
            });
            if (!cmd) return;

            await vscode.window.withProgress(
                { location: vscode.ProgressLocation.Notification, title: '🔨 运行构建并自动修复…', cancellable: false },
                async () => {
                    const { spawn } = require('child_process');
                    const cwd = root.uri.fsPath;
                    const maxIter = 3; // 最多修复 3 轮
                    for (let iter = 1; iter <= maxIter; iter++) {
                        vscode.window.showInformationMessage(`🔨 构建尝试 ${iter}/${maxIter}: ${cmd}`);
                        const result = await new Promise((resolve) => {
                            const parts = cmd.split(/\s+/);
                            const exe = parts[0];
                            const args = parts.slice(1);
                            let out = '', err = '';
                            try {
                                const proc = spawn(exe, args, { cwd, shell: true, timeout: 120000 });
                                proc.stdout.on('data', (d) => { out += d.toString(); });
                                proc.stderr.on('data', (d) => { err += d.toString(); });
                                proc.on('close', (code) => resolve({ code: code || 0, out, err }));
                                proc.on('error', (e) => resolve({ code: -1, out: '', err: String(e) }));
                            } catch (e) { resolve({ code: -1, out: '', err: String(e) }); }
                        });

                        if (result.code === 0) {
                            vscode.window.showInformationMessage(`✅ 构建成功！（第 ${iter} 次尝试）`);
                            return;
                        }

                        const tail = (result.err || result.out || '').slice(-3000);
                        vscode.window.showWarningMessage(`❌ 构建失败 (exit ${result.code})，正在让 AI 分析…`);

                        // 让 AI 分析并修复
                        const activeEditor = vscode.window.activeTextEditor;
                        let filePath = activeEditor ? activeEditor.document.fileName : '';
                        let code = activeEditor ? activeEditor.document.getText() : '';
                        let fixCode = '';
                        try {
                            const r = await callIdeStream({
                                mode: 'build_fix',
                                file: filePath,
                                code: code,
                                selection: tail,
                                maxTokens: 2048
                            });
                            const content = (r && r.content ? r.content : '').trim();
                            const m = content.match(/```[a-zA-Z]*\n([\s\S]*?)```/);
                            fixCode = m ? m[1].trim() : content.trim();
                        } catch (err) {
                            vscode.window.showErrorMessage('AI 修复建议生成失败: ' + (err.message || err));
                            return;
                        }

                        if (!fixCode || !activeEditor) {
                            vscode.window.showInformationMessage('AI 未能给出可自动应用的修复代码，跳过');
                            return;
                        }

                        // diff 预览让用户确认
                        await showDiffForCode(fixCode);
                        const retry = await vscode.window.showInformationMessage(
                            `AI 已生成修复建议（第 ${iter} 次）。请先在 Diff 面板 Accept 修复，然后点 Retry 继续。`,
                            '🔄 Retry', '⏹ 停止'
                        );
                        if (retry !== '🔄 Retry') return;
                    }
                    vscode.window.showWarningMessage(`已达最大修复次数 (${maxIter})，请手动排查`);
                }
            );
        })
    );

    // ===== H: AI Terminal —— 专用终端 + 输出监听 + AI 建议 =====
    let _aiTerminal = null;
    let _aiTerminalBusy = false;
    // AI 建议只写入输出面板：避免把回答 sendText 进终端输入行被 shell 当成命令执行
    const aiTermLog = vscode.window.createOutputChannel('Wasily AI Terminal');
    context.subscriptions.push(aiTermLog);
    context.subscriptions.push(
        vscode.commands.registerCommand('wasily.aiTerminal', async () => {
            // 复用已有 terminal（未关闭则直接 focus）
            if (_aiTerminal && _aiTerminal.exitStatus == null) {
                _aiTerminal.show(true);
                return;
            }
            _aiTerminal = vscode.window.createTerminal({
                name: '🤖 AI Terminal',
                shellPath: process.platform === 'win32'
                    ? (vscode.workspace.getConfiguration('terminal.integrated').get('defaultProfile.windows') || 'powershell.exe')
                    : '/bin/bash'
            });
            _aiTerminal.show(true);
            _aiTerminal.sendText('echo "🤖 Wasily AI Terminal 已就绪 —— 输入 !ai <你的问题> 让 AI 分析上一条命令的输出或给建议"');
            // 输出缓冲（终端输出流式到达，攒一段再分析）
            let _outBuf = '';
            let _outTimer = null;
            let _lastCmd = '';
            // 跟踪用户输入（通过 onDidWriteData 无法直接拿到输入，这里用简易命令前缀检测）
            _aiTerminal.onDidWriteData(async (data) => {
                if (!data) return;
                _outBuf += data;
                if (_outTimer) clearTimeout(_outTimer);
                _outTimer = setTimeout(async () => {
                    // 检测是否有 !ai 前缀（AI 触发命令）或检测到错误关键词
                    const hasAiCmd = _outBuf.indexOf('!ai') >= 0;
                    const errKw = /(error|Error|ERROR|traceback|Traceback|failed|Failed|FAIL|Exception|错误)/.test(_outBuf);
                    if (!hasAiCmd && !errKw) { _outBuf = ''; return; }
                    if (_aiTerminalBusy) { _outBuf = ''; return; }
                    _aiTerminalBusy = true;
                    const tail = _outBuf.slice(-3000);
                    _outBuf = '';
                    try {
                        const r = await callIdeStream({
                            mode: 'terminal_suggest',
                            selection: tail,
                            maxTokens: 512
                        });
                        const content = (r && r.content ? r.content : '').trim();
                        if (content) {
                            aiTermLog.appendLine('🤖 [' + new Date().toLocaleTimeString() + '] AI 建议');
                            aiTermLog.appendLine(content);
                            aiTermLog.appendLine('');
                            aiTermLog.show(true);
                        }
                    } catch (err) {
                        aiTermLog.appendLine('⚠️ AI 分析失败: ' + (err.message || err));
                        aiTermLog.show(true);
                    } finally {
                        _aiTerminalBusy = false;
                    }
                }, 1500); // 1.5s 后攒够一段再分析
            });
            _aiTerminal.onDidExit(() => { _aiTerminal = null; });
        })
    );

    // ===== G: 多模型切换 QuickPick =====
    context.subscriptions.push(
        vscode.commands.registerCommand('wasily.switchModel', async () => {
            try {
                const res = await fetch(getServerUrl() + '/v1/ide/models');
                if (!res.ok) { vscode.window.showErrorMessage('无法获取模型列表'); return; }
                const data = await res.json();
                const items = (data.models || []).map(m => ({
                    label: (m.current ? '✅ ' : '   ') + m.name,
                    description: m.id,
                    id: m.id,
                    current: m.current
                }));
                const choice = await vscode.window.showQuickPick(items, {
                    placeHolder: '选择 AI 模型',
                    matchOnDescription: true
                });
                if (!choice || choice.current) return;
                const switchRes = await fetch(getServerUrl() + '/v1/ide/models/switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ model_id: choice.id })
                });
                if (switchRes.ok) {
                    vscode.window.showInformationMessage('✅ 已切换到: ' + choice.description);
                } else {
                    const err = await switchRes.json();
                    vscode.window.showErrorMessage('切换失败: ' + (err.error || 'unknown'));
                }
            } catch (err) {
                vscode.window.showErrorMessage('模型切换失败: ' + (err.message || err));
            }
        })
    );

    // ===== K: Codebase Graph 可视化 webview =====
    let _graphPanel = null;
    context.subscriptions.push(
        vscode.commands.registerCommand('wasily.showGraph', async () => {
            const root = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
            if (!root) { vscode.window.showWarningMessage('请先打开一个项目文件夹'); return; }

            if (_graphPanel) { _graphPanel.reveal(); return; }
            _graphPanel = vscode.window.createWebviewPanel(
                'wasily-codebase-graph',
                '🗺 Codebase Graph — ' + root.name,
                vscode.ViewColumn.Beside,
                { enableScripts: true, retainContextWhenHidden: true }
            );
            _graphPanel.onDidDispose(() => { _graphPanel = null; });

            // 初始 loading UI
            _graphPanel.webview.html = '<html><body style="font-family:sans-serif;padding:40px;text-align:center">' +
                '<h2>🔄 正在扫描代码库…</h2><p>首次可能需要几秒</p>' +
                '<div style="margin:20px auto;width:50px;height:50px;border:4px solid #333;border-top-color:#0af;border-radius:50%;animation:spin 1s linear infinite"></div>' +
                '<style>@keyframes spin{to{transform:rotate(360deg)}}</style></body></html>';

            try {
                const res = await fetch(getServerUrl() + '/v1/ide/codebase/graph', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ root: root.uri.fsPath, max_files: 150 })
                });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                const html = _buildGraphHtml(data);
                _graphPanel.webview.html = html;
            } catch (err) {
                _graphPanel.webview.html = '<html><body style="font-family:sans-serif;padding:40px">' +
                    '<h2>❌ 加载失败</h2><p>' + String(err.message || err) + '</p></body></html>';
            }
        })
    );
}

// ===== K: Codebase Graph HTML 生成 =====
function _buildGraphHtml(data) {
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    // 按 ext 归类颜色
    const extColors = {
        py: '#3572A5', js: '#F1E05A', ts: '#3178C6', tsx: '#3178C6', jsx: '#F7DF1E',
        java: '#b07219', go: '#00ADD8', rs: '#DEA584', cpp: '#F34B7D', c: '#555555',
        cs: '#178600', rb: '#701516', php: '#4F5D95', vue: '#41b883', html: '#e34c26',
        css: '#563d7c', json: '#292929', svelte: '#ff3e00'
    };

    const nodesJson = JSON.stringify(nodes);
    const edgesJson = JSON.stringify(edges);
    const colorsJson = JSON.stringify(extColors);
    const nodeCount = nodes.length;
    const edgeCount = edges.length;

    return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>🗺 Codebase Graph</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, Segoe UI, system-ui, sans-serif; background: #1e1e2e; color: #cdd6f4; overflow: hidden; height: 100vh; }
  #header { position: fixed; top: 0; left: 0; right: 0; padding: 12px 20px; background: #181825; border-bottom: 1px solid #313244; z-index: 10; display: flex; align-items: center; gap: 16px; }
  #header h1 { font-size: 16px; font-weight: 600; }
  #stats { font-size: 12px; color: #a6adc8; }
  #legend { display: flex; gap: 12px; margin-left: auto; font-size: 11px; }
  .legend-item { display: flex; align-items: center; gap: 4px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
  #tooltip { position: absolute; background: #313244; padding: 8px 12px; border-radius: 6px; font-size: 12px; pointer-events: none; z-index: 100; display: none; border: 1px solid #45475a; max-width: 280px; }
  #tooltip .path { font-family: monospace; color: #89b4fa; word-break: break-all; }
  svg { width: 100%; height: 100vh; margin-top: 48px; }
  .node circle { cursor: pointer; stroke: #11111b; stroke-width: 1.5px; }
  .node text { fill: #cdd6f4; font-size: 10px; pointer-events: none; font-family: monospace; }
  .link { stroke: #585b70; stroke-opacity: 0.4; }
  .node:hover circle { stroke: #89b4fa; stroke-width: 2.5px; }
  .node:hover text { fill: #89b4fa; }
</style>
</head>
<body>
<div id="header">
  <h1>🗺 Codebase Graph</h1>
  <span id="stats">${nodeCount} 文件 · ${edgeCount} import 关系</span>
  <div id="legend"></div>
</div>
<div id="tooltip"></div>
<svg id="graph"></svg>
<script>
const nodes = ${nodesJson};
const edges = ${edgesJson};
const extColors = ${colorsJson};

const legend = document.getElementById('legend');
const exts = [...new Set(nodes.map(n => n.ext))].sort();
exts.forEach(ext => {
    const d = document.createElement('div');
    d.className = 'legend-item';
    d.innerHTML = '<span class="legend-dot" style="background:' + (extColors[ext] || '#888') + '"></span>.' + ext;
    legend.appendChild(d);
});

const width = window.innerWidth;
const height = window.innerHeight - 48;
const svg = d3.select('#graph').attr('width', width).attr('height', height);

// 创建索引
const nodeMap = {};
nodes.forEach(n => nodeMap[n.id] = n);
const links = edges.map(e => ({ source: e.from, target: e.to }))
    .filter(l => nodeMap[l.source] && nodeMap[l.target]);

const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(120))
    .force('charge', d3.forceManyBody().strength(-400))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(20));

const link = svg.append('g').selectAll('line')
    .data(links).join('line').attr('class', 'link');

const node = svg.append('g').selectAll('g')
    .data(nodes).join('g').attr('class', 'node')
    .call(d3.drag()
        .on('start', e => { if (!e.active) simulation.alphaTarget(0.3).restart(); e.subject.fx = e.subject.x; e.subject.fy = e.subject.y; })
        .on('drag', e => { e.subject.fx = e.x; e.subject.fy = e.y; })
        .on('end', e => { if (!e.active) simulation.alphaTarget(0); e.subject.fx = null; e.subject.fy = null; })
    );

node.append('circle')
    .attr('r', d => Math.max(6, Math.min(18, 6 + Math.log(d.size + 1) * 1.5)))
    .attr('fill', d => extColors[d.ext] || '#888')
    .on('mouseover', (e, d) => {
        const tt = document.getElementById('tooltip');
        tt.style.display = 'block';
        tt.innerHTML = '<div><b>' + d.name + '</b> <span style="color:#a6adc8;font-size:10px">.' + d.ext + '</span></div>' +
            '<div class="path">' + d.path + '</div>' +
            '<div style="color:#a6adc8">' + (d.size / 1024).toFixed(1) + ' KB</div>';
        tt.style.left = (e.clientX + 12) + 'px';
        tt.style.top = (e.clientY + 12) + 'px';
    })
    .on('mousemove', e => {
        const tt = document.getElementById('tooltip');
        tt.style.left = (e.clientX + 12) + 'px';
        tt.style.top = (e.clientY + 12) + 'px';
    })
    .on('mouseout', () => { document.getElementById('tooltip').style.display = 'none'; });

node.append('text').text(d => d.name.length > 16 ? d.name.slice(0, 14) + '…' : d.name)
    .attr('dx', 12).attr('dy', 4);

simulation.on('tick', () => {
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    node.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
});

window.addEventListener('resize', () => {
    const w = window.innerWidth, h = window.innerHeight - 48;
    svg.attr('width', w).attr('height', h);
    simulation.force('center', d3.forceCenter(w / 2, h / 2));
    simulation.alpha(0.3).restart();
});
