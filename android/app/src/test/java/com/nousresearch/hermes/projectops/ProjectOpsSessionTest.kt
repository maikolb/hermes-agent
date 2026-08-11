package com.nousresearch.hermes.projectops

import com.nousresearch.hermes.protocol.StoredSession
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

class ProjectOpsSessionTest {
    @Test
    fun `matching server session is selected without rewriting identity or metadata`() {
        val serverSession = StoredSession(
            sessionId = "server-session",
            profile = "work",
            source = "gateway",
            title = "Server title",
            messageCount = 12,
        )

        val selected = projectOpsStoredSession(
            task = task(sessionId = "server-session"),
            sessions = listOf(serverSession),
            profileId = "work",
        )

        assertSame(serverSession, selected)
    }

    @Test
    fun `blank task session id cannot create a chat identity`() {
        assertNull(
            projectOpsStoredSession(
                task = task(sessionId = "  "),
                sessions = emptyList(),
                profileId = "work",
            ),
        )
    }

    @Test
    fun `server task session id is preserved exactly when session list is not yet populated`() {
        val selected = projectOpsStoredSession(
            task = task(sessionId = "server-owned-id"),
            sessions = emptyList(),
            profileId = "work",
        )

        assertEquals("server-owned-id", selected?.durableId)
        assertEquals("project_ops", selected?.source)
        assertEquals("work", selected?.profile)
    }

    private fun task(sessionId: String?) = ProjectOpsTask(
        id = "task-1",
        title = "Task title",
        status = "todo",
        sessionId = sessionId,
    )
}
