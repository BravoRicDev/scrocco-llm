"""Normalizzazione e segmentazione dell'audio per lo STT-bridge in chat.

Perche' questo modulo esiste. Un client puo' mandare audio in chat in
qualunque formato (mp3, m4a, ogg, flac, webm, wav, PCM grezzo) e in forma
OpenAI (`input_audio`) o Gemini (`inline_data`). I backend STT non accettano
tutto: ho verificato che groq ha una whitelist esplicita
`[flac mp3 mp4 mpeg mpga m4a ogg opus wav webm]` e RIFIUTA il PCM grezzo con
400. Quindi ogni audio viene normalizzato qui in un formato garantito, e
diviso in chunk che i backend accettano.

Perche' OGG/Opus. Misurato su 60s di audio a 16kHz mono:

    wav  1.920.078 byte  ->  7 MB coprono  3,8 minuti
    ogg    333.941 byte  ->  7 MB coprono 22,0 minuti
    flac   360.314 byte  ->  7 MB coprono 20,0 minuti

Opus 24kbit e' ~7x piu' compatto del WAV e purpose-built per la voce, quindi
con 3 minuti di chunk target resta 5,8x sotto il tetto di 7 MB: il tetto
diventa una rete di sicurezza, non un vincolo attivo.

Perche' PyAV. Nel container non c'e' ffmpeg, e dei soli moduli della stdlib
`wave` copre il WAV e `audioop` e' deprecato (rimosso in Python 3.13). PyAV
porta libav come libreria: decodifica e codifica in-process, senza dipendenze
di sistema.

[EN] WHAT: pure audio helpers - normalize to OGG/Opus, balanced chunking on
the quietest point near the ideal boundary. No gateway state.
"""
from __future__ import annotations

import math
import struct
import sys
from array import array
from typing import Iterable, Iterator

# Formato di uscita: mono 16kHz Opus 24kbit. 16kHz e' la frequenza di lavoro di
# whisper: campionare piu' alto non aggiunge informazione utile per l'ASR e
# costa byte.
TARGET_RATE = 16000
TARGET_LAYOUT = "mono"
OPUS_BITRATE = 24000
# Byte per campione nel PCM s16: intero a 16 bit, mono.
_BYTES_PER_SAMPLE = 2

# Tolleranza di silenzio per considerare "taglio pulito" un punto. Sotto
# questa soglia RMS il taglio e' accettato anche se non e' un silenzio: meglio
# una sillaba tagliata che un audio che non entra nel tetto.
SILENCE_RMS = 220.0
# Sotto questa durata (0,25s) i dati non sono parlato plausibile: e' la
# guardia contro i payload corrotti che il wrapper WAV per PCM grezzo
# accetterebbe altrimenti.
_MIN_PCM_BYTES = 8000
# Quanto un punto deve essere piu' silenzioso del confine bilanciato per
# spostarlo. 2% filtra il rumore numerico (su audio a volume costante due
# finestre differiscono di ~1e-6 relativo) senza perdere i silenzi veri, che
# hanno RMS vicino a zero.
_QUIET_IMPROVE_PCT = 0.02
# Finestra di analisi del taglio (ms): e' la granularita' con cui cerchiamo il
# punto piu' silenzioso.
_SCAN_WIN_MS = 20


class AudioConversionError(Exception):
    """Audio non decodificabile o non convertibile.

    Sollevata solo per errori REALI (formato sconosciuto, dati corrotti): il
    chiamante la trasforma in marker per l'LLM, mai in 503."""


def _av():
    """Import pigro di PyAV: l'assenza non deve rompere l'avvio del gateway."""
    try:
        import av  # noqa: PLC0415
    except Exception as e:                                   # noqa: BLE001
        raise AudioConversionError(
            f"PyAV non disponibile: {e}") from e
    return av


