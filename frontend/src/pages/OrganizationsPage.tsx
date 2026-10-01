import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ImageUp, Landmark, Pencil, Plus } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, qs } from "../api/client";
import type { Organization, Page } from "../api/types";
import {
  Button, Card, EmptyState, ErrorState, Field, Input, Modal, PageHeader, PasswordInput, SkeletonRows, StatusBadge, Table,
  Textarea,
} from "../components/ui";
// import { AIUsageCard } from "../components/AIUsageCard";
import { formatDateTime } from "../lib/format";
import { useToast } from "../store/hooks";

const COLOR = /^#[0-9a-fA-F]{6}$/;

interface Branding {
  primary_color: string;
  secondary_color: string;
  contact_email: string;
  contact_phone: string;
  website: string;
  address: string;
}
const EMPTY_BRANDING: Branding = { primary_color: "", secondary_color: "", contact_email: "", contact_phone: "", website: "", address: "" };

function brandingOf(org: Organization): Branding {
  return {
    primary_color: org.primary_color ?? "", secondary_color: org.secondary_color ?? "", contact_email: org.contact_email ?? "",
    contact_phone: org.contact_phone ?? "", website: org.website ?? "", address: org.address ?? "",
  };
}

function brandingBody(b: Branding) {
  const value = (v: string) => v.trim() || null;
  return {
    primary_color: value(b.primary_color), secondary_color: value(b.secondary_color), contact_email: value(b.contact_email),
    contact_phone: value(b.contact_phone), website: value(b.website), address: value(b.address),
  };
}

function slugify(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

export function OrganizationsPage() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Organization | null>(null);
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["organizations"],
    queryFn: () => api.get<Page<Organization>>(`/organizations${qs({ limit: 100 })}`),
  });

  const toggleStatus = async (org: Organization) => {
    const nextStatus = org.status === "active" ? "suspended" : "active";
    try {
      await api.patch<Organization>(`/organizations/${org.id}`, { status: nextStatus });
      toast("ok", `Organization ${org.name} marked as ${nextStatus}.`);
      void queryClient.invalidateQueries({ queryKey: ["organizations"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Failed to update organization status.");
    }
  };

  return (
    <>
      <PageHeader
        title="Organizations"
        subtitle="Create organizations, their branding and their first administrator"
        actions={<Button onClick={() => setCreating(true)}><Plus className="size-4" />New organization</Button>}
      />
      {/* OpenAI usage card: hidden for now. */}
      {/* <div className="mb-6"><AIUsageCard /></div> */}
      <Card>
        {error ? <div className="p-4"><ErrorState error={error} onRetry={refetch} /></div>
          : isLoading ? <SkeletonRows />
          : !data?.items.length ? (
            <EmptyState icon={<Landmark className="size-6" />} title="No organizations registered"
              description="Create your first organization with its admin to onboard a bank."
              action={<Button onClick={() => setCreating(true)}><Plus className="size-4" />Add organization</Button>} />
          ) : (
            <Table head={["Organization", "Branding", "Contact", "Status", "Created", ""]}>
              {data.items.map((org) => (
                <tr key={org.id}>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3">
                      <OrgLogo org={org} />
                      <div><p className="font-medium text-ink">{org.name}</p><p className="font-mono text-xs text-muted">{org.slug}</p></div>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex gap-1.5">
                      {[org.primary_color, org.secondary_color].map((c, i) => c
                        ? <span key={i} className="size-5 rounded border border-line" style={{ backgroundColor: c }} title={c} />
                        : <span key={i} className="size-5 rounded border border-dashed border-line-strong" title="Default" />)}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-xs text-ink-soft">{org.contact_email ?? "—"}{org.contact_phone && <><br />{org.contact_phone}</>}</td>
                  <td className="px-4 py-3"><StatusBadge status={org.status} /></td>
                  <td className="px-4 py-3 text-ink-soft">{formatDateTime(org.created_at)}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setEditing(org)}><Pencil className="size-3.5" />Edit</Button>
                      <Button variant="ghost" size="sm" onClick={() => toggleStatus(org)}>{org.status === "active" ? "Suspend" : "Activate"}</Button>
                    </div>
                  </td>
                </tr>
              ))}
            </Table>
          )}
      </Card>
      <CreateOrganizationModal open={creating} onClose={() => setCreating(false)} />
      <EditOrganizationModal org={editing} onClose={() => setEditing(null)} />
    </>
  );
}

function OrgLogo({ org, size = "size-9" }: { org: Organization; size?: string }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!org.has_logo) return setSrc(null);
    let url: string | null = null;
    api.blobUrl(`/organizations/${org.id}/logo`).then((r) => { url = r.url; setSrc(r.url); }).catch(() => setSrc(null));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [org.id, org.has_logo]);
  return src
    ? <img src={src} alt="" className={`${size} rounded border border-line bg-surface object-contain`} />
    : <span className={`${size} flex items-center justify-center rounded border border-line bg-subtle text-muted`}><Landmark className="size-4" /></span>;
}

