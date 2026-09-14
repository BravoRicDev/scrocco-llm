"""Test per il troncamento cache-aware del contesto (app/ctxcompact.py)."""

from types import SimpleNamespace

from app.ctxcompact import (CtxCompactConfig, compact_tool_outputs,
                            create_ctxcompact_config,
                            ctxcompact_config_from_policy, should_compact)


def _tool(content):
    return {"role": "tool", "tool_call_id": "c", "content": content}


def _asst():
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": "c", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"}}]}


def _conversation(t1=5000, t2=5000, t3=5000):
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        _asst(), _tool("x" * t1),
        {"role": "user", "content": "u2"},
        _asst(), _tool("y" * t2),
        {"role": "user", "content": "u3"},
        _asst(), _tool("z" * t3),
    ]


class TestConfig:
    def test_defaults(self):
        c = CtxCompactConfig()
        assert c.enabled is True
        assert c.keep_turns == 4
        assert c.min_saved_tokens == 500
        assert "{n}" in c.stub_text
        assert c.min_ctx_tokens == 50000
        assert c.on_deployment_switch is True
        assert c.switch_min_tokens == 8000

    def test_from_dict(self):
        c = create_ctxcompact_config({"cache_aware": {"context_truncation": {
            "enabled": False, "keep_turns": 2, "max_tool_output_chars": 50,
            "min_saved_tokens": 10, "stub_text": "[cut {n}]"}}})
        assert c.enabled is False
        assert c.keep_turns == 2
        assert c.max_tool_output_chars == 50
        assert c.min_saved_tokens == 10
        assert c.stub_text == "[cut {n}]"

    def test_from_dict_triggers(self):
        c = create_ctxcompact_config({"cache_aware": {"context_truncation": {
            "min_ctx_tokens": 1234, "on_deployment_switch": False,
            "switch_min_tokens": 99}}})
        assert c.min_ctx_tokens == 1234
        assert c.on_deployment_switch is False
        assert c.switch_min_tokens == 99

    def test_from_policy(self):
        pol = SimpleNamespace(cache_ctx_truncation_enabled=True,
                              cache_ctx_keep_turns=2,
                              cache_ctx_max_tool_output_chars=100,
                              cache_ctx_min_saved_tokens=5,
                              cache_ctx_stub_text="[s {n}]")
        c = ctxcompact_config_from_policy(pol)
        assert c.keep_turns == 2
        assert c.max_tool_output_chars == 100
        assert c.stub_text == "[s {n}]"
        # default dei nuovi inneschi
        assert c.min_ctx_tokens == 50000
        assert c.on_deployment_switch is True
        assert c.switch_min_tokens == 8000


class TestShouldCompact:
    def test_disabled(self):
        d = should_compact(CtxCompactConfig(enabled=False), 999999)
        assert d["compact"] is False

    def test_overflow(self):
        d = should_compact(CtxCompactConfig(), 150000, max_in=100000)
        assert d["compact"] is True
        assert "overflow" in d["reason"]

    def test_abs_threshold(self):
        # Con l'isteresi anti-churn la soglia assoluta scatta solo avvicinandosi
        # alla saturazione della finestra del deployment (qui 90% di 1M).
        d = should_compact(CtxCompactConfig(), 900000, max_in=1000000,
                           holder="d1", dep_unique="d1")
        assert d["compact"] is True
        assert "abs" in d["reason"]

    def test_abs_deferred_with_headroom(self):
        # cache calda + ampio margine: NON riscrivere il prefisso (no churn).
        d = should_compact(CtxCompactConfig(), 60000, max_in=1000000,
                           holder="d1", dep_unique="d1")
        assert d["compact"] is False

    def test_below_all_thresholds(self):
        d = should_compact(CtxCompactConfig(), 5000, max_in=1000000)
        assert d["compact"] is False

    def test_switch_cold(self):
        d = should_compact(CtxCompactConfig(), 10000, max_in=1000000,
                           holder=None, dep_unique="d1")
        assert d["compact"] is True
        assert "switch" in d["reason"]
        assert d["cold"] is True

    def test_switch_holder_differs(self):
        d = should_compact(CtxCompactConfig(), 10000, max_in=1000000,
                           holder="other", dep_unique="d1")
        assert d["compact"] is True and "switch" in d["reason"]

    def test_no_switch_when_hot(self):
        d = should_compact(CtxCompactConfig(), 10000, max_in=1000000,
                           holder="d1", dep_unique="d1")
        assert d["compact"] is False
        assert d["cold"] is False

    def test_switch_below_min(self):
        d = should_compact(CtxCompactConfig(), 4000, max_in=1000000,
                           holder=None, dep_unique="d1")
        assert d["compact"] is False

    def test_switch_disabled(self):
        cfg = CtxCompactConfig(on_deployment_switch=False)
        d = should_compact(cfg, 10000, max_in=1000000, holder=None,
                           dep_unique="d1")
        assert d["compact"] is False

    def test_sticky(self):
        d = should_compact(CtxCompactConfig(), 1000, max_in=1000000,
                           session_compact=True)
        assert d["compact"] is True
        assert "sticky" in d["reason"]


