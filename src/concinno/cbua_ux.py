"""concinno.cbua_ux — Standardized CBUA UX display codes.

@module cbua_ux
@responsibility Provide a unified display format for all CBUA pipeline stages,
    thinking tools, verification dimensions, and meta-operations. i18n-aware.
@dependencies None (stdlib only)
@exports CbuaCode, cbua_label, cbua_format

Every guard, hook, and cognitive inject that emits a CBUA-tagged message MUST
use this module. No ad-hoc formatting. The format is:

    {Stage}{SubCode} {ToolName}：{Content}

Examples:
    B1 根因：PATH 順序衝突導致 pip 指向 2.7
    A3.D 驗證：50/50 tests green ✅
    ⛔L3 副作用：改 PATH 可能影響其他工具
"""

from __future__ import annotations

import json
import os
from enum import Enum
from typing import Optional

# ── CBUA Display Codes ────────────────────────────────────


class CbuaCode(str, Enum):
    """All valid CBUA display codes.

    Naming: {Stage}_{SubCode}_{Tool}
    Stage: C (Cognize), B (Budget), U (Unify), A (Act), L (Law)
    """

    # ── C: Cognize (感知 + 情報) ──
    C0 = "C0"              # 感知分類
    C0_ALPHA = "C0_α_t"    # ZIQ α_t 信心路由
    C1 = "C1"              # 情報缺口盤點
    C2 = "C2"              # 分層 RAG
    C2_INDEX = "C2.索引"    # L-Index (≤60%)
    C2_SUMMARY = "C2.摘要"  # L-Summary (≤85%)
    C2_FULL = "C2.全文"     # L-Full (無上限)
    C3 = "C3"              # 前提驗證

    # ── B: Budget (思考 + 預算) ──
    B0 = "B0"              # 快速模式
    B1_ROOT = "B1_根因"     # 三層L1 根因
    B1_SWEET = "B1_甜蜜點"  # 三層L2 甜蜜點/CP值
    B1_STRAT = "B1_策略"    # 三層L3 策略
    B1_COT = "B1_CoT"      # Chain of Thought
    B1_FIRST = "B1_第一性"  # 第一性原理
    B1_INVERT = "B1_反轉"   # 反轉思考
    B1_SOCRAT = "B1_蘇格拉底"  # 蘇格拉底式反問
    B1_SIDE = "B1_副作用"   # 副作用分析
    B1_EXIST = "B1_存在質疑"  # 存在質疑
    B1_CP = "B1_CP值"      # CP值/甜蜜點比較
    B2_TOT = "B2_ToT"      # Tree of Thought
    B2_AGOT = "B2_AGoT"    # Graph of Thought
    B3 = "B3"              # 計畫生成
    B4_ANCHOR = "B4_意圖錨"  # 意圖錨定
    B4_HALLU = "B4_幻覺偵"  # 幻覺偵測
    B4_DRIFT = "B4_漂移"    # 漂移偵測
    B5_REFLEX = "B5_Reflexion"  # Reflexion
    B5_MULTI = "B5_多視角"   # 多視角
    B5_ADMIT = "B5_承認不知"  # 承認不知

    # ── U: Unify (對抗 + 統一) ──
    U0_BLUE = "U0_藍隊"    # 藍隊自爆
    U1_RED = "U1_紅隊"     # 紅隊壓測
    U2_BOUND = "U2_邊界"   # 邊界壓力
    U3_THEORY = "U3_理論"  # 理論驗證
    U_VERDICT = "U_判決"   # 綜合判決

    # ── A: Act (執行 + 驗證 + 沉澱 + 防護) ──
    A0 = "A0"              # Pre-check
    A1 = "A1"              # 執行
    A2_BUTTER = "A2_蝴蝶"  # 蝴蝶效應修復
    # WIREDO (A3 子層)
    A3_W = "A3.W"          # 接線
    A3_I = "A3.I"          # 母版
    A3_R = "A3.R"          # 響應
    A3_E = "A3.E"          # 可配
    A3_D = "A3.D"          # 驗證
    A3_O = "A3.O"          # 觀測
    # Convention (A3 企業 SOP 子層)
    A3_NAME = "A3.命名"    # 命名規範
    A3_PLACE = "A3.放置"   # 放置規範
    A3_REUSE = "A3.複用"   # 複用檢查
    # 沉澱
    A4 = "A4"              # 沉澱
    A4_CLEAN = "A4.清"     # 先減（清理）
    A4_WRITE = "A4.寫"     # 後增（寫入）
    A4_FTRL = "A4_FTRL"    # FTRL 權重更新
    # A2A (跨階段通訊層)
    A2A_QUERY = "A2A.查詢"    # C2: 查詢外部知識 Agent
    A2A_REDTEAM = "A2A.紅隊"  # U1: 派紅隊 Agent
    A2A_DELEGATE = "A2A.委派"  # A1: 委派專業 Agent 執行
    A2A_BROADCAST = "A2A.廣播"  # A4: 廣播學習結果
    # 防護
    A5_TADS = "A5_TADS"    # TADS hijack 偵測
    A5_BUTTER = "A5_蝴蝶守"  # 蝴蝶效應 always-on
    A5_DESTRUCT = "A5_破壞守"  # 破壞性操作守衛

    # ── ⛔ Laws (六大定律，跨階段) ──
    L1_CONSERVE = "⛔L1_守恆"   # 認知守恆
    L2_MATCH = "⛔L2_匹配"     # 複雜度匹配
    L3_SIDE = "⛔L3_副作用"    # 副作用意識
    L4_VERIFY = "⛔L4_驗證"    # 驗證至上
    L5_ENTROPY = "⛔L5_反熵"   # 反熵優先
    L6_HONEST = "⛔L6_誠實"    # 誠實定律


