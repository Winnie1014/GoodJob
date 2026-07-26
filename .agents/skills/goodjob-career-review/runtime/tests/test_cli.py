from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def _send_json(process: subprocess.Popen[str], payload: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    assert isinstance(response, dict)
    return response


def _start_broker(data_dir: Path, *, cwd: Path = RUNTIME_DIR) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(RUNTIME_DIR / "scripts" / "session.py"), "--data-dir", str(data_dir)],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )


def _stop_broker(broker: subprocess.Popen[str]) -> None:
    assert broker.stdin is not None
    broker.stdin.close()
    assert broker.wait(timeout=5) == 0
    assert broker.stderr is not None
    assert broker.stderr.read() == ""


def test_session_broker_reuses_one_fd_capability_until_stdin_closes(tmp_path: Path) -> None:
    workspace = tmp_path / "工作区"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    broker = _start_broker(data_dir)

    rejected_notice = _send_json(
        broker,
        {
            "op": "authorize_source_analysis",
            "workspace": str(workspace),
            "confirmed": True,
            "notice_version": {},
        },
    )
    assert rejected_notice["status"] == "error"
    assert rejected_notice["code"] == "invalid_input"

    rejected_surrogate = _send_json(
        broker,
        {
            "op": "authorize_source_analysis",
            "workspace": "\ud800",
            "confirmed": True,
        },
    )
    assert rejected_surrogate["status"] == "error"
    assert rejected_surrogate["code"] == "invalid_input"

    rejected_nul = _send_json(
        broker,
        {
            "op": "authorize_source_analysis",
            "workspace": "\x00",
            "confirmed": True,
        },
    )
    assert rejected_nul["status"] == "error"
    assert rejected_nul["code"] == "invalid_input"

    assert broker.stdin is not None
    assert broker.stdout is not None
    broker.stdin.write('{"op":"authorize_source_analysis","workspace":1e9999}\n')
    broker.stdin.flush()
    rejected_non_finite = json.loads(broker.stdout.readline())
    assert rejected_non_finite["status"] == "error"
    assert rejected_non_finite["code"] == "invalid_input"

    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    assert authorized["status"] == "ok"
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    verified = _send_json(
        broker,
        {
            "op": "verify_source_analysis",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    assert verified["status"] == "ok"
    assert "session_capability" not in json.dumps(authorized)
    assert "session_capability" not in json.dumps(verified)

    rejected_relation = _send_json(
        broker,
        {
            "op": "authorize_external_git_relation_probe",
            "workspace": str(workspace),
            "git_pointer": str(workspace / ".git"),
            "confirmed": True,
        },
    )
    assert rejected_relation["status"] == "error"
    assert rejected_relation["code"] == "invalid_input"

    rejected_metadata = _send_json(
        broker,
        {
            "op": "authorize_external_git_metadata",
            "workspace": str(workspace),
            "git_pointer": str(workspace / ".git"),
            "git_dir": str(tmp_path / "outside-git"),
            "common_dir": str(tmp_path / "outside-common"),
            "confirmed": True,
        },
    )
    assert rejected_metadata["status"] == "error"
    assert rejected_metadata["code"] == "invalid_input"
    rejected_unknown_grant = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "external_git_metadata_receipt_ids": ["unknown-receipt"],
        },
    )
    assert rejected_unknown_grant["status"] == "error"
    assert rejected_unknown_grant["code"] == "invalid_input"

    _stop_broker(broker)

    status = subprocess.run(
        [sys.executable, "-m", "goodjob", "--data-dir", str(data_dir), "data-status"],
        cwd=RUNTIME_DIR,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["usage_bytes"]["sqlite"] > 0


def test_protected_children_ignore_a_workspace_goodjob_module(tmp_path: Path) -> None:
    workspace = tmp_path / "hostile-workspace"
    workspace.mkdir()
    marker = tmp_path / "untrusted-module-executed"
    (workspace / "goodjob.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "captured = []\n"
        "for descriptor in range(3, 256):\n"
        "    try:\n"
        "        captured.append(os.read(descriptor, 1024))\n"
        "    except OSError:\n"
        "        pass\n"
        f"Path({str(marker)!r}).write_bytes(b''.join(captured) or b'executed')\n",
        encoding="utf-8",
    )
    broker = _start_broker(tmp_path / "data", cwd=workspace)

    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )

    assert authorized["status"] == "ok"
    assert not marker.exists()
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "系统工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    assert validated["status"] == "ok"
    _stop_broker(broker)


