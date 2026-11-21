/**
 * wasily Brain Float —— Electron 桌面悬浮 3D 大脑
 *
 * 与 brain_desktop.py(pywebview) 的区别：
 *   Electron 的 transparent BrowserWindow 在 Windows 上走 Chromium 原生
 *   per-pixel alpha 合成 —— 页面透明像素真正透出桌面，无黑底/无矩形残影，
 *   也不需要 SetWindowRgn 异形裁剪（大脑线网缝隙、半球中缝全部真透明）。
 *
 * 全工作区透明覆盖层、无边框、置顶；大脑可无级放大铺满桌面（无窗口边框裁剪），
 * 空白区域动态点击穿透不影响桌面操作；拖大脑本体可移动其桌面位置。
 * 由 api_server 的 /spawn_brain 拉起（独立运行时自动等待/拉起服务）。
 */
const { app, BrowserWindow, ipcMain, screen, shell, Tray, Menu, nativeImage } = require('electron');
/* 禁用 GPU 硬件加速：透明窗口在部分 Windows 显卡驱动（NVIDIA/Intel）上
   硬件合成会输出黑色矩形残影（整窗黑底/黑框），CPU 软件合成可彻底规避；
   大脑为单画布 2D 渲染，CPU 合成开销可接受 */
app.disableHardwareAcceleration();
const http = require('http');
const path = require('path');
const { spawn } = require('child_process');

const SERVER_URL = 'http://127.0.0.1:8000';
const BRAIN_URL = SERVER_URL + '/brain?float=1';

let win = null;
let tray = null;
const workbenchWindows = new Map();

function isValidWorkbenchId(id) {
  return typeof id === 'string' && id.length >= 1 && id.length <= 128 && /^[A-Za-z0-9_-]+$/.test(id);
}

function agentWorkbenchId(argv) {
  if (!argv.includes('--agent')) return null;
  const arg = argv.find(a => a.startsWith('--workbench-id='));
  const id = arg ? arg.slice('--workbench-id='.length) : '';
  return isValidWorkbenchId(id) ? id : null;
}

/* 工作台窗口落点：显式按主屏工作区居中。
   不能只靠 BrowserWindow 的 center:true —— 多显示器下它可能把窗口放到
   副屏（甚至整窗落在主屏上方，y 为负），用户在主屏看不到任何东西，
   表现为「点了 Agent 没反应」。 */
function workbenchBounds() {
  const area = screen.getPrimaryDisplay().workArea;
  const width = Math.min(1280, Math.max(640, area.width - 80));
  const height = Math.min(840, Math.max(480, area.height - 80));
  return {
    width,
    height,
    x: Math.round(area.x + (area.width - width) / 2),
    y: Math.round(area.y + (area.height - height) / 2),
  };
}

/* 至少 40% 面积落在某块屏幕工作区内才算「用户看得见」 */
function isOnScreen(b) {
  return screen.getAllDisplays().some(d => {
    const a = d.workArea;
    const ix = Math.max(0, Math.min(b.x + b.width, a.x + a.width) - Math.max(b.x, a.x));
    const iy = Math.max(0, Math.min(b.y + b.height, a.y + a.height) - Math.max(b.y, a.y));
    return ix * iy >= b.width * b.height * 0.4;
  });
}

/* 已存在的窗口可能在屏幕外：先纠正位置再前置 */
function revealOnScreen(w) {
  if (!w || w.isDestroyed()) return;
  if (!isOnScreen(w.getBounds())) w.setBounds(workbenchBounds());
  if (w.isMinimized()) w.restore();
  w.show();
  w.focus();
}

const agentOnly = process.argv.includes('--agent');
const initialWorkbenchId = agentWorkbenchId(process.argv);

/* ---------- 托盘图标（右下角系统托盘，不占任务栏） ---------- */
function toggleBrainWindow() {
  if (!win) {
    startBrain();
    return;
  }
  if (win.isVisible()) {
    win.hide();
  } else {
    win.show();
    win.setAlwaysOnTop(true, 'screen-saver');
  }
}

