"""Keep F7 runtime payload parity strict outside Markdown delimiter rows."""

import pytest
from test_real_runtime import _clean_table_row_payload


@pytest.mark.parametrize(
    "original, different",
    [
        ("-|-", "---|---"),
        ("--|--", "---|---"),
        ("A sentence containing -|-", "A sentence containing ---|---"),
    ],
)
def test_table_payload_preserves_non_delimiter_text(original, different):
    assert _clean_table_row_payload(original) == original
    with pytest.raises(AssertionError):
        assert _clean_table_row_payload({"subquestion_text": original}) == (
            _clean_table_row_payload({"subquestion_text": different})
        )


def test_table_payload_normalizes_only_delimiters_and_cleans_none_keys():
    payload = {
        "subquestion_text": "Name | Age\n:------- | ---:\nAda | 42",
        "fields": [{"variable": "reviewed", "default": None}],
        "absent": None,
    }
    assert _clean_table_row_payload(payload) == {
        "subquestion_text": "Name | Age\n:--- | ---:\nAda | 42",
        "fields": [{"variable": "reviewed"}],
    }
    changed = {**payload, "fields": [{"variable": "other"}]}
    assert _clean_table_row_payload(payload) != _clean_table_row_payload(changed)
