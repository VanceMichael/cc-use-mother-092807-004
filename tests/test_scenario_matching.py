"""供需撮合后端业务规则测试。

覆盖：授权披露、揭榜冻结、冲突隔离、容量占用与退出释放、
阶段评价与终验、双方授权发布、重复申报、崩溃恢复、合作血缘追溯。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.scenario_matching import (
    ActorRole,
    AuthorizationError,
    CapacityError,
    ConflictError,
    DisclosureError,
    DuplicateSubmissionError,
    EvaluationError,
    EventStore,
    Material,
    MilestoneError,
    Scope,
    ScenarioMatchingBackend,
    ScopeError,
    VersionStateError,
)


def make_backend(path: Path | None = None) -> ScenarioMatchingBackend:
    backend = ScenarioMatchingBackend(EventStore(path))
    backend.register_actor("D1", "某区交通局", ActorRole.DEMANDER)
    backend.register_actor("P1", "智行科技", ActorRole.PROVIDER)
    backend.register_actor("R1", "评审专家组", ActorRole.REVIEWER)
    backend.register_actor("OP", "场景运营团队", ActorRole.OPERATOR)
    backend.add_resource_pool("POOL", 100)
    return backend


def publish_pair(
    backend: ScenarioMatchingBackend,
    opp_id: str = "O1",
    cap_id: str = "C1",
    cap_domains=frozenset({"卡口视频"}),
    cap_scale: int = 50,
    opp_scale: int = 20,
    opp_domains=frozenset({"卡口视频"}),
    accuracy: float = 0.9,
) -> None:
    backend.publish_opportunity(
        opp_id,
        "D1",
        "路口拥堵治理",
        {
            "技术方向": "视觉AI",
            "指标:准确率": accuracy,
            "真实痛点": "某关键路口长期拥堵（敏感）",
        },
        Scope(opp_domains, opp_scale),
        ("数据不出域", "需通过等保测评"),
    )
    backend.register_capability(
        cap_id,
        "P1",
        "视频流量识别方案",
        {"技术方向": "视觉AI", "实现细节": "保密架构（敏感）"},
        Scope(cap_domains, cap_scale),
        frozenset({"拥堵治理", "秩序监测"}),
    )


def disclose_each_other(backend: ScenarioMatchingBackend) -> None:
    backend.grant_disclosure("D1", "P1", "技术方向")
    backend.grant_disclosure("P1", "D1", "技术方向")


def reach_claimed(
    backend: ScenarioMatchingBackend,
    submission: str = "S-1",
    opp_id: str = "O1",
    cap_id: str = "C1",
    pool: str = "POOL",
) -> str:
    publish_pair(backend, opp_id, cap_id)
    disclose_each_other(backend)
    case, _ = backend.submit_for_matching(submission, opp_id, cap_id, pool)
    backend.claim(case.match_id)
    return case.match_id


def reach_signed(
    backend: ScenarioMatchingBackend,
    match_id: str = "M-0001",
    pool: str = "POOL",
    due_events=(100, 200),
) -> str:
    case = backend.matches[match_id]
    backend.submit_review_material(
        match_id,
        Material(
            "MT-OK",
            "P1",
            "能力证明",
            {"附件": "脱敏方案书"},
            declared_fingerprint=case.frozen.capability_fingerprint,
        ),
    )
    backend.review(match_id, "R1", True)
    specs = [
        (f"阶段{i}", {"指标:准确率": 0.9}, due)
        for i, due in enumerate(due_events, start=1)
    ]
    backend.sign_agreement(match_id, f"A-{match_id}", pool, specs)
    return match_id


class DisclosureMatchingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = make_backend()
        publish_pair(self.backend)

    def test_no_disclosure_blocks_matching(self) -> None:
        with self.assertRaises(DisclosureError):
            self.backend.submit_for_matching("S-1", "O1", "C1", "POOL")

    def test_one_sided_disclosure_blocks_matching(self) -> None:
        self.backend.grant_disclosure("D1", "P1", "技术方向")
        with self.assertRaises(DisclosureError):
            self.backend.submit_for_matching("S-1", "O1", "C1", "POOL")

    def test_matching_uses_only_disclosed_fields(self) -> None:
        # 只披露与能力不相干的字段：虽然双方都披露了，但没有匹配依据。
        self.backend.grant_disclosure("D1", "P1", "真实痛点")
        self.backend.grant_disclosure("P1", "D1", "实现细节")
        with self.assertRaisesRegex(DisclosureError, "匹配依据"):
            self.backend.submit_for_matching("S-1", "O1", "C1", "POOL")

    def test_sensitive_fields_never_enter_match_basis(self) -> None:
        disclose_each_other(self.backend)
        case, _ = self.backend.submit_for_matching("S-1", "O1", "C1", "POOL")
        suggested = self.backend.matches[case.match_id]
        # 撮合单本身不携带任何敏感字段值，只有匹配依据条目。
        self.assertEqual(suggested.status.value, "已撮合")
        self.assertFalse(hasattr(suggested, "真实痛点"))

    def test_revoked_disclosure_blocks_later_submission(self) -> None:
        disclose_each_other(self.backend)
        self.backend.submit_for_matching("S-1", "O1", "C1", "POOL")
        self.backend.grant_disclosure("P1", "D1", "技术方向", allowed=False)
        with self.assertRaises(DisclosureError):
            self.backend.submit_for_matching("S-2", "O1", "C1", "POOL")


class FreezeAndNegotiationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = make_backend()
        self.match_id = reach_claimed(self.backend)

    def test_claim_freezes_versions_and_fingerprints(self) -> None:
        case = self.backend.matches[self.match_id]
        self.assertEqual(case.frozen.opportunity_version, 1)
        self.assertEqual(case.frozen.capability_version, 1)

    def test_new_opportunity_version_does_not_change_frozen(self) -> None:
        # 需求方在揭榜后发布新版本（指标被悄悄改动），冻结版本不受影响。
        self.backend.publish_opportunity(
            "O1",
            "D1",
            "路口拥堵治理（指标下调版）",
            {"技术方向": "视觉AI", "指标:准确率": 0.7, "真实痛点": "改写"},
            Scope(frozenset({"卡口视频"}), 20),
        )
        frozen = self.backend.frozen_opportunity(
            self.backend.matches[self.match_id]
        )
        self.assertEqual(frozen.version, 1)
        self.assertEqual(frozen.fields["指标:准确率"], 0.9)
        self.assertEqual(self.backend.latest_opportunity("O1").version, 2)

    def test_milestone_plan_must_match_frozen_indicators(self) -> None:
        case = self.backend.matches[self.match_id]
        self.backend.submit_review_material(
            self.match_id,
            Material(
                "MT-OK", "P1", "能力证明", {},
                declared_fingerprint=case.frozen.capability_fingerprint,
            ),
        )
        self.backend.review(self.match_id, "R1", True)
        with self.assertRaisesRegex(MilestoneError, "篡改了冻结指标"):
            self.backend.sign_agreement(
                self.match_id,
                "A1",
                "POOL",
                [("阶段1", {"指标:准确率": 0.7}, 100)],
            )
        with self.assertRaisesRegex(MilestoneError, "不在冻结需求版本中"):
            self.backend.sign_agreement(
                self.match_id,
                "A1",
                "POOL",
                [("阶段1", {"指标:新指标": 1.0}, 100)],
            )

    def test_material_with_changed_indicator_is_quarantined(self) -> None:
        with self.assertRaises(ConflictError):
            self.backend.submit_review_material(
                self.match_id,
                Material("MT-CONF", "P1", "指标值申报", {"指标:准确率": 0.7}),
            )
        worklist = self.backend.recovery_worklist()
        self.assertEqual(
            [q["material_id"] for q in worklist["待复核隔离材料"]], ["MT-CONF"]
        )
        # 隔离材料不影响流程，复核维持冻结后可以继续。
        self.backend.resolve_quarantined("MT-CONF", "维持冻结版本", "以揭榜版本为准")
        reach_signed(self.backend, self.match_id)

    def test_material_claiming_wrong_version_is_quarantined(self) -> None:
        with self.assertRaises(ConflictError):
            self.backend.submit_review_material(
                self.match_id,
                Material(
                    "MT-WRONG",
                    "D1",
                    "需求变更声明",
                    {},
                    declared_fingerprint="0" * 16,
                ),
            )
        self.backend.resolve_quarantined("MT-WRONG", "转范围协商", "另行走协商")

    def test_scope_change_only_adds_negotiation_record(self) -> None:
        reach_signed(self.backend, self.match_id, due_events=(3, 6))
        case = self.backend.matches[self.match_id]
        frozen_fp = case.frozen.opportunity_fingerprint
        self.backend.request_scope_change(
            self.match_id, "N-1", "D1", {"pilot_scale": 30}, "扩大试点到相邻片区"
        )
        # 发起方不能单方接受。
        with self.assertRaisesRegex(Exception, "对方确认"):
            self.backend.accept_scope_change(self.match_id, "N-1", "D1", "POOL")
        self.backend.accept_scope_change(self.match_id, "N-1", "P1", "POOL")
        case = self.backend.matches[self.match_id]
        self.assertTrue(case.negotiations[0].accepted)
        self.assertEqual(case.agreement.allocated_scope.pilot_scale, 30)
        self.assertEqual(self.backend.pools["POOL"].allocated, 30)
        # 冻结引用与指纹纹丝不动。
        self.assertEqual(case.frozen.opportunity_fingerprint, frozen_fp)
        self.assertEqual(
            self.backend.frozen_opportunity(case).required_scope.pilot_scale, 20
        )


class CapacityAndExitTest(unittest.TestCase):
    def test_capacity_cannot_be_double_booked(self) -> None:
        backend = make_backend()
        m1 = reach_claimed(backend, "S-1", "O1", "C1")
        reach_signed(backend, m1, due_events=(50, 90))  # 占用 20
        self.assertEqual(backend.pools["POOL"].available, 80)

        # 同池第二单需要 90 个点位：能力范围足够，但资源池容量不足。
        backend.register_capability(
            "C2", "P1", "识别方案（大规模版）", {"技术方向": "视觉AI"},
            Scope(frozenset({"卡口视频"}), 200),
        )
        backend.register_actor("D2", "另一个区", ActorRole.DEMANDER)
        backend.publish_opportunity(
            "O2", "D2", "另一拥堵点",
            {"技术方向": "视觉AI", "指标:准确率": 0.9},
            Scope(frozenset({"卡口视频"}), 90),
        )
        backend.grant_disclosure("D2", "P1", "技术方向")
        backend.grant_disclosure("P1", "D2", "技术方向")
        case, _ = backend.submit_for_matching("S-2", "O2", "C2", "POOL")
        backend.claim(case.match_id)
        backend.review(case.match_id, "R1", True)
        with self.assertRaises(CapacityError):
            backend.sign_agreement(
                case.match_id, "A2", "POOL",
                [("阶段1", {"指标:准确率": 0.9}, 200)],
            )
        # 签约失败不得留下任何占用。
        self.assertEqual(backend.pools["POOL"].allocated, 20)

    def test_one_capability_serves_multiple_scenarios_within_scope(self) -> None:
        backend = make_backend()
        m1 = reach_claimed(backend, "S-1", "O1", "C1")
        reach_signed(backend, m1, due_events=(50, 90))

        backend.register_actor("D2", "园区管委会", ActorRole.DEMANDER)
        backend.publish_opportunity(
            "O2", "D2", "园区秩序监测",
            {"技术方向": "视觉AI", "指标:准确率": 0.9},
            Scope(frozenset({"卡口视频"}), 10),
        )
        backend.grant_disclosure("D2", "P1", "技术方向")
        backend.grant_disclosure("P1", "D2", "技术方向")
        case, _ = backend.submit_for_matching("S-2", "O2", "C1", "POOL")
        backend.claim(case.match_id)
        backend.review(case.match_id, "R1", True)
        backend.sign_agreement(
            case.match_id, "A2", "POOL",
            [("阶段1", {"指标:准确率": 0.9}, 200)],
        )
        self.assertEqual(backend.pools["POOL"].allocated, 30)

    def test_out_of_scope_scenario_is_rejected(self) -> None:
        backend = make_backend()
        m1 = reach_claimed(backend, "S-1", "O1", "C1")
        # 能力范围只含卡口视频，医保数据场景不能签约。
        backend.publish_opportunity(
            "O3", "D1", "医保智能审核",
            {"技术方向": "视觉AI", "指标:准确率": 0.9},
            Scope(frozenset({"医保结算数据"}), 5),
        )
        backend.grant_disclosure("D1", "P1", "技术方向")
        case, _ = backend.submit_for_matching("S-3", "O3", "C1", "POOL")
        backend.claim(case.match_id)
        backend.review(case.match_id, "R1", True)
        with self.assertRaises(ScopeError):
            backend.sign_agreement(
                case.match_id, "A3", "POOL",
                [("阶段1", {"指标:准确率": 0.9}, 200)],
            )
        self.assertEqual(backend.pools["POOL"].allocated, 0)

    def test_exit_releases_only_unfulfilled_capacity_keeps_evaluations(self) -> None:
        backend = make_backend()
        # 三阶段、30 点位，便于按里程碑等比折算。
        backend.publish_opportunity(
            "O1", "D1", "拥堵治理",
            {"技术方向": "视觉AI", "指标:准确率": 0.9, "真实痛点": "x"},
            Scope(frozenset({"卡口视频"}), 30),
        )
        backend.register_capability(
            "C1", "P1", "识别方案", {"技术方向": "视觉AI"},
            Scope(frozenset({"卡口视频"}), 60),
        )
        disclose_each_other(backend)
        case, _ = backend.submit_for_matching("S-1", "O1", "C1", "POOL")
        backend.claim(case.match_id)
        backend.review(case.match_id, "R1", True)
        backend.sign_agreement(
            case.match_id, "A1", "POOL",
            [
                ("阶段1", {"指标:准确率": 0.9}, 100),
                ("阶段2", {"指标:准确率": 0.9}, 200),
                ("阶段3", {"指标:准确率": 0.9}, 300),
            ],
        )
        backend.evaluate_milestone(case.match_id, 1, True, ["R1"], "完成")
        # 第二阶段失败后退出：释放 30 × 2/3 = 20，已完成阶段对应 10 不退还。
        released = backend.terminate(
            case.match_id, "第二阶段指标不达标", "POOL", failed_milestone_seq=2
        )
        self.assertEqual(released, 20)
        self.assertEqual(backend.pools["POOL"].allocated, 10)
        self.assertEqual(backend.pools["POOL"].holdings[case.match_id], 10)
        milestones = backend.matches[case.match_id].agreement.milestone_plan
        self.assertEqual(milestones[0].status, "已通过")
        self.assertEqual(milestones[0].evaluated_by, ("R1",))
        self.assertEqual(milestones[1].status, "未通过")
        self.assertEqual(milestones[2].status, "已免除")
        self.assertEqual(backend.matches[case.match_id].status.value, "已终止")


class MilestoneEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = make_backend()
        self.match_id = reach_claimed(self.backend)
        reach_signed(self.backend, self.match_id, due_events=(100, 200))

    def test_milestones_must_follow_order(self) -> None:
        with self.assertRaisesRegex(MilestoneError, "按顺序"):
            self.backend.evaluate_milestone(self.match_id, 2, True, ["R1"])

    def test_sealed_evaluation_cannot_change(self) -> None:
        self.backend.evaluate_milestone(self.match_id, 1, True, ["R1"])
        with self.assertRaisesRegex(MilestoneError, "已封存"):
            self.backend.evaluate_milestone(self.match_id, 1, False, ["R1"])

    def test_reviewer_must_participate(self) -> None:
        with self.assertRaises(EvaluationError):
            self.backend.evaluate_milestone(self.match_id, 1, True, ["D1"])

    def test_demander_alone_cannot_confirm_final_outcome(self) -> None:
        self.backend.evaluate_milestone(self.match_id, 1, True, ["R1"])
        with self.assertRaisesRegex(EvaluationError, "不能单独确认最终成效"):
            self.backend.evaluate_milestone(
                self.match_id, 2, True, ["D1"], remark="最终阶段"
            )

    def test_joint_confirmation_completes_cooperation(self) -> None:
        self.backend.evaluate_milestone(self.match_id, 1, True, ["R1"])
        self.backend.evaluate_milestone(
            self.match_id, 2, True, ["D1", "R1"], remark="最终阶段"
        )
        self.assertEqual(
            self.backend.matches[self.match_id].status.value, "已完成"
        )


class CasePublishTest(unittest.TestCase):
    def _completed(self, backend: ScenarioMatchingBackend, match_id: str) -> None:
        backend.evaluate_milestone(match_id, 1, True, ["R1"])
        backend.evaluate_milestone(
            match_id, 2, True, ["D1", "R1"], remark="最终阶段"
        )

    def test_publish_requires_both_authorizations(self) -> None:
        backend = make_backend()
        match_id = reach_claimed(backend)
        reach_signed(backend, match_id)
        self._completed(backend, match_id)
        with self.assertRaises(AuthorizationError):
            backend.publish_case(match_id, "CASE-1")
        backend.authorize_publish(match_id, "D1")
        with self.assertRaisesRegex(AuthorizationError, "能力主体"):
            backend.publish_case(match_id, "CASE-1")
        backend.authorize_publish(match_id, "P1")
        snapshot = backend.publish_case(match_id, "CASE-1")
        self.assertEqual(snapshot["frozen_opportunity_version"], 1)
        self.assertEqual(snapshot["frozen_capability_version"], 1)
        self.assertTrue(snapshot["replicable"])
        self.assertEqual(backend.matches[match_id].status.value, "已发布案例")

    def test_cannot_authorize_before_completion(self) -> None:
        backend = make_backend()
        match_id = reach_claimed(backend)
        reach_signed(backend, match_id)
        with self.assertRaises(VersionStateError):
            backend.authorize_publish(match_id, "D1")

    def test_published_case_records_negotiation_history(self) -> None:
        backend = make_backend()
        match_id = reach_claimed(backend)
        reach_signed(backend, match_id)
        backend.request_scope_change(
            match_id, "N-1", "P1",
            {"add_data_domains": ["公交GPS"]},
            "需要叠加公交数据验证",
        )
        backend.accept_scope_change(match_id, "N-1", "D1", "POOL")
        self._completed(backend, match_id)
        backend.authorize_publish(match_id, "D1")
        backend.authorize_publish(match_id, "P1")
        snapshot = backend.publish_case(match_id, "CASE-9")
        self.assertEqual(snapshot["negotiations"][0]["record_id"], "N-1")
        self.assertTrue(snapshot["negotiations"][0]["accepted"])


class DuplicateSubmissionTest(unittest.TestCase):
    def test_duplicate_returns_existing_match_without_new_event(self) -> None:
        backend = make_backend()
        publish_pair(backend)
        disclose_each_other(backend)
        first, was_dup = backend.submit_for_matching("S-DUP", "O1", "C1", "POOL")
        self.assertFalse(was_dup)
        seq_after_first = len(backend.store.events)
        again, was_dup = backend.submit_for_matching("S-DUP", "O1", "C1", "POOL")
        self.assertTrue(was_dup)
        self.assertIs(again, first)
        self.assertEqual(len(backend.store.events), seq_after_first)

    def test_duplicate_key_rebound_to_other_pair_is_rejected(self) -> None:
        backend = make_backend()
        publish_pair(backend)
        disclose_each_other(backend)
        backend.submit_for_matching("S-DUP", "O1", "C1", "POOL")
        # 同编号不能换绑到另一对供需，避免借重复申报绕过冻结。
        backend.register_actor("D2", "另一个区", ActorRole.DEMANDER)
        backend.publish_opportunity(
            "O2", "D2", "另一拥堵点",
            {"技术方向": "视觉AI", "指标:准确率": 0.9},
            Scope(frozenset({"卡口视频"}), 10),
        )
        backend.grant_disclosure("D2", "P1", "技术方向")
        backend.grant_disclosure("P1", "D2", "技术方向")
        with self.assertRaises(DuplicateSubmissionError) as ctx:
            backend.submit_for_matching("S-DUP", "O2", "C1", "POOL")
        self.assertEqual(ctx.exception.match_id, "M-0001")


class RecoveryTest(unittest.TestCase):
    def _build_pending_state(self, path: Path) -> None:
        backend = make_backend(path)

        # 撮合一：已揭榜、评审通过、协议待签。
        m1 = reach_claimed(backend, "S-1", "O1", "C1")
        backend.review(m1, "R1", True)

        # 撮合二：签约后首个里程碑已到期未评价。
        backend.register_actor("D2", "某园区", ActorRole.DEMANDER)
        backend.publish_opportunity(
            "O2", "D2", "园区监测",
            {"技术方向": "视觉AI", "指标:准确率": 0.9},
            Scope(frozenset({"卡口视频"}), 10),
        )
        backend.grant_disclosure("D2", "P1", "技术方向")
        backend.grant_disclosure("P1", "D2", "技术方向")
        case2, _ = backend.submit_for_matching("S-2", "O2", "C1", "POOL")
        backend.claim(case2.match_id)
        backend.review(case2.match_id, "R1", True)
        sign_seq = len(backend.store.events) + 1
        backend.sign_agreement(
            case2.match_id, "A2", "POOL",
            [
                ("阶段1", {"指标:准确率": 0.9}, sign_seq + 1),
                ("阶段2", {"指标:准确率": 0.9}, sign_seq + 200),
            ],
        )
        # 两条无关事件推进时间，使第一个里程碑到期。
        backend.grant_disclosure("D2", "P1", "技术方向", allowed=False)
        backend.grant_disclosure("D2", "P1", "技术方向", allowed=True)

        # 撮合三：已完成终验，仅需求方授权，案例待补另一方授权。
        backend.register_actor("D3", "某医院", ActorRole.DEMANDER)
        backend.publish_opportunity(
            "O3", "D3", "院内物流",
            {"技术方向": "视觉AI", "指标:准确率": 0.9},
            Scope(frozenset({"卡口视频"}), 5),
        )
        backend.grant_disclosure("D3", "P1", "技术方向")
        backend.grant_disclosure("P1", "D3", "技术方向")
        case3, _ = backend.submit_for_matching("S-3", "O3", "C1", "POOL")
        backend.claim(case3.match_id)
        backend.review(case3.match_id, "R1", True)
        backend.sign_agreement(
            case3.match_id, "A3", "POOL",
            [("阶段1", {"指标:准确率": 0.9}, len(backend.store.events) + 50)],
        )
        backend.evaluate_milestone(
            case3.match_id, 1, True, ["D3", "R1"], remark="最终阶段"
        )
        backend.authorize_publish(case3.match_id, "D3")

        # 一份与冻结版本冲突的材料隔离待复核。
        with self.assertRaises(ConflictError):
            backend.submit_review_material(
                m1, Material("MT-Q", "P1", "指标值申报", {"指标:准确率": 0.5})
            )

    def test_worklist_resumes_after_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self._build_pending_state(path)
            restored = ScenarioMatchingBackend(EventStore(path))
            worklist = restored.recovery_worklist()
            self.assertEqual(
                [item["match_id"] for item in worklist["待签协议"]], ["M-0001"]
            )
            self.assertTrue(worklist["待签协议"][0]["review_passed"])
            self.assertEqual(
                [item["match_id"] for item in worklist["到期里程碑"]], ["M-0002"]
            )
            self.assertEqual(
                worklist["待补授权案例"][0]["waiting_for"], "能力主体授权"
            )
            self.assertEqual(
                [q["material_id"] for q in worklist["待复核隔离材料"]], ["MT-Q"]
            )

            # 恢复后可以直接续办：补签协议、到期里程碑评价、补授权发布。
            restored.sign_agreement(
                "M-0001", "A1", "POOL",
                [("阶段1", {"指标:准确率": 0.9}, len(restored.store.events) + 10)],
            )
            restored.evaluate_milestone("M-0002", 1, True, ["R1"])
            restored.resolve_quarantined("MT-Q", "维持冻结版本", "以冻结为准")
            restored.authorize_publish("M-0003", "P1")
            snapshot = restored.publish_case("M-0003", "CASE-3")
            self.assertTrue(snapshot["replicable"])

    def test_corrupted_tail_is_quarantined_on_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self._build_pending_state(path)
            raw_store = EventStore(path)
            raw_store.simulate_corrupted_tail()
            restored = ScenarioMatchingBackend(EventStore(path))
            self.assertTrue(restored.store.recovery_notes)
            # 崩溃残留之前的状态全部还在。
            self.assertIn("M-0001", restored.matches)
            self.assertEqual(
                restored.matches["M-0001"].frozen.opportunity_version, 1
            )
            self.assertEqual(restored.recovery_worklist()["待补授权案例"][0]["match_id"], "M-0003")


class LineageTraceTest(unittest.TestCase):
    def test_full_journey_is_traceable_from_opportunity_to_case(self) -> None:
        backend = make_backend()
        match_id = reach_claimed(backend)
        # 揭榜后需求出了新版本，应同时出现在血缘里，证明“走的是 v1”。
        backend.publish_opportunity(
            "O1", "D1", "路口拥堵治理v2",
            {"技术方向": "视觉AI", "指标:准确率": 0.9, "真实痛点": "x"},
            Scope(frozenset({"卡口视频"}), 20),
        )
        reach_signed(backend, match_id)
        backend.request_scope_change(
            match_id, "N-1", "D1", {"pilot_scale": 25}, "小幅扩点"
        )
        backend.accept_scope_change(match_id, "N-1", "P1", "POOL")
        backend.evaluate_milestone(match_id, 1, True, ["R1"])
        backend.evaluate_milestone(
            match_id, 2, True, ["D1", "R1"], remark="最终阶段"
        )
        backend.authorize_publish(match_id, "D1")
        backend.authorize_publish(match_id, "P1")
        backend.publish_case(match_id, "CASE-1")

        steps = backend.trace_cooperation(match_id)
        types_in_order = [s["type"] for s in steps]
        self.assertEqual(types_in_order[0], "OpportunityPublished")
        self.assertIn("OpportunityPublished", types_in_order)  # v2 也在
        self.assertEqual(types_in_order.count("OpportunityPublished"), 2)
        for expected in (
            "CapabilityRegistered",
            "MatchSuggested",
            "Claimed",
            "ReviewPassed",
            "AgreementSigned",
            "NegotiationAdded",
            "NegotiationAccepted",
            "MilestoneEvaluated",
            "MilestoneEvaluated",
            "PublishAuthorized",
            "PublishAuthorized",
            "CasePublished",
        ):
            self.assertIn(expected, types_in_order, f"血缘缺少 {expected}")

        text = backend.explain_trace(match_id)
        self.assertIn("揭榜并冻结", text)
        self.assertIn("需求 v1", text)
        self.assertIn("发布可复制案例 CASE-1", text)


if __name__ == "__main__":
    unittest.main()
