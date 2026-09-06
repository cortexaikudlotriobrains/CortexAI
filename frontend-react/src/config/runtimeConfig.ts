export interface CortexRuntimeConfig {
  apiBase?: string;
  enableDevSessionLogin?: boolean;
  devSessionLoginToken?: string;
  directAttachmentUploads?: boolean;
  legacyAttachmentUploads?: boolean;
  workEnabled?: boolean;
}

export type AttachmentUploadMode = "direct" | "legacy" | "disabled";

export function getRuntimeConfig(): CortexRuntimeConfig {
  return (
    window as unknown as { CORTEX_RUNTIME_CONFIG?: CortexRuntimeConfig }
  ).CORTEX_RUNTIME_CONFIG ?? {};
}

export function getAttachmentUploadMode(): AttachmentUploadMode {
  const config = getRuntimeConfig();
  if (config.directAttachmentUploads) return "direct";
  if (config.legacyAttachmentUploads === false) return "disabled";
  return "legacy";
}
