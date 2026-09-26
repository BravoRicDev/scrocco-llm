"""STT-bridge: audio in chat -> testo per un LLM qualsiasi.

Blindato qui (intervento #57):
  - riconoscimento delle FORME audio: `input_audio`, `audio_url`, `inline_data`
    e `file` con mime audio/*. Prima `required_caps` guardava solo `input_audio`
    e i formati Gemini-native finivano nel mondo testo senza chiedere `audio`;
  - normalizzazione: QUALSIASI formato (mp3, m4a, ogg, flac, wav, PCM grezzo,
    WAV senza header) diventa OGG/Opus, che tutti i backend STT accettano
    (groq ha una whitelist e rifiuta il PCM);
  - chunking BILANCIATO: N = ceil(durata/target) con confine bilanciato
    agganciato al silenzio. Il caso da non produrre e' 3 minuti + 1 su un file
    da 4 minuti;
  - ancoraggio alla finestra della COMPATTAZIONE: l'audio che esce dalla
    finestra sparisce senza traduzione e SENZA marker;
  - MAI 503: se l'STT fallisce va un marker che l'LLM cita in risposta, e la
    richiesta prosegue;
  - audio + testo non si perdono: la trascrizione e' accorpata al testo dello
    stesso messaggio;
  - cache: stesso audio a un turno successivo = zero chiamate STT.
"""
import asyncio
import base64
import io
import math
import struct

import pytest

from app import audioconvert as A
from app import audiostore as S
from app import sttchat
from app.capabilities import (count_audio_parts, required_caps,
                              _is_audio_part)
from app.policy import Policy

SR = 16000


# ------------------------------------------------------------- fixture audio
def _pcm(sec, fn=None):
    """PCM s16 LE mono a 16kHz."""
    f = fn or (lambda i: int(6000 * math.sin(2 * math.pi * 440 * i / SR)))
    return b"".join(struct.pack("<h", max(-32768, min(32767, f(i))))
                    for i in range(int(SR * sec)))


