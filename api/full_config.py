"""Full Hermes Agent configuration surface for the WebUI "Config" panel.

Exposes every ``DEFAULT_CONFIG`` leaf (plus user-only keys found in the active
profile's ``config.yaml``) and every known ``.env`` variable, and persists
single-key edits through the agent's own writers:

* ``hermes_cli.config.set_config_value`` / ``unset_config_value`` — the same
  semantics as ``hermes config set|unset``: only the touched key changes and the
  round-trip YAML writer keeps the rest of the file (comments included).
* ``hermes_cli.config.save_env_value_secure`` / ``remove_env_value`` for ``.env``.

State ownership: the active request profile's ``config.yaml`` / ``.env`` are the
authoritative values. Every agent call runs under the agent's context-local
Hermes-home override bound to ``api.profiles.get_active_hermes_home()``, so a
request on a named profile never reads or writes the root profile's files.

Secrets never leave the server in clear text: ``.env`` values are returned only
for variables that are known *and* not credential-shaped (unknown variables
fail closed as secret), and credential-shaped config keys are masked, including
inside list/dict values.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sys
import threading
import tokenize
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_MISSING = object()
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SECRET_ENV_NAME_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|AUTH|CREDENTIAL|PRIVATE|COOKIE|SESSION|SIGNING|WEBHOOK)",
    re.IGNORECASE,
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MASK = "••••••••"
_MAX_VALUE_CHARS = 200_000

# Serializes agent writes and the stdout/stderr capture around them. The agent
# writers report refusals by printing and calling ``sys.exit``; capturing the
# process streams is only safe while no other full-config write is running.
_WRITE_LOCK = threading.RLock()

_DESCRIPTIONS_CACHE: dict[str, Any] = {"key": None, "value": {}}


class FullConfigError(Exception):
    """Client-visible failure with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Agent access ────────────────────────────────────────────────────────────


def _agent_modules():
    try:
        from hermes_cli import config as cfg_mod
        from hermes_cli import config_defaults as defaults_mod
    except Exception as exc:  # pragma: no cover - depends on installed agent
        raise FullConfigError(f"Hermes Agent configuration API unavailable: {exc}", 503) from exc
    return cfg_mod, defaults_mod


def _root_hermes_home() -> Path:
    """The root profile's home, fixed at server start.

    Never ``os.environ["HERMES_HOME"]``: named-profile stream workers mirror
    their home there for the whole run.
    """
    try:
        from api.profiles import _DEFAULT_HERMES_HOME

        return Path(_DEFAULT_HERMES_HOME).expanduser()
    except Exception:
        return Path.home() / ".hermes"


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _active_profile_name() -> str:
    try:
        from api.profiles import get_active_profile_name

        return (get_active_profile_name() or "").strip() or "default"
    except Exception:
        return "default"


def _check_expected_profile(expected: Any) -> None:
    """Refuse a write prepared against another profile's view (stale browser cache)."""
    if expected is None:
        return
    if not isinstance(expected, str) or expected != _active_profile_name():
        raise FullConfigError("The active profile changed; reload the configuration panel", 409)


@contextlib.contextmanager
def _active_home_scope() -> Iterator[Path]:
    """Bind the agent's Hermes home to the active request profile.

    Fails closed: without the agent's context-local override, a request whose
    profile home differs from the process home is refused instead of silently
    editing the wrong profile.
    """
    from api.profiles import get_active_hermes_home

    home = Path(get_active_hermes_home()).expanduser()
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except Exception:
        set_hermes_home_override = reset_hermes_home_override = None
    if set_hermes_home_override is None:
        # Without the context-local override the agent reads os.environ, which a
        # named-profile stream may be mirroring: both must be the root home.
        env_home = Path(os.environ.get("HERMES_HOME", "") or Path.home() / ".hermes").expanduser()
        if not (_same_path(home, _root_hermes_home()) and _same_path(env_home, home)):
            raise FullConfigError(
                "This Hermes Agent cannot scope configuration to the active profile; "
                "switch to the default profile or update the agent.",
                409,
            )
        yield home
        return
    token = set_hermes_home_override(str(home))
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


