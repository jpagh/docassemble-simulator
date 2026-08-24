"""Render docx templates through docassemble's real Jinja pipeline."""
from __future__ import annotations

import os
import re
import tempfile
import traceback
from pathlib import Path


class TemplateNotFoundError(Exception):
    """The requested template cannot be resolved under the package root."""


class RenderExpectationError(Exception):
    """An ``expect-missing`` assertion did not hold."""


class RenderError(Exception):
    """A template preparation or evaluation error.

    ``paragraph`` is the XML line reported by Jinja after one newline has been
    inserted before each Word paragraph.  This is deliberately a line number,
    rather than an approximate Word paragraph index: it is the location users
    can use to find the failing paragraph in the converted template XML.
    """

    def __init__(
        self,
        message: str,
        *,
        paragraph: int | None = None,
        error_type: str = "RenderError",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.paragraph = paragraph
        self.error_type = error_type
        self.cause = cause

    @classmethod
    def from_exception(
        cls, error: BaseException, *, paragraph: int | None = None
    ) -> "RenderError":
        if paragraph is None:
            paragraph = _exception_line(error)
        message = str(error).strip() or type(error).__name__
        return cls(
            message,
            paragraph=paragraph,
            error_type=type(error).__name__,
            cause=error,
        )


def find_template(root: str | Path, filename: str) -> Path:
    """Find a unique ``data/templates`` file below a docassemble package.

    A bare filename is preferred by the command surface.  Nested relative
    paths are supported too, while absolute paths and ``..`` traversal are
    rejected so the command cannot accidentally render outside the package.
    """
    requested = Path(filename)
    if requested.is_absolute() or ".." in requested.parts:
        raise TemplateNotFoundError(
            f"template must be a relative filename under data/templates: {filename}"
        )

    root_path = Path(root).resolve()
    da_dir = root_path / "docassemble"
    candidates: list[Path] = []
    if da_dir.is_dir():
        for package in sorted(da_dir.iterdir()):
            template = package / "data" / "templates" / requested
            if template.is_file():
                candidates.append(template.resolve())

    if not candidates:
        raise TemplateNotFoundError(
            f"template '{filename}' was not found under {da_dir}/<package>/data/templates"
        )
    if len(candidates) > 1:
        listed = "\n  ".join(str(path) for path in candidates)
        raise TemplateNotFoundError(f"template '{filename}' is ambiguous:\n  {listed}")
    return candidates[0]


def prepare_docx_template(path: str | Path):
    """Load and syntax-check a docx using the harness-compatible pipeline."""
    try:
        from docxtpl import DocxTemplate
        from docassemble.base.helpers import fix_quotes
        from docassemble.base.jinja import custom_jinja_env

        docx_template = DocxTemplate(path)
        docx_template.render_init()
        xml = docx_template.get_xml()
        # Keep these transformations byte-for-byte compatible with the local
        # server harness.  The newlines are what make Jinja report a useful
        # paragraph location instead of line 1 of one giant XML string.
        xml = re.sub(r"<w:p([ >])", r"\n<w:p\1", xml)
        xml = re.sub(r"({[%{].*?[%}]})", fix_quotes, xml)
        paragraphs = len(re.findall(r"\n<w:p(?:[ >])", xml))
        xml = docx_template.patch_xml(xml)
        custom_jinja_env().parse(xml)
        docx_template._dasimulator_paragraphs = paragraphs
        docx_template._dasimulator_prepared_xml = xml
        return docx_template
    except RenderError:
        raise
    except Exception as err:
        raise RenderError.from_exception(err) from err


def render_template(docx_template, context: dict) -> None:
    """Evaluate a prepared template with docassemble's strict Jinja environment."""
    try:
        from docassemble.base.jinja import custom_jinja_env

        docx_template.render(context, jinja_env=custom_jinja_env())
    except RenderError:
        raise
    except Exception as err:
        raise RenderError.from_exception(err) from err


def assert_missing(context: dict, var: str) -> None:
    """Assert that a Python-style dotted variable cannot be resolved."""
    try:
        eval(var, context)
    except (NameError, AttributeError, KeyError, IndexError, TypeError):
        return
    raise RenderExpectationError(f"expected missing variable {var!r}, but it resolved")


def missing_error_matches(error: RenderError, var: str) -> bool:
    """Whether a render failure is the expected strict-undefined failure."""
    message = str(error)
    return "undefined" in message and (
        f"'{var}'" in message or f'"{var}"' in message or var in message
    )


def write_artifact(docx_template, output_dir: str | Path, filename: str) -> Path:
    """Save a rendered docx atomically in ``output_dir``."""
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / Path(filename).name
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=target.suffix or ".docx", dir=directory
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        docx_template.save(str(temporary))
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def paragraph_count(docx_template) -> int:
    """Return the count recorded during preparation."""
    return int(getattr(docx_template, "_dasimulator_paragraphs", 0))


def _exception_line(error: BaseException) -> int | None:
    line = getattr(error, "lineno", None)
    if isinstance(line, int) and line > 0:
        return line
    frames = traceback.extract_tb(error.__traceback__)
    for frame in reversed(frames):
        if frame.filename in ("<template>", "<unknown>"):
            return frame.lineno
    return None


__all__ = [
    "RenderError",
    "RenderExpectationError",
    "TemplateNotFoundError",
    "assert_missing",
    "find_template",
    "missing_error_matches",
    "paragraph_count",
    "prepare_docx_template",
    "render_template",
    "write_artifact",
]
