import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  AlertTriangle,
  Columns3,
  FolderKanban,
  MessageSquare,
  Plus,
  Send,
  Users,
  WifiOff,
} from "lucide-react";

import { GatewayClient, type ConnectionState } from "@/lib/gatewayClient";
import { useProfileScope } from "@/contexts/useProfileScope";
import {
  boardsForProject,
  buildProjectOpsSessionCreateParams,
  buildProjectOpsSessionResumeParams,
  buildTopicCreatePayload,
  clearPendingTopicCreate,
  emptyConversation,
  hydrateConversation,
  linkedTopics,
  loadPendingTopicCreate,
  pickBoard,
  pickProject,
  persistPendingTopicCreate,
  projectOpsApi,
  reduceGatewayEvent,
  resolveProjectOpsAuthor,
  type ProjectOpsAuthor,
  type ProjectOpsBoard,
  type ProjectOpsBoardResponse,
  type ProjectOpsConversationState,
  type ProjectOpsProject,
  type ProjectOpsTask,
  type ProjectOpsTaskDetail,
  type PendingTopicCreate,
  type SessionCreateResponse,
  type SessionResumeResponse,
} from "@/lib/projectOps";
import { cn } from "@/lib/utils";

type MobileView = "topics" | "chat" | "board";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function statusLabel(status: string): string {
  return status.replaceAll("_", " ");
}

function sectionCard(className?: string) {
  return cn(
    "rounded-xl border border-current/15 bg-card shadow-sm",
    className,
  );
}

