import { useCallback, useEffect, useState } from "react";
import { fetchUsageSummary, type UsageSummaryParams } from "../api/usage";
import type { UsageSummary } from "../types";
import { presentError, type UserFacingError } from "../errors/userFacingError";

export interface UseUsageSummaryResult {
  summary: UsageSummary | null;
  loading: boolean;
  error: UserFacingError | null;
  reload: () => void;
}

export function useUsageSummary(params: UsageSummaryParams = {}): UseUsageSummaryResult {
  const { from, to } = params;
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<UserFacingError | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

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

  return { summary, loading, error, reload };
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}
