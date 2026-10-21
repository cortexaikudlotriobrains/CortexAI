import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PromptComposer } from "../components/composer/PromptComposer";
import { DEFAULT_MODELS } from "../config/defaultModels";
import { useChatStore } from "../store/chatStore";
import { useAttachmentUploadStore } from "../store/attachmentUploadStore";
import type { AttachmentUploadState } from "../store/attachmentUploadStore";
import type { FileUploadResponse } from "../types";

const chatActions = vi.hoisted(() => ({
  submit: vi.fn(),
  cancel: vi.fn(),
}));

vi.mock("../hooks/useChat", () => ({
  useChat: () => chatActions,
}));

vi.mock("../api/files", () => ({
  uploadFiles: vi.fn(),
  deleteFile: vi.fn().mockResolvedValue(undefined),
  fetchFileStatus: vi.fn(),
}));

describe("PromptComposer", () => {
  beforeEach(() => {
    useChatStore.setState({
      mode: "single",
      smartMode: true,
      lastManualModel: null,
      composerInitialized: false,
      compareHasSent: false,
      comparePrefsModel: null,
      optimizeMode: false,
      askReasoningLevel: "auto",
      compareReasoningLevel: "low",
      selectedModelKey: "openai:gpt-5.1",
      compareModelKeys: ["openai:gpt-5.1", "claude:claude-sonnet-4-5", ""],
      prompt: "",
      attachments: [],
      turns: [],
      activeTurnId: null,
      responses: [],
      streaming: false,
      streamingText: "",
      error: null,
    });
    useAttachmentUploadStore.setState({ tasks: [] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("inserts a newline with Shift+Enter on mobile and sends from the arrow", async () => {
    stubMobileViewport(true);
    const user = userEvent.setup();

    render(<PromptComposer models={DEFAULT_MODELS} />);

    const textarea = screen.getByRole("textbox", { name: "Prompt input" });
    await user.type(textarea, "First line{Shift>}{Enter}{/Shift}Second line");

    expect(textarea).toHaveValue("First line\nSecond line");
    expect(chatActions.submit).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(chatActions.submit).toHaveBeenCalledTimes(1);
  });

  it("preserves Enter-to-send on desktop", async () => {
    stubMobileViewport(false);
    const user = userEvent.setup();

    render(<PromptComposer models={DEFAULT_MODELS} />);

    const textarea = screen.getByRole("textbox", { name: "Prompt input" });
    await user.type(textarea, "Desktop prompt{Enter}");

    expect(textarea).toHaveValue("Desktop prompt");
    expect(chatActions.submit).toHaveBeenCalledTimes(1);
  });

  it("keeps attachments and a single model trigger in the compact Ask shell", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ attachments: [attachment()] });
    render(<PromptComposer models={DEFAULT_MODELS} />);
    const card = document.querySelector("[data-composer-card]");
    expect(card).toContainElement(screen.getByText("long-mobile-design-reference.pdf"));
    expect(screen.getByRole("textbox")).toHaveAttribute("placeholder", "Ask anything…");
    expect(screen.getByRole("button", { name: "Model: Smart" })).toBeVisible();
    expect(screen.queryByRole("switch", { name: "Smart routing" })).not.toBeInTheDocument();
    expect(card).toContainElement(screen.getByRole("button", { name: "Attach files" }));
    expect(card).toContainElement(screen.getByRole("button", { name: "Send message" }));
    await user.click(
      screen.getByRole("button", { name: "Remove long-mobile-design-reference.pdf" }),
    );
    expect(useChatStore.getState().attachments).toEqual([]);
    await user.click(screen.getByRole("button", { name: "More options" }));
    expect(screen.getByText("Applied to Smart routing")).toBeVisible();
    expect(screen.queryByText("Smart in new chats")).not.toBeInTheDocument();
  });

  it("keeps automatic web-search policy out of the Ask composer", () => {
    render(<PromptComposer models={DEFAULT_MODELS} />);

    expect(screen.queryByRole("switch", { name: "Research mode" })).not.toBeInTheDocument();
    expect(screen.queryByText("Web")).not.toBeInTheDocument();
  });

  it("preserves a supported reasoning override when selecting an explicit model", async () => {
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);

    await user.click(screen.getByRole("button", { name: "More options" }));
    await user.click(
      screen.getByRole("radio", { name: "High — Deeper analysis for difficult questions" }),
    );

    expect(useChatStore.getState().askReasoningLevel).toBe("high");
    expect(screen.getByRole("dialog", { name: "More options" })).toBeVisible();
    expect(
      screen.getByRole("radio", { name: "High — Deeper analysis for difficult questions" }),
    ).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("Applied to Smart routing")).toBeVisible();
    expect(screen.getByRole("button", { name: "More options" })).toHaveAttribute(
      "data-reasoning-level",
      "high",
    );
    expect(
      screen.getByText("High", { selector: "[data-option-chip='reasoning'] span" }),
    ).toBeVisible();

    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("button", { name: "Model: Smart" }));
    await user.click(screen.getByRole("option", { name: /gpt-5.6-luna/ }));

    expect(useChatStore.getState().smartMode).toBe(false);
    expect(useChatStore.getState().askReasoningLevel).toBe("high");
    await user.click(screen.getByRole("button", { name: "More options" }));
    expect(screen.queryByText("Manual Ask", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText("Smart Ask", { exact: true })).not.toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: "High — Deeper analysis for difficult questions" }),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("supports radio arrow keys, keeps the dialog open, and returns focus on Escape", async () => {
    stubMobileViewport(false);
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);

    const options = screen.getByRole("button", { name: "More options" });
    await user.click(options);
    const auto = screen.getByRole("radio", {
      name: "Auto — Cortex chooses for each request",
    });
    auto.focus();
    await user.keyboard("{ArrowDown}");

    expect(useChatStore.getState().askReasoningLevel).toBe("low");
    expect(screen.getByRole("dialog", { name: "More options" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Low/ })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Reset reasoning" })).toBeVisible();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "More options" })).not.toBeInTheDocument();
    await waitFor(() => expect(options).toHaveFocus());

    await user.click(screen.getByRole("button", { name: "Reset reasoning" }));
    expect(useChatStore.getState().askReasoningLevel).toBe("auto");
    expect(screen.queryByRole("button", { name: "Reset reasoning" })).not.toBeInTheDocument();
  });

  it("uses a modal bottom sheet with trapped focus on phone layouts", async () => {
    stubMobileViewport(true);
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);

    await user.click(screen.getByRole("button", { name: "More options" }));
    const dialog = screen.getByRole("dialog", { name: "More options" });

    expect(dialog).toHaveAttribute("data-layout", "sheet");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Done" })).toHaveFocus();
    });
    expect(document.querySelector("[data-composer-scrim]")).toBeVisible();
  });

  it("disables Compare reasoning levels that are not shared by every model", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      mode: "compare",
      compareModelKeys: ["openai:gpt-5.6-luna", "deepseek:deepseek-v4-flash", ""],
      compareReasoningLevel: "low",
    });
    render(<PromptComposer models={DEFAULT_MODELS} />);

    await user.click(screen.getByRole("button", { name: "More options" }));
    expect(screen.getByText("Applied to every compared model")).toBeVisible();
    expect(screen.getByRole("radio", { name: /Low/ })).toBeEnabled();
    expect(screen.getByRole("radio", { name: /Medium/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Maximum/ })).toBeEnabled();
  });

  it("uses the same shell in Compare mode without a redundant mode switch", () => {
    useChatStore.setState({ mode: "compare" });
    render(<PromptComposer models={DEFAULT_MODELS} />);

    expect(useChatStore.getState().mode).toBe("compare");
    expect(screen.getByRole("button", { name: "Models to compare" })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Smart routing" })).not.toBeInTheDocument();
    const optionsButton = screen.getByRole("button", { name: "More options" });
    expect(screen.queryByRole("switch", { name: "Research mode" })).not.toBeInTheDocument();
    expect(document.querySelector("[data-composer-card]")).toContainElement(optionsButton);
    expect(screen.queryByRole("switch", { name: /Improve prompt/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Compare" })).not.toBeInTheDocument();

    const textarea = screen.getByRole("textbox", { name: "Prompt input" });
    const card = textarea.parentElement?.parentElement;
    expect(textarea).toHaveAttribute("placeholder", "Ask once and compare model responses");
    expect(textarea).toHaveAttribute("rows", "1");
    expect(card).toContainElement(screen.getByRole("button", { name: "Models to compare" }));
    expect(card).toContainElement(screen.getByRole("button", { name: "Send message" }));
  });

  it("toggles Improve from the options menu in Ask and Compare", async () => {
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);

    await user.click(screen.getByRole("button", { name: "More options" }));
    const improveSwitch = screen.getByRole("switch", { name: /Improve prompt/ });
    await user.click(improveSwitch);

    expect(improveSwitch).toHaveAttribute("aria-checked", "true");
    expect(useChatStore.getState().optimizeMode).toBe(true);
    const optionsButton = screen.getByRole("button", { name: "More options" });
    expect(optionsButton).toHaveAttribute("data-improve-enabled", "true");
    expect(optionsButton).toHaveAttribute("data-options-configured", "true");
    expect(screen.getByRole("button", { name: "Turn off Improve prompt" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Turn off Improve prompt" }));

    expect(useChatStore.getState().optimizeMode).toBe(false);
    expect(
      screen.queryByRole("button", { name: "Turn off Improve prompt" }),
    ).not.toBeInTheDocument();
  });

  it("auto-grows longer prompts and caps the textarea height", async () => {
    render(<PromptComposer models={DEFAULT_MODELS} />);

    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "Prompt input",
    });
    Object.defineProperty(textarea, "scrollHeight", {
      configurable: true,
      value: 96,
    });

    fireEvent.change(textarea, { target: { value: "Line one\nLine two\nLine three" } });

    await waitFor(() => {
      expect(textarea.style.height).toBe("96px");
      expect(textarea.style.overflowY).toBe("hidden");
    });

    Object.defineProperty(textarea, "scrollHeight", {
      configurable: true,
      value: 220,
    });
    fireEvent.change(textarea, {
      target: { value: "Line one\nLine two\nLine three\nLine four" },
    });

    await waitFor(() => {
      expect(textarea.style.height).toBe("168px");
      expect(textarea.style.overflowY).toBe("auto");
    });
  });

  it.each(["authorizing", "uploading", "processing", "failed"] as const)(
    "blocks Send while a selected attachment is %s",
    (state) => {
      useChatStore.setState({ prompt: "Analyze this file" });
      useAttachmentUploadStore.setState({ tasks: [uploadTask(state)] });

      render(<PromptComposer models={DEFAULT_MODELS} />);

      const send = screen.getByRole("button", { name: "Send message" });
      expect(send).toBeDisabled();
      expect(send).toHaveAccessibleDescription("Waiting for attachments to finish uploading");
    },
  );

  it("enables file-only Send when every selected attachment is server-ready", () => {
    const ready = attachment();
    useChatStore.setState({ attachments: [ready] });
    useAttachmentUploadStore.setState({
      tasks: [uploadTask("ready", ready)],
    });

    render(<PromptComposer models={DEFAULT_MODELS} />);

    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
    expect(screen.getAllByText("long-mobile-design-reference.pdf")).toHaveLength(1);
  });
});

function uploadTask(state: AttachmentUploadState, serverFile?: FileUploadResponse) {
  return {
    clientId: `client-${state}`,
    file: new File(["file"], "long-mobile-design-reference.pdf", {
      type: "application/pdf",
    }),
    fileId: serverFile?.file_id ?? "file-pending",
    filename: "long-mobile-design-reference.pdf",
    mimeType: "application/pdf",
    sizeBytes: 59699,
    state,
    progress: state === "uploading" ? 42 : state === "ready" ? 100 : 0,
    retryCount: 0,
    uploadMode: "direct" as const,
    serverFile,
  };
}

function stubMobileViewport(matches: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      matches: query === "(max-width: 900px)" || query === "(max-width: 760px)" ? matches : false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

function attachment(): FileUploadResponse {
  return {
    file_id: "file-1",
    original_filename: "long-mobile-design-reference.pdf",
    mime_type: "application/pdf",
    size_bytes: 59699,
    status: "ready",
    ingestion_meta: {},
    created_at: "2026-06-10T00:00:00.000Z",
    deduplicated: false,
  };
}
