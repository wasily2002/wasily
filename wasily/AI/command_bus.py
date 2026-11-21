"""跨页面命令总线：命令执行状态的唯一事实来源。

为什么需要它：Agent 窗口（/agent/）与 IDE 面板（/panel_ui）是两个独立页面，
各自的对话流里只看得到自己触发的工具调用。没有共享状态时，一边在跑命令，
另一边完全看不到，中断也只能作用于本页。

这里把「启动 → 逐行输出 → 结束 / 中断」统一成带全局递增序号的增量事件：
- 两个页面订阅同一条 SSE 流，天然看到同一份状态；
- 事件带 id，断线重连按 after 序号补齐，不会错位或丢行；
- 中断只作用于明确的那一个 run_id，先广播状态再杀进程树。

进程输出按 readline 逐行发布，不等待命令结束，保证两页都是实时滚动而不是
结束后一次性回填。
"""
from __future__ import annotations

import collections
import os
import queue
import subprocess
import threading
import time
import uuid
from typing import Any, Callable

MAX_LINES_PER_RUN = 4000      # 单条命令保留的实时行数上限（内存与回放体积的平衡）
MAX_RUNS = 60                 # 保留的历史命令数
MAX_EVENTS = 8000             # 全局事件环形缓冲，供断线重放
SUB_QUEUE_SIZE = 2000         # 订阅队列；满了说明客户端太慢，靠重放补齐

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0

_LOCK = threading.RLock()
_RUNS: "collections.OrderedDict[str, _Run]" = collections.OrderedDict()
_EVENTS: "collections.deque[dict]" = collections.deque(maxlen=MAX_EVENTS)
_SUBS: dict[str, "queue.Queue[dict]"] = {}
_SEQ = [0]


def _next_id() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _publish(ev_type: str, **fields: Any) -> dict:
    """分配全局序号并扇出。序号与追加在同一把锁内完成，保证两页看到相同顺序。"""
    with _LOCK:
        ev = {"id": _next_id(), "type": ev_type, "ts": time.time()}
        ev.update(fields)
        _EVENTS.append(ev)
        subs = list(_SUBS.values())
    for q in subs:
        try:
            q.put_nowait(ev)
        except queue.Full:
            # 慢客户端直接丢，重连时用 after 序号重放，状态不会因此不一致
            pass
    return ev


def subscribe() -> tuple[str, "queue.Queue[dict]"]:
    token = "cmdsub_%s" % uuid.uuid4().hex[:12]
    q: "queue.Queue[dict]" = queue.Queue(maxsize=SUB_QUEUE_SIZE)
    with _LOCK:
        _SUBS[token] = q
    return token, q


def unsubscribe(token: str) -> None:
    with _LOCK:
        _SUBS.pop(token, None)


def events_after(seq: int, run_id: str = "", limit: int = MAX_EVENTS) -> list[dict]:
    with _LOCK:
        out = [e for e in _EVENTS if e["id"] > seq]
    if run_id:
        out = [e for e in out if e.get("run_id") == run_id]
    return out[:limit]


