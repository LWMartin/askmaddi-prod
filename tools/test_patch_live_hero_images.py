"""Live hero-image patch: swap the CURRENT eBay image URL -> self-hosted copy,
in place, touching nothing else. Drift-proof (replaces whatever eBay URL is live,
not a recorded one) and idempotent.

    python -m pytest tools/test_patch_live_hero_images.py -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import patch_live_hero_images as p  # noqa: E402


def _setup(tmp_path, live_img_url):
    (tmp_path / "cards" / "cam").mkdir(parents=True)
    (tmp_path / "cards" / "cam" / "index.html").write_text(
        f'<meta property="og:image" content="{live_img_url}">\n'
        f'<script>"image": "{live_img_url}"</script>\n'
        f'<img class="hero-product-img" src="{live_img_url}">\n'
        f'<a href="https://www.ebay.com/sch/i.html?_nkw=cam">from $9 used</a>\n',
        encoding="utf-8")
    reg = tmp_path / "selfhost_images.json"
    reg.write_text(json.dumps({"images": {"cam": {"file": "images/heroes/cam.jpg"}}}))
    return reg


def test_swaps_all_image_slots_but_not_affiliate_link(tmp_path):
    reg = _setup(tmp_path, "https://i.ebayimg.com/images/g/AAA/s-l1600.jpg")
    p.run(str(reg), str(tmp_path), base_url="https://askmaddi.com")
    html = (tmp_path / "cards" / "cam" / "index.html").read_text()
    assert "i.ebayimg.com" not in html                      # every product-image slot swapped
    assert html.count("https://askmaddi.com/images/heroes/cam.jpg") == 3
    assert "ebay.com/sch" in html                           # affiliate CTA untouched


def test_drift_proof_replaces_current_live_url(tmp_path):
    # Live URL differs from any 'recorded' one — must still be caught.
    reg = _setup(tmp_path, "https://i.ebayimg.com/images/g/DRIFTED/s-l1600.jpg")
    p.run(str(reg), str(tmp_path))
    assert "i.ebayimg.com" not in (tmp_path / "cards" / "cam" / "index.html").read_text()


def test_idempotent(tmp_path):
    reg = _setup(tmp_path, "https://i.ebayimg.com/images/g/AAA/s-l1600.jpg")
    p.run(str(reg), str(tmp_path))
    once = (tmp_path / "cards" / "cam" / "index.html").read_text()
    p.run(str(reg), str(tmp_path))                          # second run: nothing left to do
    assert once == (tmp_path / "cards" / "cam" / "index.html").read_text()
