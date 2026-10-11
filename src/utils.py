"""
工具函数：日志初始化、文件读写、重试装饰器。
"""

import asyncio
import logging
import random
from pathlib import Path
from typing import Optional

from slugify import slugify


def setup_logging(level: str = "INFO", log_file: Optional[str] = None):
    """初始化日志系统。

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
        log_file: 日志文件路径（可选）
    """
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-7s] %(message)s",
        datefmt="%H:%M:%S",
    )

    logger = logging.getLogger("blog_pipeline")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    # 控制台输出
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件输出
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def read_file(file_path: str) -> str:
    """读取文件内容（UTF-8）。"""
    return Path(file_path).read_text(encoding="utf-8")


def write_file(file_path: str, content: str):
    """写入文件（UTF-8），自动创建父目录。"""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def title_to_slug(title: str) -> str:
    """将中文标题转为 URL 友好的 slug。"""
    return slugify(title, allow_unicode=True)


def output_dir_name(date_str: str, title: str) -> str:
    """生成输出目录名: 日期-标题slug"""
    return f"{date_str}-{title_to_slug(title)[:50]}"


def unique_output_dir(base_dir: str, date_str: str, title: str) -> Path:
    """给一篇文章挑一个不冲突的输出目录。

    目录名是「日期-标题slug」，同名同日的两篇会算出同一个路径，
    后一篇会把前一篇的 index.md 覆盖掉。批量生成时很容易撞上，
    所以撞了就在后面加 -2、-3。

    Args:
        base_dir: 输出根目录
        date_str: 日期，形如 2026-10-10
        title: 文章标题

    Returns:
        可用的目录 Path（不创建）
    """
    base = Path(base_dir)
    name = output_dir_name(date_str, title)
    if not (base / name / "index.md").exists():
        return base / name

    for i in range(2, 100):
        candidate = base / f"{name}-{i}"
        if not (candidate / "index.md").exists():
            return candidate

    # 极少见：同名同日的文章超过 99 篇，退回带时分秒的名字
    from datetime import datetime

    return base / f"{name}-{datetime.now().strftime('%H%M%S')}"


async def retry_async(func, max_retries=3, base_delay=1.0):
    """异步重试，指数退避 + 随机抖动。

    Args:
        func: 异步无参函数
        max_retries: 最大重试次数
        base_delay: 基础延迟秒数

    Returns:
        函数返回值

    Raises:
        最后一次尝试的异常
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return await func()
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                logging.getLogger("blog_pipeline").warning(
                    f"第 {attempt + 1} 次尝试失败: {e}，"
                    f"{delay:.1f}s 后重试..."
                )
                await asyncio.sleep(delay)
    raise last_error
