import asyncio
import os
import shlex
from dataclasses import dataclass

import discord
import discord.opus
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

from keep_alive import keep_alive

load_dotenv()

# ffmpeg: usa o binário do imageio-ffmpeg se estiver instalado,
# senão usa o ffmpeg do sistema (caso do Render com Docker)
try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_EXE = "ffmpeg"

# Opus: no Windows precisa ser carregado manualmente.
# No Render (Docker com libopus0) o sistema já encontra sozinho.
OPUS_DLL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opus.dll")
if not discord.opus.is_loaded() and os.path.exists(OPUS_DLL):
    discord.opus.load_opus(OPUS_DLL)
print(f"[dj] opus carregado: {discord.opus.is_loaded()}")

intents = discord.Intents.default()
intents.message_content = True  # necessário para ler comandos com prefixo
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "scsearch",
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
    headers: dict


filas: dict[int, list[Musica]] = {}


def fila_do(guild_id: int) -> list[Musica]:
    return filas.setdefault(guild_id, [])


def buscar_musica(query: str) -> tuple[str, str, dict]:
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        headers = info.get("http_headers") or {}
        return info["title"], info["url"], headers


def montar_ffmpeg_opts(headers: dict) -> dict:
    opts = dict(FFMPEG_OPTS)
    if headers:
        texto = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        opts["before_options"] = (
            opts["before_options"] + f" -headers {shlex.quote(texto)}"
        )
    return opts


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
    fonte = discord.FFmpegPCMAudio(
        musica.url_stream,
        executable=FFMPEG_EXE,
        **montar_ffmpeg_opts(musica.headers),
    )

    def depois(erro):
        if erro:
            print(f"[dj] Erro na reprodução: {erro}")
        asyncio.run_coroutine_threadsafe(tocar_proxima(guild), bot.loop)

    vc.play(fonte, after=depois)


@bot.event
async def on_ready():
    print(f"🎧 DJ Pilantra online como {bot.user}")


async def conectar_no_canal(ctx: commands.Context, canal: discord.VoiceChannel):
    guild = ctx.guild
    if guild.voice_client is None:
        await canal.connect()
    elif guild.voice_client.channel != canal:
        await guild.voice_client.move_to(canal)
    return guild.voice_client


@bot.command(name="tocar", aliases=["t", "play"])
async def tocar(ctx: commands.Context, *, busca: str):
    """Adiciona uma música na fila (link ou nome)."""
    voz = ctx.author.voice
    if not voz or not voz.channel:
        return await ctx.send("🎧 Entra num canal de voz primeiro, camarada.")

    async with ctx.typing():
        try:
            titulo, url_stream, headers = await asyncio.to_thread(buscar_musica, busca)
        except Exception as e:
            print(f"[tocar] Falha ao buscar '{busca}': {e}")
            return await ctx.send("❌ Não consegui encontrar essa música.")

    vc = await conectar_no_canal(ctx, voz.channel)

    fila_do(ctx.guild.id).append(
        Musica(titulo, url_stream, ctx.author.display_name, headers)
    )

    if vc.is_playing() or vc.is_paused():
        await ctx.send(f"➕ **{titulo}** entrou na fila.")
    else:
        await ctx.send(f"▶️ Tocando agora: **{titulo}**")
        await tocar_proxima(ctx.guild)


@bot.command(name="teste")
async def teste(ctx: commands.Context):
    """Toca um tom de teste de 5 segundos."""
    voz = ctx.author.voice
    if not voz or not voz.channel:
        return await ctx.send("🎧 Entra numa call primeiro.")

    vc = await conectar_no_canal(ctx, voz.channel)
    if vc.is_playing():
        vc.stop()

    fonte = discord.FFmpegPCMAudio(
        "sine=frequency=440:duration=5",
        before_options="-f lavfi",
        executable=FFMPEG_EXE,
    )
    vc.play(fonte)
    await ctx.send("🔊 Tom de teste de 5 segundos.")


@bot.command(name="pular", aliases=["skip", "s"])
async def pular(ctx: commands.Context):
    """Pula a música atual."""
    vc = ctx.guild.voice_client
    if not vc or not vc.is_playing():
        return await ctx.send("❌ Nada tocando agora.")
    vc.stop()  # dispara o `after`, que toca a próxima
    await ctx.send("⏭️ Pulou!")


@bot.command(name="fila", aliases=["q"])
async def ver_fila(ctx: commands.Context):
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
        "`.parar` — para tudo e sai do canal\n"
        "`.teste` — toca um tom de 5 segundos"
    )
    await ctx.send(texto)


token = os.environ.get("DISCORD_TOKEN_DJ")
if token:
    keep_alive()
    bot.run(token)
else:
    print("Erro: DISCORD_TOKEN_DJ não encontrada nas variáveis de ambiente!")