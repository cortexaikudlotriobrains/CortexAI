import { useCallback } from "react";
import { ApiClientError } from "../api/client";
import { streamChat } from "../api/chat";
import { streamCompare } from "../api/compare";
import { fetchHistory } from "../api/history";
import { optimizePrompt } from "../api/optimize";
import {
  buildOptimizeRequest,
  cancelledOptimization,
  pendingOptimization,
  resolveOptimizationFailure,
  resolveOptimizationResponse,
} from "../optimization/promptOptimization";
import { makePlaceholderResponse, useChatStore } from "../store/chatStore";
import { clearAttachmentUploads } from "../uploads/attachmentUploadQueue";
import { StreamDeltaBuffer } from "../streaming/streamDeltaBuffer";
import { terminalResponseText } from "../streaming/terminalResponse";
import {
  activityRunStatus,
  activityTargetIndexes,
  isNewActivityEvent,
} from "../streaming/activityPresentation";
import { isSubscriptionDenial, toSubscriptionError } from "../subscription/subscriptionErrors";
import { useAttachmentUploadStore } from "../store/attachmentUploadStore";
import { parseModelKey } from "./useSmartRouting";
import { presentError, userFacingMessage, type UserFacingError } from "../errors/userFacingError";
import type {
  ApiError,
  AttachmentRequestItem,
  ChatRequest,
  ChatResponse,
  ChatTurn,
  CompareTargetRequest,
  CortexActivityEvent,
  CompareRequest,
  ConversationHistoryItem,
  FileUploadResponse,
  GenerationProfile,
  PromptOptimizationState,
  ReasoningLevel,
  ResponseRunStatus,
  UserContextRequest,
} from "../types";
import {
  generationForReasoningLevel,
  reasoningLevelFromResponse,
} from "../reasoning/reasoningLevels";

const MANAGED_GENERATION_PROFILE: GenerationProfile = "auto";

let activeAbortController: AbortController | null = null;

interface SubmitOptions {
  promptOverride?: string;
  skipOptimization?: boolean;
  attachmentsOverride?: FileUploadResponse[];
  clearComposer?: boolean;
}

