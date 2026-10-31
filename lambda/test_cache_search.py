"""Testes exaustivos de BUSCA do cache.py — corretude + velocidade.

Execução:
    docker run --rm -v "$PWD":/workspace -w /workspace/lambda idiota-ask-cli python test_cache_search.py

Seções:
  S1 corretude exaustiva — ~150 combinações aleatórias de filtros comparadas
     1:1 com uma referência O(n) independente (ordem + conteúdo)
  S2 índice — invalidação em write, edição externa, corrupção, case
  S3 escalabilidade — 1k/10k/50k/100k: lookup O(1), scan O(n), combinada,
     since, registry; com limites de crescimento
  S4 piores casos — destinatário quente, zero hits, limites degenerados
  S5 concorrência — escritas e buscas simultâneas sem corromper

Stdlib puro; exit 0 só se tudo passar.
"""
import json
import os
import random
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

TEST_DIR = tempfile.mkdtemp(prefix="idiota-search-test-")
os.environ["CACHE_DIR"] = TEST_DIR

import cache  # noqa: E402

RESULTS = {"pass": [], "fail": []}
TABLE = []  # (scale, warm_avg_ms, zerohit_avg_ms, scan_avg_ms, combined_avg_ms, cold_ms, registry_ms)


def check(name, fn):
    try:
        fn()
        RESULTS["pass"].append(name)
        print(f"  PASS  {name}")
    except Exception:
        RESULTS["fail"].append(name)
        print(f"  FAIL  {name}\n{traceback_fmt()}")


def traceback_fmt():
    import traceback
    return "\n".join("        " + l for l in traceback.format_exc(limit=3).splitlines())


def section(title):
    print(f"\n=== {title} ===")


def reset_state():
    cache._mirror.clear()
    cache._gen.clear()
    cache._email_index.update({"gen": -1, "sent": {}, "errors": {}})


# ---------------------------------------------------------------------------
# Gerador determinístico de dataset
# ---------------------------------------------------------------------------

def iso(dt):
    return dt.isoformat(timespec="microseconds")


def gen_dataset(seed, n_sent, n_errors, n_recipients, n_registry):
    """Dataset conhecido — a referência deriva daqui (não do arquivo).

    Timestamps relativos a agora, espalhados numa janela de 6 dias — frescos
    para o TTL default de 7 dias (o TTL é testado em test_cache_ttl.py).
    """
    rng = random.Random(seed)
    base = datetime.now(timezone.utc) - timedelta(days=6)
    total = max(1, n_sent + n_errors)
    span_us = 6 * 24 * 3600 * 1_000_000
    recips = [f"user{i}@ex.com" for i in range(n_recipients)]

    def stamp(pos):
        return iso(base + timedelta(microseconds=(pos * span_us) // total
                                    + rng.randrange(1_000_000)))

    def mk(i, pool):
        to = recips[rng.randrange(n_recipients)]
        subject = f"{pool} assunto {rng.choice(['voo', 'hotel', ' conta', 'BOLETO'])} {i % 97}"
        body = f"corpo da mensagem {i} com token{rng.randrange(50)} e mais texto"
        pos = i if pool == "sent" else n_sent + i
        rec = {"id": i, "to": to, "subject": subject, "body": body,
               "at": stamp(pos), "info" if pool == "sent" else "error": f"r{i}"}
        return rec

    sent = [mk(i, "sent") for i in range(n_sent)]
    errors = [mk(10_000 + i, "errors") for i in range(n_errors)]
    registry = [{"email": f"reg{i}@ex.com", "first_used": iso(base),
                 "last_used": stamp(i), "count": i + 1}
                for i in range(n_registry)]
    registry.sort(key=lambda r: r["last_used"], reverse=True)  # invariante do cache
    return {"seq": n_sent + n_errors, "registry": registry, "sent": sent, "errors": errors}


# ===========================================================================
section("S1 — corretude exaustiva: cache vs referência O(n) independente")
# ===========================================================================

DATA = gen_dataset(seed=42, n_sent=3000, n_errors=800, n_recipients=120, n_registry=150)
assert cache.write_json(cache.EMAILS, DATA) is True


