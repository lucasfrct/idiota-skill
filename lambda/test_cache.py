"""Testes exaustivos da mecânica de cache (cache.py) + integração nas duas frentes.

Execução (imagem com as dependências reais):
    docker run --rm -v "$PWD":/workspace -w /workspace/lambda idiota-ask-cli python test_cache.py

Puro stdlib — sem pytest. Cada seção imprime PASS/FAIL; exit code 0 só se tudo passar.
"""
import json
import os
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace

# --- S0: ambiente ANTES do import — cache em diretório isolado ---
TEST_DIR = tempfile.mkdtemp(prefix="idiota-cache-test-")
os.environ["CACHE_DIR"] = TEST_DIR
os.environ["CACHE_MAX_CHAT_TURNS"] = "15"
os.environ["CACHE_MAX_USERS"] = "6"

import cache  # noqa: E402
import chat   # noqa: E402

RESULTS = {"pass": [], "fail": []}


def check(name, fn):
    try:
        fn()
        RESULTS["pass"].append(name)
        print(f"  PASS  {name}")
    except Exception:
        RESULTS["fail"].append(name)
        print(f"  FAIL  {name}\n{traceback.format_exc(limit=3)}")


def section(title):
    print(f"\n=== {title} ===")


def _reset_cache_state(new_dir=None):
    cache._mirror.clear()
    cache._gen.clear()
    cache._email_index.update({"gen": -1, "sent": {}, "errors": {}})
    if new_dir:
        cache.CACHE_DIR = new_dir


# ===========================================================================
section("S1 — núcleo: leitura, escrita atômica, corrupção, mirror")
# ===========================================================================

def s1_missing():
    assert cache.read_json("inexistente", default={"a": 1}) == {"a": 1}
check("r01 read de arquivo ausente -> default", s1_missing)

def s1_roundtrip():
    data = {"k": "válido ç ã", "n": [1, 2, {"x": None}], "b": True}
    assert cache.write_json("t1", data) is True
    assert cache.read_json("t1") == data
check("r02 write/read roundtrip (unicode, aninhado)", s1_roundtrip)

def s1_overwrite():
    cache.write_json("t1", {"v": 1})
    cache.write_json("t1", {"v": 2})
    assert cache.read_json("t1") == {"v": 2}
check("r03 sobrescrita", s1_overwrite)

def s1_atomic():
    cache.write_json("t1", {"v": 3})
    leftovers = [f for f in os.listdir(cache.CACHE_DIR) if f.endswith(".tmp")]
    assert leftovers == [], leftovers
    with open(os.path.join(cache.CACHE_DIR, "t1.json"), encoding="utf-8") as f:
        assert json.load(f) == {"v": 3}
check("r04 escrita atômica: sem .tmp órfão, JSON válido no disco", s1_atomic)

def s1_corrupt():
    p = os.path.join(cache.CACHE_DIR, "corrupt.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{json quebrado!!!")
    assert cache.read_json("corrupt", default=[]) == []
    assert cache.write_json("corrupt", {"rec": 1}) is True  # recuperação
    assert cache.read_json("corrupt") == {"rec": 1}
check("r05 arquivo corrompido -> default + recupera na próxima escrita", s1_corrupt)

def s1_external_edit():
    cache.write_json("t2", {"v": 1})
    p = os.path.join(cache.CACHE_DIR, "t2.json")
    with open(p, "w", encoding="utf-8") as f:  # edição externa (mtime muda)
        json.dump({"v": 99}, f)
    time.sleep(0.01)
    assert cache.read_json("t2") == {"v": 99}
check("r06 edição externa detectada via mtime (mirror recarrega)", s1_external_edit)

def s1_bad_name():
    assert cache.read_json("../etc/passwd", default="D") == "D"
    assert cache.write_json("a/b", {}) is False
check("r07 nome inválido rejeitado (sem path traversal)", s1_bad_name)


# ===========================================================================
section("S2 — frente 1: memória de turnos (chats por usuário)")
# ===========================================================================

def s2_roundtrip():
    hist = [{"role": "user", "content": "oi"}, {"role": "assistant", "content": "olá"}]
    assert cache.save_chat("userA", hist) is True
    assert cache.load_chat("userA") == hist
