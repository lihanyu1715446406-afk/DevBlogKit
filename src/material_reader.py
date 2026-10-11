"""
材料读取：把笔记文件、截图目录读成一段可供写作的素材文本。

- 文本类（.md / .txt）直接读，保留原文结构和图片引用标记。
- 图片类（截图、课程幻灯片）走视觉模型转录。

视觉模型单独配，是为了让读图和写作可以各用各的模型。默认就是 DeepSeek，
写作和读图共用一个 key。DeepSeek 的接口能直接接受图片输入（实测 deepseek-chat
读含随机字符的图片三轮全对），所以不需要为了读图再注册别的平台。
也可以切成豆包（火山方舟）或通义万相，见 config.yaml 的 vision.provider。

注意：能读图不等于能生成图。DeepSeek API 没有文生图能力，
插图生成仍然要靠豆包或通义万相，见 image.provider。
"""

import asyncio
import base64
import logging
from pathlib import Path
from typing import Optional

from openai import OpenAI

from .config_loader import load_config
from .utils import retry_async

logger = logging.getLogger("blog_pipeline")

TEXT_EXTS = {".md", ".markdown", ".txt"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

# 转录提示词：目标是"忠实还原"，不是"总结"。
# 幻灯片里的字段名、数值、对比表格正是写作时要用的证据，总结会把这些丢掉。
_VISION_PROMPT = """请把这张图片的内容逐条转录出来，供后续写技术文章使用。

要求：
1. 图中所有文字都要转录，包括标题、正文条目、表格、代码、数值和单位
2. 表格用 Markdown 表格还原，字段名和数值一个都不要改
3. 流程图或结构图要说明有哪些节点、箭头方向和连线关系
4. 保持原有的层次和编号顺序
5. 只转录图里有的内容，不要补充、推断或总结
6. 不要输出"这张图展示了"这类开场白，直接给内容"""


class VisionClient:
    """视觉模型客户端。

    deepseek / doubao（火山方舟）/ dashscope（通义万相）都提供 OpenAI 兼容接口，
    只有 base_url、模型名和 key 不同，所以共用同一套调用代码，
    按 config.yaml 的 vision.provider 切换。

    默认用 deepseek，因为写作已经在用 DeepSeek，同一个 key 就够了，
    不用再注册其他平台。
    """

    # 没配 key 时提示该设哪个环境变量
    _KEY_ENV = {
        "deepseek": "DEEPSEEK_API_KEY",
        "doubao": "ARK_API_KEY",
        "dashscope": "DASHSCOPE_API_KEY",
    }

    def __init__(self, config: dict = None):
        config = config or load_config()
        vision = config.get("vision", {})
        self.provider = vision.get("provider", "deepseek")
        provider_config = vision.get(self.provider, {})

        self.api_key = provider_config.get("api_key", "")
        self.model = provider_config.get("model", "")
        self.base_url = provider_config.get("base_url", "")
        # detail 只有部分 provider 认（DeepSeek 支持 low/high/original/auto），
        # 没配就不传，免得其他家拒绝这个字段
        self.detail = provider_config.get("detail", "")
        self.timeout = vision.get("timeout_seconds", 180)
        self.max_retries = vision.get("max_retries", 2)
        # 课程幻灯片文字密，转录输出容易长，给足额度
        self.max_tokens = vision.get("max_tokens", 4096)
        self._client = None

    @property
    def key_env(self) -> str:
        """当前 provider 需要的环境变量名，用于给用户准确的提示。"""
        return self._KEY_ENV.get(self.provider, f"{self.provider} 的 api_key")

    def is_available(self) -> bool:
        if not self.api_key:
            logger.warning(
                "vision.provider=%s 但没读到 api_key（环境变量 %s 未设置）",
                self.provider, self.key_env,
            )
            return False
        if not self.model:
            logger.warning("vision.%s.model 未配置", self.provider)
            return False
        return True

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client


    async def describe_image(self, image_path: Path) -> str:
        """转录单张图片。

        Returns:
            转录文本；失败时返回带错误说明的占位文本，不抛异常。
        """
        if not self.is_available():
            return f"（未配置视觉模型 API，无法转录图片 {image_path.name}）"

        mime = _MIME.get(image_path.suffix.lower(), "image/png")
        try:
            data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        except OSError as e:
            logger.error("读取图片失败: %s → %s", image_path, e)
            return f"（图片读取失败：{image_path.name}）"

        image_url: dict = {"url": f"data:{mime};base64,{data}"}
        if self.detail:
            image_url["detail"] = self.detail

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": image_url},
                ],
            }
        ]

        async def _call():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                ),
            )

        try:
            logger.info("转录图片: %s", image_path.name)
            resp = await retry_async(_call, max_retries=self.max_retries)
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.error("图片转录失败: %s → %s", image_path.name, e)
            return f"（图片转录失败：{image_path.name}，原因 {e}）"


