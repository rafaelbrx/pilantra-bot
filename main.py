import discord
from discord.ext import commands
import os
import sqlite3
import requests
import random
import asyncio
from datetime import datetime, timedelta
from keep_alive import keep_alive
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

jogos_simulados = {}

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

def obter_todas_odds():
    odds, _ = buscar_odds_do_dia()
    if odds is None:
        odds = {}
    
    odds.update(jogos_simulados)
    return odds

async def aguardar_e_simular(channel, jogo_id, tempo_minutos, info):
    await asyncio.sleep(tempo_minutos * 60)
    
    if jogo_id in jogos_simulados:
        del jogos_simulados[jogo_id]
        
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

class CampeaoModal(discord.ui.Modal, title="Palpite de Campeão"):
    selecao = discord.ui.TextInput(label="Qual seleção será a campeã?", placeholder="Ex: Brasil", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect('bolao.db')
        c = conn.cursor()
        id_usuario = str(interaction.user.id)

        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
        resultado = c.fetchone()
        
        if not resultado:
            await interaction.response.send_message("❌ Você não tem conta! Digite `!registrar`.", ephemeral=True)
            conn.close()
            return
            
        saldo_atual = int(resultado[0])
        c.execute("SELECT selecao FROM palpites_campeao WHERE id_discord = ?", (id_usuario,))
        aposta_existente = c.fetchone()

        if aposta_existente:
            taxa = 200
            if saldo_atual < taxa:
                await interaction.response.send_message(f"💸 Cadê o dinheiro? Trocar o palpite custa {taxa} Pilas, e você só tem {saldo_atual}.", ephemeral=True)
            else:
                palpite_antigo = aposta_existente[0]
                novo_saldo = saldo_atual - taxa
                c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_usuario))
                c.execute("UPDATE palpites_campeao SET selecao = ? WHERE id_discord = ?", (self.selecao.value, id_usuario))
                await interaction.response.send_message(f"🔄 {interaction.user.mention} pagou {taxa} Pilas e trocou o palpite de campeão de **{palpite_antigo}** para **{self.selecao.value}**!\nSaldo: {novo_saldo} Pilas.")
        else:
            c.execute("INSERT INTO palpites_campeao (id_discord, selecao) VALUES (?, ?)", (id_usuario, self.selecao.value))
            await interaction.response.send_message(f"🏆 {interaction.user.mention} cravou que **{self.selecao.value}** será a campeã da Copa!")

        conn.commit()
        conn.close()

