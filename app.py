# ======================
# IMPORTAÇÕES
# ======================
import sqlite3
from datetime import datetime
from flask import Flask, render_template, redirect, abort, session, request, jsonify, flash, url_for, Response, stream_with_context
from flask_cors import CORS
import json
import os
import queue
from functools import wraps 
from flask_cors import cross_origin
from werkzeug.utils import secure_filename
import uuid
import subprocess
from concurrent.futures import ThreadPoolExecutor
from waitress import serve
import threading
import time
import requests

import sys
sys.stdout.reconfigure(encoding='utf-8')

# ======================
# CONFIGURAÇÕES INICIAIS
# ======================
app = Flask(__name__)
app.secret_key = 'supersecretkey'
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# ======================
# VARIÁVEIS GLOBAIS
# ======================
contador_ouvintes = []  # Usado para contador SSE (painel principal)
subscribers = []        # Usado para eventos de atividades (ex: acionamentos)
streams_usuarios = {}   # Usado para mensagens em tempo real (EdyTalk)
conexoes_ativas = {}
eventos_refresh = queue.Queue()

STATUS = {
    "PENDENTE": "pendente",
    "EM_ANDAMENTO": "em_andamento",
    "RESOLVIDO": "resolvido",
    "AGENDAR_MIGRACAO": "agendar_migracao",
    "AGENDAR_SEM_VOLTAGEM": "agendar_sem_voltagem",
    "AGENDAR_AJUSTE_DE_DROP": "agendar_ajuste_de_drop"
}

# ======================
# FUNÇÕES AUXILIARES
# ======================
def tem_acesso(funcao, permitido):
    return funcao in permitido or funcao in ['dev', 'supervisor']

def atualiza_contador_periodicamente():
    global ultimo_contador
    while True:
        try:
            conn = sqlite3.connect('acionamentos.db', timeout=3)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM caixas WHERE status = 'pendente'")
            resultado = cursor.fetchone()
            conn.close()
            ultimo_contador = resultado[0] if resultado else 0
        except Exception:
            ultimo_contador = 0
        time.sleep(5)  # atualiza a cada 5 segundos
        
def carregar_usuarios():
    try:
        conn = sqlite3.connect('acionamentos.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, nome, senha, setor, funcao, foto, bio, status FROM usuarios')
        rows = cursor.fetchall()
        conn.close()

        usuarios = []
        for row in rows:
            usuarios.append({
                'id': row[0],
                'nome': row[1],
                'senha': row[2],
                'setor': row[3],
                'funcao': row[4],
                'foto': row[5],
                'bio': row[6],
                'status': row[7]
            })
        return usuarios

    except Exception as e:
        print(f"[ERRO SQLITE] Falha ao carregar do banco: {e}")
        print("[⚠️] Tentando carregar do usuarios.json...")

        path_json = os.path.join(os.path.dirname(__file__), 'usuarios.json')
        try:
            with open(path_json, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as json_erro:
            print(f"[ERRO JSON] Falha ao carregar usuarios.json: {json_erro}")
            return []

def salvar_usuarios(lista):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    caminho = os.path.join(base_dir, 'usuarios.json')
    with open(caminho, 'w', encoding='utf-8') as f:
        json.dump(lista, f, indent=2, ensure_ascii=False)

def log_request(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        app.logger.info(f"Requisição recebida: {request.method} {request.path}")
        return f(*args, **kwargs)
    return decorated_function

def enviar_para_stream(usuario_id, mensagem):
    if usuario_id in streams_usuarios:
        for fila in streams_usuarios[usuario_id]:
            fila.put(mensagem)


def inicializar_banco():
    conn = sqlite3.connect('acionamentos.db')
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT UNIQUE,
        senha TEXT,
        setor TEXT,
        funcao TEXT,
        foto TEXT,
        bio TEXT,
        status TEXT DEFAULT 'ativo'
    )
''')
    
    cursor.execute("SELECT * FROM usuarios WHERE nome = ?", ("admin",))
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO usuarios (nome, senha, setor, funcao)
            VALUES (?, ?, ?, ?)
        """, ("admin", "123", "suporte", "dev"))

    

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS caixas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regiao TEXT,
            setor TEXT,
            cto TEXT,
            problema TEXT,
            clientes INTEGER,
            obs TEXT,
            protocolo TEXT,
            previsao TEXT,
            acionado_em DATETIME,
            status TEXT DEFAULT 'pendente',
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
            acionado_por TEXT,
            validado_por TEXT, 
            validado_em DATETIME,
            resolvido_por TEXT,
            resolvido_em DATETIME,
            resolucao TEXT,
            imagem_resolucao TEXT
        )
    ''')



    conn.commit()
    conn.close()

def formatar_obs(obs, para_copiar=False):
    if not obs:
        return ""
    
    protocolo = None
    previsao = None
    if "Protocolo:" in obs and "Previsão:" in obs:
        parts = obs.split("|")
        for part in parts:
            if "Protocolo:" in part:
                protocolo = part.split("Protocolo:")[1].split(",")[0].strip()
            if "Previsão:" in part:
                previsao = part.split("Previsão:")[1].strip()
    
    if para_copiar:
        formatted = obs.split("|")[0].strip()
        if protocolo:
            formatted += f"\nProtocolo: {protocolo}"
        return formatted
    else:
        formatted = obs.replace("|", "<br>")
        return formatted

def requer_permissao(permissoes=None):
    if permissoes is None:
        permissoes = []

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Se não estiver logado → vai pro login
            if 'usuario' not in session:
                return redirect('/login')

            usuario = session.get('usuario', {})
            funcao = str(usuario.get('funcao', '')).lower()
            setor = str(usuario.get('setor', '')).lower()
            rota = (request.endpoint or '').lower()

            # Rotas sempre permitidas
            if rota in ['perfil', 'logout']:
                return f(*args, **kwargs)

            # Normaliza permissões
            permissoes_norm = [p.lower() for p in permissoes]
            print("DEBUG:", session.get('usuario'))
            # Se a função ou o setor estiver permitido → entra
            if funcao in permissoes_norm or setor in permissoes_norm:
                return f(*args, **kwargs)

            # Caso contrário → bloqueia e redireciona
            flash('🚫 Você não tem permissão para acessar esta página.', 'error')
            return redirect('/')
        return wrapper
    return decorator


def notificar_usuario(destinatario_id, mensagem):
    fila = streams_usuarios.get(destinatario_id)
    if fila:
        for q in fila:
            q.put(mensagem)

# ======================
# INICIALIZAÇÃO DO BANCO
# ======================
inicializar_banco()

# ======================
# ROTAS DO EDDIE HOME
# ======================

@app.route('/eddie_chat', methods=['GET'])
def eddie_chat():
    return render_template('eddie_chat.html')



