# Habilitar la importación en segundo plano (E6c-3)

Qué se habilita: que confirmar un archivo **registre** la importación y devuelva un
id consultable, en vez de sostener el request HTTP hasta que termine. Hoy ese
request puede durar hasta 16 minutos, y si se corta —la pestaña, el proxy, el
wifi— no hay adónde volver a preguntar.

`/confirm` **sigue existiendo y funcionando** durante toda la transición. La
compuerta decide quién usa la ruta nueva; nada obliga a cortar de una vez.

---

## Orden de habilitación

El orden importa y no es intercambiable.

### 1. Migraciones aplicadas

```
20260910_0004  import_attempts + import_outbox
20260910_0005  import_attempts.capabilities_json
```

Additive: dos tablas nuevas, ninguna columna existente tocada. Correr antes que
todo lo demás — si el código nuevo aterriza sin las tablas, cada registro falla.

Con Railway el `preDeployCommand` de `vektor-api` la aplica sola. Verificar:

```sql
SELECT count(*) FROM import_attempts;   -- tiene que existir, no tiene que fallar
```

### 2. Workers con el código nuevo

El ejecutor (`jobs.execute_import`), el publicador (`jobs.publish_import_orders`)
y el recuperador (`jobs.recover_import_attempts`) viven en `vektor-worker`.

**Railway redespliega los servicios en paralelo y sin orden garantizado**, así que
puede haber una ventana con la api nueva y el worker viejo. Está previsto: la api
registra intentos, sus órdenes quedan en `import_outbox`, y **nadie las pierde**.
En cuanto el worker nuevo aterriza, las drena. Hay un test que lo afirma
(`test_un_worker_viejo_no_pierde_la_orden`).

Lo que **no** hay que hacer en esa ventana es prender la compuerta: los usuarios
verían importaciones "en cola" que tardan lo que tarde el deploy.

### 3. Colas del worker

El ejecutor y el publicador viven en `ingestion`; el recuperador en `scores` —a
propósito: es el auditor de la cola de ingestión y si viviera en ella se trabaría
con lo que audita, que es el incidente que este programa vino a cerrar.

Verificar que el worker consume **las dos**:

```bash
railway variables --service vektor-worker | grep CELERY_QUEUES
```

El default de `scripts/start_worker.sh` (`default,scores,notifications,reports,ingestion`)
ya alcanza. Si alguien la sobrescribió y sacó `scores`, el recuperador no corre y
un intento cuyo ejecutor muera queda colgado para siempre.

### 4. Beat

Dos entradas nuevas:

| tarea | cada | para qué |
|---|---|---|
| `publish-import-orders` | 1 min | red de seguridad: entrega lo que quedó sin publicar |
| `recover-import-attempts` | 5 min | reencola los intentos cuyo ejecutor murió |

El camino normal publica apenas commitea el confirm, así que el beat es para lo
que se cayó en el medio. **Sin beat el sistema funciona pero pierde la
recuperación**: una orden que no se pudo entregar (broker caído) o un ejecutor
muerto se quedan ahí. Es la misma verificación pendiente de H16 / E5.

### 5. Recién ahí, la compuerta

```
ASYNC_IMPORT_ROLLOUT_TENANT_IDS=<uuid>,<uuid>
```

En **`vektor-api`** (es quien registra). Vacía por default = nadie habilitado.

---

## Apagar

```
ASYNC_IMPORT_ROLLOUT_TENANT_IDS=      # vaciar
```

La compuerta gatea **sólo el registro de intentos nuevos**. El publicador y el
recuperador siguen corriendo para todos, a propósito: un intento ya registrado
tiene su orden commiteada, y apagar el publicador la dejaría sin entregar — el
usuario vería una importación "pendiente" que nadie va a ejecutar nunca.

Frenar lo nuevo no puede significar abandonar lo que ya entró. Después de vaciar
la variable, esperar a que `import_attempts` no tenga nada en curso:

```sql
SELECT status, count(*) FROM import_attempts
WHERE created_at > now() - interval '1 day' GROUP BY status;
```

Con todo en `COMPLETADO`/`FALLADO` ya no queda nada corriendo por la ruta nueva.

---

## Qué mirar

