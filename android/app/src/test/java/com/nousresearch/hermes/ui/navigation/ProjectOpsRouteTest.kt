package com.nousresearch.hermes.ui.navigation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ProjectOpsRouteTest {
    @Test
    fun `typed Project Ops route round trips derivative scheme`() {
        val route = HermesDestinationRoute.ProjectOps(
            backendId = "backend one",
            profileId = "research",
            projectId = "project/1",
            boardSlug = "ops board",
            taskId = "task/1",
            pane = ProjectOpsPane.BOARD,
        )

        val encoded = HermesDestinationUri.encode(route)

        assertTrue(encoded.startsWith("hermes-project-ops://project-ops?"))
        assertEquals(route, HermesDestinationUri.parse(encoded))
        assertEquals(ProductHome.PROJECT_OPS, HermesDestinationCatalog.productHome(route))
    }

    @Test
    fun `external Project Ops ids are stripped until server reconciliation`() {
        val result = resolveEntryDestination(
            route = HermesDestinationRoute.ProjectOps("backend", "default", "project", "board", "task", ProjectOpsPane.CHAT),
            availableBackendIds = setOf("backend"),
            authenticatedBackendId = "backend",
            authoritativeSessions = emptySet(),
            authoritativeProfileIds = setOf("default"),
            fallbackProfileId = "default",
            authoritativeAutomationResources = emptySet(),
        )

        assertEquals(
            HermesDestinationRoute.ProjectOps("backend", "default", pane = ProjectOpsPane.CHAT),
            result.route,
        )
        assertFalse(result.mutationsEnabled)
        assertTrue(result.explanation.orEmpty().contains("reconciliation"))
    }

    @Test
    fun `Project Ops keeps existing backend authentication gate`() {
        val result = resolveRestoredRoute(
            route = HermesDestinationRoute.ProjectOps("intended", "research"),
            availableBackendIds = setOf("intended"),
            authenticatedBackendId = "other",
            authoritativeSessions = emptySet(),
        )

        assertEquals(HermesRoute.BackendPicker("intended", "research"), result.route)
        assertFalse(result.mutationsEnabled)
    }
}
