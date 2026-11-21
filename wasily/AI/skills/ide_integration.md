---
{"name": "IDE集成", "triggers": ["打开文件", "编辑器打开", "ide", "vscodium", "跑终端", "打开编辑器", "编辑文件", "运行命令", "终端"], "description": "在 IDE（VS Codium）里打开/编辑/运行"}
---
用户提到 IDE / 编辑器 / vscodium 时：

## 在 IDE 里打开文件
- ide_open_file(path="绝对路径") → 在 IDE 编辑器中打开
- ide_edit_file(path="绝对路径", content="新内容") → 直接替换文件内容（会在 IDE 标签页同步刷新）

## 在 IDE 终端里跑命令
- ide_run_terminal(command="npm install") → 在 IDE 集成终端中执行
- 适合长驻服务（npm run dev、python manage.py runserver），能看到实时输出

## 普通场景
如果用户只是说"打开 xxx 文件看看"，不一定走 IDE：
- file_read 先读内容，直接在对话里给用户看更高效
- 用户明确说"在编辑器里打开"、"ide里改"时才用 ide_* 系列

## 快捷键/菜单
- 普通文件查找/编辑 → file_list + file_read + file_write 最快
- 需要用户在 IDE 里手动操作（比如按 F12、切换分支）→ ide_open_file 打开 + 文字说明怎么操作
