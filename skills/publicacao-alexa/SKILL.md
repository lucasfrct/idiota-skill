---
name: publicacao-alexa
description: Passo a passo para publicar o idiota-skill — commit/push no GitHub, deploy automático na Amazon (CodeCommit/CodeBuild + SMAPI com modelo e manifest), verificação do deploy e habilitação da skill no Echo Dot. Use quando precisar publicar a skill, fazer deploy para a Alexa, habilitar/instalar no Echo Dot, atualizar a Lambda ou diagnosticar falha de publicação.
---

# Publicação Idiota — GitHub → Amazon → Echo Dot

Fluxo completo: código → commit/push → CI publica na Alexa-hosted → habilitação no Echo.
Todos os comandos abaixo foram validados na prática neste repo.

## 0. Pré-requisitos

- `gh` autenticado (`gh auth status`) — para acompanhar o CI.
- Container local com ASK CLI: imagem `idiota-ask-cli` (serviço `ask` do `docker-compose.yaml`).
  Verificar: `docker exec idiota-ask ask --version` (2.30.7+).
- `ASK_SKILL_ID` em `lambda/.env` (`amzn1.ask.skill....`).
- GitHub Secrets obrigatórios (`gh secret list`):
  `ASK_SKILL_ID`, `ASK_LWA_CLIENT_ID`, `ASK_LWA_CLIENT_CONFIRMATION`, `ASK_CLI_CONFIG`,
  `OPENAI_API_KEY`, `OPENAI_MODEL`, `MAX_HISTORY`, `MAX_TOKENS`,
  `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_SENDER`, `EMAIL_RECIPIENT`, `EMAIL_PASSWORD`,
  `EMAIL_SUBJECT`. Opcionais: `EMAIL_AUTH` + trio `EMAIL_OAUTH_*` (XOAUTH2),
  `EMAIL_LIST_LIMIT`, `EMAIL_SNIPPET_CHARS`, `LISTING_MAX_TOKENS`.

## 1. Validar antes de publicar

```bash
cd lambda && python3 -m py_compile chat.py mail.py cache.py lambda_function.py
# Suíte completa (repo segue política "apenas código" — testes ficam no histórico):
#   git show b452402:lambda/test_<nome>.py > /tmp/tests/  (extrair e rodar em docker)
docker run --rm -v "$PWD"/..:/workspace:ro -v /tmp/tests:/tests:ro idiota-ask-cli \
  sh -c "cp -r /workspace/lambda /tmp/l && cp /tests/*.py /tmp/l/ && cd /tmp/l && python test_chat_cache_contract.py"
```

- Suítes canônicas: `test_chat_cache_contract.py`, `test_cache.py`, `test_cache_ttl.py`,
  `test_cache_search.py`, `test_mail_flow.py`.
- E2E real opcional (usa API + IMAP do `.env` local): chamar `chat.generate_gpt_response`.

## 2. Commit e push (dispara o deploy)

```bash
git add -A && git commit -m "<mensagem>" && git push origin main
```

- Push em `main` dispara automaticamente o workflow **Deploy Alexa Skill (hosted)**
  (`.github/workflows/deploy-alexa.yml`; também aceita `workflow_dispatch` manual).
- Este repo NÃO tem proxy `.scripts/gh` — git direto mesmo.

## 3. Acompanhar o deploy (Amazon)

```bash
gh run list --workflow deploy-alexa.yml --limit 3
gh run watch <run-id> --exit-status
```

O que o workflow faz (script `scripts/deploy-alexa.sh`, roda SÓ no CI — exige
`GITHUB_WORKSPACE` + Secrets; não executar localmente):

1. Gera `lambda/.env` exclusivamente dos Secrets (segredo nunca vai ao repo público).
2. Clona o repo hospedado (CodeCommit, auth via helper ASK CLI) e sincroniza
   `lambda/`, `interactionModels/` e `skill.json` (`rsync --delete`).
3. Commit + push no CodeCommit → **CodeBuild da Amazon** publica a Lambda.
4. SMAPI REST: `PUT interactionModel/locales/pt-BR` → `POST .../build` (async) →
   `PUT manifest`.

## 4. Verificar a publicação

```bash
gh run view <run-id> --json conclusion --jq .conclusion   # "success"
# status da skill/modelo (aguardar build do modelo, ~1 min):
docker exec idiota-ask ask smapi get-skill-status -s "$ASK_SKILL_ID" | head -30
```

