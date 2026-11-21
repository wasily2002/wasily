# -*- coding: utf-8 -*-
"""Blender MCP 服务（直连 Blender 插件 Socket）。

不依赖 PyPI 上的 blender-mcp 包：本服务通过 JSON Socket 与 Blender 内的
blender_mcp_addon 通信（默认 localhost:9876），协议为
{"type": "<命令>", "params": {...}} → {"status": "success"|"error", ...}。

这样做的原因：
1. uvx 首次启动要解析并安装依赖，会拖慢甚至阻塞后端启动；
2. 直接对话 Socket 后，命令集可按需裁剪，工具说明也能写成中文；
3. 后续换 Blender 版本或换插件实现，只需改 _send_cmd。

建模能力分三层：
- 原子工具：场景信息、对象信息、截图、执行 Python、导出
- 程序化建模：房屋/房间/家具/人物 + 行走动画模板，纯 bpy 生成，离线可用
- 外部生成：文生 3D / 图生 3D，走插件里的 Hyper3D、混元 3D、Tripo、Meshy 等
  （需在 Blender 插件面板填对应 API Key，未配置时明确报错而不是静默失败）
"""
import json
import os
import socket
import sys
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROTOCOL = "2024-11-05"
SERVER = {"name": "wasily-blender", "version": "1.0.0"}
HOST = os.environ.get("BLENDER_HOST", "127.0.0.1")
PORT = int(os.environ.get("BLENDER_PORT", "9876") or 9876)
TIMEOUT = float(os.environ.get("BLENDER_TIMEOUT", "60") or 60)
TRIPOSR_ROOT = os.path.join(BASE_DIR, "vendor", "TripoSR")
TRIPOSR_CLI = os.path.join(BASE_DIR, "triposr_cli.py")
TRIPOSR_PYTHON = os.path.join(BASE_DIR, "triposr_venv", "Scripts", "python.exe")
TRIPOSR_MODEL_DIR = os.environ.get("TRIPOSR_MODEL_DIR", r"C:\wasily_models\TripoSR")


class BlenderError(RuntimeError):
    pass


def _send_cmd(cmd_type, params=None, timeout=None):
    """向 Blender 插件发一条命令并读回完整 JSON 响应。"""
    payload = json.dumps({"type": cmd_type, "params": params or {}}).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout or TIMEOUT)
    try:
        sock.connect((HOST, PORT))
    except OSError as exc:
        raise BlenderError(
            "连不上 Blender 插件（%s:%s）：%s。请确认 Blender 已启动，"
            "并在插件面板点了 Connect / 已开启自动监听。" % (HOST, PORT, exc))
    try:
        sock.sendall(payload)
        chunks = []
        while True:
            buf = sock.recv(65536)
            if not buf:
                break
            chunks.append(buf)
            try:
                data = json.loads(b"".join(chunks).decode("utf-8", "ignore"))
                return data
            except ValueError:
                continue
    finally:
        try:
            sock.close()
        except OSError:
            pass
    raw = b"".join(chunks).decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except ValueError:
        raise BlenderError("Blender 返回了无法解析的内容：%s" % raw[:200])


def _ok(cmd_type, params=None, timeout=None):
    """执行命令并把插件返回归一化成 (数据, 错误)。"""
    res = _send_cmd(cmd_type, params, timeout)
    status = str((res or {}).get("status") or "").lower()
    if status == "error":
        raise BlenderError((res or {}).get("message") or "Blender 执行失败")
    return (res or {}).get("result")


# ---------------- 原子工具 ----------------
def blender_status():
    """探测 Blender 是否在线，返回插件与场景概况。"""
    try:
        pong = _ok("ping", timeout=5)
        info = {}
        try:
            info = _ok("get_addon_info", timeout=8) or {}
        except BlenderError:
            pass
        scene = _ok("get_scene_info", timeout=10) or {}
        return {"ok": True, "host": HOST, "port": PORT, "pong": pong,
                "addon": info, "scene": scene}
    except BlenderError as exc:
        return {"ok": False, "host": HOST, "port": PORT, "error": str(exc),
                "hint": "启动 Blender 后，在 3D 视图侧栏 BlenderMCP 面板点 Connect"}


def blender_scene_info():
    """读取当前场景：对象列表、选中对象、渲染设置。"""
    return {"ok": True, "scene": _ok("get_scene_info")}


def blender_object_info(name):
    """读取单个对象的变换、尺寸、材质与父子关系。"""
    if not str(name or "").strip():
        raise ValueError("name 不能为空")
    return {"ok": True, "object": _ok("get_object_info", {"name": name})}


