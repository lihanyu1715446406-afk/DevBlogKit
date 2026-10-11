"""
历史文章管理：扫描 output/ 目录，读取已生成的文章和报告。

列表页只需要少量元数据，所以只读每个文件的开头一小段（frontmatter 和自检结论都在开头），
不整篇读进来。正文只在点开某篇时才读。
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("blog_pipeline")

# 列表页只读文件开头这么多字节
HEAD_BYTES = 2048

IMAGE_RE = re.compile(r"^img_\d+\.png$")
ARTICLE_FILE = "index.md"


def _read_head(path: Path, size: int = HEAD_BYTES) -> str:
    """只读文件开头一段，读不到返回空串。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(size)
    except OSError:
        return ""


def _parse_frontmatter(text: str) -> dict:
    """从 Markdown 开头解析 YAML frontmatter 里的 title/date/category/tags。

    只做简单取值，不引入 yaml 依赖去解析半个文件。
    """
    out: dict = {}
    if not text.lstrip().startswith("---"):
        return out

    block = text.lstrip()[3:]
    end = block.find("\n---")
    if end == -1:
        return out
    block = block[:end]

    for line in block.splitlines():
        m = re.match(r"^(title|date|category|tags)\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key == "tags":
            inner = value.strip().strip("[]")
            out[key] = [t.strip().strip("'\"") for t in inner.split(",") if t.strip()]
        else:
            out[key] = value.strip().strip("'\"")
    return out


def _parse_selfcheck(text: str) -> dict:
    """解析 selfcheck.md 开头的「## 生成信息」块和结论行。"""
    out: dict = {"ok": None, "article_type": "", "skill_source": "",
                 "suggestion_reason": "", "illustration_count": ""}

    m = re.search(r"^-\s*结论：\s*(.+)$", text, re.MULTILINE)
    if m:
        out["ok"] = "未通过" not in m.group(1)

    for field, key in (
        ("文章类型", "article_type"),
        ("skill 来源", "skill_source"),
        ("配图建议原因", "suggestion_reason"),
        ("已生成插图", "illustration_count"),
    ):
        mm = re.search(rf"^-\s*{field}：\s*(.+)$", text, re.MULTILINE)
        if mm:
            out[key] = mm.group(1).strip()
    return out


def resolve_in_base(base_dir: str, dir_name: str) -> Optional[Path]:
    """把 dir_name 安全地解析成 base_dir 下的目录。

    挡掉 ../ 和绝对路径这类越界输入。解析失败或越界都返回 None。
    """
    base = Path(base_dir).resolve()
    try:
        candidate = (base / dir_name).resolve()
    except (OSError, ValueError):
        return None
    if candidate == base or not candidate.is_relative_to(base):
        return None
    if not candidate.is_dir():
        return None
    return candidate


def resolve_media(base_dir: str, dir_name: str, filename: str) -> Optional[Path]:
    """解析图片文件名，只允许目录下真实存在的图片。"""
    if not re.match(r"^[A-Za-z0-9_.\-]+$", filename):
        return None
    if not re.search(r"\.(png|jpg|jpeg|gif|webp|bmp)$", filename, re.IGNORECASE):
        return None
    d = resolve_in_base(base_dir, dir_name)
    if d is None:
        return None
    target = (d / filename).resolve()
    if not target.is_file() or not target.is_relative_to(d):
        return None
    return target


def list_articles(base_dir: str) -> list[dict]:
    """列出所有已生成的文章，按修改时间倒序。"""
    base = Path(base_dir)
    if not base.is_dir():
        return []

    items: list[dict] = []
    for entry in os.scandir(base):
        if not entry.is_dir():
            continue
        # 跳过隐藏目录和 _ 开头的临时/测试目录
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue

        d = Path(entry.path)
        article = d / ARTICLE_FILE
        if not article.is_file():
            continue

        head = _read_head(article)
        meta = _parse_frontmatter(head)

        # 自检信息在 selfcheck.md 开头
        sc = _parse_selfcheck(_read_head(d / "selfcheck.md"))

        # 图片数量只数文件名，不读内容
        image_count = 0
        for f in os.scandir(d):
            if f.is_file() and IMAGE_RE.match(f.name):
                image_count += 1

        title = meta.get("title") or entry.name
        date = meta.get("date") or entry.name[:10]

        items.append({
            "dir": entry.name,
            "title": title,
            "date": date,
            "category": meta.get("category", ""),
            "tags": meta.get("tags", []),
            "article_type": sc["article_type"],
            "selfcheck_ok": sc["ok"],
            "suggestion_reason": sc["suggestion_reason"],
            "image_count": image_count,
            "has_cover": (d / "cover.png").is_file(),
            "has_changes": (d / "humanize_changes.md").is_file(),
            "has_outline": (d / "outline.md").is_file(),
            "mtime": entry.stat().st_mtime,
        })

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def _render(text: str) -> str:
    """把 Markdown 渲染成 HTML。"""
    try:
        import markdown
    except ImportError:
        # 没装 markdown 就退回转义后的纯文本，界面至少能看
        import html

        return f"<pre class='raw'>{html.escape(text)}</pre>"

    return markdown.markdown(
        text, extensions=["fenced_code", "tables", "sane_lists", "nl2br"]
    )


def _rewrite_image_src(text: str, dir_name: str) -> str:
    """把文章里的相对图片引用改写成 /media/<dir>/<file>，预览才能显示。"""
    def repl(m):
        alt, src = m.group(1), m.group(2)
        if src.startswith(("http://", "https://", "data:", "/")):
            return m.group(0)
        return f"![{alt}](/media/{dir_name}/{src})"

    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", repl, text)


def load_article(base_dir: str, dir_name: str) -> Optional[dict]:
    """读取某一篇的正文和随附报告。

    Returns:
        {"meta": {...}, "article_html": ..., "article_raw": ...,
         "selfcheck_md": ..., "selfcheck_html": ...,
         "changes_md": ..., "changes_html": ...,
         "outline_md": ..., "outline_html": ...}
        目录不存在时返回 None
    """
    d = resolve_in_base(base_dir, dir_name)
    if d is None:
        return None

    def read(name: str) -> str:
        p = d / name
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    article_raw = read(ARTICLE_FILE)
    selfcheck_md = read("selfcheck.md")
    changes_md = read("humanize_changes.md")
    outline_md = read("outline.md")

    preview_raw = _rewrite_image_src(article_raw, dir_name)

    images = sorted(
        f.name for f in os.scandir(d)
        if f.is_file() and IMAGE_RE.match(f.name)
    )

    return {
        "dir": dir_name,
        "title": _parse_frontmatter(article_raw).get("title") or dir_name,
        "article_raw": article_raw,
        "article_html": _render(preview_raw),
        "selfcheck_md": selfcheck_md,
        "selfcheck_html": _render(selfcheck_md) if selfcheck_md else "",
        "changes_md": changes_md,
        "changes_html": _render(changes_md) if changes_md else "",
        "outline_md": outline_md,
        "outline_html": _render(outline_md) if outline_md else "",
        "images": images,
        "has_cover": (d / "cover.png").is_file(),
        "abs_path": str(d),
    }


def open_in_explorer(path: str) -> bool:
    """在文件管理器里打开目录（Windows 用 os.startfile）。"""
    p = Path(path)
    if not p.exists():
        return False
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            import subprocess
            import sys

            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(p)])
        return True
    except Exception:
        logger.exception("打开目录失败: %s", path)
        return False
