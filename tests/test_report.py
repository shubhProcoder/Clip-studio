import csv
import re

from streamclipper.report import CSV_COLUMNS, write_report


def _doc():
    bd = {"hook": 60, "story": 70, "emotion": 80, "pacing": 40, "engagement": None}
    clips = [
        {
            "rank": r,
            "start": 10.0 * r,
            "end": 10.0 * r + 25,
            "duration": 25.0,
            "score": 90 - r,
            "breakdown": bd,
            "category": "clutch" if r == 1 else "funny",
            "title": f"Clip title {r} <b>&</b>",
            "hook_headline": "NO WAY BRO",
            "reason": "voice 18 dB above the stream's usual level",
            "hashtags": ["#gta6"],
            "mp4": "render/clip 01.mp4" if r == 1 else None,
            "thumb": "render/clip 01_thumb.jpg" if r == 1 else None,
            "peak": 10.0 * r + 20,
        }
        for r in (1, 2, 3)
    ]
    return {"title": "Test stream", "duration": 600.0, "language": "en", "settings": {"min_score": 50}, "clips": clips}


def test_csv_columns_and_bom(tmp_path):
    csv_p, _ = write_report(tmp_path, _doc())
    raw = csv_p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert len(rows) == 4
    first = dict(zip(rows[0], rows[1], strict=True))
    assert first["rank"] == "1" and first["category"] == "clutch" and first["engagement"] == ""
    assert first["mp4"] == "render/clip 01.mp4"


def test_html_lists_every_clip_without_external_assets(tmp_path):
    _, html_p = write_report(tmp_path, _doc())
    page = html_p.read_text(encoding="utf-8")
    assert "http://" not in page and "https://" not in page
    assert "<script" not in page.lower()
    for r in (1, 2, 3):
        assert f"Clip title {r} &lt;b&gt;&amp;&lt;/b&gt;" in page
    assert page.index("Clip title 1") < page.index("Clip title 2") < page.index("Clip title 3")
    assert 'href="render/clip%2001.mp4"' in page
    assert re.search(r'<span class="cat">funny</span>', page)
