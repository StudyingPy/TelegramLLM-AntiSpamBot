"""Bounded local rendering. No network, model calls, or moderation decisions."""
from __future__ import annotations

import gzip
import base64
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RENDER_VERSION = "telegram-visual-v1"
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 16_000_000


@dataclass(frozen=True)
class Frame:
    png: bytes
    time_ms: int = 0


@dataclass(frozen=True)
class DecodedAsset:
    frames: tuple[Frame, ...]
    duration_ms: int = 0
    limitations: tuple[str, ...] = ()

    def at(self, time_ms: int) -> Frame:
        target = time_ms % self.duration_ms if self.duration_ms else 0
        return min(self.frames, key=lambda frame: abs(frame.time_ms - target))


@dataclass(frozen=True)
class Run:
    text: str
    emoji_id: str | None = None


@dataclass(frozen=True)
class Document:
    text: str
    runs: tuple[Run, ...]
    limitations: tuple[str, ...] = ()


def _field(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def document_from_entities(text: str, entities: Any = (), *, max_chars: int = 4000) -> Document:
    """Map Telegram UTF-16 offsets before stripping any emoji/digits/whitespace."""
    original = text
    text = text[:max_chars]
    limits = ["text_truncated"] if text != original else []
    boundaries = {0: 0}
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        boundaries[units] = index + 1
    spans = []
    for entity in entities or ():
        kind = _field(entity, "type")
        if getattr(kind, "value", kind) != "custom_emoji":
            continue
        start, length = _field(entity, "offset"), _field(entity, "length")
        key = _field(entity, "custom_emoji_id")
        if (type(start) is not int or type(length) is not int or length <= 0
                or start not in boundaries or start + length not in boundaries or not key):
            limits.append("invalid_emoji_entity")
            continue
        spans.append((boundaries[start], boundaries[start + length], str(key)))
    runs = []
    cursor = 0
    for start, end, key in sorted(spans):
        if start < cursor:
            limits.append("overlapping_emoji_entity")
            continue
        if start > cursor:
            runs.append(Run(text[cursor:start]))
        runs.append(Run(text[start:end], key))
        cursor = end
    if cursor < len(text):
        runs.append(Run(text[cursor:]))
    return Document(text, tuple(runs), tuple(dict.fromkeys(limits)))


def sample_times(duration_ms: int, count: int) -> tuple[int, ...]:
    if duration_ms <= 1 or count <= 1:
        return (0,)
    return tuple(round(index * (duration_ms - 1) / (count - 1)) for index in range(count))


def _png(image, *, max_side: int = 512) -> bytes:
    from PIL import Image

    if image.width * image.height > MAX_PIXELS:
        raise ValueError("image_pixel_limit")
    image = image.convert("RGBA")
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _video_frames(raw: bytes, max_frames: int, max_side: int) -> DecodedAsset:
    import imageio_ffmpeg

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    is_webm = raw.startswith(b"\x1aE\xdf\xa3")
    with tempfile.TemporaryDirectory(prefix="antispam-frames-") as directory:
        path = Path(directory) / ("source.webm" if is_webm else "source.mp4")
        path.write_bytes(raw)
        options = {"timeout": 8, "capture_output": True,
                   "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        probe = subprocess.run([exe, "-hide_banner", "-i", str(path)], **options)
        metadata = probe.stderr.decode("utf-8", errors="replace")
        dimensions = re.search(r"Video:[^\n]*?\b(\d{1,6})x(\d{1,6})\b", metadata)
        if not dimensions or not 0 < int(dimensions[1]) * int(dimensions[2]) <= MAX_PIXELS:
            raise ValueError("video_pixel_limit_or_dimensions_unavailable")
        match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", metadata)
        duration = round((int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])) * 1000) \
            if match else 0
        limits = ["sparse_animation_sampling"]
        if not duration:
            limits.append("duration_unavailable")
        if duration > 15_000:
            limits.append("animation_duration_truncated")
        frames = []
        # libvpx decodes the alpha plane that FFmpeg's native WebM decoder can drop.
        codec = "libvpx-vp9" if re.search(r"Video: vp9", metadata) else "libvpx"
        decoder = ["-c:v", codec] if is_webm else []
        fps = re.search(r"[, ](\d+(?:\.\d+)?) fps", metadata)
        tail_ms = round(1000 / float(fps[1])) + 1 if fps and float(fps[1]) > 0 else 100
        # Seeking after the timestamp of the last encoded frame can return no PNG.
        end_ms = min(max(1, duration - tail_ms), 15_000) if duration else 0
        for target in sample_times(end_ms, max_frames):
            command = [exe, "-v", "error", *decoder, "-ss", str(target / 1000),
                       "-i", str(path), "-frames:v", "1", "-vf",
                       f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease",
                       "-f", "image2pipe", "-vcodec", "png", "-"]
            result = subprocess.run(command, **options)
            if (result.returncode or not result.stdout) and target > 0:
                # Container duration may be rounded past the final displayable frame.
                target = max(0, target - 100)
                command[command.index("-ss") + 1] = str(target / 1000)
                result = subprocess.run(command, **options)
                if not result.returncode and result.stdout:
                    limits.append("tail_seek_adjusted")
            if result.returncode or not result.stdout:
                limits.append("frame_unavailable")
                continue
            frames.append(Frame(result.stdout, target))
        if not frames:
            raise ValueError("video_decode_failed")
        return DecodedAsset(tuple(frames), duration, tuple(dict.fromkeys(limits)))