class _Run:
    """一次命令执行；行数据与状态都在自己的锁里维护。"""

    def __init__(self, command: str, cwd: str, source: str,
                 session_id: str, timeout: int, meta: dict | None) -> None:
        self.id = "cmd_" + uuid.uuid4().hex[:12]
        self.command = command
        self.cwd = cwd
        self.source = source or "agent"
        self.session_id = session_id
        self.timeout = int(timeout or 0)
        self.meta = dict(meta or {})
        self.status = "running"       # running / exited / failed / aborted / timeout
        self.exit_code: int | None = None
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.owner = ""               # 触发页面标识（由前端上报）
        self.proc: subprocess.Popen | None = None
        self.done = threading.Event()
        self._lock = threading.RLock()
        self._lines: list[dict] = []
        self._line_seq = 0
        self._threads: list[threading.Thread] = []
        self._abort_reason = ""

    # ---------- 对外快照 ----------
    def summary(self) -> dict:
        with self._lock:
            return {
                "id": self.id, "command": self.command, "cwd": self.cwd,
                "source": self.source, "session_id": self.session_id,
                "owner": self.owner, "status": self.status,
                "exit_code": self.exit_code, "timeout": self.timeout,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "line_count": self._line_seq,
                "abort_reason": self._abort_reason,
                "meta": dict(self.meta),
            }

    def lines_after(self, after: int) -> list[dict]:
        with self._lock:
            return [ln for ln in self._lines if ln["seq"] > int(after or 0)]

    def output(self) -> str:
        with self._lock:
            out = [ln["text"] for ln in self._lines if ln["stream"] == "stdout"]
            err = [ln["text"] for ln in self._lines if ln["stream"] == "stderr"]
        text = "\n".join(out)
        if err:
            text += "\n[stderr]\n" + "\n".join(err)
        if self.exit_code not in (None, 0):
            text += "\n[exit code: %s]" % self.exit_code
        return text.strip()

    # ---------- 事件 ----------
    def _emit_line(self, stream: str, text: str) -> None:
        with self._lock:
            self._line_seq += 1
            line = {"seq": self._line_seq, "stream": stream,
                    "text": text, "ts": time.time()}
            self._lines.append(line)
            if len(self._lines) > MAX_LINES_PER_RUN:
                del self._lines[:len(self._lines) - MAX_LINES_PER_RUN]
            snapshot = self.summary()
        _publish("line", run_id=self.id, line=line, run=snapshot)

    def _emit_run(self, event: str = "run") -> None:
        _publish(event, run_id=self.id, run=self.summary())

    # ---------- 生命周期 ----------
    def spawn(self, command: str, cwd: str) -> None:
        flags = _NO_WINDOW | _NEW_GROUP
        try:
            self.proc = subprocess.Popen(
                command, shell=True, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", bufsize=1, creationflags=flags,
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.status = "failed"
                self.ended_at = time.time()
                self._abort_reason = "spawn_failed: %s" % exc
            self.done.set()
            self._emit_run()
            return

        for stream, name in ((self.proc.stdout, "stdout"), (self.proc.stderr, "stderr")):
            if stream is None:
                continue
            t = threading.Thread(target=self._pump, args=(stream, name),
                                 name="cmd-%s-%s" % (self.id, name), daemon=True)
            t.start()
            self._threads.append(t)
        threading.Thread(target=self._watch, args=(self.proc,),
                         name="cmd-%s-watch" % self.id, daemon=True).start()
        self._emit_run("run")

    def _pump(self, stream: Any, name: str) -> None:
        try:
            while True:
                chunk = stream.readline()
                if not chunk:
                    break
                self._emit_line(name, chunk.rstrip("\r\n"))
        except Exception:  # noqa: BLE001 — 进程被杀时读管道会抛错，属于正常收尾
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _watch(self, proc: subprocess.Popen) -> None:
        code: int | None = None
        try:
            code = proc.wait(timeout=self.timeout if self.timeout > 0 else None)
        except subprocess.TimeoutExpired:
            self.request_abort("timeout")
            try:
                code = proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                code = None
        except Exception:  # noqa: BLE001
            code = None
        for t in self._threads:
            t.join(timeout=5)
        self._finish(code)

    def _finish(self, code: int | None) -> None:
        with self._lock:
            if self.status == "running":
                self.status = "exited" if code == 0 else "failed"
            self.exit_code = code
            self.ended_at = time.time()
        self.done.set()
        self._emit_run()

    def request_abort(self, reason: str = "aborted", by: str = "") -> bool:
        """先广播「已中断」，再杀进程树——另一页能立刻看到终止状态而不是干等。

        杀进程（Windows 上是 taskkill /T）要几百毫秒，放后台线程执行，
        中断接口本身立即返回，两页的状态切换不受它拖慢。
        """
        with self._lock:
            if self.status != "running":
                return False
            self.status = "timeout" if reason == "timeout" else "aborted"
            self._abort_reason = by or reason
        self._emit_run()
        threading.Thread(target=self._kill, name="cmd-%s-kill" % self.id,
                         daemon=True).start()
        return True

    def _kill(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if os.name == "nt":
            try:
                # 只针对该 PID 的进程树，不做任何批量 taskkill
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10,
                               creationflags=_NO_WINDOW)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass


def _evict() -> None:
    """只淘汰已结束的旧命令，运行中的永不丢弃。调用方需持有 _LOCK。"""
    if len(_RUNS) <= MAX_RUNS:
        return
    for key in list(_RUNS.keys()):
        if len(_RUNS) <= MAX_RUNS:
            break
        run = _RUNS[key]
        if run.status != "running":
            del _RUNS[key]


def start(command: str, cwd: str = "", source: str = "agent",
          session_id: str = "", timeout: int = 0,
          owner: str = "", meta: dict | None = None) -> dict:
    """登记并异步启动一条命令，立即返回快照（状态 running）。"""
    cmd = str(command or "").strip()
    if not cmd:
        raise ValueError("command 不能为空")
    workdir = os.path.abspath(cwd) if cwd else os.getcwd()
    if not os.path.isdir(workdir):
        workdir = os.getcwd()
    run = _Run(cmd, workdir, source, session_id, timeout, meta)
    run.owner = str(owner or "")
    with _LOCK:
        _RUNS[run.id] = run
        _evict()
    run.spawn(cmd, workdir)
    return run.summary()


def get_run(run_id: str) -> "_Run | None":
    with _LOCK:
        return _RUNS.get(str(run_id or ""))


def run_sync(command: str, cwd: str = "", timeout: int = 60,
             source: str = "agent", session_id: str = "",
             owner: str = "", meta: dict | None = None) -> dict:
    """同步执行：等命令结束并返回聚合结果，供 shell_exec 这类需要返回值的调用方。

    返回值保持与旧 subprocess.run 路径一致（stdout + [stderr] + [exit code]），
    模型看到的工具结果不会因为接入总线而变化。
    """
    snap = start(command, cwd=cwd, source=source, session_id=session_id,
                 timeout=timeout, owner=owner, meta=meta)
    run = get_run(snap["id"])
    if run is None:
        return {"ok": False, "status": "failed", "output": "命令登记失败",
                "exit_code": None, "run_id": snap["id"]}
    run.done.wait(None if timeout <= 0 else timeout + 30)
    return {"ok": True, "status": run.status, "output": run.output(),
            "exit_code": run.exit_code, "run_id": run.id,
            "reason": run._abort_reason,  # noqa: SLF001
            "duration": round((run.ended_at or time.time()) - run.started_at, 3)}


def abort(run_id: str, by: str = "", reason: str = "aborted") -> dict:
    run = get_run(run_id)
    if run is None:
        raise KeyError("命令不存在: %s" % run_id)
    changed = run.request_abort(reason, by)
    return {"ok": True, "changed": changed, "run": run.summary()}


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK:
        runs = list(_RUNS.values())
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return [r.summary() for r in runs[:max(1, min(int(limit or 30), 200))]]


def detail(run_id: str, after: int = 0) -> dict:
    run = get_run(run_id)
    if run is None:
        raise KeyError("命令不存在: %s" % run_id)
    return {"ok": True, "run": run.summary(), "lines": run.lines_after(after)}


def ingest(payload: dict) -> dict:
    """镜像通道：IDE 扩展在集成终端里执行的命令从这里同步进度。

    action=start 建镜像记录；action=line 追加输出；action=finish 收尾。
    """
    action = str(payload.get("action") or "line")
    if action == "start":
        run = _Run(str(payload.get("command") or "(IDE 终端命令)"),
                   str(payload.get("cwd") or ""), "ide",
                   str(payload.get("session_id") or ""), 0, payload.get("meta"))
        run.owner = str(payload.get("owner") or "ide")
        if payload.get("id"):
            run.id = str(payload["id"])
        with _LOCK:
            _RUNS[run.id] = run
            _evict()
        run._emit_run("run")  # noqa: SLF001 — 镜像记录没有子进程，直接发事件
        return {"ok": True, "run": run.summary()}

    run = get_run(str(payload.get("run_id") or payload.get("id") or ""))
    if run is None:
        raise KeyError("镜像命令不存在")
    if action == "line":
        streams = payload.get("lines")
        if isinstance(streams, list):
            for item in streams:
                if isinstance(item, dict):
                    run._emit_line(str(item.get("stream") or "stdout"),  # noqa: SLF001
                                   str(item.get("text") or ""))
                else:
                    run._emit_line("stdout", str(item))  # noqa: SLF001
        else:
            run._emit_line(str(payload.get("stream") or "stdout"),  # noqa: SLF001
                           str(payload.get("text") or ""))
        return {"ok": True, "run": run.summary()}
    with run._lock:  # noqa: SLF001
        run.exit_code = payload.get("exit_code")
        if run.status == "running":
            run.status = "exited" if run.exit_code in (0, None) else "failed"
        run.ended_at = time.time()
    run.done.set()
    run._emit_run()  # noqa: SLF001
    return {"ok": True, "run": run.summary()}


def stats() -> dict:
    with _LOCK:
        return {"runs": len(_RUNS), "events": len(_EVENTS),
                "subscribers": len(_SUBS), "seq": _SEQ[0]}
