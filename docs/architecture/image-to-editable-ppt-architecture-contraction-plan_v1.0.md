# Image-to-Editable-PPT 架构收缩与职责分离方案 v1.0

## 1. 文档目的

本文档用于指导 `image-to-editable-ppt` 从当前混合架构收缩为两个职责清晰的系统：

```text
公开可安装 Skill
= 面向用户的快速图片转可编辑 PPT 产品

私有验证仓库
= 面向开发者的严格测试、审核、证据与发布系统
```

本轮工作的目标不是继续增加功能，而是降低结构复杂度、缩短普通转换路径，并明确哪些文件属于用户运行时、哪些文件只属于开发验证。

## 2. 当前问题

当前可安装 Skill 同时承担以下三类职责：

1. 图片分析、元素分类和可编辑 PPT 构建。
2. 独立视觉审核、评分、修订状态机和交付决策。
3. README 示例验证、发布证据、资格测试和七文件打包。

这些职责混合后产生了以下问题：

- 普通用户转换被迫进入发布级流程。
- 结构 QA 通过后仍不能直接形成用户交付。
- Planner、Reviewer、评分器、状态机和打包器之间的边界不直观。
- Schema 数量持续增加，多个版本和不同用途的契约共存。
- 用户运行文件、开发工具、测试代码和发布审计难以区分。
- 每次修复都可能影响整条发布链，导致实现和回归时间过长。
- Skill 使用者需要理解与日常转换无关的状态和证据概念。

## 3. 目标架构

### 3.1 公开可安装 Skill

公开 Skill 只提供用户转换能力：

```text
用户上传图片
→ Planner Agent 规划
→ 规格与资产校验
→ PPT 构建
→ PowerPoint 渲染
→ 结构 QA
→ 独立 Reviewer Agent
→ 必要时最多一次定向修订
→ PPTX、预览图和转换摘要
```

用户模式的关键约束：

- Planner 和 Reviewer 每次都必须执行，并使用不同的新上下文。
- Reviewer 只负责快速视觉审核，不负责发布评分和交付政策。
- 最多允许一次语义修订和一次技术重试。
- 不生成资格证据、人工验收记录或七文件发布包。
- 最终用户只需要理解输入图片、可编辑 PPT、预览图和警告。

### 3.2 私有验证仓库

私有验证仓库负责开发模式：

```text
规划
→ 构建
→ 结构 QA
→ 独立审核
→ 确定性评分
→ 最多三轮修订
→ 状态机
→ 证据链
→ 七文件交付
→ 发布审计
```

私有验证仓库固定承载：

- 单元测试、集成测试和 PowerPoint 实机回归。
- 真实图片 fixture、受控 Agent 响应和视觉基线。
- Review Evaluation、warning candidate 和 acceptance 测试。
- README 示例发布门禁。
- Profile 资格测试和发布证据。
- 历史契约迁移 fixture、故障注入和重复性报告。

私有验证系统可以调用公开 Skill 的脚本和契约，但公开 Skill 不应反向依赖私有验证仓库。

## 4. 文件归属原则

### 4.1 公共 Skill 必须保留

以下内容直接支持普通图片转 PPT，应保留在 `image-to-editable-ppt/`：

- `SKILL.md`、许可证和 UI 元数据。
- Planner、Reviewer 配置及其 Prompt。
- 用户转换所需的 Agent 调用包和响应校验。
- layout、crop、asset、build、render、QA 和快速 review 契约。
- 元素分类、资产边界、PPT 构建和结构 QA 规则。
- 裁切、背景处理、SVG 清洗和资产验证。
- PptxGenJS 构建器。
- 字体审计、PowerPoint 渲染和结构验证。
- 最多一次 Review Patch。
- 用户转换入口和精简转换摘要。

### 4.2 应迁往私有验证仓库

以下内容不属于普通用户运行时：

- Review Evaluation 和综合评分。
- recommendation relation 和评分锚点执行器。
- 完整多轮发布状态机。
- warning candidate、用户 acceptance 和 delivery decision。
- 七文件发布打包。
- README 示例发布验证。
- Profile certification、资格批次和 release evidence。
- 历史契约迁移测试、故障注入工具和发布审计。
- 测试图片、运行日志、Agent 私有调用记录和测试产物。

### 4.3 暂时保留、完成迁移后再判断

对于同时被用户流程和开发流程调用的文件，不应立即移动或删除。先通过调用关系确认其真实用途，再选择：

