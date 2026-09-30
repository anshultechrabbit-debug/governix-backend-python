import { useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Users as UsersIcon } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, ApiError, qs } from "../api/client";
import type { Branch, Organization, Page, User } from "../api/types";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Modal,
  PageHeader,
  Select,
  SkeletonRows,
  StatusBadge,
  Table,
} from "../components/ui";
import { formatDateTime } from "../lib/format";
import { useAuth, useToast } from "../store/hooks";

/** The spec names the four roles; the API's `department_user` is simply "User". */
const ROLE_LABEL: Record<User["role"], string> = {
  master_admin: "Master Admin",
  org_admin: "Organization Admin",
  branch_manager: "Branch Manager",
  department_user: "User",
};

export function UsersPage() {
  const { me, can } = useAuth();
  const queryClient = useQueryClient();
  const toast = useToast();

  const [search, setSearch] = useState("");
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [resetModalUser, setResetModalUser] = useState<User | null>(null);

  // Form states
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<string>(me?.role === "org_admin" ? "branch_manager" : "department_user");
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [selectedBranchId, setSelectedBranchId] = useState(me?.role === "branch_manager" ? (me?.branch_id ?? "") : "");
  const [submitting, setSubmitting] = useState(false);

  // Reset password state
  const [newPassword, setNewPassword] = useState("");
  const [resetting, setResetting] = useState(false);

  const isMasterAdmin = me?.role === "master_admin";
  // A Branch Manager's users belong to the manager's own branch and cannot be placed elsewhere (spec §24).
  const isBranchManager = me?.role === "branch_manager";
  const needsBranch = role === "branch_manager" || role === "department_user";

  const { data: orgsData } = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.get<Page<Organization>>("/organizations?limit=100"),
    enabled: isMasterAdmin && createModalOpen,
  });

  const { data: branchesData } = useQuery({
    queryKey: ["branches"],
    queryFn: () => api.get<Page<Branch>>("/branches?limit=100"),
    enabled: !isMasterAdmin && createModalOpen,
  });

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["users", search],
    queryFn: () => api.get<Page<User>>(`/users${qs({ search, limit: 100 })}`),
  });

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      await api.post<User>("/users", {
        email,
        full_name: fullName,
        password,
        role,
        organization_id: isMasterAdmin ? selectedOrgId || null : null,
        branch_id: needsBranch ? selectedBranchId || null : null,
      });
      toast("ok", `${ROLE_LABEL[role as User["role"]]} "${fullName}" created.`);
      setCreateModalOpen(false);
      setEmail("");
      setFullName("");
      setPassword("");
      setSelectedOrgId("");
      if (!isBranchManager) setSelectedBranchId("");
      queryClient.invalidateQueries({ queryKey: ["users"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to create user.");
    } finally {
      setSubmitting(false);
    }
  };

  const handleResetPassword = async (e: FormEvent) => {
    e.preventDefault();
    if (!resetModalUser) return;
    setResetting(true);
    try {
      await api.post(`/users/${resetModalUser.id}/reset-password`, {
        new_password: newPassword,
      });
      toast("ok", `Password reset for ${resetModalUser.full_name}.`);
      setResetModalUser(null);
      setNewPassword("");
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to reset password.");
    } finally {
      setResetting(false);
    }
  };

  const toggleUserActive = async (user: User) => {
    if (user.id === me?.id) {
      toast("info", "You cannot modify your own active status.");
      return;
    }
    try {
      await api.patch<User>(`/users/${user.id}`, { is_active: !user.is_active });
      toast("ok", `User ${user.full_name} is now ${!user.is_active ? "active" : "inactive"}.`);
      queryClient.invalidateQueries({ queryKey: ["users"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to update user status.");
    }
  };

  const availableRoles: { value: string; label: string }[] = isMasterAdmin
    ? [{ value: "org_admin", label: "Organization Admin" }]
    : isBranchManager
    ? [{ value: "department_user", label: "User" }]
    : [
        { value: "branch_manager", label: "Branch Manager" },
        { value: "department_user", label: "User" },
      ];

  const branchName = branchesData?.items.find((b) => b.id === me?.branch_id)?.name;
  const title = isBranchManager ? "Branch Users" : "Users & Roles";
  const subtitle = isBranchManager
    ? `Users of ${branchName ?? "your branch"}. They read only the policies you assign to them.`
    : "Branch Managers and Users of your organization, and the roles that decide what each can read.";

  return (
    <>
      <PageHeader
        title={title}
        subtitle={subtitle}
        actions={
          can("users:manage") && (
            <Button onClick={() => setCreateModalOpen(true)}>
              <Plus className="size-4" /> {isBranchManager ? "Add user" : isMasterAdmin ? "Add organization admin" : "Add user"}
            </Button>
          )
        }
      />

      <Card>
        <div className="border-b border-line p-4">
          <Input
            className="max-w-sm"
            placeholder="Search by name or email…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {error ? (
          <div className="p-4">
            <ErrorState error={error} onRetry={refetch} />
          </div>
        ) : isLoading ? (
          <SkeletonRows />
        ) : !data?.items.length ? (
          <EmptyState
            icon={<UsersIcon className="size-6" />}
            title="No users found"
            description="Create accounts here, then assign the policies each person is allowed to read."
            action={
              can("users:manage") && (
                <Button onClick={() => setCreateModalOpen(true)}>
                  <Plus className="size-4" /> Add user
                </Button>
              )
            }
          />
        ) : (
          <Table head={["Name & Email", "Role", "Status", "Last Login", "Created", "Actions"]}>
            {data.items.map((user) => (
              <tr key={user.id}>
                <td className="px-4 py-3">
                  <p className="font-medium text-ink">{user.full_name}</p>
                  <p className="text-xs text-muted">{user.email}</p>
                </td>
                <td className="px-4 py-3">
                  <Badge tone={user.role === "master_admin" ? "brand" : "info"}>
                    {ROLE_LABEL[user.role]}
                  </Badge>
                </td>
                <td className="px-4 py-3">
                  <StatusBadge status={user.is_active ? "active" : "suspended"} />
                </td>
                <td className="px-4 py-3 text-ink-soft">
                  {user.last_login_at ? formatDateTime(user.last_login_at) : "Never"}
                </td>
                <td className="px-4 py-3 text-ink-soft">{formatDateTime(user.created_at)}</td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setResetModalUser(user)}
                      title="Reset password"
                    >
                      <KeyRound className="size-3.5" />
                    </Button>
                    {user.id !== me?.id && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => toggleUserActive(user)}
                      >
                        {user.is_active ? "Deactivate" : "Activate"}
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      {/* Create User Modal */}
      <Modal
        open={createModalOpen}
        onClose={() => setCreateModalOpen(false)}
        title="Add new user"
        footer={
          <>
            <Button variant="secondary" onClick={() => setCreateModalOpen(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="create-user-form"
              loading={submitting}
              disabled={
                !email.trim() ||
                !fullName.trim() ||
                password.length < 12 ||
                (isMasterAdmin && !selectedOrgId) ||
                (needsBranch && !selectedBranchId)
              }
            >
              Create user
            </Button>
          </>
        }
      >
        <form id="create-user-form" onSubmit={handleCreate} className="space-y-4">
          <Field label="Full name">
            <Input
              required
              placeholder="Alice Johnson"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
            />
          </Field>
          <Field label="Email address">
            <Input
              type="email"
              required
              placeholder="alice@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field label="Temporary password" hint="Minimum 12 characters">
            <Input
              type="password"
              required
              minLength={12}
              placeholder="••••••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <Field label="Role">
            <Select value={role} onChange={(e) => setRole(e.target.value)}>
              {availableRoles.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </Select>
          </Field>

          {isMasterAdmin && (
            <Field label="Organization" hint="Required for organization administrator">
              <Select
                required
                value={selectedOrgId}
                onChange={(e) => setSelectedOrgId(e.target.value)}
              >
                <option value="">Select organization…</option>
                {orgsData?.items.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </Select>
            </Field>
          )}

          {!isMasterAdmin && needsBranch && (
            <Field
              label="Branch"
              hint={isBranchManager
                ? "Users you create always belong to your own branch; it cannot be changed here."
                : "The branch this account belongs to."}
            >
              {isBranchManager ? (
                <p className="flex h-9 items-center rounded-md border border-line bg-subtle px-3 text-sm text-ink-soft">
                  {branchesData?.items.find((b) => b.id === me?.branch_id)?.name ?? "Your branch"}
                </p>
              ) : (
                <Select required value={selectedBranchId} onChange={(e) => setSelectedBranchId(e.target.value)}>
                  <option value="">Select branch…</option>
                  {branchesData?.items.map((b) => (
                    <option key={b.id} value={b.id}>{b.name} ({b.code})</option>
                  ))}
                </Select>
              )}
            </Field>
          )}
        </form>
      </Modal>

      {/* Reset Password Modal */}
      <Modal
        open={Boolean(resetModalUser)}
        onClose={() => setResetModalUser(null)}
        title={`Reset password for ${resetModalUser?.full_name}`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setResetModalUser(null)}>
              Cancel
            </Button>
            <Button
              type="submit"
              form="reset-password-form"
              loading={resetting}
              disabled={newPassword.length < 12}
            >
              Reset password
            </Button>
          </>
        }
      >
        <form id="reset-password-form" onSubmit={handleResetPassword} className="space-y-4">
          <Field label="New password" hint="Minimum 12 characters. Revokes all current sessions.">
            <Input
              type="password"
              required
              minLength={12}
              placeholder="••••••••••••"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
          </Field>
        </form>
      </Modal>
    </>
  );
}
