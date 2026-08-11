package com.nousresearch.hermes.projectops

import com.nousresearch.hermes.data.AuthMode
import com.nousresearch.hermes.data.BackendConfig
import com.nousresearch.hermes.data.SessionCredentialStore
import com.nousresearch.hermes.network.HermesRestClient
import java.io.IOException
import javax.inject.Inject
import javax.inject.Singleton

interface ProjectOpsDataSource {
    suspend fun projects(config: BackendConfig, profileId: String): ProjectOpsProjectsResponse
    suspend fun boards(config: BackendConfig, profileId: String): ProjectOpsBoardsResponse
    suspend fun board(config: BackendConfig, profileId: String, boardSlug: String): ProjectOpsBoardResponse
    suspend fun task(config: BackendConfig, profileId: String, boardSlug: String, taskId: String): ProjectOpsTaskDetailResponse
}

@Singleton
class ProjectOpsRepository @Inject constructor(
    private val rest: HermesRestClient,
    private val credentials: SessionCredentialStore,
) : ProjectOpsDataSource {
    override suspend fun projects(config: BackendConfig, profileId: String): ProjectOpsProjectsResponse =
        rest.projectOpsProjects(config, credential(config), profileId)

    override suspend fun boards(config: BackendConfig, profileId: String): ProjectOpsBoardsResponse =
        rest.projectOpsBoards(config, credential(config), profileId)

    override suspend fun board(config: BackendConfig, profileId: String, boardSlug: String): ProjectOpsBoardResponse =
        rest.projectOpsBoard(config, credential(config), profileId, boardSlug)

    override suspend fun task(
        config: BackendConfig,
        profileId: String,
        boardSlug: String,
        taskId: String,
    ): ProjectOpsTaskDetailResponse = rest.projectOpsTask(config, credential(config), profileId, boardSlug, taskId)

    private fun credential(config: BackendConfig): String {
        if (config.authMode != AuthMode.DASHBOARD_SESSION) {
            throw IOException("Reconnect ${config.label} with Dashboard authentication before opening Project Ops")
        }
        return credentials.get(config.id)?.headerValue
            ?: throw IOException("Reconnect ${config.label} before opening Project Ops")
    }
}
