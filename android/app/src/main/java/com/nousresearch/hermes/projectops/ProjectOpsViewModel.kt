package com.nousresearch.hermes.projectops

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.nousresearch.hermes.data.BackendConfig
import com.nousresearch.hermes.ui.navigation.ProjectOpsPane
import dagger.hilt.android.lifecycle.HiltViewModel
import java.util.concurrent.atomic.AtomicLong
import javax.inject.Inject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class ProjectOpsUiState(
    val backendId: String? = null,
    val profileId: String? = null,
    val projects: List<ProjectOpsProject> = emptyList(),
    val boards: List<ProjectOpsBoard> = emptyList(),
    val columns: List<ProjectOpsColumn> = emptyList(),
    val selectedProjectId: String? = null,
    val selectedBoardSlug: String? = null,
    val selectedTaskId: String? = null,
    val selectedPane: ProjectOpsPane = ProjectOpsPane.TOPICS,
    val detail: ProjectOpsTaskDetailResponse? = null,
    val loading: Boolean = false,
    val boardLoading: Boolean = false,
    val detailLoading: Boolean = false,
    val error: String? = null,
) {
    val projectBoards: List<ProjectOpsBoard>
        get() = boardsForProject(boards, selectedProjectId)

    val tasks: List<ProjectOpsTask>
        get() = tasksForProject(columns, selectedProjectId)

    val selectedTask: ProjectOpsTask?
        get() = tasks.firstOrNull { it.id == selectedTaskId }
}