@contextlib.contextmanager
def _captured_agent_output() -> Iterator[None]:
    """Capture the agent CLI writer's prints and translate ``sys.exit`` refusals.

    The redirect is process-wide, so captured text may include other threads'
    output: it goes to the server log only. The client gets the agent's own
    ``✗`` refusal line or a generic message.
    """
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield
    except SystemExit as exc:
        lines = [ln.strip() for ln in _ANSI_RE.sub("", err.getvalue()).splitlines()]
        refusal = next((ln for ln in reversed(lines) if ln.startswith("✗")), "")
        raise FullConfigError(refusal or f"Hermes Agent refused the change (exit {exc.code})") from None
    finally:
        captured = (_ANSI_RE.sub("", out.getvalue()) + _ANSI_RE.sub("", err.getvalue())).strip()
        if captured:
            logger.info("full-config agent output:\n%s", captured)


@contextlib.contextmanager
def _preserve_process_env_for_named_profile(home: Path) -> Iterator[None]:
    """Agent ``.env`` writers publish to ``os.environ``; keep a named profile's
    values out of the shared server process environment."""
    if _same_path(home, _root_hermes_home()):
        yield
        return
    before = dict(os.environ)
    try:
        yield
    finally:
        for key in set(before) | set(os.environ):
            if key not in before:
                os.environ.pop(key, None)
            elif os.environ.get(key) != before[key]:
                os.environ[key] = before[key]


def _write_scope():
    """Locks shared with other WebUI config/.env writers.

    Looked up in ``sys.modules`` only: importing ``api.streaming`` loads
    ``run_agent`` -> ``hermes_bootstrap``, whose launch preparation may sync
    agent dependencies. In the server both modules are already loaded.
    """
    stack = contextlib.ExitStack()
    stack.enter_context(_WRITE_LOCK)
    for module_name, attr in (("api.config", "_cfg_lock"), ("api.streaming", "_ENV_LOCK")):
        lock = getattr(sys.modules.get(module_name), attr, None)
        if lock is not None:
            stack.enter_context(lock)
    return stack


# ── Paths and types ─────────────────────────────────────────────────────────


def _join_path(parts) -> str:
    return ".".join(str(p).replace(".", "\\.") for p in parts)


def _split_path(path: str) -> list[str]:
    parts, current, i = [], [], 0
    while i < len(path):
        ch = path[i]
        if ch == "\\" and i + 1 < len(path) and path[i + 1] == ".":
            current.append(".")
            i += 2
            continue
        if ch == ".":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _validate_path(path: Any) -> str:
    if not isinstance(path, str) or not path or path != path.strip() or len(path) > 512:
        raise FullConfigError("Invalid config path")
    if any(p == "" for p in _split_path(path)) or any(c in path for c in "\x00\r\n"):
        raise FullConfigError("Invalid config path")
    return path


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "str"


def _get_parts(node: Any, parts) -> Any:
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return _MISSING
    return node


def _iter_default_leaves(node: dict, prefix: tuple = ()) -> Iterator[tuple]:
    for key, value in node.items():
        parts = prefix + (str(key),)
        if isinstance(value, dict) and value:
            yield from _iter_default_leaves(value, parts)
        else:
            yield parts, value


def _iter_custom_leaves(user: Any, defaults: Any, prefix: tuple = ()) -> Iterator[tuple]:
    """User-config leaves with no DEFAULT_CONFIG counterpart."""
    if not isinstance(user, dict):
        return
    for key, value in user.items():
        parts = prefix + (str(key),)
        default = defaults.get(key, _MISSING) if isinstance(defaults, dict) else _MISSING
        if default is _MISSING:
            if isinstance(value, dict) and value:
                yield from _iter_custom_leaves(value, _MISSING, parts)
            else:
                yield parts, value
        elif isinstance(default, dict) and default:
            yield from _iter_custom_leaves(value, default, parts)
        elif isinstance(value, dict) and value and not isinstance(default, dict):
            # A scalar default the user replaced with a mapping (``model: {default: ...}``):
            # the children are the editable keys.
            yield from _iter_custom_leaves(value, _MISSING, parts)


# ── Secret masking ──────────────────────────────────────────────────────────


