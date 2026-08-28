"""Busy-steer payloads keep attachments bound to their message.

TARGET_ARCHITECTURE gap 2 (27/08): a voice follow-up whose STT failed used
to produce an empty steer payload — the placeholder skipped STT inputs
unconditionally — so steer silently demoted to queue mode and the attachment
detached from the turn it was aimed at.
"""

from __future__ import annotations

import asyncio

from gateway.run import GatewayRunner, SessionSource, MessageEvent
from gateway.config import Platform
from gateway.platforms.base import MessageType


def _runner(stt_transcripts):
    """GatewayRunner stub: STT outcome is scripted, no adapter needed."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._adapter_for_source = lambda source: None

    async def _fake_stt(event, adapter, source, text, log_context=""):
        if stt_transcripts:
            joined = " ".join(stt_transcripts)
            merged = f"{text}\n\n[Voice transcript: {joined}]".strip()
            return merged, list(stt_transcripts)
        return text, []

    runner._transcribe_and_echo_pending_voice = _fake_stt
    return runner


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="supergroup",
        thread_id="4",
        user_id="996979567",
    )


def _voice_event(path="/srv/hermes/media/voice_1.ogg"):
    return MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=_source(),
        media_urls=[path],
        media_types=["audio/ogg"],
    )


def test_voice_with_failed_stt_keeps_attachment_in_steer_payload():
    runner = _runner(stt_transcripts=[])
    steer_text = asyncio.run(runner._prepare_busy_steer_text(_voice_event()))

    assert steer_text, "STT failure must not empty the steer payload"
    assert "/srv/hermes/media/voice_1.ogg" in steer_text


def test_voice_with_successful_stt_carries_transcript_not_placeholder():
    runner = _runner(stt_transcripts=["sobe o fix do painel"])
    steer_text = asyncio.run(runner._prepare_busy_steer_text(_voice_event()))

    assert "sobe o fix do painel" in steer_text
    # The transcript already represents the audio; a placeholder would make
    # the agent open the file again and duplicate the message content.
    assert "[User sent audio:" not in steer_text


def test_caption_and_image_steer_together():
    runner = _runner(stt_transcripts=[])
    event = MessageEvent(
        text="olha esse erro no painel",
        message_type=MessageType.PHOTO,
        source=_source(),
        media_urls=["/srv/hermes/media/img_9.jpg"],
        media_types=["image/jpeg"],
    )
    steer_text = asyncio.run(runner._prepare_busy_steer_text(event))

    assert "olha esse erro no painel" in steer_text
    assert "/srv/hermes/media/img_9.jpg" in steer_text