# ==========================================
# IA LOCAL - OLLAMA (SEM TRAVAR)
# ==========================================
@app.route('/eddie_ai', methods=['POST'])
def eddie_ai():
    try:
        data = request.get_json(force=True)
        pergunta = data.get("mensagem", "").strip()
        if not pergunta:
            return jsonify({"resposta": "⚠️ Pergunta vazia."})

        # envia pro Ollama
        r = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": "tinydolphin",
                "prompt": f"Você é o Eddie, assistente técnico da Fênix Internet. Responda de forma breve e técnica.\nPergunta: {pergunta}"
            },
            stream=True,
            timeout=40
        )

        texto = ""
        for linha in r.iter_lines():
            if linha:
                try:
                    parte = json.loads(linha.decode("utf-8"))
                    texto += parte.get("response", "")
                except Exception:
                    continue

        resposta_final = texto.strip() or "🤔 Não consegui formular uma resposta agora."
        print(f"[EDDIE AI] Pergunta: {pergunta}\n[EDDIE AI] Resposta: {resposta_final[:120]}...")
        return jsonify({"resposta": resposta_final})

    except Exception as e:
        print(f"[ERRO /eddie_ai] {e}")
        return jsonify({"resposta": f"⚠️ Erro interno: {e}"})





@app.route('/menu_retratil')
def menu_retratil():
    usuario = session['usuario']
    return render_template('menu_retratil.html', nome=usuario['nome'], funcao=usuario.get('funcao', ''))




@app.route('/eddie_home')
def eddie_home():
    if 'usuario' not in session:
        return redirect('/login_page')

    usuario = session['usuario']
    nome = usuario.get('nome', 'Usuário')
    setor = usuario.get('setor', '')
    funcao = usuario.get('funcao', '')

    return render_template('eddiehome.html', nome=nome, setor=setor, funcao=funcao)


@app.route('/get_postits')
def get_postits():
    try:
        usuario = session.get('usuario', {})
        nome = (usuario.get('nome') or '').strip()
        setor = (usuario.get('setor') or '').strip().lower()
        funcao = (usuario.get('funcao') or '').strip().lower()

        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM postits ORDER BY criado_em DESC")
        registros = cursor.fetchall()
        conn.close()

        visiveis = []
        for r in registros:
            p = dict(r)
            categoria = (p.get('categoria') or '').lower().strip()
            autor = (p.get('autor') or '').strip().lower()
            setor_postit = (p.get('setor') or '').strip().lower()

            # Regras de visibilidade
            if categoria == 'empresa':
                visiveis.append(p)
            elif categoria == 'equipe' and setor_postit == setor:
                visiveis.append(p)
            elif categoria == 'pessoal' and autor == nome.lower():
                visiveis.append(p)

        print(f"[get_postits] Enviando {len(visiveis)} post-its visíveis para {nome} ({setor}/{funcao})")
        return jsonify(visiveis)

    except Exception as e:
        print(f"[get_postits] Erro: {e}")
        return jsonify({"erro": str(e)}), 500



