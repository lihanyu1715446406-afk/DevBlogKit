# Blog Pipeline — 博客自动生产流水线

输入题材（可选带笔记/截图）→ AI 按 skill 规则写作 → 去 AI 味 → 自检修正 → 生成插图 → 输出 Markdown → 你审核发布。

写作风格完全由 skill 规则决定，不参照历史文章样本。

两种用法：**网页界面**（推荐）和命令行。两者共用同一套流水线核心 `src/pipeline.py`。

## 网页界面

双击 **`启动博客生成器.bat`**，浏览器会自动打开 `http://127.0.0.1:8756/`。

不想看到控制台窗口就用 `启动博客生成器（无控制台）.bat`（启动报错会看不到，排查时用前者）。

界面有四个页签：

- **生成**：填题材、选材料（笔记文件或截图目录）、分类标签、文章类型和选项，点「加入队列」。
  可以连续提交多篇，后台依次跑。日志实时滚动，生成完可以直接查看文章或打开输出文件夹。
- **历史文章**：列出 `output/` 下所有已生成的文章，带自检结果和类型徽章。
  点开能看渲染后的正文、自检报告、去 AI 味改动清单、大纲，也能复制原始 Markdown 或打开文件夹。
- **使用教程**：三步上手、四种文章类型、各选项含义、队列与取消行为、每篇产出哪些文件、配图怎么处理。
- **写作背景**：列出实际加载的两个 skill（来源、路径、文件清单、体量）、文章类型判定规则、
  流水线八个阶段、写作取向、模型配置；还有一块**写作者背景**可以填，见下一节。

几点行为说明：

- **勾了「先确认大纲」的任务会暂停等你在页面上点「继续写正文」或「取消」**。
  默认 600 秒不确认就自动取消（可在 `config.yaml` 的 `web.review_timeout_seconds` 改）。
- **队列是顺序执行的**。所以一篇任务卡在大纲确认时，排在它后面的任务也会一起等。
  想批量跑就别勾「先确认大纲」。
- **「取消」在阶段边界生效**。正在飞的模型调用不能中断，所以点了取消要等当前这一阶段跑完。
  排队中的任务则是立刻取消。
- 日志文件仍然照常写在 `data/pipeline.log`，网页上的日志是额外的一份。

### 写作者背景

「写作背景」页上可以填写作者是谁（身份、技术栈、经验、其他补充），保存在
`data/writer_profile.json`，不用重启服务，下一篇就生效。

- 填了之后会以**轻度参考**的方式注入写作提示词：题材相关时，举例和侧重会略偏向这个背景
- **字段全空就完全不注入**，文章按默认方式写
- 注入段落明确禁止自报身份和编造个人经历（不写「作为一个后端开发者」「我在项目里遇到过」），
  并且说明其他写作规则不受影响。自检里也加了对应的守卫，出现这类句子会报出来
- 存在独立 JSON 文件而不是写回 `config.yaml`，因为用程序回写 yaml 会把文件里的注释全冲掉

## 快速开始（命令行）

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 设置 API 密钥

**一个 DeepSeek key 就够了**，写作和读课程截图都用它：

```powershell
# 写作模型 + 截图转录（当前配置用的是这个）
[Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', 'sk-你的key', 'User')

# 只有当你把写作模型切到 OpenAI 时才需要
[Environment]::SetEnvironmentVariable('OPENAI_API_KEY', 'sk-你的key', 'User')
```

设完**要关掉终端重开**，环境变量是进程启动时读进去的。验证：

```powershell
python -c "import sys; sys.path.insert(0,'.'); from src.config_loader import load_config, validate_config; w = validate_config(load_config()); print('配置检查通过，所有 key 都就绪' if not w else '还缺:'); [print('  -', x) for x in w]"
```

这个检查只看**当前选中的**写作 provider，不会对着没在用的那家报缺 key。

关于 OpenAI：**不需要 ChatGPT 会员**，但 API 要单独在 platform.openai.com 充值预付费额度，
按 token 计费（一篇文章通常几分钱到几毛钱）。另外 OpenAI 会拒绝中国大陆 IP 的 API 请求，
所以跑脚本时要开着代理。**写作模型随时可以在网页界面或配置里切回 DeepSeek**，
详见下方「切换写作模型」。

