# 对话页空状态改造（对齐 Trae 新任务页）

## 1. 目标

`/chat`（含 `?embed=agent` 内嵌、`?workbench=1` 工作台两种打开方式）从「图 1」的杂乱工作台视图，
变成 Trae 的两种形态：

| 形态 | 触发条件 | 视觉 |
|---|---|---|
| 空状态 | 当前会话没有任何消息 | 垂直居中大标题 `Build With wasily` + 居中输入卡（+ / 完全访问 / wasily / Auto / 麦克风 / 绿色发送）+ 下方 4 个快捷 chips；隐藏 token 条、`.hint`、大脑条、`#wbToolbar`、`#sideDock` |
| 会话态 | 已有消息 / 在输入框聚焦 / 点了 chip | 标题与 chips 消失，输入卡落回底部单行胶囊，消息列表铺满 |

只改前端 `chat.html`，不动后端、不动 Electron `main.js`（工作台能力保留，只是空状态时收起界面）。

## 2. 现状事实（已核实）

- `chat.html` 结构：`.container` flex 列 = `.header` → `#wbToolbar` → `#groupTimeline` → `#sideDock` → `#brain-slot` → `#tokenBar`/`#tokenDetail` → `.chat-wrap`(`#ptrHint` + `#chat`) → `#inputArea` → `.hint`。
- `body.agent-embed`、`body.workbench` 在 1409–1411 行设置。
- `const inputArea = document.getElementById('inputArea')` 在 2370 行；3580 行 `inputArea.parentNode.insertBefore(lockBanner, inputArea)` 依赖父节点为 `.container`。
- `addMsg()` 3074 行，开头会移除 `#agentWelcome`；`clearChat()` 2511 行；`restoreChat()` 3815 行；`switchToSession()` 3757 行。
- `renderAgentWelcome()` 3798 行把欢迎块 append 到 `#chat`，是顶部对齐的，且工作台模式不生效（`if(!agentEmbed) return`）。
- 初始化在 4757–4764 行：`spLoad(); if(WB_MODE) wbInit(); else spRender(); pollStatus(); if(!WB_MODE) input.focus();`
  —— 这行 `input.focus()` 会与「聚焦即进入会话态」冲突，必须改。
- 空状态旧 CSS 在 1188–1195 行（`.agent-welcome` 系列），将被新的 hero 样式取代。

## 3. 实现方案（decision-complete）

### 3.1 新增 DOM（`chat.html` 1288–1291 行 `.chat-wrap` 内，`#ptrHint` 之后）

```html
<div class="chat-hero" id="chatHero">
  <div class="hero-title">Build With wasily</div>
  <div class="hero-slot" id="heroSlot"></div>
  <div class="hero-chips" id="heroChips">
    <button data-preset="帮我开发一个应用：">应用开发</button>
    <button data-preset="帮我理解这个项目：">项目理解</button>
    <button data-preset="帮我构思一个游戏创意：">游戏创意</button>
    <button data-preset="帮我写一个工具脚本：">工具脚本</button>
  </div>
</div>
```

`#heroSlot` 是输入卡的落位容器：空状态时把 `#inputArea` 搬进来，会话态再搬回 `.hint` 之前。

### 3.2 新增 CSS（追加在 `<style>` 末尾，1203 行 `body.workbench .sp-input button` 之后）

```css
/* ===== Trae 空状态：标题 + 居中输入卡 + 快捷 chips ===== */
.chat-hero { display: none; flex: 1; min-height: 0; flex-direction: column;
  align-items: center; justify-content: center; padding: 0 24px 56px; }
body.chat-empty .chat-hero { display: flex; }
body.chat-empty #chat { display: none; }
body.chat-empty #tokenBar, body.chat-empty #tokenDetail, body.chat-empty .hint,
body.chat-empty #brain-slot, body.chat-empty #wbToolbar, body.chat-empty #groupTimeline,
body.chat-empty #sideDock { display: none !important; }
body.chat-empty .chat-wrap { flex: 0 0 auto; }
.hero-title { font-size: 30px; font-weight: 600; letter-spacing: .4px; color: #f2f2f2;
  margin-bottom: 26px; user-select: none; }
.hero-slot { width: 100%; max-width: 700px; }
body.chat-empty #inputArea { padding: 0; }
body.chat-empty #inputArea .drag-handle { display: none; }
body.chat-empty .composer { border-radius: 14px; background: #1b1b1b; border: 1px solid #2e2e2e;
  box-shadow: 0 10px 34px rgba(0,0,0,.34); }
.hero-chips { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; margin-top: 18px; }
.hero-chips button { border: 1px solid #2e2e2e; background: #1a1a1a; color: #d0d0d0;
  border-radius: 9px; padding: 8px 14px; font-size: 12.5px; font-family: inherit; cursor: pointer; }
.hero-chips button:hover { border-color: #4a4a4a; color: #fff; }
```

