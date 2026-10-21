import { useRef, useEffect, useLayoutEffect, useCallback, useMemo, useState } from "react";
import { ModelSelector } from "./ModelSelector";
import { CompareSelector } from "./CompareSelector";
import { FeatureChips } from "./FeatureChips";
import { ComposerOptionsMenu } from "./ComposerOptionsMenu";
import { AttachmentStrip } from "./AttachmentStrip";
import { useChatStore } from "../../store/chatStore";
import { useAttachmentUploadStore } from "../../store/attachmentUploadStore";
import { attachmentUploadsBlockSubmission } from "../../uploads/attachmentUploadQueue";
import { userFacingMessage } from "../../errors/userFacingError";
import { useChat } from "../../hooks/useChat";
import type { ModelCatalogItem } from "../../types";
import type { UseSubscriptionResult } from "../../hooks/useSubscription";
import { DEFAULT_MODELS } from "../../config/defaultModels";
import { resolveAskModelKey } from "../../config/askDefaults";
import {
  DEFAULT_COMPARE_MODEL_KEYS,
  newCompareModelKeys,
  resolveCompareModelKeys,
} from "../../config/compareDefaults";
import { resolveNewChatStart, useComposerPrefsStore } from "../../store/composerPrefsStore";
import {
  compareReasoningLevels,
  modelForKey,
  modelReasoningLevels,
  smartReasoningLevels,
} from "../../reasoning/reasoningLevels";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./PromptComposer.module.css";
import {
  allowanceAccessError,
  compareTargetAccessError,
  featureAccessError,
  modelAccessError,
  requiredPlanForModel,
  submitAccessError,
} from "../../subscription/subscriptionAccess";
import { detailString } from "../../subscription/subscriptionErrors";

interface PromptComposerProps {
  models: ModelCatalogItem[];
  modelsLoading?: boolean;
  subscription?: Pick<UseSubscriptionResult, "plans" | "entitlements"> &
    Partial<Pick<UseSubscriptionResult, "loading">>;
}

