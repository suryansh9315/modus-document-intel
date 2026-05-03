"use client";

import { DocumentStatus } from "@/lib/types";

const STEPS: { status: DocumentStatus; label: string; pct: number }[] = [
  { status: "INGESTING", label: "Extracting text (OCR)", pct: 15 },
  { status: "SEGMENTING", label: "Detecting sections", pct: 30 },
  { status: "ANALYZING", label: "Analyzing sections (L1)", pct: 60 },
  { status: "AGGREGATING", label: "Building summaries (L2/L3)", pct: 85 },
  { status: "READY", label: "Ready", pct: 100 },
];

interface Props {
  status: DocumentStatus;
}

export function IngestionProgress({ status }: Props) {
  const currentIndex = STEPS.findIndex((s) => s.status === status);
  const progress = currentIndex >= 0 ? STEPS[currentIndex].pct : 5;
  const label =
    currentIndex >= 0 ? STEPS[currentIndex].label : "Initializing...";

  return (
    <div className="bg-blue-50 border border-blue-100 rounded-xl p-5 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="animate-spin w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full" />
          <span className="text-sm font-medium text-blue-700">Processing document</span>
        </div>
        <span className="text-xs text-blue-500">{progress}%</span>
      </div>

      {/* Progress bar */}
      <div className="w-full bg-blue-100 rounded-full h-2">
        <div
          className="bg-blue-500 h-2 rounded-full transition-all duration-1000"
          style={{ width: `${progress}%` }}
        />
      </div>

      {/* Current step */}
      <p className="text-xs text-blue-600">{label}</p>

      {/* Step list */}
      <div className="space-y-1.5 pt-1">
        {STEPS.filter((s) => s.status !== "READY").map((step, i) => {
          const stepIndex = STEPS.findIndex((s) => s.status === status);
          const isDone = i < stepIndex;
          const isActive = i === stepIndex;
          return (
            <div
              key={step.status}
              className={`flex items-center gap-2 text-xs ${
                isDone
                  ? "text-green-600"
                  : isActive
                  ? "text-blue-700 font-medium"
                  : "text-gray-400"
              }`}
            >
              <div
                className={`w-4 h-4 rounded-full flex items-center justify-center flex-shrink-0 ${
                  isDone
                    ? "bg-green-100"
                    : isActive
                    ? "bg-blue-100"
                    : "bg-gray-100"
                }`}
              >
                {isDone ? "✓" : isActive ? "●" : "○"}
              </div>
              {step.label}
            </div>
          );
        })}
      </div>
    </div>
  );
}
