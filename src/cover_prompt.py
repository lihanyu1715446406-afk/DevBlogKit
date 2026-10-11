"""
封面提示词模块：读最终文章 → 结构化分析 → 选风格 → 渲染可直接使用的中文提示词。

设计要点：

1. 只产出提示词文本，不调用任何图片生成 API，不产生出图费用。图片由人工生成。
2. 和 humanizer 一样，是写作之后的独立阶段：复用 LLMClient，不改动写作流程，
   也不重新生成文章，只读已经定稿的正文。
3. 分析与渲染分成两步（analyze_article / render_cover_prompt）。前者产出结构化字段，
   后者只负责把字段写成提示词。设计规范集中在 prompts/cover_design.txt，
   改风格规范只动那一个文件。
4. 面向不稳定输出做兜底：非 JSON 返回、字段缺失、文章过长、标记缺失、写文件失败
   都不应该让整条流水线失败——文章已经生成成功了，封面提示词失败只是少一个附件。
"""

import json
import logging
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Optional

from .config_loader import load_config
from .cover_history import format_recent_note, history_base_dir, load_recent_covers
from .cover_quality import DEFAULT_STYLE, check_cover_quality
from .llm_client import LLMClient
from .text_utils import strip_fence
from .utils import read_file, write_file

logger = logging.getLogger("blog_pipeline")

PROJECT_ROOT = Path(__file__).parent.parent
COVER_DESIGN_FILE = PROJECT_ROOT / "prompts" / "cover_design.txt"

# 两种风格的稳定 key。中文名只用于展示；模型返回的 recommended_style 必须是这两个 key。
STYLES = {
    "editorial": {
        "name_en": "Premium Technical Editorial",
        "name_zh": "高级技术杂志风",
        "summary": "排版、网格与留白为主角的专业出版物封面",
    },
    "abstract": {
        "name_en": "Abstract Technical Conceptualism",
        "name_zh": "抽象概念视觉风",
        "summary": "把核心机制翻译成几何结构、路径、层级或循环的视觉隐喻",
    },
}

# 判断「这篇文章更像在解释机制」的兜底关键词。只在模型返回的风格不可用时才用。
_ABSTRACT_HINTS = (
    "原理", "机制", "为什么", "怎么算", "协议", "内存", "索引", "算法", "底层",
    "源码", "架构", "流程", "循环", "链路", "结构", "握手", "回收", "调度",
)

# 送进模型的正文上限。超出按「头 + 尾」截断，丢中间：
# 文章的核心机制一般在开头，结论和判断方法在结尾，中段多是代码与展开。
DEFAULT_MAX_ARTICLE_CHARS = 12000

# 文章短于这个长度就认为没写成功，不出封面提示词，避免产出一份误导性的设计稿
MIN_ARTICLE_CHARS = 200

# 渲染阶段用来切分两份提示词的标记
_MAIN_MARKER = "[MAIN PROMPT]"
_NO_TEXT_MARKER = "[NO-TEXT PROMPT]"

# 提示词块前面可能带的序号小标题（「一、」「## 三、」「1.」），剥掉
_ORDINAL_RE = re.compile(
    r"^\s*#{0,6}\s*(?:[一二三四五六七八九十]+\s*[、.．)）]|[0-9]+\s*[、.．)）])\s*"
)

# Windows 文件名里非法或容易被系统吃掉的字符
_ILLEGAL_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 提炼短标题时的截断标记，按优先级排列。只在模型没给出封面标题时兜底用。
_TITLE_CUT_MARKERS = (
    "：", ":", "——", "—", "，", ",", "？", "?", "！", "!",
    "到底", "为什么", "怎么", "如何", "一文", "一篇", "从",
    "详解", "全解", "入门", "实战", "指南", "原理",
)


class CoverPromptError(RuntimeError):
    """封面提示词生成过程中的可预期失败（模型返回不可用、设计规范缺失等）。"""


# ============================================================
# 输出契约
#
# 与 writing.txt 不同，这两个契约不放在 prompts/ 下：
# prompts/cover_design.txt 是「设计规范」（人改这个），
# 这里的契约是「程序依赖的输出格式」（改代码才动它），
# 和 skill_loader 里 _HUMANIZER_CONTRACT 的定位一致。
# ============================================================

