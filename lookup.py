"""Consulta manual: busca handitems por ID y publica su imagen en Discord.
Se lanza desde GitHub Actions (workflow "Consultar handitem")."""
import asyncio
import os
import re
import sys

import aiohttp

import core
from scan import send

MAX_IDS = 60


def parse_ids(text: str) -> list[int]:
    """Acepta: 310 | #310 | 310, 311 | 310 311 | 300-305"""
    ids: set[int] = set()
    for part in re.split(r"[,\s;]+", text.strip()):
        part = part.lstrip("#")
        if not part:
            continue
        if m := re.fullmatch(r"(\d+)-(\d+)", part):
            a, b = sorted((int(m[1]), int(m[2])))
            if b - a >= MAX_IDS:
                raise ValueError(f"Rango demasiado grande (máximo {MAX_IDS} IDs)")
            ids.update(range(a, b + 1))
        elif part.isdigit():
            ids.add(int(part))
        else:
            raise ValueError(f"No entiendo '{part}'. Ejemplos válidos: 310 | 310, 311 | 300-305")
    if not ids:
        raise ValueError("No indicaste ningún ID")
    if len(ids) > MAX_IDS:
        raise ValueError(f"Demasiados IDs (máximo {MAX_IDS})")
    return sorted(ids)


async def main() -> int:
    try:
        wanted = parse_ids(os.environ.get("HANDITEM_IDS", ""))
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    server = (os.getenv("SERVIDOR") or "auto").strip()
    if server != "auto" and server not in core.DOMAINS:
        print(f"❌ Servidor desconocido: {server}")
        return 1
    urls = core.DOMAINS if server == "auto" else {server: core.DOMAINS[server]}
    print(f"Buscando {len(wanted)} ID(s) en: {', '.join(urls)}")

    async with aiohttp.ClientSession() as session:
        found = await asyncio.gather(*(core.get_client_url(session, u) for u in urls.values()), return_exceptions=True)
        by_client: dict[str, list[str]] = {}
        for name, res in zip(urls, found):
            if isinstance(res, Exception) or not res:
                print(f"⚠️ {name}: no se pudo leer flash.client.url ({res})")
            else:
                by_client.setdefault(res, []).append(name)

        versions = []
        for client_url, doms in by_client.items():
            swf_url, data = await core.download_swf(session, client_url)
            if not data:
                print(f"⚠️ {', '.join(doms)}: no se pudo descargar {core.SWF_NAME}")
                continue
            _, ids = await asyncio.to_thread(core.analyze_swf, data)
            versions.append({"doms": doms, "swf_url": swf_url, "data": data, "ids": ids})
        if not versions:
            print("❌ No se pudo descargar ningún SWF")
            return 1

        # Agrupa los IDs según los servidores donde existen
        groups: dict[tuple, dict] = {}
        missing = []
        for i in wanted:
            have = [v for v in versions if i in v["ids"]]
            if not have:
                missing.append(i)
                continue
            doms = tuple(d for v in have for d in v["doms"])
            groups.setdefault(doms, {"ids": [], "v": have[0]})["ids"].append(i)

        for doms, g in groups.items():
            v, ids = g["v"], g["ids"]
            body = await core.download_body(session, v["swf_url"])
            if not body:
                print(f"⚠️ no se pudo descargar {core.BODY_NAME}: vista previa sin muñeco")
            poses = ("crr", "drk") if len(ids) <= 2 else ("crr",)  # "beber" solo en consultas cortas
            images, shown = await asyncio.to_thread(core.render_previews, v["data"], ids, body, poses)
            item = core.NewItems(v["swf_url"], list(doms), ids, images, shown)
            title = f"🔎 Handitem #{ids[0]}" if len(ids) == 1 else f"🔎 {len(ids)} handitems"
            for embeds, files in core.embed_batches(item, title=title):
                await send(session, embeds=embeds, files=files or None)
            print(f"✅ Enviado: {ids} ({', '.join(doms)})")

        if missing:
            where = "todos los servidores" if server == "auto" else server
            await send(session, content=f"❌ No encontré estos IDs en {where}: {', '.join(map(str, missing))}")
            print(f"❌ No encontrados: {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
