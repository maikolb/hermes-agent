package com.nousresearch.hermes.projectops

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.nousresearch.hermes.ui.navigation.ProjectOpsPane
import com.nousresearch.hermes.ui.theme.HermesTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ProjectOpsScreenTest {
    @get:Rule
    val compose = createComposeRule()

    @Test
    fun phoneSwitcherExposesOnePaneAndDisablesChatWithoutSession() {
        var selectedPane: ProjectOpsPane? = null
        compose.setContent {
            HermesTheme {
                ProjectOpsScreen(
                    state = populatedState(task = task(sessionId = null)),
                    expanded = false,
                    onBack = {},
                    onRetry = {},
                    onSelectProject = {},
                    onSelectBoard = {},
                    onSelectTask = {},
                    onSelectPane = { selectedPane = it },
                    onOpenChat = {},
                )
            }
        }

        compose.onNodeWithText("Topics").assertIsDisplayed()
        compose.onNodeWithText("Chat").assertIsNotEnabled()
        compose.onNodeWithText("Board").performClick()
        compose.onNodeWithContentDescription("Phone Project Ops pane: topics").assertIsDisplayed()
        compose.runOnIdle { assertEquals(ProjectOpsPane.BOARD, selectedPane) }
    }

    @Test
    fun drawerDetailShowsServerEvidenceAndUsesChatCallback() {
        var opened: ProjectOpsTask? = null
        val task = task(sessionId = "server-session")
        val detail = ProjectOpsTaskDetailResponse(
            task = task.copy(
                diagnostics = listOf(ProjectOpsDiagnostic("stuck", "warning", "Stuck", "No heartbeat")),
            ),
            comments = listOf(ProjectOpsComment(1, task.id, "alex", "Check logs", 1)),
            runs = listOf(ProjectOpsRun(2, task.id, profile = "worker", status = "failed", error = "boom")),
            events = listOf(ProjectOpsEvent(3, task.id, "blocked", createdAt = 2)),
        )
        compose.setContent {
            HermesTheme {
                ProjectOpsScreen(
                    state = populatedState(task).copy(selectedTaskId = task.id, detail = detail),
                    expanded = false,
                    onBack = {},
                    onRetry = {},
                    onSelectProject = {},
                    onSelectBoard = {},
                    onSelectTask = {},
                    onSelectPane = {},
                    onOpenChat = { opened = it },
                )
            }
        }

        listOf("Status: ready", "Assignee: worker", "Session: server-session", "COMMENTS", "RUNS", "EVENTS", "DIAGNOSTICS")
            .forEach { compose.onNodeWithText(it).assertIsDisplayed() }
        compose.onNodeWithText("Open existing chat").performClick()
        compose.runOnIdle {
            assertEquals("server-session", opened?.sessionId)
            assertTrue(opened === task)
        }
    }

    @Test
    fun loadingEmptyAndErrorStatesAreExplicitAndRetryable() {
        compose.setContent {
            HermesTheme {
                ProjectOpsScreen(
                    state = ProjectOpsUiState(loading = true),
                    expanded = false,
                    onBack = {}, onRetry = {}, onSelectProject = {}, onSelectBoard = {},
                    onSelectTask = {}, onSelectPane = {}, onOpenChat = {},
                )
            }
        }
        compose.onNodeWithText("Loading projects and boards").assertIsDisplayed()

        var retried = false
        compose.setContent {
            HermesTheme {
                ProjectOpsScreen(
                    state = ProjectOpsUiState(error = "Authenticated request failed"),
                    expanded = false,
                    onBack = {}, onRetry = { retried = true }, onSelectProject = {}, onSelectBoard = {},
                    onSelectTask = {}, onSelectPane = {}, onOpenChat = {},
                )
            }
        }
        compose.onNodeWithText("Authenticated request failed").assertIsDisplayed()
        compose.onNodeWithText("Retry").performClick()
        compose.runOnIdle { assertTrue(retried) }

        compose.setContent {
            HermesTheme {
                ProjectOpsScreen(
                    state = ProjectOpsUiState(),
                    expanded = false,
                    onBack = {}, onRetry = {}, onSelectProject = {}, onSelectBoard = {},
                    onSelectTask = {}, onSelectPane = {}, onOpenChat = {},
                )
            }
        }
        compose.onNodeWithText("NO PROJECTS").assertIsDisplayed()
    }

    private fun populatedState(task: ProjectOpsTask) = ProjectOpsUiState(
        projects = listOf(ProjectOpsProject("project", name = "Project")),
        boards = listOf(ProjectOpsBoard("board", "Board", "project")),
        columns = listOf(ProjectOpsColumn("ready", listOf(task))),
        selectedProjectId = "project",
        selectedBoardSlug = "board",
        selectedPane = ProjectOpsPane.TOPICS,
    )

    private fun task(sessionId: String?) = ProjectOpsTask(
        id = "task",
        title = "Topic",
        status = "ready",
        projectId = "project",
        sessionId = sessionId,
        assignee = "worker",
    )
}