ANALYSIS_CONTRACT = """
═══════════════════════════════════════════════════════════
第一阶段输出格式（硬性，优先级高于上文任何描述）
═══════════════════════════════════════════════════════════

只输出一个 JSON 对象。不要用 ``` 包裹，不要输出任何解释文字、标题或前后缀。

字段与取值要求：

{
  "article_title": "文章原始标题，原样抄写",
  "article_type": "从「教程 / 原理解析 / 对比 / 实践指南 / 架构分析」里选最贴切的一个",
  "technical_domain": "技术领域，如 Java、JVM、MySQL、计算机网络、AI Agent",
  "core_message": "一句话，这篇文章最想传达的核心观点",
  "key_mechanism": "文章重点解释的技术机制，一到两句，要具体",
  "visual_metaphor": "最适合表现该主题的视觉隐喻：画面里能看到什么形状、结构、方向、关系",
  "main_visual": "封面最重要的视觉主体，写清形状、结构、数量、元素之间的位置关系",
  "supporting_elements": ["少量必要的辅助元素，最多 3 个；没有就给空数组"],
  "recommended_style": "editorial 或 abstract，只能二选一",
  "style_reason": "为什么选这个风格，必须结合这篇文章真正讲的内容来说，不要泛泛而谈",
  "composition": "具体构图：主体位置、标题位置、版面如何切分、视线走向、留白在哪",
  "color_palette": ["2-3 个主色，写成「名称 #色值」的形式"],
  "text_plan": {
    "main_title": "封面主标题，1-2 行，短而有力，尽量保留关键技术名词",
    "subtitle": "可选副标题，没有就给空字符串",
    "keywords": ["封面上要保留英文的技术缩写，如 TCP、RTO、JVM"]
  }
}

注意：
- visual_metaphor / main_visual / composition 必须来自这篇文章的正文，
  不要套用其他题材的视觉方案，也不要用机器人、芯片、代码屏幕这类万能装饰。
- style_reason 要能让别人看懂你的判断依据。
"""

RENDER_CONTRACT = """
═══════════════════════════════════════════════════════════
第二阶段输出格式（硬性，优先级高于上文任何描述）
═══════════════════════════════════════════════════════════

你会收到第一阶段的分析结果 JSON。请把它渲染成两份可以直接复制使用的中文提示词，
严格按下面的结构输出，不要输出任何额外说明：

[MAIN PROMPT]
（第一份：包含标题排版的完整封面提示词）

[NO-TEXT PROMPT]
（第二份：无文字背景图方案）

两份提示词的共同要求：

- 中文书写，一整段或多段都可以，不要用 Markdown 的标题、列表符号或加粗。
- 必须逐条覆盖「七、最终提示词必须包含的要素」里的 11 件事，读的人照着就能出图。
- 具体、可执行：说清画面里出现什么、在哪、多大、什么颜色、什么质感。
- 明确列出画面上不许出现的东西（无关装饰、随机文字、错字、杂乱线条等）。
- 提示词开头点明风格时，只用分析结果里那个风格的名称，写法固定为：
  editorial 写「高级技术杂志风（Premium Technical Editorial）」，
  abstract 写「抽象概念视觉风（Abstract Technical Conceptualism）」。
  不要把两种风格的名字混着写成「杂志抽象概念视觉风」这类串在一起的形容词。

两份提示词的区别：

- [MAIN PROMPT] 要把 text_plan 里的主标题（和副标题，如果非空）作为画面的一部分来设计：
  写清标题原文、放在哪个位置、几行、字号层级、对齐方式、与视觉主体的距离，
  并按「八、中文文字生成策略」要求模型准确呈现指定文字、不生成其他文字。
- [NO-TEXT PROMPT] 描述同一张图的同一个视觉主体、构图和配色，但画面上不出现任何文字，
  只在标题位置留出一块干净、完整、无遮挡的标题预留区。要说明预留区的位置和大致占比。

两份提示词的构图和视觉主体必须一致，不要变成两张不同的图。
"""


# ============================================================
# 规范加载
# ============================================================


def load_cover_design(config: Optional[dict] = None) -> str:
    """读取封面设计规范（品牌规范 + 两种风格 + 选择规则 + 标题规则 + 输出要素）。"""
    path = COVER_DESIGN_FILE
    if not path.is_file():
        logger.error("封面设计规范不存在: %s", path)
        return ""
    return read_file(str(path)).strip()


# ============================================================
# 文本处理小工具
# ============================================================


