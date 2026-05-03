"use client";

import { SectionBoundary } from "@/lib/types";

interface Props {
  sections: SectionBoundary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

const KIND_INDENT: Record<string, string> = {
  CHAPTER: "ml-0",
  SECTION: "ml-2",
  SUBSECTION: "ml-4",
  APPENDIX: "ml-2",
  UNKNOWN: "ml-2",
};

export function SectionTree({ sections, selectedId, onSelect }: Props) {
  if (sections.length === 0) {
    return (
      <div className="text-sm text-gray-400 text-center py-8">
        No sections detected yet.
      </div>
    );
  }

  return (
    <div className="space-y-1 max-h-[600px] overflow-y-auto pr-1">
      <p className="text-xs font-medium text-gray-400 px-2 mb-2">
        {sections.length} sections
      </p>
      {sections.map((s) => (
        <button
          key={s.section_id}
          onClick={() => onSelect(s.section_id)}
          className={`
            w-full text-left px-3 py-2 rounded-lg text-xs transition-colors
            ${KIND_INDENT[s.kind] || "ml-2"}
            ${
              selectedId === s.section_id
                ? "bg-brand-50 text-brand-700 font-medium"
                : "text-gray-600 hover:bg-gray-50"
            }
          `}
        >
          <div className="truncate">{s.title}</div>
          <div className="text-[10px] text-gray-400 mt-0.5">
            p.{s.start_page + 1}–{s.end_page + 1}
          </div>
        </button>
      ))}
    </div>
  );
}
