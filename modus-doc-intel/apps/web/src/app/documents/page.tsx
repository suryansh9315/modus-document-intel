"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { listDocuments } from "@/lib/api";
import { DocumentSummary } from "@/lib/types";

const STATUS_COLORS: Record<string, string> = {
  PENDING: "bg-gray-100 text-gray-600",
  INGESTING: "bg-blue-100 text-blue-700",
  SEGMENTING: "bg-yellow-100 text-yellow-700",
  ANALYZING: "bg-orange-100 text-orange-700",
  AGGREGATING: "bg-purple-100 text-purple-700",
  READY: "bg-green-100 text-green-700",
  ERROR: "bg-red-100 text-red-700",
};

export default function DocumentsPage() {
  const [docs, setDocs] = useState<DocumentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listDocuments()
      .then(setDocs)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-red-700">
        {error}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Documents</h1>
        <Link
          href="/"
          className="px-4 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-700"
        >
          + Upload New
        </Link>
      </div>

      {docs.length === 0 ? (
        <div className="text-center py-16 text-gray-400">
          <p className="text-lg">No documents uploaded yet.</p>
          <Link href="/" className="text-brand-600 hover:underline text-sm mt-2 inline-block">
            Upload your first document
          </Link>
        </div>
      ) : (
        <div className="grid gap-4">
          {docs.map((doc) => (
            <Link
              key={doc.doc_id}
              href={`/documents/${doc.doc_id}`}
              className="bg-white border border-gray-200 rounded-xl p-5 flex items-center justify-between hover:border-brand-300 hover:shadow-sm transition-all"
            >
              <div className="space-y-1">
                <h3 className="font-medium text-gray-900">{doc.filename}</h3>
                <p className="text-sm text-gray-400">
                  {doc.total_pages > 0 ? `${doc.total_pages} pages` : "Processing..."}
                  {doc.updated_at && ` · Updated ${new Date(doc.updated_at).toLocaleDateString()}`}
                </p>
                {doc.error_message && (
                  <p className="text-xs text-red-600">{doc.error_message}</p>
                )}
              </div>
              <span className={`px-3 py-1 rounded-full text-xs font-medium ${STATUS_COLORS[doc.status] || "bg-gray-100"}`}>
                {doc.status}
              </span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