def blender_screenshot(max_size=1200, path=""):
    """截取 3D 视口，返回 base64 PNG 并同时落盘。

    插件要求必须给 filepath 才会截（否则直接返回 error），所以这里先定好路径。
    """
    import base64
    import time as _time
    if not path:
        out_dir = os.path.join(BASE_DIR, "outputs", "blender")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "viewport_%d.png" % int(_time.time()))
    res = _ok("get_viewport_screenshot",
              {"max_size": int(max_size or 1200), "filepath": path, "format": "png"},
              timeout=60) or {}
    if res.get("error"):
        return {"ok": False, "error": str(res.get("error")), "path": path}
    file_path = res.get("filepath") or path
    b64 = ""
    try:
        with open(file_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
    except OSError as exc:
        return {"ok": False, "error": "截图已生成但读取失败：%s" % exc,
                "path": file_path, "raw": res}
    return {"ok": True, "path": file_path, "method": res.get("method", ""),
            "bytes": len(b64) * 3 // 4, "image_base64": b64}


def blender_execute(code, timeout=120):
    """在 Blender 内执行 Python（bpy）。语法错误与运行异常都会原文回传。

    这是最通用的能力入口：房屋、物品、人物、动画都可以用它完成。
    """
    code = str(code or "")
    if not code.strip():
        raise ValueError("code 不能为空")
    try:
        compile(code, "<blender>", "exec")
    except SyntaxError as exc:
        return {"ok": False, "error": "语法错误：%s（第 %s 行）" % (exc.msg, exc.lineno)}
    try:
        result = _ok("execute_code", {"code": code}, timeout=timeout)
        return {"ok": True, "result": result}
    except BlenderError as exc:
        return {"ok": False, "error": str(exc),
                "hint": "按报错修改变量名/API 后重试，Blender 进程不会崩溃"}


def blender_export(path="", fmt="GLB"):
    """导出当前场景为 GLB/FBX，返回实际写入路径。"""
    fmt = str(fmt or "GLB").lower()
    if fmt not in ("glb", "fbx"):
        raise ValueError("fmt 只支持 GLB / FBX")
    if not path:
        out_dir = os.path.join(BASE_DIR, "outputs", "blender")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "scene." + fmt)
    # 直接走 bpy 导出算子，避开插件 export_scene 的参数差异
    if fmt == "glb":
        code = ("import bpy\n"
                "bpy.ops.export_scene.gltf(filepath=%r, export_format='GLB', "
                "export_animations=True, export_apply=True)\n"
                "print('exported glb')\n" % path)
    else:
        code = ("import bpy\n"
                "bpy.ops.export_scene.fbx(filepath=%r, use_selection=False)\n"
                "print('exported fbx')\n" % path)
    res = blender_execute(code, timeout=240)
    exists = os.path.exists(path)
    return {"ok": bool(exists), "path": path,
            "bytes": os.path.getsize(path) if exists else 0,
            "result": res, "error": "" if exists else "导出命令已执行但文件不存在"}


# ---------------- 本地免费图生 3D：TripoSR ----------------
def _triposr_status():
    """只做文件与模块探测，不加载权重，避免状态查询占用数 GB 内存。"""
    import importlib.util
    modules = ("torch", "numpy", "PIL", "trimesh", "skimage", "omegaconf",
               "transformers", "einops", "imageio", "rembg")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    files = [TRIPOSR_CLI, TRIPOSR_PYTHON,
             os.path.join(TRIPOSR_ROOT, "tsr", "system.py"),
             os.path.join(TRIPOSR_MODEL_DIR, "config.yaml"),
             os.path.join(TRIPOSR_MODEL_DIR, "model.ckpt")]
    missing_files = [path for path in files if not os.path.isfile(path)]
    available = not missing and not missing_files
    return {"available": available, "mode": "local_cpu", "cost": "free",
            "model_dir": TRIPOSR_MODEL_DIR, "missing_modules": missing,
            "missing_files": missing_files,
            "hint": "" if available else "安装缺失依赖并下载 stabilityai/TripoSR 权重后可用"}


# ---------------- 外部生成（文生 3D / 图生 3D）----------------
def blender_gen_status():
    """查看本地 TripoSR 与各外部 3D 生成后端是否可用。"""
    out = {"ok": True, "backends": {}}
    for key, cmd in (("hyper3d", "get_hyper3d_status"),
                     ("hunyuan3d", "get_hunyuan3d_status"),
                     ("tripo", "get_tripo_status"),
                     ("meshy", "get_meshy_status"),
                     ("sketchfab", "get_sketchfab_status"),
                     ("polyhaven", "get_polyhaven_status")):
        try:
            out["backends"][key] = _ok(cmd, timeout=8)
        except BlenderError as exc:
            out["backends"][key] = {"available": False, "error": str(exc)}
    out["backends"]["triposr"] = _triposr_status()
    return out


def blender_text_to_model(prompt, backend="auto"):
    """用文字生成 3D 模型并导入场景。

    auto/local 走离线模板（房子、人物、家具），无需 Key；
    hyper3d / hunyuan3d 走外部生成，需要先在 Blender 插件面板配置 API Key。
    """
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    backend = str(backend or "auto").lower()
    if backend in ("auto", "local"):
        p = prompt.lower()
        if any(k in p for k in ("房", "house", "别墅", "小屋", "villa", "building")):
            return {"ok": True, "backend": "local", "template": "house",
                    "result": blender_build_house()}
        if any(k in p for k in ("人", "角色", "character", "person", "人物", "士兵", "女孩", "男孩")):
            return {"ok": True, "backend": "local", "template": "character_walk",
                    "result": blender_build_character(True)}
        if any(k in p for k in ("桌", "table", "椅", "chair", "家具", "furniture", "床", "柜")):
            kind = "chair" if ("椅" in p or "chair" in p) else "table"
            return {"ok": True, "backend": "local", "template": "furniture_" + kind,
                    "result": blender_build_furniture(kind)}
        raise ValueError("本地模板只覆盖 房子 / 人物 / 家具；任意文字生成请显式传 "
                         "backend=hyper3d 并配置 API Key（Hyper3D / Meshy / Tripo）")
    if backend == "hyper3d":
        job = _ok("create_rodin_job", {"text_prompt": prompt}, timeout=60)
        return {"ok": True, "backend": backend, "job": job,
                "next": "用 blender_job_poll 查状态，完成后用 blender_job_import 导入"}
    if backend == "hunyuan3d":
        job = _ok("create_hunyuan_job", {"text_prompt": prompt}, timeout=60)
        return {"ok": True, "backend": backend, "job": job,
                "next": "用 blender_job_poll 查状态，完成后用 blender_job_import 导入"}
    raise ValueError("不支持的后端：%s（可用 auto/local/hyper3d/hunyuan3d）" % backend)


_RELIEF_BLENDER = r"""
import bpy, json, math

with open({cfg!r}, "r", encoding="utf-8") as _fh:
    cfg = json.load(_fh)
name = cfg["name"]
coll_name = "Wasily_Relief"
coll = bpy.data.collections.get(coll_name)
if coll is None:
    coll = bpy.data.collections.new(coll_name)
if coll.name not in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.link(coll)
for ob in list(coll.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

verts = [tuple(v) for v in cfg["verts"]]
faces = [tuple(f) for f in cfg["faces"]]
uvs = cfg["uvs"]

me = bpy.data.meshes.new(name + "Mesh")
me.from_pydata(verts, [], faces)
me.validate()
me.update()
uvl = me.uv_layers.new(name="UVMap")
for poly in me.polygons:
    for li in poly.loop_indices:
        vi = me.loops[li].vertex_index
        uvl.data[li].uv = (uvs[vi][0], uvs[vi][1])
me.polygons.foreach_set("use_smooth", [1] * len(me.polygons))
me.update()

ob = bpy.data.objects.new(name, me)
coll.objects.link(ob)
ox = float(cfg.get("loc_x", 0.0))
ob.location = (ox, 0, 0)

img = bpy.data.images.load(cfg["texture"], check_existing=True)
mat = bpy.data.materials.get(name + "_Mat") or bpy.data.materials.new(name + "_Mat")
mat.use_nodes = True
nt = mat.node_tree
for n in list(nt.nodes):
    nt.nodes.remove(n)
out = nt.nodes.new("ShaderNodeOutputMaterial")
bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
tex = nt.nodes.new("ShaderNodeTexImage")
tex.image = img
nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
bsdf.inputs["Roughness"].default_value = 0.6
me.materials.append(mat)

sx, sz = cfg["size_x"], cfg["size_z"]
cam_ob = bpy.data.objects.get("Wasily_ReliefCam")
if cam_ob is None:
    cam_data = bpy.data.cameras.new("Wasily_ReliefCam")
    cam_ob = bpy.data.objects.new("Wasily_ReliefCam", cam_data)
    coll.objects.link(cam_ob)
cam_ob.data.lens = 50
dist = max(sx, sz) * 1.6
cam_ob.location = (ox, -dist, sz / 2)
cam_ob.rotation_euler = (math.radians(90), 0, 0)
bpy.context.scene.camera = cam_ob

light = bpy.data.objects.get("Wasily_ReliefLight")
if light is None:
    ld = bpy.data.lights.new("Wasily_ReliefLight", 'AREA')
    light = bpy.data.objects.new("Wasily_ReliefLight", ld)
    light.rotation_euler = (math.radians(55), 0, 0)
    coll.objects.link(light)
light.data.energy = 1200
light.data.size = max(sx, sz) * 1.2
light.location = (ox + sx * 0.3, -dist * 0.5, sz * 1.1)

fill = bpy.data.objects.get("Wasily_ReliefFill")
if fill is None:
    fd = bpy.data.lights.new("Wasily_ReliefFill", 'AREA')
    fill = bpy.data.objects.new("Wasily_ReliefFill", fd)
    fill.rotation_euler = (math.radians(75), 0, math.radians(150))
    coll.objects.link(fill)
fill.data.energy = 400
fill.data.size = max(sx, sz)
fill.location = (ox - sx * 0.9, -dist * 0.45, sz * 0.7)

bpy.context.view_layer.objects.active = ob
ob.select_set(True)
print("relief built:", name, len(verts), "verts")
"""


def _local_image_relief(image_path, name="Relief", depth_ratio=0.18):
    """本地单目深度 → 带贴图的浮雕网格（离线，不需要任何 API Key）。

    局限：只有一个视角的深度，背面是平面；适合照片/插画/logo 做立体浮雕，
    不等同于 TripoSR 的 360° 重建。需要完整模型请用 backend=triposr。
    """
    import base64
    import re
    import subprocess
    import time as _time
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("本地浮雕需要 Pillow（pip install pillow）")

    cli = os.path.join(BASE_DIR, "depth3d_cli.py")
    if not os.path.isfile(cli):
        raise RuntimeError("找不到 depth3d_cli.py，无法做本地深度估计")

    out_dir = os.path.join(BASE_DIR, "outputs", "blender")
    os.makedirs(out_dir, exist_ok=True)
    stamp = int(_time.time())
    safe = re.sub(r"[^\w\-]+", "_", str(name or "relief"))[:40] or "relief"

    src = Image.open(image_path).convert("RGB")
    sw, sh = src.size
    longest = 96
    if sw >= sh:
        gw, gh = longest, max(12, int(round(longest * sh / float(sw))))
    else:
        gh, gw = longest, max(12, int(round(longest * sw / float(sh))))

    # 深度模型在子进程里跑，避免把 torch 权重塞进 MCP 服务进程
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", cli, "single", image_path,
         "--out_w", str(gw), "--out_h", str(gh)],
        capture_output=True, text=True, timeout=300, cwd=BASE_DIR)
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if proc.returncode != 0 or not payload or not payload.get("ok"):
        raise RuntimeError("深度估计失败：%s" % ((proc.stderr or proc.stdout or "")[-400:]))

    depth_png = os.path.join(out_dir, "relief_%s_%d_depth.png" % (safe, stamp))
    with open(depth_png, "wb") as fh:
        fh.write(base64.b64decode(payload["depth_png"]))
    tex_png = os.path.join(out_dir, "relief_%s_%d_tex.png" % (safe, stamp))
    # 贴图按长边 512 保留细节，网格仍用低分辨率深度，贴图不会跟着变糊
    if sw >= sh:
        tw, th = 512, max(16, int(round(512 * sh / float(sw))))
    else:
        tw, th = max(16, int(round(512 * sw / float(sh)))), 512
    src.resize((tw, th), Image.LANCZOS).save(tex_png)

    dep = Image.open(depth_png).convert("L")
    if dep.size != (gw, gh):
        dep = dep.resize((gw, gh))
    px = list(dep.getdata())

    size_z = 1.6
    size_x = size_z * gw / float(gh)
    max_dep = max(size_x, size_z) * float(depth_ratio)
    verts, uvs = [], []
    for j in range(gh):
        vz = size_z * (1.0 - j / float(gh - 1)) if gh > 1 else size_z * 0.5
        tv = 1.0 - j / float(gh - 1) if gh > 1 else 0.5
        for i in range(gw):
            vx = size_x * (i / float(gw - 1) - 0.5) if gw > 1 else 0.0
            tu = i / float(gw - 1) if gw > 1 else 0.5
            vy = -max_dep * (px[j * gw + i] / 255.0)
            verts.append([round(vx, 5), round(vy, 5), round(vz, 5)])
            uvs.append([round(tu, 5), round(tv, 5)])
    faces = []
    for j in range(gh - 1):
        for i in range(gw - 1):
            a = j * gw + i
            faces.append([a, a + gw, a + gw + 1, a + 1])

    cfg_path = os.path.join(out_dir, "relief_%s_%d.json" % (safe, stamp))
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"name": str(name or "Relief"), "verts": verts, "faces": faces,
                   "uvs": uvs, "texture": tex_png, "loc_x": 8.0,
                   "size_x": round(size_x, 5), "size_z": round(size_z, 5)}, fh)

    res = blender_execute(_RELIEF_BLENDER.format(cfg=cfg_path), timeout=180)
    return {"ok": bool(res.get("ok")), "backend": "local", "mode": "relief",
            "object": str(name or "Relief"), "grid": [gw, gh],
            "texture": tex_png, "depth_map": depth_png, "config": cfg_path,
            "result": res,
            "hint": "本地浮雕只有单视角深度、背面为平面；要完整 360° 模型请用 backend=triposr"}


