"""Idea pre-step tests: no network. Covers the nasty-input cases — empty string,
rambling text, non-English, an idea naming a real company, pure emoji — where
every path must degrade to a sane, searchable result and never raise. Plus the
structured-intake path: founder context (industry/geography/...) must inform the
resolved domain so idea-mode discovery is targeted, not guessed."""
from app.agents.idea import IdeaDomain, idea_to_domain
from app.models import AnalyzeIdeaRequest, IdeaContext


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


# ------------------------ structured founder context ------------------------
def _ctx(**kw):
    # matches how the orchestrator carries context: a plain dict (model_dump)
    return IdeaContext(**kw).model_dump()


def test_context_geography_is_folded_into_domain():
    _, emit = collector()
    result = idea_to_domain(
        "an app for dog walkers to schedule and take payment", emit,
        context=_ctx(industry="pet services", target_geography="Ahmedabad, India"),
        complete_fn=fake_complete({"company": "PackPay", "domain": "dog walking scheduling app"}),
    )
    assert result.company == "PackPay"
    assert "ahmedabad, india" in result.domain.lower()      # geography enforced -> local rivals


def test_context_geography_is_not_duplicated():
    _, emit = collector()
    result = idea_to_domain(
        "food delivery", emit,
        context=_ctx(target_geography="India"),
        complete_fn=fake_complete({"company": "Munch", "domain": "online food delivery in India"}),
    )
    assert result.domain.lower().count("india") == 1        # idempotent, not "in India in India"


def test_context_industry_prepended_when_missing():
    _, emit = collector()
    result = idea_to_domain(
        "a thing", emit,
        context=_ctx(industry="legaltech"),
        complete_fn=fake_complete({"company": "Lexly", "domain": "contract review tools for startups"}),
    )
    assert result.domain.lower().startswith("legaltech")


def test_no_context_leaves_domain_unchanged():
    _, emit = collector()
    result = idea_to_domain(
        "a scheduling tool for barbers", emit,
        complete_fn=fake_complete({"company": "Cutly", "domain": "scheduling tools for barbershops"}),
    )
    assert result.domain == "scheduling tools for barbershops"   # backward-compatible


def test_fallback_uses_context_industry_and_geography():
    def boom(task_class, prompt, schema, emit):
        raise RuntimeError("all lanes exhausted")
    _, emit = collector()
    result = idea_to_domain(
        "😀 unparseable", emit,
        context=_ctx(industry="fintech", target_geography="Nigeria"), complete_fn=boom,
    )
    assert "fintech" in result.domain.lower() and "nigeria" in result.domain.lower()


def test_context_only_no_free_text_still_resolves():
    _, emit = collector()
    result = idea_to_domain(
        "", emit,
        context=_ctx(industry="evtol air taxis", target_geography="UAE"),
        complete_fn=fake_complete({"company": "SkyHop", "domain": "urban air mobility"}),
    )
    assert result.domain.strip() and "uae" in result.domain.lower()


def test_geography_only_empty_domain_does_not_start_with_in():
    # LLM fails + empty/meaningless idea + no industry + only geography: the domain
    # must be "Ahmedabad, India", not "in Ahmedabad, India".
    def boom(task_class, prompt, schema, emit):
        raise RuntimeError("all lanes exhausted")
    _, emit = collector()
    result = idea_to_domain("😀", emit, context=_ctx(target_geography="Ahmedabad, India"),
                            complete_fn=boom)
    assert result.domain == "Ahmedabad, India"
    assert not result.domain.lower().startswith("in ")


# ------------------------------ request models ------------------------------
def test_idea_request_optional_fields_map_to_context():
    req = AnalyzeIdeaRequest(idea="an app", industry="pet care", target_geography="Ahmedabad")
    ctx = req.to_context()
    assert ctx.industry == "pet care" and ctx.target_geography == "Ahmedabad"
    assert ctx.target_customer == "" and not ctx.is_empty()


def test_bare_idea_request_is_backward_compatible():
    assert AnalyzeIdeaRequest(idea="an app").to_context().is_empty()


def test_idea_context_strips_control_chars_and_whitespace():
    ctx = IdeaContext(industry="pet\x00care", target_geography="  India  ")
    assert ctx.industry == "petcare" and ctx.target_geography == "India"
