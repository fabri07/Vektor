# Cierre del PR #48 y de `spike/pos-b1`

Plan del 2026-10-05, revisado el 2026-10-06 contra el código (ver «Qué cambió en la revisión» al final). Cierra las dos ramas que quedaron incompletas después de la limpieza de ramas —`feat/ingestion-identity-reread-safety` (el PR #48) y `spike/pos-b1`— y agrega los aportes de capital de los socios (parte D).

> **Desde el 2026-10-07 el orden de lo pendiente (B·3 en adelante, D·2–D·7 y el margen de producto) lo fija [`cierre-ingesta-capital-margen.md`](cierre-ingesta-capital-margen.md).** Este archivo queda como inventario del #48 y como matriz de portado.


## Orden global

Va primero lo que hoy corrompe datos o los pierde:

1. **D0.** El Libro Diario importa aportes, préstamos y saldos iniciales como VENTAS. Se arregla y se corre un diagnóstico read-only en Neon.
2. **B1.** Compuerta de relectura: hoy no se distingue reemplazar de perder.
3. **B2–B3.** Arreglos chicos, incluido el bug de `user_selected`, y datos que se pierden.
4. **D, pasos 2–5.** Aportes y retiros: modelo, migración de los retiros y onboarding.
5. **B4–B9.** Calidad del import, «Otros», F-D, vínculo por código, cola con alias y acciones masivas.
6. **D, pasos 6–7.** Aportes en especie e ingesta de aportes.

En paralelo, cuando el dueño vaya al local: **A** (B1 del POS). **C** (`.venv312`) espera saber por qué se revirtió.

---

## A. `spike/pos-b1`: medir B1 y borrar la rama

La rama es el andamio descartable de B1. No tiene código para terminar: su salida son cinco respuestas medidas en la PC del local, con el procedimiento de `docs/plans/b1-spike-arranque-sin-red.md`. El preview sigue vivo: el 2026-10-06 contestó 302 al SSO de Vercel, que es lo esperado.

### A1. Antes de ir al local

- **Confirmar que la térmica es de 80 mm.** El andamio mide sólo 80 mm. Sus tres variantes de `@page` (A `80mm 297mm`, B `72mm 200mm`, C sin `size`) son exactamente las tres de `pageCss()` en `posPrintConfigStore`, así que el resultado se traduce directo a configuración. Si la térmica es de 58 mm, la variante B no aplica tal cual.
- **Llevar la planilla de resultados** (abajo) para anotar cada número en el lugar donde lo voy a usar.

### A2. Mediciones (las hace el dueño, en el local)

| # | Qué | Cómo se anota |
|---|---|---|
| 1 | Arranque sin red después de **reiniciar**. No apagar: en Windows, "Apagar" con inicio rápido es una hibernación | sí/no + segundos del doble clic al escaneo |
| 2 | `navigator.storage.persisted()` después del reinicio | true/false + `estimate()` |
| 3 | Variante de `@page` que funciona, en las 4 combinaciones del driver (rollo / 80×297) | A/B/C + tamaño de papel del driver |
| 4 | ¿Corta? ¿Abre el cajón con `window.print()` del navegador? ¿La térmica puede ser la predeterminada? | sí/no × 3 |
| 5 | Sufijo del lector (Enter / Tab / nada), si lee QR y cuántos ms hay entre teclas | sufijo + QR sí/no + ms (mínimo / típico / máximo) |

### A3. Qué hago con cada resultado

- **3 → impresión.** El default de `posPrintConfigStore` (`pageSize`, `paperWidth`) pasa a ser la variante ganadora. Sigue siendo configuración por PC.
- **5 → lector.** El umbral de ráfaga de `useScanner.ts` pasa a ser el máximo medido entre teclas más un margen; hoy es un supuesto de ~30 ms. Si el lector no termina en Enter ni en Tab, `useScanner` necesita otro cierre de lectura y eso es un cambio de código, no de configuración.
- **4 →** si no corta, el ticket necesita margen inferior. Si no abre el cajón con `window.print()`, se documenta como límite.
- **1 → B7b.** La decisión vigente es que se enciende **por PC**, no globalmente, y eso no cambia. Lo que cambia es la recomendación en la pantalla.
  - **Contingencia: si el arranque sin red FALLA** (dinosaurio, pantalla en blanco o IndexedDB vacío), B7b no se recomienda. Se replantea el enfoque de "abrir sin internet" con un plan aparte; no se fuerza.
- **2 →** si `persisted()` da `false`, la exportación de ventas pendientes pasa a ser camino de primera.
- **Cierre.** Los resultados se registran en `docs/claude/pos-offline.md` y se borra `spike/pos-b1`. **No se pushea nada a la rama antes de medir:** el service worker es cache-first.

---

## B. El PR #48: se porta por partes, no se rebasea

### Por qué no se rebasea

Desde el merge-base (`2c18df1f`, #47), `main` reescribió todo lo que la rama toca:

| Archivo | Líneas agregadas en `main` |
|---|---|
| `ingestion_import_service.py` | +4127 |
| `reread_service.py` | +1584 |
| `api/v1/ingestion.py` | +1502 |
| `ColumnMapperPanel.tsx` | +421 |

La relectura, además, cambió de modelo: pasó de un run `RUNNING` a sesiones de revisión `QUEUED→APPLYING`. Y las tres migraciones de la rama cuelgan de `20260813_0001`, que en `main` ya tiene un hijo, así que dejarían dos heads. El head actual es `20261002_0001`.

### Decisiones tomadas (2026-10-05)

- **F-ID (identidad transversal): NO se porta.** Alcanza con `external_code` + `external_source`.
  - Sí se porta el vínculo de una venta o compra con su cliente o proveedor **por código** (paso 7).
  - El SKU automático se descarta.
- **Cola de ventas sin producto, con alias aprendidos: SÍ, con pantalla** (pasos 8a y 8b).
- **Acciones masivas del mapeo: sólo las inocuas**, «Guardar como campo propio» e «Ignorar vacías» (paso 9).

### Se descarta, porque `main` ya lo resolvió por otro camino

| De la rama | Dónde quedó resuelto en `main` |
|---|---|
| Docs y diagnósticos | Ya no aplican |
| SKU/barcode como columnas de venta (`2191dbe5`) | `CANONICAL_FIELDS["sale"]` |
| F-I(B) | E6a-B (`external_code`) |
| F-H6.f | E6a-A |
| Script de limpieza de F-O.4 | Ya no hace falta |

### Pasos (cada uno en su PR, en este orden)

1. **Compuerta de relectura, F-R (se reescribe).** Va PRIMERO porque es el único paso que evita una pérdida de datos.
   - **Qué hay hoy en `main`** (verificado): la relectura ya tiene un preview que el usuario confirma (`FileListSection.tsx:937-957`). El defecto es otro: «A anular» es `to_void = len(recon.non_edited)` (`reread_service.py:2242`), es decir, **todos** los registros previos, incluidos los que la relectura vuelve a crear. El usuario no puede distinguir una actualización de una pérdida, y por eso mira un número rojo grande que casi nunca significa lo que dice.
   - **Arreglo:**
     - (a) Separar el conteo en «Se reemplazan» y «**Se pierden (sin reemplazo)**», exactos, con la correspondencia por `source_row_ref` que el preview ya calcula.
     - (b) Si «Se pierden» > 0, el botón de aplicar exige una tilde explícita, como `PosibleRepeticionAviso`.
     - (c) Del lado del servidor, una red de seguridad: 409 `REREAD_WOULD_LOSE_DATA` al encolar el apply sin `accept_data_loss`. Cubre clientes viejos y llamadas directas a la API.
   - **Despliegue:** Railway y Vercel despliegan en paralelo y sin orden. Si el backend sale antes que el frontend nuevo, un frontend viejo puede recibir el 409, y tiene que mostrar el mensaje del servidor (verificar el manejador de error actual). Si `accept_data_loss` entra a una huella de idempotencia, entra sólo cuando es `True`, como `acepto_posible_repeticion`.
   - **Tests:** e2e por HTTP + persistencia, y `pytest -m postgres -n 0`. La rama falló justamente en un test de concurrencia contra Postgres por el orden de la compuerta.
   - **Hallazgos al implementarlo (2026-10-06):** (1) la correspondencia por `source_row_ref` NO sirve para (a): la referencia es la posición de la fila en su contexto, y D0 corre los índices del Libro Diario. (2) La revisión adversarial midió que ESTIMAR en el preview tampoco alcanza (falsos negativos y positivos masivos): **se rediseñó con el OK del dueño** — el apply mide lo que pierde dentro de su transacción y, sin aceptar, revierte y vuelve a pedir confirmación; el preview queda como estimación y el 409 previo al encolado se descartó. Ver `docs/claude/ingesta.md`. (3) Defecto previo: una edición a mano cuya fila se corrió queda duplicada. La compuerta lo detecta y lo frena; **el arreglo de la preservación es un paso aparte, pendiente.**
2. **Tres arreglos chicos (cherry-pick + corrección):** **Hecho (2026-10-06, rama `fix/ingesta-paso2-arreglos-chicos`).** F-V.2 (conteo en el filtro de categorías), que venía en `e4558d21`, quedó afuera.
   - `4063b33c`: el barcode es vinculable dentro del mismo archivo. Verificar que el índice use la MISMA normalización de barcode que `_resolve_product_identity`.
   - `687dd622`: cambiar el propósito de un archivo ya no borra el mapeo hecho a mano. **No va tal cual** (verificado): conserva en el touched-set una columna tocada cuyo target ya no existe en la entidad nueva. Esa columna recibe la sugerencia nueva, pero viaja como `user_selected: true` (`ColumnMapperPanel.tsx:1991`) y saltea la compuerta de riesgo de F8 como si el usuario la hubiera confirmado. Hay que sacarla del touched-set. **El mismo defecto ya existe hoy en el camino multi-hoja** (`ColumnMapperPanel.tsx:658-668`), así que se arreglan los dos, con un test por camino.
   - `e4558d21`: la tabla scrollea.
3. **Datos que hoy se pierden (se reescribe):**
   - `build_incomplete_product` usa `infer_category` con confianza alta, en vez de `None`.
   - `product_dedup_service` conserva el `external_code` del perdedor **sólo si el ganador no tiene uno**. El índice único parcial `(tenant_id, external_code_key)` no admite dos. Si los dos tienen código, gana el del ganador y el del perdedor queda en la auditoría del merge, nunca se descarta en silencio.
4. **Calidad del import:**
   - F-N (`a2fa53cc`): cherry-pick revisado. Sólo completa lo vacío y respeta `has_user_edits`.
   - Filas «Total» / «Subtotal» fuera de la captura de «Otros», **de forma conservadora**: la celda de etiqueta tiene que ser exactamente total/subtotal/totales (normalizada), sin otro texto descriptivo en la fila. Un producto llamado «Total Clean» NO es un total, y eso va como test.
5. **«Otros» agrupado por archivo × motivo, F-O.3 (se reescribe):** `/others/summary` + descarte por grupo.
6. **Campos de otra sección, F-D (se reescribe):** implementa los targets cruzados que hoy descarta `CROSS_ENTITY_TARGETS_IMPLEMENTED`. Una sola vez por entidad, sólo si el campo está vacío, con ledger y reversión por F11.
   - **Antes de escribir código hay que decidir** si la reversión usa las acciones `UPDATE_*` existentes, que es preferible porque no necesita migración, o acciones nuevas (CHECK re-encadenado al head del momento).
7. **Vínculo por código:** destinos `customer_external_code` / `supplier_external_code` en venta y compra.
   - Se vincula sólo si el código existe y es único: el índice lo garantiza.
   - Un código que no existe NO crea un cliente con ese código. La fila sigue la resolución actual (documento, email, teléfono, nombre) y se avisa.
   - "Ante la duda, no hay clave de identidad."
8. **Cola de ventas sin producto, con alias y pantalla, en DOS PRs:**
   - **8a. Backend:** cola + vincular + aprender alias.
   - **8b. Pantalla.**
   - **Reglas que se fijan antes de 8a:**
     - **Vincular no mueve stock.** El efecto de inventario es una decisión por archivo (`inventory_effect`) y nunca un default. Vincular cambia `product_id`, y el costo y el margen se recalculan por FactsService.
     - **El alias es una clave débil, de nivel nombre.** Va después de barcode y SKU en `_resolve_product_identity`, y **nunca** lo usa el scan del POS (B4).
     - **Un alias que apunta a dos productos no vincula nada.** Es un conflicto y se informa.
     - **Va en una tabla propia `product_aliases`**, con único `(tenant_id, alias_normalizado)` y la procedencia. No va en `custom_fields`.
     - **Fusionar productos mueve los alias.** Borrar un archivo (F11) NO revierte un alias aprendido por acción del usuario.
9. **Acciones masivas inocuas del mapeo:** de `4a2844bf`, sin «Aceptar ambiguas».

Al terminar: cerrar el #48 diciendo qué se portó y en qué PR, y borrar la rama.

### Reglas para cada paso

- Antes de cada PR, verificar contra el código actual de `main`, no contra este inventario.
- Cada paso lleva su test, verificado por mutación, y pasa por **revisión de código antes del merge**. En el programa de ASTERIA esa revisión encontró 5 bugs que la suite no veía.
- La documentación va en `docs/claude/ingesta.md` en el mismo commit.
- Toda migración es aditiva y cuelga del head del momento.
- Todo contrato nuevo de la API tolera un frontend viejo, porque el deploy es en paralelo.
- `pytest -m postgres -n 0` cuando el paso toca locks, migraciones o la relectura.

---

## C. Aparte: `backend/.venv312/` versionado

`main` versiona 10.646 archivos (148 MB) de `backend/.venv312/` desde `2619b96b` (2026-03-10). **Ya se intentó sacarlo y se revirtió:** `af8fd208` lo des-trackeó y `58f2ea1a` (2026-06-08) lo revirtió sin explicar por qué. Nada en `main` lo usa: sólo aparece en los `exclude` de ruff de `pyproject.toml`.

**Antes de repetirlo hay que saber por qué se revirtió.**

Si no había motivo:

- `git rm -r --cached backend/.venv312` en un commit propio.
- Agregar `.venv*/` a `backend/.dockerignore`. Hoy sólo excluye `.venv/`, así que esos 148 MB entran al contexto de cada build de Docker en CI y Railway.
- Agregar un test que falle si se versiona cualquier `.venv*`.

No reescribe la historia.

---

## D. Aportes de capital de los socios (nuevo, 2026-10-06)

Hasta hoy Véktor no tiene dónde poner la plata que un socio mete en el negocio. Esto falla en dos casos:

- **Archivos.** Un aporte que viene en un archivo no tiene destino.
- **Negocios que arrancan de cero.** El primer dato real que tienen es justamente el capital con que arrancan, y no hay dónde cargarlo.

### Qué hay hoy en `main` (verificado)

- **Libro Diario: toda entrada de dinero es una venta (BUG).** `parse_libro_diario` (`file_parsing.py:1103-1105`) manda a `ventas` cualquier fila con `dinero_ingreso`, sin mirar el detalle. Las filas «Aporte socio Juan», «Préstamo del banco» o «Saldo inicial de caja» **se importan como facturación** e inflan ventas, margen y score. Es el mismo error que la Fase 2 cerró en el libro mixto, todavía vivo en el Libro Diario.
- **Libro mixto (Fase 2):** un aporte va a «Otros» con el motivo «Aporte de socio: es plata que pone el dueño, no una venta» (`domain/tipo_de_movimiento.py`). Es correcto, pero no tiene a dónde ir después.
- **Retiro de ganancias:** existe (`POST /expenses/profit-withdrawal`), pero como **gasto** PAYROLL/OPEX con `custom_fields.profit_withdrawal`. El aporte no tiene contraparte.
- **Caja:** `cash_on_hand_est` sale del arqueo, si no del estimado del onboarding (`cash_on_hand_ars`), si no queda en 0 (`business_state_service.py:575-585`). `caja_liquida` (FactsService) suma sólo ventas líquidas. No hay saldo inicial más flujo, así que un aporte no puede explicar una caja que sube sin ventas.

### Límite de alcance

Véktor no es contabilidad (no hay patrimonio neto, balance general ni libro de socios). Esto es **analítico**: saber cuánta plata puso cada socio, cuándo, y si el negocio se sostiene con su operación o con aportes. No se emiten estados contables.

### Pasos

- **D0. Arreglo urgente del Libro Diario (independiente, va primero, chico).**
  - En `parse_libro_diario`, una fila de sólo `dinero_ingreso` cuyo detalle `tipo_de_movimiento.clasificar` reconoce como NO venta (aporte, préstamo, saldo inicial, devolución, ajuste) va a «Otros» con su motivo, en vez de a ventas.
  - Conservador: sólo con `es_reconocido()`. Un detalle libre sin vocabulario («Juan», «mostrador») sigue siendo venta como hoy, para no romper los libros de los clientes actuales.
  - Test con un LD de forma real.
  - **Diagnóstico read-only en Neon** (lo corre el dueño con un script SELECT-only): ventas importadas de un Libro Diario cuyo detalle matchee aporte/préstamo/saldo inicial. Si aparecen, se reparan con un plan aparte; nunca dentro de este PR.
- **D1. Decisiones de producto** (antes de modelar, ver «Preguntas abiertas»).
- **D2. Modelo:** tabla propia `owner_capital_movements`. **No** en `sales_entries` ni en `expense_entries`: un aporte no es facturación ni costo, y meterlo ahí contamina ventas, margen y gastos.
  - Columnas: `tenant_id`, `occurred_at` (fecha de NEGOCIO, invariante 2d), `kind` (`aporte` | `retiro`), `partner_name` (texto opcional; no es un maestro nuevo), `amount_ars` (> 0, el sentido lo da `kind`), `payment_method` (efectivo / banco), procedencia (`manual` | `archivo` con `source_upload_id` + `source_row_ref` | `onboarding_declarado`), `notes`, `voided_at`.
  - Migración aditiva.
  - Borrado de archivo (F11) y relectura lo revierten por procedencia, como cualquier fila importada.
  - Escritura por un comando con `decision_audit_log`.
- **D3. Métricas, sólo por FactsService** (invariante 2c):
  - Nuevos facts `aportes_de_capital` y `retiros_de_socios` por período.
  - **Flujo operativo** (ventas − gastos) y **flujo con aportes**, por separado.
  - Insight template-based: «En los últimos N días el negocio se financió con aportes por $X (Y% de lo que salió)».
  - Un aporte NUNCA suma a ventas, margen, rentabilidad ni score de rentabilidad. A liquidez sólo llega a través de la caja contada (arqueo); no la infla por declaración.
- **D4. Negocio que arranca de cero (onboarding + carga manual):**
  - **Onboarding:** «¿Con cuánta plata arrancás el negocio?» → un aporte en el día cero con procedencia `onboarding_declarado`. Vale `PESO_DECLARADO` en completeness (invariante 5b): declarar el capital **no** habilita conclusiones por sí solo. Si además dice cuánto sigue en caja o banco, eso alimenta `cash_on_hand_ars` como hoy, sin duplicar.
  - **Carga manual:** «Aporte de socio» en la carga flotante, y una sección en Balance con aportes y retiros por socio.
- **D5. Ingesta:**
  - En el libro mixto, «aporte» pasa de «Otros» a un destino importable `aporte`, con contexto `sheet:X:aportes`.
  - Una hoja suelta de aportes (socio / fecha / monto) se clasifica por contenido.
  - En el Libro Diario, D0 deja los aportes reconocidos en «Otros». D5 los vuelve importables.
  - La detección de repetición masiva y el signo negativo (`monto_negativo`) aplican igual.
- **D6. Chat:** `ANSWER_DATA_QUERY` puede responder «¿cuánto pusimos?» leyendo FactsService. **Registrar** un aporte por chat necesita un `ActionType` nuevo (catálogo cerrado + RiskEngine). Queda fuera de este plan salvo que se pida.

### Decisiones (D1, 2026-10-06)

1. **Los retiros también pasan a la tabla nueva.** Repartir ganancia no es un costo del negocio. Hoy un retiro de ganancias es un gasto PAYROLL/OPEX y baja margen y rentabilidad; deja de hacerlo.
   - **Migrar los existentes es una reparación de datos con impacto visible:** los gastos de los meses con retiros bajan y el score de rentabilidad sube. Va con un script con dry-run (`backend/scripts/`, SELECT primero, `--apply` lo corre el dueño) y un aviso en el producto, nunca dentro de una migración de Alembic.
   - `POST /expenses/profit-withdrawal` sigue funcionando para un frontend viejo (el deploy es en paralelo), pero escribe en la tabla nueva.
   - El PIN step-up que hoy protege el retiro se mantiene.
2. **Socios: nombre libre** por movimiento, agrupado por nombre normalizado. No hay maestro nuevo.
3. **En especie: mercadería y equipamiento.** ⚠️ Es lo más cercano a contabilidad. Para no cruzar el límite de alcance:
   - **Mercadería aportada:**
     - entra como stock con `stock_treatment = opening_balance` (sin COGS ni salida de caja, como hoy);
     - **además**, un aporte valorizado al costo, vinculado al ingreso de stock;
     - el costo de esas unidades sigue saliendo como COGS cuando se venden, igual que cualquier stock.
   - **Equipamiento:**
     - se registra como un aporte valorizado, con descripción;
     - es **informativo**: no se amortiza, no entra a ningún costo ni al margen, no hay bienes de uso ni balance;
     - sirve para responder «¿cuánto puso cada socio en total?».
   - Todo aporte en especie dice que lo es, y su valor es el que declaró el usuario (procedencia declarada, nunca una valuación de Véktor).
4. **Préstamo de socio = aporte marcado «a devolver»**, y su devolución es un movimiento propio.
   - **Sólo cuando el préstamo es de un SOCIO.** Un préstamo bancario o de un tercero sigue en «Otros».
   - En un archivo, un «préstamo» que no dice de quién es ambiguo y sigue en «Otros». Ante la duda, no se asume que es de un socio.

### Modelo (reemplaza el borrador de D2)

Tabla `owner_capital_movements`:

| Columna | Qué guarda |
|---|---|
| `tenant_id` | — |
| `occurred_at` | Fecha de negocio (invariante 2d) |
| `kind` | Uno de cuatro: `aporte`, `retiro`, `prestamo_de_socio` (aporte a devolver), `devolucion_de_prestamo` |
| `form` | `dinero` \| `mercaderia` \| `equipamiento` |
| `partner_name` | Opcional |
| `amount_ars` | > 0; el sentido lo da `kind`. En especie, es el valor declarado |
| `payment_method` | Sólo `dinero`: efectivo / banco |
| `inventory_movement_id` | Mercadería: el ingreso de stock vinculado |
| `description` | Equipamiento y notas |
| procedencia | `manual` \| `archivo` (`source_upload_id` + `source_row_ref`) \| `onboarding_declarado` \| `migrado_de_gasto` (con el id del gasto original, para poder revertir) |
| `voided_at` | — |

- Migración aditiva.
- F11 y la relectura revierten por procedencia.
- Toda escritura pasa por un comando con `decision_audit_log`.

### Métricas (amplía D3)

- **`aportes_de_capital`:** separa dinero y especie.
- **`retiros_de_socios`.**
- **`prestamos_de_socios_pendientes`:** prestado menos devuelto.
- **Por socio:** lo aportado en dinero, en especie y lo retirado.
- Ninguno de estos movimientos toca ventas, gastos, margen ni rentabilidad.
- **Sólo los de dinero** explican la caja, en el flujo «con aportes» separado del flujo operativo.

### Orden dentro de D

1. **D0:** arreglo del Libro Diario + diagnóstico read-only. Es urgente y chico, y va antes que todo el resto del plan.
2. **Modelo + comando + carga manual de aportes y retiros en dinero.**
3. **Migración de los retiros existentes.** Script con dry-run; el dueño corre el `--apply`.
4. **Facts + Balance** (por socio, préstamos pendientes) **+ insight.**
5. **Onboarding:** capital con el que arranca.
6. **En especie:** primero mercadería (vinculada al stock), después equipamiento.
7. **Ingesta:** destinos importables en el libro mixto, en el Libro Diario y en hojas sueltas.

---

## Qué cambió en la revisión (2026-10-06)

1. **F-R pasó a ser el paso 1 y se re-encuadró.** El preview con confirmación ya existe; lo que falta es que el conteo distinga «se reemplaza» de «se pierde». El 409 queda como red de seguridad del servidor, no como el arreglo principal.
2. **El cherry-pick de F-A tenía un bug de seguridad:** una columna marcada `user_selected` saltea la compuerta F8. El mismo bug ya está en el camino multi-hoja de `main`, y se arreglan los dos.
3. **Total/Subtotal de forma conservadora**, con «Total Clean» como test.
4. **El dedup de `external_code` respeta el índice único**, y el código del perdedor nunca se pierde en silencio.
5. **El vínculo por código nunca crea un maestro.**
6. **Las reglas de la cola de ventas se fijan antes de escribirla:** vincular no mueve stock, el alias es clave débil y no lo usa el POS, conflicto = no vincular, merge y F11. Además se parte en backend y pantalla.
7. **F-D decide la reversión antes de empezar**, para evitar la migración si se puede.
8. **Reglas transversales nuevas:** revisión de código antes de cada merge, deploy en paralelo, idempotencia.
9. **B1:** confirmar el ancho de la térmica antes de ir, planilla de resultados, contingencia si el arranque sin red falla, y B7b sigue siendo por PC.
10. **`.venv312`:** el intento anterior se revirtió y hay que entender por qué; además falta `.dockerignore` y un test que lo impida.
