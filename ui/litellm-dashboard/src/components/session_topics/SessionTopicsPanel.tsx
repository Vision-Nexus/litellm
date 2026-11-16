"use client";

import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { createParser, parseAsStringLiteral, useQueryState } from "nuqs";
import { apiClient } from "@/components/networking";
import { ApiError } from "@/lib/http/client";
import type { components } from "@/lib/http/schema";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Page } from "@/components/shared/Page";
import { PageHeader, PageHeaderDescription, PageHeaderTitle } from "@/components/shared/PageHeader";
import SessionTopicCostsPanel from "./SessionTopicCostsPanel";
import { recordedDollars, usageLabels } from "./costPresentation";

type TopicPage = components["schemas"]["TopicPage"];
type TopicOverview = components["schemas"]["TopicOverview"];

const pageSize = 50;
const offsetParser = createParser({
  parse: (value) => (/^\d{1,6}$/.test(value) && Number(value) <= 100000 ? Number(value) : null),
  serialize: String,
}).withDefault(0);
const viewParser = parseAsStringLiteral(["topics", "costs"] as const).withDefault("topics");

export default function SessionTopicsPanel({ accessToken, viewerScope }: { accessToken: string; viewerScope: string }) {
  const [view, setView] = useQueryState("view", viewParser);
  const [offset, setOffset] = useQueryState("offset", offsetParser);
  const topicOptions: UseQueryOptions<TopicPage> = {
    queryKey: ["session-topics", viewerScope, offset],
    queryFn: ({ signal }) =>
      apiClient.get<TopicPage>("/session_topics", {
        accessToken,
        query: { limit: pageSize, offset },
        signal,
      }),
    retry: false,
    staleTime: 30000,
    enabled: view === "topics",
  };
  const topics = useQuery(topicOptions);
  const overviewOptions: UseQueryOptions<TopicOverview> = {
    queryKey: ["session-topic-overview", viewerScope],
    queryFn: ({ signal }) =>
      apiClient.get<TopicOverview>("/session_topics/overview", {
        accessToken,
        query: { days: 7 },
        signal,
      }),
    retry: false,
    staleTime: 30000,
    enabled: view === "topics",
  };
  const overview = useQuery(overviewOptions);
  const disabled = topics.error instanceof ApiError && topics.error.status === 503;

  return (
    <Page>
      <PageHeader>
        <PageHeaderTitle>Session topics</PageHeaderTitle>
        <PageHeaderDescription>
          English task topics, at most 50 characters. Conversation content is not stored by this feature. Topics
          describe the first task, not every later turn, and are not proof of key misuse.
        </PageHeaderDescription>
      </PageHeader>
      <nav aria-label="Session topic views" className="flex gap-2">
        <Button
          variant={view === "topics" ? "default" : "outline"}
          aria-pressed={view === "topics"}
          onClick={() => void setView("topics")}
        >
          Topics
        </Button>
        <Button
          variant={view === "costs" ? "default" : "outline"}
          aria-pressed={view === "costs"}
          onClick={() => void setView("costs")}
        >
          Costs
        </Button>
      </nav>
      {view === "costs" ? (
        <SessionTopicCostsPanel accessToken={accessToken} viewerScope={viewerScope} />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-4">
            <Button
              variant="outline"
              onClick={() => {
                void topics.refetch();
                void overview.refetch();
              }}
            >
              Refresh
            </Button>
            {overview.data && (
              <p className="text-sm text-muted-foreground">
                Retained sessions: {overview.data.sessions.toLocaleString()} · Ready:{" "}
                {overview.data.ready.toLocaleString()}
                {" · "}Pending: {overview.data.pending.toLocaleString()} · Unavailable:{" "}
                {overview.data.unavailable.toLocaleString()}
              </p>
            )}
          </div>
          {disabled && <p role="status">Session topics are disabled. Ask an administrator to configure the feature.</p>}
          {!disabled && topics.isError && <p role="alert">Unable to load session topics.</p>}
          {topics.isPending && <p role="status">Loading session topics…</p>}
          {topics.isSuccess && (
            <>
              <Table aria-label="Session topics">
                <TableHeader>
                  <TableRow>
                    <TableHead>Topic</TableHead>
                    <TableHead>Category</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>User</TableHead>
                    <TableHead>Key reference</TableHead>
                    <TableHead>Last seen</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {topics.data.items.map((item) => (
                    <TableRow key={item.session_ref}>
                      <TableCell className="max-w-96 whitespace-normal break-words">
                        {item.topic ?? "Not generated"}
                      </TableCell>
                      <TableCell>{item.category?.replaceAll("_", " ") ?? "Unknown"}</TableCell>
                      <TableCell>{item.status.replaceAll("_", " ")}</TableCell>
                      <TableCell>{item.user_id ?? "Service key"}</TableCell>
                      <TableCell>
                        <code>{item.key_hash.slice(0, 12)}</code>
                      </TableCell>
                      <TableCell>{new Date(item.last_seen).toLocaleString()}</TableCell>
                    </TableRow>
                  ))}
                  {topics.data.items.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={6}>No session topics in this scope.</TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
              <div className="flex items-center gap-3">
                <Button
                  variant="outline"
                  disabled={offset === 0}
                  onClick={() => void setOffset(Math.max(0, offset - pageSize))}
                >
                  Previous
                </Button>
                <span className="text-sm">Page {Math.floor(offset / pageSize) + 1}</span>
                <Button
                  variant="outline"
                  disabled={topics.data.items.length < pageSize || offset + pageSize > 100000}
                  onClick={() => void setOffset(offset + pageSize)}
                >
                  Next
                </Button>
              </div>
            </>
          )}
          {overview.data && (
            <section aria-label="Separate usage accounting" className="space-y-3">
              <h2 className="text-lg font-medium">Last 7 days of gateway usage</h2>
              <p className="text-sm text-muted-foreground">
                Summary calls are separate from business inference. All costs remain in the gateway ledger. Only usage
                visible to your account is shown.
              </p>
              <Table aria-label="Usage by purpose">
                <TableHeader>
                  <TableRow>
                    <TableHead>Purpose</TableHead>
                    <TableHead>Requests</TableHead>
                    <TableHead>Tokens</TableHead>
                    <TableHead>Recorded cost (USD)</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {overview.data.usage.map((row) => (
                    <TableRow key={row.kind}>
                      <TableCell>{usageLabels[row.kind]}</TableCell>
                      <TableCell>{row.requests.toLocaleString()}</TableCell>
                      <TableCell>{row.tokens.toLocaleString()}</TableCell>
                      <TableCell>
                        {recordedDollars(row.spend)}
                        {(row.unpriced_requests ?? 0) + (row.unknown_cost_requests ?? 0) > 0 && (
                          <p className="text-xs text-muted-foreground">Incomplete pricing. See Costs for details.</p>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </section>
          )}
          {overview.isError && !disabled && (
            <p role="alert">Usage accounting is unavailable. No zero-cost assumption has been made.</p>
          )}
        </>
      )}
    </Page>
  );
}
