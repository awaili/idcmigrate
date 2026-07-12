"""Pure helper tests (no DB, no network)."""
from idc.cli.main import _parse_overrides


def test_parse_overrides_basic():
    assert _parse_overrides(["10.0.4.20=${DB_HOST}", "hunter2=${DB_PASSWORD}"]) == {
        "10.0.4.20": "${DB_HOST}", "hunter2": "${DB_PASSWORD}"}


def test_parse_overrides_value_can_contain_equals():
    # split on first '=' only: value may itself contain '='
    assert _parse_overrides(["conn=url?a=1=b"]) == {"conn": "url?a=1=b"}


def test_parse_overrides_skips_malformed_and_blank():
    assert _parse_overrides(["nope", "", " =x", "k="]) == {"k": ""}


def test_parse_overrides_none_safe():
    assert _parse_overrides(None) == {}