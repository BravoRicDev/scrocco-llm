"""Poll/download STATELESS dei job video: candidati multi-chiave e fallback 404.

Scenario reale: la submit avviene con la chiave dell'account A; il gateway
riparte (mapping perso); l'agente ripassa ?model= e il gateway deve ritrovare
il job provando le chiavi di TUTTI i deployment del gruppo video_gen."""
import asyncio
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.forwarder import Forwarder, UpstreamError
from app.policy import Policy
from app.router import Router

# due righe STESSO modello ma chiavi DIVERSE (= due account OR distinti)
CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,bytedance/seedance-2.0-mini,openrouter,https://openrouter.ai/api/v1,paid,0,0,5,sk-KEY-ACCOUNT-A,video_gen
t@x.com,bytedance/seedance-2.0-mini,openrouter,https://openrouter.ai/api/v1,paid,0,0,5,sk-KEY-ACCOUNT-B,video_gen
t@x.com,openai/gpt-oss-120b,groq,https://api.groq.com/openai/v1,free,128,8000,0,sk-FAKE-KEY,text
"""

POLICY_MAP = {"capability_routing": {"model_capabilities": {
    "*seedance*": ["text", "video_gen"],
    "openai/gpt-oss-120b": ["text"],
}}}


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    pol = Policy.from_dict(POLICY_MAP)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def test_candidates_distinct_keys(router):
    deps = router.video_gen_candidates("scrocco-llm-test")
    keys = {d["api_key"] for d in deps}
    assert keys == {"sk-KEY-ACCOUNT-A", "sk-KEY-ACCOUNT-B"}   # entrambi gli account


def test_candidates_unknown_model_empty(router):
    # CONTRATTO: nomi non risolvibili a un profilo
    # (gruppi text-only, profili inesistenti, modelli grezzi) NON ritornano
    # piu' []: tornano i candidati video_gen di TUTTI i profili — il job
    # vive su un account e il recupero stateless deve poterlo cercare.
    fallback = router.video_gen_candidates("scrocco-llm-non-esiste")
    assert fallback == router.video_gen_candidates("scrocco-llm-test-128k")
    assert {d["api_key"] for d in fallback} == \
        {"sk-KEY-ACCOUNT-A", "sk-KEY-ACCOUNT-B"}


def _fwd_with_polls(results):
    fwd = Forwarder.__new__(Forwarder)   # niente rete: monkeypatch di poll_video

    async def fake_poll(dep, job_id):    # noqa: ANN001
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    fwd.poll_video = fake_poll           # type: ignore[method-assign]
    return fwd


def test_poll_any_skips_404_other_account(router):
    deps = router.video_gen_candidates("scrocco-llm-test")
    fwd = _fwd_with_polls([UpstreamError(-404, "job not found"),
                           {"id": "J1", "status": "completed"}])
    got = asyncio.run(fwd.poll_video_any(deps, "J1"))
    assert got["status"] == "completed"


def test_poll_any_raises_when_all_404(router):
    deps = router.video_gen_candidates("scrocco-llm-test")
    fwd = _fwd_with_polls([UpstreamError(-404, "nf")] * len(deps))
    with pytest.raises(UpstreamError) as ei:
        asyncio.run(fwd.poll_video_any(deps, "JX"))
    assert abs(ei.value.status) == 404


def test_download_any_skips_404():
    fwd = Forwarder.__new__(Forwarder)

    async def fake_dl(dep, job_id):
        if dep["api_key"] == "sk-A":
            raise UpstreamError(-404, "nope")
        return b"MP4BYTES", "video/mp4"

    fwd.download_video = fake_dl         # type: ignore[method-assign]
    data, ctype = asyncio.run(fwd.download_video_any(
        [{"api_key": "sk-A"}, {"api_key": "sk-B"}], "J9"))
    assert data == b"MP4BYTES" and ctype == "video/mp4"
