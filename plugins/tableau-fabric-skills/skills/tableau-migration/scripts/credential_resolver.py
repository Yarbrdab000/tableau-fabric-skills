"""Layered, Key-Vault-free secret resolver for local / POC migration runs.

Resolves a secret (such as a Tableau Personal Access Token's *secret value*) from the FIRST
configured-and-available source, in a fixed precedence order, so a migration can run on a laptop
that has **no Azure Key Vault**:

    1. ``explicit``  -- a value the caller already holds (passed in code / orchestration)
    2. ``env_var``   -- a process environment variable
    3. ``env_file``  -- a ``KEY=VALUE`` line in a local ``.env`` file (parsed with the stdlib;
                         ``python-dotenv`` is NOT required), looked up under the ``env_var`` key
    4. ``keyring``   -- the OS secret store via the optional ``keyring`` package (Windows
                         Credential Manager, macOS Keychain, freedesktop Secret Service), used
                         only when that package is installed
    5. ``prompt``    -- an interactive ``getpass`` prompt, only when explicitly allowed AND a
                         console is attached (so an unattended run never hangs)

Security posture: the resolved value is returned to the caller and **nowhere else**. The resolver
never logs, prints, or persists it; the returned :class:`ResolvedSecret` has a redacting ``repr``
and a ``source`` label naming only the layer that answered (a value-free audit trace). A layer
whose configuration is absent is silently skipped; when no layer yields a value
:class:`CredentialNotFound` is raised with a message that lists only the layers tried -- never any
value.

Pure and dependency-free at import time: ``keyring`` and ``getpass`` are imported lazily, and the
``environ`` / ``keyring_module`` / ``prompt_func`` / ``isatty`` seams make every layer unit-testable
offline without touching the real environment, OS keyring, or a TTY.
"""

import os


class CredentialNotFound(RuntimeError):
    """Raised when no configured layer produced a secret. Never carries a secret value."""


class ResolvedSecret:
    """A resolved secret plus the NAME of the layer that produced it.

    ``repr`` / ``str`` redact the value so the secret cannot leak into a log line or a traceback;
    read the real value through :attr:`value` only. :attr:`source` is a short, value-free label
    (e.g. ``"argument"``, ``"env:TABLEAU_PAT"``, ``"keyring:tableau-migration"``, ``"prompt"``).
    """

    __slots__ = ("value", "source")

    def __init__(self, value, source):
        self.value = value
        self.source = source

    def __repr__(self):
        return "ResolvedSecret(source=%r, value=<redacted>)" % (self.source,)

    __str__ = __repr__


def _clean(value):
    """Return a non-empty stripped ``str``, or ``None`` for ``None`` / blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_env_file(path):
    """Parse a ``.env`` file into a ``{KEY: VALUE}`` dict using only the stdlib.

    Recognises ``KEY=VALUE`` lines, an optional leading ``export``, ``#`` comments, blank lines, and
    single- or double-quoted values (the matching outer quotes are stripped). Later duplicate keys
    win. Returns ``{}`` when the file is missing or unreadable -- the caller decides whether that is
    fatal -- and never raises on a malformed line. Reads as ``utf-8-sig`` so a BOM is tolerated.
    """
    result = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lines = handle.readlines()
    except (OSError, UnicodeError):
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line[:7].lower() == "export ":
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            result[key] = val
    return result


def _stdin_is_tty():
    try:
        import sys
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def clear_secret_env(*names, environ=None):
    """Remove each named secret variable from the process environment; return the names cleared.

    A centralized cleanup so a secret pulled into ``os.environ`` (e.g. ``TABLEAU_PAT_VALUE`` set
    from a vault or a masked prompt) does not linger in **this** process after it has been used.
    Designed to be called from a ``finally`` block so it runs on both the success and the failure
    path. Returns the sorted list of names that were actually present and removed (a value-free
    audit trace); names that are absent are ignored. Never reads, returns, or logs a value, and --
    because a child process only ever holds a COPY of the environment -- it cannot affect the
    parent shell's variables. ``environ`` is an injectable test seam (defaults to ``os.environ``).
    """
    environ = os.environ if environ is None else environ
    cleared = []
    for name in names:
        if name and name in environ:
            try:
                del environ[name]
            except Exception:
                continue
            cleared.append(name)
    return sorted(cleared)


def resolve_secret(name, *, explicit=None, env_var=None, env_file=None,
                   keyring_service=None, keyring_username=None, allow_prompt=False,
                   prompt_text=None, environ=None, keyring_module=None, prompt_func=None,
                   isatty=None):
    """Resolve ``name`` from the first configured-and-available layer (see the module docstring).

    Only layers whose configuration is supplied are tried -- e.g. ``env_var`` is skipped when it is
    ``None`` -- so an all-default call resolves nothing and raises :class:`CredentialNotFound`.
    ``name`` is a human label used only in prompts and error text (never as a secret key). The
    ``environ`` / ``keyring_module`` / ``prompt_func`` / ``isatty`` arguments are injectable test
    seams; they default to the real ``os.environ``, a lazily-imported ``keyring``,
    ``getpass.getpass`` and a stdin-TTY check respectively. Returns a :class:`ResolvedSecret`.
    """
    environ = os.environ if environ is None else environ
    tried = []

    # 1. explicit value the caller already holds (highest precedence)
    tried.append("argument")
    value = _clean(explicit)
    if value is not None:
        return ResolvedSecret(value, "argument")

    # 2. process environment variable
    if env_var:
        label = "env:%s" % env_var
        tried.append(label)
        value = _clean(environ.get(env_var))
        if value is not None:
            return ResolvedSecret(value, label)

    # 3. .env file, looked up under the same env_var key
    if env_file and env_var:
        label = "dotenv:%s" % env_file
        tried.append(label)
        value = _clean(parse_env_file(env_file).get(env_var))
        if value is not None:
            return ResolvedSecret(value, label)

    # 4. OS keyring (optional dependency; absence is not an error)
    if keyring_service:
        label = "keyring:%s" % keyring_service
        tried.append(label)
        module = keyring_module
        if module is None:
            try:
                import keyring as module  # type: ignore
            except Exception:
                module = None
        if module is not None:
            user = keyring_username or name
            try:
                value = _clean(module.get_password(keyring_service, user))
            except Exception:
                value = None
            if value is not None:
                return ResolvedSecret(value, label)

    # 5. interactive prompt (opt-in, last resort; never hangs an unattended run)
    if allow_prompt:
        tried.append("prompt")
        attached = _stdin_is_tty() if isatty is None else bool(isatty)
        if prompt_func is not None or attached:
            getter = prompt_func
            if getter is None:
                import getpass
                getter = getpass.getpass
            value = _clean(getter(prompt_text or ("Enter %s: " % name)))
            if value is not None:
                return ResolvedSecret(value, "prompt")

    raise CredentialNotFound(
        "no value for %r from the configured credential layers (tried: %s); supply one of: an "
        "explicit value, an environment variable, a .env file entry, an OS keyring secret, or "
        "allow an interactive prompt" % (name, ", ".join(tried) or "none")
    )
