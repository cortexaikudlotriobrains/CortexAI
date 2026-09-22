import { useEffect, useMemo, useRef } from "react";
import { deleteFile } from "../../api/files";
import { getAttachmentUploadMode } from "../../config/runtimeConfig";
import { useChatStore } from "../../store/chatStore";
import { useAttachmentUploadStore } from "../../store/attachmentUploadStore";
import type { AttachmentUploadTask } from "../../store/attachmentUploadStore";
import {
  beginAttachmentUploads,
  removeAttachmentUpload,
  retryAttachmentUpload,
} from "../../uploads/attachmentUploadQueue";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./AttachmentStrip.module.css";
import type {
  BillingPlansResponse,
  EntitlementsResponse,
  FileUploadResponse,
} from "../../types";
import { fileSelectionAccessError } from "../../subscription/subscriptionAccess";
import {
  isSubscriptionDenial,
  toSubscriptionError,
} from "../../subscription/subscriptionErrors";

const ACCEPTED =
  ".jpg,.jpeg,.png,.webp,.gif,.pdf,.txt,.csv,.json,.docx,.pptx,.xlsx";

export function AttachmentStrip({
  entitlements = null,
  plans = null,
}: {
  entitlements?: EntitlementsResponse | null;
  plans?: BillingPlansResponse | null;
}) {
  const attachments = useChatStore((state) => state.attachments);
  const addAttachment = useChatStore((state) => state.addAttachment);
  const removeAttachment = useChatStore((state) => state.removeAttachment);
  const setError = useChatStore((state) => state.setError);
  const setSubscriptionError = useChatStore((state) => state.setSubscriptionError);
  const mode = useChatStore((state) => state.mode);
  const smartMode = useChatStore((state) => state.smartMode);
  const selectedModelKey = useChatStore((state) => state.selectedModelKey);
  const tasks = useAttachmentUploadStore((state) => state.tasks);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    for (const task of tasks) {
      if (task.state === "ready" && task.serverFile) addAttachment(task.serverFile);
    }
  }, [addAttachment, tasks]);

  const taskFileIds = useMemo(
    () => new Set(tasks.map((task) => task.fileId).filter(Boolean)),
    [tasks],
  );
  const standaloneAttachments = attachments.filter(
    (attachment) => !taskFileIds.has(attachment.file_id),
  );
  const selectedCount = tasks.length + standaloneAttachments.length;

  const handleFiles = async (files: FileList | null) => {
    if (!files) return;
    const selectedFiles = Array.from(files);
    const accessError = fileSelectionAccessError(
      selectedFiles,
      selectedCount,
      entitlements,
      plans,
    );
    if (accessError) {
      setSubscriptionError(accessError);
      resetFileInput();
      return;
    }

    const target = selectedUploadTarget(mode, smartMode, selectedModelKey);
    try {
      setError(null);
      setSubscriptionError(null);
      await beginAttachmentUploads(selectedFiles, {
        mode: getAttachmentUploadMode(),
        target,
      });
    } catch (error) {
      const subscriptionError = toSubscriptionError(error, "Upload failed");
      if (isSubscriptionDenial(subscriptionError)) {
        setSubscriptionError(subscriptionError);
      } else {
        setError("Upload could not be prepared. Retry the upload.");
      }
    } finally {
      resetFileInput();
    }
  };

  const handleTaskRemove = (task: AttachmentUploadTask) => {
    if (task.fileId) removeAttachment(task.fileId);
    void removeAttachmentUpload(task.clientId);
  };

  const handleServerFileRemove = (file: FileUploadResponse) => {
    removeAttachment(file.file_id);
    void deleteFile(file.file_id).catch(() => undefined);
  };

  const handleRetry = (task: AttachmentUploadTask) => {
    setError(null);
    void retryAttachmentUpload(task.clientId).catch((error) => {
      const subscriptionError = toSubscriptionError(error, "Upload failed");
      if (isSubscriptionDenial(subscriptionError)) {
        setSubscriptionError(subscriptionError);
      } else {
        setError("Upload could not be prepared. Retry the upload.");
      }
    });
  };

  const resetFileInput = () => {
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  return (
    <div id="attachmentStrip" className={styles.strip}>
      {selectedCount > 0 && (
        <ul className={styles.list} aria-live="polite" aria-label="Selected attachments">
          {tasks.map((task) => (
            <UploadTaskChip
              key={task.clientId}
              task={task}
              onRemove={() => handleTaskRemove(task)}
              onRetry={() => handleRetry(task)}
            />
          ))}
          {standaloneAttachments.map((attachment) => (
            <ServerAttachmentChip
              key={attachment.file_id}
              attachment={attachment}
              onRemove={() => handleServerFileRemove(attachment)}
            />
          ))}
        </ul>
      )}

      <div className={styles.addWrap}>
        <button
          type="button"
          className={styles.addBtn}
          aria-label="Attach files"
          onClick={() => fileInputRef.current?.click()}
        >
          <CortexIcon name="attach" />
        </button>
      </div>

      <input
        ref={fileInputRef}
        id="attachmentInput"
        type="file"
        multiple
        accept={ACCEPTED}
        className={styles.hiddenInput}
        onChange={(event) => void handleFiles(event.target.files)}
      />
    </div>
  );
}

