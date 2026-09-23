/* Ejecuta el render real de app.js contra un JSON real de /api/board,
   con un DOM mínimo simulado. Sin navegador, pero prueba las plantillas:
   caza referencias a campos que no existen y HTML mal formado. */

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const SCRATCH = process.argv[2];

const board = JSON.parse(fs.readFileSync(path.join(SCRATCH, 'board.json'), 'utf8'));
const routes = JSON.parse(fs.readFileSync(path.join(SCRATCH, 'routes.json'), 'utf8'));

const captured = {};

function makeEl(id) {
  const el = {
    id,
    _html: '',
    textContent: '',
    value: '',
    disabled: false,
    dataset: {},
    style: {},
    selectedOptions: [{ dataset: {} }],
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { on === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c)) : (on ? this._s.add(c) : this._s.delete(c)); },
      contains(c) { return this._s.has(c); },
    },
    appendChild() {},
    addEventListener() {},
    set innerHTML(v) { this._html = v; if (id) captured[id] = v; },
    get innerHTML() { return this._html; },
    set onclick(f) { this._onclick = f; },
    get onclick() { return this._onclick; },
    set oninput(f) { this._oninput = f; },
  };
  return el;
}

const elements = new Map();
function getEl(sel) {
  if (!elements.has(sel)) elements.set(sel, makeEl(sel.startsWith('#') ? sel.slice(1) : null));
  return elements.get(sel);
}

global.document = {
  querySelector: sel => getEl(sel),
  querySelectorAll: () => [],
  createElement: () => makeEl(null),
  addEventListener: () => {},
  visibilityState: 'visible',
  body: { appendChild() {} },
};
global.window = global;
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = () => 0;
global.clearTimeout = () => {};

let altCalls = 0;
global.fetch = async (url) => {
  let body;
  if (url.startsWith('/api/board')) body = board;
  else if (url.startsWith('/api/routes')) body = routes;
  else if (url.startsWith('/api/alternatives')) { altCalls++; body = ALTS; }
  else body = {};
  return { ok: true, status: 200, json: async () => body };
};

const ALTS = {
  needed: true,
  affected: [{ line_id: 'line:IDFM:C01383', line_code: '13', level: 1, label: 'perturbada' }],
  baseline_minutes: 22,
  options: [
    { total_minutes: 28, transfers: 1, delta_minutes: 6, usable: true, worst_level: 0,
      legs: [{ code: '14', mode: 'Métro', direction: 'Orly', color: '62259D', minutes: 12, status: 'normal' },
             { code: 'A', mode: 'RER', direction: 'Cergy', color: 'E3051C', minutes: 9, status: 'normal' }] },
    { total_minutes: 41, transfers: 2, delta_minutes: 19, usable: false, worst_level: 2,
      legs: [{ code: 'N15', mode: 'Bus', direction: 'Villejuif', color: '', minutes: 30, status: 'interrumpida' }] },
  ],
};

// Cargar app.js
// Se quita la directiva 'use strict': en modo estricto eval() no expone
// las funciones declaradas al ámbito de fuera y no podríamos probarlas.
const src = fs.readFileSync(path.join(ROOT, 'app/static/app.js'), 'utf8')
  .replace(/^\s*'use strict';\s*$/m, '');
eval(src);

