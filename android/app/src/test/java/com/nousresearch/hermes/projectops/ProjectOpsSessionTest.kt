package com.nousresearch.hermes.projectops

import com.nousresearch.hermes.data.SessionRestorationState
import com.nousresearch.hermes.data.SessionRestorationStatus
import com.nousresearch.hermes.protocol.StoredSession
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
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
    fun `blank catalog source receives Project Ops fallback without losing server metadata`() {
        val serverSession = StoredSession(
            sessionId = "server-session",
            profile = "work",
            source = null,
            title = "Server title",
            messageCount = 12,
        )

        val selected = projectOpsStoredSession(
            task = task(sessionId = "server-session"),
            sessions = listOf(serverSession),
            profileId = "work",
        )

        assertEquals("server-session", selected?.durableId)
        assertEquals("project_ops", selected?.source)
        assertEquals("Server title", selected?.title)
        assertEquals(12, selected?.messageCount)
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

    @Test
    fun `Project Ops navigation waits for its resume and opens returned durable child`() {
        val oldReady = SessionRestorationState(
            status = SessionRestorationStatus.READY,
            session = StoredSession(sessionId = "unrelated"),
        )
        assertTrue(projectOpsChatNavigation("parent-session", "request-2", oldReady) is ProjectOpsChatNavigation.Waiting)

        val rehydrating = SessionRestorationState(
            status = SessionRestorationStatus.REHYDRATING,
            requestedSessionId = "parent-session",
            requestToken = "request-2",
        )
        assertTrue(projectOpsChatNavigation("parent-session", "request-2", rehydrating) is ProjectOpsChatNavigation.Waiting)

        val child = StoredSession(sessionId = "child-session", profile = "work", source = "project_ops")
        val ready = SessionRestorationState(
            status = SessionRestorationStatus.READY,
            requestedSessionId = "parent-session",
            requestToken = "request-2",
            session = child,
        )
        val decision = projectOpsChatNavigation("parent-session", "request-2", ready)

        assertEquals(child, (decision as ProjectOpsChatNavigation.Open).session)

        val stalePreviousResume = SessionRestorationState(
            status = SessionRestorationStatus.READY,
            requestedSessionId = "parent-session",
            requestToken = "request-1",
            session = StoredSession(sessionId = "previous-child"),
        )
        assertTrue(
            projectOpsChatNavigation("parent-session", "request-2", stalePreviousResume) is ProjectOpsChatNavigation.Waiting,
        )

        val failed = SessionRestorationState(
            status = SessionRestorationStatus.AUTHENTICATION_REQUIRED,
            requestedSessionId = "parent-session",
            requestToken = "request-2",
        )
        assertTrue(projectOpsChatNavigation("parent-session", "request-2", failed) is ProjectOpsChatNavigation.Cancelled)
    }

    private fun task(sessionId: String?) = ProjectOpsTask(
        id = "task-1",
        title = "Task title",
        status = "todo",
        sessionId = sessionId,
    )
}
