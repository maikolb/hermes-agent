param(
    [string]$WorkspaceRoot = (Get-Location).Path,
    [string]$RunPath,
    [ValidateSet("Definition", "Checkpoint", "Terminal")]
    [string]$Phase = "Definition",
    [string]$EventsPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runSchemaPath = Join-Path $scriptRoot "schemas\agent-loop-run.schema.json"
$eventSchemaPath = Join-Path $scriptRoot "schemas\agent-loop-event.schema.json"
$runtimeCommonPath = Join-Path $scriptRoot "agent_loop_runtime_common.ps1"
$errors = New-Object System.Collections.Generic.List[string]

if (-not (Test-Path -LiteralPath $runtimeCommonPath -PathType Leaf)) {
    throw "Agent-loop runtime common helper is missing from the installed runtime package: $runtimeCommonPath"
}
. $runtimeCommonPath

function Add-ValidationError {
    param([string]$Message)
    $errors.Add($Message) | Out-Null
}

function Resolve-WorkspacePath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }
    return Join-Path $root $Path
}


function Get-TextSha256 {
    param([string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Get-CanonicalDigest {
    param([System.Collections.IDictionary]$Descriptor)
    $canonicalJson = $Descriptor | ConvertTo-Json -Depth 20 -Compress
    return Get-TextSha256 -Text $canonicalJson
}

function Test-Placeholder {
    param([object]$Value)
    if ($null -eq $Value) {
        return $true
    }
    return ([string]$Value) -match '(?i)(FILL_|PLACEHOLDER|\{\{|TBD|TODO)'
}

function Test-Schema {
    param(
        [string]$Json,
        [string]$SchemaPath,
        [string]$Label
    )
    try {
        $valid = $Json | Test-Json -SchemaFile $SchemaPath -ErrorAction SilentlyContinue
        if (-not $valid) {
            Add-ValidationError "$Label schema validation failed."
            return $false
        }
        return $true
    } catch {
        Add-ValidationError "$Label schema validation failed: $($_.Exception.Message)"
        return $false
    }
}

if (-not $RunPath) {
    $RunPath = "internal\ops\AGENT_LOOP_RUN.json"
}
$resolvedRunPath = Resolve-WorkspacePath -Path $RunPath
if (-not (Test-Path -LiteralPath $resolvedRunPath -PathType Leaf)) {
    Write-Error "Agent loop run not found: $resolvedRunPath"
    exit 1
}
if (-not (Test-Path -LiteralPath $runSchemaPath) -or -not (Test-Path -LiteralPath $eventSchemaPath)) {
    Write-Error "Agent loop schemas are missing under $scriptRoot\schemas."
    exit 1
}

$raw = Get-Content -Raw -LiteralPath $resolvedRunPath
$schemaValid = Test-Schema -Json $raw -SchemaPath $runSchemaPath -Label "Run"
try {
    $run = $raw | ConvertFrom-Json
} catch {
    Add-ValidationError "Run JSON is invalid: $($_.Exception.Message)"
    $run = $null
}

if ($null -ne $run -and $schemaValid) {
    $effectiveEventSchemaPath = $eventSchemaPath
    if ($raw -match '(?i)"(chainOfThought|rawReasoning|privateReasoning|thoughts|secret|credential|rawPayload)"\s*:') {
        Add-ValidationError "Trace/run objects contain a prohibited raw reasoning, secret, credential, or unrestricted payload field."
    }
    if (Test-Placeholder -Value $raw) {
        Add-ValidationError "Run contains placeholder content and is not executable."
    }
    $declaredEventSchemaPath = Resolve-WorkspacePath -Path ([string]$run.eventsRef.schemaRef)
    if (-not (Test-AofPathWithinRoot -Path $declaredEventSchemaPath -Root $root) -or
        -not (Test-Path -LiteralPath $declaredEventSchemaPath -PathType Leaf)) {
        Add-ValidationError "eventsRef.schemaRef must resolve to the packaged event schema used for validation."
    } elseif ((Get-AofArtifactSha256 -Path $declaredEventSchemaPath) -ne (Get-AofArtifactSha256 -Path $eventSchemaPath)) {
        Add-ValidationError "eventsRef.schemaRef is incompatible with the packaged agent-loop event schema."
    } else {
        $effectiveEventSchemaPath = $declaredEventSchemaPath
    }
    foreach ($uuidField in @("runId", "attemptId")) {
        $parsedUuid = [guid]::Empty
        if (-not [guid]::TryParse([string]$run.identity.$uuidField, [ref]$parsedUuid)) {
            Add-ValidationError "identity.$uuidField must be a valid UUID."
        }
    }
    if ($run.identity.PSObject.Properties.Name -contains "parentRunId") {
        $parsedParentUuid = [guid]::Empty
        if (-not [guid]::TryParse([string]$run.identity.parentRunId, [ref]$parsedParentUuid)) {
            Add-ValidationError "identity.parentRunId must be a valid UUID."
        }
    }
    $parsedStartedAt = [datetimeoffset]::MinValue
    if (-not [datetimeoffset]::TryParse(
        [string]$run.current.startedAt,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$parsedStartedAt
    )) {
        Add-ValidationError "current.startedAt must be a valid date-time."
    }

    $contractPath = Resolve-WorkspacePath -Path ([string]$run.contractRef.path)
    if (-not (Test-Path -LiteralPath $contractPath -PathType Leaf)) {
        Add-ValidationError "contractRef.path does not exist: $($run.contractRef.path)"
    } else {
        $actualContractHash = Get-AofArtifactSha256 -Path $contractPath
        if ($actualContractHash -ne [string]$run.contractRef.sha256) {
            Add-ValidationError "contractRef.sha256 does not match the frozen execution contract."
        }
    }

    $fixedClasses = @(
        "transient",
        "invalid-input",
        "precondition-failed",
        "validation-failed",
        "policy-blocked",
        "ambiguous-side-effect",
        "unsafe-to-retry",
        "environment-unavailable",
        "stagnated"
    )
    $seenClasses = @{}
    foreach ($policy in @($run.failurePolicies)) {
        $class = [string]$policy.class
        if ($seenClasses.ContainsKey($class)) {
            Add-ValidationError "failurePolicies contains duplicate class '$class'."
        }
        $seenClasses[$class] = $true
        if ($policy.retryable) {
            if ([int]$policy.limit -le 0 -or [string]$policy.backoff.type -eq "none" -or -not $policy.requireReobservation) {
                Add-ValidationError "Retryable failure '$class' requires a positive limit, backoff, and mandatory re-observation."
            }
            if ([string]$policy.safetyMechanism -eq "no-retry") {
                Add-ValidationError "Retryable failure '$class' requires idempotency, reconciliation, or compensation."
            }
        }
    }
    foreach ($requiredClass in $fixedClasses) {
        if (-not $seenClasses.ContainsKey($requiredClass)) {
            Add-ValidationError "failurePolicies is missing fixed class '$requiredClass'."
        }
    }
    foreach ($unsafeClass in @("invalid-input", "policy-blocked", "unsafe-to-retry", "ambiguous-side-effect")) {
        $unsafePolicy = @($run.failurePolicies | Where-Object { $_.class -eq $unsafeClass })
        if ($unsafePolicy.Count -eq 1 -and $unsafePolicy[0].retryable) {
            Add-ValidationError "Failure '$unsafeClass' cannot be blindly retryable."
        }
    }

    if ([int]$run.budgets.maxConcurrency -gt 1) {
        $safeConcurrentModes = @("compare-and-swap", "declared-reducer", "lock", "partitioned-isolation")
        if ($safeConcurrentModes -notcontains [string]$run.concurrency.mode) {
            Add-ValidationError "Concurrency above one requires CAS, a declared reducer, a lock, or partitioned isolation."
        }
    }
    switch ([string]$run.concurrency.mode) {
        "compare-and-swap" {
            if (-not $run.concurrency.casEnabled) { Add-ValidationError "compare-and-swap concurrency requires casEnabled=true." }
        }
        "declared-reducer" {
            if ([string]::IsNullOrWhiteSpace([string]$run.concurrency.reducerRef)) { Add-ValidationError "declared-reducer concurrency requires reducerRef." }
        }
        "lock" {
            if ([string]::IsNullOrWhiteSpace([string]$run.concurrency.lockRef)) { Add-ValidationError "lock concurrency requires lockRef." }
        }
        "partitioned-isolation" {
            if (@($run.concurrency.partitions).Count -eq 0) { Add-ValidationError "partitioned-isolation requires declared partitions." }
        }
    }
    if ([string]$run.trigger.overlapPolicy -eq "parallel-isolated") {
        if ([string]$run.concurrency.mode -ne "partitioned-isolation" -or @($run.concurrency.partitions).Count -eq 0) {
            Add-ValidationError "parallel-isolated overlap requires partitioned-isolation with declared partitions."
        }
    }
    if ([string]$run.trigger.type -in @("scheduled", "event")) {
        $triggerProvenanceMissing = Test-Placeholder -Value $run.trigger.provenance
        $deduplicationKeyMissing = Test-Placeholder -Value $run.trigger.deduplicationKey
        if ($triggerProvenanceMissing -or $deduplicationKeyMissing -or [int]$run.trigger.deduplicationWindowSeconds -le 0) {
            Add-ValidationError "Scheduled/event triggers require provenance, a deduplication key, and a positive deduplication window."
        }
    }

    $resumable = ([string]$run.trigger.type -eq "resume") -or (@($run.qualification.reasons) -contains "resumable")
    if ($resumable) {
        if ([string]$run.checkpoint.resumePolicy -eq "never" -or
            (Test-Placeholder $run.checkpoint.stateSchemaRef) -or
            (Test-Placeholder $run.checkpoint.priorCheckpointRef) -or
            [string]$run.checkpoint.atomicWritePolicy -notin @("atomic-replace", "transactional", "compare-and-swap")) {
            Add-ValidationError "A resumable loop requires atomic checkpointing, a compatible state schema, and a prior checkpoint reference."
        }
        $resolvedStateSchemaPath = Resolve-WorkspacePath -Path ([string]$run.checkpoint.stateSchemaRef)
        if (-not (Test-AofPathWithinRoot -Path $resolvedStateSchemaPath -Root $root) -or
            -not (Test-Path -LiteralPath $resolvedStateSchemaPath -PathType Leaf)) {
            Add-ValidationError "A resumable loop stateSchemaRef must resolve to a packaged workspace schema file."
        } else {
            try {
                $stateSchema = Get-Content -Raw -LiteralPath $resolvedStateSchemaPath | ConvertFrom-Json
                $declaredStateSchemaVersion = if ($stateSchema.PSObject.Properties.Name -contains "properties" -and
                    $stateSchema.properties.PSObject.Properties.Name -contains "schemaVersion" -and
                    $stateSchema.properties.schemaVersion.PSObject.Properties.Name -contains "const") {
                    [string]$stateSchema.properties.schemaVersion.const
                } else {
                    ""
                }
                if ([string]::IsNullOrWhiteSpace($declaredStateSchemaVersion) -or
                    [string]$run.checkpoint.stateSchemaVersion -cne $declaredStateSchemaVersion) {
                    Add-ValidationError "A resumable loop stateSchemaVersion must match the referenced schema's declared schemaVersion const."
                }
            } catch {
                Add-ValidationError "A resumable loop stateSchemaRef must contain a readable JSON Schema: $($_.Exception.Message)"
            }
        }
    }

    $checkpointDescriptor = [ordered]@{
        atomicWritePolicy = [string]$run.checkpoint.atomicWritePolicy
        expectedStateVersion = [int]$run.checkpoint.expectedStateVersion
        iteration = [int]$run.checkpoint.iteration
        lastEventSequence = [int]$run.checkpoint.lastEventSequence
        lastKnownGoodRef = [string]$run.checkpoint.lastKnownGoodRef
        priorCheckpointRef = [string]$run.checkpoint.priorCheckpointRef
        replayPolicy = [string]$run.checkpoint.replayPolicy
        resumePolicy = [string]$run.checkpoint.resumePolicy
        stateSchemaRef = [string]$run.checkpoint.stateSchemaRef
        stateSchemaVersion = [string]$run.checkpoint.stateSchemaVersion
    }
    if ((Get-CanonicalDigest -Descriptor $checkpointDescriptor) -ne [string]$run.checkpoint.digest) {
        Add-ValidationError "checkpoint.digest does not match the canonical checkpoint descriptor."
    }
    if ([int]$run.current.observation.stateVersion -ne [int]$run.checkpoint.expectedStateVersion) {
        Add-ValidationError "Current observation state version disagrees with the checkpoint expected state version."
    }

    $requiredBindings = @("actionDigest", "argumentDigest", "preconditions", "stateVersion")
    foreach ($binding in $requiredBindings) {
        if (@($run.approvalPolicy.bindingFields) -notcontains $binding) {
            Add-ValidationError "approvalPolicy.bindingFields must include '$binding'."
        }
    }
    if (-not $run.approvalPolicy.reapprovalOnStateChange) {
        Add-ValidationError "approvalPolicy must force reapproval when state changes."
    }

    $requiredTraceFields = @("sequence", "timestamp", "eventType", "traceId", "loopId", "runId", "attemptId", "iteration", "stateVersion", "redactedFields")
    foreach ($field in $requiredTraceFields) {
        if (@($run.tracePolicy.allowedEventFields) -notcontains $field) {
            Add-ValidationError "tracePolicy.allowedEventFields must include '$field'."
        }
    }
    foreach ($prohibited in @("raw-chain-of-thought", "secrets", "credentials", "unrestricted-payloads")) {
        if (@($run.tracePolicy.prohibitedContent) -notcontains $prohibited) {
            Add-ValidationError "tracePolicy.prohibitedContent must include '$prohibited'."
        }
    }

    $checkerIdentitiesEqual = [string]$run.verificationPolicy.executorIdentity -eq [string]$run.verificationPolicy.checkerIdentity
    if ($checkerIdentitiesEqual -and $run.verificationPolicy.checkerIndependent) {
        Add-ValidationError "A checker cannot be its own final checker by self-attesting checkerIndependent=true when executor and checker identities are equal."
    } elseif ($run.verificationPolicy.highRisk -and
        $checkerIdentitiesEqual -and
        -not ([string]$run.verificationPolicy.oracleRef).StartsWith("deterministic:")) {
        Add-ValidationError "A high-risk executor cannot be its own final checker without an external deterministic oracle."
    }
    if ([string]$run.current.verification.checkerDigest -ne [string]$run.verificationPolicy.checkerDigest) {
        Add-ValidationError "Current verification checker digest differs from the frozen checker digest."
    }

    if ($run.PSObject.Properties.Name -contains "scopePolicy") {
        $scopePolicy = $run.scopePolicy
        $scopeDescriptor = [ordered]@{
            allowDeletions = [bool]$scopePolicy.allowDeletions
            allowedWritePaths = @($scopePolicy.allowedWritePaths)
            deniedWritePaths = @($scopePolicy.deniedWritePaths)
            schemaVersion = [int]$scopePolicy.schemaVersion
        }
        if ([int]$scopePolicy.schemaVersion -ne 1) {
            Add-ValidationError "scopePolicy.schemaVersion must be 1."
        }
        foreach ($scopePattern in @($scopePolicy.allowedWritePaths) + @($scopePolicy.deniedWritePaths)) {
            if ([System.IO.Path]::IsPathRooted([string]$scopePattern) -or ([string]$scopePattern) -match '(^|[\\/])\.\.([\\/]|$)') {
                Add-ValidationError "scopePolicy paths must be workspace-relative and cannot traverse parents: $scopePattern"
            }
        }
        if ((Get-CanonicalDigest -Descriptor $scopeDescriptor) -ne [string]$scopePolicy.manifestDigest) {
            Add-ValidationError "scopePolicy.manifestDigest does not match the canonical scope manifest."
        }
    }

    $checkerPath = Resolve-WorkspacePath -Path ([string]$run.verificationPolicy.checkerRef)
    $fixtureRefs = @($run.verificationPolicy.immutableFixtureRefs)
    $fixtureDigests = @($run.verificationPolicy.immutableFixtureDigests)
    $checkerFileHash = ""
    if (-not (Test-Path -LiteralPath $checkerPath -PathType Leaf)) {
        Add-ValidationError "verificationPolicy.checkerRef does not exist: $($run.verificationPolicy.checkerRef)"
    } else {
        $checkerFileHash = Get-AofArtifactSha256 -Path $checkerPath
    }
    if ($fixtureRefs.Count -ne $fixtureDigests.Count) {
        Add-ValidationError "Checker fixture references and digests must have the same count."
    }
    for ($fixtureIndex = 0; $fixtureIndex -lt [math]::Min($fixtureRefs.Count, $fixtureDigests.Count); $fixtureIndex++) {
        $fixturePath = Resolve-WorkspacePath -Path ([string]$fixtureRefs[$fixtureIndex])
        if (-not (Test-Path -LiteralPath $fixturePath -PathType Leaf)) {
            Add-ValidationError "Checker fixture does not exist: $($fixtureRefs[$fixtureIndex])"
        } elseif ((Get-AofArtifactSha256 -Path $fixturePath) -ne [string]$fixtureDigests[$fixtureIndex]) {
            Add-ValidationError "Checker fixture digest changed after the run began: $($fixtureRefs[$fixtureIndex])"
        }
    }
    $checkerDescriptor = [ordered]@{
        checkerFileSha256 = $checkerFileHash
        checkerIdentity = [string]$run.verificationPolicy.checkerIdentity
        fixtureDigests = @($fixtureDigests)
        fixtureRefs = @($fixtureRefs)
        oracleRef = [string]$run.verificationPolicy.oracleRef
    }
    if ($run.verificationPolicy.PSObject.Properties.Name -contains "checkerCommand") {
        $checkerCommand = $run.verificationPolicy.checkerCommand
        $checkerArgv = @($checkerCommand.argv)
        $checkerArgvJson = ConvertTo-Json -InputObject $checkerArgv -Compress
        $checkerArgvDigest = Get-TextSha256 -Text $checkerArgvJson
        if ($checkerArgvDigest -ne [string]$checkerCommand.argvDigest) {
            Add-ValidationError "verificationPolicy.checkerCommand.argvDigest does not match the exact argv."
        }
        $checkerExecutablePath = [string]$checkerArgv[0]
        if (-not [System.IO.Path]::IsPathRooted($checkerExecutablePath) -or -not (Test-Path -LiteralPath $checkerExecutablePath -PathType Leaf)) {
            Add-ValidationError "verificationPolicy.checkerCommand argv[0] must be an existing absolute executable path."
        } elseif ((Get-AofArtifactSha256 -Path $checkerExecutablePath) -ne [string]$checkerCommand.executableSha256) {
            Add-ValidationError "verificationPolicy.checkerCommand executable digest changed after the run began."
        }
        $checkerDescriptor["checkerCommandDigest"] = [string]$checkerCommand.argvDigest
        $checkerDescriptor["checkerExecutableSha256"] = [string]$checkerCommand.executableSha256
    }
    if ((Get-CanonicalDigest -Descriptor $checkerDescriptor) -ne [string]$run.verificationPolicy.checkerDigest) {
        Add-ValidationError "verificationPolicy.checkerDigest does not match the canonical checker descriptor."
    }

    $action = $run.current.action
    $idempotencyKeyValue = if ($action.PSObject.Properties.Name -contains "idempotencyKey") { [string]$action.idempotencyKey } else { "" }
    $reconciliationRefValue = if ($action.PSObject.Properties.Name -contains "reconciliationRef") { [string]$action.reconciliationRef } else { "" }
    $actionDescriptor = [ordered]@{
        argumentDigest = [string]$action.argumentDigest
        capabilityClass = [string]$action.capabilityClass
        consequential = [bool]$action.consequential
        fingerprint = [string]$action.fingerprint
        id = [string]$action.id
        idempotencyKey = $idempotencyKeyValue
        noRetry = [bool]$action.noRetry
        postconditions = @($action.postconditions)
        preconditions = @($action.preconditions)
        reconciliationRef = $reconciliationRefValue
    }
    if ((Get-CanonicalDigest -Descriptor $actionDescriptor) -ne [string]$action.actionDigest) {
        Add-ValidationError "current.action.actionDigest does not match the canonical action descriptor."
    }
    $intrinsicallyConsequential = [string]$action.capabilityClass -in @("external-side-effect", "destructive")
    if ($intrinsicallyConsequential -and -not [bool]$action.consequential) {
        Add-ValidationError "Capability class '$($action.capabilityClass)' is inherently consequential and cannot declare consequential=false."
    }
    $effectiveConsequential = [bool]$action.consequential -or $intrinsicallyConsequential
    if ($effectiveConsequential) {
        $hasIntentSequence = $action.PSObject.Properties.Name -contains "intentSequence"
        if (@($action.preconditions).Count -eq 0 -or @($action.postconditions).Count -eq 0 -or -not $hasIntentSequence) {
            Add-ValidationError "A consequential action requires preconditions, postconditions, and a persisted action intent."
        }
        $hasIdempotencyKey = ($action.PSObject.Properties.Name -contains "idempotencyKey") -and
            -not [string]::IsNullOrWhiteSpace([string]$action.idempotencyKey)
        $hasReconciliation = ($action.PSObject.Properties.Name -contains "reconciliationRef") -and
            -not [string]::IsNullOrWhiteSpace([string]$action.reconciliationRef)
        $hasSafety = $hasIdempotencyKey -or $hasReconciliation -or $action.noRetry
        if (-not $hasSafety) {
            Add-ValidationError "A side effect requires an idempotency key, deterministic reconciliation, or explicit no-retry policy."
        }
        if ($run.approvalPolicy.requiredForConsequentialActions) {
            if ($null -eq $action.PSObject.Properties["approval"]) {
                Add-ValidationError "A consequential action requires approval bound to the canonical action."
            } else {
                $approval = $action.approval
                $preconditionJson = @($action.preconditions) | ConvertTo-Json -Compress
                $preconditionDigest = Get-TextSha256 -Text $preconditionJson
                if ([string]$approval.actionDigest -ne [string]$action.actionDigest -or
                    [string]$approval.argumentDigest -ne [string]$action.argumentDigest -or
                    [string]$approval.preconditionDigest -ne $preconditionDigest -or
                    [int]$approval.stateVersion -ne [int]$run.current.observation.stateVersion) {
                    Add-ValidationError "Approval is not bound to the action digest, argument digest, preconditions, and state version."
                }
                if ([int]$approval.stateVersion -ne [int]$run.current.observation.stateVersion) {
                    Add-ValidationError "State changed after approval; reapproval is required."
                }
                if (@($run.approvalPolicy.authorityRefs) -notcontains [string]$approval.authorityRef -or
                    @($run.authorities.approvalAuthorityRefs) -notcontains [string]$approval.authorityRef) {
                    Add-ValidationError "Approval authority is not allowlisted by both approvalPolicy.authorityRefs and authorities.approvalAuthorityRefs."
                }
                if ([datetimeoffset]$approval.expiresAt -le [datetimeoffset]$approval.approvedAt) {
                    Add-ValidationError "Approval expiry must be later than approval time."
                }
            }
        }
    }

    $counterBudgetExceeded = [int]$run.current.iteration -gt [int]$run.budgets.maxIterations -or
        [int]$run.current.retryCount -gt [int]$run.budgets.maxRetries -or
        [int]$run.current.toolCallCount -gt [int]$run.budgets.maxToolCalls -or
        [int]$run.current.outputBytes -gt [int]$run.budgets.maxOutputBytes
    $stagnationBudgetExceeded = [int]$run.current.stagnantIterations -ge [int]$run.budgets.maxStagnantIterations
    $stateBudgetExceeded = $counterBudgetExceeded -or $stagnationBudgetExceeded
    if ($stateBudgetExceeded) {
        if ([string]$run.current.status -ne "budget-exhausted" -or -not [bool]$run.current.budgetExhausted) {
            Add-ValidationError "Exhausted usage requires budget-exhausted terminal semantics and current.budgetExhausted=true."
        }
        if ([string]$run.current.status -eq "success") {
            Add-ValidationError "Budget or iteration exhaustion cannot be represented as success."
        }
    }
    if (([string]$run.current.status -eq "budget-exhausted") -ne [bool]$run.current.budgetExhausted) {
        Add-ValidationError "budget-exhausted status and current.budgetExhausted must be consistent."
    }
    if ([int]$run.current.stagnantIterations -ge [int]$run.budgets.maxStagnantIterations -and [string]$run.current.status -eq "running") {
        Add-ValidationError "The stagnation ceiling was reached while status remained running."
    }

    if ($Phase -in @("Checkpoint", "Terminal")) {
        $resolvedEventsPath = if ($EventsPath) { Resolve-WorkspacePath -Path $EventsPath } else { Resolve-WorkspacePath -Path ([string]$run.eventsRef.path) }
        $events = New-Object System.Collections.Generic.List[object]
        $eventRaws = New-Object System.Collections.Generic.List[string]
        if (-not (Test-Path -LiteralPath $resolvedEventsPath -PathType Leaf)) {
            Add-ValidationError "Event evidence not found: $resolvedEventsPath"
        } else {
            $lineNumber = 0
            foreach ($line in (Get-Content -LiteralPath $resolvedEventsPath)) {
                $lineNumber++
                if ([string]::IsNullOrWhiteSpace($line)) { continue }
                $eventRaws.Add($line) | Out-Null
                if ($line -match '(?i)"(chainOfThought|rawReasoning|privateReasoning|thoughts|secret|credential|rawPayload)"\s*:') {
                    Add-ValidationError "Event line $lineNumber contains a prohibited raw reasoning, secret, credential, or unrestricted payload field."
                }
                if (Test-Schema -Json $line -SchemaPath $effectiveEventSchemaPath -Label "Event line $lineNumber") {
                    $eventObject = $line | ConvertFrom-Json
                    foreach ($eventField in @($eventObject.PSObject.Properties.Name)) {
                        if (@($run.tracePolicy.allowedEventFields) -notcontains [string]$eventField) {
                            Add-ValidationError "Event line $lineNumber field '$eventField' is not allowed by tracePolicy.allowedEventFields."
                        }
                    }
                    $events.Add($eventObject) | Out-Null
                }
            }
        }

        $expectedSequence = 1
        $terminalSequence = $null
        $terminalTarget = $null
        $cancelSequence = $null
        $cancelledSequence = $null
        $intentByFingerprint = @{}
        $intentByActionKey = @{}
        $intentBySequence = @{}
        $outcomeByIntentSequence = @{}
        $latestObservation = $null
        $consequentialOutcomeCount = 0
        $checkpointEventAtCursor = $null
        $passingVerificationEventCount = 0
        $previousEventTimestamp = $null
        $authoritativeEventTime = $null
        $maxEventIteration = 0

        foreach ($event in $events) {
            if ([int]$event.sequence -ne $expectedSequence) {
                Add-ValidationError "Event sequence is non-monotonic at sequence $($event.sequence); expected $expectedSequence."
                $expectedSequence = [int]$event.sequence
            }
            $expectedSequence++
            if ([int]$event.iteration -gt $maxEventIteration) {
                $maxEventIteration = [int]$event.iteration
            }
            if ([string]$event.loopId -ne [string]$run.identity.loopId -or
                [string]$event.runId -ne [string]$run.identity.runId -or
                [string]$event.attemptId -ne [string]$run.identity.attemptId -or
                [string]$event.traceId -ne [string]$run.identity.traceId) {
                Add-ValidationError "Event sequence $($event.sequence) identity does not match the run."
            }
            $parsedEventRunId = [guid]::Empty
            $parsedEventAttemptId = [guid]::Empty
            $parsedEventTimestamp = [datetimeoffset]::MinValue
            $eventRunIdValid = [guid]::TryParse([string]$event.runId, [ref]$parsedEventRunId)
            $eventAttemptIdValid = [guid]::TryParse([string]$event.attemptId, [ref]$parsedEventAttemptId)
            $eventTimestampValid = [datetimeoffset]::TryParse(
                [string]$event.timestamp,
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::RoundtripKind,
                [ref]$parsedEventTimestamp
            )
            $eventIdentityAndTimeValid = $eventRunIdValid -and $eventAttemptIdValid -and $eventTimestampValid
            if (-not $eventIdentityAndTimeValid) {
                Add-ValidationError "Event sequence $($event.sequence) has an invalid UUID or timestamp."
            } else {
                if ($parsedEventTimestamp -lt $parsedStartedAt) {
                    Add-ValidationError "Event sequence $($event.sequence) timestamp predates current.startedAt."
                }
                if ($null -ne $previousEventTimestamp -and $parsedEventTimestamp -lt $previousEventTimestamp) {
                    Add-ValidationError "Event timestamps are non-monotonic at sequence $($event.sequence)."
                }
                $previousEventTimestamp = $parsedEventTimestamp
                $authoritativeEventTime = $parsedEventTimestamp
            }
            if ($null -ne $terminalSequence) {
                Add-ValidationError "A terminal state transitioned again at event sequence $($event.sequence)."
            }
            if ($null -ne $cancelSequence -and [string]$event.eventType -eq "action-intent") {
                Add-ValidationError "New work started after cancellation was requested."
            }
            if ([string]$event.eventType -eq "cancel-requested") {
                $cancelSequence = [int]$event.sequence
            }
            if ([string]$event.eventType -eq "cancelled") {
                if ($null -eq $cancelSequence) {
                    Add-ValidationError "A cancelled event requires a preceding cancel-requested event."
                } else {
                    $cancelledSequence = [int]$event.sequence
                }
            }
            if ([string]$event.eventType -eq "observation") {
                $latestObservation = $event
            }
            if ([string]$event.eventType -eq "action-intent") {
                $fp = [string]$event.actionFingerprint
                $actionKey = "$fp|$([string]$event.actionDigest)"
                if (-not $intentByFingerprint.ContainsKey($fp)) { $intentByFingerprint[$fp] = New-Object System.Collections.Generic.List[object] }
                $intentByFingerprint[$fp].Add($event) | Out-Null
                if (-not $intentByActionKey.ContainsKey($actionKey)) { $intentByActionKey[$actionKey] = New-Object System.Collections.Generic.List[object] }
                $intentByActionKey[$actionKey].Add($event) | Out-Null
                $intentBySequence[[string]$event.sequence] = $event
                if ($null -eq $latestObservation) {
                    Add-ValidationError "Consequential action intent $($event.sequence) lacks a preceding authoritative observation."
                } elseif ([int]$event.observationSequence -ne [int]$latestObservation.sequence -or
                    [int]$event.stateVersion -ne [int]$latestObservation.stateVersion) {
                    Add-ValidationError "Consequential action intent $($event.sequence) must bind to the latest preceding authoritative observation sequence and state."
                } else {
                    $age = ([datetimeoffset]$event.timestamp - [datetimeoffset]$latestObservation.timestamp).TotalSeconds
                    if ($age -lt 0 -or $age -gt [int]$run.current.observation.freshnessCeilingSeconds) {
                        Add-ValidationError "Observation age exceeds its declared freshness ceiling before action intent $($event.sequence)."
                    }
                }
                if ($run.approvalPolicy.requiredForConsequentialActions) {
                    $intentApproval = $event.approval
                    if ([string]$intentApproval.actionDigest -ne [string]$event.actionDigest -or
                        [string]$intentApproval.argumentDigest -ne [string]$event.argumentDigest -or
                        [string]$intentApproval.preconditionDigest -ne [string]$event.preconditionDigest -or
                        [int]$intentApproval.stateVersion -ne [int]$event.stateVersion) {
                        Add-ValidationError "Consequential action intent $($event.sequence) approval is not bound to that intent's action, arguments, preconditions, and state version."
                    }
                    if (@($run.approvalPolicy.authorityRefs) -notcontains [string]$intentApproval.authorityRef -or
                        @($run.authorities.approvalAuthorityRefs) -notcontains [string]$intentApproval.authorityRef) {
                        Add-ValidationError "Consequential action intent $($event.sequence) approval authority is not allowlisted."
                    }
                    $approvalAgeSeconds = ([datetimeoffset]$event.timestamp - [datetimeoffset]$intentApproval.approvedAt).TotalSeconds
                    if ($approvalAgeSeconds -lt 0) {
                        Add-ValidationError "Approval must predate the consequential action intent."
                    } elseif ($approvalAgeSeconds -gt [int]$run.approvalPolicy.maxAgeSeconds) {
                        Add-ValidationError "Approval age exceeds approvalPolicy.maxAgeSeconds before the consequential action intent."
                    }
                    if ([datetimeoffset]$intentApproval.expiresAt -le [datetimeoffset]$event.timestamp) {
                        Add-ValidationError "Approval expired before the consequential action intent."
                    }
                    if ([datetimeoffset]$intentApproval.expiresAt -le [datetimeoffset]$intentApproval.approvedAt) {
                        Add-ValidationError "Approval expiry must be later than approval time."
                    }
                }
            }
            if ([string]$event.eventType -eq "action-outcome") {
                $fp = [string]$event.actionFingerprint
                $actionKey = "$fp|$([string]$event.actionDigest)"
                $intentSequence = [string]$event.intentSequence
                if (-not $intentBySequence.ContainsKey($intentSequence)) {
                    Add-ValidationError "Action outcome $($event.sequence) must bind to an exact preceding intent sequence."
                } else {
                    $matchedIntent = $intentBySequence[$intentSequence]
                    if ([int]$matchedIntent.sequence -ge [int]$event.sequence -or
                        [string]$matchedIntent.actionFingerprint -ne $fp -or
                        [string]$matchedIntent.actionDigest -ne [string]$event.actionDigest) {
                        Add-ValidationError "Action outcome $($event.sequence) does not match its declared intent sequence fingerprint and action digest."
                    } elseif ($outcomeByIntentSequence.ContainsKey($intentSequence)) {
                        Add-ValidationError "Action intent $intentSequence has more than one outcome."
                    } else {
                        $outcomeByIntentSequence[$intentSequence] = $event
                    }
                }
                $consequentialOutcomeCount++
            }
            if ([string]$event.eventType -eq "verification") {
                if ([string]$event.checkerDigest -ne [string]$run.verificationPolicy.checkerDigest) {
                    Add-ValidationError "Checker commands or fixtures changed after the run began."
                }
                if ([bool]$event.verificationPassed -and
                    [bool]$event.postconditionsPassed -and
                    [string]$event.resultRef -eq [string]$run.current.verification.evidenceRef -and
                    $event.PSObject.Properties.Name -contains "resultDigest") {
                    $passingVerificationEventCount++
                }
            }
            if ([string]$event.eventType -eq "checkpoint" -and [int]$event.sequence -eq [int]$run.checkpoint.lastEventSequence) {
                $checkpointEventAtCursor = $event
            }
            if ([string]$event.eventType -eq "transition" -and [string]$event.transition.to -ne "running") {
                $terminalSequence = [int]$event.sequence
                $terminalTarget = [string]$event.transition.to
            }
        }

        $lastSequence = if ($events.Count -gt 0) { [int]$events[$events.Count - 1].sequence } else { 0 }
        $eventIterationBudgetExceeded = $maxEventIteration -gt [int]$run.budgets.maxIterations
        if ([int]$run.current.iteration -ne $maxEventIteration) {
            Add-ValidationError "current.iteration does not reconcile with effective iteration usage derived from append-only event history."
        }
        if ($eventIterationBudgetExceeded) {
            $stateBudgetExceeded = $true
            if ([string]$run.current.status -ne "budget-exhausted" -or -not [bool]$run.current.budgetExhausted) {
                Add-ValidationError "Exhausted usage requires budget-exhausted terminal semantics and current.budgetExhausted=true."
            }
            if ([string]$run.current.status -eq "success") {
                Add-ValidationError "Budget or iteration exhaustion cannot be represented as success."
            }
        }
        $wallClockBudgetExceeded = $false
        if ($null -ne $authoritativeEventTime) {
            $elapsedSeconds = ($authoritativeEventTime - $parsedStartedAt).TotalSeconds
            $wallClockBudgetExceeded = $elapsedSeconds -gt [int]$run.budgets.maxWallClockSeconds
            if ($wallClockBudgetExceeded) {
                if ([string]$run.current.status -ne "budget-exhausted" -or -not [bool]$run.current.budgetExhausted) {
                    Add-ValidationError "Exhausted usage requires budget-exhausted terminal semantics and current.budgetExhausted=true."
                }
                if ([string]$run.current.status -eq "success") {
                    Add-ValidationError "Budget or iteration exhaustion cannot be represented as success."
                }
            }
        }
        if ($null -eq $checkpointEventAtCursor -or
            [string]$checkpointEventAtCursor.checkpointDigest -ne [string]$run.checkpoint.digest -or
            [int]$checkpointEventAtCursor.stateVersion -ne [int]$run.checkpoint.expectedStateVersion -or
            [int]$checkpointEventAtCursor.iteration -ne [int]$run.checkpoint.iteration) {
            Add-ValidationError "Checkpoint cursor is not bound to its checkpoint event, canonical digest, expected state version, and iteration."
        }
        foreach ($actionKey in $intentByActionKey.Keys) {
            $intentCount = $intentByActionKey[$actionKey].Count
            $unmatchedIntents = @($intentByActionKey[$actionKey] | Where-Object { -not $outcomeByIntentSequence.ContainsKey([string]$_.sequence) })
            $outcomeCount = $intentCount - $unmatchedIntents.Count
            if ($intentCount -gt 1 -and $outcomeCount -lt $intentCount -and -not $run.current.ambiguityResolved) {
                Add-ValidationError "An unresolved action intent was retried instead of classified ambiguous-side-effect."
            }
            if ($Phase -eq "Terminal" -and $outcomeCount -lt $intentCount -and -not $run.current.ambiguityResolved) {
                Add-ValidationError "Terminal run has an unresolved ambiguous side effect."
            }
            if ($outcomeCount -lt $intentCount -and $run.current.ambiguityResolved) {
                $unmatchedSequences = @($unmatchedIntents | ForEach-Object { [string]$_.sequence }) -join ","
                Add-ValidationError "Ambiguity resolution requires reconciliation evidence bound to each unresolved intent sequence; unmatched: $unmatchedSequences."
            }
        }
        foreach ($fp in $intentByFingerprint.Keys) {
            if ($intentByFingerprint[$fp].Count -gt 1) {
                $sameState = @($intentByFingerprint[$fp] | Select-Object -ExpandProperty stateVersion -Unique).Count -eq 1
                if ($sameState -and [int]$run.current.stagnantIterations -eq 0) {
                    Add-ValidationError "The same action fingerprint repeated against the same state without a declared attempt delta."
                }
            }
        }
        $sideEffectBudgetExceeded = [bool]$run.budgets.sideEffectCeiling.enabled -and
            $consequentialOutcomeCount -gt [int]$run.budgets.sideEffectCeiling.maxConsequentialActions
        if ($sideEffectBudgetExceeded -and
            ([string]$run.current.status -ne "budget-exhausted" -or -not [bool]$run.current.budgetExhausted)) {
            Add-ValidationError "Consequential action count exceeds the side-effect ceiling."
            Add-ValidationError "Exhausted usage requires budget-exhausted terminal semantics and current.budgetExhausted=true."
        }
        if (($stateBudgetExceeded -or $wallClockBudgetExceeded -or $sideEffectBudgetExceeded) -eq $false -and
            ([string]$run.current.status -eq "budget-exhausted" -or [bool]$run.current.budgetExhausted)) {
            Add-ValidationError "budget-exhausted terminal semantics require actual usage beyond a declared budget."
        }
        if ($effectiveConsequential) {
            $matchingIntent = @($events | Where-Object {
                $_.eventType -eq "action-intent" -and
                [int]$_.sequence -eq [int]$action.intentSequence -and
                [string]$_.actionFingerprint -eq [string]$action.fingerprint -and
                [string]$_.actionDigest -eq [string]$action.actionDigest
            })
            if ($matchingIntent.Count -ne 1) {
                Add-ValidationError "A consequential action lacks its exact persisted action-intent event."
            } elseif ($run.approvalPolicy.requiredForConsequentialActions) {
                $persistedApprovalJson = $matchingIntent[0].approval | ConvertTo-Json -Depth 10 -Compress
                $currentApprovalJson = $action.approval | ConvertTo-Json -Depth 10 -Compress
                if ($persistedApprovalJson -cne $currentApprovalJson) {
                    Add-ValidationError "The current consequential action approval does not match its persisted action-intent approval evidence."
                }
            }
            if ($action.PSObject.Properties.Name -contains "outcomeSequence") {
                $matchingOutcome = @($events | Where-Object {
                    $_.eventType -eq "action-outcome" -and
                    [int]$_.sequence -eq [int]$action.outcomeSequence -and
                    [int]$_.intentSequence -eq [int]$action.intentSequence -and
                    [string]$_.actionFingerprint -eq [string]$action.fingerprint -and
                    [string]$_.actionDigest -eq [string]$action.actionDigest
                })
                if ($matchingOutcome.Count -ne 1) {
                    Add-ValidationError "A consequential action outcome is not bound to its exact persisted intent sequence."
                }
            }
        }
        $matchingObservation = @($events | Where-Object {
            $_.eventType -eq "observation" -and
            [int]$_.stateVersion -eq [int]$run.current.observation.stateVersion -and
            [string]$_.observationRef -eq [string]$run.current.observation.ref -and
            [string]$_.observationDigest -eq [string]$run.current.observation.digest
        })
        if ($matchingObservation.Count -eq 0) {
            Add-ValidationError "Current observation does not match append-only evidence for its state version."
        }
        $cancellationHandled = $null -ne $cancelSequence -and
            $null -ne $cancelledSequence -and
            [string]$terminalTarget -eq "cancelled" -and
            [string]$run.current.status -eq "cancelled"
        if ($run.current.cancellation.requested -and $null -eq $cancelSequence) {
            Add-ValidationError "Current cancellation state is not backed by append-only cancel-requested evidence."
        }
        if ($null -ne $cancelSequence -and -not $run.current.cancellation.requested -and -not $cancellationHandled) {
            Add-ValidationError "Append-only cancel-requested evidence is not reconciled with current cancellation state."
        }
        if ($run.current.cancellation.requested) {
            if (-not $run.current.cancellation.propagateToChildren -or [int]$run.current.cancellation.childrenPending -gt 0) {
                Add-ValidationError "Cancellation was requested but child cancellation was not fully propagated."
            }
        }

        if ($Phase -eq "Terminal") {
            if ([string]$run.current.status -eq "running") {
                Add-ValidationError "Terminal validation requires a terminal status."
            }
            if ([string]$run.current.transition.to -ne [string]$run.current.status) {
                Add-ValidationError "Current transition target does not match terminal status."
            }
            if ($null -eq $terminalSequence) {
                Add-ValidationError "Terminal validation requires a matching terminal transition event."
            } elseif ([string]$terminalTarget -ne [string]$run.current.status) {
                Add-ValidationError "Terminal status does not match the terminal transition event."
            }
            if ([string]$run.current.status -eq "success") {
                if (-not $run.current.verification.passed -or -not $run.current.verification.postconditionsPassed) {
                    Add-ValidationError "Success requires a passing authoritative check and passing postconditions."
                }
                if ($passingVerificationEventCount -eq 0) {
                    Add-ValidationError "Success requires a matching passing verification event and evidence reference."
                }
                if ($null -ne $cancelSequence -or $run.current.cancellation.requested -or $run.current.budgetExhausted -or $run.current.ambiguityResolved -eq $false) {
                    Add-ValidationError "Success cannot coexist with cancellation, exhausted budget, or unresolved ambiguity."
                }
            }
        }
    }
}

if ($errors.Count -gt 0) {
    $ErrorActionPreference = "Continue"
    $seen = New-Object System.Collections.Generic.HashSet[string]
    foreach ($message in $errors) {
        if ($seen.Add($message)) {
            Write-Error $message
        }
    }
    exit 1
}

Write-Output "Agent loop run validated ($Phase): $resolvedRunPath"
exit 0
