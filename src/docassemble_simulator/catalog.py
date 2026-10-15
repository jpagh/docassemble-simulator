"""Read-only interview discovery, compilation, and metadata inspection."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docassemble_simulator._outcomes import ErrorKind
from docassemble_simulator.compatibility import (
    AssemblyLineCompatibilityError,
    require_assemblyline_compatibility,
)

logger = logging.getLogger(__name__)


def list_packages(root: str | Path) -> list[str]:
    """Return package names from a docassemble source workspace."""
    directory = Path(root).resolve() / "docassemble"
    return [
        path.name
        for path in sorted(directory.iterdir())
        if path.is_dir() and (path / "__init__.py").exists()
    ]


def list_interviews(root: str | Path, package: str | None = None) -> list[str]:
    """Return canonical interview identities discovered in the catalog."""
    da_dir = Path(root).resolve() / "docassemble"
    results: list[str] = []
    packages = [package] if package else list_packages(root)
    for package_name in packages:
        base = da_dir / package_name / "data" / "questions"
        if not base.is_dir():
            continue
        for yaml_path in sorted(base.rglob("*.yml")) + sorted(base.rglob("*.yaml")):
            relative = yaml_path.relative_to(da_dir / package_name)
            if any(
                part.startswith(".") or part == "__pycache__" for part in relative.parts
            ):
                continue
            results.append(f"docassemble.{package_name}:{relative.as_posix()}")
    return results


def guess_main_interview(interviews: list[str]) -> str | None:
    """Prefer a top-level main interview, then any main, then the first."""
    mains = [
        identity
        for identity in interviews
        if re.match(r"^docassemble\.[^:]+:data/questions/main\.ya?ml$", identity)
    ]
    if mains:
        return mains[0]
    others = [
        identity
        for identity in interviews
        if identity.endswith(("main.yml", "main.yaml"))
    ]
    return others[0] if others else (interviews[0] if interviews else None)


def resolve_interview(
    root: str | Path, requested: str | None = None
) -> tuple[str, list[str]]:
    """Resolve a selector to one canonical Interview definition identity."""
    interviews = list_interviews(root)
    if requested is None:
        chosen = guess_main_interview(interviews)
        if chosen is None:
            raise SystemExit(
                "error: no interview YAML files found under data/questions; "
                "pass one explicitly with --interview"
            )
        return chosen, interviews
    if ":" in requested and not requested.startswith("/"):
        return requested, interviews
    matches = [
        identity
        for identity in interviews
        if identity.rsplit(":", 1)[1] == requested
        or identity.rsplit(":", 1)[1].endswith("/" + requested)
    ]
    if len(matches) == 1:
        return matches[0], interviews
    if len(matches) > 1:
        raise SystemExit(
            "error: '" + requested + "' is ambiguous:\n  " + "\n  ".join(matches)
        )
    raise SystemExit(
        f"error: interview '{requested}' not found. Available:\n  "
        + "\n  ".join(interviews[:50])
    )


class CatalogFailure(Exception):
    """A user-facing interview definition compilation failure."""


@dataclass(frozen=True)
class CatalogOutcome:
    interview: str | None
    result: Any


def _normalize_guards(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _guards_from_source(source_code: str | None) -> list[str]:
    """Recover authored ``if:`` values when the compiler dropped them."""
    if not source_code:
        return []
    try:
        import yaml

        document = yaml.safe_load(source_code)
    except Exception:  # noqa: BLE001 - malformed authored source has a text fallback
        document = None
    if isinstance(document, dict) and "if" in document:
        return _normalize_guards(document["if"])

    match = re.search(r"(?m)^\s*if\s*:\s*(.*)$", source_code)
    if match and match.group(1).strip():
        return [match.group(1).strip()]
    if match:
        values = []
        for line in source_code.splitlines()[
            source_code[: match.start()].count("\n") + 1 :
        ]:
            item = re.match(r"^\s*-\s+(.+?)\s*$", line)
            if not item:
                break
            values.append(item.group(1))
        return values
    return []


def _source_block(source_text: str, line_number: int | None) -> str:
    if not source_text or not line_number:
        return source_text
    lines = source_text.splitlines()
    start = max(0, min(len(lines), line_number - 1))
    if start < len(lines) and lines[start].strip() != "---":
        marker = start
        while marker > 0 and lines[marker].strip() != "---":
            marker -= 1
        if lines[marker].strip() == "---":
            start = marker
    end = start + 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    return "\n".join(lines[start:end])


def _source_details(question) -> tuple[str, int | None]:
    source = getattr(question, "from_source", None)
    source_file = getattr(source, "path", None) or getattr(source, "filepath", None)
    line_number = getattr(question, "line_number", None)
    try:
        line = int(line_number) if line_number is not None else None
    except (TypeError, ValueError):
        line = None
    return str(source_file or "unknown"), line


def _guards_are_provably_disjoint(left: list[str], right: list[str]) -> bool:
    def signature(expression: str):
        try:
            node = ast.parse(expression, mode="eval").body
        except (SyntaxError, ValueError):
            return None
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            return None
        if not isinstance(node.ops[0], (ast.Eq, ast.Is)):
            return None
        if not isinstance(node.left, (ast.Name, ast.Attribute)):
            return None
        if len(node.comparators) != 1 or not isinstance(
            node.comparators[0], ast.Constant
        ):
            return None
        return ast.unparse(node.left), node.comparators[0].value

    left_signatures = [signature(item) for item in left]
    right_signatures = [signature(item) for item in right]
    if not left_signatures or not right_signatures:
        return False
    return all(
        left_signature is not None
        and right_signature is not None
        and left_signature[0] == right_signature[0]
        and left_signature[1] != right_signature[1]
        for left_signature in left_signatures
        for right_signature in right_signatures
    )


def _candidates_can_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return whether two evaluated candidates could resolve together.

    A definitely inactive guard cannot overlap anything.  An unknown guard is
    deliberately not treated as inactive; only authored guard expressions we
    can prove disjoint are excluded from the conservative conflict policy.
    """
    if left["live"] is False or right["live"] is False:
        return False
    return not _guards_are_provably_disjoint(left["guards"], right["guards"])


