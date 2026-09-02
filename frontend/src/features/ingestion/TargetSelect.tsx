"use client";

import type { ReactNode } from "react";
import type {
  CrossFieldCatalogEntry,
  FieldCatalogEntry,
} from "@/services/ingestion.service";

/**
 * Cómo se ofrece un target que NO figura en el catálogo de la entidad.
 *
 * Sin una `<option>` propia el DOM cae a la primera («Sin mapear») y la pantalla
 * muestra algo distinto de lo que se va a enviar — el incidente ASTERIA.
 *
 * Los tres lugares que renderizan este `<select>` resolvían esto de tres formas
 * distintas, así que cada valor reproduce EXACTAMENTE lo que hacía su lugar de
 * origen. Unificarlos cambia comportamiento y no es parte de este refactor.
 */
export type UnknownTargetMode =
  /**
   * Campo personalizado SIEMPRE; «(campo desconocido)» sólo con el catálogo ya
   * cargado (multi-hoja).
   */
  | "custom-always"
  /**
   * Una sola opción para ambos casos, y las dos requieren catálogo cargado
   * (lista «Columnas a revisar» del camino plano).
   */
  | "catalog-guarded"
  /**
   * Sólo campo personalizado: un canónico fuera del catálogo se queda sin
   * opción propia (tabla única del camino plano).
   */
  | "custom-only";

/**
 * F-B.0 — el `<select>` de campo destino de una columna.
 *
 * Vive en UN solo lugar porque `ColumnMapperPanel` lo dibujaba tres veces con
 * el mismo JSX copiado. Las opciones del catálogo las sirve el backend
 * (`GET /ingestion/field-catalog`): acá NO hay una lista propia, que es lo que
 * divergió en su momento.
 */
export function TargetSelect({
  target,
  onChange,
  fields,
  crossFields = [],
  className,
  disabled,
  editingCustom = false,
  ignoreLabel = "— Ignorar —",
  showIgnoreOption = true,
  placeholderLabel = "Sin mapear",
  showCustomFieldOption = false,
  unknownTarget = "custom-always",
  dataSheet,
  dataSuggests,
}: {
  /** Target mapeado hoy: `""`, `"ignore"`, un canónico o `custom_field:{key}`. */
  target: string;
  /** Recibe el valor crudo del `<select>`, incluido el centinela `__custom__`. */
  onChange: (value: string) => void;
  fields: FieldCatalogEntry[];
  /**
   * Destinos en OTRA sección (hoy: «Tienda» → `supplier:name`). Se renderizan
   * en un `<optgroup>` aparte para que se lea que el dato NO se guarda en esta
   * hoja. El backend publica sólo los que el importador escribe de verdad.
   */
  crossFields?: CrossFieldCatalogEntry[];
  className: string;
  /**
   * Deshabilitado —no vacío— mientras carga el catálogo: un select sin opciones
   * es justamente el fallo que se está corrigiendo (mostraría «Sin mapear»
   * sobre un target real).
   */
  disabled?: boolean;
  /** El usuario está escribiendo el nombre de un campo propio para esta columna. */
  editingCustom?: boolean;
  ignoreLabel?: string;
  /**
   * Ofrece «ignorar» dentro del `<select>`. El modal de columnas sin mapear la
   * expone como botón aparte, así que la apaga: sin esto, unificar el cuarto
   * selector le agregaría una opción que hoy no tiene.
   */
  showIgnoreOption?: boolean;
  /**
   * Texto de la opción vacía. En las filas del panel el vacío significa «esta
   * columna no está mapeada»; en el modal significa «todavía no elegiste», que
   * es otra frase para el mismo valor.
   */
  placeholderLabel?: string;
  /** Ofrece crear un campo propio (emite el centinela `__custom__`). */
  showCustomFieldOption?: boolean;
  unknownTarget?: UnknownTargetMode;
  /**
   * Por dónde entra el salto del banner de faltantes: a qué hoja pertenece este
   * select y qué campo SUGERÍA Véktor para esta columna. Se busca por atributo y
   * no por `id` porque el nombre de la columna es texto libre del archivo
   * (espacios, acentos, comillas).
   */
  dataSheet?: string;
  dataSuggests?: string;
}) {
  const isCustom = target.startsWith("custom_field:");
  const fueraDelCatalogo =
    !!target &&
    target !== "ignore" &&
    fields.length > 0 &&
    !fields.some((f) => f.value === target) &&
    // Un cruzado tiene su propia `<option>` abajo: sin esta condición se
    // renderizaba además como «(campo desconocido)», que es justo el fallo
    // que este componente vino a cerrar.
    !crossFields.some((f) => f.value === target);

  let opcionFueraDelCatalogo: ReactNode = null;
  if (unknownTarget === "custom-only") {
    opcionFueraDelCatalogo = isCustom ? (
      <option value={target}>{target}</option>
    ) : null;
  } else if (unknownTarget === "catalog-guarded") {
    opcionFueraDelCatalogo = fueraDelCatalogo ? (
      <option value={target}>
        {isCustom ? target : `${target} (campo desconocido)`}
      </option>
    ) : null;
  } else if (isCustom) {
    opcionFueraDelCatalogo = <option value={target}>{target}</option>;
  } else if (fueraDelCatalogo) {
    opcionFueraDelCatalogo = (
      <option value={target}>{target} (campo desconocido)</option>
    );
  }

  return (
    <select
      value={editingCustom ? "__custom__" : target}
      onChange={(e) => onChange(e.target.value)}
      data-sheet={dataSheet}
      data-suggests={dataSuggests}
      disabled={disabled}
      className={className}
    >
      <option value="">{placeholderLabel}</option>
      {showIgnoreOption && <option value="ignore">{ignoreLabel}</option>}
      {fields.map((f) => (
        <option key={f.value} value={f.value}>
          {f.label}
        </option>
      ))}
      {crossFields.length > 0 && (
        <optgroup label="Otras secciones">
          {crossFields.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </optgroup>
      )}
      {opcionFueraDelCatalogo}
      {showCustomFieldOption && (
        <option value="__custom__">+ Campo personalizado…</option>
      )}
    </select>
  );
}
