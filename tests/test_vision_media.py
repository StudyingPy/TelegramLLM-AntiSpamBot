from __future__ import annotations

import gzip
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from telegram_llm_antispam.vision_media import (  # noqa: E402
    DecodedAsset, Frame, compose_document, decode_asset, distinct_frames,
    document_from_entities, flatten_frame, sample_times,
)


def png(color, size=(64, 64)):
    image = Image.new("RGBA", size, color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def gif():
    images = [Image.new("RGB", (64, 64), color) for color in ("red", "green", "blue", "yellow")]
    out = io.BytesIO()
    images[0].save(out, format="GIF", save_all=True, append_images=images[1:],
                   duration=[100, 100, 100, 100], loop=0)
    return out.getvalue()


def entity(offset, key, length=2):
    return {"type": "custom_emoji", "offset": offset, "length": length, "custom_emoji_id": key}


def test_utf16_entities_preserve_digits_text_and_newlines():
    doc = document_from_entities("1😀甲😀\n2", [entity(1, "a"), entity(4, "b")])
    assert doc.text == "1😀甲😀\n2"
    assert [(run.text, run.emoji_id) for run in doc.runs] == [
        ("1", None), ("😀", "a"), ("甲", None), ("😀", "b"), ("\n2", None),
    ]
    assert not doc.limitations


def test_partial_surrogates_overlapping_and_out_of_bounds_do_not_replace_other_text():
    doc = document_from_entities("😀12", [entity(0, "bad", 1), entity(0, "a"),
                                         entity(0, "overlap"), entity(100, "far")])
    assert [(run.text, run.emoji_id) for run in doc.runs] == [("😀", "a"), ("12", None)]
    assert set(doc.limitations) == {"invalid_emoji_entity", "overlapping_emoji_entity"}


def test_adjacent_emoji_tiles_keep_rows_and_order():
    doc = document_from_entities("😀😀\n😀", [entity(0, "a"), entity(2, "b"), entity(5, "c")])
    assets = {key: DecodedAsset((Frame(png(color)),))
              for key, color in (("a", "red"), ("b", "blue"), ("c", "green"))}
    try:
        result = compose_document(doc, assets, width=128)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    with Image.open(io.BytesIO(result.frames[0].png)) as canvas:
        assert canvas.size == (128, 128)
        assert canvas.getpixel((32, 32)) == (255, 0, 0)
        assert canvas.getpixel((96, 32)) == (0, 0, 255)
        assert canvas.getpixel((32, 96)) == (0, 128, 0)
        assert canvas.getpixel((96, 96)) == (255, 255, 255)


def test_static_and_transparent_images_are_not_prematurely_flattened():
    result = decode_asset(png((255, 255, 255, 128)))
    assert len(result.frames) == 1
    dark = flatten_frame(result.frames[0], dark=True)
    light = flatten_frame(result.frames[0])
    with Image.open(io.BytesIO(dark.png)) as image:
        assert image.getpixel((0, 0)) != (255, 255, 255)
    assert dark.png != light.png


def test_gif_samples_late_content_and_deduplicates_only_equal_frames():
    result = decode_asset(gif(), max_frames=4)
    assert result.duration_ms == 400
    assert len(distinct_frames(result.frames)) == 4
    with Image.open(io.BytesIO(result.frames[-1].png)) as image:
        assert image.getpixel((0, 0))[:3] == (255, 255, 0)
    assert len(distinct_frames((result.frames[0], result.frames[0]))) == 1


@pytest.mark.parametrize("suffix,codec", [(".webm", "libvpx-vp9"), (".mp4", "libx264")])
def test_video_sampling_includes_last_encoded_content(tmp_path, suffix, codec):
    imageio = pytest.importorskip("imageio_ffmpeg")
    source = tmp_path / "source.gif"
    target = tmp_path / ("movie" + suffix)
    source.write_bytes(gif())
    subprocess.run([imageio.get_ffmpeg_exe(), "-v", "error", "-i", str(source),
                    "-c:v", codec, "-pix_fmt", "yuv420p", "-an", str(target)],
                   capture_output=True, timeout=15, check=True)
    result = decode_asset(target.read_bytes(), max_frames=4)
    assert len(result.frames) >= 3
    with Image.open(io.BytesIO(result.frames[-1].png)) as image:
        red, green, blue = image.convert("RGB").getpixel((32, 32))
        assert red > 180 and green > 180 and blue < 80


def test_tgs_native_renderer_reads_animation(tmp_path):
    pytest.importorskip("rlottie_python")
    value = {"v": "5.5.2", "fr": 10, "ip": 0, "op": 20, "w": 64, "h": 64,
             "layers": [{"ty": 1, "ind": 1, "sw": 64, "sh": 64, "sc": "#ff0000",
                         "ip": 0, "op": 20, "ks": {
                             "o": {"a": 1, "k": [
                                 {"t": 0, "s": [0], "e": [100],
                                  "i": {"x": [1], "y": [1]}, "o": {"x": [0], "y": [0]}},
                                 {"t": 19, "s": [100]},
                             ]}, "r": {"a": 0, "k": 0},
                             "p": {"a": 0, "k": [0, 0, 0]},
                             "a": {"a": 0, "k": [0, 0, 0]},
                             "s": {"a": 0, "k": [100, 100, 100]},
                         }}]}
    result = decode_asset(gzip.compress(json.dumps(value).encode()), max_frames=4)
    assert len(result.frames) == 4
    assert result.duration_ms == 2000
    with Image.open(io.BytesIO(result.frames[-1].png)) as image:
        assert image.getpixel((32, 32))[:3] == (255, 0, 0)
        last_alpha = image.getpixel((32, 32))[3]
    with Image.open(io.BytesIO(result.frames[0].png)) as image:
        assert image.getpixel((32, 32))[3] < last_alpha


@pytest.mark.parametrize("raw", [b"", b"unrecognized", b"x" * (4 * 1024 * 1024 + 1)],
                         ids=["empty", "invalid", "oversized"])
def test_invalid_or_oversized_media_is_explicit_failure(raw):
    with pytest.raises((ValueError, OSError)):
        decode_asset(raw)


def test_invalid_tgs_dimensions_are_rejected_before_native_render():
    raw = gzip.compress(json.dumps({"w": -10, "h": -10, "fr": 30, "op": 30}).encode())
    with pytest.raises(ValueError, match="dimensions"):
        decode_asset(raw)


def test_tgs_cannot_reference_external_files():
    raw = gzip.compress(json.dumps({"assets": [{"p": "private.png", "u": "/private/"}]}).encode())
    with pytest.raises(ValueError, match="external_assets"):
        decode_asset(raw)


def test_video_rejects_huge_dimensions_before_frame_decode(monkeypatch):
    pytest.importorskip("imageio_ffmpeg")
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stderr=b"Video: vp9, yuv420p, 5000x5000, 30 fps")

    monkeypatch.setattr(subprocess, "run", probe)
    with pytest.raises(ValueError, match="video_pixel_limit"):
        decode_asset(b"\x1aE\xdf\xa3")
    assert len(calls) == 1


def test_time_sampling_and_frame_at_wrap():
    assert sample_times(1000, 4) == (0, 333, 666, 999)
    assert sample_times(0, 4) == (0,)
    asset = DecodedAsset((Frame(b"a", 0), Frame(b"b", 500)), 1000)
    assert asset.at(1499).png == b"b"


def test_missing_asset_and_font_are_explicit(tmp_path, monkeypatch):
    import telegram_llm_antispam.vision_media as module

    doc = document_from_entities("😀", [entity(0, "missing")])
    try:
        rendered = compose_document(doc, {})
    except RuntimeError:
        pytest.skip("No CJK font installed")
    assert "emoji_asset_unavailable" in rendered.limitations
    monkeypatch.setattr(Path, "is_file", lambda _self: False)
    with pytest.raises(RuntimeError, match="font_unavailable"):
        module.load_font()
