package com.nousresearch.hermes.projectops

import com.nousresearch.hermes.protocol.StoredSession

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
