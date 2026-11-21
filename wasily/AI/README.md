# wasily（瓦西里）AI 智能体系统 · README

> 版本 0.3.0 / build 2026.11.19　|　本 README 于 2026-11-20 按实测重写
> 上一版备份：`README.md.bak_20261120`
> 全局入口文档（架构、能力矩阵、端口全表）：上级目录的 `项目总介绍.md`

完全本地运行的 AI 智能体工作台：本地大模型推理 + 对话/Agent 界面 + 自研 IDE + MCP 工具总线 + 本地知识库 + 分层长期记忆 + 站点托管 + 语音数字人。数据不出本机，断网可用。

---

## 一、环境与硬件基线

| 项目 | 实测值 |
|---|---|
| 系统 | Windows 10/11 |
| Python | 3.12.10（`C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`） |
| CPU | i5-11300H，4 核 8 线程 |
| 内存 | 15.8 GB |
| 显卡 | MX450，2 GB 显存，CUDA 12.7 |
| 默认模型 | Qwen2.5-3B-Instruct Q4_K_M（`D:\wasily_models\qwen2_5_3b\`） |
| 上下文 / 单次上限 | `n_ctx 8192` / `max_generate_tokens 512` |
| 采样参数 | `temperature 0.7`、`top_p 0.9` |
| 7B 首句延迟 | 10–15 秒（纯 CPU） |

**继承结论**：2 GB 显存不支持视频级数字人（Wav2Lip 需 4 GB、MuseTalk 需 6 GB）；首句慢是硬件天花板，换小模型或换机器才有效。

---

## 二、启动

### 2.1 推荐：直接起后端（已验证可用）

```powershell
cd "D:\煮饭仔点餐\项目文件\项目介绍\wasily\AI"
python api_server.py
```

首次启动要加载模型，约 30–60 秒后 `http://127.0.0.1:8000/health` 返回 `status: ok`。

### 2.2 后台静默启动（脚本用同一套）

```powershell
$py='C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe'
$ai='D:\煮饭仔点餐\项目文件\项目介绍\wasily\AI'
Start-Process -FilePath $py -ArgumentList '-X','utf8','api_server.py' -WorkingDirectory $ai `
  -RedirectStandardOutput "$ai\api_run.log" -RedirectStandardError "$ai\api_err.log" -WindowStyle Hidden
```

### 2.3 IDE

```powershell
& "D:\煮饭仔点餐\项目文件\项目介绍\wasily\tools\wasily-ide\wasily-ide.exe" "D:\煮饭仔点餐\项目文件\项目介绍"
```

IDE 启动后 `http://127.0.0.1:18753/health` 返回 `{"ok":true}`，`/context` 会报出当前工作区与正打开的文件。

### 2.4 ⚠️ 关于 `启动Wasily.bat`

**当前不可用。** 2026-11-20 实测：执行后 `8000` 始终拒绝连接、无 python 进程存活、日志无新增。用 2.1 的方式可以正常起服务，45 秒内 `/health` 即返回 ok。脚本里待排查的一项是 `Start-Process -WindowStyle Hidden -RedirectStandardOutput` 在含中文的路径下的行为。修好之前请按 2.1 / 2.2 启动。

---

## 三、页面入口（全部为 2026-11-20 实测 HTTP 200）

| 地址 | 页面 | 实际来源文件 |
|---|---|---|
| `/` | 对话主窗口（工具调用、语音输入、TTS、历史抽屉） | `chat.html` |
| `/chat` | 对话页（可带 `?embed=agent` 内嵌） | `chat.html` |
| `/agent` | Agent 任务台（新建任务、插件市场、模板库、我的产物、我的站点） | `agent\index.html` |
| `/settings` | 设置中心；`/settings#usage` 直达用量管理 | `settings_center.html` |
| `/kb` | 本地知识库 | `kb.html` |
| `/apps` | 内置应用 | `apps.html` |
| `/portal` | 门户页 | `portal.html` |
| `/docs` | 文档站（`?project=xxx` 注入 `docs.json`） | `docs.html` |
| `/brain` | 桌面大脑（浏览器预览版） | `brain.html` |
| `/group` | 圆桌讨论 | `group.html` |
| `/flowviz` | 链路可视化 | `flowviz.html` |
| `/changelog` | 版本更新说明 | `changelog.html` |
| `/avatar` | AI 形象预览 | `avatar.html` |
| `/history` | 历史对话管理 | `_archive\history.html` |
| `/train` | 自我训练台 | `_archive\train.html` |
| `/video_call` | 视频通话 | `_archive\video_call.html` |

