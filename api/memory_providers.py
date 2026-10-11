"""Memory provider settings for the WebUI "Config" panel (Memory tab).

Providers are discovered and loaded with the agent's own plugin loader
(``plugins.memory.discover_memory_providers`` / ``load_memory_provider``, the
same code ``hermes memory setup|status`` uses), so every bundled, user,
project and pip-installed provider shows up without a hardcoded list.

Each provider's fields come from ``MemoryProvider.get_config_schema()`` and are
persisted through the provider's native mechanism:

* non-secret values -> ``provider.save_config(values, hermes_home)`` (merge-write
  into ``mem0.json``, ``honcho.json``, ``config.yaml`` ...). Providers that keep
  the base no-op ``save_config`` read ``memory.<name>`` from config.yaml (the
  agent dashboard's fallback), written key by key with ``set_config_value``;
* secrets -> ``.env`` under the field's ``env_var`` via ``save_env_value_secure``.

State ownership: the active request profile's home is authoritative; every
agent call runs under ``full_config._active_home_scope``. Secret values never
leave the server: only ``is_set`` and a mask are returned.
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from api import full_config as fc
from api.full_config import FullConfigError

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 16_384
_MAX_JSON_BYTES = 2_000_000
_NONE_NAMES = ("", "none")
_KINDS = {
    "text": "text", "string": "text", "str": "text",
    "integer": "integer", "int": "integer",
    "number": "number", "float": "number",
    "boolean": "boolean", "bool": "boolean",
}


# ── Agent access ────────────────────────────────────────────────────────────


def _memory_plugins():
    try:
        import plugins.memory as mem_plugins
        from agent.memory_provider import MemoryProvider, is_core_memory_provider
    except Exception as exc:  # pragma: no cover - depends on installed agent
        raise FullConfigError(f"Hermes Agent memory provider API unavailable: {exc}", 503) from exc
    return mem_plugins, MemoryProvider, is_core_memory_provider


def _load_provider(mem_plugins, name: str):
    try:
        return mem_plugins.load_memory_provider(name, register_skills=False)
    except Exception:
        logger.debug("memory-providers: could not load %s", name, exc_info=True)
        return None


def _raw_schema(provider) -> list[dict]:
    try:
        raw = provider.get_config_schema() if hasattr(provider, "get_config_schema") else []
    except Exception:
        logger.warning("memory-providers: get_config_schema failed for %s", getattr(provider, "name", "?"), exc_info=True)
        return []
    return [f for f in raw or [] if isinstance(f, dict) and str(f.get("key") or "").strip()]


def _overrides_save_config(provider, base) -> bool:
    return type(provider).save_config is not base.save_config


def _active_provider(cfg_mod, is_core) -> str:
    memory = fc._read_user_config(cfg_mod).get("memory")
    name = memory.get("provider") if isinstance(memory, dict) else ""
    name = str(name or "").strip()
    return "" if is_core(name) else name


# ── Fields ──────────────────────────────────────────────────────────────────


def _is_secret_field(raw: dict) -> bool:
    """Schema secrets plus credential-shaped keys/env vars (fail closed)."""
    if raw.get("secret"):
        return True
    key = str(raw.get("key")).lower().replace("-", "_")
    env_var = str(raw.get("env_var") or "")
    return bool(fc._STRONG_SECRET_LEAF_RE.search(key) or (env_var and fc._SECRET_ENV_NAME_RE.search(env_var)))


def _field_kind(raw: dict) -> str:
    if _is_secret_field(raw):
        return "secret"
    if isinstance(raw.get("choices"), list) and raw["choices"]:
        return "select"
    explicit = _KINDS.get(str(raw.get("type") or "").strip().lower())
    if explicit:
        return explicit
    default = raw.get("default")
    if isinstance(default, bool):
        return "boolean"
    if isinstance(default, int):
        return "integer"
    if isinstance(default, float):
        return "number"
    return "text"


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _choice_text(value: Any) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def _read_json(path: Path) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        logger.debug("memory-providers: unreadable %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _native_values(name: str, home: Path, user_config: dict) -> dict:
    """Best-effort read of a provider's stored values across its native stores
    (mirrors the agent dashboard reader: ``<name>.json``, ``<name>/config.json``,
    ``memory.<name>``, legacy ``memory.provider_config``, holographic's plugin block)."""
    values: dict = {}
    for path in (home / f"{name}.json", home / name / "config.json"):
        values.update(_read_json(path))
    memory = user_config.get("memory")
    if isinstance(memory, dict):
        if isinstance(memory.get(name), dict):
            values.update(memory[name])
        if isinstance(memory.get("provider_config"), dict):
            values = {**memory["provider_config"], **values}
    plugins = user_config.get("plugins")
    if name == "holographic" and isinstance(plugins, dict) and isinstance(plugins.get("hermes-memory-store"), dict):
        values.update(plugins["hermes-memory-store"])
    return values


def _env_name_for(provider_name: str, key: str) -> str:
    """Conventional env override name (``RETAINDB_PROJECT``, ``OPENVIKING_ENDPOINT``) for
    fields whose schema declares no ``env_var``."""
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{provider_name}_{key}").strip("_").upper()


def _field_entry(raw: dict, native: dict, env: dict, provider_name: str = "", env_wins: bool = False) -> dict:
    """``env_wins``: the provider reads env before its stored config (providers that keep the
    base ``save_config`` resolve env -> ``memory.<name>`` -> default)."""
    key = str(raw["key"]).strip()
    kind = _field_kind(raw)
    env_var = str(raw.get("env_var") or "")
    choices = [_choice_text(c) for c in raw["choices"]] if kind == "select" else []
    url = str(raw.get("url") or "")
    entry = {
        "key": key,
        "kind": kind,
        "description": str(raw.get("description") or ""),
        "required": bool(raw.get("required")),
        "choices": choices,
        "minimum": _number(raw.get("minimum")),
        "maximum": _number(raw.get("maximum")),
        "step": _number(raw.get("step")),
        "url": url if url.lower().startswith(("https://", "http://")) else "",
        "env_var": env_var,
    }
    native_value = native.get(key)
    env_value = env.get(env_var) if env_var else None
    if kind == "secret":
        stored = env_value or (native_value if isinstance(native_value, (str, int, float)) else None)
        entry.update(
            value=None,
            default=None,
            is_set=bool(stored),
            masked=fc._mask(stored) if stored else "",
            editable=bool(env_var),
        )
        return entry
    override_var = env_var or (_env_name_for(provider_name, key) if provider_name else "")
    override_value = env.get(override_var) if override_var else None
    override_secret = bool(override_var and fc._SECRET_ENV_NAME_RE.search(override_var))
    has_native = native_value is not None and native_value != ""
    env_effective = bool(env_wins and override_value)
    if env_effective:
        value = fc._MASK if override_secret else override_value
    else:
        value = native_value if has_native else (env_value if env_value else raw.get("default"))
    editable = not env_effective
    structured = isinstance(value, (dict, list))
    if kind == "select":
        value = _choice_text(value) if value is not None else ""
    elif kind == "boolean":
        try:
            value = fc.coerce_value("bool", value) if value not in (None, "") else False
        except FullConfigError:
            value = False
    masked = False
    if structured:
        # Mask credential-shaped nested keys before serializing; text round-trips would
        # also change the stored type, so structured values are read-only here.
        public, masked = fc._public(None, value, (key,), False)
        value = json.dumps(public, ensure_ascii=False)
        editable = False
    elif isinstance(value, str):
        value, masked = fc._public(None, value, (key,), False)
    default = raw.get("default")
    entry.update(
        value=value,
        default=_choice_text(default) if kind == "select" and default is not None else default,
        is_set=has_native or bool(env_value) or env_effective,
        env_set=bool(override_value),
        env_override_var=override_var if override_value else "",
        env_effective=env_effective,
        url_masked=masked,
        structured=structured,
        editable=editable,
    )
    return entry


def _provider_entry(mem_plugins, base, name: str, description: str, available: bool,
                    active: str, home: Path, user_config: dict, env: dict) -> dict:
    provider = _load_provider(mem_plugins, name)
    item = {
        "name": name,
        "description": description or "",
        "active": name == active,
        "available": bool(available),
        "loaded": provider is not None,
        "storage": "",
        "fields": [],
    }
    if provider is None:
        return item
    native = _native_values(name, home, user_config)
    item["storage"] = "native" if _overrides_save_config(provider, base) else "config"
    env_wins = item["storage"] == "config"
    item["fields"] = [_field_entry(raw, native, env, name, env_wins) for raw in _raw_schema(provider)]
    return item


def list_providers() -> dict:
    cfg_mod, _ = fc._agent_modules()
    mem_plugins, base, is_core = _memory_plugins()
    with fc._active_home_scope() as home:
        user_config = fc._read_user_config(cfg_mod)
        active = _active_provider(cfg_mod, is_core)
        env = fc._load_env(cfg_mod)
        try:
            discovered = mem_plugins.discover_memory_providers()
        except Exception as exc:
            raise FullConfigError(f"Memory provider discovery failed: {exc}", 500) from exc
        providers = [
            _provider_entry(mem_plugins, base, str(name), str(desc or ""), bool(avail), active, home, user_config, env)
            for name, desc, avail in discovered
        ]
    known = {p["name"] for p in providers}
    return {
        "profile": fc._active_profile_name(),
        "active": active,
        "active_missing": bool(active) and active not in known,
        "managed": bool(fc._safe_call(getattr(cfg_mod, "is_managed", None))),
        "providers": sorted(providers, key=lambda p: (not p["active"], p["name"])),
    }


def _one_provider(name: str) -> dict:
    data = list_providers()
    return next((p for p in data["providers"] if p["name"] == name), None)


# ── Validation ──────────────────────────────────────────────────────────────


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 128 or name != name.strip():
        raise FullConfigError("Invalid provider name")
    if any(c in name for c in "\x00\r\n/\\"):
        raise FullConfigError("Invalid provider name")
    return name


def _coerce_field(entry: dict, raw: dict, value: Any) -> Any:
    key, kind = entry["key"], entry["kind"]
    if kind == "boolean":
        try:
            return fc.coerce_value("bool", value)
        except FullConfigError:
            raise FullConfigError(f"'{key}': expected a boolean") from None
    if kind in ("integer", "number"):
        try:
            number = fc.coerce_value("int" if kind == "integer" else "float", value)
        except FullConfigError:
            raise FullConfigError(f"'{key}': expected {'an integer' if kind == 'integer' else 'a number'}") from None
        if not math.isfinite(number):
            raise FullConfigError(f"'{key}': expected a finite number")
        if entry["minimum"] is not None and number < entry["minimum"]:
            raise FullConfigError(f"'{key}' must be at least {entry['minimum']}")
        if entry["maximum"] is not None and number > entry["maximum"]:
            raise FullConfigError(f"'{key}' must be at most {entry['maximum']}")
        return number
    if kind == "select":
        if isinstance(value, (dict, list)) or value is None:
            raise FullConfigError(f"'{key}': invalid choice")
        text = _choice_text(value)
        for choice in raw["choices"]:
            if _choice_text(choice) == text:
                return choice
        raise FullConfigError(f"'{key}': invalid choice (expected one of {', '.join(entry['choices'])})")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FullConfigError(f"'{key}': expected text")
    text = str(value)
    if len(text) > _MAX_TEXT_CHARS:
        raise FullConfigError(f"'{key}': value too large", 413)
    if any(c in text for c in "\x00\r\n"):
        raise FullConfigError(f"'{key}': value must be a single line")
    return text


def _check_env_writable(cfg_mod, name: str, value: str) -> None:
    """Run the agent's ``.env`` refusals up front so a rejected secret fails the request
    before any non-secret value is written (no partial save)."""
    fc._validate_env_name(name)
    checks = (
        (getattr(cfg_mod, "validate_env_var_name_for_write", None), (name,)),
        (getattr(cfg_mod, "require_env_writable", None), (name, "set")),
        (getattr(cfg_mod, "_check_non_ascii_credential", None), (name, value)),
    )
    for check, args in checks:
        if check is None:
            continue
        try:
            with fc._captured_agent_output():
                check(*args)
        except ValueError as exc:
            raise FullConfigError(fc._ANSI_RE.sub("", str(exc)) or f"Hermes Agent refuses to store {name}") from None


def _plan_writes(entries: dict, raws: dict, values: dict, cfg_mod=None, native_storage: bool = True):
    """Validate every submitted value before any write: (non_secret, resets, secrets).

    ``None`` on a non-secret field restores its default: removed from ``memory.<name>``
    for config-stored providers, or written back as the schema default through the
    provider's own ``save_config`` (which only merges)."""
    non_secret: dict = {}
    resets: list = []
    secrets: dict = {}
    for key, value in values.items():
        entry = entries.get(key)
        if entry is None:
            raise FullConfigError(f"Unknown field: {key}")
        if entry["kind"] == "secret":
            if value is None or value == "" or value == entry["masked"]:
                continue  # empty or unchanged mask: keep the stored secret
            if not isinstance(value, str):
                raise FullConfigError(f"'{key}': expected text")
            if fc._MASK in value:
                raise FullConfigError(f"'{key}': a masked secret cannot be saved; retype the secret value")
            if not entry["editable"]:
                raise FullConfigError(f"'{key}': this provider declares no env var for the secret")
            if any(c in value for c in "\x00\r\n") or len(value) > _MAX_TEXT_CHARS:
                raise FullConfigError(f"'{key}': value must be a single line")
            if cfg_mod is not None:
                _check_env_writable(cfg_mod, entry["env_var"], value)
            secrets[entry["env_var"]] = value
            continue
        if not entry["editable"]:
            if entry.get("env_effective"):
                raise FullConfigError(f"'{key}' is set by {entry['env_override_var']} in .env; edit it in the Environment tab")
            raise FullConfigError(f"'{key}' holds a structured value; edit it in the provider's own config")
        if value is None:
            if not native_storage:
                resets.append(key)
                continue
            default = raws[key].get("default")
            if default is None:
                if entry["kind"] not in ("text", "select"):
                    raise FullConfigError(f"'{key}' has no default to restore")
                default = ""
            non_secret[key] = default
            continue
        if isinstance(value, str) and fc._MASK in value:
            if entry.get("url_masked") and value == entry["value"]:
                continue  # URL with a masked password echoed back unchanged
            raise FullConfigError(f"'{key}': a masked value cannot be saved; retype it")
        non_secret[key] = _coerce_field(entry, raws[key], value)
    return non_secret, resets, secrets


# ── Writes ──────────────────────────────────────────────────────────────────


def set_provider_values(name: Any, values: Any, expected_profile: Any = None) -> dict:
    name = _validate_name(name)
    fc._check_expected_profile(expected_profile)
    if not isinstance(values, dict) or not values:
        raise FullConfigError("values must be a non-empty object")
    if len(values) > 200:
        raise FullConfigError("Too many values", 413)
    cfg_mod, _ = fc._agent_modules()
    fc._check_not_managed(cfg_mod)
    mem_plugins, base, _is_core = _memory_plugins()
    if name not in set(mem_plugins.list_memory_provider_names()):
        raise FullConfigError(f"Unknown memory provider: {name}", 404)

    with fc._write_scope(), fc._active_home_scope() as home:
        provider = _load_provider(mem_plugins, name)
        if provider is None:
            raise FullConfigError(f"Memory provider '{name}' could not be loaded (disabled or failing to import)", 409)
        raws = {str(r["key"]).strip(): r for r in _raw_schema(provider)}
        user_config = fc._read_user_config(cfg_mod)
        native = _native_values(name, home, user_config)
        env = fc._load_env(cfg_mod)
        native_storage = _overrides_save_config(provider, base)
        entries = {key: _field_entry(raw, native, env, name, not native_storage) for key, raw in raws.items()}
        non_secret, resets, secrets = _plan_writes(entries, raws, values, cfg_mod, native_storage)

        with fc._preserve_process_env_for_named_profile(home), fc._captured_agent_output():
            if non_secret and native_storage:
                try:
                    provider.save_config(dict(non_secret), str(home))
                except (FullConfigError, SystemExit):
                    raise
                except Exception as exc:
                    logger.warning("memory-providers: %s save_config failed", name, exc_info=True)
                    raise FullConfigError(f"{name} could not save its configuration: {exc}", 500) from None
            elif non_secret:
                # Typed merge (what ``hermes memory setup`` does) instead of ``config set``,
                # whose text coercion would turn ``project: "12345"`` into an int.
                cfg_mod.save_config({"memory": {name: dict(non_secret)}}, merge_existing=True)
            stored = user_config.get("memory", {}).get(name) if isinstance(user_config.get("memory"), dict) else None
            for key in resets:
                if isinstance(stored, dict) and key in stored:
                    cfg_mod.unset_config_value(fc._join_path(("memory", name, key)))
    # ``set_env`` takes the shared (non-reentrant) WebUI config/.env locks itself.
    for env_var, value in secrets.items():
        fc.set_env(env_var, value)

    return {"ok": True, "provider": _one_provider(name)}


def activate_provider(name: Any, expected_profile: Any = None) -> dict:
    if not isinstance(name, str):
        raise FullConfigError("Invalid provider name")
    fc._check_expected_profile(expected_profile)
    cfg_mod, _ = fc._agent_modules()
    fc._check_not_managed(cfg_mod)
    mem_plugins, _base, is_core = _memory_plugins()
    if name.strip().lower() in _NONE_NAMES:
        target = ""  # ``hermes memory setup`` -> "Built-in only" writes an empty provider
    else:
        target = _validate_name(name)
        if is_core(target):
            target = ""
    with fc._write_scope(), fc._active_home_scope() as home:
        if target and target not in set(mem_plugins.list_memory_provider_names()):
            raise FullConfigError(f"Unknown memory provider: {target}", 404)
        with fc._preserve_process_env_for_named_profile(home), fc._captured_agent_output():
            cfg_mod.set_config_value("memory.provider", target)
    return {"ok": True, **list_providers()}


# ── HTTP handlers ───────────────────────────────────────────────────────────


def handle_list(handler):
    return fc._respond(handler, list_providers)


def handle_set(handler, body: dict):
    body = body if isinstance(body, dict) else {}
    return fc._respond(handler, set_provider_values, body.get("provider"), body.get("values"), body.get("profile"))


def handle_activate(handler, body: dict):
    body = body if isinstance(body, dict) else {}
    return fc._respond(handler, activate_provider, body.get("provider"), body.get("profile"))
