"""
核心写作模块：两阶段写作（大纲 + 正文）。

写作规则来自 learning-tech-blog skill 的真实文件（见 skill_loader），
读不到时回退到 prompts/blog-style.txt。
"""

import logging
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .author_profile import build_prompt_section
from .config_loader import DEFAULT_CATEGORY, DEFAULT_TAGS, load_config
from .llm_client import LLMClient
from .skill_loader import load_skill
from .text_utils import extract_image_descriptions as _extract_image_descriptions
from .text_utils import strip_fence
from .utils import read_file

logger = logging.getLogger("blog_pipeline")

PROJECT_ROOT = Path(__file__).parent.parent
WRITING_TEMPLATE_FILE = PROJECT_ROOT / "prompts" / "writing.txt"


@lru_cache(maxsize=1)
def _writing_template() -> str:
    """读取写作提示词模板。

    改造前这行在**模块导入时**执行：prompts/writing.txt 一旦缺失或被占用，
    import writer 就直接抛异常，整条流水线连启动都起不来。改成用时才读，
    和 cover_prompt / skill_loader / author_profile 的加载方式一致。
    """
    return read_file(str(WRITING_TEMPLATE_FILE))

# 写作取向的兜底值。config.yaml 里 content.writing_focus 没配时用这个
DEFAULT_WRITING_FOCUS = (
    "按具体题材灵活变通，不要预设读者的水平；注重技术原理和业务实践，"
    "原理讲透，例子落在能落地的工程场景上。"
)

# 文章类型：判定后决定结构，避免所有题材都往“项目实战”上套。
#
# 这里只做**派发**，不再并列一套章节结构。改造前每个类型都自带一串
# “A → B → C → D”的结构规定，而 learning-tech-blog 规范里另有五类内容性质的
# 结构说明，两套同时注入同一个 system prompt，模型要在两套之间挑一套。
# 现在改成：结构以规范为准，这里只说清楚“本文属于哪种情况、照着规范里的哪一类写”。
#
# key 不能改：run.py 的 --type、网页的下拉框、pipeline 传给封面模块的类型名都用它。
ARTICLE_TYPES = {
    "knowledge": (
        "知识点文",
        "围绕一个概念、机制、注解、API、配置项或报错讲透，按规范里的“原理解析类”"
        "或“实践教程类”组织——看这篇的重心是讲清一个结构，还是给出能照着做的步骤。"
        "不要为了“项目感”硬凑三层代码，只有知识点本身涉及调用链时才展开。",
    ),
    "course-note": (
        "课程笔记文",
        "以提供笔记和截图的内容为边界展开，按规范里的“学习笔记类”组织："
        "建立知识框架、解释概念之间的关系、对比容易混淆的点、总结结论与复习重点。"
        "一级知识点保持材料里的顺序，材料里没出现的主题一律不写，不新增无关章节。"
        "正文不得出现“笔记中”“课堂图”“根据材料”“我看到的图片”这类来源话术，"
        "来源只放在文末参考资料。",
    ),
    "feature": (
        "项目功能文",
        "围绕一个功能走完整闭环，按规范里的“实践教程类”组织："
        "功能目标 → 接口入参与路径 → Service 业务处理 → Mapper/SQL 落库 → 验证与常见失败点。"
        "接口、配置或调用链必须支撑主线，不能只贴代码不解释参数、返回值和 SQL 条件。",
    ),
}


# 材料区块。数值必须原样引用，因为观察到写作阶段会自作主张换算、
# 四舍五入或重新计算材料里已经给定的数字。
_MATERIAL_HEADER = """以下是原始材料。

写作要求：材料里出现的具体数值、字段名、术语、比例、耗时必须原样使用，
不要自己换算、四舍五入、重新计算或改写成近似值。材料没给出的数字不要编。
材料的先后顺序就是知识点的展开顺序。"""


def build_material_block(material: str) -> str:
    """把素材文本包成带约束说明的材料区块；没有材料时返回空串。"""
    if not material.strip():
        return ""
    return f"\n\n{_MATERIAL_HEADER}\n\n{material}"


def resolve_article_type(
    explicit: Optional[str], material_text: str = ""
) -> str:
    """判定文章类型。

    Args:
        explicit: 用户通过命令行指定的类型；auto 或 None 表示自动判定
        material_text: 材料文本，空串表示没有提供笔记或截图

    Returns:
        ARTICLE_TYPES 的 key
    """
    if explicit and explicit != "auto":
        if explicit not in ARTICLE_TYPES:
            raise ValueError(
                f"未知的文章类型: {explicit}，可选 {', '.join(ARTICLE_TYPES)} 或 auto"
            )
        return explicit
    # 带了笔记或截图材料，按课程笔记文处理；否则按知识点文
    return "course-note" if material_text.strip() else "knowledge"


