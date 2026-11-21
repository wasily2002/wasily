# -*- coding: utf-8 -*-
"""
用户工具模板（下划线前缀 = 不加载，仅作编写参考）
复制本文件为 xxx.py 并修改 SPEC 与 run 即可成为一个新工具。
落盘后调用 tool_reload 立即生效；tool_unload(name) 卸载。

约定：
- SPEC: dict，必填 name(唯一，勿与内置工具重名)，可选 description / parameters
- parameters: {参数名: 参数说明}，调用时以 **kwargs 传入 run()
- run(**kwargs): 返回 str（或可被 json 序列化的对象）
- 禁止在模块顶层做耗时/联网操作（会在重载时执行）；重逻辑放进 run()
"""

SPEC = {
    "name": "my_tool",
    "description": "一句话说明这个工具做什么",
    "parameters": {
        "text": "示例参数说明",
    },
}


def run(text=""):
    return f"my_tool 收到: {text}"
