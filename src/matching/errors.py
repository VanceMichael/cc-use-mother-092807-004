"""领域错误。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域规则冲突的基类。"""


class StateError(DomainError):
    """当前状态不允许该操作（未冻结、已签字、阶段已定稿等）。"""


class AuthorizationError(DomainError):
    """字段未经双方同意披露，或操作缺少授权。"""


class DuplicateSubmission(DomainError):
    """同一申报指纹再次提交，必须返回既有结果而不是重复建单。"""

    def __init__(self, fingerprint: str, existing_id: str) -> None:
        super().__init__(f"重复申报，返回既有申报 {existing_id}")
        self.fingerprint = fingerprint
        self.existing_id = existing_id


class ConflictQuarantined(DomainError):
    """并发写入相互冲突，材料先隔离，服务恢复后人工/流程继续处理。"""

    def __init__(self, material_id: str, reason: str) -> None:
        super().__init__(f"材料 {material_id} 已隔离：{reason}")
        self.material_id = material_id
        self.reason = reason


class CapacityError(DomainError):
    """试点容量不足，剩余容量不能被重复占用。"""
