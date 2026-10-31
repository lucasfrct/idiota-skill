#!/usr/bin/env bash
# Deploy da skill Idiota para a Alexa-hosted a partir do CI.
# Fluxo: conecta o remote hospedado (Amazon) -> publica lambda/.env a partir
# de variaveis de ambiente (Secrets) -> push do codigo -> modelo + manifest via SMAPI.
# O segredo NUNCA vai para o repo publico: lambda/.env e gitignored; o force-add
# acontece apenas no checkout efemero do CI, apontando para o remote hospedado.
set -euo pipefail

: "${ASK_SKILL_ID:?ASK_SKILL_ID nao definido}"
command -v ask >/dev/null 2>&1 || { echo "ERRO: ASK CLI ausente"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "ERRO: git ausente"; exit 1; }

# --- 1. Escrever lambda/.env exclusivamente a partir do ambiente (Secrets) ---
: "${OPENAI_API_KEY:?OPENAI_API_KEY nao definido nos Secrets}"
cat > lambda/.env <<EOF
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_MODEL=${OPENAI_MODEL:-gpt-5.6-luna}
MAX_HISTORY=${MAX_HISTORY:-10}
MAX_TOKENS=${MAX_TOKENS:-300}
EMAIL_HOST=${EMAIL_HOST:-smtp.gmail.com}
EMAIL_PORT=${EMAIL_PORT:-587}
EMAIL_SENDER=${EMAIL_SENDER:-}
EMAIL_RECIPIENT=${EMAIL_RECIPIENT:-${EMAIL_SENDER:-}}
EMAIL_PASSWORD=${EMAIL_PASSWORD:-}
EMAIL_SUBJECT=${EMAIL_SUBJECT:-Mensagem do Idiota}
EOF
chmod 600 lambda/.env
echo "lambda/.env gerado a partir dos Secrets (nunca versionado no repo publico)"

# --- 2. Remote hospedado (CodeCommit) + helper de credenciais do ASK CLI ---
SKILL_UUID="${ASK_SKILL_ID#amzn1.ask.skill.}"
CODECOMMIT_URL="https://git-codecommit.us-east-1.amazonaws.com/v1/repos/${SKILL_UUID}"

mkdir -p ~/.ask/scripts
install -m 755 scripts/git-credential-helper ~/.ask/scripts/git-credential-helper
install -m 644 scripts/ask-auth-info ~/.ask/auth_info

git remote remove hosted 2>/dev/null || true
git remote add hosted "$CODECOMMIT_URL"
git config "credential.${CODECOMMIT_URL}.helper" "!$HOME/.ask/scripts/git-credential-helper default $ASK_SKILL_ID"
git config "credential.${CODECOMMIT_URL}.useHttpPath" true
REMOTE=hosted
echo "Remote hospedado: $REMOTE -> ${CODECOMMIT_URL%/repos/*}/repos/***"

# --- 3. Detectar branch padrao do repo hospedado (tambem valida o auth) ---
HOSTED_BRANCH=$(git ls-remote --symref "$REMOTE" HEAD | awk '/^ref:/ {sub("refs/heads/", "", $2); print $2}')
: "${HOSTED_BRANCH:=master}"
echo "Branch padrao do repo hospedado: $HOSTED_BRANCH"

# --- 4. Publicar codigo + .env no remote hospedado ---
git config user.email "ci@users.noreply.github.com"
git config user.name "alexa-deploy-ci"
git add -f lambda/.env
git commit -m "ci: sync envs para deploy hospedado" --allow-empty -q
git push "$REMOTE" "HEAD:$HOSTED_BRANCH"
echo "Codigo publicado no repo hospedado da Alexa"

# --- 5. Modelo de interacao (pt-BR) via SMAPI ---
ask smapi update-interaction-model \
    --skill-id "$ASK_SKILL_ID" \
    --stage development \
    --locale pt-BR \
    --file "interactionModels/custom/pt-BR.json" \
    --profile default
echo "Interaction model atualizado"

ask smapi build-model \
    --skill-id "$ASK_SKILL_ID" \
    --stage development \
    --locale pt-BR \
    --profile default
echo "Build do modelo disparado (acompanhe o status no console)"

# --- 6. Manifest (skill.json) via SMAPI ---
ask smapi update-manifest \
    --skill-id "$ASK_SKILL_ID" \
    --stage development \
    --file "skill.json" \
    --profile default
echo "Manifest atualizado"

echo "DEPLOY_CONCLUIDO: codigo + envs + modelo + manifest"