def test_documented_uv_launch_ignores_the_target_project(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    installed_runtime = tmp_path / "installed-skill" / "runtime"
    shutil.copytree(
        RUNTIME_DIR,
        installed_runtime,
        ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "*.pyc"),
    )
    workspace = tmp_path / "hostile-uv-project"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project\ninvalid", encoding="utf-8")
    (workspace / "uv.toml").write_text("[invalid\nconfig", encoding="utf-8")
    marker = tmp_path / "uv-imported-workspace-module"
    (workspace / "goodjob.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    broker = subprocess.Popen(
        [
            uv,
            "run",
            "--isolated",
            "--no-project",
            "--no-config",
            "--offline",
            "--no-python-downloads",
            "--python",
            "3.12",
            "python",
            "-I",
            "-B",
            str(installed_runtime / "scripts" / "session.py"),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "UV_CACHE_DIR": str(tmp_path / "empty-uv-cache"),
            "UV_NO_PROGRESS": "1",
        },
    )

    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )

    assert authorized["status"] == "ok"
    assert not marker.exists()
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "系统工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    assert validated["status"] == "ok"
    assert broker.stdin is not None
    broker.stdin.close()
    assert broker.wait(timeout=5) == 0
    assert broker.stderr is not None
    stderr = broker.stderr.read()
    assert "Ignoring project discovery error due to `--no-project`" in stderr
    assert "uv.toml" not in stderr
    assert not list(installed_runtime.rglob("__pycache__"))
    assert not list(installed_runtime.rglob("*.pyc"))
    assert not (installed_runtime / ".venv").exists()


def test_job_input_preflight_blocks_bad_jd_before_scan_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "job-input-preflight"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"
    broker = _start_broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    receipt_id = receipt["authorization_receipt_id"]
    assert isinstance(receipt_id, str)

    initially_valid = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    initially_valid_job = initially_valid["job_input"]
    assert isinstance(initially_valid_job, dict)
    old_validation_sha256 = initially_valid_job["validation_sha256"]
    assert isinstance(old_validation_sha256, str)

    rejected = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "file", "path": str(tmp_path / "missing-jd.txt")},
            },
        },
    )

    assert rejected["status"] == "error"
    assert rejected["code"] == "invalid_input"
    rejected_old_digest = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input_validation_sha256": old_validation_sha256,
        },
    )
    assert rejected_old_digest["status"] == "error"
    assert rejected_old_digest["code"] == "invalid_input"
    rejected_missing_digest = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
        },
    )
    assert rejected_missing_digest["status"] == "error"
    assert rejected_missing_digest["code"] == "invalid_input"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM job_inputs").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM role_lenses").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM preparation_runs").fetchone()[0] == 0
    connection.close()

    continued = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "continue_without_jd"},
            },
        },
    )
    assert continued["status"] == "ok"
    continued_job_input = continued["job_input"]
    assert isinstance(continued_job_input, dict)
    continued_sha256 = continued_job_input["validation_sha256"]
    assert isinstance(continued_sha256, str)
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": continued_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
        },
    )
    assert scanned["status"] == "ok"
    _stop_broker(broker)


