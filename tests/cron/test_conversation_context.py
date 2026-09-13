"""Final-output and exact-conversation context for scheduled jobs."""

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def context_env(tmp_path, monkeypatch):
    import cron.jobs as jobs
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(jobs, "OUTPUT_DIR", home / "cron" / "output")
    return home


def artifact(home, name, body):
    path = home / "cron" / "output" / "abcdef123456" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_build_prompt_uses_final_response_instead_of_nested_log(context_env):
    from cron.context import save_context
    from cron.scheduler import _build_job_prompt
    path = artifact(context_env, "2026-09-13_10-00-00.md", "Old prompt noise " * 1000)
    save_context(path, "Useful final response", True)
    prompt = _build_job_prompt({"id": "abcdef123456", "prompt": "Current task", "context_from": ["self"]})
    assert "Useful final response" in prompt
    assert "Old prompt noise" not in prompt
    assert "Current task" in prompt


def test_legacy_context_extracts_final_response_after_large_prompt(context_env):
    from cron.context import load_context
    artifact(context_env, "2026-09-13_10-00-00.md",
             "# Cron Job: Brief\n\n## Prompt\n\n" + "Old collector data. " * 900
             + "\n\n## Response\n\nActual final report.")
    result = load_context("abcdef123456")
    assert "Actual final report." in result
    assert "Old collector data" not in result


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_ambiguous_legacy_response_preserves_complete_bounded_record(context_env, newline):
    from cron.context import load_context
    text = "# Cron Job: Brief\n\n## Response\nComplete beginning.\n\n## Response\nNested heading tail."
    artifact(context_env, "2026-09-13_10-00-00.md", text.replace("\n", newline))
    result = load_context("abcdef123456")
    assert "Complete beginning." in result
    assert "Nested heading tail." in result


def test_oversized_ambiguous_legacy_record_does_not_guess_response_boundary(context_env):
    from cron.context import load_context, MAX_RESPONSE_CHARS
    artifact(context_env, "2026-09-13_10-00-00.md",
             "# Cron Job: Brief\n\n## Response\n" + "x" * MAX_RESPONSE_CHARS
             + "\n\n## Response\nNot an independent final response.")
    assert load_context("abcdef123456") == ""


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_unavailable_run_during_sort_does_not_discard_other_final_responses(context_env, monkeypatch, error):
    from cron.context import load_context, save_context
    good = artifact(context_env, "2026-09-13_10-00-00.md", "log")
    save_context(good, "Committed final response", True)
    unavailable = artifact(context_env, "2026-09-13_11-00-00.md", "unavailable run")
    stat = Path.stat

    def unavailable_stat(path, *args, **kwargs):
        if path == unavailable:
            raise error("run unavailable during pruning")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", unavailable_stat)
    assert "Committed final response" in load_context("abcdef123456")


def test_new_output_keeps_response_headings_and_skips_silent_or_failed_runs(context_env):
    from cron.context import load_context, save_context
    good = artifact(context_env, "2026-09-13_10-00-00.md", "log")
    save_context(good, "Report.\n\n## Response\n\nHeading inside answer.", True)
    for name, text, success in [("11", "[SILENT]", True), ("12", "Failed run", False)]:
        path = artifact(context_env, f"2026-09-13_{name}-00-00.md", "log")
        save_context(path, text, success)
    result = load_context("abcdef123456")
    assert "Report." in result and "Heading inside answer." in result
    assert "Failed run" not in result and "[SILENT]" not in result


def test_legacy_failed_log_never_replays_nested_previous_response(context_env):
    from cron.context import load_context
    artifact(context_env, "2026-09-13_10-00-00.md",
             "# Cron Job: Brief (FAILED)\n\n## Prompt\n\nOld ## Response\n\nwrong\n\n## Error\n\nfailed")
    assert not load_context("abcdef123456")


