"""Tests for indexnow_ping — key discovery, sitemap parsing, payload, soft-fail."""
import json

import pytest

import indexnow_ping


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://askmaddi.com/</loc></url>
  <url><loc>https://askmaddi.com/cards/sony-a7iv/</loc><lastmod>2026-07-16</lastmod></url>
</urlset>
"""


def _site(tmp_path, keys=("a" * 32,)):
    for k in keys:
        (tmp_path / f"{k}.txt").write_text(k)
    (tmp_path / "sitemap.xml").write_text(SITEMAP)
    return tmp_path


def test_find_key_single(tmp_path):
    _site(tmp_path)
    assert indexnow_ping.find_key(tmp_path) == "a" * 32


def test_find_key_ignores_non_key_txt(tmp_path):
    _site(tmp_path)
    (tmp_path / "robots.txt").write_text("User-agent: *")
    (tmp_path / "llms.txt").write_text("# AskMaddi")
    assert indexnow_ping.find_key(tmp_path) == "a" * 32


def test_find_key_zero_or_multiple_loud(tmp_path):
    (tmp_path / "sitemap.xml").write_text(SITEMAP)
    with pytest.raises(SystemExit):
        indexnow_ping.find_key(tmp_path)
    _site(tmp_path, keys=("a" * 32, "b" * 32))
    with pytest.raises(SystemExit):
        indexnow_ping.find_key(tmp_path)


def test_sitemap_urls(tmp_path):
    _site(tmp_path)
    urls = indexnow_ping.sitemap_urls(tmp_path / "sitemap.xml")
    assert urls == ["https://askmaddi.com/",
                    "https://askmaddi.com/cards/sony-a7iv/"]


def test_submit_dry_run_payload(tmp_path, capsys):
    _site(tmp_path)
    key = indexnow_ping.find_key(tmp_path)
    urls = indexnow_ping.sitemap_urls(tmp_path / "sitemap.xml")
    assert indexnow_ping.submit(urls, key, dry_run=True) is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["host"] == "askmaddi.com"
    assert payload["key"] == key
    assert payload["keyLocation"] == f"https://askmaddi.com/{key}.txt"
    assert payload["urlList"] == urls


def test_main_soft_fails_on_network(tmp_path, monkeypatch, capsys):
    _site(tmp_path)

    def boom(*a, **kw):
        raise OSError("no network in sandbox")
    monkeypatch.setattr(indexnow_ping, "submit", boom)
    monkeypatch.setattr("sys.argv",
                        ["indexnow_ping.py", "--browser-dir", str(tmp_path)])
    assert indexnow_ping.main() == 0          # soft by default
    assert "soft-fail" in capsys.readouterr().err


def test_main_strict_fails_on_network(tmp_path, monkeypatch):
    _site(tmp_path)

    def boom(*a, **kw):
        raise OSError("no network")
    monkeypatch.setattr(indexnow_ping, "submit", boom)
    monkeypatch.setattr("sys.argv",
                        ["indexnow_ping.py", "--browser-dir", str(tmp_path),
                         "--strict"])
    assert indexnow_ping.main() == 1
