package com.nousresearch.hermes.projectops

import com.nousresearch.hermes.data.AuthMode
import com.nousresearch.hermes.data.BackendConfig
import com.nousresearch.hermes.network.HermesRestClient
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ProjectOpsRepositoryTest {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    @Test
    fun `exact endpoints encode board and task ids and inherit dashboard cookie`() = runTest {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"projects":[]}"""))
            server.enqueue(MockResponse().setBody("""{"boards":[],"current":"ops / one"}"""))
            server.enqueue(MockResponse().setBody("""{"columns":[],"latest_event_id":0}"""))
            server.enqueue(
                MockResponse().setBody(
                    """{"task":{"id":"task / one","title":"Topic","status":"ready"},"comments":[],"runs":[],"events":[]}""",
                ),
            )
            val client = HermesRestClient(OkHttpClient(), json)
            val config = dashboardConfig(server)

            val profileId = "profile +&/ one"
            client.projectOpsProjects(config, "hermes_session_at=cookie", profileId)
            client.projectOpsBoards(config, "hermes_session_at=cookie", profileId)
            client.projectOpsBoard(config, "hermes_session_at=cookie", profileId, "ops +&/ one")
            client.projectOpsTask(config, "hermes_session_at=cookie", profileId, "ops +&/ one", "task +&/ one")

            val requests = List(4) { checkNotNull(server.takeRequest()) }
            assertEquals("/api/plugins/kanban/projects", requests[0].requestUrl?.encodedPath)
            assertEquals(profileId, requests[0].requestUrl?.queryParameter("profile"))
            assertEquals("/api/plugins/kanban/boards", requests[1].requestUrl?.encodedPath)
            assertEquals(profileId, requests[1].requestUrl?.queryParameter("profile"))
            assertEquals("/api/plugins/kanban/board", requests[2].requestUrl?.encodedPath)
            assertEquals("ops +&/ one", requests[2].requestUrl?.queryParameter("board"))
            assertEquals(profileId, requests[2].requestUrl?.queryParameter("profile"))
            assertEquals("/api/plugins/kanban/tasks/task%20+&%2F%20one", requests[3].requestUrl?.encodedPath)
            assertEquals("ops +&/ one", requests[3].requestUrl?.queryParameter("board"))
            assertEquals(profileId, requests[3].requestUrl?.queryParameter("profile"))
            requests.forEach { request ->
                assertEquals("hermes_session_at=cookie", request.getHeader("Cookie"))
                assertNull(request.getHeader("Authorization"))
            }
        }
    }

    @Test
    fun `rest helper preserves inherited bearer mode without parallel auth`() = runTest {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"projects":[]}"""))
            val client = HermesRestClient(OkHttpClient(), json)
            val config = dashboardConfig(server).copy(authMode = AuthMode.TOKEN)

            client.projectOpsProjects(config, "legacy-token", "default")

            val request = checkNotNull(server.takeRequest())
            assertEquals("Bearer legacy-token", request.getHeader("Authorization"))
            assertNull(request.getHeader("Cookie"))
        }
    }

    private fun dashboardConfig(server: MockWebServer) = BackendConfig(
        id = "backend",
        label = "Test",
        baseUrl = server.url("/").toString(),
        authMode = AuthMode.DASHBOARD_SESSION,
        allowInsecurePrivateNetwork = true,
    )
}
