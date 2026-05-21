# Amy's Patches - Changelog (Branch: amy/patches)

**Current base:** Hermes Agent v2026.6.19
**Current patch stack:** Rebased Amy/private patch stack after launchd restart reload-pending fix
**Current reconciliation reviewed through:** v0.17.0 rebase in progress; upstream-absorbed patches dropped, Amy-private patches retained
**Author:** Amy Ravenwolf <amy@ravenwolf.de>

> Current goal: keep only Amy/private patches local, and submit every generally useful feature/fix upstream as an open PR so future upgrades have less custom patch baggage.

---

## 2026-06-21 - Launchd Restart Consumes Deferred Plist Reloads

**Problem:** During an in-gateway launchd plist refresh, Hermes writes a
`.reload-pending` marker instead of booting itself out from under its own ass.
`hermes gateway start` consumed that marker, but `hermes gateway restart` still
used a plain `launchctl kickstart -k`. That could restart the old loaded launchd
job without re-reading the new plist, leaving ProgramArguments, PATH, HERMES_HOME,
or macOS app-wrapper changes stale after an upgrade.

**Solution:** `launchd_restart()` now detects pending or stale launchd service
definitions, rewrites stale plist files before any reload, skips self-requested
restart in that case, drains the running gateway, performs `bootout` + `bootstrap`
to make launchd re-read the plist, then kickstarts the refreshed job.
`launchd_status()` also reports the pending marker so the operator sees the exact
fix command.

**Affected files:**

- `hermes_cli/gateway.py`
- `tests/hermes_cli/test_gateway_service.py`
- `RELEASE_amy-patches.md`

**Verification:** `tests/hermes_cli/test_gateway_service.py` covers pending-marker
restart consumption, stale-plist rewrite before bootstrap, self-request suppression
when a plist reload is pending, status visibility, app-wrapper preservation, and
existing launchd recovery paths.

**Session reference:** 2026-06-21 Hermes v0.17.0 upgrade focused review found the
restart-blocking stale-launchd-state bug before live restart.

---

## 2026-06-21 - Codex Auxiliary Timeout Test Determinism

**Problem:** `TestCodexAuxiliaryAdapterTimeout` used real `time.sleep(0.03)`
wall-clock timing and asserted the full call completed under 0.14s. On the Mac
mini test environment the first scheduled sleep can exceed that bound even when
the adapter aborts on the first timeout check, producing a false failure during
Hermes upgrade verification.

**Solution:** Replaced the wall-clock sleep with a deterministic fake monotonic
clock that advances per emitted event. The test now asserts the adapter stops
after the second event when the synthetic deadline is crossed, preserving the
semantic regression coverage without relying on scheduler timing.

**Affected files:**

- `tests/agent/test_auxiliary_client.py`
- `RELEASE_amy-patches.md`

**Verification:**
`tests/agent/test_auxiliary_client.py::TestCodexAuxiliaryAdapterTimeout::test_enforces_total_timeout_while_stream_keeps_emitting_events`.

**Session reference:** 2026-06-21 Hermes v0.17.0 upgrade verification on Mac mini.

---

## 2026-06-21 - send_message Messaging Toolset Opt-In

**Problem:** Hermes Agent v0.17.0 deliberately removed the agent-callable
`send_message` registry entry so outbound cross-platform messaging is no longer
part of the broad default/core toolsets. For Amy's trusted single-owner runtime,
Wolfram still wants the capability available when explicitly enabled, while not
silently restoring it to every default toolset.

**Solution:** Re-registered `send_message` locally under an explicit
`messaging` toolset. This keeps the upstream safety posture for broad/default
Hermes toolsets, but lets Amy opt in via `platform_toolsets` with
`messaging` (for example alongside `hermes-cli`). The shared send engine remains
unchanged for cron delivery, `hermes send`, the gateway kanban notifier, and MCP.

**Affected files:**

- `tools/send_message_tool.py`
- `tests/tools/test_send_message_tool.py`
- `RELEASE_amy-patches.md`

