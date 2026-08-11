import { api, fetchJSON, type AuthMeResponse, type SessionMessage } from "@/lib/api";
import type { GatewayEvent } from "@/lib/gatewayClient";

export const PROJECT_OPS_API = "/api/plugins/kanban";

export interface ProjectOpsProject {
  id: string;
  slug: string;
  name: string;
  primary_path: string;
  icon?: string;
  color?: string;
}

export interface ProjectOpsBoard {
  slug: string;
  name?: string | null;
  description?: string | null;
  project_id?: string | null;
  project_name?: string | null;
  default_workdir?: string | null;
  counts?: Record<string, number>;
  total?: number;
}

export interface ProjectOpsWarningSummary {
  count: number;
  highest_severity?: string | null;
  kinds?: Record<string, number>;
}

export interface ProjectOpsTask {
  id: string;
  title: string;
  body?: string | null;
  status: string;
  assignee?: string | null;
  priority?: number;
  project_id?: string | null;
  session_id?: string | null;
  latest_summary?: string | null;
  comment_count?: number;
  warnings?: ProjectOpsWarningSummary | null;
  diagnostics?: ProjectOpsDiagnostic[];
}

export interface ProjectOpsColumn {
  name: string;
  tasks: ProjectOpsTask[];
}

export interface ProjectOpsBoardResponse {
  columns: ProjectOpsColumn[];
  latest_event_id: number;
}

export interface ProjectOpsComment {
  id: number | string;
  author: string;
  body: string;
  created_at: number;
}

export interface ProjectOpsRun {
  id: number | string;
  profile?: string | null;
  status?: string | null;
  outcome?: string | null;
  summary?: string | null;
  error?: string | null;
  started_at?: number | null;
  ended_at?: number | null;
}

export interface ProjectOpsDiagnostic {
  kind?: string;
  severity?: string;
  message?: string;
  detail?: string;
  count?: number;
}

export interface ProjectOpsTaskDetail {
  task: ProjectOpsTask;
  comments: ProjectOpsComment[];
  runs: ProjectOpsRun[];
  events: Array<{
    id: number | string;
    kind: string;
    payload?: unknown;
    created_at?: number;
  }>;
}

export interface ProjectOpsAuthor {
  id: string;
  label: string;
}

export interface ProjectOpsMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  authorId?: string;
  authorLabel: string;
}

export interface ProjectOpsConversationState {
  messages: ProjectOpsMessage[];
  streamText: string;
  busy: boolean;
  error: string | null;
  nextMessageId: number;
}

export interface SessionCreateResponse {
  session_id: string;
  stored_session_id: string;
  messages?: SessionMessage[];
}

export interface SessionResumeResponse {
  session_id: string;
  session_key: string;
  resumed?: string;
  messages: SessionMessage[];
  running?: boolean;
}

export interface TopicCreatePayload {
  title: string;
  body: string;
  priority: number;
  workspace_kind: "dir" | "scratch";
  workspace_path?: string;
  parents: string[];
  triage: boolean;
  goal_mode: boolean;
  project_id: string;
  session_id: string;
  idempotency_key: string;
}

export interface PendingTopicCreate {
  version: 1;
  operationId: string;
  projectId: string;
  boardSlug: string;
  title: string;
  state: "pending" | "failed";
  runtimeSessionId?: string;
  storedSessionId?: string;
  error?: string;
}

const PENDING_TOPIC_CREATE_PREFIX = "hermes.project-ops.pending-topic-create.v1";

export const emptyConversation = (): ProjectOpsConversationState => ({
  messages: [],
  streamText: "",
  busy: false,
  error: null,
  nextMessageId: 1,
});

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function encodeAuthorPrefix(author: ProjectOpsAuthor, text: string): string {
  return `[${encodeURIComponent(author.label)}|${encodeURIComponent(author.id)}] ${text}`;
}

export function decodeAuthorPrefix(value: string): {
  author: ProjectOpsAuthor | null;
  text: string;
} {
  const match = /^\[([^|\]]+)\|([^\]]+)\]\s([\s\S]*)$/.exec(value);
  if (!match) return { author: null, text: value };
  return {
    author: { label: safeDecode(match[1]), id: safeDecode(match[2]) },
    text: match[3],
  };
}

function appendMessage(
  state: ProjectOpsConversationState,
  role: ProjectOpsMessage["role"],
  text: string,
): ProjectOpsConversationState {
  if (!text) return state;
  const decoded = role === "user" ? decodeAuthorPrefix(text) : { author: null, text };
  const last = state.messages.at(-1);
  if (last?.role === role && last.text === decoded.text) return state;
  return {
    ...state,
    messages: [
      ...state.messages,
      {
        id: `message-${state.nextMessageId}`,
        role,
        text: decoded.text,
        authorId: decoded.author?.id,
        authorLabel: decoded.author?.label ?? (role === "assistant" ? "Hermes" : "Member"),
      },
    ],
    nextMessageId: state.nextMessageId + 1,
  };
}

export function hydrateConversation(messages: SessionMessage[]): ProjectOpsConversationState {
  return messages.reduce((state, message) => {
    if ((message.role !== "user" && message.role !== "assistant") || !message.content) {
      return state;
    }
    return appendMessage(state, message.role, message.content);
  }, emptyConversation());
}

