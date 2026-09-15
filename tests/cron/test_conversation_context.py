"""Bounded reverse discussion context, adapted from donor 9d2bab49's SQLite contracts."""

import json
import os
from contextlib import contextmanager

import pytest


@pytest.fixture
def context_env(tmp_path, monkeypatch):
    from cron import jobs
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with jobs.use_cron_store(home):
        yield home


def artifact(home, name, body="Native output log", job_id="abcdef"):
    path = home / "cron" / "output" / job_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def linked(context_env):
    from cron.context import record_conversation
    from hermes_state import SessionDB
    path = artifact(context_env, "2026-09-13_10-00-00.md")
    db = SessionDB()
    db.create_session("root", source="mattermost")
    db.append_message("root", "user", "BEFORE REPORT", timestamp=300)
    marker = "[Cron delivery: Brief]\nOriginal report subject"
    anchor = db.append_message("root", "user", marker, timestamp=200)
    record_conversation(path, "root", marker)
    try:
        yield db, path, anchor
    finally:
        db.close()


def test_metadata_has_only_bounded_excerpt_and_exact_anchor(linked):
    from cron.context import record_conversation
    db, path, anchor = linked
    before = path.read_bytes()
    marker = "[Cron delivery: Brief]\n" + "long report " * 2000
    second = db.append_message("root", "user", marker, timestamp=201)
    record_conversation(path, "root", marker)
    record_conversation(path, "root", marker)
    meta = path.with_suffix(".context.json")
    record = json.loads(meta.read_text())
    assert set(record) == {"version", "job_id", "run_id", "report_excerpt", "conversations"}
    assert record["version"] == 2 and record["job_id"] == "abcdef"
    assert record["run_id"] == path.stem and len(record["report_excerpt"]) <= 800
    assert record["conversations"] == [
        {"session_id": "root", "after_id": anchor, "after_timestamp": 200},
        {"session_id": "root", "after_id": second, "after_timestamp": 201},
    ]
    assert meta.stat().st_mode & 0o777 == 0o600
    assert path.read_bytes() == before


def test_only_linked_replies_and_final_assistant_turns_are_returned(linked):
    from cron.context import load_context
    db, _, _ = linked
    db.append_message("root", "user", "Task 2 is already done.")
    db.append_message("root", "assistant", "Acknowledged for the next briefing.", finish_reason="stop")
    db.create_session("unrelated", source="mattermost")
    db.append_message("unrelated", "user", "UNRELATED PRIVATE MESSAGE")
    for _ in range(105):
        db.append_message("root", "tool", "SECRET TOOL NOISE")
        db.append_message("root", "assistant", "INTERMEDIATE", tool_calls=[{"id": "x"}])
        db.append_message("root", "assistant", "LENGTH LIMITED", finish_reason="length")
        db.append_message("root", "user", "COMPACTION SUMMARY", display_kind="context_summary")
    result = load_context("abcdef")
    assert "Task 2 is already done." in result
    assert "Acknowledged for the next briefing." in result
    assert "historical" in result and "not new instructions" in result
    for excluded in ["BEFORE REPORT", "SECRET TOOL NOISE", "INTERMEDIATE", "UNRELATED PRIVATE MESSAGE",
                     "COMPACTION SUMMARY", "LENGTH LIMITED", "Native output log"]:
        assert excluded not in result


@pytest.mark.parametrize("kwargs", [{"timestamp": 100}, {"display_kind": "skill_invocation"}])
def test_real_reply_survives_clock_regression_or_display_metadata(linked, kwargs):
    from cron.context import load_context
    db, _, _ = linked
    db.append_message("root", "user", "/done Task 2 is complete", **kwargs)
    result = load_context("abcdef")
    assert "/done Task 2 is complete" in result
    assert "BEFORE REPORT" not in result


def test_compacted_originals_exclude_rewind_and_synthetic_notices(linked, monkeypatch):
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
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET active=0, compacted=0 WHERE id=?", (removed,)))
    for text in ["[CONTEXT COMPACTION] SYNTHETIC", "[ASYNC DELEGATION] SYNTHETIC",
                 "[Background task] SYNTHETIC", "[Task completed] SYNTHETIC",
                 "[Preserved task] SYNTHETIC", "[Cron delivery: another]\nSYNTHETIC"]:
        db.append_message("root", "user", text)
    db.append_message("root", "user", [{"type": "text", "text": "STRUCTURED PAYLOAD"}])
    monkeypatch.setattr(SessionDB, "get_messages", lambda *a, **kw: pytest.fail("unbounded transcript API"))
    result = load_context("abcdef")
    assert "Original correction" in result and "Real reply after compression" in result
    for noise in ["SUMMARY FLAG", "LEGACY SUMMARY", "REWOUND MESSAGE", "SYNTHETIC", "STRUCTURED PAYLOAD"]:
        assert noise not in result