def _shrink_article(article: str, limit: int) -> str:
    """文章过长时按「头 + 尾」截断，并留下明确的截断标记。"""
    text = (article or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text

    head_len = int(limit * 0.7)
    tail_len = limit - head_len
    logger.info("文章较长（%d 字符），截断到 %d 字符送分析", len(text), limit)
    return (
        text[:head_len]
        + "\n\n……（此处省略中间部分，不影响主题与核心机制判断）……\n\n"
        + text[-tail_len:]
    )


def _parse_json_object(text: str) -> Optional[dict]:
    """从模型输出里抠出一个 JSON 对象。抠不出来返回 None。"""
    cleaned = strip_fence(text or "")
    if not cleaned:
        return None
    # 模型偶尔在 JSON 前后加一句解释，取第一个 { 到最后一个 } 之间的内容
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as e:
        logger.warning("封面分析结果不是合法 JSON: %s", e)
        return None
    return data if isinstance(data, dict) else None


def _as_list(value, limit: int = 3) -> list[str]:
    """把字段规整成字符串列表，容忍模型给出字符串或 None。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = [v.strip() for v in re.split(r"[、,，;；\n]", value) if v.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = [str(value).strip()]
    return items[:limit]


def _as_text(value) -> str:
    """把字段规整成一句话，容忍模型给出列表。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _derive_short_title(title: str, limit: int = 16) -> str:
    """模型没给出封面标题时，从原题里抠一个能用的短标题。

    只在兜底路径上跑，不追求漂亮，但不能改变技术含义，所以只做「按常见分隔词截断」。
    """
    text = (title or "").strip()
    for marker in _TITLE_CUT_MARKERS:
        idx = text.find(marker)
        if 0 < idx <= limit:
            text = text[:idx].strip()
    text = text[:limit].strip(" 　-—·:：")
    return text or (title or "").strip()


def _guess_style(article_type: str, article: str) -> str:
    """模型没给出可用风格时的兜底判型。宁可保守走排版路线。"""
    blob = f"{article_type} {article[:1500]}"
    hits = sum(1 for h in _ABSTRACT_HINTS if h in blob)
    return "abstract" if hits >= 2 else DEFAULT_STYLE


# 识别模型返回的风格值用的关键词。按「谁先出现谁算数」判断，
# 这样「abstract（也可以用 editorial）」这种带备注的返回不会选错。
_STYLE_HINTS = {
    "editorial": ("editorial", "杂志", "排版"),
    "abstract": ("abstract", "抽象", "conceptual"),
}


def _normalize_style(value, *, article_type: str, article: str) -> str:
    """把模型给的风格值规整成 STYLES 里的 key。"""
    text = str(value or "").strip().lower()
    if text in STYLES:
        return text

    hits = {}
    for key, hints in _STYLE_HINTS.items():
        found = [text.find(h) for h in hints if text.find(h) >= 0]
        if found:
            hits[key] = min(found)
    if hits:
        return min(hits, key=hits.get)

    logger.warning("模型给出的风格不可用（%r），按文章内容兜底判型", value)
    return _guess_style(article_type, article)


# ============================================================
# 第一阶段：结构化分析
# ============================================================


def _coerce_analysis(raw: Optional[dict], *, title: str, article: str,
                     article_type: str) -> dict:
    """把模型返回的分析结果补齐成固定结构的 dict。

    缺字段、类型不对都在这里修，下游渲染阶段可以假定字段一定存在。
    """
    raw = raw if isinstance(raw, dict) else {}

    def pick(*keys):
        for k in keys:
            if raw.get(k) not in (None, "", [], {}):
                return raw[k]
        return None

    analysis = {
        "article_title": _as_text(pick("article_title")) or title,
        "article_type": _as_text(pick("article_type")) or article_type or "技术文章",
        "technical_domain": _as_text(pick("technical_domain")),
        "core_message": _as_text(pick("core_message")),
        "key_mechanism": _as_text(pick("key_mechanism")),
        "visual_metaphor": _as_text(pick("visual_metaphor")),
        "main_visual": _as_text(pick("main_visual")),
        "supporting_elements": _as_list(raw.get("supporting_elements")),
        "color_palette": _as_list(raw.get("color_palette"), limit=3),
        "composition": _as_text(pick("composition")),
    }

    analysis["recommended_style"] = _normalize_style(
        pick("recommended_style", "style"),
        article_type=article_type,
        article=article,
    )
    analysis["style_reason"] = _as_text(pick("style_reason")) or (
        f"按文章内容自动选择：{STYLES[analysis['recommended_style']]['summary']}"
    )

    text_plan = raw.get("text_plan")
    if isinstance(text_plan, str):
        text_plan = {"main_title": text_plan}
    elif not isinstance(text_plan, dict):
        text_plan = {}

    main_title = _as_text(text_plan.get("main_title")) or _derive_short_title(
        analysis["article_title"] or title
    )
    analysis["text_plan"] = {
        "main_title": main_title,
        "subtitle": _as_text(text_plan.get("subtitle")),
        "keywords": _as_list(text_plan.get("keywords"), limit=5),
    }

    # 缺关键字段只提示不失败：渲染阶段仍能基于已有字段写出可用的提示词
    missing = [
        key for key in (
            "technical_domain", "core_message", "key_mechanism",
            "visual_metaphor", "main_visual", "composition", "color_palette",
        )
        if not analysis.get(key)
    ]
    if missing:
        logger.warning("封面分析缺少字段: %s（已按空值继续）", ", ".join(missing))

    return analysis


async def analyze_article(
    article: str,
    title: str,
    *,
    article_type: str = "",
    design: str = "",
    recent_covers: Optional[list] = None,
    config: Optional[dict] = None,
    client: Optional[LLMClient] = None,
) -> dict:
    """分析文章，产出封面设计依据（结构化字段）。

    Args:
        recent_covers: 最近几篇的构图签名。只用来避让构图，不参与风格判断
            （避让提示的作用域写死在 format_recent_note 里）

    Raises:
        CoverPromptError: 设计规范缺失，或模型没返回可解析的 JSON
    """
    config = config or load_config()
    cfg = config.get("cover_prompt", {})
    design = design or load_cover_design(config)
    if not design:
        raise CoverPromptError("设计规范缺失（prompts/cover_design.txt）")

    client = client or LLMClient(config)
    body = _shrink_article(article, int(cfg.get("max_article_chars", DEFAULT_MAX_ARTICLE_CHARS)))

    recent_note = format_recent_note(list(recent_covers or []))
    if recent_note:
        logger.info("带上最近 %d 篇封面构图做避让参考", len(recent_covers or []))

    user_content = (
        f"以下是文章正文（Markdown，含 YAML frontmatter）：\n\n{body}\n\n"
        f"---\n\n"
        f"文章原始标题：{title}\n"
        f"（写作阶段判定的文章类型：{article_type or '未知'}，仅供参考，"
        f"以正文实际内容为准）\n\n"
        f"请按规范完成分析，只输出一个 JSON 对象。"
    )
    if recent_note:
        user_content = f"{user_content}\n\n{recent_note}"

    messages = [
        {"role": "system", "content": f"{design}\n\n{ANALYSIS_CONTRACT}"},
        {"role": "user", "content": user_content},
    ]

    logger.info("开始分析文章内容（%d 字符）→ 封面设计依据", len(body))
    raw_text = await client.chat_json(
        messages,
        temperature=cfg.get("temperature", 0.5),
        max_tokens=cfg.get("max_tokens", 2048),
    )

    parsed = _parse_json_object(raw_text)
    if parsed is None:
        raise CoverPromptError("模型未返回可解析的 JSON 分析结果")

    analysis = _coerce_analysis(
        parsed, title=title, article=article, article_type=article_type
    )
    logger.info(
        "封面分析完成 - 风格: %s(%s), 主视觉: %s",
        analysis["recommended_style"],
        STYLES[analysis["recommended_style"]]["name_zh"],
        analysis["main_visual"][:40] or "-",
    )
    return analysis


# ============================================================
# 第二阶段：渲染提示词
# ============================================================


def _clean_prompt_block(text: str) -> str:
    """清掉提示词块前面可能带的编号小标题。

    只按「一、」「1.」「## 三、」这种序号格式剥，不能无差别 strip 中文字符：
    提示词正文本身可能就以「一」「三」开头（例如「三台设备…」「一条时间线…」）。
    """
    text = strip_fence(text or "").strip()
    for _ in range(3):
        new = _ORDINAL_RE.sub("", text, count=1).strip()
        if new == text:
            break
        text = new
    return text


def _split_render_output(text: str) -> tuple[str, str]:
    """把渲染结果拆成 (含标题提示词, 无文字提示词)。缺标记时第二份为空串。"""
    cleaned = text or ""
    no_text = ""

    # 标记可能被写成 [NO-TEXT PROMPT]、[NO TEXT VARIANT] 等，只认前缀
    upper = cleaned.upper()
    idx = upper.find(_NO_TEXT_MARKER)
    if idx == -1:
        idx = upper.find("[NO TEXT")
    if idx != -1:
        cleaned, no_text = cleaned[:idx], cleaned[idx:]
        # 连带把标记本身和紧跟的冒号/换行去掉
        no_text = re.sub(r"^\[[^\]]*\]\s*[:：]?\s*", "", no_text.strip())

    if _MAIN_MARKER in cleaned:
        cleaned = cleaned.split(_MAIN_MARKER, 1)[1]
    else:
        cleaned = re.sub(r"^\s*\[MAIN[^\]]*\]\s*[:：]?\s*", "", cleaned, count=1)

    return _clean_prompt_block(cleaned), _clean_prompt_block(no_text)


async def _render_no_text_fallback(
    analysis: dict, *, design: str, config: dict, client: LLMClient
) -> str:
    """渲染结果里缺「无文字方案」时，单独补一次调用。"""
    prompt = (
        f"{design}\n\n"
        "只输出一段中文提示词，描述一张不含任何文字的同主题封面背景图："
        "视觉主体、构图、配色与下面这份分析保持一致，画面上不出现任何字符，"
        "只在标题位置留出一块干净完整、无遮挡的标题预留区，并说明预留区的位置和占比。"
        "不要输出解释，不要用代码围栏，不要用 Markdown 标题或列表符号。\n\n"
        f"分析结果：\n{json.dumps(analysis, ensure_ascii=False, indent=2)}"
    )
    text = await client.chat(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请输出这段提示词。"},
        ],
        temperature=0.6,
        max_tokens=config.get("cover_prompt", {}).get("render_max_tokens", 4096),
    )
    return strip_fence(text or "")


