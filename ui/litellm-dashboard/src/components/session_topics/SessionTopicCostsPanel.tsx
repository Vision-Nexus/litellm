"use client";

import { useQuery } from "@tanstack/react-query";
import { createParser, parseAsStringLiteral, useQueryStates } from "nuqs";
import { apiClient } from "@/components/networking";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError } from "@/lib/http/client";
import type { components } from "@/lib/http/schema";
import { costPresentation, usageLabels, type TopicCostTotals } from "./costPresentation";

type CostPage = components["schemas"]["TopicCostPage"];
type CostBucket = components["schemas"]["TopicCostBucket"];

const groupings = ["session", "topic", "key"] as const;
const groupingParser = parseAsStringLiteral(groupings).withDefault("session");
const daysParser = createParser({
  parse: (value) => (/^\d{1,3}$/.test(value) && Number(value) >= 1 && Number(value) <= 365 ? Number(value) : null),
  serialize: String,
}).withDefault(7);
const offsetParser = createParser({
  parse: (value) => (/^\d{1,6}$/.test(value) && Number(value) <= 100000 ? Number(value) : null),
  serialize: String,
}).withDefault(0);
const queryParsers = { cost_group: groupingParser, cost_days: daysParser, cost_offset: offsetParser };
const pageSize = 50;

function CostValue({ cost }: { cost: TopicCostTotals | null }) {
  const presentation = costPresentation(cost);
  return (
    <div>
      <span>{presentation.label}</span>
      {presentation.detail && <p className="max-w-64 text-xs text-muted-foreground">{presentation.detail}</p>}
    </div>
  );
}

