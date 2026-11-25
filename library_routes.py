"""Local Library routes: browse a folder of images and move them to and from the Frame.

``build_router`` receives the server module, which supplies TV access, data
files and shared helpers at call time.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps
from samsungtvws.exceptions import ConnectionFailure

import library
import library_files

log = logging.getLogger("docent")

_MY_PHOTOS = "MY-C0002"
_THUMB_SIZE = (480, 270)
_PREVIEW_SIZE = (1600, 1600)
MAX_FILE_BYTES = 100 * 1024 * 1024  # per image added to the library
# The folder picker and the saved library folder are limited to these places.
BROWSE_ROOTS = [Path.home(), Path("/Volumes")]

# Arguments are passed to the script, never spliced into it.
_CHOOSER_SCRIPT = """on run argv
    activate
    if (count of argv) > 0 then
        set chosen to choose folder with prompt "Choose your Docent library folder" default location (POSIX file (item 1 of argv))
    else
        set chosen to choose folder with prompt "Choose your Docent library folder"
    end if
    return POSIX path of chosen
end run"""

srv = None  # the server module, set by build_router()
job: dict = {"state": "idle"}
task: asyncio.Task | None = None
# Held while library state is read and rewritten, by TV checks and file changes.
state_lock = asyncio.Lock()


def _load_state() -> dict:
    state = srv._safe_load_json(srv.LIBRARY_STATE_FILE, dict)
    for key in ("files", "uploads", "outdated", "crops", "matte", "meta"):
        state.setdefault(key, {})
    state.setdefault("cropped", [])  # files whose saved upload crop no longer applies
    state.setdefault("backups", True)
    return state


def _save_state(state: dict) -> None:
    srv._atomic_write_json(srv.LIBRARY_STATE_FILE, state)


def _root() -> Path | None:
    """The folder chosen in the app, else ``DOCENT_LIBRARY_DIR``."""
    saved = _load_state().get("root")
    return Path(saved) if saved else srv.LIBRARY_DIR


def _available(root: Path | None) -> bool:
    try:
        return root is not None and root.is_dir()
    except OSError:
        return False


def _scan() -> dict[str, list[Path]]:
    root = _root()
    if root is None:
        raise HTTPException(400, "No library folder has been chosen")
    try:
        if _available(root):
            return library.scan(root)
    except OSError as e:
        log.warning("Cannot read library folder %s: %s", root, e)
    raise HTTPException(503, f"Library folder is not reachable: {root}")


def _rel(collection: str, path: Path) -> str:
    return f"{collection}/{path.name}" if collection else path.name


def _target(body: dict) -> list[str]:
    """Relative paths named by ``{"paths": [...]}`` or ``{"collection": name}``."""
    collections = _scan()
    paths = body.get("paths")
    if paths is not None:
        if not isinstance(paths, list) or not paths:
            raise HTTPException(400, "Choose at least one image")
        for rel in paths:
            try:
                library.resolve(_root(), rel)
            except (ValueError, OSError):
                raise HTTPException(404, "No such library image")
        return list(dict.fromkeys(paths))
    name = body.get("collection")
    if not isinstance(name, str) or name not in collections:
        raise HTTPException(404, "No such collection folder")
    if not collections[name]:
        raise HTTPException(400, "That folder has no images")
    return [_rel(name, p) for p in collections[name]]


def _index_library(state: dict) -> tuple[dict, dict]:
    """Fingerprint every library file.  Blocking: run in a worker thread.

    Returns ``({relative path: key}, {key: file info})``.  Results are cached
    in *state* by size and modification time.
    """
    # Reuse the crop chosen when a file of the same name was uploaded.
    for record in srv._load_artwork_meta()["artwork"].values():
        if record.get("crop_box") and record.get("original_filename"):
            stem = Path(record["original_filename"]).stem.removesuffix("_cropped").lower()
            state["crops"][stem] = record["crop_box"]

    keys, files, cache = {}, {}, {}
    for name, paths in _scan().items():
        for path in paths:
            rel = _rel(name, path)
            try:
                stat = path.stat()
                crop = None if rel in state["cropped"] else state["crops"].get(path.stem.lower())
                stamp = [stat.st_size, stat.st_mtime_ns, crop]
                entry = state["files"].get(rel)
                if not entry or entry["stamp"] != stamp:
                    entry = {
                        "stamp": stamp,
                        "key": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "sig": base64.b64encode(library.signature(library.render(path, crop))).decode(),
                    }
            except Exception as e:
                raise ValueError(f"Cannot read {rel}: {e}") from e
            cache[rel] = entry
            keys[rel] = entry["key"]
            files.setdefault(entry["key"], {
                "path": path, "rel": rel, "crop": crop,
                "sig": base64.b64decode(entry["sig"]),
            })
    state["files"] = cache
    return keys, files


def _my_photos(art) -> dict[str, dict]:
    return {i["content_id"]: i for i in art.available() if i.get("category_id") == _MY_PHOTOS}


def _thumbnails_for(art, content_ids: list[str]) -> dict[str, bytes]:
    try:
        result = art.get_thumbnail_list(content_ids)
    except ConnectionFailure as e:
        if "socket closed" not in str(e):
            raise
        return {}
    return {name.rsplit(".", 1)[0]: bytes(data) for name, data in result.items()}


async def _link_tv() -> tuple[dict, dict, dict, dict, dict]:
    """Match the TV's images to library files.  Changes nothing on the TV.

    Returns ``(keys by path, file info by key, TV images, links by content ID,
    outdated copies by content ID)``.  An outdated copy shows a file as it was
    before it was cropped.
    """
    async with state_lock:
        state = _load_state()
        keys, files = await asyncio.to_thread(_index_library, state)
        on_tv = await srv._tv_op(_my_photos)

        links, outdated, unrecorded = {}, {}, []
        for cid, item in on_tv.items():
            # The date guards against the TV reusing an ID for another picture.
            date = item.get("image_date")
            old = state["outdated"].get(cid)
            if old and old["image_date"] == date:
                if old["was"] in files:  # the original is back in the library
                    links[cid] = old["was"]
                    continue
                if old["key"] in files:
                    outdated[cid] = old
                    continue
            record = state["uploads"].get(cid)
            if record and record["image_date"] == date and record["key"] in files:
                links[cid] = record["key"]
            else:
                unrecorded.append(cid)

        if unrecorded:
            thumbs = await srv._tv_op(lambda art: _thumbnails_for(art, unrecorded), timeout=90)
            matched = await asyncio.to_thread(
                library.match,
                {c: library.signature(data) for c, data in thumbs.items() if c in on_tv},
                {key: info["sig"] for key, info in files.items()},
            )
            links.update(matched)

        state["uploads"] = {
            c: {"key": key, "image_date": on_tv[c].get("image_date")} for c, key in links.items()
        }
        state["outdated"] = outdated
        _save_state(state)
    return keys, files, on_tv, links, outdated


async def _plan(paths: list[str]) -> tuple[dict, dict, dict, dict, dict]:
    keys, files, on_tv, links, outdated = await _link_tv()
    plan = library.make_plan([keys[rel] for rel in paths], links, list(on_tv))
    # An old version whose original was backed up is as safe to remove as any other.
    safe = [c for c in plan["unknown"] if c in outdated and outdated[c]["backed"]]
    plan["delete"] += safe
    plan["unknown"] = [c for c in plan["unknown"] if c not in safe]
    return plan, files, links, keys, outdated


async def _run(kind: str, paths: list[str], *, interval: int = 15, shuffle: bool = True,
               matte: str = "none", delete_unknown: frozenset = frozenset(),
               collection: str | None = None) -> None:
    """Carry out a ``load``, ``display``, ``update``, ``unload`` or ``show`` job.

    Uploads everything before deleting anything, so a failure leaves the old
    pictures in place, and running it again carries on where it stopped.
    """
    try:
        plan, files, links, keys, outdated = await _plan(paths)
        target = list(dict.fromkeys(keys[rel] for rel in paths))
        shown = {key: cid for cid, key in plan["keep"].items()}

        if kind != "unload":
            job.update(step="Uploading", done=0, total=len(plan["upload"]))
            for key in plan["upload"]:
                info = files[key]
                data = await asyncio.to_thread(library.render, info["path"], info["crop"])
                cid = await srv._tv_op(
                    lambda art, _d=data: art.upload(_d, matte=matte, file_type="jpg"),
                    attempts=1, timeout=120,
                )
                shown[key] = cid
                await _record_upload(cid, key, info["path"], data)
                job["done"] += 1

        if kind == "load":
            message = f"{len(plan['upload'])} image(s) loaded onto the Frame."
        elif kind == "display":
            job.update(step="Showing the image", done=0, total=0)
            stopped = await srv._show_image(shown[target[0]])
            message = "Now showing on the Frame." + (" The slideshow was stopped." if stopped else "")
        elif kind == "update":
            old = [c for c, record in outdated.items() if record["key"] in target]
            job.update(step="Removing the old version", done=0, total=0)
            if old:
                current = await srv._tv_op(lambda art: art.get_current())
                if isinstance(current, dict) and current.get("content_id") in old:
                    new = shown[outdated[current["content_id"]]["key"]]
                    await srv._tv_op(lambda art: art.select_image(new))
                await _remove_from_tv(old)
            message = f"{len(old)} out-of-date image(s) replaced on the Frame."
        elif kind == "unload":
            remove = [cid for cid, key in links.items() if key in set(target)]
            remove += [c for c, record in outdated.items() if record["key"] in set(target)]
            job.update(step="Removing images", done=0, total=0)
            if remove:
                await _remove_from_tv(remove)
            message = f"{len(remove)} image(s) removed from the Frame. Your files are unchanged."
        else:
            job.update(step="Removing other images", done=0, total=0)
            remove = plan["delete"] + [c for c in plan["unknown"] if c in delete_unknown]
            await srv._tv_op(lambda art: art.select_image(shown[target[0]]))
            if remove:
                await _remove_from_tv(remove)
            job.update(step="Starting the slideshow")

            def _start(art):
                art.set_slideshow_status(duration=interval, type=shuffle, category_id=_MY_PHOTOS)
                return art.get_slideshow_status()

            status = await srv._tv_op(_start)
            if str(status.get("value")) != str(interval):
                raise RuntimeError("The TV did not start the slideshow")
            kept = len([c for c in plan["unknown"] if c not in delete_unknown])
            note = f" {kept} image(s) with no library copy were left on the TV." if kept else ""
            message = f"{collection or 'Your selection'} is now on the Frame.{note}"

        if collection:
            state = _load_state()
            if kind != "unload":
                state["matte"][collection] = matte
                _save_state(state)
                await _sync_docent_collection(collection, [shown[key] for key in target])
        job.update(state="done", step="", message=message)
        log.info("Library %s finished: %s", kind, message)
    except Exception as e:
        log.warning("Library %s failed during %s: %s", kind, job.get("step"), e)
        reason = str(e) or type(e).__name__
        job.update(state="error", message=f"{job.get('step')} failed: {reason}")
    finally:
        srv._invalidate_art_cache()


async def _record_upload(content_id: str, key: str, path: Path, data: bytes) -> None:
    item = (await srv._tv_op(_my_photos)).get(content_id, {})
    state = _load_state()
    state["uploads"][content_id] = {"key": key, "image_date": item.get("image_date")}
    _save_state(state)
    # Drop any cached thumbnail left over from an earlier use of this ID.
    (srv.THUMB_DIR / f"{content_id}.jpg").unlink(missing_ok=True)
    width, height = Image.open(io.BytesIO(data)).size
    async with srv._meta_lock:
        meta = srv._load_artwork_meta()
        meta["artwork"][content_id] = state["meta"].get(key) or {
            "title": path.stem,
            "original_filename": path.name,
            "width": width,
            "height": height,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        srv._save_artwork_meta(meta)


async def _remove_from_tv(content_ids: list[str]) -> None:
    # Keep titles and analysis so they return if the picture is uploaded again.
    state = _load_state()
    async with srv._meta_lock:
        meta = srv._load_artwork_meta()
        for cid in content_ids:
            record = meta["artwork"].get(cid)
            if record and cid in state["uploads"]:
                state["meta"][state["uploads"][cid]["key"]] = record
    _save_state(state)

    def _delete(art):
        art.delete_list(content_ids)
        return set(_my_photos(art)) & set(content_ids)

    left = await srv._tv_op(_delete, timeout=90)
    if left:
        raise RuntimeError(f"The TV kept {len(left)} image(s) that should have been removed")

    state = _load_state()
    async with srv._meta_lock:
        meta = srv._load_artwork_meta()
        for cid in content_ids:
            meta["artwork"].pop(cid, None)
            state["uploads"].pop(cid, None)
            state["outdated"].pop(cid, None)
            (srv.THUMB_DIR / f"{cid}.jpg").unlink(missing_ok=True)
        srv._save_artwork_meta(meta)
    _save_state(state)


async def _sync_docent_collection(name: str, content_ids: list[str]) -> None:
    """Point the Docent collection of the same name at the new TV IDs."""
    async with srv._collections_lock:
        data = srv._load_collections()
        for c in data["collections"]:
            if c["name"] == name:
                c["content_ids"] = content_ids
                srv._save_collections(data)


# --- Routes ---

def _summary() -> dict:
    root = _root()
    result = {
        "configured": root is not None,
        "available": False,
        "root": str(root or ""),
        "intervals": list(srv.SLIDESHOW_INTERVALS),
        "collections": [],
        "total": 0,
        "backups": _load_state()["backups"],
    }
    if _available(root):
        mattes = _load_state()["matte"]
        try:
            found = library.scan(root)
        except OSError as e:
            log.warning("Cannot read library folder %s: %s", root, e)
            return result
        result["available"] = True
        result["total"] = sum(map(len, found.values()))
        result["collections"] = [
            {"name": name, "count": len(paths), "matte": mattes.get(name, "none")}
            for name, paths in found.items()
        ]
    return result


def _browsable(path: str) -> Path:
    resolved = Path(path).resolve()
    if not any(resolved.is_relative_to(base.resolve()) for base in BROWSE_ROOTS):
        raise HTTPException(403, "Choose a folder in your home folder or under /Volumes")
    if not resolved.is_dir():
        raise HTTPException(404, "No such folder")
    return resolved


def _chooser_available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _can_pick(request: Request) -> bool:
    """The dialog opens on Docent's computer, so only offer it to a browser there."""
    local = request.client is not None and request.client.host in ("127.0.0.1", "::1")
    return local and _chooser_available()