def _explicit_id(question) -> str | None:
    explicit_id = getattr(question, "id", None)
    debug = getattr(question, "data_for_debug", None)
    if explicit_id is None and isinstance(debug, dict):
        explicit_id = debug.get("id")
    if explicit_id is None:
        return None
    value = str(explicit_id).strip()
    return value or None


def _authored_id_relationship(question) -> tuple[str, Any] | None:
    debug = getattr(question, "data_for_debug", None)
    authored = debug if isinstance(debug, dict) else {}
    try:
        import yaml

        parsed = yaml.safe_load(getattr(question, "source_code", ""))
        source_authored = parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 - provenance must not break checking
        source_authored = {}
    for name in ("override", "overrides", "supersede", "supersedes"):
        value = getattr(question, name, None)
        if value is None:
            value = authored.get(name, source_authored.get(name))
        if value:
            return name, value
    return None


def _id_relationship(question) -> str | None:
    relationship = _authored_id_relationship(question)
    if relationship is None:
        return None
    name, value = relationship
    if isinstance(value, bool):
        return name
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _relationship_targets(question) -> list[str]:
    relationship = _authored_id_relationship(question)
    if relationship is None:
        return []
    _name, value = relationship
    if isinstance(value, bool):
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    target = str(value).strip()
    return [target] if target else []


def _provenance(question) -> dict[str, Any]:
    source_file, line = _source_details(question)
    debug = getattr(question, "data_for_debug", None)
    authored_guards = (
        debug.get("if") if isinstance(debug, dict) and "if" in debug else None
    )
    guards = _normalize_guards(authored_guards)
    if not guards:
        guards = _guards_from_source(getattr(question, "source_code", None))
    if not guards:
        source = getattr(question, "from_source", None)
        filepath = getattr(source, "filepath", None)
        if filepath:
            try:
                source_text = Path(filepath).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                source_text = None
            guards = _guards_from_source(_source_block(source_text, line))

    guard = "none" if not guards else (guards[0] if len(guards) == 1 else guards)
    explicit_id = _explicit_id(question)
    result = {
        "source_file": source_file,
        "line": line,
        "location": f"{source_file}:{line or '?'}",
        "block_type": getattr(question, "question_type", "?"),
        "guards": guards,
        "guard": guard,
        # Keep the existing condition key, but make it authored text rather
        # than the repr of compiled code objects.
        "condition": guard,
    }
    if explicit_id is not None:
        result["id"] = explicit_id
    return result


