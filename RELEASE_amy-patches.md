# Amy's Patches — Changelog (Branch: amy/patches)

**Base:** Hermes Agent v0.6.0 (v2026.3.30)
**Patch Period:** March 30–31, 2026
**Author:** Amy Ravenwolf <amy@ravenwolf.de>

> 10 patches on top of upstream v0.6.0 — session management, tool progress relay, platform hints, WhatsApp fixes, and cross-platform /resume.

---

## ✨ Features

### Tool Progress: Full Mode (`f68ff144`, `3dc3d876`)

New `full` mode for `display.tool_progress` — the most verbose option in the mode hierarchy (`off → new → all → verbose → full`):

- **Relays assistant thinking text** between tool calls to the gateway chat (Telegram, Discord, etc.) via a `💬 _thinking` pseudo-tool notification. Previously, these intermediate messages ("Let me check...", "Found it!") were only visible in HA addon logs (stdout), never in the chat — a hardcoded guard restricted relay to subagents only.
- **Shows complete tool arguments** without any truncation. The initial implementation had a 1000-char limit; a follow-up patch (`3dc3d876`) removed it entirely — full mode means full, no half measures.
- Added to `/verbose` cycle, `hermes setup` wizard, and mode validation.

**Files:** `run_agent.py`, `gateway/run.py`, `hermes_cli/cli.py`

### /resume Command — CLI + Cross-Platform (`5256e7ac`, `eef00dbf`, `99530547`, `a47dc3b0`)

A complete /resume implementation spanning four commits:

1. **CLI handler** (`5256e7ac`) — The /resume command worked in Gateway (Telegram/Discord) but the CLI's `process_command` dispatcher had no handler for it. Added `_handle_resume_command()` with full functionality: list recent sessions, resolve by title, flush current session's memories, and switch context.

2. **API server session registration** (`eef00dbf`) — API server (SillyTavern, Open WebUI, LobeChat) sessions were invisible to /resume because the API server adapter never passed `session_db` to `AIAgent`. ~115 existing API sessions had no state.db entry. Fixed by passing the DB instance, enabling session registration.

3. **Listing modes** (`99530547`) — Four modes for flexible session discovery:
   - `/resume` — Named sessions, current platform (default)
   - `/resume all` — Named sessions, ALL platforms
   - `/resume --full` — All sessions incl. unnamed, current platform
   - `/resume all --full` — All sessions incl. unnamed, ALL platforms
   - Platform tags `[telegram]`, `[cli]` shown in cross-platform listings
   - Session ID prefix shown for unnamed sessions

4. **API session transcript loading** (`a47dc3b0`) — `load_transcript()` only knew about `.jsonl` (gateway) and SQLite (gateway DB) formats. API server sessions use `session_{id}.json` (AIAgent log format). Added a third source and made the loader pick whichever source has the most messages — fixing cross-platform /resume losing all context.

**Files:** `hermes_cli/cli.py`, `gateway/platforms/api_server.py`, `gateway/run.py`

### Platform Hints — Amy Mode Defaults (`b113c782`, `8b2c8c41`)

Platform-aware zipper mode defaults for Amy's persona:

- **API server** (`b113c782`) — API frontends (SillyTavern, Open WebUI, LobeChat) are private environments. Default to Private Assistant Mode (zipper half-open) instead of no hint at all.
- **WhatsApp cleanup** (`8b2c8c41`) — Removed redundant WhatsApp zipper hint. Amy's default is Public Assistant Mode per SOUL.md, so only platforms that deviate (Telegram, API server) need explicit overrides.

**Files:** `agent/prompt_builder.py`, `tests/agent/test_prompt_builder.py`

---

## 🐛 Bug Fixes

### Session Search: Lazy DB Creation (`154785c3`)

The `session_search` tool passed `db=None` to the search function when no pre-initialized `SessionDB` existed — silently returning zero results in cron jobs and background agents (memory flush). Direct SQL queries against `state.db` worked fine, confirming the issue was in tool initialization. Fixed by adding a `_session_search_handler` that lazily creates a `SessionDB` instance when none is provided.

**First observed:** 2026-03-25, session_search consistently returned empty results despite healthy FTS index.

**Files:** `tools/session_search.py`

### WhatsApp Voice + Bridge Audio (`a2bd3b30`)

Batch fix for three WhatsApp issues:
- Platform hints in prompt_builder (Telegram zipper mode)
- WhatsApp voice message handling
- Bridge audio download from WhatsApp servers

**Files:** `agent/prompt_builder.py`, `gateway/platforms/whatsapp.py`, `scripts/whatsapp-bridge/bridge.js`

### WhatsApp Bridge Dependencies (`944256f3`)

Updated npm lock files for the WhatsApp bridge. No functional changes — auto-generated during bridge setup/maintenance.

**Files:** `package-lock.json`, `scripts/whatsapp-bridge/package-lock.json`

---

## 📊 Summary

| Type | Count |
|------|-------|
| Features | 4 (tool_progress full mode, /resume CLI+cross-platform, platform hints, listing modes) |
| Bug Fixes | 2 (session_search lazy DB, WhatsApp voice/bridge) |
| Docs | 1 (this changelog) |
| **Total Patches** | **7 commits** (squashed from 10) |

### Commits (in order)

```
a366ea8e fix(session_search): lazy SessionDB creation for background agents
af260618 feat(cli): add /resume command handler to CLI dispatcher
aa061c56 fix: WhatsApp voice messages + bridge audio download + npm deps
82a8622e feat(tool_progress): add 'full' mode — relay assistant thinking + unlimited tool args
6187427f feat(prompt_builder): Amy platform hints — zipper mode defaults          [PRIVATE]
80dfb50e feat(resume): cross-platform /resume with API server support
1233b555 docs: add Amy's patches changelog for v0.6.0 fork                       [PRIVATE]
```

### Upstream PR Candidates

| # | Commit | Scope |
|---|--------|-------|
| 1 | `a366ea8e` | session_search lazy DB — universal bugfix |
| 2 | `af260618` | /resume CLI handler — feature gap |
| 3 | `aa061c56` | WhatsApp voice + bridge audio — universal bugfix |
| 4 | `82a8622e` | tool_progress full mode — universal feature |
| 5 | `80dfb50e` | cross-platform /resume — universal feature |

### Private (Amy-specific, not for upstream)

| # | Commit | Reason |
|---|--------|--------|
| 6 | `6187427f` | Zipper mode / persona system |
| 7 | `1233b555` | Fork-specific changelog |

---

**Branch:** `amy/patches` (7 commits ahead of `upstream/main`)
**No merge conflicts with upstream. All patch files verified identical to pre-squash.**