- 保留为公共核心模块；
- 拆分为公共核心与私有扩展；
- 由私有验证仓库包装公共接口；
- 在没有任何调用方后删除。

## 5. 实施阶段

### 阶段 A：冻结基线

1. 记录当前分支、Git HEAD 和 Skill 文件树哈希。
2. 不再添加新工作流、状态或 Schema。
3. 保存当前 quick validation、Schema 和 PowerPoint 实机检查结果。
4. 保留现有未跟踪文档、日志和宣传项目，不将其纳入本轮迁移。

完成条件：形成可复核的迁移前基线，后续每个阶段均可回滚。

### 阶段 B：建立准确清单

为每个 tracked 文件记录：

- 当前职责；
- 实际调用方；
- 是否属于用户运行时；
- 是否属于开发验证；
- 是否被两者共享；
- 建议动作：保留、拆分、迁移或删除；
- 迁移后的目标位置。

不得仅根据文件名判断用途，必须使用静态调用搜索和代表性运行验证。

完成条件：所有公共 Skill 文件均有唯一明确的归属结论。

### 阶段 C：迁移开发验证能力

1. 先将开发专用脚本、Schema 和 reference 复制到私有验证仓库的独立分支。
2. 修改私有验证入口，使其通过稳定接口调用公共 Skill。
3. 运行现有开发回归和发布门禁。
4. 私有仓库验证通过前，不删除公共仓库中的来源文件。
5. 迁移提交和公共删除提交分开，确保可独立回滚。

完成条件：开发验证系统不再依赖公共 Skill 内的发布专用实现文件。

### 阶段 D：收缩公共 Skill 文档

重写 `SKILL.md`，只保留：

- 触发范围；
- 用户转换流程；
- Planner/Reviewer 职责；
- 一次修订上限；
- 用户交付内容；
- 必要 reference 路由。

将现有编排文档拆为：

- 公共 Skill 中的 `user-orchestration.md`，只描述用户转换。
- 私有验证仓库中的开发验证编排文档，描述评分、状态机、证据和发布。

完成条件：阅读 `SKILL.md` 和用户编排 reference 即可理解普通转换，不需要阅读发布工程文档。

### 阶段 E：建立清晰用户入口

新增一个唯一的用户转换入口，负责：

1. 输入预检和 request 规范化。
2. 创建 Planner checkpoint。
3. 提交 Planner 规格并运行确定性构建链。
4. 创建独立 Reviewer checkpoint。
5. 根据 Reviewer 结果直接交付或执行一次定向修订。
6. 原子生成 PPTX、预览图和转换摘要。

底层脚本继续保持单一职责，但普通使用说明不再要求用户逐个调用底层命令。

完成条件：用户上传图片并提出转换要求后，不需要理解 iteration、evaluation、delivery decision 或 evidence。

### 阶段 F：双页面验证

选择两张图片：

- 简单页面：以原生文字、形状和连接线为主。
- 复杂页面：包含裁图、复杂连接关系或密集布局。

两页均验证：

- Planner 和 Reviewer 均真实执行。
- 两者 context ID 不同。
- 文字和结构保持可编辑。
- PowerPoint 实机渲染成功。
- 结构 QA 通过。
- Reviewer 无未处理的 critical/major。
- 最多一次修订后结束。
- 用户交付只包含 PPTX、预览图和转换摘要。

完成条件：简单页和复杂页均能通过统一用户入口完成交付。

### 阶段 G：删除无调用方文件

只有同时满足以下条件才能删除公共文件：

1. 已成功迁移到私有验证仓库，或确认不再需要。
2. 公共用户流程没有直接或间接调用。
3. 私有开发回归通过。
4. 公共双页面验证通过。
5. 全仓静态搜索没有残留引用。
6. Skill quick validation 和安装审计通过。

完成条件：公共 Skill 中不存在只为测试、评分、资格或发布服务的文件。

## 6. 验收标准

架构收缩完成后必须满足：

- 公共 Skill 只有一个面向用户的主流程。
- Planner 和 Reviewer 职责清晰且保持上下文隔离。
- 普通转换不运行综合评分、发布状态机和证据链。
- 开发验证系统仍能独立执行完整严格流程。
- 公共 Skill 不依赖私有仓库，私有仓库可依赖公共 Skill。
- 公共文档不要求用户理解开发阶段编号或发布 Gate。
- 用户交付文件集合稳定、简单且可解释。
- 删除文件前后，简单页和复杂页转换结果保持有效。
- 仓库中不提交运行日志、临时目录、Agent 私有上下文或测试缓存。

