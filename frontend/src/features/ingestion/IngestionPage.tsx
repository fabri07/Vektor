"use client";

import { FileUploadSection } from "./FileUploadSection";
import { FileListSection } from "./FileListSection";
import { ManualEntrySection } from "./ManualEntrySection";

export function IngestionPage() {
  return (
    <div className="flex flex-col gap-6">
      <FileUploadSection />
      <FileListSection />
      <ManualEntrySection />
    </div>
  );
}
