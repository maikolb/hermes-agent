package com.nousresearch.hermes.projectops

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ArrowBack
import androidx.compose.material.icons.outlined.KeyboardArrowDown
import androidx.compose.material.icons.outlined.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.nousresearch.hermes.data.BackendConfig
import com.nousresearch.hermes.ui.navigation.ProjectOpsPane

@Composable
fun ProjectOpsRoute(
    backend: BackendConfig,
    profileId: String,
    projectId: String?,
    boardSlug: String?,
    taskId: String?,
    pane: ProjectOpsPane?,
    expanded: Boolean,
    onBack: () -> Unit,
    onOpenChat: (ProjectOpsTask) -> Unit,
    viewModel: ProjectOpsViewModel = hiltViewModel(),
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    LaunchedEffect(backend.id, profileId, projectId, boardSlug, taskId, pane) {
        viewModel.bind(backend, profileId, projectId, boardSlug, taskId, pane)
    }
    ProjectOpsScreen(
        state = state,
        expanded = expanded,
        onBack = onBack,
        onRetry = viewModel::retry,
        onSelectProject = viewModel::selectProject,
        onSelectBoard = viewModel::selectBoard,
        onSelectTask = viewModel::selectTask,
        onSelectPane = viewModel::showPane,
        onOpenChat = onOpenChat,
    )
}

@Composable
@OptIn(ExperimentalMaterial3Api::class)
internal fun ProjectOpsScreen(
    state: ProjectOpsUiState,
    expanded: Boolean,
    onBack: () -> Unit,
    onRetry: () -> Unit,
    onSelectProject: (String) -> Unit,
    onSelectBoard: (String) -> Unit,
    onSelectTask: (String) -> Unit,
    onSelectPane: (ProjectOpsPane) -> Unit,
    onOpenChat: (ProjectOpsTask) -> Unit,
    modifier: Modifier = Modifier,
) {
    var detailVisible by remember(state.selectedTaskId) { mutableStateOf(state.selectedTaskId != null) }
    Column(modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Outlined.ArrowBack, "Back from Project Ops") }
            Column(Modifier.weight(1f)) {
                Text("PROJECT OPS", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                Text("Server-owned projects, topics and board evidence", style = MaterialTheme.typography.bodySmall)
            }
            IconButton(onClick = onRetry) { Icon(Icons.Outlined.Refresh, "Refresh Project Ops") }
        }
        HorizontalDivider()

        if (state.loading) {
            ProjectOpsLoading("Loading projects and boards", Modifier.weight(1f))
            return@Column
        }
        state.error?.let { error ->
            ProjectOpsError(error, onRetry, Modifier.padding(12.dp))
            if (state.projects.isEmpty()) return@Column
        }
        if (state.projects.isEmpty()) {
            ProjectOpsEmpty(
                title = "NO PROJECTS",
                detail = "Hermes did not return any active Project Ops projects.",
                modifier = Modifier.weight(1f),
            )
            return@Column
        }

        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            ProjectOpsSelector(
                label = "Project",
                selected = state.projects.firstOrNull { it.id == state.selectedProjectId }?.name ?: "Choose project",
                options = state.projects.map { it.id to it.name },
                onSelect = onSelectProject,
                modifier = Modifier.weight(1f),
            )
            ProjectOpsSelector(
                label = "Board",
                selected = state.projectBoards.firstOrNull { it.slug == state.selectedBoardSlug }?.name ?: "No linked board",
                options = state.projectBoards.map { it.slug to it.name },
                onSelect = onSelectBoard,
                modifier = Modifier.weight(1f),
            )
        }

        if (state.projectBoards.isEmpty()) {
            ProjectOpsEmpty(
                title = "NO LINKED BOARDS",
                detail = "This project has no server-owned board. Choose another project or retry after linking one in Hermes.",
                modifier = Modifier.weight(1f),
            )
            return@Column
        }

        if (!expanded) {
            ProjectOpsPaneSwitcher(
                selectedPane = state.selectedPane,
                canOpenChat = !state.selectedTask?.sessionId.isNullOrBlank(),
                onSelect = { pane ->
                    if (pane == ProjectOpsPane.CHAT) {
                        state.selectedTask?.takeIf { !it.sessionId.isNullOrBlank() }?.let(onOpenChat)
                            ?: onSelectPane(ProjectOpsPane.CHAT)
                    } else {
                        onSelectPane(pane)
                    }
                },
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
            )
        }

        if (state.boardLoading) {
            ProjectOpsLoading("Loading board topics", Modifier.weight(1f))
        } else if (expanded) {
            Row(Modifier.weight(1f).fillMaxWidth()) {
                ProjectOpsTopicsPane(
                    tasks = state.tasks,
                    selectedTaskId = state.selectedTaskId,
                    onSelectTask = { task -> onSelectTask(task.id); detailVisible = true },
                    modifier = Modifier.weight(1f).fillMaxHeight(),
                )
                HorizontalDivider(Modifier.fillMaxHeight().width(1.dp))
                ProjectOpsBoardPane(
                    state = state,
                    onSelectTask = { task -> onSelectTask(task.id); detailVisible = true },
                    modifier = Modifier.weight(1f).fillMaxHeight(),
                )
            }
        } else {
            when (state.selectedPane) {
                ProjectOpsPane.TOPICS -> ProjectOpsTopicsPane(
                    tasks = state.tasks,
                    selectedTaskId = state.selectedTaskId,
                    onSelectTask = { task -> onSelectTask(task.id); detailVisible = true },
                    modifier = Modifier.weight(1f),
                )
                ProjectOpsPane.BOARD -> ProjectOpsBoardPane(
                    state = state,
                    onSelectTask = { task -> onSelectTask(task.id); detailVisible = true },
                    modifier = Modifier.weight(1f),
                )
                ProjectOpsPane.CHAT -> ProjectOpsEmpty(
                    title = "CHAT UNAVAILABLE",
                    detail = "Select a topic with a server-owned session before opening Chat.",
                    modifier = Modifier.weight(1f),
                )
            }
        }
    }

    val selectedTask = state.selectedTask
    if (detailVisible && selectedTask != null) {
        ModalBottomSheet(onDismissRequest = { detailVisible = false }) {
            ProjectOpsTaskDetail(
                task = selectedTask,
                detail = state.detail,
                loading = state.detailLoading,
                onOpenChat = { onOpenChat(selectedTask) },
                modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp).padding(bottom = 24.dp),
            )
        }
    }
}

