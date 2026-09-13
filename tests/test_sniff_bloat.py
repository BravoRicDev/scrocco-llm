"""Memory Bloat Guard dello sniff: i payload multimodali (immagini/PDF in
base64, array di byte) vengono sostituiti con placeholder prima del log."""
from app import sniff


def _content(url):
    return {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": url}}]}]}


def test_data_uri_image_replaced():
    big = "data:image/png;base64," + "A" * 200000
    out = sniff._shrink(_content(big))
    url = out["messages"][0]["content"][0]["image_url"]["url"]
    assert "IMAGE_BASE64_TRUNCATED_BY_SNIFFER" in url
    assert len(url) < 100


def test_anthropic_base64_source_replaced():
    big = "B" * 150000
    out = sniff._shrink({"content": [
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/jpeg",
                                     "data": big}}]})
    data = out["content"][0]["source"]["data"]
    assert "BASE64_TRUNCATED_BY_SNIFFER" in data


def test_byte_array_replaced():
    out = sniff._shrink({"data": list(range(256)) * 50})   # 12800 int
    assert out["data"][0].startswith("[BYTE_ARRAY_TRUNCATED_BY_SNIFFER")


def test_plain_short_text_untouched():
    msg = {"role": "user", "content": "ciao, come stai?"}
    assert sniff._shrink(msg) == msg


def test_long_plain_text_truncated_but_kept():
    txt = "parola " * 5000
    out = sniff._shrink(txt)
    assert out.startswith("parola ")
    assert "STRING_TRUNCATED_BY_SNIFFER" in out


def test_feed_cap_bounds_memory():
    s = sniff.Sniffer("r1", {})
    chunk = b"x" * 100000
    for _ in range(40):
        s.feed(chunk)
    assert s._stored <= sniff._MAX_SSE_BYTES
    assert s._dropped > 0
