# F-ID · Identidad transversal en tres capas (Producto / Cliente / Proveedor)

## Contexto

El usuario pidió, antes de diseñar F-S (SKU de producto) en aislamiento, un mecanismo que
garantice identificador persistente para las tres entidades infaltables — producto, cliente,
proveedor — sin importar si el archivo del tenant ya trae un código propio. Una primera versión de
este plan proponía una sola columna `external_code` por entidad. El usuario la revisó y corrigió el
diseño con un argumento central que se adopta entero:

> El UUID une las tablas internas. El código Véktor permite exportar y volver a identificar
> entidades. Los identificadores del archivo permiten reconocer datos externos. Son tres problemas
> relacionados, pero distintos.

Una sola columna `external_code` no alcanza porque una entidad real puede tener MÁS de un código
externo simultáneo (el código de su ERP, el de su sistema de e-commerce, el que le puso un
proveedor en su propio catálogo) y dos fuentes pueden reusar el mismo valor crudo (`"0001"`) para
entidades distintas sin que eso sea una colisión real. Hace falta una tabla transversal con
procedencia, no una columna.

## Las tres capas

**1. Identidad interna (ya existe, no se toca).** `Product.id`/`Customer.id`/`Supplier.id` (UUID),
FK de todas las tablas de transacciones (`sales_entries.product_id/customer_id`,
`expense_entries.supplier_id`, `inventory_movements.supplier_id`), `ON DELETE SET NULL`. Los
agentes y servicios internos siempre transportan este UUID — nunca un código ni un nombre.

**2. Código Véktor (nuevo, permanente, uno por entidad).** Formato `PREFIJO-NNNN`
(`TEX-0001`/`CLI-0001`/`PRV-0001`), asignado una sola vez, nunca reciclado aunque la entidad se
desactive o se fusione. Para mostrar, buscar, exportar y para que un agente pueda referirse a una
entidad sin ambigüedad ("el cliente Juan Pérez (CLI-0042)"). Producto ya tiene el campo que cumple
este rol — `products.sku`, decisión ya aceptada por el usuario el 2026-08-14 — no se migra de
nuevo. Cliente/proveedor no tienen nada equivalente hoy: ganan una columna denormalizada nueva
`vektor_code` (barata de indexar/mostrar, escrita por el mismo servicio que la vuelca a la capa 3).

**3. Identificadores externos, multi-valuados, con procedencia (nuevo).** Tabla transversal
`entity_identifiers` — una entidad puede acumular varios códigos de fuentes distintas a lo largo
del tiempo sin perder ninguno, y sin que dos fuentes que reusan el mismo valor crudo colisionen
entre sí. Ésta es la capa que de verdad mejora la vinculación entre archivos futuros: cuando
Véktor ya vio "Almacén Doña Rosa" con CUIT `X`, o un archivo anterior trajo `CLI-918` para el mismo
cliente, ese conocimiento queda escrito y disponible para el próximo import, no sólo el código que
Véktor generó.

## Esquema nuevo

### `entity_code_sequences` — contador atómico, evita `MAX()+1`

```sql
tenant_id    UUID
entity_type  VARCHAR   -- 'product' | 'customer' | 'supplier'
prefix       VARCHAR   -- 'TEX', 'CLI', 'PRV', 'GEN', ...
next_value   INTEGER NOT NULL DEFAULT 1
UNIQUE (tenant_id, entity_type, prefix)
```

Asignación por `UPDATE ... SET next_value = next_value + 1 ... RETURNING next_value` — una sola
sentencia atómica, sin `SELECT MAX` ni reintento ante colisión de índice. Dejar huecos ante
rollback es aceptable (no es una numeración contable); lo que no es aceptable es reciclar un valor
ya entregado, y una `UPDATE ... RETURNING` nunca entrega el mismo valor dos veces sin importar
cuántas transacciones concurrentes lo pidan.

### `entity_identifiers` — el registro transversal

