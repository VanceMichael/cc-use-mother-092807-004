"""领域模型：参与方、机会/能力版本、申报、揭榜合作、协议、协商、里程碑与案例。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 参与方角色
ROLE_OPERATOR = "operator"          # 场景运营团队
ROLE_REGULATOR = "regulator"        # 行业主管部门
ROLE_DEMANDER = "demander"          # 需求单位
ROLE_PROVIDER = "provider"          # 能力主体（高校院所、技术企业）
ROLE_REVIEWER = "reviewer"          # 独立试点评审人员

# 版本状态
VERSION_DRAFT = "draft"
VERSION_OPEN = "open"
VERSION_FROZEN = "frozen"
VERSION_SUPERSEDED = "superseded"

# 申报状态
APPLICATION_SUBMITTED = "submitted"
APPLICATION_SELECTED = "selected"

# 合作（揭榜）状态
ENGAGEMENT_SELECTED = "selected"      # 已揭榜，待签协议
ENGAGEMENT_AGREED = "agreed"          # 协议已签，试点中
ENGAGEMENT_COMPLETED = "completed"    # 全部阶段验收完成
ENGAGEMENT_TERMINATED = "terminated"  # 失败或退出

# 协议状态
AGREEMENT_PENDING = "pending"
AGREEMENT_SIGNED = "signed"

# 资源承诺状态
RESOURCE_RESERVED = "reserved"   # 已预留（试点容量占用中，尚未履行）
RESOURCE_CONSUMED = "consumed"   # 阶段验收后已履行，永不释放
RESOURCE_RELEASED = "released"   # 失败/退出时释放的未履行部分

# 里程碑状态
MILESTONE_PENDING = "pending"
MILESTONE_ACCEPTED = "accepted"

EVAL_ACCEPTED = "accepted"
EVAL_REJECTED = "rejected"

# 协商记录状态
NEGOTIATION_PROPOSED = "proposed"
NEGOTIATION_AGREED = "agreed"
NEGOTIATION_REJECTED = "rejected"

# 案例状态
CASE_DRAFT = "draft"
CASE_REVIEW = "review"
CASE_PUBLISHED = "published"


@dataclass
class Party:
    id: str
    name: str
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "role": self.role}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Party":
        return cls(id=value["id"], name=value["name"], role=value["role"])


@dataclass
class FieldEntry:
    """机会/能力版本上的一个字段；披露范围以字段为最小粒度。"""

    key: str
    value: Any
    disclose_to: list[str] = field(default_factory=list)  # 拥有方愿意披露给的参与方

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "disclose_to": list(self.disclose_to)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FieldEntry":
        return cls(
            key=value["key"],
            value=value["value"],
            disclose_to=list(value.get("disclose_to", [])),
        )


@dataclass
class MetricTarget:
    """阶段指标。揭榜时冻结，变更只能走协商记录。"""

    name: str
    target: str
    data_scope: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "target": self.target, "data_scope": self.data_scope}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MetricTarget":
        return cls(name=value["name"], target=value["target"], data_scope=value["data_scope"])


@dataclass
class OpportunityVersion:
    version_no: int
    fields: dict[str, FieldEntry]
    open_conditions: list[str]
    success_metrics: list[MetricTarget]
    created_at: str
    status: str = VERSION_OPEN
    remark: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_no": self.version_no,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "open_conditions": list(self.open_conditions),
            "success_metrics": [m.to_dict() for m in self.success_metrics],
            "created_at": self.created_at,
            "status": self.status,
            "remark": self.remark,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OpportunityVersion":
        return cls(
            version_no=value["version_no"],
            fields={k: FieldEntry.from_dict(v) for k, v in value["fields"].items()},
            open_conditions=list(value["open_conditions"]),
            success_metrics=[MetricTarget.from_dict(m) for m in value["success_metrics"]],
            created_at=value["created_at"],
            status=value["status"],
            remark=value.get("remark", ""),
        )


@dataclass
class CapabilityVersion:
    version_no: int
    fields: dict[str, FieldEntry]
    data_scope: str           # 允许使用的数据范围
    capacity_total: int       # 试点规模（容量单位）
    created_at: str
    status: str = VERSION_OPEN
    remark: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_no": self.version_no,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "data_scope": self.data_scope,
            "capacity_total": self.capacity_total,
            "created_at": self.created_at,
            "status": self.status,
            "remark": self.remark,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityVersion":
        return cls(
            version_no=value["version_no"],
            fields={k: FieldEntry.from_dict(v) for k, v in value["fields"].items()},
            data_scope=value["data_scope"],
            capacity_total=value["capacity_total"],
            created_at=value["created_at"],
            status=value["status"],
            remark=value.get("remark", ""),
        )


@dataclass
class Opportunity:
    id: str
    demander_id: str
    title: str
    versions: list[OpportunityVersion] = field(default_factory=list)

    @property
    def current(self) -> OpportunityVersion:
        return self.versions[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "demander_id": self.demander_id,
            "title": self.title,
            "versions": [v.to_dict() for v in self.versions],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Opportunity":
        return cls(
            id=value["id"],
            demander_id=value["demander_id"],
            title=value["title"],
            versions=[OpportunityVersion.from_dict(v) for v in value["versions"]],
        )


@dataclass
class Capability:
    id: str
    provider_id: str
    title: str
    versions: list[CapabilityVersion] = field(default_factory=list)

    @property
    def current(self) -> CapabilityVersion:
        return self.versions[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "title": self.title,
            "versions": [v.to_dict() for v in self.versions],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Capability":
        return cls(
            id=value["id"],
            provider_id=value["provider_id"],
            title=value["title"],
            versions=[CapabilityVersion.from_dict(v) for v in value["versions"]],
        )


@dataclass
class Application:
    """解决方案申报（揭榜申请）。"""

    id: str
    fingerprint: str
    opportunity_id: str
    opportunity_version_no: int
    capability_id: str
    capability_version_no: int
    provider_id: str
    summary: str
    materials: dict[str, Any]
    status: str = APPLICATION_SUBMITTED
    engagement_id: str | None = None
    created_at: str = ""
    match_basis: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "opportunity_id": self.opportunity_id,
            "opportunity_version_no": self.opportunity_version_no,
            "capability_id": self.capability_id,
            "capability_version_no": self.capability_version_no,
            "provider_id": self.provider_id,
            "summary": self.summary,
            "materials": self.materials,
            "status": self.status,
            "engagement_id": self.engagement_id,
            "created_at": self.created_at,
            "match_basis": self.match_basis,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Application":
        return cls(**value)


@dataclass
class StageEvaluation:
    reviewer_id: str
    results: dict[str, str]   # 指标名 -> 实测结果
    verdict: str
    note: str
    evaluated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer_id": self.reviewer_id,
            "results": dict(self.results),
            "verdict": self.verdict,
            "note": self.note,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StageEvaluation":
        return cls(
            reviewer_id=value["reviewer_id"],
            results=dict(value["results"]),
            verdict=value["verdict"],
            note=value["note"],
            evaluated_at=value["evaluated_at"],
        )


@dataclass
class Milestone:
    seq: int
    name: str
    due_date: str
    reserved_units: int
    metrics: list[MetricTarget]
    resource_status: str = RESOURCE_RESERVED
    status: str = MILESTONE_PENDING
    reported: dict[str, str] | None = None
    evaluation: StageEvaluation | None = None
    accepted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "name": self.name,
            "due_date": self.due_date,
            "reserved_units": self.reserved_units,
            "metrics": [m.to_dict() for m in self.metrics],
            "resource_status": self.resource_status,
            "status": self.status,
            "reported": None if self.reported is None else dict(self.reported),
            "evaluation": None if self.evaluation is None else self.evaluation.to_dict(),
            "accepted_at": self.accepted_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Milestone":
        evaluation = value.get("evaluation")
        return cls(
            seq=value["seq"],
            name=value["name"],
            due_date=value["due_date"],
            reserved_units=value["reserved_units"],
            metrics=[MetricTarget.from_dict(m) for m in value["metrics"]],
            resource_status=value["resource_status"],
            status=value["status"],
            reported=None if value.get("reported") is None else dict(value["reported"]),
            evaluation=None if evaluation is None else StageEvaluation.from_dict(evaluation),
            accepted_at=value.get("accepted_at"),
        )


@dataclass
class PilotAgreement:
    status: str = AGREEMENT_PENDING
    content_hash: str = ""
    demander_signed_at: str | None = None
    provider_signed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "content_hash": self.content_hash,
            "demander_signed_at": self.demander_signed_at,
            "provider_signed_at": self.provider_signed_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PilotAgreement":
        return cls(**value)


@dataclass
class ChangeItem:
    """一次协商中的单项变更，保留变更前值以保证可追溯。"""

    path: str
    before: str
    after: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "before": self.before, "after": self.after}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChangeItem":
        return cls(path=value["path"], before=value["before"], after=value["after"])


@dataclass
class NegotiationRecord:
    id: str
    seq: int
    requested_by: str
    reason: str
    changes: list[ChangeItem]
    status: str = NEGOTIATION_PROPOSED
    responses: dict[str, bool] = field(default_factory=dict)
    created_at: str = ""
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "requested_by": self.requested_by,
            "reason": self.reason,
            "changes": [c.to_dict() for c in self.changes],
            "status": self.status,
            "responses": dict(self.responses),
            "created_at": self.created_at,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NegotiationRecord":
        return cls(
            id=value["id"],
            seq=value["seq"],
            requested_by=value["requested_by"],
            reason=value["reason"],
            changes=[ChangeItem.from_dict(c) for c in value["changes"]],
            status=value["status"],
            responses=dict(value.get("responses", {})),
            created_at=value["created_at"],
            decided_at=value.get("decided_at"),
        )


@dataclass
class Engagement:
    """揭榜合作：冻结当时的需求与能力版本，串联协议、协商、阶段与案例。"""

    id: str
    application_id: str
    opportunity_id: str
    capability_id: str
    demander_id: str
    provider_id: str
    capacity_units: int
    frozen_opportunity: dict[str, Any]
    frozen_capability: dict[str, Any]
    milestones: list[Milestone] = field(default_factory=list)
    agreement: PilotAgreement = field(default_factory=PilotAgreement)
    negotiations: list[NegotiationRecord] = field(default_factory=list)
    state: str = ENGAGEMENT_SELECTED
    created_at: str = ""
    terminated_at: str | None = None
    terminate_reason: str | None = None

    def milestone(self, seq: int) -> Milestone:
        for milestone in self.milestones:
            if milestone.seq == seq:
                return milestone
        raise KeyError(f"里程碑 {seq} 不存在")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "application_id": self.application_id,
            "opportunity_id": self.opportunity_id,
            "capability_id": self.capability_id,
            "demander_id": self.demander_id,
            "provider_id": self.provider_id,
            "capacity_units": self.capacity_units,
            "frozen_opportunity": self.frozen_opportunity,
            "frozen_capability": self.frozen_capability,
            "milestones": [m.to_dict() for m in self.milestones],
            "agreement": self.agreement.to_dict(),
            "negotiations": [n.to_dict() for n in self.negotiations],
            "state": self.state,
            "created_at": self.created_at,
            "terminated_at": self.terminated_at,
            "terminate_reason": self.terminate_reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Engagement":
        return cls(
            id=value["id"],
            application_id=value["application_id"],
            opportunity_id=value["opportunity_id"],
            capability_id=value["capability_id"],
            demander_id=value["demander_id"],
            provider_id=value["provider_id"],
            capacity_units=value["capacity_units"],
            frozen_opportunity=dict(value["frozen_opportunity"]),
            frozen_capability=dict(value["frozen_capability"]),
            milestones=[Milestone.from_dict(m) for m in value["milestones"]],
            agreement=PilotAgreement.from_dict(value["agreement"]),
            negotiations=[NegotiationRecord.from_dict(n) for n in value["negotiations"]],
            state=value["state"],
            created_at=value["created_at"],
            terminated_at=value.get("terminated_at"),
            terminate_reason=value.get("terminate_reason"),
        )


@dataclass
class CaseFile:
    id: str
    engagement_id: str
    title: str
    summary: str
    status: str = CASE_DRAFT
    publish_grants: dict[str, str] = field(default_factory=dict)  # 参与方 -> 授权时间
    submitted_for_review_at: str | None = None
    review: StageEvaluation | None = None
    published_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "engagement_id": self.engagement_id,
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "publish_grants": dict(self.publish_grants),
            "submitted_for_review_at": self.submitted_for_review_at,
            "review": None if self.review is None else self.review.to_dict(),
            "published_at": self.published_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CaseFile":
        review = value.get("review")
        return cls(
            id=value["id"],
            engagement_id=value["engagement_id"],
            title=value["title"],
            summary=value["summary"],
            status=value["status"],
            publish_grants=dict(value.get("publish_grants", {})),
            submitted_for_review_at=value.get("submitted_for_review_at"),
            review=None if review is None else StageEvaluation.from_dict(review),
            published_at=value.get("published_at"),
        )


@dataclass
class QuarantineItem:
    """冲突材料隔离条目，服务恢复后继续处理。"""

    id: str
    kind: str
    payload: dict[str, Any]
    reason: str
    at: str
    resolved: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "reason": self.reason,
            "at": self.at,
            "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QuarantineItem":
        return cls(**value)
