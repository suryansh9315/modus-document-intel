// Shared types matching backend Pydantic models

export type DocumentStatus =
  | "PENDING"
  | "INGESTING"
  | "SEGMENTING"
  | "ANALYZING"
  | "AGGREGATING"
  | "READY"
  | "ERROR";

export type QueryType =
  | "SUMMARIZE_SECTION"
  | "SUMMARIZE_FULL"
  | "CROSS_SECTION_COMPARE"
  | "EXTRACT_ENTITIES"
  | "EXTRACT_RISKS"
  | "EXTRACT_DECISIONS"
  | "DETECT_CONTRADICTIONS";

export interface SectionBoundary {
  section_id: string;
  doc_id: string;
  title: string;
  kind: string;
  start_page: number;
  end_page: number;
}

export interface DocumentSummary {
  doc_id: string;
  filename: string;
  status: DocumentStatus;
  total_pages: number;
  created_at?: string;
  updated_at?: string;
  error_message?: string;
}

export interface DocumentRecord extends DocumentSummary {
  section_boundaries: SectionBoundary[];
  section_summaries: SectionSummary[];
  cluster_digests: ClusterDigest[];
  global_digest?: GlobalDigest;
}

export interface SectionSummary {
  section_id: string;
  doc_id: string;
  summary_text: string;
  key_metrics: Record<string, string>;
  key_entities: string[];
  key_risks: string[];
}

export interface ClusterDigest {
  cluster_id: string;
  doc_id: string;
  digest_text: string;
  section_ids: string[];
  cluster_index: number;
}

export interface GlobalDigest {
  doc_id: string;
  digest_text: string;
  executive_summary: string;
}

export interface ContradictionReport {
  contradiction_id: string;
  subject: string;
  claim_a_text: string;
  claim_a_section: string;
  claim_a_page: number;
  claim_b_text: string;
  claim_b_section: string;
  claim_b_page: number;
  explanation: string;
  severity: "low" | "medium" | "high";
}

export interface QueryRequest {
  doc_id: string;
  query_type: QueryType;
  question: string;
  section_ids?: string[];
  stream?: boolean;
}

export interface IngestionJob {
  job_id: string;
  doc_id: string;
  status: DocumentStatus;
  progress_pct: number;
  message: string;
  error?: string;
}
