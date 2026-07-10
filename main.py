import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import requests
import random
import asyncio
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
# Integração com a The Odds API
# ---------------------------------------------------------------------------

def buscar_odds_do_dia():
    API_KEY = os.environ.get('ODDS_API_KEY')
    if not API_KEY:
        return None, "⚠️ A variável `ODDS_API_KEY` não foi encontrada no Render!"

    url = f"https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/?apiKey={API_KEY}&regions=eu&markets=h2h"
    try:
        resposta = requests.get(url)
        if resposta.status_code != 200:
            return None, "⚠️ Erro na API."

        dados = resposta.json()
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

    url = f"https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/scores/?apiKey={API_KEY}&daysFrom=1"
    try:
        resposta = requests.get(url)
        if resposta.status_code == 200:
            return resposta.json()
    except Exception as e:
        print(f"[buscar_resultados_api] Erro ao buscar resultados: {e}")
    return None


async def obter_todas_odds():
    odds, _ = await asyncio.to_thread(buscar_odds_do_dia)
    if odds is None:
        odds = {}
    odds.update(jogos_simulados)
    return odds


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
    """FIX: no startup, recarrega jogos de cassino que ainda não foram
    resolvidos (o bot reiniciou no meio da rodada) e retoma o timer, ou
    resolve na hora se o tempo já tiver estourado."""
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


@tasks.loop(minutes=5)
async def verificar_resultados_loop():
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

    for jogo in dados:
        if jogo.get('completed'):
            t_casa = jogo.get('home_team')
            t_fora = jogo.get('away_team')
            jogo_id = f"{t_casa} x {t_fora}"

            if jogo_id in jogos_pendentes:
                scores = jogo.get('scores')
                if scores:
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
                        await processar_resultado_interno(channel, jogo_id, vencedor)
                    else:
                        print(f"[verificar_resultados_loop] Canal de resultados ({CANAL_RESULTADOS_ID}) não encontrado — não consegui anunciar '{jogo_id}'.")


@tasks.loop(hours=24)
async def enviar_ranking_diario():
    await bot.wait_until_ready()
    canal = bot.get_channel(CANAL_RANKING_ID)
    if canal:
        embed_repaginado = gerar_embed_ranking()
        await canal.send(content="⏰ **Fechamento do Mercado!** Olha como ficou o placar hoje:", embed=embed_repaginado)
    else:
        # FIX: log em vez de falhar em silêncio.
        print(f"[enviar_ranking_diario] Canal de ranking ({CANAL_RANKING_ID}) não encontrado.")


# ---------------------------------------------------------------------------
# Modais e Views (inalterados na lógica, só passam a ser chamados por slash commands)
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


class CampeaoModal(discord.ui.Modal, title="Palpite de Campeão"):
    selecao = discord.ui.TextInput(label="Qual seleção será a campeã?", placeholder="Ex: Brasil", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        conn = get_conn()
        c = conn.cursor()
        id_usuario = str(interaction.user.id)
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
            resultado = c.fetchone()
            if not resultado:
                return await interaction.response.send_message("❌ Você não tem conta! Use `/registrar`.", ephemeral=True)

            saldo_atual = int(resultado[0])
            c.execute("SELECT selecao FROM palpites_campeao WHERE id_discord = %s", (id_usuario,))
            aposta_existente = c.fetchone()

            if aposta_existente:
                taxa = 200
                if saldo_atual < taxa:
                    return await interaction.response.send_message(f"💸 Cadê o dinheiro? Trocar o palpite custa {taxa} Pilas, e você só tem {saldo_atual}.", ephemeral=True)
                palpite_antigo = aposta_existente[0]
                novo_saldo = saldo_atual - taxa
                c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_usuario))
                c.execute("UPDATE palpites_campeao SET selecao = %s WHERE id_discord = %s", (self.selecao.value, id_usuario))
                await interaction.response.send_message(f"🔄 {interaction.user.mention} pagou {taxa} Pilas e trocou o palpite de campeão de **{palpite_antigo}** para **{self.selecao.value}**!\nSaldo: {novo_saldo} Pilas.")
            else:
                c.execute("INSERT INTO palpites_campeao (id_discord, selecao) VALUES (%s, %s)", (id_usuario, self.selecao.value))
                await interaction.response.send_message(f"🏆 {interaction.user.mention} cravou que **{self.selecao.value}** será a campeã da Copa!")
            conn.commit()
        finally:
            conn.close()


