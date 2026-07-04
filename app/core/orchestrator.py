"""LangGraph orchestrator: the pipeline spine.

Topology: START -> discovery -> [news | product | review] in parallel -> merge ->
strategist -> validate -> END. Agents are injected as (state, emit) -> partial
state dict functions, so independently built agents plug in without edits through
the adapter factories at the bottom.

Guarantees:
  - Parallel branches write only to fields declared with an additive reducer
    (Annotated[list[X], operator.add]); each returns deltas only, never an
    accumulated list.
  - Every node runs inside a boundary that validates its output. An exception or a
    bad value becomes an emitted event, a typed-empty substitute, and one
    low_signal finding. The graph never raises to the caller.
  - validate grants the strategist exactly one repair retry, then degrades the
    report to None with a typed finding rather than looping.
"""
from __future__ import annotations

import inspect
import operator
from typing import (TYPE_CHECKING, Annotated, Any, Callable, Mapping, Optional,
                    TypedDict, get_args, get_origin)

from langgraph.graph import END, START, StateGraph

from app.models import (Competitor, CompetitiveReport, NewsSignals, ProductIntel,
                        SentimentIntel, UnifiedSignals)

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

EmitFn = Callable[[str, str], None]
NodeFn = Callable[[dict, EmitFn], dict]


class PipelineState(TypedDict, total=False):
    company: str
    domain: str
    idea: Optional[str]
    run_id: str
    competitors: list[Competitor]
    news_results: Annotated[list[NewsSignals], operator.add]
    product_results: Annotated[list[ProductIntel], operator.add]
    review_results: Annotated[list[SentimentIntel], operator.add]
    unified: Optional[UnifiedSignals]
    report: Optional[CompetitiveReport]
    low_signal_findings: Annotated[list[str], operator.add]


NODE_OUTPUT_SPEC: dict[str, dict[str, Any]] = {
    "discovery":  {"competitors": list[Competitor]},
    "news":       {"news_results": list[NewsSignals]},
    "product":    {"product_results": list[ProductIntel]},
    "review":     {"review_results": list[SentimentIntel]},
    "merge":      {"unified": UnifiedSignals},
    "strategist": {"report": CompetitiveReport},
}

GATHER_NODES = ("news", "product", "review")
REQUIRED_AGENTS = ("discovery", "news", "product", "review", "merge", "strategist")

_EMIT_LANE = {"review": "reviews"}

_INVALID = object()


def _lane(node: str) -> str:
    return _EMIT_LANE.get(node, node)


def _typed_empty(expected: Any) -> Any:
    """[] for list keys; a defaulted instance where the model allows one
    (UnifiedSignals()); None where it does not (report=None)."""
    if get_origin(expected) is list:
        return []
    try:
        return expected()
    except Exception:
        return None


def _validated(value: Any, expected: Any, node: str, key: str, emit: EmitFn) -> Any:
    """Coerce one returned value to its contract type, or _INVALID. Lists are
    checked item by item, so one malformed item costs that item, not the whole
    branch. Single values are all-or-nothing."""
    lane = _lane(node)
    if get_origin(expected) is list:
        (item_model,) = get_args(expected)
        if not isinstance(value, list):
            emit(lane, f"low signal: {node} returned {type(value).__name__} for {key}, expected list")
            return _INVALID
        kept = []
        for i, item in enumerate(value):
            try:
                kept.append(item if isinstance(item, item_model)
                            else item_model.model_validate(item))
            except Exception:
                emit(lane, f"{node}: dropped invalid {item_model.__name__} at {key}[{i}]")
        return kept
    try:
        return value if isinstance(value, expected) else expected.model_validate(value)
    except Exception:
        emit(lane, f"low signal: {node} returned invalid {expected.__name__} for {key}")
        return _INVALID


def _degraded_delta(node: str, spec: dict[str, Any], exc: Exception) -> dict:
    """The substitute a raising node produces: typed-empty for every key it owns,
    plus one low_signal finding naming the failure."""
    out: dict = {k: _typed_empty(t) for k, t in spec.items()}
    out["low_signal_findings"] = [
        f"{node}: agent failed ({type(exc).__name__}) — typed-empty substituted"]
    return out


