"""Testes do TTL de 7 dias no cache.py — expiração, fronteira, purga e env.

Execução:
    docker run --rm -v "$PWD":/workspace -w /workspace/lambda idiota-ask-cli python test_cache_ttl.py

Stdlib puro; exit 0 só se tudo passar.
"""
import importlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

TEST_DIR = tempfile.mkdtemp(prefix="idiota-ttl-test-")
os.environ["CACHE_DIR"] = TEST_DIR

import cache  # noqa: E402

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


def reset_state():
    cache._mirror.clear()
    cache._gen.clear()
    cache._email_index.update({"gen": -1, "sent": {}, "errors": {}})


def iso(dt):
    return dt.isoformat(timespec="microseconds")


def days_ago(days, extra_us=0):
    dt = datetime.now(timezone.utc) - timedelta(days=days, microseconds=extra_us)
    return iso(dt)


def reload_ttl(days):
    os.environ["CACHE_TTL_DAYS"] = str(days)
    importlib.reload(cache)
    reset_state()


# ===========================================================================
section("S1 — TTL default e chat (memória de turnos)")
# ===========================================================================

def t01_default():
    assert cache.TTL_DAYS == 7, cache.TTL_DAYS
    c = cache._cutoff()
    assert c, "cutoff não deveria ser vazio com TTL ativo"
    # cutoff = agora - 7 dias (tolerância de 5s entre chamadas)
    delta = datetime.fromisoformat(c) - (datetime.now(timezone.utc) - timedelta(days=7))
    assert abs(delta.total_seconds()) < 5, delta
check("t01 TTL_DAYS default = 7 e cutoff correto", t01_default)

def t02_chat_fresh_vs_expired():
    fresh = [{"role": "user", "content": "oi"}]
    cache.write_json(cache.CHATS, {
        "u-fresh": {"history": fresh, "updated": days_ago(2)},
        "u-old": {"history": [{"role": "user", "content": "antigo"}], "updated": days_ago(8)},
    })
    reset_state()
    assert cache.load_chat("u-fresh") == fresh
    assert cache.load_chat("u-old") == [], "memória com 8 dias deveria expirar"
check("t02 load_chat: 2 dias mantém, 8 dias expira", t02_chat_fresh_vs_expired)

def t03_boundary():
    fixed = "2026-01-08T00:00:00.000000+00:00"
    real = cache._cutoff
    cache._cutoff = lambda: fixed
    try:
        one_us_before = "2026-01-07T23:59:59.999999+00:00"
        cache.write_json(cache.CHATS, {
            "u-on-cutoff": {"history": [{"role": "user", "content": "exatamente no corte"}],
                            "updated": fixed},
            "u-just-before": {"history": [{"role": "user", "content": "1µs antes"}],
                              "updated": one_us_before},
        })
        reset_state()
        assert cache.load_chat("u-on-cutoff") != [], "no corte exato ainda vale (ts >= cutoff)"
        assert cache.load_chat("u-just-before") == [], "1µs antes do corte já expirou"
    finally:
        cache._cutoff = real
        reset_state()
check("t03 fronteira: ts == cutoff mantém; 1µs antes expira", t03_boundary)

def t04_purge_on_save():
    cache.write_json(cache.CHATS, {
        "u-keep": {"history": [{"role": "user", "content": "recente"}], "updated": days_ago(1)},
        "u-old": {"history": [{"role": "user", "content": "velho"}], "updated": days_ago(9)},
    })
    reset_state()
    assert cache.save_chat("u-new", [{"role": "user", "content": "novo"}]) is True
    data = cache.read_json(cache.CHATS)
    assert "u-old" not in data, "expirado deveria ser purgado do arquivo na escrita"
    assert "u-keep" in data and "u-new" in data
check("t04 save_chat purga usuários expirados do arquivo", t04_purge_on_save)

def t04b_reactivate():
    cache.write_json(cache.CHATS, {
        "u-back": {"history": [{"role": "user", "content": "muito antigo"}], "updated": days_ago(30)},
    })
    reset_state()
    assert cache.load_chat("u-back") == []
    assert cache.save_chat("u-back", [{"role": "user", "content": "voltou"}]) is True
    assert cache.load_chat("u-back") == [{"role": "user", "content": "voltou"}]
check("t04b usuário expirado reativa ao conversar de novo", t04b_reactivate)


# ===========================================================================
section("S2 — TTL nos e-mails (enviados, erros, registry)")
# ===========================================================================