function CostBuckets({ title, buckets }: { title: string; buckets: CostBucket[] }) {
  return (
    <section aria-label={title} className="space-y-2">
      <h3 className="font-medium">{title}</h3>
      {buckets.length === 0 ? (
        <p className="text-sm text-muted-foreground">No retained requests in this scope.</p>
      ) : (
        <Table aria-label={title}>
          <TableHeader>
            <TableRow>
              <TableHead>Purpose</TableHead>
              <TableHead>Requests</TableHead>
              <TableHead>Tokens</TableHead>
              <TableHead>Amount (USD)</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {buckets.map((bucket) => (
              <TableRow key={bucket.kind}>
                <TableCell>{usageLabels[bucket.kind]}</TableCell>
                <TableCell>{bucket.costs.requests.toLocaleString()}</TableCell>
                <TableCell>{bucket.costs.tokens.toLocaleString()}</TableCell>
                <TableCell>
                  <CostValue cost={bucket.costs} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

export default function SessionTopicCostsPanel({
  accessToken,
  viewerScope,
}: {
  accessToken: string;
  viewerScope: string;
}) {
  const [query, setQuery] = useQueryStates(queryParsers);
  const costs = useQuery<CostPage>({
    queryKey: ["session-topic-costs", viewerScope, query.cost_group, query.cost_days, query.cost_offset],
    queryFn: ({ signal }) =>
      apiClient.get<CostPage>("/session_topics/costs", {
        accessToken,
        query: { group_by: query.cost_group, days: query.cost_days, offset: query.cost_offset, limit: pageSize },
        signal,
      }),
    retry: false,
    staleTime: 30000,
  });
  const disabled = costs.error instanceof ApiError && costs.error.status === 503;

  return (
    <section aria-label="Session cost details" className="space-y-4">
      <div className="flex flex-wrap items-center gap-4">
        <label htmlFor="topic-cost-group">Group by</label>
        <select
          id="topic-cost-group"
          className="rounded-md border p-2"
          value={query.cost_group}
          onChange={(event) => {
            const group = groupingParser.parse(event.target.value);
            if (group) void setQuery({ cost_group: group, cost_offset: 0 });
          }}
        >
          <option value="session">Session</option>
          <option value="topic">Exact topic and category</option>
          <option value="key">Key reference</option>
        </select>
        <label htmlFor="topic-cost-days">Time window</label>
        <select
          id="topic-cost-days"
          className="rounded-md border p-2"
          value={query.cost_days}
          onChange={(event) => {
            const days = daysParser.parse(event.target.value);
            if (days) void setQuery({ cost_days: days, cost_offset: 0 });
          }}
        >
          {Array.from(new Set([7, 30, 90, 365, query.cost_days]))
            .sort((a, b) => a - b)
            .map((days) => (
              <option key={days} value={days}>
                Last {days} days
              </option>
            ))}
        </select>
        <Button variant="outline" onClick={() => void costs.refetch()}>
          Refresh costs
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        Costs use retained gateway records, not provider invoices. Missing prices are not free calls. Internal summary
        costs are administrator-only. Billing writes are asynchronous and can appear after the topic.
      </p>
      {disabled && <p role="status">Session costs are unavailable while session topics are disabled.</p>}
      {!disabled && costs.isError && (
        <p role="alert">Unable to load session costs. No zero-cost assumption has been made.</p>
      )}
      {costs.isPending && <p role="status">Loading session costs…</p>}
      {costs.data && (
        <>
          <p className="text-sm text-muted-foreground">
            {costs.data.total_groups.toLocaleString()} groups. Topic retention: {costs.data.topic_retention_days} days
            since last activity. Grouping matches exact text and category, not semantic similarity.
          </p>
          {costs.data.legacy_scan_truncated && (
            <p role="status">
              Historical lookup reached its {costs.data.legacy_scan_limit.toLocaleString()}-row limit. Unmatched charges
              remain in unassociated usage; this is not complete historical attribution.
            </p>
          )}
          <Table aria-label="Session cost breakdown">
            <TableHeader>
              <TableRow>
                <TableHead>Topic / key</TableHead>
                <TableHead>Sessions</TableHead>
                <TableHead>Business requests</TableHead>
                <TableHead>Business tokens</TableHead>
                <TableHead>Business (USD)</TableHead>
                <TableHead>Summaries (USD)</TableHead>
                <TableHead>Total (USD)</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {costs.data.items.map((row) => (
                <TableRow key={row.session_ref ?? row.key_hash ?? JSON.stringify([row.topic, row.category])}>
                  <TableCell className="max-w-96 whitespace-normal break-words">
                    {query.cost_group === "key" ? `Key ${row.key_hash?.slice(0, 12)}` : row.topic ?? "Not generated"}
                    <p className="text-xs text-muted-foreground">
                      {row.category?.replaceAll("_", " ") ?? "Unclassified"}
                      {row.key_hash && ` · Key ${row.key_hash.slice(0, 12)}`}
                      {row.session_ref && ` · Session ${row.session_ref.slice(0, 12)}`}
                    </p>
                    {row.historical_business_requests > 0 && (
                      <p className="text-xs text-muted-foreground">
                        {row.historical_business_requests} historical business requests linked
                      </p>
                    )}
                  </TableCell>
                  <TableCell>{row.sessions.toLocaleString()}</TableCell>
                  <TableCell>{row.business.requests.toLocaleString()}</TableCell>
                  <TableCell>{row.business.tokens.toLocaleString()}</TableCell>
                  <TableCell>
                    <CostValue cost={row.business} />
                  </TableCell>
                  <TableCell>
                    <CostValue cost={row.summary} />
                  </TableCell>
                  <TableCell>
                    <CostValue cost={row.total} />
                  </TableCell>
                </TableRow>
              ))}
              {costs.data.items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7}>No retained sessions in this window.</TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
          <div className="flex items-center gap-3">
            <Button
              variant="outline"
              disabled={query.cost_offset === 0}
              onClick={() => void setQuery({ cost_offset: Math.max(0, query.cost_offset - pageSize) })}
            >
              Previous cost page
            </Button>
            <span>Page {Math.floor(query.cost_offset / pageSize) + 1}</span>
            <Button
              variant="outline"
              disabled={
                query.cost_offset + pageSize >= costs.data.total_groups || query.cost_offset + pageSize > 100000
              }
              onClick={() => void setQuery({ cost_offset: query.cost_offset + pageSize })}
            >
              Next cost page
            </Button>
          </div>
          <CostBuckets title="All retained usage in this window" buckets={costs.data.totals} />
          <p className="text-sm text-muted-foreground">
            Unassociated usage includes missing or expired topics, absent session IDs, unrecoverable historical requests
            and summaries without a verified source session. No costs are apportioned by title or timing.
          </p>
          <CostBuckets title="Unassociated usage" buckets={costs.data.unassociated} />
        </>
      )}
    </section>
  );
}
