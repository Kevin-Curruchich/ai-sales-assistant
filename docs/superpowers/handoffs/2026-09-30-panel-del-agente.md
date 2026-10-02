# Encargo: el panel del agente

**Fecha:** 2026-09-30 · **Estado del backend:** terminado y mergeado a `main`.

> **Esto es un encargo, no un registro.** Describe trabajo que todavía no
> existe. Si lo leés mucho después de la fecha de arriba, verificá contra el
> código antes de confiar: la autoridad es `docs/agente.md` en el repo del
> backend, y este archivo es un resumen para arrancar sin leerlo entero.

## Qué hay que construir

Un panel web para conversar con el agente de ventas: un chat donde la persona
escribe en lenguaje natural («vendí dos cartones a Aurita»), el agente responde
en streaming, y **cada escritura se confirma con una tarjeta** antes de tocar la
base de datos. Varias conversaciones (hilos) por usuario, cada una con su
historial.

El negocio es una venta de huevos. Los usuarios son el dueño y un puñado de
personas de confianza; no hay roles.

## El backend ya está listo

Servido en proceso por FastAPI, un solo servicio en Railway. 326 tests. Los
endpoints de abajo están en `main` y desplegados.

**No hace falta tocar el backend para construir el panel.** Si aparece algo que
el contrato no cubre, es un hallazgo: anotalo y preguntá antes de inventar una
forma nueva.

## Cinco cosas que se rompen en silencio si se hacen mal

Esto va primero porque son las que no avisan.

1. **La huella se devuelve byte por byte, nunca recalculada.** Ver «Responder una
   confirmación». Un panel que pide un preview fresco al aprobar y manda *ese*
   como huella desactiva la protección entera y no se nota hasta el día que
   importa.
2. **No toda `confirmacion` trae huella.** `registrar_movimiento_caja` y
   `registrar_cobro` no mandan ninguna. Un panel que exija `huella` se rompe con
   el primer aporte de caja o el primer cobro.
3. **`interrupt_id` es obligatorio para responder**, y va como campo hermano de
   `decision`, no anidado adentro.
4. **No se puede usar `EventSource`** ni el `useStream` del SDK de LangGraph para
   React. Ver «El cliente SSE».
5. **Un hilo corre de a una corrida.** Un segundo pedido sobre el mismo hilo
   mientras hay uno en vuelo recibe 409. El panel tiene que deshabilitar el
   input, no reintentar.

## Autenticación

El mismo token de Firebase que ya usa el resto de la app, en cada request:

```
Authorization: Bearer <id token de Firebase>
```

No hay credencial nueva ni segundo login. Un token ausente, vencido o inválido
es **401**.

El backend resuelve el usuario desde ese token y **nunca** desde el cuerpo del
pedido: si mandás `user_id` o `configurable` en el body se descartan en silencio.
No intentes pasar identidad por ahí; no va a funcionar y no es un bug.

## Los endpoints

Base: `/api/v1/agent`

| Método | Ruta | Para qué |
|---|---|---|
| `GET` | `/threads` | Lista los hilos del usuario |
| `POST` | `/threads` | Crea un hilo. Body: `{"title": "..."}` o `{}` |
| `GET` | `/threads/{id}` | Título y fechas de un hilo |
| `PATCH` | `/threads/{id}` | Renombra. Body: `{"title": "..."}` |
| `DELETE` | `/threads/{id}` | Borra el hilo. 204 |
| `GET` | `/threads/{id}/state` | **La conversación guardada y las confirmaciones abiertas** |
| `POST` | `/stream` | Corre un turno. Devuelve `text/event-stream` |

Un hilo de otro usuario, o inexistente, es **404** en todos. Es el mismo 404 a
propósito: distinguir «no existe» de «no es tuyo» le confirmaría a un tercero
que ese hilo existe.

`POST /threads` con `{}` crea el hilo con el título `"Conversación nueva"`, y el
primer mensaje lo reemplaza por sus primeros caracteres. El panel no necesita
titular nada.

