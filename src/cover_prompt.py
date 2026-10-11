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
from .llm_client import LLMClient
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

# 模型没给出可用风格时按这个兜底。保守偏 editorial：
# 没有依据时宁可走排版路线，也不要硬套一个可能对不上文章的抽象隐喻。
DEFAULT_STYLE = "editorial"

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

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n")

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


def _strip_fence(text: str) -> str:
    """去掉模型可能给整段内容套上的 ``` 代码围栏。"""
    text = (text or "").strip()
    if _FENCE_RE.match(text):
        text = _FENCE_RE.sub("", text, count=1)
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


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
    cleaned = _strip_fence(text or "")
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
    text = _strip_fence(text or "").strip()
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
    return _strip_fence(text or "")


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
# 第三阶段：质量检查（纯规则，零额外模型调用）
#
# 在渲染之后跑一次，按 10 项逐条检查。命中 fail 级问题才允许做一次针对性修正
# （repair_cover_prompt），修正稿还要再查一遍、确实变好才采纳。
#
# 大部分项只给 warn：规则判断不了「这个隐喻好不好」，但能抓住确定性矛盾——
# 标题丢了、构图和上一篇撞了、画面里冒出机器人。宁可少报，也不要把正常提示词
# 判成问题：每一次误报都可能换来一次多余的付费调用。
# ============================================================

# 交付文件「七、质量检查」里逐行展示的检查项
QUALITY_ITEMS = (
    (1, "核心主题理解"),
    (2, "风格与内容匹配"),
    (3, "核心视觉主体"),
    (4, "主体表达技术概念"),
    (5, "构图与空间关系"),
    (6, "配色克制协调"),
    (7, "留白充足"),
    (8, "无无关装饰"),
    (9, "文字不过量"),
    (10, "构图不与最近封面撞车"),
)

# 阈值。可以在 config.yaml 的 cover_prompt.quality_limits 里覆盖其中任意一项。
_QUALITY_LIMITS = {
    "min_core_chars": 12,          # 核心观点 / 关键机制的字数下限
    "min_style_reason_chars": 20,  # 风格选择理由的字数下限
    "min_main_visual_chars": 15,   # 主视觉描述的字数下限
    "min_composition_chars": 20,   # 构图描述的字数下限
    "min_spatial_tokens": 2,       # 构图里至少要出现的方位词个数
    "max_palette_colors": 3,       # 品牌规范：2-3 种主色
    # 引文块数量相对「主标题行数 + 副标题」的容差。取 2 是因为提示词里常会再引一次
    # 核心短语或副标题的变体（真实产物实测 2~4 处），卡太紧只会刷出无用的提示。
    "quoted_tolerance": 2,
    "min_concept_overlap": 2,      # 主视觉与关键机制的三元组重叠下限
}

# 画面里不该出现的万能装饰。注意「二进制」不在表里——「RDB 二进制快照」是正当表述。
#
# 这批词几乎每份**正确**的提示词都会出现，因为 RENDER_CONTRACT 要求明确列出不许出现的
# 东西。所以只能走 _affirmative_terms 的否定作用域判定，直接关键词匹配会 100% 误报。
_BANNED_DECOR = (
    "机器人", "芯片", "电路板", "代码窗口", "代码屏幕", "代码界面",
    "霓虹", "全息", "赛博", "赛博朋克", "光晕", "无人机",
    "二进制雨", "矩阵雨", "数据流瀑",
)

# 高饱和用色。同样要过否定作用域：真实产物里「不要大面积彩色」是合格写法。
_SATURATION_TERMS = ("彩虹", "荧光", "多彩", "大面积彩色", "霓虹渐变")

# 否定词。出现在禁用词**之前**，就说明这个禁用词是被禁止的，不是被要求的。
_NEGATION_TOKENS = (
    "不要", "不许", "不得", "不能", "不可", "不应", "不出现", "不含", "不加",
    "不用", "不再是", "禁止", "避免", "杜绝", "切勿", "别", "勿", "没有", "无",
    "而非", "不是",
)

# 子句切分。刻意不切顿号「、」：禁用词列表「不要出现机器人、芯片、电路板」是靠顿号
# 连起来的，切开会让「芯片」脱离前面的否定词，被误判成违规。
_CLAUSE_SEP_RE = re.compile(r"[。；;！!？?\n，,]")

