"""
封面历史：扫已经生成过的封面提示词，提炼「构图签名」，供新一篇避让。

为什么不用数据库：

1. 每篇的封面提示词本来就以 {标题}.cover_prompt.md 落在 output/<文章目录>/ 下，
   文件本身就是历史库。再建一层索引只会多一份要同步的状态。
2. 一次最多读最近 3 篇，扫一层目录、读几个文件的开头，成本可以忽略。

签名解析的是 cover_prompt.build_cover_prompt_markdown 确定性写出的字段
（「二、自动选择的风格」和「三、核心视觉概念」两节），所以不依赖模型遵守格式。
任何文件读不到、字段缺失都只是少一条参考，不能影响封面提示词生成。
"""

import logging
import re
from pathlib import Path
from typing import Optional

from .utils import read_file

logger = logging.getLogger("blog_pipeline")

PROJECT_ROOT = Path(__file__).parent.parent

COVER_PROMPT_SUFFIX = ".cover_prompt.md"

# 从交付文件里抽字段用的前缀。这些行由 build_cover_prompt_markdown 写出，
# 改那边的措辞就要同步改这里。
_FIELD_PREFIXES = {
    "style": "- 内部标识：",
    "visual_metaphor": "- 视觉隐喻：",
    "main_visual": "- 主视觉：",
    "composition": "- 构图：",
}

_TITLE_RE = re.compile(r"^#\s*封面提示词\s*[—\-–]\s*(.+?)\s*$", re.MULTILINE)

# 只取文件开头这么多字符。需要的一~四节都在前部，后面是会越来越长的提示词正文。
_HEAD_CHARS = 4000


def parse_cover_signature(path) -> Optional[dict]:
    """从一个 .cover_prompt.md 里提炼构图签名。

    Returns:
        签名 dict；读不到、或者主视觉和构图都没有（没有参考价值）时返回 None
    """
    try:
        text = read_file(str(path))[:_HEAD_CHARS]
    except (OSError, ValueError):
        # 文件被占用、编码不对、权限问题：都只是少一条参考
        logger.debug("封面历史读取失败，跳过: %s", path)
        return None

    sig = {
        "path": str(path),
        "title": "",
        "style": "",
        "visual_metaphor": "",
        "main_visual": "",
        "composition": "",
    }

    m = _TITLE_RE.search(text)
    if m:
        sig["title"] = m.group(1).strip()
    if not sig["title"]:
        # 退回文件名：xxx.cover_prompt.md → xxx
        sig["title"] = Path(path).name[: -len(COVER_PROMPT_SUFFIX)]

    for key, prefix in _FIELD_PREFIXES.items():
        idx = text.find(prefix)
        if idx != -1:
            sig[key] = text[idx + len(prefix):].split("\n", 1)[0].strip()

    if not sig["main_visual"] and not sig["composition"]:
        logger.debug("封面历史里没有可用的构图字段，跳过: %s", path)
        return None
    return sig


def load_recent_covers(
    base_dir,
    *,
    limit: int = 3,
    exclude_dir=None,
    exclude_title: str = "",
) -> list[dict]:
    """取最近生成过的几篇封面签名，按修改时间从新到旧。

    Args:
        base_dir: output 根目录（每个文章一个子目录，结构是平的）
        limit: 最多取几篇；<=0 表示不用历史
        exclude_dir: 要排除的文章目录。回填旧文章时它自己的封面就在同目录下，
            不排除就会「自己和自己比构图」，必然判成雷同
        exclude_title: 标题兜底排除（目录被改名或换过输出路径时还能挡住）

    Returns:
        签名列表；目录不存在、读不到任何一篇都返回空列表
    """
    if limit <= 0 or not base_dir:
        return []

    base = Path(base_dir)
    try:
        if not base.is_dir():
            return []

        excluded = Path(exclude_dir).resolve() if exclude_dir else None
        candidates: list[tuple[float, Path]] = []
        for path in base.glob(f"*/*{COVER_PROMPT_SUFFIX}"):
            try:
                if excluded is not None and path.parent.resolve() == excluded:
                    continue
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue

        candidates.sort(key=lambda item: item[0], reverse=True)

        recent: list[dict] = []
        for _, path in candidates:
            sig = parse_cover_signature(path)
            if sig is None:
                continue
            if exclude_title and sig["title"] == exclude_title:
                continue
            recent.append(sig)
            if len(recent) >= limit:
                break
        return recent

    except Exception:
        # 历史只是参考，任何意外都不能拖垮封面提示词生成
        logger.exception("扫描封面历史失败，按无历史处理: %s", base_dir)
        return []


def history_base_dir(output_dir, config: Optional[dict] = None) -> Optional[Path]:
    """定位历史封面所在目录。

    正常路径是文章输出目录的父目录：output/ 下每篇文章一个子目录，结构是平的，
    这样不用解析配置里的相对路径，也不受启动目录影响。output_dir 为空（只做分析、
    不落盘）时才退回 config 里的 output.base_dir，先按当前目录找，再按项目根找。
    """
    if output_dir:
        return Path(output_dir).resolve().parent

    base = ((config or {}).get("output") or {}).get("base_dir") or "./output"
    candidates = [Path(base), PROJECT_ROOT / base]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def format_recent_note(recent: list[dict]) -> str:
    """把最近几篇的构图写成一段提示，附到第一阶段的分析请求后面。

    作用域必须写死：只影响视觉主体和构图，绝不影响风格选择。否则会变成
    「为了不一样而换风格」，那和「为了统一而全都选一种风格」是同一个错误。
    """
    if not recent:
        return ""

    lines = [
        "【视觉去重参考 —— 只影响视觉主体与构图，绝不影响风格选择】",
        "最近已经生成过的封面（越靠前越新）：",
    ]
    for i, sig in enumerate(recent, 1):
        parts = [f"风格={sig.get('style') or '未知'}"]
        if sig.get("main_visual"):
            parts.append(f"主视觉={sig['main_visual']}")
        if sig.get("composition"):
            parts.append(f"构图={sig['composition']}")
        lines.append(f"{i}. 《{sig.get('title') or '未命名'}》 " + "；".join(parts))

    lines += [
        "要求：",
        "- 风格仍然完全按这篇文章的内容独立判断，不得因为这些历史而改变 recommended_style；",
        "- 如果文章内容允许，换一个同样贴切的视觉主体或构图切入点，"
        "尤其避免主体位置和整体走向与上面几篇雷同；",
        "- 如果这篇文章最合适的方案恰好与上面接近，就以内容为准，不要为了「差异」"
        "牺牲贴合度，也不要为了「差异」新增任何无关元素。",
    ]
    return "\n".join(lines)
