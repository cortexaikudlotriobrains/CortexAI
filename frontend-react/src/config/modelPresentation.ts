export interface ModelPresentation {
  label: string;
  model: string;
  providerLabel: string;
  logoUrl: string;
  color: string;
}

const PROVIDERS: Record<
  string,
  { label: string; logoUrl: string; color: string }
> = {
  smart: {
    label: "Smart routing",
    logoUrl: "",
    color: "#475569",
  },
  openai: {
    label: "ChatGPT",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=openai.com&sz=64",
    color: "#10A37F",
  },
  anthropic: {
    label: "Claude",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=claude.ai&sz=64",
    color: "#D97706",
  },
  gemini: {
    label: "Gemini",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=gemini.google.com&sz=64",
    color: "#4285F4",
  },
  google: {
    label: "Gemini",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=gemini.google.com&sz=64",
    color: "#4285F4",
  },
  deepseek: {
    label: "DeepSeek",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=deepseek.com&sz=64",
    color: "#5B5BD6",
  },
  grok: {
    label: "Grok",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=grok.com&sz=64",
    color: "#1DA1F2",
  },
  claude: {
    label: "Claude",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=claude.ai&sz=64",
    color: "#D97706",
  },
  meta: {
    label: "Meta Llama",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=meta.com&sz=64",
    color: "#0866FF",
  },
  llama: {
    label: "Meta Llama",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=meta.com&sz=64",
    color: "#0866FF",
  },
  mistral: {
    label: "Mistral",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=mistral.ai&sz=64",
    color: "#FF7000",
  },
  mistralai: {
    label: "Mistral",
    logoUrl: "https://www.google.com/s2/favicons?domain_url=mistral.ai&sz=64",
    color: "#FF7000",
  },
};

export function getModelPresentation(
  providerRaw: string,
  modelRaw: string,
): ModelPresentation {
  const provider = providerRaw.trim().toLowerCase();
  const model = modelRaw.trim();
  const providerMeta = PROVIDERS[provider] ?? {
    label: titleCase(provider || "Model"),
    logoUrl: "",
    color: "#94A3B8",
  };

  return {
    label: modelDisplayLabel(provider, model, providerMeta.label),
    model,
    providerLabel: providerMeta.label,
    logoUrl: providerMeta.logoUrl,
    color: providerMeta.color,
  };
}

function modelDisplayLabel(
  provider: string,
  model: string,
  providerLabel: string,
): string {
  const lower = model.toLowerCase();
  if (provider === "openai") {
    const match = lower.match(/gpt-([0-9]+(?:\.[0-9]+)?[a-z]?)/);
    if (match) {
      const suffix = capabilityWord(lower);
      const base = `GPT-${match[1]}`;
      return suffix && !base.toLowerCase().includes(suffix.toLowerCase())
        ? `${base} ${suffix}`
        : base;
    }
  }

  const capability = capabilityWord(lower);
  return capability ? `${providerLabel} ${capability}` : providerLabel;
}

function capabilityWord(model: string): string {
  if (model.includes("astra")) return "Astra";
  if (model.includes("argon")) return "Argon";
  if (model.includes("fable")) return "Fable";
  if (model.includes("terra")) return "Terra";
  if (model.includes("luna")) return "Luna";
  if (model.includes("sol")) return "Sol";
  if (model.includes("flash")) return "Flash";
  if (model.includes("pro")) return "Pro";
  if (model.includes("mini")) return "Mini";
  if (model.includes("sonnet")) return "Sonnet";
  if (model.includes("haiku")) return "Haiku";
  if (model.includes("opus")) return "Opus";
  if (model.includes("reasoner") || model.includes("reasoning")) return "Reasoning";
  if (model.includes("fast")) return "Fast";
  if (model.includes("chat")) return "Chat";
  if (model.includes("instruct")) return "Instruct";
  return "";
}

function titleCase(value: string): string {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((part) => `${part[0]?.toUpperCase() ?? ""}${part.slice(1)}`)
    .join(" ");
}