check("r08 save/load de memória de turnos", s2_roundtrip)

def s2_bound():
    turns = [{"role": "user", "content": f"m{i}"} for i in range(60)]
    cache.save_chat("userB", turns)
    stored = cache.load_chat("userB")
    assert len(stored) == cache.MAX_CHAT_TURNS, len(stored)
    assert stored[-1]["content"] == "m59"  # mantém os ÚLTIMOS
check("r09 limite de turnos (mantém os últimos)", s2_bound)

def s2_isolation():
    assert cache.load_chat("userA") != cache.load_chat("userB")
    cache.save_chat("userC", [{"role": "user", "content": "só do C"}])
    assert cache.load_chat("userA") == [{"role": "user", "content": "oi"},
                                        {"role": "assistant", "content": "olá"}]
check("r10 isolamento por usuário", s2_isolation)

def s2_clear():
    cache.clear_chat("userC")
    assert cache.load_chat("userC") == []
check("r11 clear_chat", s2_clear)

def s2_eviction():
    real_now = cache._now
    counter = iter(range(1000))
    cache._now = lambda: f"t{next(counter):04d}"  # timestamps crescentes determinísticos
    try:
        for i in range(7):
            cache.save_chat(f"ev-u{i}", [{"role": "user", "content": str(i)}])
        data = cache.read_json(cache.CHATS)
        assert len(data) == cache.MAX_USERS, len(data)
        assert "ev-u0" not in data, "mais antigo deveria ser evictado"
        assert "ev-u6" in data
    finally:
        cache._now = real_now
check("r12 eviction de usuários antigos (MAX_USERS)", s2_eviction)


# ===========================================================================
section("S3 — frente 2: e-mails enviados, disponíveis e com erro")
# ===========================================================================

def s3_record_ok():
    assert cache.record_email("dest1@ex.com", subject="Assunto 1", body="Corpo um",
                              ok=True, info="E-mail enviado com sucesso para dest1@ex.com") is True
    sent = cache.list_sent_emails()
    assert len(sent) == 1 and sent[0]["to"] == "dest1@ex.com"
    assert sent[0]["subject"] == "Assunto 1" and sent[0]["body"] == "Corpo um"
    assert sent[0]["info"].startswith("E-mail enviado")
    assert "id" in sent[0] and sent[0]["at"].startswith("20")
check("r13 registro de enviado com metadados", s3_record_ok)

def s3_record_error():
    assert cache.record_email("dest2@ex.com", subject="Falhou", body="corpo erro",
                              ok=False, info="Autenticação SMTP falhou") is True
    errors = cache.list_error_emails()
    assert len(errors) == 1 and errors[0]["error"] == "Autenticação SMTP falhou"
    assert cache.list_sent_emails() == [cache.list_sent_emails()[0]]  # sent intacto
check("r14 registro de erro separado dos enviados", s3_record_error)

def s3_registry():
    cache.record_email("DEST1@EX.COM", ok=True)          # mesmo e-mail, caixa diferente
    cache.record_email("dest3@ex.com", ok=True)
    reg = cache.list_available_emails()
    assert reg == ["dest3@ex.com", "dest1@ex.com", "dest2@ex.com"], reg  # mais recente primeiro
    data = cache.read_json(cache.EMAILS)
    entry = next(r for r in data["registry"] if r["email"] == "dest1@ex.com")
    assert entry["count"] == 2, entry  # mesclou maiúsc/minúsculas
check("r15 lista de e-mails disponíveis (todo novo vai p/ lista, dedup case-insensitive)", s3_registry)

def s3_filter_recipient():
    for i in range(4):
        cache.record_email("dest1@ex.com", subject=f"rec {i}", body=f"b {i}", ok=True)
    got = cache.list_sent_emails(recipient="DEST1@EX.COM")  # índice O(1), case-insensitive
    assert len(got) == 6, len(got)  # r13 + r15 (DEST1) + 4 novos
    assert all(e["to"] == "dest1@ex.com" for e in got)
    assert got[0]["subject"] == "rec 3"  # mais novo primeiro
