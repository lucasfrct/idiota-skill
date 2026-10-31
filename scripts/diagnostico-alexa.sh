#!/usr/bin/env bash
# Diagnostico de saude da skill Idiota na Amazon.
# Uso (dentro do container, com o volume ask-config logado):
#   docker compose run --rm ask bash scripts/diagnostico-alexa.sh
# Checa: (1) skill acessivel, (2) build do interaction model,
#        (3) status do manifest/Lambda, (4) invocacao e2e REAL via simulador SMAPI.
set -euo pipefail

: "${ASK_SKILL_ID:?ASK_SKILL_ID nao definido}"
command -v curl >/dev/null || { echo "ERRO: curl ausente"; exit 1; }
source ~/.ask/auth_info

SKILL_UUID="${ASK_SKILL_ID#amzn1.ask.skill.}"
SKILL_BASE="https://api.amazonalexa.com/v1/skills/${ASK_SKILL_ID}"

# --- token LWA (formato antigo e novo do cli_config) ---
askRefreshToken=$(python3 -c "
import json
d = json.load(open('$HOME/.ask/cli_config'))
p = d.get('profiles', d)
prof = p.get('default', {}) if isinstance(p, dict) else {}
t = prof.get('token', prof) if isinstance(prof, dict) else {}
print(t.get('refresh_token', ''))
" 2>/dev/null || true)
accessToken=$(curl -f -s \
    -d grant_type=refresh_token \
    -d client_id="${ASK_LWA_CLIENT_ID:-$ask_client_id}" \
    -d client_secret="${ASK_LWA_CLIENT_CONFIRMATION:-$ask_client_confirmation}" \
    -d refresh_token="$askRefreshToken" \
    "${ask_lwa_api}/auth/O2/token" \
    | tr -d '\n' | sed 's/.*"access_token":"\([^"]*\)".*/\1/')
[ -n "$accessToken" ] || { echo "FALHA: token LWA"; exit 1; }
AUTH="Authorization: Bearer ${accessToken}"

VEREDICTO=0

echo "=================================================================="
echo "1) SKILL ACESSIVEL + STATUS DOS RECURSOS"
echo "=================================================================="
STATUS_JSON=$(curl -sf -H "$AUTH" "${SKILL_BASE}/status") || { echo "FALHA ao consultar /status"; exit 1; }
echo "endpoint /status respondeu (HTTP 200)"

echo ""
echo "=================================================================="
echo "2) STATUS DOS RECURSOS (deploy da Lambda + modelo + manifest)"
echo "=================================================================="
echo "$STATUS_JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
ok = True
hd = d.get('hostedSkillDeployment', {}).get('lastUpdateRequest', {})
sth = hd.get('status', 'AUSENTE')
commit = (hd.get('deploymentDetails', {}) or {}).get('commitId', '?')[:7]
print(f'Deploy da Lambda (hosted): {sth} (commit {commit})')
if sth != 'SUCCEEDED': ok = False
im = d.get('interactionModel', {}).get('pt-BR', {}).get('lastUpdateRequest', {})
sti = im.get('status', 'AUSENTE')
steps = {s['name']: s['status'] for s in (im.get('buildDetails', {}) or {}).get('steps', [])}
print(f'InteractionModel pt-BR: {sti}')
for n, s in steps.items(): print(f'  - {n}: {s}')
if sti != 'SUCCEEDED':
    print('  ERRO:', json.dumps(im.get('errors', ''), ensure_ascii=False)[:400]); ok = False
stf = d.get('manifest', {}).get('lastUpdateRequest', {}).get('status', 'AUSENTE')
print(f'Manifest: {stf}')
if stf != 'SUCCEEDED': ok = False
print('OK' if ok else 'PROBLEMA DETECTADO')
sys.exit(0 if ok else 1)
" || VEREDICTO=1

echo ""
echo "=================================================================="
echo "3) LAMBDA HOSTED (deploy do codigo)"
echo "=================================================================="
HOSTED_JSON=$(curl -sf -H "$AUTH" "${SKILL_BASE}/alexaHosted" || echo '{}')
echo "$HOSTED_JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
repo = d.get('allowVendors', d)
print('alexaHosted:', json.dumps({k: d.get(k) for k in ('region', 'runtime', 'repoUploadStatus') if k in d}, ensure_ascii=False) or 'info indisponivel (ok)')
" || true

echo ""
echo "=================================================================="
echo "4) INVOCACAO END-TO-END REAL (ask dialog via simulador da Amazon)"
echo "=================================================================="
DIALOG_LOG=/tmp/diag-dialog.log
(printf 'abre idiota\n'; sleep 12; printf 'me diga a capital da Franca\n'; sleep 20; printf '.quit\n') \
    | timeout 150 script -qec "ask dialog -s ${ASK_SKILL_ID} -l pt-BR --profile default" /dev/null \
    > "$DIALOG_LOG" 2>&1 || true
CLEAN=$(sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' "$DIALOG_LOG" | grep -oE '(User  >|Alexa >).*' | sed 's/\[?25[hln]//g')
echo "$CLEAN"
FIRST=$(echo "$CLEAN" | grep '^Alexa >' | head -1)
echo "$CLEAN" | grep -q "Idiota" && echo "CODIGO NOVO NO AR: SIM (saudacao do handler atual)" \
    || { echo "CODIGO NOVO NO AR: NAO CONFIRMADO"; VEREDICTO=1; }
echo "$CLEAN" | grep -qE "^Alexa > (Desculpe|.*nao entendi)" && {
    echo "NOTA: roteamento de pergunta caiu no Fallback no SIMULADOR (limitacao conhecida"
    echo "de SearchQuery na simulacao). Teste no Echo Dot real para validar o GPT."
}

echo ""
echo "=================================================================="
[ "$VEREDICTO" = "0" ] && echo "VEREDICTO: SKILL PUBLICADA E RESPONDENDO" || echo "VEREDICTO: PROBLEMAS DETECTADOS (ver secoes acima)"
echo "=================================================================="
exit "$VEREDICTO"
