"""Límites de tiempo del apply de relectura — una sola fuente para tres números
que TIENEN que moverse juntos.

Viven acá, y no en el worker ni en el servicio, porque el invariante que los liga
cruza los dos módulos y su violación no da error: da dos applies concurrentes sobre
el mismo tenant.

    REREAD_APPLY_SOFT_LIMIT_SECONDS  <  REREAD_APPLY_HARD_LIMIT_SECONDS
                                     <  REREAD_STALE_AFTER_SECONDS

- **soft → hard** es la ventana que tiene el worker para hacer rollback de una
  transacción grande y marcar el run como ``FAILED`` cuando salta
  ``SoftTimeLimitExceeded``. Si es corta, el hard kill llega en el medio (SIGKILL,
  sin excepción posible en Python) y el run queda **zombie en APPLYING para
  siempre**. Eso es exactamente lo que pasó con Asteria: 3 runs muertos (14/8, 18/8
  y 1/9) con los 30 s que separaban 270 de 300.
- **hard < stale** porque ``_STALE_RUNNING_AFTER_SECONDS`` es lo que usa el guard de
  ``start_background_apply`` para decidir que un run "colgado" ya no cuenta y dejar
  arrancar otro. Si el umbral fuera MENOR que el límite duro, un apply que todavía
  está corriendo legítimamente sería declarado muerto y un segundo apply arrancaría
  encima. Subir el límite sin subir el umbral convierte un timeout en una carrera.

Los valores salen de una medición real, no de una corazonada
(``scripts/bench_reread_apply.py`` sobre el Excel de Asteria, 6.103 filas / 2.563
registros a reemplazar): **4.556 statements** en el apply. Contra Neon, a 30-50 ms
de ida y vuelta por statement, son ~137-228 s de pura latencia, más el commit. Los
300 s de límite duro que había caían dentro de ese rango — por eso a veces parecía
avanzar y siempre moría.

El margen es ~4× sobre el peor caso medido: cubre un archivo bastante más grande que
el de Asteria sin volver a tocar esto. No es gratis (un apply retiene un slot de
worker y una conexión mientras corre), pero el guard anti-duplicado ya impide dos
relecturas simultáneas por tenant, así que el costo está acotado — y la alternativa
medida es no terminar nunca.
"""

from __future__ import annotations

#: Celery levanta ``SoftTimeLimitExceeded`` (una ``Exception`` normal) a los N s.
REREAD_APPLY_SOFT_LIMIT_SECONDS = 25 * 60

#: SIGKILL al proceso hijo. Entre soft y hard hay 5 minutos para revertir y marcar
#: ``FAILED``: un rollback de ~5.000 filas contra Neon no entra en 30 s.
REREAD_APPLY_HARD_LIMIT_SECONDS = 30 * 60

#: A partir de acá un run en QUEUED/APPLYING se considera abandonado. Estrictamente
#: mayor que el límite duro: un apply vivo nunca debe parecer un zombie.
REREAD_STALE_AFTER_SECONDS = 40 * 60

assert REREAD_APPLY_SOFT_LIMIT_SECONDS < REREAD_APPLY_HARD_LIMIT_SECONDS
assert REREAD_APPLY_HARD_LIMIT_SECONDS < REREAD_STALE_AFTER_SECONDS