export default function ProjectOpsPage() {
  const { profile } = useProfileScope();
  const gatewayRef = useRef<GatewayClient | null>(null);
  const runtimeSessionRef = useRef<string | null>(null);
  const openSequenceRef = useRef(0);
  const boardSequenceRef = useRef(0);
  const recoveryOperationRef = useRef<string | null>(null);
  const pendingCreateRef = useRef<PendingTopicCreate | null>(null);

  const [projects, setProjects] = useState<ProjectOpsProject[]>([]);
  const [boards, setBoards] = useState<ProjectOpsBoard[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [boardSlug, setBoardSlug] = useState<string | null>(null);
  const [boardData, setBoardData] = useState<ProjectOpsBoardResponse | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectOpsTaskDetail | null>(null);
  const [author, setAuthor] = useState<ProjectOpsAuthor | null>(null);
  const [conversation, setConversation] = useState<ProjectOpsConversationState>(
    emptyConversation,
  );
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [loading, setLoading] = useState(true);
  const [boardLoading, setBoardLoading] = useState(false);
  const [topicLoading, setTopicLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newTopicTitle, setNewTopicTitle] = useState("");
  const [draft, setDraft] = useState("");
  const [apiError, setApiError] = useState<string | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [mobileView, setMobileView] = useState<MobileView>("topics");

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === projectId) ?? null,
    [projectId, projects],
  );
  const scopedBoards = useMemo(
    () => boardsForProject(boards, projectId),
    [boards, projectId],
  );
  const topics = useMemo(() => linkedTopics(boardData), [boardData]);
  const activeTask = useMemo(
    () => topics.find((task) => task.id === activeTaskId) ?? null,
    [activeTaskId, topics],
  );

  const reloadBoard = useCallback(async (slug: string) => {
    const sequence = ++boardSequenceRef.current;
    setBoardLoading(true);
    try {
      const next = await projectOpsApi.getBoard(slug);
      if (sequence !== boardSequenceRef.current) return;
      setBoardData(next);
      const available = linkedTopics(next);
      setActiveTaskId((current) =>
        available.some((task) => task.id === current) ? current : (available[0]?.id ?? null),
      );
      setApiError(null);
    } catch (error) {
      if (sequence !== boardSequenceRef.current) return;
      setBoardData(null);
      setApiError(`Board unavailable: ${errorText(error)}`);
    } finally {
      if (sequence === boardSequenceRef.current) setBoardLoading(false);
    }
  }, []);

  const completePendingTopicCreate = useCallback(async (
    pending: PendingTopicCreate,
    project: ProjectOpsProject,
    gateway: GatewayClient,
  ) => {
    let nextPending: PendingTopicCreate = {
      ...pending,
      state: "pending",
      error: undefined,
    };
    persistPendingTopicCreate(nextPending, profile);

    if (!nextPending.storedSessionId) {
      const created = await gateway.request<SessionCreateResponse>(
        "session.create",
        buildProjectOpsSessionCreateParams(nextPending, project, profile),
      );
      nextPending = {
        ...nextPending,
        runtimeSessionId: created.session_id,
        storedSessionId: created.stored_session_id,
      };
      pendingCreateRef.current = nextPending;
      persistPendingTopicCreate(nextPending, profile);
    } else if (!nextPending.runtimeSessionId) {
      const resumed = await gateway.request<SessionResumeResponse>(
        "session.resume",
        buildProjectOpsSessionResumeParams(nextPending.storedSessionId, profile),
      );
      nextPending = { ...nextPending, runtimeSessionId: resumed.session_id };
      pendingCreateRef.current = nextPending;
      persistPendingTopicCreate(nextPending, profile);
    }

    let response: { task: ProjectOpsTask };
    try {
      response = await projectOpsApi.createTopic(
        nextPending.boardSlug,
        buildTopicCreatePayload({
          operationId: nextPending.operationId,
          project,
          sessionId: nextPending.storedSessionId!,
          title: nextPending.title,
        }),
      );
    } catch (error) {
      // Cross-database saga compensation: the durable operation id survives,
      // but no live runtime is allowed to remain orphaned when task creation
      // fails after session.create. The backend restricts this reason to
      // Project Ops runtimes and reopens the same deterministic durable row on
      // retry; localStorage remains recovery state, never task authority.
      if (nextPending.runtimeSessionId) {
        await gateway.request("session.close", {
          session_id: nextPending.runtimeSessionId,
          reason: "orphaned_create",
        }).catch(() => undefined);
      }
      nextPending = {
        ...nextPending,
        state: "failed",
        runtimeSessionId: undefined,
        storedSessionId: undefined,
        error: errorText(error),
      };
      pendingCreateRef.current = nextPending;
      persistPendingTopicCreate(nextPending, profile);
      throw error;
    }

    if (nextPending.runtimeSessionId) {
      await gateway.request("session.subscribe", {
        session_id: nextPending.runtimeSessionId,
      });
    }
    clearPendingTopicCreate(profile);
    pendingCreateRef.current = null;
    setNewTopicTitle("");
    await reloadBoard(nextPending.boardSlug);
    setActiveTaskId(response.task.id);
    setApiError(null);
  }, [profile, reloadBoard]);

  useEffect(() => {
    let mounted = true;
    const gateway = new GatewayClient();
    gatewayRef.current = gateway;
    const offState = gateway.onState((state) => mounted && setConnection(state));
    const offEvents = gateway.onAny((event) => {
      if (!mounted) return;
      setConversation((current) =>
        reduceGatewayEvent(current, event, runtimeSessionRef.current),
      );
    });

    void gateway.connect().catch((error) => {
      if (mounted) setApiError(`Gateway disconnected: ${errorText(error)}`);
    });

    const loadCatalog = Promise.all([
      projectOpsApi.getProjects(),
      projectOpsApi.getBoards(),
    ])
      .then(([projectResponse, boardResponse]) => {
        if (!mounted) return;
        const selected = pickProject(projectResponse.projects);
        const selectedBoard = pickBoard(boardResponse.boards, selected?.id ?? null);
        setProjects(projectResponse.projects);
        setBoards(boardResponse.boards);
        setProjectId(selected?.id ?? null);
        setBoardSlug(selectedBoard?.slug ?? null);
      })
      .catch((error) => {
        if (mounted) setApiError(`Project Ops unavailable: ${errorText(error)}`);
      });
    const loadIdentity = resolveProjectOpsAuthor()
      .then((resolvedAuthor) => mounted && setAuthor(resolvedAuthor))
      .catch((error) => {
        if (mounted) {
          setAuthError(`Authenticated identity unavailable: ${errorText(error)}`);
        }
      });
    void Promise.allSettled([loadCatalog, loadIdentity]).then(() => {
      if (mounted) setLoading(false);
    });

    return () => {
      mounted = false;
      offEvents();
      offState();
      gateway.close();
      gatewayRef.current = null;
      runtimeSessionRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (boardSlug) void reloadBoard(boardSlug);
    else {
      setBoardData(null);
      setActiveTaskId(null);
    }
  }, [boardSlug, reloadBoard]);

  useEffect(() => {
    if (!activeTask?.session_id || connection !== "open") {
      ++openSequenceRef.current;
      const previousRuntime = runtimeSessionRef.current;
      const gateway = gatewayRef.current;
      if (previousRuntime && gateway) {
        void gateway
          .request("session.unsubscribe", { session_id: previousRuntime })
          .catch(() => undefined);
      }
      setDetail(null);
      setConversation(emptyConversation());
      runtimeSessionRef.current = null;
      return;
    }

    const sequence = ++openSequenceRef.current;
    const gateway = gatewayRef.current;
    if (!gateway || !boardSlug) return;
    const durableSessionId = activeTask.session_id;
    const previousRuntime = runtimeSessionRef.current;
    runtimeSessionRef.current = null;
    setTopicLoading(true);
    setConversation(emptyConversation());

    void (async () => {
      if (previousRuntime) {
        await gateway
          .request("session.unsubscribe", { session_id: previousRuntime })
          .catch(() => undefined);
      }
      const [taskDetail, resumed] = await Promise.all([
        projectOpsApi.getTask(boardSlug, activeTask.id),
        gateway.request<SessionResumeResponse>(
          "session.resume",
          buildProjectOpsSessionResumeParams(durableSessionId, profile),
        ),
      ]);
      await gateway.request("session.subscribe", { session_id: resumed.session_id });
      if (sequence !== openSequenceRef.current) {
        await gateway
          .request("session.unsubscribe", { session_id: resumed.session_id })
          .catch(() => undefined);
        return;
      }
      runtimeSessionRef.current = resumed.session_id;
      setDetail(taskDetail);
      setConversation({
        ...hydrateConversation(resumed.messages ?? []),
        busy: Boolean(resumed.running),
      });
      setApiError(null);
      setMobileView("chat");
    })()
      .catch((error) => {
        if (sequence !== openSequenceRef.current) return;
        setDetail(null);
        setApiError(`Topic unavailable: ${errorText(error)}`);
      })
      .finally(() => {
        if (sequence === openSequenceRef.current) setTopicLoading(false);
      });
  }, [activeTask, boardSlug, connection, profile]);

  useEffect(() => {
    if (connection !== "open" || !projects.length || !boards.length) return;
    const pending = loadPendingTopicCreate(profile);
    if (!pending || recoveryOperationRef.current === pending.operationId) return;
    const project = projects.find((item) => item.id === pending.projectId);
    const gateway = gatewayRef.current;
    if (!project || !gateway) return;

    recoveryOperationRef.current = pending.operationId;
    pendingCreateRef.current = pending;
    setCreating(true);
    void completePendingTopicCreate(pending, project, gateway)
      .catch((error) => {
        const failed = {
          ...pendingCreateRef.current!,
          state: "failed" as const,
          error: errorText(error),
        };
        pendingCreateRef.current = failed;
        persistPendingTopicCreate(failed, profile);
        setApiError(
          `Topic creation failed: ${errorText(error)}. Retry keeps the same operation.`,
        );
      })
      .finally(() => {
        recoveryOperationRef.current = null;
        setCreating(false);
      });
  }, [boards.length, completePendingTopicCreate, connection, profile, projects]);

  function selectProject(nextProjectId: string) {
    const nextBoard = pickBoard(boards, nextProjectId);
    setProjectId(nextProjectId);
    setBoardSlug(nextBoard?.slug ?? null);
    setActiveTaskId(null);
    setDetail(null);
    setMobileView("topics");
  }

  async function createTopic(event: FormEvent) {
    event.preventDefault();
    const title = newTopicTitle.trim();
    const gateway = gatewayRef.current;
    if (!title || !selectedProject || !boardSlug || !gateway || connection !== "open") {
      return;
    }

    setCreating(true);
    let pending = pendingCreateRef.current;
    if (
      !pending ||
      pending.projectId !== selectedProject.id ||
      pending.boardSlug !== boardSlug ||
      pending.title !== title
    ) {
      pending = {
        version: 1,
        operationId: crypto.randomUUID(),
        projectId: selectedProject.id,
        boardSlug,
        title,
        state: "pending",
      };
      pendingCreateRef.current = pending;
      persistPendingTopicCreate(pending, profile);
    }

    try {
      await completePendingTopicCreate(pending, selectedProject, gateway);
    } catch (error) {
      const failed = {
        ...pending,
        state: "failed" as const,
        error: errorText(error),
      };
      pendingCreateRef.current = failed;
      persistPendingTopicCreate(failed, profile);
      setApiError(`Topic creation failed: ${errorText(error)}. Retry keeps the same operation.`);
    } finally {
      setCreating(false);
    }
  }

  async function sendPrompt(event: FormEvent) {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const field = form.elements.namedItem("message");
    const text = (
      field instanceof HTMLTextAreaElement ? field.value : draft
    ).trim();
    const gateway = gatewayRef.current;
    const runtimeSessionId = runtimeSessionRef.current;
    if (!text || !author || !gateway || !runtimeSessionId || conversation.busy) return;
    setConversation((current) => ({ ...current, busy: true, error: null }));
    try {
      await gateway.request("prompt.submit", {
        session_id: runtimeSessionId,
        text,
      });
      setDraft("");
    } catch (error) {
      setConversation((current) => ({
        ...current,
        busy: false,
        error: `Message failed: ${errorText(error)}`,
      }));
    }
  }

  if (loading) {
    return <PageState title="Loading Project Ops" detail="Connecting projects, boards and identity…" />;
  }
  if (authError) {
    return <PageState tone="error" title="Authentication required" detail={authError} />;
  }

  return (
    <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden" data-testid="project-ops-page">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-current/15 px-4 py-3 lg:px-6">
        <div>
          <div className="flex items-center gap-2 text-midground">
            <FolderKanban className="h-5 w-5" />
            <h1 className="text-lg font-semibold">Project Ops</h1>
          </div>
          <p className="mt-1 text-xs text-text-secondary">
            Shared project topics, one durable Hermes session each.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-text-secondary">
          <Users className="h-4 w-4" />
          <span>{author?.label ?? "Identity unavailable"}</span>
          <span
            className={cn(
              "rounded-full px-2 py-1",
              connection === "open"
                ? "bg-emerald-500/15 text-emerald-300"
                : "bg-amber-500/15 text-amber-300",
            )}
          >
            {connection === "open" ? "Live" : connection}
          </span>
        </div>
      </header>

      {apiError && (
        <div role="alert" className="mx-4 mt-3 flex items-start gap-2 rounded-lg border border-red-400/30 bg-red-500/10 p-3 text-sm text-red-200 lg:mx-6">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{apiError}</span>
        </div>
      )}
      {connection !== "open" && (
        <div className="mx-4 mt-3 flex items-center gap-2 rounded-lg border border-amber-400/25 bg-amber-500/10 p-3 text-sm text-amber-200 lg:mx-6">
          <WifiOff className="h-4 w-4" />
          Shared chat is unavailable until the Gateway reconnects.
        </div>
      )}

      {!projects.length ? (
        <PageState title="No projects yet" detail="Create a Hermes project, then link a Kanban board to open shared topics." />
      ) : (
        <>
          <div className="flex shrink-0 gap-2 overflow-x-auto border-b border-current/10 px-4 py-3 lg:px-6">
            {projects.map((project) => (
              <button
                key={project.id}
                type="button"
                onClick={() => selectProject(project.id)}
                className={cn(
                  "shrink-0 rounded-full border px-3 py-1.5 text-sm transition",
                  project.id === projectId
                    ? "border-midground bg-midground/15 text-midground"
                    : "border-current/15 text-text-secondary hover:text-text-primary",
                )}
              >
                {project.icon ? `${project.icon} ` : ""}{project.name}
              </button>
            ))}
          </div>

          <div className="grid min-h-0 min-w-0 flex-1 grid-cols-1 grid-rows-[minmax(0,1fr)] overflow-hidden lg:grid-cols-[minmax(14rem,18rem)_minmax(20rem,1fr)_minmax(18rem,23rem)]">
            <aside
              className={cn(
                "min-h-0 flex-col overflow-y-auto border-r border-current/10 p-3",
                mobileView === "topics" ? "flex" : "hidden",
                "lg:flex",
              )}
              data-testid="topics-panel"
            >
              <label className="mb-3 text-xs font-medium uppercase tracking-wider text-text-secondary">
                Board
                <select
                  aria-label="Board"
                  className="mt-1 w-full rounded-lg border border-current/15 bg-background-base px-3 py-2 text-sm text-text-primary"
                  value={boardSlug ?? ""}
                  onChange={(event) => setBoardSlug(event.target.value || null)}
                >
                  {!scopedBoards.length && <option value="">No linked boards</option>}
                  {scopedBoards.map((board) => (
                    <option key={board.slug} value={board.slug}>{board.name || board.slug}</option>
                  ))}
                </select>
              </label>

              {boardSlug && (
                <form onSubmit={createTopic} className="mb-3 flex gap-2">
                  <input
                    aria-label="New topic title"
                    value={newTopicTitle}
                    onChange={(event) => setNewTopicTitle(event.target.value)}
                    placeholder="New topic"
                    className="min-w-0 flex-1 rounded-lg border border-current/15 bg-background-base px-3 py-2 text-sm outline-none focus:border-midground"
                  />
                  <button
                    type="submit"
                    aria-label="Create topic"
                    disabled={creating || connection !== "open" || !newTopicTitle.trim()}
                    className="rounded-lg bg-midground px-3 text-background-base disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Plus className="h-4 w-4" />
                  </button>
                </form>
              )}

              <div className="mb-2 flex items-center justify-between text-xs uppercase tracking-wider text-text-secondary">
                <span>Topics</span><span>{topics.length}</span>
              </div>
              {boardLoading ? (
                <InlineState text="Loading topics…" />
              ) : !boardSlug ? (
                <InlineState text="This project has no linked board." />
              ) : !topics.length ? (
                <InlineState text="No session-linked topics on this board." />
              ) : (
                <div className="space-y-1">
                  {topics.map((task) => (
                    <button
                      type="button"
                      key={task.id}
                      onClick={() => setActiveTaskId(task.id)}
                      className={cn(
                        "w-full rounded-lg border px-3 py-2 text-left transition",
                        task.id === activeTaskId
                          ? "border-midground/60 bg-midground/10"
                          : "border-transparent hover:border-current/15 hover:bg-card",
                      )}
                    >
                      <span className="block truncate text-sm font-medium"># {task.title}</span>
                      <span className="mt-1 flex items-center justify-between text-xs text-text-secondary">
                        <span className="capitalize">{statusLabel(task.status)}</span>
                        {task.warnings?.count ? <span>{task.warnings.count} warning(s)</span> : null}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </aside>

            <section
              className={cn(
                "min-h-0 min-w-0 flex-col",
                mobileView === "chat" ? "flex" : "hidden",
                "lg:flex",
              )}
              data-testid="chat-panel"
            >
              <div className="shrink-0 border-b border-current/10 px-4 py-3">
                <h2 className="truncate font-medium">{activeTask ? `# ${activeTask.title}` : "Choose a topic"}</h2>
                <p className="mt-0.5 text-xs text-text-secondary">
                  {activeTask?.session_id ? `Session ${activeTask.session_id}` : "A topic opens one shared Hermes transcript."}
                </p>
              </div>
              <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4" aria-live="polite">
                {topicLoading ? (
                  <InlineState text="Resuming shared session…" />
                ) : !activeTask ? (
                  <InlineState text="Select or create a topic to begin." />
                ) : !conversation.messages.length && !conversation.streamText ? (
                  <InlineState text="No messages yet. Start the shared conversation." />
                ) : (
                  <>
                    {conversation.messages.map((message) => (
                      <article
                        key={message.id}
                        className={cn(
                          "max-w-[88%] rounded-xl border px-3 py-2",
                          message.role === "user"
                            ? "ml-auto border-midground/30 bg-midground/10"
                            : "border-current/15 bg-card",
                        )}
                      >
                        <div className="mb-1 text-xs font-semibold text-midground">{message.authorLabel}</div>
                        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{message.text}</p>
                      </article>
                    ))}
                    {conversation.streamText && (
                      <article className="max-w-[88%] rounded-xl border border-current/15 bg-card px-3 py-2">
                        <div className="mb-1 text-xs font-semibold text-midground">Hermes · streaming</div>
                        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{conversation.streamText}</p>
                      </article>
                    )}
                  </>
                )}
              </div>
              {conversation.error && <div role="alert" className="mx-4 mb-2 text-sm text-red-300">{conversation.error}</div>}
              <form onSubmit={sendPrompt} className="shrink-0 border-t border-current/10 p-3">
                <div className="flex items-end gap-2 rounded-xl border border-current/15 bg-card p-2 focus-within:border-midground/60">
                  <textarea
                    aria-label="Message"
                    name="message"
                    rows={2}
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    placeholder={activeTask ? `Message as ${author?.label ?? "member"}` : "Choose a topic"}
                    disabled={!activeTask || connection !== "open" || conversation.busy}
                    className="max-h-32 min-h-10 min-w-0 flex-1 resize-none bg-transparent px-2 py-1 text-sm outline-none disabled:opacity-50"
                  />
                  <button
                    type="submit"
                    aria-label="Send message"
                    disabled={!draft.trim() || !activeTask || connection !== "open" || conversation.busy}
                    className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-midground text-background-base disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <Send className="h-4 w-4" />
                  </button>
                </div>
              </form>
            </section>

            <aside
              className={cn(
                "min-h-0 flex-col gap-3 overflow-y-auto border-l border-current/10 p-3",
                mobileView === "board" ? "flex" : "hidden",
                "lg:flex",
              )}
              data-testid="board-panel"
            >
              <BoardSummary board={boardData} />
              <TaskDetail detail={detail} task={activeTask} loading={topicLoading} />
            </aside>
          </div>

          <nav className="grid shrink-0 grid-cols-3 border-t border-current/15 bg-background-base lg:hidden" aria-label="Project Ops views">
            {([
              ["topics", MessageSquare, "Topics"],
              ["chat", Send, "Chat"],
              ["board", Columns3, "Board"],
            ] as const).map(([view, Icon, label]) => (
              <button
                key={view}
                type="button"
                onClick={() => setMobileView(view)}
                className={cn(
                  "flex min-h-14 items-center justify-center gap-2 text-sm",
                  mobileView === view ? "text-midground" : "text-text-secondary",
                )}
              >
                <Icon className="h-4 w-4" />{label}
              </button>
            ))}
          </nav>
        </>
      )}
    </main>
  );
}

function InlineState({ text }: { text: string }) {
  return <div className="rounded-lg border border-dashed border-current/15 p-4 text-center text-sm text-text-secondary">{text}</div>;
}

function PageState({
  title,
  detail,
  tone = "neutral",
}: {
  title: string;
  detail: string;
  tone?: "neutral" | "error";
}) {
  return (
    <main className="flex min-h-0 flex-1 items-center justify-center p-6">
      <div className={cn(sectionCard("max-w-lg p-6 text-center"), tone === "error" && "border-red-400/30")}>
        <h1 className="text-lg font-semibold">{title}</h1>
        <p className={cn("mt-2 text-sm text-text-secondary", tone === "error" && "text-red-300")}>{detail}</p>
      </div>
    </main>
  );
}

function BoardSummary({ board }: { board: ProjectOpsBoardResponse | null }) {
  return (
    <section className={sectionCard("p-3")}>
      <div className="mb-3 flex items-center gap-2">
        <Columns3 className="h-4 w-4 text-midground" />
        <h2 className="text-sm font-semibold">Board summary</h2>
      </div>
      {!board?.columns.length ? (
        <p className="text-sm text-text-secondary">No board state loaded.</p>
      ) : (
        <div className="grid grid-cols-2 gap-2">
          {board.columns.map((column) => (
            <div key={column.name} className="rounded-lg bg-background-base/70 p-2">
              <div className="text-[0.68rem] uppercase tracking-wide text-text-secondary">{statusLabel(column.name)}</div>
              <div className="mt-1 text-lg font-semibold">{column.tasks.length}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function TaskDetail({
  detail,
  task,
  loading,
}: {
  detail: ProjectOpsTaskDetail | null;
  task: ProjectOpsTask | null;
  loading: boolean;
}) {
  if (loading) return <InlineState text="Loading task detail…" />;
  if (!task) return <InlineState text="Choose a topic for task detail." />;
  const diagnostics = detail?.task?.diagnostics ?? [];
  return (
    <section className={sectionCard("p-3")}>
      <h2 className="text-sm font-semibold">Task detail</h2>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-2 text-sm">
        <dt className="text-text-secondary">Status</dt><dd className="capitalize">{statusLabel(task.status)}</dd>
        <dt className="text-text-secondary">Assignee</dt><dd>{task.assignee || "Unassigned"}</dd>
        <dt className="text-text-secondary">Session</dt><dd className="truncate font-mono text-xs">{task.session_id}</dd>
      </dl>
      {task.body && <p className="mt-3 whitespace-pre-wrap text-sm text-text-secondary">{task.body}</p>}

      <DetailList title="Comments" empty="No comments." items={(detail?.comments ?? []).map((comment) => ({
        key: String(comment.id),
        heading: comment.author || "Member",
        body: comment.body,
      }))} />
      <DetailList title="Runs" empty="No runs yet." items={(detail?.runs ?? []).map((run) => ({
        key: String(run.id),
        heading: `${run.profile || "Hermes"} · ${run.status || run.outcome || "unknown"}`,
        body: run.summary || run.error || "No run evidence recorded.",
      }))} />
      <DetailList title="Warnings" empty="No active warnings." items={diagnostics.map((warning, index) => ({
        key: `${warning.kind || "warning"}-${index}`,
        heading: `${warning.severity || "warning"} · ${warning.kind || "diagnostic"}`,
        body: warning.message || warning.detail || `${warning.count || 1} occurrence(s)`,
      }))} />
      <DetailList title="Evidence" empty="No task events yet." items={(detail?.events ?? []).slice(-6).reverse().map((event) => ({
        key: String(event.id),
        heading: event.kind,
        body: typeof event.payload === "string" ? event.payload : event.payload ? JSON.stringify(event.payload) : "Recorded event",
      }))} />
    </section>
  );
}

function DetailList({
  title,
  empty,
  items,
}: {
  title: string;
  empty: string;
  items: Array<{ key: string; heading: string; body: string }>;
}) {
  return (
    <div className="mt-4 border-t border-current/10 pt-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-text-secondary">{title}</h3>
      {!items.length ? (
        <p className="mt-2 text-xs text-text-secondary">{empty}</p>
      ) : (
        <ul className="mt-2 space-y-2">
          {items.map((item) => (
            <li key={item.key} className="rounded-lg bg-background-base/70 p-2">
              <div className="text-xs font-medium">{item.heading}</div>
              <p className="mt-1 whitespace-pre-wrap break-words text-xs text-text-secondary">{item.body}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
