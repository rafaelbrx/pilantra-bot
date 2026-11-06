import os
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError(
            "⚠️ A variável DATABASE_URL não foi encontrada. Configure-a no painel "
            "do Render com a connection string do Supabase (formato: "
            "postgresql://usuario:senha@host:porta/nome_do_banco)."
        )
    return psycopg2.connect(DATABASE_URL, sslmode='require')


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