@app.route('/adicionar_postit', methods=['POST'])
def adicionar_postit():
    try:
        data = request.get_json()
        titulo = data.get('titulo')
        descricao = data.get('descricao')
        categoria = data.get('categoria')

        usuario = session.get('usuario', {})
        autor = usuario.get('nome', 'Desconhecido')
        setor = usuario.get('setor', '')
        funcao = usuario.get('funcao', '')

        conn = sqlite3.connect('acionamentos.db')
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO postits (titulo, descricao, categoria, autor, setor, funcao, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
        """, (titulo, descricao, categoria, autor, setor, funcao))
        conn.commit()
        conn.close()

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500



@app.route('/feed_acionamentos')
def feed_acionamentos():
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                cto,
                status,
                acionado_por,
                validado_por,
                resolvido_por,
                acionado_em,
                validado_em,
                resolvido_em
            FROM caixas
            ORDER BY acionado_em DESC
            LIMIT 10
        """)
        registros = cursor.fetchall()
        conn.close()

        feed = []
        for r in registros:
            data_ref = None
            status = (r['status'] or '').lower()

            # Prioriza a data conforme o status
            if status == 'resolvido' and r['resolvido_em']:
                data_ref = r['resolvido_em']
            elif status == 'em_andamento' and r['validado_em']:
                data_ref = r['validado_em']
            elif r['acionado_em']:
                data_ref = r['acionado_em']

            tempo = "—"
            if data_ref:
                try:
                    # Aceita múltiplos formatos de data (com ou sem milissegundos, com T, etc)
                    data_ref = data_ref.replace("T", " ").split(".")[0]
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
                        try:
                            dt = datetime.strptime(data_ref, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        dt = None

                    if dt:
                        diff = datetime.now() - dt
                        minutos = int(diff.total_seconds() // 60)
                        tempo = f"{minutos}min atrás" if minutos < 60 else f"{minutos // 60}h atrás"
                except Exception as e:
                    print(f"[feed_acionamentos] Erro ao calcular tempo: {e}")

            feed.append({
                "cto": r['cto'],
                "status": r['status'],
                "acionado_por": r['acionado_por'] or '',
                "validado_por": r['validado_por'] or '',
                "resolvido_por": r['resolvido_por'] or '',
                "tempo": tempo
            })

        return jsonify(feed)
    except Exception as e:
        print(f"[feed_acionamentos] Erro geral: {e}")
        return jsonify({"erro": str(e)}), 500

@app.route('/excluir_postit/<int:postit_id>', methods=['DELETE'])
def excluir_postit(postit_id):
          try:
              usuario = session.get('usuario', {})
              autor = usuario.get('nome', '')
      
              conn = sqlite3.connect('acionamentos.db')
              cursor = conn.cursor()
              cursor.execute("SELECT autor FROM postits WHERE id = ?", (postit_id,))
              dono = cursor.fetchone()
      
              if not dono:
                  return jsonify({'erro': 'Post-it não encontrado'}), 404
              if dono[0].lower() != autor.lower():
                  return jsonify({'erro': 'Sem permissão'}), 403
      
              cursor.execute("DELETE FROM postits WHERE id = ?", (postit_id,))
              conn.commit()
              conn.close()
              return jsonify({'status': 'ok'})
          except Exception as e:
              return jsonify({'erro': str(e)}), 500
      
@app.route('/editar_postit/<int:postit_id>', methods=['PUT'])
def editar_postit(postit_id):
          try:
              usuario = session.get('usuario', {})
              autor = usuario.get('nome', '')
      
              data = request.get_json()
              novo_titulo = data.get('titulo')
              nova_descricao = data.get('descricao')
      
              conn = sqlite3.connect('acionamentos.db')
              cursor = conn.cursor()
              cursor.execute("SELECT autor FROM postits WHERE id = ?", (postit_id,))
              dono = cursor.fetchone()
      
              if not dono:
                  return jsonify({'erro': 'Post-it não encontrado'}), 404
              if dono[0].lower() != autor.lower():
                  return jsonify({'erro': 'Sem permissão'}), 403
      
              cursor.execute("UPDATE postits SET titulo=?, descricao=? WHERE id=?", 
                             (novo_titulo, nova_descricao, postit_id))
              conn.commit()
              conn.close()
              return jsonify({'status': 'ok'})
          except Exception as e:
              return jsonify({'erro': str(e)}), 500
      


# ======================
# ROTAS DE AUTENTICAÇÃO
# ======================
@app.route('/login', methods=['POST'])
@log_request
def login():
    if not request.is_json:
        return jsonify({"status": "erro", "mensagem": "Content-Type deve ser application/json"}), 400

    dados = request.get_json()
    if not dados or 'nome' not in dados or 'senha' not in dados:
        return jsonify({"status": "erro", "mensagem": "Campos 'nome' e 'senha' são obrigatórios"}), 400

    nome = dados['nome'].strip()
    senha = dados['senha'].strip()
    usuarios = carregar_usuarios()

    for usuario in usuarios:
        if usuario['nome'] == nome and usuario['senha'] == senha:
            return jsonify({
                "status": "ok",
                "nome": usuario["nome"],
                "setor": usuario["setor"],
                "funcao": usuario.get("funcao", "")
            })

    app.logger.warning(f"Tentativa de login falha para usuário: {nome}")
    return jsonify({"status": "erro", "mensagem": "Usuário ou senha inválidos"}), 401

@app.route('/login_page', methods=['GET'])
def login_page():
    return render_template('login.html')

@app.route('/login_form', methods=['POST'])
def login_form():
    nome = request.form.get('nome', '').strip()
    senha = request.form.get('senha', '').strip()
    usuarios = carregar_usuarios()

    for usuario in usuarios:
        if usuario['nome'] == nome and usuario['senha'] == senha:
            session['usuario'] = {
                "nome": usuario["nome"],
                "setor": usuario.get("setor", "").lower(),
                "funcao": usuario.get("funcao", "").lower(),
                "foto": usuario.get("foto", "icon.png"),
                "bio": usuario.get("bio", ""),
                "status": usuario.get("status", "Ausente"),
                "id": usuario["id"]
            }
            session['nome'] = usuario['nome']

            if session['usuario']['setor'] == 'infra':
                funcao = session['usuario']['funcao']
                if funcao == 'matriz':
                    return redirect('/painel_matriz')
                elif funcao == 'sinos':
                    return redirect('/painel_sinos')
                elif funcao == 'litoral':
                    return redirect('/painel_litoral')
                else:
                    abort(403)

            return redirect('/menu')

    return render_template('login.html', erro='Usuário ou senha inválidos')

@app.route('/logout')
def logout():
    session.pop('usuario', None)
    return redirect('/login_page')

# ======================
# ROTAS PRINCIPAIS
# ======================
@app.route('/menu')
@requer_permissao(['auxiliar', 'n1', 'n2', 'Admin', 'supervisor', 'dev', 'infra', 'gerente'])
def menu():
    usuario = session['usuario']
    setor = usuario.get('setor')
    funcao = usuario.get('funcao')

    if setor == 'infra':
        if funcao == 'matriz':
            return redirect('/painel_matriz')
        elif funcao == 'sinos':
            return redirect('/painel_sinos')
        elif funcao == 'litoral':
            return redirect('/painel_litoral')
    
    return render_template('menu.html', nome=usuario['nome'], funcao=usuario.get('funcao', ''))

@app.route('/boas_vindas')
def boas_vindas():
    return render_template("boas_vindas.html")

@app.route('/calcula')
def calcula():  
    return render_template("calcula.html")

@app.route('/teste')
def teste():
    return render_template('teste.html')

# ======================
# ROTAS DE ACIONAMENTOS
# ======================
@app.route('/acionamentos', methods=['GET', 'POST'])
@requer_permissao(['auxiliar', 'n1', 'n2', 'admin', 'supervisor', 'dev'])
def acionamentos():
    usuario = session['usuario']
    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        cursor = conn.cursor()
        
        if request.method == 'POST':
            setor = request.form.get('setor', '').strip()
            if setor not in ['Matriz', 'Sinos', 'Litoral']:
                setor = 'Matriz'
            
            agora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            dados = {
                'regiao': request.form.get('regiao', '').strip(),
                'setor': setor,
                'cto': request.form.get('cto', '').strip(),
                'problema': request.form.get('problema', '').strip(),
                'clientes': int(request.form.get('clientes', 0)),
                'obs': request.form.get('obs', '').strip(),
                'status': 'pendente',
                'criado_em': agora,
                'acionado_por': usuario['nome'],
                'acionado_em': agora
            }
            
            cursor.execute('''
                INSERT INTO caixas (
                    regiao, setor, cto, problema, clientes, obs,
                    status, criado_em, acionado_por, acionado_em
                )
                VALUES (
                    :regiao, :setor, :cto, :problema, :clientes, :obs,
                    :status, :criado_em, :acionado_por, :acionado_em
                )
            ''', dados)
            conn.commit()

            try:
                conn_temp = sqlite3.connect('acionamentos.db')
                cursor_temp = conn_temp.cursor()
                cursor_temp.execute("SELECT COUNT(*) FROM caixas WHERE status = 'pendente'")
                total = cursor_temp.fetchone()[0]
                conn_temp.close()
                for q in contador_ouvintes:
                    q.put(total)
            except Exception as e:
                app.logger.warning(f"Erro ao atualizar f ao vivo: {e}")

            texto_notificacao = f"[{dados['setor']}] {dados['regiao']} - {dados['cto']} acionado ({dados['problema']})"
            for q in subscribers:
                q.put(texto_notificacao)

            flash("Acionamento cadastrado com sucesso!", "success")
            return redirect('/painel_geral')

        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
            id, 
            regiao, 
            setor, 
            cto, 
            problema, 
            clientes, 
            obs, 
            status,
            strftime('%H:%M', criado_em) || ' - ' || strftime('%d/%m/%Y', criado_em) as criado_em_formatado
            FROM caixas 
            WHERE status NOT IN ('resolvido')
            ORDER BY criado_em DESC
        ''')
        caixas = [dict(row) for row in cursor.fetchall()]
        
        return render_template('acionamentos.html', caixas=caixas)
        
    except Exception as e:
        if conn:
            conn.rollback()
        app.logger.error(f"Erro no acionamento: {str(e)}")
        flash("Erro ao processar o acionamento", "error")
        abort(500)
    finally:
        if conn:
            conn.close()

@app.route('/painel_geral')
@requer_permissao(['auxiliar','n1', 'n2', 'admin', 'supervisor', 'dev', 'gerente'])
def painel_geral():
    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                id,
                regiao,
                setor,
                cto,
                problema,
                clientes,
                obs,
                status,
                protocolo,
                previsao,
                acionado_por,
                validado_por,
                validado_em,
                strftime('%H:%M', acionado_em) || ' - ' || strftime('%d/%m/%Y', acionado_em) as acionado_em_formatado,
                strftime('%H:%M', validado_em) || ' - ' || strftime('%d/%m/%Y', validado_em) as validado_em_formatado,
                strftime('%H:%M', criado_em) || ' - ' || strftime('%d/%m/%Y', criado_em) as criado_em_formatado
            FROM caixas 
            WHERE status IN ('em_andamento', 'agendar_migracao', 'agendar_sem_voltagem', 'agendar_ajuste_de_drop')
            ORDER BY acionado_em DESC
        ''')
        
        caixas_raw = cursor.fetchall()
        caixas = []

        for row in caixas_raw:
            c = dict(row)
            if c.get('validado_em'):
                try:
                    dt = datetime.strptime(c['validado_em'], '%Y-%m-%d %H:%M:%S')
                    c['validado_em_iso'] = dt.isoformat()
                except:
                    c['validado_em_iso'] = ''
            else:
                c['validado_em_iso'] = ''
            caixas.append(c)

        return render_template('painel_geral.html', caixas=caixas)

    except Exception as e:
        app.logger.error(f"Erro no painel geral: {str(e)}")
        abort(500)
    finally:
        if conn:
            conn.close()

@app.route('/painel_atividades', methods=['GET', 'POST'])
@requer_permissao(['n1', 'n2', 'admin', 'supervisor', 'dev'])
def painel_atividades():
    usuario = session['usuario']
    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        
        if request.method == 'POST':
            id_caixa = request.form['id']
            protocolo = request.form['protocolo']
            previsao_raw = request.form['previsao']
            agora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            try:
                previsao_formatada = datetime.strptime(previsao_raw, "%Y-%m-%dT%H:%M").strftime("%H:%M %d/%m/%Y")
            except ValueError:
                previsao_formatada = previsao_raw

            validado_por = usuario['nome']
            validado_em = agora
            acionado_em = agora

            cursor = conn.cursor()
            cursor.execute('''
                UPDATE caixas
                SET status = 'em_andamento',
                    protocolo = ?,
                    previsao = ?,
                    acionado_em = ?,
                    validado_por = ?,
                    validado_em = ?,
                    obs = CASE 
                        WHEN obs IS NULL THEN 'Protocolo: ' || ? || ', Previsão: ' || ?
                        ELSE obs || ' | Protocolo: ' || ? || ', Previsão: ' || ?
                    END
                WHERE id = ?
            ''', (
                protocolo,
                previsao_formatada,
                acionado_em,
                validado_por,
                validado_em,
                protocolo,
                previsao_formatada,
                protocolo,
                previsao_formatada,
                id_caixa
            ))
            conn.commit()

            try:
                for s in subscribers:
                    s.put("atualizar")
            except Exception as e:
                app.logger.warning(f"Erro ao enviar atualização do painel de atividades: {e}")

            return redirect('/painel_geral')

        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                id, 
                regiao, 
                setor,
                cto, 
                problema, 
                clientes, 
                obs,
                status,
                strftime('%d/%m/%Y %H:%M', criado_em) as criado_em,
                strftime('%H:%M', validado_em) || ' - ' || strftime('%d/%m/%Y', validado_em) as validado_em_formatado
            FROM caixas 
            WHERE status = 'pendente'
            ORDER BY criado_em DESC
        ''')

        caixas_raw = cursor.fetchall()
        caixas = []

        for caixa in caixas_raw:
            c = dict(caixa)
            if c.get('validado_em_formatado'):
                try:
                    dt = datetime.strptime(c['validado_em_formatado'], '%H:%M - %d/%m/%Y')
                    c['validado_em_iso'] = dt.isoformat()
                except Exception:
                    c['validado_em_iso'] = ''
            else:
                c['validado_em_iso'] = ''
            caixas.append(c)

        return render_template('painel_atividades.html', caixas=caixas)

    except Exception as e:
        app.logger.error(f"Erro no painel de atividades: {str(e)}")
        abort(500)
    finally:
        if conn:
            conn.close()

@app.route('/atualizar_caixa', methods=['POST'])
@requer_permissao(['n1', 'n2', 'admin', 'supervisor', 'dev'])
def atualizar_caixa():
    usuario = session['usuario']
    id_caixa = request.form['id']
    protocolo = request.form['protocolo']
    previsao_bruta = request.form['previsao']
    previsao = datetime.strptime(previsao_bruta, "%Y-%m-%dT%H:%M").strftime("%Y/%m/%d %H:%M")
    agora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    validado_por = usuario['nome']
    validado_em = agora
    acionado_em = agora

    conn = sqlite3.connect('acionamentos.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE caixas
        SET status = 'em_andamento',
            protocolo = ?,
            previsao = ?,
            acionado_em = ?,
            validado_por = ?,
            validado_em = ?,
            obs = CASE 
                WHEN obs IS NULL THEN 'Protocolo: ' || ? || ', Previsão: ' || ?
                ELSE obs || ' | Protocolo: ' || ? || ', Previsão: ' || ?
            END
        WHERE id = ?
    ''', (
        protocolo,
        previsao,
        acionado_em,
        validado_por,
        validado_em,
        protocolo,
        previsao,
        protocolo,
        previsao,
        id_caixa
    ))
    conn.commit()

    try:
        conn_temp = sqlite3.connect('acionamentos.db')
        cursor_temp = conn_temp.cursor()
        cursor_temp.execute("SELECT COUNT(*) FROM caixas WHERE status = 'pendente'")
        total = cursor_temp.fetchone()[0]
        conn_temp.close()
        for q in contador_ouvintes:
            q.put(total)
    except Exception as e:
        app.logger.warning(f"Erro ao notificar contador após atualizar status: {e}")

    conn.close()

    try:
        for s in subscribers:
            s.put("atualizar")
    except Exception as e:
        app.logger.warning(f"Erro ao enviar atualização para painel geral: {e}")

    return redirect('/painel_geral')