export function useChat() {
  const submit = useCallback(async (options: SubmitOptions = {}) => {
    const state = useChatStore.getState();
    const rawPrompt = (options.promptOverride ?? state.prompt).trim();
    const attachments = options.attachmentsOverride
      ? [...options.attachmentsOverride]
      : [...state.attachments];
    const clearComposer = options.clearComposer ?? !options.promptOverride;
    if (!rawPrompt && attachments.length === 0) return;
    if (state.mode === "compare" && state.compareModelKeys.filter(Boolean).length < 2) {
      state.setError(
        userFacingMessage("Choose another model", "Select at least two models to compare.", {
          code: "compare_requires_two_models",
          context: "chat",
        }),
      );
      return;
    }

    const composerSubmission = beginComposerSubmission({
      enabled: clearComposer,
      prompt: state.prompt,
      attachments: [...state.attachments],
    });
    const previousActiveTurnId = state.activeTurnId;
    const controller = beginRequestController();

    state.setError(null);
    state.setSubscriptionError(null);
    state.setStreamingText("");

    let finalPrompt = rawPrompt;
    let turnId: string | undefined;
    let optimization: PromptOptimizationState | undefined;
    const creditActivityId = createCreditActivityId();
    const requestStartedAt = new Date().toISOString();
    const attachmentItems = toAttachmentItems(attachments);
    const conversationHistory = buildConversationHistory(state.turns);
    const context = buildContext({
      sessionId: state.sessionId,
      pendingNewSession: state.pendingNewSession,
      conversationHistory,
    });

    try {
      if (!options.skipOptimization && state.optimizeMode && rawPrompt) {
        optimization = pendingOptimization(rawPrompt);
        turnId = state.beginTurn({
          mode: state.mode,
          prompt: rawPrompt,
          submittedPrompt: rawPrompt,
          researchEnabled: false,
          optimizeEnabled: true,
          attachments,
          responses: buildPlaceholdersForCurrentMode(state, requestStartedAt, "optimizing"),
          status: "optimizing",
          optimization,
        });
        try {
          const optimized = await optimizePrompt(
            buildOptimizeRequest({
              prompt: rawPrompt,
              conversationHistory,
              context,
              attachments,
              creditActivityId,
            }),
            controller.signal,
          );
          if (controller.signal.aborted) return;
          const resolved = resolveOptimizationResponse(optimized, rawPrompt);
          finalPrompt = resolved.finalPrompt;
          optimization = resolved.optimization;
        } catch (err) {
          if (controller.signal.aborted) return;
          if (isSubscriptionDenial(err)) throw toSubscriptionError(err);
          console.warn("Prompt optimization failed; continuing with original prompt", err);
          const resolved = resolveOptimizationFailure(rawPrompt);
          finalPrompt = resolved.finalPrompt;
          optimization = resolved.optimization;
        }

        useChatStore.getState().setTurnOptimization(turnId, optimization);
      }

      if (state.mode === "compare") {
        await runCompareTurn({
          prompt: finalPrompt,
          submittedPrompt: finalPrompt,
          context,
          attachmentItems,
          attachments,
          signal: controller.signal,
          turnId,
          optimization,
          creditActivityId,
          initialQuery: rawPrompt,
          startedAt: requestStartedAt,
          acceptComposer: composerSubmission.accept,
        });
      } else {
        await runAskTurn({
          prompt: finalPrompt,
          submittedPrompt: finalPrompt,
          context,
          attachmentItems,
          attachments,
          signal: controller.signal,
          turnId,
          optimization,
          creditActivityId,
          initialQuery: rawPrompt,
          startedAt: requestStartedAt,
          acceptComposer: composerSubmission.accept,
        });
      }
    } catch (err: unknown) {
      if (controller.signal.aborted) return;
      const latest = useChatStore.getState();
      if (isSubscriptionDenial(err)) {
        const denial = toSubscriptionError(err);
        if (latest.activeTurnId && latest.activeTurnId !== previousActiveTurnId) {
          latest.discardTurn(latest.activeTurnId);
        }
        latest.setSubscriptionError(denial);
        latest.setError(null);
        latest.setStreaming(false);
        return;
      }
      const visibleError = presentError(err, "chat");
      if (latest.activeTurnId) {
        markTurnResponsesFailed(latest.activeTurnId, visibleError);
        latest.setTurnStatus(latest.activeTurnId, "error");
        latest.setError(null);
      } else {
        latest.setError(visibleError);
      }
      latest.setStreaming(false);
    } finally {
      composerSubmission.restore();
      clearRequestController(controller);
    }
  }, []);

  const submitFollowUp = useCallback(
    async (suggestion: string) => {
      await submit({
        promptOverride: suggestion,
        skipOptimization: true,
        attachmentsOverride: [],
        clearComposer: false,
      });
    },
    [submit],
  );

  const regenerate = useCallback(
    async (turnId: string, responseIndex = 0, generationProfileOverride?: GenerationProfile) => {
      const state = useChatStore.getState();
      const sourceTurn = state.turns.find((turn) => turn.id === turnId);
      const sourceResponse = sourceTurn?.responses[responseIndex];
      const prompt = (sourceTurn?.submittedPrompt || sourceTurn?.prompt || "").trim();

      if (!sourceTurn || !sourceResponse || !prompt) return;

      const requestStartedAt = new Date().toISOString();
      const attachmentItems = toAttachmentItems(sourceTurn.attachments);
      const conversationHistory = buildRegenerationConversationHistory(state.turns, turnId);
      const context = buildContext({
        sessionId: state.sessionId,
        pendingNewSession: state.pendingNewSession,
        conversationHistory,
      });
      const target = responseToExplicitTarget(sourceResponse);
      const controller = beginRequestController();

      state.setError(null);
      state.setSubscriptionError(null);
      state.setStreamingText("");
      state.setStreaming(true);

      try {
        await runRegenerateResponse({
          turnId,
          responseIndex,
          submittedPrompt: prompt,
          context,
          attachmentItems,
          signal: controller.signal,
          startedAt: requestStartedAt,
          targetOverride: target,
          researchEnabledOverride: !!sourceTurn.researchEnabled,
          regenerationSourceRequestId:
            sourceTurn.mode === "compare" ? sourceResponse.request_id : undefined,
          generationProfileOverride,
          reasoningLevelOverride: reasoningLevelFromResponse(
            sourceResponse.generation_budget?.requested_reasoning_effort,
          ),
        });
      } catch (err: unknown) {
        if (controller.signal.aborted) return;
        const latest = useChatStore.getState();
        if (generationProfileOverride) {
          latest.updateTurnResponse(turnId, responseIndex, sourceResponse);
          latest.setTurnStatus(turnId, "complete");
        }
        if (isSubscriptionDenial(err)) {
          latest.setSubscriptionError(toSubscriptionError(err));
          latest.setError(null);
          latest.setStreaming(false);
          return;
        }
        const visibleError = presentError(err, "chat");
        if (generationProfileOverride) {
          latest.setError(
            userFacingMessage(
              "Retry couldn't finish",
              `The partial answer was kept. ${visibleError.message}`,
              {
                code: visibleError.code,
                retryable: visibleError.retryable,
                action: visibleError.retryable ? "retry" : "dismiss",
                actionLabel: visibleError.retryable ? "Try again" : undefined,
                requestId: visibleError.requestId,
                context: "chat",
              },
            ),
          );
          latest.setStreaming(false);
          return;
        }
        if (latest.activeTurnId) {
          markTurnResponsesFailed(latest.activeTurnId, visibleError);
          latest.setTurnStatus(latest.activeTurnId, "error");
          latest.setError(null);
        } else {
          latest.setError(visibleError);
        }
        latest.setStreaming(false);
      } finally {
        clearRequestController(controller);
      }
    },
    [],
  );

  const retryWithMoreRoom = useCallback(
    async (turnId: string, responseIndex = 0) => {
      const state = useChatStore.getState();
      const response = state.turns.find((turn) => turn.id === turnId)?.responses[responseIndex];
      const profile = response?.retry_with_more_room?.recommended_profile;
      if (!profile) return;
      await regenerate(turnId, responseIndex, profile);
    },
    [regenerate],
  );

  const cancel = useCallback(() => {
    activeAbortController?.abort();
    activeAbortController = null;
    const state = useChatStore.getState();
    if (state.activeTurnId) {
      const activeTurn = state.turns.find((turn) => turn.id === state.activeTurnId);
      if (activeTurn?.status === "optimizing" && activeTurn.optimization) {
        state.setTurnOptimization(
          state.activeTurnId,
          cancelledOptimization(activeTurn.optimization.originalPrompt),
        );
      }
      const cancelledAt = new Date().toISOString();
      activeTurn?.responses.forEach((response, index) => {
        if (
          response.ui_status === "complete" ||
          response.ui_status === "failed" ||
          response.ui_status === "cancelled"
        ) {
          return;
        }
        state.updateTurnResponse(state.activeTurnId!, index, {
          ...response,
          ui_status: "cancelled",
          cancelled_at: cancelledAt,
          activity: {
            request_id: response.activity?.request_id ?? response.request_id,
            conversation_id: response.activity?.conversation_id ?? response.session_id,
            provider: response.provider,
            model: response.model,
            mode: activeTurn.mode === "compare" ? "compare" : "chat",
            event_type: "REQUEST_CANCELLED",
            phase: "cancelled",
            display_message: "Generation stopped",
            timestamp: cancelledAt,
            sequence_number: (response.activity?.sequence_number ?? 0) + 1,
            metadata: {},
          },
        });
      });
      state.setTurnStatus(state.activeTurnId, "cancelled");
    }
    state.setStreaming(false);
  }, []);

  return { submit, submitFollowUp, regenerate, retryWithMoreRoom, cancel };
}

