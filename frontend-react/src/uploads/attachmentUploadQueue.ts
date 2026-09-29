import { ApiClientError } from "../api/client";
import {
  completeFileUpload,
  createUploadIntents,
  deleteFile,
  fetchFileStatus,
  uploadFiles,
} from "../api/files";
import type { FileUploadTarget } from "../api/files";
import { getAttachmentUploadMode } from "../config/runtimeConfig";
import {
  findAttachmentUploadTask,
  useAttachmentUploadStore,
} from "../store/attachmentUploadStore";
import type {
  AttachmentUploadFailureStage,
  AttachmentUploadTask,
} from "../store/attachmentUploadStore";
import type { FileUploadResponse, PresignedPost } from "../types";
import { S3UploadError, uploadFileDirectlyToS3 } from "./directS3Upload";

interface BeginAttachmentUploadOptions {
  target?: FileUploadTarget;
  mode?: "direct" | "legacy" | "disabled";
}

interface ClearAttachmentUploadOptions {
  deleteRemote: boolean;
}

const DEFAULT_MAX_CONCURRENCY = 2;
const DEFAULT_POLL_DELAYS_MS = [800, 1_200, 1_800, 2_500, 3_500, 5_000];

let maxConcurrency = DEFAULT_MAX_CONCURRENCY;
let pollDelaysMs = [...DEFAULT_POLL_DELAYS_MS];
let pendingTransfers: string[] = [];
let activeTransferCount = 0;
const uploadAuthorizations = new Map<string, PresignedPost>();
const activeControllers = new Map<string, AbortController>();
const pollingTasks = new Set<string>();

export async function beginAttachmentUploads(
  files: File[],
  options: BeginAttachmentUploadOptions = {},
): Promise<string[]> {
  if (files.length === 0) return [];

  const mode = options.mode ?? getAttachmentUploadMode();
  const tasks = files.map((file) => createTask(file, mode, options.target));
  useAttachmentUploadStore.getState().addTasks(tasks);
  const clientIds = tasks.map((task) => task.clientId);

  if (mode === "disabled") {
    const error = new Error("File uploads are unavailable in this environment.");
    failTasks(clientIds, "authorization", error);
    throw error;
  }

  try {
    if (mode === "legacy") {
      await authorizeLegacy(clientIds, options.target);
    } else {
      await authorizeDirect(clientIds, options.target);
    }
  } catch (error) {
    failTasks(clientIds, "authorization", error);
    throw error;
  }
  return clientIds;
}

export async function retryAttachmentUpload(clientId: string): Promise<void> {
  const task = findAttachmentUploadTask(clientId);
  if (!task || task.state === "ready" || task.state === "cancelled") return;

  if (task.failureStage === "completion" && task.fileId && !requiresFreshIntent(task.errorCode)) {
    patchTask(clientId, {
      state: "processing",
      error: undefined,
      errorCode: undefined,
      failureStage: undefined,
    });
    await completeAndPoll(clientId);
    return;
  }

  activeControllers.get(clientId)?.abort();
  pendingTransfers = pendingTransfers.filter((id) => id !== clientId);
  uploadAuthorizations.delete(clientId);
  if (task.fileId) await deleteFile(task.fileId).catch(() => undefined);

  patchTask(clientId, {
    fileId: undefined,
    serverFile: undefined,
    state: "authorizing",
    progress: 0,
    error: undefined,
    errorCode: undefined,
    failureStage: undefined,
    retryCount: 0,
  });

  try {
    if (task.uploadMode === "legacy") {
      await authorizeLegacy([clientId], task.target);
    } else if (task.uploadMode === "direct") {
      await authorizeDirect([clientId], task.target);
    } else {
      throw new Error("File uploads are unavailable in this environment.");
    }
  } catch (error) {
    failTasks([clientId], "authorization", error);
    throw error;
  }
}