@app.route('/editar_caixa/<int:id>', methods=['GET', 'POST'])
@requer_permissao(['n1', 'n2', 'Admin', 'supervisor', 'dev'])
def editar_caixa(id):
    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        
        if request.method == 'POST':
            dados = {
                'id': id,
                'regiao': request.form['regiao'],
                'setor': request.form['setor'],
                'cto': request.form['cto'],
                'problema': request.form['problema'],
                'clientes': request.form['clientes'],
                'obs': request.form['obs'],
                'status': request.form.get('status', 'pendente')
            }
            
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE caixas SET
                    regiao = :regiao,
                    setor = :setor,
                    cto = :cto,
                    problema = :problema,
                    clientes = :clientes,
                    obs = :obs,
                    status = :status
                WHERE id = :id
            ''', dados)
            conn.commit()

            cursor.execute("SELECT COUNT(*) FROM caixas WHERE status = 'pendente'")
            total = cursor.fetchone()[0]
            for q in contador_ouvintes:
                q.put(total)

            try:
                for s in subscribers:
                    s.put("atualizar")
            except Exception as e:
                app.logger.warning(f"Erro ao enviar atualização do editar_caixa: {e}")

            return redirect('/acionamentos')
        
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM caixas WHERE id = ?', (id,))
        caixa = cursor.fetchone()
        
        if not caixa:
            return redirect('/acionamentos')
            
        return render_template('editar_caixa.html', caixa=dict(caixa), status_options=STATUS)

    except Exception as e:
        app.logger.error(f"Erro ao editar caixa: {str(e)}")
        abort(500)

    finally:
        if conn:
            conn.close()

@app.route('/excluir_caixa/<int:id>', methods=['POST'])
@requer_permissao(['n1', 'n2', 'admin', 'supervisor', 'dev'])
def excluir_caixa(id):
    try:
        conn = sqlite3.connect('acionamentos.db')
        cursor = conn.cursor()
        cursor.execute('DELETE FROM caixas WHERE id = ?', (id,))
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM caixas WHERE status = 'pendente'")
        total = cursor.fetchone()[0]
        for q in contador_ouvintes:
            q.put(total)

        try:
            for s in subscribers:
                s.put("atualizar")
        except Exception as e:
            app.logger.warning(f"Erro ao enviar atualização após exclusão: {e}")

        flash('Caixa excluída com sucesso!', 'success')

    except Exception as e:
        conn.rollback()
        app.logger.error(f"Erro ao excluir caixa: {str(e)}")
        flash('Erro ao excluir caixa', 'error')

    finally:
        if conn:
            conn.close()

    return redirect('/painel_geral')

@app.route('/alterar_status/<int:id>', methods=['POST'])
@requer_permissao(['n1', 'n2', 'admin', 'supervisor', 'dev'])
def alterar_status(id):
    try:
        novo_status = request.form['novo_status']
        conn = sqlite3.connect('acionamentos.db')
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE caixas 
            SET status = ?
            WHERE id = ?
        ''', (novo_status, id))
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM caixas WHERE status = 'pendente'")
        total = cursor.fetchone()[0]
        for q in contador_ouvintes:
            q.put(total)

        flash('Status atualizado com sucesso!', 'success')
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Erro ao alterar status: {str(e)}")
        flash('Erro ao alterar status', 'error')
    finally:
        if conn:
            conn.close()
    
    return redirect(request.referrer or '/acionamentos')

