// -*- coding: utf-8 -*-
/**
 * IDE 侧 MCP Host
 *
 * 让 Wasily IDE 自己充当 MCP 客户端（与 VSCode / Trae 的做法一致）：
 * 扩展进程读取 AI/mcp.json，拉起 stdio 子进程或用 Streamable HTTP 连接远程 server，
 * 完成 initialize / tools/list，并把工具表通过 18753 桥暴露给后端 api_server 转发调用。
 *
 * 传输：stdio（JSON-RPC 2.0，换行分隔）+ Streamable HTTP（JSON / SSE 响应，Mcp-Session-Id）
 */
const { spawn } = require('child_process');
const http = require('http');
const https = require('https');
const path = require('path');
const fs = require('fs');

const AI_DIR = path.resolve(__dirname, '../../../../../../AI');
const CONFIG_PATH = path.join(AI_DIR, 'mcp.json');
const PROTOCOL_VERSION = '2024-11-05';
const DEFAULT_TIMEOUT = 120;

const WRITE_HINTS = ['write', 'edit', 'delete', 'remove', 'move', 'rename', 'create',
    'mkdir', 'touch', 'patch', 'upload', 'install', 'exec', 'run', 'shell',
    'kill', 'restart', 'apply', 'commit', 'push'];

function logLine(msg) {
    try {
        const dir = path.join(AI_DIR, 'wasily_log');
        fs.mkdirSync(dir, { recursive: true });
        const f = path.join(dir, 'mcp_host.log');
        try { if (fs.statSync(f).size > 512 * 1024) fs.renameSync(f, f + '.old'); } catch (e) { /* 首次写 */ }
        fs.appendFileSync(f, '[' + new Date().toISOString() + '] ' + msg + '\n');
    } catch (e) { /* 日志失败不影响主流程 */ }
}

function timeoutOf(cfg) {
    const v = Number(cfg && cfg.timeout);
    return isFinite(v) && v > 0 ? v : DEFAULT_TIMEOUT;
}

// namespace 语义与后端 mcp_hub 保持一致：未配置 → mcp__<server>__；空串 → 不加前缀
function nsOf(cfg, name) {
    const ns = cfg ? cfg.namespace : undefined;
    return (ns === undefined || ns === null) ? ('mcp__' + name + '__') : String(ns);
}

function isWriteTool(rawName) {
    const low = String(rawName || '').toLowerCase();
    return WRITE_HINTS.some((h) => low.indexOf(h) >= 0);
}

function parseSse(raw) {
    let last = {};
    const blocks = String(raw).replace(/\r\n/g, '\n').split('\n\n');
    for (const block of blocks) {
        const data = block.split('\n')
            .filter((ln) => ln.indexOf('data:') === 0)
            .map((ln) => ln.slice(5).trim());
        if (!data.length) continue;
        try {
            const obj = JSON.parse(data.join('\n'));
            if (obj && (obj.result !== undefined || obj.error !== undefined)) return obj;
            if (obj) last = obj;
        } catch (e) { /* 跳过非 JSON 块 */ }
    }
    return last;
}

// ---------------- stdio 传输 ----------------
class StdioTransport {
    constructor(name, cfg) {
        this.name = name;
        this.cfg = cfg || {};
        this.timeout = timeoutOf(this.cfg);
        this.proc = null;
        this._id = 0;
        this._pending = new Map();
        this._buf = '';
        this._err = '';
    }

    alive() {
        return !!(this.proc && this.proc.exitCode === null && !this.proc.killed);
    }

    async connect() {
        this.close();
        let cmd = String(this.cfg.command || '').trim();
        if (!cmd) throw new Error('缺少 command');
        // Windows 下 npx / npm 需要 .cmd 后缀，且 .cmd 必须通过 shell 运行
        let useShell = false;
        if (process.platform === 'win32' && !path.isAbsolute(cmd) && !/\.(exe|cmd|bat)$/i.test(cmd)) {
            cmd = cmd + '.cmd';
        }
        if (process.platform === 'win32' && /\.(cmd|bat)$/i.test(cmd)) {
            useShell = true;
        }
        const args = Array.isArray(this.cfg.args) ? this.cfg.args.map(String) : [];
        const cwd = this.cfg.cwd ? path.resolve(AI_DIR, String(this.cfg.cwd)) : AI_DIR;
        const env = Object.assign({}, process.env, this.cfg.env || {});
        this.proc = spawn(cmd, args, { cwd, env, windowsHide: true, shell: useShell, stdio: ['pipe', 'pipe', 'pipe'] });
        this.proc.stdout.setEncoding('utf8');
        this.proc.stdout.on('data', (c) => this._onData(c));
        this.proc.stderr.setEncoding('utf8');
        this.proc.stderr.on('data', (c) => { this._err = (this._err + c).slice(-4000); });
        this.proc.on('exit', (code) => {
            const err = new Error('MCP 进程退出 code=' + code + (this._err ? ' ' + this._err.slice(-300) : ''));
            for (const [, p] of this._pending) { clearTimeout(p.timer); p.reject(err); }
            this._pending.clear();
        });
        this.proc.on('error', (e) => {
            const err = new Error('MCP 启动失败: ' + ((e && e.message) || e));
            for (const [, p] of this._pending) { clearTimeout(p.timer); p.reject(err); }
            this._pending.clear();
        });
        await this.request('initialize', {
            protocolVersion: PROTOCOL_VERSION,
            capabilities: {},
            clientInfo: { name: 'wasily-ide', version: '1.0.0' }
        });
        this.notify('notifications/initialized', {});
    }

