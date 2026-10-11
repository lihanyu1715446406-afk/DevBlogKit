"""
封面提示词的规则检查（第三阶段）。

**纯规则**：不读文件、不调模型、无副作用。所以它可以在修正前后各跑一次，
也方便离线验证。

和 cover_prompt.py 的分工：
- 这个模块只做判定——按 10 项给通过/提示/问题。
- cover_prompt.py 负责一切与模型交互的环节（分析、渲染、修正调用、组装落盘）。

判定用的词表和维护注意事项（都是踩过才知道的坑）都写在下面各常量旁边，
改规则看这里，不要改到 cover_prompt 去。
"""

import re
import unicodedata
from typing import Optional

# 模型没给出可用风格时按这个兜底。保守偏 editorial：
# 没有依据时宁可走排版路线，也不要硬套一个可能对不上文章的抽象隐喻。
# （和 cover_prompt.STYLES 里的 key 对应；这里是纯规则模块，不反向依赖 cover_prompt）
DEFAULT_STYLE = "editorial"


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


