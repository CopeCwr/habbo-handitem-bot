"""Lógica compartida por scan.py (GitHub Actions) y bot.py (bot persistente opcional)."""
import asyncio
import io
import json
import lzma
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Pillow es opcional: sin él no hay vista previa
    Image = None

# Los assets del SWF se llaman p. ej. "hh_human_item_h_crr_ri_100_0_0":
#   h_crr_ri_<id>_<dir>_<frame>  -> handitem sujetado (carry)
#   h_drk_ri_<id>_<dir>_<frame>  -> handitem al beber/usar (drink)
#   h_sig_li_<n>_<dir>_<frame>   -> carteles (sign), NO son handitems
# El ID queda en el grupo 1. Se puede sobrescribir con HANDITEM_REGEX.
HANDITEM_REGEX = re.compile(
    os.getenv("HANDITEM_REGEX") or r"_h_(?:crr|drk)_[a-z]+_(\d+)_\d+_\d+$"
)

DOMAINS = {
    "sandbox": "https://sandbox.habbo.com/gamedata/external_variables/1",
    "com": "https://habbo.com/gamedata/external_variables/1",
    "es": "https://habbo.es/gamedata/external_variables/1",
    "fi": "https://habbo.fi/gamedata/external_variables/1",
    "com.br": "https://habbo.com.br/gamedata/external_variables/1",
    "com.tr": "https://habbo.com.tr/gamedata/external_variables/1",
    "nl": "https://habbo.nl/gamedata/external_variables/1",
    "de": "https://habbo.de/gamedata/external_variables/1",
    "fr": "https://habbo.fr/gamedata/external_variables/1",
    "it": "https://habbo.it/gamedata/external_variables/1",
}

STATE_FILE = Path(os.getenv("STATE_FILE") or "state.json")
SWF_NAME = "hh_human_item.swf"
HEADERS = {"User-Agent": "Mozilla/5.0 (HabboHanditemBot)"}
MAX_SEEN = 50  # versiones del cliente recordadas
ROWS_PER_PAGE = 6  # filas (handitem + tipo) por imagen
MAX_PAGES = 3  # imágenes máximas del respaldo en tabla
AV_MAX_PAGES = 12  # imágenes máximas con muñeco (una por handitem)
IMAGES_PER_MESSAGE = 4  # Discord agrupa hasta 4 imágenes por mensaje
BODY_NAME = "hh_human_body.swf"
SPRITE_REGEX = re.compile(r"_h_(crr|drk)_[a-z]+_(\d+)_(\d+)_(\d+)$")


# ───────────────────────── Estado ─────────────────────────
def load_state() -> dict:
    default = {"initialized": False, "handitems": [], "client_urls": []}
    if STATE_FILE.exists():
        return {**default, **json.loads(STATE_FILE.read_text())}
    return default


def save_state(state: dict) -> bool:
    """Guarda el estado solo si cambió. Devuelve True si escribió."""
    text = json.dumps(state, indent=2, sort_keys=True) + "\n"
    if STATE_FILE.exists() and STATE_FILE.read_text() == text:
        return False
    STATE_FILE.write_text(text)
    return True


# ───────────────────────── Parser SWF ─────────────────────────
def decompress_swf(data: bytes) -> bytes:
    sig = data[:3]
    if sig == b"FWS":
        return data
    if sig == b"CWS":
        return data[:8] + zlib.decompress(data[8:])
    if sig == b"ZWS":
        size = struct.unpack("<I", data[4:8])[0] - 8
        props, body = data[12:17], data[17:]
        dec = lzma.LZMADecompressor(lzma.FORMAT_ALONE)
        return data[:8] + dec.decompress(props + struct.pack("<Q", size) + body)
    raise ValueError("No es un archivo SWF válido")


def iter_tags(swf: bytes):
    nbits = swf[8] >> 3
    pos = 8 + (5 + nbits * 4 + 7) // 8 + 4  # RECT + framerate + framecount
    while pos + 2 <= len(swf):
        header = struct.unpack_from("<H", swf, pos)[0]
        pos += 2
        code, length = header >> 6, header & 0x3F
        if length == 0x3F:
            length = struct.unpack_from("<I", swf, pos)[0]
            pos += 4
        yield code, swf[pos : pos + length]
        pos += length
        if code == 0:
            break


