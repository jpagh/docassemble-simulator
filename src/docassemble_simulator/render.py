"""Render docx templates through docassemble's real Jinja pipeline."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from docassemble_simulator._artifacts import publish_attachment
from docassemble_simulator._attachments import (
    AttachmentAssemblyError,
    AttachmentInputError,
    AttachmentTarget,
    assemble_attachment,
    enumerate_entries,
    resolve_automatic_target,
    resolve_expect_missing_alias,
    resolve_explicit_target,
)
from docassemble_simulator._files import (
    ProtectedDirectory,
    ProtectedPath,
    atomic_replace,
)
from docassemble_simulator._outcomes import ErrorKind, Failure, Outcome
from docassemble_simulator._runtime import PDFConversionUnavailable, _is_missing_module
from docassemble_simulator.execution import (
    FixtureSource,
    FreshSource,
    RenderSource,
    SavedSessionSource,
    SnapshotSource,
)


class TemplateNotFoundError(Exception):
    """The requested template cannot be resolved under the package root."""


class RenderExpectationError(Exception):
    """An ``expect-missing`` assertion did not hold."""


def _custom_jinja_env():
    try:
        from docassemble.base.jinja import custom_jinja_env
    except ModuleNotFoundError as error:
        if not _is_missing_module(error, "docassemble.base.jinja"):
            raise
        from docassemble.base.parse import custom_jinja_env
    return custom_jinja_env


class RenderBindingError(ValueError):
    """A requested ephemeral template binding could not be evaluated."""

    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name


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
        template: str | None = None,
        render_pass: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.paragraph = paragraph
        self.error_type = error_type
        self.template = template
        self.render_pass = render_pass
        self.cause = cause

    @classmethod
    def from_exception(
        cls,
        error: BaseException,
        *,
        paragraph: int | None = None,
        template: str | None = None,
        render_pass: int | None = None,
    ) -> RenderError:
        if paragraph is None:
            paragraph = _exception_line(error)
        message = str(error).strip() or type(error).__name__
        return cls(
            message,
            paragraph=paragraph,
            error_type=type(error).__name__,
            template=_exception_template(error) or template,
            render_pass=render_pass,
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
        try:
            from docassemble.base.helpers import fix_quotes
        except ModuleNotFoundError as error:
            if not _is_missing_module(error, "docassemble.base.helpers"):
                raise
            from docassemble.base.parse import fix_quotes
        custom_jinja_env = _custom_jinja_env()
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
        docx_template._dasimulator_paragraphs = paragraphs
        docx_template._dasimulator_prepared_xml = xml
        return docx_template
    except RenderError:
        raise
    except Exception as err:
        raise RenderError.from_exception(
            err, template=str(Path(path).resolve())
        ) from err


def render_template(
    docx_template, context: dict, *, template: str | None = None
) -> Any:
    """Evaluate a template in docassemble's real document-render context.

    ``include_docx_template`` relies on the same thread context used by the
    server's ``assemble_docx`` path.  Includes can add subdocuments whose Jinja
    needs a second render pass, so return the final ``DocxTemplate`` object.
    """
    reset_context = None
    render_pass = None
    try:
        from docassemble.base.functions import (
            reset_context as reset_docx_context,
        )
        from docassemble.base.functions import (
            set_context,
            this_thread,
        )

        custom_jinja_env = _custom_jinja_env()

        reset_context = reset_docx_context
        misc = this_thread.misc
        misc.pop("docx_subdocs", None)
        current = docx_template
        paragraphs = paragraph_count(docx_template)

        for pass_number in range(11):
            render_pass = pass_number + 1
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
                current._dasimulator_paragraphs = paragraphs
            finally:
                temporary.unlink(missing_ok=True)
        else:
            raise RenderError("docx template did not complete rendering")

        # Current docassemble performs include integration through repeated
        # DocxTemplate save/reload passes. Older versions exposed a separate
        # fix_subdoc helper; invoking that optional legacy hook here breaks
        # supported versions where it no longer exists.
        return current
    except RenderError:
        raise
    except Exception as err:
        raise RenderError.from_exception(
            err, template=template, render_pass=render_pass
        ) from err
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
    return atomic_replace(
        target,
        lambda temporary: docx_template.save(str(temporary)),
        lock_destination=True,
    )


def copy_artifact(source: str | Path, output: str | Path) -> Path:
    """Copy a rendered docx atomically to an exact target path."""
    source_path = Path(source).resolve()
    target = Path(output).resolve()
    return atomic_replace(
        target,
        lambda temporary: shutil.copyfile(source_path, temporary),
        lock_destination=True,
    )


def paragraph_count(docx_template) -> int:
    """Return the count recorded during preparation."""
    return int(getattr(docx_template, "_dasimulator_paragraphs", 0))


@dataclass(frozen=True)
class RenderRequest:
    template: str | None
    source: RenderSource
    assemble: bool
    save_snapshot: Path | None = None
    output: Path | None = None
    expect_missing: str | None = None
    bindings: tuple[tuple[str, str], ...] = ()
    attachment: str | None = None


RenderFailure = Failure
RenderOutcome = Outcome


@dataclass(frozen=True)
class RenderResult:
    template: str
    paragraphs: int
    artifact: Path | None = None
    attachment: str | None = None


def _apply_bindings(
    namespace: dict[str, Any], bindings: tuple[tuple[str, str], ...]
) -> None:
    """Add explicit, ephemeral template variables to the render namespace."""
    for name, expression in bindings:
        if not isinstance(name, str) or not name.isidentifier():
            raise RenderBindingError(
                str(name),
                f"invalid render binding name {name!r}; use a simple Python name",
            )
        if not isinstance(expression, str) or not expression.strip():
            raise RenderBindingError(
                name, f"invalid render binding {name}: expression is empty"
            )
        try:
            namespace[name] = eval(expression, namespace)
        except Exception as error:  # report the user-provided binding precisely
            raise RenderBindingError(
                name,
                f"render binding {name}={expression!r} failed: "
                f"{type(error).__name__}: {error}",
            ) from error


def _validate_render_target(
    template: str | None,
    attachment: str | None,
    source: RenderSource,
    *,
    template_label: str = "TEMPLATE",
    attachment_label: str = "--attachment",
) -> tuple[str | None, str | None]:
    """Validate render target mutual exclusion and fixture compatibility."""
    cleaned_template = (
        template.strip() if isinstance(template, str) and template.strip() else None
    )
    cleaned_attachment = (
        attachment.strip()
        if isinstance(attachment, str) and attachment.strip()
        else None
    )
    if cleaned_template is not None and cleaned_attachment is not None:
        raise ValueError(
            f"specify either {template_label} or {attachment_label}, not both"
        )
    if cleaned_template is None and cleaned_attachment is None:
        raise ValueError(f"specify either {template_label} or {attachment_label}")
    if isinstance(source, FixtureSource) and cleaned_attachment is not None:
        raise ValueError(
            f"fixture rendering cannot assemble attachments; omit {attachment_label} "
            "or use a saved-session, snapshot, or fresh source"
        )
    return cleaned_template, cleaned_attachment


class InterviewRenderer:
    """Own render validation, evaluation, expectations, and artifact effects."""

    def __init__(self, root: str | Path, execution):
        self.root = Path(root).resolve()
        self.execution = execution

    def render(self, request: RenderRequest):
        from docassemble_simulator.execution import _RenderPreparation

        try:
            template, attachment = _validate_render_target(
                request.template, request.attachment, request.source
            )
        except ValueError as error:
            return _render_failure("input", str(error))

        if isinstance(request.source, FixtureSource):
            if request.assemble:
                return _render_failure("input", "fixture rendering cannot assemble")
            if not request.source.path.is_file():
                return _render_failure(
                    "input", f"fixture script not found: {request.source.path}"
                )

        template_path = None
        protected: list[ProtectedPath | ProtectedDirectory] = []
        if template is not None:
            try:
                template_path = find_template(self.root, template)
            except TemplateNotFoundError as error:
                return _render_failure("input", str(error))
            protected.append(ProtectedPath(template_path, "a template or render input"))
        if isinstance(request.source, (FixtureSource, SnapshotSource)):
            protected.append(
                ProtectedPath(
                    request.source.path,
                    "a template or render input",
                )
            )
        protected.extend(
            ProtectedDirectory(directory, "template directories")
            for directory in _template_directories(self.root)
        )

        def action(context):
            namespace = context.namespace
            interview = context.interview
            try:
                _apply_bindings(namespace, request.bindings)
            except RenderBindingError as error:
                details: dict[str, Any] = {
                    "error_type": type(error).__name__,
                    "binding": error.name,
                }
                if template is not None:
                    details["template"] = template
                if attachment is not None:
                    details["attachment"] = attachment
                return RenderOutcome(
                    False,
                    error=RenderFailure(ErrorKind.INPUT, str(error), details),
                )

            if attachment is not None:
                try:
                    entries = enumerate_entries(interview)
                    target = resolve_explicit_target(
                        entries, attachment, namespace=namespace
                    )
                except AttachmentInputError as error:
                    return RenderOutcome(
                        False,
                        error=RenderFailure(ErrorKind.INPUT, str(error), error.details),
                    )
                return self._render_attachment(
                    namespace,
                    target,
                    request,
                    requested_template=None,
                )

            assert template is not None and template_path is not None
            if isinstance(request.source, (SavedSessionSource, SnapshotSource)):
                try:
                    entries = enumerate_entries(interview)
                    auto_target = resolve_automatic_target(entries, namespace, template)
                except AttachmentInputError as error:
                    return RenderOutcome(
                        False,
                        error=RenderFailure(ErrorKind.INPUT, str(error), error.details),
                    )
                if auto_target is not None:
                    return self._render_attachment(
                        namespace,
                        auto_target,
                        request,
                        requested_template=template,
                    )

            return self._render_direct(namespace, template, template_path, request)

        destinations = tuple(
            path for path in (request.save_snapshot, request.output) if path is not None
        )
        prepared = self.execution._with_render_state(
            _RenderPreparation(
                request.source,
                request.assemble,
                request.save_snapshot,
                destinations,
                tuple(protected),
            ),
            action,
        )
        if not prepared.ok:
            return RenderOutcome(
                False,
                error=RenderFailure(
                    prepared.error.kind,
                    prepared.error.message,
                    prepared.error.details or {},
                ),
                diagnostics=prepared.diagnostics,
                attachments=prepared.attachments,
            )
        if isinstance(prepared.result, RenderOutcome):
            return replace(
                prepared.result,
                diagnostics=prepared.diagnostics,
                attachments=prepared.attachments,
            )
        return _render_failure("fault", "render action returned an invalid outcome")

    def _render_direct(
        self,
        namespace: dict[str, Any],
        template: str,
        template_path: Path,
        request: RenderRequest,
    ) -> RenderOutcome:
        prepared = None
        try:
            prepared = prepare_docx_template(template_path)
            rendered = render_template(prepared, namespace, template=template)
            if request.expect_missing:
                raise RenderExpectationError(
                    f"expected missing variable {request.expect_missing!r}, "
                    "but render succeeded"
                )
            artifact = (
                write_artifact(rendered, request.output) if request.output else None
            )
            return RenderOutcome(
                True,
                RenderResult(
                    template,
                    paragraph_count(rendered),
                    artifact,
                ),
            )
        except RenderError as error:
            if request.expect_missing and missing_error_matches(
                error, request.expect_missing
            ):
                return RenderOutcome(
                    True,
                    RenderResult(
                        template,
                        paragraph_count(prepared),
                    ),
                )
            details = {
                "template": _attributed_template(self.root, error, template),
                "error_type": error.error_type,
                "paragraph": error.paragraph,
            }
            if error.render_pass is not None:
                details["render_pass"] = error.render_pass
            return RenderOutcome(
                False,
                error=RenderFailure(ErrorKind.RENDER, str(error), details),
            )
        except (RenderExpectationError, OSError) as error:
            details = {
                "template": template,
                "error_type": type(error).__name__,
            }
            return RenderOutcome(
                False,
                error=RenderFailure(ErrorKind.RENDER, str(error), details),
            )

    def _render_attachment(
        self,
        namespace: dict[str, Any],
        target: AttachmentTarget,
        request: RenderRequest,
        *,
        requested_template: str | None,
    ) -> RenderOutcome:
        fallback_template = (
            requested_template or target.entry.first_template or target.variable
        )
        try:
            assembly = assemble_attachment(namespace, target)
            if request.expect_missing:
                raise RenderExpectationError(
                    f"expected missing variable {request.expect_missing!r}, "
                    "but render succeeded"
                )
            if request.output is not None:
                artifact = copy_artifact(assembly.docx_path, request.output)
                publish_attachment(artifact, assembly.filename)
            else:
                artifact = None
                publish_attachment(assembly.docx_path, assembly.filename)
            result_template = requested_template or assembly.template
            return RenderOutcome(
                True,
                RenderResult(
                    result_template,
                    assembly.paragraphs,
                    artifact,
                    attachment=assembly.canonical_variable,
                ),
            )
        except AttachmentInputError as error:
            return RenderOutcome(
                False,
                error=RenderFailure(ErrorKind.INPUT, str(error), error.details),
            )
        except PDFConversionUnavailable as error:
            return _pdf_unavailable_outcome(error, target.variable)
        except (RenderExpectationError, OSError) as error:
            return RenderOutcome(
                False,
                error=RenderFailure(
                    ErrorKind.RENDER,
                    str(error),
                    {
                        "template": fallback_template,
                        "error_type": type(error).__name__,
                    },
                ),
            )
        except AttachmentAssemblyError as assembly_error:
            cause = assembly_error.cause
            canonical = assembly_error.canonical_variable
            if isinstance(cause, PDFConversionUnavailable):
                return _pdf_unavailable_outcome(cause, canonical)
            error_type = type(cause).__name__
            message = str(cause).strip() or error_type
            paragraph = _exception_line(cause)
            sought_variable = _extract_sought_variable(cause)
            attributed = _attachment_attributed_template(
                self.root, cause, fallback_template
            )
            resolved_alias = (
                resolve_expect_missing_alias(
                    request.expect_missing,
                    assembly_error.prepared_bindings,
                    namespace,
                )
                if request.expect_missing
                else None
            )
            if request.expect_missing and _attachment_missing_matches(
                message,
                sought_variable,
                request.expect_missing,
                resolved_alias,
            ):
                return RenderOutcome(
                    True,
                    RenderResult(
                        fallback_template,
                        0,
                        None,
                        attachment=canonical,
                    ),
                )
            details: dict[str, Any] = {
                "template": attributed,
                "error_type": error_type,
                "paragraph": paragraph,
            }
            if sought_variable is not None:
                details["sought_variable"] = sought_variable
            if request.expect_missing:
                details["expected_missing"] = request.expect_missing
                if resolved_alias is not None:
                    details["resolved_expected_missing"] = resolved_alias
            return RenderOutcome(
                False,
                error=RenderFailure(ErrorKind.RENDER, message, details),
            )


def _render_failure(kind: ErrorKind | str, message: str) -> RenderOutcome:
    return RenderOutcome(False, error=RenderFailure(ErrorKind(kind), message, {}))


def _pdf_unavailable_outcome(error: BaseException, attachment: str) -> RenderOutcome:
    return RenderOutcome(
        False,
        error=RenderFailure(
            ErrorKind.PDF_UNAVAILABLE,
            str(error),
            {
                "error_type": type(error).__name__,
                "attachment": attachment,
            },
        ),
    )


def _template_directories(root: Path) -> list[Path]:
    return [
        path.resolve()
        for path in (root / "docassemble").glob("*/data/templates")
        if path.is_dir()
    ]


def _resolve_package_template(
    root: Path, reference: str, *, allow_relative: bool = True
) -> Path | None:
    """Resolve a template path or relative filename inside ``data/templates``."""
    cleaned = reference.strip()
    if not cleaned:
        return None
    template_dirs = _template_directories(root)
    if not template_dirs:
        return None
    candidate = Path(cleaned).expanduser()
    if candidate.is_file():
        resolved = candidate.resolve()
        if any(resolved.is_relative_to(directory) for directory in template_dirs):
            return resolved
        return None
    if not allow_relative or candidate.is_absolute() or ".." in candidate.parts:
        return None
    for directory in template_dirs:
        direct = (directory / candidate).resolve()
        if direct.is_relative_to(directory) and direct.is_file():
            return direct
    if len(candidate.parts) == 1 and candidate.name:
        nested = [
            match.resolve()
            for directory in template_dirs
            for match in directory.rglob(candidate.name)
            if match.is_file() and match.resolve().is_relative_to(directory)
        ]
        if len(nested) == 1:
            return nested[0]
    return None


def _attributed_template(root: Path, error: RenderError, requested: str) -> str:
    """Prefer an exception template only when it maps to a real package file."""
    if error.template is None:
        return requested
    if (
        _resolve_package_template(root, error.template, allow_relative=False)
        is not None
    ):
        return error.template
    return requested


def _attachment_attributed_template(
    root: Path, error: BaseException, fallback: str
) -> str:
    """Attribute an attachment render failure to a DOCX basename when reliable."""
    filename = _exception_template(error)
    if filename is not None:
        # docassemble sets TemplateError.filename to either a path or
        # comma-joined basenames.
        resolved = _resolve_package_template(root, filename)
        if resolved is None and "," in filename:
            first = filename.split(",", 1)[0].strip()
            if first:
                resolved = _resolve_package_template(root, first)
        if resolved is not None:
            return resolved.name
    return Path(fallback).name if fallback.endswith(".docx") else fallback


def _extract_sought_variable(error: BaseException) -> str | None:
    """Extract the missing variable or attribute name from a template/DA error."""
    variable = getattr(error, "variable", None)
    if isinstance(variable, str) and variable.strip():
        return variable.strip()
    message = str(error)
    match = re.search(
        r"'(.*)' (?:is not defined|referenced before assignment|is undefined|where it is not)",
        message,
    )
    if match:
        return match.group(1)
    attr_match = re.search(
        r"object '([^']+)' has no attribute '([^']+)'",
        message,
    )
    if attr_match:
        return f"{attr_match.group(1)}.{attr_match.group(2)}"
    try:
        from docassemble.base.parse import extract_missing_name

        extracted = extract_missing_name(error)
        if isinstance(extracted, str) and extracted.strip():
            return extracted.strip()
    except Exception as exc:  # noqa: BLE001 - optional docassemble helper
        logger.debug("extract_missing_name failed: %s", exc)
    return None


def _attachment_missing_matches(
    message: str,
    sought_variable: str | None,
    expected: str,
    resolved_alias: str | None,
) -> bool:
    """Whether an attachment render error satisfies ``--expect-missing``."""
    candidates = [expected]
    if resolved_alias and resolved_alias != expected:
        candidates.append(resolved_alias)
    render_error = RenderError(message)
    for candidate in candidates:
        if missing_error_matches(render_error, candidate):
            return True
        if sought_variable is not None and sought_variable == candidate:
            return True
    return False


def _exception_template(error: BaseException) -> str | None:
    filename = getattr(error, "filename", None)
    if not isinstance(filename, str) or filename in {"<template>", "<unknown>"}:
        return None
    return filename


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
    "FixtureSource",
    "FreshSource",
    "InterviewRenderer",
    "RenderBindingError",
    "RenderError",
    "RenderExpectationError",
    "RenderFailure",
    "RenderOutcome",
    "RenderRequest",
    "RenderResult",
    "SavedSessionSource",
    "SnapshotSource",
    "TemplateNotFoundError",
    "assert_missing",
    "find_template",
    "missing_error_matches",
    "paragraph_count",
    "prepare_docx_template",
    "render_template",
    "write_artifact",
]