# 空间关系词（第 5 项）
_SPATIAL_TOKENS = (
    "左", "右", "上", "下", "中央", "居中", "中心", "角", "一侧", "边缘", "纵",
    "横", "水平", "垂直", "对角", "贯穿", "分栏", "三分", "占比", "位于", "排列",
    "对齐",
)

# 形状/结构词（第 3 项失分点：说了半天没说清画的是什么形状）
_SHAPE_TOKENS = (
    "圆", "线", "方", "框", "环", "弧", "角", "条", "块", "带", "格", "树",
    "节点", "箭头", "分区", "层", "轴", "链", "螺旋", "网", "点", "面",
)

# 风格应该配得上的版面语言（第 2 项）
_LAYOUT_TOKENS = (
    "排版", "分栏", "栅格", "网格", "对齐", "字号", "字重", "标题区", "留白",
    "分隔线",
)
_RELATION_TOKENS = (
    "左", "右", "上", "下", "中央", "贯穿", "围绕", "相连", "环", "层", "节点",
    "路径", "方向",
)

# 留白相关（第 7 项）
_WHITESPACE_TOKENS = ("留白", "空白", "留出", "预留区", "呼吸", "无遮挡")
_SPACE_RATIO_TOKENS = ("三分之一", "五分之", "占比", "大块", "大面积", "约占", "半幅")

# 风格固定名。RENDER_CONTRACT 要求提示词开头点明风格且写法固定，
# 所以「提示词里出现的风格名」和「analysis 选的风格」必须一致（第 2 项）。
_STYLE_LABELS = {
    "editorial": ("高级技术杂志风", "premium technical editorial"),
    "abstract": ("抽象概念视觉风", "abstract technical conceptualism"),
}

# 构图骨架：主体在哪 + 整体什么走向，用来跨篇查重（第 10 项）。
#
# 这里**不用**字符相似度：实测最近 4 篇两两 bigram Jaccard 只有 0.095~0.132，
# 哪怕其中 3 篇构图骨架完全一样也分不出来——这个指标被题材词汇淹没了。
# 位置词按首次出现归一，「画面中央偏右」算「中央」（重心仍在中间）。
_POSITION_WORDS = (
    ("中央", ("中央", "居中", "中心", "正中")),
    ("左", ("左",)),
    ("右", ("右",)),
    ("上", ("上",)),
    ("下", ("下",)),
    ("角", ("角",)),
    ("满幅", ("满幅", "铺满", "全屏")),
)
_DIRECTION_WORDS = ("水平", "纵向", "垂直", "对角", "环形", "螺旋", "放射", "网格", "分层")

# 判断「无文字方案里是不是真的要求渲染标题」时的两个判据：
# 提到预留区/排版而顺带写了标题名，不算违规（_RESERVE_MARKERS）；
# 只是交代画面主题（「主题是『TCP 粘包』」）也不算，要有要求写字的动词才作数。
_RESERVE_MARKERS = ("预留", "留白", "排版", "位置", "空白", "人工", "供")
_TEXT_RENDER_MARKERS = (
    "文字为", "文字是", "文字内容", "写着", "写上", "写上文字", "放一行", "放上",
    "标注", "字样", "大标题", "标题文字", "呈现文字", "渲染", "显示文字",
    "出现文字", "一行大字", "排一行",
)

_QUOTE_RE = re.compile(r"[「『“\"]([^」』”\"]{1,30})[」』”\"]")


def _as_clean(text) -> str:
    """把字段规整成去空白的字符串。"""
    return str(text or "").strip()


def _split_clauses(text: str) -> list[str]:
    """按句读切子句（不切顿号，原因见 _CLAUSE_SEP_RE）。"""
    return [c for c in _CLAUSE_SEP_RE.split(text or "") if c.strip()]


def _clause_negates(clause: str, term_pos: int) -> bool:
    """这个子句是不是在「禁止」某个词。

    两种常见写法都要认：
    - 否定词在禁用词前面：「不要出现机器人、芯片、电路板」；
    - 否定词挂在子句末尾：「机器人也不要有」。
    """
    if any(tok in clause[:term_pos] for tok in _NEGATION_TOKENS):
        return True
    tail = clause[term_pos + 1:]
    return any(tok in tail for tok in ("不要", "不出现", "不得", "不能", "没有", "也不用"))


