"""Machine-readable answer dispositions and screen guidance."""

from docassemble_simulator.execution import _apply_assignments, _unknown_field_details


def test_assignments_report_applied_unknown_and_parse_error():
    screen = {
        "fields": [
            {"variable": "name", "type": "text"},
            {"variable": "filing_date", "type": "date"},
        ]
    }
    namespace = {}
    dispositions = []

    errors = _apply_assignments(
        namespace,
        screen,
        (("name", "Ada"), ("nmae", "typo"), ("filing_date", "not-a-date")),
        False,
        allowed={"name", "filing_date"},
        dispositions=dispositions,
    )

    assert namespace["name"] == "Ada"
    assert [item["disposition"] for item in dispositions] == [
        "applied",
        "unknown field",
        "parse error",
    ]
    assert dispositions[1]["input"] == "typo"
    assert dispositions[2]["input"] == "not-a-date"
    assert any("filing_date" in error for error in errors)
    details = _unknown_field_details(screen, dispositions)
    assert details["active_fields"] == ["name", "filing_date"]
    assert details["closest_matches"]["nmae"] == "name"


def test_unknown_field_details_keep_active_screen_examples():
    screen = {
        "fields": [
            {
                "variable": "topics",
                "type": "checkboxes",
                "example": 'topics={"housing":true}',
            }
        ]
    }
    dispositions = [
        {"variable": "topic", "input": "{}", "disposition": "unknown field"}
    ]

    details = _unknown_field_details(screen, dispositions)

    assert details["active_fields"] == ["topics"]
    assert details["active_field_details"][0]["example"] == 'topics={"housing":true}'
