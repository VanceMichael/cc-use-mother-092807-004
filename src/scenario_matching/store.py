"""事件溯源存储：每个命令落一条事件，重启后重放即可恢复全部状态。

- JSONL 追加写，落盘带 fsync；进程崩溃在最后一条写坏的行上只丢该条，
  重放时跳过末尾不完整行并记录告警，不影响此前已确认的事实。
- 重复申报去重、冲突材料隔离都在事件流中有迹可循。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


class EventStore:
    """JSONL 事件存储接口的文件实现。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self._events: list[dict[str, Any]] = []
        self.recovery_notes: list[str] = []
        if path is not None and path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or "seq" not in event:
                    raise ValueError("事件结构不完整")
            except (json.JSONDecodeError, ValueError):
                # 末尾写坏（崩溃在写入中途）：隔离该行，后续事件不再信任，
                # 与“冲突材料先隔离”同一处置原则。
                note = f"第 {index} 行事件不完整，已跳过（疑似崩溃残留）"
                self.recovery_notes.append(note)
                break
            self._events.append(event)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    @property
    def next_seq(self) -> int:
        return len(self._events) + 1

    def append(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        event = {"seq": self.next_seq, "type": event_type, "data": data}
        self._events.append(event)
        if self.path is not None:
            self._persist(event)
        return event

    def _persist(self, event: dict[str, Any]) -> None:
        assert self.path is not None
        line = json.dumps(event, ensure_ascii=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 同目录临时文件 + replace 不适合追加流；这里直接追加并 fsync，
        # 崩溃残留由 _load 的容错扫描处理。
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def replay(self, apply_event: Callable[[dict[str, Any]], None]) -> None:
        for event in self._events:
            apply_event(event)

    def simulate_corrupted_tail(self) -> None:
        """测试辅助：制造一条写了一半的崩溃残留。"""
        assert self.path is not None
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": ' + str(self.next_seq) + ', "type": "半截')


def atomic_write_json(path: Path, value: Any) -> None:
    """快照/读模型导出用的原子写：临时文件落盘后替换。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        temp_name = handle.name
    os.replace(temp_name, path)