    _onData(chunk) {
        this._buf += chunk;
        let idx;
        while ((idx = this._buf.indexOf('\n')) >= 0) {
            const line = this._buf.slice(0, idx).trim();
            this._buf = this._buf.slice(idx + 1);
            if (!line) continue;
            let msg;
            try { msg = JSON.parse(line); } catch (e) { continue; }
            const p = this._pending.get(msg.id);
            if (!p) continue;               // 服务端通知 / 无主响应，忽略
            this._pending.delete(msg.id);
            clearTimeout(p.timer);
            if (msg.error) p.reject(new Error((msg.error && msg.error.message) || 'MCP 错误'));
            else p.resolve(msg.result || {});
        }
    }

    request(method, params, timeout) {
        if (!this.alive()) return Promise.reject(new Error('MCP 未连接'));
        const id = ++this._id;
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this._pending.delete(id);
                reject(new Error('MCP 超时 ' + method));
            }, (timeout || this.timeout) * 1000);
            this._pending.set(id, { resolve, reject, timer });
            try {
                this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params: params || {} }) + '\n');
            } catch (e) {
                clearTimeout(timer);
                this._pending.delete(id);
                reject(e);
            }
        });
    }

    notify(method, params) {
        try {
            this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params: params || {} }) + '\n');
        } catch (e) { /* 进程已退出 */ }
    }

    close() {
        if (this.proc) {
            try { this.proc.kill(); } catch (e) { /* ignore */ }
            this.proc = null;
        }
    }
}

// ---------------- Streamable HTTP 传输 ----------------
class HttpTransport {
    constructor(name, cfg) {
        this.name = name;
        this.cfg = cfg || {};
        this.timeout = timeoutOf(this.cfg);
        this.url = String(this.cfg.url || '').trim();
        this.headers = this.cfg.headers || {};
        this.sessionId = '';
        this._id = 0;
    }

    alive() { return !!this.url; }

    async connect() {
        if (!this.url) throw new Error('缺少 url');
        this.sessionId = '';
        this._id = 0;
        await this.request('initialize', {
            protocolVersion: PROTOCOL_VERSION,
            capabilities: {},
            clientInfo: { name: 'wasily-ide', version: '1.0.0' }
        });
        this.notify('notifications/initialized', {});
    }

    _post(obj, timeout) {
        return new Promise((resolve, reject) => {
            let u;
            try { u = new URL(this.url); } catch (e) { reject(new Error('url 非法: ' + this.url)); return; }
            const mod = u.protocol === 'https:' ? https : http;
            const body = Buffer.from(JSON.stringify(obj), 'utf8');
            const headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream',
                'Content-Length': body.length
            };
            if (this.sessionId) headers['Mcp-Session-Id'] = this.sessionId;
            Object.assign(headers, this.headers);
            const req = mod.request({
                protocol: u.protocol, hostname: u.hostname, port: u.port || (u.protocol === 'https:' ? 443 : 80),
                path: u.pathname + u.search, method: 'POST', headers
            }, (res) => {
                const sid = res.headers['mcp-session-id'];
                if (sid) this.sessionId = String(sid);
                let raw = '';
                res.setEncoding('utf8');
                res.on('data', (c) => { raw += c; });
                res.on('end', () => {
                    const ctype = String(res.headers['content-type'] || '').toLowerCase();
                    try {
                        if (ctype.indexOf('text/event-stream') >= 0
                            || raw.trim().indexOf('event:') === 0
                            || raw.indexOf('\ndata:') >= 0) {
                            resolve(parseSse(raw));
                        } else {
                            resolve(raw.trim() ? JSON.parse(raw) : {});
                        }
                    } catch (e) {
                        reject(new Error('MCP 响应解析失败: ' + raw.slice(0, 200)));
                    }
                });
            });
            req.setTimeout((timeout || this.timeout) * 1000, () => {
                req.destroy(new Error('MCP 请求超时'));
            });
            req.on('error', reject);
            req.write(body);
            req.end();
        });
    }

    async request(method, params, timeout) {
        const msg = await this._post({ jsonrpc: '2.0', id: ++this._id, method, params: params || {} }, timeout);
        if (msg && msg.error) throw new Error((msg.error && msg.error.message) || 'MCP 错误');
        return (msg && msg.result) || {};
    }

    notify(method, params) {
        this._post({ jsonrpc: '2.0', method, params: params || {} }).catch(() => { /* 通知失败可忽略 */ });
    }

    close() { this.sessionId = ''; }
}

