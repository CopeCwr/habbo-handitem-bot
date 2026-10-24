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
MAX_PREVIEW = 18  # handitems que se dibujan en la imagen de vista previa
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


def render_preview(data: bytes, ids, max_items: int = MAX_PREVIEW) -> bytes | None:
    """Hoja PNG con un sprite por handitem. None si no hay Pillow o no hay imágenes."""
    if Image is None:
        return None
    swf = decompress_swf(data)
    bitmaps = {}
    for code, body in iter_tags(swf):
        if code == 36:
            bitmaps[struct.unpack_from("<H", body, 0)[0]] = body

    sprites: dict[int, list] = {}
    for name, tag in symbol_ids(swf).items():
        m = SPRITE_REGEX.search(name)
        if m:  # preferimos "carry", dirección 2 y primer frame
            key = (m.group(1) != "crr", abs(int(m.group(3)) - 2), int(m.group(4)))
            sprites.setdefault(int(m.group(2)), []).append((key, tag))

    tiles = []
    for i in sorted(ids)[:max_items]:
        img = None
        for _, tag in sorted(sprites.get(i, [])):
            if tag in bitmaps:
                img = _decode_lossless2(bitmaps[tag])
                if img:
                    break
        tiles.append((i, img))
    if not any(img for _, img in tiles):
        return None

    cell_w, cell_h, box_w, box_h = 120, 120, 96, 80
    cols = min(6, len(tiles))
    rows = -(-len(tiles) // cols)
    sheet = Image.new("RGBA", (cols * cell_w, rows * cell_h), (43, 45, 49, 255))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=16)
    except TypeError:
        font = ImageFont.load_default()
    for n, (i, img) in enumerate(tiles):
        cx, cy = (n % cols) * cell_w, (n // cols) * cell_h
        if img:
            scale = max(1, min(5, box_w // img.width, box_h // img.height))
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
            sheet.paste(img, (cx + (cell_w - img.width) // 2, cy + 8 + (box_h - img.height) // 2), img)
        label = f"#{i}"
        tw = draw.textlength(label, font=font)
        draw.text((cx + (cell_w - tw) / 2, cy + cell_h - 28), label, fill=(220, 221, 222, 255), font=font)
    buf = io.BytesIO()
    sheet.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


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


# ───────────────────────── Escaneo ─────────────────────────
@dataclass
class NewItems:
    swf_url: str
    domains: list[str]
    ids: list[int]
    image: bytes | None = None  # PNG con la vista previa


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
            image = None
            if added and state["initialized"]:  # en la línea base no se avisa
                try:
                    image = await asyncio.to_thread(render_preview, data, added)
                except Exception as e:
                    result.log.append(f"⚠️ vista previa no disponible ({e})")
            known |= ids
            seen.append(client_url)
            result.log.append(f"📦 {', '.join(doms)}: {swf_url} → {len(ids)} handitems, {len(added)} nuevos")
            if added:
                result.new.append(NewItems(swf_url, doms, added, image))

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


def embed_dict(item: NewItems, title: str | None = None) -> dict:
    ids = ", ".join(map(str, item.ids))
    if len(ids) > 1000:
        ids = ids[:997] + "..."
    embed = {
        "title": title or f"🆕 {len(item.ids)} nuevo(s) handitem(s)",
        "color": 0x2ECC71,
        "fields": [
            {"name": "IDs", "value": ids, "inline": False},
            {"name": "Servidores", "value": ", ".join(item.domains), "inline": False},
            {"name": "SWF", "value": item.swf_url[:1000], "inline": False},
        ],
    }
    if item.image:
        embed["image"] = {"url": "attachment://preview.png"}
        if len(item.ids) > MAX_PREVIEW:
            embed["footer"] = {"text": f"Vista previa de los primeros {MAX_PREVIEW}"}
    return embed
