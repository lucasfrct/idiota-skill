#!/usr/bin/env python3
"""REPL local de teste para a skill Idiota (sem Alexa, sem Lambda).

Roda dentro do container do docker-compose.yaml, invocando o handler da
lambda_function.py diretamente com requisicoes Alexa sintetizadas.

Uso:
    docker exec -it -w /workspace/lambda idiota-ask python3 /workspace/.deploy/test_local.py
    docker exec -w /workspace/lambda -i idiota-ask python3 /workspace/.deploy/test_local.py --smoke

Comandos do REPL: abrir | ajuda | email: <texto> | sair | pergunta livre
Modo --smoke: executa 3 turnos automaticos (launch + GPT + memoria de sessao).
"""
import json
import sys

sys.path.insert(0, "/workspace/lambda")  # roda dentro do container do docker-compose

import lambda_function as lf

SKILL_ID = "amzn1.ask.skill.a3057584-6fe3-43ab-b326-12be9d687519"
SESSION_ID = "amzn1.echo-api.session.local-repl"


def event(itype, attrs, extra=None):
    return {
        "version": "1.0",
        "session": {
            "new": True, "sessionId": SESSION_ID,
            "application": {"applicationId": SKILL_ID},
            "user": {"userId": "amzn1.ask.account.local"},
            "attributes": attrs or {},
        },
        "context": {"System": {
            "application": {"applicationId": SKILL_ID},
            "user": {"userId": "amzn1.ask.account.local"},
            "device": {"deviceId": "amzn1.ask.device.local"}}},
        "request": {"type": itype, "requestId": "amzn1.echo-api.request.local-repl",
                    "timestamp": "2026-01-01T00:00:00Z", "locale": "pt-BR",
                    **(extra or {})},
    }


def intent(name, slots=None):
    return {"intent": {"name": name, "confirmationStatus": "NONE",
            "slots": {k: {"name": k, "value": v, "confirmationStatus": "NONE"}
                      for k, v in (slots or {}).items()}}}


def call(attrs, itype, extra=None):
    resp = lf.lambda_handler(event(itype, attrs, extra), None)
    out = ((resp.get("response") or {}).get("outputSpeech") or {}).get("ssml", "")
    end = (resp.get("response") or {}).get("shouldEndSession", True)
    return out.replace("<speak>", "").replace("</speak>", "").strip(), \
        resp.get("sessionAttributes", {}), end


def say(text):
    print(f"\n  IDIOTA > {text}\n")


def smoke():
    print("== SMOKE: 1) launch  2) GPT  3) memoria de sessao ==")
    s, attrs, _ = call({}, "LaunchRequest")
    say(s)
    s, attrs, _ = call(attrs, "IntentRequest", intent("GptQueryIntent", {"query": "quem foi Santos Dumont"}))
    say(s)
    s, _, _ = call(attrs, "IntentRequest", intent("GptQueryIntent", {"query": "e o 14 Bis?"}))
    say(s)


def repl():
    attrs, opened = {}, False
    print("REPL local Idiota — pergunte à vontade | abrir (reinicia) | ajuda | email: <texto> | sair")
    s, attrs, _ = call({}, "LaunchRequest")  # abre a sessão sozinho, como "Alexa, abrir idiota"
    opened = True
    say(s)
    while True:
        try:
            line = input("VOCÊ   > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        if low in ("exit", "quit", "/q"):
            break
        if low == "abrir":
            s, attrs, _ = call({}, "LaunchRequest")
            opened = True
            say(s)
            continue
        if not opened:
            print("  (diga 'abrir' para iniciar a sessão, como no Echo)")
            continue
        if low in ("sair", "parar"):
            s, attrs, end = call(attrs, "IntentRequest", intent("AMAZON.StopIntent"))
            say(s)
            opened = False
            continue
        if low in ("ajuda", "help"):
            s, attrs, _ = call(attrs, "IntentRequest", intent("AMAZON.HelpIntent"))
            say(s)
            continue
        if low.startswith("email:"):
            s, attrs, _ = call(attrs, "IntentRequest", intent("SendEmailIntent", {"message": line[6:].strip()}))
            say(s)
            continue
        if low in ("emails", "e-mails"):
            import mail
            r = mail.list_emails(5)
            print(mail.format_email_blocks(r["data"]) if r.get("ok") else r.get("error"))
            continue
        if low.startswith("enderecos"):
            import mail
            term = line[9:].strip() or None
            r = mail.list_addresses(term)
            print("\n".join(f"• {a}" for a in r["data"]) if r.get("ok") else r.get("error"))
            continue
        s, attrs, end = call(attrs, "IntentRequest", intent("GptQueryIntent", {"query": line}))
        say(s)


if __name__ == "__main__":
    smoke() if "--smoke" in sys.argv else repl()
