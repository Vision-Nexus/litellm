"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import SessionTopicsPanel from "@/components/session_topics/SessionTopicsPanel";

export default function SessionTopicsPage() {
  const { accessToken, userId, userRole, isAuthorized } = useAuthorized();
  if (!accessToken || !userId || !isAuthorized) return <p role="status">Loading session topics…</p>;
  return <SessionTopicsPanel accessToken={accessToken} viewerScope={`${userId}:${userRole}`} />;
}
