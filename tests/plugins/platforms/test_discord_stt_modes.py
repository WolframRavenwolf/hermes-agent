import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    VoiceReceiver,
    _read_discord_stt_settings,
)


class FakeLiveController:
    def __init__(self, result):
        self.result = result
        self.appended = []
        self.finished = []
        self.aborted = []
        self.closed = 0

    async def append_pcm48(self, user_id, pcm):
        self.appended.append((user_id, pcm))

    async def finish_utterance(self, user_id, *, expected_source_bytes):
        self.finished.append((user_id, expected_source_bytes))
        return dict(self.result)

    async def abort_user(self, user_id):
        self.aborted.append(user_id)

    async def close(self):
        self.closed += 1


def _receiver(*, allowed_user_ids=None, members=None):
    vc = MagicMock()
    vc.channel = SimpleNamespace(members=members or [])
    vc.user = SimpleNamespace(id=999)
    vc._connection = MagicMock()
    receiver = VoiceReceiver(vc, allowed_user_ids=allowed_user_ids)
    receiver._running = True
    return receiver


def _adapter():
    adapter = object.__new__(DiscordAdapter)
    adapter._voice_input_callback = AsyncMock()
    adapter._voice_live_transcribers = {}
    return adapter


def test_voice_receiver_exposes_mapped_pcm_chunks_once():
    receiver = _receiver(allowed_user_ids={"42"})
    receiver.map_ssrc(100, 42)
    pcm = b"\x00\x00\x00\x00" * 10
    receiver._buffer_decoded_pcm(100, pcm)
    assert receiver.drain_stream_chunks() == [(0, 100, 1, 42, pcm)]
    assert receiver.drain_stream_chunks() == []
    assert bytes(receiver._buffers[100]) == pcm


def test_voice_receiver_stop_restores_installed_speaking_hooks():
    original_connection_hook = MagicMock()
    original_ws_hook = MagicMock()
    connection = SimpleNamespace(
        hook=original_connection_hook,
        ws=SimpleNamespace(_hook=original_ws_hook),
    )
    voice_client = MagicMock()
    voice_client._connection = connection
    receiver = VoiceReceiver(voice_client, allowed_user_ids={"42"})
    receiver._running = True
    with patch.dict(
        "sys.modules",
        {"discord.utils": SimpleNamespace(MISSING=object())},
    ):
        receiver._install_speaking_hook(connection)

    assert connection.hook is not original_connection_hook
    assert connection.ws._hook is not original_ws_hook

    receiver.stop()

    assert connection.hook is original_connection_hook
    assert connection.ws._hook is original_ws_hook


def test_voice_receiver_waits_for_explicit_mapping_then_streams_local_preroll():
    members = [
        SimpleNamespace(id=999),
        SimpleNamespace(id=42),
        SimpleNamespace(id=43),
    ]
    receiver = _receiver(allowed_user_ids={"42"}, members=members)
    pcm = b"\x01\x00\x02\x00" * 10
    receiver._buffer_decoded_pcm(100, pcm)
    assert receiver.drain_stream_chunks() == []
    assert 100 not in receiver._ssrc_to_user

    receiver.map_ssrc(100, 42)
    assert receiver.drain_stream_chunks() == [(0, 100, 1, 42, pcm)]
    assert receiver._ssrc_to_user[100] == 42


def test_voice_receiver_never_streams_unmapped_ambiguous_audio():
    members = [
        SimpleNamespace(id=999),
        SimpleNamespace(id=42),
        SimpleNamespace(id=43),
    ]
    receiver = _receiver(allowed_user_ids={"42", "43"}, members=members)
    pcm = b"\x01\x00\x02\x00" * 10
    receiver._buffer_decoded_pcm(100, pcm)
    assert receiver.drain_stream_chunks() == []
    assert bytes(receiver._buffers[100]) == pcm


