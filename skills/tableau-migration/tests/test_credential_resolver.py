"""Unit tests for the layered, Key-Vault-free credential resolver (``credential_resolver``).

Every layer is exercised offline through the injectable seams (``environ`` / ``keyring_module`` /
``prompt_func`` / ``isatty``) so no test touches the real environment, OS keyring, or a TTY. No
secret value ever appears in a ``repr`` or in the not-found error.
"""
import pytest

import credential_resolver as C


# -- precedence --------------------------------------------------------------------------------
def test_explicit_value_wins():
    r = C.resolve_secret("PAT", explicit="x", env_var="PAT", environ={"PAT": "y"})
    assert r.value == "x"
    assert r.source == "argument"


def test_explicit_blank_is_skipped_and_env_used():
    r = C.resolve_secret("PAT", explicit="   ", env_var="PAT", environ={"PAT": "fromenv"})
    assert r.value == "fromenv"
    assert r.source == "env:PAT"


def test_env_var_layer():
    r = C.resolve_secret("PAT", env_var="TABLEAU_PAT", environ={"TABLEAU_PAT": "tok"})
    assert (r.value, r.source) == ("tok", "env:TABLEAU_PAT")


def test_env_layer_skipped_when_no_env_var_configured():
    with pytest.raises(C.CredentialNotFound):
        C.resolve_secret("PAT", environ={"PAT": "tok"})  # env_var is None -> layer not tried


# -- .env file ---------------------------------------------------------------------------------
def test_dotenv_layer(tmp_path):
    p = tmp_path / ".env"
    p.write_text('export TABLEAU_PAT="dotenv-tok"\n# comment\nOTHER=zzz\n', encoding="utf-8")
    r = C.resolve_secret("PAT", env_var="TABLEAU_PAT", env_file=str(p), environ={})
    assert r.value == "dotenv-tok"
    assert r.source == "dotenv:%s" % p


def test_dotenv_missing_file_is_not_fatal():
    with pytest.raises(C.CredentialNotFound):
        C.resolve_secret("PAT", env_var="PAT", env_file="/no/such/file.env", environ={})


def test_parse_env_file_handles_quotes_comments_export_and_bom(tmp_path):
    p = tmp_path / "creds.env"
    p.write_text("\ufeff# header\nexport A='one'\nB = \"two\"\nC=three\n\nBAD LINE\n",
                 encoding="utf-8")
    parsed = C.parse_env_file(str(p))
    assert parsed == {"A": "one", "B": "two", "C": "three"}


# -- keyring (optional dependency, injected) ----------------------------------------------------
class _FakeKeyring:
    def __init__(self, store):
        self._store = store

    def get_password(self, service, username):
        return self._store.get((service, username))


def test_keyring_layer_with_injected_module():
    kr = _FakeKeyring({("tableau-migration", "migrator"): "kr-tok"})
    r = C.resolve_secret("PAT", keyring_service="tableau-migration", keyring_username="migrator",
                         environ={}, keyring_module=kr)
    assert r.value == "kr-tok"
    assert r.source == "keyring:tableau-migration"


def test_keyring_miss_falls_through():
    kr = _FakeKeyring({})
    with pytest.raises(C.CredentialNotFound):
        C.resolve_secret("PAT", keyring_service="svc", keyring_username="u",
                         environ={}, keyring_module=kr)


def test_keyring_error_is_swallowed():
    class _Boom:
        def get_password(self, *_a):
            raise RuntimeError("locked")
    with pytest.raises(C.CredentialNotFound):
        C.resolve_secret("PAT", keyring_service="svc", environ={}, keyring_module=_Boom())


# -- prompt (opt-in, injected) ------------------------------------------------------------------
def test_prompt_layer_used_when_allowed_and_func_injected():
    seen = {}

    def fake_prompt(text):
        seen["text"] = text
        return "typed-tok"

    r = C.resolve_secret("PAT", allow_prompt=True, prompt_text="Enter PAT: ",
                         environ={}, prompt_func=fake_prompt)
    assert r.value == "typed-tok"
    assert r.source == "prompt"
    assert seen["text"] == "Enter PAT: "


def test_prompt_not_used_without_tty_and_no_injected_func():
    # allow_prompt but no TTY and no injected prompt -> must NOT hang; just not found.
    with pytest.raises(C.CredentialNotFound):
        C.resolve_secret("PAT", allow_prompt=True, environ={}, isatty=False)


def test_prompt_not_used_when_not_allowed():
    with pytest.raises(C.CredentialNotFound):
        C.resolve_secret("PAT", allow_prompt=False, environ={},
                         prompt_func=lambda *_a: "should-not-be-called")


# -- ordering + safety --------------------------------------------------------------------------
def test_full_precedence_chain_env_over_dotenv_over_keyring(tmp_path):
    p = tmp_path / ".env"
    p.write_text("PAT=dotenv\n", encoding="utf-8")
    kr = _FakeKeyring({("svc", "PAT"): "keyring"})
    # env beats dotenv beats keyring
    r = C.resolve_secret("PAT", env_var="PAT", env_file=str(p), keyring_service="svc",
                         environ={"PAT": "envwins"}, keyring_module=kr)
    assert r.value == "envwins"


def test_not_found_lists_layers_without_value():
    with pytest.raises(C.CredentialNotFound) as exc:
        C.resolve_secret("PAT", env_var="PAT", environ={"PAT": "  "})
    msg = str(exc.value)
    assert "env:PAT" in msg
    assert "  " not in msg  # the (blank) value is never echoed


def test_resolved_secret_repr_redacts_value():
    r = C.ResolvedSecret("super-secret", "argument")
    assert "super-secret" not in repr(r)
    assert "super-secret" not in str(r)
    assert "<redacted>" in repr(r)
    assert r.value == "super-secret"


def test_all_default_call_finds_nothing():
    with pytest.raises(C.CredentialNotFound):
        C.resolve_secret("PAT", environ={})


# -- clear_secret_env (cleanup) -----------------------------------------------------------------
def test_clear_secret_env_removes_present_and_returns_names():
    env = {"TABLEAU_PAT_VALUE": "s3cr3t", "OTHER": "keep"}
    cleared = C.clear_secret_env("TABLEAU_PAT_VALUE", "TABLEAU_CONNECTED_APP_SECRET_VALUE",
                                 environ=env)
    assert cleared == ["TABLEAU_PAT_VALUE"]      # only the one that was present
    assert "TABLEAU_PAT_VALUE" not in env        # actually removed
    assert env["OTHER"] == "keep"                # untouched


def test_clear_secret_env_ignores_absent_and_is_value_free():
    env = {"OTHER": "keep"}
    cleared = C.clear_secret_env("TABLEAU_PAT_VALUE", environ=env)
    assert cleared == []                          # nothing present -> nothing cleared
    assert "s3cr3t" not in repr(cleared)          # the return trace carries no value
    assert env == {"OTHER": "keep"}


def test_clear_secret_env_clears_multiple_sorted():
    env = {"B_SECRET": "x", "A_SECRET": "y", "OTHER": "z"}
    cleared = C.clear_secret_env("B_SECRET", "A_SECRET", environ=env)
    assert cleared == ["A_SECRET", "B_SECRET"]    # sorted, value-free
    assert env == {"OTHER": "z"}
