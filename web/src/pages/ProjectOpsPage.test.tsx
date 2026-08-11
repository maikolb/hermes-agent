// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchJSON = vi.hoisted(() => vi.fn());
const getAuthMe = vi.hoisted(() => vi.fn());
const gatewayRequest = vi.hoisted(() => vi.fn());
const gatewayEvents = vi.hoisted(() => new Set<(event: unknown) => void>());

vi.mock("@/lib/api", () => ({
  api: { getAuthMe },
  fetchJSON,
}));

vi.mock("@/lib/gatewayClient", () => ({
  GatewayClient: class {
    private stateHandlers = new Set<(state: string) => void>();

    close() {}

    async connect() {
      for (const handler of this.stateHandlers) handler("open");
    }

    onAny(handler: (event: unknown) => void) {
      gatewayEvents.add(handler);
      return () => gatewayEvents.delete(handler);
    }

    onState(handler: (state: string) => void) {
      this.stateHandlers.add(handler);
      handler("idle");
      return () => this.stateHandlers.delete(handler);
    }

    request(method: string, params: unknown) {
      return gatewayRequest(method, params);
    }
  },
}));

let container: HTMLDivElement;
let root: Root;

async function render(ui: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(ui));
}

beforeEach(() => {
  vi.clearAllMocks();
  gatewayEvents.clear();
  localStorage.clear();
  gatewayRequest.mockResolvedValue({});
  Object.defineProperty(window, "__HERMES_AUTH_REQUIRED__", {
    configurable: true,
    value: false,
    writable: true,
  });
});

describe("Project Ops route host contract", () => {
  it(
    "uses the bounded full-height App host only for the normalized route",
    async () => {
      const { getRouteHostClassNames } = await import("../App");
      const routeHost = getRouteHostClassNames("/project-ops");

      expect(routeHost.outer).toContain("min-h-0");
      expect(routeHost.outer).toContain("flex-1");
      expect(routeHost.outer).toContain("flex-col");
      expect(routeHost.outer).toContain("pt-2 sm:pt-4 lg:pt-6");
      expect(routeHost.inner).toContain("min-h-0 flex flex-1 flex-col");
      expect(routeHost.inner).toContain(
        "pb-[calc(2rem+env(safe-area-inset-bottom,0px))] lg:pb-8",
      );
      expect(getRouteHostClassNames("/project-ops/")).toEqual(routeHost);

      for (const path of [
        "/project-ops/topic",
        "/project-ops-extra",
        "/sessions",
      ]) {
        expect(getRouteHostClassNames(path).inner).not.toContain(
          "min-h-0 flex flex-1 flex-col",
        );
      }
    },
    15_000,
  );
});

