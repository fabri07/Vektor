# F-M · Reconocimiento de encabezados en dos capas

> Fase nueva, insertada **antes** de terminar F-H6.c/d. La distribución de costos
> no se puede construir sobre un reconocedor que convierte un flete en un precio
> de compra.

> **Estado (2026-08-10): CERRADA, 7/7.** El 7 entró en `309bd0f2` (targets de
> compra `discount`/`taxes`/`shipping_cost_line`) y sus consumidores en F-H6.c/d.
> El camino no fue recto: el cableado se revirtió a mitad porque la
> tabla nueva sabía menos que la vieja. Lo que se aprendió está incorporado abajo
> y marcado con «CORRECCIÓN» — el plan original decía otra cosa en tres puntos, y
> re-litigarlos costaría repetir el incidente.
>
> Este archivo vivía en `~/.claude/plans/`, fuera del repo. Se versionó acá
> porque un plan que sólo existe en una sesión ya se perdió dos veces en este
> mismo programa.

## Contexto

F-H6.c/d necesitaba mapear columnas nuevas de una compra (envío, descuento,
impuestos). Al medir cómo resolvían los encabezados reales —en vez de asumirlo—
aparecieron dos cosas: un bug de acentos, y abajo, el problema de fondo.

**El modo de falla no es de vocabulario, es del enfoque.** `_heuristic_match`
elige *la keyword ganadora*: el substring más largo que aparezca en el header.
Los encabezados reales combinan un sustantivo núcleo con modificadores, y cuando
un modificador coincide con la keyword de otro campo, **el modificador le gana al
núcleo**:

| Encabezado | Resuelve hoy a | Debería |
|---|---|---|
| `Bonificación proveedor` | `supplier_name` (`proveedor`, 9 > `bonificacion`) | un descuento |
| `Total factura sin impuestos` | `taxes` (`impuestos`, 9 > `total`, 5) | un monto |
| `Envío unitario` | `unit_price` (`unitario`, 8 > `envio`, 5) | un envío |
| `Precio con IVA` | `taxes` (`iva` es el único match) | un precio |
| `Costo final por producto` | `product_name` (`producto`, 8 > `costo`, 5) | un costo |

Las cinco son la misma falla, y ninguna es rara. El daño no es cosmético: un
flete que entra como precio de compra corrompe el costo del producto, el margen y
la valuación de stock — el mismo eje de bugs que ya se pagó con el incidente
ASTERIA y que motivó F10.

Agregar keywords no lo arregla: cada keyword nueva es un competidor más en la
misma pelea por longitud. El propio docstring de `_heuristic_match` ya admite que
**la ambigüedad existe hoy y se resuelve en silencio** («ante un empate de
longitud gana el primero declarado en `_HEURISTICS`»).

**Regla rectora de la fase:** si Véktor no puede demostrar qué quiso decir el
usuario, **conserva el dato y pide confirmación; nunca lo transforma en silencio
en otro concepto contable.** Es la misma regla que ya gobierna las fechas
(F6-A2), las filas sin monto (F-H4), el envío sin comprobante (F-H6.b) y el
replay que no se puede validar (F-H3.d.6).

---

## Diseño

### 1. Dos capas: qué concepto es, y qué semántica tiene

Módulo puro nuevo: `backend/app/domain/header_semantics.py`.

- **Concepto (el sustantivo núcleo):** `precio · monto · envio · impuesto ·
  descuento · cantidad · fecha · producto · proveedor · cliente · comprobante ·
  metodo_pago · recurrencia · nota`.
- **Calificador (lo que modifica, nunca lo que identifica):**
  - granularidad — `unitario · por_linea · por_comprobante · total`
  - fiscal — `con_impuesto · sin_impuesto`
  - rol — `de_compra · de_venta · de_lista · final`
  - entidad — `de_producto · de_proveedor · de_cliente`

**Un calificador nunca puede ser la respuesta.** `envío + unitario` sigue siendo
envío; `descuento + proveedor` sigue siendo descuento; `precio + IVA` sigue
siendo precio.

Reglas de análisis, sobre los tokens del `match_key`:

- **R1 — inclusión.** Un token de concepto precedido por `sin`/`con` es un
  calificador fiscal, nunca el núcleo. (`sin impuestos`, `con IVA`)
- **R2 — entidad.** Los tokens de entidad (`producto`, `proveedor`, `cliente`,
  `factura`, `comprobante`, `linea`, `unitario`) son calificadores **cuando hay
  otro concepto presente**; son el núcleo sólo si están solos.
- **R3 — bigramas primero.** `forma pago` es un concepto, no `forma` + `pago`.
- **R4 — dos núcleos = ambiguo.** Si después de R1–R3 queda más de un candidato a
  núcleo, no se elige: se pregunta.

