"""
交付前自检：格式标记数量、前言长度、frontmatter、禁用句式、敏感词、模板残留、图片存在性。

对应 learning-tech-blog skill“五、交付格式”的格式门禁与“六、完成后自检”。
超限时由调用方决定是否回喂给模型重写（见 build_repair_instruction）。
"""

import logging
import re
from pathlib import Path
from typing import Optional

from .text_utils import FENCE_LINE_RE, IMAGE_MARKER_RE

logger = logging.getLogger("blog_pipeline")

# 门禁阈值。quote / inline_code / intro_chars 是硬性（超了要重写），
# bold 是软性（只在报告里提示，不触发重写）。
DEFAULT_LIMITS = {
    "quote": 2,          # 引用块
    "inline_code": 5,    # 正文内联反引号（不含代码块围栏）
    "bold": 12,          # 加粗片段，软性
    "intro_chars": 120,  # 前言字数
}

# 禁用句式。{"pattern": 正则, "label": 说明, "max_hits": 允许出现次数}
BANNED_PATTERNS = [
    {"pattern": r"不是[^。\n]{0,12}而是", "label": "先抑后扬句式（不是……而是）", "max_hits": 1},
    {"pattern": r"通常不是[^。\n]{0,12}而是", "label": "先抑后扬句式", "max_hits": 0},
    {"pattern": r"往往不是[^。\n]{0,12}而是", "label": "先抑后扬句式", "max_hits": 0},
    {"pattern": r"真正有用的不是", "label": "先抑后扬句式", "max_hits": 0},
    {"pattern": r"表面上看", "label": "先抑后扬句式（表面上看）", "max_hits": 0},
    {"pattern": r"很多人会以为", "label": "错误认知对比", "max_hits": 0},
    {"pattern": r"真正会发现|真正把[^。\n]{0,10}串起来", "label": "AI 空过渡句", "max_hits": 0},
    {"pattern": r"值得单独讲清楚|最值得记住的是|值得单独拿出来讲", "label": "AI 空过渡句", "max_hits": 0},
    {"pattern": r"项目里的做法很清楚", "label": "AI 空过渡句", "max_hits": 0},
    {"pattern": r"你的笔记|我在笔记里|本文根据|根据提供的材料", "label": "作者自述/来源标签", "max_hits": 0},
    {"pattern": r"课堂图|课程资料显示|我看到的图片", "label": "来源标签", "max_hits": 0},
    {"pattern": r"这篇文章我打算|下面我分三步|我这里准备讲", "label": "作者自述句", "max_hits": 0},
    # 注入了写作者背景之后，模型可能开始自报身份或编造个人经历。
    # 正文必须是面向读者的解释，不能变成“作为一个 X 开发者”的自述。
    {"pattern": r"作为(?:一[名个])?(?:后端|前端|全栈|Java|服务端|运维)?开发", "label": "作者身份自述", "max_hits": 0},
    {"pattern": r"我(?:在|做)[^。\n]{0,12}项目(?:里|中)", "label": "作者经历自述", "max_hits": 0},
    {"pattern": r"我(?:自己)?(?:也)?(?:曾经)?(?:踩过|踩了)[^。\n]{0,8}坑", "label": "作者经历自述", "max_hits": 0},
    {"pattern": r"下面是[^。\n]{0,20}的实现|项目里的实现和?[^。\n]{0,10}一致", "label": "代码导读句", "max_hits": 0},
    {"pattern": r"先补充几个\s*import|把以下代码放进|把以下方法加进去", "label": "编辑指令", "max_hits": 0},
    {"pattern": r"综上所述|总而言之|值得注意的是|此外，", "label": "AI 高频过渡词", "max_hits": 0},
    {"pattern": r"赋能|彰显|凸显|无缝|优雅|极致的|为[^。\n]{0,10}奠定基础|标志着|见证了", "label": "AI 高频词/宣传腔", "max_hits": 0},
    {"pattern": r"希望对[你您]有帮助|当然！|你说得非常对", "label": "协作痕迹", "max_hits": 0},
    {"pattern": r"截至\s*\d{4}\s*年|根据目前掌握的信息", "label": "知识截止免责", "max_hits": 0},
    # 直角引号：用户明确要求正文不出现。label 直接写成动作，回喂重写时模型才知道怎么改。
    # hide_match：回喂指令里只报行号、不复现字符本身——把被禁的符号再送到模型眼前，
    # 反而可能让它照抄回来。
    {
        "pattern": r"[「」]",
        "label": "直角引号（改用中文双引号 “ ”）",
        "max_hits": 0,
        "hide_match": True,
    },
]

