"use client";

import { useEffect, useRef } from "react";

interface Props {
  answer: string;
  streaming: boolean;
}

// Simple markdown renderer (avoids adding heavy deps)
function renderMarkdown(text: string): string {
  return text
    // Headers
    .replace(/^## (.+)$/gm, '<h2 class="text-lg font-semibold mt-6 mb-2">$1</h2>')
    .replace(/^### (.+)$/gm, '<h3 class="text-base font-semibold mt-4 mb-1">$1</h3>')
    // Bold
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // Italic
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    // Bullet points
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    // Page citations [p.X]
    .replace(/\[p\.(\d+)\]/g, '<sup class="text-brand-600 font-medium">[p.$1]</sup>')
    // Paragraphs (double newlines)
    .replace(/\n\n/g, '</p><p class="mb-3">')
    // Wrap in paragraph
    .replace(/^/, '<p class="mb-3">')
    .replace(/$/, '</p>')
    // Fix list items
    .replace(/(<li>.*<\/li>\n?)+/g, (match) => `<ul class="list-disc list-inside space-y-1 ml-2 mb-3">${match}</ul>`);
}

export function AnswerDisplay({ answer, streaming }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (streaming) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [answer, streaming]);

  if (!answer && !streaming) {
    return (
      <div className="bg-white border border-gray-200 rounded-2xl p-8 min-h-[400px] flex items-center justify-center">
        <div className="text-center space-y-3 text-gray-400">
          <div className="w-16 h-16 bg-gray-50 rounded-2xl flex items-center justify-center mx-auto">
            <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
            </svg>
          </div>
          <p className="text-sm">Ask a question to get started</p>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-white border border-gray-200 rounded-2xl p-8 min-h-[400px]">
      <div className="flex items-center gap-2 mb-4">
        <div className="w-2 h-2 bg-green-500 rounded-full" />
        <span className="text-xs text-gray-400 font-medium">
          {streaming ? "Generating answer..." : "Answer"}
        </span>
        {streaming && (
          <div className="animate-spin w-3 h-3 border border-gray-400 border-t-transparent rounded-full" />
        )}
      </div>

      <div
        className="answer-text text-gray-800 text-sm leading-relaxed prose prose-sm max-w-none"
        dangerouslySetInnerHTML={{ __html: renderMarkdown(answer) }}
      />

      {streaming && (
        <span className="inline-block w-0.5 h-4 bg-brand-500 animate-pulse ml-0.5 align-text-bottom" />
      )}

      <div ref={bottomRef} />
    </div>
  );
}
