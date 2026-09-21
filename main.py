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

def init_novas_tabelas():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS receita_federal (
                    id_discord TEXT,
                    data DATE,
                    lucro_diario INTEGER,
                    PRIMARY KEY (id_discord, data)
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS votos_comunismo (
                    id_discord TEXT PRIMARY KEY
                 )''')
    conn.commit()
    conn.close()

init_novas_tabelas()


# ---------------------------------------------------------------------------
# Backoff persistente contra crash loops
# ---------------------------------------------------------------------------
def aplicar_backoff_de_conexao():
    BACKOFFS_SEGUNDOS = [5, 30, 120, 300, 600]

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
                print(f"[startup] Reinício rápido detectado. Aguardando {espera:.0f}s.")
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
        print(f"[startup] Não consegui aplicar o backoff de conexão: {e}")


def resetar_contador_de_tentativas():
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
        return None, "⚠️ A variável `ODDS_API_KEY` não foi encontrada no Render!"

    dados = None
    regiao_usada = None
    houve_sucesso_http = False

    for regiao in REGIOES_TENTATIVAS:
        url = f"https://api.the-odds-api.com/v4/sports/soccer_brazil_campeonato/odds/?apiKey={API_KEY}&regions={regiao}&markets=h2h"
        try:
            resposta = requests.get(url, timeout=10)
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            continue

        if resposta.status_code != 200:
            continue

        houve_sucesso_http = True
        candidato = resposta.json()
        n_jogos = len(candidato)
        n_com_odds = sum(1 for j in candidato if j.get("bookmakers"))

        if n_com_odds > 0:
            dados = candidato
            regiao_usada = regiao
            break
        elif dados is None and n_jogos > 0:
            dados = candidato
            regiao_usada = regiao

    if dados is None:
        if houve_sucesso_http:
            return {}, "Sucesso"
        return None, ("⚠️ Nenhuma região respondeu com sucesso à API.")

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
        return odds_do_dia, "Sucesso"
    except Exception as e:
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
    except Exception:
        pass
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
        return {}, "Timeout"


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
            "Horario": "SIMULADO (recuperado)",
            "Horario_DT": horario_resolucao + timedelta(minutes=10),
        }
        jogos_simulados[jogo_id] = info

        channel = bot.get_channel(channel_id)
        if channel is None:
            continue

        restante_segundos = (horario_resolucao - agora).total_seconds()
        if restante_segundos <= 0:
            await channel.send(f"🔄 **Recuperando evento perdido:** **{jogo_id}** Sorteando agora...")
            await resolver_simulacao(channel, jogo_id, info)
        else:
            bot.loop.create_task(aguardar_e_simular(channel, jogo_id, restante_segundos, info))


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
    except Exception:
        pass
    finally:
        if jogo_id in jogos_simulados:
            del jogos_simulados[jogo_id]
        remover_jogo_simulado_db(jogo_id)


# ---------------------------------------------------------------------------
# Receita Federal (Leão)
# ---------------------------------------------------------------------------
def aplicar_leao(id_discord: str, lucro_bruto: int) -> tuple[int, int, bool]:
    if lucro_bruto <= 0:
        return lucro_bruto, 0, False

    hoje = (datetime.utcnow() - timedelta(hours=3)).date()
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT lucro_diario FROM receita_federal WHERE id_discord = %s AND data = %s", (id_discord, hoje))
    res = c.fetchone()
    lucro_anterior = int(res[0]) if res else 0

    novo_lucro_diario = lucro_anterior + lucro_bruto
    imposto = 0
    caiu_agora = False

    if lucro_anterior >= 5000:
        imposto = int(lucro_bruto * 0.15)
    elif novo_lucro_diario >= 5000:
        imposto = int(novo_lucro_diario * 0.15)
        caiu_agora = True

    c.execute('''INSERT INTO receita_federal (id_discord, data, lucro_diario)
                 VALUES (%s, %s, %s)
                 ON CONFLICT (id_discord, data) DO UPDATE SET
                 lucro_diario = EXCLUDED.lucro_diario''', (id_discord, hoje, novo_lucro_diario))
    conn.commit()
    conn.close()

    lucro_liquido = lucro_bruto - imposto
    return lucro_liquido, imposto, caiu_agora


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
        await channel.send("🤝 **DEU EMPATE!** Ninguém sai ganhando ou perdendo (aposta devolvida).")

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
            await channel.send(f"↩️ <@{id_discord}> recebeu de volta **{int(valor)} Pilas** (cancelada).")
            continue

        if palpite == vencedor:
            retorno_total = int(valor * odd)
            lucro_bruto = retorno_total - int(valor)
            lucro_liquido, imposto, caiu_agora = aplicar_leao(id_discord, lucro_bruto)
            novo_saldo = saldo + int(valor) + lucro_liquido 
            
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_discord))

            msg_leao = ""
            if caiu_agora:
                msg_leao = f"\n🦁 **RECEITA FEDERAL:** Passou de 5k no dia! O Leão confiscou **{imposto} Pilas** (15% sobre o total diário)."
            elif imposto > 0:
                msg_leao = f"\n🦁 O Leão reteve **{imposto} Pilas** na fonte."

            if odd >= 3.50:
                await channel.send(f"🦓 **A PLATAFORMA TA BUGADA!** <@{id_discord}> faturou absurdos {retorno_total} Pilas numa zebra!{msg_leao}")
                await channel.send("https://c.tenor.com/IoIaVLN2efsAAAAd/tenor.gif")
            else:
                await channel.send(f"✅ <@{id_discord}> ganhou a aposta e recebeu {retorno_total} Pilas!{msg_leao}")
        else:
            if valor >= 500:
                await channel.send(f"📉 **DEU RED!** O loss de {valor} Pilas veio pesado pra <@{id_discord}>.")
                await channel.send("https://c.tenor.com/aSkdq3IU0g0AAAAd/tenor.gif")
            else:
                await channel.send(f"❌ <@{id_discord}> apostou {valor} Pilas e se deu mal. Faz o PIX pra casa!")

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
        print(f"[verificar_resultados_loop] Erro no ciclo: {e}")

@verificar_resultados_loop.error
async def verificar_resultados_loop_error(error):
    if not verificar_resultados_loop.is_running():
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
                await channel.send(f"🚨 **O JOGO ACABOU!**\n⚽ Placar Final: **{t_casa} {score_casa} x {score_fora} {t_fora}**")
                await processar_resultado_interno(channel, jogo_id_real, vencedor)

        except Exception:
            continue


@tasks.loop(hours=24)
async def enviar_ranking_diario():
    await bot.wait_until_ready()
    canal = bot.get_channel(CANAL_RANKING_ID)
    if canal:
        embed_repaginado = gerar_embed_ranking()
        await canal.send(content="⏰ **Fechamento do Mercado!**", embed=embed_repaginado)


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
            if valor_int <= 0: raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Digite um inteiro > 0!", ephemeral=True)

        id_usuario = str(interaction.user.id)
        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
            res = c.fetchone()
            if not res:
                return await interaction.response.send_message("❌ Sem conta! Use `/registrar`.", ephemeral=True)

            saldo = int(res[0])
            if valor_int > saldo:
                return await interaction.response.send_message(f"💸 Saldo insuficiente!", ephemeral=True)

            novo_saldo = saldo - valor_int
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_usuario))
            c.execute("INSERT INTO apostas (id_discord, jogo, palpite, valor, odd) VALUES (%s, %s, %s, %s, %s)",
                      (id_usuario, self.jogo, self.palpite, valor_int, self.odd))
            conn.commit()
        finally:
            conn.close()

        await interaction.response.send_message(f"✅ **Aposta Registrada!** {valor_int} Pilas no **{self.palpite}**.")


class PixModal(discord.ui.Modal, title="Fazer um PIX"):
    valor = discord.ui.TextInput(label="Quantos Pilas quer transferir?", style=discord.TextStyle.short, placeholder="Ex: 100", required=True)

    def __init__(self, destinatario: discord.Member):
        super().__init__()
        self.destinatario = destinatario

    async def on_submit(self, interaction: discord.Interaction):
        try:
            valor_int = int(self.valor.value)
            if valor_int <= 0: raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Valor inválido!", ephemeral=True)

        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
            remetente = c.fetchone()
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(self.destinatario.id),))
            destinatario_db = c.fetchone()

            if not remetente or not destinatario_db:
                return await interaction.response.send_message("❌ Usuário(s) sem conta.", ephemeral=True)
            elif int(remetente[0]) < valor_int:
                return await interaction.response.send_message("💸 Saldo insuficiente!", ephemeral=True)
            else:
                c.execute("UPDATE usuarios SET saldo = saldo - %s WHERE id_discord = %s", (valor_int, str(interaction.user.id)))
                c.execute("UPDATE usuarios SET saldo = saldo + %s WHERE id_discord = %s", (valor_int, str(self.destinatario.id)))
                await interaction.response.send_message(f"💸 **PIX REALIZADO!** para {self.destinatario.mention}!")
            conn.commit()
        finally:
            conn.close()


class PixSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Selecione para quem vai o PIX...")
    async def callback(self, interaction: discord.Interaction):
        destinatario = self.values[0]
        if destinatario.id == interaction.user.id or destinatario.bot:
            return await interaction.response.send_message("❌ Alvo inválido!", ephemeral=True)
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
        options = [discord.SelectOption(label=jogo, description=f"{info['Horario']} | {info['Vencedor_Casa']} x {info['Vencedor_Fora']}", value=jogo) for jogo, info in list(odds.items())[:25]]
        super().__init__(placeholder="Escolha o jogo...", options=options)
        self.odds = odds
    async def callback(self, interaction: discord.Interaction):
        jogo = self.values[0]
        info = self.odds[jogo]
        agora_brasil = datetime.utcnow() - timedelta(hours=3)
        if agora_brasil > info["Horario_DT"] - timedelta(minutes=10):
            return await interaction.response.send_message(f"🚨 Apostas encerradas!", ephemeral=True)
        await interaction.response.send_message(f"⚽ Você escolheu: **{jogo}**", view=BotoesTimes(jogo, info), ephemeral=True)


class JogoView(discord.ui.View):
    def __init__(self, odds):
        super().__init__(timeout=120)
        self.add_item(JogoSelect(odds))


class SimplesButtonView(discord.ui.View):
    def __init__(self, modal_class, label="Abrir"):
        super().__init__(timeout=60)
        self.modal_class = modal_class
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.success)
        btn.callback = self.abrir_modal
        self.add_item(btn)
    async def abrir_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.modal_class())


class AdminButtonView(discord.ui.View):
    def __init__(self, modal_class, label="Admin"):
        super().__init__(timeout=60)
        self.modal_class = modal_class
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        btn.callback = self.abrir_modal
        self.add_item(btn)
    async def abrir_modal(self, interaction: discord.Interaction):
        if not any(role.name == "Pilantra BOT" for role in interaction.user.roles):
            return await interaction.response.send_message("⛔ Tira a mãozinha daí!", ephemeral=True)
        await interaction.response.send_modal(self.modal_class())


class SimularModal(discord.ui.Modal, title="Criar Jogo Simulado"):
    t_casa = discord.ui.TextInput(label="Time da Casa", required=True)
    o_casa = discord.ui.TextInput(label="Odd da Casa", required=True)
    t_fora = discord.ui.TextInput(label="Time de Fora", required=True)
    o_fora = discord.ui.TextInput(label="Odd de Fora", required=True)
    tempo = discord.ui.TextInput(label="Minutos (Máx 10)", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            odd_c, odd_f = float(self.o_casa.value.replace(',', '.')), float(self.o_fora.value.replace(',', '.'))
            t_min = int(self.tempo.value)
            if t_min <= 0 or t_min > 10 or odd_c <= 1 or odd_f <= 1: raise ValueError
        except ValueError:
            return await interaction.response.send_message("❌ Valores inválidos!", ephemeral=True)

        jogo_id = f"{self.t_casa.value} x {self.t_fora.value}"
        agora_brasil = datetime.utcnow() - timedelta(hours=3)
        horario_resolucao_real = agora_brasil + timedelta(minutes=t_min)

        info = {
            "Vencedor_Casa": self.t_casa.value, "Odd_Casa": odd_c,
            "Vencedor_Fora": self.t_fora.value, "Odd_Fora": odd_f,
            "Horario": horario_resolucao_real.strftime("%d/%m às %H:%M (SIMULADO)"),
            "Horario_DT": agora_brasil + timedelta(minutes=t_min + 10),
        }

        jogos_simulados[jogo_id] = info
        salvar_jogo_simulado_db(jogo_id, info, interaction.channel.id, horario_resolucao_real)
        await interaction.response.send_message(f"🎰 **NOVO EVENTO DE CASSINO CRIADO!**\n⏳ Vocês têm **{t_min} minutos**!")
        bot.loop.create_task(aguardar_e_simular(interaction.channel, jogo_id, t_min * 60, info))


class ResultadoModal(discord.ui.Modal, title="Processar Resultado Oficial"):
    jogo = discord.ui.TextInput(label="Nome exato do Jogo")
    vencedor = discord.ui.TextInput(label="Quem ganhou? (ou 'Empate')")
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"⚽ **FIM DE PAPO!**")
        await processar_resultado_interno(interaction.channel, self.jogo.value, self.vencedor.value)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="apostar", description="Abre o menu para apostar nos jogos")
async def apostar(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        odds_completas, erro = await obter_todas_odds_com_timeout(apenas_hoje=False)
        hoje = (datetime.utcnow() - timedelta(hours=3)).date()
        odds = filtrar_odds_por_hoje(odds_completas, hoje)
        if not odds:
            if erro: return await interaction.followup.send(f"⚠️ Erro: {erro}")
            return await interaction.followup.send("❌ Não há jogos abertos no momento.")
        await interaction.followup.send("👇 **Selecione a partida:**", view=JogoView(odds))
    except Exception:
        pass


@bot.tree.command(name="pix", description="Transfere Pilas para outro usuário")
async def pix(interaction: discord.Interaction):
    view = discord.ui.View()
    view.add_item(PixSelect())
    await interaction.response.send_message("💸 **Mercado Interno:** Selecione:", view=view)


@bot.tree.command(name="simular", description="[Admin] Cria um evento de aposta simulado")
@app_commands.checks.has_role("Pilantra BOT")
async def simular(interaction: discord.Interaction):
    await interaction.response.send_message("🎲", view=AdminButtonView(SimularModal, "Criar Evento"))


@bot.tree.command(name="resultado", description="[Admin] Informa o resultado oficial")
@app_commands.checks.has_role("Pilantra BOT")
async def resultado(interaction: discord.Interaction):
    await interaction.response.send_message("⚽", view=AdminButtonView(ResultadoModal, "Informar"))


@bot.tree.command(name="registrar", description="Cria sua conta e recebe 1000 Pilas")
async def registrar(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    id_usuario = str(interaction.user.id)
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
    if c.fetchone():
        await interaction.response.send_message(f"⚠️ {interaction.user.mention}, já é Pilantra!")
    else:
        c.execute("INSERT INTO usuarios (id_discord, saldo) VALUES (%s, %s)", (id_usuario, 1000))
        await interaction.response.send_message(f"🎉 Bem-vindo! Recebeu **1000 Pilas**.")
    conn.commit()
    conn.close()


@bot.tree.command(name="saldo", description="Mostra seu saldo atual de Pilas")
async def saldo(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
    res = c.fetchone()
    conn.close()
    if res: await interaction.response.send_message(f"💰 Seu saldo é **{int(res[0])} Pilas**.")
    else: await interaction.response.send_message(f"⚠️ Use `/registrar`.")


@bot.tree.command(name="jogos", description="Lista os jogos com as odds")
async def jogos(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        odds_completas, _ = await obter_todas_odds_com_timeout(apenas_hoje=False)
        hoje = (datetime.utcnow() - timedelta(hours=3)).date()
        odds = filtrar_odds_por_hoje(odds_completas, hoje)
        if not odds: return await interaction.followup.send("⚽ **Sem jogos hoje!**")
        embed = discord.Embed(title="⚽ Jogos", color=discord.Color.green())
        for jogo, info in odds.items():
            embed.add_field(name=jogo, value=f"{info['Odd_Casa']} x {info['Odd_Fora']}\n⏰ {info['Horario']}", inline=False)
        await interaction.followup.send(embed=embed)
    except Exception:
        pass


@bot.tree.command(name="palpites", description="Mostra suas apostas")
async def palpites(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT jogo, palpite, valor, odd FROM apostas WHERE id_discord = %s", (str(interaction.user.id),))
    apostas = c.fetchall()
    conn.close()
    embed = discord.Embed(title=f"🧾 Bilhete", color=discord.Color.gold())
    if apostas:
        txt = "".join([f"**{a[0]}** -> **{a[1]}** | 💸 {int(a[2])} (Odd: {a[3]})\n" for a in apostas])
        embed.add_field(name="Apostas Ativas", value=txt)
    else:
        embed.add_field(name="Apostas Ativas", value="Nenhuma aposta ativa.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="salario", description="Resgata 350 Pilas (a cada 24h)")
@app_commands.checks.cooldown(1, 86400)
async def salario(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
        res = c.fetchone()
        if not res: return await interaction.response.send_message("❌ Use `/registrar`.")
        novo = int(res[0]) + 350
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo, str(interaction.user.id)))
        conn.commit()
    finally: conn.close()
    await interaction.response.send_message(f"🎁 Novo saldo: {novo} Pilas.")


@bot.tree.command(name="mendigar", description="Pede 150 Pilas (a cada 6h, só se quebrado)")
@app_commands.checks.cooldown(1, 21600)
async def mendigar(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
        res = c.fetchone()
        if not res: return await interaction.response.send_message("❌ Use `/registrar`.")
        if int(res[0]) >= 150: return await interaction.response.send_message(f"🛑 Vá apostar!")
        novo = int(res[0]) + 150
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo, str(interaction.user.id)))
        conn.commit()
    finally: conn.close()
    await interaction.response.send_message(f"🥺 Recebeu **100 Pilas**! Saldo: {novo}")


@bot.tree.command(name="comunismo", description="Vota para instaurar a revolução e dividir o dinheiro de todos igualmente")
async def comunismo(interaction: discord.Interaction):
    id_discord = str(interaction.user.id)
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT 1 FROM usuarios WHERE id_discord = %s", (id_discord,))
    if not c.fetchone():
        return await interaction.response.send_message("❌ Camarada, você precisa de uma conta (`/registrar`) antes de participar da revolução.", ephemeral=True)

    c.execute("INSERT INTO votos_comunismo (id_discord) VALUES (%s) ON CONFLICT DO NOTHING", (id_discord,))
    conn.commit()

    c.execute("SELECT COUNT(*) FROM votos_comunismo")
    votos_atuais = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM usuarios")
    total_usuarios = c.fetchone()[0]

    porcentagem = (votos_atuais / total_usuarios) * 100 if total_usuarios > 0 else 0
    votos_necessarios = math.ceil(total_usuarios * 0.6)

    if porcentagem >= 60.0:
        c.execute("SELECT SUM(saldo) FROM usuarios")
        riqueza_total = int(c.fetchone()[0] or 0)
        media = riqueza_total // total_usuarios if total_usuarios > 0 else 0

        c.execute("UPDATE usuarios SET saldo = %s", (media,))
        c.execute("DELETE FROM votos_comunismo")
        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"☭ **A REVOLUÇÃO CHEGOU!** ☭\n"
            f"O proletariado se uniu! A riqueza total do cassino (**{riqueza_total} Pilas**) foi tomada da burguesia e dividida igualmente.\n"
            f"Todos os camaradas agora possuem exatamente **{media} Pilas**!"
        )
        canal = bot.get_channel(CANAL_RANKING_ID)
        if canal:
            await canal.send("https://media.tenor.com/tHqgU_2k7x8AAAAC/bugs-bunny-communist.gif")
    else:
        conn.close()
        await interaction.response.send_message(
            f"🚩 Seu voto foi registrado, camarada! Temos **{votos_atuais}/{votos_necessarios}** votos necessários para a revolução bater 60%."
        )


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
    partes = [f"**【{fita_emojis[i]}】**" if i == indice_central else fita_emojis[i] for i in range(inicio, fim)]
    return f"{ROLETA_BORDA}\n{'  '.join(partes)}\n{ROLETA_BORDA}"

async def executar_roleta(interaction: discord.Interaction, valor: int, cor_escolhida: str):
    id_usuario = str(interaction.user.id)
    info_aposta = ROLETA_CORES[cor_escolhida]

    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
        res = c.fetchone()
        if not res: return await interaction.response.edit_message(embed=discord.Embed(title="❌ Erro"), view=None)

        saldo_atual = int(res[0])
        if valor > saldo_atual:
            return await interaction.response.edit_message(embed=discord.Embed(title="💸 Saldo insuficiente"), view=None)

        novo_saldo = saldo_atual - valor
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_usuario))
        conn.commit()
    finally:
        conn.close()

    resultado = sortear_cor_roleta()
    fita = gerar_fita_roleta(resultado, tamanho=30)
    indice_final = len(fita) - 1

    embed = discord.Embed(title="🎰 A roleta tá girando...", description=renderizar_janela_roleta(fita, 2), color=discord.Color.dark_grey())
    await interaction.response.edit_message(embed=embed, view=None)

    for indice, delay in zip([2, 6, 11, 16, 20, 23, 25, 27, indice_final], [0.6, 0.6, 0.6, 0.7, 0.7, 0.8, 0.9, 1.0, 1.3]):
        await asyncio.sleep(delay)
        embed.description = renderizar_janela_roleta(fita, indice)
        try: await interaction.edit_original_response(embed=embed)
        except: pass

    ganhou = (resultado == cor_escolhida)
    info_resultado = ROLETA_CORES[resultado]
    embed.description = renderizar_janela_roleta(fita, indice_final)
    embed.clear_fields()
    embed.add_field(name="Sua aposta", value=f"{info_aposta['emoji']} {info_aposta['label']}", inline=True)
    embed.add_field(name="Resultado", value=f"{info_resultado['emoji']} {info_resultado['label']}", inline=True)

    if ganhou:
        retorno_total = valor * info_resultado["multiplicador"]
        lucro_bruto = retorno_total - valor
        lucro_liquido, imposto, caiu_agora = aplicar_leao(id_usuario, lucro_bruto)
        
        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
            saldo_pos_aposta = int(c.fetchone()[0])
            saldo_final = saldo_pos_aposta + valor + lucro_liquido
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (saldo_final, id_usuario))
            conn.commit()
        finally:
            conn.close()

        embed.title = f"{info_resultado['emoji']} GREEN! Bateu certinho!"
        embed.color = discord.Color.green()
        embed.add_field(name="Lucro Bruto", value=f"+{lucro_bruto} Pilas", inline=True)
        
        if caiu_agora:
            embed.add_field(name="🦁 Malha Fina!", value=f"-{imposto} Pilas (Passou de 5k/dia)", inline=True)
        elif imposto > 0:
            embed.add_field(name="🦁 Imposto", value=f"-{imposto} Pilas (15%)", inline=True)
            
        embed.set_footer(text=f"Saldo atual: {saldo_final} Pilas")
    else:
        embed.title = f"{info_resultado['emoji']} RED! Não foi dessa vez"
        embed.color = discord.Color.red()
        embed.add_field(name="Prejuízo", value=f"-{valor} Pilas", inline=True)
        embed.set_footer(text=f"Saldo atual: {novo_saldo} Pilas")

    try: await interaction.edit_original_response(embed=embed)
    except: await interaction.followup.send(embed=embed)


class RoletaEscolhaView(discord.ui.View):
    def __init__(self, autor_id: int, valor: int):
        super().__init__(timeout=30)
        self.autor_id, self.valor, self.message = autor_id, valor, None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message("⛔", ephemeral=True)
            return False
        return True

    async def _escolher(self, interaction, cor):
        self.stop()
        await executar_roleta(interaction, self.valor, cor)

    @discord.ui.button(label="Vermelho", emoji="🔴", style=discord.ButtonStyle.danger)
    async def b_verm(self, i, b): await self._escolher(i, "vermelho")
    @discord.ui.button(label="Preto", emoji="⚫", style=discord.ButtonStyle.secondary)
    async def b_preto(self, i, b): await self._escolher(i, "preto")
    @discord.ui.button(label="Verde", emoji="🟢", style=discord.ButtonStyle.success)
    async def b_verde(self, i, b): await self._escolher(i, "verde")


@bot.tree.command(name="roleta", description="Abre a roleta do cassino")
async def roleta(interaction: discord.Interaction, valor: int):
    if valor <= 0: return await interaction.response.send_message("❌ >0", ephemeral=True)
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(interaction.user.id),))
        res = c.fetchone()
    finally: conn.close()

    if not res: return await interaction.response.send_message("❌ Use `/registrar`.", ephemeral=True)
    if valor > int(res[0]): return await interaction.response.send_message("💸 Saldo insuficiente.", ephemeral=True)

    embed = discord.Embed(title="🎰 Roleta", description=f"Mesa: **{valor} Pilas**\n🔴 (2x) ⚫ (2x) 🟢 (14x)")
    view = RoletaEscolhaView(autor_id=interaction.user.id, valor=valor)
    await interaction.response.send_message(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Crash / Aviator (cassino)
# ---------------------------------------------------------------------------
CRASH_CHANCE_INSTANTANEO = 0.05
CRASH_MULTIPLICADOR_MAXIMO = 10.0
CRASH_TAXA_CRESCIMENTO = 0.15
CRASH_MAX_TICKS = 40
CRASH_DELAY_TICK = 1.5
_CRASH_BLOCOS = "▁▂▃▄▅▆▇█"
_CRASH_LARGURA = 16

def calcular_ponto_de_quebra() -> float:
    if random.random() < CRASH_CHANCE_INSTANTANEO: return 1.00
    r = random.random()
    ponto = max(1.00, 0.99 / (1 - r))
    return round(min(ponto, CRASH_MULTIPLICADOR_MAXIMO), 2)

def calcular_multiplicador_no_tick(tick: int) -> float:
    return round(math.exp(CRASH_TAXA_CRESCIMENTO * tick), 2)

def gerar_sparkline(historico_mults: list) -> str:
    if not historico_mults: return _CRASH_BLOCOS[0]
    janela = historico_mults[-_CRASH_LARGURA:] if len(historico_mults) > _CRASH_LARGURA else historico_mults
    mult_max_visivel = max(max(janela) * 1.15, 1.3)
    linha = []
    for m in janela:
        ratio = (m - 1.0) / max(mult_max_visivel - 1.0, 0.01)
        idx = min(len(_CRASH_BLOCOS) - 1, max(0, round(ratio * (len(_CRASH_BLOCOS) - 1))))
        linha.append(_CRASH_BLOCOS[idx])
    return f"`{''.join(linha)}`"

def gerar_texto_status(sparkline: str, mult_atual: float, tick_saque_mult: float = None, valor_crash: float = None, explodiu_antes_do_saque: bool = False) -> str:
    linhas = [sparkline, f"# {mult_atual:.2f}x"]
    if tick_saque_mult is not None: linhas.append(f"🟡 Você sacou em **{tick_saque_mult:.2f}x**")
    if valor_crash is not None:
        if explodiu_antes_do_saque or tick_saque_mult is None:
            linhas.append(f"💥 Crashou em **{valor_crash:.2f}x**")
        else:
            linhas.append(f"💥 Teria crashado em **{valor_crash:.2f}x**")
    return "\n".join(linhas)


class CrashView(discord.ui.View):
    def __init__(self, autor_id: int, valor: int):
        super().__init__(timeout=90)
        self.autor_id, self.valor = autor_id, valor
        self.retirou = self.encerrado = False
        self.multiplicador_atual = 1.00
        self.tick_atual = 0
        self.tick_saque = self.embed = self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.autor_id

    @discord.ui.button(label="💰 Retirar", style=discord.ButtonStyle.success)
    async def botao_retirar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.encerrado: return
        self.retirou = self.encerrado = True
        self.tick_saque = self.tick_atual
        multiplicador_saque = self.multiplicador_atual
        button.disabled = True
        self.stop()

        id_usuario = str(interaction.user.id)
        ganho_total = int(self.valor * multiplicador_saque)
        lucro_bruto = ganho_total - self.valor
        lucro_liquido, imposto, caiu_agora = aplicar_leao(id_usuario, lucro_bruto)

        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
            res = c.fetchone()
            saldo_atual = int(res[0]) if res else 0
            saldo_final = saldo_atual + self.valor + lucro_liquido
            c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (saldo_final, id_usuario))
            conn.commit()
        finally:
            conn.close()

        embed = self.embed
        embed.title = f"🟡 {interaction.user.display_name} sacou em {multiplicador_saque:.2f}x!"
        
        desc = f"Green de **+{lucro_liquido} Pilas** garantido no bolso (líquido)."
        if caiu_agora:
            desc += f"\n🦁 **RECEITA:** Passou de 5k diários! Retidos **{imposto} Pilas**."
        elif imposto > 0:
            desc += f"\n🦁 **RECEITA:** Imposto de renda reteve **{imposto} Pilas**."
            
        embed.description = desc + f"\n⏳ *Aguarda -- o foguete vai continuar pra você ver até onde ia...*"
        embed.color = discord.Color.yellow()
        embed.clear_fields()
        embed.add_field(name="Aposta", value=f"{self.valor} Pilas", inline=True)
        embed.add_field(name="Sacou em", value=f"{multiplicador_saque:.2f}x", inline=True)
        embed.set_footer(text=f"Saldo atual: {saldo_final} Pilas")
        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="crash", description="Aposte no foguete")
async def crash(interaction: discord.Interaction, valor: int):
    if valor <= 0: return await interaction.response.send_message("❌ >0", ephemeral=True)
    id_usuario = str(interaction.user.id)
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
        res = c.fetchone()
        if not res: return await interaction.response.send_message("❌ Sem conta", ephemeral=True)
        saldo_atual = int(res[0])
        if valor > saldo_atual: return await interaction.response.send_message("💸 Saldo insuficiente.", ephemeral=True)
        novo_saldo = saldo_atual - valor
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_usuario))
        conn.commit()
    finally: conn.close()

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
    view.embed = embed
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()

    tick = 0
    while tick < CRASH_MAX_TICKS:
        multiplicador_atual = calcular_multiplicador_no_tick(tick)
        if multiplicador_atual >= ponto_de_quebra and not view.retirou:
            historico_mults.append(ponto_de_quebra)
            embed.title = f"💥 O Foguete de {interaction.user.display_name} CRASHOU!"
            embed.description = gerar_texto_status(
                gerar_sparkline(historico_mults), ponto_de_quebra, valor_crash=ponto_de_quebra, explodiu_antes_do_saque=True)
            embed.color = discord.Color.red()
            embed.set_field_at(0, name="Aposta perdida", value=f"-{valor} Pilas", inline=True)
            embed.set_field_at(1, name="Quebrou em", value=f"{ponto_de_quebra:.2f}x", inline=True)
            view.encerrado = True
            for item in view.children: item.disabled = True
            try: await interaction.edit_original_response(embed=embed, view=view)
            except: pass
            return

        if view.retirou: break
        view.multiplicador_atual = multiplicador_atual
        view.tick_atual = tick
        historico_mults.append(multiplicador_atual)
        embed.description = gerar_texto_status(gerar_sparkline(historico_mults), multiplicador_atual)
        try: await interaction.edit_original_response(embed=embed)
        except: pass
        await asyncio.sleep(CRASH_DELAY_TICK)
        tick += 1

    if not view.retirou: return

    mult_saque = view.multiplicador_atual
    tick_ghost = tick + 1
    while tick_ghost < CRASH_MAX_TICKS:
        mult_ghost = calcular_multiplicador_no_tick(tick_ghost)
        crashou_agora = mult_ghost >= ponto_de_quebra
        historico_mults.append(ponto_de_quebra if crashou_agora else mult_ghost)

        if crashou_agora:
            embed.title = f"✅ Sacou em {mult_saque:.2f}x — Foguete crashou em {ponto_de_quebra:.2f}x"
            texto_final = "🔥 Saiu antes -- boa decisão!" if mult_saque < ponto_de_quebra else "😅 Quase..."
            embed.description = gerar_texto_status(gerar_sparkline(historico_mults), ponto_de_quebra, tick_saque_mult=mult_saque, valor_crash=ponto_de_quebra) + f"\n\n{texto_final}"
            embed.color = discord.Color.green()
            try: await interaction.edit_original_response(embed=embed, view=view)
            except: pass
            return

        embed.description = gerar_texto_status(gerar_sparkline(historico_mults), mult_ghost, tick_saque_mult=mult_saque)
        try: await interaction.edit_original_response(embed=embed)
        except: pass
        await asyncio.sleep(CRASH_DELAY_TICK)
        tick_ghost += 1

    mult_final = calcular_multiplicador_no_tick(tick_ghost - 1)
    embed.title = f"🚀 Sacou em {mult_saque:.2f}x — Foguete foi além!"
    embed.description = gerar_texto_status(gerar_sparkline(historico_mults), mult_final, tick_saque_mult=mult_saque) + "\n\nPassou sem explodir."
    embed.color = discord.Color.green()
    try: await interaction.edit_original_response(embed=embed, view=view)
    except: pass


@bot.tree.command(name="comandos", description="Lista todos os comandos do bot")
async def comandos(interaction: discord.Interaction):
    embed = discord.Embed(title="📜 Comandos do Pilantra BOT", color=discord.Color.blue())
    embed.add_field(name="/registrar", value="Cria sua conta e recebe 1000 Pilas para começar.", inline=False)
    embed.add_field(name="/saldo", value="Mostra seu saldo atual de Pilas.", inline=False)
    embed.add_field(name="/jogos", value="Lista os jogos do dia com odds.", inline=False)
    embed.add_field(name="/apostar", value="Abre o menu interativo para apostar nos jogos.", inline=False)
    embed.add_field(name="/palpites", value="Mostra seus palpites e apostas.", inline=False)
    embed.add_field(name="/salario", value="Resgata 350 Pilas de salário (a cada 24h).", inline=False)
    embed.add_field(name="/pix", value="Transfere Pilas para outro usuário.", inline=False)
    embed.add_field(name="/mendigar", value="Solicita 100 Pilas de graça (a cada 6h).", inline=False)
    embed.add_field(name="/roleta", value="Abre a roleta do cassino.", inline=False)
    embed.add_field(name="/crash", value="Aposta no foguete.", inline=False)
    embed.add_field(name="/comunismo", value="Vota para dividir o dinheiro de todos.", inline=False)
    embed.add_field(name="/ranking", value="Mostra o ranking.", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ranking", description="Mostra o ranking dos usuários")
async def ranking(interaction: discord.Interaction):
    await interaction.response.send_message(embed=gerar_embed_ranking())


@bot.tree.command(name="addsaldo", description="[Admin] Adiciona Pilas")
@app_commands.checks.has_role("Pilantra BOT")
async def addsaldo(interaction: discord.Interaction, membro: discord.Member, valor: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(membro.id),))
    res = c.fetchone()
    if res:
        novo = int(res[0]) + valor
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo, str(membro.id)))
        await interaction.response.send_message(f"🏦 {valor} Pilas injetados. Saldo: {novo}")
    else: await interaction.response.send_message("❌ Usuário não registrado.")
    conn.commit()
    conn.close()


@bot.tree.command(name="remsaldo", description="[Admin] Remove Pilas")
@app_commands.checks.has_role("Pilantra BOT")
async def remsaldo(interaction: discord.Interaction, membro: discord.Member, valor: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (str(membro.id),))
    res = c.fetchone()
    if res:
        novo = int(res[0]) - valor
        c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo, str(membro.id)))
        await interaction.response.send_message(f"🏦 {valor} Pilas removidos. Saldo: {novo}")
    else: await interaction.response.send_message("❌ Usuário não registrado.")
    conn.commit()
    conn.close()


@bot.tree.command(name="remaposta", description="[Admin] Reembolsa aposta")
@app_commands.checks.has_role("Pilantra BOT")
async def remaposta(interaction: discord.Interaction, membro: discord.Member, jogo: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT valor FROM apostas WHERE id_discord = %s AND jogo = %s", (str(membro.id), jogo))
    apostas_encontradas = c.fetchall()
    if not apostas_encontradas: return await interaction.response.send_message("❌ Nenhuma aposta.")
    total = sum(int(v[0]) for v in apostas_encontradas)
    c.execute("UPDATE usuarios SET saldo = saldo + %s WHERE id_discord = %s", (total, str(membro.id)))
    c.execute("DELETE FROM apostas WHERE id_discord = %s AND jogo = %s", (str(membro.id), jogo))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"🗑️ Reembolso: **{total} Pilas**.")


@bot.tree.command(name="apostasdodia", description="[Admin] Lista apostas ativas")
@app_commands.checks.has_role("Pilantra BOT")
async def apostasdodia(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id_discord, jogo, palpite, valor, odd FROM apostas")
    apostas = c.fetchall()
    conn.close()
    if not apostas: return await interaction.response.send_message("📅 Nenhuma aposta hoje.")
    embed = discord.Embed(title="📅 Apostas Ativas", color=discord.Color.purple())
    for a in apostas: embed.add_field(name=a[1], value=f"<@{a[0]}> apostou em **{a[2]}** | 💸 {int(a[3])}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingRole) or isinstance(error, app_commands.MissingAnyRole): msg = "⛔ Sem permissão!"
    elif isinstance(error, app_commands.CommandOnCooldown): msg = f"⏳ Volte em **{int(error.retry_after//3600)}h**."
    else: msg = "❌ Erro inesperado."
    try: await (interaction.followup.send if interaction.response.is_done() else interaction.response.send_message)(msg, ephemeral=True)
    except: pass


_slash_commands_ja_sincronizados = False
@bot.event
async def on_ready():
    print(f'🔥 Pilantra online como {bot.user}')
    resetar_contador_de_tentativas()
    global _slash_commands_ja_sincronizados
    if not _slash_commands_ja_sincronizados:
        try:
            if DEV_GUILD_ID:
                guild = discord.Object(id=int(DEV_GUILD_ID))
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
            else:
                await bot.tree.sync()
            _slash_commands_ja_sincronizados = True
        except: pass
    try: reconciliar_apostas_orfas()
    except: pass
    try: await retomar_simulacoes()
    except: pass
    if not verificar_resultados_loop.is_running(): verificar_resultados_loop.start()
    if not enviar_ranking_diario.is_running(): enviar_ranking_diario.start()

keep_alive()
token = os.environ.get('DISCORD_TOKEN')
if token:
    aplicar_backoff_de_conexao()
    bot.run(token)
else:
    print("Erro: Token do Discord não encontrado!")
