"""
写作者背景。

存在独立文件 data/writer_profile.json，不写回 config.yaml——用程序回写 yaml
会把文件里的注释全部冲掉。路径按项目根目录解析，不受进程 CWD 影响。

这段背景会以「轻度参考」的方式注入写作提示词：只在题材相关时影响举例和视角，
不改变读者定位，也不允许出现作者自述。字段全空时不注入任何东西。
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("blog_pipeline")

PROJECT_ROOT = Path(__file__).parent.parent
PROFILE_PATH = PROJECT_ROOT / "data" / "writer_profile.json"

# 界面上的字段定义。key 用于存取，label 用于展示
FIELDS = [
    {"key": "identity", "label": "身份 / 职业", "placeholder": "例如：后端开发者"},
    {"key": "tech_stack", "label": "技术栈", "placeholder": "例如：Java、Spring Boot、MySQL、Redis、RabbitMQ"},
    {"key": "experience", "label": "经验", "placeholder": "例如：3 年，主要做电商后台"},
    {"key": "extra", "label": "其他补充", "placeholder": "还想让文章体现的角度，例如：偏工程实践，少谈原理推导"},
]

_EMPTY = {f["key"]: "" for f in FIELDS}

# 注入提示词的模板。刻意写得很克制，避免和 skill 里「正文不得出现作者自述」打架。
_PROMPT_TEMPLATE = """## 作者背景（轻度参考）

这篇的写作者是：{summary}

用法要求：
1. 只在题材和这个背景相关时，自然地体现这个视角——比如举更贴近这类开发者日常的例子、
   点出实际项目里容易踩的地方。关系不大就当它不存在
2. 不要以第一人称自述身份或经历，不要写「作为后端开发者」「我在项目里遇到过」这类句子
3. 上面所有写作规则不受影响，不要因为作者背景而改变文章的深度、范围和结构
4. 背景以「略微影响举例和侧重」为限，不要改变文章的主题、结构和结论"""


def load_profile(path: Path = None) -> dict:
    """读取写作者背景。文件不存在或损坏时返回全空。"""
    p = path or PROFILE_PATH
    if not p.is_file():
        return dict(_EMPTY)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取写作者背景失败，按空处理: %s", e)
        return dict(_EMPTY)

    # 只保留已知字段，防止文件里混入别的东西
    return {k: str(data.get(k, "") or "").strip() for k in _EMPTY}


def save_profile(profile: dict, path: Path = None) -> dict:
    """保存写作者背景，返回规范化后的结果。"""
    p = path or PROFILE_PATH
    clean = {k: str(profile.get(k, "") or "").strip() for k in _EMPTY}

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("写作者背景已保存到 %s", p)
    return clean


def is_empty(profile: dict) -> bool:
    """所有字段都为空。"""
    return not any((profile or {}).get(k, "").strip() for k in _EMPTY)


def _summary_parts(profile: dict) -> list[str]:
    """把各字段拼成自然的中文短语，避免「身份 / 职业：xxx」这种啰嗦写法。"""
    parts = []
    identity = (profile.get("identity") or "").strip()
    tech_stack = (profile.get("tech_stack") or "").strip()
    experience = (profile.get("experience") or "").strip()
    extra = (profile.get("extra") or "").strip()

    if identity:
        parts.append(identity)
    if tech_stack:
        parts.append(f"技术栈偏 {tech_stack}")
    if experience:
        parts.append(f"经验：{experience}")
    if extra:
        parts.append(extra)
    return parts


def build_prompt_section(profile: dict = None) -> str:
    """把背景渲染成注入写作提示词的一段文本。全空时返回空串。"""
    profile = profile if profile is not None else load_profile()
    if is_empty(profile):
        return ""

    return _PROMPT_TEMPLATE.format(summary="；".join(_summary_parts(profile)))