def _build_system_prompt(
    config: dict,
    *,
    article_type: str = "knowledge",
    has_material: bool = False,
    category: str = None,
    tags: list[str] = None,
) -> tuple[str, str]:
    """构建写作系统提示词。

    写作风格以注入的 skill 规则为准（见 skill_loader），不参照历史文章样本。

    Args:
        category: 命令行指定的分类。要传给 frontmatter 模板，否则模板里写的是
            default_category，模型会照着模板写，命令行的值就丢了。
        tags: 命令行指定的标签，同上。

    Returns:
        (提示词, skill 来源标识)
    """
    content_config = config.get("content", {})
    style_section = (
        "## 写作风格要求\n"
        "写作风格以本文档后附的写作规范为准，那是硬约束。\n"
        "在这套规范之内，用自然的中文写，像同事给你讲一个技术点，\n"
        "句子长短交替，不要写成产品说明书。\n"
    )

    # 命令行覆盖优先，其次是配置里的默认值
    effective_category = category or content_config.get("default_category", DEFAULT_CATEGORY)
    effective_tags = tags or content_config.get("default_tags", DEFAULT_TAGS)
    tags_str = ", ".join(effective_tags) if effective_tags else "技术, 后端"

    today = date.today().strftime("%Y-%m-%d")

    prompt = _writing_template().format(
        style_section=style_section,
        date=today,
        category=effective_category,
        tags=tags_str,
        min_words=content_config.get("min_words", 1500),
        max_words=content_config.get("max_words", 4000),
        writing_focus=content_config.get("writing_focus", DEFAULT_WRITING_FOCUS).strip(),
    )

    # 注入文章类型判定结果
    type_name, type_rule = ARTICLE_TYPES.get(
        article_type, ARTICLE_TYPES["knowledge"]
    )
    prompt += (
        f"\n\n## 本文类型：{type_name}\n\n{type_rule}\n"
        "按这个类型的结构组织全文，不要在几种类型之间摇摆。"
    )

    # 注入 skill 规则（真实文件优先，读不到回退 prompts 副本）。
    # 拼接放在 .format() 之后，这样规则文件里出现 { } 不会被当成插值占位符。
    skill_text, skill_source = load_skill(
        "csdn", config=config, include_material=has_material
    )
    if skill_text:
        prompt = f"{prompt}\n\n{skill_text}"

    # 作者背景放在最后，它内部声明的“上面所有写作规则不受影响”
    # 才能覆盖到 skill 规则。字段全空时这里返回空串，不注入任何东西。
    author_section = build_prompt_section()
    if author_section:
        prompt = f"{prompt}\n\n{author_section}"

    return prompt, skill_source


async def generate_outline(
    topic: str,
    *,
    category: str = None,
    tags: list[str] = None,
    material: str = "",
    article_type: str = "knowledge",
) -> str:
    """生成文章大纲（含标题和插图计划）。

    Args:
        topic: 用户输入的题材
        category: 文章分类（覆盖默认值）
        tags: 文章标签（覆盖默认值）
        material: 笔记或截图转录出来的素材文本
        article_type: 文章类型

    Returns:
        大纲文本（Markdown 格式）
    """
    config = load_config()
    client = LLMClient(config)
    system_prompt, _ = _build_system_prompt(
        config,
        article_type=article_type,
        has_material=bool(material.strip()),
        category=category,
        tags=tags,
    )

    extra = ""
    if category:
        extra += f"\n文章分类：{category}"
    if tags:
        extra += f"\n文章标签：{', '.join(tags)}"

    material_block = build_material_block(material)

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"请先输出大纲和插图计划。\n\n"
                f"题材：{topic}{extra}{material_block}"
            ),
        },
    ]

    logger.info("开始生成大纲 - 题材: %s", topic[:50])
    return await client.chat(messages, temperature=0.7)


async def generate_article(
    topic: str,
    outline: Optional[str] = None,
    *,
    category: str = None,
    tags: list[str] = None,
    material: str = "",
    article_type: str = "knowledge",
    extra_instruction: str = "",
) -> str:
    """生成完整文章。

    改造前这里有两个几乎逐行相同的函数（generate_article / generate_article_direct），
    差别只在 user message 的措辞：有大纲就按大纲写，没有就直接写。合并成一个函数，
    按 outline 是否为空分支——两段措辞原样保留，不改模型看到的内容。

    Args:
        topic: 原始题材
        outline: 上一步生成的大纲；为空表示跳过大纲直接写（--fast）
        category: 文章分类
        tags: 文章标签
        material: 素材文本
        article_type: 文章类型
        extra_instruction: 追加要求（自检不合格时的修正指令用）

    Returns:
        完整的 Markdown 文章（含 frontmatter）
    """
    config = load_config()
    client = LLMClient(config)
    system_prompt, _ = _build_system_prompt(
        config,
        article_type=article_type,
        has_material=bool(material.strip()),
        category=category,
        tags=tags,
    )

    extra = ""
    if category:
        extra += f"\n文章分类：{category}"
    if tags:
        extra += f"\n文章标签：{', '.join(tags)}"

    material_block = build_material_block(material)

    if outline:
        body = (
            f"题材：{topic}{extra}{material_block}\n\n"
            f"以下是大纲，请根据大纲写出完整文章：\n\n{outline}\n\n"
            f"请直接输出完整的 Markdown 文章（以 YAML frontmatter 开头）。"
            f"记住：在需要配图的位置用 [IMAGE: 描述] 标记。"
            + (f"\n\n{extra_instruction}" if extra_instruction else "")
        )
        logger.info("开始生成正文 - 题材: %s", topic[:50])
    else:
        body = (
            f"请根据以下题材，直接写出完整的 Markdown 文章（以 YAML frontmatter 开头）：\n\n"
            f"{topic}{extra}{material_block}\n\n"
            f"在需要配图的位置用 [IMAGE: 描述] 标记。"
        )
        logger.info("直接生成文章（跳过大纲） - 题材: %s", topic[:50])

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": body},
    ]

    return await client.chat(messages, temperature=0.8, max_tokens=8192)


