import json
from agent.background_review import summarize_background_review_actions
from tools.skill_manager_tool import skill_manage


def _messages(args, data):
    return [{"role": "assistant", "tool_calls": [{"id": "skill", "function": {
        "name": "skill_manage", "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "skill", "content": json.dumps(data)}]


def test_applied_skill_operations_notify_with_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    name = "notify-contract"
    content = f"---\nname: {name}\ndescription: Use when checking notices. Verify applied writes.\n---\nRead the sample before editing.\n"
    operations = [{"name": name, "action": "create", "content": content},
                  {"name": name, "action": "patch", "old_string": "sample", "new_string": "example"},
                  {"name": name, "action": "write_file", "file_path": "references/a.md", "file_content": "Check the example."},
                  {"name": name, "action": "remove_file", "file_path": "references/a.md"},
                  {"name": name, "action": "delete"}]
    for op in operations:
        data = json.loads(skill_manage(action="", name="", operations=[op]))
        assert data["success"], data
        messages = _messages({"operations": [op]}, data)
        for mode in ("on", "verbose"):
            actions = summarize_background_review_actions(messages, [], mode)
            assert actions and all(name in line and "?" not in line for line in actions)
        assert summarize_background_review_actions(messages, [], "off") == []
    assert not (tmp_path / "skills" / name).exists()


def test_unapplied_skill_operations_never_notify():
    args = {"operations": [{"name": "pending", "action": "create"}]}
    for data in (
        {"success": True, "staged": True, "message": "Write staged for approval."},
        {"success": False, "results": [{"success": True, "name": "pending", "action": "create"}]},
        {"success": True, "operations_applied": 0, "results": []},
        {"success": True, "operations_applied": 1, "results": [{"success": False, "name": "pending", "action": "create"}]},
    ):
        for mode in ("on", "verbose"):
            assert summarize_background_review_actions(_messages(args, data), [], mode) == []


def test_cross_skill_batch_names_each_applied_operation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    operations = [
        {"name": name, "action": "create", "content":
         f"---\nname: {name}\ndescription: Use when checking notices.\n---\nRead the sample.\n"}
        for name in ("notice-first", "notice-second")
    ] + [
        {"name": "notice-first", "action": "patch", "old_string": "sample", "new_string": "example"},
        {"name": "notice-second", "action": "write_file", "file_path": "references/a.md", "file_content": "A reference."},
    ]
    data = json.loads(skill_manage(action="", name="", operations=operations))
    assert data["success"], data
    expected = [
        "Skill 'notice-first' created",
        "Skill 'notice-second' created",
        "Skill 'notice-first' patched",
        "Skill 'notice-second' written (references/a.md)",
    ]
    messages = _messages({"operations": operations}, data)
    for mode in ("on", "verbose"):
        assert summarize_background_review_actions(messages, [], mode) == expected
    assert "example" in (tmp_path / "skills/notice-first/SKILL.md").read_text()
    assert (tmp_path / "skills/notice-second/references/a.md").read_text() == "A reference."


def test_notifications_use_successful_results_not_requested_operations():
    args = {"operations": [{"name": "requested-only", "action": "create"}]}
    data = {"success": True, "operations_applied": 1, "results": [
        {"name": "actually-applied", "action": "patch", "success": True},
        {"name": "not-applied", "action": "create", "success": False},
    ]}
    for mode in ("on", "verbose"):
        assert summarize_background_review_actions(_messages(args, data), [], mode) == [
            "Skill 'actually-applied' patched"
        ]
