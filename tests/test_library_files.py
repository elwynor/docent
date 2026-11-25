"""Library file management: nothing is overwritten, escaped from, or lost."""
from __future__ import annotations

import io

import pytest
from PIL import Image

import library_files as lf


def _jpeg(color="red", size=(1600, 1200), **save) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", **save)
    return buf.getvalue()


@pytest.fixture
def root(tmp_path):
    lib = tmp_path / "lib"
    (lib / "Winter").mkdir(parents=True)
    (lib / "Spring").mkdir()
    (lib / "Winter" / "a.jpg").write_bytes(_jpeg("red"))
    (lib / "Winter" / "b.jpg").write_bytes(_jpeg("blue"))
    (tmp_path / "outside.jpg").write_bytes(_jpeg("green"))
    return lib


def _backups(root):
    return sorted(p.relative_to(root / ".backups").as_posix()
                  for p in (root / ".backups").rglob("*") if p.is_file())


# --- names and paths ---

@pytest.mark.parametrize("name", [
    "", " ", ".", "..", ".hidden", "@eaDir", "#recycle", "a/b", "a\\b", "a:b", "a?b", "a\nb", "x" * 200,
])
def test_bad_names_are_rejected_everywhere(root, name):
    for action in (
        lambda: lf.create_collection(root, name),
        lambda: lf.rename_collection(root, "Winter", name),
        lambda: lf.rename_image(root, "Winter/a.jpg", name),
        lambda: lf.add_image(root, "Winter", name + ".jpg" if name else name, _jpeg()),
    ):
        with pytest.raises(ValueError):
            action()


@pytest.mark.parametrize("collection", ["..", "../..", ".backups", "Missing", "Winter/x"])
def test_destinations_outside_the_library_are_rejected(root, collection):
    with pytest.raises(ValueError):
        lf.move_image(root, "Winter/a.jpg", collection)
    with pytest.raises(ValueError):
        lf.add_image(root, collection, "new.jpg", _jpeg())
    assert (root / "Winter" / "a.jpg").exists()


def test_source_outside_the_library_is_rejected(root):
    with pytest.raises(ValueError):
        lf.delete_image(root, "../outside.jpg", backup=True)
    assert (root.parent / "outside.jpg").exists()


# --- add, rename, move, copy ---

def test_add_never_overwrites(root):
    original = (root / "Winter" / "a.jpg").read_bytes()

    rel = lf.add_image(root, "Winter", "a.jpg", _jpeg("black"))

    assert rel == "Winter/a (2).jpg"
    assert (root / "Winter" / "a.jpg").read_bytes() == original


def test_add_to_main_folder(root):
    assert lf.add_image(root, "", "loose.png", _jpeg()) == "loose.png"


@pytest.mark.parametrize("filename,data", [("notes.txt", b"hi"), ("fake.jpg", b"not an image")])
def test_add_rejects_things_that_are_not_images(root, filename, data):
    with pytest.raises(ValueError):
        lf.add_image(root, "Winter", filename, data)
    assert sorted(p.name for p in (root / "Winter").iterdir()) == ["a.jpg", "b.jpg"]


def test_rename_keeps_the_extension_and_never_overwrites(root):
    blue = (root / "Winter" / "b.jpg").read_bytes()

    rel = lf.rename_image(root, "Winter/a.jpg", "b")

    assert rel == "Winter/b (2).jpg"
    assert (root / "Winter" / "b.jpg").read_bytes() == blue
    assert not (root / "Winter" / "a.jpg").exists()


def test_rename_accepts_a_typed_extension_and_case_change(root):
    assert lf.rename_image(root, "Winter/a.jpg", "A.jpg") == "Winter/A.jpg"
    assert sorted(p.name for p in (root / "Winter").iterdir()) == ["A.jpg", "b.jpg"]


def test_move_and_copy(root):
    (root / "Spring" / "a.jpg").write_bytes(_jpeg("white"))

    copied = lf.copy_image(root, "Winter/b.jpg", "")
    moved = lf.move_image(root, "Winter/a.jpg", "Spring")

    assert copied == "b.jpg" and (root / "Winter" / "b.jpg").exists()
    assert moved == "Spring/a (2).jpg" and not (root / "Winter" / "a.jpg").exists()


def test_moving_to_the_same_place_changes_nothing(root):
    assert lf.move_image(root, "Winter/a.jpg", "Winter") == "Winter/a.jpg"


# --- delete, backups, restore ---

def test_delete_backs_up_first(root):
    data = (root / "Winter" / "a.jpg").read_bytes()

    lf.delete_image(root, "Winter/a.jpg", backup=True)

    assert not (root / "Winter" / "a.jpg").exists()
    [entry] = lf.list_backups(root)
    assert entry["name"] == "a.jpg" and entry["collection"] == "Winter"
    assert (root / ".backups" / entry["id"]).read_bytes() == data


def test_delete_without_backup(root):
    lf.delete_image(root, "Winter/a.jpg", backup=False)

    assert lf.list_backups(root) == []


def test_nothing_is_deleted_if_the_backup_fails(root):
    (root / ".backups").write_bytes(b"in the way")

    with pytest.raises(OSError):
        lf.delete_image(root, "Winter/a.jpg", backup=True)

    assert (root / "Winter" / "a.jpg").exists()


