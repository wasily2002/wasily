# -*- coding: utf-8 -*-
"""示例用户工具：获取当前时间（验证动态加载链路）"""

SPEC = {
    "name": "time_now",
    "description": "获取当前日期、时间和星期",
    "parameters": {},
}


def run():
    import datetime
    d = datetime.datetime.now()
    week = "一二三四五六日"[d.weekday()]
    return d.strftime("%Y-%m-%d %H:%M:%S") + f" 星期{week}"
