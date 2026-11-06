import asyncio
import os
from dataclasses import dataclass

import discord
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

from keep_alive import keep_alive

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True  # necessário para ler comandos com prefixo
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "scsearch"
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


@dataclass
class Musica:
    titulo: str
    url_stream: str
    pedido_por: str


filas: dict[int, list[Musica]] = {}


def fila_do(guild_id: int) -> list[Musica]:
    return filas.setdefault(guild_id, [])


def buscar_musica(query: str) -> tuple[str, str]:
    opts = {**YDL_OPTS, "default_search": "scsearch"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return info["title"], info["url"]


async def tocar_proxima(guild: discord.Guild):
    vc = guild.voice_client
    fila = fila_do(guild.id)

    if vc is None:
        return
    if not fila:
        await asyncio.sleep(120)  # desconecta após 2 min ocioso
        if not fila and vc.is_connected() and not vc.is_playing():
            await vc.disconnect()
        return

    musica = fila.pop(0)
    fonte = discord.FFmpegPCMAudio(musica.url_stream, **FFMPEG_OPTS)

    def depois(erro):
        if erro:
            print(f"[dj] Erro na reprodução: {erro}")
        asyncio.run_coroutine_threadsafe(tocar_proxima(guild), bot.loop)

    vc.play(fonte, after=depois)


@bot.event
async def on_ready():
    print(f"🎧 DJ Pilantra online como {bot.user}")


@bot.command(name="tocar", aliases=["t", "play"])
async def tocar(ctx: commands.Context, *, busca: str):
    """Adiciona uma música na fila (link ou nome)."""
    voz = ctx.author.voice
    if not voz or not voz.channel:
        return await ctx.send("🎧 Entra num canal de voz primeiro, camarada.")

    async with ctx.typing():
        try:
            titulo, url_stream = await asyncio.to_thread(buscar_musica, busca)
        except Exception as e:
            print(f"[tocar] Falha ao buscar '{busca}': {e}")
            return await ctx.send("❌ Não consegui encontrar essa música.")

    guild = ctx.guild
    if guild.voice_client is None:
        await voz.channel.connect()
    elif guild.voice_client.channel != voz.channel:
        await guild.voice_client.move_to(voz.channel)

    fila_do(guild.id).append(Musica(titulo, url_stream, ctx.author.display_name))

    vc = guild.voice_client
    if vc.is_playing() or vc.is_paused():
        await ctx.send(f"➕ **{titulo}** entrou na fila.")
    else:
        await ctx.send(f"▶️ Tocando agora: **{titulo}**")
        await tocar_proxima(guild)


@bot.command(name="pular", aliases=["skip", "s"])
async def pular(ctx: commands.Context):
    """Pula a música atual."""
    vc = ctx.guild.voice_client
    if not vc or not vc.is_playing():
        return await ctx.send("❌ Nada tocando agora.")
    vc.stop()  # dispara o `after`, que toca a próxima
    await ctx.send("⏭️ Pulou!")


@bot.command(name="fila", aliases=["q"])
async def fila(ctx: commands.Context):
    """Mostra a fila de músicas."""
    itens = fila_do(ctx.guild.id)
    if not itens:
        return await ctx.send("📭 A fila tá vazia.")
    linhas = [f"`{i}.` **{m.titulo}** (pedido por {m.pedido_por})" for i, m in enumerate(itens, 1)]
    await ctx.send("🎶 **Próximas:**\n" + "\n".join(linhas[:15]))


@bot.command(name="parar", aliases=["stop"])
async def parar(ctx: commands.Context):
    """Para tudo, limpa a fila e sai do canal."""
    guild = ctx.guild
    fila_do(guild.id).clear()
    if guild.voice_client:
        await guild.voice_client.disconnect()
    await ctx.send("⏹️ Parei tudo e saí do canal.")


@bot.command(name="ajuda")
async def ajuda(ctx: commands.Context):
    """Lista os comandos."""
    texto = (
        "🎧 **DJ Pilantra**\n"
        "`.tocar <link ou nome>` (ou `.t`) — adiciona na fila\n"
        "`.pular` (ou `.s`) — pula a música\n"
        "`.fila` (ou `.q`) — mostra a fila\n"
        "`.parar` — para tudo e sai do canal"
    )
    await ctx.send(texto)


token = os.environ.get("DISCORD_TOKEN_DJ")
if token:
    keep_alive()
    bot.run(token)
else:
    print("Erro: DISCORD_TOKEN_DJ não encontrada nas variáveis de ambiente!")