def test_scan_overview_reuses_a_terminal_run_in_a_new_task(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "overview-reuse"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    first_broker = _start_broker(data_dir)
    first_authorized = _send_json(
        first_broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    first_receipt = first_authorized["receipt"]
    assert isinstance(first_receipt, dict)
    first_validated = _send_json(
        first_broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": first_receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "应用软件工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    first_job = first_validated["job_input"]
    assert isinstance(first_job, dict)
    scanned = _send_json(
        first_broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": first_receipt["authorization_receipt_id"],
            "job_input_validation_sha256": first_job["validation_sha256"],
        },
    )
    first_run = scanned["scan_run"]
    assert isinstance(first_run, dict)
    first_scan_run_id = first_run["scan_run_id"]
    _stop_broker(first_broker)

    second_broker = _start_broker(data_dir)
    second_authorized = _send_json(
        second_broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    second_receipt = second_authorized["receipt"]
    assert isinstance(second_receipt, dict)
    second_validated = _send_json(
        second_broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": second_receipt["authorization_receipt_id"],
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "系统工程师",
                "jd_input": {"kind": "none"},
            },
        },
    )
    second_job = second_validated["job_input"]
    assert isinstance(second_job, dict)
    overview_response = _send_json(
        second_broker,
        {
            "op": "scan_overview",
            "workspace": str(workspace),
            "authorization_receipt_id": second_receipt["authorization_receipt_id"],
            "job_input_validation_sha256": second_job["validation_sha256"],
        },
    )
    overview = overview_response["scan_overview"]
    assert isinstance(overview, dict)
    assert overview["found"] is True
    reused_run = overview["scan_run"]
    assert isinstance(reused_run, dict)
    assert reused_run["scan_run_id"] == first_scan_run_id
    assert overview["coverage"] == scanned["coverage"]

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM scan_run_overviews").fetchone()[0] == 1
    connection.close()
    _stop_broker(second_broker)


def test_maximum_escaped_jd_crosses_the_broker_for_validation_and_prepare(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "large-jd"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (workspace / "main.py").write_text("def serve():\n    return True\n", encoding="utf-8")
    large_jd = "\n" * (512 * 1024 - 1) + "x"
    assert len(large_jd.encode("utf-8")) == 512 * 1024
    broker = _start_broker(tmp_path / "data")
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    receipt_id = receipt["authorization_receipt_id"]
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "中间件工程师",
                "jd_input": {"kind": "text", "text": large_jd},
            },
        },
    )
    job_input = validated["job_input"]
    assert isinstance(job_input, dict)
    validation_sha256 = job_input["validation_sha256"]
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input_validation_sha256": validation_sha256,
        },
    )
    scan_run = scanned["scan_run"]
    assert isinstance(scan_run, dict)
    prepared = _send_json(
        broker,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_request": {
                "contract_version": "preparation-request-v1",
                "request_id": str(uuid.uuid4()),
                "scan_run_id": scan_run["scan_run_id"],
                "config_revision": "large-jd-test-v1",
                "target_role": "中间件工程师",
                "jd_input": {"kind": "text", "text": large_jd},
                "job_input_validation_sha256": validation_sha256,
                "requested_exports": [],
                "role_lens": {
                    "contract_version": "role-lens-v1",
                    "dimensions": [
                        {
                            "key": "middleware_depth",
                            "display_name": "中间件深度",
                            "weight_bps": 10000,
                            "evaluation_criteria": "评价实现证据",
                            "required_evidence_kinds": ["implementation"],
                        }
                    ],
                    "evidence_requirements": ["implementation"],
                    "ranking_rules": ["岗位相关性优先"],
                    "output_sections": ["中间件能力"],
                    "question_strategy": {"primary": "追问实现机制"},
                    "gap_rules": ["缺少证据则记录缺口"],
                    "assumptions": [],
                    "generator_id": "large-jd-test",
                    "prompt_contract_version": "large-jd-test-v1",
                },
            },
        },
    )
    preparation_run = prepared["preparation_run"]
    assert isinstance(preparation_run, dict)
    assert preparation_run["status"] == "analyzing"
    assert large_jd not in str(prepared)
    _stop_broker(broker)


