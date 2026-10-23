"""Framework de versionado de lógica de ingestión.

`INGESTION_VERSION` marca qué versión del protocolo de interpretación de ingestión
se usó para procesar/confirmar un `UploadedFile` — permite cambios forward- e
backward-compatible en el pipeline sin perder trazabilidad de qué archivos pasaron
por qué lógica.

Historial:
- 1 = baseline pre-F8 (ningún archivo tenía columnas riesgosas evaluadas).
- 2 = F8 (protocolo de riesgo contextual de columnas, 2026-07-25/26).
- 3 = ledger de reversa (2026-07-31). El confirm registra qué productos CREÓ el
  import (`file_deletion_service.record_import_ledger`), que es lo único que
  permite deshacerlos al borrar el archivo: `products` no tiene columna de
  origen. Un archivo con versión < 3 se importó SIN ese registro, así que sus
  productos no son rastreables y el borrado los deja vivos avisando, en vez de
  adivinar cuáles creó (desactivar uno preexistente sería peor).
  La próxima entrada se agrega acá con su propio comentario.
"""

INGESTION_VERSION = 3

# Primera versión que dejó ledger de productos creados. Leerlo es lo que
# distingue "este archivo no creó productos" de "no sabemos qué creó".
INGESTION_VERSION_WITH_LEDGER = 3
