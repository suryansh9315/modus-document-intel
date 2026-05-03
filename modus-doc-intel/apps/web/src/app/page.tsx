"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { uploadDocument } from "@/lib/api";

export default function UploadPage() {
  const router = useRouter();
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  async function handleFile(file: File) {
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setError("Only PDF files are supported.");
      return;
    }
    setError(null);
    setUploading(true);
    try {
      const result = await uploadDocument(file);
      router.push(`/documents/${result.doc_id}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Upload failed");
      setUploading(false);
    }
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
  }

  function handleInput(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) handleFile(file);
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-[70vh] gap-8">
      <div className="text-center space-y-3">
        <h1 className="text-4xl font-bold text-gray-900">
          Document Intelligence
        </h1>
        <p className="text-lg text-gray-500 max-w-xl">
          Upload a financial PDF report. Our multi-agent AI system will analyze it,
          extract insights, and answer your questions.
        </p>
      </div>

      {/* Upload zone */}
      <div
        onDrop={handleDrop}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        className={`
          w-full max-w-xl border-2 border-dashed rounded-2xl p-12
          flex flex-col items-center gap-4 cursor-pointer transition-colors
          ${dragOver ? "border-brand-500 bg-brand-50" : "border-gray-300 bg-white hover:border-gray-400"}
          ${uploading ? "opacity-60 pointer-events-none" : ""}
        `}
      >
        <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center">
          <svg className="w-8 h-8 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
          </svg>
        </div>

        <div className="text-center">
          <p className="text-base font-medium text-gray-700">
            {uploading ? "Uploading..." : "Drop your PDF here"}
          </p>
          <p className="text-sm text-gray-400 mt-1">or click to browse</p>
        </div>

        <label className="cursor-pointer">
          <input
            type="file"
            accept=".pdf"
            className="hidden"
            onChange={handleInput}
            disabled={uploading}
          />
          <span className="inline-flex items-center px-4 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-700 transition-colors">
            Select PDF
          </span>
        </label>
      </div>

      {error && (
        <div className="w-full max-w-xl bg-red-50 border border-red-200 rounded-xl p-4 text-red-700 text-sm">
          {error}
        </div>
      )}

      <Link
        href="/documents"
        className="text-sm text-brand-600 hover:underline"
      >
        View previously uploaded documents →
      </Link>
    </div>
  );
}
