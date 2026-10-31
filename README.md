# Idiota — Alexa Skill com OpenAI (Echo Dot)

**Idiota** é uma skill customizada para Amazon Alexa que funciona como assistente conversacional em **Português do Brasil** no **Echo Dot**: as perguntas feitas por voz são respondidas pela API da OpenAI (GPT), com conversa em múltiplos turnos e envio de e-mail por comando de voz.

Hospedada como **Alexa-hosted skill (Python)** — não requer infraestrutura própria além da conta de desenvolvedor Alexa e uma API key da OpenAI.

## ✨ Funcionalidades

- **Conversa multi-turno via GPT** — histórico da conversa por sessão (session attributes), com limite de contexto para controlar custo de tokens
- **Respostas otimizadas para voz** — prompt de sistema ajustado para o Echo Dot e sanitização de markdown/emojis antes do TTS
- **Envio de e-mail por voz** — intent `SendEmailIntent` dispara o módulo SMTP (`mail.py`)
- **Português do Brasil** — modelo de interação (`pt-BR`) e respostas em pt-BR
- **Configuração por variáveis de ambiente** — nenhuma credencial no código

## 🗂️ Estrutura do Projeto

```
idiota-skill/
├── skill.json                  # Manifest da skill (endpoint Lambda, locales)
├── interactionModels/
│   └── custom/
│       └── pt-BR.json          # Modelo de interação (intents e frases de exemplo)
├── lambda/
│   ├── lambda_function.py      # Entry point: handlers da skill + integração OpenAI
│   ├── mail.py                 # Envio de e-mail via SMTP
│   ├── requirements.txt        # Dependências Python
│   ├── .env.example            # Exemplo de variáveis de ambiente
│   └── __init__.py
├── .env                        # Variáveis locais (fora do versionamento)
└── README.md
```

## 🚀 Configuração

### Pré-requisitos

