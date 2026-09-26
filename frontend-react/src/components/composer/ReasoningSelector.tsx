import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ReasoningLevel } from "../../types";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./ReasoningSelector.module.css";

const OPTIONS: Array<{
  value: ReasoningLevel;
  label: string;
  description: string;
}> = [
  { value: "auto", label: "Auto", description: "Cortex chooses for each request" },
  { value: "low", label: "Low", description: "Faster, lighter analysis" },
  { value: "medium", label: "Medium", description: "Balanced speed and depth" },
  { value: "high", label: "High", description: "Deeper analysis for difficult questions" },
  { value: "max", label: "Maximum", description: "Most reasoning available; slower" },
];

interface ReasoningSelectorProps {
  value: ReasoningLevel;
  supportedLevels: Exclude<ReasoningLevel, "auto">[];
  onChange: (level: ReasoningLevel) => void;
  contextLabel: string;
  unavailable?: boolean;
}

export function ReasoningSelector({
  value,
  supportedLevels,
  onChange,
  contextLabel,
  unavailable = false,
}: ReasoningSelectorProps) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 12, top: 12, width: 320 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const updatePosition = () => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(340, Math.max(280, window.innerWidth - 24));
    const left = Math.min(
      Math.max(12, rect.right - width),
      Math.max(12, window.innerWidth - width - 12),
    );
    const menuHeight = menuRef.current?.offsetHeight ?? 354;
    const top = Math.max(12, rect.top - menuHeight - 8);
    setPosition({ left, top, width });
  };

  useLayoutEffect(() => {
    if (open) updatePosition();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const label = unavailable ? "Unavailable" : optionLabel(value);
  const menu = open
    ? createPortal(
        <div
          ref={menuRef}
          id="reasoning-level-menu"
          className={styles.menu}
          role="listbox"
          aria-label="Reasoning levels"
          style={position}
        >
          <div className={styles.menuHeader}>
            <span>Reasoning</span>
            <span>{contextLabel}</span>
          </div>
          {OPTIONS.map((option) => {
            const disabled = option.value !== "auto" && !supportedLevels.includes(option.value);
            const selected = option.value === value;
            return (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={selected}
                disabled={disabled}
                className={`${styles.option} ${selected ? styles.selected : ""}`}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
                  triggerRef.current?.focus();
                }}
              >
                <span className={styles.optionIcon} aria-hidden="true">
                  {selected ? <CortexIcon name="check" /> : null}
                </span>
                <span className={styles.optionCopy}>
                  <strong>{option.label}</strong>
                  <small>
                    {disabled ? "Not supported by the current model selection" : option.description}
                  </small>
                </span>
              </button>
            );
          })}
          <div className={styles.note}>
            Higher reasoning may take longer and use more AI credits.
          </div>
        </div>,
        document.body,
      )
    : null;

  return (
    <div className={styles.wrap}>
      <button
        ref={triggerRef}
        type="button"
        className={styles.trigger}
        aria-label={`Reasoning level: ${label}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls="reasoning-level-menu"
        disabled={unavailable}
        onClick={() => setOpen((current) => !current)}
      >
        <CortexIcon name="smart" />
        <span className={styles.prefix}>Reasoning · </span>
        <span>{label}</span>
        {!unavailable ? <CortexIcon name="chevron-down" /> : null}
      </button>
      {menu}
    </div>
  );
}

function optionLabel(level: ReasoningLevel): string {
  if (level === "max") return "Maximum";
  return `${level.charAt(0).toUpperCase()}${level.slice(1)}`;
}
