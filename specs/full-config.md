# Spec: Full Hermes configuration panel ("Configuração completa")

## Objetivo
Adicionar à WebUI um painel que torna configurável, de forma intuitiva, TODA chave de configuração do Hermes Agent local (config.yaml, ~929 chaves folha em ~99 seções) e TODA variável de ambiente conhecida (~/.hermes/.env), persistindo de forma permanente na máquina sem sobrescrever o resto do arquivo.

## Contexto
- Agente local em `$HERMES_HOME/hermes-agent` (já descoberto por `api/config.py::_discover_agent_dir`; o servidor já roda com o python do agente e importa `hermes_cli`).
- Fonte do schema: `hermes_cli.config_defaults.DEFAULT_CONFIG` (dict aninhado; tipo inferido do valor default) e `hermes_cli.config_defaults.OPTIONAL_ENV_VARS` (dict NOME -> {description, prompt, url, password, category, advanced, ...}).
- Persistência: usar as APIs do próprio agente — `hermes_cli.config.load_config()`, `hermes_cli.config.set_config_value(key, value)` (mesma semântica de `hermes config set`), `_unset_nested`/`save_config` para reset, `save_env_value_secure(key, value)` e `remove_env_value(key)` para .env. NUNCA reescrever o config.yaml inteiro com yaml.safe_dump a partir do estado da UI; alterar só a chave editada. Respeitar o profile ativo da requisição (padrão existente `profile_env_for_active_request*` em api/routes.py).
- Painéis existentes (chat, settings, profiles, skills, memory, tasks, kanban, logs...) ficam em `static/index.html` (`data-panel=`), `static/panels.js`, `static/style.css`, rotas em `api/routes.py`. Siga os padrões e o tema visual existentes (DESIGN.md, THEMES.md). Sem build step, vanilla JS.

## Escopo
1. Backend: novo módulo `api/full_config.py` + rotas em `api/routes.py`:
   - `GET /api/full-config/schema` → lista de seções; cada chave: `path` (dotted), `type` (bool/int/float/str/list/dict/null), `default`, `value` efetivo, `is_set` (presente no config.yaml do usuário), `description` (se derivável; senão vazio). Inclui chaves presentes no config do usuário que não existem no DEFAULT_CONFIG (marcadas `custom`).
   - `POST /api/full-config/set` {path, value} → valida/coage o tipo pelo default; grava via `set_config_value` (ou equivalente do agente que preserve o restante do arquivo); retorna o valor relido do disco.
   - `POST /api/full-config/reset` {path} → remove a chave do config do usuário (volta ao default).
   - `GET /api/full-config/env` → OPTIONAL_ENV_VARS + chaves extras do .env; para `password`/secretas devolver só `is_set` e valor mascarado (nunca o segredo em claro).
   - `POST /api/full-config/env/set` {name, value} e `/env/unset` {name}, via APIs do agente; validar nome.
   - Proteção CSRF/auth igual às demais rotas POST existentes.
2. Frontend: novo painel "Config" na navegação (`data-panel="fullconfig"`, ícone coerente):
   - Lista de seções colapsáveis + busca global (por path, nome e descrição) + filtros "só alteradas" / "todas".
   - Widget por tipo: toggle (bool), input numérico (int/float), texto (str), editor JSON/YAML validado para list/dict, campo vazio para null.
   - Badge "alterado" quando `is_set`, botão "restaurar padrão", feedback de salvo/erro inline; salvar por chave (não há "salvar tudo").
   - Aba "Variáveis de ambiente" agrupada por `category`, segredos mascarados com campo de substituição, link `url` quando houver.
   - Textos da UI em pt-BR e en via `static/i18n.js` (padrão existente).
3. Testes pytest em `tests/test_full_config.py`: schema cobre todas as chaves folha do DEFAULT_CONFIG; set grava só a chave alvo e preserva outras chaves/comentários de um config.yaml temporário (HERMES_HOME isolado em tmp_path); coerção de tipo e rejeição de tipo inválido; reset; env set/unset; segredos nunca retornados em claro.

## Critérios de aceitação
- `pytest tests/test_full_config.py -q` passa.
- Número de chaves no schema >= número de folhas do DEFAULT_CONFIG.
- Alterar uma chave pela API muda somente essa chave no config.yaml (diff de 1 linha lógica).
- Nenhum segredo de .env aparece em resposta de API.
- Nenhuma alteração fora de: api/full_config.py, api/routes.py, static/index.html, static/panels.js (ou novo static/full_config.js incluído no index.html), static/style.css, static/i18n.js, tests/test_full_config.py.

## Restrições
- Não alterar ~/.hermes real nos testes. Não commitar. Não adicionar dependências.
- Não quebrar testes existentes relacionados a rotas/painéis (rode os que tocar: `pytest tests -q -k "route or panel or config" -x` se viável em tempo).