DeepSeek 的接口能直接接受图片输入，所以读图和写作可以共用一个 key
（实测 `deepseek-chat` 读含随机字符的图片三轮全对），不需要为读图注册别的平台。

**插图默认手动配图**，不需要额外 key。文章里每个插图位置会输出一条
`> 📷 **配图建议：**` 引用块，你照着补图。想改成自动出图见下方「读图与出图是两件事」。

### 3. 开始生成

```bash
# 只给题材
python run.py "Java 21 虚拟线程深入解析"

# 带笔记或截图（自动判型为课程笔记文）
python run.py "RabbitMQ 入门" -m ./笔记.md -m ./截图目录
```

## 写作规则来自哪里

写作规则直接读真实 skill 文件：

| 阶段 | skill | 位置 | 用到的文件 |
| --- | --- | --- | --- |
| 正文写作 | `learning-tech-blog` | **项目级**：`.claude/skills/learning-tech-blog/` | `SKILL.md`（带笔记/截图材料时追加 `references/course-note-blogging.md`） |
| 去 AI 味 | `Humanizer-zh` | 用户级：`~/.claude/skills/` | `SKILL.md` |

正文规范放在**项目里**，是为了让 Claude Code 和这个脚本读同一份文件：
Claude Code 把它当 Skill 自动调用，脚本在写文章前把同一个 `SKILL.md` 注进模型提示词。
一份规范两个入口，不需要两边各维护一份。

skill 目录不存在时自动回退到 `prompts/blog-style.txt` 和 `prompts/humanize.txt`。
启动时会打印实际来源，自检报告里也会记录。

```yaml
skills:
  primary: "skill"     # skill = 读真实 skill；prompts = 只用蒸馏副本
```

## 常用命令

```bash
# 指定分类和标签
python run.py "xxx" --category "Java" --tags "Java, 并发"

# 带材料：笔记文件或截图目录，可重复传
python run.py "xxx" -m ./day03.md -m ./截图

# 指定文章类型（默认 auto）
python run.py "xxx" --type course-note

# 先出大纲，确认后再写正文
python run.py "xxx" --review

# 快速模式（跳过大纲，直接生成）
python run.py "xxx" --fast

# 纯文本（不生成插图，全部保留为配图建议）
python run.py "xxx" --no-images

# 跳过去 AI 味改写
python run.py "xxx" --no-humanize

# 跳过交付前自检
python run.py "xxx" --no-selfcheck

# 跳过封面提示词生成
python run.py "xxx" --no-cover-prompt

# 指定输出目录
python run.py "xxx" --output ./output/自定义文件夹
```

## 工作原理

```
你输入题材（可选带笔记/截图）
    │
    ▼
┌────────────┐
│ 材料读取    │  笔记直接读；截图走视觉模型转录成文字
└─────┬──────┘
      ▼
┌────────────┐
│ 文章判型    │  有材料 → 课程笔记文；无材料 → 知识点文；可 --type 指定
└─────┬──────┘
      ▼
┌────────────┐
│ 大纲生成    │  写作模型：标题、结构、插图位置（--review 时停下等你确认）
└─────┬──────┘
      ▼
┌────────────┐
│ 正文写作    │  写作模型：注入真实 skill 规则 + 写作取向 + 作者背景
└─────┬──────┘
      ▼
┌────────────┐
│ 去 AI 味    │  按 Humanizer-zh skill 改写一遍
└─────┬──────┘    （frontmatter / 代码块 / [IMAGE:] 标记原样保留）
      ▼
┌────────────┐
│ 交付前自检  │  数格式标记、量前言长度、扫禁用句式；不合格回喂重写一次
└─────┬──────┘
      ▼
┌────────────┐
│ 插图处理    │  手动配图模式：每个插图位置落成「配图建议」
└─────┬──────┘    切到豆包/通义才真的出图
      ▼
┌────────────┐
│ 封面提示词  │  读定稿正文 → 选风格 → 输出中文提示词 → 10 项质量检查（只出文本，不出图）
└─────┬──────┘
      ▼
┌────────────┐
│ 组装输出    │  frontmatter 补齐 + 图片/配图建议落地 → index.md
└─────┬──────┘
      ▼
  你审核发布 + 手动出封面图
```

