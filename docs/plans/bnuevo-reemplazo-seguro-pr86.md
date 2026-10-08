# B·nuevo v3: reemplazo seguro del PR #86, achicado y con compuerta de evidencia

Fecha: 2026-10-07. `main` en `9c2d7719`. Mejora `docs/plans/bnuevo-reemplazo-seguro-pr86.md` e incorpora la revisión del dueño sobre la v2.

**Lo que se aprueba ahora: sólo el paso 0 y cerrar el #86.** El paso 1 se reescribe y se presenta para OK después del paso 0, con los resultados.

## Contexto

El PR #86 (`200ff590`, +2.143 líneas, cinco rondas) reubicaba ediciones por digest de contenido. La revisión encontró 9 defectos, y 1 a 4 los crea el propio mecanismo.

La propuesta aportada diagnostica bien la causa: contenido ≠ identidad, la huella es por fila y no por efecto, y hay cuatro versiones de la correspondencia. Pero su remedio (plan versionado, preview en rollback, importador por efecto, UI de correspondencia, undo versionado) repite el patrón de corrección en cascada. Ver la memoria `feedback_correccion_en_cascada`.

**Lo que ya hay en `main`:**
- **Freno del #81** (`_ediciones_en_riesgo`, `reread_service.py:2364`). Detecta el corrimiento por los registros no editados del contexto y frena el apply.
  - **No garantiza cubrir todo.** Su propio docstring declara dos límites. Un contexto con todo editado no tiene con qué detectar. Y con importe no confiable compara por día, así que un corrimiento dentro del mismo día pasa sin verse.
- **Identidad fuerte** (E6a/E6b):
  - `clave_de_operacion` (`domain/operation_identity.py:333`);
  - `operation_identities` + `operation_identity_links`, que vinculan identidad → efecto;
  - la relectura conserva la identidad del efecto editado (`liberar_identidades_de`, `:1343-1351`).
- **Preview y apply van por caminos distintos.** El preview usa `_estimate_reread` (`:2702`) y el apply usa `_reconcile` (`:1213`).
- **`accept_data_loss`** (`:3830`, `:3956`) sólo destraba la compuerta de pérdida medida. **No borra la edición**: `_split_records` (`:768`) la conserva.

## Decisiones del recorte (respecto de la propuesta)

1. **Compuerta de evidencia antes de construir** (paso 0).
2. **Las reproducciones validan `main`, no `200ff590`.**
   - **Criterio de aceptación:** cada escenario da **el resultado correcto, o frena con rollback sin escribir**. No se exige que todos frenen.
   - Si alguno pierde o duplica datos en silencio, es un defecto de `main` y va primero.
3. **Sin traslado por contenido**, ni siquiera como candidato confirmable. Queda como opción (C), sólo si el paso 0 muestra volumen sin identidad fuerte.
4. **Sin motor nuevo de preview, de undo ni de versión.** Pero «reusar la identidad» **no unifica preview y apply por sí solo**. Si se construye el paso 1, la regla de elegibilidad es **una función pura compartida** que llaman `_estimate_reread` y `_reconcile`, y hay un test que prueba que los dos reconocen exactamente los mismos casos.
5. **Tope de unas 400 líneas.** Es una señal para reconsiderar el diseño, no una prueba de que sea seguro. Si la revisión encuentra un defecto que creó el propio paso, o se pasa del tope, se para con `needs_decision`. No se hace otra ronda.
6. **Commits separados.** B·3b (categoría) y B·nuevo (seguridad de relectura) van en PR o commits distintos, revisables y revertibles por separado. Esto incluye la doc: la matriz de portado y este plan no van en el commit de B·3b.

**Cómo queda cada hallazgo del #86:**
- **1, 2, 3, 5, 8 y 9** no tienen sujeto: no hay digest, emparejamiento por orden ni reubicación posicional.
- **4:** no se crean estados de bloqueo nuevos.
- **6:** la limitación se informa.
- **7:** se reproduce en `main` en el paso 0b y se arregla aparte si existe.

## Pasos

### 0. Evidencia (lo que se aprueba ahora)
- **0a. Diagnóstico:** `backend/scripts/diag_ediciones_en_riesgo.py`, SELECT-only, nunca imprime la URL, por tenant. Lo corrés vos con tu `DATABASE_URL`. Cuenta:
  - los registros con `has_user_edits` de archivos importados;
  - cuántos tienen vínculo en `operation_identity_links`, y de esos:
    - cuántos tienen identidad **de línea**;
    - cuántos sólo de documento;
    - cuántos tienen una identidad compartida con otros efectos o movimientos;
  - cuántos vienen de filas con más de un efecto;
  - cuántos son de archivos legacy (sin `mapping_contexts`);
  - cuántos están en contextos totalmente editados;
  - las relecturas frenadas por el #81.

  **Ausencia de dato ≠ cero.** Si una métrica no tiene fuente (por ejemplo, no hay traza de los frenos), el script imprime `sin medición` con el motivo, nunca `0`.