## 7. 回滚原则

- 每个阶段使用独立 Git commit。
- 迁移与删除不得放在同一个 commit。
- 私有验证仓库先完成接收和回归，公共仓库后执行删除。
- 任一核心构建、Reviewer 或 PowerPoint 实机验证失败时，停止后续删除。
- 回滚只撤销当前阶段，不修改已冻结的历史 iteration 和测试证据。

## 8. 非目标

本轮不负责：

- 增加新的 PPT 元素类型。
- 扩展渐变、自由路径或复杂动画能力。
- 新增模型供应商或运行时 Adapter。
- 优化宣传网页或演示视频。
- 重做 README 视觉示例。
- 执行新的大规模真实模型资格测试。

这些工作必须在架构收缩完成后单独规划。

## 9. 最终形态

```text
公开仓库 / 可安装 Skill
└─ 快速、清晰、可编辑的图片转 PPT 产品能力

私有验证仓库
└─ 严格、可复现、可审计的开发与发布保障系统
```

架构判断标准不是“文件越少越好”，而是每个文件只服务于清晰、必要且可解释的职责。

## 附录 A：Git 分支与提交操作

本附录将架构阶段映射为可执行的 Git 操作。命令只作为实施规范；执行前必须确认工作区状态，不得使用 `git add .`、`git reset --hard` 或覆盖未跟踪文件的命令。

### A.1 当前基线

编写本文档时的公共仓库基线为：

```text
repository: E:\github_project\visual-to-editable-ppt-skill
branch: codex/fix-screenshot-reconstruction-quality
HEAD: 4a20a4f
main: 27b4c95
```

当前修复分支相对 `main` 包含：

```text
00a7ddb  fix: harden screenshot asset boundaries and review gate
ff6b818  fix: accept 1.4 agent role call records
4a20a4f  fix: harden editable reconstruction pipeline
```

以下未跟踪内容不得混入修复分支或架构代码提交：

```text
docs/PROJECT_CONTEXT_EXPORT_2026-07-30.md
docs/diagnostics/
pipeline.log
web-video-demo/
```

架构方案文档只允许在新的架构分支提交。

### A.2 合并当前修复分支

先检查待推送内容：

```powershell
Set-Location E:\github_project\visual-to-editable-ppt-skill
git status --short --branch
git log --oneline main..HEAD
git diff --stat main...HEAD
```

确认当前三个修复提交准确后推送分支：

```powershell
git push -u origin codex/fix-screenshot-reconstruction-quality
```

随后通过 GitHub Pull Request 将该分支合并到 `main`。架构收缩文档、诊断文档、宣传项目和日志不得进入该 Pull Request。

合并完成后更新本地 `main`：

```powershell
git switch main
git pull --ff-only origin main
```

若未跟踪文件与目标分支存在路径冲突，应先停止切换并单独处理对应文件；不得强制覆盖。

### A.3 创建收缩前标签

确认 `main` 已包含三个修复提交后创建带说明的基线标签：

```powershell
git tag -a pre-architecture-contraction-v1 -m "Baseline before installable skill architecture contraction"
git push origin pre-architecture-contraction-v1
```

标签必须指向合并后的稳定 `main`，不能指向尚未合并的功能分支。

验证标签：

```powershell
git show --stat pre-architecture-contraction-v1
```

### A.4 创建公共架构分支

从更新后的 `main` 创建：

```powershell
git switch -c codex/architecture-contraction-v1
```

只暂存架构方案文档：

```powershell
git add -- docs/architecture/image-to-editable-ppt-architecture-contraction-plan_v1.0.md
git diff --cached --name-only
git diff --cached --check
```

暂存集合必须只有该文档，然后提交：

```powershell
git commit -m "docs: define skill architecture contraction plan"
git push -u origin codex/architecture-contraction-v1
```

### A.5 公共架构分支的阶段提交

每个阶段使用独立提交，不允许把迁移、实现和删除合并为一个提交。

