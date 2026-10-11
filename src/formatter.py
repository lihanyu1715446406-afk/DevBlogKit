"""
组装输出：将文章 + 插图合并为最终 Markdown 文件。
"""

import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

from .utils import title_to_slug, write_file

logger = logging.getLogger("blog_pipeline")

# 配图建议的说明文字。原因不同，读者该做的事也不同，
# 所以不能所有情况都写"插图数量已达上限"。
_SUGGESTION_REASON = {
    "manual": "请手动配图",
    "no_api": "图片 API 未配置，请手动配图",
    "cap": "插图数量已达上限，请手动补充",
    "failed": "图片生成失败，请手动补充",
    "disabled": "插图生成已关闭，请手动配图",
}


def _suggestion_block(desc: str, reason: str) -> str:
    """生成一段配图建议引用块。

    前后都要空行：模型有时把 [IMAGE: ...] 标记写在句子中间，
    没有前后空行的话引用块会被当成段落续行，渲染不出来。
    """
    note = _SUGGESTION_REASON.get(reason, _SUGGESTION_REASON["cap"])
    return f"\n\n> 📷 **配图建议：** {desc}\n> *（{note}）*\n\n"


def ensure_frontmatter(
    article: str,
    title: str,
    *,
    category: str = "技术",
    tags: Optional[list[str]] = None,
    date_str: Optional[str] = None,
) -> tuple[str, bool]:
    """保证文章带 YAML frontmatter，缺失时本地补齐。

    模型偶尔会漏掉 frontmatter。title / date / category / tags 在管线里都是已知的，
    本地补齐比回喂给模型重写一次更可靠，也不会顺带改坏正文。
    如果文章自带 h1 标题，就把它移进 frontmatter，避免 CSDN 上标题重复显示。

    Returns:
        (处理后的文章, 是否补了 frontmatter)
    """
    if re.match(r"^\s*---\s*\n", article):
        return article, False

    body = article.lstrip()
    h1_match = re.match(r"^#\s+(.+?)\s*\n", body)
    if h1_match:
        # 文章自带 h1，用它的文字做 title，并去掉这行
        title = h1_match.group(1).strip()
        body = body[h1_match.end():].lstrip()

    date_str = date_str or date.today().strftime("%Y-%m-%d")
    tag_list = tags or []
    tags_yaml = "[" + ", ".join(tag_list) + "]"

    frontmatter = (
        "---\n"
        f"title: {title}\n"
        f"date: {date_str}\n"
        f"category: {category}\n"
        f"tags: {tags_yaml}\n"
        "---\n\n"
    )
    logger.warning("文章缺少 frontmatter，已本地补齐: title=%s", title)
    return frontmatter + body, True


def assemble_output(
    article: str,
    title: str,
    *,
    image_descriptions: list[str] = None,
    cover_path: Optional[str] = None,
    illustration_paths: list[str] = None,
    output_dir: Optional[str] = None,
    suggestion_reason: str = "cap",
    category: str = "技术",
    tags: Optional[list[str]] = None,
) -> str:
    """组装最终的 Markdown 输出文件。

    Args:
        article: 完整的 Markdown 文章（含 frontmatter）
        title: 文章标题
        image_descriptions: 插图描述列表
        cover_path: 封面图路径（相对或绝对）
        illustration_paths: 插图路径列表
        output_dir: 输出目录（默认自动生成）
        suggestion_reason: 未生成图片的原因，决定配图建议里的说明文字。
            取值 no_api / cap / failed / disabled
        category: 分类，仅在文章缺 frontmatter 需要补齐时用到
        tags: 标签，同上

    Returns:
        最终的 Markdown 文件路径
    """
    if output_dir is None:
        today = date.today().strftime("%Y-%m-%d")
        slug = title_to_slug(title)[:50]
        output_dir = f"./output/{today}-{slug}"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 缺 frontmatter 时先补齐，后面插封面图也依赖它
    final_article, _ = ensure_frontmatter(
        article, title, category=category, tags=tags
    )

    # 替换插图标记
    if illustration_paths and image_descriptions:
        for i, (desc, img_path) in enumerate(
            zip(image_descriptions, illustration_paths)
        ):
            if img_path is None:
                # 生成失败，退回成配图建议
                replacement = _suggestion_block(desc, "failed")
            else:
                img_rel = Path(img_path).name
                replacement = f"![{desc}]({img_rel})"

            # 替换第一个 [IMAGE: ...]
            final_article = final_article.replace(
                f"[IMAGE: {desc}]", replacement, 1
            )

    # 处理未被替换的 [IMAGE: ...]（未配图 API / 超出上限）
    def _replace_remaining(match):
        return _suggestion_block(match.group(1), suggestion_reason)
    final_article = re.sub(
        r'\[IMAGE:\s*([^\]]+)\]', _replace_remaining, final_article
    )

    # 在 frontmatter 后面插入封面图
    if cover_path:
        cover_rel = Path(cover_path).name
        final_article = _insert_cover_after_frontmatter(
            final_article, cover_rel
        )

    # 写入文件
    output_file = output_dir / "index.md"
    write_file(str(output_file), final_article.strip() + "\n")

    logger.info("文章已输出到: %s", output_file)
    return str(output_file)


def _insert_cover_after_frontmatter(article: str, cover_filename: str) -> str:
    """在 YAML frontmatter 之后插入封面图。"""
    # 匹配 frontmatter 块
    fm_end = re.search(r'^---\s*$', article, re.MULTILINE)
    if not fm_end:
        # 没有 frontmatter，在开头插入
        title_match = re.match(r'^#\s+.+\n', article)
        if title_match:
            pos = title_match.end()
            return (
                article[:pos]
                + f"\n![封面]({cover_filename})\n\n"
                + article[pos:]
            )
        return f"![封面]({cover_filename})\n\n{article}"

    # 找到第二个 ---（frontmatter 结束）
    second_fm = re.search(
        r'^---\s*$', article[fm_end.end():], re.MULTILINE
    )
    if second_fm:
        insert_pos = fm_end.end() + second_fm.end()
        return (
            article[:insert_pos]
            + f"\n![封面]({cover_filename})\n"
            + article[insert_pos:]
        )

    return article


def save_outline(outline: str, output_dir: str):
    """保存大纲到输出目录（供参考）。"""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    outline_path = Path(output_dir) / "outline.md"
    write_file(str(outline_path), outline)
    logger.info("大纲已保存: %s", outline_path)
