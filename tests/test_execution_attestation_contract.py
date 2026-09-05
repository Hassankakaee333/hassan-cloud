from hassan_cloud.device_identity import canonical_execution_attestation


def test_execution_attestation_sorts_file_hash_scope_and_preserves_args_order():
    payload = {
        "device_id": "phone-1",
        "permit_id": "permit-1",
        "execution_request_id": "exec-1",
        "project_id": "p1",
        "agent_id": "agent-1",
        "version": "1.2.3",
        "task_id": "task-1",
        "goal_sha256": "a" * 64,
        "approval_evidence_id": "approval-evidence",
        "comparison_evidence_id": "comparison-evidence",
        "static_evidence_id": "static-1",
        "security_verification_job_id": "security-1",
        "benchmark_job_id": "benchmark-1",
        "shadow_job_id": "shadow-1",
        "source_url": "https://example.com/agent.tar.gz",
        "expected_sha256": "d" * 64,
        "command": "bin/agent",
        "protocol_version": 1,
        "files": [
            {"path": "z.txt", "sha256": "c" * 64, "content_base64": "must-not-be-signed"},
            {"path": "a.txt", "sha256": "b" * 64, "content_base64": "must-not-be-signed"},
        ],
        "actions": ["READ_FILES"],
        "args": ["--acp", "--quiet"],
    }
    value = canonical_execution_attestation(payload)
    assert value.startswith("policy:38:frishta-agent-execution-attestation-v1\n")
    assert value.index("file:5:a.txt") < value.index("file:5:z.txt")
    assert f"file_sha256:64:{'b' * 64}" in value
    assert f"file_sha256:64:{'c' * 64}" in value
    assert value.endswith("arg:7:--quiet\n")
    assert "must-not-be-signed" not in value