# Per-process key for mask tags: a mask identifies WHICH stored value it stands
# for (so a secret moved to another position is never restored into it), without
# revealing anything about the value.
_MASK_TAG_KEY = os.urandom(32)


def _mask(value: Any) -> str:
    text = str(value if value is not None else "")
    if not text:
        return ""
    tag = hmac.new(_MASK_TAG_KEY, text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:8]
    tail = text[-4:] if len(text) >= 20 else ""
    return f"{_MASK}{tail}#{tag}"


# Local rules on top of the agent classifier. Strong names are secret whatever
# the value type; generic plurals only when they hold text or lists, so numeric
# counters (``compression.threshold_tokens``) and flags (``redact_secrets``) stay visible.
_STRONG_SECRET_LEAF_RE = re.compile(
    r"(^|_)(cookies?|set_cookie|authorization|api_keys?|access_tokens?|auth_tokens?|refresh_tokens?|"
    r"bearer_tokens?|client_secrets?|private_keys?|passwords?|passphrases?|credentials?)$"
)
_PLURAL_SECRET_LEAF_RE = re.compile(r"(^|_)(keys|tokens|secrets)$")
_URL_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s:]+)(?::([^/@\s]*))?@")


def _is_secret_config_key(cfg_mod, parts, value: Any = _MISSING) -> bool:
    """Credential-shaped config key. The agent classifier sees the FULL dotted path
    (its env-routing branch depends on it); local rules add cookies and plurals."""
    parts = tuple(parts)
    path = ".".join(str(p) for p in parts)
    leaf = str(parts[-1]).lower().replace("-", "_").replace(".", "_")
    checker = getattr(cfg_mod, "_is_secret_config_key", None)
    if checker is not None:
        try:
            if checker(path):
                return True
        except Exception:
            pass
    elif _SECRET_ENV_NAME_RE.search(leaf):
        return True
    if _STRONG_SECRET_LEAF_RE.search(leaf):
        return True
    return isinstance(value, (str, list)) and bool(_PLURAL_SECRET_LEAF_RE.search(leaf))


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value) is not None


def _mask_url_userinfo(text: str) -> str:
    match = _URL_USERINFO_RE.match(text)
    if not match or match.group(3) is None:
        return text
    return f"{match.group(1)}{match.group(2)}:{_mask(match.group(3))}@" + text[match.end():]


def _public(cfg_mod, value: Any, parts: tuple, secret: bool, depth: int = 0) -> tuple[Any, bool]:
    """Client-safe copy of ``value`` at ``parts``. Everything under a secret key is
    masked (list items and nested values included); URL passwords are masked anywhere."""
    if depth > 64:
        return _MASK, True
    if isinstance(value, dict):
        out, changed = {}, False
        for key, item in value.items():
            child = parts + (str(key),)
            out[key], sub = _public(cfg_mod, item, child, secret or _is_secret_config_key(cfg_mod, child, item), depth + 1)
            changed = changed or sub
        return out, changed
    if isinstance(value, list):
        out_list, changed = [], False
        for item in value:
            red, sub = _public(cfg_mod, item, parts, secret, depth + 1)
            out_list.append(red)
            changed = changed or sub
        return out_list, changed
    if isinstance(value, str):
        if not value or _is_placeholder(value):
            return value, False
        if secret:
            return _mask(value), True
        masked = _mask_url_userinfo(value)
        return masked, masked != value
    if secret and isinstance(value, (int, float)) and not isinstance(value, bool):
        return _mask(value), True
    return value, False


def _public_value(cfg_mod, parts: tuple, value: Any) -> tuple[Any, bool]:
    return _public(cfg_mod, value, tuple(parts), _is_secret_config_key(cfg_mod, parts, value))


