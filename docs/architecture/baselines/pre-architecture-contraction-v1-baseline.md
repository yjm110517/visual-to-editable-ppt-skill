# 架构收缩前运行基线

## 1. 基线身份

本报告冻结架构收缩前的可安装 Skill 状态。采集时间为
`2026-08-07T09:09:37Z`，基线对象是合并后的 `main`，而不是合并前的功能分支。

| 项目 | 值 |
|---|---|
| Pull Request | [#1](https://github.com/yjm110517/visual-to-editable-ppt-skill/pull/1) |
| PR merge commit | `dc2b9de838123c9f0cce845ca98e8463b05c0174` |
| 基线标签 | `pre-architecture-contraction-v1` |
| 标签目标 | `dc2b9de838123c9f0cce845ca98e8463b05c0174` |
| Skill Git tree ID | `2dfdf3ab50470cb9a455a59da27eb32c5a1c0b9c` |
| Skill tree manifest SHA-256 | `94701309c56f27dc1acb00bbedbb2aa59f2942e25d2ef7775b253211d4ad5923` |
| Skill 跟踪文件数 | `64` |
| 架构分支 | `codex/architecture-contraction-v1` |
| 架构方案提交 | `80f906a427b7cbb916ce3d4a5465d3f6423c6524` |

Skill tree manifest SHA-256 的计算口径是对以下命令的原始 stdout 字节计算 SHA-256：

```powershell
git ls-tree -r --full-tree main image-to-editable-ppt
```

## 2. 已合并的修复提交

以下三个提交均可从合并后的 `main` 到达，并通过 merge commit 保留：

1. `00a7ddb` — `fix: harden screenshot asset boundaries and review gate`
2. `ff6b818` — `fix: accept 1.4 agent role call records`
3. `4a20a4f` — `fix: harden editable reconstruction pipeline`

## 3. Skill 与 Schema 验证

| 检查 | 结果 | 说明 |
|---|---|---|
| Skill quick validation | `PASS` | 输出 `Skill is valid!` |
| 顶层 Schema 数量 | `18` | 只统计 `schemas/*.schema.json` |
| Draft 2020-12 Schema 自检 | `PASS` | 全部通过 `Draft202012Validator.check_schema()` |
| PptxGenJS 导入 | `PASS` | 使用 Codex bundled Node 从 `scripts/` 导入 |

Quick validation 使用系统 Python 3.12.6 及其已安装的 PyYAML。Codex bundled Python
3.12.13 在未注入额外依赖路径时不包含 PyYAML，因此不作为 quick validation 的直接入口；
它仍是本基线记录的文档与 PPT 运行时。

## 4. 运行时与工具链

| 组件 | 实际版本 | 基线判断 |
|---|---:|---|
| Codex bundled Python | `3.12.13` | 有效开发运行时 |
| 系统 Python | `3.12.6` | 用于 quick validation 与 Schema 自检 |
| Codex bundled Node.js | `24.14.0` | 有效 Node 运行时 |
| 系统 Node.js | `16.20.2` | 低于项目要求，不作为有效基线运行时 |
| Codex bundled pnpm | `11.16.0` | 可用，但与项目声明存在漂移 |
| 项目声明 pnpm | `11.9.0` | `package.json` 冻结值 |

pnpm `11.16.0` 与项目声明的 `11.9.0` 不一致。本阶段只记录该工具链漂移，
不修改依赖文件，也不阻塞架构基线冻结。

## 5. PowerPoint 实机验证

| 检查 | 结果 |
|---|---|
| PowerPoint COM 版本 | `16.0` |
| 可执行文件 | `C:\Program Files\Microsoft Office\Root\Office16\POWERPNT.EXE` |
| 文件版本 | `16.0.20228.20124` |
| 独立 COM 实例创建 | `PASS` |
| 新建单页演示文稿 | `PASS` |
| 临时 PPTX 保存 | `PASS`，`32,778` 字节 |
| 演示文稿关闭与 COM 退出 | `PASS` |

验证文件位于自动清理的临时目录，未进入仓库或工作目录。

## 6. 排除范围与工作区状态

阶段 A 明确排除并保持未跟踪的内容：

```text
docs/PROJECT_CONTEXT_EXPORT_2026-07-30.md
docs/diagnostics/
pipeline.log
web-video-demo/
```

这些内容未进入修复 PR、标签、架构方案提交或本基线报告提交。阶段 A 不删除、
移动或修改这些文件。

## 7. 阶段 A 边界声明

阶段 A 只完成以下操作：

- 合并并标记已完成的截图重建修复；
- 创建架构收缩分支；
- 提交架构收缩方案；
- 记录可复现的运行基线。

架构分支相对 `main` 只增加架构方案和本基线报告。阶段 A 未修改
`image-to-editable-ppt/` 下的运行代码、Agent Prompt、Schema、references 或工作流。
功能与职责迁移从后续阶段开始。

## 8. 已知风险

1. 系统 Node.js 16.20.2 不满足项目 Node 基线，必须继续使用 Codex bundled Node 24.14.0。
2. bundled pnpm 11.16.0 与项目声明 11.9.0 不一致，后续应单独统一依赖工具链。
3. bundled Python 直接运行 quick validator 时缺少 PyYAML；当前验证由系统 Python 完成。

以上风险均已显式记录，不影响阶段 A Gate。
