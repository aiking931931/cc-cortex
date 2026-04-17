"""concinno.memory_palace — 空間結構化記憶系統。

借鑑 MemPalace（Milla Jovovich 開源）的三個核心設計：
1. 逐字儲存優先（不讓 AI 決定記什麼）
2. 空間過濾（Wing+Room 縮小搜索 +34% recall）
3. 分層載入（L0 ~50t identity → L1 ~120t facts → L2 按需）

結構：Palace → Wing（專案/角色）→ Room（主題）→ Memory
跨 Wing 關聯用 Tunnel。

@module memory_palace
@responsibility 結構化記憶存取、空間過濾搜索、分層摘要
@dependencies concinno.core.state_store
@exports MemoryPalace, Memory, Room, Wing, Tunnel
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from concinno.core.state_store import StateStore

logger = logging.getLogger("concinno.memory_palace")

# ── 容量限制 ──────────────────────────────────────────
MAX_MEMORIES_PER_ROOM = 500
MAX_ROOMS_PER_WING = 50
MAX_WINGS = 20

# ── 資料結構 ────────────────────────────────────────────


@dataclass
class Memory:
    """單一記憶項目。"""

    id: str
    content: str
    source_type: str  # "user", "correction", "observation", "derived"
    importance: float  # 0.0 ~ 1.0
    created_at: float  # epoch
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化為 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Memory:
        """從 dict 反序列化。"""
        return cls(
            id=data["id"],
            content=data["content"],
            source_type=data["source_type"],
            importance=data["importance"],
            created_at=data["created_at"],
            tags=data.get("tags", []),
        )


@dataclass
class Room:
    """主題房間，包含多個 Memory。"""

    name: str
    memories: list[Memory] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "name": self.name,
            "memories": [m.to_dict() for m in self.memories],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Room:
        """反序列化。"""
        return cls(
            name=data["name"],
            memories=[Memory.from_dict(m) for m in data.get("memories", [])],
        )


@dataclass
class Wing:
    """專案/角色翼，包含多個 Room。"""

    name: str
    rooms: dict[str, Room] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "name": self.name,
            "rooms": {k: v.to_dict() for k, v in self.rooms.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Wing:
        """反序列化。"""
        return cls(
            name=data["name"],
            rooms={k: Room.from_dict(v) for k, v in data.get("rooms", {}).items()},
        )


@dataclass
class Tunnel:
    """跨 Wing 的交叉引用。"""

    id: str
    wing_a: str
    room_a: str
    wing_b: str
    room_b: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tunnel:
        """反序列化。"""
        return cls(
            id=data["id"],
            wing_a=data["wing_a"],
            room_a=data["room_a"],
            wing_b=data["wing_b"],
            room_b=data["room_b"],
            created_at=data["created_at"],
        )


# ── 輕量關鍵字搜索 ──────────────────────────────────────

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "and",
        "or",
        "not",
        "it",
        "this",
        "that",
        "with",
        "be",
        "as",
        "by",
        "from",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "but",
        "if",
        "no",
        "so",
        "的",
        "了",
        "是",
        "在",
        "和",
        "也",
        "就",
        "不",
        "都",
        "而",
        "及",
        "與",
        "或",
        "等",
        "被",
        "把",
    }
)

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """拆分為小寫 token，移除停用詞。"""
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if t not in _STOP_WORDS]


def _keyword_score(query_tokens: list[str], content: str) -> float:
    """計算關鍵字匹配分數（TF-IDF 簡化版）。

    回傳 0.0~1.0 之間的分數。
    """
    if not query_tokens:
        return 0.0
    content_lower = content.lower()
    content_tokens = set(_tokenize(content_lower))
    matched = sum(1 for t in query_tokens if t in content_tokens)
    # 額外加分：完整子字串匹配
    substring_bonus = 0.0
    for t in query_tokens:
        if t in content_lower:
            substring_bonus += 0.1
    base = matched / len(query_tokens)
    return min(1.0, base + substring_bonus)


# ── MemoryPalace 主類 ───────────────────────────────────


class MemoryPalace:
    """空間結構化記憶系統。

    借鑑 MemPalace 的三個核心設計：
    1. 逐字儲存優先（不讓 AI 決定記什麼）
    2. 空間過濾（Wing+Room 縮小搜索 +34% recall）
    3. 分層載入（L0 ~50t identity → L1 ~120t facts → L2 按需）
    """

    NAMESPACE = "memory_palace"
    FILENAME = "palace.json"

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._wings: dict[str, Wing] = {}
        self._tunnels: list[Tunnel] = []
        self._load()

    # ── 持久化 ──────────────────────────────────────────

    def _load(self) -> None:
        """從 StateStore 載入完整 Palace。"""
        data = self._store.read_flat(self.NAMESPACE, self.FILENAME, default={})
        for name, wing_data in data.get("wings", {}).items():
            self._wings[name] = Wing.from_dict(wing_data)
        for tunnel_data in data.get("tunnels", []):
            self._tunnels.append(Tunnel.from_dict(tunnel_data))

    def _save(self) -> None:
        """持久化到 StateStore。"""
        data = {
            "wings": {k: v.to_dict() for k, v in self._wings.items()},
            "tunnels": [t.to_dict() for t in self._tunnels],
        }
        self._store.write_flat(self.NAMESPACE, self.FILENAME, data)

    # ── 核心 API ────────────────────────────────────────

    def add_memory(
        self,
        wing: str,
        room: str,
        content: str,
        source_type: str = "observation",
        importance: float = 0.5,
        *,
        tags: list[str] | None = None,
    ) -> Memory:
        """新增記憶到指定 Wing/Room。

        Args:
            wing: Wing 名稱（專案/角色）。
            room: Room 名稱（主題）。
            content: 記憶內容（逐字儲存）。
            source_type: 來源類型（user/correction/observation/derived）。
            importance: 重要度 0.0~1.0。
            tags: 可選標籤列表。

        Returns:
            新建的 Memory 物件。
        """
        # P1 fix: validate inputs
        if not wing or not isinstance(wing, str):
            raise ValueError("wing must be a non-empty string")
        if not room or not isinstance(room, str):
            raise ValueError("room must be a non-empty string")
        importance = max(0.0, min(1.0, importance))

        # Capacity checks
        if wing not in self._wings and len(self._wings) >= MAX_WINGS:
            raise ValueError(f"Max wings ({MAX_WINGS}) reached")
        if wing not in self._wings:
            self._wings[wing] = Wing(name=wing)
        w = self._wings[wing]
        if room not in w.rooms and len(w.rooms) >= MAX_ROOMS_PER_WING:
            raise ValueError(f"Max rooms ({MAX_ROOMS_PER_WING}) in '{wing}'")
        if room not in w.rooms:
            w.rooms[room] = Room(name=room)
        r = w.rooms[room]
        if len(r.memories) >= MAX_MEMORIES_PER_ROOM:
            # R1 fix: never evict user memories
            evictable = [
                m for m in r.memories if m.source_type != "user"
            ]
            if evictable:
                evictable.sort(key=lambda m: m.importance)
                r.memories.remove(evictable[0])
            else:
                r.memories.sort(key=lambda m: m.importance)
                r.memories.pop(0)

        mem = Memory(
            id=uuid.uuid4().hex[:12],
            content=content,
            source_type=source_type,
            importance=importance,
            created_at=time.time(),
            tags=tags or [],
        )
        r.memories.append(mem)
        self._save()
        logger.debug("memory_palace: added %s to %s/%s", mem.id, wing, room)
        return mem

    def search(
        self,
        query: str,
        *,
        wing: str | None = None,
        room: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """空間過濾搜索。

        先用 wing+room 縮小範圍，再用關鍵字匹配排序。
        回傳 top_k 個結果，每個包含 memory、wing、room、score。

        Args:
            query: 搜索查詢。
            wing: 限定 Wing（None = 全部）。
            room: 限定 Room（None = 全部）。
            top_k: 回傳數量上限。
            min_score: 最低分數門檻。

        Returns:
            搜索結果列表，按 (score * importance) 降序。
        """
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        candidates: list[dict[str, Any]] = []

        wings_to_search = (
            {wing: self._wings[wing]} if wing and wing in self._wings else self._wings
        )

        for w_name, w in wings_to_search.items():
            rooms_to_search = (
                {room: w.rooms[room]} if room and room in w.rooms else w.rooms
            )
            for r_name, r in rooms_to_search.items():
                for mem in r.memories:
                    score = _keyword_score(query_tokens, mem.content)
                    # 標籤也參與匹配
                    if mem.tags:
                        tag_text = " ".join(mem.tags)
                        tag_score = _keyword_score(query_tokens, tag_text)
                        score = max(score, tag_score)
                    if score < min_score:
                        continue
                    # 綜合分數 = 匹配分數 * 重要度加權
                    combined = score * (0.5 + 0.5 * mem.importance)
                    candidates.append(
                        {
                            "memory": mem,
                            "wing": w_name,
                            "room": r_name,
                            "score": combined,
                        }
                    )

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:top_k]

    def get_summary(
        self,
        wing: str | None = None,
        depth: Literal["L0", "L1", "L2"] = "L1",
    ) -> str:
        """分層摘要。

        L0（~50t）：Wing 名稱列表 + 各 Wing 的 Room 數量。
        L1（~120t）：每個 Wing 的重要記憶（importance >= 0.7）。
        L2：指定 Wing 的全部記憶。

        Args:
            wing: L2 時必須指定。L0/L1 忽略。
            depth: 摘要深度。

        Returns:
            格式化的摘要文字。
        """
        if depth == "L0":
            return self._summary_l0()
        if depth == "L1":
            return self._summary_l1()
        # L2
        if not wing or wing not in self._wings:
            available = list(self._wings.keys())
            return f"[L2] Wing '{wing}' 不存在。可用：{available}"
        return self._summary_l2(wing)

    def _summary_l0(self) -> str:
        """L0：Wing 列表 + Room 計數。"""
        if not self._wings:
            return "[L0] Palace 為空。"
        lines = ["[L0] Palace 結構："]
        for name, w in self._wings.items():
            mem_count = sum(len(r.memories) for r in w.rooms.values())
            lines.append(f"  - {name}: {len(w.rooms)} rooms, {mem_count} memories")
        return "\n".join(lines)

    def _summary_l1(self) -> str:
        """L1：每 Wing 重要記憶。"""
        if not self._wings:
            return "[L1] Palace 為空。"
        lines = ["[L1] 重要記憶摘要："]
        for name, w in self._wings.items():
            lines.append(f"\n## {name}")
            important = []
            for r in w.rooms.values():
                for m in r.memories:
                    if m.importance >= 0.7:
                        important.append((r.name, m))
            if not important:
                lines.append("  （無重要記憶）")
                continue
            important.sort(key=lambda x: x[1].importance, reverse=True)
            for r_name, m in important[:5]:  # 每 Wing 最多 5 筆
                lines.append(f"  [{r_name}] {m.content[:80]}")
        return "\n".join(lines)

    def _summary_l2(self, wing: str) -> str:
        """L2：指定 Wing 全部記憶。"""
        w = self._wings[wing]
        lines = [f"[L2] Wing '{wing}' 完整內容："]
        for r_name, r in w.rooms.items():
            lines.append(f"\n### {r_name} ({len(r.memories)} memories)")
            for m in r.memories:
                tag_str = f" [{', '.join(m.tags)}]" if m.tags else ""
                lines.append(
                    f"  - [{m.source_type}|{m.importance:.1f}]{tag_str} {m.content}"
                )
        return "\n".join(lines)

    def create_tunnel(
        self,
        wing_a: str,
        room_a: str,
        wing_b: str,
        room_b: str,
    ) -> Tunnel:
        """建立跨 Wing 交叉引用。

        Args:
            wing_a: 來源 Wing。
            room_a: 來源 Room。
            wing_b: 目標 Wing。
            room_b: 目標 Room。

        Returns:
            新建的 Tunnel 物件。

        Raises:
            ValueError: Wing 或 Room 不存在時。
        """
        for w_name, r_name in [(wing_a, room_a), (wing_b, room_b)]:
            if w_name not in self._wings:
                msg = f"Wing '{w_name}' 不存在"
                raise ValueError(msg)
            if r_name not in self._wings[w_name].rooms:
                msg = f"Room '{r_name}' 在 Wing '{w_name}' 中不存在"
                raise ValueError(msg)

        # 檢查重複
        for t in self._tunnels:
            same_forward = (
                t.wing_a == wing_a
                and t.room_a == room_a
                and t.wing_b == wing_b
                and t.room_b == room_b
            )
            same_reverse = (
                t.wing_a == wing_b
                and t.room_a == room_b
                and t.wing_b == wing_a
                and t.room_b == room_a
            )
            if same_forward or same_reverse:
                return t  # 已存在，回傳現有

        tunnel = Tunnel(
            id=uuid.uuid4().hex[:12],
            wing_a=wing_a,
            room_a=room_a,
            wing_b=wing_b,
            room_b=room_b,
            created_at=time.time(),
        )
        self._tunnels.append(tunnel)
        self._save()
        logger.debug(
            "memory_palace: tunnel %s/%s <-> %s/%s",
            wing_a,
            room_a,
            wing_b,
            room_b,
        )
        return tunnel

    # ── 查詢輔助 ────────────────────────────────────────

    def list_wings(self) -> list[str]:
        """列出所有 Wing 名稱。"""
        return list(self._wings.keys())

    def list_rooms(self, wing: str) -> list[str]:
        """列出指定 Wing 的所有 Room 名稱。"""
        if wing not in self._wings:
            return []
        return list(self._wings[wing].rooms.keys())

    def list_tunnels(self) -> list[Tunnel]:
        """列出所有 Tunnel。"""
        return list(self._tunnels)

    def get_tunnels_for(self, wing: str, room: str | None = None) -> list[Tunnel]:
        """取得與指定 Wing/Room 相關的 Tunnel。"""
        results = []
        for t in self._tunnels:
            match_a = t.wing_a == wing and (room is None or t.room_a == room)
            match_b = t.wing_b == wing and (room is None or t.room_b == room)
            if match_a or match_b:
                results.append(t)
        return results

    def get_memory_count(self, wing: str | None = None) -> int:
        """取得記憶總數（可限定 Wing）。"""
        total = 0
        wings = (
            {wing: self._wings[wing]}
            if wing and wing in self._wings
            else self._wings
        )
        for w in wings.values():
            for r in w.rooms.values():
                total += len(r.memories)
        return total

    def delete_memory(self, wing: str, room: str, memory_id: str) -> bool:
        """刪除指定記憶。回傳是否成功。"""
        if wing not in self._wings:
            return False
        w = self._wings[wing]
        if room not in w.rooms:
            return False
        r = w.rooms[room]
        before = len(r.memories)
        r.memories = [m for m in r.memories if m.id != memory_id]
        if len(r.memories) < before:
            self._save()
            return True
        return False

    @property
    def stats(self) -> dict[str, Any]:
        """Palace 統計摘要。"""
        wing_count = len(self._wings)
        room_count = sum(len(w.rooms) for w in self._wings.values())
        mem_count = self.get_memory_count()
        tunnel_count = len(self._tunnels)
        return {
            "wings": wing_count,
            "rooms": room_count,
            "memories": mem_count,
            "tunnels": tunnel_count,
        }
