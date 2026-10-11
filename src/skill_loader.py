"""
Skill 加载器：把 ~/.claude/skills 下的真实 skill 文件读成写作提示词。

设计要点：
1. 以真实 skill 文件为主，读不到时回退到 prompts/ 下的蒸馏副本，保证换机器也能跑。
2. 真实 SKILL.md 带 YAML frontmatter，读取时剥掉，只保留正文。
3. 真实 skill 是写给交互式助手的，含"询问用户""等待确认"这类流程说明。
   本管线是单轮生成，必须追加一段声明让模型忽略这些内容，否则可能输出确认请求。
4. 蒸馏副本 prompts/writing.txt 里"结尾可以提下一步方向"等宽松规则与 skill 冲突，
   追加的声明里要保留"以 skill 为准"的优先级，否则规则会互相打架。
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("blog_pipeline")

# 项目根目录（src/ 的上一级）
PROJECT_ROOT = Path(__file__).parent.parent

# 默认 skill 规格。dir 支持 ~ 展开，也支持相对项目根的路径；files 按顺序拼接。
#
# 关于 key 名字：写作规范的内部 key 沿用历史命名 "csdn"，它现在的含义是
# 「正文写作规范 skill」，实际加载的是 learning-tech-blog。
# 之所以不改 key：pipeline.py / writer.py / webui 都按这个名字取用，
# 改名要同时动四五处调用链，收益只是好看一点。对外展示的名字看 SKILL_LABELS。
DEFAULT_SKILL_SPECS = {
    "csdn": {
        # 项目级 skill：放在项目里，随项目一起走，Claude Code 和本脚本读同一份文件。
        # 换机器、拷贝项目都不需要另外装 skill。
        "dir": ".claude/skills/learning-tech-blog",
        "files": ["SKILL.md"],
        # 只有在提供了笔记/截图材料、要写课程笔记文时才追加
        "material_files": ["references/course-note-blogging.md"],
        "fallback": "prompts/blog-style.txt",
    },
    "humanizer": {
        # 去 AI 味仍然用用户级 skill，不随项目走
        "dir": "~/.claude/skills/Humanizer-zh",
        "files": ["SKILL.md"],
        "material_files": [],
        "fallback": "prompts/humanize.txt",
    },
}

# 真实 skill 正文加载后追加的声明。
# 解决两个问题：交互式流程说明会污染单轮生成；蒸馏副本的宽松规则会与 skill 冲突。
_SKILL_PREAMBLE = """
═══════════════════════════════════════════════════════════
以下规则取自本项目的写作规范 skill 文件，对本次文章有约束力。
当这些规则与上文"文章写作规范"冲突时，以本节为准。
（例如：上文允许结尾提"下一步方向"，本节对其做了更严格的限制。）

本次是一次单轮生成任务：请直接输出成品文章的 Markdown。
忽略本节中面向交互式助手的流程性说明——不要向用户提问，不要输出"是否继续""请确认"
这类内容，不要只给架构就停下，不要输出改动清单以外的元信息。
═══════════════════════════════════════════════════════════
"""

# 去 AI 味阶段必须保留的输出契约。
# 真实 Humanizer-zh skill 的"输出格式"要求输出改动总结和质量评分表，
# 与本管线依赖的 CHANGES_MARKER 切分协议不兼容，所以用这份契约覆盖它。
_HUMANIZER_CONTRACT = """
═══════════════════════════════════════════════════════════
以下是本管线的硬性约束和输出格式要求，优先级高于上文任何冲突描述。
═══════════════════════════════════════════════════════════

## 硬性约束（违反任何一条都算失败）

1. **frontmatter 原样保留** —— 文章开头的 YAML frontmatter（--- 之间的部分）必须逐字保留，
   title、date、category、tags 一个字都不要改。原文没有 frontmatter 就不要自己加。
2. **[IMAGE: 描述] 标记全部原样保留** —— 数量和位置都不许变，描述文字也不许改。
3. **代码块原样保留** —— 包括代码里的注释和缩进。不要在代码里加表情、改命名风格。
4. **标题层级和数量不变** —— 不要增删 h2/h3，不要重排章节顺序。
5. **不加不减技术内容** —— 不引入原文没有的技术点，不删掉原文讲过的步骤。
6. **深度和内容不变** —— 改写后不能变得更晦涩，不要引入原文没有的专业术语，
   也不要把技术原理和业务实践的内容改没了。

