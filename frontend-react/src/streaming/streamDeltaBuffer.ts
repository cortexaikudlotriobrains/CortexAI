import { useChatStore } from "../store/chatStore";

// Applying every SSE delta to the store re-renders the transcript and
// re-parses markdown per token, which can saturate the main thread on long
// responses. Batch deltas and flush at a stable ~8/sec cadence.
const FLUSH_INTERVAL_MS = 120;

export class StreamDeltaBuffer {
  private pending = new Map<number, string>();
  private timer: number | null = null;

  constructor(private readonly turnId: string) {}

  append(index: number, text: string): void {
    this.pending.set(index, (this.pending.get(index) ?? "") + text);
    if (this.timer === null) {
      this.timer = window.setTimeout(() => this.flush(), FLUSH_INTERVAL_MS);
    }
  }

  flush(): void {
    this.clearTimer();
    if (this.pending.size === 0) return;

    const state = useChatStore.getState();
    const turn = state.turns.find((item) => item.id === this.turnId);
    if (!turn || turn.status !== "streaming") {
      this.pending.clear();
      return;
    }

    for (const [index, text] of this.pending) {
      const response = turn.responses[index];
      if (
        response?.ui_status === "complete" ||
        response?.ui_status === "failed" ||
        response?.ui_status === "cancelled"
      ) {
        continue;
      }
      state.appendTurnResponseText(this.turnId, index, text, {
        ui_status: "streaming",
      });
    }
    this.pending.clear();
  }

  dispose(): void {
    this.clearTimer();
    this.pending.clear();
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
