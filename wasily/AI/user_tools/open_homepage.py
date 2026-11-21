# -*- coding: utf-8 -*-
"""打开系统默认浏览器并导航到检测到的主页（不猜网址）。"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _homepage_probe  # noqa: E402

SPEC = {
    "name": "open_homepage",
    "description": "打开系统默认浏览器并导航到真实检测到的系统主页；用户已给具体网址时可传 url 直接打开（未配置主页时如实失败，不编造网址）",
    "parameters": {
        "url": "可选。用户已明确给出网址时传入该网址；留空则打开系统检测到的主页",
    },
}


def run(url=""):
    return _homepage_probe.open_page(url)