class TestCompact:
    def test_stubs_only_old(self):
        msgs = _conversation()
        new, rep = compact_tool_outputs(msgs, CtxCompactConfig(keep_turns=2))
        assert rep["changed"] is True
        assert rep["stubbed"] == 1                 # solo il primo tool
        assert new[3]["content"].startswith("[tool output omesso")
        assert new[6]["content"] == "y" * 5000     # ultimi 2 turni intatti
        assert new[9]["content"] == "z" * 5000
        assert msgs[3]["content"] == "x" * 5000    # input non mutato

    def test_pairing_preserved(self):
        msgs = _conversation()
        new, _ = compact_tool_outputs(msgs, CtxCompactConfig(keep_turns=1))
        assert len(new) == len(msgs)
        assert [m["role"] for m in new] == [m["role"] for m in msgs]
        assert new[2].get("tool_calls") and new[3]["role"] == "tool"

    def test_idempotent(self):
        msgs = _conversation()
        cfg = CtxCompactConfig(keep_turns=2)
        once, rep1 = compact_tool_outputs(msgs, cfg)
        assert rep1["changed"] is True
        twice, rep2 = compact_tool_outputs(once, cfg)
        assert rep2["changed"] is False
        assert twice == once

    def test_small_output_untouched(self):
        msgs = _conversation(t1=50)
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=2, max_tool_output_chars=2000))
        assert rep["changed"] is False

    def test_min_saved_gate(self):
        msgs = _conversation(t1=800)
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=2, min_saved_tokens=100000))
        assert rep["changed"] is False
        assert new == msgs

    def test_no_user_message(self):
        msgs = [{"role": "system", "content": "s"}, _asst(), _tool("x" * 9000)]
        new, rep = compact_tool_outputs(msgs, CtxCompactConfig())
        assert rep["changed"] is False


# ============================================================ v2: head+tail
def _conv2(calls):
    """[user] + N×(assistant[bash cid_i] + tool(content_i)) + [user] finale."""
    msgs = [{"role": "user", "content": "inizio"}]
    for j, content in enumerate(calls):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{j}", "type": "function",
                                     "function": {"name": "bash",
                                                  "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{j}",
                     "content": content})
    msgs.append({"role": "user", "content": "fine"})
    return msgs


