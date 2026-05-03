"use client";

import { useState } from "react";

interface Props {
  onUpload: (file: File) => void;
  uploading?: boolean;
}

export function UploadZone({ onUpload, uploading = false }: Props) {
  const [dragOver, setDragOver] = useState(false);

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) onUpload(file);
  }

  function handleInput(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) onUpload(file);
  }

  return (
    <div
      onDrop={handleDrop}
      onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      className={`
        border-2 border-dashed rounded-2xl p-12 flex flex-col items-center gap-4
        cursor-pointer transition-colors
        ${dragOver ? "border-brand-500 bg-brand-50" : "border-gray-300 bg-white hover:border-gray-400"}
        ${uploading ? "opacity-60 pointer-events-none" : ""}
      `}
    >
      <svg className="w-10 h-10 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
          d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
      </svg>
      <p className="text-sm font-medium text-gray-600">
        {uploading ? "Uploading..." : "Drop PDF here or click to upload"}
      </p>
      <label>
        <input type="file" accept=".pdf" className="hidden" onChange={handleInput} disabled={uploading} />
        <span className="px-4 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-700 transition-colors cursor-pointer">
          Select File
        </span>
      </label>
    </div>
  );
}
