"""File management for the folder library.  No TV code.

Every function takes the library root and library-relative names, refuses
anything outside the root, and never overwrites an existing file.  Crop and
delete copy the original to ``.backups/`` first when asked; if that copy
fails, the original is left untouched.
"""
from __future__ import annotations

import io
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

import library

BACKUPS = ".backups"
MAX_NAME = 150
_BAD_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
# Added to a backup's name: the time, plus a number if that name was taken.
_STAMP = re.compile(r"__(\d{8}-\d{6})(?: \(\d+\))?$")
_CLUTTER = {".DS_Store", "Thumbs.db", "@eaDir"}
_ASPECT = 16 / 9


# --- names and paths ---

def check_name(name: str) -> str:
    """Return *name* trimmed, or raise ``ValueError`` if it isn't a safe name."""
    name = name.strip() if isinstance(name, str) else ""
    if (
        not name
        or len(name) > MAX_NAME
        or name.startswith((".", "@", "#"))
        or name.endswith(".")
        or _BAD_CHARS.search(name)
    ):
        raise ValueError('Names cannot be empty, start with . @ or #, or contain / \\ : * ? " < > |')
    return name


def collection_dir(root: Path, collection: str) -> Path:
    """The existing folder for *collection* (``""`` is the main folder)."""
    if collection == "":
        return root
    folder = root / check_name(collection)
    if not folder.is_dir() or folder.resolve().parent != root.resolve():
        raise ValueError("No such collection")
    return folder


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _reserve(folder: Path, filename: str) -> Path:
    """Create an empty file with a free name based on *filename* and return it."""
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for n in range(1, 1000):
        path = folder / (filename if n == 1 else f"{stem} ({n}){suffix}")
        try:
            path.open("xb").close()
            return path
        except FileExistsError:
            continue
    raise ValueError("Too many files with that name")


def _write_new(folder: Path, filename: str, data: bytes) -> Path:
    path = _reserve(folder, filename)
    try:
        path.write_bytes(data)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return path


def _relocate(source: Path, folder: Path, filename: str) -> Path:
    wanted = folder / filename
    if wanted.exists() and wanted.samefile(source):
        # Same file: at most the letter case changes.
        source.rename(wanted)
        return wanted
    target = _reserve(folder, filename)
    try:
        os.replace(source, target)
    except OSError:
        target.unlink(missing_ok=True)
        raise
    return target


# --- images ---

def add_image(root: Path, collection: str, filename: str, data: bytes) -> str:
    folder = collection_dir(root, collection)
    filename = check_name(filename)
    if Path(filename).suffix.lower() not in library.IMAGE_SUFFIXES:
        raise ValueError("Only JPEG and PNG images can be added")
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in ("JPEG", "PNG"):
                raise ValueError("Only JPEG and PNG images can be added")
            img.verify()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"{filename} is not a readable image") from e
    return _rel(root, _write_new(folder, filename, data))


def rename_image(root: Path, rel: str, new_name: str) -> str:
    source = library.resolve(root, rel)
    name = check_name(new_name)
    if Path(name).suffix.lower() == source.suffix.lower():
        name = name[: -len(source.suffix)]
    filename = check_name(check_name(name) + source.suffix)
    return _rel(root, _relocate(source, source.parent, filename))


def move_image(root: Path, rel: str, collection: str) -> str:
    source = library.resolve(root, rel)
    folder = collection_dir(root, collection)
    if folder.samefile(source.parent):
        return rel
    return _rel(root, _relocate(source, folder, source.name))


def copy_image(root: Path, rel: str, collection: str) -> str:
    source = library.resolve(root, rel)
    folder = collection_dir(root, collection)
    return _rel(root, _write_new(folder, source.name, source.read_bytes()))


def _back_up(root: Path, source: Path) -> None:
    relative = source.relative_to(root)
    folder = root / BACKUPS / relative.parent
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    data = source.read_bytes()
    copy = _write_new(folder, f"{source.stem}__{stamp}{source.suffix}", data)
    if copy.stat().st_size != len(data):
        raise OSError("The backup copy is incomplete")


def delete_image(root: Path, rel: str, *, backup: bool) -> None:
    source = library.resolve(root, rel)
    if backup:
        _back_up(root, source)
    source.unlink()


