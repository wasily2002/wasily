# -*- coding: utf-8 -*-
"""一键部署技能：根据一句话需求生成静态站点并立即部署到本机端口。

流程：解析标题 → 生成单文件网站 → 写入 deploy_sites/<id>/index.html
     → 调 sites_net.deploy 挂端口 → 返回部署信息（供对话里展示）。
"""
import os
import re
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(BASE_DIR, "deploy_sites")


def _site_name(prompt):
    """从「我要的是XXX网站」这类句子里提取站点名。"""
    p = (prompt or "").strip()
    for pat in (r"我要的是(.+?)(?:网站|官网|站点)",
                r"做一个(.+?)(?:网站|官网|站点)",
                r"创建(?:一个)?(.+?)(?:网站|官网|站点)"):
        m = re.search(pat, p)
        if m:
            name = m.group(1).strip(" ，,。.的")
            name = re.sub(r"^(?:一个|个)", "", name).strip(" ，,。.的")
            if name and name not in ("网站", "官网", "站点"):
                return name if len(name) <= 20 else name[:20]
    return "我的网站"


def _build_html(name):
    """生成一个完整、可用的响应式企业站（单文件，无外部依赖）。"""
    safe = (name or "我的网站").replace("<", "&lt;")
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>%s</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--red:#c62828;--red2:#e53935;--ink:#1b1b1b;--gray:#666;--bg:#f7f7f5}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;color:var(--ink);background:#fff;line-height:1.7}
a{text-decoration:none;color:inherit}
.nav{position:sticky;top:0;z-index:9;background:rgba(255,255,255,.94);backdrop-filter:blur(8px);border-bottom:1px solid #eee}
.nav .in{max-width:1120px;margin:0 auto;height:64px;display:flex;align-items:center;gap:28px;padding:0 24px}
.logo{display:flex;align-items:center;gap:9px;font-weight:700;font-size:17px}
.logo i{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,var(--red),var(--red2));display:block}
.nav nav{display:flex;gap:22px;font-size:14px;color:#444;margin-left:14px}
.nav nav a:hover{color:var(--red)}
.nav .tel{margin-left:auto;font-size:13px;color:var(--red);font-weight:600}
.hero{background:linear-gradient(120deg,#8e0000,#c62828 55%%,#e53935);color:#fff;padding:84px 24px 96px}
.hero .in{max-width:1120px;margin:0 auto;display:grid;grid-template-columns:1.1fr .9fr;gap:48px;align-items:center}
.hero h1{font-size:42px;line-height:1.3;letter-spacing:.5px}
.hero p{margin-top:16px;font-size:15px;color:rgba(255,255,255,.86);max-width:520px}
.hero .btns{margin-top:28px;display:flex;gap:12px}
.btn{display:inline-flex;align-items:center;justify-content:center;height:42px;padding:0 22px;border-radius:8px;font-size:14px;cursor:pointer;border:0}
.btn.main{background:#fff;color:var(--red);font-weight:600}
.btn.ghost{background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.4)}
.hero .card{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.22);border-radius:14px;padding:22px;backdrop-filter:blur(6px)}
.hero .card h4{font-size:14px;font-weight:600;margin-bottom:14px;color:#fff}
.hero .card .row{display:flex;justify-content:space-between;font-size:13px;padding:9px 0;border-bottom:1px dashed rgba(255,255,255,.2);color:rgba(255,255,255,.85)}
.hero .card .row:last-child{border-bottom:0}
.stats{max-width:1120px;margin:-42px auto 0;background:#fff;border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.09);display:grid;grid-template-columns:repeat(4,1fr);padding:26px 10px;position:relative;z-index:2}
.stats div{text-align:center}
.stats b{display:block;font-size:28px;color:var(--red);font-weight:700}
.stats span{font-size:12.5px;color:var(--gray)}
section{max-width:1120px;margin:0 auto;padding:72px 24px}
.sec-h{text-align:center;margin-bottom:36px}
.sec-h em{font-style:normal;font-size:12px;letter-spacing:2px;color:var(--red);font-weight:600}
.sec-h h2{font-size:27px;margin-top:8px}
.sec-h p{color:var(--gray);font-size:14px;margin-top:8px}
.grid{display:grid;gap:18px}
.g3{grid-template-columns:repeat(3,1fr)}
.g6{grid-template-columns:repeat(3,1fr)}
.item{border:1px solid #ececea;border-radius:12px;padding:22px;background:#fff;transition:.2s}
.item:hover{transform:translateY(-3px);box-shadow:0 14px 34px rgba(0,0,0,.08);border-color:#e0d7d7}
.item i{display:flex;width:38px;height:38px;border-radius:9px;background:#fdecec;color:var(--red);align-items:center;justify-content:center;font-style:normal;font-size:17px;margin-bottom:14px}
.item b{display:block;font-size:15px;margin-bottom:6px}
.item p{font-size:13px;color:var(--gray)}
.news{display:grid;grid-template-columns:1.2fr .8fr;gap:22px}
.news .main{border:1px solid #ececea;border-radius:12px;overflow:hidden}
.news .main .pic{height:210px;background:linear-gradient(120deg,#b71c1c,#ef5350)}
.news .main .txt{padding:18px 20px}
.news .main b{font-size:15px}
.news .main p{font-size:13px;color:var(--gray);margin-top:6px}
.news ul{list-style:none}
.news li{border-bottom:1px dashed #e6e6e3;padding:13px 0;font-size:13.5px;display:flex;justify-content:space-between;color:#444}
.news li span{color:#aaa;font-size:12px}
.contact{background:var(--bg);border-radius:16px;padding:40px;display:grid;grid-template-columns:1fr 1fr;gap:34px;max-width:1120px;margin:0 auto 72px}
.contact h2{font-size:24px;margin-bottom:14px}
.contact p{font-size:13.5px;color:var(--gray);margin-bottom:6px}
.contact .f{display:grid;gap:10px}
.contact input,.contact textarea{width:100%%;border:1px solid #ddd;border-radius:8px;padding:10px 12px;font-size:13.5px;font-family:inherit;outline:none}
.contact textarea{height:88px;resize:vertical}
.contact button{height:40px;border:0;border-radius:8px;background:var(--red);color:#fff;font-size:14px;cursor:pointer}
footer{background:#1d1d1d;color:#9a9a9a;font-size:12.5px;text-align:center;padding:26px 20px}
@media(max-width:900px){
  .hero .in{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}
  .g3,.g6{grid-template-columns:1fr}.news{grid-template-columns:1fr}
  .contact{grid-template-columns:1fr;margin:0 16px 50px}.hero h1{font-size:32px}
  .nav nav{display:none}
}
</style>
</head>
<body>
<div class="nav"><div class="in">
  <div class="logo"><i></i>%s</div>
  <nav><a href="#about">关于我们</a><a href="#biz">业务板块</a><a href="#news">新闻动态</a><a href="#contact">联系我们</a></nav>
  <div class="tel">服务热线 400-000-0000</div>
</div></div>

<div class="hero"><div class="in">
  <div>
    <h1>%s<br>让每一项服务都清晰可见</h1>
    <p>面向产业与用户的数字化服务平台，覆盖业务办理、资源对接、资讯与售后支持，用一套系统连接线上与线下。</p>
    <div class="btns"><button class="btn main">立即咨询</button><button class="btn ghost">了解业务</button></div>
  </div>
  <div class="card">
    <h4>服务概览</h4>
    <div class="row"><span>在线业务</span><span>12 类</span></div>
    <div class="row"><span>服务网点</span><span>268 个</span></div>
    <div class="row"><span>年度服务人次</span><span>120 万+</span></div>
    <div class="row"><span>响应时效</span><span>&lt; 2 小时</span></div>
  </div>
</div></div>

<div class="stats">
  <div><b>12</b><span>业务板块</span></div>
  <div><b>268</b><span>线下网点</span></div>
  <div><b>120万+</b><span>累计服务</span></div>
  <div><b>99.9%%</b><span>服务可用性</span></div>
</div>

<section id="about">
  <div class="sec-h"><em>ABOUT US</em><h2>关于我们</h2><p>以数字化能力重构服务流程，让用户少跑腿、数据多跑路。</p></div>
  <div class="grid g3">
    <div class="item"><i>◈</i><b>我们的定位</b><p>连接资源与需求的综合服务平台，提供从咨询到交付的一站式支持。</p></div>
    <div class="item"><i>◎</i><b>我们的使命</b><p>让优质服务触手可及，用透明流程与稳定系统建立长期信任。</p></div>
    <div class="item"><i>◇</i><b>我们的能力</b><p>线上业务系统、线下服务网络与专业团队协同，覆盖全流程。</p></div>
  </div>
</section>

<section id="biz">
  <div class="sec-h"><em>BUSINESS</em><h2>业务板块</h2><p>六大业务方向，覆盖日常所需与产业服务。</p></div>
  <div class="grid g6">
    <div class="item"><i>01</i><b>业务办理</b><p>在线提交、进度查询与结果通知，全流程可追溯。</p></div>
    <div class="item"><i>02</i><b>资源对接</b><p>供需信息发布与撮合，缩短沟通链路。</p></div>
    <div class="item"><i>03</i><b>电商服务</b><p>商品展示、下单与售后，支持多渠道触达。</p></div>
    <div class="item"><i>04</i><b>综合服务</b><p>生活缴费、便民服务与社区支持一体化。</p></div>
    <div class="item"><i>05</i><b>再生资源</b><p>回收网络与循环利用，推动绿色低碳。</p></div>
    <div class="item"><i>06</i><b>企业合作</b><p>开放接口与联合运营，共建服务生态。</p></div>
  </div>
</section>

<section id="news">
  <div class="sec-h"><em>NEWS</em><h2>新闻动态</h2><p>了解平台最新进展与行业资讯。</p></div>
  <div class="news">
    <div class="main"><div class="pic"></div><div class="txt">
      <b>平台服务能力升级，线上业务覆盖更多场景</b>
      <p>本次升级优化了办理流程与消息通知，平均处理时长缩短 38%%。</p></div></div>
    <ul>
      <li>新服务网点上线运营<span>11-18</span></li>
      <li>数字化服务标准发布<span>11-12</span></li>
      <li>年度合作伙伴大会顺利举办<span>11-05</span></li>
      <li>绿色回收专项行动启动<span>10-28</span></li>
    </ul>
  </div>
</section>

<div class="contact" id="contact">
  <div>
    <h2>联系我们</h2>
    <p>服务热线：400-000-0000</p>
    <p>工作时间：周一至周日 9:00 - 18:00</p>
    <p>地址：本地服务中心（示例地址）</p>
  </div>
  <div class="f">
    <input placeholder="您的称呼">
    <input placeholder="联系电话">
    <textarea placeholder="请描述您的需求"></textarea>
    <button>提交需求</button>
  </div>
</div>

<footer>© 2026 %s · 由 wasily 一键部署生成</footer>
</body>
</html>""" % (safe, safe, safe, safe)


def one_click(prompt):
    """生成并部署，返回部署信息。"""
    import sites_net

    name = _site_name(prompt)
    sid = "dpl-" + time.strftime("%y%m%d%H%M%S") + uuid.uuid4().hex[:4]
    folder = os.path.join(OUT_ROOT, sid)
    os.makedirs(folder, exist_ok=True)
    index = os.path.join(folder, "index.html")
    with open(index, "w", encoding="utf-8") as f:
        f.write(_build_html(name))
    site = sites_net.deploy(name, folder, "private")
    return {
        "site": site,
        "site_name": name,
        "deployment_id": "dap_" + sid,
        "deploy_type": "StaticWeb（静态网站）",
        "path": folder,
        "files": [{"name": "index.html", "added": 218, "deleted": 0}],
        "summary": [
            "首页 Hero —— 品牌视觉 + 核心数据看板",
            "关于我们 —— 定位、使命与能力三张卡片",
            "业务板块 —— 六大业务方向",
            "新闻动态 —— 最新资讯列表",
            "联系我们 —— 联系方式 + 在线留言表单",
            "响应式设计 —— 适配桌面端、平板与移动端",
        ],
    }