**Verification:** Targeted registry regression in
`tests/tools/test_send_message_tool.py::test_send_message_registered_as_explicit_messaging_toolset`.

**Session reference:** 2026-06-21 Mattermost Hermes v0.17.0 upgrade/rebase;
Wolfram approved Amy's "best local solution" for `send_message`.

---

## 2026-06-17 - Mattermost Configurable Post-Length Limit

**Problem:** Long Amy/Hermes replies in Mattermost were split into multiple thread replies at Hermes' hard-coded 4,000-character adapter limit, even though Mattermost 5+ supports up to 16,383 characters per post. This forced Wolfram to click `Show more` once per chunk instead of once per long reply and made Mattermost threads noisier than necessary.

**Solution:** Added a configurable Mattermost post chunk limit via `mattermost.max_post_length` / `MATTERMOST_MAX_POST_LENGTH`, clamped to Mattermost's 16,383-character hard cap, with too-small values falling back to the legacy 4,000-character default so the generic chunker cannot hang on impossible limits. The adapter now exposes the effective value through `MAX_MESSAGE_LENGTH` so final replies, streaming, and progress sizing use the same limit. Direct `send_message`/cron delivery resolves the configured Mattermost limit before chunking. Deployments can leave a safety margin below the hard cap for Markdown/code-fence handling and part indicators.

**Affected files:**

- `plugins/platforms/mattermost/adapter.py`
- `tools/send_message_tool.py`
- `hermes_cli/config.py`
- `tests/gateway/test_mattermost.py`
- `RELEASE_amy-patches.md`

**Verification:**

- RED: new Mattermost max-post-length tests failed before implementation (`5 failed, 4 passed`).
- GREEN: `HERMES_HOME=/amy .venv/bin/python -m pytest tests/gateway/test_mattermost.py -q -o 'addopts='` -> `70 passed`.
- Config bridge check: `load_gateway_config()` seeded `extra.max_post_length=16000`, `MATTERMOST_MAX_POST_LENGTH=16000`, and `MattermostAdapter.MAX_MESSAGE_LENGTH=16000`.

**Session reference:** 2026-06-17 Mattermost thread on reducing long-post splitting and repeated `Show more` clicks.

---
## Current Upstream PR Reconciliation (2026-06-08)

Counted with `git describe --tags --abbrev=0 HEAD` and `git rev-list --count v2026.6.5..HEAD`.
Do not count against stale `main`; tag-to-HEAD is the authoritative patch stack.

