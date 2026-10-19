import type { Nodes } from "mdast";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import type { WebSourceItem } from "../types";
import { normalizeSourceUrl } from "../utils/sourceMeta";

const parser = unified().use(remarkParse).use(remarkGfm);
export interface SourcePassage {
  quote: string;
  text: string;
}

// Resolve the same one-based [n] -> web_source_items ordering as response pills.
// Links/reference links must also match an attached, approved HTTP(S) URL.
export function sourcePassages(
  text: string,
  orderedSources: WebSourceItem[],
  approvedUrls: string[],
): Map<string, SourcePassage[]> {
  const tree = parser.parse(text);
  const approved = new Set(
    approvedUrls.map(normalizeSourceUrl).filter((url): url is string => !!url),
  );
  const definitions = new Map<string, string>();
  const definitionsText = tree.children
    .filter((node) => node.type === "definition")
    .map((node) => text.slice(node.position?.start.offset, node.position?.end.offset))
    .join("\n");
  for (const node of tree.children)
    if (node.type === "definition") definitions.set(node.identifier.toLowerCase(), node.url);
  const result = new Map<string, SourcePassage[]>();

  function add(
    urlValue: string,
    block: Nodes,
    referenceStart: number,
    referenceEnd: number,
    linkLabel?: string,
  ) {
    const url = normalizeSourceUrl(urlValue);
    if (!url || !approved.has(url)) return;
    const start = block.position?.start.offset;
    const end = block.position?.end.offset;
    if (start == null || end == null) return;
    const quote = citedSentence(
      text.slice(start, end),
      referenceStart - start,
      referenceEnd - start,
    );
    const display = plainText(parser.parse(`${quote}\n\n${definitionsText}`))
      .replace(/\s+/g, " ")
      .trim();
    // A bibliography entry or standalone URL does not describe what it supports.
    if (
      !display ||
      display.replace(/[.!?]$/, "") === linkLabel?.replace(/[.!?]$/, "") ||
      display === urlValue
    )
      return;
    const passages = result.get(url) ?? [];
    if (!passages.some((item) => item.quote === quote)) passages.push({ quote, text: display });
    result.set(url, passages);
  }

  function references(node: Nodes, block: Nodes) {
    if (["code", "inlineCode", "html", "image", "imageReference"].includes(node.type)) return;
    const start = node.position?.start.offset;
    const end = node.position?.end.offset;
    if (node.type === "link" || node.type === "linkReference") {
      const url = node.type === "link" ? node.url : definitions.get(node.identifier.toLowerCase());
      if (url && start != null && end != null) add(url, block, start, end, plainText(node).trim());
      return;
    }
    if (node.type === "text" && start != null && end != null) {
      const raw = text.slice(start, end);
      for (const match of raw.matchAll(/\[(\d+)\]/g)) {
        const prefix = raw.slice(0, match.index);
        const escapes = prefix.match(/\\+$/)?.[0].length ?? 0;
        if (escapes % 2 || raw[match.index + match[0].length] === ":") continue;
        const source = orderedSources[Number(match[1]) - 1];
        if (source)
          add(source.url, block, start + match.index, start + match.index + match[0].length);
      }
    }
    if ("children" in node) for (const child of node.children) references(child as Nodes, block);
  }
  function blocks(node: Nodes) {
    if (["code", "html", "definition", "footnoteDefinition", "heading"].includes(node.type)) return;
    if (node.type === "paragraph" || node.type === "tableRow") {
      references(node, node);
      return;
    }
    if ("children" in node) for (const child of node.children) blocks(child as Nodes);
  }
  let referencesDepth: number | null = null;
  for (const node of tree.children) {
    if (node.type === "heading") {
      if (referencesDepth != null && node.depth <= referencesDepth) referencesDepth = null;
      if (
        /^(?:sources?|references?|bibliography|citations?|further reading|links)(?:\s*\([^)]*\))?\s*:?$/i.test(
          plainText(node).trim(),
        )
      )
        referencesDepth = node.depth;
    }
    if (referencesDepth == null) blocks(node);
  }
  return result;
}

function citedSentence(block: string, referenceStart: number, referenceEnd: number): string {
  let start = 0;
  for (const match of block.matchAll(/[.!?]["'”’)]*(?:[ \t]*\[\d+\](?:\([^\n)]*\))?)*(?=\s|$)/g)) {
    const end = match.index + match[0].length;
    const prefix = block.slice(0, match.index + 1);
    if (
      /(?:\b[A-Za-z]\.){2,}$/.test(prefix) ||
      /\b(?:Mr|Mrs|Ms|Dr|Prof|vs|e\.g|i\.e)\.$/i.test(prefix)
    )
      continue;
    if (end >= referenceEnd) return block.slice(start, end).trim();
    if (end <= referenceStart) start = end;
  }
  return block.slice(start).trim();
}

function plainText(node: Nodes): string {
  if (
    (node.type === "link" || node.type === "linkReference") &&
    /^\d+$/.test(node.children.map((child) => plainText(child as Nodes)).join(""))
  )
    return "";
  if (node.type === "text") return node.value.replace(/\[\d+\]/g, "");
  if (node.type === "inlineCode") return node.value;
  if (node.type === "html" || node.type === "definition") return "";
  if ("children" in node)
    return node.children
      .map((child) => plainText(child as Nodes))
      .join(node.type === "tableRow" ? " · " : "");
  return "";
}
