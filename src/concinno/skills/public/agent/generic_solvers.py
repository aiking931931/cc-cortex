"""Generic hybrid vision solvers — public OSS API.

Extracted from ``gaia_agent.py`` as part of wave-1 refactor (cont'd¹⁴) so
any vision-arithmetic / OCR-with-rule agent can import and reuse these
pipelines without depending on GAIA-runner glue.

Three public solvers:

* :func:`solve_orthogonal_polygon_via_opencv_hybrid`
  OpenCV vertex extraction + narrow Sonnet OCR + Python shoelace.

* :func:`solve_colour_coded_numeric_via_hybrid`
  OpenCV colour-isolation + narrow Sonnet OCR + Python statistics.

* :func:`solve_image_quiz_scoring_via_hybrid`
  Sonnet OCR/classification + deterministic ``fractions`` judge + arithmetic
  plan compute.

All three share the same design contract:
  - Return ``(answer: str, info: dict)``.
  - Empty answer string on any pipeline failure (caller falls through to
    alternative solver).
  - ``info`` carries audit fields for evidence smoke output.
  - Feature-gated by ``concinno.feature_config`` (callers control routing;
    these functions always execute when called directly).
  - No task-specific entity tokens in detectors or prompts (generic over
    question text).

Internal helpers follow the same ``_`` prefix convention and are **not**
re-exported. The only public surface is the three ``solve_*`` functions and
the detector predicates ``is_orthogonal_polygon_area_question``,
``is_colour_coded_numeric_data_question``, ``is_image_quiz_scoring_question``.
"""
from __future__ import annotations

import base64
import json
import os
import re

__all__ = [
    # ── Public solvers ──────────────────────────────────────────────────────
    "solve_orthogonal_polygon_via_opencv_hybrid",
    "solve_colour_coded_numeric_via_hybrid",
    "solve_image_quiz_scoring_via_hybrid",
    # ── Detector predicates ─────────────────────────────────────────────────
    "is_orthogonal_polygon_area_question",
    "is_colour_coded_numeric_data_question",
    "is_image_quiz_scoring_question",
    # ── Low-level helpers re-exported for tests ─────────────────────────────
    "extract_json_object",
]

# ---------------------------------------------------------------------------
# Shared infrastructure (self-contained — no circular import with gaia_agent)
# ---------------------------------------------------------------------------

_anthropic_client = None


def _get_anthropic():
    global _anthropic_client  # noqa: PLW0603
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def _model_drops_temperature(model: str) -> bool:
    """Return True when the named Anthropic model rejects ``temperature``.

    Newer reasoning-tier models (claude-opus-4-7 onward) return HTTP 400
    ``temperature is deprecated for this model`` when the parameter is sent.
    The helper is a name-prefix check so future opus minor releases are
    covered without further edits.
    """
    if not model:
        return False
    name = model.lower()
    return name.startswith("claude-opus-4-7") or name.startswith(
        ("claude-opus-4-8", "claude-opus-4-9", "claude-opus-5"),
    )


MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp",
}

# ---------------------------------------------------------------------------
# Detector regexes (generic, no task-specific entity tokens)
# ---------------------------------------------------------------------------

_POLYGON_AREA_RE = re.compile(
    r"\b(area|surface)\b.{0,80}\b(polygon|shape|figure|region|"
    r"label(?:s|ed)?|side\s*length|edges?|cm|mm|inch|inches|"
    r"meters?|metres?|ft|feet|units?)\b|"
    r"\b(polygon|shape|figure|region)\b.{0,80}\b(area|surface)\b",
    re.I | re.S,
)

_COLOUR_NAME_RE = re.compile(
    r"\b(red|green|blue|yellow|orange|purple|cyan|magenta|"
    r"pink|black)\b",
    re.I,
)
_NUMERIC_OP_RE = re.compile(
    r"\b(average|mean|median|sum|product|standard\s+deviation|"
    r"deviation|variance|range|max(?:imum)?|min(?:imum)?|"
    r"percentage|percent|ratio|count)\b",
    re.I,
)
_NUMBER_NOUN_RE = re.compile(r"\bnumbers?\b", re.I)

_QUIZ_SCORE_RULE_RE = re.compile(
    r"([Pp]roblem[^:\n]*?):\s*(\d+)\s+points?",
)
_QUIZ_BONUS_RE = re.compile(r"(\d+)\s+bonus\s+points?", re.I)
_QUIZ_SCORED_AS_FOLLOWS_RE = re.compile(r"scored\s+as\s+follows", re.I)
_QUIZ_MIXED_RE = re.compile(r"^\s*(-?\d+)\s+(\d+)\s*/\s*(\d+)\s*$")

# ---------------------------------------------------------------------------
# Detector predicates (public)
# ---------------------------------------------------------------------------


def is_orthogonal_polygon_area_question(question: str) -> bool:
    """Return True when the question asks for the area of an orthogonal polygon.

    Generic: uses structural regex tokens only (area/surface + polygon/shape/
    figure/region and common unit words). No task-specific entity tokens.
    """
    return bool(_POLYGON_AREA_RE.search(question))


def is_colour_coded_numeric_data_question(question: str) -> bool:
    """Return True when the question asks for arithmetic over colour-tagged
    numbers in an image.

    Two signals required:
      1. ≥2 distinct colour names mentioned.
      2. ≥1 numeric operation keyword AND the word "number(s)".

    Anti-leakage: reads only structural tokens.
    """
    if not _NUMBER_NOUN_RE.search(question):
        return False
    if not _NUMERIC_OP_RE.search(question):
        return False
    found_colours = {m.lower() for m in _COLOUR_NAME_RE.findall(question)}
    return len(found_colours) >= 2


def is_image_quiz_scoring_question(
    question: str, file_path: str | None,
) -> bool:
    """Return True when the question asks the agent to grade a quiz in an
    attached image given a per-type scoring rule.

    Conditions:
      1. ``file_path`` is a non-empty string ending in an image extension.
      2. Question text either says ``scored as follows`` OR contains ≥2
         ``N points`` rule clauses with ≥1 classifiable problem-type phrase.
    """
    if not question:
        return False
    if not file_path:
        return False
    if not str(file_path).lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"),
    ):
        return False
    rules, _ = _parse_quiz_scoring_rules(question)
    if _QUIZ_SCORED_AS_FOLLOWS_RE.search(question) and rules:
        return True
    return len(rules) >= 2


# ---------------------------------------------------------------------------
# Shared JSON helper
# ---------------------------------------------------------------------------

_POLYGON_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.I,
)
_POLYGON_FIRST_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(raw: str) -> str | None:
    """Pull the first JSON object out of a model response.

    Handles three shapes seen in practice: (a) pure JSON, (b) ```json
    fenced``` block, (c) JSON embedded after prose. Returns the object
    substring (still text) or None when no candidate is found.
    """
    if not raw:
        return None
    fenced = _POLYGON_JSON_FENCE_RE.search(raw)
    if fenced:
        return fenced.group(1)
    naked = _POLYGON_FIRST_OBJECT_RE.search(raw)
    return naked.group(0) if naked else None