def test_voice_receiver_remap_invalidates_old_buffer_decoder_and_stream_chunks():
    receiver = _receiver(allowed_user_ids={"42", "43"})
    old_pcm = b"\x01\x00\x02\x00" * 10
    new_pcm = b"\x03\x00\x04\x00" * 10
    receiver.map_ssrc(100, 42)
    receiver._decoders[100] = object()
    receiver._buffer_decoded_pcm(100, old_pcm)

    receiver.map_ssrc(100, 43)
    receiver._buffer_decoded_pcm(100, new_pcm)

    assert receiver.drain_stream_chunks() == [(0, 100, 2, 43, new_pcm)]
    assert bytes(receiver._buffers[100]) == new_pcm
    assert 100 not in receiver._decoders


def test_voice_receiver_rechecks_running_state_before_buffer_publication():
    receiver = _receiver(allowed_user_ids={"42"})
    receiver.map_ssrc(100, 42)
    receiver._running = False
    receiver._buffer_decoded_pcm(100, b"\x01\x00\x02\x00" * 10)

    assert receiver.drain_stream_chunks() == []
    assert bytes(receiver._buffers[100]) == b""


def test_voice_receiver_rejects_frame_decoded_across_ssrc_remap():
    receiver = _receiver()
    receiver.map_ssrc(100, 42)
    snapshot = receiver._snapshot_packet_generation(100)

    receiver.map_ssrc(100, 43)
    receiver._buffer_decoded_pcm(100, b"old", *snapshot)

    assert receiver.drain_stream_chunks() == []
    assert bytes(receiver._buffers[100]) == b""


def test_voice_receiver_rejects_frame_decoded_across_pause_resume():
    receiver = _receiver()
    receiver.map_ssrc(100, 42)
    snapshot = receiver._snapshot_packet_generation(100)

    receiver.pause()
    receiver.resume()
    receiver._buffer_decoded_pcm(100, b"old", *snapshot)

    assert receiver.drain_stream_chunks() == []


def test_drained_chunk_retains_generation_for_egress_recheck():
    receiver = _receiver()
    receiver.map_ssrc(100, 42)
    receiver._buffer_decoded_pcm(100, b"frame")
    chunk = receiver.drain_stream_chunks()[0]

    receiver.map_ssrc(100, 43)

    assert receiver.stream_chunk_is_current(*chunk[:4]) is False


def test_unmap_user_revokes_mapping_buffers_decoder_and_queued_audio():
    receiver = _receiver()
    receiver.map_ssrc(100, 42)
    receiver._decoders[100] = object()
    receiver._buffer_decoded_pcm(100, b"frame")

    receiver.unmap_user(42)

    assert 100 not in receiver._ssrc_to_user
    assert 100 not in receiver._buffers
    assert 100 not in receiver._decoders
    assert receiver.drain_stream_chunks() == []


@pytest.mark.asyncio
async def test_revoke_voice_user_unmaps_ssrc_and_aborts_live_session():
    adapter = _adapter()
    receiver = MagicMock()
    controller = MagicMock()
    controller.abort_user = AsyncMock()
    adapter._voice_receivers = {7: receiver}
    adapter._voice_live_transcribers = {7: controller}

    await adapter._revoke_voice_stt_user(7, 42)

    receiver.unmap_user.assert_called_once_with(42)
    controller.abort_user.assert_awaited_once_with(42)


def test_voice_receiver_bounds_stream_queue_and_preserves_full_local_buffer():
    receiver = _receiver(allowed_user_ids={"42"})
    receiver.map_ssrc(100, 42)
    pcm = b"\x01\x00\x02\x00" * 10
    receiver.MAX_STREAM_QUEUE_BYTES = len(pcm)

    receiver._buffer_decoded_pcm(100, pcm)
    receiver._buffer_decoded_pcm(100, pcm)

    assert receiver.drain_stream_chunks() == [(0, 100, 1, 42, pcm)]
    assert bytes(receiver._buffers[100]) == pcm + pcm


