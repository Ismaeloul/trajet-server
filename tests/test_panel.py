"""El panel web (GET / y /panel/static/*): CSP estricta, sin estilos ni
scripts en linea, estaticos servidos desde el propio servidor (nada de CDN)
y sin la clave PRIM en ningun sitio (R83).

La CSP es `default-src 'self'` sin 'unsafe-inline': un <script> con cuerpo,
un <style>, un atributo style= o un onclick= los bloquearia el navegador y el
panel se quedaria a medias. Estos tests lo miran en el HTML y en el JS.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest
from conftest import FAKE_KEY

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "app", "panel")
STATIC = os.path.join(PANEL, "static")
NUEVA = "PANEL0nueva0clave0de0pruebas0Q7Zk"


class _Html(HTMLParser):
    """Lo que interesa del HTML: etiquetas con sus atributos y los textos de
    <script> y <style>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.inline: list[tuple[str, str]] = []
        self._open: str | None = None

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._open = tag

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == self._open:
            self._open = None

    def handle_data(self, data):
        if self._open and data.strip():
            self.inline.append((self._open, data))


def _parse(html: str) -> _Html:
    p = _Html()
    p.feed(html)
    return p


def _static_refs(parsed: _Html) -> set[str]:
    refs = set()
    for _, attrs in parsed.tags:
        for name in ("src", "href"):
            v = attrs.get(name) or ""
            if v.startswith("/panel/static/"):
                refs.add(v.split("#", 1)[0])
    return refs


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


# ---------------- el HTML y sus cabeceras ----------------

