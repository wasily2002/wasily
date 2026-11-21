---
{"name": "AI视频生成", "triggers": ["生成视频", "做个视频", "做视频", "视频生成", "AI视频", "制作视频", "渲染视频"], "description": "AI 视频生成：全参数工程化配置、多风格一键切换、本地素材导入、分片渲染断点续渲（支持10分钟以上长视频）"}
---
用户要生成视频时，按「建工程 → 加片段 → 渲染」三步走，全部用 video_* 工具完成，禁止只描述不动手。

## 技能说明
视频生成是工程化的：先 video_new 建工程（定参数），再 video_add_segment 逐个导入本地素材（图片/视频+字幕+时长），最后 video_render 分片渲染合并输出 mp4 到 agent/outputs。每个片段独立渲染并缓存，中断后重新调用 video_render 会自动跳过已完成片段（断点续渲），因此 10 分钟以上长视频可稳定产出。

## 分步使用引导
1. video_new(project, title, resolution, fps, duration, samplerate, style)
   - 所有参数可省略，省略即用默认模板（1280x720 · 30fps · 44100Hz · 电影感）
2. video_add_segment(project, material_path, text, seconds) —— 每个镜头调用一次
   - material_path: 本地图片/视频绝对路径（图片自动配缓慢推镜动画）
   - text: 该镜头底部字幕
   - seconds: 该镜头时长；长视频 = 片段数量 × 单片段时长累加
3. video_render(project) —— 分片渲染 + 合并，返回成品路径
4. video_status(project) 随时查进度；video_set(project, style=...) 渲染前一键换风格；
   video_defaults() 查看默认模板。

## 参数调优指南
- 分辨率: 720p 渲染快适合预览；1080p 成片；4K 渲染耗时约为 1080p 的 4 倍。
- 帧率: 24 电影感 / 30 通用 / 60 运动画面丝滑但渲染量翻倍。
- 风格预设: 电影感(压暗+暗角)、清新(提亮+增饱和)、复古(胶片色调+颗粒)、科技(高对比+冷色)、原片(不调色)。
- 字幕: 单条 ≤ 40 字最佳；特殊字符 ' : % 会被自动过滤。
- 长视频建议: 单片段 3~8 秒，60+ 片段串行；渲染中断后直接再次调用 video_render 即可续渲。
- 改参数/素材后需重渲：删除工程目录下 segments 文件夹再 video_render。
