"""Bot persistente OPCIONAL (comandos !revisar, !handitems, !simbolos).
Necesita un equipo/VPS encendido 24/7. Para el escaneo automático usa GitHub Actions (scan.py)."""
import asyncio
import io
import os

from dotenv import load_dotenv

load_dotenv()  # antes de importar core, que lee variables de entorno

import discord  # noqa: E402
from discord.ext import commands, tasks  # noqa: E402

import core  # noqa: E402

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "5"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
lock = asyncio.Lock()
last_names: set[str] = set()


async def run_scan(channel) -> core.ScanResult:
    global last_names
    async with lock:
        result = await core.scan()
        if result.names:
            last_names = result.names
        if result.first_run:
            await channel.send(f"✅ Línea base guardada: {result.known_count} handitems conocidos.")
        for item in result.new:
            files = [discord.File(io.BytesIO(data), name) for name, data in core.preview_files(item)]
            embeds = [discord.Embed.from_dict(e) for e in core.embed_dicts(item)]
            await channel.send(embeds=embeds, files=files)
        core.save_state(result.state)  # tras avisar
        return result


@tasks.loop(minutes=INTERVAL_MIN)
async def periodic_check():
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        try:
            await run_scan(channel)
        except Exception as e:
            print("Error en revisión periódica:", e)


@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")
    if not periodic_check.is_running():
        periodic_check.start()


@bot.command(name="revisar")
async def revisar(ctx):
    """Fuerza una revisión ahora mismo."""
    msg = await ctx.send("🔍 Escaneando servidores…")
    result = await run_scan(ctx.channel)
    resumen = "\n".join(result.log) or "Sin incidencias."
    if not result.new and not result.first_run:
        resumen += "\nNo hay handitems nuevos."
    await msg.edit(content=resumen[:1900])


@bot.command(name="handitems")
async def handitems(ctx):
    """Lista todos los handitems conocidos."""
    ids = core.load_state()["handitems"]
    if not ids:
        return await ctx.send("Aún no hay datos. Usa `!revisar`.")
    await ctx.send(f"**{len(ids)} handitems conocidos:**\n{', '.join(map(str, ids))[:1800]}")


@bot.command(name="simbolos")
async def simbolos(ctx):
    """Envía los nombres de assets del último SWF analizado en esta sesión."""
    if not last_names:
        return await ctx.send("No hay SWF analizado en esta sesión. Borra state.json y usa `!revisar`.")
    await ctx.send(file=discord.File(io.BytesIO("\n".join(sorted(last_names)).encode()), "simbolos.txt"))


bot.run(TOKEN)
