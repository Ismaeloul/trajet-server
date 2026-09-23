"""Genera los iconos de la app a partir del glifo de la pestana 'Rutas'.

Solo libreria estandar: no hace falta Pillow ni Inkscape, asi que corre igual
en el portatil que en el NAS. Los PNG se escriben a mano (zlib + CRC) y el
dibujo se rasteriza por campo de distancia, que da el antialias gratis y
evita depender de un motor de SVG.

    python tools/make_icons.py

Escribe en app/static/icons/. Solo hay que volver a lanzarlo si se cambia el
color de acento o el propio glifo.
"""
import math
import os
import struct
import zlib

BG_TOP = (0x16, 0x1e, 0x2c)   # el fondo de la app, un punto mas claro arriba
BG_BOT = (0x0b, 0x0f, 0x16)   # --bg
INK    = (0xff, 0xd6, 0x29)   # --accent

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "app", "static", "icons")

# El glifo de la pestana "Rutas", en el mismo lienzo 24x24 del SVG:
#   M6 20V7a3 3 0 0 1 6 0v10a3 3 0 0 0 6 0V4
# Dos rectas verticales cosidas por dos semicircunferencias, y un punto
# gordo en cada extremo. Es un trayecto con su origen y su destino.
STROKE = 2.5          # ancho del trazo en unidades de 24
DOT_R = 1.75          # radio de los extremos


def path_points(step=0.04):
    """El trazo convertido en polilinea. Con paso fino el campo de distancia
    no distingue esto de una curva de verdad."""
    pts = [(6.0, 20.0), (6.0, 7.0)]
    for i in range(1, 41):                      # semicircunferencia de arriba
        t = math.pi * (1 - i / 40)
        pts.append((9 + 3 * math.cos(t), 7 - 3 * math.sin(t)))
    pts.append((12.0, 17.0))
    for i in range(1, 41):                      # semicircunferencia de abajo
        t = math.pi * (1 - i / 40)
        pts.append((15 + 3 * math.cos(t), 17 + 3 * math.sin(t)))
    pts.append((18.0, 4.0))
    return pts


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    den = vx * vx + vy * vy
    t = 0.0 if den == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / den))
    dx, dy = wx - t * vx, wy - t * vy
    return math.hypot(dx, dy)


def coverage(size, inset):
    """Cobertura por pixel (0..1) del glifo centrado en un lienzo de `size`.

    `inset` es la fraccion de lienzo que queda libre por el lado mas apretado:
    0.12 para un icono normal, mas para el 'maskable', al que Android le
    recorta las esquinas.
    """
    pts = path_points()
    half = STROKE / 2
    pad = max(half, DOT_R)
    x0 = min(p[0] for p in pts) - pad
    x1 = max(p[0] for p in pts) + pad
    y0 = min(p[1] for p in pts) - pad
    y1 = max(p[1] for p in pts) + pad

    usable = size * (1 - 2 * inset)
    k = usable / max(x1 - x0, y1 - y0)
    ox = (size - (x1 - x0) * k) / 2 - x0 * k
    oy = (size - (y1 - y0) * k) / 2 - y0 * k

    cov = [0.0] * (size * size)
    r_stroke = half * k
    r_dot = DOT_R * k

    def stamp(cx, cy, radius, seg=None):
        """Pinta un circulo, o una capsula si `seg` trae el otro extremo.
        Solo recorre la caja que toca: asi esto tarda milisegundos."""
        if seg:
            bx, by = seg
            lo_x, hi_x = min(cx, bx) - radius - 1, max(cx, bx) + radius + 1
            lo_y, hi_y = min(cy, by) - radius - 1, max(cy, by) + radius + 1
        else:
            lo_x, hi_x = cx - radius - 1, cx + radius + 1
            lo_y, hi_y = cy - radius - 1, cy + radius + 1
        for y in range(max(0, int(lo_y)), min(size, int(hi_y) + 1)):
            base = y * size
            fy = y + 0.5
            for x in range(max(0, int(lo_x)), min(size, int(hi_x) + 1)):
                fx = x + 0.5
                d = (seg_dist(fx, fy, cx, cy, seg[0], seg[1]) if seg
                     else math.hypot(fx - cx, fy - cy))
                a = radius + 0.5 - d          # borde de 1px difuminado
                if a <= 0:
                    continue
                if a > 1:
                    a = 1.0
                if a > cov[base + x]:
                    cov[base + x] = a

    scr = [(p[0] * k + ox, p[1] * k + oy) for p in pts]
    for (ax, ay), (bx, by) in zip(scr, scr[1:]):
        stamp(ax, ay, r_stroke, (bx, by))
    stamp(scr[0][0], scr[0][1], r_dot)
    stamp(scr[-1][0], scr[-1][1], r_dot)
    return cov


def render(size, inset=0.14):
    cov = coverage(size, inset)
    rows = []
    for y in range(size):
        t = y / max(1, size - 1)
        bg = tuple(int(round(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t))
                   for i in range(3))
        row = bytearray()
        base = y * size
        for x in range(size):
            a = cov[base + x]
            if a <= 0:
                row += bytes(bg)
            else:
                row += bytes(int(round(bg[i] + (INK[i] - bg[i]) * a))
                             for i in range(3))
        rows.append(bytes(row))
    return rows


def write_png(path, rows):
    size = len(rows)
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


# Android recorta el 'maskable' a un circulo: el glifo se mete dentro de la
# zona segura (el 80% central) o le corta los extremos.
JOBS = [
    ("icon-192.png", 192, 0.14),
    ("icon-512.png", 512, 0.14),
    ("icon-maskable-512.png", 512, 0.26),
    ("apple-touch-icon.png", 180, 0.16),
    ("favicon-32.png", 32, 0.10),
]

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, size, inset in JOBS:
        p = os.path.normpath(os.path.join(OUT, name))
        n = write_png(p, render(size, inset))
        print(f"{name:26} {size}x{size}  {n/1024:.1f} kB")
