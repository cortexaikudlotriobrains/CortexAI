import { useRef, useEffect, useCallback } from "react";
import { ModelSelector } from "./ModelSelector";
import { CompareSelector } from "./CompareSelector";
import { FeatureChips } from "./FeatureChips";
import { AttachmentStrip } from "./AttachmentStrip";
import { useChatStore } from "../../store/chatStore";
import { useAttachmentUploadStore } from "../../store/attachmentUploadStore";
import { attachmentUploadsBlockSubmission } from "../../uploads/attachmentUploadQueue";
import { useChat } from "../../hooks/useChat";
import { isModelDropdownVisible } from "../../hooks/useSmartRouting";
import type { ModelCatalogItem } from "../../types";
import type { UseSubscriptionResult } from "../../hooks/useSubscription";
import { formatAiCredits } from "../../utils/aiCredits";
import { DEFAULT_MODELS } from "../../config/defaultModels";
import { resolveAskModelKey } from "../../config/askDefaults";
import { resolveCompareModelKeys } from "../../config/compareDefaults";
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
  const mode = useChatStore((s) => s.mode);
  const smartMode = useChatStore((s) => s.smartMode);
  const setSmartMode = useChatStore((s) => s.setSmartMode);
  const researchMode = useChatStore((s) => s.researchMode);
  const setResearchMode = useChatStore((s) => s.setResearchMode);
  const compareResearchMode = useChatStore((s) => s.compareResearchMode);
  const setCompareResearchMode = useChatStore((s) => s.setCompareResearchMode);
  const optimizeMode = useChatStore((s) => s.optimizeMode);
  const setOptimizeMode = useChatStore((s) => s.setOptimizeMode);
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
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const uploadsPending =
    attachmentUploadsBlockSubmission(uploadTasks) ||
    attachments.some((attachment) => attachment.status !== "ready");
  const uploadWaitMessage = "Waiting for attachments to finish uploading";

  const lockedModelKeys = availableModels
    .filter((model) => modelAccessError(model, entitlements, plans) !== null)
    .map((model) => `${model.provider}:${model.model}`);
  const lockedModelLabels = Object.fromEntries(
    availableModels.map((model) => {
      const required = requiredPlanForModel(model.billing_class, entitlements, plans);
      return [`${model.provider}:${model.model}`, required ? capitalize(required) : "Unavailable"];
    }),
  );
  const researchFeatureError = featureAccessError("research", entitlements, plans);
  const researchAllowanceError = allowanceAccessError(
    "ai_credits",
    1,
    entitlements,
    plans,
  );
  const improveFeatureError = featureAccessError("prompt_improvement", entitlements, plans);
  const improveAllowanceError = allowanceAccessError(
    "ai_credits",
    1,
    entitlements,
    plans,
  );
  const thirdTargetError = compareTargetAccessError(3, entitlements, plans);

  const handleSubmit = () => {
    if (uploadsPending) {
      setError(uploadWaitMessage);
      return;
    }
    const accessError = submitAccessError({
      mode,
      smartMode,
      selectedModelKey,
      compareModelKeys,
      models: availableModels,
      researchEnabled: mode === "compare" ? compareResearchMode : researchMode,
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

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!streaming && uploadsPending) {
        setError(uploadWaitMessage);
      } else if (!streaming && (prompt.trim() || attachments.length > 0)) {
        handleSubmit();
      }
    }
  };

  const showModelDropdown = isModelDropdownVisible(mode, smartMode);
  const showModelRow = mode === "compare" || showModelDropdown;
  const featureChipProps = {
    compareMode: mode === "compare",
    smartMode: mode === "single" ? smartMode : false,
    researchMode: mode === "compare" ? compareResearchMode : researchMode,
    optimizeMode,
    onSmartToggle: mode === "single" ? setSmartMode : () => undefined,
    onResearchToggle: mode === "compare" ? setCompareResearchMode : setResearchMode,
    onOptimizeToggle: setOptimizeMode,
    researchBlocked: Boolean(researchFeatureError || researchAllowanceError),
    optimizeBlocked: Boolean(improveFeatureError || improveAllowanceError),
    researchAllowanceLabel: allowanceLabel(entitlements),
    optimizeAllowanceLabel: allowanceLabel(entitlements),
    onResearchBlocked: () =>
      setSubscriptionError(researchFeatureError ?? researchAllowanceError),
    onOptimizeBlocked: () =>
      setSubscriptionError(improveFeatureError ?? improveAllowanceError),
  };

  return (
    <div className={styles.card}>
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
              trailingControls={
                <FeatureChips
                  {...featureChipProps}
                  compareMode
                  variant="sourcesOnly"
                />
              }
            />
          ) : (
            <ModelSelector
              id="singleModel"
              label="Using"
              models={availableModels}
              value={selectedModelKey}
              onChange={setSelectedModelKey}
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
            mode === "compare"
              ? "Ask once and compare model responses"
              : "Ask anything…"
          }
        />

        <AttachmentStrip entitlements={entitlements} plans={plans} />

        <div className={styles.featureControls}>
          <FeatureChips
            {...featureChipProps}
            variant={mode === "compare" ? "improveOnly" : "default"}
          />
        </div>

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
              !streaming &&
              (uploadsPending || (!prompt.trim() && attachments.length === 0))
            }
          >
            <CortexIcon name={streaming ? "stop" : "send"} />
          </button>
        </div>
      </div>
    </div>
  );
}

function allowanceLabel(
  entitlements: UseSubscriptionResult["entitlements"],
): string | undefined {
  const allowance = entitlements?.allowances.ai_credits;
  return allowance
    ? `${formatAiCredits(allowance.remaining)} credits left`
    : undefined;
}

function capitalize(value: string): string {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}