def _duplicate_id_report(interview, interview_identity: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    entries = []
    for question in getattr(interview, "questions_list", None) or ():
        explicit_id = _explicit_id(question)
        if explicit_id is None:
            continue
        block = _provenance(question)
        block["relationship"] = _id_relationship(question)
        entry = {
            "id": explicit_id,
            "block": block,
            "targets": _relationship_targets(question),
        }
        entries.append(entry)
        grouped.setdefault(explicit_id, []).append(entry)

    reports = []
    for explicit_id, blocks in grouped.items():
        if len(blocks) < 2:
            continue
        reports.append(
            {
                "id": explicit_id,
                "kind": ErrorKind.DUPLICATE_ID,
                "interview": interview_identity,
                "warning": True,
                "blocks": [entry["block"] for entry in blocks],
            }
        )

    # A valid supersedes/override relationship is useful to report, but it
    # never excuses a separate duplicate-id collision.  The upstream runtime
    # resolves an id relationship by the target id, so use the last-loaded
    # target when an authored target is itself duplicated.
    targets_by_id = {}
    for entry in entries:
        targets_by_id.setdefault(entry["id"], []).append(entry)
    for entry in entries:
        for target_id in entry["targets"]:
            if target_id == entry["id"] or target_id not in targets_by_id:
                continue
            target = targets_by_id[target_id][-1]
            reports.append(
                {
                    "id": entry["id"],
                    "kind": "deliberate-override",
                    "interview": interview_identity,
                    "warning": True,
                    "blocks": [target["block"], entry["block"]],
                }
            )
    return reports


@dataclass(frozen=True)
class _WatchRecord:
    """The compile-time reload state for one cached interview identity.

    ``source`` is docassemble's own source object (its ``get_modtime`` is the
    timestamp docassemble keeps for the selected file). ``main_path`` is that
    file's resolved path when it is inside the workspace root. ``source_paths``
    is every workspace source the compiled questions came from (the selected
    file plus its includes). ``module_paths`` is the declared workspace helper
    files and package directories. Each signature is captured at compile time
    and compared at the next operation boundary.
    """

    source: Any
    main_path: str | None
    source_paths: tuple[str, ...]
    source_signature: tuple | None
    module_paths: tuple[str, ...]
    module_signature: tuple


class InterviewCatalog:
    """Compile and inspect interview definitions without creating session state."""

    def __init__(
        self,
        root: str | Path,
        selector: str | None = None,
        *,
        cache_compiled: bool = False,
    ):
        self.root = Path(root).resolve()
        self.selector = selector
        # Only persistent workers opt in. One-shot and test callers retain the
        # historical fresh-compile behavior, while a worker's catalog cache is
        # still scoped to one root/selector/process.
        self._cache_compiled = cache_compiled
        self._compiled: dict[str, Any] = {}
        # Reload bookkeeping for the persistent worker, one record per compiled
        # identity: the docassemble source object, the watched source files
        # (the selected YAML plus every included YAML), the declared workspace
        # helper files and package directories, and the signatures captured at
        # compile time. Only populated when the compiled cache is on.
        self._watches: dict[str, _WatchRecord] = {}

    @property
    def identity(self) -> str:
        return resolve_interview(self.root, self.selector)[0]

    def _compile(self, identity: str | None = None):
        """Internal definition loader shared with execution.

        The AssemblyLine compatibility probe runs before the target Interview:
        a runtime pair that cannot compile the standard birthdate metadata
        would otherwise surface as an authored-looking label error. Authored
        parse errors after a passing probe keep their compile-error behavior.

        A cached catalog revalidates the identity it is asked for before
        serving it, so every caller compiles through this one authority and
        reads a current program without a separate refresh step. On a cache hit
        it compares the watched signatures. With no watch of its own but
        docassemble's process-global cache already holding the identity
        (another catalog in this process compiled it), it drops that entry and
        the declared workspace helpers it imported before recompiling.
        """
        from docassemble_simulator._diagnostics import capture_compile_warnings

        selected = identity or self.identity
        require_assemblyline_compatibility()
        from docassemble.base.interview_cache import get_interview

        from docassemble_simulator._runtime import runtime_context

        if self._cache_compiled:
            cached = self._compiled.get(selected)
            if cached is not None:
                if self._source_current(selected):
                    return cached
                # The source changed under a running worker: drop both
                # docassemble's compile and ours so get_interview recompiles.
                self._reload_source(selected)
            elif self._cached_interview(selected) is not None:
                # No watch of our own, but docassemble's process-global cache
                # already holds this identity (another catalog in this process
                # compiled it). With no signature to compare it may be stale,
                # so drop the entry and the workspace helpers it imported
                # before compiling.
                self._reload_source(selected)
        saved_argv = list(sys.argv)
        sys.argv[:] = ["docassemble-simulator", selected]
        try:
            with runtime_context(), capture_compile_warnings(selected):
                interview = get_interview(selected)
        finally:
            sys.argv[:] = saved_argv
        if self._cache_compiled:
            self._compiled[selected] = interview
            self._record_source(selected, interview)
        return interview

    def _global_cache_entry(self, selected: str):
        """docassemble's process-global cache entry for an identity, if any.

        A cache read, never a compile. The reload path uses it to learn what
        another catalog compiled for this identity, because that entry is the
        only record of the workspace helpers the compile imported once this
        catalog has no watch of its own.
        """
        try:
            from docassemble.base.interview_cache import cache
        except (ImportError, AttributeError):
            return None
        entry = cache.get(selected) if isinstance(cache, dict) else None
        return entry if isinstance(entry, dict) else None

    def _cached_source(self, selected: str):
        """The docassemble source object for a compiled identity, if any."""
        entry = self._global_cache_entry(selected)
        return entry.get("source") if entry is not None else None

    def _cached_interview(self, selected: str):
        """The interview docassemble's cache holds for an identity, if any."""
        entry = self._global_cache_entry(selected)
        return entry.get("interview") if entry is not None else None

    def _resolved_source_path(self, selected: str) -> str | None:
        """Fall back to the workspace path for a canonical identity."""
        package, separator, relative = selected.partition(":")
        if not separator or not package.startswith("docassemble."):
            return None
        candidate = self.root / package.replace(".", "/") / relative
        return self._contained(candidate)

    def _contained(self, candidate: Path) -> str | None:
        """A candidate file/dir under the resolved workspace root, or ``None``.

        ``Path.resolve()`` collapses symlinks, so a workspace name that points
        outside the tree is rejected before it can be watched, evicted, or have
        its bytecode unlinked. Existence is still required for the fallback
        identity resolution; a recorded path that later disappears is handled
        by the signature check, not here.
        """
        if not candidate.is_file() and not candidate.is_dir():
            return None
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(self.root):
            return None
        return str(resolved)

    def _record_source(self, selected: str, interview) -> None:
        """Capture the source and declared-module signatures for later checks."""
        source = self._cached_source(selected)
        main_path = getattr(source, "filepath", None) or self._resolved_source_path(
            selected
        )
        source_paths = self._source_paths(interview, main_path)
        main_resolved = self._contained(Path(main_path)) if main_path else None
        module_paths = self._module_paths_for(self._declared_module_names(interview))
        self._watches[selected] = _WatchRecord(
            source=source,
            main_path=main_resolved,
            source_paths=source_paths,
            source_signature=self._source_signature(
                source, main_resolved, source_paths
            ),
            module_paths=module_paths,
            module_signature=self._tree_signatures(module_paths),
        )

    def _source_paths(self, interview, main_path) -> tuple[str, ...]:
        """The workspace files a reload must invalidate on.

        Two sets are combined. The exact set comes from question provenance:
        every compiled question carries ``from_source.filepath``, the absolute
        file docassemble read it from, which covers abbreviated colon identities
        (``docassemble.other:screens.yml`` resolving under
        ``docassemble/other/data/questions/``) and a nested relative include
        resolved against its own owning source. The safety net is every YAML
        under the workspace questions trees, because docassemble excludes
        include-only blocks and features-only documents from
        ``questions_list``, so a routing YAML that changes its ``include:``
        target has no compiled question to watch. Over-inclusion only causes a
        reload; under-inclusion would serve a stale program. Only sources under
        the resolved workspace root are watched.
        """
        candidates: list[str] = []
        if main_path:
            candidates.append(str(main_path))
        for question in getattr(interview, "questions_list", None) or ():
            from_source = getattr(question, "from_source", None)
            filepath = getattr(from_source, "filepath", None)
            if filepath:
                candidates.append(str(filepath))
        candidates.extend(self._workspace_source_candidates())
        return self._contained_paths(candidates)

    def _workspace_source_candidates(self) -> list[str]:
        """Every YAML source under the workspace's ``data/questions`` trees."""
        docassemble = self.root / "docassemble"
        if not docassemble.is_dir():
            return []
        candidates: list[str] = []
        for questions in sorted(docassemble.glob("*/data/questions")):
            if not questions.is_dir():
                continue
            for path in sorted(questions.rglob("*.yml")) + sorted(
                questions.rglob("*.yaml")
            ):
                candidates.append(str(path))
        return candidates

    def _contained_paths(self, candidates: list[str]) -> tuple[str, ...]:
        """Resolved, workspace-contained, de-duplicated paths in order."""
        kept: list[str] = []
        for candidate in candidates:
            try:
                resolved = Path(candidate).resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(self.root):
                logger.debug(
                    "reload watch skips source outside workspace %r", candidate
                )
                continue
            text = str(resolved)
            if text not in kept:
                kept.append(text)
        return tuple(kept)

    def _source_signature(self, source, main_path, paths: tuple[str, ...]):
        """docassemble's own source timestamp plus each file's size.

        The simulator's stubbed redis keeps docassemble's interview index at a
        constant and nothing here calls ``update_index`` (that is the web
        editor's job), so ``interview_cache.cache_valid`` never notices an
        edit. ``InterviewSourceFile.get_modtime`` is the per-source timestamp
        docassemble itself keeps for the selected file; the rest of the files
        (includes) use ``os.stat``. A missing or unreadable source yields
        ``None`` so the next check forces a reload and compilation surfaces
        the typed error.
        """
        entries = []
        get_modtime = getattr(source, "get_modtime", None)
        for path in paths:
            try:
                stat = os.stat(path)
            except OSError:
                return None
            mtime = stat.st_mtime
            if path == main_path and callable(get_modtime):
                try:
                    mtime = get_modtime()
                except Exception:  # noqa: BLE001, S110 - runtime source object
                    pass
            entries.append((path, mtime, stat.st_size))
        return tuple(entries)

    def _source_current(self, selected: str) -> bool:
        """Whether the cached compile still matches its source and helpers."""
        record = self._watches.get(selected)
        if record is None or not record.source_paths:
            # No measurable source (for example an in-memory source); trust the
            # cache rather than recompiling every operation.
            return True
        current = self._source_signature(
            record.source, record.main_path, record.source_paths
        )
        if current is None or current != record.source_signature:
            return False
        return self._tree_signatures(record.module_paths) == record.module_signature

    def _declared_module_names(self, interview) -> set[str]:
        """Fully-qualified names declared by the interview's import directives."""
        names: set[str] = set()
        for question in getattr(interview, "questions_list", None) or ():
            if getattr(question, "question_type", "") not in {"modules", "imports"}:
                continue
            package = getattr(question, "package", None)
            for name in getattr(question, "module_list", None) or ():
                if not isinstance(name, str):
                    continue
                if name.startswith(".") and package:
                    names.add(str(package) + name)
                else:
                    names.add(name)
        return names

    def _module_paths_for(self, names: set[str]) -> tuple[str, ...]:
        """Files and package directories for the interview's own helpers.

        Resolution is a workspace-tree lookup rather than
        ``importlib.util.find_spec``: find_spec imports a dotted module's parent
        package, which would execute authored package code during compilation
        and could import a heavy installed dependency. A module that is not a
        workspace package (stdlib, an installed docassemble package, a
        mis-declared name) is skipped; those are not the interview's own source
        and are not edited during interview development.
        """
        paths = set()
        for name in sorted(names):
            path = self._module_path(name)
            if path is None:
                logger.debug("reload watch skips non-workspace module %r", name)
                continue
            paths.add(path)
        return tuple(sorted(paths))

    def _module_path(self, name: str) -> str | None:
        """Map ``docassemble.<pkg>[.<sub>]`` to a workspace file or package dir."""
        parts = name.split(".")
        if len(parts) < 2 or parts[0] != "docassemble":
            return None
        package_dir = self.root.joinpath(*parts)
        module_file = self.root.joinpath(*parts[:-1]) / (parts[-1] + ".py")
        if (package_dir / "__init__.py").is_file():
            candidate = package_dir
        elif module_file.is_file():
            candidate = module_file
        elif package_dir.is_dir():
            # A namespace package has no __init__.py; walk it so its modules
            # are still watched.
            candidate = package_dir
        else:
            return None
        # ``_contained`` resolves symlinks, so a workspace name that escapes the
        # tree is skipped instead of watched, evicted, or unlinked.
        return self._contained(candidate)

    def _tree_signatures(self, paths: tuple[str, ...]) -> tuple:
        """Per-path ``*.py`` timestamp and size for the watched module trees."""
        return tuple(self._tree_signature(path) for path in paths)

    def _tree_signature(self, path: str):
        """A sorted file signature for one module file or package directory."""
        try:
            if os.path.isdir(path):
                entries = []
                for file in sorted(Path(path).rglob("*.py")):
                    if "__pycache__" in file.parts:
                        continue
                    resolved = file.resolve()
                    if not resolved.is_relative_to(self.root):
                        continue
                    stat = file.stat()
                    entries.append((str(resolved), stat.st_mtime, stat.st_size))
                return tuple(entries)
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(self.root):
                return None
            stat = os.stat(path)
            return ((str(resolved), stat.st_mtime, stat.st_size),)
        except OSError:
            return None

    def _reload_source(self, selected: str) -> None:
        """Forget the stale compile and the helper modules it imported."""
        try:
            from docassemble.base.interview_cache import clear_cache
        except (ImportError, AttributeError):
            clear_cache = None
        record = self._watches.get(selected)
        cached = self._compiled.get(selected)
        # Read docassemble's entry before clearing it. Without a local watch the
        # cached interview is the only record of which workspace helpers the
        # compile imported, so it has to be read while the entry still exists.
        global_interview = self._cached_interview(selected)
        if clear_cache is not None:
            clear_cache(selected)
        if record is not None:
            # Drop cached bytecode for helpers that changed, so a same-size,
            # same-second edit cannot re-import stale bytecode after eviction.
            self._invalidate_bytecode(self._changed_module_paths(record))
        if cached is not None:
            self._evict_modules(cached)
        elif global_interview is not None:
            # Never watched (an identity another catalog in this process
            # compiled): the cached interview declares the helpers that compile
            # imported, and with no signature saying which of them changed, all
            # of their bytecode is dropped.
            declared = self._declared_module_names(global_interview)
            self._invalidate_bytecode(self._module_paths_for(declared))
            self._evict_modules(global_interview)
        self._compiled.pop(selected, None)
        self._watches.pop(selected, None)

    def _changed_module_paths(self, record: _WatchRecord) -> tuple[str, ...]:
        """Watched helper paths whose file signature changed since compile."""
        changed = []
        for index, path in enumerate(record.module_paths):
            previous = (
                record.module_signature[index]
                if index < len(record.module_signature)
                else None
            )
            if self._tree_signature(path) != previous:
                changed.append(path)
        return tuple(changed)

    def _invalidate_bytecode(self, paths: tuple[str, ...]) -> None:
        """Unlink cached bytecode for the workspace helpers about to reload.

        CPython validates a timestamp ``.pyc`` against the source's
        integer-second mtime and size, so a same-size edit within the same
        second re-imports the stale bytecode. Removing the cache forces a
        recompile from the edited source.
        """
        for path in paths:
            target = Path(path)
            files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
            for file in files:
                if "__pycache__" in file.parts:
                    continue
                try:
                    resolved = file.resolve()
                except OSError:
                    continue
                if not resolved.is_relative_to(self.root):
                    continue
                try:
                    cache = importlib.util.cache_from_source(str(resolved))
                except (NotImplementedError, ValueError):
                    continue
                # Resolve the cache path too: a ``__pycache__`` directory that
                # is a symlink outside the tree would otherwise have its
                # external bytecode deleted.
                try:
                    cache_resolved = Path(cache).resolve()
                except OSError:
                    continue
                if not cache_resolved.is_relative_to(self.root):
                    continue
                try:
                    os.unlink(cache_resolved)
                except OSError:
                    continue

    def _evict_modules(self, interview) -> None:
        """Forget the interview's declared workspace ``modules:``/``imports:``.

        docassemble imports those helpers on first assembly and leaves them in
        ``sys.modules``; without eviction a recompiled interview keeps serving
        the old helper code for the rest of the worker's life. Only declared
        names that resolve under the workspace root are evicted: removing an
        installed module (``imports: [docassemble.base]``) would split its
        identity and thread-local state from references the rest of the process
        still holds.
        """
        names = self._declared_module_names(interview)
        prefixes = {name for name in names if self._module_path(name) is not None}
        if not prefixes:
            return
        evicted = [
            name
            for name in list(sys.modules)
            if any(
                name == prefix or name.startswith(prefix + ".") for prefix in prefixes
            )
        ]
        for name in evicted:
            sys.modules.pop(name, None)
        if evicted:
            importlib.invalidate_caches()

    def check(self) -> CatalogOutcome:
        identities = [self.identity] if self.selector else list_interviews(self.root)
        rows = []
        for identity in identities:
            try:
                interview = self._compile(identity)
                duplicate_ids = _duplicate_id_report(interview, identity)
                rows.append(
                    {
                        "interview": identity,
                        "ok": True,
                        "blocks": len(interview.questions_list),
                        "questions": sum(
                            getattr(q, "question_type", "") in {"question", "fields"}
                            for q in interview.questions_list
                        ),
                        "duplicate_ids": duplicate_ids,
                    }
                )
            except AssemblyLineCompatibilityError:
                # A runtime-pair incapability is not a per-Interview compile
                # failure; surface it as one typed environment failure.
                raise
            except Exception as error:  # noqa: BLE001 - DAError varies by runtime release
                logger.debug("interview %r failed to compile: %s", identity, error)
                rows.append(
                    {
                        "interview": identity,
                        "ok": False,
                        "error_type": type(error).__name__,
                        "message": str(error)[:300],
                    }
                )
        failures = sum(not row["ok"] for row in rows)
        duplicate_ids = [
            duplicate for row in rows for duplicate in row.get("duplicate_ids", [])
        ]
        return CatalogOutcome(
            None,
            {
                "checked": len(rows),
                "failures": failures,
                "duplicate_ids": duplicate_ids,
                "totals": {
                    "checked": len(rows),
                    "ok": len(rows) - failures,
                    "failed": failures,
                    "duplicate_ids": len(
                        [
                            item
                            for item in duplicate_ids
                            if item["kind"] == ErrorKind.DUPLICATE_ID
                        ]
                    ),
                },
                "results": rows,
            },
        )

    def questions(self, contains: str | None = None) -> CatalogOutcome:
        from docassemble_simulator.describe import field_variable

        interview = self._compile_for_inspection()
        rows = []
        for index, question in enumerate(interview.questions_list):
            variables = []
            for field in getattr(question, "fields", None) or []:
                variables.append(field_variable(field))
            name = getattr(question, "name", None)
            if (
                contains
                and contains.lower() not in " ".join(variables + [name or ""]).lower()
            ):
                continue
            provenance = _provenance(question)
            rows.append(
                {
                    "index": index,
                    "mandatory": bool(getattr(question, "is_mandatory", False)),
                    "name": name,
                    "first_variable": variables[0] if variables else "",
                    "variables": variables or None,
                    **provenance,
                }
            )
        return CatalogOutcome(self.identity, {"blocks": rows})

    def index(self, contains: str | None = None) -> CatalogOutcome:
        interview = self._compile_for_inspection()
        mapping = {}
        for variable, entry in interview.questions.items():
            if contains and contains.lower() not in variable.lower():
                continue
            screens = []
            for language in ("en", "*"):
                for question in entry.get(language) or []:
                    screens.append(
                        {
                            "language": language,
                            "question_name": getattr(question, "name", None),
                            **_provenance(question),
                        }
                    )
            mapping[variable] = screens or None
        return CatalogOutcome(self.identity, {"index": mapping})

    def why(
        self,
        variable: str,
        namespace: dict[str, Any] | None = None,
        *,
        interview=None,
        language: str | None = None,
    ):
        """Explain resolution using the compiled runtime's candidate order.

        ``interview`` is supplied by a prepared execution when a saved
        session is available.  Keeping compilation and guard evaluation in
        that execution context lets real runtime objects and helpers behave as
        they do during seeking, while the returned report contains only plain
        values.
        """
        interview = interview or self._compile_for_inspection()
        entry = (getattr(interview, "questions", {}) or {}).get(variable) or {}
        if language is None:
            try:
                from docassemble.base.functions import get_language

                language = get_language()
            except (ImportError, AttributeError, RuntimeError):
                language = None
        language = language or getattr(interview, "default_language", None) or "en"
        questions = []
        for candidate_language in (language, "*"):
            values = entry.get(candidate_language) or []
            sorter = getattr(interview, "sort_with_orderings", None)
            ordered = sorter(values) if callable(sorter) else reversed(values)
            questions.extend((question, candidate_language) for question in ordered)

        candidates = []
        for order, (question, language) in enumerate(questions, start=1):
            provenance = _provenance(question)
            guards = provenance["guards"]
            live = None
            evaluation_error = None
            if namespace is not None:
                if not guards:
                    live = True
                else:
                    try:
                        live = all(bool(eval(guard, namespace)) for guard in guards)
                    except Exception as error:  # noqa: BLE001 - guard code varies by runtime
                        live = None
                        evaluation_error = f"{type(error).__name__}: {error}"
            candidate = {
                "order": order,
                "language": language,
                "winner": False,
                "live": live,
                "provenance": provenance,
                **provenance,
            }
            if namespace is not None:
                candidate["state"] = (
                    "unknown" if live is None else ("live" if live else "inactive")
                )
            if evaluation_error:
                candidate["guard_error"] = evaluation_error
            candidates.append(candidate)

        live_indexes = [
            index
            for index, candidate in enumerate(candidates)
            if candidate["live"] is True
        ]
        statically_disjoint = all(
            _guards_are_provably_disjoint(
                candidates[left]["guards"], candidates[right]["guards"]
            )
            for left in range(len(candidates))
            for right in range(left + 1, len(candidates))
        )
        unknown_overlap = False
        if namespace is None:
            conflict = len(candidates) > 1 and not statically_disjoint
            winner_index = 0 if candidates else None
        else:
            conflict = False
            for left in range(len(candidates)):
                for right in range(left + 1, len(candidates)):
                    if not _candidates_can_overlap(candidates[left], candidates[right]):
                        continue
                    conflict = True
                    unknown_overlap |= (
                        candidates[left]["live"] is None
                        or candidates[right]["live"] is None
                    )
            # A live candidate is a winner only when its guard state is known
            # enough to exclude an overlapping unknown candidate.  Keep the
            # established first-live winner for known live/live conflicts, but
            # never claim a winner that an evaluation error could displace.
            winner_index = (
                live_indexes[0] if live_indexes and not unknown_overlap else None
            )
        if winner_index is not None:
            candidates[winner_index]["winner"] = True

        winner = candidates[winner_index] if winner_index is not None else None
        return CatalogOutcome(
            self.identity,
            {
                "variable": variable,
                "order": "last-loaded-first",
                "session_state": "saved" if namespace is not None else "not saved",
                "candidates": candidates,
                "winner": winner,
                "conflict": conflict,
                "conflict_details": (
                    {
                        "kind": ErrorKind.RESOLUTION_CONFLICT,
                        "variable": variable,
                        "candidates": [
                            {
                                **candidate["provenance"],
                                "order": candidate["order"],
                                **(
                                    {"state": candidate["state"]}
                                    if "state" in candidate
                                    else {}
                                ),
                                **(
                                    {"guard_error": candidate["guard_error"]}
                                    if "guard_error" in candidate
                                    else {}
                                ),
                            }
                            for candidate in candidates
                        ],
                    }
                    if conflict
                    else None
                ),
            },
        )

    def _compile_for_inspection(self):
        try:
            return self._compile()
        except AssemblyLineCompatibilityError:
            raise
        except Exception as error:  # includes DAError/DAErrorMissingVariable
            raise CatalogFailure(f"{type(error).__name__}: {error}") from error


__all__ = [
    "CatalogFailure",
    "CatalogOutcome",
    "InterviewCatalog",
    "guess_main_interview",
    "list_interviews",
    "list_packages",
    "resolve_interview",
]