function UploadTaskChip({
  task,
  onRemove,
  onRetry,
}: {
  task: AttachmentUploadTask;
  onRemove: () => void;
  onRetry: () => void;
}) {
  const status = taskStatus(task);
  return (
    <li
      className={styles.item}
      aria-label={`${task.filename}, ${status.accessibleLabel}`}
      title={task.error}
    >
      <span className={`attachment-chip is-${task.state}`} aria-hidden="true" />
      <span className={styles.fileName} title={task.filename}>
        {task.filename}
      </span>
      <span className={styles.fileSize}>{formatSize(task.sizeBytes)}</span>
      <span
        className={`${styles.status} ${task.state === "failed" ? styles.failedStatus : ""}`}
        role={task.state === "uploading" ? "progressbar" : "status"}
        aria-label={status.accessibleLabel}
        aria-valuemin={task.state === "uploading" ? 0 : undefined}
        aria-valuemax={task.state === "uploading" ? 100 : undefined}
        aria-valuenow={task.state === "uploading" ? task.progress : undefined}
      >
        {status.visibleLabel}
      </span>
      {task.state === "failed" ? (
        <button
          type="button"
          className={styles.retryBtn}
          aria-label={`Retry ${task.filename}`}
          onClick={onRetry}
        >
          Retry
        </button>
      ) : null}
      <RemoveButton filename={task.filename} onClick={onRemove} />
    </li>
  );
}

function ServerAttachmentChip({
  attachment,
  onRemove,
}: {
  attachment: FileUploadResponse;
  onRemove: () => void;
}) {
  const label = attachment.status === "ready" ? "Ready" : attachment.status;
  return (
    <li
      className={styles.item}
      aria-label={`${attachment.original_filename}, ${label}`}
    >
      <span className={`attachment-chip is-${attachment.status}`} aria-hidden="true" />
      <span className={styles.fileName} title={attachment.original_filename}>
        {attachment.original_filename}
      </span>
      <span className={styles.fileSize}>{formatSize(attachment.size_bytes)}</span>
      <span className={styles.status}>{label}</span>
      <RemoveButton filename={attachment.original_filename} onClick={onRemove} />
    </li>
  );
}

function RemoveButton({ filename, onClick }: { filename: string; onClick: () => void }) {
  return (
    <button
      type="button"
      className={styles.removeBtn}
      aria-label={`Remove ${filename}`}
      onClick={onClick}
    >
      &times;
    </button>
  );
}

function taskStatus(task: AttachmentUploadTask): {
  visibleLabel: string;
  accessibleLabel: string;
} {
  if (task.state === "authorizing") {
    return { visibleLabel: "Preparing…", accessibleLabel: "preparing upload" };
  }
  if (task.state === "uploading") {
    return {
      visibleLabel: `Uploading ${task.progress}%`,
      accessibleLabel: `uploading, ${task.progress} percent`,
    };
  }
  if (task.state === "processing") {
    return { visibleLabel: "Processing…", accessibleLabel: "processing" };
  }
  if (task.state === "ready") {
    return { visibleLabel: "Ready", accessibleLabel: "ready" };
  }
  if (task.state === "failed") {
    return {
      visibleLabel: "Upload failed",
      accessibleLabel: task.error ? `upload failed, ${task.error}` : "upload failed",
    };
  }
  return { visibleLabel: "Cancelled", accessibleLabel: "cancelled" };
}

function selectedUploadTarget(
  mode: "single" | "compare",
  smartMode: boolean,
  selectedModelKey: string,
): { provider: string; model: string } | undefined {
  const separatorIndex = selectedModelKey.indexOf(":");
  if (mode !== "single" || smartMode || separatorIndex <= 0) return undefined;
  return {
    provider: selectedModelKey.slice(0, separatorIndex),
    model: selectedModelKey.slice(separatorIndex + 1),
  };
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
