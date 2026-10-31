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

# --- 2. Conectar o remote hospedado da skill (idempotente; nome varia) ---
if ! git remote | grep -q .; then
    echo "ERRO: nenhum remote git no checkout"; exit 1
fi
if ! git remote -v | grep -q "amazonaws.com"; then
    ask init --hosted-skill-id "$ASK_SKILL_ID" --profile default < /dev/null
fi
REMOTE=$(for r in $(git remote); do
    git remote get-url "$r" 2>/dev/null | grep -q "amazonaws.com" && { echo "$r"; break; }
done)
: "${REMOTE:=$(git remote | head -1)}"
echo "Remote hospedado: $REMOTE ($(git remote get-url "$REMOTE" | sed 's/@.*/@***/'))"

# --- 3. Detectar branch padrao do repo hospedado ---
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
