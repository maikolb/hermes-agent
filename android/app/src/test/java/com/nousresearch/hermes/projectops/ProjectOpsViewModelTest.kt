package com.nousresearch.hermes.projectops

import com.nousresearch.hermes.data.AuthMode
import com.nousresearch.hermes.data.BackendConfig
import com.nousresearch.hermes.ui.navigation.ProjectOpsPane
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withContext
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ProjectOpsViewModelTest {
    private val dispatcher = UnconfinedTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `project board and task reconciliation stays server scoped`() = runTest(dispatcher) {
        val repository = StaticProjectOpsDataSource()
        val viewModel = ProjectOpsViewModel(repository)
        val profileId = "research +&/"

        viewModel.bind(config("backend"), profileId, "missing", "wrong", "other-project-task", ProjectOpsPane.BOARD)
        advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals("project-a", state.selectedProjectId)
        assertEquals("board-a", state.selectedBoardSlug)
        assertNull(state.selectedTaskId)
        assertEquals(listOf("task-a"), state.tasks.map(ProjectOpsTask::id))
        assertEquals(ProjectOpsPane.BOARD, state.selectedPane)

        viewModel.selectProject("project-b")
        advanceUntilIdle()
        assertEquals("board-b", viewModel.state.value.selectedBoardSlug)
        assertEquals(listOf("task-b"), viewModel.state.value.tasks.map(ProjectOpsTask::id))
        assertEquals(setOf(profileId), repository.requestedProfiles.toSet())
    }

    @Test
    fun `late response from cancelled backend cannot replace current state`() = runTest(dispatcher) {
        val oldProjects = CompletableDeferred<ProjectOpsProjectsResponse>()
        val repository = object : StaticProjectOpsDataSource() {
            override suspend fun projects(config: BackendConfig, profileId: String): ProjectOpsProjectsResponse =
                if (config.id == "old") withContext(NonCancellable) { oldProjects.await() }
                else ProjectOpsProjectsResponse(listOf(ProjectOpsProject("new-project", name = "New")))

            override suspend fun boards(config: BackendConfig, profileId: String): ProjectOpsBoardsResponse =
                if (config.id == "new") {
                    ProjectOpsBoardsResponse(listOf(ProjectOpsBoard("new-board", "New board", "new-project")), "new-board")
                } else {
                    super.boards(config, profileId)
                }

            override suspend fun board(config: BackendConfig, profileId: String, boardSlug: String): ProjectOpsBoardResponse =
                ProjectOpsBoardResponse(emptyList(), 0)
        }
        val viewModel = ProjectOpsViewModel(repository)

        viewModel.bind(config("old"), "default", null, null, null, null)
        viewModel.bind(config("new"), "default", null, null, null, null)
        advanceUntilIdle()
        assertEquals("new-project", viewModel.state.value.selectedProjectId)

        oldProjects.complete(ProjectOpsProjectsResponse(listOf(ProjectOpsProject("old-project", name = "Old"))))
        advanceUntilIdle()

        assertEquals("new", viewModel.state.value.backendId)
        assertEquals("new-project", viewModel.state.value.selectedProjectId)
    }

    private fun config(id: String) = BackendConfig(
        id = id,
        label = id,
        baseUrl = "https://hermes.example",
        authMode = AuthMode.DASHBOARD_SESSION,
    )
}

private open class StaticProjectOpsDataSource : ProjectOpsDataSource {
    val requestedProfiles = mutableListOf<String>()

    override suspend fun projects(config: BackendConfig, profileId: String): ProjectOpsProjectsResponse {
        requestedProfiles += profileId
        return ProjectOpsProjectsResponse(
            listOf(ProjectOpsProject("project-a", name = "A"), ProjectOpsProject("project-b", name = "B")),
        )
    }

    override suspend fun boards(config: BackendConfig, profileId: String): ProjectOpsBoardsResponse {
        requestedProfiles += profileId
        return ProjectOpsBoardsResponse(
            boards = listOf(
                ProjectOpsBoard("board-a", "A board", "project-a"),
                ProjectOpsBoard("board-b", "B board", "project-b"),
            ),
            current = "board-a",
        )
    }

    override suspend fun board(config: BackendConfig, profileId: String, boardSlug: String): ProjectOpsBoardResponse {
        requestedProfiles += profileId
        return ProjectOpsBoardResponse(
            columns = listOf(
                ProjectOpsColumn(
                    "ready",
                    listOf(
                        ProjectOpsTask("task-a", "A", "ready", "project-a", "session-a"),
                        ProjectOpsTask("task-b", "B", "ready", "project-b", "session-b"),
                        ProjectOpsTask("other-project-task", "Other", "ready", "other", "session-other"),
                    ),
                ),
            ),
            latestEventId = 1,
        )
    }

    override suspend fun task(
        config: BackendConfig,
        profileId: String,
        boardSlug: String,
        taskId: String,
    ): ProjectOpsTaskDetailResponse {
        requestedProfiles += profileId
        return ProjectOpsTaskDetailResponse(
            ProjectOpsTask(taskId, taskId, "ready", if (taskId == "task-a") "project-a" else "project-b"),
        )
    }
}