// ---- comprobaciones ----
setTimeout;
(async () => {
  await new Promise(r => process.nextTick(r));
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  const legsHtml = captured['legs'] || '';
  const stripHtml = captured['alert-strip'] || '';
  let fails = 0;
  const check = (name, cond, extra) => {
    console.log(`  ${cond ? 'ok ' : 'MAL'} ${name}${cond ? '' : '  <<< ' + (extra || '')}`);
    if (!cond) fails++;
  };

  console.log('=== render del tablero ===');
  check('se ha pintado algo', legsHtml.length > 200, `sólo ${legsHtml.length} chars`);
  check('sin "undefined" en el HTML', !legsHtml.includes('undefined'));
  check('sin "[object Object]"', !legsHtml.includes('[object Object]'));
  check('sin "NaN"', !legsHtml.includes('NaN'));
  check('sin "null"', !legsHtml.includes('>null<'));

  const legCount = (legsHtml.match(/class="leg[ "]/g) || []).length;
  check(`${board.legs.length} tramos pintados`, legCount === board.legs.length, `pintados ${legCount}`);

  // Ya no se esconde ninguna salida: la tira es horizontal, así que caben
  // todas y se llega a ellas deslizando. Antes se cortaban a dos por tramo y
  // el resto quedaba detrás de un "+N".
  const todas = board.legs.reduce((k, l) => k + l.departures.length, 0);
  const depCount = (legsHtml.match(/class="dep(?: |")/g) || []).length;
  check(`${todas} salidas, todas visibles en la tira`,
        depCount === todas, `pintadas ${depCount}`);
  check('sin salidas escondidas detrás de un "+N"',
        !legsHtml.includes('class="more"'));

  const conSalidas = board.legs.filter(l => l.departures.length).length;
  const primeras = (legsHtml.match(/class="dep [^"]*\bfirst\b/g) || []).length;
  check('la próxima salida de cada tramo va destacada',
        primeras === conSalidas, `${primeras} de ${conSalidas}`);
  check('los tramos con salidas llevan tira',
        (legsHtml.match(/class="strip"/g) || []).length === conSalidas);

  // Un tramo sin salidas no se queda en blanco: dice si es fin de servicio o
  // si simplemente ahora no hay ninguno anunciado.
  const vacios = board.legs.length - conSalidas;
  check(`${vacios} tramos sin salidas lo explican`,
        (legsHtml.match(/class="sin-pasos"/g) || []).length === vacios);

  // Cada tramo lleva su color oficial y el del siguiente, para el hilo.
  const conColor = (legsHtml.match(/--lc:#/g) || []).length;
  check('cada tramo lleva su color de línea', conColor === board.legs.length,
        `${conColor} vs ${board.legs.length}`);
  check('y el color del siguiente, para el degradado',
        (legsHtml.match(/--lc-next:/g) || []).length === board.legs.length);

  // El andén solo donde existe. Metro, bus y tranvía no lo publican nunca
  // (0 de ~600 medidos), así que ahí la casilla no se dibuja.
  const conVia = board.legs
    .filter(l => !['Métro', 'Bus', 'Tramway'].includes(l.line_mode))
    .reduce((k, l) => k + l.departures.filter(d => d.platform || d.guess).length, 0);
  const viaCount = (legsHtml.match(/class="via[ "]/g) || []).length;
  check('la vía se pinta solo en tren y RER, y solo si hay dato',
        viaCount === conVia, `${viaCount} vs ${conVia}`);
  check('metro y bus no reservan sitio para la vía',
        !board.legs.some(l => ['Métro', 'Bus', 'Tramway'].includes(l.line_mode))
        || legsHtml.includes('no-plat'));

  // Una vía probable NUNCA puede leerse como confirmada.
  if (board.legs.some(l => l.departures.some(d => d.guess))) {
    check('la vía probable va marcada como probable', /class="via guess"/.test(legsHtml));
    check('y lo dice con la palabra', /probable/.test(legsHtml));
  }

  // La API no da ocupación, pero sí si el tren es corto o largo.
  const conLargo = board.legs.reduce(
    (k, l) => k + l.departures.filter(d => d.length).length, 0);
  if (conLargo) {
    check(`${conLargo} salidas dicen si el tren es corto o largo`,
          (legsHtml.match(/class="tren /g) || []).length === conLargo,
          (legsHtml.match(/class="tren /g) || []).length);
    check('y el corto se distingue del largo',
          /class="tren corto"/.test(legsHtml) && /class="tren largo"/.test(legsHtml));
  }

  // El ritmo: correr, andar o con calma. Sale de los minutos.
  const pace = (legsHtml.match(/class="pace (corre|anda|calma)"/g) || []).length;
  const conPace = board.legs.reduce((k, l) =>
    k + l.departures.filter(d => !d.at_stop && d.minutes > 0).length, 0);
  check(`${conPace} salidas dicen si hay que correr o da tiempo`,
        pace === conPace, `${pace} vs ${conPace}`);

  // Esperas largas: "165 min" no dice nada, "2h45" sí.
  if (board.legs.some(l => l.departures.some(d => d.minutes >= 60))) {
    check('las esperas de más de una hora salen como 1h46, no como 106 min',
          !/>\s*1[0-9]{2}<span class="u">min/.test(legsHtml)
          && /<span class="u">h<\/span>/.test(legsHtml));
  }
  if (board.legs.some(l => l.departures.some(d => d.platform_new))) {
    check('la vía nueva se resalta', /class="via fresh"/.test(legsHtml));
  }
  const tocados = board.legs.filter(l => l.status.level > 0).length;
  check(`${tocados} avisos pegados a su línea`,
        (legsHtml.match(/class="warn /g) || []).length === tocados,
        `${(legsHtml.match(/class="warn /g) || []).length}`);
  if (board.legs.some(l => l.status.translating)) {
    check('se avisa de que la traducción está en marcha',
          /traduciendo/.test(legsHtml));
  }

  // Etiquetas balanceadas
  const open = (legsHtml.match(/<div/g) || []).length;
  const close = (legsHtml.match(/<\/div>/g) || []).length;
  check('divs balanceados', open === close, `${open} abiertos, ${close} cerrados`);
  console.log('\n=== avisos, pegados a su línea ===');
  // El aviso vive dentro del tramo del que habla. La franja global quedó solo
  // para las alternativas, que sí valen para el trayecto entero.
  const troubled = board.legs.filter(l => l.status.level > 0).length;
  if (troubled) {
    check('el botón de alternativa está en el tramo tocado',
          (legsHtml.match(/data-alts="1"/g) || []).length === troubled,
          (legsHtml.match(/data-alts="1"/g) || []).length);
    check('la franja global no repite el aviso', stripHtml.length === 0,
          stripHtml.slice(0, 40));
    const traducidos = board.legs.filter(l => (l.status.messages_es || [])[0]).length;
    if (traducidos) {
      check('el aviso traducido sale en español',
            /interrumpido en toda la l/.test(legsHtml));
      check('y conserva el original en francés',
            /ver original en franc/.test(legsHtml));
    }
    // Mientras el LLM local traduce, el aviso sale en francés tal cual. Se
    // comprueba con el texto real del escenario, no con una cadena fija.
    const sinTraducir = board.legs.filter(
      l => l.status.level > 0 && l.status.messages.length
           && !(l.status.messages_es || [])[0]);
    sinTraducir.forEach(l => {
      const trozo = l.status.messages[0].slice(0, 24);
      check(`el aviso de ${l.line_code} sale en francés mientras se traduce`,
            legsHtml.includes(esc(trozo)), trozo);
    });
  } else {
    check('sin perturbación no hay franja',
          stripHtml.length === 0 || !stripHtml.includes('alt-opt'));
  }


  console.log('\n=== render de alternativas ===');
  const altHtml = renderAlts(ALTS);
  check('sin undefined', !altHtml.includes('undefined'));
  check('muestra el tiempo total', altHtml.includes('28 min'));
  check('muestra la diferencia +6', altHtml.includes('+6 min'));
  check('avisa de la opción inservible', altHtml.includes('también está interrumpida'));
  check('pinta las líneas', altHtml.includes('>14<') && altHtml.includes('>A<'));

  console.log('\n=== escape de HTML ===');
  check('escapa comillas y etiquetas',
        esc('<img src=x onerror=alert(1)>') === '&lt;img src=x onerror=alert(1)&gt;');

  console.log('\n=== contraste del texto sobre el color de línea ===');
  check('amarillo claro -> texto oscuro', textOn('#CEC73D') === '#0a0e14');
  check('verde oscuro -> texto claro',  textOn('#6E6E00') === '#ffffff');

  console.log(fails ? `\n${fails} COMPROBACIONES FALLIDAS` : '\nTODAS LAS COMPROBACIONES OK');

  console.log('\n--- primeros 620 chars del HTML generado ---');
  console.log(legsHtml.slice(0, 620).replace(/\s+/g, ' '));
  process.exit(fails ? 1 : 0);
})();