def _triposr_image_model(image_path, name="TripoSR", resolution=256):
    """子进程完成本地重建，再把带顶点颜色的 GLB 导入 Blender。"""
    import re
    import subprocess
    import time as _time
    status = _triposr_status()
    if not status["available"]:
        raise BlenderError("TripoSR 不可用：%s；%s" % (
            status["missing_modules"] + status["missing_files"], status["hint"]))
    resolution = int(resolution or 256)
    if not 32 <= resolution <= 512:
        raise ValueError("resolution 应在 32 到 512 之间")
    out_dir = os.path.join(BASE_DIR, "outputs", "blender")
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", str(name or "TripoSR"))[:40] or "TripoSR"
    out_path = os.path.join(out_dir, "triposr_%s_%d.glb" % (safe, int(_time.time())))
    env = os.environ.copy()
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            [TRIPOSR_PYTHON, "-X", "utf8", TRIPOSR_CLI, image_path,
             "--model-dir", TRIPOSR_MODEL_DIR, "--output", out_path,
             "--resolution", str(resolution)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=7200, cwd=BASE_DIR, env=env)
    except subprocess.TimeoutExpired as exc:
        raise BlenderError("TripoSR 子进程超过 7200 秒仍未完成：%s" % exc)
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                payload = json.loads(line.strip())
                break
            except ValueError:
                continue
    if proc.returncode != 0 or not payload or not payload.get("ok"):
        detail = str((payload or {}).get("error")
                     or (proc.stderr or proc.stdout or ""))[:1600]
        raise BlenderError("TripoSR 生成失败：%s" % detail)
    if not os.path.isfile(out_path):
        raise BlenderError("TripoSR 返回成功但 GLB 不存在：%s" % out_path)

    obj_name = str(name or "TripoSR")
    code = ("import bpy\n"
            "before = set(bpy.data.objects)\n"
            "bpy.ops.import_scene.gltf(filepath=%r)\n"
            "created = [ob for ob in bpy.data.objects if ob not in before]\n"
            "meshes = [ob for ob in created if ob.type == 'MESH']\n"
            "bpy.ops.object.select_all(action='DESELECT')\n"
            "for i, ob in enumerate(meshes):\n"
            "    ob.name = %r if i == 0 else %r + '_%%02d' %% i\n"
            "    ob['wasily_backend'] = 'triposr'\n"
            "    ob['wasily_source'] = %r\n"
            "    ob.select_set(True)\n"
            "if meshes:\n"
            "    bpy.context.view_layer.objects.active = meshes[0]\n"
            "print('TripoSR imported:', len(meshes), 'mesh objects')\n"
            % (out_path, obj_name, obj_name, image_path))
    imported = blender_execute(code, timeout=240)
    payload.update({"ok": bool(imported.get("ok")), "object": obj_name,
                    "result": imported,
                    "hint": "TripoSR 是单图 AI 重建，背面与遮挡区域为模型推测，并非扫描级复刻"})
    return payload