def decode_asset(raw: bytes, *, max_frames: int = 4, max_side: int = 512) -> DecodedAsset:
    """Decode static images, GIF, TGS and WebM/MP4 without following URLs."""
    from PIL import Image

    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input_size_limit")
    max_frames = max(1, min(6, max_frames))
    max_side = max(64, min(1024, max_side))
    if raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            body = stream.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("tgs_expansion_limit")
        info = json.loads(body)
        if info.get("fonts") or any(isinstance(item, dict) and ("p" in item or "u" in item)
                                    for item in info.get("assets", [])):
            raise ValueError("tgs_external_assets_unsupported")
        width, height = float(info.get("w", 0)), float(info.get("h", 0))
        rate = float(info.get("fr", 0))
        total = float(info.get("op", 0)) - float(info.get("ip", 0))
        if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= MAX_PIXELS
                and 0 < rate <= 120 and 1 <= total <= 3600):
            raise ValueError("tgs_dimensions_or_duration_limit")
        from rlottie_python import LottieAnimation

        duration = round(total / rate * 1000)
        with tempfile.TemporaryDirectory(prefix="antispam-tgs-") as directory:
            path = Path(directory) / "source.tgs"
            path.write_bytes(raw)
            animation = LottieAnimation.from_tgs(str(path))
            frames = tuple(Frame(_png(animation.render_pillow_frame(
                frame_num=min(int(time_ms / 1000 * rate), int(total) - 1),
                width=max_side, height=max_side,
            ), max_side=max_side), time_ms)
                for time_ms in sample_times(min(duration, 15_000), max_frames))
        limits = ("sparse_animation_sampling",)
        if duration > 15_000:
            limits += ("animation_duration_truncated",)
        return DecodedAsset(frames, duration, limits)
    if raw.startswith(b"\x1aE\xdf\xa3") or raw[4:8] == b"ftyp":
        return _video_frames(raw, max_frames, max_side)
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("image_pixel_limit")
        count = getattr(image, "n_frames", 1)
        if count == 1:
            return DecodedAsset((Frame(_png(image, max_side=max_side)),))
        if count > 1000:
            raise ValueError("animation_frame_limit")
        timeline = []
        duration = 0
        for index in range(count):
            image.seek(index)
            timeline.append((duration, index))
            duration += max(10, int(image.info.get("duration", 100)))
        frames = []
        for target in sample_times(min(duration, 15_000), max_frames):
            frame_index = max(index for start, index in timeline if start <= target)
            image.seek(frame_index)
            frames.append(Frame(_png(image, max_side=max_side), target))
        limits = ("sparse_animation_sampling",)
        if duration > 15_000:
            limits += ("animation_duration_truncated",)
        return DecodedAsset(tuple(frames), duration, limits)