async function runAskTurn({
  prompt,
  submittedPrompt,
  context,
  attachmentItems,
  attachments,
  signal,
  turnId,
  optimization,
  creditActivityId,
  initialQuery,
  startedAt,
  targetOverride,
  acceptComposer,
}: {
  prompt: string;
  submittedPrompt: string;
  context: UserContextRequest;
  attachmentItems: AttachmentRequestItem[];
  attachments: FileUploadResponse[];
  signal: AbortSignal;
  turnId?: string;
  optimization?: PromptOptimizationState;
  creditActivityId: string;
  initialQuery: string;
  startedAt: string;
  targetOverride?: Partial<CompareTargetRequest>;
  researchEnabledOverride?: boolean;
  acceptComposer: () => void;
}) {
  const state = useChatStore.getState();
  const selected = parseModelKey(state.selectedModelKey);
  const provider = targetOverride?.provider ?? selected.provider;
  const model = targetOverride?.model ?? selected.model;
  const smartMode = targetOverride?.provider ? false : state.smartMode;
  const researchEnabled = false;
  const request: ChatRequest = {
    prompt: submittedPrompt,
    credit_activity_id: creditActivityId,
    initial_query: initialQuery,
    provider: smartMode ? undefined : provider || undefined,
    model: smartMode ? undefined : model || undefined,
    routing: { smart_mode: smartMode, web_mode: "auto" },
    generation: generationForReasoningLevel(state.askReasoningLevel, MANAGED_GENERATION_PROFILE),
    attachments: attachmentItems.length > 0 ? attachmentItems : undefined,
    context,
  };

  const placeholder = makePlaceholderResponse(
    0,
    smartMode ? "smart" : provider,
    smartMode ? "Selecting best model" : model,
    state.sessionId,
    { startedAt },
  );
  const activeTurnId =
    turnId ??
    state.beginTurn({
      mode: "single",
      prompt,
      submittedPrompt,
      researchEnabled,
      optimizeEnabled: !!optimization,
      attachments,
      responses: [placeholder],
    });
  if (turnId) {
    state.prepareTurnForStreaming(turnId, {
      prompt,
      submittedPrompt,
      responses: [placeholder],
      optimization,
    });
  }

  let finalResponse: Partial<ChatResponse> = {};
  const deltaBuffer = new StreamDeltaBuffer(activeTurnId);
  // finally guarantees no orphaned timeout can mutate a turn after the
  // cancel/error path has settled it.
  try {
    for await (const chunk of streamChat(request, signal)) {
      if (signal.aborted) break;
      acceptComposer();
      if (chunk.type === "delta" && chunk.text) {
        deltaBuffer.append(0, chunk.text);
        continue;
      }
      deltaBuffer.flush();
      const latest = useChatStore.getState();
      if (chunk.type === "activity" && chunk.activity) {
        applyActivityToResponse(latest, activeTurnId, 0, chunk.activity, placeholder);
      } else if (!smartMode && chunk.type === "start" && (chunk.provider || chunk.model)) {
        const current =
          latest.turns.find((turn) => turn.id === activeTurnId)?.responses[0] ??
          latest.responses[0] ??
          placeholder;
        latest.updateTurnResponse(activeTurnId, 0, {
          ...current,
          provider: chunk.provider ?? current.provider,
          model: chunk.model ?? current.model,
          ui_status: "requesting",
        });
      } else if (chunk.type === "start") {
        const current =
          latest.turns.find((turn) => turn.id === activeTurnId)?.responses[0] ??
          latest.responses[0] ??
          placeholder;
        latest.updateTurnResponse(activeTurnId, 0, {
          ...current,
          ui_status: "requesting",
        });
      } else if (chunk.type === "metadata" && chunk.metadata) {
        finalResponse = { ...finalResponse, ...chunk.metadata };
        const current =
          latest.turns.find((turn) => turn.id === activeTurnId)?.responses[0] ??
          latest.responses[0] ??
          placeholder;
        latest.updateTurnResponse(activeTurnId, 0, {
          ...current,
          ...finalResponse,
          text: terminalResponseText(chunk.metadata.text, current.text),
          started_at: current.started_at ?? startedAt,
          ui_status: finalResponse.error ? "failed" : "finalizing",
          failed_at: finalResponse.error ? new Date().toISOString() : current.failed_at,
        } as ChatResponse);
      } else if (chunk.type === "done") {
        if (chunk.session_id) latest.setSessionId(chunk.session_id);
        break;
      } else if (chunk.type === "error") {
        throw streamError(chunk.error);
      }
    }
    if (!signal.aborted) acceptComposer();
  } finally {
    if (signal.aborted) {
      deltaBuffer.dispose();
    } else {
      deltaBuffer.flush();
    }
  }

  const latest = useChatStore.getState();
  if (!signal.aborted) {
    const current = latest.responses[0] ?? placeholder;
    const completedAt = new Date().toISOString();
    const completedResponse = {
      ...current,
      ...finalResponse,
      text: terminalResponseText(finalResponse.text, current.text),
      session_id: finalResponse.session_id ?? latest.sessionId ?? current.session_id,
      started_at: current.started_at ?? startedAt,
      completed_at: finalResponse.error ? current.completed_at : completedAt,
      failed_at: finalResponse.error ? (current.failed_at ?? completedAt) : current.failed_at,
      ui_status: finalResponse.error
        ? "failed"
        : finalResponse.completion_status === "incomplete"
          ? "incomplete"
          : "complete",
    } as ChatResponse;
    latest.updateTurnResponse(activeTurnId, 0, completedResponse);
    const resolvedSession = completedResponse.session_id;
    if (resolvedSession) latest.setSessionId(resolvedSession);
    latest.setTurnStatus(activeTurnId, "complete");
    latest.setStreaming(false);
    await refreshHistory();
  }
}

