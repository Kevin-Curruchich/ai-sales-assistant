# Multi-tenant con RLS — Plan de implementacion

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Varios comercios en la misma base y despliegue, aislados por Row-Level Security de Postgres.

**Architecture:** Cada tabla de negocio lleva `business_id` con `DEFAULT` desde la variable de sesion `app.business_id` y una politica RLS que filtra por ella. Un listener `after_begin` de SQLAlchemy fija esa variable en cada transaccion a partir de `session.info`, que llenan el request (`get_current_user`), el agente, el worker y los scripts. La zona horaria del comercio viaja en `session.info["business_tz"]`.

**Tech Stack:** FastAPI, SQLAlchemy 2 (sync), Alembic, Postgres, Firebase Admin SDK, LangGraph, pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-multi-tenant-rls-design.md`

## Global Constraints

- Variable de sesion: `app.business_id`. Expresion en politicas y DEFAULT: `nullif(current_setting('app.business_id', true), '')::uuid` (tras un reset la variable vale `''`, no NULL).
- Politica: `tenant_isolation`, con `USING` y `WITH CHECK` identicos; RLS con `ENABLE` + `FORCE`.
- Rol de grupo: `app_tenant` (`NOLOGIN NOSUPERUSER NOBYPASSRLS`), creado por la migracion si no existe. El usuario LOGIN de la API se crea a mano como miembro (runbook, Task 8).
- Tablas con RLS (11): `customers`, `products`, `purchases`, `purchase_items`, `sales`, `sale_items`, `sale_item_lot_allocations`, `cash_movements`, `follow_ups`, `customer_product_cycles`, `agent_threads`. Sin RLS: `businesses`, `users`.
- `session.info` keys: `"business_id"` (`uuid.UUID`), `"business_tz"` (`str`, zona IANA).
- Roles de usuario validos: `admin`, `sales_rep`, `viewer`. Signup crea `admin`; el admin crea `sales_rep` o `viewer`.
- Usuario de otro comercio o recurso ajeno: **404**. Usuario de Firebase sin fila: **403** con detalle `"Tu cuenta no pertenece a ningun comercio"`.
- Sin comercio en la sesion, `business_tz(db)` lanza `MissingBusinessContext`; nunca cae a `settings.BUSINESS_TIMEZONE`.
- El enlace de contrasena nunca se escribe en logs.
- Comentarios y mensajes en espanol, como el resto del codigo.

## Review Focus

1. **Commit a mitad de request**: los servicios hacen `commit()` y siguen consultando; la variable debe seguir fijada en la transaccion siguiente (test en Task 1).
2. **Conexion como superusuario**: los tests de aislamiento deben correr con `SET ROLE app_tenant`, o pasan siempre (fixture `tenant_session` en Task 2; guard de arranque en Task 8).
3. **Thread del agente de otro comercio**: `/agent/threads/{id}`, `/state` y `/stream` deben dar 404 antes de tocar el checkpointer (test en Task 6).
4. **Fallo de BD despues de crear el usuario en Firebase**: no puede quedar un usuario de Firebase huerfano (tests en Task 5).
5. **Dia local distinto por comercio**: a las 03:00 UTC, Guatemala y Madrid tienen fechas distintas; `business_today` debe respetar la del comercio (test en Task 3).

---

### Task 1: Contexto de comercio en la sesion

**Files:**
- Modify: `app/core/database.py`
- Test: `tests/test_business_context.py`

**Interfaces:**
- Produces:
  - `bind_business(db: Session, business_id: uuid.UUID, timezone: str) -> None` en `app/core/database.py`: guarda ambas keys en `db.info` y, si la sesion ya esta en transaccion, ejecuta `select set_config('app.business_id', :id, true)` en ella.
  - Listener `@event.listens_for(Session, "after_begin")` (clase `sqlalchemy.orm.Session`, no `SessionLocal`, para cubrir sesiones de tests y worker) que hace el mismo `set_config` si `session.info` tiene `business_id`.

- [ ] **Step 1: Write the failing tests** (usan `db_session`; no necesitan tablas nuevas)

```python
def _current(db):
    return db.execute(text("select current_setting('app.business_id', true)")).scalar()

def test_bind_sets_variable_in_open_transaction(db_session):
    bid = uuid.uuid4()
    db_session.execute(text("select 1"))  # abre transaccion
    bind_business(db_session, bid, "America/Guatemala")
    assert _current(db_session) == str(bid)