@app.route('/resolver_caixa', methods=['POST'])
def resolver_caixa():
    id_caixa = request.form['id_caixa']
    resolvido_em = request.form['resolvido_em']
    resolucao = request.form['resolucao']
    status = request.form['status']
    foto = request.files.get('foto')
    
    # PEGAR O USUÁRIO DA SESSÃO
    resolvido_por = session.get('usuario', {}).get('nome', 'Desconhecido')

    foto_nome = None
    if foto:
        extensao = os.path.splitext(foto.filename)[1]
        foto_nome = f"{uuid.uuid4()}{extensao}"
        caminho = os.path.join('static', 'fotos', foto_nome)
        foto.save(caminho)

    conn = sqlite3.connect('acionamentos.db')
    cursor = conn.cursor()
    
    # ATUALIZAR INCLUINDO resolvido_por
    cursor.execute("""
        UPDATE caixas
        SET resolucao = ?, resolvido_em = ?, status = ?, imagem_resolucao = ?, resolvido_por = ?
        WHERE id = ?
    """, (resolucao, resolvido_em, status, foto_nome, resolvido_por, id_caixa))
    
    conn.commit()
    conn.close()

    eventos_refresh.put(json.dumps({
        "tipo": "refresh",
        "mensagem": "Caixa resolvida",
        "id_caixa": id_caixa
    }))
    
    return jsonify({"status": "ok"})


