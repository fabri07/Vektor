"use client";

import { useState } from "react";

export type MainConcern = "MARGIN" | "STOCK" | "CASH";

export interface Step2Data {
  weekly_sales_estimate_ars: number;
  monthly_inventory_cost_ars: number;
  monthly_fixed_expenses_ars: number;
  cash_on_hand_ars: number;
  product_count_estimate: number;
  supplier_count_estimate: number;
  main_concern: MainConcern;
}

interface Step2FormProps {
  initialData: Step2Data | null;
  onSubmit: (data: Step2Data) => void;
}

interface FormErrors {
  weekly_sales_estimate_ars?: string;
  product_count_estimate?: string;
  supplier_count_estimate?: string;
  main_concern?: string;
}

const CONCERN_OPTIONS: { value: MainConcern; label: string }[] = [
  { value: "MARGIN", label: "Mis márgenes" },
  { value: "STOCK", label: "Mi stock" },
  { value: "CASH", label: "Mi caja" },
];

function FieldHint({ text }: { text: string }) {
  return <p className="mt-1.5 text-xs text-gray-400">{text}</p>;
}

function FieldError({ text }: { text: string }) {
  return <p className="mt-1.5 text-xs text-vk-danger">{text}</p>;
}

function NumberInput({
  id,
  label,
  value,
  onChange,
  hint,
  error,
  prefix,
  isInteger,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint: string;
  error?: string;
  prefix?: string;
  isInteger?: boolean;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-gray-800">
        {label}
      </label>
      <div className="relative mt-2">
        {prefix && (
          <span className="absolute inset-y-0 left-0 flex items-center pl-3.5 text-base font-medium text-gray-400 select-none">
            {prefix}
          </span>
        )}
        <input
          id={id}
          type="number"
          inputMode={isInteger ? "numeric" : "decimal"}
          min={0}
          step={isInteger ? 1 : "any"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="0"
          className={[
            "h-14 w-full rounded-xl border bg-white text-base text-gray-900 transition-colors",
            "focus:outline-none focus:ring-2",
            prefix ? "pl-9 pr-4" : "px-4",
            error
              ? "border-vk-danger/60 focus:ring-vk-danger/20"
              : "border-gray-300 focus:border-gray-400 focus:ring-gray-200",
          ].join(" ")}
        />
      </div>
      {error ? <FieldError text={error} /> : <FieldHint text={hint} />}
    </div>
  );
}

export function Step2Form({ initialData, onSubmit }: Step2FormProps) {
  const [fields, setFields] = useState({
    weekly_sales_estimate_ars: String(
      initialData?.weekly_sales_estimate_ars ?? "",
    ),
    monthly_inventory_cost_ars: String(
      initialData?.monthly_inventory_cost_ars ?? "",
    ),
    monthly_fixed_expenses_ars: String(
      initialData?.monthly_fixed_expenses_ars ?? "",
    ),
    cash_on_hand_ars: String(initialData?.cash_on_hand_ars ?? ""),
    product_count_estimate: String(initialData?.product_count_estimate ?? ""),
    supplier_count_estimate: String(initialData?.supplier_count_estimate ?? ""),
    main_concern: initialData?.main_concern ?? ("" as MainConcern | ""),
  });
  const [errors, setErrors] = useState<FormErrors>({});

  function set(key: keyof typeof fields) {
    return (v: string) => setFields((prev) => ({ ...prev, [key]: v }));
  }

  function validate(): Step2Data | null {
    const errs: FormErrors = {};

    const weeklySales = parseFloat(fields.weekly_sales_estimate_ars);
    if (isNaN(weeklySales) || weeklySales <= 0) {
      errs.weekly_sales_estimate_ars = "Ingresá un monto mayor a 0.";
    }

    const productCount = parseInt(fields.product_count_estimate, 10);
    if (isNaN(productCount) || productCount < 0) {
      errs.product_count_estimate = "Ingresá un número válido.";
    }

    const supplierCount = parseInt(fields.supplier_count_estimate, 10);
    if (isNaN(supplierCount) || supplierCount < 0) {
      errs.supplier_count_estimate = "Ingresá un número válido.";
    }

    if (!fields.main_concern) {
      errs.main_concern = "Seleccioná una opción.";
    }

    setErrors(errs);
    if (Object.keys(errs).length > 0) return null;

    return {
      weekly_sales_estimate_ars: weeklySales,
      monthly_inventory_cost_ars:
        parseFloat(fields.monthly_inventory_cost_ars) || 0,
      monthly_fixed_expenses_ars:
        parseFloat(fields.monthly_fixed_expenses_ars) || 0,
      cash_on_hand_ars: parseFloat(fields.cash_on_hand_ars) || 0,
      product_count_estimate: productCount ?? 0,
      supplier_count_estimate: supplierCount ?? 0,
      main_concern: fields.main_concern as MainConcern,
    };
  }

  function handleNext() {
    const data = validate();
    if (data) onSubmit(data);
  }

  return (
    <div>
      <h2 className="mb-1 text-xl font-semibold text-gray-900">
        Contanos sobre tu negocio
      </h2>
      <p className="mb-8 text-sm text-gray-500">
        No hace falta que sea exacto, una estimación está bien.
      </p>

      <div className="space-y-6">
        <NumberInput
          id="weekly-sales"
          label="¿Cuánto vendés por semana, aproximadamente?"
          value={fields.weekly_sales_estimate_ars}
          onChange={set("weekly_sales_estimate_ars")}
          hint="Suma total de ventas en una semana típica, en pesos."
          error={errors.weekly_sales_estimate_ars}
          prefix="$"
        />

        <NumberInput
          id="inventory-cost"
          label="¿Cuánto gastás en mercadería por mes?"
          value={fields.monthly_inventory_cost_ars}
          onChange={set("monthly_inventory_cost_ars")}
          hint="Todo lo que comprás para reponer stock en un mes."
          prefix="$"
        />

        <NumberInput
          id="fixed-expenses"
          label="¿Cuánto pagás de gastos fijos por mes?"
          value={fields.monthly_fixed_expenses_ars}
          onChange={set("monthly_fixed_expenses_ars")}
          hint="Alquiler, luz, internet, teléfono y otros gastos fijos."
          prefix="$"
        />

        <NumberInput
          id="cash-on-hand"
          label="¿Cuánta plata tenés disponible hoy?"
          value={fields.cash_on_hand_ars}
          onChange={set("cash_on_hand_ars")}
          hint="Efectivo en caja o en cuenta bancaria, lo que tenés para operar."
          prefix="$"
        />

        <NumberInput
          id="product-count"
          label="¿Cuántos productos distintos vendés, más o menos?"
          value={fields.product_count_estimate}
          onChange={set("product_count_estimate")}
          hint="Una cantidad aproximada está bien, no necesitás ser exacto."
          error={errors.product_count_estimate}
          isInteger
        />

        <NumberInput
          id="supplier-count"
          label="¿Cuántos proveedores usás?"
          value={fields.supplier_count_estimate}
          onChange={set("supplier_count_estimate")}
          hint="Cantidad de proveedores con los que trabajás habitualmente."
          error={errors.supplier_count_estimate}
          isInteger
        />

        <div>
          <p className="mb-3 text-sm font-medium text-gray-800">
            ¿Qué te preocupa más hoy?
          </p>
          <div className="flex flex-col gap-2 sm:flex-row sm:gap-3">
            {CONCERN_OPTIONS.map((opt) => {
              const isSelected = fields.main_concern === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() =>
                    setFields((prev) => ({
                      ...prev,
                      main_concern: opt.value,
                    }))
                  }
                  className={[
                    "flex-1 rounded-xl border-2 py-3 px-4 text-sm font-medium transition-all duration-150",
                    isSelected
                      ? "border-vk-danger bg-vk-danger-bg text-vk-danger"
                      : "border-gray-200 bg-white text-gray-600 hover:border-gray-300",
                  ].join(" ")}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
          {errors.main_concern && (
            <FieldError text={errors.main_concern} />
          )}
          <FieldHint text="Esto nos ayuda a personalizar los análisis para vos." />
        </div>
      </div>

      <div className="mt-8 flex justify-end">
        <button
          type="button"
          onClick={handleNext}
          className="h-11 rounded-xl bg-vk-navy px-8 text-sm font-semibold text-white transition-opacity hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-vk-navy/30"
        >
          Siguiente
        </button>
      </div>
    </div>
  );
}
