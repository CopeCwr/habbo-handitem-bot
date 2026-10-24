"""Escaneo de un solo disparo para GitHub Actions. Avisa a Discord y guarda state.json."""
import asyncio
import os
import sys

import aiohttp

import core

API = "https://discord.com/api/v10"


async def send(session: aiohttp.ClientSession, content: str | None = None, embeds: list | None = None):
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook:
        url, headers = webhook, {}
    else:
        url = f"{API}/channels/{os.environ['CHANNEL_ID']}/messages"
        headers = {"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}"}

    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status >= 300:
            raise RuntimeError(f"Discord respondió {r.status}: {await r.text()}")


async def main() -> int:
    result = await core.scan()
    for line in result.log:
        print(line)
    print(f"Handitems conocidos: {result.known_count}")

    async with aiohttp.ClientSession() as session:
        if result.first_run:
            await send(session, content=f"✅ Línea base guardada: {result.known_count} handitems conocidos. "
                                        "A partir de ahora avisaré de los nuevos.")
        for i in range(0, len(result.new), 10):  # máx. 10 embeds por mensaje
            await send(session, embeds=[core.embed_dict(n) for n in result.new[i : i + 10]])

    # Solo se guarda tras avisar: si Discord falla, el próximo run lo reintenta.
    if core.save_state(result.state):
        print("state.json actualizado")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
