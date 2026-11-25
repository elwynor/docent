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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from PIL import Image, ImageOps
from samsungtvws.exceptions import ConnectionFailure

import library

log = logging.getLogger("docent")

_MY_PHOTOS = "MY-C0002"
_SLIDESHOW_INTERVALS = (3, 15, 60, 720, 1440)  # minutes
_THUMB_SIZE = (480, 270)
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


def _load_state() -> dict:
    state = srv._safe_load_json(srv.LIBRARY_STATE_FILE, dict)
    for key in ("files", "uploads", "crops", "matte", "meta"):
        state.setdefault(key, {})
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
                crop = state["crops"].get(path.stem.lower())
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


async def _link_tv() -> tuple[dict, dict, dict, dict]:
    """Match the TV's images to library files.  Changes nothing on the TV.

    Returns ``(keys by path, file info by key, TV images, links by content ID)``.
    """
    state = _load_state()
    keys, files = await asyncio.to_thread(_index_library, state)
    on_tv = await srv._tv_op(_my_photos)

    links, unrecorded = {}, []
    for cid, item in on_tv.items():
        record = state["uploads"].get(cid)
        # The date guards against the TV reusing an ID for another picture.
        if record and record["image_date"] == item.get("image_date") and record["key"] in files:
            links[cid] = record["key"]
        else:
            unrecorded.append(cid)

    if unrecorded:
        thumbs = await srv._tv_op(lambda art: _thumbnails_for(art, unrecorded), timeout=90)
        matched = await asyncio.to_thread(
            library.match,
            {cid: library.signature(data) for cid, data in thumbs.items() if cid in on_tv},
            {key: info["sig"] for key, info in files.items()},
        )
        links.update(matched)

    state["uploads"] = {
        cid: {"key": key, "image_date": on_tv[cid].get("image_date")} for cid, key in links.items()
    }
    _save_state(state)
    return keys, files, on_tv, links


async def _plan(paths: list[str]) -> tuple[dict, dict, dict, dict]:
    keys, files, on_tv, links = await _link_tv()
    plan = library.make_plan([keys[rel] for rel in paths], links, list(on_tv))
    return plan, files, links, keys


async def _run(kind: str, paths: list[str], *, interval: int = 15, shuffle: bool = True,
               matte: str = "none", delete_unknown: frozenset = frozenset(),
               collection: str | None = None) -> None:
    """Carry out a ``load``, ``unload`` or ``show`` (replace) job.

    Uploads everything before deleting anything, so a failure leaves the old
    pictures in place, and running it again carries on where it stopped.
    """
    try:
        plan, files, links, keys = await _plan(paths)
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
        elif kind == "unload":
            remove = [cid for cid, key in links.items() if key in set(target)]
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
        "intervals": list(_SLIDESHOW_INTERVALS),
        "collections": [],
        "total": 0,
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


def _thumbnail(path: Path) -> bytes:
    stat = path.stat()
    name = hashlib.sha256(f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()
    cached = srv.CACHE_DIR / "library" / f"{name}.jpg"
    if cached.exists():
        return cached.read_bytes()
    img = Image.open(path)
    img.draft("RGB", _THUMB_SIZE)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail(_THUMB_SIZE, Image.LANCZOS)
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
            keys, _, _, links = await _link_tv()
        except HTTPException:
            raise
        except Exception as e:
            log.warning("Library TV check failed: %s", e)
            raise HTTPException(502, "Cannot reach TV — is it on and connected?")
        by_key = {key: cid for cid, key in links.items()}
        return {"on_tv": {rel: by_key[key] for rel, key in keys.items() if key in by_key}}

    @router.post("/plan")
    async def plan_changes(body: dict):
        _check_idle()
        paths = _target(body)
        try:
            plan, files, links, _ = await _plan(paths)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            log.warning("Library plan failed: %s", e)
            raise HTTPException(502, "Cannot reach TV — is it on and connected?")
        return {
            "keep": len(plan["keep"]),
            "upload": [files[key]["path"].name for key in plan["upload"]],
            "delete": [{"content_id": c, "file": files[links[c]]["rel"]} for c in plan["delete"]],
            "unknown": plan["unknown"],
        }

    @router.get("/status")
    async def status():
        return job

    @router.post("/load")
    async def load(body: dict):
        return _start_job("load", body, matte=_matte(body))

    @router.post("/unload")
    async def unload(body: dict):
        return _start_job("unload", body)

    @router.post("/show")
    async def show(body: dict):
        interval = body.get("interval", 15)
        if interval not in _SLIDESHOW_INTERVALS:
            raise HTTPException(400, f"interval must be one of {list(_SLIDESHOW_INTERVALS)}")
        delete_unknown = body.get("delete_unknown") or []
        srv._validate_content_ids(delete_unknown)
        return _start_job(
            "show", body, interval=interval, shuffle=bool(body.get("shuffle", True)),
            matte=_matte(body), delete_unknown=frozenset(delete_unknown),
        )

    return router