def t05_emails_filtered():
    cache.write_json(cache.EMAILS, {
        "seq": 4,
        "registry": [
            {"email": "velho@ex.com", "first_used": days_ago(20), "last_used": days_ago(10), "count": 1},
            {"email": "novo@ex.com", "first_used": days_ago(2), "last_used": days_ago(1), "count": 3},
        ],
        "sent": [
            {"id": 1, "to": "novo@ex.com", "subject": "recente", "body": "b", "at": days_ago(2), "info": ""},
            {"id": 2, "to": "velho@ex.com", "subject": "antigo", "body": "b", "at": days_ago(10), "info": ""},
        ],
        "errors": [
            {"id": 3, "to": "novo@ex.com", "subject": "erro recente", "body": "b", "at": days_ago(3), "error": "x"},
            {"id": 4, "to": "velho@ex.com", "subject": "erro antigo", "body": "b", "at": days_ago(15), "error": "x"},
        ],
    })
    reset_state()
    sent = cache.list_sent_emails()
    assert [e["subject"] for e in sent] == ["recente"], sent
    errs = cache.list_error_emails()
    assert [e["subject"] for e in errs] == ["erro recente"], errs
    assert cache.list_available_emails() == ["novo@ex.com"]
    # índice (recipient) e texto (contains) também respeitam TTL
    assert cache.list_sent_emails(recipient="velho@ex.com") == []
    assert cache.list_sent_emails(contains="antigo") == []
check("t05 listas excluem expirados (scan, índice, texto, registry)", t05_emails_filtered)

def t06_purge_on_record():
    assert cache.record_email("novo2@ex.com", subject="hoje", body="b", ok=True) is True
    data = cache.read_json(cache.EMAILS)
    assert all(e["subject"] != "antigo" for e in data["sent"]), "expirado deveria sair do arquivo"
    assert all(e["subject"] != "erro antigo" for e in data["errors"])
    assert all(r["email"] != "velho@ex.com" for r in data["registry"])
    assert any(e["subject"] == "recente" for e in data["sent"]), "frescos permanecem"
check("t06 record_email purga expirados do arquivo (enxuto)", t06_purge_on_record)

def t07_ttl_wins_over_since():
    # expirado mesmo com at >= since continua fora; fresco filtrado por since sai
    got = cache.list_sent_emails(since="2000-01-01", limit=10_000)
    assert [e["subject"] for e in got] == ["hoje", "recente"]
    got = cache.list_sent_emails(since="2999-01-01", limit=10_000)
    assert got == []
check("t07 TTL tem precedência sobre since (nos dois sentidos)", t07_ttl_wins_over_since)

def t08_unparseable_kept():
    data = cache.read_json(cache.EMAILS)
    data["sent"].append({"id": 999, "to": "weird@ex.com", "subject": "sem data legível",
                         "body": "b", "at": "não-é-data", "info": ""})
    cache.write_json(cache.EMAILS, data)
    reset_state()
    got = cache.list_sent_emails(recipient="weird@ex.com")
    assert len(got) == 1, "timestamp ilegível nunca expira (não destruir dado)"
check("t08 timestamp ilegível é preservado", t08_unparseable_kept)


# ===========================================================================
section("S3 — TTL configurável por env")
# ===========================================================================

def t09_disabled():
    reload_ttl(0)
    assert cache.TTL_DAYS == 0 and cache._cutoff() == ""
    cache.write_json(cache.EMAILS, {
        "seq": 1, "registry": [], "errors": [],
        "sent": [{"id": 1, "to": "a@b.c", "subject": "muito antigo", "body": "b",
                  "at": days_ago(365), "info": ""}],
    })
    got = cache.list_sent_emails()
    assert [e["subject"] for e in got] == ["muito antigo"], "TTL=0 desativa expiração"
check("t09 CACHE_TTL_DAYS=0 desativa o TTL", t09_disabled)

def t10_one_day():
    reload_ttl(1)
    assert cache.TTL_DAYS == 1
    cache.write_json(cache.EMAILS, {
        "seq": 2, "registry": [], "errors": [],
        "sent": [
            {"id": 1, "to": "a@b.c", "subject": "12h", "body": "b", "at": days_ago(0.5), "info": ""},
            {"id": 2, "to": "a@b.c", "subject": "2d", "body": "b", "at": days_ago(2), "info": ""},
        ],
    })
    got = cache.list_sent_emails()
    assert [e["subject"] for e in got] == ["12h"], got
check("t10 CACHE_TTL_DAYS=1: 12h mantém, 2 dias expira", t10_one_day)

try:
    reload_ttl(7)  # restaura o default para os demais testes/processos
except Exception:
    pass

# ===========================================================================
print(f"\n{'=' * 66}")
print(f"PASS: {len(RESULTS['pass'])}  FAIL: {len(RESULTS['fail'])}")
if RESULTS["fail"]:
    print("FAILED TESTS:")
    for name in RESULTS["fail"]:
        print(f"  - {name}")
    sys.exit(1)
print("TTL_TESTS_PASSED")
