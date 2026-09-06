import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import type { AttachmentUploadTask } from "../../store/attachmentUploadStore";
import type { ToolCatalogItem, ToolConnection, WorkWebMode } from "../../types";
import { formatAiCredits } from "../../utils/aiCredits";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./Work.module.css";

const POPOVER_GAP = 9;
const POPOVER_VIEWPORT_MARGIN = 16;
const TOOLS_POPOVER_MAX_HEIGHT = 520;
const TOOLS_POPOVER_WIDTH = 302;

interface WorkComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onFiles: (files: File[]) => void;
  onRemoveFile: (clientId: string) => void;
  onRetryFile: (clientId: string) => void;
  tasks: AttachmentUploadTask[];
  connections: ToolConnection[];
  catalog: ToolCatalogItem[];
  enabledConnectionIds: string[];
  onToggleConnection: (id: string) => void;
  onConnect: (connectorKey: string) => void;
  onAddMcp: (name: string, url: string) => Promise<void>;
  webMode: WorkWebMode;
  onWebModeChange: (mode: WorkWebMode) => void;
  maxCreditBudget: number;
  maxPlanBudget: number;
  onBudgetChange: (value: number) => void;
  busy?: boolean;
  disabled?: boolean;
  followup?: boolean;
}

export function WorkComposer({
  value,
  onChange,
  onSubmit,
  onFiles,
  onRemoveFile,
  onRetryFile,
  tasks,
  connections,
  catalog,
  enabledConnectionIds,
  onToggleConnection,
  onConnect,
  onAddMcp,
  webMode,
  onWebModeChange,
  maxCreditBudget,
  maxPlanBudget,
  onBudgetChange,
  busy,
  disabled,
  followup,
}: WorkComposerProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const toolsButtonRef = useRef<HTMLButtonElement>(null);
  const toolsPopoverRef = useRef<HTMLDivElement>(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [toolsPopoverStyle, setToolsPopoverStyle] = useState<CSSProperties>({});
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [mcpName, setMcpName] = useState("");
  const [mcpUrl, setMcpUrl] = useState("");
  const [mcpBusy, setMcpBusy] = useState(false);
  const ready = tasks.every((task) => task.state === "ready" || task.state === "cancelled");
  const canSubmit = Boolean(value.trim()) && ready && !busy && !disabled;
  const connectedCount = enabledConnectionIds.length;
  const currentInformationPrompt = looksLikeCurrentInformation(value);
  const webLabel = webMode === "auto" ? "Auto" : webMode === "on" ? "On" : "Off";

  const updateToolsPopoverPosition = useCallback(() => {
    const button = toolsButtonRef.current;
    const popover = toolsPopoverRef.current;
    if (!button || !popover) return;

    const triggerRect = button.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const width = Math.min(
      TOOLS_POPOVER_WIDTH,
      Math.max(0, viewportWidth - POPOVER_VIEWPORT_MARGIN * 2),
    );
    const naturalHeight = Math.min(
      popover.scrollHeight,
      TOOLS_POPOVER_MAX_HEIGHT,
      Math.max(0, viewportHeight - POPOVER_VIEWPORT_MARGIN * 2),
    );
    const availableAbove = Math.max(
      0,
      triggerRect.top - POPOVER_GAP - POPOVER_VIEWPORT_MARGIN,
    );
    const availableBelow = Math.max(
      0,
      viewportHeight - triggerRect.bottom - POPOVER_GAP - POPOVER_VIEWPORT_MARGIN,
    );
    const openAbove =
      availableAbove >= naturalHeight || availableAbove >= availableBelow;
    const availableHeight = openAbove ? availableAbove : availableBelow;
    const maxHeight = Math.min(
      TOOLS_POPOVER_MAX_HEIGHT,
      Math.floor(viewportHeight * 0.7),
      availableHeight,
    );
    const renderedHeight = Math.min(naturalHeight, maxHeight);
    const left = Math.min(
      Math.max(POPOVER_VIEWPORT_MARGIN, triggerRect.left),
      Math.max(
        POPOVER_VIEWPORT_MARGIN,
        viewportWidth - width - POPOVER_VIEWPORT_MARGIN,
      ),
    );
    const top = openAbove
      ? triggerRect.top - POPOVER_GAP - renderedHeight
      : triggerRect.bottom + POPOVER_GAP;

    setToolsPopoverStyle({
      position: "fixed",
      zIndex: 80,
      top: Math.max(POPOVER_VIEWPORT_MARGIN, top),
      right: "auto",
      bottom: "auto",
      left,
      width,
      maxHeight,
    });
  }, []);

  useLayoutEffect(() => {
    if (!toolsOpen) return;

    updateToolsPopoverPosition();
    const frame = window.requestAnimationFrame(updateToolsPopoverPosition);
    window.addEventListener("resize", updateToolsPopoverPosition);
    window.addEventListener("scroll", updateToolsPopoverPosition, true);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", updateToolsPopoverPosition);
      window.removeEventListener("scroll", updateToolsPopoverPosition, true);
    };
  }, [catalog.length, connections.length, mcpOpen, toolsOpen, updateToolsPopoverPosition]);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (canSubmit) onSubmit();
    }
  };

  return (
    <div className={`${styles.composer} ${followup ? styles.composerFollowup : styles.composerStart}`}>
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={followup ? "Ask Cortex to refine the work..." : "Describe what you want Cortex to accomplish..."}
        aria-label={followup ? "Refine this work" : "Work goal"}
        disabled={disabled || busy}
      />
      {tasks.length > 0 && (
        <div className={styles.attachmentStrip} aria-label="Work attachments">
          {tasks.map((task) => (
            <span className={styles.attachmentItem} key={task.clientId}>
              <CortexIcon name="attach" size={14} />
              <span>{task.filename}</span>
              <small>{task.state === "uploading" ? `${task.progress}%` : task.state}</small>
              {task.state === "failed" && <button type="button" onClick={() => onRetryFile(task.clientId)}>Retry</button>}
              <button type="button" aria-label={`Remove ${task.filename}`} onClick={() => onRemoveFile(task.clientId)}>×</button>
            </span>
          ))}
        </div>
      )}
      <div className={styles.composerFooter}>
        <div className={styles.chipRow}>
          <input
            ref={inputRef}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              onFiles(Array.from(event.target.files || []));
              event.target.value = "";
            }}
          />
          <button type="button" className={styles.workChip} onClick={() => inputRef.current?.click()} disabled={disabled}>
            <CortexIcon name="attach" size={15} /> Files {tasks.length > 0 && <b>{tasks.length}</b>}
          </button>
          <button type="button" className={`${styles.workChip} ${webMode !== "off" ? styles.workChipActive : ""}`} onClick={() => onWebModeChange(nextWebMode(webMode))} disabled={disabled} aria-label={`Web access: ${webLabel}. Activate to change mode.`}>
            <CortexIcon name="web" size={15} /> Web · {webLabel}{webMode === "on" && " ✓"}
          </button>
          <div className={styles.popoverAnchor}>
            <button ref={toolsButtonRef} type="button" className={`${styles.workChip} ${connectedCount ? styles.workChipActive : ""}`} onClick={() => { setToolsOpen((open) => !open); setSettingsOpen(false); }} aria-haspopup="dialog" aria-expanded={toolsOpen} disabled={disabled}>
              <CortexIcon name="tools" size={15} /> Tools {connectedCount > 0 && <b>{connectedCount}</b>} <CortexIcon name="chevron-down" size={12} />
            </button>
            {toolsOpen && typeof document !== "undefined" && createPortal(
              <div ref={toolsPopoverRef} className={styles.toolsPopover} style={toolsPopoverStyle} role="dialog" aria-label="Tools">
                <div className={styles.popoverHeader}><strong>Tools</strong><button type="button" aria-label="Close tools" onClick={() => setToolsOpen(false)}>×</button></div>
                <p className={styles.popoverEyebrow}>Connected</p>
                {connections.length === 0 ? <p className={styles.popoverEmpty}>No connected apps yet.</p> : connections.map((connection) => (
                  <label className={styles.toolRow} key={connection.id}>
                    <span className={styles.toolTile}>{connection.display_name.slice(0, 1)}</span>
                    <span><strong>{connection.display_name}</strong><small>{connection.status}</small></span>
                    <input type="checkbox" checked={enabledConnectionIds.includes(connection.id)} onChange={() => onToggleConnection(connection.id)} />
                  </label>
                ))}
                <p className={styles.popoverEyebrow}>Available apps</p>
                {catalog.filter((item) => !["cortex_files", "cortex_web", "custom_mcp"].includes(item.connector_key) && !connections.some((connection) => connection.connector_key === item.connector_key)).map((item) => (
                  <div className={styles.toolRow} key={item.connector_key}>
                    <span className={styles.toolTile}>{item.display_name.slice(0, 1)}</span>
                    <span><strong>{item.display_name}</strong><small>{item.configuration_required ? "Setup required" : item.description}</small></span>
                    <button type="button" className={styles.connectButton} onClick={() => onConnect(item.connector_key)} disabled={item.configuration_required}>Connect</button>
                  </div>
                ))}
                {mcpOpen ? (
                  <form className={styles.mcpForm} onSubmit={(event) => { event.preventDefault(); setMcpBusy(true); void onAddMcp(mcpName, mcpUrl).then(() => { setMcpName(""); setMcpUrl(""); setMcpOpen(false); }).finally(() => setMcpBusy(false)); }}>
                    <label>Name<input value={mcpName} onChange={(event) => setMcpName(event.target.value)} required maxLength={120} /></label>
                    <label>HTTPS endpoint<input value={mcpUrl} onChange={(event) => setMcpUrl(event.target.value)} required type="url" placeholder="https://mcp.example.com/mcp" /></label>
                    <button type="submit" className={styles.inkButton} disabled={mcpBusy}>Add server</button>
                  </form>
                ) : (
                  <button type="button" className={styles.mcpLink} onClick={() => setMcpOpen(true)}><CortexIcon name="plus" size={14} /> Add MCP server</button>
                )}
              </div>,
              document.body,
            )}
          </div>
        </div>
        <div className={styles.composerActions}>
          <div className={styles.popoverAnchor}>
            <button type="button" className={styles.settingsButton} aria-label="Work settings" onClick={() => { setSettingsOpen((open) => !open); setToolsOpen(false); }} aria-haspopup="dialog" aria-expanded={settingsOpen} disabled={disabled}>
              <CortexIcon name="settings" size={17} />
            </button>
            {settingsOpen && (
              <div className={`${styles.settingsPopover} ${styles.popoverRight}`} role="dialog" aria-label="Work settings">
                <div className={styles.popoverHeader}><strong>Work settings</strong><button type="button" aria-label="Close settings" onClick={() => setSettingsOpen(false)}>×</button></div>
                <p className={styles.popoverEyebrow}>Agent</p>
                <label className={styles.agentChoice}><input type="radio" checked readOnly /><span><strong>Cortex Auto</strong><small>Picks the right model for each step</small></span></label>
                <p className={styles.popoverEyebrow}>Maximum task budget</p>
                <div className={styles.budgetOptions}>
                  {[25_000, 100_000, 250_000, 1_000_000].filter((option) => option <= maxPlanBudget).map((option) => (
                    <button type="button" key={option} className={option === maxCreditBudget ? styles.budgetOptionActive : ""} onClick={() => onBudgetChange(option)}>{formatBudget(option)}</button>
                  ))}
                </div>
                <p className={styles.settingsNote}>Cortex stops before it would exceed this budget.</p>
              </div>
            )}
          </div>
          <button type="button" className={followup ? styles.followupSend : styles.startButton} onClick={onSubmit} disabled={!canSubmit}>
            {followup ? <CortexIcon name="send" size={17} /> : <>Start work <CortexIcon name="send" size={16} /></>}
          </button>
        </div>
      </div>
      {webMode === "off" && currentInformationPrompt && (
        <p className={styles.webWarning} role="status">
          This request appears to need current information, but Web is explicitly off.
        </p>
      )}
    </div>
  );
}

function nextWebMode(mode: WorkWebMode): WorkWebMode {
  if (mode === "auto") return "on";
  if (mode === "on") return "off";
  return "auto";
}

function looksLikeCurrentInformation(value: string): boolean {
  return /\b(latest|recent|today|current|up[ -]to[ -]date|this (?:week|month|year)|opening hours?|business hours?|ticket prices?|live (?:price|availability|status|schedule)|weather(?: forecast)?|source links?|verify (?:online|on the web|with sources))\b/i.test(value);
}

function formatBudget(value: number): string {
  return `${formatAiCredits(value)} credits`;
}
