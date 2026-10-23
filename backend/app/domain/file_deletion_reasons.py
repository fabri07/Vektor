"""Por qué una entidad NO se pudo revertir al borrar el archivo que la trajo.

Set cerrado y compartido por el preview, el resultado del ``DELETE`` y la UI. Es
lo que convierte "el archivo se borró" en una afirmación verificable: si algo
quedó vivo, el usuario ve **cuál** y **por qué**, en vez de un borrado que dice
haber limpiado todo.

Los motivos se acumulan: una misma entidad puede conservarse por más de uno.
"""

from __future__ import annotations

from enum import StrEnum


class PreservationReason(StrEnum):
    """Motivo por el que una entidad sobrevive al borrado de su archivo."""

    # ── La entidad tiene otra fuente viva, con procedencia demostrable ────────
    # Nunca por coincidencia de nombre: siempre por ledger, `source_upload_id` o
    # identidad canónica inequívoca.
    OTRO_ARCHIVO_ACTIVO = "otro_archivo_activo"
    VENTA_MANUAL_POSTERIOR = "venta_manual_posterior"
    COMPRA_POSTERIOR = "compra_posterior"
    MOVIMIENTO_POSTERIOR = "movimiento_posterior"
    REFERENCIA_DE_OTRA_ENTIDAD = "referencia_de_otra_entidad"
    DEPENDENCIAS_POSTERIORES = "dependencias_posteriores"

    # ── Alguien la tocó después del import ───────────────────────────────────
    EDICION_MANUAL_POSTERIOR = "edicion_manual_posterior"
    # Grano fino: el campo cambió después, así que ese campo NO se restaura
    # aunque el resto de la entidad sí. Viaja con la lista `fields`.
    CAMPO_MODIFICADO_POSTERIORMENTE = "campo_modificado_posteriormente"

    # ── No hay a qué volver ──────────────────────────────────────────────────
    # La entidad la CREÓ el archivo: no existe `before_json`, así que "restaurar"
    # no está definido. Se conserva la identidad y se marca para completar.
    ENTIDAD_CREADA_SIN_ESTADO_ANTERIOR = "entidad_creada_sin_estado_anterior"
    # Import anterior al ledger de reversa: no se puede saber qué creó el archivo.
    SIN_LEDGER = "sin_ledger"
    # Fila de "Otros" que el usuario clasificó a mano antes de que la
    # clasificación propagara la procedencia: el registro derivado no tiene
    # vínculo reconstruible con el archivo.
    OTRO_CLASIFICADO_HISTORICO_SIN_PROCEDENCIA = (
        "otro_clasificado_historico_sin_procedencia"
    )


# Texto en castellano para la UI. Vive acá y no en el frontend para que agregar un
# motivo sin su explicación sea imposible de pasar por alto.
REASON_LABELS: dict[PreservationReason, str] = {
    PreservationReason.OTRO_ARCHIVO_ACTIVO: "otro archivo activo también lo respalda",
    PreservationReason.VENTA_MANUAL_POSTERIOR: "tiene ventas posteriores",
    PreservationReason.COMPRA_POSTERIOR: "tiene compras posteriores",
    PreservationReason.MOVIMIENTO_POSTERIOR: "tiene movimientos de stock posteriores",
    PreservationReason.REFERENCIA_DE_OTRA_ENTIDAD: "otra ficha lo referencia",
    PreservationReason.DEPENDENCIAS_POSTERIORES: "tiene operaciones posteriores",
    PreservationReason.EDICION_MANUAL_POSTERIOR: "lo editaste a mano después de importarlo",
    PreservationReason.CAMPO_MODIFICADO_POSTERIORMENTE: (
        "cambiaste estos campos después de importarlo"
    ),
    PreservationReason.ENTIDAD_CREADA_SIN_ESTADO_ANTERIOR: (
        "lo creó este archivo y no hay un valor anterior al que volver"
    ),
    PreservationReason.SIN_LEDGER: (
        "se importó antes de que Véktor registrara qué creaba cada carga"
    ),
    PreservationReason.OTRO_CLASIFICADO_HISTORICO_SIN_PROCEDENCIA: (
        "salió de «Otros» y no quedó registrado de qué archivo venía"
    ),
}