def _wav(sec, fn=None):
    """WAV con header RIFF, via PyAV (nessun encoder scritto a mano)."""
    av = pytest.importorskip("av")
    data = _pcm(sec, fn)
    buf = io.BytesIO()
    c = av.open(buf, "w", format="wav")
    st = c.add_stream("pcm_s16le", rate=SR)
    st.layout = "mono"
    fr = av.AudioFrame(format="s16", layout="mono", samples=len(data) // 2)
    fr.planes[0].update(data)
    fr.sample_rate = SR
    fr.pts = 0
    for p in st.encode(fr):
        c.mux(p)
    c.close()
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ================================================== 1. forme audio riconosciute
def test_forme_audio_riconosciute():
    cases = {
        "input_audio": {"type": "input_audio",
                        "input_audio": {"data": "AAA", "format": "wav"}},
        "audio_url": {"type": "audio_url",
                      "audio_url": {"url": "data:audio/wav;base64,AAA"}},
        "inline_data audio": {"type": "inline_data",
                              "mime_type": "audio/wav", "data": "AAA"},
        "file audio": {"type": "file",
                       "file": {"mime_type": "audio/mpeg", "file_data": "AA"}},
    }
    for name, part in cases.items():
        p = {"messages": [{"role": "user", "content": [part]}]}
        assert _is_audio_part(part), name
        assert "audio" in required_caps(p), name
        assert count_audio_parts(p["messages"]) == 1, name


def test_forme_media_non_confuse():
    """Un'immagine non deve mai essere trattata come audio (e viceversa)."""
    for part in ({"type": "image_url", "image_url": {"url": "x"}},
                 {"type": "inline_data", "mime_type": "image/png", "data": "x"},
                 {"type": "text", "text": "ciao"},
                 {"type": "input_audio", "input_audio": {"data": "x"}},
                 {"type": "inline_data",
                  "mime_type": "application/octet-stream", "data": "x"}):
        p = {"messages": [{"role": "user", "content": [part]}]}
        want = "audio" in required_caps(p)
        assert want == _is_audio_part(part), part


# ====================================================== 2. normalizzazione
def test_normalizza_ogni_formato_in_ogg():
    av = pytest.importorskip("av")
    data = _pcm(3)

    def enc(fmt, codec, **kw):
        buf = io.BytesIO()
        c = av.open(buf, "w", format=fmt)
        st = c.add_stream(codec, rate=SR)
        st.layout = "mono"
        for k, v in kw.items():
            setattr(st, k, v)
        fr = av.AudioFrame(format="s16", layout="mono", samples=len(data) // 2)
        fr.planes[0].update(data)
        fr.sample_rate = SR
        fr.pts = 0
        for p in st.encode(fr):
            c.mux(p)
        c.close()
        return buf.getvalue()

    sources = {
        "wav": _wav(3),
        "mp3": enc("mp3", "libmp3lame", bit_rate=32000),
        "flac": enc("flac", "flac"),
        "ogg": enc("ogg", "libopus", bit_rate=24000),
        "m4a": enc("ipod", "aac", bit_rate=64000),
        "pcm grezzo": data,
    }
    for name, raw in sources.items():
        chunks = A.to_ogg_chunks(raw, fmt_hint="wav", target_sec=180)
        assert chunks, name
        for ch in chunks:
            assert ch[:4] == b"OggS", f"{name} non produce OGG"
        # l'input e' piu' grande dell'output: e' la compressione che ci serve
        assert len(chunks[0]) < len(raw), name


def test_rifiuta_dati_non_audio():
    for name, bad in (("vuoto", b""),
                      ("html", b"<!DOCTYPE html><html>x</html>"),
                      ("png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000),
                      ("riempitivo", b"\x00" * 8000),
                      ("random", b"\x00\x01\x02\x03" * 500)):
        with pytest.raises(A.AudioConversionError):
            A.to_ogg_chunks(bad, target_sec=180)


def test_rifiuta_clip_troppo_corto():
    with pytest.raises(A.AudioConversionError):
        A.to_ogg_chunks(_pcm(0.1), fmt_hint="wav", target_sec=180)


# ============================================== 3. chunking bilanciato
def _durations_min(chunks):
    return [A.probe_duration(c, "ogg") / 60 for c in chunks]


def test_nessun_chunk_sotto_il_target():
    ch = A.to_ogg_chunks(_wav(120), target_sec=180)
    assert len(ch) == 1


def test_quattro_minuti_due_chunk_equilibrati():
    """Il caso esplicito: 4 minuti non devono diventare 3 + 1."""
    cd = _durations_min(A.to_ogg_chunks(_wav(240), target_sec=180))
    assert len(cd) == 2, cd
    assert cd[0] == pytest.approx(2.0, abs=0.35), cd
    assert cd[1] == pytest.approx(2.0, abs=0.35), cd


def test_bilanciamento_su_tutte_le_durate():
    for sec, n in ((360, 2), (420, 3), (600, 4), (1320, 8)):
        cd = _durations_min(A.to_ogg_chunks(_wav(sec), target_sec=180))
        assert len(cd) == n, (sec, cd)
        exp = sec / n / 60
        for d in cd:
            assert d == pytest.approx(exp, abs=0.75), (sec, cd, exp)


def test_il_taglio_aggancia_il_silenzio():
    """Con un silenzio reale vicino al confine, il taglio ci va dentro."""
    # 600s -> 4 chunk, bersagli 150/300/450s. Silenzio a 300-312s.
    def gen(i):
        t = i / SR
        return 0 if 300 <= t < 312 else int(
            6000 + 3000 * math.sin(2 * math.pi * 400 * i / SR))
    cd = _durations_min(A.to_ogg_chunks(_wav(600, gen), target_sec=180))
    assert len(cd) == 4
    # il 2o chunk deve finire nel silenzio, non al confine esatto
    assert 4.4 < cd[0] + cd[1] < 5.6, cd


def test_nessun_chunk_vuoto():
    chunks = A.to_ogg_chunks(_wav(700), target_sec=180)
    assert chunks
    for ch in chunks:
        assert len(ch) > 0
        assert ch[:4] == b"OggS"


# ============================================== 4. sostituzione nel payload
def _msg(parts, role="user"):
    return {"role": role, "content": parts}


def _audio_part(sec=3, fmt="wav"):
    return {"type": "input_audio",
            "input_audio": {"data": _b64(_wav(sec)), "format": fmt}}


class _Pol:
    """Policy minima per il bridge."""
    stt_chat_enabled = True
    stt_chat_target_sec = 180
    stt_chat_search_pct = 0.2
    stt_chat_max_bytes = 7 * 1024 * 1024
    stt_chat_max_parallel = 4
    stt_chat_unavailable_notice = "Nessun STT ha risposto."


def _run(payload, *, boundary=None, fails=False, calls=None,
         pol=None, raise_exc=None):
    """Esegue il bridge con un trascrittore finto."""
    pol = pol or _Pol()
    box = calls if calls is not None else []

    async def _one(chunk, idx):
        box.append((idx, len(chunk)))
        if raise_exc:
            raise raise_exc
        if fails:
            raise RuntimeError("STT non disponibile")
        return f"testo chunk {idx}"

    async def _main():
        return await sttchat.resolve_audio_in_payload(
            payload, boundary=boundary, transcript_one=_one, policy=pol)

    return asyncio.run(_main())


def test_audio_sostituito_da_trascrizione():
    S.clear()
    payload = {"model": "m", "messages": [_msg([_audio_part()])]}
    calls = []
    out = _run(payload, calls=calls)
    assert len(calls) == 1
    content = out["messages"][0]["content"]
    # nessuna parte audio residua
    assert not any(_is_audio_part(p) for p in content)
    txt = " ".join(p.get("text", "") for p in content)
    assert "trascrizione audio:" in txt
    assert "testo chunk 0" in txt


def test_originale_intatto():
    S.clear()
    payload = {"model": "m", "messages": [_msg([_audio_part()])]}
    before = count_audio_parts(payload["messages"])
    out = _run(payload)
    assert out is not payload
    assert count_audio_parts(payload["messages"]) == before


def test_nessun_audio_payload_identico():
    payload = {"model": "m", "messages": [_msg([{"type": "text",
                                                 "text": "ciao"}])]}
    assert _run(payload) is payload


def test_kill_switch_disabilita():
    pol = _Pol()
    pol.stt_chat_enabled = False
    payload = {"model": "m", "messages": [_msg([_audio_part()])]}
    out = _run(payload, pol=pol)
    assert out is payload
    assert count_audio_parts(out["messages"]) == 1


# ==================================== 5. mai 503: marker invece dell'errore
def test_stt_fallito_mette_marker_e_non_erra():
    S.clear()
    payload = {"model": "m", "messages": [_msg([_audio_part()])]}
    out = _run(payload, fails=True)
    content = out["messages"][0]["content"]
    assert not any(_is_audio_part(p) for p in content)
    txt = " ".join(p.get("text", "") for p in content)
    assert "audio non trascritto" in txt
    assert "Nessun STT ha risposto" in txt


def test_stt_che_solleva_marca_e_prosegue():
    S.clear()
    payload = {"model": "m", "messages": [_msg([_audio_part()])]}
    out = _run(payload, raise_exc=ValueError("boom"))
    txt = " ".join(p.get("text", "")
                   for p in out["messages"][0]["content"])
    assert "audio non trascritto" in txt


def test_audio_illeggibile_mette_marker():
    S.clear()
    bad = {"type": "input_audio",
           "input_audio": {"data": _b64(b"non sono audio" * 400),
                           "format": "wav"}}
    payload = {"model": "m", "messages": [_msg([bad])]}
    out = _run(payload)
    txt = " ".join(p.get("text", "")
                   for p in out["messages"][0]["content"])
    assert "audio non trascritto" in txt


# ==================================== 6. audio fuori finestra: via, senza marker
def test_audio_fuori_finestra_rimosso_senza_marker():
    S.clear()
    payload = {"model": "m", "messages": [
        _msg([{"type": "text", "text": "vecchio"}, _audio_part()]),
        {"role": "assistant", "content": "ok"},
        _msg([{"type": "text", "text": "nuovo"}]),
    ]}
    calls = []
    out = _run(payload, boundary=2, calls=calls)
    assert calls == []                       # niente STT per un audio vecchio
    first = out["messages"][0]["content"]
    assert not any(_is_audio_part(p) for p in first)
    txt = " ".join(p.get("text", "") for p in first)
    assert "vecchio" in txt
    assert "audio non trascritto" not in txt  # non e' un fallimento
    assert "trascrizione" not in txt


def test_audio_dentro_finiera_trascritto():
    S.clear()
    payload = {"model": "m", "messages": [
        _msg([{"type": "text", "text": "vecchio"}, _audio_part()]),
        {"role": "assistant", "content": "ok"},
        _msg([{"type": "text", "text": "nuovo"}]),
    ]}
    calls = []
    out = _run(payload, boundary=0, calls=calls)
    assert calls                                # l'audio e' ancora ancorato
    txt = " ".join(p.get("text", "")
                   for p in out["messages"][0]["content"])
    assert "trascrizione audio:" in txt


# ==================================== 7. audio + testo NON si perdono
def test_audio_e_testo_nello_stesso_messaggio():
    S.clear()
    payload = {"model": "m", "messages": [_msg([
        {"type": "text", "text": "cosa dice l'audio?"},
        _audio_part(),
    ])]}
    out = _run(payload)
    parts = out["messages"][0]["content"]
    txt = " ".join(p.get("text", "") for p in parts)
    assert "cosa dice l'audio?" in txt, "il testo dell'utente e' andato perso"
    assert "trascrizione audio:" in txt, "la trascrizione e' andata persa"
    # lo stesso blocco di testo: accorpati, non separati
    assert sum(1 for p in parts if "trascrizione" in p.get("text", "")) == 1
    assert "trascrizione" in parts[0].get("text", "")


def test_audio_senza_testo_crea_il_blocco():
    S.clear()
    payload = {"model": "m", "messages": [_msg([_audio_part()])]}
    out = _run(payload)
    parts = out["messages"][0]["content"]
    assert len(parts) == 1
    assert "trascrizione audio:" in parts[0]["text"]


def test_due_audio_nello_stesso_messaggio():
    S.clear()
    payload = {"model": "m", "messages": [_msg([
        {"type": "text", "text": "prima e dopo"},
        _audio_part(3), _audio_part(4),
    ])]}
    calls = []
    out = _run(payload, calls=calls)
    assert len(calls) == 2
    txt = " ".join(p.get("text", "")
                   for p in out["messages"][0]["content"])
    assert "prima e dopo" in txt
    assert txt.count("trascrizione audio:") == 2


# ==================================== 8. cache: niente STT ripetuto
def test_cache_evita_seconda_trascrizione():
    S.clear()
    part = _audio_part()
    calls_a = []
    _run({"model": "m", "messages": [_msg([part])]}, calls=calls_a)
    calls_b = []
    # stesso audio, turno successivo: la history rimanda lo stesso base64
    _run({"model": "m", "messages": [
        _msg([{"type": "text", "text": "prima"}]),
        {"role": "assistant", "content": "..."},
        _msg([part]),
    ]}, boundary=0, calls=calls_b)
    assert len(calls_a) == 1
    assert calls_b == []                      # zero chiamate STT


def test_cache_non_memorizza_i_fallimenti():
    S.clear()
    part = _audio_part()
    _run({"model": "m", "messages": [_msg([part])]}, fails=True)
    calls = []
    _run({"model": "m", "messages": [_msg([part])]}, calls=calls)
    assert len(calls) == 1, "il fallimento non deve finire in cache"


# ==================================== 9. chunk paralleli e ordinati
def test_chunk_multipli_in_ordine():
    S.clear()
    # 7 minuti -> 3 chunk
    payload = {"model": "m", "messages": [_msg([
        {"type": "input_audio",
         "input_audio": {"data": _b64(_wav(420)), "format": "wav"}}])]}
    calls = []

    async def _one(chunk, idx):
        calls.append(idx)
        await asyncio.sleep(0.01 * (3 - idx))    # il primo finisce per ultimo
        return f"C{idx}"

    async def _main():
        return await sttchat.resolve_audio_in_payload(
            payload, boundary=0, transcript_one=_one, policy=_Pol())

    out = asyncio.run(_main())
    assert sorted(calls) == [0, 1, 2]
    txt = " ".join(p.get("text", "")
                   for p in out["messages"][0]["content"])
    # l'ordine del testo segue l'indice del chunk, non la fine delle chiamate
    assert txt.index("C0") < txt.index("C1") < txt.index("C2")


def test_un_chunk_fallito_non_perso_gli_altri():
    S.clear()
    payload = {"model": "m", "messages": [_msg([
        {"type": "input_audio",
         "input_audio": {"data": _b64(_wav(420)), "format": "wav"}}])]}

    async def _one(chunk, idx):
        if idx == 1:
            raise RuntimeError("chunk 1 perso")
        return f"C{idx}"

    async def _main():
        return await sttchat.resolve_audio_in_payload(
            payload, boundary=0, transcript_one=_one, policy=_Pol())

    out = asyncio.run(_main())
    txt = " ".join(p.get("text", "")
                   for p in out["messages"][0]["content"])
    assert "C0" in txt and "C2" in txt, "i chunk buoni devono restare"


def test_parallelismo_limitato():
    S.clear()
    payload = {"model": "m", "messages": [_msg([
        {"type": "input_audio",
         "input_audio": {"data": _b64(_wav(1200)), "format": "wav"}}])]}
    pol = _Pol()
    pol.stt_chat_max_parallel = 2
    live = {"now": 0, "max": 0}

    async def _one(chunk, idx):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return "x"

    async def _main():
        return await sttchat.resolve_audio_in_payload(
            payload, boundary=0, transcript_one=_one, policy=pol)

    asyncio.run(_main())
    assert live["max"] <= 2, live


# ==================================== 10. tetto byte
def test_tetto_byte_applicato_dopo_la_compressione():
    """Il tetto NON guarda l'ingresso grezzo: 7 minuti di WAV sono 13 MB ma
    diventano ~1,5 MB in OGG, e rifiutarli ucciderebbe audio valido. Qui si
    forza un tetto piccolo sul chunk gia' compresso."""
    S.clear()
    pol = _Pol()
    pol.stt_chat_max_bytes = 2000        # sotto il chunk OGG di 3 minuti
    payload = {"model": "m", "messages": [_msg([_audio_part()])]}
    out = _run(payload, pol=pol)
    txt = " ".join(p.get("text", "")
                   for p in out["messages"][0]["content"])
    assert "troppo grande" in txt
    assert "compressione" in txt


def test_wav_grande_ma_compresso_non_rifiutato():
    """Il caso inverso: WAV da 13 MB (7 min) deve passare, perche' in OGG
    sta sotto il tetto."""
    S.clear()
    payload = {"model": "m", "messages": [_msg([_audio_part(sec=420)])]}
    assert len(_wav(420)) > 7 * 1024 * 1024      # ingresso oltre il tetto
    calls = []
    out = _run(payload, calls=calls)
    # 420s / 180s = 3 chunk da 2,33 minuti
    assert len(calls) == 3, "il WAV grande doveva essere trascritto in 3 chunk"
    txt = " ".join(p.get("text", "")
                   for p in out["messages"][0]["content"])
    assert "troppo grande" not in txt


# ==================================== 11. knob
def test_knob_stt_chat():
    pol = Policy()
    assert pol.stt_chat_enabled is True
    assert pol.stt_chat_target_sec == 180
    assert pol.stt_chat_search_pct == pytest.approx(0.2)
    assert pol.stt_chat_max_parallel == 4
    assert pol.stt_chat_cache_ttl_sec == 3600
    assert pol.audio_token_estimate > 0
    assert "nessuno" in pol.stt_chat_unavailable_notice.lower()
    with pytest.raises(ValueError):
        Policy.from_dict({"capability_routing": {"stt_chat_target_sec": 0}})
    with pytest.raises(ValueError):
        Policy.from_dict({"capability_routing": {"stt_chat_search_pct": 2}})
    with pytest.raises(ValueError):
        Policy.from_dict({"capability_routing": {"stt_chat_max_parallel": 0}})
    assert Policy.from_dict(
        {"capability_routing": {"stt_chat_enabled": False}}
    ).stt_chat_enabled is False


def test_cache_store():
    S.clear()
    k = S.key_for(b"abc")
    assert S.key_for(b"abc") == k
    assert S.key_for(b"abd") != k
    assert S.get(k) is None
    S.put(k, "testo")
    assert S.get(k) == "testo"
    S.put(k, "")
    assert S.get(k) == "testo", "un put vuoto non deve sovrascrivere"
    st = S.stats()
    assert st["items"] == 1
    S.clear()
    assert S.get(k) is None


# ============================================ FIX 4: container vs PCM grezzo
def _enc(fmtname, codec, secs=2, bitrate=None):
    """Codifica _pcm(secs) nel container indicato (PyAV)."""
    av = pytest.importorskip("av")
    data = _pcm(secs)
    buf = io.BytesIO()
    c = av.open(buf, "w", format=fmtname)
    st = c.add_stream(codec, rate=SR)
    st.layout = "mono"
    if bitrate:
        st.bit_rate = bitrate
    off = 0
    while off < len(data):
        chunk = data[off:off + 2048]
        off += 2048
        fr = av.AudioFrame(format="s16", layout="mono", samples=len(chunk) // 2)
        fr.planes[0].update(chunk)
        fr.sample_rate = SR
        fr.pts = None
        for p in st.encode(fr):
            c.mux(p)
    for p in st.encode(None):
        c.mux(p)
    c.close()
    return buf.getvalue()


class TestContainerGuard:
    """La guardia magic non copriva ISO-BMFF: `ftyp` sta a OFFSET 4, non in
    testa. mp4/m4a di lunghezza PARI passavano il fallback PCM ed erano
    avvolti come s16: rumore che lo STT trascrive a caso (cachato 1h)."""

    def test_ftyp_a_offset_4_riconosciuto(self):
        m4a = _enc("ipod", "aac", secs=2, bitrate=64000)
        assert m4a[4:8] == b"ftyp"
        assert A._looks_like_container(m4a) is True

    def test_m4a_pari_non_viene_avvolto_come_pcm(self):
        """Il caso del bug: m4a di lunghezza pari. Prima veniva avvolto e il
        risultato era rumore di 0,4s invece dei 2s di audio vero."""
        m4a = _enc("ipod", "aac", secs=2, bitrate=64000)
        if len(m4a) % 2:
            m4a += b"\x00"                       # forza la lunghezza pari
        assert len(m4a) % 2 == 0
        out = b"".join(A.decode_pcm(m4a, fmt_hint="m4a"))
        assert abs(len(out) / (SR * 2) - 2.0) < 0.3, len(out) / (SR * 2)

    def test_probe_pcm_coerente_con_decode(self):
        """Stessa guardia nei due call site: altrimenti probe_duration
        direbbe 0,4s dove decode ne produce 2."""
        m4a = _enc("ipod", "aac", secs=2, bitrate=64000)
        if len(m4a) % 2:
            m4a += b"\x00"
        total, rate = A.probe_pcm(m4a, fmt_hint="m4a")
        assert rate == SR
        assert abs(total / (SR * 2) - 2.0) < 0.3, total / (SR * 2)

    def test_durata_reale_di_un_m4a(self):
        m4a = _enc("ipod", "aac", secs=2, bitrate=64000)
        assert abs(A.probe_duration(m4a, fmt_hint="m4a") - 2.0) < 0.3

    def test_mp4_stesso_vincolo(self):
        mp4 = _enc("mp4", "aac", secs=2, bitrate=64000)
        if len(mp4) % 2:
            mp4 += b"\x00"
        assert A._looks_like_container(mp4) is True
        assert abs(A.probe_duration(mp4, fmt_hint="mp4") - 2.0) < 0.3

    def test_to_ogg_chunks_m4a(self):
        m4a = _enc("ipod", "aac", secs=2, bitrate=64000)
        if len(m4a) % 2:
            m4a += b"\x00"
        chunks = A.to_ogg_chunks(m4a, fmt_hint="m4a")
        assert len(chunks) == 1
        # Tolleranza 0.5s: il pre-skip dell'Opus aggiunge ~0,2s di padding ed
        # e' un artefatto preesistente della codifica, non della guardia. Il
        # bug da scovare era 2s di audio vero che diventavano 0,43s di rumore.
        assert abs(A.probe_duration(chunks[0]) - 2.0) < 0.5


class TestContainerMagicLengths:
    """`data[:4]` non puo' matchare un magic a 3 byte: `b"ID3"` era CODICE
    MORTO, la guardia non scattava mai su un mp3 con tag ID3 pari."""

    def test_id3_tre_byte(self):
        mp3 = _enc("mp3", "mp3", secs=2)
        assert mp3[:3] == b"ID3"
        assert A._looks_like_container(mp3) is True

    def test_mp3_id3_resta_riconosciuto_qualunque_parita(self):
        """NON si padda il file: aggiungere 0x00 a un mp3 di libmp3lame ne
        corrompe il trailer (InvalidDataError in avcodec_send_packet).
        Il punto del test e' che la guardia non dipende dalla parita'."""
        mp3 = _enc("mp3", "mp3", secs=2)
        assert mp3[:3] == b"ID3"
        assert A._looks_like_container(mp3) is True
        # dispari: non wrappata, decodifica normale
        assert abs(A.probe_duration(mp3, fmt_hint="mp3") - 2.0) < 0.4
        # pari: la guardia lo riconosce comunque, non lo wrappa. Il file non
        # viene decodificato perche' e' mutato (coda 0xff): qui si testa la
        # guardia, non il decoder.
        pari = mp3 + b"\xff"
        assert len(pari) % 2 == 0
        assert A._looks_like_container(pari) is True

    def test_frame_sync_mpeg_senza_id3(self):
        """Frame-sync MPEG stretto: 11 bit a 1. La guardia stretta (0xFF Ex/Fx)
        evita di dichiarare 'container' un PCM che comincia per 0xFF."""
        assert A._looks_like_container(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        assert A._looks_like_container(b"\xff\xe0" + b"\x00" * 100)
        # 0xFF seguito da 0b011xxxxx NON e' un frame-sync MPEG
        assert not A._looks_like_container(b"\xff\x60\x00\x00" + b"\x00" * 100)

    def test_magic_noti_ancora_validi(self):
        for magic in (b"RIFF", b"OggS", b"fLaC", b"\x1a\x45\xdf\xa3"):
            assert A._looks_like_container(magic + b"\x00" * 64), magic


class TestPcmPlausible:
    def test_pcm_vero_resta_plausibile(self):
        for amp in (6000, 500, 30000, 50, 100):
            assert A._pcm_plausible(_pcm(2, lambda i, a=amp: (
                int(a * math.sin(2 * math.pi * 440 * i / SR))))) is True, amp

    def test_dc_e_fill_rifiutati(self):
        assert A._pcm_plausible(b"\x00" * 32000) is False
        assert A._pcm_plausible((b"\xab\xcd") * 16000) is False
        assert A._pcm_plausible((b"\x00\x01") * 16000) is False

    def test_dispari_rifiutato(self):
        assert A._pcm_plausible(_pcm(2) + b"\x00") is False

    def test_troppo_corto_rifiutato(self):
        assert A._pcm_plausible(_pcm(0.001)) is False

    def test_guardia_magic_e_la_vera_barriera(self):
        """`_pcm_plausible` NON distingue un container da un segnale: misurato
        su 95 blob reali (m4a/mp4/ogg/webm/mp3) i metadati non vengono
        respinti, perche' i bit di un payload codificato oscillano anche su
        valori piccoli. Il blocco sui container lo mette `_looks_like_container`
        (ftyp a offset 4 incluso): e' quello il meccanismo che conta, e
        questo test lo blocca esplicitamente contro un future che lo
        indebolirebbe."""
        m4a = _enc("ipod", "aac", secs=2, bitrate=64000)
        assert A._looks_like_container(m4a) is True
        assert A._looks_like_container(m4a + b"\x00") is True
        assert abs(A.probe_duration(m4a, fmt_hint="m4a") - 2.0) < 0.3

    def test_pcm_dispari_non_e_plausibile(self):
        """Byte dispari: non e' PCM s16, quindi non e' plausibile come tale
        (il wrapper WAV non lo puo' nemmeno costruire)."""
        assert A._pcm_plausible(_pcm(2) + b"\x00") is False

    def test_png_ancora_rifiutato(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8000
        with pytest.raises(A.AudioConversionError):
            A.to_ogg_chunks(png, target_sec=180)
