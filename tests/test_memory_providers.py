"""Memory provider settings (api/memory_providers.py + /api/full-config/memory-providers*).

* Route wiring, CSRF and the local-origin gate run in-process through
  ``routes.handle_post`` with the handlers stubbed.
* Discovery, schema reads and persistence run against the REAL Hermes Agent
  plugin loader and provider ``save_config`` writers, in one subprocess on the
  agent interpreter with an isolated ``HERMES_HOME`` under tmp (same harness as
  ``test_full_config``). The real ``~/.hermes`` files are fingerprinted
  before/after to prove they were not touched.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api import memory_providers as mp  # noqa: E402
from tests.test_full_config import _agent_dir, _agent_python, _fingerprint, _Handler  # noqa: E402


# ── Wiring, CSRF and origin gate ────────────────────────────────────────────


def test_routes_and_frontend_are_wired():
    routes = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for path in ("/api/full-config/memory-providers", "/api/full-config/memory-providers/set",
                 "/api/full-config/memory-providers/activate"):
        assert f'"{path}"' in routes
    post_start = routes.index("def handle_post(")
    assert routes.index("_check_csrf(handler)", post_start) < routes.index(
        '"/api/full-config/memory-providers/set"', post_start)
    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-fc-tab="memory"' in html
    js = (REPO_ROOT / "static" / "full_config.js").read_text(encoding="utf-8")
    assert "/api/full-config/memory-providers" in js
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    import re

    used = set(re.findall(r"t\('(fc_mp_[a-z_]+|fc_tab_memory)'", js)) | {"fc_tab_memory"}
    assert len(used) > 5
    for key in used:
        assert i18n.count(f"    {key}:") >= 2, key  # en + pt


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

    for name in ("handle_set", "handle_activate"):
        monkeypatch.setattr(mp, name, fake(name))
    monkeypatch.delenv("HERMES_WEBUI_ONBOARDING_OPEN", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)

    def post(path, handler):
        routes.handle_post(handler, types.SimpleNamespace(path=path, query=""))
        return handler

    return calls, post


_BODY = {"provider": "mem0", "values": {"user_id": "x"}}


@pytest.mark.parametrize("path", ["/api/full-config/memory-providers/set",
                                  "/api/full-config/memory-providers/activate"])
def test_origin_gate_blocks_remote_writes_when_auth_disabled(routed, monkeypatch, path):
    calls, post = routed
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    h = post(path, _Handler(client_ip="8.8.8.8", body=_BODY))
    assert h.status == 403 and "HERMES_WEBUI_ONBOARDING_OPEN" in h.payload()["error"]
    assert calls == []
    h = post(path, _Handler(client_ip="127.0.0.1", body=_BODY))
    assert h.status == 200 and len(calls) == 1


def test_csrf_rejects_tokenless_and_cross_origin_writes(routed, monkeypatch):
    calls, post = routed
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    same_origin = {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787", "Sec-Fetch-Site": "same-origin"}
    assert post("/api/full-config/memory-providers/set", _Handler(headers=same_origin, body=_BODY)).status == 403
    cross = {**same_origin, "Origin": "http://evil.invalid", "Sec-Fetch-Site": "cross-site"}
    assert post("/api/full-config/memory-providers/activate", _Handler(headers=cross, body=_BODY)).status == 403
    assert calls == []
    monkeypatch.setattr("api.auth.verify_csrf_token", lambda cookie, submitted: submitted == "good")
    h = post("/api/full-config/memory-providers/set", _Handler(headers={**same_origin, "X-CSRF-Token": "good"}, body=_BODY))
    assert h.status == 200 and calls[0][0] == "handle_set"


# ── Pure validation ─────────────────────────────────────────────────────────


def test_field_kinds_and_secret_classification():
    assert mp._field_kind({"key": "api_key", "secret": True}) == "secret"
    assert mp._field_kind({"key": "api_key"}) == "secret"  # credential-shaped key fails closed
    assert mp._field_kind({"key": "x", "env_var": "FOO_TOKEN"}) == "secret"
    assert mp._field_kind({"key": "rerank", "choices": ["true", "false"]}) == "select"
    assert mp._field_kind({"key": "n", "type": "integer"}) == "integer"
    assert mp._field_kind({"key": "n", "default": 0.5}) == "number"
    assert mp._field_kind({"key": "flag", "default": False}) == "boolean"
    assert mp._field_kind({"key": "host"}) == "text"


# ── Real agent (subprocess on the agent interpreter) ────────────────────────


_SCENARIO = r'''
import difflib, json, os, sys, types
from pathlib import Path
agent_dir, repo_root, home = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, agent_dir); sys.path.insert(0, repo_root)

import hermes_cli.venv_sync as _vs
def _blocked(*a, **k):
    raise RuntimeError("venv sync blocked in tests")
for _name in ("prepare_launch", "_finish_source_update", "_sync_source_dependencies"):
    if hasattr(_vs, _name):
        setattr(_vs, _name, _blocked)

_prof = types.ModuleType("api.profiles")
_prof._DEFAULT_HERMES_HOME = str(home)
_prof.get_active_hermes_home = lambda: home
_prof.get_active_profile_name = lambda: "default"
import api
sys.modules["api.profiles"] = _prof

_helpers = types.ModuleType("api.helpers")
_sent = []
def _j(handler, payload, status=200, **kw):
    _sent.append((status, payload)); return True
def _bad(handler, msg, status=400):
    return _j(handler, {"error": msg}, status=status)
_helpers.j, _helpers.bad = _j, _bad
sys.modules["api.helpers"] = _helpers

from api import memory_providers as mp

R = {"responses": []}
cfg_path, env_path, mem0_path = home / "config.yaml", home / ".env", home / "mem0.json"

def call(fn, *args):
    _sent.clear(); fn(None, *args); R["responses"].append(_sent[-1]); return _sent[-1]

def set_(provider, values, **extra):
    return call(mp.handle_set, {"provider": provider, "values": values, **extra})

status, listing = call(mp.handle_list)
R["list_status"] = status
R["names"] = [p["name"] for p in listing["providers"]]
R["active_initial"] = listing["active"]
R["profile"] = listing["profile"]
by = {p["name"]: p for p in listing["providers"]}
R["mem0"] = by.get("mem0")
R["honcho"] = by.get("honcho")
mask = next(f for f in by["mem0"]["fields"] if f["key"] == "api_key")["masked"]

env_before = env_path.read_text(encoding="utf-8")
R["set_mem0"] = set_("mem0", {"user_id": "user-two", "rerank": "true", "api_key": mask})
R["mem0_json_after_set"] = json.loads(mem0_path.read_text(encoding="utf-8"))
R["env_unchanged_after_mask"] = env_path.read_text(encoding="utf-8") == env_before
R["set_empty_secret"] = set_("mem0", {"api_key": ""})
R["env_unchanged_after_empty"] = env_path.read_text(encoding="utf-8") == env_before
R["set_secret"] = set_("mem0", {"api_key": "m0-ROTATEDSECRET-0123456789abcdef"})
R["env_after_secret"] = env_path.read_text(encoding="utf-8")
R["set_other_mask"] = set_("mem0", {"api_key": "••••••••#deadbeef"})

R["bad_choice"] = set_("mem0", {"rerank": "maybe"})
R["bad_field"] = set_("mem0", {"not_a_field": "x"})
R["bad_text_type"] = set_("mem0", {"user_id": ["x"]})
R["bad_provider"] = set_("does-not-exist", {"a": 1})
R["wrong_profile"] = set_("mem0", {"user_id": "z"}, profile="other")
R["mem0_json_after_rejections"] = json.loads(mem0_path.read_text(encoding="utf-8"))
if "openviking" in R["names"]:
    R["ov_bad_int"] = set_("openviking", {"recall_limit": "abc"})
    R["ov_over_max"] = set_("openviking", {"recall_limit": 1000})
    R["ov_bad_bool"] = set_("openviking", {"recall_resources": "maybe"})

# Finding 1: config-stored provider keeps text values as text (no CLI coercion).
import yaml
R["rdb_listing"] = next(p for p in call(mp.handle_list)[1]["providers"] if p["name"] == "retaindb")
R["rdb_set_numeric_text"] = set_("retaindb", {"project": "12345"})
R["rdb_project_stored"] = (yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["memory"].get("retaindb") or {}).get("project")
# Finding 5: RETAINDB_BASE_URL in .env wins at runtime -> shown as effective and read-only.
R["rdb_set_env_shadowed"] = set_("retaindb", {"base_url": "https://config.example"})
# Finding 4: null restores the default (config store: key removed; native store: default written).
R["rdb_reset"] = set_("retaindb", {"project": None})
R["rdb_after_reset"] = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["memory"].get("retaindb") or {}
R["mem0_reset"] = set_("mem0", {"user_id": None})
R["mem0_json_after_reset"] = json.loads(mem0_path.read_text(encoding="utf-8"))
# Finding 2: a secret the agent refuses fails the whole request before any write.
R["refused_env_precheck"] = None
import hermes_cli.config as _cfg
try:
    mp._check_env_writable(_cfg, "PATH", "x")
except Exception as exc:
    R["refused_env_precheck"] = type(exc).__name__
from plugins.memory.mem0 import Mem0MemoryProvider
_orig_schema = Mem0MemoryProvider.get_config_schema
Mem0MemoryProvider.get_config_schema = lambda self: _orig_schema(self) + [
    {"key": "path_secret", "description": "x", "secret": True, "env_var": "PATH"}]
mem0_before_partial = mem0_path.read_text(encoding="utf-8")
R["partial_write"] = set_("mem0", {"user_id": "should-not-land", "path_secret": "x"})
R["mem0_unchanged_after_refusal"] = mem0_path.read_text(encoding="utf-8") == mem0_before_partial
Mem0MemoryProvider.get_config_schema = _orig_schema

cfg_before = cfg_path.read_text(encoding="utf-8")
R["activate"] = call(mp.handle_activate, {"provider": "mem0"})
cfg_after = cfg_path.read_text(encoding="utf-8")
sm = difflib.SequenceMatcher(None, cfg_before.splitlines(), cfg_after.splitlines())
R["activate_diff"] = [(op, cfg_before.splitlines()[i1:i2], cfg_after.splitlines()[j1:j2])
                      for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
R["activate_unknown"] = call(mp.handle_activate, {"provider": "does-not-exist"})
R["activate_wrong_profile"] = call(mp.handle_activate, {"provider": "honcho", "profile": "other"})
R["deactivate"] = call(mp.handle_activate, {"provider": "none"})
import yaml
R["provider_after_deactivate"] = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["memory"]["provider"]
R["final_config"] = cfg_path.read_text(encoding="utf-8")
print("@@RESULT@@" + json.dumps(R, default=repr))
'''

_SEED_CONFIG = """\
# top comment: keep me
memory:
  provider: ''
  memory_enabled: true
