"""
插图生成模块：封面图 + 文中插图。

支持两个 provider，在 config.yaml 的 image.provider 里切换：

- doubao   火山方舟 Seedream（默认）。POST {base_url}/images/generations，
           Bearer ARK_API_KEY 鉴权，返回图片 URL 或 base64。
- dashscope 通义万相。用 dashscope SDK 的 ImageSynthesis。

dashscope 只在真的用到时才 import，只用豆包的话不需要装 dashscope。
"""

import asyncio
import base64
import logging
from pathlib import Path
from typing import Optional

import requests

from .config_loader import load_config
from .llm_client import LLMClient

logger = logging.getLogger("blog_pipeline")

# 方舟 Seedream 对像素尺寸的限制：总像素和宽高比都要落在区间内
_ARK_MIN_PIXELS = 921_600          # 1280x720
_ARK_MAX_PIXELS = 16_777_216       # 4096x4096
_ARK_MIN_RATIO = 1 / 16
_ARK_MAX_RATIO = 16

# 方舟切换绘图模型版本的提示，出错时打给用户看
_ARK_MODEL_HINT = (
    "如果提示模型不存在或未开通，说明你的方舟账号没开通这个绘图模型。"
    "在 config.yaml 里把 image.doubao.model 改成你已开通的版本，"
    "例如 doubao-seedream-3-0-t2i-250415，然后重跑。"
)