function createTray() {
  if (tray) return;
  let img = nativeImage.createFromPath(path.join(__dirname, 'tray.png'));
  if (!img.isEmpty()) img = img.resize({ width: 16, height: 16 });
  tray = new Tray(img);
  tray.setToolTip('wasily Brain');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '显示 / 隐藏大脑', click: toggleBrainWindow },
    { label: '打开对话', click: () => shell.openExternal(SERVER_URL + '/') },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() },
  ]));
  tray.on('click', toggleBrainWindow);
}

/* ---------- 单实例：Agent 参数路由到独立工作台，普通启动仍前置大脑 ---------- */
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', (_event, commandLine) => {
    if (commandLine.includes('--agent')) {
      const id = agentWorkbenchId(commandLine);
      if (id) {
        openWorkbench(id).catch(e => console.error('[Workbench] 启动失败', e));
      } else {
        console.error('[Workbench] 缺少合法的 --workbench-id');
      }
      return;
    }
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.setAlwaysOnTop(true);
      win.focus();
    } else {
      startBrain().catch(e => console.error('[BrainFloat] 启动失败', e));
    }
  });
  app.whenReady().then(async () => {
    if (agentOnly) {
      if (!initialWorkbenchId) {
        console.error('[Workbench] Agent 模式需要合法的 --workbench-id');
        app.quit();
        return;
      }
      if (!(await openWorkbench(initialWorkbenchId))) app.quit();
      return;
    }
    await startBrain();
  }).catch(e => {
    console.error('[BrainFloat] 启动失败', e);
    app.quit();
  });
}