def test_backups_never_overwrite_each_other(root):
    for color in ("red", "black"):
        (root / "Winter" / "a.jpg").write_bytes(_jpeg(color))
        lf.delete_image(root, "Winter/a.jpg", backup=True)

    assert len(_backups(root)) == 2


def test_restore_recreates_the_collection_and_never_overwrites(root):
    data = (root / "Winter" / "a.jpg").read_bytes()
    lf.delete_image(root, "Winter/a.jpg", backup=True)
    [entry] = lf.list_backups(root)
    (root / "Winter" / "a.jpg").write_bytes(_jpeg("black"))

    assert lf.restore_backup(root, entry["id"]) == "Winter/a (2).jpg"
    assert (root / "Winter" / "a (2).jpg").read_bytes() == data

    lf.delete_collection(root, "Winter", backup=False)
    assert lf.restore_backup(root, entry["id"]) == "Winter/a.jpg"


@pytest.mark.parametrize("backup_id", ["../Winter/a.jpg", "../../outside.jpg", "/etc/passwd", "Winter/none.jpg"])
def test_restore_rejects_other_paths(root, backup_id):
    (root / ".backups" / "Winter").mkdir(parents=True)

    with pytest.raises(ValueError):
        lf.restore_backup(root, backup_id)


# --- crop ---

def test_crop_cuts_a_16_9_window_and_keeps_the_original(root):
    original = (root / "Winter" / "a.jpg").read_bytes()

    lf.crop_image(root, "Winter/a.jpg", left=0.25, top=0.1, width=0.5, backup=True)

    assert Image.open(root / "Winter" / "a.jpg").size == (800, 450)
    [entry] = lf.list_backups(root)
    assert (root / ".backups" / entry["id"]).read_bytes() == original


def test_crop_follows_camera_rotation(root):
    exif = Image.Exif()
    exif[274] = 6  # stored sideways: displays as 1200x1600
    (root / "Winter" / "a.jpg").write_bytes(_jpeg(exif=exif))

    lf.crop_image(root, "Winter/a.jpg", left=0, top=0, width=1, backup=False)

    out = Image.open(root / "Winter" / "a.jpg")
    assert out.size == (1200, 675) and out.getexif().get(274, 1) == 1


@pytest.mark.parametrize("box", [
    dict(left=-0.1, top=0, width=0.5), dict(left=0.6, top=0, width=0.5),
    dict(left=0, top=0.9, width=1), dict(left=0, top=0, width=0), dict(left=0, top=0, width=2),
])
def test_crop_rejects_boxes_outside_the_picture(root, box):
    original = (root / "Winter" / "a.jpg").read_bytes()

    with pytest.raises(ValueError):
        lf.crop_image(root, "Winter/a.jpg", backup=True, **box)

    assert (root / "Winter" / "a.jpg").read_bytes() == original


def test_crop_is_abandoned_if_the_backup_fails(root):
    (root / ".backups").write_bytes(b"in the way")
    original = (root / "Winter" / "a.jpg").read_bytes()

    with pytest.raises(OSError):
        lf.crop_image(root, "Winter/a.jpg", left=0, top=0, width=1, backup=True)

    assert (root / "Winter" / "a.jpg").read_bytes() == original


# --- collections ---

def test_create_and_rename_collections(root):
    lf.create_collection(root, "Summer")
    lf.rename_collection(root, "Summer", "Beach")

    assert (root / "Beach").is_dir() and not (root / "Summer").exists()


@pytest.mark.parametrize("action", [
    lambda r: lf.create_collection(r, "Winter"),
    lambda r: lf.rename_collection(r, "Spring", "Winter"),
    lambda r: lf.rename_collection(r, "", "Other"),
    lambda r: lf.delete_collection(r, "", backup=True),
])
def test_collections_are_never_merged_or_the_main_folder_changed(root, action):
    with pytest.raises(ValueError):
        action(root)
    assert (root / "Winter" / "a.jpg").exists()


def test_delete_collection_backs_up_images_and_removes_system_clutter(root):
    (root / "Winter" / ".DS_Store").write_bytes(b"x")
    (root / "Winter" / "@eaDir").mkdir()

    assert lf.delete_collection(root, "Winter", backup=True) == {"deleted": 2, "folder_removed": True}

    assert not (root / "Winter").exists() and len(_backups(root)) == 2


def test_delete_collection_leaves_other_files_alone(root):
    (root / "Winter" / "notes.txt").write_text("mine")

    result = lf.delete_collection(root, "Winter", backup=True)

    assert result == {"deleted": 2, "folder_removed": False}
    assert (root / "Winter" / "notes.txt").read_text() == "mine"


def test_restored_name_keeps_its_own_brackets(root):
    (root / "Winter" / "a (2).jpg").write_bytes(_jpeg("black"))
    for _ in range(2):  # the second backup gets a collision suffix
        lf.delete_image(root, "Winter/a (2).jpg", backup=True)
        (root / "Winter" / "a (2).jpg").write_bytes(_jpeg("black"))

    assert {e["name"] for e in lf.list_backups(root)} == {"a (2).jpg"}