async function runCompareTurn({
  prompt,
  submittedPrompt,
  context,
  attachmentItems,
  attachments,
  signal,
  turnId,
  optimization,
  creditActivityId,
  initialQuery,
  startedAt,
  acceptComposer,
}: {
  prompt: string;
  submittedPrompt: string;
  context: UserContextRequest;
  attachmentItems: AttachmentRequestItem[];
  attachments: FileUploadResponse[];
  signal: AbortSignal;
  turnId?: string;
  optimization?: PromptOptimizationState;
  creditActivityId: string;
  initialQuery: string;
  startedAt: string;
  acceptComposer: () => void;
}) {
  const state = useChatStore.getState();
  const activeKeys = state.compareModelKeys.filter(Boolean);
  if (activeKeys.length < 2) {
    state.setError(
      userFacingMessage("Choose another model", "Select at least two models to compare.", {
        code: "compare_requires_two_models",
        context: "chat",
      }),
    );
    state.setStreaming(false);
    return;
  }

  const targets = activeKeys.map((key) => {
    const { provider, model } = parseModelKey(key);
    return { provider, model: model || undefined };
  });
  const researchEnabled = false;
  const placeholders = targets.map((target, index) =>
    makePlaceholderResponse(index, target.provider, target.model ?? "", state.sessionId, {
      startedAt,
    }),
  );
  const request: CompareRequest = {
    prompt: submittedPrompt,
    credit_activity_id: creditActivityId,
    initial_query: initialQuery,
    targets,
    routing: { smart_mode: false, web_mode: "auto" },
    generation: generationForReasoningLevel(
      state.compareReasoningLevel,
      MANAGED_GENERATION_PROFILE,
    ),
    attachments: attachmentItems.length > 0 ? attachmentItems : undefined,
    context,
  };

  const activeTurnId =
    turnId ??
    state.beginTurn({
      mode: "compare",
      prompt,
      submittedPrompt,
      researchEnabled,
      optimizeEnabled: !!optimization,
      attachments,
      responses: placeholders,
    });
  if (turnId) {
    state.prepareTurnForStreaming(turnId, {
      prompt,
      submittedPrompt,
      responses: placeholders,
      optimization,
    });
  }

  const deltaBuffer = new StreamDeltaBuffer(activeTurnId);
  try {
    for await (const chunk of streamCompare(request, signal)) {
      if (signal.aborted) break;
      acceptComposer();
      const index = chunk.index ?? 0;
      if (chunk.type === "delta" && chunk.text) {
        deltaBuffer.append(index, chunk.text);
        continue;
      }
      deltaBuffer.flush();
      const latest = useChatStore.getState();
      if (chunk.type === "activity" && chunk.activity) {
        const responseCount =
          latest.turns.find((turn) => turn.id === activeTurnId)?.responses.length ??
          placeholders.length;
        const activityIndexes = activityTargetIndexes(chunk.index, responseCount);
        for (const activityIndex of activityIndexes) {
          applyActivityToResponse(
            latest,
            activeTurnId,
            activityIndex,
            chunk.activity,
            placeholders[activityIndex],
          );
        }
      } else if (chunk.type === "response_start") {
        const current = getTurnResponse(latest, activeTurnId, index, placeholders[index]);
        latest.updateTurnResponse(activeTurnId, index, {
          ...current,
          provider:
            chunk.provider ?? latest.responses[index]?.provider ?? targets[index]?.provider ?? "",
          model: chunk.model ?? latest.responses[index]?.model ?? targets[index]?.model ?? "",
          ui_status: "requesting",
        });
      } else if (chunk.type === "response_done" && chunk.response) {
        const current = getTurnResponse(latest, activeTurnId, index, placeholders[index]);
        const completedAt = new Date().toISOString();
        const failed = !!chunk.response.error;
        latest.updateTurnResponse(activeTurnId, index, {
          ...current,
          ...chunk.response,
          text: terminalResponseText(chunk.response.text, current.text),
          started_at: current.started_at ?? startedAt,
          completed_at: failed ? current.completed_at : completedAt,
          failed_at: failed ? (current.failed_at ?? completedAt) : current.failed_at,
          ui_status: failed
            ? "failed"
            : chunk.response.completion_status === "incomplete"
              ? "incomplete"
              : "complete",
        });
      } else if (chunk.type === "done") {
        if (chunk.compare) latest.setTurnCompareSummary(activeTurnId, chunk.compare);
        if (chunk.session_id) latest.setSessionId(chunk.session_id);
        break;
      } else if (chunk.type === "error") {
        throw streamError(chunk.error);
      }
    }
    if (!signal.aborted) acceptComposer();
  } finally {
    if (signal.aborted) {
      deltaBuffer.dispose();
    } else {
      deltaBuffer.flush();
    }
  }

  const latest = useChatStore.getState();
  if (!signal.aborted) {
    latest.setTurnStatus(activeTurnId, "complete");
    latest.setStreaming(false);
    await refreshHistory();
  }
}