La normalización de acentos vive acá, en el tokenizador — que es donde
corresponde. Todos los vocabularios están escritos sin tilde, así que `Envío`,
`Artículo`, `Mercadería`, `Categoría` y `Bonificación` hoy **no matchean nada**
(medido: el problema no era que ganara el target equivocado, era que no había
sugerencia). Se delega en `app/domain/text_norm.normalize_text`, que es la cadena
canónica NFKD y pide explícitamente que no se la copie.

### 2. Tres resultados

```python
@dataclass(frozen=True)
class HeaderReading:
    outcome: Literal["unico", "ambiguo", "sin_evidencia"]
    target: str | None            # sólo en `unico`
    options: tuple[str, ...]      # en `ambiguo`: los candidatos
    duda: str | None              # en castellano, por qué no alcanza
    concept: str | None
    qualifiers: frozenset[str]
```

| Encabezado | Resultado |
|---|---|
| `Precio unitario` (compra) | **único** → `unit_price` |
| `Bonificación proveedor` | **único** → `discount` (el calificador no cambia el concepto) |
| `Precio con IVA` | **ambiguo** → `[unit_price, amount]` · «¿es el precio de cada unidad o el total de la línea?» |
| `Envío` | **ambiguo** → `[shipping_cost, shipping_cost_line]` · «¿el envío de todo el comprobante, o el que le toca a esta línea?» |
| `Envío unitario` | **sin evidencia** · «Es un costo de envío por unidad, y Véktor todavía no tiene un campo para esa granularidad.» |
| `Total factura sin impuestos` | **sin evidencia** · «Parece el total del comprobante, no el de esta línea.» |
| `xyz_123` | **sin evidencia** · sin duda: no se reconoció nada |

`sin_evidencia` con `duda` y sin `duda` son el mismo resultado para el importador
—la columna no se mapea sola— y distintos para la persona: «no entiendo esto» y
«entiendo qué es pero no tengo dónde ponerlo» no se explican igual.

### 3. La cadena de capas: el ambiguo corta

Hoy `suggest_mappings` (`column_mapping_service.py:867-885`) es un `if/elif/else`
y un tercer estado no tiene rama. Las dos salidas obvias son peores que el bug:

- si el ambiguo entra por la rama de heurística con `confidence=0.75`, **no corre
  nada después** y llega al frontend como `mapped` con un target arbitrario;
- si cae al `else`, **lo resuelve fuzzy** — la capa menos informada, que compara
  contra `_HEURISTICS` crudo sin colapsar preposiciones ni acentos.

Entonces: **`ambiguo` y `sin_evidencia`-con-duda cortan la cadena.** No corre
fuzzy ni LLM. Sólo `sin_evidencia` sin duda (no se reconoció nada) sigue al
camino de hoy.

Tampoco se manda al LLM a desambiguar. `_apply_llm_fallback` filtra por confianza
y su payload no tiene canal para candidatos (`llm_column_mapper.py:45-60`), pero
el motivo de fondo es otro: **una respuesta del LLM no es demostración de la
intención del usuario**, que es lo único que la regla rectora acepta.

### 4. Contrato y pantalla

`ColumnMappingSuggestion` (`schemas/ingestion.py:178`) suma, aditivo:

```python
status: Literal["mapped", "unmapped", "ambiguo", "required_missing"]
options: list[str] = []
duda: str | None = None
```

⚠️ `ingestion.py:1179` construye el schema con `**s` directo: toda clave nueva
tiene que estar declarada o Pydantic falla.

Frontend: el `Literal` está espejado en `ingestion.service.ts:256-267` y
`ColumnMapperPanel.tsx`. Una columna ambigua se muestra con las opciones que el
backend mandó y la duda en castellano — sin lista propia, igual que el catálogo
de campos.

### 5. Los otros dos consumidores

`supplier_extraction_service.py:65` y `remito_extraction_service.py:118` llaman a
`_heuristic_match` esperando `str | None`; son pipelines sincrónicos sin pantalla
donde desambiguar. Se les deja un wrapper `heuristic_target(normalized, entity)
-> str | None` que devuelve el target **sólo** en `unico`. Su comportamiento no
cambia: ya descartaban lo que no reconocían, y en los dos casos el usuario
confirma la extracción antes de que se persista nada.

### 6. Lo que NO entra

- El vocabulario propio de `file_parsing` (`GASTO_SIGNAL_COLS`,
  `FORMA_PAGO_COLS`, …) queda como está. Comparte sólo `match_key` con el mapeo y
  unificarlo es una fase aparte; se deja anotado.
- `_normalize_col` no se toca: es lo que se persiste en
  `tenant_column_mappings.source_column` y cambiarlo dejaría huérfano el
  historial de alias de cada tenant.

---