def reference(pool, recipient=None, contains=None, since=None, limit=20):
    limit = max(0, int(limit or 0))
    if limit == 0:
        return []
    entries = list(DATA[pool])
    if recipient:
        r = str(recipient).strip().lower()
        entries = [e for e in entries if e["to"].lower() == r]
    entries = list(reversed(entries))
    if since:
        entries = [e for e in entries if e.get("at", "") >= since]
    if contains:
        needle = str(contains).strip().lower()
        entries = [e for e in entries
                   if needle in e.get("subject", "").lower()
                   or needle in e.get("body", "").lower()]
    return [dict(e) for e in entries[:limit]]


def s1_random_combos():
    rng = random.Random(7)
    recips = [e["to"] for e in DATA["sent"]]
    tokens = ["voo", "BOLETO", "conta", "token17", "token49", "corpo da mensagem",
              "zzz-nada", "ASSUNTO 55", "5", "user"]
    mismatches = 0
    for trial in range(150):
        recipient = rng.choice([None, rng.choice(recips), rng.choice(recips).upper(),
                                "ghost@nowhere.com"])
        contains = rng.choice([None, rng.choice(tokens), f"token{rng.randrange(50)}"])
        since = rng.choice([None, DATA["sent"][rng.randrange(len(DATA['sent']))]["at"],
                            "2026-01-01T00:00:05"])
        limit = rng.choice([None, 0, 1, 3, 17, 10_000, -5])
        kw = dict(recipient=recipient, contains=contains, since=since, limit=limit)
        got = cache.list_sent_emails(**kw)
        want = reference("sent", **kw)
        if got != want:
            mismatches += 1
            if mismatches == 1:
                print(f"        [diff] kw={kw}\n        got[:2]={got[:2]}\n        want[:2]={want[:2]}")
        got_e = cache.list_error_emails(**kw)
        want_e = reference("errors", **kw)
        if got_e != want_e:
            mismatches += 1
    assert mismatches == 0, f"{mismatches} combinações divergentes (x2 pools)"
check("r01 150 combinações aleatórias × 2 pools = 0 divergências", s1_random_combos)

def s1_full_order():
    got = cache.list_sent_emails(limit=10_000)
    want = reference("sent", limit=10_000)
    assert got == want and len(got) == len(DATA["sent"])
    assert [e["id"] for e in got] == [e["id"] for e in reversed(DATA["sent"])]
check("r02 ordem exata (mais novo primeiro) no dataset inteiro", s1_full_order)

def s1_registry_properties():
    got = cache.list_available_emails(limit=10_000)
    stored = cache.read_json(cache.EMAILS)["registry"]
    emails = [r["email"] for r in stored]
    assert got == emails
    assert len({e.lower() for e in emails}) == len(emails), "duplicatas case-variantes"
    lasts = [r["last_used"] for r in stored]
    assert lasts == sorted(lasts, reverse=True), "ordem last_used desc quebrada"
    assert len(stored) == 150
check("r03 registry: sem duplicatas, ordem last_used desc, conteúdo íntegro", s1_registry_properties)

def s1_isolation():
    got = cache.list_sent_emails(limit=5)
    got[0]["subject"] = "MUTADO"
    got.pop()
    again = cache.list_sent_emails(limit=5)
    assert again[0]["subject"] != "MUTADO" and len(again) == 5
check("r04 resultados isolados (mutar retorno não afeta o store)", s1_isolation)


# ===========================================================================
section("S2 — comportamento do índice sob mutação")
# ===========================================================================

def s2_write_invalidates():
    before = len(cache.list_sent_emails(recipient="novo@ex.com"))
    cache.record_email("novo@ex.com", subject="achei", body="x", ok=True)
    after = cache.list_sent_emails(recipient="novo@ex.com")
    assert before == 0 and len(after) == 1 and after[0]["subject"] == "achei"
    assert after[0] == cache.list_sent_emails(contains="achei")[0]  # índice e scan coerentes
check("r05 write invalida índice e a busca reflete o novo registro", s2_write_invalidates)

