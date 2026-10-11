"""
文本生成模型客户端：一个入口，背后可以是 DeepSeek，也可以是 OpenAI。

流水线里所有「生成文字」的阶段都走这里：写作、去 AI 味、封面提示词、插图 prompt 转写。
读图（截图转录）不在这里，它按 vision.provider 单独配置，见 material_reader.py。

为什么两家共用一个客户端：

1. 两家的接口都是 OpenAI 兼容的 chat completions，差别只在 base_url 和模型名；
2. 但有一个绕不开的参数差异——新模型（GPT-5 系列、o 系列）拒收 max_tokens，必须用
   max_completion_tokens，而且默认不接受 temperature。流水线每个阶段都在传 temperature，
   模型选错会直接报 400。

所以这里做两件事：

- 用 config_loader.resolve_llm_config 拿到「一套扁平参数」——provider 解析和配置形状兼容
  放在 config_loader 里（它本来就管配置和 provider 校验），这个模块只管怎么把请求发出去；
- 参数形状按模型自适应：先按当前形状发一次，收到「这个参数不支持」就换另一种形状重发，
  并把结论记住，同一个实例后续请求不再重复踩坑。

这样换模型不用改代码，也不需要提前知道每个模型的参数脾气。
"""

import logging
from typing import Any, Optional

from openai import OpenAI

from .config_loader import PROVIDER_KEY_ENV, resolve_llm_config
from .utils import retry_async

logger = logging.getLogger("blog_pipeline")

# 长度上限参数名。老的模型用 max_tokens，新的（GPT-5 / o 系列）只认 max_completion_tokens。
# 默认先用老名字，收到报错再换，避免猜错。
_TOKEN_PARAM = "max_tokens"
_TOKEN_PARAM_ALT = "max_completion_tokens"


class LLMEmptyResponseError(RuntimeError):
    """模型返回了空内容。

    单独定义成一种异常，是为了让失败原因一眼能认出来：空内容通常不是网络问题，
    重试也没用（还会白烧一次 token），所以这个异常不做重试，直接带着修复建议抛出去。
    """


def _extra_field(obj, name: str):
    """取 SDK 模型上没有声明、但服务端确实返回了的字段（如 reasoning_content）。

    openai 的 pydantic 模型把这类字段放在 model_extra 里，直接 getattr 可能取不到。
    """
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None) or {}
        value = extra.get(name)
    return value