def blender_image_to_model(image_path, backend="auto", name="", resolution=256):
    """用图片生成 3D 模型。

    auto 优先走本地免费的 TripoSR 完整网格，未安装时退回单视角浮雕；
    local 强制使用浮雕；hyper3d / hunyuan3d 走插件已配置的外部服务。
    """
    image_path = str(image_path or "").strip()
    if not image_path or not os.path.isfile(image_path):
        raise ValueError("image_path 不存在：%s" % image_path)
    backend = str(backend or "auto").lower()
    if backend == "auto":
        backend = "triposr" if _triposr_status()["available"] else "local"
    if backend == "triposr":
        return _triposr_image_model(image_path, name=name or "TripoSR",
                                    resolution=resolution)
    if backend in ("local", "relief"):
        out = _local_image_relief(image_path, name=name or "Relief")
        out["hint"] = "本地浮雕只有正面；免费完整 360° 重建请用 backend=triposr"
        return out
    if backend == "hyper3d":
        job = _ok("create_rodin_job", {"image_path": image_path}, timeout=60)
    elif backend == "hunyuan3d":
        job = _ok("create_hunyuan_job", {"image_path": image_path}, timeout=60)
    else:
        raise ValueError("不支持的后端：%s（可用 auto / triposr / local / hyper3d / hunyuan3d）"
                         % backend)
    return {"ok": True, "backend": backend, "job": job,
            "next": "用 blender_job_poll 查状态，完成后用 blender_job_import 导入"}


