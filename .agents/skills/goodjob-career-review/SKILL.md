---
name: goodjob-career-review
description: Build a local, evidence-traceable scan baseline for one explicitly authorized workspace. Use when the user asks to scan or refresh local projects for later interview preparation.
---

# GoodJob Career Review

Use this Skill only after the Owner explicitly asks to work with a local workspace. Never scan, read project files, or reuse an old receipt before the Owner confirms the current workspace scope and their authority to analyze it.

## Session Workflow

1. Collect one workspace path and the requested scan or refresh intent. The current runtime creates scan evidence only; do not claim that it has created a role package, export, or interview review.
2. Show the normalized workspace path, the planned processing categories, the local data directory, and that source files opened by Codex enter the current Codex model-processing boundary. Explain that GoodJob adds no separate upload or telemetry channel and does not assess NDA, copyright, or organization-policy compliance.
3. Require a clear Owner confirmation before authorizing source analysis. A readable path alone is not confirmation. For a bad JD file, require correction or explicit `continue_without_jd` before creating job input or a preparation run.
4. Resolve `runtime_dir` as the directory next to this `SKILL.md` plus `/runtime`. Start `uv run --isolated --with <runtime_dir> python <runtime_dir>/scripts/session.py` once for the current Codex task and keep its standard input open only for that task. This uses uv's cache-backed isolated environment rather than creating `.venv` under the installed Skill. Send JSONL operations to authorize and then invoke protected core work through the same broker; it exits when standard input closes. The broker holds one fresh 256-bit capability only in task-scoped process memory and forwards it to short-lived core children only through inherited file descriptors. Never place a capability in arguments, environment variables, notes, logs, or output.
5. Send `{"op":"authorize_source_analysis","workspace":"<workspace>","confirmed":true}` only after confirmation. Keep the resulting receipt ID as non-authorizing bookkeeping; every later protected operation must remain in the same broker process. Use only JSON returned by the local runtime. Treat `authorization_session_mismatch` and `writer_busy` as explicit outcomes; do not retry by copying a receipt ID, reading SQLite directly, or removing a lock file.
6. If scan reports `external_git_authorization_required`, do not retry with guessed paths. Send `inspect_external_git_candidate` with the source receipt; this reads only the in-workspace `.git` marker. Show its exact `marker_kind`, `git_dir_candidate`, optional `common_dir_candidate`, and `read_fields`, then request explicit confirmation before `authorize_external_git_relation_probe`. Call `probe_external_git_relation` with both receipt IDs and show the exact `git_dir`, `common_dir`, directory identities, and `read_fields`; request a second explicit confirmation before `authorize_external_git_metadata`. Only then pass that metadata receipt ID to `scan` or `refresh`. External metadata mode directly reads only relation files and HEAD/ref through bound descriptors; it never invokes Git or reads root-external index, dirty state, history, objects, blobs, diffs, config, other worktrees, or source.
7. Use `query_history_candidates` only when a concrete role-analysis question cannot be answered by the frozen scan's current sources and recent 180-day history. Supply that `scan_run_id`, the role-analysis identifier, one internal Git project/worktree, bounded indexed relative paths, and a short reason. Select only a candidate returned by that operation, then send `read_history_candidate` for one of its returned changed paths through the same broker. Candidate approval exists only in broker memory and cannot be copied to another task. The returned diff/blob text is transient model input: do not persist it, claim it as Evidence, or imply that `record_analysis` exists in the current runtime. Root-external Git projects are never eligible for this operation.

## Current Runtime Surface

- `goodjob bootstrap`: create the owner-local layout and apply migrations.
- `goodjob data-status`: show aggregate storage usage without exposing project data.
- `goodjob scan`: after a valid source-analysis receipt, create a full immutable workspace scan snapshot.
- `goodjob refresh`: after a valid source-analysis receipt, create an explicit `fast` or `verify_content` scan snapshot for a registered workspace.
- `goodjob query-history-candidates`: return bounded commit/path metadata older than the initial history window for one frozen internal Git baseline.
- `goodjob read-history-candidate`: transiently read one selected candidate path through the same bounded Git sandbox; it never persists source or diff text.
- `scripts/session.py`: task-scoped JSONL broker for `authorize_source_analysis`, `verify_source_analysis`, `inspect_external_git_candidate`, `authorize_external_git_relation_probe`, `probe_external_git_relation`, `authorize_external_git_metadata`, `scan`, `refresh`, `query_history_candidates`, and `read_history_candidate`; it exits when input closes and retains no capability or history-candidate approval afterwards.

Scan, refresh, and targeted history operations only read the confirmed workspace. They do not execute project code, builds, package managers, network requests, Git fetches, or checkouts, and they do not modify the workspace. They persist paths, hashes, bounded local commit metadata, structured coverage, and short evidence summaries in owner-local state, never complete source files, secrets, or diffs. Keep existing artifacts readable; never modify a user workspace.
