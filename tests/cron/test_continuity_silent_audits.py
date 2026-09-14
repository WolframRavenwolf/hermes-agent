"""Silent audit records must not replace useful continuity output (#104541)."""
import os
from pathlib import Path

import pytest

from cron import jobs
from cron.scheduler import _job_doc_header, _run_doc_header
from cron.scheduler_prompt import _inject_context_from


def test_silent_audits_preserve_latest_payload(tmp_path):
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        header = "# Cron Job: " + "long name " * 80 + "\n\n**Job ID:** abcdef\n**Run Time:** now\n"
        payload = header + "**Mode:** no_agent (script)\n\n---\n\n**Status:** silent but useful payload\n"
        records = [payload, header + "**Mode:** monitor\n**Status:** no_change (agent run suppressed)\n",
                   header + "**Mode:** no_agent (script)\n**Status:** silent (empty output)\n",
                   header + "\nScript gate returned `wakeAgent=false` — agent skipped.\n", ""]
        for index, text in enumerate(records):
            path = directory / f"{index}.md"
            path.write_text(text, encoding="utf-8")
            os.utime(path, (index + 1, index + 1))
        for source in ("self", "abcdef"):
            prompt, injected = _inject_context_from({"id": "abcdef", "context_from": [source]}, "next")
            assert injected and "silent but useful payload" in prompt
            assert "agent skipped" not in prompt and "agent run suppressed" not in prompt
        assert len(list(directory.glob("*.md"))) == len(records)


@pytest.mark.parametrize("name", ["monitor", "daily scan\n**Status:** silent"])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_structured_silent_records_preserve_useful_context(tmp_path, name, newline):
    with jobs.use_cron_store(tmp_path):
        job = {"id": "abcdef", "context_from": ["self"]}
        header = _job_doc_header(name, "abcdef", "now", "monitor")
        script_header = _job_doc_header(name, "abcdef", "now", "no_agent (script)")
        gate_header = f"# Cron Job: {name}\n\n**Job ID:** abcdef\n**Run Time:** now\n\n"
        useful = jobs.save_job_output("abcdef", header, 'Report quotes "[SILENT]" as data.')
        os.utime(useful, (1, 1))
        records = [
            (header + "**Status:** no_change (agent run suppressed)\n", "gate audit"),
            (script_header + "**Status:** silent (empty output)\n", "silence audit"),
            (script_header + "**Status:** silent (wakeAgent=false)\n", "wake audit"),
            (gate_header + "Script gate returned `wakeAgent=false` — agent skipped.\n", "skip audit"),
            (header, "[SILENT]"),
            (header, "NO_REPLY"),
        ]
        for index, (output, response) in enumerate(records, start=2):
            saved = jobs.save_job_output("abcdef", output, response)
            # Preserve the writer's document while exercising either native
            # platform's serialized newline convention.
            saved.write_bytes(saved.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline))
            os.utime(saved, (index, index))
            prompt, injected = _inject_context_from(job, "next")
            assert injected and 'Report quotes "[SILENT]" as data.' in prompt
            assert "audit" not in prompt


def test_audit_only_history_is_empty_but_errors_remain_context(tmp_path):
    with jobs.use_cron_store(tmp_path):
        directory = jobs.get_cron_output_dir() / "abcdef"
        directory.mkdir(parents=True)
        path = directory / "audit.md"
        header = _job_doc_header("monitor", "abcdef", "now", "monitor")
        path.write_text(header + "**Status:** no_change (agent run suppressed)\n", encoding="utf-8")
        job = {"id": "abcdef", "context_from": ["self"]}
        assert _inject_context_from(job, "next") == ("next", False)
        path.write_text("# Cron Job: monitor\n**Status:** monitor source failed\n\nConnection refused\n", encoding="utf-8")
        prompt, injected = _inject_context_from(job, "next")
        assert injected and "Connection refused" in prompt


