# GoodJob 发布验收证据账本

> 状态：按代码基线冻结的验收记录，不是产品契约
> 证据基线：`8f05c43aa7d757966781cb3b3a869ccad14fb43f`
> 记录日期：2026-08-05
> 适用范围：[验收基线 §3.2](acceptance-baseline.md#32-场景矩阵)与[看板呈现契约 §12](../20-architecture/dashboard-design.md#12-可判定验收规则)
> 上游：上述两份权威文档；本账本只记录该基线已有的可复现实证与缺口，不改变场景定义。

## 1. 判定口径

- `verified`：场景与通过条件的每个子句都有可区分正确/错误行为的适格证据。
- `partial`：至少一个子句已被适格证据证明，但仍有仓库内或外部缺口。
- `missing`：没有适格证据；表内 `evidence_class` 表示应补的最小证据类别。
- `owner_blocked`：仓库内证据已齐全，只差 Owner 授权、决策或人工视觉判断。本基线没有此状态；仍有仓库内缺口的条目均保守记为 `partial`。
- `automated` 指确定性单元/集成/静态门禁；`synthetic_e2e` 指临时工作区、真实子进程或真实浏览器形成的合成端到端证据；`real_workspace` 指经授权的真实工作区；`owner_visual` 指 Owner 人工视觉核对。`verified`、`partial`、`owner_blocked` 的 `evidence_class` 只列当前已取得的证据；`missing` 因没有现有证据，列完成该条所需的最小证据类别。尚未取得的类别只写在缺口列。
- 测试名称只用于定位。下表结论来自实际执行、打开断言并逐子句对照；夹具存在但没有相关行为断言不算证明。

## 2. 可复现证据入口

`P1` 至 `P4` 于 2026-08-02 在仓库根执行，覆盖下表引用的既有 Python 节点，成功集合分别为 51、50、61、20 条，共 182 条；`E1` 于 2026-08-05 在候选 `1ddaa28` 上复验；`W1`、`F1` 与 `R1` 于 2026-08-05 在候选 `8f05c43` 上由 Architect 独立复验。

| 代号 | 精确命令 | 本次结果 |
| --- | --- | --- |
| `P1` | `cd .agents/skills/goodjob-career-review/runtime && uv run pytest -q tests/test_scanner.py tests/test_history.py tests/test_adapters.py tests/test_safe_fs.py` | `51 passed` |
| `P2` | `cd .agents/skills/goodjob-career-review/runtime && uv run pytest -q tests/test_preparation.py tests/test_analysis.py` | `50 passed` |
| `P3` | `cd .agents/skills/goodjob-career-review/runtime && uv run pytest -q tests/test_reporting.py tests/test_exporting.py` | `61 passed` |
| `P4` | `cd .agents/skills/goodjob-career-review/runtime && uv run pytest -q tests/test_cli.py tests/test_auth.py tests/test_db.py tests/test_installation.py` | `20 passed` |
| `E1` | `cd .agents/skills/goodjob-career-review/runtime && uv run pytest -q tests/test_e2e_preparation.py` | `1 passed in 3.77s`；真实 JSONL broker、双项目主链与三个失败关闭请求 |
| `W1` | `cd .agents/skills/goodjob-career-review/runtime && uv run pytest -q tests/test_scanner.py::test_root_external_linked_worktree_requires_two_stage_authorization_and_never_reads_history tests/test_scanner.py::test_root_internal_linked_worktree_is_grouped_without_external_authorization tests/test_scanner.py::test_git_directory_with_external_commondir_uses_the_same_candidate_bound_protocol tests/test_scanner.py::test_same_content_worktrees_reuse_analysis_and_keep_expandable_sources tests/test_scanner.py::test_three_worktrees_preserve_branch_state_and_divergent_evidence tests/test_analysis.py::test_module_claim_requires_worktree_scope_without_equivalent_branch_coverage tests/test_reporting.py::test_carried_forward_evidence_keeps_snapshot_worktree_provenance` | `7 passed in 21.78s`；真实 Git 三工作树、作用域失败关闭与冻结 provenance |
| `F1` | `cd .agents/skills/goodjob-career-review/runtime/frontend && npm run verify` | Chromium + WebKit，`152 passed / 0 failed` |
| `R1` | `make gate-release` | format、lint、mypy 39 个源文件、184 pytest、前端门禁、20 文档测试、46 份 Markdown 链接、152/152 浏览器核对与 sdist/wheel 构建全部通过；运行 HEAD 为 `8f05c43` |

## 3. IMP 证据矩阵

| ID | status | evidence_class | 精确证据节点 | 已覆盖子句 | 未覆盖子句 / 最小后续归属 |
| --- | --- | --- | --- | --- | --- |
| IMP-01 | partial | automated, synthetic_e2e | `P2` · `tests/test_preparation.py::test_jd_file_and_level_override_are_frozen`、`::test_bad_jd_creates_no_job_lens_or_run`、`::test_explicit_continue_without_bad_jd_creates_a_visible_assumption_run`；`P4` · `tests/test_cli.py::test_job_input_preflight_blocks_bad_jd_before_scan_state` | 无 JD、文本/文件 JD、职级覆盖、缺失/目录/坏编码拒绝、继续假设及坏 JD 零业务写入 | 未证明显式 Skill 对话只追问缺失项；补 Skill 入口合成会话验收 |
| IMP-02 | partial | automated, synthetic_e2e | `P1` · `tests/test_scanner.py::test_scan_discovers_isolated_projects_and_keeps_sensitive_bytes_out_of_sqlite`；`P2` · `tests/test_preparation.py::test_normally_failed_empty_scan_creates_a_failed_preparation_without_bundle` | Git/嵌套 Git/manifest 项目发现及空工作区失败、不产准备包 | 未用单一夹具覆盖 `.git` 指针混合发现、重复扫描 identity 稳定和完整下一步提示；补扫描器合成 E2E |
| IMP-03 | partial | automated, synthetic_e2e | `W1` · `tests/test_scanner.py::test_root_external_linked_worktree_requires_two_stage_authorization_and_never_reads_history`、`::test_root_internal_linked_worktree_is_grouped_without_external_authorization`、`::test_git_directory_with_external_commondir_uses_the_same_candidate_bound_protocol`、`::test_same_content_worktrees_reuse_analysis_and_keep_expandable_sources`、`::test_three_worktrees_preserve_branch_state_and_divergent_evidence`；`tests/test_analysis.py::test_module_claim_requires_worktree_scope_without_equivalent_branch_coverage`；`tests/test_reporting.py::test_carried_forward_evidence_keeps_snapshot_worktree_provenance` | 根内工作树归并；根外 file/directory marker 两阶段授权与禁读历史；三工作树 branch/HEAD/dirty；相同内容单次分析且保留全部来源；分支独有 Evidence 隔离；module/project 提升与精确 worktree scope 失败关闭；冻结 branch/HEAD/dirty/scan-run provenance | 未穷举 `IMP-03` 要求的全部非法 external config/candidate 组合；补根外 Git 非法配置专项矩阵 |
| IMP-04 | verified | automated, synthetic_e2e | `P1` · `tests/test_scanner.py::test_scan_discovers_isolated_projects_and_keeps_sensitive_bytes_out_of_sqlite`、`::test_unsupported_ignore_patterns_report_source_raw_line_and_approximation`、`::test_relative_project_exclusion_precedes_snapshot_and_stays_distinct_from_failure` | 父 ignore 不吞嵌套 Git、内层应用自身 ignore；不支持模式显式给出原行/近似语义；Owner 排除与失败分离并进入覆盖摘要 | 无（本基线） |
| IMP-05 | partial | automated, synthetic_e2e | `P1` · `tests/test_scanner.py::test_scan_discovers_isolated_projects_and_keeps_sensitive_bytes_out_of_sqlite`、`::test_descriptor_reader_rejects_a_file_or_directory_symlink`；`P2` · `tests/test_preparation.py::test_bad_jd_creates_no_job_lens_or_run` | 根外 symlink 不跟随，秘密/依赖/生成目录及坏 JD 排除，敏感字节不落库 | 未证明 symlink 环终止、目录别名实路径去重和显式根外 JD 仅输入的组合场景；补路径安全 E2E |
| IMP-06 | partial | automated, synthetic_e2e | `P1` · `tests/test_scanner.py::test_refresh_fast_rebuilds_evidence_when_an_untracked_file_becomes_committed` | untracked 到 committed 转换会重建证据并更新 `commit_state` | 未同时断言 committed/modified/untracked 的代码与文档都可入证据；补当前状态矩阵测试 |
| IMP-07 | partial | automated, synthetic_e2e | `P2` · `tests/test_analysis.py::test_invalid_facets_reject_the_entire_batch_without_commit_checks` | 计划冒充 implemented、测试定义冒充 test_verified 均整批拒绝且零半提交 | 未证明匹配 revision 的通过结果可接受为 test_verified；补正向执行结果夹具 |
| IMP-08 | partial | automated, synthetic_e2e | `P1` · `tests/test_scanner.py::test_refresh_fast_reuses_metadata_but_verify_content_detects_same_stat_change`、`::test_verify_refresh_links_a_same_content_move_without_rewriting_history`、`::test_fast_refresh_reanalyzes_when_adapter_version_changes` | fast/verify_content 区分同 stat 篡改、同 hash 移动复用、adapter 版本变化重分析 | 未完整覆盖删除、普通改单文件、旧 locator 失效及“不重新深读全仓”的行为断言；补增量矩阵与读取计数 |
| IMP-09 | partial | automated, synthetic_e2e | `P1` · `tests/test_scanner.py::test_internal_git_history_uses_remote_head_and_persists_bounded_commit_evidence`、`::test_git_history_falls_back_to_main_master_or_head_only_and_handles_detached_head`、`::test_git_history_keeps_an_old_head_as_the_current_worktree_anchor`、`::test_git_directory_with_external_commondir_uses_the_same_candidate_bound_protocol`、`::test_internal_git_config_cannot_read_an_include_outside_the_authorized_workspace`、`::test_root_external_linked_worktree_requires_two_stage_authorization_and_never_reads_history`；`tests/test_history.py::test_targeted_history_is_bounded_transient_and_session_scoped` | remote HEAD/main/master/HEAD-only/detached 选择、180 天与旧 HEAD 锚点、根内有界临时深读不落正文、根外禁读历史和外部 config 隔离 | 未以命令拦截器直接区分“没有 fetch/checkout”，且全部 candidate 伪造与回退未在一个原子批次断言；补 Git argv 负向审计与复合 E2E |
| IMP-10 | partial | automated | `P1` · `tests/test_adapters.py::test_first_release_adapters_emit_structural_language_evidence`、`::test_manifest_declaration_does_not_masquerade_as_actual_usage` | TS/TSX、Python、Rust、Dart、SQL 产生结构化语言证据，声明不冒充使用 | 未知语言的结构/依赖/文档档案没有专门断言；补 unknown adapter 场景 |
| IMP-11 | partial | automated, synthetic_e2e | `P2` · `tests/test_preparation.py::test_prepare_start_freezes_dynamic_lenses_and_is_idempotent`、`::test_jd_file_and_level_override_are_frozen`、`::test_invalid_weight_sum_is_rejected_before_business_writes`、`::test_fixed_point_scoring_recomputes_boundaries_and_stable_ties` | 同一事实以两岗位生成差异 Lens、有/无 JD 与职级、9999/10001 拒绝、整数边界及稳定并列 | 未明确覆盖“未内置岗位”与两岗位差异解释输出；补动态岗位与说明断言 |
| IMP-12 | partial | automated, synthetic_e2e | `P2` · `tests/test_preparation.py::test_preflight_mismatch_terminates_without_an_evidence_bundle`、`::test_before_read_check_enforces_session_binding_and_detects_drift`；`tests/test_analysis.py::test_commit_source_drift_requires_refresh_and_leaves_zero_analysis_entities`、`::test_deep_read_evidence_requires_before_read_and_persists_only_pointer_data` | preflight/before_read/commit 三阶段失效、refresh_required、零分析半提交及深读正文不持久化 | 未证明 Agent 总是先读 EvidenceBundle、权限收回/删除全变体、旧 latest 保持和大工作区不读无关项目；补会话级读取审计 |
| IMP-13 | partial | automated, synthetic_e2e | `E1` · `tests/test_e2e_preparation.py::test_synthetic_workspace_full_chain_freezes_traceable_role_package`；`P1` · `tests/test_scanner.py::test_project_failure_carries_forward_its_baseline_and_keeps_other_projects_fresh`、`::test_relative_project_exclusion_precedes_snapshot_and_stays_distinct_from_failure`；`P3` · `tests/test_reporting.py::test_carried_forward_evidence_keeps_snapshot_worktree_provenance` | fresh/carried_forward/failed/excluded 生产与沿用来源可区分，空合资格失败已有断言；同一运行的两个 fresh 项目均有 Assessment，`860/260` 分且 rank 连续，低分项目仍进入报告 | 未在同一 PreparationRun 同场证明 carried-forward、failed、excluded 与证据不足项目只按资格进入 Assessment/Coverage；补完整资格矩阵 E2E |
| IMP-14 | partial | automated, synthetic_e2e | `E1` · `tests/test_e2e_preparation.py::test_synthetic_workspace_full_chain_freezes_traceable_role_package`；`P2` · `tests/test_analysis.py::test_personal_implementation_requires_and_accepts_bound_role_context`、`::test_skipped_context_requires_a_visible_open_project_gap`；`P3` · `tests/test_reporting.py::test_report_bundle_and_snapshot_are_deterministic_safe_and_idempotent` | 同一准备包可见客观“如何实现”、个人实现、主导、学习和结果叙事并绑定同项目证据；缺 role 的实现归因整批拒绝且零可渲染半提交 | 未以团队模块且缺个人上下文的同一夹具穷举过去式学习、负责、主导与取得指标的拒绝矩阵及客观结果降级；补叙事拒绝矩阵 E2E |
| IMP-15 | verified | automated, synthetic_e2e | `E1` · `tests/test_e2e_preparation.py::test_synthetic_workspace_full_chain_freezes_traceable_role_package`；`P2` · `tests/test_analysis.py::test_context_interview_appends_facts_and_freezes_them_for_later_runs` | 两个项目由一次双卡 request 批量提问业务目标、角色、学习、结果与取舍，由一次 answer batch 回答；十条事实按项目独立持久化、分页取齐，并在新 task 的后续 PreparationRun 逐项冻结复用 | 无（本基线） |
| IMP-16 | partial | automated, synthetic_e2e | `E1` · `tests/test_e2e_preparation.py::test_synthetic_workspace_full_chain_freezes_traceable_role_package`；`P3` · `tests/test_exporting.py::test_translation_prepare_reads_one_frozen_projection_without_writing_export_state`、`::test_translation_publish_atomically_creates_one_immutable_derived_export`、`::test_translation_publish_rejects_non_equivalent_candidate_batches_without_writes`、`::test_normal_export_failure_is_diagnostic_and_leaves_no_visible_partial_output` | 中文四件套与 latest 全字节冻结；英文 prepare 零文件，source/target 与数字/单位/技术锚点等价校验，漏项失败零目录树变化，成功原子发布且中文源不变 | 未证明中文工作稿默认不覆盖以及工作稿后再次 prepare 的完整链；补 drafts 生命周期 E2E |
| IMP-17 | partial | automated, synthetic_e2e | `P3` · `tests/test_reporting.py::test_review_lineage_projects_only_equivalent_subjects_into_new_snapshots`、`::test_render_failure_preserves_latest_and_retries_the_same_bundle`、`::test_committed_snapshot_repairs_latest_without_duplicate_render`、`::test_dead_render_owner_is_interrupted_and_only_registered_paths_are_cleaned` | 多运行生成不同快照且旧 HTML/ReportBundle hash 不变；普通失败保留 latest、进入 render_failed 并以同 bundle 重试；提交后 latest 可修复且不重复创建快照；死亡 owner 的 attempt 可标 interrupted 并只清登记路径 | 未直接按字节证明旧 Markdown、简历与 manifest 等完整快照均不覆盖；中断路径未断言 PreparationRun 先进入 render_failed 再重试，不能由最终 attempt 状态推导；补完整快照不可变与中断状态转换断言 |
| IMP-18 | partial | automated, synthetic_e2e | `P3` · `tests/test_reporting.py::test_report_bundle_and_snapshot_are_deterministic_safe_and_idempotent`；`F1` · `clean-external-requests`、全视图 `no-horizontal-overflow` | 单文件 HTML 内联生成、CSP 与零外部请求；file URL 下九视图可加载 | 未执行断网双击、把 HTML 单独复制到其他目录后复测，以及全部筛选/展开/缺口/复习交互；补可搬移离线 E2E |
| IMP-19 | verified | automated, synthetic_e2e | `E1` · `tests/test_e2e_preparation.py::test_synthetic_workspace_full_chain_freezes_traceable_role_package`；`P3` · `tests/test_reporting.py::test_review_lineage_projects_only_equivalent_subjects_into_new_snapshots`、`::test_review_sequence_breaks_equal_timestamp_ties_and_freezes_run_cutoff` | ReviewTargetBinding、掌握度、薄弱点、摘要和日期持久化；非法 transcript 拒绝且 mastery 零写入；同 ScanRun 新建运行并复用上下文，旧报告/简历 Markdown、HTML、manifest 与 latest 全字节不变 | 无（本基线） |
| IMP-20 | partial | automated, synthetic_e2e | `P1` · `tests/test_scanner.py::test_adapter_failures_and_sql_plan_boundaries_are_visible`、`::test_project_failure_carries_forward_its_baseline_and_keeps_other_projects_fresh`、`::test_all_projects_failing_without_baseline_remains_failed` | 单项目/adapter 失败不吞其余项目，失败原因及沿用基线可见 | 未以无权限、损坏仓库、未知语言三者同场证明原因/影响/补救均在最终包可见；补部分失败复合 E2E |
| IMP-21 | partial | automated, synthetic_e2e | `P4` · `tests/test_db.py::test_migration_creates_stable_owner_layout`；`tests/test_installation.py::test_isolated_installed_copy_does_not_create_a_skill_venv` | 个人数据根与 Skill 目录分离，隔离副本启动不会写 Skill venv，仓库内无个人数据库 | 未实际删除/升级 Skill 后重跑并比对个人数据完整性；补隔离安装升级测试 |
| IMP-22 | partial | automated, synthetic_e2e | `E1` · `tests/test_e2e_preparation.py::test_synthetic_workspace_full_chain_freezes_traceable_role_package`；`P4` · `tests/test_cli.py::test_protected_children_ignore_a_workspace_goodjob_module`；`P2` · `tests/test_analysis.py::test_non_personal_claim_rejects_personal_attribution_across_code_seam`；`P3` · `tests/test_reporting.py::test_dashboard_renders_markup_like_code_tokens_as_inert_data`；`F1` · `clean-external-requests`、`csp-style-positive-control` | manifest/JD/回答中的提示注入、标记、外部 URL 与 shell 载荷沿 broker 主链保持数据态，marker 未执行；源码全文与 JD 未进入禁止产物，HTML 无活动标记/远端资源；另有恶意模块隔离、ClaimDraft 校验、CSP 与零请求证据 | 未以同一完整语料集覆盖项目路径、文档和 ClaimDraft，也未由真实 Agent 证明不服从仓库/JD/回答指令；补 Agent 会话级注入与浏览器语料 E2E |
| IMP-23 | partial | automated, synthetic_e2e | `E1` · `tests/test_e2e_preparation.py::test_synthetic_workspace_full_chain_freezes_traceable_role_package`；`P4` · `tests/test_cli.py::test_session_broker_reuses_one_fd_capability_until_stdin_closes`、`::test_preparation_protocol_does_not_echo_private_payload_and_is_task_bound`；`tests/test_auth.py::test_receipt_validates_only_for_same_capability_scope_and_notice`、`::test_database_contains_digest_not_raw_capability`；`P1` · `tests/test_scanner.py::test_root_external_linked_worktree_requires_two_stage_authorization_and_never_reads_history` | 同 broker capability 贯通完整准备链且响应无 capability 字段；新 broker 拒绝旧 receipt 与旧 analysis task binding；已有 scope/notice/能力错误、DB 仅 digest 及根外两阶段授权证据 | 未在同一新任务复制 SQLite/receipt 场景扫描未知 capability 原值的 argv/env/stdout/stderr/日志/全部产物，也未形成完整零读取断言；补能力泄漏面与跨任务复合 E2E |
| IMP-24 | partial | automated, synthetic_e2e | `P4` · `tests/test_db.py::test_writer_lock_never_steals_an_active_lock`、`::test_writer_busy_performs_no_personal_data_initialization`；`P1` · `tests/test_scanner.py::test_refresh_marks_confirmed_dead_run_interrupted_and_never_uses_it_as_fast_baseline`；`P3` · `tests/test_reporting.py::test_render_writer_busy_creates_no_attempt_or_artifact` | writer_busy 零初始化/零 attempt、不偷活锁；确认死亡的 scan/render owner 可中断恢复 | 未覆盖 PID 复用/旧锁全矩阵，以及 awaiting_context 同任务续接、新任务接管拒绝、Owner 明示 restart 全链；补生命周期进程 E2E |
| IMP-25 | partial | automated, synthetic_e2e | `P2` · `tests/test_analysis.py::test_verified_review_semantics_survive_wording_only_claim_revision`、`::test_verified_review_semantics_survive_equivalent_source_path_move`；`P3` · `tests/test_reporting.py::test_review_lineage_projects_only_equivalent_subjects_into_new_snapshots` | 措辞变化与等价路径移动延续；语义键/缺口严重度变化触发重评；不直接依赖 Revision/Gap ID | 未穷举顺序/行号/facet/conflict/validity/角色结果锚点/gap 状态及跨项目相似题面不合并；补 canonical projection 参数矩阵 |
| IMP-26 | partial | automated, synthetic_e2e | `E1` · `tests/test_e2e_preparation.py::test_synthetic_workspace_full_chain_freezes_traceable_role_package`；`P4` · `tests/test_db.py::test_migration_creates_stable_owner_layout`、`::test_migration_upgrades_populated_v5_without_losing_preparation_evidence`；`P3` · `tests/test_exporting.py::test_translation_publish_atomically_creates_one_immutable_derived_export` | 两次 PreparationRun、两个中文快照与英文导出后，独立进程 `data-status` 显示非零 SQLite/artifacts/exports、drafts 键和快照计数，旧快照保持；已有布局与 migration 保留证据 | 未加入工作稿、升级/重装 Skill 后的完整状态比对，也未断言每次 scan/prepare 响应都显示 usage；补隔离升级、工作稿保留与逐次 usage E2E |
| IMP-27 | partial | automated, synthetic_e2e | `P3` · `tests/test_exporting.py::test_translation_prepare_reads_one_frozen_projection_without_writing_export_state`、`::test_real_sigkill_export_is_recovered_by_the_next_writer_entry`、`::test_dead_export_owner_recovery_cleans_only_registered_paths_and_retries_fresh`、`::test_export_recovery_does_not_interrupt_or_clean_an_unproven_live_owner` | 候选阶段零文件；三处真实 SIGKILL 可被下一 writer 中断清理；注入中断场景证明只清登记路径、不碰成功/未知目录并以新 attempt 重试且 latest 不变 | 未在同一真实杀进程场景同时放入成功导出、未知目录并完成重试；PID 复用也未与该组合交互，故不能证明复合条件；补真实多状态 SIGKILL E2E |
| IMP-28 | partial | automated, synthetic_e2e | `F1` · 152 条 Chromium/WebKit 行为断言；`P3` · `tests/test_reporting.py::test_dashboard_renders_markup_like_code_tokens_as_inert_data`、`::test_report_bundle_and_snapshot_are_deterministic_safe_and_idempotent` | 零请求/错误、CSP 阳性对照、多宽度、部分键盘、forced-colors、打印、注入惰性化、双快照身份与逐条同源投影等子集 | 下方 DASH 矩阵仍有仓库内缺口，且未做灰度/色觉与 Owner 视觉；完成全部 DASH 后再重评 |

## 4. DASH 证据矩阵

`F1` 的实际 assertion 名直接列在证据列；所有 assertion 均在 Chromium 与 WebKit 执行，除 `no-horizontal-overflow` 外不以夹具存在替代断言。

| ID | status | evidence_class | 精确证据节点 / assertion | 已覆盖子句 | 未覆盖子句 / 最小后续归属 |
| --- | --- | --- | --- | --- | --- |
| DASH-01 | partial | automated, synthetic_e2e | `F1` · `clean-external-requests`、`deferred-search-focus`、`coverage-scope-link-activates-filter`、`focused-project-enter-activation` | file URL、九视图、检索/筛选/导航子集及双引擎零外部请求 | 未断言断网双击、证据展开、缺口和复习的全部可用性与请求即阻止发布；补离线完整交互 E2E |
| DASH-02 | partial | automated, synthetic_e2e | `F1` · `clean-console-errors`、`clean-page-errors`、`csp-style-positive-control`、`csp-connect-probe`、`probe-errors-separated` | 双引擎干净加载、CSP style/connect 阳性对照能区分失效 | 未建立“全部交互”的可枚举覆盖及正常运行 `securitypolicyviolation` 零记录断言；补交互清单与事件计数 |
| DASH-03 | partial | automated, synthetic_e2e | `P3` · `tests/test_reporting.py::test_embedded_json_escape_keys_match_independent_expected_set`、`::test_dashboard_renders_markup_like_code_tokens_as_inert_data`、`::test_report_bundle_and_snapshot_are_deterministic_safe_and_idempotent`；`F1` · `clean-external-requests` | 关键 Unicode/标记/style/事件属性转义、长串 CSS、javascript href 静态排除且渲染不拒绝 | 未在真实浏览器载入完整恶意语料并断言文本可读、无点击目标、无执行/请求及长串不溢出；补 adversarial browser fixture |
| DASH-04 | partial | automated, synthetic_e2e | `F1` · `no-horizontal-overflow`（5 宽度 × 9 视图 × 2 引擎） | 375px 在全部视图无横向滚动 | 未机检无遮挡与多列表格转定义列表；补窄屏结构/遮挡断言并由 Owner 视觉抽验 |
| DASH-05 | partial | automated, synthetic_e2e | `F1` · `forced-colors-project-disposition`、`role-lens-assumption-visible`、`coverage-scope-link-activates-filter` | partial 夹具中四种 disposition 标签的图标与文字可见，覆盖范围链接可激活项目筛选 | 未断言 L0 partial、L2 各段计数与 carried_forward 参与评分、L3 原因/影响/补救、不可折叠且位置固定；补首屏降级语义断言 |
| DASH-06 | partial | automated, synthetic_e2e | `F1` · `forced-colors-evidence-validity`、`forced-colors-commit-state-text-channel`、`print-full-locator` | current/stale/missing/plan 与 commit_state 文本通道、完整 locator 存在 | 未断言两次交互上限、必需字段全集、多 worktree 等价来源展开、降级措辞和不含源码/diff；补 Claim→Evidence 交互链 |
| DASH-07 | partial | automated, synthetic_e2e | `F1` · `print-controls-hidden`、`print-details-expanded`、`print-full-locator` | 打印时隐藏交互控件、展开 details、locator 不截断 | 未断言 hash、快照身份与降级带保留，也未做 PDF/打印视觉核对；补打印语义断言与 Owner 视觉 |
| DASH-08 | partial | automated, synthetic_e2e | `F1` · `coverage-scope-link-focusable`、`coverage-scope-link-outside-nav`、`deferred-search-focus`、`focused-project-enter-activation` | 若干链接/检索/项目项可聚焦并由 Enter 激活 | 未覆盖切视图→检索→筛选→展开证据→复制 locator 全链、焦点顺序/焦点环及鼠标唯一入口；补单一纯键盘 E2E 与视觉抽验 |
| DASH-09 | partial | automated, synthetic_e2e | `F1` · `forced-colors-media-active`、`forced-colors-project-disposition`、`forced-colors-evidence-validity`、`forced-colors-support-level`、`forced-colors-commit-state-text-channel`、`forced-colors-review-continuity` | forced-colors 下状态具有图标+文字通道，commit_state 仍可读 | 未测灰度打印、色觉模拟以及分数/覆盖度可读；补媒体模拟自动化与 Owner 视觉 |
| DASH-10 | verified | automated, synthetic_e2e | `F1` · `dash10-completed-snapshot-identity`、`dash10-partial-snapshot-identity`、`dash10-same-role-distinct-snapshots`、`dash10-cross-version-deep-link-rejected`、`dash10-cross-version-no-wrong-object`；变异 `visible-snapshot-identity`、`snapshot-identity`、`cross-version-fallback` | 同岗位 completed/partial 两份真实渲染快照分别核对可见与数据镜像中的 status、run ID、snapshot hash；四类跨版本深链明确报错、显示目标 hash 且不落错对象；三项变异均使对应语义判红 | 无（本基线） |
| DASH-11 | partial | automated, synthetic_e2e | `F1` · `forced-colors-review-continuity` | new/continued/reassess_required 三态的图标与文字可区分 | 未断言冻结时间、可复制 Skill 调用、无写控件与不创建提醒；补复习区只读契约断言 |
| DASH-12 | verified | automated, synthetic_e2e | `F1` · `dash12-claim-evidence-parity`、`dash12-limitation-parity`、`dash12-no-html-only-conclusions`；变异 `parity-field`、`visible-projection-field`、`limitation-id` | 对同一冻结投影分别提取非空 Markdown 与 HTML，逐条核对 Claim、Evidence validity、facets、commit state 与 limitations；HTML 可见字段同时对账数据镜像；三项变异均使发布判红 | 无（本基线） |

## 5. 动态汇总与下一批候选

本次按表内首列现场计算：

| 集合 | verified | partial | missing | owner_blocked | 合计 |
| --- | ---: | ---: | ---: | ---: | ---: |
| IMP | 3 | 25 | 0 | 0 | 28 |
| DASH | 2 | 10 | 0 | 0 | 12 |
| 总计 | 5 | 35 | 0 | 0 | 40 |

建议后续按最小目标分卡，而非把缺口一次性塞进一张卡：

1. GJ-14 已闭合 `IMP-15`、`IMP-19` 并为六项 `partial` 增加主链证据；GJ-15 已闭合 `DASH-10`、`DASH-12`。下一卡继续从本表逐行选择最小剩余缺口。
2. 再补看板首屏降级、Claim 到证据、纯键盘与打印/无色通道；自动化闭合后再单独请求 Owner 视觉核对。
3. 扫描与分析侧优先补根外 Git 非法配置矩阵、增量删除/读取计数、未知语言、匹配测试结果和完整项目资格五类合成 E2E。
4. 安装/保留侧单独验证 Skill 升级重装、usage 统计与个人数据不变；不要和运行时行为卡混合。
5. `OWN-01` 已裁定且 GJ-16A 已闭合工作树合成证据；下一步由 GJ-16B 对 Owner 指定的真实 CodeRoute/SliverShield 取得 `real_workspace` 证据，用户级安装仍由 GJ-17 单独验证。

## 6. 机械一致性检查

以下命令以两个权威表动态提取 ID，并只读取本账本两张主表与汇总表；成功条件为三个 `diff` 均为空、两组 `uniq -d` 均为空，且最终打印 `IMP=28 DASH=12`：

```sh
baseline_ids=$(sed -n '/^### 3\.2 /,/^## 4\./p' docs/40-delivery/acceptance-baseline.md | rg -o 'IMP-[0-9]{2}' | sort -u)
dashboard_ids=$(sed -n '/^## 12\./,/^## 13\./p' docs/20-architecture/dashboard-design.md | rg -o 'DASH-[0-9]{2}' | sort -u)
ledger_imp=$(sed -n '/^## 3\./,/^## 4\./p' docs/40-delivery/acceptance-evidence.md | sed -n 's/^| \(IMP-[0-9][0-9]\) |.*/\1/p')
ledger_dash=$(sed -n '/^## 4\./,/^## 5\./p' docs/40-delivery/acceptance-evidence.md | sed -n 's/^| \(DASH-[0-9][0-9]\) |.*/\1/p')
computed_summary=$(awk -F'|' '
  /^[|] (IMP|DASH)-[0-9][0-9] / {
    set=$2; status=$3; gsub(/^ +| +$/, "", set); set=(substr(set,1,4)=="DASH" ? "DASH" : "IMP"); gsub(/^ +| +$/, "", status)
    count[set,status]++; count[set,"total"]++
  }
  END {
    split("verified partial missing owner_blocked", statuses, " ")
    for (set_index=1; set_index<=2; set_index++) {
      set=(set_index==1 ? "IMP" : "DASH"); printf "%s", set
      for (status_index=1; status_index<=4; status_index++) printf "|%d", count[set,statuses[status_index]]
      printf "|%d\n", count[set,"total"]
    }
    printf "总计|%d|%d|%d|%d|%d\n",
      count["IMP","verified"]+count["DASH","verified"],
      count["IMP","partial"]+count["DASH","partial"],
      count["IMP","missing"]+count["DASH","missing"],
      count["IMP","owner_blocked"]+count["DASH","owner_blocked"],
      count["IMP","total"]+count["DASH","total"]
  }
' docs/40-delivery/acceptance-evidence.md)
recorded_summary=$(sed -n '/^| IMP |/p;/^| DASH |/p;/^| 总计 |/p' docs/40-delivery/acceptance-evidence.md | tr -d ' ' | sed 's/^|//;s/|$//')
diff -u <(printf '%s\n' "$baseline_ids") <(printf '%s\n' "$ledger_imp" | sort -u)
diff -u <(printf '%s\n' "$dashboard_ids") <(printf '%s\n' "$ledger_dash" | sort -u)
diff -u <(printf '%s\n' "$computed_summary") <(printf '%s\n' "$recorded_summary")
printf '%s\n' "$ledger_imp" | sort | uniq -d
printf '%s\n' "$ledger_dash" | sort | uniq -d
printf 'IMP=%s DASH=%s\n' "$(printf '%s\n' "$ledger_imp" | sed '/^$/d' | wc -l | tr -d ' ')" "$(printf '%s\n' "$ledger_dash" | sed '/^$/d' | wc -l | tr -d ' ')"
```