# ======================
# ROTAS DE PAINÉIS
# ======================
@app.route('/painel_admin')
@requer_permissao(['n1', 'n2', 'admin', 'supervisor', 'dev'])
def painel_admin():
    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT 
                id,
                regiao,
                setor,
                cto,
                problema,
                clientes,
                obs,
                status,
                protocolo,
                previsao,
                strftime('%d/%m/%Y %H:%M', criado_em) as criado_em_formatado,
                strftime('%d/%m/%Y %H:%M', acionado_em) as acionado_em_formatado
            FROM caixas 
            ORDER BY criado_em DESC
        ''')

        caixas = [dict(row) for row in cursor.fetchall()]
        usuario = session.get('usuario')
        funcao = usuario.get('funcao') if usuario else None

        return render_template('painel_admin.html', caixas=caixas, funcao=funcao)

    except Exception as e:
        app.logger.error(f"Erro no painel admin: {str(e)}")
        abort(500)
    finally:
        if conn:
            conn.close()

@app.route('/painel_matriz', methods=['GET', 'POST'])
@requer_permissao(['infra', 'matriz'])
def painel_matriz():
    usuario = session['usuario']
    funcao = usuario.get('funcao', '').lower()
    setor = usuario.get('setor', '').lower()

    if not (setor == 'infra' and funcao == 'matriz'):
        abort(403)

    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if request.method == 'POST':
            id_caixa = int(request.form.get('id'))
            resolucao = request.form.get('relato', '').strip()
            status = request.form.get('status')
            foto = request.files.get('foto')

            if not id_caixa or not resolucao or not status:
                flash('Todos os campos são obrigatórios.', 'error')
                return redirect('/painel_matriz')

            caminho_foto = None
            if foto and foto.filename:
                filename = secure_filename(str(uuid.uuid4()) + '_' + foto.filename)
                os.makedirs(os.path.join('static', 'fotos'), exist_ok=True)
                foto.save(os.path.join('static', 'fotos', filename))
                caminho_foto = f'fotos/{filename}'

            cursor.execute('''
                UPDATE caixas
                SET status = ?,
                    resolvido_em = ?,
                    resolvido_por = ?,
                    resolucao = ?,
                    imagem_resolucao = ?
                WHERE id = ?
            ''', (status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  usuario['nome'], resolucao, caminho_foto, id_caixa))
            conn.commit()

            flash('Caixa resolvida com sucesso!', 'success')
            return redirect('/painel_matriz')

        cursor.execute('''
            SELECT 
                id,
                regiao,
                setor,
                cto,
                problema,
                clientes,
                obs,
                status,
                strftime('%d/%m/%Y %H:%M', criado_em) as criado_em_formatado
            FROM caixas 
            WHERE LOWER(status) = 'em_andamento' AND LOWER(setor) = 'matriz'
            ORDER BY criado_em DESC
        ''')
        caixas = [dict(row) for row in cursor.fetchall()]

        return render_template('painel_matriz.html', caixas=caixas)

    except Exception as e:
        app.logger.error(f"Erro no painel matriz: {str(e)}")
        abort(500)
    finally:
        if conn:
            conn.close()

@app.route('/painel_sinos', methods=['GET', 'POST'])
@requer_permissao(['infra', 'sinos'])
def painel_sinos():
    usuario = session['usuario']
    funcao = usuario.get('funcao', '').lower()
    setor = usuario.get('setor', '').lower()

    if not (setor == 'infra' and funcao == 'sinos'):
        abort(403)

    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if request.method == 'POST':
            id_caixa = int(request.form.get('id'))
            resolucao = request.form.get('relato', '').strip()
            status = request.form.get('status')
            foto = request.files.get('foto')

            if not id_caixa or not resolucao or not status:
                flash('Todos os campos são obrigatórios.', 'error')
                return redirect('/painel_sinos')

            caminho_foto = None
            if foto and foto.filename:
                filename = secure_filename(str(uuid.uuid4()) + '_' + foto.filename)
                os.makedirs(os.path.join('static', 'fotos'), exist_ok=True)
                foto.save(os.path.join('static', 'fotos', filename))
                caminho_foto = f'fotos/{filename}'

            cursor.execute('''
                UPDATE caixas
                SET status = ?,
                    resolvido_em = ?,
                    resolvido_por = ?,
                    resolucao = ?,
                    imagem_resolucao = ?
                WHERE id = ?
            ''', (status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  usuario['nome'], resolucao, caminho_foto, id_caixa))
            conn.commit()

            flash('Caixa resolvida com sucesso!', 'success')
            return redirect('/painel_sinos')

        cursor.execute('''
            SELECT 
                id,
                regiao,
                setor,
                cto,
                problema,
                clientes,
                obs,
                status,
                strftime('%d/%m/%Y %H:%M', criado_em) as criado_em_formatado
            FROM caixas 
            WHERE LOWER(status) = 'em_andamento' AND LOWER(setor) = 'sinos'
            ORDER BY criado_em DESC
        ''')
        caixas = [dict(row) for row in cursor.fetchall()]

        return render_template('painel_sinos.html', caixas=caixas)

    except Exception as e:
        app.logger.error(f"Erro no painel sinos: {str(e)}")
        abort(500)
    finally:
        if conn:
            conn.close()

@app.route('/painel_litoral', methods=['GET', 'POST'])
@requer_permissao(['infra', 'litoral'])
def painel_litoral():
    usuario = session['usuario']
    funcao = usuario.get('funcao', '').lower()
    setor = usuario.get('setor', '').lower()

    if not (setor == 'infra' and funcao == 'litoral'):
        abort(403)

    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if request.method == 'POST':
            id_caixa = int(request.form.get('id'))
            resolucao = request.form.get('relato', '').strip()
            status = request.form.get('status')
            foto = request.files.get('foto')

            if not id_caixa or not resolucao or not status:
                flash('Todos os campos são obrigatórios.', 'error')
                return redirect('/painel_litoral')

            caminho_foto = None
            if foto and foto.filename:
                filename = secure_filename(str(uuid.uuid4()) + '_' + foto.filename)
                os.makedirs(os.path.join('static', 'fotos'), exist_ok=True)
                foto.save(os.path.join('static', 'fotos', filename))
                caminho_foto = f'fotos/{filename}'

            cursor.execute('''
                UPDATE caixas
                SET status = ?,
                    resolvido_em = ?,
                    resolvido_por = ?,
                    resolucao = ?,
                    imagem_resolucao = ?
                WHERE id = ?
            ''', (status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  usuario['nome'], resolucao, caminho_foto, id_caixa))
            conn.commit()

            flash('Caixa resolvida com sucesso!', 'success')
            return redirect('/painel_litoral')

        cursor.execute('''
            SELECT 
                id,
                regiao,
                setor,
                cto,
                problema,
                clientes,
                obs,
                status,
                strftime('%d/%m/%Y %H:%M', criado_em) as criado_em_formatado
            FROM caixas 
            WHERE LOWER(status) = 'em_andamento' AND LOWER(setor) = 'litoral'
            ORDER BY criado_em DESC
        ''')
        caixas = [dict(row) for row in cursor.fetchall()]

        return render_template('painel_litoral.html', caixas=caixas)

    except Exception as e:
        app.logger.error(f"Erro no painel litoral: {str(e)}")
        abort(500)
    finally:
        if conn:
            conn.close()


# ======================
# ROTAS DE HISTÓRICO E MÉTRICAS
# ======================
@app.route('/historico_geral')
@requer_permissao(['auxiliar', 'n1', 'n2', 'admin', 'supervisor', 'dev', 'gerente'])
def historico_geral():
    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT 
                id,
                regiao,
                setor,
                cto,
                problema,
                clientes,
                obs,
                status,
                protocolo,
                previsao,
                acionado_por,
                validado_por,
                resolvido_por,
                resolucao,
                imagem_resolucao,
                strftime('%H:%M', criado_em) || ' - ' || strftime('%d/%m/%Y', criado_em) as criado_em_formatado,
                strftime('%H:%M', acionado_em) || ' - ' || strftime('%d/%m/%Y', acionado_em) as acionado_em_formatado,
                strftime('%H:%M', validado_em) || ' - ' || strftime('%d/%m/%Y', validado_em) as validado_em_formatado,
                strftime('%H:%M', resolvido_em) || ' - ' || strftime('%d/%m/%Y', resolvido_em) as resolvido_em_formatado
            FROM caixas 
            ORDER BY criado_em DESC
        ''')

        caixas = [dict(row) for row in cursor.fetchall()]
        return render_template('historico_geral.html', caixas=caixas)

    except Exception as e:
        app.logger.error(f"Erro no histórico geral: {str(e)}")
        abort(500)
    finally:
        if conn:
            conn.close()


