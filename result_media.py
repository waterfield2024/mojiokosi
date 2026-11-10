"""Resolve paths from portable result metadata without following filesystem links."""
from pathlib import Path, PureWindowsPath
import stat

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".wmv", ".mpg", ".mpeg", ".ts"}


def result_file(folder, value, extensions):
    """Check names lexically before any I/O. Inspect each component without following it."""
    if not isinstance(value, (str, Path)) or not value:
        return None
    windows = PureWindowsPath(value)
    if windows.drive.startswith(chr(92)) or (windows.anchor and not Path(value).is_absolute()):
        return None
    root = Path(folder).absolute()
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return None
    if not path.parts or path.suffix.lower() not in extensions:
        return None
    for part in path.parts:
        if (part == ".." or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
                or any(c in '<>:"|?*' or ord(c) < 32 for c in part)):
            return None
    candidate = root
    try:
        for part in ("", *path.parts):
            candidate = candidate / part
            entry = candidate.lstat()
            if stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return None
        return candidate if stat.S_ISREG(entry.st_mode) else None
    except (OSError, ValueError):
        return None