`/stream` **nunca** crea un hilo: hay que crearlo antes y mandar su `thread_id`.

### `GET /threads/{id}/state`

Es lo que hace que un refresh no rompa nada. Devuelve:

```json
{
  "mensajes": [
    {"rol": "usuario", "texto": "vendí dos cartones a Aurita"},
    {"rol": "herramienta", "nombre": "previsualizar_venta"},
    {"rol": "asistente", "texto": "Son Q50. ¿Confirmás?"}
  ],
  "confirmaciones_pendientes": [
    {"tipo": "confirmar_venta", "preview": {"...": "..."},
     "huella": {"datos": "...", "firma": "..."},
     "interrupt_id": "05094bc42458644e29e114d15a75e3ae"}
  ]
}
```

Los tres `rol` son `usuario`, `asistente` y `herramienta` (ésta con `nombre` en
vez de `texto`). Los resultados internos de las herramientas y el prompt del
sistema no aparecen: no son conversación.

**Cada confirmación pendiente tiene exactamente la misma forma que el `data` del
evento `confirmacion`.** Eso es deliberado: usá **un solo componente** para la
tarjeta, venga del stream o de esta carga.

Un hilo que nunca corrió devuelve las dos listas vacías, no un 404. Una
confirmación ya respondida no aparece.

**Llamalo al montar el hilo y al reconectar después de perder el stream.** Es la
única fuente de la huella si el panel perdió su estado.

## El cliente SSE

`POST /stream` devuelve `text/event-stream`. **No podés usar `EventSource`**: esa
API sólo hace `GET` y no deja poner cabeceras, y acá hace falta `POST` con
`Authorization`. Tampoco aplica el `useStream` del SDK de LangGraph para React:
ese hook habla el protocolo del *servidor* LangGraph, que este backend
deliberadamente dejó de exponer cuando el agente pasó a servirse en proceso.

Hay que leerlo a mano con `fetch`:

```js
const resp = await fetch("/api/v1/agent/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
  body: JSON.stringify({ thread_id, mensaje }),
  signal: abortController.signal,
});

if (!resp.ok) { /* 401 / 404 / 409 / 422 / 503 -- ver abajo */ }

const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const bloques = buffer.split("\n\n");
  buffer = bloques.pop();               // el último puede estar incompleto
  for (const bloque of bloques) {
    const evento = bloque.match(/^event: (.+)$/m)?.[1];
    const data = JSON.parse(bloque.match(/^data: (.+)$/m)[1]);
    // despachar por `evento`
  }
}
```

Es poco código, pero no es la API que uno buscaría primero. Dos cuidados: el
último bloque del buffer puede venir partido (por eso el `pop()`), y usá un
`AbortController` para cortar al desmontar — **cuando el cliente se va, el
backend cancela la corrida de verdad** y deja de gastar modelo, así que abortar
es lo correcto y no un descuido.

Los códigos de estado salen **antes** del primer byte. Una vez que empezó el
stream el `200 OK` ya salió, así que un fallo a mitad del turno llega como evento
`error`, no como código HTTP.

## Los cinco eventos

```
event: token
data: {"texto": "Vendiste medio"}
```
Lo que el panel escribe, en pedazos. Concatenar.

```
event: herramienta
data: {"nombre": "previsualizar_venta", "estado": "llamando"}
```
Para mostrar «consultando lotes…» en vez de una pantalla quieta. Es el tramo
largo: ahí corre FIFO contra la base. Las diez herramientas son
`buscar_cliente`, `buscar_producto`, `consultar_caja`, `consultar_seguimiento`,
`consultar_ventas`, `previsualizar_venta`, `registrar_venta`,
`registrar_compra`, `registrar_movimiento_caja` y `registrar_cobro`.

