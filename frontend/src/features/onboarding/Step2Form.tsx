"use client";

import { useState } from "react";
import {
  FISCAL_CONDITION_OPTIONS,
  FISCAL_PRIVACY_NOTE,
  type FiscalCondition,
} from "@/lib/fiscalCondition";
import { CONFIDENTIALITY_NOTICE } from "@/lib/privacyNotices";

export type MainConcern = "MARGIN" | "STOCK" | "CASH";

export interface Step2Data {
  weekly_sales_estimate_ars: number;
  monthly_inventory_cost_ars: number;
  monthly_fixed_expenses_ars: number;
  cash_on_hand_ars: number;
  product_count_estimate: number;
  supplier_count_estimate: number;
  main_concern: MainConcern;
  work_days: number[];
  work_open_hour: number;
  work_close_hour: number;
  /** Régimen fiscal — opcional y salteable. `null` = el usuario no eligió. */
  fiscal_condition: FiscalCondition | null;
}

// 0=lunes … 6=domingo (consistente con el backend)
const DAY_LABELS: { value: number; label: string }[] = [
  { value: 0, label: "Lun" },
  { value: 1, label: "Mar" },
  { value: 2, label: "Mié" },
  { value: 3, label: "Jue" },
  { value: 4, label: "Vie" },
  { value: 5, label: "Sáb" },
  { value: 6, label: "Dom" },
];
const DEFAULT_WORK_DAYS = [0, 1, 2, 3, 4, 5];
const DEFAULT_OPEN_HOUR = 9;
const DEFAULT_CLOSE_HOUR = 18;
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, h) => h);

interface Step2FormProps {
  initialData: Step2Data | null;
  onSubmit: (data: Step2Data) => void;
}

interface FormErrors {
  weekly_sales_estimate_ars?: string;
  product_count_estimate?: string;
  supplier_count_estimate?: string;
  main_concern?: string;
  work_schedule?: string;
}

const CONCERN_OPTIONS: { value: MainConcern; label: string }[] = [
  { value: "MARGIN", label: "Mis márgenes" },
  { value: "STOCK", label: "Mi stock" },
  { value: "CASH", label: "Mi caja" },
];

function FieldHint({ text }: { text: string }) {
  return <p className="mt-1.5 text-xs text-gray-400">{text}</p>;
}