@app.route('/metricas')
@requer_permissao(['n1', 'n2', 'admin', 'supervisor', 'dev'])
def metricas():
    conn = None
    try:
        conn = sqlite3.connect('acionamentos.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT 
                id,
                setor,
                cto,
                criado_em,
                acionado_em,
                acionado_por,
                validado_por,
                validado_em,
                status,
                previsao
            FROM caixas
            WHERE status IN ('em_andamento', 'resolvido')
            ORDER BY criado_em DESC
        ''')

        caixas = []
        for row in cursor.fetchall():
            try:
                criado_em_str = row['criado_em']
                criado = datetime.strptime(criado_em_str, '%Y-%m-%d %H:%M:%S')

                acionado_em_str = row['acionado_em']
                acionado = datetime.strptime(acionado_em_str, '%Y-%m-%d %H:%M:%S') if acionado_em_str else None

                validado_em_str = row['validado_em']
                validado = datetime.strptime(validado_em_str, '%Y-%m-%d %H:%M:%S') if validado_em_str else None

                tempo = 'Ainda não acionado'
                if acionado:
                    delta = acionado - criado
                    horas, remainder = divmod(delta.total_seconds(), 3600)
                    minutos, _ = divmod(remainder, 60)
                    tempo = f"{int(horas)}h {int(minutos)}min"

                tempo_total = '—'
                if validado:
                    total = validado - criado
                    h, rem = divmod(total.total_seconds(), 3600)
                    m, _ = divmod(rem, 60)
                    tempo_total = f"{int(h)}h {int(m)}min"

                caixas.append({
                    'id': row['id'],
                    'setor': row['setor'],
                    'cto': row['cto'],
                    'criado_em': criado.strftime('%d/%m/%Y %H:%M'),
                    'acionado_em': acionado.strftime('%d/%m/%Y %H:%M') if acionado else '—',
                    'tempo_resposta': tempo,
                    'acionado_por': row['acionado_por'] or '—',
                    'validado_por': row['validado_por'] or '—',
                    'tempo_total': tempo_total,
                    'status': row['status'].replace('_', ' ').title(),
                    'previsao': row['previsao'] or '—'
                })

            except Exception as e:
                app.logger.error(f"Erro ao processar linha {row['id']}: {str(e)}")
                continue

        return render_template('metricas.html', caixas=caixas)

    except Exception as e:
        app.logger.error(f"Erro na rota de métricas: {str(e)}", exc_info=True)
        abort(500)
    finally:
        if conn:
            conn.close()

# ======================
# ROTAS DE USUÁRIOS
# ======================
@app.route('/cadastrar_usuario', methods=['GET', 'POST'])
@requer_permissao(['admin', 'dev', 'supervisor', 'n2'])
def cadastrar_usuario():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        senha = request.form.get('senha', '').strip()
        setor = request.form.get('setor', '').strip().lower()
        funcao = request.form.get('funcao', '').strip()

        if not nome or not senha or not setor:
            flash('Nome, senha e setor são obrigatórios.', 'error')
            return redirect('/cadastrar_usuario')

        if setor == 'infra':
            funcao = funcao
        elif setor != 'suporte':
            funcao = setor
        elif not funcao:
            flash('Função é obrigatória para o setor Suporte.', 'error')
            return redirect('/cadastrar_usuario')

        conn = sqlite3.connect('acionamentos.db')
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO usuarios (nome, senha, setor, funcao, status, foto, bio)
            VALUES (?, ?, ?, ?, 'Online', 'icon.png', '')
        """, (nome, senha, setor, funcao))  # ← Corrigido: 4 placeholders para 4 valores
        conn.commit()
        conn.close()

        flash(f"Usuário '{nome}' criado com sucesso!", 'success')
        return redirect('/usuarios')

    return render_template('cadastrar_usuario.html')

@app.route('/usuarios')
@requer_permissao(['admin', 'dev', 'supervisor', 'n2'])
def listar_usuarios():
    conn = sqlite3.connect('acionamentos.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM usuarios ORDER BY nome;")
    usuarios = cursor.fetchall()
    conn.close()
    return render_template('usuarios.html', usuarios=usuarios)

# ---------- EDITAR USUÁRIO ----------
@app.route('/editar_usuario/<id>', methods=['GET', 'POST'])
@requer_permissao(['admin', 'dev', 'supervisor', 'n2'])
def editar_usuario(id):
    import sqlite3

    conn = sqlite3.connect('acionamentos.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Busca o usuário pelo ID
    cursor.execute("SELECT * FROM usuarios WHERE id = ?", (id,))
    row = cursor.fetchone()
    usuario = dict(row) if row else None

    if not usuario:
        flash('Usuário não encontrado no banco de dados.', 'error')
        conn.close()
        return redirect('/usuarios')

    # --- Se for envio do formulário (edição) ---
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        senha = request.form.get('senha', '').strip()
        setor = request.form.get('setor', '').strip().lower()
        funcao = request.form.get('funcao', '').strip()

        # Validação de campos obrigatórios
        if not nome or not senha or not setor:
            flash('Nome, senha e setor são obrigatórios.', 'error')
            conn.close()
            return redirect(f'/editar_usuario/{id}')

        # Regras de função conforme setor
        if setor == 'infra':
            funcao = funcao
        elif setor != 'suporte':
            funcao = setor
        elif not funcao:
            flash('Função é obrigatória para o setor Suporte.', 'error')
            conn.close()
            return redirect(f'/editar_usuario/{id}')

        try:
            cursor.execute("""
                UPDATE usuarios
                SET nome = ?, senha = ?, setor = ?, funcao = ?
                WHERE id = ?
            """, (nome, senha, setor, funcao, id))
            conn.commit()
            flash('Usuário atualizado com sucesso!', 'success')

        except Exception as e:
            flash(f'Erro ao atualizar usuário: {e}', 'error')

        finally:
            conn.close()

        return redirect('/usuarios')

    # --- Se for apenas exibir o formulário ---
    conn.close()
    return render_template('editar_usuario.html', usuario=usuario)

@app.route('/deletar_usuario/<int:id>', methods=['DELETE'])
@requer_permissao(['admin', 'dev', 'supervisor'])
def deletar_usuario(id):
    try:
        # Impede que o usuário exclua a si mesmo
        usuario_logado = session.get('usuario', {})
        usuario_id_logado = usuario_logado.get('id')
        
        if usuario_id_logado == id:
            return jsonify({"erro": "Você não pode excluir seu próprio usuário!"}), 403
        
        conn = sqlite3.connect('acionamentos.db')
        cursor = conn.cursor()
        
        # Verifica se o usuário existe
        cursor.execute("SELECT nome, funcao FROM usuarios WHERE id = ?", (id,))
        usuario = cursor.fetchone()
        
        if not usuario:
            conn.close()
            return jsonify({"erro": "Usuário não encontrado!"}), 404
        
        nome_usuario = usuario[0]
        funcao_usuario = usuario[1]
        
        # Protege o admin principal (opcional, mas recomendado)
        if nome_usuario.lower() == 'admin' or funcao_usuario == 'dev':
            conn.close()
            return jsonify({"erro": "Não é permitido excluir usuários administradores principais!"}), 403
        
        # Verifica se o usuário tem registros vinculados em caixas
        cursor.execute("""
            SELECT COUNT(*) FROM caixas 
            WHERE acionado_por = ? OR validado_por = ? OR resolvido_por = ?
        """, (nome_usuario, nome_usuario, nome_usuario))
        
        count = cursor.fetchone()[0]
        
        if count > 0:
            conn.close()
            return jsonify({
                "erro": f"Usuário possui {count} registro(s) vinculado(s) no sistema! Exclua ou reassine os registros primeiro."
            }), 400
        
        # Exclui o usuário
        cursor.execute("DELETE FROM usuarios WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        
        return jsonify({"mensagem": f"Usuário '{nome_usuario}' excluído com sucesso!"}), 200
        
    except Exception as e:
        print(f"[ERRO] Falha ao excluir usuário {id}: {e}")
        return jsonify({"erro": f"Erro ao excluir usuário: {str(e)}"}), 500


# ---------- PERFIL (MEU PERFIL) ----------

@app.route('/perfil', methods=['GET', 'POST'])
@requer_permissao(['admin', 'dev', 'supervisor', 'n1', 'n2', 'auxiliar', 'infra', 'matriz', 'sinos', 'litoral', 'gerente'])
def perfil():
    # Garante login ativo
    if 'usuario' not in session:
        return redirect('/login')

    usuario_id = session['usuario']['id']

    conn = sqlite3.connect('acionamentos.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,))
    row = cursor.fetchone()
    usuario_atual = dict(row) if row else None

    if not usuario_atual:
        flash("Usuário não encontrado no banco de dados.", "error")
        conn.close()
        return redirect('/')

    # Se envio de formulário
    if request.method == 'POST':
        novo_nome = request.form.get('nome', '').strip()
        nova_bio = request.form.get('bio', '').strip()
        nova_senha = request.form.get('senha', '').strip()
        novo_status = request.form.get('status', '').strip()
        foto = request.files.get('foto')

        # Atualiza campos
        if novo_nome:
            usuario_atual['nome'] = novo_nome
        usuario_atual['bio'] = nova_bio
        usuario_atual['status'] = novo_status or '💬 Online'

        if nova_senha:
            usuario_atual['senha'] = nova_senha

        # Upload da foto
        if foto and foto.filename:
            extensao = foto.filename.rsplit('.', 1)[-1].lower()
            nome_foto = f"{usuario_id}.{extensao}"
            caminho_foto = os.path.join('static', 'perfis', nome_foto)
            foto.save(caminho_foto)
            usuario_atual['foto'] = nome_foto
        else:
            usuario_atual['foto'] = usuario_atual.get('foto', 'icon.png')

        # Atualiza no banco
        cursor.execute("""
            UPDATE usuarios
            SET nome=?, senha=?, status=?, foto=?, bio=?
            WHERE id=?
        """, (
            usuario_atual['nome'],
            usuario_atual['senha'],
            usuario_atual['status'],
            usuario_atual['foto'],
            usuario_atual['bio'],
            usuario_id
        ))

        conn.commit()
        conn.close()

        # Atualiza sessão
        session['usuario'].update({
            'nome': usuario_atual['nome'],
            'status': usuario_atual['status'],
            'foto': usuario_atual['foto']
        })

        flash("Perfil atualizado com sucesso! 💜", "success")
        return redirect('/perfil')

    # --- Garante que não quebre com campos vazios ---
    usuario_atual['foto'] = usuario_atual.get('foto') or 'icon.png'
    usuario_atual['bio'] = usuario_atual.get('bio') or ''
    usuario_atual['status'] = usuario_atual.get('status') or '💬 Online'

    conn.close()
    return render_template('perfil.html', usuario=usuario_atual)
    

# ======================
# ROTAS DE STREAM (SSE)
# ======================
threading.Thread(target=atualiza_contador_periodicamente, daemon=True).start()
@app.route('/contador_stream')
def contador_stream():
    def gerar():
        q = queue.Queue()
        contador_ouvintes.append(q)
        last_ping = time.time()

        try:
            while True:
                # tenta ler algo da fila com timeout de 10s
                try:
                    dado = q.get(timeout=10)
                    yield f"data: {dado}\n\n"
                except queue.Empty:
                    # envia um ping de keep-alive a cada 30s
                    if time.time() - last_ping > 30:
                        yield ": ping\n\n"
                        last_ping = time.time()
                    continue
        except GeneratorExit:
            # cliente desconectou
            if q in contador_ouvintes:
                contador_ouvintes.remove(q)
        except Exception as e:
            print("Erro em contador_stream:", e)
        finally:
            if q in contador_ouvintes:
                contador_ouvintes.remove(q)

    return Response(stream_with_context(gerar()), mimetype='text/event-stream')


# ======================
# ROTAS DE DEBUG E MIGRAÇÃO
# ======================
@app.route('/debug_caixas')
@requer_permissao(['dev', 'supervisor'])
def debug_caixas():
    conn = sqlite3.connect('acionamentos.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM caixas")
    caixas = cursor.fetchall()
    conn.close()
    return jsonify({
        'count': len(caixas),
        'caixas': caixas,
        'first_caixa_columns': [description[0] for description in cursor.description] if caixas else []
    })

@app.route('/criar_tabela_usuarios')
def criar_tabela_usuarios():
    conn = sqlite3.connect('acionamentos.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id TEXT PRIMARY KEY,
            nome TEXT NOT NULL,
            senha TEXT NOT NULL,
            setor TEXT,
            funcao TEXT,
            foto TEXT,
            bio TEXT,
            status TEXT
        )
    ''')
    conn.commit()
    conn.close()
    return "✅ Tabela 'usuarios' criada com sucesso"

