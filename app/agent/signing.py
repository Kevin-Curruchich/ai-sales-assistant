"""Firma de la huella.

La huella es lo unico que cruza la pausa de `interrupt()`: LangGraph re-ejecuta
la tarea desde el principio al reanudar, asi que todo lo demas se recalcula
despues de la pausa y no es "lo que la persona vio".

Sin firma, el servidor solo puede comparar el valor que el panel devuelve
contra el estado actual de la base.  No puede verificar que ese valor haya
salido de el.  Un panel que RECALCULE la huella en vez de guardarla apaga la
proteccion **en silencio**: todo parece funcionar y ya nadie esta comparando
contra lo que se aprobo.

Con firma, esa falla se vuelve ruidosa la primera vez que alguien aprueba algo.

El panel no cambia: la firma viaja DENTRO del mismo valor opaco que ya tenia
que guardar y devolver tal cual.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

SECRETO_ENV = "AGENT_HUELLA_SECRET"


class HuellaSinSecreto(RuntimeError):
    """No hay secreto para firmar.

    Se levanta en vez de degradar a "sin firma": un control de seguridad que
    se apaga solo cuando falta su configuracion no es un control.
    """


def _secreto() -> bytes:
    valor = os.environ.get(SECRETO_ENV, "")
    if not valor:
        raise HuellaSinSecreto(
            f"Falta {SECRETO_ENV}. La huella de aprobacion se firma con ese "
            "secreto y sin el no se puede verificar que una aprobacion haya "
            "salido de este servidor. Definilo en el entorno del agente."
        )
    return valor.encode("utf-8")


def _canonico(datos) -> bytes:
    """Bytes estables para los mismos datos.

    `sort_keys` y separadores sin espacios: dos estructuras iguales tienen que
    dar la misma firma sin importar el orden en que se armaron los dicts.
    """
    return json.dumps(
        datos, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _hmac(datos) -> str:
    return hmac.new(_secreto(), _canonico(datos), hashlib.sha256).hexdigest()


def firmar(datos):
    """El sobre que viaja en el payload de `interrupt()`."""
    return {"datos": datos, "firma": _hmac(datos)}


def verificar(huella) -> tuple[bool, object]:
    """`(valida, datos)`.

    `valida` es False si el sobre no tiene la forma esperada o si la firma no
    corresponde -- incluida una huella del contrato viejo, que era una lista
    pelada sin firma.  `compare_digest` para no filtrar por tiempo cuanto
    coincide un intento.
    """
    if not isinstance(huella, dict):
        return (False, None)
    datos = huella.get("datos")
    firma = huella.get("firma")
    if datos is None or not isinstance(firma, str):
        return (False, None)
    return (hmac.compare_digest(_hmac(datos), firma), datos)
