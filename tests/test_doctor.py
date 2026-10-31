import json
import time

import pytest

from cc_autopilot_notify import doctor, hook_permission, judge as hook_judge, state
from cc_autopilot_notify.judge import Decision
from cc_autopilot_notify.presence import Presence


@pytest.fixture(autouse=True)
def _stub_host(monkeypatch, tmp_path):
    """Doctor tests must never launch real PowerShell, claude, or read ~/.claude."""
    from cc_autopilot_notify import presence

    monkeypatch.setattr(
        presence, "sample", lambda: Presence(True, 1000.0))

    class Result:
        returncode = 0
        stdout = "2.1.309"
        stderr = ""

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: Result())
    monkeypatch.setenv("CC_AUTOPILOT_SETTINGS",
                       str(tmp_path / "settings-absent.json"))


def test_doctor_fails_when_a_hook_recorded_a_config_error(tmp_path):
    (tmp_path / "config_error.json").write_text(
        json.dumps({"error": "quiet_hours.start must be HH:MM"}))
    checks = doctor.run_all(
        tmp_path, {"channels": {}}, probe=lambda: {},
        presence_fn=lambda: Presence(True, 1.0))
    check = {c.name: c for c in checks}["hook config errors"]
    assert check.status == "FAIL"
    assert "quiet_hours.start" in check.detail


def test_cc_version_failure_is_not_a_pass(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "command not found"

    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: Result())
    check = doctor._cc_version()
    assert check.status == "FAIL"
    assert "exited 1" in check.detail


def test_doctor_reports_presence_pass_with_idle(tmp_path):
    checks = doctor.run_all(
        tmp_path, {"channels": {}}, probe=lambda: {},
        presence_fn=lambda: Presence(True, 12.3))
    check = {c.name: c for c in checks}["powershell presence"]
    assert check.status == "PASS"
    assert "idle=12" in check.detail


def test_doctor_reports_presence_fail_when_unreadable(tmp_path):
    checks = doctor.run_all(
        tmp_path, {"channels": {}}, probe=lambda: {},
        presence_fn=lambda: None)
    assert {c.name: c for c in checks}["powershell presence"].status == "FAIL"


def test_doctor_reports_presence_fail_when_idle_missing(tmp_path):
    checks = doctor.run_all(
        tmp_path, {"channels": {}}, probe=lambda: {},
        presence_fn=lambda: Presence(True, None))
    assert {c.name: c for c in checks}["powershell presence"].status == "FAIL"


def _auq():
    return {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion",
            "session_id": "s1",
            "tool_input": {"questions": [{"question": "Q",
                                          "options": [{"label": "A"}, {"label": "B (Recommended)"}]}]}}


def test_answer_returns_hook_shape():
    out = hook_permission.answer(
        _auq(), wait_seconds=0,
        judge_fn=lambda q: Decision(indices=(1,), labels=("B (Recommended)",),
                                    reason="", fallback=False))
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    answers = out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]
    assert answers["Q"] == "B (Recommended)"


def test_answer_ignores_other_tools():
    ev = dict(_auq(), tool_name="Bash")
    assert hook_permission.answer(ev, wait_seconds=0, judge_fn=lambda q: 0) is None


def test_answer_multiselect_ticks_recommended():
    ev = _auq()
    ev["tool_input"]["questions"][0]["multiSelect"] = True
    out = hook_permission.answer(ev, wait_seconds=0, judge_fn=lambda q: 0)
    answers = out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]
    assert answers["Q"] == "B (Recommended)"


def test_answer_multiselect_delegates_without_marker():
    ev = _auq()
    q = ev["tool_input"]["questions"][0]
    q["multiSelect"] = True
    q["options"] = [{"label": "A"}, {"label": "B"}]
    out = hook_permission.answer(ev, wait_seconds=0, judge_fn=lambda q: 0)
    answers = out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]
    assert answers["Q"] == hook_judge.DELEGATE_TEXT


def test_answer_single_select_fallback_delegates():
    out = hook_permission.answer(
        _auq(), wait_seconds=0,
        judge_fn=lambda q: Decision(indices=(0,), labels=("A",),
                                    reason="", fallback=True))
    answers = out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]
    assert answers["Q"] == hook_judge.DELEGATE_TEXT


def test_doctor_reports_missing_heartbeat_as_fail(tmp_path):
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {"ok": False})
    by = {c.name: c for c in checks}
    assert by["daemon heartbeat"].status == "FAIL"