class ArtilheiroModal(discord.ui.Modal, title="Palpite de Artilheiro"):
    jogador = discord.ui.TextInput(label="Quem será o artilheiro?", placeholder="Ex: Neymar", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect('bolao.db')
        c = conn.cursor()
        id_usuario = str(interaction.user.id)

        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
        resultado = c.fetchone()
        
        if not resultado:
            await interaction.response.send_message("❌ Você não tem conta! Digite `!registrar`.", ephemeral=True)
            conn.close()
            return
            
        saldo_atual = int(resultado[0])
        c.execute("SELECT jogador FROM palpites_artilheiro WHERE id_discord = ?", (id_usuario,))
        aposta_existente = c.fetchone()

        if aposta_existente:
            taxa = 200
            if saldo_atual < taxa:
                await interaction.response.send_message(f"💸 Tá quebrado! Trocar o artilheiro custa {taxa} Pilas, e você tem apenas {saldo_atual}.", ephemeral=True)
            else:
                palpite_antigo = aposta_existente[0]
                novo_saldo = saldo_atual - taxa
                c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_usuario))
                c.execute("UPDATE palpites_artilheiro SET jogador = ? WHERE id_discord = ?", (self.jogador.value, id_usuario))
                await interaction.response.send_message(f"🔄 {interaction.user.mention} pagou {taxa} Pilas e trocou o palpite de artilheiro de **{palpite_antigo}** para **{self.jogador.value}**!\nSaldo: {novo_saldo} Pilas.")
        else:
            c.execute("INSERT INTO palpites_artilheiro (id_discord, jogador) VALUES (?, ?)", (id_usuario, self.jogador.value))
            await interaction.response.send_message(f"👟 {interaction.user.mention} cravou que **{self.jogador.value}** será o artilheiro da Copa!")

        conn.commit()
        conn.close()

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
            return await interaction.response.send_message("❌ Digite um valor numérico inteiro maior que zero!", ephemeral=True)

        conn = sqlite3.connect('bolao.db')
        c = conn.cursor()
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (str(interaction.user.id),))
        remetente = c.fetchone()
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (str(self.destinatario.id),))
        destinatario_db = c.fetchone()

        if not remetente:
            await interaction.response.send_message("❌ Você não tem conta. Use `!registrar`.", ephemeral=True)
        elif not destinatario_db:
            await interaction.response.send_message(f"❌ O alvo ainda não tem conta no bot.", ephemeral=True)
        elif int(remetente[0]) < valor_int:
            await interaction.response.send_message(f"💸 PIX Recusado! Você só tem {remetente[0]} Pilas.", ephemeral=True)
        else:
            novo_remetente = int(remetente[0]) - valor_int
            novo_destinatario = int(destinatario_db[0]) + valor_int
            c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_remetente, str(interaction.user.id)))
            c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_destinatario, str(self.destinatario.id)))
            await interaction.response.send_message(f"💸 **PIX REALIZADO!** {interaction.user.mention} transferiu **{valor_int} Pilas** para {self.destinatario.mention}!")
            
        conn.commit()
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
    def __init__(self, modal_class, label="Abrir Formulário"):
        super().__init__(timeout=60)
        self.modal_class = modal_class
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.danger)
        btn.callback = self.abrir_modal
        self.add_item(btn)

    async def abrir_modal(self, interaction: discord.Interaction):
        if not any(role.name == "Pilantra BOT" for role in interaction.user.roles):
            return await interaction.response.send_message(f"⛔ Tira a mãozinha daí <@{interaction.user.id}>! Só administradores podem usar este botão.", ephemeral=True)
        
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
            return await interaction.response.send_message("❌ Valores inválidos! Use ponto para decimais (ex: 1.50) e um número inteiro para o tempo.", ephemeral=True)

        jogo_id = f"{self.t_casa.value} x {self.t_fora.value}"
        
        agora_brasil = datetime.utcnow() - timedelta(hours=3)
        horario_fechamento = agora_brasil + timedelta(minutes=t_min)

        info = {
            "Vencedor_Casa": self.t_casa.value,
            "Odd_Casa": odd_c,
            "Vencedor_Fora": self.t_fora.value,
            "Odd_Fora": odd_f,
            "Horario": horario_fechamento.strftime("%d/%m às %H:%M (SIMULADO)"),
            "Horario_DT": agora_brasil + timedelta(minutes=t_min + 10)
        }
        
        jogos_simulados[jogo_id] = info
        
        await interaction.response.send_message(
            f"🎰 **NOVO EVENTO DE CASSINO CRIADO!**\n"
            f"⚽ Partida: **{jogo_id}**\n"
            f"📈 Odds: {self.t_casa.value} (**{odd_c}**) x {self.t_fora.value} (**{odd_f}**)\n"
            f"⏳ Vocês têm **{t_min} minutos** para apostar usando o comando `!apostar`!\n"
            f"*(O resultado sairá sozinho assim que o tempo acabar)*"
        )
        
        bot.loop.create_task(aguardar_e_simular(interaction.channel, jogo_id, t_min, info))

class ResultadoModal(discord.ui.Modal, title="Processar Resultado Oficial"):
    jogo = discord.ui.TextInput(label="Nome exato do Jogo", placeholder="Ex: Spain x Austria")
    vencedor = discord.ui.TextInput(label="Quem ganhou?", placeholder="Ex: Spain")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"⚽ **FIM DE PAPO!** O **{self.vencedor.value}** venceu a partida **{self.jogo.value}**! Calculando...")
        await processar_resultado_interno(interaction.channel, self.jogo.value, self.vencedor.value)


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
            return await interaction.response.send_message("❌ Digite um número inteiro maior que zero!", ephemeral=True)

        id_usuario = str(interaction.user.id)
        conn = sqlite3.connect('bolao.db')
        c = conn.cursor()
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_usuario,))
        res = c.fetchone()

        if not res:
            return await interaction.response.send_message("❌ Você não tem conta! Use `!registrar`.", ephemeral=True)
        saldo = int(res[0])
        if valor_int > saldo:
            return await interaction.response.send_message(f"💸 Saldo insuficiente! Você só tem {saldo} Pilas.", ephemeral=True)

        novo_saldo = saldo - valor_int
        c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo_saldo, id_usuario))
        c.execute("INSERT INTO apostas (id_discord, jogo, palpite, valor, odd) VALUES (?, ?, ?, ?, ?)", (id_usuario, self.jogo, self.palpite, valor_int, self.odd))
        conn.commit()
        conn.close()

        await interaction.response.send_message(f"✅ **Aposta Registrada!**\nVocê investiu **{valor_int} Pilas** no **{self.palpite}** (Odd: {self.odd}).\nSaldo restante: {novo_saldo} Pilas.")

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

    async def apostar_casa(self, interaction): await interaction.response.send_modal(ApostaModal(self.jogo, self.info['Vencedor_Casa'], self.info['Odd_Casa']))
    async def apostar_fora(self, interaction): await interaction.response.send_modal(ApostaModal(self.jogo, self.info['Vencedor_Fora'], self.info['Odd_Fora']))

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