async def render_cover_prompt(
    analysis: dict,
    *,
    design: str = "",
    config: Optional[dict] = None,
    client: Optional[LLMClient] = None,
) -> dict:
    """把分析结果渲染成两份中文提示词。

    Returns:
        {"main": 含标题排版的提示词, "no_text": 无文字背景图提示词}

    Raises:
        CoverPromptError: 模型没返回可用内容
    """
    config = config or load_config()
    cfg = config.get("cover_prompt", {})
    design = design or load_cover_design(config)
    if not design:
        raise CoverPromptError("设计规范缺失（prompts/cover_design.txt）")

    client = client or LLMClient(config)

    messages = [
        {"role": "system", "content": f"{design}\n\n{RENDER_CONTRACT}"},
        {
            "role": "user",
            "content": (
                "以下是第一阶段的分析结果 JSON：\n\n"
                f"{json.dumps(analysis, ensure_ascii=False, indent=2)}\n\n"
                "请按规范渲染两份中文提示词。"
            ),
        },
    ]

    logger.info("开始渲染封面提示词 - 风格: %s", analysis.get("recommended_style", ""))
    raw = await client.chat(
        messages,
        temperature=cfg.get("render_temperature", 0.8),
        max_tokens=cfg.get("render_max_tokens", 4096),
    )

    main_prompt, no_text_prompt = _split_render_output(raw)
    if len(main_prompt) < 80:
        raise CoverPromptError("渲染结果过短或为空")

    if not no_text_prompt:
        logger.warning("渲染结果缺少无文字方案标记，单独补一次调用")
        try:
            no_text_prompt = await _render_no_text_fallback(
                analysis, design=design, config=config, client=client
            )
        except Exception as e:
            logger.warning("无文字方案补充生成失败: %s", e)

    return {"main": main_prompt, "no_text": no_text_prompt}


