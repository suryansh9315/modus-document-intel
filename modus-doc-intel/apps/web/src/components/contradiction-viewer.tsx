"use client";

import { ContradictionReport } from "@/lib/types";

const SEVERITY_STYLES: Record<string, string> = {
  high: "bg-red-50 border-red-200 text-red-700",
  medium: "bg-orange-50 border-orange-200 text-orange-700",
  low: "bg-yellow-50 border-yellow-200 text-yellow-700",
};

const SEVERITY_BADGE: Record<string, string> = {
  high: "bg-red-100 text-red-700",
  medium: "bg-orange-100 text-orange-700",
  low: "bg-yellow-100 text-yellow-700",
};

interface Props {
  contradictions: ContradictionReport[];
}

export function ContradictionViewer({ contradictions }: Props) {
  if (contradictions.length === 0) {
    return (
      <div className="text-sm text-gray-400 text-center py-8 space-y-2">
        <div className="w-10 h-10 bg-gray-100 rounded-full flex items-center justify-center mx-auto">
          ✓
        </div>
        <p>No contradictions found</p>
        <p className="text-xs text-gray-300">Run a &ldquo;Find Contradictions&rdquo; query to detect conflicts</p>
      </div>
    );
  }

  return (
    <div className="space-y-3 max-h-[600px] overflow-y-auto pr-1">
      <p className="text-xs font-medium text-gray-400 px-1">
        {contradictions.length} contradiction{contradictions.length > 1 ? "s" : ""} found
      </p>
      {contradictions.map((c) => (
        <div
          key={c.contradiction_id}
          className={`border rounded-xl p-4 space-y-3 ${SEVERITY_STYLES[c.severity] || SEVERITY_STYLES.medium}`}
        >
          {/* Header */}
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold">{c.subject}</span>
            <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${SEVERITY_BADGE[c.severity]}`}>
              {c.severity.toUpperCase()}
            </span>
          </div>

          {/* Claims */}
          <div className="space-y-2">
            <div className="bg-white/60 rounded-lg p-2.5 text-xs">
              <div className="font-medium text-gray-500 mb-1">
                Claim A [p.{c.claim_a_page}] — {c.claim_a_section}
              </div>
              <div className="text-gray-800 italic">&ldquo;{c.claim_a_text}&rdquo;</div>
            </div>
            <div className="bg-white/60 rounded-lg p-2.5 text-xs">
              <div className="font-medium text-gray-500 mb-1">
                Claim B [p.{c.claim_b_page}] — {c.claim_b_section}
              </div>
              <div className="text-gray-800 italic">&ldquo;{c.claim_b_text}&rdquo;</div>
            </div>
          </div>

          {/* Explanation */}
          <p className="text-xs leading-relaxed">{c.explanation}</p>
        </div>
      ))}
    </div>
  );
}