check("r16 busca filtrada por destinatário (índice, case-insensitive)", s3_filter_recipient)

def s3_filter_contains_limit():
    got = cache.list_sent_emails(contains="rec 2")
    assert len(got) == 1 and got[0]["subject"] == "rec 2"
    got = cache.list_sent_emails(contains="CORPO UM")  # acha em body, case-insensitive
    assert len(got) == 1 and got[0]["to"] == "dest1@ex.com"
    assert len(cache.list_sent_emails(limit=2)) == 2
    assert cache.list_sent_emails(limit=0) == []
check("r17 busca filtrada por texto (assunto+corpo) + limite", s3_filter_contains_limit)

def s3_filter_combined():
    got = cache.list_sent_emails(recipient="dest1@ex.com", contains="rec 1")
    assert len(got) == 1 and got[0]["subject"] == "rec 1"
    assert cache.list_sent_emails(recipient="dest2@ex.com", contains="rec 1") == []
check("r18 busca combinada destinatário + texto", s3_filter_combined)

def s3_errors_filter():
    cache.record_email("dest2@ex.com", subject="e2", ok=False, info="boom 2")
    got = cache.list_error_emails(recipient="dest2@ex.com")
    assert len(got) == 2 and got[0]["subject"] == "e2"
    assert cache.list_error_emails(recipient="dest1@ex.com") == []
check("r19 lista de erros com filtro por destinatário", s3_errors_filter)

def s3_body_trunc():
    cache.record_email("big@ex.com", subject="s", body="x" * 9999, ok=True)
    rec = cache.list_sent_emails(recipient="big@ex.com")[0]
    assert len(rec["body"]) == cache.BODY_MAX
check("r20 truncamento de corpo (limita tamanho do arquivo)", s3_body_trunc)


# ===========================================================================
section("S4 — busca otimizada: índice + mirror (performance)")
# ===========================================================================

def s4_perf():
    big = {"seq": 20000, "registry": [], "errors": [],
           "sent": [{"id": i, "to": f"u{i % 500}@ex.com", "subject": f"s{i}",
                     "body": f"corpo {i}", "at": "2026-01-01", "info": ""}
                    for i in range(20000)]}
    t0 = time.perf_counter()
    assert cache.write_json(cache.EMAILS, big) is True
    write_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    total = 0
    for i in range(300):  # 300 buscas por destinatário (caminho do índice)
        total += len(cache.list_sent_emails(recipient=f"u{i % 500}@ex.com", limit=100))
    lookup_s = time.perf_counter() - t0
    assert total == 300 * 40, total

    t0 = time.perf_counter()
    hits = cache.list_sent_emails(contains="corpo 19999", limit=5)  # varredura em memória
    scan_s = time.perf_counter() - t0
    assert len(hits) == 1 and hits[0]["id"] == 19999

    assert lookup_s < 1.0, f"300 lookups demoraram {lookup_s:.3f}s"
    assert scan_s < 1.0, f"scan demorou {scan_s:.3f}s"
    print(f"        [perf] write 20k={write_s:.3f}s | 300 lookups={lookup_s:.3f}s | scan={scan_s:.3f}s")
check("r21 20k registros: 300 buscas por destinatário + scan em <1s cada", s4_perf)


# ===========================================================================
section("S5 — fail-open: cache nunca derruba a skill")
# ===========================================================================

def s5_failopen():
    real_dir = cache.CACHE_DIR
    blocked = os.path.join(TEST_DIR, "not-a-dir")
    with open(blocked, "w") as f:
        f.write("x")  # caminho existe mas é arquivo -> makedirs/mkdir falha
    try:
        _reset_cache_state(blocked)
        assert cache.save_chat("u", [{"role": "user", "content": "x"}]) is False
        assert cache.record_email("a@b.c", ok=True) is False
        assert cache.load_chat("u") == []
        assert cache.list_sent_emails() == []
        assert cache.list_available_emails() == []
        assert cache.read_json("x", default="D") == "D"
        assert cache.write_json("x", {}) is False
    finally:
        _reset_cache_state(real_dir)