## 读图与出图是两件事

**读图（截图转录）** 和 **出图（插图生成）** 分别配置，可以各用各的模型。

```yaml
vision:                                # 读图：截图 → 文字
  provider: "deepseek"                 # deepseek | doubao | dashscope
  deepseek:
    api_key: "${DEEPSEEK_API_KEY}"
    base_url: "https://api.deepseek.com"
    model: "deepseek-chat"
    detail: "high"                     # low | high | original | auto，读小字用 high

image:                                 # 出图：文生图
  provider: "none"                     # none | doubao | dashscope
  doubao:
    api_key: "${ARK_API_KEY}"
    model: "doubao-seedream-4-0-250828"
```

三家的读图接口都是 OpenAI 兼容格式，所以共用一套调用代码。

**读图默认走 DeepSeek**，是 `vision` 节单独配的，和写作模型用哪家没有关系
（写作模型切到 OpenAI 时，读图仍然走 DeepSeek）。
把 `vision.provider` 改成 `doubao` 或 `dashscope` 也能用，只是要多配一个 key。

**出图默认是 `none`（手动配图）**。文章里每处 `[IMAGE: ...]` 都会变成引用块：

```markdown
> 📷 **配图建议：** 同步调用链路示意，从支付服务依次串起用户服务、交易服务……
> *（请手动配图）*
```

想改成自动出图，把 `provider` 设成 `doubao` 或 `dashscope`，并配好对应的 key。
`image.provider` 填 `deepseek` 会直接报错，因为 DeepSeek 没有文生图能力。

封面图在 `none` 模式下不生成，但大纲里的「封面建议」会存进 `outline.md`。

**绘画尺寸**。豆包要求总像素落在 921,600 ~ 16,777,216、宽高比落在 1/16 ~ 16 之间。
默认封面 1664×928 和插图 1024×1024 都在区间内。改配置时如果越界，代码会记一条警告并自动退回
`2K` 档位，由模型自己决定宽高，不会直接报错。

**换绘图模型版本**。只开通了 Seedream 3.0 的话把 `image.doubao.model` 改成
`doubao-seedream-3-0-t2i-250415`。调用失败时日志里也会提示这一点。

**材料里的数值按原样引用**。材料区块会明确要求模型不要自己换算、四舍五入或重新计算。
（没有这条约束时观察到过模型把「五个下游各 50ms」写成「四个下游接近 500ms」。）

## 输出文件

`output/日期-标题/` 下：

| 文件 | 说明 |
| --- | --- |
| `index.md` | 完整文章（已过自检） |
| `outline.md` | 大纲 |
| `humanize_changes.md` | 去 AI 味改动清单 |
| `selfcheck.md` | 交付前自检报告（格式标记、前言字数、命中项） |
| `标题.cover_prompt.md` | 封面提示词（含风格选择、设计依据、可直接复制的提示词） |
| `cover.png` | 封面图 |
| `img_01.png` … | 文中插图 |

生成不出图片的位置会保留成引用块，方便你手动配图：

```markdown
> 📷 **配图建议：** 余额支付的同步调用链，支付服务依次调用用户服务……
> *（图片 API 未配置，请手动配图）*
```

括号里的说明会按真实原因变：`图片 API 未配置` / `插图数量已达上限` / `图片生成失败` / `插图生成已关闭`。

## 封面提示词

文章定稿之后（过了去 AI 味和自检），流水线会读一遍**最终正文**，做一次结构化分析，
然后输出一份可以直接复制到 ChatGPT ImageGen、即梦、Midjourney 等工具里的中文提示词，
存成 `输出目录/文章标题.cover_prompt.md`。

