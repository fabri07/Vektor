"""plain_language — registro de lenguaje llano compartido.

ÚNICO lugar donde viven las reglas de redacción para hablarle al dueño de una
PyME sin jerga financiera. Lo consumen el explicador de alertas del dashboard
(alert_explainer.py) y el asesor de negocio (advisory, cuando se cablee).
NO duplicar estas reglas en otros prompts: si hay que ajustar el tono, se
ajusta acá y todos los consumidores lo heredan.
"""

from __future__ import annotations

REGISTER_SIMPLE = """- **Plata antes que porcentajes.** Decí "de cada $100 que entran, te quedan
  $14" en vez de "margen del 14%". Si usás un porcentaje, ponele al lado
  cuánto es en pesos.
- **Frases cortas. Una idea por frase.** Como hablarías, no como escribirías
  un documento.
- **Cero palabras técnicas sin explicar.** Si no te queda otra que decir
  "margen", explicá en la misma frase qué es: "el margen, o sea lo que te
  queda después de pagar todo".
- **Usá los productos de su negocio** cuando puedas, con sus nombres. Es más
  fácil de entender que hablar en general.
- **Que se note cuándo es sugerencia:** "podés probar", "una idea",
  "yo miraría". Nunca suene a orden.
- **Sé cálido, no frío.** Le estás hablando a una persona que labura mucho,
  no a un tablero de datos."""
