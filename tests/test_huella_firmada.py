"""La huella lleva firma del servidor.

Sin firma, el servidor solo podia comparar el valor que el panel devolvia
contra el estado actual: no podia verificar que ese valor hubiera salido de el.
Un panel que RECALCULE la huella en vez de guardarla apagaba la comparacion en
silencio, y era indetectable del lado del servidor.
"""

import os

import pytest

from app.agent.signing import (
    SECRETO_ENV,
    HuellaSinSecreto,
    firmar,
    verificar,
)

CIFRAS = [{"cost_basis_unit": "33.33", "subtotal": "18.00"}]


def test_una_huella_propia_verifica():
    valida, datos = verificar(firmar(CIFRAS))
    assert valida
    assert datos == CIFRAS


def test_el_panel_sigue_devolviendo_un_valor_opaco():
    """El sobre es lo que el panel guarda y devuelve: no tiene que entenderlo."""
    huella = firmar(CIFRAS)
    assert set(huella) == {"datos", "firma"}
    assert verificar(huella)[0]


def test_alterar_las_cifras_invalida_la_firma():
    huella = firmar(CIFRAS)
    huella["datos"] = [{"cost_basis_unit": "10.00", "subtotal": "18.00"}]
    assert verificar(huella)[0] is False


def test_alterar_la_firma_la_invalida():
    huella = firmar(CIFRAS)
    huella["firma"] = "0" * 64
    assert verificar(huella)[0] is False


def test_una_huella_recalculada_por_el_panel_no_verifica():
    """El caso que motiva todo esto: el panel arma el sobre por su cuenta."""
    inventada = {"datos": CIFRAS, "firma": "lo-que-sea"}
    assert verificar(inventada)[0] is False


def test_el_contrato_viejo_sin_firma_no_verifica():
    """Antes la huella era la lista pelada.  Ahora no alcanza."""
    assert verificar(CIFRAS)[0] is False


@pytest.mark.parametrize("basura", [None, "", 0, [], "una-cadena", {"datos": CIFRAS}])
def test_formas_que_no_son_un_sobre(basura):
    assert verificar(basura)[0] is False


def test_el_orden_de_las_claves_no_cambia_la_firma():
    a = firmar({"b": "2", "a": "1"})
    b = firmar({"a": "1", "b": "2"})
    assert a["firma"] == b["firma"]


def test_sin_secreto_levanta_en_vez_de_degradar(monkeypatch):
    """Un control que se apaga cuando falta su configuracion no es un control."""
    monkeypatch.delenv(SECRETO_ENV, raising=False)
    with pytest.raises(HuellaSinSecreto):
        firmar(CIFRAS)
    with pytest.raises(HuellaSinSecreto):
        verificar({"datos": CIFRAS, "firma": "x" * 64})


def test_otro_secreto_no_verifica_la_huella_ajena(monkeypatch):
    huella = firmar(CIFRAS)
    monkeypatch.setenv(SECRETO_ENV, "otro-secreto-distinto")
    assert verificar(huella)[0] is False
