"""decision_runtime.remote_state — the redaction/allowlist filter between
decision state and the TypeSafe API (docs/JEV-DESIGN.md design rule 4).

Covered here: the exact field allowlist (unknown state fields and complete
file contents never leave), redaction BEFORE truncation (no partial-secret
tails), URL userinfo/query-token/bearer/token-shape redaction, exact
configured credential ENV values (no .env reading anywhere), workstation
absolute-path/home-name stripping, shape violations dropped (fail closed at
the field level), the fail-closed raise when a text field cannot be proven
clean, and non-mutation of the caller's state dict.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import decision_runtime.remote_state as remote_state  # noqa: E402
from decision_runtime.errors import DecisionBackendError, DecisionBackendFailureReason  # noqa: E402
from decision_runtime.remote_state import (  # noqa: E402
    ALLOWED_STATE_FIELDS,
    UNTRUSTED_STATE_FIELDS,
    sanitize_process_output,
    sanitize_remote_state,
)


def _base_state(**overrides):
    state = {
        "command": ["pytest", "-q", "tests/"],
        "exit_code": 1,
        "timed_out": False,
        "error_block": "AssertionError: expected 4 == 5",
        "changed_paths": ["src/adapter.py"],
        "baseline_status": "fail",
        "baseline_exit_code": 1,
        # unknown fields that must never leave:
        "full_file_contents": "import os\nSECRET = 1\n" * 50,
        "workspace_root": "/home/someuser/secret-project",
        "diff": "+++ b/secrets.txt\n",
        "prompt": "user typed something private",
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# allowlist
# ---------------------------------------------------------------------------


def test_allowlist_is_exactly_the_triage_state_contract():
    assert ALLOWED_STATE_FIELDS == {
        "command", "exit_code", "timed_out", "error_block",
        "changed_paths", "baseline_status", "baseline_exit_code",
    }


def test_unknown_arbitrary_fields_and_file_contents_never_leave():
    state = _base_state()

    payload, untrusted = sanitize_remote_state(state)

    assert set(payload) == ALLOWED_STATE_FIELDS
    assert "full_file_contents" not in payload
    assert "SECRET FILE" not in str(payload)
    assert "user typed something private" not in str(payload)


def test_triage_state_round_trips_unchanged():
    payload, untrusted = sanitize_remote_state(_base_state())

    assert payload == {
        "command": ["pytest", "-q", "tests/"],
        "exit_code": 1,
        "timed_out": False,
        "error_block": "AssertionError: expected 4 == 5",
        "changed_paths": ["src/adapter.py"],
        "baseline_status": "fail",
        "baseline_exit_code": 1,
    }
    assert untrusted == UNTRUSTED_STATE_FIELDS & set(payload)


def test_input_state_is_never_mutated():
    state = _base_state()
    snapshot = {
        key: (list(value) if isinstance(value, list) else value) for key, value in state.items()
    }

    sanitize_remote_state(state, workspace_root="/home/someuser/secret-project")

    assert {
        key: (list(value) if isinstance(value, list) else value) for key, value in state.items()
    } == snapshot


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_url_userinfo_and_query_tokens_are_redacted():
    state = _base_state(
        error_block="clone failed: https://deploy:hunter2@git.internal/repo.git?token=abc123def&x=1",
    )

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert "hunter2" not in text
    assert "abc123def" not in text
    assert "https://[REDACTED]@git.internal/repo.git?token=[REDACTED]&x=1" in text


def test_bearer_strings_and_common_token_shapes_are_redacted():
    state = _base_state(
        error_block=(
            "Authorization: Bearer abcdefghijklmno; openai key sk-proj-abcdefgh12345678; "
            "ghp_0123456789abcdefghij; AKIAABCDEFGHIJKLMNOP; jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.s3cr3tsig"
        ),
    )

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert "abcdefghijklmno" not in text
    assert "sk-proj-abcdefgh12345678" not in text
    assert "ghp_0123456789abcdefghij" not in text
    assert "AKIAABCDEFGHIJKLMNOP" not in text
    assert "eyJhbGciOiJIUzI1NiJ9" not in text
    assert "[REDACTED]" in text


def test_exact_configured_credential_env_values_are_redacted(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "supersecretvalue123")
    monkeypatch.setenv("OTHER_API_KEY", "apikeyliteral4567")
    monkeypatch.setenv("NOT_A_SECRET_VAR", "harmless-value-1")  # not credential-named
    state = _base_state(
        error_block="curl failed with MY_SERVICE_TOKEN=supersecretvalue123 and other=apikeyliteral4567",
    )

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert "supersecretvalue123" not in text
    assert "apikeyliteral4567" not in text
    assert "[REDACTED]" in text


# S1b bug regressions: an exact configured credential literal must be
# removed AS A WHOLE before the pattern passes run — pattern redaction
# consumes only the credential-shaped middle of a composite value and would
# otherwise leave the private prefix/suffix of the known literal behind.


def test_exact_env_credential_is_redacted_as_a_whole_in_error_block(monkeypatch):
    """REVIEW_SERVICE_TOKEN='privateprefix sk-proj-abcdefgh12345678
    privatesuffix' in an error_block: pattern-first order used to return
    'failure: privateprefix [REDACTED] privatesuffix' — leaking most of the
    known credential. Benign prose around it stays."""
    value = "privateprefix sk-proj-abcdefgh12345678 privatesuffix"
    monkeypatch.setenv("REVIEW_SERVICE_TOKEN", value)
    state = _base_state(error_block="failure: " + value)

    payload, _ = sanitize_remote_state(state)

    assert payload["error_block"] == "failure: [REDACTED]"
    assert "privateprefix" not in payload["error_block"]
    assert "privatesuffix" not in payload["error_block"]
    assert "sk-proj-abcdefgh12345678" not in payload["error_block"]


def test_exact_env_credential_is_redacted_as_a_whole_in_command_tokens(monkeypatch):
    value = "privateprefix sk-proj-abcdefgh12345678 privatesuffix"
    monkeypatch.setenv("REVIEW_SERVICE_TOKEN", value)
    state = _base_state(command=["run", "creds: " + value])

    payload, _ = sanitize_remote_state(state)

    text = str(payload["command"])
    assert "privateprefix" not in text
    assert "privatesuffix" not in text
    assert "sk-proj-abcdefgh12345678" not in text
    assert "creds: [REDACTED]" in text


def test_exact_env_credential_is_redacted_as_a_whole_in_changed_paths(monkeypatch):
    value = "privateprefix sk-proj-abcdefgh12345678 privatesuffix"
    monkeypatch.setenv("REVIEW_SERVICE_TOKEN", value)
    state = _base_state(changed_paths=["src/" + value + ".py", "src/clean.py"])

    payload, _ = sanitize_remote_state(state)

    assert payload["changed_paths"] == ["src/[REDACTED].py", "src/clean.py"]


def test_exact_env_credential_composites_are_redacted_whole_in_process_output(monkeypatch):
    """sanitize_process_output seam: provider-token-, URL- and assignment-
    shaped composite env credentials in stdout/stderr/argv — each literal is
    removed whole (prefix/suffix gone), and redaction stays idempotent."""
    token_value = "privateprefix sk-proj-abcdefgh12345678 privatesuffix"
    password_value = 'lead-in PASSWORD="hunter2secret99" trail'
    url_value = "start https://user:topsecret99@api.example.com end"
    monkeypatch.setenv("REVIEW_SERVICE_TOKEN", token_value)
    monkeypatch.setenv("VAULT_PASSWORD", password_value)
    monkeypatch.setenv("GATEWAY_API_KEY", url_value)

    clean_stdout, clean_stderr, command = sanitize_process_output(
        "boom: " + url_value,
        "warn: " + password_value,
        ("run", "creds: " + token_value),
    )
    re_clean_stdout, re_clean_stderr, re_command = sanitize_process_output(
        clean_stdout, clean_stderr, command,
    )

    assert clean_stdout == "boom: [REDACTED]"
    assert clean_stderr == "warn: [REDACTED]"
    assert command == ("run", "creds: [REDACTED]")
    for text in (clean_stdout, clean_stderr, " ".join(command)):
        for fragment in ("privateprefix", "privatesuffix", "sk-proj-abcdefgh12345678",
                         "lead-in", "trail", "hunter2secret99",
                         "start", "end", "topsecret99"):
            assert fragment not in text
    assert (re_clean_stdout, re_clean_stderr, re_command) == (clean_stdout, clean_stderr, command)


def test_redaction_happens_before_truncation_no_partial_secret_survives(monkeypatch):
    """The planted bearer sits such that truncate-first would cut the token
    in half (leaving 'tailsec…'); redact-first must make even a PARTIAL
    fragment impossible."""
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tailsecret9")
    state = _base_state(error_block="x" * 3_993 + "Bearer tailsecret9 " + "y" * 200)

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert len(text) <= 4_000
    assert "tails" not in text  # no partial secret fragment survives the cut
    assert "y" not in text[-1]  # bounded to the cap


def test_command_credential_flags_and_assignments_are_redacted():
    state = _base_state(
        command=[
            "pg_restore", "--password=hunter2secret", "--db-token", "tokendeadbeef99",
            "backup.dump", "https://api.example.com/v1?api_key=abcd1234efgh",
        ],
    )

    payload, _ = sanitize_remote_state(state)

    text = str(payload["command"])
    assert "hunter2secret" not in text
    assert "tokendeadbeef99" not in text
    assert "abcd1234efgh" not in text
    assert "--password=[REDACTED]" in text
    assert "--db-token" in text and "[REDACTED]" in text
    assert "https://api.example.com/v1?api_key=[REDACTED]" in text


# ---------------------------------------------------------------------------
# quoted assignments / multiline keys / flags inside shell strings
# (S1b review REQUIRED corrections)
# ---------------------------------------------------------------------------


def test_quoted_assignment_value_is_redacted_completely():
    """`PASSWORD="correct horse battery staple"` must become
    `PASSWORD=[REDACTED]` — the WHOLE quoted value; a `\\S+` grab would leak
    `… [REDACTED] horse battery staple"`."""
    state = _base_state(error_block='PASSWORD="correct horse battery staple" in config')

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert text == 'PASSWORD=[REDACTED] in config'
    assert "horse battery" not in text


def test_single_quoted_and_escaped_quote_values_are_redacted_completely():
    state = _base_state(error_block="TOKEN='say it\\'s \"gone\"'; retry")

    payload, _ = sanitize_remote_state(state)

    assert payload["error_block"].startswith("TOKEN=[REDACTED]")
    assert "gone" not in payload["error_block"]
    assert "say it" not in payload["error_block"]


def test_quoted_value_prose_after_it_is_kept():
    state = _base_state(error_block='PASSWORD="abc123xyz" and the retry succeeded')

    payload, _ = sanitize_remote_state(state)

    assert payload["error_block"] == "PASSWORD=[REDACTED] and the retry succeeded"


def test_unquoted_multiword_assignment_value_is_redacted_to_end_of_line():
    state = _base_state(error_block="error: db_password=correct horse staple; retrying now")

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert "correct horse" not in text
    assert "; retrying now" in text


def test_flag_value_inside_a_shell_command_string_is_redacted():
    state = _base_state(command=["sh", "-c", "tool --password secret && cleanup"])

    payload, _ = sanitize_remote_state(state)

    text = str(payload["command"])
    assert "secret" not in text
    assert "--password [REDACTED]" in text
    assert "&& cleanup" in text


def test_flag_with_quoted_value_inside_a_shell_string_is_redacted_completely():
    state = _base_state(command=["sh", "-c", 'tool --password "one two three" && cleanup'])

    payload, _ = sanitize_remote_state(state)

    text = str(payload["command"])
    assert "one two three" not in text
    assert "&& cleanup" in text


def test_bearer_redaction_keeps_the_surrounding_prose():
    state = _base_state(error_block="Authorization: Bearer abcdefghijklmno failed with 401")

    payload, _ = sanitize_remote_state(state)

    assert payload["error_block"] == "Authorization: Bearer [REDACTED] failed with 401"


def test_url_query_tail_survives_the_token_redaction():
    state = _base_state(error_block="GET https://api.example.com/v1?token=abcdef123456&expand=1 -> 403")

    payload, _ = sanitize_remote_state(state)

    assert "abcdef123456" not in payload["error_block"]
    assert "&expand=1" in payload["error_block"]


def test_multiline_private_key_block_is_redacted(monkeypatch):
    body = "\n".join("Ab3dEf6Gh9Ij2Kl5Mn8Qr1Tu4Wx7Yz0A" + str(i) * 6 for i in range(8))
    key_block = "-----BEGIN RSA PRIVATE KEY-----\n" + body + "\n-----END RSA PRIVATE KEY-----"
    state = _base_state(error_block="check failed with:\n" + key_block + "\nnothing else matters")

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert "PRIVATE KEY" not in text
    assert "Ab3dEf6Gh9Ij2Kl5" not in text


def test_base64_key_body_surviving_an_upstream_crop_is_redacted():
    """Fail-closed depth for pre-cropped input: a run of base64 lines (the
    body of a key whose BEGIN marker was already cropped away) is redacted."""
    body = "\n".join(("Ab3dEf6Gh9Ij2Kl5Mn8Qr1Tu4Wx7Yz0A" * 2) + str(i) * 6 for i in range(8))
    state = _base_state(error_block=body + "\n-----END PRIVATE KEY-----")

    payload, _ = sanitize_remote_state(state)

    assert "Ab3dEf6Gh9Ij2Kl5" not in payload["error_block"]


def test_multiline_and_whitespace_env_credential_values_are_redacted_completely(monkeypatch):
    monkeypatch.setenv("DEPLOY_PASSWORD", "correct horse battery staple")
    key_env = "-----BEGIN PRIVATE KEY-----\nAb3dEf6Gh9Ij2Kl5Mn8Qr1Tu4Wx7Yz0A000000\n-----END PRIVATE KEY-----"
    monkeypatch.setenv("DEPLOY_PRIVATE_KEY", key_env)
    state = _base_state(
        error_block=(
            'auth used DEPLOY_PASSWORD="correct horse battery staple"; '
            "key was " + key_env + "; done"
        ),
    )

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert "correct horse battery staple" not in text
    assert "Ab3dEf6Gh9Ij2Kl5" not in text
    assert "PRIVATE KEY" not in text


def test_short_env_values_are_not_used_as_needles(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "short")  # < 8 chars: over-redaction guard
    state = _base_state(error_block="token short appears in prose")

    payload, _ = sanitize_remote_state(state)

    assert payload["error_block"] == "token short appears in prose"


def test_unterminated_quoted_assignment_fails_closed():
    state = _base_state(error_block='PASSWORD="correct horse battery staple')

    with pytest.raises(DecisionBackendError) as excinfo:
        sanitize_remote_state(state)
    assert excinfo.value.reason is DecisionBackendFailureReason.STATE_REJECTED


def test_broken_redactor_with_quoted_assignment_fails_closed(monkeypatch):
    """Verification includes the surviving assignment patterns: a redactor
    that somehow leaves `PASSWORD="secret"` intact must fail closed, not
    ship it."""
    monkeypatch.setenv("MY_TOKEN", "zzzz-secret-zzzz")
    original = remote_state._redact_text

    def broken(text, credential_values):  # pragma: no cover - simulates a broken pass
        return text

    monkeypatch.setattr(remote_state, "_redact_text", broken)
    try:
        with pytest.raises(DecisionBackendError) as excinfo:
            sanitize_remote_state(_base_state(error_block='MY_TOKEN="zzzz-secret-zzzz" here'))
        assert excinfo.value.reason is DecisionBackendFailureReason.STATE_REJECTED
    finally:
        monkeypatch.setattr(remote_state, "_redact_text", original)


def test_malformed_windows_drive_only_path_does_not_crash():
    state = _base_state(changed_paths=["C:", "src/ok.py"])

    payload, _ = sanitize_remote_state(state)

    assert "C:" in payload["changed_paths"]
    assert "src/ok.py" in payload["changed_paths"]


def test_unhashable_baseline_status_is_downgraded_not_crashing():
    state = _base_state(baseline_status={"malicious": True})
    payload, _ = sanitize_remote_state(state)
    assert payload["baseline_status"] is None

    state2 = _base_state(baseline_status=["fail"])
    payload2, _ = sanitize_remote_state(state2)
    assert payload2["baseline_status"] is None


def test_process_output_helper_redacts_before_any_crop():
    """The gate's sanitize-first seam: full raw text in, redacted FULL text
    out (no truncation here — the caller crops afterwards)."""
    secret = 'TOKEN="' + "x" * 5000 + '"'
    clean_stdout, clean_stderr, command = sanitize_process_output(
        "head\n" + secret + "\ntail", "err-stdout", ("pytest", "--api-key", "abcd1234efghij"),
    )

    assert "x" * 100 not in clean_stdout
    assert "[REDACTED]" in clean_stdout
    assert clean_stderr == "err-stdout"
    assert command == ("pytest", "--api-key", "[REDACTED]")


# ---------------------------------------------------------------------------
# path stripping (relative changed paths only, home names never leave)
# ---------------------------------------------------------------------------


def test_absolute_changed_paths_are_dropped_without_workspace_root():
    state = _base_state(changed_paths=["/home/someuser/proj/src/a.py", "src/adapter.py"])

    payload, _ = sanitize_remote_state(state)

    assert payload["changed_paths"] == ["src/adapter.py"]
    assert "someuser" not in str(payload)


def test_absolute_changed_paths_relativize_against_workspace_root():
    state = _base_state(
        changed_paths=["/home/someuser/proj/src/a.py", "/elsewhere/outside.py"],
    )

    payload, _ = sanitize_remote_state(state, workspace_root="/home/someuser/proj")

    assert payload["changed_paths"] == ["src/a.py"]
    assert "someuser" not in str(payload)
    assert "/elsewhere" not in str(payload)


def test_home_and_users_prefixes_are_stripped_from_diagnostics():
    state = _base_state(
        error_block=(
            "FileNotFoundError: /home/someuser/proj/data.csv missing; "
            "log at /Users/someuser/Library/Logs/x.log; win C:\\Users\\someuser\\tmp\\y.log"
        ),
    )

    payload, _ = sanitize_remote_state(state)

    text = payload["error_block"]
    assert "/home/someuser/" not in text
    assert "/Users/someuser/" not in text
    assert "Users\\someuser\\" not in text


def test_changed_paths_with_secret_shaped_material_are_redacted():
    state = _base_state(changed_paths=["src/sk-proj-abcdefgh12345678.py", "src/clean.py"])

    payload, _ = sanitize_remote_state(state)

    assert payload["changed_paths"] == ["src/[REDACTED].py", "src/clean.py"]


def test_changed_paths_parent_traversal_entries_are_dropped():
    state = _base_state(changed_paths=["src/../../etc/passwd", "src/ok.py"])

    payload, _ = sanitize_remote_state(state)

    assert payload["changed_paths"] == ["src/ok.py"]


def test_changed_path_count_is_bounded():
    state = _base_state(changed_paths=[f"src/file_{i}.py" for i in range(80)])

    payload, _ = sanitize_remote_state(state)

    assert len(payload["changed_paths"]) == 50


# ---------------------------------------------------------------------------
# shape violations are dropped (never sent), fail closed on unredactable text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("command", "not-a-list"),
        ("command", ["ok", 7]),
        ("error_block", {"not": "a string"}),
        ("changed_paths", "not-a-list"),
        ("changed_paths", [42]),
        ("exit_code", "1"),
        ("exit_code", True),
        ("baseline_exit_code", 1.0),
        ("timed_out", "no"),
        ("baseline_status", "maybe"),
    ],
)
def test_wrongly_shaped_fields_are_dropped_never_sent(field, value):
    state = _base_state(**{field: value})

    payload, _ = sanitize_remote_state(state)

    if field in ("exit_code", "timed_out", "baseline_status", "baseline_exit_code"):
        # bounded contract fields are downgraded to their safe "unknown" value
        assert payload[field] is None
    elif field == "changed_paths":
        # every invalid entry is dropped; a fully-invalid list sends nothing useful
        assert payload.get(field, []) == [p for p in (value if isinstance(value, list) else []) if isinstance(p, str) and p and ".." not in p.replace("\\", "/").split("/") and not p.startswith("/")]
    else:
        assert field not in payload


def test_non_mapping_state_fails_closed():
    with pytest.raises(DecisionBackendError) as excinfo:
        sanitize_remote_state(["not", "a", "mapping"])
    assert excinfo.value.reason is DecisionBackendFailureReason.STATE_REJECTED


def test_unredactable_secret_text_fails_closed(monkeypatch):
    """Fail closed: if redaction cannot prove the text clean, nothing is
    sent and the caller falls back to the deterministic backend."""
    monkeypatch.setenv("MY_SERVICE_TOKEN", "supersecretvalue123")
    original_redact = remote_state._redact_text

    def broken_redact(text, credential_values):  # pragma: no cover - simulate a broken redactor
        return text  # never redacts anything

    monkeypatch.setattr(remote_state, "_redact_text", broken_redact)
    try:
        with pytest.raises(DecisionBackendError) as excinfo:
            sanitize_remote_state(_base_state(error_block="token is supersecretvalue123 here"))
        assert excinfo.value.reason is DecisionBackendFailureReason.STATE_REJECTED
    finally:
        monkeypatch.setattr(remote_state, "_redact_text", original_redact)


def test_no_credential_values_are_logged(caplog, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "supersecretvalue123")
    with caplog.at_level(logging.DEBUG):
        sanitize_remote_state(_base_state(error_block="leaks supersecretvalue123"))

    assert all("supersecretvalue123" not in record.getMessage() for record in caplog.records)
