---
{"name": "Blender 文生3D / 图生3D", "triggers": ["文生3D", "图生3D", "图片生成3D", "文字生成3D", "图片转3D", "照片转3D", "图片转三维", "照片转三维", "图片转换成三维", "照片转换成三维", "单图建模", "AI建模", "hyper3d", "rodin", "混元3D", "hunyuan3d", "tripo", "meshy", "polyhaven", "sketchfab", "3D资产", "下载模型", "素材库"], "description": "通过 Blender MCP 调用本地或 Hyper3D/混元等后端做文生3D、图生3D，并检索 3D 资产"}
---
文生 3D / 图生 3D 必须真实调用 `mcp__blender__*` 工具并验证输出，不能只给步骤或 Python 代码，不能假装生成成功。开工先调用 `mcp__blender__blender_status`。

## 后端选择
- `local` / `auto`：本地离线，不需要任何 Key。文生 3D 走程序化模板（房子/人物/家具）；图生 3D 走单目深度估计生成带贴图浮雕。
- `hyper3d` / `hunyuan3d`：外部服务，能出完整 360° 模型，但需在 Blender 插件面板配置 API Key。
- 用户没说后端时用 `auto`，先拿到可用结果，再说明本地与外部的能力差异。

## 检查外部后端
调用 `mcp__blender__blender_gen_status`，返回里每个后端有 `enabled` 与 `message`。
- 未启用时，告诉用户在 Blender 3D 视图按 `N` 打开侧栏 → `MCP for Blender` 面板 → 勾选对应生成器并填 API Key，然后重新连接。
- Hyper3D Rodin 还需要在面板里接受服务条款；余额不足会返回 `API_INSUFFICIENT_FUNDS`。
- 不要在未启用时反复重试，也不要编造模型文件。

## 文生 3D
1. `mcp__blender__blender_text_to_model(prompt=..., backend=auto|local|hyper3d|hunyuan3d)`。
2. 外部后端返回 `job.job_id` 后，用 `mcp__blender__blender_job_poll(job_id, backend)` 轮询，通常 30–120 秒。
3. 状态完成后 `mcp__blender__blender_job_import(job_id, name, backend)` 导入场景。
4. `mcp__blender__blender_screenshot` 验证，再按需 `mcp__blender__blender_export`。

## 图生 3D
1. 确认图片是本地可访问路径（png/jpg/webp）。
2. `mcp__blender__blender_image_to_model(image_path=..., backend=auto|local|hyper3d|hunyuan3d)`。
3. 本地浮雕：对象默认命名 `Relief`，放进 `Wasily_Relief` 集合、位于 X=8 的空处；返回 `texture`、`depth_map`、`grid`、`config`。单视角深度、背面为平面，适合照片/插画/logo 做立体效果。
4. 外部后端：轮询 → 导入 → 截图 → 导出。主体尽量居中、背景干净；多视角需求分别生成或用 `mcp__blender__blender_execute` 补建。
5. 用户要「可 360° 旋转的完整模型」时必须走外部后端并说明 Key 要求，不能把浮雕说成完整重建。

## 现成资产
- `mcp__blender__blender_asset_search(query, source=polyhaven|sketchfab|polypizza, asset_type=models|hdris|textures)`。
- `mcp__blender__blender_asset_download(asset_id, source, asset_type, name)` 直接导入。
- 下载前先说明来源与授权；PolyHaven 为 CC0，Sketchfab 需注意模型许可。

## 失败处理
- `API key` 缺失：停止并给配置步骤。
- 余额不足：说明是外部账户余额问题；可建议 `backend=local` 出浮雕，但要讲清能力差异。
- 深度估计失败：检查 `depth3d_cli.py` 是否存在、Pillow/opencv 是否可用。
- 任务超时：继续 `mcp__blender__blender_job_poll` 2–3 次，仍失败则报告 job_id 与原始错误。
- 生成结果比例异常：用 `mcp__blender__blender_execute` 读取 `dimensions`，按目标尺寸等比缩放后再截图。

## 输出
最终说明使用的后端、prompt/图片、job_id、导入对象名、截图与导出路径；失败时原样保留错误信息。