function FieldHelper({ text }: { text: string }) {
  return <p className="mt-1 text-xs italic text-vk-text-muted">{text}</p>;
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
  helper,
  error,
  prefix,
  isInteger,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint: string;
  helper?: string;
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
      {helper && <FieldHelper text={helper} />}
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
  const [workDays, setWorkDays] = useState<number[]>(
    initialData?.work_days ?? DEFAULT_WORK_DAYS,
  );
  const [openHour, setOpenHour] = useState<number>(
    initialData?.work_open_hour ?? DEFAULT_OPEN_HOUR,
  );
  const [closeHour, setCloseHour] = useState<number>(
    initialData?.work_close_hour ?? DEFAULT_CLOSE_HOUR,
  );
  const [fiscalCondition, setFiscalCondition] = useState<FiscalCondition | null>(
    initialData?.fiscal_condition ?? null,
  );
  const [errors, setErrors] = useState<FormErrors>({});

  function toggleDay(day: number) {
    setWorkDays((prev) =>
      prev.includes(day)
        ? prev.filter((d) => d !== day)
        : [...prev, day].sort((a, b) => a - b),
    );
  }

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

    if (workDays.length === 0) {
      errs.work_schedule = "Elegí al menos un día laboral.";
    } else if (closeHour <= openHour) {
      errs.work_schedule = "El horario de cierre debe ser posterior al de apertura.";
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
      work_days: workDays,
      work_open_hour: openHour,
      work_close_hour: closeHour,
      fiscal_condition: fiscalCondition,
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
          helper="Usamos esto para calcular tu margen estimado. No hace falta que sea exacto."
          error={errors.weekly_sales_estimate_ars}
          prefix="$"
        />

        <NumberInput
          id="inventory-cost"
          label="¿Cuánto gastás en mercadería por mes?"
          value={fields.monthly_inventory_cost_ars}
          onChange={set("monthly_inventory_cost_ars")}
          hint="Todo lo que comprás para reponer stock en un mes."
          helper="Junto con tus ventas, determina si estás ganando o perdiendo en cada producto."
          prefix="$"
        />

        <NumberInput
          id="fixed-expenses"
          label="¿Cuánto pagás de gastos fijos por mes?"
          value={fields.monthly_fixed_expenses_ars}
          onChange={set("monthly_fixed_expenses_ars")}
          hint="Alquiler, luz, internet, teléfono y otros gastos fijos."
          helper="Necesitamos saber tu punto de equilibrio: cuánto tenés que vender para no perder."
          prefix="$"
        />

        <NumberInput
          id="cash-on-hand"
          label="¿Cuánta plata tenés disponible hoy?"
          value={fields.cash_on_hand_ars}
          onChange={set("cash_on_hand_ars")}
          hint="Efectivo en caja o en cuenta bancaria, lo que tenés para operar."
          helper="Este dato tiene 7 días de vigencia. Podés actualizarlo cuando quieras desde el dashboard."
          prefix="$"
        />

        <NumberInput
          id="product-count"
          label="¿Cuántos productos distintos vendés, más o menos?"
          value={fields.product_count_estimate}
          onChange={set("product_count_estimate")}
          hint="Una cantidad aproximada está bien, no necesitás ser exacto."
          helper="Con 5 o más productos podemos darte recomendaciones específicas por categoría."
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

        <div>
          <p className="mb-1 text-sm font-medium text-gray-800">
            ¿Qué días y horario abrís?
          </p>
          <FieldHelper text="Lo usamos para recordarte el cierre de caja al final del día. Podés cambiarlo después en Configuración." />
          <div className="mt-3 flex flex-wrap gap-2">
            {DAY_LABELS.map((d) => {
              const isSelected = workDays.includes(d.value);
              return (
                <button
                  key={d.value}
                  type="button"
                  onClick={() => toggleDay(d.value)}
                  className={[
                    "rounded-lg border-2 px-3 py-2 text-sm font-medium transition-all duration-150",
                    isSelected
                      ? "border-vk-navy bg-vk-navy text-white"
                      : "border-gray-200 bg-white text-gray-600 hover:border-gray-300",
                  ].join(" ")}
                >
                  {d.label}
                </button>
              );
            })}
          </div>
          <div className="mt-3 flex items-center gap-3">
            <label className="text-sm text-gray-600">Abre</label>
            <select
              value={openHour}
              onChange={(e) => setOpenHour(Number(e.target.value))}
              className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-vk-navy/20"
            >
              {HOUR_OPTIONS.map((h) => (
                <option key={h} value={h}>
                  {String(h).padStart(2, "0")}:00
                </option>
              ))}
            </select>
            <label className="text-sm text-gray-600">Cierra</label>
            <select
              value={closeHour}
              onChange={(e) => setCloseHour(Number(e.target.value))}
              className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-vk-navy/20"
            >
              {HOUR_OPTIONS.map((h) => (
                <option key={h} value={h}>
                  {String(h).padStart(2, "0")}:00
                </option>
              ))}
            </select>
          </div>
          {errors.work_schedule && <FieldError text={errors.work_schedule} />}
        </div>

        {/*
          Aviso de confidencialidad — el MISMO texto que el formulario público
          de solicitud (`lib/privacyNotices.ts`), reusado, no duplicado. Va acá
          arriba porque a partir de este punto el onboarding pregunta números y
          régimen fiscal, que es justo donde un negocio chico argentino se pone
          a la defensiva. `FISCAL_PRIVACY_NOTE` (abajo, en el selector) es otro
          aviso y convive con este: explica para qué sirve el dato, no qué
          hacemos con él.
        */}
        <div className="rounded-xl border border-gray-200 bg-gray-50 p-4">
          <p className="mb-2 flex items-center gap-2 text-sm font-semibold text-gray-800">
            <span aria-hidden>🔒</span>
            {CONFIDENTIALITY_NOTICE.title}
          </p>
          {CONFIDENTIALITY_NOTICE.paragraphs.map((parrafo) => (
            <p key={parrafo.slice(0, 24)} className="mt-2 text-xs leading-relaxed text-gray-500">
              {parrafo}
            </p>
          ))}
        </div>

        <div>
          <p className="mb-1 text-sm font-medium text-gray-800">
            ¿Cuál es tu régimen fiscal?{" "}
            <span className="font-normal text-gray-400">(opcional)</span>
          </p>
          <FieldHelper text="Adapta la guía de comprobantes del cierre de caja. Podés saltearlo y configurarlo después en Configuración." />
          <div
            role="radiogroup"
            aria-label="Régimen fiscal"
            className="mt-3 flex flex-col gap-2"
          >
            {FISCAL_CONDITION_OPTIONS.map((opt) => {
              const isSelected = fiscalCondition === opt.value;
              return (
                <label
                  key={opt.value}
                  className={[
                    "flex cursor-pointer items-start gap-3 rounded-xl border-2 px-4 py-3 transition-all duration-150",
                    isSelected
                      ? "border-vk-navy bg-vk-bg-light"
                      : "border-gray-200 bg-white hover:border-gray-300",
                  ].join(" ")}
                >
                  <input
                    type="radio"
                    name="fiscal-condition"
                    value={opt.value}
                    checked={isSelected}
                    onChange={() => setFiscalCondition(opt.value)}
                    className="mt-0.5 accent-vk-navy focus:outline-none focus:ring-2 focus:ring-vk-navy/30"
                  />
                  <span className="text-sm">
                    <span className="font-medium text-gray-900">{opt.label}</span>
                    <span className="block text-xs text-gray-500">{opt.detail}</span>
                  </span>
                </label>
              );
            })}
            <button
              type="button"
              onClick={() => setFiscalCondition(null)}
              className={[
                "self-start text-xs underline underline-offset-2 transition-colors",
                fiscalCondition === null
                  ? "text-gray-500"
                  : "text-gray-400 hover:text-gray-600",
              ].join(" ")}
            >
              Prefiero no decirlo ahora
            </button>
          </div>
          <p className="mt-3 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-500">
            {FISCAL_PRIVACY_NOTE}
          </p>
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
