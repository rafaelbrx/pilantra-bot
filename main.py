import discord
from discord.ext import commands
import os
import sqlite3
import requests
import random
from datetime import datetime, timedelta
from keep_alive import keep_alive
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

def init_db():
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios (id_discord TEXT PRIMARY KEY, saldo INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS palpites_campeao (id_discord TEXT PRIMARY KEY, selecao TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS palpites_artilheiro (id_discord TEXT PRIMARY KEY, jogador TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS apostas (id_discord TEXT, jogo TEXT, palpite TEXT, valor INTEGER, odd REAL)''')
    conn.commit()
    conn.close()

init_db()

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
                    odds_do_dia[chave_jogo] = {
                        "Vencedor_Casa": time_casa,
                        "Odd_Casa": odd_casa,
                        "Vencedor_Fora": time_fora,
                        "Odd_Fora": odd_fora,
                        "Horario": horario_formatado,
                        "Horario_DT": horario_brasil,
                    }
        return odds_do_dia, "Sucesso"
    except Exception as e:
        return None, str(e)


class ApostaModal(discord.ui.Modal, title="Sua Aposta"):
    valor = discord.ui.TextInput(
        label="Quantos Pilas quer apostar?",
        style=discord.TextStyle.short,
        placeholder="Ex: 500",
        required=True
    )

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
            await interaction.response.send_message("❌ Digite um número inteiro válido e maior que zero!", ephemeral=True)
            return

        id_usuario = str(interaction.user.id)
        conn = sqlite3.connect('bolao.db')
        c = conn.cursor()
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
        resultado = c.fetchone()

        if not resultado:
            await interaction.response.send_message("❌ Você não tem conta! Use `!registrar`.", ephemeral=True)
            conn.close()
            return

        saldo_atual = int(resultado[0])
        if valor_int > saldo_atual:
            await interaction.response.send_message(f"💸 Saldo insuficiente! Você só tem {saldo_atual} Pilas.", ephemeral=True)
            conn.close()
            return

        novo_saldo = saldo_atual - valor_int
        c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_usuario))
        c.execute("INSERT INTO apostas (id_discord, jogo, palpite, valor, odd) VALUES (?, ?, ?, ?, ?)",
                  (id_usuario, self.jogo, self.palpite, valor_int, self.odd))
        conn.commit()
        conn.close()

        await interaction.response.send_message(f"✅ **Aposta Registrada!**\nVocê investiu **{valor_int} Pilas** no **{self.palpite}** (Odd: {self.odd}).\nSaldo restante: {novo_saldo} Pilas.")

class BotoesTimes(discord.ui.View):
    def __init__(self, jogo, info):
        super().__init__(timeout=120)
        self.jogo = jogo
        self.info = info

        btn_casa = discord.ui.Button(label=f"{info['Vencedor_Casa']} (Odd: {info['Odd_Casa']})", style=discord.ButtonStyle.primary)
        btn_casa.callback = self.apostar_casa
        self.add_item(btn_casa)

        btn_fora = discord.ui.Button(label=f"{info['Vencedor_Fora']} (Odd: {info['Odd_Fora']})", style=discord.ButtonStyle.danger)
        btn_fora.callback = self.apostar_fora
        self.add_item(btn_fora)

    async def apostar_casa(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ApostaModal(self.jogo, self.info['Vencedor_Casa'], self.info['Odd_Casa']))

    async def apostar_fora(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ApostaModal(self.jogo, self.info['Vencedor_Fora'], self.info['Odd_Fora']))

class JogoSelect(discord.ui.Select):
    def __init__(self, odds):
        options = []
        for jogo, info in list(odds.items())[:25]:
            options.append(discord.SelectOption(
                label=jogo,
                description=f"⏰ {info['Horario']} | {info['Vencedor_Casa']} x {info['Vencedor_Fora']}",
                value=jogo
            ))
        super().__init__(placeholder="Escolha o jogo que deseja apostar...", min_values=1, max_values=1, options=options)
        self.odds = odds

    async def callback(self, interaction: discord.Interaction):
        jogo = self.values[0]
        info = self.odds[jogo]

        agora_brasil = datetime.utcnow() - timedelta(hours=3)
        horario_limite = info["Horario_DT"] - timedelta(minutes=10)

        if agora_brasil > horario_limite:
            await interaction.response.send_message(f"🚨 As apostas para **{jogo}** já estão encerradas!", ephemeral=True)
            return

        view = BotoesTimes(jogo, info)
        await interaction.response.send_message(f"⚽ Você escolheu: **{jogo}**\nQuem vai vencer a partida?", view=view, ephemeral=True)

class JogoView(discord.ui.View):
    def __init__(self, odds):
        super().__init__(timeout=120)
        self.add_item(JogoSelect(odds))


@bot.command()
async def apostar(ctx):
    odds, status = buscar_odds_do_dia()
    if not odds:
        await ctx.send("❌ Não há jogos com odds abertas no momento.")
        return
    view = JogoView(odds)
    await ctx.send("👇 **Abra o menu abaixo e selecione a partida:**", view=view)

@bot.command()
async def jogos(ctx):
    odds, status = buscar_odds_do_dia()
    if not odds:
        await ctx.send("⚽ **Sem jogos hoje!**")
        return

    embed = discord.Embed(title="⚽ Jogos de Hoje", description="Digite `!apostar` para fazer sua fezinha!", color=discord.Color.green())
    for jogo, info in list(odds.items())[:15]:
        texto = f"**{info['Vencedor_Casa']}** ({info['Odd_Casa']}) ou **{info['Vencedor_Fora']}** ({info['Odd_Fora']})\n⏰ {info['Horario']}"
        embed.add_field(name=jogo, value=texto, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def registrar(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_usuario = str(ctx.author.id)

    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
    if c.fetchone():
        await ctx.send(f"⚠️ {ctx.author.mention}, você já é um Pilantra!")
    else:
        c.execute("INSERT INTO usuarios (id_discord, saldo) VALUES (?, ?)", (id_usuario, 1000))
        await ctx.send(f"🎉 Bem-vindo ao vício, {ctx.author.mention}! Você recebeu **1000 Pilas** para começar. LET'S GO GAMBLING")
        await ctx.send("https://media.tenor.com/i-gbL-IgbbYAAAAj/dodep2.gif")
    conn.commit()
    conn.close()

@bot.command()
async def saldo(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_usuario = str(ctx.author.id)
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
    resultado = c.fetchone()
    if resultado:
        await ctx.send(f"💰 {ctx.author.mention}, seu saldo atual é de **{int(resultado[0])} Pilas**.")
    else:
        await ctx.send(f"⚠️ {ctx.author.mention}, você ainda não tem conta! Digite `!registrar` para começar.")
    conn.close()

@bot.command()
async def palpites(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_usuario = str(ctx.author.id)

    c.execute("SELECT selecao FROM palpites_campeao WHERE id_discord = ?", (id_usuario,))
    campeao_resultado = c.fetchone()
    c.execute("SELECT jogador FROM palpites_artilheiro WHERE id_discord = ?", (id_usuario,))
    artilheiro_resultado = c.fetchone()
    c.execute("SELECT jogo, palpite, valor, odd FROM apostas WHERE id_discord = ?", (id_usuario,))
    apostas_resultados = c.fetchall()
    conn.close()

    embed = discord.Embed(title=f"🧾 Bilhete de Apostas | {ctx.author.display_name}", color=discord.Color.gold())
    
    val_camp = f"**{campeao_resultado[0]}**" if campeao_resultado else "Vazio"
    val_art = f"**{artilheiro_resultado[0]}**" if artilheiro_resultado else "Vazio"
    
    embed.add_field(name="🏆 Campeão", value=val_camp, inline=True)
    embed.add_field(name="👟 Artilheiro", value=val_art, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)

    if apostas_resultados:
        texto_jogos = ""
        for aposta in apostas_resultados:
            texto_jogos += f"⚽ **{aposta[0]}**\n↳ Palpite: **{aposta[1]}** | 💸 {int(aposta[2])} Pilas (Odd: {aposta[3]})\n\n"
        embed.add_field(name="📅 Jogos do Dia", value=texto_jogos, inline=False)
    else:
        embed.add_field(name="📅 Jogos do Dia", value="Nenhuma aposta ativa hoje.", inline=False)

    await ctx.send(embed=embed)

async def processar_resultado(ctx, jogo: str, vencedor: str):
    """Função utilitária para pagar as apostas (usada pelo resultado e simular)"""
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT id_discord, palpite, valor, odd FROM apostas WHERE jogo = ?", (jogo,))
    apostas = c.fetchall()

    if not apostas:
        await ctx.send("🤷‍♂️ Ninguém apostou nesse jogo.")
        conn.close()
        return

    for aposta in apostas:
        id_discord, palpite, valor, odd = aposta
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_discord,))
        saldo_atual = int(c.fetchone()[0])

        if palpite == vencedor:
            lucro = int(valor * odd)
            novo_saldo = saldo_atual + lucro
            c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_discord))
            
            if odd >= 3.50:
                await ctx.send(f"🦓 **VAI TOMANDO!** <@{id_discord}> faturou absurdos {lucro} Pilas numa zebra!")
            else:
                await ctx.send(f"✅ <@{id_discord}> ganhou a aposta e recebeu {lucro} Pilas!")
        else:
            if saldo_atual < 10:
                await ctx.send(f"📉 **DEU RED!** O loss veio pesado pra <@{id_discord}>, hora de vender o celta.")

    c.execute("DELETE FROM apostas WHERE jogo = ?", (jogo,))
    conn.commit()
    conn.close()

@bot.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! Latência: {round(bot.latency * 1000)}ms")

@bot.command()
async def comandos(ctx):
    embed = discord.Embed(title="📜 Comandos do Pilantra BOT", color=discord.Color.blue())
    embed.add_field(name="!registrar", value="Cria sua conta e recebe 1000 Pilas para começar.", inline=False)
    embed.add_field(name="!saldo", value="Mostra seu saldo atual de Pilas.", inline=False)
    embed.add_field(name="!jogos", value="Lista os jogos do dia com odds.", inline=False)
    embed.add_field(name="!apostar", value="Abre o menu interativo para apostar nos jogos do dia.", inline=False)
    embed.add_field(name="!palpites", value="Mostra seus palpites e apostas registradas.", inline=False)
    embed.add_field(name="Administração", value="!resultado, !simular, !addsaldo, !remsaldo, !remaposta", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_role("Pilantra BOT")
async def resultado(ctx, jogo: str, vencedor: str):
    await ctx.send(f"⚽ **FIM DE PAPO!** O **{vencedor}** venceu a partida **{jogo}**! Calculando pagamentos...")
    await processar_resultado(ctx, jogo, vencedor)

@bot.command()
@commands.has_role("Pilantra BOT")
async def simular(ctx, time_casa: str, odd_casa: float, time_fora: str, odd_fora: float):
    jogo = f"{time_casa} x {time_fora}"
    
    prob_casa = 1 / odd_casa
    prob_fora = 1 / odd_fora
    total = prob_casa + prob_fora
    
    chance_casa = (prob_casa / total) * 100
    chance_fora = (prob_fora / total) * 100
    
    vencedor = random.choices(
        population=[time_casa, time_fora], 
        weights=[chance_casa, chance_fora], 
        k=1
    )[0]
    
    await ctx.send(f"🎲 **SIMULAÇÃO REALISTA INICIADA!**\n"
                   f"⚖️ **Chances de Vitória:** {time_casa} (**{chance_casa:.1f}%**) x {time_fora} (**{chance_fora:.1f}%**)\n"
                   f"🏆 O sistema girou a roleta matemática e cravou: **{vencedor}**!")
                   
    await processar_resultado(ctx, jogo, vencedor)

@bot.command()
@commands.has_role("Pilantra BOT")
async def addsaldo(ctx, membro: discord.Member, valor: int):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (str(membro.id),))
    res = c.fetchone()
    if res:
        novo_saldo = int(res[0]) + valor
        c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, str(membro.id)))
        await ctx.send(f"🏦 **Administração:** {valor} Pilas injetados na conta de {membro.mention}. Novo saldo: {novo_saldo}")
    else:
        await ctx.send("❌ Esse usuário não está registrado no bot.")
    conn.commit()
    conn.close()

@bot.command()
@commands.has_role("Pilantra BOT")
async def remsaldo(ctx, membro: discord.Member, valor: int):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (str(membro.id),))
    res = c.fetchone()
    if res:
        novo_saldo = int(res[0]) - valor
        c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, str(membro.id)))
        await ctx.send(f"🏦 **Administração:** {valor} Pilas removidos da conta de {membro.mention}. Novo saldo: {novo_saldo}")
    conn.commit()
    conn.close()

@bot.command()
@commands.has_role("Pilantra BOT")
async def remaposta(ctx, membro: discord.Member, *, jogo: str):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("DELETE FROM apostas WHERE id_discord = ? AND jogo = ?", (str(membro.id), jogo))
    if c.rowcount > 0:
        await ctx.send(f"🗑️ Aposta de {membro.mention} no jogo **{jogo}** foi cancelada. (Atenção: o saldo não foi devolvido automaticamente, use `!addsaldo` se necessário).")
    else:
        await ctx.send("❌ Nenhuma aposta encontrada com esses dados.")
    conn.commit()
    conn.close()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send("⛔ Só o mais pilantra pode usar este comando!")

@bot.event
async def on_ready():
    print(f'🔥 Pilantra online como {bot.user}')

keep_alive()
token = os.environ.get('DISCORD_TOKEN')
if token:
    bot.run(token)