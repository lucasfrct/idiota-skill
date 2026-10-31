"""Contrato chat.py <-> cache.py — assinaturas, invocações e parâmetros.

Execução:
    docker run --rm -v "$PWD":/workspace -w /workspace/lambda idiota-ask-cli python test_chat_cache_contract.py

Garante mecanicamente (inspect + passthrough + edge cases):
  S1 assinaturas exatas de chat.py e das funções de cache que ele usa
  S2 passthrough: chat repassa exatamente (user_id) / (user_id, histórico)
  S3 parâmetros de contorno: None, vazio, unicode, trim, não-serializável
  S4 persistência warm-restart (mirror vazio, arquivo presente)
  S5 wiring do lambda_function (user_id = session.user.user_id)

Stdlib puro; exit 0 só se tudo passar.
"""
import inspect
import json
import os
import sys
import tempfile
from types import SimpleNamespace

TEST_DIR = tempfile.mkdtemp(prefix="idiota-contract-test-")
os.environ["CACHE_DIR"] = TEST_DIR

import cache  # noqa: E402
import chat   # noqa: E402

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


def params(fn):
    return list(inspect.signature(fn).parameters)


# ===========================================================================
section("S1 — assinaturas exatas (inspect)")
# ===========================================================================

def s1_chat_signatures():
    assert params(chat.load_history) == ["user_id"], params(chat.load_history)
    assert params(chat.save_history) == ["user_id", "history"], params(chat.save_history)
    assert params(chat.clear_history) == ["user_id"], params(chat.clear_history)
    assert params(chat.generate_gpt_response) == ["history", "query"]
check("c01 chat: load_history(user_id), save_history(user_id, history), clear_history(user_id)", s1_chat_signatures)

def s1_cache_signatures():
    assert params(cache.load_chat) == ["user_id"], params(cache.load_chat)
    assert params(cache.save_chat) == ["user_id", "history"], params(cache.save_chat)
    assert params(cache.clear_chat) == ["user_id"], params(cache.clear_chat)
    # tipos declarados coerentes com o contrato documentado
    ok = (list, "list", inspect.Signature.empty)
    assert inspect.signature(chat.load_history).return_annotation in ok
    assert inspect.signature(chat.save_history).return_annotation in (bool, "bool", inspect.Signature.empty)
    assert inspect.signature(chat.clear_history).return_annotation in (bool, "bool", inspect.Signature.empty)
check("c02 cache: load_chat(user_id), save_chat(user_id, history), clear_chat(user_id)", s1_cache_signatures)

def s1_chat_uses_only_public_cache_api():
    import re
    src = open("chat.py", encoding="utf-8").read()
    accesses = set(re.findall(r"cache\.([A-Za-z_][A-Za-z0-9_]*)", src))
    assert accesses <= {"load_chat", "save_chat", "clear_chat"}, accesses
    assert "_mirror" not in src and "_gen" not in src and "_email_index" not in src, \
        "chat.py acessando internals do cache"
check("c03 chat.py só usa API pública do cache (sem internals)", s1_chat_uses_only_public_cache_api)


# ===========================================================================
section("S2 — passthrough: invocação e parâmetros corretos")
# ===========================================================================

def s2_load_passthrough():
    calls = []
    orig = cache.load_chat
    cache.load_chat = lambda uid: (calls.append(uid), orig(uid))[1]
    try:
        assert chat.load_history("amzn1.user.X") == []
        assert calls == ["amzn1.user.X"], calls
    finally:
        cache.load_chat = orig
check("c04 load_history repassa user_id intacto p/ cache.load_chat", s2_load_passthrough)

def s2_save_passthrough():
    calls = []
    orig = cache.save_chat
    hist = [{"role": "user", "content": f"m{i}"} for i in range(25)]
    def spy(uid, h):
        calls.append((uid, h))
        return orig(uid, h)
    cache.save_chat = spy
    try:
        assert chat.save_history("amzn1.user.Y", hist) is True
        uid, stored = calls[0]
        assert uid == "amzn1.user.Y"
        assert len(stored) == chat.MAX_HISTORY, len(stored)      # trim p/ MAX_HISTORY
        assert stored[-1]["content"] == "m24"                     # mantém os últimos
    finally:
        cache.save_chat = orig
check("c05 save_history repassa (user_id, últimos MAX_HISTORY turnos)", s2_save_passthrough)

def s2_clear_passthrough():
    calls = []
    orig = cache.clear_chat
    cache.clear_chat = lambda uid: (calls.append(uid), orig(uid))[1]
    try:
        chat.save_history("amzn1.user.Z", [{"role": "user", "content": "x"}])
        assert chat.clear_history("amzn1.user.Z") is True
        assert calls[-1] == "amzn1.user.Z"
        assert chat.load_history("amzn1.user.Z") == []
    finally:
        cache.clear_chat = orig
check("c06 clear_history repassa user_id e apaga a memória", s2_clear_passthrough)


# ===========================================================================
section("S3 — parâmetros de contorno (fail-open)")
# ===========================================================================

