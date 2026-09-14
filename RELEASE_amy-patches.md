# Amy's Patches — Stable 0.21 Ledger

## Search pagination keeps valid JSON - checkpoint 2026-09-14

- Problem: appending a human pagination hint after the JSON payload broke strict
  consumers, including the Python tool RPC path (upstream issue #90322).
- Solution: retain the existing September 9 repair, putting the same pagination
  guidance in the `_hint` field before serialization. Credential-result filtering
  and the remaining response fields are unchanged.
- Files: `tools/file_tools.py`, `tests/tools/test_file_tools.py`,
  `tests/agent/test_file_safety_credentials.py`, this ledger.
- Validation: two pagination tests and nine credential-file tests passed in
  isolated homes; scoped Ruff passed. A live Python tool RPC returned a truncated
  search with `_hint` and subsequently read source text without a JSON parse error.
- Provenance: https://github.com/NousResearch/hermes-agent/issues/90322;
  repair and live acceptance on 2026-09-09.
  This checkpoint changes neither the upstream base nor the running service.

## Owner-maintained restart helper - checkpoint 2026-09-14

- Problem: the fixed helper digest compiled into the restart corridor rejected
  legitimate owner-maintained helper updates, preventing routine restarts.
- Solution: retain the September 9 removal of that content pin. The exact helper
  path, strict direct-command grammar, regular-file/owner/mode/size checks,
  descriptor-bound execution and sanitized interpreter environment remain.
  Maintenance payloads still require their separate hash and target identity.
- Files: `cron/lifecycle_guard.py`, `tools/terminal_tool.py`,
  `tests/hermes_cli/test_gateway_restart_loop.py`, this ledger.
- Validation: 52 focused restart-corridor and lifecycle-guard tests passed in
  isolated homes using harmless helper fixtures; scoped Ruff passed. No service
  action was performed for this checkpoint; live acceptance occurred on September 9.
- This follow-up supersedes the original P18/P30 content-pin contract below;
  those rows preserve the reconstruction's historical commit ownership.
- Implementation reference: restart repair, 2026-09-09.
  Source behavior was already deployed before this commit.

## Named skill batch notifications - 2026-09-14

- Problem: successful background skill batches were silent in `on` mode and
  rendered `Skill ?` in `verbose` mode.
- Solution: backport upstream PR #104896, commit
  `ab98a92a45f2b5de1034b18b769e2ef6f70bafc3`, into the existing summary loop.
  Name each successful applied result and its action; suppress staged and
  rolled-back outcomes. Preserve legacy delete/support-file notices.
- Port differences: use loop `continue`/`actions.append` in place of the newer
  `_action_lines` helper's returns; normalize an absent action to an empty key.
  The upstream refactor and later feature changes are not included.
- Files: `agent/background_review.py`,
  `tests/run_agent/test_skill_applied_notifications.py`,
  `website/docs/user-guide/features/memory.md`, this ledger.
- Validation: all four new regressions failed on the original code; 28 focused
  tests passed after the port, including real cross-skill writes in an isolated
  test home. Scoped Ruff passed. Runtime activation awaits a gateway restart.
- Upstream status: merged 2026-09-07 and included in v2026.9.11; drop this
  downstream patch when adopting an upstream base that contains the fix.
- Implementation reference: named skill notification backport, 2026-09-14.

## Cron final-output and conversation continuity - 2026-09-13

- Problem: chained runs read the beginning of nested log artifacts instead of
  final answers, and Mattermost report roots did not seed the incoming reply
  session. Attachment alone did not provide subsequent replies to later runs.
- Solution: store final text and verified session anchors alongside run logs;
  load bounded discussion from selected jobs' report conversations; return and
  seed Mattermost's actual first-post root and channel type. Preserve explicit
  roots and Telegram private-DM delivery. Newly created jobs snapshot an enabled
  mirror default while explicit false and broadcast boundaries remain intact.
- Files: `cron/context.py`, `cron/jobs.py`, `cron/scheduler.py`,
  `hermes_state.py`, `plugins/platforms/mattermost/adapter.py`,
  `tests/cron/test_conversation_context.py`,
  `tests/cron/test_conversation_defaults.py`,
  `tests/cron/test_mattermost_conversation_delivery.py`,
  `tests/cron/test_discussion_identity.py`,
  `tests/gateway/test_mattermost_cron_roots.py`,
  `website/docs/user-guide/features/cron.md`, this ledger.
- Validation: isolated regression tests cover final-only output, real SQLite
  reply visibility, compression lineage and exclusion boundaries, exact
  Mattermost reply keys/chunk roots, default overrides and Telegram DM routing.
  Runtime activation requires a separately approved gateway restart.
- Implementation reference: cron conversation continuity, 2026-09-13.
- Review synchronization, 2026-09-14: preserve complete bounded legacy reports
  with ambiguous Response headings, skip oversized ambiguous records, and ignore
  individual output files that disappear or become unreadable during selection.
  The release-native `.context.json` storage remains the final-output owner.
  Discussion references now bind verified route identity. Older unbound dialog
  references are not re-exported; their final-output text remains readable.
  Continuation seeding verifies profile, participant and root, and an explicitly
  rejected root cannot silently fall back to a rootless post.
  `tests/cron/test_conversation_context.py` and
  `tests/cron/test_cron_context_from.py` passed 53 isolated tests. Public behavior
  reference: [final-output contribution](https://github.com/tachyon-r/hermes-agent/pull/2).
- Bounded provenance closure, 2026-09-15: both native compression publication
  owners stamp original-row references in existing message metadata and commit
  the child seed boundary with its messages. Cron selects and deduplicates
  original insertion IDs, never copied text or timestamps. Unresolved copies,
  old unproved descendant seeds and legacy discussion references are excluded;
  saved version-1 final report text remains available. No pre-compression input
  persistence, new table, backfill or migration was added. Unique native output
  names prevent simultaneous runs from overwriting each other's report anchors.
  Additional regression file: `tests/test_compression_watermark_commit.py`.
  The local Cron/identity/compression cohort passes 251 isolated tests; the
  corresponding public candidate passes 267. Both retain one existing audioop
  deprecation warning. In-flight copies without an independently stored original
  are deliberately omitted by this bounded context contract.
  Implementation reference: bounded original-row discussion context, 2026-09-15.
  Installation and service activation remain separate operations.

This ledger documents the downstream contracts retained on `amy/patches` above upstream Hermes Agent 0.21.0 (`29112bef099274229cadff79cdff7bf7b99c4b77`). It was rebuilt from the target branch's actual Git history; detailed test receipts and rollback records remain outside the public branch.

## Scope and verification boundary

- The reconstructed stack contains 24 runtime/test commits plus the commit that adds this ledger; it contains no merge commits.
- A target commit listed below identifies the original reconstructed owner of that retained contract; later corrections are folded into that logical patch. P08, P18, and P30 intentionally share one target commit because they form one verified restart corridor.
- `ABSORBED` means the required behavior is supplied by upstream and no downstream delta remains. `DROP_UPSTREAM` means the temporary downstream implementation was removed in favor of native upstream support.
- Changed paths are derived from the target commit objects, not copied from the old release notes.
- Package-specific tests and independent reviews were run against the reconstructed target. This document does not itself claim that a live deployment or service restart occurred.
- **Implementation reference:** Stable 0.21 reconstruction, 2026-09-07 to 2026-09-08.

## Source-patch disposition

| Patch | Disposition | Target commit | Retained contract |
|---|---|---|---|
| P01 | PORT | `37ddfaeaee8d` | Preserve configurable, signed macOS application-wrapper identity across native launchd supervisor generation. |
| P02 | PORT | `95441e2dbef3` | Report asynchronously resolved model/context metadata on native status routes. |
| P03 | PORT | `b6a0babac271` | Keep outbound `send_message` explicit and opt-in while preserving platform/thread targeting. |
| P04 | PORT | `a9410e277e69` | Enforce Mattermost message/media limits and retain complete post receipts. |
| P05 | KEEP | `71bfc26fa9cd` | Keep Mattermost behavioral YAML values profile-local with explicit environment precedence. |
| P06 | PORT | `7e173169cdc6` | Preserve media captions, filename fallbacks, and truthful partial-delivery receipts. |
| P07 | KEEP | `88a64d423765` | Keep deterministic auxiliary deadline regression coverage without runtime changes. |
| P08 | PORT | `b768ddd2e375` | Guard gateway lifecycle operations while allowing only the reviewed self-management corridor. |
| P09 | PORT | `26ee0eebbec5` | Preserve automatic vision-capable auxiliary fallback and backend identity. |
| P10 | PORT | `9068acf6ba86` | Preserve native provider-profile identity, headers, transport, and target-model propagation. |
| P11 | KEEP | `33ee8b90f8dc` | Retain the narrow allowlist for safe operational metadata without weakening redaction. |
| P12 | PORT | `2825fd6a0bd8` | Preserve canonical resume provenance, origin authorization, global lookup, and pagination. |
| P13 | KEEP | `605565f4ba75` | Preserve nested WhatsApp media normalization and MIME-aware audio metadata. |
| P14 | PORT | `dca04e0bd2d7` | Preserve transactional WhatsApp runtime staging, validation, replacement, rollback, and state. |
| P15 | PORT | `d9c91e806718` | Preserve full redacted tool/lifecycle progress with cancellation-safe acknowledgements. |
| P16 | PORT | `0393d631377a` | Fail closed if isolated tests reach live launchd or the live Hermes state database. |
| P17 | PORT | `451e7ce284d6` | Preserve transactional launchd publication, bounded restart/stop behavior, rollback, and process identity checks. |
| P18 | PORT | `b768ddd2e375` | Bind restart trust to reviewed helper bytes, owner/mode constraints, and the verified descriptor. |
| P19 | PORT | `8fc7766e9998` | Keep premium service-tier policy isolated per fallback route and restore primary runtime state. |
| P20 | ABSORBED | upstream | Use native `runtime.nofile_soft_limit` launchd resource limits; no duplicate downstream constant. |
| P21 | ABSORBED | upstream | Use native centralized post-turn goal continuation; no duplicate direct goal invocation. |
| P22 | PORT | `1aaed0da2aad` | Preserve contextual per-call transcription provider/context overrides through native hooks. |
| P23 | PORT | `0bfdf7014af9` | Preserve selectable, bounded Discord live transcription with capture and egress gates. |
| P24 | PORT | `b7d9c27c5cb1` | Prevent rejected or race-grown local scripts from reaching unbounded fallback reads. |
| P25 | PORT | `cf195a49d787` | Re-arm compression recovery only after verified progress while retaining completed boundaries. |
| P26 | PORT | `82dcdc8696d0` | Honor reset mode `none` on compression exhaustion and require explicit recovery. |
| P27 | DROP_UPSTREAM | upstream | Remove the temporary Grok 4.6 backport and use native model/reasoning support. |
| P28 | LEDGER_UPDATE | this commit | Replace the old-version ledger with this history-derived Stable 0.21 ledger. |
| P29 | PORT | `6f87963f284b` | Preserve session-owned manual fallback selection, profile-scoped credentials, and metadata. |
| P30 | PORT | `b768ddd2e375` | Bind the shared restart corridor to the independently reviewed current helper digest. |

## Target commit manifests

The hashes below identify the initial Stable 0.21 reconstruction. Later review
corrections are folded into their owning patches. The 2026-09-14 reconciliation
covers compression-pause persistence, transactional WhatsApp activation,
Mattermost target validation and source-aware delivery recovery, native provider
warnings, final-output reading and route identity, progress refusal and
redaction, vision error handling, transcription context selection, Discord
capture cleanup, and launchd update compensation. Public comparison references are
[#110835](https://github.com/NousResearch/hermes-agent/pull/110835),
[WhatsApp runtime updates](https://github.com/Diaspar4u/hermes-agent/pull/1),
[#110738](https://github.com/NousResearch/hermes-agent/pull/110738),
[#48014](https://github.com/NousResearch/hermes-agent/pull/48014),
[#108730](https://github.com/NousResearch/hermes-agent/pull/108730), and
[final-output reading](https://github.com/tachyon-r/hermes-agent/pull/2).
Release-native module ownership and deliberately local behavior remain separate
from the public contribution status.

Full progress also keeps Slack JSON literal through native send/edit, disables
mention and link expansion, and leaves answer-stream finalization with the final
reply. The transport-backed Slack and progress/lifecycle cohorts passed 289 and
236 tests respectively; those cohorts overlap and are not a unique total.
Full-progress masking additionally recognizes exact `pass`, `pw`, `pwd`, and
`passcode` keys and long options while preserving the shell working-directory
variable `PWD`. Twelve native callback cases per variant verify redaction and
unchanged input structure. The focused public/local cohorts pass 191/131 tests
respectively. Implementation reference: password-alias correction, 2026-09-15.
Manual fallback retains its marker across verified compression recovery and
rebuilds cached agent/request state when only its projected token limit changes.

### P16 — `0393d631377a` — `test(isolation): preserve fail-closed runtime guards`

- `hermes_state.py`
- `tests/conftest.py`
- `tests/hermes_state/test_live_db_isolation_guard.py`
- `tests/test_live_system_guard_self_test.py`

### P11 — `33ee8b90f8dc` — `fix(redaction): preserve narrowly scoped operational metadata`

- `agent/redact.py`
- `tests/agent/test_redact.py`

### P25 — `cf195a49d787` — `fix(compression): retain completed boundaries through model recovery`

- `agent/conversation_loop.py`
- `tests/run_agent/test_compression_budget_rearm.py`

### P19 — `8fc7766e9998` — `feat(fallback): preserve route-local service tier isolation`

- `agent/agent_init.py`
- `agent/agent_runtime_helpers.py`
- `agent/background_review.py`
- `agent/chat_completion_helpers.py`
- `agent/fallback_policy.py`
- `agent/turn_finalizer.py`
- `tests/agent/test_turn_finalizer_cleanup_guard.py`
- `tests/run_agent/test_background_review_cost_controls.py`
- `tests/run_agent/test_fallback_service_tier_override.py`
- `tests/run_agent/test_init_fallback_on_exhausted_pool.py`
- `tests/run_agent/test_primary_runtime_restore.py`
- `tests/run_agent/test_provider_fallback.py`
- `tests/run_agent/test_switch_model_fallback_prune.py`

### P07 — `88a64d423765` — `test: preserve auxiliary deadline regression coverage`

- `tests/agent/test_auxiliary_client.py`

### P02 — `95441e2dbef3` — `fix(status): retain resolved context metadata on native status routes`

- `gateway/slash_commands.py`
- `tests/gateway/test_status_command.py`

### P12 — `2825fd6a0bd8` — `fix(resume): retain canonical origin authorization and cross-platform listings`

- `gateway/slash_commands.py`
- `hermes_cli/commands.py`
- `hermes_cli/session_listing.py`
- `hermes_state.py`
- `tests/gateway/test_matrix_project_context_isolation.py`
- `tests/gateway/test_resume_command.py`
- `tests/hermes_cli/test_commands.py`
- `tests/hermes_cli/test_session_listing.py`

### P09 — `26ee0eebbec5` — `fix(auxiliary): preserve auto vision fallback policy and backend identity`

- `agent/auxiliary_client.py`
- `tests/agent/test_auxiliary_client.py`

### P15 — `d9c91e806718` — `feat(progress): preserve full redacted output and lifecycle acknowledgements`

- `agent/display.py`
- `cli.py`
- `gateway/display_config.py`
- `gateway/platforms/base.py`
- `gateway/relay/adapter.py`
- `gateway/run.py`
- `gateway/slash_commands.py`
- `gateway/turn_context.py`
- `plugins/platforms/slack/adapter.py`
- `hermes_cli/commands.py`
- `hermes_cli/config_defaults.py`
- `locales/af.yaml`
- `locales/de.yaml`
- `locales/en.yaml`
- `locales/es.yaml`
- `locales/fr.yaml`
- `locales/ga.yaml`
- `locales/hu.yaml`
- `locales/it.yaml`
- `locales/ja.yaml`
- `locales/ko.yaml`
- `locales/pt.yaml`
- `locales/ru.yaml`
- `locales/tr.yaml`
- `locales/uk.yaml`
- `locales/zh-hant.yaml`
- `locales/zh.yaml`
- `tests/agent/test_tool_progress_comment_descriptions.py`
- `tests/cli/test_cli_init.py`
- `tests/gateway/test_compression_progress_notices.py`
- `tests/gateway/test_consolidated_lifecycle_progress.py`
- `tests/gateway/test_display_config.py`
- `tests/gateway/test_run_progress_topics.py`
- `tests/gateway/test_slack_native_streaming.py`
- `tests/gateway/test_tool_log_mode.py`
- `tests/gateway/test_tool_progress_comment_descriptions.py`
- `tests/gateway/test_verbose_command.py`
- `website/docs/user-guide/cli.md`
- `website/docs/user-guide/configuration.md`

### P26 — `82dcdc8696d0` — `fix(session): preserve explicit compression exhaustion pause`

- `gateway/run.py`
- `gateway/session.py`
- `gateway/slash_commands.py`
- `tests/gateway/test_compress_command.py`
- `tests/gateway/test_compression_exhaustion_reset_policy.py`
- `tests/gateway/test_consolidated_lifecycle_progress.py`

### P29 — `6f87963f284b` — `feat(fallback): preserve session-owned fallback selection and metadata`

- `agent/agent_init.py`
- `agent/chat_completion_helpers.py`
- `gateway/run.py`
- `gateway/session.py`
- `gateway/slash_commands.py`
- `hermes_cli/commands.py`
- `hermes_cli/config.py`
- `hermes_cli/fallback_config.py`
- `hermes_cli/runtime_provider.py`
- `run_agent.py`
- `tests/gateway/test_fallback_command.py`
- `tests/gateway/test_session_hygiene.py`
- `tests/gateway/test_session_store_prune.py`
- `tests/gateway/test_session_store_stale_prune.py`
- `tests/hermes_cli/test_config_env_expansion.py`
- `tests/hermes_cli/test_fallback_config.py`
- `tests/run_agent/test_provider_fallback.py`

### P10 — `9068acf6ba86` — `feat(providers): preserve native profile identity and target transport`

- `gateway/run.py`
- `hermes_cli/providers.py`
- `hermes_cli/runtime_provider.py`
- `tests/gateway/test_channel_overrides.py`
- `tests/gateway/test_session_model_override_persistence.py`
- `tests/hermes_cli/test_model_switch_custom_providers.py`
- `tests/hermes_cli/test_runtime_provider_resolution.py`

### P22 — `1aaed0da2aad` — `feat(transcription): preserve contextual overrides through native hooks`

- `tests/tools/test_transcription_tools.py`
- `tools/transcription_tools.py`

### P23 — `0bfdf7014af9` — `feat(discord): preserve selectable bounded live transcription`

- `hermes_cli/config_defaults.py`
- `plugins/platforms/discord/adapter.py`
- `plugins/platforms/discord/live_transcription.py`
- `tests/gateway/test_voice_command.py`
- `tests/plugins/platforms/test_discord_live_transcription.py`
- `tests/plugins/platforms/test_discord_stt_modes.py`
- `website/docs/user-guide/features/voice-mode.md`

### P13 — `605565f4ba75` — `fix(whatsapp): preserve nested media normalization`

- `scripts/whatsapp-bridge/bridge.js`
- `scripts/whatsapp-bridge/bridge.native.test.mjs`
- `scripts/whatsapp-bridge/bridge_helpers.js`

### P14 — `dca04e0bd2d7` — `fix(whatsapp): preserve transactional runtime updates and state`

- `gateway/platforms/whatsapp_common.py`
- `hermes_cli/main.py`
- `hermes_cli/web_server.py`
- `plugins/platforms/whatsapp/adapter.py`
- `scripts/whatsapp-bridge/bridge.js`
- `scripts/whatsapp-bridge/bridge.native.test.mjs`
- `scripts/whatsapp-bridge/bridge_helpers.js`
- `scripts/whatsapp-bridge/package.json`
- `tests/gateway/test_whatsapp_bridge_dir_resolution.py`
- `tests/gateway/test_whatsapp_bridge_runtime_update.py`
- `tests/gateway/test_whatsapp_connect.py`
- `tests/gateway/test_whatsapp_stale_bridge.py`

### P01 — `37ddfaeaee8d` — `feat(macos): preserve configured app wrapper identity`

- `hermes_cli/gateway.py`
- `hermes_cli/subcommands/gateway.py`
- `tests/hermes_cli/test_gateway_service.py`

### P03 — `b6a0babac271` — `feat(messaging): preserve explicit outbound tool opt-in`

- `hermes_cli/tools_config.py`
- `model_tools.py`
- `plugins/platforms/mattermost/adapter.py`
- `tests/gateway/test_mattermost.py`
- `tests/hermes_cli/test_tools_config.py`
- `tests/test_toolsets.py`
- `tests/tools/test_send_message_target_parse.py`
- `tests/tools/test_send_message_tool.py`
- `tools/send_message_tool.py`
- `toolsets.py`

### P04 — `a9410e277e69` — `fix(mattermost): preserve limits and complete post receipts`

- `gateway/config.py`
- `gateway/run.py`
- `gateway/stream_consumer.py`
- `hermes_cli/config_defaults.py`
- `plugins/platforms/mattermost/adapter.py`
- `tests/gateway/test_mattermost.py`
- `tests/gateway/test_mattermost_source_receipts.py`
- `tests/gateway/test_stream_consumer.py`
- `tools/send_message_tool.py`

### P05 — `71bfc26fa9cd` — `fix(mattermost): preserve profile-local configuration`

- `plugins/platforms/mattermost/adapter.py`
- `tests/gateway/test_allowed_channels_widening.py`
- `tests/gateway/test_mattermost.py`

### P06 — `7e173169cdc6` — `fix(mattermost): preserve media captions and receipts`

- `plugins/platforms/mattermost/adapter.py`
- `tests/gateway/test_mattermost.py`
- `tests/tools/test_send_message_tool.py`
- `tools/send_message_tool.py`

### P08/P18/P30 — `b768ddd2e375` — `fix(self-management): preserve verified gateway lifecycle controls`

- `cron/lifecycle_guard.py`
- `hermes_cli/gateway.py`
- `tests/hermes_cli/test_gateway_restart_loop.py`
- `tools/terminal_tool.py`

### P17 — `451e7ce284d6` — `fix(macos): make launchd lifecycle transactional`

- `hermes_cli/gateway.py`
- `hermes_cli/service_manager.py`
- `hermes_cli/update_cmd.py`
- `tests/hermes_cli/test_gateway_service.py`
- `tests/hermes_cli/test_launchd_public_contracts.py`
- `tests/hermes_cli/test_update_launchd_restart_verification.py`
- `tests/hermes_cli/test_update_launchd_unloaded_gateway.py`

### P24 — `b7d9c27c5cb1` — `fix(terminal): bound local script fallback reads`

- `tests/hermes_cli/test_gateway_restart_loop.py`
- `tools/terminal_tool.py`

## Non-delta dispositions

- **P20 / ABSORBED:** native launchd generation already emits the configured soft open-file limit.
- **P21 / ABSORBED:** native post-turn orchestration already owns streamed goal continuation.
- **P27 / DROP_UPSTREAM:** no downstream Grok 4.6 production delta remains.
- **P28 / LEDGER_UPDATE:** this file is the only changed path of the ledger commit.

## Temporary backports after the Stable 0.21 reconstruction

### P31 — Emoji ZWJ context scanning — TEMP_BACKPORT

- **Problem:** a normal compound emoji containing U+200D caused an entire context file such as `AGENTS.md` or `SOUL.md` to be rejected; skill scanning had the same false positive.
- **Solution:** backport [upstream PR #76857](https://github.com/NousResearch/hermes-agent/pull/76857), at head `22f0db934e8cba96916d21920353a65d7fd34a54`, as one downstream commit. The shared two-sided emoji-neighbor heuristic is used by the context, skill, and cron scanners. Bare/mixed text joiners and other invisible/bidi characters remain detectable. This is the upstream range-based heuristic, not a complete Unicode emoji-sequence validator.
- **Provenance:** Oscar Estrada's commits `e21935c9181f6fd738deb3d7edb73abf4f0313f5`, `e7b33ac8f610d768b2fab62b5f2e2975e9f27942`, and `22f0db934e8cba96916d21920353a65d7fd34a54`. Production changes are unchanged; patch application preserves the newer `skills-guard-v2` version context. One additional downstream regression covers a light-skin/red-hair emoji and retained threat detection across all three scan scopes.
- **Paths:** `tools/threat_patterns.py`, `tools/skills_guard.py`, `tools/cronjob_tools.py`, `tests/tools/test_threat_patterns.py`, `tests/tools/test_skills_guard.py`, and this ledger entry.
- **Verification:** focused regressions fail before the fix; the bounded threat/skill/cron/context-loader suite passes (220 tests, one skipped). A fresh-process comparison verifies that an unchanged context file is blocked before and accepted verbatim by the scanner after the fix. Normal context-size limits still apply.
- **Removal:** drop this entire temporary patch when a controlled update brings the equivalent merged upstream fix into the deployed base. A remote PR merge alone is not permission to upgrade or to remove the fix from an older deployed release.
- **Session reference:** owner-approved emoji-scanner backport, 2026-09-09.

### P32 — Cua element-token schema compatibility — TEMP_BACKPORT

- **Problem:** current cua-driver releases expose `element_token` in each action tool's live input schema without the older `accessibility.element_tokens` capability marker. Hermes therefore cached valid tokens after capture but omitted them from element-index actions, which cua-driver rejected with `snapshot_id_required`.
- **Solution:** treat either the live `element_token` input property or the legacy capability marker as proof that an action accepts tokens. Older drivers that advertise neither surface still receive no unknown field. A focused regression covers the live-schema-only contract.
- **Provenance:** local compatibility fix against cua-driver 0.25.0 after a live capture succeeded and a background element click reproduced the refusal. The same capability-only gate remains on the inspected `upstream/main` ref `fd6434b3b36592367ac5faa180b905d64e29214c`.
- **Paths:** `tools/computer_use/cua_backend.py`, `tests/tools/test_computer_use.py`, and this ledger entry.
- **Verification:** the new live-schema-only regression fails with a missing `element_token` before the fix and passes afterward; the complete affected computer-use test set passes 133 tests.
- **Removal:** drop this temporary patch after a controlled upgrade brings an equivalent upstream fix into the deployed base.
- **Session reference:** live cua-driver 0.25.0 compatibility repair, 2026-09-10.

### P33 - Restore Tavily web search and extraction - TEMP_BACKPORT

- **Problem:** upstream 0.21.0 removed the complete Tavily backend while existing keyed installations retained `web.search_backend: tavily` and `web.extract_backend: tavily`; both tools failed with an unregistered-provider error.
- **Solution:** backport [upstream PR #100552](https://github.com/NousResearch/hermes-agent/pull/100552), head `740071626fd24ccdb5cd3edc4de2ff9c414bd716`, as one downstream patch. Restore the bundled provider, credential/config/setup/status integration, and corresponding docs/tests. Existing keyed selection works again; opt-in keyless Tavily remains outside the unchanged four-provider free ring. No live configuration, credentials, billing route, or gateway lifecycle is changed by this patch.
- **Provenance:** the Tavily restore and documentation commits `9117f663be82a2fa35d1f31a8733dc7696981579` and `fe90cc8e2f696cfaffffdccc04ab76cbe51e49eb` are preserved. The final reconciliation commit `740071626fd24ccdb5cd3edc4de2ff9c414bd716` changes a removed-backend warning registry introduced after our release base; that registry and its tests do not exist here, so only those two file changes are omitted. All other upstream file changes are applied unchanged except the two approved downstream extraction corrections and their regression tests described below.
- **Downstream corrections:** Tavily extraction now filters the existing website policy before either keyed or opt-in keyless HTTP requests, preserves policy metadata on errors, and matches results by requested URL before the positional wrapper/cache receives them. Missing, shuffled, extra, failed, and duplicate results retain correct input slots. These corrections are provider-local; other providers and the shared dispatcher are unchanged. Open upstream overlap: [#96859](https://github.com/NousResearch/hermes-agent/pull/96859), [#97430](https://github.com/NousResearch/hermes-agent/pull/97430), and [#55938](https://github.com/NousResearch/hermes-agent/pull/55938). Coordinate any later submission with those existing efforts.
- **Paths:** `plugins/web/tavily/`; `agent/web_search_{provider,registry}.py`; `tools/web_tools.py`; credential discovery/setup/status modules under `hermes_cli/`; related web-provider comments, test fixtures, regression tests, and web documentation. The commit manifest is authoritative for the complete file list.
- **Verification:** four restored routing regressions fail before the backport; nine new extraction-contract cases fail before the downstream corrections. The same nine-file affected suite then passes 352 tests (10 skipped: four native-Windows cases and six first-release-badge cases); two Parallel SDK failures are deselected after identical reproduction on the untouched baseline. One dependency `audioop` deprecation warning remains. Changed Python files pass Ruff. The real wrapper/cache tests verify failure/success batches and later cache reads, shuffled/sparse/duplicate responses, keyed/keyless policy filtering, and transport errors. Fresh-process real keyed search and Example Domain extraction passed with rescue disabled; the documentation URL returned a page-specific Tavily fetch error. Live configuration and credentials are outside this patch.
- **Removal:** after a separately approved upgrade, remove the upstream restore portion when its equivalent is in the deployed base. Retain these downstream ordering/policy corrections until equivalent fixes are also present and verified; the restore alone in upstream 0.21.1 does not satisfy that condition.
- **Session reference:** owner-requested Tavily restore backport, 2026-09-10.


## 2026-09-10 - Prefer the active live model catalog and highlight implicit provider changes

- **Problem:** Typed `/model` could route a newly available subscription model to metered OpenRouter because the static active-provider catalog lacked the model. The normal success message did not prominently flag the provider change.
- **Solution:** Backport the live-catalog check from [upstream PR #97487](https://github.com/NousResearch/hermes-agent/pull/97487), reviewed at `53d6caabb8a6193ceb72c2ce58133e3ffb45d287`, before the OpenRouter fallback. Preserve the existing fallback if the live catalog fails or has no match. Add a separate warning to successful shared switch results when the original command omitted `--provider` and the provider changed. CLI renders it red/bold; gateway replies lead with a red alarm emoji and bold text. Existing validation warnings and confirmation/cancellation behavior remain intact.
- **Boundary:** The warning reports an applied switch; it is not a new confirmation gate. Catalog failures can still fall back to another provider. Explicit provider selection remains the deterministic routing option.
- **Affected files:** `hermes_cli/models.py`, `hermes_cli/model_switch.py`, `cli.py`, `gateway/slash_commands.py`, `tests/hermes_cli/test_model_switch_provider_warning.py`, `tests/hermes_cli/test_model_switch_confirm_thread.py`, `tests/gateway/test_model_command_expensive_confirm.py`, and this ledger.
- **Validation:** New failure-first coverage reproduced the Codex-to-OpenRouter misroute and missing warnings. The isolated targeted suite passed 123 tests across 11 files, including adjacent configured-provider, API-mode, variant-tag, gateway and CLI tests. No live inference or service restart was performed.
- **Implementation reference:** live model catalog routing, 2026-09-10.
- **Review synchronization, 2026-09-14:** Preserve the complete combined provider
  and validation warnings in immediate native switch results. Deferred switches
  emit a session-owned warning event when applied; Ink and Desktop display it
  without changing routing, confirmation, scope or turn state. This aligns with
  [upstream PR #108730](https://github.com/NousResearch/hermes-agent/pull/108730).
  Additional files: `tui_gateway/server.py`,
  `tests/tui_gateway/test_provider_switch_warning.py`,
  `ui-tui/src/app/createGatewayEventHandler.ts`,
  `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`,
  `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/status.ts`,
  `apps/desktop/src/app/session/hooks/use-message-stream/timeline-events.test.tsx`,
  `apps/desktop/src/app/session/hooks/use-model-controls.ts`, and
  `apps/desktop/src/app/session/hooks/use-model-controls.test.tsx`.
  Verification: 31 isolated Python tests and 114 native frontend tests; the Ink
  package built successfully. No live provider switch was used for verification.
  Immediate and confirmed success warnings additionally reach the real Desktop
  notification store and DOM; 30 consumer, 22 picker and 7 queued tests passed.

## Ordered web extraction and cache provenance - review synchronization 2026-09-14

- Problem: reordered provider rows could be cached under another requested URL;
  blocked inputs could reach transport, and mismatched cache-body/index writes
  could associate content with stale provenance.
- Solution: pair request and source identities before reconstruction, reject
  blocked input/final URLs, restrict rescue to eligible slots, and bind cached
  text to its reported final URL using a content digest. Unknown provenance and
  legacy or mismatched digests miss the cache. Native timeouts and Tavily's
  stricter per-input filtering remain unchanged.
- Files: `tools/web_tools.py`, `tools/web_result_cache.py`,
  `plugins/web/firecrawl/provider.py`, `plugins/web/keenable/provider.py`,
  `plugins/web/keyless_mcp.py`, `plugins/web/tavily/provider.py`,
  `tests/tools/test_web_result_cache.py`,
  `tests/tools/test_web_extract_native_parity.py`,
  `tests/tools/test_website_policy.py`, and this ledger.
- Validation: 249 affected tests passed in isolation. Two unchanged Parallel
  client-construction tests remain blocked by the isolated SDK dependency gate;
  no dependency installation or live provider request was used.
- Redirect closure, 2026-09-15: direct Firecrawl results now retain their native
  requested URL separately from the reported final URL. Pairing prefers this
  explicit request owner, and unsafe-redirect refusals retain the original
  request slot. SDK metadata, website rules, selection, timeouts and cache final
  provenance remain intact. Native Firecrawl cases cover collision and
  unrelated redirects in both input orders plus policy/SSRF refusals. The
  final quality check additionally found an IPv6 key collision; preserving
  hostname brackets separates address and port without changing default-port
  equivalence. Its native provider/cache regression fails before that two-line
  correction. The three-file cohort then passes 149 tests.
- Implementation reference: ordered web extraction synchronization, 2026-09-14;
  bounded Firecrawl request ownership and IPv6 authority normalization, 2026-09-15.