function payloadText(payload: unknown, key = "text"): string {
  if (!payload || typeof payload !== "object") return "";
  const value = (payload as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

export function reduceGatewayEvent(
  state: ProjectOpsConversationState,
  event: GatewayEvent,
  runtimeSessionId?: string | null,
): ProjectOpsConversationState {
  if (!runtimeSessionId || event.session_id !== runtimeSessionId) {
    return state;
  }
  if (event.type === "message.start") {
    const withUser = appendMessage(state, "user", payloadText(event.payload, "user"));
    return { ...withUser, busy: true, streamText: "", error: null };
  }
  if (event.type === "message.delta") {
    return { ...state, busy: true, streamText: state.streamText + payloadText(event.payload) };
  }
  if (event.type === "message.complete") {
    const finalText = payloadText(event.payload) || state.streamText;
    const completed = appendMessage(state, "assistant", finalText);
    return { ...completed, busy: false, streamText: "", error: null };
  }
  if (event.type === "error") {
    return {
      ...state,
      busy: false,
      streamText: "",
      error: payloadText(event.payload, "message") || "Hermes reported an error.",
    };
  }
  if (event.type === "session.info") {
    const payload = event.payload as { running?: boolean } | undefined;
    return payload?.running === false ? { ...state, busy: false } : state;
  }
  return state;
}

export function boardsForProject(
  boards: ProjectOpsBoard[],
  projectId: string | null,
): ProjectOpsBoard[] {
  if (!projectId) return [];
  return boards.filter((board) => board.project_id === projectId);
}

export function pickProject(
  projects: ProjectOpsProject[],
  requestedId?: string | null,
): ProjectOpsProject | null {
  return projects.find((project) => project.id === requestedId) ?? projects[0] ?? null;
}

export function pickBoard(
  boards: ProjectOpsBoard[],
  projectId: string | null,
  requestedSlug?: string | null,
): ProjectOpsBoard | null {
  const scoped = boardsForProject(boards, projectId);
  return scoped.find((board) => board.slug === requestedSlug) ?? scoped[0] ?? null;
}

export function linkedTopics(board: ProjectOpsBoardResponse | null): ProjectOpsTask[] {
  return (board?.columns ?? []).flatMap((column) =>
    column.tasks.filter((task) => Boolean(task.session_id)),
  );
}

export function buildTopicCreatePayload(input: {
  operationId: string;
  project: ProjectOpsProject;
  sessionId: string;
  title: string;
}): TopicCreatePayload {
  const cwd = input.project.primary_path.trim();
  return {
    title: input.title.trim(),
    body: "Collaborative Project Ops topic",
    priority: 0,
    workspace_kind: cwd ? "dir" : "scratch",
    ...(cwd ? { workspace_path: cwd } : {}),
    parents: [],
    triage: false,
    goal_mode: false,
    project_id: input.project.id,
    session_id: input.sessionId,
    idempotency_key: `project-ops:${input.operationId}`,
  };
}

export function buildProjectOpsSessionCreateParams(
  pending: PendingTopicCreate,
  project: ProjectOpsProject,
  profile?: string | null,
) {
  const selectedProfile = (profile || "").trim();
  return {
    source: "project_ops",
    persist: true,
    creation_key: `project-ops:${pending.operationId}`,
    cwd: project.primary_path,
    title: pending.title,
    ...(selectedProfile ? { profile: selectedProfile } : {}),
  };
}

export function buildProjectOpsSessionResumeParams(
  sessionId: string,
  profile?: string | null,
) {
  const selectedProfile = (profile || "").trim();
  return {
    session_id: sessionId,
    source: "project_ops",
    ...(selectedProfile ? { profile: selectedProfile } : {}),
  };
}

export function pendingTopicCreateStorageKey(profile?: string | null): string {
  return `${PENDING_TOPIC_CREATE_PREFIX}:${(profile || "current").trim() || "current"}`;
}

export function persistPendingTopicCreate(
  pending: PendingTopicCreate,
  profile?: string | null,
): void {
  window.localStorage.setItem(
    pendingTopicCreateStorageKey(profile),
    JSON.stringify(pending),
  );
}

export function loadPendingTopicCreate(
  profile?: string | null,
): PendingTopicCreate | null {
  const raw = window.localStorage.getItem(pendingTopicCreateStorageKey(profile));
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as PendingTopicCreate;
    if (
      parsed?.version !== 1 ||
      !parsed.operationId ||
      !parsed.projectId ||
      !parsed.boardSlug ||
      !parsed.title
    ) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function clearPendingTopicCreate(profile?: string | null): void {
  window.localStorage.removeItem(pendingTopicCreateStorageKey(profile));
}

export async function resolveProjectOpsAuthor(): Promise<ProjectOpsAuthor> {
  if (!window.__HERMES_AUTH_REQUIRED__) {
    return { id: "owner", label: "Owner" };
  }
  const me: AuthMeResponse = await api.getAuthMe();
  return {
    id: me.user_id,
    label: me.display_name || me.email || me.user_id,
  };
}

export const projectOpsApi = {
  getProjects: () =>
    fetchJSON<{ projects: ProjectOpsProject[] }>(`${PROJECT_OPS_API}/projects`),
  getBoards: () =>
    fetchJSON<{ boards: ProjectOpsBoard[]; current?: string }>(`${PROJECT_OPS_API}/boards`),
  getBoard: (slug: string) =>
    fetchJSON<ProjectOpsBoardResponse>(
      `${PROJECT_OPS_API}/board?board=${encodeURIComponent(slug)}`,
    ),
  getTask: (boardSlug: string, taskId: string) =>
    fetchJSON<ProjectOpsTaskDetail>(
      `${PROJECT_OPS_API}/tasks/${encodeURIComponent(taskId)}?board=${encodeURIComponent(boardSlug)}`,
    ),
  createTopic: (boardSlug: string, payload: TopicCreatePayload) =>
    fetchJSON<{ task: ProjectOpsTask }>(
      `${PROJECT_OPS_API}/tasks?board=${encodeURIComponent(boardSlug)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
};
