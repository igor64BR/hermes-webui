"""Full Hermes configuration panel (api/full_config.py + /api/full-config/* routes).

Two layers:

* Pure helpers (type coercion, path escaping, description parsing, secret
  masking, env secret classification) and static wiring run in-process.
* Persistence runs against the REAL Hermes Agent writers
  (``set_config_value`` / ``unset_config_value`` / ``save_env_value_secure`` /
  ``remove_env_value``). The agent runs on its own managed interpreter, so the
  scenario executes once in a subprocess with that interpreter and an isolated
  ``HERMES_HOME`` under ``tmp_path``. The agent's launch-time dependency sync is
  blocked inside the subprocess, and the real ``~/.hermes`` config/.env are
  fingerprinted before/after to prove they were not touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api import full_config as fc  # noqa: E402


# ── Pure helpers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "type_name,value,expected",
    [
        ("bool", True, True),
        ("bool", "off", False),
        ("bool", "YES", True),
        ("bool", 1, True),
        ("int", "42", 42),
        ("int", 7.0, 7),
        ("float", 2, 2.0),
        ("float", "0.25", 0.25),
        ("str", "hello", "hello"),
        ("str", 12, "12"),
        ("list", ["a", 1], ["a", 1]),
        ("list", "[1, 2]", [1, 2]),
        ("list", "- x\n- y\n", ["x", "y"]),
        ("dict", {"a": 1}, {"a": 1}),
        ("dict", "a: 1\nb: [2]\n", {"a": 1, "b": [2]}),
        ("any", None, None),
        ("any", "free text", "free text"),
    ],
)
def test_coerce_value_accepts_compatible_values(type_name, value, expected):
    assert fc.coerce_value(type_name, value) == expected


@pytest.mark.parametrize(
    "type_name,value",
    [
        ("bool", "maybe"),
        ("bool", 2),
        ("int", "abc"),
        ("int", True),
        ("int", 1.5),
        ("float", "x"),
        ("float", False),
        ("str", None),
        ("str", ["a"]),
        ("list", {"a": 1}),
        ("list", "a: 1"),
        ("dict", [1]),
        ("dict", "[unclosed"),
    ],
)
def test_coerce_value_rejects_invalid_types(type_name, value):
    with pytest.raises(fc.FullConfigError):
        fc.coerce_value(type_name, value)


def test_cli_text_round_trips_types():
    assert fc._cli_text(True) == "true"
    assert fc._cli_text(None) == "null"
    assert fc._cli_text(3) == "3"
    assert fc._cli_text(2.0) == "2.0"
    assert fc._cli_text(1e-5) == "1e-05"
    assert json.loads(fc._cli_text({"a": [1]})) == {"a": [1]}


def test_dotted_key_paths_escape_and_split():
    path = fc._join_path(("providers", "qwen3.5", "api_key"))
    assert path == "providers.qwen3\\.5.api_key"
    assert fc._split_path(path) == ["providers", "qwen3.5", "api_key"]
    for bad in ("", " agent.x", "agent.", "a..b", "a\nb", 5):
        with pytest.raises(fc.FullConfigError):
            fc._validate_path(bad)


def test_descriptions_come_from_default_config_comments():
    source = textwrap.dedent(
        '''
        DEFAULT_CONFIG = {
            # Top-level toolsets.
            "toolsets": ["hermes-cli"],
            "agent": {
                # Turn cap.
                # null = unlimited.
                "max_turns": None,
                "timeout": 30,  # seconds "#" in text
                "nested": {"x": 1},
            },
            **{"spread": True},
        }
        '''
    )
    desc = fc._parse_descriptions(source)
    assert desc["toolsets"] == "Top-level toolsets."
    assert desc["agent.max_turns"] == "Turn cap. null = unlimited."
    assert desc["agent.timeout"] == 'seconds "#" in text'
    assert "agent.nested.x" not in desc


def test_leaf_iteration_and_custom_keys():
    defaults = {"a": 1, "sec": {"b": True, "empty": {}}, "model": ""}
    leaves = dict(fc._iter_default_leaves(defaults))
    assert set(leaves) == {("a",), ("sec", "b"), ("sec", "empty"), ("model",)}
    user = {"sec": {"b": False, "extra": 3}, "mine": {"deep": {"v": 1}}, "model": {"default": "x"}}
    custom = dict(fc._iter_custom_leaves(user, defaults))
    assert set(custom) == {("sec", "extra"), ("mine", "deep", "v"), ("model", "default")}


def _fake_cfg_mod():
    mod = types.SimpleNamespace()
    mod.seen = []

    def checker(key):
        mod.seen.append(key)
        return key.rsplit(".", 1)[-1].lower().endswith(("api_key", "token", "password"))

    mod._is_secret_config_key = checker
    return mod


def test_masking_covers_cookies_lists_urls_and_non_strings():
    cfg_mod = _fake_cfg_mod()
    secret = "sk-live-abcdefghijklmnopqrstuvwxyz"
    value = {
        "p": {"api_key": secret, "url": "https://x", "env": "${OPENAI_API_KEY}"},
        "list": [{"token": "t0k3n"}],
        "headers": {"Cookie": "sid=COOKIESECRET", "X-Api-Key": "HEADERSECRET"},
        "api_keys": ["LISTSECRET1", "LISTSECRET2"],
        "proxy": "https://user:URLPASSWORD@proxy.example:8080/x",
        "pin": {"password": 123456},
    }
    public, changed = fc._public_value(cfg_mod, ("providers",), value)
    dumped = json.dumps(public)
    assert changed
    for leaked in (secret, "t0k3n", "COOKIESECRET", "HEADERSECRET", "LISTSECRET", "URLPASSWORD", "123456"):
        assert leaked not in dumped, leaked
    assert public["p"]["url"] == "https://x"
    assert public["p"]["env"] == "${OPENAI_API_KEY}"
    assert public["proxy"].startswith("https://user:") and public["proxy"].endswith("@proxy.example:8080/x")
    # The agent classifier receives the full dotted path, not only the leaf.
    assert "providers.p.api_key" in cfg_mod.seen
    # Generic plurals only hide text/lists: numeric counters and flags stay editable.
    counters, _ = fc._public_value(cfg_mod, ("compression",), {"threshold_tokens": 4096, "redact_secrets": True,
                                                                "extra_tokens": ["TOKSECRET"]})
    assert counters["threshold_tokens"] == 4096 and counters["redact_secrets"] is True
    assert "TOKSECRET" not in json.dumps(counters)


def test_masked_echo_restores_secrets_in_place_only():
    cfg_mod = _fake_cfg_mod()
    old = {"p": {"api_key": "sk-live-abcdefghijklmnopqrstuvwxyz", "url": "https://x"},
           "api_keys": ["LISTSECRET-one-0000000000", "LISTSECRET-two-1111111111"],
           "proxy": "https://user:pw@h/"}
    public, _ = fc._public_value(cfg_mod, ("c",), old)
    edited = json.loads(json.dumps(public))
    edited["p"]["url"] = "https://y"
    restored = fc._restore_masked(cfg_mod, edited, old, ("c",), False)
    assert restored["p"]["api_key"] == old["p"]["api_key"] and restored["p"]["url"] == "https://y"
    assert restored["api_keys"] == old["api_keys"] and restored["proxy"] == old["proxy"]
    assert not fc._contains_mask(restored)
    # Reordering moves masks off their positions: they must stay detectable.
    reordered = json.loads(json.dumps(public))
    reordered["api_keys"].reverse()
    assert fc._contains_mask(fc._restore_masked(cfg_mod, reordered, old, ("c",), False))
    renamed = {"q": public["p"]}
    assert fc._contains_mask(fc._restore_masked(cfg_mod, renamed, old, ("c",), False))


def test_yaml_aliases_and_oversized_values_are_rejected():
    bomb = "a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n"
    with pytest.raises(fc.FullConfigError):
        fc.coerce_value("dict", bomb)
    with pytest.raises(fc.FullConfigError):
        fc.coerce_value("dict", "x: &anchor 1\n")
    with pytest.raises(fc.FullConfigError):
        fc.coerce_value("list", "[" + "1," * (fc._MAX_VALUE_CHARS // 2) + "1]")
    deep = cur = []
    for _ in range(fc._MAX_DEPTH + 5):
        cur.append([])
        cur = cur[0]
    with pytest.raises(fc.FullConfigError):
        fc._check_bounded(deep)
    with pytest.raises(fc.FullConfigError):
        fc._check_bounded(list(range(fc._MAX_NODES + 1)))
    fc._check_bounded({"ok": [1, 2, {"x": "y"}]})


def test_agent_refusal_message_excludes_other_stream_output():
    with pytest.raises(fc.FullConfigError) as info:
        with fc._captured_agent_output():
            print("other thread log line with SECRET-ish data")
            print("noise on stderr", file=sys.stderr)
            print("\x1b[31m✗ Invalid config key: 'x'\x1b[0m", file=sys.stderr)
            raise SystemExit(1)
    assert str(info.value) == "✗ Invalid config key: 'x'"
    with pytest.raises(fc.FullConfigError) as info:
        with fc._captured_agent_output():
            print("unrelated output", file=sys.stderr)
            raise SystemExit(1)
    assert "unrelated" not in str(info.value)


def test_named_profile_env_preservation_ignores_mirrored_hermes_home(monkeypatch, tmp_path):
    root, named = tmp_path / "root", tmp_path / "profiles" / "work"
    root.mkdir()
    named.mkdir(parents=True)
    monkeypatch.setattr("api.profiles._DEFAULT_HERMES_HOME", str(root), raising=False)
    # A named-profile stream worker mirrors its home into the process env.
    monkeypatch.setenv("HERMES_HOME", str(named))
    monkeypatch.delenv("FC_TEST_LEAK", raising=False)
    with fc._preserve_process_env_for_named_profile(named):
        os.environ["FC_TEST_LEAK"] = "1"
    assert "FC_TEST_LEAK" not in os.environ
    with fc._preserve_process_env_for_named_profile(root):
        os.environ["FC_TEST_LEAK"] = "1"
    assert os.environ.pop("FC_TEST_LEAK") == "1"  # root profile publishes as the agent intends


def test_list_env_treats_malformed_metadata_as_unknown_secret(monkeypatch):
    cfg_mod = types.SimpleNamespace(load_env=lambda: {"WEIRD_URL": "https://visible?"}, get_env_path=lambda: "/x/.env")
    defaults_mod = types.SimpleNamespace(OPTIONAL_ENV_VARS={"WEIRD_URL": "not-a-dict"})
    monkeypatch.setattr(fc, "_agent_modules", lambda: (cfg_mod, defaults_mod))
    import contextlib

    monkeypatch.setattr(fc, "_active_home_scope", contextlib.nullcontext)
    item = fc.list_env()["categories"][0]["vars"][0]
    assert item["name"] == "WEIRD_URL" and item["secret"] is True and item["value"] is None
    assert "visible" not in json.dumps(item)


def test_env_secret_classification_fails_closed():
    assert fc._env_is_secret("ANYTHING", None) is True  # unknown variable
    assert fc._env_is_secret("OPENROUTER_API_KEY", {"password": True}) is True
    assert fc._env_is_secret("SLACK_BOT_TOKEN", {"password": False}) is True
    assert fc._env_is_secret("NOUS_BASE_URL", {"password": False}) is False
    entry = fc._env_entry("X_TOKEN", None, {"X_TOKEN": "super-secret-value-123456789"})
    assert entry["value"] is None and "super-secret" not in json.dumps(entry)
    plain = fc._env_entry("NOUS_BASE_URL", {"category": "provider"}, {"NOUS_BASE_URL": "https://n"})
    assert plain["value"] == "https://n" and plain["category"] == "provider"


# ── Static wiring ───────────────────────────────────────────────────────────


def test_routes_and_frontend_are_wired():
    routes = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for path in (
        "/api/full-config/schema", "/api/full-config/env", "/api/full-config/set",
        "/api/full-config/reset", "/api/full-config/env/set", "/api/full-config/env/unset",
    ):
        assert f'"{path}"' in routes
    # POST routes are dispatched after the shared CSRF gate in handle_post.
    post_start = routes.index("def handle_post(")
    assert routes.index("_check_csrf(handler)", post_start) < routes.index('"/api/full-config/set"', post_start)

    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert html.count('data-panel="fullconfig"') == 2  # rail + mobile nav
    assert 'id="panelFullconfig"' in html and 'id="mainFullconfig"' in html
    assert 'src="static/full_config.js?v=__WEBUI_VERSION__"' in html
    panels = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "'fullconfig'" in panels and "loadFullConfig" in panels
    css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "main.main.showing-fullconfig > #mainFullconfig" in css
    assert ":not(.showing-fullconfig)" in css

    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    js = (REPO_ROOT / "static" / "full_config.js").read_text(encoding="utf-8")
    import re

    used = set(re.findall(r"t\('((?:fc|tab)_[a-z_]+)'", js)) | set(re.findall(r'data-i18n(?:-placeholder|-title)?="(fc_[a-z_]+|tab_fullconfig)"', html))
    assert used
    for key in used:
        assert i18n.count(f"    {key}:") >= 2, key  # en + pt (all locales carry it)
    assert "fc_filter_changed: 'Só alteradas'" in i18n


# ── Real requests through handle_post: CSRF and origin gate ────────────────


class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _Handler:
    def __init__(self, *, client_ip="127.0.0.1", headers=None, body=None):
        raw = json.dumps(body or {"path": "agent.max_turns", "value": 3}).encode()
        self.client_address = (client_ip, 40000)
        self.headers = _Headers({"Content-Type": "application/json", "Content-Length": str(len(raw)), **(headers or {})})
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.request = None
        self.status = None
        self.sent_headers = []

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8") or "{}")


@pytest.fixture
def routed(monkeypatch):
    from api import routes

    calls = []

    def fake(name):
        def handler_fn(handler, body):
            calls.append((name, body))
            from api.helpers import j

            return j(handler, {"ok": True})
        return handler_fn

    for name in ("handle_set", "handle_reset", "handle_env_set", "handle_env_unset"):
        monkeypatch.setattr(fc, name, fake(name))
    monkeypatch.delenv("HERMES_WEBUI_ONBOARDING_OPEN", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)

    def post(path, handler):
        routes.handle_post(handler, types.SimpleNamespace(path=path, query=""))
        return handler

    return routes, calls, post


@pytest.mark.parametrize("path", ["/api/full-config/set", "/api/full-config/reset",
                                  "/api/full-config/env/set", "/api/full-config/env/unset"])
def test_origin_gate_blocks_remote_writes_when_auth_disabled(routed, monkeypatch, path):
    routes, calls, post = routed
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    h = post(path, _Handler(client_ip="8.8.8.8"))
    assert h.status == 403 and "HERMES_WEBUI_ONBOARDING_OPEN" in h.payload()["error"]
    assert calls == []
    h = post(path, _Handler(client_ip="127.0.0.1"))
    assert h.status == 200 and len(calls) == 1


def test_csrf_rejects_tokenless_and_cross_origin_browser_writes(routed, monkeypatch):
    routes, calls, post = routed
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    same_origin = {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787", "Sec-Fetch-Site": "same-origin"}
    h = post("/api/full-config/set", _Handler(headers=same_origin))
    assert h.status == 403
    h = post("/api/full-config/env/set", _Handler(headers={**same_origin, "Origin": "http://evil.invalid",
                                                           "Sec-Fetch-Site": "cross-site"}))
    assert h.status == 403
    assert calls == []
    monkeypatch.setattr("api.auth.verify_csrf_token", lambda cookie, submitted: submitted == "good")
    h = post("/api/full-config/set", _Handler(headers={**same_origin, "X-CSRF-Token": "good"}))
    assert h.status == 200 and calls and calls[0][0] == "handle_set"


# ── Real-agent persistence (subprocess on the agent interpreter) ────────────


def _agent_dir() -> pathlib.Path | None:
    home = pathlib.Path(os.getenv("HERMES_HOME") or pathlib.Path.home() / ".hermes")
    for cand in (os.getenv("HERMES_WEBUI_AGENT_DIR", ""), str(home / "hermes-agent"),
                 str(REPO_ROOT.parent / "hermes-agent"), str(pathlib.Path.home() / ".hermes" / "hermes-agent")):
        if cand and (pathlib.Path(cand) / "hermes_cli" / "config.py").exists():
            return pathlib.Path(cand).resolve()
    return None


def _agent_python(agent: pathlib.Path, probe_home: pathlib.Path) -> str | None:
    cands = [os.getenv("HERMES_WEBUI_TEST_AGENT_PYTHON"), os.getenv("HERMES_WEBUI_PYTHON"), sys.executable]
    cands += [str(agent / v / "bin" / "python") for v in ("venv", ".venv")]
    try:  # PM-managed installs keep the dependency venv outside the source tree
        probe = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]);"
             "from pathlib import Path; from pm.environments import committed_venv;"
             "e = committed_venv(Path(sys.argv[1])); print(e or '')", str(agent)],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            cands.append(str(pathlib.Path(probe.stdout.strip()) / "bin" / "python"))
    except Exception:
        pass
    env = {**os.environ, "HERMES_HOME": str(probe_home)}
    for cand in cands:
        if not cand or not pathlib.Path(cand).exists():
            continue
        try:
            ok = subprocess.run(
                [cand, "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
                 "import hermes_cli.config, hermes_cli.config_defaults, hermes_constants", str(agent)],
                capture_output=True, text=True, timeout=90, env=env, cwd=str(probe_home),
            )
        except Exception:
            continue
        if ok.returncode == 0:
            return cand
    return None


_SCENARIO = r'''
import difflib, json, os, sys, types
from pathlib import Path
agent_dir, repo_root, home = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, agent_dir); sys.path.insert(0, repo_root)

# Never let an agent import trigger launch-time dependency sync from a test.
import hermes_cli.venv_sync as _vs
def _blocked(*a, **k):
    raise RuntimeError("venv sync blocked in tests")
for _name in ("prepare_launch", "_finish_source_update", "_sync_source_dependencies"):
    if hasattr(_vs, _name):
        setattr(_vs, _name, _blocked)

# Active request profile -> the isolated home (switchable to a named profile).
_state = {"home": home, "name": "default"}
_prof = types.ModuleType("api.profiles")
_prof._DEFAULT_HERMES_HOME = str(home)
_prof.get_active_hermes_home = lambda: _state["home"]
_prof.get_active_profile_name = lambda: _state["name"]
import api
sys.modules["api.profiles"] = _prof

# Capture HTTP responses from the route handlers.
_helpers = types.ModuleType("api.helpers")
_sent = []
def _j(handler, payload, status=200, **kw):
    _sent.append((status, payload)); return True
def _bad(handler, msg, status=400):
    return _j(handler, {"error": msg}, status=status)
_helpers.j, _helpers.bad = _j, _bad
sys.modules["api.helpers"] = _helpers

from api import full_config as fc
from hermes_cli.config_defaults import DEFAULT_CONFIG, OPTIONAL_ENV_VARS

R = {}
cfg_path, env_path = home / "config.yaml", home / ".env"

def leaves(node, prefix=()):
    for k, v in node.items():
        if isinstance(v, dict) and v:
            yield from leaves(v, prefix + (k,))
        else:
            yield prefix + (k,), v

all_leaves = list(leaves(DEFAULT_CONFIG))
R["default_leaf_count"] = len(all_leaves)
R["default_leaf_paths"] = sorted(fc._join_path(p) for p, _ in all_leaves)

def call(handler_fn, *args):
    _sent.clear(); handler_fn(None, *args); return _sent[-1]

status, schema = call(fc.handle_schema)
R["schema_status"] = status
R["schema_json"] = json.dumps(schema, ensure_ascii=False)
keys = {k["path"]: k for s in schema["sections"] for k in s["keys"]}
R["schema_total"] = schema["total"]
R["schema_paths"] = sorted(keys)
R["entry_gateway_timeout"] = keys.get("agent.gateway_timeout")
R["entry_max_turns"] = keys.get("agent.max_turns")
R["entry_custom"] = keys.get("custom_root.nested")
R["entry_model_api_key"] = keys.get("model.api_key")

def pick(t, exclude=()):
    for p, v in all_leaves:
        path = fc._join_path(p)
        if fc._type_name(v) == t and p[0] not in ("model", "terminal", "display") and path not in exclude:
            return path, v
    return None, None

def text():
    return cfg_path.read_text(encoding="utf-8")

def set_(path, value):
    return call(fc.handle_set, {"path": path, "value": value})

before = text()
R["set_int"] = set_("agent.gateway_timeout", "99")
after = text()
sm = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
R["diff_ops"] = [(op, before.splitlines()[i1:i2], after.splitlines()[j1:j2]) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
R["set_int_invalid"] = set_("agent.gateway_timeout", "abc")
R["after_invalid_unchanged"] = text() == after

bool_path, bool_default = pick("bool")
R["bool_path"] = bool_path
R["set_bool"] = set_(bool_path, "on" if not bool_default else "off")
R["set_bool_invalid"] = set_(bool_path, "maybe")
float_path, _ = pick("float")
R["float_path"] = float_path
R["set_float"] = set_(float_path, 3) if float_path else None
list_path, _ = pick("list")
R["list_path"] = list_path
R["set_list"] = set_(list_path, ["alpha", "beta"])
R["set_list_yaml"] = set_(list_path, "- gamma\n")
R["set_list_invalid"] = set_(list_path, {"not": "a list"})
str_path, _ = pick("str")
R["str_path"] = str_path
R["set_str"] = set_(str_path, "true")
R["set_str_invalid"] = set_(str_path, ["x"])
R["set_section"] = set_("agent", 1)
R["set_custom"] = set_("custom_root.nested", "world")
R["set_masked_echo"] = set_("model.api_key", keys["model.api_key"]["value"])

R["schema_profile"] = schema.get("profile")
R["redirect_env_key"] = set_("OPENROUTER_API_KEY", "x")
R["redirect_alias"] = set_("model.api_base", "https://x")
R["wrong_profile"] = call(fc.handle_set, {"path": "agent.gateway_timeout", "value": 5, "profile": "other"})
R["right_profile"] = call(fc.handle_set, {"path": "agent.gateway_timeout", "value": 99, "profile": "default"})

# Item 1: a masked secret that moved must never be written to disk.
cp = keys["custom_providers"]["value"] if "custom_providers" in keys else None
R["cp_public"] = cp
reordered = list(reversed(cp))
R["cp_reorder"] = set_("custom_providers", reordered)
R["cp_after_reorder"] = text()
renamed = [dict(cp[0], name="renamed"), cp[1]]
R["cp_rename_in_place"] = set_("custom_providers", renamed)

# Item 3: reset only accepts leaves.
pre_reset = text()
R["reset_section"] = call(fc.handle_reset, {"path": "agent"})
R["reset_model_mapping"] = call(fc.handle_reset, {"path": "model"})
R["reset_custom_mapping"] = call(fc.handle_reset, {"path": "custom_root"})
R["after_section_resets_unchanged"] = text() == pre_reset

R["reset"] = call(fc.handle_reset, {"path": "agent.max_turns"})
R["reset_again"] = call(fc.handle_reset, {"path": "agent.max_turns"})
R["final_config"] = text()

import yaml
R["final_parsed"] = yaml.safe_load(R["final_config"])

status, env = call(fc.handle_env)
R["env_status"] = status
R["env_json"] = json.dumps(env, ensure_ascii=False)
env_items = {v["name"]: v for c in env["categories"] for v in c["vars"]}
R["env_total"] = env["total"]
R["env_known_count"] = len(OPTIONAL_ENV_VARS)
R["env_openrouter"] = env_items.get("OPENROUTER_API_KEY")
R["env_unknown"] = env_items.get("MY_PRIVATE_THING")
plain = next(n for n, m in OPTIONAL_ENV_VARS.items() if not m.get("password") and not fc._SECRET_ENV_NAME_RE.search(n))
R["env_plain_name"] = plain
R["env_set_plain"] = call(fc.handle_env_set, {"name": plain, "value": "https://plain.example"})
R["env_set_secret"] = call(fc.handle_env_set, {"name": "OPENROUTER_API_KEY", "value": "sk-or-ROTATED-0123456789abcdefghij"})
R["env_file_after_set"] = env_path.read_text(encoding="utf-8")
R["env_unset_plain"] = call(fc.handle_env_unset, {"name": plain})
R["env_unset_missing"] = call(fc.handle_env_unset, {"name": "NEVER_DEFINED_VAR"})
R["env_set_bad_name"] = call(fc.handle_env_set, {"name": "bad-name", "value": "x"})
R["env_set_denied"] = call(fc.handle_env_set, {"name": "PATH", "value": "/tmp"})
R["env_set_multiline"] = call(fc.handle_env_set, {"name": "FOO_X", "value": "a\nb"})
R["env_file_final"] = env_path.read_text(encoding="utf-8")

# Named profile: writes land in the profile home, never the root one, and the
# profile's .env values stay out of the shared process env even while a stream
# worker mirrors that home into os.environ["HERMES_HOME"].
named = home / "profiles" / "work"
named.mkdir(parents=True)
(named / "config.yaml").write_text("# named profile\nagent:\n  gateway_timeout: 7\n", encoding="utf-8")
root_cfg_before, root_env_before = text(), env_path.read_text(encoding="utf-8")
_state.update(home=named, name="work")
os.environ["HERMES_HOME"] = str(named)
os.environ.pop("FC_NAMED_ONLY", None)
R["named_schema_profile"] = call(fc.handle_schema)[1].get("profile")
R["named_set"] = call(fc.handle_set, {"path": "agent.gateway_timeout", "value": 8, "profile": "work"})
R["named_env_set"] = call(fc.handle_env_set, {"name": "FC_NAMED_ONLY", "value": "named-value-123", "profile": "work"})
R["named_stale_profile"] = call(fc.handle_set, {"path": "agent.gateway_timeout", "value": 9, "profile": "default"})
R["named_config"] = (named / "config.yaml").read_text(encoding="utf-8")
R["named_env"] = (named / ".env").read_text(encoding="utf-8")
R["named_env_leaked_to_process"] = "FC_NAMED_ONLY" in os.environ
R["root_untouched"] = text() == root_cfg_before and env_path.read_text(encoding="utf-8") == root_env_before
os.environ["HERMES_HOME"] = str(home)
_state.update(home=home, name="default")
print("@@RESULT@@" + json.dumps(R, default=repr))
'''

_SEED_CONFIG = """\
# top comment: keep me
agent:
  # turn cap comment
  max_turns: 50  # inline comment
  gateway_timeout: 1800