def _restore_masked(cfg_mod, new: Any, old: Any, parts: tuple, secret: bool, depth: int = 0) -> Any:
    """Put back on-disk secrets the client only ever saw masked (same position only)."""
    if depth > 64 or old is _MISSING:
        return new
    if isinstance(new, dict) and isinstance(old, dict):
        out = {}
        for key, item in new.items():
            child = parts + (str(key),)
            prev = old.get(key, _MISSING)
            out[key] = _restore_masked(cfg_mod, item, prev, child,
                                       secret or _is_secret_config_key(cfg_mod, child, prev), depth + 1)
        return out
    if isinstance(new, list) and isinstance(old, list):
        return [
            _restore_masked(cfg_mod, item, old[i] if i < len(old) else _MISSING, parts, secret, depth + 1)
            for i, item in enumerate(new)
        ]
    if isinstance(new, str) and not isinstance(old, (dict, list)):
        public, changed = _public(cfg_mod, old, parts, secret)
        if changed and new == public:
            return old
    return new


def _contains_mask(value: Any, depth: int = 0) -> bool:
    if depth > 64:
        return True
    if isinstance(value, str):
        return _MASK in value
    if isinstance(value, dict):
        return any(_contains_mask(k, depth + 1) or _contains_mask(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_mask(v, depth + 1) for v in value)
    return False


# ── Descriptions from DEFAULT_CONFIG source comments ────────────────────────


def _clean_comment(text: str) -> str:
    return text.lstrip("#").strip()


def _parse_descriptions(source: str) -> dict[str, str]:
    """Map dotted paths to the comment block above (or trailing) each DEFAULT_CONFIG key."""
    comments: dict[int, str] = {}
    comment_only: set[int] = set()
    first_token_line: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                        tokenize.ENCODING, tokenize.ENDMARKER):
            continue
        line = tok.start[0]
        if tok.type == tokenize.COMMENT:
            comments[line] = _clean_comment(tok.string)
            if line not in first_token_line:
                comment_only.add(line)
        first_token_line.add(line)

    tree = ast.parse(source)
    root = None
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG" for t in targets):
            if isinstance(node.value, ast.Dict):
                root = node.value
    out: dict[str, str] = {}
    if root is None:
        return out

    def walk(dict_node: ast.Dict, prefix: tuple) -> None:
        for key_node, value_node in zip(dict_node.keys, dict_node.values, strict=True):
            if key_node is None:  # ``**spread``
                if isinstance(value_node, ast.Dict):
                    walk(value_node, prefix)
                continue
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            parts = prefix + (key_node.value,)
            lines = []
            line = key_node.lineno - 1
            while line in comment_only:
                lines.append(comments[line])
                line -= 1
            lines.reverse()
            end = getattr(value_node, "end_lineno", None)
            if (not isinstance(value_node, ast.Dict) and end == key_node.lineno
                    and end in comments and end not in comment_only):
                lines.append(comments[end])
            text = " ".join(part for part in lines if part).strip()
            if text:
                out[_join_path(parts)] = text
            if isinstance(value_node, ast.Dict):
                walk(value_node, parts)

    walk(root, ())
    return out


def _default_descriptions(defaults_mod) -> dict[str, str]:
    path = getattr(defaults_mod, "__file__", None)
    if not path:
        return {}
    try:
        stat = os.stat(path)
        key = (path, stat.st_mtime_ns, stat.st_size)
        if _DESCRIPTIONS_CACHE["key"] == key:
            return _DESCRIPTIONS_CACHE["value"]
        source = Path(path).read_text(encoding="utf-8")
        value = _parse_descriptions(source)
    except Exception:
        logger.debug("full-config: could not derive descriptions", exc_info=True)
        return {}
    _DESCRIPTIONS_CACHE.update(key=key, value=value)
    return value


# ── Schema ──────────────────────────────────────────────────────────────────


def _read_user_config(cfg_mod) -> dict:
    reader = getattr(cfg_mod, "read_user_config_raw", None) or cfg_mod.read_raw_config
    try:
        data = reader()
    except Exception as exc:
        raise FullConfigError(f"Could not read config.yaml: {exc}", 500) from exc
    return data if isinstance(data, dict) else {}


