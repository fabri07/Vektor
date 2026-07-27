"""Framework de versionado de lógica de ingestión.

`INGESTION_VERSION` marca qué versión del protocolo de interpretación de ingestión
se usó para procesar/confirmar un `UploadedFile` — permite cambios forward- e
backward-compatible en el pipeline sin perder trazabilidad de qué archivos pasaron
por qué lógica.

Historial:
- 1 = baseline pre-F8 (ningún archivo tenía columnas riesgosas evaluadas).
- 2 = F8 (protocolo de riesgo contextual de columnas, 2026-07-25/26).
  La próxima entrada (F9/F10) se agrega acá con su propio comentario.
"""

INGESTION_VERSION = 2
