# blog-pipeline 项目规则

技术博客自动生成流水线：材料 → 判型 → 写作 → 去 AI 味 → 自检 → 配图 → 封面提示词 → 组装。

## 什么时候用哪个 skill

**涉及技术博客写作的任务，先读 `learning-tech-blog` skill**
（`.claude/skills/learning-tech-blog/SKILL.md`）：

- 写新文章、把笔记或代码整理成文章
- 讲解技术原理、整理学习笔记
- 润色、改写已有文章
- 检查文章的技术准确性和 AI 模板化表达
- 生成博客封面提示词

**去 AI 味改写**用 `Humanizer-zh`（`~/.claude/skills/Humanizer-zh/SKILL.md`）。

**与博客写作无关的任务**（改脚本逻辑、调配置、修 bug、调界面）不要套用写作 skill，
按普通开发任务处理。

## 规范只有一份

完整写作规范在 `learning-tech-blog/SKILL.md`，**不要在本文件里复述或另建副本**。
`src/skill_loader.py` 会在写文章前把同一个文件注进模型提示词——改规范只改 skill 文件，
两边同时生效。本文件只负责说明「何时调用」。

## 这个项目的硬约束

- **不自动出图、不发布**：封面模块只输出提示词文本（`src/cover_prompt.py`），
  配图默认手动（`image.provider: none`）。不要接入图片生成 API 或任何发布动作，
  除非明确要求。
- **改 `config.yaml` 手工编辑**：文件里的注释是给人看的文档，
  禁止用 `yaml.dump` 整体重写（会丢注释）。只做定点替换。
- **改动保守可回退**：文章生成是主链路，写作、去 AI 味、自检、封面提示词各阶段
  失败都只降级不中断——文章已经生成成功时，后续阶段出问题不能影响交付。

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `run.py` | 命令行入口 |
| `start_web.py` / `webui/` | 网页界面 |
| `src/pipeline.py` | 流水线编排（各阶段的事件、进度、取消） |
| `src/skill_loader.py` | 加载写作/去 AI 味规范，回退到 `prompts/` 副本 |
| `src/cover_prompt.py` | 封面提示词：分析正文 → 选风格 → 渲染 → 质量检查 |
| `prompts/cover_design.txt` | 封面设计规范（品牌规范 + 两种风格 + 选择规则） |
