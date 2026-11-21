"""独立嵌入进程：llama.cpp 与 torch 同进程会原生崩溃（无 Python 回溯），
所以向量编码放在子进程里做，主服务只通过管道要结果。"""
import json
import os
import sys

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def main():
    model_dir = sys.argv[1]
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_dir, device="cpu", local_files_only=True)
    out = sys.stdout
    out.write(json.dumps({"ready": True,
                          "dim": int(model.get_sentence_embedding_dimension() or 0)},
                         ensure_ascii=False) + "\n")
    out.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            texts = [str(t) for t in (req.get("texts") or [])]
            vecs = model.encode(texts, batch_size=int(req.get("batch_size", 16)),
                                show_progress_bar=False, convert_to_numpy=True,
                                normalize_embeddings=True)
            msg = {"vectors": vecs.tolist()}
        except Exception as exc:  # noqa: BLE001 单条失败不能拖垮整条管道
            msg = {"error": f"{type(exc).__name__}: {exc}"}
        out.write(json.dumps(msg) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