async def _run_chooser(start: Path | None) -> tuple[int, str, str]:
    """Show the macOS folder dialog; return ``(exit code, stdout, stderr)``."""
    args = [str(start)] if start else []
    process = await asyncio.create_subprocess_exec(
        "osascript", "-e", _CHOOSER_SCRIPT, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=600)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 1, "", "User canceled. (-128)"
    return process.returncode, out.decode(), err.decode()


def _save_root(folder: Path) -> None:
    state = _load_state()
    state["root"] = str(folder)
    _save_state(state)


def _thumbnail(path: Path, size: tuple[int, int] = _THUMB_SIZE) -> bytes:
    stat = path.stat()
    stamp = f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{size}"
    name = hashlib.sha256(stamp.encode()).hexdigest()
    cached = srv.CACHE_DIR / "library" / f"{name}.jpg"
    if cached.exists():
        return cached.read_bytes()
    img = Image.open(path)
    img.draft("RGB", size)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(buf.getvalue())
    return buf.getvalue()


def _check_idle() -> None:
    if job["state"] == "running":
        raise HTTPException(409, "Another library job is already running")


def _start_job(kind: str, body: dict, **options) -> dict:
    global job, task
    paths = _target(body)
    _check_idle()
    collection = body.get("collection") if body.get("paths") is None else None
    job = {"state": "running", "kind": kind, "step": "Checking the TV",
           "done": 0, "total": 0, "message": ""}
    task = asyncio.create_task(_run(kind, paths, collection=collection, **options))
    return job


