import { useEffect, useState } from "react";
import { post } from "../api/client";
import { useChatStore } from "../store/chatStore";
import { generationForReasoningLevel } from "../reasoning/reasoningLevels";

export interface AnalyzeEstimate {
  estimated_max_ai_credits: number;
  analysis_ai_credits: number;
  can_authorize: boolean;
}

export function useAnalyzeEstimate() {
  const mode = useChatStore((state) => state.mode);
  const prompt = useChatStore((state) => state.prompt);
  const keys = useChatStore((state) => state.compareModelKeys);
  const reasoning = useChatStore((state) => state.compareReasoningLevel);
  const turns = useChatStore((state) => state.turns);
  const history = JSON.stringify(
    turns
      .filter((turn) => !["streaming", "optimizing", "cancelled"].includes(turn.status))
      .flatMap((turn) => [
        { role: "user", content: turn.submittedPrompt || turn.prompt },
        {
          role: "assistant",
          content: turn.responses
            .map((response) => response.text)
            .filter(Boolean)
            .join("\n\n"),
        },
      ])
      .filter((item) => item.content)
      .slice(-10),
  );
  const signature = JSON.stringify({ prompt, keys, reasoning, history });
  const [result, setResult] = useState<{
    signature: string;
    estimate: AnalyzeEstimate | null;
    failed: boolean;
  } | null>(null);
  useEffect(() => {
    if (mode !== "compare") return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      const targets = keys.filter(Boolean).map((key) => {
        const separator = key.indexOf(":");
        return { provider: key.slice(0, separator), model: key.slice(separator + 1) };
      });
      if (targets.length < 2) return;
      void post<AnalyzeEstimate>(
        "/v1/billing/estimate-generation",
        {
          prompt,
          targets,
          generation: generationForReasoningLevel(reasoning, "auto"),
          include_analysis: true,
          include_native_search: true,
          context: { conversation_history: JSON.parse(history) },
        },
        controller.signal,
      )
        .then((estimate) => {
          if (!controller.signal.aborted) setResult({ signature, estimate, failed: false });
        })
        .catch(() => {
          if (!controller.signal.aborted) setResult({ signature, estimate: null, failed: true });
        });
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [mode, prompt, keys, reasoning, history, signature]);
  return result?.signature === signature ? result : null;
}