function BrandingFields({ value, onChange }: { value: Branding; onChange: (next: Branding) => void }) {
  const set = (key: keyof Branding) => (e: { target: { value: string } }) => onChange({ ...value, [key]: e.target.value });
  const colorField = (key: "primary_color" | "secondary_color", label: string, hint: string) => (
    <Field label={label} hint={hint}>
      <div className="flex items-center gap-2">
        <input type="color" aria-label={`${label} picker`} value={COLOR.test(value[key]) ? value[key] : "#244bb0"}
          onChange={set(key)} className="h-9 w-10 cursor-pointer rounded border border-line-strong bg-surface p-0.5" />
        <Input value={value[key]} onChange={set(key)} placeholder="Default" maxLength={7} />
      </div>
      {value[key] && !COLOR.test(value[key]) && <p className="text-xs text-bad-600">Use a hex colour such as #1e3a8a.</p>}
    </Field>
  );
  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        {colorField("primary_color", "Primary colour", "Buttons, links and highlights")}
        {colorField("secondary_color", "Secondary colour", "The navigation sidebar")}
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Contact email"><Input type="email" value={value.contact_email} onChange={set("contact_email")} /></Field>
        <Field label="Contact phone"><Input value={value.contact_phone} onChange={set("contact_phone")} /></Field>
      </div>
      <Field label="Website"><Input value={value.website} onChange={set("website")} placeholder="https://" /></Field>
      <Field label="Address"><Textarea rows={2} value={value.address} onChange={set("address")} /></Field>
    </div>
  );
}

const brandingValid = (b: Branding) => [b.primary_color, b.secondary_color].every((c) => !c || COLOR.test(c));

function CreateOrganizationModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [branding, setBranding] = useState<Branding>(EMPTY_BRANDING);
  const [admin, setAdmin] = useState({ full_name: "", email: "", password: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setName(""); setSlug(""); setBranding(EMPTY_BRANDING); setAdmin({ full_name: "", email: "", password: "" }); setError(null);
  }, [open]);
  const adminGiven = Boolean(admin.email || admin.full_name || admin.password);
  const invalid = !name.trim() || !slug.trim() || !brandingValid(branding)
    || (adminGiven && (!admin.email || admin.full_name.trim().length < 2 || admin.password.length < 12));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (invalid) return;
    setBusy(true);
    setError(null);
    try {
      await api.post<Organization>("/organizations", {
        name: name.trim(), slug, ...brandingBody(branding), admin: adminGiven ? { ...admin, full_name: admin.full_name.trim() } : undefined,
      });
      toast("ok", `Organization "${name}" created${adminGiven ? ` with admin ${admin.email}` : ""}.`);
      void queryClient.invalidateQueries({ queryKey: ["organizations"] });
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create the organization.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} wide title="Create organization"
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button type="submit" form="create-org-form" loading={busy} disabled={invalid}>Create organization</Button></>}>
      <form id="create-org-form" onSubmit={submit} className="space-y-6">
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">Organization</h3>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Organization name *"><Input value={name} onChange={(e) => { setName(e.target.value); setSlug(slugify(e.target.value)); }} placeholder="ABC Bank" /></Field>
            <Field label="Slug *" hint="Lowercase letters, numbers and hyphens"><Input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="abc-bank" /></Field>
          </div>
        </section>
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">Branding and contact</h3>
          <BrandingFields value={branding} onChange={setBranding} />
          <p className="text-xs text-muted">Upload the logo after creating the organization (Edit).</p>
        </section>
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">First organization admin</h3>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Full name"><Input value={admin.full_name} onChange={(e) => setAdmin({ ...admin, full_name: e.target.value })} /></Field>
            <Field label="Email"><Input type="email" value={admin.email} onChange={(e) => setAdmin({ ...admin, email: e.target.value })} placeholder="admin@abcbank.com" /></Field>
          </div>
          <Field
            label="Temporary password"
            hint={
              admin.password && admin.password.length < 12
                ? `At least 12 characters required (${admin.password.length}/12 entered)`
                : "At least 12 characters. Share it securely; the admin can change it after signing in."
            }
          >
            <PasswordInput value={admin.password} onChange={(e) => setAdmin({ ...admin, password: e.target.value })} autoComplete="new-password" />
          </Field>
        </section>
        {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
      </form>
    </Modal>
  );
}

function EditOrganizationModal({ org, onClose }: { org: Organization | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [branding, setBranding] = useState<Branding>(EMPTY_BRANDING);
  const [current, setCurrent] = useState<Organization | null>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!org) return;
    setName(org.name); setBranding(brandingOf(org)); setCurrent(org);
  }, [org]);

  async function save() {
    setBusy(true);
    try {
      await api.patch(`/organizations/${org!.id}`, { name: name.trim(), ...brandingBody(branding) });
      toast("ok", "Organization updated.");
      void queryClient.invalidateQueries({ queryKey: ["organizations"] });
      void queryClient.invalidateQueries({ queryKey: ["my-organization"] });
      onClose();
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  async function uploadLogo(chosen: File) {
    const form = new FormData();
    form.append("file", chosen);
    try {
      const updated = await api.upload<Organization>(`/organizations/${org!.id}/logo`, form);
      setCurrent({ ...updated, has_logo: false });
      setTimeout(() => setCurrent(updated), 0); // reload the preview
      toast("ok", "Logo updated.");
      void queryClient.invalidateQueries({ queryKey: ["organizations"] });
    } catch (err) {
      toast("bad", err instanceof ApiError ? err.message : "Could not upload the logo.");
    }
  }

  return (
    <Modal open={Boolean(org)} onClose={onClose} wide title={`Edit ${org?.name ?? "organization"}`}
      footer={<><Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button loading={busy} disabled={!name.trim() || !brandingValid(branding)} onClick={save}>Save</Button></>}>
      <div className="space-y-5">
        <div className="flex items-center gap-4">
          {current && <OrgLogo org={current} size="size-16" />}
          <div>
            <Button size="sm" variant="secondary" onClick={() => file.current?.click()}><ImageUp className="size-3.5" />Upload logo</Button>
            <p className="mt-1 text-xs text-muted">PNG, JPEG or WebP, up to 2 MB.</p>
            <input ref={file} type="file" accept="image/png,image/jpeg,image/webp" className="hidden"
              onChange={(e) => { const chosen = e.target.files?.[0]; e.target.value = ""; if (chosen) void uploadLogo(chosen); }} />
          </div>
        </div>
        <Field label="Organization name"><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <BrandingFields value={branding} onChange={setBranding} />
      </div>
    </Modal>
  );
}

export { OrgLogo };
