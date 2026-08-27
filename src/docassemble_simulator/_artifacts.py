"""Durable simulator-owned numbered files and published attachment capture."""

from __future__ import annotations

import json
import mimetypes
import shutil
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from docassemble_simulator._diagnostics import Diagnostic
from docassemble_simulator._files import atomic_replace, flock
from docassemble_simulator._outcomes import PublishedAttachment

_PUBLISHED: ContextVar[list[PublishedAttachment] | None] = ContextVar(
    "docassemble_simulator_published_attachments", default=None
)
_ACTIVE_REGISTRY: ContextVar[LocalFileRegistry | None] = ContextVar(
    "docassemble_simulator_file_registry", default=None
)


@contextmanager
def capture_published_attachments():
    attachments: list[PublishedAttachment] = []
    token = _PUBLISHED.set(attachments)
    try:
        yield attachments
    finally:
        _PUBLISHED.reset(token)


class LocalFileRegistry:
    """Persist docassemble numbered files beneath one simulator workspace."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.index_path = self.directory / "index.json"
        self.lock_path = self.directory / "index.lock"

    def save(self, filename: str, source: str | Path) -> tuple[int, str, str]:
        source_path = Path(source)
        suffix = Path(filename).suffix or source_path.suffix
        extension = suffix.lstrip(".").lower()
        mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.directory.mkdir(parents=True, exist_ok=True)
        with flock(self.lock_path):
            index = self._read_index()
            number = int(index.get("next", 1))
            index["next"] = number + 1
            destination = self.directory / f"dasimulator-{number}{suffix}"
            atomic_replace(
                destination,
                lambda temporary: shutil.copyfile(source_path, temporary),
                lock_destination=True,
            )
            files = index.setdefault("files", {})
            files[str(number)] = {
                "path": str(destination),
                "filename": Path(filename).name,
                "extension": extension,
                "mimetype": mimetype,
                "persistent": False,
                "private": True,
            }
            self._write_index(index)
        return number, extension, mimetype

    def find(self, file_number: int, filename: str | None = None) -> dict | None:
        del filename
        with flock(self.lock_path):
            value = self._read_index().get("files", {}).get(str(file_number))
        return dict(value) if isinstance(value, dict) else None

    def url_for(self, file_reference: Any) -> str | None:
        file_reference = self._first_file(file_reference)
        number = getattr(file_reference, "number", None)
        if number is None:
            return None
        metadata = self.find(number)
        if metadata is None:
            return None
        path = Path(metadata["path"]).resolve()
        if not path.is_file():
            return None
        uri = path.as_uri()
        attachment = PublishedAttachment(
            str(metadata["filename"]),
            str(metadata["extension"]),
            str(metadata["mimetype"]),
            path,
            uri,
            tuple(validate_docx_structure(path))
            if str(metadata["extension"]).lower() == "docx"
            else (),
        )
        published = _PUBLISHED.get()
        if published is not None and all(item.path != path for item in published):
            published.append(attachment)
        return uri

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return {"schema": 1, "next": 1, "files": {}}
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise RuntimeError(
                f"simulator file index is unreadable: {self.index_path} ({error})"
            ) from error
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise RuntimeError(
                f"simulator file index uses an unsupported format: {self.index_path}"
            )
        return value

    def _write_index(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
        atomic_replace(
            self.index_path,
            lambda temporary: temporary.write_text(payload, encoding="utf-8"),
            lock_destination=False,
        )

    @staticmethod
    def _first_file(file_reference: Any) -> Any:
        elements = getattr(file_reference, "elements", None)
        if isinstance(elements, list) and elements:
            return elements[0]
        first_file = getattr(file_reference, "_first_file", None)
        if callable(first_file):
            return first_file()
        return file_reference


def validate_docx_structure(path: str | Path) -> list[Diagnostic]:
    """Report strict paragraph nesting without mutating the DOCX package."""
    diagnostics: list[Diagnostic] = []
    paragraph_tag = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
    try:
        with ZipFile(path) as archive:
            parts = [
                name
                for name in archive.namelist()
                if name.startswith("word/") and name.endswith(".xml")
            ]
            for part in parts:
                try:
                    root = ET.fromstring(archive.read(part))
                except ET.ParseError:
                    continue
                count = sum(
                    1
                    for paragraph in root.iter(paragraph_tag)
                    for descendant in paragraph.iter(paragraph_tag)
                    if descendant is not paragraph
                )
                if count:
                    diagnostics.append(
                        Diagnostic(
                            "docx-structure",
                            f"{part} contains {count} nested paragraph element(s)",
                            {
                                "part": part,
                                "problem": "nested-paragraph",
                                "count": count,
                            },
                        )
                    )
    except (BadZipFile, OSError):
        return []
    return diagnostics


def activate_file_registry(directory: str | Path) -> LocalFileRegistry:
    registry = LocalFileRegistry(directory)
    _ACTIVE_REGISTRY.set(registry)
    return registry


def active_file_registry() -> LocalFileRegistry | None:
    return _ACTIVE_REGISTRY.get()


__all__ = [
    "LocalFileRegistry",
    "PublishedAttachment",
    "activate_file_registry",
    "active_file_registry",
    "capture_published_attachments",
    "validate_docx_structure",
]