def s3_none_and_empty():
    assert chat.load_history(None) == []
    assert chat.load_history("") == []
    assert chat.load_history("   ") == []
    assert chat.save_history(None, [{"role": "user", "content": "x"}]) is False
    assert chat.save_history("u", None) is False
    assert chat.save_history("u", []) is True          # vazia é estado válido
    assert chat.load_history("u") == []
check("c07 None/vazio/branco: valores seguros, sem exceção", s3_none_and_empty)

def s3_unicode_and_exact_bound():
    hist = [{"role": "user", "content": "ção 🎉 çã"} ,
            {"role": "assistant", "content": "ok"}]
    assert chat.save_history("u-uni", hist) is True
    assert chat.load_history("u-uni") == hist          # unicode round-trip exato
    exact = [{"role": "user", "content": f"e{i}"} for i in range(chat.MAX_HISTORY)]
    assert chat.save_history("u-exact", exact) is True
    assert chat.load_history("u-exact") == exact       # limite exato não corta
check("c08 unicode round-trip e limite exato de MAX_HISTORY", s3_unicode_and_exact_bound)

def s3_non_serializable():
    assert chat.save_history("u-bad", [{"role": "user", "content": {"set", 1}}]) is False
    assert chat.save_history("u-bad2", [{"role": "user", "content": object()}]) is False
    assert chat.load_history("u-bad") == []            # nada foi gravado
    assert chat.save_history("u-bad", [{"role": "user", "content": "ok agora"}]) is True
    assert chat.load_history("u-bad") == [{"role": "user", "content": "ok agora"}]
check("c09 histórico não-serializável -> False, store intacto, recupera depois", s3_non_serializable)

def s3_cache_fail_open():
    real_dir = cache.CACHE_DIR
    blocked = os.path.join(TEST_DIR, "arquivo-nao-diretorio")
    with open(blocked, "w") as f:
        f.write("x")
    try:
        cache.CACHE_DIR = blocked
        cache._mirror.clear()
        assert chat.save_history("u-fail", [{"role": "user", "content": "x"}]) is False
        assert chat.load_history("u-fail") == []
        assert chat.clear_history("u-fail") is False
    finally:
        cache.CACHE_DIR = real_dir
        cache._mirror.clear()
check("c10 cache indisponível -> chat degrada sem exceção", s3_cache_fail_open)


# ===========================================================================
section("S4 — persistência warm-restart (sandbox quente, memória vazia)")
# ===========================================================================

def s4_warm_restart():
    hist = [{"role": "user", "content": "pergunta antiga"},
            {"role": "assistant", "content": "resposta antiga"}]
    assert chat.save_history("u-thaw", hist) is True
    # simula thaw: mirror some (novo handler em outra invocação), arquivo permanece
    cache._mirror.clear()
    cache._gen.clear()
    cache._email_index.update({"gen": -1, "sent": {}, "errors": {}})
    assert chat.load_history("u-thaw") == hist
    # nova memória continua sendo gravada por cima do arquivo recuperado
    assert chat.save_history("u-thaw", hist + [{"role": "user", "content": "novo"}]) is True
    cache._mirror.clear()
    assert chat.load_history("u-thaw") == hist + [{"role": "user", "content": "novo"}]
check("c11 memória sobrevive a reinício do mirror (arquivo é a fonte)", s4_warm_restart)

def s4_isolation():
    chat.save_history("iso-a", [{"role": "user", "content": "A"}])
    chat.save_history("iso-b", [{"role": "user", "content": "B"}])
    assert chat.load_history("iso-a") == [{"role": "user", "content": "A"}]
    assert chat.load_history("iso-b") == [{"role": "user", "content": "B"}]
check("c12 isolamento por user_id na frente do chat", s4_isolation)


# ===========================================================================
section("S5 — wiring do lambda_function")
# ===========================================================================

def s5_wiring():
    src = open("lambda_function.py", encoding="utf-8").read()
    assert "chat.load_history(user_id(handler_input))" in src
    assert "chat.save_history(user_id(handler_input), history)" in src
    assert 'session.user.user_id' in src, "user_id deve vir da sessão Alexa"
    assert "session.session_id" not in src, "session_id muda por sessão — não usar como chave"
check("c13 launch restaura e GptQuery persiste com user_id da sessão", s5_wiring)

def s5_user_id_helper():
    import lambda_function as lf
    assert params(lf.user_id) == ["handler_input"]
    session = SimpleNamespace(user=SimpleNamespace(user_id="amzn1.ask.account.TEST"))
    envelope = SimpleNamespace(session=session)
    hi = SimpleNamespace(request_envelope=envelope)
    assert lf.user_id(hi) == "amzn1.ask.account.TEST"
    broken = SimpleNamespace(request_envelope=SimpleNamespace())
    assert lf.user_id(broken) == ""                      # AttributeError capturado
check("c14 helper user_id extrai da sessão e degrada p/ vazio", s5_user_id_helper)

print(f"\n{'=' * 66}")
print(f"PASS: {len(RESULTS['pass'])}  FAIL: {len(RESULTS['fail'])}")
if RESULTS["fail"]:
    print("FAILED TESTS:")
    for name in RESULTS["fail"]:
        print(f"  - {name}")
    sys.exit(1)
print("CONTRACT_TESTS_PASSED")