| Commit | Subject | Upstream disposition |
|---|---|---|
| `d843d29be` | `fix: WhatsApp voice messages + bridge audio download + npm deps` | Public bugfix - submitted as [PR #41616](https://github.com/NousResearch/hermes-agent/pull/41616). |
| `9df07dd98` | `feat(tool_progress): add 'full' mode - unlimited tool args in gateway chat` | Public UX/config feature - submitted as [PR #41617](https://github.com/NousResearch/hermes-agent/pull/41617). |
| `344e1c0ae` | `feat(prompt): add Amy platform hints and Mattermost Private Assistant default` | Private Amy/persona patch - do not upstream. |
| `2fcdbd133` | `feat(memory): configurable background memory update notifications` | Public feature - existing [PR #4684](https://github.com/NousResearch/hermes-agent/pull/4684) updated. |
| `0edd4de6e` | `feat(display): add independent thinking_progress config option` | Public feature - existing [PR #4512](https://github.com/NousResearch/hermes-agent/pull/4512) updated. |
| `789c20c90` | `docs: add Amy's patches changelog for v0.6.0 fork` | Private fork documentation - do not upstream. |
| `968562f7c` | `feat(display): show delegate_task goals in tool progress notifications` | Public UX feature - submitted as [PR #41618](https://github.com/NousResearch/hermes-agent/pull/41618). |
| `4c7cab0c5` | `feat(display): verbose skill change notifications with content previews` | Public notification feature - folded into updated [PR #4684](https://github.com/NousResearch/hermes-agent/pull/4684). |
| `191c334f0` | `feat(prompt): make context-file truncation limit configurable` | Public config/observability feature - submitted as [PR #41619](https://github.com/NousResearch/hermes-agent/pull/41619); upstream keeps the 20K default while users can raise `context_file_max_chars`. |
| `8e6cd69c5` | `feat(hooks): session:compress event_callback for MemPalace sync` | Public hook/event feature - generalized and submitted as [PR #41624](https://github.com/NousResearch/hermes-agent/pull/41624). |
| `4e89d4a95` | `feat: add tool_progress_style config (accumulate vs separate)` | Public UX/config feature - submitted as [PR #41620](https://github.com/NousResearch/hermes-agent/pull/41620). |
| `76f7f07f3` | `fix: suppress pkg_resources deprecation warning from lark_oapi` | Public startup-noise fix - submitted as [PR #41621](https://github.com/NousResearch/hermes-agent/pull/41621). |
| `bcac7fbd9` | `fix(addon): make dashboard assets work behind HA ingress` | Public reverse-proxy/dashboard bugfix - submitted as [PR #41629](https://github.com/NousResearch/hermes-agent/pull/41629). |
| `e7c573aeb` | `feat(vision): add provider-safe inject_image tool` | Public multimodal/tool feature - submitted as [PR #41632](https://github.com/NousResearch/hermes-agent/pull/41632). |
| `e33e1a30b` | `fix(config): read browser inactivity timeout from config` | Public config bugfix - submitted as [PR #41623](https://github.com/NousResearch/hermes-agent/pull/41623). |
| `d45f99345` | `feat(moa): route experts through provider-aware clients` | Public provider-routing feature/fix - submitted as [PR #41626](https://github.com/NousResearch/hermes-agent/pull/41626). |
| `b071671aa` | `feat(gateway): inject stable human-readable message timestamps` | Public temporal-context feature - made opt-in/default-off and submitted as [PR #41633](https://github.com/NousResearch/hermes-agent/pull/41633). |
| `1b3657a86` | `feat(gateway): add macOS app-wrapper launchd identity` | Public macOS/TCC feature - submitted with logical-path hardening as [PR #41635](https://github.com/NousResearch/hermes-agent/pull/41635). |
| `53a13a742` | `fix: enable GPT-5.5 priority processing fast mode` | Upstream code path was already current; regression coverage submitted as [PR #41628](https://github.com/NousResearch/hermes-agent/pull/41628). |
| `ead67e583` | `fix(gateway): keep macOS launchd runtime paths logical` | Public macOS launchd hardening - generalized and folded into [PR #41635](https://github.com/NousResearch/hermes-agent/pull/41635). |
| `22de314a6` | `fix(state): skip redundant trigram backfill before v11 FTS rebuild` | Public performance/migration fix - submitted as [PR #41622](https://github.com/NousResearch/hermes-agent/pull/41622). |
| `68306dd6d` | `fix(deps): restore CVE-fixed pyproject pins` | Public dependency/security fix - submitted as [PR #41641](https://github.com/NousResearch/hermes-agent/pull/41641). |
| `cc097a495` | `feat(status): restore model and context in gateway status` | Public feature - existing [PR #4678](https://github.com/NousResearch/hermes-agent/pull/4678) updated; overlaps open #8355/#16079. |
| `327edcb58` | `feat(resume): restore cross-platform full session listing` | Public feature - existing [PR #4689](https://github.com/NousResearch/hermes-agent/pull/4689) updated. |
| `2cfb721d6` | `fix(mattermost): keep plugin sends in threads` | Public Mattermost bugfix - submitted with delivery hygiene as [PR #41640](https://github.com/NousResearch/hermes-agent/pull/41640). |
| `81f297b52` | `fix(mattermost): harden delivery hygiene` | Public Mattermost safety/delivery bugfix - submitted with thread routing as [PR #41640](https://github.com/NousResearch/hermes-agent/pull/41640). |
| `5d5ac2f9e` | `docs(patches): update upstream PR reconciliation` | Private fork documentation - do not upstream. |
| `72f882cc2` | `fix(display): preserve generic skill patch notifications` | Public follow-up fix - folded into updated [PR #4684](https://github.com/NousResearch/hermes-agent/pull/4684). |

### Existing PRs Updated

| PR | Branch | Local replacement commit(s) | Result |
|---|---|---|---|
| [#4512](https://github.com/NousResearch/hermes-agent/pull/4512) | `feat/thinking-progress` | `0edd4de6e` | Rebuilt on current `upstream/main`, force-pushed, and PR body updated. |
| [#4678](https://github.com/NousResearch/hermes-agent/pull/4678) | `feat/status-model-context` | `cc097a495` | Rebuilt on current `upstream/main`, force-pushed, and PR body updated; overlaps still-open #8355/#16079. |
| [#4684](https://github.com/NousResearch/hermes-agent/pull/4684) | `feat/memory-notifications` | `2fcdbd133`, `4c7cab0c5`, `72f882cc2` | Rebuilt on current `upstream/main`, force-pushed, and PR body updated; skill-change notification follow-up included. |
| [#4689](https://github.com/NousResearch/hermes-agent/pull/4689) | `feat/resume-cross-platform` | `327edcb58` | Rebuilt on current `upstream/main`, force-pushed, and PR body updated around `/resume --all`, `/resume --full`, and session-ID/prefix lookup. |

### New PRs Submitted

| PR | Branch | Local commit(s) |
|---|---|---|
| [#41616](https://github.com/NousResearch/hermes-agent/pull/41616) | `fix/whatsapp-voice-messages` | `d843d29be` |
| [#41617](https://github.com/NousResearch/hermes-agent/pull/41617) | `feat/tool-progress-full-mode` | `9df07dd98` |
| [#41618](https://github.com/NousResearch/hermes-agent/pull/41618) | `feat/delegate-task-progress-goals` | `968562f7c` |
| [#41619](https://github.com/NousResearch/hermes-agent/pull/41619) | `feat/context-file-truncation-warnings` | `191c334f0` |
| [#41620](https://github.com/NousResearch/hermes-agent/pull/41620) | `feat/tool-progress-style` | `4e89d4a95` |
| [#41621](https://github.com/NousResearch/hermes-agent/pull/41621) | `fix/lark-oapi-pkg-resources-warning` | `76f7f07f3` |
| [#41622](https://github.com/NousResearch/hermes-agent/pull/41622) | `fix/session-db-trigram-backfill-skip` | `22de314a6` |
| [#41623](https://github.com/NousResearch/hermes-agent/pull/41623) | `fix/browser-inactivity-timeout-config` | `e33e1a30b` |
| [#41624](https://github.com/NousResearch/hermes-agent/pull/41624) | `feat/session-compress-event-callback` | `8e6cd69c5` |
| [#41626](https://github.com/NousResearch/hermes-agent/pull/41626) | `feat/moa-provider-aware-clients` | `d45f99345` |
| [#41628](https://github.com/NousResearch/hermes-agent/pull/41628) | `test/gpt55-fast-mode-coverage` | `53a13a742` test coverage |
| [#41629](https://github.com/NousResearch/hermes-agent/pull/41629) | `fix/dashboard-assets-ingress` | `bcac7fbd9` |
| [#41632](https://github.com/NousResearch/hermes-agent/pull/41632) | `feat/provider-safe-inject-image-tool` | `e7c573aeb` |
| [#41633](https://github.com/NousResearch/hermes-agent/pull/41633) | `feat/gateway-message-timestamps` | `b071671aa` |
| [#41635](https://github.com/NousResearch/hermes-agent/pull/41635) | `feat/macos-launchd-app-wrapper` | `1b3657a86`, `ead67e583` |
| [#41640](https://github.com/NousResearch/hermes-agent/pull/41640) | `fix/mattermost-thread-delivery-hygiene` | `2cfb721d6`, `81f297b52` |
| [#41641](https://github.com/NousResearch/hermes-agent/pull/41641) | `fix/cve-dependency-pins` | `68306dd6d` |

Private/local-only patches after reconciliation: Amy platform/persona hints (`344e1c0ae`), private fork patch documentation (`789c20c90`, `5d5ac2f9e`).

---

## Historical v0.6.0 Patch Notes

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

### Dependency Pins: Restore CVE-Fixed pyproject Metadata (2026-05-21)

**Problem:** `uv.lock` was dirty because a local `uv lock` run regenerated the
lockfile from stale `pyproject.toml` pins. The dirty lock would have downgraded
`aiohttp` 3.13.4 → 3.13.3 and `anthropic` 0.87.0 → 0.86.0, while removing the
explicit `cryptography==46.0.7` core pin. That would undo upstream commit
`d725407c5`'s CVE-fixed dependency floors and leave `pyproject.toml`,
`uv.lock`, and `tools/lazy_deps.py` disagreeing.

**Solution:** Reapplied the CVE-fixed dependency pins to `pyproject.toml` so it
matches the committed lockfile and lazy-install map, then regenerated `uv.lock`
with Wolfram's global uv release-age guard. The final lock keeps
`aiohttp==3.13.4`, `anthropic==0.87.0`, `cryptography==46.0.7`, keeps the
editable root package on the rebased upstream version, and records uv's 24h
`exclude-newer-span` option.

**Affected files:** `pyproject.toml`, `uv.lock`

**Session reference:** 2026-05-21 dependency-lock maintenance review.

**Verification:** `uv lock --check`, metadata consistency assertions for
`pyproject.toml`/`uv.lock`, and `git diff --check`.

### Mattermost MEDIA Attachments: Keep Thread Context (2026-05-19)

**Problem:** In Mattermost `MATTERMOST_REPLY_MODE=thread`, normal text replies
used `metadata.thread_id`/`root_id`, but image attachments extracted from
`MEDIA:` tags were routed through the Mattermost `send_multiple_images()` batch
path. That path uploaded files and posted `file_ids` without setting
`root_id`, so images landed in the parent channel while the surrounding text
stayed in the thread.

**Solution:** `MattermostAdapter.send_multiple_images()` now honors
`metadata.thread_id` when reply mode is `thread`, sets `root_id` on the batch
file post, and mirrors the existing invalid-root flat fallback used by normal
messages and single-file sends. Added a regression test covering batched local
MEDIA image uploads with Mattermost thread metadata.

**Affected files:** `gateway/platforms/mattermost.py`,
`tests/gateway/test_mattermost.py`

**Session reference:** 2026-05-19 Mattermost media-threading regression review.

**Verification:**
`tests/gateway/test_mattermost.py::TestMattermostSend::test_send_multiple_images_uses_metadata_thread_id`,
`tests/gateway/test_mattermost.py`, `tests/gateway/test_send_multiple_images.py`.

### Gateway /status: Provider-Aware Idle Context Window (2026-05-19)

`/status` shows model, context usage, and cumulative token labels. Its idle
fallback resolves the model context window provider-aware instead of using raw
`DEFAULT_CONTEXT_LENGTHS`, so `gpt-5.5` via `openai-codex` displays the real
272,000-token Codex OAuth window instead of the direct-OpenAI 1,050,000-token
window. The same path respects provider/base URL/custom-provider/context
overrides used by `/model` and compression.

**Files:** `gateway/run.py`, `tests/gateway/test_status_command.py`

**Verification:** `tests/gateway/test_status_command.py`,
`tests/hermes_cli/test_model_switch_context_display.py`.

### macOS LaunchAgent Path Cleanup (2026-05-17)

The Mac mini migration left the generated launchd `PATH` depending on legacy
root-level `/config` compatibility shims. Removed `/config/amy/bin` and
`/config/.go/bin` from the LaunchAgent path generator so native macOS runtime
startup uses canonical `/amy` / `HERMES_HOME` paths only, and filters inherited
`/config` / `/share` entries so stale shell environments cannot reintroduce the
old shims.

**Files:** `hermes_cli/gateway.py`, `tests/hermes_cli/test_gateway_service.py`

**Verification:** focused launchd PATH test, `tests/hermes_cli/test_gateway_service.py`,
and live LaunchAgent refresh after commit.

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
