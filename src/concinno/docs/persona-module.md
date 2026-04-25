# `concinno.persona` — generic agent persona harness

> Status: Track 1 (in-process backend ships in 2.37.0+). Track 2 (HTTP
> backend) and Track 3 (local fine-tuned model) are reserved on the
> public API surface and raise ``NotImplementedError`` until shipped.

`concinno.persona` normalises the "agent reads a persona file ->
generates a reply -> updates persona state" pattern into a
reusable, dependency-light Python module. It gives third-party
developers a single SDK call site they can swap between providers
(Anthropic / OpenAI / local) and, in future releases, between
backends (in-process / hosted endpoint / local fine-tuned model).

## Quickstart

```bash
pip install concinno
```

```python
from concinno.persona import Persona, InProcessBackend

p = Persona.load("alice.md", backend=InProcessBackend(provider="anthropic"))
reply = p.chat("hi, my name is Bob")
p.consolidate()                                   # checkpoint marker
p.pin_memory("user is Bob", reason="first intro") # anchor identity
hits = p.recall("Bob", top_k=3)                   # priority-ordered
p.save("alice.jsonl")                             # JSONL state log
```

## Persona file format

A persona file is plain Markdown with a YAML frontmatter block. The
loader parses both with PyYAML when available and falls back to a
small built-in parser otherwise (no PyYAML dependency required).

```yaml
---
name: alice
personality: friendly, curious, occasionally sarcastic
voice: casual, uses emoji sparingly
memory_seed:
  - "born in Tokyo 1995"
  - "loves jazz piano"
pinned_memories:
  - content: "user's name is Bob"
    pinned_at: "2026-04-25T10:00:00Z"
    reason: "first introduction"
emotional_state:
  default: neutral
  intensity: 0.5
  decay_rate: 0.95
---

# Free-form persona description (optional)
Alice grew up in...
```

The `PersonaSchema` Pydantic model uses
`model_config = ConfigDict(extra="forbid")`, so unknown frontmatter
fields raise at validation time. This is by design — keep the
schema surface small and reviewable.

## Public API

### `Persona`

| Method | Returns | Notes |
|---|---|---|
| `Persona.load(path, state=None, backend=None)` | `Persona` | Load from MD; optionally attach a JSONL state log. |
| `p.chat(message, recall_top_k=3, ...)` | `str` | Run one turn through the backend. Records the turn unless `record=False`. |
| `p.consolidate(turn=None)` | `str` | Append a checkpoint marker (pinned facts always survive). |
| `p.pin_memory(content, reason=None)` | `None` | Add a pinned memory + log it. Idempotent. |
| `p.unpin_memory(content)` | `bool` | Remove a pin + log it. |
| `p.pinned()` | `list[PinnedMemory]` | Current pinned set. |
| `p.recall(query, top_k=3)` | `list[RAGHit]` | Pinned matches first, then BM25-ish history hits. |
| `p.save(path)` | `None` | Persist the JSONL state log. |
| `p.use_endpoint(url, api_key="")` | `None` | Swap to Track 2 backend (raises until shipped). |
| `p.use_local_model(model_id)` | `None` | Swap to Track 3 backend (raises until shipped). |
| `p.decay_emotion()` | `EmotionalState` | Apply one decay step. |

### Backends

```python
from concinno.persona import (
    InProcessBackend, HTTPBackend, LocalModelBackend, PersonaBackend,
)

# Track 1 — ships now.
b = InProcessBackend(provider="anthropic", model="claude-haiku-4-5")
b = InProcessBackend(provider="openai",    model="gpt-4o-mini")
b = InProcessBackend(provider="echo")  # deterministic, offline-safe

# Track 2 stub — raises until shipped.
HTTPBackend("https://example.com/v1/persona/alice/turn", api_key="...")

# Track 3 stub — raises until shipped.
LocalModelBackend("aiking/concinno-persona-8b-v1")
```

`PersonaBackend` is an abstract base class with a single
required method:

```python
class PersonaBackend(ABC):
    @abstractmethod
    def chat(self, system_prompt, history, user, *, max_tokens, temperature) -> str:
        """Return assistant text. Empty string on failure (never raise)."""
```

This contract — fail-soft instead of raising on transport errors —
mirrors `concinno.llm_runtime.LLMBackend`. Bring-your-own-backend
implementations should follow the same convention so consumer
retry logic stays uniform.

## State log format

State is appended to a single JSONL file, one record per line:

```json
{"ts": "2026-04-25T10:00:00Z", "kind": "turn",        "user": "hi",         "assistant": "hello"}
{"ts": "2026-04-25T10:00:01Z", "kind": "pin",         "state_delta": {"content": "user is Bob", "reason": "intro"}}
{"ts": "2026-04-25T10:00:02Z", "kind": "consolidate", "state_delta": {"summary": "checkpoint: 1 turns"}}
```

`Persona.load(persona_path, state=state_path)` replays this log to
rebuild the in-memory pin store and the BM25 history index.
Corrupt lines are tolerated (skipped) so a single bad write
cannot lock the persona out of its own history.

## Pinned-memory mechanism

Pinned memories are the module's anti-drift primitive:

1. **User or agent explicitly pins.** No automatic detection.
2. **Consolidation skips pins.** Even after summarisation, pinned
   facts remain in the system prompt verbatim.
3. **Recall returns pins first.** `Persona.recall` always merges
   pinned matches at the top of the result list before any general
   BM25 hit.

This is a deliberately simple rule-based primitive (explicit pin
+ skip + priority). It is **not** related to any algorithmic
peak-detection / tension-tracking system.

## CLI

```bash
# One-shot chat (echo backend = no LLM credentials required)
concinno persona run --persona ./alice.md --message "hello"

# With state log and a real provider
concinno persona run --persona ./alice.md --state ./alice.jsonl \
  --provider anthropic --model claude-haiku-4-5 --message "hello"

# Pin / list / recall
concinno persona pin     --state ./alice.jsonl --content "user is Bob" --reason "intro"
concinno persona pinned  --state ./alice.jsonl
concinno persona recall  --state ./alice.jsonl --query "Bob" --top-k 3
```

`--format json` is available on `pinned` and `recall` for
machine-readable output.

## Backend upgrade path

Track 1 is the entry-level primitive. Track 2 and Track 3 land
later as drop-in backends, swapping inference paths without
changing call sites:

```python
p = Persona.load("alice.md")          # Track 1, in-process
p.use_endpoint("https://...", "...")  # Track 2, hosted (future)
p.use_local_model("aiking/...")       # Track 3, local model (future)
```

Track 2 and Track 3 ship in separate Concinno releases; consult
the release notes when they land.

## Limitations (Track 1)

- The bundled `PersonaRAG` is a small in-memory BM25-ish scorer.
  Conversations beyond a few thousand turns will benefit from a
  consumer-supplied vector index. Subclass `PersonaRAG` and override
  `add` / `search` to plug in your own backend.
- Consolidation in Track 1 only emits a marker. It does **not**
  actively summarise the conversation. Heavy summarisation is left
  to the consumer (or to Track 2's server-side path).
- Pinned-memory recall is naive substring + token overlap. A
  consumer wanting semantic recall over pins should subclass
  `PinnedMemoryStore`.

## See also

- `src/concinno/persona/__init__.py` — public API surface
- `tests/persona/test_e2e.py` — end-to-end smoke test of the full loop
- `tests/persona/test_ip_safe_naming.py` — naming policy CI gate
