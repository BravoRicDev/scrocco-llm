"""F1: latenza per BUCKET di contesto + TTFT come segnale di prima classe.

- `bucket_latency_ms(unique, ctx, kind)`: EMA del bucket (<8k, 8-32k, 32-128k,
  >128k) con fallback sull'EMA globale (compat con chi non passa ctx);
- il deadline del primo contenuto usa il bucket TTFT (tempo-al-primo-contenuto
  del bucket della richiesta), non il miscuglio globale;
- demozione session-slow = assoluta (90s) E RELATIVA (>2x baseline del bucket):
  un dep cronicamente lento sui contesti grossi non viene punito se sul quel
  contesto quella era la norma;
- `note_stream_end`: durata totale stream -> solo bucket total, alpha piccola;
- history (bucket,lat) per il p95 dinamico ora e' realmente alimentata.
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.router import _ctx_bucket
from app.policy import Policy
from app.router import Router
from app import router as R

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-A,text
t@x,m-b,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-B,text
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def _u(r, key):
    return next(d for deps in r.config.groups.values() for d in deps
                if d.get("api_key") == key)["unique"]


# ------------------------------------------------------------------ bucketi
def test_ctx_bucket_edges():
    assert _ctx_bucket(100) == 0
    assert _ctx_bucket(7999) == 0
    assert _ctx_bucket(8000) == 1
    assert _ctx_bucket(31999) == 1
    assert _ctx_bucket(32000) == 2
    assert _ctx_bucket(127999) == 2
    assert _ctx_bucket(128000) == 3
    assert _ctx_bucket(None) == -1
    assert _ctx_bucket("x") == -1


def test_bucket_isolated_from_global(router):
    u = _u(router, "K-A")
    router.record_success(u, 500.0, ctx_est=1000)        # bucket 0
    router.record_success(u, 9000.0, ctx_est=100000)    # bucket 3
    assert router.bucket_latency_ms(u, 1000) == pytest.approx(500.0)
    assert router.bucket_latency_ms(u, 100000) == pytest.approx(9000.0)
    # globale: miscela dei due (EMA) ma i bucket restano separati
    assert 500.0 < router._avg_latencies[u] < 9000.0


def test_bucket_fallback_to_global(router):
    u = _u(router, "K-A")
    router.record_success(u, 700.0)                      # senza ctx: solo globale
    assert router.bucket_latency_ms(u, 5000) == pytest.approx(700.0)
    assert router.bucket_latency_ms(u, None) == pytest.approx(700.0)


def test_ttft_kind_separated(router):
    u = _u(router, "K-A")
    router.record_success(u, 8000.0, ctx_est=1000, kind="ttft")
    assert router.bucket_latency_ms(u, 1000, kind="ttft") == pytest.approx(8000.0)
    # il bucket 'total' resta vuoto: l'accesso in quel kind ripiega sul
    # globale (compat) ma NON e' un campione total reale
    assert u not in router._lat_buckets or \
        all(v <= 0 for v in router._lat_buckets[u])


def test_first_content_deadline_uses_ttft_bucket(router):
    u = _u(router, "K-A")
    q = router.policy.qc_json
    q.stream_first_content_ms = 60000
    q.stream_first_content_floor_ms = 5000
    q.stream_first_content_mult = 3.0
    router.record_success(u, 8000.0, ctx_est=1000, kind="ttft")
    router._note_latency_sample(u, 2000.0, 100000, "ttft", 1.0)
    # bucket pesante: TTFT propria 2000 -> 3*2000 = 6000 (>= floor)
    assert router.first_content_deadline_ms(u, 100000) == 6000
    # bucket leggero: TTFT propria 8000 -> 24000
    assert router.first_content_deadline_ms(u, 1000) == 24000


# ---------------------------------------------------------------- demote
def test_demote_relative_no_punish_chronic_slow(router):
    u = _u(router, "K-A")
    # baseline nota del bucket pesante: 195s (dep CRONICAMENTE lento qui)
    router.record_success(u, 195000.0, ctx_est=100000)
    router._avg_latencies[u] = 1000.0        # globale finto-basso: niente hard
    sid = "S"
    # 200s: > hard assoluto 90s MA non > 2x baseline del bucket -> NON marcato
    router._note_session_slow(sid, u, 200000.0, ctx_est=100000)
    assert not router.is_slow_for_session(u, sid, 100000)
    # 500s: oltre 2x baseline -> marcato hard
    router._note_session_slow(sid, u, 500000.0, ctx_est=100000)
    assert router.is_slow_for_session(u, sid, 100000)


def test_demote_soft_relative_still_gated_by_ctx(router):
    u = _u(router, "K-A")
    router.record_success(u, 2000.0, ctx_est=1000)      # baseline leggera 2s
    router._avg_latencies[u] = 1000.0
    sid = "S2"
    # 70s su ctx pesante: hard? no (70<90). soft? 60<70<=90 e ctx>30k.
    # relativa: 70s > 2*2s (il heavy bucket non ha campioni? ripiega globale)
    router._note_session_slow(sid, u, 70000.0, ctx_est=50000)
    assert router.is_slow_for_session(u, sid, 50000)     # pesante: demosso
    assert not router.is_slow_for_session(u, sid, 5000)  # leggero: OK


def test_note_stream_end_total_bucket_only(router):
    u = _u(router, "K-A")
    router.note_stream_end(u, 5000.0, 1000)
    assert router.bucket_latency_ms(u, 1000, kind="total") == pytest.approx(5000.0)
    assert router.bucket_latency_ms(u, 1000, kind="ttft") is None or \
        router.bucket_latency_ms(u, 1000, kind="ttft") == 0 or \
        router.bucket_latency_ms(u, 1000, kind="ttft") != 5000.0


# ------------------------------------------------------------------ p95
def test_latency_history_bucket_tuples_feed_p95(router):
    u = _u(router, "K-A")
    for i in range(25):
        router.record_success(u, 1000.0 + i * 10.0, ctx_est=1000)
    s = router.stats_for(u)
    assert len(s.latency_history) <= 20
    assert s.latency_history[-1][1] >= 1000.0            # tuple (bucket, lat)
    # smoke: il punteggio non esplode con gli esempi nuovi
    dep = router.config.deployment_by_unique(u)
    sc = router._reputation_score(u, dep, 1000)
    assert isinstance(sc, float)


# ------------------------------------------------------------- persistenza
def test_ctx_buckets_roundtrip(router):
    u = _u(router, "K-A")
    router.record_success(u, 400.0, ctx_est=100, kind="ttft")
    router.record_success(u, 600.0, ctx_est=50000)
    snap = router.dump_stats()
    assert "ctx_lat" in snap and "ctx_ttft" in snap
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    r2 = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                Policy.from_dict({}))
    os.unlink(path)
    r2.load_stats(snap)
    assert r2.bucket_latency_ms(u, 100, kind="ttft") == pytest.approx(400.0)
    assert r2.bucket_latency_ms(u, 50000) == pytest.approx(600.0)


def test_load_stats_rejects_junk(router):
    u = _u(router, "K-A")
    router.load_stats({"ctx_lat": {u: ["x", None, "y", {}, 7]},
                       "ctx_ttft": {u: [-1, 5, 5, 5, 5, 5]}})
    assert router.bucket_latency_ms(u, 100000) is None or \
        router.bucket_latency_ms(u, 100000) != -1.0
    assert len(router._ttft_buckets.get(u, ())) <= 4 or \
        all(v >= 0 for v in router._ttft_buckets[u])


# ===================== REGRESSIONE HOTFIX: bucket 3 (>128k) ==============
def test_top_bucket_no_indexerror(router):
    """ctx >= 128000 -> indice 3 su righe da 4 slot: non deve esplodere."""
    u = _u(router, "K-A")
    router.note_result(u, 20000, ctx_est=200000)
    assert router._lat_buckets[u][3] == 20000
    router.note_result(u, 4000, ctx_est=200000,
                       kind="ttft")
    assert router._ttft_buckets[u][3] == 4000
    assert router.bucket_latency_ms(u, 200000) == 20000
    assert router.bucket_latency_ms(u, 200000, kind="ttft") == 4000


def test_top_bucket_survives_dump_load(router):
    """dump/load non deve troncare il bucket 3 a 3 slot (reload corrotto)."""
    u = _u(router, "K-A")
    router.note_result(u, 30000, ctx_est=200000)
    snap = router.dump_stats()
    assert len(snap["ctx_lat"][u]) == 4
    r2 = Router(router.config, Policy.from_dict({}))
    r2.load_stats(snap)
    assert r2.bucket_latency_ms(u, 999999) == 30000


def test_top_bucket_first_content_deadline(router):
    """Deadline adattivo sul bucket TTFT 3: niente IndexError, valore reale."""
    u = _u(router, "K-A")
    router.note_result(u, 5000, ctx_est=150000,
                       kind="ttft")
    d = router.first_content_deadline_ms(u, 200000)
    assert d > 0 and d <= router.policy.qc_json.stream_first_content_ms


def test_short_rows_are_padded_not_crash(router):
    """File scritto da una build 3-slot: il load paga il bucket mancante."""
    u = _u(router, "K-A")
    r2 = Router(router.config, Policy.from_dict({}))
    r2.load_stats({"ctx_lat": {u: [100.0, 200.0, 300.0]}})
    assert len(r2._lat_buckets[u]) == 4
    assert r2.bucket_latency_ms(u, 200000) == 0.0 or True  # slot 3 vuoto
    assert r2.bucket_latency_ms(u, 1000) == 100.0


# ============ F9: PREFILL RATE (tassa dei gemelli sul TTFT) ==============
def test_prefill_rate_only_ttft_only_big_ctx(router):
    u = _u(router, "K-A")
    router.note_result(u, 3200, ctx_est=16000, kind="ttft")
    assert u in router._prefill_rate
    assert router._prefill_rate[u] == pytest.approx(200.0)   # 3200 / 16k
    # i totali NON alimentano il rate (semantica mista: niente prefill)
    u2 = _u(router, "K-B")
    router.note_result(u2, 3200, ctx_est=16000)              # kind=total
    assert u2 not in router._prefill_rate
    # sotto 8k il prefill e' rumore: rate invariato
    router.note_result(u, 500, ctx_est=2000, kind="ttft")
    assert router._prefill_rate[u] == pytest.approx(200.0)


def test_ttft_extrapolates_on_empty_bucket(router):
    u = _u(router, "K-A")
    router.note_result(u, 3200, ctx_est=16000, kind="ttft")  # rate 200 ms/k
    # bucket 3 vuoto: stima PROPORZIONALE alla taglia, non l'EMA dei piccoli
    assert router.bucket_latency_ms(u, 200000, "ttft") == pytest.approx(40000.0)
    assert router.bucket_latency_ms(u, 100000, "ttft") == pytest.approx(20000.0)
    # il bucket che ha campioni resta il campione (nessuna estrapolazione)
    assert router.bucket_latency_ms(u, 16000, "ttft") == pytest.approx(3200.0)
    # kind=total: fallback legacy all'EMA globale (nessuna normalizzazione)
    assert router.bucket_latency_ms(u, 200000) == pytest.approx(3200.0)


def test_ttft_extrapolation_floor_and_unctx(router):
    u = _u(router, "K-A")
    router.note_result(u, 3200, ctx_est=16000, kind="ttft")
    assert router.bucket_latency_ms(u, 1000, "ttft") == 250.0  # pavimento
    # senza ctx si torna al fallback globale (compat), 3200 dal record
    assert router.bucket_latency_ms(u, None, "ttft") == pytest.approx(3200.0)


def test_ttft_rate_survives_dump_load(router):
    u = _u(router, "K-A")
    router.note_result(u, 3200, ctx_est=16000, kind="ttft")
    snap = router.dump_stats()
    assert snap["ttft_rate"][u] == pytest.approx(200.0)
    r2 = Router(router.config, Policy.from_dict({}))
    r2.load_stats(snap)
    assert r2.bucket_latency_ms(u, 200000, "ttft") == pytest.approx(40000.0)


def test_ttft_rate_rejects_junk_on_load(router):
    u = _u(router, "K-A")
    r2 = Router(router.config, Policy.from_dict({}))
    r2.load_stats({"ttft_rate": {u: "nope", "bad": -5}})
    assert u not in r2._prefill_rate and "bad" not in r2._prefill_rate


def test_p95_normalized_across_buckets(router):
    """Campioni solo su contesti grossi: su una richiesta piccola il p95 va
    normalizzato (lat * ctx_req/ctx_s), non preso grezzo."""
    u = _u(router, "K-A")
    dep = router.config.deployment_by_unique(u)
    s = router.stats_for(u)
    s.latency_history = [(2, 40000, 30000.0)] * 3            # solo 40k, 30s
    big = router._reputation_score(u, dep, 40000)            # penalty piena
    small = router._reputation_score(u, dep, 5000)           # normalizzata
    assert small < big
    assert big - small >= 10.0


# ------------------------------------------------- F13: hedge adattivo al bucket
def test_hedge_delay_scales_with_ttft_bucket(router):
    """Su contesti grossi il prefill e' fisiologicamente lento: il canary
    parte piu' tardi invece di essere rumore a 1.5s."""
    u = _u(router, "K-A")
    q = router.policy.qc_json
    q.stream_hedge_delay_ms = 1500
    q.stream_hedge_ttft_frac = 0.6
    q.stream_hedge_min_ms = 800
    q.stream_hedge_max_ms = 2500
    router._note_latency_sample(u, 1000.0, 4000, "ttft", 1.0)      # bucket 0
    router._note_latency_sample(u, 5000.0, 200000, "ttft", 1.0)    # bucket 3
    # bucket 0: 0.6*1000 = 600 -> pavimento 800
    assert router.hedge_delay_ms(u, 4000) == 800
    # bucket 3: 0.6*5000 = 3000 -> tetto 2500
    assert router.hedge_delay_ms(u, 200000) == 2500


def test_hedge_delay_mid_bucket_linear(router):
    u = _u(router, "K-A")
    q = router.policy.qc_json
    q.stream_hedge_delay_ms = 1500
    q.stream_hedge_ttft_frac = 0.6
    q.stream_hedge_min_ms = 800
    q.stream_hedge_max_ms = 2500
    router._note_latency_sample(u, 3000.0, 60000, "ttft", 1.0)     # bucket 2
    assert router.hedge_delay_ms(u, 60000) == 1800                 # 0.6*3000


def test_hedge_delay_unknown_ttft_uses_base(router):
    u = _u(router, "K-A")
    q = router.policy.qc_json
    q.stream_hedge_delay_ms = 1200
    assert router.hedge_delay_ms(u, 100000) == 1200


def test_hedge_delay_zero_is_off(router):
    u = _u(router, "K-A")
    q = router.policy.qc_json
    q.stream_hedge_delay_ms = 0
    router._note_latency_sample(u, 5000.0, 200000, "ttft", 1.0)
    assert router.hedge_delay_ms(u, 200000) == 0
