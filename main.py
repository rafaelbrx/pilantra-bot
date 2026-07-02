import discord
from discord.ext import commands
import os
import sqlite3
import requests
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
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios (id_discord TEXT PRIMARY KEY, saldo REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS palpites_campeao (id_discord TEXT PRIMARY KEY, selecao TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS palpites_artilheiro (id_discord TEXT PRIMARY KEY, jogador TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS apostas (id_discord TEXT, jogo TEXT, palpite TEXT, valor REAL, odd REAL)''')
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
            erro_api = resposta.json().get("message", "Erro desconhecido")
            return None, f"⚠️ A API recusou o acesso. Código: {resposta.status_code} | Motivo: {erro_api}"

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

                    odd_casa = 0
                    odd_fora = 0

                    for resultado in resultados:
                        if resultado["name"] == time_casa:
                            odd_casa = resultado["price"]
                        elif resultado["name"] == time_fora:
                            odd_fora = resultado["price"]

                    chave_jogo = f"{time_casa}x{time_fora}"

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
        return None, f"⚠️ O código falhou ao ler os dados: {e}"


@bot.command()
async def jogoshoje(ctx):
    odds, status_ou_erro = buscar_odds_do_dia()

    if odds is None:
        await ctx.send("❌ **Ih, deu ruim!** Não consegui puxar os jogos. O dono do bot deve ter esquecido de pagar a conta da API.")
        print(f"[ERRO NO !jogoshoje] {status_ou_erro}", flush=True)
        return

    if not odds:
        await ctx.send("⚽ **Sem jogos!** A The Odds API não tem nenhum jogo da Copa do Mundo com odds abertas no momento.")
        return

    embed = discord.Embed(title="⚽ Jogos e Odds de Hoje", color=discord.Color.green())
    for jogo, info in odds.items():
        texto = f"**{info['Vencedor_Casa']}** (Odd: {info['Odd_Casa']}) ou **{info['Vencedor_Fora']}** (Odd: {info['Odd_Fora']})\n⏰ Horário: {info['Horario']}"
        embed.add_field(name=jogo, value=texto, inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong!")

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
        await ctx.send("https://media.tenor.com/i-gbL-IgbbYAAAAj/dodep2.gif")

    conn.commit()
    conn.close()

@bot.command()
async def apostar(ctx, jogo: str, palpite: str, valor: float):
    odds, status_ou_erro = buscar_odds_do_dia()

    if odds is None:
        await ctx.send("❌ **Ih, deu ruim!** O sistema de apostas está fora do ar momentaneamente.")
        print(f"[ERRO NO !apostar] {status_ou_erro}", flush=True)
        return

    if jogo not in odds:
        await ctx.send("❌ Jogo não encontrado. Use `!jogoshoje` para ver as opções disponíveis.")
        return

    horario_jogo = odds[jogo]["Horario_DT"]
    horario_limite = horario_jogo - timedelta(minutes=10)

    if datetime.now() > horario_limite:
        await ctx.send(f"🚨 {ctx.author.mention}, as apostas para este jogo já estão encerradas!")
        return

    vencedor_casa = odds[jogo]["Vencedor_Casa"]
    vencedor_fora = odds[jogo]["Vencedor_Fora"]

    if palpite == vencedor_casa:
        odd_valida = odds[jogo]["Odd_Casa"]
    elif palpite == vencedor_fora:
        odd_valida = odds[jogo]["Odd_Fora"]
    else:
        await ctx.send(
            f"❌ Palpite inválido! As opções para esse jogo são **{vencedor_casa}** ou **{vencedor_fora}**."
        )
        return

    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_usuario = str(ctx.author.id)

    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
    resultado = c.fetchone()

    if not resultado:
        await ctx.send("❌ Você não tem conta! Digite `!registrar` primeiro.")
        conn.close()
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
async def campeao(ctx, *, selecao: str):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_usuario = str(ctx.author.id)

    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
    resultado = c.fetchone()
    
    if not resultado:
        await ctx.send("❌ Você não tem conta! Digite `!registrar` primeiro.")
        conn.close()
        return
        
    saldo_atual = resultado[0]

    c.execute("SELECT selecao FROM palpites_campeao WHERE id_discord = ?", (id_usuario,))
    aposta_existente = c.fetchone()

    if aposta_existente:
        taxa = 50.0
        if saldo_atual < taxa:
            await ctx.send(f"💸 Você está quebrado demais pra mudar de ideia! Trocar o palpite custa {taxa} Pilas, e você só tem {saldo_atual}.")
        else:
            palpite_antigo = aposta_existente[0]
            novo_saldo = saldo_atual - taxa
            
            c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_usuario))
            c.execute("UPDATE palpites_campeao SET selecao = ? WHERE id_discord = ?", (selecao, id_usuario))
            await ctx.send(f"🔄 {ctx.author.mention} pagou a taxa de {taxa} Pilas e trocou o palpite de campeão de **{palpite_antigo}** para **{selecao}**!\nSaldo restante: {novo_saldo} Pilas.")
    else:
        c.execute("INSERT INTO palpites_campeao (id_discord, selecao) VALUES (?, ?)", (id_usuario, selecao))
        await ctx.send(f"🏆 {ctx.author.mention} cravou que **{selecao}** será a campeã da Copa!")

    conn.commit()
    conn.close()

@bot.command()
async def artilheiro(ctx, *, jogador: str):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_usuario = str(ctx.author.id)

    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
    resultado = c.fetchone()
    
    if not resultado:
        await ctx.send("❌ Você não tem conta! Digite `!registrar` primeiro.")
        conn.close()
        return
        
    saldo_atual = resultado[0]

    c.execute("SELECT jogador FROM palpites_artilheiro WHERE id_discord = ?", (id_usuario,))
    aposta_existente = c.fetchone()

    if aposta_existente:
        taxa = 50.0
        if saldo_atual < taxa:
            await ctx.send(f"💸 Cadê o dinheiro? Trocar o palpite de artilheiro custa {taxa} Pilas, e você tem apenas {saldo_atual}.")
        else:
            palpite_antigo = aposta_existente[0]
            novo_saldo = saldo_atual - taxa
            
            c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_usuario))
            c.execute("UPDATE palpites_artilheiro SET jogador = ? WHERE id_discord = ?", (jogador, id_usuario))
            await ctx.send(f"🔄 {ctx.author.mention} pagou a taxa de {taxa} Pilas e trocou o palpite de artilheiro de **{palpite_antigo}** para **{jogador}**!\nSaldo restante: {novo_saldo} Pilas.")
    else:
        c.execute("INSERT INTO palpites_artilheiro (id_discord, jogador) VALUES (?, ?)", (id_usuario, jogador))
        await ctx.send(f"👟 {ctx.author.mention} cravou que **{jogador}** será o artilheiro da Copa!")

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
        conn.close()
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
                await ctx.send("https://media1.tenor.com/m/IoIaVLN2efsAAAAd/money-make-it-rain.gif")
            else:
                await ctx.send(f"✅ <@{id_discord}> ganhou a aposta e recebeu {lucro} Pilas!")

        else:
            if saldo_atual < 10:
                await ctx.send(f"📉 **DEU RED!** O loss veio pesado pra <@{id_discord}>, hora de vender o celta.")
                await ctx.send("https://media1.tenor.com/m/aSkdq3IU0g0AAAAd/laughing-cat.gif")

    c.execute("DELETE FROM apostas WHERE jogo = ?", (jogo,))
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
        saldo_atual = resultado[0]
        await ctx.send(f"💰 {ctx.author.mention}, seu saldo atual é de **{saldo_atual} Pilas**.")
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

    embed = discord.Embed(
        title=f"🧾 Bilhete de Apostas | {ctx.author.display_name}",
        color=discord.Color.gold()
    )

    if campeao_resultado:
        embed.add_field(name="🏆 Campeão da Copa", value=f"**{campeao_resultado[0]}**", inline=True)
    else:
        embed.add_field(name="🏆 Campeão da Copa", value="Vazio. Use `!campeao`", inline=True)

    if artilheiro_resultado:
        embed.add_field(name="👟 Artilheiro", value=f"**{artilheiro_resultado[0]}**", inline=True)
    else:
        embed.add_field(name="👟 Artilheiro", value="Vazio. Use `!artilheiro`", inline=True)

    embed.add_field(name="\u200b", value="\u200b", inline=False)

    if apostas_resultados:
        texto_jogos = ""
        for aposta in apostas_resultados:
            jogo, palpite, valor, odd = aposta
            texto_jogos += f"⚽ **{jogo}**\n↳ Palpite: **{palpite}** | 💸 {valor} Pilas (Odd: {odd})\n\n"

        embed.add_field(name="📅 Jogos do Dia", value=texto_jogos, inline=False)
    else:
        embed.add_field(name="📅 Jogos do Dia", value="Você não apostou em nenhum jogo hoje. Use `!apostar`", inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="📜 Comandos do Pilantra Bot",
        description="Aqui estão os comandos disponíveis:",
        color=discord.Color.blue()
    )

    embed.add_field(name="!ping", value="Verifica se o bot está online.", inline=False)
    embed.add_field(name="!registrar", value="Cria uma conta e recebe 1000 Pilas para apostar.", inline=False)
    embed.add_field(name="!jogoshoje", value="Mostra os jogos de hoje com suas odds.", inline=False)
    embed.add_field(name="!apostar <jogo> <palpite> <valor>", value="Faz uma aposta em um jogo específico.", inline=False)
    embed.add_field(name="!campeao <selecao>", value="Diz quem você acha que será o campeão da Copa.", inline=False)
    embed.add_field(name="!artilheiro <jogador>", value="Diz quem você acha que será o artilheiro da Copa.", inline=False)
    embed.add_field(name="!saldo", value="Mostra seu saldo atual de Pilas.", inline=False)
    embed.add_field(name="!palpites", value="Mostra todos os seus palpites e apostas atuais.", inline=False)

    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ **Faltou informação aí, {ctx.author.name}!**\nJeito certo de usar: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`")
    
    elif isinstance(error, commands.BadArgument):
        await ctx.send("⚠️ **Você digitou algo errado!** Verifique se não colocou letras onde deveriam ser números.")
    
    elif isinstance(error, commands.CommandNotFound):
        await ctx.send(f"❌ **Comando não encontrado, {ctx.author.name}!** Use `!help` para ver a lista de comandos disponíveis.")
        pass 
    
    else:
        print(f"Erro interno não tratado: {error}")

@bot.event
async def on_ready():
    print(f'🔥 Pilantra online como {bot.user}')

keep_alive()

token = os.environ.get('DISCORD_TOKEN')
if token:
    bot.run(token)
else:
    print("Erro: Token do Discord não encontrado nas variáveis de ambiente!")