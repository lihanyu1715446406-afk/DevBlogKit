#!/usr/bin/env python3
"""
博客自动生产流水线 — 命令行入口

实际的编排逻辑在 src/pipeline.py。这里只负责把命令行参数转成调用，
并把进度事件渲染成终端文案、把大纲确认接成 input()。

流程：材料读取 → 文章判型 → 大纲 →（可选确认）→ 正文 → 去 AI 味
      → 交付前自检（不合格回喂重写一次）→ 插图 → 封面提示词 → 组装输出

用法:
    # 日常：输入题材，自动生成文章 + 插图 + 封面提示词
    python run.py "Java 21 虚拟线程深入解析"

    # 指定分类和标签
    python run.py "xxx" --category "Java" --tags "Java, 并发, 性能"

    # 带笔记或截图材料（自动判型为课程笔记文）
    python run.py "RabbitMQ 入门" -m ./笔记.md -m ./截图目录

    # 大纲先给你确认，确认后再写正文
    python run.py "xxx" --review

    # 跳过大纲阶段，直接生成（更快）
    python run.py "xxx" --fast

    # 跳过插图生成（纯文本 + 配图建议）
    python run.py "xxx" --no-images

    # 跳过「去 AI 味」改写
    python run.py "xxx" --no-humanize

    # 跳过交付前自检
    python run.py "xxx" --no-selfcheck

    # 跳过封面提示词生成
    python run.py "xxx" --no-cover-prompt
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 确保项目根目录在 Python path 中
sys.path.insert(0, str(Path(__file__).parent))

from src.config_loader import load_config, validate_config
from src.pipeline import ProgressEvent, run_pipeline

logger = logging.getLogger("blog_pipeline")


def _banner(text: str):
    print(f"\n{'=' * 50}")
    print(f"  {text}")
    print(f"{'=' * 50}\n")


def _terminal_printer(event: ProgressEvent):
    """把进度事件渲染回原来的终端文案。"""
    if event.banner:
        _banner(event.message)
    else:
        print(event.message)


async def _ask_outline(outline: str) -> bool:
    """命令行的大纲确认。stdin 关闭时按「继续」处理，避免卡死。"""
    try:
        answer = input("  继续写正文？[Y/n] ").strip().lower()
    except EOFError:
        answer = ""
    return answer not in ("n", "no", "否")


async def run_pipeline_cli(
    topic: str,
    *,
    category: str = None,
    tags: list[str] = None,
    output_dir: str = None,
    fast: bool = False,
    no_images: bool = False,
    humanize: bool = True,
    materials: list[str] = None,
    article_type: str = "auto",
    review: bool = False,
    selfcheck: bool = True,
    cover_prompt: bool = True,
):
    """命令行版本的流水线调用。

    Returns:
        成功时返回 index.md 路径；大纲被否时返回 None（与旧行为一致）
    """
    result = await run_pipeline(
        topic,
        category=category,
        tags=tags,
        output_dir=output_dir,
        fast=fast,
        no_images=no_images,
        humanize=humanize,
        materials=materials,
        article_type=article_type,
        review=review,
        selfcheck=selfcheck,
        cover_prompt=cover_prompt,
        on_progress=_terminal_printer,
        confirm_outline=_ask_outline if review else None,
    )

    if result.get("cancelled"):
        return None
    return result.get("output_path")


def main():
    parser = argparse.ArgumentParser(
        description="博客自动生产流水线 — 输入题材（可选带笔记/截图），输出 Markdown 文章 + 插图",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run.py "Java 虚拟线程实战"
  python run.py "MySQL 索引优化指南" --category "数据库" --tags "MySQL, 优化"
  python run.py "RabbitMQ 入门" -m ./笔记.md -m ./截图目录 --review
        """,
    )

    parser.add_argument(
        "topic",
        nargs="?",
        help="文章题材（一句话或一段描述）",
    )
    parser.add_argument(
        "--material",
        "-m",
        action="append",
        default=None,
        metavar="PATH",
        help="笔记文件或截图目录，可重复传入",
    )
    parser.add_argument(
        "--type",
        default="auto",
        choices=["auto", "knowledge", "course-note", "feature"],
        help="文章类型（默认 auto：有材料按课程笔记文，无材料按知识点文）",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="先输出大纲等你确认，确认后再写正文",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="文章分类（如 Java、后端架构）",
    )
    parser.add_argument(
        "--tags",
        default=None,
        help="文章标签，逗号分隔（如 'Java,并发'）",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="输出目录（默认自动生成: output/日期-标题/）",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="跳过大纲阶段，直接生成文章（速度更快）",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="跳过插图生成，仅输出纯文本 + 配图建议",
    )
    parser.add_argument(
        "--no-humanize",
        action="store_true",
        help="跳过去 AI 味改写，直接输出原始文章",
    )
    parser.add_argument(
        "--no-selfcheck",
        action="store_true",
        help="跳过交付前自检",
    )
    parser.add_argument(
        "--no-cover-prompt",
        action="store_true",
        help="跳过封面提示词生成（不做内容分析和风格选择）",
    )

    args = parser.parse_args()

    # 初始化日志（命令行下保留控制台 + 文件双写）
    try:
        config = load_config()
        log_config = config.get("logging", {})
        from src.utils import setup_logging

        setup_logging(
            level=log_config.get("level", "INFO"),
            log_file=log_config.get("file"),
        )
    except FileNotFoundError:
        from src.utils import setup_logging

        setup_logging("INFO")

    # 检查配置
    if args.topic:
        try:
            config = load_config()
            warnings = validate_config(config)
            if warnings:
                print("\n⚠️ 配置提醒:")
                for w in warnings:
                    print(f"  - {w}")
                print()
        except FileNotFoundError:
            pass

    # 路由命令
    if args.topic:
        tags = None
        if args.tags:
            tags = [t.strip() for t in args.tags.split(",") if t.strip()]

        asyncio.run(
            run_pipeline_cli(
                topic=args.topic,
                category=args.category,
                tags=tags,
                output_dir=args.output,
                fast=args.fast,
                no_images=args.no_images,
                humanize=not args.no_humanize,
                materials=args.material,
                article_type=args.type,
                review=args.review,
                selfcheck=not args.no_selfcheck,
                cover_prompt=not args.no_cover_prompt,
            )
        )
    else:
        parser.print_help()
        print("\n请提供文章题材，例如:")
        print('  python run.py "Java 21 虚拟线程深入解析"')


if __name__ == "__main__":
    main()
