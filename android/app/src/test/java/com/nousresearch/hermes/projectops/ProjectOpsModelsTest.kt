package com.nousresearch.hermes.projectops

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ProjectOpsModelsTest {
    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun `literal route wrappers tolerate future fields`() {
        val projects = json.decodeFromString<ProjectOpsProjectsResponse>(
            """{"projects":[{"id":"p-1","slug":"ops","name":"Ops","future":true}],"next":"cursor"}""",
        )
        val boards = json.decodeFromString<ProjectOpsBoardsResponse>(
            """{"boards":[{"slug":"board one","name":"Board","project_id":"p-1","counts":{"ready":2},"future":1}],"current":"board one","future":{}}""",
        )
        val board = json.decodeFromString<ProjectOpsBoardResponse>(
            """{"columns":[{"name":"ready","tasks":[{"id":"task/1","title":"Topic","status":"ready","project_id":"p-1","session_id":"session-1","future":"ok"}]}],"latest_event_id":42,"future":[]}""",
        )
        val detail = json.decodeFromString<ProjectOpsTaskDetailResponse>(
            """{"task":{"id":"task/1","title":"Topic","status":"ready","project_id":"p-1","session_id":"session-1"},"comments":[],"runs":[],"events":[],"attachments":[{"future":true}],"future":true}""",
        )

        assertEquals("p-1", projects.projects.single().id)
        assertEquals("board one", boards.current)
        assertEquals(2, boards.boards.single().counts.getValue("ready"))
        assertEquals(42L, board.latestEventId)
        assertEquals("session-1", board.columns.single().tasks.single().sessionId)
        assertEquals("task/1", detail.task.id)
    }

    @Test
    fun `detail preserves comments runs events and diagnostics`() {
        val detail = json.decodeFromString<ProjectOpsTaskDetailResponse>(
            """{
              "task":{"id":"t","title":"Topic","status":"blocked","project_id":"p","diagnostics":[{"kind":"stuck","severity":"warning","title":"Stuck","detail":"No heartbeat"}]},
              "comments":[{"id":1,"task_id":"t","author":"alex","body":"Check logs","created_at":10}],
              "runs":[{"id":2,"task_id":"t","profile":"worker","status":"failed","error":"boom"}],
              "events":[{"id":3,"task_id":"t","kind":"blocked","payload":{"reason":"input"},"created_at":11}]
            }""",
        )

        assertEquals("alex", detail.comments.single().author)
        assertEquals("failed", detail.runs.single().status)
        assertEquals("blocked", detail.events.single().kind)
        assertTrue(detail.task.diagnostics.single().detail.contains("heartbeat"))
    }
}