@HiltViewModel
class ProjectOpsViewModel @Inject constructor(
    private val repository: ProjectOpsDataSource,
) : ViewModel() {
    private val mutableState = MutableStateFlow(ProjectOpsUiState())
    val state = mutableState.asStateFlow()

    private val generation = AtomicLong()
    private var binding: Binding? = null
    private var loadJob: Job? = null
    private var boardJob: Job? = null
    private var detailJob: Job? = null

    fun bind(
        config: BackendConfig,
        profileId: String,
        projectId: String?,
        boardSlug: String?,
        taskId: String?,
        pane: ProjectOpsPane?,
    ) {
        val next = Binding(config, profileId, projectId, boardSlug, taskId, pane ?: ProjectOpsPane.TOPICS)
        if (binding == next && (mutableState.value.loading || mutableState.value.projects.isNotEmpty())) return
        binding = next
        reload()
    }

    fun retry() = reload()

    fun selectProject(projectId: String) {
        val current = mutableState.value
        if (current.projects.none { it.id == projectId } || current.selectedProjectId == projectId) return
        val currentBinding = binding ?: return
        val requestGeneration = nextGeneration()
        val projectBoards = boardsForProject(current.boards, projectId)
        val boardSlug = reconcileBoardSlug(projectBoards, null, current.boards.firstOrNull(ProjectOpsBoard::isCurrent)?.slug)
        binding = currentBinding.copy(projectId = projectId, boardSlug = boardSlug, taskId = null, pane = ProjectOpsPane.TOPICS)
        mutableState.update {
            it.copy(
                selectedProjectId = projectId,
                selectedBoardSlug = boardSlug,
                selectedTaskId = null,
                selectedPane = ProjectOpsPane.TOPICS,
                columns = emptyList(),
                detail = null,
                boardLoading = boardSlug != null,
                detailLoading = false,
                error = null,
            )
        }
        boardSlug?.let { loadBoard(currentBinding.config, currentBinding.profileId, requestGeneration, projectId, it, null) }
    }

    fun selectBoard(boardSlug: String) {
        val current = mutableState.value
        if (current.projectBoards.none { it.slug == boardSlug } || current.selectedBoardSlug == boardSlug) return
        val currentBinding = binding ?: return
        val projectId = current.selectedProjectId ?: return
        val requestGeneration = nextGeneration()
        binding = currentBinding.copy(boardSlug = boardSlug, taskId = null, pane = ProjectOpsPane.TOPICS)
        mutableState.update {
            it.copy(
                selectedBoardSlug = boardSlug,
                selectedTaskId = null,
                selectedPane = ProjectOpsPane.TOPICS,
                columns = emptyList(),
                detail = null,
                boardLoading = true,
                detailLoading = false,
                error = null,
            )
        }
        loadBoard(currentBinding.config, currentBinding.profileId, requestGeneration, projectId, boardSlug, null)
    }

    fun selectTask(taskId: String) {
        val current = mutableState.value
        val task = current.tasks.firstOrNull { it.id == taskId } ?: return
        val currentBinding = binding ?: return
        val boardSlug = current.selectedBoardSlug ?: return
        val requestGeneration = generation.get()
        binding = currentBinding.copy(taskId = task.id)
        mutableState.update { it.copy(selectedTaskId = task.id, detail = null, detailLoading = true, error = null) }
        loadDetail(currentBinding.config, currentBinding.profileId, requestGeneration, boardSlug, task.id)
    }

    fun showPane(pane: ProjectOpsPane) {
        mutableState.update { it.copy(selectedPane = pane) }
        binding = binding?.copy(pane = pane)
    }

    private fun reload() {
        val currentBinding = binding ?: return
        val requestGeneration = nextGeneration()
        mutableState.value = ProjectOpsUiState(
            backendId = currentBinding.config.id,
            profileId = currentBinding.profileId,
            selectedPane = currentBinding.pane,
            loading = true,
        )
        loadJob = viewModelScope.launch {
            try {
                val projects = repository.projects(currentBinding.config, currentBinding.profileId).projects.filter { it.id.isNotBlank() }
                val boardsResponse = repository.boards(currentBinding.config, currentBinding.profileId)
                if (generation.get() != requestGeneration) return@launch
                val selectedProjectId = reconcileProjectId(projects, currentBinding.projectId)
                val projectBoards = boardsForProject(boardsResponse.boards, selectedProjectId)
                val selectedBoardSlug = reconcileBoardSlug(projectBoards, currentBinding.boardSlug, boardsResponse.current)
                binding = currentBinding.copy(projectId = selectedProjectId, boardSlug = selectedBoardSlug)
                mutableState.update {
                    it.copy(
                        projects = projects,
                        boards = boardsResponse.boards,
                        selectedProjectId = selectedProjectId,
                        selectedBoardSlug = selectedBoardSlug,
                        loading = false,
                        boardLoading = selectedBoardSlug != null,
                        error = null,
                    )
                }
                if (selectedProjectId != null && selectedBoardSlug != null) {
                    loadBoard(
                        currentBinding.config,
                        currentBinding.profileId,
                        requestGeneration,
                        selectedProjectId,
                        selectedBoardSlug,
                        currentBinding.taskId,
                    )
                }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Throwable) {
                if (generation.get() == requestGeneration) {
                    mutableState.update { it.copy(loading = false, boardLoading = false, error = error.projectOpsMessage()) }
                }
            }
        }
    }

    private fun loadBoard(
        config: BackendConfig,
        profileId: String,
        requestGeneration: Long,
        projectId: String,
        boardSlug: String,
        requestedTaskId: String?,
    ) {
        boardJob?.cancel()
        detailJob?.cancel()
        boardJob = viewModelScope.launch {
            try {
                val response = repository.board(config, profileId, boardSlug)
                if (generation.get() != requestGeneration) return@launch
                val columns = response.columns.map { column ->
                    column.copy(tasks = column.tasks.filter { it.projectId == projectId })
                }
                val tasks = tasksForProject(columns, projectId)
                val selectedTaskId = requestedTaskId?.takeIf { requested -> tasks.any { it.id == requested } }
                mutableState.update {
                    it.copy(
                        columns = columns,
                        selectedTaskId = selectedTaskId,
                        boardLoading = false,
                        detail = null,
                        detailLoading = selectedTaskId != null,
                        error = null,
                    )
                }
                selectedTaskId?.let { loadDetail(config, profileId, requestGeneration, boardSlug, it) }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Throwable) {
                if (generation.get() == requestGeneration) {
                    mutableState.update { it.copy(boardLoading = false, detailLoading = false, error = error.projectOpsMessage()) }
                }
            }
        }
    }

    private fun loadDetail(
        config: BackendConfig,
        profileId: String,
        requestGeneration: Long,
        boardSlug: String,
        taskId: String,
    ) {
        detailJob?.cancel()
        detailJob = viewModelScope.launch {
            try {
                val detail = repository.task(config, profileId, boardSlug, taskId)
                val current = mutableState.value
                if (
                    generation.get() != requestGeneration ||
                    current.selectedBoardSlug != boardSlug ||
                    current.selectedTaskId != taskId ||
                    detail.task.id != taskId ||
                    detail.task.projectId != current.selectedProjectId
                ) return@launch
                mutableState.update { it.copy(detail = detail, detailLoading = false, error = null) }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Throwable) {
                if (generation.get() == requestGeneration && mutableState.value.selectedTaskId == taskId) {
                    mutableState.update { it.copy(detailLoading = false, error = error.projectOpsMessage()) }
                }
            }
        }
    }

    private fun nextGeneration(): Long {
        loadJob?.cancel()
        boardJob?.cancel()
        detailJob?.cancel()
        return generation.incrementAndGet()
    }

    private data class Binding(
        val config: BackendConfig,
        val profileId: String,
        val projectId: String?,
        val boardSlug: String?,
        val taskId: String?,
        val pane: ProjectOpsPane,
    )
}

internal fun reconcileProjectId(projects: List<ProjectOpsProject>, requested: String?): String? =
    requested?.takeIf { candidate -> projects.any { it.id == candidate } } ?: projects.firstOrNull()?.id

internal fun boardsForProject(boards: List<ProjectOpsBoard>, projectId: String?): List<ProjectOpsBoard> =
    projectId?.let { selected -> boards.filter { it.projectId == selected } }.orEmpty()

internal fun reconcileBoardSlug(
    boards: List<ProjectOpsBoard>,
    requested: String?,
    current: String?,
): String? = requested?.takeIf { candidate -> boards.any { it.slug == candidate } }
    ?: current?.takeIf { candidate -> boards.any { it.slug == candidate } }
    ?: boards.firstOrNull()?.slug

internal fun tasksForProject(columns: List<ProjectOpsColumn>, projectId: String?): List<ProjectOpsTask> =
    projectId?.let { selected ->
        columns.flatMap(ProjectOpsColumn::tasks).filter { it.projectId == selected }
    }.orEmpty()

private fun Throwable.projectOpsMessage(): String = message?.trim().takeUnless { it.isNullOrBlank() }
    ?: "Hermes Project Ops could not load server-owned data"
