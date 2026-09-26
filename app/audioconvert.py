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


# ------------------------------------------------------------------ magic
# Magic dei container reali. Attenzione alla LUNGHEZZA: `data[:4]` su un magic
# a 3 byte come ID3 non puo' MAI matchare (4 != 3), quindi la guardia non
# scattava e un mp3 con tag ID3 di lunghezza pari finiva avvolto come PCM.
# Ogni voce e' (offset, byte attesi) e i magic corti si confrontano con la
# lunghezza esatta per non creare falsi positivi sui primi byte di un PCM.
_MAGIC4 = (b"RIFF", b"OggS", b"fLaC", b"\x1a\x45\xdf\xa3")
_MAGIC3 = (b"ID3", b"ID4")
# Frame-sync MPEG: 11 bit a 1 (0xFF Ex/Fx) precedono ogni frame audio. Un
# PCM non puo' iniziare con 0xFFEx: e' un sincronizzatore, non un dato.
_MPEG_SYNC = 0xFF


def _looks_like_container(data: bytes) -> bool:
    """True se `data` e' un container audio riconoscibile (NON PCM grezzo).

    La versione precedente confrontava `data[:4]` contro una lista di magic
    di lunghezze diverse. Il buco vero era ISO-BMFF: mp4/m4a NON hanno un
    magic in testa, hanno un box di 4 byte (`00 00 00 xx`) seguito da `ftyp`
    a OFFSET 4. Non riconosciuti, questi file passavano la guardia SOLO se
    la lunghezza era dispari, e venivano avvolti come PCM s16: rumore che lo
    STT trascrive a caso (risposta cachata 1h) o "audio troppo corto".
    Misurato su m4a reali rigenerati: 39/69 (56,5%) di lunghezza pari.
    """
    if len(data) >= 4 and data[:4] in _MAGIC4:
        return True
    if len(data) >= 4 and data[4:8] == b"ftyp":       # ISO-BMFF: mp4/m4a/mov
        return True
    if len(data) >= 3 and data[:3] in _MAGIC3:          # ID3: 3 byte, non 4
        return True
    if len(data) >= 2 and data[0] == _MPEG_SYNC \
            and (data[1] & 0xE0) == 0xE0:             # frame-sync MPEG
        return True
    return False


def _pcm_plausible(data: bytes) -> bool:
    """True se `data`, avvolto come PCM s16, SOMIGLIA a un segnale audio.

    Seconda barriera oltre al magic: il wrapper WAV accetta qualunque
    sequenza di byte pari, e un m4a non riconosciuto produce rumore che
    `_looks_like_signal` ACCETTA (le code AAC hanno ampiezza anche a caso):
    serve una verifica che guardi i campioni, non il container.

    Cosa distingue un segnale da un payload non audio:
      - `hi - lo` ampio E NON tutto costante (`_looks_like_signal`): esclude
        fill, silenzio DC e un'immagine coi pixel quasi uniformi;
      - cambi di segno in un blocco iniziale: un segnale reale (speech/musica)
        passa da campioni positivi a negativi molte volte al secondo.

    LIMITE MISURATO, dichiarato per onesta': NON e' un discriminante fra
    container e PCM. Su 95 blob reali (m4a/mp4/ogg/webm/mp3, 19 durate da 0,5s
    a 60s) i metadati di un container NON vengono respinti da questo criterio,
    con soglia assoluta 256 come con soglia relativa peak//64: i bit di un
    payload audio codificato oscillano anche su valori piccoli. Serve quindi
    come rete di sicurezza contro i payload NON AUDIO (fill, DC, padding),
    non come riconoscitore di formato: e' la guardia magic a fare il grosso e
    questa seconda barriera non deve mai rifiutare un file vero.

    Soglia RELATIVA al picco (1/64, mai sotto 8): una soglia assoluta taglia
    fuori una voce sussurrata, che e' proprio il caso d'uso dello STT.
    """
    if not data:
        return False
    n = len(data) // _BYTES_PER_SAMPLE
    if n < 64 or len(data) % _BYTES_PER_SAMPLE:
        return False
    if not _looks_like_signal(data):
        return False
    a = array("h")
    a.frombytes(data[:n * _BYTES_PER_SAMPLE])
    if sys.byteorder == "big":
        a.byteswap()
    peak = max(abs(min(a)), abs(max(a)))
    thr = max(8, peak // 64)
    limit = min(n, 8192)
    prev_sign = 0
    flips = 0
    for v in a[:limit]:
        if -thr < v < thr:                   # zona morta: anti rumore numerico
            continue
        s = 1 if v > 0 else -1
        if prev_sign and s != prev_sign:
            flips += 1
        prev_sign = s
    return flips >= 4


def _raw_pcm_payload(data: bytes, sample_rate: int) -> bytes:
    """Ritorna `data` avvolto in un header WAV PCM s16 mono.

    Riusato dai due call site perche' la regola (quando avvolgere) deve
    essere IDENTICA fra `decode_pcm` e `probe_pcm`: una guardia diversa
    farebbe dire a una il m4a "2 secondi" e all'altra "0,04 secondi", e il
    tetto dei byte verrebbe calcolato sul valore sbagliato."""
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                          sample_rate * _BYTES_PER_SAMPLE,
                          _BYTES_PER_SAMPLE, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


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
    # La guardia deverecognoscere TUTTI i container (magic a 3/4 byte, ISO-BMFF
    # con `ftyp` a offset 4, frame-sync MPEG) E chiedere che i byte restanti
    # sembrino un segnale: vedi `_looks_like_container` / `_pcm_plausible`.
    if (not _looks_like_container(data)
            and len(data) % _BYTES_PER_SAMPLE == 0
            and _pcm_plausible(data)):
        payload = _raw_pcm_payload(data, sample_rate)
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
    # STESSA guardia di `decode_pcm`: se qui avvolgesse un m4a (rumore) ma
    # decode non lo facesse, probe_duration direbbe 0,04s di un file da 2s e
    # il tetto dei byte sarebbe calcolato sul valore sbagliato.
    if (not _looks_like_container(data)
            and len(data) % _BYTES_PER_SAMPLE == 0
            and _pcm_plausible(data)):
        payload = _raw_pcm_payload(data, sample_rate)
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
