import type { Tier } from "@/lib/api";

export type TierMeta = {
  value: Tier;
  badge: string;
  label: string;
  description: string;
  searchable: boolean;
};

export const TIERS: readonly TierMeta[] = [
  {
    value: "tier_1",
    badge: "Tier 1",
    label: "Structure",
    description: "View your documents.",
    searchable: false,
  },
  {
    value: "tier_2",
    badge: "Tier 2",
    label: "Search",
    description: "View, search, and chat.",
    searchable: true,
  },
];

export function tierMeta(value: string): TierMeta {
  return TIERS.find((t) => t.value === value) ?? TIERS[0];
}