def test_variable_survives_commit(db_session):
    bid = uuid.uuid4()
    bind_business(db_session, bid, "America/Guatemala")
    db_session.commit()
    assert _current(db_session) == str(bid)

def test_unbound_session_has_no_business(db_session):
    assert _current(db_session) in (None, "")
```

- [ ] **Step 2:** `pytest tests/test_business_context.py -v` → FAIL (`bind_business` no existe).
- [ ] **Step 3:** Implementar `bind_business` y el listener en `app/core/database.py`, junto a `set_search_path`.
- [ ] **Step 4:** `pytest tests/test_business_context.py -v` → PASS.
- [ ] **Step 5:** Commit `feat: Bind the current business to every DB transaction`.

---

### Task 2: Modelo `Business`, migracion y RLS

**Files:**
- Create: `app/models/business.py`, `alembic/versions/<rev>_multi_tenant_rls.py` (`down_revision = "5c2e8f1a9b3d"`; verificar que siga siendo el head al empezar)
- Modify: `app/models/__init__.py`, `app/models/user.py` y los modelos de las 11 tablas (`business_id`), `app/models/product.py` (`sku`), `app/models/purchase.py` (`reference_number`), `tests/fixtures_domain.py`, `tests/conftest.py`
- Test: `tests/test_tenant_isolation_db.py`, `tests/test_migrations.py` (agregar caso)

**Interfaces:**
- Consumes: `bind_business` (Task 1).
- Produces:
  - `Business` (`app/models/business.py`): `id: uuid`, `name: str(255)`, `timezone: str(64)` default `"America/Guatemala"`, `google_calendar_id: Optional[str(255)]`, `created_at`. `User.business` relationship.
  - En cada modelo con `business_id`: `mapped_column(Uuid, ForeignKey("businesses.id"), nullable=False, server_default=text("nullif(current_setting('app.business_id', true), '')::uuid"))`. `users.business_id` lleva el mismo DEFAULT (asi `User(...)` sin comercio sigue funcionando en tests y fixtures).
  - Uniques: `UniqueConstraint("business_id", "sku")` y `UniqueConstraint("business_id", "reference_number")`; se quita `unique=True` de las columnas.
  - Fixture `db_session` (modificada): crea un `Business` y llama `bind_business` antes de entregarla. Fixture nueva `seeded_business` devuelve ese comercio. Fixture nueva `other_business` crea un segundo comercio (sin bindear).
  - Fixture nueva `tenant_session(migrated_schema)` en `tests/fixtures_domain.py`: sesion sobre el mismo schema cuyo `connect` ejecuta tambien `SET ROLE app_tenant`.

**Migracion (orden):**
1. `create_table("businesses")`; insertar un comercio `name="Comercio principal"`, `timezone=settings.BUSINESS_TIMEZONE`, `google_calendar_id=settings.GOOGLE_CALENDAR_ID`.
2. Para `users` y las 11 tablas: `add_column` nullable → `UPDATE ... SET business_id = <id>` → `NOT NULL` → `server_default` → FK.
3. Reemplazar los uniques de `products.sku` y `purchases.reference_number`.
4. `DO $$ ... IF NOT EXISTS (select from pg_roles where rolname='app_tenant') THEN CREATE ROLE app_tenant NOLOGIN NOSUPERUSER NOBYPASSRLS; ...$$`; `GRANT USAGE` sobre `current_schema()`, `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES`, `GRANT USAGE ON ALL SEQUENCES`, y `ALTER DEFAULT PRIVILEGES` equivalentes (con `format('%I', current_schema())` dentro de un `DO`).
5. Por cada una de las 11 tablas: `ENABLE` + `FORCE ROW LEVEL SECURITY` y `CREATE POLICY tenant_isolation`.
- `downgrade`: orden inverso; no borra el rol (lo comparten otros schemas).

- [ ] **Step 1: Write the failing tests** en `tests/test_tenant_isolation_db.py`

```python
def test_tenant_sees_only_its_business(db_session, tenant_session, seeded_business, other_business):
    db_session.add(Customer(name="De A"))           # business_id por DEFAULT = A
    db_session.commit()
    bind_business(db_session, other_business.id, other_business.timezone)
    db_session.add(Customer(name="De B"))
    db_session.commit()
    bind_business(tenant_session, seeded_business.id, seeded_business.timezone)
    assert [c.name for c in tenant_session.query(Customer)] == ["De A"]

