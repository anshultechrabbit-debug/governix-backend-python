import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { useToast } from "../store/hooks";
import { Button, Input, Modal } from "./ui";

/** Confirm by typing the policy's name, then delete it with every version, document and file. */
export function DeletePolicyDialog({ policy, open, onClose, onDeleted }: {
  policy: { id: string; name: string; version_count?: number; versions?: unknown[] };
  open: boolean;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [typed, setTyped] = useState("");
  useEffect(() => { if (open) setTyped(""); }, [open]);
  const count = policy.version_count ?? policy.versions?.length ?? 0;
  const matches = typed.trim().toLowerCase() === policy.name.trim().toLowerCase();

  const remove = useMutation({
    mutationFn: () => api.delete<{ versions: number; documents: number }>(`/policies/${policy.id}`),
    onSuccess: (result) => {
      toast("ok", `Deleted ${policy.name} (${result.versions} version${result.versions === 1 ? "" : "s"}).`);
      for (const key of ["policies", "documents", "category-documents"]) queryClient.invalidateQueries({ queryKey: [key] });
      queryClient.removeQueries({ queryKey: ["policy", policy.id] });
      onClose();
      onDeleted?.();
    },
    onError: (err) => toast("bad", err instanceof ApiError ? err.message : "The policy could not be deleted."),
  });

  return (
    <Modal
      open={open}
      onClose={() => !remove.isPending && onClose()}
      title="Delete policy?"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={remove.isPending}>Cancel</Button>
        <Button variant="danger" disabled={!matches} onClick={() => remove.mutate()} loading={remove.isPending}>
          <Trash2 className="size-4" />Delete policy
        </Button>
      </>}
    >
      <div className="flex gap-3">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-full bg-bad-50">
          <AlertTriangle className="size-5 text-bad-600" />
        </div>
        <div className="min-w-0 text-sm">
          <p className="font-medium text-ink">{policy.name}</p>
          <ul className="mt-2 list-disc space-y-1 pl-4 text-ink-soft">
            <li>{count ? `All ${count} version${count === 1 ? "" : "s"} and their documents` : "All its versions and documents"} are deleted, with their files and search index.</li>
            <li>Assignments to users and links to related documents are removed. The AI Assistant will no longer answer from it.</li>
            <li>This cannot be undone. The audit log keeps a record of the deletion.</li>
          </ul>
          <p className="mt-3 text-xs text-muted">To withdraw a single version and keep it in the history, use the version's menu instead.</p>
        </div>
      </div>
      <div className="mt-4 space-y-1.5">
        <label htmlFor="delete-policy-name" className="block text-sm text-ink-soft">Type <span className="font-semibold text-ink">{policy.name}</span> to confirm</label>
        <Input id="delete-policy-name" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={policy.name} autoComplete="off"
          onKeyDown={(e) => { if (e.key === "Enter" && matches && !remove.isPending) remove.mutate(); }} />
      </div>
    </Modal>
  );
}
