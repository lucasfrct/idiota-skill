# Altair - Alexa Skill com Integração OpenAI

Altair é uma skill para Amazon Alexa que integra com a API da OpenAI para fornecer um assistente conversacional inteligente em Português do Brasil. A skill permite fazer perguntas e receber respostas do modelo GPT através de comandos de voz na Alexa.

## 🎯 Funcionalidades

- **Assistente Conversacional**: Integração com modelos GPT da OpenAI (gpt-4o-mini por padrão)
- **Suporte ao Português**: Totalmente configurado para Português do Brasil
- **Funcionalidade de Email**: Capacidade de gerar e enviar emails via SMTP (em desenvolvimento)
- **Hospedagem Alexa**: Compatível com Alexa-hosted Python skills

## 🚀 Como Configurar

### 1. Pré-requisitos

- Conta na [OpenAI](https://platform.openai.com/) com API key
- Conta de desenvolvedor da [Amazon Alexa](https://developer.amazon.com/alexa/console/ask/)
- Créditos na conta OpenAI para uso da API

### 2. Configuração da API OpenAI

1. Acesse [OpenAI API Keys](https://platform.openai.com/account/api-keys)
2. Crie uma nova API key
3. Adicione créditos em [Billing](https://platform.openai.com/account/billing/overview)

### 3. Criação da Alexa Skill

1. Acesse o [Console do Alexa Developer](https://developer.amazon.com/alexa/console/ask/)
2. Clique em "Create Skill"
3. Configure:
   - **Name**: Altair (ou nome de sua preferência)
   - **Primary locale**: Portuguese (BR)
   - **Experience type**: Other > Custom > Alexa-hosted (Python)
   - **Hosting region**: US East (N. Virginia)
   - **Template**: Import Skill
   - **URL**: `https://github.com/lucasfrct/altair-skill.git`

### 4. Configuração das Variáveis de Ambiente

No console da Alexa, vá para a aba "Code" e configure as variáveis de ambiente:

1. Na aba "Code", acesse a pasta `lambda`
2. Crie um arquivo `.env` baseado no `.env.example`
3. Adicione sua API key da OpenAI:

```env
OPENAI_API_KEY=sua_api_key_aqui
```

### 5. Deploy

1. Salve todas as alterações
2. Clique em "Deploy" na aba Code
3. Vá para a aba "Build" e clique em "Build Model"

## 🎮 Como Usar

### Comandos Básicos

- **Ativar**: "Alexa, abrir Altair"
- **Fazer pergunta**: Após ativar, simplesmente faça sua pergunta
- **Exemplos**:
  - "Qual é a capital do Brasil?"
  - "Me explique o que é inteligência artificial"
  - "Como fazer um bolo de chocolate?"
- **Sair**: "Sair" ou "Parar"

### Exemplos de Interação

```
Usuário: "Alexa, abrir Altair"
Altair: "Oi!"

Usuário: "Qual é a capital da França?"
Altair: "A capital da França é Paris."

Usuário: "Sair"
Altair: "Até mais!"
```

## 🛠️ Desenvolvimento Local

### Instalação das Dependências

```bash
cd lambda
pip install -r requirements.txt
```

### Estrutura do Projeto

```
altair-skill/
├── README.md
├── skill.json                 # Configurações da skill
├── interactionModels/
│   └── custom/
│       └── pt-BR.json        # Modelo de interação em português
└── lambda/
    ├── lambda_function.py    # Função principal da skill
    ├── mail.py              # Funcionalidade de email (opcional)
    ├── requirements.txt     # Dependências Python
    ├── .env.example        # Exemplo de variáveis de ambiente
    └── __init__.py
```

### Principais Componentes

- **`lambda_function.py`**: Contém toda a lógica da skill, incluindo handlers para diferentes tipos de requisições
- **`mail.py`**: Funcionalidade de email (em desenvolvimento)
- **`pt-BR.json`**: Define os intents e samples para o modelo de linguagem em português

## 🔧 Configurações Avançadas

### Alteração do Modelo GPT

No arquivo `lambda_function.py`, você pode alterar o modelo utilizado:

```python
MODEL = "gpt-4"  # ou "gpt-3.5-turbo", "gpt-4-1106-preview", etc.
```

Consulte a [lista completa de modelos](https://platform.openai.com/docs/models) da OpenAI.

### Personalização das Respostas

As instruções do sistema podem ser modificadas na função `system_instructions()`:

```python
def system_instructions():
    instruction = """
        Você é um assistente pessoal de nome Altair. 
        Responda de forma, direta e curta. 
        Responda em Português do Brasil.
        [Adicione suas customizações aqui]
    """
    return { "role": "system", "content": instruction, }
```

## 📋 Dependências

- `ask-sdk-core==1.21.1` - SDK da Amazon para desenvolvimento de skills
- `openai==1.54.4` - Cliente oficial da API OpenAI
- `python-dotenv==1.0.1` - Gerenciamento de variáveis de ambiente

## 🔒 Segurança

- ✅ API keys são carregadas via variáveis de ambiente
- ✅ Validação de entrada implementada
- ✅ Tratamento de erros robusto
- ✅ Logs de erro para debugging

## 📄 Licença

Este projeto está sob licença MIT. Veja o arquivo LICENSE para mais detalhes.

## 🤝 Contribuição

Contribuições são bem-vindas! Por favor:

1. Faça um fork do projeto
2. Crie uma branch para sua feature (`git checkout -b feature/AmazingFeature`)
3. Commit suas mudanças (`git commit -m 'Add some AmazingFeature'`)
4. Push para a branch (`git push origin feature/AmazingFeature`)
5. Abra um Pull Request

## 📞 Suporte

- Para problemas relacionados à OpenAI: [OpenAI Support](https://help.openai.com/)
- Para problemas com Alexa Skills: [Amazon Developer Support](https://developer.amazon.com/support/)
- Para problemas específicos deste projeto: Abra uma [issue](https://github.com/lucasfrct/altair-skill/issues)

## 💰 Custos

A utilização desta skill gera custos na API da OpenAI baseados no uso:
- Cada pergunta e resposta consome tokens
- Valores variam por modelo utilizado
- Consulte a [tabela de preços](https://openai.com/pricing) da OpenAI

**Dica**: Comece com o modelo `gpt-4o-mini` que é mais econômico para testes.