"""
流水线编排：材料 → 判型 → 写作 → 去 AI 味 → 自检 → 插图 → 封面提示词 → 组装。

这一层不碰终端，也不依赖任何界面框架。进度通过 on_progress 回调往外抛，
大纲确认通过 confirm_outline 回调往外抛，所以命令行和网页界面可以复用同一套逻辑：

- 命令行（run.py）把事件映射回原来的终端文案，confirm_outline 用 input()
- 网页界面（webui/）把事件塞进队列推给浏览器，confirm_outline 等前端点按钮
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config_loader import DEFAULT_CATEGORY, DEFAULT_TAGS, load_config
from .cover_prompt import generate_cover_prompt
from .formatter import assemble_output, save_outline
from .humanizer import humanize_article, save_humanize_report
from .image_generator import ImageGenerator
from .material_reader import read_materials
from .selfcheck import build_repair_instruction, check_article, save_report
from .skill_loader import describe_skill_source
from .utils import unique_output_dir
from .writer import (
    ARTICLE_TYPES,
    resolve_article_type,
    revise_article,
    write_article,
)

logger = logging.getLogger("blog_pipeline")

# 自检最多重写几次。一次通常够用，多了会反复改动正文反而引入新问题。
MAX_SELFCHECK_RETRIES = 1


@dataclass
class ProgressEvent:
    """一条进度事件。

    文案字段刻意保持和原来的终端输出一致，命令行侧直接打印即可还原原样。

    Attributes:
        stage: 阶段标识，界面用来分组或显示进度
        level: info | success | warn | error
        message: 展示用文案（行内容已含缩进，banner 只含标题）
        banner: True 表示这是阶段标题，终端会加 ==== 边框
        raw: True 表示 message 要原样输出（大纲正文）
        data: 机器可读的附带信息，如 title、counts、paths
    """

    stage: str
    level: str = "info"
    message: str = ""
    banner: bool = False
    raw: bool = False
    data: dict = field(default_factory=dict)


def _emit(
    on_progress: Optional[Callable[[ProgressEvent], None]],
    stage: str,
    message: str,
    *,
    level: str = "info",
    banner: bool = False,
    raw: bool = False,
    **data,
):
    """抛一条进度事件。没有回调时什么都不做。"""
    if on_progress is None:
        return
    try:
        on_progress(
            ProgressEvent(
                stage=stage,
                level=level,
                message=message,
                banner=banner,
                raw=raw,
                data=data,
            )
        )
    except Exception:
        # 界面侧的异常不能拖垮生成流程
        logger.exception("进度回调抛出异常，已忽略")


async def run_pipeline(
    topic: str,
    *,
    category: str = None,
    tags: list[str] = None,
    output_dir: str = None,
    fast: bool = False,
    no_images: bool = False,
    humanize: bool = True,
    materials: list[str] = None,
    article_type: str = "auto",
    review: bool = False,
    selfcheck: bool = True,
    cover_prompt: bool = True,
    on_progress: Optional[Callable[[ProgressEvent], None]] = None,
    confirm_outline: Optional[Callable[[str], Awaitable[bool]]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> dict:
    """执行完整的文章生产流水线。

    Args:
        topic: 题材
        category: 分类（覆盖默认值）
        tags: 标签（覆盖默认值）
        output_dir: 输出目录，None 时按 日期-标题slug 自动生成（撞名会加 -2）
        fast: True = 跳过大纲阶段
        no_images: True = 跳过插图生成
        humanize: True = 做去 AI 味改写
        materials: 笔记文件或截图目录路径列表
        article_type: auto | knowledge | course-note | feature
        review: True = 写正文前先把大纲交出去确认
        selfcheck: True = 做交付前自检
        cover_prompt: True = 按定稿正文生成封面提示词文件（只出文本，不出图）
        on_progress: 进度回调，None 表示不关心进度
        confirm_outline: 大纲确认回调，返回 True 继续、False 取消。
            review=True 但没传这个回调时按「继续」处理
        is_cancelled: 取消查询回调。只在阶段边界检查，
            正在飞行的模型调用不能中断，所以取消是「下一个阶段开始时生效」

    Returns:
        {
            "cancelled": bool,          # 大纲被否或中途取消时为 True
            "output_path": str | None,  # index.md 路径
            "title": str,
            "article_type": str,        # 内部 key
            "article_type_name": str,   # 中文名
            "outline": str | None,
            "article": str,
            "counts": dict | None,      # 自检统计
            "selfcheck_ok": bool | None,
            "problems": list,
            "humanized": bool,
            "images": {"reason": str, "cover": str | None, "paths": list},
            "cover_prompt": {"ok": bool, "path": str | None,
                             "style": str, "style_name": str, "reason": str,
                             "quality": dict | None},
            "skill_source": str,
        }
    """
    config = load_config()

    # config 里关掉了就一律不做，调用方只负责显式跳过
    humanize = humanize and config.get("humanize", {}).get("enabled", True)
    selfcheck = selfcheck and config.get("selfcheck", {}).get("enabled", True)
    cover_prompt = (
        cover_prompt and config.get("cover_prompt", {}).get("enabled", True)
    )

    result: dict = {
        "cancelled": False,
        "output_path": None,
        "title": topic,
        "article_type": article_type,
        "article_type_name": "",
        "outline": None,
        "article": "",
        "counts": None,
        "selfcheck_ok": None,
        "problems": [],
        "humanized": False,
        "images": {"reason": "", "cover": None, "paths": []},
        "cover_prompt": {
            "ok": False, "path": None, "style": "",
            "style_name": "", "reason": "skipped",
        },
        "skill_source": "unknown",
    }

    def _cancelled() -> bool:
        """查一次取消状态。回调异常时按「未取消」处理，不能因为界面出错就中断生成。"""
        if is_cancelled is None:
            return False
        try:
            return bool(is_cancelled())
        except Exception:
            logger.exception("取消查询回调抛出异常，按未取消处理")
            return False

    def _stop_if_cancelled() -> bool:
        """阶段边界处检查取消。返回 True 表示调用方应该直接 return。"""
        if not _cancelled():
            return False
        _emit(
            on_progress,
            "cancelled",
            "\n  ⏹️ 已取消（在阶段边界生效）",
            level="warn",
        )
        result["cancelled"] = True
        return True

    _emit(on_progress, "start", "📝 开始写作...", banner=True)

    for name in ("csdn", "humanizer"):
        _emit(
            on_progress,
            "start",
            f"  规则来源 {describe_skill_source(name, config)}",
        )

    # 1. 读取材料（笔记 / 截图）
    material_text = ""
    if materials:
        _emit(on_progress, "material", "\n  📚 读取材料...")
        mat = await read_materials(materials, config=config)
        material_text = mat["text"]
        _emit(
            on_progress,
            "material",
            f"  ✅ 文本 {len(mat['text_files'])} 个，"
            f"图片 {len(mat['image_files'])} 张",
            level="success",
            text_files=mat["text_files"],
            image_files=mat["image_files"],
            errors=mat["errors"],
        )
        for err in mat["errors"]:
            _emit(on_progress, "material", f"  ⚠️ {err}", level="warn")
        if not material_text.strip():
            _emit(
                on_progress,
                "material",
                "  ⚠️ 材料为空，将按无材料处理",
                level="warn",
            )

    # 2. 判型
    if _stop_if_cancelled():
        return result
    resolved_type = resolve_article_type(article_type, material_text)
    type_name = ARTICLE_TYPES[resolved_type][0]
    result["article_type"] = resolved_type
    result["article_type_name"] = type_name
    _emit(
        on_progress,
        "type",
        f"\n  🏷️ 文章类型: {type_name}",
        article_type=resolved_type,
        article_type_name=type_name,
    )

    # 3. 写作
    write_result = await write_article(
        topic,
        category=category,
        tags=tags,
        material=material_text,
        article_type=resolved_type,
        skip_outline=fast,
    )

    title = write_result["title"]
    article = write_result["article"]
    image_descriptions = write_result["image_descriptions"]
    outline = write_result.get("outline")
    skill_source = write_result.get("skill_source", "unknown")

    result["title"] = title
    result["outline"] = outline
    result["article"] = article
    result["skill_source"] = skill_source

    _emit(
        on_progress,
        "writing",
        f"  写作完成：{title}（{len(article)} 字符，"
        f"{len(image_descriptions)} 处插图标记）",
        level="success",
        title=title,
        chars=len(article),
        image_count=len(image_descriptions),
    )

    if _stop_if_cancelled():
        return result

    # 4. 大纲确认
    if review and outline:
        _emit(
            on_progress,
            "outline",
            "📋 大纲（确认后继续写正文）",
            banner=True,
        )
        _emit(on_progress, "outline", outline, raw=True, outline=outline)
        _emit(on_progress, "outline", "")

        approved = True
        if confirm_outline is not None:
            approved = await confirm_outline(outline)

        if not approved:
            _emit(
                on_progress,
                "cancelled",
                "\n  已取消。大纲未保存。",
                level="warn",
            )
            result["cancelled"] = True
            return result

    # 5. 去 AI 味
    if _stop_if_cancelled():
        return result
    humanize_changes = None
    if humanize:
        _emit(on_progress, "humanize", "🧹 去 AI 味...", banner=True)
        before_len = len(article)
        h_result = await humanize_article(article, config=config)

        if h_result["humanized"]:
            article = h_result["article"]
            humanize_changes = h_result["changes"]
            result["humanized"] = True
            _emit(
                on_progress,
                "humanize",
                f"  ✅ 改写完成: {before_len} → {len(article)} 字符",
                level="success",
                before=before_len,
                after=len(article),
            )
            if humanize_changes:
                _emit(
                    on_progress, "humanize", "  📝 改动清单已生成"
                )
        else:
            _emit(
                on_progress,
                "humanize",
                f"  ⚠️ 已回退到原文: {h_result['reason']}",
                level="warn",
                reason=h_result["reason"],
            )
    else:
        _emit(
            on_progress,
            "humanize",
            "\n  ⏭️ 已跳过去 AI 味改写",
            level="info",
        )

    # 确定输出目录（保持在去 AI 味之后算，与原来的顺序一致）
    if output_dir is None:
        today = date.today().strftime("%Y-%m-%d")
        art_dir = unique_output_dir(
            config["output"]["base_dir"], today, title
        )
        output_dir = str(art_dir)

    art_dir = Path(output_dir)
    art_dir.mkdir(parents=True, exist_ok=True)

    # 6. 交付前自检，不合格回喂重写一次
    if _stop_if_cancelled():
        return result
    check_result = None
    if selfcheck:
        _emit(on_progress, "selfcheck", "🔍 交付前自检...", banner=True)
        limits = config.get("selfcheck", {}).get("limits", {})
        check_result = check_article(article, limits=limits)
        _emit(
            on_progress,
            "selfcheck",
            f"  引用 {check_result['counts']['quote']} / "
            f"反引号 {check_result['counts']['inline_code']} / "
            f"加粗 {check_result['counts']['bold']} / "
            f"前言 {check_result['intro_len']} 字",
            counts=check_result["counts"],
            intro_len=check_result["intro_len"],
        )

        repaired = False
        for attempt in range(MAX_SELFCHECK_RETRIES):
            if check_result["ok"]:
                break
            repaired = True
            _emit(
                on_progress,
                "selfcheck",
                f"  ⚠️ 未通过，第 {attempt + 1} 次修正：",
                level="warn",
            )
            for p in check_result["problems"]:
                _emit(
                    on_progress,
                    "selfcheck",
                    f"     - {p['label']}: {p['detail'][:80]}",
                    level="warn",
                )

            article = await revise_article(
                article,
                build_repair_instruction(check_result, limits),
                article_type=resolved_type,
                has_material=bool(material_text.strip()),
                category=category,
                tags=tags,
            )
            check_result = check_article(article, limits=limits)

        result["selfcheck_ok"] = check_result["ok"]
        result["problems"] = check_result["problems"]
        result["counts"] = check_result["counts"]

        if check_result["ok"]:
            _emit(
                on_progress,
                "selfcheck",
                "  ✅ 修正后通过" if repaired else "  ✅ 通过",
                level="success",
                repaired=repaired,
            )
        else:
            _emit(
                on_progress,
                "selfcheck",
                "  ⚠️ 仍有问题，已写入自检报告供人工处理",
                level="warn",
                problems=check_result["problems"],
            )
    else:
        _emit(
            on_progress,
            "selfcheck",
            "\n  ⏭️ 已跳过交付前自检",
        )

    result["article"] = article

    # 保存大纲
    if outline:
        save_outline(outline, str(art_dir))
        _emit(on_progress, "save", "  📋 大纲已保存")

    # 保存去 AI 味改动清单
    if humanize_changes:
        save_humanize_report(humanize_changes, str(art_dir))
        _emit(on_progress, "save", "  🧹 改动清单已保存")

    # 7. 生成插图
    if _stop_if_cancelled():
        return result
    cover_path = None
    illustration_paths = []
    suggestion_reason = "cap"

    if not no_images:
        _emit(on_progress, "images", "🎨 生成插图...", banner=True)
        img_gen = ImageGenerator(config)

        if img_gen.disabled:
            suggestion_reason = "manual"
            _emit(
                on_progress,
                "images",
                "  ℹ️ 已设为手动配图（image.provider=none）",
            )
            _emit(
                on_progress,
                "images",
                "  💡 文章中的插图位置将输出配图建议",
            )
        elif img_gen.is_available():
            if img_gen.want_cover:
                _emit(on_progress, "images", "  生成封面图...")
                summary = article[:200]
                cover_path = await img_gen.generate_cover(
                    title, summary, str(art_dir)
                )
                if cover_path:
                    _emit(
                        on_progress,
                        "images",
                        f"  ✅ 封面图: {cover_path}",
                        level="success",
                        cover=cover_path,
                    )
                else:
                    _emit(
                        on_progress,
                        "images",
                        "  ⚠️ 封面图生成失败",
                        level="warn",
                    )

            if img_gen.want_illustrations and image_descriptions:
                _emit(
                    on_progress,
                    "images",
                    f"\n  生成 {len(image_descriptions)} 张文中插图...",
                )
                illustration_paths = await img_gen.generate_illustrations(
                    image_descriptions, str(art_dir)
                )
                success = sum(1 for p in illustration_paths if p)
                _emit(
                    on_progress,
                    "images",
                    f"  ✅ 插图: {success}/{len(image_descriptions)} 张成功",
                    level="success",
                    success=success,
                    total=len(image_descriptions),
                )
                if len(image_descriptions) > img_gen.max_illustrations:
                    _emit(
                        on_progress,
                        "images",
                        f"  ℹ️ 超过上限 {img_gen.max_illustrations} 张的部分"
                        f"保留为配图建议",
                    )
        else:
            suggestion_reason = "no_api"
            _emit(
                on_progress,
                "images",
                f"  ⚠️ 图片 API 未配置（{img_gen.key_env} 未设置，"
                f"image.provider={img_gen.provider}），跳过插图生成",
                level="warn",
                key_env=img_gen.key_env,
                provider=img_gen.provider,
            )
            _emit(
                on_progress,
                "images",
                "  💡 文章中的插图位置将保留为配图建议",
            )
    else:
        suggestion_reason = "disabled"
        _emit(
            on_progress,
            "images",
            "\n  ⏭️ 已跳过插图生成 (--no-images)",
        )

    result["images"] = {
        "reason": suggestion_reason,
        "cover": cover_path,
        "paths": illustration_paths,
    }

    # 7.5 封面提示词
    # 读上一步定稿的正文（已过去 AI 味和自检修正），分析内容、选风格、渲染中文提示词。
    # 只产出文本，不调用任何图片生成 API；失败也只是少一个附件，不能影响文章交付。
    cover_info = result["cover_prompt"]
    if _stop_if_cancelled():
        return result
    if cover_prompt:
        _emit(on_progress, "cover_prompt", "🖼️ 生成封面提示词...", banner=True)
        try:
            cp = await generate_cover_prompt(
                article=article,
                title=title,
                article_type=type_name,
                output_dir=str(art_dir),
                config=config,
            )
        except Exception as e:
            # 模块内部已经兜过一层，这里再兜一层，保证文章交付不受影响
            logger.exception("封面提示词生成异常")
            cp = {"ok": False, "path": None, "style": "", "style_name": "",
                  "reason": f"{type(e).__name__}: {e}"}

        cover_info = {
            "ok": bool(cp.get("ok")),
            "path": cp.get("path"),
            "style": cp.get("style", ""),
            "style_name": cp.get("style_name", ""),
            "reason": cp.get("reason", ""),
            "quality": cp.get("quality"),
        }
        result["cover_prompt"] = cover_info

        if cover_info["ok"]:
            _emit(
                on_progress,
                "cover_prompt",
                f"  风格: {cover_info['style_name']}（{cover_info['style']}）",
                level="success",
                style=cover_info["style"],
                style_name=cover_info["style_name"],
            )
            _emit(
                on_progress,
                "cover_prompt",
                f"  ✅ 提示词: {cover_info['path']}",
                level="success",
                path=cover_info["path"],
            )

            q = cover_info.get("quality") or {}
            if q.get("checked"):
                repair_note = {
                    "applied": "，已自动修正",
                    "rejected": "，修正没变好已保留原稿",
                    "failed": "，修正调用失败已保留原稿",
                }.get(q.get("repair", ""), "")
                _emit(
                    on_progress,
                    "cover_prompt",
                    f"  🔎 质量检查: {q.get('passed', 0)}/{q.get('total', 0)} 项通过，"
                    f"{q.get('warn', 0)} 项提示，{q.get('fail', 0)} 项问题{repair_note}",
                    level="warn" if q.get("fail") else "info",
                    **q,
                )
        else:
            _emit(
                on_progress,
                "cover_prompt",
                f"  ⚠️ 未生成: {cover_info['reason']}",
                level="warn",
                reason=cover_info["reason"],
            )
    else:
        _emit(
            on_progress,
            "cover_prompt",
            "\n  ⏭️ 已跳过封面提示词生成",
        )

    # 8. 组装输出
    _emit(on_progress, "assemble", "📦 组装输出...", banner=True)

    content_config = config.get("content", {})
    final_path = assemble_output(
        article=article,
        title=title,
        image_descriptions=image_descriptions,
        cover_path=cover_path,
        illustration_paths=illustration_paths,
        output_dir=str(art_dir),
        suggestion_reason=suggestion_reason,
        category=category or content_config.get("default_category", DEFAULT_CATEGORY),
        tags=tags or content_config.get("default_tags", DEFAULT_TAGS),
    )
    result["output_path"] = final_path

    # 9. 组装后再查一次图片引用（此时配图建议已经落地成文本或图片）
    if selfcheck:
        final_check = check_article(
            Path(final_path).read_text(encoding="utf-8"),
            limits=config.get("selfcheck", {}).get("limits", {}),
            base_dir=str(art_dir),
        )
        report = final_check["report"]
        q = cover_info.get("quality") or {}
        quality_line = (
            f"{q.get('passed', 0)}/{q.get('total', 0)} 项通过"
            f"（{q.get('warn', 0)} 提示 / {q.get('fail', 0)} 问题）"
            if q.get("checked") else "未启用"
        )
        header = (
            f"## 生成信息\n\n"
            f"- 文章类型：{type_name}\n"
            f"- skill 来源：{skill_source}\n"
            f"- 配图建议原因：{suggestion_reason}\n"
            f"- 已生成插图：{sum(1 for p in illustration_paths if p)} 张\n"
            f"- 封面提示词风格：{cover_info['style_name'] or '未生成'}\n"
            f"- 封面提示词文件：{cover_info['path'] or '未生成'}\n"
            f"- 封面提示词质量检查：{quality_line}\n\n"
        )
        report_path = save_report(header + report, str(art_dir))
        if final_check["missing_images"]:
            _emit(
                on_progress,
                "selfcheck",
                "  ⚠️ 有图片引用缺失，见自检报告",
                level="warn",
                missing_images=final_check["missing_images"],
            )
        _emit(
            on_progress,
            "selfcheck",
            f"  🔍 自检报告: {report_path}",
            report_path=report_path,
        )

        # 以组装后的复查为准。组装会补 frontmatter、插入配图建议、追加参考资料，
        # 这些都可能改变自检结果，所以这一步的结论才是写入报告的那个，
        # 也必须覆盖前面那次检查的结果，否则界面显示的和报告里写的不一致。
        if final_check["ok"] != result["selfcheck_ok"]:
            _emit(
                on_progress,
                "selfcheck",
                "  ℹ️ 组装后复查结论有变："
                + ("通过" if final_check["ok"] else "未通过"),
                level="info" if final_check["ok"] else "warn",
            )
        result["selfcheck_ok"] = final_check["ok"]
        result["counts"] = final_check["counts"]
        result["problems"] = final_check["problems"]

    # 10. 完成
    _emit(on_progress, "done", "✨ 完成!", banner=True)
    _emit(on_progress, "done", f"  标题: {title}", level="success", title=title)
    _emit(on_progress, "done", f"  字数: {len(article)} 字符")
    _emit(
        on_progress,
        "done",
        f"  输出: {final_path}",
        level="success",
        output_path=final_path,
    )
    if cover_path:
        _emit(on_progress, "done", f"  封面: {cover_path}")
    if cover_info.get("path"):
        _emit(on_progress, "done", f"  封面提示词: {cover_info['path']}")
    if illustration_paths:
        _emit(
            on_progress,
            "done",
            f"  插图: {sum(1 for p in illustration_paths if p)} 张",
        )
    if image_descriptions and not illustration_paths:
        _emit(
            on_progress,
            "done",
            f"  配图建议: {len(image_descriptions)} 处（待手动配图）",
            suggestion_count=len(image_descriptions),
        )
    _emit(on_progress, "done", "")

    return result
