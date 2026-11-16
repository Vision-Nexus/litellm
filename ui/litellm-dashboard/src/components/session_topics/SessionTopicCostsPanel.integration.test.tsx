import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, renderWithProviders, screen, testQueryClient, waitFor, within } from "../../../tests/test-utils";
import type { components } from "@/lib/http/schema";
import SessionTopicsPanel from "./SessionTopicsPanel";

type CostPage = components["schemas"]["TopicCostPage"];
const business = {
  requests: 2,
  successful: 2,
  failed: 0,
  tokens: 40,
  recorded_spend: 0.002,
  amount: null,
  unpriced_requests: 1,
  unknown_cost_requests: 0,
};
const summary = { ...business, requests: 1, tokens: 10, amount: 0.002, unpriced_requests: 0 };
const costPage: CostPage = {
  items: [
    {
      session_ref: "a".repeat(64),
      topic: "Reviewing database connections",
      category: "engineering",
      key_hash: "b".repeat(64),
      user_id: "user-one",
      sessions: 1,
      ready_sessions: 1,
      last_seen: "2026-01-01T00:00:00Z",
      models: ["test-model"],
      historical_business_requests: 1,
      business,
      summary,
      total: { ...business, requests: 3, recorded_spend: 0.004 },
    },
  ],
  total_groups: 2,
  totals: [
    { kind: "business", costs: business },
    { kind: "session_topic", costs: summary },
  ],
  unassociated: [{ kind: "session_topic", costs: summary }],
  days: 7,
  group_by: "session",
  limit: 50,
  offset: 0,
  legacy_scan_limit: 1000,
  legacy_scan_truncated: true,
  topic_retention_days: 30,
};

describe("session cost details", () => {
  beforeEach(() => testQueryClient.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("opens Costs and displays incomplete amounts without losing unassociated charges", async () => {
    const emptyOverview = { sessions: 0, ready: 0, pending: 0, unavailable: 0, usage: [] };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        if (url.includes("/costs")) return Response.json(costPage);
        if (url.includes("/overview")) return Response.json(emptyOverview);
        return Response.json({ items: [], limit: 50, offset: 0 });
      }),
    );
    renderWithProviders(<SessionTopicsPanel accessToken="test-only-token" viewerScope="admin" />);
    fireEvent.click(screen.getByRole("button", { name: "Costs", exact: true }));
    const table = await screen.findByRole("table", { name: "Session cost breakdown" });
    expect(table).toHaveTextContent("Reviewing database connections");
    expect(table).toHaveTextContent("Incomplete");
    expect(table).toHaveTextContent("Recorded subtotal $0.002000");
    expect(screen.getByRole("table", { name: "Unassociated usage" })).toHaveTextContent("Internal summaries");
    expect(screen.getByText(/Historical lookup reached its/)).toHaveTextContent("1,000-row limit");
    expect(screen.queryByRole("table", { name: "Session topics" })).not.toBeInTheDocument();
  });

  it("restores filters from the URL and resets pagination when grouping or days change", async () => {
    const fetcher = vi.fn(async () => Response.json(costPage));
    const onUrlUpdate = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    renderWithProviders(<SessionTopicsPanel accessToken="test-only-token" viewerScope="admin" />, {
      searchParams: "view=costs&cost_group=key&cost_days=30&cost_offset=50",
      onUrlUpdate,
    });
    expect(await screen.findByRole("combobox", { name: "Group by" })).toHaveValue("key");
    expect(screen.getByRole("combobox", { name: "Time window" })).toHaveValue("30");
    expect(await screen.findByText("Page 2")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Group by" }), { target: { value: "topic" } });
    expect(await screen.findByText("Page 1")).toBeInTheDocument();
    expect(onUrlUpdate).toHaveBeenLastCalledWith(
      expect.objectContaining({ queryString: "?view=costs&cost_group=topic&cost_days=30" }),
    );
    fireEvent.change(screen.getByRole("combobox", { name: "Time window" }), { target: { value: "90" } });
    await waitFor(() =>
      expect(fetcher).toHaveBeenLastCalledWith(
        expect.stringMatching(/\/session_topics\/costs\?group_by=topic&days=90&offset=0&limit=50$/),
        expect.anything(),
      ),
    );
    await waitFor(() =>
      expect(onUrlUpdate).toHaveBeenLastCalledWith(
        expect.objectContaining({ queryString: "?view=costs&cost_group=topic&cost_days=90" }),
      ),
    );
  });

  it("does not turn private summary costs into zero for ordinary readers", async () => {
    const ownCosts: CostPage = {
      ...costPage,
      items: costPage.items.map((item) => ({ ...item, summary: null, total: null })),
      totals: [{ kind: "business", costs: business }],
      unassociated: [],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json(ownCosts)),
    );
    renderWithProviders(<SessionTopicsPanel accessToken="test-only-token" viewerScope="user-one" />, {
      searchParams: "view=costs",
    });
    const table = await screen.findByRole("table", { name: "Session cost breakdown" });
    expect(within(table).getAllByText("Not shared")).toHaveLength(2);
    expect(screen.queryByText("Internal summaries")).not.toBeInTheDocument();
    expect(within(table).queryByText("$0.000000")).not.toBeInTheDocument();
  });
});