- **0b. Reproducciones contra `main`.** Tests de integración con aserciones de IDs, importes, stock, `source_row_ref`, vínculos de identidad y cantidad de operaciones, no sólo conteos. Cubren:
  - los 9 escenarios del #86;
  - el loop de apply (hallazgo 7);
  - un contexto con todo editado;
  - un corrimiento dentro del mismo día con importe no confiable;
  - el renombrado de hoja;
  - una relectura con `accept_data_loss=True`, para documentar qué pasa realmente con la edición (se conserva);
  - el undo de la relectura, verificando que restaure también las huellas (`source_row_ref`) y los vínculos de identidad afectados, no sólo los registros.

  Cada test pasa con «resultado correcto o freno con rollback». Los que no pasan se listan como defectos de `main`, con prioridad.
- **0c. Mensaje del freno.** Hoy no se promete ninguna salida destructiva. El freno ofrece **«dejar el archivo como está y revisar el conflicto»**. «Aceptar la pérdida y re-editar» **no** se ofrece hasta que exista y esté probada la operación que de verdad descarte la edición.
  - El motivo va como un campo nuevo en la respuesta existente: «filas corridas», «sin identificador de operación», «fila con venta y gasto», «archivo anterior a las huellas».
  - Es compatible en cualquier orden de deploy.
- **Decisión con tus números:**
  - **(A) Automatización descartada; limitación documentada.** Se registra así, nunca como «conservación automática resuelta». Si 0b mostró defectos del freno, se arreglan igual: pocos casos justifican posponer la automatización, no dejar una protección defectuosa.
  - **(B)** Construir el paso 1.
  - **(C)** Reconsiderar la confirmación manual por contenido.

- **Decisión del dueño 2026-10-07: opción A — automatización descartada; limitación documentada. Diagnóstico en producción: ningún tenant con ediciones sobre registros importados ni relecturas frenadas. Los defectos (a)(b)(c) se arreglan igual, en orden c → b → a.**
- **Defectos de `main` que dejó el paso 0** (`xfail(strict=True)` en `test_relectura_freno_escenarios.py`):
  - (c) `undo_reread` no restauraba `operation_identity_links` — **resuelto** en `fix/relectura-undo-vinculos-identidad` (foto de identidades en el `before_json` del void + `restaurar_vinculos` en el undo; ver `docs/claude/ingesta.md`).
  - (b) con identidad fuerte, fila nueva en la posición de la edición se pierde en silencio — pendiente.
  - (a) contexto todo editado con dos de tres campos cambiados — pendiente.

### 1. Traslado por identidad fuerte (borrador; se reescribe y pide OK después del paso 0)
Condiciones que tiene que resolver y probar antes de implementar:
- **Elegibilidad, en una función pura compartida** por `_estimate_reread` y `_reconcile`. Una edición en riesgo es elegible sólo si se cumplen las cuatro:
  - su efecto tiene identidad **de línea**, o se demuestra que la identidad de documento corresponde a **una sola** operación preservada (revisando sus vínculos y movimientos en `operation_identity_links` y en el inventario);
  - hay exactamente una fila fresca con esa clave en el contexto;
  - la clave es única en el archivo;
  - esa fila produce un único efecto, del mismo tipo.

  Si falta cualquiera, rige el freno del #81 como hoy.
- **La huella posicional.** El importador hoy saltea por `source_row_ref` **antes** de decidir por identidad. El paso 1 tiene que especificar:
  - qué pasa con la huella de la posición vieja;
  - cómo se procesa la fila que hoy ocupa esa posición **sin perder la protección contra reintentos**, ni para ella ni para su stock;
  - cómo queda la huella de la edición conservada.

  Se especifica primero, con revisión, y recién después se escribe código.
- **Undo.** Se usa el undo existente, con un test que confirme que restaura los registros, las huellas, los vínculos y el stock después de un traslado.
- **Tests:**
  - corrimiento con ID de línea: sin duplicar, sin saltear y con el stock correcto;
  - reintento de la misma relectura: no reaplica;
  - identidad de documento compartida, que frena;
  - clave repetida, que frena;
  - venta + gasto, que frena;
  - legacy, que frena;
  - preview = apply en todos esos casos;
  - permutar el orden da el mismo resultado;
  - mutación;
  - `-m postgres -n 0` contra `vektor_pgtest`.
- Doc en `docs/claude/ingesta.md`, en el mismo commit.

## PR #86
Cerrarlo sin merge con un comentario que deje:
- `200ff590`;
- la revisión de 9 hallazgos;
- la referencia a este plan.

La rama `fix/relectura-edicion-sin-posicion` se conserva. No se porta nada de él. **Cerrar el #86 entra en este OK.**

