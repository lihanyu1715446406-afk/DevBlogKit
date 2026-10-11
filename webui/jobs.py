"""
任务队列与后台执行。

一个 worker 线程顺序执行任务，每个任务在自己的线程和自己的事件循环里跑 asyncio。

为什么用线程而不是在 Flask 里直接跑：
`src/llm_client.py` 在 async 函数里调用同步的 OpenAI 客户端，会阻塞它所在的事件循环
整个网络往返（超时默认 120 秒）。把整条流水线放进自己的线程和自己的 loop，这个阻塞就只影响
那一个任务，Flask 的请求线程和 SSE 推送完全不受影响。

进度事件是 JSON 可序列化的 dict，每个任务自带单调递增的 seq，浏览器断线重连可以按 since 补齐。
"""

import asyncio
import logging
import queue
import threading
import time
import uuid
from typing import Optional

from src.pipeline import ProgressEvent, run_pipeline

logger = logging.getLogger("blog_pipeline")

# 每个任务保留的事件条数上限，防止长任务把内存吃满
MAX_EVENTS_PER_JOB = 2000

# 大纲确认的默认等待时间（秒）。超时按取消处理，不继续烧 token
DEFAULT_REVIEW_TIMEOUT = 600

# 终态：任务不会再产生新事件了。SSE 流看到终态就该关掉
TERMINAL_STATUSES = {"done", "failed", "cancelled"}

# 用线程局部变量标记「当前正在跑哪个任务」，
# 日志 Handler 靠它把 logger 的输出认领到正确的任务上
_current_job = threading.local()


class QueueLogHandler(logging.Handler):
    """把 blog_pipeline 的日志记录转发到当前任务的事件流。

    必须在 setup_logging() 之后挂载，因为 setup_logging 会先 handlers.clear()。
    """

    def __init__(self):
        super().__init__()
        self.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)-7s] %(message)s",
                              datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord):
        job = getattr(_current_job, "job", None)
        if job is None:
            # 不在任何任务上下文里（启动期、路由里的日志），不往界面推
            return
        try:
            job.emit({
                "kind": "log",
                "stage": "log",
                "level": record.levelname.lower(),
                "message": self.format(record),
            })
        except Exception:
            self.handleError(record)


class Job:
    """一个生成任务。"""

    def __init__(self, params: dict, review_timeout: int = DEFAULT_REVIEW_TIMEOUT):
        self.id = uuid.uuid4().hex[:12]
        self.params = params
        self.review_timeout = review_timeout
        # queued | running | awaiting_confirm | done | failed | cancelled
        self.status = "queued"
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.created_at = time.time()
        self.finished_at: Optional[float] = None

        self._seq = 0
        self._events: list = []
        self._lock = threading.RLock()
        self._subscribers: list[queue.Queue] = []
        self._confirm_event = threading.Event()
        self._decision: Optional[bool] = None
        self._cancel_requested = False

    # ---------- 事件 ----------

    def emit(self, event: dict):
        """记一条事件并推给所有订阅者。"""
        event = dict(event)
        event.setdefault("ts", time.time())
        with self._lock:
            self._seq += 1
            event["seq"] = self._seq
            self._events.append(event)
            if len(self._events) > MAX_EVENTS_PER_JOB:
                del self._events[: len(self._events) - MAX_EVENTS_PER_JOB]
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 订阅者消费不过来就丢，不能反过来卡住生成
                pass

    def progress_callback(self):
        """返回给流水线用的 on_progress 回调。"""

        def on_progress(pe: ProgressEvent):
            self.emit({
                "kind": "progress",
                "stage": pe.stage,
                "level": pe.level,
                "message": pe.message,
                "banner": pe.banner,
                "raw": pe.raw,
                "data": pe.data,
            })

        return on_progress

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def events_since(self, since: int = 0) -> list:
        with self._lock:
            return [e for e in self._events if e["seq"] > since]

    # ---------- 大纲确认 ----------

    async def confirm_outline(self, outline: str) -> bool:
        """等前端确认大纲。

        会阻塞当前 worker 线程的事件循环，这是可以接受的：
        这个 loop 上只有这一个任务，而等待期间本来也没别的事要跑。
        Flask 的线程不受影响。

        Returns:
            True 继续写正文，False 取消
        """
        with self._lock:
            self._confirm_event.clear()
            self._decision = None
            self.status = "awaiting_confirm"

        self.emit({
            "kind": "review",
            "stage": "outline",
            "level": "info",
            "message": "等待确认大纲…",
            "need_confirm": True,
            "outline": outline,
            "timeout": self.review_timeout,
        })

        got = self._confirm_event.wait(self.review_timeout)

        with self._lock:
            self.status = "running"

        if not got:
            self.emit({
                "kind": "review",
                "stage": "outline",
                "level": "warn",
                "message": f"  ⚠️ 等大纲确认超过 {self.review_timeout} 秒，按取消处理",
            })
            return False

        if self._cancel_requested:
            return False

        return bool(self._decision)

    def resolve_confirm(self, decision: bool):
        """前端点了「继续」或「取消」。"""
        with self._lock:
            self._decision = bool(decision)
        self._confirm_event.set()

    # ---------- 取消 ----------

    def request_cancel(self):
        with self._lock:
            self._cancel_requested = True
        # 如果正卡在大纲确认上，顺手解除等待
        self._confirm_event.set()

    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def is_terminal(self) -> bool:
        """任务是否已经跑到终态（不会再产生新事件）。"""
        return self.status in TERMINAL_STATUSES

    # ---------- 对外快照 ----------

    def snapshot(self) -> dict:
        with self._lock:
            last_seq = self._seq
            return {
                "id": self.id,
                "status": self.status,
                "params": self.params,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "result": self.result,
                "last_seq": last_seq,
                "event_count": len(self._events),
            }


