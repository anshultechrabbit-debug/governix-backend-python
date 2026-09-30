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
    refetchInterval: (query) => (query.state.data && ACTIVE.includes(query.state.data.status) ? 1500 : false),
  });
}

export function useAnalysis(id: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["analysis", id],
    queryFn: () => api.get<Analysis>(`/documents/${id}/analysis`),
    enabled: Boolean(id) && enabled,
  });
}
