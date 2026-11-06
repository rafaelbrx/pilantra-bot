

import os
import time
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')


def get_conn():
    """Abre uma nova conexão com o Postgres. Chame conn.close() quando terminar,
    igual já era feito com sqlite3."""
    if not DATABASE_URL:
        raise RuntimeError(
            "⚠️ A variável DATABASE_URL não foi encontrada. Configure-a no painel "
            "do Render com a connection string do Supabase/Neon (formato: "
            "postgresql://usuario:senha@host:porta/nome_do_banco)."
        )
    # FIX: sem connect_timeout, psycopg2.connect() pode travar INDEFINIDAMENTE
    # se a rede até o Postgres estiver instável (mesma classe de bug do
    # requests.get sem timeout). Como get_conn() é chamado de forma síncrona
    # em vários comandos (fora de asyncio.to_thread), travar aqui podia
    # congelar o event loop inteiro do bot, não só um comando.
    #
    # Banco de dados gratuito (Supabase/Neon) pode "hibernar" após um tempo
    # sem uso, e a primeira conexão depois disso pode demorar mais que o
    # normal para acordar -- por isso o timeout aqui é mais generoso (15s)
    # que o das chamadas de API externa, e o tempo gasto fica logado para
    # facilitar diagnóstico.
    inicio = time.monotonic()
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require', connect_timeout=15)
        duracao = time.monotonic() - inicio
        if duracao > 3:
            print(f"[db.get_conn] Conexão demorou {duracao:.1f}s (banco pode estar hibernando/acordando).")
        return conn
    except Exception as e:
        duracao = time.monotonic() - inicio
        print(f"[db.get_conn] Falha ao conectar após {duracao:.1f}s: {e}")
        raise


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS usuarios (
                    id_discord TEXT PRIMARY KEY,
                    saldo INTEGER
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS palpites_campeao (
                    id_discord TEXT PRIMARY KEY,
                    selecao TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS palpites_artilheiro (
                    id_discord TEXT PRIMARY KEY,
                    jogador TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS apostas (
                    id_discord TEXT,
                    jogo TEXT,
                    palpite TEXT,
                    valor INTEGER,
                    odd REAL
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS horarios_jogos (
                    jogo TEXT PRIMARY KEY,
                    horario_dt TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS jogos_simulados_db (
                    jogo TEXT PRIMARY KEY,
                    t_casa TEXT, odd_casa REAL,
                    t_fora TEXT, odd_fora REAL,
                    horario_resolucao TEXT,
                    channel_id BIGINT
                 )''')
    conn.commit()
    c.close()
    conn.close()