import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { ChatPage } from "./pages/ChatPage";
import { BillingPage } from "./pages/BillingPage";
import { CreditsPage } from "./pages/CreditsPage";
import { ModelsPage } from "./pages/ModelsPage";
import { PricingPage } from "./pages/PricingPage";
import { UsageInsightsPage } from "./pages/UsageInsightsPage";
import { WorkPage } from "./pages/WorkPage";

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        <Route path="/usage" element={<UsageInsightsPage />} />
        <Route path="/work" element={<WorkPage />} />
        <Route path="/work/:workSessionId" element={<WorkPage />} />
        <Route path="/credits" element={<CreditsPage />} />
        <Route path="/models" element={<ModelsPage />} />
        <Route path="/pricing" element={<PricingPage />} />
        <Route path="/account/billing" element={<BillingPage />} />
        {/* Cognito redirects back to /auth?code=...; the backend handles OAuth exchange. */}
        <Route path="/auth" element={<RedirectHome />} />
        <Route path="/index.html" element={<RedirectHome />} />
        <Route path="*" element={<RedirectHome />} />
      </Routes>
    </BrowserRouter>
  );
}

function RedirectHome() {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  const freshLogin = params.get("fresh_login");
  const search = freshLogin === "1" ? "?fresh_login=1" : "";

  return <Navigate to={{ pathname: "/", search }} replace />;
}
