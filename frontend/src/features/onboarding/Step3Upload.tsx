"use client";

import { useRef, useState } from "react";

const ACCEPTED = ".xlsx,.csv,.txt,.docx,.jpg,.jpeg,.png";

interface Step3UploadProps {
  onNext: (file: File | null) => void;
}

export function Step3Upload({ onNext }: Step3UploadProps) {
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function handleFile(f: File) {
    setFile(f);
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    const dropped = e.dataTransfer.files[0];
    if (dropped) handleFile(dropped);
  }

  function handleInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const selected = e.target.files?.[0];
    if (selected) handleFile(selected);
  }

  return (
    <div>
      <h2 className="mb-1 text-xl font-semibold text-gray-900">
        ¿Tenés datos de tu negocio?
      </h2>
      <p className="mb-2 text-sm text-gray-500">
        Si tenés una lista de productos o ventas, podés subirla. Esto mejora
        la precisión del análisis.
      </p>
      <p className="mb-8 text-xs text-gray-400">
        Formatos aceptados: .xlsx, .csv, .txt, .docx, .jpg, .png
      </p>

      <div
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className={[
          "flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors cursor-pointer",
          dragOver
            ? "border-[#1A1A2E] bg-gray-100"
            : file
              ? "border-green-400 bg-green-50"
              : "border-gray-300 bg-white hover:border-gray-400",
        ].join(" ")}
      >
        <svg
          width="36"
          height="36"
          viewBox="0 0 24 24"
          fill="none"
          stroke={file ? "#22c55e" : "#9ca3af"}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
          <polyline points="17 8 12 3 7 8" />
          <line x1="12" y1="3" x2="12" y2="15" />
        </svg>

        {file ? (
          <div>
            <p className="text-sm font-medium text-green-700">{file.name}</p>
            <p className="mt-1 text-xs text-gray-400">
              {(file.size / 1024).toFixed(0)} KB — hacé clic para cambiar
            </p>
          </div>
        ) : (
          <div>
            <p className="text-sm font-medium text-gray-700">
              Arrastrá tu archivo aquí o{" "}
              <span className="text-[#1A1A2E] underline underline-offset-2">
                elegí uno
              </span>
            </p>
            <p className="mt-1 text-xs text-gray-400">Máx. 10 MB</p>
          </div>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED}
        className="hidden"
        onChange={handleInputChange}
      />

      <div className="mt-8 flex items-center justify-between">
        <button
          type="button"
          onClick={() => onNext(null)}
          className="text-sm text-gray-400 underline underline-offset-2 hover:text-gray-600 transition-colors"
        >
          Saltar por ahora
        </button>

        <button
          type="button"
          onClick={() => onNext(file)}
          className="h-11 rounded-xl bg-[#1A1A2E] px-8 text-sm font-semibold text-white transition-opacity hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-[#1A1A2E]/30"
        >
          {file ? "Continuar con archivo" : "Continuar"}
        </button>
      </div>
    </div>
  );
}