export function PromptComposer({
  models,
  modelsLoading = false,
  subscription,
}: PromptComposerProps) {
  const availableModels = models.length > 0 ? models : DEFAULT_MODELS;
  const entitlements = subscription?.entitlements ?? null;
  const plans = subscription?.plans ?? null;
  const subscriptionLoading = subscription?.loading ?? false;
  const subscriptionPending = Boolean(subscription && !entitlements);
  const mode = useChatStore((s) => s.mode);
  const smartMode = useChatStore((s) => s.smartMode);
  const setSmartMode = useChatStore((s) => s.setSmartMode);
  const optimizeMode = useChatStore((s) => s.optimizeMode);
  const setOptimizeMode = useChatStore((s) => s.setOptimizeMode);
  const askReasoningLevel = useChatStore((s) => s.askReasoningLevel);
  const setAskReasoningLevel = useChatStore((s) => s.setAskReasoningLevel);
  const compareReasoningLevel = useChatStore((s) => s.compareReasoningLevel);
  const setCompareReasoningLevel = useChatStore((s) => s.setCompareReasoningLevel);
  const selectedModelKey = useChatStore((s) => s.selectedModelKey);
  const setSelectedModelKey = useChatStore((s) => s.setSelectedModelKey);
  const setResolvedModelKey = useChatStore((s) => s.setResolvedModelKey);
  const prefs = useComposerPrefsStore((s) => s.prefs);
  const updatePrefs = useComposerPrefsStore((s) => s.updatePrefs);
  const composerInitialized = useChatStore((s) => s.composerInitialized);
  const compareHasSent = useChatStore((s) => s.compareHasSent);
  const sessionId = useChatStore((s) => s.sessionId);
  const hasTurns = useChatStore((s) => s.turns.length > 0);
  const compareModelKeys = useChatStore((s) => s.compareModelKeys);
  const setCompareModelKey = useChatStore((s) => s.setCompareModelKey);
  const prompt = useChatStore((s) => s.prompt);
  const setPrompt = useChatStore((s) => s.setPrompt);
  const attachments = useChatStore((s) => s.attachments);
  const streaming = useChatStore((s) => s.streaming);
  const setError = useChatStore((s) => s.setError);
  const setSubscriptionError = useChatStore((s) => s.setSubscriptionError);
  const subscriptionError = useChatStore((s) => s.subscriptionError);
  const uploadTasks = useAttachmentUploadStore((s) => s.tasks);

  const { submit, cancel } = useChat();
  const cardRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [openPopover, setOpenPopover] = useState<null | "model" | "options">(null);
  const setOptionsOpen = useCallback(
    (open: boolean) => setOpenPopover(open ? "options" : null),
    [],
  );
  const setModelOpen = useCallback((open: boolean) => setOpenPopover(open ? "model" : null), []);
  const [isTall, setIsTall] = useState(false);
  const mirrorRef = useRef<HTMLSpanElement>(null);
  const modelRef = useRef<HTMLDivElement>(null);
  const chipsRef = useRef<HTMLDivElement>(null);
  const [optionsAnnouncement, setOptionsAnnouncement] = useState("");
  const uploadsPending =
    attachmentUploadsBlockSubmission(uploadTasks) ||
    attachments.some((attachment) => attachment.status !== "ready");
  const uploadWaitMessage = "Waiting for attachments to finish uploading";
  const uploadWaitError = userFacingMessage(
    "Attachments are still uploading",
    "Wait for every file to finish, then send your message.",
    { code: "attachments_pending", context: "chat" },
  );

  const lockedModelKeys = useMemo(
    () =>
      availableModels
        .filter((model) => modelAccessError(model, entitlements, plans) !== null)
        .map((model) => `${model.provider}:${model.model}`),
    [availableModels, entitlements, plans],
  );
  const lockedModelLabels = Object.fromEntries(
    availableModels.map((model) => {
      const required = requiredPlanForModel(model.billing_class, entitlements, plans);
      return [`${model.provider}:${model.model}`, required ? capitalize(required) : "Unavailable"];
    }),
  );
  const plan = useMemo(
    () => ({ code: entitlements?.plan.code ?? "free", lockedModelKeys }),
    [entitlements?.plan.code, lockedModelKeys],
  );
  const newChatStart = resolveNewChatStart(prefs, plan, availableModels);
  const improveFeatureError = featureAccessError("prompt_improvement", entitlements, plans);
  const improveAllowanceError = allowanceAccessError("ai_credits", 1, entitlements, plans);
  const thirdTargetError = compareTargetAccessError(3, entitlements, plans);

  const handleSubmit = () => {
    if (uploadsPending) {
      setError(uploadWaitError);
      return;
    }
    const accessError = submitAccessError({
      mode,
      smartMode,
      selectedModelKey,
      compareModelKeys,
      models: availableModels,
      researchEnabled: true,
      optimizeEnabled: optimizeMode,
      attachmentCount: attachments.length,
      entitlements,
      plans,
    });
    if (accessError) {
      setSubscriptionError(accessError);
      return;
    }
    setSubscriptionError(null);
    void submit();
  };

  const handleLockedModel = (key: string) => {
    const model = availableModels.find(
      (candidate) => `${candidate.provider}:${candidate.model}` === key,
    );
    const accessError = modelAccessError(model, entitlements, plans);
    if (accessError) setSubscriptionError(accessError);
  };

  // Always measure against the compact grid, including while the text spans both rows.
  const measure = useCallback(() => {
    const el = textareaRef.current;
    const body = el?.parentElement;
    if (!el || !body) return;
    const mobile = window.matchMedia?.("(max-width: 760px)").matches ?? false;
    const chipWidths = Array.from(
      chipsRef.current?.querySelectorAll<HTMLElement>("[data-option-chip]") ?? [],
    ).reduce((total, chip, index) => total + chip.offsetWidth + (index ? 6 : 0), 0);
    const width =
      body.clientWidth -
      18 -
      34 -
      34 -
      38 -
      30 -
      (modelRef.current?.offsetWidth ?? 100) -
      chipWidths;
    const tall =
      !mobile &&
      (prompt.includes("\n") ||
        (mirrorRef.current?.getBoundingClientRect().width ?? 0) > Math.max(0, width - 12));
    setIsTall(tall);
    el.style.height = "0px";
    const height = mobile || tall ? Math.min(Math.max(el.scrollHeight, 34), 168) : 34;
    el.style.height = `${height}px`;
    el.style.overflowY = el.scrollHeight > 168 ? "auto" : "hidden";
  }, [prompt]);
  useLayoutEffect(() => {
    measure();
  }, [
    measure,
    isTall,
    selectedModelKey,
    smartMode,
    optimizeMode,
    askReasoningLevel,
    compareReasoningLevel,
    mode,
  ]);
  useEffect(() => {
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (cardRef.current) observer?.observe(cardRef.current);
    if (modelRef.current) observer?.observe(modelRef.current);
    if (chipsRef.current) observer?.observe(chipsRef.current);
    window.addEventListener("resize", measure);
    let active = true;
    void document.fonts?.ready.then(() => {
      if (active) measure();
    });
    return () => {
      active = false;
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure]);

  useEffect(() => {
    if (subscriptionLoading || subscriptionPending || modelsLoading || availableModels.length === 0)
      return;
    if (sessionId && !composerInitialized && !hasTurns) return;
    const eligible = availableModels.filter(
      (model) =>
        model.enabled &&
        model.selectable !== false &&
        !lockedModelKeys.includes(`${model.provider}:${model.model}`),
    );
    if (!composerInitialized) {
      useComposerPrefsStore.getState().migrate(plan.code, availableModels);
      const start = resolveNewChatStart(
        useComposerPrefsStore.getState().prefs,
        plan,
        availableModels,
      );
      const askKey = start === "smart" ? resolveAskModelKey(eligible, "", plan.code, null) : start;
      const compareStart = start === "smart" ? DEFAULT_COMPARE_MODEL_KEYS[0] : start;
      const compare = newCompareModelKeys(eligible, compareStart, eligible);
      useChatStore.setState({
        composerInitialized: true,
        smartMode: start === "smart",
        selectedModelKey: selectedModelKey || askKey,
        compareModelKeys: compareModelKeys.some(Boolean) ? compareModelKeys : compare,
        comparePrefsModel: start,
      });
    } else {
      const resolvedAsk = resolveAskModelKey(
        availableModels,
        selectedModelKey,
        plan.code,
        entitlements?.model_access.allowed_billing_classes ?? null,
      );
      if (resolvedAsk !== selectedModelKey) setResolvedModelKey(resolvedAsk);
      if (!compareHasSent) {
        const resolved = resolveCompareModelKeys(availableModels, compareModelKeys, eligible);
        if (resolved.some((key, index) => key !== compareModelKeys[index]))
          useChatStore.setState({ compareModelKeys: resolved });
      }
    }
  }, [
    availableModels,
    composerInitialized,
    compareHasSent,
    sessionId,
    hasTurns,
    compareModelKeys,
    entitlements,
    modelsLoading,
    selectedModelKey,
    setResolvedModelKey,
    subscriptionLoading,
    subscriptionPending,
    lockedModelKeys,
    plan,
  ]);

  const selectedModel = modelForKey(selectedModelKey, availableModels);
  const supportedReasoningLevels =
    mode === "compare"
      ? compareReasoningLevels(compareModelKeys, availableModels)
      : smartMode
        ? smartReasoningLevels(availableModels)
        : modelReasoningLevels(selectedModel);
  const reasoningLevel = mode === "compare" ? compareReasoningLevel : askReasoningLevel;
  const reasoningUnavailable =
    supportedReasoningLevels.length === 0 && (mode === "compare" || !smartMode);

  useEffect(() => {
    if (mode === "compare") {
      if (
        compareReasoningLevel !== "auto" &&
        !supportedReasoningLevels.includes(compareReasoningLevel)
      ) {
        setCompareReasoningLevel("auto");
      }
      return;
    }
    if (smartMode) {
      if (askReasoningLevel !== "auto" && !supportedReasoningLevels.includes(askReasoningLevel)) {
        setAskReasoningLevel("auto");
      }
      return;
    }
    if (askReasoningLevel !== "auto" && !supportedReasoningLevels.includes(askReasoningLevel)) {
      setAskReasoningLevel("auto");
    }
  }, [
    askReasoningLevel,
    compareReasoningLevel,
    mode,
    selectedModel,
    setAskReasoningLevel,
    setCompareReasoningLevel,
    smartMode,
    supportedReasoningLevels,
  ]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (!streaming && uploadsPending) {
        setError(uploadWaitError);
      } else if (!streaming && (prompt.trim() || attachments.length > 0)) {
        handleSubmit();
      }
    }
  };

  const reasoningContextLabel =
    mode === "compare"
      ? "Selected models"
      : smartMode
        ? "Smart Ask"
        : selectedModel?.display_name || selectedModel?.model || "Selected model";
  const setReasoningLevel = mode === "compare" ? setCompareReasoningLevel : setAskReasoningLevel;
  const hasOptionChips = optimizeMode || (!reasoningUnavailable && reasoningLevel !== "auto");

  const handleOptimizeChange = (enabled: boolean) => {
    setOptimizeMode(enabled);
    setOptionsAnnouncement(`Improve prompt turned ${enabled ? "on" : "off"}`);
  };

  const handleReasoningChange = (level: typeof reasoningLevel) => {
    setReasoningLevel(level);
    setOptionsAnnouncement(`Reasoning set to ${reasoningLevelLabel(level)}`);
  };

  useEffect(() => {
    setOpenPopover(null);
  }, [mode, sessionId, composerInitialized]);

  const chooseModel = (key: string) => {
    if (key === "smart") {
      setSmartMode(true);
    } else {
      setSelectedModelKey(key);
      setSmartMode(false, key);
      useComposerPrefsStore.getState().rememberModel(key);
    }
    requestAnimationFrame(() => textareaRef.current?.focus());
  };
  const filesPresent = attachments.length > 0 || uploadTasks.length > 0;

  return (
    <div ref={cardRef} className={styles.card} data-composer-card data-tall={isTall}>
      <div
        className={`${styles.composerBody} ${isTall ? styles.tall : ""} ${hasOptionChips ? styles.withChips : ""} ${filesPresent ? styles.withAttachments : ""}`}
      >
        <AttachmentStrip entitlements={entitlements} plans={plans} compact />
        <textarea
          ref={textareaRef}
          id="promptInput"
          className={styles.textarea}
          rows={1}
          aria-label="Prompt input"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={streaming}
          placeholder={
            mode === "compare" ? "Ask once and compare model responses" : "Ask anything…"
          }
        />
        <span ref={mirrorRef} className={styles.measurement} aria-hidden="true">
          {prompt}
        </span>
        <div ref={chipsRef} className={styles.optionsChips} id="promptFeatureControls">
          <FeatureChips
            optimizeMode={optimizeMode}
            reasoningValue={reasoningLevel}
            reasoningUnavailable={reasoningUnavailable}
            onOpen={() => setOptionsOpen(true)}
            onOptimizeReset={() => {
              handleOptimizeChange(false);
              requestAnimationFrame(() => textareaRef.current?.focus());
            }}
            onReasoningReset={() => {
              handleReasoningChange("auto");
              requestAnimationFrame(() => textareaRef.current?.focus());
            }}
          />
        </div>
        <div ref={modelRef} className={styles.modelControl}>
          {mode === "compare" ? (
            <CompareSelector
              models={availableModels}
              keys={compareModelKeys}
              onChange={(index, key) => {
                setCompareModelKey(index, key);
                if (key) useComposerPrefsStore.getState().rememberModel(key);
              }}
              lockedKeys={lockedModelKeys}
              lockedLabels={lockedModelLabels}
              onLockedModel={handleLockedModel}
              maxTargets={entitlements?.features.max_compare_models ?? 3}
              thirdTargetPlanLabel={
                thirdTargetError
                  ? capitalize(detailString(thirdTargetError, "recommended_plan") ?? "Upgrade")
                  : "Upgrade"
              }
              onTargetLimit={() => {
                if (thirdTargetError) setSubscriptionError(thirdTargetError);
              }}
              open={openPopover === "model"}
              onOpenChange={setModelOpen}
              interactionBlocked={Boolean(subscriptionError)}
            />
          ) : (
            <ModelSelector
              id="singleModel"
              models={availableModels}
              value={selectedModelKey}
              onChange={chooseModel}
              lockedKeys={lockedModelKeys}
              lockedLabels={lockedModelLabels}
              onLockedSelect={handleLockedModel}
              composer={{
                smart: smartMode,
                recentModels: prefs.recentModels,
                newChatStart: prefs.newChatStart === "smart" ? "smart" : newChatStart,
                onNewChatStart: (key) => updatePrefs({ newChatStart: key }),
                open: openPopover === "model",
                onOpenChange: setModelOpen,
                interactionBlocked: Boolean(subscriptionError),
              }}
            />
          )}
        </div>
        <div className={styles.optionsControl}>
          <ComposerOptionsMenu
            optimizeMode={optimizeMode}
            onOptimizeToggle={handleOptimizeChange}
            optimizeBlocked={Boolean(improveFeatureError || improveAllowanceError)}
            onOptimizeBlocked={() =>
              setSubscriptionError(improveFeatureError ?? improveAllowanceError)
            }
            reasoningValue={reasoningLevel}
            supportedReasoningLevels={supportedReasoningLevels}
            reasoningUnavailable={reasoningUnavailable}
            reasoningContextLabel={reasoningContextLabel}
            onReasoningChange={handleReasoningChange}
            open={openPopover === "options"}
            onOpenChange={setOptionsOpen}
            interactionBlocked={Boolean(subscriptionError)}
          />
        </div>
        <div className={styles.actions}>
          {uploadsPending && (
            <span id="attachmentSubmitStatus" className={styles.screenReaderOnly}>
              {uploadWaitMessage}
            </span>
          )}
          <button
            className={`${styles.submitButton} ${streaming ? styles.stopButton : ""}`}
            type="button"
            aria-label={streaming ? "Stop" : "Send message"}
            aria-describedby={uploadsPending ? "attachmentSubmitStatus" : undefined}
            title={!streaming && uploadsPending ? uploadWaitMessage : undefined}
            id="submitBtn"
            onClick={() => (streaming ? cancel() : handleSubmit())}
            disabled={
              !streaming && (uploadsPending || (!prompt.trim() && attachments.length === 0))
            }
          >
            <CortexIcon
              name={streaming ? "stop" : "composer-send"}
              strokeWidth={streaming ? undefined : 2.2}
            />
          </button>
        </div>
        <span className={styles.screenReaderOnly} aria-live="polite" aria-atomic="true">
          {optionsAnnouncement}
        </span>
      </div>
    </div>
  );
}

function capitalize(value: string): string {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}

function reasoningLevelLabel(level: "auto" | "low" | "medium" | "high" | "max"): string {
  if (level === "max") return "Maximum";
  return capitalize(level);
}