class TestConfigV2:
    def test_defaults_v2(self):
        c = CtxCompactConfig()
        assert c.head_chars == 600 and c.tail_chars == 600
        assert c.keep_tail_pct == 2.0 and c.keep_error_outputs is True

    def test_from_dict_v2(self):
        c = create_ctxcompact_config({"cache_aware": {"context_truncation": {
            "head_chars": 300, "tail_chars": 0, "keep_tail_pct": 0.4,
            "keep_error_outputs": False}}})
        assert c.head_chars == 300 and c.tail_chars == 0
        assert c.keep_tail_pct == 0.4 and c.keep_error_outputs is False

    def test_from_policy_v2(self):
        pol = SimpleNamespace(cache_ctx_head_chars=100, cache_ctx_tail_chars=50,
                              cache_ctx_keep_tail_pct=1.5,
                              cache_ctx_keep_error_outputs=False)
        c = ctxcompact_config_from_policy(pol)
        assert c.head_chars == 100 and c.tail_chars == 50
        assert c.keep_tail_pct == 1.5 and c.keep_error_outputs is False


class TestRichStub:
    def test_head_tail_deterministic_and_line_safe(self):
        lines = [f"riga-{i:04d} " + "x" * 60 for i in range(200)]
        body = "\n".join(lines) + "\n"          # ~13200 char
        msgs = _conv2([body, "piccolo"])
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=1, min_saved_tokens=10))
        content = new[2]["content"]
        assert content.startswith("[tool output omesso:")   # riga 1 legacy
        assert "[bash]" in content.splitlines()[1]          # summary riga 2
        assert "righe" in content
        assert "...[omessi " in content
        head_part = content.split("\n", 2)[2].split("...[omessi")[0]
        assert head_part.startswith("riga-0000")
        assert head_part.endswith("\n")                     # mai a meta' riga
        assert content.split("...[omessi ")[-1].split("]...\n", 1)[1]
        tail_part = content.split("]...\n", 1)[1]
        assert tail_part.startswith("riga-") and "\nriga-" in tail_part
        # puro: stesso input -> stessi byte
        new2, _ = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=1, min_saved_tokens=10))
        assert new2[2]["content"] == content
        assert rep["stubbed"] == 1

    def test_plain_stub_when_no_head_tail(self):
        msgs = _conv2(["y" * 9000])
        cfg = CtxCompactConfig(keep_turns=1, head_chars=0, tail_chars=0)
        new, rep = compact_tool_outputs(msgs, cfg, )
        assert new[2]["content"] == "[tool output omesso: 9000 caratteri]"
        assert rep["changed"] is True

    def test_exit_zero_visible_and_stubbed(self):
        body = ("output\n" * 300) + "exit code: 0\n"
        msgs = _conv2([body])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=10)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert rep["changed"] is True
        assert ", exit 0" in new[2]["content"]

    def test_errors_never_touched_even_overflow(self):
        corpo = ("Traceback (most recent call last):\n  File \"x.py\"\n"
                 + "z" * 9000)
        msgs = _conv2([corpo])
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=0, min_saved_tokens=0))
        assert rep["changed"] is False
        assert new[2]["content"] == corpo
        # ValueError/ENOSPC riconosciuti allo stesso modo
        msgs2 = _conv2(["bla\nValueError: boom\n" + "w" * 9000])
        new2, _ = compact_tool_outputs(
            msgs2, CtxCompactConfig(keep_turns=1, min_saved_tokens=0))
        assert new2[2]["content"] == msgs2[2]["content"]

    def test_error_guard_disattivabile(self):
        corpo = "ValueError: boom\n" + "q" * 9000
        msgs = _conv2([corpo])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=10,
                               keep_error_outputs=False)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert rep["changed"] is True

    def test_multimodal_content_intatta(self):
        big = [{"type": "text", "text": "t" * 9000},
               {"type": "image_url", "image_url": {"url": "dataright"}}]
        msgs = _conv2([big])
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=1, min_saved_tokens=10))
        assert new[2]["content"] == big

    def test_custom_stub_text_prima_riga(self):
        msgs = _conv2(["l" * 9000])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=10,
                               stub_text="[cut {n}]")
        new, _ = compact_tool_outputs(msgs, cfg)
        assert new[2]["content"].startswith("[cut 9000]")
        assert "...[omessi " in new[2]["content"]          # head+tail attivo


