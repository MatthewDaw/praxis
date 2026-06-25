"""U2: ImageIngestor + deterministic card generation."""

from __future__ import annotations

from knowledge.injestion.image.cards import build_card
from knowledge.injestion.injestor_variants.image_injestor import ImageIngestor


class SpyGraph:
    """Records every (content, state) write so tests can assert lifecycle + payload.

    Accepts (and ignores) the optional provenance/metadata kwargs the ingestor
    threads through (``source``/``scope``/``category``/``meta``/``tabular``) so the
    double matches the real graph's ``write`` signature.
    """

    def __init__(self):
        self.writes: list[tuple[str, str]] = []

    def write(self, content: str, *, state: str = "proposed", **_: object) -> None:
        self.writes.append((content, state))

    def read(self, context=None) -> str:
        return "\n\n".join(c for c, _ in self.writes)


def _png(path, size=(8, 6), color=(10, 20, 30)):
    from PIL import Image

    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def _distinct_png(path, seed, size=(32, 32)):
    """A structurally-distinct image (solid colors share a pHash; structure differs)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    x = (seed * 7) % (size[0] - 8)
    y = (seed * 11) % (size[1] - 8)
    d.rectangle([x, y, x + 8, y + 8], fill=(0, 0, 0))
    d.line([0, seed % size[1], size[0], (seed * 3) % size[1]], fill=(0, 0, 0), width=2)
    img.save(path, format="PNG")
    return path


# --- card builder ---------------------------------------------------------- #
def test_build_card_includes_path_token_and_fields():
    card = build_card(
        asset_path="assets/mascot.png",
        folder="Common",
        dims=(64, 64),
        layer_names=["body", "cap"],
    )
    assert "asset: mascot" in card
    assert "folder: Common" in card
    assert "dims: 64x64" in card
    assert "layers: body, cap" in card
    assert "path=assets/mascot.png" in card


def test_build_card_omits_empty_fields():
    card = build_card(asset_path="assets/x.png", folder=".", dims=(1, 1))
    assert "folder:" not in card  # "." taxonomy dropped
    assert "layers:" not in card
    assert "caption:" not in card


# --- ingestor synthesis ---------------------------------------------------- #
def test_two_pngs_yield_two_cards_with_provenance(tmp_path):
    _distinct_png(tmp_path / "a.png", seed=1)
    _distinct_png(tmp_path / "b.png", seed=2)
    ing = ImageIngestor(SpyGraph())
    insights = ing.synthesis(str(tmp_path))

    assert len(insights) == 2
    for ins in insights:
        assert ins.category == "asset"
        assert ins.source.startswith("asset:")
        # source = asset:<sha256>:<relpath>
        _, sha, relpath = ins.source.split(":", 2)
        assert len(sha) == 64
        assert relpath in {"a.png", "b.png"}
        assert f"path=assets/{relpath}" in ins.raw_text


def test_folder_taxonomy_in_card(tmp_path):
    sub = tmp_path / "Common"
    sub.mkdir()
    _png(sub / "logo.png")
    ing = ImageIngestor(SpyGraph())
    [ins] = ing.synthesis(str(tmp_path))
    assert "folder: Common" in ins.raw_text
    assert "path=assets/Common/logo.png" in ins.raw_text


def test_ingest_writes_active_by_default(tmp_path):
    _png(tmp_path / "a.png")
    graph = SpyGraph()
    ImageIngestor(graph).ingest(str(tmp_path))
    assert graph.writes, "expected at least one write"
    assert all(state == "active" for _, state in graph.writes)


def test_unknown_files_skipped(tmp_path):
    _png(tmp_path / "a.png")
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "project.aep").write_bytes(b"binary junk")
    ing = ImageIngestor(SpyGraph())
    insights = ing.synthesis(str(tmp_path))
    assert len(insights) == 1
    assert "a.png" in insights[0].source


def test_empty_folder_yields_nothing(tmp_path):
    ing = ImageIngestor(SpyGraph())
    assert ing.synthesis(str(tmp_path)) == []


def test_layer_names_from_psd_appear_in_card(tmp_path, monkeypatch):
    from PIL import Image

    import psd_tools
    from knowledge.injestion.image.normalize import NormalizedAsset

    class _Fake:
        def __init__(self, names, thumb):
            self._names, self._thumb = names, thumb
            self.width, self.height = thumb.size

        def descendants(self):
            return [type("L", (), {"name": n})() for n in self._names]

        def thumbnail(self):
            return self._thumb

        def composite(self):
            return self._thumb

    thumb = Image.new("RGBA", (4, 4), (0, 0, 0, 255))
    monkeypatch.setattr(
        psd_tools.PSDImage,
        "open",
        classmethod(lambda cls, p: _Fake(["mascot_body", "glow"], thumb)),
    )
    (tmp_path / "art.psd").write_bytes(b"fake")
    ing = ImageIngestor(SpyGraph())
    [ins] = ing.synthesis(str(tmp_path))
    assert "layers: mascot_body, glow" in ins.raw_text
    assert isinstance(NormalizedAsset, type)  # import sanity
