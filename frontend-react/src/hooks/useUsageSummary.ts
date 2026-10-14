import { useCallback, useEffect, useState } from "react";
import { fetchUsageByokStatus, fetchUsageSummary, type UsageSummaryParams } from "../api/usage";
import type { UsageSummary } from "../types";
import { presentError, type UserFacingError } from "../errors/userFacingError";

export interface UseUsageSummaryResult {
  summary: UsageSummary | null;
  loading: boolean;
  error: UserFacingError | null;
  reload: () => void;
  hasConfiguredByok: boolean;
}

export function useUsageSummary(
  params: UsageSummaryParams = {},
  byokApiKeyId?: string,
): UseUsageSummaryResult {
  const { from, to } = params;
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<UserFacingError | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [byokState, setByokState] = useState<{
    apiKeyId: string;
    reloadToken: number;
    from: string | undefined;
    to: string | undefined;
    configured: boolean;
  } | null>(null);

  useEffect(() => {
    if (!byokApiKeyId) return;
    const controller = new AbortController();
    setByokState(null);

    fetchUsageByokStatus(controller.signal)
      .then((status) => {
        if (controller.signal.aborted) return;
        setByokState({
          apiKeyId: byokApiKeyId,
          reloadToken,
          from,
          to,
          configured:
            Array.isArray(status.providers) &&
            status.providers.some((provider) => provider.configured === true),
        });
      })
      .catch(() => {
        // Unconfirmed BYOK status keeps provider costs hidden.
      });

    return () => controller.abort();
  }, [byokApiKeyId, from, to, reloadToken]);

  useEffect(() => {
    const controller = new AbortController();

    setLoading(true);
    setError(null);
    setSummary(null);

    fetchUsageSummary({ from, to }, controller.signal)
      .then((nextSummary) => {
        if (controller.signal.aborted) return;
        setSummary(nextSummary);
      })
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        setError(presentError(err, "usage_load"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [from, to, reloadToken]);

  const reload = useCallback(() => {
    setReloadToken((current) => current + 1);
  }, []);

  const hasConfiguredByok =
    !!byokApiKeyId &&
    byokState !== null &&
    byokState.apiKeyId === byokApiKeyId &&
    byokState.reloadToken === reloadToken &&
    byokState.from === from &&
    byokState.to === to &&
    byokState.configured;

  return { summary, loading, error, reload, hasConfiguredByok };
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}