def _entry(cfg_mod, parts: tuple, default: Any, user: dict, descriptions: dict, custom: bool) -> dict:
    path = _join_path(parts)
    user_value = _get_parts(user, parts)
    is_set = user_value is not _MISSING
    value = user_value if is_set else default
    declared = _type_name(default) if not custom else _type_name(value)
    value_type = _type_name(value)
    readonly = False
    if declared == "null" and is_set:
        declared_for_ui = value_type
    else:
        declared_for_ui = declared
    if not custom and value_type == "dict" and declared not in ("dict", "null") and value:
        # Scalar default replaced by a mapping; its children are listed as custom keys.
        readonly = True
        declared_for_ui = "dict"
    public_value, value_masked = _public_value(cfg_mod, parts, copy.deepcopy(value))
    public_default, _ = _public_value(cfg_mod, parts, copy.deepcopy(default))
    redirect = _redirect_reason(cfg_mod, path)
    if redirect:
        readonly = True
    return {
        "path": path,
        "section": parts[0] if len(parts) > 1 else "",
        "key": parts[-1],
        "type": declared,
        "widget": declared_for_ui,
        "default": None if custom else public_default,
        "value": public_value,
        "is_set": is_set,
        "custom": custom,
        "secret": value_masked,
        "readonly": readonly,
        "readonly_reason": redirect or "",
        "description": descriptions.get(path, ""),
    }


def build_schema() -> dict:
    cfg_mod, defaults_mod = _agent_modules()
    defaults = getattr(defaults_mod, "DEFAULT_CONFIG", None) or getattr(cfg_mod, "DEFAULT_CONFIG", {})
    descriptions = _default_descriptions(defaults_mod)
    with _active_home_scope():
        user = _read_user_config(cfg_mod)
        config_path = str(cfg_mod.get_config_path())

    entries = [_entry(cfg_mod, parts, default, user, descriptions, False)
               for parts, default in _iter_default_leaves(defaults)]
    entries.extend(_entry(cfg_mod, parts, None, user, descriptions, True)
                   for parts, _ in _iter_custom_leaves(user, defaults))

    sections: dict[str, dict] = {}
    for item in entries:
        name = item["section"]
        sec = sections.get(name)
        if sec is None:
            sec = sections[name] = {
                "name": name,
                "description": descriptions.get(name, "") if name else "",
                "keys": [],
            }
        sec["keys"].append(item)
    ordered = sorted(sections.values(), key=lambda s: (s["name"] != "", s["name"]))
    for sec in ordered:
        sec["changed"] = sum(1 for k in sec["keys"] if k["is_set"])
    return {
        "profile": _active_profile_name(),
        "config_path": config_path,
        "managed": bool(_safe_call(getattr(cfg_mod, "is_managed", None))),
        "total": len(entries),
        "changed": sum(1 for e in entries if e["is_set"]),
        "sections": ordered,
    }


def _safe_call(fn):
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


# ── Coercion ────────────────────────────────────────────────────────────────

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


_MAX_NODES = 20_000
_MAX_DEPTH = 32


def _no_alias_loader():
    import yaml  # PyYAML is a WebUI runtime dependency

    class _NoAliasSafeLoader(yaml.SafeLoader):
        """SafeLoader that refuses anchors/aliases (no "billion laughs" expansion)."""

        def compose_node(self, parent, index):
            if self.check_event(yaml.events.AliasEvent):
                raise yaml.YAMLError("YAML aliases are not allowed")
            event = self.peek_event()
            if getattr(event, "anchor", None):
                raise yaml.YAMLError("YAML anchors are not allowed")
            return super().compose_node(parent, index)

    return _NoAliasSafeLoader