| 阶段 | 建议提交信息 | 允许内容 |
|---|---|---|
| A | `docs: define skill architecture contraction plan` | 架构方案 |
| B | `docs: classify runtime and validation responsibilities` | 文件职责清单和调用证据 |
| D | `docs: separate user and development orchestration` | `SKILL.md` 和编排 reference 拆分 |
| E | `feat: add simplified user conversion entry` | 用户入口和必要核心契约 |
| F | `fix: address user workflow validation findings` | 双页面验证确认的通用修复 |
| G1 | `cleanup: remove release scoring from installable skill` | 已迁移的评分能力 |
| G2 | `cleanup: remove release state and packaging runtime` | 已迁移的状态和发布打包能力 |
| G3 | `cleanup: remove obsolete schemas and references` | 已无调用方的契约和文档 |

每次提交前执行：

```powershell
git status --short
git diff --name-only
git diff --check
```

使用精确路径暂存：

```powershell
git add -- <本阶段允许的文件路径>
git diff --cached --name-only
git diff --cached --check
```

如果暂存集合出现当前阶段之外的文件，先使用以下命令取消对应暂存，不修改文件内容：

```powershell
git restore --staged -- <误暂存路径>
```

### A.6 创建私有验证仓库同步分支

私有验证仓库使用独立分支：

```powershell
Set-Location E:\github_project\visual-to-editable-ppt-skill-validation
git status --short --branch
git switch main
git pull --ff-only origin main
git switch -c codex/architecture-contraction-sync-v1
```

私有仓库阶段提交建议为：

```text
refactor: receive release validation runtime
refactor: bind validation harness to public skill interfaces
test: restore release scoring and state regressions
test: validate contracted user workflow
```

迁移顺序固定为：

```text
私有仓库复制开发能力
→ 调整调用路径
→ 私有测试通过
→ 提交私有迁移
→ 公共仓库才允许删除来源文件
```

不得在私有迁移尚未提交或回归失败时执行公共阶段 G。

### A.7 跨仓库阶段 Gate

公共删除提交之前，应记录以下对应关系：

```text
公共待删除文件
→ 私有目标文件
→ 私有迁移 commit SHA
→ 私有回归命令
→ 私有回归结果
```

文件职责清单应增加 `private_migration_commit` 字段。该字段为空时，文件不能从公共 Skill 删除。

阶段 G 开始前必须同时满足：

- 私有同步分支已推送；
- 私有迁移 commit SHA 已记录；
- 私有评分、状态机、打包和发布门禁回归通过；
- 公共用户入口已通过简单页和复杂页验证；
- 全仓搜索确认公共用户入口没有调用待删除文件。

### A.8 Pull Request 策略

推荐只创建两个主要 Pull Request：

1. 当前截图质量修复 PR：`codex/fix-screenshot-reconstruction-quality → main`。
2. 架构收缩 PR：`codex/architecture-contraction-v1 → main`。

架构收缩 PR 可以持续包含多个阶段提交，但在阶段 G 完成和双页面回归通过前保持 Draft。

私有验证仓库使用独立 PR：

```text
codex/architecture-contraction-sync-v1 → main
```

合并顺序固定为：

```text
当前公共修复 PR
→ 私有验证迁移 PR
→ 公共架构收缩 PR
```

### A.9 回滚操作

优先使用 `git revert` 创建可审计的反向提交，不改写共享分支历史。

回滚单个阶段：

```powershell
git revert <stage-commit-sha>
```

如果公共删除导致回归：

1. 只回滚对应的公共 cleanup commit。
2. 保留私有仓库已经完成的迁移。
3. 修复公共调用边界后重新执行阶段 G。

如果整个架构方向需要暂停，使用标签恢复对比和重建分支：

```powershell
git switch main
git switch -c codex/architecture-contraction-recovery pre-architecture-contraction-v1
```

不得对 `main` 或已推送的协作分支执行 `git reset --hard`。

### A.10 合并前最终检查

公共架构收缩 PR 合并前执行：

```powershell
git status --short
git diff --check main...HEAD
git log --oneline --decorate main..HEAD
```

并确认：

- Skill quick validation 通过；
- 公共 Schema 自检通过；
- Python 和 Node 运行依赖通过；
- PowerPoint 实机构建、渲染和结构 QA 通过；
- Planner 和 Reviewer 在用户流程中均执行且上下文不同；
- 简单页和复杂页均完成交付；
- 私有开发验证回归通过；
- 公共仓库没有日志、缓存、测试工作目录或 Agent 私有上下文；
- PR 文件列表与本方案的公共职责范围一致。
