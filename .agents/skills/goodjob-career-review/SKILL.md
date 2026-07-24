---
name: goodjob-career-review
description: Generate a local, evidence-traceable career preparation package for one explicit workspace and target role. Use when the user asks to scan local projects for interview preparation, create a role-focused preparation package, refresh GoodJob evidence, export English materials, or record interview review progress.
---

# GoodJob Career Review

Use this Skill only after the Owner explicitly asks to work with a local workspace. Never scan, read project files, or reuse an old receipt before the Owner confirms the current workspace scope and their authority to analyze it.

## Session Workflow

1. Collect one workspace path, one target role, optional JD input, optional level override, and requested output intent.
2. Show the normalized workspace path, the planned processing categories, the local data directory, and that source files opened by Codex enter the current Codex model-processing boundary. Explain that GoodJob adds no separate upload or telemetry channel and does not assess NDA, copyright, or organization-policy compliance.
3. Require a clear Owner confirmation before authorizing source analysis. A readable path alone is not confirmation. For a bad JD file, require correction or explicit `continue_without_jd` before creating job input or a preparation run.
4. Resolve `runtime_dir` as the directory next to this `SKILL.md` plus `/runtime`. Run `uv run --project <runtime_dir> python <runtime_dir>/scripts/session.py --workspace <workspace> --verify` after confirmation. The wrapper keeps a fresh 256-bit capability in process memory and forwards it only through inherited file descriptors; never place a capability in arguments, environment variables, notes, logs, or output.
5. Use only JSON returned by the local runtime. Treat `authorization_session_mismatch` and `writer_busy` as explicit outcomes; do not retry by copying a receipt ID, reading SQLite directly, or removing a lock file.

## Current Runtime Surface

- `goodjob bootstrap`: create the owner-local layout and apply migrations.
- `goodjob data-status`: show aggregate storage usage without exposing project data.
- `scripts/session.py --workspace <path> --verify`: issue and verify one `source_analysis` authorization receipt in the same volatile session.

The initial runtime deliberately does not claim to scan, analyze, or generate career materials until those later commands are implemented and validated. Keep existing artifacts readable; never modify a user workspace.
