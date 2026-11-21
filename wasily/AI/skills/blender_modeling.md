---
{"name": "Blender 程序化建模", "triggers": ["建模", "三维", "建个房子", "房屋", "房子", "房间", "家具", "桌子", "椅子", "人物模型", "人物", "行走", "走路", "骨骼", "渲染", "导出模型", "GLB", "FBX"], "description": "用 Blender MCP 生成房屋/家具/人物，带骨骼行走与截图验证"}
---
在 Blender 里建模时，必须真实调用 `mcp__blender__*` 工具，不要只给 Python 代码让用户自己跑。

## 开工前
1. `mcp__blender__blender_status` 确认在线；失败时提示用户先启动 Blender（C:\Blender\app\blender-4.2.16-windows-x64\blender.exe），插件会自动监听 9876。
2. `mcp__blender__blender_scene_info` 看现有对象，避免覆盖用户场景。

## 工具选择
- 输入包含图片/照片并要求转三维、生成 3D、补全角度时，走「Blender 文生3D / 图生3D」技能；禁止用 `mcp__blender__blender_build_character` 的低模人代替照片人物。
- 房屋：`mcp__blender__blender_build_house(width, depth, height)`，生成墙体、门洞、窗户、四坡屋顶，放在 `Wasily_House` 集合。
- 家具：`mcp__blender__blender_build_furniture(kind=table|chair|block)`，放在 `Wasily_Furniture` 集合。
- 人物 + 行走：`mcp__blender__blender_build_character(walk=true)`，生成 7 根骨骼（root/spine/head/双臂/双腿）和 1–30 帧行走循环，放在 `Wasily_Character` 集合。
- 复杂形状：`mcp__blender__blender_execute(code=...)` 写 bpy 脚本。优先低多边形 + 材质颜色，单个对象面数控制在 5 万以内。

## 标准闭环
1. 生成 → `mcp__blender__blender_scene_info` 确认对象数量与名称。
2. `mcp__blender__blender_screenshot` 截图，检查比例、门窗朝向、人物是否穿模；必要时用 `mcp__blender__blender_execute` 调整位置/尺寸。
3. 多轮修改只改自己的 `Wasily_*` 集合，禁止执行 `bpy.data.objects.remove` 清空全场景。
4. 交付文件用 `mcp__blender__blender_export(path, fmt=GLB)`，默认输出到 `wasily/AI/outputs/blender`。
5. 正式渲染输出尽量用纯英文目录，避免个别 bpy 版本对中文路径处理异常。
6. 最终回复说明：生成了什么、对象/骨骼数量、截图路径、导出路径。

## 人物行走
- 生成后动画会自动播放；用 `mcp__blender__blender_execute` 设置 `bpy.context.scene.frame_set(15)` 可看中帧。
- 调节步幅：修改 `pose["leg.L"]` / `pose["leg.R"]` 的 `rotation_euler` 关键帧角度（默认 30°）。
- 调节速度：改 `scene.frame_end` 或关键帧间隔；导出 GLB 时动画会一并写入。
- 角色默认放在 (2.6, -4.6)，避免和房屋重叠；需要移动时改 `bpy.data.objects["WasilyRig"].location`。

## Blender 4.2 兼容要点
- 圆锥用 `radius1=`，不是 `radius=`。
- 布尔差集要先设 `bpy.context.view_layer.objects.active = 墙体对象` 再 `modifier_apply`。
- `primitive_cube_add(size=1)` 后 `ob.scale` 即实际尺寸。
- 视口截图需要 GUI；纯后台模式只能渲染，不能 capture viewport。

## 禁止
- 不要删除用户已有对象、相机、灯光。
- 不要在没截图验证时声称「模型已完成」。
- 不要把整段 bpy 代码当最终答案输出而不执行。
