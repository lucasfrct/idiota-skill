"""Garantias de envio de e-mail: exigências (título/corpo/remetente), sugestão
de título no fim do fluxo, e chat gerando + enviando (GPT e SMTP reais).

Execução:
    docker run --rm -v "$PWD":/workspace -w /workspace/lambda idiota-ask-cli python test_mail_flow.py

S1 contrato do mail.py (SMTP mockado — sem rede)
S2 fluxo Alexa: título sugerido quando o corpo já existe (unidade)
S3 chat gera e envia (GPT REAL + SMTP REAL — envia 1 e-mail de teste p/ si)
S4 fallback e nomes (sem rede)

Stdlib + deps do projeto; exit 0 só se tudo passar.
"""
import os
import re
import sys
import tempfile
from email import message_from_string
from email.header import decode_header, make_header
from types import SimpleNamespace


def dec(value):
    """Decodifica header MIME (ex.: =?utf-8?q?Relat=C3=B3rio?= -> Relatório)."""
    return str(make_header(decode_header(value or "")))

TEST_DIR = tempfile.mkdtemp(prefix="idiota-mail-test-")
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


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendmail(self, f, t, msg):
        self.sent.append((f, t, msg))


def headers(msg_str):
    m = message_from_string(msg_str)
    return dec(m.get("From", "")), dec(m.get("To", "")), dec(m.get("Subject", "")), body_of(msg_str)


def body_of(msg_str):
    m = message_from_string(msg_str)
    for part in m.walk():
        if part.get_content_type() == "text/plain":
            return part.get_payload(decode=True).decode("utf-8", "replace")
    return ""


# ===========================================================================
section("S1 — contrato: título, corpo e remetente são exigência")
# ===========================================================================

def m01_body_required():
    r = mail.send_email(subject="título", body="   ", from_email="remet@ex.com")
    assert r["ok"] is False and "Corpo" in r["error"], r
check("m01 corpo ausente -> erro explícito", m01_body_required)

def m02_subject_required():
    for bad in ("", None, "   "):
        r = mail.send_email(subject=bad, body="corpo", from_email="remet@ex.com")
        assert r["ok"] is False and "Assunto" in r["error"], (bad, r)
check("m02 título ausente -> erro (sem default silencioso)", m02_subject_required)

def m03_sender_required():
    real = os.environ.pop("EMAIL_SENDER", None)
    try:
        r = mail.send_email(subject="t", body="b", from_email=None)
        assert r["ok"] is False and "Remetente" in r["error"], r
        r = mail.send_email(subject="t", body="b", from_email="sem-arroba")
        assert r["ok"] is False and "Remetente" in r["error"], r
    finally:
        if real:
            os.environ["EMAIL_SENDER"] = real
check("m03 remetente ausente/inválido -> erro", m03_sender_required)

def m04_valid_send_mocked():
    fake = FakeSMTP()
    orig = mail._connect_smtp
    mail._connect_smtp = lambda: fake
    try:
        r = mail.send_email(subject="Relatório", body="linha um\nlinha dois",
                            from_email="remet@ex.com", to_email="dest@ex.com")
        assert r["ok"] is True and "dest@ex.com" in r["data"], r
        assert len(fake.sent) == 1
        frm, to, msg = fake.sent[0]
        f, t, s, body = headers(msg)
        assert (f, t, s) == ("remet@ex.com", "dest@ex.com", "Relatório"), (f, t, s)
        assert "linha um" in body and "linha dois" in body
    finally:
        mail._connect_smtp = orig
check("m04 envio válido (SMTP mockado): cabeçalhos e corpo corretos", m04_valid_send_mocked)

def m05_recipient_fallback():
    fake = FakeSMTP()
    orig_conn, real_rec = mail._connect_smtp, os.environ.get("EMAIL_RECIPIENT")
    mail._connect_smtp = lambda: fake
    os.environ["EMAIL_RECIPIENT"] = "fixo@ex.com"
    try:
        mail.send_email(subject="t", body="b", from_email="r@ex.com")  # to None -> env
        assert fake.sent[-1][1] == ["fixo@ex.com"]
        os.environ.pop("EMAIL_RECIPIENT", None)
        mail.send_email(subject="t", body="b", from_email="r@ex.com")  # to None -> remetente
        assert fake.sent[-1][1] == ["r@ex.com"]
    finally:
        mail._connect_smtp = orig_conn
        if real_rec is not None:
            os.environ["EMAIL_RECIPIENT"] = real_rec
check("m05 destinatário: env -> remetente (cadeia de fallback)", m05_recipient_fallback)

def m06_suggest_subject():
    s = mail.suggest_subject
    assert s("Reunião mudou para amanhã. Depois avisar o time.") == "Reunião mudou para amanhã"
    assert s("oi!") == "oi!"                                   # frase curta -> texto todo
    out = s("palavra " * 30)                                   # corta em limite de palavra
    assert out.endswith("…") and len(out) <= 49 and not out.endswith(" …")
    assert s("  espaços\n\ntab\t  ok. fim") == "espaços tab ok"  # normalização
    assert s("") == "" and s(None) == ""
check("m06 suggest_subject: 1ª frase, curta, corte, normalização, vazio", m06_suggest_subject)


# ===========================================================================
section("S2 — título sugerido no fim do fluxo (corpo já existe)")
# ===========================================================================