## Encaje con la fase 1
- La fase 1 se cierra con:
  - B·3 (ya está);
  - B·3b, en su PR;
  - el paso 0, en su propio PR (`diag`, tests 0b y mensaje 0c);
  - el ensayo tipo ASTERIA, que pasa con «resultado correcto o freno con rollback».
- `fase1-resto.js` se ajusta antes de lanzarlo:
  - el texto «B·nuevo DESCARTADO» pasa a «B·nuevo reducido: paso 0 de evidencia; automatización pendiente de decisión»;
  - el prompt de B·3b deja de tocar la matriz y los planes de B·nuevo;
  - se agrega el paso 0 como su propio step, con review de 3 lentes y merge al dar verde.
- La decisión A/B/C se toma con vos después del paso 0. El paso 1, si va, es otro OK.

## Archivos
- **Se reescribe:** `docs/plans/bnuevo-reemplazo-seguro-pr86.md`, con este contenido. Entra en el PR del paso 0, no en el de B·3b.
- **Nuevos:**
  - `backend/scripts/diag_ediciones_en_riesgo.py`;
  - `backend/app/tests/integration/test_relectura_freno_escenarios.py` (0b).
- **Se modifican (0c):**
  - el motivo en la respuesta del freno (`reread_service.py` en `_ediciones_en_riesgo` y el lugar donde se arma el error, `ingestion.py`);
  - el texto del frontend de relectura;
  - `docs/claude/ingesta.md`, con los límites reales del freno y lo que hace `accept_data_loss`.
- **Se reusan:**
  - `clave_de_operacion`;
  - `operation_identity_service`;
  - `_ediciones_en_riesgo`;
  - el undo de la relectura.

## Verificación
- **Paso 0:**
  - los tests 0b dan «correcto o freno con rollback», y los que fallan quedan listados como defectos;
  - el diagnóstico corre en local contra `vektor_pgtest` sin errores e imprime `sin medición` donde corresponde;
  - CI verde sobre el SHA revisado;
  - merge con `--match-head-commit`.
- **Cierre de la fase 1:**
  - suite backend completa con el env de CI;
  - `-m postgres -n 0`;
  - jest `--runInBand`;
  - el ensayo tipo ASTERIA;
  - registro del SHA, los comandos, las cantidades y los skips.

## Revisión externa (2026-10-07): condiciones para aprobar

Veredicto: **se avanza con el paso 0. El paso 1, tal como está escrito, NO se aprueba todavía.** El recorte es acertado: reutilizar identidades existentes y no hacer otra reubicación por contenido baja mucho el riesgo. Correcciones obligatorias:

1. **#81 no cubre todos los casos.** `_ediciones_en_riesgo` declara una limitación cuando todo el contexto está editado. Las pruebas del paso 0 suman ese caso, cambios dentro del mismo día y renombrado de hoja. Criterio de aceptación: **resultado correcto, o bloqueo con rollback**. No hace falta que todos los casos frenen.
2. **La identidad fuerte no elimina la huella posicional.** El importador saltea una fila por su posición ANTES de aplicar la decisión de identidad. El paso 1 tiene que explicar cómo procesa la fila que ocupa la posición antigua **sin perder la protección contra reintentos**, stock incluido. Necesita prueba y revisión: no alcanza con «omitir por identidad».
3. **Preview y apply usan caminos distintos.** Preview llama a `_estimate_reread` y apply ejecuta `_reconcile`. Reutilizar el servicio de identidad no los unifica. En el alcance reducido se comparte la decisión de elegibilidad y se prueba que los dos reconocen los mismos casos. No se construye el motor completo propuesto antes.
4. **El camino manual descrito es incorrecto.** `accept_data_loss` no elimina la edición, porque `_split_records` la conserva. «Aceptar la pérdida de la edición y volver a editar» puede describir un resultado falso. Hasta definir y probar esa operación, se ofrece **conservar el archivo y revisar el conflicto**, sin prometer una salida destructiva que no existe.
5. **«Único efecto» se comprueba también en la identidad persistida.** Una fila fresca única puede compartir identidad de documento con registros anteriores. Se exige identidad de línea, o demostrar que el documento identifica una sola operación preservada, revisando sus vínculos y movimientos.
6. **El diagnóstico no convierte ausencia de datos en ausencia de problemas.** Sin trazabilidad de bloqueos se informa «sin medición», no cero. Pocos casos pueden justificar posponer la automatización, pero no cerrar una protección defectuosa.

Además:

- **Commits separados de B·3b y B·nuevo**: la categoría y la seguridad de relectura tienen que poder revisarse y revertirse por separado.
- **Orden aprobado**: primero el paso 0 (diagnóstico y pruebas sobre `main`); después se decide A/B/C con los resultados. Si se elige A, se registra como **«automatización descartada; limitación documentada»**, no como conservación automática resuelta.
- **El límite de 400 líneas** sirve para reconsiderar el diseño; no demuestra que sea seguro.
- **El undo existente se mantiene**, siempre que una prueba confirme que restaura también las huellas y vínculos afectados.
