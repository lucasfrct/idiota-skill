#!/usr/bin/env bash
# Deploy da skill Idiota para a Alexa-hosted a partir do CI.
# Fluxo: clona o repo hospedado (CodeCommit, auth via helper ASK CLI) ->
# sincroniza o codigo da skill -> escreve lambda/.env a partir dos Secrets ->
# commit + push (dispara o CodeBuild da Amazon) -> modelo e manifest via SMAPI.
# O segredo NUNCA vai para o repo publico: vive so nos Secrets e no repo
# privado hospedado da Amazon.
set -euo pipefail

: "${ASK_SKILL_ID:?ASK_SKILL_ID nao definido}"
: "${GITHUB_WORKSPACE:?Fora do CI}"
command -v ask >/dev/null 2>&1 || { echo "ERRO: ASK CLI ausente"; exit 1; }

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

# --- 2. Auth do repo hospedado (CodeCommit) via helper do ASK CLI ---
SKILL_UUID="${ASK_SKILL_ID#amzn1.ask.skill.}"
CODECOMMIT_URL="https://git-codecommit.us-east-1.amazonaws.com/v1/repos/${SKILL_UUID}"

mkdir -p ~/.ask/scripts
install -m 755 "${GITHUB_WORKSPACE}/scripts/git-credential-helper" ~/.ask/scripts/git-credential-helper
install -m 644 "${GITHUB_WORKSPACE}/scripts/ask-auth-info" ~/.ask/auth_info
git config --global "credential.${CODECOMMIT_URL}.helper" "!$HOME/.ask/scripts/git-credential-helper default $ASK_SKILL_ID"
git config --global "credential.${CODECOMMIT_URL}.useHttpPath" true
echo "Helper de credenciais instalado (credenciais de curta duracao via SMAPI)"

# --- 3. Clonar o repo hospedado e sincronizar o codigo da skill ---
rm -rf /tmp/hosted-deploy
git clone --depth 1 "$CODECOMMIT_URL" /tmp/hosted-deploy 2>/dev/null
cd /tmp/hosted-deploy
BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "Repo hospedado clonado - branch de deploy: $BRANCH"

rsync -a --delete \
    --exclude '__pycache__/' --exclude '.venv/' --exclude '.env' \
    "${GITHUB_WORKSPACE}/lambda/" lambda/
rsync -a --delete --exclude '__pycache__/' \
    "${GITHUB_WORKSPACE}/interactionModels/" skill-package/interactionModels/ 2>/dev/null || true
cp "${GITHUB_WORKSPACE}/skill.json" skill-package/skill.json 2>/dev/null || \
    cp "${GITHUB_WORKSPACE}/skill.json" skill.json

# .env do CI entra no pacote da Lambda (repo privado da Amazon)
cp "${GITHUB_WORKSPACE}/lambda/.env" lambda/.env
chmod 600 lambda/.env

git config user.email "ci@users.noreply.github.com"
git config user.name "alexa-deploy-ci"
git add -A
git commit -q -m "deploy: ${GITHUB_SHA::7} (codigo + envs)" --allow-empty
git push origin "HEAD:$BRANCH"
echo "Codigo + envs publicados no repo hospedado (CodeBuild da Amazon disparado)"

# --- 4. Modelo de interacao (pt-BR) via SMAPI ---
ask smapi update-interaction-model \
    --skill-id "$ASK_SKILL_ID" \
    --stage development \
    --locale pt-BR \
    --file "${GITHUB_WORKSPACE}/interactionModels/custom/pt-BR.json" \
    --profile default
echo "Interaction model atualizado"

ask smapi build-model \
    --skill-id "$ASK_SKILL_ID" \
    --stage development \
    --locale pt-BR \
    --profile default
echo "Build do modelo disparado (acompanhe o status no console)"

# --- 5. Manifest (skill.json) via SMAPI ---
ask smapi update-manifest \
    --skill-id "$ASK_SKILL_ID" \
    --stage development \
    --file "${GITHUB_WORKSPACE}/skill.json" \
    --profile default
echo "Manifest atualizado"

echo "DEPLOY_CONCLUIDO: codigo + envs + modelo + manifest"