class JobQueue:
    """顺序执行的任务队列，用一个后台线程跑。"""

    def __init__(self, review_timeout: int = DEFAULT_REVIEW_TIMEOUT):
        self.review_timeout = review_timeout
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._running: Optional[str] = None

    def submit(self, params: dict) -> Job:
        job = Job(params, review_timeout=self.review_timeout)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        job.emit({
            "kind": "meta",
            "stage": "queue",
            "level": "info",
            "message": f"任务已排队：{params.get('topic', '')}",
        })
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._lock:
            ids = list(self._order)
        out = []
        for jid in reversed(ids):  # 新的在前
            job = self.get(jid)
            if job:
                out.append(job.snapshot())
        return out

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._run_forever, name="blog-worker", daemon=True
        )
        self._worker.start()
        logger.info("后台 worker 已启动")

    # ---------- worker ----------

    def _run_forever(self):
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue

            if job.cancel_requested():
                job.status = "cancelled"
                job.finished_at = time.time()
                job.emit({
                    "kind": "state",
                    "stage": "queue",
                    "level": "warn",
                    "message": "任务在开始前已被取消",
                    "status": job.status,
                })
                continue

            with self._lock:
                self._running = job_id
            _current_job.job = job
            job.status = "running"
            job.emit({
                "kind": "state",
                "stage": "queue",
                "level": "info",
                "message": "开始执行",
            })

            try:
                result = asyncio.run(self._run_job(job))
                job.result = {
                    k: v for k, v in result.items()
                    if k in ("output_path", "title", "article_type_name",
                             "selfcheck_ok", "counts", "humanized",
                             "cancelled", "skill_source")
                }
                if result.get("cancelled"):
                    job.status = "cancelled"
                else:
                    job.status = "done"
                job.emit({
                    "kind": "state",
                    "stage": "done",
                    "level": "success" if job.status == "done" else "warn",
                    "message": "任务完成" if job.status == "done" else "任务已取消",
                    "status": job.status,
                    "result": job.result,
                })
            except Exception as e:
                logger.exception("任务执行失败")
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.emit({
                    "kind": "state",
                    "stage": "error",
                    "level": "error",
                    "message": f"任务失败：{job.error}",
                    "status": job.status,
                })
            finally:
                job.finished_at = time.time()
                _current_job.job = None
                with self._lock:
                    self._running = None

    async def _run_job(self, job: Job) -> dict:
        p = job.params
        return await run_pipeline(
            p["topic"],
            category=p.get("category"),
            tags=p.get("tags"),
            output_dir=p.get("output_dir"),
            fast=p.get("fast", False),
            no_images=p.get("no_images", False),
            humanize=p.get("humanize", True),
            materials=p.get("materials") or None,
            article_type=p.get("article_type", "auto"),
            review=p.get("review", False),
            selfcheck=p.get("selfcheck", True),
            on_progress=job.progress_callback(),
            confirm_outline=job.confirm_outline,
            is_cancelled=job.cancel_requested,
        )
