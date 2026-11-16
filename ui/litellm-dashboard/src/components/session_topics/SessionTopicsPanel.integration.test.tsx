import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, renderWithProviders, testQueryClient } from "../../../tests/test-utils";
import SessionTopicsPanel from "./SessionTopicsPanel";

const page = {
  items: [
    {
      session_ref: "a".repeat(64),
      key_hash: "b".repeat(64),
      user_id: "user-one",
      team_id: null,
      organization_id: null,
      topic: "Investigating database connection timeouts",
      category: "engineering",
      status: "ready",
      attempts: 1,
      model: "session-topic-luna",
      prompt_version: "english-topic-v1",
      first_seen: "2026-01-01T00:00:00Z",
      last_seen: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    },
  ],
  limit: 50,
  offset: 0,
};
const overview = {
  sessions: 1,
  ready: 1,
  pending: 0,
  unavailable: 0,
  usage: [
    { kind: "business", requests: 20, successful: 20, failed: 0, tokens: 1000, spend: 2 },
    { kind: "session_topic", requests: 1, successful: 1, failed: 0, tokens: 100, spend: 0.01 },
  ],
};

describe("SessionTopicsPanel", () => {
  beforeEach(() => testQueryClient.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("shows the English topic and keeps summary costs separate", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        return Response.json(url.includes("/overview") ? overview : page);
      }),
    );
    renderWithProviders(<SessionTopicsPanel accessToken="test-only-token" viewerScope="user-one" />);
    expect(await screen.findByText("Investigating database connection timeouts")).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Usage by purpose" })).toHaveTextContent("Business inference");
    expect(screen.getByRole("table", { name: "Usage by purpose" })).toHaveTextContent("Internal summaries");
    expect(screen.getByRole("table", { name: "Usage by purpose" })).toHaveTextContent("$0.0100");
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("shows feature-disabled status rather than an empty success report", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ detail: "Session topics are disabled" }, { status: 503 })),
    );
    renderWithProviders(<SessionTopicsPanel accessToken="test-only-token" viewerScope="user-one" />);
    expect(
      await screen.findByText("Session topics are disabled. Ask an administrator to configure the feature."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Usage by purpose" })).not.toBeInTheDocument();
  });

  it("renders untrusted topic text without creating HTML", async () => {
    const text = "<img src=x onerror=alert(1)>";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : String(input);
        return Response.json(
          url.includes("/overview") ? overview : { ...page, items: [{ ...page.items[0], topic: text }] },
        );
      }),
    );
    renderWithProviders(<SessionTopicsPanel accessToken="test-only-token" viewerScope="user-one" />);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
