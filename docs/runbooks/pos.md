# Runbook de la caja (POS)

Guía de uso y operación de la caja de Véktor (`/pos`). Sirve para instalar una
PC nueva, para el día a día del local y para cuando algo sale mal.

Este documento contiene sólo instrucciones que no cambian. Los resultados de
pruebas, los ids y las incidencias se registran en el acta del piloto:
[`docs/plans/b15-piloto-2026-09-29.md`](../plans/b15-piloto-2026-09-29.md).

---

## 1. Instalar una PC como caja

Se hace **en la PC que va a ser la caja**, no en una notebook de prueba. El
driver de la impresora, la versión de Chrome y el disco forman parte de lo que
tiene que funcionar.

### Lo que hace falta antes

- Windows con **Google Chrome** actualizado.
- La impresora térmica instalada con **su propio driver**, no con el genérico de
  Windows, y configurada como **impresora predeterminada**. `--kiosk-printing`
  imprime siempre en la predeterminada y no muestra el diálogo, así que esa PC
  deja de imprimir cómodamente en otra impresora.
- El lector de códigos enchufado.
- El usuario **dueño** del negocio, con su PIN.

### Pasos

1. **Habilitar la PC.** Con el dueño logueado, ir a **Ajustes → Seguridad →
   Cajas habilitadas → «Habilitar esta PC»** y ponerle un nombre, por ejemplo
   «Mostrador». Pide el PIN.
   - La habilitación identifica **a la PC, no a la persona**. Queda guardada en
     el navegador y cerrar la sesión no la borra.
2. **Abrir la caja** en `/pos`.
3. **Preparar la PC para abrir sin internet.** En la barra de la caja, marcar
   **«Preparar esta PC para abrir sin internet»** y esperar a que diga que la PC
   quedó preparada.
   - Hasta que no lo diga, la PC **no** está lista. Registrar el service worker
     no alcanza: tienen que quedar guardados todos los archivos de la pantalla.
   - Sólo lo puede hacer el dueño. Desmarcarla borra lo guardado en el momento.
4. **Instalar la app.** En Chrome, menú ⋮ → «Instalar Véktor» (o «Guardar y
   compartir → Instalar»). Queda un ícono en el escritorio.
5. **Imprimir sin diálogo.** Clic derecho en el ícono → Propiedades → campo
   **Destino**. Al final de lo que ya dice (algo como
   `chrome_proxy.exe --profile-directory=Default --app-id=…`), agregar un
   espacio y **`--kiosk-printing`**. Aceptar.
