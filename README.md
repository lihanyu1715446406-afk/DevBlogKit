# Blog Pipeline — 技术博客自动生产流水线

输入一个题材（可选附上笔记或课程截图），自动产出一篇结构完整的中文技术博客：
判型 → 大纲 → 正文 → 去 AI 味 → 格式自检 → 配图建议 → 封面提示词，最后组装成可直接发布的 Markdown。

**它解决什么问题**：写技术博客最耗时的不是打字，是「想清楚结构、把原理讲透、再删掉 AI 味」。
这个项目把这套流程固化成一条流水线，并且写文章时始终遵循同一份写作规范。

**适合谁用**：有明确技术选题、想稳定产出结构化博客的人。对「先写清楚结构再落笔」有要求、
而不是随手发一条动态的场景最合适。

**设计理念**（几条贯穿全项目的取舍）：

1. **只产出文本，不自动出图、不自动发布。** 图片由你手动生成，避免不可控的版式和费用。
2. **写作规范只有一份。** Claude Code 和 Python 脚本读同一个文件，改一处两边生效。
3. **失败要降级，不能中断。** 文章定稿后，去 AI 味、自检、封面提示词任一步失败都只是少一个附件。
4. **规范放文件，程序依赖的格式放代码。** 想调风格改 `SKILL.md`，不用动代码。

---

## 一、核心功能

| 功能 | 说明 | 入口 | 状态 |
| --- | --- | --- | --- |
| 文章生成 | 大纲 + 正文两次调用，按题材判型组织结构 | 网页「生成文章」/ `python run.py "题材"` | 已实现 |
| 材料读取 | `.md`/`.txt` 直读；截图走视觉模型逐条转录 | `-m 路径`（可重复） | 已实现 |
| 文章判型 | 有材料 → 课程笔记文；无材料 → 知识点文；可手动指定 | `--type` | 已实现 |
| 去 AI 味 | 按 Humanizer 规则改写一遍，校验不通过自动回退原文 | 默认开启，`--no-humanize` 关闭 | 已实现 |
| 交付前自检 | 数格式标记、量前言字数、扫禁用句式，不合格回喂重写一次 | 默认开启，`--no-selfcheck` 关闭 | 已实现 |
| 大纲确认 | 先出大纲，你点确认再写正文 | `--review` | 已实现 |
| 封面提示词 | 分析正文 → 选风格 → 渲染两份中文提示词 → 质量检查 | 默认开启，`--no-cover-prompt` 关闭 | 已实现 |
| 封面提示词质量检查 | 10 项纯规则检查；命中 fail 问题时做一次针对性修正 | 配置 `cover_prompt.quality_check` | 已实现 |
| 插图生成 | 豆包 Seedream / 通义万相自动出图 | `image.provider` 切换 | 已实现（默认关闭） |
| 手动配图 | 不出图，每个插图位置留一条配图建议 | `image.provider: none`（默认） | 已实现 |
| 网页界面 | 生成、历史、封面提示词、写作规范、设置 | `python start_web.py` | 已实现 |
| 历史浏览 | 搜索/筛选历史文章，预览正文、自检报告、封面提示词 | 网页「历史文章」 | 已实现 |
| 写作者背景 | 把身份/技术栈注入写作提示词作轻度参考 | 网页「设置与帮助」 | 已实现 |
| 写作模型切换 | DeepSeek ↔ OpenAI，写回 config.yaml | 网页「设置与帮助」 | 已实现 |

**计划实现**：暂无明显缺口。后续方向见文末「后续规划」。

## 二、界面预览

当前仓库内**没有截图文件**，所以这里描述真实布局，等截图补齐后再替换成图片。

网页界面是**左侧导航 + 右侧内容**的结构：

```
┌──────────────┬────────────────────────────────────────────┐
│ 博客生成器    │  ⚠️ 配置提醒（有才显示）                     │
│              ├────────────────────────────────────────────┤
│ 首页          │                                            │
│ 生成文章      │   运行状态：写作模型 / 转录模型 / 插图 /     │
│ 历史文章      │             写作规范来源 / 输出目录          │
│ 封面提示词    │                                            │
│ 写作规范      │   最近生成：最近 5 篇，点开看详情            │
│ 设置与帮助    │                                            │
│              │                                            │
│ 就绪          │                                            │
└──────────────┴────────────────────────────────────────────┘
```