check("r22 diretório inválido -> valores seguros, sem exceção", s5_failopen)

def s5_recover():
    assert cache.write_json("pos-fail", {"ok": 1}) is True
    assert cache.read_json("pos-fail") == {"ok": 1}
check("r23 recuperação após falha (estado limpo)", s5_recover)


# ===========================================================================
section("S6 — integração frente 1: chat.py (memória de turnos)")
# ===========================================================================

def s6_chat_memory():
    hist = [{"role": "user", "content": f"q{i}"} for i in range(25)]
    assert chat.save_history("voice-user-1", hist) is True
    got = chat.load_history("voice-user-1")
    assert len(got) == chat.MAX_HISTORY, len(got)   # 10 (MAX_HISTORY do chat)
    assert got[-1]["content"] == "q24"
    assert chat.load_history("outro-usuario") == []
check("r24 chat.save_history/load_history com limite do chat", s6_chat_memory)

def s6_chat_no_ask_sdk():
    src = open("chat.py", encoding="utf-8").read()
    assert "ask_sdk" not in src
    assert "cache.load_chat" in src and "cache.save_chat" in src
check("r25 chat.py puro de Alexa e usando o cache", s6_chat_no_ask_sdk)


# ===========================================================================
section("S7 — integração frente 2: fluxo de e-mail do lambda_function")
# ===========================================================================

def s7_send_ok():
    import lambda_function as lf
    _reset_cache_state(os.path.join(TEST_DIR, "s7-ok"))  # store fresco
    class AM:
        session_attributes = {"email_flow": {"subject": "Relatório", "body": "Linha 1"}}
    hi = SimpleNamespace(attributes_manager=AM())
    lf.send_email = lambda *a, **k: {"ok": True, "data": "E-mail enviado com sucesso para x@y.com"}
    spoken = lf.send_pending_email(hi)
    assert spoken == "E-mail enviado com sucesso para x@y.com", spoken  # fala = texto, não dict
    sent = cache.list_sent_emails()
    assert len(sent) == 1 and sent[0]["subject"] == "Relatório" and sent[0]["body"] == "Linha 1"
    assert cache.list_available_emails() == [lf.EMAIL_RECIPIENT.lower()]
check("r26 envio OK: fala correta + metadado gravado + e-mail na lista", s7_send_ok)

def s7_send_error():
    import lambda_function as lf
    _reset_cache_state(os.path.join(TEST_DIR, "s7-err"))  # store fresco
    class AM:
        session_attributes = {"email_flow": {"subject": "S", "body": "B"}}
    hi = SimpleNamespace(attributes_manager=AM())
    lf.send_email = lambda *a, **k: {"ok": False, "error": "Autenticação SMTP falhou"}
    spoken = lf.send_pending_email(hi)
    assert spoken == "Autenticação SMTP falhou", spoken
    errs = cache.list_error_emails()
    assert len(errs) == 1 and errs[0]["error"] == "Autenticação SMTP falhou"
    assert cache.list_sent_emails() == []
    assert len(cache.list_available_emails()) == 1  # tentativa também registra o e-mail
check("r27 envio com erro: fala do erro + registro em errors", s7_send_error)

def s7_wiring():
    src = open("lambda_function.py", encoding="utf-8").read()
    assert "chat.load_history(user_id(handler_input))" in src   # Launch restaura memória
    assert "chat.save_history(user_id(handler_input), history)" in src  # GptQuery persiste
    assert "cache.record_email(" in src                          # e-mail registra no cache
check("r28 wiring das duas frentes presente no lambda_function", s7_wiring)

def s7_lambda_import():
    import lambda_function as lf
    assert callable(lf.lambda_handler)
check("r29 import integral do lambda_function (deps reais)", s7_lambda_import)


# ===========================================================================
# Veredito
# ===========================================================================
print(f"\n{'=' * 60}")
print(f"PASS: {len(RESULTS['pass'])}  FAIL: {len(RESULTS['fail'])}")
if RESULTS["fail"]:
    print("FAILED TESTS:")
    for name in RESULTS["fail"]:
        print(f"  - {name}")
    sys.exit(1)
print("ALL_TESTS_PASSED")
