import { beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";

const apiMocks = vi.hoisted(() => ({
  completeFileUpload: vi.fn(),
  createUploadIntents: vi.fn(),
  deleteFile: vi.fn(),
  fetchFileStatus: vi.fn(),
  uploadFiles: vi.fn(),
}));
const transferMock = vi.hoisted(() => vi.fn());

vi.mock("../api/files", () => apiMocks);
vi.mock("../uploads/directS3Upload", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../uploads/directS3Upload")>();
  return { ...actual, uploadFileDirectlyToS3: transferMock };
});

import {
  beginAttachmentUploads,
  clearAttachmentUploads,
  configureAttachmentUploadQueueForTests,
  removeAttachmentUpload,
  retryAttachmentUpload,
} from "../uploads/attachmentUploadQueue";
import { useAttachmentUploadStore } from "../store/attachmentUploadStore";
import type { FileUploadResponse } from "../types";
import { S3UploadError } from "../uploads/directS3Upload";
import { ApiClientError } from "../api/client";

describe("attachmentUploadQueue", () => {
  beforeEach(async () => {
    await clearAttachmentUploads({ deleteRemote: false });
    configureAttachmentUploadQueueForTests({ concurrency: 2, pollDelaysMs: [0, 0] });
    vi.clearAllMocks();
    apiMocks.deleteFile.mockResolvedValue(undefined);
  });

  it("creates immediate authorizing tasks and completes only after S3 succeeds", async () => {
    const intent = deferred<ReturnType<typeof intentResponse>>();
    const transfer = deferred<void>();
    apiMocks.createUploadIntents.mockReturnValue(intent.promise);
    transferMock.mockReturnValue(transfer.promise);
    apiMocks.completeFileUpload.mockResolvedValue(serverFile("file-1", "report.pdf", "ready"));

    const begin = beginAttachmentUploads([textFile("report.pdf")], { mode: "direct" });

    expect(useAttachmentUploadStore.getState().tasks).toMatchObject([
      { filename: "report.pdf", state: "authorizing", progress: 0 },
    ]);
    expect(apiMocks.createUploadIntents).toHaveBeenCalledWith(
      [{ filename: "report.pdf", mime_type: "text/plain", size_bytes: 10 }],
      undefined,
    );

    intent.resolve(intentResponse("file-1", "report.pdf"));
    await begin;
    await waitFor(() => {
      expect(useAttachmentUploadStore.getState().tasks[0].state).toBe("uploading");
    });
    expect(apiMocks.completeFileUpload).not.toHaveBeenCalled();

    transfer.resolve();
    await waitFor(() => {
      expect(useAttachmentUploadStore.getState().tasks[0]).toMatchObject({
        state: "ready",
        progress: 100,
        fileId: "file-1",
      });
    });
    expect(apiMocks.completeFileUpload).toHaveBeenCalledWith("file-1");
  });

  it("limits direct transfers to two and starts the next queued file when a slot opens", async () => {
    const transfers = [deferred<void>(), deferred<void>(), deferred<void>()];
    apiMocks.createUploadIntents.mockResolvedValue(
      intentResponse(
        ["file-a", "file-b", "file-c"],
        ["a.txt", "b.txt", "c.txt"],
      ),
    );
    transferMock.mockImplementation(() => transfers[transferMock.mock.calls.length - 1].promise);
    apiMocks.completeFileUpload.mockImplementation((fileId: string) =>
      Promise.resolve(serverFile(fileId, `${fileId}.txt`, "ready")),
    );

    await beginAttachmentUploads(
      [textFile("a.txt"), textFile("b.txt"), textFile("c.txt")],
      { mode: "direct" },
    );
    await waitFor(() => expect(transferMock).toHaveBeenCalledTimes(2));

    transfers[0].resolve();
    await waitFor(() => expect(transferMock).toHaveBeenCalledTimes(3));
    transfers[1].resolve();
    transfers[2].resolve();

    await waitFor(() => {
      expect(useAttachmentUploadStore.getState().tasks.every((task) => task.state === "ready"))
        .toBe(true);
    });
  });

  it("maps duplicate filenames by response order instead of filename identity", async () => {
    apiMocks.createUploadIntents.mockResolvedValue(
      intentResponse(["duplicate-a", "duplicate-b"], ["same.txt", "same.txt"]),
    );
    transferMock.mockResolvedValue(undefined);
    apiMocks.completeFileUpload.mockImplementation((fileId: string) =>
      Promise.resolve(serverFile(fileId, "same.txt", "ready")),
    );

    await beginAttachmentUploads([textFile("same.txt"), textFile("same.txt")], {
      mode: "direct",
    });

    await waitFor(() => {
      expect(useAttachmentUploadStore.getState().tasks.map((task) => task.state)).toEqual([
        "ready",
        "ready",
      ]);
    });
    expect(useAttachmentUploadStore.getState().tasks.map((task) => task.fileId)).toEqual([
      "duplicate-a",
      "duplicate-b",
    ]);
  });

  it("keeps successful siblings and retries one failed S3 upload with a fresh intent", async () => {
    let intentSequence = 0;
    const transferAttempts = new Map<string, number>();
    apiMocks.createUploadIntents.mockImplementation(
      (metadata: Array<{ filename: string }>) => {
        intentSequence += 1;
        return Promise.resolve(
          intentResponse(
            metadata.map((item, index) => `file-${intentSequence}-${index}-${item.filename}`),
            metadata.map((item) => item.filename),
          ),
        );
      },
    );
    transferMock.mockImplementation((file: File) => {
      const attempt = (transferAttempts.get(file.name) ?? 0) + 1;
      transferAttempts.set(file.name, attempt);
      if (file.name === "bad.txt" && attempt <= 2) {
        return Promise.reject(new S3UploadError("network"));
      }
      return Promise.resolve();
    });
    apiMocks.completeFileUpload.mockImplementation((fileId: string) =>
      Promise.resolve(serverFile(fileId, fileId.includes("good") ? "good.txt" : "bad.txt", "ready")),
    );

    await beginAttachmentUploads([textFile("good.txt"), textFile("bad.txt")], {
      mode: "direct",
    });
    await waitFor(() => {
      const states = Object.fromEntries(
        useAttachmentUploadStore.getState().tasks.map((task) => [task.filename, task.state]),
      );
      expect(states).toEqual({ "good.txt": "ready", "bad.txt": "failed" });
    });

    const failed = useAttachmentUploadStore
      .getState()
      .tasks.find((task) => task.filename === "bad.txt");
    expect(failed?.error).toContain("reach storage");
    await retryAttachmentUpload(failed!.clientId);

    await waitFor(() => {
      expect(
        useAttachmentUploadStore
          .getState()
          .tasks.find((task) => task.filename === "bad.txt")?.state,
      ).toBe("ready");
    });
    expect(apiMocks.deleteFile).toHaveBeenCalledWith(failed?.fileId);
    expect(apiMocks.createUploadIntents).toHaveBeenCalledTimes(2);
  });

  it("polls a processing completion to ready and leaves bounded processing visible", async () => {
    apiMocks.createUploadIntents.mockResolvedValue(
      intentResponse(["file-ready", "file-slow"], ["ready.txt", "slow.txt"]),
    );
    transferMock.mockResolvedValue(undefined);
    apiMocks.completeFileUpload.mockImplementation((fileId: string) =>
      Promise.resolve(serverFile(fileId, `${fileId}.txt`, "processing")),
    );
    apiMocks.fetchFileStatus.mockImplementation((fileId: string) =>
      Promise.resolve(
        serverFile(fileId, `${fileId}.txt`, fileId === "file-ready" ? "ready" : "processing"),
      ),
    );

    await beginAttachmentUploads([textFile("ready.txt"), textFile("slow.txt")], {
      mode: "direct",
    });

    await waitFor(() => {
      const states = Object.fromEntries(
        useAttachmentUploadStore.getState().tasks.map((task) => [task.filename, task.state]),
      );
      expect(states).toEqual({ "ready.txt": "ready", "slow.txt": "processing" });
    });
  });

  it("surfaces Cortex verification failures without treating S3 success as ready", async () => {
    apiMocks.createUploadIntents.mockResolvedValue(
      intentResponse("file-mismatch", "mismatch.txt"),
    );
    transferMock.mockResolvedValue(undefined);
    apiMocks.completeFileUpload.mockRejectedValue(
      new ApiClientError(409, "mismatch", {
        detail: { code: "attachment_upload_mismatch", message: "opaque detail" },
      }),
    );

    await beginAttachmentUploads([textFile("mismatch.txt")], { mode: "direct" });

    await waitFor(() => {
      expect(useAttachmentUploadStore.getState().tasks[0]).toMatchObject({
        state: "failed",
        failureStage: "completion",
        error: "The uploaded file could not be verified.",
      });
    });
    expect(transferMock).toHaveBeenCalledTimes(1);
    expect(apiMocks.completeFileUpload).toHaveBeenCalledTimes(1);
  });

  it("does not expose a persisted processing error returned by the API", async () => {
    apiMocks.createUploadIntents.mockResolvedValue(
      intentResponse("file-failed", "failed.txt"),
    );
    transferMock.mockResolvedValue(undefined);
    apiMocks.completeFileUpload.mockResolvedValue({
      ...serverFile("file-failed", "failed.txt", "failed"),
      error_code: "ingestion_failed",
      error_message: "Traceback: storage_key=attachments/users/secret",
    });

    await beginAttachmentUploads([textFile("failed.txt")], { mode: "direct" });

    await waitFor(() => {
      expect(useAttachmentUploadStore.getState().tasks[0]).toMatchObject({
        state: "failed",
        failureStage: "processing",
        error: "This file could not be processed. Retry the upload.",
      });
    });
    expect(useAttachmentUploadStore.getState().tasks[0].error).not.toContain("storage_key");
  });

  it("explains model-incompatible authorization before any S3 transfer", async () => {
    apiMocks.createUploadIntents.mockRejectedValue(
      new ApiClientError(400, "incompatible", {
        detail: { code: "attachment_model_incompatible", message: "opaque detail" },
      }),
    );

    await expect(
      beginAttachmentUploads([textFile("incompatible.txt")], { mode: "direct" }),
    ).rejects.toThrow("incompatible");

    expect(useAttachmentUploadStore.getState().tasks[0]).toMatchObject({
      state: "failed",
      failureStage: "authorization",
      error: "Selected model does not support this file type.",
    });
    expect(transferMock).not.toHaveBeenCalled();
  });

  it("aborts and deletes an authorized upload when the user removes it", async () => {
    apiMocks.createUploadIntents.mockResolvedValue(intentResponse("file-remove", "remove.txt"));
    transferMock.mockImplementation(
      (_file: File, _upload: unknown, options: { signal: AbortSignal }) =>
        new Promise<void>((_resolve, reject) => {
          options.signal.addEventListener(
            "abort",
            () => reject(new S3UploadError("aborted")),
            { once: true },
          );
        }),
    );

    await beginAttachmentUploads([textFile("remove.txt")], { mode: "direct" });
    await waitFor(() => {
      expect(useAttachmentUploadStore.getState().tasks[0].state).toBe("uploading");
    });
    const task = useAttachmentUploadStore.getState().tasks[0];

    await removeAttachmentUpload(task.clientId);

    expect(useAttachmentUploadStore.getState().tasks).toEqual([]);
    expect(apiMocks.deleteFile).toHaveBeenCalledWith("file-remove");
  });

  it("uses the same task UI model for the legacy rollout path", async () => {
    apiMocks.uploadFiles.mockResolvedValue([serverFile("legacy-1", "legacy.txt", "ready")]);

    await beginAttachmentUploads([textFile("legacy.txt")], { mode: "legacy" });

    expect(useAttachmentUploadStore.getState().tasks[0]).toMatchObject({
      uploadMode: "legacy",
      state: "ready",
      fileId: "legacy-1",
    });
    expect(transferMock).not.toHaveBeenCalled();
  });
});

