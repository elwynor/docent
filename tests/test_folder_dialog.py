"""The operating system's folder dialog, on each platform Docent may run on.

Only the macOS dialog has been tried on a real machine; these tests check the
commands and how their results are read.
"""
from __future__ import annotations

import base64
import subprocess

import pytest

import folder_dialog as fd


@pytest.fixture
def system(monkeypatch):
    """Pretend to be a given platform with certain programs installed."""

    def _set(platform, programs=(), env=None):
        monkeypatch.setattr(fd.sys, "platform", platform)
        monkeypatch.setattr(fd.shutil, "which", lambda n: f"/bin/{n}" if n in programs else None)
        for name in ("DISPLAY", "WAYLAND_DISPLAY"):
            monkeypatch.delenv(name, raising=False)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)

    return _set


@pytest.mark.parametrize("platform,programs,env,expected", [
    ("darwin", ["osascript"], {}, "macos"),
    ("win32", ["powershell"], {}, "windows"),
    ("win32", ["pwsh"], {}, "windows"),
    ("linux", ["zenity", "kdialog"], {"DISPLAY": ":0"}, "zenity"),
    ("linux", ["kdialog"], {"WAYLAND_DISPLAY": "wayland-0"}, "kdialog"),
    ("linux", ["zenity"], {}, None),  # a server or container with no desktop
    ("linux", [], {"DISPLAY": ":0"}, None),
    ("win32", [], {}, None),
    ("freebsd14", ["zenity"], {"DISPLAY": ":0"}, None),
])
def test_backend_matches_the_system(system, platform, programs, env, expected):
    system(platform, programs, env)

    assert fd.backend() == expected
    assert fd.available() is (expected is not None)


ODD = "/tmp/it's \"odd\"; $(rm -rf x)"


def test_macos_passes_the_start_folder_as_an_argument(system):
    system("darwin", ["osascript"])

    argv, env = fd.command(ODD)

    assert argv[-1] == ODD and ODD not in argv[2]


def test_windows_never_puts_the_start_folder_in_the_script(system):
    system("win32", ["powershell"])

    argv, env = fd.command(ODD)
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")

    assert "-STA" in argv and "FolderBrowserDialog" in script
    assert ODD not in script and env["DOCENT_PICK_START"] == ODD


@pytest.mark.parametrize("program", ["zenity", "kdialog"])
def test_linux_passes_the_start_folder_as_one_argument(system, program):
    system("linux", [program], {"DISPLAY": ":0"})

    argv, _ = fd.command(ODD)

    assert argv[0] == f"/bin/{program}"
    assert sum(ODD in part for part in argv) == 1


@pytest.mark.parametrize("platform,programs", [
    ("darwin", ["osascript"]), ("win32", ["powershell"]), ("linux", ["zenity"]), ("linux", ["kdialog"]),
])
def test_works_without_a_start_folder(system, platform, programs):
    system(platform, programs, {"DISPLAY": ":0"})

    argv, env = fd.command(None)

    assert all(part for part in argv) and "DOCENT_PICK_START" not in env


@pytest.mark.parametrize("backend,code,out,err,expected", [
    ("macos", 0, "/Users/a/Pictures/\n", "", "/Users/a/Pictures/"),
    ("macos", 1, "", "execution error: User canceled. (-128)", None),
    ("windows", 0, "D:\\Photos", "", "D:\\Photos"),
    ("windows", 2, "", "", None),
    ("zenity", 0, "/home/a/photos\n", "", "/home/a/photos"),
    ("zenity", 1, "", "", None),
    ("kdialog", 1, "", "", None),
])
def test_reads_a_choice_or_a_cancel(backend, code, out, err, expected):
    assert fd.interpret(backend, code, out, err) == expected


@pytest.mark.parametrize("backend,code,err", [
    ("macos", 1, "execution error: no window server (-1712)"),
    ("windows", 1, "Add-Type : Cannot add type."),
    ("zenity", 1, "Gtk-WARNING: cannot open display: :0"),
    ("kdialog", 1, "qt.qpa.xcb: could not connect to display :0"),
    ("zenity", 255, "unexpected"),
])
def test_failures_are_not_mistaken_for_a_cancel(backend, code, err):
    with pytest.raises(fd.DialogError):
        fd.interpret(backend, code, "", err)


async def test_pick_runs_the_command(system, monkeypatch):
    system("linux", ["zenity"], {"DISPLAY": ":0"})
    monkeypatch.setattr(fd.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"/data/art\n", b""))

    assert await fd.pick("/data") == "/data/art"


async def test_a_dialog_left_open_counts_as_cancelled(system, monkeypatch):
    system("linux", ["zenity"], {"DISPLAY": ":0"})

    def _slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(fd.subprocess, "run", _slow)

    assert await fd.pick(None) is None


@pytest.mark.parametrize("platform,extra", [
    ("darwin", ["/Volumes"]), ("linux", ["/media", "/mnt", "/run/media"]),
])
def test_folder_list_reaches_mounted_drives(monkeypatch, platform, extra):
    monkeypatch.setattr(fd.sys, "platform", platform)
    monkeypatch.setattr(fd.Path, "is_dir", lambda self: True)

    assert [str(p) for p in fd.browse_roots()][1:] == extra


def test_folder_list_reaches_windows_drives(monkeypatch):
    monkeypatch.setattr(fd.sys, "platform", "win32")
    monkeypatch.setattr(fd.Path, "is_dir", lambda self: str(self) in ("C:\\", "Z:\\"))

    assert [str(p) for p in fd.browse_roots()][1:] == ["C:\\", "Z:\\"]
