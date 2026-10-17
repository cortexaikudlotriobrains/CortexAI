import { create } from "zustand";

type Feedback = "up" | "down" | null;
interface ResponseUi {
  feedback: Record<string, Feedback>;
  setFeedback: (key: string, value: Feedback) => void;
}

// Deliberately local to this application session; no server persistence is implied.
export const useResponseUiStore = create<ResponseUi>((set) => ({
  feedback: {},
  setFeedback: (key, value) => set((state) => ({ feedback: { ...state.feedback, [key]: value } })),
}));
