"""data_store.py: where the private data comes from."""

import pytest

import data_store
from data_store import DataUnavailable, parse_source


@pytest.mark.parametrize("url,expected", [
    ("hf://buckets/alice/daily-data", ("bucket", "alice/daily-data", "")),
    ("https://huggingface.co/buckets/alice/daily-data/v1/", ("bucket", "alice/daily-data", "v1")),
    ("https://huggingface.co/datasets/alice/news", ("dataset", "alice/news", "")),
    ("hf://datasets/alice/news/tree/main/sub", ("dataset", "alice/news", "sub")),
])
def test_parse_source(url, expected):
    assert parse_source(url) == expected


def test_parse_source_rejects_other_urls():
    with pytest.raises(ValueError):
        parse_source("https://drive.google.com/x")


def test_sync_is_a_no_op_when_data_is_present(tmp_path):
    (tmp_path / "news-data").mkdir()
    (tmp_path / "news-data" / "a.jsonl").write_text("{}\n")
    assert data_store.sync(tmp_path, url="") == tmp_path


def test_sync_explains_what_to_set_when_nothing_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_DATA_URL", raising=False)
    with pytest.raises(DataUnavailable, match="HF_DATA_URL"):
        data_store.sync(tmp_path, url="")