def test_voice_receiver_turn_cap_bounds_buffer_and_streamed_bytes_together():
    receiver = _receiver(allowed_user_ids={"42"})
    receiver.map_ssrc(100, 42)
    pcm = b"\x01\x00\x02\x00" * 10
    receiver.MAX_UTTERANCE_BYTES = len(pcm)
    receiver.MIN_SPEECH_DURATION = 0.0

    receiver._buffer_decoded_pcm(100, pcm)
    receiver._buffer_decoded_pcm(100, pcm)

    assert receiver.drain_stream_chunks() == [(0, 100, 1, 42, pcm)]
    assert receiver.check_silence() == [(42, pcm)]


@pytest.mark.asyncio
async def test_contextual_mode_uses_explicit_openai_gpt_transcribe(tmp_path):
    adapter = _adapter()
    pcm = b"\x00\x00\x00\x00" * 100

    def fake_wav(_pcm, path):
        with open(path, "wb") as handle:
            handle.write(b"wav")

    with patch.object(VoiceReceiver, "pcm_to_wav", side_effect=fake_wav), \
         patch(
             "tools.transcription_tools._transcribe_audio_with_provider",
             return_value={
                 "success": True,
                 "transcript": "Kontext gewinnt",
                 "provider": "openai",
             },
         ) as transcribe:
        await adapter._process_voice_input(
            guild_id=7,
            user_id=42,
            pcm_data=pcm,
            stt_mode="openai_contextual",
        )

    assert transcribe.call_args.kwargs == {
        "model": "gpt-transcribe",
        "provider": "openai",
    }
    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=7,
        user_id=42,
        transcript="Kontext gewinnt",
    )


@pytest.mark.asyncio
async def test_configured_mode_preserves_existing_transcription_dispatch():
    adapter = _adapter()
    pcm = b"\x00\x00\x00\x00" * 100

    def fake_wav(_pcm, path):
        with open(path, "wb") as handle:
            handle.write(b"wav")

    with patch.object(VoiceReceiver, "pcm_to_wav", side_effect=fake_wav), \
         patch(
             "tools.transcription_tools.transcribe_audio",
             return_value={"success": True, "transcript": "Default"},
         ) as transcribe:
        await adapter._process_voice_input(
            guild_id=7,
            user_id=42,
            pcm_data=pcm,
            stt_mode="configured",
        )

    assert transcribe.call_args.args[0].endswith(".wav")
    assert transcribe.call_args.kwargs == {}


@pytest.mark.asyncio
async def test_live_mode_finishes_streamed_turn_and_dispatches_transcript():
    adapter = _adapter()
    controller = FakeLiveController(
        {
            "success": True,
            "transcript": "Live gewinnt",
            "provider": "openai_realtime",
            "item_id": "item_42",
        }
    )
    adapter._voice_live_transcribers[7] = controller
    pcm = b"\x00\x00\x00\x00" * 100

    await adapter._process_completed_voice_utterance(
        guild_id=7,
        user_id=42,
        pcm_data=pcm,
        stt_mode="openai_live_high",
    )

    assert controller.finished == [(42, len(pcm))]
    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=7,
        user_id=42,
        transcript="Live gewinnt",
    )


@pytest.mark.asyncio
async def test_live_failure_does_not_secretly_call_paid_contextual_fallback():
    adapter = _adapter()
    controller = FakeLiveController(
        {
            "success": False,
            "transcript": "",
            "error": "incomplete_live_stream",
            "provider": "openai_realtime",
        }
    )
    adapter._voice_live_transcribers[7] = controller
    adapter._process_voice_input = AsyncMock()

    await adapter._process_completed_voice_utterance(
        guild_id=7,
        user_id=42,
        pcm_data=b"\x00\x00\x00\x00" * 100,
        stt_mode="openai_live_high",
    )

    adapter._process_voice_input.assert_not_awaited()
    adapter._voice_input_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_live_mode_delegates_to_file_pipeline():
    adapter = _adapter()
    adapter._process_voice_input = AsyncMock()
    pcm = b"\x00\x00\x00\x00" * 100

    await adapter._process_completed_voice_utterance(
        guild_id=7,
        user_id=42,
        pcm_data=pcm,
        stt_mode="openai_contextual",
    )

    adapter._process_voice_input.assert_awaited_once_with(
        7,
        42,
        pcm,
        stt_mode="openai_contextual",
    )