async function runRegenerateResponse({
  turnId,
  responseIndex,
  submittedPrompt,
  context,
  attachmentItems,
  signal,
  startedAt,
  targetOverride,
  regenerationSourceRequestId,
  generationProfileOverride,
  reasoningLevelOverride,
}: {
  turnId: string;
  responseIndex: number;
  submittedPrompt: string;
  context: UserContextRequest;
  attachmentItems: AttachmentRequestItem[];
  signal: AbortSignal;
  startedAt: string;
  targetOverride?: Partial<CompareTargetRequest>;
  researchEnabledOverride?: boolean;
  regenerationSourceRequestId?: string;
  generationProfileOverride?: GenerationProfile;
  reasoningLevelOverride: ReasoningLevel;
}) {
  const state = useChatStore.getState();
  const selected = parseModelKey(state.selectedModelKey);
  const provider = targetOverride?.provider ?? selected.provider;
  const model = targetOverride?.model ?? selected.model;
  const smartMode = targetOverride?.provider ? false : state.smartMode;
  const request: ChatRequest = {
    prompt: submittedPrompt,
    credit_activity_id: createCreditActivityId(),
    initial_query: submittedPrompt,
    provider: smartMode ? undefined : provider || undefined,
    model: smartMode ? undefined : model || undefined,
    routing: { smart_mode: smartMode, web_mode: "auto" },
    generation: generationForReasoningLevel(
      reasoningLevelOverride,
      generationProfileOverride ?? MANAGED_GENERATION_PROFILE,
    ),
    attachments: attachmentItems.length > 0 ? attachmentItems : undefined,
    context,
    regeneration: regenerationSourceRequestId
      ? {
          source_request_id: regenerationSourceRequestId,
          retry_reason: generationProfileOverride ? "output_limit" : undefined,
        }
      : undefined,
  };

  const placeholder = makePlaceholderResponse(
    responseIndex,
    smartMode ? "smart" : provider,
    smartMode ? "Selecting best model" : model,
    state.sessionId,
    { startedAt },
  );

  let finalResponse: Partial<ChatResponse> = {};
  const deltaBuffer = new StreamDeltaBuffer(turnId);
  let prepared = false;
  try {
    for await (const chunk of streamChat(request, signal)) {
      if (signal.aborted) break;
      if (!prepared) {
        useChatStore.getState().prepareTurnResponseForStreaming(turnId, responseIndex, placeholder);
        prepared = true;
      }
      if (chunk.type === "delta" && chunk.text) {
        deltaBuffer.append(responseIndex, chunk.text);
        continue;
      }
      deltaBuffer.flush();
      const latest = useChatStore.getState();
      if (chunk.type === "activity" && chunk.activity) {
        applyActivityToResponse(latest, turnId, responseIndex, chunk.activity, placeholder);
      } else if (!smartMode && chunk.type === "start" && (chunk.provider || chunk.model)) {
        const current = getTurnResponse(latest, turnId, responseIndex, placeholder);
        latest.updateTurnResponse(turnId, responseIndex, {
          ...current,
          provider: chunk.provider ?? current.provider,
          model: chunk.model ?? current.model,
          ui_status: "requesting",
        });
      } else if (chunk.type === "start") {
        const current = getTurnResponse(latest, turnId, responseIndex, placeholder);
        latest.updateTurnResponse(turnId, responseIndex, {
          ...current,
          ui_status: "requesting",
        });
      } else if (chunk.type === "metadata" && chunk.metadata) {
        finalResponse = { ...finalResponse, ...chunk.metadata };
        const current = getTurnResponse(latest, turnId, responseIndex, placeholder);
        latest.updateTurnResponse(turnId, responseIndex, {
          ...current,
          ...finalResponse,
          text: chunk.metadata.text ?? current.text,
          started_at: current.started_at ?? startedAt,
          ui_status: finalResponse.error ? "failed" : "finalizing",
          failed_at: finalResponse.error ? new Date().toISOString() : current.failed_at,
        } as ChatResponse);
      } else if (chunk.type === "done") {
        if (chunk.session_id) latest.setSessionId(chunk.session_id);
        break;
      } else if (chunk.type === "error") {
        throw streamError(chunk.error);
      }
    }
  } finally {
    if (signal.aborted) {
      deltaBuffer.dispose();
    } else {
      deltaBuffer.flush();
    }
  }

  const latest = useChatStore.getState();
  if (!signal.aborted) {
    const current = getTurnResponse(latest, turnId, responseIndex, placeholder);
    const completedAt = new Date().toISOString();
    const completedResponse = {
      ...current,
      ...finalResponse,
      text: finalResponse.text ?? current.text,
      session_id: finalResponse.session_id ?? latest.sessionId ?? current.session_id,
      started_at: current.started_at ?? startedAt,
      completed_at: finalResponse.error ? current.completed_at : completedAt,
      failed_at: finalResponse.error ? (current.failed_at ?? completedAt) : current.failed_at,
      ui_status: finalResponse.error
        ? "failed"
        : finalResponse.completion_status === "incomplete"
          ? "incomplete"
          : "complete",
    } as ChatResponse;
    latest.updateTurnResponse(turnId, responseIndex, completedResponse);
    const resolvedSession = completedResponse.session_id;
    if (resolvedSession) latest.setSessionId(resolvedSession);
    latest.setTurnStatus(turnId, "complete");
    latest.setStreaming(false);
    await refreshHistory();
  }
}