def test_context_reads_only_linked_discussion_and_excludes_tool_noise(context_env):
    from cron.context import load_context, save_context, record_conversation
    from hermes_state import SessionDB
    path = artifact(context_env, "2026-09-13_10-00-00.md", "log")
    save_context(path, "Task 2: respond to the invitation.", True)
    db = SessionDB()
    try:
        db.create_session("linked-session", source="mattermost", chat_id="fixture-dm", chat_type="dm",
                          session_key="agent:main:mattermost:dm:fixture-dm")
        db.append_message("linked-session", "user", "Before this report")
        marker = "[Cron delivery: Brief]\nTask 2: respond to the invitation."
        db.append_message("linked-session", "user", marker)
        record_conversation(path, "linked-session", marker)
        db.append_message("linked-session", "user", "[owner] Task 2 is already done.")
        db.append_message("linked-session", "assistant", "Acknowledged for the next briefing.", finish_reason="stop")
        db.append_message("linked-session", "tool", "SECRET TOOL NOISE")
        db.append_message("linked-session", "assistant", "INTERMEDIATE", tool_calls=[{"id": "x", "function": {"name": "foo", "arguments": "{}"}}])
        db.append_message("linked-session", "user", "COMPACTION SUMMARY", display_kind="context_summary")
        db.create_session("unrelated-session", source="mattermost")
        db.append_message("unrelated-session", "user", "UNRELATED PRIVATE MESSAGE")
    finally:
        db.close()
    result = load_context("abcdef123456")
    assert "Task 2 is already done." in result
    assert "Acknowledged for the next briefing." in result
    for excluded in ["Before this report", "SECRET TOOL NOISE", "INTERMEDIATE", "UNRELATED PRIVATE MESSAGE", "COMPACTION SUMMARY"]:
        assert excluded not in result


def test_no_discussion_reference_without_real_mirror_record(context_env):
    from cron.context import load_context, save_context, record_conversation
    from hermes_state import SessionDB
    path = artifact(context_env, "2026-09-13_10-00-00.md", "log")
    save_context(path, "Report", True)
    db = SessionDB()
    try:
        db.create_session("unseeded", source="mattermost")
        db.append_message("unseeded", "user", "Do not collect me")
    finally:
        db.close()
    record_conversation(path, "unseeded", "[Cron delivery: missing]\nReport")
    assert "Do not collect me" not in load_context("abcdef123456")


def test_later_replies_to_previous_report_root_still_appear(context_env):
    from cron.context import load_context, save_context, record_conversation
    from hermes_state import SessionDB
    old = artifact(context_env, "2026-09-12_10-00-00.md", "log")
    save_context(old, "Older task report", True)
    db = SessionDB()
    try:
        db.create_session("older-thread", source="mattermost", chat_id="fixture-dm", chat_type="dm",
                          session_key="agent:main:mattermost:dm:fixture-dm")
        marker = "[Cron delivery: Brief]\nOlder task report"
        db.append_message("older-thread", "user", marker)
        record_conversation(old, "older-thread", marker)
        new = artifact(context_env, "2026-09-13_10-00-00.md", "log")
        save_context(new, "Today's report", True)
        db.append_message("older-thread", "user", "Late correction to yesterday's item")
    finally:
        db.close()
    result = load_context("abcdef123456")
    assert "Today's report" in result
    assert "Older task report" in result
    assert "Late correction to yesterday's item" in result


def test_context_bounds_and_path_validation(context_env):
    from cron.context import load_context, save_context
    path = artifact(context_env, "2026-09-13_10-00-00.md", "log")
    save_context(path, "z" * 40000, True)
    result = load_context("abcdef123456")
    assert len(result) <= 17000
    assert "truncated" in result
    for bad in ["../outside", "abc", "f" * 13, None, 42]:
        assert load_context(bad) == ""
    assert path.with_suffix(".context.json").stat().st_mode & 0o777 == 0o600


@pytest.fixture
def linked(context_env):
    from cron.context import save_context, record_conversation
    from hermes_state import SessionDB
    path = artifact(context_env, "2026-09-13_10-00-00.md", "log")
    save_context(path, "Original report subject", True)
    db = SessionDB()
    db.create_session("root", source="mattermost", chat_id="fixture-dm", chat_type="dm",
                          session_key="agent:main:mattermost:dm:fixture-dm")
    db.append_message("root", "user", "BEFORE REPORT", timestamp=300)
    marker = "[Cron delivery: Brief]\nOriginal report subject"
    anchor = db.append_message("root", "user", marker, timestamp=200)
    record_conversation(path, "root", marker)
    try:
        yield db, path, anchor
    finally:
        db.close()