```sql
id                  UUID PK
tenant_id           UUID NOT NULL
entity_type         VARCHAR   -- 'product' | 'customer' | 'supplier'
entity_id           UUID NOT NULL
identifier_type     VARCHAR   -- 'vektor_code' | 'sku' | 'barcode' | 'dni' | 'cuit'
                              -- | 'email' | 'phone' | 'business_code' | 'alias'
namespace           VARCHAR   -- 'vektor' | 'business' | 'supplier:<supplier_id>'
raw_value           VARCHAR NOT NULL
normalized_value    VARCHAR NOT NULL
origin              VARCHAR   -- 'business' | 'vektor' | 'import' | 'user_confirmed'
is_primary          BOOLEAN DEFAULT FALSE
first_seen_at       TIMESTAMPTZ NOT NULL
last_seen_at        TIMESTAMPTZ NOT NULL
source_upload_id    UUID NULL  -- FK uploaded_files, igual que sales_entries.source_upload_id
created_by_user_id  UUID NULL
revoked_at          TIMESTAMPTZ NULL

UNIQUE (tenant_id, entity_type, identifier_type, namespace, normalized_value)
    WHERE revoked_at IS NULL
```

**Namespaces, acotados a lo que hace falta ahora** (no literal "upload:&lt;id&gt;" como en el
borrador original del usuario — un namespace por archivo individual haría que dos subidas
sucesivas del mismo sistema externo del tenant NUNCA compartan namespace, y el propósito entero es
que la próxima importación SÍ reconozca lo que trajo la anterior):
- `"vektor"` — códigos que generamos nosotros (incluye `vektor_code` y, si algún día conviene,
  cualquier otro identificador nuestro).
- `"business"` — cualquier código/documento que el tenant ya tenía (SKU real, DNI, CUIT, un ID de
  cliente de su sistema anterior) sin importar en qué archivo llegó — la granularidad correcta
  para que repetir un import lo reconozca.
- `"supplier:<supplier_id>"` — soportado por el esquema para cuando un producto tiene un código
  distinto por cada proveedor que lo vende (un mismo número puede significar productos distintos
  según el proveedor) — **no se pobla en esta fase**, es una extensión que el esquema no bloquea a
  futuro, no un compromiso de esta entrega.

**No reciclar códigos, mecanismo estructural, no una promesa de aplicación:** el valor de
`vektor_code` sólo puede salir de `entity_code_sequences` (nunca dos veces, ver arriba) y su fila
en `entity_identifiers` **nunca se borra ni se revoca**, aunque la entidad se desactive o se
fusione en un dedup — queda como historia permanente, igual que `decision_audit_log` es
insert-only. Deactivar un cliente no libera su `CLI-0042`; fusionarlo tampoco — el código del
perdedor pasa a ser una fila más (revocada como PRIMARIA pero **no** borrada) apuntando al
sobreviviente.

**Migración necesaria:** `entity_code_sequences` + `entity_identifiers` (tablas nuevas) +
`customers.vektor_code`/`suppliers.vektor_code` (columnas denormalizadas nuevas, nullable al
principio, con índice único parcial mientras se hace el backfill). **Producto no gana ninguna
columna** — `sku`/`sku_normalized`/su índice único ya existen y siguen siendo la fuente de verdad
para el código Véktor de producto; lo que gana es una fila espejo en `entity_identifiers`
(namespace `"vektor"` cuando lo generamos, `"business"` cuando lo trajo el negocio) para que
producto participe del mismo resolvedor que cliente/proveedor sin una segunda tabla de reglas.

## Resolvedor común, con conflicto explícito

```python
@dataclass(frozen=True)
class EntityResolution:
    status: Literal["resolved", "not_found", "ambiguous", "conflict"]
    entity_id: uuid.UUID | None
    matched_by: list[str]         # qué identifier_type(s) matchearon
    candidates: list[uuid.UUID]   # con qué otras entidades era ambiguo
    conflicts: list[IdentityConflict]  # identificadores fuertes que apuntan a entidades distintas

async def resolve_entity_reference(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entity_type: EntityKind,
    references: dict[str, str],   # {"vektor_code": ..., "sku": ..., "dni": ..., "name": ...}
) -> EntityResolution: ...
```

