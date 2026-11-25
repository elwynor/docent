"""Folder library: each sub-folder is a collection that can be swapped onto the Frame."""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

import library


def _picture(seed: int, size=(1600, 900)) -> Image.Image:
    img = Image.new("RGB", size, ((seed * 53) % 255, (seed * 97) % 255, (seed * 151) % 255))
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse([w * 0.1 * seed % w, h // 4, w * 0.1 * seed % w + w // 4, h * 3 // 4], fill="white")
    d.rectangle([0, h - h // (seed + 2), w // 2, h], fill="black")
    return img


def _jpeg(img: Image.Image, quality=95) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def test_scan_treats_each_folder_as_a_collection(tmp_path):
    (tmp_path / "Halloween" / "@eaDir").mkdir(parents=True)
    (tmp_path / "#recycle").mkdir()
    (tmp_path / "Winter").mkdir()
    for name in ("Halloween/b.JPG", "Halloween/a.jpeg", "Halloween/.DS_Store",
                 "Halloween/._a.jpeg", "Halloween/notes.txt", "#recycle/x.jpg", "Winter/c.png"):
        (tmp_path / name).write_bytes(b"x")

    found = library.scan(tmp_path)

    assert {k: [p.name for p in v] for k, v in found.items()} == {
        "Halloween": ["a.jpeg", "b.JPG"], "Winter": ["c.png"],
    }


def test_render_passes_frame_ready_jpeg_through_unchanged(tmp_path):
    data = _jpeg(_picture(1, (3840, 2160)))
    path = tmp_path / "a.jpg"
    path.write_bytes(data)

    assert library.render(path) == data


def test_render_applies_saved_crop_and_limits_size(tmp_path):
    img = Image.new("RGB", (6000, 4000), "red")
    ImageDraw.Draw(img).rectangle([0, 1000, 6000, 4000], fill="blue")
    path = tmp_path / "big.jpg"
    img.save(path)

    out = Image.open(io.BytesIO(library.render(path, {"x": 0, "y": 600, "w": 6000, "h": 3375})))

    assert out.size == (3840, 2160)
    r, _, b = out.getpixel((100, 100))
    assert r > 200 and b < 50  # top of the crop is still in the red band


def test_render_centre_crops_when_no_crop_saved(tmp_path):
    path = tmp_path / "tall.png"
    _picture(2, (1000, 1000)).save(path)

    w, h = Image.open(io.BytesIO(library.render(path))).size

    assert abs(w / h - 16 / 9) < 0.01


def test_match_links_thumbnails_to_their_files():
    files = {f"key{n}": library.signature(_jpeg(_picture(n))) for n in range(1, 6)}
    thumbs = {
        f"MY_F000{n}": library.signature(_jpeg(_picture(n).resize((384, 216)), quality=70))
        for n in (2, 4)
    }

    assert library.match(thumbs, files) == {"MY_F0002": "key2", "MY_F0004": "key4"}


def test_match_leaves_unknown_pictures_unlinked():
    files = {"key1": library.signature(_jpeg(_picture(1)))}
    thumbs = {"MY_F0009": library.signature(_jpeg(_picture(7)))}

    assert library.match(thumbs, files) == {}


def test_match_refuses_to_guess_between_lookalikes():
    base = _picture(3)
    variant = base.copy()
    ImageDraw.Draw(variant).rectangle([700, 100, 900, 300], fill="green")
    files = {"plain": library.signature(_jpeg(base)), "edited": library.signature(_jpeg(variant))}
    between = base.copy()
    ImageDraw.Draw(between).rectangle([700, 100, 800, 300], fill="green")

    assert library.match({"MY_F0001": library.signature(_jpeg(between))}, files) == {}


def test_plan_keeps_uploads_and_deletes_only_backed_up_images():
    plan = library.make_plan(
        target=["a", "b", "c"],
        links={"MY1": "a", "MY2": "z", "MY3": "a"},
        tv_ids=["MY1", "MY2", "MY3", "MY4"],
    )

    assert plan == {
        "keep": {"MY1": "a"},
        "upload": ["b", "c"],
        "delete": ["MY2", "MY3"],
        "unknown": ["MY4"],
    }


def test_plan_ignores_links_to_images_no_longer_on_tv():
    plan = library.make_plan(target=["a"], links={"GONE": "a"}, tv_ids=[])

    assert plan["upload"] == ["a"]
    assert plan["delete"] == []


def test_plan_rejects_empty_collection():
    """An empty or unreadable folder must never empty the TV."""
    with pytest.raises(ValueError):
        library.make_plan(target=[], links={"MY1": "a"}, tv_ids=["MY1"])


def test_scan_includes_images_in_the_main_folder(tmp_path):
    (tmp_path / "Winter").mkdir()
    (tmp_path / "loose.jpg").write_bytes(b"x")

    found = library.scan(tmp_path)

    assert [p.name for p in found[""]] == ["loose.jpg"]
    assert found["Winter"] == []


@pytest.mark.parametrize("rel", [
    "../outside.jpg", "/etc/passwd", ".backups/a.jpg", "Winter/../../outside.jpg",
    "Winter/.hidden.jpg", "a/b/c.jpg", "Winter/notes.txt", "missing.jpg", "",
])
def test_resolve_rejects_paths_outside_the_library(tmp_path, rel):
    root = tmp_path / "lib"
    (root / "Winter").mkdir(parents=True)
    (root / ".backups").mkdir()
    for p in (tmp_path / "outside.jpg", root / ".backups" / "a.jpg",
              root / "Winter" / ".hidden.jpg", root / "Winter" / "notes.txt"):
        p.write_bytes(b"x")

    with pytest.raises(ValueError):
        library.resolve(root, rel)


def test_resolve_returns_library_images(tmp_path):
    (tmp_path / "Winter").mkdir()
    (tmp_path / "Winter" / "a.jpg").write_bytes(b"x")

    assert library.resolve(tmp_path, "Winter/a.jpg") == tmp_path / "Winter" / "a.jpg"
