import { useCallback, useState } from "react";
import type { ModelCatalogItem } from "../../types";
import { ModelPicker } from "./ModelPicker";

interface CompareSelectorProps {
  models: ModelCatalogItem[];
  keys: [string, string, string];
  onChange: (index: 0 | 1 | 2, key: string) => void;
  lockedKeys?: string[];
  lockedLabels?: Record<string, string>;
  onLockedModel?: (key: string) => void;
  maxTargets?: number;
  thirdTargetPlanLabel?: string;
  onTargetLimit?: () => void;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  interactionBlocked?: boolean;
}

export function CompareSelector({
  models,
  keys,
  onChange,
  lockedKeys = [],
  lockedLabels,
  onLockedModel,
  maxTargets = 3,
  thirdTargetPlanLabel,
  onTargetLimit,
  open: controlledOpen,
  onOpenChange,
  interactionBlocked,
}: CompareSelectorProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = controlledOpen ?? internalOpen;
  const setOpen = useCallback(
    (next: boolean) => {
      if (controlledOpen === undefined) setInternalOpen(next);
      onOpenChange?.(next);
    },
    [controlledOpen, onOpenChange],
  );
  const toggle = (key: string) => {
    const index = keys.indexOf(key);
    const count = keys.filter(Boolean).length;
    if (index >= 0 && count > 2) onChange(index as 0 | 1 | 2, "");
    else if (index < 0 && count < Math.min(3, maxTargets) && !lockedKeys.includes(key))
      onChange(keys.findIndex((value) => !value) as 0 | 1 | 2, key);
  };
  return (
    <ModelPicker
      id="compareModels"
      models={models}
      value={keys[0]}
      onChange={() => undefined}
      ariaLabel="Models to compare"
      lockedKeys={lockedKeys}
      lockedLabels={lockedLabels}
      onLockedSelect={onLockedModel}
      composer={{
        compareKeys: keys,
        onCompareToggle: toggle,
        maxTargets,
        thirdTargetPlanLabel,
        onTargetLimit,
        open,
        onOpenChange: setOpen,
        interactionBlocked,
      }}
    />
  );
}
