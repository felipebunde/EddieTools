# 🚀 Eddie Tools

O **Eddie Tools** é um ecossistema web robusto desenvolvido para centralizar operações críticas e monitoramento de CTOs (Caixas de Terminação Óptica). O sistema foi projetado para resolver a fragmentação de processos, unificando ferramentas de cálculo, gestão de equipe e monitoramento em tempo real.

> **✅ Validação em Produção:** Este sistema foi implementado e segue em uso ativo, servindo como ferramenta central para otimização de fluxo de trabalho e redução de erros operacionais.

---

## 🛠️ Tecnologias Utilizadas

O projeto utiliza uma stack focada em resiliência e rapidez de resposta:

* **Backend:** Python com Flask
* **Segurança:** Flask-CORS e sistema de autenticação com permissões por nível de acesso (RBAC).
* **Banco de Dados:** SQLite (Persistência local estruturada).
* **Frontend:** HTML5, CSS3 e JavaScript (ES6+).
* **Resiliência:** Watchdog customizado para monitoramento e autorrecuperação do serviço.

---

## ✨ Funcionalidades Principais

### 📡 Monitoramento de CTOs
* **Visualização Kanban:** Gestão de status organizada por região geográfica.
* **Tempo Real:** Acompanhamento dinâmico do tempo de acionamento.
* **Rastreabilidade:** Logs detalhados de quem acionou, validou e resolveu cada chamado.

### 🔒 Gestão de Acessos
* Autenticação segura e controle de permissões por cargo.
* Painel administrativo para gestão de usuários.

### 🏠 Eddie Home (Dashboard)
* **Central de Comunicação:** Mural de post-its (Gerência, Equipe, Pessoal).
* **Calendário Integrado:** Gestão de eventos e prazos internos.
* **Live Feed:** Atualizações instantâneas sobre o status da rede.

### 🧮 Ferramentas Internas
* Calculadora de troca de vencimento de faturas.
* Cálculo de desconto proporcional por tempo de interrupção de conexão.

### 🛡️ Sistema Watchdog
* Mecanismo de monitoramento automático que reinicia o backend em caso de quedas, garantindo que a aplicação permaneça online sem intervenção manual.

---

## 📂 Estrutura do Projeto

* `app.py`: Ponto de entrada principal da API/Flask.
* `watchdog.py`: Monitor de disponibilidade e inicializador do processo do backend.
* `run.py`: Script auxiliar de execução da aplicação.
* `iniciar.bat`: Script de lote para inicialização rápida em ambiente Windows.
* `templates/`: Pasta contendo as views HTML (Jinja2).
* `static/`: Pasta contendo assets estáticos (CSS, JS, Imagens).
* `.gitignore`: Arquivo de configuração de exclusões do Git.

---

## 🚀 Como Executar o Projeto

### Pré-requisitos
* Python 3.8+
* pip

### Instalação e Execução

1. **Clone o repositório:**
   git clone https://github.com/felipebunde/eddie-tools.git
   cd eddie-tools

2. **Instale as dependências:**
   pip install -r requirements.txt
   *(Caso o arquivo requirements.txt não esteja presente, certifique-se de instalar `Flask` e `Flask-CORS`).*

3. **Inicie a aplicação via Watchdog (Recomendado):**
   A inicialização através do watchdog garante a monitoria e autorrecuperação do backend.
   python watchdog.py

---

## 🔑 Acesso Padrão

🔗 **URL:** http://localhost:5050/login_page

* **Usuário:** admin
* **Senha:** 123
*(Recomenda-se a alteração da senha logo após o primeiro acesso).*

---

## 👨‍💻 Autor

Desenvolvido por **Felipe Bunde**.

[GitHub](https://github.com/felipebunde)

---
*Este projeto demonstra a aplicação de Python e Flask na resolução de problemas reais de infraestrutura e gestão operacional.*
