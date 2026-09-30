import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Plus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, ApiError, qs } from "../api/client";
import type { Branch, Page } from "../api/types";
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Modal,
  PageHeader,
  SkeletonRows,
  StatusBadge,
  Table,
} from "../components/ui";
import { formatDateTime } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";

export function BranchesPage() {
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();

  const [modalOpen, setModalOpen] = useState(false);
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["branches"],
    queryFn: () => api.get<Page<Branch>>(`/branches${qs({ limit: 100 })}`),
  });

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      await api.post<Branch>("/branches", { name, code: code.toUpperCase() });
      toast("ok", `Branch "${name}" created.`);
      setModalOpen(false);
      setName("");
      setCode("");
      queryClient.invalidateQueries({ queryKey: ["branches"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to create branch.");
    } finally {
      setSubmitting(false);
    }
  };

  const toggleStatus = async (branch: Branch) => {
    try {
      await api.patch<Branch>(`/branches/${branch.id}`, { is_active: !branch.is_active });
      toast("ok", `Branch "${branch.name}" is now ${!branch.is_active ? "active" : "inactive"}.`);
      queryClient.invalidateQueries({ queryKey: ["branches"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to update branch.");
    }
  };

  return (
    <>
      <PageHeader
        title="Branches"
        subtitle="Manage organizational offices and geographic divisions"
        actions={
          can("branches:manage") && (
            <Button onClick={() => setModalOpen(true)}>
              <Plus className="size-4" /> Add branch
            </Button>
          )
        }
      />

      <Card>
        {error ? (
          <div className="p-4">
            <ErrorState error={error} onRetry={refetch} />
          </div>
        ) : isLoading ? (
          <SkeletonRows />
        ) : !data?.items.length ? (
          <EmptyState
            icon={<Building2 className="size-6" />}
            title="No branches found"
            description="Create branch offices to organize policies and staff by physical location or business unit."
            action={
              can("branches:manage") && (
                <Button onClick={() => setModalOpen(true)}>
                  <Plus className="size-4" /> Add branch
                </Button>
              )
            }
          />
        ) : (
          <Table head={["Branch Name", "Code", "Status", "Created", "Actions"]}>
            {data.items.map((branch) => (
              <tr key={branch.id}>
                <td className="px-4 py-3 font-medium text-ink">{branch.name}</td>
                <td className="px-4 py-3 font-mono text-xs text-muted">{branch.code}</td>
                <td className="px-4 py-3">
                  <StatusBadge status={branch.is_active ? "active" : "suspended"} />
                </td>
                <td className="px-4 py-3 text-ink-soft">{formatDateTime(branch.created_at)}</td>
                <td className="px-4 py-3">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => toggleStatus(branch)}
                  >
                    {branch.is_active ? "Deactivate" : "Activate"}
                  </Button>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="Add new branch"
        footer={
          <>
            <Button variant="secondary" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="create-branch-form"
              loading={submitting}
              disabled={!name.trim() || !code.trim()}
            >
              Create branch
            </Button>
          </>
        }
      >
        <form id="create-branch-form" onSubmit={handleCreate} className="space-y-4">
          <Field label="Branch name" hint="e.g. Headquarters, Mumbai Regional Office">
            <Input
              required
              placeholder="Headquarters"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label="Branch code" hint="Short unique identifier, e.g. HQ, MUM-01">
            <Input
              required
              placeholder="HQ"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
            />
          </Field>
        </form>
      </Modal>
    </>
  );
}
