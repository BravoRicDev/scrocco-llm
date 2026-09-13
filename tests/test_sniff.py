"""Debug SNIFF: input/output su file con rotazione. Deve essere OSSERVAZIONE
PURA (mai toccare i byte verso il client) e disattivabile (default OFF).
"""
import json
import os
import tempfile

from app import sniff
from app.policy import Policy


def test_disabled_by_default():
    assert Policy().debug_sniff_enabled is False
    assert Policy().debug_sniff_retention_hours == 24
    # senza env e con policy default -> spento
    os.environ.pop("GATEWAY_DEBUG_SNIFF", None)
    assert sniff.enabled(Policy()) is False


def test_enabled_via_policy_and_env():
    pol = Policy.from_dict({"debug": {"sniff": {"enabled": True,
                                                "retention_hours": 6}}})
    assert pol.debug_sniff_enabled is True
    assert pol.debug_sniff_retention_hours == 6
    assert sniff.enabled(pol) is True
    # env override
    os.environ["GATEWAY_DEBUG_SNIFF"] = "1"
    try:
        assert sniff.enabled(Policy()) is True
    finally:
        os.environ.pop("GATEWAY_DEBUG_SNIFF", None)


def test_writes_input_and_stream_output():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    # reset handler verso il path di test
    lg = sniff.logging.getLogger("nx.sniff")
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()
    sniff._logger = None
    sniff.configure(path, retention_hours=24)
    assert sniff._logger is not None

    sniffer = sniff.begin("rid1", {"model": "m", "stream": True},
                          {"model": "m", "messages": [{"role": "user",
                                                       "content": "ciao"}]})
    sniffer.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    sniffer.feed(b'data: {"choices":[{"delta":{"content":"<turn|>"}}]}\n\n')
    sniffer.feed(b"data: [DONE]\n\n")
    sniffer.finish_stream({"status": "success", "chunks": 3})

    for h in lg.handlers:
        h.flush()
    lines = [json.loads(x) for x in open(path, encoding="utf-8")
             if x.strip()]
    dirs = [r["dir"] for r in lines]
    assert dirs == ["in", "out"]
    assert lines[0]["payload"]["messages"][0]["content"] == "ciao"
    assert "<turn|>" in lines[1]["sse"]      # il testo incriminato e' tracciato
    assert lines[1]["status"] == "success"
    os.unlink(path)
