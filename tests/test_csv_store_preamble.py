"""FIX 3 — `csv_store.load_table` e il preambolo `#` dell'example.

Prima della fix l'header veniva preso da `raw[0]`, cioe' dalla PRIMA riga
fisica del file. `var/keys_rotation.csv.example` ha 45 righe di commento
prima dell'intestazione: la admin API rispondeva 400 e
`GET /admin/deployments` elencava righe-commento come deployment.

VINCOLO CRITICO (test `TestInlineCommentsAreNOTSkipped`): i commenti si
saltano SOLO prima dell'header. Nell'exexample le righe

    # local-whisper,...,stt
    # local-tts-it,...,tts

sono deployment REALI commentati (disabilitati di default, non preambolo).
Skippandole un successivo `save_table` le distruggerebbe perche' riscrive il
file senza commenti: 2 deployment persi.
"""
import os

os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")

import pytest

from app import csv_store
from app.csv_store import CsvStoreError

HEADER = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
          "scrocco-llm-myteam,caps")

PREAMBLE = "\n".join(
    ["# Chiavi di rotazione per il gateway scrocco-llm",
     "# Colonne: commento, modello, provider, endpoint, ...",
     "#",
     "# Colonna opzionale \"caps\": membership ai gruppi capacita'.",
     '#   "" vuoto = solo testo (dims); "vision" = -vision/-vision-go;',
     ""])


def _csv(preamble_lines=3, tail=""):
    body = "\n".join(f"# riga di preambolo {i}" for i in range(preamble_lines))
    return (body + "\n" + HEADER + "\n"
            "t@x,m-a,groq,https://a.test/v1,free,128,8000,0,sk-FAKE-A,,\n"
            "t@x,m-b,groq,https://b.test/v1,free,128,8000,0,sk-FAKE-B,,\n"
            + tail)


class TestPreambleSkipped:
    def test_header_viene_dalle_colonne_reali(self, tmp_path):
        p = tmp_path / "k.csv"
        p.write_text(_csv())
        header, rows = csv_store.load_table(p)
        assert header[0] == "commento"
        assert header[1] == "modello"
        assert "caps" in header

    def test_nessuna_riga_commento_diventa_deployment(self, tmp_path):
        p = tmp_path / "k.csv"
        p.write_text(_csv(preamble_lines=45))
        _header, rows = csv_store.load_table(p)
        assert len(rows) == 2
        for r in rows:
            assert r["modello"] in ("m-a", "m-b")

    def test_righe_vuote_prima_dell_header_anche(self, tmp_path):
        p = tmp_path / "k.csv"
        p.write_text("\n\n\n" + _csv(preamble_lines=2))
        header, rows = csv_store.load_table(p)
        assert header[1] == "modello"
        assert len(rows) == 2

    def test_csv_solo_commenti_raises(self, tmp_path):
        p = tmp_path / "k.csv"
        p.write_text("# solo\n# commenti\n\n# e vuoti\n")
        with pytest.raises(CsvStoreError):
            csv_store.load_table(p)

    def test_csv_vuoto_raises(self, tmp_path):
        p = tmp_path / "k.csv"
        p.write_text("")
        with pytest.raises(CsvStoreError):
            csv_store.load_table(p)

    def test_senza_preambolo_comportamento_invariato(self, tmp_path):
        """Nessun commento: l'header e' raw[0] come prima, stesso risultato."""
        p = tmp_path / "k.csv"
        p.write_text(HEADER + "\n" "t@x,m-a,groq,https://a.test/v1,free,"
                                  "128,8000,0,sk-FAKE-A,,\n")
        header, rows = csv_store.load_table(p)
        assert header[1] == "modello"
        assert len(rows) == 1


