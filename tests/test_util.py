import pytest

from streamclipper.util import ToolError, parse_time, utf16_len


@pytest.mark.parametrize(
    "value, expected",
    [("75", 75.0), ("75.5", 75.5), (75, 75.0), ("1:15", 75.0), ("01:02:03", 3723.0), (" 0:05 ", 5.0)],
)
def test_parse_time_forms(value, expected):
    assert parse_time(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["abc", "1:2:3:4", "-5", "1:-2", ""])
def test_parse_time_rejects_bad_values(value):
    with pytest.raises(ToolError):
        parse_time(value)


def test_utf16_len_ascii():
    assert utf16_len("WAIT FOR IT") == 11


def test_utf16_len_emoji_counts_surrogate_pairs():
    assert utf16_len("🔥") == 2
    assert utf16_len("GG 💀🔥") == 7


def test_utf16_len_devanagari():
    # न म स ् त े : six BMP code points, one UTF-16 unit each
    assert utf16_len("नमस्ते") == 6
