import { useEffect, useRef, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router";
import { Spinner, Toaster } from "./components/ui";
import { AppLayout } from "./components/layout/AppLayout";
import { AssistantPage } from "./pages/AssistantPage";
import { BranchesPage } from "./pages/BranchesPage";
import { DashboardPage } from "./pages/DashboardPage";
import { DocumentDetailPage } from "./pages/DocumentDetailPage";
import { DocumentsPage } from "./pages/DocumentsPage";
import { DocumentViewerPage } from "./pages/DocumentViewerPage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { NotificationsPage } from "./pages/NotificationsPage";
import { OrganizationsPage } from "./pages/OrganizationsPage";
import { ComparePage } from "./pages/ComparePage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { PolicyDetailPage } from "./pages/PolicyDetailPage";
import { SupportPage } from "./pages/SupportPage";
import { TicketPage } from "./pages/TicketPage";
import { UploadBatchesPage, UploadBatchPage } from "./pages/UploadBatchPage";
import { UploadPage } from "./pages/UploadPage";
import { UsersPage } from "./pages/UsersPage";
import { restoreSession } from "./store/authSlice";
import { useAppDispatch, useAuth } from "./store/hooks";

function Guard({ permission, children }: { permission: string; children: ReactNode }) {
  const { can } = useAuth();
  return can(permission) ? children : <NotFoundPage />;
}

export function App() {
  const dispatch = useAppDispatch();
  const { me, loading, can } = useAuth();
  const restoreStarted = useRef(false);

  useEffect(() => {
    // React Strict Mode re-runs effects in development. Refresh tokens rotate
    // on use, so two restore calls would make the second call look like token reuse.
    if (loading && !restoreStarted.current) {
      restoreStarted.current = true;
      dispatch(restoreSession());
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return <div className="flex h-screen items-center justify-center"><Spinner /></div>;
  }
  if (!me) {
    return (
      <>
        <Routes><Route path="*" element={<LoginPage />} /></Routes>
        <Toaster />
      </>
    );
  }
  // The Master Admin runs the platform and never reads tenant knowledge, so it lands on Organizations.
  const home = can("documents:read") ? <DashboardPage /> : <Navigate to="/organizations" replace />;
  return (
    <>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={home} />

          {/* AI — access-controlled retrieval, latest version first (spec §28–35) */}
          <Route path="assistant" element={<Guard permission="ai:query"><AssistantPage /></Guard>} />

          {/* Knowledge */}
          <Route path="policies" element={<Guard permission="policies:read"><PoliciesPage /></Guard>} />
          <Route path="policies/:id" element={<Guard permission="policies:read"><PolicyDetailPage /></Guard>} />
          <Route path="policies/:id/compare" element={<Guard permission="policies:read"><ComparePage /></Guard>} />
          <Route path="documents" element={<Guard permission="documents:read"><DocumentsPage /></Guard>} />
          <Route path="documents/upload" element={<Guard permission="documents:upload"><UploadPage /></Guard>} />
          <Route path="documents/:id" element={<Guard permission="documents:read"><DocumentDetailPage /></Guard>} />
          <Route path="documents/:id/view" element={<Guard permission="documents:read"><DocumentViewerPage /></Guard>} />
          <Route path="uploads" element={<Guard permission="documents:upload"><UploadBatchesPage /></Guard>} />
          <Route path="uploads/:id" element={<Guard permission="documents:upload"><UploadBatchPage /></Guard>} />

          {/* Organization */}
          <Route path="users" element={<Guard permission="users:manage"><UsersPage /></Guard>} />
          <Route path="branches" element={<Guard permission="branches:manage"><BranchesPage /></Guard>} />
          <Route path="organizations" element={<Guard permission="organizations:manage"><OrganizationsPage /></Guard>} />

          {/* Account */}
          <Route path="notifications" element={<NotificationsPage />} />
          <Route path="support" element={<Guard permission="support"><SupportPage /></Guard>} />
          <Route path="support/:id" element={<Guard permission="support"><TicketPage /></Guard>} />

          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
      <Toaster />
    </>
  );
}
