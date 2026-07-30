# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

El **dueño de una PYME argentina de reventa** — kiosco/almacén, artículos de limpieza,
decoración del hogar, librería y papelería, indumentaria, verdulería y frutería. No es
contador ni analista: sabe cuánto vendió hoy, no cuánto gana.

Lo usa en **dos situaciones distintas, y las dos importan**:

- **En el mostrador, entre cliente y cliente.** Celular en la mano, treinta segundos,
  ruido alrededor. Necesita cargar algo o mirar un dato de un vistazo.
- **Al cierre, sentado.** Pantalla grande, cabeza puesta en el negocio, sesión larga.
  Quiere entender cómo le fue y qué hacer.

Toda pantalla tiene que funcionar en las dos. Una que solo sirve al cierre queda sin
usar durante el día; una que solo sirve en el mostrador no deja pensar.

**Cuentas de equipo:** `OWNER`, `ADMIN`, `VIEWER`. Un empleado puede cargar datos sin
poder modificar ni borrar: el OWNER habilita esa capacidad por sub-cuenta y cada una
tiene su propio PIN.

## Product Purpose

Que el dueño sepa **cómo está su negocio y qué hacer al respecto**, a partir de los
datos desordenados que ya tiene: planillas, remitos, un libro diario, lo que se acuerda.

Éxito es que deje de decidir por intuición en las cuatro cosas que lo hunden: la caja,
el margen, el stock parado y la concentración de proveedores. No es que use Véktor todos
los días — es que la próxima decisión de reposición o de precio la tome mirando un
número real.

## Positioning

**Los números no los produce un modelo de lenguaje.** Toda la aritmética financiera es
determinística (`FactsService`, `DeterministicFinance`, `shared/analytics`,
`stats_engine`); el LLM narra lo que otro calculó y no tiene forma de calcular por su
cuenta. No es una política de prompt: es dónde vive el código.

De ahí salen dos consecuencias que un producto vecino no puede copiar sin rehacer su
arquitectura:

- **Sin datos suficientes no hay diagnóstico.** Con confianza baja, Véktor muestra un
  estado vacío y pide los datos que faltan, en vez de producir un número plausible. Un
  score ausente nunca se rellena con un valor neutro.
- **La vara declara su procedencia.** Los umbrales de cada rubro salen de fuentes
  sectoriales declaradas y la confianza del diagnóstico es la del eslabón más débil:
  datos del negocio y fundamento del umbral. Un umbral sin fuente baja la confianza del
  score aunque los datos estén impecables.

## Operating Context

- Los datos llegan **como están**: planillas Excel/CSV con columnas inventadas, libros
  diarios de doble encabezado, catálogos de productos, remitos en papel o foto, listas
  de precios de proveedor. La ingestión los interpreta por contenido —el nombre del
  archivo orienta, no determina— y **toda importación pasa por confirmación humana**.
- **Plata argentina real:** efectivo, fiado (cuenta corriente), transferencia, QR,
  tarjeta que entra a los ~30 días. Compra al mercado concentrador con flete. Precios
  que se mueven con la inflación.
- **Régimen fiscal** (monotributo / responsable inscripto / informal) es informativo:
  afina heurísticas y guía el arqueo, nunca bloquea.
- **Cobranza por WhatsApp** vía link `wa.me` (click-to-chat), no por API.
- Integraciones Google (Gmail, Calendar, Sheets, Docs) vía MCP, detrás de flag.
- **El registro es cerrado.** Se pide acceso por formulario público y un humano aprueba;
  no hay autoservicio. El demo público está deshabilitado.

## Capabilities and Constraints

**Hace:** score de salud financiera por rubro; chat multiagente en español rioplatense
sobre el negocio; ingestión de archivos con mapeo de columnas y confirmación; caja y
cierre con arqueo; stock con descuento en venta y prohibición de stock negativo;
clientes y proveedores con ficha fiscal argentina; gastos con COGS/OPEX; resumen
económico; pronóstico de caja.

**No hace, y es una decisión de producto, no una carencia:** contabilidad formal,
facturación fiscal, liquidación de impuestos, ERP, nómina, CRM de pipeline, asesoramiento
legal o financiero profesional. Antes de sumar una feature: ¿es análisis y optimización o
gestión operativa total? ¿la convierte en ERP/CRM/contabilidad? ¿introduce obligación
legal directa? ¿multiplica el soporte humano? Si alguna da que sí, no se construye.

**El chat es del negocio, no de propósito general.** Un mensaje fuera de tema se rechaza
con texto fijo, sin gastar tokens.