def blender_job_poll(job_id, backend="hyper3d"):
    """轮询外部生成任务状态。"""
    backend = str(backend or "hyper3d").lower()
    cmd = "poll_rodin_job_status" if backend == "hyper3d" else "poll_hunyuan_job_status"
    return {"ok": True, "status": _ok(cmd, {"job_id": job_id}, timeout=60)}


def blender_job_import(job_id, name="", backend="hyper3d"):
    """把已完成的外部生成结果导入当前场景。"""
    backend = str(backend or "hyper3d").lower()
    if backend == "hyper3d":
        res = _ok("import_generated_asset",
                  {"job_id": job_id, "name": name or "generated"}, timeout=180)
    else:
        res = _ok("import_generated_asset_hunyuan",
                  {"job_id": job_id, "name": name or "generated"}, timeout=180)
    return {"ok": True, "result": res}


# ---------------- 资产库检索 ----------------
def blender_asset_search(query, source="polyhaven", asset_type="models", limit=10):
    """在 PolyHaven / Sketchfab / Poly Pizza 检索现成模型或材质。"""
    q = str(query or "").strip()
    source = str(source or "polyhaven").lower()
    if source == "polyhaven":
        res = _ok("search_polyhaven_assets",
                  {"asset_type": asset_type, "categories": [q] if q else []}, timeout=60)
    elif source == "sketchfab":
        res = _ok("search_sketchfab_models", {"query": q}, timeout=60)
    elif source == "polypizza":
        res = _ok("search_polypizza_models", {"query": q}, timeout=60)
    else:
        raise ValueError("不支持的资产源：%s" % source)
    return {"ok": True, "source": source, "result": res}


def blender_asset_download(asset_id, source="polyhaven", asset_type="models", name=""):
    """下载指定资产并导入场景。"""
    source = str(source or "polyhaven").lower()
    if source == "polyhaven":
        res = _ok("download_polyhaven_asset",
                  {"asset_id": asset_id, "asset_type": asset_type,
                   "resolution": "1k", "file_format": "gltf"}, timeout=300)
    elif source == "sketchfab":
        res = _ok("download_sketchfab_model", {"uid": asset_id}, timeout=300)
    elif source == "polypizza":
        res = _ok("download_polypizza_model", {"id": asset_id}, timeout=300)
    else:
        raise ValueError("不支持的资产源：%s" % source)
    return {"ok": True, "result": res}