def test_tenant_without_business_sees_nothing(db_session, tenant_session, seeded_business):
    db_session.add(Customer(name="De A")); db_session.commit()
    assert tenant_session.query(Customer).count() == 0

def test_tenant_cannot_write_into_other_business(tenant_session, seeded_business, other_business):
    bind_business(tenant_session, seeded_business.id, seeded_business.timezone)
    tenant_session.add(Customer(name="x", business_id=other_business.id))
    with pytest.raises(ProgrammingError):  # WITH CHECK
        tenant_session.commit()

def test_same_sku_allowed_in_two_businesses(...):  # dos Product con el mismo sku, uno por comercio → ambos se guardan
```

  En `tests/test_migrations.py`: `test_existing_rows_move_to_first_business` — migrar a `5c2e8f1a9b3d`, insertar un customer y un user con SQL, `upgrade head`, comprobar que ambos tienen el `business_id` del unico registro de `businesses`.

- [ ] **Step 2:** `pytest tests/test_tenant_isolation_db.py tests/test_migrations.py -v` → FAIL.
- [ ] **Step 3:** Implementar modelos, migracion y fixtures descritos arriba.
- [ ] **Step 4:** `pytest -q` (suite completa) → PASS, incluido `tests/test_metadata.py` (sin deriva de autogenerate).
- [ ] **Step 5:** Commit `feat: Isolate business data per tenant with Postgres RLS`.

---

### Task 3: Zona horaria por comercio

**Files:**
- Modify: `app/core/datetime_utils.py`, `app/schemas/sale.py`, `app/services/sales/orchestrator.py`, `app/repositories/sale_repository.py`, `app/services/customer_service.py`, `app/services/product_service.py`, `app/services/purchase_service.py`, `app/services/follow_up_service.py`, `app/api/v1/endpoints/cash.py`, y sus tests existentes (`tests/test_business_timezone.py`, `tests/test_business_today_everywhere.py`)
- Test: `tests/test_business_timezone.py`

**Interfaces:**
- Consumes: `db.info["business_tz"]` (Task 1).
- Produces (todas en `app/core/datetime_utils.py`, `db` como primer parametro):
  `business_tz(db) -> ZoneInfo`, `business_today(db) -> date`, `business_midnight(db, day)`, `business_instant(db, day)`, `business_end_of_day(db, day)`, `to_business_tz(db, value)`, `format_business_date(db, value)`, `format_business_datetime(db, value)`, y `class MissingBusinessContext(RuntimeError)`.
- Las herramientas del agente y `prompt_runtime` se migran en Task 6.

- [ ] **Step 1: Write the failing tests**

```python
def test_today_follows_business_timezone(db_session, monkeypatch):
    # 2026-10-26 03:00 UTC: Guatemala sigue en el 25, Madrid ya esta en el 26
    freeze_utc(monkeypatch, datetime(2026, 10, 26, 3, tzinfo=timezone.utc))
    db_session.info["business_tz"] = "America/Guatemala"
    assert business_today(db_session) == date(2026, 10, 25)
    db_session.info["business_tz"] = "Europe/Madrid"
    assert business_today(db_session) == date(2026, 10, 26)

def test_missing_timezone_raises():
    with pytest.raises(MissingBusinessContext):
        business_tz(SessionLocal())

def test_sale_create_leaves_occurred_at_unset():
    assert SaleCreate(customerId=uuid.uuid4(), date=date(2026, 10, 1), items=[]).occurredAt is None
```

  (`freeze_utc`: usar el mismo mecanismo de reloj que ya usa `tests/test_business_timezone.py`.) Mantener el test existente de venta sin `occurredAt` que comprueba el instante resultante, ahora a traves del servicio.

- [ ] **Step 2:** `pytest tests/test_business_timezone.py -v` → FAIL.
- [ ] **Step 3:** Cambiar firmas en `datetime_utils.py`; actualizar cada llamada listada por `grep -rnE "business_(tz|today|midnight|instant|end_of_day)\(|to_business_tz\(|format_business_(date|datetime)\(" app` fuera de `app/agent/` pasando la sesion que ya tiene el llamador (`self.db`, el `db` del repositorio, la del servicio en `cash.py`). Quitar el default de `occurredAt` del validador de `SaleCreate` y aplicarlo en el orquestador al crear la venta: `data.occurredAt or business_instant(self.db, data.date)`.
- [ ] **Step 4:** `pytest -q` → PASS salvo tests del agente (que se arreglan en Task 6); anotar cuales fallan.
- [ ] **Step 5:** Commit `feat: Use each business's own timezone`.

