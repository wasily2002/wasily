---
{"name": "浏览器操作", "triggers": ["打开浏览器", "看一下网页", "截图", "网页截图", "访问网站", "浏览一下", "打开网址", "网页", "浏览器", "web_snap"], "description": "截图/打开网页/浏览验证"}
---
需要操作浏览器或看网页时：

## 快速看网页
- 想看某个网页长什么样 → web_snap(url="https://...", filename="截图路径.png") 直接截图保存
- 想让浏览器真正打开 → shell_exec('start "" "https://..."') 用系统默认浏览器

## 自动化操作（如果需要点击/输入）
- 用 process_start 启动目标浏览器
- wait 几秒后用 screenshot 截当前屏幕
- 用 mouse_move + mouse_click 定位点击（先用 screenshot 看清楚屏幕布局，再坐标）
- 用 key_type 输入文本、key_press 回车/Tab

## 验证网页效果
- 用 file_write 写 HTML → shell_exec('explorer "文件完整路径"') 打开浏览器预览
- 用 screenshot 截图验证渲染效果

## 完成标记
网页已打开/截图已保存 → task_done(summary="截图路径或验证结论")
