# Spec: Memory provider settings tab (Config panel)

## Goal
Make the settings of EVERY Hermes memory provider plugin (mem0, honcho, holographic, supermemory, openviking, byterover, retaindb, and any other provider found at runtime) editable from the existing Config panel. Changes must persist on the machine using each provider's own native mechanism, without overwriting the rest of the file.

## Context
- This branch already has the Config panel: `api/full_config.py`, `static/full_config.js`, the tabs `config.yaml` and `Environment`, and `tests/test_full_config.py`. Reuse its patterns: profile-aware home, the CSRF and origin gate (`_onboarding_gate_allows`), secret masking, and error capture.
- The agent lives at `$HERMES_HOME/hermes-agent` (the server already imports it). The contract is in `agent/memory_provider.py`:
  - `get_config_schema()` returns a list of fields. Each field has `key`, `description`, and optionally `secret`, `required`, `default`, `choices`, `type` (text | integer | number | boolean), `minimum`, `maximum`, `step`, `url`, `env_var`.
  - `save_config(values, hermes_home)` writes the non-secret values to the provider's native config. Example: mem0 merge-writes `$HERMES_HOME/mem0.json`.
  - Secrets go to `.env` under `env_var`.
- Find the same discovery and loading code that `hermes memory setup` / `hermes memory status` use (look in `hermes_cli/` and `plugins/memory/__init__.py`) and reuse it. Do NOT hardcode the list of providers.
- The active provider is `memory.provider` in config.yaml.

## Scope
1. Backend: a new `api/memory_providers.py` plus routes:
   - `GET /api/full-config/memory-providers` returns, for each provider:
     - `name`, `active`, `available` (deps/config ok, if detectable);
     - its fields, with the current value read from the provider's native config or from the `.env` `env_var`;
     - secrets returned masked only (`is_set` plus the mask), never in clear.
   - `POST /api/full-config/memory-providers/set` with `{provider, values}`:
     - validates against the schema (type, choices, min/max);
     - non-secret values are written with `provider.save_config(values, home)`;
     - secrets are written with `save_env_value_secure(env_var, value)`;
     - an empty secret or an unchanged mask means "keep the current value";
     - returns the provider re-read from disk.
   - `POST /api/full-config/memory-providers/activate` with `{provider}` sets `memory.provider` through the agent's `set_config_value`. Also accept `""` or `"none"` to deactivate (check how the agent represents "no provider").
   - Same CSRF, origin gate and profile check (409 on mismatch) as the existing POST routes.
2. Frontend: a third "Memory" tab in the Config panel:
   - one card per provider, with an "Active" badge and an "Activate" button;
   - a form generated from the schema: select for `choices`, toggle for boolean, number with min/max/step, text field, and password field for secrets (placeholder "unchanged");
   - a link to `url`, descriptions as help text, and a "Save" button per provider with inline feedback;
   - en and pt-BR strings in `static/i18n.js`.
3. Tests in `tests/test_memory_providers.py`, with HERMES_HOME isolated in tmp_path:
   - provider listing (at least mem0 and honcho);
   - a set on mem0 writes `mem0.json` merging the existing keys (keys not in the form are preserved);
   - a secret goes to `.env` and never comes back in clear;
   - an unchanged mask does not overwrite the secret;
   - activate changes only `memory.provider` in config.yaml;
   - invalid choice or type returns 400;
   - the CSRF and gate checks apply.

## Acceptance criteria
- `TMPDIR=/tmp/claude-1000 .venv/bin/python -m pytest tests/test_memory_providers.py tests/test_full_config.py -q` passes.
- No secret appears in clear in any response.
- Changes only to: `api/memory_providers.py`, `api/routes.py`, `api/full_config.py` (only if needed for reuse), `static/full_config.js`, `static/index.html`, `static/style.css`, `static/i18n.js`, `tests/test_memory_providers.py`.

## Constraints
- Do not touch the real `~/.hermes` in tests. Do not commit. Add no dependencies. Be economical with turns: write whole files.