# ---------------- 程序化模板：房屋 / 物品 / 人物 / 行走 ----------------
_HOUSE_TMPL = r"""
import bpy, math
COLL_NAME = "Wasily_House"
coll = bpy.data.collections.get(COLL_NAME)
if coll is None:
    coll = bpy.data.collections.new(COLL_NAME)
if coll.name not in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.link(coll)
for ob in list(coll.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

def put(ob):
    for c in list(ob.users_collection):
        c.objects.unlink(ob)
    coll.objects.link(ob)
    return ob

def box(name, sx, sy, sz, loc):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.scale = (sx, sy, sz)
    return put(ob)

w, d, h = {w}, {d}, {h}
t = 0.15

def mat(name, color):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        m.diffuse_color = color
    return m

wall_mat = mat("WasilyWall", (0.82, 0.78, 0.68, 1))
roof_mat = mat("WasilyRoof", (0.52, 0.16, 0.13, 1))
floor_mat = mat("WasilyFloor", (0.38, 0.33, 0.28, 1))

box("Floor", w, d, t, (0, 0, -t / 2)).data.materials.append(floor_mat)
box("Wall_N", w, t, h, (0,  d / 2, h / 2)).data.materials.append(wall_mat)
box("Wall_S", w, t, h, (0, -d / 2, h / 2)).data.materials.append(wall_mat)
wall_e = box("Wall_E", t, d, h, ( w / 2, 0, h / 2))
wall_e.data.materials.append(wall_mat)
wall_w = box("Wall_W", t, d, h, (-w / 2, 0, h / 2))
wall_w.data.materials.append(wall_mat)

# 门洞：开在 +X 墙（默认视角可见）
bpy.ops.mesh.primitive_cube_add(size=1, location=(w / 2, 0, h * 0.35))
door = put(bpy.context.active_object)
door.name = "DoorCut"
door.scale = (t * 3, 0.9, h * 0.7)
mod = wall_e.modifiers.new("door", 'BOOLEAN')
mod.operation = 'DIFFERENCE'
mod.object = door
bpy.context.view_layer.objects.active = wall_e
bpy.ops.object.modifier_apply(modifier=mod.name)
bpy.data.objects.remove(door, do_unlink=True)

# 窗户：开在 -Y 墙两处
wall_s = bpy.data.objects["Wall_S"]
for i, x in enumerate((-w * 0.25, w * 0.25)):
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, -d / 2, h * 0.62))
    win = put(bpy.context.active_object)
    win.name = "WindowCut%d" % i
    win.scale = (0.9, t * 3, 0.9)
    wm = wall_s.modifiers.new("win%d" % i, 'BOOLEAN')
    wm.operation = 'DIFFERENCE'
    wm.object = win
    bpy.context.view_layer.objects.active = wall_s
    bpy.ops.object.modifier_apply(modifier=wm.name)
    bpy.data.objects.remove(win, do_unlink=True)

# 四坡屋顶
bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=max(w, d) * 0.78, depth=h * 0.5,
                                location=(0, 0, h + h * 0.25),
                                rotation=(0, 0, math.pi / 4))
roof = put(bpy.context.active_object)
roof.name = "Roof"
roof.data.materials.append(roof_mat)

# 相机与灯光：自动对准房子中心，避免默认渲染只拍到墙角
import mathutils
cam = bpy.data.objects.get("WasilyCam")
if cam is None:
    cam_data = bpy.data.cameras.new("WasilyCam")
    cam = bpy.data.objects.new("WasilyCam", cam_data)
    bpy.context.scene.collection.objects.link(cam)
cam.location = (w * 1.45, -d * 1.75, h * 1.65)
target = mathutils.Vector((0, 0, h * 0.35))
cam.rotation_euler = (target - cam.location).to_track_quat('-Z', 'Y').to_euler()
cam.data.lens = 32
bpy.context.scene.camera = cam
if not any(o.type == 'LIGHT' for o in bpy.data.objects):
    bpy.ops.object.light_add(type='SUN', location=(4, -4, 8))
    bpy.context.active_object.data.energy = 3.0

bpy.context.scene.frame_set(1)
print("house built: %.1f x %.1f x %.1f" % (w, d, h))
"""

_CHAR_TMPL = r"""
import bpy, math
COLL_NAME = "Wasily_Character"
coll = bpy.data.collections.get(COLL_NAME)
if coll is None:
    coll = bpy.data.collections.new(COLL_NAME)
if coll.name not in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.link(coll)
for ob in list(coll.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

def put(ob):
    for c in list(ob.users_collection):
        c.objects.unlink(ob)
    coll.objects.link(ob)
    return ob

def mat(name, color):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        m.diffuse_color = color
    return m

# 地面
bpy.ops.mesh.primitive_plane_add(size=8, location=(0, 0, 0))
ground = put(bpy.context.active_object)
ground.name = "WasilyGround"
ground.data.materials.append(mat("WasilyGroundMat", (0.22, 0.24, 0.28, 1)))

# 骨架
bpy.ops.object.armature_add(location=(0, 0, 0))
rig = put(bpy.context.object)
rig.name = "WasilyRig"
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='EDIT')
eb = rig.data.edit_bones
root = eb[0]
root.name = "root"
root.head = (0, 0, 0)
root.tail = (0, 0, 0.9)
made = {"root": root}
bones = [("spine", (0, 0, 0.9), (0, 0, 1.4), "root"),
         ("head",  (0, 0, 1.4), (0, 0, 1.68), "spine"),
         ("arm.L", (-0.18, 0, 1.33), (-0.18, 0, 0.95), "spine"),
         ("arm.R", ( 0.18, 0, 1.33), ( 0.18, 0, 0.95), "spine"),
         ("leg.L", (-0.1, 0, 0.9), (-0.1, 0, 0.12), "root"),
         ("leg.R", ( 0.1, 0, 0.9), ( 0.1, 0, 0.12), "root")]
for nm, hd, tl, parent in bones:
    b = eb.new(nm)
    b.head = hd
    b.tail = tl
    b.parent = made.get(parent, root)
    made[nm] = b
bpy.ops.object.mode_set(mode='OBJECT')

# 身体部件：每个部件 100% 绑定到一根骨骼
def part(name, scale, loc, parent_bone, color):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=loc, segments=24, ring_count=16)
    ob = put(bpy.context.active_object)
    ob.name = name
    ob.scale = scale
    ob.data.materials.append(mat(name + "Mat", color))
    vg = ob.vertex_groups.new(name=parent_bone)
    vg.add(range(len(ob.data.vertices)), 1.0, 'REPLACE')
    m = ob.modifiers.new("arm", 'ARMATURE')
    m.object = rig
    ob.parent = rig
    ob.matrix_parent_inverse = rig.matrix_world.inverted()
    return ob

part("body",      (0.22, 0.16, 0.34), (0, 0, 1.15), "spine", (0.24, 0.55, 0.85, 1))
part("head_mesh", (0.16, 0.16, 0.17), (0, 0, 1.56), "head",  (0.92, 0.76, 0.62, 1))
part("armL_mesh", (0.07, 0.07, 0.22), (-0.18, 0, 1.14), "arm.L", (0.24, 0.55, 0.85, 1))
part("armR_mesh", (0.07, 0.07, 0.22), ( 0.18, 0, 1.14), "arm.R", (0.24, 0.55, 0.85, 1))
part("legL_mesh", (0.08, 0.08, 0.24), (-0.1, 0, 0.52), "leg.L", (0.18, 0.2, 0.3, 1))
part("legR_mesh", (0.08, 0.08, 0.24), ( 0.1, 0, 0.52), "leg.R", (0.18, 0.2, 0.3, 1))

# 行走循环：1 / 15 / 30 帧，四肢交替摆动，同时整体前移
scene = bpy.context.scene
scene.frame_start = 1
scene.frame_end = 30
bpy.ops.object.select_all(action='DESELECT')
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='POSE')
pose = rig.pose.bones
swing = math.radians(30)
for f in (1, 15, 30):
    scene.frame_set(f)
    s = 1 if f in (1, 30) else -1
    for bn in ("arm.L", "arm.R", "leg.L", "leg.R"):
        pose[bn].rotation_mode = 'XYZ'
    pose["arm.L"].rotation_euler = ( s * swing, 0, 0)
    pose["arm.R"].rotation_euler = (-s * swing, 0, 0)
    pose["leg.L"].rotation_euler = (-s * swing, 0, 0)
    pose["leg.R"].rotation_euler = ( s * swing, 0, 0)
    for bn in ("arm.L", "arm.R", "leg.L", "leg.R"):
        pose[bn].keyframe_insert(data_path="rotation_euler", frame=f)
    pose["root"].location = (0, 0, 0) if f == 1 else ((2.0, 0, 0) if f == 30 else (1.0, 0, 0.04))
    pose["root"].keyframe_insert(data_path="location", frame=f)
bpy.ops.object.mode_set(mode='OBJECT')
scene.frame_set(1)
# 放到房屋外侧，避免与程序化房屋重叠
rig.location = (2.6, -4.6, 0)

# 相机 / 灯光
cam = bpy.data.objects.get("WasilyCam")
if cam is None:
    bpy.ops.object.camera_add(location=(4.2, -6.0, 3.2),
                              rotation=(math.radians(66), 0, math.radians(32)))
    cam = bpy.context.active_object
    cam.name = "WasilyCam"
scene.camera = cam
if not any(o.type == 'LIGHT' for o in bpy.data.objects):
    bpy.ops.object.light_add(type='SUN', location=(4, -4, 8))
    bpy.context.active_object.data.energy = 3.0
print("character + walk cycle built")
"""

