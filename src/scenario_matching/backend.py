"""供需撮合后端：把命令校验、事件落盘、状态重放收敛在一处。

所有写操作都遵循“先校验、再追加事件、事件函数改状态”的模式，
因此从 EventStore 重放可以无损恢复；读操作不产生事件。
"""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any, Optional

from .domain import (
    Agreement,
    AuthorizationError,
    Capability,
    CapacityError,
    ConflictError,
    DisclosureError,
    DomainError,
    DuplicateSubmissionError,
    EvaluationError,
    FrozenRef,
    MatchCase,
    MatchStatus,
    Material,
    Milestone,
    MilestoneError,
    NegotiationRecord,
    Opportunity,
    QuarantineEntry,
    ResourcePool,
    Scope,
    ScopeError,
    VersionStateError,
    Actor,
    ActorRole,
    capability_fingerprint,
    opportunity_fingerprint,
)
from .store import EventStore


def _scope_to_data(scope: Scope) -> dict[str, Any]:
    return {
        "data_domains": sorted(scope.data_domains),
        "pilot_scale": scope.pilot_scale,
        "scale_unit": scope.scale_unit,
    }


def _scope_from_data(data: dict[str, Any]) -> Scope:
    return Scope(
        data_domains=frozenset(data["data_domains"]),
        pilot_scale=data["pilot_scale"],
        scale_unit=data.get("scale_unit", "点位"),
    )


def _opp_to_data(opp: Opportunity) -> dict[str, Any]:
    return {
        "opportunity_id": opp.opportunity_id,
        "demander_id": opp.demander_id,
        "version": opp.version,
        "title": opp.title,
        "fields": opp.fields,
        "required_scope": _scope_to_data(opp.required_scope),
        "open_conditions": list(opp.open_conditions),
        "withdrawn": opp.withdrawn,
    }


def _opp_from_data(d: dict[str, Any]) -> Opportunity:
    return Opportunity(
        opportunity_id=d["opportunity_id"],
        demander_id=d["demander_id"],
        version=d["version"],
        title=d["title"],
        fields=d["fields"],
        required_scope=_scope_from_data(d["required_scope"]),
        open_conditions=tuple(d.get("open_conditions", ())),
        withdrawn=d.get("withdrawn", False),
    )


def _cap_to_data(cap: Capability) -> dict[str, Any]:
    return {
        "capability_id": cap.capability_id,
        "provider_id": cap.provider_id,
        "version": cap.version,
        "title": cap.title,
        "fields": cap.fields,
        "scope": _scope_to_data(cap.scope),
        "answers_scenarios": sorted(cap.answers_scenarios),
        "withdrawn": cap.withdrawn,
    }


def _cap_from_data(d: dict[str, Any]) -> Capability:
    return Capability(
        capability_id=d["capability_id"],
        provider_id=d["provider_id"],
        version=d["version"],
        title=d["title"],
        fields=d["fields"],
        scope=_scope_from_data(d["scope"]),
        answers_scenarios=frozenset(d.get("answers_scenarios", ())),
        withdrawn=d.get("withdrawn", False),
    )


def _milestone_to_data(m: Milestone) -> dict[str, Any]:
    return asdict(m)


def _milestone_from_data(d: dict[str, Any]) -> Milestone:
    return Milestone(
        milestone_id=d["milestone_id"],
        seq=d["seq"],
        name=d["name"],
        indicators=d["indicators"],
        due_event=d["due_event"],
        status=d.get("status", "待履行"),
        evaluated_by=tuple(d.get("evaluated_by", ())),
        remark=d.get("remark", ""),
    )