**它不出图，也不调用任何图片生成 API，不产生出图费用**，图片仍然由你手动生成。
这和 `image.provider: none` 的手动配图模式是一套思路：脚本负责「想清楚画什么」，出图交给你。

文件里包含七段：

| 段落 | 内容 |
| --- | --- |
| 一、文章信息 | 类型、技术领域、核心观点、关键机制 |
| 二、自动选择的风格 | 风格中文名 + 英文名 + 选择原因 |
| 三、核心视觉概念 | 视觉隐喻、主视觉、辅助元素、构图、配色 |
| 四、封面文字 | 主标题、副标题、保留的英文缩写 |
| 五、完整封面生成提示词 | 含标题排版，可直接复制 |
| 六、无文字背景图方案 | 同一张图但不出文字、留出标题预留区，供人工排版 |
| 七、质量检查 | 10 项检查结论（内部记录，**不属于要给图片模型的提示词**） |

### 两种风格

| 风格 | 英文名 | 什么时候选它 |
| --- | --- | --- |
| 高级技术杂志风 | Premium Technical Editorial | 价值落在「怎么用、怎么选、怎么配、怎么排错」上，比如教程、对比、实践指南、工具用法。排版和秩序感是主角。 |
| 抽象概念视觉风 | Abstract Technical Conceptualism | 价值落在「一个结构为什么这样运转」上，而且这个结构本身能画成有空间关系的图，比如容差带 + 阈值 + 退避、嵌套排序 + 检索路径 + 断口。 |

选择规则写成了三步判断，重点是两条防偏差：

- 几乎每篇技术文章都解释了某种机制，所以「文章里有机制」**不足以**选抽象风。
- 一批文章里判断结果几乎全是抽象风，说明第二步判太松，要回头重判。

### 改设计规范

设计规范全部集中在 `prompts/cover_design.txt`：

- 要改配色、留白、禁用元素（品牌视觉规范）→ 改「一、品牌视觉规范」。
- 要改某一种风格长什么样、适合什么题材 → 改「二」或「三」。
- 要改风格选择倾向 → 改「四、风格选择规则」。
- 要改标题怎么变短 → 改「六、标题处理规则」。
- 要改提示词必须覆盖哪些要素 → 改「七、最终提示词必须包含的要素」。

改这个文件不用动任何代码。程序依赖的输出格式（分析阶段的 JSON 字段、渲染阶段的
`[MAIN PROMPT]` / `[NO-TEXT PROMPT]` 标记）在 `src/cover_prompt.py` 顶部，
和 `skill_loader.py` 里的输出契约放在一起的是同一类东西，正常不需要改。

行为参数在 `config.yaml` 的 `cover_prompt` 节：`enabled`、`max_article_chars`（文章过长
时按「头 + 尾」截断）、`temperature` / `max_tokens`（分析阶段）、
`render_temperature` / `render_max_tokens`（渲染阶段）。

封面提示词失败不影响文章交付：模型返回非 JSON、缺字段、缺标记、写文件失败都会记日志后跳过，
已经生成好的 `index.md` 照常输出。文章太短（不足 200 字符）时不会硬出提示词，避免产出误导性的设计稿。

### 质量检查

提示词渲染完之后会自动过一遍 10 项检查，结论写在交付文件的**「七、质量检查」**一节。
那一节排在两份提示词的代码块**之后**，属于内部记录，不会被复制给图片模型。

| # | 检查项 | 怎么判 |
| --- | --- | --- |
| 1 | 核心主题理解 | 核心观点 / 关键机制是否提炼出来 |
| 2 | 风格与内容匹配 | 选择理由是否具体；提示词里写的风格名与分析的风格是否一致 |
| 3 | 核心视觉主体 | 主视觉是否具体到形状与结构 |
| 4 | 主体表达技术概念 | 主视觉与关键机制有没有共同的表述 |
| 5 | 构图与空间关系 | 构图里的方位词够不够 |
| 6 | 配色克制协调 | 主色数量是否在 2-3 种 |
| 7 | 留白充足 | 两份提示词里有没有留白要求 |
| 8 | 无无关装饰 | 有没有机器人、芯片、代码窗口这类通用装饰 |
| 9 | 文字不过量 | 含标题的那份是否真的有主标题；无文字方案是否偷偷要求渲染标题 |
| 10 | 构图不撞车 | 与最近几篇的构图骨架是否雷同 |