# ============================================================
# 一次性针对性修正
#
# 只有 fail 级问题才走到这里，而且整条流程最多调用一次。修正稿必须再查一遍、
# 确认 fail 真的变少且没引入新问题才采纳，否则保留原稿——宁可不动，也不能改坏。
# ============================================================

REPAIR_CONTRACT = """
═══════════════════════════════════════════════════════════
修正任务（硬性，优先级高于上文任何描述）
═══════════════════════════════════════════════════════════

上面两份提示词没有通过质量检查。请只针对被判定为 fail 的检查项做**最小修正**，
然后重新输出一个 JSON 对象。不要输出解释、Markdown 或代码围栏。

{
  "analysis_patch": {
    "只放需要改动的分析字段，没改的字段不要出现。可改字段仅限：
     visual_metaphor / main_visual / supporting_elements / composition /
     color_palette / text_plan / key_mechanism / style_reason"
  },
  "main_prompt": "修正后的完整含标题提示词",
  "no_text_prompt": "修正后的完整无文字提示词"
}

硬性要求：

- 除非 fail 明确指出「风格与内容不匹配」或提示词里写了另一种风格的名字，
  否则不得修改 recommended_style。构图撞车的正解是换视觉主体和构图，
  不是换风格——不要为了「不一样」去改一篇真正适合这个风格的文章。
- 不得为通过检查而新增无关元素或装饰；修正后的提示词不得比原文更长，
  也不得比原文更啰嗦。
- 两份提示词必须描述同一个视觉主体与构图，[无文字] 那份不出现任何要渲染的文字。
- 保留原主标题原文和要保留的英文缩写，不得新增画面上要渲染的文字。
- 逐条落实「七、最终提示词必须包含的要素」，仍然要写清画面上不许出现什么。
"""

# 修正调用允许改动的分析字段白名单。
_PATCHABLE_FIELDS = (
    "visual_metaphor", "main_visual", "supporting_elements", "composition",
    "color_palette", "text_plan", "key_mechanism", "style_reason",
)


def _merge_analysis_patch(
    analysis: dict, patch, *, title: str, article: str, article_type: str,
    allow_style_change: bool,
) -> dict:
    """把修正稿的 analysis_patch 合并进原分析结果。

    只认白名单字段，其余丢弃：修正调用不该顺手改掉没坏的字段。recommended_style
    默认禁止改动，只有第 2 项本身 fail 时才放行。
    """
    merged = dict(analysis)
    if isinstance(patch, dict):
        for key in _PATCHABLE_FIELDS:
            if patch.get(key):
                merged[key] = patch[key]
        if allow_style_change and patch.get("recommended_style"):
            merged["recommended_style"] = patch["recommended_style"]
    return _coerce_analysis(
        merged, title=title, article=article, article_type=article_type
    )


