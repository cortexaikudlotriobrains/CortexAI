export function terminalResponseText(
  terminalText: unknown,
  streamedText: string | null | undefined,
): string {
  return typeof terminalText === "string" && terminalText.length > 0
    ? terminalText
    : (streamedText ?? "");
}
