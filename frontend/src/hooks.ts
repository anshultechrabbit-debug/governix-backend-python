import { useQuery } from "@tanstack/react-query";
import { api } from "./api/client";
import type { Analysis, DocumentDetail } from "./api/types";

const ACTIVE = ["uploaded", "processing", "indexing"];

/** A document, polled while the pipeline is running. */
export function useDocument(id: string | undefined) {
  return useQuery({
    queryKey: ["document", id],
    queryFn: () => api.get<DocumentDetail>(`/documents/${id}`),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && (ACTIVE.includes(data.status) || data.filed_by_batch || semanticIndexing(data)) ? 1500 : false;
    },
  });
}

/** A READY document is searchable by keyword while its embeddings are still being written. */
export function semanticIndexing(document: DocumentDetail): boolean {
  return Boolean(document.progress?.stages.some((s) => (s.stage === "embedding" || s.stage === "indexing") && s.status === "running"));
}

export function useAnalysis(id: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["analysis", id],
    queryFn: () => api.get<Analysis>(`/documents/${id}/analysis`),
    enabled: Boolean(id) && enabled,
  });
}