class ArtilheiroModal(discord.ui.Modal, title="Palpite de Artilheiro"):
    jogador = discord.ui.TextInput(label="Quem será o artilheiro?", placeholder="Ex: Neymar", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        conn = get_conn()
        c = conn.cursor()
        id_usuario = str(interaction.user.id)
        try:
            c.execute("SELECT saldo FROM usuarios WHERE id_discord = %s", (id_usuario,))
            resultado = c.fetchone()
            if not resultado:
                return await interaction.response.send_message("❌ Você não tem conta! Use `/registrar`.", ephemeral=True)

            saldo_atual = int(resultado[0])
            c.execute("SELECT jogador FROM palpites_artilheiro WHERE id_discord = %s", (id_usuario,))
            aposta_existente = c.fetchone()

            if aposta_existente:
                taxa = 200
                if saldo_atual < taxa:
                    return await interaction.response.send_message(f"💸 Tá quebrado! Trocar o artilheiro custa {taxa} Pilas, e você tem apenas {saldo_atual}.", ephemeral=True)
                palpite_antigo = aposta_existente[0]
                novo_saldo = saldo_atual - taxa
                c.execute("UPDATE usuarios SET saldo = %s WHERE id_discord = %s", (novo_saldo, id_usuario))
                c.execute("UPDATE palpites_artilheiro SET jogador = %s WHERE id_discord = %s", (self.jogador.value, id_usuario))
                await interaction.response.send_message(f"🔄 {interaction.user.mention} pagou {taxa} Pilas e trocou o palpite de artilheiro de **{palpite_antigo}** para **{self.jogador.value}**!\nSaldo: {novo_saldo} Pilas.")
            else:
                c.execute("INSERT INTO palpites_artilheiro (id_discord, jogador) VALUES (%s, %s)", (id_usuario, self.jogador.value))
                await interaction.response.send_message(f"👟 {interaction.user.mention} cravou que **{self.jogador.value}** será o artilheiro da Copa!")
            conn.commit()
        finally:
            conn.close()


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
    odds = await obter_todas_odds()
    if not odds:
        return await interaction.response.send_message("❌ Não há jogos abertos no momento.")
    await interaction.response.send_message("👇 **Selecione a partida:**", view=JogoView(odds))


@bot.tree.command(name="campeao", description="Registra (ou troca) seu palpite de campeão da Copa")
async def campeao(interaction: discord.Interaction):
    await interaction.response.send_message("🏆 Clique para registrar seu Campeão:", view=SimplesButtonView(CampeaoModal, "Palpite Campeão"))


@bot.tree.command(name="artilheiro", description="Registra (ou troca) seu palpite de artilheiro da Copa")
async def artilheiro(interaction: discord.Interaction):
    await interaction.response.send_message("👟 Clique para registrar o Artilheiro:", view=SimplesButtonView(ArtilheiroModal, "Palpite Artilheiro"))


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


@bot.tree.command(name="jogos", description="Lista os jogos do dia com as odds")
async def jogos(interaction: discord.Interaction):
    await interaction.response.defer()
    odds = await obter_todas_odds()
    if not odds:
        return await interaction.followup.send("⚽ **Sem jogos hoje!**")
    embed = discord.Embed(title="⚽ Jogos de Hoje", color=discord.Color.green())
    for jogo, info in list(odds.items())[:15]:
        embed.add_field(name=jogo, value=f"**{info['Vencedor_Casa']}** ({info['Odd_Casa']}) ou **{info['Vencedor_Fora']}** ({info['Odd_Fora']})\n⏰ {info['Horario']}", inline=False)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="palpites", description="Mostra seus palpites e apostas registradas")
async def palpites(interaction: discord.Interaction):
    conn = get_conn()
    c = conn.cursor()
    id_us = str(interaction.user.id)
    c.execute("SELECT selecao FROM palpites_campeao WHERE id_discord = %s", (id_us,))
    camp = c.fetchone()
    c.execute("SELECT jogador FROM palpites_artilheiro WHERE id_discord = %s", (id_us,))
    art = c.fetchone()
    c.execute("SELECT jogo, palpite, valor, odd FROM apostas WHERE id_discord = %s", (id_us,))
    apostas = c.fetchall()
    conn.close()

    embed = discord.Embed(title=f"🧾 Bilhete de {interaction.user.display_name}", color=discord.Color.gold())
    embed.add_field(name="🏆 Campeão", value=f"**{camp[0]}**" if camp else "Vazio", inline=True)
    embed.add_field(name="👟 Artilheiro", value=f"**{art[0]}**" if art else "Vazio", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)

    if apostas:
        txt = "".join([f"⚽ **{a[0]}**\n↳ Palpite: **{a[1]}** | 💸 {int(a[2])} Pilas (Odd: {a[3]})\n\n" for a in apostas])
        embed.add_field(name="📅 Jogos do Dia", value=txt, inline=False)
    else:
        embed.add_field(name="📅 Jogos do Dia", value="Nenhuma aposta ativa hoje.", inline=False)
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
    embed.add_field(name="/campeao", value="Registra seu palpite de campeão da Copa.", inline=False)
    embed.add_field(name="/artilheiro", value="Registra seu palpite de artilheiro da Copa.", inline=False)
    embed.add_field(name="/salario", value="Resgata 350 Pilas de salário diário (a cada 72h).", inline=False)
    embed.add_field(name="/pix", value="Transfere Pilas para outro usuário.", inline=False)
    embed.add_field(name="/mendigar", value="Solicita 100 Pilas de graça (a cada 24h).", inline=False)
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

@bot.event
async def on_ready():
    print(f'🔥 Pilantra online como {bot.user}')

    try:
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"[sync] {len(synced)} slash commands sincronizados no servidor de testes {DEV_GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"[sync] {len(synced)} slash commands sincronizados globalmente (pode levar até 1h pra propagar).")
    except Exception as e:
        print(f"[sync] Erro ao sincronizar slash commands: {e}")

    reconciliar_apostas_orfas()
    await retomar_simulacoes()

    if not verificar_resultados_loop.is_running():
        verificar_resultados_loop.start()
    if not enviar_ranking_diario.is_running():
        enviar_ranking_diario.start()


keep_alive()
token = os.environ.get('DISCORD_TOKEN')
if token:
    bot.run(token)
else:
    print("Erro: Token do Discord não encontrado nas variáveis de ambiente!")