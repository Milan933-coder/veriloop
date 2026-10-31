"""Pure ingestion helpers: URL normalisation, platform detection, page meta, frame plan, yt-dlp errors."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from veriloop.contracts import Platform
from veriloop.services.ingestion import ffmpeg, ytdlp
from veriloop.services.ingestion.fetchers import x_syndication_token
from veriloop.services.ingestion.pagemeta import (
    clean_caption,
    oembed_request,
    page_status,
    parse_date,
    parse_page_meta,
    parse_x_oembed_html,
)
from veriloop.services.ingestion.urls import (
    detect_platform,
    extract_urls,
    is_short_link,
    normalize_url,
    oembed_lookup_url,
    text_without_urls,
    youtube_video_id,
)


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://x.com/PIBFactCheck/status/2112600705232249021", Platform.X),
        ("https://twitter.com/a/status/1", Platform.X),
        ("https://www.instagram.com/reel/abc/", Platform.INSTAGRAM),
        ("https://m.facebook.com/reel/1815114716474947", Platform.FACEBOOK),
        ("https://fb.watch/xyz", Platform.FACEBOOK),
        ("https://youtu.be/XeYAGU1Z6l8", Platform.YOUTUBE),
        ("https://www.tiktok.com/@u/video/1", Platform.TIKTOK),
        ("https://t.me/channel/42", Platform.TELEGRAM),
        ("https://wa.me/123", Platform.WHATSAPP),
        ("https://www.thehindu.com/news/a.ece", Platform.WEB),
        ("https://notx.com/a", Platform.WEB),  # suffix match must be on a label boundary
    ],
)
def test_detect_platform(url: str, platform: Platform) -> None:
    assert detect_platform(url) is platform


def test_normalize_strips_tracking_and_canonicalises() -> None:
    assert (
        normalize_url("https://www.youtube.com/shorts/XeYAGU1Z6l8?si=abc&feature=share")
        == "https://www.youtube.com/watch?v=XeYAGU1Z6l8"
    )
    assert normalize_url("youtu.be/XeYAGU1Z6l8?t=12") == "https://www.youtube.com/watch?v=XeYAGU1Z6l8&t=12"
    assert (
        normalize_url("https://twitter.com/PIBFactCheck/status/2112600705232249021/video/1?s=20&t=x")
        == "https://x.com/PIBFactCheck/status/2112600705232249021"
    )
    assert normalize_url("http://instagram.com/reel/abc/?igshid=1") == "https://www.instagram.com/reel/abc/"
    # On arbitrary sites only universal trackers go; `s`/`ref` may be real parameters.
    assert (
        normalize_url("https://example.org/search?s=floods&utm_source=x&fbclid=1#frag")
        == "https://example.org/search?s=floods"
    )


def test_short_links_and_text_urls() -> None:
    assert is_short_link("https://t.co/abc") and is_short_link("https://bit.ly/x")
    assert not is_short_link("https://x.com/a")
    text = "Look at this https://youtu.be/XeYAGU1Z6l8, shocking! (see https://bit.ly/x)."
    assert extract_urls(text) == ["https://youtu.be/XeYAGU1Z6l8", "https://bit.ly/x"]
    assert text_without_urls(text) == "Look at this shocking! (see )."
    assert youtube_video_id("https://www.youtube.com/watch?v=XeYAGU1Z6l8") == "XeYAGU1Z6l8"
    assert youtube_video_id("https://www.youtube.com/watch?v=bad") is None
    assert oembed_lookup_url("https://youtube.com/shorts/XeYAGU1Z6l8").endswith("watch?v=XeYAGU1Z6l8")


def test_oembed_endpoints() -> None:
    assert oembed_request(Platform.YOUTUBE, "u")[0] == "https://www.youtube.com/oembed"  # type: ignore[index]
    endpoint, params = oembed_request(Platform.X, "u")  # type: ignore[misc]
    assert endpoint == "https://publish.twitter.com/oembed" and params["omit_script"] == "1"
    assert oembed_request(Platform.TIKTOK, "u")[0] == "https://www.tiktok.com/oembed"  # type: ignore[index]
    assert oembed_request(Platform.INSTAGRAM, "u") is None  # needs a token: page meta instead


def test_parse_page_meta_and_status() -> None:
    html = """<html><head><title>T</title>
      <meta property="og:title" content="Cyclone &amp; floods">
      <meta property="og:description" content="Video shows the cyclone.">
      <meta property="og:image" content="/img/a.jpg">
      <meta property="article:published_time" content="2026-10-28T10:00:00+05:30">
      <meta property="og:type" content="video.other"></head></html>"""
    meta = parse_page_meta(html, "https://news.example.com/story/1")
    assert meta["title"] == "Cyclone & floods"
    assert meta["image"] == "https://news.example.com/img/a.jpg"
    assert meta["published"].startswith("2026-10-28")
    assert meta["og_type"] == "video.other" and meta["status"] == "ok"
    assert page_status("<title>Just a moment...</title>") == "blocked"
    assert page_status("<title>Login • Instagram</title>") == "login"


def test_json_ld_date_fallback() -> None:
    html = '<script type="application/ld+json">{"@graph":[{"datePublished":"2026-07-06"}]}</script>'
    assert parse_page_meta(html, "https://a.b/")["published"] == "2026-07-06"


def test_parse_x_oembed_html() -> None:
    snippet = (
        '<blockquote class="twitter-tweet"><p lang="en" dir="ltr">DEEPFAKE VIDEO ALERT!<br>Do not share '
        '<a href="https://t.co/x">pic.twitter.com/x</a></p>&mdash; PIB Fact Check (@PIBFactCheck) '
        '<a href="https://twitter.com/x/status/1">October 20, 2026</a></blockquote>'
    )
    out = parse_x_oembed_html(snippet)
    assert out["text"] == "DEEPFAKE VIDEO ALERT!\nDo not share"
    assert out["date_text"] == "October 20, 2026" and out["lang"] == "en"


def test_parse_date_variants() -> None:
    assert parse_date("20260706") == datetime(2026, 7, 6, tzinfo=UTC)
    assert parse_date(0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert parse_date("2923-01-02") is None  # garbage years are rejected, not trusted
    assert parse_date("not a date") is None and parse_date(None) is None
    assert parse_date("2026-10-28T10:00:00Z").tzinfo is not None


def test_clean_caption_drops_boilerplate() -> None:
    text = "Typhoon hits Guam\n\nSubscribe for more!\nhttps://example.com\n-----\nWinds at 200 km/h"
    assert clean_caption(text) == "Typhoon hits Guam\nWinds at 200 km/h"
    assert clean_caption("") is None


def test_frame_plan_short_video_samples_at_fps() -> None:
    plan = ffmpeg.plan_frame_times(10.0, fps=1.0, cap=16)
    assert len(plan.times) == 10 and not plan.capped
    assert plan.times[0] == 0.5 and plan.note() == "Sampled 10 frames of 10 s video"


def test_frame_plan_long_video_is_capped_and_spread(settings) -> None:
    """E8: a 412 s video gets FRAME_CAP frames spread over the whole duration plus a note."""
    plan = ffmpeg.plan_frame_times(412.0, fps=settings.frame_fps, cap=settings.frame_cap)
    assert len(plan.times) == settings.frame_cap == 16 and plan.capped
    assert plan.times[0] > 0 and plan.times[-1] > 380  # spread to the end, not the first 16 s
    assert plan.note() == "Sampled 16 frames of 412 s video"


def test_frame_plan_unknown_duration() -> None:
    plan = ffmpeg.plan_frame_times(None, fps=1.0, cap=16)
    assert plan.times == [0.0] and plan.note() == "Sampled 1 frame (duration unknown)"


def test_parse_probe() -> None:
    info = ffmpeg.parse_probe(
        {
            "format": {
                "duration": "34.0",
                "format_name": "mov,mp4",
                "bit_rate": "900000",
                "tags": {"ENCODER": "Lavf"},
            },
            "streams": [
                {
                    "codec_type": "video",
                    "width": 720,
                    "height": 1280,
                    "avg_frame_rate": "30/1",
                    "codec_name": "h264",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
    )
    assert info.duration_s == 34.0 and info.has_video and info.has_audio
    assert (info.width, info.height, info.fps, info.bit_rate) == (720, 1280, 30.0, 900000)
    assert info.tags["encoder"] == "Lavf"


@pytest.mark.parametrize(
    ("message", "error_type"),
    [
        (
            "ERROR: [Instagram] abc: Requested content is not available, rate-limit reached or login required",  # noqa: E501
            "login_required",
        ),
        ("ERROR: [twitter] 1: No video could be found in this tweet", "no_media"),
        ("ERROR: Unsupported URL: https://example.com", "unsupported"),
        ("ERROR: HTTP Error 404: Not Found", "not_found"),
        ("ERROR: Connection reset by peer", "http_error"),
    ],
)
def test_ytdlp_error_classification(message: str, error_type: str) -> None:
    err = ytdlp.classify_error(message)
    assert err.error_type == error_type
    assert err.retryable is (error_type == "http_error")


def test_x_syndication_token_is_deterministic() -> None:
    token = x_syndication_token("2112600705232249021")
    assert token == x_syndication_token("2112600705232249021") and token.isalnum() and "0" not in token
