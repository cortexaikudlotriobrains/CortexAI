import { create } from "zustand";

interface SidebarStoreState {
  isCollapsed: boolean;
  setCollapsed: (collapsed: boolean) => void;
  toggleCollapsed: () => void;
}

export const useSidebarStore = create<SidebarStoreState>((set) => ({
  isCollapsed: false,
  setCollapsed: (isCollapsed) => set({ isCollapsed }),
  toggleCollapsed: () => set((state) => ({ isCollapsed: !state.isCollapsed })),
}));
