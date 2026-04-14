────────────────────── ZIQ ROUTING (which dims matter) ──────────────────────

Match the change type and use the routing table to decide which
dimensions are REQUIRED vs N/A. Filling in N/A still counts — you
must explicitly mark it, not skip it.

  frontend (web UI)        → REQUIRED: W D R O      | N/A: I E
  backend (API)            → REQUIRED: W I D O E    | N/A: R
  library (importable)     → REQUIRED: W I D E      | N/A: R O
  hook (CC/CCC guard)      → REQUIRED: W I D E      | N/A: R O
  migration (DB schema)    → REQUIRED: W D O E      | N/A: I R
  deploy (live infra)      → REQUIRED: W D O        | N/A: I R E
  cli (terminal tool)      → REQUIRED: W I D E      | N/A: R O
  word_doc (.docx layout)  → REQUIRED: I D R        | N/A: W E O
  image (visual asset)     → REQUIRED: D R          | N/A: W I E O
  audio (sound asset)      → REQUIRED: D            | N/A: W I R E O
  video (motion asset)     → REQUIRED: D R          | N/A: W I E O
  db_query (data ops)      → REQUIRED: D            | N/A: W I R E O
  ai_prompt (LLM/Skill)    → REQUIRED: I D E        | N/A: W R O
  build_artifact           → REQUIRED: W D E O      | N/A: I R
  test_only                → REQUIRED: D            | N/A: W I R E O
  docs_only                → AUTO-PASS (return {})

Pick the closest match. If a change spans multiple types, take the
union of REQUIRED and run all matching recipes. When unsure between
two types, pick the stricter.
