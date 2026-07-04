"""Idea pre-step tests: no network. Covers the nasty-input cases — empty string,
rambling text, non-English, an idea naming a real company, pure emoji — where
every path must degrade to a sane, searchable result and never raise."""
from app.agents.idea import IdeaDomain, idea_to_domain


def collector():
    events: list[tuple[str, str]] = []
    return events, lambda agent, msg: events.append((agent, msg))


def fake_complete(payload: dict):
    def _fn(task_class, prompt, schema, emit):
        return schema.model_validate(payload), "fake-lane"
    return _fn


def test_happy_path_returns_coined_company_and_domain():
    events, emit = collector()
    result = idea_to_domain(
        "an AI copilot for freelance designers to manage client feedback",
        emit,
        complete_fn=fake_complete({"company": "Briefly", "domain": "AI feedback tools for freelance designers"}),
    )
    assert result.company == "Briefly"
    assert result.domain == "AI feedback tools for freelance designers"
    assert any(agent == "system" and "idea pre-step" in msg for agent, msg in events)


def test_overlong_domain_clamped_to_eight_words():
    _, emit = collector()
    long_domain = " ".join(f"word{i}" for i in range(20))
    result = idea_to_domain(
        "some idea",
        emit,
        complete_fn=fake_complete({"company": "Coined", "domain": long_domain}),
    )
    assert len(result.domain.split()) == 8
    assert result.domain == " ".join(f"word{i}" for i in range(8))


def test_empty_idea_short_circuits_without_calling_the_model():
    calls = []

    def spy_complete(task_class, prompt, schema, emit):
        calls.append(1)
        return schema.model_validate({"company": "x", "domain": "y"}), "lane"

    _, emit = collector()
    result = idea_to_domain("   ", emit, complete_fn=spy_complete)
    assert calls == []
    assert result.company == "your venture"
    assert result.domain


def test_rambling_idea_reduces_to_short_heuristic_domain_on_model_failure():
    def boom(task_class, prompt, schema, emit):
        raise RuntimeError("all lanes exhausted")

    _, emit = collector()
    rambling = ("so basically I really just want to build a platform for the "
                "the the connecting local urban beekeepers with restaurants "
                "that want fresh honey and pollination services nearby")
    result = idea_to_domain(rambling, emit, complete_fn=boom)
    assert result.company == "your venture"
    assert 1 <= len(result.domain.split()) <= 6
    assert "the" not in result.domain.split()
    assert "basically" not in result.domain.split()


def test_non_english_or_symbol_heavy_idea_never_raises():
    def boom(task_class, prompt, schema, emit):
        raise ValueError("schema-fail")

    _, emit = collector()
    result = idea_to_domain("😀🚀💡 一个用于宠物的应用", emit, complete_fn=boom)
    assert isinstance(result, IdeaDomain)
    assert result.company == "your venture"
    assert result.domain


def test_idea_naming_a_real_company_does_not_crash_and_still_degrades_safely():
    def boom(task_class, prompt, schema, emit):
        raise RuntimeError("lane exhausted")

    _, emit = collector()
    result = idea_to_domain("basically another Notion but for lawyers", emit, complete_fn=boom)
    assert result.company == "your venture"
    assert "notion" in result.domain or "lawyers" in result.domain


def test_blank_company_from_model_is_coerced_to_your_venture():
    _, emit = collector()
    result = idea_to_domain(
        "an idea",
        emit,
        complete_fn=fake_complete({"company": "   ", "domain": "generic market description here"}),
    )
    assert result.company == "your venture"


def test_model_returning_empty_domain_triggers_fallback():
    _, emit = collector()
    result = idea_to_domain(
        "a scheduling tool for barbers",
        emit,
        complete_fn=fake_complete({"company": "Cutly", "domain": "   "}),
    )
    assert result.domain.strip() != ""
    assert result.company == "your venture"


def test_wrapped_quotes_and_fences_are_stripped():
    _, emit = collector()
    result = idea_to_domain(
        "an idea",
        emit,
        complete_fn=fake_complete({"company": "\"Coined\"", "domain": "```scheduling tools for small barbershops```"}),
    )
    assert result.company == "Coined"
    assert "`" not in result.domain and '"' not in result.domain
