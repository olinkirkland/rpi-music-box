"""The file manager: browse, upload, rename and delete in the music folder.

Everything stays inside the music directory: every path from the web page is
resolved and checked against it, hidden files and folders are left alone,
and symlinks pointing out of it are refused. Cards are kept in step: a
renamed file or folder is renamed in every card that plays it, and a deleted
file is taken out of cards' track lists.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from .cards import AUDIO_EXTENSIONS

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
UPLOAD_EXTENSIONS = AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
MAX_UPLOAD = 1024 * 1024 * 1024  # 1 GB per file
KEEP_FREE = 500 * 1024 * 1024  # never fill the SD card past this
MAX_NAME = 200
_BAD_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]')


class FileError(Exception):
    """Something the page can show, with an HTTP status."""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


def clean_filename(name: str) -> str:
    """A safe single file or folder name, or FileError."""
    if not isinstance(name, str):
        raise FileError("the name must be text")
    name = " ".join(name.split())
    if not name or name in (".", ".."):
        raise FileError("the name can't be empty")
    if name.startswith("."):
        raise FileError("names can't start with a dot")
    if _BAD_CHARS.search(name):
        raise FileError('names can\'t contain / \\ : * ? " < > |')
    if len(name) > MAX_NAME:
        raise FileError(f"the name is longer than {MAX_NAME} characters")
    return name


class FileManager:
    def __init__(self, music_dir: Path, cards, keep_free: int = KEEP_FREE) -> None:
        self.root = music_dir
        self.cards = cards
        self.keep_free = keep_free

    # --- paths -----------------------------------------------------------------

    def resolve(self, rel: str, must_exist: bool = True) -> Path:
        """`rel` (relative to the music folder, "" = the folder itself) as an
        absolute path that is guaranteed to be inside the music folder."""
        rel = (rel or "").strip().strip("/")
        if "\x00" in rel:
            raise FileError("bad path")
        parts = [p for p in rel.split("/") if p]
        if any(p in (".", "..") or p.startswith(".") for p in parts):
            raise FileError("bad path")
        root = self.root.resolve()
        path = (root.joinpath(*parts)).resolve()
        if path != root and not path.is_relative_to(root):
            raise FileError("that path is outside the music folder", 403)
        if must_exist and not path.exists():
            raise FileError("no such file or folder", 404)
        return path

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    # --- reading -----------------------------------------------------------------

    def list(self, rel: str = "") -> dict:
        folder = self.resolve(rel)
        if not folder.is_dir():
            raise FileError("that's not a folder")
        folders, files = [], []
        for entry in sorted(folder.iterdir(), key=lambda p: self.cards._natural(p.name)):
            if entry.name.startswith("."):
                continue
            try:
                inside = entry.resolve().is_relative_to(self.root.resolve())
            except OSError:
                continue
            if not inside:
                continue  # a symlink pointing out of the music folder
            entry_rel = self.rel(entry)
            if entry.is_dir():
                try:
                    count = sum(1 for c in entry.iterdir() if not c.name.startswith("."))
                except OSError:
                    count = 0
                folders.append({"name": entry.name, "path": entry_rel, "items": count,
                                "used_by": self.cards.users_of(entry_rel)})
            elif entry.is_file():
                st = entry.stat()
                suffix = entry.suffix.lower()
                files.append({"name": entry.name, "path": entry_rel, "size": st.st_size,
                              "modified": st.st_mtime,
                              "kind": "audio" if suffix in AUDIO_EXTENSIONS
                              else "image" if suffix in IMAGE_EXTENSIONS else "other",
                              "used_by": self.cards.users_of(entry_rel)})
        here = self.rel(folder) if folder != self.root.resolve() else ""
        crumbs = [{"name": "Music", "path": ""}]
        acc = []
        for part in [p for p in here.split("/") if p]:
            acc.append(part)
            crumbs.append({"name": part, "path": "/".join(acc)})
        usage = shutil.disk_usage(self.root)
        return {"path": here, "absolute": str(folder), "crumbs": crumbs,
                "folders": folders, "files": files,
                "disk": {"total": usage.total, "free": usage.free}}

    # --- changing ------------------------------------------------------------------

    def make_folder(self, parent: str, name: str) -> str:
        folder = self.resolve(parent)
        if not folder.is_dir():
            raise FileError("that's not a folder")
        target = folder / clean_filename(name)
        if target.exists():
            raise FileError("something with that name is already there", 409)
        target.mkdir()
        return self.rel(target)

    def upload(self, folder_rel: str, name: str, stream, length: int, overwrite: bool = False) -> dict:
        """Write `length` bytes from `stream` as a new file in the folder."""
        folder = self.resolve(folder_rel)
        if not folder.is_dir():
            raise FileError("that's not a folder")
        name = clean_filename(name)  # a name with a folder in it is refused, not trimmed
        if Path(name).suffix.lower() not in UPLOAD_EXTENSIONS:
            raise FileError("only audio files (" + ", ".join(sorted(AUDIO_EXTENSIONS)) +
                            ") and cover images can be uploaded", 415)
        if length <= 0:
            raise FileError("the file is empty")
        if length > MAX_UPLOAD:
            raise FileError("the file is larger than 1 GB", 413)
        if shutil.disk_usage(folder).free - length < self.keep_free:
            raise FileError("not enough free space on the SD card", 507)
        target = folder / name
        existed = target.exists()
        if existed and (not overwrite or target.is_dir()):
            raise FileError(f"{name} is already there", 409)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".upload-", suffix=".part")
        try:
            remaining = length
            with os.fdopen(fd, "wb") as out:
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise FileError("the upload stopped before the end")
                    out.write(chunk)
                    remaining -= len(chunk)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)  # appears complete, or not at all
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return {"path": self.rel(target), "replaced": existed}

    def rename(self, rel: str, new_name: str) -> dict:
        """Rename within the same folder; cards follow.

        A file always keeps its extension: `new_name` may include it or not
        ("Song" and "Song.mp3" both give "Song.mp3"); anything else typed
        after a dot is part of the name ("Song.wav" gives "Song.wav.mp3").
        """
        path = self.resolve(rel)
        if path == self.root.resolve():
            raise FileError("the music folder itself can't be renamed")
        new_name = clean_filename(new_name)
        if path.is_file() and path.suffix:
            base = new_name[: -len(path.suffix)] if new_name.lower().endswith(path.suffix.lower()) else new_name
            new_name = clean_filename(base) + path.suffix
        target = path.parent / new_name
        if target.exists() and target.resolve() != path.resolve():
            raise FileError("something with that name is already there", 409)
        old_rel = self.rel(path)
        path.rename(target)
        new_rel = self.rel(target)
        return {"path": new_rel, "cards": self.cards.moved(old_rel, new_rel)}

    def delete(self, rel: str) -> dict:
        path = self.resolve(rel)
        if path == self.root.resolve():
            raise FileError("the music folder itself can't be deleted")
        old_rel = self.rel(path)
        if path.is_dir():
            if any(not c.name.startswith(".") for c in path.iterdir()):
                raise FileError("only empty folders can be deleted; delete or move what's inside first", 409)
            shutil.rmtree(path)  # only hidden leftovers remain at this point
            return {"path": old_rel, "cards": []}
        path.unlink()
        return {"path": old_rel, "cards": self.cards.deleted(old_rel)}