## Qué pasa con lo ya hecho

**Commiteado, se queda** (no lo toca esta crítica): `d7b0b6c0` (aritmética pura
del costo final) y `302eb1cd` (`identidad_de_comprobante` + `plan_line_shipping`).

**Sin commitear, se descarta** — `git checkout` de los sub-commits 3 y 4. Vuelven
después, en su lugar correcto:

| Descartado ahora | Vuelve en |
|---|---|
| Normalización de acentos en `match_key` | el tokenizador del reconocedor, que la necesita para separar tokens |
| Targets `shipping_cost_line`/`discount`/`taxes` + `SINGLE_VALUE_FIELDS` | F-M, como destinos de la tabla de resolución |
| Keywords planos (`precio_con_iva`, `neto`, …) | **no vuelven**: son la expansión plana que esta fase reemplaza |
| `PurchaseCostDecision` + validación previa al lease | F-H6.c, sub-commit 4, sin cambios |

---

## CORRECCIÓN 1 — la tabla de resolución NO es opcionalmente incompleta

`RESOLUCION` reemplaza a `_HEURISTICS` como capa de mayor prioridad. Todo concepto
que una entidad no declare deja de resolverse por esa vía. Medido sobre los 299
keywords de `_HEURISTICS` en las 5 entidades, la primera versión **perdía 20
mapeos y cambiaba 11 de campo**: `customer/cliente` y `supplier/proveedor` dejaban
de mapear a `name` —requerido, o sea 422 al confirmar— y `expense/cod_barra`
pasaba de `barcode` a `sku`, un código de barras entrando a identidad de producto.

Reglas que salieron de ahí y que el plan original no tenía:

- **Una magnitud de dinero no rivaliza con lo que mide.** «Costo envío» es un
  envío; «Precio costo» es un costo, porque entre magnitudes gana la más
  específica. Sin esto quedaban como núcleos rivales y el header se volvía
  indecidible — la familia del incidente ASTERIA.
- **Un débil que es núcleo conserva su propio rol.** «Compra» a secas ya dice de
  qué operación habla. Sólo los roles, nunca las granularidades: `total` también
  es débil, y arrastrar su `por_comprobante` convertiría la columna «Total» en una
  duda sobre el total del comprobante.

## CORRECCIÓN 2 — el corte de cadena alcanza también a «no tengo dónde ponerlo»

El plan decía que cortan `ambiguo` y `sin_evidencia`-con-duda. Al revertir se
propuso distinguir «la tabla decidió» de «la tabla calla» y dejar que lo segundo
cayera a fuzzy. **Se midió y se descartó:** con la tabla ya completa las dos
reglas dan resultados idénticos (5 pérdidas, 3 cambios), así que la causa era una
sola — la incompletitud— y la distinción no agregaba nada.

Y hay un argumento a favor de cortar que el plan no tenía: dejar caer a fuzzy un
concepto reconocido sin campo **no es neutral**. Medido: `product/Comprobante` →
`unit_cost_ars`, `customer/Cantidad` → `locality`, `customer/Vencimiento` →
`birthday`. Una columna sin mapear se completa a mano; una mapeada mal se
descubre cuando el número ya está mal.

## CORRECCIÓN 3 — los consumidores sincrónicos SÍ empeoran, salvo que declaren su contexto

El plan afirmaba que el wrapper `heuristic_target` no cambia nada para la
extracción de remitos y de proveedores porque «ya descartaban lo que no
reconocían». **Es falso y se midió:** un remito con columnas
`Producto | Cantidad | Precio | Total | Código` se quedaba sin ninguna columna de
precio, y uno con `Descripción` sin nombre de producto.

La salida no es debilitar el reconocedor. Una ambigüedad puede ser real en general
y estar **resuelta por el tipo de documento**: «Precio» en un catálogo no dice
cuál de los tres es, pero un remito es un documento de líneas. El que sabe eso es
el llamador, así que lo declara — `heuristic_target(..., prefer=(...))` elige
entre los candidatos **que el reconocedor ofreció**, nunca fuera de ellos, y sin
`prefer` un ambiguo sigue sin mapear.

## CORRECCIÓN 4 — Pydantic ignora las claves no declaradas, no falla

El plan advertía que una clave nueva sin declarar en `ColumnMappingSuggestion`
rompe el endpoint. Medido: **las ignora en silencio**. El modo de falla real es
peor —el dato no rompe nada, simplemente nunca llega a la pantalla— y por eso los
tests del contrato tienen que leer la respuesta HTTP, no el dict del servicio.

## Sub-commits

1. **Caracterización, antes de tocar nada.** Batería de ~80 encabezados reales por
   entidad que fija lo que el motor de hoy responde, **incluidas las cinco
   respuestas equivocadas**, marcadas como tales. Sin esto el cambio de
   comportamiento no es revisable: son ~40 aserciones repartidas en cinco
   archivos y ninguna dice qué pasa con un header compuesto.