agent:
  max_turns: 50  # inline comment
"""
_MEM0_SECRET = "m0-ORIGINALSECRET-0123456789abcdefghij"
_HONCHO_SECRET = "hch-HONCHOSECRET-0123456789abcdefghij"
_SECRETS = (_MEM0_SECRET, _HONCHO_SECRET, "m0-ROTATEDSECRET-0123456789abcdef")


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
    (home / ".env").write_text(f"MEM0_API_KEY={_MEM0_SECRET}\nHONCHO_API_KEY={_HONCHO_SECRET}\n"
                               "RETAINDB_BASE_URL=https://env.example\n", encoding="utf-8")
    (home / "mem0.json").write_text(json.dumps({"user_id": "user-one", "keep_me": "untouched", "oss": {"a": 1}}),
                                    encoding="utf-8")
    real_home = pathlib.Path.home() / ".hermes"
    guarded = [real_home / "config.yaml", real_home / ".env", real_home / "mem0.json", real_home / "honcho.json"]
    before = _fingerprint(guarded)
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV") and not k.startswith(("MEM0_", "HONCHO_", "RETAINDB_"))}
    env["HERMES_HOME"] = str(home)
    proc = subprocess.run(
        [python, "-c", _SCENARIO, str(agent), str(REPO_ROOT), str(home)],
        capture_output=True, text=True, timeout=240, env=env, cwd=str(home),
    )
    assert _fingerprint(guarded) == before, "real ~/.hermes files changed"
    marker = "@@RESULT@@"
    line = next((l for l in proc.stdout.splitlines() if l.startswith(marker)), None)
    assert proc.returncode == 0 and line, f"scenario failed:\nSTDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-4000:]}"
    return json.loads(line[len(marker):])


def _field(provider, key):
    return next(f for f in provider["fields"] if f["key"] == key)


def test_listing_discovers_providers_with_fields(agent_run):
    assert agent_run["list_status"] == 200
    assert {"mem0", "honcho"} <= set(agent_run["names"])
    assert agent_run["active_initial"] == "" and agent_run["profile"] == "default"
    mem0 = agent_run["mem0"]
    assert mem0["active"] is False and mem0["loaded"] is True and mem0["storage"] == "native"
    assert _field(mem0, "user_id")["value"] == "user-one" and _field(mem0, "user_id")["is_set"] is True
    assert _field(mem0, "agent_id")["value"] == "hermes"  # schema default
    rerank = _field(mem0, "rerank")
    assert rerank["kind"] == "select" and rerank["choices"] == ["true", "false"]
    key = _field(mem0, "api_key")
    assert key["kind"] == "secret" and key["is_set"] is True and key["value"] is None
    assert key["masked"] and key["env_var"] == "MEM0_API_KEY" and key["url"].startswith("https://")
    assert _field(agent_run["honcho"], "api_key")["is_set"] is True


def test_set_merges_native_json_and_keeps_unlisted_keys(agent_run):
    status, payload = agent_run["set_mem0"]
    assert status == 200 and payload["ok"] is True
    stored = agent_run["mem0_json_after_set"]
    assert stored["user_id"] == "user-two" and stored["rerank"] == "true"
    assert stored["keep_me"] == "untouched" and stored["oss"] == {"a": 1}
    assert "api_key" not in stored  # secrets never go to the native file
    assert _field(payload["provider"], "user_id")["value"] == "user-two"


def test_unchanged_mask_and_empty_secret_keep_the_stored_secret(agent_run):
    assert agent_run["env_unchanged_after_mask"] is True
    assert agent_run["set_empty_secret"][0] == 200 and agent_run["env_unchanged_after_empty"] is True
    assert agent_run["set_other_mask"][0] == 400  # a foreign mask is never written


def test_secret_goes_to_env_and_never_comes_back_in_clear(agent_run):
    status, payload = agent_run["set_secret"]
    assert status == 200
    assert "MEM0_API_KEY=m0-ROTATEDSECRET-0123456789abcdef" in agent_run["env_after_secret"]
    assert _field(payload["provider"], "api_key")["is_set"] is True
    dump = json.dumps(agent_run["responses"], ensure_ascii=False)
    for secret in _SECRETS:
        assert secret not in dump


def test_invalid_values_are_rejected_without_writing(agent_run):
    for key in ("bad_choice", "bad_field", "bad_text_type"):
        assert agent_run[key][0] == 400, key
    assert agent_run["bad_provider"][0] == 404
    assert agent_run["wrong_profile"][0] == 409
    assert agent_run["mem0_json_after_rejections"]["user_id"] == "user-two"
    for key in ("ov_bad_int", "ov_over_max", "ov_bad_bool"):
        if key in agent_run:
            assert agent_run[key][0] == 400, key


def test_activate_changes_only_memory_provider(agent_run):
    status, payload = agent_run["activate"]
    assert status == 200 and payload["active"] == "mem0"
    assert next(p for p in payload["providers"] if p["name"] == "mem0")["active"] is True
    diff = agent_run["activate_diff"]
    assert len(diff) == 1 and diff[0][0] == "replace"
    assert [l.strip() for l in diff[0][1]] == ["provider: ''"]
    assert [l.strip() for l in diff[0][2]] in (["provider: mem0"], ["provider: 'mem0'"])  # quote style kept
    assert agent_run["activate_unknown"][0] == 404
    assert agent_run["activate_wrong_profile"][0] == 409
    assert agent_run["deactivate"][0] == 200 and agent_run["deactivate"][1]["active"] == ""
    assert agent_run["provider_after_deactivate"] in ("", None)
    final = agent_run["final_config"]
    assert "# top comment: keep me" in final and "max_turns: 50  # inline comment" in final


# ── Review findings ─────────────────────────────────────────────────────────


def test_config_stored_text_keeps_its_type(agent_run):
    """Finding 1: ``project: "12345"`` must stay a string (retaindb ignores non-strings)."""
    assert agent_run["rdb_listing"]["storage"] == "config"
    assert agent_run["rdb_set_numeric_text"][0] == 200
    assert agent_run["rdb_project_stored"] == "12345"


def test_refused_secret_fails_before_any_write(agent_run):
    """Finding 2: env refusals are checked before the non-secret write (no partial save)."""
    assert agent_run["refused_env_precheck"] == "FullConfigError"
    assert agent_run["partial_write"][0] == 400
    assert agent_run["mem0_unchanged_after_refusal"] is True


def test_env_var_prechecks_without_agent():
    class _Cfg:
        @staticmethod
        def require_env_writable(name, action):
            raise ValueError(f"{name} is locked")

    entries = {"k": {"key": "k", "kind": "secret", "masked": "", "editable": True, "env_var": "GOOD_KEY"}}
    with pytest.raises(mp.FullConfigError, match="locked"):
        mp._plan_writes(entries, {"k": {}}, {"k": "v"}, _Cfg())
    entries["k"]["env_var"] = "bad-name"
    with pytest.raises(mp.FullConfigError):
        mp._plan_writes(entries, {"k": {}}, {"k": "v"}, types.SimpleNamespace())


def test_structured_values_are_masked_and_read_only():
    """Finding 3: nested credential keys are masked before serializing; no text round-trip."""
    raw = {"key": "oss", "description": "OSS backend"}
    entry = mp._field_entry(raw, {"oss": {"llm": {"api_key": "sk-NESTEDSECRET-0123456789abcdef"}, "n": 1}}, {})
    assert "NESTEDSECRET" not in json.dumps(entry)
    assert entry["structured"] is True and entry["editable"] is False and '"n": 1' in entry["value"]
    with pytest.raises(mp.FullConfigError):
        mp._plan_writes({"oss": entry}, {"oss": raw}, {"oss": "{}"})


def test_null_restores_default(agent_run):
    """Finding 4: clearing a stored field restores its default in either store."""
    assert agent_run["rdb_reset"][0] == 200
    assert "project" not in agent_run["rdb_after_reset"]
    assert agent_run["mem0_reset"][0] == 200
    assert agent_run["mem0_json_after_reset"]["user_id"] == "hermes-user"
    assert agent_run["mem0_json_after_reset"]["keep_me"] == "untouched"
    js = (REPO_ROOT / "static" / "full_config.js").read_text(encoding="utf-8")
    assert "if (f.is_set) values[f.key] = null;" in js


def test_reset_without_default_is_refused_for_numbers():
    entry = {"key": "n", "kind": "integer", "editable": True}
    with pytest.raises(mp.FullConfigError, match="no default"):
        mp._plan_writes({"n": entry}, {"n": {"key": "n"}}, {"n": None})


def test_env_override_is_shown_as_effective_value(agent_run):
    """Finding 5: retaindb resolves RETAINDB_* before config.yaml; the UI shows that value."""
    base_url = _field(agent_run["rdb_listing"], "base_url")
    assert base_url["value"] == "https://env.example"
    assert base_url["env_effective"] is True and base_url["editable"] is False
    assert base_url["env_override_var"] == "RETAINDB_BASE_URL"
    assert agent_run["rdb_set_env_shadowed"][0] == 400
