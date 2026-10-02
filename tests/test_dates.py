import pytest

from paperpress.dates import (DateFormatError, date_from_filename, infer_filename_format,
                              parse_date)


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


def test_date_from_filename():
    assert date_from_filename("dth_sn92073228_03041960.pdf", "MMDDYYYY") == "1960-03-04"
    assert date_from_filename("dth_sn92073228_03041960.pdf", "DDMMYYYY") == "1960-04-03"
    assert date_from_filename("nw_1921-02-12.pdf", "YYYY-MM-DD") == "1921-02-12"
    assert date_from_filename("nw_1921_02_12.pdf", "YYYY-MM-DD") == "1921-02-12"
    # the 8-digit LCCN must not be read as a date
    assert date_from_filename("dth_sn92073228.pdf", "YYYYMMDD") is None


def test_infer_filename_format():
    dth = ["dth_sn92073228_03041960.pdf", "dth_sn92073228_09251960.pdf",
           "dth_sn92068245_01142008.pdf"]
    assert infer_filename_format(dth) == "MMDDYYYY"          # 25 can't be a month
    assert infer_filename_format(["a_19210212.pdf", "a_19210219.pdf"]) == "YYYYMMDD"
    assert infer_filename_format(["x 1921-02-12.pdf"]) == "YYYY-MM-DD"


def test_infer_filename_format_ambiguous_or_missing():
    with pytest.raises(DateFormatError, match=r"'p_03041960.pdf' could be 1960-03-04 "
                                              r"\(MMDDYYYY\) or 1960-04-03 \(DDMMYYYY\)"):
        infer_filename_format(["p_03041960.pdf", "p_01021961.pdf"])
    with pytest.raises(DateFormatError, match="can't find a date"):
        infer_filename_format(["p_03041960.pdf", "notes.pdf"])
    with pytest.raises(DateFormatError, match="different date formats"):
        infer_filename_format(["p_19600304.pdf", "p_03251961.pdf"])
