"""
GPU 加速环境配置与基准测试（MX450 2GB）
功能：
  1. 检测 llama-cpp-python 是否为 CUDA 版（检查包内 cudart64_*.dll）
  2. 自动安装/回滚 cu121 预编译轮子（无需 CUDA Toolkit）
  3. 按显存自动计算 n_gpu_layers
  4. tokens/s 基准对比：python gpu_setup.py bench [--cpu] [--layers N] [--rounds 2]
"""
import os
import re
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import config  # noqa: E402

CU121_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cu121/"
GPU_WHEEL_VERSION = "0.3.4"
CPU_WHEEL_VERSION = "0.3.36"


def llama_cpp_info():
    """返回 {version, cuda: bool, lib_dir}"""
    try:
        import llama_cpp
    except ImportError:
        return {"version": None, "cuda": False, "lib_dir": None}
    lib_dir = os.path.join(os.path.dirname(llama_cpp.__file__), "lib")
    cuda = False
    if os.path.isdir(lib_dir):
        for f in os.listdir(lib_dir):
            if re.match(r"cudart64_\d+\.dll", f, re.I):
                cuda = True
                break
    return {"version": llama_cpp.__version__, "cuda": cuda, "lib_dir": lib_dir}


def gpu_total_vram_mb():
    """经 nvidia-smi 读取总显存(MB)，失败返回 0"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        ).stdout.strip().splitlines()
        return int(out[0])
    except Exception:
        return 0


def gpu_free_vram_mb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        ).stdout.strip().splitlines()
        return int(out[0])
    except Exception:
        return 0


def estimate_gpu_layers(model_size_gb=4.4, n_layers=28, n_ctx=4096):
    """
    按显存估算可卸载层数：
      可用显存 = 总显存×0.82 - KV/compute缓冲约550MB
      每层权重 ≈ 模型体积/层数（Q4_K_M 7B ≈ 160MB/层）
    """
    vram = gpu_total_vram_mb()
    if vram <= 0:
        return 0
    usable = vram * 0.82 - 550
    per_layer = model_size_gb * 1024 / n_layers
    n = int(usable / per_layer)
    return max(0, min(n, n_layers - 2))


def model_layer_count(model_path):
    """读 GGUF 元数据里的 block_count（不加载模型权重），失败返回 0"""
    try:
        from gguf import GGUFReader
        r = GGUFReader(model_path)
        for k, f in r.fields.items():
            if k.endswith(".block_count"):
                try:
                    return int(f.contents())
                except Exception:
                    return int(f.parts[-1][0])
    except Exception:
        return 0
    return 0


def estimate_layers_for(model_path, n_ctx=8192, reserve_mb=550):
    """按「空闲显存 + 该模型层数」估算可卸载层数；够就返回全部层（全量卸载）"""
    total_layers = model_layer_count(model_path)
    try:
        size_bytes = os.path.getsize(model_path)
        base = os.path.basename(model_path)
        if "-00001-of-" in base:   # 分片模型：把同名前缀的分片都算上
            d = os.path.dirname(model_path)
            prefix = base.split("-00001-of-")[0]
            size_bytes = sum(os.path.getsize(os.path.join(d, f))
                             for f in os.listdir(d) if f.startswith(prefix))
    except Exception:
        return 0
    size_gb = size_bytes / 1024 ** 3
    if not total_layers or not size_gb:
        return 0
    free = gpu_free_vram_mb() or gpu_total_vram_mb()
    if free <= 0:
        return 0
    per_layer = size_gb * 1024 / total_layers
    n = int((free * 0.95 - reserve_mb) / per_layer)
    return max(0, min(n, total_layers))


def install_gpu_wheel():
    """安装 cu121 预编译轮子（自带CUDA运行时，免Toolkit）。返回 bool"""
    print(f"[gpu] 安装 llama-cpp-python=={GPU_WHEEL_VERSION} (CUDA 12.1 预编译)...")
    cmd = [sys.executable, "-m", "pip", "install",
           f"llama-cpp-python=={GPU_WHEEL_VERSION}",
           "--extra-index-url", CU121_INDEX,
           "--force-reinstall", "--no-cache-dir"]
    r = subprocess.run(cmd)
    if r.returncode == 0 and llama_cpp_info()["cuda"]:
        print("[gpu] CUDA 版安装成功")
        return True
    print("[gpu] 安装失败")
    return False


def rollback_cpu_wheel():
    """一键回滚 CPU 版"""
    print(f"[gpu] 回滚 llama-cpp-python=={CPU_WHEEL_VERSION} (CPU)...")
    subprocess.run([sys.executable, "-m", "pip", "install",
                    f"llama-cpp-python=={CPU_WHEEL_VERSION}",
                    "--force-reinstall", "--no-cache-dir"])


def resolve_gpu_layers():
    """
    读取 config 的 n_gpu_layers 决策：
      0  → 纯CPU；>0 → 直接用；-2 → 按显存自动计算并写回 config
    """
    v = config.get("server.n_gpu_layers", -2)
    if v is None or int(v) == -2:
        n = estimate_gpu_layers()
        config.set_value("server.n_gpu_layers", n)
        print(f"[gpu] 自动计算 n_gpu_layers={n} (显存 {gpu_total_vram_mb()}MB)")
        return n
    return int(v)


def _run_bench(n_gpu_layers, rounds):
    """加载模型跑固定提示，返回 {tokens_per_sec, load_sec, layers}"""
    import json as _json
    model_path = config.get("server.model_path")
    n_ctx = config.get("server.n_ctx", 4096)
    from llama_cpp import Llama
    t0 = time.time()
    llm = Llama(model_path=model_path, n_ctx=n_ctx,
                n_threads=config.get("server.n_threads") or (os.cpu_count() or 4),
                n_gpu_layers=n_gpu_layers, verbose=False)
    load_sec = time.time() - t0
    prompt = "".join(
        f"<|im_start|>user\n第{i}问：用约150字介绍一下人工智能的{'历史' if i % 2 else '应用'}。<|im_end|>\n"
        for i in range(rounds)
    ) + "<|im_start|>assistant\n"
    n_tok = 0
    t1 = time.time()
    for _ in range(rounds):
        stream = llm(prompt, max_tokens=128, temperature=0.7, stream=True,
                     stop=["<|im_end|>"])
        for chunk in stream:
            n_tok += 1
    gen_sec = time.time() - t1
    return {"layers": n_gpu_layers, "load_sec": round(load_sec, 1),
            "tokens": n_tok, "gen_sec": round(gen_sec, 1),
            "tokens_per_sec": round(n_tok / gen_sec, 2) if gen_sec else 0}


def bench(layers_list=None, rounds=2):
    """多组层数基准对比，输出表格"""
    if llama_cpp_info()["cuda"]:
        candidates = layers_list or [0, max(4, resolve_gpu_layers()),
                                     min(26, resolve_gpu_layers() + 6)]
    else:
        candidates = [0]
    print(f"{'层数':>4} | {'加载(s)':>7} | {'tokens':>6} | {'耗时(s)':>7} | {'tokens/s':>8}")
    print("-" * 45)
    for n in candidates:
        try:
            r = _run_bench(n, rounds)
            print(f"{r['layers']:>4} | {r['load_sec']:>7} | {r['tokens']:>6} | "
                  f"{r['gen_sec']:>7} | {r['tokens_per_sec']:>8}")
        except Exception as e:
            print(f"{n:>4} | 加载/推理失败: {str(e)[:80]}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "bench":
        cpu_flag = "--cpu" in args
        n = int(args[args.index("--layers") + 1]) if "--layers" in args else None
        rounds = int(args[args.index("--rounds") + 1]) if "--rounds" in args else 2
        if cpu_flag:
            bench([0], rounds)
        elif n is not None:
            bench([n], rounds)
        else:
            bench(None, rounds)
    elif args and args[0] == "install":
        install_gpu_wheel()
    elif args and args[0] == "rollback":
        rollback_cpu_wheel()
    else:
        info = llama_cpp_info()
        print(json_info := f"llama-cpp-python {info['version']} | CUDA: {info['cuda']} | "
                           f"显存: {gpu_total_vram_mb()}MB | 自动层数: {estimate_gpu_layers()}")