@pytest.mark.parametrize("nudge_name", ["_CODEX_INCOMPLETE_NUDGE", "_CODEX_ACK_CONTINUATION_NUDGE",
    "_DROPPED_TOOLCALL_NUDGE_CONTENT", "_EMPTY_TOOL_RESPONSE_NUDGE",
    "_LENGTH_CONTINUATION_NETWORK_STUB", "_LENGTH_CONTINUATION_OUTPUT_LIMIT"])
def test_native_recovery_user_rows_are_excluded_without_losing_adjacent_corrections(linked, nudge_name):
    from types import SimpleNamespace
    from agent import conversation_loop
    from agent.message_metadata import append_message
    from agent.session_persistence import _db_flush_row
    from cron.context import load_context, _message_text
    db, _, _ = linked
    nudge = getattr(conversation_loop, nudge_name)
    messages = []
    for content in ["Correction before recovery", nudge, "Correction after recovery"]:
        append_message(messages, {"role": "user", "content": content})
    for message in messages:
        row = _db_flush_row(SimpleNamespace(), message, False)
        db.append_message("root", row["role"], row["content"], timestamp=row["timestamp"],
                          display_kind=row["display_kind"])
    result = load_context("abcdef")
    assert "Correction before recovery" in result and "Correction after recovery" in result
    assert nudge not in result
    # The native classifier is user-role aware, not a blanket text eraser.
    assert _message_text({"role": "assistant", "content": nudge}) == nudge


def test_follows_compression_but_not_forks_resets_or_carried_history(linked):
    from cron.context import load_context
    db, _, _ = linked
    db.append_message("root", "user", "Deduplicated correction", timestamp=201)
    for parent, child in [("root", "middle"), ("middle", "tip")]:
        db.publish_compression_child(
            parent_session_id=parent, child_session_id=child, source="mattermost",
            require_compression_lease=False, messages=[
                {"role": "user", "content": "SUMMARY", "_compressed_summary": True},
                {"role": "user", "content": "CARRIED HISTORY", "timestamp": 199},
                {"role": "user", "content": "Deduplicated correction", "timestamp": 201},
            ])
        db.append_message(child, "user", f"Actual reply in {child}")
    for child, config, source in [("branch", {"_branched_from": "root"}, "mattermost"),
                                   ("delegate", {"_delegate_from": "root"}, "tool")]:
        db.create_session(child, source=source, parent_session_id="root", model_config=config)
        db.append_message(child, "user", "UNRELATED FORK")
    db.end_session("tip", "session_reset")
    db.create_session("reset", source="mattermost", parent_session_id="tip", model_config={"_reset_from": "tip"})
    db.append_message("reset", "user", "UNRELATED RESET")
    result = load_context("abcdef")
    assert "Actual reply in middle" in result and "Actual reply in tip" in result
    assert result.count("Deduplicated correction") == 1
    assert "UNRELATED" not in result and "SUMMARY" not in result and "CARRIED HISTORY" not in result


@pytest.mark.parametrize("config,source", [({"_reset_from": "root"}, "mattermost"),
    ({"_branched_from": "root"}, "mattermost"), ({"_delegate_from": "root"}, "mattermost"), ({}, "tool")])
