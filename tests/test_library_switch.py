"""Swapping a library folder onto the Frame, end to end against a fake TV."""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image, ImageDraw

import library_routes
import server


def _jpeg(seed: int, size=(3840, 2160)) -> bytes:
    img = Image.new("RGB", size, ((seed * 53) % 255, (seed * 97) % 255, (seed * 151) % 255))
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse([(seed * 400) % (w - 900), h // 4, (seed * 400) % (w - 900) + 900, h * 3 // 4], fill="white")
    d.rectangle([0, h - h // (seed + 2), w // 2, h], fill="black")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class FakeFrame:
    """Just enough of SamsungTVArt, holding real image bytes."""

    def __init__(self):
        self.images: dict[str, bytes] = {}
        self.dates: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.next_id = 1
        self.slideshow = {"value": "off"}
        self.fail_upload_after: int | None = None
        self.uploads = 0

    def add(self, data: bytes) -> str:
        cid = f"MY_F{self.next_id:04d}"
        self.images[cid] = data
        self.dates[cid] = f"date-{self.next_id}"
        self.next_id += 1
        return cid

    def open(self):
        pass

    def available(self):
        return [
            {"content_id": c, "category_id": "MY-C0002", "image_date": self.dates[c], "matte_id": "none"}
            for c in self.images
        ]

    def get_current(self):
        return {"content_id": next(iter(self.images), None)}

    def get_thumbnail_list(self, ids):
        out = {}
        for cid in ids:
            img = Image.open(io.BytesIO(self.images[cid])).resize((384, 216))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
            out[f"{cid}.jpg"] = bytearray(buf.getvalue())
        return out

    def upload(self, data, matte=None, file_type="jpg"):
        if self.fail_upload_after is not None and self.uploads >= self.fail_upload_after:
            raise OSError("TV went away")
        self.uploads += 1
        cid = self.add(bytes(data))
        self.calls.append(("upload", cid, matte))
        return cid

    def select_image(self, cid, category=None, show=True):
        self.calls.append(("select", cid))

    def delete_list(self, ids):
        self.calls.append(("delete", sorted(ids)))
        for cid in ids:
            self.images.pop(cid, None)
        return True

    def set_slideshow_status(self, duration=0, type=True, category=2, category_id=None):
        self.calls.append(("slideshow", duration, type, category_id))
        self.slideshow = {"value": str(duration) if duration else "off"}

    def get_slideshow_status(self):
        return self.slideshow


@pytest.fixture
def frame(monkeypatch, mock_tv):
    tv = FakeFrame()
    monkeypatch.setattr(server, "get_tv", lambda: type("TV", (), {"art": lambda s: tv, "close": lambda s: None})())
    monkeypatch.setattr(server, "TV_RETRY_DELAY", 0)
    return tv


@pytest.fixture
def lib(tmp_path, monkeypatch):
    root = tmp_path / "library"
    for folder, seeds in (("Halloween", (1, 2)), ("Winter", (3, 4, 5))):
        (root / folder).mkdir(parents=True)
        for s in seeds:
            (root / folder / f"pic{s}.jpg").write_bytes(_jpeg(s))
    monkeypatch.setattr(server, "LIBRARY_DIR", root)
    monkeypatch.setattr(server, "LIBRARY_STATE_FILE", tmp_path / "library_state.json")
    monkeypatch.setattr(library_routes, "job", {"state": "idle"})
    monkeypatch.setattr(library_routes, "task", None)
    return root


async def _show(client, **body):
    body = {"collection": "Winter", "interval": 15, "shuffle": True, "matte": "none", **body}
    resp = await client.post("/api/library/show", json=body)
    if resp.status_code == 200:
        await library_routes.task
    return resp


async def _job(client):
    return (await client.get("/api/library/status")).json()


async def test_lists_folders_as_collections(client, lib):
    data = (await client.get("/api/library")).json()

    assert data["configured"] and data["available"]
    assert [(c["name"], c["count"]) for c in data["collections"]] == [("Halloween", 2), ("Winter", 3)]


async def test_unreachable_library_is_reported(client, lib, monkeypatch):
    monkeypatch.setattr(server, "LIBRARY_DIR", lib / "not-mounted")

    assert (await client.get("/api/library")).json()["available"] is False
    resp = await client.post("/api/library/plan", json={"collection": "Winter"})
    assert resp.status_code == 503


async def test_plan_separates_backed_up_and_unknown_images(client, lib, frame):
    backed = frame.add((lib / "Halloween" / "pic1.jpg").read_bytes())
    stranger = frame.add(_jpeg(9))

    plan = (await client.post("/api/library/plan", json={"collection": "Winter"})).json()

    assert plan["upload"] == ["pic3.jpg", "pic4.jpg", "pic5.jpg"]
    assert plan["delete"] == [{"content_id": backed, "file": "Halloween/pic1.jpg"}]
    assert plan["unknown"] == [stranger]
    assert frame.calls == []  # planning changes nothing


async def test_show_uploads_then_deletes_then_starts_slideshow(client, lib, frame):
    old = frame.add((lib / "Halloween" / "pic1.jpg").read_bytes())
    stranger = frame.add(_jpeg(9))

    await _show(client, matte="shadowbox_polar")

    assert (await _job(client))["state"] == "done"
    assert [c[0] for c in frame.calls] == ["upload"] * 3 + ["select", "delete", "slideshow"]
    assert frame.calls[0][2] == "shadowbox_polar"
    assert old not in frame.images
    assert stranger in frame.images  # never deleted without permission
    assert frame.calls[-1] == ("slideshow", 15, True, "MY-C0002")


async def test_show_deletes_unknown_images_only_when_listed(client, lib, frame):
    stranger = frame.add(_jpeg(9))

    await _show(client, delete_unknown=[stranger])

    assert stranger not in frame.images


async def test_failed_upload_deletes_nothing_and_rerun_resumes(client, lib, frame):
    old = frame.add((lib / "Halloween" / "pic1.jpg").read_bytes())
    frame.fail_upload_after = 2

    await _show(client)

    assert (await _job(client))["state"] == "error"
    assert old in frame.images
    assert not any(c[0] in ("delete", "slideshow") for c in frame.calls)

    frame.fail_upload_after = None
    await _show(client)

    assert (await _job(client))["state"] == "done"
    assert frame.uploads == 3  # the two that made it were not sent again
    assert len(frame.images) == 3


async def test_switching_back_and_forth_leaves_only_that_collection(client, lib, frame):
    await _show(client, collection="Halloween")
    await _show(client, collection="Winter")
    await _show(client, collection="Halloween")

    assert sorted(frame.images.values()) == sorted(p.read_bytes() for p in (lib / "Halloween").iterdir())


async def test_reused_content_id_is_not_trusted(client, lib, frame):
    """If the TV reuses an ID for a different picture, it is not 'backed up'."""
    await _show(client, collection="Halloween")
    cid = next(iter(frame.images))
    frame.images[cid] = _jpeg(9)
    frame.dates[cid] = "someone-else"

    plan = (await client.post("/api/library/plan", json={"collection": "Winter"})).json()

    assert cid in plan["unknown"]


async def test_show_updates_matching_docent_collection(client, lib, frame):
    server.COLLECTIONS_FILE.write_text(json.dumps({"collections": [
        {"id": "w", "name": "Winter", "content_ids": ["STALE"], "created": "x"}]}))

    await _show(client)

    saved = json.loads(server.COLLECTIONS_FILE.read_text())["collections"]
    assert sorted(saved[0]["content_ids"]) == sorted(frame.images)


async def test_unreadable_file_changes_nothing(client, lib, frame):
    old = frame.add((lib / "Halloween" / "pic1.jpg").read_bytes())
    (lib / "Winter" / "broken.jpg").write_bytes(b"not an image")

    await _show(client)

    job = await _job(client)
    assert job["state"] == "error" and "broken.jpg" in job["message"]
    assert frame.calls == [] and old in frame.images


@pytest.mark.parametrize("body", [{"interval": 7}, {"collection": "Nope"}, {"collection": "../x"}])
async def test_show_rejects_bad_requests(client, lib, frame, body):
    resp = await _show(client, **body)

    assert resp.status_code in (400, 404)
    assert frame.calls == []


# --- Local Library view ---

async def _act(client, action, **body):
    resp = await client.post(f"/api/library/{action}", json=body)
    if resp.status_code == 200:
        await library_routes.task
    return resp


async def test_load_adds_images_and_removes_nothing(client, lib, frame):
    stranger = frame.add(_jpeg(9))

    await _act(client, "load", paths=["Winter/pic3.jpg"])

    assert (await _job(client))["state"] == "done"
    assert stranger in frame.images and len(frame.images) == 2
    assert [c[0] for c in frame.calls] == ["upload"]


async def test_load_skips_images_already_on_the_tv(client, lib, frame):
    await _act(client, "load", collection="Halloween")
    await _act(client, "load", collection="Halloween")

    assert frame.uploads == 2


async def test_unload_removes_only_the_chosen_images(client, lib, frame):
    stranger = frame.add(_jpeg(9))
    await _act(client, "load", collection="Halloween")
    await _act(client, "load", collection="Winter")

    await _act(client, "unload", collection="Halloween")

    assert (await _job(client))["state"] == "done"
    assert stranger in frame.images and len(frame.images) == 4
    assert all(p.exists() for p in (lib / "Halloween").iterdir())


async def test_show_accepts_a_selection(client, lib, frame):
    await _act(client, "load", collection="Winter")

    await _act(client, "show", paths=["Winter/pic3.jpg"], interval=3, shuffle=False)

    assert len(frame.images) == 1


async def test_lists_images_and_says_which_are_on_the_tv(client, lib, frame):
    (lib / "loose.jpg").write_bytes(_jpeg(8))
    cid = frame.add((lib / "Winter" / "pic3.jpg").read_bytes())

    images = (await client.get("/api/library/images")).json()["images"]
    main = (await client.get("/api/library/images", params={"collection": ""})).json()["images"]
    on_tv = (await client.get("/api/library/tv")).json()["on_tv"]

    assert len(images) == 6
    assert [i["path"] for i in main] == ["loose.jpg"]
    assert on_tv == {"Winter/pic3.jpg": cid}


async def test_thumbnail_is_made_from_the_local_file(client, lib):
    resp = await client.get("/api/library/thumb", params={"path": "Winter/pic3.jpg"})

    assert resp.status_code == 200
    assert Image.open(io.BytesIO(resp.content)).width <= 480


async def test_paths_outside_the_library_are_rejected(client, lib, frame):
    thumb = await client.get("/api/library/thumb", params={"path": "../x.jpg"})
    load = await _act(client, "load", paths=["../x.jpg"])

    assert thumb.status_code == 404 and load.status_code == 404
    assert frame.calls == []


async def test_saved_folder_replaces_the_default(client, lib, tmp_path, monkeypatch):
    monkeypatch.setattr(library_routes, "BROWSE_ROOTS", [tmp_path])
    other = tmp_path / "other"
    (other / "Spring").mkdir(parents=True)

    resp = await client.put("/api/library/settings", json={"root": str(other)})

    assert resp.json()["root"] == str(other)
    assert [c["name"] for c in (await client.get("/api/library")).json()["collections"]] == ["Spring"]


async def test_folder_browser_stays_inside_allowed_places(client, lib, tmp_path, monkeypatch):
    monkeypatch.setattr(library_routes, "BROWSE_ROOTS", [tmp_path])

    inside = await client.get("/api/library/browse", params={"path": str(tmp_path)})
    outside = await client.get("/api/library/browse", params={"path": "/etc"})
    save = await client.put("/api/library/settings", json={"root": "/etc"})

    assert "library" in inside.json()["folders"]
    assert outside.status_code == 403 and save.status_code == 403


# --- Native folder chooser ---

@pytest.fixture
def chooser(monkeypatch):
    """Stand in for the macOS folder dialog."""
    calls = {"start": None, "result": (0, "", "")}

    async def _run(start):
        calls["start"] = start
        return calls["result"]

    monkeypatch.setattr(library_routes, "_chooser_available", lambda: True)
    monkeypatch.setattr(library_routes, "_run_chooser", _run)
    return calls


async def test_chooser_saves_the_picked_folder(client, lib, chooser, tmp_path):
    picked = tmp_path / "picked"
    (picked / "Spring").mkdir(parents=True)
    chooser["result"] = (0, f"{picked}/\n", "")

    data = (await client.post("/api/library/pick", json={})).json()

    assert data["root"] == str(picked) and data["cancelled"] is False
    assert chooser["start"] == lib  # opens at the current folder
    assert (await client.get("/api/library")).json()["root"] == str(picked)


async def test_cancelling_the_chooser_changes_nothing(client, lib, chooser):
    chooser["result"] = (1, "", "execution error: User canceled. (-128)")

    data = (await client.post("/api/library/pick", json={})).json()

    assert data["cancelled"] is True and data["root"] == str(lib)


async def test_chooser_failure_is_reported(client, lib, chooser):
    chooser["result"] = (1, "", "execution error: no window server (-1712)")

    assert (await client.post("/api/library/pick", json={})).status_code == 500


async def test_chooser_is_only_offered_to_a_browser_on_this_computer(lib, chooser):
    from httpx import ASGITransport, AsyncClient

    remote = ASGITransport(app=server.app, client=("10.0.1.50", 5000))
    async with AsyncClient(transport=remote, base_url="http://test") as other:
        assert (await other.get("/api/library")).json()["native_picker"] is False
        assert (await other.post("/api/library/pick", json={})).status_code == 403
    assert chooser["start"] is None


async def test_local_browser_is_offered_the_chooser(client, lib, chooser):
    assert (await client.get("/api/library")).json()["native_picker"] is True
