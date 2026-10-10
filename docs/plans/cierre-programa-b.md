# Cierre completo del programa B del POS

## Contexto

B0–B13 están en producción y B14 (suscripción vencida ≠ caja bloqueada) está en el PR #71, con CI verde y sin mergear. La revisión externa dejó cinco huecos abiertos. El usuario pidió **no postergar nada**:

1. **Terminal reemplazada.** Las ventas pendientes de una caja dada de baja quedan trabadas para siempre: `pendientesSincronizables` exige `o.terminalId === terminalId` (`frontend/src/lib/offline/posDb.ts:265`). «Reintentar» no lo arregla, y el runbook dice lo contrario (`docs/runbooks/pos.md:201`).
2. **Venta por peso/volumen (B3).** No hay manera de configurar `sale_unit` ni el factor. Varias cuentas no dividen por el factor (detalle en la PR 3), las compras no conocen el factor y la caja pide gramos enteros.
3. **Aprendizaje de códigos sin red (B4).** Hoy necesita servidor. Además, sin red el buscador por nombre tampoco funciona.
4. **Alcance de B5.** Faltan el permiso de cambio manual de precio y el permiso delegable para resolver diferencias de stock (`archive/b5-rol-cajero.md:38` los dejó para «cuando exista la acción»).
5. **B1 y B15.** No hay evidencia. El runbook y el acta están sin commitear en `Vektor-b14`.

### Revisión externa de origen (2026-10-02) y dónde se resuelve cada punto

