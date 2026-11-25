"""Open the operating system's folder dialog on the computer running Docent.

A web page is never told the full path of a folder chosen in the browser, and
Docent's server needs the path, so the server opens the dialog itself.
"""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import string
import subprocess
import sys
from pathlib import Path

TITLE = "Choose your Docent library folder"
TIMEOUT = 600  # seconds a dialog may stay open

# Paths are handed over as arguments or environment variables, never spliced
# into a script.
_APPLESCRIPT = f"""on run argv
    activate
    if (count of argv) > 0 then
        set chosen to choose folder with prompt "{TITLE}" default location (POSIX file (item 1 of argv))
    else
        set chosen to choose folder with prompt "{TITLE}"
    end if
    return POSIX path of chosen
end run"""

_POWERSHELL = f"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '{TITLE}'
if ($env:DOCENT_PICK_START) {{ $dialog.SelectedPath = $env:DOCENT_PICK_START }}
$owner = New-Object System.Windows.Forms.Form -Property @{{ TopMost = $true }}
$result = $dialog.ShowDialog($owner)
$owner.Dispose()
if ($result -ne [System.Windows.Forms.DialogResult]::OK) {{ exit 2 }}
[Console]::Out.Write($dialog.SelectedPath)
"""


class DialogError(RuntimeError):
    """The dialog could not be shown."""


def backend() -> str | None:
    """Which dialog this computer can show, if any."""
    if sys.platform == "darwin":
        return "macos" if shutil.which("osascript") else None
    if sys.platform == "win32":
        return "windows" if shutil.which("powershell") or shutil.which("pwsh") else None
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return None
        return next((name for name in ("zenity", "kdialog") if shutil.which(name)), None)
    return None


def available() -> bool:
    return backend() is not None


def command(start: str | None) -> tuple[list[str], dict[str, str]]:
    """The command line and environment that show the dialog at *start*."""
    kind = backend()
    env = {k: v for k, v in os.environ.items() if k != "DOCENT_PICK_START"}
    if kind == "macos":
        return [shutil.which("osascript"), "-e", _APPLESCRIPT, *([start] if start else [])], env
    if kind == "windows":
        if start:
            env["DOCENT_PICK_START"] = start
        shell = shutil.which("powershell") or shutil.which("pwsh")
        encoded = base64.b64encode(_POWERSHELL.encode("utf-16-le")).decode()
        return [shell, "-NoProfile", "-STA", "-EncodedCommand", encoded], env
    if kind == "zenity":
        argv = [shutil.which("zenity"), "--file-selection", "--directory", f"--title={TITLE}"]
        if start:
            argv.append(f"--filename={start}/")  # the slash opens inside the folder
        return argv, env
    if kind == "kdialog":
        return [shutil.which("kdialog"), "--title", TITLE,
                "--getexistingdirectory", start or str(Path.home())], env
    raise DialogError("This computer has no folder dialog Docent can use")


def interpret(kind: str, code: int, out: str, err: str) -> str | None:
    """The chosen folder, ``None`` if cancelled, or ``DialogError``."""
    if code == 0:
        return out.rstrip("\r\n")
    cancelled = (
        (kind == "macos" and "-128" in err)
        or (kind == "windows" and code == 2)
        # zenity and kdialog use 1 for Cancel, and also when there's no display.
        or (kind in ("zenity", "kdialog") and code == 1 and "display" not in err.lower())
    )
    if cancelled:
        return None
    raise DialogError(err.strip() or f"The folder dialog exited with code {code}")


async def pick(start: str | None) -> str | None:
    """Show the dialog and wait for the answer."""
    kind = backend()
    argv, env = command(start)
    try:
        done = await asyncio.to_thread(
            subprocess.run, argv, env=env, capture_output=True, timeout=TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as e:
        raise DialogError(str(e)) from e
    return interpret(
        kind, done.returncode,
        done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"),
    )


def browse_roots() -> list[Path]:
    """Places the in-page folder list may look: home, then mounted drives."""
    if sys.platform == "win32":
        places = [Path(f"{letter}:\\") for letter in string.ascii_uppercase]
    elif sys.platform == "darwin":
        places = [Path("/Volumes")]
    else:
        places = [Path("/media"), Path("/mnt"), Path("/run/media")]
    return [Path.home(), *(p for p in places if p.is_dir())]
