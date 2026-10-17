/**
 * avatar/src/renderer/settings.js
 * Settings window logic. Provider fields are built from the backend's
 * provider list (config.yaml), never hardcoded. Key and PIN fields are
 * masked, only sent when filled in, and cleared after saving.
 */

(function init() {
  const api = window.kiraSettings;
  const $ = (id) => document.getElementById(id);
  let loaded = null;

  function el(tag, attrs = {}, text = '') {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text) node.textContent = text;
    return node;
  }

  function setStatus(node, text, ok) {
    node.textContent = text;
    node.className = `status ${ok === true ? 'ok' : ok === false ? 'error' : ''}`.trim();
  }

  function testButton(providerName, input, status) {
    const test = el('button', { type: 'button' }, 'Test');
    test.addEventListener('click', async () => {
      setStatus(status, 'Testing…');
      const res = await api.testKey(providerName, input ? input.value : '');
      setStatus(status, res.message || (res.ok ? 'OK' : 'Failed'), Boolean(res.ok));
    });
    return test;
  }

  function renderProviders(providers) {
    const box = $('providers');
    box.replaceChildren();
    for (const p of providers) {
      const wrap = el('div', { class: 'provider' });
      wrap.append(el('h2', {}, p.name));
      const status = el('div', { class: 'status' });
      const row = el('div', { class: 'row' });

      if (p.api_key_env) {
        const keyId = `key-${p.name}`;
        wrap.append(el('label', { for: keyId }, `API key (${p.api_key_env})`));
        const input = el('input', {
          id: keyId, type: 'password', autocomplete: 'off', spellcheck: 'false', class: 'grow',
          placeholder: p.key_set ? `saved ${p.key_masked}, leave blank to keep` : 'not set'
        });
        input.dataset.env = p.api_key_env;
        row.append(input, testButton(p.name, input, status));
      } else {
        row.append(el('p', { class: 'grow' }, 'Runs locally, no key needed.'), testButton(p.name, null, status));
      }
      wrap.append(row);

      const modelId = `model-${p.name}`;
      wrap.append(el('label', { for: modelId }, 'Model'));
      const model = el('input', { id: modelId, type: 'text', maxlength: '128', spellcheck: 'false' });
      model.value = p.model || '';
      model.dataset.provider = p.name;
      wrap.append(model, status);
      box.append(wrap);
    }
  }

  function fillSelect(select, options, value) {
    select.replaceChildren(...options.map(([val, label]) => {
      const o = el('option', { value: val }, label);
      if (val === value) o.selected = true;
      return o;
    }));
  }

  async function load() {
    const data = await api.get();
    loaded = data;
    const b = data.backend || {};
    if (!b.ok) {
      setStatus($('save-status'), 'Could not read settings: ' + (b.errors || []).join(', '), false);
      return;
    }
    renderProviders(b.providers || []);
    $('pin').value = '';
    $('pin').placeholder = b.pin_set ? 'set, leave blank to keep' : 'not set';
    $('pin-hint').textContent = b.pin_set ? '' : 'Free mode stays disabled until you set a PIN.';
    $('free-timeout').value = Math.round((b.free_revert_timeout || 600) / 60);
    $('edge-voice').value = b.voice?.edge_voice || '';
    $('ptt').value = b.voice?.hotkey || '';
    fillSelect($('persona'), (b.personas || []).map((p) => [p, p.charAt(0).toUpperCase() + p.slice(1)]), b.persona);
    $('start-with-windows').checked = Boolean(data.avatar?.startWithWindows);
    $('hotkey').value = data.avatar?.showHideHotkey || '';

    fillSelect($('mic'), [['', 'System default']], '');
    const mics = await api.listMics();
    const current = typeof b.voice?.input_device === 'string' ? b.voice.input_device : '';
    const opts = [['', 'System default'], ...((mics.mics || []).map((m) => [m.name, m.name + (m.default ? ' (default)' : '')]))];
    fillSelect($('mic'), opts, current);
  }

  /** Collect only what changed; secrets only when typed in. */
  function collect() {
    const b = loaded?.backend || {};
    const backend = {};
    const keys = {};
    document.querySelectorAll('input[data-env]').forEach((i) => { if (i.value) keys[i.dataset.env] = i.value; });
    if (Object.keys(keys).length) backend.keys = keys;
    if ($('pin').value) backend.pin = $('pin').value;

    const models = {};
    document.querySelectorAll('input[data-provider]').forEach((i) => {
      const before = (b.providers || []).find((p) => p.name === i.dataset.provider)?.model;
      if (i.value.trim() && i.value.trim() !== before) models[i.dataset.provider] = i.value.trim();
    });
    if (Object.keys(models).length) backend.models = models;

    const voice = {};
    if ($('edge-voice').value.trim() !== (b.voice?.edge_voice || '')) voice.edge_voice = $('edge-voice').value.trim();
    if ($('ptt').value.trim() !== (b.voice?.hotkey || '')) voice.hotkey = $('ptt').value.trim();
    const mic = $('mic').value || null;
    const micBefore = typeof b.voice?.input_device === 'string' ? b.voice.input_device : null;
    if (mic !== micBefore) voice.input_device = mic;
    if (Object.keys(voice).length) backend.voice = voice;

    const minutes = Number($('free-timeout').value);
    if (Number.isFinite(minutes) && minutes > 0 && Math.round(minutes * 60) !== b.free_revert_timeout) {
      backend.free_revert_timeout = Math.round(minutes * 60);
    }
    if ($('persona').value && $('persona').value !== b.persona) backend.persona = $('persona').value;

    const avatar = {
      startWithWindows: $('start-with-windows').checked,
      showHideHotkey: $('hotkey').value.trim()
    };
    return { backend, avatar };
  }

  $('save').addEventListener('click', async () => {
    const status = $('save-status');
    setStatus(status, 'Saving…');
    const res = await api.save(collect());
    // Clear secret fields whatever the result
    document.querySelectorAll('input[type=password]').forEach((i) => { i.value = ''; });
    if (!res.ok) {
      setStatus(status, (res.errors || ['Could not save']).join('; '), false);
      return;
    }
    setStatus(status, res.needsRestart ? 'Saved. Some changes apply after "Restart backend".' : 'Saved.', true);
    await load();
  });

  $('restart').addEventListener('click', async () => {
    setStatus($('save-status'), 'Restarting backend…');
    const res = await api.restartBackend();
    const msg = {
      started: 'Backend restarted.',
      external: 'The backend was started outside the app (run.bat). Restart it there.',
      'missing-python': 'Python .venv not found. See README Quick Setup.'
    };
    setStatus($('save-status'), msg[res.status] || 'Restart requested.', res.status === 'started');
  });

  $('run-doctor').addEventListener('click', async () => {
    const list = $('doctor-list');
    list.replaceChildren(el('li', {}, 'Running…'));
    const res = await api.doctor();
    if (!res.checks) {
      list.replaceChildren(el('li', {}, 'Doctor could not run: ' + (res.errors || []).join(', ')));
      return;
    }
    list.replaceChildren(...res.checks.map((c) => {
      const li = el('li');
      li.append(el('span', { class: `tag ${c.status}` }, c.status.toUpperCase()),
        document.createTextNode(`${c.name}: ${c.detail}`));
      if (c.status !== 'ok' && c.fix) li.append(el('span', { class: 'fix' }, c.fix));
      return li;
    }));
  });

  load();
})();