# Private alias (used throughout this module)
_extract_json_object = extract_json_object

# ---------------------------------------------------------------------------
# ── Polygon area: structured-JSON multipass (legacy path) ──────────────────
# ---------------------------------------------------------------------------

_POLYGON_STRUCTURED_PROMPT = (
    "[Orthogonal polygon area — structured analysis]\n"
    "Look at the image. It shows an orthogonal (axis-aligned) polygon "
    "with side lengths labelled. Output a single JSON object using the "
    "exact schema below — NO prose before or after, NO markdown fence, "
    "JSON only.\n\n"
    "Required schema:\n"
    "{\n"
    "  \"labels_visible\": [<every numeric label you can read on the "
    "image, as a number; include duplicates; ignore scale bars / year "
    "watermarks / logos>],\n"
    "  \"rectangles\": [\n"
    "    {\"width\": <number>, \"height\": <number>, "
    "\"explanation\": \"<one short sentence: which labels gave width "
    "and height>\"}\n"
    "    // exactly N entries; N = (concave-corner count + 1) for a "
    "simply-connected orthogonal polygon, or higher for shapes with "
    "holes. An L-shape is N=2, a C/T-shape is usually N=3, and a "
    "staircase with k steps is N=k+1.\n"
    "  ],\n"
    "  \"edge_sums\": {\n"
    "    \"horizontal_right\": <sum of edge lengths going RIGHT around "
    "the boundary>,\n"
    "    \"horizontal_left\":  <sum going LEFT>,\n"
    "    \"vertical_down\":    <sum going DOWN>,\n"
    "    \"vertical_up\":      <sum going UP>\n"
    "  },\n"
    "  \"computed_area\": <number, your sum of width*height across all "
    "rectangles>\n"
    "}\n\n"
    "Closure constraint (you MUST satisfy before answering):\n"
    "  horizontal_right == horizontal_left\n"
    "  vertical_down    == vertical_up\n"
    "If your edge_sums fail closure, re-trace the boundary and adjust "
    "until they balance — then update rectangles and computed_area "
    "accordingly. Do NOT output a JSON whose closure is broken.\n\n"
    "Decomposition rule: every rectangle's width and height MUST come "
    "from the labels_visible list (or be deducible by subtracting "
    "smaller labels along the same axis from a larger one). Do not "
    "invent numbers.\n"
)


def _validate_polygon_pass(obj: dict, tol: float = 0.51) -> float | None:
    """Return rectangle-derived area when the structured pass is valid.

    Validation rules:
      * ``rectangles`` is a non-empty list of ``{width, height}`` numbers
      * ``edge_sums`` keys exist and are numbers
      * ``horizontal_right`` matches ``horizontal_left`` within ``tol``
      * ``vertical_down`` matches ``vertical_up`` within ``tol``
      * Recomputed ``sum(w*h)`` matches the model's ``computed_area``
        within ``tol`` (catches arithmetic errors in-pass)

    Returns the deterministically-recomputed area on pass, ``None`` on
    any validation failure.
    """
    rects = obj.get("rectangles")
    if not isinstance(rects, list) or not rects:
        return None
    try:
        widths_heights: list[tuple[float, float]] = []
        for r in rects:
            if not isinstance(r, dict):
                return None
            w = float(r["width"])
            h = float(r["height"])
            if w <= 0 or h <= 0:
                return None
            widths_heights.append((w, h))
    except (KeyError, TypeError, ValueError):
        return None
    edge_sums = obj.get("edge_sums")
    if not isinstance(edge_sums, dict):
        return None
    try:
        hr = float(edge_sums["horizontal_right"])
        hl = float(edge_sums["horizontal_left"])
        vd = float(edge_sums["vertical_down"])
        vu = float(edge_sums["vertical_up"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(hr - hl) > tol or abs(vd - vu) > tol:
        return None
    recomputed = sum(w * h for w, h in widths_heights)
    try:
        model_area = float(obj.get("computed_area", 0))
    except (TypeError, ValueError):
        return None
    if abs(recomputed - model_area) > tol:
        return None
    return recomputed


def _format_polygon_area(value: float) -> str:
    """Render a polygon area value as the answer string.

    Half-integer-aware: if ``value`` rounds to an integer within 0.05,
    return the integer string; otherwise keep one fractional digit.
    """
    nearest_int = round(value)
    if abs(value - nearest_int) < 0.05:
        return str(int(nearest_int))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _solve_polygon_structured_multipass(
    question: str,
    image_path: str,
    *,
    model: str = "claude-sonnet-4-6",
    passes_count: int = 5,
) -> tuple[str, list[dict]]:
    """Polygon-area solver using structured-JSON multipass + closure.

    Each pass requests a JSON object with rectangle decomposition and
    direction-keyed edge sums. The Python side validates closure and
    re-derives area from rectangles, dropping passes whose decomposition
    fails closure or whose self-claimed area disagrees with the
    rectangle sum. Returns ``(voted_answer, raw_pass_records)`` so
    callers can audit per-pass output. Universal for any orthogonal
    polygon area question — no task-specific keywords, no expected-
    answer reading.

    On total validation failure (zero valid passes) returns ``("", ...)``
    so the caller can fall back to plain vision multipass.
    """
    ext = os.path.splitext(image_path)[1].lower()
    mime = MIME_MAP.get(ext, "image/png")
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as err:
        print(
            f"  [polygon-structured read error] {err}",
            flush=True,
        )
        return "", []
    user_text = (
        f"{_POLYGON_STRUCTURED_PROMPT}\n\nQuestion: {question}\n"
    )
    request_kwargs: dict[str, object] = {
        "model": model,
        "max_tokens": 2000,
        "timeout": 120.0,
    }
    if not _model_drops_temperature(model):
        request_kwargs["temperature"] = 0.0
    try:
        client = _get_anthropic()
    except Exception as err:
        print(f"  [polygon-structured client error] {err}", flush=True)
        return "", []
    pass_records: list[dict] = []
    valid_areas: list[float] = []
    for pass_idx in range(max(1, passes_count)):
        try:
            resp = client.messages.create(
                **request_kwargs,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": mime,
                            "data": b64,
                        }},
                        {"type": "text", "text": user_text},
                    ],
                }],
            )
        except Exception as err:
            pass_records.append({"pass": pass_idx, "error": str(err)})
            continue
        raw = resp.content[0].text if resp.content else ""
        obj_str = _extract_json_object(raw)
        rec: dict = {"pass": pass_idx, "raw_excerpt": raw[:300]}
        if not obj_str:
            rec["error"] = "no JSON object"
            pass_records.append(rec)
            continue
        try:
            obj = json.loads(obj_str)
        except json.JSONDecodeError as je:
            rec["error"] = f"json decode: {je}"
            pass_records.append(rec)
            continue
        area = _validate_polygon_pass(obj)
        rec["obj"] = obj
        rec["validated_area"] = area
        pass_records.append(rec)
        if area is not None:
            valid_areas.append(area)
    if not valid_areas:
        return "", pass_records
    import statistics
    median = statistics.median(valid_areas)
    return _format_polygon_area(median), pass_records