class ScenarioMatchingBackend:
    """事件溯源的供需撮合后端。"""

    def __init__(self, store: Optional[EventStore] = None) -> None:
        self.store = store or EventStore()
        self.actors: dict[str, Actor] = {}
        self.opp_versions: dict[str, dict[int, Opportunity]] = {}
        self.cap_versions: dict[str, dict[int, Capability]] = {}
        self.pools: dict[str, ResourcePool] = {}
        self.matches: dict[str, MatchCase] = {}
        self.submission_index: dict[str, str] = {}
        self.quarantine: list[QuarantineEntry] = []
        self.published_cases: dict[str, dict[str, Any]] = {}
        # 以下为易失索引，重放事件流时重建。
        self._grants_map: dict[tuple[str, str, str], bool] = {}
        self._match_pools: dict[str, str] = {}
        self._material_log: dict[str, list[dict[str, str]]] = {}
        self._resolved_quarantine: set[str] = set()
        self.store.replay(self._apply)

    # ----- 基础登记 -----------------------------------------------------

    def register_actor(self, actor_id: str, name: str, role: ActorRole) -> Actor:
        if actor_id in self.actors:
            raise DomainError(f"参与方 {actor_id} 已登记")
        self._append(
            "ActorRegistered",
            {"actor_id": actor_id, "name": name, "role": role.value},
        )
        return self.actors[actor_id]

    def add_resource_pool(self, pool_id: str, total: int) -> ResourcePool:
        if pool_id in self.pools:
            raise DomainError(f"资源池 {pool_id} 已存在")
        if total < 0:
            raise CapacityError("资源池容量不能为负")
        self._append("ResourcePoolAdded", {"pool_id": pool_id, "total": total})
        return self.pools[pool_id]

    def publish_opportunity(
        self,
        opportunity_id: str,
        demander_id: str,
        title: str,
        fields: dict[str, Any],
        required_scope: Scope,
        open_conditions: tuple[str, ...] = (),
    ) -> Opportunity:
        if demander_id not in self.actors:
            raise DomainError("需求单位尚未登记")
        history = self.opp_versions.setdefault(opportunity_id, {})
        if history:
            previous = history[max(history)]
            if previous.demander_id != demander_id:
                raise DomainError("机会新版本只能由原需求单位发布")
            if previous.withdrawn:
                raise VersionStateError("机会已撤下，不能再发新版本")
        version = len(history) + 1  # 资料版本只能递增
        opp = Opportunity(
            opportunity_id=opportunity_id,
            demander_id=demander_id,
            version=version,
            title=title,
            fields=dict(fields),
            required_scope=required_scope,
            open_conditions=tuple(open_conditions),
        )
        self._append("OpportunityPublished", {"opportunity": _opp_to_data(opp)})
        return opp

    def register_capability(
        self,
        capability_id: str,
        provider_id: str,
        title: str,
        fields: dict[str, Any],
        scope: Scope,
        answers_scenarios: frozenset[str] = frozenset(),
    ) -> Capability:
        if provider_id not in self.actors:
            raise DomainError("能力主体尚未登记")
        history = self.cap_versions.setdefault(capability_id, {})
        if history:
            previous = history[max(history)]
            if previous.provider_id != provider_id:
                raise DomainError("能力新版本只能由原能力主体登记")
            if previous.withdrawn:
                raise VersionStateError("能力已撤下，不能再发新版本")
        version = len(history) + 1
        cap = Capability(
            capability_id=capability_id,
            provider_id=provider_id,
            version=version,
            title=title,
            fields=dict(fields),
            scope=scope,
            answers_scenarios=frozenset(answers_scenarios),
        )
        self._append("CapabilityRegistered", {"capability": _cap_to_data(cap)})
        return cap

    def withdraw_opportunity(self, opportunity_id: str) -> None:
        current = self.latest_opportunity(opportunity_id)
        if current.withdrawn:
            raise VersionStateError("机会已撤下")
        withdrawn = Opportunity(**{**current.__dict__, "withdrawn": True})
        self._append("OpportunityWithdrawn", {"opportunity": _opp_to_data(withdrawn)})

    def withdraw_capability(self, capability_id: str) -> None:
        current = self.latest_capability(capability_id)
        if current.withdrawn:
            raise VersionStateError("能力已撤下")
        withdrawn = Capability(**{**current.__dict__, "withdrawn": True})
        self._append("CapabilityWithdrawn", {"capability": _cap_to_data(withdrawn)})

    def grant_disclosure(
        self, owner_id: str, viewer_id: str, field_key: str, allowed: bool = True
    ) -> None:
        if owner_id not in self.actors or viewer_id not in self.actors:
            raise DomainError("授权双方必须均已登记")
        self._append(
            "DisclosureGranted",
            {
                "owner_id": owner_id,
                "viewer_id": viewer_id,
                "field_key": field_key,
                "allowed": allowed,
            },
        )

    # ----- 撮合 ---------------------------------------------------------

    def visible_opportunity_fields(
        self, opp: Opportunity, viewer_id: str
    ) -> dict[str, Any]:
        allowed = {
            field
            for (owner, viewer, field), ok in self._grants_map.items()
            if owner == opp.demander_id and viewer == viewer_id and ok
        }
        return {k: v for k, v in opp.fields.items() if k in allowed}

    def visible_capability_fields(
        self, cap: Capability, viewer_id: str
    ) -> dict[str, Any]:
        allowed = {
            field
            for (owner, viewer, field), ok in self._grants_map.items()
            if owner == cap.provider_id and viewer == viewer_id and ok
        }
        return {k: v for k, v in cap.fields.items() if k in allowed}

    def submit_for_matching(
        self,
        submission_key: str,
        opportunity_id: str,
        capability_id: str,
        pool_id: str,
    ) -> tuple[MatchCase, bool]:
        """申报撮合。重复申报（同一 submission_key）直接返回既有结果。

        返回 (撮合单, 是否重复)；重复时不产生任何新事件。
        """

        if submission_key in self.submission_index:
            existing_id = self.submission_index[submission_key]
            # 不允许借重复申报换绑另一对供需。
            existing = self.matches[existing_id]
            if (
                existing.opportunity_id != opportunity_id
                or existing.capability_id != capability_id
            ):
                raise DuplicateSubmissionError(submission_key, existing_id)
            return existing, True

        opp = self.latest_opportunity(opportunity_id)
        cap = self.latest_capability(capability_id)
        if opp.withdrawn or cap.withdrawn:
            raise VersionStateError("机会或能力已撤下，不能申报")
        if pool_id not in self.pools:
            raise DomainError("试点资源池不存在")

        # 匹配只能使用双方同意披露给对方的字段。
        opp_view = self.visible_opportunity_fields(opp, cap.provider_id)
        cap_view = self.visible_capability_fields(cap, opp.demander_id)
        if not opp_view or not cap_view:
            raise DisclosureError("双方未互相披露可供匹配的字段，不能撮合")

        basis = self._compute_basis(opp_view, cap_view)
        if not basis:
            raise DisclosureError("在已授权披露的字段内没有匹配依据，不能撮合")

        match_id = f"M-{len(self.matches) + 1:04d}"
        self._append(
            "MatchSuggested",
            {
                "match_id": match_id,
                "submission_key": submission_key,
                "opportunity_id": opportunity_id,
                "capability_id": capability_id,
                "pool_id": pool_id,
                "demander_id": opp.demander_id,
                "provider_id": cap.provider_id,
                "opportunity_version": opp.version,
                "capability_version": cap.version,
                "basis": basis,
            },
        )
        return self.matches[match_id], False

    @staticmethod
    def _compute_basis(
        opp_view: dict[str, Any], cap_view: dict[str, Any]
    ) -> list[dict[str, str]]:
        """仅基于双方可见字段求匹配依据：同键且值相容（相等或集合相交）。"""

        basis: list[dict[str, str]] = []
        for key, opp_value in opp_view.items():
            if key not in cap_view:
                continue
            cap_value = cap_view[key]
            if opp_value == cap_value:
                basis.append({"field": key, "type": "相等", "value": str(opp_value)})
                continue
            if isinstance(opp_value, (list, tuple, set)) and isinstance(
                cap_value, (list, tuple, set)
            ):
                overlap = sorted(set(opp_value) & set(cap_value))
                if overlap:
                    basis.append(
                        {"field": key, "type": "相交", "value": ",".join(map(str, overlap))}
                    )
        return basis

    # ----- 揭榜、冻结、评审 ----------------------------------------------

    def claim(self, match_id: str) -> MatchCase:
        """揭榜：冻结当时的需求版本与能力版本。"""

        case = self._require_case(match_id)
        if case.status != MatchStatus.SUGGESTED:
            raise VersionStateError(f"当前状态 {case.status.value}，不能揭榜")
        opp = self.latest_opportunity(case.opportunity_id)
        cap = self.latest_capability(case.capability_id)
        if opp.withdrawn or cap.withdrawn:
            raise VersionStateError("机会或能力已撤下，不能揭榜")
        frozen = FrozenRef(
            opportunity_version=opp.version,
            capability_version=cap.version,
            opportunity_fingerprint=opportunity_fingerprint(opp),
            capability_fingerprint=capability_fingerprint(cap),
            frozen_at_event=self.store.next_seq,
        )
        self._append(
            "Claimed",
            {
                "match_id": match_id,
                "frozen": asdict(frozen),
                "opportunity_title": opp.title,
                "capability_title": cap.title,
            },
        )
        return case

    def submit_review_material(self, match_id: str, material: Material) -> None:
        """向评审提交材料；与冻结事实冲突的材料先隔离，不允许覆盖。"""

        case = self._require_case(match_id)
        if case.frozen is None:
            raise VersionStateError("尚未揭榜冻结，不能提交评审材料")
        frozen_opp = self.frozen_opportunity(case)
        frozen_cap = self.frozen_capability(case)

        conflict_reason: Optional[str] = None
        kind = material.kind
        if kind == "需求变更声明" and material.declared_fingerprint:
            if material.declared_fingerprint != case.frozen.opportunity_fingerprint:
                conflict_reason = (
                    "材料声称的需求版本与揭榜冻结版本不一致，"
                    "疑似冻结后被悄悄更换指标"
                )
        elif kind == "能力变更声明" and material.declared_fingerprint:
            if material.declared_fingerprint != case.frozen.capability_fingerprint:
                conflict_reason = "材料声称的能力版本与揭榜冻结版本不一致"
        elif kind == "指标值申报":
            for key, value in material.payload.items():
                if key.startswith("指标:"):
                    indicator = key[3:]
                    declared = frozen_opp.fields.get(key)
                    if declared is not None and float(declared) != float(value):
                        conflict_reason = (
                            f"指标 {indicator} 的目标值被改写："
                            f"冻结值 {declared}，材料申报 {value}"
                        )
                        break
        data = {
            "match_id": match_id,
            "material": {
                "material_id": material.material_id,
                "submitter_id": material.submitter_id,
                "kind": material.kind,
                "payload": material.payload,
                "declared_fingerprint": material.declared_fingerprint,
            },
            "opportunity_fingerprint_now": opportunity_fingerprint(frozen_opp),
            "capability_fingerprint_now": capability_fingerprint(frozen_cap),
        }
        if conflict_reason is not None:
            data["conflict_reason"] = conflict_reason
            self._append("ReviewMaterialQuarantined", data)
            raise ConflictError(conflict_reason, material)
        self._append("ReviewMaterialAccepted", data)

    def resolve_quarantined(
        self, material_id: str, resolution: str, note: str
    ) -> None:
        """运营团队对隔离材料作出复核结论（维持冻结/另行协商）。"""

        if resolution not in ("维持冻结版本", "转范围协商"):
            raise DomainError("未知的隔离处置方式")
        if not any(q.material.material_id == material_id for q in self.quarantine):
            raise DomainError("隔离区中没有该材料")
        self._append(
            "QuarantineResolved",
            {
                "material_id": material_id,
                "resolution": resolution,
                "note": note,
            },
        )

    def review(self, match_id: str, reviewer_id: str, passed: bool, remark: str = "") -> MatchCase:
        case = self._require_case(match_id)
        if case.status != MatchStatus.CLAIMED:
            raise VersionStateError(f"当前状态 {case.status.value}，不能评审")
        reviewer = self.actors.get(reviewer_id)
        if reviewer is None or reviewer.role != ActorRole.REVIEWER:
            raise DomainError("评审必须由登记的评审人员作出")
        self._append(
            "ReviewPassed" if passed else "ReviewRejected",
            {
                "match_id": match_id,
                "reviewer_id": reviewer_id,
                "remark": remark,
            },
        )
        return case

    # ----- 协议与试点容量 -------------------------------------------------

    def sign_agreement(
        self,
        match_id: str,
        agreement_id: str,
        pool_id: str,
        milestone_specs: list[tuple[str, dict[str, float], int]],
    ) -> MatchCase:
        """签订协议：校验范围、原子占用容量、按冻结需求版本固化阶段指标。

        milestone_specs：[(阶段名, {指标键: 目标值}, 到期事件序号), ...]
        指标键必须存在于冻结的需求版本字段中（防止签约时换指标）。
        """

        case = self._require_case(match_id)
        if case.status != MatchStatus.CLAIMED or not case.review_passed:
            raise VersionStateError("须先评审通过，才能签订协议")
        pool = self.pools.get(pool_id)
        if pool is None:
            raise DomainError("试点资源池不存在")

        frozen_opp = self.frozen_opportunity(case)
        frozen_cap = self.frozen_capability(case)
        required = frozen_opp.required_scope
        if not frozen_cap.scope.covers(required):
            raise ScopeError(
                "能力适用范围不覆盖需求的数据范围或试点规模："
                f"需求 {sorted(required.data_domains)}/规模 {required.pilot_scale}"
                f"{required.scale_unit}，能力 {sorted(frozen_cap.scope.data_domains)}"
                f"/规模 {frozen_cap.scope.pilot_scale}{frozen_cap.scope.scale_unit}"
            )

        if not milestone_specs:
            raise DomainError("协议至少包含一个阶段")
        milestones: list[Milestone] = []
        last_due = 0
        for seq, (name, indicators, due_event) in enumerate(milestone_specs, start=1):
            for key, value in indicators.items():
                if key not in frozen_opp.fields:
                    raise MilestoneError(
                        f"阶段 {name} 的指标 {key} 不在冻结需求版本中"
                    )
                if float(frozen_opp.fields[key]) != float(value):
                    raise MilestoneError(
                        f"阶段 {name} 篡改了冻结指标 {key}："
                        f"{value} != {frozen_opp.fields[key]}"
                    )
            if due_event <= last_due:
                raise MilestoneError("里程碑到期时点必须严格递增")
            last_due = due_event
            milestones.append(
                Milestone(
                    milestone_id=f"{agreement_id}-S{seq}",
                    seq=seq,
                    name=name,
                    indicators=dict(indicators),
                    due_event=due_event,
                )
            )

        amount = required.pilot_scale
        if not pool.can_hold(amount):
            raise CapacityError(
                f"试点容量不足：需要 {amount}{required.scale_unit}，"
                f"剩余 {pool.available}{required.scale_unit}"
            )
        agreement = Agreement(
            agreement_id=agreement_id,
            signed_at_event=self.store.next_seq,
            allocated_scope=required,
            milestone_plan=tuple(milestones),
        )
        self._append(
            "AgreementSigned",
            {
                "match_id": match_id,
                "pool_id": pool_id,
                "agreement": {
                    "agreement_id": agreement_id,
                    "signed_at_event": agreement.signed_at_event,
                    "allocated_scope": _scope_to_data(required),
                    "milestone_plan": [_milestone_to_data(m) for m in milestones],
                },
                "hold_amount": amount,
            },
        )
        return case

    def request_scope_change(
        self,
        match_id: str,
        record_id: str,
        requested_by: str,
        change: dict[str, Any],
        reason: str,
    ) -> None:
        """揭榜后范围变化只形成协商记录，绝不回改冻结版本。"""

        case = self._require_case(match_id)
        if case.frozen is None:
            raise VersionStateError("尚未揭榜，不存在范围变更")
        if case.status in (MatchStatus.TERMINATED, MatchStatus.PUBLISHED):
            raise VersionStateError(f"{case.status.value} 的合作不能再变更范围")
        if requested_by not in (case.demander_id, case.provider_id):
            raise DomainError("只有供需双方可以发起范围协商")
        if any(r.record_id == record_id for r in case.negotiations):
            raise DomainError("协商记录编号重复")
        self._append(
            "NegotiationAdded",
            {
                "match_id": match_id,
                "record_id": record_id,
                "requested_by": requested_by,
                "change": change,
                "reason": reason,
                "accepted": False,
            },
        )

    def accept_scope_change(
        self, match_id: str, record_id: str, accepter_id: str, pool_id: str
    ) -> None:
        """双方接受后协商才生效；容量调整原子完成，失败不留中间态。"""

        case = self._require_case(match_id)
        record = self._find_negotiation(case, record_id)
        if record.accepted:
            raise DomainError("该协商已接受")
        if accepter_id not in (case.demander_id, case.provider_id):
            raise DomainError("只有供需双方可以接受范围协商")
        if accepter_id == record.requested_by:
            raise DomainError("发起方不能单方面接受，须由对方确认")
        change = dict(record.change)
        delta = 0
        if "pilot_scale" in change:
            target = int(change.pop("pilot_scale"))
            current = case.agreement.allocated_scope.pilot_scale
            delta = target - current
        extra_domains = change.pop("add_data_domains", [])
        if change:
            raise DomainError(f"不支持的范围变更项：{sorted(change)}")
        # 只做不落库的预校验：实际变更在事件应用时一次性完成，避免双扣。
        if delta:
            pool = self.pools[pool_id]
            held = pool.holdings.get(match_id)
            if held is None:
                raise CapacityError(f"撮合 {match_id} 没有占用记录")
            if held + delta < 0:
                raise CapacityError("缩容量不能超过已占用量")
            if delta > 0 and not pool.can_hold(delta):
                raise CapacityError(f"扩容后超出容量：剩余 {pool.available}")
        self._append(
            "NegotiationAccepted",
            {
                "match_id": match_id,
                "record_id": record_id,
                "accepter_id": accepter_id,
                "pool_id": pool_id if delta else None,
                "capacity_delta": delta,
                "add_data_domains": list(extra_domains),
            },
        )

    # ----- 里程碑评价、退出释放、终验 --------------------------------------

    def evaluate_milestone(
        self,
        match_id: str,
        milestone_seq: int,
        passed: bool,
        evaluator_ids: list[str],
        remark: str = "",
    ) -> MatchCase:
        case = self._require_case(match_id)
        agreement = case.agreement
        if agreement is None or case.status != MatchStatus.SIGNED:
            raise VersionStateError("协议未生效，不能评价阶段")
        milestones = agreement.milestone_plan
        target = next((m for m in milestones if m.seq == milestone_seq), None)
        if target is None:
            raise MilestoneError("里程碑不存在")
        if target.status in ("已通过", "未通过"):
            raise MilestoneError("已完成阶段的评价已封存，不能修改")
        if milestone_seq > 1:
            prev = milestones[milestone_seq - 2]
            if prev.status != "已通过":
                raise MilestoneError("必须按顺序评价，前置阶段尚未通过")

        roles = {self.actors[e].role for e in evaluator_ids if e in self.actors}
        if not evaluator_ids:
            raise EvaluationError("阶段评价必须有评价参与人")
        is_final = milestone_seq == len(milestones)
        only_demander = set(evaluator_ids) == {case.demander_id}
        if is_final and only_demander:
            raise EvaluationError("需求提出者不能单独确认最终成效，须有评审独立确认")
        if ActorRole.REVIEWER not in roles:
            raise EvaluationError("阶段评价必须有评审人员参与")
        if is_final and passed and case.demander_id not in evaluator_ids:
            raise EvaluationError("最终成效须由需求方与评审共同确认")

        self._append(
            "MilestoneEvaluated",
            {
                "match_id": match_id,
                "milestone_seq": milestone_seq,
                "passed": passed,
                "evaluator_ids": list(evaluator_ids),
                "remark": remark,
            },
        )
        return case

    def terminate(
        self,
        match_id: str,
        reason: str,
        pool_id: str,
        failed_milestone_seq: Optional[int] = None,
    ) -> int:
        """失败或退出：只释放尚未履行的容量，已通过阶段保留原评价。

        返回实际释放的容量。
        """

        case = self._require_case(match_id)
        if case.status != MatchStatus.SIGNED:
            raise VersionStateError(f"当前状态 {case.status.value}，无需退出释放")
        milestones = case.agreement.milestone_plan
        total = len(milestones)
        done = sum(1 for m in milestones if m.status == "已通过")
        held = self.pools[pool_id].holdings.get(match_id, 0)
        release_amount = math.ceil(held * (total - done) / total) if total else held

        self._append(
            "CooperationTerminated",
            {
                "match_id": match_id,
                "pool_id": pool_id,
                "reason": reason,
                "failed_milestone_seq": failed_milestone_seq,
                "release_amount": release_amount,
                "kept_done_milestones": done,
            },
        )
        return release_amount

    # ----- 案例发布 -------------------------------------------------------

    def authorize_publish(self, match_id: str, actor_id: str) -> None:
        case = self._require_case(match_id)
        if case.status != MatchStatus.COMPLETED:
            raise VersionStateError("只有已完成终验的合作才能授权发布案例")
        if actor_id == case.demander_id:
            party = "demander"
        elif actor_id == case.provider_id:
            party = "provider"
        else:
            raise AuthorizationError("只有供需双方可以授权案例发布")
        if getattr(case, f"{party}_authors_publish"):
            raise AuthorizationError("该方已授权，请勿重复操作")
        self._append(
            "PublishAuthorized",
            {"match_id": match_id, "party": party, "actor_id": actor_id},
        )

    def publish_case(self, match_id: str, case_id: str) -> dict[str, Any]:
        case = self._require_case(match_id)
        if case.status != MatchStatus.COMPLETED:
            raise VersionStateError("合作尚未完成，不能发布案例")
        if not (case.demander_authors_publish and case.provider_authors_publish):
            missing = []
            if not case.demander_authors_publish:
                missing.append("需求单位")
            if not case.provider_authors_publish:
                missing.append("能力主体")
            raise AuthorizationError(f"案例发布还缺少授权：{'、'.join(missing)}")
        if case_id in self.published_cases:
            raise DomainError("案例编号已存在")

        frozen_opp = self.frozen_opportunity(case)
        frozen_cap = self.frozen_capability(case)
        snapshot = {
            "case_id": case_id,
            "match_id": match_id,
            "title": f"{frozen_opp.title} × {frozen_cap.title}",
            "frozen_opportunity_version": frozen_opp.version,
            "frozen_capability_version": frozen_cap.version,
            "frozen_opportunity_fingerprint": case.frozen.opportunity_fingerprint,
            "frozen_capability_fingerprint": case.frozen.capability_fingerprint,
            "milestones": [
                _milestone_to_data(m) for m in case.agreement.milestone_plan
            ],
            "negotiations": [asdict(n) for n in case.negotiations],
            "demander_authorized": True,
            "provider_authorized": True,
            "replicable": True,
        }
        self._append(
            "CasePublished",
            {"match_id": match_id, "case_id": case_id, "snapshot": snapshot},
        )
        return snapshot

    # ----- 读模型 ---------------------------------------------------------

    def latest_opportunity(self, opportunity_id: str) -> Opportunity:
        history = self.opp_versions.get(opportunity_id)
        if not history:
            raise VersionStateError("机会不存在")
        return history[max(history)]

    def latest_capability(self, capability_id: str) -> Capability:
        history = self.cap_versions.get(capability_id)
        if not history:
            raise VersionStateError("能力不存在")
        return history[max(history)]

    def frozen_opportunity(self, case: MatchCase) -> Opportunity:
        return self.opp_versions[case.opportunity_id][case.frozen.opportunity_version]

    def frozen_capability(self, case: MatchCase) -> Capability:
        return self.cap_versions[case.capability_id][case.frozen.capability_version]

    def _require_case(self, match_id: str) -> MatchCase:
        case = self.matches.get(match_id)
        if case is None:
            raise DomainError("撮合单不存在")
        return case

    @staticmethod
    def _find_negotiation(
        case: MatchCase, record_id: str
    ) -> NegotiationRecord:
        record = next((n for n in case.negotiations if n.record_id == record_id), None)
        if record is None:
            raise DomainError("协商记录不存在")
        return record

    # ----- 恢复与续办 ------------------------------------------------------

    def recovery_worklist(self) -> dict[str, list[dict[str, Any]]]:
        """服务恢复后需要继续推进的事项。"""

        current_seq = len(self.store.events)
        pending_agreements: list[dict[str, Any]] = []
        overdue_milestones: list[dict[str, Any]] = []
        cases_awaiting_authorization: list[dict[str, Any]] = []

        for case in self.matches.values():
            if case.status == MatchStatus.CLAIMED:
                pending_agreements.append(
                    {
                        "match_id": case.match_id,
                        "review_passed": case.review_passed,
                        "frozen_opportunity_version": case.frozen.opportunity_version,
                        "frozen_capability_version": case.frozen.capability_version,
                    }
                )
            elif case.status == MatchStatus.SIGNED:
                for m in case.agreement.milestone_plan:
                    if m.status == "待履行" and m.due_event <= current_seq:
                        overdue_milestones.append(
                            {
                                "match_id": case.match_id,
                                "milestone_id": m.milestone_id,
                                "name": m.name,
                                "due_event": m.due_event,
                            }
                        )
            elif case.status == MatchStatus.COMPLETED and (
                case.demander_authors_publish != case.provider_authors_publish
            ):
                cases_awaiting_authorization.append(
                    {
                        "match_id": case.match_id,
                        "waiting_for": (
                            "能力主体授权"
                            if case.demander_authors_publish
                            else "需求单位授权"
                        ),
                    }
                )
        quarantined = [
            {
                "material_id": q.material.material_id,
                "reason": q.reason,
                "at_event": q.at_event,
            }
            for q in self.quarantine
            if q.material.material_id not in self._resolved_quarantine
        ]
        return {
            "待签协议": pending_agreements,
            "到期里程碑": overdue_milestones,
            "待补授权案例": cases_awaiting_authorization,
            "待复核隔离材料": quarantined,
        }

    # ----- 合作血缘追溯 ----------------------------------------------------

    def trace_cooperation(self, match_id: str) -> list[dict[str, Any]]:
        """按时间顺序还原一项合作：机会/能力版本 → 冻结 → 协议 → 阶段 → 案例。"""

        case = self._require_case(match_id)
        steps: list[dict[str, Any]] = []
        for event in self.store.events:
            data = event["data"]
            etype = event["type"]
            if etype in ("OpportunityPublished", "OpportunityWithdrawn"):
                if data["opportunity"]["opportunity_id"] == case.opportunity_id:
                    steps.append(self._explain(event))
            elif etype in ("CapabilityRegistered", "CapabilityWithdrawn"):
                if data["capability"]["capability_id"] == case.capability_id:
                    steps.append(self._explain(event))
            elif data.get("match_id") == match_id:
                steps.append(self._explain(event))
            elif etype == "CasePublished" and data.get("match_id") == match_id:
                steps.append(self._explain(event))
        return steps

    def explain_trace(self, match_id: str) -> str:
        """便于参与者阅读的文字版合作经过。"""

        return "\n".join(
            f"{step['seq']:>3}. [{step['at']}] {step['text']}"
            for step in self.trace_cooperation(match_id)
        )

    @staticmethod
    def _explain(event: dict[str, Any]) -> dict[str, Any]:
        etype = event["type"]
        data = event["data"]
        seq = event["seq"]
        texts = {
            "ActorRegistered": lambda: f"登记参与方 {data['name']}（{data['role']}）",
            "ResourcePoolAdded": lambda: f"设立试点资源池 {data['pool_id']}，容量 {data['total']}",
            "OpportunityPublished": lambda: (
                f"需求单位发布机会 v{data['opportunity']['version']}："
                f"{data['opportunity']['title']}"
            ),
            "OpportunityWithdrawn": lambda: (
                f"机会 v{data['opportunity']['version']} 撤下（已冻结合作不受影响）"
            ),
            "CapabilityRegistered": lambda: (
                f"能力主体登记能力 v{data['capability']['version']}："
                f"{data['capability']['title']}"
            ),
            "CapabilityWithdrawn": lambda: (
                f"能力 v{data['capability']['version']} 撤下（已冻结合作不受影响）"
            ),
            "DisclosureGranted": lambda: (
                f"{data['owner_id']} 向 {data['viewer_id']} "
                f"{'开放' if data['allowed'] else '收回'}字段 {data['field_key']}"
            ),
            "MatchSuggested": lambda: (
                f"按双方授权字段撮合（依据 {len(data['basis'])} 项）："
                f"机会 v{data['opportunity_version']} × 能力 v{data['capability_version']}"
            ),
            "Claimed": lambda: (
                "揭榜并冻结："
                f"需求 v{data['frozen']['opportunity_version']}"
                f"({data['frozen']['opportunity_fingerprint']})，"
                f"能力 v{data['frozen']['capability_version']}"
                f"({data['frozen']['capability_fingerprint']})"
            ),
            "ReviewMaterialAccepted": lambda: (
                f"评审材料 {data['material']['material_id']} 与冻结版本一致，进入评审"
            ),
            "ReviewMaterialQuarantined": lambda: (
                f"评审材料 {data['material']['material_id']} 与冻结事实冲突，先隔离："
                f"{data['conflict_reason']}"
            ),
            "QuarantineResolved": lambda: (
                f"隔离材料 {data['material_id']} 复核结论：{data['resolution']}"
            ),
            "ReviewPassed": lambda: f"评审人员 {data['reviewer_id']} 评审通过",
            "ReviewRejected": lambda: f"评审人员 {data['reviewer_id']} 评审未通过",
            "AgreementSigned": lambda: (
                f"签订协议 {data['agreement']['agreement_id']}，"
                f"占用试点容量 {data['hold_amount']}，"
                f"固化 {len(data['agreement']['milestone_plan'])} 个阶段指标"
            ),
            "NegotiationAdded": lambda: (
                f"{data['requested_by']} 发起范围协商（待对方接受）：{data['reason']}"
            ),
            "NegotiationAccepted": lambda: (
                f"范围协商生效，容量调整 {data['capacity_delta']:+d}"
                if data.get("capacity_delta")
                else "范围协商生效（数据范围调整，容量不变）"
            ),
            "MilestoneEvaluated": lambda: (
                f"阶段 {data['milestone_seq']} 评价"
                f"{'通过' if data['passed'] else '未通过'}，"
                f"评价人 {','.join(data['evaluator_ids'])}"
                + ("（最终成效）" if data.get("remark") == "最终阶段" else "")
            ),
            "CooperationTerminated": lambda: (
                f"合作终止：{data['reason']}；释放未履行容量 "
                f"{data['release_amount']}，已完成 {data['kept_done_milestones']} "
                "个阶段评价保留"
            ),
            "PublishAuthorized": lambda: (
                f"{'需求单位' if data['party'] == 'demander' else '能力主体'}授权案例发布"
            ),
            "CasePublished": lambda: f"双方授权齐备，发布可复制案例 {data['case_id']}",
        }
        return {"seq": seq, "at": seq, "type": etype, "text": texts.get(etype, lambda: etype)()}

    # ----- 内部：追加与应用事件 --------------------------------------------

    def _append(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        event = self.store.append(event_type, data)
        self._apply(event)
        return event

    def _apply(self, event: dict[str, Any]) -> None:
        etype = event["type"]
        data = event["data"]
        seq = event["seq"]
        handler = getattr(self, f"_on_{etype}", None)
        if handler is None:
            raise DomainError(f"未知事件类型：{etype}")
        handler(seq, data)

    # 以下 _on_* 只做状态迁移，不做校验（重放时事实已被命令校验过）。
    def _on_ActorRegistered(self, seq: int, d: dict[str, Any]) -> None:
        self.actors[d["actor_id"]] = Actor(
            actor_id=d["actor_id"], name=d["name"], role=ActorRole(d["role"])
        )

    def _on_ResourcePoolAdded(self, seq: int, d: dict[str, Any]) -> None:
        self.pools[d["pool_id"]] = ResourcePool(pool_id=d["pool_id"], total=d["total"])

    def _on_OpportunityPublished(self, seq: int, d: dict[str, Any]) -> None:
        opp = _opp_from_data(d["opportunity"])
        self.opp_versions.setdefault(opp.opportunity_id, {})[opp.version] = opp

    def _on_CapabilityRegistered(self, seq: int, d: dict[str, Any]) -> None:
        cap = _cap_from_data(d["capability"])
        self.cap_versions.setdefault(cap.capability_id, {})[cap.version] = cap

    def _on_OpportunityWithdrawn(self, seq: int, d: dict[str, Any]) -> None:
        self._on_OpportunityPublished(seq, d)

    def _on_CapabilityWithdrawn(self, seq: int, d: dict[str, Any]) -> None:
        self._on_CapabilityRegistered(seq, d)

    def _on_DisclosureGranted(self, seq: int, d: dict[str, Any]) -> None:
        self._grants_map[(d["owner_id"], d["viewer_id"], d["field_key"])] = d[
            "allowed"
        ]

    def _on_MatchSuggested(self, seq: int, d: dict[str, Any]) -> None:
        case = MatchCase(
            match_id=d["match_id"],
            opportunity_id=d["opportunity_id"],
            capability_id=d["capability_id"],
            demander_id=d["demander_id"],
            provider_id=d["provider_id"],
            status=MatchStatus.SUGGESTED,
            submission_key=d["submission_key"],
            created_event=seq,
        )
        self.matches[d["match_id"]] = case
        self.submission_index[d["submission_key"]] = d["match_id"]
        self._match_pools[d["match_id"]] = d["pool_id"]

    def _on_Claimed(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        f = d["frozen"]
        case.frozen = FrozenRef(
            opportunity_version=f["opportunity_version"],
            capability_version=f["capability_version"],
            opportunity_fingerprint=f["opportunity_fingerprint"],
            capability_fingerprint=f["capability_fingerprint"],
            frozen_at_event=f["frozen_at_event"],
        )
        case.status = MatchStatus.CLAIMED

    def _on_ReviewMaterialAccepted(self, seq: int, d: dict[str, Any]) -> None:
        self._material_log.setdefault(d["match_id"], []).append(
            {"material_id": d["material"]["material_id"], "state": "accepted"}
        )

    def _on_ReviewMaterialQuarantined(self, seq: int, d: dict[str, Any]) -> None:
        m = d["material"]
        material = Material(
            material_id=m["material_id"],
            submitter_id=m["submitter_id"],
            kind=m["kind"],
            payload=m["payload"],
            declared_fingerprint=m.get("declared_fingerprint"),
        )
        self.quarantine.append(
            QuarantineEntry(
                at_event=seq,
                reason=d["conflict_reason"],
                material=material,
                match_id=d["match_id"],
            )
        )
        self._material_log.setdefault(d["match_id"], []).append(
            {"material_id": m["material_id"], "state": "quarantined"}
        )

    def _on_QuarantineResolved(self, seq: int, d: dict[str, Any]) -> None:
        self._resolved_quarantine.add(d["material_id"])

    def _on_ReviewPassed(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        case.status = MatchStatus.CLAIMED  # 维持“已揭榜/待签约”，待签协议
        case.review_passed = True

    def _on_ReviewRejected(self, seq: int, d: dict[str, Any]) -> None:
        self.matches[d["match_id"]].status = MatchStatus.REJECTED

    def _on_AgreementSigned(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        a = d["agreement"]
        case.agreement = Agreement(
            agreement_id=a["agreement_id"],
            signed_at_event=a["signed_at_event"],
            allocated_scope=_scope_from_data(a["allocated_scope"]),
            milestone_plan=tuple(
                _milestone_from_data(m) for m in a["milestone_plan"]
            ),
        )
        case.status = MatchStatus.SIGNED
        pool = self.pools[d["pool_id"]]
        pool.hold(d["match_id"], d["hold_amount"])

    def _on_NegotiationAdded(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        case.negotiations.append(
            NegotiationRecord(
                record_id=d["record_id"],
                at_event=seq,
                requested_by=d["requested_by"],
                change=d["change"],
                reason=d["reason"],
                accepted=False,
            )
        )

    def _on_NegotiationAccepted(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        record = self._find_negotiation(case, d["record_id"])
        record.accepted = True
        record.accepted_by = (record.requested_by, d["accepter_id"])
        delta = d.get("capacity_delta") or 0
        if delta:
            pool = self.pools[d["pool_id"]]
            pool.adjust(d["match_id"], delta)
            new_scale = case.agreement.allocated_scope.pilot_scale + delta
            old = case.agreement
            case.agreement = Agreement(
                agreement_id=old.agreement_id,
                signed_at_event=old.signed_at_event,
                allocated_scope=Scope(
                    data_domains=old.allocated_scope.data_domains
                    | frozenset(d.get("add_data_domains", [])),
                    pilot_scale=new_scale,
                    scale_unit=old.allocated_scope.scale_unit,
                ),
                milestone_plan=old.milestone_plan,
            )
        elif d.get("add_data_domains"):
            old = case.agreement
            case.agreement = Agreement(
                agreement_id=old.agreement_id,
                signed_at_event=old.signed_at_event,
                allocated_scope=Scope(
                    data_domains=old.allocated_scope.data_domains
                    | frozenset(d["add_data_domains"]),
                    pilot_scale=old.allocated_scope.pilot_scale,
                    scale_unit=old.allocated_scope.scale_unit,
                ),
                milestone_plan=old.milestone_plan,
            )

    def _on_MilestoneEvaluated(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        milestone = case.agreement.milestone_plan[d["milestone_seq"] - 1]
        milestone.status = "已通过" if d["passed"] else "未通过"
        milestone.evaluated_by = tuple(d["evaluator_ids"])
        milestone.remark = d.get("remark", "")
        if d["passed"] and all(
            m.status == "已通过" for m in case.agreement.milestone_plan
        ):
            case.status = MatchStatus.COMPLETED

    def _on_CooperationTerminated(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        case.status = MatchStatus.TERMINATED
        if d.get("failed_milestone_seq") is not None:
            m = case.agreement.milestone_plan[d["failed_milestone_seq"] - 1]
            if m.status == "待履行":
                m.status = "未通过"
        for m in case.agreement.milestone_plan:
            if m.status == "待履行":
                m.status = "已免除"
        self.pools[d["pool_id"]].release(d["match_id"], d["release_amount"])

    def _on_PublishAuthorized(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        if d["party"] == "demander":
            case.demander_authors_publish = True
        else:
            case.provider_authors_publish = True

    def _on_CasePublished(self, seq: int, d: dict[str, Any]) -> None:
        case = self.matches[d["match_id"]]
        case.status = MatchStatus.PUBLISHED
        case.published_case_id = d["case_id"]
        self.published_cases[d["case_id"]] = d["snapshot"]