def load_font(path: str | None = None, size: int = 36):
    from PIL import ImageFont

    candidates = [path] if path else []
    candidates += ["C:/Windows/Fonts/msyh.ttc",
                   "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                   "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    raise RuntimeError("vision_font_unavailable_set_VISION_FONT_PATH")


def compose_document(
    document: Document, emoji: dict[str, DecodedAsset], *, font_path: str | None = None,
    width: int = 1024, max_height: int = 2048, cell: int = 64, frames: int = 4,
    dark: bool = False,
) -> DecodedAsset:
    """Render source runs in order, preserving hard newlines and adjacent tiles.

    Telegram's client font/viewport are unknown; soft wrapping is an approximation.
    """
    from PIL import Image, ImageDraw, ImageOps

    width = max(128, min(2048, width))
    max_height = max(cell, min(4096, max_height))
    font = load_font(font_path, size=36)
    limits = [*document.limitations, "approximate_client_layout"]
    placements = []
    x, y = 0, 0
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for run in document.runs:
        if run.emoji_id:
            parts = [(run.emoji_id, cell, True)]
        else:
            parts = [(char, 0 if char in {"\n", "\r"} else
                      max(1, round(measure.textlength(char, font=font))), False)
                     for char in run.text.replace("\t", "    ")]
        for value, advance, is_emoji in parts:
            if not is_emoji and value in {"\n", "\r"}:
                if value == "\n":
                    x, y = 0, y + cell
                continue
            if x + advance > width:
                x, y = 0, y + cell
            if y + cell > max_height:
                limits.append("layout_truncated")
                break
            placements.append((value, is_emoji, x, y))
            x += advance
    duration = max((item.duration_ms for item in emoji.values()), default=0)
    times = sample_times(min(duration, 15_000), max(1, min(6, frames)))
    if duration:
        limits.append("sparse_animation_sampling")
    rendered = []
    for time_ms in times:
        canvas = Image.new("RGBA", (width, min(max_height, y + cell)),
                           "#202020" if dark else "white")
        draw = ImageDraw.Draw(canvas)
        for value, is_emoji, px, py in placements:
            if is_emoji:
                asset = emoji.get(value)
                if asset is None:
                    limits.append("emoji_asset_unavailable")
                    draw.rectangle((px, py, px + cell - 1, py + cell - 1), outline="gray")
                    draw.text((px + 20, py + 8), "?", font=font, fill="gray")
                    continue
                with Image.open(io.BytesIO(asset.at(time_ms).png)) as tile:
                    tile = ImageOps.contain(tile.convert("RGBA"), (cell, cell))
                    canvas.alpha_composite(tile, (px, py))
            else:
                draw.text((px, py + 8), value, font=font, fill="white" if dark else "black")
        out = io.BytesIO()
        canvas.convert("RGB").save(out, format="PNG")
        rendered.append(Frame(out.getvalue(), time_ms))
    return DecodedAsset(tuple(rendered), duration, tuple(dict.fromkeys(limits)))


def flatten_frame(frame: Frame, *, dark: bool = False) -> Frame:
    from PIL import Image

    with Image.open(io.BytesIO(frame.png)) as image:
        image = image.convert("RGBA")
        canvas = Image.new("RGBA", image.size, "#202020" if dark else "white")
        canvas.alpha_composite(image)
        out = io.BytesIO()
        canvas.convert("RGB").save(out, format="PNG")
    return Frame(out.getvalue(), frame.time_ms)


def distinct_frames(frames: tuple[Frame, ...]) -> tuple[Frame, ...]:
    seen = set()
    result = []
    for frame in frames:
        key = hashlib.sha256(frame.png).digest()
        if key not in seen:
            seen.add(key)
            result.append(frame)
    return tuple(result)


def decode_in_subprocess(raw: bytes, *, max_frames: int = 4) -> DecodedAsset:
    """Native renderers have a hard process deadline, including their FFmpeg children."""
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input_size_limit")
    with tempfile.TemporaryDirectory(prefix="antispam-render-") as directory:
        environment = {**os.environ, "TMPDIR": directory, "TEMP": directory, "TMP": directory}
        process = subprocess.Popen(
            [sys.executable, "-m", "telegram_llm_antispam.vision_media", str(max_frames)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            output, _ = process.communicate(raw, timeout=25)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.kill()
            process.communicate()
            raise ValueError("render_process_timeout") from None
        if process.returncode:
            raise ValueError("render_process_failed")
        data = json.loads(output)
        return DecodedAsset(tuple(Frame(base64.b64decode(frame["png"]), frame["time_ms"])
                                  for frame in data["frames"]),
                            data["duration_ms"], tuple(data["limitations"]))


if __name__ == "__main__":
    try:
        asset = decode_asset(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1),
                             max_frames=int(sys.argv[1]))
        sys.stdout.write(json.dumps({"frames": [{"png": base64.b64encode(frame.png).decode("ascii"),
                                               "time_ms": frame.time_ms} for frame in asset.frames],
                                     "duration_ms": asset.duration_ms,
                                     "limitations": asset.limitations}))
    except Exception:
        raise SystemExit(1) from None
