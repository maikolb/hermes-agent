package com.nousresearch.hermes.projectops

import com.nousresearch.hermes.data.SessionRestorationState
import com.nousresearch.hermes.data.SessionRestorationStatus
import com.nousresearch.hermes.protocol.StoredSession

internal sealed interface ProjectOpsChatNavigation {
    data object Waiting : ProjectOpsChatNavigation
    data object Cancelled : ProjectOpsChatNavigation
    data class Open(val session: StoredSession) : ProjectOpsChatNavigation
}

internal fun projectOpsStoredSession(
    task: ProjectOpsTask,
    sessions: List<StoredSession>,
    profileId: String,
): StoredSession? {
    val sessionId = task.sessionId?.takeIf(String::isNotBlank) ?: return null
    val serverSession = sessions.firstOrNull { session ->
        session.durableId == sessionId &&
            (session.profile?.takeIf(String::isNotBlank) ?: profileId) == profileId
    }
    if (serverSession != null) {
        return if (serverSession.source.isNullOrBlank()) {
            serverSession.copy(source = "project_ops")
        } else {
            serverSession
        }
    }
    return StoredSession(
        sessionId = sessionId,
        profile = profileId,
        source = "project_ops",
        title = task.title,
    )
}

internal fun projectOpsChatNavigation(
    pendingSessionId: String?,
    restoration: SessionRestorationState,
): ProjectOpsChatNavigation {
    val pending = pendingSessionId?.takeIf(String::isNotBlank)
        ?: return ProjectOpsChatNavigation.Cancelled
    val requested = restoration.requestedSessionId?.takeIf(String::isNotBlank)
        ?: return ProjectOpsChatNavigation.Waiting
    if (requested != pending) return ProjectOpsChatNavigation.Waiting
    return when (restoration.status) {
        SessionRestorationStatus.AUTHENTICATING,
        SessionRestorationStatus.REHYDRATING,
        -> ProjectOpsChatNavigation.Waiting
        SessionRestorationStatus.READY -> restoration.session
            ?.let(ProjectOpsChatNavigation::Open)
            ?: ProjectOpsChatNavigation.Cancelled
        SessionRestorationStatus.IDLE,
        SessionRestorationStatus.BACKEND_UNAVAILABLE,
        SessionRestorationStatus.AUTHENTICATION_REQUIRED,
        SessionRestorationStatus.RECOVERY_REQUIRED,
        SessionRestorationStatus.SESSION_UNAVAILABLE,
        SessionRestorationStatus.PROFILE_MISMATCH,
        -> ProjectOpsChatNavigation.Cancelled
    }
}
