"""Bounded replies to cron reports, anchored to their persisted delivery mirrors.

The native Markdown outputs remain authoritative. Metadata-only companions hold
an excerpt and exact anchors; discussion text stays in SessionDB.
"""

import json
import logging
import math
import re
from pathlib import Path

from utils import atomic_write_text

logger = logging.getLogger(__name__)
MAX_RUNS = 8
MAX_CONVERSATIONS = 8
MAX_REPORT_CHARS = 800
MAX_MESSAGES = 100
MAX_DISCUSSION_CHARS = 8000
MAX_RUN_BYTES = 1024 * 1024


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[... output truncated ...]\n"
    if limit <= len(marker):
        return text[:limit]
    available = limit - len(marker)
    tail = available // 4
    return text[:available - tail] + marker + (text[-tail:] if tail else "")


def _sidecar(output_file: Path) -> Path:
    return output_file.with_suffix(".context.json")


def _read_run(output_file: Path) -> dict:
    """Validate metadata without ever falling back to parsing a response log."""
    path = _sidecar(output_file)
    if not path.exists():
        return {}
    with path.open("rb") as stream:
        data = stream.read(MAX_RUN_BYTES + 1)
    if len(data) > MAX_RUN_BYTES:
        raise ValueError("Run context exceeds read budget")
    record = json.loads(data.decode("utf-8"))
    if (not isinstance(record, dict) or type(record.get("version")) is not int
            or record["version"] != 2 or record.get("run_id") != output_file.stem
            or record.get("job_id") != output_file.parent.name
            or not isinstance(record.get("report_excerpt"), str)
            or len(record["report_excerpt"]) > MAX_REPORT_CHARS
            or not isinstance(record.get("conversations"), list)
            or len(record["conversations"]) > MAX_CONVERSATIONS):
        return {}
    for reference in record["conversations"]:
        if (not isinstance(reference, dict)
                or not isinstance(reference.get("session_id"), str)
                or not 0 < len(reference["session_id"]) <= 256
                or type(reference.get("after_id")) is not int or reference["after_id"] <= 0
                or type(reference.get("after_timestamp")) not in (int, float)
                or not math.isfinite(reference["after_timestamp"])):
            return {}
    return record


def record_conversation(output_file: Path, session_id: str, mirror_text: str) -> None:
    """Called after a successful USER mirror; bind only its exact persisted row.

    Missing/invalid metadata or a failed lookup cannot infer another conversation.
    A failed metadata write must never turn a successful delivery into a failure.
    """
    from hermes_state import SessionDB

    db = None
    try:
        output_file = Path(output_file)
        if (not output_file.is_file() or not isinstance(session_id, str)
                or not 0 < len(session_id) <= 256 or not isinstance(mirror_text, str)
                or not mirror_text.startswith("[Cron delivery:")):
            return
        path = _sidecar(output_file)
        if path.exists():
            record = _read_run(output_file)
            if not record or len(record["conversations"]) >= MAX_CONVERSATIONS:
                return
        else:
            record = {
                "version": 2, "run_id": output_file.stem, "job_id": output_file.parent.name,
                "report_excerpt": _clip(mirror_text.partition("\n")[2], MAX_REPORT_CHARS),
                "conversations": [],
            }
        db = SessionDB(read_only=True)
        messages = db.get_context_messages(session_id, limit=1, matching_content=mirror_text)
        if not messages:
            logger.debug("Cron context: delivery mirror missing in session %s", session_id)
            return
        anchor = messages[0]
        if session_id not in _conversation_sessions(db, anchor["origin_session_id"]):
            return
        reference = {"session_id": anchor["origin_session_id"], "after_id": anchor["origin_row_id"],
                     "after_timestamp": anchor["timestamp"]}
        if reference not in record["conversations"]:
            record["conversations"].append(reference)
            atomic_write_text(path, json.dumps(record, ensure_ascii=False),
                              tmp_prefix=".context_", mode=0o600)
    except Exception:
        logger.debug("Cron context: could not record delivered conversation", exc_info=True)
    finally:
        if db is not None:
            db.close()