def _expand_inputs(inputs: list[str]) -> tuple[list[Path], list[Path]]:
    """把用户给的路径展开成 (文本文件列表, 图片文件列表)，目录按文件名排序展开。"""
    texts, images = [], []
    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidates = sorted(
                p for p in path.iterdir() if p.is_file() and not p.name.startswith(".")
            )
        elif path.is_file():
            candidates = [path]
        else:
            logger.warning("材料路径不存在，已跳过: %s", raw)
            continue

        for p in candidates:
            suffix = p.suffix.lower()
            if suffix in TEXT_EXTS:
                texts.append(p)
            elif suffix in IMAGE_EXTS:
                images.append(p)
            else:
                logger.warning("不支持的材料类型，已跳过: %s", p.name)

    return texts, images


async def read_materials(
    inputs: list[str],
    *,
    config: dict = None,
    max_images: Optional[int] = None,
) -> dict:
    """读取材料，返回素材文本和读取过程中的问题。

    Args:
        inputs: 文件或目录路径列表
        config: 配置字典
        max_images: 最多转录几张图，默认取 config.vision.max_images

    Returns:
        {
            "text": str,          # 拼接好的素材文本（含来源标注）
            "text_files": [...],  # 读入的文本文件路径
            "image_files": [...], # 读入的图片文件路径
            "errors": [...],      # 读取失败的文件说明
        }
    """
    config = config or load_config()
    if max_images is None:
        max_images = config.get("vision", {}).get("max_images", 20)

    texts, images = _expand_inputs(inputs)
    errors: list[str] = []

    if len(images) > max_images:
        logger.warning(
            "图片数量 %d 超过上限 %d，只转录前 %d 张",
            len(images), max_images, max_images,
        )
        errors.append(
            f"图片超过上限 {max_images} 张，仅处理前 {max_images} 张"
        )
        images = images[:max_images]

    parts: list[str] = []

    for p in texts:
        try:
            content = p.read_text(encoding="utf-8").strip()
        except OSError as e:
            errors.append(f"{p.name} 读取失败：{e}")
            continue
        parts.append(f"### 材料：{p.name}\n\n{content}")

    if images:
        client = VisionClient(config)
        if not client.is_available():
            errors.append(
                f"未配置 {client.key_env}（vision.provider={client.provider}），"
                f"截图无法转录"
            )
        # 并发转录，顺序与文件顺序一致
        results = await asyncio.gather(
            *(client.describe_image(p) for p in images)
        )
        for p, content in zip(images, results):
            if content.startswith("（") and content.endswith("）"):
                errors.append(f"{p.name} 未成功转录")
            parts.append(f"### 截图：{p.name}\n\n{content}")

    logger.info(
        "材料读取完成 - 文本 %d 个，图片 %d 个，问题 %d 条",
        len(texts), len(images), len(errors),
    )
    return {
        "text": "\n\n".join(parts),
        "text_files": [str(p) for p in texts],
        "image_files": [str(p) for p in images],
        "errors": errors,
    }