---

### Task 4: Request → comercio

**Files:**
- Modify: `app/api/dependencies.py`, `app/services/user_service.py`, `tests/fixtures_http.py`
- Test: `tests/test_api_auth_required.py` (agregar casos)

**Interfaces:**
- Consumes: `bind_business` (Task 1), `User.business` (Task 2).
- Produces: `get_current_user` llama `bind_business(db, user.business_id, user.business.timezone)`. `UserService.get_or_create_from_firebase` se reemplaza por `UserService.get_for_firebase(decoded_token) -> User` que lanza 403 si no hay fila. El override `_override_get_current_user` de `tests/fixtures_http.py` tambien llama `bind_business`.

- [ ] **Step 1: Write the failing tests**

```python
def test_unknown_firebase_user_is_forbidden(db_session, monkeypatch):
    monkeypatch.setattr("app.api.dependencies.verify_firebase_token", lambda _t: {"uid": str(uuid.uuid4()), "email": "x@x.com"})
    with pytest.raises(HTTPException) as exc:
        get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials="t"), db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Tu cuenta no pertenece a ningun comercio"
    assert db_session.query(User).count() == 0

def test_known_user_binds_business(db_session, seeded_user, monkeypatch):
    # verify_firebase_token devuelve el uid de seeded_user
    user = get_current_user(...)
    assert db_session.info["business_id"] == seeded_user.business_id
```

  Nota: hoy `get_current_user` envuelve todo en `except Exception` → 401. El 403 debe dejarse pasar (`except HTTPException: raise` antes del generico).

- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** `pytest -q` → PASS. 
- [ ] **Step 5:** Commit `feat: Resolve the caller's business on every request`.

---

### Task 5: Signup de comercio y equipo

**Files:**
- Modify: `app/core/security.py`, `app/schemas/user.py`, `app/schemas/business.py` (crear), `app/services/user_service.py`, `app/api/v1/endpoints/auth.py`
- Test: `tests/test_auth_tenancy.py`

**Interfaces:**
- Consumes: `Business` (Task 2), `bind_business` (Task 1).
- Produces:
  - `security.py`: `create_firebase_user(uid, email, password: str | None = None, display_name=None)`, `delete_firebase_user(uid: str) -> None`, `generate_password_setup_link(email: str) -> str` (envuelve `firebase_auth.generate_password_reset_link`).
  - Schemas: `BusinessSignUp {business_name: str, email: EmailStr, password: str, display_name: Optional[str]}`; `SellerCreate {email: EmailStr, display_name: Optional[str], role: Literal["sales_rep", "viewer"]}`; `SellerCreatedResponse(UserResponse) + password_setup_link: str`; `PasswordLinkResponse {password_setup_link: str}`. Se borra `UserSignUp`. `UserUpdateRole.role` y `UserUpdate.role` pasan a `Literal["admin", "sales_rep", "viewer"]`.
  - `UserService`: `signup_business(data: BusinessSignUp) -> User`, `create_seller(data: SellerCreate, admin: User) -> tuple[User, str]`, `password_link(user_id, admin: User) -> str`; `get_by_id`/`get_all`/`update`/`update_role` reciben el admin y filtran por `admin.business_id` (ajeno → 404).
  - Endpoints: `POST /auth/signup` (201, `UserResponse`), `POST /auth/users` (201, `SellerCreatedResponse`, admin), `POST /auth/users/{user_id}/password-link` (200, `PasswordLinkResponse`, admin).
- Rollback: si falla la escritura en BD tras `create_firebase_user`, llamar `delete_firebase_user(uid)` y relanzar.

- [ ] **Step 1: Write the failing tests** (Firebase mockeado con `monkeypatch` sobre `app.services.user_service`)

```python
def test_signup_creates_business_and_admin(client, fake_firebase):
    r = client.post("/api/v1/auth/signup", json={"business_name": "Tienda A", "email": "a@a.com", "password": "secreto123"})
    assert r.status_code == 201 and r.json()["role"] == "admin"
    # existe un Business "Tienda A" y el user apunta a el

def test_signup_ignores_role_field(...)            # body con "role": "admin" extra → igual admin; con "viewer" → admin
def test_signup_rolls_back_firebase_on_db_error(...)  # forzar error en repo.create → delete_firebase_user llamado con ese uid
def test_admin_creates_seller_without_password(...)   # create_firebase_user llamado con password=None; respuesta trae password_setup_link
def test_admin_cannot_create_admin(...)             # role="admin" → 422
def test_seller_creation_rolls_back_firebase(...)
def test_password_link_for_other_business_is_404(...)
def test_admin_lists_only_own_business_users(...)
def test_admin_cannot_update_user_of_other_business(...)  # PUT y PATCH /role → 404
def test_update_role_accepts_sales_rep(...)
def test_sales_rep_cannot_create_users(...)          # 403
```

- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** `pytest tests/test_auth_tenancy.py -q` y `pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: Sign up a business and onboard sellers with a password link`.

---

### Task 6: Agente por comercio

**Files:**
- Modify: `app/agent/session.py`, `app/agent/tools/read.py`, `app/agent/tools/write.py`, `app/agent/prompt_runtime.py`, `app/agent/graph.py` (si hay que envolver el prompt), `app/api/v1/endpoints/agent.py`, tests del agente afectados
- Test: `tests/test_agent_tenancy.py`

**Interfaces:**
- Consumes: `bind_business` (Task 1), funciones de `datetime_utils` con `db` (Task 3).
- Produces:
  - `configurable` en `/agent/stream`: agrega `"business_id": str(current_user.business_id)` y `"business_tz": current_user.business.timezone`.
  - `agent_session(config) ` (antes sin argumentos): lee ambas claves de `config["configurable"]`, lanza `AgentAuthError` si falta alguna, y llama `bind_business`. Todas las herramientas pasan su `config`.
  - `prompt_con_fecha(state, config)` y `_hoy(tz: ZoneInfo) -> date`; si `create_react_agent` no pasa `config` a un callable, envolverlo en `RunnableLambda` en `build_graph`.
  - Helpers de fecha de las herramientas (`read.py` ~l.198, `write.py::_occurred_at`) reciben `db`; `hoy = business_today()` de `consultar_ventas` pasa dentro del `with agent_session(config) as db`.

- [ ] **Step 1: Write the failing tests**

```python
def test_agent_session_requires_business(config_sin_business):
    with pytest.raises(AgentAuthError):
        with agent_session({"configurable": {"user_id": str(uuid.uuid4())}}):
            pass

def test_thread_of_other_business_is_404(client, seeded_user, user_of_other_business):
    t = _create_thread_as(client, user_of_other_business, "ajeno")
    for method, path in [("get", f"/api/v1/agent/threads/{t['id']}"),
                         ("get", f"/api/v1/agent/threads/{t['id']}/state")]:
        assert getattr(client, method)(path, headers=_auth(seeded_user)).status_code == 404
    r = client.post("/api/v1/agent/stream", json={"thread_id": t["id"], "mensaje": "hola"}, headers=_auth(seeded_user))
    assert r.status_code == 404

def test_write_tool_creates_rows_in_callers_business(...)  # registrar_venta via herramienta → sale.business_id == business del config
def test_prompt_uses_business_timezone(...)                # mismo instante UTC, business_tz distinto → fecha distinta en el prompt
```

  `user_of_other_business` (fixture nueva en `tests/fixtures_domain.py`): `User` con `business_id=other_business.id`. Para que el 404 sea real por RLS, el `client` de este test usa `tenant_session` como `get_db` (no `db_session`, que es superusuario).

- [ ] **Step 2:** FAIL. **Step 3:** Implementar; verificar en `agent.py` que `/stream` y `/state` cargan la fila de `agent_threads` antes de `graph.aget_state`/`astream`. **Step 4:** `pytest -q` → PASS (incluye los tests del agente que Task 3 dejo pendientes).
- [ ] **Step 5:** Commit `feat: Scope the agent to the caller's business`.

---

### Task 7: Worker de calendario y scripts por comercio

**Files:**
- Modify: `app/services/calendar_sync.py`, `app/services/google_calendar.py`, `app/main.py`, `scripts/backfill_projections.py`, `scripts/fix_historical_cost_basis.py`, `scripts/copy_schema_data.py`
- Test: `tests/test_calendar_sync_worker.py`, `tests/test_backfill_projections.py`, `tests/test_copy_schema_data.py` (agregar casos)

