"""
Flask 应用：本地网页界面。

只服务本机（默认 127.0.0.1），单用户使用。
后台生成走 webui.jobs 的 worker 线程，进度通过 SSE 推给浏览器。
"""

import json
import logging
import queue
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)

from src.author_profile import (
    FIELDS as AUTHOR_FIELDS,
    build_prompt_section,
    load_profile,
    save_profile,
)
from src.config_loader import (
    LLM_PROVIDERS,
    PROVIDER_KEY_ENV,
    load_config,
    resolve_llm_config,
    set_llm_provider,
    validate_config,
)
from src.skill_loader import all_skill_info
from src.writer import ARTICLE_TYPES, DEFAULT_WRITING_FOCUS

from .history import list_articles, load_article, open_in_explorer, resolve_media
from .jobs import JobQueue

logger = logging.getLogger("blog_pipeline")

# SSE 单次等待多久发一个心跳（秒）
SSE_KEEPALIVE = 15

ARTICLE_TYPE_CHOICES = [
    {"value": "auto", "label": "自动（有材料→课程笔记文，无材料→知识点文）"},
    {"value": "knowledge", "label": ARTICLE_TYPES["knowledge"][0]},
    {"value": "course-note", "label": ARTICLE_TYPES["course-note"][0]},
    {"value": "feature", "label": ARTICLE_TYPES["feature"][0]},
]

# 流水线阶段，给「使用教程」页展示用。顺序与 src/pipeline.py 一致
PIPELINE_STAGES = [
    ("材料读取", "笔记文件直接读；截图走视觉模型转录成文字，数值和表格原样保留"),
    ("文章判型", "自动判定：带材料按课程笔记文写，不带材料按知识点文写"),
    ("大纲生成", "确定标题、结构和插图位置。勾了「先确认大纲」会在这里停下来等你点确认"),
    ("正文写作", "注入 skill 规则、读者定位和作者背景，写出完整 Markdown"),
    ("去 AI 味", "按 Humanizer-zh skill 改写一遍，结构、代码块和插图标记原样保留"),
    ("交付前自检", "数格式标记、量前言长度、扫禁用句式；不合格自动回喂重写一次"),
    ("插图处理", "手动配图模式下，每个插图位置输出一条配图建议"),
    ("封面提示词", "读定稿正文分析内容，自动选视觉风格，输出可直接用的中文提示词（不出图）"),
    ("组装输出", "补齐 frontmatter，把图片或配图建议落地，写出 index.md"),
]


def _llm_info() -> dict:
    """当前写作模型的完整状态，供界面展示和切换。

    每次调用都重新读一次 config.yaml：这样界面上手动改了配置、或者切换过 provider，
    界面显示的和实际用的不会对不上（每个任务开始时也会重新读配置）。
    """
    cfg = load_config()
    llm = resolve_llm_config(cfg)
    llm_node = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}

    return {
        "provider": llm["provider"],
        "label": llm["label"],
        "model": llm["model"],
        "base_url": llm["base_url"],
        "api_key_env": PROVIDER_KEY_ENV.get(llm["provider"], ""),
        "api_key_configured": bool(llm["api_key"]),
        # 旧版配置形状（只有顶层 deepseek: 节）没法用界面切换，界面会提示手动改
        "switchable": bool(llm_node),
        "providers": [
            {
                "key": key,
                "label": spec["label"],
                "model": (llm_node.get(key) or {}).get("model")
                or spec["default_model"],
                "api_key_env": PROVIDER_KEY_ENV.get(key, ""),
                "api_key_configured": bool(
                    (llm_node.get(key) or {}).get("api_key")
                ),
            }
            for key, spec in LLM_PROVIDERS.items()
        ],
    }