**Regla explícita, la que corrige el problema real de la propuesta anterior ("primero que
matchea gana"):** si dos identificadores fuertes de la misma fila apuntan a entidades DISTINTAS →
`conflict`, nunca gana el primero en silencio. Orden de precedencia por entidad:

- **Producto:** `vektor_code` → barcode → sku (namespace `business`) → alias confirmado →
  nombre+marca → nombre normalizado (conservador, último recurso). Mismo orden que ya implementa
  `_resolve_product_identity`, sólo se le antepone `vektor_code`.
- **Cliente:** `vektor_code` → `business_code` → CUIT/DNI → email → teléfono → alias confirmado →
  nombre (nunca certeza automática, sólo candidato — regla ya congelada por F-E,
  `_classify_row_reference`).
- **Proveedor:** mismo orden que cliente, con CUIT en vez de CUIT/DNI.

## Sitios de alta reales (inventario cerrado, verificado contra código — no cambia con la revisión)

**Producto** — chokepoint único `add_product_or_reuse` (`product_identity.py:251`, cubre 5
callers) + 2 llamadas explícitas (`api/v1/products.py:379`, `api/v1/others.py:556`). Ningún agente
LLM crea `Product` directo.

**Cliente** — chokepoint `CustomerRepository.save()` (`customer_repository.py:212`, cubre
`POST /customers` + `customer_import_service.py:292`) + 1 llamada explícita
(`api/v1/others.py:458`) + sentinela "Local" (`customer_sentinel.py:80`) que nunca recibe código.

**Proveedor** — chokepoint `SupplierRepository.save()` (`supplier_repository.py:135`, cubre
`POST /suppliers` + `supplier_import_service.py:256`) + 2 llamadas explícitas
(`api/v1/others.py:468`, `ingestion_import_service.py:292` modo `legacy`) + sentinela "No
identificado" (`ingestion_import_service.py:380`) que nunca recibe código.

**Distinción alta vs. edición:** `save()` en ambos repos se usa para crear Y actualizar — el hook
de asignación debe activarse sólo cuando la fila es genuinamente nueva (`inspect(obj).transient`
antes del `add`, no "¿el campo está vacío?" sobre un objeto ya persistido), para que asignar un
código nunca sea un efecto colateral impredecible de una edición cualquiera.

## Backfill: nunca saltear, nunca fusionar solo

Corrección sobre el borrador anterior (que salteaba proveedores ambiguos sin numerarlos): **toda
entidad real recibe su `vektor_code`**, ambigua o no — dos proveedores llamados igual reciben
`PRV-0012`/`PRV-0013` cada uno, y ESO es lo que permite después revisarlos por código en vez de por
nombre repetido. La detección de posibles duplicados es un paso SEPARADO que marca para revisión
humana, nunca fusiona automáticamente:

- **Producto** — el motor de dedup ya existe (`product_dedup_service.py`) y sigue corriendo ANTES
  del backfill de código, como ya decidía F-S — fusiona por barcode/sku, deja nombre+marca en
  revisión. Lo que queda en revisión igual recibe su código, como producto propio.
- **Cliente** — sin gate de duplicados nuevo: `dni`/`cuit` ya son únicos por índice desde
  `20260721_0001`, y F-E ya decidió que el nombre nunca es identidad — no hay ambigüedad real que
  detectar hoy.
- **Proveedor** — gate NUEVO pero que ya no bloquea la numeración: agrupa activos no-sentinela por
  `normalize_text(name)`; todo proveedor recibe código; un grupo con más de uno se reporta como
  `POSIBLE_DUPLICADO` (visible, auditado) para que un humano decida fusionar — nunca automático.

**Fusión (dedup) transfiere identificadores, no los pisa:** cuando dos entidades se fusionan, TODAS
las filas de `entity_identifiers` del perdedor (incluida su propia `vektor_code`, ya marcada
`is_primary=false`) se re-apuntan al `entity_id` del sobreviviente. Si el sobreviviente ya tenía un
identificador del mismo `(identifier_type, namespace)`, el del perdedor queda igual, sólo dejando
de ser primario — nunca se descarta.

## Garantía de presencia — nullable hoy, `NOT NULL` cuando el backfill lo permita

Todo alta nueva pasa por el servicio compartido; el backfill cubre lo histórico. Recién cuando un
backfill confirme cobertura 100% sobre entidades no-sentinela se agrega el `CHECK` correspondiente
(`is_sentinel OR vektor_code IS NOT NULL`, o el equivalente para `products.sku` si se decide en su
momento) — antes de eso sería una migración que rompe con datos reales todavía sin numerar. Se deja
como tarea explícita al final, no implícita.

## Tareas (TDD, un commit por mecanismo, backend-completo-primero — igual que F-S.0/F8/F-O.3)

- **ID.0 — Contrato de identidad (doc).** Volcar esta sección (tres capas, namespaces, no-reciclo,
  conflicto vs. ambigüedad) en `docs/plans/ingestion-mapping-overhaul.md`, reemplazando las
  secciones `F-S`/`F-I` actuales por punteros a `F-ID`. Sin código.

- **ID.1 — `entity_code_sequences` + `domain/entity_code.py`.** Migración de la tabla nueva +
  `assign_next_sequence()` (la `UPDATE ... RETURNING` atómica) + prefijos (`CUSTOMER_PREFIX="CLI"`,
  `SUPPLIER_PREFIX="PRV"`, `PRODUCT_CATEGORY_PREFIXES` curado por vertical — mismo test que exige
  cobertura de las 6 verticales reales de `PRODUCT_CATEGORY_LABELS` y falla el CI si falta una) +
  `format_code()`. Test de concurrencia real contra Postgres: N corridas paralelas al mismo
  `(tenant, entity_type, prefix)` nunca repiten valor.

- **ID.2 — `entity_identifiers` (migración) + `entity_identity_service.py`.** `record_identifier()`
  (upsert por `(tenant, entity_type, identifier_type, namespace, normalized_value)`, actualiza
  `last_seen_at` si ya existía) + `assign_vektor_code_if_missing()` (llama a ID.1, escribe la fila
  permanente `identifier_type="vektor_code", namespace="vektor"`, y para cliente/proveedor
  sincroniza la columna denormalizada `vektor_code` en el mismo commit — un solo camino de
  escritura). Test de no-reciclo: desactivar la entidad, confirmar que la fila sigue viva y
  `is_primary` sigue en `true` salvo fusión explícita.

- **ID.3 — `resolve_entity_reference()`.** El resolvedor rico de la sección de arriba, puro sobre
  índices ya cargados (mismo estilo que `_resolve_product_identity`) — sin ingesta todavía, tests
  unitarios contra fixtures armadas a mano cubriendo los 4 status y el caso de conflicto real (dos
  identificadores fuertes de la misma fila, entidades distintas).

- **ID.4 — Bootstrap: volcar lo que ya existe a `entity_identifiers`.** Backfill que lee
  `sku`/`barcode` (producto), `dni`/`cuit` (cliente), `cuit`/`cuil` (proveedor) y los alias ya
  guardados en `custom_fields["_aliases"]` (producto) y los escribe como filas
  `namespace="business"`/`origin="business"` (o `"vektor"` si `_sku_origin == "vektor"`) — para que
  el resolvedor tenga datos reales desde el primer día, no sólo lo nuevo. Idempotente.

- **ID.5 — Que nazcan con código: los 8 sitios reales.** Wireo de `assign_vektor_code_if_missing`
  en los 3 chokepoints + 5 llamadas explícitas del inventario de arriba, con la distinción
  alta-vs-edición. Un test por sitio (creación end-to-end por ese camino exacto) + control negativo
  (sentinela nunca recibe código) + control de no-pisado (código propio del negocio sobrevive).

- **ID.6 — Backfill de código para lo histórico.** `scripts/backfill_entity_code.py` (o uno por
  entidad, a definir al escribir — probablemente uno compartido parametrizado por `EntityKind` dado
  que ID.1-ID.2 ya son transversales), dry-run/`--apply`, `--tenant`/`--all-active`, auditado,
  aplicando el orden correcto (dedup de producto antes; gate de posible-duplicado de proveedor que
  numera igual y sólo marca para revisión — nunca saltea, nunca fusiona).

- **ID.7 — Ingesta: capturar el código externo de un archivo.** Targets nuevos en
  `GET /ingestion/field-catalog` (`customer:business_code`, `supplier:business_code`) que escriben
  en `entity_identifiers` (namespace `"business"`, `origin="import"`) en vez de a una columna —
  wirear `resolve_entity_reference` en `_classify_row_reference` (cliente/proveedor) y en la
  resolución de venta/gasto (producto), mismo patrón que F-S.0 ya hizo para sku/barcode. Regla "dos
  códigos iguales en el mismo archivo → 422, nunca last-wins" vive acá, es de importación.

- **ID.8 — Dedup transfiere identificadores.** Extender `product_dedup_service.py` (y, si se
  construye un merge de proveedor a futuro) para que la fusión re-apunte las filas de
  `entity_identifiers` del perdedor al sobreviviente en vez de perderlas.

- **ID.9 — Frontend, sólo lectura primero.** Código Véktor visible en `/products`/`/customers`/
  `/suppliers` + ficha + CSV. Búsqueda por código exacto. Gestión editable de identificadores
  externos (agregar/revocar un `business_code` a mano) queda para cuando ID.7 le dé un propósito
  visible al usuario — mismo criterio de secuenciación que ya usa el resto del programa (backend
  completo primero, UI fast-follow).

- **ID.10 — `get_entity_ref()` para agentes.** Helper de sólo lectura, `{id, code, display_name}`,
  para que cualquier agente que ya tiene un UUID pueda formatear "Juan Pérez (CLI-0042)" en una
  respuesta. **No se rediseña el tooling interno de los 9 agentes en esta fase** — hoy ninguno crea
  entidades directo y todos ya transportan UUID; si aparece un caso concreto (ej. un agente que
  necesita resolver "el cliente con código X" desde un mensaje de chat) se aborda como su propia
  tarea chica cuando exista, no especulativamente.

- **ID.11 (al final, cuando ID.6 confirme cobertura 100%) — `CHECK` de no-nulo** sobre
  `vektor_code`/`sku` para entidades no-sentinela.

## Decisiones tomadas (no volver a preguntar)

- Namespace granular por SISTEMA DE ORIGEN (`business`, `vektor`, `supplier:<id>` a futuro), no por
  archivo individual — un namespace por upload rompería el propósito de reconocer la próxima
  importación.
- `Product.sku` no se migra a una columna nueva `vektor_code` — sigue siendo el campo único,
  decisión ya cerrada por el usuario el 2026-08-14; `entity_identifiers` lo espeja para que
  producto entre al mismo resolvedor sin una segunda tabla de reglas.
- Backfill nunca saltea por ambigüedad — numera y marca para revisión aparte, incluyendo proveedor.
- No-reciclo de código es estructural (secuencia atómica que nunca repite + fila permanente
  insert-only), no una promesa de aplicación con índice parcial sobre activos.
- Identificadores editables en frontend quedan para después de ID.7 (cuando resuelven algo real),
  no antes.
- Tooling de agentes se limita a un helper de display de sólo lectura en esta fase; no se
  rediseñan las 9 integraciones de agentes sin un caso de uso concreto.

## Verificación end-to-end

Suite completa backend (`make test-cov`, gate 60%) + `ruff check` + `mypy` + suite marcada
`postgres` (la concurrencia de `entity_code_sequences` necesita Postgres real) + `alembic upgrade
head`/`downgrade -1`/`upgrade head` limpio + smoke: crear una entidad por cada camino de alta real
y confirmar código asignado; correr el backfill dos veces y confirmar que la segunda no cambia
nada; desactivar una entidad con código y confirmar que su fila en `entity_identifiers` sigue viva
y el valor nunca se re-entrega a otra entidad; fusionar dos productos duplicados y confirmar que
los identificadores del perdedor sobreviven apuntando al ganador.
