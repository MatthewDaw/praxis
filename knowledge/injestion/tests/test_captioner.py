"""U4: captioner wiring — cassette replay, graceful live failure, canonical-only."""

from __future__ import annotations

from knowledge.injestion.image.captioner import make_captioner, vlm_caption
from knowledge.injestion.injestor_variants.image_injestor import ImageIngestor
from knowledge.llm.caption_cassette import CaptionCassette


def _png_bytes(color=(1, 2, 3), size=(8, 8)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_vlm_caption_builds_vision_payload():
    seen: dict = {}

    def fake_complete(messages, model):
        seen["messages"] = messages
        seen["model"] = model
        return "  a red square  "

    out = vlm_caption(_png_bytes(), model="m/vlm", complete=fake_complete)
    assert out == "a red square"  # stripped
    assert seen["model"] == "m/vlm"
    content = seen["messages"][0]["content"]
    assert any(part["type"] == "text" for part in content)
    img = next(p for p in content if p["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


def test_cassette_hit_skips_live_call(tmp_path):
    png = _png_bytes()
    from knowledge.injestion.image import hashing

    cas = CaptionCassette(
        tmp_path / "c.json", model_id="m/vlm", prompt="caption prompt", allow_compute=True
    )
    cas.caption(hashing.content_hash(png), lambda: "cached caption")

    calls: list[int] = []

    def fake_complete(messages, model):
        calls.append(1)
        return "live"

    cap = make_captioner(model="m/vlm", cassette=cas, complete=fake_complete)
    assert cap(png) == "cached caption"
    assert calls == []  # served from cassette, no live call


def test_live_failure_degrades_to_none(tmp_path):
    def boom(messages, model):
        raise RuntimeError("429 rate limited")

    # production path: no cassette, has_key True, live call raises -> None
    cap = make_captioner(model="m/vlm", cassette=None, has_key=True, complete=boom)
    assert cap(_png_bytes()) is None


def test_no_key_no_cassette_returns_none():
    cap = make_captioner(model="m/vlm", cassette=None, has_key=False)
    assert cap(_png_bytes()) is None


def test_only_canonical_captioned_per_cluster(tmp_path):
    from PIL import Image

    # near-duplicate cluster: one image + a resize of it
    base = tmp_path / "big.png"
    Image.new("RGB", (40, 40), (255, 255, 255)).save(base)
    with Image.open(base) as im:
        # draw structure so pHash is stable, then resize a copy
        pass
    from PIL import ImageDraw

    img = Image.new("RGB", (40, 40), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([5, 5, 20, 20], fill=(0, 0, 0))
    img.save(tmp_path / "big.png")
    img.resize((20, 20)).save(tmp_path / "small.png")

    calls: list[int] = []

    def counting_complete(messages, model):
        calls.append(1)
        return f"caption {len(calls)}"

    cap = make_captioner(model="m/vlm", cassette=None, has_key=True, complete=counting_complete)

    class SpyGraph:
        def write(self, content, *, state="proposed"):
            pass

        def read(self, context=None):
            return ""

    insights = ImageIngestor(SpyGraph(), captioner=cap).synthesis(str(tmp_path))
    assert len(insights) == 1  # collapsed to one cluster
    assert len(calls) == 1  # only the canonical image was captioned
    assert "caption 1" in insights[0].raw_text
