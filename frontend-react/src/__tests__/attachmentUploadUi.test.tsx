import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

import { AttachmentStrip } from "../components/composer/AttachmentStrip";
import { useAttachmentUploadStore } from "../store/attachmentUploadStore";
import { useChatStore } from "../store/chatStore";
import { clearAttachmentUploads } from "../uploads/attachmentUploadQueue";
import type { EntitlementsResponse, FileUploadResponse } from "../types";

describe("AttachmentStrip direct uploads", () => {
  beforeEach(async () => {
    await clearAttachmentUploads({ deleteRemote: false });
    useChatStore.setState({
      mode: "single",
      smartMode: true,
      attachments: [],
      error: null,
      subscriptionError: null,
    });
    vi.clearAllMocks();
    apiMocks.deleteFile.mockResolvedValue(undefined);
    (
      window as unknown as {
        CORTEX_RUNTIME_CONFIG?: Record<string, unknown>;
      }
    ).CORTEX_RUNTIME_CONFIG = {
      directAttachmentUploads: true,
      legacyAttachmentUploads: true,
    };
  });

  afterEach(() => {
    cleanup();
    delete (
      window as unknown as {
        CORTEX_RUNTIME_CONFIG?: Record<string, unknown>;
      }
    ).CORTEX_RUNTIME_CONFIG;
  });

  it("shows Preparing, upload progress, Processing, and Ready before promotion", async () => {
    const intent = deferred<ReturnType<typeof intentResponse>>();
    const transfer = deferred<void>();
    apiMocks.createUploadIntents.mockReturnValue(intent.promise);
    transferMock.mockImplementation(
      (_file: File, _upload: unknown, options: { onProgress?: (value: number) => void }) => {
        options.onProgress?.(36);
        return transfer.promise;
      },
    );
    const completed = deferred<FileUploadResponse>();
    apiMocks.completeFileUpload.mockReturnValue(completed.promise);

    render(<AttachmentStrip entitlements={entitlements()} />);
    fireEvent.change(document.querySelector("#attachmentInput")!, {
      target: {
        files: [new File(["report"], "report.pdf", { type: "application/pdf" })],
      },
    });

    expect(screen.getByText("report.pdf")).toBeInTheDocument();
    expect(screen.getByText("Preparing…")).toBeInTheDocument();
    intent.resolve(intentResponse("file-1", "report.pdf"));
    await waitFor(() => expect(screen.getByText("Uploading 36%")).toBeInTheDocument());
    expect(useChatStore.getState().attachments).toEqual([]);

    transfer.resolve();
    await waitFor(() => expect(screen.getByText("Processing…")).toBeInTheDocument());
    expect(apiMocks.completeFileUpload).toHaveBeenCalledWith("file-1");
    expect(useChatStore.getState().attachments).toEqual([]);

    completed.resolve(serverFile("file-1", "report.pdf"));
    await waitFor(() => expect(screen.getByText("Ready")).toBeInTheDocument());
    await waitFor(() => expect(useChatStore.getState().attachments).toHaveLength(1));
  });

  it("keeps an authorization failure visible with an individual Retry action", async () => {
    apiMocks.createUploadIntents.mockRejectedValue(new Error("opaque provider failure"));

    render(<AttachmentStrip entitlements={entitlements()} />);
    fireEvent.change(document.querySelector("#attachmentInput")!, {
      target: {
        files: [new File(["notes"], "notes.txt", { type: "text/plain" })],
      },
    });

    await waitFor(() => expect(screen.getByText("Upload failed")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Retry notes.txt" })).toBeVisible();
    expect(screen.getByText("notes.txt").closest("li")).toHaveAttribute(
      "title",
      "Upload could not be prepared. Retry the upload.",
    );
    expect(useAttachmentUploadStore.getState().tasks[0]).toMatchObject({
      state: "failed",
      failureStage: "authorization",
    });
    expect(useChatStore.getState().error).toMatchObject({
      title: "The file upload couldn't start",
      message: "Retry the upload. Your message and other files are unchanged.",
    });
    expect(JSON.stringify(useChatStore.getState().error)).not.toContain("opaque provider failure");
  });
});

function entitlements(): EntitlementsResponse {
  return {
    plan: {
      code: "pro",
      display_name: "Pro",
      status: "active",
      source: "stripe",
      renews_at: "2026-09-01T00:00:00Z",
      cancel_at_period_end: false,
      grace_until: null,
    },
    features: {
      compare_enabled: true,
      max_compare_models: 3,
      research_enabled: true,
      prompt_improvement_enabled: true,
      file_analysis_enabled: true,
      usage_export_enabled: true,
      saved_history_enabled: true,
      models_catalog_enabled: true,
    },
    model_access: {
      allowed_billing_classes: ["economical", "standard", "advanced", "premium"],
    },
    limits: { max_files_per_request: 5, max_file_bytes: 20_000_000 },
    allowances: {
      ai_credits: { used: 0, reserved: 0, limit: 3_000_000, remaining: 3_000_000 },
    },
    period: {
      starts_at: "2026-08-01T00:00:00Z",
      ends_at: "2026-09-01T00:00:00Z",
    },
  };
}

function intentResponse(fileId: string, filename: string) {
  return {
    files: [
      {
        file_id: fileId,
        original_filename: filename,
        mime_type: "application/pdf",
        size_bytes: 6,
        status: "uploading",
        ingestion_meta: {},
        created_at: "2026-08-11T00:00:00Z",
        upload: {
          url: "https://bucket.s3.us-east-1.amazonaws.com/",
          fields: { key: `attachments/${fileId}` },
          expires_at: "2026-08-11T00:05:00Z",
        },
      },
    ],
  };
}

function serverFile(fileId: string, filename: string): FileUploadResponse {
  return {
    file_id: fileId,
    original_filename: filename,
    mime_type: "application/pdf",
    size_bytes: 6,
    status: "ready",
    error_code: null,
    error_message: null,
    ingestion_meta: {},
    created_at: "2026-08-11T00:00:00Z",
    deduplicated: false,
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}