async def revise_article(
    article: str,
    instruction: str,
    *,
    article_type: str = "knowledge",
    has_material: bool = False,
    category: str = None,
    tags: list[str] = None,
) -> str:
    """按修正指令对已有文章做最小改动。

    用于自检不合格时的一次重写。与 generate_article 的区别是不重新出大纲，
    直接把现成文章交给模型改，避免整篇重写时丢内容或换主线。

    Args:
        article: 现有文章全文
        instruction: 修正指令（自检问题清单 + 修改要求）
        article_type: 文章类型，用于选对规则集
        has_material: 是否基于笔记/截图材料写成
        category: 文章分类，保证修正后 frontmatter 不被改回默认值
        tags: 文章标签，同上

    Returns:
        修正后的文章；调用失败时返回原文
    """
    config = load_config()
    client = LLMClient(config)
    system_prompt, _ = _build_system_prompt(
        config,
        article_type=article_type,
        has_material=has_material,
        category=category,
        tags=tags,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"以下是现有文章全文：\n\n{article}\n\n"
                f"---\n\n{instruction}"
            ),
        },
    ]

    logger.info("开始按自检结果修正文章 - 原文 %d 字符", len(article))
    try:
        revised = await client.chat(
            messages, temperature=0.4, max_tokens=8192
        )
    except Exception as e:
        logger.error("文章修正调用失败，保留原文: %s", e)
        return article

    # 模型偶尔会把整篇套进代码围栏，剥掉
    revised = strip_fence(revised)

    if len(revised) < len(article) * 0.5:
        logger.warning("修正结果长度异常（%d → %d），保留原文", len(article), len(revised))
        return article

    return revised


def extract_title(article: str) -> Optional[str]:
    """从文章中提取标题。"""
    # 优先 frontmatter
    fm_match = re.match(r'^---\s*\ntitle:\s*(.+?)\s*\n', article)
    if fm_match:
        return fm_match.group(1).strip().strip('"').strip("'")

    h1_match = re.search(r'^#\s+(.+)$', article, re.MULTILINE)
    if h1_match:
        return h1_match.group(1).strip()

    return None


def extract_image_descriptions(article: str) -> list[str]:
    """提取文章里的 [IMAGE: 描述] 标记。

    实现放在 text_utils，这里保留同名函数是因为它在 writer 的对外接口上
    （write_article 的返回值、管道其他阶段都按这个名字取用）。
    """
    return _extract_image_descriptions(article)


async def write_article(
    topic: str,
    *,
    category: str = None,
    tags: list[str] = None,
    material: str = "",
    article_type: str = "auto",
    skip_outline: bool = False,
) -> dict:
    """完整写作流程：大纲 → 正文。

    Returns:
        {
            "topic": str,
            "outline": str | None,
            "article": str,          # 完整 Markdown
            "image_descriptions": [...],
            "title": str,
            "article_type": str,     # 实际生效的类型
            "skill_source": str,     # "skill" 或 "prompts"
        }
    """
    resolved_type = resolve_article_type(article_type, material)
    logger.info(
        "文章类型: %s（%s）", resolved_type, ARTICLE_TYPES[resolved_type][0]
    )

    if skip_outline:
        outline = None
    else:
        outline = await generate_outline(
            topic,
            category=category,
            tags=tags,
            material=material,
            article_type=resolved_type,
        )

    # outline 为空就是“跳过大纲直接写”，generate_article 内部按这个分支
    article = await generate_article(
        topic,
        outline,
        category=category,
        tags=tags,
        material=material,
        article_type=resolved_type,
    )

    title = extract_title(article) or topic

    # 取 skill 来源用于日志和自检报告
    config = load_config()
    _, skill_source = _build_system_prompt(
        config, article_type=resolved_type, has_material=bool(material.strip())
    )

    image_descriptions = extract_image_descriptions(article)

    logger.info(
        "写作完成 - 标题: %s, 长度: %d 字, 插图: %d 处",
        title, len(article), len(image_descriptions),
    )

    return {
        "topic": topic,
        "outline": outline,
        "article": article,
        "image_descriptions": image_descriptions,
        "title": title,
        "article_type": resolved_type,
        "skill_source": skill_source,
    }
