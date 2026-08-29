"""G5 (29/08): mid-turn compaction must not eat the current turn's media.

Incident 28/08 17:17 DOVCRM: a compaction fired in the middle of a video
analysis; pass 3.5's global keep-newest-3 retired most of the turn's
storyboard sheets and the analysis lost its evidence.  The fix protects
tool media generated after the last user message (the current turn) up
to ``_CURRENT_TURN_MEDIA_TOKEN_CAP`` tokens; above the cap the OLDEST
in-turn media degrades first.  History keeps the legacy keep-newest
behaviour, and current-turn media never counts against that window.
"""

from __future__ import annotations

from agent.context_compressor import (
    ContextCompressor,
    _CURRENT_TURN_MEDIA_TOKEN_CAP,
    _IMAGE_TOKEN_ESTIMATE,
    _MAX_KEEP_TOOL_IMAGES,
    _last_user_index,
    _retire_stale_tool_result_images,
    _tool_content_has_images,
)


def _compressor() -> ContextCompressor:
    c = ContextCompressor.__new__(ContextCompressor)
    c.quiet_mode = True
    return c


def _image_tool(i: int, *, prefix: str = "call") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"{prefix}_{i}",
                    "type": "function",
                    "function": {
                        "name": "vision_analyze",
                        "arguments": f'{{"image_url":"sheet{i}.png"}}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"{prefix}_{i}",
            "content": [
                {"type": "text", "text": f"storyboard sheet {i}"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{'A' * 400}{i}"},
                },
            ],
        },
    ]


def _live_ids(messages: list[dict]) -> list[str]:
    return [
        m["tool_call_id"]
        for m in messages
        if m.get("role") == "tool" and _tool_content_has_images(m.get("content"))
    ]


class TestReproductionIncidentDovcrm:
    def test_legacy_global_cap_eats_current_turn_media(self):
        """Frozen reproduction of the 28/08 incident mechanism.

        Without the current-turn marker (pre-fix behaviour) a turn with 8
        storyboard sheets keeps only ``_MAX_KEEP_TOOL_IMAGES`` of them —
        the video analysis loses most of its frames mid-turn.
        """
        msgs: list[dict] = [{"role": "user", "content": "analyze this video"}]
        for i in range(8):
            msgs.extend(_image_tool(i))

        _retire_stale_tool_result_images(msgs, current_turn_start_idx=None)
        assert len(_live_ids(msgs)) == _MAX_KEEP_TOOL_IMAGES

    def test_current_turn_media_survives_with_marker(self):
        """Same transcript, marker on: every in-turn sheet stays live."""
        msgs: list[dict] = [{"role": "user", "content": "analyze this video"}]
        for i in range(8):
            msgs.extend(_image_tool(i))

        pruned = _retire_stale_tool_result_images(
            msgs, current_turn_start_idx=_last_user_index(msgs)
        )
        assert pruned == 0
        assert len(_live_ids(msgs)) == 8


class TestCurrentTurnMediaThroughPrune:
    def test_history_retires_but_current_turn_survives(self):
        """Integration through _prune_old_tool_results.

        5 sheets from a previous turn + 5 sheets of the current turn:
        the current turn's media all stays live and never counts against
        the history keep-window (which keeps its newest
        ``_MAX_KEEP_TOOL_IMAGES``).
        """
        msgs: list[dict] = [{"role": "user", "content": "old ask"}]
        for i in range(5):
            msgs.extend(_image_tool(i, prefix="old"))
        msgs.append({"role": "user", "content": "now analyze this video"})
        for i in range(5):
            msgs.extend(_image_tool(i, prefix="cur"))

        out, _ = _compressor()._prune_old_tool_results(
            msgs, protect_tail_count=50,
        )
        live = _live_ids(out)
        assert [x for x in live if x.startswith("cur_")] == [
            f"cur_{i}" for i in range(5)
        ]
        assert [x for x in live if x.startswith("old_")] == [
            f"old_{i}" for i in range(5 - _MAX_KEEP_TOOL_IMAGES, 5)
        ]

    def test_over_cap_degrades_oldest_in_turn_first(self):
        """Above the token cap the oldest in-turn media goes first.

        The cap is in TOKENS (never message count); the assert checks
        monotonicity — every surviving sheet is newer than every
        degraded one — instead of pinning the estimator's calibration.
        """
        n = (_CURRENT_TURN_MEDIA_TOKEN_CAP // _IMAGE_TOKEN_ESTIMATE) + 6
        msgs: list[dict] = [{"role": "user", "content": "analyze this long video"}]
        for i in range(n):
            msgs.extend(_image_tool(i, prefix="cur"))

        pruned = _retire_stale_tool_result_images(
            msgs, current_turn_start_idx=_last_user_index(msgs)
        )
        assert pruned > 0
        live = sorted(int(x.split("_")[1]) for x in _live_ids(msgs))
        assert live, "cap must keep the newest frames live, not strip all"
        assert live[-1] == n - 1
        # Monotonic: survivors form a contiguous newest suffix.
        assert live == list(range(live[0], n))

    def test_pressure_pass_demotes_text_before_current_turn_media(self):
        """Pass 4 (#61932 pressure): current-turn media demotes LAST.

        A huge completed read_file body inside the protected region must
        be demoted before the storyboard sheet the turn is analyzing;
        once the soft ceiling is met the media stays live.
        """

        def _text_pair(i: int, chars: int) -> list[dict]:
            return [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"txt_{i}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": f'{{"path":"f{i}.py"}}',
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"txt_{i}",
                    "content": f"FILE_{i}\n" + (f"unique line {i} " * (chars // 15)),
                },
            ]

        msgs: list[dict] = [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "analyze this video"},
        ]
        for i in range(4):
            msgs.extend(_text_pair(i, 2000))
        msgs.extend(_text_pair(90, 60000))  # the demotable bulk
        msgs.extend(_image_tool(0, prefix="cur"))
        msgs.append({"role": "assistant", "content": "analysing the sheet"})

        out, _ = _compressor()._prune_old_tool_results(
            msgs, protect_tail_count=50, protect_tail_tokens=6000,
        )
        assert _live_ids(out) == ["cur_0"]
        bulk = next(m for m in out if m.get("tool_call_id") == "txt_90")
        assert "unique line 90" not in str(bulk.get("content"))

    def test_no_user_message_keeps_legacy_behavior(self):
        msgs: list[dict] = []
        for i in range(_MAX_KEEP_TOOL_IMAGES + 2):
            msgs.extend(_image_tool(i))
        assert _last_user_index(msgs) is None
        _retire_stale_tool_result_images(
            msgs, current_turn_start_idx=_last_user_index(msgs)
        )
        assert len(_live_ids(msgs)) == _MAX_KEEP_TOOL_IMAGES