class TestDedup:
    def test_rimando_al_doppione(self):
        corpo = "ripeti\n" * 2000                          # 14000 char
        msgs = _conv2([corpo, corpo])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=10)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert rep["deduped"] == 1
        assert new[2]["content"].startswith("[rimando:")
        assert "c1" in new[2]["content"]                  # il piu' recente
        # il target (piu' vecchio di boundary? qui entrambi <boundary) viene
        # comunque stubbato: il rimando resta un puntatore valido
        assert new[4]["content"].startswith("[tool output omesso:")
        # idempotenza: il rimando non viene rielaborato
        new2, rep2 = compact_tool_outputs(new, cfg)
        assert rep2["changed"] is False

    def test_dedup_non_tocca_gli_errori(self):
        corpo = "Traceback x\n" + "e" * 9000
        msgs = _conv2([corpo, corpo])
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=1, min_saved_tokens=0))
        assert rep["deduped"] == 0 and rep["changed"] is False


class TestDynamicBoundary:
    def _three_big_turns(self, per_turn=6):
        """user + [per_turn x (asst+tool 4k char)] + user, ripetuto x3 turni."""
        body = "out\n" * 1000                              # 4000 char
        msgs = [{"role": "user", "content": "u0"}]
        k = 0
        for t in range(3):
            for _ in range(per_turn):
                msgs.append({"role": "assistant", "content": "",
                             "tool_calls": [{"id": f"c{k}", "type": "function",
                                             "function": {"name": "bash",
                                                          "arguments": "{}"}}]})
                msgs.append({"role": "tool", "tool_call_id": f"c{k}",
                             "content": body})
                k += 1
            msgs.append({"role": "user", "content": f"u{t + 1}"})
        return msgs

    def _stubbed_between(self, msgs, lo, hi):
        return sum(1 for i, m in enumerate(msgs)
                   if lo <= i < hi and m.get("role") == "tool"
                   and isinstance(m.get("content"), str)
                   and m["content"].startswith("[tool output omesso"))

    def test_budget_stringe_dentro_keep_turns(self):
        msgs = self._three_big_turns()
        users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        b_user = users[-2]                    # keep_turns=2 -> penultimo user
        cfg = CtxCompactConfig(keep_turns=2, min_saved_tokens=0,
                               keep_tail_pct=1.0)          # 1% di 400k = 4000tok
        new, rep = compact_tool_outputs(msgs, cfg, max_in=400000)
        assert rep["boundary"] > b_user        # pota DENTRO i turni tenuti
        assert self._stubbed_between(new, b_user, rep["boundary"]) >= 1
        # la coda dentro budget resta integra
        for i in range(rep["boundary"], len(new)):
            if new[i].get("role") == "tool":
                assert new[i]["content"].startswith("out")

    def test_budget_generoso_non_prune_piu_di_keep_turns(self):
        msgs = self._three_big_turns()
        users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        cfg = CtxCompactConfig(keep_turns=2, min_saved_tokens=0,
                               keep_tail_pct=50.0)         # 200k tok: tutto
        new, rep = compact_tool_outputs(msgs, cfg, max_in=400000)
        assert rep["boundary"] == users[-2]    # max(B_user, 0) = B_user
        assert self._stubbed_between(new, 0, len(msgs)) >= 1

    def test_budget_disattivato_usa_keep_turns(self):
        msgs = self._three_big_turns()
        users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        cfg = CtxCompactConfig(keep_turns=2, min_saved_tokens=0,
                               keep_tail_pct=0.0)
        new, rep = compact_tool_outputs(msgs, cfg, max_in=400000)
        assert rep["boundary"] == users[-2]

    def test_floor_8_messaggi(self):
        msgs = self._three_big_turns(2)       # 16 msg: floor 8
        n = len(msgs)
        cfg = CtxCompactConfig(keep_turns=3, min_saved_tokens=0,
                               keep_tail_pct=0.001)        # budget ~0
        new, rep = compact_tool_outputs(msgs, cfg, max_in=100000)
        assert rep["boundary"] == n - 8        # floor: gli ultimi 8 integri
        for i in range(n - 8, n):
            if new[i].get("role") == "tool":
                assert new[i]["content"].startswith("out")


