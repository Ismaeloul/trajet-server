/* Panel de Trajet: sin dependencias ni paso de compilación.
 *
 * Reglas de este fichero (la CSP es default-src 'self'):
 *  - Nada de estilos ni scripts en línea: los estilos van en panel.css y lo
 *    único que se toca desde aquí son clases, atributos data-* y variables CSS
 *    con style.setProperty (el CSSOM no lo bloquea la CSP).
 *  - Todo lo que llega del servidor entra en el DOM como TEXTO (textContent):
 *    nombres de dispositivos, mensajes de error, avisos… nunca como HTML.
 *  - El QR (SVG que genera el servidor) se enseña como <img src="data:…">:
 *    una imagen SVG no puede ejecutar nada, así que no hace falta sanearlo.
 *  - La clave de PRIM no se guarda en ninguna variable de la página: se lee
 *    del campo, el campo se vacía al momento y solo viaja en la petición.
 *  - Lo que modifica lleva `X-Trajet-Panel: 1` (anti-CSRF, lo exige el servidor).
 *  - Refresco cada 15 s solo con la pestaña a la vista. Si la red falla se
 *    avisa discretamente y se sigue enseñando lo último que se supo.
 */
'use strict';

(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const SPRITE = '/panel/static/iconos.svg';
  const REFRESH_MS = 15000;
  const PAIR_POLL_MS = 2000;

  const state = {
    ov: null, devices: null, quota: null, errors: null, settings: null,
    offline: false, lastRefresh: 0,
  };

  // ------------------------------------------------------------------ DOM
  const $ = (id) => document.getElementById(id);

  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = String(v);
      else if (k === 'data') Object.entries(v).forEach(([dk, dv]) => { n.dataset[dk] = String(dv); });
      else if (k === 'on') Object.entries(v).forEach(([ev, fn]) => n.addEventListener(ev, fn));
      else n.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }

  function icon(name, cls) {
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', cls ? `ico ${cls}` : 'ico');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const use = document.createElementNS(NS, 'use');
    use.setAttribute('href', `${SPRITE}#${name}`);
    svg.append(use);
    return svg;
  }

  const setText = (id, value) => { const n = $(id); if (n) n.textContent = value; };
  const show = (id, on) => { const n = typeof id === 'string' ? $(id) : id; if (n) n.hidden = !on; };

  function setChip(id, label, tone) {
    const chip = $(id);
    if (!chip) return;
    chip.dataset.tone = tone;
    const span = chip.querySelector('span');
    (span || chip).textContent = label;
  }

  function setBar(bar, fraction) {
    const p = Math.max(0, Math.min(1, Number(fraction) || 0));
    bar.style.setProperty('--p', p.toFixed(4));
  }

  function busy(btn, on, label) {
    if (!btn) return;
    const lbl = btn.querySelector('span');
    if (on) {
      if (lbl && label) { btn.dataset.label = lbl.textContent; lbl.textContent = label; }
      btn.disabled = true;
      btn.classList.add('is-busy');
      btn.setAttribute('aria-busy', 'true');
    } else {
      if (lbl && btn.dataset.label) { lbl.textContent = btn.dataset.label; delete btn.dataset.label; }
      btn.disabled = false;
      btn.classList.remove('is-busy');
      btn.removeAttribute('aria-busy');
    }
  }

  function fieldError(input, errorEl, message) {
    const err = typeof errorEl === 'string' ? $(errorEl) : errorEl;
    const inp = typeof input === 'string' ? $(input) : input;
    if (err) { err.textContent = message || ''; err.hidden = !message; }
    if (inp) {
      if (message) inp.setAttribute('aria-invalid', 'true');
      else inp.removeAttribute('aria-invalid');
    }
  }

  // ------------------------------------------------------------------ formato
  let skew = 0;                       // reloj del servidor - reloj del navegador (ms)
  const now = () => Date.now() + skew;
  const LOCALE = 'es-ES';
  const fmtInt = new Intl.NumberFormat(LOCALE);
  const fmtDec = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 1 });
  const fmtPct = new Intl.NumberFormat(LOCALE, { style: 'percent', maximumFractionDigits: 0 });
  const fmtDateTime = new Intl.DateTimeFormat(LOCALE, {
    day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  const fmtTime = new Intl.DateTimeFormat(LOCALE, { hour: '2-digit', minute: '2-digit' });
  const fmtTimeS = new Intl.DateTimeFormat(LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const fmtWeekday = new Intl.DateTimeFormat(LOCALE, { weekday: 'narrow', timeZone: 'UTC' });
  const fmtDayLong = new Intl.DateTimeFormat(LOCALE, { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' });
  const rtf = new Intl.RelativeTimeFormat('es', { numeric: 'auto', style: 'short' });

  const int = (n) => (n === null || n === undefined ? '—' : fmtInt.format(n));
  const plural = (n, one, many) => `${int(n)} ${n === 1 ? one : many}`;

  function ago(iso) {
    const t = Date.parse(iso || '');
    if (Number.isNaN(t)) return '—';
    const s = (t - now()) / 1000;
    const a = Math.abs(s);
    if (a < 45) return s <= 0 ? 'hace unos segundos' : 'en unos segundos';
    if (a < 3600) return rtf.format(Math.round(s / 60), 'minute');
    if (a < 86400) return rtf.format(Math.round(s / 3600), 'hour');
    if (a < 7 * 86400) return rtf.format(Math.round(s / 86400), 'day');
    return fmtDateTime.format(t);
  }

  function timeEl(iso) {
    const t = Date.parse(iso || '');
    return el('time', { datetime: iso || undefined, title: Number.isNaN(t) ? undefined : fmtDateTime.format(t),
      text: ago(iso) });
  }

  function duration(sec) {
    sec = Math.max(0, Math.round(Number(sec) || 0));
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d) return `${d} d ${h} h`;
    if (h) return `${h} h ${m} min`;
    if (m) return `${m} min`;
    return `${sec} s`;
  }

  function bytes(n) {
    if (n === null || n === undefined) return '—';
    if (n < 1024 * 1024) return `${fmtDec.format(n / 1024)} KB`;
    if (n < 1024 ** 3) return `${fmtDec.format(n / 1024 ** 2)} MB`;
    return `${fmtDec.format(n / 1024 ** 3)} GB`;
  }

  // ------------------------------------------------------------------ API
  class NetError extends Error {
    constructor(message, kind) { super(message); this.kind = kind || 'net'; }
  }

  async function api(method, path, body, headers) {
    const opts = { method, credentials: 'same-origin', cache: 'no-store',
      headers: Object.assign({ Accept: 'application/json' }, headers || {}) };
    if (method !== 'GET') opts.headers['X-Trajet-Panel'] = '1';
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new NetError('No hay conexión con el servidor.');
    } finally {
      opts.body = undefined;
    }
    const date = Date.parse(res.headers.get('date') || '');
    if (!Number.isNaN(date)) {
      // La cabecera Date va al segundo: solo se corrige un desfase claro.
      const d = date - Date.now();
      skew = Math.abs(d) > 2000 ? d : 0;
    }
    const type = res.headers.get('content-type') || '';
    if (!type.includes('application/json')) {
      // El login de Umbrel responde con su página (tras una redirección)
      // cuando la sesión caduca.
      if (res.redirected || type.includes('text/html')) {
        throw new NetError('La sesión de Umbrel ha caducado: recarga la página para volver a entrar.', 'session');
      }
      throw new NetError(`Respuesta inesperada del servidor (HTTP ${res.status}).`);
    }
    let data;
    try { data = await res.json(); } catch (e) { throw new NetError('La respuesta del servidor no se puede leer.'); }
    return { ok: res.ok, status: res.status, data };
  }

  const errText = (r) => (r && r.data && r.data.error && r.data.error.message) || `Error HTTP ${r ? r.status : '?'}`;

  // ------------------------------------------------------------------ avisos flotantes
  let toastTimer = 0;
  function toast(message, tone) {
    const t = $('toast');
    t.textContent = message;
    t.dataset.tone = tone || 'ok';
    t.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('is-on'), 4500);
  }

  // ------------------------------------------------------------------ confirmación
  function confirmDialog({ title, body, ok, typed }) {
    const dlg = $('dlg');
    const input = $('dlg-input');
    const okBtn = $('dlg-ok');
    const cancelBtn = $('dlg-cancel');
    if (typeof dlg.showModal !== 'function') {
      return Promise.resolve(false);           // navegador muy viejo: mejor no hacer nada
    }
    return new Promise((resolve) => {
      let settled = false;
      setText('dlg-title', title);
      setText('dlg-text', body);
      okBtn.textContent = ok;
      show('dlg-typed', Boolean(typed));
      input.value = '';
      okBtn.disabled = Boolean(typed);
      const valid = () => !typed || input.value.trim().toLowerCase() === typed;
      const onInput = () => { okBtn.disabled = !valid(); };
      const onKey = (ev) => { if (ev.key === 'Enter' && valid()) { ev.preventDefault(); finish(true); } };
      const onOk = () => { if (valid()) finish(true); };
      const onCancel = () => finish(false);
      const onClose = () => finish(false);
      function finish(value) {
        if (settled) return;
        settled = true;
        input.removeEventListener('input', onInput);
        input.removeEventListener('keydown', onKey);
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        dlg.removeEventListener('close', onClose);
        if (dlg.open) dlg.close();
        input.value = '';
        resolve(value);
      }
      input.addEventListener('input', onInput);
      input.addEventListener('keydown', onKey);
      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      dlg.addEventListener('close', onClose);
      dlg.showModal();
      (typed ? input : cancelBtn).focus();
    });
  }

  // ------------------------------------------------------------------ red
  function setOnline(ok, message, kind) {
    state.offline = !ok;
    if (ok) {
      show('net', false);
    } else {
      setText('net-text', message || 'Sin conexión con el servidor.');
      show('net-retry', kind !== 'session');
      show('net-reload', kind === 'session');
      show('net', true);
    }
    renderStatePill();
  }

  // ------------------------------------------------------------------ refresco
  let refreshing = null;
  let refreshTimer = 0;

  function schedule() {
    clearTimeout(refreshTimer);
    if (document.visibilityState === 'visible') refreshTimer = setTimeout(refresh, REFRESH_MS);
  }

  function refresh() {
    if (refreshing) return refreshing;
    const btn = $('refresh');
    btn.classList.add('is-spinning');
    refreshing = (async () => {
      try {
        const [ov, dv, qt, er] = await Promise.all([
          api('GET', '/api/admin/overview'),
          api('GET', '/api/admin/devices'),
          api('GET', '/api/admin/quota'),
          api('GET', '/api/admin/errors?limit=50'),
        ]);
        if (ov.ok) { state.ov = ov.data; renderOverview(); }
        if (dv.ok) { state.devices = dv.data.devices; renderDevices(); }
        if (qt.ok) { state.quota = qt.data; renderQuota(); }
        if (er.ok) { state.errors = er.data.errors; renderErrors(); }
        const bad = [ov, dv, qt, er].find((r) => !r.ok);
        if (bad) setOnline(false, `El servidor no deja leer el panel: ${errText(bad)}`, 'server');
        else setOnline(true);
        state.lastRefresh = Date.now();
        setText('updated', `actualizado a las ${fmtTimeS.format(new Date())}`);
        if (!state.settings) loadSettings();
      } catch (e) {
        setOnline(false, `${e.message} Se enseña lo último que se supo.`, e.kind);
      } finally {
        btn.classList.remove('is-spinning');
        refreshing = null;
        schedule();
      }
    })();
    return refreshing;
  }

  // ------------------------------------------------------------------ resumen
  const KEY_STATE = {
    valid: ['Válida', 'ok'], invalid: ['Inválida', 'bad'], forbidden: ['Sin permisos', 'bad'],
    quota_exhausted: ['Cuota agotada', 'warn'], unreachable: ['PRIM caído', 'warn'],
    unknown: ['Sin comprobar', 'neutral'], missing: ['Sin clave', 'bad'],
  };
  const QUOTA_LEVEL = {
    ok: ['Normal', 'ok'], warn: ['Justa', 'warn'], critical: ['Casi agotada', 'warn'], exhausted: ['Agotada', 'bad'],
  };
  const API_NAME = {
    'stop-monitoring': 'Horarios en tiempo real',
    'general-message': 'Avisos de tráfico',
    navitia: 'Buscador e itinerarios',
  };
  const WARN_LEVEL = { error: 'Error', warn: 'Aviso', info: 'Info' };

  function renderStatePill() {
    const pill = $('state-pill');
    let tone = 'loading';
    let label = 'Cargando…';
    if (state.offline && state.ov) { tone = 'warn'; label = 'Sin conexión'; }
    else if (state.offline) { tone = 'bad'; label = 'Sin conexión'; }
    else if (state.ov) {
      const ws = state.ov.warnings || [];
      if (ws.some((w) => w.level === 'error')) { tone = 'bad'; label = 'Revisar'; }
      else if (ws.some((w) => w.level === 'warn')) { tone = 'warn'; label = 'Con avisos'; }
      else { tone = 'ok'; label = 'En marcha'; }
    }
    pill.dataset.tone = tone;
    setText('state-text', label);
  }

  function spent(ep) {
    const cap = Math.max(1, ep.cap);
    let rem = cap - ep.used;
    if (ep.remaining_reported !== null && ep.remaining_reported !== undefined) rem = Math.min(rem, ep.remaining_reported);
    rem = Math.max(0, rem);
    return { fraction: (cap - rem) / cap, remaining: rem };
  }

  function renderOverview() {
    const ov = state.ov;
    setText('version', `versión ${ov.version}`);
    renderStatePill();
    renderWarnings(ov.warnings || []);

    // Estado general
    const [kLabel, kTone] = KEY_STATE[ov.prim_key.state] || [ov.prim_key.state, 'neutral'];
    $('sum-key').dataset.tone = kTone;
    setText('sum-key-v', kLabel);
    setText('sum-key-s', ov.prim_key.last4 ? `termina en ${ov.prim_key.last4}` : 'hay que pegarla');
    const q = (state.quota && state.quota.today) || ov.quota;
    renderQuotaSummary(q);
    setText('sum-dev-v', int(ov.devices));
    setText('sum-dev-s', ov.devices === 1 ? 'emparejado' : 'emparejados');
    setText('sum-up-v', duration(ov.uptime_s));
    setText('sum-up-s', `versión ${ov.version}`);

    renderKey(ov.prim_key);
    renderHealth(ov);
    renderTranslator(ov.translator);
    renderPlatform(ov);
    if (!state.quota) renderQuotaMeters(ov.quota);
  }

  function renderWarnings(ws) {
    const list = $('warning-list');
    list.replaceChildren(...ws.map((w) => el('li', { data: { level: w.level } },
      el('span', { class: 'w-ico' }, icon(w.level === 'info' ? 'info' : 'warn')),
      el('p', {}, el('span', { class: 'w-level', text: `${WARN_LEVEL[w.level] || w.level}: ` }), w.text))));
    show('warnings', ws.length > 0);
  }

  // ------------------------------------------------------------------ clave PRIM
  let keyFormOpen = false;

  function renderKey(k) {
    const [label, tone] = KEY_STATE[k.state] || [k.state, 'neutral'];
    setChip('key-chip', label, tone);
    show('key-missing', !k.configured);
    show('key-facts', k.configured);
    setText('key-last4', k.last4 ? `•••• ${k.last4}` : '—');
    setText('key-source', { panel: 'Guardada en el panel', env: 'Variable PRIM_API_KEY', none: 'Ninguna' }[k.source] || k.source);
    const saved = $('key-saved');
    if (k.saved_at) saved.replaceChildren(timeEl(k.saved_at));
    else saved.textContent = k.source === 'env' ? 'En el entorno del contenedor' : '—';
    const checked = $('key-checked');
    if (k.checked_at) checked.replaceChildren(timeEl(k.checked_at));
    else checked.textContent = 'Todavía no';
    setText('key-enc', k.source !== 'panel' ? 'No aplica'
      : { app_seed: 'AES-256-GCM con APP_SEED', local_master_key: 'AES-256-GCM con clave local (menos segura)',
        none: '—' }[k.encryption] || k.encryption);
    // Sin clave, el recuadro de arriba ya lo explica: no se repite.
    setText('key-detail', k.configured ? (k.state_detail || '') : '');
    show('key-delete', k.source === 'panel');
    show('key-check', k.configured);
    show('key-replace', k.configured && !keyFormOpen);
    show('key-cancel', k.configured);
    if (!k.configured && !keyFormOpen) openKeyForm(false);
  }

  function openKeyForm(focus) {
    keyFormOpen = true;
    show('key-form', true);
    show('key-replace', false);
    if (focus) $('key-input').focus();
  }

  function closeKeyForm() {
    keyFormOpen = false;
    const input = $('key-input');
    input.value = '';
    setKeyVisible(false);
    fieldError(input, 'key-error', '');
    show('key-form', false);
    if (state.ov && state.ov.prim_key.configured) show('key-replace', true);
  }

  function setKeyVisible(on) {
    const input = $('key-input');
    input.type = on ? 'text' : 'password';
    const btn = $('key-show');
    btn.setAttribute('aria-pressed', String(on));
    btn.setAttribute('aria-label', on ? 'Ocultar la clave' : 'Mostrar la clave');
    $('key-show-ico').setAttribute('href', `${SPRITE}#${on ? 'eye-off' : 'eye'}`);
  }

  function renderKeyResult(res) {
    const head = $('key-result-head');
    let tone = 'ok';
    let message;
    if (res.saved) message = 'Guardada y en uso: el iPhone ya la está usando.';
    else if (res.error) {
      tone = res.error.code === 'prim_key_rejected' ? 'bad' : 'warn';
      message = `No se ha guardado: ${res.error.message}.`;
      if (res.error.code === 'prim_key_missing') message = res.error.message;
    } else message = 'La clave en uso responde bien en las tres APIs.';
    head.dataset.tone = tone;
    head.replaceChildren(icon(tone === 'ok' ? 'check' : 'warn'), el('span', { text: message }));
    $('key-checks').replaceChildren(...(res.checks || []).map((c) => el('li', { data: { ok: c.ok } },
      el('span', { class: 'check-ico' }, icon(c.ok ? 'check' : 'x')),
      el('span', { class: 'check-name' }, `${API_NAME[c.api] || c.api} `,
        el('small', { text: `${c.api} · ${c.status === null ? 'sin respuesta' : `HTTP ${c.status}`}` })),
      el('span', { class: 'check-msg', text: c.message }))));
    show('key-result', true);
  }

  function checkKeyText(key) {
    if (!key) return 'Pega la clave primero.';
    if (key.length < 8 || key.length > 256) return 'La clave tiene que tener de 8 a 256 caracteres.';
    if (/[^\x21-\x7e]/.test(key)) return 'La clave no puede tener espacios ni saltos de línea: vuelve a copiarla del portal de PRIM.';
    return '';
  }

  async function submitKey(ev) {
    ev.preventDefault();
    const input = $('key-input');
    const submit = $('key-submit');
    // La clave se lee y el campo se vacía al momento: no se queda en el DOM
    // ni aunque la prueba falle o la red se caiga.
    const problem = checkKeyText(input.value.trim());
    if (problem) {
      input.value = '';
      fieldError(input, 'key-error', problem);
      input.focus();
      return;
    }
    const body = { key: input.value.trim() };
    input.value = '';
    setKeyVisible(false);
    fieldError(input, 'key-error', '');
    busy(submit, true, 'Probando con PRIM…');
    show('key-result', false);
    let r = null;
    try {
      r = await api('POST', '/api/admin/prim-key', body);
    } catch (e) {
      fieldError(input, 'key-error', `${e.message} No se ha guardado nada.`);
    } finally {
      body.key = '';
      busy(submit, false);
    }
    if (!r) return;
    if (r.status === 200 || r.status === 422) {
      renderKeyResult(r.data);
      if (state.ov) state.ov.prim_key = r.data.info;
      if (r.data.saved) {
        closeKeyForm();
        renderKey(r.data.info);
        toast('Clave guardada y en uso.');
        refresh();
      } else {
        renderKey(r.data.info);
        input.focus();
      }
    } else {
      fieldError(input, 'key-error', errText(r));
      input.focus();
    }
  }

  async function recheckKey() {
    const btn = $('key-check');
    busy(btn, true, 'Comprobando…');
    try {
      const r = await api('POST', '/api/admin/prim-key/check');
      if (r.ok) {
        renderKeyResult(r.data);
        if (state.ov) state.ov.prim_key = r.data.info;
        renderKey(r.data.info);
      } else toast(errText(r), 'bad');
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      busy(btn, false);
    }
  }

  async function deleteKey() {
    const envAvailable = state.ov && state.ov.prim_key.env_available;
    const ok = await confirmDialog({
      title: '¿Borrar la clave del panel?',
      body: envAvailable
        ? 'Se borra la guardada en el panel y el servidor pasa a usar la de PRIM_API_KEY del entorno.'
        : 'No hay otra clave en el entorno: el servidor se quedará sin clave y el iPhone sin horarios hasta que guardes otra.',
      ok: 'Borrar la clave',
      typed: 'borrar',
    });
    if (!ok) return;
    const btn = $('key-delete');
    busy(btn, true);
    try {
      const r = await api('DELETE', '/api/admin/prim-key', undefined, { 'X-Trajet-Confirm': 'borrar' });
      if (r.ok) {
        if (state.ov) state.ov.prim_key = r.data;
        show('key-result', false);
        renderKey(r.data);
        toast(r.data.source === 'env' ? 'Clave borrada: ahora se usa la del entorno.' : 'Clave borrada: el servidor no tiene clave.');
        refresh();
      } else toast(errText(r), 'bad');
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      busy(btn, false);
    }
  }

  // ------------------------------------------------------------------ cuota
  function renderQuotaSummary(q) {
    if (!q) return;
    const [label, tone] = QUOTA_LEVEL[q.level] || [q.level, 'neutral'];
    const worst = Math.max(...q.endpoints.map((ep) => spent(ep).fraction));
    $('sum-quota').dataset.tone = tone;
    setText('sum-quota-v', fmtPct.format(worst));
    const resets = Date.parse(q.resets_at);
    setText('sum-quota-s', label);
    $('sum-quota').title = Number.isNaN(resets) ? '' : `Se reinicia a las ${fmtTime.format(resets)} (medianoche UTC)`;
  }

  function renderQuotaMeters(q) {
    if (!q) return;
    const [label, tone] = QUOTA_LEVEL[q.level] || [q.level, 'neutral'];
    setChip('quota-chip', label, tone);
    const resets = Date.parse(q.resets_at);
    if (!Number.isNaN(resets)) {
      const left = Math.max(0, (resets - now()) / 1000);
      setText('quota-reset', `Se reinicia a las ${fmtTime.format(resets)} (medianoche UTC), dentro de ${duration(left)}.`);
    }
    $('quota-meters').replaceChildren(...q.endpoints.map((ep) => {
      const s = spent(ep);
      const bar = el('i');
      setBar(bar, s.fraction);
      const [lvl] = QUOTA_LEVEL[ep.level] || [ep.level];
      const reported = ep.remaining_reported === null || ep.remaining_reported === undefined
        ? 'PRIM aún no ha dicho cuántas quedan'
        : `PRIM dice que quedan ${int(ep.remaining_reported)}`;
      return el('li', { class: 'meter', data: { level: ep.level } },
        el('div', { class: 'meter-top' },
          el('span', { class: 'meter-name' }, API_NAME[ep.endpoint] || ep.endpoint, el('small', { text: ep.endpoint })),
          el('span', { class: 'meter-val' }, el('b', { class: 'num', text: int(ep.used) }), ` / ${int(ep.cap)}`)),
        el('span', { class: 'track', role: 'img', 'aria-label': `${fmtPct.format(s.fraction)} gastado` }, bar),
        el('p', { class: 'meter-foot', text: `${lvl} · ${reported}` }));
    }));
  }

  const SHORT_NAME = { 'stop-monitoring': 'Horarios', 'general-message': 'Avisos', navitia: 'Buscador' };

  function renderQuotaHistory(history, today, cap0) {
    const table = $('quota-history');
    const days = [...new Set(history.map((h) => h.day_utc))].sort();
    const eps = ['stop-monitoring', 'general-message', 'navitia'];
    const by = {};
    history.forEach((h) => { by[`${h.day_utc}|${h.endpoint}`] = h.used; });
    // Altura = parte del tope diario (1000): se ve lo cerca que se estuvo del
    // límite cada día, con la misma escala en las tres filas.
    const cap = Math.max(1, cap0 || 1000);
    const dayDate = (d) => new Date(`${d}T12:00:00Z`);
    const headRow = el('tr', {}, el('td'),
      ...days.map((d) => el('th', { scope: 'col', class: d === today ? 'is-today' : undefined,
        title: fmtDayLong.format(dayDate(d)) },
      d === today ? 'hoy' : fmtWeekday.format(dayDate(d)))));
    table.tHead.replaceChildren(headRow);
    table.tBodies[0].replaceChildren(...eps.map((ep) => el('tr', {},
      el('th', { scope: 'row', title: `${API_NAME[ep] || ep} (${ep})`, text: SHORT_NAME[ep] || ep }),
      ...days.map((d) => {
        const n = by[`${d}|${ep}`] || 0;
        const bar = el('span', { class: 'spark', 'aria-hidden': 'true' });
        bar.style.setProperty('--h', Math.min(1, n / cap).toFixed(4));
        return el('td', { class: d === today ? 'is-today' : undefined,
          title: `${fmtDayLong.format(dayDate(d))}: ${int(n)} llamadas` },
        bar, el('span', { class: 'sr', text: `${int(n)} llamadas` }));
      }))));
  }

  function renderQuota() {
    const q = state.quota;
    renderQuotaMeters(q.today);
    renderQuotaSummary(q.today);
    renderQuotaHistory(q.history, q.today.day_utc, (q.today.endpoints[0] || {}).cap);
  }

  // ------------------------------------------------------------------ salud, Ollama, andenes
  function renderHealth(ov) {
    setText('h-version', ov.version);
    setText('h-uptime', duration(ov.uptime_s));
    setText('h-paris', (ov.now_paris || '').slice(11, 16) || '—');
    const mem = ov.memory || {};
    const memBox = $('h-mem');
    if (mem.rss_bytes === null || mem.rss_bytes === undefined) {
      setText('h-mem-v', 'no disponible aquí');
      setBar($('h-mem-bar'), 0);
      memBox.dataset.level = 'none';
    } else if (mem.limit_bytes) {
      const f = mem.rss_bytes / mem.limit_bytes;
      setText('h-mem-v', `${bytes(mem.rss_bytes)} de ${bytes(mem.limit_bytes)}`);
      setBar($('h-mem-bar'), f);
      memBox.dataset.level = f >= 0.9 ? 'exhausted' : f >= 0.75 ? 'warn' : 'ok';
    } else {
      setText('h-mem-v', `${bytes(mem.rss_bytes)} · sin límite`);
      setBar($('h-mem-bar'), 0);
      memBox.dataset.level = 'none';
    }
    const db = ov.database;
    setText('h-db', `${bytes(db.size_bytes)} · esquema v${ov.schema_version}`);
    setText('h-rows', `${plural(db.routes, 'ruta', 'rutas')} · ${int(db.history)} de historial · ${plural(db.platform_obs, 'andén', 'andenes')}`);
    const map = ov.map_data;
    const mapDd = $('h-map');
    if (map.last_error) {
      mapDd.replaceChildren(`${int(map.cached_items)} guardados`, el('small', { text: map.last_error }));
    } else if (map.last_refresh) {
      mapDd.replaceChildren(`${int(map.cached_items)} guardados`, el('small', {}, 'actualizados ', timeEl(map.last_refresh)));
    } else {
      mapDd.textContent = map.cached_items ? `${int(map.cached_items)} guardados` : 'Todavía nada';
    }
  }

  function renderTranslator(t) {
    let label = 'No responde';
    let tone = 'bad';
    let line;
    const reason = t.reason || '';
    if (t.ok) {
      label = 'Funciona'; tone = 'ok';
      line = `Traduce del francés los avisos de tráfico con ${t.model}.`;
    } else if (reason === 'sin configurar') {
      label = 'Sin configurar'; tone = 'neutral';
      line = 'No hay OLLAMA_URL: los avisos se ven en francés. Es opcional.';
    } else if (reason.startsWith('falta el modelo')) {
      label = 'Falta el modelo'; tone = 'warn';
      line = `Ollama responde, pero ${reason}. Descárgalo con «ollama pull ${t.model}».`;
    } else {
      line = `Ollama ${reason || 'no responde'}. Mientras tanto, los avisos se ven en francés.`;
    }
    setChip('tr-chip', label, tone);
    setText('tr-text', line);
    setText('tr-model', t.model || '—');
    const models = t.models || [];
    setText('tr-models', models.length ? models.join(', ') : '—');
  }

  function renderPlatform(ov) {
    const pm = ov.platform_model;
    const c = ov.collector;
    setText('p-rate', pm.rate === null || pm.rate === undefined ? '—' : fmtPct.format(pm.rate));
    setText('p-rate-s', pm.predictions ? `${int(pm.hits)} de ${int(pm.predictions)}` : 'sin previsiones aún');
    setText('p-obs', int(pm.observations));
    setText('p-days', `trenes · ${plural(pm.days, 'día', 'días')}`);
    setText('p-st', int(c.stations));
    let label = 'En marcha';
    let tone = 'ok';
    if (!c.enabled) { label = 'Apagado'; tone = 'neutral'; }
    else if (!c.running) { label = 'Parado'; tone = 'warn'; }
    setChip('col-chip', label, tone);
    setText('c-last', c.last_at ? `${c.last_at} · ${plural(c.recorded, 'nuevo', 'nuevos')}` : 'Todavía ninguna');
    setText('c-next', !c.enabled ? 'No muestrea (TRAJET_COLLECT=0)'
      : c.interval ? `en unos ${duration(c.interval)}` : 'Ahora no toca (vuelve a mirar en 10 min)');
    const extra = [];
    if (c.remaining !== null && c.remaining !== undefined) extra.push(`cuota conocida: ${int(c.remaining)}`);
    if (c.priority) extra.push('alguna ruta en su franja');
    const reason = $('c-reason');
    reason.replaceChildren(c.reason || '—', extra.length ? el('small', { text: extra.join(' · ') }) : '');
    setText('c-total', plural(c.session_total, 'andén nuevo', 'andenes nuevos'));
  }

  // ------------------------------------------------------------------ errores
  const ERR_LEVEL = { WARNING: 'Aviso', ERROR: 'Error', CRITICAL: 'Crítico' };

  function renderErrors() {
    const list = state.errors || [];
    const serious = list.filter((e) => e.level !== 'WARNING').length;
    setChip('err-count', String(list.length), !list.length ? 'neutral' : serious ? 'bad' : 'warn');
    const last = $('err-last');
    if (list.length) last.replaceChildren('El último ', timeEl(list[0].ts));
    else last.textContent = 'Ninguno reciente';
    if (!list.length) {
      $('err-list').replaceChildren(el('li', { class: 'empty', text: 'Ningún aviso ni error reciente.' }));
      return;
    }
    $('err-list').replaceChildren(...list.map((e) => el('li', { class: 'err', data: { level: e.level } },
      el('div', { class: 'err-head' },
        el('span', { class: 'lvl', text: ERR_LEVEL[e.level] || e.level }),
        timeEl(e.ts),
        el('span', { class: 'mono', text: e.logger })),
      el('p', { class: 'err-msg', text: e.message }))));
  }

  // ------------------------------------------------------------------ dispositivos
  let editingId = null;

  function renderDevices() {
    if (editingId !== null) return;          // no pisar un renombrado a medias
    const devs = state.devices || [];
    setChip('dev-count', String(devs.length), devs.length ? 'ok' : 'neutral');
    const list = $('dev-list');
    if (!devs.length) {
      list.replaceChildren(el('li', { class: 'empty' },
        el('p', { text: 'Ningún iPhone emparejado todavía.' }),
        el('button', { class: 'btn btn-soft', type: 'button', on: { click: () => {
          $('emparejar').scrollIntoView({ block: 'start' });
          const start = $('pair-start');
          if (!start.closest('[hidden]')) start.focus();
        } } }, icon('plus'), el('span', { text: 'Emparejar uno' }))));
      return;
    }
    list.replaceChildren(...devs.map((d) => {
      const meta = [d.model || 'modelo desconocido'];
      if (d.app_version) meta.push(`app ${d.app_version}`);
      const used = el('span', {}, d.last_used_at ? 'usado ' : 'sin usar', d.last_used_at ? timeEl(d.last_used_at) : '');
      const li = el('li', { class: 'dev', data: { id: d.id } },
        el('span', { class: 'dev-ico' }, icon('phone')),
        el('div', { class: 'dev-body' },
          el('p', { class: 'dev-name', text: d.name }),
          el('p', { class: 'dev-meta' }, `${meta.join(' · ')} · `, used, d.last_ip ? ` · ${d.last_ip}` : '')),
        el('div', { class: 'dev-actions' },
          el('button', { class: 'btn btn-small btn-soft', type: 'button', 'aria-label': `Renombrar ${d.name}`,
            on: { click: () => startRename(d, li) } }, icon('pencil'), el('span', { text: 'Renombrar' })),
          el('button', { class: 'btn btn-small btn-danger-soft', type: 'button', 'aria-label': `Revocar ${d.name}`,
            on: { click: () => revokeDevice(d) } }, icon('trash'), el('span', { text: 'Revocar' }))));
      return li;
    }));
  }

  function startRename(dev, li) {
    editingId = dev.id;
    const input = el('input', { class: 'input', type: 'text', maxlength: 60, autocomplete: 'off',
      spellcheck: 'false', 'aria-label': `Nuevo nombre para ${dev.name}` });
    input.value = dev.name;
    const err = el('p', { class: 'field-error', role: 'alert', hidden: true });
    const save = el('button', { class: 'btn btn-small btn-primary', type: 'submit' }, el('span', { text: 'Guardar' }));
    const cancel = el('button', { class: 'btn btn-small btn-soft', type: 'button', text: 'Cancelar' });
    const form = el('form', { class: 'dev-rename', novalidate: true }, input, err, el('div', { class: 'row' }, save, cancel));
    const stop = () => {
      editingId = null;
      renderDevices();
      const again = document.querySelector(`.dev[data-id="${dev.id}"] .btn-soft`);
      if (again) again.focus();
    };
    cancel.addEventListener('click', stop);
    input.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); stop(); } });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const name = input.value.trim();
      if (!name || name.length > 60) { fieldError(input, err, 'El nombre tiene que tener de 1 a 60 caracteres.'); return; }
      busy(save, true);
      try {
        const r = await api('PATCH', `/api/admin/devices/${encodeURIComponent(dev.id)}`, { name });
        if (r.ok) {
          const i = state.devices.findIndex((d) => d.id === dev.id);
          if (i >= 0) state.devices[i] = r.data;
          toast(`Ahora se llama «${r.data.name}».`);
          stop();
        } else fieldError(input, err, errText(r));
      } catch (e) {
        fieldError(input, err, e.message);
      } finally {
        busy(save, false);
      }
    });
    li.querySelector('.dev-body').replaceWith(form);
    li.querySelector('.dev-actions').remove();
    input.focus();
    input.select();
  }

  async function revokeDevice(dev) {
    const ok = await confirmDialog({
      title: `¿Revocar «${dev.name}»?`,
      body: 'Su token deja de valer al momento. Para volver a usar Trajet en ese iPhone habrá que emparejarlo otra vez.',
      ok: 'Revocar',
    });
    if (!ok) return;
    try {
      const r = await api('DELETE', `/api/admin/devices/${encodeURIComponent(dev.id)}`);
      if (r.ok || r.status === 404) toast(`«${dev.name}» ya no tiene acceso.`);
      else toast(errText(r), 'bad');
    } catch (e) {
      toast(e.message, 'bad');
    }
    refresh();
  }

  // ------------------------------------------------------------------ emparejar
  const pairing = { id: null, status: null, deadline: 0, ttl: 300, poll: 0, tick: 0, busy: false };

  function isLoopback(host) {
    return host === 'localhost' || host.endsWith('.localhost') || /^127\./.test(host) || host === '[::1]';
  }

  function pairPane(which) {
    show('pair-idle', which === 'idle');
    show('pair-live', which === 'live');
    show('pair-end', which === 'end');
  }

  function renderPairHints() {
    const s = state.settings;
    const none = Boolean(s) && !s.lan_url && !s.tailscale_url;
    show('pair-nourl', none);
    const canPropose = none && !isLoopback(location.hostname);
    show('pair-use-origin', canPropose);
    setText('pair-origin', `${location.origin} como dirección de casa`);
    show('pair-nourl-hint', !canPropose);
  }

  function withXmlns(svg) {
    const head = svg.slice(0, svg.indexOf('>') + 1);
    return /\sxmlns=/.test(head) ? svg : svg.replace(/^<svg\b/, '<svg xmlns="http://www.w3.org/2000/svg"');
  }

  async function startPairing() {
    if (pairing.busy) return;
    pairing.busy = true;
    const btns = [$('pair-start'), $('pair-again'), $('pair-restart')];
    btns.forEach((b) => busy(b, true));
    show('pair-error', false);
    try {
      const r = await api('POST', '/api/admin/pairing');
      if (!r.ok) throw new NetError(errText(r), 'server');
      const s = r.data;
      if (typeof s.qr_svg !== 'string' || !s.qr_svg.startsWith('<svg')) throw new NetError('El servidor no ha dado un QR válido.');
      stopPairTimers();
      pairing.id = s.id;
      pairing.status = 'pending';
      pairing.ttl = s.ttl_s || 300;
      // La cuenta atrás va con el reloj de este navegador desde que llega la
      // respuesta: así un reloj del ordenador mal puesto no la estropea.
      pairing.deadline = Date.now() + pairing.ttl * 1000;
      const img = $('pair-qr');
      img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(withXmlns(s.qr_svg))}`;
      img.alt = `Código QR para emparejar. Código: ${s.code}`;
      setText('pair-code', s.code);
      const urls = (s.urls || []).map((u) => `${u.kind === 'lan' ? 'Casa' : u.kind === 'tailscale' ? 'Tailscale' : 'Otra'}: ${u.url}`);
      setText('pair-urls', urls.length ? `El QR lleva: ${urls.join(' · ')}` : 'El QR no lleva ninguna dirección del servidor.');
      setText('pair-status', 'Esperando al iPhone…');
      pairPane('live');
      tickPairing();
      pairing.tick = setInterval(tickPairing, 1000);
      pairing.poll = setTimeout(pollPairing, PAIR_POLL_MS);
      $('pair-cancel').focus();
    } catch (e) {
      const err = $('pair-error');
      err.textContent = e.message;
      pairPane('idle');
      show(err, true);
    } finally {
      btns.forEach((b) => busy(b, false));
      pairing.busy = false;
    }
  }

  function stopPairTimers() {
    clearInterval(pairing.tick);
    clearTimeout(pairing.poll);
    pairing.tick = 0;
    pairing.poll = 0;
  }

  function tickPairing() {
    if (pairing.status !== 'pending') return;
    const left = Math.max(0, pairing.deadline - Date.now());
    const secs = Math.ceil(left / 1000);
    setText('pair-left', `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`);
    $('pair-left').setAttribute('aria-label', `Caduca en ${Math.floor(secs / 60)} minutos y ${secs % 60} segundos`);
    setBar($('pair-bar'), left / (pairing.ttl * 1000));
    $('pair-countdown').dataset.tone = secs <= 60 ? 'warn' : 'ok';
    if (left <= 0) {
      // Una última pregunta al servidor por si el iPhone llegó justo a tiempo.
      clearInterval(pairing.tick);
      pairing.tick = 0;
      pollPairing(true);
    }
  }

  async function pollPairing(final) {
    clearTimeout(pairing.poll);
    pairing.poll = 0;
    if (pairing.status !== 'pending' || !pairing.id) return;
    if (document.visibilityState !== 'visible' && !final) return;     // se reanuda al volver
    const id = pairing.id;
    try {
      const r = await api('GET', `/api/admin/pairing/${encodeURIComponent(id)}`);
      if (id !== pairing.id) return;                  // ya hay otro código
      if (r.ok) applyPairStatus(r.data);
      else if (r.status === 404) applyPairStatus({ status: 'expired' });
    } catch (e) {
      // Sin red: se sigue esperando; el aviso de red ya sale arriba.
    }
    if (pairing.status === 'pending') {
      if (final || Date.now() >= pairing.deadline) applyPairStatus({ status: 'expired' });
      else pairing.poll = setTimeout(pollPairing, PAIR_POLL_MS);
    }
  }

  function applyPairStatus(st) {
    if (st.status === 'pending') return;
    pairing.status = st.status;
    stopPairTimers();
    const iconBox = $('pair-end-icon');
    const use = iconBox.querySelector('use');
    if (st.status === 'used') {
      iconBox.dataset.tone = 'ok';
      use.setAttribute('href', `${SPRITE}#check`);
      const name = st.device && st.device.name ? st.device.name : 'el iPhone';
      setText('pair-end-text', `Emparejado: ${name}`);
      setText('pair-restart-lbl', 'Emparejar otro');
      refresh();
    } else {
      iconBox.dataset.tone = 'warn';
      use.setAttribute('href', `${SPRITE}#clock`);
      setText('pair-end-text', st.status === 'cancelled' ? 'Código anulado' : 'Caducado');
      setText('pair-restart-lbl', 'Generar otro');
    }
    $('pair-qr').removeAttribute('src');
    pairPane('end');
    $('pair-restart').focus();
  }

  async function cancelPairing() {
    if (!pairing.id || pairing.status !== 'pending') return;
    const btn = $('pair-cancel');
    busy(btn, true);
    try {
      const r = await api('DELETE', `/api/admin/pairing/${encodeURIComponent(pairing.id)}`);
      if (r.ok) applyPairStatus(r.data);
      else toast(errText(r), 'bad');
    } catch (e) {
      toast(e.message, 'bad');
    } finally {
      busy(btn, false);
    }
  }

  function resumePairing() {
    if (pairing.status === 'pending' && !pairing.poll) pollPairing();
  }

  // ------------------------------------------------------------------ ajustes
  let settingsDirty = false;

  async function loadSettings() {
    try {
      const r = await api('GET', '/api/admin/settings');
      if (r.ok) applySettings(r.data, true);
    } catch (e) {
      // El refresco ya avisa de la red.
    }
  }

  function applySettings(s, fillForm) {
    state.settings = s;
    setText('server-name', s.server_name || 'Trajet');
    document.title = `${s.server_name || 'Trajet'} · panel`;
    if (fillForm || !settingsDirty) {
      $('set-name').value = s.server_name || '';
      $('set-lan').value = s.lan_url || '';
      $('set-ts').value = s.tailscale_url || '';
      settingsDirty = false;
    }
    show('set-use-origin', !isLoopback(location.hostname) && !$('set-lan').value);
    renderPairHints();
  }

  function checkUrl(value) {
    const v = value.trim();
    if (!v) return '';
    let u;
    try { u = new URL(v); } catch (e) { return 'No es una dirección: escribe algo como http://192.168.1.10:7796.'; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return 'Tiene que empezar por http:// o https://.';
    if (u.username || u.password) return 'Sin usuario ni contraseña.';
    if ((u.pathname && u.pathname !== '/') || u.search || u.hash || /[?#]/.test(v)) return 'Solo host y puerto, sin ruta (p. ej. http://192.168.1.10:7796).';
    return '';
  }

  async function saveSettings(ev, override) {
    if (ev) ev.preventDefault();
    const name = $('set-name');
    const lan = $('set-lan');
    const ts = $('set-ts');
    const body = override || { server_name: name.value.trim(), lan_url: lan.value.trim() || null,
      tailscale_url: ts.value.trim() || null };
    let bad = false;
    const nameErr = body.server_name.length > 40 ? 'Como mucho 40 caracteres.' : '';
    fieldError(name, 'set-name-error', nameErr);
    const lanErr = body.lan_url ? checkUrl(body.lan_url) : '';
    fieldError(lan, 'set-lan-error', lanErr);
    const tsErr = body.tailscale_url ? checkUrl(body.tailscale_url) : '';
    fieldError(ts, 'set-ts-error', tsErr);
    bad = Boolean(nameErr || lanErr || tsErr);
    const status = $('set-status');
    status.textContent = '';
    if (bad) {
      (nameErr ? name : lanErr ? lan : ts).focus();
      return false;
    }
    const btn = $('set-save');
    busy(btn, true);
    try {
      const r = await api('PUT', '/api/admin/settings', body);
      if (r.ok) {
        settingsDirty = false;
        applySettings(r.data, true);
        status.dataset.tone = 'ok';
        status.textContent = 'Guardado. Los QR nuevos ya lo llevan.';
        refresh();
        return true;
      }
      const msg = errText(r);
      const field = msg.startsWith('lan_url') ? [lan, 'set-lan-error'] : msg.startsWith('tailscale_url') ? [ts, 'set-ts-error']
        : msg.startsWith('server_name') ? [name, 'set-name-error'] : null;
      if (field) { fieldError(field[0], field[1], msg.replace(/^\w+:\s*/, '')); field[0].focus(); }
      else { status.dataset.tone = 'bad'; status.textContent = msg; }
    } catch (e) {
      status.dataset.tone = 'bad';
      status.textContent = e.message;
    } finally {
      busy(btn, false);
    }
    return false;
  }

  async function useOriginForPairing() {
    const s = state.settings || { server_name: 'Trajet', tailscale_url: null };
    const ok = await saveSettings(null, { server_name: s.server_name || '', lan_url: location.origin,
      tailscale_url: s.tailscale_url || null });
    if (ok) toast(`Dirección de casa: ${location.origin}`);
  }

  // ------------------------------------------------------------------ arranque
  function init() {
    // Los formularios se activan solo con JavaScript: sin él no se puede
    // mandar nada (y la clave nunca acabaría en una URL por un GET).
    $('key-fieldset').disabled = false;
    $('set-fieldset').disabled = false;

    $('refresh').addEventListener('click', () => { loadSettings(); refresh(); });
    $('net-retry').addEventListener('click', () => refresh());
    $('net-reload').addEventListener('click', () => location.reload());

    $('pair-start').addEventListener('click', startPairing);
    $('pair-again').addEventListener('click', startPairing);
    $('pair-restart').addEventListener('click', startPairing);
    $('pair-cancel').addEventListener('click', cancelPairing);
    $('pair-use-origin').addEventListener('click', useOriginForPairing);

    $('key-form').addEventListener('submit', submitKey);
    $('key-replace').addEventListener('click', () => openKeyForm(true));
    $('key-cancel').addEventListener('click', () => { closeKeyForm(); $('key-replace').focus(); });
    $('key-check').addEventListener('click', recheckKey);
    $('key-delete').addEventListener('click', deleteKey);
    $('key-show').addEventListener('click', () => setKeyVisible($('key-input').type === 'password'));

    $('set-form').addEventListener('submit', saveSettings);
    ['set-name', 'set-lan', 'set-ts'].forEach((id) => $(id).addEventListener('input', () => {
      settingsDirty = true;
      setText('set-status', '');
      show('set-use-origin', !isLoopback(location.hostname) && !$('set-lan').value);
    }));
    $('set-use-origin').addEventListener('click', () => {
      $('set-lan').value = location.origin;
      settingsDirty = true;
      fieldError('set-lan', 'set-lan-error', '');
      $('set-lan').focus();
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        if (Date.now() - state.lastRefresh > 3000) refresh(); else schedule();
        resumePairing();
      } else {
        clearTimeout(refreshTimer);
      }
    });
    // Al salir de la página (o quedarse en la caché de atrás/adelante) el
    // campo de la clave se vacía: que el navegador no lo restaure.
    window.addEventListener('pagehide', () => { $('key-input').value = ''; });

    loadSettings();
    refresh();
  }

  init();
}());
