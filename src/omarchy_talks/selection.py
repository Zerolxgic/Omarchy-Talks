"""Wayland primary-selection acquisition for Omarchy."""
import shutil
import subprocess

from .config import ReaderError


CAPTURE_TIMEOUT_SECONDS = 1.0


class SelectionError(ReaderError):
    """The current Wayland primary selection could not be read as text."""


def capture_selected_text(
    timeout: float = CAPTURE_TIMEOUT_SECONDS,
    executable: str = "wl-paste",
) -> str:
    """Return the current textual primary selection without modifying it."""
    capture = shutil.which(executable)
    if capture is None:
        raise SelectionError(
            "Selection: wl-paste is missing; install wl-clipboard to read selected text"
        )

    command = [capture, "--primary", "--type", "text", "--no-newline"]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SelectionError(
            f"Selection: wl-paste timed out after {timeout:g} seconds"
        ) from exc
    except OSError as exc:
        raise SelectionError(f"Selection: could not run wl-paste: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if any(
            phrase in detail.casefold()
            for phrase in ("no selection", "nothing is copied")
        ):
            raise SelectionError("Selection: no primary selection is available")
        suffix = f": {detail}" if detail else ""
        raise SelectionError(
            f"Selection: wl-paste exited {result.returncode}{suffix}"
        )

    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SelectionError(
            "Selection: primary selection is not valid UTF-8 text"
        ) from exc
    if not text.strip():
        raise SelectionError("Selection: selected text is empty or whitespace-only")
    return text
