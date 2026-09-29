import { useRef, useEffect, useCallback, useState } from "react";
import { ModelSelector } from "./ModelSelector";
import { CompareSelector } from "./CompareSelector";
import { FeatureChips } from "./FeatureChips";
import { ComposerOptionChips, ComposerOptionsMenu } from "./ComposerOptionsMenu";
import { AttachmentStrip } from "./AttachmentStrip";
import { useChatStore } from "../../store/chatStore";
import { useAttachmentUploadStore } from "../../store/attachmentUploadStore";
import { attachmentUploadsBlockSubmission } from "../../uploads/attachmentUploadQueue";
import { userFacingMessage } from "../../errors/userFacingError";
import { useChat } from "../../hooks/useChat";
import { isModelDropdownVisible } from "../../hooks/useSmartRouting";
import type { ModelCatalogItem } from "../../types";
import type { UseSubscriptionResult } from "../../hooks/useSubscription";
import { DEFAULT_MODELS } from "../../config/defaultModels";
import { resolveAskModelKey } from "../../config/askDefaults";
import { resolveCompareModelKeys } from "../../config/compareDefaults";
import {
  compareReasoningLevels,
  defaultModelReasoningLevel,
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

const MOBILE_COMPOSER_MEDIA_QUERY = "(max-width: 900px)";

export function PromptComposer({
  models,
  modelsLoading = false,
  subscription,
}: PromptComposerProps) {
  const availableModels = models.length > 0 ? models : DEFAULT_MODELS;
  const entitlements = subscription?.entitlements ?? null;
  const plans = subscription?.plans ?? null;
  const subscriptionLoading = subscription?.loading ?? false;
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
  const compareModelKeys = useChatStore((s) => s.compareModelKeys);
  const setCompareModelKey = useChatStore((s) => s.setCompareModelKey);
  const prompt = useChatStore((s) => s.prompt);
  const setPrompt = useChatStore((s) => s.setPrompt);
  const attachments = useChatStore((s) => s.attachments);
  const streaming = useChatStore((s) => s.streaming);
  const setError = useChatStore((s) => s.setError);
  const setSubscriptionError = useChatStore((s) => s.setSubscriptionError);
  const uploadTasks = useAttachmentUploadStore((s) => s.tasks);

  const { submit, cancel } = useChat();
  const cardRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [optionsOpen, setOptionsOpen] = useState(false);
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

  const lockedModelKeys = availableModels
    .filter((model) => modelAccessError(model, entitlements, plans) !== null)
    .map((model) => `${model.provider}:${model.model}`);
  const lockedModelLabels = Object.fromEntries(
    availableModels.map((model) => {
      const required = requiredPlanForModel(model.billing_class, entitlements, plans);
      return [`${model.provider}:${model.model}`, required ? capitalize(required) : "Unavailable"];
    }),
  );
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

  const resize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "0px";
    const nextHeight = Math.min(Math.max(el.scrollHeight, 44), 160);
    el.style.height = `${nextHeight}px`;
    el.style.overflowY = el.scrollHeight > 160 ? "auto" : "hidden";
  }, []);

  useEffect(() => {
    resize();
  }, [prompt, resize]);

  useEffect(() => {
    if (subscriptionLoading || modelsLoading || availableModels.length === 0) return;

    if (availableModels.length >= 2) {
      const defaultModels = entitlements
        ? availableModels.filter((model) =>
            entitlements.model_access.allowed_billing_classes.includes(model.billing_class),
          )
        : availableModels;
      const resolvedCompareKeys = resolveCompareModelKeys(
        availableModels,
        compareModelKeys,
        defaultModels,
      );
      for (const index of [0, 1, 2] as const) {
        if (resolvedCompareKeys[index] !== compareModelKeys[index]) {
          setCompareModelKey(index, resolvedCompareKeys[index]);
        }
      }
    }

    const resolvedAskModelKey = resolveAskModelKey(
      availableModels,
      selectedModelKey,
      entitlements?.plan.code ?? null,
      entitlements?.model_access.allowed_billing_classes ?? null,
    );
    if (resolvedAskModelKey !== selectedModelKey) {
      setSelectedModelKey(resolvedAskModelKey);
    }
  }, [
    availableModels,
    compareModelKeys,
    entitlements,
    modelsLoading,
    selectedModelKey,
    setCompareModelKey,
    setSelectedModelKey,
    subscriptionLoading,
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
      setAskReasoningLevel(defaultModelReasoningLevel(selectedModel));
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
    if (event.key === "Enter" && !event.shiftKey) {
      const usesMobileComposer =
        typeof window !== "undefined" &&
        Boolean(window.matchMedia?.(MOBILE_COMPOSER_MEDIA_QUERY).matches);
      if (usesMobileComposer) return;

      event.preventDefault();
      if (!streaming && uploadsPending) {
        setError(uploadWaitError);
      } else if (!streaming && (prompt.trim() || attachments.length > 0)) {
        handleSubmit();
      }
    }
  };

  const showModelDropdown = isModelDropdownVisible(mode, smartMode);
  const showModelRow = mode === "compare" || showModelDropdown;
  const featureChipProps = {
    smartMode,
    onSmartToggle: (enabled: boolean) => {
      setSmartMode(enabled);
      setAskReasoningLevel(enabled ? "auto" : defaultModelReasoningLevel(selectedModel));
    },
  };

  const reasoningContextLabel =
    mode === "compare"
      ? "Selected models"
      : smartMode
        ? "Smart Ask"
        : selectedModel?.display_name || selectedModel?.model || "Selected model";
  const setReasoningLevel =
    mode === "compare" ? setCompareReasoningLevel : setAskReasoningLevel;
  const hasOptionChips =
    optimizeMode || (!reasoningUnavailable && reasoningLevel !== "auto");

  const handleOptimizeChange = (enabled: boolean) => {
    setOptimizeMode(enabled);
    setOptionsAnnouncement(`Improve prompt turned ${enabled ? "on" : "off"}`);
  };

  const handleReasoningChange = (level: typeof reasoningLevel) => {
    setReasoningLevel(level);
    setOptionsAnnouncement(`Reasoning set to ${reasoningLevelLabel(level)}`);
  };

  useEffect(() => {
    setOptionsOpen(false);
  }, [mode]);

  return (
    <div ref={cardRef} className={styles.card}>
      {showModelRow && (
        <div className={styles.modelRow}>
          {mode === "compare" ? (
            <CompareSelector
              models={availableModels}
              keys={compareModelKeys}
              onChange={setCompareModelKey}
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
            />
          ) : (
            <ModelSelector
              id="singleModel"
              label="Using"
              models={availableModels}
              value={selectedModelKey}
              onChange={(key) => {
                setSelectedModelKey(key);
                setAskReasoningLevel(defaultModelReasoningLevel(modelForKey(key, availableModels)));
              }}
              lockedKeys={lockedModelKeys}
              lockedLabels={lockedModelLabels}
              onLockedSelect={handleLockedModel}
            />
          )}
        </div>
      )}

      <div className={styles.composerBody}>
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

        <div className={styles.composerControls} data-composer-mode={mode}>
          <AttachmentStrip entitlements={entitlements} plans={plans} />

          <div id="promptFeatureControls" className={styles.featureControls}>
            <div className={styles.featureControlRow}>
              {mode === "single" ? <FeatureChips {...featureChipProps} /> : null}
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
                modeLabel={mode === "compare" ? "Compare" : smartMode ? "Smart Ask" : "Manual Ask"}
                anchorRef={cardRef}
                open={optionsOpen}
                onOpenChange={setOptionsOpen}
              />
            </div>
          </div>

          {hasOptionChips ? (
            <div className={styles.optionsChips}>
              <ComposerOptionChips
                optimizeMode={optimizeMode}
                reasoningValue={reasoningLevel}
                reasoningUnavailable={reasoningUnavailable}
                onOpen={() => setOptionsOpen(true)}
                onOptimizeReset={() => handleOptimizeChange(false)}
                onReasoningReset={() => handleReasoningChange("auto")}
              />
            </div>
          ) : null}

          <div className={styles.actions}>
            {uploadsPending ? (
              <span id="attachmentSubmitStatus" className={styles.screenReaderOnly}>
                {uploadWaitMessage}
              </span>
            ) : null}
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