# ── i18n Labels ───────────────────────────────────────────

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "C0": "C0 Perceive",
        "C0_α_t": "C0 α_t",
        "C1": "C1 Intel",
        "C2": "C2 RAG",
        "C2.索引": "C2.Index",
        "C2.摘要": "C2.Summary",
        "C2.全文": "C2.Full",
        "C3": "C3 Premise",
        "B0": "B0 Fast",
        "B1_根因": "B1 Root Cause",
        "B1_甜蜜點": "B1 Sweet Spot",
        "B1_策略": "B1 Strategy",
        "B1_CoT": "B1 CoT",
        "B1_第一性": "B1 First Principles",
        "B1_反轉": "B1 Inversion",
        "B1_蘇格拉底": "B1 Socratic",
        "B1_副作用": "B1 Side Effects",
        "B1_存在質疑": "B1 Existence Check",
        "B1_CP值": "B1 CP Value",
        "B2_ToT": "B2 ToT",
        "B2_AGoT": "B2 AGoT",
        "B3": "B3 Plan",
        "B4_意圖錨": "B4 Intent Anchor",
        "B4_幻覺偵": "B4 Hallucination",
        "B4_漂移": "B4 Drift",
        "B5_Reflexion": "B5 Reflexion",
        "B5_多視角": "B5 Multi-Perspective",
        "B5_承認不知": "B5 Admit Unknown",
        "U0_藍隊": "U0 Blue Team",
        "U1_紅隊": "U1 Red Team",
        "U2_邊界": "U2 Boundary",
        "U3_理論": "U3 Theory",
        "U_判決": "U Verdict",
        "A0": "A0 Pre-Check",
        "A1": "A1 Execute",
        "A2_蝴蝶": "A2 Butterfly Fix",
        "A3.W": "A3.W Wired",
        "A3.I": "A3.I Inherited",
        "A3.R": "A3.R Responsive",
        "A3.E": "A3.E Extensible",
        "A3.D": "A3.D Defended",
        "A3.O": "A3.O Observable",
        "A3.命名": "A3.Naming",
        "A3.放置": "A3.Placement",
        "A3.複用": "A3.Reuse",
        "A4": "A4 Sediment",
        "A4.清": "A4.Clean",
        "A4.寫": "A4.Write",
        "A4_FTRL": "A4 FTRL",
        "A5_TADS": "A5 TADS",
        "A5_蝴蝶守": "A5 Butterfly Guard",
        "A5_破壞守": "A5 Destruction Guard",
        "A2A.查詢": "A2A.Query",
        "A2A.紅隊": "A2A.RedTeam",
        "A2A.委派": "A2A.Delegate",
        "A2A.廣播": "A2A.Broadcast",
        "⛔L1_守恆": "⛔L1 Conservation",
        "⛔L2_匹配": "⛔L2 Matching",
        "⛔L3_副作用": "⛔L3 Side Effects",
        "⛔L4_驗證": "⛔L4 Verify",
        "⛔L5_反熵": "⛔L5 Anti-Entropy",
        "⛔L6_誠實": "⛔L6 Honesty",
    },
    "zh-TW": {
        "C0": "C0 感知",
        "C0_α_t": "C0 α_t",
        "C1": "C1 情報",
        "C2": "C2 RAG",
        "C2.索引": "C2.索引",
        "C2.摘要": "C2.摘要",
        "C2.全文": "C2.全文",
        "C3": "C3 前提",
        "B0": "B0 快速",
        "B1_根因": "B1 根因",
        "B1_甜蜜點": "B1 甜蜜點",
        "B1_策略": "B1 策略",
        "B1_CoT": "B1 CoT",
        "B1_第一性": "B1 第一性",
        "B1_反轉": "B1 反轉",
        "B1_蘇格拉底": "B1 蘇格拉底",
        "B1_副作用": "B1 副作用",
        "B1_存在質疑": "B1 存在質疑",
        "B1_CP值": "B1 CP值",
        "B2_ToT": "B2 ToT",
        "B2_AGoT": "B2 AGoT",
        "B3": "B3 計畫",
        "B4_意圖錨": "B4 意圖錨",
        "B4_幻覺偵": "B4 幻覺偵",
        "B4_漂移": "B4 漂移",
        "B5_Reflexion": "B5 Reflexion",
        "B5_多視角": "B5 多視角",
        "B5_承認不知": "B5 承認不知",
        "U0_藍隊": "U0 藍隊",
        "U1_紅隊": "U1 紅隊",
        "U2_邊界": "U2 邊界",
        "U3_理論": "U3 理論",
        "U_判決": "U 判決",
        "A0": "A0 檢查",
        "A1": "A1 執行",
        "A2_蝴蝶": "A2 蝴蝶",
        "A3.W": "A3.W 接線",
        "A3.I": "A3.I 母版",
        "A3.R": "A3.R 響應",
        "A3.E": "A3.E 可配",
        "A3.D": "A3.D 驗證",
        "A3.O": "A3.O 觀測",
        "A3.命名": "A3.命名",
        "A3.放置": "A3.放置",
        "A3.複用": "A3.複用",
        "A4": "A4 沉澱",
        "A4.清": "A4.清",
        "A4.寫": "A4.寫",
        "A4_FTRL": "A4 FTRL",
        "A5_TADS": "A5 TADS",
        "A5_蝴蝶守": "A5 蝴蝶守",
        "A5_破壞守": "A5 破壞守",
        "A2A.查詢": "A2A.查詢",
        "A2A.紅隊": "A2A.紅隊",
        "A2A.委派": "A2A.委派",
        "A2A.廣播": "A2A.廣播",
        "⛔L1_守恆": "⛔L1 守恆",
        "⛔L2_匹配": "⛔L2 匹配",
        "⛔L3_副作用": "⛔L3 副作用",
        "⛔L4_驗證": "⛔L4 驗證",
        "⛔L5_反熵": "⛔L5 反熵",
        "⛔L6_誠實": "⛔L6 誠實",
    },
}

