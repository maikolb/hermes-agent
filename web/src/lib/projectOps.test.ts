// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import {
  boardsForProject,
  buildProjectOpsSessionCreateParams,
  buildProjectOpsSessionResumeParams,
  buildTopicCreatePayload,
  clearPendingTopicCreate,
  decodeAuthorPrefix,
  emptyConversation,
  encodeAuthorPrefix,
  loadPendingTopicCreate,
  linkedTopics,
  pendingTopicCreateStorageKey,
  pickBoard,
  pickProject,
  persistPendingTopicCreate,
  reduceGatewayEvent,
  type ProjectOpsBoard,
  type ProjectOpsBoardResponse,
  type ProjectOpsProject,
} from "./projectOps";

const projects: ProjectOpsProject[] = [
  { id: "p1", slug: "alpha", name: "Alpha", primary_path: "C:/alpha" },
  { id: "p2", slug: "beta", name: "Beta", primary_path: "C:/beta" },
];

const boards: ProjectOpsBoard[] = [
  { slug: "alpha-main", name: "Alpha main", project_id: "p1" },
  { slug: "beta-main", name: "Beta main", project_id: "p2" },
  { slug: "unscoped", name: "Unscoped", project_id: null },
];

describe("Project Ops author attribution", () => {
  it("round-trips delimiter-like names and multiline text", () => {
    const encoded = encodeAuthorPrefix(
      { id: "user|42", label: "Maikol ] Ops" },
      "first line\nsecond line",
    );

    expect(decodeAuthorPrefix(encoded)).toEqual({
      author: { id: "user|42", label: "Maikol ] Ops" },
      text: "first line\nsecond line",
    });
  });

  it("leaves ordinary historical user messages untouched", () => {
    expect(decodeAuthorPrefix("plain message")).toEqual({
      author: null,
      text: "plain message",
    });
  });
});

describe("Project Ops Gateway event reducer", () => {
  it("converges two clients fed the same fan-out events", () => {
    const events = [
      {
        type: "message.start",
        session_id: "runtime-1",
        payload: { user: encodeAuthorPrefix({ id: "u1", label: "Lucas" }, "Ship it") },
      },
      { type: "message.delta", session_id: "runtime-1", payload: { text: "On " } },
      { type: "message.delta", session_id: "runtime-1", payload: { text: "it." } },
      { type: "message.complete", session_id: "runtime-1", payload: { text: "On it." } },
    ];

    const reduceAll = () =>
      events.reduce(
        (state, event) => reduceGatewayEvent(state, event, "runtime-1"),
        emptyConversation(),
      );

    expect(reduceAll()).toEqual(reduceAll());
    expect(reduceAll().messages).toMatchObject([
      { role: "user", authorId: "u1", authorLabel: "Lucas", text: "Ship it" },
      { role: "assistant", authorLabel: "Hermes", text: "On it." },
    ]);
    expect(reduceAll().busy).toBe(false);
  });

  it("ignores events from a different runtime", () => {
    const state = emptyConversation();
    expect(
      reduceGatewayEvent(
        state,
        { type: "message.delta", session_id: "other", payload: { text: "leak" } },
        "runtime-1",
      ),
    ).toBe(state);
  });

  it("ignores session events while no topic runtime is selected", () => {
    const state = emptyConversation();
    expect(
      reduceGatewayEvent(
        state,
        { type: "message.delta", session_id: "stale", payload: { text: "leak" } },
        null,
      ),
    ).toBe(state);
  });
});

describe("Project, board and topic selection", () => {
  it("keeps boards inside the selected project", () => {
    expect(boardsForProject(boards, "p1").map((board) => board.slug)).toEqual([
      "alpha-main",
    ]);
    expect(pickProject(projects, "p2")?.id).toBe("p2");
    expect(pickBoard(boards, "p2", "alpha-main")?.slug).toBe("beta-main");
  });

  it("exposes only durable session-linked tasks as topics", () => {
    const board: ProjectOpsBoardResponse = {
      latest_event_id: 1,
      columns: [
        {
          name: "todo",
          tasks: [
            { id: "t1", title: "Topic", status: "todo", session_id: "session-1" },
            { id: "t2", title: "Worker only", status: "todo", session_id: null },
          ],
        },
      ],
    };
    expect(linkedTopics(board).map((topic) => topic.id)).toEqual(["t1"]);
  });
});

describe("idempotent topic-create payload", () => {
  it("keeps the project, durable session and operation key stable", () => {
    const input = {
      operationId: "operation-123",
      project: projects[0],
      sessionId: "durable-session-1",
      title: " Shared topic ",
    };

    const first = buildTopicCreatePayload(input);
    const retry = buildTopicCreatePayload(input);

    expect(retry).toEqual(first);
    expect(first).toMatchObject({
      title: "Shared topic",
      project_id: "p1",
      session_id: "durable-session-1",
      idempotency_key: "project-ops:operation-123",
      goal_mode: false,
      triage: false,
    });
  });

  it("uses the same operation for durable session creation and active task creation", () => {
    const pending = {
      version: 1 as const,
      operationId: "operation-123",
      projectId: "p1",
      boardSlug: "alpha-main",
      title: "Shared topic",
      state: "pending" as const,
    };

    expect(buildProjectOpsSessionCreateParams(pending, projects[0], "coder")).toMatchObject({
      source: "project_ops",
      persist: true,
      creation_key: "project-ops:operation-123",
      profile: "coder",
    });
    expect(buildProjectOpsSessionResumeParams("stored-1", "coder")).toEqual({
      session_id: "stored-1",
      source: "project_ops",
      profile: "coder",
    });
  });
});

describe("pending topic recovery state", () => {
  it("persists before effects, survives reload and clears only after reconciliation", () => {
    localStorage.clear();
    const pending = {
      version: 1 as const,
      operationId: "operation-reload",
      projectId: "p2",
      boardSlug: "beta-main",
      title: "Recover me",
      state: "failed" as const,
      storedSessionId: "stored-2",
      error: "ambiguous response",
    };

    persistPendingTopicCreate(pending, "coder");
    expect(localStorage.getItem(pendingTopicCreateStorageKey("coder"))).not.toBeNull();
    expect(loadPendingTopicCreate("coder")).toEqual(pending);
    clearPendingTopicCreate("coder");
    expect(loadPendingTopicCreate("coder")).toBeNull();
  });
});
