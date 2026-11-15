"""Read-only Telegram material collection; media bytes remain outside feature metadata."""
from __future__ import annotations

import asyncio
import hashlib
import io
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .vision_media import (
    DecodedAsset, Document, Frame, _field, compose_document, decode_in_subprocess,
    distinct_frames, document_from_entities, flatten_frame, RENDER_VERSION,
)


@dataclass(frozen=True)
class FileRef:
    source: str
    file_id: str = field(repr=False)
    unique_id: str = ""
    emoji_id: str | None = None
    repaint: bool = False


@dataclass(frozen=True)
class VisualRequest:
    user_id: int
    document: Document
    files: tuple[FileRef, ...] = ()
    inspect_profile: bool = True


@dataclass(frozen=True)
class VisualImage:
    source: str
    frame: Frame = field(repr=False)
    asset_hash: str = ""
    background: str = "light"


@dataclass(frozen=True)
class PreparedVisuals:
    images: tuple[VisualImage, ...]
    limitations: tuple[str, ...] = ()

    def manifest(self) -> dict:
        return {
            "render_version": RENDER_VERSION,
            "images": [{"source": image.source, "time_ms": image.frame.time_ms,
                        "asset_hash": image.asset_hash, "background": image.background}
                       for image in self.images],
            "limitations": list(self.limitations),
        }


def request_from_message(message: Any, user_id: int, *, inspect_profile: bool) -> VisualRequest:
    text = _field(message, "text")
    if text is not None:
        entities = _field(message, "entities")
    else:
        text, entities = _field(message, "caption", "") or "", _field(message, "caption_entities")
    document = document_from_entities(text, entities)
    if _field(message, "rich_message") or _field(message, "checklist") or _field(message, "live_photo"):
        document = Document(document.text, document.runs,
                            document.limitations + ("structured_media_not_rendered",))
    files = []
    photo = _field(message, "photo") or ()
    if photo:
        item = photo[-1]
        files.append(FileRef("photo", _field(item, "file_id"), _field(item, "file_unique_id", "")))
    for source in ("sticker", "animation", "video"):
        item = _field(message, source)
        if item and _field(item, "file_id"):
            files.append(FileRef(source, _field(item, "file_id"), _field(item, "file_unique_id", "")))
            premium = _field(item, "premium_animation") if source == "sticker" else None
            if premium and _field(premium, "file_id"):
                files.append(FileRef("premium_animation", _field(premium, "file_id"),
                                     _field(premium, "file_unique_id", "")))
    return VisualRequest(user_id, document, tuple(files), inspect_profile)