# ── Config ────────────────────────────────────────────────

_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "hooks", "cc_config.json",
)


def _get_locale() -> str:
    """Read locale from cc_config.json, default to 'en'."""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return str(cfg.get("locale", "en"))
    except (OSError, ValueError):
        return "en"


# ── Public API ────────────────────────────────────────────


def cbua_label(code: CbuaCode, locale: Optional[str] = None) -> str:
    """Get the localized label for a CBUA code.

    Args:
        code: The CBUA display code.
        locale: Override locale. If None, reads from cc_config.json.

    Returns:
        Localized label string (e.g., "B1 根因" or "B1 Root Cause").
    """
    loc = locale or _get_locale()
    labels = _LABELS.get(loc, _LABELS["en"])
    return labels.get(code.value, code.value)


def cbua_format(code: CbuaCode, content: str, locale: Optional[str] = None) -> str:
    """Format a CBUA UX message.

    This is the ONLY sanctioned way to emit CBUA-tagged messages.
    All guards, hooks, and cognitive injects MUST use this function.

    Args:
        code: The CBUA display code.
        content: The specific content/observation.
        locale: Override locale.

    Returns:
        Formatted string like "B1 根因：PATH 順序衝突".

    Example::

        >>> cbua_format(CbuaCode.B1_ROOT, "PATH 順序衝突導致 pip 指向 2.7")
        'B1 根因：PATH 順序衝突導致 pip 指向 2.7'

        >>> cbua_format(CbuaCode.A3_D, "50/50 tests green ✅")
        'A3.D 驗證：50/50 tests green ✅'

        >>> cbua_format(CbuaCode.U1_RED, "#1 學術砲轟 3 FATAL")
        'U1 紅隊：#1 學術砲轟 3 FATAL'
    """
    label = cbua_label(code, locale)
    sep = "：" if (locale or _get_locale()) == "zh-TW" else ": "
    return f"{label}{sep}{content}"


