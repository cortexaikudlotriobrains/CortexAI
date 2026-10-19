import { describe, expect, it } from "vitest";
import { sourcePassages } from "../markdown/sourcePassages";
import { collectAnalysisSources } from "../analysis/analysisSources";
import { normalizeSourceUrl } from "../utils/sourceMeta";
import type { ChatResponse, CortexAnalysisRun } from "../types";

const sources = [
  { title: "Private investment", url: "https://example.com/private?year=2026" },
  { title: "Borrowing", url: "https://example.com/borrowing" },
];
const extract = (text: string) =>
  sourcePassages(
    text,
    sources,
    sources.map((source) => source.url),
  );

describe("source citation passages", () => {
  it("ties each source to its cited sentence rather than unrelated nearby claims", () => {
    const result = extract(
      "Investment measures money raised. [1] Borrowing is different from cash spent.[2]",
    );
    expect(result.get(sources[0]!.url)).toEqual([
      { quote: "Investment measures money raised. [1]", text: "Investment measures money raised." },
    ]);
    expect(result.get(sources[1]!.url)).toEqual([
      {
        quote: "Borrowing is different from cash spent.[2]",
        text: "Borrowing is different from cash spent.",
      },
    ]);
  });
  it("supports grouped markers and keeps repeated citations distinct without duplicates", () => {
    const result = extract(
      "The sources discuss financing.[1][2]\n\nInvestment is not spending.[1][1]",
    );
    expect(result.get(sources[0]!.url)).toHaveLength(2);
    expect(result.get(sources[1]!.url)?.[0]?.text).toBe("The sources discuss financing.");
  });
  it("resolves inline URLs, reference links and meaningful query parameters", () => {
    const result = extract(
      "[The private figure](https://example.com/private?year=2026#table) is money raised.\n\nBorrowing covers [leases][borrowing].\n\n[borrowing]: https://example.com/borrowing",
    );
    expect(result.get(sources[0]!.url)?.[0]?.text).toBe("The private figure is money raised.");
    expect(result.get(sources[1]!.url)?.[0]?.quote).toBe("Borrowing covers [leases][borrowing].");
    expect(result.get(sources[1]!.url)?.[0]?.text).toBe("Borrowing covers leases.");
    expect(
      extract("A different year is discussed [here](https://example.com/private?year=2025).").size,
    ).toBe(0);
  });
  it("keeps postfix numeric Markdown links with the preceding claim", () => {
    const result = extract(
      "Investment is money raised. [1](https://example.com/private?year=2026) Borrowing is financing. [2](https://example.com/borrowing)",
    );
    expect(result.get(sources[0]!.url)?.[0]?.text).toBe("Investment is money raised.");
    expect(result.get(sources[1]!.url)?.[0]?.text).toBe("Borrowing is financing.");
  });
  it("ignores code, escaped markers, bibliography sections, bare links and unknown references", () => {
    const result = extract(
      "`array[1]`\n\n```text\nExample[1]\n```\n\nEscaped \\[1\\] and unknown [99].\n\n[Private investment](https://example.com/private?year=2026)\n\n## Sources\n\nReference list entry.[1]\n\n## Conclusion\n\nReal cited conclusion.[2]",
    );
    expect([...result.keys()]).toEqual([sources[1]!.url]);
    expect(result.get(sources[1]!.url)?.[0]?.text).toBe("Real cited conclusion.");
  });
  it("resolves citations in lists and table rows", () => {
    const result = extract(
      "- The figure is money raised.[1]\n\n| Funding | Meaning |\n| --- | --- |\n| Borrowing | Leases, not cash spent.[2] |",
    );
    expect(result.get(sources[0]!.url)?.[0]?.text).toBe("The figure is money raised.");
    expect(result.get(sources[1]!.url)?.[0]?.quote).toContain("Leases, not cash spent.[2]");
  });
  it("does not turn attached metadata into claim support without an explicit citation", () => {
    expect(extract("Investment includes real infrastructure spending.").size).toBe(0);
    expect(sourcePassages("A claim.[1]", [], [sources[0]!.url]).size).toBe(0);
    expect(sourcePassages("A real claim.[2]", [{ title: "Unsafe", url: "javascript:alert(1)" }, sources[0]!], [sources[0]!.url]).get(sources[0]!.url)?.[0]?.text).toBe("A real claim.");
  });
  it.each([
    "javascript:alert(1)",
    "https://user:password@example.com/a",
    "https://example.com/\nunsafe",
    "https://example.com\\evil",
  ])("rejects unsafe source URL %s", (url) => {
    expect(normalizeSourceUrl(url)).toBeNull();
    expect(sourcePassages("A claim.[1]", [{ title: "Unsafe", url }], [url]).size).toBe(0);
  });
});

describe("analysis source attribution", () => {
  const response = (provider: string, version: number, text: string, ordered = sources) =>
    ({
      request_id: provider,
      response_version: version,
      provider,
      model: provider === "openai" ? "gpt-5.1" : "claude-sonnet-4-5",
      text,
      web_source_items: ordered,
    }) as ChatResponse;
  it("deduplicates URLs while resolving each model and revision with its own source ordering", () => {
    const current = response("openai", 2, "Current borrowing.[1] Current private figure.[2]", [
      sources[1]!,
      sources[0]!,
    ]);
    const other = response("claude", 1, "Claude's private figure.[1]");
    const run = {
      sourceResponses: [
        {
          requestId: "openai",
          responseVersion: 1,
          provider: "openai",
          model: "gpt-5.1",
          responseName: "GPT-5.1",
          content: "Earlier private figure.[1]",
          webSources: sources,
        },
      ],
      structuredResult: { linkedSources: [] },
    } as unknown as CortexAnalysisRun;
    const details = collectAnalysisSources([current, other], run, true);
    const privateSource = details.find((source) => source.url === sources[0]!.url)!;
    expect(
      privateSource.references.map((ref) => [
        ref.requestId,
        ref.responseVersion,
        ref.passages[0]?.text,
      ]),
    ).toEqual([
      ["openai", 2, "Current private figure."],
      ["claude", 1, "Claude's private figure."],
      ["openai", 1, "Earlier private figure."],
    ]);
    expect(details).toHaveLength(2);
  });
  it("never borrows current source ordering or text for a missing earlier revision", () => {
    const run = {
      sourceResponses: [
        {
          requestId: "openai",
          responseVersion: 1,
          provider: "openai",
          model: "gpt-5.1",
          responseName: "GPT-5.1",
          content: "An old assertion.[1]",
        },
      ],
      structuredResult: {
        linkedSources: [
          {
            ...sources[0],
            sourceId: "saved",
            attributions: [{ requestId: "openai", responseVersion: 1 }],
          },
        ],
      },
    } as unknown as CortexAnalysisRun;
    const details = collectAnalysisSources(
      [response("openai", 2, "A new assertion.[1]", [sources[1]!])],
      run,
      true,
    );
    expect(
      details.find((source) => source.url === sources[0]!.url)?.references[0]?.passages,
    ).toEqual([]);
    run.sourceResponses[0]!.content = null;
    expect(collectAnalysisSources([], run, true)[0]?.references[0]?.available).toBe(false);
  });
});
