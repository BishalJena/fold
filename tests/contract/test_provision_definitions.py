from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_PATH = REPO_ROOT / "foldos" / "provision" / "dashboards" / "foldos.json"
ALERT_PATH = REPO_ROOT / "foldos" / "provision" / "alerts" / "policy-violations.json"

DASHBOARD_SCHEMA_VERSION = "v6"
ALERT_SCHEMA_VERSION = "v2alpha1"
ALERT_VERSION = "v5"

FOLDOS_METRICS = {
    "foldos.llm.calls",
    "foldos.llm.input_tokens",
    "foldos.llm.output_tokens",
    "foldos.llm.cost_usd",
    "foldos.llm.latency_ms",
    "foldos.tool.calls",
    "foldos.policy.violations",
    "foldos.chain.head_seq",
    "foldos.chain.head_hash",
}

# Maps expected panel display names to the contract metric the panel must reference.
EXPECTED_PANELS = {
    "LLM calls": "foldos.llm.calls",
    "LLM input tokens": "foldos.llm.input_tokens",
    "LLM output tokens": "foldos.llm.output_tokens",
    "LLM cost": "foldos.llm.cost_usd",
    "LLM latency": "foldos.llm.latency_ms",
    "Tool calls": "foldos.tool.calls",
    "Tool errors": "foldos.tool.calls",
    "Policy violations": "foldos.policy.violations",
    "Chain head sequence": "foldos.chain.head_seq",
    "Chain head hash": "foldos.chain.head_hash",
}

# Panels that must group by these dimensions (names only). Relevant dimensions:
# agent/session are universal; model applies to LLM panels; tool/error to tool panels;
# policy to the policy panel; hash to the chain head hash panel.
EXPECTED_GROUP_BY = {
    "LLM calls": {"agent", "session", "model"},
    "LLM input tokens": {"agent", "session", "model"},
    "LLM output tokens": {"agent", "session", "model"},
    "LLM cost": {"agent", "session", "model"},
    "LLM latency": {"agent", "session", "model"},
    "Tool calls": {"agent", "session", "tool", "error"},
    "Tool errors": {"agent", "session", "tool"},
    "Policy violations": {"agent", "session", "policy"},
    "Chain head sequence": {"agent", "session"},
    "Chain head hash": {"agent", "session", "hash"},
}


