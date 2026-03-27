import subprocess
import time
import socket
import os
import signal
import threading
from datetime import datetime

# ===============================
# 🔧 CONFIGURAÇÕES GERAIS
# ===============================
PORTA_SERVIDOR = 5050
LIMITE_QUEUE_DEPTH = 3
LIMITE_RESTARTS = 5        # evita loop infinito
TOLERANCIA_INICIAL = 10
ESPERA_PARA_SUBIR = 5
MAX_TEMPO_SEM_LOGS = 25    # detecta travamento silencioso
CMD_SERVIDOR = ["python", "run.py"]
MODO_SILENCIOSO = False    # True = sem prints coloridos

# ===============================
# 🎨 CORES TERMINAL
# ===============================
class Cor:
    RESET = "\033[0m"
    VERDE = "\033[92m"
    VERMELHO = "\033[91m"
    AMARELO = "\033[93m"
    AZUL = "\033[94m"
    CINZA = "\033[90m"

def log(msg, cor=Cor.CINZA):
    if not MODO_SILENCIOSO:
        print(f"{cor}[{datetime.now().strftime('%H:%M:%S')}] {msg}{Cor.RESET}")

# ===============================
# ⚡ FUNÇÕES AUXILIARES
# ===============================
def porta_esta_ativa(porta=PORTA_SERVIDOR):
    try:
        with socket.create_connection(("127.0.0.1", porta), timeout=1):
            return True
    except:
        return False

def matar_processo(proc):
    try:
        proc.kill()
        log("💀 Processo encerrado.", Cor.VERMELHO)
    except:
        pass

# ===============================
# 🔍 MONITOR DE LOGS
# ===============================
def monitorar_saida(processo, reiniciar_callback, inicio_timestamp):
    ultimo_log = time.time()

    for linha in iter(processo.stdout.readline, b''):
        texto = linha.decode('utf-8', errors='ignore').strip()
        if texto:
            log(f"{texto}", Cor.CINZA)
            ultimo_log = time.time()

            # Verificações
            if "total open connections reached" in texto:
                log("🧨 Limite de conexões atingido! Reiniciando...", Cor.VERMELHO)
                reiniciar_callback(); break

            if "Task queue depth" in texto:
                tempo_rodando = time.time() - inicio_timestamp
                if tempo_rodando > TOLERANCIA_INICIAL:
                    try:
                        valor = int(texto.split("depth is")[1].strip())
                        if valor >= LIMITE_QUEUE_DEPTH:
                            log(f"🔴 Queue depth crítico ({valor}). Reiniciando...", Cor.VERMELHO)
                            reiniciar_callback(); break
                    except Exception:
                        pass

        # Detecta travamento sem logs
        if time.time() - ultimo_log > MAX_TEMPO_SEM_LOGS:
            log("⏱️ Sem logs por muito tempo, pode estar travado. Reiniciando...", Cor.AMARELO)
            reiniciar_callback()
            break

# ===============================
# 🧠 LOOP PRINCIPAL
# ===============================
falhas_consecutivas = 0

while True:
    log("🚀 Iniciando o servidor...", Cor.AZUL)
    inicio = time.time()

    processo = subprocess.Popen(
        CMD_SERVIDOR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1
    )

    def forcar_restart():
        global falhas_consecutivas
        matar_processo(processo)
        falhas_consecutivas += 1

    # Thread de monitoramento
    thread_log = threading.Thread(
        target=monitorar_saida,
        args=(processo, forcar_restart, inicio),
        daemon=True
    )
    thread_log.start()

    time.sleep(ESPERA_PARA_SUBIR)

    try:
        while True:
            time.sleep(2)

            if processo.poll() is not None:
                log("❌ Processo finalizado. Reiniciando...\n", Cor.VERMELHO)
                break

            if not porta_esta_ativa():
                log("⚠️ Porta não está respondendo. Reiniciando...\n", Cor.AMARELO)
                matar_processo(processo)
                break

    except KeyboardInterrupt:
        log("🛑 Watchdog encerrado manualmente.", Cor.AMARELO)
        matar_processo(processo)
        break

    # Evita loops de restart infinito
    if falhas_consecutivas >= LIMITE_RESTARTS:
        log("🚫 Muitos reinícios consecutivos. Aguardando 60s...", Cor.VERMELHO)
        falhas_consecutivas = 0
        time.sleep(60)

    log("⏳ Reiniciando em 3 segundos...\n", Cor.CINZA)
    time.sleep(3)