class ImageGenerator:
    """插图生成器。"""

    def __init__(self, config: dict = None):
        config = config or load_config()

        img_config = config.get("image", {})
        # 默认 none：没有明确配置 provider 时不出图、只出配图建议。
        # 改造前这里的默认值是 doubao，等于「没配置就悄悄去调付费出图接口」。
        self.provider = img_config.get("provider", "none")
        self.provider_config = img_config.get(self.provider, {})

        self.cover_size = (
            img_config.get("cover_width", 1664),
            img_config.get("cover_height", 928),
        )
        self.illustration_size = (
            img_config.get("illustration_width", 1024),
            img_config.get("illustration_height", 1024),
        )
        self.max_illustrations = img_config.get("max_illustrations", 3)
        # 注意：这两个名字不能叫 generate_cover / generate_illustrations，
        # 会遮蔽下面同名的 async 方法，调用时报 'bool' object is not callable
        self.want_cover = img_config.get("generate_cover", True)
        self.want_illustrations = img_config.get("generate_illustrations", True)

        self.api_key = self.provider_config.get("api_key", "")
        self.model = self.provider_config.get("model", "")
        self.base_url = self.provider_config.get("base_url", "")
        self.watermark = self.provider_config.get("watermark", False)
        self.response_format = self.provider_config.get("response_format", "url")

    @property
    def disabled(self) -> bool:
        """是否处于「只出配图建议、不自动出图」模式。"""
        return self.provider == "none"

    @property
    def key_env(self) -> str:
        """当前 provider 需要的环境变量名，用于给用户准确的提示。"""
        return "ARK_API_KEY" if self.provider == "doubao" else "DASHSCOPE_API_KEY"

    def is_available(self) -> bool:
        """检查图片生成服务是否可用。"""
        if self.disabled:
            # 这是用户主动选的模式，不是故障，所以走 info 不走 warning
            logger.info(
                "image.provider=none，不自动出图，插图位置输出配图建议"
            )
            return False
        if self.provider == "deepseek":
            # DeepSeek 能读图但不能出图，写错了要明确报错，别让人以为只是缺 key
            logger.error(
                "image.provider 不能设成 deepseek：DeepSeek API 没有文生图能力，"
                "只有图像理解。出图请用 doubao、dashscope，或设成 none 手动配图。"
            )
            return False
        if not self.api_key:
            logger.warning(
                "image.provider=%s 但没读到 api_key（环境变量 %s 未设置）",
                self.provider, self.key_env,
            )
            return False
        if not self.model:
            logger.warning("image.%s.model 未配置", self.provider)
            return False
        return True

    # ---------- 对外接口 ----------

    async def generate_cover(
        self,
        title: str,
        article_summary: str = "",
        output_dir: str = "./output",
    ) -> Optional[str]:
        """生成封面图。"""
        if not self.want_cover or not self.is_available():
            logger.warning("封面生成已禁用或 API 不可用，跳过")
            return None

        prompt = await self._title_to_image_prompt(title, article_summary)

        logger.info("生成封面图: %s", title[:40])
        return await self._generate_image(
            prompt=prompt,
            size=self.cover_size,
            output_path=str(Path(output_dir) / "cover.png"),
        )

    async def generate_illustrations(
        self,
        descriptions: list[str],
        output_dir: str = "./output",
    ) -> list[str]:
        """批量生成文中插图。

        Args:
            descriptions: 插图描述列表（从 [IMAGE: ...] 解析）
            output_dir: 输出目录

        Returns:
            图片路径列表（失败项为 None）
        """
        if not self.want_illustrations or not self.is_available():
            logger.warning("插图生成已禁用或 API 不可用，跳过")
            return []

        descriptions = descriptions[: self.max_illustrations]
        if not descriptions:
            return []

        logger.info("生成 %d 张文中插图...", len(descriptions))

        tasks = []
        for i, desc in enumerate(descriptions):
            output_path = str(Path(output_dir) / f"img_{i + 1:02d}.png")
            tasks.append(self._generate_illustration(desc, output_path, i + 1))

        return await asyncio.gather(*tasks)

    async def _generate_illustration(
        self,
        description: str,
        output_path: str,
        index: int,
    ) -> Optional[str]:
        """生成单张插图。"""
        prompt = await self._desc_to_image_prompt(description)
        logger.info("  插图 %d: %s → prompt 已生成", index, description[:30])
        return await self._generate_image(prompt, self.illustration_size, output_path)

    # ---------- prompt 构造 ----------

    async def _title_to_image_prompt(self, title: str, summary: str = "") -> str:
        """将文章标题 + 摘要转为适合生成封面图的视觉提示词。"""
        prompt = (
            f"现代科技风格插图，主题：{title}。"
            f"简洁的设计，蓝色和深色配色，几何元素，"
            f"适合技术博客封面的抽象插画。不要包含文字。"
        )
        if summary:
            prompt += f" 内容概要：{summary[:200]}"
        return prompt

    async def _desc_to_image_prompt(self, description: str) -> str:
        """将技术插图描述转为视觉化 prompt。"""
        try:
            config = load_config()
            client = LLMClient(config)

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是一个 AI 绘图提示词专家。将技术插图描述转为"
                        "适合文生图模型的英文/中文混合 prompt。\n"
                        "规则：\n"
                        "1. 描述视觉元素，不要抽象概念\n"
                        "2. 指定风格：现代科技插图、简洁设计\n"
                        "3. 指定配色：蓝、白为主调\n"
                        "4. 不要包含文字（text-free）\n"
                        "5. 50 字以内"
                    ),
                },
                {
                    "role": "user",
                    "content": f"请将以下插图描述转为生图 prompt：{description}",
                },
            ]

            prompt = await client.chat(messages, temperature=0.5, max_tokens=200)
            return prompt.strip()
        except Exception:
            # LLM 转写失败，直接用原始描述
            return f"现代科技插图，{description}，简洁设计，蓝色配色，不含文字"

    # ---------- 尺寸处理 ----------

    def _format_size(self, size: tuple) -> str:
        """把 (width, height) 转成 provider 要求的尺寸字符串。"""
        width, height = size
        if self.provider == "doubao":
            # 方舟用 "宽x高"，小写 x。尺寸越界就退回分辨率档位，让模型自己定宽高。
            pixels = width * height
            ratio = width / height if height else 0
            if not (_ARK_MIN_PIXELS <= pixels <= _ARK_MAX_PIXELS) or not (
                _ARK_MIN_RATIO <= ratio <= _ARK_MAX_RATIO
            ):
                logger.warning(
                    "尺寸 %dx%d 超出方舟限制（总像素 %d~%d，宽高比 1/16~16），"
                    "改用 2K 档位由模型决定宽高",
                    width, height, _ARK_MIN_PIXELS, _ARK_MAX_PIXELS,
                )
                return "2K"
            return f"{width}x{height}"
        # 通义万相用 "宽*高"
        return f"{width}*{height}"

    # ---------- 实际调用 ----------

    async def _generate_image(
        self,
        prompt: str,
        size: tuple,
        output_path: str,
    ) -> Optional[str]:
        """按当前 provider 生成单张图片。"""
        if self.provider == "doubao":
            return await self._generate_doubao(prompt, size, output_path)
        if self.provider == "dashscope":
            return await self._generate_dashscope(prompt, size, output_path)
        logger.error("未知的 image.provider: %s", self.provider)
        return None

    async def _generate_doubao(
        self, prompt: str, size: tuple, output_path: str
    ) -> Optional[str]:
        """调用火山方舟的图片生成接口。"""
        url = f"{self.base_url.rstrip('/')}/images/generations"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": self._format_size(size),
            "response_format": self.response_format,
            "watermark": self.watermark,
        }

        async def _call():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=180,
                ),
            )

        try:
            resp = await _call()
        except Exception as e:
            logger.error("方舟图片生成请求异常: %s", e)
            return None

        if resp.status_code != 200:
            logger.error(
                "方舟图片生成失败: HTTP %s, %s\n  %s",
                resp.status_code, resp.text[:300], _ARK_MODEL_HINT,
            )
            return None

        try:
            data = resp.json().get("data") or []
            if not data:
                logger.error("方舟返回里没有 data 字段: %s", resp.text[:300])
                return None
            item = data[0]
            if item.get("b64_json"):
                return self._save_b64(item["b64_json"], output_path)
            if item.get("url"):
                return self._download_image(item["url"], output_path)
            logger.error("方舟返回既没有 url 也没有 b64_json: %s", resp.text[:200])
            return None
        except Exception as e:
            logger.error("解析方舟返回失败: %s → %s", e, resp.text[:200])
            return None

    async def _generate_dashscope(
        self, prompt: str, size: tuple, output_path: str
    ) -> Optional[str]:
        """调用通义万相生成图片。"""
        try:
            # 延迟 import：只用豆包的话不需要装 dashscope
            import dashscope
            from dashscope import ImageSynthesis
        except ImportError as e:
            logger.error("未安装 dashscope，无法使用通义万相: %s", e)
            return None

        dashscope.api_key = self.api_key
        size_str = self._format_size(size)

        async def _call():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: ImageSynthesis.call(
                    model=self.model,
                    prompt=prompt,
                    n=1,
                    size=size_str,
                ),
            )

        try:
            result = await _call()
        except Exception as e:
            logger.error("通义万相请求异常: %s", e)
            return None

        if result.status_code == 200:
            img_url = result.output.results[0].url
            return self._download_image(img_url, output_path)

        logger.error(
            "通义万相生成失败: code=%s, message=%s",
            result.code, result.message,
        )
        return None

    # ---------- 落盘 ----------

    def _save_b64(self, b64_data: str, output_path: str) -> Optional[str]:
        """保存 base64 图片。"""
        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(base64.b64decode(b64_data))
            return output_path
        except Exception as e:
            logger.error("base64 图片保存失败: %s", e)
            return None

    def _download_image(self, url: str, output_path: str) -> Optional[str]:
        """下载图片到本地。"""
        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

            resp = requests.get(url, timeout=60)
            resp.raise_for_status()

            with open(output_path, "wb") as f:
                f.write(resp.content)

            return output_path
        except Exception as e:
            logger.error("图片下载失败: %s → %s", url[:60], e)
            return None