def s2_external_edit():
    reset_state()
    path = os.path.join(cache.CACHE_DIR, "emails.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["sent"].append({"id": 999999, "to": "externo@ex.com", "subject": "externo",
                         "body": "b", "at": iso(datetime.now(timezone.utc) + timedelta(days=30)),
                         "info": ""})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    time.sleep(0.01)
    got = cache.list_sent_emails(recipient="externo@ex.com")
    assert len(got) == 1 and got[0]["id"] == 999999
check("r06 edição externa no arquivo aparece na busca (mirror+índice)", s2_external_edit)

def s2_corrupt_then_search():
    path = os.path.join(cache.CACHE_DIR, "emails.json")
    backup = open(path, encoding="utf-8").read()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("{corrompido")
        reset_state()
        assert cache.list_sent_emails() == []
        assert cache.list_available_emails() == []
        assert cache.write_json(cache.EMAILS, DATA) is True
        assert len(cache.list_sent_emails(limit=3)) == 3
    finally:
        with open(path, "w", encoding="utf-8") as f:
            f.write(backup)
check("r07 corrupção -> busca segura; recupera após reescrita", s2_corrupt_then_search)

def s2_case_insensitive():
    for probe in ["USER0@EX.COM", "User0@ex.com", "user0@ex.com", "  user0@ex.com  "]:
        got = cache.list_sent_emails(recipient=probe, limit=10_000)
        assert got and all(e["to"] == "user0@ex.com" for e in got), probe
check("r08 busca por destinatário em qualquer caixa/espaços", s2_case_insensitive)

def s2_pools_separated():
    sent_ids = {e["id"] for e in cache.list_sent_emails(limit=10_000)}
    err_ids = {e["id"] for e in cache.list_error_emails(limit=10_000)}
    assert sent_ids and err_ids and not (sent_ids & err_ids)
check("r09 pools sent/errors não se cruzam", s2_pools_separated)


# ===========================================================================
section("S3 — escalabilidade das buscas (1k → 100k)")
# ===========================================================================

