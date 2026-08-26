"""Render docx templates through docassemble's real Jinja pipeline."""

from __future__ import annotations

import os
import re
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from docassemble_simulator.execution import RenderSource


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
    ) -> RenderError:
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
            templates_dir = (package / "data" / "templates").resolve()
            template = (templates_dir / requested).resolve()
            try:
                template.relative_to(templates_dir)
            except ValueError:
                continue
            if template.is_file():
                candidates.append(template)

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
        from docassemble.base.helpers import fix_quotes
        from docassemble.base.jinja import custom_jinja_env
        from docxtpl import DocxTemplate

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
        setattr(docx_template, "_dasimulator_paragraphs", paragraphs)
        setattr(docx_template, "_dasimulator_prepared_xml", xml)
        return docx_template
    except RenderError:
        raise
    except Exception as err:
        raise RenderError.from_exception(err) from err


def render_template(docx_template, context: dict) -> Any:
    """Evaluate a template in docassemble's real document-render context.

    ``include_docx_template`` relies on the same thread context used by the
    server's ``assemble_docx`` path.  Includes can add subdocuments whose Jinja
    needs a second render pass, so return the final ``DocxTemplate`` object.
    """
    reset_context = None
    try:
        from docassemble.base.functions import (
            reset_context as reset_docx_context,
        )
        from docassemble.base.functions import (
            set_context,
            this_thread,
        )
        from docassemble.base.jinja import custom_jinja_env

        reset_context = reset_docx_context
        misc = this_thread.misc
        misc.pop("docx_subdocs", None)
        current = docx_template
        paragraphs = paragraph_count(docx_template)

        for pass_number in range(11):
            set_context("docx", template=current)
            old_count = misc.get("docx_include_count", 0)
            current.render(context, jinja_env=custom_jinja_env())
            new_count = misc.get("docx_include_count", 0)
            if new_count <= old_count:
                break
            if pass_number == 10:
                raise RenderError(
                    "docx template includes exceeded the render pass limit"
                )

            from docxtpl import DocxTemplate

            fd, temporary_name = tempfile.mkstemp(suffix=".docx")
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                current.save(str(temporary))
                current = DocxTemplate(str(temporary))
                current.render_init()
                setattr(current, "_dasimulator_paragraphs", paragraphs)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            raise RenderError("docx template did not complete rendering")

        subdocs = misc.get("docx_subdocs", [])
        if subdocs:
            from docassemble.base import file_docx

            fix_subdoc = getattr(file_docx, "fix_subdoc")
            for subdoc in subdocs:
                fix_subdoc(current.docx, subdoc)
        return current
    except RenderError:
        raise
    except Exception as err:
        raise RenderError.from_exception(err) from err
    finally:
        if reset_context is not None:
            reset_context()


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
    return "undefined" in message and (f"'{var}'" in message or f'"{var}"' in message)


def write_artifact(
    docx_template, output: str | Path, filename: str | None = None
) -> Path:
    """Save a rendered docx atomically to an exact target path."""
    requested = Path(output).resolve()
    target = requested / Path(filename).name if filename is not None else requested
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=target.suffix or ".docx", dir=target.parent
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


@dataclass(frozen=True)
class RenderRequest:
    template: str
    source: RenderSource
    assemble: bool
    save_snapshot: Path | None = None
    output: Path | None = None
    expect_missing: str | None = None


class InterviewRenderer:
    """Own render validation, evaluation, expectations, and artifact effects."""

    def __init__(self, root: str | Path, execution):
        self.root = Path(root).resolve()
        self.execution = execution

    def render(self, request: RenderRequest):
        from docassemble_simulator.execution import PrepareRender

        if request.source.kind == "fixture":
            if request.assemble:
                return _render_failure("input", "fixture rendering cannot assemble")
            if request.source.path is None or not request.source.path.is_file():
                return _render_failure(
                    "input", f"fixture script not found: {request.source.path}"
                )
        try:
            template_path = find_template(self.root, request.template)
        except TemplateNotFoundError as error:
            return _render_failure("input", str(error))

        def action(namespace):
            prepared = None
            try:
                prepared = prepare_docx_template(template_path)
                rendered = render_template(prepared, namespace)
                if request.expect_missing:
                    raise RenderExpectationError(
                        f"expected missing variable {request.expect_missing!r}, but render succeeded"
                    )
                result = {
                    "template": request.template,
                    "paragraphs": paragraph_count(rendered),
                }
                if request.output:
                    result["artifact"] = str(write_artifact(rendered, request.output))
                return {"ok": True, "result": result}
            except RenderError as error:
                if request.expect_missing and missing_error_matches(
                    error, request.expect_missing
                ):
                    return {
                        "ok": True,
                        "result": {
                            "template": request.template,
                            "paragraphs": paragraph_count(prepared),
                        },
                    }
                return {
                    "ok": False,
                    "error": {
                        "kind": "render",
                        "message": str(error),
                        "details": {
                            "error_type": error.error_type,
                            "paragraph": error.paragraph,
                        },
                    },
                }
            except RenderExpectationError as error:
                return {
                    "ok": False,
                    "error": {"kind": "render", "message": str(error), "details": {}},
                }
            except OSError as error:
                return {
                    "ok": False,
                    "error": {"kind": "render", "message": str(error), "details": {}},
                }

        prepared = self.execution.run(
            PrepareRender(
                request.source, request.assemble, request.save_snapshot, action
            )
        )
        if not prepared.ok:
            return {
                "ok": False,
                "error": {
                    "kind": prepared.error.kind,
                    "message": prepared.error.message,
                    "details": prepared.error.details or {},
                },
            }
        return prepared.result


def _render_failure(kind: str, message: str):
    return {"ok": False, "error": {"kind": kind, "message": message, "details": {}}}


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
    "InterviewRenderer",
    "RenderError",
    "RenderExpectationError",
    "RenderRequest",
    "TemplateNotFoundError",
    "assert_missing",
    "find_template",
    "missing_error_matches",
    "paragraph_count",
    "prepare_docx_template",
    "render_template",
    "write_artifact",
]