async def processar_resultado_interno(channel, jogo: str, vencedor: str):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT id_discord, palpite, valor, odd FROM apostas WHERE jogo = ?", (jogo,))
    apostas = c.fetchall()

    if not apostas:
        await channel.send("🤷‍♂️ Ninguém apostou nesse jogo.")
        conn.close()
        return

    for aposta in apostas:
        id_discord, palpite, valor, odd = aposta
        c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (id_discord,))
        saldo = int(c.fetchone()[0])

        if palpite == vencedor:
            lucro = int(valor * odd)
            c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (saldo + lucro, id_discord))
            
            if odd >= 3.50: 
                await channel.send(f"🦓 **VAI TOMANDO!** A PLATAFORMA TA BUGADA! <@{id_discord}> faturou absurdos {lucro} Pilas numa zebra!")
                await channel.send("https://c.tenor.com/IoIaVLN2efsAAAAd/tenor.gif")
                await channel.send("")
            else: 
                await channel.send(f"✅ <@{id_discord}> ganhou a aposta e recebeu {lucro} Pilas!")
        else:
            if valor >= 500: 
                await channel.send(f"📉 **DEU RED!** O loss de {valor} Pilas veio pesado pra <@{id_discord}>, hora de vender o celta.")
                await channel.send("https://c.tenor.com/aSkdq3IU0g0AAAAd/tenor.gif")
                await channel.send("")
            else:
                await channel.send(f"❌ <@{id_discord}> apostou {valor} Pilas e se deu mal. Faz o PIX pra casa de apostas!")
                await channel.send("")

    c.execute("DELETE FROM apostas WHERE jogo = ?", (jogo,))
    conn.commit()
    conn.close()


@bot.command()
async def apostar(ctx):
    odds = obter_todas_odds()
    if not odds: return await ctx.send("❌ Não há jogos abertos no momento.")
    await ctx.send("👇 **Selecione a partida:**", view=JogoView(odds))

@bot.command()
async def campeao(ctx):
    await ctx.send("🏆 Clique para registrar seu Campeão:", view=SimplesButtonView(CampeaoModal, "Palpite Campeão"))

@bot.command()
async def artilheiro(ctx):
    await ctx.send("👟 Clique para registrar o Artilheiro:", view=SimplesButtonView(ArtilheiroModal, "Palpite Artilheiro"))

@bot.command()
async def pix(ctx):
    view = discord.ui.View()
    view.add_item(PixSelect())
    await ctx.send("💸 **Mercado Interno:** Selecione abaixo quem vai receber o PIX:", view=view)


@bot.command()
@commands.has_role("Pilantra BOT")
async def simular(ctx):
    await ctx.send("🎲 Clique para criar seu Evento de Cassino:", view=AdminButtonView(SimularModal, "Criar Evento"))

@bot.command()
@commands.has_role("Pilantra BOT")
async def resultado(ctx):
    await ctx.send("⚽ Clique para informar quem venceu:", view=AdminButtonView(ResultadoModal, "Informar Resultado"))

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
        await ctx.send(f"🎉 Bem-vindo ao vício, {ctx.author.mention}! Você recebeu **1000 Pilas**.")
    conn.commit()
    conn.close()

@bot.command()
async def saldo(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (str(ctx.author.id),))
    resultado = c.fetchone()
    if resultado: await ctx.send(f"💰 {ctx.author.mention}, seu saldo é **{int(resultado[0])} Pilas**.")
    else: await ctx.send(f"⚠️ {ctx.author.mention}, você não tem conta!")
    conn.close()

