# Amy's Patches - Changelog

This ledger documents the downstream patches maintained on `amy/patches`. Detailed rollback records are retained privately outside the public branch.

## P01 - feat(macos): preserve configurable app-wrapper identity

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Keep the signed macOS application-wrapper execution identity configurable without freezing stale derived launchd paths.
- **Affected files:**
  - `hermes_cli/gateway.py`
  - `hermes_cli/subcommands/gateway.py`
  - `tests/hermes_cli/test_gateway_service.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-07-04.

## P02 - feat(gateway): show resolved model context in status

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Report the effective model and context window through asynchronous metadata resolution without blocking the gateway event loop.
- **Affected files:**
  - `gateway/slash_commands.py`
  - `tests/gateway/test_status_command.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-05-31.

## P03 - feat(messaging): make send_message an explicit toolset

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Keep outbound messaging explicit and opt-in while preserving platform and thread targeting.
- **Affected files:**
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
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-06-21.

## P04 - fix(mattermost): enforce message and media limits

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Keep Mattermost post-length configuration and hard on-wire message/media limits independently revertible from profile and receipt handling.
- **Affected files:**
  - `gateway/config.py`
  - `gateway/stream_consumer.py`
  - `hermes_cli/config_defaults.py`
  - `plugins/platforms/mattermost/adapter.py`
  - `tests/gateway/test_mattermost.py`
  - `tests/gateway/test_stream_consumer.py`
  - `tools/send_message_tool.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-09.

## P05 - fix(mattermost): keep YAML configuration profile-local

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Keep Mattermost behavioral YAML values in profile-local configuration while preserving explicit operator environment overrides.
- **Affected files:**
  - `plugins/platforms/mattermost/adapter.py`
  - `tests/gateway/test_allowed_channels_widening.py`
  - `tests/gateway/test_mattermost.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-09.

## P06 - fix(mattermost): preserve media captions

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Preserve Mattermost media captions and complete delivery receipts as their own independently revertible adapter patch.
- **Affected files:**
  - `plugins/platforms/mattermost/adapter.py`
  - `tests/gateway/test_mattermost.py`
  - `tools/send_message_tool.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-09.

## P07 - test(auxiliary): make Codex timeout coverage deterministic

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Make the auxiliary Codex timeout regression deterministic without changing runtime behavior.
- **Affected files:**
  - `tests/agent/test_auxiliary_client.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-06-21.

## P08 - feat(self-management): guard gateway lifecycle operations

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Permit the narrowly reviewed gateway self-management corridor while rejecting unsafe lifecycle invocations, with self-contained process-tree detection that launchd transactionality later hardens.
- **Affected files:**
  - `cron/lifecycle_guard.py`
  - `hermes_cli/gateway.py`
  - `tests/hermes_cli/test_cron.py`
  - `tests/hermes_cli/test_gateway_restart_loop.py`
  - `tests/hermes_cli/test_gateway_service.py`
  - `tools/terminal_tool.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-06-21.

## P09 - fix(auxiliary): recover through vision-capable fallback chains

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Preserve automatic auxiliary fallback while continuing across capability mismatches and model-scoped failures.
- **Affected files:**
  - `agent/auxiliary_client.py`
  - `tests/agent/test_auxiliary_client.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-09.

## P10 - fix(providers): preserve ProviderProfile CLI identity

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Resolve ProviderProfile aliases consistently across CLI and runtime provider catalogs.
- **Affected files:**
  - `gateway/run.py`
  - `hermes_cli/auth.py`
  - `hermes_cli/model_switch.py`
  - `hermes_cli/providers.py`
  - `hermes_cli/runtime_provider.py`
  - `tests/gateway/test_channel_overrides.py`
  - `tests/gateway/test_session_model_override_persistence.py`
  - `tests/hermes_cli/test_model_switch_custom_providers.py`
  - `tests/hermes_cli/test_runtime_provider_resolution.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-06-25.

## P11 - fix(security): preserve safe operational redaction metadata

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Keep the exact safe metadata allowlist while preserving false-positive protections in operational output.
- **Affected files:**
  - `agent/redact.py`
  - `tests/agent/test_redact.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-07-16.

