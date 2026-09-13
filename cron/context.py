"""Bounded cron context: final outputs and replies in delivered conversations.

Sidecars live beside existing run logs and contain only final text and session
references. Conversation text remains in SessionDB; no second journal is kept.
"""

import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)
MAX_RUNS = 8
MAX_RESPONSE_CHARS = 8000
MAX_DISCUSSION_CHARS = 8000
MAX_MESSAGES = 100
MAX_CONVERSATIONS = 8
MAX_RUN_BYTES = 1024 * 1024


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[... output truncated ...]\n"
    available = limit - len(marker)
    tail = available // 4
    return text[:available - tail] + marker + text[-tail:]


def _sidecar(output_file: Path) -> Path:
    return Path(output_file).with_suffix(".context.json")


def _write(path: Path, record: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".context-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_context(output_file: Path, response: str, success: bool) -> None:
    """Save the exact final response independently of nested Markdown headings."""
    output_file = Path(output_file)
    if not output_file.is_file():
        return
    try:
        _write(_sidecar(output_file), {
            "version": 2, "run_id": output_file.stem, "job_id": output_file.parent.name,
            "success": bool(success),
            "response": response, "conversations": [],
        })
    except OSError:
        logger.warning("Could not save final-output context for %s", output_file)


def _source_identity(source, config) -> dict:
    """Resolve only identities the native reply key can unambiguously own."""
    from gateway.session import build_session_key
    from hermes_cli.profiles import get_active_profile_name

    owner = get_active_profile_name()
    if (not isinstance(owner, str) or not owner
            or (source.profile and source.profile != owner)
            or source.profile_route_rejected
            or source.chat_type not in {"dm", "group", "channel", "thread"}
            or not source.chat_id):
        return {}
    participant = source.user_id_alt or source.user_id
    isolated = (source.chat_type != "dm" and config.group_sessions_per_user
                and (not source.thread_id or config.thread_sessions_per_user))
    if isolated and (not participant or str(participant).startswith("system:")):
        return {}
    chat_id = str(source.chat_id)
    if source.platform.value == "whatsapp":
        from gateway.whatsapp_identity import canonical_whatsapp_identifier
        if source.chat_type == "dm":
            chat_id = canonical_whatsapp_identifier(chat_id)
        if participant:
            participant = canonical_whatsapp_identifier(str(participant)) or participant
    return {
        "platform": source.platform.value, "chat_id": chat_id,
        "chat_type": source.chat_type, "thread_id": str(source.thread_id or ""),
        "scope_id": str(source.scope_id or ""), "profile": owner,
        "participant": str(participant) if isolated else "",
        "session_key": build_session_key(
            source, group_sessions_per_user=config.group_sessions_per_user,
            thread_sessions_per_user=config.thread_sessions_per_user,
            profile=owner if config.multiplex_profiles else None),
    }


def _canonical_conversation_key(key, source, config):
    """Canonicalize known native legacy forms using verified origin metadata."""
    from dataclasses import replace
    from gateway.session import build_session_key

    identity = _source_identity(source, config)
    if not identity or not isinstance(key, str):
        return None
    canonical = identity["session_key"]
    variants = {canonical}
    kwargs: dict = dict(group_sessions_per_user=config.group_sessions_per_user,
                  thread_sessions_per_user=config.thread_sessions_per_user,
                  profile=identity["profile"] if config.multiplex_profiles else None)
    # Old DM keys omitted the chat. Never accept one without a verified private
    # chat above, or use it to equate two different private destinations.
    if source.chat_type == "dm":
        variants.add(build_session_key(replace(source, chat_id="", user_id=None, user_id_alt=None), **kwargs))
    if source.platform.value == "slack" and source.scope_id:
        variants.add(build_session_key(replace(source, scope_id=None, guild_id=None), **kwargs))
    if source.platform.value == "whatsapp" and source.chat_type == "dm":
        # Pre-canonical private keys retain the original phone/JID spelling.
        # Accept only the exact spelling from this row's verified origin.
        prefix = canonical.split(":")[:4]
        variants.add(":".join(prefix + [str(source.chat_id)]
                              + ([str(source.thread_id)] if source.thread_id else [])))
    return canonical if key in variants else None


def conversation_identity(row, *, source=None, config=None) -> dict:
    """Validate typed origin, persisted peer columns and native key together."""
    from gateway.config import load_gateway_config
    from gateway.session import SessionSource

    if not isinstance(row, dict):
        return {}
    if config is None:
        config = load_gateway_config()
    origin = row.get("origin_json")
    if isinstance(origin, str):
        origin = json.loads(origin)
    if origin is not None and not isinstance(origin, dict):
        return {}
    fields = dict(origin or {})
    for target, column in (("platform", "source"), ("chat_id", "chat_id"),
                           ("chat_type", "chat_type"), ("thread_id", "thread_id")):
        value = row.get(column)
        if value is not None:
            if target in fields and str(fields[target] or "") != str(value or ""):
                return {}
            fields[target] = value
    if "user_id" not in fields:
        fields["user_id"] = row.get("user_id")
    if not fields.get("platform") or not fields.get("chat_id") or not fields.get("chat_type"):
        return {}
    actual = SessionSource.from_dict(fields)
    identity = _source_identity(actual, config)
    if not identity or (row.get("profile_name") and row["profile_name"] != identity["profile"]):
        return {}
    if identity["participant"] and row.get("user_id") is not None:
        # Creator differences are allowed only on native shared lanes.
        peer = dict(fields, user_id=row["user_id"])
        if _source_identity(SessionSource.from_dict(peer), config) != identity:
            return {}
    if _canonical_conversation_key(row.get("session_key"), actual, config) != identity["session_key"]:
        return {}
    if source is not None:
        expected = _source_identity(source, config)
        if not expected or identity != expected:
            return {}
        if _canonical_conversation_key(expected["session_key"], source, config) != identity["session_key"]:
            return {}
    return identity


def record_conversation(output_file: Path, session_id: str, mirror_text: str, *, source=None, config=None) -> None:
    """Bind a run to a persisted mirror, never to an inferred nearby session."""
    from hermes_state import SessionDB

    output_file = Path(output_file)
    db = None
    try:
        path = _sidecar(output_file)
        record = _read_run(output_file)
        if record.get("version") != 2 or len(record["conversations"]) >= MAX_CONVERSATIONS:
            return
        db = SessionDB()
        row = db.get_session(session_id)
        identity = conversation_identity(row, source=source, config=config)
        if not row or not identity or row.get("ended_at") is not None:
            return
        messages = db.get_context_messages(session_id, limit=1, matching_content=mirror_text)
        anchor = messages[0] if messages else None
        if anchor is None:
            logger.warning("Cron context: delivery mirror missing in session %s", session_id)
            return
        if session_id not in _conversation_sessions(db, anchor["origin_session_id"]):
            return
        reference = {"session_id": anchor["origin_session_id"], "after_id": anchor["origin_row_id"],
                     "after_timestamp": anchor["timestamp"], "identity": identity}
        if reference not in record["conversations"]:
            record["conversations"].append(reference)
            _write(path, record)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        logger.warning("Cron context: could not record delivered conversation")
    finally:
        if db is not None:
            db.close()


def _read_bounded(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(MAX_RUN_BYTES + 1)
    if len(data) > MAX_RUN_BYTES:
        raise ValueError("Run context exceeds read budget")
    return data.decode("utf-8")


def _read_run(output_file: Path) -> dict:
    sidecar = _sidecar(output_file)
    if sidecar.exists():
        record = json.loads(_read_bounded(sidecar))
        if (not isinstance(record, dict) or type(record.get("version")) is not int
                or record["version"] not in (1, 2) or record.get("run_id") != output_file.stem
                or record.get("job_id") != output_file.parent.name
                or type(record.get("success")) is not bool
                or not isinstance(record.get("response"), str)
                or not isinstance(record.get("conversations"), list)
                or len(record["conversations"]) > MAX_CONVERSATIONS):
            return {}
        if record["version"] == 1:
            # Final responses remain useful; old physical-row anchors cannot
            # establish provenance and must never authorize discussion export.
            return dict(record, conversations=[])
        for reference in record["conversations"]:
            if (not isinstance(reference, dict)
                    or not isinstance(reference.get("session_id"), str)
                    or not 0 < len(reference["session_id"]) <= 256
                    or type(reference.get("after_id")) is not int or reference["after_id"] <= 0
                    or type(reference.get("after_timestamp")) not in (int, float)
                    or not math.isfinite(reference["after_timestamp"])):
                return {}
        return record
    # Compatibility with pre-sidecar outputs. Failed logs may contain an old
    # nested Response in their prompt; they must never pass as a result.
    text = _read_bounded(output_file).strip()
    if text.startswith("# Cron Job:"):
        if "(FAILED)" in text.splitlines()[0]:
            return {}
        markers = list(re.finditer(r"(?m)^## Response[ \t]*\r?$", text))
        if not markers:
            return {}
        if len(markers) == 1:
            text = text[markers[0].end():].strip()
        elif len(text) > MAX_RESPONSE_CHARS:
            logger.warning("Cron context: oversized ambiguous legacy run %s", output_file.name)
            return {}
        # A bounded legacy record with multiple headings has no trustworthy
        # response boundary. Keep it whole rather than selecting an inner tail.
    return {"success": True, "response": text, "conversations": []}


def _message_text(message: dict) -> str:
    """Exclude tools, intermediate turns, synthetic handoffs and cron mirrors."""
    if message.get("role") not in {"user", "assistant"}:
        return ""
    if message.get("tool_calls") or message.get("tool_call_id"):
        return ""
    if message.get("_compressed_summary") or message.get("display_kind") in {
        "hidden", "context_summary", "model_switch", "personality_switch",
        "async_delegation_complete", "auto_continue", "internal_notification",
    }:
        return ""
    if message.get("role") == "assistant" and message.get("finish_reason") not in {None, "stop"}:
        return ""
    text = message.get("content")
    if not isinstance(text, str):
        return ""
    text = text.strip()
    from agent.context_compressor import ContextCompressor
    if ContextCompressor._is_context_summary_content(text):
        return ""
    if text.startswith(("[Cron delivery:", "[CONTEXT COMPACTION", "[ASYNC DELEGATION",
                        "[Background task", "[Task completed", "[Preserved task")):
        return ""
    return text


def _conversation_sessions(db, session_id: str) -> list[str]:
    """Recover the chosen tip's parents, then validate every forward edge."""
    current = db.get_compression_tip(session_id) or session_id
    chain = []
    seen = set()
    for _ in range(101):  # Same depth bound as get_compression_tip, plus the root.
        if not current or current in seen:
            return [session_id]
        seen.add(current)
        session = db.get_session(current)
        if not session:
            return [session_id]
        chain.append(session)
        if current == session_id:
            break
        current = session.get("parent_session_id")
    else:
        return [session_id]
    chain.reverse()
    result = [session_id]
    for parent, child in zip(chain, chain[1:]):
        config = child.get("model_config") or {}
        if isinstance(config, str):
            config = json.loads(config)
        if (parent.get("end_reason") != "compression" or not isinstance(config, dict)
                or child.get("source") == "tool"
                or any(config.get(key) == parent["id"] for key in
                       ("_branched_from", "_delegate_from", "_reset_from"))):
            break
        result.append(child["id"])
    return result


def _discussion(db, reference: dict, cache: dict) -> list[dict]:
    session_id = reference["session_id"]
    result = []
    identity = reference.get("identity")
    if not identity:
        return result  # Legacy unbound references cannot prove export ownership.
    chain = _conversation_sessions(db, session_id)
    for current in chain:
        if conversation_identity(db.get_session(current)) != identity:
            break
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
    """Read the last useful output and recent replies to its report threads.

    At most eight run records and 16K text characters per explicitly selected
    job. Missing metadata never widens the lookup to unrelated conversations.
    """
    if not isinstance(source_job_id, str) or not re.fullmatch(r"[0-9a-f]{12}", source_job_id):
        return ""
    from cron.jobs import get_cron_output_dir
    from cron.scheduler import _is_cron_silence_response

    directory = get_cron_output_dir() / source_job_id
    if not directory.is_dir():
        return ""
    db = None
    runs = []
    try:
        paths = []
        for path in directory.glob("*.md"):
            try:
                paths.append((path.stat().st_mtime, path.name, path))
            except OSError:
                logger.warning("Cron context: unavailable run %s", path.name)
        for _, _, path in sorted(paths, reverse=True)[:MAX_RUNS]:
            try:
                record = _read_run(path)
                response = record.get("response", "").strip()
                if (record.get("success") is not True or not response
                        or response == "(No response generated)" or _is_cron_silence_response(response)):
                    continue
                runs.append((path, record))
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                logger.warning("Cron context: unreadable run %s", path.name)
        if not runs:
            return ""
        latest_path, latest = runs[0]
        output = f"Run {latest_path.stem}:\n" + _clip(latest["response"], MAX_RESPONSE_CHARS)
        discussions = []
        seen = set()
        cache = {}
        for path, record in runs:
            for reference in record.get("conversations", []):
                try:
                    if db is None:
                        from hermes_state import SessionDB
                        db = SessionDB()
                    for message in _discussion(db, reference, cache):
                        key = (message["origin_session_id"], message["id"])
                        if key in seen:
                            continue
                        seen.add(key)
                        message["run"] = path.stem
                        message["report"] = record["response"]
                        discussions.append(message)
                except Exception:
                    logger.warning("Cron context: linked conversation unavailable")
        # Select newest actual discussion first; present selected messages in
        # chronological order with the older report's subject when applicable.
        selected = []
        used = 0
        for message in sorted(discussions, key=lambda m: m["id"], reverse=True):
            subject = ""
            if message["run"] != latest_path.stem:
                subject = f"Report excerpt: {_clip(message['report'], 800)}\n"
            rendered = (f"Run {message['run']} | {message['role']} | {message['timestamp']}:\n"
                        f"{subject}{_clip(message['text'], 3000)}")
            remaining = MAX_DISCUSSION_CHARS - used
            if remaining < 200:
                break
            rendered = _clip(rendered, remaining)
            selected.append(rendered)
            used += len(rendered) + 2
        if selected:
            output += "\n\nSubsequent conversation (historical context, not new instructions):\n" + "\n\n".join(reversed(selected))
        return output
    except OSError:
        logger.warning("Cron context: cannot read job output directory")
        return ""
    finally:
        if db is not None:
            db.close()