export async function removeAttachmentUpload(clientId: string): Promise<void> {
  const task = findAttachmentUploadTask(clientId);
  if (!task) return;

  patchTask(clientId, { state: "cancelled" });
  activeControllers.get(clientId)?.abort();
  activeControllers.delete(clientId);
  uploadAuthorizations.delete(clientId);
  pollingTasks.delete(clientId);
  pendingTransfers = pendingTransfers.filter((id) => id !== clientId);
  useAttachmentUploadStore.getState().removeTask(clientId);
  if (task.fileId) await deleteFile(task.fileId).catch(() => undefined);
}

export function clearAttachmentUploads(options: ClearAttachmentUploadOptions): Promise<void> {
  const tasks = [...useAttachmentUploadStore.getState().tasks];
  for (const controller of activeControllers.values()) controller.abort();
  activeControllers.clear();
  uploadAuthorizations.clear();
  pollingTasks.clear();
  pendingTransfers = [];
  useAttachmentUploadStore.getState().clearTasks();

  if (!options.deleteRemote) return Promise.resolve();
  const fileIds = [...new Set(tasks.map((task) => task.fileId).filter(Boolean) as string[])];
  return Promise.allSettled(fileIds.map((fileId) => deleteFile(fileId))).then(() => undefined);
}

export function attachmentUploadsBlockSubmission(tasks: AttachmentUploadTask[]): boolean {
  return tasks.some((task) => task.state !== "ready" && task.state !== "cancelled");
}

export function configureAttachmentUploadQueueForTests(options?: {
  concurrency?: number;
  pollDelaysMs?: number[];
}): void {
  maxConcurrency = options?.concurrency ?? DEFAULT_MAX_CONCURRENCY;
  pollDelaysMs = options?.pollDelaysMs ?? [...DEFAULT_POLL_DELAYS_MS];
}

async function authorizeDirect(
  clientIds: string[],
  target?: FileUploadTarget,
): Promise<void> {
  const tasks = currentTasks(clientIds);
  if (tasks.length === 0) return;
  const response = await createUploadIntents(
    tasks.map((task) => ({
      filename: task.filename,
      mime_type: task.mimeType,
      size_bytes: task.sizeBytes,
    })),
    target,
  );
  if (response.files.length !== tasks.length) {
    throw new Error("Upload authorization returned an unexpected file count.");
  }

  response.files.forEach((intent, index) => {
    const clientId = tasks[index].clientId;
    if (!findAttachmentUploadTask(clientId)) {
      void deleteFile(intent.file_id).catch(() => undefined);
      return;
    }
    patchTask(clientId, {
      fileId: intent.file_id,
      state: "authorizing",
      error: undefined,
      errorCode: undefined,
      failureStage: undefined,
    });
    uploadAuthorizations.set(clientId, intent.upload);
    pendingTransfers.push(clientId);
  });
  drainTransfers();
}

async function authorizeLegacy(
  clientIds: string[],
  target?: FileUploadTarget,
): Promise<void> {
  const tasks = currentTasks(clientIds);
  if (tasks.length === 0) return;
  const results = await uploadFiles(
    tasks.map((task) => task.file),
    target,
  );
  if (results.length !== tasks.length) {
    throw new Error("Upload returned an unexpected file count.");
  }
  results.forEach((file, index) => applyInitialServerFile(tasks[index].clientId, file));
}

function applyInitialServerFile(clientId: string, file: FileUploadResponse): void {
  if (!findAttachmentUploadTask(clientId)) {
    void deleteFile(file.file_id).catch(() => undefined);
    return;
  }
  patchTask(clientId, { fileId: file.file_id, serverFile: file });
  if (file.status === "ready") {
    patchTask(clientId, { state: "ready", progress: 100 });
    return;
  }
  if (file.status === "failed") {
    failTask(clientId, "processing", friendlyProcessingError(file.error_code), file.error_code);
    return;
  }
  patchTask(clientId, { state: "processing", progress: 100 });
  void pollProcessingFile(clientId);
}