// ---------------- Host ----------------
class McpHost {
    constructor() {
        this.servers = {};
        this.startedAt = 0;
    }

    loadConfig() {
        try {
            return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
        } catch (e) {
            logLine('读取 mcp.json 失败: ' + ((e && e.message) || e));
            return { mcpServers: {} };
        }
    }

    async start() {
        const cfg = this.loadConfig();
        const map = cfg.mcpServers || cfg.servers || {};
        const jobs = Object.keys(map)
            .filter((n) => map[n] && map[n].enabled !== false)
            .map((n) => this._connectOne(n, map[n]));
        await Promise.all(jobs);
        this.startedAt = Date.now();
        logLine('已启动: ' + Object.keys(this.servers).map((n) => {
            const st = this.servers[n];
            return n + '=' + (st.connected ? st.tools.length + '工具' : '失败(' + st.error + ')');
        }).join(', '));
        return this.status();
    }

    async _connectOne(name, cfg) {
        const transport = String((cfg && cfg.transport) || 'stdio').toLowerCase();
        const client = transport === 'http' ? new HttpTransport(name, cfg) : new StdioTransport(name, cfg);
        const st = {
            name: name, transport: transport, client: client,
            connected: false, error: '', tools: [], readonly: {}
        };
        this.servers[name] = st;
        try {
            await client.connect();
            st.tools = await this._listAll(client);
            st.tools.forEach((t) => { st.readonly[t && t.name] = !isWriteTool(t && t.name); });
            st.connected = true;
        } catch (e) {
            st.error = String((e && e.message) || e);
            logLine('连接失败 ' + name + ': ' + st.error);
        }
    }

    async _listAll(client) {
        const out = [];
        let cursor = '';
        for (let i = 0; i < 10; i++) {
            const r = await client.request('tools/list', cursor ? { cursor: cursor } : {});
            (r && r.tools ? r.tools : []).forEach((t) => out.push(t));
            cursor = r && r.nextCursor;
            if (!cursor) break;
        }
        return out;
    }

    status() {
        return Object.keys(this.servers).map((n) => {
            const st = this.servers[n];
            return {
                name: n,
                transport: st.transport,
                connected: !!st.connected,
                error: st.error,
                namespace: nsOf(st.client.cfg, n),
                timeout: timeoutOf(st.client.cfg),
                tools: st.tools.map((t) => ({
                    name: t && t.name,
                    description: String((t && t.description) || '').slice(0, 120),
                    readonly: !!st.readonly[t && t.name]
                }))
            };
        });
    }

    tools() {
        const out = [];
        Object.keys(this.servers).forEach((n) => {
            const st = this.servers[n];
            if (!st.connected) return;
            const ns = nsOf(st.client.cfg, n);
            st.tools.forEach((t) => {
                if (!t || !t.name) return;
                out.push({
                    name: ns + t.name,
                    server: n,
                    raw: t.name,
                    description: '[MCP:' + n + '] ' + String(t.description || ''),
                    inputSchema: t.inputSchema || { properties: {} },
                    readonly: !!st.readonly[t.name]
                });
            });
        });
        return out;
    }

    find(fullName) {
        const name = String(fullName || '');
        return this.tools().find((t) => t.name === name) || null;
    }

    async call(fullName, args) {
        const t = this.find(fullName);
        if (!t) throw new Error('未找到 MCP 工具: ' + fullName);
        const st = this.servers[t.server];
        const result = await st.client.request('tools/call', { name: t.raw, arguments: args || {} });
        const parts = (result && result.content) || [];
        let text = parts.filter((p) => p && p.type === 'text').map((p) => p.text || '').join('\n');
        if (!text) text = '（工具无输出）';
        if (result && result.isError) text = '[MCP 工具错误] ' + text;
        return text;
    }

    async reload() {
        this.closeAll();
        this.servers = {};
        return this.start();
    }

    closeAll() {
        Object.keys(this.servers).forEach((n) => {
            try { this.servers[n].client.close(); } catch (e) { /* ignore */ }
        });
    }
}

module.exports = { McpHost, AI_DIR, CONFIG_PATH };