# ---------------------------------------------------------------------------
# ── Polygon area: OpenCV + narrow OCR + Python shoelace (primary path) ─────
# ---------------------------------------------------------------------------

_POLYGON_HUE_RANGES: tuple[tuple[str, tuple[int, int, int], tuple[int, int, int]], ...] = (
    ("green",  (35, 60, 60),  (85, 255, 255)),
    ("blue",   (100, 60, 60), (130, 255, 255)),
    ("red_lo", (0, 60, 60),   (10, 255, 255)),
    ("red_hi", (170, 60, 60), (180, 255, 255)),
    ("yellow", (20, 60, 60),  (35, 255, 255)),
    ("cyan",   (85, 60, 60),  (100, 255, 255)),
)


def _detect_polygon_mask(img):
    """Return ``(area_px, mask, hue_name)`` for the most prominent
    saturated colour. Tries common geometry-figure colours and picks
    the largest single contour. Returns ``(0, None, None)`` if no
    candidate exceeds the noise floor.
    """
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    best = (0.0, None, None)
    for name, lo, hi in _POLYGON_HUE_RANGES:
        m = cv2.inRange(hsv, np.array(lo), np.array(hi))
        cnts, _ = cv2.findContours(
            m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )
        if not cnts:
            continue
        biggest = max(cnts, key=cv2.contourArea)
        area = float(cv2.contourArea(biggest))
        if area > best[0]:
            best = (area, m, name)
    return best


def _extract_orthogonal_polygon_structure(
    image_path: str,
) -> tuple[float, str, list[dict]] | None:
    """OpenCV-based orthogonal polygon structure extractor.

    Returns ``(pixel_area, hue_name, edges_template)`` where
    ``edges_template`` is a list of edge descriptor dicts with keys
    ``idx``, ``axis``, ``direction``, ``midpoint_xy``, ``length_px``.
    Returns ``None`` on any failure (cv2/numpy missing, no polygon
    found, non-orthogonal).
    """
    try:
        import cv2
    except ImportError:
        return None
    try:
        img = cv2.imread(image_path)
        if img is None:
            return None
    except Exception:
        return None
    pixel_area, mask, hue_name = _detect_polygon_mask(img)
    if mask is None or pixel_area < 100:
        return None
    # Find the largest contour on the mask
    cnts, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
    )
    if not cnts:
        return None
    contour = max(cnts, key=cv2.contourArea)
    # Approximate to polygon; epsilon tuned for orthogonal shapes
    peri = cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
    pts = [tuple(p[0]) for p in approx]
    if len(pts) < 4:
        return None
    # Build edge descriptors
    edges: list[dict] = []
    n = len(pts)
    for i in range(n):
        p1 = pts[i]
        p2 = pts[(i + 1) % n]
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        if abs(dx) > abs(dy):
            axis = "h"
            direction = 1 if dx > 0 else -1
            length_px = abs(dx)
        else:
            axis = "v"
            direction = 1 if dy > 0 else -1
            length_px = abs(dy)
        mx = (p1[0] + p2[0]) / 2
        my = (p1[1] + p2[1]) / 2
        edges.append({
            "idx": i,
            "axis": axis,
            "direction": direction,
            "midpoint_xy": (mx, my),
            "length_px": float(length_px),
            "label": None,
        })
    return float(pixel_area), str(hue_name), edges


_POLYGON_EDGE_OCR_PROMPT_TEMPLATE = (
    "[Polygon-edge label OCR — narrow task]\n\n"
    "The image shows an orthogonal polygon with numeric side-length "
    "labels. Below is the list of polygon edges extracted by computer "
    "vision, each identified by index, axis (h=horizontal / "
    "v=vertical), direction, and the pixel coordinates of its midpoint.\n\n"
    "{edges_block}\n\n"
    "Your task: for EACH edge listed above, identify the numeric label "
    "in the image that belongs to that edge.\n\n"
    "Output EXACTLY this JSON, no prose, no markdown fence, JSON only:\n"
    "{{\n"
    "  \"assignments\": [\n"
    "    {{\"edge_idx\": <int>, \"label\": <number or null>}}\n"
    "  ]\n"
    "}}\n\n"
    "Rules:\n"
    "  - Every edge in the list above must have exactly one entry in "
    "\"assignments\" (same order, same edge_idx).\n"
    "  - \"label\" is the numeric value of the closest visible label for "
    "that edge. Use null if no label is visible near that edge.\n"
    "  - Each label in the image belongs to exactly ONE edge. If you've "
    "already used a label for one edge, do NOT reuse it for another "
    "edge unless the image clearly shows the same value labelled twice "
    "on different edges.\n"
    "  - **Spatial proximity matters more than visual scanning "
    "order.** Match each edge to the label whose pixel position is "
    "closest to that edge's midpoint, NOT the next label your eye "
    "would land on.\n"
)


def _build_polygon_edges_block(edges: list[dict]) -> str:
    lines = []
    for e in edges:
        mx, my = e["midpoint_xy"]
        if e["axis"] == "h":
            d = "right" if e["direction"] == 1 else "left"
        else:
            d = "down" if e["direction"] == 1 else "up"
        lines.append(
            f"  edge {e['idx']:>2d}: axis={e['axis']} direction={d} "
            f"midpoint_pixel=({mx:.0f}, {my:.0f}) length_px="
            f"{e['length_px']:.0f}"
        )
    return "\n".join(lines)


