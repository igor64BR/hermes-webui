// ── Full Hermes configuration panel ("Config") ──
// Every DEFAULT_CONFIG key of the active profile's config.yaml plus every known
// .env variable. Edits are saved one key at a time through /api/full-config/*;
// the server writes only the touched key and never returns secrets in clear.

const _fc = {
  tab: 'config',          // 'config' | 'env'
  filter: 'all',          // 'all' | 'changed'
  query: '',
  schema: null,
  env: null,
  open: new Set(),        // expanded section names (config) / categories (env)
  loading: {config: false, env: false},
  loadedFor: {config: null, env: null},  // client profile token each cache was fetched under
};

// Browser-side identity of the active profile; caches are only valid for it.
function _fcProfileToken(){
  return (typeof S !== 'undefined' && S && S.activeProfile) || 'default';
}

// Server-side profile name echoed by the payload the user is editing; sent with
// every write so the server refuses edits prepared against another profile.
function _fcServerProfile(tab){
  const data = (tab || _fc.tab) === 'config' ? _fc.schema : _fc.env;
  return data && data.profile ? data.profile : undefined;
}

// Called on profile switch (switchToProfile) and after server-side redirects.
function fullConfigInvalidate(){
  _fc.schema = null;
  _fc.env = null;
  _fc.loadedFor = {config: null, env: null};
  if (typeof _currentPanel !== 'undefined' && _currentPanel === 'fullconfig') loadFullConfig(true);
}

function _fcEl(id){ return document.getElementById(id); }

function _fcSectionLabel(name){ return name ? name : t('fc_general_section'); }

async function loadFullConfig(force){
  const tab = _fc.tab;
  const field = tab === 'config' ? 'schema' : 'env';
  const profile = _fcProfileToken();
  if (_fc.loadedFor[tab] !== profile) _fc[field] = null;  // profile switched since fetch
  if (!force && _fc[field]) { _fcRender(); return; }
  const body = _fcEl('fullConfigBody');
  if (body && !_fc[field]) body.innerHTML = `<div class="fc-empty">${esc(t('loading'))}</div>`;
  if (_fc.loading[tab]) return;  // in flight; its completion renders if this tab is still shown
  _fc.loading[tab] = true;
  let data = null, error = null;
  try {
    data = await api(tab === 'config' ? '/api/full-config/schema' : '/api/full-config/env');
  } catch (e) {
    error = e;
  } finally {
    _fc.loading[tab] = false;
  }
  if (_fcProfileToken() !== profile) {  // profile switched mid-flight: discard
    if (_fc.tab === tab) loadFullConfig(true);
    return;
  }
  if (data) { _fc[field] = data; _fc.loadedFor[tab] = profile; }
  if (_fc.tab !== tab) return;
  if (error) {
    const el = _fcEl('fullConfigBody');
    if (el) el.innerHTML = `<div class="fc-empty fc-error">${esc(t('fc_load_error'))}: ${esc(error.message || error)}</div>`;
    return;
  }
  _fcRender();
}

// A 409 means the server's active profile differs from the cached view.
function _fcHandleWriteError(e){
  if (e && e.status === 409) { showToast(e.message || String(e)); fullConfigInvalidate(); return true; }
  return false;
}

