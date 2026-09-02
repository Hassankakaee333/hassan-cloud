export type ProviderDescriptor = {
  id: string;
  capabilities: string[];
  status: "WORKING" | "NOT_CONFIGURED";
  cost_type: "FREE";
  health: "HEALTHY";
  quality_tier: string;
  limits: Record<string, string>;
};

export const PROVIDERS: ProviderDescriptor[] = [
  {
    id: "hassan-honest-chat",
    capabilities: ["chat"],
    status: "NOT_CONFIGURED",
    cost_type: "FREE",
    health: "HEALTHY",
    quality_tier: "basic",
    limits: { note: "No paid LLM configured; honest fallback only" },
  },
  {
    id: "github-actions-coding-worker",
    capabilities: ["coding", "testing", "android_build"],
    status: "WORKING",
    cost_type: "FREE",
    health: "HEALTHY",
    quality_tier: "verified_mvp",
    limits: { runtime: "GitHub-hosted runner", timeout: "15 minutes" },
  },
  {
    id: "github-radar",
    capabilities: ["research", "discovery"],
    status: "WORKING",
    cost_type: "FREE",
    health: "HEALTHY",
    quality_tier: "curated_seed",
    limits: { activation: "manual approval only" },
  },
  {
    id: "neon-persistence",
    capabilities: ["persistence"],
    status: "WORKING",
    cost_type: "FREE",
    health: "HEALTHY",
    quality_tier: "production_poc",
    limits: { plan: "free tier" },
  },
];

export function providersForCapability(capability: string): ProviderDescriptor[] {
  return PROVIDERS.filter((provider) => provider.capabilities.includes(capability));
}
