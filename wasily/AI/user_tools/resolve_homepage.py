# -*- coding: utf-8 -*-
"""只读解析系统默认浏览器与主页配置（不打开、不修改任何系统设置）。"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _homepage_probe  # noqa: E402

SPEC = {
    "name": "resolve_homepage",
    "description": "只读解析当前系统默认浏览器与其主页配置，返回 homepage_kind=url/newtab/none；不打开网页",
    "parameters": {},
}


def run():
    return _homepage_probe.resolve()