function buildContext({
  sessionId,
  pendingNewSession,
  conversationHistory,
}: {
  sessionId: string | null;
  pendingNewSession: boolean;
  conversationHistory: ConversationHistoryItem[];
}): UserContextRequest {
  return {
    session_id: pendingNewSession ? undefined : (sessionId ?? undefined),
    conversation_history: conversationHistory.length > 0 ? conversationHistory : undefined,
    new_session: pendingNewSession || !sessionId,
  };
}

type ChatStateSnapshot = ReturnType<typeof useChatStore.getState>;

function applyActivityToResponse(
  state: ChatStateSnapshot,
  turnId: string,
  index: number,
  activity: CortexActivityEvent,
  fallback?: ChatResponse,
) {
  const current = getTurnResponse(state, turnId, index, fallback);
  if (!isNewActivityEvent(current.activity, activity)) return;
  if (
    current.ui_status === "complete" ||
    current.ui_status === "failed" ||
    current.ui_status === "cancelled"
  ) {
    return;
  }
  state.updateTurnResponse(turnId, index, {
    ...current,
    provider: activity.provider || current.provider,
    model: activity.model || current.model,
    activity,
    ui_status: activityRunStatus(activity),
  });
}

function buildPlaceholdersForCurrentMode(
  state: ChatStateSnapshot,
  startedAt: string,
  status: ResponseRunStatus,
): ChatResponse[] {
  if (state.mode === "compare") {
    return state.compareModelKeys.filter(Boolean).map((key, index) => {
      const { provider, model } = parseModelKey(key);
      return makePlaceholderResponse(index, provider, model, state.sessionId, {
        startedAt,
        status,
      });
    });
  }

  const { provider, model } = parseModelKey(state.selectedModelKey);
  return [
    makePlaceholderResponse(
      0,
      state.smartMode ? "smart" : provider,
      state.smartMode ? "Selecting best model" : model,
      state.sessionId,
      { startedAt, status },
    ),
  ];
}

