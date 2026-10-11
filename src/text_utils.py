"""
文本处理共用小工具。

这几个正则在改造前各写了一遍：代码围栏剥离有 2 份完全相同的实现（外加 1 处内联），
`[IMAGE: ...]` 标记有 3 份、代码围栏正则有 3 份。改一处忘一处就会出现
「某个阶段识别不出标记」这类很难查的问题，所以集中到这里，各模块统一引用。

只放跨模块复用的东西，模块自己的专用正则不要往这里塞。
"""

import re

# 整行代码围栏（``` 或 ```python 开头），用于剥掉模型给整段内容套上的外壳。
FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*\n")

# 行首围栏前缀。按行扫描、在「围栏内/外」之间切换时用（selfcheck 统计内联反引号）。
# 刻意不带 \n：它要匹配的是行首那三个反引号本身。
FENCE_LINE_RE = re.compile(r"^\s*```")

# [IMAGE: 描述] 标记。写作阶段产出，组装阶段替换成图片引用或配图建议。
IMAGE_MARKER_RE = re.compile(r"\[IMAGE:\s*([^\]]+)\]")


def strip_fence(text: str) -> str:
    """去掉模型可能给整段内容套上的 ``` 代码围栏。"""
    text = (text or "").strip()
    if FENCE_RE.match(text):
        text = FENCE_RE.sub("", text, count=1)
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def extract_image_descriptions(article: str) -> list[str]:
    """提取文章里的 [IMAGE: 描述] 标记，按出现顺序返回。"""
    return IMAGE_MARKER_RE.findall(article or "")