2. `domain/header_semantics.py`: tokenizador (con acentos), vocabularios de
   concepto y calificador, R1–R4, `HeaderReading`. Tests puros.
3. Tabla de resolución `(entidad, concepto, calificadores) → target | opciones`,
   reproduciendo las ~40 aserciones ya pineadas (ASTERIA, los tres precios,
   `Precio unitario` distinto en venta y en catálogo).
3-bis. **Completar la tabla contra el corpus** (CORRECCIÓN 1). No estaba en el
   plan y es el paso que faltaba: sin él, el 4 rompe imports reales.
4. Cableado en `suggest_mappings` + corte de cadena + `heuristic_target` con
   `prefer` para los dos consumidores sincrónicos (CORRECCIÓN 3).
5. Contrato: `status="ambiguo"`, `options`, `duda`.
6. Frontend: opciones + duda en `ColumnMapperPanel`, espejo del `Literal`,
   `StatusDot` propio y la columna ambigua DENTRO de `needsAttention` — dejarla
   afuera permitía confirmar sin verla.
7. Targets de compra (`shipping_cost_line`, `discount`, `taxes`) +
   `SINGLE_VALUE_FIELDS`, ya sobre el motor nuevo. **Revisar ahí los dos desvíos
   declarados:** `Bonificación proveedor` debería pasar de `sin_evidencia` a
   `único → discount`, y `Envío` a secas de `único → shipping_cost` a `ambiguo`
   entre el del comprobante y el de línea.

Después de esto se retoma **F-H6.c/d** desde el sub-commit 4 del plan anterior
(`PurchaseCostDecision` + validación), 5 (`POST /purchase-groups`), 6–10.

---

## Verificación

Por sub-commit: `cd backend && make check` y la suite con el entorno del CI.
**Nunca `ruff format` ni `make format`.** `git diff --stat` + `git diff --check`
antes de commitear. Frontend: `npm run type-check && npm run lint && npm run test`.
Todo test de regresión se **mutation-testea**.

Referencia medida hoy sobre `HEAD`: **3331 passed, 78 skipped, 76.89% de
cobertura, exit 0** (31 min). Es la línea de base contra la cual comparar.

| Caso | Qué prueba |
|---|---|
| Los 5 encabezados del problema | ninguno resuelve a un concepto contable distinto del suyo |
| `Bonificación proveedor` → `discount` | un calificador de entidad no se lleva el campo (mutation: sin R2 → rojo) |
| `Envío unitario` | **sin evidencia**, no `unit_price` (mutation: si el calificador puede ganar → rojo) |
| `Precio con IVA` | ambiguo con dos opciones, no `taxes` |
| `Total factura sin impuestos` | sin evidencia, con la duda que nombra el comprobante |
| `Precio de compra` → `unit_cost_ars` | ASTERIA sigue clavado |
| `Precio unitario` en venta vs catálogo | el mismo header a targets distintos por entidad |
| `Envío`, `Artículo`, `Categoría`, `Método de pago` | los acentos resuelven (hoy son `None`) |
| Un ambiguo NO llega a fuzzy ni al LLM | mutation: dejarlo caer al `else` → rojo |
| Un ambiguo NO llega al frontend como `mapped` | contrato HTTP, en `test_column_mapping_e2e.py` |
| Batería completa antes/después | el diff de comportamiento es explícito y revisable, no un efecto lateral |
| **Corpus completo, 5 entidades** | **la compuerta que faltaba.** La batería tiene 90 filas elegidas a mano y no cubría `customer` ni `supplier`: por eso declaró «cero regresiones» sobre una rama que rompía los dos imports de maestros. El corpus no se elige — son los 299 keywords de `_HEURISTICS` × 5 entidades, con las excepciones declaradas con motivo escrito |
| Las 3 llamadas reales de remito y proveedores | un consumidor sin pantalla que pierde una columna la pierde en silencio (CORRECCIÓN 3) |
| `supplier_import` y `remito_extraction` | mismos resultados que hoy (el wrapper no cambió nada) |

**Lección de testing que cruzó toda la fase:** un test de RESULTADO no puede
pinear una capa INTERMEDIA. Tres mutaciones sobre `RESOLUCION` volvieron verdes
porque fuzzy rescataba lo que la tabla ya no decía — probaban la cadena y no la
tabla. Lo que se quiera fijar de la tabla se afirma sobre `read_header` directo.

Al cerrar: agregar F-M al orden de entrega y a la tabla de compuertas de
`docs/plans/ingestion-mapping-overhaul.md`, que es lo que se lee para saber dónde
quedó el programa.