describe("ProjectOpsPage operation contracts", () => {
  const project = {
    id: "p1",
    slug: "alpha",
    name: "Alpha",
    primary_path: "C:/alpha",
  };
  const task = {
    id: "t1",
    title: "Existing topic",
    status: "ready",
    project_id: "p1",
    session_id: "stored-existing",
  };

  function installCatalog() {
    fetchJSON.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/projects")) return { projects: [project] };
      if (url.endsWith("/boards")) {
        return { boards: [{ slug: "alpha-main", name: "Alpha", project_id: "p1" }] };
      }
      if (url.includes("/board?")) {
        return { latest_event_id: 1, columns: [{ name: "ready", tasks: [task] }] };
      }
      if (url.includes("/tasks/t1?")) {
        return { task, comments: [], runs: [], events: [] };
      }
      if (url.includes("/tasks?") && init?.method === "POST") {
        return { task: { ...task, id: "created-task", title: "Recovered topic" } };
      }
      throw new Error(`unexpected URL ${url}`);
    });
  }

  it("sends raw prompt text and passes the selected profile to resume", async () => {
    installCatalog();
    gatewayRequest.mockImplementation(async (method: string) => {
      if (method === "session.resume") {
        return { session_id: "runtime-1", session_key: "stored-existing", messages: [] };
      }
      return {};
    });
    const { ProfileContext } = await import("@/contexts/profile-context");
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");

    await render(
      <ProfileContext.Provider
        value={{ profile: "coder", currentProfile: "default", profiles: ["coder"], setProfile: vi.fn() }}
      >
        <ProjectOpsPage />
      </ProfileContext.Provider>,
    );
    await vi.waitFor(() =>
      expect(gatewayRequest).toHaveBeenCalledWith(
        "session.resume",
        expect.objectContaining({ session_id: "stored-existing", profile: "coder" }),
      ),
    );
    const textarea = container.querySelector('textarea[aria-label="Message"]') as HTMLTextAreaElement;
    await act(async () => {
      textarea.value = "[Forged|other] raw body";
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const form = textarea.closest("form")!;
    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

    await vi.waitFor(() =>
      expect(gatewayRequest).toHaveBeenCalledWith("prompt.submit", {
        session_id: "runtime-1",
        text: "[Forged|other] raw body",
      }),
    );
  });

  it("reconciles a reloaded pending create with the same durable operation", async () => {
    installCatalog();
    localStorage.setItem(
      "hermes.project-ops.pending-topic-create.v1:coder",
      JSON.stringify({
        version: 1,
        operationId: "operation-reload",
        projectId: "p1",
        boardSlug: "alpha-main",
        title: "Recovered topic",
        state: "failed",
        error: "ambiguous response",
      }),
    );
    gatewayRequest.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === "session.create") {
        expect(params.creation_key).toBe("project-ops:operation-reload");
        expect(params.profile).toBe("coder");
        return { session_id: "runtime-recovered", stored_session_id: "stored-recovered" };
      }
      if (method === "session.resume") {
        return { session_id: "runtime-1", session_key: "stored-existing", messages: [] };
      }
      return {};
    });
    const { ProfileContext } = await import("@/contexts/profile-context");
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");

    await render(
      <ProfileContext.Provider
        value={{ profile: "coder", currentProfile: "default", profiles: ["coder"], setProfile: vi.fn() }}
      >
        <ProjectOpsPage />
      </ProfileContext.Provider>,
    );

    await vi.waitFor(() => expect(gatewayRequest).toHaveBeenCalledWith(
      "session.create",
      expect.objectContaining({ creation_key: "project-ops:operation-reload" }),
    ));
    await vi.waitFor(() => {
      const createCall = fetchJSON.mock.calls.find(
        ([url, init]) => String(url).includes("/tasks?") && init?.method === "POST",
      );
      expect(createCall).toBeTruthy();
      const body = JSON.parse(String(createCall?.[1]?.body));
      expect(body).toMatchObject({
        idempotency_key: "project-ops:operation-reload",
        session_id: "stored-recovered",
      });
    });
    expect(localStorage.getItem("hermes.project-ops.pending-topic-create.v1:coder")).toBeNull();
  });

  it("does not let a stale board response overwrite the current board", async () => {
    let resolveOld!: (value: unknown) => void;
    const oldBoard = new Promise((resolve) => { resolveOld = resolve; });
    fetchJSON.mockImplementation(async (url: string) => {
      if (url.endsWith("/projects")) return { projects: [project] };
      if (url.endsWith("/boards")) {
        return {
          boards: [
            { slug: "old-board", project_id: "p1" },
            { slug: "new-board", project_id: "p1" },
          ],
        };
      }
      if (url.includes("board=old-board")) return oldBoard;
      if (url.includes("board=new-board")) {
        return {
          latest_event_id: 2,
          columns: [{ name: "ready", tasks: [{ ...task, id: "new-task", title: "New board topic" }] }],
        };
      }
      if (url.includes("/tasks/new-task?")) {
        return { task: { ...task, id: "new-task", title: "New board topic" }, comments: [], runs: [], events: [] };
      }
      throw new Error(`unexpected URL ${url}`);
    });
    gatewayRequest.mockImplementation(async (method: string) => {
      if (method === "session.resume") {
        return { session_id: "runtime-new", session_key: "stored-existing", messages: [] };
      }
      return {};
    });
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");
    await render(<ProjectOpsPage />);
    const select = await vi.waitFor(() => {
      const element = container.querySelector('select[aria-label="Board"]') as HTMLSelectElement;
      expect(element).not.toBeNull();
      return element;
    });

    await act(async () => {
      select.value = "new-board";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await vi.waitFor(() => expect(container.textContent).toContain("New board topic"));
    await act(async () => resolveOld({
      latest_event_id: 1,
      columns: [{ name: "ready", tasks: [{ ...task, id: "old-task", title: "Stale topic" }] }],
    }));

    expect(container.textContent).toContain("New board topic");
    expect(container.textContent).not.toContain("Stale topic");
  });

  it("unsubscribes a runtime attached by a stale topic open", async () => {
    let resolveFirst!: (value: unknown) => void;
    const firstResume = new Promise((resolve) => { resolveFirst = resolve; });
    const secondTask = { ...task, id: "t2", title: "Second topic", session_id: "stored-second" };
    fetchJSON.mockImplementation(async (url: string) => {
      if (url.endsWith("/projects")) return { projects: [project] };
      if (url.endsWith("/boards")) {
        return { boards: [{ slug: "alpha-main", project_id: "p1" }] };
      }
      if (url.includes("/board?")) {
        return { latest_event_id: 1, columns: [{ name: "ready", tasks: [task, secondTask] }] };
      }
      if (url.includes("/tasks/")) {
        return { task: url.includes("t2") ? secondTask : task, comments: [], runs: [], events: [] };
      }
      throw new Error(`unexpected URL ${url}`);
    });
    gatewayRequest.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === "session.resume" && params.session_id === "stored-existing") {
        return firstResume;
      }
      if (method === "session.resume" && params.session_id === "stored-second") {
        return { session_id: "runtime-second", session_key: "stored-second", messages: [] };
      }
      return {};
    });
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");
    await render(<ProjectOpsPage />);
    const second = await vi.waitFor(() => {
      const button = [...container.querySelectorAll("button")].find((item) =>
        item.textContent?.includes("Second topic"),
      );
      expect(button).toBeTruthy();
      return button!;
    });
    await act(async () => second.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await vi.waitFor(() => expect(gatewayRequest).toHaveBeenCalledWith(
      "session.subscribe",
      { session_id: "runtime-second" },
    ));

    await act(async () => resolveFirst({
      session_id: "runtime-first",
      session_key: "stored-existing",
      messages: [],
    }));
    await vi.waitFor(() => expect(gatewayRequest).toHaveBeenCalledWith(
      "session.unsubscribe",
      { session_id: "runtime-first" },
    ));
  });

  it("invalidates a pending topic open when selection becomes empty", async () => {
    let resolveResume!: (value: unknown) => void;
    const pendingResume = new Promise((resolve) => { resolveResume = resolve; });
    const secondProject = {
      id: "p2",
      slug: "beta",
      name: "Beta",
      primary_path: "C:/beta",
    };
    fetchJSON.mockImplementation(async (url: string) => {
      if (url.endsWith("/projects")) return { projects: [project, secondProject] };
      if (url.endsWith("/boards")) {
        return { boards: [{ slug: "alpha-main", name: "Alpha", project_id: "p1" }] };
      }
      if (url.includes("/board?")) {
        return { latest_event_id: 1, columns: [{ name: "ready", tasks: [task] }] };
      }
      if (url.includes("/tasks/t1?")) {
        return { task, comments: [], runs: [], events: [] };
      }
      throw new Error(`unexpected URL ${url}`);
    });
    gatewayRequest.mockImplementation(async (method: string) => {
      if (method === "session.resume") return pendingResume;
      return {};
    });
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");
    await render(<ProjectOpsPage />);
    const beta = await vi.waitFor(() => {
      const button = [...container.querySelectorAll("button")].find(
        (item) => item.textContent?.trim() === "Beta",
      );
      expect(button).toBeTruthy();
      return button!;
    });

    await act(async () => beta.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(container.textContent).toContain("Choose a topic");
    await act(async () => resolveResume({
      session_id: "runtime-late",
      session_key: "stored-existing",
      messages: [],
    }));

    await vi.waitFor(() => expect(gatewayRequest).toHaveBeenCalledWith(
      "session.unsubscribe",
      { session_id: "runtime-late" },
    ));
  });

  it("compensates failed task creation and retries the same durable operation", async () => {
    let taskAttempts = 0;
    installCatalog();
    fetchJSON.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/projects")) return { projects: [project] };
      if (url.endsWith("/boards")) {
        return { boards: [{ slug: "alpha-main", name: "Alpha", project_id: "p1" }] };
      }
      if (url.includes("/board?")) {
        return { latest_event_id: 1, columns: [{ name: "ready", tasks: [task] }] };
      }
      if (url.includes("/tasks/t1?")) {
        return { task, comments: [], runs: [], events: [] };
      }
      if (url.includes("/tasks?") && init?.method === "POST") {
        taskAttempts += 1;
        if (taskAttempts === 1) throw new Error("task create failed");
        return { task: { ...task, id: "created-task", title: "Retry topic" } };
      }
      throw new Error(`unexpected URL ${url}`);
    });
    let createdRuntime = 0;
    gatewayRequest.mockImplementation(async (method: string) => {
      if (method === "session.resume") {
        return { session_id: "runtime-existing", session_key: "stored-existing", messages: [] };
      }
      if (method === "session.create") {
        createdRuntime += 1;
        return {
          session_id: `runtime-created-${createdRuntime}`,
          stored_session_id: "stored-created",
        };
      }
      return {};
    });
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");
    await render(<ProjectOpsPage />);
    const input = await vi.waitFor(() => {
      const element = container.querySelector('input[aria-label="New topic title"]') as HTMLInputElement;
      expect(element).not.toBeNull();
      return element;
    });
    await act(async () => {
      Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )?.set?.call(input, "Retry topic");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const form = input.closest("form")!;

    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    await vi.waitFor(() => expect(gatewayRequest).toHaveBeenCalledWith(
      "session.close",
      { session_id: "runtime-created-1", reason: "orphaned_create" },
    ));
    const storageKey = "hermes.project-ops.pending-topic-create.v1:current";
    const failed = JSON.parse(String(localStorage.getItem(storageKey)));
    expect(failed.operationId).toBeTruthy();
    expect(failed.runtimeSessionId).toBeUndefined();
    expect(failed.storedSessionId).toBeUndefined();

    await act(async () => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    await vi.waitFor(() => expect(taskAttempts).toBe(2));
    const createCalls = gatewayRequest.mock.calls.filter(([method]) => method === "session.create");
    expect(createCalls).toHaveLength(2);
    expect(createCalls[0][1].creation_key).toBe(createCalls[1][1].creation_key);
    expect(createCalls[0][1].creation_key).toBe(`project-ops:${failed.operationId}`);
    expect(localStorage.getItem(storageKey)).toBeNull();
  });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
});

