---
{"name": "系统服务", "triggers": ["php", "npm", "node", "启动服务", "起个服务", "本地服务", "数据库服务"], "description": "管理本地开发服务（php/npm/python 静态服务等）"}
---
用户涉及本地开发服务时：
1. 优先调用已安装的服务类工具（php_server / npm_runner / python_server，如未安装先告知用户可在插件市场一键安装，或用 shell_exec 直接执行对应命令）。
2. 启动服务用后台方式（process_start 或 shell_exec 加 start /b），不要阻塞对话。
3. 启动后用 network_request 访问服务地址验证可用，并把访问地址告诉用户。
4. 端口被占用时换端口重试，最多 3 次。