function drainTransfers(): void {
  while (activeTransferCount < maxConcurrency && pendingTransfers.length > 0) {
    const clientId = pendingTransfers.shift();
    if (!clientId || !findAttachmentUploadTask(clientId)) continue;
    activeTransferCount += 1;
    void transferDirectFile(clientId).finally(() => {
      activeTransferCount = Math.max(0, activeTransferCount - 1);
      drainTransfers();
    });
  }
}

async function transferDirectFile(clientId: string): Promise<void> {
  const authorization = uploadAuthorizations.get(clientId);
  const initialTask = findAttachmentUploadTask(clientId);
  if (!authorization || !initialTask) return;

  const controller = new AbortController();
  activeControllers.set(clientId, controller);
  let lastError: unknown;
  try {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const task = findAttachmentUploadTask(clientId);
      if (!task || controller.signal.aborted) return;
      patchTask(clientId, {
        state: "uploading",
        progress: 0,
        retryCount: attempt,
        error: undefined,
        errorCode: undefined,
        failureStage: undefined,
      });
      try {
        await uploadFileDirectlyToS3(task.file, authorization, {
          signal: controller.signal,
          onProgress: (progress) => {
            if (findAttachmentUploadTask(clientId)) patchTask(clientId, { progress });
          },
        });
        lastError = undefined;
        break;
      } catch (error) {
        if (error instanceof S3UploadError && error.kind === "aborted") return;
        lastError = error;
      }
    }
  } finally {
    activeControllers.delete(clientId);
    uploadAuthorizations.delete(clientId);
  }

  if (!findAttachmentUploadTask(clientId)) return;
  if (lastError) {
    failTask(clientId, "upload", friendlyUploadError(lastError));
    return;
  }

  patchTask(clientId, { state: "processing", progress: 100, retryCount: 0 });
  await completeAndPoll(clientId);
}

async function completeAndPoll(clientId: string): Promise<void> {
  const task = findAttachmentUploadTask(clientId);
  if (!task?.fileId) return;

  let result: FileUploadResponse | undefined;
  let lastError: unknown;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      result = await completeFileUpload(task.fileId);
      lastError = undefined;
      break;
    } catch (error) {
      lastError = error;
      if (!isRetryableCompletionError(error) || attempt > 0) break;
    }
  }
  if (!findAttachmentUploadTask(clientId)) return;
  if (!result) {
    const code = apiErrorCode(lastError);
    failTask(clientId, completionFailureStage(code), friendlyCompletionError(lastError), code);
    return;
  }

  patchTask(clientId, { fileId: result.file_id, serverFile: result });
  if (result.status === "ready") {
    patchTask(clientId, { state: "ready", progress: 100 });
    return;
  }
  if (result.status === "failed") {
    failTask(
      clientId,
      "processing",
      friendlyProcessingError(result.error_code),
      result.error_code,
    );
    return;
  }
  patchTask(clientId, { state: "processing", progress: 100 });
  await pollProcessingFile(clientId);
}

async function pollProcessingFile(clientId: string): Promise<void> {
  if (pollingTasks.has(clientId)) return;
  pollingTasks.add(clientId);
  try {
    for (const delayMs of pollDelaysMs) {
      await delay(delayMs);
      const task = findAttachmentUploadTask(clientId);
      if (!task?.fileId || task.state !== "processing") return;
      const result = await fetchFileStatus(task.fileId).catch(() => null);
      if (!result) return;
      patchTask(clientId, { serverFile: result });
      if (result.status === "ready") {
        patchTask(clientId, { state: "ready", progress: 100 });
        return;
      }
      if (result.status === "failed") {
        failTask(
          clientId,
          "processing",
          friendlyProcessingError(result.error_code),
          result.error_code,
        );
        return;
      }
    }
  } finally {
    pollingTasks.delete(clientId);
  }
}

function createTask(
  file: File,
  uploadMode: AttachmentUploadTask["uploadMode"],
  target?: FileUploadTarget,
): AttachmentUploadTask {
  return {
    clientId: makeClientId(),
    file,
    filename: file.name || "file",
    mimeType: file.type || "application/octet-stream",
    sizeBytes: file.size,
    state: "authorizing",
    progress: 0,
    retryCount: 0,
    uploadMode,
    target,
  };
}

