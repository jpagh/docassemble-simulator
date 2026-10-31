"""Reviewed corpus expectations for capability boundaries and deliberate errors.

Issue #89 pins sixteen reviewed expectations: eleven PDF-only attachments
(ADR-0006), two rtf-to-docx converter requests (ADR-0001), and three deliberate
error demonstrations. Issue #90 pins five more PDF cases: three attachment
``.pdf`` link requests and two manual attachment concatenations.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from docassemble_simulator._runtime import PDF_UNAVAILABLE_MESSAGE
from docassemble_simulator.demo_corpus import (
    CaseResult,
    CorpusError,
    Expectation,
    RunPhase,
    load_expectations,
    load_provenance_manifest,
)

PROJECT = Path(__file__).resolve().parents[1]
EXPECTATIONS = PROJECT / "tests" / "demo_corpus_expectations.toml"
PROVENANCE = PROJECT / "tests" / "demo_corpus_provenance.toml"

PDF_ONLY_FIXTURES = {
    "allow-emailing-false-pdf.yml",
    "attachment-notice.yml",
    "attachment.yml",
    "document-links-limited.yml",
    "formatting.yml",
    "immediate_file.yml",
    "manual-attachment-list.yml",
    "pdf-a.yml",
    "tagged-pdf.yml",
    "valid-formats-code.yml",
    "valid-formats.yml",
}
CONVERTER_FIXTURES = {
    "document-docx-from-rtf.yml",
    "document-docx-from-rtf-standalone.yml",
}
DELIBERATE_ERROR_FIXTURES = {
    "error-help.yml",
    "error-help-language.yml",
    "infinite-loop-2.yml",
}
PDF_LINK_FIXTURES = {
    "dafile-url-for.yml",
    "dafile-url-for-temporary.yml",
    "document-variable-name-link.yml",
}
MANUAL_PDF_FIXTURES = {
    "document-manual.yml",
    "document-manual-code.yml",
}
REVIEWED_FIXTURES = (
    PDF_ONLY_FIXTURES
    | CONVERTER_FIXTURES
    | DELIBERATE_ERROR_FIXTURES
    | PDF_LINK_FIXTURES
    | MANUAL_PDF_FIXTURES
)
PDF_FIXTURES = PDF_ONLY_FIXTURES | PDF_LINK_FIXTURES | MANUAL_PDF_FIXTURES


def _identities() -> dict[str, str]:
    return {
        entry.fixture: entry.identity for entry in load_provenance_manifest(PROVENANCE)
    }


def _reviewed_expectations() -> tuple[Expectation, ...]:
    identities = _identities()
    selected = {identities[name] for name in REVIEWED_FIXTURES}
    expectations = tuple(
        expectation
        for expectation in load_expectations(EXPECTATIONS)
        if expectation.interview in selected
    )
    assert len(expectations) == len(selected)
    return expectations


def test_expectation_file_is_exactly_the_twenty_one_reviewed_cases() -> None:
    identities = _identities()
    expectations = load_expectations(EXPECTATIONS)

    assert len(expectations) == 21
    assert {expectation.interview for expectation in expectations} == {
        identities[name] for name in REVIEWED_FIXTURES
    }
    assert Counter(expectation.category for expectation in expectations) == {
        "capability-boundary": 18,
        "deliberate-error": 3,
    }
    assert Counter(expectation.error_kind for expectation in expectations) == {
        "pdf-unavailable": 16,
        "execution": 5,
    }


def test_reviewed_expectations_are_exactly_the_sixteen_issue89_cases() -> None:
    identities = _identities()
    issue89 = {
        identities[name]
        for name in PDF_ONLY_FIXTURES | CONVERTER_FIXTURES | DELIBERATE_ERROR_FIXTURES
    }
    expectations = tuple(
        expectation
        for expectation in load_expectations(EXPECTATIONS)
        if expectation.interview in issue89
    )

    assert len(expectations) == 16
    assert (
        sum(
            expectation.category == "capability-boundary"
            for expectation in expectations
        )
        == 13
    )
    assert (
        sum(expectation.category == "deliberate-error" for expectation in expectations)
        == 3
    )
    capability_fixtures = {
        expectation.interview
        for expectation in expectations
        if expectation.category == "capability-boundary"
    }
    assert capability_fixtures == {
        identities[name] for name in PDF_ONLY_FIXTURES | CONVERTER_FIXTURES
    }
    deliberate_fixtures = {
        expectation.interview
        for expectation in expectations
        if expectation.category == "deliberate-error"
    }
    assert deliberate_fixtures == {
        identities[name] for name in DELIBERATE_ERROR_FIXTURES
    }


def test_reviewed_expectations_are_five_further_issue90_pdf_cases() -> None:
    identities = _identities()
    issue90 = {identities[name] for name in PDF_LINK_FIXTURES | MANUAL_PDF_FIXTURES}
    expectations = tuple(
        expectation
        for expectation in load_expectations(EXPECTATIONS)
        if expectation.interview in issue90
    )

    assert len(expectations) == 5
    assert {expectation.interview for expectation in expectations} == issue90
    assert all(
        expectation.category == "capability-boundary" for expectation in expectations
    )


def test_reviewed_expectations_constrain_every_reviewed_failure() -> None:
    for expectation in _reviewed_expectations():
        assert expectation.phase == RunPhase.START
        assert expectation.classification == "failure"
        assert expectation.returncode == 2
        assert expectation.error_kind
        assert expectation.message
        assert expectation.reason.strip()
        assert expectation.category in {"capability-boundary", "deliberate-error"}


def test_pdf_expectations_pin_the_typed_message_not_the_superseded_seek() -> None:
    identities = _identities()
    pdf_expectations = tuple(
        expectation
        for expectation in load_expectations(EXPECTATIONS)
        if expectation.interview in {identities[name] for name in PDF_FIXTURES}
    )

    assert len(pdf_expectations) == 16
    for expectation in pdf_expectations:
        assert expectation.error_kind == "pdf-unavailable"
        assert expectation.message in PDF_UNAVAILABLE_MESSAGE
        assert "There was a reference to a variable" not in expectation.message
        assert "ADR-0006" in expectation.reason


def test_converter_and_deliberate_errors_name_their_governing_decision() -> None:
    identities = _identities()
    by_interview = {
        expectation.interview: expectation
        for expectation in load_expectations(EXPECTATIONS)
    }

    for name in CONVERTER_FIXTURES:
        expectation = by_interview[identities[name]]
        assert expectation.error_kind == "execution"
        assert "libreoffice" in expectation.message
        assert "ADR-0001" in expectation.reason

    for name in DELIBERATE_ERROR_FIXTURES:
        expectation = by_interview[identities[name]]
        assert expectation.error_kind == "execution"
        assert "deliberately" in expectation.reason


def _expectation_result(
    expectation: Expectation,
    *,
    error_kind: str,
    message: str,
) -> CaseResult:
    return CaseResult(
        expectation.interview,
        expectation.interview.rsplit("/", 1)[-1],
        "fixture-sha256",
        expectation.phase,
        ("python",),
        2,
        0.1,
        "failure",
        error_kind,
        message,
    )


def test_issue90_expectations_reject_the_superseded_unresolved_variable_result() -> (
    None
):
    identities = _identities()
    expectations = tuple(
        expectation
        for expectation in load_expectations(EXPECTATIONS)
        if expectation.interview
        in {identities[name] for name in PDF_LINK_FIXTURES | MANUAL_PDF_FIXTURES}
    )

    for expectation in expectations:
        typed = _expectation_result(
            expectation,
            error_kind="pdf-unavailable",
            message=PDF_UNAVAILABLE_MESSAGE,
        )
        superseded = _expectation_result(
            expectation,
            error_kind="unresolved-variable",
            message=(
                "Interview has an error.  There was a reference to a variable "
                "'complaint.pdf' that could not be looked up."
            ),
        )
        assert expectation.matches(typed)
        assert not expectation.matches(superseded)


def test_all_reviewed_expectations_match_their_pinned_results() -> None:
    for expectation in _reviewed_expectations():
        result = _expectation_result(
            expectation,
            error_kind=str(expectation.error_kind),
            message=str(expectation.message),
        )
        assert expectation.matches(result)


def test_reviewed_expectations_do_not_include_unreviewed_corpus_failures() -> None:
    reviewed = {expectation.interview for expectation in _reviewed_expectations()}
    identities = _identities()
    optional_or_service_fixtures = {
        "calendar.yml",
        "get-time.yml",
        "google-sheet.yml",
        "graph.yml",
        "realtimegd.yml",
        "ml-classify.yml",
    }
    assert reviewed.isdisjoint(
        identities[name] for name in optional_or_service_fixtures
    )


@pytest.mark.parametrize("category", ["environmental", "optional-package", "unknown"])
def test_harness_rejects_unsupported_expectation_categories(
    tmp_path: Path, category: str
) -> None:
    path = tmp_path / "expectations.toml"
    path.write_text(
        "[[expectations]]\n"
        'interview = "x"\n'
        'phase = "start"\n'
        'classification = "failure"\n'
        "returncode = 2\n"
        'error_kind = "execution"\n'
        'message = "analysis"\n'
        'reason = "test"\n'
        f"category = {category!r}\n"
    )

    with pytest.raises(CorpusError, match="unsupported category"):
        load_expectations(path)
