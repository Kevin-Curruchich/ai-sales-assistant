import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class AgentThreadCreate(BaseModel):
    title: Optional[str] = None


class AgentThreadRename(BaseModel):
    title: str


class AgentThreadResponse(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Menciones y comandos del composer (POST /agent/stream) ---

Comando = Literal["venta", "compra", "cobro", "caja"]
TipoMencion = Literal["cliente", "producto", "venta"]


class Mencion(BaseModel):
    """Una entidad que la persona menciono con `@` en el composer.

    `inicio`/`fin` son el rango `[inicio, fin)` de la mencion dentro de
    `mensaje`, incluida la `@`, contado en code points (lo que cuenta `len` en
    Python), no en unidades UTF-16 como `String.length` en JavaScript. El `id`
    no se valida contra la base: si no existe, la herramienta que lo use
    responde `no_encontrada`.
    """

    tipo: TipoMencion
    id: uuid.UUID
    nombre: str
    inicio: int = Field(ge=0)
    fin: int

    @model_validator(mode="after")
    def _rango_no_vacio(self):
        if self.fin <= self.inicio:
            raise ValueError("'fin' tiene que ser mayor que 'inicio'")
        return self


def validar_menciones(mensaje: str, menciones: list[Mencion]) -> None:
    """Cada rango cae dentro de `mensaje` y ninguno se superpone con otro."""
    largo = len(mensaje)
    for mencion in menciones:
        if mencion.fin > largo:
            raise ValueError(
                f"La mencion {mencion.nombre!r} termina en {mencion.fin}, fuera del "
                f"mensaje ({largo} code points)"
            )
    ordenadas = sorted(menciones, key=lambda m: m.inicio)
    for anterior, siguiente in zip(ordenadas, ordenadas[1:]):
        if siguiente.inicio < anterior.fin:
            raise ValueError(
                f"Las menciones {anterior.nombre!r} y {siguiente.nombre!r} se superponen"
            )
