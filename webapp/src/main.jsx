import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import AppShell from "@/components/layout/AppShell";
import IntroductionPage from "./pages/IntroductionPage";
import "./styles/base.css";
import "./styles/introduction.css";
import "./styles/workbench.css";
import "./styles/admin.css";
import "./styles/document.css";

const AdminPage = lazy(() => import("./pages/AdminPage"));
const DocumentPage = lazy(() => import("./pages/DocumentPage"));
const WorkbenchPage = lazy(() => import("./pages/WorkbenchPage"));

function RouteFallback() {
  return (
    <div className="grid min-h-[calc(100svh-4rem)] place-items-center p-6" role="status" aria-live="polite">
      <p className="text-sm font-medium text-muted-foreground">화면을 불러오는 중입니다.</p>
    </div>
  );
}

function ApplicationLayout() {
  return (
    <AppShell>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/" element={<IntroductionPage />} />
          <Route path="/classification" element={<WorkbenchPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/document/:jobId/:taric10" element={<DocumentPage />} />
          <Route path="*" element={<Navigate to="/classification" replace />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}

function App() {
  return (
    <BrowserRouter>
      <ApplicationLayout />
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