class TestEstimator:
    def test_saved_uses_estimator(self):
        msgs = _conv2(["m" * 9000])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=50)
        new, rep = compact_tool_outputs(
            msgs, cfg, estimator=lambda ms: sum(len(str(m)) for m in ms) // 100)
        # //100 e' piu' severo del //4: il gate 50 puo' bloccare il //4-only?
        # qui 9000//100=90 > 50 -> passa, e il numero NON e' saved_chars//4
        assert rep["changed"] is True
        assert rep["saved_tokens_est"] != rep["saved_chars"] // 4

    def test_gate_estimator_severo(self):
        msgs = _conv2(["n" * 3000])
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=100)
        new, rep = compact_tool_outputs(
            msgs, cfg, estimator=lambda ms: sum(len(str(m)) for m in ms) // 1000)
        assert rep["changed"] is False
        assert new == msgs


# ============================================================= WATERMARK FRONTIERA
class TestBoundaryFloor:
    def _fat(self):
        """6 messaggi di testa (user...) + 12 coppie asst/tool grosse: la
        walk a budget strette stringe la frontiera FINO a 22, una finestra
        enorme la riporta a 5 (keep_turns=1 = ultimo user)."""
        corpo = "riga\n" * 1000                             # 4000 char
        msgs = [{"role": "user", "content": f"u{i}"} for i in range(6)]
        for k in range(12):
            msgs.append({"role": "assistant", "content": "a", "tool_calls": [
                {"id": f"q{k}", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"q{k}",
                         "content": corpo})
        return msgs

    def test_floor_non_rigredisce_la_frontiera(self):
        """Finestra piccola: la walk pruna fino a 22. Rotazione su finestra
        grande: senza floor la frontiera INDIETREGGIA (gli stub 6..21
        tornerebbero originali: byte diversi a meta' prefisso). Con il floor
        (watermark) i byte restano identici -> cache della famiglia salva."""
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=1)
        msgs = self._fat()
        new1, rep1 = compact_tool_outputs(msgs, cfg, max_in=30_000)
        assert rep1["changed"] and rep1["boundary"] == 22
        # finestra enorme: la walk non morde, varrebbe solo keep_turns (=5)
        bare, rep0 = compact_tool_outputs(msgs, cfg, max_in=1_000_000)
        assert rep0["boundary"] < rep1["boundary"]
        assert bare != new1                                 # il bug
        # con il floor della sessione: niente regressione, byte identici
        new2, rep2 = compact_tool_outputs(msgs, cfg, max_in=1_000_000,
                                          boundary_floor=rep1["boundary"])
        assert rep2["boundary"] == 22
        assert new2 == new1

    def test_floor_ignora_valori_stupidi(self):
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=1)
        new, rep = compact_tool_outputs(self._fat(), cfg, boundary_floor=0)
        assert rep["boundary"] == 5                           # solo keep_turns
        new, rep = compact_tool_outputs(self._fat(), cfg, boundary_floor=999)
        assert rep["boundary"] == len(self._fat())            # clamp a len


# ============================================================== RIMANDO STABILE
class TestRimandoStabile:
    CORPO = "doppione\n" * 1200

    def _msgs(self, prefix=()):
        return list(prefix) + [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x1", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "content": self.CORPO},       # no tool_call_id!
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x2", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "content": self.CORPO},        # no cid, identico
            {"role": "user", "content": "u2"},
        ]

    def test_msg_ref_indipendente_dagli_indici(self):
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=1)
        _, rep_a = compact_tool_outputs(self._msgs(), cfg)
        shifted = self._msgs(prefix=[{"role": "user", "content": "extra"},
                                     {"role": "assistant", "content": "eh"},
                                     {"role": "user", "content": "u0"}])
        _, rep_b = compact_tool_outputs(shifted, cfg)
        assert rep_a["deduped"] == rep_b["deduped"] == 1
        ra = next(m["content"] for m in self._out(cfg) if str(m["content"]).startswith("[rimando"))
        rb = self._out2(cfg, shifted)
        assert ra == rb

    # helper separati per estrarre il rimando
    def _out(self, cfg):
        new, _ = compact_tool_outputs(self._msgs(), cfg)
        return new

    def _out2(self, cfg, shifted):
        new, _ = compact_tool_outputs(shifted, cfg)
        return next(m["content"] for m in new
                    if str(m["content"]).startswith("[rimando"))

    def test_rimando_contiene_hash_non_indice(self):
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=1)
        new, _ = compact_tool_outputs(self._msgs(), cfg)
        r = next(m["content"] for m in new
                 if str(m["content"]).startswith("[rimando"))
        assert "msg@" in r and "msg@5" not in r      # niente indice nudo


