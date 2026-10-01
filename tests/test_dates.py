import pytest

from paperpress.dates import parse_date


@pytest.mark.parametrize("text,expected", [
    ("1870-04-07", ("1870-04-07", "day")),
    ("1912-02-17T00:00:00Z", ("1912-02-17", "day")),
    ("The Revolution - April 7, 1870", ("1870-04-07", "day")),
    ("The Suffragist  1914-09-05: Vol 2 Iss 36", ("1914-09-05", "day")),
    ("Saturday, 5th September 1914", ("1914-09-05", "day")),
    ("Sept. 5, 1914", ("1914-09-05", "day")),
    ("1915-05", ("1915-05-01", "month")),
    ("May 1915", ("1915-05-01", "month")),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", [None, "", "1913", "The Suffragist", "Vol 1 Iss 4",
                                  "1914-02-30"])
def test_no_usable_date(text):
    assert parse_date(text) is None
