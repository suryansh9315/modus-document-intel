"use client";

import { useState } from "react";
import { SectionBoundary, QueryType, ContradictionReport } from "@/lib/types";

const QUERY_TYPES: { value: QueryType; label: string; needsSection?: boolean }[] = [
  { value: "SUMMARIZE_FULL", label: "Full Document Summary" },
  { value: "SUMMARIZE_SECTION", label: "Summarize Section", needsSection: true },
  { value: "CROSS_SECTION_COMPARE", label: "Compare Two Sections", needsSection: true },
  { value: "EXTRACT_ENTITIES", label: "Extract Entities" },
  { value: "EXTRACT_RISKS", label: "Extract Risks" },
  { value: "EXTRACT_DECISIONS", label: "Extract Decisions" },
  { value: "DETECT_CONTRADICTIONS", label: "Find Contradictions" },
];

interface Props {
  docId: string;
  sections: SectionBoundary[];
  selectedSection: string | null;
  onSelectSection: (id: string | null) => void;
  onAnswer: (text: string) => void;
  onContradictions: (items: ContradictionReport[]) => void;
  onStreamingChange: (streaming: boolean) => void;
}

export function QueryPanel({
  docId,
  sections,
  selectedSection,
  onSelectSection,
  onAnswer,
  onContradictions,
  onStreamingChange,
}: Props) {
  const [queryType, setQueryType] = useState<QueryType>("SUMMARIZE_FULL");
  const [question, setQuestion] = useState("");
  const [sectionA, setSectionA] = useState<string>("");
  const [sectionB, setSectionB] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const needsSection = QUERY_TYPES.find((q) => q.value === queryType)?.needsSection;
  const isCrossCompare = queryType === "CROSS_SECTION_COMPARE";

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!question.trim()) return;

    setError(null);
    setLoading(true);
    onStreamingChange(true);
    onAnswer("");

    const section_ids =
      isCrossCompare
        ? [sectionA, sectionB].filter(Boolean)
        : sectionA
        ? [sectionA]
        : undefined;

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          doc_id: docId,
          query_type: queryType,
          question,
          section_ids,
          stream: true,
        }),
      });

      if (!res.ok || !res.body) {
        const err = await res.text();
        throw new Error(err || "Query failed");
      }

      // Parse Vercel AI SDK data-stream protocol
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let fullAnswer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const text = decoder.decode(value, { stream: true });
        const lines = text.split("\n");

        for (const line of lines) {
          if (line.startsWith("0:")) {
            // Text token
            const token = JSON.parse(line.slice(2));
            fullAnswer += token;
            onAnswer(fullAnswer);
          } else if (line.startsWith("8:")) {
            // Metadata (sources, contradictions)
            try {
              const meta = JSON.parse(line.slice(2));
              if (meta.contradictions) {
                onContradictions(meta.contradictions);
              }
            } catch {
              // ignore parse errors
            }
          } else if (line.startsWith("d:")) {
            // Done
            break;
          }
        }
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Query failed");
      onAnswer("");
    } finally {
      setLoading(false);
      onStreamingChange(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {/* Query type selector */}
      <div>
        <label className="block text-xs font-medium text-gray-500 mb-1.5">
          Query Type
        </label>
        <select
          value={queryType}
          onChange={(e) => setQueryType(e.target.value as QueryType)}
          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-brand-500"
        >
          {QUERY_TYPES.map((q) => (
            <option key={q.value} value={q.value}>
              {q.label}
            </option>
          ))}
        </select>
      </div>

      {/* Section selector */}
      {needsSection && sections.length > 0 && (
        <div className={isCrossCompare ? "grid grid-cols-2 gap-2" : ""}>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1.5">
              {isCrossCompare ? "Section A" : "Section"}
            </label>
            <select
              value={sectionA}
              onChange={(e) => setSectionA(e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-brand-500"
            >
              <option value="">Select section...</option>
              {sections.map((s) => (
                <option key={s.section_id} value={s.section_id}>
                  {s.title.slice(0, 40)}
                </option>
              ))}
            </select>
          </div>
          {isCrossCompare && (
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1.5">
                Section B
              </label>
              <select
                value={sectionB}
                onChange={(e) => setSectionB(e.target.value)}
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-brand-500"
              >
                <option value="">Select section...</option>
                {sections.map((s) => (
                  <option key={s.section_id} value={s.section_id}>
                    {s.title.slice(0, 40)}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      )}

      {/* Question input */}
      <div>
        <label className="block text-xs font-medium text-gray-500 mb-1.5">
          Question
        </label>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. What are the key risks mentioned in this report?"
          rows={3}
          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-brand-500"
        />
      </div>

      {error && (
        <p className="text-xs text-red-600 bg-red-50 rounded-lg px-3 py-2">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={loading || !question.trim()}
        className="w-full py-2.5 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
      >
        {loading ? (
          <>
            <div className="animate-spin w-4 h-4 border-2 border-white border-t-transparent rounded-full" />
            Analyzing...
          </>
        ) : (
          "Ask Question"
        )}
      </button>
    </form>
  );
}
