"""The landing page is a static, offline bundle: check the parts that must not regress."""
import re
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"


def _html() -> str:
    return (SITE / "index.html").read_text()


def test_illustrations_are_labelled_and_offline():
    html = _html()
    arts = re.findall(r'<svg class="art"[^>]*>', html)
    assert len(arts) == 4, "GPU, DIMM, PSU and the whole node"
    for tag in arts:
        assert 'role="img"' in tag and 'aria-label="' in tag, tag   # not decorative: name it
    # the page fetches nothing off this origin
    for attr in ('src="http', 'href="http://', "url(http", "@import"):
        assert attr not in html, attr
    assert "@import" not in (SITE / "style.css").read_text()


def test_card_icons_are_decorative():
    icons = re.findall(r'<svg class="ico"[^>]*>', _html())
    assert len(icons) == 9
    assert all('aria-hidden="true"' in t for t in icons)


def test_illustration_facts_match_the_sample_manifest():
    """The labels claim 8 GPUs, 2 CPUs, 16 DIMMs and 4 PSUs: keep them tied to the real manifest."""
    import json
    from collections import Counter
    root = SITE.parent / "examples" / "gpu-node" / "v1.1"
    manifest = json.loads(next(root.glob("*.node.json")).read_text())
    n = Counter(c["type"] for c in manifest["components"])
    assert (n["gpu"], n["cpu"], n["dimm"], n["psu"]) == (8, 2, 16, 4)
    assert "8 GPU &middot; 2 CPU &middot; 16 DIMM &middot; 4 PSU" in _html()