## P12 - feat(resume): enforce cross-platform origin authorization

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Provide global and full session discovery while enforcing persisted-origin authorization and deterministic duplicate-title resolution.
- **Affected files:**
  - `gateway/slash_commands.py`
  - `hermes_cli/commands.py`
  - `hermes_cli/session_listing.py`
  - `hermes_state.py`
  - `tests/gateway/test_matrix_project_context_isolation.py`
  - `tests/gateway/test_resume_command.py`
  - `tests/hermes_cli/test_commands.py`
  - `tests/hermes_cli/test_session_listing.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-09.

## P13 - fix(whatsapp): normalize nested audio metadata

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Normalize nested WhatsApp audio metadata and MIME-preserving filenames independently from runtime repair.
- **Affected files:**
  - `scripts/whatsapp-bridge/bridge.js`
  - `scripts/whatsapp-bridge/bridge.native.test.mjs`
  - `scripts/whatsapp-bridge/bridge_helpers.js`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-07-16.

## P14 - fix(whatsapp): update bridge runtimes transactionally

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Bind complete bridge runtime identity to transactional validation, backup, replacement, and rollback.
- **Affected files:**
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
  - `tests/gateway/test_whatsapp_stale_bridge.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-07-16.

## P15 - feat(progress): preserve lossless lifecycle and tool progress

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Unify secure, adapter-aware tool and lifecycle progress with cancellation-safe flushing and opt-in compact intent labels.
- **Affected files:**
  - `agent/display.py`
  - `cli.py`
  - `gateway/display_config.py`
  - `gateway/run.py`
  - `gateway/slash_commands.py`
  - `gateway/turn_context.py`
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
  - `tests/gateway/test_tool_log_mode.py`
  - `tests/gateway/test_tool_progress_comment_descriptions.py`
  - `tests/gateway/test_verbose_command.py`
  - `website/docs/user-guide/cli.md`
  - `website/docs/user-guide/configuration.md`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-11.

## P16 - test(macos): block live launchctl during collection

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Fail closed if test collection or execution reaches the host launchd service manager.
- **Affected files:**
  - `tests/conftest.py`
  - `tests/test_live_system_guard_self_test.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-07-16.

## P17 - fix(macos): make launchd lifecycle transactional

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Make launchd install, refresh, stop, reload, rollback, and launchd-specific process identity verification transactional with explicit failure contracts.
- **Affected files:**
  - `hermes_cli/gateway.py`
  - `hermes_cli/service_manager.py`
  - `tests/hermes_cli/test_gateway_restart_loop.py`
  - `tests/hermes_cli/test_gateway_service.py`
  - `tests/hermes_cli/test_launchd_public_contracts.py`
  - `tests/hermes_cli/test_service_manager.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-09.

## P18 - fix(self-management): bind restart trust to reviewed helper content

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Bind the canonical restart corridor to owner-controlled reviewed content and complete its live-cutover health guards.
- **Affected files:**
  - `cron/lifecycle_guard.py`
  - `tests/hermes_cli/test_gateway_restart_loop.py`
  - `tools/terminal_tool.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-09.

## P19 - feat(fallback): isolate service tier per fallback route

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Prevent primary-route premium service tiers from leaking into fallback providers and report the effective wire policy.
- **Affected files:**
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
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-10.

## P20 - fix(macos): preserve launchd open-file capacity

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Raise launchd file-descriptor capacity without changing non-macOS service behavior.
- **Affected files:**
  - `hermes_cli/gateway.py`
  - `tests/hermes_cli/test_gateway_service.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-10.

## P21 - fix(gateway): continue goals after streamed replies

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Run post-turn goal continuation inside the successful already-sent streaming branch while the final response remains available. Preserve the None return that suppresses duplicate delivery and keep judge failures best-effort after the response was already sent. Add a real handler-level regression covering both successful and failed post-turn hooks.
- **Affected files:**
  - `gateway/run.py`
  - `tests/gateway/test_42039_duplicate_user_message.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-11.

## P22 - feat(transcription): support contextual per-call overrides

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Add explicit provider and context overrides to the shared transcription path independently from Discord streaming.
- **Affected files:**
  - `tests/tools/test_transcription_tools.py`
  - `tools/transcription_tools.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-11.

## P23 - feat(discord): add selectable live STT

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Add the independently selectable Discord live transcription pipeline on top of contextual transcription support.
- **Affected files:**
  - `hermes_cli/config_defaults.py`
  - `plugins/platforms/discord/adapter.py`
  - `plugins/platforms/discord/live_transcription.py`
  - `tests/gateway/test_voice_command.py`
  - `tests/plugins/platforms/test_discord_live_transcription.py`
  - `tests/plugins/platforms/test_discord_stt_modes.py`
  - `website/docs/user-guide/features/voice-mode.md`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-11.

## P24 - fix(terminal): bound local script fallback reads

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Prevent rejected, oversized, binary, non-regular, or race-grown local scripts from reaching unbounded fallback reads.
- **Affected files:**
  - `tests/hermes_cli/test_gateway_restart_loop.py`
  - `tools/terminal_tool.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-12.

## P25 - fix(compression): re-arm recovery after verified progress

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Re-arm same-turn compression recovery only after provider-reported usage proves meaningful progress.
- **Affected files:**
  - `agent/conversation_loop.py`
  - `tests/run_agent/test_compression_budget_rearm.py`
  - `tests/run_agent/test_compression_budget_refund.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-14.