def bench(scale):
    n_sent = scale
    n_errors = max(1, scale // 5)
    n_rec = max(1, scale // 10)
    ds = gen_dataset(seed=scale, n_sent=n_sent, n_errors=n_errors,
                     n_recipients=n_rec, n_registry=min(1000, n_rec))
    reset_state()
    t0 = time.perf_counter()
    cache.write_json(cache.EMAILS, ds)
    write_s = time.perf_counter() - t0

    recips = [e["to"] for e in ds["sent"]]

    t0 = time.perf_counter()  # cold: 1ª busca reconstrói o índice
    cache.list_sent_emails(recipient=recips[0], limit=10)
    cold_s = time.perf_counter() - t0

    N = 200
    t0 = time.perf_counter()
    for i in range(N):
        cache.list_sent_emails(recipient=recips[i % len(recips)], limit=100)
    warm_avg = (time.perf_counter() - t0) / N * 1000

    t0 = time.perf_counter()
    for i in range(N):
        assert cache.list_sent_emails(recipient=f"fantasma{i}@x.com") == []
    zero_avg = (time.perf_counter() - t0) / N * 1000

    M = 20
    t0 = time.perf_counter()
    for i in range(M):
        cache.list_sent_emails(contains=f"token{i % 50}", limit=50)
    scan_avg = (time.perf_counter() - t0) / M * 1000

    t0 = time.perf_counter()
    for i in range(50):
        cache.list_sent_emails(recipient=recips[i % len(recips)], contains="voo", limit=50)
    combined_avg = (time.perf_counter() - t0) / 50 * 1000

    t0 = time.perf_counter()
    for i in range(50):
        cache.list_sent_emails(since="2026-01-01T00:00:30", limit=100)
    since_avg = (time.perf_counter() - t0) / 50 * 1000

    t0 = time.perf_counter()
    for i in range(20):
        cache.list_available_emails(limit=10_000)
    registry_avg = (time.perf_counter() - t0) / 20 * 1000

    TABLE.append(dict(scale=scale, write_s=write_s, cold_ms=cold_s * 1000,
                      warm_ms=warm_avg, zero_ms=zero_avg, scan_ms=scan_avg,
                      comb_ms=combined_avg, since_ms=since_avg, reg_ms=registry_avg))
    return TABLE[-1]


B = {}
for scale in (1_000, 10_000, 50_000, 100_000):
    B[scale] = bench(scale)

print("\n  escala | write_s | cold_ms | warm_ms | zero_ms | scan_ms | comb_ms | since_ms | reg_ms")
for r in TABLE:
    print(f"  {r['scale']:>6} | {r['write_s']:>7.3f} | {r['cold_ms']:>7.2f} | {r['warm_ms']:>7.3f} | "
          f"{r['zero_ms']:>7.3f} | {r['scan_ms']:>7.2f} | {r['comb_ms']:>7.2f} | {r['since_ms']:>8.2f} | {r['reg_ms']:>6.2f}")

def s3_warm_is_constant():
    # lookup indexado não pode crescer com o dataset (O(1) + O(hits))
    ratio = B[100_000]["warm_ms"] / max(B[10_000]["warm_ms"], 0.001)
    assert ratio < 5, f"warm cresceu {ratio:.1f}x de 10k p/ 100k"
    assert B[100_000]["warm_ms"] < 5.0, f"lookup médio {B[100_000]['warm_ms']:.3f}ms > 5ms"
check("r10 lookup indexado constante: warm(100k)/warm(10k) < 5x e < 5ms", s3_warm_is_constant)

def s3_cold_bounded():
    assert B[100_000]["cold_ms"] < 2000, f"rebuild do índice com 100k: {B[100_000]['cold_ms']:.1f}ms"
check("r11 rebuild do índice (cold) com 100k < 2s", s3_cold_bounded)

def s3_scan_linear_bounded():
    ratio = B[100_000]["scan_ms"] / max(B[10_000]["scan_ms"], 0.001)
    assert ratio < 25, f"scan cresceu {ratio:.1f}x (esperado ~10x, O(n))"
    assert B[100_000]["scan_ms"] < 500, f"scan médio {B[100_000]['scan_ms']:.2f}ms > 500ms"
check("r12 scan textual O(n) e barato: razão < 25x e < 500ms em 100k", s3_scan_linear_bounded)

def s3_zerohit_fast():
    assert B[100_000]["zero_ms"] < 5.0, "zero-hit deveria ser O(1) do índice"
check("r13 zero-hit é O(1) (< 5ms) mesmo com 100k", s3_zerohit_fast)

def s3_registry_ok():
    assert B[100_000]["reg_ms"] < 50, "listar registry não pode ser lento"
check("r14 registry listagem < 50ms", s3_registry_ok)


# ===========================================================================
section("S4 — piores casos")
# ===========================================================================

def s4_hot_recipient():
    now_iso = iso(datetime.now(timezone.utc))
    hot = {"seq": 5000, "registry": [], "errors": [],
           "sent": [{"id": i, "to": "hot@ex.com", "subject": f"h{i}",
                     "body": "corpo", "at": now_iso, "info": ""}
                    for i in range(5000)]}
    cache.write_json(cache.EMAILS, hot)
    # best-of-2: a 1ª busca pós-write pode pagar GC/alocação do dataset anterior
    timings, results = [], []
    for _ in range(2):
        t0 = time.perf_counter()
        results.append(cache.list_sent_emails(recipient="hot@ex.com", limit=10))
        timings.append(time.perf_counter() - t0)
    dt = min(timings)
    got = results[-1]
    assert len(got) == 10 and got[0]["id"] == 4999
    t0 = time.perf_counter()
    got_all = cache.list_sent_emails(recipient="hot@ex.com", limit=10_000)
    dt_all = time.perf_counter() - t0
    assert len(got_all) == 5000
    assert dt < 0.05 and dt_all < 0.5, f"hot: {dt:.4f}s / all {dt_all:.4f}s"
check("r15 destinatário quente (1 bucket, 5k hits) com limit baixo e total", s4_hot_recipient)

def s4_since_boundary():
    ds = gen_dataset(seed=99, n_sent=500, n_errors=50, n_recipients=20, n_registry=20)
    cache.write_json(cache.EMAILS, ds)
    global DATA
    DATA = ds  # referência passa a descrever o store atual
    since = ds["sent"][250]["at"]  # corte no ponto médio real dos timestamps
    got = cache.list_sent_emails(since=since, limit=10_000)
    assert got, "dataset deve ter registros após o corte"
    assert all(e["at"] >= since for e in got)
    ref = reference("sent", since=since, limit=10_000)
    assert got == ref
    assert cache.list_error_emails(since=since, limit=10_000) == \
        reference("errors", since=since, limit=10_000)
    # inclusivo: registro exatamente no corte entra
    assert any(e["at"] == since for e in got)
check("r17 filtro since inclusivo e idêntico à referência", s4_since_boundary)

def s4_degenerate_limits():
    assert cache.list_sent_emails(limit=0) == []
    assert cache.list_sent_emails(limit=-7) == []
    assert cache.list_sent_emails(limit=None) == []
    assert cache.list_sent_emails(recipient="", contains="") == cache.list_sent_emails()
    one = cache.list_sent_emails(limit=1)
    assert len(one) == 1
check("r16 limites degenerados (0, negativo, None, vazio) estáveis", s4_degenerate_limits)


# ===========================================================================
section("S5 — concorrência: buscas durante escritas")
# ===========================================================================

def s5_threads():
    reset_state()
    base = {"seq": 0, "registry": [], "sent": [], "errors": []}
    cache.write_json(cache.EMAILS, base)
    errors = []
    stop = threading.Event()

    def writer():
        try:
            for i in range(300):
                ok = cache.record_email(f"t{i % 40}@ex.com", subject=f"s{i}",
                                        body=f"b{i}", ok=(i % 10 != 0), info="x")
                assert ok, f"record_email falhou em i={i}"
        except Exception as e:  # noqa: BLE001
            errors.append(("writer", e))

    def reader(rid):
        try:
            while not stop.is_set():
                got = cache.list_sent_emails(recipient=f"t{rid}@ex.com", limit=50)
                for e in got:
                    assert {"id", "to", "subject", "body", "at"} <= set(e), e
                cache.list_sent_emails(contains="b1", limit=10)
                cache.list_available_emails(limit=10)
                cache.list_error_emails(limit=10)
        except Exception as e:  # noqa: BLE001
            errors.append((f"reader{rid}", e))

    threads = [threading.Thread(target=writer)] + \
              [threading.Thread(target=reader, args=(i,)) for i in range(4)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    threads[0].join()
    stop.set()
    for t in threads[1:]:
        t.join()
    dt = time.perf_counter() - t0

    assert not errors, f"exceções em threads: {errors}"
    data = cache.read_json(cache.EMAILS)
    assert data is not None and json.dumps(data)  # JSON íntegro
    assert len(data["sent"]) == 270 and len(data["errors"]) == 30
    assert len(data["registry"]) == 40
    print(f"        [conc] 300 writes + buscas em 4 readers: {dt:.2f}s, 0 exceções")
check("r18 1 writer + 4 readers simultâneos: 0 exceções, store íntegro", s5_threads)

def s5_final_state():
    final = cache.list_sent_emails(limit=10_000)
    assert len(final) == 270
    newest = final[0]
    assert newest["subject"] == "s299"
    errs = cache.list_error_emails(limit=10_000)
    assert len(errs) == 30
    # seq global: registro do loop i tem id i+1; erro quando i%10==0 => ids ≡ 1 (mod 10)
    assert {e["id"] for e in errs} == {i + 1 for i in range(300) if i % 10 == 0}
    assert all("error" in e and "info" not in e for e in errs)
    sent_ids = {e["id"] for e in final}
    assert not (sent_ids & {e["id"] for e in errs})
check("r19 estado final consistente pós-concorrência", s5_final_state)


# ===========================================================================
print(f"\n{'=' * 66}")
print(f"PASS: {len(RESULTS['pass'])}  FAIL: {len(RESULTS['fail'])}")
if RESULTS["fail"]:
    print("FAILED TESTS:")
    for name in RESULTS["fail"]:
        print(f"  - {name}")
    sys.exit(1)
print("ALL_SEARCH_TESTS_PASSED")
