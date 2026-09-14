"""Fallback sulle risposte TRONCATE dal modello (finish_reason=length).

`_length_truncated_should_fail` decide se una risposta con contenuto ma
finish_reason=length va trattata come errore (cooldown + rotazione). Guardia
importante: se il client ha chiesto `max_tokens` e il modello si e' fermato
esattamente li', e' il cap del client -> nessun cooldown (ruotare non cambia).
"""
from app.main import _length_truncated_should_fail
from app.policy import Policy


def test_disabilitato():
    assert _length_truncated_should_fail(
        True, 100, None, 50, False) is False


def test_non_troncato():
    assert _length_truncated_should_fail(
        False, 100, None, 50, True) is False


def test_zero_contenuto_non_gestito_qui():
    assert _length_truncated_should_fail(
        True, 0, None, 0, True) is False


def test_troncato_senza_max_client():
    assert _length_truncated_should_fail(
        True, 5309, None, 5309, True) is True


def test_troncato_ma_sotto_il_max_client():
    # max richiesto 8000, il modello si e' fermato a 5309 -> troncato vero.
    assert _length_truncated_should_fail(
        True, 5309, 8000, 5309, True) is True


def test_cap_del_client_nessun_cooldown():
    # il modello si e' fermato esattamente sul max_tokens del client.
    assert _length_truncated_should_fail(
        True, 400, 400, 400, True) is False
    assert _length_truncated_should_fail(
        True, 400, 400, 399, True) is False   # tolleranza -2


def test_usage_assente_tratta_come_troncato():
    assert _length_truncated_should_fail(
        True, 123, None, None, True) is True


def test_parse_policy_rotate_on_length_truncated():
    d = Policy.from_dict({})
    assert d.qc_sanity.rotate_on_length_truncated is False
    p = Policy.from_dict({"qc_sanity": {"rotate_on_length_truncated": True}})
    assert p.qc_sanity.rotate_on_length_truncated is True
    assert Policy.from_dict(
        {"qc_sanity": {"rotate_on_length_empty": True}}
    ).qc_sanity.rotate_on_length_truncated is False