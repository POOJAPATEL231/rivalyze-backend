"""Evidence grounding: only corpus-present URLs survive — an invented URL, even a
well-formed https one, is dropped so every evidence row is a real source."""
from app.core.grounding import corpus_urls, ground_sources

CORPUS = (
    "Coda raises Series B\nsome body text\nSOURCE: https://techcrunch.com/coda-b\n\n"
    "Coda vs Notion\nmore text\nSOURCE: https://theverge.com/coda-notion\n"
)


def test_corpus_urls_extracts_source_lines():
    assert corpus_urls(CORPUS) == {
        "https://techcrunch.com/coda-b",
        "https://theverge.com/coda-notion",
    }


def test_invented_url_is_dropped():
    got = ground_sources(
        ["https://techcrunch.com/coda-b", "https://example.com/made-up"], CORPUS)
    assert got == ["https://techcrunch.com/coda-b"]


def test_non_http_and_dupes_dropped_order_preserved():
    got = ground_sources(
        ["not-a-url", "https://theverge.com/coda-notion",
         "https://techcrunch.com/coda-b", "https://theverge.com/coda-notion"], CORPUS)
    assert got == ["https://theverge.com/coda-notion", "https://techcrunch.com/coda-b"]


def test_empty_and_none_safe():
    assert ground_sources([], CORPUS) == []
    assert ground_sources(None, CORPUS) == []
    assert ground_sources(["https://techcrunch.com/coda-b"], "") == []