**检查本身不调用模型**，全是代码规则，所以不产生额外费用。只有命中 fail 级问题时
（画面里冒出机器人芯片、标题丢了、构图和上一篇骨架相同等）才允许**一次**针对性修正调用；
修正稿还要再查一遍，**只有问题确实变少才会被采纳**，否则保留原稿。

几个刻意的设计，都是踩过才定下来的：

- **禁用装饰词必须按「子句 + 否定」判断**。正确提示词里本来就写着「不要出现机器人、芯片」，
  朴素关键词匹配会把每一份合格产物都判成违规。
- **不数 hex 色值个数**。合格产物常用 3 主色 + 2 辅助色描述渐变，数 hex 会误伤。
- **构图查重用「位置 + 走向」骨架，不用文字相似度**。实测合格产物的 bigram 相似度只有
  0.095~0.132，哪怕骨架完全一样也分不出来。
- **标题匹配先归一化**（去空格标点、统一大小写全半角）。否则渲染模型给标题换个行就判「标题丢了」，
  变成每篇都触发付费修正。

历史构图来自 `output/*/*.cover_prompt.md`，**直接读文件，没有数据库**。最近 3 篇的构图会作为
避让参考喂给分析阶段，提示里写死了「只影响视觉主体与构图，绝不影响风格选择」——不能为了让
封面不重样，去改一篇本来适合那个风格的文章。当前没有任何历史文件时会自然跳过。

`quality_check: false` 可整体关掉；`repair: false` 可只检查、不花钱。
`history_count` 控制参考最近几篇（0 = 不看历史）。

### 给已有的旧文章补封面提示词

不需要重跑文章，直接读现成的 `index.md` 就行：

```bash
python -c "
import asyncio, sys
from pathlib import Path
sys.path.insert(0, '.')
from src.cover_prompt import generate_cover_prompt

d = Path('output/2026-10-10-mysql-联合索引的最左前缀原则-从一条走错索引的查询说起')
art = (d / 'index.md').read_text(encoding='utf-8')
title = [l.split(':', 1)[1].strip() for l in art.splitlines() if l.startswith('title:')][0]
print(asyncio.run(generate_cover_prompt(article=art, title=title, output_dir=str(d)))['path'])
"
```

## 交付前自检查什么

对应 learning-tech-blog skill「五、交付格式」的格式门禁，硬性问题会触发一次重写：

| 检查项 | 阈值 | 类型 |
| --- | --- | --- |
| 引用块数量 | ≤ 2 | 硬性 |
| 正文内联反引号 | ≤ 5 | 硬性 |
| 前言字数 | ≤ 120 | 硬性 |
| YAML frontmatter | 必须有 | 硬性 |
| 加粗片段数量 | ≤ 12 | 软性，超标提示，超两倍才要求压缩 |
| 禁用句式 | 先抑后扬、代码导读句、空过渡句、作者自述等 | 硬性 |
| 总结尾巴 | 「后续学习计划」这类清单 | 硬性 |
| 敏感词 / 模板残留 | 密码、密钥、`{{`、TODO | 提示 |
| 图片引用 | 相对路径文件是否存在 | 硬性 |

阈值在 `config.yaml` 的 `selfcheck.limits` 里改。

`> 📷 配图建议` 块不会被算进引用块数量，否则每篇待配图的文章都会误判超标。

## 项目结构

