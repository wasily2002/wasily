/* Trae 风格标题栏 v4（注入式）：
   对齐参考图：左侧「白色代码图标 + IDE」深色胶囊 → 原生菜单（紧凑）→「项目介绍 ⌄」
   → 放大镜「搜索」窄入口（点击展开，Esc 收起，回车执行全局搜索）→ 右侧
   ←/→、侧边栏/面板/AI 面板三个布局开关（与真实布局双向同步）、设置齿轮、
   黄色头像（账户菜单）、绿色 Agent 胶囊（打开 AI 面板并切到 Agent 模式）。
   全部按钮走真实命令（IDE 桥 /command → vscode.commands.executeCommand），
   删除促销条、假积分计数与装饰性铃铛。保留系统窗口按钮原生行为。 */
(function () {
    'use strict';

    var SERVER = 'http://127.0.0.1:8000';
    var BRIDGE = 'http://127.0.0.1:18753';
    var PORTAL = 'http://127.0.0.1:8001/';
    var mode = 'ide';
    var brandNode = null, portalBtn = null, searchBox = null, searchSep = null, rightGroup = null;
    var agentBtn = null, avatarBtn = null, accMenu = null;
    var layoutBtns = [];
    var accInfo = null;

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined) n.textContent = text;
        return n;
    }

    function icon(path, cls) {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '1.7');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        if (cls) svg.setAttribute('class', cls);
        var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        p.setAttribute('d', path);
        svg.appendChild(p);
        return svg;
    }

    /* 把 AI 辅助面板拖到指定宽度：直接对它的 sash 派发鼠标事件，
       走工作台原生拖动逻辑，比手工改 DOM 宽度更稳（不会和 grid 的布局打架） */
    function resizeAuxTo(target) {
        var bar = document.querySelector('.part.auxiliarybar');
        if (!bar) return;
        var br = bar.getBoundingClientRect();
        if (!br.width) return;
        var sash = null;
        Array.prototype.slice.call(document.querySelectorAll('.monaco-sash.vertical')).forEach(function (s) {
            var r = s.getBoundingClientRect();
            if (Math.abs((r.left + r.width / 2) - (br.left - 2)) < 14) sash = s;
        });
        if (!sash) return;
        var sr = sash.getBoundingClientRect();
        var x = Math.round(sr.left + sr.width / 2), y = Math.round(sr.top + 300);
        var dx = Math.round(br.width - target);
        if (!dx) return;
        function me(type, targetEl, mx, btns) {
            // 合成事件打标记：自绘拖条自己的 document 监听要能区分出“这是我发的”
            var ev = new MouseEvent(type, {
                bubbles: true, cancelable: true, clientX: mx, clientY: y,
                screenX: mx, screenY: y, button: 0, buttons: btns
            });
            ev.__wasilySynth = true;
            targetEl.dispatchEvent(ev);
        }
        me('mousedown', sash, x, 1);
        me('mousemove', document, x + dx, 1);
        me('mouseup', document, x + dx, 0);
    }

    /* AI 面板自定义拖拽手柄：原生 sash 在浮层布局/窄窗口下经常抓不住，
       这里在面板左缘叠一条自己的拖条，拖动时复用原生拖动把面板拉到目标宽度 */
    function ensureAuxResizer() {
        var bar = document.querySelector('.part.auxiliarybar');
        var h = document.getElementById('wasilyAuxResizer');
        if (!bar) { if (h) h.style.display = 'none'; return; }
        var r = bar.getBoundingClientRect();
        if (r.width < 2) { if (h) h.style.display = 'none'; return; }
        if (!h) {
            h = el('div', null);
            h.id = 'wasilyAuxResizer';
            document.body.appendChild(h);
            var dragging = false, startX = 0, startW = 0;
            // 拖动时铺一层透明遮罩：AI 面板是 iframe/webview，指针移到它上面后
            // 事件会进 iframe 的文档，主文档收不到 mousemove，遮罩能兜住整段拖动
            var mask = el('div', null);
            mask.id = 'wasilyAuxDragMask';
            var line = el('div', null);
            line.id = 'wasilyAuxDragLine';
            mask.appendChild(line);
            document.body.appendChild(mask);
            h.addEventListener('mousedown', function (e) {
                if (e.button !== 0) return;
                e.preventDefault();
                e.stopPropagation();
                var b = document.querySelector('.part.auxiliarybar');
                if (!b) return;
                dragging = true;
                startX = e.clientX;
                startW = b.getBoundingClientRect().width;
                mask.classList.add('show');
                line.style.left = Math.round(e.clientX) + 'px';
                h.classList.add('active');
                document.body.style.cursor = 'col-resize';
                document.body.style.userSelect = 'none';
            }, true);
            document.addEventListener('mousemove', function (e) {
                if (e.__wasilySynth || !dragging) return;
                line.style.left = Math.round(e.clientX) + 'px';
            }, true);
            document.addEventListener('mouseup', function (e) {
                if (e.__wasilySynth || !dragging) return;
                dragging = false;
                mask.classList.remove('show');
                h.classList.remove('active');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                // 一次拖动只做一次原生 resize：逐帧反复启停会被 sash 的最小/最大跨度截断
                var target = Math.round(Math.max(240, Math.min(window.innerWidth - 260, startW + (startX - e.clientX))));
                try { resizeAuxTo(target); } catch (err) {}
                // 保存真实落地的宽度（原生可能因最小/最大限制没走到目标值）
                setTimeout(function () {
                    var b2 = document.querySelector('.part.auxiliarybar');
                    if (b2) saveAuxWidth(b2.getBoundingClientRect().width);
                }, 300);
                ensureAuxResizer();
            }, true);
            // 指针在窗口外松开时收不到 mouseup，失焦兜底复位
            window.addEventListener('blur', function () {
                if (!dragging) return;
                dragging = false;
                mask.classList.remove('show');
                h.classList.remove('active');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            });
        }
        h.style.display = 'block';
        h.style.left = Math.round(r.left - 5) + 'px';
        h.style.top = Math.round(r.top) + 'px';
        h.style.height = Math.round(r.height) + 'px';
    }

    /* 面板宽度记忆：桥 → 扩展 globalState 落盘，重载或重启 IDE 后还原上次宽度 */
    var AUX_WIDTH_KEY = 'auxPanelWidth';
    var auxWidthRestored = false;

    function saveAuxWidth(w) {
        if (!w || w < 2) return;
        try { bridge('/ui_state', { key: AUX_WIDTH_KEY, value: Math.round(w) }); } catch (e) {}
    }

    function restoreAuxWidth() {
        if (auxWidthRestored) return;
        var bar = document.querySelector('.part.auxiliarybar');
        if (!bar) return;
        var cur = bar.getBoundingClientRect().width;
        if (cur < 2) return;   // 面板还没打开，等它出现后再还原
        auxWidthRestored = true;
        try {
            fetch(BRIDGE + '/ui_state?key=' + AUX_WIDTH_KEY).then(function (r) { return r.json(); }).then(function (d) {
                if (!d || !d.ok || typeof d.value !== 'number') return;
                var target = Math.round(Math.max(240, Math.min(window.innerWidth - 260, d.value)));
                if (Math.abs(cur - target) < 8) return;
                resizeAuxTo(target);
            }).catch(function () {});
        } catch (e) {}
    }

    function injectStyle() {
        var css = [
            /* ===== 底色对齐参考图（#222427） ===== */
            '.monaco-workbench .part.titlebar,.monaco-workbench .part.titlebar>.titlebar-container{background:#222427!important}',
            /* ===== 三段式：左区贴左、中区搜索、右区贴右，均不互相挤压 ===== */
            '.monaco-workbench .part.titlebar>.titlebar-container{display:flex!important;align-items:center!important}',
            /* 左区不吞空余空间：搜索入口紧跟「项目介绍」之后（对齐参考图），空余留在中区与右区之间 */
            /* 左区内缩清零：参考图品牌块从 x=0 起，原实现有约 11px 原生左内边距（整组右移） */
            '.monaco-workbench .part.titlebar>.titlebar-container>.titlebar-left{order:0!important;flex:0 1 auto!important;width:auto!important;min-width:0!important;overflow:hidden!important;justify-content:flex-start!important;padding-left:0!important}',
            '.monaco-workbench .part.titlebar>.titlebar-container>.titlebar-center{order:1!important;flex:0 1 auto!important;min-width:0!important;justify-content:flex-start!important}',
            /* margin-left:auto 把右段推到窗口右边缘（对齐参考图，原先右组停在内容宽处、右侧留 339px 空白） */
            '.monaco-workbench .part.titlebar>.titlebar-container>.titlebar-right{order:2!important;flex:0 0 auto!important;width:auto!important;min-width:0!important;margin-left:auto!important;justify-content:flex-end!important;padding-left:8px!important}',
            '.part.titlebar .titlebar-left,.part.titlebar .menubar{flex:0 1 auto!important;min-width:0!important;overflow:hidden!important}',
            '.part.titlebar .menubar{justify-content:flex-start!important;gap:0!important;column-gap:0!important}',

            /* ===== 原生菜单紧凑化 ===== */
            /* 菜单项间距对齐参考图：参考文字块间隙 16-17px；实测内边距 2px→间隙仅 5px（挤成一团），8px→约 16px */
            '.part.titlebar .menubar .menubar-menu-button{margin:0!important;padding:0 8px!important;border-radius:4px}',
            /* 隐藏原生「更多」溢出按钮：visibility:hidden 但仍占 36px，把「项目介绍」整体右推 36px */
            '.part.titlebar .menubar .menubar-menu-button:has(.toolbar-toggle-more){display:none!important}',
            '.part.titlebar .menubar .menubar-menu-button:last-child .toolbar-toggle-more{display:none!important}',
            '.part.titlebar .menubar-menu-title{font-size:12px!important;line-height:22px!important;padding:0!important;margin:0!important}',
            /* 去掉原生 menubar 自带右侧留白，让「项目介绍」贴紧菜单末尾 */
            '.part.titlebar .titlebar-left .menubar{padding-right:0!important;margin-right:0!important}',
            '.part.titlebar .menubar-menu-button:hover{background:rgba(255,255,255,.08)}',
            /* 原生产品图标隐藏（用自绘品牌胶囊替代） */
            '.part.titlebar .window-appicon{display:none!important}',

            /* ===== 品牌胶囊（白色代码图标 + IDE） ===== */
            '#tbBrand{-webkit-app-region:no-drag;display:flex;align-items:center;gap:6px;height:22px;margin:0 12px 0 4px;',
            'padding:0 6px 0 3px;border-radius:6px;background:rgba(255,255,255,.04);flex:none;cursor:default}',
            '#tbBrand .lg{width:22px;height:22px;border-radius:5px;background:#f4f6fb;display:flex;align-items:center;justify-content:center;flex:none}',
            '#tbBrand .lg svg{width:13px;height:13px;display:block;color:#1b1c20}',
            '#tbBrand .ide{font-size:11px;letter-spacing:.6px;color:#c9cdd6;border:1px solid rgba(255,255,255,.22);',
            'border-radius:4px;padding:1px 6px;line-height:14px}',

            /* ===== 项目介绍（菜单末尾） ===== */
            /* 项目介绍紧贴菜单末尾：参考「帮助」末字→「项目介绍」首字 31px；实测空「更多」按钮占 36px，去掉后补 22px 左边距 */
            '.titlebar-left .tbPortal{-webkit-app-region:no-drag;cursor:pointer;position:relative;z-index:2500;order:3;',
            'display:flex;align-items:center!important;gap:4px;margin-left:22px!important;padding-left:6px!important;max-width:180px;overflow:hidden;flex:none;',
            'border-left:1px solid rgba(255,255,255,.16)}',
            '.titlebar-left .tbPortal .tbCaret{width:12px;height:12px;display:block;color:#9aa0aa;flex:none}',
            '.titlebar-left .tbPortal .menubar-menu-title{font-weight:600!important;color:#eef0f4!important;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',

            /* ===== 搜索窄入口 ===== */
            '.monaco-workbench .part.titlebar .titlebar-center .window-title>.command-center{display:none!important}',
            '#tbSearch{-webkit-app-region:no-drag;position:relative;z-index:2500;display:flex;align-items:center;height:26px;width:92px;margin-left:0;',
            'border-radius:6px;background:transparent;border:1px solid transparent;transition:width .15s ease,background .15s,border-color .15s;overflow:hidden;flex:0 1 auto;min-width:30px}',
            '#tbSearch:hover{background:rgba(255,255,255,.07)}',
            '#tbSearch.open{width:300px;min-width:220px;background:#1d1e24;border-color:#34353c;padding:0 6px}',
            '#tbSearch .sbBtn{-webkit-app-region:no-drag;display:inline-flex;align-items:center;gap:6px;height:100%;',
            'border:none;background:transparent;color:#9aa0aa;font:12px/1 var(--vscode-font-family,"Microsoft YaHei",sans-serif);',
            'padding:0 8px;cursor:pointer;flex:none}',
            '#tbSearch.open .sbBtn{padding:0 6px 0 2px}',
            '#tbSearch .sbBtn svg{width:13px;height:13px;display:block}',
            '#tbSearch .sbTxt{overflow:hidden;white-space:nowrap;min-width:0}',
            '#tbSearch.open .sbBtn .sbTxt{display:none}',
            '#tbSearch input{display:none;flex:1 1 auto;min-width:0;height:22px;border:none;outline:none;background:transparent;',
            'color:#e6e9f0;font:12px/1 var(--vscode-font-family,"Microsoft YaHei",sans-serif)}',
            '#tbSearch.open input{display:block}',
            /* 项目介绍与搜索之间的分隔线（对齐参考图） */
            '.monaco-workbench .part.titlebar .titlebar-center .tbSepC{width:1px;height:16px;background:rgba(255,255,255,.16);margin:0 3px 0 0;flex:none}',

            /* ===== 窄窗图标（默认隐藏，仅断点内显示） ===== */
            '.titlebar-left .tbPortal .tbPIco{display:none;width:13px;height:13px;color:#c9cdd6;flex:none}',

            /* ===== 宽度断点（只收起文字/间距，不改 1920 现状） ===== */
            '@media (max-width:1149px){',
            '.titlebar-left .tbPortal .menubar-menu-title{display:none!important}',
            '.titlebar-left .tbPortal .tbPIco{display:block!important}',
            '.titlebar-left .tbPortal{padding-left:8px!important;margin-left:4px!important}',
            '}',
            '@media (max-width:979px){',
            '#tbSearch:not(.open){width:30px!important;min-width:30px!important}',
            '#tbSearch:not(.open) .sbTxt{display:none!important}',
            '#tbSearch:not(.open) .sbBtn{padding:0 8px!important}',
            '.part.titlebar .menubar-menu-button{padding:0 5px!important}',
            '.part.titlebar .menubar-menu-title{font-size:11.5px!important}',
            '}',

            /* ===== 右侧按钮组 ===== */
            '#tbRight{display:flex;align-items:center;gap:1px;padding-right:0;height:100%;-webkit-app-region:no-drag;position:relative;z-index:2500}',
            '.part.titlebar .action-toolbar-container{padding:0!important;width:0!important;min-width:0!important;overflow:hidden!important}',
            '.monaco-workbench.windows:not(.web) .part.titlebar .window-controls-container{width:auto!important}',
            /* 窗口按钮宽度还原原生 46px：参考图三个按钮中心间距 46px，原实现 24px（整组右移缩窄 66px） */
            '.monaco-workbench .part.titlebar .window-controls-container>.window-icon{width:46px!important;font-size:15px!important;border-radius:3px}',
            '#tbRight button{border:none;background:transparent;color:#9aa0aa;cursor:pointer;display:inline-flex;align-items:center;',
            'justify-content:center;font:12px/1 var(--vscode-font-family,"Microsoft YaHei",sans-serif);padding:0;height:24px}',
            /* 图标按钮宽 27（+1px 间隙 = 28px 步距，对齐参考图右组 28px 步距） */
            '#tbRight .tbIco{width:27px;border-radius:5px}',
            '#tbRight .tbIco svg{width:15px;height:15px;display:block}',
            '#tbRight .tbIco:hover{background:rgba(255,255,255,.08);color:#e6e9f0}',
            '#tbRight .tbIco.on{color:#e0e3ea}',
            '#tbRight .tbSep{width:1px;height:16px;background:rgba(255,255,255,.16);margin:0 5px;flex:none}',
            '#tbRight .tbAvatar{width:22px;height:22px;border-radius:5px;background:#ffd84d;color:#2b2200;margin:0 3px}',
            '#tbRight .tbAvatar svg{width:13px;height:13px;display:block}',
            '#tbRight .tbAvatar:hover{background:#ffe270}',
            '#tbRight .tbAgent{height:22px;padding:0 11px;border-radius:11px;background:#243d33;color:#6f9a83;border:1px solid rgba(80,180,120,.25);',
            'font-size:11.5px;gap:5px;margin-left:2px}',
            '#tbRight .tbAgent svg{width:12px;height:12px;display:block}',
            '#tbRight .tbAgent:hover{background:#2b4a3d;color:#8fd8ae}',
            '#tbRight .tbAgent.on{background:#243d33;border-color:rgba(63,217,127,.55);color:#45e894}',

            /* ===== 项目下拉菜单 ===== */
            '#portalMenu{position:fixed;width:252px;z-index:100001;display:none;background:#25262c;border:1px solid #34353c;',
            'border-radius:8px;box-shadow:0 10px 28px rgba(0,0,0,.5);padding:6px;',
            'font:13px/1.5 var(--vscode-font-family,"Microsoft YaHei",sans-serif);color:#d7dae0;text-align:left}',
            '#portalMenu.open{display:block}',
            '#portalMenu .pmRow{display:flex;align-items:center;gap:10px;padding:7px 9px;border-radius:6px;cursor:pointer;font-size:13px}',
            '#portalMenu .pmRow:hover{background:rgba(255,255,255,.07)}',
            '#portalMenu .pmRow .sp{flex:1}',
            '#portalMenu .pmRow svg{width:15px;height:15px;flex:none;color:#a6acb8}',
            '#portalMenu .pmRow .ext{color:#6b6f79;font-size:11.5px}',
            '#portalMenu .pmSep{height:1px;background:rgba(255,255,255,.08);margin:5px 4px}',
            '#portalMenu .pmHead{padding:7px 9px 3px;color:#8b8f9a;font-size:11.5px;letter-spacing:.4px}',
            '#portalMenu .pmRecent{display:flex;align-items:center;gap:10px;padding:7px 9px;border-radius:6px;cursor:pointer}',
            '#portalMenu .pmRecent:hover{background:rgba(255,255,255,.07)}',
            '#portalMenu .pmAv{width:22px;height:22px;border-radius:5px;background:#1f9d4d;color:#fff;display:flex;',
            'align-items:center;justify-content:center;font-size:11.5px;flex:none}',
            '#portalMenu .pmMain{min-width:0;flex:1}',
            '#portalMenu .pmName{display:block;font-size:13px;color:#dde0e6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
            '#portalMenu .pmPath{display:block;color:#6b6f79;font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}',

            /* ===== 账户菜单 ===== */
            '#accMenu{position:fixed;width:240px;z-index:100002;display:none;background:#25262c;border:1px solid #34353c;',
            'border-radius:8px;box-shadow:0 10px 28px rgba(0,0,0,.5);padding:6px;',
            'font:12.5px/1.5 var(--vscode-font-family,"Microsoft YaHei",sans-serif);color:#d7dae0}',
            '#accMenu.open{display:block}',
            '#accMenu .amHead{display:flex;align-items:center;gap:8px;padding:8px 8px 10px}',
            '#accMenu .amAv{width:26px;height:26px;border-radius:6px;background:#ffd84d;color:#2b2200;display:flex;align-items:center;justify-content:center;flex:none}',
            '#accMenu .amAv svg{width:14px;height:14px;display:block}',
            '#accMenu .amName{font-size:13px;font-weight:600;color:#eef0f4}',
            '#accMenu .amPlan{font-size:11px;color:#8b8f9a;margin-left:2px}',
            '#accMenu .amRow{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;cursor:pointer;font-size:12.5px}',
            '#accMenu .amRow:hover{background:rgba(255,255,255,.07)}',
            '#accMenu .amRow svg{width:13px;height:13px;color:#8f939c;flex:none}',
            '#accMenu .amRow .sp{flex:1}',
            '#accMenu .amRow .muted{color:#6b6f79;font-size:11.5px}',
            '#accMenu .amSep{height:1px;background:rgba(255,255,255,.08);margin:5px 4px}',

            /* ===== Toast ===== */
            '#tbToast{position:fixed;top:44px;left:50%;transform:translateX(-50%);z-index:100002;background:#25262c;',
            'border:1px solid #34353c;color:#dfe3ea;font-size:12px;padding:8px 16px;border-radius:8px;',
            'box-shadow:0 8px 22px rgba(0,0,0,.5);opacity:0;transition:opacity .18s;pointer-events:none}',
            '#tbToast.show{opacity:1}',

            /* ===== 辅助侧边栏顶部横条：与 Trae 一致整条隐藏 =====
               composite.title 是容器标题条（曾显示 workbench.view.extension.wasilyAiBar / Wasily）；
               只有一个视图时它下面还会有一层 view 自己的 pane-header，一并收掉。
               多视图时保留 pane-header，否则无法切换视图。 */
            '.monaco-workbench .part.auxiliarybar>.composite.title{display:none!important;height:0!important;min-height:0!important;padding:0!important;border:none!important}',
            '.monaco-workbench .part.auxiliarybar>.header-or-footer{display:none!important}',
            /* 视图自己的 pane-header（第二层细横条）：辅助侧边栏里只有 AI 面板一个视图，整条收掉 */
            '.monaco-workbench .part.auxiliarybar .monaco-pane-view .pane>.pane-header{display:none!important;height:0!important;min-height:0!important;padding:0!important;border:none!important}',
            /* Workbench 拖文件时会内联禁用 Webview 指针事件，导致 AI 面板收不到 drop；仅恢复本面板 */
            '[id="workbench.view.extension.wasilyAiBar"] iframe.webview{pointer-events:auto!important}',
            /* ===== 侧栏可调宽：原生 sash 只有 4px，肉眼几乎抓不到 =====
               sashes 是编辑器/侧边栏/辅助侧边栏之间的拖拽分隔条，加宽命中区并给悬停提示，
               拖动逻辑仍由 Workbench 原生 Sash 处理（宽度会被自动记住）。 */
            '.monaco-workbench{--vscode-sash-size:10px!important;--vscode-sash-hover-size:10px!important}',
            '.monaco-workbench .monaco-sash.vertical{width:10px!important;cursor:col-resize!important}',
            '.monaco-workbench .monaco-sash.vertical:hover{background:linear-gradient(90deg,transparent 4px,rgba(108,158,248,.7) 4px,rgba(108,158,248,.7) 6px,transparent 6px)}',
            '.monaco-workbench .monaco-sash.vertical.active{background:linear-gradient(90deg,transparent 4px,rgba(108,158,248,.95) 4px,rgba(108,158,248,.95) 6px,transparent 6px)}',
            /* AI 面板左缘常显一条细线，提示这里可拖动调宽 */
            '.monaco-workbench .part.auxiliarybar::before{content:"";position:absolute;left:0;top:10%;bottom:10%;width:1px;pointer-events:none;z-index:5;background:rgba(140,150,170,.35)}',
            /* 自绘拖拽手柄（叠在 AI 面板左缘，原生 sash 抓不住时的兜底） */
            '#wasilyAuxResizer{position:fixed;width:10px;z-index:3000;cursor:col-resize;-webkit-app-region:no-drag;background:transparent;display:none}',
            '#wasilyAuxResizer:hover,#wasilyAuxResizer.active{background:linear-gradient(90deg,transparent 4px,rgba(108,158,248,.85) 4px,rgba(108,158,248,.85) 6px,transparent 6px)}',
            /* 拖动中的预览竖线：松手才真正 resize，避免逐帧启停被原生跨度限制截断 */
            '#wasilyAuxDragMask{position:fixed;inset:0;z-index:3001;display:none;cursor:col-resize;background:transparent}',
            '#wasilyAuxDragMask.show{display:block}',
            '#wasilyAuxDragLine{position:fixed;top:0;bottom:0;width:2px;background:#6c9ef8;display:none;pointer-events:none}',
            '#wasilyAuxDragMask.show #wasilyAuxDragLine{display:block}'
        ].join('');
        var s = document.createElement('style');
        s.id = 'traeBarStyle';
        s.appendChild(document.createTextNode(css));
        (document.head || document.documentElement).appendChild(s);
    }

    var ICO = {
        user: 'M12 12a3.4 3.4 0 1 0 0-6.8 3.4 3.4 0 0 0 0 6.8zM5 19.5c.7-3 3.4-4.6 7-4.6s6.3 1.6 7 4.6',
        gear: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 1-.1 1.2l2 1.5-2 3.5-2.4-1a7 7 0 0 1-2 1.2L14 21h-4l-.4-2.6a7 7 0 0 1-2-1.2l-2.4 1-2-3.5 2-1.5A7 7 0 0 1 5 12a7 7 0 0 1 .1-1.2l-2-1.5 2-3.5 2.4 1a7 7 0 0 1 2-1.2L10 3h4l.4 2.6a7 7 0 0 1 2 1.2l2.4-1 2 3.5-2 1.5a7 7 0 0 1 .1 1.2z',
        folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
        git: 'M6 6a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM6 22a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM18 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM6 6v12M18 8v3a4 4 0 0 1-4 4H8',
        remote: 'M3 5h18v11H3zM9 20h6M12 16v4',
        star: 'M12 3l1.9 4.6 4.9.4-3.7 3.2 1.1 4.8L12 13.5 7.8 16l1.1-4.8L5.2 8l4.9-.4z',
        moon: 'M20 13.5A8 8 0 0 1 10.5 4 8 8 0 1 0 20 13.5z',
        book: 'M4 5a2 2 0 0 1 2-2h14v16H6a2 2 0 0 0-2 2zM4 19V5',
        refresh: 'M20 12a8 8 0 1 1-2.3-5.6M20 3v5h-5'
    };

    function showToast(msg) {
        var t = document.getElementById('tbToast');
        if (!t) { t = el('div'); t.id = 'tbToast'; document.body.appendChild(t); }
        t.textContent = msg;
        t.classList.add('show');
        clearTimeout(t._h);
        t._h = setTimeout(function () { t.classList.remove('show'); }, 2400);
    }

    function bridge(path, payload) {
        try {
            return fetch(BRIDGE + path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload || {})
            });
        } catch (e) { return null; }
    }

    // 执行 workbench 命令：IDE 桥 → vscode.commands.executeCommand（真实命令 ID）
    function cmd(name, args, fallback) {
        var c = bridge('/command', { command: name, args: args || [] });
        if (!c) { if (fallback) fallback(); return; }
        c.then(function (r) {
            if (!r.ok && fallback) fallback();
        }).catch(function () { if (fallback) fallback(); });
    }

    function shortcut(o) {
        var e = new KeyboardEvent('keydown', {
            bubbles: true, cancelable: true,
            ctrlKey: !!o.ctrl, altKey: !!o.alt, shiftKey: !!o.shift, metaKey: false
        });
        Object.defineProperty(e, 'keyCode', { get: function () { return o.kc; } });
        Object.defineProperty(e, 'which', { get: function () { return o.kc; } });
        Object.defineProperty(e, 'key', { get: function () { return o.key || ''; } });
        var root = document.querySelector('.monaco-workbench') || document.body;
        root.dispatchEvent(e);
    }

    function openSettings(tab) {
        var r = bridge('/open_settings', { tab: tab || '' });
        if (!r) {
            shortcut({ ctrl: true, alt: true, kc: 188, key: ',' });
            return;
        }
        r.catch(function () { shortcut({ ctrl: true, alt: true, kc: 188, key: ',' }); });
    }

 /* ===== 模式总线（标题栏 ↔ 面板 ↔ 服务端） ===== */
    function postMode(v) {
        mode = v;
        try {
            fetch(SERVER + '/ide_mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: v })
            }).catch(function () { });
        } catch (e) { }
        paintAgentMode();
    }

    function paintAgentMode() {
        if (!agentBtn) return;
        var solo = (mode === 'solo');
        agentBtn.classList.toggle('on', solo);
        agentBtn.title = solo ? 'Agent 模式（点击打开独立 Agent 窗口）' : '打开 Agent 独立窗口（当前 Ask/Plan）';
    }

    function pullMode() {
        try {
            var ctrl = new AbortController();
            var t = setTimeout(function () { ctrl.abort(); }, 1200);
            fetch(SERVER + '/ide_mode', { signal: ctrl.signal }).then(function (r) {
                clearTimeout(t);
                return r.ok ? r.json() : null;
            }).then(function (d) {
                if (!d) return;
                var v = (d.mode === 'solo' || d.mode === 'plan' || d.mode === 'ask') ? d.mode : 'ask';
                if (v !== mode) { mode = v; paintAgentMode(); }
            }).catch(function () { clearTimeout(t); });
        } catch (e) { }
    }

    /* ===== 品牌胶囊 ===== */
    function buildBrand() {
        var b = el('div');
        b.id = 'tbBrand';
        var lg = el('span', 'lg');
        // 代码窗口图标（深色）：外框 + 内部两个小块
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '1.6');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        [['M4 6.5h16v11H4z'], ['M8 10h3v4H8z'], ['M14 10h2.2v4H14z']].forEach(function (d) {
            var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            p.setAttribute('d', d[0]);
            svg.appendChild(p);
        });
        lg.appendChild(svg);
        b.appendChild(lg);
        b.appendChild(el('span', 'ide', 'IDE'));
        return b;
    }

    /* ===== 搜索 ===== */
    function buildSearch() {
        var box = el('div');
        box.id = 'tbSearch';
        var btn = el('button', 'sbBtn');
        btn.title = '搜索（点击展开，回车执行全局搜索，Esc 收起）';
        // 放大镜
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '1.7');
        svg.setAttribute('stroke-linecap', 'round');
        var c1 = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        c1.setAttribute('cx', '11'); c1.setAttribute('cy', '11'); c1.setAttribute('r', '6');
        var p1 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        p1.setAttribute('d', 'M15.5 15.5L21 21');
        svg.appendChild(c1); svg.appendChild(p1);
        btn.appendChild(svg);
        btn.appendChild(el('span', 'sbTxt', '搜索'));
        var input = el('input');
        input.type = 'text';
        input.placeholder = '搜索文件或内容…';
        input.setAttribute('aria-label', '搜索文件或内容');
        box.appendChild(btn);
        box.appendChild(input);

        function open() {
            box.classList.add('open');
            setTimeout(function () { try { input.focus(); } catch (e) { } }, 60);
        }
        function close(clear) {
            box.classList.remove('open');
            if (clear) input.value = '';
            input.blur();
        }
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (box.classList.contains('open')) close(false);
            else open();
        });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); return; }
            if (e.key === 'Enter') {
                e.preventDefault();
                var q = String(input.value || '').trim();
                if (q) {
                    // 全局搜索（文件和内容）：workbench.action.findInFiles 支持 { query, triggerSearch }
                    cmd('workbench.action.findInFiles', [{ query: q, triggerSearch: true }],
                        function () { cmd('workbench.action.quickOpen'); });
                } else {
                    // 空查询 → 快速打开（文件搜索）
                    cmd('workbench.action.quickOpen');
                }
                close(false);
            }
        });
        // 焦点离开且为空时自动收起，避免占位
        input.addEventListener('blur', function () {
            setTimeout(function () { if (!input.value) box.classList.remove('open'); }, 140);
        });
        return box;
    }

    /* ===== 布局状态（真实 DOM 状态，双向同步） ===== */
    function partVisible(sel) {
        var n = document.querySelector(sel);
        if (!n) return false;
        var s = getComputedStyle(n);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        var r = n.getBoundingClientRect();
        return r.width > 1 && r.height > 1;
    }

    function refreshLayout() {
        var sels = ['.part.sidebar', '.part.panel', '.part.auxiliarybar'];
        for (var i = 0; i < layoutBtns.length; i++) {
            var v = partVisible(sels[i]);
            layoutBtns[i].classList.toggle('on', v);
        }
    }

    /* ===== 账户菜单（真实数据 /account/info） ===== */
    function buildAccMenu() {
        var m = el('div');
        m.id = 'accMenu';
        m.addEventListener('click', function (e) { e.stopPropagation(); });
        var head = el('div', 'amHead');
        var av = el('span', 'amAv');
        av.appendChild(icon(ICO.user));
        head.appendChild(av);
        var nb = el('div');
        nb.appendChild(el('div', 'amName', '用户'));
        var plan = el('span', 'amPlan', '本地');
        nb.appendChild(plan);
        head.appendChild(nb);
        m.appendChild(head);
        m.appendChild(el('div', 'amSep'));

        function row(label, ico, fn, right) {
            var r = el('div', 'amRow');
            r.appendChild(icon(ico));
            r.appendChild(el('span', null, label));
            r.appendChild(el('span', 'sp'));
            if (right) r.appendChild(el('span', 'muted', right));
            r.addEventListener('click', function () { toggleMenu(false); fn(); });
            m.appendChild(r);
            return r;
        }
        row('管理账号', ICO.user, function () { openSettings('account'); });
        row('设置', ICO.gear, function () { openSettings(''); });
        row('主题', ICO.moon, function () {
            cmd('workbench.action.selectTheme', null, function () {
                shortcut({ ctrl: true, kc: 75, key: 'k' });
                setTimeout(function () { shortcut({ ctrl: true, kc: 84, key: 't' }); }, 120);
            });
        });
        m.appendChild(el('div', 'amSep'));
        row('帮助文档', ICO.book, function () { window.open(PORTAL, '_blank'); });
        row('检查更新', ICO.refresh, function () {
            fetch(SERVER + '/health').then(function (r) {
                showToast(r.ok ? '当前已是最新版本' : '服务异常');
            }).catch(function () { showToast('AI 服务未连接'); });
        });
        document.body.appendChild(m);

        // 真实账号数据回填
        fetch(SERVER + '/account/info').then(function (r) {
            return r.ok ? r.json() : null;
        }).then(function (d) {
            if (!d) return;
            accInfo = d;
            var nm = m.querySelector('.amName');
            if (nm) nm.textContent = d.name || '用户';
            if (plan) plan.textContent = d.plan || '本地';
        }).catch(function () { });
        return m;
    }

    function toggleMenu(force) {
        if (!accMenu) accMenu = buildAccMenu();
        var open = force !== undefined ? force : !accMenu.classList.contains('open');
        if (open && avatarBtn) {
            var r = avatarBtn.getBoundingClientRect();
            accMenu.style.top = (r.bottom + 6) + 'px';
            accMenu.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
        }
        accMenu.classList.toggle('open', open);
    }

    /* ===== 项目介绍下拉（最近项目真实数据） ===== */
    var portalMenu = null;

    function pmRow(labelText, d, onClick) {
        var row = el('div', 'pmRow');
        row.appendChild(icon(d));
        row.appendChild(el('span', null, labelText));
        row.appendChild(el('span', 'sp'));
        row.addEventListener('click', function (e) {
            e.stopPropagation();
            togglePortal(false);
            try { onClick(); } catch (err) { }
        });
        return row;
    }

    function buildPortal() {
        var m = el('div');
        m.id = 'portalMenu';
        m.addEventListener('click', function (e) { e.stopPropagation(); });
        m.appendChild(pmRow('打开文件夹', ICO.folder, function () { cmd('workbench.action.files.openFolder'); }));
        m.appendChild(pmRow('克隆 Git 仓库', ICO.git, function () { cmd('git.clone'); }));
        m.appendChild(pmRow('连接远程主机', ICO.remote, function () { cmd('workbench.action.remote.showMenu'); }));
        m.appendChild(el('div', 'pmSep'));
        var recent = el('div');
        recent.id = 'pmRecentList';
        m.appendChild(recent);
        document.body.appendChild(m);
        return m;
    }

    function togglePortal(force) {
        if (!portalMenu) portalMenu = buildPortal();
        var open = force !== undefined ? force : !portalMenu.classList.contains('open');
        if (open && portalBtn) {
            var r = portalBtn.getBoundingClientRect();
            portalMenu.style.top = Math.round(r.bottom + 4) + 'px';
            portalMenu.style.left = Math.round(r.left) + 'px';
            portalMenu.style.right = 'auto';
            toggleMenu(false);
            pullRecents();
        }
        portalMenu.classList.toggle('open', open);
    }

    function clearKids(n) { while (n.firstChild) n.removeChild(n.firstChild); }

    function pullRecents() {
        var box = document.getElementById('pmRecentList');
        if (!box) return;
        clearKids(box);
        box.appendChild(el('div', 'pmHead', '最近'));
        var r = bridge('/recent', {});
        if (!r) { box.appendChild(el('div', 'pmHead', '（最近列表不可用）')); return; }
        r.then(function (x) { return x.json(); }).then(function (d) {
            var ws = (d && d.workspaces) || [];
            if (!ws.length) { box.appendChild(el('div', 'pmHead', '（暂无最近项目）')); return; }
            ws.slice(0, 6).forEach(function (w, i) {
                var row = el('div', 'pmRecent');
                var nm = String(w.label || '').trim();
                var mm = nm.match(/[A-Za-z\u4e00-\u9fa5]/);
                row.appendChild(el('span', 'pmAv', mm ? mm[0].toUpperCase() : '项'));
                var main = el('div', 'pmMain');
                main.appendChild(el('span', 'pmName', w.label || w.path));
                main.appendChild(el('span', 'pmPath', w.path));
                row.appendChild(main);
                if (i === 0) row.appendChild(el('span', 'pmT', '✓'));
                row.title = w.path;
                row.addEventListener('click', function (e) {
                    e.stopPropagation();
                    togglePortal(false);
                    bridge('/open_recent', { path: w.path });
                });
                box.appendChild(row);
            });
        }).catch(function () {
            box.appendChild(el('div', 'pmHead', '（最近列表读取失败）'));
        });
    }

    /* ===== 右侧按钮组 ===== */
    function buildRight() {
        var rg = el('div');
        rg.id = 'tbRight';

        var back = el('button', 'tbIco');
        back.title = '后退（Alt+←）';
        back.appendChild(icon('M20 12H5M11 5L4 12l7 7'));
        back.addEventListener('click', function () {
            cmd('workbench.action.navigateBack', null, function () { shortcut({ alt: true, kc: 37, key: 'ArrowLeft' }); });
        });
        rg.appendChild(back);

        var fwd = el('button', 'tbIco');
        fwd.title = '前进（Alt+→）';
        fwd.appendChild(icon('M4 12h15M13 5l7 7-7 7'));
        fwd.addEventListener('click', function () {
            cmd('workbench.action.navigateForward', null, function () { shortcut({ alt: true, kc: 39, key: 'ArrowRight' }); });
        });
        rg.appendChild(fwd);

        layoutBtns = [];
        var layouts = [
            { p: 'M4 5h16v14H4z M8.5 5v14', t: '切换主侧边栏（Ctrl+B）', c: 'workbench.action.toggleSidebarVisibility', k: { ctrl: true, kc: 66, key: 'b' } },
            { p: 'M4 5h16v14H4z M4 13h16', t: '切换面板（Ctrl+J）', c: 'workbench.action.togglePanel', k: { ctrl: true, kc: 74, key: 'j' } },
            { p: 'M4 5h16v14H4z M15.5 5v14', t: '切换 AI 面板（Ctrl+Alt+B）', c: 'workbench.action.toggleAuxiliaryBar', k: { ctrl: true, alt: true, kc: 66, key: 'b' } }
        ];
        layouts.forEach(function (it) {
            var b = el('button', 'tbIco');
            b.title = it.t;
            b.appendChild(icon(it.p));
            b.addEventListener('click', function () {
                cmd(it.c, null, function () { shortcut(it.k); });
                setTimeout(refreshLayout, 120);
                setTimeout(refreshLayout, 400);
            });
            layoutBtns.push(b);
            rg.appendChild(b);
        });

        // 移动 AI 对话面板：先聚焦本视图，再走原生 Move Focused View
        // 弹出的是原生「将视图移动到…」选择框（左侧栏 / 底部面板 / 右侧辅助栏）
        var moveBtn = el('button', 'tbIco');
        moveBtn.title = '移动 AI 对话面板（左侧 / 底部 / 右侧）';
        moveBtn.appendChild(icon('M8 7L4 12l4 5M16 7l4 5-4 5'));
        moveBtn.addEventListener('click', function () {
            cmd('wasily-ai.panelView.focus');
            setTimeout(function () {
                cmd('workbench.action.moveFocusedView', ['wasily-ai.panelView']);
            }, 250);
            setTimeout(refreshLayout, 600);
            setTimeout(refreshLayout, 1200);
        });
        rg.appendChild(moveBtn);

        // 放大 / 还原 AI 面板：复用原生 sash 的拖动机制把面板拉到目标宽度。
        // 窗口很窄时「最大化辅助栏」会被内部状态挡住（可能静默无效），拖动是始终可用的路径。
        var maxBtn = el('button', 'tbIco');
        maxBtn.title = '放大 / 还原 AI 面板';
        maxBtn.appendChild(icon('M9 4H4v5M15 20h5v-5M4 15v5h5M20 9V4h-5'));
        maxBtn.addEventListener('click', function () {
            var bar = document.querySelector('.part.auxiliarybar');
            if (!bar) return;
            var w = bar.getBoundingClientRect().width;
            var wide = Math.round(window.innerWidth * 0.62);
            resizeAuxTo(w > window.innerWidth * 0.55 ? 340 : wide);
            setTimeout(refreshLayout, 250);
            setTimeout(refreshLayout, 800);
        });
        rg.appendChild(maxBtn);

        var sep1 = el('span', 'tbSep');
        rg.appendChild(sep1);

        var gear = el('button', 'tbIco');
        gear.title = '设置（Ctrl+,）';
        gear.appendChild(icon(ICO.gear));
        gear.addEventListener('click', function () { openSettings(''); });
        rg.appendChild(gear);

        avatarBtn = el('button', 'tbAvatar');
        avatarBtn.title = '账户';
        avatarBtn.appendChild(icon(ICO.user));
        avatarBtn.addEventListener('click', function (e) { e.stopPropagation(); toggleMenu(); });
        rg.appendChild(avatarBtn);

        agentBtn = el('button', 'tbAgent');
        agentBtn.title = 'Agent';
        agentBtn.appendChild(document.createTextNode('Agent'));
        agentBtn.appendChild(icon('M8 16L16 8M10 8h6v6'));
        agentBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            toggleMenu(false); // 账户菜单若开着，点 Agent 应先收起
            // Agent 中心是独立 App 窗口；桥不可用时才退回右侧面板
            cmd('wasily-ai.openAgent', [], function () { cmd('wasily-ai.openPanel'); });
            postMode('solo');
            setTimeout(refreshLayout, 200);
        });
        rg.appendChild(agentBtn);

        var sep2 = el('span', 'tbSep');
        rg.appendChild(sep2);

        return rg;
    }

    /* ===== AI 面板资源拖放：Workbench 外层转发，兼容未进入 Webview 的 IDE 内部拖放 ===== */
    function aiDropTarget(target, clientX, clientY) {
        var bar = document.getElementById('workbench.parts.auxiliarybar');
        var view = document.getElementById('workbench.view.extension.wasily-ai.panelView')
            || document.getElementById('workbench.view.extension.wasilyAiBar');
        if (!bar || !view || !target) return false;
        if (!(view.offsetWidth || view.offsetHeight || view.getClientRects().length)) return false;
        // 松手目标可能被 VS Code 替换为栏外节点，实际光标位置才是可靠的落点
        if (typeof clientX === 'number' && typeof clientY === 'number') {
            var r = view.getBoundingClientRect();
            return clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom;
        }
        return bar.contains(target);
    }

    function conversationDrag(dt) {
        return Array.from((dt && dt.types) || []).some(function (t) {
            var type = String(t).toLowerCase();
            return type === 'files' || type === 'codefiles' || type === 'text/uri-list'
                || type === 'resourceurls' || type.indexOf('application/vnd.code.') === 0;
        });
    }

    function droppedPaths(dt) {
        var uris = [];
        function collect(value) {
            if (!value) return;
            if (typeof value === 'string') { uris.push(value); return; }
            if (Array.isArray(value)) { value.forEach(collect); return; }
            if (typeof value === 'object') Object.keys(value).forEach(function (k) { collect(value[k]); });
        }
        Array.from((dt && dt.types) || []).forEach(function (type) {
            try {
                var raw = String(dt.getData(type) || '').trim();
                if (!raw) return;
                try { collect(JSON.parse(raw)); return; } catch (e2) { }
                raw.split(/\r?\n/).forEach(function (line) {
                    line = line.trim();
                    if (line && line.indexOf('#') !== 0) uris.push(line);
                });
            } catch (e2) { }
        });
        var paths = [];
        uris.forEach(function (u) {
            u = String(u || '').trim().replace(/^["']|["']$/g, '');
            var p = u;
            if (u.indexOf('file://') === 0) {
                try { p = decodeURIComponent(u.replace(/^file:\/\/\/?/, '')); }
                catch (e2) { p = u.replace(/^file:\/\/\/?/, ''); }
            }
            if (/^\/[a-zA-Z]:/.test(p)) p = p.slice(1);
            if (!/^[a-zA-Z]:[\\/]/.test(p) && !/^\\\\/.test(p)) return;
            if (paths.indexOf(p) < 0) paths.push(p);
        });
        // 外部文件被 VS Code 截到 Workbench 后，Files 负载没有 URI，必须从 Electron File 取真实路径
        Array.from((dt && dt.files) || []).forEach(function (file) {
            var p = '';
            try {
                var webUtils = window.vscode && window.vscode.webUtils;
                if (webUtils && typeof webUtils.getPathForFile === 'function') {
                    p = webUtils.getPathForFile(file) || '';
                }
                if (!p) p = String(file.path || '');
            } catch (e2) { }
            if (p && paths.indexOf(p) < 0) paths.push(p);
        });
        return paths;
    }

    var aiDropHintVisible = false;
    function setAiDropHint(visible) {
        visible = !!visible;
        if (aiDropHintVisible === visible) return;
        aiDropHintVisible = visible;
        // 只转发状态，实际高亮由 Webview 输入框自己绘制，避免覆盖层尺寸超出
        bridge('/workbench_drag_hint', { active: visible });
    }

    function startAiDropBridge() {
        function nodeInfo(node) {
            return node ? { tag: String(node.tagName || ''), id: String(node.id || ''),
                className: String(node.className || '').slice(0, 300) } : null;
        }
        function dragPayload(stage, e) {
            var dt = e.dataTransfer;
            var view = document.getElementById('workbench.view.extension.wasily-ai.panelView')
                || document.getElementById('workbench.view.extension.wasilyAiBar');
            var rect = view && view.getBoundingClientRect();
            var hit = document.elementFromPoint(e.clientX, e.clientY);
            return { stage: stage, accepted: aiDropTarget(e.target, e.clientX, e.clientY),
                types: Array.from((dt && dt.types) || []),
                fileCount: dt && dt.files ? dt.files.length : 0,
                clientX: e.clientX, clientY: e.clientY,
                viewRect: rect ? { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom } : null,
                target: String((e.target && e.target.tagName) || ''),
                targetId: String((e.target && e.target.id) || ''),
                targetClass: String((e.target && e.target.className) || '').slice(0, 300),
                hit: nodeInfo(hit), defaultPrevented: e.defaultPrevented,
                frames: Array.from(document.querySelectorAll('iframe,webview')).map(function (frame) {
                    var info = nodeInfo(frame);
                    info.pointerEvents = frame.style.pointerEvents || '';
                    return info;
                }) };
        }
        window.addEventListener('dragenter', function (e) {
            if (!conversationDrag(e.dataTransfer) || !aiDropTarget(e.target, e.clientX, e.clientY)) return;
            setAiDropHint(true);
            bridge('/workbench_drag', dragPayload('dragenter', e));
        }, true);
        window.addEventListener('dragover', function (e) {
            if (!conversationDrag(e.dataTransfer)) return;
            if (!aiDropTarget(e.target, e.clientX, e.clientY)) {
                setAiDropHint(false);
                return;
            }
            setAiDropHint(true);
            e.preventDefault();
            e.dataTransfer.dropEffect = 'copy';
        }, true);
        window.addEventListener('dragleave', function (e) {
            if (conversationDrag(e.dataTransfer) && !aiDropTarget(e.target, e.clientX, e.clientY)) {
                setAiDropHint(false);
            }
        }, true);
        window.addEventListener('drop', function (e) {
            setAiDropHint(false);
            var dt = e.dataTransfer;
            var payload = dragPayload('drop-observed', e);
            var paths = droppedPaths(dt);
            var values = {};
            Array.from((dt && dt.types) || []).forEach(function (type) {
                try { values[type] = String(dt.getData(type) || '').slice(0, 4000); } catch (e2) { }
            });
            payload.paths = payload.accepted ? paths : [];
            payload.observedPaths = paths;
            payload.values = values;
            payload.stage = payload.accepted && conversationDrag(dt) ? 'drop' : 'drop-outside';
            var req = bridge('/workbench_drag', payload);
            if (!payload.accepted || !conversationDrag(dt) || !paths.length) return;
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            showToast('正在读取拖入的 ' + paths.length + ' 项…');
            if (req) req.then(function (r) { return r.json(); }).then(function (d) {
                if (!d || !d.ok) showToast('拖入失败：' + ((d && d.error) || 'IDE 桥不可用'));
            }).catch(function (err) { showToast('拖入失败：' + err.message); });
        }, true);
        window.addEventListener('dragend', function () { setAiDropHint(false); }, true);
        window.addEventListener('blur', function () { setAiDropHint(false); }, true);
    }

    /* ===== 任务完成系统通知：扩展宿主 SSE → Electron 原生 Notification ===== */
    var _notifES = null;

    function notifySupported() { return typeof Notification !== 'undefined'; }

    function ackNotify(id, rendered) {
        bridge('/notify_ack', { id: id, rendered: !!rendered });
    }

    function probeNotify() {
        bridge('/notify_probe', {
            has: notifySupported(),
            perm: notifySupported() ? String(Notification.permission) : 'n/a',
            focused: document.hasFocus()
        });
    }

    // 点击通知：优先交给 Electron 主进程（restore/show/focus + 把 sid 回传渲染进程）；
    // 主进程不可用时退回原来的窗口内聚焦 + 桥点击
    function clickViaMain(n) {
        try {
            var ipc = window.vscode && window.vscode.ipcRenderer;
            if (ipc && ipc.send) {
                ipc.send('vscode:wasilyNotifyClick', { id: n.id, sid: n.sid || '' });
                return true;
            }
        } catch (e) { }
        return false;
    }

    // 主进程恢复窗口后会下发 vscode:wasilyNotifyFocus，这里只负责切会话 + 高亮
    function startNotifyFocusBridge() {
        try {
            var ipc = window.vscode && window.vscode.ipcRenderer;
            if (!ipc || !ipc.on) return;
            ipc.on('vscode:wasilyNotifyFocus', function (ev, p) {
                bridge('/notify_click', { id: (p && p.id) || '', sid: (p && p.sid) || '' });
            });
        } catch (e) { }
    }

    function showSysNotify(n) {
        if (!notifySupported() || Notification.permission === 'denied') { ackNotify(n.id, false); return; }
        var note;
        try {
            note = new Notification(n.title || 'Wasily AI', { body: n.body || '', tag: n.id, silent: false });
        } catch (e) { ackNotify(n.id, false); return; }
        var done = false;
        // 测试用：ackDelayMs > 0 时故意延迟回执，验证不会触发兜底双弹
        var ackDelay = Math.max(0, Number(n.ackDelayMs || 0));
        function settle(ok) {
            if (done) return;
            done = true;
            if (ackDelay > 0) setTimeout(function () { ackNotify(n.id, ok); }, ackDelay);
            else ackNotify(n.id, ok);
        }
        note.onshow = function () { settle(true); };
        note.onerror = function () { settle(false); };
        note.onclick = function () {
            settle(true);
            try { note.close(); } catch (e) { }
            if (!clickViaMain(n)) {
                try { window.focus(); } catch (e) { }
                bridge('/notify_click', { id: n.id, sid: n.sid || '' });
            }
        };
        setTimeout(function () { settle(true); }, 4000);
    }

    function startNotifyStream() {
        if (typeof EventSource === 'undefined') { probeNotify(); return; }
        try {
            _notifES = new EventSource(BRIDGE + '/notify_stream');
            _notifES.onopen = function () { probeNotify(); };
            _notifES.onmessage = function (ev) {
                try {
                    var n = JSON.parse(ev.data);
                    if (n && n.id) showSysNotify(n);
                } catch (e) { }
            };
        } catch (e) { }
    }

    /* ===== 组装 ===== */
    function build() {
        var left = document.querySelector('.part.titlebar .titlebar-left');
        var menubar = document.querySelector('.part.titlebar .titlebar-left .menubar');
        var center = document.querySelector('.part.titlebar .titlebar-center');
        var right = document.querySelector('.part.titlebar .titlebar-right');
        if (!left || !menubar || !center || !right) return false;
        if (!document.getElementById('traeBarStyle')) injectStyle();

        // 1) 品牌胶囊（菜单之前）
        if (!brandNode) brandNode = buildBrand();
        if (brandNode.isConnected && menubar.parentNode === left && brandNode.nextSibling !== menubar) {
            left.insertBefore(brandNode, menubar);
        } else if (!brandNode.isConnected) {
            left.insertBefore(brandNode, menubar);
        }

        // 2) 项目介绍（菜单末尾，单行 + 下拉箭头）
        if (!portalBtn) {
            portalBtn = el('div', 'menubar-menu-button tbPortal');
            portalBtn.setAttribute('role', 'menuitem');
            portalBtn.setAttribute('tabindex', '-1');
            portalBtn.title = '项目介绍：打开文件夹 / 克隆 / 最近项目';
            var pIco = icon(ICO.folder, 'tbPIco');
            pIco.setAttribute('width', '13');
            pIco.setAttribute('height', '13');
            portalBtn.appendChild(pIco);
            portalBtn.appendChild(el('div', 'menubar-menu-title', '项目介绍'));
            var caret = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            caret.setAttribute('viewBox', '0 0 24 24');
            caret.setAttribute('fill', 'none');
            caret.setAttribute('stroke', 'currentColor');
            caret.setAttribute('stroke-width', '2');
            caret.setAttribute('class', 'tbCaret');
            var cp = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            cp.setAttribute('d', 'M7 10l5 5 5-5');
            caret.appendChild(cp);
            portalBtn.appendChild(caret);
            portalBtn.addEventListener('click', function (e) { e.stopPropagation(); togglePortal(); });
        }
        // 项目名固定在左侧末尾（不放进 menubar，避免窄窗口被菜单溢出吞掉）
        if (portalBtn.parentNode !== left) {
            if (menubar.nextSibling) left.insertBefore(portalBtn, menubar.nextSibling);
            else left.appendChild(portalBtn);
        }

        // 3) 搜索窄入口（替换原生 command center 的显示位）
        if (!searchBox) searchBox = buildSearch();
        if (!searchSep) searchSep = el('span', 'tbSepC');
        if (center && (!searchBox.isConnected || searchBox.parentNode !== center)) {
            center.appendChild(searchSep);
            center.appendChild(searchBox);
        }

        // 4) 右侧按钮组（插在系统窗口按钮之前）
        if (!rightGroup) rightGroup = buildRight();
        if (!rightGroup.isConnected) right.insertBefore(rightGroup, right.firstChild);
        if (rightGroup.nextSibling && rightGroup.nextSibling.classList &&
            !rightGroup.nextSibling.classList.contains('window-controls-container')) {
            // 保证按钮组紧邻窗口控制按钮
            right.insertBefore(rightGroup, right.firstChild);
        }

        refreshLayout();
        return true;
    }

    // 通知流不依赖标题栏 DOM，立即连接；主进程恢复窗口后的回传监听也一起挂上
    try { probeNotify(); } catch (e) { }
    try { startNotifyStream(); } catch (e) { }
    try { startNotifyFocusBridge(); } catch (e) { }
    try { startAiDropBridge(); } catch (e) { }

    var tries = 0;
    var timer = setInterval(function () {
        if (build()) {
            clearInterval(timer);
            setInterval(function () { try { build(); } catch (e) {} }, 2500);
            try { pullMode(); } catch (e) {}
            setInterval(pullMode, 3000);
            setInterval(refreshLayout, 1000);
            // 面板位置/宽度会随布局抖动，拖条按 400ms 跟随；拖动中自身也会立刻刷新
            setInterval(function () { try { ensureAuxResizer(); restoreAuxWidth(); } catch (e) {} }, 400);
            window.addEventListener('resize', function () {
                try { refreshLayout(); } catch (e) {}
                try { ensureAuxResizer(); } catch (e) {}
            });

            document.addEventListener('click', function (e) {
                if (!e.target || !e.target.closest) return;
                if (searchBox && searchBox.classList.contains('open')
                    && !e.target.closest('#tbSearch')) {
                    searchBox.classList.remove('open');
                }
                if (accMenu && accMenu.classList.contains('open')
                    && !e.target.closest('#accMenu') && !e.target.closest('.tbAvatar')) {
                    accMenu.classList.remove('open');
                }
                if (portalMenu && portalMenu.classList.contains('open')
                    && !e.target.closest('#portalMenu') && !e.target.closest('.tbPortal')) {
                    portalMenu.classList.remove('open');
                }
            });
            document.addEventListener('keydown', function (e) {
                if (e.key !== 'Escape') return;
                if (searchBox && searchBox.classList.contains('open')) {
                    searchBox.classList.remove('open');
                    var si = searchBox.querySelector('input');
                    if (si) { si.value = ''; si.blur(); }
                }
                if (accMenu) accMenu.classList.remove('open');
                if (portalMenu) portalMenu.classList.remove('open');
            });
        } else if (++tries > 60) {
            clearInterval(timer);
        }
    }, 400);
})();