窄屏（≤860px）下侧栏折叠成顶部标题栏，点 ☰ 展开导航。

## 三、技术栈

| 层 | 用了什么 |
| --- | --- |
| 语言 | Python 3.10+（开发环境 3.14） |
| 模型接口 | OpenAI 兼容 Chat Completions。默认 DeepSeek（`deepseek-chat`），可选 OpenAI |
| 网页后端 | Flask（本地 127.0.0.1，单 worker 线程 + asyncio） |
| 网页前端 | 原生 HTML / CSS / JavaScript，**无框架、无构建步骤、无 npm** |
| Markdown 渲染 | `markdown` 包（只在网页预览里用） |
| 依赖 | 见 `requirements.txt`：pyyaml、openai、requests、python-slugify、flask、markdown（dashscope 可选） |

**没有用到**：数据库、消息队列、前端框架、容器、云服务。所有状态都在本地文件里。

## 四、环境准备

- **Python 3.10 或更新**（用到了 `X | None` 这类类型标注语法）。
- **一个模型服务的 API Key**。默认用 DeepSeek：到 platform.deepseek.com 拿 key。
  想用 OpenAI 就换成 `OPENAI_API_KEY`（需要梯子 + 预付费额度，和 ChatGPT 会员无关）。
- 可选：要自动出图才需要 `ARK_API_KEY`（豆包）或 `DASHSCOPE_API_KEY`（通义万相）。
  默认 `image.provider: none`，**不需要这些 key 也能跑完全流程**。

## 五、快速开始

```bash
# 1. 进入项目
cd blog-pipeline

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 API Key（Windows PowerShell 用 $env:，cmd 用 set）
#    macOS / Linux:
export DEEPSEEK_API_KEY="你的key"

# 4. 启动网页界面（推荐）
python start_web.py
# 也可以双击「启动博客生成器.bat」

# 5. 或者直接用命令行生成一篇
python run.py "RabbitMQ 入门：同步调用与异步调用"
```

生成结果在 `output/日期-标题/` 下：`index.md` 就是最终文章。

> 配置项实在太多记不住？把 `config.yaml` 留着不动即可——所有键都有默认值，
> 只有 API Key 是必须的。

## 六、使用指南

### 用网页界面（推荐）

1. **生成文章**：填题材 →（可选）加笔记/截图材料 → 勾选项 → 加入队列。
   任务在后台顺序执行，日志实时滚动。
2. **历史文章**：搜索或按类型筛选；点开某篇可以看正文预览、**封面提示词**、自检报告、
   去 AI 味改动、大纲、原始 Markdown，并能一键复制。
3. **封面提示词**：列出所有带封面提示词的文章，点进去直接复制到出图工具。
4. **写作规范**：查看**实际生效**的规范原文，以及改动要改哪个文件。
5. **设置与帮助**：切换写作模型、填写作者背景、看文章类型判定规则和操作说明。

**队列行为**：顺序执行，不会同时写两篇；可以连续提交多篇。勾了「先确认大纲」的任务会停住等你点确认——
注意队列是串行的，一篇卡住后面的也会一起等。取消在**阶段边界**生效（正在飞的模型调用中断不了）。

### 用命令行

```bash
# 只给题材
python run.py "Java 21 虚拟线程深入解析"

# 指定分类和标签
python run.py "xxx" --category "Java" --tags "Java, 并发, 性能"

# 带材料：笔记文件或截图目录，可重复传；有材料会自动判成课程笔记文
python run.py "RabbitMQ 入门" -m ./笔记.md -m ./截图目录

# 指定文章类型（默认 auto）
python run.py "xxx" --type course-note

# 先出大纲，确认后再写正文
python run.py "xxx" --review

# 快速模式：跳过大纲直接写
python run.py "xxx" --fast

# 各阶段开关
python run.py "xxx" --no-images          # 跳过插图，全部保留为配图建议
python run.py "xxx" --no-humanize        # 跳过去 AI 味
python run.py "xxx" --no-selfcheck       # 跳过交付前自检
python run.py "xxx" --no-cover-prompt    # 跳过封面提示词

# 指定输出目录
python run.py "xxx" --output ./output/我的目录
```

### 封面提示词怎么用

