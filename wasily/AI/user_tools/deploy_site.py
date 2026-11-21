SPEC = {"name": "deploy_site",
        "description": "一键部署：根据一句话需求生成网站并部署到本机，返回可访问地址",
        "parameters": {"prompt": "网站需求描述，例如：帮我创建一个咖啡店官网并部署"}}


def run(prompt=""):
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import deploy_skill
    r = deploy_skill.one_click(prompt or "帮我创建一个网站并部署。")
    s = r["site"]
    return ("部署成功\n访问地址: %s\nSite ID: %s\nDeployment ID: %s\n源码目录: %s"
            % (s.get("url_lan") or s.get("url_local"), s.get("id"),
               r.get("deployment_id"), r.get("path")))