# 总结尾巴：skill 明确禁止追加这类话题清单。
# 注意这些动词是必须的——只写“后续可以”会误伤正常叙述，
# 比如“把后续可以独立完成、独立重试的步骤交给异步”。
TAIL_PATTERNS = [
    r"后续学习计划",
    r"后续可以(?:继续)?(?:了解|学习|阅读|研究|深入|看看)",
    r"以后还可以(?:了解|学习|阅读|研究|看看)",
    r"接下来可以继续了解",
    r"下一步可以(?:学习|了解|阅读)",
    r"感兴趣的话可以继续",
    r"有兴趣的(?:话)?可以(?:了解|学习)",
]

# 敏感信息，命中即需人工确认（判断是否为已脱敏示例）
SENSITIVE_PATTERNS = [
    r"password\s*[:=]\s*(?!\*)\S+",
    r"secret\s*[:=]\s*(?!\*)\S+",
    r"access-?key[-_]?(?:id|secret)?\s*[:=]\s*(?!\*)\S+",
    r"token\s*[:=]\s*(?!\*)\S+",
    r"appid\s*[:=]\s*(?!\*)\S+",
    r"(?<!\d)1[3-9]\d{9}(?!\d)",
    r"[\w.+-]+@[\w-]+\.[\w.]{2,}",
]

# 模板残留
TEMPLATE_PATTERNS = [r"\{\{", r"\bTODO\b", r"占位符", r"<[a-z_]+_placeholder>"]

_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def split_code_blocks(text: str) -> str:
    """去掉围栏代码块，返回只含正文的文本（用于统计内联反引号）。"""
    body, in_fence = [], False
    for line in text.splitlines():
        if FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            body.append(line)
    return "\n".join(body)