# ============================================================= ERRORI (anchor)
class TestErrorAnchors:
    def _one(self, corpo):
        return [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c", "content": corpo},
            {"role": "user", "content": "u2"},
        ]

    def test_pytest_failed_e_git_fatal_protetti(self):
        cfg = CtxCompactConfig(keep_turns=0, min_saved_tokens=0)
        for corpo in ("FAILED tests/test_x.py::test_y\n" + "z" * 6000,
                      "ERROR: network unreachable\n" + "z" * 6000,
                      "fatal: not a git repository\n" + "z" * 6000):
            new, rep = compact_tool_outputs(self._one(corpo), cfg)
            assert rep["stubbed"] == 0, corpo[:20]

    def test_parole_normali_non_anchorate_passano(self):
        cfg = CtxCompactConfig(keep_turns=0, min_saved_tokens=0)
        corpo = ("il test log FAILED e fatal: compaiono solo perche' il "
                 "test e' passato\n" + "q" * 6000)
        # "FAILED" e "fatal:" sono a meta' riga: NON anchor -> comprimibile
        new, rep = compact_tool_outputs(self._one(corpo), cfg)
        assert rep["stubbed"] == 1

    def test_FAILED_a_inizio_riga_protetto(self):
        cfg = CtxCompactConfig(keep_turns=0, min_saved_tokens=0)
        corpo = "q" * 6000 + "\nFAILED hard\n" + "q" * 6000
        new, rep = compact_tool_outputs(self._one(corpo), cfg)
        assert rep["stubbed"] == 0


# ==================================================================== ARGS (B)
class TestArgsTruncation:
    def _conv(self, args):
        return [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "w1", "type": "function",
                 "function": {"name": "write_file", "arguments": args}}]},
            {"role": "tool", "tool_call_id": "w1", "content": "ok"},
            {"role": "user", "content": "u2"},
        ]

    def _cfg(self, **kw):
        k = dict(keep_turns=0, min_saved_tokens=0,
                 tool_args_max_chars=500)
        k.update(kw)
        return CtxCompactConfig(**k)

    def test_stringa_lunga_tagliata_json_valido(self):
        import json
        args = json.dumps({"path": "/tmp/x", "content": "CODICE\n" * 500})
        new, rep = compact_tool_outputs(self._conv(args), self._cfg())
        assert rep["args_trimmed"] == 1
        out = new[1]["tool_calls"][0]["function"]["arguments"]
        obj = json.loads(out)                       # JSON SEMPRE valido
        assert obj["path"] == "/tmp/x"
        assert "omessi" in obj["content"]
        assert len(out) < len(args)
        assert rep["tools"].get("write_file")

    def test_non_parseable_intatto(self):
        args = "{" + "x" * 4000                    # JSON spazzatura
        new, rep = compact_tool_outputs(self._conv(args), self._cfg())
        assert rep["args_trimmed"] == 0
        assert new[1]["tool_calls"][0]["function"]["arguments"] == args

    def test_solo_stringhe_corte_intatto(self):
        import json
        args = json.dumps({"a": "x" * 3000, "b": "y"})   # il campo > 500? no: soglia per-valore? 3000>500 -> taglia
        # NB: soglia per-stringa = tool_args_max_chars -> "x"*3000 viene
        # tagliata; qui verifichiamo che un args SOPRA soglia ma SENZA
        # stringhe sopra soglia resti byte-identico (no re-dump inutile)
        args2 = json.dumps({"a": ["z" * 100] * 10, "n": 42})
        assert len(args2) > 500
        new, rep = compact_tool_outputs(self._conv(args2), self._cfg())
        assert rep["args_trimmed"] == 0
        assert new[1]["tool_calls"][0]["function"]["arguments"] == args2

    def test_idempotenza_args(self):
        import json
        args = json.dumps({"content": "print(1)\n" * 400})
        cfg = self._cfg()
        new1, r1 = compact_tool_outputs(self._conv(args), cfg)
        new2, r2 = compact_tool_outputs(new1, cfg)
        assert r1["args_trimmed"] == 1
        assert r2["changed"] is False or new2[1] == new1[1]

    def test_disattivato(self):
        import json
        args = json.dumps({"content": "C\n" * 3000})
        new, rep = compact_tool_outputs(
            self._conv(args), self._cfg(tool_args_max_chars=0))
        assert rep["args_trimmed"] == 0 and new[1] is not None
        assert new[1]["tool_calls"][0]["function"]["arguments"] == args


