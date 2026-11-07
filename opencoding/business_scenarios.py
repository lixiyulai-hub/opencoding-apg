"""Explicit, sourced user journeys using the existing fact/adoption boundary."""
from copy import deepcopy
import json

from .documents import _validate_scenarios
from .facts import add_fact, list_facts
from .safety import canonical_json, sha256_bytes
from .sessions import read_session_snapshot

SCOPE = "opencoding.business-scenarios.v1"


def requirement_digest(session):
    return sha256_bytes(canonical_json({"goal": session["goal"], "requirements": session["requirements"]}))


def confirm_scenarios(root, session_id, scenarios):
    """Called only after explicit user confirmation; never infer confirmation from prose."""
    _validate_scenarios(scenarios)
    if not scenarios:
        raise ValueError("confirm at least one business scenario")
    session = read_session_snapshot(root, session_id)
    previous = [f for f in list_facts(root, session_id) if f["scope"] == SCOPE]
    payload = {"requirement_digest": requirement_digest(session), "scenarios": scenarios}
    if previous:
        prior = json.loads(previous[-1]["content"])
        if all(prior.get(k) == v for k, v in payload.items()):
            return previous[-1]
    payload["supersedes"] = previous[-1]["fact_id"] if previous else None
    return add_fact(root, session_id, content=json.dumps(payload, ensure_ascii=False),
                    source_type="user", confirmed=True, source_ref="confirmed-business-scenarios", scope=SCOPE)


def apply_scenario_facts(recommendation, session, facts):
    candidates = [f for f in facts or [] if f.get("scope") == SCOPE and f.get("confirmed") is True and f.get("source_type") == "user"]
    if not candidates:
        return recommendation, []
    fact = candidates[-1]
    payload = json.loads(fact["content"])
    _validate_scenarios(payload["scenarios"])
    current = payload["requirement_digest"] == requirement_digest(session)
    result = deepcopy(recommendation)
    if current:
        result["project"]["scenarios"] = deepcopy(payload["scenarios"])
    else:
        result["status"] = "draft"
        result["unresolved"].append("business_scenarios:requirements_changed")
    trace = {"fact_id": fact["fact_id"], "kind": "business_scenarios", "source_type": "user",
             "applied": current, "effect": "confirmed journeys" if current else "reconfirmation required",
             "explanation": "场景绑定到已确认的需求摘要；不把历史场景当作新需求的授权。"}
    return result, [trace]
