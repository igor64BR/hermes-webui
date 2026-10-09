# Spec: OAuth sign-in and sign-out from the Providers panel

## Goal
Let the user sign in, re-authenticate and sign out of OAuth providers (openai-codex, copilot, copilot-acp, qwen-oauth, nous, anthropic, and any other provider the backend flags `is_oauth`) directly from the Providers settings panel. This replaces the current "Run hermes auth in the terminal" hint.

## Context
- The backend already implements the OAuth device/PKCE flow for the first-run wizard:
  - `POST /api/onboarding/oauth/start`, `GET /api/onboarding/oauth/poll`, `POST /api/onboarding/oauth/cancel`;
  - the functions `start_onboarding_oauth_flow` etc. live in `api/onboarding.py`;
  - the frontend counterpart is in `static/onboarding.js` (`_renderOnboardingProviderOAuthField` and its helpers).
  - It writes to the profile-scoped `auth.json`, the same file `hermes auth` uses.
- The Providers panel is in `static/panels.js` (around the `isOauth` handling, ~line 11580) and fed by `GET /api/providers` (`is_oauth`, `key_source`).
- Hermes' own CLI logic for logging out / removing credentials lives in `$HERMES_HOME/hermes-agent/hermes_cli/auth*.py`. Reuse the agent function that `hermes auth logout` (or its equivalent) uses. Do not hand-edit `auth.json` if an agent function exists.

## Scope
1. Frontend (`static/panels.js`, `static/style.css`, `static/i18n.js`):
   - for each OAuth provider card, add a "Sign in" button when not authenticated and "Re-authenticate" + "Sign out" when authenticated;
   - sign-in runs the existing start → (show verification URL / user code, open link) → poll → done/cancel flow inline in the card, reusing the onboarding helpers where practical (extract shared helpers rather than duplicating, if that is cleaner);
   - on success, refresh the provider list and the model list;
   - keep the `config_yaml` key_source hint for tokens set in config.yaml.
2. Backend:
   - add neutral aliases `POST /api/providers/oauth/start`, `GET /api/providers/oauth/poll`, `POST /api/providers/oauth/cancel` that call the same functions, so the panel works after onboarding has completed. Check whether the onboarding versions refuse to run once onboarding is complete; the aliases must not.
   - add `POST /api/providers/oauth/logout` with `{provider}`, which removes that provider's OAuth credentials via the agent's auth API. It is profile-scoped.
   - All four routes go through the same `_onboarding_gate_allows` + CSRF protection as the onboarding OAuth routes. Tokens are never returned in responses.
3. Tests in `tests/test_provider_oauth_login.py`, with HERMES_HOME isolated in tmp_path and the actual network flow mocked:
   - the aliases route to the same functions;
   - the gate and CSRF are enforced;
   - logout removes only the target provider's credentials from `auth.json` (other providers and the credential_pool entries of other providers stay intact);
   - no token appears in any response;
   - a static check that the panel renders the Sign in / Sign out controls for `is_oauth` providers.

## Acceptance criteria
- `TMPDIR=/tmp/claude-1000 .venv/bin/python -m pytest tests/test_provider_oauth_login.py tests/test_full_config.py tests/test_memory_providers.py -q` passes.
- Existing onboarding OAuth tests still pass (`-k onboarding_oauth or oauth`).
- Changes only to: `api/routes.py`, `api/onboarding.py` (only to extract or reuse helpers), `api/providers.py` (only if needed), `static/panels.js`, `static/onboarding.js` (only to extract shared helpers), `static/style.css`, `static/i18n.js`, `tests/test_provider_oauth_login.py`.

## Constraints
- Do not touch the real `~/.hermes`. Make no real OAuth network calls in tests. Do not commit. Add no dependencies. Be economical with turns: write whole files.
