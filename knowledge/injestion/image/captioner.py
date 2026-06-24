"""VLM "reverse prompt": caption a canonical image to text, cached by content hash.

The caption is the rich, human-readable half of an asset card — it produces
*text*, so it flows through the existing text-embed + dedup path with no new
modality. Cost is controlled by the throughput layer, not by avoiding captions:
caption once per unique image (content-hash keyed), only the canonical image of
each variant cluster, and replay from a committed cassette offline.

Two failure postures, deliberately different:

- **Cassette miss with recording disabled** (eval, offline) → loud error from the
  cassette: a stale fixture must fail, not silently degrade.
- **Live VLM call failure** (production, no cassette) → graceful ``None``: the
  asset still lands with its deterministic card. One bad call never aborts a dump.
"""

from __future__ import annotations

import base64
import logging
from typing import Callable

from knowledge.injestion.image import hashing

logger = logging.getLogger(__name__)

CAPTION_PROMPT = (
    "You are describing a single visual asset for a knowledge graph. In one or two "
    "plain sentences, state concretely what the image depicts: its subject, art style "
    "(e.g. pixel art, oil portrait, engraving, flat illustration), and dominant colors. "
    "Do not add commentary, markdown, or quotation marks — return only the description."
)

# A chat-completion seam: (messages, model) -> assistant text. Injectable for tests.
Complete = Callable[[list[dict], str], str]


def _default_complete(messages: list[dict], model: str) -> str:
    from knowledge.llm import openrouter_http

    return openrouter_http.chat_complete(messages, model=model)


def _vision_messages(png_bytes: bytes) -> list[dict]:
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": CAPTION_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]


def vlm_caption(png_bytes: bytes, *, model: str, complete: Complete | None = None) -> str:
    """Caption a single canonical PNG via a vision model. May raise on failure."""
    complete = complete or _default_complete
    return (complete(_vision_messages(png_bytes), model)).strip()


def make_captioner(
    *,
    model: str,
    cassette=None,
    has_key: bool = False,
    complete: Complete | None = None,
):
    """Build a ``Captioner`` (png_bytes -> caption | None) honoring cassette/live wiring.

    - With a cassette: replay/record/loud-miss per :class:`CaptionCassette`.
    - Without a cassette (production): call the live VLM when ``has_key``, and
      degrade to ``None`` on any failure.
    """

    def compute_for(png_bytes: bytes) -> str:
        return vlm_caption(png_bytes, model=model, complete=complete)

    def captioner(png_bytes: bytes) -> str | None:
        payload = hashing.content_hash(png_bytes)
        if cassette is not None:
            return cassette.caption(payload, lambda: compute_for(png_bytes))
        if not has_key:
            return None
        try:
            return compute_for(png_bytes)
        except Exception as exc:  # noqa: BLE001 - graceful degradation is the contract
            logger.warning("caption: live VLM call failed (%s); deterministic-only card", exc)
            return None

    return captioner
