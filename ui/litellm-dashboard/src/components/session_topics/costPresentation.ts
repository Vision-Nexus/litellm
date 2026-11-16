import type { components } from "@/lib/http/schema";

export type TopicCostTotals = components["schemas"]["TopicCostTotals"];

export const usageLabels = {
  business: "Business inference",
  session_topic: "Internal summaries",
  other: "Other gateway traffic",
};

export function recordedDollars(value: number): string {
  if (value > 0 && value < 0.000001) return "< $0.000001";
  if (value < 0 && value > -0.000001) return "> -$0.000001";
  return `$${value.toFixed(6)}`;
}

export function costPresentation(cost: TopicCostTotals | null): { label: string; detail: string | null } {
  if (cost === null) return { label: "Not shared", detail: "Internal summary costs are administrator-only." };
  if (cost.requests === 0) return { label: "No records", detail: null };
  if (cost.amount === null) {
    return {
      label: "Incomplete",
      detail: `${cost.unpriced_requests} unpriced, ${cost.unknown_cost_requests} unknown. Recorded subtotal ${recordedDollars(cost.recorded_spend)}.`,
    };
  }
  return { label: recordedDollars(cost.amount), detail: null };
}