@pytest.mark.parametrize("kwargs", [{"timestamp": 100}, {"display_kind": "skill_invocation"}])
def test_real_reply_survives_clock_regression_or_display_metadata(linked, kwargs):
    from cron.context import load_context
    db, _, _ = linked
    db.append_message("root", "user", "/done Task 2 is complete", **kwargs)
    result = load_context("abcdef123456")
    assert "/done Task 2 is complete" in result
    assert "BEFORE REPORT" not in result


def test_compacted_originals_and_new_reply_without_unbounded_transcript_reads(linked, monkeypatch):
    from cron.context import load_context
    from hermes_state import SessionDB
    db, _, _ = linked
    db.append_message("root", "user", "Original correction", timestamp=201)
    db.archive_and_compact("root", [
        {"role": "user", "content": "SUMMARY FLAG", "_compressed_summary": True},
        {"role": "user", "content": "[CONTEXT SUMMARY]: LEGACY SUMMARY"},
    ])
    db.append_message("root", "user", "Real reply after compression")
    removed = db.append_message("root", "user", "REWOUND MESSAGE")
    db._conn.execute("UPDATE messages SET active=0, compacted=0 WHERE id=?", (removed,))
    db._conn.commit()
    for _ in range(105):
        db.append_message("root", "tool", "TOOL DATA")
        db.append_message("root", "assistant", "TOOL CALL DATA", tool_calls=[{"id": "call"}])
        db.append_message("root", "user", "SUMMARY DISPLAY", display_kind="context_summary")
    monkeypatch.setattr(SessionDB, "get_messages", lambda *a, **kw: pytest.fail("unbounded transcript API"))
    result = load_context("abcdef123456")
    assert "Original correction" in result
    assert "Real reply after compression" in result
    for noise in ["SUMMARY FLAG", "LEGACY SUMMARY", "REWOUND MESSAGE", "TOOL DATA", "TOOL CALL DATA", "SUMMARY DISPLAY"]:
        assert noise not in result


def test_follows_intermediate_compression_sessions_but_not_forks_or_resets(linked):
    from cron.context import load_context
    db, _, _ = linked
    for parent, child in [("root", "middle"), ("middle", "tip")]:
        db.publish_compression_child(parent_session_id=parent, child_session_id=child,
                                     source="mattermost", require_compression_lease=False,
                                     messages=[{"role": "user", "content": "SUMMARY", "_compressed_summary": True}])
        db.append_message(child, "user", f"Actual reply in {child}")
    for child, config, source in [("branch", {"_branched_from": "root"}, "mattermost"),
                                   ("delegate", {"_delegate_from": "root"}, "tool")]:
        db.create_session(child, source=source, parent_session_id="root", model_config=config)
        db.append_message(child, "user", "UNRELATED FORK")
    db.end_session("tip", "session_reset")
    db.create_session("reset", source="mattermost", parent_session_id="tip", model_config={"_reset_from": "tip"})
    db.append_message("reset", "user", "UNRELATED RESET")
    result = load_context("abcdef123456")
    assert "Actual reply in middle" in result
    assert "Actual reply in tip" in result
    assert "UNRELATED" not in result
    assert "SUMMARY" not in result


def test_explicit_reset_child_is_not_a_compression_continuation(linked):
    from cron.context import load_context
    db, _, _ = linked
    db.append_message("root", "user", "Root correction")
    db.end_session("root", "compression")
    db.create_session("reset", source="mattermost", parent_session_id="root", model_config={"_reset_from": "root"})
    db.append_message("reset", "user", "RESET PRIVATE MESSAGE")
    result = load_context("abcdef123456")
    assert "Root correction" in result
    assert "RESET PRIVATE MESSAGE" not in result