function getTurnResponse(
  state: ChatStateSnapshot,
  turnId: string,
  index: number,
  fallback?: ChatResponse,
): ChatResponse {
  return (
    state.turns.find((turn) => turn.id === turnId)?.responses[index] ??
    state.responses[index] ??
    fallback ??
    makePlaceholderResponse(index, "", "", state.sessionId)
  );
}

function markTurnResponsesFailed(turnId: string, visibleError: UserFacingError) {
  const state = useChatStore.getState();
  const turn = state.turns.find((item) => item.id === turnId);
  if (!turn) return;

  const failedAt = new Date().toISOString();
  const responses =
    turn.responses.length > 0
      ? turn.responses
      : [makePlaceholderResponse(0, "auto", "Working", state.sessionId)];

  responses.forEach((response, index) => {
    if (
      response.ui_status === "complete" ||
      response.ui_status === "failed" ||
      response.ui_status === "cancelled"
    ) {
      return;
    }
    state.updateTurnResponse(turnId, index, {
      ...response,
      error: response.error ?? makeUiError(response, visibleError),
      ui_status: "failed",
      failed_at: failedAt,
      started_at: response.started_at ?? failedAt,
    });
  });
}

function makeUiError(response: ChatResponse, visibleError: UserFacingError): ApiError {
  return {
    code: visibleError.code || "stream_error",
    message: visibleError.message,
    provider: response.provider,
    retryable: visibleError.retryable,
    details: {
      user_title: visibleError.title,
      request_id: visibleError.requestId,
    },
  };
}