function serverAlive() {
  return new Promise(resolve => {
    const req = http.get(SERVER_URL + '/health', r => {
      r.resume();
      resolve(r.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.setTimeout(1500, () => { req.destroy(); resolve(false); });
  });
}

async function waitServer(maxMs = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    if (await serverAlive()) return true;
    await new Promise(r => setTimeout(r, 1000));
  }
  return false;
}

async function startBrain() {
  if (win) {
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    return true;
  }
  if (!(await waitServer())) {
    console.error('[BrainFloat] 模型服务未就绪，退出');
    app.quit();
    return;
  }

  // 单屏透明覆盖层（当前显示器工作区）：拖动大脑 = 窗口整体迁移到目标位置，
  // 跨显示器时窗口搬到新屏并自动适配该屏 DPI/尺寸（混合 DPI 下跨屏窗口
  // 会被 Windows 虚拟化裁剪，故不做跨屏大窗口）；透明区域动态点击穿透
  const wa = screen.getPrimaryDisplay().workArea;

  win = new BrowserWindow({
    width: wa.width,
    height: wa.height,
    x: wa.x,
    y: wa.y,
    frame: false,
    transparent: true,          // per-pixel alpha 真透明
    resizable: true,            // 无边框窗口没有调整边框，用户无法手动改尺寸；
                                // 必须为 true：跨屏迁移要对齐目标屏工作区（分辨率/DPI 不同），
                                // resizable:false 时 setBounds 改尺寸会被忽略，窗口直接消失
    movable: true,              // 必须为 true：Windows 上 movable:false 会连程序化
                                // setPosition/setBounds 一起禁用（electron#10891），
                                // 窗口迁移式拖动依赖 setPosition；无边框窗口
                                // 没有标题栏，用户无法手动拖动，安全
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,          // 运行图标只放右下角托盘，不占任务栏
    alwaysOnTop: true,
    hasShadow: false,
    backgroundColor: '#00000000',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      zoomFactor: 1.0,
    },
  });

  // 右下角系统托盘图标（任务栏不显示运行图标）
  createTray();

  // 尺寸仅由拖动结束时的跨屏吸附（setBounds 对齐光标所在屏工作区）改变，
  // 页面自身不做任何运行时窗口尺寸调整
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  // 初始鼠标穿透：页面按光标命中状态实时切换（悬停大脑=接管，空白=穿透到桌面）
  win.setIgnoreMouseEvents(true, { forward: true });

  // 固定窗口标题为 wasily Brain Float（页面 <title> 是 wasily Brain，
  // 必须拦截 title 更新，避免与 agent.py 的精确窗口枚举冲突）
  win.setTitle('wasily Brain Float');
  win.on('page-title-updated', e => {
    e.preventDefault();
    win.setTitle('wasily Brain Float');
  });
  win.webContents.on('did-finish-load', () => win.setTitle('wasily Brain Float'));

  win.once('ready-to-show', () => win.show());
  win.loadURL(BRAIN_URL);

  win.on('closed', () => { win = null; });
  return true;
}

/* ---------- Agent 原生工作台：与悬浮大脑相互独立 ---------- */
async function openWorkbench(id) {
  if (!isValidWorkbenchId(id)) return false;
  const existing = workbenchWindows.get(id);
  if (existing && !existing.isDestroyed()) {
    revealOnScreen(existing);
    return true;
  }
  if (existing) workbenchWindows.delete(id);
  if (!(await waitServer())) {
    console.error('[Workbench] 模型服务未就绪');
    return false;
  }

  // 等待服务期间可能已有另一个 second-instance 创建同一工作台
  const current = workbenchWindows.get(id);
  if (current && !current.isDestroyed()) {
    revealOnScreen(current);
    return true;
  }

  const wbBounds = workbenchBounds();
  const wb = new BrowserWindow({
    width: wbBounds.width,
    height: wbBounds.height,
    x: wbBounds.x,
    y: wbBounds.y,
    frame: false,
    transparent: false,
    resizable: true,
    minimizable: true,
    maximizable: true,
    skipTaskbar: false,
    alwaysOnTop: false,
    autoHideMenuBar: true,
    backgroundColor: '#0a0e18',
    title: 'wasily Agent',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  workbenchWindows.set(id, wb);

  const sendState = state => {
    if (!wb.isDestroyed() && !wb.webContents.isDestroyed()) {
      wb.webContents.send('workbench-state', state);
    }
  };
  wb.on('maximize', () => sendState('max'));
  wb.on('unmaximize', () => sendState('restore'));
  wb.on('restore', () => sendState('restore'));
  wb.webContents.on('did-finish-load', () => sendState(wb.isMaximized() ? 'max' : 'restore'));
  wb.once('ready-to-show', () => {
    if (wb.isDestroyed()) return;
    // 显示前再校验一次：显示器热插拔/缩放变化可能把窗口推到屏幕外
    if (!isOnScreen(wb.getBounds())) wb.setBounds(workbenchBounds());
    wb.show();
    wb.focus();
  });
  wb.on('closed', () => {
    if (workbenchWindows.get(id) === wb) workbenchWindows.delete(id);
  });

  await wb.loadURL(SERVER_URL + '/agent?workbench=1&workbench_id=' + encodeURIComponent(id));
  return true;
}

function workbenchFromSender(event) {
  for (const wb of workbenchWindows.values()) {
    if (!wb.isDestroyed() && wb.webContents === event.sender) return wb;
  }
  return null;
}

ipcMain.on('workbench-minimize', event => {
  const wb = workbenchFromSender(event);
  if (wb) wb.minimize();
});
ipcMain.on('workbench-maximize-or-restore', event => {
  const wb = workbenchFromSender(event);
  if (!wb) return;
  if (wb.isMaximized()) wb.unmaximize();
  else wb.maximize();
});
ipcMain.on('workbench-close', event => {
  const wb = workbenchFromSender(event);
  if (wb) wb.close();
});

/* ---------- 页面右键菜单 IPC ---------- */
ipcMain.on('quit', () => app.quit());
ipcMain.on('hide-brain', () => { if (win) win.hide(); });
ipcMain.on('open-chat', () => shell.openExternal(SERVER_URL + '/'));
ipcMain.on('open-chat-settings', () => shell.openExternal(SERVER_URL + '/settings'));
/* 功能页面组：白名单校验后在默认浏览器打开服务端独立页面，防任意 URL 注入
   只校验路径部分，允许携带 ?mode=edit 之类的受控查询参数 */
const OPEN_PAGE_WHITELIST = new Set(['/', '/brain', '/history', '/train', '/video_call', '/settings', '/flowviz', '/docs', '/changelog']);
const isAllowedPage = p => typeof p === 'string' && OPEN_PAGE_WHITELIST.has(p.split('?')[0]);
ipcMain.on('open-page', (_e, p) => {
  if (isAllowedPage(p)) shell.openExternal(SERVER_URL + p);
});
/* 内嵌窗口打开（系统设置 ui.page_open_mode=embed）：应用内 BrowserWindow，
   独立于桌面大脑窗口，关闭互不影响 */
ipcMain.on('open-page-embed', (_e, p) => {
  if (!isAllowedPage(p)) return;
  const ew = new BrowserWindow({
    width: 1100, height: 760,
    autoHideMenuBar: true,
    backgroundColor: '#0a0e18',
    title: 'wasily',
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  ew.loadURL(SERVER_URL + p);
});
/* 工作台里的站内链接：用受控窗口打开并置顶到父窗口中央。
   window.open 在部分机器会被拦或开到屏幕外，用户表现为「点了没反应」。 */
ipcMain.on('workbench-open-page', (event, pathname, title) => {
  if (!isAllowedPage(pathname)) return;
  const parent = workbenchFromSender(event);
  const rect = parent && !parent.isDestroyed() ? parent.getBounds() : null;
  const width = 1100, height = 760;
  const ew = new BrowserWindow({
    width, height,
    x: rect ? Math.round(rect.x + (rect.width - width) / 2) : undefined,
    y: rect ? Math.round(rect.y + (rect.height - height) / 2) : undefined,
    autoHideMenuBar: true,
    backgroundColor: '#181818',
    title: title || 'wasily',
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  ew.once('ready-to-show', () => { ew.show(); ew.focus(); });
  ew.loadURL(SERVER_URL + pathname);
});
ipcMain.on('set-on-top', (_e, on) => { if (win) win.setAlwaysOnTop(!!on); });
ipcMain.on('set-opacity', (_e, v) => {
  if (win) win.setOpacity(Math.max(0.2, Math.min(1, Number(v) / 100)));
});

/* ---------- 页面拖动 = 窗口整体迁移（跨显示器安全） ----------
   增量拖动：只跟踪光标 delta，不依赖窗口尺寸或坐标系换算。
   跨屏 DPR 变化时仍然正确，Windows 自动适配目标屏 DPI。 */
let dragTimer = null, lastDragCursorX = 0, lastDragCursorY = 0;
ipcMain.on('brain-drag-start', (_e) => {
  if (!win) return;
  const c = screen.getCursorScreenPoint();
  lastDragCursorX = c.x; lastDragCursorY = c.y;
  clearInterval(dragTimer);
  dragTimer = setInterval(() => {
    if (!win) { clearInterval(dragTimer); dragTimer = null; return; }
    const p = screen.getCursorScreenPoint();
    const dx = p.x - lastDragCursorX;
    const dy = p.y - lastDragCursorY;
    lastDragCursorX = p.x; lastDragCursorY = p.y;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;   // 过滤微抖动
    const b = win.getBounds();
    win.setPosition(b.x + dx, b.y + dy);
  }, 16);   // ~60fps，跟手流畅
});
ipcMain.on('brain-drag-end', () => {
  clearInterval(dragTimer); dragTimer = null;
  if (!win) return;
  const cursorPoint = screen.getCursorScreenPoint();
  const targetDisplay = screen.getDisplayNearestPoint(cursorPoint);
  const curBounds = win.getBounds();
  const curCenter = { x: curBounds.x + curBounds.width / 2, y: curBounds.y + curBounds.height / 2 };
  const curDisplay = screen.getDisplayNearestPoint(curCenter);
  /* 只在跨屏时才 setBounds 适配新屏 DPI/分辨率；
     同屏拖动窗口已正确定位，无需重新吸附工作区 */
  if (targetDisplay.id !== curDisplay.id) {
    const wa = targetDisplay.workArea;
    win.setBounds({ x: wa.x, y: wa.y, width: wa.width, height: wa.height });
    // 光标在新窗口内的 CSS 像素坐标（Windows/Electron 的 screen API 返回 CSS px）
    const clientX = cursorPoint.x - wa.x;
    const clientY = cursorPoint.y - wa.y;
    win.webContents.send('brain-pan-sync', { clientX, clientY });
  }
});
ipcMain.on('brain-recenter', () => {
  if (!win) return;
  const wa = screen.getPrimaryDisplay().workArea;
  const wb = win.getBounds();
  win.setPosition(wa.x + Math.round((wa.width - wb.width) / 2),
                  wa.y + Math.round((wa.height - wb.height) / 2));
});
ipcMain.on('set-click-through', (_e, on) => {
  if (win) win.setIgnoreMouseEvents(!!on, { forward: true });
});