def symbol_ids(swf: bytes) -> dict[str, int]:
    """Nombre de asset -> ID del tag (para localizar su bitmap)."""
    out = {}
    for code, body in iter_tags(swf):
        if code == 76:  # SymbolClass
            count = struct.unpack_from("<H", body, 0)[0]
            p = 2
            for _ in range(count):
                tag_id = struct.unpack_from("<H", body, p)[0]
                p += 2
                end = body.index(b"\0", p)
                out[body[p:end].decode("utf-8", "ignore")] = tag_id
                p = end + 1
    return out


def symbol_names(swf: bytes) -> set[str]:
    return set(symbol_ids(swf))


def analyze_swf(data: bytes) -> tuple[set[str], set[int]]:
    names = symbol_names(decompress_swf(data))
    ids = {int(m.group(1)) for n in names if (m := HANDITEM_REGEX.search(n))}
    return names, ids


# ───────────────────────── Vista previa ─────────────────────────
def _decode_lossless2(body: bytes):
    """DefineBitsLossless2 -> imagen RGBA (formatos 5 = ARGB y 3 = paleta)."""
    fmt = body[2]
    w, h = struct.unpack_from("<HH", body, 3)
    if not w or not h:
        return None
    if fmt == 5:  # ARGB 32 bits, alfa premultiplicado
        raw = zlib.decompress(body[7:])
        out = bytearray(len(raw))
        for i in range(0, len(raw), 4):
            a, r, g, b = raw[i : i + 4]
            if 0 < a < 255:
                r, g, b = min(255, r * 255 // a), min(255, g * 255 // a), min(255, b * 255 // a)
            out[i : i + 4] = bytes((r, g, b, a))
        return Image.frombytes("RGBA", (w, h), bytes(out))
    if fmt == 3:  # 8 bits con paleta RGBA
        n = body[7] + 1
        raw = zlib.decompress(body[8:])
        pal, px = raw[: n * 4], raw[n * 4 :]
        stride = (w + 3) // 4 * 4
        out = bytearray()
        for y in range(h):
            for x in range(w):
                k = px[y * stride + x] * 4
                out += pal[k : k + 4]
        return Image.frombytes("RGBA", (w, h), bytes(out))
    return None


LABEL_W, CELL_W, CELL_H, HEAD_H = 84, 96, 100, 28
BOX_W, BOX_H = 88, 84
KIND_LABEL = {"crr": "mano", "drk": "beber"}


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fit_scale(imgs) -> float:
    """Misma escala para todas las direcciones de una fila."""
    s = min(min(BOX_W / im.width, BOX_H / im.height) for im in imgs.values())
    return float(min(5, int(s))) if s >= 1 else s


def _scaled(img, s: float):
    if s >= 1:
        k = int(s)
        return img.resize((img.width * k, img.height * k), Image.NEAREST)
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)