def cbua_wiredo(
    results: dict[str, tuple[bool, str]],
    locale: Optional[str] = None,
) -> str:
    """Format a WIREDO verification block.

    Args:
        results: Dict mapping WIREDO letter to (passed, detail).
            Keys: "W", "I", "R", "E", "D", "O".

    Returns:
        Multi-line WIREDO block.

    Example::

        >>> cbua_wiredo({"W": (True, "import ok"), "D": (False, "no test")})
        'A3.W 接線：import ok ✅\\nA3.D 驗證：no test ❌'
    """
    code_map = {
        "W": CbuaCode.A3_W,
        "I": CbuaCode.A3_I,
        "R": CbuaCode.A3_R,
        "E": CbuaCode.A3_E,
        "D": CbuaCode.A3_D,
        "O": CbuaCode.A3_O,
    }
    lines: list[str] = []
    for letter in "WIREDO":
        if letter not in results:
            continue
        passed, detail = results[letter]
        mark = " ✅" if passed else " ❌"
        lines.append(cbua_format(code_map[letter], detail + mark, locale))
    return "\n".join(lines)


def cbua_three_layer(
    root_cause: str,
    sweet_spot: str,
    strategy: str,
    locale: Optional[str] = None,
) -> str:
    """Format a three-layer thinking block (根因→甜蜜點→策略).

    Returns:
        Multi-line three-layer block.
    """
    return "\n".join([
        cbua_format(CbuaCode.B1_ROOT, root_cause, locale),
        cbua_format(CbuaCode.B1_SWEET, sweet_spot, locale),
        cbua_format(CbuaCode.B1_STRAT, strategy, locale),
    ])


def cbua_redteam_verdict(
    hit: int,
    miss: int,
    actions: str,
    locale: Optional[str] = None,
) -> str:
    """Format a red team verdict summary.

    Args:
        hit: Number of red team findings adopted.
        miss: Number rejected (打偏).
        actions: Summary of adopted changes.
    """
    content = f"打中{hit}/打偏{miss} → {actions}"
    return cbua_format(CbuaCode.U_VERDICT, content, locale)


def cbua_clean_write(
    cleaned: int,
    written: int,
    locale: Optional[str] = None,
) -> str:
    """Format a 反熵優先 (entropy-first) summary.

    Args:
        cleaned: Lines removed.
        written: Lines added.
    """
    net = written - cleaned
    sign = "+" if net > 0 else ""
    clean_msg = cbua_format(
        CbuaCode.A4_CLEAN, f"刪 {cleaned} 行", locale,
    )
    write_msg = cbua_format(
        CbuaCode.A4_WRITE, f"加 {written} 行（淨{sign}{net}）", locale,
    )
    return f"{clean_msg}\n{write_msg}"
