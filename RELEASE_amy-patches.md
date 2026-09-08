# Amy's Patches — Stable 0.21 Ledger

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