生成后存在 `output/日期-标题/标题.cover_prompt.md`，含七段：文章信息、风格选择、
核心视觉概念、封面文字、**完整提示词**、**无文字背景图方案**、质量检查结论。

直接把第五段（含标题排版）或第六段（无文字、留标题预留区）复制到 ChatGPT ImageGen、
即梦、Midjourney 出图即可。**脚本不出图、不调用任何图片生成接口、不产生出图费用。**

### 调整写作风格

改 `.claude/skills/learning-tech-blog/SKILL.md`。这是**唯一一份**写作规范：
Claude Code 把它当 Skill 加载，Python 脚本在写文章前把同一个文件注入模型请求。
改完两边同时生效，不需要改代码。详见「Skill 说明」。

### 排查常见错误

| 现象 | 原因与处理 |
| --- | --- |
| 命令行提示 API Key 未设置 | 环境变量没配或没生效，见「快速开始」第 3 步 |
| 网页打不开 / 点提交报「连不上后端服务」 | 服务被关了。重新双击「启动博客生成器.bat」，再刷新页面 |
| 文章是空的 / 报「返回了空内容」 | 多半是用了「思考模型」把输出预算吃光，见「常见问题」 |
| 某篇没有 `selfcheck.md` | 生成时勾了「跳过自检」 |
| 某篇没有封面提示词文件 | 生成时勾了「跳过封面提示词」，或该阶段失败了（看日志） |
| 文章里的图片显示不出来 | 手动配图模式下引用块要替换成 `![说明](文件名)`，文件放进同一目录 |
| 同名同日生成两篇 | 不会互相覆盖，第二个目录自动加 `-2` 后缀 |

## 七、项目目录结构

```
blog-pipeline/
├── run.py                     # 命令行入口（只负责参数解析与终端渲染）
├── start_web.py               # 网页入口（找空闲端口、起服务、开浏览器）
├── config.yaml                # 全部配置（含注释，用定点替换修改，不要整体重写）
├── CLAUDE.md                  # 项目规则：什么任务用哪个 skill
├── .claude/skills/
│   └── learning-tech-blog/    # 写作规范（项目级 skill，Claude Code 与脚本共用）
│       ├── SKILL.md           # 唯一一份写作规范
│       └── references/course-note-blogging.md
├── prompts/                   # 提示词与规范副本
│   ├── writing.txt            # 写作主模板（格式骨架：frontmatter、字数、[IMAGE:] 约定）
│   ├── cover_design.txt       # 封面设计规范：品牌规范 + 两种风格 + 选择规则
│   ├── blog-style.txt         # 写作规范的精简兜底副本（读不到 skill 时用）
│   └── humanize.txt           # 去 AI 味兜底副本
├── src/
│   ├── pipeline.py            # 流水线编排（界面无关，靠回调对外）
│   ├── writer.py              # 大纲 + 正文；文章判型；按自检结果修正
│   ├── humanizer.py           # 去 AI 味改写
│   ├── selfcheck.py           # 交付前自检（格式门禁）+ 修正指令生成
│   ├── cover_prompt.py        # 封面提示词：分析 → 渲染 → 修正 → 组装落盘
│   ├── cover_quality.py       # 封面质量检查（纯规则，不调模型）
│   ├── cover_history.py       # 扫历史封面提示词，提炼构图签名供避让
│   ├── image_generator.py     # 插图生成（豆包 / 通义万相），默认关闭
│   ├── material_reader.py     # 笔记/截图 → 素材文本（截图走视觉模型）
│   ├── author_profile.py      # 写作者背景
│   ├── skill_loader.py        # 加载写作/去 AI 味规范，带 prompts 回退
│   ├── text_utils.py          # 共用文本工具（代码围栏剥离、[IMAGE:] 标记）
│   ├── llm_client.py          # 文本模型客户端（参数形状自适应）
│   ├── deepseek_client.py     # 兼容层：旧 import 不报错
│   ├── config_loader.py       # 配置加载 + 默认值常量
│   ├── formatter.py           # 文章组装、frontmatter 补齐、配图建议
│   └── utils.py               # 文件读写、重试、输出目录命名
├── webui/                     # 网页界面
│   ├── app.py                 # Flask 路由
│   ├── jobs.py                # 任务队列 + worker 线程 + SSE 事件流
│   ├── history.py             # 扫描 output/ 读取历史文章与报告
│   ├── templates/index.html
│   └── static/{app.js,style.css}
├── output/                    # 生成的文章（不进版本控制）
└── data/                      # 日志与写作者背景
```

