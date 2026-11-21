/**
 * preload：向 brain.html 暴露 window.floatAPI（与 pywebview.api 同名方法），
 * 页面通过统一桥接调用，无需感知宿主是 Electron 还是 pywebview。
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('floatAPI', {
  isElectron: true,
  quit: () => ipcRenderer.send('quit'),
  hide_brain: () => ipcRenderer.send('hide-brain'),
  toggle_chat: () => ipcRenderer.send('open-chat'),
  open_chat_settings: () => ipcRenderer.send('open-chat-settings'),
  // 功能页面组：白名单内的独立页面经主进程 shell.openExternal 打开
  open_page: (p) => ipcRenderer.send('open-page', p),
  // 内嵌窗口打开（系统设置 ui.page_open_mode=embed 时）
  open_page_embed: (p) => ipcRenderer.send('open-page-embed', p),
  set_on_top: (on) => ipcRenderer.send('set-on-top', on),
  set_opacity: (v) => ipcRenderer.send('set-opacity', v),
  // Electron per-pixel alpha 原生透明，无需异形区域；空实现兼容页面调用
  set_shape: () => {},
  set_perf_mode: () => {},
  // 拖动移动窗口：增量拖动只通知 start/end，主进程轮询光标 delta 跟随
  drag_start: () => ipcRenderer.send('brain-drag-start'),
  drag_end: () => ipcRenderer.send('brain-drag-end'),
  // 双击复位：窗口回到主屏工作区居中
  brain_recenter: () => ipcRenderer.send('brain-recenter'),
  // 点击穿透开关：光标在空白处时鼠标事件穿透到桌面（forward 保持 mousemove 转发）
  set_click_through: (on) => ipcRenderer.send('set-click-through', on),
  // endDrag 主进程 setBounds 到目标屏后，通知前端 pan 同步让大脑视觉上停在光标位置
  on_pan_sync: (cb) => ipcRenderer.on('brain-pan-sync', (_e, data) => cb(data)),
});

contextBridge.exposeInMainWorld('workbenchAPI', {
  isElectron: true,
  minimize: () => ipcRenderer.send('workbench-minimize'),
  maximizeOrRestore: () => ipcRenderer.send('workbench-maximize-or-restore'),
  close: () => ipcRenderer.send('workbench-close'),
  // 站内页面（更新日志 / 帮助文档）：由主进程开受控窗口，保证可见可交互
  openPage: (p, title) => ipcRenderer.send('workbench-open-page', p, title || 'wasily'),
  onState: (cb) => {
    const listener = (_e, state) => cb(state);
    ipcRenderer.on('workbench-state', listener);
    return () => ipcRenderer.removeListener('workbench-state', listener);
  },
});
