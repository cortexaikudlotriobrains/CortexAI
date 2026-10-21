import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import styles from "./ComposerPopover.module.css";
import { useComposerSheet } from "./useComposerSheet";

export function ComposerPopover({
  open,
  triggerRef,
  label,
  id,
  width = 340,
  onClose,
  onBack,
  view = "main",
  interactionBlocked = false,
  children,
}: {
  open: boolean;
  triggerRef: RefObject<HTMLButtonElement>;
  label: string;
  id: string;
  width?: number;
  onClose: () => void;
  onBack?: () => void;
  view?: string;
  interactionBlocked?: boolean;
  children: ReactNode;
}) {
  const sheet = useComposerSheet();
  const menuRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 12, bottom: 12, width, maxHeight: 560 });
  const close = useCallback(() => {
    onClose();
    requestAnimationFrame(() => triggerRef.current?.focus());
  }, [onClose, triggerRef]);
  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger || sheet) return;
    const rect = trigger.getBoundingClientRect();
    const card = trigger.closest("[data-composer-card]")?.getBoundingClientRect();
    const menuWidth = Math.min(width, window.innerWidth - 24, card?.width ?? width);
    const minLeft = Math.max(12, card?.left ?? 12);
    const maxLeft =
      Math.min(window.innerWidth - 12, card?.right ?? window.innerWidth - 12) - menuWidth;
    const anchorTop = card?.top ?? rect.top;
    setPosition({
      left: Math.max(minLeft, Math.min(rect.right - menuWidth, maxLeft)),
      bottom: window.innerHeight - anchorTop + 8,
      width: menuWidth,
      maxHeight: Math.max(0, Math.min(560, window.innerHeight - 140, anchorTop - 20)),
    });
  }, [sheet, triggerRef, width]);

  useLayoutEffect(() => {
    if (!open) return;
    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open, updatePosition]);

  useEffect(() => {
    if (!open || interactionBlocked) return;
    const menu = menuRef.current;
    if (!menu) return;
    const focusable = () =>
      Array.from(
        menu.querySelectorAll<HTMLElement>(
          'button:not(:disabled), [href], input:not(:disabled), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((item) => item.tabIndex >= 0);
    const pointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!menu.contains(target) && !triggerRef.current?.contains(target)) {
        // Let another composer trigger receive focus when switching popovers.
        onClose();
        if (sheet || !(target instanceof Element && target.closest("button, input, textarea, a")))
          requestAnimationFrame(() => triggerRef.current?.focus());
      }
    };
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (onBack) onBack();
        else close();
      } else if (event.key === "Tab" && sheet) {
        const items = focusable();
        const first = items[0];
        const last = items[items.length - 1];
        if (!first) {
          event.preventDefault();
          menu.focus();
          return;
        }
        if (
          event.shiftKey &&
          (document.activeElement === first || !menu.contains(document.activeElement))
        ) {
          event.preventDefault();
          last.focus();
        } else if (
          !event.shiftKey &&
          (document.activeElement === last || !menu.contains(document.activeElement))
        ) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    const focusin = (event: FocusEvent) => {
      if (sheet && !menu.contains(event.target as Node)) focusable()[0]?.focus();
    };
    const previousOverflow = document.body.style.overflow;
    if (sheet) document.body.style.overflow = "hidden";
    document.addEventListener("pointerdown", pointer);
    document.addEventListener("keydown", keydown);
    document.addEventListener("focusin", focusin);
    return () => {
      document.removeEventListener("pointerdown", pointer);
      document.removeEventListener("keydown", keydown);
      document.removeEventListener("focusin", focusin);
      if (sheet) document.body.style.overflow = previousOverflow;
    };
  }, [close, interactionBlocked, onBack, onClose, open, sheet, triggerRef, view]);

  useLayoutEffect(() => {
    if (!open || interactionBlocked) return;
    const menu = menuRef.current;
    if (menu) menu.scrollTop = 0;
    const first = menu?.querySelector<HTMLElement>("button:not(:disabled)");
    const selected = menu?.querySelector<HTMLElement>(
      '[aria-selected="true"]:not(:disabled), [data-initial-focus]',
    );
    (view === "main" ? (selected ?? first) : first)?.focus();
  }, [open, view, interactionBlocked, sheet]);

  if (!open) return null;
  return createPortal(
    <>
      {sheet && (
        <div
          className={styles.scrim}
          data-composer-scrim
          aria-hidden="true"
          onPointerDown={close}
        />
      )}
      <div
        ref={menuRef}
        id={id}
        role="dialog"
        aria-label={label}
        aria-modal={sheet || undefined}
        tabIndex={-1}
        data-layout={sheet ? "sheet" : "popover"}
        className={`${styles.menu} ${sheet ? styles.sheet : styles.popover}`}
        style={sheet ? undefined : position}
      >
        {sheet && <div className={styles.handle} data-sheet-grabber aria-hidden="true" />}
        {children}
      </div>
    </>,
    document.body,
  );
}