def _draw_page(rows) -> bytes:
    width = LABEL_W + 8 * CELL_W
    sheet = Image.new("RGBA", (width, HEAD_H + len(rows) * CELL_H), (43, 45, 49, 255))
    draw = ImageDraw.Draw(sheet)
    f_big, f_small = _font(17), _font(13)
    muted, text, line = (148, 155, 164, 255), (220, 221, 222, 255), (60, 63, 68, 255)

    draw.text((8, 6), "dir", fill=muted, font=f_small)
    for d in range(8):
        label = str(d)
        tw = draw.textlength(label, font=f_small)
        draw.text((LABEL_W + d * CELL_W + (CELL_W - tw) / 2, 6), label, fill=muted, font=f_small)

    for r, (item_id, kind, imgs) in enumerate(rows):
        y = HEAD_H + r * CELL_H
        draw.line([(0, y), (width, y)], fill=line)
        draw.text((10, y + CELL_H // 2 - 20), f"#{item_id}", fill=text, font=f_big)
        draw.text((10, y + CELL_H // 2 + 2), KIND_LABEL[kind], fill=muted, font=f_small)
        scale = _fit_scale(imgs)
        for d, img in imgs.items():
            img = _scaled(img, scale)
            x = LABEL_W + d * CELL_W + (CELL_W - img.width) // 2
            sheet.paste(img, (x, y + (CELL_H - img.height) // 2), img)
    buf = io.BytesIO()
    sheet.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _render_table(data: bytes, ids, rows_per_page: int = ROWS_PER_PAGE,
                  max_pages: int = MAX_PAGES) -> tuple[list[bytes], int]:
    """Respaldo sin muñeco: tabla con cada handitem en sus 8 direcciones (mano y, si existe, beber).
    Devuelve (páginas, cuántos handitems caben en ellas)."""
    if Image is None:
        return [], 0
    swf = decompress_swf(data)
    bitmaps = {}
    for code, body in iter_tags(swf):
        if code == 36:
            bitmaps[struct.unpack_from("<H", body, 0)[0]] = body

    sprites: dict[tuple[int, str], dict[int, int]] = {}
    for name, tag in symbol_ids(swf).items():
        m = SPRITE_REGEX.search(name)
        if m and int(m.group(4)) == 0:  # frame 0
            sprites.setdefault((int(m.group(2)), m.group(1)), {})[int(m.group(3))] = tag

    rows = []
    for i in sorted(ids):
        for kind in ("crr", "drk"):
            imgs = {}
            for d, tag in sprites.get((i, kind), {}).items():
                if d < 8 and tag in bitmaps:
                    img = _decode_lossless2(bitmaps[tag])
                    if img and img.width * img.height > 4:  # 1x1 = marcador vacío
                        imgs[d] = img
            if imgs:
                rows.append((i, kind, imgs))
    rows = rows[: rows_per_page * max_pages]
    if not rows:
        return [], 0
    pages = [_draw_page(rows[k : k + rows_per_page]) for k in range(0, len(rows), rows_per_page)]
    return pages, len({r[0] for r in rows})


# ───────────────────────── Muñeco ─────────────────────────
AXIS = 33  # eje de simetría del muñeco, medido en cuerpo/cabeza/manos frontales (las direcciones 4, 5 y 6 son la 2, 1 y 0 en espejo)
DEF_WIN = (-14, -92, 78, 14)  # ventana por defecto (izq, arriba, der, abajo) en coords del muñeco
AV_CELL_W, AV_CELL_H, AV_SCALE, AV_TITLE_H = 184, 212, 2, 36
# Orden de dibujo (de atrás hacia delante) según hacia dónde mira el muñeco:
#   0-3 mira hacia la derecha/frente: el brazo derecho (objeto) es el cercano -> delante
#   4-7 mira hacia la izquierda/espalda: el brazo derecho queda lejos -> detrás del cuerpo,
#       y el izquierdo (lh) pasa a ser el cercano -> delante del cuerpo
FRONT_ORDER = ("sd", "lh", "bd", "hd", "rh", "it")
FRONT_LEFT_ORDER = ("sd", "rh", "bd", "lh", "hd", "it")  # dir 4: de frente, el objeto tapa la cara
BEHIND_ORDER = ("sd", "rh", "it", "bd", "lh", "hd")
# Al sujetar (crr) de lado (dir 0 y 1) los dedos se enroscan sobre el objeto: la mano va delante.
# Al beber no: el vaso está en los labios y la mano lo taparía.
GRIP_ORDER = ("sd", "lh", "bd", "hd", "it", "rh")
ORDERS = {0: FRONT_ORDER, 1: FRONT_ORDER, 2: FRONT_ORDER, 3: FRONT_ORDER,
          4: FRONT_LEFT_ORDER, 5: BEHIND_ORDER, 6: BEHIND_ORDER, 7: BEHIND_ORDER}
POSE_LABEL = {"crr": "mano", "drk": "beber"}
SHORT_RE = re.compile(r"(h_[a-z]+_[a-z]+_\d+_\d+_\d+)$")
OFFSET_RE = re.compile(
    r"<asset name=['\"]([^'\"]+)['\"][^>]*>\s*<param key=['\"]offset['\"] value=['\"](-?\d+),(-?\d+)['\"]"
)


class SpriteLib:
    """Sprites de un SWF de Habbo con sus offsets (los bitmaps se decodifican bajo demanda)."""

    def __init__(self, data: bytes):
        swf = decompress_swf(data)
        self._bitmaps: dict[int, bytes] = {}
        manifest = ""
        for code, body in iter_tags(swf):
            if code == 36:
                self._bitmaps[struct.unpack_from("<H", body, 0)[0]] = body
            elif code == 87:  # binaryData: el manifest XML con los offsets
                manifest = body[6:].decode("utf-8", "ignore")
        self._offsets = {m[1]: (int(m[2]), int(m[3])) for m in OFFSET_RE.finditer(manifest)}
        self._tags = {}
        for name, tag in symbol_ids(swf).items():
            if m := SHORT_RE.search(name):
                self._tags[m.group(1)] = tag
        self._cache: dict = {}

    def get(self, short: str):
        """(imagen, (offset_x, offset_y)) o None si no existe o es un marcador vacío."""
        if short not in self._cache:
            res = None
            tag, off = self._tags.get(short), self._offsets.get(short)
            if tag in self._bitmaps and off:
                img = _decode_lossless2(self._bitmaps[tag])
                if img and img.width * img.height > 4:  # 1x1 = vacío
                    res = (img, off)
            self._cache[short] = res
        return self._cache[short]


SWAP_SIDE = {"lh": "rh", "rh": "lh"}


def _part(lib: SpriteLib, action: str, part: str, typ: int, d: int, flip_ok: bool, swap: bool = False):
    """(imagen, izquierda, arriba) de una pieza. Las direcciones 4-6 del cuerpo se
    obtienen en espejo de la 2-0 cuando no existen de forma nativa. Al espejar, la
    izquierda y la derecha se intercambian (swap=True): el brazo izquierdo de la
    dirección 4 es el espejo del brazo DERECHO de la dirección 2."""
    s = lib.get(f"h_{action}_{part}_{typ}_{d}_0")
    if s:
        img, (ox, oy) = s
        return img, -ox, -oy
    if flip_ok and d in (4, 5, 6):
        src = SWAP_SIDE.get(part, part) if swap else part
        s = lib.get(f"h_{action}_{src}_{typ}_{6 - d}_0")
        if s:
            img, (ox, oy) = s
            return img.transpose(Image.FLIP_LEFT_RIGHT), 2 * AXIS - (-ox + img.width), -oy
    return None


def _avatar_parts(body: SpriteLib, items: SpriteLib, item_id: int, pose: str, d: int):
    parts = {
        "sd": _part(body, "std", "sd", 1, 0, False),
        "bd": _part(body, "std", "bd", 1, d, True),
        "hd": _part(body, "std", "hd", 1, d, True),
        # Solo el brazo derecho adopta la pose (crr/drk); el izquierdo queda en reposo (std)
        "lh": _part(body, "std", "lh", 1, d, True, swap=True),
        "rh": _part(body, pose, "rh", 1, d, True, swap=True),
        "it": _part(items, pose, "ri", item_id, d, False),  # los objetos no se espejan
    }
    order = GRIP_ORDER if (pose == "crr" and d in (0, 1)) else ORDERS[d]
    return [(k, parts[k]) for k in order if parts[k]]


def _blit(canvas, img, x: int, y: int):
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + img.width, canvas.width), min(y + img.height, canvas.height)
    if x1 > x0 and y1 > y0:
        canvas.alpha_composite(img.crop((x0 - x, y0 - y, x1 - x, y1 - y)), (x0, y0))


def _faded(img, pct: int = 30):
    img = img.copy()
    img.putalpha(img.getchannel("A").point(lambda v: v * pct // 100))
    return img


def _render_avatar_cells(body: SpriteLib, items: SpriteLib, item_id: int, pose: str):
    """Un dibujo del muñeco con el handitem por cada dirección, o None si no hay sprite."""
    dirs = {d: _avatar_parts(body, items, item_id, pose, d) for d in range(8)}
    if not any(k == "it" for parts in dirs.values() for k, _ in parts):
        return None
    l, t, r, b = DEF_WIN
    for parts in dirs.values():
        for _, (img, x, y) in parts:
            l, t, r, b = min(l, x), min(t, y), max(r, x + img.width), max(b, y + img.height)
    w, h = r - l, b - t
    scale = min(AV_SCALE, AV_CELL_W / w, AV_CELL_H / h)  # objetos enormes reducen el zoom
    cells = {}
    for d, parts in dirs.items():
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for k, (img, x, y) in parts:
            _blit(canvas, _faded(img) if k == "sd" else img, x - l, y - t)
        cells[d] = _scaled(canvas, float(int(scale)) if scale >= 1 else scale)
    return cells


def _draw_avatar_page(item_id: int, pose: str, cells) -> bytes:
    width, height = 4 * AV_CELL_W, AV_TITLE_H + 2 * AV_CELL_H
    sheet = Image.new("RGBA", (width, height), (43, 45, 49, 255))
    draw = ImageDraw.Draw(sheet)
    f_big, f_small = _font(20), _font(13)
    muted, text, line = (148, 155, 164, 255), (220, 221, 222, 255), (60, 63, 68, 255)
    draw.text((12, 7), f"#{item_id} - {POSE_LABEL[pose]}", fill=text, font=f_big)
    for d, img in cells.items():
        cx, cy = (d % 4) * AV_CELL_W, AV_TITLE_H + (d // 4) * AV_CELL_H
        draw.rectangle([cx, cy, cx + AV_CELL_W, cy + AV_CELL_H], outline=line)
        draw.text((cx + 8, cy + 6), f"dir {d}", fill=muted, font=f_small)
        _blit(sheet, img, cx + (AV_CELL_W - img.width) // 2, cy + (AV_CELL_H - img.height) // 2)
    buf = io.BytesIO()
    sheet.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _render_avatar(data: bytes, body_data: bytes, ids, poses, max_pages: int):
    items, body = SpriteLib(data), SpriteLib(body_data)
    pages, shown = [], set()
    for i in sorted(ids):
        for pose in poses:
            if len(pages) >= max_pages:
                break
            cells = _render_avatar_cells(body, items, i, pose)
            if cells:
                pages.append(_draw_avatar_page(i, pose, cells))
                shown.add(i)
    return pages, len(shown)


def render_previews(data: bytes, ids, body_data: bytes | None = None,
                    poses: tuple = ("crr",)) -> tuple[list[bytes], int]:
    """Vista previa de los handitems. Con body_data (hh_human_body.swf) dibuja el muñeco
    sosteniendo el objeto en las 8 direcciones; sin él, usa la tabla de respaldo.
    Devuelve (imágenes, cuántos handitems se muestran)."""
    if Image is None:
        return [], 0
    if body_data:
        try:
            pages, shown = _render_avatar(data, body_data, ids, poses, AV_MAX_PAGES)
            if pages:
                return pages, shown
        except Exception as e:  # si algo falla con el muñeco, mejor la tabla que nada
            print(f"⚠️ no se pudo dibujar el muñeco ({e}); uso la tabla de respaldo")
    return _render_table(data, ids)


# ───────────────────────── Red ─────────────────────────
async def get_client_url(session: aiohttp.ClientSession, url: str):
    async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        text = await r.text()
    for line in text.splitlines():
        if line.startswith("flash.client.url="):
            value = line.split("=", 1)[1].strip()
            if value.startswith("//"):
                value = "https:" + value
            return value if value.endswith("/") else value + "/"
    return None


def swf_candidates(client_url: str) -> list[str]:
    cands = [client_url + SWF_NAME]
    if "flash-assets-" not in client_url:
        cands.insert(0, client_url.replace("/gordon/", "/gordon/flash-assets-") + SWF_NAME)
    return cands


async def download_swf(session: aiohttp.ClientSession, client_url: str):
    for url in swf_candidates(client_url):
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 200:
                    return url, await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
    return None, None


async def download_body(session: aiohttp.ClientSession, swf_url: str) -> bytes | None:
    """hh_human_body.swf vive en la misma carpeta que hh_human_item.swf."""
    url = swf_url.rsplit("/", 1)[0] + "/" + BODY_NAME
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status == 200:
                return await r.read()
            print(f"⚠️ {BODY_NAME}: HTTP {r.status} en {url}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"⚠️ {BODY_NAME}: {e}")
    return None


# ───────────────────────── Escaneo ─────────────────────────
@dataclass
class NewItems:
    swf_url: str
    domains: list[str]
    ids: list[int]
    images: list[bytes] = field(default_factory=list)  # PNG de vista previa
    ids_shown: int = 0  # handitems incluidos en las imágenes


@dataclass
class ScanResult:
    first_run: bool = False
    new: list[NewItems] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    known_count: int = 0
    names: set[str] = field(default_factory=set)
    state: dict = field(default_factory=dict)  # estado a guardar DESPUÉS de avisar


async def scan() -> ScanResult:
    """Escanea todos los servidores. No guarda nada: el llamador guarda
    result.state con save_state() una vez avisado, para no perder avisos."""
    state = load_state()
    known = set(state["handitems"])
    seen = list(state["client_urls"])
    result = ScanResult()

    async with aiohttp.ClientSession() as session:
        found = await asyncio.gather(
            *(get_client_url(session, u) for u in DOMAINS.values()), return_exceptions=True
        )
        by_client: dict[str, list[str]] = {}
        for name, res in zip(DOMAINS, found):
            if isinstance(res, Exception):
                result.log.append(f"⚠️ {name}: error leyendo external_variables ({res})")
            elif not res:
                result.log.append(f"⚠️ {name}: no se encontró flash.client.url")
            else:
                by_client.setdefault(res, []).append(name)

        for client_url, doms in by_client.items():
            if client_url in seen:
                continue
            swf_url, data = await download_swf(session, client_url)
            if not data:
                result.log.append(f"⚠️ {', '.join(doms)}: no se pudo descargar {SWF_NAME} ({client_url})")
                continue
            try:
                names, ids = await asyncio.to_thread(analyze_swf, data)
            except Exception as e:
                result.log.append(f"⚠️ {', '.join(doms)}: error procesando SWF ({e})")
                continue
            if not ids:
                result.log.append(f"⚠️ {', '.join(doms)}: el SWF no contiene handitems (¿cambió el formato?)")
                continue
            result.names |= names
            added = sorted(ids - known)
            images, shown = [], 0
            if added and state["initialized"]:  # en la línea base no se avisa
                try:
                    body = await download_body(session, swf_url)
                    if not body:
                        result.log.append(f"⚠️ no se pudo descargar {BODY_NAME}: vista previa sin muñeco")
                    images, shown = await asyncio.to_thread(render_previews, data, added, body)
                except Exception as e:
                    result.log.append(f"⚠️ vista previa no disponible ({e})")
            known |= ids
            seen.append(client_url)
            result.log.append(f"📦 {', '.join(doms)}: {swf_url} → {len(ids)} handitems, {len(added)} nuevos")
            if added:
                result.new.append(NewItems(swf_url, doms, added, images, shown))

    result.first_run = (not state["initialized"]) and bool(known)
    if result.first_run:
        result.new = []  # la primera pasada solo crea la línea base
    result.known_count = len(known)
    result.state = {
        "initialized": state["initialized"] or bool(known),
        "handitems": sorted(known),
        "client_urls": seen[-MAX_SEEN:],
    }
    return result


def embed_batches(item: NewItems, title: str | None = None,
                  per_message: int = IMAGES_PER_MESSAGE) -> list[tuple[list[dict], list[tuple[str, bytes]]]]:
    """Mensajes a enviar: lista de (embeds, adjuntos). Discord agrupa hasta 4 imágenes
    por mensaje (embeds con el mismo url), así que se reparten en varios mensajes."""
    ids = ", ".join(map(str, item.ids))
    if len(ids) > 1000:
        ids = ids[:997] + "..."
    title = title or f"🆕 {len(item.ids)} nuevo(s) handitem(s)"
    batches = []
    for b, start in enumerate(range(0, max(len(item.images), 1), per_message)):
        chunk = item.images[start : start + per_message]
        if b == 0:
            head = {
                "title": title,
                "color": 0x2ECC71,
                "fields": [
                    {"name": "IDs", "value": ids, "inline": False},
                    {"name": "Servidores", "value": ", ".join(item.domains), "inline": False},
                    {"name": "SWF", "value": item.swf_url[:1000], "inline": False},
                ],
            }
            if item.images and item.ids_shown < len(item.ids):
                head["footer"] = {"text": f"Vista previa de {item.ids_shown} de {len(item.ids)} handitems"}
        else:
            head = {"title": f"{title} (continuación)", "color": 0x2ECC71}
        embeds, files = [head], []
        for n, data in enumerate(chunk, 1):
            files.append((f"preview_{n}.png", data))
        if chunk:
            if len(chunk) > 1:
                head["url"] = item.swf_url  # mismo url -> Discord agrupa las imágenes
            head["image"] = {"url": "attachment://preview_1.png"}
            for n in range(2, len(chunk) + 1):
                embeds.append({"url": item.swf_url, "image": {"url": f"attachment://preview_{n}.png"}})
        batches.append((embeds, files))
    return batches
