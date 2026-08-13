# ADR-0010：Host Agent 无关会话（解除 Codex 硬编码绑定）

> 状态：已接受（Architect 确认接受，2026-08-13）
> 日期：2026-08-13
> 权威范围：Agent 运行时标识、数据目录平台感知、启动器 uv 回退、宿主兼容性探针准入矩阵
> 上游：[产品需求](../../10-product/product-requirements.md)（FR-17、NFR-10）、[跨平台多 Agent 适配计划](../../40-delivery/cross-platform-multi-agent-plan.md) §5
> 下游：[系统设计](../../20-architecture/system-design.md)、[证据模型](../../20-architecture/evidence-model.md)、[扫描与分析设计](../../20-architecture/scanning-and-analysis.md)、[验收基线](../../40-delivery/acceptance-baseline.md)
> 关系：部分替代 [ADR-0001](ADR-0001-skill-and-state-isolation.md) 的 Codex-only 产品入口与数据目录绑定；部分替代 [ADR-0003](ADR-0003-evidence-pointers-without-source-snapshots.md) 的 Codex 深读策略措辞；部分替代 [ADR-0006](ADR-0006-authorized-codex-analysis-and-external-git-metadata.md) 的 Codex 专属 SessionCapability 与 issuer_kind 绑定；部分替代 [ADR-0007](ADR-0007-review-state-lineage-and-snapshot-integrity.md) 的 Codex task awaiting-context 条款。旧 ADR 正文保留为历史决策记录，不被改写。

## 背景

ADR-0001/0003/0006/0007 在 2026-07-24 制定时，GoodJob 仅以 Codex 为唯一支持宿主。这导致四处硬编码耦合：DB schema 的 `issuer_kind` CHECK 约束、`auth.py` 的 INSERT 字面量、`paths.py` 的 `~/.codex/` 默认数据目录、SKILL.md 的 Codex 措辞和启动指令。

跨平台多 Agent 适配计划（§5）要求在保持 Codex 向后兼容的前提下，让 ZCode/ClaudeCode/OpenCode/MimoCode 逐个通过兼容性探针后进入支持矩阵。本 ADR 正式建立 Agent 无关的会话契约。

## 决策

### 1. DB 迁移 v11：解除 issuer_kind CHECK 约束

移除 `authorization_receipts.issuer_kind` 的 `CHECK (issuer_kind = 'codex_task_runtime')` 约束。使用 11 步外键安全表重建流程（VACUUM INTO 备份 -> 事务外 FK OFF -> CREATE/INSERT/DROP/RENAME -> 重建索引 -> foreign_key_check 非空即 ROLLBACK -> COMMIT -> 事务外恢复 FK ON），因为 `authorization_receipts` 被 `scan_runs`、`worktree_observations`、`preparation_runs` 外键引用。任一步失败时回滚当前事务，并从 `VACUUM INTO` 快照恢复数据库后再报错。

应用层（`auth.py`）负责校验合法值。现有数据 `issuer_kind='codex_task_runtime'` 不变（向后兼容）。

**被替代条款：** ADR-0006 决策 2 中 `issuer_kind` 绑定 `codex_task_runtime` 的隐含约束。新契约：`issuer_kind` 是自由文本，由 `--agent-runtime` 参数传入，默认 `codex_task_runtime` 保持兼容。

### 2. 参数化 Codex 硬编码值

| 位置 | 旧值 | 新契约 |
|------|------|--------|
| `auth.py` `issue()` | `'codex_task_runtime'` 字面量 | 接受 `issuer_kind` 参数，默认 `codex_task_runtime` |
| `paths.py` | `~/.codex/goodjob-career-review` | 平台感知：legacy 目录存在则沿用；否则 Linux `~/.local/share/goodjob-career-review`，Windows `%LOCALAPPDATA%/goodjob-career-review`，macOS 保持 `~/.codex/` |
| `cli.py` | help 文本 `~/.codex/...` | 平台感知描述 |
| `session.py` | 无 agent 标识 | 接收 `--agent-runtime`，传给子进程 |
| `pyproject.toml` | `"GoodJob Codex Skill"` | `"GoodJob Career Review Skill"` |

**被替代条款：** ADR-0001 决策 5 中数据目录默认 `~/.codex/goodjob-career-review` 的平台限定。新契约：数据目录平台感知，legacy 路径向后兼容。

### 3. 启动器脚本（uv + python 回退）

新增 `runtime/scripts/launch_broker.py`，宿主以 `python3 -I -B <runtime_dir>/scripts/launch_broker.py --agent-runtime <runtime>` 启动，使 launcher 自身在建立子进程命令前也不加载 `PYTHON*` 环境、用户 site 或写入 bytecode。launcher 检测 `uv` 是否在 PATH 上；有 uv 则用完整隔离模式（`--isolated --no-project --no-config --offline --no-python-downloads --python 3.12`）启动 `session.py`；无 uv 则查找 `python3.12`（或 `python3` >= 3.12）直接启动。

**被替代条款：** SKILL.md 中 `uv run` 为唯一启动方式的隐含约束。新契约：uv 为推荐启动器，python3.12 回退为等价替代。

### 4. SKILL.md 通用化

- "Codex" -> "host agent"（产品入口、会话边界、模型处理链路描述）
- 启动指令改为 `python3 -I -B <runtime_dir>/scripts/launch_broker.py --agent-runtime <runtime>`
- `generator_id` 示例从 `"codex"` 改为 `"<agent-runtime>"`
- JSONL 协议本身不变（已 agent 无关）

**被替代条款：** ADR-0001 中 "Codex Skill 为入口"的产品形态描述、ADR-0003 中 "Codex 深读策略"的措辞、ADR-0006 中 "Codex 打开的原文件进入模型处理链路"的会话边界描述。新契约：产品入口为 host agent Skill；深读和会话边界描述使用 "host agent" 措辞。

### 5. 宿主兼容性探针准入矩阵

每个宿主 agent 必须通过以下 5 项探针后才能以"已验证"身份进入支持矩阵：

1. **发现路径**：清单文件格式与位置可被宿主发现
2. **长驻 stdin / 进程句柄保持**：宿主能保持 broker 进程的 stdin 在整个 task 期间不关闭
3. **交互确认（JSONL 往返）**：宿主能正确发送 JSONL 请求并解析 JSONL 响应
4. **退出清理与失败关闭**：宿主在 task 结束时关闭 stdin，broker 正确退出并清理 capability
5. **真机 E2E**：在真实环境（非 mock/monkeypatch）中完成一次完整的授权 -> 扫描 -> 准备流程

当前支持矩阵状态：

| Agent | 状态 | 说明 |
|-------|------|------|
| Codex | 待回归 | 现有支持基线；须按同一 5 项探针回归，通过前不标"已验证" |
| ZCode | 待探针 | SKILL.md 通用化后需探针验证 |
| ClaudeCode | 待探针 | 未验证 |
| OpenCode | 待探针 | 未验证 |
| MimoCode | 待探针 | 未验证 |

未通过探针的宿主明确留在"待支持"状态，不得因为"薄清单即可发现"就默认视为已支持。

**被替代条款：** ADR-0007 决策 8 中 "持有原 SessionCapability 的同一 Codex task" 的 awaiting-context 条款。新契约：awaiting_context 绑定的是持有原 SessionCapability 的同一 host agent task，不限定具体 agent 产品。

## 不在范围

- 原生 Windows 平台支持（Phase 3）
- JSONL 协议本身修改（已 agent 无关）
- 重写现有 `issuer_kind='codex_task_runtime'` 历史数据
- CI/GitHub Actions 矩阵
