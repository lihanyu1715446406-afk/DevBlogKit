"""
YAML 配置加载器，支持 ${ENV_VAR} 环境变量替换。
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

logger = logging.getLogger("blog_pipeline")


def _resolve_env_vars(value: Any) -> Any:
    """递归解析字符串中的 ${ENV_VAR} 占位符。"""
    if isinstance(value, str):
        pattern = re.compile(r'\$\{([^}]+)\}')
        matches = pattern.findall(value)
        if len(matches) == 1 and value.strip() == f"${{{matches[0]}}}":
            # 整个值就是一个环境变量引用，返回环境变量的实际类型
            env_val = os.environ.get(matches[0], "")
            return env_val
        # 部分引用，只做字符串替换
        return pattern.sub(
            lambda m: os.environ.get(m.group(1), ""), value
        )
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def load_config(config_path: str = None) -> Dict[str, Any]:
    """加载并解析配置文件。

    Args:
        config_path: 配置文件路径，默认从项目根目录找 config.yaml

    Returns:
        解析后的配置字典

    Raises:
        FileNotFoundError: 配置文件不存在
        yaml.YAMLError: YAML 语法错误
    """
    if config_path is None:
        # 默认从项目根目录找
        config_path = Path(__file__).parent.parent / "config.yaml"

    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            f"请复制 config.yaml.example 为 config.yaml 并填入配置"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"配置文件为空: {config_path}")

    return _resolve_env_vars(raw)


# provider 对应的环境变量名，用于生成准确的提示
PROVIDER_KEY_ENV = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "doubao": "ARK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}

# 文本生成（写作、去 AI 味、封面提示词）支持的 provider。
# default_base_url / default_model 只是兜底值，正常都从 config.yaml 的 llm 节读。
LLM_PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "default_base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
    },
    "openai": {
        "label": "OpenAI",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1",
    },
}

DEFAULT_LLM_PROVIDER = "deepseek"


def resolve_llm_config(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """把配置解析成一套扁平的「写作模型」参数。

    支持两种配置形状：

    新形状（推荐，见 config.yaml 的 llm 节）::

        llm:
          provider: openai
          temperature: 0.8
          max_tokens: 8192
          deepseek: {api_key, base_url, model}
          openai:   {api_key, base_url, model}

    老形状（只有顶层 deepseek: 节、没有 llm:）当作 provider=deepseek 处理，
    这样以前存下来的 config.yaml 副本不会因为这次改动失效。

    Returns:
        {
            "provider": str, "label": str, "api_key": str, "base_url": str,
            "model": str, "temperature": float, "max_tokens": int,
            "timeout": int, "max_retries": int,
            "token_param": str | None,        # 强制长度参数名，一般留空
            "send_temperature": bool | None,  # 强制不发 temperature，一般留空
            "reasoning_effort": str,          # "none" 关掉思考；空串表示不传
        }
    """
    if config is None:
        config = load_config()
    llm = config.get("llm")

    if isinstance(llm, dict) and llm:
        provider = str(llm.get("provider") or DEFAULT_LLM_PROVIDER).strip().lower()
        node = llm.get(provider)
        if not isinstance(node, dict):
            logger.warning(
                "llm.provider=%s 但配置里没有 llm.%s 节，按空配置处理",
                provider, provider,
            )
            node = {}
        shared = llm
    else:
        # 老配置：整块 deepseek: 就是 provider 节
        provider = DEFAULT_LLM_PROVIDER
        node = config.get("deepseek", {}) or {}
        shared = node

    spec = LLM_PROVIDERS.get(provider, {})
    return {
        "provider": provider,
        "label": spec.get("label", provider),
        "api_key": node.get("api_key", ""),
        "base_url": node.get("base_url") or spec.get("default_base_url", ""),
        "model": node.get("model") or spec.get("default_model", ""),
        # 下面几个是共用参数：provider 节里写了用它的，没写就用 llm 层的
        "temperature": node.get("temperature", shared.get("temperature", 0.8)),
        "max_tokens": node.get("max_tokens", shared.get("max_tokens", 8192)),
        "timeout": node.get("timeout_seconds", shared.get("timeout_seconds", 120)),
        "max_retries": node.get("max_retries", shared.get("max_retries", 3)),
        "token_param": node.get("max_tokens_param") or shared.get("max_tokens_param"),
        "send_temperature": node.get(
            "supports_temperature", shared.get("supports_temperature")
        ),
        # 思考模型的开关。填 "none" 关掉思考；留空则用模型默认。
        # 思考 token 也算在 max_tokens 里，不关掉可能把正文挤没（见 llm_client 的空内容保护）。
        "reasoning_effort": node.get(
            "reasoning_effort", shared.get("reasoning_effort", "")
        ),
    }


def _selected_provider(config: Dict[str, Any], section: str) -> tuple[str, str]:
    """取出某个配置节选中的 provider 名称和它的 api_key。"""
    node = config.get(section, {})
    provider = node.get("provider", "")
    return provider, node.get(provider, {}).get("api_key", "")


def set_llm_provider(provider: str, config_path: str = None) -> str:
    """把 config.yaml 里 llm.provider 改成指定值，只动那一行。

    刻意不用 yaml.dump 重写整个文件：config.yaml 里那些注释是给人看的文档，
    dump 一次会把它们全部丢掉。这里做定点替换，块内其他内容和全部注释原样保留。

    Args:
        provider: deepseek | openai
        config_path: 配置文件路径，默认项目根目录的 config.yaml

    Returns:
        配置文件路径

    Raises:
        ValueError: provider 不在支持列表里
        FileNotFoundError: 配置文件不存在
        RuntimeError: 找不到顶层 llm: 块（老配置形状），需要手动加
    """
    provider = (provider or "").strip().lower()
    if provider not in LLM_PROVIDERS:
        raise ValueError(
            f"不支持的写作模型 provider: {provider!r}，可选 "
            + "、".join(LLM_PROVIDERS)
        )

    path = (
        Path(config_path)
        if config_path
        else Path(__file__).parent.parent / "config.yaml"
    )
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    # 定位顶层 llm: 块
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^llm\s*:\s*(#.*)?$", line):
            start = i
            break
    if start is None:
        raise RuntimeError(
            "config.yaml 里找不到顶层 llm: 块（可能是旧版配置），"
            "请手动加上 llm 节后再用界面切换"
        )

    # 块的结束：下一个顶格且不是注释的行
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].rstrip("\n")
        if stripped.strip() and not stripped[0].isspace():
            end = j
            break

    # 块内找 provider: 行。取缩进最小的那个 —— 那才是 llm 的直接子键，
    # 避免误改 provider 子块（如 llm.openai）里同名的键。
    candidates = []
    for k in range(start + 1, end):
        m = re.match(r"^(\s+)provider\s*:\s*(.*?)\s*$", lines[k].rstrip("\n"))
        if m:
            candidates.append((k, len(m.group(1)), m.group(1)))

    if candidates:
        k, _, indent = min(candidates, key=lambda c: c[1])
        lines[k] = f'{indent}provider: "{provider}"\n'
    else:
        # 没有 provider 行就插在 llm: 后面
        lines.insert(start + 1, f'  provider: "{provider}"\n')

    new_text = "".join(lines)
    if new_text != original:
        path.write_text(new_text, encoding="utf-8")
        logger.info("写作模型已切换为 %s（%s）", provider, path)
    return str(path)


def validate_config(config: Dict[str, Any]) -> list[str]:
    """检查配置完整性，返回警告列表。"""
    warnings = []

    # 写作模型：只检查当前选中的 provider，别对着没在用的那家报缺 key
    llm = resolve_llm_config(config)
    if not llm["api_key"]:
        env = PROVIDER_KEY_ENV.get(llm["provider"], f"{llm['provider']} 的 api_key")
        warnings.append(
            f"{env} 未设置，写作功能不可用（llm.provider={llm['provider']}）"
        )

    for section, label in (
        ("image", "插图生成不可用，配图建议会保留在文章里"),
        ("vision", "截图材料无法转录，纯文本笔记不受影响"),
    ):
        provider, key = _selected_provider(config, section)
        # provider=none 是主动选择的手动配图模式，不算配置缺失
        if provider == "none":
            continue
        if not key:
            env = PROVIDER_KEY_ENV.get(provider, f"{provider} 的 api_key")
            warnings.append(f"{env} 未设置，{label}（{section}.provider={provider}）")

    return warnings