describe("ProjectOpsPage states", () => {
  it("renders an explicit empty-project state", async () => {
    fetchJSON.mockImplementation(async (url: string) => {
      if (url.endsWith("/projects")) return { projects: [] };
      if (url.endsWith("/boards")) return { boards: [] };
      throw new Error(`unexpected URL ${url}`);
    });
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");

    await render(<ProjectOpsPage />);
    await vi.waitFor(() => expect(container.textContent).toContain("No projects yet"));

    expect(container.textContent).toContain("Create a Hermes project");
  });

  it("surfaces API failures instead of rendering placeholder data", async () => {
    fetchJSON.mockRejectedValue(new Error("kanban offline"));
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");

    await render(<ProjectOpsPage />);
    await vi.waitFor(() => expect(container.textContent).toContain("Project Ops unavailable"));

    expect(container.textContent).toContain("kanban offline");
    expect(container.textContent).toContain("No projects yet");
  });

  it("exposes progressive mobile views while retaining the desktop panels", async () => {
    fetchJSON.mockImplementation(async (url: string) => {
      if (url.endsWith("/projects")) {
        return {
          projects: [
            { id: "p1", slug: "alpha", name: "Alpha", primary_path: "C:/alpha" },
          ],
        };
      }
      if (url.endsWith("/boards")) return { boards: [] };
      throw new Error(`unexpected URL ${url}`);
    });
    const { default: ProjectOpsPage } = await import("./ProjectOpsPage");

    await render(<ProjectOpsPage />);
    await vi.waitFor(() => expect(container.querySelector('[data-testid="project-ops-page"]')).not.toBeNull());

    expect(container.querySelector('[data-testid="topics-panel"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="chat-panel"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="board-panel"]')).not.toBeNull();
    expect(container.textContent).toContain("Topics");
    expect(container.textContent).toContain("Chat");
    expect(container.textContent).toContain("Board");
    expect(container.firstElementChild?.className).toContain("overflow-hidden");

    const mobileNav = container.querySelector('nav[aria-label="Project Ops views"]');
    const panelGrid = mobileNav?.previousElementSibling;
    expect(panelGrid?.classList).toContain("grid-rows-[minmax(0,1fr)]");
    expect(panelGrid?.classList).toContain("overflow-hidden");
    expect(mobileNav?.classList).toContain("shrink-0");
    expect(panelGrid?.nextElementSibling).toBe(mobileNav);
  });
});
