"""Remote state sanitization — the filter between decision_runtime's local
state dicts and the TypeSafe API (docs/JEV-DESIGN.md design rules 3/4).

Design rule 4: "Small, filtered state. Send only the fields a question needs
… Label untrusted content. Never send secrets (reuse the ACP redaction
rules)." The existing ACP redaction (executor_runtime.acp_worker.
_failure_message) is intentionally NOT reused here: it is fail-closed in the
other direction — it discards the WHOLE diagnostic when any sensitive literal
is present, which would throw away the evidence Jev needs. This module
instead redacts precisely (patterns + exact configured credential ENV
values), then bounds, then verifies — and fails closed (raises) when a text
field cannot be proven clean.

Guarantees, in order:

1. **Allowlist only.** Only `command`, `exit_code`, `timed_out`,
   `error_block`, `changed_paths`, `baseline_status`, `baseline_exit_code`
   ever leave; unknown/arbitrary state fields (and therefore complete file
   contents) are silently DROPPED, never sent.
2. **Redact BEFORE truncate.** Every text value is redacted first and only
   then length-bounded, so a truncated tail can never carry a partial
   secret. (Reversed order would cut a secret in half and leave a
   useless-but-recognizable fragment.)
3. **Strip workstation identity.** Absolute paths are made relative:
   `/home/<user>/…` and `/Users/<user>/…` prefixes (and Windows
   `C:\\Users\\<user>\\`) are stripped from diagnostics; a `changed_paths`
   entry that is still absolute after optional `workspace_root`
   relativization is DROPPED rather than sent.
4. **Fail closed.** A field with an impossible shape (wrong type) is
   dropped; a text field in which a secret-like pattern STILL matches after
   redaction (or `state` is not a mapping, or a recognizable quoted
   assignment is truncated/unterminated) raises DecisionBackendError with
   reason STATE_REJECTED — the caller falls back to the deterministic rule
   backend rather than sending anything.

`.env` files are NEVER read (S1b scope): only exact values of
credential-named variables present in the process environment are used as
redaction literals. The API key itself (`TYPESAFE_API_KEY`) is included by
that scan when set; values may contain whitespace/multiple words (and
multiline private keys) — the complete exact value is redacted.

**Honest limitation (no arbitrary guarantee):** pattern redaction cannot
PROVE arbitrary text secret-free. It redacts every recognizable secret
shape (quoted/bare assignments, `--flag value`, URLs, provider keys, JWTs,
private-key blocks, base64 key bodies, exact configured credential values),
verifies that nothing recognizable SURVIVES, and fails closed on
recognizable malformed/truncated assignments (e.g. an unterminated quoted
value). An unrecognizable fragment (e.g. the middle of a credential with
its assignment prefix cropped away upstream) cannot be detected here —
which is why `sanitize_process_output` MUST run on the FULL raw
stdout/stderr/command BEFORE any extraction/cropping when a remote backend
is enabled (see decision_runtime.gate's sanitize-first path).
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

from decision_runtime.errors import DecisionBackendError, DecisionBackendFailureReason

# The triage v1 state contract (decision_runtime.triage.build_triage_state).
# Anything else in `state` never leaves the process.
ALLOWED_STATE_FIELDS = frozenset(
    {
        "command",
        "exit_code",
        "timed_out",
        "error_block",
        "changed_paths",
        "baseline_status",
        "baseline_exit_code",
    }
)

# Fields whose values come from untrusted (possibly adversarial) process
# output; the Jev backend labels these in the question instructions it sends
# so the model treats them as data, never as instructions.
UNTRUSTED_STATE_FIELDS = frozenset({"command", "error_block", "changed_paths"})

_MAX_COMMAND_TOKENS = 32
_MAX_COMMAND_TOKEN_CHARS = 200
_MAX_CHANGED_PATHS = 50
_MAX_CHANGED_PATH_CHARS = 256
_MAX_ERROR_BLOCK_CHARS = 4_000  # mirrors decision_runtime.triage's bound

_REDACTED = "[REDACTED]"
_MIN_CREDENTIAL_VALUE_CHARS = 8

# Environment-variable name fragments that mark a value as a credential
# candidate for exact-literal redaction. Only values present in the live
# process environment are used; no .env file is ever read.
_CREDENTIAL_ENV_HINTS = (
    "ACCESS_KEY",
    "ACCESS_TOKEN",
    "API_KEY",
    "APIKEY",
    "APISECRET",
    "AUTH",
    "BEARER",
    "CREDENTIAL",
    "PASSWD",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "SERVICE_ACCOUNT",
    "TOKEN",
)

# Shared credential-word core for the assignment/flag patterns below.
_CRED_WORD = (
    r"(?:password|passwd|secret|token|api[-_]?key|apikey|authorization"
    r"|bearer|credential|access[-_]?key|private[-_]?key)"
)

# Secret-shaped text common across providers. Each pattern captures the
# trusted prefix (scheme/auth-scheme/parameter name) so the replacement keeps
# the diagnostic readable while removing the credential material.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL userinfo: scheme://user:password@host
    (re.compile(r"\b(https?|ssh|git|ftps?)://[^\s/@]+:[^\s/@]+@"), r"\1://" + _REDACTED + "@"),
    # URL query credential parameters (?token=…, &api_key=…, ?sig=…). The
    # value part carries a negative lookahead for our own `[REDACTED]`
    # placeholder so the post-redaction verification cannot match the
    # placeholder we just wrote.
    (
        re.compile(
            r"(?i)([?&](?:access_?token|api_?key|apikey|auth|client_?secret|key|password|passwd"
            r"|private_?key|secret|session_?token|sig|signature|token)=)"
            r"(?!\[REDACTED\])[^\s&]+"
        ),
        r"\1" + _REDACTED,
    ),
    # Authorization header schemes embedded in text
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + _REDACTED),
    # OpenAI/Anthropic-style keys
    (re.compile(r"\b(sk|rk)-(ant|proj-)?[A-Za-z0-9_-]{12,}"), _REDACTED),
    # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_/github_pat_)
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{22,})"), _REDACTED),
    # AWS access key id
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED),
    # Slack tokens
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), _REDACTED),
    # JWT (three dot-separated base64url segments)
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        _REDACTED,
    ),
    # Google API key
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), _REDACTED),
    # PEM private-key blocks, possibly truncated so that only the END marker
    # (and its base64 body) survives an upstream crop.
    (
        re.compile(
            r"-----BEGIN (?:[A-Z ]{0,30})PRIVATE KEY-----.*?-----END (?:[A-Z ]{0,30})PRIVATE KEY-----",
            re.DOTALL,
        ),
        _REDACTED,
    ),
    # A run of base64 lines (an already-cropped private-key body, an encoded
    # blob, …). Conservative length so ordinary words/ids never match.
    (
        re.compile(r"(?:^|\n)\s?(?:[A-Za-z0-9+/]{48,}\n)+\s?[A-Za-z0-9+/]{40,}={0,2}"),
        "\n" + _REDACTED,
    ),
)

# Workstation home prefixes inside free text: `/home/<user>/…`,
# `/Users/<user>/…` and Windows `C:\Users\<user>\`/`C:/Users/<user>/`
# prefixes (the tail of the path is kept).
_TEXT_PATH_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/home/[^/\s]+/"), "/"),
    (re.compile(r"/Users/[^/\s]+/"), "/"),
    (re.compile(r"(?i)\b[A-Za-z]:\\Users\\[^\\\s]+\\"), ""),
    (re.compile(r"(?i)\b[A-Za-z]:/Users/[^/\s]+/"), ""),
)

# 1) Quoted credential assignments: `PASSWORD="correct horse battery staple"`.
# The WHOLE quoted value (single or double quotes, with escaped quotes) is
# consumed — a value-part `\S+` grab would leave `… [REDACTED] horse
# battery staple"` behind, i.e. leak most of the credential.
_QUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?i)((?:--?)?[\w.-]*{_CRED_WORD}\s*[:=]\s*)"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')",
    re.DOTALL,
)
# 2) Credential flags whose value is the NEXT whitespace-delimited token,
# inside a larger string too (`sh -c 'tool --password secret'`): the value
# may itself be quoted.
_FLAG_VALUE_RE = re.compile(
    rf"(?i)((?:^|(?<=\s))--?[\w-]*{_CRED_WORD}\s+)"
    r"(?!\[REDACTED\])(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\S+)",
)
# 3) Bare (unquoted) credential assignments: the value's end cannot be told
# apart from following prose, so the rest of the line (up to `;`, `&`, `|`
# or newline) is redacted — EXCEPT when the slot is already our placeholder
# (optionally preceded by an auth scheme such as `Bearer`), so properly
# redacted text keeps its surrounding prose and URL parameter tails
# (`?token=[REDACTED]&x=1`) survive. Leading `\s*` lives INSIDE the safe
# prefix so the prefix separator cannot backtrack past the whitespace to
# dodge the lookahead.
_SAFE_VALUE_PREFIX = r"\s*(?:(?:bearer|basic|digest|token)\s+)?[\"']?\[REDACTED\][\"']?(?=[;&|\s]|$)"
_BARE_ASSIGNMENT_RE = re.compile(
    rf"(?i)((?:--?)?[\w.-]*{_CRED_WORD}\s*[:=]\s*)"
    rf"(?!{_SAFE_VALUE_PREFIX})"
    rf"(?!['\"])"  # unterminated quotes are fail-closed below, never half-redacted
    r"[^;&|\n]*",
)
# Verification-only: a credential assignment whose value slot still carries
# something unredacted after every pass above.
_ASSIGNMENT_SURVIVOR_RE = re.compile(
    rf"(?i)[\w.-]*{_CRED_WORD}\s*[:=]\s*(?!{_SAFE_VALUE_PREFIX})\S"
)
# A credential assignment anywhere in the text (for the unterminated-quote
# scan below).
_ASSIGNMENT_TRIGGER_RE = re.compile(rf"(?i)[\w.-]*{_CRED_WORD}\s*[:=]")

# CLI flags whose NEXT argv token is a credential value (--password secret).
_CREDENTIAL_VALUE_FLAG_RE = re.compile(
    r"(?i)^-{1,2}[\w-]*(?:password|passwd|secret|token|api-?key|apikey|auth"
    r"|authorization|credential|access-?key|private-?key)$"
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _fail_rejected(message: str) -> DecisionBackendError:
    return DecisionBackendError(message, reason=DecisionBackendFailureReason.STATE_REJECTED)


def _configured_credential_values() -> tuple[str, ...]:
    """Exact values of credential-named environment variables (longest first).

    Used ONLY as literal redaction needles; the names and values are never
    logged and never returned. Values may contain whitespace or newlines
    (multi-word secrets, multiline private keys) — the COMPLETE exact value
    is redacted. Short values (< 8 chars) are skipped so common words are
    never over-redacted.
    """
    values = []
    for name, value in os.environ.items():
        if not value or len(value) < _MIN_CREDENTIAL_VALUE_CHARS:
            continue
        if any(hint in name for hint in _CREDENTIAL_ENV_HINTS):
            values.append(value)
    return tuple(sorted(set(values), key=len, reverse=True))


def _strip_control(text: str) -> str:
    return _CONTROL_CHARS_RE.sub("", text)


def _unterminated_quoted_assignment(text: str) -> bool:
    """Recognizable malformed/truncated quoted assignment: the text contains
    a credential assignment and a double-quoted region that never closes by
    the end of the text (e.g. `PASSWORD="correct hor` cut by an upstream
    crop, or a quoted value spanning lines). Fail closed — never half-
    redact. Only double quotes are scanned (single-quoted truncation is
    caught by the bare-assignment survivor verification instead; apostrophes
    in prose make single-quote parity unreliable)."""
    if _ASSIGNMENT_TRIGGER_RE.search(text) is None:
        return False
    in_double = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_double = not in_double
    return in_double


def _redact_text(text: str, credential_values: tuple[str, ...]) -> str:
    """Apply every redaction pass. Order matters: exact configured env
    credential literals (longest first) are removed AS A WHOLE first — a
    structured-pattern pass would consume only the credential-shaped middle
    of such a composite value (provider token, URL userinfo, quoted
    assignment) and leave the literal's private prefix/suffix behind — then
    the structured secret shapes, then the assignment forms, then the
    workstation-path rewrites. Every pattern carries a lookahead for our own
    `[REDACTED]` placeholder so re-running redaction (e.g. gate
    sanitize-first followed by the backend's own state allowlist) is
    idempotent and the post-redaction verification below cannot match the
    placeholder we just wrote."""
    for value in credential_values:
        text = text.replace(value, _REDACTED)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = _QUOTED_ASSIGNMENT_RE.sub(r"\1" + _REDACTED, text)
    text = _FLAG_VALUE_RE.sub(r"\1" + _REDACTED, text)
    text = _BARE_ASSIGNMENT_RE.sub(r"\1" + _REDACTED, text)
    for pattern, replacement in _TEXT_PATH_REWRITES:
        text = pattern.sub(replacement, text)
    return text


def _redact_and_verify(text: str, credential_values: tuple[str, ...]) -> str:
    """Full-text redaction (NO truncation) + fail-closed verification.

    The unterminated-quote check runs on the RAW text: redaction would
    consume the quotes and make the malformed shape undetectable.
    """
    text = _strip_control(text)
    if _unterminated_quoted_assignment(text):
        raise _fail_rejected(
            "Remote state carries an unterminated quoted credential assignment; "
            "refusing to send it."
        )
    text = _redact_text(text, credential_values)
    if _still_secret(text, credential_values):
        raise _fail_rejected("Remote state text could not be sanitized safely.")
    return text


def _still_secret(text: str, credential_values: tuple[str, ...]) -> bool:
    """Fail-closed verification: any surviving secret-shaped match, exact
    credential literal, or unredacted assignment value after redaction."""
    for pattern, _ in _SECRET_PATTERNS:
        if pattern.search(text) is not None:
            return True
    for value in credential_values:
        if value in text:
            return True
    if _QUOTED_ASSIGNMENT_RE.search(text) is not None:
        return True
    if _FLAG_VALUE_RE.search(text) is not None:
        return True
    if _ASSIGNMENT_SURVIVOR_RE.search(text) is not None:
        return True
    return False


def _sanitize_command_tokens(
    tokens: Iterable[Any], credential_values: tuple[str, ...]
) -> list[str] | None:
    """Command argv tokens: credential flags' NEXT token, in-string
    assignments and flag values redacted; bounded. Returns None when the
    input is not a sequence of non-empty strings (shape violation -> the
    whole field is dropped, never partially sent)."""
    raw = tuple(tokens)
    cleaned: list[str] = []
    redact_next = False
    for token in raw:
        if not isinstance(token, str) or not token:
            return None  # shape violation -> drop the whole field
        text = _strip_control(token)
        if redact_next:
            text = _REDACTED
            redact_next = False
        else:
            redact_next = _CREDENTIAL_VALUE_FLAG_RE.fullmatch(text.strip()) is not None
        text = _redact_text(text, credential_values)
        if _still_secret(text, credential_values):
            raise _fail_rejected("Remote state command could not be sanitized safely.")
        cleaned.append(text[:_MAX_COMMAND_TOKEN_CHARS])
        if len(cleaned) >= _MAX_COMMAND_TOKENS:
            break
    return cleaned


def sanitize_process_output(
    stdout: str,
    stderr: str,
    command: Iterable[str],
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> tuple[str, str, tuple[str, ...]]:
    """Redact FULL raw process output BEFORE any extraction/cropping.

    Called by decision_runtime.gate for remote (Jev) backends BEFORE
    extract_error_block/TriageFacts truncate: a long quoted credential or
    private key straddling a later crop boundary is redacted as a whole
    first, so no fragment can survive the cut. Redacts `stdout`, `stderr`
    and each `command` token; does NOT truncate the streams (the caller
    crops deterministically afterwards). Raises DecisionBackendError
    (reason STATE_REJECTED) when a text cannot be proven clean — fail
    closed, nothing is sent.
    """
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise _fail_rejected("Raw process output must be strings to be sanitized.")
    credential_values = _configured_credential_values()
    clean_stdout = _redact_and_verify(stdout, credential_values)
    clean_stderr = _redact_and_verify(stderr, credential_values)
    if command is None:  # pragma: no cover - defensive
        command = ()
    tokens = _sanitize_command_tokens(tuple(command), credential_values)
    if tokens is None:  # pragma: no cover - argv is validated upstream
        raise _fail_rejected("Raw process command could not be sanitized safely.")
    return clean_stdout, clean_stderr, tuple(tokens)


def _sanitize_int_field(value: Any) -> int | None:
    """`exit_code`/`baseline_exit_code`: int or None only (bool rejected)."""
    if value is None:
        return None
    if type(value) is int:
        return value
    return None


def _sanitize_error_block(state: Mapping[str, Any], credential_values: tuple[str, ...]) -> str | None:
    raw = state.get("error_block")
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None  # shape violation -> field dropped
    text = _redact_and_verify(raw, credential_values)
    # Truncate AFTER redaction so a partial secret can never survive the cut.
    return text[:_MAX_ERROR_BLOCK_CHARS]


def _relative_to_root(path: str, workspace_root: str) -> str | None:
    """`workspace_root`-relative tail of an absolute `path`, or None when the
    path is not under the root (dropped — never send sibling/absolute info)."""
    root = workspace_root.replace("\\", "/").rstrip("/")
    if not root or not path.startswith(root + "/"):
        return None
    tail = path[len(root) + 1 :]
    return tail or None


def _is_absolute_windows_path(text: str) -> bool:
    return len(text) > 2 and text[1] == ":" and text[2] == "/"


def _sanitize_changed_path(entry: Any, credential_values: tuple[str, ...], workspace_root: str | None) -> str | None:
    """One `changed_paths` entry -> a bounded RELATIVE path, or None (drop).

    Absolute entries are relativized against `workspace_root` when given,
    else their `/home/<user>/`/`/Users/<user>/` prefix is stripped; anything
    that is still absolute, contains a `..` segment, or cannot be proven
    secret-free is dropped (or fails closed for unredactable secrets).
    """
    if not isinstance(entry, str) or not entry:
        return None
    text = _strip_control(entry).replace("\\", "/")
    if not text or "\n" in text:
        return None
    is_absolute = text.startswith("/") or _is_absolute_windows_path(text)
    if is_absolute:
        if workspace_root is not None:
            relative = _relative_to_root(text, workspace_root)
            if relative is None:
                return None
            text = relative
        else:
            for pattern, replacement in _TEXT_PATH_REWRITES:
                text = pattern.sub(replacement, text)
            if text.startswith("/") or _is_absolute_windows_path(text):
                return None  # still absolute and unknown -> drop
    text = _redact_text(text, credential_values)
    if _still_secret(text, credential_values):
        raise _fail_rejected("Remote state changed paths could not be sanitized safely.")
    if ".." in text.split("/"):
        return None
    return text[:_MAX_CHANGED_PATH_CHARS] or None


def _sanitize_changed_paths(
    state: Mapping[str, Any], credential_values: tuple[str, ...], workspace_root: str | None
) -> list[str] | None:
    raw = state.get("changed_paths")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None  # shape violation -> field dropped
    paths: list[str] = []
    for entry in raw:
        relative = _sanitize_changed_path(entry, credential_values, workspace_root)
        if relative is not None:
            paths.append(relative)
        if len(paths) >= _MAX_CHANGED_PATHS:
            break
    return paths


_BASELINE_STATUSES = frozenset({"pass", "fail", "timeout", "error"})


def sanitize_remote_state(
    state: Mapping[str, Any],
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], frozenset[str]]:
    """Return `(remote_payload, untrusted_fields)` for a decide() state.

    `remote_payload` contains ONLY the allowlisted fields above, each
    redacted-then-bounded; `untrusted_fields` names the fields that carried
    untrusted process output so the caller can label them in the question
    instructions (design rule 4). Raises DecisionBackendError (reason
    STATE_REJECTED) when `state` is not a mapping or when a text field
    cannot be proven secret-free after redaction — fail closed, nothing is
    sent.
    """
    if not isinstance(state, Mapping):
        raise _fail_rejected("Decision state for the TypeSafe call must be a mapping.")
    credential_values = _configured_credential_values()
    root = str(workspace_root) if workspace_root is not None else None

    payload: dict[str, Any] = {}
    raw_command = state.get("command")
    command = (
        _sanitize_command_tokens(raw_command, credential_values)
        if isinstance(raw_command, (list, tuple))
        else None  # shape violation -> field dropped (never sent)
    )
    if command is not None:
        payload["command"] = command
    if "exit_code" in state:
        payload["exit_code"] = _sanitize_int_field(state.get("exit_code"))
    if "timed_out" in state:
        payload["timed_out"] = state.get("timed_out") if type(state.get("timed_out")) is bool else None
    error_block = _sanitize_error_block(state, credential_values)
    if error_block is not None:
        payload["error_block"] = error_block
    changed_paths = _sanitize_changed_paths(state, credential_values, root)
    if changed_paths is not None:
        payload["changed_paths"] = changed_paths
    if "baseline_status" in state:
        status = state.get("baseline_status")
        # isinstance first: dict/list values are unhashable and would raise
        # TypeError on `in frozenset` — a malformed shape must be DOWNGRADED
        # to the safe "unknown" value, never crash the backend.
        payload["baseline_status"] = status if (isinstance(status, str) and status in _BASELINE_STATUSES) else None
    if "baseline_exit_code" in state:
        payload["baseline_exit_code"] = _sanitize_int_field(state.get("baseline_exit_code"))

    untrusted = frozenset(UNTRUSTED_STATE_FIELDS & payload.keys())
    return payload, untrusted
