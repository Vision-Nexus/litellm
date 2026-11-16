import { describe, expect, it } from "vitest";
import { costPresentation, recordedDollars, type TopicCostTotals } from "./costPresentation";

const known: TopicCostTotals = {
  requests: 1,
  successful: 1,
  failed: 0,
  tokens: 10,
  recorded_spend: 0.01,
  amount: 0.01,
  unpriced_requests: 0,
  unknown_cost_requests: 0,
};

describe("costPresentation", () => {
  it("distinguishes hidden, absent, free and unpriced costs", () => {
    expect(costPresentation(null).label).toBe("Not shared");
    expect(costPresentation({ ...known, requests: 0 }).label).toBe("No records");
    expect(costPresentation({ ...known, amount: 0, recorded_spend: 0 }).label).toBe("$0.000000");
    expect(costPresentation({ ...known, amount: null, unpriced_requests: 1 })).toEqual({
      label: "Incomplete",
      detail: "1 unpriced, 0 unknown. Recorded subtotal $0.010000.",
    });
  });

  it("preserves visible tiny charges and credits", () => {
    expect(recordedDollars(0.0000001)).toBe("< $0.000001");
    expect(recordedDollars(-0.0000001)).toBe("> -$0.000001");
    expect(recordedDollars(0.000001)).toBe("$0.000001");
    expect(costPresentation(known)).toEqual({ label: "$0.010000", detail: null });
  });
});