```
event: confirmacion
data: {"tipo": "confirmar_venta", "interrupt_id": "c97a81a8...", "preview": {...}, "huella": {"datos": ..., "firma": "..."}}
```
La tarjeta. Los `tipo` son `confirmar_venta`, `confirmar_compra`,
`confirmar_movimiento_caja` y `confirmar_cobro`. Los dos últimos **no traen
`huella`**: `confirmar_movimiento_caja` trae `movimiento` en vez de `preview`, y
`confirmar_cobro` trae `cobro` (`venta_id`, `cliente`, `fecha_venta`, `total`,
`fecha_pago`, `medio_pago`).

```
event: fin
data: {"estado": "completo"}    // o "pausado"
```
`pausado` significa que hay una confirmación esperando. `completo`, que el turno
terminó.

```
event: error
data: {"mensaje": "Hubo un problema y no se registró nada. Intentá de nuevo."}
```
Mensaje fijo y humano, sin internos. La causa queda en el log del servidor. Es
el último evento del stream: no viene un `fin` después.

**Un turno termina en exactamente un evento terminal** (`fin` o `error`), nunca
los dos. La única excepción es deliberada: si el cliente se desconecta no recibe
ninguno, porque no hay nadie escuchando.

## Responder una confirmación

```json
{
  "thread_id": "<uuid>",
  "interrupt_id": "<el que vino en el evento confirmacion>",
  "decision": {"accion": "aprobar", "huella": <la huella de ese mismo evento, tal cual>}
}
```

`interrupt_id` y `decision` son **hermanos**, no anidados: `decision` es el
payload que lee la herramienta, `interrupt_id` es transporte.

Tres acciones:

- `{"accion": "aprobar", "huella": ...}` — escribe. Para
  `confirmar_movimiento_caja` y `confirmar_cobro`, `{"accion": "aprobar"}` sin
  huella.
- `{"accion": "cancelar"}` — no escribe nada. Nunca lleva huella.
- `{"accion": "corregir", "valores": {"cantidad": "3"}}` — sólo los campos que
  cambian. **Vuelve a pausar** con una huella nueva, no reanuda la conversación:
  el panel recibe otra `confirmacion` y vuelve a mostrar la tarjeta.

### La regla de la huella

**Guardala opaca y devolvela idéntica.** No la interpretes, no la reformatees, no
la recalcules. La recibís, la guardás como venga (estado de React, lo que sea), y
la reenviás igual al aprobar.

**Por qué importa tanto:** la huella es lo que prueba que las cifras que la
persona aprobó son las que el servidor le mostró. Si el panel, en vez de guardar
la huella recibida, pide un preview fresco al momento de aprobar y manda *ése*,
la comparación del servidor va a coincidir siempre — contra lo que el panel acaba
de calcular, no contra lo que la persona vio. **Ningún cambio real de inventario
entre el preview y la aprobación se detectaría nunca.** El servidor no puede
distinguir eso de un echo honesto. Es un bug que no aparece en pruebas casuales.

## Dos confirmaciones a la vez

Un mensaje del modelo puede pedir dos escrituras. Entonces llegan **dos eventos
`confirmacion`** (cada uno con su `interrupt_id`) y **un solo `fin`** con
`pausado`.

**Se responden de a una, en pedidos separados.** No hay forma de responder las
dos en el mismo `POST`. Al responder una, la otra se vuelve a anunciar con una
`confirmacion` nueva en el stream de *ese* pedido — con el mismo id, pero leelo
del evento nuevo en vez de cachearlo. El ciclo es: leer las que llegan, responder
una, leer las que vuelven.

## Los códigos, y qué hace el panel con cada uno

| Código | Qué pasó | Qué hace el panel |
|---|---|---|
| **401** | Token ausente, vencido o inválido | Renovar el token de Firebase y reintentar |
| **404** | El hilo no existe o no es tuyo | Volver a la lista de hilos |
| **409** | Dos significados, distinguibles por el texto del `detail` | Ver abajo |
| **422** | Ni `mensaje` ni `decision`, o `decision` sin `interrupt_id` | Bug del panel |
| **503** | El agente no arrancó en el servidor | Mostrar que no está disponible; no reintentar en loop |

