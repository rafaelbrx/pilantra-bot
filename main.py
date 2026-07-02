import discord
from discord.ext import commands
import os
import sqlite3
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from keep_alive import keep_alive

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

def init_db():
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    # Tabela de carteira da galera
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios (id_discord TEXT PRIMARY KEY, saldo REAL)''')
    # Tabela do campeão da copa
    c.execute('''CREATE TABLE IF NOT EXISTS palpites_campeao (id_discord TEXT PRIMARY KEY, selecao TEXT)''')
    # Tabela das apostas dos jogos do dia
    c.execute('''CREATE TABLE IF NOT EXISTS apostas (id_discord TEXT, jogo TEXT, palpite TEXT, valor REAL, odd REAL)''')
    conn.commit()
    conn.close()

init_db()

def buscar_odds_do_dia():
    odds = {
        "BRAxARG": {"Vencedor_Casa": "BRA", "Odd_Casa": 2.50, "Vencedor_Fora": "ARG", "Odd_Fora": 3.10, "Horario": "16:00"},
        "FRAxENG": {"Vencedor_Casa": "FRA", "Odd_Casa": 3.80, "Vencedor_Fora": "ENG", "Odd_Fora": 1.90, "Horario": "20:00"} 
    }
    return odds

@bot.command()
async def jogoshoje(ctx):
    odds = buscar_odds_do_dia()
    embed = discord.Embed(title="⚽ Jogos e Odds de Hoje", color=discord.Color.green())
    for jogo, info in odds.items():
        texto = f"**{info['Vencedor_Casa']}** (Odd: {info['Odd_Casa']}) ou **{info['Vencedor_Fora']}** (Odd: {info['Odd_Fora']})\n⏰ Horário: {info['Horario']}"
        embed.add_embed_field(name=jogo, value=texto, inline=False)
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
        c.execute("INSERT INTO usuarios (id_discord, saldo) VALUES (?, ?)", (id_usuario, 1000.0))
        await ctx.send(f"🎉 Bem-vindo ao vício, {ctx.author.mention}! Você recebeu **1000 Pilas** para começar. LET'S GO GAMBLING")
        await ctx.send("https://tenor.com/pt-BR/view/dodep2-gif-10081337658340044214")

    conn.commit()
    conn.close()

@bot.command()
async def apostar(ctx, jogo: str, palpite: str, valor: float):
    horario_jogo_str = "2026-07-02 16:00:00" 
    horario_jogo = datetime.strptime(horario_jogo_str, "%Y-%m-%d %H:%M:%S")
    horario_limite = horario_jogo - timedelta(minutes=10)

    if datetime.now() > horario_limite:
        await ctx.send(f"🚨 {ctx.author.mention}, as apostas para este jogo já estão encerradas!")
        return

    odds = buscar_odds_do_dia()
    if jogo not in odds:
        await ctx.send("❌ Jogo não encontrado. Use `!jogoshoje` para ver as opções.")
        return
        
    odd_valida = odds[jogo]["Odd_Casa"] if palpite == odds[jogo]["Vencedor_Casa"] else odds[jogo]["Odd_Fora"]

    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_usuario = str(ctx.author.id)

    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
    resultado = c.fetchone()

    if not resultado:
        await ctx.send("❌ Você não tem conta! Digite `!registrar` primeiro.")
        return
    
    saldo_atual = resultado[0]

    if valor > saldo_atual:
        await ctx.send(f"💸 Tá achando que é o Neymar? Você só tem {saldo_atual} Pilas. Diminui essa aposta aí.")
    else:
        novo_saldo = saldo_atual - valor
        c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_usuario))
        c.execute("INSERT INTO apostas (id_discord, jogo, palpite, valor, odd) VALUES (?, ?, ?, ?, ?)", 
                  (id_usuario, jogo, palpite, valor, odd_valida))
        
        await ctx.send(f"✅ Aposta registrada! {ctx.author.mention} apostou **{valor} Pilas** no **{palpite}** (Odd: {odd_valida}).\nSaldo restante: {novo_saldo} Pilas.")
    
    conn.commit()
    conn.close()

@bot.command()
async def campeao(ctx, selecao: str):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_usuario = str(ctx.author.id)
    
    c.execute("SELECT selecao FROM palpites_campeao WHERE id_discord = ?", (id_usuario,))
    aposta_existente = c.fetchone()

    if aposta_existente:
        palpite_antigo = aposta_existente[0]
        taxa = 50.0
        c.execute("UPDATE palpites_campeao SET selecao = ? WHERE id_discord = ?", (selecao, id_usuario))
        await ctx.send(f"🔄 {ctx.author.mention} pagou a taxa de {taxa} Pilas e trocou o palpite de campeão de **{palpite_antigo}** para **{selecao}**!")
    else:
        c.execute("INSERT INTO palpites_campeao (id_discord, selecao) VALUES (?, ?)", (id_usuario, selecao))
        await ctx.send(f"🏆 {ctx.author.mention} cravou que **{selecao}** será a campeã da Copa!")
    
    conn.commit()
    conn.close()


@bot.command()
@commands.has_permissions(administrator=True)
async def resultado(ctx, jogo: str, vencedor: str):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    
    c.execute("SELECT id_discord, palpite, valor, odd FROM apostas WHERE jogo = ?", (jogo,))
    apostas = c.fetchall()
    
    if not apostas:
        await ctx.send("🤷‍♂️ Ninguém apostou nesse jogo.")
        return

    await ctx.send(f"⚽ **FIM DE PAPO!** O {vencedor} venceu o jogo {jogo}! Calculando as apostas...")

    for aposta in apostas:
        id_discord, palpite, valor, odd = aposta
        
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_discord,))
        saldo_atual = c.fetchone()[0]

        if palpite == vencedor:
            lucro = valor * odd
            novo_saldo = saldo_atual + lucro
            c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_discord))
            
            if odd >= 3.50:
                await ctx.send(f"🦓 **VAI TOMANDO!** A PLATAFORMA TA BUGADA! <@{id_discord}> faturou absurdos {lucro} Pilas!")
                await ctx.send("https://tenor.com/pt-BR/view/money-make-it-rain-rain-guap-dollar-gif-2486578895352396283")
            else:
                await ctx.send(f"✅ <@{id_discord}> ganhou a aposta e recebeu {lucro} Pilas!")
                
        else:
            if saldo_atual < 10:
                await ctx.send(f"📉 **DEU RED!** O loss veio pesado pra <@{id_discord}>, hora de vender o celta.")
                await ctx.send("https://tenor.com/pt-BR/view/laughing-cat-catlaughing-laughingcat-point-gif-7577620470218150413")

    c.execute("DELETE FROM apostas WHERE jogo = ?", (jogo,))
    conn.commit()
    conn.close()


@bot.event
async def on_ready():
    print(f'🔥 Pilantra online como {bot.user}')

keep_alive()

token = os.environ.get('DISCORD_TOKEN')
if token:
    bot.run(token)
else:
    print("Erro: Token do Discord não encontrado nas variáveis de ambiente!")