def _affirmative_terms(text: str, terms) -> list[str]:
    """找出出现在「非否定子句」里的词。

    这是第 8 项的防误报核心：正确提示词里那句话本身就是「不要出现机器人、芯片」，
    朴素关键词匹配会把每一份合格提示词都判成违规。
    """
    hits: list[str] = []
    for clause in _split_clauses(text):
        for term in terms:
            pos = clause.find(term)
            if pos == -1 or term in hits:
                continue
            if _clause_negates(clause, pos):
                continue
            hits.append(term)
    return hits


def _norm_for_match(text: str) -> str:
    """归一化到「只比字」，用于判断标题有没有出现在提示词里。

    渲染模型经常给标题换行、加空格、用全角标点，或者改动英文大小写
    （真实产物里「Redis 持久化 / RDB 还是 AOF」就是两行）。直接 in 判断会每次都
    判成「标题丢了」，然后每篇都触发一次付费修正——这是最典型的慢性烧钱故障。
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(
        r"[\s　·・,，。.、；;：:！!？?\"'“”‘’「」『』（）()\[\]【】<>《》—\-—_/\\|]+",
        "",
        text,
    )


def _title_leaked(title: str, no_text_prompt: str) -> bool:
    """无文字方案里是不是真的要求渲染标题。

    无文字方案几乎都会交代「主题是『TCP 粘包』」这类画面主题，甚至把标题原名写进
    说明里——那是给模型的背景说明，不是渲染要求。所以只有三条同时满足才算违规：
    引号里恰好是标题某一行 + 这句出现了「要求写字」的动词 + 这句不是在讲标题预留区。

    宁可漏判也不误判：这里误判一次就是一次多余的付费修正。
    """
    lines = [ln.strip() for ln in (title or "").splitlines() if ln.strip()]
    if not lines:
        return False
    targets = {_norm_for_match(ln) for ln in lines}

    for clause in _split_clauses(no_text_prompt):
        if any(marker in clause for marker in _RESERVE_MARKERS):
            continue
        if not any(marker in clause for marker in _TEXT_RENDER_MARKERS):
            continue
        for span in _QUOTE_RE.findall(clause):
            if _norm_for_match(span) in targets:
                return True
    return False


def _composition_skeleton(sig: dict) -> tuple[str, str]:
    """取「主体在哪 + 整体什么走向」，用作跨篇查重的骨架。

    取每个字段里最先出现的那个词；取不到就给空串，空串不参与比对。
    """
    text = f"{sig.get('main_visual') or ''}；{sig.get('composition') or ''}"

    position, best = "", len(text) + 1
    for canon, words in _POSITION_WORDS:
        for word in words:
            idx = text.find(word)
            if idx != -1 and idx < best:
                best, position = idx, canon

    direction, best = "", len(text) + 1
    for word in _DIRECTION_WORDS:
        idx = text.find(word)
        if idx != -1 and idx < best:
            best, direction = idx, word

    return position, direction


def _style_label_conflict(style: str, text: str) -> str:
    """提示词里是否写了另一种风格的固定名。返回冲突说明，没有冲突返回空串。"""
    lowered = (text or "").lower()
    for key, labels in _STYLE_LABELS.items():
        if key == style:
            continue
        for label in labels:
            if label in lowered:
                return f"提示词里出现了「{label}」，但分析选的风格是 {style}"
    return ""


def _style_fits_composition(style: str, composition: str) -> bool:
    """构图描述是否用上了该风格应有的版面语言。"""
    if not composition:
        return False
    tokens = _LAYOUT_TOKENS if style == "editorial" else _RELATION_TOKENS
    return any(t in composition for t in tokens)


def _similarity_status(analysis: dict, recent: list[dict]) -> tuple[str, str]:
    """第 10 项：新一篇的构图骨架有没有和最近几篇撞上。"""
    if not recent:
        return "pass", "暂无历史封面可比对"

    position, direction = _composition_skeleton(analysis)
    if not position and not direction:
        return "warn", "本篇没有可识别的构图骨架，跳过跨篇比对"

    worst = "pass"
    detail = f"与最近 {len(recent)} 篇的构图骨架都不重合"
    for sig in recent:
        rpos, rdir = _composition_skeleton(sig)
        if not rpos and not rdir:
            continue
        title = sig.get("title") or "未命名"
        if position and direction and position == rpos and direction == rdir:
            return "fail", f"与最近《{title}》构图骨架相同（{position} + {direction}）"
        if (position and position == rpos) or (direction and direction == rdir):
            worst = "warn"
            detail = f"与最近《{title}》部分重合（位置或走向有一项相同）"
    return worst, detail


def check_cover_quality(
    analysis: dict,
    main_prompt: str,
    no_text_prompt: str,
    *,
    recent_covers: Optional[list] = None,
    limits: Optional[dict] = None,
) -> dict:
    """按 10 项做规则检查。

    纯函数：不读文件、不调模型，所以修正前后各跑一次很便宜，也能离线验证。
    历史封面由调用方传进来，检查本身不碰磁盘。

    Returns:
        {
            "passed": bool,           # 没有 fail 级问题
            "total": int,             # 检查项总数
            "fail_ids": [...],        # 触发修正的项
            "warn_ids": [...],
            "checks": [{"id","name","status","detail"}, ...],
        }
    """
    limits = {**_QUALITY_LIMITS, **(limits or {})}
    recent = list(recent_covers or [])
    main_prompt = main_prompt or ""
    no_text_prompt = no_text_prompt or ""
    text_all = f"{main_prompt}\n{no_text_prompt}"

    text_plan = analysis.get("text_plan") or {}
    core = _as_clean(analysis.get("core_message"))
    mechanism = _as_clean(analysis.get("key_mechanism"))
    style = analysis.get("recommended_style", DEFAULT_STYLE)
    main_visual = _as_clean(analysis.get("main_visual"))
    metaphor = _as_clean(analysis.get("visual_metaphor"))
    composition = _as_clean(analysis.get("composition"))
    palette = analysis.get("color_palette") or []
    supporting = analysis.get("supporting_elements") or []

    checks: list[dict] = []

    def add(item_id: int, status: str, detail: str):
        checks.append({
            "id": item_id,
            "name": dict(QUALITY_ITEMS)[item_id],
            "status": status,
            "detail": detail,
        })

    # ---- 1. 是否准确理解文章的核心主题 ----
    if not core or not mechanism:
        add(1, "fail", "核心观点或关键机制为空，分析阶段没读懂这篇文章")
    elif min(len(core), len(mechanism)) < limits["min_core_chars"]:
        add(1, "warn", f"核心观点/关键机制偏短（{len(core)}/{len(mechanism)} 字），可能没吃透")
    else:
        add(1, "pass", "核心观点与关键机制都已提炼")

    # ---- 2. 风格是否与文章内容匹配 ----
    reason = _as_clean(analysis.get("style_reason"))
    conflict = _style_label_conflict(style, text_all)
    if conflict:
        add(2, "fail", conflict)
    elif len(reason) < limits["min_style_reason_chars"]:
        add(2, "warn", "风格选择理由过短，看不出判断依据")
    elif not _style_fits_composition(style, composition):
        add(2, "warn", f"{style} 的构图描述里缺少该风格应有的版面语言")
    else:
        add(2, "pass", f"{style}，选择理由具体且与构图自洽")

    # ---- 3. 是否确定了具体的核心视觉主体 ----
    if len(main_visual) < limits["min_main_visual_chars"]:
        add(3, "fail", f"主视觉过短或缺失（{len(main_visual)} 字），没有确定的核心视觉主体")
    elif not any(t in main_visual for t in _SHAPE_TOKENS):
        add(3, "warn", "主视觉里看不出具体的形状或结构")
    else:
        add(3, "pass", "主视觉具体到形状与结构")

    # ---- 4. 视觉主体是否真正表达技术概念 ----
    # 纯规则判断不了「隐喻贴不贴切」，只能查主视觉和关键机制有没有共同的表述。
    # 用三元组而不是关键词：真实产物里主视觉几乎不会出现 TCP / Redis 这类英文缩写
    # （它们只在标题里），按关键词判会把合格产物全部报警。实测 4 份合格产物的三元组
    # 重叠是 9~17，取 2 作阈值很宽松，只拦「主视觉和这篇技术概念毫无关系」的情况。
    shared = _trigrams(f"{main_visual}{metaphor}") & _trigrams(f"{mechanism}{core}")
    if not main_visual:
        add(4, "warn", "没有主视觉可判断")
    elif len(shared) >= limits["min_concept_overlap"]:
        add(4, "pass", "主视觉用到了文章自身的技术表述")
    else:
        add(4, "warn", "主视觉与关键机制没有共同的表述，确认它真的在表达这篇文章的技术概念")

    # ---- 5. 是否明确描述了构图和空间关系 ----
    spatial_hits = [t for t in _SPATIAL_TOKENS if t in composition]
    if len(composition) < limits["min_composition_chars"]:
        add(5, "fail", "构图缺失或过短，没说清主体、标题和留白的位置关系")
    elif len(spatial_hits) < limits["min_spatial_tokens"]:
        add(5, "warn", "构图描述里方位词偏少，空间关系不够明确")
    else:
        add(5, "pass", f"构图含明确的方位与空间关系（命中 {len(spatial_hits)} 个方位词）")

    # ---- 6. 配色是否克制协调 ----
    # 刻意不数提示词里的 hex 色值个数：合格产物会用 3 主色 + 2 辅助色共 5 个 hex
    # 来描述渐变，数 hex 会把完全合格的提示词判成配色超标。
    hot = _affirmative_terms(text_all, _SATURATION_TERMS)
    if hot:
        add(6, "fail", f"提示词里出现肯定的高饱和用色要求：{'、'.join(hot)}")
    elif not 2 <= len(palette) <= limits["max_palette_colors"]:
        add(6, "warn", f"主色数量 {len(palette)} 个，品牌规范是 2-3 个")
    else:
        add(6, "pass", f"{len(palette)} 个主色，符合克制的要求")

    # ---- 7. 是否有充足的留白 ----
    has_whitespace = any(t in text_all for t in _WHITESPACE_TOKENS)
    if not has_whitespace:
        add(7, "fail", "两份提示词里都没有留白要求")
    elif not any(t in text_all for t in _SPACE_RATIO_TOKENS):
        add(7, "warn", "提到了留白但没交代占多大比例")
    else:
        add(7, "pass", "留白位置和占比都有交代")

    # ---- 8. 是否存在无关的芯片、机器人、代码窗口等装饰 ----
    # 提示词侧要过否定作用域；分析侧的 main_visual / supporting_elements 是要
    # 「画进画面」的肯定语义，直接查即可。
    decor = _affirmative_terms(text_all, _BANNED_DECOR)
    for term in _BANNED_DECOR:
        if term in decor:
            continue
        if term in main_visual or term in metaphor or any(term in e for e in supporting):
            decor.append(term)
    if decor:
        add(8, "fail", f"出现了无关的通用装饰：{'、'.join(decor)}")
    else:
        add(8, "pass", "没有机器人、芯片、代码窗口这类通用装饰")

    # ---- 9. 是否出现大量没有必要的文字 ----
    title = _as_clean(text_plan.get("main_title"))
    subtitle = _as_clean(text_plan.get("subtitle"))
    title_lines = [ln for ln in title.splitlines() if ln.strip()]
    if not title:
        add(9, "fail", "分析结果里没有封面主标题")
    elif _norm_for_match(title) not in _norm_for_match(main_prompt):
        add(9, "fail", f"含标题的提示词里找不到主标题「{title}」，标题丢了")
    elif _title_leaked(title, no_text_prompt):
        add(9, "fail", "无文字方案里仍然要求渲染主标题")
    else:
        allowed = len(title_lines) + (1 if subtitle else 0) + limits["quoted_tolerance"]
        quoted = {q.strip() for q in _QUOTE_RE.findall(main_prompt) if q.strip()}
        if len(quoted) > allowed:
            add(9, "warn", f"画面上要求的文字块有 {len(quoted)} 处，"
                           f"标题本身只占 {len(title_lines) + (1 if subtitle else 0)} 处，确认没有多余文字")
        else:
            add(9, "pass", f"文字只用于指定标题（{len(quoted)} 处引文）")

    # ---- 10. 是否与最近生成的封面在构图上过于相似 ----
    status, detail = _similarity_status(analysis, recent)
    add(10, status, detail)

    fail_ids = [c["id"] for c in checks if c["status"] == "fail"]
    warn_ids = [c["id"] for c in checks if c["status"] == "warn"]
    return {
        "passed": not fail_ids,
        "total": len(QUALITY_ITEMS),
        "fail_ids": fail_ids,
        "warn_ids": warn_ids,
        "checks": checks,
    }


def _trigrams(text: str) -> set[str]:
    """取字符三元组，用于判断两段中文描述有没有共同表述。

    不做分词（不引入 jieba 这类依赖），三元组在中文里足够稳定。
    """
    text = text or ""
    return {text[i:i + 3] for i in range(len(text) - 2)}


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
