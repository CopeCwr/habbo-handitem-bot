"""Lógica compartida por scan.py (GitHub Actions) y bot.py (bot persistente opcional)."""
import asyncio
import json
import lzma
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

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


def symbol_names(swf: bytes) -> set[str]:
    names = set()
    for code, body in iter_tags(swf):
        if code == 76:  # SymbolClass
            count = struct.unpack_from("<H", body, 0)[0]
            p = 2
            for _ in range(count):
                p += 2  # tag id
                end = body.index(b"\0", p)
                names.add(body[p:end].decode("utf-8", "ignore"))
                p = end + 1
    return names


def analyze_swf(data: bytes) -> tuple[set[str], set[int]]:
    names = symbol_names(decompress_swf(data))
    ids = {int(m.group(1)) for n in names if (m := HANDITEM_REGEX.search(n))}
    return names, ids


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
            known |= ids
            seen.append(client_url)
            result.log.append(f"📦 {', '.join(doms)}: {swf_url} → {len(ids)} handitems, {len(added)} nuevos")
            if added:
                result.new.append(NewItems(swf_url, doms, added))

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


def embed_dict(item: NewItems) -> dict:
    ids = ", ".join(map(str, item.ids))
    if len(ids) > 1000:
        ids = ids[:997] + "..."
    return {
        "title": f"🆕 {len(item.ids)} nuevo(s) handitem(s)",
        "color": 0x2ECC71,
        "fields": [
            {"name": "IDs", "value": ids, "inline": False},
            {"name": "Servidores", "value": ", ".join(item.domains), "inline": False},
            {"name": "SWF", "value": item.swf_url[:1000], "inline": False},
        ],
    }