**Rubros: seis, cerrados en código.** `Vertical` es un enum cerrado; los CHECK de base lo
sostienen. Los seis son de **reventa** (comprar un producto y venderlo). Los que
transforman materia prima —carnicería, pollería, panadería, rotisería— quedan afuera a
propósito: el motor asume `compra → producto → venta` y no tiene concepto de rendimiento
ni de receta. Un kilo de media res no es un producto, son doce cortes con precios
distintos.

**Un rubro sin calibrar no se rechaza: se encola.** Es el diseño, no una limitación.
Cualquier PYME puede pedir acceso; el formulario tiene `otros`, y cuando lo elige
**`vertical_other_text` es obligatorio** (CHECK `ck_access_requests_vertical_other_text`),
así que el rubro real queda escrito. Esa solicitud queda en `PENDING` o pasa a
**`WAITLIST`**, que es un estado deliberadamente **no terminal**: se puede aprobar más
adelante. Cuando ese rubro se calibra —o cuando el dueño ve que encaja en uno existente—
se aprueba asignándole el vertical, y `otros` siempre termina corregido en ese paso.

La consecuencia para el producto es que **la cola de solicitudes es la fuente de demanda
que decide qué rubro se calibra próximo**, y por eso el texto libre del rubro es dato de
producto, no un campo de formulario más.

Por eso el carrusel de la landing muestra rubros fuera de los seis (peluquería, gimnasio,
taller mecánico): invita a pedir acceso a cualquier PYME, que es lo correcto. Lo que no
se puede prometer es heurística calibrada para un rubro que no la tiene — de ahí que
`/rubros` hable de los seis que Véktor "entiende hoy".

**La merma no está implementada.** Es lo primero que pide una verdulería y necesita un
motor de rendimiento que no existe. No se promete.

## Brand Commitments

- Nombre **Véktor**. Producto y comunicación en **español rioplatense**, de vos.
- Voz directa y concreta, sin promesas de resultado. Véktor estima y explica; no
  garantiza rentabilidad ni promete evitar una quiebra.
- **Hecho en Argentina**, para el contexto argentino.
- **No se promete lo que no está implementado.** Es el compromiso más operativo de todos:
  antes de escribir una capacidad en una página pública hay que poder señalar el campo o
  el cálculo que la sostiene.

## Evidence on Hand

- **Hay negocios reales operando en producción, y no hay permiso para nombrarlos.** Por
  lo tanto: **prohibido** testimonios, casos de éxito, logos de clientes, cantidad de
  usuarios o cualquier prueba social que identifique a alguien. No hay una versión
  "genérica" aceptable de esto — si no se puede nombrar, no se usa.
- Capturas reales de la aplicación en `frontend/public/screenshots/`.
- Doodles line-art originales en `frontend/public/doodles/` (fuente de verdad en Figma).
- Fuentes sectoriales de los umbrales, declaradas por bloque en
  `backend/app/application/data/heuristics/*.json` (CAME, INDEC, CACE, CAPLA/CAL, CIAI,
  Mercado Central). Son citables porque son públicas y están declaradas con año de
  revisión. **No** hay benchmarks propios publicables: la muestra cross-tenant cuenta
  eventos de recálculo, no negocios distintos, y por eso no puntúa ni se publica.

## Product Principles

1. **Los números no los inventa un modelo.** Toda aritmética por un servicio
   determinístico. El LLM narra; nunca calcula.
2. **Sin datos, no hay diagnóstico.** Estado vacío pidiendo lo que falta, antes que un
   número plausible. Nunca rellenar un score ausente con un valor neutro.
3. **Lo que se afirma tiene procedencia.** Un umbral sin fuente declarada vale menos, y
   el sistema lo dice en vez de disimularlo.
4. **Lo irreversible lo confirma una persona.** Importaciones, acciones de riesgo,
   borrados con historial: fail-closed, con paso humano.
5. **Cada rubro se mide con su propia vara.** Un margen sano de kiosco es una alarma en
   decoración. Un umbral genérico marca en rojo a negocios sanos.

## Accessibility & Inclusion

No hay un estándar formal comprometido. Lo establecido hasta hoy:

- El embudo público (solicitud de acceso, onboarding) se revisó con lector de pantalla.
  Queda **un hallazgo de navegación por teclado diferido a propósito**, no resuelto.
- Los componentes públicos respetan `prefers-reduced-motion`.
- Los desplegables de navegación son *disclosures*, no `role="menu"`: no se promete
  semántica de menú que no está implementada.