El **409** es el que necesita cuidado, porque significa dos cosas:

- *«Ya hay una corrida en curso para este hilo»* — el panel mandó dos pedidos
  encima. Deshabilitá el input mientras un turno está en vuelo; no reintentes.
- *«Esa confirmación no está pendiente»* — el `interrupt_id` es viejo, de otra
  pausa, o de una que ya se respondió (un doble click en «aprobar», una pestaña
  vieja). El `detail` nombra los ids que sí están abiertos. Recargá el estado del
  hilo con `/state`, que además es lo único que trae la huella.

**Mientras una confirmación esté abierta, el único pedido que el hilo acepta es
responderla.** Mandar un `mensaje` en ese momento es 409 — y no es capricho: sin
ese rechazo, un mensaje ahí **destruía el hilo de forma permanente**. Para cambiar
de tema hay que cancelar la confirmación primero. En la interfaz eso significa
que con una tarjeta en pantalla el input de texto va deshabilitado, con
«Cancelar» como la salida.

## Lo que el panel NO tiene que manejar

Las herramientas devuelven estados como `recalculado`, `ya_registrado`,
`aprobacion_sin_huella` o `huella_de_otra_operacion`. **Nunca llegan al panel como
eventos.** Son resultados internos que el modelo lee y explica en sus propias
palabras, así que salen como `token`. No hay que ramificar por ellos.

Si estás depurando y ves al agente decir «el inventario cambió» en cada
aprobación, el sospechoso número uno es la regla de la huella: el panel no está
devolviendo la que recibió.

## Decisiones que todavía no están tomadas

Estas son del dueño del producto, no del código. Preguntá antes de resolverlas
solo:

1. **Qué pasa cuando la persona quiere cambiar de tema con una tarjeta abierta.**
   Hoy el backend exige cancelar primero. La alternativa —que un mensaje cancele
   implícitamente la pausa— no está implementada y nadie la decidió.
2. **Si una confirmación pendiente debería expirar.** La huella no tiene
   vencimiento: una tarjeta de hace tres días se puede aprobar, y el servidor
   recalcula el inventario en ese momento y rechaza si cambió. Puede estar bien;
   nadie lo decidió.
3. **Cómo se ven `corregir` y los lotes FIFO en la tarjeta.** El `preview` trae
   los lotes consumidos y las advertencias; cuánto de eso mostrar es diseño.

## Fuera de alcance, declarado

Sin paginado del historial: las conversaciones largas están fuera de alcance en
el spec, y la salida es abrir un hilo nuevo. Sin compartir hilos entre usuarios
(diferido a propósito). Sin roles dentro del agente. Sin notificaciones push ni
bandeja proactiva — eso es la pieza 2B, otro encargo.

## Lo que este documento no decide

El framework, el manejo de estado y los estilos del panel. Son del repo donde se
implemente. Si ese repo ya existe, seguí sus convenciones en vez de traer las de
acá.

## Dónde está la autoridad

En el repo del backend, `docs/agente.md`, secciones:

- «El contrato del panel: el token de Firebase»
- «El contrato del panel: cargar un hilo»
- «El contrato del panel: los eventos SSE»
- «Responder una confirmación: el cuerpo de `POST /stream`»
- «El contrato del panel: la huella» — leer completa antes de implementar la
  tarjeta
- «Idempotencia: una escritura por tarea, y lo que queda afuera»

El código: `app/api/v1/endpoints/agent.py` (endpoints),
`app/agent/streaming.py` (los eventos), `app/agent/historial.py` (lo que devuelve
`/state`), `app/agent/tools/write.py` (las tres confirmaciones).

Si algo de este resumen contradice a `docs/agente.md`, gana `docs/agente.md` — y
avisá, porque significa que este encargo quedó viejo.