## P26 - fix(gateway): honor reset policy on compression exhaustion

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Preserve exhausted sessions under reset mode none while retaining explicit reset recovery for idle, daily, and both policies.
- **Affected files:**
  - `gateway/run.py`
  - `gateway/session.py`
  - `gateway/slash_commands.py`
  - `tests/gateway/test_compress_command.py`
  - `tests/gateway/test_compression_exhaustion_reset_policy.py`
  - `tests/gateway/test_consolidated_lifecycle_progress.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-14.

## P27 - feat(xai): backport manual Grok 4.6 runtime support

- **Problem:** The deployed v2026.8.3 stack requires this downstream behavior to remain independently reversible and maintainable.
- **Solution:** Backport only the bounded manual native xAI Grok 4.6 runtime contracts required on the deployed v2026.8.3 base.
- **Affected files:**
  - `agent/model_metadata.py`
  - `agent/reasoning_timeouts.py`
  - `agent/transports/codex.py`
  - `tests/agent/test_model_metadata.py`
  - `tests/agent/test_reasoning_stale_timeout_floor.py`
  - `tests/agent/transports/test_codex_transport.py`
  - `tests/hermes_cli/test_xai_grok46_manual_switch.py`
- **Verification:** Preserved the commit's exact tree and validated its changed-path ownership in the reconstructed downstream stack.
- **Session reference:** Local implementation session, 2026-08-15.

## P28 - docs(patches): publish the downstream patch ledger

- **Problem:** The downstream patches need a concise public ledger without exposing private implementation records or machine-local provenance.
- **Solution:** Document every retained patch with its purpose, affected files, verification boundary, and a generic dated implementation reference.
- **Affected files:**
  - `RELEASE_amy-patches.md`
- **Verification:** Reconciled P1-P27 entries against the actual commit order and changed-path manifests.
- **Session reference:** Local documentation session, 2026-08-15.

## P29 - gateway: add session-scoped manual fallback controls

- **Problem:** Sessions could use automatic provider fallback, but users could not enable, disable, or inspect fallback policy for one session without changing global configuration. Named-provider expansion also had to remain bound to the active profile secret scope.
- **Solution:** Add `/fallback on`, `/fallback off`, and `/fallback status`; persist only the session policy marker and selected fallback index; preserve routing and cancellation invariants; validate provider endpoints, API modes, and credential sources before mutation; redact display labels; and resolve runtime configuration through the active profile secret resolver.
- **Affected files:**
  - `RELEASE_amy-patches.md`
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
  - `tests/hermes_cli/test_config_env_expansion.py`
  - `tests/hermes_cli/test_fallback_config.py`
  - `tests/run_agent/test_provider_fallback.py`
- **Verification:**
  - 175 P29-owned and directly related tests passed.
  - 180 security, profile, and provider-resolution tests passed.
  - 401 affected tests passed.
  - 244 adjacent tests passed.
- **Session reference:** Local implementation session, 2026-08-28 to 2026-08-29.

## P31 - fix(terminal): restore verified gateway self-restart

- **Problem:** The trusted canonical restart-helper digest drifted after a reviewed helper update. On macOS systems where the Hermes home lives on an external ownership-disabled APFS volume, launchd also rejected the one-shot worker's control plist and later denied its unsigned interpreter access to the external worker, while the helper could still report only that submission had been attempted.
- **Solution:** Refresh the exact trusted-helper digest after hardening the canonical helper's external handoff: keep launchd's temporary plist, initial standard I/O, and working directory on the ownership-enabled home volume; reuse the already installed and signed gateway app wrapper with only its required Python runtime environment; retain SHA-256 and file-descriptor binding for the frozen worker; and require an atomic worker-started handshake before reporting a successful submission. Arbitrary gateway lifecycle commands remain blocked.
- **Affected files:**
  - `RELEASE_amy-patches.md`
  - `tools/terminal_tool.py`
- **Verification:**
  - 134 focused gateway-lifecycle and terminal-tool tests passed in an isolated Hermes test home.
  - Four canonical-helper contract tests and Bash syntax validation passed in the private runtime repository.
  - A live gateway-hosted dry-run produced both worker-started and success receipts, preserved the gateway PID, and removed the one-shot launchd job.
- **Rollback:** Revert this patch together with the matching private canonical-helper hardening commit; the generic in-gateway lifecycle guard remains the fail-closed fallback.
- **Session reference:** Local implementation session, 2026-09-04.
