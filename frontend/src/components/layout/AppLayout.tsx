import {
  Bell, Building2, FileStack, FileText, Landmark, LayoutDashboard, LifeBuoy,
  ScrollText, ShieldCheck, Sparkles, Upload, UserCog, Users,
} from "lucide-react";
import { useEffect, useState, type ComponentType } from "react";
import { NavLink, Outlet } from "react-router";
import { api } from "../../api/client";
import type { Me } from "../../api/types";
import { cn } from "../../lib/format";
import { useAuth } from "../../store/hooks";
import { UploadActivity, UploadJobModal } from "../BulkUpload";
import { NotificationBell } from "../Notifications";
import { UserMenu } from "./UserMenu";

interface NavItem {
  to: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  permission?: string;
  end?: boolean;
}
interface NavSection { section: string; items: NavItem[] }

type Role = Me["role"];

/**
 * Navigation mirrors the spec's hierarchy (Master Admin → Organization Admin →
 * Branch Manager → User). Each role sees only its own keys, and each key is
 * additionally filtered by the permissions the backend granted, so the menu can
 * never offer a screen the API would refuse.
 */
const NAV: Record<Role, NavSection[]> = {
  master_admin: [
    {
      section: "Platform",
      items: [
        { to: "/organizations", label: "Organizations", icon: Landmark, permission: "organizations:manage" },
        { to: "/support", label: "Platform Support", icon: LifeBuoy, permission: "support" },
      ],
    },
  ],
  org_admin: [
    {
      section: "Knowledge",
      items: [
        { to: "/", label: "Dashboard", icon: LayoutDashboard, permission: "documents:read", end: true },
        { to: "/assistant", label: "AI Assistant", icon: Sparkles, permission: "ai:query" },
        { to: "/policies", label: "Global Policies", icon: ScrollText, permission: "policies:read" },
        { to: "/documents", label: "Documents", icon: FileText, permission: "documents:read" },
        { to: "/documents/upload", label: "Upload", icon: Upload, permission: "documents:upload" },
        { to: "/uploads", label: "Upload Batches", icon: FileStack, permission: "documents:upload" },
      ],
    },
    {
      section: "Organization",
      items: [
        { to: "/users", label: "Users & Roles", icon: Users, permission: "users:manage" },
        { to: "/branches", label: "Branches", icon: Building2, permission: "branches:manage" },
      ],
    },
  ],
  branch_manager: [
    {
      section: "Knowledge",
      items: [
        { to: "/", label: "Dashboard", icon: LayoutDashboard, permission: "documents:read", end: true },
        { to: "/assistant", label: "AI Assistant", icon: Sparkles, permission: "ai:query" },
        { to: "/policies", label: "Policies", icon: ScrollText, permission: "policies:read" },
        { to: "/documents", label: "Documents", icon: FileText, permission: "documents:read" },
        { to: "/documents/upload", label: "Upload", icon: Upload, permission: "documents:upload" },
        { to: "/uploads", label: "Upload Batches", icon: FileStack, permission: "documents:upload" },
      ],
    },
    {
      section: "My Branch",
      items: [{ to: "/users", label: "Branch Users", icon: UserCog, permission: "users:manage" }],
    },
  ],
  department_user: [
    {
      section: "Knowledge",
      items: [
        { to: "/", label: "Home", icon: LayoutDashboard, permission: "documents:read", end: true },
        { to: "/assistant", label: "AI Assistant", icon: Sparkles, permission: "ai:query" },
        { to: "/policies", label: "My Policies", icon: ScrollText, permission: "policies:read" },
      ],
    },
  ],
};

/** Support and notifications are available to every role (spec §40–46). */
const ACCOUNT: NavItem[] = [
  { to: "/notifications", label: "Notifications", icon: Bell },
  { to: "/support", label: "Support", icon: LifeBuoy, permission: "support" },
];

const ROLE_LABEL: Record<Role, string> = {
  master_admin: "Master Admin",
  org_admin: "Organization Admin",
  branch_manager: "Branch Manager",
  department_user: "User",
};

export function AppLayout() {
  const { me, can } = useAuth();
  const sections = me ? NAV[me.role] : [];
  const account = ACCOUNT.filter((item) => !item.permission || can(item.permission));

  if (!me) return null;
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col border-r border-line bg-surface">
        <OrganizationMark />
        <nav className="flex-1 space-y-6 overflow-y-auto px-3 pb-4" aria-label="Main">
          {sections.map(({ section, items }) => {
            const visible = items.filter((item) => !item.permission || can(item.permission));
            if (!visible.length) return null;
            return (
              <div key={section}>
                <p className="px-2 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">{section}</p>
                <ul className="space-y-0.5">
                  {visible.map(({ to, label, icon: Icon, end }) => (
                    <li key={to}>
                      <NavLink
                        to={to}
                        end={end}
                        className={({ isActive }) => cn(
                          "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                          isActive
                            ? "bg-brand-50 font-semibold text-brand-700"
                            : "text-ink-soft hover:bg-subtle hover:text-ink",
                        )}
                      >
                        <Icon className="size-4 shrink-0" />
                        {label}
                      </NavLink>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </nav>
        <div className="border-t border-line p-3">
          <UploadActivity />
          {!!account.length && (
            <ul className="mb-3 space-y-0.5">
              {account.map(({ to, label, icon: Icon }) => (
                <li key={to}>
                  <NavLink
                    to={to}
                    className={({ isActive }) => cn(
                      "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                      isActive ? "bg-brand-50 font-semibold text-brand-700" : "text-ink-soft hover:bg-subtle hover:text-ink",
                    )}
                  >
                    <Icon className="size-4 shrink-0" />
                    {label}
                  </NavLink>
                </li>
              ))}
            </ul>
          )}
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-ink">{me.full_name}</p>
              <p className="truncate text-[11px] text-muted">{ROLE_LABEL[me.role]}</p>
            </div>
            <NotificationBell />
            <UserMenu />
          </div>
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-8 py-7">
        <Outlet />
      </main>
      {/* The live upload queue follows the person across every page (spec §7–8). */}
      <UploadJobModal />
    </div>
  );
}

/** Organisation branding (spec §7.2): logo when the organization has one, mark otherwise. */
function OrganizationMark() {
  const { me } = useAuth();
  const [logo, setLogo] = useState<string | null>(null);
  useEffect(() => {
    if (!me?.organization_id) return;
    let url: string | undefined;
    let cancelled = false;
    void api.blobUrl(`/organizations/${me.organization_id}/logo`)
      .then((result) => { url = result.url; if (!cancelled) setLogo(result.url); })
      .catch(() => undefined); // no logo uploaded: the Governix mark is shown instead
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [me?.organization_id]);
  return (
    <div className="flex items-center gap-2.5 px-5 py-5">
      {logo
        ? <img src={logo} alt="" className="size-8 rounded object-contain" />
        : <ShieldCheck className="size-7 shrink-0 text-brand-600" />}
      <div className="min-w-0">
        <p className="truncate text-sm font-semibold tracking-wide text-ink">Governix</p>
        <p className="truncate text-[11px] text-muted" title={me?.organization_name ?? "Platform administration"}>
          {me?.organization_name ?? "Platform administration"}
        </p>
      </div>
    </div>
  );
}