def test_preparation_protocol_does_not_echo_private_payload_and_is_task_bound(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "session-prepare"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    request_id = str(uuid.uuid4())
    request: dict[str, object] = {
        "contract_version": "preparation-request-v1",
        "request_id": request_id,
        "scan_run_id": "filled-after-scan",
        "config_revision": "prepare-config-v1",
        "target_role": "平台工程师",
        "jd_input": {"kind": "text", "text": "负责平台工程和可靠性。"},
        "requested_exports": [],
        "role_lens": {
            "contract_version": "role-lens-v1",
            "dimensions": [
                {
                    "key": "platform_depth",
                    "display_name": "平台深度",
                    "weight_bps": 10000,
                    "evaluation_criteria": "评价实现和运行边界",
                    "required_evidence_kinds": ["implementation"],
                }
            ],
            "evidence_requirements": ["implementation"],
            "ranking_rules": ["岗位相关性优先"],
            "output_sections": ["能力地图", "项目讲解"],
            "question_strategy": {"primary": "追问机制"},
            "gap_rules": ["证据不足则标记缺口"],
            "assumptions": [],
            "generator_id": "codex-test",
            "prompt_contract_version": "role-prompt-v1",
        },
    }
    broker = _start_broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    receipt_id = receipt["authorization_receipt_id"]
    assert isinstance(receipt_id, str)
    validated = _send_json(
        broker,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": request["target_role"],
                "jd_input": request["jd_input"],
            },
        },
    )
    assert validated["status"] == "ok"
    assert "负责平台工程和可靠性" not in json.dumps(validated, ensure_ascii=False)
    validated_job_input = validated["job_input"]
    assert isinstance(validated_job_input, dict)
    validation_sha256 = validated_job_input["validation_sha256"]
    assert isinstance(validation_sha256, str)
    request["job_input_validation_sha256"] = validation_sha256
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "job_input_validation_sha256": validation_sha256,
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
        },
    )
    scan_run = scanned["scan_run"]
    assert isinstance(scan_run, dict)
    scan_run_id = scan_run["scan_run_id"]
    assert isinstance(scan_run_id, str)
    request["scan_run_id"] = scan_run_id

    prepared = _send_json(
        broker,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "preparation_request": request,
        },
    )
    assert prepared["status"] == "ok"
    assert "负责平台工程和可靠性" not in json.dumps(prepared, ensure_ascii=False)
    preparation_run = prepared["preparation_run"]
    evidence_bundle = prepared["evidence_bundle"]
    assert isinstance(preparation_run, dict)
    assert isinstance(evidence_bundle, dict)
    preparation_run_id = preparation_run["preparation_run_id"]
    suggestions = evidence_bundle["deep_read_suggestions"]
    assert isinstance(preparation_run_id, str)
    assert isinstance(suggestions, list) and suggestions
    suggestion = suggestions[0]
    assert isinstance(suggestion, dict)
    source_revision_id = suggestion["source_revision_id"]
    assert isinstance(source_revision_id, str)
    reauthorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    replacement_receipt = reauthorized["receipt"]
    assert isinstance(replacement_receipt, dict)
    replacement_receipt_id = replacement_receipt["authorization_receipt_id"]
    assert isinstance(replacement_receipt_id, str)
    repeated = _send_json(
        broker,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": replacement_receipt_id,
            "preparation_request": request,
        },
    )
    assert repeated["status"] == "ok"
    repeated_run = repeated["preparation_run"]
    assert isinstance(repeated_run, dict)
    assert repeated_run["preparation_run_id"] == preparation_run_id
    checked = _send_json(
        broker,
        {
            "op": "verify_source_revision",
            "workspace": str(workspace),
            "authorization_receipt_id": replacement_receipt_id,
            "preparation_run_id": preparation_run_id,
            "source_revision_ids": [source_revision_id],
            "phase": "before_read",
        },
    )
    assert checked["status"] == "ok"
    assert checked["run_status"] == "analyzing"
    _stop_broker(broker)

    next_task = _start_broker(data_dir)
    next_authorized = _send_json(
        next_task,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    next_receipt = next_authorized["receipt"]
    assert isinstance(next_receipt, dict)
    next_receipt_id = next_receipt["authorization_receipt_id"]
    assert isinstance(next_receipt_id, str)
    next_validated = _send_json(
        next_task,
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": next_receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": request["target_role"],
                "jd_input": request["jd_input"],
            },
        },
    )
    assert next_validated["status"] == "ok"
    copied_request = _send_json(
        next_task,
        {
            "op": "prepare_start",
            "workspace": str(workspace),
            "authorization_receipt_id": next_receipt_id,
            "preparation_request": request,
        },
    )
    assert copied_request["status"] == "error"
    assert copied_request["code"] == "authorization_session_mismatch"
    copied_run = _send_json(
        next_task,
        {
            "op": "verify_source_revision",
            "workspace": str(workspace),
            "authorization_receipt_id": next_receipt_id,
            "preparation_run_id": preparation_run_id,
            "source_revision_ids": [source_revision_id],
            "phase": "before_read",
        },
    )
    assert copied_run["status"] == "error"
    assert copied_run["code"] == "invalid_input"
    _stop_broker(next_task)
