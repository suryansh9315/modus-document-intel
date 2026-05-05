// API client for the FastAPI backend

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function listDocuments() {
  const res = await fetch(`${API_BASE}/documents/`);
  if (!res.ok) throw new Error(`Failed to list documents: ${res.statusText}`);
  return res.json();
}

export async function getDocument(docId: string) {
  const res = await fetch(`${API_BASE}/documents/${docId}`);
  if (!res.ok) throw new Error(`Failed to get document: ${res.statusText}`);
  return res.json();
}

export async function getDocumentSections(docId: string) {
  const res = await fetch(`${API_BASE}/documents/${docId}/sections`);
  if (!res.ok) throw new Error(`Failed to get sections: ${res.statusText}`);
  return res.json();
}

export async function uploadDocument(
  file: File,
  onProgress?: (pct: number) => void,
): Promise<{ doc_id: string; filename: string; status: string }> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/ingestion/upload`);

    if (onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          onProgress(Math.round((e.loaded / e.total) * 100));
        }
      });
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch {
          reject(new Error("Invalid response from server"));
        }
      } else {
        try {
          const err = JSON.parse(xhr.responseText);
          reject(new Error(err.detail || "Upload failed"));
        } catch {
          reject(new Error(`Upload failed (${xhr.status})`));
        }
      }
    };

    xhr.onerror = () => reject(new Error("Network error during upload"));
    xhr.onabort = () => reject(new Error("Upload cancelled"));

    xhr.send(form);
  });
}

export async function getIngestionStatus(docId: string) {
  const res = await fetch(`${API_BASE}/ingestion/${docId}`);
  if (!res.ok) throw new Error(`Failed to get status: ${res.statusText}`);
  return res.json();
}

export function getQueryStreamUrl() {
  return `${API_BASE}/queries/stream`;
}

export async function runQuery(body: {
  doc_id: string;
  query_type: string;
  question: string;
  section_ids?: string[];
  stream?: boolean;
}) {
  const res = await fetch(`${API_BASE}/queries/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Query failed");
  }
  return res.json();
}