def decode_pcm(data: bytes, fmt_hint: str = "",
               sample_rate: int = TARGET_RATE) -> Iterator[bytes]:
    """Decodifica `data` in PCM s16 mono al sample rate richiesto.

    `fmt_hint` serve solo quando i byte sono PCM grezzo (nessun container
    riconoscibile): i client OpenAI mandano `input_audio.format` = wav/mp3/...
    con il base64. Su PCM grezzo assumiamo s16 LE alla frequenza data, che e'
    la convenzione di `input_audio`; se il file risulta corto e pari, e' WAV
    senza header e lo trattiamo come tale."""
    av = _av()
    import io                                                  # noqa: PLC0415
    payload = data
    # Rileva un WAV "a cappello" senza RIFF (base64 di solo dati PCM): alcuni
    # client lo fanno. 44 byte sono lo header canonico del WAV PCM.
    if (not data[:4] in (b"RIFF", b"OggS", b"fLaC", b"ID3", b"\x1a\x45\xdf\xa3")
            and len(data) % _BYTES_PER_SAMPLE == 0):
        payload = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
                   + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                 sample_rate * _BYTES_PER_SAMPLE,
                                 _BYTES_PER_SAMPLE, 16)
                   + b"data" + struct.pack("<I", len(data)) + data)
    try:
        container = av.open(io.BytesIO(payload))
    except Exception as e:                                     # noqa: BLE001
        raise AudioConversionError(
            f"formato audio non riconosciuto ({fmt_hint or 'sconosciuto'}"
            f"): {e}") from e
    try:
        if not container.streams.audio:
            raise AudioConversionError("nessun flusso audio nel file")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout=TARGET_LAYOUT,
                                      rate=sample_rate)
        produced = False
        total = 0
        for frame in container.decode(stream):
            for rf in resampler.resample(frame):
                produced = True
                chunk = bytes(rf.planes[0])
                total += len(chunk)
                yield chunk
        if not produced:
            raise AudioConversionError("decodifica vuota")
        if total < _MIN_PCM_BYTES:
            # Il wrapper WAV per PCM grezzo accetta QUALSIASI sequenza di byte
            # pari: senza questo guard un payload corrotto (o un'immagine
            # mandata come audio) diventerebbe un chunk di 0,06s che whisper
            # trascriverebbe a caso. Sotto ~0,25s non c'e' parlato plausibile.
            raise AudioConversionError(
                f"audio troppo corto ({total / (sample_rate * 2):.2f}s): "
                "i dati non sembrano audio")
    finally:
        try:
            container.close()
        except Exception:                                     # noqa: BLE001
            pass


def probe_pcm(data: bytes, fmt_hint: str = "",
              sample_rate: int = TARGET_RATE) -> tuple[int, int]:
    """(byte_totali, sample_rate_effettivo) del PCM decodificato.

    Il sample rate restituito e' quello REALE dei frame, non quello richiesto:
    un OGG/Opus si decodifica a 48 kHz anche se il contenuto e' mono 16 kHz, e
    dividere i byte per la frequenza sbagliata sbaglia la durata di un fattore
    3. Serve proprio per non gonfiare il tetto dei byte."""
    av = _av()
    import io                                                  # noqa: PLC0415
    payload = data
    if (not data[:4] in (b"RIFF", b"OggS", b"fLaC", b"ID3", b"\x1a\x45\xdf\xa3")
            and len(data) % _BYTES_PER_SAMPLE == 0):
        payload = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
                   + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                 sample_rate * _BYTES_PER_SAMPLE,
                                 _BYTES_PER_SAMPLE, 16)
                   + b"data" + struct.pack("<I", len(data)) + data)
    try:
        container = av.open(io.BytesIO(payload))
        if not container.streams.audio:
            raise AudioConversionError("nessun flusso audio nel file")
        stream = container.streams.audio[0]
        rate = int(stream.rate or sample_rate)
        total = 0
        for frame in container.decode(stream):
            # frame.rate e' la frequenza reale del frame
            total += frame.samples * _BYTES_PER_SAMPLE
        return total, rate
    except AudioConversionError:
        raise
    except Exception as e:                                     # noqa: BLE001
        raise AudioConversionError(
            f"formato audio non riconosciuto ({fmt_hint or 'sconosciuto'}"
            f"): {e}") from e
    finally:
        try:
            container.close()
        except Exception:                                     # noqa: BLE001
            pass