## 输出格式

先输出改写后的完整文章（Markdown，含 frontmatter，直接从 --- 开始，
不要用 ``` 包裹整个文章），然后另起一行输出这个标记：

<!-- HUMANIZE-CHANGES -->

标记后面跟一个简短的改动清单（5-10 条，每条一行，说明删掉了哪一类 AI 痕迹）。
不要输出质量评分表，不要输出其他说明文字。
"""

# csdn 规则在 writing.txt 之后拼接，留出分隔便于模型区分两套来源
_SKILL_SEPARATOR = "\n\n"


def _strip_frontmatter(text: str) -> str:
    """剥掉 SKILL.md 的 YAML frontmatter，只留正文。"""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text.strip()

    # 找第二个 --- 作为 frontmatter 结束
    lines = stripped.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    return text.strip()


def _resolve_skill_dir(name: str, spec: dict, config: Optional[dict] = None) -> Optional[Path]:
    """解析 skill 目录，优先用 config 里的覆盖值。

    相对路径按项目根解析，这样项目级 skill（.claude/skills/...）不用写死绝对路径，
    项目换目录或换机器都不用改配置。
    """
    configured = None
    if config:
        configured = (
            config.get("skills", {}).get("specs", {}).get(name, {}).get("dir")
        )
    raw = configured or spec.get("dir", "")
    if not raw:
        return None

    path = Path(raw).expanduser()
    if not path.is_absolute() and not raw.startswith("~"):
        path = PROJECT_ROOT / path

    return path if path.is_dir() else None


def _read_skill_files(
    skill_dir: Path, filenames: list[str]
) -> tuple[list[str], list[str]]:
    """读取 skill 文件，返回 (读到的正文列表, 缺失的文件名列表)。"""
    parts, missing = [], []
    for name in filenames:
        path = skill_dir / name
        if not path.is_file():
            missing.append(name)
            continue
        parts.append(_strip_frontmatter(path.read_text(encoding="utf-8")))
    return parts, missing


def _read_fallback(rel_path: str) -> str:
    """读取 prompts/ 下的蒸馏副本。"""
    path = PROJECT_ROOT / rel_path
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def load_skill(
    name: str,
    *,
    config: Optional[dict] = None,
    include_material: bool = False,
) -> tuple[str, str]:
    """加载一个 skill 的完整提示词文本。

    Args:
        name: "csdn" 或 "humanizer"
        config: 配置字典；可用 skills.enabled / skills.specs.<name>.dir 覆盖默认值
        include_material: 是否追加材料相关的参考文件（如 course-note-blogging.md）

    Returns:
        (提示词文本, 来源标识)。来源标识为 "skill" 或 "prompts"，供日志和自检报告使用。
    """
    spec = DEFAULT_SKILL_SPECS.get(name)
    if spec is None:
        raise ValueError(f"未知的 skill: {name}")

    skills_config = (config or {}).get("skills", {})
    mode = skills_config.get("primary", "skill")
    if not skills_config.get("enabled", True) or mode == "prompts":
        return _read_fallback(spec["fallback"]), "prompts"

    skill_dir = _resolve_skill_dir(name, spec, config)
    if skill_dir is None:
        logger.warning(
            "未找到 %s skill 目录 (%s)，回退到 %s",
            name, spec["dir"], spec["fallback"],
        )
        return _read_fallback(spec["fallback"]), "prompts"

    filenames = list(spec["files"])
    if include_material:
        filenames += spec.get("material_files", [])

    parts, missing = _read_skill_files(skill_dir, filenames)
    if not parts:
        logger.warning(
            "%s skill 目录存在但没读到任何文件，回退到 %s", name, spec["fallback"]
        )
        return _read_fallback(spec["fallback"]), "prompts"

    if missing:
        logger.warning("%s skill 缺少文件: %s", name, ", ".join(missing))

    body = _SKILL_SEPARATOR.join(parts)

    if name == "humanizer":
        # 契约必须放在最后，覆盖真实 skill 里冲突的输出格式说明
        body = f"{body}\n{_HUMANIZER_CONTRACT}"
    else:
        body = f"{_SKILL_PREAMBLE}\n{body}"

    logger.info(
        "已加载 %s skill: %s (%d 字符, %d 个文件)",
        name, skill_dir, len(body), len(parts),
    )
    return body, "skill"


def read_skill_body(name: str, config: Optional[dict] = None) -> tuple[str, str]:
    """读 skill 主文件的正文（剥掉 YAML frontmatter），返回 (正文, 文件路径)。

    和 load_skill 的区别：那个返回的是**实际注入模型请求**的形态（前面拼了单轮生成声明，
    humanizer 还追加了输出契约）。界面上要展示的是规范原文，所以这里单独读文件、不做包装。

    读不到 skill 目录时返回兜底副本的正文；两者都没有时返回 ("", 预期路径)。
    """
    spec = DEFAULT_SKILL_SPECS.get(name)
    if spec is None:
        raise ValueError(f"未知的 skill: {name}")

    skill_dir = _resolve_skill_dir(name, spec, config)
    if skill_dir is not None:
        main = skill_dir / spec["files"][0]
        if main.is_file():
            return _strip_frontmatter(main.read_text(encoding="utf-8")), str(main)

    fallback = PROJECT_ROOT / spec["fallback"]
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip(), str(fallback)
    return "", str(PROJECT_ROOT / spec["fallback"])


def describe_skill_source(name: str, config: Optional[dict] = None) -> str:
    """返回 skill 来源的一句话描述，用于启动信息和自检报告。"""
    _, source = load_skill(name, config=config)
    spec = DEFAULT_SKILL_SPECS[name]
    if source == "skill":
        skill_dir = _resolve_skill_dir(name, spec, config)
        return f"{name}: 真实 skill ({skill_dir})"
    return f"{name}: prompts 副本 ({spec['fallback']})"


# 界面上展示用的说明
SKILL_LABELS = {
    "csdn": "learning-tech-blog",
    "humanizer": "Humanizer-zh",
}
SKILL_PURPOSE = {
    "csdn": "正文写作规则：写作定位与优先级、按类型选结构、技术准确性、交付格式与格式门禁",
    "humanizer": "去 AI 味改写规则：改写阶段单独加载一次",
}


def skill_info(
    name: str,
    *,
    config: Optional[dict] = None,
    include_material: bool = False,
) -> dict:
    """返回一个 skill 的结构化信息，供界面展示。

    不返回提示词正文，只返回来源、路径、文件清单和体量。
    """
    spec = DEFAULT_SKILL_SPECS[name]
    info = {
        "key": name,
        "label": SKILL_LABELS.get(name, name),
        "purpose": SKILL_PURPOSE.get(name, ""),
        "source": "prompts",
        "source_label": "prompts 蒸馏副本",
        "path": str(PROJECT_ROOT / spec["fallback"]),
        "files": [],
        "missing": [],
        "chars": 0,
    }

    skills_config = (config or {}).get("skills", {})
    if not skills_config.get("enabled", True) or skills_config.get("primary") == "prompts":
        info["chars"] = len(_read_fallback(spec["fallback"]))
        return info

    skill_dir = _resolve_skill_dir(name, spec, config)
    if skill_dir is None:
        info["chars"] = len(_read_fallback(spec["fallback"]))
        info["missing"] = [f"{skill_dir or spec['dir']} 不存在"]
        return info

    filenames = list(spec["files"])
    if include_material:
        filenames += spec.get("material_files", [])

    parts, missing = _read_skill_files(skill_dir, filenames)
    if not parts:
        info["chars"] = len(_read_fallback(spec["fallback"]))
        info["missing"] = [f"{skill_dir} 下没读到任何文件"]
        return info

    info.update({
        "source": "skill",
        "source_label": "真实 skill 文件",
        "path": str(skill_dir),
        "files": filenames,
        "missing": missing,
        "chars": sum(len(p) for p in parts),
    })
    return info


def all_skill_info(config: Optional[dict] = None, **kwargs) -> list[dict]:
    """返回所有 skill 的信息，顺序同写作流程。"""
    return [skill_info(n, config=config, **kwargs) for n in ("csdn", "humanizer")]