@Composable
private fun ProjectOpsPaneSwitcher(
    selectedPane: ProjectOpsPane,
    canOpenChat: Boolean,
    onSelect: (ProjectOpsPane) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        ProjectOpsPane.entries.forEach { pane ->
            OutlinedButton(
                onClick = { onSelect(pane) },
                modifier = Modifier.weight(1f),
                enabled = pane != ProjectOpsPane.CHAT || canOpenChat,
            ) {
                Text(pane.name.lowercase().replaceFirstChar(Char::uppercase))
            }
        }
    }
    Text(
        "Showing ${selectedPane.name.lowercase()} pane",
        style = MaterialTheme.typography.labelSmall,
        modifier = Modifier.semantics { contentDescription = "Phone Project Ops pane: ${selectedPane.name.lowercase()}" },
    )
}

@Composable
private fun ProjectOpsSelector(
    label: String,
    selected: String,
    options: List<Pair<String, String>>,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var expanded by remember { mutableStateOf(false) }
    Box(modifier) {
        OutlinedButton(
            onClick = { expanded = true },
            enabled = options.isNotEmpty(),
            modifier = Modifier.fillMaxWidth().semantics { contentDescription = "$label selector: $selected" },
        ) {
            Text(selected, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
            Icon(Icons.Outlined.KeyboardArrowDown, null)
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            options.forEach { (id, name) ->
                DropdownMenuItem(
                    text = { Text(name) },
                    onClick = { expanded = false; onSelect(id) },
                )
            }
        }
    }
}

@Composable
private fun ProjectOpsTopicsPane(
    tasks: List<ProjectOpsTask>,
    selectedTaskId: String?,
    onSelectTask: (ProjectOpsTask) -> Unit,
    modifier: Modifier = Modifier,
) {
    if (tasks.isEmpty()) {
        ProjectOpsEmpty("NO TOPICS", "This board has no topics belonging to the selected project.", modifier)
        return
    }
    LazyColumn(modifier, contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { Text("TOPICS", style = MaterialTheme.typography.titleMedium, modifier = Modifier.semantics { heading() }) }
        items(tasks, key = ProjectOpsTask::id) { task ->
            Surface(
                modifier = Modifier.fillMaxWidth().clickable { onSelectTask(task) }
                    .semantics { contentDescription = "Topic ${task.title}, status ${task.status}" },
                shape = RoundedCornerShape(12.dp),
                tonalElevation = if (task.id == selectedTaskId) 4.dp else 1.dp,
            ) {
                Column(Modifier.padding(14.dp)) {
                    Text(task.title, style = MaterialTheme.typography.titleSmall)
                    Text(task.status.uppercase(), style = MaterialTheme.typography.labelMedium)
                    task.assignee?.takeIf(String::isNotBlank)?.let { Text("Assignee: $it", style = MaterialTheme.typography.bodySmall) }
                    if (task.sessionId.isNullOrBlank()) {
                        Text("No server session; Chat is disabled", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
    }
}

@Composable
private fun ProjectOpsBoardPane(
    state: ProjectOpsUiState,
    onSelectTask: (ProjectOpsTask) -> Unit,
    modifier: Modifier = Modifier,
) {
    LazyColumn(modifier, contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item { Text("BOARD", style = MaterialTheme.typography.titleMedium, modifier = Modifier.semantics { heading() }) }
        item {
            Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 1.dp, modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(14.dp)) {
                    Text("STATUS SUMMARY", style = MaterialTheme.typography.labelLarge)
                    if (state.columns.isEmpty()) Text("No status columns returned")
                    state.columns.forEach { column ->
                        Text("${column.name.replace('_', ' ').uppercase()}: ${column.tasks.size}")
                    }
                }
            }
        }
        state.columns.forEach { column ->
            item { Text(column.name.replace('_', ' ').uppercase(), style = MaterialTheme.typography.labelLarge) }
            items(column.tasks, key = { "${column.name}:${it.id}" }) { task ->
                TextButton(onClick = { onSelectTask(task) }, modifier = Modifier.fillMaxWidth()) {
                    Text(task.title, modifier = Modifier.weight(1f), maxLines = 2, overflow = TextOverflow.Ellipsis)
                    Text(task.status)
                }
            }
        }
    }
}

@Composable
private fun ProjectOpsTaskDetail(
    task: ProjectOpsTask,
    detail: ProjectOpsTaskDetailResponse?,
    loading: Boolean,
    onOpenChat: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("TOPIC DETAIL", style = MaterialTheme.typography.titleMedium, modifier = Modifier.semantics { heading() })
        Text(task.title, style = MaterialTheme.typography.titleLarge)
        Text("Status: ${task.status}")
        Text("Assignee: ${task.assignee?.takeIf(String::isNotBlank) ?: "Unassigned"}")
        Text("Session: ${task.sessionId?.takeIf(String::isNotBlank) ?: "Unavailable"}")
        task.body?.takeIf(String::isNotBlank)?.let { Text(it) }
        Button(onClick = onOpenChat, enabled = !task.sessionId.isNullOrBlank()) { Text("Open existing chat") }
        if (task.sessionId.isNullOrBlank()) {
            Text("Chat is disabled because Hermes did not return a session_id for this topic.")
        }
        if (loading) {
            CircularProgressIndicator(Modifier.align(Alignment.CenterHorizontally))
            return@Column
        }
        detail?.let { hydrated ->
            ProjectOpsEvidence("COMMENTS", hydrated.comments.map { "${it.author}: ${it.body}" })
            ProjectOpsEvidence("RUNS", hydrated.runs.map { run ->
                listOfNotNull(run.profile, run.status, run.outcome, run.summary, run.error).joinToString(" · ")
            })
            ProjectOpsEvidence("EVENTS", hydrated.events.map { it.kind })
            ProjectOpsEvidence("DIAGNOSTICS", hydrated.task.diagnostics.map { "${it.severity}: ${it.title} — ${it.detail}" })
        }
        Spacer(Modifier.height(8.dp))
    }
}

@Composable
private fun ProjectOpsEvidence(title: String, rows: List<String>) {
    if (rows.isEmpty()) return
    HorizontalDivider()
    Text(title, style = MaterialTheme.typography.labelLarge)
    rows.forEach { row -> Text(row, style = MaterialTheme.typography.bodySmall) }
}

@Composable
private fun ProjectOpsLoading(label: String, modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
            CircularProgressIndicator()
            Text(label)
        }
    }
}

@Composable
private fun ProjectOpsEmpty(title: String, detail: String, modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(detail, style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun ProjectOpsError(error: String, onRetry: () -> Unit, modifier: Modifier = Modifier) {
    Surface(modifier.fillMaxWidth(), color = MaterialTheme.colorScheme.errorContainer, shape = RoundedCornerShape(10.dp)) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(error, modifier = Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
            TextButton(onClick = onRetry) { Text("Retry") }
        }
    }
}