def encode_ogg_opus(pcm_chunks: Iterable[bytes],
                    sample_rate: int = TARGET_RATE) -> bytes:
    """Codifica PCM s16 mono in un OGG/Opus."""
    av = _av()
    import io                                                  # noqa: PLC0415
    out = io.BytesIO()
    container = av.open(out, "w", format="ogg")
    try:
        stream = container.add_stream("libopus", rate=sample_rate)
        stream.layout = TARGET_LAYOUT
        stream.bit_rate = OPUS_BITRATE
        first = True
        for pcm in pcm_chunks:
            if not pcm:
                continue
            frame = av.AudioFrame(format="s16", layout=TARGET_LAYOUT,
                                  samples=len(pcm) // _BYTES_PER_SAMPLE)
            frame.planes[0].update(pcm)
            frame.sample_rate = sample_rate
            frame.pts = 0 if first else None
            first = False
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        try:
            container.close()
        except Exception:                                     # noqa: BLE001
            pass
    data = out.getvalue()
    if not data:
        raise AudioConversionError("codifica OGG vuota")
    return data


def _rms(pcm: bytes) -> float:
    """RMS di un blocco PCM s16 LE, in Python puro.

    Nessun numpy: evita una dipendenza per una riga di matematica. Il costo
    e' ~29 ms per 3 minuti di audio, cioe' trascurabile rispetto alla rete.
    `array` con frombytes evita il parsing struct per campione."""
    n = len(pcm) // _BYTES_PER_SAMPLE
    if n <= 0:
        return 0.0
    a = array("h")
    a.frombytes(pcm[:n * _BYTES_PER_SAMPLE])
    if sys.byteorder == "big":
        a.byteswap()
    total = 0
    for v in a:
        total += v * v
    return math.sqrt(total / n)


def _rms_at(pcm: bytes, start: int, length: int) -> float:
    """RMS della finestra [start, start+length) (clippata)."""
    a = max(0, start)
    b = min(len(pcm), start + length)
    if b <= a:
        return 0.0
    return _rms(pcm[a:b])


def _looks_like_signal(pcm: bytes) -> bool:
    """True se il PCM contiene un segnale che puo' essere audio.

    Serve per il wrapper WAV del PCM grezzo, che accetta qualunque sequenza di
    byte pari: testo, riempitivi e PNG passerebbero come audio e whisper ne
    trascriverebbe l'invenzione. Distingue:
      - campioni tutti identici (riempitivo, DC) -> non audio;
      - ampiezza quasi nulla -> non audio;
      - otherwise -> segnale, che per il nostro scopo basta: la verifica piu'
        fine la fa comunque whisper, e rifiutare qui un audio vero sarebbe
        peggio che lasciarlo passare."""
    n = len(pcm) // _BYTES_PER_SAMPLE
    if n < 64:
        return False
    a = array("h")
    a.frombytes(pcm[:n * _BYTES_PER_SAMPLE])
    if sys.byteorder == "big":
        a.byteswap()
    lo = min(a)
    hi = max(a)
    return (hi - lo) >= 64


def split_pcm(pcm: bytes, target_sec: float, search_pct: float) -> list[bytes]:
    """Divide un PCM s16 mono in chunk di ~target_sec, tagliando sul silenzio.

    Ogni confine viene spostato sul punto PIU' SILENZIOSO entro +/-search_pct
    del confine bilanciato, con i vincoli:
      - mai tagli sul silenzio iniziale o finale del file;
      - mai un chunk finale sotto 1 secondo (meglio accorciare il penultimo
        che lasciare un frammento da 0,4s che whisper trascrive a caso);
      - tagli sempre all'interno del buffer, mai ai bordi.

    Restituisce i chunk in ordine: al chiamante basta riunirli."""
    total = len(pcm)
    if total <= 0:
        return []
    chunk_bytes = int(target_sec * TARGET_RATE * _BYTES_PER_SAMPLE)
    if total <= chunk_bytes:
        return [pcm]
    n_chunks = math.ceil(total / chunk_bytes)
    search = int(chunk_bytes * max(0.0, min(1.0, search_pct)))
    win = max(_BYTES_PER_SAMPLE,
              int(TARGET_RATE * _SCAN_WIN_MS / 1000) * _BYTES_PER_SAMPLE)
    min_tail = int(1.0 * TARGET_RATE * _BYTES_PER_SAMPLE)

    bounds: list[int] = []
    start = 0
    for i in range(n_chunks - 1):
        remaining = n_chunks - i
        target = start + (total - start) // remaining
        lo = max(start + win, target - search)
        hi = min(total - win, target + search)
        if hi <= lo:
            off = max(start + _BYTES_PER_SAMPLE,
                      min(target, total - min_tail))
        else:
            # Il bersaglio e' il confine BILANCIATO: si parte da lui e lo si
            # sposta SOLO se si trova un punto OGGETIVAMENTE piu' silenzioso.
            # La tolleranza NON e' un dettaglio: su audio a volume costante gli
            # RMS di due finestre diverse differiscono di ~1e-6 relativo (rumore
            # in virgola mobile). Senza soglia, la scansione insegue quelle
            # micro-differenze e trascina ogni taglio al bordo sinistro della
            # finestra: 4 minuti diventerebbero 1,4 + 2,6 invece di 2 + 2. Un
            # silenzio vero ha RMS vicino a zero, quindi un miglioramento del
            # 2% non perde mai i tagli buoni.
            best, best_rms = target, _rms_at(pcm, target, win)
            if best_rms > SILENCE_RMS:
                floor = best_rms * (1.0 - _QUIET_IMPROVE_PCT)
                off = lo
                while off <= hi:
                    r = _rms_at(pcm, off, win)
                    if r <= SILENCE_RMS or r < floor:
                        best_rms, best = r, off
                        if r <= SILENCE_RMS:
                            break          # silenzio vero: non serve di meglio
                    off += win
            off = best
        off = max(start + _BYTES_PER_SAMPLE, min(off, total - min_tail))
        # s16 = 2 byte per campione: un confine su offset dispari taglia a
        # meta' campione e l'encoder fallisce con "got N bytes; need N-1".
        off -= off % _BYTES_PER_SAMPLE
        if off <= start:
            break
        bounds.append(off)
        start = off
    if not bounds:
        return [pcm]
    out: list[bytes] = []
    prev = 0
    for b in bounds + [total]:
        out.append(pcm[prev:b])
        prev = b
    return [c for c in out if c]


def to_ogg_chunks(data: bytes, fmt_hint: str = "",
                  target_sec: float = 180.0, search_pct: float = 0.2,
                  sample_rate: int = TARGET_RATE) -> list[bytes]:
    """Chiave di ingresso: audio QUALSIASI -> lista di chunk OGG/Opus.

    Applica la regola di bilanciamento: se il file supera il chunk target si
    divide in `ceil(durata/target)` pezzi tagliati sul silenzio, e ogni pezzo
    viene codificato in OGG/Opus. Un file sotto il target produce un solo
    chunk. Solleva `AudioConversionError` se l'audio non e' decodificabile."""
    pcm_parts: list[bytes] = []
    for pcm in decode_pcm(data, fmt_hint=fmt_hint, sample_rate=sample_rate):
        pcm_parts.append(pcm)
    pcm = b"".join(pcm_parts)
    if not pcm:
        raise AudioConversionError("nessun campione decodificato")
    if not _looks_like_signal(pcm):
        raise AudioConversionError(
            "i dati non contengono un segnale audio (campioni costanti "
            "o ampiezza nulla): probabile payload non audio")
    pieces = split_pcm(pcm, target_sec, search_pct)
    out: list[bytes] = []
    for piece in pieces:
        out.append(encode_ogg_opus([piece], sample_rate=sample_rate))
    return out


def probe_duration(data: bytes, fmt_hint: str = "",
                   sample_rate: int = TARGET_RATE) -> float:
    """Durata in secondi di un audio, decodificandolo.

    Usa il sample rate REALE dei frame (`probe_pcm`): un OGG/Opus si decodifica
    a 48 kHz anche se il contenuto e' mono 16 kHz, e dividere per 16000
    sbaglierebbe la durata di un fattore 3."""
    total, rate = probe_pcm(data, fmt_hint=fmt_hint, sample_rate=sample_rate)
    if not rate:
        return 0.0
    return total / float(rate * _BYTES_PER_SAMPLE)
