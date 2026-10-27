"""Límites de tiempo del ejecutor de importaciones asíncronas (I3-2).

Viven acá, y no en el worker, por el mismo motivo que ``reread_limits``: el
invariante que los liga cruza módulos y su violación no da error, da dos
ejecuciones del mismo import a la vez.

    IMPORT_EXECUTE_SOFT_LIMIT_SECONDS  <  IMPORT_EXECUTE_HARD_LIMIT_SECONDS
                                       <  los dos leases (intento y archivo)

- Sin límites propios, ``jobs.execute_import`` heredaba los globales de Celery
  (240 s soft / 300 s hard). Un import grande no terminaba nunca: a los 300 s el
  pool mataba el proceso sin compensar nada, y el archivo quedaba ``IMPORTING``,
  el intento ``EJECUTANDO`` y la reserva de cupo tomada.
- **hard < lease** porque el lease es lo que usan ``liberar_huerfanos`` (intento)
  y el takeover de ``acquire_import_lease`` (archivo) para declarar muerto a un
  ejecutor. Un hard limit por encima del lease deja que la recuperación reencole
  —o que un ``/confirm`` tome el archivo— mientras el primero sigue escribiendo.
- **soft → hard** es la ventana para revertir, devolver el archivo a
  ``NEEDS_CONFIRMATION`` y cerrar el intento ``FALLADO`` con un mensaje. Son tres
  transacciones cortas: con 3 minutos sobra, y el hard kill (SIGKILL, sin Python
  posible) no las alcanza.

Los leases NO se subieron (decisión conservadora): el del archivo también gobierna
la ruta sincrónica y la relectura, y un lease más largo deja un archivo de un
proceso muerto trabado más tiempo. El costo es un tope de duración declarado: a la
medición documentada (unas 5.000 filas cada 25 s) entran unas 100.000 filas; un
archivo más largo falla con un mensaje que dice qué hacer, en vez de quedar
"importando" para siempre.
"""

from __future__ import annotations

from app.application.services.import_attempt_service import LEASE_TTL_SEGUNDOS
from app.application.services.ingestion_lease_service import (
    DEFAULT_IMPORT_LEASE_TTL_SECONDS,
)

#: Celery levanta ``SoftTimeLimitExceeded`` (una ``Exception``) a los N s.
IMPORT_EXECUTE_SOFT_LIMIT_SECONDS = 10 * 60

#: SIGKILL al proceso hijo. Debajo de los dos leases (15 min) con 2 min de margen.
IMPORT_EXECUTE_HARD_LIMIT_SECONDS = 13 * 60

assert IMPORT_EXECUTE_SOFT_LIMIT_SECONDS < IMPORT_EXECUTE_HARD_LIMIT_SECONDS
assert IMPORT_EXECUTE_HARD_LIMIT_SECONDS < LEASE_TTL_SEGUNDOS
assert IMPORT_EXECUTE_HARD_LIMIT_SECONDS < DEFAULT_IMPORT_LEASE_TTL_SECONDS
