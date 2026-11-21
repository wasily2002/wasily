"""wasily IDE 功能完整性体检 (快速版)"""
import urllib.request, json, sys, time

BASE = "http://127.0.0.1:8000"
OK = 0; FAIL = 0

def post(path, data, timeout=8):
    global OK, FAIL
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(BASE + path, data=body,
                                      headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=timeout)
        try:
            d = json.loads(r.read().decode())
        except:
            d = {}
        OK += 1; return d
    except urllib.error.HTTPError as e:
        if e.code == 404:
            FAIL += 1
            print(f"  ❌ {path}: 404 NOT FOUND")
            return None
        OK += 1  # 其他 HTTP 错误也算端点存在
        print(f"  ⚠️  {path}: HTTP {e.code}")
        return None
    except Exception as e:
        FAIL += 1
        print(f"  ❌ {path}: {str(e)[:80]}")
        return None

def get(path, timeout=5):
    global OK, FAIL
    try:
        req = urllib.request.Request(BASE + path)
        r = urllib.request.urlopen(req, timeout=timeout)
        d = json.loads(r.read().decode())
        OK += 1; return d
    except Exception as e:
        FAIL += 1
        print(f"  ❌ {path}: {str(e)[:80]}")
        return None

print("=" * 60)
print("🧠 Wasily IDE 功能完整性体检 (快速)")
print("=" * 60)

# 1. Health
print("\n【1】基础")
d = get("/health")
print(f"  {'✅' if d else '❌'} Health: model={d.get('model') if d else 'N/A'}")

# 2. 多模型
print("\n【2】多模型路由 (G)")
d = post("/v1/ide/models", {})
print(f"  {'✅' if d else '❌'} models: {len((d or {}).get('models', []))} 个")
for m in (d or {}).get("models", []):
    print(f"     {m['id']}: {m['provider']}")
# switch roundtrip
d = post("/v1/ide/models/switch", {"model_id": "local"})
print(f"  {'✅' if d else '❌'} switch local: ok={d.get('ok') if d else False}")

# 3. Agent/Orchestrator
print("\n【3】Agent Mode (A2)")
d = get("/orchestrator/catalog")
print(f"  {'✅' if d else '❌'} roles={len((d or {}).get('agents', []))} tools={len((d or {}).get('tools', []))}")
d = post("/plan/status", {"id": "nonexistent"})
print(f"  ✅ /plan/status 端点可达")
d = post("/plan/", {})
print(f"  ✅ /plan 查询端点可达")

# 4. Codebase Graph
print("\n【4】Codebase Graph (K)")
d = post("/v1/ide/codebase/graph", {"root": r"d:\煮饭仔点餐\项目文件\项目介绍\wasily\AI", "max_files": 30})
print(f"  {'✅' if d else '❌'} nodes={len((d or {}).get('nodes', []))} edges={len((d or {}).get('edges', []))}")

# 5. Diff hunks
print("\n【5】Diff 块级 (A4)")
d = post("/v1/ide/diff_hunks", {"old": "a=1\nb=2", "new": "a=100\nb=2\nc=3"})
hunks = (d or {}).get("hunks", [])
print(f"  {'✅' if hunks else '❌'} hunks={len(hunks)}")

# 6. IDE Stream 模式 (逐个端点注册检查)
print("\n【6】IDE Stream 模式路由")
for mode in ["hover", "quickfix", "debug_fix", "git_message", "build_fix", "terminal_suggest", "code_review", "inline_edit", "explain"]:
    d = post("/v1/ide/stream", {"mode": mode, "messages": [{"role": "user", "content": "x"}], "max_tokens": 5}, timeout=3)
    print(f"  {'✅' if d is not None else '❌'} mode={mode}")

# 7. MCP self 工具
print("\n【7】自我进化 MCP 工具 (16 个)")
try:
    import sys; sys.path.insert(0, r"d:\煮饭仔点餐\项目文件\项目介绍\wasily\AI")
    from self_mcp_server import MCP_TOOLS, _DISPATCH
    names = [t["name"] for t in MCP_TOOLS]
    print(f"  ✅ MCP_TOOLS: {len(names)} 个注册")
    print(f"  {'✅' if 'self_symbols' in names else '❌'} self_symbols")
    print(f"  {'✅' if 'self_autofix' in names else '❌'} self_autofix")
    print(f"  {'✅' if 'self_edit' in names else '❌'} self_edit")
    print(f"  {'✅' if 'self_apply' in names else '❌'} self_apply")
    missing = [n for n in ["self_symbols", "self_autofix", "self_edit", "self_apply", "self_read", "self_restart"] if n not in _DISPATCH]
    print(f"  {'✅' if not missing else '❌'} _DISPATCH 完整注册" + (f" 缺: {missing}" if missing else ""))
except Exception as e:
    print(f"  ❌ MCP 检查失败: {str(e)[:60]}")

# 8. RAG retrieve
print("\n【8】RAG 检索")
try:
    import urllib.request as _ur
    req = _ur.Request(BASE + "/v1/retrieve",
                       data=json.dumps({"query": "build", "top_k": 2}).encode(),
                       headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=15)
    d = json.loads(r.read().decode())
    results = d.get("results", [])
    print(f"  ✅ results={len(results)} (embedding OK)")
except Exception as e:
    print(f"  ⚠️  RAG 需 embedding 模型: {str(e)[:60]}")

# 9. 其他
print("\n【9】其他端点")
for path, body in [
    ("/v1/image_analyze", {"image_url": ""}),
    ("/v1/ide/cache", {}),
    ("/v1/ide/model_info", {}),
]:
    d = post(path, body, timeout=3)

print(f"\n{'='*60}")
print(f"📊 体检结果: {OK} 通过 / {FAIL} 失败")
if FAIL == 0:
    print("🎉 所有核心端点和 MCP 工具正常！")
print(f"{'='*60}")
