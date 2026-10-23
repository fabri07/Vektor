"use client";

import {
  SHIPPING_LABELS,
  TAX_LABELS,
  type PurchaseTerms,
  type ShippingMode,
  type TaxTreatment,
} from "./purchaseTerms";

/** B·5b: IVA y envío de una compra. Los mismos campos en «Otros», «Registrar
 * compra» y el remito, para que la misma compra dé el mismo costo. */
export function PurchaseTermsFields({
  value,
  onChange,
  showShippingModes = true,
}: {
  value: PurchaseTerms;
  onChange: (next: PurchaseTerms) => void;
  /** El remito siempre carga el envío aparte: ahí sólo se pide el IVA. */
  showShippingModes?: boolean;
}) {
  const inputCls = "rounded border border-vk-border-w px-3 py-2";
  const conAlicuota = value.taxTreatment === "incluido" || value.taxTreatment === "adicional";
  return (
    <div className="grid gap-3">
      <div className="grid grid-cols-2 gap-3">
        <label className="grid gap-1 text-sm text-vektor-body">
          IVA
          <select
            className={inputCls}
            value={value.taxTreatment}
            onChange={(e) => onChange({ ...value, taxTreatment: e.target.value as TaxTreatment })}
          >
            {(Object.keys(TAX_LABELS) as TaxTreatment[]).map((k) => (
              <option key={k} value={k}>
                {TAX_LABELS[k]}
              </option>
            ))}
          </select>
        </label>
        {conAlicuota ? (
          <label className="grid gap-1 text-sm text-vektor-body">
            Alícuota (%)
            <input
              className={inputCls}
              inputMode="decimal"
              placeholder="21"
              value={value.taxRatePct}
              onChange={(e) => onChange({ ...value, taxRatePct: e.target.value })}
            />
          </label>
        ) : null}
      </div>
      {showShippingModes ? (
        <div className="grid grid-cols-2 gap-3">
          <label className="grid gap-1 text-sm text-vektor-body">
            Envío
            <input
              className={inputCls}
              type="number"
              min={0}
              step="0.01"
              value={value.shippingAmount}
              onChange={(e) => onChange({ ...value, shippingAmount: e.target.value })}
            />
          </label>
          <label className="grid gap-1 text-sm text-vektor-body">
            ¿Qué se hace con el envío?
            <select
              className={inputCls}
              value={value.shippingMode}
              onChange={(e) => onChange({ ...value, shippingMode: e.target.value as ShippingMode })}
            >
              {(Object.keys(SHIPPING_LABELS) as ShippingMode[]).map((k) => (
                <option key={k} value={k}>
                  {SHIPPING_LABELS[k]}
                </option>
              ))}
            </select>
          </label>
        </div>
      ) : null}
    </div>
  );
}
