"""Folder library: pure logic for swapping collections onto the Frame.

Each sub-folder of the library root is a collection.  Images already on the
TV are recognised by comparing their thumbnails with the library files, so
only images known to have a local copy are ever deleted without asking.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
MAX_WIDTH, MAX_HEIGHT = 3840, 2160
_ASPECT = 16 / 9
_ASPECT_TOLERANCE = 0.01
_SIG_SIZE = (64, 36)
# Signature distances are mean absolute pixel differences (0-255).
MATCH_MAX_DISTANCE = 4.0  # furthest a thumbnail may be from its file
MATCH_MARGIN = 2.0  # how much closer the best file must be than the next one
_SAME_PICTURE = 1.0  # files this close to each other are interchangeable

# Library photos can be far larger than Pillow's decompression-bomb default.
Image.MAX_IMAGE_PIXELS = 250_000_000


def _visible(path: Path) -> bool:
    # Skip hidden files and Synology's @eaDir / #recycle folders.
    return not path.name.startswith((".", "@", "#"))


def _is_image(path: Path) -> bool:
    return path.is_file() and _visible(path) and path.suffix.lower() in IMAGE_SUFFIXES


def _images_in(folder: Path) -> list[Path]:
    return sorted((p for p in folder.iterdir() if _is_image(p)), key=lambda p: p.name.lower())


def scan(root: Path) -> dict[str, list[Path]]:
    """Return ``{collection name: image files}`` for each sub-folder of *root*.

    Images directly in *root* belong to no collection and are listed under ``""``.
    """
    collections = {}
    loose = _images_in(root)
    if loose:
        collections[""] = loose
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and _visible(folder):
            collections[folder.name] = _images_in(folder)
    return collections


def resolve(root: Path, rel: str) -> Path:
    """Turn a library-relative image path into a file, or raise ``ValueError``."""
    parts = rel.split("/") if isinstance(rel, str) else []
    if not 1 <= len(parts) <= 2 or any(
        not part or part.startswith((".", "@", "#")) or "\\" in part or "\0" in part
        for part in parts
    ):
        raise ValueError("Not a library image")
    path = root.joinpath(*parts)
    if not _is_image(path) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Not a library image")
    return path


def render(path: Path, crop: dict | None = None) -> bytes:
    """Return the bytes to upload for *path*: 16:9 and no larger than 4K.

    A JPEG that already qualifies is returned untouched.  Otherwise it is cut
    to *crop* (source pixels) or, without one, to the centred 16:9 window.
    """
    data = path.read_bytes()
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    if (
        img.format == "JPEG"
        and img.mode == "RGB"
        and img.getexif().get(274, 1) == 1
        and abs(w / h - _ASPECT) <= _ASPECT_TOLERANCE
        and w <= MAX_WIDTH
        and h <= MAX_HEIGHT
    ):
        return data

    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    if crop:
        img = img.crop((crop["x"], crop["y"], crop["x"] + crop["w"], crop["y"] + crop["h"]))
    elif abs(w / h - _ASPECT) > _ASPECT_TOLERANCE:
        cw, ch = (round(h * _ASPECT), h) if w / h > _ASPECT else (w, round(w / _ASPECT))
        left, top = (w - cw) // 2, (h - ch) // 2
        img = img.crop((left, top, left + cw, top + ch))
    img.thumbnail((MAX_WIDTH, MAX_HEIGHT), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def signature(data: bytes) -> bytes:
    """A tiny fingerprint that survives resizing and recompression."""
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (_SIG_SIZE[0] * 4, _SIG_SIZE[1] * 4))
    return img.convert("RGB").resize(_SIG_SIZE, Image.LANCZOS).tobytes()


def distance(a: bytes, b: bytes) -> float:
    diff = ImageChops.difference(
        Image.frombytes("RGB", _SIG_SIZE, a), Image.frombytes("RGB", _SIG_SIZE, b)
    )
    return sum(ImageStat.Stat(diff).mean) / 3


def match(thumbs: dict[str, bytes], files: dict[str, bytes]) -> dict[str, str]:
    """Link TV thumbnails to library files: ``{content_id: file key}``.

    Both arguments map to signatures.  A thumbnail is linked only when one
    picture is clearly the closest; anything doubtful stays unlinked.
    """
    links = {}
    for content_id, thumb in thumbs.items():
        ranked = sorted((distance(thumb, sig), key) for key, sig in files.items())
        if not ranked or ranked[0][0] > MATCH_MAX_DISTANCE:
            continue
        best_distance, best = ranked[0]
        rivals = [d for d, key in ranked[1:] if distance(files[best], files[key]) > _SAME_PICTURE]
        if rivals and rivals[0] - best_distance < MATCH_MARGIN:
            continue
        links[content_id] = best
    return links


def make_plan(target: list[str], links: dict[str, str], tv_ids: list[str]) -> dict:
    """Work out how to make the TV hold exactly the *target* file keys.

    ``keep`` stays on the TV, ``upload`` must be sent, ``delete`` has a local
    copy and can be removed, and ``unknown`` has no local copy, so it is only
    removed if the user says so.
    """
    if not target:
        raise ValueError("Collection has no images")
    wanted = set(target)
    keep: dict[str, str] = {}
    delete, unknown = [], []
    for content_id in tv_ids:
        key = links.get(content_id)
        if key is None:
            unknown.append(content_id)
        elif key in wanted and key not in keep.values():
            keep[content_id] = key
        else:
            delete.append(content_id)
    have = set(keep.values())
    upload = [key for key in dict.fromkeys(target) if key not in have]
    return {"keep": keep, "upload": upload, "delete": delete, "unknown": unknown}