class FakeAM:
    def __init__(self):
        self.session_attributes = {}

class FakeBuilder:
    def __init__(self):
        self.spoken = ""
    def speak(self, s):
        self.spoken = s
        return self
    def ask(self, s):
        return self
    @property
    def response(self):
        return "RESP"

def s2_confirmation_suggests():
    import lambda_function as lf
    am, fb = FakeAM(), FakeBuilder()
    hi = SimpleNamespace(attributes_manager=am, response_builder=fb)
    lf.ask_email_confirmation(hi, "", "lembrete: pagar a conta de luz amanhã")
    flow = am.session_attributes["email_flow"]
    assert flow["subject"] == lf.suggest_subject(flow["body"]), flow
    assert flow["subject"] == "lembrete: pagar a conta de luz amanhã"
    assert flow["subject"] in fb.spoken, "sugestão deve ser falada na confirmação"
    # usuário disse assunto -> prevalece, sem sugestão
    lf.ask_email_confirmation(hi, "Assunto Dito", "corpo")
    assert am.session_attributes["email_flow"]["subject"] == "Assunto Dito"
check("s1 confirmação sugere o título do corpo e o fala", s2_confirmation_suggests)

def s2_send_pending_kwargs():
    import lambda_function as lf
    captured = {}
    lf.send_email = lambda **kw: captured.update(kw) or {"ok": True, "data": "ok enviado"}
    am = FakeAM()
    am.session_attributes["email_flow"] = {"subject": "", "body": "corpo pendente aqui"}
    hi = SimpleNamespace(attributes_manager=am)
    spoken = lf.send_pending_email(hi)
    assert spoken == "ok enviado"
    assert captured["subject"] == "corpo pendente aqui"      # sugerido no envio
    assert set(captured) == {"subject", "body", "from_email", "to_email"}
check("s2 send_pending_email usa keywords e sugere título faltante", s2_send_pending_kwargs)


# ===========================================================================
section("S3 — chat gera e envia (GPT REAL + SMTP REAL: 1 e-mail de teste)")
# ===========================================================================

def have_creds():
    return bool(os.getenv("OPENAI_API_KEY")) and bool(os.getenv("EMAIL_SENDER")) \
        and bool(os.getenv("EMAIL_PASSWORD")) and "@" in (os.getenv("EMAIL_RECIPIENT") or "@")

def c01_generate_email_live():
    assert have_creds(), "credenciais ausentes (OPENAI_API_KEY/SMTP) — não é possível garantir o envio"
    draft = chat.generate_email(
        "avise o Lucas que a reunião de projeto mudou para amanhã às 15 horas na sala 3")
    assert draft and draft["subject"] and draft["body"], draft
    assert len(draft["subject"].split()) <= 12, draft["subject"]
    assert len(draft["body"]) >= 10
    print(f"        [gpt] assunto={draft['subject']!r} corpo={draft['body'][:60]!r}…")
check("c01 generate_email (GPT real): assunto curto + corpo válido", c01_generate_email_live)

def c02_send_generated_live():
    r = chat.send_composed_email(
        "[TESTE AUTOMATICO] avise a mim mesmo que o fluxo de e-mail da skill Idiota "
        "esta funcionando e pode gerar e enviar e-mails com titulo sugerido")
    assert r.get("ok") is True, r
    print(f"        [smtp] {r['data']}")
check("c02 send_generated_email (SMTP REAL): e-mail de teste enviado", c02_send_generated_live)


# ===========================================================================
section("S4 — fallback e nomes públicos (sem rede)")
# ===========================================================================

def c03_fallback_suggestion():
    fake = FakeSMTP()
    orig_conn, orig_gen = mail._connect_smtp, chat.generate_email
    mail._connect_smtp = lambda: fake
    chat.generate_email = lambda m: None  # força o caminho SEM GPT
    try:
        msg = "lembrete rápido: levar o notebook para o conserto hoje"
        r = chat.send_composed_email(msg)
        assert r["ok"] is True, r
        frm, to, subj, body = headers(fake.sent[0][2])
        assert subj == mail.suggest_subject(msg), subj
        assert subj.startswith("lembrete rápido") and subj.endswith("…")
        assert msg in body, "fallback usa a mensagem literal como corpo"
    finally:
        mail._connect_smtp = orig_conn
        chat.generate_email = orig_gen
check("c04 sem GPT: fallback usa corpo literal + título sugerido", c03_fallback_suggestion)

def c04_empty_message():
    r = chat.send_composed_email("   ")
    assert r["ok"] is False and "vazia" in r["error"].lower(), r
check("c05 mensagem vazia -> erro, sem chamar GPT/SMTP", c04_empty_message)

def c05_public_mail_api():
    src = open("chat.py", encoding="utf-8").read()
    accesses = set(re.findall(r"mail\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", src))
    assert accesses <= {"send_email", "suggest_subject", "list_emails",
                        "filter_emails", "list_addresses"}, accesses
check("c06 chat.py só chama API pública do mail.py", c05_public_mail_api)


print(f"\n{'=' * 66}")
print(f"PASS: {len(RESULTS['pass'])}  FAIL: {len(RESULTS['fail'])}")
if RESULTS["fail"]:
    print("FAILED TESTS:")
    for name in RESULTS["fail"]:
        print(f"  - {name}")
    sys.exit(1)
print("MAIL_FLOW_TESTS_PASSED")
