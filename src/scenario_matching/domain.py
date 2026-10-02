"""创新场景供需撮合与阶段成果验收的领域模型与不变量。

流程主线：

    需求单位发布机会(版本) ── 能力主体登记能力(版本，含适用范围)
        │            │
        └─ 仅按双方授权披露字段匹配 ─┘
                     ▼
                  揭榜（冻结当时的需求版本与能力版本）
                     ▼
            评审通过 → 签订协议（占用试点容量）
                     ▼
          里程碑（指标由冻结版本推导；变更范围形成协商记录）
                     ▼
        完成并通过终验（双方确认，提出者不能单独确认最终成效）
                     ▼
        案例发布（需求方与能力方分别授权；发布后不可改写）

所有状态均为可重放的数据，业务规则集中在本模块，便于事件溯源存储与崩溃恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class DomainError(Exception):
    """所有业务规则冲突的基类。"""


class DisclosureError(DomainError):
    """匹配或查看时使用了未授权披露的字段。"""


class VersionStateError(DomainError):
    """版本不存在、已停用或不能继续流转。"""


class DuplicateSubmissionError(DomainError):
    """重复申报：同一诉求已在既有撮合中处理，直接返回既有结果。"""

    def __init__(self, submission_key: str, match_id: str) -> None:
        super().__init__(f"该申报已存在，沿用既有撮合结果 {match_id}")
        self.submission_key = submission_key
        self.match_id = match_id


class CapacityError(DomainError):
    """试点容量不足或被重复占用。"""


class ScopeError(DomainError):
    """能力适用范围不覆盖机会的数据范围或试点规模。"""


class MilestoneError(DomainError):
    """里程碑流转违规（未到期完成、跳过顺序、改已封存评价等）。"""


class EvaluationError(DomainError):
    """成效评价规则违规（最终成效由提出者单方确认等）。"""


class AuthorizationError(DomainError):
    """案例缺少一方或双方的发布授权。"""


class ConflictError(DomainError):
    """材料与已冻结事实冲突，须先隔离复核，不能直接覆盖。"""

    def __init__(self, message: str, material: "Material") -> None:
        super().__init__(message)
        self.material = material


class ActorRole(str, Enum):
    OPERATOR = "场景运营团队"
    DEMANDER = "需求单位"
    SUPERVISOR = "行业主管部门"
    PROVIDER = "能力主体"  # 高校院所、技术企业等
    REVIEWER = "评审人员"


# 敏感等级：字段只有在双方都披露到对方可见时才能用于匹配。
SENSITIVITY_LEVELS = ("公开", "对接披露", "保密")


@dataclass(frozen=True)
class Actor:
    actor_id: str
    name: str
    role: ActorRole


@dataclass(frozen=True)
class FieldSpec:
    """可披露字段的定义。"""

    key: str
    label: str
    sensitivity: str  # 见 SENSITIVITY_LEVELS

    def __post_init__(self) -> None:
        if self.sensitivity not in SENSITIVITY_LEVELS:
            raise ValueError(f"未知敏感等级：{self.sensitivity}")


@dataclass(frozen=True)
class DisclosureGrant:
    """一次定向授权：owner 允许把 field_key 展示给 viewer_id。

    匹配引擎只能使用双方都向对方授权过的字段（对机会与能力均成立）。
    """

    owner_id: str
    viewer_id: str
    field_key: str
    allowed: bool = True


@dataclass(frozen=True)
class Scope:
    """能力/试点的适用范围与规模。

    data_domains 为允许使用的数据范围（如“交通卡口视频”），
    pilot_scale 为可承载的试点规模（点数、线路数等），用整数度量以便比较。
    """

    data_domains: frozenset[str] = frozenset()
    pilot_scale: int = 0
    scale_unit: str = "点位"

    def covers(self, required: "Scope") -> bool:
        if not required.data_domains <= self.data_domains:
            return False
        return self.pilot_scale >= required.pilot_scale


@dataclass(frozen=True)
class Opportunity:
    """场景机会。真实痛点可放入保密字段，不披露就不会进入匹配。"""

    opportunity_id: str
    demander_id: str
    version: int
    title: str
    fields: dict[str, Any]  # field_key -> 值（痛点、预算、指标诉求等）
    required_scope: Scope
    open_conditions: tuple[str, ...] = ()  # 开放条件：数据边界、资质门槛等
    withdrawn: bool = False


@dataclass(frozen=True)
class Capability:
    """技术能力/解决方案。一项能力可同时回应多个场景，范围以 Scope 为限。"""

    capability_id: str
    provider_id: str
    version: int
    title: str
    fields: dict[str, Any]
    scope: Scope
    answers_scenarios: frozenset[str] = frozenset()  # 可回应的机会主题
    withdrawn: bool = False


@dataclass(frozen=True)
class Material:
    """申报/补充材料。冲突材料只携带内容与声明，处理状态由系统决定。"""

    material_id: str
    submitter_id: str
    kind: str
    payload: dict[str, Any]
    declared_fingerprint: Optional[str] = None


class MatchStatus(str, Enum):
    SUGGESTED = "已撮合"          # 系统按披露字段给出的候选
    CLAIMED = "已揭榜"            # 揭榜，需求/能力版本已冻结
    REJECTED = "评审未通过"
    SIGNED = "已签约"
    TERMINATED = "已终止"
    COMPLETED = "已完成"
    PUBLISHED = "已发布案例"


@dataclass
class FrozenRef:
    """揭榜时冻结的版本引用；范围变化只追加协商记录，不改本引用。"""

    opportunity_version: int
    capability_version: int
    opportunity_fingerprint: str
    capability_fingerprint: str
    frozen_at_event: int  # 事件序列号，便于审计


@dataclass
class Agreement:
    agreement_id: str
    signed_at_event: int
    allocated_scope: Scope  # 实际占用的试点容量（从资源池扣除）
    milestone_plan: tuple["Milestone", ...]  # 签约时由冻结版本确定的阶段指标


@dataclass
class Milestone:
    milestone_id: str
    seq: int
    name: str
    indicators: dict[str, float]  # 指标键 -> 目标值（源自冻结的需求版本）
    due_event: int               # 到期时点（用事件序号表达，存储无关）
    status: str = "待履行"        # 待履行/已通过/未通过/已免除
    evaluated_by: tuple[str, ...] = ()  # 评价参与人
    remark: str = ""


@dataclass
class NegotiationRecord:
    """揭榜后的范围变化：不回改冻结版本，只追加协商记录。"""

    record_id: str
    at_event: int
    requested_by: str
    change: dict[str, Any]       # 例如 {"pilot_scale": 40} 或新增数据范围
    reason: str
    accepted: bool = False       # 双方接受后才影响后续履行
    accepted_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuarantineEntry:
    at_event: int
    reason: str
    material: Material
    match_id: str = ""


@dataclass
class MatchCase:
    match_id: str
    opportunity_id: str
    capability_id: str
    demander_id: str
    provider_id: str
    status: MatchStatus = MatchStatus.SUGGESTED
    review_passed: bool = False
    submission_key: Optional[str] = None
    frozen: Optional[FrozenRef] = None
    agreement: Optional[Agreement] = None
    negotiations: list[NegotiationRecord] = field(default_factory=list)
    demander_authors_publish: bool = False
    provider_authors_publish: bool = False
    published_case_id: Optional[str] = None
    created_event: int = 0


@dataclass
class ResourcePool:
    """试点资源池：容量在“签约”时原子占用，失败/退出只释放未履行部分。"""

    pool_id: str
    total: int
    allocated: int = 0
    # match_id -> 当前占用量；随协商记录可增减，退出时按未履行量释放
    holdings: dict[str, int] = field(default_factory=dict)

    @property
    def available(self) -> int:
        return self.total - self.allocated

    def can_hold(self, amount: int) -> bool:
        return amount >= 0 and self.allocated + amount <= self.total

    def hold(self, match_id: str, amount: int) -> None:
        if amount < 0:
            raise CapacityError("占用容量不能为负")
        if match_id in self.holdings:
            raise CapacityError(f"撮合 {match_id} 已占用容量，禁止重复占用")
        if not self.can_hold(amount):
            raise CapacityError(
                f"试点容量不足：需要 {amount}，剩余 {self.available}"
            )
        self.holdings[match_id] = amount
        self.allocated += amount

    def adjust(self, match_id: str, delta: int) -> None:
        """协商一致后的容量变更（delta 可正可负）。"""

        held = self.holdings.get(match_id)
        if held is None:
            raise CapacityError(f"撮合 {match_id} 没有占用记录")
        new_amount = held + delta
        if new_amount < 0:
            raise CapacityError("释放量不能超过已占用量")
        if not self.can_hold(delta if delta > 0 else 0):
            raise CapacityError(f"扩容后超出容量：剩余 {self.available}")
        self.holdings[match_id] = new_amount
        self.allocated += delta

    def release(self, match_id: str, amount: Optional[int] = None) -> int:
        """释放容量；amount 为 None 表示释放该撮合当前全部未履行占用。"""

        held = self.holdings.get(match_id, 0)
        amount = held if amount is None else min(amount, held)
        if amount < 0:
            raise CapacityError("释放容量不能为负")
        self.holdings[match_id] = held - amount
        self.allocated -= amount
        return amount


def _fingerprint(payload: Any) -> str:
    import hashlib
    import json

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def opportunity_fingerprint(opp: Opportunity) -> str:
    return _fingerprint(
        {
            "v": opp.version,
            "fields": opp.fields,
            "scope": (sorted(opp.required_scope.data_domains),
                      opp.required_scope.pilot_scale),
            "open_conditions": opp.open_conditions,
        }
    )


def capability_fingerprint(cap: Capability) -> str:
    return _fingerprint(
        {
            "v": cap.version,
            "fields": cap.fields,
            "scope": (sorted(cap.scope.data_domains), cap.scope.pilot_scale),
            "answers": sorted(cap.answers_scenarios),
        }
    )


def matching_visible_fields(
    owner_fields: dict[str, Any],
    grants: frozenset[DisclosureGrant],
    owner_id: str,
    viewer_id: str,
) -> dict[str, Any]:
    """返回 owner 明确授权给 viewer 的字段视图；未授权字段一律剔除。"""

    allowed = {
        g.field_key
        for g in grants
        if g.owner_id == owner_id and g.viewer_id == viewer_id and g.allowed
    }
    return {k: v for k, v in owner_fields.items() if k in allowed}
