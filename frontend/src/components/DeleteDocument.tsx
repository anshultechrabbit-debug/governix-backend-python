import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Trash2 } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { DocumentRead } from "../api/types";
import { useToast } from "../store/hooks";
import { Button, Modal } from "./ui";

/** Statuses the pipeline is still working on: the server refuses to delete these. */
export const PROCESSING = ["uploaded", "processing", "indexing"];

export function canDelete(document: Pick<DocumentRead, "status">) {
  return !PROCESSING.includes(document.status);
}

/** Confirm, then permanently delete a document. */
export function DeleteDocumentDialog({ document, open, onClose, onDeleted }: {
  document: Pick<DocumentRead, "id" | "title" | "original_filename" | "policy_id">;
  open: boolean;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.delete(`/documents/${document.id}`),
    onSuccess: () => {
      toast("ok", `Deleted ${document.title ?? document.original_filename}.`);
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.invalidateQueries({ queryKey: ["policies"] });
      queryClient.removeQueries({ queryKey: ["document", document.id] });
      onClose();
      onDeleted?.();
    },
    onError: (err) => toast("bad", err instanceof ApiError ? err.message : "The document could not be deleted."),
  });

  return (
    <Modal
      open={open}
      onClose={() => !remove.isPending && onClose()}
      title="Delete document?"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={remove.isPending}>Cancel</Button>
        <Button variant="danger" onClick={() => remove.mutate()} loading={remove.isPending}><Trash2 className="size-4" />Delete permanently</Button>
      </>}
    >
      <div className="flex gap-3">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-bad-50">
          <AlertTriangle className="size-5 text-bad-600" />
        </div>
        <div className="min-w-0 text-sm">
          <p className="truncate font-medium text-ink" title={document.title ?? document.original_filename}>
            {document.title ?? document.original_filename}
          </p>
          {document.title && <p className="truncate text-xs text-muted">{document.original_filename}</p>}
          <ul className="mt-3 list-disc space-y-1 pl-4 text-ink-soft">
            <li>The file, its pages and its search index are removed. The AI Assistant will no longer answer from it.</li>
            {document.policy_id && <li>Its version is removed from the policy. The previous version, if any, is back in force; a policy left with no versions is deleted.</li>}
            <li>This cannot be undone. The audit log keeps a record of the deletion.</li>
          </ul>
        </div>
      </div>
    </Modal>
  );
}
