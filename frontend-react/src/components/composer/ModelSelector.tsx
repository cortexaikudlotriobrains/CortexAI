import type { ModelCatalogItem } from "../../types";
import type { ComposerModelMenuConfig } from "./ComposerModelMenu";
import { ModelPicker } from "./ModelPicker";

interface ModelSelectorProps {
  models: ModelCatalogItem[];
  value: string;
  onChange: (key: string) => void;
  label?: string;
  id?: string;
  lockedKeys?: string[];
  lockedLabels?: Record<string, string>;
  onLockedSelect?: (key: string) => void;
  composer?: ComposerModelMenuConfig;
}

export function ModelSelector({
  models,
  value,
  onChange,
  id = "modelSelector",
  label,
  lockedKeys,
  lockedLabels,
  onLockedSelect,
  composer,
}: ModelSelectorProps) {
  return (
    <ModelPicker
      id={id}
      models={models}
      value={value}
      onChange={onChange}
      ariaLabel={label ?? "Model"}
      listboxLabel={`${label ?? "Model"} options`}
      lockedKeys={lockedKeys}
      lockedLabels={lockedLabels}
      onLockedSelect={onLockedSelect}
      placement="up"
      align="right"
      composer={composer}
    />
  );
}