class TestInlineCommentsAreNOTSkipped:
    """Il vincolo: DOPO l'header i commenti sono dati, non preambolo."""

    # righe 54 e 57 di var/keys_rotation.csv.example, verbatim nella forma
    WHISPER = ("# local-whisper,Systran/faster-whisper-base,speaches,"
               "http://speaches:8000/v1,free,128,0,10,placeholder-key,stt")
    TTS = ("# local-tts-it,speaches-ai/piper-it_IT-paola-medium,speaches,"
           "http://speaches:8000/v1,free,128,0,10,placeholder-key,tts")

    def _example_tail(self):
        return self.WHISPER + "\n" + self.TTS + "\n"

    def test_commenti_inline_sono_righe(self, tmp_path):
        p = tmp_path / "k.csv"
        p.write_text(_csv(preamble_lines=10, tail=self._example_tail()))
        _header, rows = csv_store.load_table(p)
        assert len(rows) == 4, "2 dati + 2 deployment commentati"
        # `# local-whisper` sta nella PRIMA colonna (commento), il modello
        # vero nella seconda: skippando la riga spariscono entrambi.
        assert rows[2]["commento"] == "# local-whisper"
        assert rows[2]["modello"] == "Systran/faster-whisper-base"
        assert rows[2]["caps"] == "stt"
        assert rows[3]["commento"] == "# local-tts-it"
        assert rows[3]["modello"] == "speaches-ai/piper-it_IT-paola-medium"
        assert rows[3]["caps"] == "tts"

    def test_id_stabili_anche_per_commenti_inline(self, tmp_path):
        p = tmp_path / "k.csv"
        p.write_text(_csv(preamble_lines=10, tail=self._example_tail()))
        header, rows = csv_store.load_table(p)
        ids = [csv_store.row_id(r, csv_store.endpoint_of(header, r))
               for r in rows]
        assert len(set(ids)) == 4, ids

    def test_roundtrip_non_distrugge_i_deployment_commentati(self, tmp_path):
        """load -> save -> reload: i 2 deployment commentati devono TORNARE,
        identici. Se load_table li skippasse, save_table riscriverebbe il
        file senza di loro: dati distrutti in modo irreversibile."""
        p = tmp_path / "k.csv"
        p.write_text(_csv(preamble_lines=10, tail=self._example_tail()))
        cfg = csv_store.GatewayConfig  # solo per il tipo: serve `like`
        header, rows = csv_store.load_table(p)
        assert len(rows) == 4

        # `like` minimo: serve un GatewayConfig con gli attributi che
        # save_table->_validate_like_live usa (proxy_prefix/go_suffix/
        # fallback_suffix/extra_prefixes).
        class _Like:
            proxy_prefix = "scrocco-llm-"
            go_suffix = "-go"
            fallback_suffix = "-fallback"
            extra_prefixes = []
        del cfg
        csv_store.save_table(p, header, rows, _Like())

        header2, rows2 = csv_store.load_table(p)
        assert len(rows2) == 4, "roundtrip: i deployment commentati sono persi"
        assert rows2 == rows
        # e i byte sono tornati (il preambolo no, save_table non lo scrive:
        # non e' un dato, ma i DATI devono essere integri)
        assert self.WHISPER in p.read_text()
        assert self.TTS in p.read_text()

    def test_riga_tutto_commento_dopo_header_ma_senza_dati_vuota(self, tmp_path):
        """Una riga commento SENZA campi (tipo `# nota inline`) resta una
        riga: viene contata, non skippata. Il contratto e' 'non skippare i
        commenti inline', non 'riconoscere i deployment'."""
        p = tmp_path / "k.csv"
        p.write_text(_csv(preamble_lines=2, tail="# nota inline\n"))
        _h, rows = csv_store.load_table(p)
        assert len(rows) == 3
        # e' l'unico campo della riga, quindi finisce nella PRIMA colonna
        assert rows[2]["commento"] == "# nota inline"


class TestExampleFileDelRepo:
    """Il file reale, se presente: 45 righe di preambolo + 2 commentati."""

    EXAMPLE = "var/keys_rotation.csv.example"

    def test_example_reale(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), self.EXAMPLE)
        if not os.path.exists(path):
            pytest.skip("esempio non presente")
        header, rows = csv_store.load_table(path)
        assert header[0] == "commento"
        assert header[-1] == "api_style"
        # 4 righe dati (51-... le prime 4 non commentate) + 7 righe commento
        # dopo l'header: 2 sono deployment reali (whisper, tts), 5 sono note
        # di spiegazione. Il vincolo impone di NON skippare nessuna delle 7:
        # skippandole si perderebbero i 2 deployment commentati.
        real = [r for r in rows
                if not (r.get("commento") or "").lstrip().startswith("#")]
        commented = [r for r in rows
                     if (r.get("commento") or "").lstrip().startswith("#")]
        assert len(real) == 4, [r.get("modello") for r in real]
        assert len(commented) == 7, [r.get("commento") for r in commented]
        # i 2 deployment commentati, per intero. `# local-whisper` e' il
        # valore della PRIMA colonna (commento): `modello` ne contiene il
        # nome del modello vero e proprio.
        deps = [r for r in rows
                if (r.get("commento") or "").startswith("# local-")]
        assert len(deps) == 2, [d.get("commento") for d in deps]
        assert {d["commento"].split(",")[0] for d in deps} == {
            "# local-whisper", "# local-tts-it"}
        assert {d["caps"] for d in deps} == {"stt", "tts"}
        assert {d["modello"] for d in deps} == {
            "Systran/faster-whisper-base",
            "speaches-ai/piper-it_IT-paola-medium"}