| Fase | Estado según la revisión | Lo cierra |
|---|---|---|
| B0, B2, B9, B10 | Integradas, sin pendientes concretos | — |
| B3 | Contrato y cálculos hechos; falta habilitar gramos y litros | **PR 3** |
| B4 | Escaneo y aprendizaje con red hechos; falta aprender sin red | **PR 4** |
| B5 | Rol y 4 permisos; diferencias con el alcance original | **PR 2** |
| B6–B7 | Terminales y offline hechos; la recuperación al reemplazar una terminal falla | **PR 1** |
| B8 | Backend y pantalla integrados (PR #70) | — |
| B11–B13 | Implementados; falta la validación física (impresión, lector, funcionamiento completo) | **Verificación 5–8** |
| B14 | CI verde, PR #71 abierta | **PR 0** + Verificación 1 |
| B1, B15 | Sin evidencia | **Verificación 5–9**; el runbook y el acta se commitean en la **PR 1** |

Puntos concretos de la revisión y cómo se tratan:
1. **B14:** mergear, verificar el deploy y comprobar en producción que `READ_ONLY` cobra y bloquea el análisis → PR 0 + Verificación 1.
2. **B1:** el acta está vacía (reinicio sin red, turno y ventas conservados, sincronización sin duplicados, `persisted()`, impresión, lector) → Verificación 5.
3. **B15:** el runbook y el acta están redactados pero sin agregar a Git. Faltan resultados, versiones desplegadas, mediciones y constancia de limpieza → se commitean en la PR 1; se completan en la Verificación 8–9 y con los sha de cada merge.
4. **B6/B7 — terminal reemplazada:** el id nuevo deja las ventas pendientes fuera de la cola (`posDb.ts:265`) y «Reintentar» no cambia la pertenencia. Hace falta una recuperación autorizada y probada, y corregir el runbook → PR 1 + Verificación 7.
5. **B3/B13 — peso y volumen:** el alta y la edición no aceptan `sale_unit` ni el factor, y la valorización todavía no divide por el factor (`products/page.tsx:371`) → PR 1 (valor del inventario) + PR 3 + Verificación 2.
6. **B4/B7 — aprender sin red:** guardar las asociaciones pendientes, sincronizarlas y resolver los conflictos sin modificar ventas anteriores → PR 4 + Verificación 6.
- **Alcance de B5:** el cambio manual de precio y el permiso delegable para resolver diferencias se **implementan**, no se postergan → PR 2 + Verificación 3–4.
- **B11:** las dos resoluciones actuales son una decisión documentada; la tercera resolución del plan original (`register_shrinkage`) **no** es un olvido y no se agrega, porque no cierra la aritmética (ver el bullet de B11 en `CLAUDE.md`).

Resultado buscado: el acta queda con los dos cierres, **«(1) Caja validada»** y **«(2) Alcance original completo»**, incluido el escenario 4 (gramos) y uno nuevo de aprendizaje sin red.

Reglas que valen para todos los PRs:
- Rama propia desde `main` actualizado.
- La documentación de `CLAUDE.md` va en el **mismo commit** que el cambio.
- Tests nuevos verificados por mutación: revertir el arreglo y confirmar que fallan.
- Correr `ruff check`, `mypy`, `pytest`, `jest`, `tsc` y `next build`. Si hay migración, además `pytest -m postgres -n 0` sobre una `vektor_pgtest` limpia, con `DATABASE_URL` local.
- `/code-review` antes de pushear.
- Merge **sólo con OK del usuario**, y verificar el deploy con una sonda de ruta (404→401) cuando haya una ruta nueva.
- Nunca correr `ruff format`.

---

## PR 0 — Mergear B14 (#71)
- Pido OK y mergeo con squash.
- Sonda: una ruta de B14 sin token tiene que dar 401.
- Actualizo la memoria.

## PR 1 — Recuperar las ventas de una caja reemplazada + valor de inventario + runbook/acta

**Backend**
- Ruta nueva `POST /pos/terminals/adopt-pending` en `api/v1/pos.py`, sólo OWNER con PIN (`require_owner_stepup`, igual que el alta y la baja de terminales).
  - Body: `{previous_terminal_id: UUID | null, client_operation_ids: list[str] (1..500)}`.
  - La terminal **actual** sale del header `X-POS-Terminal` vía `pos_terminal_service.resolver_terminal`; si no viene, 403 `TERMINAL_REQUIRED`.
  - Validaciones:
    - `previous_terminal_id` tiene que ser del mismo tenant. Si no, 404.
    - Tiene que ser distinta de la actual. Si no, 422 `SAME_TERMINAL`.
    - Tiene que estar **dada de baja**. Si no, 409 `PREVIOUS_TERMINAL_ACTIVE`: no se le roban ventas a una caja viva.
  - Audita `POS_PENDING_REASSIGNED` con las dos terminales y la lista de operaciones.
  - Es idempotente: repetir la llamada sólo vuelve a auditar.
  - No toca `pos_operations`, porque esas ventas todavía no subieron.
  - La lógica va en un helper nuevo, `pos_terminal_service.autorizar_adopcion(...)`.
- La venta adoptada sube con la terminal nueva en el header. La huella de idempotencia no cambia, porque la terminal no viaja en el cuerpo.
- Ya verifiqué que `abrir_o_adoptar` (B8) no compara la terminal, así que el turno de la venta se adopta sin conflicto.

**Frontend**
- `posDb.ts`: nueva función `reasignarTerminal(tenantId, deTerminal, aTerminal, ids)`. En una sola transacción reescribe `terminalId` y pasa `REQUIERE_AUTENTICACION` a `PENDIENTE`.
- `PendingOperationsPanel.tsx`: agrupa las ventas no sincronizadas cuya `terminalId` ≠ la terminal actual, que tienen que ser de esta PC porque IndexedDB es local. Muestra el aviso «N ventas se cobraron en la caja anterior. Pasarlas a esta caja» y el botón llama a la ruta; el 428 abre el PIN por el interceptor que ya existe. Recién con el 200 reescribe y dispara `flush`.
  - El botón sólo se le muestra al OWNER. Un cajero ve «pedile al dueño que las pase a esta caja».
- `TerminalsSection.tsx`: después de «Habilitar esta PC», si hay ventas de otra terminal, avisa en el momento.

**Otros**
- `products/page.tsx:371`: el valor del inventario pasa a usar el helper `valorDeStock` nuevo de `lib/pos/saleUnit.ts`, espejo de `sale_unit.valor_de_stock`. El helper lo usa también la PR 3.
- Commitear `docs/runbooks/pos.md` (con la línea 201 corregida y la sección «Reemplazar una PC») y `docs/plans/b15-piloto-2026-09-29.md`. Los dos se copian desde `Vektor-b14`.

**Tests**
- Backend (`test_pos_terminals.py`):
  - Permisos: el cajero recibe 403 y sin PIN da 428.
  - Terminal anterior activa → 409; de otro tenant → 404; la misma → 422.
  - Se registra la auditoría.
  - La ruta queda fuera de `CASHIER_ALLOWED_ROUTES`; lo cubre el barrido.
- Jest:
  - Una venta de la terminal vieja no sube.
  - Después de adoptarla sube una sola vez, con el header de la terminal nueva.
  - Si el servidor rechaza la adopción, no se reescribe nada.

## PR 2 — B5: permisos `price_override` y `resolve_discrepancy` (migración `20261002_0001`)

**Permisos**
- Sumar `PRICE_OVERRIDE` y `RESOLVE_DISCREPANCY` en `domain/pos_permissions.py` y en `frontend/src/lib/roles.ts` (`POS_PERMISSIONS`); `SecurityPanel` los muestra solo.
- `pin_service.puede_tener_pin` acepta también `RESOLVE_DISCREPANCY`.

**Precio manual**
- `PosLineRequest` gana `manual_unit_price_ars: Decimal | None = None` (> 0, 2 decimales) y `manual_price_reason: str | None = None` (obligatorio si hay precio manual → 422 `PRICE_OVERRIDE_REASON_REQUIRED`). El default `None` no mueve `hash_peticion`.
- Se chequea con `assert_pos_permission(..., PRICE_OVERRIDE)` junto a DISCOUNT y FIADO (`pos.py:~276`): antes del claim y sólo si la venta es nueva.
- El token firmado se sigue verificando igual. Acredita el precio de lista que vio la caja, y ese valor queda en `unit_price_catalog_ars`.
- El precio manual entra como `precio` en `LineaPedida`, así que `calcular_operacion` no cambia.
- Migración aditiva e idempotente (convención E8c) en `pos_operation_lines`: `price_overridden BOOLEAN NOT NULL DEFAULT false` y `price_override_reason VARCHAR(200) NULL`.
- Se audita `POS_PRICE_OVERRIDE` por línea: lista, manual, motivo y usuario.

**Diferencias de stock delegables**
- Nuevo `require_discrepancy_access` en `deps.py`, siguiendo el modelo de `require_void_access`: el cajero necesita el permiso más su PIN; para el resto vale `require_modify_access`.
- Las dos rutas de `/pos/stock-discrepancies` entran a `CASHIER_ALLOWED_ROUTES`. El GET pasa a OWNER/ADMIN o al permiso.
- Un cajero con el permiso puede usar `ajuste_de_entrada`. Para `anular_ticket` aplican **además** las reglas de anulación de B10: `void_ticket` y ticket propio (`VOID_NOT_OWN_TICKET`). Así el permiso nuevo no se saltea la anulación.
- Frontend: `StockDiscrepanciesPanel` pasa a decidir con `hasPosPermission` y se monta también dentro de `PosScreen`, en un botón «Diferencias de stock», porque el cajero no llega a `/products`.

**Carrito**
- `CartLine.overridePriceCents?` y una acción `SET_LINE_PRICE` (sólo en estado `editando`, con motivo).
- `precioEfectivo(l)` lo usan `lineNetCents`, `previewTotals`, `buildPayload` y `reciboLocal`.
- En la pantalla, el precio de la línea se puede editar si el usuario tiene `price_override`.
- Además, cubrir con un test `APPLY_PRICES` (`cart.ts:518`): cambia `sale_price_ars` y conserva el `price_token` viejo. Si el test confirma que eso da `PRICE_TOKEN_INVALID`, `APPLY_PRICES` también actualiza o descarta el token.

**Tests**
- `test_cashier_access.py`: el dict de permisos pasa a 6 y se agregan casos para cada permiso nuevo.
- `test_pos_operations.py`:
  - Precio manual sin permiso → 403.
  - Precio manual sin motivo → 422.
  - Con precio manual: Σ líneas = total y la auditoría queda registrada.
  - Un reintento con el permiso revocado devuelve el ticket.
- Diferencias: el cajero ajusta, pero no anula un ticket ajeno.
- Jest del carrito y del panel.

## PR 3 — B3: venta por peso o volumen de punta a punta

**Contrato único, el mismo que ya usa la caja:**
- Cantidad y stock, siempre en **unidades base** (g/ml).
- Precio y costo, siempre **por unidad de venta** (kg/L).
- Importe = precio × cantidad / factor.

La interfaz convierte kg ↔ g. No cambia ningún tipo en la base: `quantity` y `stock_units` siguen siendo enteros.

**Backend**
- `CreateProductRequest`/`UpdateProductRequest` (`schemas/product.py`) ganan `sale_unit: Literal[...]` y `base_units_per_sale_unit: int ≥ 1`.
  - Si la unidad es `unit`, el factor tiene que ser 1.
  - Para `gram`/`milliliter` sólo se aceptan los factores con nombre: 1 o 1000.
- `update_product`: cambiar la unidad o el factor de un producto **con historia** (stock > 0, o ventas, o movimientos) → 409 `SALE_UNIT_LOCKED`. Si no, el histórico cambiaría de significado.
- Helper único `sale_unit.importe(precio, cantidad, factor)`, reusando la fórmula de `importe_bruto_en_centavos`. Se aplica en:
  - `api/v1/insights.py:261-275` y `:367-377`.
  - `api/v1/sales.py:455` (`manual-batch`).
  - `data_repair_service.py:274/296/1091`.
  - `agents/income/agent.py:440`.
  - `agents/supplier/agent.py:704`.
  - Compras: `purchase_service.py:161`, `ingestion_import_service.py:3119/9656` (`import_receipt`), `others.py:686`. En `others.py` el costo pasa a ser `amount × factor / quantity`, para que quede expresado por unidad de venta.
  - `stock_service.increment_stock`: documentar que `unit_cost` es por unidad de venta. `inventory_repository.py:133/294` usa el mismo helper.
- Compuerta: un test que hace grep de `stock_units *` / `quantity *` sobre los campos de precio o costo fuera del helper, al estilo de `test_las_rutas_con_gate`.
- Umbral de stock bajo: `effective_threshold(threshold, factor)`. El default pasa a `5 × factor` (5 kg), y se aplica en `domain/product.py`, `insights.py:290`, `stock_service.get_low_stock_products` y en `is_low_stock`. `ProductStockItem` expone `sale_unit`, el factor y `stock_display`.

**Frontend**
- Tipos `ProductResponse`/`CreateProductPayload` con los campos nuevos.
- Selector «Se vende por: unidad / kg / L» en el alta (`ManualEntrySection.tsx`) y en la edición (`ProductEditModal`). Stock y umbral se cargan en kg/L con hasta 3 decimales y se convierten a base; precio y costo, «por kg».
- Las pantallas muestran `stock_display`: `/products`, `DashboardAnalysisScreen`, el stock crítico de `dashboardData.ts` (que deja de sumar unidades mezcladas) y `ManualEntrySection`.
- Etiquetas y caja: el precio dice «/kg» (`labelSheet.ts`, `CartTable.tsx`) y se corrige `unitLabel`.
- Caja:
  - `QuantityInput` acepta kg con 3 decimales cuando el factor es > 1 y convierte a base con `Math.round(kg × factor)`.
  - `ADD_PRODUCT` de un producto por peso agrega 1 unidad de venta y deja el foco en el peso.
- Remito y compra manual: la cantidad se carga en kg/L y se manda en base.

**Tests**
- Backend:
  - Alta y edición con la unidad (validaciones y `SALE_UNIT_LOCKED`).
  - Cada lector arreglado con un producto de 1000 g/kg, comprobando el importe exacto.
  - Una compra de 5 kg a $800/kg deja stock 5000 y costo 800.
  - Umbral por defecto en 5 kg.
- Jest: conversión kg↔g, carrito con 0,750 kg × $1.234,56 = $925,92 (el mismo caso del backend) y formularios.

## PR 4 — B4: aprender códigos sin red

- `posDb` **VERSION 2**, con un store nuevo `aprendizajes`. Cada registro tiene: `{tenantId, codigo, productId, userId, terminalId, estado: PENDIENTE|SINCRONIZADO|REQUIERE_REVISION, creadoEn, ultimoCodigo, intentos}`.
- **Superposición derivada:** `catalogDb.catalogoDeCaja` agrega en memoria los códigos pendientes al producto, con el mismo patrón que `stock_estimado`. `sincronizarCatalogo` nunca los pisa, porque viven en otro store.
- **Precondición local:** antes de aprender se corre `buscarEnCatalogo(clasificar(code))` sobre el catálogo superpuesto. El código tiene que ser aprendible, no responder ya a otro producto y el producto no puede tener otro barcode. Si alguna falla, se rechaza en el acto con el mismo texto que el servidor.
- **Buscador sin red:** `ProductPicker` usa una búsqueda local por nombre o SKU sobre `catalogoDeCaja` cuando `posService.searchCatalog` falla por red.
- **Sincronización:** `usePosSync.flush` sube los aprendizajes después de las ventas, con la regla de pertenencia por tenant, usuario y caja, y `X-POS-Offline: 1`.
  - 200 → `SINCRONIZADO`.
  - `BARCODE_TAKEN`, `BARCODE_ALREADY_SET` o `SCAN_CODE_NOT_LEARNABLE` → `REQUIERE_REVISION`. La superposición deja de aplicarse y **no se toca ninguna venta**.
  - Error de red → reintento sin gastar presupuesto.
- **Backend:** `learn_product_barcode` lee `X-POS-Offline` (`_es_offline`) y agrega `learned_offline` y `terminal_id` a `PRODUCT_BARCODE_LEARNED`. No hay migración.
- **Panel:** «Ventas sin sincronizar» muestra también «Códigos por revisar», con producto, código y motivo, y permite descartar.
- **Tests:**
  - Jest: aprender sin red resuelve el segundo escaneo, sube una vez y `BARCODE_TAKEN` pasa a revisión. Se prueban la migración de la base v1→v2 sin perder ventas, el picker sin red y que el catálogo reemplazado no borra lo pendiente.
  - Backend: la auditoría lleva la marca de sin red.
  - Corpus: casos de `buscarEnCatalogo` en `shared/pos/scan_corpus.json` probados por los dos lados.

## Actualización final de documentación

En el commit de cada PR:
- `CLAUDE.md`: bullets nuevos y corrección de los «Declarado sin hacer» de B3, B4, B5 y B7.
- El runbook, con las tareas nuevas del dueño: pasar ventas de una caja reemplazada, configurar un producto por peso, precio manual y diferencias delegadas.
- El acta: el escenario 4 deja de figurar como «no ejecutable» y se suman el 15 (aprender sin red), el 16 (precio manual) y el 17 (caja reemplazada).

## Verificación (la ejecuta el usuario después de cada deploy; me pasa los resultados y yo completo el acta)

**Tenant demo (`demo.kiosco@vektor.app`)**
1. B14: pongo la suscripción en `READ_ONLY`. La caja tiene que cobrar, imprimir y cerrar el turno; `/dashboard`, `/dashboard/analisis` y `/resumen-economico` tienen que decir «el análisis está pausado». Después la restauro y lo confirmo con `show`.
2. Peso: crear «Queso PRUEBA» por kg a $10.000/kg con stock 2 kg. Vender 0,750 kg: el total tiene que ser $7.500 y el stock tiene que mostrar 1,250 kg. En `/products`, el valor del inventario tiene que usar el costo por kg. Un remito de 1 kg tiene que dejar el stock en 2,250 kg. Cambiar la unidad del producto tiene que dar «no se puede, tiene historia».
3. Precio manual: un cajero sin el permiso no puede editar el precio; con el permiso, el motivo es obligatorio. El ticket tiene que mostrar el precio cobrado y la auditoría tiene que tener `POS_PRICE_OVERRIDE`.
4. Diferencias delegadas: un cajero con el permiso resuelve un ajuste desde la caja con su PIN y no puede anular el ticket de otro.

**ASTERIA, en la PC del local (B1 y B15)**
5. B1 completo: preparar la PC, cortar la red de verdad, abrir el turno, vender, reiniciar, abrir desde el ícono, vender otra vez y reconectar; las dos ventas tienen que subir una sola vez. Mediciones: `persisted()`, `@page` A/B/C × papel, corte y cajón, y el lector (terminador, ms entre teclas, QR, etiqueta).
6. Aprender sin red: cortar la red, escanear el EAN de «PRUEBA POS Aprender», vincularlo, escanearlo de nuevo (tiene que resolverlo), reconectar y verificar `PRODUCT_BARCODE_LEARNED` con la marca de sin red.
7. Caja reemplazada: sin red, cobrar una venta. Dar de baja la caja desde otra PC, volver a habilitar esta y pasar las ventas con PIN. Al reconectar tienen que subir una sola vez y tiene que quedar `POS_PENDING_REASSIGNED`.
8. Escenarios 1–3 y 5–14 del acta, con el 13 en un solo bloque y la suscripción restaurada al final.
9. Limpieza completa según la tabla del acta, y lo verifico con consultas de sólo lectura en Neon.