```
blog-pipeline/
├── 启动博客生成器.bat         # 双击启动网页界面
├── start_web.py            # 网页界面启动入口（切 CWD → 起服务 → 开浏览器）
├── run.py                  # 命令行入口
├── config.yaml             # 配置文件
├── CLAUDE.md               # 项目规则：什么任务用哪个 skill（不复述规范正文）
├── requirements.txt
├── .claude/
│   └── skills/
│       └── learning-tech-blog/    # 正文写作规范（项目级 skill，Claude Code 和脚本共用）
│           ├── SKILL.md           # 唯一一份写作规范：定位/风格/结构/准确性/格式/自检/封面
│           └── references/
│               └── course-note-blogging.md  # 有笔记材料时的课程笔记写法
├── prompts/                # 提示词模板（skill 读不到时的回退副本）
│   ├── blog-style.txt      # learning-tech-blog 的精简兜底副本
│   └── cover_design.txt    # 封面设计规范：品牌规范 + 两种风格 + 选择规则
├── src/
│   ├── pipeline.py         # 流水线编排（界面无关，靠回调往外抛进度）
│   ├── author_profile.py   # 写作者背景：存取 + 渲染成注入提示词的片段
│   ├── skill_loader.py     # 加载真实 skill 文件，带 prompts 回退
│   ├── material_reader.py  # 笔记/截图 → 素材文本（截图走视觉模型）
│   ├── writer.py           # 大纲 + 正文，文章判型、按自检结果修正
│   ├── humanizer.py        # 去 AI 味改写
│   ├── selfcheck.py        # 交付前自检 + 修正指令生成
│   ├── image_generator.py  # 插图生成（豆包 Seedream / 通义万相）
│   ├── cover_prompt.py     # 封面提示词：分析正文 → 选风格 → 渲染 → 质量检查（不出图）
│   ├── cover_history.py    # 扫历史封面提示词，提炼构图签名供新一篇避让
│   ├── llm_client.py       # 文本模型客户端（DeepSeek / OpenAI，参数自动适配）
│   ├── deepseek_client.py  # 兼容层，指向 llm_client（旧脚本 import 不报错）
│   ├── formatter.py        # 文章组装、frontmatter 补齐、配图建议
│   ├── config_loader.py    # 配置加载
│   └── utils.py            # 工具函数
├── webui/                  # 网页界面
│   ├── app.py              # Flask 路由
│   ├── jobs.py             # 任务队列 + worker 线程 + 日志转发
│   ├── history.py          # 扫描 output/、读文章和报告
│   ├── templates/index.html
│   └── static/{app.js,style.css}
├── data/                   # 运行时数据、日志
└── output/                 # 输出目录
```

命令行和网页界面共用 `src/pipeline.py`：它不打印任何东西，进度走 `on_progress` 回调、
大纲确认走 `confirm_outline` 回调。`run.py` 把回调接成 `print` 和 `input()`，
`webui/jobs.py` 把回调接成 SSE 事件流。

## 需要哪些 API

| 服务 | 用途 | 必需 |
| --- | --- | --- |
| DeepSeek（`deepseek-chat`） | 写作、去 AI 味、封面提示词、插图 prompt 转写，以及截图转录 | 是（当前用这个） |
| OpenAI（`gpt-4.1`） | 上面那些文字环节的另一个选择（截图转录仍走 DeepSeek） | 否，切过去才需要 |
| 火山方舟 `doubao-seedream-4-0-250828` | 插图生成 | 否，当前用手动配图 |

## 切换写作模型

写作模型负责所有「生成文字」的环节：正文、去 AI 味、封面提示词、插图 prompt 转写。
截图转录由 `vision` 节单独配置，不受切换影响。

**方式一：网页界面**（推荐，改完立刻生效，不用重启服务）

「写作背景」标签页 →「模型配置」卡片 → 点 DeepSeek / OpenAI 按钮。

切换会写回 `config.yaml` 的 `llm.provider`，**只改那一行**，文件里所有注释原样保留。
当前 provider 没配 key 时，提示行会标红告诉你缺哪个环境变量。

**方式二：改配置文件**

```yaml
llm:
  provider: "openai"      # 改成 deepseek 就切回去
```

两套配置都留着：`llm.deepseek` 和 `llm.openai` 各自保存自己的 api_key / base_url / model，
切 provider 不会丢另一套。

**模型参数是自适应的。** GPT-5 系列、o 系列拒收 `max_tokens`（必须用
`max_completion_tokens`），而且默认不接受 `temperature`。客户端会先按当前形状发一次，
收到「参数不支持」的报错就换形状重发，并把结论记住，同一个进程后续请求不再重复踩坑。
所以 `model` 填哪个都能跑，不需要提前查参数脾气。要省掉这一次探测请求，可以在 provider 节里钉死：

