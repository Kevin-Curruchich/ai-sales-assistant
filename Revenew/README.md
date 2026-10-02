# Export de Fleet — referencia histórica

Esto es el agente Revenew tal como vivía en Fleet, con Google Sheets como base de
datos, Slack como canal y Google Calendar para agendar. **Ese runtime ya no
existe.**

Se conserva versionado porque `AGENTS.md` y las nueve skills son la descripción
original del dominio: el costeo FIFO, las fórmulas de margen, el vocabulario de
caja, la detección de precio habitual y la proyección de próxima compra. Sirven
para contrastar si alguna vez se sospecha que una regla se perdió al traducirla.

**La fuente viva es `app/agent/prompt.py::SYSTEM_PROMPT`**, no estos archivos.
El prompt traduce estas reglas a las diez herramientas reales y, donde el código
cambió, sigue al código. Dos diferencias conocidas y deliberadas:

- `registrar-compra` aquí describe un modelo de un paso con `monto_aporte_propio`.
  El código usa dos llamadas: `registrar_compra` y, sólo si un socio pone el
  dinero, `registrar_movimiento_caja` con el `compra_id`.
- El vocabulario de caja del código tiene un quinto tipo, `saldo_inicial`, que no
  existe acá.

Si se cambia una regla de negocio, se cambia en el prompt. Estos archivos no se
leen en tiempo de ejecución y un contenedor desplegado no los necesita.

`config.json` y `tools.json` quedaron fuera del repo a propósito: no describen el
dominio, sólo identificadores de la plataforma Fleet (tenant, organización,
proveedor de OAuth de Slack). Siguen en disco para quien los necesite.