def extract_frontmatter(text: str) -> tuple[Optional[str], str]:
    """拆出 frontmatter 和正文。

    Returns:
        (frontmatter 文本 或 None, 去掉 frontmatter 后的正文)
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None, stripped

    lines = stripped.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm = "\n".join(lines[: i + 1])
            return fm, "\n".join(lines[i + 1:]).lstrip()
    return None, stripped


def extract_intro(body: str) -> str:
    """取前言：正文开头到第一个 h2 之间的部分。"""
    match = re.search(r"^##\s", body, re.MULTILINE)
    intro = body[: match.start()] if match else body
    # 去掉标题行和配图建议块，只留叙述文字
    kept = [
        ln for ln in intro.splitlines()
        if not ln.lstrip().startswith(("#", ">", "!"))
    ]
    return "\n".join(kept).strip()


def _count_matches(patterns: list[str], text: str) -> list[tuple[int, str]]:
    """返回 [(行号, 命中文本), ...]。"""
    hits = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            line_no = text[: m.start()].count("\n") + 1
            hits.append((line_no, m.group(0)))
    return hits


def _count_quotes(body: str) -> int:
    """统计引用块数量。

    formatter 生成的“配图建议”块也是 > 开头，但它们不是作者写的引用，
    算进来会让每篇带配图建议的文章都超标、被反复重写，所以排除掉。
    """
    count = 0
    for line in body.splitlines():
        if not line.lstrip().startswith(">"):
            continue
        stripped = line.lstrip()[1:].strip()
        if "配图建议" in stripped:
            continue
        if re.match(r"^\*（", stripped):  # 配图建议的第二行说明
            continue
        count += 1
    return count


def check_article(
    article: str,
    *,
    limits: Optional[dict] = None,
    base_dir: Optional[str] = None,
) -> dict:
    """对文章做一次交付前自检。

    Args:
        article: 完整 Markdown 文章（可以还带 [IMAGE: ...] 标记）
        limits: 覆盖默认阈值
        base_dir: 文章所在目录；给了就检查相对路径引用的图片是否存在

    Returns:
        {
            "ok": bool,                # 有无硬性问题
            "source": "selfcheck",
            "counts": {...},
            "problems": [{"label": str, "detail": str}, ...],
            "warnings": [...],
            "missing_images": [...],
            "report": str,             # 可直接写入文件的报告
        }
    """
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    body = split_code_blocks(article)
    frontmatter, content = extract_frontmatter(article)

    problems: list[dict] = []
    warnings: list[dict] = []

    # ---- 1. frontmatter ----
    if frontmatter is None:
        problems.append({
            "label": "缺少 YAML frontmatter",
            "detail": "文章不是以 --- 开头且闭合，CSDN 后台无法自动识别标题/分类/标签",
        })
    else:
        for field in ("title", "date", "category", "tags"):
            if not re.search(rf"^{field}\s*:", frontmatter, re.MULTILINE):
                warnings.append({
                    "label": f"frontmatter 缺字段 {field}",
                    "detail": frontmatter.replace("\n", " ")[:80],
                })

    # ---- 2. 格式标记数量 ----
    counts = {
        "quote": _count_quotes(body),
        "bold": len(re.findall(r"\*\*[^*\n]+\*\*", body)),
        "inline_code": len(re.findall(r"`[^`\n]+`", body)),
        "chars": len(re.sub(r"\s", "", article)),
    }
    for key, label in (
        ("quote", "引用块"),
        ("inline_code", "正文内联反引号"),
        ("bold", "加粗片段"),
    ):
        limit = limits[key]
        if counts[key] > limit:
            entry = {
                "label": f"{label}数量超标",
                "detail": f"{counts[key]} 处，阈值 {limit} 处",
            }
            # 加粗是软性约束，只提示不触发重写
            (warnings if key == "bold" else problems).append(entry)

    # ---- 3. 前言长度 ----
    intro = extract_intro(content)
    intro_len = len(re.sub(r"\s", "", intro))
    if intro_len > limits["intro_chars"]:
        problems.append({
            "label": "前言超长",
            "detail": f"{intro_len} 字，上限 {limits['intro_chars']} 字：{intro[:60]}…",
        })

    # ---- 4. 禁用句式 ----
    for item in BANNED_PATTERNS:
        hits = _count_matches([item["pattern"]], body)
        if len(hits) > item["max_hits"]:
            if item.get("hide_match"):
                # 只报行号：复现字符本身会把被禁的符号又送进模型上下文
                spot = "；".join(f"第 {ln} 行" for ln, _ in hits[:3])
            else:
                spot = "；".join(f"第 {ln} 行 {txt!r}" for ln, txt in hits[:3])
            problems.append({
                "label": item["label"],
                "detail": f"出现 {len(hits)} 次（允许 {item['max_hits']} 次）：{spot}",
            })

    # ---- 5. 总结尾巴 ----
    if content:
        tail = content[-500:]
        hits = _count_matches(TAIL_PATTERNS, tail)
        if hits:
            spot = "；".join(f"第 {ln} 行 {txt!r}" for ln, txt in hits[:3])
            problems.append({
                "label": "总结带了话题清单尾巴",
                "detail": f"结尾 500 字内命中：{spot}",
            })

    # ---- 6. 敏感词 / 模板残留 ----
    for label, patterns in (
        ("敏感信息", SENSITIVE_PATTERNS),
        ("模板残留", TEMPLATE_PATTERNS),
    ):
        for line_no, text in _count_matches(patterns, body):
            warnings.append({
                "label": label,
                "detail": f"第 {line_no} 行 {text!r}，确认是否为已脱敏示例",
            })

    # ---- 7. 图片存在性 ----
    missing_images = []
    if base_dir:
        for rel in _IMAGE_MD_RE.findall(article):
            if rel.startswith(("http://", "https://", "data:")):
                continue
            if not (Path(base_dir) / rel).exists():
                missing_images.append(rel)
        if missing_images:
            problems.append({
                "label": "图片引用缺失",
                "detail": "、".join(missing_images),
            })

    report = render_report(
        ok=not problems,
        counts=counts,
        problems=problems,
        warnings=warnings,
        intro_len=intro_len,
        limits=limits,
        has_frontmatter=frontmatter is not None,
        marker_count=len(IMAGE_MARKER_RE.findall(article)),
    )

    return {
        "ok": not problems,
        "source": "selfcheck",
        "counts": counts,
        "problems": problems,
        "warnings": warnings,
        "missing_images": missing_images,
        "intro_len": intro_len,
        "report": report,
    }


def render_report(
    *,
    ok: bool,
    counts: dict,
    problems: list[dict],
    warnings: list[dict],
    intro_len: int,
    limits: dict,
    has_frontmatter: bool,
    marker_count: int,
) -> str:
    """生成 Markdown 格式的自检报告。"""
    lines = ["# 交付前自检报告", ""]
    lines.append(f"- 结论：{'通过' if ok else '**未通过**，有硬性问题'}")
    lines.append(f"- 正文字数（不含空白）：{counts['chars']}")
    lines.append(f"- frontmatter：{'有' if has_frontmatter else '**缺失**'}")
    lines.append(f"- 前言字数：{intro_len}（上限 {limits['intro_chars']}）")
    lines.append(f"- 待生成插图标记：{marker_count} 处")
    lines.append("")

    lines.append("## 格式标记")
    lines.append("")
    lines.append("| 标记 | 数量 | 阈值 | 类型 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(
        f"| 引用块 | {counts['quote']} | {limits['quote']} | 硬性 |"
    )
    lines.append(
        f"| 内联反引号 | {counts['inline_code']} | {limits['inline_code']} | 硬性 |"
    )
    lines.append(
        f"| 加粗片段 | {counts['bold']} | {limits['bold']} | 软性 |"
    )
    lines.append("")

    if problems:
        lines.append("## 硬性问题（需修正）")
        lines.append("")
        for p in problems:
            lines.append(f"- **{p['label']}**：{p['detail']}")
        lines.append("")

    if warnings:
        lines.append("## 提示项（人工确认）")
        lines.append("")
        for w in warnings:
            lines.append(f"- {w['label']}：{w['detail']}")
        lines.append("")

    if not problems and not warnings:
        lines.append("禁用句式、敏感词、模板残留均无命中。")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def build_repair_instruction(result: dict, limits: Optional[dict] = None) -> str:
    """把自检问题转成回喂给模型的重写指令。

    只列需要改的点，并明确要求不要动其他内容，避免重写时引入新问题。
    加粗是软性约束，但严重超标（超过阈值两倍）时也一并提出，
    因为它是最显眼的 AI 痕迹之一。
    """
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    lines = [
        "以下文章没有通过交付前自检，请只针对列出的问题做最小修改，",
        "然后输出修改后的完整文章：",
        "",
    ]
    for p in result["problems"]:
        lines.append(f"- {p['label']}：{p['detail']}")

    bold = result["counts"].get("bold", 0)
    bold_over = bold > limits["bold"] * 2
    if bold_over:
        lines.append(
            f"- 加粗片段数量偏多：{bold} 处，建议压到 {limits['bold']} 处以内。"
            f"只保留关键结论、步骤名和易错提醒，普通技术名词不要加粗"
        )

    lines += [
        "",
        "修改要求：",
        "1. 只改上面列出的问题，不要改技术内容、章节结构、代码块和 [IMAGE: ...] 标记",
        "2. frontmatter 必须保留并放在文件最开头（--- 包裹）",
        "3. 前言必须合并成一整段（第一个 ## 之前只有这一段），2-3 句话，"
        "压到 100 字以内（上限是 120，要到 100 以内才算稳妥）。"
        "把原来开篇的场景句压缩成第一句，后面接一两句概括本文覆盖什么、"
        "这些内容怎么串成主线，不要拆成“场景段 + 引言段”",
        "4. 保持现在这种自然的中文表达，不要引入套话",
        "5. 直接输出完整 Markdown 文章，不要用 ``` 包裹，不要输出解释或改动说明",
    ]
    return "\n".join(lines)


def save_report(report: str, output_dir: str, name: str = "selfcheck.md") -> str:
    """把自检报告写入输出目录。"""
    path = Path(output_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    logger.info("自检报告已保存: %s", path)
    return str(path)