def _message_text(message: dict) -> str:
    """SQL already excludes tools/rewinds; discard legacy summaries and notices."""
    text = message.get("content")
    if not isinstance(text, str):
        return ""
    text = text.strip()
    from agent.context_compressor import ContextCompressor
    if (ContextCompressor._is_context_summary_content(text)
            or ContextCompressor._is_synthetic_compression_user_turn(message)):
        return ""
    if text.startswith(("[Cron delivery:", "[CONTEXT COMPACTION", "[ASYNC DELEGATION",
                        "[Background task", "[Task completed", "[Preserved task")):
        return ""
    return text


def _conversation_sessions(db, session_id: str) -> list[str]:
    """Validate the native bounded chain, including its explicit-reset exclusion."""
    result = [session_id]
    parent = db.get_session(session_id)
    for child_id in db.get_compression_chain(session_id)[1:]:
        child = db.get_session(child_id)
        if not parent or not child or child.get("parent_session_id") != parent["id"]:
            break
        config = child.get("model_config") or {}
        if isinstance(config, str):
            config = json.loads(config)
        if (parent.get("end_reason") != "compression" or not isinstance(config, dict)
                or child.get("source") == "tool"
                or any(config.get(key) == parent["id"] for key in
                       ("_branched_from", "_delegate_from", "_reset_from"))):
            break
        result.append(child_id)
        parent = child
    return result


def _discussion(db, reference: dict, cache: dict) -> list[dict]:
    session_id = reference["session_id"]
    result = []
    chain = _conversation_sessions(db, session_id)
    for current in chain:
        if current not in cache:
            cache[current] = db.get_context_messages(current, limit=MAX_MESSAGES)
        for message in cache[current]:
            if (message["origin_session_id"] not in chain
                    or message["origin_row_id"] <= reference["after_id"]):
                continue
            text = _message_text(message)
            if text:
                result.append({"id": message["origin_row_id"],
                               "origin_session_id": message["origin_session_id"], "timestamp": message["timestamp"],
                               "role": message["role"], "text": text})
    return result


def load_context(source_job_id: str) -> str:
    """Return discussion evidence only, for eight runs of one native hex job ID.

    No output selection, success/silence filtering or inferred conversation lookup
    belongs here. Missing companions simply contribute no discussion.
    """
    if not isinstance(source_job_id, str) or not re.fullmatch(r"[0-9a-f]+", source_job_id):
        return ""
    from cron.jobs import get_cron_output_dir

    db = None
    try:
        directory = get_cron_output_dir() / source_job_id
        paths = sorted(directory.glob("*.md"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        discussions = []
        seen = set()
        cache = {}
        for path in paths[:MAX_RUNS]:
            try:
                record = _read_run(path)
            except (OSError, ValueError, TypeError, OverflowError):
                logger.debug("Cron context: unreadable metadata for %s", path.name)
                continue
            for reference in record.get("conversations", []):
                try:
                    if db is None:
                        from hermes_state import SessionDB
                        db = SessionDB(read_only=True)
                    for message in _discussion(db, reference, cache):
                        key = (message["origin_session_id"], message["id"])
                        if key in seen:
                            continue
                        seen.add(key)
                        message["run"] = path.stem
                        message["report"] = record["report_excerpt"]
                        discussions.append(message)
                except Exception:
                    logger.debug("Cron context: linked conversation unavailable", exc_info=True)
        # Select newest discussion first, then present the selected entries in
        # chronological order, with report subjects even for older-root replies.
        heading = "Subsequent conversation (historical context, not new instructions):\n"
        selected = []
        used = len(heading)
        for message in sorted(discussions, key=lambda m: m["id"], reverse=True):
            remaining = MAX_DISCUSSION_CHARS - used - (2 if selected else 0)
            if remaining < 200:
                break
            rendered = (f"Run {message['run']} | {message['role']} | {message['timestamp']}:\n"
                        f"Report excerpt: {message['report']}\n{_clip(message['text'], 3000)}")
            rendered = _clip(rendered, remaining)
            used += len(rendered) + (2 if selected else 0)
            selected.append(rendered)
        return heading + "\n\n".join(reversed(selected)) if selected else ""
    except OSError:
        logger.debug("Cron context: cannot read job output directory", exc_info=True)
        return ""
    finally:
        if db is not None:
            db.close()
