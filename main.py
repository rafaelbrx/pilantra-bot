import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import requests
import random
import math
import time
import asyncio
import traceback
from datetime import datetime, timedelta
from keep_alive import keep_alive
from db import get_conn, init_db
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

DEV_GUILD_ID = os.environ.get('DEV_GUILD_ID')

CANAL_RESULTADOS_ID = 1523044681313681449
CANAL_RANKING_ID = 1523039770337480874

jogos_simulados = {}


init_db()


# ---------------------------------------------------------------------------
# Backoff persistente contra crash loops
# ---------------------------------------------------------------------------
# Se o Render reiniciar o processo repetidamente em sequência rápida (crash
# loop), cada reinício tentava reconectar ao Discord imediatamente -- e é
# exatamente esse padrão de reconexões rápidas em sequência que a Cloudflare
# identifica como abuso e usa pra ESTENDER o bloqueio (erro 1015), em vez de
# deixá-lo expirar. Como o processo é reiniciado do zero a cada crash, um
# backoff guardado só em memória não sobreviveria -- por isso persistimos no
# Postgres, que continua de pé entre reinícios.

def aplicar_backoff_de_conexao():
    """Roda ANTES de bot.run(). Se detectar que a última tentativa de conexão
    foi há pouco tempo, espera (bloqueando, de propósito -- isso é antes do
    event loop do bot existir) por um período crescente."""
    BACKOFFS_SEGUNDOS = [5, 30, 120, 300, 600]  # 5s, 30s, 2min, 5min, 10min (teto)

    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS bot_startup_control (
                        chave TEXT PRIMARY KEY,
                        ultimo_timestamp TEXT,
                        tentativas INTEGER
                     )''')
        c.execute("SELECT ultimo_timestamp, tentativas FROM bot_startup_control WHERE chave = 'startup'")
        res = c.fetchone()
        agora = datetime.utcnow()

        if res:
            ultimo_timestamp_str, tentativas = res
            ultimo = datetime.fromisoformat(ultimo_timestamp_str)
            segundos_desde_ultimo = (agora - ultimo).total_seconds()
            indice_backoff = min(tentativas, len(BACKOFFS_SEGUNDOS) - 1)
            backoff_necessario = BACKOFFS_SEGUNDOS[indice_backoff]

            if segundos_desde_ultimo < backoff_necessario:
                espera = backoff_necessario - segundos_desde_ultimo
                print(f"[startup] Reinício rápido detectado (última tentativa há {segundos_desde_ultimo:.0f}s, "
                      f"tentativa nº{tentativas + 1}). Aguardando {espera:.0f}s antes de conectar ao Discord "
                      f"-- isso evita alimentar um possível bloqueio da Cloudflare (erro 1015).")
                time.sleep(espera)
            novas_tentativas = min(tentativas + 1, len(BACKOFFS_SEGUNDOS) - 1)
        else:
            novas_tentativas = 0

        c.execute('''INSERT INTO bot_startup_control (chave, ultimo_timestamp, tentativas)
                     VALUES ('startup', %s, %s)
                     ON CONFLICT (chave) DO UPDATE SET
                         ultimo_timestamp = EXCLUDED.ultimo_timestamp,
                         tentativas = EXCLUDED.tentativas''',
                  (agora.isoformat(), novas_tentativas))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[startup] Não consegui aplicar o backoff de conexão (seguindo sem essa proteção): {e}")


def resetar_contador_de_tentativas():
    """Chamado assim que on_ready() dispara com sucesso -- zera o contador,
    já que a conexão funcionou e não faz sentido continuar escalando o
    backoff pra reinícios normais e saudáveis no futuro."""
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''UPDATE bot_startup_control SET tentativas = 0 WHERE chave = 'startup' ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[startup] Não consegui resetar o contador de backoff: {e}")


# ---------------------------------------------------------------------------
# Integração com a The Odds API
# ---------------------------------------------------------------------------

REGIOES_TENTATIVAS = ["eu", "uk", "us"]


def buscar_odds_do_dia():
    API_KEY = os.environ.get('ODDS_API_KEY')
    if not API_KEY:
        print("[buscar_odds_do_dia] ODDS_API_KEY não encontrada nas variáveis de ambiente do Render.")
        return None, "⚠️ A variável `ODDS_API_KEY` não foi encontrada no Render!"

    dados = None
    regiao_usada = None
    houve_sucesso_http = False

    for regiao in REGIOES_TENTATIVAS:
        url = f"https://api.the-odds-api.com/v4/sports/soccer_brazil_campeonato/odds/?apiKey={API_KEY}&regions={regiao}&markets=h2h"
        try:
            resposta = requests.get(url, timeout=10)
        except requests.exceptions.Timeout:
            print(f"[buscar_odds_do_dia] Timeout (10s) na região '{regiao}' -- pulando para a próxima.")
            continue
        except Exception as e:
            print(f"[buscar_odds_do_dia] Falha de rede na região '{regiao}': {e}")
            continue

        restantes = resposta.headers.get('x-requests-remaining')
        print(f"[buscar_odds_do_dia] região={regiao} status={resposta.status_code} requests-restantes={restantes}")

        if resposta.status_code != 200:
            print(f"[buscar_odds_do_dia] Erro na API (região '{regiao}'): {resposta.text[:300]}")
            continue

        houve_sucesso_http = True
        candidato = resposta.json()
        
        n_jogos = len(candidato)
        n_com_odds = sum(1 for j in candidato if j.get("bookmakers"))
        print(f"[buscar_odds_do_dia] região={regiao}: {n_jogos} jogo(s) retornado(s), {n_com_odds} com odds disponíveis.")

        if n_com_odds > 0:
            dados = candidato
            regiao_usada = regiao
            break
        elif dados is None and n_jogos > 0:
            dados = candidato
            regiao_usada = regiao

    if dados is None:
        if houve_sucesso_http:
            print("[buscar_odds_do_dia] Nenhuma região retornou jogos — sem rodada no momento.")
            return {}, "Sucesso"
        return None, ("⚠️ Nenhuma região respondeu com sucesso à API. Confira os logs do Render para mais detalhes.")

    print(f"[buscar_odds_do_dia] Usando dados da região '{regiao_usada}'.")

    try:
        odds_do_dia = {}
        conn = get_conn()
        c = conn.cursor()

        for jogo in dados:
            horario_bruto = jogo.get("commence_time")
            horario_obj = datetime.strptime(horario_bruto, "%Y-%m-%dT%H:%M:%SZ")
            horario_brasil = horario_obj - timedelta(hours=3)
            horario_formatado = horario_brasil.strftime("%d/%m às %H:%M")

            time_casa = jogo.get("home_team")
            time_fora = jogo.get("away_team")

            if jogo.get("bookmakers"):
                mercados = jogo["bookmakers"][0].get("markets", [])
                if mercados and mercados[0].get("outcomes"):
                    resultados = mercados[0]["outcomes"]
                    odd_casa = odd_fora = 0
                    for resultado in resultados:
                        if resultado["name"] == time_casa:
                            odd_casa = resultado["price"]
                        elif resultado["name"] == time_fora:
                            odd_fora = resultado["price"]

                    chave_jogo = f"{time_casa} x {time_fora}"

                    c.execute("""INSERT INTO horarios_jogos (jogo, horario_dt) VALUES (%s, %s)
                                 ON CONFLICT (jogo) DO UPDATE SET horario_dt = EXCLUDED.horario_dt""",
                              (chave_jogo, horario_brasil.isoformat()))

                    odds_do_dia[chave_jogo] = {
                        "Vencedor_Casa": time_casa,
                        "Odd_Casa": odd_casa,
                        "Vencedor_Fora": time_fora,
                        "Odd_Fora": odd_fora,
                        "Horario": horario_formatado,
                        "Horario_DT": horario_brasil,
                    }

        conn.commit()
        conn.close()

        if not odds_do_dia:
            print(f"[buscar_odds_do_dia] {len(dados)} jogo(s) na resposta, mas nenhum tinha bookmaker com mercado h2h aberto ainda.")

        return odds_do_dia, "Sucesso"
    except Exception as e:
        print(f"[buscar_odds_do_dia] Exceção ao processar resposta: {e}")
        return None, str(e)


def buscar_resultados_api():
    API_KEY = os.environ.get('ODDS_API_KEY')
    if not API_KEY:
        return None

    url = f"https://api.the-odds-api.com/v4/sports/soccer_brazil_campeonato/scores/?apiKey={API_KEY}&daysFrom=1"
    try:
        resposta = requests.get(url, timeout=10)
        if resposta.status_code == 200:
            return resposta.json()
        else:
            print(f"[buscar_resultados_api] status={resposta.status_code} body={resposta.text[:300]}")
    except Exception as e:
        print(f"[buscar_resultados_api] Erro ao buscar resultados: {e}")
    return None


async def obter_todas_odds(apenas_hoje: bool = True):
    odds, erro = await asyncio.to_thread(buscar_odds_do_dia)
    erro_real = erro if odds is None else None
    if odds is None:
        odds = {}

    odds = dict(odds)
    odds.update(jogos_simulados)

    if not apenas_hoje:
        return odds, erro_real

    hoje = (datetime.utcnow() - timedelta(hours=3)).date()
    odds = {jogo: info for jogo, info in odds.items() if info["Horario_DT"].date() == hoje}
    return odds, erro_real


async def obter_todas_odds_com_timeout(apenas_hoje: bool = True, segundos: int = 25):
    try:
        return await asyncio.wait_for(obter_todas_odds(apenas_hoje=apenas_hoje), timeout=segundos)
    except asyncio.TimeoutError:
        msg = f"Timeout: a busca demorou mais de {segundos}s"
        print(f"[obter_todas_odds_com_timeout] {msg} -- abortando.")
        return {}, msg


def filtrar_odds_por_hoje(odds_completas: dict, hoje) -> dict:
    return {
        jogo: info for jogo, info in odds_completas.items()
        if info["Horario_DT"].date() == hoje or jogo in jogos_simulados
    }


DIAS_SEMANA_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def formatar_data_extenso(data) -> str:
    return f"{DIAS_SEMANA_PT[data.weekday()]}, {data.strftime('%d/%m')}"


def encontrar_proxima_data_com_jogo(odds_completas: dict, hoje):
    datas_futuras = sorted({
        info["Horario_DT"].date()
        for info in odds_completas.values()
        if info["Horario_DT"].date() > hoje
    })
    return datas_futuras[0] if datas_futuras else None


def gerar_embed_ranking():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id_discord, saldo FROM usuarios ORDER BY saldo DESC LIMIT 10")
    top_usuarios = c.fetchall()
    conn.close()

    if not top_usuarios:
        return discord.Embed(title="📊 Mercado Fechado", description="Nenhum apostador registrado ainda.", color=discord.Color.dark_gray())

    embed = discord.Embed(
        title="🏆 Top 10 Maiores Pilantras",
        description="A nata da casa de apostas! Quem tá luxando e quem tá na lama?",
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url="https://media1.tenor.com/m/mRKpCnz2eAcAAAAC/money-cash.gif")

    for i, (id_discord, saldo) in enumerate(top_usuarios, start=1):
        if i == 1:
            icone = "🥇 **Rei do Camarote**"
        elif i == 2:
            icone = "🥈 **Magnata**"
        elif i == 3:
            icone = "🥉 **Burguês**"
        else:
            icone = f"🏅 **{i}º Lugar**"
        embed.add_field(name=icone, value=f"> <@{id_discord}> — 💰 **{saldo} Pilas**", inline=False)

    embed.set_footer(text="Gaste com sabedoria (ou perca tudo).")
    return embed


# ---------------------------------------------------------------------------
# Persistência dos jogos simulados (cassino)
# ---------------------------------------------------------------------------

def salvar_jogo_simulado_db(jogo_id, info, channel_id, horario_resolucao):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""INSERT INTO jogos_simulados_db
                 (jogo, t_casa, odd_casa, t_fora, odd_fora, horario_resolucao, channel_id)
                 VALUES (%s, %s, %s, %s, %s, %s, %s)
                 ON CONFLICT (jogo) DO UPDATE SET
                     t_casa = EXCLUDED.t_casa, odd_casa = EXCLUDED.odd_casa,
                     t_fora = EXCLUDED.t_fora, odd_fora = EXCLUDED.odd_fora,
                     horario_resolucao = EXCLUDED.horario_resolucao,
                     channel_id = EXCLUDED.channel_id""",
              (jogo_id, info["Vencedor_Casa"], info["Odd_Casa"], info["Vencedor_Fora"], info["Odd_Fora"],
               horario_resolucao.isoformat(), channel_id))
    conn.commit()
    conn.close()


def remover_jogo_simulado_db(jogo_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM jogos_simulados_db WHERE jogo = %s", (jogo_id,))
    conn.commit()
    conn.close()


def reconciliar_apostas_orfas():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT jogo FROM apostas")
    jogos_com_aposta = [r[0] for r in c.fetchall()]

    for jogo in jogos_com_aposta:
        c.execute("SELECT 1 FROM horarios_jogos WHERE jogo = %s", (jogo,))
        eh_real = c.fetchone()
        c.execute("SELECT 1 FROM jogos_simulados_db WHERE jogo = %s", (jogo,))
        eh_simulado_pendente = c.fetchone()

        if eh_real or eh_simulado_pendente:
            continue

        c.execute("SELECT id_discord, valor FROM apostas WHERE jogo = %s", (jogo,))
        apostas_orfas = c.fetchall()
        for id_discord, valor in apostas_orfas:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_discord,))
            res = c.fetchone()
            if res:
                novo_saldo = int(res[0]) + int(valor)
                c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_discord))

        c.execute("DELETE FROM apostas WHERE jogo = %s", (jogo,))
        print(f"[reconciliação] Jogo órfão '{jogo}' encontrado no startup — apostas reembolsadas automaticamente.")

    conn.commit()
    conn.close()


async def retomar_simulacoes():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT jogo, t_casa, odd_casa, t_fora, odd_fora, horario_resolucao, channel_id FROM jogos_simulados_db")
    rows = c.fetchall()
    conn.close()

    if not rows:
        return

    agora = datetime.utcnow() - timedelta(hours=3)

    for jogo_id, t_casa, odd_casa, t_fora, odd_fora, horario_resolucao_str, channel_id in rows:
        horario_resolucao = datetime.fromisoformat(horario_resolucao_str)
        info = {
            "Vencedor_Casa": t_casa,
            "Odd_Casa": odd_casa,
            "Vencedor_Fora": t_fora,
            "Odd_Fora": odd_fora,
            "Horario": "SIMULADO (recuperado após reinício)",
            "Horario_DT": horario_resolucao + timedelta(minutes=10),
        }
        jogos_simulados[jogo_id] = info

        channel = bot.get_channel(channel_id)
        if channel is None:
            print(f"[retomar_simulacoes] Canal {channel_id} não encontrado/inacessível — não consigo retomar '{jogo_id}'.")
            continue

        restante_segundos = (horario_resolucao - agora).total_seconds()
        if restante_segundos <= 0:
            await channel.send(f"🔄 **Recuperando evento perdido:** o bot reiniciou e **{jogo_id}** já devia ter sido resolvido. Sorteando agora...")
            await resolver_simulacao(channel, jogo_id, info)
        else:
            bot.loop.create_task(aguardar_e_simular(channel, jogo_id, restante_segundos, info))
            print(f"[retomar_simulacoes] '{jogo_id}' retomado, resolve em {int(restante_segundos)}s.")


async def resolver_simulacao(channel, jogo_id, info):
    if jogo_id in jogos_simulados:
        del jogos_simulados[jogo_id]
    remover_jogo_simulado_db(jogo_id)

    t_casa = info["Vencedor_Casa"]
    t_fora = info["Vencedor_Fora"]
    odd_casa = info["Odd_Casa"]
    odd_fora = info["Odd_Fora"]

    prob_casa = 1 / odd_casa
    prob_fora = 1 / odd_fora
    total = prob_casa + prob_fora
    ch_casa = (prob_casa / total) * 100
    ch_fora = (prob_fora / total) * 100

    vencedor = random.choices([t_casa, t_fora], weights=[ch_casa, ch_fora], k=1)[0]

    await channel.send(f"⏰ **TEMPO ESGOTADO!** As apostas para **{jogo_id}** fecharam.\n"
                        f"🎲 **GIRANDO A ROLETA:** {t_casa} ({ch_casa:.1f}%) x {t_fora} ({ch_fora:.1f}%)\n"
                        f"🏆 O sistema cravou: **{vencedor}**! Pagando os ganhadores...")

    await processar_resultado_interno(channel, jogo_id, vencedor)


async def aguardar_e_simular(channel, jogo_id, segundos, info):
    try:
        await asyncio.sleep(segundos)
        await resolver_simulacao(channel, jogo_id, info)
    except Exception as e:
        print(f"[aguardar_e_simular] Erro ao resolver '{jogo_id}': {e}")
    finally:
        if jogo_id in jogos_simulados:
            del jogos_simulados[jogo_id]
        remover_jogo_simulado_db(jogo_id)


# ---------------------------------------------------------------------------
# Resolução de apostas
# ---------------------------------------------------------------------------

async def processar_resultado_interno(channel, jogo: str, vencedor: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id_discord, palpite, valor, odd FROM apostas WHERE jogo = %s", (jogo,))
    apostas = c.fetchall()

    if not apostas:
        await channel.send("🤷‍♂️ Ninguém apostou nesse jogo.")
        conn.close()
        return

    eh_empate = vencedor.strip().lower() in ("empate", "draw", "tie")

    if eh_empate:
        await channel.send("🤝 **DEU EMPATE!** Como não existe opção de apostar em empate, "
                            "todo mundo recebe o valor apostado de volta (sem lucro nem prejuízo).")

    for aposta in apostas:
        id_discord, palpite, valor, odd = aposta
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_discord,))
        res = c.fetchone()
        if not res:
            continue
        saldo = int(res[0])

        if eh_empate:
            novo_saldo = saldo + int(valor)
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_discord))
            await channel.send(f"↩️ <@{id_discord}> recebeu de volta **{int(valor)} Pilas** (aposta cancelada por empate).")
            continue

        if palpite == vencedor:
            lucro = int(valor * odd)
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (saldo + lucro, id_discord))

            if odd >= 3.50:
                await channel.send(f"🦓 **A PLATAFORMA TA BUGADA!** <@{id_discord}> faturou absurdos {lucro} Pilas numa zebra!")
                await channel.send("https://c.tenor.com/IoIaVLN2efsAAAAd/tenor.gif")
            else:
                await channel.send(f"✅ <@{id_discord}> ganhou a aposta e recebeu {lucro} Pilas!")
        else:
            if valor >= 500:
                await channel.send(f"📉 **DEU RED!** O loss de {valor} Pilas veio pesado pra <@{id_discord}>, hora de vender o celta.")
                await channel.send("https://c.tenor.com/aSkdq3IU0g0AAAAd/tenor.gif")
            else:
                await channel.send(f"❌ <@{id_discord}> apostou {valor} Pilas e se deu mal. Faz o PIX pra casa de apostas!")

    c.execute("DELETE FROM apostas WHERE jogo = %s", (jogo,))
    conn.commit()
    conn.close()


import unicodedata


def normalizar_nome_jogo(texto: str) -> str:
    sem_acento = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('ASCII')
    return " ".join(sem_acento.casefold().split())


@tasks.loop(minutes=5)
async def verificar_resultados_loop():
    try:
        await _verificar_resultados_loop_corpo()
    except Exception as e:
        print(f"[verificar_resultados_loop] Erro inesperado no ciclo (loop CONTINUA rodando): {e}")


@verificar_resultados_loop.error
async def verificar_resultados_loop_error(error):
    print(f"[verificar_resultados_loop] Task crashou de forma inesperada: {error}")
    if not verificar_resultados_loop.is_running():
        print("[verificar_resultados_loop] Reiniciando a task automaticamente...")
        verificar_resultados_loop.restart()


async def _verificar_resultados_loop_corpo():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT jogo FROM apostas")
    jogos_pendentes = [row[0] for row in c.fetchall()]

    if not jogos_pendentes:
        conn.close()
        return

    agora_brasil = datetime.utcnow() - timedelta(hours=3)
    precisa_chamar_api = False

    for jogo in jogos_pendentes:
        if jogo in jogos_simulados:
            continue

        c.execute("SELECT horario_dt FROM horarios_jogos WHERE jogo = %s", (jogo,))
        res = c.fetchone()
        if res:
            horario_dt = datetime.fromisoformat(res[0])
            if agora_brasil >= horario_dt + timedelta(minutes=105):
                precisa_chamar_api = True
                break
        else:
            precisa_chamar_api = True
            break

    conn.close()

    if not precisa_chamar_api:
        return

    dados = await asyncio.to_thread(buscar_resultados_api)
    if not dados:
        return

    jogos_pendentes_norm = {normalizar_nome_jogo(j): j for j in jogos_pendentes}

    for jogo in dados:
        try:
            if not jogo.get('completed'):
                continue

            t_casa = jogo.get('home_team')
            t_fora = jogo.get('away_team')
            jogo_id_api = f"{t_casa} x {t_fora}"
            jogo_id_real = jogos_pendentes_norm.get(normalizar_nome_jogo(jogo_id_api))

            if not jogo_id_real:
                continue

            scores = jogo.get('scores')
            if not scores:
                print(f"[verificar_resultados_loop] '{jogo_id_real}' está completed=True mas sem placar.")
                continue

            score_casa = score_fora = 0
            for s in scores:
                if s['name'] == t_casa:
                    score_casa = int(s['score'])
                elif s['name'] == t_fora:
                    score_fora = int(s['score'])

            if score_casa > score_fora:
                vencedor = t_casa
            elif score_fora > score_casa:
                vencedor = t_fora
            else:
                vencedor = "Empate"

            channel = bot.get_channel(CANAL_RESULTADOS_ID)
            if channel:
                await channel.send(f"🚨 **O JOGO ACABOU!**\n⚽ Placar Final: **{t_casa} {score_casa} x {score_fora} {t_fora}**\nProcessando os pagamentos do bot...")
                await processar_resultado_interno(channel, jogo_id_real, vencedor)
            else:
                print(f"[verificar_resultados_loop] Canal de resultados não encontrado.")

        except Exception as e:
            print(f"[verificar_resultados_loop] Erro ao processar resultado: {e}.")
            continue


@tasks.loop(hours=24)
async def enviar_ranking_diario():
    await bot.wait_until_ready()
    canal = bot.get_channel(CANAL_RANKING_ID)
    if canal:
        embed_repaginado = gerar_embed_ranking()
        await canal.send(content="⏰ **Fechamento do Mercado!** Olha como ficou o placar hoje:", embed=embed_repaginado)
    else:
        print(f"[enviar_ranking_diario] Canal de ranking ({CANAL_RANKING_ID}) não encontrado.")


# ---------------------------------------------------------------------------
# Modais e Views
# ---------------------------------------------------------------------------

class ApostaModal(discord.ui.Modal, title="Sua Aposta"):
    valor = discord.ui.TextInput(label="Quantos Pilas quer apostar?", style=discord.TextStyle.short, placeholder="Ex: 500", required=True)

    def __init__(self, jogo, palpite, odd):
        super().__init__()
        self.jogo = jogo
        self.palpite = palpite
        self.odd = odd

    async def on_submit(self, interaction: discord.Interaction):
        try:
            valor_int = int(self.valor.value)
            if valor_int <= 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Digite um número inteiro maior que zero!", ephemeral=True)

        id_usuario = str(interaction.user.id)
        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
            res = c.fetchone()
            if not res:
                return await interaction.response.send_message("❌ Você não tem conta! Use `/registrar`.", ephemeral=True)

            saldo = int(res[0])
            if valor_int > saldo:
                return await interaction.response.send_message(f"💸 Saldo insuficiente! Você só tem {saldo} Pilas.", ephemeral=True)

            novo_saldo = saldo - valor_int
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_usuario))
            c.execute("INSERT INTO apostas (id_discord, jogo, palpite, valor, odd) VALUES (%s, %s, %s, %s, %s)",
                      (id_usuario, self.jogo, self.palpite, valor_int, self.odd))
            conn.commit()
        finally:
            conn.close()

        await interaction.response.send_message(f"✅ **Aposta Registrada!**\nVocê investiu **{valor_int} Pilas** no **{self.palpite}** (Odd: {self.odd}).\nSaldo restante: {novo_saldo} Pilas.")


class PixModal(discord.ui.Modal, title="Fazer um PIX"):
    valor = discord.ui.TextInput(label="Quantos Pilas quer transferir?", style=discord.TextStyle.short, placeholder="Ex: 100", required=True)

    def __init__(self, destinatario: discord.Member):
        super().__init__()
        self.destinatario = destinatario

    async def on_submit(self, interaction: discord.Interaction):
        try:
            valor_int = int(self.valor.value)
            if valor_int <= 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Digite um valor numérico inteiro maior que zero!", ephemeral=True)

        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
            remetente = c.fetchone()
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(self.destinatario.id),))
            destinatario_db = c.fetchone()

            if not remetente:
                return await interaction.response.send_message("❌ Você não tem conta. Use `/registrar`.", ephemeral=True)
            elif not destinatario_db:
                return await interaction.response.send_message("❌ O alvo ainda não tem conta no bot.", ephemeral=True)
            elif int(remetente[0]) < valor_int:
                return await interaction.response.send_message(f"💸 PIX Recusado! Você só tem {remetente[0]} Pilas.", ephemeral=True)
            else:
                novo_remetente = int(remetente[0]) - valor_int
                novo_destinatario = int(destinatario_db[0]) + valor_int
                c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_remetente, str(interaction.user.id)))
                c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_destinatario, str(self.destinatario.id)))
                await interaction.response.send_message(f"💸 **PIX REALIZADO!** {interaction.user.mention} transferiu **{valor_int} Pilas** para {self.destinatario.mention}!")
            conn.commit()
        finally:
            conn.close()


class PixSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Selecione para quem vai o PIX...")

    async def callback(self, interaction: discord.Interaction):
        destinatario = self.values[0]
        if destinatario.id == interaction.user.id:
            return await interaction.response.send_message("❌ Você não pode mandar PIX pra si mesmo!", ephemeral=True)
        if destinatario.bot:
            return await interaction.response.send_message("❌ Robôs não usam dinheiro, escolha um humano!", ephemeral=True)
        await interaction.response.send_modal(PixModal(destinatario))


class BotoesTimes(discord.ui.View):
    def __init__(self, jogo, info):
        super().__init__(timeout=120)
        self.jogo = jogo
        self.info = info

        btn_casa = discord.ui.Button(label=f"{info['Vencedor_Casa']} ({info['Odd_Casa']})", style=discord.ButtonStyle.primary)
        btn_casa.callback = self.apostar_casa
        self.add_item(btn_casa)
        btn_fora = discord.ui.Button(label=f"{info['Vencedor_Fora']} ({info['Odd_Fora']})", style=discord.ButtonStyle.danger)
        btn_fora.callback = self.apostar_fora
        self.add_item(btn_fora)

    async def apostar_casa(self, interaction):
        await interaction.response.send_modal(ApostaModal(self.jogo, self.info['Vencedor_Casa'], self.info['Odd_Casa']))

    async def apostar_fora(self, interaction):
        await interaction.response.send_modal(ApostaModal(self.jogo, self.info['Vencedor_Fora'], self.info['Odd_Fora']))


class JogoSelect(discord.ui.Select):
    def __init__(self, odds):
        options = [discord.SelectOption(label=jogo, description=f"⏰ {info['Horario']} | {info['Vencedor_Casa']} x {info['Vencedor_Fora']}", value=jogo) for jogo, info in list(odds.items())[:25]]
        super().__init__(placeholder="Escolha o jogo que deseja apostar...", options=options)
        self.odds = odds

    async def callback(self, interaction: discord.Interaction):
        jogo = self.values[0]
        info = self.odds[jogo]
        agora_brasil = datetime.utcnow() - timedelta(hours=3)
        if agora_brasil > info["Horario_DT"] - timedelta(minutes=10):
            return await interaction.response.send_message(f"🚨 Apostas para **{jogo}** encerradas!", ephemeral=True)
        await interaction.response.send_message(f"⚽ Você escolheu: **{jogo}**\nQuem vai vencer?", view=BotoesTimes(jogo, info), ephemeral=True)


class JogoView(discord.ui.View):
    def __init__(self, odds):
        super().__init__(timeout=120)
        self.add_item(JogoSelect(odds))


class SimplesButtonView(discord.ui.View):
    def __init__(self, modal_class, label="Abrir Formulário"):
        super().__init__(timeout=60)
        self.modal_class = modal_class
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.success)
        btn.callback = self.abrir_modal
        self.add_item(btn)

    async def abrir_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.modal_class())


class AdminButtonView(discord.ui.View):
    def __init__(self, modal_class, label="Abrir Formulário (Admin)"):
        super().__init__(timeout=60)
        self.modal_class = modal_class
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        btn.callback = self.abrir_modal
        self.add_item(btn)

    async def abrir_modal(self, interaction: discord.Interaction):
        if not any(role.name == "Pilantra BOT" for role in interaction.user.roles):
            return await interaction.response.send_message("⛔ Tira a mãozinha daí! Só administradores podem usar este botão.", ephemeral=True)
        await interaction.response.send_modal(self.modal_class())


class SimularModal(discord.ui.Modal, title="Criar Jogo Simulado (Admin)"):
    t_casa = discord.ui.TextInput(label="Time da Casa", placeholder="Ex: Flamengo", required=True)
    o_casa = discord.ui.TextInput(label="Odd da Casa (Ex: 1.50)", placeholder="1.50", style=discord.TextStyle.short, required=True)
    t_fora = discord.ui.TextInput(label="Time de Fora", placeholder="Ex: Vasco", required=True)
    o_fora = discord.ui.TextInput(label="Odd de Fora (Ex: 3.20)", placeholder="3.20", style=discord.TextStyle.short, required=True)
    tempo = discord.ui.TextInput(label="Duração em Minutos (Máx 10)", placeholder="Ex: 5", style=discord.TextStyle.short, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            odd_c = float(self.o_casa.value.replace(',', '.'))
            odd_f = float(self.o_fora.value.replace(',', '.'))
            t_min = int(self.tempo.value)

            if t_min <= 0 or t_min > 10:
                return await interaction.response.send_message("❌ O tempo deve ser de no máximo 10 minutos!", ephemeral=True)
            if odd_c <= 1 or odd_f <= 1:
                return await interaction.response.send_message("❌ As odds devem ser maiores que 1.0!", ephemeral=True)
        except ValueError:
            return await interaction.response.send_message("❌ Valores inválidos! Use ponto para decimais.", ephemeral=True)

        jogo_id = f"{self.t_casa.value} x {self.t_fora.value}"
        agora_brasil = datetime.utcnow() - timedelta(hours=3)
        horario_fechamento = agora_brasil + timedelta(minutes=t_min)
        horario_resolucao_real = agora_brasil + timedelta(minutes=t_min)

        info = {
            "Vencedor_Casa": self.t_casa.value,
            "Odd_Casa": odd_c,
            "Vencedor_Fora": self.t_fora.value,
            "Odd_Fora": odd_f,
            "Horario": horario_fechamento.strftime("%d/%m às %H:%M (SIMULADO)"),
            "Horario_DT": agora_brasil + timedelta(minutes=t_min + 10),
        }

        jogos_simulados[jogo_id] = info
        salvar_jogo_simulado_db(jogo_id, info, interaction.channel.id, horario_resolucao_real)

        await interaction.response.send_message(
            f"🎰 **NOVO EVENTO DE CASSINO CRIADO!**\n"
            f"⚽ Partida: **{jogo_id}**\n"
            f"📈 Odds: {self.t_casa.value} (**{odd_c}**) x {self.t_fora.value} (**{odd_f}**)\n"
            f"⏳ Vocês têm **{t_min} minutos** para apostar!"
        )
        bot.loop.create_task(aguardar_e_simular(interaction.channel, jogo_id, t_min * 60, info))


class ResultadoModal(discord.ui.Modal, title="Processar Resultado Oficial"):
    jogo = discord.ui.TextInput(label="Nome exato do Jogo", placeholder="Ex: Spain x Austria")
    vencedor = discord.ui.TextInput(label="Quem ganhou? (ou 'Empate')", placeholder="Ex: Spain")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"⚽ **FIM DE PAPO!** O **{self.vencedor.value}** venceu a partida **{self.jogo.value}**! Calculando...")
        await processar_resultado_interno(interaction.channel, self.jogo.value, self.vencedor.value)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="apostar", description="Abre o menu para apostar nos jogos do dia")
async def apostar(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        odds_completas, erro = await obter_todas_odds_com_timeout(apenas_hoje=False)
        hoje = (datetime.utcnow() - timedelta(hours=3)).date()
        odds = filtrar_odds_por_hoje(odds_completas, hoje)

        if not odds:
            if erro:
                await interaction.followup.send(f"⚠️ Não consegui buscar os jogos: {erro}")
                return
            proxima_data = encontrar_proxima_data_com_jogo(odds_completas, hoje)
            if proxima_data:
                await interaction.followup.send(
                    f"❌ Não há jogos hoje. O próximo jogo é em **{formatar_data_extenso(proxima_data)}**.")
            else:
                await interaction.followup.send("❌ Não há jogos abertos no momento.")
            return

        view = JogoView(odds)
        await interaction.followup.send("👇 **Selecione a partida:**", view=view)

    except Exception:
        traceback.print_exc()
        try:
            await interaction.followup.send("❌ Deu erro inesperado ao buscar os jogos. Já registrei os detalhes no log.")
        except Exception as e2:
            print(f"[apostar] Não consegui nem enviar a mensagem de erro: {e2}")


@bot.tree.command(name="pix", description="Transfere Pilas para outro usuário")
async def pix(interaction: discord.Interaction):
    view = discord.ui.View()
    view.add_item(PixSelect())
    await interaction.response.send_message("💸 **Mercado Interno:** Selecione abaixo quem vai receber o PIX:", view=view)


@bot.tree.command(name="simular", description="[Admin] Cria um evento de aposta simulado (cassino)")
@app_commands.checks.has_role("Pilantra BOT")
async def simular(interaction: discord.Interaction):
    await interaction.response.send_message("🎲 Clique para criar seu Evento de Cassino:", view=AdminButtonView(SimularModal, "Criar Evento"))


@bot.tree.command(name="resultado", description="[Admin] Informa o resultado oficial de um jogo")
@app_commands.checks.has_role("Pilantra BOT")
async def resultado(interaction: discord.Interaction):
    await interaction.response.send_message("⚽ Clique para informar quem venceu:", view=AdminButtonView(ResultadoModal, "Informar Resultado"))


@bot.tree.command(name="registrar", description="Cria sua conta e recebe 1000 Pilas para começar")
async def registrar(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    id_usuario = str(interaction.user.id)
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
    if c.fetchone():
        await interaction.response.send_message(f"⚠️ {interaction.user.mention}, você já é um Pilantra!")
    else:
        c.execute("INSERT INTO usuarios (id_discord, saldo) VALUES (%s, %s)", (id_usuario, 1000))
        await interaction.response.send_message(f"🎉 Bem-vindo ao vício, {interaction.user.mention}! Você recebeu **1000 Pilas**.")
        await interaction.followup.send("https://media.tenor.com/i-gbL-IgbbYAAAAi/dodep2.gif")
    conn.commit()
    conn.close()


@bot.tree.command(name="saldo", description="Mostra seu saldo atual de Pilas")
async def saldo(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
    res = c.fetchone()
    conn.close()
    if res:
        await interaction.response.send_message(f"💰 {interaction.user.mention}, seu saldo é **{int(res[0])} Pilas**.")
    else:
        await interaction.response.send_message(f"⚠️ {interaction.user.mention}, você não tem conta! Use `/registrar`.")


@bot.tree.command(name="jogos", description="Lista os jogos de hoje com as odds")
async def jogos(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        odds_completas, erro = await obter_todas_odds_com_timeout(apenas_hoje=False)
        hoje = (datetime.utcnow() - timedelta(hours=3)).date()
        odds = filtrar_odds_por_hoje(odds_completas, hoje)

        if not odds:
            if erro:
                await interaction.followup.send(f"⚠️ Não consegui buscar os jogos: {erro}")
                return
            proxima_data = encontrar_proxima_data_com_jogo(odds_completas, hoje)
            if proxima_data:
                await interaction.followup.send(
                    f"⚽ **Sem jogos hoje!** O próximo jogo é em **{formatar_data_extenso(proxima_data)}**.")
            else:
                await interaction.followup.send("⚽ **Sem jogos hoje!**")
            return

        embed = discord.Embed(title="⚽ Jogos de Hoje", color=discord.Color.green())
        for jogo, info in odds.items():
            texto = (f"**{info['Vencedor_Casa']}** ({info['Odd_Casa']}) ou "
                     f"**{info['Vencedor_Fora']}** ({info['Odd_Fora']})\n"
                     f"⏰ {info['Horario']}")
            embed.add_field(name=jogo, value=texto, inline=False)

        await interaction.followup.send(embed=embed)

    except Exception:
        traceback.print_exc()
        try:
            await interaction.followup.send("❌ Deu erro inesperado ao buscar os jogos. Já registrei os detalhes no log.")
        except Exception as e2:
            print(f"[jogos] Não consegui nem enviar a mensagem de erro: {e2}")


@bot.tree.command(name="palpites", description="Mostra suas apostas registradas")
async def palpites(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    id_us = str(interaction.user.id)
    c.execute("SELECT jogo, palpite, valor, odd FROM apostas WHERE id_discord = %s", (id_us,))
    apostas = c.fetchall()
    conn.close()

    embed = discord.Embed(title=f"🧾 Bilhete de {interaction.user.display_name}", color=discord.Color.gold())

    if apostas:
        txt = "".join([f"⚽ **{a[0]}**\n↳ Palpite: **{a[1]}** | 💸 {int(a[2])} Pilas (Odd: {a[3]})\n\n" for a in apostas])
        embed.add_field(name="📅 Apostas Ativas", value=txt, inline=False)
    else:
        embed.add_field(name="📅 Apostas Ativas", value="Nenhuma aposta ativa hoje.", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="salario", description="Resgata 350 Pilas de salário (a cada 72h)")
@app_commands.checks.cooldown(1, 259200)
async def salario(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
        res = c.fetchone()
        if not res:
            try:
                salario.reset_cooldown(interaction)
            except Exception:
                pass
            return await interaction.response.send_message("❌ Você não tem conta! Use `/registrar`.")
        novo = int(res[0]) + 350
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo, str(interaction.user.id)))
        conn.commit()
    finally:
        conn.close()
    await interaction.response.send_message(f"🎁 {interaction.user.mention} resgatou a diária! Novo saldo: {novo} Pilas.")


@bot.tree.command(name="mendigar", description="Pede 100 Pilas de graça (a cada 24h, só se estiver quebrado)")
@app_commands.checks.cooldown(1, 86400)
async def mendigar(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
        res = c.fetchone()
        if not res:
            try:
                mendigar.reset_cooldown(interaction)
            except Exception:
                pass
            return await interaction.response.send_message("❌ Crie sua conta primeiro com `/registrar`.")

        saldo_atual = int(res[0])
        if saldo_atual >= 100:
            try:
                mendigar.reset_cooldown(interaction)
            except Exception:
                pass
            return await interaction.response.send_message(f"🛑 Você ainda tem {saldo_atual} Pilas. Vá apostar!")

        novo = saldo_atual + 100
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo, str(interaction.user.id)))
        conn.commit()
    finally:
        conn.close()
    await interaction.response.send_message(f"🥺 O sistema teve pena. Você recebeu **100 Pilas**! Saldo: {novo}")


# ---------------------------------------------------------------------------
# Roleta (cassino)
# ---------------------------------------------------------------------------

ROLETA_CORES = {
    "vermelho": {"emoji": "🔴", "label": "Vermelho", "multiplicador": 2, "peso": 7},
    "preto":    {"emoji": "⚫", "label": "Preto",    "multiplicador": 2, "peso": 7},
    "verde":    {"emoji": "🟢", "label": "Verde",    "multiplicador": 14, "peso": 1},
}

ROLETA_BORDA = "━━━━━━━━━━━━━━━━━━━━"


def sortear_cor_roleta() -> str:
    cores = list(ROLETA_CORES.keys())
    pesos = [ROLETA_CORES[c]["peso"] for c in cores]
    return random.choices(cores, weights=pesos, k=1)[0]


def gerar_fita_roleta(resultado: str, tamanho: int = 30) -> list:
    cores = list(ROLETA_CORES.keys())
    pesos = [ROLETA_CORES[c]["peso"] for c in cores]
    fita = [random.choices(cores, weights=pesos, k=1)[0] for _ in range(tamanho)]
    fita[-1] = resultado
    return [ROLETA_CORES[c]["emoji"] for c in fita]


def renderizar_janela_roleta(fita_emojis: list, indice_central: int, janela: int = 5) -> str:
    metade = janela // 2
    inicio = max(0, indice_central - metade)
    fim = min(len(fita_emojis), inicio + janela)
    inicio = max(0, fim - janela)

    partes = []
    for i in range(inicio, fim):
        if i == indice_central:
            partes.append(f"**【{fita_emojis[i]}】**")
        else:
            partes.append(fita_emojis[i])
    linha_fita = "  ".join(partes)
    return f"{ROLETA_BORDA}\n{linha_fita}\n{ROLETA_BORDA}"


async def executar_roleta(interaction: discord.Interaction, valor: int, cor_escolhida: str):
    id_usuario = str(interaction.user.id)
    info_aposta = ROLETA_CORES[cor_escolhida]

    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
        res = c.fetchone()
        if not res:
            embed = discord.Embed(title="❌ Erro", description="Sua conta sumiu? Use `/registrar` de novo.", color=discord.Color.red())
            return await interaction.response.edit_message(embed=embed, view=None)

        saldo_atual = int(res[0])
        if valor > saldo_atual:
            embed = discord.Embed(title="💸 Saldo insuficiente",
                                   description=f"Você só tem **{saldo_atual} Pilas** agora -- rolou algum gasto nesse meio tempo.",
                                   color=discord.Color.red())
            return await interaction.response.edit_message(embed=embed, view=None)

        novo_saldo = saldo_atual - valor
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_usuario))
        conn.commit()
    finally:
        conn.close()

    resultado = sortear_cor_roleta()
    fita = gerar_fita_roleta(resultado, tamanho=30)
    indice_final = len(fita) - 1

    embed = discord.Embed(
        title="🎰 A roleta tá girando...",
        description=renderizar_janela_roleta(fita, 2),
        color=discord.Color.dark_grey(),
    )
    embed.add_field(name="Aposta", value=f"{valor} Pilas no {info_aposta['emoji']} **{info_aposta['label']}**", inline=False)
    await interaction.response.edit_message(embed=embed, view=None)

    checkpoints = [2, 6, 11, 16, 20, 23, 25, 27, indice_final]
    delays =      [0.6, 0.6, 0.6, 0.7, 0.7, 0.8, 0.9, 1.0, 1.3]
    for indice, delay in zip(checkpoints, delays):
        await asyncio.sleep(delay)
        embed.description = renderizar_janela_roleta(fita, indice)
        try:
            await interaction.edit_original_response(embed=embed)
        except discord.HTTPException as e:
            print(f"[roleta] Falha ao editar animação (ignorando, segue pro resultado final): {e}")

    ganhou = (resultado == cor_escolhida)
    info_resultado = ROLETA_CORES[resultado]
    embed.description = renderizar_janela_roleta(fita, indice_final)
    embed.clear_fields()
    embed.add_field(name="Sua aposta", value=f"{info_aposta['emoji']} {info_aposta['label']}", inline=True)
    embed.add_field(name="Resultado", value=f"{info_resultado['emoji']} {info_resultado['label']}", inline=True)

    if ganhou:
        retorno_total = valor * info_resultado["multiplicador"]
        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
            saldo_pos_aposta = int(c.fetchone()[0])
            saldo_final = saldo_pos_aposta + retorno_total
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (saldo_final, id_usuario))
            conn.commit()
        finally:
            conn.close()

        lucro = retorno_total - valor
        embed.title = f"{info_resultado['emoji']} GREEN! Bateu certinho!"
        embed.color = discord.Color.green()
        embed.add_field(name="Lucro", value=f"+{lucro} Pilas (pagou {info_resultado['multiplicador']}x)", inline=True)
        embed.set_footer(text=f"Saldo atual: {saldo_final} Pilas")
    else:
        embed.title = f"{info_resultado['emoji']} RED! Não foi dessa vez"
        embed.color = discord.Color.red()
        embed.add_field(name="Prejuízo", value=f"-{valor} Pilas", inline=True)
        embed.set_footer(text=f"Saldo atual: {novo_saldo} Pilas")

    try:
        await interaction.edit_original_response(embed=embed)
    except discord.HTTPException as e:
        print(f"[roleta] Falha ao editar mensagem final: {e}")
        await interaction.followup.send(embed=embed)


class RoletaEscolhaView(discord.ui.View):
    def __init__(self, autor_id: int, valor: int):
        super().__init__(timeout=30)
        self.autor_id = autor_id
        self.valor = valor
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message("⛔ Essa mesa não é sua -- usa `/roleta` pra abrir a sua.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message is None:
            return
        for item in self.children:
            item.disabled = True
        embed = discord.Embed(
            title="⌛ Tempo esgotado",
            description="Você demorou demais pra escolher a cor. Ninguém foi debitado, tenta de novo quando quiser.",
            color=discord.Color.dark_grey(),
        )
        try:
            await self.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    async def _escolher(self, interaction: discord.Interaction, cor: str):
        self.stop()
        await executar_roleta(interaction, self.valor, cor)

    @discord.ui.button(label="Vermelho", emoji="🔴", style=discord.ButtonStyle.danger)
    async def botao_vermelho(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._escolher(interaction, "vermelho")

    @discord.ui.button(label="Preto", emoji="⚫", style=discord.ButtonStyle.secondary)
    async def botao_preto(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._escolher(interaction, "preto")

    @discord.ui.button(label="Verde (14x)", emoji="🟢", style=discord.ButtonStyle.success)
    async def botao_verde(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._escolher(interaction, "verde")


@bot.tree.command(name="roleta", description="Abre a roleta do cassino -- escolha a cor nos botões depois")
@app_commands.describe(valor="Quantos Pilas você quer colocar na mesa")
async def roleta(interaction: discord.Interaction, valor: int):
    if valor <= 0:
        return await interaction.response.send_message("❌ Aposta tem que ser maior que zero, parceiro.", ephemeral=True)

    id_usuario = str(interaction.user.id)
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
        res = c.fetchone()
    finally:
        conn.close()

    if not res:
        return await interaction.response.send_message("❌ Você ainda não tem banca aberta! Usa `/registrar` primeiro.", ephemeral=True)

    saldo_atual = int(res[0])
    if valor > saldo_atual:
        return await interaction.response.send_message(
            f"💸 Calma, apostador! Você só tem **{saldo_atual} Pilas** na conta -- essa aposta é maior que sua banca.",
            ephemeral=True)

    embed = discord.Embed(
        title="🎰 Roleta do Cassino",
        description=(f"Valor na mesa: **{valor} Pilas**\n\n"
                      f"🔴 **Vermelho** -- paga 2x\n"
                      f"⚫ **Preto** -- paga 2x\n"
                      f"🟢 **Verde** -- paga 14x\n\n"
                      f"Escolhe a cor nos botões abaixo 👇"),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Você tem 30 segundos pra escolher.")

    view = RoletaEscolhaView(autor_id=interaction.user.id, valor=valor)
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()


# ---------------------------------------------------------------------------
# Crash / Aviator (cassino)
# ---------------------------------------------------------------------------

CRASH_CHANCE_INSTANTANEO = 0.05    # 5% de o foguete quebrar direto em 1.00x
CRASH_MULTIPLICADOR_MAXIMO = 50.0  # teto -- ninguém sai rico demais
CRASH_TAXA_CRESCIMENTO = 0.15      # velocidade de subida do multiplicador por tick
CRASH_MAX_TICKS = 40               # rede de segurança contra loop infinito
CRASH_DELAY_TICK = 1.5             # segundos entre cada atualização visual (>= 1.5s obrigatório)

_CRASH_LINHAS = 5      # linhas da grade do gráfico (de baixo pra cima)
_CRASH_LARGURA = 16    # largura em colunas de tick exibidas


def calcular_ponto_de_quebra() -> float:
    """Sorteia em que multiplicador o foguete explode.

    A casa garante a vantagem em duas camadas: 5% de chance de instacrash em
    1.00x (perda total garantida), e nos outros 95% a fórmula hiperbólica
    clássica de jogos crash (0.99 / (1 - r)), com piso em 1.00x e teto em 50x.
    """
    if random.random() < CRASH_CHANCE_INSTANTANEO:
        return 1.00
    r = random.random()  # [0.0, 1.0) -- nunca bate 1.0, então nunca divide por zero
    ponto = max(1.00, 0.99 / (1 - r))
    return round(min(ponto, CRASH_MULTIPLICADOR_MAXIMO), 2)


def calcular_multiplicador_no_tick(tick: int) -> float:
    """Curva exponencial de subida do multiplicador -- começa devagar e
    acelera. tick=0 -> 1.00x."""
    return round(math.exp(CRASH_TAXA_CRESCIMENTO * tick), 2)


_CRASH_BLOCOS = "▁▂▃▄▅▆▇█"  # 8 níveis de altura, monoespaçados, sem emoji misturado


def gerar_sparkline(historico_mults: list) -> str:
    """Renderiza a curva como uma única linha monoespaçada (estilo sparkline),
    usando só caracteres de bloco -- sem misturar emoji no meio de texto
    monoespaçado, que é o que quebrava o alinhamento da versão em grade."""
    if not historico_mults:
        return _CRASH_BLOCOS[0]

    janela = historico_mults[-_CRASH_LARGURA:] if len(historico_mults) > _CRASH_LARGURA else historico_mults
    mult_max = max(janela)
    mult_max_visivel = max(mult_max * 1.15, 1.3)  # margem no topo

    linha = []
    for m in janela:
        ratio = (m - 1.0) / max(mult_max_visivel - 1.0, 0.01)
        idx = min(len(_CRASH_BLOCOS) - 1, max(0, round(ratio * (len(_CRASH_BLOCOS) - 1))))
        linha.append(_CRASH_BLOCOS[idx])
    return f"`{''.join(linha)}`"


def gerar_texto_status(sparkline: str, mult_atual: float, tick_saque_mult: float = None,
                        valor_crash: float = None, explodiu_antes_do_saque: bool = False) -> str:
    """Monta o texto completo mostrado no embed: sparkline + multiplicador
    grande + anotações de saque/crash como linhas de texto separadas (não
    dentro do bloco monoespaçado, evitando o problema de largura do emoji)."""
    linhas = [sparkline, f"# {mult_atual:.2f}x"]

    if tick_saque_mult is not None:
        linhas.append(f"🟡 Você sacou em **{tick_saque_mult:.2f}x**")
    if valor_crash is not None:
        # FIX: usa SEMPRE o ponto_de_quebra real (o mesmo valor do título),
        # nunca recalculado a partir de um tick discreto -- é isso que
        # causava a divergência entre título e legenda (ex.: 4.84x vs 5.21x).
        if explodiu_antes_do_saque or tick_saque_mult is None:
            linhas.append(f"💥 Crashou em **{valor_crash:.2f}x**")
        else:
            linhas.append(f"💥 Teria crashado em **{valor_crash:.2f}x**")

    return "\n".join(linhas)


class CrashView(discord.ui.View):
    """View do jogo Crash. Comunicação com o loop principal via `self.retirou`.
    Após o saque, guarda `self.tick_saque` para o loop continuar animando a
    curva em modo 'fantasma' até revelar onde o foguete teria explodido."""

    def __init__(self, autor_id: int, valor: int):
        super().__init__(timeout=90)
        self.autor_id = autor_id
        self.valor = valor
        self.retirou = False
        self.encerrado = False
        self.multiplicador_atual = 1.00
        self.tick_atual = 0
        self.tick_saque = None
        self.embed = None  # referência ao MESMO Embed usado pelo loop principal
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message(
                "⛔ Esse foguete não é seu -- chama o seu com `/crash`.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.encerrado or self.message is None:
            return
        self.encerrado = True
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="💰 Retirar", style=discord.ButtonStyle.success)
    async def botao_retirar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.encerrado:
            return await interaction.response.send_message(
                "🚀 Já era, esse foguete já decidiu o destino dele.", ephemeral=True)

        self.retirou = True
        self.encerrado = True
        self.tick_saque = self.tick_atual
        multiplicador_saque = self.multiplicador_atual
        button.disabled = True
        self.stop()

        ganho_total = int(self.valor * multiplicador_saque)
        lucro = ganho_total - self.valor
        id_usuario = str(interaction.user.id)

        # FIX: conexão aberta EXCLUSIVAMENTE aqui dentro do callback, e
        # fechada logo em seguida -- isolando por completo a lógica
        # financeira do loop de animação (que não abre banco nenhum).
        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
            res = c.fetchone()
            saldo_atual = int(res[0]) if res else 0
            saldo_final = saldo_atual + ganho_total
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (saldo_final, id_usuario))
            conn.commit()
        finally:
            conn.close()

        embed = self.embed
        # FIX: antes criava um discord.Embed() NOVO aqui, desconectado do
        # objeto usado pelo loop principal em crash(). Quando a fase fantasma
        # continuava e editava o embed ORIGINAL (sem os campos de lucro), o
        # valor ganho desaparecia da mensagem. Agora mutamos o MESMO objeto.
        embed.title = f"🟡 {interaction.user.display_name} sacou em {multiplicador_saque:.2f}x!"
        embed.description = (
            f"Green de **+{lucro} Pilas** garantido no bolso.\n"
            f"⏳ *Aguarda -- o foguete vai continuar pra você ver até onde ia...*"
        )
        embed.color = discord.Color.yellow()
        embed.clear_fields()
        embed.add_field(name="Aposta", value=f"{self.valor} Pilas", inline=True)
        embed.add_field(name="Sacou em", value=f"{multiplicador_saque:.2f}x", inline=True)
        embed.add_field(name="Lucro", value=f"+{lucro} Pilas", inline=True)
        embed.set_footer(text=f"Saldo atual: {saldo_final} Pilas")
        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="crash", description="Aposte no foguete -- retire antes dele explodir!")
@app_commands.describe(valor="Quantos Pilas você quer arriscar no foguete")
async def crash(interaction: discord.Interaction, valor: int):
    if valor <= 0:
        return await interaction.response.send_message(
            "❌ Aposta tem que ser maior que zero, parceiro.", ephemeral=True)

    id_usuario = str(interaction.user.id)

    # --- Validação + débito (conexão fechada ANTES do loop de animação) ---
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
        res = c.fetchone()
        if not res:
            return await interaction.response.send_message(
                "❌ Você ainda não tem banca aberta! Usa `/registrar` primeiro.", ephemeral=True)

        saldo_atual = int(res[0])
        if valor > saldo_atual:
            return await interaction.response.send_message(
                f"💸 Calma, apostador! Você só tem **{saldo_atual} Pilas** -- desce o valor da aposta.",
                ephemeral=True)

        novo_saldo = saldo_atual - valor
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_usuario))
        conn.commit()
    finally:
        conn.close()  # FECHADO antes de qualquer asyncio.sleep

    ponto_de_quebra = calcular_ponto_de_quebra()
    view = CrashView(autor_id=interaction.user.id, valor=valor)

    historico_mults = [1.00]
    embed = discord.Embed(
        title=f"🚀 Foguete de {interaction.user.display_name}",
        description=gerar_texto_status(gerar_sparkline(historico_mults), 1.00),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Aposta", value=f"{valor} Pilas", inline=True)
    embed.add_field(name="Status", value="🚀 Voando...", inline=True)
    view.embed = embed  # FIX: mesma referência usada pelo botão -- nunca mais se perde
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()

    tick = 0

    # -----------------------------------------------------------------------
    # Fase 1: loop normal -- para quando o usuário saca ou quando crasha
    # -----------------------------------------------------------------------
    while tick < CRASH_MAX_TICKS:
        multiplicador_atual = calcular_multiplicador_no_tick(tick)

        if multiplicador_atual >= ponto_de_quebra and not view.retirou:
            # Crashou antes do saque
            historico_mults.append(ponto_de_quebra)
            embed.title = f"💥 O Foguete de {interaction.user.display_name} CRASHOU!"
            embed.description = gerar_texto_status(
                gerar_sparkline(historico_mults), ponto_de_quebra, valor_crash=ponto_de_quebra,
                explodiu_antes_do_saque=True)
            embed.color = discord.Color.red()
            embed.set_field_at(0, name="Aposta perdida", value=f"-{valor} Pilas", inline=True)
            embed.set_field_at(1, name="Quebrou em", value=f"{ponto_de_quebra:.2f}x", inline=True)
            view.encerrado = True
            for item in view.children:
                item.disabled = True
            try:
                await interaction.edit_original_response(embed=embed, view=view)
            except discord.HTTPException as e:
                print(f"[crash] Falha ao editar crash final: {e}")
            return

        if view.retirou:
            break  # usuário sacou -- sai do loop normal e cai na fase 2

        view.multiplicador_atual = multiplicador_atual
        view.tick_atual = tick
        historico_mults.append(multiplicador_atual)

        embed.description = gerar_texto_status(gerar_sparkline(historico_mults), multiplicador_atual)
        try:
            await interaction.edit_original_response(embed=embed)
        except discord.HTTPException as e:
            print(f"[crash] Falha ao editar animação: {e}")

        await asyncio.sleep(CRASH_DELAY_TICK)
        tick += 1

    if not view.retirou:
        return  # já tratou o crash antes do saque acima

    # -----------------------------------------------------------------------
    # Fase 2: usuário sacou -- continua animando (modo "fantasma") até o
    # ponto de quebra real, revelando onde o foguete teria ido. O embed é
    # o MESMO objeto que o botão já preencheu com Aposta/Sacou em/Lucro --
    # daqui pra frente só description/title/color mudam, os campos ficam.
    # -----------------------------------------------------------------------
    mult_saque = view.multiplicador_atual
    tick_ghost = tick + 1

    while tick_ghost < CRASH_MAX_TICKS:
        mult_ghost = calcular_multiplicador_no_tick(tick_ghost)
        crashou_agora = mult_ghost >= ponto_de_quebra
        historico_mults.append(ponto_de_quebra if crashou_agora else mult_ghost)

        if crashou_agora:
            embed.title = (
                f"✅ Sacou em {mult_saque:.2f}x — Foguete crashou em {ponto_de_quebra:.2f}x"
            )
            texto_final = "🔥 Saiu antes -- boa decisão!" if mult_saque < ponto_de_quebra else "😅 Quase..."
            embed.description = (
                gerar_texto_status(gerar_sparkline(historico_mults), ponto_de_quebra,
                                    tick_saque_mult=mult_saque, valor_crash=ponto_de_quebra)
                + f"\n\n{texto_final}"
            )
            embed.color = discord.Color.green()
            try:
                await interaction.edit_original_response(embed=embed, view=view)
            except discord.HTTPException as e:
                print(f"[crash] Falha ao editar reveal final: {e}")
            return

        embed.description = gerar_texto_status(
            gerar_sparkline(historico_mults), mult_ghost, tick_saque_mult=mult_saque)
        try:
            await interaction.edit_original_response(embed=embed)
        except discord.HTTPException as e:
            print(f"[crash] Falha ao editar fantasma: {e}")

        await asyncio.sleep(CRASH_DELAY_TICK)
        tick_ghost += 1

    # chegou no limite de ticks sem crashar (teto de 50x) -- reveal
    mult_final = calcular_multiplicador_no_tick(tick_ghost - 1)
    embed.title = f"🚀 Sacou em {mult_saque:.2f}x — Foguete foi além!"
    embed.description = (
        gerar_texto_status(gerar_sparkline(historico_mults), mult_final, tick_saque_mult=mult_saque)
        + "\n\nO foguete passou sem explodir. Impressionante."
    )
    embed.color = discord.Color.green()
    try:
        await interaction.edit_original_response(embed=embed, view=view)
    except discord.HTTPException as e:
        print(f"[crash] Falha ao editar reveal final (sem crash): {e}")


@bot.tree.command(name="ping", description="Testa se o bot está online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latência: {round(bot.latency * 1000)}ms")


@bot.tree.command(name="comandos", description="Lista todos os comandos do bot")
async def comandos(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Comandos do Pilantra BOT", color=discord.Color.blue())
    embed.add_field(name="/registrar", value="Cria sua conta e recebe 1000 Pilas para começar.", inline=False)
    embed.add_field(name="/saldo", value="Mostra seu saldo atual de Pilas.", inline=False)
    embed.add_field(name="/jogos", value="Lista os jogos do dia com odds.", inline=False)
    embed.add_field(name="/apostar", value="Abre o menu interativo para apostar nos jogos do dia.", inline=False)
    embed.add_field(name="/palpites", value="Mostra seus palpites e apostas registradas.", inline=False)
    embed.add_field(name="/salario", value="Resgata 350 Pilas de salário diário (a cada 72h).", inline=False)
    embed.add_field(name="/pix", value="Transfere Pilas para outro usuário.", inline=False)
    embed.add_field(name="/mendigar", value="Solicita 100 Pilas de graça (a cada 24h).", inline=False)
    embed.add_field(name="/roleta", value="Abre a roleta do cassino: escolha vermelho, preto (2x) ou verde (14x) nos botões.", inline=False)
    embed.add_field(name="/crash", value="Aposta no foguete -- retire antes dele explodir para multiplicar sua aposta.", inline=False)
    embed.add_field(name="/ranking", value="Mostra o ranking dos usuários com mais Pilas.", inline=False)
    embed.add_field(name="Administração", value="/resultado, /simular, /addsaldo, /remsaldo, /remaposta, /apostasdodia", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ranking", description="Mostra o ranking dos usuários com mais Pilas")
async def ranking(interaction: discord.Interaction):
    await interaction.response.send_message(embed=gerar_embed_ranking())


@bot.tree.command(name="addsaldo", description="[Admin] Adiciona Pilas na conta de um usuário")
@app_commands.checks.has_role("Pilantra BOT")
@app_commands.describe(membro="Usuário que vai receber", valor="Quantidade de Pilas a adicionar")
async def addsaldo(interaction: discord.Interaction, membro: discord.Member, valor: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(membro.id),))
    res = c.fetchone()
    if res:
        novo_saldo = int(res[0]) + valor
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, str(membro.id)))
        await interaction.response.send_message(f"🏦 **Administração:** {valor} Pilas injetados na conta de {membro.mention}. Novo saldo: {novo_saldo}")
    else:
        await interaction.response.send_message("❌ Esse usuário não está registrado no bot.")
    conn.commit()
    conn.close()


@bot.tree.command(name="remsaldo", description="[Admin] Remove Pilas da conta de um usuário")
@app_commands.checks.has_role("Pilantra BOT")
@app_commands.describe(membro="Usuário que vai perder Pilas", valor="Quantidade de Pilas a remover")
async def remsaldo(interaction: discord.Interaction, membro: discord.Member, valor: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(membro.id),))
    res = c.fetchone()
    if res:
        novo_saldo = int(res[0]) - valor
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, str(membro.id)))
        await interaction.response.send_message(f"🏦 **Administração:** {valor} Pilas removidos da conta de {membro.mention}. Novo saldo: {novo_saldo}")
    else:
        await interaction.response.send_message("❌ Esse usuário não está registrado no bot.")
    conn.commit()
    conn.close()


@bot.tree.command(name="remaposta", description="[Admin] Cancela e reembolsa a(s) aposta(s) de um usuário num jogo")
@app_commands.checks.has_role("Pilantra BOT")
@app_commands.describe(membro="Dono da aposta", jogo="Nome exato do jogo (veja em /apostasdodia)")
async def remaposta(interaction: discord.Interaction, membro: discord.Member, jogo: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT valor FROM apostas WHERE id_discord = %s AND jogo = %s", (str(membro.id), jogo))
    apostas_encontradas = c.fetchall()

    if not apostas_encontradas:
        await interaction.response.send_message("❌ Nenhuma aposta encontrada com esses dados.")
        conn.close()
        return

    total_reembolso = sum(int(v[0]) for v in apostas_encontradas)

    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(membro.id),))
    res = c.fetchone()
    if res:
        novo_saldo = int(res[0]) + total_reembolso
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, str(membro.id)))

    c.execute("DELETE FROM apostas WHERE id_discord = %s AND jogo = %s", (str(membro.id), jogo))
    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"🗑️ Aposta(s) de {membro.mention} no jogo **{jogo}** foram canceladas e **{total_reembolso} Pilas** foram devolvidas."
    )


@bot.tree.command(name="apostasdodia", description="[Admin] Lista todas as apostas ativas no momento")
@app_commands.checks.has_role("Pilantra BOT")
async def apostasdodia(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id_discord, jogo, palpite, valor, odd FROM apostas")
    apostas = c.fetchall()
    conn.close()

    if not apostas:
        return await interaction.response.send_message("📅 Nenhuma aposta registrada hoje.")

    embed = discord.Embed(title="📅 Apostas Ativas", color=discord.Color.purple())
    for aposta in apostas:
        id_discord, jogo, palpite, valor, odd = aposta
        embed.add_field(name=jogo, value=f"<@{id_discord}> apostou em **{palpite}** | 💸 {int(valor)} Pilas (Odd: {odd})", inline=False)
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Comando de diagnóstico
# ---------------------------------------------------------------------------

@bot.tree.command(name="debugodds", description="[Admin] Mostra o status bruto da API de odds para diagnóstico")
@app_commands.checks.has_role("Pilantra BOT")
async def debugodds(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    API_KEY = os.environ.get('ODDS_API_KEY')
    if not API_KEY:
        return await interaction.followup.send("⚠️ ODDS_API_KEY não configurada.", ephemeral=True)

    linhas = []
    for regiao in REGIOES_TENTATIVAS:
        url = f"https://api.the-odds-api.com/v4/sports/soccer_brazil_campeonato/odds/?apiKey={API_KEY}&regions={regiao}&markets=h2h"
        resposta = await asyncio.to_thread(requests.get, url, timeout=10)
        restantes = resposta.headers.get('x-requests-remaining', '?')
        if resposta.status_code == 200:
            dados = resposta.json()
            n_jogos = len(dados)
            n_com_odds = sum(1 for j in dados if j.get("bookmakers"))
            linhas.append(f"**{regiao}**: {n_jogos} jogo(s), {n_com_odds} com odds | quota restante: {restantes}")
        else:
            linhas.append(f"**{regiao}**: erro HTTP {resposta.status_code} | quota restante: {restantes}")

    embed = discord.Embed(title="🔧 Diagnóstico - The Odds API (Brasileirão)", description="\n".join(linhas), color=discord.Color.orange())
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="statusresultados", description="[Admin] Mostra se o loop de resultados está rodando e o que falta pra cada aposta pendente")
@app_commands.checks.has_role("Pilantra BOT")
async def statusresultados(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    rodando = verificar_resultados_loop.is_running()
    proxima_execucao = verificar_resultados_loop.next_iteration
    falhas_seguidas = verificar_resultados_loop.failed()

    linhas = [
        f"**Loop rodando?** {'✅ Sim' if rodando else '❌ NÃO -- esse é o bug! Reinicie o bot no Render.'}",
        f"**Falhou na última execução?** {'⚠️ Sim' if falhas_seguidas else 'Não'}",
        f"**Próxima verificação:** {proxima_execucao.strftime('%d/%m %H:%M:%S UTC') if proxima_execucao else 'N/A'}",
        "",
    ]

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT jogo FROM apostas")
    jogos_pendentes = [r[0] for r in c.fetchall()]

    if not jogos_pendentes:
        linhas.append("📭 Nenhuma aposta pendente no momento.")
    else:
        agora_brasil = datetime.utcnow() - timedelta(hours=3)
        linhas.append(f"**Apostas pendentes ({len(jogos_pendentes)} jogo(s)):**")
        for jogo in jogos_pendentes:
            if jogo in jogos_simulados:
                linhas.append(f"🎰 {jogo} — evento simulado (resolve pelo próprio timer, não pela API)")
                continue

            c.execute("SELECT horario_dt FROM horarios_jogos WHERE jogo = %s", (jogo,))
            res = c.fetchone()
            if not res:
                linhas.append(f"⚠️ {jogo} — SEM horário registrado em `horarios_jogos` "
                               f"(a API nunca chega a ser consultada pra esse jogo!)")
                continue

            horario_dt = datetime.fromisoformat(res[0])
            minutos_passados = (agora_brasil - horario_dt).total_seconds() / 60
            gate_liberado = minutos_passados >= 105
            status = "✅ liberado pra consultar API" if gate_liberado else f"⏳ faltam {105 - int(minutos_passados)} min pro gate de 105min"
            linhas.append(f"⚽ {jogo} — kickoff há {int(minutos_passados)} min — {status}")

    conn.close()

    embed = discord.Embed(title="🔧 Diagnóstico - Loop de Resultados", description="\n".join(linhas), color=discord.Color.orange())
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Tratamento de erros dos slash commands
# ---------------------------------------------------------------------------

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingRole) or isinstance(error, app_commands.MissingAnyRole):
        mensagem = "⛔ Só o mais pilantra pode usar este comando!"
    elif isinstance(error, app_commands.CommandOnCooldown):
        h = int(error.retry_after // 3600)
        m = int((error.retry_after % 3600) // 60)
        mensagem = f"⏳ Calma aí! Volte daqui a **{h}h e {m}m**."
    elif isinstance(error, app_commands.CheckFailure):
        mensagem = "⛔ Você não tem permissão para usar este comando."
    else:
        original = getattr(error, "original", error)
        print(f"[on_app_command_error] Comando: /{interaction.command.name if interaction.command else '?'} | Erro: {original}")
        mensagem = "❌ Deu ruim ao executar esse comando. Já ficou registrado no log."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(mensagem, ephemeral=True)
        else:
            await interaction.response.send_message(mensagem, ephemeral=True)
    except Exception as e:
        print(f"[on_app_command_error] Não consegui nem responder ao usuário: {e}")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

_slash_commands_ja_sincronizados = False


@bot.event
async def on_ready():
    print(f'🔥 Pilantra online como {bot.user}')

    # Conexão bem-sucedida -- zera o contador de backoff pra não continuar
    # escalando desnecessariamente em reinícios futuros e saudáveis.
    resetar_contador_de_tentativas()

    global _slash_commands_ja_sincronizados
    if _slash_commands_ja_sincronizados:
        print("[sync] Reconexão detectada -- pulando novo bot.tree.sync() (já sincronizado nesta execução).")
    else:
        try:
            if DEV_GUILD_ID:
                guild = discord.Object(id=int(DEV_GUILD_ID))
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                print(f"[sync] {len(synced)} slash commands sincronizados no servidor de testes {DEV_GUILD_ID}.")
            else:
                synced = await bot.tree.sync()
                print(f"[sync] {len(synced)} slash commands sincronizados globalmente (pode levar até 1h pra propagar).")
            _slash_commands_ja_sincronizados = True
        except Exception as e:
            print(f"[sync] Erro ao sincronizar slash commands: {e}")

    try:
        reconciliar_apostas_orfas()
    except Exception as e:
        print(f"[on_ready] Erro ao reconciliar apostas órfãs (não impede o resto do startup): {e}")

    try:
        await retomar_simulacoes()
    except Exception as e:
        print(f"[on_ready] Erro ao retomar simulações (não impede o resto do startup): {e}")

    if not verificar_resultados_loop.is_running():
        verificar_resultados_loop.start()
        print("[on_ready] verificar_resultados_loop INICIADO.")
    else:
        print("[on_ready] verificar_resultados_loop já estava rodando.")

    if not enviar_ranking_diario.is_running():
        enviar_ranking_diario.start()
        print("[on_ready] enviar_ranking_diario INICIADO.")


keep_alive()
token = os.environ.get('DISCORD_TOKEN')
if token:
    aplicar_backoff_de_conexao()
    bot.run(token)
else:
    print("Erro: Token do Discord não encontrado nas variáveis de ambiente!")