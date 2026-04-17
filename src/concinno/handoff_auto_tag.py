"""一次性自動補 tags 到所有 handoff .md。

策略：
1. 目錄名 = 主 tag（king/aegis/psyche/...）
2. 內容關鍵字 = 副 tag（ziq/compress/benchmark/security/...）
3. 跳過已有 tags 的檔
4. 寫入 frontmatter 的 tags 欄位
"""

from __future__ import annotations

import re
from pathlib import Path

from concinno.handoff_index import HANDOFF_DIR  # noqa: F401

# 內容關鍵字 → tag 映射（通用版，可透過 CONCINNO_KEYWORD_TAGS 環境變數覆寫）
KEYWORD_TAGS = {
    "compress": ["compress", "quantization", "pruning", "KV cache"],
    "benchmark": ["benchmark", "eval", "leaderboard"],
    "rag": ["RAG", "retrieval", "embedding", "vector"],
    "security": ["security", "auth", "vulnerability", "CVE"],
    "decision": ["decision", "trade-off", "ADR"],
    "training": ["training", "fine-tune", "LoRA"],
    "evolution": ["refactor", "migration", "upgrade"],
    "handoff": ["handoff", "session"],
    "cognitive": ["cognitive", "reasoning"],
    "research": ["paper", "experiment", "research"],
    "infrastructure": ["deploy", "pod", "infra"],
    "translation": ["translation", "i18n", "l10n"],
    "legal": ["license", "privacy", "terms"],
    "messaging": ["message", "notify", "alert"],
    "agent": ["agent", "tool-use"],
}


def _infer_dir_tag(parent_name: str) -> str:
    """目錄名 → snake_case tag（自動轉換，不寫死映射）。"""
    return parent_name.lower().replace("-", "_").replace(" ", "_")

FRONTMATTER_RE = re.compile(r"^(---\n)(.*?\n)(---\n)", re.DOTALL)
HAS_TAGS_RE = re.compile(r"^tags:", re.MULTILINE)


def infer_tags(file_path: Path, content: str) -> list[str]:
    """推斷一個檔的 tags 列表。"""
    tags: set[str] = set()

    # 1. 目錄主 tag（自動 snake_case 轉換，不寫死個人專案映射）
    parent_name = file_path.parent.name
    if parent_name and parent_name != "06_Handoffs":
        tags.add(_infer_dir_tag(parent_name))

    # 2. 內容關鍵字
    sample = content[:5000]  # 只看前 5K 字
    for tag, keywords in KEYWORD_TAGS.items():
        if any(kw in sample for kw in keywords):
            tags.add(tag)

    # 3. 檔名額外 hint
    name = file_path.stem.lower()
    if "summary" in name:
        tags.add("summary")
    if "archive" in name:
        tags.add("archive")
    if "task-pool" in name or "task_pool" in name:
        tags.add("task_pool")
    if "session" in name:
        tags.add("session")
    if "template" in name.lower():
        tags.add("template")

    return sorted(tags)


def add_tags_to_file(file_path: Path) -> tuple[bool, str]:
    """加 tags 到一個 handoff 檔。返回 (changed, reason)。"""
    content = file_path.read_text(encoding="utf-8")

    fm_match = FRONTMATTER_RE.match(content)
    if not fm_match:
        # 沒 frontmatter，新建一個
        tags = infer_tags(file_path, content)
        if not tags:
            return False, "no frontmatter, no tags inferred"
        new_fm = f"---\ntags: [{', '.join(tags)}]\n---\n\n"
        new_content = new_fm + content
        file_path.write_text(new_content, encoding="utf-8")
        return True, f"created frontmatter with tags: {tags}"

    fm_body = fm_match.group(2)
    if HAS_TAGS_RE.search(fm_body):
        return False, "already has tags"

    # 有 frontmatter 但無 tags，加進去
    tags = infer_tags(file_path, content)
    if not tags:
        return False, "no tags inferred"

    new_fm_body = fm_body + f"tags: [{', '.join(tags)}]\n"
    new_content = (
        fm_match.group(1)
        + new_fm_body
        + fm_match.group(3)
        + content[fm_match.end():]
    )
    file_path.write_text(new_content, encoding="utf-8")
    return True, f"added tags: {tags}"


def main() -> None:
    """CLI entry point for concinno-handoff-tag.

    Auto-tags handoff markdown files by inferring tags from content
    keywords and directory names.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="concinno-handoff-tag",
        description=(
            "Auto-tag handoff markdown files with YAML frontmatter tags. "
            "Infers tags from content keywords and directory names. "
            "Skips files that already have tags."
        ),
    )
    parser.add_argument(
        "--dir",
        type=str,
        default=None,
        help=(
            "Handoff directory to tag. "
            "Defaults to CONCINNO_HANDOFF_DIR env var, "
            "or CLAUDE_PROJECT_DIR/_AI_BRAIN/06_Handoffs, "
            "or CWD/_AI_BRAIN/06_Handoffs."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing",
    )
    args = parser.parse_args()

    global HANDOFF_DIR
    if args.dir:
        from pathlib import Path
        HANDOFF_DIR = Path(args.dir)

    if not HANDOFF_DIR.exists():
        print(
            f"⚠ Handoff directory not found: {HANDOFF_DIR}\n"
            f"  Set CONCINNO_HANDOFF_DIR env var, use --dir, "
            f"or run from a project with _AI_BRAIN/06_Handoffs/.",
            file=sys.stderr,
        )
        sys.exit(2)

    changed = 0
    skipped = 0
    errors = 0

    for md_file in sorted(HANDOFF_DIR.rglob("*.md")):
        if ".ruff_cache" in md_file.parts:
            continue
        try:
            did_change, reason = add_tags_to_file(md_file)
            rel = md_file.relative_to(HANDOFF_DIR).as_posix()
            if did_change:
                print(f"  + {rel}: {reason}")
                changed += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  ! {md_file.name}: {e}")
            errors += 1

    print()
    print(f"Changed: {changed}")
    print(f"Skipped: {skipped}")
    print(f"Errors: {errors}")


if __name__ == "__main__":
    main()