function textFile(name: string): File {
  return new File([name], name, { type: "text/plain" });
}

interface IntentResponseFixture {
  files: Array<{
    file_id: string;
    original_filename: string;
    mime_type: string;
    size_bytes: number;
    status: string;
    ingestion_meta: Record<string, never>;
    created_at: string;
    upload: { url: string; fields: Record<string, string>; expires_at: string };
  }>;
}

function intentResponse(fileId: string, filename: string): IntentResponseFixture;
function intentResponse(fileIds: string[], filenames: string[]): IntentResponseFixture;
function intentResponse(
  fileIds: string | string[],
  filenames: string | string[],
): IntentResponseFixture {
  const ids = Array.isArray(fileIds) ? fileIds : [fileIds];
  const names = Array.isArray(filenames) ? filenames : [filenames];
  return {
    files: ids.map((fileId, index) => ({
      file_id: fileId,
      original_filename: names[index],
      mime_type: "text/plain",
      size_bytes: names[index].length,
      status: "uploading",
      ingestion_meta: {},
      created_at: "2026-08-11T00:00:00Z",
      upload: {
        url: "https://bucket.s3.us-east-1.amazonaws.com/",
        fields: { key: `attachments/${fileId}` },
        expires_at: "2026-08-11T00:05:00Z",
      },
    })),
  };
}

function serverFile(
  fileId: string,
  filename: string,
  status: "ready" | "processing" | "failed",
): FileUploadResponse {
  return {
    file_id: fileId,
    original_filename: filename,
    mime_type: "text/plain",
    size_bytes: filename.length,
    status,
    error_code: null,
    error_message: null,
    ingestion_meta: {},
    created_at: "2026-08-11T00:00:00Z",
    deduplicated: false,
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