function fullConfigSetTab(tab){
  _fc.tab = tab === 'env' ? 'env' : 'config';
  document.querySelectorAll('.fc-tab').forEach(b => {
    const active = b.dataset.fcTab === _fc.tab;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  loadFullConfig(false);
}

function fullConfigSetFilter(value){
  _fc.filter = value === 'changed' ? 'changed' : 'all';
  _fcRender();
}

let _fcSearchTimer = null;
function fullConfigSearch(value){
  clearTimeout(_fcSearchTimer);
  _fcSearchTimer = setTimeout(() => { _fc.query = String(value || '').trim().toLowerCase(); _fcRender(); }, 120);
}

function fullConfigToggleAll(open){
  const groups = _fc.tab === 'config'
    ? ((_fc.schema && _fc.schema.sections) || []).map(s => s.name)
    : ((_fc.env && _fc.env.categories) || []).map(c => 'env:' + c.name);
  _fc.open = open ? new Set(groups) : new Set();
  _fcRender();
}

function _fcMatchesKey(k){
  if (_fc.filter === 'changed' && !k.is_set) return false;
  if (!_fc.query) return true;
  return (k.path + ' ' + (k.description || '')).toLowerCase().includes(_fc.query);
}

function _fcMatchesEnv(v){
  if (_fc.filter === 'changed' && !v.is_set) return false;
  if (!_fc.query) return true;
  return [v.name, v.description, v.prompt, v.help, v.category].join(' ').toLowerCase().includes(_fc.query);
}

function _fcRender(){
  const body = _fcEl('fullConfigBody');
  const status = _fcEl('fullConfigStatus');
  const nav = _fcEl('fullConfigSections');
  if (!body) return;
  if (_fc.tab === 'config') _fcRenderConfig(body, status, nav);
  else _fcRenderEnv(body, status, nav);
}

function _fcGroupHtml(id, label, count, changed, desc, inner, open){
  return `<details class="fc-section" data-fc-group="${esc(id)}"${open ? ' open' : ''}>
    <summary><span class="fc-section-name">${esc(label)}</span>
      <span class="fc-section-meta">${count}${changed ? ` · <span class="fc-badge fc-badge-changed">${changed} ${esc(t('fc_changed_badge'))}</span>` : ''}</span></summary>
    ${desc ? `<div class="fc-section-desc">${esc(desc)}</div>` : ''}
    <div class="fc-section-body">${inner}</div>
  </details>`;
}

function _fcBindGroups(body, renderRows){
  body.querySelectorAll('details.fc-section').forEach(d => {
    d.addEventListener('toggle', () => {
      const id = d.dataset.fcGroup;
      // Search/filter results open every match; only remember explicit choices.
      const remember = !_fc.query && _fc.filter !== 'changed';
      if (d.open) {
        if (remember) _fc.open.add(id);
        const inner = d.querySelector('.fc-section-body');
        if (inner && !inner.dataset.rendered) { inner.innerHTML = renderRows(id); inner.dataset.rendered = '1'; }
      } else if (remember) {
        _fc.open.delete(id);
      }
    });
  });
}

function _fcRenderNav(nav, groups){
  if (!nav) return;
  nav.innerHTML = groups.map(g => `<button type="button" class="fc-nav-item" data-fc-jump="${esc(g.id)}">
    <span>${esc(g.label)}</span><span class="fc-nav-count">${g.changed ? `${g.changed}/` : ''}${g.count}</span></button>`).join('')
    || `<div class="fc-empty">${esc(t('fc_no_results'))}</div>`;
  nav.querySelectorAll('[data-fc-jump]').forEach(btn => btn.addEventListener('click', () => {
    const id = btn.dataset.fcJump;
    const el = Array.from(document.querySelectorAll('#fullConfigBody details.fc-section')).find(d => d.dataset.fcGroup === id);
    if (!el) return;
    if (!el.open) el.open = true;
    el.scrollIntoView({block: 'start', behavior: 'smooth'});
    if (typeof _isDesktopWidth === 'function' && !_isDesktopWidth() && typeof closeMobileSidebar === 'function') closeMobileSidebar();
  }));
}

// ── config.yaml ──

function _fcRenderConfig(body, status, nav){
  const schema = _fc.schema;
  if (!schema) return;
  if (status) status.textContent = t('fc_summary', schema.total, schema.changed) + (schema.config_path ? ` · ${schema.config_path}` : '');
  const searching = !!_fc.query || _fc.filter === 'changed';
  const groups = [];
  const html = [];
  const visible = {};
  for (const sec of schema.sections) {
    const keys = sec.keys.filter(_fcMatchesKey);
    if (!keys.length) continue;
    visible[sec.name] = keys;
    const open = searching || _fc.open.has(sec.name);
    const label = _fcSectionLabel(sec.name);
    groups.push({id: sec.name, label, count: sec.keys.length, changed: sec.changed});
    html.push(_fcGroupHtml(sec.name, label, keys.length, keys.filter(k => k.is_set).length,
      sec.description, open ? keys.map(_fcKeyRow).join('') : '', open));
  }
  body.innerHTML = (schema.managed ? `<div class="fc-banner">${esc(t('fc_managed'))}</div>` : '')
    + (html.join('') || `<div class="fc-empty">${esc(t('fc_no_results'))}</div>`);
  body.querySelectorAll('details.fc-section[open] .fc-section-body').forEach(el => { el.dataset.rendered = '1'; });
  _fcBindGroups(body, id => (visible[id] || []).map(_fcKeyRow).join(''));
  _fcRenderNav(nav, groups);
}

function _fcFindKey(path){
  for (const sec of (_fc.schema && _fc.schema.sections) || []) {
    const k = sec.keys.find(x => x.path === path);
    if (k) return k;
  }
  return null;
}

function _fcValueText(value, widget){
  if (value === null || value === undefined) return '';
  if (widget === 'list' || widget === 'dict' || typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

function _fcInputHtml(k){
  const path = esc(k.path);
  const disabled = k.readonly || (_fc.schema && _fc.schema.managed) ? ' disabled' : '';
  const w = k.widget;
  if (w === 'bool') {
    return `<label class="fc-switch"><input type="checkbox" data-fc-input="${path}"${k.value ? ' checked' : ''}${disabled}
      onchange="fullConfigSave(this.dataset.fcInput)"><span class="fc-switch-track"></span></label>`;
  }
  if (w === 'int' || w === 'float') {
    return `<input class="fc-input fc-input-num" type="number" ${w === 'int' ? 'step="1"' : 'step="any"'} data-fc-input="${path}"
      value="${esc(_fcValueText(k.value, w))}"${disabled} onkeydown="if(event.key==='Enter')fullConfigSave(this.dataset.fcInput)">`;
  }
  if (w === 'list' || w === 'dict') {
    const text = _fcValueText(k.value, w);
    const rows = Math.min(14, Math.max(2, text.split('\n').length));
    return `<textarea class="fc-input fc-input-code" rows="${rows}" spellcheck="false" data-fc-input="${path}"${disabled}
      oninput="_fcValidateCode(this)">${esc(text)}</textarea>`;
  }
  const placeholder = w === 'null' ? ` placeholder="${esc(t('fc_null_hint'))}"` : '';
  return `<input class="fc-input" type="text" autocomplete="off" spellcheck="false" data-fc-input="${path}"
    value="${esc(_fcValueText(k.value, w))}"${placeholder}${disabled} onkeydown="if(event.key==='Enter')fullConfigSave(this.dataset.fcInput)">`;
}

function _fcKeyRow(k){
  const path = esc(k.path);
  const badges = [
    k.is_set ? `<span class="fc-badge fc-badge-changed">${esc(t('fc_changed_badge'))}</span>` : '',
    k.custom ? `<span class="fc-badge">${esc(t('fc_custom_badge'))}</span>` : '',
    `<span class="fc-badge fc-badge-type">${esc(k.widget)}</span>`,
  ].join('');
  const showDefault = !k.custom && k.widget !== 'bool';
  const defaultText = showDefault ? _fcValueText(k.default, k.widget) : '';
  const canSave = !k.readonly && k.widget !== 'bool' && !(_fc.schema && _fc.schema.managed);
  return `<div class="fc-row${k.is_set ? ' is-set' : ''}" data-fc-row="${path}">
    <div class="fc-row-head">
      <code class="fc-path">${path}</code>${badges}
    </div>
    ${k.description ? `<div class="fc-desc">${esc(k.description)}</div>` : ''}
    ${k.readonly ? `<div class="fc-hint">${esc(k.readonly_reason || t('fc_readonly_hint'))}</div>` : ''}
    ${k.secret ? `<div class="fc-hint">${esc(t('fc_secret_hint'))}</div>` : ''}
    <div class="fc-row-edit">
      ${_fcInputHtml(k)}
      <div class="fc-row-actions">
        ${canSave ? `<button type="button" class="fc-btn fc-btn-primary" onclick="fullConfigSave(this.closest('[data-fc-row]').dataset.fcRow)">${esc(t('fc_save'))}</button>` : ''}
        ${k.is_set && !k.readonly && !(_fc.schema && _fc.schema.managed) ? `<button type="button" class="fc-btn" onclick="fullConfigReset(this.closest('[data-fc-row]').dataset.fcRow)">${esc(t('fc_reset'))}</button>` : ''}
      </div>
    </div>
    ${showDefault && defaultText !== '' ? `<div class="fc-default">${esc(t('fc_default'))}: <code>${esc(defaultText.length > 160 ? defaultText.slice(0, 160) + '…' : defaultText)}</code></div>` : ''}
    <div class="fc-feedback" aria-live="polite"></div>
  </div>`;
}

function _fcRowEl(path){
  return Array.from(document.querySelectorAll('#fullConfigBody [data-fc-row]')).find(el => el.dataset.fcRow === path) || null;
}

function _fcFeedback(row, ok, msg){
  if (!row) return;
  const fb = row.querySelector('.fc-feedback');
  if (!fb) return;
  fb.textContent = msg;
  fb.className = 'fc-feedback ' + (ok ? 'ok' : 'err');
  if (ok) setTimeout(() => { if (fb.textContent === msg) { fb.textContent = ''; fb.className = 'fc-feedback'; } }, 2500);
}

function _fcParseCode(text){
  const trimmed = String(text || '').trim();
  if (!trimmed) return {ok: false};
  try { return {ok: true, value: JSON.parse(trimmed)}; }
  catch (_) { return {ok: true, value: trimmed, raw: true}; } // YAML: validated server-side
}

function _fcValidateCode(el){
  const trimmed = String(el.value || '').trim();
  let bad = false;
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    try { JSON.parse(trimmed); } catch (_) { bad = true; }
  }
  el.classList.toggle('invalid', bad);
}

function _fcReplaceKey(entry){
  if (!entry || !_fc.schema) return;
  for (const sec of _fc.schema.sections) {
    const i = sec.keys.findIndex(x => x.path === entry.path);
    if (i >= 0) {
      const was = sec.keys[i].is_set;
      sec.keys[i] = entry;
      if (was !== entry.is_set) {
        sec.changed += entry.is_set ? 1 : -1;
        _fc.schema.changed += entry.is_set ? 1 : -1;
      }
      return;
    }
  }
}

function _fcRerenderRow(path, ok, msg){
  const k = _fcFindKey(path);
  const row = _fcRowEl(path);
  if (k && row) {
    const tmp = document.createElement('div');
    tmp.innerHTML = _fcKeyRow(k);
    row.replaceWith(tmp.firstElementChild);
  }
  _fcFeedback(_fcRowEl(path), ok, msg);
  const status = _fcEl('fullConfigStatus');
  if (status && _fc.schema) status.textContent = t('fc_summary', _fc.schema.total, _fc.schema.changed) + (_fc.schema.config_path ? ` · ${_fc.schema.config_path}` : '');
}

async function fullConfigSave(path){
  const k = _fcFindKey(path);
  const row = _fcRowEl(path);
  if (!k || !row) return;
  const input = row.querySelector('[data-fc-input]');
  if (!input) return;
  let value;
  if (k.widget === 'bool') value = !!input.checked;
  else if (k.widget === 'int' || k.widget === 'float') {
    if (input.value.trim() === '') { _fcFeedback(row, false, t('fc_invalid_number')); return; }
    value = Number(input.value);
    if (!Number.isFinite(value) || (k.widget === 'int' && !Number.isInteger(value))) { _fcFeedback(row, false, t('fc_invalid_number')); return; }
  } else if (k.widget === 'list' || k.widget === 'dict') {
    const parsed = _fcParseCode(input.value);
    if (!parsed.ok) { _fcFeedback(row, false, t('fc_invalid_json')); return; }
    if (!parsed.raw && (k.widget === 'list') !== Array.isArray(parsed.value)) { _fcFeedback(row, false, t('fc_invalid_json')); return; }
    value = parsed.value;
  } else if (k.widget === 'null') {
    if (input.value.trim() === '') { if (k.is_set) await fullConfigReset(path, true); return; }
    value = input.value;
  } else value = input.value;
  row.classList.add('saving');
  try {
    const res = await api('/api/full-config/set', {method: 'POST', body: JSON.stringify({path, value, profile: _fcServerProfile('config')})});
    if (res.stale) { showToast(t('fc_saved')); await loadFullConfig(true); return; }
    _fcReplaceKey(res.entry);
    _fcRerenderRow(path, true, t('fc_saved'));
  } catch (e) {
    row.classList.remove('saving');
    if (_fcHandleWriteError(e)) return;
    if (k.widget === 'bool') input.checked = !!k.value;
    _fcFeedback(row, false, e.message || String(e));
  }
}

async function fullConfigReset(path, skipConfirm){
  const row = _fcRowEl(path);
  if (!skipConfirm) {
    const ok = await showConfirmDialog({title: t('fc_reset'), message: t('fc_confirm_reset', path), confirmLabel: t('fc_reset'), focusCancel: true});
    if (!ok) return;
  }
  try {
    const res = await api('/api/full-config/reset', {method: 'POST', body: JSON.stringify({path, profile: _fcServerProfile('config')})});
    if (res.entry) { _fcReplaceKey(res.entry); _fcRerenderRow(path, true, t('fc_reset_done')); }
    else { await loadFullConfig(true); showToast(t('fc_reset_done')); }
  } catch (e) {
    if (_fcHandleWriteError(e)) return;
    _fcFeedback(row, false, e.message || String(e));
  }
}

// ── .env ──

function _fcRenderEnv(body, status, nav){
  const env = _fc.env;
  if (!env) return;
  if (status) status.textContent = t('fc_env_summary', env.total, env.set) + (env.env_path ? ` · ${env.env_path}` : '');
  const searching = !!_fc.query || _fc.filter === 'changed';
  const groups = [];
  const html = [];
  const visible = {};
  for (const cat of env.categories) {
    const vars = cat.vars.filter(_fcMatchesEnv);
    if (!vars.length) continue;
    visible[cat.name] = vars;
    const open = searching || _fc.open.has('env:' + cat.name);
    groups.push({id: 'env:' + cat.name, label: cat.name, count: cat.vars.length, changed: cat.vars.filter(v => v.is_set).length});
    html.push(_fcGroupHtml('env:' + cat.name, cat.name, vars.length, vars.filter(v => v.is_set).length, '',
      open ? vars.map(_fcEnvRow).join('') : '', open));
  }
  const add = `<div class="fc-env-add">
    <input class="fc-input fc-env-name" id="fcEnvNewName" type="text" placeholder="${esc(t('fc_env_name'))}" autocomplete="off" spellcheck="false">
    <input class="fc-input" id="fcEnvNewValue" type="password" placeholder="${esc(t('fc_env_value'))}" autocomplete="new-password" data-1p-ignore data-lpignore="true">
    <button type="button" class="fc-btn fc-btn-primary" onclick="fullConfigEnvAdd()">${esc(t('fc_env_add'))}</button>
    <div class="fc-feedback" id="fcEnvAddFeedback" aria-live="polite"></div>
  </div>`;
  body.innerHTML = add + (html.join('') || `<div class="fc-empty">${esc(t('fc_no_results'))}</div>`);
  body.querySelectorAll('details.fc-section[open] .fc-section-body').forEach(el => { el.dataset.rendered = '1'; });
  _fcBindGroups(body, id => (visible[id.replace(/^env:/, '')] || []).map(_fcEnvRow).join(''));
  _fcRenderNav(nav, groups);
}

function _fcFindEnv(name){
  for (const cat of (_fc.env && _fc.env.categories) || []) {
    const v = cat.vars.find(x => x.name === name);
    if (v) return v;
  }
  return null;
}

function _fcEnvRow(v){
  const name = esc(v.name);
  const badges = [
    v.is_set ? `<span class="fc-badge fc-badge-changed">${esc(t('fc_env_set_badge'))}</span>` : '',
    v.secret ? `<span class="fc-badge">${esc(t('fc_secret_badge'))}</span>` : '',
    v.advanced ? `<span class="fc-badge">${esc(t('fc_advanced'))}</span>` : '',
    !v.known ? `<span class="fc-badge">${esc(t('fc_custom_badge'))}</span>` : '',
  ].join('');
  const desc = v.description || v.prompt;
  const url = /^https?:\/\//i.test(v.url || '') ? v.url : '';
  const input = v.secret
    ? `<input class="fc-input" type="password" data-fc-env="${name}" autocomplete="new-password" data-1p-ignore data-lpignore="true"
        placeholder="${esc(v.is_set ? (v.masked + ' — ' + t('fc_env_secret_placeholder')) : t('fc_env_value'))}"
        onkeydown="if(event.key==='Enter')fullConfigEnvSave(this.dataset.fcEnv)">`
    : `<input class="fc-input" type="text" data-fc-env="${name}" value="${esc(v.value || '')}" autocomplete="off" spellcheck="false"
        onkeydown="if(event.key==='Enter')fullConfigEnvSave(this.dataset.fcEnv)">`;
  return `<div class="fc-row${v.is_set ? ' is-set' : ''}" data-fc-env-row="${name}">
    <div class="fc-row-head"><code class="fc-path">${name}</code>${badges}
      ${url ? `<a class="fc-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(t('fc_env_docs'))} ↗</a>` : ''}</div>
    ${desc ? `<div class="fc-desc">${esc(desc)}</div>` : ''}
    ${v.help ? `<div class="fc-hint">${esc(v.help)}</div>` : ''}
    <div class="fc-row-edit">
      ${input}
      <div class="fc-row-actions">
        <button type="button" class="fc-btn fc-btn-primary" onclick="fullConfigEnvSave(this.closest('[data-fc-env-row]').dataset.fcEnvRow)">${esc(t('fc_save'))}</button>
        ${v.is_set ? `<button type="button" class="fc-btn" onclick="fullConfigEnvUnset(this.closest('[data-fc-env-row]').dataset.fcEnvRow)">${esc(t('fc_env_unset'))}</button>` : ''}
      </div>
    </div>
    <div class="fc-feedback" aria-live="polite"></div>
  </div>`;
}

function _fcEnvRowEl(name){
  return Array.from(document.querySelectorAll('#fullConfigBody [data-fc-env-row]')).find(el => el.dataset.fcEnvRow === name) || null;
}

function _fcReplaceEnv(entry){
  if (!entry || !_fc.env) return false;
  for (const cat of _fc.env.categories) {
    const i = cat.vars.findIndex(x => x.name === entry.name);
    if (i >= 0) {
      if (cat.vars[i].is_set !== entry.is_set) _fc.env.set += entry.is_set ? 1 : -1;
      cat.vars[i] = entry;
      return true;
    }
  }
  return false;
}

function _fcRerenderEnvRow(name, ok, msg){
  const v = _fcFindEnv(name);
  const row = _fcEnvRowEl(name);
  if (v && row) {
    const tmp = document.createElement('div');
    tmp.innerHTML = _fcEnvRow(v);
    row.replaceWith(tmp.firstElementChild);
  }
  _fcFeedback(_fcEnvRowEl(name), ok, msg);
  const status = _fcEl('fullConfigStatus');
  if (status && _fc.env) status.textContent = t('fc_env_summary', _fc.env.total, _fc.env.set) + (_fc.env.env_path ? ` · ${_fc.env.env_path}` : '');
}

async function fullConfigEnvSave(name){
  const row = _fcEnvRowEl(name);
  const input = row && row.querySelector('[data-fc-env]');
  if (!input) return;
  const value = input.value;
  if (!value) { _fcFeedback(row, false, t('fc_env_empty')); return; }
  try {
    const res = await api('/api/full-config/env/set', {method: 'POST', body: JSON.stringify({name, value, profile: _fcServerProfile('env')})});
    _fcReplaceEnv(res.entry);
    _fcRerenderEnvRow(name, true, t('fc_saved'));
  } catch (e) {
    if (_fcHandleWriteError(e)) return;
    _fcFeedback(row, false, e.message || String(e));
  }
}

async function fullConfigEnvUnset(name){
  const ok = await showConfirmDialog({title: t('fc_env_unset'), message: t('fc_confirm_unset', name), confirmLabel: t('fc_env_unset'), danger: true, focusCancel: true});
  if (!ok) return;
  try {
    const res = await api('/api/full-config/env/unset', {method: 'POST', body: JSON.stringify({name, profile: _fcServerProfile('env')})});
    if (res.entry && !res.entry.known && !res.entry.is_set) await loadFullConfig(true);
    else { _fcReplaceEnv(res.entry); _fcRerenderEnvRow(name, true, t('fc_env_removed')); }
  } catch (e) {
    if (_fcHandleWriteError(e)) return;
    _fcFeedback(_fcEnvRowEl(name), false, e.message || String(e));
  }
}

async function fullConfigEnvAdd(){
  const nameEl = _fcEl('fcEnvNewName'), valueEl = _fcEl('fcEnvNewValue'), fb = _fcEl('fcEnvAddFeedback');
  const name = (nameEl && nameEl.value || '').trim();
  const value = valueEl ? valueEl.value : '';
  const show = (ok, msg) => { if (fb) { fb.textContent = msg; fb.className = 'fc-feedback ' + (ok ? 'ok' : 'err'); } };
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) { show(false, t('fc_env_bad_name')); return; }
  if (!value) { show(false, t('fc_env_empty')); return; }
  try {
    const res = await api('/api/full-config/env/set', {method: 'POST', body: JSON.stringify({name, value, profile: _fcServerProfile('env')})});
    if (!_fcReplaceEnv(res.entry)) await loadFullConfig(true);
    else _fcRender();
    showToast(t('fc_saved'));
  } catch (e) {
    if (_fcHandleWriteError(e)) return;
    show(false, e.message || String(e));
  }
}