async def repair_cover_prompt(
    analysis: dict,
    rendered: dict,
    quality: dict,
    *,
    title: str,
    article: str,
    article_type: str = "",
    design: str = "",
    config: Optional[dict] = None,
    client: Optional[LLMClient] = None,
) -> Optional[dict]:
    """针对 fail 项做一次最小修正。

    Returns:
        {"analysis": dict, "main": str, "no_text": str}；任何失败都返回 None，
        由调用方保留原稿
    """
    config = config or load_config()
    cfg = config.get("cover_prompt", {})
    design = design or load_cover_design(config)
    if not design:
        return None

    client = client or LLMClient(config)

    failed = [c for c in quality.get("checks", []) if c["status"] == "fail"]
    if not failed:
        return None

    issues = "\n".join(f"- 第 {c['id']} 项「{c['name']}」：{c['detail']}" for c in failed)
    allow_style_change = any(c["id"] == 2 for c in failed)

    messages = [
        {"role": "system", "content": f"{design}\n\n{RENDER_CONTRACT}\n\n{REPAIR_CONTRACT}"},
        {
            "role": "user",
            "content": (
                f"第一阶段的分析结果：\n\n"
                f"{json.dumps(analysis, ensure_ascii=False, indent=2)}\n\n"
                f"当前两份提示词：\n\n[MAIN PROMPT]\n{rendered.get('main', '')}\n\n"
                f"[NO-TEXT PROMPT]\n{rendered.get('no_text', '')}\n\n"
                f"质量检查未通过的项：\n{issues}\n\n"
                f"请按修正任务输出 JSON。"
            ),
        },
    ]

    logger.info("封面提示词命中 %d 项问题，做一次针对性修正", len(failed))
    raw = await client.chat_json(
        messages,
        temperature=cfg.get("repair_temperature", 0.4),
        max_tokens=cfg.get("repair_max_tokens", 4600),
    )

    parsed = _parse_json_object(raw)
    if not isinstance(parsed, dict):
        logger.warning("修正结果不是可解析的 JSON，保留原稿")
        return None

    main_prompt = _clean_prompt_block(_as_text(parsed.get("main_prompt")))
    no_text_prompt = _clean_prompt_block(_as_text(parsed.get("no_text_prompt")))
    if len(main_prompt) < 80:
        logger.warning("修正后的提示词过短，保留原稿")
        return None

    return {
        "analysis": _merge_analysis_patch(
            analysis, parsed.get("analysis_patch"),
            title=title, article=article, article_type=article_type,
            allow_style_change=allow_style_change,
        ),
        "main": main_prompt,
        "no_text": no_text_prompt,
    }


def accept_repair(before: dict, after: dict) -> bool:
    """修正稿只有「fail 确实变少、且没引入新的 fail」才值得采纳。

    等量替换不算变好：模型可能只是换了个说法，甚至把别的地方改坏。
    这里要求新的 fail 集合是旧的**真子集**。
    """
    return set(after.get("fail_ids") or []) < set(before.get("fail_ids") or [])


async def _repair_if_better(
    analysis: dict,
    rendered: dict,
    quality: dict,
    recent: list,
    *,
    title: str,
    article: str,
    article_type: str,
    design: str,
    config: dict,
    client: LLMClient,
) -> tuple[dict, dict, dict]:
    """做一次针对性修正，只有确实变好才采纳。

    修正没变好、调用失败、返回不可解析，都原样返回原稿——封面提示词已经生成出来了，
    改动它必须有明确收益。
    """
    try:
        repaired = await repair_cover_prompt(
            analysis, rendered, quality,
            title=title, article=article, article_type=article_type,
            design=design, config=config, client=client,
        )
    except Exception as e:
        logger.warning("封面修正调用失败，保留原稿: %s", e)
        repaired = None

    if not repaired:
        quality["repair"] = "failed"
        return analysis, rendered, quality

    after = check_cover_quality(
        repaired["analysis"], repaired["main"], repaired["no_text"],
        recent_covers=recent,
        limits=(config.get("cover_prompt") or {}).get("quality_limits"),
    )
    if not accept_repair(quality, after):
        logger.info("修正稿没有让问题变少，保留原稿")
        quality["repair"] = "rejected"
        return analysis, rendered, quality

    after["repair"] = "applied"
    logger.info(
        "封面提示词已按质量问题修正: %s → %s", quality["fail_ids"], after["fail_ids"]
    )
    return (
        repaired["analysis"],
        {"main": repaired["main"], "no_text": repaired["no_text"]},
        after,
    )


def quality_summary(quality: Optional[dict]) -> dict:
    """给 pipeline 和进度事件用的精简摘要。"""
    if not quality:
        return {"checked": False, "passed": 0, "total": 0, "fail": 0, "warn": 0, "repair": "skipped"}
    total = quality.get("total", 0)
    fail = len(quality.get("fail_ids") or [])
    warn = len(quality.get("warn_ids") or [])
    return {
        "checked": True,
        "passed": total - fail - warn,
        "total": total,
        "fail": fail,
        "warn": warn,
        "repair": quality.get("repair", "none"),
    }


# ============================================================
# 文件组装与保存
# ============================================================