def test_discord_stt_mode_is_read_from_discord_config(monkeypatch):
    config = {
        "stt": {"openai": {"languages": ["de", "en"]}},
        "discord": {"voice_stt": {"mode": "live-high"}},
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    mode, loaded = _read_discord_stt_settings()
    assert mode == "openai_live_high"
    assert loaded is config


def test_default_config_keeps_dual_discord_stt_opt_in():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    voice_stt = DEFAULT_CONFIG["discord"]["voice_stt"]
    assert voice_stt["mode"] == "configured"
    assert (
        voice_stt["openai_live"]["delay"]
        == "high"
    )
    assert (
        voice_stt["openai_live"]["max_session_seconds"]
        == 3300.0
    )
    assert DEFAULT_CONFIG["stt"]["openai"]["prompt"] == ""
    assert DEFAULT_CONFIG["stt"]["openai"]["languages"] == []
    assert DEFAULT_CONFIG["stt"]["openai"]["keywords"] == []


@pytest.mark.asyncio
async def test_stt_disabled_never_creates_live_controller():
    adapter = _adapter()
    config = {
        "stt": {"enabled": False},
        "discord": {"voice_stt": {"mode": "openai_live_high"}},
    }
    with patch(
        "plugins.platforms.discord.live_transcription.DiscordLiveTranscriptionController"
    ) as controller_type:
        await adapter._activate_voice_stt_mode(7, "openai_live_high", config)

    controller_type.assert_not_called()
    assert 7 not in adapter._voice_live_transcribers


@pytest.mark.asyncio
async def test_runtime_stt_kill_switch_closes_live_controller_and_discards_audio():
    adapter = _adapter()
    controller = FakeLiveController({"success": True, "transcript": "unused"})
    adapter._voice_live_transcribers[7] = controller
    receiver = MagicMock()

    with patch(
        "plugins.platforms.discord.adapter._read_runtime_stt_enabled",
        return_value=False,
    ):
        await adapter._process_voice_listener_tick(
            guild_id=7,
            receiver=receiver,
            stt_mode="openai_live_high",
            guild=MagicMock(),
        )

    receiver.discard_pending.assert_called_once_with()
    assert controller.closed == 1
    assert 7 not in adapter._voice_live_transcribers


@pytest.mark.asyncio
async def test_listener_tick_streams_authorized_chunks_before_live_commit():
    adapter = _adapter()
    order = []

    class OrderedController(FakeLiveController):
        async def append_pcm48(self, user_id, pcm):
            order.append(("append", user_id, pcm))
            await super().append_pcm48(user_id, pcm)

    controller = OrderedController({"success": True, "transcript": "Live"})
    adapter._voice_live_transcribers[7] = controller
    adapter._is_allowed_user = lambda user_id, **kwargs: user_id == "42"
    adapter._process_completed_voice_utterance = AsyncMock(
        side_effect=lambda *args, **kwargs: order.append(("finish", args, kwargs))
    )
    receiver = MagicMock()
    receiver.drain_stream_chunks.return_value = [
        (0, 100, 1, 42, b"authorized-1"),
        (0, 100, 1, 42, b"-authorized-2"),
        (0, 101, 1, 99, b"blocked"),
    ]
    receiver.stream_chunk_is_current.return_value = True
    receiver.check_silence.return_value = [(42, b"full utterance")]
    guild = MagicMock()

    await adapter._process_voice_listener_tick(
        guild_id=7,
        receiver=receiver,
        stt_mode="openai_live_high",
        guild=guild,
    )

    assert controller.appended == [(42, b"authorized-1-authorized-2")]
    assert order[0] == ("append", 42, b"authorized-1-authorized-2")
    assert order[1][0] == "finish"
    adapter._process_completed_voice_utterance.assert_awaited_once_with(
        7,
        42,
        b"full utterance",
        stt_mode="openai_live_high",
    )


@pytest.mark.asyncio
async def test_listener_drops_drained_chunk_after_ssrc_generation_changes():
    adapter = _adapter()
    receiver = MagicMock()
    receiver.drain_stream_chunks.return_value = [(0, 100, 1, 42, b"stale")]
    receiver.stream_chunk_is_current.return_value = False
    receiver.check_silence.return_value = []
    controller = FakeLiveController({"success": True, "transcript": "unused"})
    adapter._voice_live_transcribers[7] = controller
    adapter._process_completed_voice_utterance = AsyncMock()

    await adapter._process_voice_listener_tick(
        guild_id=7,
        receiver=receiver,
        stt_mode="openai_live_high",
        guild=MagicMock(),
    )

    receiver.stream_chunk_is_current.assert_called_once_with(0, 100, 1, 42)
    assert controller.appended == []
    adapter._process_completed_voice_utterance.assert_not_awaited()


@pytest.mark.asyncio
async def test_listener_drops_later_drained_chunks_after_pause_resume():
    adapter = _adapter()
    receiver = _receiver(allowed_user_ids={"42", "43"})
    receiver.map_ssrc(100, 42)
    receiver.map_ssrc(101, 43)
    receiver._buffer_decoded_pcm(100, b"first")
    receiver._buffer_decoded_pcm(101, b"second")
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingController(FakeLiveController):
        async def append_pcm48(self, user_id, pcm):
            self.appended.append((user_id, pcm))
            if user_id == 42:
                first_started.set()
                await release_first.wait()

    controller = BlockingController({"success": True, "transcript": "unused"})
    adapter._voice_live_transcribers[7] = controller
    adapter._is_allowed_user = lambda user_id, **kwargs: True
    adapter._process_completed_voice_utterance = AsyncMock()

    tick = asyncio.create_task(
        adapter._process_voice_listener_tick(
            guild_id=7,
            receiver=receiver,
            stt_mode="openai_live_high",
            guild=MagicMock(),
        )
    )
    await first_started.wait()
    receiver.pause()
    receiver.resume()
    release_first.set()
    await tick

    assert controller.appended == [(42, b"first")]


@pytest.mark.asyncio
async def test_listener_tick_rechecks_kill_switch_before_each_live_append():
    adapter = _adapter()
    controller = FakeLiveController({"success": True, "transcript": "unused"})
    adapter._voice_live_transcribers[7] = controller
    adapter._is_allowed_user = lambda user_id, **kwargs: True
    receiver = MagicMock()
    receiver.drain_stream_chunks.return_value = [
        (0, 100, 1, 42, b"first"),
        (0, 101, 1, 43, b"second"),
    ]
    receiver.stream_chunk_is_current.return_value = True
    receiver.check_silence.return_value = []

    with patch(
        "plugins.platforms.discord.adapter._read_runtime_stt_enabled",
        side_effect=[True, True, False],
    ):
        await adapter._process_voice_listener_tick(
            guild_id=7,
            receiver=receiver,
            stt_mode="openai_live_high",
            guild=MagicMock(),
        )

    assert controller.appended == [(42, b"first")]
    assert controller.closed == 1
    assert 7 not in adapter._voice_live_transcribers
    receiver.discard_pending.assert_called_once_with()


@pytest.mark.asyncio
async def test_listener_tick_discards_stream_chunks_for_contextual_mode():
    adapter = _adapter()
    adapter._is_allowed_user = lambda user_id, **kwargs: True
    adapter._process_completed_voice_utterance = AsyncMock()
    receiver = MagicMock()
    receiver.drain_stream_chunks.return_value = [
        (0, 100, 1, 42, b"unused live chunk")
    ]
    receiver.check_silence.return_value = [(42, b"full utterance")]

    await adapter._process_voice_listener_tick(
        guild_id=7,
        receiver=receiver,
        stt_mode="openai_contextual",
        guild=MagicMock(),
    )

    adapter._process_completed_voice_utterance.assert_awaited_once_with(
        7,
        42,
        b"full utterance",
        stt_mode="openai_contextual",
    )


@pytest.mark.asyncio
async def test_activating_new_mode_closes_existing_live_controller():
    adapter = _adapter()
    adapter._voice_stt_modes = {7: "openai_live_high"}
    old = FakeLiveController({"success": True, "transcript": "old"})
    adapter._voice_live_transcribers[7] = old

    await adapter._activate_voice_stt_mode(
        7,
        "openai_contextual",
        {},
    )

    assert old.closed == 1
    assert adapter._voice_stt_modes[7] == "openai_contextual"
    assert 7 not in adapter._voice_live_transcribers


@pytest.mark.asyncio
async def test_reactivating_same_live_mode_replaces_controller_to_apply_new_config():
    adapter = object.__new__(DiscordAdapter)
    old_controller = FakeLiveController({"success": True, "transcript": "old"})
    new_controller = MagicMock()
    adapter._voice_live_transcribers = {7: old_controller}
    adapter._voice_stt_modes = {7: "openai_live_high"}
    config = {
        "discord": {
            "voice_stt": {
                "openai_live": {
                    "delay": "high",
                    "max_session_seconds": 3200,
                }
            }
        }
    }

    with (
        patch(
            "plugins.platforms.discord.live_transcription.resolve_openai_realtime_api_key",
            return_value="test-key",
        ),
        patch(
            "plugins.platforms.discord.live_transcription.DiscordLiveTranscriptionController",
            return_value=new_controller,
        ),
    ):
        await adapter._activate_voice_stt_mode(
            7,
            "openai_live_high",
            config,
        )

    assert old_controller.closed == 1
    assert adapter._voice_live_transcribers[7] is new_controller


@pytest.mark.asyncio
async def test_join_does_not_replace_latched_mode_while_already_connected():
    adapter = _adapter()
    adapter._client = MagicMock()
    adapter._voice_locks = {}
    adapter._voice_clients = {}
    adapter._voice_stt_modes = {}
    adapter._activate_voice_stt_mode = AsyncMock()
    adapter._reset_voice_timeout = MagicMock()
    existing = MagicMock()
    existing.is_connected.return_value = True
    existing.channel.id = 70
    adapter._voice_clients[7] = existing
    channel = MagicMock()
    channel.id = 70
    channel.guild.id = 7
    config = {"discord": {"voice_stt": {"mode": "openai_contextual"}}}

    with patch(
        "plugins.platforms.discord.adapter.DISCORD_AVAILABLE", True
    ), patch(
        "plugins.platforms.discord.adapter._read_discord_stt_settings",
        return_value=("openai_contextual", config),
    ):
        result = await adapter.join_voice_channel(channel)

    assert result is True
    adapter._activate_voice_stt_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_join_latches_selected_mode_before_receiver_start():
    adapter = _adapter()
    adapter._client = MagicMock()
    adapter._voice_locks = {}
    adapter._voice_clients = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_fx_cfg = {"enabled": False}
    adapter._allowed_user_ids = {"42"}
    adapter._activate_voice_stt_mode = AsyncMock()
    adapter._reset_voice_timeout = MagicMock()
    voice_client = MagicMock()
    channel = MagicMock()
    channel.guild.id = 7
    channel.connect = AsyncMock(return_value=voice_client)
    config = {"discord": {"voice_stt": {"mode": "openai_contextual"}}}

    def consume_coroutine(coroutine):
        coroutine.close()
        return MagicMock()

    with (
        patch("plugins.platforms.discord.adapter.DISCORD_AVAILABLE", True),
        patch(
            "plugins.platforms.discord.adapter._read_discord_stt_settings",
            return_value=("openai_contextual", config),
        ),
        patch("plugins.platforms.discord.adapter.VoiceReceiver") as receiver_cls,
        patch(
            "plugins.platforms.discord.adapter.asyncio.ensure_future",
            side_effect=consume_coroutine,
        ),
    ):
        assert await adapter.join_voice_channel(channel) is True

    adapter._activate_voice_stt_mode.assert_awaited_once_with(
        7,
        "openai_contextual",
        config,
    )
    receiver_cls.assert_called_once_with(
        voice_client,
        allowed_user_ids={"42"},
    )


@pytest.mark.asyncio
async def test_disconnected_rejoin_discards_old_receiver_listener_and_mode():
    adapter = _adapter()
    adapter._client = MagicMock()
    adapter._voice_locks = {}
    old_client = MagicMock()
    old_client.is_connected.return_value = False
    adapter._voice_clients = {7: old_client}
    old_receiver = MagicMock()
    adapter._voice_receivers = {7: old_receiver}
    old_task = asyncio.create_task(asyncio.sleep(60))
    adapter._voice_listen_tasks = {7: old_task}
    adapter._voice_mixers = {7: MagicMock()}
    timeout_task = MagicMock()
    adapter._voice_timeout_tasks = {7: timeout_task}
    adapter._voice_text_channels = {7: 70}
    adapter._voice_sources = {7: {"channel_id": "70"}}
    adapter._voice_stt_modes = {7: "openai_live_high"}
    adapter._voice_live_transcribers = {7: MagicMock()}
    adapter._close_voice_stt_mode = AsyncMock()
    adapter._activate_voice_stt_mode = AsyncMock()
    adapter._reset_voice_timeout = MagicMock()
    adapter._voice_fx_cfg = {"enabled": False}
    adapter._allowed_user_ids = {"42"}
    new_client = MagicMock()
    channel = MagicMock()
    channel.guild.id = 7
    channel.connect = AsyncMock(return_value=new_client)

    def consume_coroutine(coroutine):
        coroutine.close()
        return MagicMock()

    with (
        patch("plugins.platforms.discord.adapter.DISCORD_AVAILABLE", True),
        patch(
            "plugins.platforms.discord.adapter._read_discord_stt_settings",
            return_value=("openai_contextual", {}),
        ),
        patch("plugins.platforms.discord.adapter.VoiceReceiver"),
        patch(
            "plugins.platforms.discord.adapter.asyncio.ensure_future",
            side_effect=consume_coroutine,
        ),
    ):
        assert await adapter.join_voice_channel(channel) is True

    assert old_task.cancelled()
    old_receiver.stop.assert_called_once_with()
    adapter._close_voice_stt_mode.assert_awaited_once_with(7)
    timeout_task.cancel.assert_called_once_with()
    assert adapter._voice_clients[7] is new_client
    adapter._activate_voice_stt_mode.assert_awaited_once_with(
        7,
        "openai_contextual",
        {},
    )


@pytest.mark.asyncio
async def test_real_listen_loop_calls_mode_aware_tick():
    adapter = _adapter()
    receiver = MagicMock()
    receiver._running = True
    adapter._voice_receivers = {7: receiver}
    adapter._voice_stt_modes = {7: "openai_live_high"}
    adapter._voice_clients = {}
    adapter._client = None

    async def one_tick(**kwargs):
        receiver._running = False

    adapter._process_voice_listener_tick = AsyncMock(side_effect=one_tick)
    with patch("asyncio.sleep", new=AsyncMock()):
        await adapter._voice_listen_loop(7)

    adapter._process_voice_listener_tick.assert_awaited_once_with(
        guild_id=7,
        receiver=receiver,
        stt_mode="openai_live_high",
        guild=None,
    )


@pytest.mark.asyncio
async def test_leave_closes_latched_live_mode_without_receiver():
    adapter = _adapter()
    adapter._voice_locks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_mixers = {}
    adapter._voice_clients = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._client = None
    adapter._close_voice_stt_mode = AsyncMock()

    await adapter.leave_voice_channel(7)

    adapter._close_voice_stt_mode.assert_awaited_once_with(7)
