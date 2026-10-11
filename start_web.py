#!/usr/bin/env python3
"""
网页界面启动入口。

直接双击「启动博客生成器.bat」即可，也可以命令行运行 python start_web.py。

这里必须在导入 src.* 和读配置之前把工作目录切到项目根目录：
config.yaml 里的 output.base_dir（./output）和 logging.file（./data/pipeline.log）
都是相对进程 CWD 解析的，双击启动时 CWD 不确定，不切就会把输出散到别处。
"""

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import (  # noqa: E402
    DEFAULT_LOG_LEVEL,
    DEFAULT_REVIEW_TIMEOUT,
    load_config,
    validate_config,
)
from src.utils import setup_logging  # noqa: E402
from webui.app import create_app  # noqa: E402
from webui.jobs import JobQueue, QueueLogHandler  # noqa: E402


def is_port_in_use(port: int) -> bool:
    """端口上是否已经有服务在监听。

    分两步判断，缺一不可：
    1. 先尝试连一下，能连上说明有活的服务
    2. 再尝试 bind。注意这里**不能**设 SO_REUSEADDR——Windows 下设了之后，
       即使端口已被占用也能绑定成功，探测就完全失效了（会导致双击两次启动出两个服务）
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def find_free_port(preferred: int, attempts: int = 20) -> int:
    """从 preferred 开始找一个没被占用的端口。"""
    for offset in range(attempts):
        port = preferred + offset
        if not is_port_in_use(port):
            return port
    raise RuntimeError(f"{preferred} 起连续 {attempts} 个端口都被占用了")


def looks_like_our_app(port: int) -> bool:
    """端口上跑的是不是本程序（而不是别的软件占了端口）。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/config", timeout=1.5
        ) as resp:
            body = resp.read(400).decode("utf-8", "replace")
        return "article_types" in body
    except Exception:
        return False


def main():
    config = load_config()
    web_config = config.get("web", {})

    # 日志只初始化一次。setup_logging 内部会 handlers.clear()，
    # 所以界面用的 Handler 必须在这之后挂，否则会被清掉。
    log_config = config.get("logging", {})
    setup_logging(
        level=log_config.get("level", DEFAULT_LOG_LEVEL),
        log_file=log_config.get("file"),
    )
    import logging

    logging.getLogger("blog_pipeline").addHandler(QueueLogHandler())

    # 配置提醒在控制台打一遍，界面里也会显示
    for w in validate_config(config):
        print(f"  ⚠️ {w}")

    job_queue = JobQueue(
        review_timeout=web_config.get("review_timeout_seconds", DEFAULT_REVIEW_TIMEOUT)
    )
    job_queue.start()

    app = create_app(job_queue=job_queue, config=config)

    host = web_config.get("host", "127.0.0.1")
    preferred = web_config.get("port", 8756)

    # 已经有一个实例在跑就不要再起第二个：重复双击很容易发生，
    # 两个服务同抢一个端口会让请求落到哪个进程变随机，任务列表看着就像丢了。
    if looks_like_our_app(preferred):
        url = f"http://{host}:{preferred}/"
        print()
        print(f"  已经在运行了，直接打开 {url}")
        print("  要重启的话，先关掉原来那个窗口（或结束 python 进程）")
        if web_config.get("open_browser", True):
            webbrowser.open(url)
        return

    port = find_free_port(preferred)
    url = f"http://{host}:{port}/"

    print()
    print("=" * 56)
    print("  博客生成器已启动")
    print(f"  界面地址: {url}")
    print(f"  输出目录: {Path(config['output']['base_dir']).resolve()}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 56)
    print()

    if web_config.get("open_browser", True):
        # 等一秒再开浏览器，确保服务已经起来
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # use_reloader 必须关掉：它会 fork 出第二个进程，
    # 导致日志 Handler 和 worker 线程各起一份、任务被跑两遍。
    app.run(
        host=host,
        port=port,
        threaded=True,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
