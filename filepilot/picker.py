"""Native macOS folder picker.

Uses AppleScript's `choose folder` through `osascript`. The command is a fixed
string run without a shell, so no request data ever reaches the script.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from .config import settings

_SCRIPT = (
    'set chosenFolder to choose folder with prompt "Choose a folder for FilePilot to scan"\n'
    "return POSIX path of chosenFolder"
)


class PickerUnavailable(Exception):
    pass


class PickerCancelled(Exception):
    pass


def picker_available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def choose_folder(timeout: int = settings.picker_timeout_seconds) -> str:
    """Show the native folder dialog and return the chosen POSIX path."""
    if not picker_available():
        raise PickerUnavailable("The native folder picker is only available on macOS.")
    try:
        proc = subprocess.run(
            ["osascript", "-e", _SCRIPT],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise PickerCancelled("The folder picker timed out.") from None
    except OSError as exc:
        raise PickerUnavailable(f"Could not open the folder picker: {exc}") from None

    if proc.returncode != 0:
        # Error -128 is "User canceled."
        if "-128" in proc.stderr:
            raise PickerCancelled("No folder was chosen.")
        raise PickerUnavailable("The folder picker could not be opened.")

    path = proc.stdout.strip()
    # POSIX paths of folders end in "/"; keep "/" itself intact for validation.
    if len(path) > 1:
        path = path.rstrip("/")
    return path
