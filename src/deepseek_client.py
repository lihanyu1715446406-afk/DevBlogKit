"""
兼容层：文本生成已经统一走 src/llm_client.py（DeepSeek / OpenAI 共用）。

这个文件留着只是为了不让还在 `from src.deepseek_client import DeepSeekClient`
的旧脚本报 ImportError —— 比如你自己写的临时脚本，或者历史版本的代码。
新代码请直接用 `from .llm_client import LLMClient`。
"""

from .llm_client import LLMClient

# 旧名字，指向同一个实现
DeepSeekClient = LLMClient

__all__ = ["DeepSeekClient", "LLMClient"]