def _call_sonnet_polygon_edge_ocr(
    image_path: str, edges: list[dict], *, model: str = "claude-sonnet-4-6",
) -> dict | None:
    """One narrow Anthropic vision call: assign numeric labels to polygon
    edges. Returns ``{edge_idx: label}`` dict or ``None`` on failure."""
    try:
        client = _get_anthropic()
    except Exception:
        return None
    ext = os.path.splitext(image_path)[1].lower()
    mime = MIME_MAP.get(ext, "image/png")
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception:
        return None
    edges_block = _build_polygon_edges_block(edges)
    prompt = _POLYGON_EDGE_OCR_PROMPT_TEMPLATE.format(
        edges_block=edges_block,
    )
    request_kwargs: dict[str, object] = {
        "model": model,
        "max_tokens": 1000,
        "timeout": 90.0,
    }
    if not _model_drops_temperature(model):
        request_kwargs["temperature"] = 0.0
    try:
        resp = client.messages.create(
            **request_kwargs,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mime,
                        "data": b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception:
        return None
    raw = resp.content[0].text if resp.content else ""
    obj_str = _extract_json_object(raw)
    if not obj_str:
        return None
    try:
        d = json.loads(obj_str)
    except json.JSONDecodeError:
        return None
    assignments = d.get("assignments")
    if not isinstance(assignments, list):
        return None
    result: dict = {}
    for a in assignments:
        if not isinstance(a, dict):
            continue
        idx = a.get("edge_idx")
        lbl = a.get("label")
        if isinstance(idx, int):
            result[idx] = float(lbl) if lbl is not None else None
    return result or None


def _assign_labels_to_polygon_edges(
    edges: list[dict], ocr_dict: dict,
) -> list[dict]:
    """Return a copy of ``edges`` with ``label`` filled from ``ocr_dict``."""
    out = []
    for e in edges:
        ec = dict(e)
        ec["label"] = ocr_dict.get(e["idx"])
        out.append(ec)
    return out


def _closure_solve_polygon_missing(edges: list[dict]) -> int:
    """Fill missing edge labels using closure constraints.

    For an orthogonal polygon: sum of right-going horizontal edges ==
    sum of left-going; same for vertical. If exactly one edge per axis
    direction is missing, deduce its label.

    Returns the number of labels filled.
    """
    filled = 0
    for axis in ("h", "v"):
        for direction in (1, -1):
            same = [e for e in edges if e["axis"] == axis and e["direction"] == direction]
            opp = [e for e in edges if e["axis"] == axis and e["direction"] != direction]
            opp_sum = sum(e["label"] for e in opp if e["label"] is not None)
            same_known = [e for e in same if e["label"] is not None]
            same_unknown = [e for e in same if e["label"] is None]
            if len(same_unknown) == 1:
                deduced = opp_sum - sum(e["label"] for e in same_known)
                if deduced > 0:
                    same_unknown[0]["label"] = deduced
                    filled += 1
    return filled


def _polygon_closure_repair(
    edges: list[dict],
    label_pool: list[float],
    *,
    max_iters: int = 4,
) -> list[dict]:
    """Iterative closure repair using pool of all candidate labels.

    For edges still missing a label, try each pool value and keep
    whichever minimises closure imbalance. Repeat up to ``max_iters``
    times or until all edges labelled.
    """
    edges = [dict(e) for e in edges]
    for _ in range(max_iters):
        missing = [e for e in edges if e["label"] is None]
        if not missing:
            break
        _closure_solve_polygon_missing(edges)
        still_missing = [e for e in edges if e["label"] is None]
        if not still_missing:
            break
        # Assign from pool by proximity heuristic (length_px match)
        used_labels = {e["label"] for e in edges if e["label"] is not None}
        remaining = [v for v in label_pool if v not in used_labels]
        if not remaining:
            break
        for e in still_missing:
            if not remaining:
                break
            # Pick the pool value closest in magnitude to the edge's length_px
            # (heuristic: label ~ physical edge length in the image units)
            best = min(remaining, key=lambda v: abs(v - e["length_px"] / 50))
            e["label"] = best
            remaining.remove(best)
    return edges


def _collect_polygon_label_pool(
    edges: list[dict],
    ocr_passes: list[dict],
) -> list[float]:
    """Collect all numeric label values seen across OCR passes."""
    pool: list[float] = []
    seen: set[float] = set()
    for ocr_dict in ocr_passes:
        for v in ocr_dict.values():
            if v is not None and v not in seen:
                pool.append(float(v))
                seen.add(float(v))
    return pool


def _polygon_closure_check(edges: list[dict], tol: float = 0.5) -> bool:
    """Return True when labelled edges satisfy closure constraints."""
    for axis in ("h", "v"):
        pos_sum = sum(
            e["label"] for e in edges
            if e["axis"] == axis and e["direction"] == 1
            and e["label"] is not None
        )
        neg_sum = sum(
            e["label"] for e in edges
            if e["axis"] == axis and e["direction"] == -1
            and e["label"] is not None
        )
        if abs(pos_sum - neg_sum) > tol:
            return False
    return True


def _polygon_shoelace_area_unit_space(edges: list[dict]) -> float:
    """Compute polygon area via shoelace formula in unit space.

    Traces the polygon boundary edge-by-edge using the labelled unit
    lengths and cardinal directions. All edges must be labelled.
    Returns the absolute shoelace area.
    """
    x, y = 0.0, 0.0
    vertices = [(x, y)]
    for e in edges:
        label = e["label"] or 0.0
        axis = e["axis"]
        direction = e["direction"]
        if axis == "h":
            x += direction * label
        else:
            y += direction * label
        vertices.append((x, y))
    # Shoelace
    n = len(vertices)
    area = 0.0
    for i in range(n - 1):
        x1, y1 = vertices[i]
        x2, y2 = vertices[i + 1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2


def _per_edge_majority_label(
    ocr_dicts: list[dict], edges: list[dict],
) -> list[dict]:
    """For each edge, pick the modal label across OCR passes.

    Ties go to the smallest value (conservative estimate). Returns
    a copy of ``edges`` with ``label`` set to the majority value or
    ``None`` if no pass assigned a value to that edge.
    """
    from collections import Counter
    out = []
    for e in edges:
        idx = e["idx"]
        labels = [
            d[idx] for d in ocr_dicts
            if idx in d and d[idx] is not None
        ]
        ec = dict(e)
        if labels:
            counts = Counter(labels)
            max_count = max(counts.values())
            candidates = sorted(
                v for v, c in counts.items() if c == max_count
            )
            ec["label"] = candidates[0]
        else:
            ec["label"] = None
        out.append(ec)
    return out


def solve_orthogonal_polygon_via_opencv_hybrid(
    question: str,
    image_path: str,
    *,
    model: str = "claude-sonnet-4-6",
    passes_count: int = 3,
) -> tuple[str, dict]:
    """Hybrid OpenCV + narrow Sonnet OCR + Python shoelace solver.

    Generic for any orthogonal polygon area question with labelled
    side lengths. OpenCV vertex extraction is run once (deterministic).
    The narrow Sonnet OCR call is invoked ``passes_count`` times; per
    edge the modal label across passes is taken (ties go to the
    smallest value), so single-pass OCR mistakes are voted out by the
    majority. Closure constraints fill any remaining missing labels.
    The shoelace area is then computed deterministically in unit
    space.

    Falls back: on any pipeline failure (OpenCV missing, total OCR
    failure, closure broken even after majority vote) returns "" so
    the caller can fall through to the structured-JSON multipass.

    Returns ``(answer, info)``; ``info`` carries audit fields for
    evidence smoke output (stage, n_edges, per-pass OCR records,
    final closure status, winning area).
    """
    info: dict = {"stage": "init"}
    structure = _extract_orthogonal_polygon_structure(image_path)
    if structure is None:
        return "", {**info, "error": "opencv structure extraction fail"}
    pixel_area, hue_name, edges_template = structure
    info.update({
        "stage": "ocr",
        "polygon_pixel_area": pixel_area,
        "polygon_hue": hue_name,
        "n_edges": len(edges_template),
    })
    pass_records: list[dict] = []
    ocr_dicts: list[dict] = []
    for pass_idx in range(max(1, passes_count)):
        ocr_dict = _call_sonnet_polygon_edge_ocr(
            image_path, edges_template, model=model,
        )
        rec: dict = {"pass": pass_idx, "ok": ocr_dict is not None}
        if ocr_dict is not None:
            rec["assignments"] = dict(ocr_dict)
            ocr_dicts.append(ocr_dict)
        pass_records.append(rec)
    info["pass_records"] = pass_records
    info["n_ocr_ok"] = len(ocr_dicts)
    if not ocr_dicts:
        return "", {**info, "error": "all OCR passes failed"}

    info["stage"] = "majority_vote"
    edges = _per_edge_majority_label(ocr_dicts, edges_template)
    label_pool = _collect_polygon_label_pool(edges, ocr_dicts)
    info["label_pool"] = label_pool

    info["stage"] = "closure"
    filled = _closure_solve_polygon_missing(edges)
    info["closure_filled_initial"] = filled

    still_missing = sum(1 for e in edges if e["label"] is None)
    if still_missing:
        edges = _polygon_closure_repair(edges, label_pool)
        info["closure_repair_applied"] = True

    closure_ok = _polygon_closure_check(edges)
    info["closure_ok"] = closure_ok
    if not closure_ok:
        return "", {**info, "error": "closure broken after repair"}

    info["stage"] = "shoelace"
    area_units = _polygon_shoelace_area_unit_space(edges)
    info["shoelace_area"] = area_units
    if area_units <= 0:
        return "", {**info, "error": "shoelace produced non-positive area"}

    info["stage"] = "done"
    answer = _format_polygon_area(area_units)
    info["answer"] = answer
    return answer, info


# ---------------------------------------------------------------------------
# ── Colour-coded numeric: OpenCV colour-isolation + narrow OCR + stats ──────
# ---------------------------------------------------------------------------

_COLOUR_HSV_RANGES: dict[
    str, tuple[tuple[int, int, int], tuple[int, int, int]]
    | tuple[
        tuple[tuple[int, int, int], tuple[int, int, int]],
        tuple[tuple[int, int, int], tuple[int, int, int]],
    ],
] = {
    "red": (
        ((0, 80, 80), (10, 255, 255)),
        ((170, 80, 80), (180, 255, 255)),
    ),
    "green":   ((35, 80, 80), (85, 255, 255)),
    "blue":    ((100, 80, 80), (130, 255, 255)),
    "yellow":  ((20, 80, 80), (35, 255, 255)),
    "cyan":    ((85, 80, 80), (100, 255, 255)),
    "orange":  ((10, 80, 80), (20, 255, 255)),
    "purple":  ((130, 80, 80), (165, 255, 255)),
    "magenta": ((140, 80, 80), (170, 255, 255)),
    "pink":    ((160, 50, 80), (180, 200, 255)),
}


def _isolate_image_colour(
    image_path: str, colour: str,
) -> bytes | None:
    """Return PNG bytes of the image with only the named colour visible
    (other pixels masked to black). ``None`` on failure (cv2/numpy
    missing, image read fail, unknown colour)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    rng = _COLOUR_HSV_RANGES.get(colour.lower())
    if rng is None:
        return None
    try:
        img = cv2.imread(image_path)
        if img is None:
            return None
    except Exception:
        return None
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    # red wraps around; handle two-range case
    if isinstance(rng[0][0], tuple):
        lo1, hi1 = rng[0]  # type: ignore[misc]
        lo2, hi2 = rng[1]  # type: ignore[misc]
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, np.array(lo1), np.array(hi1)),
            cv2.inRange(hsv, np.array(lo2), np.array(hi2)),
        )
    else:
        lo, hi = rng  # type: ignore[misc]
        mask = cv2.inRange(hsv, np.array(lo), np.array(hi))
    result = cv2.bitwise_and(img, img, mask=mask)
    ok, buf = cv2.imencode(".png", result)
    if not ok:
        return None
    return bytes(buf)


_COLOUR_OCR_PROMPT = (
    "[Colour-isolated image OCR — narrow task]\n\n"
    "The image has been pre-processed: only one colour remains visible; "
    "everything else is blacked out. Your task is to read every visible "
    "number in the image — these are the numbers of that colour from the "
    "original image.\n\n"
    "Read every visible number in row-major order (left to right, "
    "top to bottom).\n\n"
    "Output EXACTLY this JSON, no prose, no markdown fence, JSON "
    "only:\n"
    "{\n"
    "  \"numbers\": [<integer or decimal>, ...]\n"
    "}\n\n"
    "Rules:\n"
    "  - Every visible number appears in the list, in row-major order.\n"
    "  - Numbers that have been blacked out are NOT in the list.\n"
    "  - Two adjacent numbers separated by a black gap are TWO "
    "numbers (do NOT concatenate).\n"
    "  - Output is consumed by deterministic Python; precision matters.\n"
)


def _call_sonnet_single_colour_list_ocr(
    image_bytes: bytes, *, model: str = "claude-sonnet-4-6",
) -> list[float] | None:
    """One narrow Anthropic vision call on a colour-isolated image
    asking for a JSON list of numbers in row-major order. Returns the
    list (numbers as floats, integer-valued where possible) or None
    on parse / API failure."""
    try:
        client = _get_anthropic()
    except Exception:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    request_kwargs: dict[str, object] = {
        "model": model,
        "max_tokens": 500,
        "timeout": 90.0,
    }
    if not _model_drops_temperature(model):
        request_kwargs["temperature"] = 0.0
    try:
        resp = client.messages.create(
            **request_kwargs,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": b64,
                    }},
                    {"type": "text", "text": _COLOUR_OCR_PROMPT},
                ],
            }],
        )
    except Exception:
        return None
    raw = resp.content[0].text if resp.content else ""
    obj_str = _extract_json_object(raw)
    if not obj_str:
        return None
    try:
        d = json.loads(obj_str)
    except json.JSONDecodeError:
        return None
    nums = d.get("numbers")
    if not isinstance(nums, list):
        return None
    out: list[float] = []
    for v in nums:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out or None


def _per_position_majority_numbers(
    passes: list[list[float]],
) -> list[float]:
    """Aggregate N OCR passes by position → modal value per position.

    Pads shorter passes with ``None`` so position indices align.
    Missing (None) values are excluded from the mode vote. If all
    passes agree, the unique value is returned. On a tie the smallest
    value wins (conservative estimate).
    """
    if not passes:
        return []
    max_len = max(len(p) for p in passes)
    padded: list[list[float | None]] = [
        list(p) + [None] * (max_len - len(p)) for p in passes
    ]
    from collections import Counter
    result: list[float] = []
    for pos in range(max_len):
        col = [row[pos] for row in padded if row[pos] is not None]
        if not col:
            continue
        counts = Counter(col)
        max_count = max(counts.values())
        candidates = sorted(v for v, c in counts.items() if c == max_count)
        result.append(candidates[0])
    return result


def _detect_colours_in_question(question: str) -> list[str]:
    """Return the list of colour names mentioned in the question, in
    order of first occurrence, deduplicated."""
    seen: list[str] = []
    for m in _COLOUR_NAME_RE.findall(question):
        c = m.lower()
        if c in _COLOUR_HSV_RANGES and c not in seen:
            seen.append(c)
    return seen


_STATS_PLAN_PROMPT = (
    "[Statistics operation planner — narrow task]\n\n"
    "Given the question below and clean numeric data lists already "
    "extracted from the image, output a JSON operation plan that "
    "Python will execute against the data using the ``statistics`` "
    "module (Python 3.11+).\n\n"
    "Output EXACTLY this JSON, no prose, no markdown fence, JSON "
    "only:\n"
    "{\n"
    "  \"intermediate\": [\n"
    "    {\"name\": \"<id>\", \"fn\": \"<statistics fn name>\", "
    "\"input\": \"<colour list name>\"}\n"
    "  ],\n"
    "  \"final\": {\"fn\": \"<statistics fn name>\", "
    "\"input\": [\"<intermediate id>\", ...]},\n"
    "  \"round_decimals\": <int N or null>\n"
    "}\n\n"
    "Rules:\n"
    "  - ``fn`` must be one of: ``pstdev``, ``stdev``, ``pvariance``, "
    "``variance``, ``mean``, ``median``, ``mode``, ``geometric_mean``, "
    "``harmonic_mean``, ``fmean``.\n"
    "  - The keyword \"standard population deviation\" → ``pstdev``; "
    "\"standard sample deviation\" or just \"standard deviation\" → "
    "``stdev``.\n"
    "  - The keyword \"average\" / \"mean\" → ``mean``.\n"
    "  - ``intermediate`` may be empty if the final operation is "
    "computed directly on a colour list.\n"
    "  - ``round_decimals`` is the number from the question's "
    "rounding instruction (e.g. \"rounded to the nearest three "
    "decimal points\" → 3); ``null`` if no rounding requested.\n"
    "  - The output is consumed by deterministic Python; precision "
    "matters.\n"
)


_STATS_FN_WHITELIST = {
    "pstdev", "stdev", "pvariance", "variance",
    "mean", "median", "mode",
    "geometric_mean", "harmonic_mean", "fmean",
}


def _execute_statistics_plan(
    plan: dict, data: dict[str, list[float]],
) -> tuple[str, dict]:
    """Run a parsed operation plan against the extracted data lists.

    Returns ``(formatted_answer, info)``. ``info`` carries intermediate
    values for audit. Empty answer string on validation failure or
    statistics module error.
    """
    import statistics as _stats
    info: dict = {}
    intermediate = plan.get("intermediate") or []
    final = plan.get("final")
    round_decimals = plan.get("round_decimals")
    namespace: dict[str, list[float]] = dict(data)
    for step in intermediate:
        fn_name = step.get("fn")
        inp_name = step.get("input")
        out_name = step.get("name")
        if fn_name not in _STATS_FN_WHITELIST:
            return "", {**info, "error": f"fn not in whitelist: {fn_name}"}
        inp_data = namespace.get(inp_name)
        if not isinstance(inp_data, list) or not inp_data:
            return "", {**info, "error": f"input not found: {inp_name}"}
        fn = getattr(_stats, fn_name, None)
        if fn is None:
            return "", {**info, "error": f"statistics.{fn_name} not found"}
        try:
            val = fn(inp_data)
        except Exception as err:
            return "", {**info, "error": f"statistics.{fn_name}: {err}"}
        info[out_name] = val
        namespace[out_name] = [val]
    if not isinstance(final, dict):
        return "", {**info, "error": "no final step"}
    fn_name = final.get("fn")
    inp_names = final.get("input") or []
    if fn_name not in _STATS_FN_WHITELIST:
        return "", {**info, "error": f"final fn not in whitelist: {fn_name}"}
    if isinstance(inp_names, str):
        inp_names = [inp_names]
    combined: list[float] = []
    for n in inp_names:
        vals = namespace.get(n)
        if vals is None:
            return "", {**info, "error": f"final input not found: {n}"}
        combined.extend(vals if isinstance(vals, list) else [vals])
    if not combined:
        return "", {**info, "error": "final input list empty"}
    fn = getattr(_stats, fn_name, None)
    if fn is None:
        return "", {**info, "error": f"statistics.{fn_name} not found"}
    try:
        result = fn(combined)
    except Exception as err:
        return "", {**info, "error": f"final statistics.{fn_name}: {err}"}
    info["final_raw"] = result
    if round_decimals is not None:
        try:
            result = round(result, int(round_decimals))
        except (TypeError, ValueError):
            pass
    # Format: strip trailing zeros for clean answer
    formatted = f"{result:.10f}".rstrip("0").rstrip(".")
    return formatted, info


def _compute_via_statistics_plan(
    question: str, data: dict[str, list[float]],
    *, model: str = "claude-sonnet-4-6",
) -> tuple[str, dict]:
    """Ask Sonnet for an operation plan JSON, then execute via Python
    ``statistics`` module deterministically. Returns
    ``(answer, info)``. Empty answer on plan parse / execute failure.
    """
    info: dict = {"compute_step": "plan_request"}
    try:
        client = _get_anthropic()
    except Exception:
        return "", {**info, "error": "anthropic client init fail"}
    data_lines = []
    for colour, lst in data.items():
        rendered = [int(x) if x == int(x) else x for x in lst]
        data_lines.append(f"  {colour} = {rendered}")
    plan_prompt = (
        f"{_STATS_PLAN_PROMPT}\n"
        f"Question: {question}\n\n"
        "Extracted data lists:\n"
        + "\n".join(data_lines)
    )
    request_kwargs: dict[str, object] = {
        "model": model,
        "max_tokens": 1500,
        "timeout": 120.0,
    }
    if not _model_drops_temperature(model):
        request_kwargs["temperature"] = 0.0
    try:
        resp = client.messages.create(
            **request_kwargs,
            messages=[{
                "role": "user",
                "content": [{"type": "text", "text": plan_prompt}],
            }],
        )
    except Exception as err:
        return "", {**info, "error": f"plan call: {err}"}
    raw = resp.content[0].text if resp.content else ""
    info["plan_raw_excerpt"] = raw[:600]
    obj_str = _extract_json_object(raw)
    if obj_str is None:
        return "", {**info, "error": "plan json parse fail"}
    try:
        plan = json.loads(obj_str)
    except json.JSONDecodeError as je:
        return "", {**info, "error": f"plan json decode: {je}"}
    info["compute_step"] = "plan_executed"
    answer, exec_info = _execute_statistics_plan(plan, data)
    info.update(exec_info)
    return answer, info


def solve_colour_coded_numeric_via_hybrid(
    question: str,
    image_path: str,
    *,
    model: str = "claude-sonnet-4-6",
    passes_count: int = 3,
) -> tuple[str, dict]:
    """Hybrid OpenCV colour-isolation + narrow Sonnet OCR + Sonnet
    arithmetic-from-clean-data solver.

    Steps:
      1. Detect colour names in the question (≥2 required).
      2. For each colour, mask the image with HSV → black-out everything
         else → run N narrow-OCR passes → per-position majority.
      3. Build a clean text-only follow-up prompt: original question +
         the extracted lists labelled by colour. Ask Sonnet for
         FINAL ANSWER on this text-only payload (no vision burden,
         arithmetic-only). Sonnet uses its own internal calculator /
         python understanding to compute the answer specified by the
         question (mean / pstdev / stdev / sum / etc.).

    Returns ``(answer, info)`` where ``answer`` is "" on any pipeline
    failure (caller can fall through to legacy multipass / local Gemma).
    """
    info: dict = {"stage": "init"}
    colours = _detect_colours_in_question(question)
    if len(colours) < 2:
        return "", {**info, "error": "fewer than 2 colour names detected"}
    info["colours"] = colours
    info["stage"] = "ocr"
    extracted: dict[str, list[float]] = {}
    per_colour_records: dict[str, dict] = {}
    for colour in colours:
        img_bytes = _isolate_image_colour(image_path, colour)
        rec: dict = {"colour": colour}
        if img_bytes is None:
            rec["error"] = "isolate fail (cv2/numpy missing or read fail)"
            per_colour_records[colour] = rec
            continue
        passes: list[list[float]] = []
        for _ in range(max(1, passes_count)):
            nums = _call_sonnet_single_colour_list_ocr(img_bytes, model=model)
            if nums:
                passes.append(nums)
        rec["n_passes_ok"] = len(passes)
        if not passes:
            rec["error"] = "all OCR passes failed"
            per_colour_records[colour] = rec
            continue
        voted = _per_position_majority_numbers(passes)
        rec["voted"] = voted
        per_colour_records[colour] = rec
        if voted:
            extracted[colour] = voted
    info["per_colour"] = per_colour_records
    if len(extracted) < 2:
        return "", {**info, "error": f"extracted only {len(extracted)} colour(s), need ≥2"}
    info["extracted"] = extracted
    info["stage"] = "compute"
    answer, plan_info = _compute_via_statistics_plan(
        question, extracted, model=model,
    )
    info.update(plan_info)
    if not answer:
        return "", info
    info["stage"] = "done"
    return answer, info


# ---------------------------------------------------------------------------
# ── Image-quiz scoring: OCR + deterministic judge + arithmetic plan ─────────
# ---------------------------------------------------------------------------

_QUIZ_TYPE_TAGS = (
    "add_subtract_fractions",
    "multiply_divide_fractions",
    "form_improper_fraction",
    "form_mixed_number",
)

_QUIZ_OCR_PROMPT = (
    "[Image-quiz OCR — narrow extraction]\n\n"
    "This image is a graded quiz with numbered problems. For EACH "
    "numbered problem visible in the image, output one entry in a JSON "
    "array. You are the OCR + classifier layer ONLY — Python "
    "downstream computes correctness, do NOT judge correctness.\n\n"
    "For each problem, output:\n"
    "  - idx: 1-based problem number visible in image\n"
    "  - problem_type: pick exactly one of:\n"
    "    * \"add_subtract_fractions\"  — two fractions joined by + or - "
    "(e.g. a/b + c/d  or  a/b - c/d)\n"
    "    * \"multiply_divide_fractions\"  — two fractions joined by × or ÷ "
    "(e.g. a/b × c/d  or  a/b ÷ c/d)\n"
    "    * \"form_improper_fraction\"  — phrasing 'turn X y/z into an "
    "improper fraction' (input is a MIXED number, output should be "
    "improper)\n"
    "    * \"form_mixed_number\"  — phrasing 'turn p/q into a mixed "
    "number' (input is an IMPROPER fraction, output should be mixed)\n"
    "  - For ARITHMETIC types (add_subtract / multiply_divide) "
    "additionally output:\n"
    "    * operands: list of TWO strings, each a fraction in 'a/b' form, "
    "exactly as written in the image (preserve sign)\n"
    "    * operator: one of \"+\", \"-\", \"*\", \"/\"  (use * for ×, / for ÷)\n"
    "  - For CONVERSION types (form_improper / form_mixed) additionally "
    "output:\n"
    "    * input_value: the value being converted, exactly as written in "
    "the image. Mixed-number form is 'W n/d' (whole space "
    "numerator-slash-denominator). Improper form is 'a/b'.\n"
    "  - student_answer: a string copied EXACTLY from the answer field "
    "as shown in the image. Preserve sign, spaces, and slash. Mixed-"
    "number form 'W n/d' or improper 'a/b'.\n\n"
    "Output EXACTLY this JSON shape, no prose, no markdown fence:\n"
    "{\n"
    "  \"problems\": [\n"
    "    {\"idx\": <int>, \"problem_type\": \"<...>\", "
    "\"operands\": [\"a/b\", \"c/d\"], \"operator\": \"+\", "
    "\"input_value\": null, \"student_answer\": \"<string>\"}\n"
    "  ]\n"
    "}\n\n"
    "For fields not applicable to a problem type, use null.\n"
    "Output is consumed by deterministic Python; OCR precision matters."
)


def _call_sonnet_quiz_extract(
    image_bytes: bytes, model: str = "claude-sonnet-4-6",
) -> list[dict] | None:
    """Send the quiz image to Sonnet, parse JSON, return the per-
    problem OCR rows or ``None`` on parse failure."""
    try:
        client = _get_anthropic()
    except Exception:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    request_kwargs: dict[str, object] = {
        "model": model,
        "max_tokens": 2000,
        "timeout": 120.0,
    }
    if not _model_drops_temperature(model):
        request_kwargs["temperature"] = 0.0
    try:
        resp = client.messages.create(
            **request_kwargs,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": b64,
                    }},
                    {"type": "text", "text": _QUIZ_OCR_PROMPT},
                ],
            }],
        )
    except Exception:
        return None
    raw = resp.content[0].text if resp.content else ""
    obj_str = _extract_json_object(raw)
    if not obj_str:
        return None
    try:
        d = json.loads(obj_str)
    except json.JSONDecodeError:
        return None
    probs = d.get("problems")
    if not isinstance(probs, list):
        return None
    out: list[dict] = []
    for p in probs:
        if not isinstance(p, dict):
            continue
        idx = p.get("idx")
        ptype = p.get("problem_type")
        if not isinstance(idx, int) or not isinstance(ptype, str):
            continue
        out.append({
            "idx": idx,
            "problem_type": ptype,
            "operands": p.get("operands"),
            "operator": p.get("operator"),
            "input_value": p.get("input_value"),
            "student_answer": p.get("student_answer"),
        })
    return out or None


def _per_problem_majority_vote_quiz(
    passes: list[list[dict]],
) -> list[dict]:
    """Aggregate N OCR passes by problem ``idx`` → modal full-row tuple.
    Robust to one or two passes hallucinating a wrong field; majority
    over (type, operands, operator, input_value, student_answer)."""
    from collections import Counter as _Counter
    by_idx: dict[int, list[tuple]] = {}
    for p in passes:
        for entry in p:
            key = (
                entry["problem_type"],
                tuple(entry.get("operands") or ()),
                entry.get("operator"),
                entry.get("input_value"),
                entry.get("student_answer"),
            )
            by_idx.setdefault(entry["idx"], []).append(key)
    voted: list[dict] = []
    for idx in sorted(by_idx):
        c = _Counter(by_idx[idx])
        key, _ = c.most_common(1)[0]
        ptype, ops, oper, inp, sans = key
        voted.append({
            "idx": idx,
            "problem_type": ptype,
            "operands": list(ops) if ops else None,
            "operator": oper,
            "input_value": inp,
            "student_answer": sans,
        })
    return voted


def _parse_simple_fraction_str(s: str | None):
    """Parse 'a/b' or '-a/b' or pure integer string → ``Fraction``.
    Raises :class:`ValueError` on unparseable input."""
    from fractions import Fraction as _F
    s = (s or "").strip().replace(" ", "")
    if not s:
        raise ValueError("empty fraction")
    if "/" in s:
        a, b = s.split("/", 1)
        return _F(int(a), int(b))
    return _F(int(s))


def _parse_mixed_or_improper_str(s: str | None):
    """Parse mixed 'W n/d' OR improper 'a/b' OR integer → ``Fraction``."""
    from fractions import Fraction as _F
    s = (s or "").strip()
    if not s:
        raise ValueError("empty")
    m = _QUIZ_MIXED_RE.match(s)
    if m:
        whole = int(m.group(1))
        num = int(m.group(2))
        den = int(m.group(3))
        sign = -1 if whole < 0 else 1
        mag = abs(whole) + _F(num, den)
        return sign * mag
    return _parse_simple_fraction_str(s)


def _judge_quiz_problem_correct(entry: dict) -> bool | None:
    """Determine whether the student's answer is correct given the
    OCR-extracted problem fields. Deterministic via ``fractions``.
    Returns ``True``/``False``/``None`` (None = unparseable; caller
    treats as wrong for safety)."""
    ptype = entry.get("problem_type")
    student = entry.get("student_answer")
    if not student:
        return None
    try:
        if ptype == "add_subtract_fractions":
            ops = entry.get("operands") or []
            oper = entry.get("operator")
            if len(ops) != 2 or oper not in ("+", "-"):
                return None
            a = _parse_simple_fraction_str(ops[0])
            b = _parse_simple_fraction_str(ops[1])
            correct = a + b if oper == "+" else a - b
            return correct == _parse_simple_fraction_str(student)
        if ptype == "multiply_divide_fractions":
            ops = entry.get("operands") or []
            oper = entry.get("operator")
            if len(ops) != 2 or oper not in ("*", "/"):
                return None
            a = _parse_simple_fraction_str(ops[0])
            b = _parse_simple_fraction_str(ops[1])
            if oper == "/" and b == 0:
                return None
            correct = a * b if oper == "*" else a / b
            return correct == _parse_simple_fraction_str(student)
        if ptype == "form_improper_fraction":
            inp = entry.get("input_value")
            if not inp:
                return None
            return (
                _parse_mixed_or_improper_str(inp)
                == _parse_simple_fraction_str(student)
            )
        if ptype == "form_mixed_number":
            inp = entry.get("input_value")
            if not inp:
                return None
            return (
                _parse_simple_fraction_str(inp)
                == _parse_mixed_or_improper_str(student)
            )
    except (ValueError, ZeroDivisionError):
        return None
    return None


def _classify_quiz_rule_phrase(phrase: str) -> str | None:
    """Map a scoring-rule phrase to one of ``_QUIZ_TYPE_TAGS``, or
    ``None`` if nothing matches."""
    p = phrase.lower()
    if "improper" in p:
        return "form_improper_fraction"
    if "mixed" in p:
        return "form_mixed_number"
    if any(k in p for k in ("multipl", "divid")):
        return "multiply_divide_fractions"
    if any(k in p for k in ("add", "subtract", "sum", "differ")):
        return "add_subtract_fractions"
    return None


def _parse_quiz_scoring_rules(question: str) -> tuple[dict[str, int], int]:
    """Extract the per-type point map + bonus from the question."""
    type_points: dict[str, int] = {}
    for phrase, pts in _QUIZ_SCORE_RULE_RE.findall(question):
        try:
            n = int(pts)
        except ValueError:
            continue
        tag = _classify_quiz_rule_phrase(phrase)
        if tag and tag not in type_points:
            type_points[tag] = n
    bonus = 0
    bm = _QUIZ_BONUS_RE.search(question)
    if bm:
        try:
            bonus = int(bm.group(1))
        except ValueError:
            bonus = 0
    return type_points, bonus


def _compute_quiz_via_arithmetic_plan(
    voted: list[dict], type_points: dict[str, int], bonus: int,
) -> tuple[str, dict]:
    """Map voted problems → awarded points list → sum + bonus via
    :func:`concinno.tools.builtin.compute.execute_arithmetic_plan`.
    Returns ``(formatted_answer, info)``."""
    try:
        from concinno.tools.builtin.compute import (
            ComputePlanError,
            execute_arithmetic_plan,
        )
    except Exception as err:
        return "", {"error": f"compute import: {err}"}
    awarded = [
        type_points.get(p["problem_type"], 0)
        if p.get("student_correct")
        else 0
        for p in voted
    ]
    plan = {
        "steps": [
            {
                "name": "subtotal",
                "op": "sum_list",
                "args": ["awarded_points"],
            },
            {
                "name": "total",
                "op": "add",
                "args": ["subtotal", float(bonus)],
            },
        ],
        "final": "total",
        "round_decimals": 0,
    }
    try:
        result = execute_arithmetic_plan(
            plan, {"awarded_points": awarded},
        )
    except ComputePlanError as err:
        return "", {
            "error": f"arithmetic_plan: {err}",
            "awarded_points": awarded,
        }
    info = {
        "awarded_points": awarded,
        "bonus": bonus,
        "raw_result": result.get("raw_result"),
        "plan": plan,
    }
    return result.get("answer", ""), info


def solve_image_quiz_scoring_via_hybrid(
    question: str,
    image_path: str,
    *,
    model: str = "claude-sonnet-4-6",
    passes_count: int = 3,
) -> tuple[str, dict]:
    """Hybrid OCR + classifier (Sonnet) + deterministic correctness +
    arithmetic-plan compute (Python) solver for image-quiz scoring
    questions. Returns ``(answer, info)``; empty answer on any
    pipeline failure (caller falls through to legacy)."""
    info: dict = {"stage": "init"}
    type_points, bonus = _parse_quiz_scoring_rules(question)
    if not type_points:
        return "", {**info, "error": "no scoring rules parsed"}
    info["type_points"] = type_points
    info["bonus"] = bonus

    info["stage"] = "ocr"
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except Exception as err:
        return "", {**info, "error": f"image read: {err}"}
    passes: list[list[dict]] = []
    for _ in range(max(1, passes_count)):
        rows = _call_sonnet_quiz_extract(image_bytes, model=model)
        if rows:
            passes.append(rows)
    info["passes_n"] = len(passes)
    if not passes:
        return "", {**info, "error": "all OCR passes failed"}

    voted = _per_problem_majority_vote_quiz(passes)
    if not voted:
        return "", {**info, "error": "majority vote produced empty list"}
    for v in voted:
        v["student_correct"] = bool(_judge_quiz_problem_correct(v))
    info["voted"] = voted
    info["n_problems"] = len(voted)
    info["n_correct"] = sum(1 for v in voted if v["student_correct"])

    info["stage"] = "compute"
    answer, compute_info = _compute_quiz_via_arithmetic_plan(
        voted, type_points, bonus,
    )
    info.update(compute_info)
    if not answer:
        return "", info
    info["stage"] = "done"
    return answer, info