export function buildConversationHistory(turns: ChatTurn[]): ConversationHistoryItem[] {
  const messages: ConversationHistoryItem[] = [];
  for (const turn of turns) {
    if (
      turn.status === "streaming" ||
      turn.status === "optimizing" ||
      turn.status === "cancelled"
    ) {
      continue;
    }
    const prompt = turn.submittedPrompt || turn.prompt;
    if (prompt) messages.push({ role: "user", content: prompt });
    const responseText = turn.responses
      .map((response) => response.text)
      .filter(Boolean)
      .join("\n\n");
    if (responseText) messages.push({ role: "assistant", content: responseText });
  }
  return messages.slice(-10);
}

function toAttachmentItems(attachments: FileUploadResponse[]): AttachmentRequestItem[] {
  return attachments.map((attachment) => ({
    file_id: attachment.file_id,
    usage_role: "primary",
    transform_mode: "auto",
  }));
}

function beginComposerSubmission({
  enabled,
  prompt,
  attachments,
}: {
  enabled: boolean;
  prompt: string;
  attachments: FileUploadResponse[];
}): { accept: () => void; restore: () => void } {
  if (!enabled) return { accept: () => undefined, restore: () => undefined };

  // Hand the prompt off to the optimistic turn immediately, but keep a local
  // snapshot until the server accepts the stream so pre-stream failures remain editable.
  let pending = true;
  const uploadTasks = [...useAttachmentUploadStore.getState().tasks];
  const state = useChatStore.getState();
  void clearAttachmentUploads({ deleteRemote: false });
  state.setPrompt("");
  state.clearAttachments();

  return {
    accept: () => {
      pending = false;
    },
    restore: () => {
      if (!pending) return;
      pending = false;

      const latest = useChatStore.getState();
      if (!latest.prompt) latest.setPrompt(prompt);
      for (const attachment of attachments) latest.addAttachment(attachment);

      const uploadState = useAttachmentUploadStore.getState();
      const currentTaskIds = new Set(uploadState.tasks.map((task) => task.clientId));
      const missingTasks = uploadTasks.filter((task) => !currentTaskIds.has(task.clientId));
      if (missingTasks.length > 0) uploadState.addTasks(missingTasks);
    },
  };
}

async function refreshHistory() {
  const state = useChatStore.getState();
  try {
    const entries = await fetchHistory(500);
    state.setHistory(entries);
  } catch (err) {
    console.warn("History refresh failed", err);
  }
}

function streamError(rawMessage?: string): ApiClientError {
  return new ApiClientError(0, rawMessage ?? "Stream error", undefined, {
    code: "stream_error",
    retryable: true,
  });
}

function createCreditActivityId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `credit-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function beginRequestController(): AbortController {
  activeAbortController?.abort();
  const controller = new AbortController();
  activeAbortController = controller;
  return controller;
}

function clearRequestController(controller: AbortController) {
  if (activeAbortController === controller) activeAbortController = null;
}

function buildRegenerationConversationHistory(
  turns: ChatTurn[],
  turnId: string,
): ConversationHistoryItem[] {
  const turnIndex = turns.findIndex((turn) => turn.id === turnId);
  return buildConversationHistory(turnIndex >= 0 ? turns.slice(0, turnIndex) : turns);
}

function responseToExplicitTarget(response: ChatResponse): Partial<CompareTargetRequest> {
  const provider = response.provider.trim().toLowerCase();
  const model = response.model.trim();
  const nonConcreteProviders = new Set(["", "auto", "smart", "unknown"]);
  const nonConcreteModels = new Set(["", "working", "selecting best model", "unknown"]);

  if (nonConcreteProviders.has(provider) || nonConcreteModels.has(model.trim().toLowerCase())) {
    return {};
  }
  return { provider, model };
}
