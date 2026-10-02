"""供需撮合平台：把机会、能力、申报、揭榜、协议、协商、试点、验收、案例连成可追溯流程。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.matching.errors import (
    AuthorizationError,
    CapacityError,
    DomainError,
    DuplicateSubmission,
    StateError,
)
from src.matching.models import (
    AGREEMENT_PENDING,
    AGREEMENT_SIGNED,
    APPLICATION_SELECTED,
    APPLICATION_SUBMITTED,
    CASE_DRAFT,
    CASE_PUBLISHED,
    CASE_REVIEW,
    ENGAGEMENT_AGREED,
    ENGAGEMENT_COMPLETED,
    ENGAGEMENT_SELECTED,
    ENGAGEMENT_TERMINATED,
    EVAL_ACCEPTED,
    EVAL_REJECTED,
    MILESTONE_ACCEPTED,
    MILESTONE_PENDING,
    NEGOTIATION_AGREED,
    NEGOTIATION_PROPOSED,
    NEGOTIATION_REJECTED,
    RESOURCE_CONSUMED,
    RESOURCE_RELEASED,
    RESOURCE_RESERVED,
    VERSION_FROZEN,
    VERSION_OPEN,
    VERSION_SUPERSEDED,
    Application,
    Capability,
    CapabilityVersion,
    CaseFile,
    ChangeItem,
    Engagement,
    FieldEntry,
    MetricTarget,
    Milestone,
    NegotiationRecord,
    Opportunity,
    OpportunityVersion,
    Party,
    PilotAgreement,
    QuarantineItem,
    StageEvaluation,
)

DISCLOSURE_PUBLIC = "*"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class MatchingPlatform:
    """所有写操作遵循领域不变量；状态可整体快照到磁盘并恢复。"""

    def __init__(self) -> None:
        self.parties: dict[str, Party] = {}
        self.opportunities: dict[str, Opportunity] = {}
        self.capabilities: dict[str, Capability] = {}
        self.applications: dict[str, Application] = {}
        self.engagements: dict[str, Engagement] = {}
        self.cases: dict[str, CaseFile] = {}
        self.quarantine: dict[str, QuarantineItem] = {}
        self._application_seq = 0
        self._engagement_seq = 0
        self._negotiation_seq = 0
        self._quarantine_seq = 0

    # ---------- 参与方 ----------

    def register_party(self, party_id: str, name: str, role: str) -> Party:
        if party_id in self.parties:
            raise StateError(f"参与方 {party_id} 已注册")
        party = Party(id=party_id, name=name, role=role)
        self.parties[party_id] = party
        return party

    def _party(self, party_id: str) -> Party:
        try:
            return self.parties[party_id]
        except KeyError:
            raise StateError(f"参与方 {party_id} 不存在") from None

    # ---------- 机会版本 ----------

    def publish_opportunity(
        self,
        opportunity_id: str,
        demander_id: str,
        title: str,
        fields: list[FieldEntry],
        open_conditions: list[str],
        success_metrics: list[MetricTarget],
        at: str,
    ) -> OpportunityVersion:
        demander = self._party(demander_id)
        if demander.role not in ("demander", "regulator"):
            raise AuthorizationError("只有需求单位或行业主管部门可以发布场景机会")
        if not fields or not success_metrics:
            raise DomainError("机会的事实字段与成效指标不能为空")
        opportunity = self.opportunities.get(opportunity_id)
        if opportunity is None:
            opportunity = Opportunity(
                id=opportunity_id, demander_id=demander_id, title=title, versions=[]
            )
            self.opportunities[opportunity_id] = opportunity
        return self._append_opportunity_version(
            opportunity, fields, open_conditions, success_metrics, at
        )

    def revise_opportunity(
        self,
        opportunity_id: str,
        fields: list[FieldEntry],
        open_conditions: list[str],
        success_metrics: list[MetricTarget],
        at: str,
    ) -> OpportunityVersion:
        """发布新版本；未冻结的旧开放版本标记为已替代，已冻结版本永久保留。"""
        opportunity = self._opportunity(opportunity_id)
        if not fields or not success_metrics:
            raise DomainError("机会的事实字段与成效指标不能为空")
        return self._append_opportunity_version(
            opportunity, fields, open_conditions, success_metrics, at
        )

    def _append_opportunity_version(
        self,
        opportunity: Opportunity,
        fields: list[FieldEntry],
        open_conditions: list[str],
        success_metrics: list[MetricTarget],
        at: str,
    ) -> OpportunityVersion:
        if opportunity.versions and opportunity.current.status == VERSION_OPEN:
            opportunity.current.status = VERSION_SUPERSEDED
        version = OpportunityVersion(
            version_no=len(opportunity.versions) + 1,
            fields={entry.key: entry for entry in fields},
            open_conditions=list(open_conditions),
            success_metrics=list(success_metrics),
            created_at=at,
        )
        opportunity.versions.append(version)
        return version

    def _opportunity(self, opportunity_id: str) -> Opportunity:
        try:
            return self.opportunities[opportunity_id]
        except KeyError:
            raise StateError(f"场景机会 {opportunity_id} 不存在") from None

    # ---------- 能力版本 ----------

    def publish_capability(
        self,
        capability_id: str,
        provider_id: str,
        title: str,
        fields: list[FieldEntry],
        data_scope: str,
        capacity_total: int,
        at: str,
    ) -> CapabilityVersion:
        provider = self._party(provider_id)
        if provider.role != "provider":
            raise AuthorizationError("只有高校院所或技术企业可以发布技术能力")
        if not fields:
            raise DomainError("能力的事实字段不能为空")
        if not data_scope:
            raise DomainError("能力必须声明允许使用的数据范围")
        if capacity_total <= 0:
            raise DomainError("试点规模必须为正数")
        capability = self.capabilities.get(capability_id)
        if capability is None:
            capability = Capability(
                id=capability_id, provider_id=provider_id, title=title, versions=[]
            )
            self.capabilities[capability_id] = capability
        return self._append_capability_version(
            capability, fields, data_scope, capacity_total, at
        )

    def revise_capability(
        self,
        capability_id: str,
        fields: list[FieldEntry],
        data_scope: str,
        capacity_total: int,
        at: str,
    ) -> CapabilityVersion:
        """能力可升级为新版本；旧版本若已冻结则继续约束既有合作，新申报使用新版本。"""
        capability = self._capability(capability_id)
        if not fields or not data_scope:
            raise DomainError("能力字段与数据范围不能为空")
        if capacity_total <= 0:
            raise DomainError("试点规模必须为正数")
        return self._append_capability_version(
            capability, fields, data_scope, capacity_total, at
        )

    def _append_capability_version(
        self,
        capability: Capability,
        fields: list[FieldEntry],
        data_scope: str,
        capacity_total: int,
        at: str,
    ) -> CapabilityVersion:
        if capability.versions and capability.current.status == VERSION_OPEN:
            capability.current.status = VERSION_SUPERSEDED
        version = CapabilityVersion(
            version_no=len(capability.versions) + 1,
            fields={entry.key: entry for entry in fields},
            data_scope=data_scope,
            capacity_total=capacity_total,
            created_at=at,
        )
        capability.versions.append(version)
        return version

    def _capability(self, capability_id: str) -> Capability:
        try:
            return self.capabilities[capability_id]
        except KeyError:
            raise StateError(f"技术能力 {capability_id} 不存在") from None

    # ---------- 授权可见字段与撮合 ----------

    @staticmethod
    def _visible_fields(
        fields: dict[str, FieldEntry], viewer_id: str
    ) -> dict[str, FieldEntry]:
        """匹配只能使用拥有方同意披露给查看方的字段。"""
        return {
            key: entry
            for key, entry in fields.items()
            if DISCLOSURE_PUBLIC in entry.disclose_to or viewer_id in entry.disclose_to
        }

    @staticmethod
    def _tags(visible: dict[str, FieldEntry]) -> set[str]:
        tags: set[str] = set()
        entry = visible.get("tags")
        if entry is not None and isinstance(entry.value, list):
            tags.update(str(tag) for tag in entry.value)
        return tags

    def match_candidates(self, opportunity_id: str, viewer_id: str) -> list[dict[str, Any]]:
        """以查看方身份撮合。需求侧痛点未授权给该能力主体时，绝不参与计算。"""
        self._party(viewer_id)
        opportunity = self._opportunity(opportunity_id)
        version = opportunity.current
        if version.status not in (VERSION_OPEN, VERSION_FROZEN):
            return []
        visible_opp = self._visible_fields(version.fields, viewer_id)
        opp_tags = self._tags(visible_opp)
        candidates: list[dict[str, Any]] = []
        for capability in self.capabilities.values():
            cap_version = capability.current
            if cap_version.status not in (VERSION_OPEN, VERSION_FROZEN):
                continue
            visible_cap = self._visible_fields(cap_version.fields, opportunity.demander_id)
            cap_tags = self._tags(visible_cap)
            shared = sorted(opp_tags & cap_tags)
            if not shared:
                continue
            candidates.append(
                {
                    "capability_id": capability.id,
                    "capability_version_no": cap_version.version_no,
                    "provider_id": capability.provider_id,
                    "shared_tags": shared,
                    "visible_opportunity_fields": sorted(visible_opp),
                    "visible_capability_fields": sorted(visible_cap),
                    "data_scope": cap_version.data_scope,
                }
            )
        return candidates

    # ---------- 解决方案申报（幂等） ----------

    @staticmethod
    def _submission_fingerprint(
        opportunity_id: str,
        capability_id: str,
        provider_id: str,
        summary: str,
        materials: dict[str, Any],
    ) -> str:
        return _sha256(
            {
                "opportunity_id": opportunity_id,
                "capability_id": capability_id,
                "provider_id": provider_id,
                "summary": summary,
                "materials": materials,
            }
        )

    def submit_solution(
        self,
        opportunity_id: str,
        capability_id: str,
        provider_id: str,
        summary: str,
        materials: dict[str, Any],
        at: str,
    ) -> Application:
        opportunity = self._opportunity(opportunity_id)
        capability = self._capability(capability_id)
        provider = self._party(provider_id)
        if provider.role != "provider":
            raise AuthorizationError("只有能力主体可以提交解决方案")
        if capability.provider_id != provider_id:
            raise AuthorizationError("不能替其他主体申报能力")
        opp_version = opportunity.current
        cap_version = capability.current
        if opp_version.status not in (VERSION_OPEN, VERSION_FROZEN):
            raise StateError("机会当前版本不在可申报状态")
        if cap_version.status not in (VERSION_OPEN, VERSION_FROZEN):
            raise StateError("能力当前版本不在可申报状态")

        # 同一业务组合的授权撮合关系仍须成立：未授权字段不能进入申报。
        candidates = {
            c["capability_id"]: c
            for c in self.match_candidates(opportunity_id, provider_id)
        }
        if capability_id not in candidates:
            raise AuthorizationError("双方同意披露的字段不足以形成匹配")

        fingerprint = self._submission_fingerprint(
            opportunity_id, capability_id, provider_id, summary, materials
        )
        for existing in self.applications.values():
            if existing.fingerprint == fingerprint:
                # 重复申报返回既有结果，不重复建单、不重复占用任何资源。
                raise DuplicateSubmission(fingerprint, existing.id)

        self._application_seq += 1
        application = Application(
            id=f"app-{self._application_seq:04d}",
            fingerprint=fingerprint,
            opportunity_id=opportunity_id,
            opportunity_version_no=opp_version.version_no,
            capability_id=capability_id,
            capability_version_no=cap_version.version_no,
            provider_id=provider_id,
            summary=summary,
            materials=dict(materials),
            created_at=at,
            match_basis=candidates[capability_id],
        )
        self.applications[application.id] = application
        return application

    def get_application_by_fingerprint(self, fingerprint: str) -> Application | None:
        for application in self.applications.values():
            if application.fingerprint == fingerprint:
                return application
        return None

    # ---------- 揭榜：冻结版本 + 容量预留 ----------

    def award(
        self,
        application_id: str,
        milestone_plan: list[dict[str, Any]],
        at: str,
    ) -> Engagement:
        application = self._application(application_id)
        if application.status != APPLICATION_SUBMITTED:
            raise StateError("申报已处理，不能重复揭榜")
        opportunity = self._opportunity(application.opportunity_id)
        capability = self._capability(application.capability_id)
        opp_version = opportunity.versions[application.opportunity_version_no - 1]
        cap_version = capability.versions[application.capability_version_no - 1]

        if not milestone_plan:
            raise DomainError("揭榜必须给出阶段里程碑计划")
        milestones: list[Milestone] = []
        total_units = 0
        for seq, plan in enumerate(milestone_plan, start=1):
            units = int(plan["reserved_units"])
            if units <= 0:
                raise DomainError("阶段预留资源必须为正数")
            total_units += units
            milestones.append(
                Milestone(
                    seq=seq,
                    name=str(plan["name"]),
                    due_date=str(plan["due_date"]),
                    reserved_units=units,
                    metrics=[MetricTarget(**metric) for metric in plan.get("metrics", [])],
                )
            )
        if not milestones[0].metrics:
            # 未单独给出阶段指标时，以揭榜版本冻结的成效指标贯穿首阶段。
            milestones[0].metrics = list(opp_version.success_metrics)

        # 容量池按（能力 + 冻结版本）隔离：同一能力可同时回应多个场景，
        # 但同一版本声明的试点规模不得被重复占用。
        used = self.capacity_used(capability.id, cap_version.version_no)
        if used + total_units > cap_version.capacity_total:
            raise CapacityError(
                f"试点容量不足：需要 {total_units}，已占用 {used}，"
                f"总规模 {cap_version.capacity_total}"
            )

        self._engagement_seq += 1
        engagement = Engagement(
            id=f"eng-{self._engagement_seq:04d}",
            application_id=application.id,
            opportunity_id=opportunity.id,
            capability_id=capability.id,
            demander_id=opportunity.demander_id,
            provider_id=application.provider_id,
            capacity_units=total_units,
            frozen_opportunity={
                "opportunity_id": opportunity.id,
                "title": opportunity.title,
                "version": opp_version.to_dict(),
            },
            frozen_capability={
                "capability_id": capability.id,
                "title": capability.title,
                "version": cap_version.to_dict(),
            },
            milestones=milestones,
            created_at=at,
        )
        self.engagements[engagement.id] = engagement
        application.status = APPLICATION_SELECTED
        application.engagement_id = engagement.id
        opp_version.status = VERSION_FROZEN
        cap_version.status = VERSION_FROZEN
        return engagement

    def _application(self, application_id: str) -> Application:
        try:
            return self.applications[application_id]
        except KeyError:
            raise StateError(f"申报 {application_id} 不存在") from None

    def _engagement(self, engagement_id: str) -> Engagement:
        try:
            return self.engagements[engagement_id]
        except KeyError:
            raise StateError(f"揭榜合作 {engagement_id} 不存在") from None

    def capacity_used(self, capability_id: str, version_no: int) -> int:
        """已预留 + 已履行的资源都算占用；已释放的未履行资源不再占用。"""
        used = 0
        for engagement in self.engagements.values():
            if (
                engagement.capability_id != capability_id
                or engagement.frozen_capability["version"]["version_no"] != version_no
            ):
                continue
            if engagement.state == ENGAGEMENT_TERMINATED:
                # 终止合作中只剩已履行部分继续占位。
                used += sum(
                    m.reserved_units
                    for m in engagement.milestones
                    if m.resource_status == RESOURCE_CONSUMED
                )
            else:
                used += sum(
                    m.reserved_units
                    for m in engagement.milestones
                    if m.resource_status in (RESOURCE_RESERVED, RESOURCE_CONSUMED)
                )
        return used

    def capacity_available(self, capability_id: str, version_no: int) -> int:
        capability = self._capability(capability_id)
        total = capability.versions[version_no - 1].capacity_total
        return total - self.capacity_used(capability_id, version_no)

    # ---------- 试点协议 ----------

    @staticmethod
    def agreement_hash(engagement: Engagement) -> str:
        """协议内容覆盖冻结版本与阶段指标，任何一方都无法悄悄更换。"""
        return _sha256(
            {
                "frozen_opportunity_version": engagement.frozen_opportunity["version"]["version_no"],
                "frozen_capability_version": engagement.frozen_capability["version"]["version_no"],
                "success_metrics": engagement.frozen_opportunity["version"]["success_metrics"],
                "data_scope": engagement.frozen_capability["version"]["data_scope"],
                "milestones": [
                    {
                        "seq": m.seq,
                        "name": m.name,
                        "due_date": m.due_date,
                        "reserved_units": m.reserved_units,
                        "metrics": [metric.to_dict() for metric in m.metrics],
                    }
                    for m in engagement.milestones
                ],
            }
        )

    def sign_agreement(
        self, engagement_id: str, party_id: str, content_hash: str, at: str
    ) -> PilotAgreement:
        engagement = self._engagement(engagement_id)
        if engagement.state != ENGAGEMENT_SELECTED:
            raise StateError("只有待签协议的揭榜合作可以签署")
        if party_id not in (engagement.demander_id, engagement.provider_id):
            raise AuthorizationError("只有协议双方可以签署")
        expected = self.agreement_hash(engagement)
        if content_hash != expected:
            raise StateError("协议内容与冻结的需求、能力版本及阶段指标不一致")
        agreement = engagement.agreement
        if agreement.status == AGREEMENT_PENDING and not agreement.content_hash:
            agreement.content_hash = content_hash
        if party_id == engagement.demander_id:
            if agreement.demander_signed_at is not None:
                raise StateError("需求方已签署，不能重复签署")
            agreement.demander_signed_at = at
        else:
            if agreement.provider_signed_at is not None:
                raise StateError("能力方已签署，不能重复签署")
            agreement.provider_signed_at = at
        if agreement.demander_signed_at and agreement.provider_signed_at:
            agreement.status = AGREEMENT_SIGNED
            engagement.state = ENGAGEMENT_AGREED
        return agreement

    # ---------- 范围变化：协商记录 ----------

    def propose_change(
        self,
        engagement_id: str,
        requested_by: str,
        reason: str,
        changes: list[dict[str, str]],
        at: str,
    ) -> NegotiationRecord:
        engagement = self._engagement(engagement_id)
        if engagement.state not in (ENGAGEMENT_SELECTED, ENGAGEMENT_AGREED):
            raise StateError("当前合作状态不接受范围变更")
        if requested_by not in (engagement.demander_id, engagement.provider_id):
            raise AuthorizationError("只有协议双方可以提出范围变更")
        items = [ChangeItem(**change) for change in changes]
        for item in items:
            self._locate_change_target(engagement, item.path)
        self._negotiation_seq += 1
        record = NegotiationRecord(
            id=f"neg-{self._negotiation_seq:04d}",
            seq=self._negotiation_seq,
            requested_by=requested_by,
            reason=reason,
            changes=items,
            responses={requested_by: True},  # 提出方提议即视为本方同意
            created_at=at,
        )
        engagement.negotiations.append(record)
        return record

    @staticmethod
    def _locate_change_target(engagement: Engagement, path: str) -> tuple[Milestone, str]:
        # 仅允许形如 milestone.2.reserved_units / milestone.2.due_date 的路径
        parts = path.split(".")
        if len(parts) != 3 or parts[0] != "milestone":
            raise StateError(f"不支持的变更路径 {path}")
        milestone = engagement.milestone(int(parts[1]))
        if parts[2] not in ("reserved_units", "due_date"):
            raise StateError(f"不支持的变更路径 {path}")
        return milestone, parts[2]

    def respond_negotiation(
        self, engagement_id: str, negotiation_id: str, party_id: str, accept: bool, at: str
    ) -> NegotiationRecord:
        engagement = self._engagement(engagement_id)
        if party_id not in (engagement.demander_id, engagement.provider_id):
            raise AuthorizationError("只有协议双方可以响应协商")
        record = next((n for n in engagement.negotiations if n.id == negotiation_id), None)
        if record is None:
            raise StateError(f"协商记录 {negotiation_id} 不存在")
        if record.status != NEGOTIATION_PROPOSED:
            raise StateError("协商已有结论")
        record.responses[party_id] = accept
        other_pending = (
            engagement.demander_id not in record.responses
            or engagement.provider_id not in record.responses
        )
        if other_pending:
            return record
        if not all(record.responses.values()):
            record.status = NEGOTIATION_REJECTED
            record.decided_at = at
            return record
        # 双方一致同意：应用变更，冻结快照保持揭榜时原样，变更经过由记录承载。
        for item in record.changes:
            milestone, attr = self._locate_change_target(engagement, item.path)
            if attr == "reserved_units":
                if milestone.resource_status != RESOURCE_RESERVED:
                    raise StateError("已履行资源不可调整")
                if milestone.reserved_units != int(item.before):
                    raise StateError("变更前值与当前预留不符，需重新协商")
                delta = int(item.after) - int(item.before)
                version_no = engagement.frozen_capability["version"]["version_no"]
                cap = self._capability(engagement.capability_id)
                total = cap.versions[version_no - 1].capacity_total
                if self.capacity_used(engagement.capability_id, version_no) + delta > total:
                    raise CapacityError("变更后的资源需求超出试点容量")
                milestone.reserved_units = int(item.after)
                engagement.capacity_units += delta
            else:
                if milestone.due_date != item.before:
                    raise StateError("变更前值与当前到期日不符，需重新协商")
                setattr(milestone, attr, item.after)
        record.status = NEGOTIATION_AGREED
        record.decided_at = at
        return record

    # ---------- 阶段上报与独立评审 ----------

    def report_stage(
        self, engagement_id: str, seq: int, results: dict[str, str], at: str
    ) -> Milestone:
        engagement = self._engagement(engagement_id)
        if engagement.state != ENGAGEMENT_AGREED:
            raise StateError("只有试点中的合作可以上报阶段成果")
        milestone = engagement.milestone(seq)
        if milestone.status != MILESTONE_PENDING:
            raise StateError("该阶段已有定稿评价")
        required = {metric.name for metric in milestone.metrics}
        if set(results) != required:
            raise StateError("上报结果必须逐项对应阶段指标")
        milestone.reported = dict(results)
        return milestone

    def evaluate_stage(
        self,
        engagement_id: str,
        seq: int,
        reviewer_id: str,
        verdict: str,
        note: str,
        at: str,
    ) -> Milestone:
        """阶段验收由独立评审作出；需求提出者不能确认自己的成效。"""
        engagement = self._engagement(engagement_id)
        reviewer = self._party(reviewer_id)
        if reviewer.role != "reviewer":
            raise AuthorizationError("阶段验收必须由独立试点评审人员作出")
        if reviewer_id in (engagement.demander_id, engagement.provider_id):
            raise AuthorizationError("需求方或能力方不能作为本合作的评审人")
        if verdict not in (EVAL_ACCEPTED, EVAL_REJECTED):
            raise DomainError("评审结论只能是 accepted 或 rejected")
        milestone = engagement.milestone(seq)
        if milestone.reported is None:
            raise StateError("尚无阶段上报，无法评审")
        if milestone.status == MILESTONE_ACCEPTED:
            raise StateError("已通过的阶段评价不可更改")
        evaluation = StageEvaluation(
            reviewer_id=reviewer_id,
            results=dict(milestone.reported),
            verdict=verdict,
            note=note,
            evaluated_at=at,
        )
        milestone.evaluation = evaluation
        if verdict == EVAL_ACCEPTED:
            milestone.status = MILESTONE_ACCEPTED
            milestone.accepted_at = at
            # 已履行：预留容量转为已消耗，失败或退出时不再释放。
            milestone.resource_status = RESOURCE_CONSUMED
            if all(m.status == MILESTONE_ACCEPTED for m in engagement.milestones):
                engagement.state = ENGAGEMENT_COMPLETED
        return milestone

    # ---------- 失败 / 退出 ----------

    def terminate(self, engagement_id: str, reason: str, at: str) -> Engagement:
        """失败或退出只释放尚未履行的资源；已完成阶段保留原评价。"""
        engagement = self._engagement(engagement_id)
        if engagement.state in (ENGAGEMENT_COMPLETED, ENGAGEMENT_TERMINATED):
            raise StateError("合作已结束，不能再次终止")
        for milestone in engagement.milestones:
            if milestone.resource_status == RESOURCE_RESERVED:
                milestone.resource_status = RESOURCE_RELEASED
        engagement.state = ENGAGEMENT_TERMINATED
        engagement.terminated_at = at
        engagement.terminate_reason = reason
        return engagement

    # ---------- 案例发布（双方授权 + 复核） ----------

    def create_case(
        self, case_id: str, engagement_id: str, title: str, summary: str
    ) -> CaseFile:
        engagement = self._engagement(engagement_id)
        if engagement.state != ENGAGEMENT_COMPLETED:
            raise StateError("只有全部阶段验收通过的合作才能形成案例")
        if case_id in self.cases:
            raise StateError(f"案例 {case_id} 已存在")
        case_file = CaseFile(
            id=case_id,
            engagement_id=engagement_id,
            title=title,
            summary=summary,
        )
        self.cases[case_id] = case_file
        return case_file

    def authorize_publish(self, case_id: str, party_id: str, at: str) -> CaseFile:
        case_file = self._case(case_id)
        engagement = self._engagement(case_file.engagement_id)
        if party_id not in (engagement.demander_id, engagement.provider_id):
            raise AuthorizationError("只有合作双方可以授权发布案例")
        if party_id in case_file.publish_grants:
            raise StateError("该方已授权")
        case_file.publish_grants[party_id] = at
        return case_file

    def submit_case_for_review(self, case_id: str, at: str) -> CaseFile:
        case_file = self._case(case_id)
        engagement = self._engagement(case_file.engagement_id)
        required = {engagement.demander_id, engagement.provider_id}
        if not required.issubset(case_file.publish_grants):
            raise AuthorizationError("案例发布必须取得需求方与能力方双方授权")
        if case_file.status != CASE_DRAFT:
            raise StateError("案例已提交复核")
        case_file.status = CASE_REVIEW
        case_file.submitted_for_review_at = at
        return case_file

    def review_case(
        self, case_id: str, reviewer_id: str, verdict: str, note: str, at: str
    ) -> CaseFile:
        case_file = self._case(case_id)
        engagement = self._engagement(case_file.engagement_id)
        reviewer = self._party(reviewer_id)
        if reviewer.role != "reviewer" or reviewer_id in (
            engagement.demander_id,
            engagement.provider_id,
        ):
            raise AuthorizationError("案例复核必须由独立评审人员作出")
        if case_file.status != CASE_REVIEW:
            raise StateError("案例不在复核环节")
        case_file.review = StageEvaluation(
            reviewer_id=reviewer_id,
            results={},
            verdict=verdict,
            note=note,
            evaluated_at=at,
        )
        return case_file

    def publish_case(self, case_id: str, at: str) -> CaseFile:
        case_file = self._case(case_id)
        engagement = self._engagement(case_file.engagement_id)
        required = {engagement.demander_id, engagement.provider_id}
        if not required.issubset(case_file.publish_grants):
            raise AuthorizationError("案例发布必须取得双方授权")
        if case_file.review is None or case_file.review.verdict != EVAL_ACCEPTED:
            raise StateError("案例尚未通过独立复核")
        if case_file.status == CASE_PUBLISHED:
            raise StateError("案例已发布")
        case_file.status = CASE_PUBLISHED
        case_file.published_at = at
        return case_file

    def _case(self, case_id: str) -> CaseFile:
        try:
            return self.cases[case_id]
        except KeyError:
            raise StateError(f"案例 {case_id} 不存在") from None

    # ---------- 冲突材料隔离 ----------

    def quarantine_material(
        self, kind: str, payload: dict[str, Any], reason: str, at: str
    ) -> QuarantineItem:
        self._quarantine_seq += 1
        item = QuarantineItem(
            id=f"q-{self._quarantine_seq:04d}",
            kind=kind,
            payload=dict(payload),
            reason=reason,
            at=at,
        )
        self.quarantine[item.id] = item
        return item

    def resolve_quarantine(self, item_id: str, resolution: str) -> QuarantineItem:
        try:
            item = self.quarantine[item_id]
        except KeyError:
            raise StateError(f"隔离材料 {item_id} 不存在") from None
        item.resolved = resolution
        return item

    # ---------- 服务恢复：继续未竟事项 ----------

    def resume_queue(self, as_of: str) -> dict[str, Any]:
        """服务恢复后继续：待签协议、到期里程碑、案例复核与未处理的隔离材料。"""
        return {
            "pending_agreements": [
                e.id
                for e in self.engagements.values()
                if e.state == ENGAGEMENT_SELECTED
            ],
            "due_milestones": [
                {"engagement_id": e.id, "seq": m.seq, "due_date": m.due_date}
                for e in self.engagements.values()
                if e.state == ENGAGEMENT_AGREED
                for m in e.milestones
                if m.status == MILESTONE_PENDING and m.due_date <= as_of
            ],
            "cases_in_review": [
                c.id for c in self.cases.values() if c.status == CASE_REVIEW
            ],
            "quarantined": [
                q.id for q in self.quarantine.values() if q.resolved is None
            ],
        }

    # ---------- 全链路追溯 ----------

    def explain_collaboration(self, engagement_id: str) -> dict[str, Any]:
        """把一项合作从机会与能力还原到可复制案例的完整轨迹。"""
        engagement = self._engagement(engagement_id)
        application = self.applications[engagement.application_id]
        timeline: list[dict[str, Any]] = [
            {
                "at": application.created_at,
                "event": "solution_submitted",
                "detail": {
                    "application_id": application.id,
                    "match_basis": application.match_basis,
                },
            },
            {
                "at": engagement.created_at,
                "event": "awarded_versions_frozen",
                "detail": {
                    "opportunity_version": engagement.frozen_opportunity["version"]["version_no"],
                    "capability_version": engagement.frozen_capability["version"]["version_no"],
                    "capacity_units": engagement.capacity_units,
                },
            },
        ]
        agreement = engagement.agreement
        if agreement.content_hash:
            timeline.append(
                {
                    "at": agreement.demander_signed_at or agreement.provider_signed_at,
                    "event": "agreement_circulated",
                    "detail": {"content_hash": agreement.content_hash},
                }
            )
        for record in engagement.negotiations:
            timeline.append(
                {
                    "at": record.created_at,
                    "event": "change_proposed",
                    "detail": {
                        "negotiation_id": record.id,
                        "requested_by": record.requested_by,
                        "reason": record.reason,
                        "changes": [c.to_dict() for c in record.changes],
                        "status": record.status,
                    },
                }
            )
        for milestone in engagement.milestones:
            if milestone.reported is not None:
                timeline.append(
                    {
                        "at": milestone.accepted_at or "",
                        "event": "stage_reported",
                        "detail": {"seq": milestone.seq, "results": milestone.reported},
                    }
                )
            if milestone.evaluation is not None:
                timeline.append(
                    {
                        "at": milestone.evaluation.evaluated_at,
                        "event": "stage_evaluated",
                        "detail": {
                            "seq": milestone.seq,
                            "reviewer_id": milestone.evaluation.reviewer_id,
                            "verdict": milestone.evaluation.verdict,
                            "resource_status": milestone.resource_status,
                        },
                    }
                )
        if engagement.state == ENGAGEMENT_TERMINATED:
            timeline.append(
                {
                    "at": engagement.terminated_at,
                    "event": "terminated",
                    "detail": {
                        "reason": engagement.terminate_reason,
                        "released_units": sum(
                            m.reserved_units
                            for m in engagement.milestones
                            if m.resource_status == RESOURCE_RELEASED
                        ),
                        "kept_evaluations": [
                            m.seq
                            for m in engagement.milestones
                            if m.status == MILESTONE_ACCEPTED
                        ],
                    },
                }
            )
        case_files = [c for c in self.cases.values() if c.engagement_id == engagement_id]
        for case_file in case_files:
            timeline.append(
                {
                    "at": case_file.published_at or case_file.submitted_for_review_at or "",
                    "event": "case_progress",
                    "detail": {
                        "case_id": case_file.id,
                        "status": case_file.status,
                        "grants": case_file.publish_grants,
                        "review": None
                        if case_file.review is None
                        else {
                            "reviewer_id": case_file.review.reviewer_id,
                            "verdict": case_file.review.verdict,
                        },
                    },
                }
            )
        return {
            "engagement_id": engagement.id,
            "state": engagement.state,
            "frozen_opportunity": engagement.frozen_opportunity,
            "frozen_capability": engagement.frozen_capability,
            "agreement": agreement.to_dict(),
            "timeline": timeline,
        }

    # ---------- 快照持久化与恢复 ----------

    def save(self, path: str | Path) -> None:
        snapshot = {
            "parties": {k: v.to_dict() for k, v in self.parties.items()},
            "opportunities": {k: v.to_dict() for k, v in self.opportunities.items()},
            "capabilities": {k: v.to_dict() for k, v in self.capabilities.items()},
            "applications": {k: v.to_dict() for k, v in self.applications.items()},
            "engagements": {k: v.to_dict() for k, v in self.engagements.items()},
            "cases": {k: v.to_dict() for k, v in self.cases.items()},
            "quarantine": {k: v.to_dict() for k, v in self.quarantine.items()},
            "sequences": {
                "application": self._application_seq,
                "engagement": self._engagement_seq,
                "negotiation": self._negotiation_seq,
                "quarantine": self._quarantine_seq,
            },
        }
        Path(path).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> "MatchingPlatform":
        snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
        platform = cls()
        platform.parties = {
            k: Party.from_dict(v) for k, v in snapshot["parties"].items()
        }
        platform.opportunities = {
            k: Opportunity.from_dict(v) for k, v in snapshot["opportunities"].items()
        }
        platform.capabilities = {
            k: Capability.from_dict(v) for k, v in snapshot["capabilities"].items()
        }
        platform.applications = {
            k: Application.from_dict(v) for k, v in snapshot["applications"].items()
        }
        platform.engagements = {
            k: Engagement.from_dict(v) for k, v in snapshot["engagements"].items()
        }
        platform.cases = {k: CaseFile.from_dict(v) for k, v in snapshot["cases"].items()}
        platform.quarantine = {
            k: QuarantineItem.from_dict(v) for k, v in snapshot["quarantine"].items()
        }
        sequences = snapshot["sequences"]
        platform._application_seq = sequences["application"]
        platform._engagement_seq = sequences["engagement"]
        platform._negotiation_seq = sequences["negotiation"]
        platform._quarantine_seq = sequences["quarantine"]
        return platform
