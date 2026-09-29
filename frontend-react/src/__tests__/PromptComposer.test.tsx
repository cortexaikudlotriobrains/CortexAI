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

  it("inserts a newline with Enter on mobile and sends from the arrow", async () => {
    stubMobileViewport(true);
    const user = userEvent.setup();

    render(<PromptComposer models={DEFAULT_MODELS} />);

    const textarea = screen.getByRole("textbox", { name: "Prompt input" });
    await user.type(textarea, "First line{Enter}Second line");

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

  it("keeps the compact Ask composer controls in one shell", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ attachments: [attachment()] });

    render(<PromptComposer models={DEFAULT_MODELS} />);

    const textarea = screen.getByRole("textbox", { name: "Prompt input" });
    const card = textarea.parentElement?.parentElement;
    const fileName = screen.getByText("long-mobile-design-reference.pdf");
    const attachButton = screen.getByRole("button", { name: "Attach files" });
    const smartSwitch = screen.getByRole("switch", { name: "Smart routing" });
    const smartTooltip = screen.getByRole("tooltip", {
      name: "Gets you the best answer automatically",
    });
    const optionsButton = screen.getByRole("button", { name: "More options" });
    const sendButton = screen.getByRole("button", { name: "Send message" });
    const controls = card?.querySelector('[data-composer-mode="single"]');
    const featureControls = card?.querySelector("#promptFeatureControls");

    expect(textarea).toHaveAttribute("rows", "1");
    expect(textarea).toHaveAttribute("placeholder", "Ask anything…");
    expect(card).toContainElement(fileName);
    expect(card).toContainElement(attachButton);
    expect(card).toContainElement(smartSwitch);
    expect(controls).toContainElement(attachButton);
    expect(featureControls).toContainElement(smartSwitch);
    expect(featureControls).toContainElement(optionsButton);
    expect(optionsButton).toHaveAccessibleDescription("Improve off; reasoning Auto");
    expect(optionsButton).toHaveAttribute("data-improve-enabled", "false");
    expect(optionsButton).toHaveAttribute("data-reasoning-level", "auto");
    expect(optionsButton).toHaveAttribute("data-options-configured", "false");
    expect(optionsButton).toHaveAttribute("aria-haspopup", "dialog");
    expect(optionsButton.querySelector("svg")).toBeInTheDocument();
    expect(smartSwitch).toHaveAttribute("aria-describedby", smartTooltip.id);
    expect(screen.queryByRole("switch", { name: "Research mode" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("switch", { name: /Improve prompt/ }),
    ).not.toBeInTheDocument();
    expect(card).toContainElement(sendButton);
    expect(screen.queryByRole("combobox", { name: "Answer depth" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Compare" })).not.toBeInTheDocument();
    expect(
      textarea.compareDocumentPosition(fileName) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    await user.click(
      screen.getByRole("button", { name: "Remove long-mobile-design-reference.pdf" }),
    );

    await waitFor(() => {
      expect(screen.queryByText("long-mobile-design-reference.pdf")).not.toBeInTheDocument();
    });
    expect(useChatStore.getState().attachments).toEqual([]);

    await user.click(optionsButton);
    expect(screen.getByRole("dialog", { name: "More options" })).toBeVisible();
    expect(screen.getByRole("switch", { name: /Improve prompt/ })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(screen.getByRole("radiogroup", { name: "Reasoning" })).toBeVisible();
    expect(
      screen.getByRole("radio", { name: "Auto — Cortex chooses for each request" }),
    ).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("Applied to Smart routing")).toBeVisible();
    expect(screen.queryByText(/credits left/i)).not.toBeInTheDocument();
  });

  it("keeps automatic web-search policy out of the Ask composer", () => {
    render(<PromptComposer models={DEFAULT_MODELS} />);

    expect(screen.queryByRole("switch", { name: "Research mode" })).not.toBeInTheDocument();
    expect(screen.queryByText("Web")).not.toBeInTheDocument();
  });

  it("lets Smart users override Auto reasoning and resets explicit models to Low", async () => {
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
    expect(screen.getByText("High", { selector: "[data-option-chip='reasoning'] span" })).toBeVisible();

    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("switch", { name: "Smart routing" }));

    expect(useChatStore.getState().smartMode).toBe(false);
    expect(useChatStore.getState().askReasoningLevel).toBe("low");
    await user.click(screen.getByRole("button", { name: "More options" }));
    expect(
      screen.getByRole("radio", { name: "Low — Faster, lighter analysis" }),
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
    expect(screen.getByRole("button", { name: "Reset reasoning to Auto" })).toBeVisible();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "More options" })).not.toBeInTheDocument();
    await waitFor(() => expect(options).toHaveFocus());

    await user.click(screen.getByRole("button", { name: "Reset reasoning to Auto" }));
    expect(useChatStore.getState().askReasoningLevel).toBe("auto");
    expect(screen.queryByRole("button", { name: "Reset reasoning to Auto" })).not.toBeInTheDocument();
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
      expect(screen.getByRole("switch", { name: /Improve prompt/ })).toHaveFocus();
    });
    expect(screen.getByRole("button", { name: "Dismiss options sheet" })).toBeVisible();
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
    expect(screen.getByText("Shared across selected models")).toBeVisible();
    expect(screen.getByRole("radio", { name: /Low/ })).toBeEnabled();
    expect(screen.getByRole("radio", { name: /Medium/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Maximum/ })).toBeEnabled();
  });

  it("uses the same shell in Compare mode without a redundant mode switch", () => {
    useChatStore.setState({ mode: "compare" });
    render(<PromptComposer models={DEFAULT_MODELS} />);

    expect(useChatStore.getState().mode).toBe("compare");
    expect(screen.getByLabelText("Compare model selectors")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Smart routing" })).not.toBeInTheDocument();
    const optionsButton = screen.getByRole("button", { name: "More options" });
    const featureControls = document.querySelector("#promptFeatureControls");
    expect(screen.queryByRole("switch", { name: "Research mode" })).not.toBeInTheDocument();
    expect(featureControls).toContainElement(optionsButton);
    expect(
      screen.queryByRole("switch", { name: /Improve prompt/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Compare" })).not.toBeInTheDocument();

    const textarea = screen.getByRole("textbox", { name: "Prompt input" });
    const card = textarea.parentElement?.parentElement;
    expect(textarea).toHaveAttribute("placeholder", "Ask once and compare model responses");
    expect(textarea).toHaveAttribute("rows", "1");
    expect(card).toContainElement(screen.getByLabelText("Compare model selectors"));
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
    expect(screen.queryByRole("button", { name: "Turn off Improve prompt" })).not.toBeInTheDocument();
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
      expect(textarea.style.height).toBe("160px");
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
      matches:
        query === "(max-width: 900px)" || query === "(max-width: 767px)" ? matches : false,
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
