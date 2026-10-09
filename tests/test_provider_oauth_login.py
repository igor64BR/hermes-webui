"""OAuth sign-in / sign-out from the Providers settings panel.

* ``/api/providers/oauth/{start,poll,cancel}`` are aliases of the onboarding
  OAuth flow functions and ``/api/providers/oauth/logout`` clears one provider's
  credentials through the agent's ``clear_provider_auth``. All four go through
  ``_onboarding_gate_allows``; the POSTs also go through CSRF.
* Route wiring runs in-process with the flow functions replaced by sentinels
  (no OAuth network calls).
* Logout semantics run against the REAL agent auth store writer in a
  subprocess on the agent interpreter with ``HERMES_HOME`` under ``tmp_path``;
  the real ``~/.hermes/auth.json`` is fingerprinted before/after.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import subprocess
import sys
from urllib.parse import urlparse

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import api.routes as routes  # noqa: E402
from tests.test_full_config import _agent_dir, _agent_python, _fingerprint  # noqa: E402

PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

SECRET_ACCESS = "sk-ACCESS-TOKEN-must-never-leak-0123456789"
SECRET_REFRESH = "rt-REFRESH-TOKEN-must-never-leak-0123456789"
POST_PATHS = ("/api/providers/oauth/start", "/api/providers/oauth/cancel", "/api/providers/oauth/logout")


class _FakeHandler:
    def __init__(self, method="POST", path="/", headers=None):
        self.status = None
        self.headers = {"Content-Type": "application/json", "Content-Length": "0", **(headers or {})}
        self.rfile = io.BytesIO(b"")
        self.wfile = io.BytesIO()
        self.command = method
        self.path = path
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


@pytest.fixture
def wired(monkeypatch):
    """Capture responses and replace the flow functions with recording sentinels."""
    cap = {"calls": []}

    def _j(_handler, obj, status=200, **kwargs):
        cap["status"], cap["payload"], cap["kwargs"] = status, obj, kwargs
        return True

    def _bad(_handler, msg, status=400):
        cap["status"], cap["payload"] = status, {"error": msg}
        return True

    def _rec(name, result):
        def _fn(arg):
            cap["calls"].append((name, arg))
            return dict(result)
        return _fn

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_onboarding_gate_allows", lambda _h, auth_enabled=None: True)
    monkeypatch.setattr(routes, "start_onboarding_oauth_flow",
                        _rec("start", {"ok": True, "flow_id": "f1", "status": "pending",
                                       "user_code": "ABCD-1234", "verification_uri": "https://x/device"}))
    monkeypatch.setattr(routes, "poll_onboarding_oauth_flow", _rec("poll", {"ok": True, "flow_id": "f1", "status": "pending"}))
    monkeypatch.setattr(routes, "cancel_onboarding_oauth_flow", _rec("cancel", {"ok": True, "flow_id": "f1", "status": "cancelled"}))
    import api.providers as providers

    monkeypatch.setattr(providers, "logout_provider_oauth",
                        _rec("logout", {"ok": True, "provider": "openai-codex", "cleared": True}))
    return cap


def _post(monkeypatch, path, body, handler=None):
    monkeypatch.setattr(routes, "read_body", lambda _h: dict(body))
    handler = handler or _FakeHandler("POST", path)
    routes.handle_post(handler, urlparse(path))
    return handler


def _get(path):
    handler = _FakeHandler("GET", path)
    routes.handle_get(handler, urlparse(path))
    return handler


# ── Route wiring ────────────────────────────────────────────────────────────


def test_aliases_route_to_the_onboarding_flow_functions(monkeypatch, wired):
    _post(monkeypatch, "/api/providers/oauth/start", {"provider": "openai-codex"})
    assert wired["calls"][-1] == ("start", {"provider": "openai-codex"})
    assert wired["status"] == 200 and wired["payload"]["flow_id"] == "f1"
    assert wired["kwargs"]["extra_headers"]["Cache-Control"] == "no-store"

    _get("/api/providers/oauth/poll?flow_id=f1")
    assert wired["calls"][-1] == ("poll", "f1")
    assert wired["status"] == 200 and wired["payload"]["status"] == "pending"

    _post(monkeypatch, "/api/providers/oauth/cancel", {"flow_id": "f1", "provider": "openai-codex"})
    assert wired["calls"][-1] == ("cancel", {"flow_id": "f1", "provider": "openai-codex"})
    assert wired["payload"]["status"] == "cancelled"

    _post(monkeypatch, "/api/providers/oauth/logout", {"provider": "openai-codex"})
    assert wired["calls"][-1] == ("logout", "openai-codex")
    assert wired["payload"] == {"ok": True, "provider": "openai-codex", "cleared": True}


def test_aliases_do_not_depend_on_onboarding_completion():
    src = (REPO_ROOT / "api" / "oauth.py").read_text(encoding="utf-8")
    start = src[src.index("def start_onboarding_oauth_flow"):src.index("def poll_onboarding_oauth_flow")]
    assert "onboarding_completed" not in start and "completed" not in start


def test_gate_denies_all_four_routes(monkeypatch, wired):
    monkeypatch.setattr(routes, "_onboarding_gate_allows", lambda _h, auth_enabled=None: False)
    for path in POST_PATHS:
        _post(monkeypatch, path, {"provider": "openai-codex", "flow_id": "f1"})
        assert wired["status"] == 403, path
    _get("/api/providers/oauth/poll?flow_id=f1")
    assert wired["status"] == 403
    assert wired["calls"] == []


def test_csrf_check_runs_on_every_post(monkeypatch, wired):
    seen = []

    def _deny(handler):
        seen.append(handler.path)
        return False

    monkeypatch.setattr(routes, "_check_csrf", _deny)
    for path in POST_PATHS:
        _post(monkeypatch, path, {"provider": "openai-codex", "flow_id": "f1"})
        assert wired["status"] == 403, path
    assert seen == list(POST_PATHS)
    assert wired["calls"] == []


def test_real_csrf_rejects_cross_origin_post(monkeypatch):
    cap = {}
    monkeypatch.setattr(routes, "j", lambda _h, obj, status=200, **k: cap.update(status=status, payload=obj) or True)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, status=400: cap.update(status=status, payload={"error": msg}) or True)
    called = []
    monkeypatch.setattr(routes, "start_onboarding_oauth_flow", lambda body: called.append(body) or {"ok": True})
    monkeypatch.setattr(routes, "read_body", lambda _h: {"provider": "openai-codex"})
    handler = _FakeHandler("POST", "/api/providers/oauth/start",
                           {"Origin": "http://evil.invalid", "Host": "127.0.0.1:8787"})
    routes.handle_post(handler, urlparse("/api/providers/oauth/start"))
    assert cap["status"] == 403
    assert called == []


def test_logout_rejects_non_oauth_and_blank_providers(monkeypatch):
    import api.providers as providers

    for bad_id in ("", "openrouter", "../auth"):
        with pytest.raises(ValueError):
            providers.logout_provider_oauth(bad_id)


def test_flow_errors_map_to_client_errors(monkeypatch, wired):
    def _boom(_body):
        raise ValueError("Only OpenAI Codex and Anthropic/Claude OAuth are supported")

    monkeypatch.setattr(routes, "start_onboarding_oauth_flow", _boom)
    _post(monkeypatch, "/api/providers/oauth/start", {"provider": "nous"})
    assert wired["status"] == 400

    import api.providers as providers

    monkeypatch.setattr(providers, "logout_provider_oauth",
                        lambda _p: {"ok": False, "status": 409, "error": "cannot scope"})
    _post(monkeypatch, "/api/providers/oauth/logout", {"provider": "nous"})
    assert wired["status"] == 409 and wired["payload"] == {"error": "cannot scope"}


def test_start_and_poll_responses_never_carry_tokens(monkeypatch, tmp_path):
    """Real flow functions (network mocked): payloads stay browser-safe."""
    import api.oauth as oauth

    cap = []
    monkeypatch.setattr(routes, "j", lambda _h, obj, status=200, **k: cap.append((status, obj)) or True)
    monkeypatch.setattr(routes, "bad", lambda _h, msg, status=400: cap.append((status, {"error": msg})) or True)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_onboarding_gate_allows", lambda _h, auth_enabled=None: True)
    monkeypatch.setattr(oauth, "_get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(oauth, "_request_codex_user_code", lambda: {
        "user_code": "ABCD-1234", "device_auth_id": "DEVICE-SECRET-xyz", "interval": 5, "expires_in": 600})
    monkeypatch.setattr(oauth, "_spawn_codex_oauth_worker", lambda _fid: None)
    with oauth._OAUTH_FLOWS_LOCK:
        oauth._OAUTH_FLOWS.clear()
    try:
        _post(monkeypatch, "/api/providers/oauth/start", {"provider": "openai-codex"})
        status, payload = cap[-1]
        assert status == 200 and payload["user_code"] == "ABCD-1234"
        fid = payload["flow_id"]
        with oauth._OAUTH_FLOWS_LOCK:  # simulate a worker that stored token material
            oauth._OAUTH_FLOWS[fid].update(access_token=SECRET_ACCESS, refresh_token=SECRET_REFRESH)
        _get(f"/api/providers/oauth/poll?flow_id={fid}")
        _post(monkeypatch, "/api/providers/oauth/cancel", {"flow_id": fid})
        dumped = json.dumps(cap)
        for secret in (SECRET_ACCESS, SECRET_REFRESH, "DEVICE-SECRET-xyz"):
            assert secret not in dumped
    finally:
        with oauth._OAUTH_FLOWS_LOCK:
            oauth._OAUTH_FLOWS.clear()


# ── Logout against the real agent auth store ────────────────────────────────


_SEED_AUTH = {
    "version": 1,
    "active_provider": "openai-codex",
    "providers": {
        "openai-codex": {"tokens": {"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH}},
        "nous": {"access_token": "nous-KEEP-token", "refresh_token": "nous-KEEP-refresh"},
        "anthropic": {"access_token": "sk-ant-oat01-STATE-gone", "refresh_token": "anthropic-REFRESH-gone"},
    },
    "credential_pool": {
        "openai-codex": [{"id": "codex-1", "auth_type": "oauth", "access_token": SECRET_ACCESS}],
        "anthropic": [
            {"id": "anthropic-1", "source": "claude_code_linked", "auth_type": "oauth"},
            # No auth_type: the agent infers OAuth from the sk-ant-oat prefix.
            {"id": "anthropic-oat", "source": "manual", "access_token": "sk-ant-oat01-POOL-gone"},
            {"id": "anthropic-key", "source": "manual", "auth_type": "api_key",
             "access_token": "sk-ant-api03-APIKEY-KEEP"},
        ],
        "openrouter": [{"id": "or-1", "auth_type": "api_key", "access_token": "sk-or-KEEP"}],
    },
}

_SCENARIO = r'''
import json, os, sys
from pathlib import Path
agent_dir, repo_root, home = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, agent_dir); sys.path.insert(0, repo_root)

import hermes_cli.venv_sync as _vs
def _blocked(*a, **k):
    raise RuntimeError("venv sync blocked in tests")
for _name in ("prepare_launch", "_finish_source_update", "_sync_source_dependencies"):
    if hasattr(_vs, _name):
        setattr(_vs, _name, _blocked)

import api.profiles as _prof
_prof.get_active_hermes_home = lambda: home
import api.providers as providers

R = {}
R["logout_codex"] = providers.logout_provider_oauth("openai-codex")
R["after_codex"] = json.loads((home / "auth.json").read_text())
R["logout_alias"] = providers.logout_provider_oauth("claude-code")
R["after_anthropic"] = json.loads((home / "auth.json").read_text())
R["logout_again"] = providers.logout_provider_oauth("openai-codex")
R["logout_anthropic_again"] = providers.logout_provider_oauth("anthropic")
R["after_anthropic_again"] = json.loads((home / "auth.json").read_text())
print("@@RESULT@@" + json.dumps(R))
'''


@pytest.fixture(scope="module")
def logout_run(tmp_path_factory):
    agent = _agent_dir()
    if agent is None:
        pytest.skip("hermes-agent source not found")
    probe_home = tmp_path_factory.mktemp("probe-home")
    python = _agent_python(agent, probe_home)
    if python is None:
        pytest.skip("no interpreter can import hermes_cli")
    home = tmp_path_factory.mktemp("hermes-home")
    state = tmp_path_factory.mktemp("webui-state")
    (home / "auth.json").write_text(json.dumps(_SEED_AUTH), encoding="utf-8")
    real_home = pathlib.Path.home() / ".hermes"
    guarded = [real_home / "auth.json", real_home / "config.yaml", real_home / ".env"]
    before = _fingerprint(guarded)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(HERMES_HOME=str(home), HERMES_BASE_HOME=str(home), HERMES_WEBUI_STATE_DIR=str(state))
    proc = subprocess.run(
        [python, "-c", _SCENARIO, str(agent), str(REPO_ROOT), str(home)],
        capture_output=True, text=True, timeout=240, env=env, cwd=str(home),
    )
    assert _fingerprint(guarded) == before, "real ~/.hermes auth/config/.env changed"
    line = next((l for l in proc.stdout.splitlines() if l.startswith("@@RESULT@@")), None)
    assert proc.returncode == 0 and line, f"scenario failed:\nSTDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-4000:]}"
    return json.loads(line[len("@@RESULT@@"):])


def test_logout_removes_only_the_target_provider(logout_run):
    assert logout_run["logout_codex"] == {"ok": True, "provider": "openai-codex", "cleared": True}
    after = logout_run["after_codex"]
    assert "openai-codex" not in after.get("providers", {})
    assert "openai-codex" not in after.get("credential_pool", {})
    assert after["providers"]["nous"] == _SEED_AUTH["providers"]["nous"]
    assert after["credential_pool"]["anthropic"] == _SEED_AUTH["credential_pool"]["anthropic"]
    assert after["credential_pool"]["openrouter"] == _SEED_AUTH["credential_pool"]["openrouter"]
    assert after.get("active_provider") is None


def test_anthropic_logout_removes_only_oauth_and_keeps_api_keys(logout_run):
    assert logout_run["logout_alias"] == {"ok": True, "provider": "anthropic", "cleared": True}
    after = logout_run["after_anthropic"]
    assert "anthropic" not in after.get("providers", {})
    remaining = after["credential_pool"]["anthropic"]
    assert [e["id"] for e in remaining] == ["anthropic-key"]
    assert remaining[0]["access_token"] == "sk-ant-api03-APIKEY-KEEP"
    dumped = json.dumps(after)
    assert "sk-ant-oat01-POOL-gone" not in dumped and "sk-ant-oat01-STATE-gone" not in dumped
    # A second sign-out finds no OAuth left and still keeps the API key.
    assert logout_run["logout_anthropic_again"] == {"ok": True, "provider": "anthropic", "cleared": False}
    assert [e["id"] for e in logout_run["after_anthropic_again"]["credential_pool"]["anthropic"]] == ["anthropic-key"]
    assert after["credential_pool"]["openrouter"] == _SEED_AUTH["credential_pool"]["openrouter"]
    assert after["providers"]["nous"] == _SEED_AUTH["providers"]["nous"]


def test_unsupported_provider_error_is_not_onboarding_specific(monkeypatch, wired):
    import api.oauth as oauth

    monkeypatch.setattr(routes, "start_onboarding_oauth_flow", oauth.start_onboarding_oauth_flow)
    _post(monkeypatch, "/api/providers/oauth/start", {"provider": "copilot"})
    assert wired["status"] == 400
    assert "onboarding" not in wired["payload"]["error"].lower()


def test_logout_is_idempotent_and_returns_no_token(logout_run):
    assert logout_run["logout_again"] == {"ok": True, "provider": "openai-codex", "cleared": False}
    dumped = json.dumps([logout_run["logout_codex"], logout_run["logout_alias"], logout_run["logout_again"]])
    dumped += json.dumps(logout_run["logout_anthropic_again"])
    for secret in (SECRET_ACCESS, SECRET_REFRESH, "nous-KEEP-token", "sk-or-KEEP", "sk-ant-api03-APIKEY-KEEP"):
        assert secret not in dumped


# ── Static wiring ───────────────────────────────────────────────────────────


def test_routes_register_get_poll_and_post_start_cancel_logout():
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    get_body = src[src.index("def handle_get("):src.index("def handle_post(")]
    post_body = src[src.index("def handle_post("):]
    assert '"/api/providers/oauth/poll"' in get_body
    for path in POST_PATHS:
        assert f'"{path}"' in src and path not in get_body
    assert "_PROVIDER_OAUTH_POST_PATHS" in post_body


def test_providers_payload_flags_in_app_login_support():
    src = (REPO_ROOT / "api" / "providers.py").read_text(encoding="utf-8")
    assert '"oauth_login_supported": is_oauth and _provider_oauth_login_supported(pid)' in src
    import api.providers as providers

    assert providers._provider_oauth_login_supported("openai-codex")
    assert not providers._provider_oauth_login_supported("copilot")


def test_panel_renders_sign_in_and_sign_out_for_oauth_providers():
    block = PANELS_JS[PANELS_JS.index("function _buildProviderCard(p){"):]
    block = block[:block.index("let input=null;")]
    assert "if(isOauth){" in block
    assert "_buildProviderOAuthControls(p,card)" in block
    assert "providers_oauth_config_yaml_hint" in block

    ctl = PANELS_JS[PANELS_JS.index("function _buildProviderOAuthControls(p,card){"):PANELS_JS.index("function _buildProviderCard(p){")]
    assert "t('providers_oauth_sign_in')" in ctl
    assert "t('providers_oauth_reauth')" in ctl
    assert "t('providers_oauth_sign_out')" in ctl
    assert "p.oauth_login_supported" in ctl
    # Sign out only for real OAuth sources, never env/token/api-key/config.yaml.
    assert "const signedIn=!!p.has_key&&p.key_source==='oauth';" in ctl
    assert "res.cleared===false" in ctl and "t('providers_oauth_nothing_to_remove')" in ctl
    for endpoint in ("/api/providers/oauth/start", "/api/providers/oauth/poll?flow_id=",
                     "/api/providers/oauth/cancel", "/api/providers/oauth/logout"):
        assert endpoint in ctl
    assert "/api/onboarding/oauth" not in ctl
    assert "_refreshModelDropdownsAfterProviderChange();" in ctl and "loadProvidersPanel()" in ctl
    # The verification link is attached via DOM APIs, never via innerHTML interpolation.
    assert not re.search(r"innerHTML\s*=\s*`", ctl)


def test_i18n_has_provider_oauth_strings_in_english():
    en = I18N_JS[I18N_JS.index("  en: {"):I18N_JS.index("  it: {")]
    for key in ("providers_oauth_sign_in", "providers_oauth_reauth", "providers_oauth_sign_out",
                "providers_oauth_sign_in_hint", "providers_oauth_sign_out_confirm_message"):
        assert f"    {key}:" in en