class LimitedBuffer(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ValueError("visual_download_limit")
        return super().write(data)


class VisualCollector:
    def __init__(self, settings):
        self.settings = settings
        self.max_assets = max(1, min(64, settings.vision_max_assets))
        self.max_images = max(1, min(24, settings.vision_max_images))
        self.max_bytes = max(1, min(4 * 1024 * 1024, settings.vision_max_download_bytes))
        # Only IDs and expiry, never the live personal-channel contents or Bio.
        self._profiles: OrderedDict[int, tuple[float, tuple[FileRef, ...], tuple[str, ...]]] = \
            OrderedDict()
        self._decoded: OrderedDict[str, tuple[DecodedAsset, str]] = OrderedDict()
        self._cache_bytes = 0

    async def _emoji_refs(self, bot, ids: list[str], source: str) -> list[FileRef]:
        if not ids:
            return []
        stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=ids)
        return [FileRef(source, _field(item, "file_id"), _field(item, "file_unique_id", ""),
                        str(_field(item, "custom_emoji_id")), bool(_field(item, "needs_repainting")))
                for item in stickers if _field(item, "file_id") and _field(item, "custom_emoji_id")]

    async def _profile_refs(self, bot, user_id: int) -> tuple[tuple[FileRef, ...], tuple[str, ...]]:
        now = time.time()
        cached = self._profiles.get(user_id)
        if cached and now < cached[0]:
            self._profiles.move_to_end(user_id)
            return cached[1], cached[2]
        refs, limits = [], []
        expiry = now + max(1, self.settings.vision_profile_ttl_seconds)
        try:
            chat = await bot.get_chat(user_id)
            photo = _field(chat, "photo")
            if photo and _field(photo, "big_file_id"):
                refs.append(FileRef("avatar", _field(photo, "big_file_id"),
                                    _field(photo, "big_file_unique_id", "")))
            status_id = _field(chat, "emoji_status_custom_emoji_id")
            end = _field(chat, "emoji_status_expiration_date")
            if status_id and (not end or end > now):
                refs.extend(await self._emoji_refs(bot, [str(status_id)], "status_emoji"))
                if end:
                    expiry = min(expiry, end)
        except Exception:
            limits.append("profile_or_status_unavailable")
        if not any(ref.source == "avatar" for ref in refs):
            try:
                result = await bot.get_user_profile_photos(user_id=user_id, limit=1)
                photos = _field(result, "photos") or ()
                if photos:
                    photo = photos[0][-1]
                    refs.append(FileRef("avatar", _field(photo, "file_id"),
                                        _field(photo, "file_unique_id", "")))
            except Exception:
                limits.append("avatar_unavailable")
        # Failures only get a short retry delay, never a cached clean profile.
        if limits:
            expiry = min(expiry, now + 15)
        self._profiles[user_id] = (expiry, tuple(refs), tuple(limits))
        while len(self._profiles) > 512:
            self._profiles.popitem(last=False)
        return tuple(refs), tuple(limits)

    async def _decode(self, bot, ref: FileRef, remaining: int) -> tuple[DecodedAsset, str, int]:
        cache_key = ref.unique_id or ref.file_id
        cached = self._decoded.get(cache_key)
        if cached is not None:
            self._decoded.move_to_end(cache_key)
            return cached[0], cached[1], 0
        info = await bot.get_file(ref.file_id)
        if (_field(info, "file_size") or 0) > remaining:
            raise ValueError("visual_download_limit")
        buffer = LimitedBuffer(remaining)
        await bot.download(info, destination=buffer, timeout=10, chunk_size=65536)
        raw = buffer.getvalue()
        decoded = await asyncio.to_thread(
            decode_in_subprocess, raw, max_frames=self.settings.vision_max_frames,
        )
        digest = hashlib.sha256(raw).hexdigest()
        cost = sum(len(frame.png) for frame in decoded.frames)
        if cost <= 32 * 1024 * 1024:
            # Concurrent collectors may have decoded the same asset while awaiting.
            previous = self._decoded.pop(cache_key, None)
            if previous:
                self._cache_bytes -= sum(len(frame.png) for frame in previous[0].frames)
            self._decoded[cache_key] = (decoded, digest)
            self._cache_bytes += cost
            while len(self._decoded) > 128 or self._cache_bytes > 32 * 1024 * 1024:
                _, item = self._decoded.popitem(last=False)
                self._cache_bytes -= sum(len(frame.png) for frame in item[0].frames)
        return decoded, digest, len(raw)

    async def prepare(self, bot, request: VisualRequest) -> PreparedVisuals:
        refs = list(request.files)
        limits = list(request.document.limitations)
        ids = list(dict.fromkeys(run.emoji_id for run in request.document.runs if run.emoji_id))
        if len(ids) > self.max_assets:
            limits.append("emoji_asset_count_truncated")
        try:
            refs += await self._emoji_refs(bot, ids[:self.max_assets], "body_emoji")
        except Exception:
            limits.append("body_emoji_lookup_unavailable")
        if request.inspect_profile:
            profile_refs, profile_limits = await self._profile_refs(bot, request.user_id)
            refs += profile_refs
            limits += profile_limits
        # Body tiles and the status icon take priority over a generic avatar.
        refs.sort(key=lambda ref: ref.source == "avatar")
        if len(refs) > self.max_assets:
            limits.append("asset_count_truncated")
        remaining = self.max_bytes
        decoded, emoji = [], {}
        for ref in refs[:self.max_assets]:
            try:
                asset, digest, size = await self._decode(bot, ref, remaining)
                remaining -= size
                limits += list(asset.limitations)
                decoded.append((ref, asset, digest))
                if ref.source == "body_emoji":
                    emoji[ref.emoji_id] = asset
            except Exception:
                limits.append(ref.source + "_decode_unavailable")
        images = []
        layout_available = False
        if ids:
            try:
                layout = await asyncio.to_thread(
                    compose_document, request.document, emoji,
                    font_path=self.settings.vision_font_path,
                    frames=self.settings.vision_max_frames,
                )
                limits += list(layout.limitations)
                # Hash the composition itself; input association stays in its source label.
                images += [VisualImage("body_layout", frame, hashlib.sha256(frame.png).hexdigest())
                           for frame in distinct_frames(layout.frames)]
                layout_available = True
                if any(ref.source == "body_emoji" and ref.repaint for ref, _, _ in decoded):
                    layout_dark = await asyncio.to_thread(
                        compose_document, request.document, emoji,
                        font_path=self.settings.vision_font_path,
                        frames=self.settings.vision_max_frames, dark=True,
                    )
                    images += [VisualImage("body_layout", frame,
                                           hashlib.sha256(frame.png).hexdigest(), "dark")
                               for frame in distinct_frames(layout_dark.frames)]
            except Exception:
                limits.append("body_layout_unavailable")
        for ref, asset, digest in decoded:
            # Layout preserves the whole puzzle first. Individual tiles come last.
            if ref.source == "body_emoji" and layout_available:
                continue
            for frame in distinct_frames(asset.frames):
                images.append(VisualImage(ref.source, flatten_frame(frame), digest))
                if ref.repaint:
                    images.append(VisualImage(ref.source, flatten_frame(frame, dark=True),
                                              digest, "dark"))
        if len(images) > self.max_images:
            limits.append("image_count_truncated")
        return PreparedVisuals(tuple(images[:self.max_images]),
                               tuple(dict.fromkeys(limits)))