def test_html_del_panel_con_csp_estricta(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    directivas = {d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()}
    assert directivas["default-src"] == "default-src 'self'"
    assert directivas["script-src"] == "script-src 'self'"
    assert directivas["style-src"] == "style-src 'self'"
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-cache"


def test_html_sin_scripts_ni_estilos_en_linea(client):
    html = client.get("/").text
    p = _parse(html)
    assert not p.inline, f"hay {p.inline[0][0]} en linea"
    scripts = [a for t, a in p.tags if t == "script"]
    assert scripts and all(a.get("src", "").startswith("/panel/static/") for a in scripts)
    for tag, attrs in p.tags:
        assert "style" not in attrs, f"<{tag} style=…>"
        eventos = [k for k in attrs if k.startswith("on")]
        assert not eventos, f"<{tag} {eventos[0]}=…>"
        for k in ("href", "src", "action"):
            assert not (attrs.get(k) or "").strip().lower().startswith("javascript:")
    assert "<style" not in html.lower()
    # Nada de recursos de fuera (fuentes, CDN…): solo el enlace al portal de PRIM,
    # que es una navegacion con rel=noopener, no un recurso de la pagina.
    for tag, attrs in p.tags:
        for k in ("src", "href"):
            v = attrs.get(k) or ""
            if v.startswith(("http://", "https://", "//")):
                assert tag == "a" and "noopener" in (attrs.get("rel") or ""), f"<{tag} {k}={v}>"


def test_html_accesible_lo_basico(client):
    p = _parse(client.get("/").text)
    html_attrs = next(a for t, a in p.tags if t == "html")
    assert html_attrs.get("lang") == "es"
    ids = {a["id"] for _, a in p.tags if "id" in a}
    # Cada campo tiene su <label for> (o aria-label)
    labels = {a.get("for") for t, a in p.tags if t == "label"}
    for tag, attrs in p.tags:
        if tag == "input" and attrs.get("type") != "hidden":
            assert attrs.get("id") in labels or attrs.get("aria-label"), attrs
    # Los botones de solo icono llevan nombre
    for tag, attrs in p.tags:
        if tag == "button" and "icon-btn" in (attrs.get("class") or ""):
            assert attrs.get("aria-label"), attrs
    # aria-labelledby/aria-describedby apuntan a ids que existen
    for _, attrs in p.tags:
        for k in ("aria-labelledby", "aria-describedby"):
            for ref in (attrs.get(k) or "").split():
                assert ref in ids, f"{k}={ref} no existe"
    # La clave: campo de contrasena, sin autocompletar ni corrector
    clave = next(a for t, a in p.tags if t == "input" and a.get("id") == "key-input")
    assert clave["type"] == "password" and clave["autocomplete"] == "off"
    assert clave["spellcheck"] == "false"
    # Los formularios no se pueden mandar sin JS (el fieldset va desactivado)
    for t, a in p.tags:
        if t == "fieldset":
            assert "disabled" in a, a


def test_ids_que_usa_el_js_existen_en_el_html(client):
    """Un getElementById que no encuentra nada rompe el panel entero."""
    html = client.get("/").text
    ids = {a["id"] for _, a in _parse(html).tags if "id" in a}
    js = _read("panel.js")
    usados = set(re.findall(r"\$\('([a-z0-9-]+)'\)", js))
    usados |= set(re.findall(r"(?:setText|show|setChip|fieldError)\('([a-z0-9-]+)'", js))
    usados |= set(re.findall(r"fieldError\([^,]+, '([a-z0-9-]+)'", js))
    faltan = sorted(usados - ids)
    assert not faltan, f"el JS usa ids que no estan en el HTML: {faltan}"
    # Y los iconos que pide existen en el sprite
    sprite = _read("iconos.svg")
    simbolos = set(re.findall(r'<symbol id="([a-z-]+)"', sprite))
    pedidos = set(re.findall(r"icon\('([a-z-]+)'", js))
    pedidos |= set(re.findall(r"\$\{SPRITE\}#([a-z-]+)", js))
    pedidos |= set(re.findall(r"iconos\.svg#([a-z-]+)", html))
    assert pedidos and not (pedidos - simbolos), sorted(pedidos - simbolos)


# ---------------- estaticos ----------------

@pytest.mark.parametrize("nombre, tipo", [
    ("panel.css", "text/css"),
    ("panel.js", "javascript"),
    ("iconos.svg", "image/svg+xml"),
    ("icono.svg", "image/svg+xml"),
])
def test_estaticos_se_sirven(client, nombre, tipo):
    r = client.get(f"/panel/static/{nombre}")
    assert r.status_code == 200 and tipo in r.headers["content-type"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_todo_lo_que_pide_el_html_existe(client):
    refs = _static_refs(_parse(client.get("/").text))
    assert {"/panel/static/panel.css", "/panel/static/panel.js",
            "/panel/static/iconos.svg", "/panel/static/icono.svg"} <= refs
    for ref in refs:
        assert client.get(ref).status_code == 200, ref
    assert client.get("/panel/static/no-existe.js").status_code == 404
    assert client.get("/panel/static/../../main.py").status_code == 404


def test_css_y_js_sin_recursos_de_fuera_ni_codigo_peligroso():
    css = _read("panel.css")
    js = _read("panel.js")
    assert not re.search(r"@import|url\(\s*['\"]?(https?:)?//", css), "el CSS carga algo de fuera"
    assert "fonts.googleapis" not in css and "cdn" not in css.lower()
    # Las unicas URL absolutas del JS: el espacio de nombres de SVG y los
    # ejemplos de los mensajes de ayuda. Las llamadas van todas a /api/admin.
    urls = {u.rstrip(".") for u in re.findall(r"https?://[^\s'\"`),]*", js)}
    assert urls <= {"http://www.w3.org/2000/svg", "http://192.168.1.10:7796", "http://", "https://"}, urls
    assert set(re.findall(r"api\('[A-Z]+', `?'?([^'`$]+)", js)) <= {
        "/api/admin/overview", "/api/admin/devices", "/api/admin/quota", "/api/admin/errors?limit=50",
        "/api/admin/prim-key", "/api/admin/prim-key/check", "/api/admin/pairing", "/api/admin/pairing/",
        "/api/admin/devices/", "/api/admin/settings"}
    assert js.count("fetch(") == 1                        # un solo sitio que habla con la red
    for peligro in ("eval(", "new Function", "document.write", "innerHTML", "outerHTML",
                    "insertAdjacentHTML", "setAttribute('style'", "localStorage", "sessionStorage"):
        assert peligro not in js, peligro
    # Lo que modifica lleva la cabecera del panel; la de borrar, su confirmacion
    assert "'X-Trajet-Panel'" in js and "'X-Trajet-Confirm': 'borrar'" in js


def test_css_las_casillas_no_recortan_el_valor():
    """En un iPhone (390 px) las tres casillas de «Salud del servidor» y de
    «Andenes» van justas: «2 h 53 min» o «sin previsiones aún» tienen que
    saltar de línea, no recortarse con puntos suspensivos (se perdía el dato;
    visto en la FASE 4 al probar el panel en el navegador)."""
    css = _read("panel.css")
    for selector in (".tile .num", ".tile-sub"):
        reglas = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert reglas, selector
        for cuerpo in reglas:
            assert "ellipsis" not in cuerpo and "nowrap" not in cuerpo, (selector, cuerpo)
    # Y en pantallas estrechas el valor se hace un poco mas pequeno para que
    # quepa en una linea las mas de las veces.
    assert re.search(r"@media \(max-width: 430px\)\s*\{[^}]*\.tile \.num", css)


def test_svg_de_los_iconos_sin_scripts():
    for nombre in ("iconos.svg", "icono.svg"):
        svg = _read(nombre).lower()
        assert "<script" not in svg and "onload" not in svg and "href=\"http" not in svg


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node para comprobar la sintaxis del JS")
def test_js_sin_errores_de_sintaxis():
    r = subprocess.run([shutil.which("node"), "--check", os.path.join(STATIC, "panel.js")],  # noqa: S603
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ---------------- la clave, nunca (R83) ----------------

def test_html_y_estaticos_sin_la_clave(client, fake_prim):
    from app import prim
    assert client.portal.call(prim.set_key_and_save, NUEVA)["saved"] is True
    textos = [client.get("/").text]
    for ref in _static_refs(_parse(textos[0])):
        textos.append(client.get(ref).text)
    todo = "\n".join(textos)
    for clave in (NUEVA, FAKE_KEY):
        assert clave not in todo and clave[-4:] not in todo