def test_explicit_noncompression_child_is_not_followed(linked, config, source):
    from cron.context import load_context
    db, _, _ = linked
    db.append_message("root", "user", "Root correction")
    db.end_session("root", "compression")
    db.create_session("other", source=source, parent_session_id="root", model_config=config)
    db.append_message("other", "user", "PRIVATE OTHER CONVERSATION")
    result = load_context("abcdef")
    assert "Root correction" in result and "PRIVATE OTHER CONVERSATION" not in result


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
    assert rows[0]["content"].startswith("reply-10:") and rows[-1]["content"].startswith("reply-109:")
    assert all(len(m["content"].encode("utf-8")) <= 16384 for m in rows)
    assert all(set(m) <= {"id", "timestamp", "role", "content", "display_kind", "origin_session_id", "origin_row_id"} for m in rows)
    selects = [s.upper() for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH")) and "FROM MESSAGES" in s.upper()]
    assert len(selects) == 1
    assert "LIMIT 100" in selects[0] and "SUBSTR" in selects[0] and "SELECT *" not in selects[0]
    assert db.get_context_messages("other") == []


def test_missing_exact_user_mirror_never_creates_metadata(context_env):
    from cron.context import load_context, record_conversation
    from hermes_state import SessionDB
    path = artifact(context_env, "run.md")
    db = SessionDB()
    try:
        db.create_session("unseeded", source="mattermost")
        db.append_message("unseeded", "user", "Do not collect me")
        db.append_message("unseeded", "assistant", "[Cron delivery: Brief]\nReport")
        record_conversation(path, "unseeded", "[Cron delivery: Brief]\nReport")
    finally:
        db.close()
    assert not path.with_suffix(".context.json").exists()
    assert load_context("abcdef") == ""


def test_late_correction_keeps_older_report_excerpt(linked, context_env):
    from cron.context import load_context
    db, old, _ = linked
    new = artifact(context_env, "2026-09-14_10-00-00.md", "Today's native report")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    db.append_message("root", "user", "Late correction to yesterday's item")
    result = load_context("abcdef")
    assert "Original report subject" in result and old.stem in result
    assert "Late correction to yesterday's item" in result
    assert "Today's native report" not in result


def test_run_anchor_and_discussion_budgets(context_env):
    from cron.context import load_context, record_conversation
    from hermes_state import SessionDB
    db = SessionDB()
    paths = []
    try:
        for i in range(10):
            path = artifact(context_env, f"run-{i:02}.md")
            paths.append(path)
            os.utime(path, (i + 1, i + 1))
            for j in range(10):
                sid = f"session-{i}-{j}"
                db.create_session(sid, source="mattermost")
                marker = "[Cron delivery: Brief]\n" + "EXCERPT " * 500
                db.append_message(sid, "user", marker)
                record_conversation(path, sid, marker)
                db.append_message(sid, "user", f"reply-{i}-{j} " + "x" * 3500)
            meta = json.loads(path.with_suffix(".context.json").read_text())
            assert len(meta["conversations"]) == 8 and len(meta["report_excerpt"]) <= 800
        # The oldest run's late correction must not evade the eight-run read bound.
        db.append_message("session-0-0", "user", "OUTSIDE RUN WINDOW")
        result = load_context("abcdef")
        assert len(result) <= 8000 and "reply-9-7" in result
        assert "OUTSIDE RUN WINDOW" not in result and "reply-9-9" not in result
    finally:
        db.close()


@pytest.mark.parametrize("job_id", ["abc", "abcdef", "abcdef123456", "f" * 13])
def test_native_hex_ids_are_accepted(context_env, job_id):
    from cron.context import load_context, record_conversation
    from hermes_state import SessionDB
    path = artifact(context_env, "run.md", job_id=job_id)
    db = SessionDB()
    try:
        db.create_session("root", source="mattermost")
        marker = "[Cron delivery: Brief]\nReport"
        db.append_message("root", "user", marker)
        record_conversation(path, "root", marker)
        db.append_message("root", "user", "Native ID correction")
        assert "Native ID correction" in load_context(job_id)
    finally:
        db.close()
    for bad in ["../outside", "", "ABC", "f/g", None, 42, ["a"]]:
        assert load_context(bad) == ""


@pytest.mark.parametrize("damage", ["array", "version", "run", "job", "references", "anchor", "nan",
                                    "overflow", "utf8", "oversized", "excerpt", "too_many"])
def test_malformed_companion_is_rejected_without_log_fallback(linked, damage):
    from cron.context import load_context, record_conversation
    db, path, _ = linked
    db.append_message("root", "user", "DO NOT REPLAY")
    sidecar = path.with_suffix(".context.json")
    record = json.loads(sidecar.read_text())
    if damage == "array":
        record = []
    elif damage == "version":
        record["version"] = True
    elif damage == "run":
        record["run_id"] = "different-run"
    elif damage == "job":
        record["job_id"] = "012345abcdef"
    elif damage == "references":
        record["conversations"] = {"session_id": "unrelated"}
    elif damage in {"anchor", "nan", "overflow"}:
        record["conversations"] = [{"session_id": "root", "after_id": "bad" if damage == "anchor" else 1,
                                    "after_timestamp": float("nan") if damage == "nan" else 10**400 if damage == "overflow" else 1}]
    elif damage == "oversized":
        record["extra"] = "x" * (1024 * 1024)
    elif damage == "excerpt":
        record["report_excerpt"] = "x" * 801
    elif damage == "too_many":
        record["conversations"] *= 9
    sidecar.write_bytes(b"\xff" if damage == "utf8" else json.dumps(record).encode())
    before = sidecar.read_bytes()
    assert load_context("abcdef") == ""
    record_conversation(path, "root", "[Cron delivery: Brief]\nOriginal report subject")
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