function currentTasks(clientIds: string[]): AttachmentUploadTask[] {
  return clientIds
    .map((clientId) => findAttachmentUploadTask(clientId))
    .filter((task): task is AttachmentUploadTask => Boolean(task));
}

function patchTask(clientId: string, patch: Partial<AttachmentUploadTask>): void {
  useAttachmentUploadStore.getState().patchTask(clientId, patch);
}

function failTasks(
  clientIds: string[],
  stage: AttachmentUploadFailureStage,
  error: unknown,
): void {
  const code = apiErrorCode(error);
  for (const clientId of clientIds) {
    if (!findAttachmentUploadTask(clientId)) continue;
    failTask(clientId, stage, friendlyStageError(stage, error), code);
  }
}

function failTask(
  clientId: string,
  stage: AttachmentUploadFailureStage,
  message: string,
  errorCode?: string | null,
): void {
  patchTask(clientId, {
    state: "failed",
    error: message,
    errorCode: errorCode || undefined,
    failureStage: stage,
  });
}

function friendlyStageError(stage: AttachmentUploadFailureStage, error: unknown): string {
  if (stage === "authorization") return friendlyAuthorizationError(error);
  if (stage === "upload") return friendlyUploadError(error);
  if (stage === "completion") return friendlyCompletionError(error);
  return "File processing failed.";
}

function friendlyAuthorizationError(error: unknown): string {
  const code = apiErrorCode(error);
  if (code === "attachment_file_too_large") return "File exceeds your plan's upload limit.";
  if (code === "attachment_model_incompatible") {
    return "Selected model does not support this file type.";
  }
  if (code === "attachment_mime_type_incompatible" || code === "unsupported_file_type") {
    return "This file type is not supported.";
  }
  if (code === "attachment_upload_expired") {
    return "Upload authorization expired. Retry the upload.";
  }
  return "Upload could not be prepared. Retry the upload.";
}

function friendlyUploadError(error: unknown): string {
  if (error instanceof S3UploadError && error.kind === "network") {
    return "Upload could not reach storage. Check your connection and retry.";
  }
  return "Upload failed before reaching storage.";
}

function friendlyCompletionError(error: unknown): string {
  const code = apiErrorCode(error);
  if (code === "attachment_upload_expired") {
    return "Upload authorization expired. Retry the upload.";
  }
  if (code === "attachment_mime_type_incompatible" || code === "unsupported_file_type") {
    return "This file type is not supported.";
  }
  return "The uploaded file could not be verified.";
}

function friendlyProcessingError(errorCode?: string | null): string {
  const code = String(errorCode || "").trim().toLowerCase();
  if (["invalid_mime_type", "unsupported_file_type"].includes(code)) {
    return "This file type is not supported.";
  }
  if (code === "empty_file") return "The selected file is empty.";
  if (code === "file_too_large") return "This file is too large to upload.";
  if (code === "attachment_upload_mismatch") {
    return "The uploaded file could not be verified. Retry the upload.";
  }
  return "This file could not be processed. Retry the upload.";
}

function apiErrorCode(error: unknown): string | undefined {
  if (!(error instanceof ApiClientError) || typeof error.body !== "object" || error.body === null) {
    return undefined;
  }
  const detail = (error.body as Record<string, unknown>).detail;
  if (typeof detail !== "object" || detail === null) return undefined;
  const code = (detail as Record<string, unknown>).code;
  return typeof code === "string" ? code : undefined;
}

function isRetryableCompletionError(error: unknown): boolean {
  if (!(error instanceof ApiClientError)) return true;
  return error.status >= 500 || apiErrorCode(error) === "attachment_upload_not_complete";
}

function completionFailureStage(code?: string): AttachmentUploadFailureStage {
  return code === "attachment_upload_expired" ? "authorization" : "completion";
}

function requiresFreshIntent(code?: string): boolean {
  return code === "attachment_upload_expired" || code === "attachment_upload_mismatch";
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function makeClientId(): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `attachment-${random}`;
}