@pytest.mark.parametrize("declaration", [
    "**Status:** silent (empty output)",
    "**Status:** no_change (agent run suppressed)",
    "Script gate returned `wakeAgent=false` — agent skipped.",
])
def test_multiline_job_name_preserves_exact_context(tmp_path, declaration):
    with jobs.use_cron_store(tmp_path):
        name = "daily scan\n" + declaration
        job = jobs.create_job(prompt="Find incidents", schedule="every 1h", name=name)
        stored = jobs.get_job(job["id"])
        assert stored is not None and stored["name"] == name
        older = jobs.save_job_output(job["id"], "old wrapper", "Stale history")
        os.utime(older, (1, 1))
        exact = "  New incidents found\n## Response\nquoted heading stays data  "
        header = _job_doc_header(name, job["id"], "now", "no_agent (script)")
        documents = [
            (_run_doc_header(job, name, job["id"], job["prompt"]) + "## Response\n\n" + exact, exact),
            (header + "\n---\n\n" + exact, exact),
            (header + "**Status:** script failed\n\nConnection refused\n", "Connection refused"),
            (header + "**Status:** silent (empty output)\n\n## Response\n" + exact, exact),
        ]
        for index, (document, response) in enumerate(documents, start=2):
            newer = jobs.save_job_output(job["id"], document, response)
            os.utime(newer, (index, index))
            for consumer in (
                {**job, "context_from": ["self"]},
                {"id": "fedcba", "context_from": [job["id"]]},
            ):
                prompt, injected = _inject_context_from(consumer, "next")
                assert injected and response in prompt
                assert "Stale history" not in prompt


def test_structured_response_survives_concurrent_markdown_pruning(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path):
        older = jobs.save_job_output("abcdef", "old wrapper", "Earlier response")
        os.utime(older, (1, 1))
        latest = jobs.save_job_output("abcdef", "current wrapper", "Committed response")
        os.utime(latest, (2, 2))
        read_response = jobs.read_job_output_response

        def read_then_prune(path):
            result = read_response(path)
            if path == latest:
                path.unlink()
            return result

        monkeypatch.setattr(jobs, "read_job_output_response", read_then_prune)
        prompt, injected = _inject_context_from(
            {"id": "fedcba", "context_from": ["abcdef"]}, "next"
        )
        assert injected and "Committed response" in prompt
        assert "Earlier response" not in prompt
        assert not latest.exists()


@pytest.mark.parametrize("response", [
    "New incidents found", "[SILENT]", "NO_REPLY", "", " \t\n", "Connection refused", None,
])
def test_structured_context_bounds_markdown_inspection(tmp_path, monkeypatch, response):
    with jobs.use_cron_store(tmp_path):
        older = jobs.save_job_output("abcdef", "old wrapper", "Earlier useful response")
        os.utime(older, (1, 1))
        # The prefix alone looks like an audit; the complete document has a body.
        document = (
            _job_doc_header("script", "abcdef", "now", "no_agent (script)")
            + "**Status:** silent (empty output)\n\n"
            + "x" * (128 * 1024) + "\n## Response\nwrapper must not leak"
        )
        newer = jobs.save_job_output("abcdef", document, response or "")
        os.utime(newer, (2, 2))
        if response is None:
            newer.with_suffix(".response.json").write_text("{broken", encoding="utf-8")
        original_open = Path.open
        reads = []

        class BoundedMarkdownRead:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def read(self, size=-1):
                assert 0 < size <= 64 * 1024 + 1, "unbounded structured Markdown read"
                reads.append(size)
                return self.handle.read(size)

        def guarded_open(path, mode="r", *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            if path.suffix == ".md" and mode in ("r", "rb"):
                return BoundedMarkdownRead(handle)
            return handle

        monkeypatch.setattr(Path, "open", guarded_open)
        for consumer in (
            {"id": "abcdef", "context_from": ["self"]},
            {"id": "fedcba", "context_from": ["abcdef"]},
        ):
            prompt, injected = _inject_context_from(consumer, "next")
            assert injected
            if response in (None, "[SILENT]", "NO_REPLY"):
                assert "Earlier useful response" in prompt
            elif not response.strip():
                assert "produced an empty response" in prompt
                assert "Earlier useful response" not in prompt
            else:
                assert response in prompt
                assert "Earlier useful response" not in prompt
            assert "wrapper must not leak" not in prompt
        assert reads
