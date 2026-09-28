"""Which model we talk to, described by a JSON file.

A config file is about *endpoints*: model, base url, where the key lives. It
is deliberately not about the agent -- max steps, context budget and the
workspace stay command-line flags, so every setting has exactly one home and
there is no "which one won" question to answer.

    {
      "default": "deepseek",
      "profiles": {
        "deepseek": {
          "base_url": "https://api.deepseek.com",
          "model": "deepseek-chat",
          "api_key_env": "DEEPSEEK_API_KEY"
        },
        "local": {
          "base_url": "http://localhost:8000/v1",
          "model": "qwen-max",
          "api_key_env": null
        }
      }
    }

Keys are never stored in the file. A profile names the environment variable
to read instead, because this file is meant to be committed, and a key in it
would only surface as a leak after somebody pushed. `api_key_env: null` means
the endpoint needs no auth, which is the usual case for a local vLLM.

Lookup order, highest first:

    1. an explicit --config path
    2. .agent.json / agent.json in the working directory, then each parent
    3. the built-in openai + deepseek profiles
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

CONFIG_FILENAMES = (".agent.json", "agent.json")
PROFILE_FIELDS = ("base_url", "model", "api_key_env", "timeout")

# A profile says which env var to read, never what the key is. Naming these
# explicitly means an attempted inline key produces a clear refusal instead of
# being silently ignored or, worse, honoured.
SECRET_FIELDS = ("api_key", "key", "token", "secret")


class ConfigError(RuntimeError):
    """A config file we could not use. The message says what to fix."""


@dataclass(frozen=True)
class Profile:
    """One reachable model endpoint.

    A value, not a connection: nothing here opens a socket, so a profile can
    be compared and printed without spending anything.
    """

    name: str
    base_url: str
    model: str
    # None means the endpoint takes no auth. "" would mean the same thing, so
    # the distinction is carried here rather than left to a string sentinel.
    api_key_env: str | None = None
    timeout: int | None = None

    def api_key(self) -> str:
        """The key for this profile, or "" when the endpoint needs no auth.

        Reading the environment belongs here rather than at load time so that
        a config loaded once still sees a key exported later.
        """
        if self.api_key_env is None:
            return ""
        return os.environ.get(self.api_key_env, "")

    def describe(self) -> str:
        """One line for --list-profiles, including whether the key is there."""
        if self.api_key_env is None:
            key_state = "no auth needed"
        elif self.api_key():
            key_state = f"{self.api_key_env} is set"
        else:
            key_state = f"{self.api_key_env} is NOT set"
        extra = f" timeout={self.timeout}s" if self.timeout else ""
        return f"{self.base_url}{extra} [{key_state}]"


@dataclass(frozen=True)
class Config:
    """A named set of profiles plus which one to use.

    `profiles` preserves the file's order, which `infer` relies on: with two
    keys exported, the first profile in the file wins, deterministically.
    """

    profiles: dict[str, Profile]
    default: str
    source: str = "(built-in)"
    _names: list[str] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.profiles:
            raise ConfigError(f"{self.source}: no profiles defined, so there is nothing to connect to")
        if self.default not in self.profiles:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise ConfigError(f"{self.source}: default is {self.default!r}, which is not a profile. Known: {known}")
        if not self._names:
            object.__setattr__(self, "_names", sorted(self.profiles))

    def names(self) -> list[str]:
        return list(self._names)

    def get(self, name: str | None) -> Profile:
        """A profile by name, or the default when name is None."""
        if name is None:
            return self.profiles[self.default]
        if name not in self.profiles:
            known = ", ".join(self.names())
            raise ConfigError(
                f"{self.source}: no profile named {name!r}. Known: {known or '(none)'}"
            )
        return self.profiles[name]

    def infer(self) -> Profile:
        """The first profile whose key is set, else the default.

        This is what makes `--provider`/`--profile` optional: export one
        variable and the agent picks the matching endpoint by itself.
        """
        for profile in self.profiles.values():
            if profile.api_key():
                return profile
        return self.profiles[self.default]


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def builtin_config() -> Config:
    """The two endpoints most people reach for, so no file is required."""
    return Config(
        profiles={
            "openai": Profile("openai", "https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
            "deepseek": Profile("deepseek", "https://api.deepseek.com", "deepseek-chat", "DEEPSEEK_API_KEY"),
        },
        default="openai",
        source="built-in profiles",
    )


def find_config(start: str | None = None) -> str | None:
    """Search `start` and each parent for a config file.

    Walking up means running the agent from a subdirectory still finds the
    project's config, which is where a user will have put it.
    """
    here = os.path.abspath(start or os.getcwd())
    while True:
        for filename in CONFIG_FILENAMES:
            candidate = os.path.join(here, filename)
            if os.path.isfile(candidate):
                return candidate
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def load_config(path: str | None = None) -> Config:
    """Read a config file, or return the built-ins when path is None.

    A path that was asked for explicitly and is not there is an error. Only
    the search in `load_config_or_builtin` may quietly fall through.
    """
    if path is None:
        return builtin_config()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from None
    return _parse(raw, path)


def load_config_or_builtin(path: str | None = None) -> Config:
    """Explicit path, else search upward, else the built-in profiles."""
    if path is not None:
        return load_config(path)
    found = find_config()
    return load_config(found) if found else builtin_config()


def _parse(raw: Any, source: str) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: top level must be a JSON object, got {_kind(raw)}")

    profiles_raw = raw.get("profiles")
    if profiles_raw is None:
        raise ConfigError(
            f"{source}: no 'profiles' object. The file needs at least "
            '{"profiles": {"name": {"base_url": ..., "model": ...}}}'
        )
    if not isinstance(profiles_raw, dict):
        raise ConfigError(f"{source}: 'profiles' must be an object, got {_kind(profiles_raw)}")

    profiles = {name: _profile(name, body, source) for name, body in profiles_raw.items()}

    default = raw.get("default")
    if default is None:
        # No stated default: first profile in the file, which JSON preserves.
        default = next(iter(profiles))

    return Config(profiles=profiles, default=default, source=source)


def _profile(name: str, body: Any, source: str) -> Profile:
    where = f"{source}: profile '{name}'"
    if not isinstance(body, dict):
        raise ConfigError(f"{where} must be an object, got {_kind(body)}")

    leaked = sorted(set(body) & set(SECRET_FIELDS))
    if leaked:
        raise ConfigError(
            f"{where} has {leaked}. Secrets do not belong in this file -- it is meant to "
            f"be committed. Name the environment variable instead: \"api_key_env\": \"MY_KEY\"."
        )

    unknown = sorted(set(body) - set(PROFILE_FIELDS))
    if unknown:
        raise ConfigError(f"{where} has unknown field(s) {unknown}. Known: {list(PROFILE_FIELDS)}")

    missing = [f for f in ("base_url", "model") if not body.get(f)]
    if missing:
        raise ConfigError(f"{where} is missing {missing}")

    base_url = str(body["base_url"]).rstrip("/")
    model = str(body["model"])

    api_key_env = body.get("api_key_env")
    if api_key_env is not None and not isinstance(api_key_env, str):
        raise ConfigError(
            f"{where}: api_key_env must be a variable name, or null for an endpoint "
            f"that needs no key; got {_kind(api_key_env)}"
        )

    timeout = body.get("timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError(f"{where}: timeout must be a number of seconds, got {_kind(timeout)}")
        timeout = int(timeout)
        if timeout <= 0:
            raise ConfigError(f"{where}: timeout must be positive, got {timeout}")

    return Profile(
        name=name, base_url=base_url, model=model, api_key_env=api_key_env, timeout=timeout
    )


def _kind(value: Any) -> str:
    return {
        dict: "object", list: "array", str: "string", bool: "boolean",
        int: "number", float: "number", type(None): "null",
    }.get(type(value), type(value).__name__)
