"""File management through the API, and how it affects copies already on the TV."""
# Pytest fixtures imported from another module look like redefinitions.
# ruff: noqa: F811
from __future__ import annotations

import io

from PIL import Image

import library_routes

from test_library_switch import _act, _jpeg, _job, frame, lib  # noqa: F401  (fixtures)


async def _post(client, endpoint, /, **body):
    return await client.post(f"/api/library/{endpoint}", json=body)


async def _on_tv(client):
    return (await client.get("/api/library/tv")).json()


async def test_upload_adds_files_to_a_collection(client, lib):
    files = [("files", ("new.jpg", _jpeg(7), "image/jpeg")), ("files", ("bad.jpg", b"nope", "image/jpeg"))]

    data = (await client.post("/api/library/files", data={"collection": "Winter"}, files=files)).json()

    assert data["added"] == ["Winter/new.jpg"]
    assert data["errors"][0]["name"] == "bad.jpg"
    assert (lib / "Winter" / "new.jpg").exists()


async def test_upload_from_another_website_is_refused(client, lib):
    resp = await client.post(
        "/api/library/files", data={"collection": "Winter"},
        files=[("files", ("new.jpg", _jpeg(7), "image/jpeg"))],
        headers={"Origin": "https://example.com"},
    )

    assert resp.status_code == 403 and not (lib / "Winter" / "new.jpg").exists()


async def test_rename_and_move_keep_the_tv_link(client, lib, frame):
    await _act(client, "load", paths=["Winter/pic3.jpg", "Winter/pic4.jpg"])

    renamed = await _post(client, "files/rename", path="Winter/pic3.jpg", name="snow")
    moved = await _post(client, "files/move", paths=["Winter/pic4.jpg"], collection="Halloween")

    assert renamed.json()["path"] == "Winter/snow.jpg"
    assert moved.json()["paths"] == ["Halloween/pic4.jpg"]
    assert sorted((await _on_tv(client))["on_tv"]) == ["Halloween/pic4.jpg", "Winter/snow.jpg"]
    assert frame.uploads == 2


async def test_copy_and_delete_use_the_backup_setting(client, lib):
    await _post(client, "files/copy", paths=["Winter/pic3.jpg"], collection="Halloween")
    await _post(client, "files/delete", paths=["Winter/pic3.jpg"])
    assert len((await client.get("/api/library/backups")).json()["backups"]) == 1

    await client.put("/api/library/options", json={"backups": False})
    await _post(client, "files/delete", paths=["Winter/pic4.jpg"])

    assert len((await client.get("/api/library/backups")).json()["backups"]) == 1
    assert (await client.get("/api/library")).json()["backups"] is False
    assert (lib / "Halloween" / "pic3.jpg").exists()


async def test_one_bad_path_stops_the_whole_request(client, lib):
    resp = await _post(client, "files/delete", paths=["Winter/pic3.jpg", "../x.jpg"])

    assert resp.status_code == 400 and (lib / "Winter" / "pic3.jpg").exists()


async def test_restore(client, lib):
    await _post(client, "files/delete", paths=["Winter/pic3.jpg"])
    [entry] = (await client.get("/api/library/backups")).json()["backups"]

    thumb = await client.get("/api/library/backups/thumb", params={"id": entry["id"]})
    resp = await _post(client, "backups/restore", id=entry["id"])

    assert thumb.status_code == 200
    assert resp.json()["path"] == "Winter/pic3.jpg" and (lib / "Winter" / "pic3.jpg").exists()


async def test_collection_management_keeps_docent_collection_in_step(client, lib):
    import json
    import server
    server.COLLECTIONS_FILE.write_text(json.dumps({"collections": [
        {"id": "w", "name": "Winter", "content_ids": [], "created": "x"}]}))

    await _post(client, "collections", name="Spring")
    await client.put("/api/library/collections", json={"name": "Winter", "new_name": "Snow"})
    gone = await _post(client, "collections/delete", name="Halloween")

    assert sorted(p.name for p in lib.iterdir() if not p.name.startswith(".")) == ["Snow", "Spring"]
    assert gone.json()["folder_removed"] is True
    assert json.loads(server.COLLECTIONS_FILE.read_text())["collections"][0]["name"] == "Snow"


async def test_file_changes_wait_for_tv_jobs(client, lib, monkeypatch):
    monkeypatch.setattr(library_routes, "job", {"state": "running"})

    resp = await _post(client, "files/delete", paths=["Winter/pic3.jpg"])

    assert resp.status_code == 409 and (lib / "Winter" / "pic3.jpg").exists()


async def test_preview_gives_size_as_displayed(client, lib):
    resp = await client.get("/api/library/preview", params={"path": "Winter/pic3.jpg"})

    assert Image.open(io.BytesIO(resp.content)).width == 1600


async def _crop_a_loaded_image(client, frame):
    await _act(client, "load", paths=["Winter/pic3.jpg"])
    [old] = frame.images
    resp = await _post(client, "crop", path="Winter/pic3.jpg", left=0, top=0, width=0.5)
    assert resp.status_code == 200
    return old


async def test_crop_marks_the_tv_copy_outdated(client, lib, frame):
    old = await _crop_a_loaded_image(client, frame)

    data = await _on_tv(client)

    assert data["outdated"] == {"Winter/pic3.jpg": old} and data["on_tv"] == {}


async def test_update_replaces_the_outdated_copy(client, lib, frame):
    old = await _crop_a_loaded_image(client, frame)

    await _act(client, "update", paths=["Winter/pic3.jpg"])

    assert (await _job(client))["state"] == "done"
    assert old not in frame.images and len(frame.images) == 1
    assert Image.open(io.BytesIO(next(iter(frame.images.values())))).width == 1920
    assert (await _on_tv(client))["outdated"] == {}


async def test_unload_also_removes_the_outdated_copy(client, lib, frame):
    await _crop_a_loaded_image(client, frame)

    await _act(client, "unload", paths=["Winter/pic3.jpg"])

    assert frame.images == {}


async def test_replace_removes_a_backed_up_outdated_copy(client, lib, frame):
    await _crop_a_loaded_image(client, frame)

    plan = (await _post(client, "plan", collection="Halloween")).json()
    await _act(client, "show", collection="Halloween", interval=15)

    assert len(plan["delete"]) == 1 and plan["unknown"] == []
    assert len(frame.images) == 2


async def test_replace_keeps_an_outdated_copy_without_backup(client, lib, frame):
    await client.put("/api/library/options", json={"backups": False})
    old = await _crop_a_loaded_image(client, frame)

    await _act(client, "show", collection="Halloween", interval=15)

    assert old in frame.images  # now the only copy of the uncropped picture


async def test_saved_upload_crop_is_not_applied_after_cropping(client, lib, frame):
    big = Image.new("RGB", (6000, 4000), "red")
    big.save(lib / "Winter" / "big.jpg")
    import server
    server.ARTWORK_META_FILE.write_text(__import__("json").dumps({"artwork": {"X": {
        "original_filename": "big_cropped.jpg", "crop_box": {"x": 0, "y": 0, "w": 3000, "h": 1688}}}}))

    await _post(client, "crop", path="Winter/big.jpg", left=0, top=0, width=1)
    await _act(client, "load", paths=["Winter/big.jpg"])

    sent = Image.open(io.BytesIO(next(iter(frame.images.values()))))
    assert sent.size == (3840, 2160)
    assert (await _job(client))["state"] == "done"
    state = library_routes._load_state()
    assert state["files"]["Winter/big.jpg"]["stamp"][2] is None
