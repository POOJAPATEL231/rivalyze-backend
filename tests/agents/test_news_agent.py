"""News agent tests: no network. Covers source-URL grounding, the low-signal
degradation ladder (thin corpus, zero survivors, lane exhaustion), the item cap,
dedupe, and query shape."""
from datetime import datetime

from app.agents import news


def collector():
    events: list[tuple[str, str]] = []
    return events, lambda agent, msg: events.append((agent, msg))


REAL_URL = "https://techcrunch.com/2026/06/12/coda-raises-series-b"
REAL_URL_2 = "https://www.theverge.com/2026/06/20/coda-launches-ai-tables"

RICH_RESULT = {
    "title": "Coda raises $50M Series B",
    "url": REAL_URL,
    "content": "Coda announced a $50M Series B round led by a top-tier fund to expand its AI tables product " * 3,
}
RICH_RESULT_2 = {
    "title": "Coda launches AI-powered tables",
    "url": REAL_URL_2,
    "content": "Coda shipped a new AI tables feature aimed at enterprise teams managing large workflows " * 3,
}


def fake_search(results_by_query: dict[str, list[dict]]):
    def _fn(query, emit):
        return results_by_query.get(query, [])
    return _fn


def fake_complete(items: list[dict]):
    def _fn(task_class, prompt, schema, emit):
        return schema.model_validate({"items": items}), "fake-lane"
    return _fn


def month() -> str:
    return datetime.now().strftime("%B %Y")


def two_queries(c: str) -> tuple[str, str]:
    return f"{c} latest news {month()}", f"{c} product launch funding {month()}"


def test_publication_name_only_source_is_rejected():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT], q2: []})
    complete_fn = fake_complete([
        {"event": "Coda raised a round", "impact": "war chest", "source_url": "News article", "date": ""},
    ])
    events, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert result[0].items == []
    assert result[0].low_signal is True
    assert any("low signal" in msg for _, msg in events)


def test_url_not_present_in_corpus_is_rejected():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT], q2: []})
    complete_fn = fake_complete([
        {"event": "Coda raised a round", "impact": "war chest",
         "source_url": "https://example.com/made-up-link", "date": ""},
    ])
    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert result[0].items == []
    assert result[0].low_signal is True


def test_real_corpus_url_survives_and_is_preserved_verbatim():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT], q2: []})
    complete_fn = fake_complete([
        {"event": "Coda raised $50M Series B", "impact": "more capital to compete on pricing",
         "source_url": REAL_URL, "date": "2026-06-12"},
    ])
    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert result[0].low_signal is False
    assert len(result[0].items) == 1
    assert result[0].items[0].source_url == REAL_URL
    assert result[0].items[0].date == "2026-06-12"


def test_mixed_batch_keeps_only_grounded_items():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT, RICH_RESULT_2], q2: []})
    complete_fn = fake_complete([
        {"event": "Coda raised $50M", "impact": "a", "source_url": REAL_URL, "date": ""},
        {"event": "Coda ships AI tables", "impact": "b", "source_url": REAL_URL_2, "date": ""},
        {"event": "Coda invented event", "impact": "c", "source_url": "TechCrunch", "date": ""},
    ])
    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=complete_fn)
    urls = {i.source_url for i in result[0].items}
    assert urls == {REAL_URL, REAL_URL_2}


def test_thin_corpus_skips_extraction_entirely():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [{"title": "x", "url": REAL_URL, "content": "short"}], q2: []})

    def spy_complete(task_class, prompt, schema, emit):
        raise AssertionError("complete_fn must not be called for a thin corpus")

    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=spy_complete)
    assert result[0].low_signal is True
    assert result[0].items == []


def test_zero_extracted_items_is_low_signal_not_an_error():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT], q2: []})
    complete_fn = fake_complete([])
    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert result[0].low_signal is True


def test_complete_fn_runtime_error_degrades_to_low_signal_never_raises():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT], q2: []})

    def boom(task_class, prompt, schema, emit):
        raise RuntimeError("all lanes exhausted")

    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=boom)
    assert result[0].low_signal is True
    assert result[0].items == []


def test_items_capped_at_dos_ceiling():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT], q2: []})
    items = [
        {"event": f"Event {n}", "impact": "x", "source_url": REAL_URL, "date": ""}
        for n in range(14)
    ]
    complete_fn = fake_complete(items)
    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert len(result[0].items) == news._MAX_ITEMS  # keeps all up to the DoS ceiling


def test_duplicate_event_text_is_deduped_case_insensitively():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT], q2: []})
    complete_fn = fake_complete([
        {"event": "Coda raised $50M", "impact": "a", "source_url": REAL_URL, "date": ""},
        {"event": "coda raised $50m", "impact": "b", "source_url": REAL_URL, "date": ""},
    ])
    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert len(result[0].items) == 1


def test_malformed_date_is_blanked_not_dropped():
    q1, q2 = two_queries("Coda")
    search_fn = fake_search({q1: [RICH_RESULT], q2: []})
    complete_fn = fake_complete([
        {"event": "Coda raised $50M", "impact": "a", "source_url": REAL_URL, "date": "not-a-date"},
    ])
    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert len(result[0].items) == 1
    assert result[0].items[0].date == ""


def test_queries_contain_current_month_and_year():
    seen_queries = []

    def spy_search(query, emit):
        seen_queries.append(query)
        return []

    _, emit = collector()
    news.run(["Coda"], emit, search_fn=spy_search, complete_fn=fake_complete([]))
    assert seen_queries == [
        f"Coda latest news {month()}",
        f"Coda product launch funding {month()}",
        f"Coda partnership expansion strategy {month()}",
    ]


def test_malformed_search_row_missing_keys_never_raises():
    def search_fn(query, emit):
        return [{"title": "headline but no url or content key"}]

    _, emit = collector()
    result = news.run(["Coda"], emit, search_fn=search_fn, complete_fn=fake_complete([]))
    assert result[0].low_signal is True
    assert result[0].items == []


def test_search_backend_exception_is_isolated_to_one_competitor():
    def search_fn(query, emit):
        if query.startswith("Coda"):
            raise ConnectionError("network blip")
        return [RICH_RESULT]

    complete_fn = fake_complete([
        {"event": "ClickUp ships a feature", "impact": "x", "source_url": REAL_URL, "date": ""},
    ])
    _, emit = collector()
    result = news.run(["Coda", "ClickUp"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert result[0].competitor == "Coda" and result[0].low_signal is True and result[0].items == []
    assert result[1].competitor == "ClickUp" and result[1].low_signal is False
    assert len(result[1].items) == 1


def test_multiple_competitors_return_one_signal_each_in_order():
    def search_fn(query, emit):
        return [RICH_RESULT] if query.startswith("Coda") else []

    complete_fn = fake_complete([
        {"event": "raised a round", "impact": "x", "source_url": REAL_URL, "date": ""},
    ])
    _, emit = collector()
    result = news.run(["Coda", "ClickUp", "Slite"], emit, search_fn=search_fn, complete_fn=complete_fn)
    assert [r.competitor for r in result] == ["Coda", "ClickUp", "Slite"]
    assert result[0].low_signal is False
    assert result[1].low_signal is True
    assert result[2].low_signal is True