custom_root:
  nested: hello
model:
  default: foo
  api_key: sk-live-CONFIGSECRET-abcdefghijklmnopqrstuvwxyz
custom_providers:
  - name: first
    base_url: https://one.example/v1
    api_key: sk-live-PROVIDERONE-abcdefghijklmnopqrstuvwxyz
  - name: second
    base_url: https://two.example/v1
    api_key: sk-live-PROVIDERTWO-abcdefghijklmnopqrstuvwxyz
"""
_ENV_SECRET = "sk-or-ENVSECRET-0123456789abcdefghijklmnop"
_UNKNOWN_SECRET = "private-UNKNOWNSECRET-value-987654321"


def _fingerprint(paths):
    out = {}
    for p in paths:
        try:
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out[str(p)] = None
    return out


@pytest.fixture(scope="module")
def agent_run(tmp_path_factory):
    agent = _agent_dir()
    if agent is None:
        pytest.skip("hermes-agent source not found")
    probe_home = tmp_path_factory.mktemp("probe-home")
    python = _agent_python(agent, probe_home)
    if python is None:
        pytest.skip("no interpreter can import hermes_cli.config")
    home = tmp_path_factory.mktemp("hermes-home")
    (home / "config.yaml").write_text(_SEED_CONFIG, encoding="utf-8")
    (home / ".env").write_text(
        f"OPENROUTER_API_KEY={_ENV_SECRET}\nMY_PRIVATE_THING={_UNKNOWN_SECRET}\n", encoding="utf-8")
    real_home = pathlib.Path.home() / ".hermes"
    guarded = [real_home / "config.yaml", real_home / ".env"]
    before = _fingerprint(guarded)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env["HERMES_HOME"] = str(home)
    proc = subprocess.run(
        [python, "-c", _SCENARIO, str(agent), str(REPO_ROOT), str(home)],
        capture_output=True, text=True, timeout=240, env=env, cwd=str(home),
    )
    assert _fingerprint(guarded) == before, "real ~/.hermes config/.env changed"
    marker = "@@RESULT@@"
    line = next((l for l in proc.stdout.splitlines() if l.startswith(marker)), None)
    assert proc.returncode == 0 and line, f"scenario failed:\nSTDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-4000:]}"
    return json.loads(line[len(marker):])


def test_schema_covers_every_default_config_leaf(agent_run):
    assert agent_run["schema_status"] == 200
    assert agent_run["schema_total"] >= agent_run["default_leaf_count"]
    assert set(agent_run["default_leaf_paths"]) <= set(agent_run["schema_paths"])


def test_schema_entries_report_type_default_value_and_set_state(agent_run):
    gt = agent_run["entry_gateway_timeout"]
    assert gt["type"] == "int" and gt["is_set"] is True and gt["value"] == 1800 and gt["custom"] is False
    assert gt["description"]  # derived from DEFAULT_CONFIG comments
    mt = agent_run["entry_max_turns"]
    assert mt["is_set"] is True and mt["value"] == 50
    custom = agent_run["entry_custom"]
    assert custom["custom"] is True and custom["value"] == "hello"


def test_schema_never_returns_config_secrets(agent_run):
    for leaked in ("CONFIGSECRET", "PROVIDERONE", "PROVIDERTWO"):
        assert leaked not in agent_run["schema_json"]
    key = agent_run["entry_model_api_key"]
    assert key["secret"] is True and key["is_set"] is True


def test_set_changes_only_the_target_line_and_keeps_comments(agent_run):
    status, payload = agent_run["set_int"]
    assert status == 200 and payload["entry"]["value"] == 99 and payload["entry"]["is_set"]
    assert agent_run["diff_ops"] == [["replace", ["  gateway_timeout: 1800"], ["  gateway_timeout: 99"]]]
    final = agent_run["final_config"]
    for kept in ("# top comment: keep me", "# turn cap comment", "custom_root:", "default: foo"):
        assert kept in final


def test_type_coercion_and_rejection(agent_run):
    assert agent_run["set_int_invalid"][0] == 400
    assert agent_run["after_invalid_unchanged"] is True
    assert agent_run["set_bool"][0] == 200 and isinstance(agent_run["set_bool"][1]["entry"]["value"], bool)
    assert agent_run["set_bool_invalid"][0] == 400
    if agent_run["float_path"]:
        status, payload = agent_run["set_float"]
        assert status == 200 and payload["entry"]["value"] == 3.0
    assert agent_run["set_list"][0] == 200 and agent_run["set_list"][1]["entry"]["value"] == ["alpha", "beta"]
    assert agent_run["set_list_yaml"][1]["entry"]["value"] == ["gamma"]
    assert agent_run["set_list_invalid"][0] == 400
    status, payload = agent_run["set_str"]
    assert status == 200 and payload["entry"]["value"] == "true"  # string stays a string
    assert agent_run["set_str_invalid"][0] == 400
    assert agent_run["set_section"][0] == 400
    assert agent_run["set_custom"][1]["entry"]["value"] == "world"


def test_masked_secret_echo_keeps_the_stored_secret(agent_run):
    assert agent_run["set_masked_echo"][0] == 200
    assert agent_run["final_parsed"]["model"]["api_key"] == "sk-live-CONFIGSECRET-abcdefghijklmnopqrstuvwxyz"
    assert "CONFIGSECRET" not in json.dumps(agent_run["set_masked_echo"])


def test_reset_removes_the_key_from_user_config(agent_run):
    status, payload = agent_run["reset"]
    assert status == 200 and payload["removed"] is True
    assert payload["entry"]["is_set"] is False and payload["entry"]["value"] is None
    assert agent_run["reset_again"][1]["removed"] is False
    assert "max_turns" not in agent_run["final_parsed"]["agent"]
    assert agent_run["final_parsed"]["agent"]["gateway_timeout"] == 99


def test_env_listing_masks_secrets(agent_run):
    assert agent_run["env_status"] == 200
    assert agent_run["env_total"] >= agent_run["env_known_count"]
    for secret in (_ENV_SECRET, _UNKNOWN_SECRET, "ENVSECRET", "UNKNOWNSECRET"):
        assert secret not in agent_run["env_json"]
    orr = agent_run["env_openrouter"]
    assert orr["is_set"] and orr["secret"] and orr["value"] is None and orr["masked"]
    unknown = agent_run["env_unknown"]
    assert unknown["known"] is False and unknown["secret"] is True and unknown["value"] is None


def test_env_set_and_unset_through_agent(agent_run):
    plain = agent_run["env_plain_name"]
    status, payload = agent_run["env_set_plain"]
    assert status == 200 and payload["entry"]["value"] == "https://plain.example"
    status, payload = agent_run["env_set_secret"]
    assert status == 200 and payload["entry"]["is_set"] and "ROTATED" not in json.dumps(payload)
    assert f"{plain}=" in agent_run["env_file_after_set"]
    assert "ROTATED" in agent_run["env_file_after_set"]
    status, payload = agent_run["env_unset_plain"]
    assert status == 200 and payload["removed"] is True and payload["entry"]["is_set"] is False
    assert f"{plain}=" not in agent_run["env_file_final"]
    assert agent_run["env_unset_missing"][1]["removed"] is False
    assert "MY_PRIVATE_THING=" in agent_run["env_file_final"]


def test_env_writes_validate_names_and_values(agent_run):
    assert agent_run["env_set_bad_name"][0] == 400
    assert agent_run["env_set_denied"][0] == 400
    assert agent_run["env_set_multiline"][0] == 400
    assert "PATH=" not in agent_run["env_file_final"]


def test_masked_secret_is_never_written_literally(agent_run):
    assert agent_run["cp_public"] and all("PROVIDER" not in json.dumps(p) for p in agent_run["cp_public"])
    status, payload = agent_run["cp_reorder"]
    assert status == 400 and "masked secret" in payload["error"]
    assert "PROVIDERONE" in agent_run["cp_after_reorder"] and "••" not in agent_run["cp_after_reorder"]
    status, _ = agent_run["cp_rename_in_place"]
    assert status == 200
    providers = agent_run["final_parsed"]["custom_providers"]
    assert providers[0]["name"] == "renamed"
    assert providers[0]["api_key"] == "sk-live-PROVIDERONE-abcdefghijklmnopqrstuvwxyz"
    assert providers[1]["api_key"] == "sk-live-PROVIDERTWO-abcdefghijklmnopqrstuvwxyz"
    assert "••" not in agent_run["final_config"]


def test_reset_refuses_sections_and_mappings(agent_run):
    for key in ("reset_section", "reset_model_mapping", "reset_custom_mapping"):
        assert agent_run[key][0] == 400, key
    assert agent_run["after_section_resets_unchanged"] is True
    assert agent_run["final_parsed"]["model"]["default"] == "foo"
    assert agent_run["final_parsed"]["custom_root"] == {"nested": "world"}


def test_redirected_paths_and_stale_profile_are_refused(agent_run):
    assert agent_run["schema_profile"] == "default"
    assert agent_run["redirect_env_key"][0] == 400
    assert agent_run["redirect_alias"][0] == 400
    assert agent_run["wrong_profile"][0] == 409
    assert agent_run["right_profile"][0] == 200


def test_named_profile_writes_stay_in_the_profile_home(agent_run):
    assert agent_run["named_schema_profile"] == "work"
    assert agent_run["named_set"][0] == 200 and agent_run["named_set"][1]["entry"]["value"] == 8
    assert "gateway_timeout: 8" in agent_run["named_config"] and "# named profile" in agent_run["named_config"]
    assert agent_run["named_env_set"][0] == 200
    assert "FC_NAMED_ONLY=named-value-123" in agent_run["named_env"]
    assert agent_run["named_env_leaked_to_process"] is False
    assert agent_run["named_stale_profile"][0] == 409
    assert agent_run["root_untouched"] is True
