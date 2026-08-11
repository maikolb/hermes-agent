package com.nousresearch.hermes.projectops

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

@Serializable
data class ProjectOpsProjectsResponse(
    val projects: List<ProjectOpsProject> = emptyList(),
)

@Serializable
data class ProjectOpsProject(
    val id: String,
    val slug: String = "",
    val name: String,
    @SerialName("primary_path") val primaryPath: String = "",
    val icon: String = "",
    val color: String = "",
)

@Serializable
data class ProjectOpsBoardsResponse(
    val boards: List<ProjectOpsBoard> = emptyList(),
    val current: String? = null,
)

@Serializable
data class ProjectOpsBoard(
    val slug: String,
    val name: String,
    @SerialName("project_id") val projectId: String? = null,
    val description: String = "",
    val icon: String = "",
    val color: String = "",
    @SerialName("default_workdir") val defaultWorkdir: String? = null,
    @SerialName("created_at") val createdAt: Long? = null,
    val archived: Boolean = false,
    @SerialName("is_current") val isCurrent: Boolean = false,
    val counts: Map<String, Int> = emptyMap(),
    val total: Int = counts.values.sum(),
    @SerialName("project_name") val projectName: String? = null,
    @SerialName("default_workspace_kind") val defaultWorkspaceKind: String? = null,
)

@Serializable
data class ProjectOpsBoardResponse(
    val columns: List<ProjectOpsColumn> = emptyList(),
    @SerialName("latest_event_id") val latestEventId: Long,
)

@Serializable
data class ProjectOpsColumn(
    val name: String,
    val tasks: List<ProjectOpsTask> = emptyList(),
)

@Serializable
data class ProjectOpsTask(
    val id: String,
    val title: String,
    val status: String,
    @SerialName("project_id") val projectId: String? = null,
    @SerialName("session_id") val sessionId: String? = null,
    val body: String? = null,
    val assignee: String? = null,
    val priority: Int = 0,
    @SerialName("created_by") val createdBy: String? = null,
    @SerialName("created_at") val createdAt: Long = 0,
    @SerialName("started_at") val startedAt: Long? = null,
    @SerialName("completed_at") val completedAt: Long? = null,
    val tenant: String? = null,
    val result: String? = null,
    @SerialName("latest_summary") val latestSummary: String? = null,
    val diagnostics: List<ProjectOpsDiagnostic> = emptyList(),
)

@Serializable
data class ProjectOpsTaskDetailResponse(
    val task: ProjectOpsTask,
    val comments: List<ProjectOpsComment> = emptyList(),
    val runs: List<ProjectOpsRun> = emptyList(),
    val events: List<ProjectOpsEvent> = emptyList(),
)

@Serializable
data class ProjectOpsComment(
    val id: Long,
    @SerialName("task_id") val taskId: String,
    val author: String,
    val body: String,
    @SerialName("created_at") val createdAt: Long,
)

@Serializable
data class ProjectOpsRun(
    val id: Long,
    @SerialName("task_id") val taskId: String,
    val profile: String? = null,
    @SerialName("step_key") val stepKey: String? = null,
    val status: String? = null,
    @SerialName("started_at") val startedAt: Long? = null,
    @SerialName("ended_at") val endedAt: Long? = null,
    val outcome: String? = null,
    val summary: String? = null,
    val error: String? = null,
    val metadata: JsonElement? = null,
)

@Serializable
data class ProjectOpsEvent(
    val id: Long,
    @SerialName("task_id") val taskId: String,
    val kind: String,
    val payload: JsonElement? = null,
    @SerialName("created_at") val createdAt: Long,
    @SerialName("run_id") val runId: Long? = null,
)

@Serializable
data class ProjectOpsDiagnostic(
    val kind: String,
    val severity: String,
    val title: String,
    val detail: String,
    @SerialName("first_seen_at") val firstSeenAt: Long = 0,
    @SerialName("last_seen_at") val lastSeenAt: Long = 0,
    val count: Int = 1,
    @SerialName("run_id") val runId: Long? = null,
)
