import { useCallback, useEffect, useRef, useState } from "react";
import { CortexIcon, type CortexIconName } from "../shared/CortexIcon";
import type { AppTheme } from "../../hooks/useTheme";
import { getRuntimeConfig } from "../../config/runtimeConfig";
import styles from "./AccountMenu.module.css";

interface AccountMenuProps {
  authEnabled: boolean;
  loggedIn: boolean;
  onLogin?: () => void;
  onLogout?: () => void;
  planLabel?: string;
  billingActionLabel?: string;
  billingPastDue?: boolean;
  onBilling?: () => void;
  onModels?: () => void;
  onUsageInsights?: () => void;
  onCredits?: () => void;
  theme?: AppTheme;
  onToggleTheme?: () => void;
}

type AccountMenuActionKey =
  | "billing"
  | "credits"
  | "login"
  | "logout"
  | "models"
  | "theme"
  | "usage";

interface AccountMenuAction {
  key: AccountMenuActionKey;
  label: string;
  subtitle?: string;
  ariaLabel?: string;
  icon?: CortexIconName;
  accent?: boolean;
  warning?: boolean;
}

export function AccountMenu({
  authEnabled,
  loggedIn,
  onLogin,
  onLogout,
  planLabel,
  billingActionLabel,
  billingPastDue = false,
  onBilling,
  onModels,
  onUsageInsights,
  onCredits,
  theme,
  onToggleTheme,
}: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const canLogin = authEnabled && !!onLogin;
  const canLogout = !!onLogout;
  const canOpenBilling = !!planLabel && !!billingActionLabel && !!onBilling;
  const canOpenModels = getRuntimeConfig().modelsCatalogEnabled === true && !!onModels;
  const canOpenUsage = !!onUsageInsights;
  const canOpenCredits = !!onCredits;
  const canToggleTheme = !!theme && !!onToggleTheme;
  const nextTheme = theme === "dark" ? "light" : "dark";
  const menuActions: AccountMenuAction[] = [];
  if (canOpenBilling) {
    menuActions.push({
      key: "billing",
      label: planLabel,
      subtitle: billingPastDue ? `Past due · ${billingActionLabel}` : billingActionLabel,
      ariaLabel: `${planLabel}, ${billingPastDue ? "Past due, " : ""}${billingActionLabel}`,
      icon: "cost",
      accent: true,
      warning: billingPastDue,
    });
  }
  if (canOpenModels && !canOpenBilling) {
    menuActions.push({
      key: "models",
      label: "Models",
      subtitle: "Current model catalogue",
      icon: "models",
      accent: true,
    });
  }
  if (!loggedIn && canLogin) {
    menuActions.push({ key: "login", label: "Sign in" });
  }
  if (canOpenUsage) {
    menuActions.push({
      key: "usage",
      label: "Usage & insights",
      icon: "usage",
    });
  }
  if (canOpenCredits) {
    menuActions.push({
      key: "credits",
      label: "AI credits",
      icon: "cost",
    });
  }
  if (canOpenModels && canOpenBilling) {
    menuActions.push({
      key: "models",
      label: "Models",
      subtitle: "Current model catalogue",
      icon: "models",
    });
  }
  if (canToggleTheme) {
    menuActions.push({
      key: "theme",
      label: nextTheme === "dark" ? "Dark theme" : "Light theme",
      ariaLabel: `Switch to ${nextTheme} theme`,
      icon: nextTheme === "dark" ? "moon" : "sun",
    });
  }
  if (canLogout) {
    menuActions.push({ key: "logout", label: "Log off" });
  }
  const showMenu = menuActions.length > 0;
  const buttonLabel = loggedIn ? "Account" : "Guest account";

  const clearCloseTimer = useCallback(() => {
    if (closeTimerRef.current === null) return;
    window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = null;
  }, []);

  const openMenu = useCallback(() => {
    clearCloseTimer();
    if (showMenu) setOpen(true);
  }, [clearCloseTimer, showMenu]);

  const closeMenu = useCallback(() => {
    clearCloseTimer();
    setOpen(false);
  }, [clearCloseTimer]);

  const scheduleCloseMenu = useCallback(() => {
    clearCloseTimer();
    closeTimerRef.current = window.setTimeout(() => {
      setOpen(false);
      closeTimerRef.current = null;
    }, 160);
  }, [clearCloseTimer]);

  useEffect(() => {
    return () => {
      if (closeTimerRef.current !== null) {
        window.clearTimeout(closeTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!open) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) return;
      closeMenu();
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeMenu();
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, closeMenu]);

  const handleAction = (action: AccountMenuActionKey) => {
    closeMenu();
    if (action === "logout") {
      onLogout?.();
    } else if (action === "billing") {
      onBilling?.();
    } else if (action === "models") {
      onModels?.();
    } else if (action === "theme") {
      onToggleTheme?.();
    } else if (action === "usage") {
      onUsageInsights?.();
    } else if (action === "credits") {
      onCredits?.();
    } else {
      onLogin?.();
    }
  };

  return (
    <div
      ref={rootRef}
      className={styles.accountMenu}
      onMouseEnter={openMenu}
      onMouseLeave={(event) => {
        const nextTarget = event.relatedTarget;
        if (!(nextTarget instanceof Node) || !rootRef.current?.contains(nextTarget)) {
          scheduleCloseMenu();
        }
      }}
    >
      <button
        type="button"
        className={styles.accountButton}
        aria-label={buttonLabel}
        aria-haspopup={showMenu ? "menu" : undefined}
        aria-expanded={showMenu ? open : undefined}
        onClick={() => {
          if (showMenu) setOpen(true);
        }}
      >
        <CortexIcon name="user" />
      </button>

      {showMenu && open && (
        <div
          className={styles.menu}
          role="menu"
          aria-label="Account menu"
          onMouseEnter={openMenu}
        >
          {menuActions.map((action) => (
            <button
              key={action.key}
              type="button"
              role="menuitem"
              className={`${styles.menuItem} ${action.accent ? styles.menuItemAccent : ""} ${action.warning ? styles.menuItemWarning : ""}`}
              aria-label={action.ariaLabel}
              onClick={() => handleAction(action.key)}
            >
              {action.icon && (
                <span className={styles.menuIcon}>
                  <CortexIcon name={action.icon} />
                </span>
              )}
              <span className={styles.menuCopy}>
                <span>{action.label}</span>
                {action.subtitle ? <small>{action.subtitle}</small> : null}
              </span>
              {action.accent ? (
                <CortexIcon
                  className={styles.menuChevron}
                  name="chevron-right"
                  size={17}
                  strokeWidth={2}
                />
              ) : null}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