## 八、配置文件与环境变量

### 环境变量

| 变量 | 用途 | 必需 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 写作 + 截图转录（默认 provider） | 是（用 DeepSeek 时） |
| `OPENAI_API_KEY` | 写作（切到 OpenAI 时） | 否 |
| `ARK_API_KEY` | 插图生成（豆包） | 否 |
| `DASHSCOPE_API_KEY` | 插图生成（通义万相） | 否 |

配置里用 `${VAR}` 占位符引用，代码不硬编码读取任何环境变量。

### 主要配置项

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `llm.provider` | `deepseek` | 写作模型；改为 `openai` 即切换 |
| `llm.deepseek.model` | `deepseek-chat` | 换模型只改这一行 |
| `content.min_words` / `max_words` | 1500 / 4000 | 字数区间，注入写作提示词 |
| `content.writing_focus` | （一段取向说明） | 注入写作提示词，决定怎么取舍 |
| `content.default_category` / `default_tags` | `技术` / `[]` | frontmatter 默认值，命令行可覆盖 |
| `skills.primary` | `skill` | `skill` = 读真实 skill 文件；`prompts` = 只用副本 |
| `skills.specs.csdn.dir` | `.claude/skills/learning-tech-blog` | 写作规范位置（相对路径按项目根解析） |
| `selfcheck.limits` | quote 2 / inline_code 5 / bold 12 / intro_chars 120 | 格式门禁阈值 |
| `humanize.enabled` | `true` | 是否做去 AI 味 |
| `cover_prompt.enabled` | `true` | 是否产出封面提示词 |
| `cover_prompt.quality_check` | `true` | 10 项规则检查（不调模型、不花钱） |
| `cover_prompt.repair` | `true` | 命中 fail 时允许一次针对性修正调用 |
| `cover_prompt.history_count` | `3` | 参考最近几篇构图做避让；0 = 不看历史 |
| `cover_prompt.quality_limits` | （不在文件里） | 覆盖质量检查阈值，需要时自行添加 |
| `image.provider` | `none` | `none` = 手动配图；`doubao` / `dashscope` = 自动出图 |
| `output.base_dir` | `./output` | 输出根目录 |
| `web.host` / `web.port` | `127.0.0.1` / `8756` | 只监听本机；端口占用会自动顺延 |
| `logging.level` / `logging.file` | `INFO` / `./data/pipeline.log` | 日志 |

**注意事项**：

- 目录名固定为 `日期-标题slug`（撞名加 `-2`），文章文件名固定为 `index.md`，这两项没有配置项。
- 改 `config.yaml` **手工编辑**。文件里的注释是文档，用 `yaml.dump` 整体重写会把注释全部丢掉。
  网页上切换模型走的是定点替换，只改 `llm.provider` 那一行。
- 优先级：命令行参数 > 配置文件 > 代码里的默认常量（集中在 `config_loader.py`）。

## 九、Skill 说明

**`learning-tech-blog` 已经接入**，不是待办。它分两条轨，共用**同一份文件**：

| 轨 | 谁在用 | 怎么生效 |
| --- | --- | --- |
| Claude Code | 你在 Claude Code 里让它写博客/整理笔记/讲解原理/润色/出封面提示词时 | 项目级 skill，靠 `CLAUDE.md` 的触发约定自动加载 |
| Python 脚本 | `python run.py` / 网页生成文章时 | `src/skill_loader.py` 在每次写作调用前，把同一个 `SKILL.md` 注入模型请求 |

**关键点**：Claude Code 的 Skill **不会**自动进入 Python 的模型请求——那是两套独立的进程。
项目通过「共享同一个规范文件」来保证两边一致，而不是假设 Skill 能传递过去。

- 规范文件：`.claude/skills/learning-tech-blog/SKILL.md`
- 兜底副本：`prompts/blog-style.txt`（skill 目录读不到时用，内容同源、更精简）
- 去 AI 味是另一套规则，仍用用户级 skill `~/.claude/skills/Humanizer-zh`
- 界面上「写作规范」页展示的就是这个文件的正文；页面顶部会显示**实际生效的路径**

