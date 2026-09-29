# Handoff: lo que queda abierto tras la pieza 2A

**Fecha:** 2026-09-25
**Origen:** cierre de `feat/agent-conversational-runtime` (35 commits, 261 tests).
**Spec:** `docs/superpowers/specs/2026-09-24-agente-runtime-conversacional-design.md`
**Operación del agente:** `docs/agente.md`

La pieza 2A dejó al agente conversando y escribiendo con confirmación humana. Esto
es lo que quedó abierto, con la razón por la que quedó y lo que cuesta cerrarlo.
Sale de nueve revisiones por task más una de rama completa.

## Decisiones que son tuyas, no técnicas

**Firmar la huella.** Toda escritura muestra sus cifras y espera aprobación; esas
cifras cruzan la pausa como una huella que el panel devuelve. Hoy el servidor
confía en que el panel devuelve lo que recibió. Si el panel alguna vez la
**recalcula** en lugar de guardarla, la protección se desactiva y **el servidor no
puede detectarlo**. Un HMAC con un secreto del servidor convierte ese fallo
silencioso en uno ruidoso, sin cambiar lo que el panel hace. Cuesta un secreto más
que administrar. El momento barato es mientras el contrato del panel se escribe.

**El control no bloqueante de saldo negativo.** `AGENTS.md` §7 pide avisar antes de
dejar la caja en negativo. Hoy vive sólo en el prompt, y un prompt es una
instrucción que un modelo puede no seguir. La versión en código no debe bloquear —
rechazaría el caso legítimo que la regla describe, con el dueño poniendo plata de su
bolsillo — sino anotar: `PurchaseService.confirm()` calcula el saldo proyectado y lo
adjunta a la respuesta. Eso además protege a llamadores que no son conversaciones.

**Las dos filas históricas de `cost_basis_unit = 33.34`.** Siguen sin corregir desde
la pieza 1. Q0.01 en total.

## Huecos conocidos del runtime

**No hay autorización, sólo autenticación.** Cualquier usuario autenticado puede leer
y reanudar cualquier hilo, incluida una aprobación pendiente de otra persona. Con un
solo usuario no importa; con dos, sí.

**`langgraph dev` nunca se corrió contra esta rama.** La forma de la API de
autenticación está verificada leyendo el fuente de `langgraph-api` 0.15.1 línea por
línea, pero el camino completo token → fila firmada quiere una corrida manual. Es lo
primero que hay que hacer, y hay que mirar el contador de conexiones de Postgres
durante las primeras corridas.

**El mecanismo de despliegue no está determinado.** No hay `langgraph-cli` ni
`langgraph-api` en requirements, ni `Procfile`, ni `railway.json`. `docs/agente.md`
lo declara abierto y lista qué hay que decidir. Además esas versiones **no están
pineadas**, y toda la cadena de autenticación es sensible a la versión: 0.9.1 y
0.15.1 ya difieren.

**Idempotencia, residuo.** Una muerte del proceso entre las dos transacciones de
`registrar_compra` deja un borrador huérfano; la reanudación crea otro y lo confirma,
así que la compra queda registrada una vez, con su stock y su caja. `agent.tool_writes`
crece sin purga. Las invocaciones directas sin grafo no se deduplican.

**La marca de idempotencia depende del pin `version="v2"`** de `create_react_agent`,
y ningún test lo vigila directamente. Bajo `v1` los hermanos colisionan en una sola
clave y la segunda escritura legítima de un turno se saltearía en silencio. Una
aserción barata lo cerraría.

**La estabilidad de la clave entre procesos está probada, pero no por la suite.** Un
revisor la demostró con dos procesos reales; el test del repo ejercita uno solo, que
es una condición más débil que la que el mecanismo necesita.

## Bugs preexistentes que se encontraron y no se tocaron

**`SaleUpdate.date` sólo acepta `None`.** `date: Optional[date] = None` liga el nombre
`date` a `None` en el cuerpo de la clase, así que `PUT /sales/{id}` con una fecha
siempre da 422. Y con `{"date": null}` pasa la validación y escribe `None` en una
columna `nullable=False`: **500**. Se dejó intacto porque arreglarlo hace que un
endpoint vivo empiece a aceptar lo que hoy rechaza — decisión del dueño.

**`SaleRepository.update()` y `.delete()` siguen commiteando por dentro** mientras
`create()` ya no. Un llamador futuro que use `create()` directo vería la fila
desaparecer, porque `get_db()` sólo cierra y cerrar revierte.

**`SalePaymentStatusUpdate` no tiene `payment_method`,** así que una venta pagada sólo
por ese endpoint queda con el medio en `None`.

**El constraint duplicado** `ck_cash_movements_ck_cash_movements_type`, por la
convención de nombres de `Base.metadata`. Igual que su hermano de `payment_method`.

## Lo que hay que resolver del contrato del panel

Está todo en `docs/agente.md` § "El contrato del panel". Lo que no puede fallar: la
huella se guarda **opaca** y se devuelve **byte por byte**. Y el panel tiene que
mandar su token de Firebase, cosa que el spec siempre dijo y que la documentación
anterior a esta rama no mencionaba.