def test_context_query_limits_rows_and_bytes_inside_sql(linked, monkeypatch):
    db, _, anchor = linked
    for i in range(110):
        db.append_message("root", "user", f"reply-{i}:" + "\U0001f600" * 6000,
                          api_content="PRIVATE API CONTENT", display_metadata={"private": "metadata"})
    statements = []
    read_ctx = db._read_ctx

    @contextmanager
    def traced_read():
        with read_ctx() as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(db, "_read_ctx", traced_read)
    rows = db.get_context_messages("root", limit=10000, after_id=anchor)
    assert len(rows) == 100
    assert rows[0]["content"].startswith("reply-10:")
    assert rows[-1]["content"].startswith("reply-109:")
    assert all(len(m["content"].encode("utf-8")) <= 16384 for m in rows)
    assert all(set(m) <= {"id", "timestamp", "role", "content", "display_kind", "origin_session_id", "origin_row_id"} for m in rows)
    selects = [s.upper() for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH")) and "FROM MESSAGES" in s.upper()]
    assert len(selects) == 1
    assert "LIMIT 100" in selects[0] and "SUBSTR" in selects[0] and "SELECT *" not in selects[0]
    assert db.get_context_messages("other") == []


@pytest.mark.parametrize("damage", ["array", "version", "run", "job", "references", "anchor", "nan", "overflow", "utf8", "oversized"])
def test_malformed_sidecar_is_skipped_without_replaying_its_log(context_env, damage):
    from cron.context import load_context, save_context, record_conversation
    old = artifact(context_env, "2026-09-12_10-00-00.md", "log")
    save_context(old, "Last valid report", True)
    path = artifact(context_env, "2026-09-13_10-00-00.md", "POISON LOG")
    save_context(path, "POISON REPORT", True)
    sidecar = path.with_suffix(".context.json")
    record = json.loads(sidecar.read_text())
    if damage == "array":
        record = []
    elif damage == "version":
        record["version"] = 999
    elif damage == "run":
        record["run_id"] = "different-run"
    elif damage == "job":
        record["job_id"] = "012345abcdef"
    elif damage == "references":
        record["conversations"] = {"session_id": "unrelated"}
    elif damage in {"anchor", "nan", "overflow"}:
        record["conversations"] = [{"session_id": "unrelated", "after_id": "bad" if damage == "anchor" else 1,
                                    "after_timestamp": float("nan") if damage == "nan" else 10**400 if damage == "overflow" else 1}]
    elif damage == "oversized":
        record["response"] += "x" * (1024 * 1024)
    sidecar.write_bytes(b"\xff" if damage == "utf8" else json.dumps(record).encode())
    before = sidecar.read_bytes()
    assert "Last valid report" in load_context("abcdef123456")
    assert "POISON" not in load_context("abcdef123456")
    record_conversation(path, "unrelated", "missing")
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("mode", ["in_place", "rotation"])
def test_bounded_provenance_uses_original_order_and_content(linked, mode):
    from cron.context import load_context
    db, path, anchor = linked
    correction = db.append_message("root", "user", "Provable correction", timestamp=100)
    handoff = db.get_messages_as_conversation("root", include_row_ids=True)
    watermark = db.get_active_message_watermark("root")
    db.append_message("root", "user", "Concurrent correction", timestamp=50)
    # A merged carrier must never export its new summary-bearing payload.
    handoff[-1]["content"] = "SECRET MERGED SUMMARY plus Provable correction"
    handoff.append({"role": "user", "content": "UNPERSISTED INPUT", "timestamp": 400,
                    "display_metadata": {"_compression_origin": {
                        "version": 1, "session_id": "root", "row_id": correction}}})
    if mode == "in_place":
        db.archive_and_compact("root", handoff, watermark=watermark, tail_count=1)
        current = "root"
    else:
        db.publish_compression_child(parent_session_id="root", child_session_id="child",
            source="mattermost", require_compression_lease=False, messages=handoff,
            watermark=watermark)
        current = "child"
    # Repeat through another native generation to exercise flattened provenance.
    copied = db.get_messages_as_conversation(current, include_row_ids=True)
    db.archive_and_compact(current, copied, tail_count=len(copied))
    db.append_message(current, "user", "Later ordinary reply", timestamp=1)
    result = load_context(path.parent.name)
    assert "BEFORE REPORT" not in result
    assert "SECRET MERGED SUMMARY" not in result
    assert "UNPERSISTED INPUT" not in result  # Deliberate bounded-coverage omission.
    for text in ("Provable correction", "Concurrent correction", "Later ordinary reply"):
        assert result.count(text) == 1
    assert result.index("Provable correction") < result.index("Concurrent correction") < result.index("Later ordinary reply")


def test_bounded_provenance_rewound_carrier_does_not_authorize_original(linked):
    from cron.context import load_context
    db, path, _ = linked
    original = db.append_message("root", "user", "WITHDRAWN correction")
    handoff = db.get_messages_as_conversation("root", include_row_ids=True)
    db.archive_and_compact("root", handoff, tail_count=1)
    carrier = handoff[-1]["_row_id"]
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET active=0, compacted=0 WHERE id=?", (carrier,)))
    assert "WITHDRAWN" not in load_context(path.parent.name)


@pytest.mark.parametrize("damage", ["unproved_child", "legacy_companion", "malformed", "foreign", "double_encoded"])
def test_bounded_provenance_unknown_history_fails_closed(linked, damage):
    from cron.context import load_context
    db, path, _ = linked
    if damage == "legacy_companion":
        sidecar = path.with_suffix(".context.json")
        record = json.loads(sidecar.read_text())
        record["version"] = 1
        sidecar.write_text(json.dumps(record))
        db.append_message("root", "user", "PRIVATE UNPROVED")
    elif damage == "unproved_child":
        db.end_session("root", "compression")
        db.create_session("child", source="mattermost", parent_session_id="root")
        db.append_message("child", "user", "PRIVATE UNPROVED", timestamp=500)
    else:
        db.create_session("foreign", source="mattermost")
        foreign = db.append_message("foreign", "user", "PRIVATE FOREIGN")
        meta = {"_compression_origin": {"version": 1, "session_id": "foreign", "row_id": foreign}}
        if damage == "malformed":
            meta["_compression_origin"]["row_id"] = True
        row = db.append_message("root", "user", "PRIVATE UNPROVED", display_metadata=meta)
        if damage == "double_encoded":
            db._execute_write(lambda conn: conn.execute(
                "UPDATE messages SET display_metadata=? WHERE id=?", (json.dumps(json.dumps(meta)), row)))
    assert "PRIVATE" not in load_context(path.parent.name)


def test_bounded_provenance_copied_mirror_keeps_original_anchor(linked):
    from cron.context import record_conversation
    db, path, anchor = linked
    handoff = db.get_messages_as_conversation("root", include_row_ids=True)
    db.archive_and_compact("root", handoff)
    record_conversation(path, "root", "[Cron delivery: Brief]\nOriginal report subject")
    record = json.loads(path.with_suffix(".context.json").read_text())
    assert record["version"] == 2
    assert [r["after_id"] for r in record["conversations"]] == [anchor]


def test_same_clock_runs_keep_separate_reports_and_anchors(context_env, monkeypatch):
    from datetime import datetime, timezone
    from cron import jobs, context
    from hermes_state import SessionDB
    monkeypatch.setattr(jobs, "_hermes_now", lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    db = SessionDB()
    paths = []
    try:
        for number in range(2):
            sid = f"report-{number}"
            db.create_session(sid, source="mattermost", chat_id="fixture-dm", chat_type="dm",
                              session_key="agent:main:mattermost:dm:fixture-dm")
            marker = f"[Cron delivery: Brief]\nReport {number}"
            db.append_message(sid, "user", marker)
            path = jobs.save_job_output("abcdef123456", f"Native log {number}")
            if hasattr(context, "save_context"):
                context.save_context(path, f"Report {number}", True)
            context.record_conversation(path, sid, marker)
            paths.append(path)
        assert paths[0] != paths[1]
        for number, path in enumerate(paths):
            assert path.read_text() == f"Native log {number}"
            record = json.loads(path.with_suffix(".context.json").read_text())
            assert [ref["session_id"] for ref in record["conversations"]] == [f"report-{number}"]
            assert f"Report {number}" in record.get("report_excerpt", record.get("response", ""))
    finally:
        db.close()


def test_legacy_companion_keeps_final_report_without_discussion(linked):
    from cron.context import load_context, record_conversation
    db, path, _ = linked
    sidecar = path.with_suffix(".context.json")
    record = json.loads(sidecar.read_text())
    record["version"] = 1
    sidecar.write_text(json.dumps(record))
    db.append_message("root", "user", "UNPROVABLE legacy discussion")
    result = load_context(path.parent.name)
    assert "Original report subject" in result
    assert "UNPROVABLE" not in result
    before = sidecar.read_bytes()
    record_conversation(path, "root", "[Cron delivery: Brief]\nOriginal report subject")
    assert sidecar.read_bytes() == before
