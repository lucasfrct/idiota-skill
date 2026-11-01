"""Garantias de leitura de e-mail ESPECÍFICO pelo chat — data, título, remetente.

Execução:
    docker run --rm -v "$PWD":/workspace -w /workspace/lambda idiota-ask-cli python test_email_read.py

S1 mail.find_email — parse de datas + filtros (IMAP mockado, sem rede)
S2 chat: tool read_email registrada + payload correto no _tool_payload
S3 REAL: find_email na caixa real (o fixture se auto-envia se necessário)
S4 REAL ponta a ponta: usuário pede -> GPT chama read_email -> lê o corpo

Stdlib + deps do projeto; exit 0 só se tudo passar.
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

TEST_DIR = tempfile.mkdtemp(prefix="idiota-read-test-")
os.environ["CACHE_DIR"] = TEST_DIR

import dotenv   # noqa: E402
import mail     # noqa: E402
import chat     # noqa: E402

dotenv.load_dotenv(".env")

RESULTS = {"pass": [], "fail": []}


def check(name, fn):
    try:
        fn()
        RESULTS["pass"].append(name)
        print(f"  PASS  {name}")
    except Exception:
        import traceback
        RESULTS["fail"].append(name)
        print(f"  FAIL  {name}")
        print("\n".join("        " + l for l in traceback.format_exc(limit=3).splitlines()))


def section(title):
    print(f"\n=== {title} ===")


def msg(uid, frm, subject, days_ago, body=""):
    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    hdr = at.strftime("%a, %d %b %Y %H:%M:%S +0000")
    return {"uid": uid, "from": frm, "subject": subject, "date": hdr, "body": body}


INBOX = [
    msg("10", "Boleto Cap <cobranca@banco.com>", "Boleto outubro vence dia 10", 0,
        "Seu boleto vence dia 10. Valor R$ 250,00."),
    msg("9", "Lucas <lucasfrct@gmail.com>", "[TESTE AUTOMATICO] fluxo de e-mail", 0,
        "e-mail de teste automatico da skill Idiota com titulo sugerido"),
    msg("8", "Ana Souza <ana@empresa.com>", "Reunião de projeto mudou", 1,
        "A reunião foi remarcada para amanhã às 15h na sala 3."),
    msg("7", "Newsletter <news@tech.com>", "Resumo semanal tech", 9,
        "As 10 notícias da semana."),
]


def patch_inbox():
    orig = mail._fetch_messages
    mail._fetch_messages = lambda criteria, limit, with_body: [dict(m) for m in INBOX]
    return orig


# ===========================================================================
section("S1 — mail.find_email: datas e filtros (IMAP mockado)")
# ===========================================================================

def r01_parse_day():
    p = mail._parse_day
    today = datetime.now(timezone.utc).date()
    assert p("hoje") == (today.isoformat(), False)
    assert p("HOJE") == (today.isoformat(), False)
    assert p("ontem") == ((today - timedelta(days=1)).isoformat(), False)
    assert p("anteontem") == ((today - timedelta(days=2)).isoformat(), False)
    assert p("2026-02-07") == ("2026-02-07", False)
    assert p("07/02/2026") == ("2026-02-07", False)
    assert p("7-2-2026") == ("2026-02-07", False)
    assert p("oct") == ("oct", True)
    assert p("") == (None, False) and p(None) == (None, False)
check("r01 _parse_day: hoje/ontem/anteontem/ISO/BR/substring", r01_parse_day)

def r02_day_key():
    assert mail._day_key("Fri, 31 Oct 2026 17:42:10 +0000") == "2026-10-31"
    assert mail._day_key("lixo!!!") == ""
    assert mail._day_key(None) == ""
check("r02 _day_key extrai o dia do cabeçalho", r02_day_key)

def r03_requires_criteria():
    r = mail.find_email()
    assert r["ok"] is False and "critério" in r["error"].lower(), r
check("r03 sem critério -> erro explícito", r03_requires_criteria)

def r04_by_sender():
    orig = patch_inbox()
    try:
        r = mail.find_email(sender="ana@empresa.com")
        assert r["ok"] and len(r["data"]) == 1
        assert r["data"][0]["uid"] == "8" and "15h" in r["data"][0]["body"]
        r = mail.find_email(sender="banco.com")
        assert r["ok"] and r["data"][0]["uid"] == "10"
    finally:
        mail._fetch_messages = orig
check("r04 leitura por REMETENTE (com corpo completo)", r04_by_sender)

def r05_by_subject_and_date():
    orig = patch_inbox()
    try:
        r = mail.find_email(subject="boleto")
        assert [m["uid"] for m in r["data"]] == ["10"]
        r = mail.find_email(date="hoje")
        assert sorted(m["uid"] for m in r["data"]) == ["10", "9"]
        r = mail.find_email(date="ontem")
        assert [m["uid"] for m in r["data"]] == ["8"]
        r = mail.find_email(date=(datetime.now(timezone.utc) - timedelta(days=9)).strftime("%d/%m/%Y"))
        assert [m["uid"] for m in r["data"]] == ["7"]
    finally:
        mail._fetch_messages = orig
check("r05 leitura por TÍTULO e por DATA (hoje/ontem/DD/MM/AAAA)", r05_by_subject_and_date)

def r06_combined_and_term():
    orig = patch_inbox()
    try:
        r = mail.find_email(subject="reunião", date="ontem")
        assert [m["uid"] for m in r["data"]] == ["8"], r
        r = mail.find_email(term="boleto")            # termo no assunto
        assert [m["uid"] for m in r["data"]] == ["10"]
        r = mail.find_email(term="250,00")            # termo no corpo
        assert [m["uid"] for m in r["data"]] == ["10"]
        r = mail.find_email(subject="reunião", date="hoje")
        assert r["data"] == []
    finally:
        mail._fetch_messages = orig
check("r06 critérios combinados e termo no corpo", r06_combined_and_term)

def r07_limit_and_missing():
    orig = patch_inbox()
    try:
        r = mail.find_email(date="hoje", limit=1)
        assert len(r["data"]) == 1
        r = mail.find_email(sender="ninguem@x.com")
        assert r["ok"] and r["data"] == []
    finally:
        mail._fetch_messages = orig
check("r07 limite respeitado; sem correspondência -> lista vazia", r07_limit_and_missing)


# ===========================================================================
section("S2 — chat: tool read_email registrada e payload correto")
# ===========================================================================

def c01_tool_registered():
    tools = {t["function"]["name"] for t in chat.EMAIL_TOOLS}
    assert "read_email" in tools, tools
    props = next(t for t in chat.EMAIL_TOOLS
                 if t["function"]["name"] == "read_email")["function"]["parameters"]["properties"]
    assert {"sender", "subject", "term", "date"} <= set(props)
    assert "read_email" in chat.system_instructions()["content"]
check("c01 tool read_email no EMAIL_TOOLS e no prompt de sistema", c01_tool_registered)

def c02_payload_shape():
    orig = patch_inbox()
    try:
        payload = chat._tool_payload("read_email", json.dumps({"subject": "boleto"}))
        assert payload["total"] == 1
        item = payload["emails"][0]
        assert item["assunto"] == "Boleto outubro vence dia 10"
        assert "250,00" in item["corpo_completo"]
        assert {"remetente", "assunto", "data", "corpo_completo"} <= set(item)
        err = chat._tool_payload("read_email", json.dumps({}))
        assert "erro" in err
        bad = chat._tool_payload("read_email", "{json quebrado")
        assert "erro" in bad
    finally:
        mail._fetch_messages = orig
check("c02 _tool_payload retorna corpo completo e trata erros", c02_payload_shape)


# ===========================================================================
section("S3 — REAL: find_email na caixa verdadeira (fixture auto-adaptativo)")
# ===========================================================================

def newest_real_email():
    """E-mail mais recente da caixa COM corpo — critérios derivados dele mesmo."""
    r = mail.list_emails(10, with_body=True)
    assert r["ok"] and r["data"], "caixa vazia ou IMAP indisponível"
    with_body = [m for m in r["data"] if (m.get("body") or "").strip()] or r["data"]
    return with_body[0]


def sender_addr(from_hdr):
    m = re.search(r"<([^>]+)>", from_hdr or "")
    return (m.group(1) if m else from_hdr or "").strip().lower()


def c03_live_find():
    newest = newest_real_email()
    day = mail._day_key(newest["date"])
    tokens = [w for w in re.findall(r"[^\W\d_]{5,}", newest["subject"], re.UNICODE)]
    assert tokens, f"assunto sem token utilizável: {newest['subject']!r}"
    token = tokens[0]

    r = mail.find_email(subject=token, date=day, limit=5)
    assert r["ok"] and r["data"], (token, day, r)
    hit = next((m for m in r["data"] if m["uid"] == newest["uid"]), None)
    assert hit, f"e-mail mais recente ({newest['uid']}) não veio na busca por {token!r}/{day}"
    assert (hit.get("body") or "").strip() and hit["body"] == newest.get("body", hit["body"]), \
        "corpo completo obrigatório na leitura"

    r2 = mail.find_email(sender=sender_addr(newest["from"]), date=day, limit=5)
    assert any(m["uid"] == newest["uid"] for m in r2["data"]), r2

    r3 = mail.find_email(subject=token, date="01/01/1999", limit=5)
    assert r3["ok"] and all(m["uid"] != newest["uid"] for m in r3["data"])
    print(f"        [imap] lido: uid {newest['uid']} | {newest['subject'][:40]!r} "
          f"| {day} | {len(newest.get('body', ''))} chars")
check("c03 find_email REAL: assunto+data, remetente+data, data errada exclui", c03_live_find)


# ===========================================================================
section("S4 — REAL ponta a ponta: pedido de voz -> GPT -> read_email")
# ===========================================================================

def c04_e2e_read():
    newest = newest_real_email()
    day = mail._day_key(newest["date"])
    tokens = [w for w in re.findall(r"[^\W\d_]{5,}", newest["subject"], re.UNICODE)]
    token = tokens[0]
    date_phrase = "hoje" if day == datetime.now(timezone.utc).date().isoformat() else day

    calls = []
    orig = mail.find_email
    mail.find_email = lambda **kw: (calls.append(kw), orig(**kw))[1]
    try:
        history = []
        reply = chat.generate_gpt_response(
            history, f"leia o e-mail com assunto {token} de {date_phrase}")
    finally:
        mail.find_email = orig

    assert calls, "GPT não chamou a ferramenta read_email"
    assert reply and len(reply) > 20, reply
    assert "houve um problema" not in reply.lower(), reply
    assert "não entendi" not in reply.lower(), reply
    print(f"        [voz] {reply[:140]!r}")
check("c04 chat lê o e-mail específico de verdade (GPT chamou read_email)", c04_e2e_read)


print(f"\n{'=' * 66}")
print(f"PASS: {len(RESULTS['pass'])}  FAIL: {len(RESULTS['fail'])}")
if RESULTS["fail"]:
    print("FAILED TESTS:")
    for name in RESULTS["fail"]:
        print(f"  - {name}")
    sys.exit(1)
print("EMAIL_READ_TESTS_PASSED")