> `/history`、`/train`、`/video_call` 三个页面文件已移入 `_archive\`，但路由仍指向它们，页面可正常打开。属于「归档未摘路由」，改动这三个页面时记得去 `_archive\` 下找。
> 另有 `_archive\settings.html` 作为 `/settings` 的回退。

---

## 四、模型切换

`config.json` 的 `models` 数组与 `current_model` 决定当前模型：

| id | 模型 | 位置 | GPU 层 |
|---|---|---|---|
| `local-3b` | Qwen2.5-3B Q4_K_M（**当前默认**） | `D:\wasily_models\qwen2_5_3b\` | 0 |
| `local` | Qwen2.5-7B Q4_K_M | `models_7b\` | 7 |
| `local-1.5b` | Qwen2.5-1.5B Q4_K_M | `D:\wasily_models\qwen2_5_1_5b\` | -1 |
| `local-large` | 14B（需自行放入） | `models_14b\` | 未设 |
| `remote-openai` | gpt-4o-mini | 远程 API | — |
| `remote-deepseek` | deepseek-chat | 远程 API | — |

切换后需要在设置页或 `POST /v1/ide/models/switch` 生效。远程模型需先填 `api_key`。

---

## 五、能力模块（对应代码）

| 模块 | 关键文件 | 说明 |
|---|---|---|
| 主服务 | `api_server.py` | 单进程 `ThreadingHTTPServer`，140 条 `parsed_path ==` 分支；负责模型路由、页面宿主、工具编排、MCP 网关、SSE 流式 |
| 对话界面 | `chat.html` | 流式思考、工具卡片、停止、推荐胶囊、积分芯片、编辑名称 |
| Agent 任务台 | `agent\index.html` | 任务、插件、模板、产物、站点 |
| IDE 面板 | `ide_panel.html`（由 `GET /v1/ide/panel` 下发） | 与 chat 同组件，改这一个文件即可远程更新所有编辑器 |
| 工具 | `tools.py`（128 KB）+ `user_tools\`（37 个文件） | 运行期实测注册 **201 个工具**（内置 170 + 用户 31） |
| MCP | `mcp_hub.py`、`mcp.json` | 9 个 server，实测 9/9 connected：self / echo / filesystem / playwright / remote / anythingllm / kb / browser / blender |
| 编排 | `orchestrator.py`、`subagents.py`、`task_router.py` | 任务分解、子智能体派生、路由；配合 `/plan/confirm`、`/plan/status` 由用户确认后执行 |
| 规则层 | `kernel.py` | L0 全局 `AI\rules\*.md` → L1 项目规则（向当前目录上溯 3 层的 `.traerules`/`.wasilyrules`/`RULES.md`/`AGENT.md`/`AGENTS.md`/`project_rules.md`） |
| 知识库 | `kb_*.py`（14 个）、`obsidian_kb.py`、`anythingllm_bridge.py` | 全盘只读索引、检索、摘要、报告；Obsidian 索引 2.8 MB / 向量库 15.5 MB |
| 长期记忆 | `memory_net.py`、`memory_vector.py`、`secure_store.py` | L1 摘要 / L2 事实 / L3 认知；BGE-small-zh 语义召回；正文与向量分离加密 |
| 加密存储 | `secure_store.py`、`key.bin` | AES + Windows DPAPI 护钥 |
| 语音与数字人 | `voice.py`、`silk_voice.py`、`video_chat.py`、`live_avatar.js` | SAPI 离线 / edge-tts 在线 / F5 克隆三档；微信式通话页 |
| 照片转 3D | `triposr_cli.py`、`photo3d.js`、`import_render.py` | 照片生成 GLB + 骨骼绑定 |
| 训练 | `lora_lab.py`、`train_data.py`、`train_assess.py`、`finetune\` | 数据收集、QLoRA、导出与应用 |
| 站点托管 | `sites_net.py`、`site_api.py`、`sites_store.json` | 一键部署、端口分配、访问统计；当前 3 个站点跑在 8300–8302 |
| 运维 | `watchdog.py`、`self_restarter.py`、`update_service.py`、`version.json` | 看门狗、自重启、更新检查 |

---

## 六、L1–L5 自主编程能力

上一版 README 在此处截断，以下为补写。**注意**：原文档所列的 `plan_task` 工具在当前 `tools.py` 中已不存在，L4 改由「编排层分解 + 用户确认」承担，见下表。

| 层级 | 名称 | 实际工具 / 机制 | 能力 | 安全约束 |
|---|---|---|---|---|
| **L1** | 代码理解 | `code_read`、`code_search` | 带行号读取源码；正则搜索项目代码 | 路径白名单（`agent_policy.roots`）校验 |
| **L2** | 代码修改 | `apply_patch` | 应用 unified diff，支持 `dry_run` 预览 | 上下文精确匹配 + 行数上限；可撤销（`/undo/step`、`/undo/to`） |
| **L3** | 安全执行 | `run_code` | 在受控子进程中运行 Python 验证代码 | 危险模块黑名单 + 超时控制 |
| **L4** | 自主规划 | `orchestrator.py` + `task_router.py` + `/plan/confirm`、`/plan/status` | 把高层目标分解为步骤序列，交用户确认后再执行 | 纯推理阶段无副作用；确认前不动文件 |
| **L5** | 结果反馈 | 强制自检 + `changes` diff 审阅 + `/changes/accept`、`/changes/reject` | 修改后自检并向用户报告变更摘要，改动需人工接受 | 未接受的变更不落定 |

### 6.1 标准工作流

1. **读**：`code_read` / `file_read` 定位目标代码，确认现状（不猜内容）。
2. **搜**：`code_search` 找调用点与相关实现，评估影响面。
3. **改**：`apply_patch` 先 `dry_run` 预览，再正式应用；或用 `ide_edit_file` 在 IDE 内改。
4. **验**：Python 改完 `python -c "import py_compile; py_compile.compile(r'<path>', doraise=True)"`；`extension.js` / `mcpHost.js` 改完 `node --check <path>`；后端逻辑改完重启并确认 `/health`。
5. **报**：汇总改了什么、为什么改、影响哪些文件。
6. **审**：改动进入 changes 记录，由用户 `/changes/accept` 或 `/changes/reject`。

### 6.2 权限边界

`config.json → agent_policy` 当前为 `role: operator`，`caps: ["exec","read","write"]`，`roots` 仅允许 `D:\煮饭仔点餐\项目文件\项目介绍` 与系统 Temp，`allow_mcp_write: false`。超出 roots 的写请求会被拦截，不是靠提示词约束。

---

## 七、配置要点（`config.json`）

```jsonc
{
  "server": { "port": 8000, "n_ctx": 8192, "max_generate_tokens": 512, "n_gpu_layers": 0 },
  "current_model": "local-3b",
  "ai_name": "小助手",
  "resource": { "enabled": true, "cpu_infer_level": 70, "cpu_conflict_level": 85 },  // S0~S3 四级降载
  "tts": { "voice": "wxx_clone", "fallback_sapi": true },
  "memory": { "vector": { "enabled": true, "model": "BAAI/bge-small-zh-v1.5", "top_k": 5, "inject_top": 3, "min_score": 0.45 } },
  "kb": { "roots": ["C:\\", "D:\\"], "max_chunk_chars": 700, "max_db_gb": 3.0 },
  "browser": { "cdp_port": 9222 },
  "train": { "rounds": 3, "turns_per_topic": 4, "eval_threshold": 4 },
  "install_tool": { "allow": true, "allow_confirm": true, "whitelist": ["psutil", "requests", ...] }
}
```

在线改配置：`POST /config/set {"path":"ui.opacity","value":0.8}`，改完持久化。

---

## 八、数据与日志

| 文件 | 说明 |
|---|---|
| `history.enc` / `audit.enc` | 加密的对话历史 / AI 行为审计 |
| `mem_layers.enc` / `mem_pending.enc` | 分层记忆正文 / 待压缩缓冲 |
| `learnings.enc` | 强制规则库（`memory_save/list/forget` 写入，全量注入提示词） |
| `mem_vectors.db` | 记忆向量索引（SQLite，仅存向量与 id，不含正文） |
| `obsidian_kb_vectors.db` / `obsidian_kb_chunks.jsonl` | Obsidian 知识库索引 |
| `sites_store.json` | 站点托管记录与访问统计 |
| `config.json` | 全部配置，缺字段自动补默认 |
| `wasily_log\` | 运行日志（RotatingFileHandler） |
| `api_run.log` / `api_err.log` | 后台启动时的 stdout / stderr |

---

## 九、故障排查

1. **端口被占用**：改 `config.json → server.port`，或 `netstat -ano | findstr :8000`。
2. **GPU 未生效**：`python gpu_setup.py check`；启动日志看 `[GPU] 已启用 GPU 卸载: N/28 层`。本机显存 2 GB，实际多为 0–7 层。
3. **大脑动画卡顿**：`resource_manager.py` 会按 S0–S3 自动降 FPS/粒子；也可手动暂停或调 `cpu_conflict_level`。
4. **语音没声音**：edge-tts 需联网，断网自动回退 Windows SAPI。
5. **知识库扫不到**：SSDP 需路由器放行组播；权限不足或命中排除目录时记为 skipped，不是错误。
6. **页面空白**：`/history`、`/train`、`/video_call` 的源文件在 `_archive\`，改错目录会出现路由正常但内容不对。

---

## 十、文档索引

| 文档 | 内容 |
|---|---|
| `项目总介绍.md`（工程根） | 架构、能力矩阵、端口全表、文档一致性清单 |
| `AGENTS.md`（工程根） | 仓库规则与自检口径 |
| `.traerules`（本目录） | 项目级技术栈约束（会被 kernel L1 规则层加载） |
| `spec.md` / `tasks.md`（工程根） | Trae 风格 UI 还原方案与任务 |
| `.trae\documents\`（工程根） | 双模式工作台、向量记忆、本地知识库三份方案 |

> 本 README 只描述**代码当前的行为**。设计与规划类内容以 `.trae\documents\` 与 `spec.md` 为准。
