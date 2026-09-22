# B1 — Spike: arranque sin red y medición contra el hardware

**El resultado de B1 son cinco respuestas escritas, no código.** Todo lo que se
construya para medir es descartable y se tira al cerrar el bloque.

Va **antes** de construir la interfaz de caja (B7, B12, B13) porque esos tres
bloques hoy se apoyan en supuestos que nadie verificó: que Chrome arranca sin
red, que no evacúa IndexedDB, que el `@page` de 80 mm no produce una hoja en
blanco, y que el detector de ráfaga del lector se dispara a los ~30 ms.

---

## Prerrequisito — el andamio (no es del usuario)

No existe nada de esto todavía: `frontend/public/` sólo tiene `doodles` y
`screenshots`, no hay `manifest.json`, ni `sw.js`, ni ruta `/pos`, ni CSS de
impresión.

El andamio mínimo para que las mediciones sean válidas:

- `/pos-spike` estática (HTML a mano, sin RSC — el bypass de RSC es de B7).
- `manifest.json` + `sw.js` escrito a mano (cache-first de sus propios
  recursos). Sin librerías de PWA: acá se mide el navegador, no un wrapper.
- Un botón "guardar venta falsa" que escribe en IndexedDB y muestra el id.
- Un ticket de prueba con **tres variantes de `@page`** en la misma página.
- Un log de `keydown` con timestamp por tecla.

**Desplegado a un preview de Vercel (HTTPS real).** `localhost` no sirve: el
service worker anda ahí por excepción de origen seguro, y un servidor local
encendido hace que "sin red" no signifique nada.

---

## Paso 1 — Arranque en frío, sin red, después de reiniciar

En **la PC que va a ser la caja**, no en una notebook de desarrollo: el driver,
la versión de Chrome y el disco son parte de lo que se mide.

1. Abrir el preview **una vez con internet**. Instalar como app
   (Chrome → ⋮ → Instalar). Esperar a que la página diga `service worker: activated`.
2. Apretar "guardar venta falsa". **Anotar el id que muestra.**
3. Cortar la red **de verdad**: apagar el router o deshabilitar el adaptador en
   Windows. El checkbox "Offline" de devtools **no vale** — sólo hace fallar los
   fetch de la página; no prueba que Chrome recupere del disco el registro del
   service worker tras reiniciar el proceso, ni cómo arranca sin DNS.
4. **Reiniciar** (Inicio → Reiniciar). No suspender ni hibernar: la hibernación
   restaura el proceso en memoria y no probaría nada.
   ⚠️ En Windows 10/11 "Apagar" con **inicio rápido** activado es una hibernación
   del kernel. Usar **Reiniciar**, que sí es arranque limpio, o desactivar el
   inicio rápido en Opciones de energía.
5. Encender, **seguir sin red**, abrir la app **desde su ícono** — no restaurando
   pestañas, que es otro camino.

| | |
|---|---|
| **Pasa** | Abre la pantalla y el id del paso 2 sigue ahí |
| **Falla** | Dinosaurio, pantalla en blanco, o abre con IndexedDB vacío |

**Cronometrar**: del doble clic hasta poder escanear. Ese número decide si un
cajero lo tolera a las 8 de la mañana.

**Qué cambia:** si no arranca, B7 no es "persistencia offline" sino un problema
de plataforma y hay que reconsiderar el enfoque entero. Si arranca pero perdió
IndexedDB, el paso 2 pasa a ser obligatorio.

---

## Paso 2 — ¿Chrome concedió persistencia?

En la consola, **después** del reinicio:

```js
await navigator.storage.persisted()   // se espera true
await navigator.storage.estimate()    // cuota y uso
```

Chrome lo concede por heurística: sitio instalado, marcado como favorito,
engagement alto, permiso de notificaciones. Si da `false` **aun estando
instalada**, hay que saberlo ahora: significa que el navegador puede evacuar
ventas ya cobradas bajo presión de disco, y eso convierte la exportación de
pendientes en un camino de primera, no en el último recurso que hoy dice el plan.

---

## Paso 3 — El `@page` contra el driver real

Con la térmica instalada con **su** driver de Windows (no el genérico).

Imprimir la página de prueba, que trae las tres variantes juntas. Por cada una
anotar:

- ¿el texto entra a lo ancho o se corta?
- **¿sale una segunda hoja en blanco?** — es el síntoma clásico de una altura
  fija impuesta sobre papel continuo.
- ¿el margen izquierdo queda corrido?

Repetir con el **tamaño de papel del driver** en "rollo / continuo" y en
"80 × 297". Son cuatro combinaciones, no una.

Recordatorio: `size: 80mm auto` **no es sintaxis válida** — CSS Paged Media no
permite mezclar un `<length>` con `auto`. Por eso hay que medir cuál sí anda.

El valor ganador va a **configuración**, nunca clavado en el CSS.

---

## Paso 4 — Lo que `window.print()` no controla

Tres cosas que hay que saber antes de diseñar el ticket, no después:

- **¿Corta el papel?** El corte lo emite el driver al cerrar el trabajo. Si no
  corta, el cajero corta a mano — aceptable, pero entonces el ticket necesita
  margen inferior para que el corte no se lleve la última línea.
- **¿Abre el cajón?** Activar "abrir cajón al imprimir" en las propiedades del
  driver y confirmar que el pulso sale con un `window.print()` **del navegador**,
  no sólo con la página de prueba del driver.
- **`--kiosk-printing` imprime a la impresora predeterminada sin diálogo.** O sea
  que la térmica tiene que ser la predeterminada de Windows, y esa PC deja de
  poder imprimir cómodo a otra cosa. Confirmar que eso es aceptable en el local.

---

## Paso 5 — El lector (barato de medir ahora)

Enchufar el 2D imager. Primero en el Bloc de notas:

- ¿Termina en **Enter**, en **Tab**, o en nada? ¿Manda un `\r` de más?
- ¿**Lee un QR**? Si sólo lee las barras es un 1D láser y no sirve.
- ¿Lee una etiqueta impresa en papel común, no sólo la de fábrica?

Después en la página del spike, que loguea cada tecla con su timestamp:

- **El intervalo entre teclas, en milisegundos.** Ese número parametriza el
  detector de ráfaga de B13; hoy el plan dice "~30 ms" y eso es una suposición,
  no una medición.

---

## Lo que NO prueba nada

- El checkbox "Offline" de devtools.
- `localhost` (origen seguro por excepción + servidor local vivo).
- Ventana de incógnito (no persiste nada, por diseño).
- Suspender o hibernar en vez de reiniciar.
- Una notebook de desarrollo en vez de la PC que va a ser la caja.

---

## La salida de B1

1. ¿Arrancó sin red? + segundos del arranque en frío.
2. `persisted()` después del reinicio: sí / no.
3. El `@page` que funciona + el tamaño de papel del driver.
4. ¿Corta? ¿Abre el cajón? ¿La térmica puede ser la predeterminada?
5. Sufijo del lector + milisegundos entre teclas.

Con esas cinco, B7 y B12 dejan de apoyarse en supuestos.