def _safe_filename(title: str, limit: int = 60) -> str:
    """把标题变成 Windows 上安全的文件名片段。"""
    name = _ILLEGAL_FILENAME_RE.sub("-", (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .-")
    name = name[:limit].strip(" .-")
    if not name:
        return "cover"
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = f"{name}_"
    return name


def build_cover_prompt_markdown(
    analysis: dict,
    main_prompt: str,
    no_text_prompt: str,
    *,
    source_dir: str = "",
    quality: Optional[dict] = None,
    recent_covers: Optional[list] = None,
) -> str:
    """组装最终交付的 Markdown 文件内容。

    文章信息、风格选择、核心视觉概念这几段由代码确定性渲染，
    只有两份提示词正文来自模型，文件结构不依赖模型遵守格式。

    quality 只用来生成末尾的「七、质量检查」一节。那一节刻意放在两份提示词的
    代码块**之后**：检查过程属于内部记录，不能混进要复制给图片模型的提示词。
    """
    style = analysis.get("recommended_style", DEFAULT_STYLE)
    style_info = STYLES.get(style, STYLES[DEFAULT_STYLE])
    text_plan = analysis.get("text_plan", {})
    supporting = analysis.get("supporting_elements") or []
    palette = analysis.get("color_palette") or []

    lines = [
        f"# 封面提示词 — {analysis.get('article_title', '')}",
        "",
        f"> 由 blog-pipeline 自动生成 · {date.today().strftime('%Y-%m-%d')} · "
        "仅生成提示词文本，不调用任何图片生成 API。",
        "> 直接把下面两份提示词复制到 ChatGPT ImageGen、即梦、Midjourney 等工具里出图。",
        "",
        "## 一、文章信息",
        "",
        f"- 文章标题：{analysis.get('article_title', '')}",
        f"- 文章类型：{analysis.get('article_type', '')}",
        f"- 技术领域：{analysis.get('technical_domain', '') or '未识别'}",
        f"- 核心观点：{analysis.get('core_message', '') or '未识别'}",
        f"- 关键机制：{analysis.get('key_mechanism', '') or '未识别'}",
    ]
    if source_dir:
        lines.append(f"- 文章目录：{source_dir}")
    lines += [
        "",
        "## 二、自动选择的风格",
        "",
        f"- 风格：{style_info['name_zh']}（{style_info['name_en']}）",
        f"- 内部标识：{style}",
        f"- 选择原因：{analysis.get('style_reason', '')}",
        "",
        "## 三、核心视觉概念",
        "",
        f"- 视觉隐喻：{analysis.get('visual_metaphor', '') or '未识别'}",
        f"- 主视觉：{analysis.get('main_visual', '') or '未识别'}",
        f"- 辅助元素：{'、'.join(supporting) if supporting else '无'}",
        f"- 构图：{analysis.get('composition', '') or '未识别'}",
        f"- 配色：{'、'.join(palette) if palette else '按品牌规范（雾白 / 浅灰蓝 / 深蓝黑 + 少量强调色）'}",
        "",
        "## 四、封面文字",
        "",
        f"- 主标题：{text_plan.get('main_title', '')}",
        f"- 副标题：{text_plan.get('subtitle', '') or '（无）'}",
        f"- 保留的英文缩写：{'、'.join(text_plan.get('keywords') or []) or '（无）'}",
        "",
        "---",
        "",
        "## 五、完整封面生成提示词（含标题排版，可直接复制）",
        "",
    ]

    lines.append("````text")
    lines.append(main_prompt.strip())
    lines.append("````")
    lines.append("")
    lines.append("## 六、无文字背景图方案（可选，自行排版标题）")
    lines.append("")
    if no_text_prompt.strip():
        lines.append("````text")
        lines.append(no_text_prompt.strip())
        lines.append("````")
    else:
        lines.append("*这次没有生成无文字方案。可以把上面那份提示词里的文字要求删掉，"
                     "并把标题位置改成「留出干净的标题预留区」。*")
    lines.append("")

    if quality:
        lines.extend(_render_quality_section(quality, list(recent_covers or [])))

    return "\n".join(lines)


_QUALITY_MARK = {"pass": "✅", "warn": "⚠️", "fail": "❌"}

_QUALITY_REPAIR_TEXT = {
    "none": "未触发",
    "applied": "已触发，修正后采纳",
    "rejected": "已触发，但修正没有变好，保留原稿",
    "failed": "已触发，但修正调用失败，保留原稿",
    "skipped": "未启用",
}


def _render_quality_section(quality: dict, recent_covers: list) -> list[str]:
    """生成「七、质量检查」一节。

    这一节是给人工核对用的内部记录，不含任何要复制给图片模型的内容，
    所以刻意排在两份提示词代码块之后。
    """
    checks = quality.get("checks") or []
    if not checks:
        return []

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    passed = len(checks) - len(fails) - len(warns)

    recent_text = (
        "、".join(f"《{s.get('title') or '未命名'}》" for s in recent_covers)
        if recent_covers else "暂无历史封面"
    )

    lines = [
        "## 七、质量检查（自动生成，仅供人工核对，不属于图片提示词）",
        "",
        f"- 结论：{len(checks)} 项中 {passed} 项通过 · {len(warns)} 项提示 · {len(fails)} 项问题",
        f"- 历史参考：最近 {len(recent_covers)} 篇（{recent_text}）",
        f"- 针对性修正：{_QUALITY_REPAIR_TEXT.get(quality.get('repair', 'none'), '未触发')}",
        "",
        "| # | 检查项 | 结果 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for c in checks:
        mark = _QUALITY_MARK.get(c["status"], "")
        detail = str(c.get("detail", "")).replace("|", "/").replace("\n", " ")
        lines.append(f"| {c['id']} | {c['name']} | {mark} | {detail} |")
    lines.append("")
    return lines


def save_cover_prompt(markdown: str, output_dir: str, title: str) -> Optional[str]:
    """把封面提示词写到文章输出目录，返回文件路径；写入失败返回 None。"""
    filename = f"{_safe_filename(title)}.cover_prompt.md"
    path = Path(output_dir) / filename
    try:
        write_file(str(path), markdown)
    except OSError as e:
        logger.error("封面提示词写入失败: %s → %s", path, e)
        return None
    logger.info("封面提示词已保存: %s", path)
    return str(path)


# ============================================================
# 对外主入口
# ============================================================


async def generate_cover_prompt(
    article: str,
    title: str,
    *,
    article_type: str = "",
    output_dir: Optional[str] = None,
    config: Optional[dict] = None,
    client: Optional[LLMClient] = None,
) -> dict:
    """完整流程：分析最终文章 → 选风格 → 渲染提示词 → 落盘。

    任何一步失败都只返回 ok=False，不抛异常：文章此时已经生成成功，
    封面提示词失败不应该让整篇交付失败。

    Returns:
        {
            "ok": bool,
            "path": str | None,          # 提示词文件路径，output_dir 为空时为 None
            "style": str,                # editorial | abstract
            "style_name": str,           # 中文风格名
            "reason": str,               # ok=False 时的原因
            "analysis": dict | None,     # 结构化分析结果
            "markdown": str,             # 文件内容（output_dir 为空时也能拿到）
            "quality": dict,             # 质量检查摘要（见 quality_summary）
        }
    """
    config = config or load_config()
    result = {
        "ok": False, "path": None, "style": "", "style_name": "",
        "reason": "", "analysis": None, "markdown": "", "quality": None,
    }

    body = (article or "").strip()
    if len(body) < MIN_ARTICLE_CHARS:
        result["reason"] = f"文章内容过短（{len(body)} 字符），不出封面提示词"
        logger.warning("封面提示词跳过: %s", result["reason"])
        return result

    design = load_cover_design(config)
    if not design:
        result["reason"] = f"设计规范缺失: {COVER_DESIGN_FILE}"
        logger.error("封面提示词跳过: %s", result["reason"])
        return result

    cfg = config.get("cover_prompt", {})
    client = client or LLMClient(config)

    # 最近几篇的构图签名。只是避让参考，取不到就当没有历史。
    recent = load_recent_covers(
        history_base_dir(output_dir, config),
        limit=int(cfg.get("history_count", 3)),
        exclude_dir=output_dir,
        exclude_title=title,
    )

    try:
        analysis = await analyze_article(
            body, title,
            article_type=article_type,
            design=design,
            recent_covers=recent,
            config=config,
            client=client,
        )
        rendered = await render_cover_prompt(
            analysis, design=design, config=config, client=client
        )
    except CoverPromptError as e:
        result["reason"] = str(e)
        logger.warning("封面提示词未生成: %s", e)
        return result
    except Exception as e:
        # 网络、鉴权、超时等：记日志，保留已经生成好的文章
        result["reason"] = f"{type(e).__name__}: {e}"
        logger.exception("封面提示词生成失败，已跳过")
        return result

    # 质量检查 + 最多一次针对性修正
    quality = None
    if cfg.get("quality_check", True):
        quality = check_cover_quality(
            analysis, rendered["main"], rendered["no_text"],
            recent_covers=recent,
            limits=cfg.get("quality_limits"),
        )
        logger.info(
            "封面质量检查 - %d 项通过, %d 项提示, %d 项问题",
            quality["total"] - len(quality["fail_ids"]) - len(quality["warn_ids"]),
            len(quality["warn_ids"]),
            len(quality["fail_ids"]),
        )

        if quality["fail_ids"] and cfg.get("repair", True):
            analysis, rendered, quality = await _repair_if_better(
                analysis, rendered, quality, recent,
                title=title, article=body, article_type=article_type,
                design=design, config=config, client=client,
            )
        elif quality["fail_ids"]:
            quality["repair"] = "skipped"

    style = analysis["recommended_style"]
    markdown = build_cover_prompt_markdown(
        analysis,
        rendered["main"],
        rendered["no_text"],
        source_dir=str(output_dir or ""),
        quality=quality,
        recent_covers=recent,
    )

    result.update({
        "ok": True,
        "style": style,
        "style_name": STYLES[style]["name_zh"],
        "reason": "",
        "analysis": analysis,
        "markdown": markdown,
        "quality": quality_summary(quality),
    })

    if output_dir:
        path = save_cover_prompt(markdown, output_dir, title)
        result["path"] = path
        if path is None:
            result["ok"] = False
            result["reason"] = "提示词写入文件失败（内容已生成，见日志）"

    return result