@bot.command()
async def jogos(ctx):
    odds = obter_todas_odds()
    if not odds: return await ctx.send("⚽ **Sem jogos hoje!**")
    embed = discord.Embed(title="⚽ Jogos de Hoje", color=discord.Color.green())
    for jogo, info in list(odds.items())[:15]:
        embed.add_field(name=jogo, value=f"**{info['Vencedor_Casa']}** ({info['Odd_Casa']}) ou **{info['Vencedor_Fora']}** ({info['Odd_Fora']})\n⏰ {info['Horario']}", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def palpites(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    id_us = str(ctx.author.id)
    c.execute("SELECT selecao FROM palpites_campeao WHERE id_discord = ?", (id_us,))
    camp = c.fetchone()
    c.execute("SELECT jogador FROM palpites_artilheiro WHERE id_discord = ?", (id_us,))
    art = c.fetchone()
    c.execute("SELECT jogo, palpite, valor, odd FROM apostas WHERE id_discord = ?", (id_us,))
    apostas = c.fetchall()
    conn.close()

    embed = discord.Embed(title=f"🧾 Bilhete de {ctx.author.display_name}", color=discord.Color.gold())
    embed.add_field(name="🏆 Campeão", value=f"**{camp[0]}**" if camp else "Vazio", inline=True)
    embed.add_field(name="👟 Artilheiro", value=f"**{art[0]}**" if art else "Vazio", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)

    if apostas:
        txt = "".join([f"⚽ **{a[0]}**\n↳ Palpite: **{a[1]}** | 💸 {int(a[2])} Pilas (Odd: {a[3]})\n\n" for a in apostas])
        embed.add_field(name="📅 Jogos do Dia", value=txt, inline=False)
    else: embed.add_field(name="📅 Jogos do Dia", value="Nenhuma aposta ativa hoje.", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.cooldown(1, 259200, commands.BucketType.user)
async def diaria(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (str(ctx.author.id),))
    res = c.fetchone()
    if not res:
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("❌ Você não tem conta! Use `!registrar`.")
    novo = int(res[0]) + 350
    c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo, str(ctx.author.id)))
    conn.commit()
    conn.close()
    await ctx.send(f"🎁 {ctx.author.mention} resgatou a diária! Novo saldo: {novo} Pilas.")

@bot.command()
@commands.cooldown(1, 86400, commands.BucketType.user)
async def mendigar(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT saldo FROM usuarios WHERE id_discord = ?", (str(ctx.author.id),))
    res = c.fetchone()
    if not res:
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("❌ Crie sua conta primeiro com `!registrar`.")
    
    saldo = int(res[0])
    if saldo >= 100:
        ctx.command.reset_cooldown(ctx)
        return await ctx.send(f"🛑 Você ainda tem {saldo} Pilas. Vá apostar!")
    
    novo = saldo + 100
    c.execute("UPDATE usuarios SET saldo = ? WHERE id_discord = ?", (novo, str(ctx.author.id)))
    conn.commit()
    conn.close()
    await ctx.send(f"🥺 O sistema teve pena. Você recebeu **100 Pilas**! Saldo: {novo}")


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
    embed.add_field(name="!pix", value="Transfere Pilas para outro usuário.", inline=False)
    embed.add_field(name="!mendigar", value="Solicita 100 Pilas de graça (só pode uma vez a cada 24h).", inline=False)
    embed.add_field(name="!ranking", value="Mostra o ranking dos usuários com mais Pilas.", inline=False)
    embed.add_field(name="Administração", value="!resultado, !simular, !addsaldo, !remsaldo, !remaposta, !apostasDoDia", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def ranking(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT id_discord, saldo FROM usuarios ORDER BY saldo DESC LIMIT 10")
    top_usuarios = c.fetchall()
    conn.close()

    if not top_usuarios:
        await ctx.send("📊 Nenhum usuário registrado ainda.")
        return

    embed = discord.Embed(title="🏆 Ranking dos Pilantras", color=discord.Color.gold())
    for i, (id_discord, saldo) in enumerate(top_usuarios, start=1):
        embed.add_field(name=f"{i}º Lugar", value=f"<@{id_discord}> — 💰 {saldo} Pilas", inline=False)

    await ctx.send(embed=embed)

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
    else:
        await ctx.send("❌ Esse usuário não está registrado no bot.")
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

@bot.command()
@commands.has_role("Pilantra BOT")
async def apostasDoDia(ctx):
    conn = sqlite3.connect('bolao.db')
    c = conn.cursor()
    c.execute("SELECT id_discord, jogo, palpite, valor, odd FROM apostas")
    apostas = c.fetchall()
    conn.close()

    if not apostas:
        await ctx.send("📅 Nenhuma aposta registrada hoje.")
        return

    embed = discord.Embed(title="📅 Apostas Ativas", color=discord.Color.purple())
    for aposta in apostas:
        id_discord, jogo, palpite, valor, odd = aposta
        embed.add_field(name=jogo, value=f"<@{id_discord}> apostou em **{palpite}** | 💸 {int(valor)} Pilas (Odd: {odd})", inline=False)
    
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send("⛔ Só o mais pilantra pode usar este comando!")
    elif isinstance(error, commands.CommandOnCooldown):
        h = int(error.retry_after // 3600)
        m = int((error.retry_after % 3600) // 60)
        await ctx.send(f"⏳ Calma aí! Volte daqui a **{h}h e {m}m**.")

@bot.event
async def on_ready():
    print(f'🔥 Pilantra online como {bot.user}')

keep_alive()
token = os.environ.get('DISCORD_TOKEN')
if token: bot.run(token)