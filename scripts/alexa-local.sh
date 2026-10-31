#!/usr/bin/env bash
# Teste local da skill Idiota com ferramentas oficiais Amazon (ASK CLI v2 + ask-sdk-local-debug)
# Uso (a partir da raiz do projeto):
#   docker compose run --rm ask bash scripts/alexa-local.sh login    # 1a vez: login na conta Amazon
#   docker compose run --rm ask bash scripts/alexa-local.sh debug    # testa o CODIGO LOCAL (sem deploy)
#   docker compose run --rm ask bash scripts/alexa-local.sh dialog   # testa a skill DEPLOYADA
set -euo pipefail

SKILL_ID="${ASK_SKILL_ID:?ASK_SKILL_ID nao definido no compose}"
LOCALE="${ASK_DEFAULT_DEVICE_LOCALE:-pt-BR}"
INVOKER="$(python -c 'import ask_sdk_local_debug, os; print(os.path.join(os.path.dirname(ask_sdk_local_debug.__file__), "local_debugger_invoker.py"))')"

login() {
    ask configure --no-browser
    echo "Perfil salvo no volume ask-config (persiste entre execucoes)."
}

token() {
    # Gera token LWA com escopo de debug local (login no browser do HOST)
    ask util generate-lwa-tokens --scopes alexa::ask:skills:debug
}

debug() {
    local token="${ASK_LWA_TOKEN:-}"
    if [ -z "$token" ]; then
        echo "1) Gere o token:  docker compose run --rm ask bash scripts/alexa-local.sh token"
        echo "   (abra a URL no browser do host, faca login e copie o ACCESS_TOKEN)"
        read -rp "2) Cole o ACCESS_TOKEN: " token
    fi

    echo ">> Conectando codigo local ao proxy Alexa (skill ${SKILL_ID})..."
    python "$INVOKER" \
        --accessToken "$token" \
        --skillId "$SKILL_ID" \
        --skillEntryFile lambda/lambda_function.py \
        --handlerName lambda_handler \
        --region NA &
    local pid=$!
    sleep 3

    echo ">> Conversando com a skill LOCAL (digite .quit para sair):"
    ask dialog --skill-id "$SKILL_ID" --locale "$LOCALE" || true
    kill "$pid" 2>/dev/null || true
}

dialog() {
    # Conversa com a versao DEPLOYADA (Lambda), nao o codigo local
    ask dialog --skill-id "$SKILL_ID" --locale "$LOCALE"
}

case "${1:-}" in
    login)  login ;;
    token)  token ;;
    debug)  debug ;;
    dialog) dialog ;;
    *)
        echo "Uso: $0 {login|token|debug|dialog}"
        echo "  login  -> autentica a conta Amazon developer (persiste no volume)"
        echo "  token  -> gera ACCESS_TOKEN para o debug local"
        echo "  debug  -> roteia a Alexa para o CODIGO LOCAL (sem deploy)"
        echo "  dialog -> conversa com a skill DEPLOYADA"
        exit 1
        ;;
esac
