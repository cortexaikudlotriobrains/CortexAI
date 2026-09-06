import { useState } from "react";
import type { WorkApproval as WorkApprovalType } from "../../types";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./Work.module.css";

interface WorkApprovalProps {
  approval: WorkApprovalType;
  busy?: boolean;
  onApprove: (remember: boolean) => void;
  onDeny: () => void;
}

export function WorkApproval({ approval, busy, onApprove, onDeny }: WorkApprovalProps) {
  const [remember, setRemember] = useState(false);
  const details = Object.entries(approval.request_payload).slice(0, 4);
  const canRemember = approval.action_type === "WRITE" && approval.connection_id !== null;
  return (
    <section className={styles.approvalCard} aria-live="assertive">
      <div className={styles.approvalStrip}>
        <CortexIcon name="alert" size={17} />
        <span>Approval needed</span>
      </div>
      <div className={styles.approvalBody}>
        <h3>Cortex wants to use {humanize(approval.tool_name)}</h3>
        <p>{approval.description}</p>
        <dl className={styles.approvalDetails}>
          <div>
            <dt>Action</dt>
            <dd>{humanize(approval.action_type)}</dd>
          </div>
          <div>
            <dt>Tool</dt>
            <dd>{approval.tool_name}</dd>
          </div>
          {details.map(([key, value]) => (
            <div key={key}>
              <dt>{humanize(key)}</dt>
              <dd>{formatValue(value)}</dd>
            </div>
          ))}
        </dl>
        <div className={styles.approvalFooter}>
          {canRemember ? <label className={styles.rememberApproval}>
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            <span>Always allow this write action in this Work session</span>
          </label> : <span />}
          <div className={styles.approvalActions}>
            <button type="button" className={styles.outlineButton} onClick={onDeny} disabled={busy}>
              Don&apos;t allow
            </button>
            <button type="button" className={styles.inkButton} onClick={() => onApprove(remember)} disabled={busy}>
              Approve
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value).slice(0, 220);
}
