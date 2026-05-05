"use client";

import { useEffect, useState, useRef } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { getDocument, getIngestionStatus } from "@/lib/api";
import { DocumentRecord, ContradictionReport } from "@/lib/types";
import { SectionTree } from "@/components/section-tree";
import { QueryPanel } from "@/components/query-panel";
import { AnswerDisplay } from "@/components/answer-display";
import { IngestionProgress } from "@/components/ingestion-progress";

const POLL_INTERVAL = 3000; // 3s

export default function DocumentPage() {
  const params = useParams();
  const docId = params.docId as string;

  const [doc, setDoc] = useState<DocumentRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [answer, setAnswer] = useState<string>("");
  const [streaming, setStreaming] = useState(false);
  const [contradictions, setContradictions] = useState<ContradictionReport[]>([]);
  const [selectedSection, setSelectedSection] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"query" | "sections">("query");

  const pollRef = useRef<NodeJS.Timeout | null>(null);

  // Load document and poll if not ready
  useEffect(() => {
    async function loadDoc() {
      try {
        const data = await getDocument(docId);
        setDoc(data);

        if (!["READY", "ERROR"].includes(data.status)) {
          // Poll for updates
          pollRef.current = setInterval(async () => {
            try {
              const updated = await getDocument(docId);
              setDoc(updated);
              if (["READY", "ERROR"].includes(updated.status)) {
                clearInterval(pollRef.current!);
              }
            } catch {
              // ignore poll errors
            }
          }, POLL_INTERVAL);
        }
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : "Failed to load document");
      } finally {
        setLoading(false);
      }
    }
    loadDoc();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [docId]);

  function handleAnswer(text: string) {
    setAnswer(text);
  }

  function handleContradictions(items: ContradictionReport[]) {
    setContradictions(items);
  }

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <div className="animate-spin w-8 h-8 border-2 border-brand-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error || !doc) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-red-700">
        {error || "Document not found"}
      </div>
    );
  }

  const isReady = doc.status === "READY";
  const isProcessing = !["READY", "ERROR"].includes(doc.status);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 truncate max-w-2xl">
            {doc.filename}
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            {doc.total_pages > 0 && `${doc.total_pages} pages · `}
            {doc.section_boundaries.length > 0 && `${doc.section_boundaries.length} sections · `}
            <span className="font-medium">{doc.status}</span>
          </p>
        </div>
        <Link href="/documents" className="text-sm text-gray-500 hover:text-gray-700">
          ← All documents
        </Link>
      </div>

      {/* Ingestion progress */}
      {isProcessing && (
        <IngestionProgress status={doc.status} />
      )}

      {/* Error state */}
      {doc.status === "ERROR" && doc.error_message && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-red-700 text-sm">
          <strong>Ingestion failed:</strong> {doc.error_message}
        </div>
      )}

      {/* Main interface — only when ready */}
      {isReady && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left: sections + tabs */}
          <div className="lg:col-span-1 space-y-4">
            <div className="flex gap-1 bg-gray-100 p-1 rounded-lg">
              {(["query", "sections"] as const).map((tab) => (
                <button
                  key={tab}
                  onClick={() => setActiveTab(tab)}
                  className={`flex-1 py-1.5 px-2 text-xs font-medium rounded-md transition-colors capitalize ${
                    activeTab === tab
                      ? "bg-white text-gray-900 shadow-sm"
                      : "text-gray-500 hover:text-gray-700"
                  }`}
                >
                  {tab}
                </button>
              ))}
            </div>

            {activeTab === "query" && (
              <QueryPanel
                docId={docId}
                sections={doc.section_boundaries}
                selectedSection={selectedSection}
                onSelectSection={setSelectedSection}
                onAnswer={handleAnswer}
                onContradictions={handleContradictions}
                onStreamingChange={setStreaming}
              />
            )}

            {activeTab === "sections" && (
              <SectionTree
                sections={doc.section_boundaries}
                selectedId={selectedSection}
                onSelect={setSelectedSection}
              />
            )}

          </div>

          {/* Right: answer display */}
          <div className="lg:col-span-2">
            <AnswerDisplay answer={answer} streaming={streaming} />
          </div>
        </div>
      )}
    </div>
  );
}