def _require_root() -> Path:
    _scan()  # raises a helpful error if there is no reachable folder
    return _root()


async def _change_files(change):
    """Run ``change(root, state)`` while no TV job or check is using the library."""
    _check_idle()
    async with state_lock:
        _check_idle()
        root = _require_root()
        state = _load_state()
        try:
            return await asyncio.to_thread(change, root, state)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            log.warning("Library file change failed: %s", e)
            raise HTTPException(500, f"Could not change the files: {e.strerror or e}")
        finally:
            _save_state(state)


def _paths(root: Path, body: dict) -> list[str]:
    """Every path in the request, all checked before any is acted on."""
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("Choose at least one image")
    for rel in paths:
        library.resolve(root, rel)
    return list(dict.fromkeys(paths))


def _carry_crop(state: dict, old: str, new: str, *, moved: bool) -> None:
    """Keep crop settings with a file that was renamed, moved or copied."""
    if old in state["cropped"]:
        state["cropped"].append(new)
        if moved:
            state["cropped"].remove(old)
        return
    crop = state["crops"].get(Path(old).stem.lower())
    if crop:
        state["crops"].setdefault(Path(new).stem.lower(), crop)


def _file_key(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _crop(root: Path, state: dict, rel: str, box: dict) -> None:
    path = library.resolve(root, rel)
    before = _file_key(path)
    library_files.crop_image(root, rel, backup=state["backups"], **box)
    after = _file_key(path)
    if rel not in state["cropped"]:
        state["cropped"].append(rel)
    # Another file with the old content still accounts for the copy on the TV.
    if any(other != rel and entry["key"] == before and (root / other).is_file()
           for other, entry in state["files"].items()):
        return
    for cid, record in list(state["uploads"].items()):
        if record["key"] == before:
            del state["uploads"][cid]
            state["outdated"][cid] = {
                "image_date": record["image_date"], "was": before, "key": after,
                "backed": state["backups"],
            }
    for record in state["outdated"].values():
        if record["key"] == before:  # cropped again before the TV was updated
            record["key"] = after


async def _rename_docent_collection(old: str, new: str) -> None:
    async with srv._collections_lock:
        data = srv._load_collections()
        for c in data["collections"]:
            if c["name"] == old:
                c["name"] = new
        srv._save_collections(data)


def _text(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str):
        raise HTTPException(400, f"{field} is required")
    return value


def _matte(body: dict) -> str:
    matte = str(body.get("matte") or "none")
    srv._validate_content_id(matte)
    return matte


def build_router(server_module) -> APIRouter:
    global srv
    srv = server_module
    router = APIRouter(prefix="/api/library")
    chooser_lock = asyncio.Lock()

    @router.get("")
    async def get_library(request: Request):
        return {**await asyncio.to_thread(_summary), "native_picker": _can_pick(request)}

    @router.post("/pick")
    async def pick_folder(body: dict, request: Request):
        # Requiring a JSON body stops other websites from opening the dialog.
        if not _can_pick(request):
            raise HTTPException(403, "The folder dialog is only available on Docent's own computer")
        _check_idle()
        if chooser_lock.locked():
            raise HTTPException(409, "The folder dialog is already open")
        async with chooser_lock:
            current = _root()
            code, out, err = await _run_chooser(current if _available(current) else None)
        cancelled = code != 0 and "-128" in err
        if code != 0 and not cancelled:
            log.warning("Folder dialog failed: %s", err.strip())
            raise HTTPException(500, "Could not open the folder dialog")
        if not cancelled:
            folder = Path(out.rstrip("\n"))
            if not folder.is_absolute() or not folder.is_dir():
                raise HTTPException(400, "That is not a folder Docent can read")
            _check_idle()
            _save_root(folder)
        return {**await asyncio.to_thread(_summary), "native_picker": True, "cancelled": cancelled}

    @router.put("/settings")
    async def save_settings(body: dict):
        _check_idle()
        folder = _browsable(str(body.get("root") or ""))
        _save_root(folder)
        return await asyncio.to_thread(_summary)

    @router.get("/browse")
    async def browse(path: str = ""):
        folder = _browsable(path or str(BROWSE_ROOTS[0]))
        try:
            folders = sorted(
                (p.name for p in folder.iterdir()
                 if not p.name.startswith((".", "@", "#")) and p.is_dir()),
                key=str.lower,
            )
        except OSError as e:
            raise HTTPException(403, f"Cannot open that folder: {e.strerror}")
        parent = folder.parent
        allowed = any(parent.is_relative_to(base.resolve()) for base in BROWSE_ROOTS)
        return {"path": str(folder), "parent": str(parent) if allowed else None,
                "folders": folders, "places": [str(base) for base in BROWSE_ROOTS]}

    @router.get("/images")
    async def images(collection: str | None = None):
        found = await asyncio.to_thread(_scan)
        if collection and collection not in found:
            raise HTTPException(404, "No such collection folder")
        return {"images": [
            {"path": _rel(name, p), "name": p.name, "collection": name}
            for name, paths in found.items() if collection is None or name == collection
            for p in paths
        ]}

    @router.get("/thumb")
    async def thumb(path: str):
        root = _root()
        try:
            if root is None:
                raise ValueError
            data = await asyncio.to_thread(_thumbnail, library.resolve(root, path))
        except (ValueError, OSError):
            raise HTTPException(404, "No such library image")
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})

    @router.get("/tv")
    async def on_tv():
        _check_idle()
        try:
            keys, _, _, links, outdated = await _link_tv()
        except HTTPException:
            raise
        except Exception as e:
            log.warning("Library TV check failed: %s", e)
            raise HTTPException(502, "Cannot reach TV — is it on and connected?")
        by_key = {key: cid for cid, key in links.items()}
        old = {record["key"]: cid for cid, record in outdated.items()}
        return {
            "on_tv": {rel: by_key[key] for rel, key in keys.items() if key in by_key},
            "outdated": {rel: old[key] for rel, key in keys.items() if key in old},
        }

    @router.post("/plan")
    async def plan_changes(body: dict):
        _check_idle()
        paths = _target(body)
        try:
            plan, files, links, _, outdated = await _plan(paths)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            log.warning("Library plan failed: %s", e)
            raise HTTPException(502, "Cannot reach TV — is it on and connected?")
        return {
            "keep": len(plan["keep"]),
            "upload": [files[key]["path"].name for key in plan["upload"]],
            "delete": [
                {"content_id": c, "file": files[links.get(c) or outdated[c]["key"]]["rel"]}
                for c in plan["delete"]
            ],
            "unknown": plan["unknown"],
        }

    @router.get("/status")
    async def status():
        return job

    @router.post("/load")
    async def load(body: dict):
        return _start_job("load", body, matte=_matte(body))

    @router.post("/display")
    async def display(body: dict):
        paths = body.get("paths")
        if not isinstance(paths, list) or len(paths) != 1:
            raise HTTPException(400, "Choose exactly one image to show")
        return _start_job("display", body, matte=_matte(body))

    @router.post("/update")
    async def update(body: dict):
        return _start_job("update", body, matte=_matte(body))

    @router.post("/unload")
    async def unload(body: dict):
        return _start_job("unload", body)

    @router.post("/show")
    async def show(body: dict):
        interval = body.get("interval", 15)
        if interval not in srv.SLIDESHOW_INTERVALS:
            raise HTTPException(400, f"interval must be one of {list(srv.SLIDESHOW_INTERVALS)}")
        delete_unknown = body.get("delete_unknown") or []
        srv._validate_content_ids(delete_unknown)
        return _start_job(
            "show", body, interval=interval, shuffle=bool(body.get("shuffle", True)),
            matte=_matte(body), delete_unknown=frozenset(delete_unknown),
        )

    # --- file management ---

    @router.put("/options")
    async def options(body: dict):
        if not isinstance(body.get("backups"), bool):
            raise HTTPException(400, "backups must be true or false")
        await _change_files(lambda root, state: state.update(backups=body["backups"]))
        return {"backups": body["backups"]}

    @router.post("/files")
    async def add_files(
        request: Request, collection: str = Form(""), files: list[UploadFile] = File(...)
    ):
        # A form post needs no permission check by the browser, so check here.
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "Images can only be added from Docent's own page")
        added, errors = [], []
        for upload in files:
            name = Path(upload.filename or "").name
            data = await upload.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                errors.append({"name": name, "error": "The file is larger than 100 MB"})
                continue
            try:
                added.append(await _change_files(
                    lambda root, state: library_files.add_image(root, collection, name, data)))
            except HTTPException as e:
                if e.status_code == 409:
                    raise
                errors.append({"name": name, "error": e.detail})
        return {"added": added, "errors": errors}

    @router.post("/files/rename")
    async def rename_file(body: dict):
        old, name = _text(body, "path"), _text(body, "name")

        def change(root, state):
            new = library_files.rename_image(root, old, name)
            _carry_crop(state, old, new, moved=True)
            return {"path": new}

        return await _change_files(change)

    async def transfer(body: dict, operation, *, moved: bool):
        collection = _text(body, "collection")

        def change(root, state):
            results = []
            library_files.collection_dir(root, collection)
            for rel in _paths(root, body):
                new = operation(root, rel, collection)
                if new != rel:
                    _carry_crop(state, rel, new, moved=moved)
                results.append(new)
            return {"paths": results}

        return await _change_files(change)

    @router.post("/files/move")
    async def move_files(body: dict):
        return await transfer(body, library_files.move_image, moved=True)

    @router.post("/files/copy")
    async def copy_files(body: dict):
        return await transfer(body, library_files.copy_image, moved=False)

    @router.post("/files/delete")
    async def delete_files(body: dict):
        def change(root, state):
            paths = _paths(root, body)
            for rel in paths:
                library_files.delete_image(root, rel, backup=state["backups"])
                if rel in state["cropped"]:
                    state["cropped"].remove(rel)
            return {"deleted": len(paths)}

        return await _change_files(change)

    @router.get("/preview")
    async def preview(path: str):
        try:
            root = _require_root()
            data = await asyncio.to_thread(
                _thumbnail, library.resolve(root, path), _PREVIEW_SIZE)
        except (ValueError, OSError):
            raise HTTPException(404, "No such library image")
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})

    @router.post("/crop")
    async def crop(body: dict):
        rel = _text(body, "path")
        box = {k: body.get(k) for k in ("left", "top", "width")}
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box.values()):
            raise HTTPException(400, "left, top and width are required numbers")
        await _change_files(lambda root, state: _crop(root, state, rel, box))
        return {"path": rel}

    @router.post("/collections")
    async def create_collection(body: dict):
        name = _text(body, "name")
        return {"name": await _change_files(
            lambda root, state: library_files.create_collection(root, name))}

    @router.put("/collections")
    async def rename_collection(body: dict):
        old, wanted = _text(body, "name"), _text(body, "new_name")

        def change(root, state):
            new = library_files.rename_collection(root, old, wanted)
            prefix = f"{old}/"
            state["cropped"] = [
                f"{new}/{rel[len(prefix):]}" if rel.startswith(prefix) else rel
                for rel in state["cropped"]
            ]
            if old in state["matte"]:
                state["matte"][new] = state["matte"].pop(old)
            return new

        new = await _change_files(change)
        await _rename_docent_collection(old, new)
        return {"name": new}

    @router.post("/collections/delete")
    async def delete_collection(body: dict):
        name = _text(body, "name")
        return await _change_files(lambda root, state: library_files.delete_collection(
            root, name, backup=state["backups"]))

    @router.get("/backups")
    async def backups():
        root = _require_root()
        return {"backups": await asyncio.to_thread(library_files.list_backups, root)}

    @router.get("/backups/thumb")
    async def backup_thumb(id: str):
        try:
            root = _require_root()
            data = await asyncio.to_thread(
                lambda: _thumbnail(library_files.backup_path(root, id)))
        except (ValueError, OSError):
            raise HTTPException(404, "No such backup")
        return Response(content=data, media_type="image/jpeg")

    @router.post("/backups/restore")
    async def restore(body: dict):
        backup_id = _text(body, "id")
        return {"path": await _change_files(
            lambda root, state: library_files.restore_backup(root, backup_id))}

    return router