def crop_image(root: Path, rel: str, *, left: float, top: float, width: float, backup: bool) -> None:
    """Cut a 16:9 window from the picture as displayed.

    *left* and *width* are fractions of the picture's width, *top* of its height.
    """
    source = library.resolve(root, rel)
    with Image.open(source) as opened:
        fmt, icc = opened.format, opened.info.get("icc_profile")
        img = ImageOps.exif_transpose(opened)
        img.load()
    exif = img.getexif()
    full_w, full_h = img.size
    x, y, w = round(left * full_w), round(top * full_h), round(width * full_w)
    h = round(w / _ASPECT)
    # Allow a pixel of rounding at the far edges.
    # The box is chosen on a scaled preview, so allow slight rounding overshoot.
    slack_x, slack_y = max(1, full_w // 200), max(1, full_h // 200)
    if w < 16 or x < 0 or y < 0 or x + w > full_w + slack_x or y + h > full_h + slack_y:
        raise ValueError("The crop must lie inside the picture")
    w = min(w, full_w)
    h = min(h, full_h)
    x, y = min(x, full_w - w), min(y, full_h - h)
    out = img.crop((x, y, x + w, y + h))

    options = {"icc_profile": icc} if icc else {}
    if fmt == "JPEG":
        out = out.convert("RGB")
        options.update(quality=95, exif=exif.tobytes())
    buf = io.BytesIO()
    out.save(buf, format=fmt, **options)

    if backup:
        _back_up(root, source)
    # Write alongside, then swap in, so a failure can't leave half a picture.
    temp = source.with_name(f".{source.name}.tmp")
    try:
        temp.write_bytes(buf.getvalue())
        os.replace(temp, source)
    finally:
        temp.unlink(missing_ok=True)


# --- backups ---

def list_backups(root: Path) -> list[dict]:
    """Backed-up images, newest first."""
    base = root / BACKUPS
    if not base.is_dir():
        return []
    entries = []
    for path in base.rglob("*"):
        parts = path.relative_to(base).parts
        if not path.is_file() or len(parts) > 2 or path.name.startswith("."):
            continue
        if path.suffix.lower() not in library.IMAGE_SUFFIXES:
            continue
        match = _STAMP.search(path.stem)
        entries.append({
            "id": "/".join(parts),
            "collection": parts[0] if len(parts) == 2 else "",
            "name": _STAMP.sub("", path.stem) + path.suffix,
            "saved": match.group(1) if match else "",
        })
    return sorted(entries, key=lambda e: (e["saved"], e["id"]), reverse=True)


def backup_path(root: Path, backup_id: str) -> Path:
    if backup_id not in {e["id"] for e in list_backups(root)}:
        raise ValueError("No such backup")
    return root / BACKUPS / backup_id


def restore_backup(root: Path, backup_id: str) -> str:
    """Copy a backup into its collection.  The backup itself is kept."""
    source = backup_path(root, backup_id)
    entry = next(e for e in list_backups(root) if e["id"] == backup_id)
    if entry["collection"]:
        (root / check_name(entry["collection"])).mkdir(exist_ok=True)
    folder = collection_dir(root, entry["collection"])
    return _rel(root, _write_new(folder, entry["name"], source.read_bytes()))


# --- collections ---

def create_collection(root: Path, name: str) -> str:
    name = check_name(name)
    try:
        (root / name).mkdir()
    except FileExistsError:
        raise ValueError("Something with that name already exists") from None
    return name


def rename_collection(root: Path, name: str, new_name: str) -> str:
    if name == "":
        raise ValueError("The main folder cannot be renamed")
    source = collection_dir(root, name)
    new_name = check_name(new_name)
    target = root / new_name
    if target.exists() and not target.samefile(source):
        raise ValueError("Something with that name already exists")
    source.rename(target)
    return new_name


def delete_collection(root: Path, name: str, *, backup: bool) -> dict:
    """Delete a collection's images, and its folder if nothing else is in it."""
    if name == "":
        raise ValueError("The main folder cannot be deleted")
    folder = collection_dir(root, name)
    images = [rel for rel in (f"{name}/{p.name}" for p in library.scan(root)[name])]
    if backup:  # back everything up before removing anything
        for rel in images:
            _back_up(root, root / rel)
    for rel in images:
        (root / rel).unlink()

    leftovers = list(folder.iterdir())
    if any(p.name not in _CLUTTER and not p.name.startswith("._") for p in leftovers):
        return {"deleted": len(images), "folder_removed": False}
    try:
        for path in leftovers:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        folder.rmdir()
    except OSError:
        return {"deleted": len(images), "folder_removed": False}
    return {"deleted": len(images), "folder_removed": True}