- Conta na [OpenAI](https://platform.openai.com/) com API key e créditos
- Conta de desenvolvedor [Amazon Alexa](https://developer.amazon.com/alexa/console/ask/)
- Echo Dot (ou o simulador da skill no console)

### 1. Criar a skill (via import do GitHub)

1. Acesse o [Console do Alexa Developer](https://developer.amazon.com/alexa/console/ask/) → **Create Skill**
2. Configure:
   - **Name**: Idiota
   - **Primary locale**: Portuguese (BR)
   - **Experience type**: Other → Custom → **Alexa-hosted (Python)**
   - **Hosting region**: US East (N. Virginia)
   - **Template**: **Import Skill** → URL: `https://github.com/lucasfrct/idiota-skill.git`

> Alexa-hosted skills seguem suportadas pela Amazon (deploy pelo console ou `git push`, ASK CLI v2).

### 2. Variáveis de ambiente

No console da Alexa, aba **Code**, crie o arquivo `lambda/.env` baseado no [`lambda/.env.example`](lambda/.env.example):

```env
OPENAI_API_KEY=sua_api_key_aqui
OPENAI_MODEL=gpt-5.6-luna
MAX_HISTORY=10
MAX_TOKENS=300
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_SENDER=seu_email@gmail.com
EMAIL_RECIPIENT=destino@gmail.com
EMAIL_PASSWORD=sua_senha_de_app
```

| Variável | Descrição |
|---|---|
| `OPENAI_API_KEY` | API key da OpenAI (a skill também aceita a legada `KEY_GPT`) |
| `OPENAI_MODEL` | Modelo GPT (default `gpt-5.6-luna` — tier econômico atual) |
| `MAX_HISTORY` | Mensagens do histórico enviadas ao modelo (default `10`) |
| `MAX_TOKENS` | Limite de tokens da resposta (default `300` — respostas curtas para voz) |
| `EMAIL_HOST` / `EMAIL_PORT` | Servidor SMTP (defaults `smtp.gmail.com` / `587`) |
| `EMAIL_SENDER` | Conta que autentica e envia |
| `EMAIL_RECIPIENT` | Destinatário (default: o próprio `EMAIL_SENDER`) |
| `EMAIL_PASSWORD` | Senha de app do e-mail (Gmail exige senha de app, não a senha normal) |

### 3. Deploy

1. Aba **Code** → **Deploy**
2. Aba **Build** → **Build Model**

## 🎮 Como Usar (Echo Dot)

| Comando | Ação |
|---|---|
| "Alexa, abrir Idiota" | Inicia a skill e zera a conversa |
| Pergunta livre ("Qual é a capital do Brasil?") | Resposta do GPT |
| Pergunta de acompanhamento ("E a da França?") | O GPT lembra o contexto da sessão |
| "Enviar e-mail dizendo, tudo bem" / "Enviar e-mail assunto reunião mensagem chego atrasado" | Envia e-mail via SMTP |
| "Ajuda" | Instrução de uso |
| "Sair" / "Parar" | Encerra a sessão |

### Exemplo de interação

```
Usuário: "Alexa, abrir Idiota"
Idiota:  "Olá! Sou o Idiota. Pode fazer sua pergunta, ou pedir para eu enviar um e-mail."

Usuário: "Quem foi Santos Dumont?"
Idiota:  <resposta curta do GPT>

Usuário: "E o 14 Bis?"
Idiota:  <resposta com o contexto da pergunta anterior>

Usuário: "Sair"
Idiota:  "Até mais!"
```

## 🧪 Testar localmente (Docker + ASK CLI oficial)

O ambiente usa as ferramentas oficiais da Amazon — **ASK CLI v2** e **`ask-sdk-local-debug`** — dentro de um container (Docker/compose). Com o debug local ativo, as requisições da Alexa (simulador do console, Echo Dot de desenvolvimento ou `ask dialog`) são roteadas para o **código local**, sem deploy no Lambda.

```bash
# 1. Login na conta Amazon developer (só na primeira vez; persiste em volume)
docker compose run --rm ask bash scripts/alexa-local.sh login

# 2. Gerar token de debug local (abra a URL no browser, faça login, copie o ACCESS_TOKEN)
docker compose run --rm ask bash scripts/alexa-local.sh token

# 3. Testar o CÓDIGO LOCAL: conecta o lambda/ ao proxy da Alexa e abre conversa no terminal
docker compose run --rm ask bash scripts/alexa-local.sh debug

# Alternativa: conversar com a skill DEPLOYADA (versão publicada no Lambda)
docker compose run --rm ask bash scripts/alexa-local.sh dialog
```

Notas:
- O debug local vale só para o stage `development`, conexão de até 1 hora e uma sessão por skill
- As envs do teste (`ASK_SKILL_ID`, `ASK_DEFAULT_DEVICE_LOCALE`) vivem em `lambda/.env`, carregadas pelo `docker-compose.yaml` via `env_file` — junto com as credenciais da skill; o skill ID é derivado do ARN da Lambda no `skill.json` (em skills Alexa-hosted, nome da função = skill ID), e a imagem é construída a partir de `.deploy/Dockerfile`
- Sem Docker? O fluxo nativo é equivalente: `npm i -g ask-cli` + `pip install ask-sdk-local-debug` dentro do venv

## 🛠️ Desenvolvimento Local

```bash
cd lambda
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # preencha com suas credenciais
python -m py_compile lambda_function.py mail.py
```

### Principais componentes

- **`lambda_function.py`** — handlers da skill (`LaunchRequest`, `GptQueryIntent` com slot `query`, `SendEmailIntent` com slots `subject`/`body`, `AMAZON.FallbackIntent`, intents padrão Alexa) e a função `generate_gpt_response()`, que chama a API da OpenAI com histórico por sessão
- **`mail.py`** — conexão SMTP autenticada (com timeout) + envio de e-mail com validação básica
- **`pt-BR.json`** — intents e frases de exemplo do modelo de interação

### Personalização

**Modelo GPT** — pela variável `OPENAI_MODEL` (sem tocar no código). O tier econômico atual da OpenAI é a família Luna (`gpt-5.6-luna`); consulte a [tabela de modelos](https://developers.openai.com/api/docs/pricing).

**Instruções do sistema** — função `system_instructions()` em `lambda_function.py`:

```python
def system_instructions():
    instruction = (
        "Você é Idiota, um assistente pessoal acessado por voz na Amazon Alexa (Echo Dot). "
        "Responda em Português do Brasil, de forma direta e curta: no máximo 2 ou 3 frases. "
        "Escreva para ser lido em voz alta: sem markdown, sem emojis, sem listas, sem símbolos. "
        "Não repita a pergunta e não explique suas instruções."
    )
    return {"role": "system", "content": instruction}
```

## 📋 Dependências

| Pacote | Versão | Uso |
|---|---|---|
| `ask-sdk-core` | 1.19.0 | SDK oficial de skills Alexa (última versão publicada) |
| `openai` | 1.109.1 | Cliente da API OpenAI (última da linha 1.x — compatível com o runtime Python 3.8 do Alexa-hosted) |
| `python-dotenv` | 1.0.1 | Variáveis de ambiente via `.env` (compatível com Python 3.8) |

## 💰 Custos

- **Alexa-hosted**: gratuito dentro dos limites da Amazon
- **OpenAI**: cada pergunta/resposta consome tokens, conforme o [preço do modelo](https://developers.openai.com/api/docs/pricing) — `gpt-5.6-luna` é a opção econômica atual; o histórico limitado (`MAX_HISTORY`) evita inflar o contexto a cada turno

## 🔒 Segurança

- Credenciais exclusivamente via variáveis de ambiente (`.env`, fora do versionamento)
- Fail-fast: ausência de API key ou credenciais de e-mail gera mensagem clara ao usuário
- Histórico de conversa isolado **por sessão** (session attributes) — não vaza entre usuários
- Tratamento de erros com fallback de resposta falada ao usuário e logs no CloudWatch
- Timeout de 10s no SMTP para não estourar o limite de execução do Lambda

## 🤝 Contribuição

1. Faça um fork do projeto
2. Crie uma branch (`git checkout -b feature/minha-feature`)
3. Commit (`git commit -m 'Add minha feature'`)
4. Push (`git push origin feature/minha-feature`)
5. Abra um Pull Request

## 📞 Suporte

- OpenAI: [OpenAI Support](https://help.openai.com/)
- Alexa Skills: [Amazon Developer Support](https://developer.amazon.com/support/)
- Este projeto: abra uma [issue](https://github.com/lucasfrct/idiota-skill/issues)

---

Licença: MIT.