**Interfaces:**
- Consumes: `bind_business` (Task 1), `Business` (Task 2).
- Produces:
  - `GoogleCalendarClient.from_settings(calendar_id: str)` (antes leia `settings.GOOGLE_CALENDAR_ID`). Credenciales: las mismas de hoy.
  - `sync_pass(session_factory, client_for: Callable[[str], GoogleCalendarClient]) -> int`: lista `Business` con `google_calendar_id` no nulo; por cada uno abre sesion, `bind_business`, llama `sync_pending_follow_ups(db, client_for(b.google_calendar_id))` sin cambios; una excepcion de un comercio suma 1 a fallas y sigue con el siguiente.
  - `start_calendar_sync(session_factory, client_for)`; `main.py` lo arranca siempre (el `if settings.GOOGLE_CALENDAR_ID` desaparece); el fallo de credenciales sigue yendo a `startup_issues`.
  - Scripts: flag `--business-id`; sin el, recorren `select id, timezone from businesses` y hacen `bind_business` por cada uno. `copy_schema_data` copia `businesses` antes que `users` (agregar a su orden de tablas).

- [ ] **Step 1: Write the failing tests**

```python
def test_sync_pass_visits_only_businesses_with_calendar(...)   # A con calendario, B sin → client_for llamado solo con el id de A
def test_sync_pass_failure_in_one_business_continues(...)      # client_for(A) lanza → B igual se sincroniza; retorno >= 1
def test_backfill_runs_per_business(...)                       # filas creadas quedan con el business_id correcto
def test_copy_schema_copies_businesses_first(...)
```

- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** `pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: Run calendar sync and maintenance scripts per business`.

---

### Task 8: Guard de arranque, aislamiento HTTP y runbook

**Files:**
- Modify: `app/main.py`, `alembic/env.py`, `entrypoint.sh`, `tests/fixtures_http.py`
- Create: `docs/runbooks/multi-tenant-rollout.md`
- Test: `tests/test_startup.py` (agregar caso), `tests/test_tenant_isolation_http.py`

**Interfaces:**
- Produces:
  - `assert_rls_enforced(engine) -> None` en `app/main.py`: `select rolsuper or rolbypassrls from pg_roles where rolname = current_user`; si es true lanza `RuntimeError("La API no puede conectarse como superusuario ni con BYPASSRLS: RLS quedaria desactivado")`. Se llama al inicio de `lifespan` y **no** se degrada a `startup_issues`: la API no arranca. El `client` de `tests/fixtures_http.py` lo parchea a no-op, igual que hoy parchea `build_graph`.
  - La API sigue leyendo su conexion de las variables actuales (`DATABASE_URL` / `POSTGRES_*`), que en produccion pasan al usuario de la API. `alembic/env.py` usa `MIGRATIONS_DATABASE_URL` si esta definida (con `postgres`), antes del fallback a settings; `entrypoint.sh` corre las migraciones con esa URL. Test: `tests/test_alembic_env.py::test_migrations_url_overrides_settings`.

- [ ] **Step 1: Write the failing tests**

```python
def test_startup_refuses_superuser(test_engine):     # TEST_DATABASE_URL es superusuario
    with pytest.raises(RuntimeError):
        assert_rls_enforced(test_engine)

@pytest.mark.parametrize("path", ["/api/v1/customers", "/api/v1/products", "/api/v1/sales", "/api/v1/purchases",
                                  "/api/v1/cash/movements", "/api/v1/follow-ups", "/api/v1/agent/threads"])
def test_list_endpoints_hide_other_business(tenant_client, path, data_in_both_businesses, user_a):
    body = tenant_client.get(path, headers=_auth(user_a)).text
    assert data_in_both_businesses.marker_b not in body
```

  `tenant_client`: igual que `client` pero con `get_db` → `tenant_session`. `data_in_both_businesses` siembra un registro por recurso en A y en B con un texto marcador (`marker_b`) en B. Ajustar las rutas a las reales de `app/api/v1/endpoints/`. Agregar un caso `GET /{id}` de B desde A → 404 para customers, products, sales y purchases.

- [ ] **Step 2:** FAIL. **Step 3:** Implementar guard y fixtures; escribir el runbook con: (1) `CREATE ROLE <api_user> LOGIN PASSWORD ... IN ROLE app_tenant`; (2) `CREATE SCHEMA IF NOT EXISTS agent AUTHORIZATION <api_user>` o `ALTER SCHEMA agent OWNER TO <api_user>` y reasignar sus tablas; (3) correr migraciones como `postgres`; (4) cambiar las variables de la API en Railway al nuevo usuario; (5) verificar `/health` y que el comercio principal ve sus datos; (6) rollback: volver las variables a `postgres` (RLS no aplica a superusuario, la app sigue funcionando con un solo comercio).
- [ ] **Step 4:** `pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: Refuse to start without RLS and document the rollout`.
