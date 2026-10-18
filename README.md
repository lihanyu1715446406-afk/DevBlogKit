# Blog Pipeline

把技术选题（可附笔记、截图）变成一篇结构完整、可直接发布的中文技术博客。

流水线：**材料读取 → 文章判型 → 大纲 → 正文 → 去 AI 味 → 交付前自检 → 配图建议 → 封面提示词 → 组装 Markdown**。

它解决的不是「让模型随便写一篇」，而是把「先想清楚结构、把原理讲透、再删掉 AI 味」固化成一条可重复执行的流程。写作规范只有一份，Claude Code 和 Python 脚本读同一个文件。

## 特点

- **本地运行**：Flask + 原生 HTML/CSS/JS，无数据库、无前端构建步骤、无云服务。
- **写作规范可替换**：规范在 `.claude/skills/learning-tech-blog/SKILL.md`，改文件即可调整文风，不用改代码。
- **失败只降级，不中断**：去 AI 味、自检、封面提示词任一阶段失败，已生成的正文照常交付。
- **默认不出图、不发布**：只输出配图建议和封面提示词，图片与发布由你手动完成。
- **模型可切换**：默认 DeepSeek，也可切到 OpenAI，共用一套 OpenAI 兼容接口。

## 环境要求

- Python 3.10 或更新版本
- 一个文本模型 API Key（默认 DeepSeek；也支持 OpenAI）
- 可选：截图转录、自动出图需要对应的视觉/图片模型 Key

## 安装

```bash
git clone <repo-url>
cd blog-pipeline
pip install -r requirements.txt
```

配置 API Key：

```bash
# macOS / Linux
export DEEPSEEK_API_KEY="sk-..."

# Windows PowerShell
$env:DEEPSEEK_API_KEY = "sk-..."

# Windows cmd
set DEEPSEEK_API_KEY=sk-...
```

默认配置使用 DeepSeek 的 `deepseek-chat`。想换模型，编辑 `config.yaml` 的 `llm.provider`（`deepseek` / `openai`），并设置对应的 `OPENAI_API_KEY`。

## 快速开始

### 网页界面（推荐）

```bash
python start_web.py
```

默认打开 <http://127.0.0.1:8756/>；端口被占用时会自动顺延。Windows 也可以双击 `启动博客生成器.bat`。

界面包含：生成文章、历史文章、封面提示词、写作规范、设置与帮助。任务在后台按顺序执行，日志实时滚动；勾选「先确认大纲」后，任务会停下等你确认。

### 命令行