Falha de modelo transiente → reexecutar o workflow ou Build Model no console.

## 5. Habilitar no Echo Dot

A skill development fica disponível nos dispositivos Echo vinculados à MESMA
conta de desenvolvedor. Habilitar (idempotente):

```bash
docker exec idiota-ask ask smapi set-skill-enablement -s "$ASK_SKILL_ID" -g development
docker exec idiota-ask ask smapi get-skill-enablement-status -s "$ASK_SKILL_ID" -g development
```

Resposta vazia (HTTP 204) = habilitado. Testar no Echo: **"Alexa, abre Idiota"** →
*"Idiota, quais são meus e-mails?"* / *"leia o segundo e-mail"*.

## 6. Diagnóstico e manutenção

- `scripts/diagnostico-alexa.sh` — saúde da skill (status, modelo, manifest, invocação e2e).
- Lambda não atualizou: conferir run do workflow → logs do CodeBuild no console AWS.
- Imagem docker local desatualizada (deps mudaram): `docker compose build --pull`
  e **`docker compose up -d --force-recreate`** (o `up -d` simples NÃO recria).
- Código é volume-montado (`:.:/workspace`) — rebuild só é preciso para deps/CLI.
- `gpt-5.6-luna` + function tools: exigem `reasoning_effort="none"` em `/chat/completions`.

## Erros comuns

| Sintoma | Causa | Ação |
|---|---|---|
| `run` falha no passo de credenciais | Secret ausente/renovado | `gh secret set` do valor novo |
| Resposta vazia do GPT após tool | reasoning consumiu o budget | já mitigado (`reasoning_effort=none`); checar `LISTING_MAX_TOKENS` |
| Listagem cortada no meio | `finish_reason=length` | subir `LISTING_MAX_TOKENS` (default 700) |
| "conteúdo não disponibilizado" na voz | e-mail só-HTML não parseado | já mitigado (fallback HTML); revisar `_body_text` |
| Echo não reconhece a skill | enablement ausente / conta diferente | refazer passo 5; Echo deve estar na conta dev |

## 7. Verificação final — publicado na Amazon e instalado no Echo Dot

Rodar SEMPRE no fim do fluxo. Sucesso = os 4 checks abaixo verdes; qualquer
vermelho → diagnostics (seção 6) antes de anunciar o deploy.

```bash
# 1) CI concluiu com sucesso (deploy executado)
gh run list --workflow deploy-alexa.yml --limit 1
gh run view <run-id> --json conclusion --jq .conclusion   # esperado: "success"

# 2) Lambda atualizada: o último commit do CodeCommit é o deploy DESTE push
#    (o CI grava "deploy: <sha-do-github>")
git ls-remote https://git-codecommit.us-east-1.amazonaws.com/v1/repos/a3057584-6fe3-43ab-b326-12be9d687519 master
git log --oneline -1 origin/main   # conferir que o sha citado no CodeCommit = este push
# (credenciais do CodeCommit: ask smapi generate-credentials-for-alexa-hosted-skill ...)

# 3) Modelo + manifest publicados (build assíncrono — aguardar ~1 min)
docker exec idiota-ask ask smapi get-skill-status -s "$ASK_SKILL_ID" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
im=d.get('interactionModel',{}).get('pt-BR',{}).get('lastUpdateRequest',{}); \
mf=d.get('manifest',{}).get('lastUpdateRequest',{}); \
print('MODEL:', im.get('status'), '| MANIFEST:', mf.get('status'))"
# esperado: MODEL: SUCCEEDED | MANIFEST: SUCCEEDED (erros: null)

# 4) Skill habilitada para a conta do Echo (204/vazio = habilitado)
docker exec idiota-ask ask smapi get-skill-enablement-status -s "$ASK_SKILL_ID" -g development && echo "habilitada ✓"
```

Checklist de aceite (todos obrigatórios):

- [ ] CI `success`
- [ ] CodeCommit head = commit "deploy: <sha deste push>"
- [ ] `MODEL: SUCCEEDED` e `MANIFEST: SUCCEEDED`
- [ ] enablement ativo (HTTP 204)
- [ ] Teste de voz no Echo: **"Alexa, abre Idiota"** → saudação; funcionalidade
      nova do deploy confirmada (ex.: *"quais são meus e-mails?"* / ditado / conversa)

SMAPI **simulation** (`simulate-skill`) NÃO serve de verificação nesta conta
(retorna "An unexpected error occurred" sempre) — o teste real é o Echo.