6. **Configurar la impresora** desde el menú «Impresora» de la caja: papel
   (80 o 58 mm), formato de página y «Imprimir al cobrar». Después, **«Imprimir
   prueba»**. Los valores para cada modelo están en la [sección 9](#9-valores-medidos-por-equipo).
7. **Configurar el lector** desde el menú «Lector», con el **terminador del
   lector** (Enter o Tab) que manda tu modelo. Para saberlo, se escanea un
   código en el Bloc de notas: si el cursor baja de renglón es Enter, si salta
   como con el tabulador es Tab.
8. **Comprobar el arranque sin red una vez**:
   1. Cortar internet de verdad (desenchufar el cable, apagar el Wi-Fi o el
      router). El modo «Offline» de las herramientas de Chrome **no sirve**
      para esta prueba.
   2. Reiniciar Windows con **Inicio → Reiniciar**. No con «Apagar»: en
      Windows 10/11, con el inicio rápido activado, «Apagar» es en realidad una
      hibernación.
   3. Abrir la caja **desde el ícono**. Tiene que abrir y dejar cobrar.

---

## 2. El día: abrir y cerrar el turno

- **Abrir el turno.** Antes de cobrar la caja pide el **fondo inicial**, que es
  lo que hay en el cajón al empezar.
  - $0 es un fondo válido, pero hay que escribirlo.
  - Se puede abrir sin internet: el turno se avisa solo cuando vuelve la
    conexión.
  - Cada cajero tiene su turno en cada caja. Si entra otro cajero en la misma
    PC, abre el suyo.
- **Durante el turno.** La barra muestra la hora de apertura y el fondo.
  - Si dice que la apertura no está sincronizada, falta internet. No hace falta
    hacer nada: se sincroniza sola.
- **Cerrar el turno** (botón «Cerrar turno», **requiere internet**):
  1. La caja primero sube las ventas que tenga pendientes. Si alguna no puede
     subir, lo avisa. Esas ventas llegan después como **tardías** y no entran
     en este arqueo.
  2. Se cuenta el efectivo del cajón y se escribe en «Efectivo contado en el
     cajón». **La caja no muestra cuánto debería haber**, a propósito: el
     arqueo es ciego.
  3. Después de firmar se ven el fondo, lo esperado, lo contado y la
     diferencia (sobrante o faltante).
  - **Un arqueo firmado no cambia nunca.** Una venta que llega después aparece
    como tardía en el panel del encargado.

### Panel «Turnos de caja» (sólo dueño o encargado)

Desde la barra de la caja se entra a **«Turnos de caja»**. Ahí se ve:

- **Los turnos abiertos** de todas las cajas.
  - **«Cerrar sin arqueo»** sirve para turnos que nadie va a contar. Pide PIN y
    un motivo.
  - **«Elegir los viejos sin fondo declarado»** selecciona los turnos
    abandonados.
  - Si cerrás un turno que alguien está usando, esa caja se entera y le pide
    abrir otro.
- **Los cerrados recientes**, con su diferencia. En «Ver» aparecen las
  **ventas que llegaron después del cierre** con el efectivo que el arqueo no
  vio: eso es lo que hay que conciliar.

---

## 3. Cobrar

- **Escanear o buscar** el producto.
  - Si un código responde a más de un producto, la caja **pide elegir**: nunca
    elige sola.
  - Un código desconocido puede **vincularse** a un producto. Lo hace quien
    tiene el permiso «aprender códigos», y hace falta internet.
- **Descuentos**, por línea o a la venta, si el usuario tiene el permiso. El
  total cobrado siempre cierra al centavo.
- **Pago mixto.** Con **«Dividir pago»** se reparte el cobro, por ejemplo
  efectivo más débito.
  - Si el reparto no suma el total, la caja lo dice («Falta asignar» o
    «Sobra») y no deja cobrar.
  - El vuelto se calcula sólo sobre la parte en efectivo.
- **Fiado.** Necesita un cliente real y el permiso «fiado». No funciona sin
  internet.
- **Reimprimir.** Desde **«Últimos tickets» → «Reimprimir»**. Reimprimir no
  crea nada nuevo.
- **Anular un ticket.** Desde «Últimos tickets».
  - Se anula el ticket entero: líneas, pagos y stock.
  - Pide un motivo. El cajero sólo puede anular sus propios tickets, con su PIN
    y con el permiso correspondiente.
  - No existe la devolución parcial.

---

## 4. Sin internet

- La caja **sigue cobrando**. Cada venta se guarda en la PC **antes** de
  mandarse, y sale un ticket rotulado **«PENDIENTE DE SINCRONIZAR»**.
  - El ticket definitivo tiene otro número y se puede reimprimir cuando la
    venta sube.
- Cuando vuelve la conexión, las ventas suben solas y **una sola vez**.
  Reenviar una venta nunca la duplica.
- Cada venta pendiente sube con la sesión **del cajero que la cobró**. Si
  cambia el usuario, las ventas del anterior esperan a que ese cajero vuelva a
  entrar.
- **«Stock estimado — última sinc. HH:MM»** es el stock que tenía el servidor
  en la última sincronización, menos lo vendido desde entonces en esta PC.
  - Es una **estimación**: no ve lo que vendió otra caja.
  - Nunca frena una venta.
- Sin internet **no funcionan**: fiado, aprender códigos, cerrar el turno,
  anular.

### Panel «Ventas sin sincronizar»

- Muestra cada venta pendiente con su motivo. Tiene **«Reintentar»**,
  **«Descartar»** (pide confirmación) y **«Exportar a un archivo»**.
- **Si una venta no sube, exportala antes de hacer cualquier otra cosa.** Es la
  única copia de esa venta.
- Si el panel avisa que el navegador **no garantiza conservar los datos**
  (persistencia no concedida), exportar deja de ser el último recurso y pasa a
  ser rutina: exportá los pendientes al final de cada día sin conexión.
- Las ventas **«sin completar (en corrección)»** son cobros que el servidor
  rechazó y el cajero todavía no rehizo. Puede haber plata de esos cobros en
  el cajón: se revisan con el encargado.

---

## 5. Diferencias de stock

Una venta cobrada sin internet puede llegar cuando el stock ya no alcanza.
**La venta entra igual**: el cliente ya se llevó la mercadería. Queda una
diferencia para resolver en **Productos → panel de diferencias** (dueño o
encargado, con PIN y motivo obligatorio).

- La diferencia muestra la **salida pendiente completa**. Si había 3 y se
  vendieron 5, la salida pendiente es de 5, no de 2.
- **«La mercadería estaba»**: el sistema no la tenía cargada. Entra lo que
  falta como ajuste, fechado el día de la venta, y después sale la venta
  completa.
- **«La venta está mal»**: se anula el ticket entero.
- No existe la opción de «ocultar el aviso»: ocultarlo no arregla el stock.

---

## 6. Cajeros y permisos

- El alta se hace en **Ajustes → Seguridad → Equipo**, con el rol **Cajero**.
  - El cajero **ocupa una plaza** del plan.
  - Sólo ve la caja. No ve costos, márgenes ni reportes.
- Hay cuatro permisos, y cada uno se marca aparte:
  - **descuentos**;
  - **anular tickets** (sólo los propios, con su PIN);
  - **fiado**;
  - **aprender códigos**.
- Un cajero **sólo cobra desde una PC habilitada**.

---

## 7. Cuando algo sale mal

| Qué pasa | Qué hacer |
|---|---|
| «Esta caja fue dada de baja» | Una caja dada de baja **no se reactiva**: el dueño habilita la PC de nuevo en Ajustes → Seguridad, y eso crea una caja **nueva**. Las ventas pendientes **no se pierden**, pero quedan atadas a la caja anterior y **no suben solas**: ver «Reemplazar una PC» abajo. |
| Se venció la sesión del cajero con ventas pendientes | Volver a entrar **con el mismo usuario**. Las pendientes suben con su sesión. Si ese cajero no puede volver, exportar las pendientes. |
| El ticket no sale | La venta está cobrada igual. Revisar la impresora y usar «Reimprimir». |
| Un cobro quedó sin respuesta del servidor | No volver a cobrar. La venta queda en la cola y, cuando sube, el servidor devuelve la original sin duplicarla. |
| El turno se cerró solo | Lo cerró el encargado desde el panel. Abrir uno nuevo. Lo cobrado sigue registrado. |
| La suscripción está en solo lectura | La caja **sigue cobrando**. Lo que se pausa es el análisis (score, insights, proyecciones, balance). Los datos se siguen viendo y corrigiendo. |
| Se cortó internet a mitad de un cobro | La venta queda guardada en la PC y sube sola. Si se cerró el navegador, al volver a abrir la caja la venta sigue ahí. |

### Reemplazar una PC (o rehabilitarla) con ventas sin subir

1. El dueño da de baja la caja vieja (si no estaba dada de baja) y habilita
   esta PC en **Ajustes → Seguridad → Cajas habilitadas**. Si en la PC quedaron
   ventas de la caja anterior, el aviso lo dice en el momento.
2. En la caja, abrir **«Ventas sin sincronizar»**: arriba aparece «N ventas se
   cobraron en la caja anterior». El dueño aprieta **«Pasarlas a esta caja»** y
   pone su PIN.
3. Recién entonces suben, **una sola vez**, con la caja nueva. Queda registrado
   quién las pasó (`POS_PENDING_REASSIGNED`).

- Sólo el **dueño** puede hacerlo, y **desde la PC que las recibe**. Un cajero
  ve «Pedile al dueño que las pase a esta caja».
- La caja anterior tiene que estar **dada de baja**: a una caja que sigue
  habilitada no se le quitan sus ventas.
- Las ventas siguen siendo del cajero que las cobró: suben cuando entra él.

---

## 8. Lo que la caja todavía no hace

- **Vender por peso.** Todavía no se puede cargar un producto en gramos: la
  cantidad se vende por unidad.
- **Aprender códigos sin internet.** Vincular un código nuevo necesita conexión.
- **Numeración correlativa y datos fiscales en el ticket.** El comprobante es
  **no fiscal**.
- **Confirmar que salió el papel.** El navegador no lo sabe.

---

## 9. Valores medidos por equipo

Se completa con la prueba en cada PC (el paso B1 del programa).

| Equipo | Valor |
|---|---|
| Chrome / Windows | *(versión)* |
| Arranque en frío sin red | *(segundos desde el doble clic hasta poder escanear)* |
| Persistencia de almacenamiento | *(`navigator.storage.persisted()`: sí / no)* |
| Impresora (modelo / driver) | *(…)* |
| Papel del driver | *(rollo / 80×297)* |
| Formato de página (`@page`) | *(A / B / C)* |
| Corte de papel | *(sí / no; si no, margen inferior)* |
| Cajón | *(abre con la impresión: sí / no / no hay)* |
| Lector (modelo) | *(…)* |
| Terminador | *(Enter / Tab)* |
| Intervalo entre teclas | *(ms)* |
| Lee QR / etiqueta impresa | *(sí / no)* |