```bash
# 只给题材
python run.py "Java 21 虚拟线程深入解析"

# 带笔记文件或截图目录（可重复传入）
python run.py "RabbitMQ 入门" -m ./笔记.md -m ./截图目录

# 指定分类和标签
python run.py "MySQL 索引优化" --category "数据库" --tags "MySQL,索引"

# 先出大纲，确认后再写正文
python run.py "Redis 持久化" --review
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `topic` | 文章题材（位置参数） |
| `-m, --material PATH` | 笔记或截图目录，可重复传入；有材料时默认判为课程笔记文 |
| `--type` | `auto` / `knowledge` / `course-note` / `feature`，默认 `auto` |
| `--review` | 先输出大纲，确认后再写正文 |
| `--fast` | 跳过大纲，直接写正文 |
| `--category` / `--tags` | frontmatter 的分类与标签 |
| `-o, --output DIR` | 指定输出目录 |
| `--no-images` | 跳过插图，保留配图建议 |
| `--no-humanize` | 跳过去 AI 味改写 |
| `--no-selfcheck` | 跳过交付前自检 |
| `--no-cover-prompt` | 跳过封面提示词 |

## 输出

每次生成会创建 `output/日期-标题/`，目录内通常包含：

```
output/2026-10-18-一次-ping-到底走了多少层/
├── index.md                              # 最终文章（可直接发布）
├── outline.md                            # 大纲
├── humanize_changes.md                   # 去 AI 味改动清单（该阶段成功时）
├── selfcheck.md                          # 自检报告
└── 一次 ping 到底走了多少层：....cover_prompt.md   # 封面提示词
```

- 目录名固定为 `日期-标题slug`，同日同名会自动加 `-2`，不会互相覆盖。
- 最终文章文件名固定为 `index.md`。
- 自动出图开启时，目录内还会出现生成的图片。

## 封面提示词

封面阶段只做三件事：分析正文、选择视觉风格、输出可直接粘贴到图片生成工具的中文提示词。

产物包含完整提示词和无文字背景图方案，**脚本不调用任何图片生成接口，不产生出图费用**。质量检查是纯规则检查，不额外调模型；命中问题时最多做一次针对性修正。

## 配置

所有配置在 `config.yaml`，API Key 用 `${ENV_VAR}` 占位符引用。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `llm.provider` | `deepseek` | 写作模型，可选 `deepseek` / `openai` |
| `llm.deepseek.model` | `deepseek-chat` | 默认写作模型 |
| `content.min_words` / `max_words` | `1500` / `4000` | 正文字数区间 |
| `humanize.enabled` | `true` | 是否做去 AI 味 |
| `selfcheck.enabled` | `true` | 是否做交付前自检 |
| `cover_prompt.enabled` | `true` | 是否生成封面提示词 |
| `image.provider` | `none` | `none` 为手动配图；`doubao` / `dashscope` 为自动出图 |
| `output.base_dir` | `./output` | 输出根目录 |
| `web.host` / `web.port` | `127.0.0.1` / `8756` | 网页服务地址，只监听本机 |

环境变量：`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`；自动出图时另需 `ARK_API_KEY`（豆包）或 `DASHSCOPE_API_KEY`（通义万相）。

> `config.yaml` 里的注释就是配置文档，请手工编辑。用 `yaml.dump` 整体重写会丢失全部注释。

## 写作规范

写作规范只有一份：`.claude/skills/learning-tech-blog/SKILL.md`。

- 在 Claude Code 里写博客、整理笔记、讲解原理、润色、生成封面提示词时，它作为项目级 Skill 加载。
- `python run.py` 和网页生成时，`src/skill_loader.py` 会把同一个文件注入模型请求。

改文风只改这个文件，两边同时生效。去 AI 味使用 Humanizer 规则：本机安装了 `~/.claude/skills/Humanizer-zh` 时读取真实 skill，否则自动回退到 `prompts/humanize.txt` 的精简副本，因此开箱即可运行。

## 注意事项

- **AI 生成内容需要人工核验**。技术细节、代码示例和事实性表述在发布前请自行检查。
- **默认不自动出图、不自动发布**。`image.provider: none` 时不会调用任何图片生成接口。
- **会产生模型调用费用**。默认配置下一篇约 5 次调用（大纲、正文、去 AI 味、封面分析、封面渲染）；自检或封面质量检查命中问题时，各自最多再加 1 次修正调用。有截图材料时，每张图另加 1 次转录。
- **思考模型要留足输出预算**。`deepseek-flash`、`deepseek-v4-pro` 这类模型的思考 token 也占用 `max_tokens`，可能把正文挤空；建议换回 `deepseek-chat`，或配置 `reasoning_effort: "none"`。
- **任务队列是单进程的**。关闭网页服务会中断正在跑的任务，已写出的文件保留为半成品。
- **网页默认只监听本机**（`127.0.0.1`），不要直接暴露到公网。
- **文章语言目前固定为中文**。
- **暂无自动化测试框架**，改动后需要手动验证主流程。
- `output/`、日志和 `data/writer_profile.json`（本机作者背景）已在 `.gitignore` 中排除。

## 项目结构

```
blog-pipeline/
├── run.py                     # 命令行入口
├── start_web.py               # 网页入口
├── config.yaml                # 全部配置
├── requirements.txt
├── .claude/skills/
│   └── learning-tech-blog/    # 写作规范（Claude Code 与脚本共用）
├── prompts/                   # 写作、去 AI 味、封面设计提示词
├── src/
│   ├── pipeline.py            # 流水线编排
│   ├── writer.py              # 判型、大纲、正文
│   ├── humanizer.py           # 去 AI 味
│   ├── selfcheck.py           # 交付前自检
│   ├── cover_prompt.py        # 封面提示词
│   ├── skill_loader.py        # 规范加载与回退
│   └── ...
├── webui/                     # Flask 界面、任务队列、历史浏览
├── output/                    # 生成结果（不进版本控制）
└── data/                      # 日志与本地作者背景
```

## License

[MIT](LICENSE)
