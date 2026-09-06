import {
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { formatHistoryDateTime } from "../../history/historyDate";
import { buildHistoryThreads, filterHistoryThreads } from "../../history/historyThreads";
import { normalizeSessionId } from "../../session/activeSession";
import { useChatStore } from "../../store/chatStore";
import { useHistory } from "../../hooks/useHistory";
import type { ChatMode, HistoryThread, WhoAmIResponse, WorkSession } from "../../types";
import { CortexIcon } from "../shared/CortexIcon";
import brandMarkUrl from "../../assets/brand/brand-mark.svg";
import styles from "./Sidebar.module.css";

interface SidebarProps {
  onSelectThread: (thread: HistoryThread) => void;
  activeView?: "chat" | "work" | "usage" | "credits" | "models" | "account";
  onNavigateChat?: (mode: ChatMode) => void;
  onNavigateWork?: () => void;
  onNavigateUsage?: () => void;
  onNavigateCredits?: () => void;
  onNavigateModels?: () => void;
  whoAmI?: WhoAmIResponse | null;
  loggedIn?: boolean;
  onLogin?: () => void;
  signedOut?: boolean;
  newLabel?: "New chat" | "New work";
  onNew?: () => void;
  workSessions?: WorkSession[];
  activeWorkSessionId?: string | null;
  onSelectWorkSession?: (session: WorkSession) => void;
}

interface HistoryDateGroup {
  key: string;
  label: string;
  threads: HistoryThread[];
}

const MAX_VISIBLE_HISTORY_THREADS = 100;

export function Sidebar({
  onSelectThread,
  activeView = "chat",
  onNavigateChat,
  onNavigateWork,
  onNavigateUsage,
  onNavigateCredits,
  onNavigateModels,
  whoAmI,
  loggedIn,
  onLogin,
  signedOut = false,
  newLabel = "New chat",
  onNew,
  workSessions = [],
  activeWorkSessionId,
  onSelectWorkSession,
}: SidebarProps) {
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [openMenuKey, setOpenMenuKey] = useState<string | null>(null);
  const [renamingKey, setRenamingKey] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [confirmingDeleteKey, setConfirmingDeleteKey] = useState<string | null>(null);
  const deleteConfirmTimerRef = useRef<number | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const rowButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const history = useChatStore((s) => s.history);
  const historySearch = useChatStore((s) => s.historySearch);
  const setHistorySearch = useChatStore((s) => s.setHistorySearch);
  const sessionId = useChatStore((s) => s.sessionId);
  const mode = useChatStore((s) => s.mode);
  const setMode = useChatStore((s) => s.setMode);
  const startNewChat = useChatStore((s) => s.startNewChat);
  const { removeThread, renameThread } = useHistory();

  const filteredThreads = useMemo(() => {
    return filterHistoryThreads(buildHistoryThreads(history), historySearch).slice(
      0,
      MAX_VISIBLE_HISTORY_THREADS,
    );
  }, [history, historySearch]);
  const historyGroups = useMemo(() => groupHistoryThreads(filteredThreads), [filteredThreads]);

  const userLabel = signedOut ? "Sign in" : (whoAmI?.user_id ?? (loggedIn ? "Signed in" : "Guest"));
  const planLabel = signedOut
    ? "Access your workspace"
    : (whoAmI?.plan_tier ?? (loggedIn ? "Session active" : "Local session"));
  const sessionLabel = sessionId && !signedOut ? formatSessionId(sessionId) : userLabel;
  const sessionStatus = sessionId && !signedOut ? "Session active" : planLabel;
  const canSignInFromProfile = !loggedIn && !!onLogin;

  useEffect(() => {
    return () => {
      if (deleteConfirmTimerRef.current !== null) {
        window.clearTimeout(deleteConfirmTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!openMenuKey) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (menuRef.current?.contains(target) || target.closest("[data-chat-more]")) return;
      setOpenMenuKey(null);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenMenuKey(null);
    };
    const handleScroll = () => setOpenMenuKey(null);

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("scroll", handleScroll, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("scroll", handleScroll, true);
    };
  }, [openMenuKey]);

  const showDeleteConfirm = (threadKey: string) => {
    if (deleteConfirmTimerRef.current !== null) {
      window.clearTimeout(deleteConfirmTimerRef.current);
    }
    setConfirmingDeleteKey(threadKey);
    deleteConfirmTimerRef.current = window.setTimeout(() => {
      setConfirmingDeleteKey((current) => (current === threadKey ? null : current));
      deleteConfirmTimerRef.current = null;
    }, 3000);
  };

  const clearDeleteConfirm = () => {
    if (deleteConfirmTimerRef.current !== null) {
      window.clearTimeout(deleteConfirmTimerRef.current);
      deleteConfirmTimerRef.current = null;
    }
    setConfirmingDeleteKey(null);
  };

  const beginDelete = (thread: HistoryThread) => {
    setOpenMenuKey(null);
    setRenamingKey(null);
    showDeleteConfirm(thread.key);
  };

  const handleConfirmDeleteThread = async (
    event: MouseEvent<HTMLButtonElement>,
    thread: HistoryThread,
  ) => {
    event.stopPropagation();
    clearDeleteConfirm();

    const deleted = await removeThread(thread);
    if (
      deleted &&
      normalizeSessionId(thread.sessionId) &&
      normalizeSessionId(thread.sessionId) === normalizeSessionId(sessionId)
    ) {
      startNewChat();
    }
  };

  const handleCancelDelete = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    clearDeleteConfirm();
  };

  const beginRename = (thread: HistoryThread) => {
    clearDeleteConfirm();
    setOpenMenuKey(null);
    setRenamingKey(thread.key);
    setRenameDraft(thread.title);
  };

  const finishRename = async (thread: HistoryThread) => {
    const title = renameDraft.trim();
    setRenamingKey(null);
    if (title && title !== thread.title) {
      await renameThread(thread, title);
    }
    window.requestAnimationFrame(() => rowButtonRefs.current.get(thread.key)?.focus());
  };

  const cancelRename = (threadKey: string) => {
    setRenamingKey(null);
    setRenameDraft("");
    window.requestAnimationFrame(() => rowButtonRefs.current.get(threadKey)?.focus());
  };

  const handleThreadKeyDown = (event: ReactKeyboardEvent<HTMLElement>, thread: HistoryThread) => {
    if (event.target instanceof HTMLInputElement) return;

    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const index = filteredThreads.findIndex((candidate) => candidate.key === thread.key);
      if (index < 0) return;
      const delta = event.key === "ArrowDown" ? 1 : -1;
      const nextIndex = Math.min(filteredThreads.length - 1, Math.max(0, index + delta));
      rowButtonRefs.current.get(filteredThreads[nextIndex]?.key ?? "")?.focus();
      return;
    }

    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const key = event.key.toLowerCase();
    if (key === "enter") {
      event.preventDefault();
      onSelectThread(thread);
    } else if (key === "r") {
      event.preventDefault();
      beginRename(thread);
    } else if (key === "d") {
      event.preventDefault();
      beginDelete(thread);
    } else if (key === "escape") {
      event.preventDefault();
      setOpenMenuKey(null);
      clearDeleteConfirm();
    }
  };

  const handleModeNavigation = (nextMode: ChatMode) => {
    setMode(nextMode);
    onNavigateChat?.(nextMode);
  };

  const usageActive = activeView === "usage";
  const creditsActive = activeView === "credits";
  const modelsActive = activeView === "models";
  const askActive = activeView === "chat" && mode === "single";
  const compareActive = activeView === "chat" && mode === "compare";
  const workActive = activeView === "work";
  const visibleWorkSessions = workSessions.filter(
    (item) =>
      item.latest_run_status !== null &&
      (item.title || "New work").toLowerCase().includes(historySearch.trim().toLowerCase()),
  );
  const handleNew = onNew ?? startNewChat;

  const sidebarClassName = isCollapsed
    ? `${styles.sidebar} ${styles.sidebarCollapsed}`
    : styles.sidebar;
  const collapseLabel = isCollapsed ? "Expand sidebar" : "Collapse sidebar";

  return (
    <aside
      id="desktopSidebar"
      className={sidebarClassName}
      aria-label="Primary navigation"
      data-collapsed={isCollapsed ? "true" : "false"}
    >
      <div className={styles.brand}>
        <div className={styles.brandHeader}>
          <div className={styles.brandLockup}>
            <img className={styles.brandMark} src={brandMarkUrl} alt="" aria-hidden="true" />
            <div className={styles.brandText} hidden={isCollapsed}>
              <h1>CortexAI</h1>
              <p>LLM GATEWAY</p>
            </div>
          </div>
          <button
            type="button"
            className={styles.collapseButton}
            onClick={() => setIsCollapsed((current) => !current)}
            aria-controls="desktopSidebar"
            aria-expanded={!isCollapsed}
            aria-label={collapseLabel}
            title={collapseLabel}
          >
            <CortexIcon name={isCollapsed ? "expand-sidebar" : "collapse-sidebar"} />
          </button>
        </div>
      </div>

      <div className={styles.primaryAction}>
        <button
          id="historyNewChatBtn"
          type="button"
          className={styles.newChatButton}
          onClick={handleNew}
          aria-label={newLabel}
          title={isCollapsed ? newLabel : undefined}
          disabled={signedOut}
        >
          <CortexIcon name="new-chat" />
          <span>{newLabel}</span>
          <span className={styles.commandChip} aria-hidden="true">
            ⌘K
          </span>
        </button>
      </div>

      <nav className={styles.nav} aria-label="Workspace">
        <button
          type="button"
          className={askActive ? styles.navItemActive : styles.navItem}
          onClick={() => handleModeNavigation("single")}
          aria-current={askActive ? "page" : undefined}
          aria-label="Ask"
          title={isCollapsed ? "Ask" : undefined}
          disabled={signedOut}
        >
          <CortexIcon name="ask" />
          <span>Ask</span>
        </button>
        <button
          type="button"
          className={compareActive ? styles.navItemActive : styles.navItem}
          onClick={() => handleModeNavigation("compare")}
          aria-current={compareActive ? "page" : undefined}
          aria-label="Compare"
          title={isCollapsed ? "Compare" : undefined}
          disabled={signedOut}
        >
          <CortexIcon name="compare" />
          <span>Compare</span>
        </button>
        {onNavigateWork && (
          <button
            type="button"
            className={workActive ? styles.navItemActive : styles.navItem}
            onClick={onNavigateWork}
            aria-current={workActive ? "page" : undefined}
            aria-label="Work"
            title={isCollapsed ? "Work" : undefined}
            disabled={signedOut}
          >
            <CortexIcon name="work" />
            <span>Work</span>
          </button>
        )}
        <button
          type="button"
          className={usageActive ? styles.navItemActive : styles.navItem}
          onClick={onNavigateUsage}
          aria-current={usageActive ? "page" : undefined}
          aria-label="Usage"
          title={isCollapsed ? "Usage" : undefined}
          disabled={signedOut}
        >
          <CortexIcon name="usage" />
          <span>Usage</span>
        </button>
        <button
          type="button"
          className={creditsActive ? styles.navItemActive : styles.navItem}
          onClick={onNavigateCredits}
          aria-current={creditsActive ? "page" : undefined}
          aria-label="AI credits"
          title={isCollapsed ? "AI credits" : undefined}
          disabled={signedOut}
        >
          <CortexIcon name="cost" />
          <span>AI credits</span>
        </button>
        <button
          type="button"
          className={modelsActive ? styles.navItemActive : styles.navItem}
          onClick={onNavigateModels}
          aria-current={modelsActive ? "page" : undefined}
          aria-label="Models"
          title={isCollapsed ? "Models" : undefined}
          disabled={signedOut}
        >
          <CortexIcon name="models" />
          <span>Models</span>
        </button>
      </nav>

      <div className={styles.historyBlock} hidden={isCollapsed}>
        <div className={styles.historyHeader}>
          <span>Recent</span>
          {!signedOut && (
            <button
              type="button"
              className={styles.historyFilterButton}
              onClick={() => searchInputRef.current?.focus()}
              aria-label="Filter chats"
              title="Filter chats"
            >
              <FilterIcon />
            </button>
          )}
        </div>
        {signedOut ? (
          <div className={styles.signedOutHistory}>Sign in to view history.</div>
        ) : (
          <>
            <div className={styles.historySearchWrap}>
              <CortexIcon name="search" />
              <input
                id="historySearch"
                ref={searchInputRef}
                className={styles.historySearch}
                value={historySearch}
                onChange={(event) => setHistorySearch(event.target.value)}
                placeholder="Search chats"
                aria-label="Search chats"
              />
            </div>
            <ul className={styles.historyList}>
              {visibleWorkSessions.length > 0 && (
                <li className={styles.historyGroup}>
                  <div className={styles.historyGroupLabel}>Work</div>
                  <ul className={styles.historyGroupItems}>
                    {visibleWorkSessions.map((item) => {
                      const title = item.title || "New work";
                      const isActive = item.id === activeWorkSessionId;
                      return (
                        <li key={item.id} className={styles.historyItemRow}>
                          <div
                            className={`${styles.historyThreadSurface} ${isActive ? styles.historyItemActive : ""}`}
                            data-mode="work"
                            title={title}
                          >
                            <button
                              type="button"
                              className={styles.historySelectButton}
                              aria-label={`${title}. Work, ${item.latest_run_status || item.status}`}
                              aria-current={isActive ? "page" : undefined}
                              onClick={() => onSelectWorkSession?.(item)}
                            >
                              <span className={styles.historyTitle}>{title}</span>
                              <span className={styles.historyRight}>
                                <span className={styles.historyMeta}>
                                  {(item.latest_run_status || item.status).replaceAll("_", " ").toUpperCase()}
                                </span>
                              </span>
                            </button>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                </li>
              )}
              {historyGroups.map((group) => (
                <li key={group.key} className={styles.historyGroup}>
                  <div className={styles.historyGroupLabel}>{group.label}</div>
                  <ul className={styles.historyGroupItems}>
                    {group.threads.map((thread) => {
                      const isCompare = thread.preferredMode === "compare";
                      const modeLabel = isCompare ? "Compare" : "Ask";
                      const timeLabel = formatHistoryTime(thread.latestTimestamp);
                      const dateTimeLabel = formatHistoryDateTime(thread.latestTimestamp);
                      const isActive =
                        normalizeSessionId(thread.sessionId) !== null &&
                        normalizeSessionId(thread.sessionId) === normalizeSessionId(sessionId);
                      const isMenuOpen = openMenuKey === thread.key;
                      const isRenaming = renamingKey === thread.key;
                      const isConfirmingDelete = confirmingDeleteKey === thread.key;

                      return (
                        <li
                          key={thread.key}
                          className={styles.historyItemRow}
                          onKeyDown={(event) => handleThreadKeyDown(event, thread)}
                        >
                          {isConfirmingDelete ? (
                            <div
                              className={styles.historyDeleteConfirm}
                              role="group"
                              aria-label="Confirm delete chat"
                            >
                              <span className={styles.historyDeleteConfirmText}>Delete?</span>
                              <button
                                type="button"
                                className={styles.historyDeleteConfirmButton}
                                onClick={(event) => void handleConfirmDeleteThread(event, thread)}
                                aria-label="Confirm delete chat"
                              >
                                Delete
                              </button>
                              <button
                                type="button"
                                className={styles.historyDeleteCancelButton}
                                onClick={handleCancelDelete}
                              >
                                Cancel
                              </button>
                            </div>
                          ) : (
                            <div
                              className={[
                                styles.historyThreadSurface,
                                isActive ? styles.historyItemActive : "",
                                isMenuOpen ? styles.historyMenuOpen : "",
                              ]
                                .filter(Boolean)
                                .join(" ")}
                              title={thread.title}
                              data-mode={isCompare ? "compare" : "ask"}
                            >
                              {isRenaming ? (
                                <div className={styles.historyRenameRow}>
                                  <input
                                    className={styles.historyRenameInput}
                                    value={renameDraft}
                                    onChange={(event) => setRenameDraft(event.target.value)}
                                    onBlur={() => void finishRename(thread)}
                                    onKeyDown={(event) => {
                                      if (event.key === "Enter") {
                                        event.preventDefault();
                                        void finishRename(thread);
                                      } else if (event.key === "Escape") {
                                        event.preventDefault();
                                        event.stopPropagation();
                                        cancelRename(thread.key);
                                      }
                                    }}
                                    aria-label={`Rename ${thread.title}`}
                                    maxLength={120}
                                    autoFocus
                                  />
                                </div>
                              ) : (
                                <button
                                  type="button"
                                  ref={(element) => {
                                    if (element) rowButtonRefs.current.set(thread.key, element);
                                    else rowButtonRefs.current.delete(thread.key);
                                  }}
                                  className={styles.historySelectButton}
                                  data-history-thread={thread.key}
                                  data-session-id={thread.sessionId}
                                  aria-label={`${thread.title}. ${modeLabel}, ${
                                    dateTimeLabel || "Date unavailable"
                                  }`}
                                  aria-current={isActive ? "page" : undefined}
                                  onClick={() => onSelectThread(thread)}
                                >
                                  <span className={styles.historyTitle} data-history-title>
                                    {thread.title}
                                  </span>
                                  <span className={styles.historyRight}>
                                    <span className={styles.historyMeta}>
                                      {modeLabel.toUpperCase()} ·{" "}
                                      <time dateTime={thread.latestTimestamp}>
                                        {timeLabel || "--:--"}
                                      </time>
                                    </span>
                                  </span>
                                </button>
                              )}
                              {!isRenaming && (
                                <button
                                  type="button"
                                  className={styles.historyMoreButton}
                                  data-chat-more
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    clearDeleteConfirm();
                                    setOpenMenuKey((current) =>
                                      current === thread.key ? null : thread.key,
                                    );
                                  }}
                                  aria-label={`Chat options for ${thread.title}`}
                                  aria-haspopup="menu"
                                  aria-expanded={isMenuOpen}
                                  title="Chat options"
                                >
                                  <KebabIcon />
                                </button>
                              )}
                              {isMenuOpen && (
                                <div
                                  ref={menuRef}
                                  className={styles.historyMenu}
                                  role="menu"
                                  aria-label={`Options for ${thread.title}`}
                                >
                                  <button
                                    type="button"
                                    className={styles.historyMenuItem}
                                    role="menuitem"
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      beginRename(thread);
                                    }}
                                  >
                                    <PencilIcon />
                                    Rename
                                    <span className={styles.historyMenuKey} aria-hidden="true">
                                      R
                                    </span>
                                  </button>
                                  <div className={styles.historyMenuSeparator} />
                                  <button
                                    type="button"
                                    className={`${styles.historyMenuItem} ${styles.historyMenuDanger}`}
                                    role="menuitem"
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      beginDelete(thread);
                                    }}
                                  >
                                    <TrashIcon />
                                    Delete
                                    <span className={styles.historyMenuKey} aria-hidden="true">
                                      D
                                    </span>
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </li>
              ))}
              {filteredThreads.length === 0 && (
                <li className={styles.historyEmpty}>
                  {historySearch.trim() ? "No chats match" : "No recent chats"}
                </li>
              )}
            </ul>
          </>
        )}
      </div>

      {canSignInFromProfile ? (
        <button
          type="button"
          className={`${styles.profile} ${styles.profileInteractive}`}
          onClick={onLogin}
          aria-label="Sign in"
          title={isCollapsed ? userLabel : undefined}
        >
          <SessionProfileContent sessionLabel={sessionLabel} sessionStatus={sessionStatus} />
        </button>
      ) : (
        <div
          className={styles.profile}
          aria-label={`${sessionLabel}. ${sessionStatus}`}
          title={isCollapsed ? userLabel : undefined}
        >
          <SessionProfileContent sessionLabel={sessionLabel} sessionStatus={sessionStatus} />
        </div>
      )}
    </aside>
  );
}

function SessionProfileContent({
  sessionLabel,
  sessionStatus,
}: {
  sessionLabel: string;
  sessionStatus: string;
}) {
  return (
    <>
      <span className={styles.sessionDot} aria-hidden="true">
        <span />
      </span>
      <span className={styles.profileText}>
        <strong>{sessionLabel}</strong>
        <span>{sessionStatus}</span>
      </span>
    </>
  );
}

function groupHistoryThreads(threads: HistoryThread[]): HistoryDateGroup[] {
  const groups = new Map<string, HistoryDateGroup>();

  for (const thread of threads) {
    const label = formatHistoryGroupLabel(thread.latestTimestamp);
    const key = label || "unknown";
    const group = groups.get(key) ?? { key, label: label || "Date unavailable", threads: [] };
    group.threads.push(thread);
    groups.set(key, group);
  }

  return [...groups.values()];
}

function formatHistoryGroupLabel(value: string, now = new Date()): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "";

  const date = new Date(timestamp);
  if (isSameLocalDate(date, now)) return "Today";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (isSameLocalDate(date, yesterday)) return "Yesterday";

  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

function formatHistoryTime(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "";

  return new Date(timestamp).toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
}

function isSameLocalDate(left: Date, right: Date): boolean {
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  );
}

function formatSessionId(value: string): string {
  const normalized = value.trim();
  if (normalized.length <= 14) return normalized;
  return `${normalized.slice(0, 8)}...${normalized.slice(-4)}`;
}

function FilterIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M4 7h16M7 12h10M10 17h4" />
    </svg>
  );
}

function KebabIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="5" r="1.55" />
      <circle cx="12" cy="12" r="1.55" />
      <circle cx="12" cy="19" r="1.55" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 20h8M16.5 4.5a2.12 2.12 0 0 1 3 3L8 19l-4 1 1-4z" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 6h18M8 6V4h8v2m-9 0 1 14h8l1-14" />
    </svg>
  );
}