_FURNITURE_TMPL = r"""
import bpy, math
COLL_NAME = "Wasily_Furniture"
coll = bpy.data.collections.get(COLL_NAME)
if coll is None:
    coll = bpy.data.collections.new(COLL_NAME)
if coll.name not in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.link(coll)
for ob in list(coll.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

def put(ob):
    for c in list(ob.users_collection):
        c.objects.unlink(ob)
    coll.objects.link(ob)
    return ob

def box(name, scale, loc):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    ob = put(bpy.context.active_object)
    ob.name = name
    ob.scale = scale
    return ob

kind = "{kind}"
if kind == "table":
    box("TableTop", (0.8, 0.5, 0.04), (0, 0, 0.72))
    for i, (x, y) in enumerate([(-0.34, -0.2), (0.34, -0.2), (-0.34, 0.2), (0.34, 0.2)]):
        box("Leg%d" % i, (0.05, 0.05, 0.7), (x, y, 0.35))
elif kind == "chair":
    box("Seat", (0.42, 0.42, 0.05), (0, 0, 0.45))
    box("Back", (0.42, 0.04, 0.55), (0, -0.19, 0.75))
    for i, (x, y) in enumerate([(-0.17, -0.17), (0.17, -0.17), (-0.17, 0.17), (0.17, 0.17)]):
        box("Leg%d" % i, (0.04, 0.04, 0.45), (x, y, 0.22))
else:
    box("Block", (0.5, 0.5, 0.5), (0, 0, 0.5))

cam = bpy.data.objects.get("WasilyCam")
if cam is None:
    bpy.ops.object.camera_add(location=(2.4, -3.2, 2.1),
                              rotation=(math.radians(64), 0, math.radians(36)))
    cam = bpy.context.active_object
    cam.name = "WasilyCam"
bpy.context.scene.camera = cam
if not any(o.type == 'LIGHT' for o in bpy.data.objects):
    bpy.ops.object.light_add(type='SUN', location=(3, -3, 6))
    bpy.context.active_object.data.energy = 3.0
print("furniture built: " + kind)
"""


def blender_build_house(width=6.0, depth=5.0, height=2.8):
    """程序化生成一栋带门窗与四坡屋顶的房子（离线可用）。"""
    code = _HOUSE_TMPL.replace("{w}", repr(float(width))) \
                     .replace("{d}", repr(float(depth))) \
                     .replace("{h}", repr(float(height)))
    return blender_execute(code, timeout=120)


def blender_build_character(walk=True):
    """生成带骨骼与行走循环的人物（离线可用）。walk=False 只建静态角色。"""
    code = _CHAR_TMPL if walk else _CHAR_TMPL.split("# 行走循环")[0]
    return blender_execute(code, timeout=180)


def blender_build_furniture(kind="table"):
    """生成常用家具：table / chair / block。"""
    kind = str(kind or "table").lower()
    if kind not in ("table", "chair", "block"):
        raise ValueError("kind 只支持 table / chair / block")
    return blender_execute(_FURNITURE_TMPL.replace("{kind}", kind), timeout=90)


