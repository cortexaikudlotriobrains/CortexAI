import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listWorkSessions } from "../../api/work";
import { getRuntimeConfig } from "../../config/runtimeConfig";
import { loadCompleteHistoryThread } from "../../history/loadHistoryThread";
import { presentError } from "../../errors/userFacingError";
import { useChat } from "../../hooks/useChat";
import { useHistory } from "../../hooks/useHistory";
import { useChatStore } from "../../store/chatStore";
import { useWorkStore } from "../../store/workStore";
import type { ChatMode, HistoryThread, WorkSession } from "../../types";
import { Sidebar, type SidebarView } from "./Sidebar";

interface WorkspaceSidebarProps {
  activeView: SidebarView;
  authLoading: boolean;
  authEnabled: boolean;
  loggedIn: boolean;
  restoreActiveTranscript?: boolean;
  onChatThreadSelected?: () => void;
  newLabel?: "New chat" | "New work";
  onNew?: () => void;
  workSessions?: WorkSession[];
  activeWorkSessionId?: string | null;
  onSelectWorkSession?: (session: WorkSession) => void;
}

export function WorkspaceSidebar({
  activeView,
  authLoading,
  authEnabled,
  loggedIn,
  restoreActiveTranscript = false,
  onChatThreadSelected,
  newLabel = "New chat",
  onNew,
  workSessions,
  activeWorkSessionId,
  onSelectWorkSession,
}: WorkspaceSidebarProps) {
  const navigate = useNavigate();
  const { cancel } = useChat();
  const { load: loadHistory } = useHistory();
  const hydrateFromHistoryThread = useChatStore((state) => state.hydrateFromHistoryThread);
  const setError = useChatStore((state) => state.setError);
  const setMode = useChatStore((state) => state.setMode);
  const startNewChat = useChatStore((state) => state.startNewChat);
  const rememberedWorkSessionId = useWorkStore((state) => state.session?.id);
  const [loadedWorkSessions, setLoadedWorkSessions] = useState<WorkSession[]>([]);
  const signedOut = !authLoading && authEnabled && !loggedIn;
  const workspaceReady = !authLoading && !signedOut;
  const workEnabled = getRuntimeConfig().workEnabled !== false;
  const modelsCatalogEnabled = getRuntimeConfig().modelsCatalogEnabled === true;
  const usesProvidedWorkSessions = workSessions !== undefined;
  const workDestinationSessionId = activeWorkSessionId ?? rememberedWorkSessionId;
  const workDestination = workDestinationSessionId
    ? `/work/${encodeURIComponent(workDestinationSessionId)}`
    : "/work";

  useEffect(() => {
    if (!workspaceReady) return;
    void loadHistory({ restoreActiveTranscript });
  }, [loadHistory, restoreActiveTranscript, workspaceReady]);

  useEffect(() => {
    if (!workspaceReady || !workEnabled) {
      setLoadedWorkSessions([]);
      return;
    }
    if (usesProvidedWorkSessions) return;

    let cancelled = false;
    void listWorkSessions()
      .then((sessions) => {
        if (!cancelled) setLoadedWorkSessions(sessions);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [usesProvidedWorkSessions, workEnabled, workspaceReady]);

  const openChatMode = (mode: ChatMode) => {
    setMode(mode);
    navigate(mode === "compare" ? "/?mode=compare" : "/");
  };

  const handleSelectHistoryThread = async (thread: HistoryThread) => {
    try {
      const loaded = await loadCompleteHistoryThread(thread);
      hydrateFromHistoryThread(loaded.thread, loaded.analysisRuns);
      onChatThreadSelected?.();
      if (activeView !== "chat") navigate("/");
    } catch (historyError) {
      setError(presentError(historyError, "history_load"));
      if (activeView !== "chat") navigate("/");
    }
  };

  const handleNew = () => {
    if (newLabel === "New work" && onNew) {
      onNew();
      return;
    }
    cancel();
    startNewChat();
    if (activeView !== "chat") setMode("single");
    onNew?.();
    if (activeView !== "chat") navigate("/");
  };

  return (
    <Sidebar
      onSelectThread={(thread) => void handleSelectHistoryThread(thread)}
      activeView={activeView}
      onNavigateChat={openChatMode}
      onNavigateWork={workEnabled ? () => navigate(workDestination) : undefined}
      onNavigateUsage={() => navigate("/usage")}
      onNavigateCredits={() => navigate("/credits")}
      onNavigateModels={modelsCatalogEnabled ? () => navigate("/models") : undefined}
      signedOut={signedOut}
      newLabel={newLabel}
      onNew={handleNew}
      workSessions={workSessions ?? loadedWorkSessions}
      activeWorkSessionId={activeWorkSessionId}
      onSelectWorkSession={onSelectWorkSession ?? ((session) => navigate(`/work/${session.id}`))}
    />
  );
}