删除旧 `.agent-welcome` 三态规则（1188–1195）以及 1024–1029 的旧 welcome 规则，避免重复。

### 3.3 新增 JS（放在 `addMsg` 之前，约 3070 行）

```js
/* 空状态/会话态切换：Trae 的「新任务页 → 对话页」两段式 */
const chatHero = document.getElementById('chatHero');
const heroSlot = document.getElementById('heroSlot');
const hintEl = document.querySelector('.hint');
let chatEmpty = null;

function msgExists() {
  if (document.querySelector('#chat .msg')) return true;
  // 工作台模式消息在侧面板里，只有真正进入工作台且面板有消息才算会话态
  if (WB_MODE && document.querySelector('#sideDock .sp-msgs .msg')) return true;
  return false;
}
function setChatEmpty(on) {
  if (on === chatEmpty) return;
  chatEmpty = on;
  document.body.classList.toggle('chat-empty', on);
  if (on) {
    heroSlot.appendChild(inputArea);          // 输入卡居中
  } else if (hintEl && hintEl.parentNode) {
    hintEl.parentNode.insertBefore(inputArea, hintEl);  // 落回底部
  }
}
function syncChatEmpty() { setChatEmpty(!msgExists()); }
```

### 3.4 状态同步点

| 位置 | 改动 |
|---|---|
| init（4763 行后） | `syncChatEmpty();` |
| init（4764 行） | `if(!WB_MODE) input.focus();` → `if(!WB_MODE && msgExists()) input.focus();` |
| `addMsg()` 末尾 | `syncChatEmpty();`（无论 host 是 `#chat` 还是侧面板） |
| `clearChat()` | `chat.innerHTML=''` 后改为 `syncChatEmpty();`，删掉 `renderAgentWelcome()` |
| `restoreChat()` | `if(!all.length) renderAgentWelcome();` → `syncChatEmpty();` |
| `switchToSession()` | 载入完成后补 `syncChatEmpty();` |
| 删除 `renderAgentWelcome()` 3798–3813 全函数；`addMsg` 开头两行 `#agentWelcome` 清理一并删除 |

### 3.5 进入会话态的交互

```js
function leaveHero() {
  if (chatEmpty) { setChatEmpty(false); setTimeout(() => input.focus({ preventScroll: true }), 0); }
}
input.addEventListener('focus', leaveHero);
input.addEventListener('input', () => { if (input.value.trim()) leaveHero(); });
document.getElementById('heroChips').addEventListener('click', e => {
  const b = e.target.closest('button[data-preset]');
  if (!b) return;
  input.value = b.dataset.preset;
  input.dispatchEvent(new Event('input'));
  leaveHero();
});
```

`leaveHero()` 用 `setTimeout` 重新聚焦：DOM 搬迁会丢失焦点，必须搬完再 focus。
`chatEmpty` 守卫防止 focus 事件与搬迁互相触发死循环。

### 3.6 兼容性约束

- `#inputArea` 搬迁后 `parentNode` 变为 `#heroSlot`；3580 行的 `lockBanner` 插入发生在初始化阶段（父节点仍是 `.container`），不受影响。
- 会话态恢复位置必须是 `.hint` 之前，保证与原有 DOM 顺序一致。
- 工作台模式（`body.workbench`）空状态隐藏 `#sideDock`，首次发送后 `addMsg` 触发 `syncChatEmpty()` 自动恢复面板显示。

## 4. 验收

1. `node --check`：提取 `chat.html` 内联脚本校验通过（沿用 `%TEMP%\extract_js.py`）。
2. 浏览器打开 `http://127.0.0.1:8000/chat?embed=agent&ipc=1`：
   - 初始：标题居中、输入卡在标题下方居中、4 个 chips、无 token 条 / `.hint` / 侧栏；
   - 点输入框：标题与 chips 消失，输入卡落到底部；
   - 点「应用开发」：输入框出现 `帮我开发一个应用：`，同时落底；
   - 控制台无报错。
3. 浏览器打开 `http://127.0.0.1:8000/chat?embed=agent&ipc=1&workbench=1&workbench_id=<有效id>`：
   - 初始不出现 `＋ 新对话 / 对话 N / 独立模式` 工具条与三个下拉；
   - 发送一条后工具条与面板恢复。
4. 主窗口「新对话」清空后回到空状态（IPC `clear` 链路）。

## 5. 不做的事

- 不改 `brain_electron/main.js` 的 `?workbench=1` 启动参数（工作台是多面板能力，保留）。
- 不改 `agent/index.html` 的三栏结构与右侧工作台。
- 不动后端与 IPC 协议。