def _apply_spec(raw: dict, spec: dict[str, Any], node: str, emit: EmitFn) -> dict:
    """Validate a node's returned dict key by key: agent-reported
    low_signal_findings pass through as findings, unknown keys pass through
    untouched, and spec'd keys are validated and replaced with a typed-empty
    value plus a finding on failure."""
    out: dict = {}
    findings: list[str] = []
    for key, value in raw.items():
        if key == "low_signal_findings":
            if isinstance(value, list):
                findings.extend(str(v) for v in value)
        elif key not in spec:
            out[key] = value
        else:
            checked = _validated(value, spec[key], node, key, emit)
            if checked is _INVALID:
                out[key] = _typed_empty(spec[key])
                findings.append(f"{node}: invalid {key} at the boundary — typed-empty substituted")
            else:
                out[key] = checked
    if findings:
        out["low_signal_findings"] = findings
    return out


def _boundary(node: str, fn: NodeFn, emit: EmitFn) -> Callable[[dict], dict]:
    """Wrap an injected agent so nothing it does can take down the graph. An
    exception or a bad shape becomes an event, a typed-empty substitute, and one
    finding; unknown keys and agent-reported findings pass through."""
    spec = NODE_OUTPUT_SPEC.get(node, {})
    lane = _lane(node)

    def guarded(state: dict) -> dict:
        emit(lane, "node start")
        try:
            raw = fn(dict(state), emit)
            raw = {} if raw is None else raw
            if not isinstance(raw, dict):
                raise TypeError(f"node returned {type(raw).__name__}, expected dict")
        except Exception as exc:
            emit(lane, f"low signal: {node} raised {type(exc).__name__}: {exc}")
            emit(lane, "node done · degraded")
            return _degraded_delta(node, spec, exc)
        out = _apply_spec(raw, spec, node, emit)
        emit(lane, "node done")
        return out

    return guarded


def _report_problems(report: Any) -> list[str]:
    """The honesty gate: a report that looks like model debris (fences, raw JSON)
    or lacks its summary or date never reaches the UI."""
    if report is None:
        return ["report missing"]
    if not isinstance(report, CompetitiveReport):
        return [f"report is {type(report).__name__}, not CompetitiveReport"]
    problems = []
    summary = report.executive_summary.strip()
    if not summary:
        problems.append("empty executive_summary")
    if "```" in summary:
        problems.append("markdown fence in executive_summary")
    if summary.startswith(("{", "[")) or '{"' in summary:
        problems.append("JSON-looking executive_summary")
    if not report.analysis_date.strip():
        problems.append("analysis_date not set")
    return problems


def _validate_node(strategist_fn: NodeFn, emit: EmitFn) -> Callable[[dict], dict]:
    """Sanity-check the report and grant exactly one strategist repair retry. The
    retry re-invokes the strategist with _repair_attempt set; a throttled or
    unavailable lane fails over on the second try, while a stable-lane result is
    unlikely to change at low temperature — so one retry is the honest bound, then
    the report degrades to None with a finding rather than looping."""
    def validate(state: dict) -> dict:
        problems = _report_problems(state.get("report"))
        if not problems:
            emit("system", "validate: report passed sanity checks")
            return {}
        emit("system", f"validate: report failed ({'; '.join(problems)}) · one repair retry")
        emit("strategist", "repair retry — a second synthesis attempt (router may fail over to another lane)")
        repaired: Optional[CompetitiveReport] = None
        try:
            raw = strategist_fn({**dict(state), "_repair_attempt": True}, emit)
            candidate = (raw or {}).get("report")
            if candidate is not None:
                repaired = (candidate if isinstance(candidate, CompetitiveReport)
                            else CompetitiveReport.model_validate(candidate))
        except Exception as exc:
            emit("strategist", f"low signal: repair retry raised {type(exc).__name__}")
        remaining = _report_problems(repaired)
        if not remaining:
            emit("system", "validate: repaired report accepted")
            return {"report": repaired}
        emit("system", f"validate: still failing ({'; '.join(remaining)}) · report degraded to None")
        return {"report": None,
                "low_signal_findings": [
                    "strategist: report failed validation after one repair retry — "
                    "degraded run (report=None)"]}
    return validate