```sql
-- Órdenes que no se pudieron entregar. Si `publish_attempts` crece sin que
-- `published_at` se llene, el problema es el broker, no los archivos.
SELECT id, attempt_id, publish_attempts, last_error
FROM import_outbox WHERE published_at IS NULL ORDER BY created_at;

-- Intentos colgados: EJECUTANDO con el lease vencido = el ejecutor murió.
-- El recuperador los toma cada 5 min; si se acumulan, no está corriendo.
SELECT id, tenant_id, attempts, lease_expires_at
FROM import_attempts
WHERE status = 'EJECUTANDO' AND lease_expires_at < now();

-- Por qué fallan. `error_code` es un set cerrado y se puede agrupar.
SELECT error_code, count(*) FROM import_attempts
WHERE status = 'FALLADO' GROUP BY error_code ORDER BY 2 DESC;

-- Intentos que se rechazaron porque una compuerta cambió mientras estaban en
-- vuelo. Un puñado justo después de girar una llave es lo esperado (ver abajo);
-- un goteo constante significa que la api y el worker NO tienen el mismo
-- entorno, y ahí el problema es la configuración, no los archivos.
SELECT tenant_id, capabilities_json, error_detail, created_at
FROM import_attempts
WHERE error_code = 'capacidades_cambiaron' ORDER BY created_at DESC;
```

---

## Girar otra compuerta de rollout con importaciones en vuelo

Las cuatro compuertas que cambian **qué se persiste** de un mismo archivo

| variable |
|---|
| `PURCHASE_COST_ROLLOUT_TENANT_IDS` |
| `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS` |
| `CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS` |
| `INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS` |

quedan **congeladas en cada intento** (`capabilities_json`) en el momento en que
el usuario confirma. Antes de escribir nada, el ejecutor compara contra las
vigentes; si cambiaron, **no importa y lo dice** (`capacidades_cambiaron`).

Existe porque la api y el worker son dos servicios con entornos propios que
Railway redespliega en paralelo: sin esto, alguien confirma viendo el preview con
el motor de costos de compra encendido y el worker escribe los costos con el
motor apagado. Números distintos de los que mostró la pantalla, sin un error a la
vista.

**La consecuencia operativa:** girar una de esas llaves para un tenant invalida
los intentos que ese tenant tenía encolados. Son segundos o minutos de trabajo, y
el usuario ve un mensaje que le dice que vuelva a confirmar — no un import a
medias. Si querés evitarle el reintento, esperá a que no quede nada corriendo:

```sql
SELECT count(*) FROM import_attempts
WHERE tenant_id = '<uuid>' AND status IN ('PENDIENTE','EJECUTANDO');
```

Un intento registrado **antes** de la migración `0005` tiene `capabilities_json`
en `NULL` y se ejecuta sin verificar: no hay con qué comparar, y romper los
intentos en vuelo durante el deploy es exactamente lo que esta ruta existe para
evitar.

El usuario no queda trabado: al llegar a un estado terminal el frontend olvida la
clave de petición, así que volver a confirmar registra un intento nuevo con las
capacidades vigentes. No hay que limpiar nada a mano.

---

## Lo que este cambio NO promete

**"Exactly once" de transporte.** Celery no lo da, y fingirlo sería peor que no
tenerlo. La misma orden se puede entregar dos veces —el publicador entrega primero
y marca después, porque al revés perdería órdenes— y lo que impide la doble
ejecución es el **lease con token del intento**, no el transporte.

**Que una compuerta se pueda girar sin consecuencias para lo que ya está
encolado.** Se puede girar cuando quieras, pero los intentos en vuelo de ese
tenant se rechazan y hay que volver a confirmarlos. Es deliberado: la alternativa
era importar bajo reglas que el usuario no vio.

**Atomicidad entre los efectos y el cierre del intento.** `confirm_file` cierra su
propia transacción. Si el proceso muere entre el commit de los efectos y el cierre
del intento, el intento queda `EJECUTANDO`, la recuperación lo reencola, y la
segunda corrida no duplica nada porque las huellas de fila hacen el import
idempotente. La ventana existe, se recupera sola, y el precio es una corrida extra
que no escribe nada. Está probado, no asumido.

**Que el frontend caiga a `/confirm` si el POST falla.** Sólo lo hace ante un
**404** —la compuerta apagada, donde el servidor respondió sin mirar el cuerpo—.
Ante un timeout o un 5xx **no cambia de ruta**: la petición pudo haberse
registrado, y disparar el confirm sincrónico encima importaría el archivo dos
veces. Reintenta con la misma clave, que el backend reconoce.