class LLMClient:
    """文本生成模型客户端（DeepSeek / OpenAI 共用）。"""

    def __init__(self, config: dict = None):
        cfg = resolve_llm_config(config)

        self.provider = cfg["provider"]
        self.label = cfg["label"]
        self.model = cfg["model"]
        self.base_url = cfg["base_url"]
        self.default_temperature = cfg["temperature"]
        self.default_max_tokens = cfg["max_tokens"]
        self.timeout = cfg["timeout"]
        self.max_retries = cfg["max_retries"]

        # 参数形状。可以从配置钉死，也可以留空让第一次报错来纠正（见 _adapt_from_error）
        self._token_param = cfg["token_param"] or _TOKEN_PARAM
        send_temp = cfg["send_temperature"]
        self._send_temperature = True if send_temp is None else bool(send_temp)
        # 思考开关：空串 = 不传这个参数（普通模型不需要）
        self.reasoning_effort = cfg.get("reasoning_effort") or ""

        self.client = OpenAI(
            api_key=cfg["api_key"] or "missing-api-key",
            base_url=self.base_url or None,
        )

        if not cfg["api_key"]:
            logger.warning(
                "%s 的 api_key 为空（环境变量 %s 未设置），调用时会失败",
                self.label, PROVIDER_KEY_ENV.get(self.provider, "?"),
            )

    # ---------- 参数自适应 ----------

    def _adapt_from_error(self, err: Exception) -> bool:
        """从报错里认出「参数形状不对」，改掉并返回 True。

        只认得出确定的情况，其余原样抛出去。改完由调用方重发一次。
        """
        msg = str(err).lower()
        changed = False

        # 长度参数名：新的模型会说 use 'max_completion_tokens' instead
        if self._token_param == _TOKEN_PARAM and _TOKEN_PARAM_ALT in msg:
            self._token_param = _TOKEN_PARAM_ALT
            changed = True
        elif (
            self._token_param == _TOKEN_PARAM_ALT
            and _TOKEN_PARAM in msg
            and _TOKEN_PARAM_ALT not in msg
        ):
            # 注意：'max_tokens' 是 'max_completion_tokens' 的子串，
            # 所以要排掉包含后者的报错，否则会来回横跳
            self._token_param = _TOKEN_PARAM
            changed = True

        # 温度：这类模型只接受默认值，报错形如
        # "Unsupported value: 'temperature' does not support 0.8 with this model"
        if self._send_temperature and "temperature" in msg and any(
            m in msg
            for m in ("not support", "unsupported", "does not support", "only the default")
        ):
            self._send_temperature = False
            changed = True

        # 配置里填了 reasoning_effort 但模型不认（普通模型常见）→ 去掉这个参数
        if self.reasoning_effort and "reasoning_effort" in msg and any(
            m in msg
            for m in ("not support", "unsupported", "does not support",
                      "unknown", "unrecognized", "unexpected", "invalid")
        ):
            logger.warning(
                "%s 的模型 %s 不认 reasoning_effort，已去掉这个参数",
                self.label, self.model,
            )
            self.reasoning_effort = ""
            changed = True

        if changed:
            logger.warning(
                "%s 的模型 %s 不接受当前参数形状，已自动调整："
                "长度参数=%s，发送 temperature=%s",
                self.label, self.model, self._token_param, self._send_temperature,
            )
        return changed

    def _create(
        self,
        messages: list[dict],
        temperature: Optional[float],
        max_tokens: Optional[int],
        model: Optional[str],
    ):
        """按当前参数形状发一次请求，返回原始响应对象。"""
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "timeout": self.timeout,
        }
        if max_tokens is not None:
            kwargs[self._token_param] = max_tokens
        if self._send_temperature and temperature is not None:
            kwargs["temperature"] = temperature
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort

        return self.client.chat.completions.create(**kwargs)

    def _extract_text(self, response, model: str, max_tokens: Optional[int]) -> str:
        """从响应里取出正文。

        空内容必须报错，不能返回空串：思考模型（deepseek-flash / deepseek-v4-pro 这类）
        的思考 token 也算 max_tokens，预算被思考吃光时正文就是空的。
        返回空串的话，写作阶段会安安静静写出一篇空文章，比直接失败糟得多。
        """
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        finish = getattr(choice, "finish_reason", None)

        if text:
            if finish == "length":
                logger.warning(
                    "%s 的输出被 max_tokens 截断（finish_reason=length），正文可能不完整",
                    model,
                )
            return text

        raise LLMEmptyResponseError(
            self._empty_hint(model, choice.message, finish, max_tokens)
        )

    def _empty_hint(self, model: str, message, finish, max_tokens) -> str:
        """空内容的修复建议。要说清是哪一种空，别让人猜。"""
        reasoning = _extra_field(message, "reasoning_content") or ""
        budget = max_tokens if max_tokens is not None else self.default_max_tokens

        if reasoning and finish == "length":
            return (
                f"{self.label} 的 {model} 返回了空内容：思考过程把 max_tokens={budget} "
                f"用完了，正文没来得及写。这是思考模型（deepseek-flash / deepseek-v4-pro "
                f"这类）的典型表现。修复：在 llm.{self.provider} 里加一行 "
                f'reasoning_effort: "none" 关掉思考，或者把 max_tokens 调大'
                f"（思考 token 也占这个预算）。"
            )
        if finish == "length":
            return (
                f"{self.label} 的 {model} 返回了空内容：输出被 max_tokens={budget} "
                f"截断。把 max_tokens 调大。"
            )
        return (
            f"{self.label} 的 {model} 返回了空内容（finish_reason={finish}）。"
            f"检查 model 名字是否正确、提示词是否触发了拒答。"
        )

    # ---------- 对外接口 ----------

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> str:
        """发送聊天请求，返回文本响应。

        Args:
            messages: OpenAI 格式的消息列表
            temperature: 温度参数（默认使用配置值）。模型不接受时会自动不发这个参数
            max_tokens: 输出长度上限（默认使用配置值）
            model: 模型名称（默认使用配置值）

        Returns:
            LLM 的文本响应
        """
        # 这里是同步调用，会阻塞所在的事件循环整个网络往返。
        # 保持原样：网页界面每个任务有自己的线程和 loop，阻塞不影响别的任务。
        effective_temperature = (
            self.default_temperature if temperature is None else temperature
        )
        effective_max_tokens = (
            self.default_max_tokens if max_tokens is None else max_tokens
        )

        async def _call():
            # 参数形状最多纠正两次（长度参数 + 温度 / reasoning_effort），
            # 再多说明不是参数问题。返回原始响应对象，空内容检查放到重试之外。
            last_error: Optional[Exception] = None
            for _ in range(3):
                try:
                    return await self._request_once(
                        messages, effective_temperature, effective_max_tokens, model
                    )
                except Exception as e:
                    last_error = e
                    if not self._adapt_from_error(e):
                        raise
                    logger.info("按新的参数形状重发一次")
            raise last_error

        logger.info(
            "%s 请求: model=%s, messages=%d条, 首条前60字=%s...",
            self.label,
            model or self.model,
            len(messages),
            str(messages[-1].get("content", ""))[:60] if messages else "",
        )
        response = await retry_async(_call, max_retries=self.max_retries)

        # 空内容在重试之外检查：重试也变不出正文，反而会再烧一次 token
        result = self._extract_text(
            response, model or self.model, effective_max_tokens
        )
        logger.info("%s 响应: %d 字符", self.label, len(result))
        return result

    async def _request_once(
        self,
        messages: list[dict],
        temperature: Optional[float],
        max_tokens: Optional[int],
        model: Optional[str],
    ):
        """把同步的 SDK 调用包成 await 得动的形式（本函数自身不阻塞调度）。"""
        return self._create(messages, temperature, max_tokens, model)

    async def chat_json(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        **kwargs,
    ) -> str:
        """发送聊天请求并强制返回 JSON 格式。

        在系统提示词中追加 JSON 输出指令。用提示词约束而不是 response_format，
        是为了让 DeepSeek 和 OpenAI 表现一致（也方便换成任何兼容接口的中转服务）。
        """
        # 不改调用方传来的 messages 对象本身
        messages = [dict(m) for m in messages]
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] += (
                "\n\n请严格输出 JSON 格式，不要包含任何其他文字。"
            )
        else:
            messages.insert(0, {
                "role": "system",
                "content": "请严格输出 JSON 格式，不要包含任何其他文字。"
            })

        return await self.chat(
            messages,
            temperature=temperature,
            **kwargs,
        )