# ---------------- MCP 工具表 ----------------
def _tool(name, desc, props=None, required=None):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {},
                            "required": required or []}}


TOOLS = [
    _tool("blender_status", "检查 Blender 是否在线，返回插件版本与场景概况"),
    _tool("blender_scene_info", "读取当前场景：对象列表、选中对象、渲染设置"),
    _tool("blender_object_info", "读取单个对象的位置/尺寸/材质/父子关系",
          {"name": {"type": "string", "description": "对象名"}}, ["name"]),
    _tool("blender_screenshot", "截取 3D 视口并返回 base64 PNG（同时落盘到 outputs/blender）",
          {"max_size": {"type": "integer", "description": "最长边像素，默认 1200"},
           "path": {"type": "string", "description": "输出 PNG 路径，留空自动命名"}}),
    _tool("blender_execute", "在 Blender 内执行 Python(bpy)：建模/材质/动画/渲染通用入口，报错会原文回传",
          {"code": {"type": "string", "description": "要执行的 Python 代码"},
           "timeout": {"type": "integer", "description": "超时秒数，默认 120"}}, ["code"]),
    _tool("blender_export", "导出场景为 GLB/FBX",
          {"path": {"type": "string", "description": "输出路径，留空写到 outputs/blender"},
           "fmt": {"type": "string", "description": "GLB 或 FBX，默认 GLB"}}),
    _tool("blender_gen_status", "查看文生3D/图生3D后端可用性（是否已配 API Key）"),
    _tool("blender_text_to_model", "用文字生成 3D 模型：auto/local 走离线模板（房子/人物/家具），hyper3d/hunyuan3d 需 API Key",
          {"prompt": {"type": "string", "description": "模型描述"},
           "backend": {"type": "string", "description": "auto / local / hyper3d / hunyuan3d，默认 auto"}},
          ["prompt"]),
    _tool("blender_image_to_model",
          "用图片生成 3D 模型。auto 优先使用本地免费 TripoSR，生成完整 360° 顶点颜色网格并自动导入；"
          "local 是单视角深度浮雕；hyper3d/hunyuan3d 需 API Key",
          {"image_path": {"type": "string", "description": "本地图片路径"},
           "backend": {"type": "string",
                       "description": "auto / triposr / local / hyper3d / hunyuan3d，默认 auto"},
           "name": {"type": "string", "description": "导入后的对象名前缀"},
           "resolution": {"type": "integer",
                          "description": "TripoSR 网格分辨率，默认 256；越高越慢、内存越大"}},
          ["image_path"]),
    _tool("blender_job_poll", "轮询外部生成任务状态",
          {"job_id": {"type": "string"},
           "backend": {"type": "string", "description": "hyper3d / hunyuan3d"}}, ["job_id"]),
    _tool("blender_job_import", "把已完成的外部生成结果下载并导入场景",
          {"job_id": {"type": "string"}, "name": {"type": "string"},
           "backend": {"type": "string", "description": "hyper3d / hunyuan3d"}}, ["job_id"]),
    _tool("blender_asset_search", "检索 PolyHaven / Sketchfab / Poly Pizza 现成资产",
          {"query": {"type": "string"}, "source": {"type": "string"},
           "asset_type": {"type": "string"}, "limit": {"type": "integer"}}),
    _tool("blender_asset_download", "下载并导入指定资产",
          {"asset_id": {"type": "string"}, "source": {"type": "string"},
           "asset_type": {"type": "string"}, "name": {"type": "string"}}, ["asset_id"]),
    _tool("blender_build_house", "程序化生成带门窗与屋顶的房子（离线）",
          {"width": {"type": "number"}, "depth": {"type": "number"},
           "height": {"type": "number"}}),
    _tool("blender_build_character", "生成带骨骼与行走循环的人物（离线）",
          {"walk": {"type": "boolean", "description": "是否生成行走动画，默认 true"}}),
    _tool("blender_build_furniture", "生成常用家具（离线）",
          {"kind": {"type": "string", "description": "table / chair / block"}}),
]

_HANDLERS = {
    "blender_status": blender_status,
    "blender_scene_info": blender_scene_info,
    "blender_object_info": blender_object_info,
    "blender_screenshot": blender_screenshot,
    "blender_execute": blender_execute,
    "blender_export": blender_export,
    "blender_gen_status": blender_gen_status,
    "blender_text_to_model": blender_text_to_model,
    "blender_image_to_model": blender_image_to_model,
    "blender_job_poll": blender_job_poll,
    "blender_job_import": blender_job_import,
    "blender_asset_search": blender_asset_search,
    "blender_asset_download": blender_asset_download,
    "blender_build_house": blender_build_house,
    "blender_build_character": blender_build_character,
    "blender_build_furniture": blender_build_furniture,
}


def _call(name, args):
    fn = _HANDLERS.get(name)
    if not fn:
        raise ValueError("未知工具：%s" % name)
    return fn(**(args or {}))


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        return _send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER}})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _send({"jsonrpc": "2.0", "id": mid, "result": {}})
    if method == "tools/list":
        return _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    if method == "tools/call":
        name = params.get("name")
        try:
            out = _call(name, params.get("arguments") or {})
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, ensure_ascii=False, indent=2)}],
                "isError": False}})
        except Exception as exc:  # noqa: BLE001
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps({
                    "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                    "trace": traceback.format_exc(limit=2)}, ensure_ascii=False)}],
                "isError": True}})
        return None
    return _send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "不支持的方法：%s" % method}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "解析失败：%s" % exc}})


if __name__ == "__main__":
    main()