def _check_bounded(value: Any) -> None:
    """Reject containers too large or deep to serialize safely."""
    count, stack = 0, [(value, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > _MAX_NODES or depth > _MAX_DEPTH:
            raise FullConfigError("Value too large or too deeply nested", 413)
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
            stack.extend((k, depth + 1) for k in node.keys())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
        elif isinstance(node, str) and len(node) > _MAX_VALUE_CHARS:
            raise FullConfigError("Value too large", 413)


def _parse_structured(value: str, want: type) -> Any:
    if len(value) > _MAX_VALUE_CHARS:
        raise FullConfigError("Value too large", 413)
    try:
        import yaml

        parsed = yaml.load(value, Loader=_no_alias_loader())  # noqa: S506 - SafeLoader subclass
    except Exception as exc:
        raise FullConfigError(f"Invalid YAML/JSON: {str(exc).splitlines()[0]}") from exc
    if not isinstance(parsed, want):
        raise FullConfigError(f"Expected a {'list' if want is list else 'mapping'}")
    return parsed


def coerce_value(type_name: str, value: Any) -> Any:
    """Validate/coerce a client value against the key's declared type."""
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return value.strip().lower() in _TRUE
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise FullConfigError("Expected a boolean (true/false)")
    if type_name == "int":
        if isinstance(value, bool):
            raise FullConfigError("Expected an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise FullConfigError("Expected an integer")
    if type_name == "float":
        if isinstance(value, bool):
            raise FullConfigError("Expected a number")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise FullConfigError("Expected a number")
    if type_name == "str":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise FullConfigError("Expected text")
    if type_name == "list":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return _parse_structured(value, list)
        raise FullConfigError("Expected a list")
    if type_name == "dict":
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return _parse_structured(value, dict)
        raise FullConfigError("Expected a mapping")
    # null / custom: any JSON value is accepted as-is.
    return value


def _cli_text(value: Any) -> str:
    """Render a typed value as the ``hermes config set`` argument that round-trips to it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(c in text for c in ".eEn") else text + ".0"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ── Config writes ───────────────────────────────────────────────────────────


def _check_not_managed(cfg_mod) -> None:
    if _safe_call(getattr(cfg_mod, "is_managed", None)):
        raise FullConfigError("Configuration is managed by a package manager and cannot be edited here", 409)


def _redirect_reason(cfg_mod, path: str) -> str:
    """Why the agent would store ``path`` somewhere else (``""`` when it would not)."""
    try:
        from hermes_cli.config_env_routing import is_env_setting_key
    except Exception:
        is_env_setting_key = None
    is_env_config_key = getattr(cfg_mod, "_is_env_config_key", None)
    try:
        if (is_env_config_key and is_env_config_key(path)) or (is_env_setting_key and is_env_setting_key(path)):
            return "Stored in .env by Hermes; edit it in the Environment tab"
    except Exception:
        pass
    if path.lower() in ("api_base", "model.api_base"):
        return "Alias of model.base_url; edit model.base_url"
    legacy = getattr(cfg_mod, "_legacy_gateway_platforms_key", None)
    redirect = getattr(cfg_mod, "_redirect_platform_display_key", None)
    try:
        if legacy and legacy(path):
            target = redirect(path)[0] if redirect else ""
            return f"Legacy path; edit {target}" if target and target != path else "Legacy path"
        if redirect:
            target = redirect(path)[0]
            if target != path:
                return f"Canonical path is {target}"
    except Exception:
        pass
    return ""


def _refuse_redirected(cfg_mod, path: str) -> None:
    reason = _redirect_reason(cfg_mod, path)
    if reason:
        raise FullConfigError(reason)


def set_value(path: Any, value: Any, expected_profile: Any = None) -> dict:
    path = _validate_path(path)
    _check_expected_profile(expected_profile)
    cfg_mod, defaults_mod = _agent_modules()
    _check_not_managed(cfg_mod)
    _refuse_redirected(cfg_mod, path)
    defaults = getattr(defaults_mod, "DEFAULT_CONFIG", {})
    parts = tuple(_split_path(path))
    default = _get_parts(defaults, parts)
    if isinstance(default, dict) and default:
        raise FullConfigError("This path is a section; edit its keys individually")

    with _write_scope(), _active_home_scope() as home:
        user = _read_user_config(cfg_mod)
        current = _get_parts(user, parts)
        if default is _MISSING:
            if isinstance(current, dict) and current:
                raise FullConfigError("This path is a section; edit its keys individually")
            type_name = _type_name(current) if current is not _MISSING else "null"
            if type_name == "null":
                type_name = "any"
        else:
            type_name = _type_name(default)
            if type_name == "null":
                type_name = "any"
            elif isinstance(current, dict) and current and type_name != "dict":
                raise FullConfigError("This key holds a mapping; edit its sub-keys individually")
        coerced = coerce_value(type_name, value)
        _check_bounded(coerced)
        coerced = _restore_masked(cfg_mod, coerced, current, parts, _is_secret_config_key(cfg_mod, parts, current))
        if _contains_mask(coerced):
            # A masked secret moved (reordered list, renamed key) or no longer matches
            # the stored value: writing it would replace the secret with the mask.
            raise FullConfigError("A masked secret cannot be saved; retype the secret value")
        text = _cli_text(coerced)
        if len(text) > _MAX_VALUE_CHARS:
            raise FullConfigError("Value too large", 413)
        with _preserve_process_env_for_named_profile(home), _captured_agent_output():
            cfg_mod.set_config_value(path, text)
        user_after = _read_user_config(cfg_mod)
        entry = _entry(
            cfg_mod, parts, None if default is _MISSING else default, user_after,
            _default_descriptions(defaults_mod), default is _MISSING,
        )
        stored = _get_parts(user_after, parts)
    # The agent may canonicalize a write elsewhere; tell the client to reload.
    return {"ok": True, "entry": entry, "stale": stored is _MISSING or stored != coerced}


def reset_value(path: Any, expected_profile: Any = None) -> dict:
    path = _validate_path(path)
    _check_expected_profile(expected_profile)
    cfg_mod, defaults_mod = _agent_modules()
    _check_not_managed(cfg_mod)
    _refuse_redirected(cfg_mod, path)
    defaults = getattr(defaults_mod, "DEFAULT_CONFIG", {})
    parts = tuple(_split_path(path))
    default = _get_parts(defaults, parts)
    if isinstance(default, dict) and default:
        raise FullConfigError("This path is a section; reset its keys individually")
    with _write_scope(), _active_home_scope() as home:
        user = _read_user_config(cfg_mod)
        current = _get_parts(user, parts)
        if isinstance(current, dict) and current and (default is _MISSING or _type_name(default) != "dict"):
            # Only leaves: never drop a whole user mapping (``model: {...}``, custom sections).
            raise FullConfigError("This path holds a mapping; reset its keys individually")
        if current is _MISSING:
            removed = False
        else:
            with _preserve_process_env_for_named_profile(home), _captured_agent_output():
                cfg_mod.unset_config_value(path)
            removed = True
        user_after = _read_user_config(cfg_mod)
        if default is _MISSING:
            entry = None
        else:
            entry = _entry(cfg_mod, parts, default, user_after, _default_descriptions(defaults_mod), False)
    return {"ok": True, "removed": removed, "entry": entry}


# ── .env ────────────────────────────────────────────────────────────────────


def _validate_env_name(name: Any) -> str:
    if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
        raise FullConfigError("Invalid environment variable name")
    return name


def _env_is_secret(name: str, meta: dict | None) -> bool:
    if meta is None:
        return True  # unknown variables fail closed
    if meta.get("password"):
        return True
    return bool(_SECRET_ENV_NAME_RE.search(name))


def _env_entry(name: str, meta: dict | None, env: dict) -> dict:
    secret = _env_is_secret(name, meta)
    raw = env.get(name)
    is_set = raw is not None
    item = {
        "name": name,
        "known": meta is not None,
        "category": (meta or {}).get("category") or "other",
        "description": (meta or {}).get("description") or "",
        "prompt": (meta or {}).get("prompt") or "",
        "help": (meta or {}).get("help") or "",
        "url": (meta or {}).get("url") or "",
        "advanced": bool((meta or {}).get("advanced")),
        "tools": list((meta or {}).get("tools") or []),
        "secret": secret,
        "is_set": is_set,
    }
    if secret:
        item["value"] = None
        item["masked"] = _mask(raw) if is_set else ""
    else:
        item["value"] = raw if is_set else ""
    return item


def _optional_env_vars(cfg_mod, defaults_mod) -> dict:
    data = getattr(defaults_mod, "OPTIONAL_ENV_VARS", None) or getattr(cfg_mod, "OPTIONAL_ENV_VARS", {})
    return data if isinstance(data, dict) else {}


def _load_env(cfg_mod) -> dict:
    try:
        data = cfg_mod.load_env()
    except Exception as exc:
        raise FullConfigError(f"Could not read .env: {exc}", 500) from exc
    return {str(k): str(v) for k, v in (data or {}).items()}


def list_env() -> dict:
    cfg_mod, defaults_mod = _agent_modules()
    known = _optional_env_vars(cfg_mod, defaults_mod)
    with _active_home_scope():
        env = _load_env(cfg_mod)
        env_path = str(cfg_mod.get_env_path())
    items = [_env_entry(name, meta if isinstance(meta, dict) else None, env) for name, meta in known.items()]
    items.extend(_env_entry(name, None, env) for name in sorted(env) if name not in known)
    categories: dict[str, list] = {}
    for item in items:
        categories.setdefault(item["category"], []).append(item)
    return {
        "profile": _active_profile_name(),
        "env_path": env_path,
        "total": len(items),
        "set": sum(1 for i in items if i["is_set"]),
        "categories": [{"name": name, "vars": vars_} for name, vars_ in sorted(categories.items())],
    }


def set_env(name: Any, value: Any, expected_profile: Any = None) -> dict:
    name = _validate_env_name(name)
    _check_expected_profile(expected_profile)
    if not isinstance(value, str):
        raise FullConfigError("Value must be text")
    if not value:
        raise FullConfigError("Value is empty; use unset to remove the variable")
    if any(c in value for c in "\x00\r\n"):
        raise FullConfigError("Value must be a single line")
    if len(value) > 16_384:
        raise FullConfigError("Value too large", 413)
    cfg_mod, defaults_mod = _agent_modules()
    _check_not_managed(cfg_mod)
    known = _optional_env_vars(cfg_mod, defaults_mod)
    with _write_scope(), _active_home_scope() as home:
        with _preserve_process_env_for_named_profile(home), _captured_agent_output():
            try:
                cfg_mod.save_env_value_secure(name, value)
            except ValueError as exc:
                raise FullConfigError(_ANSI_RE.sub("", str(exc))) from None
        env = _load_env(cfg_mod)
    if env.get(name) is None:
        raise FullConfigError("Hermes Agent refused to store this variable")
    meta = known.get(name)
    return {"ok": True, "entry": _env_entry(name, meta if isinstance(meta, dict) else None, env)}


def unset_env(name: Any, expected_profile: Any = None) -> dict:
    name = _validate_env_name(name)
    _check_expected_profile(expected_profile)
    cfg_mod, defaults_mod = _agent_modules()
    _check_not_managed(cfg_mod)
    known = _optional_env_vars(cfg_mod, defaults_mod)
    with _write_scope(), _active_home_scope() as home:
        with _preserve_process_env_for_named_profile(home), _captured_agent_output():
            require = getattr(cfg_mod, "require_env_writable", None)
            try:
                if require is not None:
                    require(name, "remove")
                removed = bool(cfg_mod.remove_env_value(name))
            except ValueError as exc:
                raise FullConfigError(_ANSI_RE.sub("", str(exc))) from None
        env = _load_env(cfg_mod)
    meta = known.get(name)
    return {
        "ok": True,
        "removed": removed,
        "entry": _env_entry(name, meta if isinstance(meta, dict) else None, env),
    }


# ── HTTP handlers ───────────────────────────────────────────────────────────


def _respond(handler, fn, *args):
    from api.helpers import bad, j

    try:
        return j(handler, fn(*args))
    except FullConfigError as exc:
        return bad(handler, str(exc), exc.status)
    except Exception:
        logger.exception("full-config request failed")
        return bad(handler, "Configuration request failed", 500)


def handle_schema(handler):
    return _respond(handler, build_schema)


def handle_set(handler, body: dict):
    if not isinstance(body, dict) or "value" not in body:
        from api.helpers import bad

        return bad(handler, "Missing required field(s): path, value")
    return _respond(handler, set_value, body.get("path"), body.get("value"), body.get("profile"))


def handle_reset(handler, body: dict):
    body = body if isinstance(body, dict) else {}
    return _respond(handler, reset_value, body.get("path"), body.get("profile"))


def handle_env(handler):
    return _respond(handler, list_env)


def handle_env_set(handler, body: dict):
    body = body if isinstance(body, dict) else {}
    return _respond(handler, set_env, body.get("name"), body.get("value"), body.get("profile"))


def handle_env_unset(handler, body: dict):
    body = body if isinstance(body, dict) else {}
    return _respond(handler, unset_env, body.get("name"), body.get("profile"))
