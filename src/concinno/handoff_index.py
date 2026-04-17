"""Handoff 2.0 索引腳本。

掃描 _AI_BRAIN/06_Handoffs/ 所有 .md，提取：
- YAML frontmatter 的 tags
- corrections 段的 {prohibit, use, reason} 結構
- 每個檔的 last_updated / 行數

輸出 _AI_BRAIN/06_Handoffs/_index.json，模型查詢時讀此 index 定位。

使用：
    python scripts/handoff_index.py          # 重建 index
    python scripts/handoff_index.py --watch  # 持續監視（暫不實作）
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def _resolve_handoff_dir() -> Path:
    """從環境變數或 CWD 找 handoff 目錄。

    優先順序：
    1. CONCINNO_HANDOFF_DIR 環境變數
    2. CLAUDE_PROJECT_DIR/_AI_BRAIN/06_Handoffs
    3. CWD/_AI_BRAIN/06_Handoffs
    """
    env = os.environ.get("CONCINNO_HANDOFF_DIR")
    if env:
        return Path(env)
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        candidate = Path(proj) / "_AI_BRAIN" / "06_Handoffs"
        if candidate.exists():
            return candidate
    return Path.cwd() / "_AI_BRAIN" / "06_Handoffs"


HANDOFF_DIR = _resolve_handoff_dir()
INDEX_FILE = HANDOFF_DIR / "_index.json"

# Frontmatter parser（簡單版，避免引入 PyYAML 依賴）
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TAGS_LINE_RE = re.compile(r"^tags:\s*\[(.*?)\]\s*$", re.MULTILINE)
LAST_UPDATED_RE = re.compile(r"^last_updated:\s*(.+?)\s*$", re.MULTILINE)

# Corrections 段偵測（強制 schema）
CORRECTIONS_SECTION_RE = re.compile(
    r"^##\s+(?:corrections?|糾正|錯誤紀錄)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
CORRECTION_BLOCK_RE = re.compile(
    r"prohibit:\s*(.+?)\n\s*use:\s*(.+?)\n\s*reason:\s*(.+?)"
    r"(?:\n\s*allow_as_lesson:\s*(true|false))?"
    r"(?=\n\s*(?:prohibit:|##|\Z))",
    re.DOTALL,
)


def parse_frontmatter(content: str) -> dict:
    """提取 YAML frontmatter（簡單版）。"""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}

    body = match.group(1)
    result = {}

    tags_match = TAGS_LINE_RE.search(body)
    if tags_match:
        tags_str = tags_match.group(1)
        result["tags"] = [
            t.strip().strip("'").strip('"')
            for t in tags_str.split(",")
            if t.strip()
        ]

    last_updated_match = LAST_UPDATED_RE.search(body)
    if last_updated_match:
        result["last_updated"] = last_updated_match.group(1)

    return result


def parse_corrections(content: str) -> list[dict]:
    """提取 corrections 段的結構化條目。"""
    section_match = CORRECTIONS_SECTION_RE.search(content)
    if not section_match:
        return []

    # 從 corrections 標題到下一個 ## 為止
    start = section_match.end()
    next_section = re.search(r"^##\s+", content[start:], re.MULTILINE)
    end = start + next_section.start() if next_section else len(content)
    section_text = content[start:end]

    corrections = []
    for match in CORRECTION_BLOCK_RE.finditer(section_text):
        # group(4) is optional allow_as_lesson, default false
        # (紅隊 #4 修法：避免 prohibit hard filter 誤殺教學資源)
        allow_lesson = match.group(4) if match.lastindex >= 4 else None
        corrections.append({
            "prohibit": match.group(1).strip(),
            "use": match.group(2).strip(),
            "reason": match.group(3).strip(),
            "allow_as_lesson": allow_lesson == "true",
        })
    return corrections


def extract_chunks(content: str, file_path: Path) -> list[dict]:
    """從 handoff 內容抽 chunks，每個 chunk 帶 parent_section 指針。

    紅隊 #2 共識：細粒度 chunk 是 benchmark 必須，但要保留 parent 解孤島。
    策略：
    - 按 ## heading 切 section
    - 每 section 內按 sentence/bullet 切 chunk
    - 每 chunk 帶 parent_section（指回原 heading）
    - 主進程用 section 讀，benchmark 用 chunk 答
    """
    chunks: list[dict] = []
    rel_path = file_path.relative_to(HANDOFF_DIR).as_posix()

    # 跳過 frontmatter
    fm_match = FRONTMATTER_RE.match(content)
    body_start = fm_match.end() if fm_match else 0
    body = content[body_start:]

    # 按 ## 切 section
    section_pattern = re.compile(r"^##\s+(.+?)$", re.MULTILINE)
    section_matches = list(section_pattern.finditer(body))

    for i, sec_match in enumerate(section_matches):
        section_title = sec_match.group(1).strip()
        sec_start = sec_match.end()
        sec_end = (
            section_matches[i + 1].start()
            if i + 1 < len(section_matches)
            else len(body)
        )
        section_text = body[sec_start:sec_end].strip()

        if not section_text:
            continue

        # 切 chunk：bullet (- /1.) 或句子（。/.）
        # 簡單策略：bullet 為主，連續多行 bullet 合併為一個 chunk
        lines = section_text.split("\n")
        current_chunk: list[str] = []
        chunk_idx = 0

        def flush_chunk():
            nonlocal chunk_idx
            text = "\n".join(current_chunk).strip()
            if text and len(text) >= 10:  # 過濾太短
                chunks.append({
                    "id": f"{rel_path}#{section_title}/{chunk_idx}",
                    "parent_section": section_title,
                    "parent_file": rel_path,
                    "text": text[:512],  # 上限 512 字
                })
                chunk_idx += 1

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if current_chunk:
                    flush_chunk()
                    current_chunk = []
            else:
                current_chunk.append(line)
                # 段落太大 → flush
                if sum(len(x) for x in current_chunk) > 400:
                    flush_chunk()
                    current_chunk = []

        if current_chunk:
            flush_chunk()

    return chunks


def index_handoff(file_path: Path) -> dict:
    """為一個 handoff .md 檔產生 index entry。"""
    content = file_path.read_text(encoding="utf-8")
    fm = parse_frontmatter(content)
    corrections = parse_corrections(content)
    chunks = extract_chunks(content, file_path)

    rel_path = file_path.relative_to(HANDOFF_DIR).as_posix()
    return {
        "path": rel_path,
        "tags": fm.get("tags", []),
        "last_updated": fm.get("last_updated", ""),
        "lines": content.count("\n") + 1,
        "corrections_count": len(corrections),
        "corrections": corrections,
        "chunks_count": len(chunks),
        "chunks": chunks,
    }


def build_index() -> dict:
    """掃描所有 handoff .md，建立完整 index。"""
    entries = []
    skipped = []

    for md_file in sorted(HANDOFF_DIR.rglob("*.md")):
        # 跳過 .ruff_cache 內的檔
        if ".ruff_cache" in md_file.parts:
            continue
        try:
            entries.append(index_handoff(md_file))
        except Exception as e:
            skipped.append({"path": str(md_file), "error": str(e)})

    # 反向索引：tag → files
    tag_index: dict[str, list[str]] = {}
    for entry in entries:
        for tag in entry["tags"]:
            tag_index.setdefault(tag, []).append(entry["path"])

    return {
        "version": "handoff_2.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_files": len(entries),
        "total_corrections": sum(e["corrections_count"] for e in entries),
        "tag_index": tag_index,
        "files": entries,
        "skipped": skipped,
    }


def main() -> None:
    """CLI entry point for concinno-handoff-index.

    Scans handoff directory and builds a searchable index.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="concinno-handoff-index",
        description=(
            "Build a searchable index of handoff markdown files. "
            "Extracts frontmatter tags, corrections schema, "
            "and chunks for search."
        ),
    )
    parser.add_argument(
        "--dir",
        type=str,
        default=None,
        help=(
            "Handoff directory to index. "
            "Defaults to CONCINNO_HANDOFF_DIR env var, "
            "or CLAUDE_PROJECT_DIR/_AI_BRAIN/06_Handoffs, "
            "or CWD/_AI_BRAIN/06_Handoffs."
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON file path. Defaults to <dir>/_index.json",
    )
    args = parser.parse_args()

    # Resolve handoff dir
    global HANDOFF_DIR, INDEX_FILE
    if args.dir:
        HANDOFF_DIR = Path(args.dir)
    if args.output:
        INDEX_FILE = Path(args.output)
    else:
        INDEX_FILE = HANDOFF_DIR / "_index.json"

    # Crash protection
    if not HANDOFF_DIR.exists():
        print(
            f"⚠ Handoff directory not found: {HANDOFF_DIR}\n"
            f"  Set CONCINNO_HANDOFF_DIR env var, use --dir, "
            f"or run from a project with _AI_BRAIN/06_Handoffs/.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"Scanning {HANDOFF_DIR}...")
    index = build_index()

    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Indexed {index['total_files']} files")
    print(f"Total corrections: {index['total_corrections']}")
    print(f"Tags found: {sorted(index['tag_index'].keys())}")
    print(f"Output: {INDEX_FILE}")
    if index["skipped"]:
        print(f"Skipped {len(index['skipped'])} files")


if __name__ == "__main__":
    main()