**怎么维护**：只改 `SKILL.md`。改完同步一下 `prompts/blog-style.txt`（兜底副本），
不要在 `CLAUDE.md` 或别处另建副本——那正是改造前的问题所在。

> 内部代码里这个 skill 的 key 仍叫 `csdn`（历史命名）。改名要动四五处调用链，
> 收益只是好看一点，所以保留；对外显示的名字是 `learning-tech-blog`。

## 十、常见问题

**Q：报「返回了空内容」或文章是空的？**
多半是用了「思考模型」（deepseek-flash / deepseek-v4-pro 这类）。思考 token 也算在 `max_tokens` 里，
预算被思考吃光后正文就是空的。两种解法：换回 `deepseek-chat`，或在 `llm.<provider>` 下加
`reasoning_effort: "none"` 关掉思考。

**Q：换成 GPT-5 系列要注意什么？**
客户端会自动改用 `max_completion_tokens` 并去掉 `temperature`，模型错误会自动纠正并记住，
不用改代码。但那样 `temperature` 就失效了，风格控制全靠提示词。

**Q：只想手动配图，会额外花钱吗？**
不会。默认 `image.provider: none`，脚本只输出配图建议和封面提示词，不调用任何图片生成接口。
封面质量检查也是纯规则，不额外调用模型。

**Q：一篇大概调用几次模型？**
默认配置下稳定 6 次：大纲、正文、去 AI 味、封面分析、封面渲染、（自检不合格时）+1 次修正。
实测约 27% 的文章会触发那次修正。勾「跳过大纲」可省 1 次。有截图材料时每张图另加 1 次转录。

**Q：自检不合格会怎样？**
会回喂给模型重写一次（最多一次）。仍不合格就把问题写进 `selfcheck.md`，文章照常交付——
**不会因为格式问题丢掉已经写好的正文**。

**Q：文章会不会被覆盖？**
不会。同名同日生成两篇时，第二个目录自动加 `-2`。但手动改同名文件仍可能覆盖，注意一下。

**Q：网页上的「跳过插图」「跳过封面提示词」有什么差别？**
「跳过插图」只影响文中插图位置的处理；「跳过封面提示词」则完全不产出封面提示词文件。
两者都不影响正文。

**Q：`output/` 会进版本控制吗？**
不会。`.gitignore` 已排除 `output/`、日志和 `__pycache__`，`data/writer_profile.json`
（本机个人背景）默认也不纳入。想让写作者背景进版本控制，删掉 `.gitignore` 里对应那行即可。

## 十一、后续规划

**已实现**（本轮整理）：

- 清理了 7 处无消费者的死代码/死配置，合并了三组重复的文本工具与重复函数
- 拆出 `cover_quality.py`（纯规则）与 `text_utils.py`（共用工具）
- 界面从 4 个顶栏 tab 升级为 6 个侧边导航页面，新增首页、封面提示词页、写作规范页
- 补齐了两个后端本来就支持、但界面点不到的开关
- README 按真实实现重写，修掉 6 处事实偏差
- 建立 Git 版本控制基线

**计划实现**：

- **写作规范收敛**（收益最大，但会改变产出，放最后做）：
  目前规范仍有两处冗余——`prompts/writing.txt` 里还留着与 `SKILL.md` 重复的写作要求
  和一段过期的「两步写作流程」；`src/writer.py` 的 `ARTICLE_TYPES`（3 类）与
  `SKILL.md` 的类型体系（5 类）是两套分类法，同时注入同一个提示词。
  目标是 `SKILL.md` 成为唯一真源，`writing.txt` 退化为纯格式骨架。
- 禁用表达清单目前在三处维护（规范散文、去 AI 味副本、自检正则），计划以正则为唯一可执行版本。
- 界面截图：仓库内暂无截图，补齐后替换「界面预览」一节的文字说明。

## 十二、已知限制

- **没有测试框架**。验证靠离线断言脚本 + 手动点测，改动后需要人工确认。
- **任务队列是单进程的**。关掉网页服务，正在跑的任务会中断（文章不会损坏，目录里是半成品）。
- **中文文字生成依赖图片模型**。封面提示词会给一份「无文字背景图方案」供人工排版，
  因为多数文生图模型渲染中文标题都不稳定。
- **文章语言固定为中文**，没有多语言支持。