def create_app(job_queue: JobQueue = None, config: dict = None) -> Flask:
    """创建 Flask 应用。

    Args:
        job_queue: 任务队列，None 时自己建一个
        config: 配置字典，None 时自动加载
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JSON_AS_ASCII"] = False

    cfg = config or load_config()
    queue_ = job_queue or JobQueue(
        review_timeout=cfg.get("web", {}).get("review_timeout_seconds", 600)
    )
    base_dir = cfg.get("output", {}).get("base_dir", "./output")

    # ---------- 页面 ----------

    @app.get("/")
    def index():
        return render_template("index.html")

    # ---------- 配置 ----------

    @app.get("/api/config")
    def api_config():
        return jsonify({
            "default_category": cfg.get("content", {}).get("default_category", "技术"),
            "default_tags": cfg.get("content", {}).get("default_tags", []),
            "article_types": ARTICLE_TYPE_CHOICES,
            "review_timeout": cfg.get("web", {}).get("review_timeout_seconds", 600),
            "output_base": str(Path(base_dir).resolve()),
            # 现读一次：切换过写作模型之后，这里的提醒要跟着变
            "warnings": validate_config(load_config()),
        })

    @app.get("/api/background")
    def api_background():
        """写作背景：用了哪些 skill、怎么判型、流水线怎么走、作者是谁。"""
        profile = load_profile()
        image = cfg.get("image", {})
        vision = cfg.get("vision", {})
        content = cfg.get("content", {})
        # 写作模型这块每次现读，切换 provider 之后界面不会显示成旧的
        llm = _llm_info()

        return jsonify({
            "skills": all_skill_info(cfg),
            "article_types": [
                {"key": k, "label": v[0], "rule": v[1]}
                for k, v in ARTICLE_TYPES.items()
            ],
            "stages": [{"name": n, "desc": d} for n, d in PIPELINE_STAGES],
            "llm": llm,
            "models": {
                "writing": f"{llm['label']} / {llm['model']}",
                "vision": f"{vision.get('provider', '')} / "
                          f"{vision.get(vision.get('provider', ''), {}).get('model', '')}",
                "image": image.get("provider", ""),
            },
            "writing_focus": content.get(
                "writing_focus", DEFAULT_WRITING_FOCUS
            ).strip(),
            "author": profile,
            "author_fields": AUTHOR_FIELDS,
            "author_prompt_preview": build_prompt_section(profile),
        })

    @app.post("/api/llm-provider")
    def api_set_llm_provider():
        """切换写作模型（DeepSeek / OpenAI），写回 config.yaml 的 llm.provider。

        只改配置里那一行，其余内容和注释原样保留。下一个任务开始时会重新读配置，
        所以切换后不需要重启服务。
        """
        payload = request.get_json(silent=True) or {}
        provider = (payload.get("provider") or "").strip().lower()
        try:
            set_llm_provider(provider)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except (FileNotFoundError, RuntimeError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except OSError as e:
            logger.exception("切换写作模型失败")
            return jsonify({"ok": False, "error": f"写入配置失败: {e}"}), 500

        info = _llm_info()
        logger.info("写作模型已切换为 %s (%s)", info["label"], info["model"])
        return jsonify({
            "ok": True,
            "llm": info,
            "warnings": validate_config(load_config()),
        })

    @app.post("/api/author")
    def api_save_author():
        """保存写作者背景。字段全空就等于不注入，文章按默认方式写。"""
        payload = request.get_json(silent=True) or {}
        profile = save_profile(payload)
        logger.info("写作者背景已更新")
        return jsonify({
            "ok": True,
            "author": profile,
            "author_prompt_preview": build_prompt_section(profile),
        })

    @app.post("/api/pick-path")
    def api_pick_path():
        """弹原生文件/目录选择框。

        浏览器出于安全拿不到本地绝对路径，所以选择动作放在服务端做。
        """
        payload = request.get_json(silent=True) or {}
        kind = payload.get("kind", "file")
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                if kind == "dir":
                    path = filedialog.askdirectory(title="选择截图或笔记所在目录")
                else:
                    path = filedialog.askopenfilename(
                        title="选择笔记文件",
                        filetypes=[
                            ("笔记文件", "*.md *.markdown *.txt"),
                            ("所有文件", "*.*"),
                        ],
                    )
            finally:
                root.destroy()

            return jsonify({"ok": True, "path": path or ""})
        except Exception as e:
            logger.exception("打开文件选择框失败")
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})

    # ---------- 任务 ----------

    @app.post("/api/jobs")
    def api_submit_job():
        payload = request.get_json(silent=True) or {}
        topic = (payload.get("topic") or "").strip()
        if not topic:
            return jsonify({"ok": False, "error": "请先填写题材"}), 400

        materials = payload.get("materials") or []
        materials = [m.strip() for m in materials if isinstance(m, str) and m.strip()]

        # 材料路径先检查存在性，省得排到队里才发现写错
        missing = [m for m in materials if not Path(m).expanduser().exists()]
        if missing:
            return jsonify({
                "ok": False,
                "error": "这些材料路径不存在：" + "、".join(missing),
            }), 400

        params = {
            "topic": topic,
            "category": (payload.get("category") or "").strip() or None,
            "tags": [t.strip() for t in (payload.get("tags") or []) if t.strip()] or None,
            "materials": materials,
            "article_type": payload.get("article_type", "auto"),
            "fast": bool(payload.get("fast", False)),
            "review": bool(payload.get("review", False)),
            "humanize": not bool(payload.get("no_humanize", False)),
            "selfcheck": not bool(payload.get("no_selfcheck", False)),
            "no_images": bool(payload.get("no_images", False)),
        }

        job = queue_.submit(params)
        logger.info("已提交任务 %s: %s", job.id, topic)
        return jsonify({"ok": True, "job": job.snapshot()})

    @app.get("/api/jobs")
    def api_list_jobs():
        return jsonify({"ok": True, "jobs": queue_.list()})

    @app.get("/api/jobs/<job_id>")
    def api_get_job(job_id):
        job = queue_.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        since = request.args.get("since", type=int, default=0)
        snap = job.snapshot()
        snap["events"] = job.events_since(since)
        return jsonify({"ok": True, "job": snap})

    @app.get("/api/jobs/<job_id>/events")
    def api_job_events(job_id):
        """SSE 进度流。断开只影响订阅，不影响任务本身。

        任务跑到终态后必须把流关掉。浏览器对同一个源大约只允许 6 个并发连接，
        流不关就会一直占着，攒够几个跑完的任务之后，后续的 fetch 全部失败
        （前端表现是「加载失败：Failed to fetch」）。服务端也会白占一个线程。
        """
        job = queue_.get(job_id)
        if job is None:
            abort(404)

        since = request.args.get("since", type=int, default=0)

        def stream():
            # 先补发历史事件，页面刷新后不会空白
            for ev in job.events_since(since):
                yield _sse(ev)

            # 补发时如果任务已经结束，直接收流
            if job.is_terminal():
                yield _sse({"kind": "state", "stage": "stream",
                            "status": job.status, "message": "流已结束"})
                return

            sub = job.subscribe()
            try:
                while True:
                    try:
                        ev = sub.get(timeout=SSE_KEEPALIVE)
                    except queue.Empty:
                        # 心跳，顺便探测连接是否还活着
                        yield ": keepalive\n\n"
                        # 期间任务可能已经结束（比如取消），这里再确认一次
                        if job.is_terminal():
                            yield _sse({"kind": "state", "stage": "stream",
                                        "status": job.status,
                                        "message": "流已结束"})
                            return
                        continue

                    yield _sse(ev)

                    # 终态事件已经送达，收流
                    if ev.get("kind") == "state" and job.is_terminal():
                        return
            except GeneratorExit:
                return
            finally:
                job.unsubscribe(sub)

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/jobs/<job_id>/confirm")
    def api_confirm(job_id):
        job = queue_.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        payload = request.get_json(silent=True) or {}
        decision = bool(payload.get("continue", False))
        job.resolve_confirm(decision)
        job.emit({
            "kind": "review",
            "stage": "outline",
            "level": "info",
            "message": "已确认，继续写正文" if decision else "已取消本次生成",
        })
        return jsonify({"ok": True})

    @app.post("/api/jobs/<job_id>/cancel")
    def api_cancel(job_id):
        job = queue_.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        job.request_cancel()
        job.emit({
            "kind": "state",
            "stage": "queue",
            "level": "warn",
            "message": "已请求取消，将在当前阶段结束后生效",
        })
        return jsonify({"ok": True})

    # ---------- 历史 ----------

    @app.get("/api/history")
    def api_history():
        return jsonify({"ok": True, "articles": list_articles(base_dir)})

    @app.get("/api/history/<path:dir_name>")
    def api_history_detail(dir_name):
        data = load_article(base_dir, dir_name)
        if data is None:
            return jsonify({"ok": False, "error": "找不到这篇文章"}), 404
        return jsonify({"ok": True, "article": data})

    @app.get("/media/<path:dir_name>/<filename>")
    def media(dir_name, filename):
        path = resolve_media(base_dir, dir_name, filename)
        if path is None:
            abort(404)
        return send_file(str(path))

    @app.post("/api/open-folder")
    def api_open_folder():
        payload = request.get_json(silent=True) or {}
        target = payload.get("path")
        if target:
            # 只允许打开输出根目录下的内容
            allowed = Path(base_dir).resolve()
            p = Path(target).resolve()
            if not p.is_relative_to(allowed):
                return jsonify({"ok": False, "error": "只允许打开输出目录内的路径"}), 403
        else:
            p = Path(base_dir).resolve()

        ok = open_in_explorer(str(p))
        return jsonify({"ok": ok, "path": str(p)})

    return app


def _sse(event: dict) -> str:
    """把一条事件编码成 SSE 帧。data 里必须是单行 JSON。"""
    payload = json.dumps(event, ensure_ascii=False)
    return f"data: {payload}\n\n"
