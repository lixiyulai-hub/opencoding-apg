"""Offline W5 synthetic beginner/platform acceptance matrix.

The matrix is a reviewable contract around existing intake, recommendation and
target-adapter facts. It does not execute a target, connector, provider or
filesystem write. A separate product-loop executor is required for reviewed
local actions; this record keeps preview, waves, evidence and rollback scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Mapping

from .decisions import build_recommendation
from .evidence_boundary import build_read_only_audit_snapshot, platform_compatibility_declaration
from .host_capabilities import build_capability_matrix, normalize_target_platform
from .intake import QUESTION_DEFINITIONS, answer_question, new_session
from .safety import canonical_json, inspect_sensitive, sha256_bytes
from .target_adapters import TargetAdapterError, target_adapter_status

MATRIX_SCHEMA = "opencoding-w5-synthetic-acceptance-v1"
SUPPORTED_TARGET_LABELS = ("windows", "macos", "ios", "android", "web", "mini-program", "cli")
CAPABILITY_IDS = ("server", "database", "api", "auth", "payment", "notifications", "admin", "storage")
WAVE_IDS = ("clarify", "plan", "preview", "execute", "verify", "rollback", "review")


def _digest(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


class W5AcceptanceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SyntheticBeginnerScenario:
    """A fixture-labelled sentence and complete answers; never a user interview."""

    scenario_id: str
    sentence: str
    answers: Mapping[str, str]
    expected_platform: str

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id or len(self.scenario_id) > 64:
            raise W5AcceptanceError("scenario_id_invalid", "scenario id is invalid")
        if not isinstance(self.sentence, str) or not self.sentence.strip() or len(self.sentence) > 2000:
            raise W5AcceptanceError("sentence_invalid", "synthetic sentence is required and bounded")
        if inspect_sensitive(self.sentence)["sensitive"]:
            raise W5AcceptanceError("sentence_sensitive", "synthetic sentence contains sensitive material")
        if not isinstance(self.answers, Mapping) or set(self.answers) != {item["id"] for item in QUESTION_DEFINITIONS}:
            raise W5AcceptanceError("answers_incomplete", "answers must cover every clarification question")
        if any(not isinstance(value, str) or not value.strip() for value in self.answers.values()):
            raise W5AcceptanceError("answers_invalid", "fixture answers must be non-empty strings")
        if any(inspect_sensitive(value)["sensitive"] for value in self.answers.values()):
            raise W5AcceptanceError("answers_sensitive", "fixture answers contain sensitive material")
        if not isinstance(self.expected_platform, str) or not self.expected_platform.strip():
            raise W5AcceptanceError("platform_invalid", "expected platform is required")

    def recommendation(self) -> dict[str, Any]:
        session = new_session(self.sentence)
        for question in QUESTION_DEFINITIONS:
            session = answer_question(session, question["id"], self.answers[question["id"]])
        return build_recommendation(session)


DEFAULT_SCENARIOS = (
    SyntheticBeginnerScenario(
        "cli-local", "我想要一个离线命令行清单，记录和查看待办事项。",
        {"audience": "我自己", "outcome": "记录和查看待办事项", "platform": "命令行",
         "data_persistence": "需要", "cross_device": "不需要", "file_storage": "不需要",
         "external_data": "不需要", "admin_access": "不需要", "account_access": "不需要",
         "notifications": "不需要", "payments": "不需要", "multi_user": "不需要"}, "cli"),
    SyntheticBeginnerScenario(
        "web-collaboration", "我想做一个网页，让团队同步任务、上传附件并提醒成员。",
        {"audience": "小团队成员", "outcome": "团队同步任务、上传附件并提醒成员", "platform": "网页",
         "data_persistence": "需要", "cross_device": "需要", "file_storage": "需要",
         "external_data": "不需要", "admin_access": "需要", "account_access": "需要",
         "notifications": "需要", "payments": "不需要", "multi_user": "需要"}, "web"),
    SyntheticBeginnerScenario(
        "ambiguous-platform", "我想在 Windows 和 Mac 上使用一个家庭记录工具。",
        {"audience": "家庭成员", "outcome": "记录和查看家庭事项", "platform": "Windows 和 Mac",
         "data_persistence": "需要", "cross_device": "不需要", "file_storage": "不需要",
         "external_data": "不需要", "admin_access": "不需要", "account_access": "不需要",
         "notifications": "不需要", "payments": "不需要", "multi_user": "不需要"}, "windows"),
)


def _platform_status(label: str) -> dict[str, Any]:
    normalized = normalize_target_platform(label)
    if not normalized["recognized"]:
        declaration = platform_compatibility_declaration(
            normalized["requested"], reason="目标平台标签未识别，不能静默选择平台"
        )
        return {"target": normalized, "adapter": None, "status": "unverified",
                "execution": {"status": "unverified", "observed": False, "attempted": False,
                               "reason": "目标平台标签未识别，不能静默选择平台"}, "contract_only": True,
                "evidence_class": "unverified", "compatibility": declaration}
    if normalized["normalized"] in {"windows", "macos", "web"}:
        try:
            report = target_adapter_status(normalized["normalized"], host_family="linux")
            report["evidence_class"] = "unverified"
            report["compatibility"] = platform_compatibility_declaration(
                normalized["normalized"], status=report["status"],
                execution_observed=report["execution"]["observed"],
                toolchain_status=report.get("toolchain", {}).get("status", "unverified"),
                host_family=report.get("host", {}).get("family"),
                reason=report["execution"].get("reason", ""),
            )
            return report
        except TargetAdapterError as error:
            return {"target": normalized, "adapter": None, "status": "blocked",
                    "execution": {"status": "blocked", "observed": False, "attempted": False,
                                   "reason": str(error)}, "contract_only": True,
                    "evidence_class": "unverified",
                    "compatibility": platform_compatibility_declaration(
                        normalized["normalized"], status="blocked", reason=str(error)
                    )}
    matrix = build_capability_matrix(target_platform=normalized["normalized"])
    report = {"target": normalized, "adapter": None, "status": "unverified",
            "execution": {"status": "unverified", "observed": False, "attempted": False,
                           "reason": "当前没有该目标的执行适配器；只保留规划输入"},
            "toolchain": matrix["toolchain"], "contract_only": True}
    report["evidence_class"] = "unverified"
    report["compatibility"] = platform_compatibility_declaration(
        normalized["normalized"], status="unverified",
        toolchain_status=matrix["toolchain"].get("status", "unverified"),
        reason=report["execution"]["reason"],
    )
    return report


def _capabilities(recommendation: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    values = {item["id"]: item for item in recommendation.get("capabilities", [])}
    if set(values) != set(CAPABILITY_IDS):
        raise W5AcceptanceError("capabilities_invalid", "recommendation must expose all capability categories")
    return {key: {"need": values[key]["need"], "reason": values[key]["reason"],
                  "activation_gate": values[key]["activation_gate"],
                  "source": values[key]["source"]} for key in CAPABILITY_IDS}


class SyntheticAcceptanceMatrix:
    """Immutable-after-preview acceptance record with explicit wave evidence."""

    def __init__(self, scenarios: tuple[SyntheticBeginnerScenario, ...] = DEFAULT_SCENARIOS):
        if not isinstance(scenarios, tuple) or not scenarios or len(scenarios) > 8:
            raise W5AcceptanceError("scenarios_invalid", "scenario matrix must be a bounded tuple")
        if len({item.scenario_id for item in scenarios}) != len(scenarios):
            raise W5AcceptanceError("scenario_duplicate", "scenario ids must be unique")
        self._scenarios = scenarios
        records = []
        for scenario in scenarios:
            recommendation = scenario.recommendation()
            requested = list(recommendation["platforms"]["requested"])
            records.append({
                "scenario_id": scenario.scenario_id,
                "sentence": scenario.sentence,
                "fixture": True,
                "evidence_class": "synthetic",
                "real_user": False,
                "provider_used": False,
                "expected_platform": normalize_target_platform(scenario.expected_platform),
                "recommendation": {
                    "status": recommendation["status"],
                    "platforms": recommendation["platforms"],
                    "unresolved": recommendation["unresolved"],
                    "capabilities": _capabilities(recommendation),
                    "stack": recommendation["stack"],
                },
                "requested_platform_status": [_platform_status(label) for label in requested],
                "all_platform_status": [_platform_status(label) for label in SUPPORTED_TARGET_LABELS],
            })
        self._payload = {
            "schema": MATRIX_SCHEMA, "fixture_label": "synthetic", "evidence_class": "synthetic",
            "synthetic": True, "real_user": False,
            "provider_used": False, "external_actions": False,
            "questions": [deepcopy(item) for item in QUESTION_DEFINITIONS],
            "capability_ids": list(CAPABILITY_IDS),
            "platforms": list(SUPPORTED_TARGET_LABELS),
            "waves": [
                {"id": "clarify", "effect": "questions-and-fixture-answers", "writes": False},
                {"id": "plan", "effect": "recommendation-and-capability-matrix", "writes": False},
                {"id": "preview", "effect": "reviewable-digest-and-targets", "writes": False},
                {"id": "execute", "effect": "reviewed-local-actions-only", "writes": True},
                {"id": "verify", "effect": "non-empty-test-evidence", "writes": False},
                {"id": "rollback", "effect": "receipt-covered-files-only", "writes": True},
                {"id": "review", "effect": "independent-record-review", "writes": False},
            ],
            "scenarios": records,
        }
        self._digest = _digest(self._payload)
        self._approved = False
        self._waves: list[dict[str, Any]] = []
        self._rolled_back = False

    @property
    def digest(self) -> str:
        return self._digest

    def preview(self) -> dict[str, Any]:
        return {"schema": MATRIX_SCHEMA, "status": "ready_for_review", "matrix": deepcopy(self._payload),
                "preview_digest": self._digest, "synthetic": True, "writes": False,
                "authorization_granted": False, "external_audit": self.audit_snapshot(status="ready_for_review")}

    def audit_snapshot(self, *, status: str | None = None) -> dict[str, Any]:
        declarations = []
        seen: set[str] = set()
        for scenario in self._payload["scenarios"]:
            for report in scenario["all_platform_status"]:
                boundary = report.get("compatibility")
                if isinstance(boundary, Mapping) and boundary.get("target") not in seen:
                    declarations.append(boundary)
                    seen.add(boundary["target"])
        return build_read_only_audit_snapshot(
            self._payload,
            source=MATRIX_SCHEMA,
            platform_declarations=declarations,
            summary={
                "scenario_count": len(self._payload["scenarios"]),
                "platform_count": len(declarations),
                "wave_count": len(self._waves),
                "passed_wave_count": sum(1 for item in self._waves if item["status"] == "passed"),
            },
            status=status,
        )

    def approve(self, *, expected_digest: str, synthetic: bool) -> dict[str, Any]:
        if synthetic is not True:
            raise W5AcceptanceError("synthetic_confirmation_required", "fixture approval must be explicitly synthetic")
        if expected_digest != self._digest:
            raise W5AcceptanceError("preview_drifted", "preview drifted after review")
        self._approved = True
        return {"schema": MATRIX_SCHEMA, "status": "approved_synthetic_fixture", "preview_digest": self._digest,
                "synthetic": True, "real_user": False, "authorization_granted": False}

    def record_wave(self, wave_id: str, *, status: str, evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self._approved:
            raise W5AcceptanceError("approval_required", "must approve exact matrix before recording a wave")
        if wave_id not in WAVE_IDS or any(item["id"] == wave_id for item in self._waves):
            raise W5AcceptanceError("wave_invalid", "wave must be known and recorded once")
        if status not in {"passed", "blocked", "failed"}:
            raise W5AcceptanceError("wave_status_invalid", "wave status must be passed, blocked or failed")
        record = {"id": wave_id, "status": status, "evidence": dict(evidence or {}), "synthetic": True,
                  "preview_digest": self._digest}
        if wave_id == "verify" and status == "passed":
            tests_run = record["evidence"].get("tests_run")
            if isinstance(tests_run, bool) or not isinstance(tests_run, int) or tests_run <= 0:
                raise W5AcceptanceError("test_evidence_invalid", "verify wave requires positive tests_run")
        if wave_id == "rollback" and status == "passed":
            if record["evidence"].get("scope") != "receipt_covered_files_only":
                raise W5AcceptanceError("rollback_evidence_invalid", "rollback scope must be receipt_covered_files_only")
            self._rolled_back = True
        self._waves.append(record)
        return deepcopy(record)

    def report(self) -> dict[str, Any]:
        required = {"clarify", "plan", "preview", "execute", "verify", "rollback", "review"}
        status = "passed" if self._rolled_back and {item["id"] for item in self._waves} == required and all(item["status"] == "passed" for item in self._waves) else "incomplete"
        return {"schema": MATRIX_SCHEMA, "status": status, "preview_digest": self._digest,
                "evidence_class": "synthetic", "synthetic": True, "real_user": False, "provider_used": False,
                "external_actions": False, "waves": deepcopy(self._waves),
                "rollback": {"status": "simulated_receipt_scope" if self._rolled_back else "not_recorded",
                             "automatic": False}, "external_audit": self.audit_snapshot(status=status)}


def build_synthetic_acceptance_matrix() -> dict[str, Any]:
    return SyntheticAcceptanceMatrix().preview()


__all__ = [
    "CAPABILITY_IDS", "DEFAULT_SCENARIOS", "MATRIX_SCHEMA", "SUPPORTED_TARGET_LABELS",
    "SyntheticAcceptanceMatrix", "SyntheticBeginnerScenario", "W5AcceptanceError",
    "build_synthetic_acceptance_matrix",
]
