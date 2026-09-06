import type { WorkArtifact } from "../../types";
import { workArtifactDownloadUrl } from "../../api/work";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./Work.module.css";

export function WorkArtifacts({ runId, artifacts }: { runId: string; artifacts: WorkArtifact[] }) {
  if (artifacts.length === 0) return null;
  return (
    <section className={styles.artifactsSection} aria-labelledby="work-deliverables-title">
      <h3 id="work-deliverables-title">Deliverables</h3>
      <div className={styles.artifactList}>
        {artifacts.map((artifact) => {
          const downloadUrl = workArtifactDownloadUrl(runId, artifact.file_id);
          return (
            <article className={styles.artifactRow} key={artifact.id}>
              <span className={styles.artifactIcon} aria-hidden="true">
                <CortexIcon name={artifact.mime_type.includes("sheet") ? "table" : "summarize"} />
              </span>
              <span className={styles.artifactCopy}>
                <strong>{artifact.filename}</strong>
                <span>{artifactKind(artifact.mime_type)} · {formatBytes(artifact.size_bytes)}</span>
              </span>
              <a className={styles.outlineButton} href={`${downloadUrl}?inline=1`} target="_blank" rel="noreferrer">
                Open
              </a>
              <a className={styles.iconOutlineButton} href={downloadUrl} aria-label={`Download ${artifact.filename}`}>
                <CortexIcon name="download" size={17} />
              </a>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function artifactKind(mimeType: string): string {
  if (mimeType.includes("pdf")) return "PDF";
  if (mimeType.includes("sheet") || mimeType.includes("excel")) return "Spreadsheet";
  if (mimeType.includes("json")) return "JSON";
  if (mimeType.startsWith("text/")) return "Text";
  return "File";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
