import time

from cc_autopilot_notify import doctor, hook_permission, state


def _auq():
    return {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion",
            "session_id": "s1",
            "tool_input": {"questions": [{"question": "Q",
                                          "options": [{"label": "A"}, {"label": "B (Recommended)"}]}]}}


def test_answer_returns_hook_shape():
    out = hook_permission.answer(_auq(), wait_seconds=0, judge_fn=lambda q: 1)
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    answers = out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]
    assert answers["Q"] == "B (Recommended)"


def test_answer_ignores_other_tools():
    ev = dict(_auq(), tool_name="Bash")
    assert hook_permission.answer(ev, wait_seconds=0, judge_fn=lambda q: 0) is None


def test_answer_multiselect_joins_labels():
    ev = _auq()
    ev["tool_input"]["questions"][0]["multiSelect"] = True
    out = hook_permission.answer(ev, wait_seconds=0, judge_fn=lambda q: 0)
    assert "," in out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]["Q"]


def test_doctor_reports_missing_heartbeat_as_fail(tmp_path):
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {"ok": False})
    by = {c.name: c for c in checks}
    assert by["daemon heartbeat"].status == "FAIL"


def test_doctor_reports_fresh_heartbeat_as_pass(tmp_path):
    state.write_heartbeat(tmp_path, when=time.time())
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {"ok": False})
    assert {c.name: c for c in checks}["daemon heartbeat"].status == "PASS"


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