def build_graph(agents: Mapping[str, NodeFn], emit: EmitFn) -> "CompiledStateGraph":
    """Compile the pipeline. Raises only on a missing agent key at build time; the
    compiled graph itself never raises, and run_pipeline owns even this guard."""
    missing = sorted(set(REQUIRED_AGENTS) - set(agents))
    if missing:
        raise ValueError(f"agents mapping missing required keys: {', '.join(missing)}")

    builder = StateGraph(PipelineState)
    for node in REQUIRED_AGENTS:
        builder.add_node(node, _boundary(node, agents[node], emit))
    builder.add_node("validate", _validate_node(agents["strategist"], emit))

    builder.add_edge(START, "discovery")
    for branch in GATHER_NODES:
        builder.add_edge("discovery", branch)
    builder.add_edge(list(GATHER_NODES), "merge")
    builder.add_edge("merge", "strategist")
    builder.add_edge("strategist", "validate")
    builder.add_edge("validate", END)
    return builder.compile()


def discovery_node(run_fn: Callable[..., Any]) -> NodeFn:
    """Adapt a discovery run function into a graph node.

    Signature-robust: it binds to both run(company, domain, run_id, emit) and
    run(company, domain, emit), detecting run_id in the callable's parameters once
    at wire time and passing it only when accepted. It is a no-op when the state
    already carries confirmed competitors, so a completed confirm step is never
    silently re-run."""
    takes_run_id = "run_id" in inspect.signature(run_fn).parameters

    def node(state: dict, emit: EmitFn) -> dict:
        if state.get("competitors"):
            emit("discovery", "competitors already confirmed · discovery skipped")
            return {}
        args = (state.get("company", ""), state.get("domain", ""))
        if takes_run_id:
            args += (state.get("run_id", ""),)
        result = run_fn(*args, emit)
        competitor_set = result[0] if isinstance(result, tuple) else result
        return {"competitors": list(competitor_set.competitors)}
    return node


def gather_node(kind: str, run_fn: Callable[..., Any]) -> NodeFn:
    """Adapt a gathering agent run(competitors: list[str], emit[, company]) ->
    list[Model]. kind is one of {news, product, review}; output lands only in the
    branch's own additive-reducer field, so parallel siblings never clobber each
    other. The company name is passed to agents whose signature accepts it,
    detected once at wire time."""
    if kind not in GATHER_NODES:
        raise ValueError(f"kind must be one of {GATHER_NODES}, got {kind!r}")
    key = f"{kind}_results"
    takes_company = "company" in inspect.signature(run_fn).parameters

    def node(state: dict, emit: EmitFn) -> dict:
        names = [c.name for c in state.get("competitors", [])]
        if takes_company:
            return {key: run_fn(names, emit, company=state.get("company", ""))}
        return {key: run_fn(names, emit)}
    return node


def strategist_node(run_fn: Callable[..., Any], confidence_fn: Callable[..., float]) -> NodeFn:
    """Adapt run(unified, company, confidence_fn, emit) -> CompetitiveReport.
    confidence_fn is injected here so every confidence can be recomputed from cited
    evidence; model-asserted numbers are discarded."""
    def node(state: dict, emit: EmitFn) -> dict:
        unified = state.get("unified") or UnifiedSignals()
        return {"report": run_fn(unified, state.get("company", ""), confidence_fn, emit)}
    return node


def _resolve_idea(state: dict, emit: EmitFn) -> dict:
    """Turn idea-mode into company-mode with one pre-step call, importing the idea
    module lazily and falling back to a heuristic if it is unavailable."""
    try:
        from app.agents.idea import idea_to_domain
        resolved = idea_to_domain(state["idea"], emit)
        company, domain = resolved.company, resolved.domain
    except Exception as exc:
        emit("system", f"idea pre-step unavailable ({type(exc).__name__}) · heuristic fallback")
        company, domain = "your venture", " ".join(str(state["idea"]).split()[:6])
    emit("system", f'idea resolved · company="{company}" · domain="{domain}"')
    return {**state, "company": company, "domain": domain or state.get("domain", "")}