def test_doctor_reports_fresh_heartbeat_as_pass(tmp_path):
    state.write_heartbeat(tmp_path, when=time.time())
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {"ok": False})
    assert {c.name: c for c in checks}["daemon heartbeat"].status == "PASS"


def test_doctor_fails_instead_of_raising_on_huge_heartbeat_timestamp(tmp_path):
    (tmp_path / "heartbeat.json").write_text(f'{{"at": {10 ** 400}}}')
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {"ok": False})
    assert {c.name: c for c in checks}["daemon heartbeat"].status == "FAIL"


def test_doctor_passes_runtime_check_when_capabilities_present(tmp_path):
    desc = {"protocol_version": 1, "capabilities": ["conversation.read", "conversation.choose"]}
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: desc)
    by = {c.name: c for c in checks}
    assert by["pebrel runtime"].status == "PASS"
    assert by["pebrel conversation capabilities"].status == "PASS"


def test_doctor_fails_on_wrong_protocol_version(tmp_path):
    desc = {"protocol_version": 2, "capabilities": ["conversation.read", "conversation.choose"]}
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: desc)
    assert {c.name: c for c in checks}["pebrel protocol version"].status == "FAIL"


def _probe_verdict(tmp_path, verdict):
    spike = tmp_path / "spike.md"
    spike.write_text(f"# spike\n\n## Result\n\nVERDICT: {verdict}\n", encoding="utf-8")
    return doctor._answers_probe(spike)


def test_answers_probe_fails_on_discarded(tmp_path):
    assert _probe_verdict(tmp_path, "DISCARDED").status == "FAIL"


def test_answers_probe_passes_on_accepted(tmp_path):
    assert _probe_verdict(tmp_path, "ACCEPTED").status == "PASS"


def test_answers_probe_skips_when_not_run(tmp_path):
    assert _probe_verdict(tmp_path, "NOT_RUN").status == "SKIP"


def test_answers_probe_skips_on_undecodable_document(tmp_path):
    spike = tmp_path / "spike.md"
    spike.write_bytes(b"VERDICT: ACCEPTED\xff\xfe")
    assert doctor._answers_probe(spike).status == "SKIP"


def test_cc_version_survives_undecodable_output(monkeypatch):
    import subprocess

    def bad(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(subprocess, "run", bad)
    assert doctor._cc_version().status == "SKIP"


def test_doctor_fails_when_legacy_hooks_remain(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"command":
                    "PYTHONPATH=/x python3 /x/cc_autopilot_notify/run_hook.py notify"}]}]}}))
    monkeypatch.setenv("CC_AUTOPILOT_SETTINGS", str(settings))
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {})
    assert {c.name: c for c in checks}["legacy install"].status == "FAIL"


def test_doctor_passes_when_settings_have_no_legacy_hooks(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}))
    monkeypatch.setenv("CC_AUTOPILOT_SETTINGS", str(settings))
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {})
    assert {c.name: c for c in checks}["legacy install"].status == "PASS"


def test_doctor_passes_for_another_projects_run_hook(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"command": "python3 /other/run_hook.py notify"}]}]}}))
    monkeypatch.setenv("CC_AUTOPILOT_SETTINGS", str(settings))
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {})
    assert {c.name: c for c in checks}["legacy install"].status == "PASS"


def test_doctor_reports_lock_state(tmp_path):
    checks = doctor.run_all(
        tmp_path, {"channels": {}}, probe=lambda: {},
        presence_fn=lambda: Presence(True, 12.3, locked=True))
    check = {c.name: c for c in checks}["windows lock state"]
    assert check.status == "PASS" and "locked=True" in check.detail


def test_doctor_lock_state_skip_when_unreadable(tmp_path):
    checks = doctor.run_all(
        tmp_path, {"channels": {}}, probe=lambda: {},
        presence_fn=lambda: Presence(True, 12.3, locked=None))
    check = {c.name: c for c in checks}["windows lock state"]
    assert check.status == "SKIP"


def test_doctor_canary_counts_blind_matches(tmp_path):
    state.bump_canary(tmp_path, when=time.time())
    checks = doctor.run_all(
        tmp_path, {"channels": {}}, probe=lambda: {},
        presence_fn=lambda: Presence(True, 12.3, locked=True))
    check = {c.name: c for c in checks}["continuation canary"]
    assert check.status == "FAIL" and "1" in check.detail
