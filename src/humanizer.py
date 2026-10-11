"""
去 AI 味模块：文章写完之后再过一遍 Humanizer 规则，输出更自然的中文。

规则来自 Humanizer-zh skill 的真实文件（见 skill_loader），读不到时回退到
prompts/humanize.txt。作为写作之后的独立阶段运行，不改变文章结构、代码块和插图标记。
"""

import logging
from pathlib import Path
from typing import Optional

from .config_loader import load_config
from .llm_client import LLMClient
from .skill_loader import load_skill
from .text_utils import IMAGE_MARKER_RE, strip_fence
from .utils import write_file

logger = logging.getLogger("blog_pipeline")

# 模型用它分隔「改写后的文章」和「改动清单」
CHANGES_MARKER = "<!-- HUMANIZE-CHANGES -->"


def load_humanize_prompt(config: dict) -> tuple[str, str]:
    """加载去 AI 味提示词。

    Returns:
        (提示词文本, 来源标识 skill|prompts)
    """
    return load_skill("humanizer", config=config)


def _split_changes(raw: str) -> tuple[str, Optional[str]]:
    """把「改写后的文章」和「改动清单」拆开。没有标记时整段当作文章。"""
    if CHANGES_MARKER in raw:
        article, changes = raw.split(CHANGES_MARKER, 1)
        return article.strip(), changes.strip() or None
    return raw.strip(), None


def _validate(
    original: str, rewritten: str, min_length_ratio: float
) -> tuple[bool, str]:
    """检查改写结果能不能用，不能用就回退到原文。

    Returns:
        (是否通过, 未通过的原因)
    """
    if not rewritten or len(rewritten) < 100:
        return False, "改写结果为空或过短"

    if original.lstrip().startswith("---") and not rewritten.lstrip().startswith("---"):
        return False, "frontmatter 丢失"

    before = IMAGE_MARKER_RE.findall(original)
    after = IMAGE_MARKER_RE.findall(rewritten)
    if before != after:
        return False, f"插图标记不一致（{len(before)} → {len(after)}）"

    ratio = len(rewritten) / max(len(original), 1)
    if ratio < min_length_ratio:
        return False, f"改写后字数缩水过多（原文的 {ratio:.0%}）"

    return True, ""


async def humanize_article(article: str, *, config: dict = None) -> dict:
    """对文章做一次去 AI 味改写。

    Args:
        article: 完整 Markdown 文章（含 frontmatter）
        config: 配置字典（默认自动加载）

    Returns:
        {
            "article": str,       # 改写后的文章（失败时是原文）
            "changes": str | None, # 改动清单
            "humanized": bool,    # 是否真的改写了
            "reason": str,        # 未改写时的原因
        }
    """
    config = config or load_config()
    hcfg = config.get("humanize", {})

    client = LLMClient(config)
    prompt_text, skill_source = load_humanize_prompt(config)

    messages = [
        {"role": "system", "content": prompt_text},
        {
            "role": "user",
            "content": (
                "改写时不要提高理解门槛，技术原理和业务实践的内容一个字都不要删。\n\n"
                f"以下是需要改写的文章：\n\n{article}"
            ),
        },
    ]

    logger.info(
        "开始去 AI 味改写 - 原文 %d 字符，规则来源: %s",
        len(article), skill_source,
    )
    raw = await client.chat(
        messages,
        temperature=hcfg.get("temperature", 0.7),
        max_tokens=hcfg.get("max_tokens", 8192),
    )

    rewritten, changes = _split_changes(strip_fence(raw))

    ok, reason = _validate(
        article, rewritten, hcfg.get("min_length_ratio", 0.6)
    )
    if not ok:
        logger.warning("去 AI 味改写被丢弃: %s", reason)
        return {
            "article": article,
            "changes": None,
            "humanized": False,
            "reason": reason,
            "skill_source": skill_source,
        }

    logger.info("去 AI 味完成 - %d → %d 字符", len(article), len(rewritten))
    return {
        "article": rewritten,
        "changes": changes,
        "humanized": True,
        "reason": "",
        "skill_source": skill_source,
    }


def save_humanize_report(changes: str, output_dir: str) -> str:
    """把改动清单写到输出目录，供人工审核。"""
    path = Path(output_dir) / "humanize_changes.md"
    write_file(str(path), "# 去 AI 味改动清单\n\n" + changes.strip() + "\n")
    logger.info("去 AI 味改动清单已保存: %s", path)
    return str(path)
