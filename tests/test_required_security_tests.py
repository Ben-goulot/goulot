"""Every mandatory security test of the finished phases exists in tests/security, and none is skipped
(docs/SECURITY.md, « Tests de sécurité obligatoires »)."""

from __future__ import annotations

import ast
import re

from goulot.settings import REPO_ROOT

PHASE_1 = {
    "test_bind_loopback_only", "test_host_header_rejected", "test_csrf_and_origin",
    "test_auth_required_on_all_routes", "test_launch_token_single_use", "test_security_headers",
    "test_outbound_allowlist", "test_private_ip_blocked", "test_sdk_uses_allowlist_transport",
    "test_xml_bombs_rejected", "test_no_secret_in_responses", "test_log_redaction", "test_file_permissions",
    "test_dependencies_allowlist", "test_private_data_encrypted_at_rest", "test_ciphertext_bound_to_slot",
    "test_encrypted_fields_survive_migration", "test_inputs_digest_is_keyed", "test_vault_recovery_roundtrip",
    "test_private_routes_require_vault", "test_vault_token_expiry", "test_poll_does_not_extend_vault_token",
    "test_unlock_rate_limited", "test_passkey_enrollment_requires_proof",
}

PHASE_2 = {"test_prompt_injection_fixtures", "test_no_user_data_in_prompts"}
PHASE_3 = {
    "test_xss_fixtures_rendered_as_text", "test_prompt_injection_fixtures", "test_fake_sites_cannot_confirm",
    "test_alert_links_defanged", "test_budget_cap_modes", "test_circuit_breaker", "test_telegram_send_only",
    "test_collector_limits", "test_ledger_append_only", "test_ledger_tamper_detected",
}
PHASE_4 = {"test_locked_view_leaks_nothing", "test_private_canary_never_leaves", "test_csv_import_in_memory"}
PHASE_5 = {
    "test_update_tampered_rejected", "test_untrusted_key_rejected", "test_update_downgrade_rejected",
    "test_update_rejected_version_not_reinstalled", "test_update_path_traversal_rejected",
    "test_update_healthcheck_rollback", "test_update_manual_rollback_restores_backup",
    "test_rollback_keeps_ledger_and_private_inputs", "test_update_pause_after_two_failures", "test_key_rotation", "test_update_unapproved_release_rejected",
}


def _security_tests() -> dict[str, ast.FunctionDef]:
    found: dict[str, ast.FunctionDef] = {}
    for path in (REPO_ROOT / "tests" / "security").glob("test_*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found[node.name] = node
    return found


def test_security_tests_of_finished_phases_present_and_real() -> None:
    found = _security_tests()
    missing = (PHASE_1 | PHASE_2 | PHASE_3 | PHASE_4 | PHASE_5) - found.keys()
    assert not missing, missing
    for name, node in found.items():
        source = ast.unparse(node)
        assert "skip" not in source and "xfail" not in source, name
        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        raises = [n for n in ast.walk(node) if isinstance(n, ast.With) and "raises" in ast.unparse(n)]
        assert asserts or raises, f"{name} checks nothing"
        assert not re.search(r"assert\s+True\b", source), name