# ================================================================= GIÀ COMPRESSO
class TestRimandoOnesto:
    def test_tag_gia_compresso(self):
        corpo = "uguali\n" * 1500
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a", "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": corpo},
            {"role": "assistant", "content": "b", "tool_calls": [
                {"id": "t2", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t2", "content": corpo},
            {"role": "user", "content": "u2"},
        ]
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=1)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert "(già compresso)" in new[2]["content"]
        assert "t2" in new[2]["content"]

    def test_legacy_stub_secco_non_promette_compressione(self):
        corpo = "uguali\n" * 1500
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "tool", "tool_call_id": "t1", "content": corpo},
            {"role": "tool", "tool_call_id": "t2", "content": corpo},
            {"role": "user", "content": "u2"},
        ]
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=1,
                               head_chars=0, tail_chars=0)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert "(già compresso)" not in new[1]["content"]


# ==================================================================== REPORT
class TestReportV2:
    def test_contatori_per_tool(self):
        corpo = "lento\n" * 3000
        msgs = _conv2([corpo, "altro\n" * 3000]) if False else None
        # costruzione esplicita: due tool grandi di tool diversi
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a", "tool_calls": [
                {"id": "b1", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "b1", "content": corpo},
            {"role": "assistant", "content": "a", "tool_calls": [
                {"id": "s1", "type": "function",
                 "function": {"name": "search_files", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "s1", "content": "y" * 8000},
            {"role": "user", "content": "u2"},
        ]
        cfg = CtxCompactConfig(keep_turns=1, min_saved_tokens=1)
        new, rep = compact_tool_outputs(msgs, cfg)
        assert rep["stubbed"] == 2
        assert rep["tools"].get("bash") == 1
        assert rep["tools"].get("search_files") == 1


# ========================================================== REASONING HEADROOM
class TestReasoningHeadroom:
    def test_compatta_prima_sui_modelli_che_pensano(self):
        cfg = CtxCompactConfig(min_ctx_tokens=1, abs_headroom_ratio=0.8,
                               reasoning_headroom_ratio=0.7)
        # ctx = 0.75 della finestra: storico NO, reasoning SI'
        assert should_compact(cfg, 7500, 10000)["compact"] is False
        assert should_compact(cfg, 7500, 10000,
                              reasoning=True)["compact"] is True

    def test_ratio_a_zero_non_cambia_niente(self):
        cfg = CtxCompactConfig(min_ctx_tokens=1, abs_headroom_ratio=0.8,
                               reasoning_headroom_ratio=0.0)
        assert should_compact(cfg, 7500, 10000, reasoning=True)["compact"] \
            is False