def _load_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def _iter_metric_names(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "metricName" and isinstance(value, str):
                yield value
            else:
                yield from _iter_metric_names(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_metric_names(item)


def _panel_query_spec(panel: dict[str, Any]) -> dict[str, Any]:
    """Return the builder/composite query spec from a panel's first query."""
    queries = panel.get("spec", {}).get("queries", [])
    if not queries:
        return {}
    return queries[0].get("spec", {}).get("plugin", {}).get("spec", {})


def _builder_query_specs(query_spec: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield individual builder_query specs from a CompositeQuery or single query."""
    queries = query_spec.get("queries", [])
    if queries:
        for q in queries:
            yield q.get("spec", {})
    else:
        yield query_spec


def _group_by_names(query_spec: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for spec in _builder_query_specs(query_spec):
        for entry in spec.get("groupBy", []):
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str):
                    names.add(name)
    return names


def test_dashboard_file_exists_and_is_valid_json() -> None:
    payload = _load_json(DASHBOARD_PATH)
    assert isinstance(payload, dict)


def test_dashboard_has_required_v2_fields() -> None:
    payload = _load_json(DASHBOARD_PATH)

    assert payload.get("schemaVersion") == DASHBOARD_SCHEMA_VERSION
    assert payload.get("name") == "foldos-governance"
    assert isinstance(payload.get("tags"), list)

    spec = payload.get("spec")
    assert isinstance(spec, dict)
    assert spec.get("display", {}).get("name")
    assert isinstance(spec.get("variables"), list)
    assert isinstance(spec.get("panels"), dict)
    assert isinstance(spec.get("layouts"), list)

    for key, panel in spec["panels"].items():
        assert isinstance(key, str) and key
        assert panel.get("kind") == "Panel"
        p_spec = panel.get("spec", {})
        assert p_spec.get("display", {}).get("name")
        queries = p_spec.get("queries")
        assert isinstance(queries, list) and len(queries) == 1

    for layout in spec["layouts"]:
        assert layout.get("kind") == "Grid"


def test_dashboard_queries_reference_only_contract_metrics() -> None:
    payload = _load_json(DASHBOARD_PATH)
    names = set(_iter_metric_names(payload))
    unknown = names - FOLDOS_METRICS
    assert not unknown, f"Dashboard references non-contract metrics: {unknown}"
    missing = set(EXPECTED_PANELS.values()) - names
    assert not missing, f"Dashboard missing expected contract metrics: {missing}"


def test_dashboard_has_expected_panels_with_grouping() -> None:
    payload = _load_json(DASHBOARD_PATH)
    panels = payload["spec"]["panels"]
    by_name = {
        p["spec"]["display"]["name"]: p
        for p in panels.values()
        if p.get("kind") == "Panel"
    }

    for expected_name, expected_metric in EXPECTED_PANELS.items():
        assert expected_name in by_name, f"Missing panel: {expected_name}"
        panel = by_name[expected_name]
        metric_names = set(_iter_metric_names(panel))
        assert expected_metric in metric_names, (
            f"Panel {expected_name!r} missing metric {expected_metric!r}"
        )

        query_spec = _panel_query_spec(panel)
        group_by = _group_by_names(query_spec)
        required_group_by = EXPECTED_GROUP_BY.get(expected_name, set())
        missing_group_by = required_group_by - group_by
        assert not missing_group_by, (
            f"Panel {expected_name!r} missing required groupBy dimensions: {missing_group_by}"
        )


def test_alert_file_exists_and_is_valid_json() -> None:
    payload = _load_json(ALERT_PATH)
    assert isinstance(payload, dict)


def test_alert_has_required_v2_fields() -> None:
    payload = _load_json(ALERT_PATH)

    assert payload.get("alert") == "FoldOS policy violation"
    assert payload.get("alertType") == "METRIC_BASED_ALERT"
    assert payload.get("ruleType") == "threshold_rule"
    assert payload.get("schemaVersion") == ALERT_SCHEMA_VERSION
    assert payload.get("version") == ALERT_VERSION

    condition = payload.get("condition")
    assert isinstance(condition, dict)
    assert condition.get("selectedQueryName")
    assert condition.get("compositeQuery")
    thresholds = condition.get("thresholds")
    assert isinstance(thresholds, dict)
    assert thresholds.get("kind") == "basic"
    assert isinstance(thresholds.get("spec"), list)

    evaluation = payload.get("evaluation")
    assert isinstance(evaluation, dict)
    assert evaluation.get("kind") == "rolling"
    assert isinstance(evaluation.get("spec", {}).get("evalWindow"), str)
    assert isinstance(evaluation.get("spec", {}).get("frequency"), str)

    assert isinstance(payload.get("labels"), dict)
    assert isinstance(payload.get("annotations"), dict)
    assert isinstance(payload.get("notificationSettings"), dict)


def test_alert_queries_reference_exact_policy_violations_metric() -> None:
    payload = _load_json(ALERT_PATH)
    names = set(_iter_metric_names(payload))
    assert names == {"foldos.policy.violations"}


def test_alert_threshold_is_above_zero() -> None:
    payload = _load_json(ALERT_PATH)
    thresholds = payload["condition"]["thresholds"]
    assert thresholds.get("kind") == "basic"
    specs = thresholds.get("spec", [])
    assert specs
    found = any(
        t.get("op") == "above" and t.get("target") == 0 for t in specs
    )
    assert found, "Expected a threshold with op 'above' and target 0"
