"use client";

import { FileUploadSection } from "./FileUploadSection";
import { FileListSection } from "./FileListSection";
import { ManualEntryLauncher } from "./ManualEntryLauncher";

export function IngestionPage() {
  return (
    <div className="flex flex-col gap-6">
      <FileUploadSection />
      <FileListSection />
      <div className="flex items-center justify-between rounded-xl border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
        <div>
          <h2 className="text-sm font-semibold text-vk-text-primary">Carga manual</h2>
          <p className="text-xs text-vk-text-muted">
            Registrá ventas, gastos o productos uno por uno, con los campos de tu rubro.
          </p>
        </div>
        <ManualEntryLauncher />
      </div>
    </div>
  );
}