def run_pipeline(state: dict, agents: Mapping[str, NodeFn], emit: EmitFn) -> dict:
    """The (single-pass) lifecycle entry point. Never raises: any failure in the
    idea pre-step, graph build, or graph run degrades to a state dict with
    report=None plus a typed finding, so the run finishes and the poller never
    sees an error. Retained for the one-shot path; the two-phase flow uses
    run_discovery / run_analysis below."""
    state = dict(state)
    try:
        if state.get("idea") and not state.get("company"):
            state = _resolve_idea(state, emit)
        graph = build_graph(agents, emit)
        return dict(graph.invoke(state))
    except Exception as exc:
        emit("system", f"pipeline degraded at the outer boundary: {type(exc).__name__}: {exc}")
        findings = list(state.get("low_signal_findings", []))
        findings.append(f"pipeline: {type(exc).__name__} at the outer boundary — degraded run")
        return {**state, "report": None, "low_signal_findings": findings}


# ============================ two-phase graphs ============================
# The two-phase flow (Rivalyze_TwoPhase_Pipeline.md) compiles TWO graphs over the
# SAME PipelineState, split at the human-approval gate. Phase 1 runs discovery only
# and parks at awaiting_confirmation; Phase 2 fans out the gathering agents on the
# user-confirmed competitor list. Both reuse the identical _boundary / _validate_node
# / adapter machinery above — only the edge topology differs.

_ANALYSIS_AGENTS = ("news", "product", "review", "merge", "strategist")


def build_discovery_graph(agents: Mapping[str, NodeFn], emit: EmitFn) -> "CompiledStateGraph":
    """Phase 1: START -> discovery -> END. Requires only the 'discovery' agent."""
    if "discovery" not in agents:
        raise ValueError("agents mapping missing required key: discovery")
    builder = StateGraph(PipelineState)
    builder.add_node("discovery", _boundary("discovery", agents["discovery"], emit))
    builder.add_edge(START, "discovery")
    builder.add_edge("discovery", END)
    return builder.compile()


def build_analysis_graph(agents: Mapping[str, NodeFn], emit: EmitFn) -> "CompiledStateGraph":
    """Phase 2: START -> [news | product | review] -> merge -> strategist -> validate
    -> END. No discovery node — the confirmed competitors are seeded into state."""
    missing = sorted(set(_ANALYSIS_AGENTS) - set(agents))
    if missing:
        raise ValueError(f"agents mapping missing required keys: {', '.join(missing)}")
    builder = StateGraph(PipelineState)
    for node in _ANALYSIS_AGENTS:
        builder.add_node(node, _boundary(node, agents[node], emit))
    builder.add_node("validate", _validate_node(agents["strategist"], emit))
    for branch in GATHER_NODES:
        builder.add_edge(START, branch)
    builder.add_edge(list(GATHER_NODES), "merge")
    builder.add_edge("merge", "strategist")
    builder.add_edge("strategist", "validate")
    builder.add_edge("validate", END)
    return builder.compile()


def run_discovery(state: dict, agents: Mapping[str, NodeFn], emit: EmitFn) -> dict:
    """Phase 1 entry point. Resolves idea-mode, runs discovery only, never raises."""
    state = dict(state)
    try:
        if state.get("idea") and not state.get("company"):
            state = _resolve_idea(state, emit)
        graph = build_discovery_graph(agents, emit)
        return dict(graph.invoke(state))
    except Exception as exc:  # noqa: BLE001 — the poller must never see an error
        emit("system", f"discovery degraded at the outer boundary: {type(exc).__name__}: {exc}")
        findings = list(state.get("low_signal_findings", []))
        findings.append(f"discovery: {type(exc).__name__} at the outer boundary — degraded run")
        return {**state, "competitors": state.get("competitors", []), "low_signal_findings": findings}


def run_analysis(state: dict, agents: Mapping[str, NodeFn], emit: EmitFn) -> dict:
    """Phase 2 entry point. Seeds the additive-reducer lanes so the fan-out from
    START accumulates cleanly, runs the analysis graph, never raises."""
    state = dict(state)
    for lane in ("news_results", "product_results", "review_results", "low_signal_findings"):
        state.setdefault(lane, [])
    try:
        graph = build_analysis_graph(agents, emit)
        return dict(graph.invoke(state))
    except Exception as exc:  # noqa: BLE001
        emit("system", f"analysis degraded at the outer boundary: {type(exc).__name__}: {exc}")
        findings = list(state.get("low_signal_findings", []))
        findings.append(f"analysis: {type(exc).__name__} at the outer boundary — degraded run")
        return {**state, "report": None, "low_signal_findings": findings}
