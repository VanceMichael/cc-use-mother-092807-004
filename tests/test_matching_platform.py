"""供需撮合后端的端到端规则测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.matching import (
    AGREEMENT_PENDING,
    CASE_PUBLISHED,
    CASE_REVIEW,
    ENGAGEMENT_AGREED,
    ENGAGEMENT_COMPLETED,
    ENGAGEMENT_SELECTED,
    ENGAGEMENT_TERMINATED,
    MILESTONE_ACCEPTED,
    NEGOTIATION_AGREED,
    RESOURCE_CONSUMED,
    RESOURCE_RELEASED,
    RESOURCE_RESERVED,
    MatchingPlatform,
)
from src.matching.errors import (
    AuthorizationError,
    CapacityError,
    DomainError,
    DuplicateSubmission,
    StateError,
)
from src.matching.models import (
    EVAL_ACCEPTED,
    EVAL_REJECTED,
    VERSION_FROZEN,
    VERSION_OPEN,
    VERSION_SUPERSEDED,
    FieldEntry,
    MetricTarget,
)


def _platform() -> MatchingPlatform:
    platform = MatchingPlatform()
    platform.register_party("op", "场景运营团队", "operator")
    platform.register_party("dem", "某行业主管部门", "regulator")
    platform.register_party("dem2", "某需求单位", "demander")
    platform.register_party("prov", "某技术企业", "provider")
    platform.register_party("prov2", "某高校院所", "provider")
    platform.register_party("rev", "独立评审组", "reviewer")
    return platform


def _opportunity_fields(real_pain_disclose_to: list[str] | None = None) -> list[FieldEntry]:
    return [
        FieldEntry("title", "城市路网安全监测", ["*"]),
        FieldEntry("tags", ["视频识别", "边缘计算"], ["*"]),
        # 真实痛点默认不公开：行业主管部门担心公开太早泄露痛点。
        FieldEntry("real_pain", "重点桥梁病害发现率不足", real_pain_disclose_to or []),
    ]


def _capability_fields(tags: list[str] | None = None) -> list[FieldEntry]:
    return [
        FieldEntry("title", "边缘视觉识别能力", ["*"]),
        FieldEntry("tags", tags or ["视频识别", "边缘计算"], ["*"]),
        FieldEntry("pricing", "试点期免费，商业期另议", ["dem", "dem2"]),
    ]


def _metrics() -> list[MetricTarget]:
    return [MetricTarget("病害发现率", ">=95%", "桥梁监测脱敏视频")]


def _milestone_plan() -> list[dict]:
    return [
        {
            "seq": 1,
            "name": "实验室联调",
            "due_date": "2026-11-01",
            "reserved_units": 2,
            "metrics": [
                {"name": "病害发现率", "target": ">=90%", "data_scope": "样本库A"}
            ],
        },
        {
            "seq": 2,
            "name": "现场试点",
            "due_date": "2026-12-15",
            "reserved_units": 3,
            "metrics": [
                {"name": "病害发现率", "target": ">=95%", "data_scope": "桥梁监测脱敏视频"}
            ],
        },
    ]


class DisclosureAndMatchingTest(unittest.TestCase):
    def test_matching_uses_only_mutually_disclosed_fields(self) -> None:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "城市路网安全监测", _opportunity_fields(),
            ["在3座试点桥梁部署"], _metrics(), "2026-10-01",
        )
        platform.publish_capability(
            "cap-1", "prov", "边缘视觉识别能力", _capability_fields(),
            "脱敏视频与样本库A", 10, "2026-10-02",
        )
        # 公共标签足以匹配；真实痛点未披露，不出现在撮合依据中。
        candidates = platform.match_candidates("opp-1", "prov")
        self.assertEqual([c["capability_id"] for c in candidates], ["cap-1"])
        self.assertNotIn("real_pain", candidates[0]["visible_opportunity_fields"])

    def test_undisclosed_capability_cannot_match(self) -> None:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "城市路网安全监测", _opportunity_fields(),
            [], _metrics(), "2026-10-01",
        )
        # 能力标签只对其他需求方可见，对 dem 不可见 -> 不匹配。
        platform.publish_capability(
            "cap-1", "prov", "边缘视觉识别能力",
            [FieldEntry("tags", ["视频识别", "边缘计算"], ["dem2"])],
            "脱敏视频", 10, "2026-10-02",
        )
        self.assertEqual(platform.match_candidates("opp-1", "prov"), [])
        application_kwargs = dict(
            opportunity_id="opp-1", capability_id="cap-1", provider_id="prov",
            summary="方案", materials={}, at="2026-10-03",
        )
        with self.assertRaises(AuthorizationError):
            platform.submit_solution(**application_kwargs)

    def test_disclosing_real_pain_after_consent_expands_basis(self) -> None:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "城市路网安全监测", _opportunity_fields(["prov"]),
            [], _metrics(), "2026-10-01",
        )
        platform.publish_capability(
            "cap-1", "prov", "边缘视觉识别能力", _capability_fields(),
            "脱敏视频", 10, "2026-10-02",
        )
        candidates = platform.match_candidates("opp-1", "prov")
        self.assertIn("real_pain", candidates[0]["visible_opportunity_fields"])


class VersioningAndFreezeTest(unittest.TestCase):
    def _setup(self) -> MatchingPlatform:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "城市路网安全监测", _opportunity_fields(),
            ["3座桥"], _metrics(), "2026-10-01",
        )
        platform.publish_capability(
            "cap-1", "prov", "边缘视觉识别能力", _capability_fields(),
            "脱敏视频", 10, "2026-10-02",
        )
        return platform

    def test_revision_creates_new_version_and_supersedes_open(self) -> None:
        platform = self._setup()
        platform.revise_capability(
            "cap-1", _capability_fields(["视频识别", "边缘计算", "大模型"]),
            "脱敏视频", 12, "2026-10-05",
        )
        v1, v2 = platform.capabilities["cap-1"].versions
        self.assertEqual(v1.status, VERSION_SUPERSEDED)
        self.assertEqual(v2.status, VERSION_OPEN)
        self.assertEqual(v2.capacity_total, 12)

    def test_award_freezes_versions_and_metrics(self) -> None:
        platform = self._setup()
        app = platform.submit_solution(
            "opp-1", "cap-1", "prov", "边缘识别试点方案", {"doc": "a"}, "2026-10-03"
        )
        engagement = platform.award(app.id, _milestone_plan(), "2026-10-06")
        self.assertEqual(engagement.state, ENGAGEMENT_SELECTED)
        self.assertEqual(
            engagement.frozen_opportunity["version"]["success_metrics"][0]["target"],
            ">=95%",
        )
        self.assertEqual(engagement.frozen_capability["version"]["data_scope"], "脱敏视频")
        opp_v1 = platform.opportunities["opp-1"].versions[0]
        cap_v1 = platform.capabilities["cap-1"].versions[0]
        self.assertEqual(opp_v1.status, VERSION_FROZEN)
        self.assertEqual(cap_v1.status, VERSION_FROZEN)
        # 冻结后发布新版本不影响既有揭榜合作。
        platform.revise_opportunity(
            "opp-1", _opportunity_fields(), ["5座桥"],
            [MetricTarget("病害发现率", ">=99%", "新数据范围")], "2026-10-07",
        )
        self.assertEqual(opp_v1.status, VERSION_FROZEN)
        self.assertEqual(
            engagement.frozen_opportunity["version"]["success_metrics"][0]["target"],
            ">=95%",
        )


class DuplicateSubmissionTest(unittest.TestCase):
    def test_duplicate_returns_existing_result(self) -> None:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", 10, "2026-10-02"
        )
        first = platform.submit_solution(
            "opp-1", "cap-1", "prov", "方案", {"doc": "a"}, "2026-10-03"
        )
        with self.assertRaises(DuplicateSubmission) as caught:
            platform.submit_solution(
                "opp-1", "cap-1", "prov", "方案", {"doc": "a"}, "2026-10-04"
            )
        self.assertEqual(caught.exception.existing_id, first.id)
        # 没有重复建单。
        self.assertEqual(len(platform.applications), 1)
        self.assertEqual(
            platform.get_application_by_fingerprint(first.fingerprint).id, first.id
        )


class AgreementAndNegotiationTest(unittest.TestCase):
    def _agreed(self) -> tuple[MatchingPlatform, str]:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", 10, "2026-10-02"
        )
        app = platform.submit_solution(
            "opp-1", "cap-1", "prov", "方案", {}, "2026-10-03"
        )
        engagement = platform.award(app.id, _milestone_plan(), "2026-10-06")
        content_hash = platform.agreement_hash(engagement)
        platform.sign_agreement(engagement.id, "dem", content_hash, "2026-10-07")
        platform.sign_agreement(engagement.id, "prov", content_hash, "2026-10-08")
        return platform, engagement.id

    def test_agreement_must_match_frozen_terms(self) -> None:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", 10, "2026-10-02"
        )
        app = platform.submit_solution("opp-1", "cap-1", "prov", "方案", {}, "2026-10-03")
        engagement = platform.award(app.id, _milestone_plan(), "2026-10-06")
        with self.assertRaises(StateError):
            # 需求方无法拿一份悄悄改了指标的协议来签。
            platform.sign_agreement(engagement.id, "dem", "deadbeef", "2026-10-07")
        self.assertEqual(engagement.agreement.status, AGREEMENT_PENDING)

    def test_scope_change_becomes_negotiation_record(self) -> None:
        platform, engagement_id = self._agreed()
        record = platform.propose_change(
            engagement_id, "dem", "试点窗口顺延",
            [{"path": "milestone.2.due_date", "before": "2026-12-15", "after": "2027-01-15"}],
            "2026-11-20",
        )
        self.assertEqual(record.responses, {"dem": True})
        record = platform.respond_negotiation(
            engagement_id, record.id, "prov", True, "2026-11-21"
        )
        self.assertEqual(record.status, NEGOTIATION_AGREED)
        self.assertEqual(platform.engagements[engagement_id].milestone(2).due_date, "2027-01-15")
        # 冻结快照仍记录揭榜时指标；协商轨迹保留变更前后。
        self.assertEqual(
            platform.engagements[engagement_id].frozen_opportunity["version"][
                "success_metrics"
            ][0]["target"],
            ">=95%",
        )

    def test_rejected_change_leaves_terms_untouched(self) -> None:
        platform, engagement_id = self._agreed()
        record = platform.propose_change(
            engagement_id, "prov", "希望压缩容量",
            [{"path": "milestone.2.reserved_units", "before": "3", "after": "1"}],
            "2026-11-20",
        )
        record = platform.respond_negotiation(
            engagement_id, record.id, "dem", False, "2026-11-21"
        )
        self.assertEqual(record.status, "rejected")
        self.assertEqual(platform.engagements[engagement_id].milestone(2).reserved_units, 3)

    def test_consumed_resource_cannot_be_renegotiated(self) -> None:
        platform, engagement_id = self._agreed()
        platform.report_stage(
            engagement_id, 1, {"病害发现率": "92%"}, "2026-10-31"
        )
        platform.evaluate_stage(
            engagement_id, 1, "rev", EVAL_ACCEPTED, "联调达标", "2026-11-01"
        )
        record = platform.propose_change(
            engagement_id, "dem", "想改已履行阶段",
            [{"path": "milestone.1.reserved_units", "before": "2", "after": "4"}],
            "2026-11-02",
        )
        with self.assertRaises(StateError):
            platform.respond_negotiation(engagement_id, record.id, "prov", True, "2026-11-03")


class CapacityTest(unittest.TestCase):
    def _awarded(self, capacity: int = 5, units: int = 5) -> tuple[MatchingPlatform, str]:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", capacity,
            "2026-10-02",
        )
        app = platform.submit_solution("opp-1", "cap-1", "prov", "方案", {}, "2026-10-03")
        plan = _milestone_plan()
        total_per_eng = sum(m["reserved_units"] for m in plan)
        engagement = platform.award(app.id, plan, "2026-10-06")
        self.assertEqual(total_per_eng, units)
        return platform, engagement.id

    def test_capacity_not_double_booked(self) -> None:
        platform, _ = self._awarded(capacity=5, units=5)
        self.assertEqual(platform.capacity_available("cap-1", 1), 0)
        # 同一能力回应第二个场景（新机会），但容量池已被前一揭榜占满。
        platform.publish_opportunity(
            "opp-2", "dem2", "第二个场景",
            [FieldEntry("title", "隧道监测", ["*"]),
             FieldEntry("tags", ["视频识别"], ["*"])],
            [], [MetricTarget("发现率", ">=90%", "隧道视频")], "2026-10-04",
        )
        app2 = platform.submit_solution("opp-2", "cap-1", "prov", "方案二", {}, "2026-10-05")
        with self.assertRaises(CapacityError):
            platform.award(app2.id, _milestone_plan(), "2026-10-07")

    def test_one_capability_serves_multiple_scenes_when_capacity_allows(self) -> None:
        platform, eng1 = self._awarded(capacity=10, units=5)
        self.assertEqual(platform.capacity_available("cap-1", 1), 5)
        platform.publish_opportunity(
            "opp-2", "dem2", "第二个场景",
            [FieldEntry("title", "隧道监测", ["*"]),
             FieldEntry("tags", ["视频识别"], ["*"])],
            [], [MetricTarget("发现率", ">=90%", "隧道视频")], "2026-10-04",
        )
        app2 = platform.submit_solution("opp-2", "cap-1", "prov", "方案二", {}, "2026-10-05")
        small_plan = [
            {"seq": 1, "name": "单阶段试点", "due_date": "2026-12-01",
             "reserved_units": 5, "metrics": []}
        ]
        eng2 = platform.award(app2.id, small_plan, "2026-10-07")
        self.assertTrue(eng2)
        self.assertEqual(platform.capacity_available("cap-1", 1), 0)

    def test_termination_releases_only_unfulfilled_and_keeps_evaluations(self) -> None:
        platform, engagement_id = self._agreed_with_capacity(8)
        # 阶段1 通过：2 单位转为已履行。
        platform.report_stage(engagement_id, 1, {"病害发现率": "92%"}, "2026-10-31")
        platform.evaluate_stage(engagement_id, 1, "rev", EVAL_ACCEPTED, "达标", "2026-11-01")
        m1 = platform.engagements[engagement_id].milestone(1)
        m2 = platform.engagements[engagement_id].milestone(2)
        self.assertEqual(m1.resource_status, RESOURCE_CONSUMED)
        self.assertEqual(m2.resource_status, RESOURCE_RESERVED)
        # 试点失败退出：仅释放阶段2 的 3 单位。
        platform.terminate(engagement_id, "现场条件不具备", "2026-11-10")
        self.assertEqual(m1.resource_status, RESOURCE_CONSUMED)
        self.assertEqual(m2.resource_status, RESOURCE_RELEASED)
        self.assertEqual(m1.status, MILESTONE_ACCEPTED)  # 原评价保留
        self.assertEqual(platform.capacity_used("cap-1", 1), 2)
        self.assertEqual(platform.capacity_available("cap-1", 1), 6)
        self.assertEqual(platform.engagements[engagement_id].state, ENGAGEMENT_TERMINATED)
        with self.assertRaises(StateError):
            platform.terminate(engagement_id, "再次退出", "2026-11-11")

    def _agreed_with_capacity(self, capacity: int) -> tuple[MatchingPlatform, str]:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", capacity,
            "2026-10-02",
        )
        app = platform.submit_solution("opp-1", "cap-1", "prov", "方案", {}, "2026-10-03")
        engagement = platform.award(app.id, _milestone_plan(), "2026-10-06")
        content_hash = platform.agreement_hash(engagement)
        platform.sign_agreement(engagement.id, "dem", content_hash, "2026-10-07")
        platform.sign_agreement(engagement.id, "prov", content_hash, "2026-10-08")
        return platform, engagement.id


class StageEvaluationTest(unittest.TestCase):
    def _agreed(self) -> tuple[MatchingPlatform, str]:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", 10, "2026-10-02"
        )
        app = platform.submit_solution("opp-1", "cap-1", "prov", "方案", {}, "2026-10-03")
        engagement = platform.award(app.id, _milestone_plan(), "2026-10-06")
        content_hash = platform.agreement_hash(engagement)
        platform.sign_agreement(engagement.id, "dem", content_hash, "2026-10-07")
        platform.sign_agreement(engagement.id, "prov", content_hash, "2026-10-08")
        return platform, engagement.id

    def test_demander_cannot_confirm_own_outcomes(self) -> None:
        platform, engagement_id = self._agreed()
        platform.report_stage(engagement_id, 1, {"病害发现率": "92%"}, "2026-10-31")
        with self.assertRaises(AuthorizationError):
            platform.evaluate_stage(engagement_id, 1, "dem", EVAL_ACCEPTED, "自评通过", "2026-11-01")

    def test_provider_cannot_evaluate_either(self) -> None:
        platform, engagement_id = self._agreed()
        platform.report_stage(engagement_id, 1, {"病害发现率": "92%"}, "2026-10-31")
        with self.assertRaises(AuthorizationError):
            platform.evaluate_stage(engagement_id, 1, "prov", EVAL_ACCEPTED, "供货方评", "2026-11-01")

    def test_rejected_stage_can_be_reevaluated_accepted_is_locked(self) -> None:
        platform, engagement_id = self._agreed()
        platform.report_stage(engagement_id, 1, {"病害发现率": "85%"}, "2026-10-31")
        milestone = platform.evaluate_stage(
            engagement_id, 1, "rev", EVAL_REJECTED, "未达标", "2026-11-01"
        )
        self.assertEqual(milestone.status, "pending")
        # 整改后重新上报并通过。
        platform.report_stage(engagement_id, 1, {"病害发现率": "93%"}, "2026-11-05")
        platform.evaluate_stage(engagement_id, 1, "rev", EVAL_ACCEPTED, "整改达标", "2026-11-06")
        with self.assertRaises(StateError):
            platform.evaluate_stage(engagement_id, 1, "rev", EVAL_REJECTED, "不可翻案", "2026-11-07")

    def test_report_must_cover_exactly_frozen_stage_metrics(self) -> None:
        platform, engagement_id = self._agreed()
        with self.assertRaises(StateError):
            platform.report_stage(engagement_id, 1, {"别的指标": "x"}, "2026-10-31")

    def test_completion_requires_all_stages_accepted(self) -> None:
        platform, engagement_id = self._agreed()
        for seq, result, verdict, at in (
            (1, "92%", EVAL_REJECTED, "2026-11-01"),
        ):
            platform.report_stage(engagement_id, seq, {"病害发现率": result}, "2026-10-31")
            platform.evaluate_stage(engagement_id, seq, "rev", verdict, "未达标", at)
        self.assertEqual(platform.engagements[engagement_id].state, ENGAGEMENT_AGREED)
        with self.assertRaises(StateError):
            platform.create_case("case-1", engagement_id, "标题", "摘要")


class CasePublishingTest(unittest.TestCase):
    def _completed(self) -> tuple[MatchingPlatform, str]:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", 10, "2026-10-02"
        )
        app = platform.submit_solution("opp-1", "cap-1", "prov", "方案", {}, "2026-10-03")
        engagement = platform.award(app.id, _milestone_plan(), "2026-10-06")
        content_hash = platform.agreement_hash(engagement)
        platform.sign_agreement(engagement.id, "dem", content_hash, "2026-10-07")
        platform.sign_agreement(engagement.id, "prov", content_hash, "2026-10-08")
        platform.report_stage(engagement.id, 1, {"病害发现率": "92%"}, "2026-10-31")
        platform.evaluate_stage(engagement.id, 1, "rev", EVAL_ACCEPTED, "达标", "2026-11-01")
        platform.report_stage(engagement.id, 2, {"病害发现率": "96%"}, "2026-12-14")
        platform.evaluate_stage(engagement.id, 2, "rev", EVAL_ACCEPTED, "达标", "2026-12-15")
        self.assertEqual(engagement.state, ENGAGEMENT_COMPLETED)
        return platform, engagement.id

    def test_case_requires_both_authorizations_and_review(self) -> None:
        platform, engagement_id = self._completed()
        case_file = platform.create_case("case-1", engagement_id, "可复制案例", "过程摘要")
        # 单方授权不能送复核。
        platform.authorize_publish("case-1", "dem", "2026-12-20")
        with self.assertRaises(AuthorizationError):
            platform.submit_case_for_review("case-1", "2026-12-21")
        platform.authorize_publish("case-1", "prov", "2026-12-21")
        platform.submit_case_for_review("case-1", "2026-12-22")
        self.assertEqual(case_file.status, CASE_REVIEW)
        with self.assertRaises(AuthorizationError):
            platform.review_case("case-1", "dem", EVAL_ACCEPTED, "需求方不能复核", "2026-12-23")
        platform.review_case("case-1", "rev", EVAL_ACCEPTED, "材料属实", "2026-12-24")
        published = platform.publish_case("case-1", "2026-12-25")
        self.assertEqual(published.status, CASE_PUBLISHED)

    def test_case_fails_review_stays_unpublished(self) -> None:
        platform, engagement_id = self._completed()
        platform.create_case("case-1", engagement_id, "案例", "摘要")
        platform.authorize_publish("case-1", "dem", "2026-12-20")
        platform.authorize_publish("case-1", "prov", "2026-12-20")
        platform.submit_case_for_review("case-1", "2026-12-21")
        platform.review_case("case-1", "rev", EVAL_REJECTED, "缺少脱敏证明", "2026-12-22")
        with self.assertRaises(StateError):
            platform.publish_case("case-1", "2026-12-23")


class QuarantineAndRecoveryTest(unittest.TestCase):
    def test_conflicting_material_quarantined_and_resumed(self) -> None:
        platform, engagement_id = self._mid_pilot()
        item = platform.quarantine_material(
            kind="stage_report",
            payload={"engagement_id": engagement_id, "seq": 2, "results": {}},
            reason="与已登记上报并发冲突",
            at="2026-11-30T10:00:00",
        )
        queue = platform.resume_queue("2026-12-31")
        self.assertIn(item.id, queue["quarantined"])
        # 到期里程碑也出现在恢复队列。
        self.assertIn(
            {"engagement_id": engagement_id, "seq": 2, "due_date": "2026-12-15"},
            queue["due_milestones"],
        )
        platform.resolve_quarantine(item.id, "核对后以正式上报为准")
        self.assertNotIn(item.id, platform.resume_queue("2026-12-31")["quarantined"])

    def test_pending_agreement_resumes_after_restart(self) -> None:
        platform, engagement_id = self._selected()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            platform.save(path)
            restored = MatchingPlatform.load(path)
        queue = restored.resume_queue("2026-12-31")
        self.assertEqual(queue["pending_agreements"], [engagement_id])
        # 恢复后可继续签署并走完试点。
        engagement = restored.engagements[engagement_id]
        content_hash = restored.agreement_hash(engagement)
        restored.sign_agreement(engagement_id, "dem", content_hash, "2027-01-02")
        restored.sign_agreement(engagement_id, "prov", content_hash, "2027-01-03")
        self.assertEqual(engagement.state, ENGAGEMENT_AGREED)

    def _selected(self) -> tuple[MatchingPlatform, str]:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", 10, "2026-10-02"
        )
        app = platform.submit_solution("opp-1", "cap-1", "prov", "方案", {}, "2026-10-03")
        engagement = platform.award(app.id, _milestone_plan(), "2026-10-06")
        return platform, engagement.id

    def _mid_pilot(self) -> tuple[MatchingPlatform, str]:
        platform, engagement_id = self._selected()
        engagement = platform.engagements[engagement_id]
        content_hash = platform.agreement_hash(engagement)
        platform.sign_agreement(engagement_id, "dem", content_hash, "2026-10-07")
        platform.sign_agreement(engagement_id, "prov", content_hash, "2026-10-08")
        platform.report_stage(engagement_id, 1, {"病害发现率": "92%"}, "2026-10-31")
        platform.evaluate_stage(engagement_id, 1, "rev", EVAL_ACCEPTED, "达标", "2026-11-01")
        return platform, engagement_id


class TraceabilityTest(unittest.TestCase):
    def test_explain_collaboration_covers_full_journey(self) -> None:
        platform = _platform()
        platform.publish_opportunity(
            "opp-1", "dem", "监测", _opportunity_fields(), [], _metrics(), "2026-10-01"
        )
        platform.publish_capability(
            "cap-1", "prov", "能力", _capability_fields(), "脱敏视频", 10, "2026-10-02"
        )
        app = platform.submit_solution("opp-1", "cap-1", "prov", "方案", {}, "2026-10-03")
        engagement = platform.award(app.id, _milestone_plan(), "2026-10-06")
        content_hash = platform.agreement_hash(engagement)
        platform.sign_agreement(engagement.id, "dem", content_hash, "2026-10-07")
        platform.sign_agreement(engagement.id, "prov", content_hash, "2026-10-08")
        neg = platform.propose_change(
            engagement.id, "dem", "顺延",
            [{"path": "milestone.2.due_date", "before": "2026-12-15", "after": "2026-12-20"}],
            "2026-11-20",
        )
        platform.respond_negotiation(engagement.id, neg.id, "prov", True, "2026-11-21")
        platform.report_stage(engagement.id, 1, {"病害发现率": "92%"}, "2026-10-31")
        platform.evaluate_stage(engagement.id, 1, "rev", EVAL_ACCEPTED, "达标", "2026-11-01")
        platform.report_stage(engagement.id, 2, {"病害发现率": "96%"}, "2026-12-19")
        platform.evaluate_stage(engagement.id, 2, "rev", EVAL_ACCEPTED, "达标", "2026-12-20")
        platform.create_case("case-1", engagement.id, "案例", "摘要")
        platform.authorize_publish("case-1", "dem", "2026-12-22")
        platform.authorize_publish("case-1", "prov", "2026-12-22")
        platform.submit_case_for_review("case-1", "2026-12-23")
        platform.review_case("case-1", "rev", EVAL_ACCEPTED, "属实", "2026-12-24")
        platform.publish_case("case-1", "2026-12-25")

        story = platform.explain_collaboration(engagement.id)
        events = [point["event"] for point in story["timeline"]]
        self.assertEqual(
            events,
            [
                "solution_submitted",
                "awarded_versions_frozen",
                "agreement_circulated",
                "change_proposed",
                "stage_reported",
                "stage_evaluated",
                "stage_reported",
                "stage_evaluated",
                "case_progress",
            ],
        )
        # 参与者能看到合作从哪个机会/能力版本走来。
        self.assertEqual(story["frozen_opportunity"]["opportunity_id"], "opp-1")
        self.assertEqual(story["frozen_capability"]["capability_id"], "cap-1")
        self.assertEqual(story["agreement"]["status"], "signed")


if __name__ == "__main__":
    unittest.main()