@app.route('/migrar_usuarios')
def migrar_usuarios():
    import json
    with open('usuarios.json', 'r', encoding='utf-8') as f:
        usuarios = json.load(f)

    conn = sqlite3.connect('acionamentos.db')
    cursor = conn.cursor()

    for u in usuarios:
        cursor.execute('''
            INSERT OR IGNORE INTO usuarios (id, nome, senha, setor, funcao, foto, bio, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            u.get('id'),
            u.get('nome'),
            u.get('senha'),
            u.get('setor'),
            u.get('funcao'),
            u.get('foto', 'icon.png'),
            u.get('bio', ''),
            u.get('status', 'Ausente')
        ))

    conn.commit()
    conn.close()
    return "✅ Usuários migrados para o banco"

@app.route('/ver_usuarios')
def ver_usuarios():
    conn = sqlite3.connect('acionamentos.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM usuarios')
    usuarios = cursor.fetchall()
    conn.close()

    html = "<h2>Usuários no banco:</h2><ul>"
    for u in usuarios:
        html += f"<li>{u['nome']} ({u['id']}) - {u['setor']}/{u['funcao']}</li>"
    html += "</ul>"
    return html


# ======================
# FILTROS TEMPLATE E CONTEXT PROCESSORS
# ======================
@app.template_filter('formatar_previsao')
def formatar_previsao(previsao_str):
    from datetime import datetime
    formatos = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]
    for fmt in formatos:
        try:
            dt = datetime.strptime(previsao_str, fmt)
            return dt.strftime("%H:%M - %d/%m/%Y")
        except ValueError:
            continue
    return previsao_str

@app.context_processor
def utility_processor():
    return dict(formatarObs=formatar_obs)


if __name__ == "__main__":
    app.run(debug=False)