```yaml
  openai:
    model: "gpt-5.1"
    max_tokens_param: "max_completion_tokens"
    supports_temperature: false
```

**两家写起来有什么不一样**：

| | DeepSeek | OpenAI |
| --- | --- | --- |
| 中文技术写作 | 稳，术语准 | 更流畅，但更容易自己加篇幅 |
| 成本 | 低得多 | 高一些 |
| 网络 | 国内直连 | 需要代理（国内 IP 会被拒） |
| temperature | 有效 | gpt-4.1 系列有效；GPT-5 系列会被忽略 |

换模型后建议先跑一篇看看：去 AI 味和交付前自检的阈值当初是按 DeepSeek 调的，
换家之后命中情况可能不一样（自检报告里会写清哪几项超标）。

用别家的 OpenAI 兼容服务（中转、本地 Ollama / LM Studio 等）也走同一套：
在 `llm` 下加一个 provider 节、把 `provider` 指过去即可。`api_key` 留空时客户端会发一个
占位 key（OpenAI SDK 不接受空 key），本地服务和中转服务一般都忽略这个头。

### 换 DeepSeek 模型要注意「思考模型」

`deepseek-chat` 是**普通模型**，不思考，实测写作稳定，当前就用它。
但 `deepseek-flash` 和 `deepseek-v4-pro` 是**思考模型**：它们先输出一段推理，
**这段推理 token 也算在 `max_tokens` 里**，而且按输出计费。

实测（同一题材、同一提示词、`max_tokens: 8192`）：

| 模型 | 结果 |
| --- | --- |
| `deepseek-chat` | 15s，3778 字符，完整成文 |
| `deepseek-flash`（不关思考） | 44s，**0 字符**（思考把 8192 吃光） |
| `deepseek-flash` + `reasoning_effort: "none"` | 14s，3819 字符，正常 |
| `deepseek-v4-pro`（不关思考） | 89s，397 字符 |

所以换这两个模型**必须同时关掉思考**：

```yaml
  deepseek:
    model: "deepseek-flash"
    reasoning_effort: "none"     # 关掉思考；普通模型（deepseek-chat）不需要这行
```

不关也不会静默出错 —— 客户端检测到「返回空内容 + 有思考痕迹 + 被 max_tokens 截断」时
会直接报错并告诉你这么改，而不是安安静静写出一篇空文章：

```
DeepSeek 的 deepseek-flash 返回了空内容：思考过程把 max_tokens=8192 用完了，正文没来得及写。
这是思考模型（deepseek-flash / deepseek-v4-pro 这类）的典型表现。
修复：在 llm.deepseek 里加一行 reasoning_effort: "none" 关掉思考，或者把 max_tokens 调大。
```

顺便一提，`reasoning_effort` 要是被普通模型拒绝了（报参数不支持），客户端会自动去掉它重发。

## 已知限制

- **豆包的模型 ID 没能对着官方原文核实**。抓取官方文档被网络策略拦了，模型名来自搜索结果汇总。
  如果提示模型不存在或未开通，去方舟控制台核对实际模型 ID，改 `config.yaml` 里对应那一行。
  DeepSeek 那条路径是实测过的，不受影响。
- **豆包的图片接口参数以官方文档为准**。`size`、`response_format`、`watermark` 这几项
  如果方舟侧有调整，改 `src/image_generator.py` 的 `_generate_doubao` 即可，只影响一处。
- **加粗数量是软性约束**。模型倾向多用加粗，自检报告里会提示，超过阈值两倍时修正指令会要求它压缩，但不强制重写。
- **转录和写作是两道关，数字漂移可能发生在第二道**。截图转录经核对是逐格准确的，
  但写作阶段仍可能自己改数字，所以材料区块加了「数值原样引用」的约束。
  交付前建议扫一眼文章里的关键数值和原始材料是否一致，自检目前不校验数字。
