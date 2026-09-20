import { type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import brandMarkUrl from "../../assets/brand/brand-mark.svg";
import { useTheme } from "../../hooks/useTheme";
import { useChatStore } from "../../store/chatStore";
import { AccountMenu } from "../layout/AccountMenu";
import { WorkspaceSidebar } from "../layout/WorkspaceSidebar";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./SubscriptionPageShell.module.css";

interface SubscriptionPageShellProps {
  title: string;
  subtitle: string;
  children: ReactNode;
  authLoading: boolean;
  authEnabled: boolean;
  loggedIn: boolean;
  onLogin?: () => void;
  onLogout: () => void | Promise<void>;
  planLabel?: string;
  billingActionLabel?: string;
  billingPastDue?: boolean;
  billingDestination?: "/pricing" | "/account/billing";
  activeView?: "account" | "credits";
}

export function SubscriptionPageShell({
  title,
  subtitle,
  children,
  authLoading,
  authEnabled,
  loggedIn,
  onLogin,
  onLogout,
  planLabel,
  billingActionLabel,
  billingPastDue,
  billingDestination = "/account/billing",
  activeView = "account",
}: SubscriptionPageShellProps) {
  const navigate = useNavigate();
  const { theme, toggleTheme } = useTheme();
  const startNewChat = useChatStore((state) => state.startNewChat);
  const setHistory = useChatStore((state) => state.setHistory);
  const setHistorySearch = useChatStore((state) => state.setHistorySearch);

  const handleLogout = () => {
    startNewChat();
    setHistory([]);
    setHistorySearch("");
    void onLogout();
  };

  const accountMenu = (
    <AccountMenu
      authEnabled={authEnabled}
      loggedIn={loggedIn}
      onLogin={authEnabled ? onLogin : undefined}
      onLogout={handleLogout}
      planLabel={planLabel}
      billingActionLabel={billingActionLabel}
      billingPastDue={billingPastDue}
      onBilling={planLabel ? () => navigate(billingDestination) : undefined}
      onModels={() => navigate("/models")}
      onUsageInsights={() => navigate("/usage")}
      onCredits={() => navigate("/credits")}
      theme={theme}
      onToggleTheme={toggleTheme}
    />
  );

  return (
    <div className={styles.layout}>
      <WorkspaceSidebar
        activeView={activeView}
        authLoading={authLoading}
        authEnabled={authEnabled}
        loggedIn={loggedIn}
      />

      <main className={styles.main}>
        <header className={styles.desktopHeader}>
          <button type="button" className={styles.brandButton} onClick={() => navigate("/")}>
            <img src={brandMarkUrl} alt="" aria-hidden="true" />
            <span>CortexAI</span>
          </button>
          <div className={styles.desktopTitle}>
            <strong>{title}</strong>
            <span>{subtitle}</span>
          </div>
          {accountMenu}
        </header>

        <header className={styles.mobileHeader}>
          <button
            type="button"
            className={styles.backButton}
            aria-label="Back to chat"
            onClick={() => navigate("/")}
          >
            <CortexIcon name="chevron-left" size={18} strokeWidth={2} />
          </button>
          <div className={styles.mobileTitle}>
            <strong>{title}</strong>
            <span>{subtitle}</span>
          </div>
          {accountMenu}
        </header>

        <div className={styles.content}>{children}</div>
      </main>
    </div>
  );
}
