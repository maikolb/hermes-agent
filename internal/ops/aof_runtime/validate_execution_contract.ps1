param(
    [string]$WorkspaceRoot = (Get-Location).Path,
    [string]$ContractPath,
    [switch]$AllowLegacyContractPath,
    [switch]$RequireCurrentVersion,
    [switch]$RequireCanonicalPath,
    [ValidateSet("Preflight", "Final")]
    [string]$Phase = "Preflight"
)

$runtimeCommonPath = Join-Path $PSScriptRoot "agent_loop_runtime_common.ps1"
if (-not (Test-Path -LiteralPath $runtimeCommonPath -PathType Leaf)) {
    Write-Error "Execution-contract validator requires the shared artifact hashing helper: $runtimeCommonPath"
    exit 1
}
. $runtimeCommonPath

function Resolve-ContractPath {
    param(
        [string]$Root,
        [string]$ExplicitPath,
        [switch]$AllowLegacy
    )

    $resolvedRoot = [System.IO.Path]::GetFullPath($Root)
    $canonical = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot "docs\EXECUTION_CONTRACT.md"))
    $legacyCandidates = @(
        [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot "internal\ops\EXECUTION_CONTRACT.md")),
        [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot ".codex\EXECUTION_CONTRACT.md")),
        [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot "docs\CODEX_EXECUTION_CONTRACT.md")),
        [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot ".codex\CODEX_EXECUTION_CONTRACT.md"))
    )
    $authorityCandidates = @($canonical) + $legacyCandidates
    $activeAuthorities = @($authorityCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    $pathComparison = if ($env:OS -eq "Windows_NT") {
        [System.StringComparison]::OrdinalIgnoreCase
    } else {
        [System.StringComparison]::Ordinal
    }

    if ($ExplicitPath) {
        $resolvedExplicit = if ([System.IO.Path]::IsPathRooted($ExplicitPath)) {
            [System.IO.Path]::GetFullPath($ExplicitPath)
        } else {
            [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot $ExplicitPath))
        }
        $isKnownAuthority = @($authorityCandidates | Where-Object {
            [string]::Equals($_, $resolvedExplicit, $pathComparison)
        }).Count -gt 0
        if ($isKnownAuthority -and $activeAuthorities.Count -gt 1) {
            Write-Error "Multiple active execution-contract authorities found: $($activeAuthorities -join ', '). Keep only docs\EXECUTION_CONTRACT.md active."
            return $null
        }
        $isLegacyAuthority = @($legacyCandidates | Where-Object {
            [string]::Equals($_, $resolvedExplicit, $pathComparison)
        }).Count -gt 0
        if ($isLegacyAuthority -and -not $AllowLegacy) {
            Write-Error "Legacy execution-contract path requires -AllowLegacyContractPath: $resolvedExplicit"
            return $null
        }
        return $resolvedExplicit
    }

    if ($activeAuthorities.Count -gt 1) {
        Write-Error "Multiple active execution-contract authorities found: $($activeAuthorities -join ', '). Keep only docs\EXECUTION_CONTRACT.md active."
        return $null
    }
    if (Test-Path -LiteralPath $canonical -PathType Leaf) {
        return $canonical
    }
    if ($activeAuthorities.Count -eq 1) {
        if (-not $AllowLegacy) {
            Write-Error "Canonical execution contract is missing at $canonical. A legacy authority exists at $($activeAuthorities[0]); pass -AllowLegacyContractPath only for an intentional migration window."
            return $null
        }
        return $activeAuthorities[0]
    }
    return $canonical
}

function Get-SectionBody {
    param(
        [string]$Content,
        [string]$Heading
    )

    $pattern = "(?ms)^" + [regex]::Escape($Heading) + "\r?\n(.*?)(?=^## |\z)"
    $match = [regex]::Match($Content, $pattern)
    if (-not $match.Success) {
        return $null
    }
    return $match.Groups[1].Value.Trim()
}

function Test-GuideOrPlaceholderLine {
    # Returns $true when the line carries no real user content: a leftover
    # template guide sentinel, a known placeholder token, an empty bracket
    # stub, or a bare "- Key:" label whose value was never filled in.
    param([string]$Line)

    $nonContentPatterns = @(
        # Template guide sentinels (invisible in rendered markdown).
        '^\s*<!--\s*guide:.*-->\s*$',
        # Placeholder tokens. NOTE: bare 'pending' is matched here, but the
        # '## Status' defaults are 'Key: pending' (value present), so they read
        # as real content via the bare-label rule below. Do not tighten Status
        # parsing to treat 'Key: pending' as a placeholder without revisiting this.
        '^\s*[-*]?\s*(TBD|TODO|pending|fill before implementation\.?|n/a)\s*$',
        '^\s*[-*]\s*[xX]\s*$',
        # Pure angle/bracket stubs.
        '^\s*<.*>\s*$',
        '^\s*\[.*\]\s*$',
        # Bare "- Label:" / "Label:" with nothing after the colon. A trailing-colon
        # lead-in (e.g. "- Steps to reproduce:") also matches here and is treated as
        # non-content, which is harmless: such lines are always followed by real
        # content lines, so Test-MeaningfulBody still passes the section on those.
        '^\s*[-*]?\s*\S[^:]*:\s*$'
    )

    foreach ($pattern in $nonContentPatterns) {
        if ($Line -match $pattern) {
            return $true
        }
    }
    return $false
}

function Test-MeaningfulBody {
    # A section is meaningful only when it contains at least one line of real
    # user content: not blank, not a leftover template guide, not a placeholder
    # token, and not an unfilled "- Key:" label.
    param([string]$Body)
    if ([string]::IsNullOrWhiteSpace($Body)) {
        return $false
    }

    $lines = @($Body -split "\r?\n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lines.Count -eq 0) {
        return $false
    }

    foreach ($line in $lines) {
        if (-not (Test-GuideOrPlaceholderLine -Line $line)) {
            return $true
        }
    }

    return $false
}

$resolvedPath = Resolve-ContractPath -Root $WorkspaceRoot -ExplicitPath $ContractPath -AllowLegacy:$AllowLegacyContractPath
if ([string]::IsNullOrWhiteSpace($resolvedPath)) {
    exit 1
}
$resolvedRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)
$resolvedPath = [System.IO.Path]::GetFullPath($resolvedPath)
$pathComparison = if ($env:OS -eq "Windows_NT") {
    [System.StringComparison]::OrdinalIgnoreCase
} else {
    [System.StringComparison]::Ordinal
}
$rootPrefix = $resolvedRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedPath.StartsWith($rootPrefix, $pathComparison)) {
    Write-Error "Execution contract must stay inside WorkspaceRoot. Workspace='$resolvedRoot' Contract='$resolvedPath'"
    exit 1
}
$canonicalPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot "docs\EXECUTION_CONTRACT.md"))
if ($RequireCanonicalPath -and -not [string]::Equals($resolvedPath, $canonicalPath, $pathComparison)) {
    Write-Error "Canonical execution contract required: $canonicalPath"
    exit 1
}
if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
    Write-Error "Execution contract not found. Expected: $resolvedPath"
    exit 1
}

$content = Get-Content -LiteralPath $resolvedPath -Raw
$requiredHeadings = @(
    "## Contract Metadata",
    "## Requested Outcome",
    "## In Scope",
    "## Out of Scope",
    "## Failure Signal / Repro",
    "## Root-Cause Hypothesis",
    "## Claim Discipline",
    "## Forbidden Actions",
    "## Validation Plan",
    "## Status"
)

# Sections whose body must carry real user content (not just structure) for the
# contract to mean anything. These are surfaced explicitly in the success line.
$criticalHeadings = @(
    "## In Scope",
    "## Out of Scope",
    "## Root-Cause Hypothesis",
    "## Validation Plan"
)

$errors = New-Object System.Collections.Generic.List[string]

foreach ($heading in $requiredHeadings) {
    $body = Get-SectionBody -Content $content -Heading $heading
    if ($null -eq $body) {
        $errors.Add("Missing required section: $heading")
        continue
    }
    if (-not (Test-MeaningfulBody -Body $body)) {
        if ($criticalHeadings -contains $heading) {
            $errors.Add("Critical section still holds only template guide/placeholder text (needs at least one line of real content): $heading")
        } else {
            $errors.Add("Section has no meaningful content: $heading")
        }
    }
}

$metadata = Get-SectionBody -Content $content -Heading "## Contract Metadata"
$mode = $null
if ($metadata -and $metadata -match '(?mi)^- Mode:\s*(.+)$') {
    $mode = $matches[1].Trim()
}
$riskLevel = $null
if ($metadata -and $metadata -match '(?mi)^- Risk Level:\s*(.+)$') {
    $riskLevel = $matches[1].Trim()
}

$contractVersion = 1
if ($metadata -and $metadata -match '(?mi)^- Contract Version:\s*(.*?)\s*$') {
    $explicitContractVersion = $matches[1].Trim()
    $parsedContractVersion = 0
    if ($explicitContractVersion -notmatch '^\d+$' -or
        -not [int]::TryParse($explicitContractVersion, [ref]$parsedContractVersion) -or
        $parsedContractVersion -lt 1) {
        $errors.Add("Explicit Contract Version must be a positive integer; found '$explicitContractVersion'.")
    } else {
        $contractVersion = $parsedContractVersion
    }
}

if ([string]::IsNullOrWhiteSpace($mode)) {
    $errors.Add("Contract metadata must include a non-empty '- Mode:' field.")
} elseif ($mode.ToUpperInvariant() -notin @("IMPLEMENT", "REPAIR", "MIGRATE", "RELEASE_LOCK", "RELEASE", "REVIEW_ONLY", "RESTORE_ONLY", "RESTORE_THEN_VERIFY")) {
    $errors.Add("Contract Mode must be one of IMPLEMENT, REPAIR, MIGRATE, RELEASE_LOCK, REVIEW_ONLY, RESTORE_ONLY, or RESTORE_THEN_VERIFY (legacy RELEASE is also accepted); found '$mode'.")
}
if ([string]::IsNullOrWhiteSpace($riskLevel)) {
    $errors.Add("Contract metadata must include a non-empty '- Risk Level:' field.")
} elseif ($riskLevel.ToUpperInvariant() -notin @("LOW", "MEDIUM", "HIGH", "CRITICAL")) {
    $errors.Add("Risk Level must be LOW, MEDIUM, HIGH, or CRITICAL; found '$riskLevel'.")
}

if ($Phase -eq "Final" -and $contractVersion -ne 3) {
    $errors.Add("Final validation requires current execution contract version 3; legacy version $contractVersion cannot complete a delivery.")
} elseif ($RequireCurrentVersion -and $contractVersion -ne 3) {
    $errors.Add("Current execution contract version 3 is required; legacy version $contractVersion is not evidence-complete.")
}

if ($contractVersion -in @(2, 3)) {
    foreach ($currentHeading in @("## Acceptance Criteria", "## Loop Control")) {
        $currentBody = Get-SectionBody -Content $content -Heading $currentHeading
        if ($null -eq $currentBody) {
            $errors.Add("Version 2 contract is missing required section: $currentHeading")
        } elseif (-not (Test-MeaningfulBody -Body $currentBody)) {
            $errors.Add("Version 2 section has no meaningful content: $currentHeading")
        }
    }

    $loopRunRef = $null
    if ($metadata -match '(?mi)^- Machine Runtime Authority:\s*(.+)$') {
        $loopRunRef = $matches[1].Trim().Replace(([char]96).ToString(), "")
    }
    if ([string]::IsNullOrWhiteSpace($loopRunRef) -or $loopRunRef -match '(?i)(\{\{|FILL_|TBD|TODO)') {
        $errors.Add("Version 2 metadata must declare a real Machine Runtime Authority path or explicit 'none: <reason>'.")
    } elseif ($loopRunRef -match '^(?i)none\s*$') {
        $errors.Add("Machine Runtime Authority opt-out requires 'none: <authored reason>'; bare none is not sufficient.")
    } elseif ($loopRunRef -match '^(?i)none\s*:\s*(.*)$') {
        $metadataOptOutReason = $matches[1].Trim()
        if ([string]::IsNullOrWhiteSpace($metadataOptOutReason) -or
            (Test-GuideOrPlaceholderLine -Line $metadataOptOutReason) -or
            $metadataOptOutReason -match '^(?i)(none|not required|lower-risk)$') {
            $errors.Add("Machine Runtime Authority opt-out requires an authored reason after 'none:'.")
        }
        $loopBody = Get-SectionBody -Content $content -Heading "## Loop Control"
        $authoredLoopOptOut = $false
        foreach ($loopLine in @($loopBody -split "\r?\n")) {
            if ([string]::IsNullOrWhiteSpace($loopLine) -or (Test-GuideOrPlaceholderLine -Line $loopLine)) {
                continue
            }
            $normalizedLoopLine = ($loopLine -replace '^\s*[-*]\s*', '').Trim()
            if ($normalizedLoopLine -match '(?i)(not required|does not qualify|lower-risk)' -and
                $normalizedLoopLine -match '(?i)(because|due to|since|as (?:a|this|the)|:\s*\S)') {
                $authoredLoopOptOut = $true
                break
            }
        }
        if (-not $authoredLoopOptOut) {
            $errors.Add("Machine Runtime Authority may be none only when an authored Loop Control reason explains why the controlled micro-loop is not required.")
        }
    } else {
        $loopRunPath = $loopRunRef
        if (-not [System.IO.Path]::IsPathRooted($loopRunPath)) {
            $loopRunPath = Join-Path $WorkspaceRoot $loopRunPath
        }
        if (-not (Test-Path -LiteralPath $loopRunPath -PathType Leaf)) {
            $errors.Add("Declared Machine Runtime Authority does not exist: $loopRunRef")
        } else {
            $loopValidatorPath = Join-Path $PSScriptRoot "validate_agent_loop_run.ps1"
            if (-not (Test-Path -LiteralPath $loopValidatorPath -PathType Leaf)) {
                $errors.Add("Agent loop run validator is missing: $loopValidatorPath")
            } else {
                $loopOutput = & $loopValidatorPath -WorkspaceRoot $WorkspaceRoot -RunPath $loopRunPath -Phase Definition 2>&1 | Out-String
                $loopExit = $LASTEXITCODE
                if ($loopExit -ne 0) {
                    $errors.Add("Declared Machine Runtime Authority failed Definition validation: $($loopOutput.Trim())")
                }
            }
        }
    }
} elseif ($contractVersion -gt 3) {
    $errors.Add("Unsupported execution contract version: $contractVersion")
}

$acceptanceCriteria = [ordered]@{}
$validationEvidenceBody = $null
if ($contractVersion -eq 3) {
    $acceptanceBody = Get-SectionBody -Content $content -Heading "## Acceptance Criteria"
    if (-not [string]::IsNullOrWhiteSpace($acceptanceBody)) {
        foreach ($criterionLine in @($acceptanceBody -split "\r?\n")) {
            if ([string]::IsNullOrWhiteSpace($criterionLine) -or $criterionLine -match '^\s*<!--') {
                continue
            }
            if ($criterionLine -notmatch '^\s*-\s*(AC-\d{3,})\s*:\s*(\S.*)$') {
                $errors.Add("Version 3 acceptance criteria must use one stable '- AC-NNN: <observable criterion>' entry per line; found '$($criterionLine.Trim())'.")
                continue
            }
            $criterionId = $matches[1].ToUpperInvariant()
            $criterionText = ($matches[2] -replace '\s+', ' ').Trim()
            if ($acceptanceCriteria.Contains($criterionId)) {
                $errors.Add("Version 3 acceptance criterion ID is duplicated: $criterionId")
                continue
            }
            $acceptanceCriteria[$criterionId] = $criterionText
        }
    }
    if ($acceptanceCriteria.Count -eq 0) {
        $errors.Add("Version 3 contract requires at least one stable acceptance criterion ID in '## Acceptance Criteria'.")
    }

    $validationEvidenceBody = Get-SectionBody -Content $content -Heading "## Validation Evidence"
    if ($null -eq $validationEvidenceBody) {
        $errors.Add("Version 3 contract is missing required section: ## Validation Evidence")
    } elseif (-not (Test-MeaningfulBody -Body $validationEvidenceBody)) {
        $errors.Add("Version 3 section has no meaningful content: ## Validation Evidence")
    }
}

# Default Forbidden Actions boilerplate shipped in the starter template. These
# lines are mandatory safety rails, but on their own they are not evidence that
# the author thought about what is off-limits for *this* task. Comparison is
# whitespace-normalized to stay robust against reflowing/indentation.
$forbiddenDefaults = @(
    'No scope expansion beyond the requested outcome.',
    'No hidden side effects.',
    'No behavior changes outside the declared scope.',
    'No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.',
    'If mode is `RESTORE_ONLY` or `RESTORE_THEN_VERIFY`, no new implementation paths, experiments, or refactors.'
) | ForEach-Object { ($_ -replace '\s+', ' ').Trim() }

$forbiddenActions = Get-SectionBody -Content $content -Heading "## Forbidden Actions"
if ($mode -match '^RESTORE') {
    # Restore modes must still forbid placeholders/fake values/temporary keys/config overrides.
    if ($forbiddenActions -notmatch '(?i)placeholder|fake|override|temporary key|config') {
        $errors.Add("Restore modes must explicitly forbid placeholders, fake values, temporary keys, and config overrides.")
    }

    # ...and must name at least one task-specific forbidden action beyond the
    # shipped boilerplate, so the default template text alone cannot satisfy this gate.
    $authoredForbidden = $false
    if (-not [string]::IsNullOrWhiteSpace($forbiddenActions)) {
        $forbiddenLines = $forbiddenActions -split "\r?\n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        foreach ($line in $forbiddenLines) {
            if (Test-GuideOrPlaceholderLine -Line $line) {
                continue
            }
            $normalized = (($line -replace '^\s*[-*]\s*', '') -replace '\s+', ' ').Trim()
            if ($forbiddenDefaults -notcontains $normalized) {
                $authoredForbidden = $true
                break
            }
        }
    }
    if (-not $authoredForbidden) {
        $errors.Add("Restore modes must list at least one task-specific forbidden action beyond the default template boilerplate in '## Forbidden Actions'.")
    }
}

# REPAIR-mode causal-evidence gate (REG-2026-08-22-001): a repair contract must
# anchor its failure signal to at least one verifiable evidence artifact -- a
# backtick-quoted path that resolves to an existing file (log excerpt, saved
# command output, captured event) -- or carry an authored
# '- Evidence-Absent: <reason>' line stating why no artifact can exist yet.
# A code path that could produce the symptom is a hypothesis, not causal
# evidence; this gate makes that distinction mechanical instead of textual.
if ($mode -and $mode.ToUpperInvariant() -eq "REPAIR") {
    $failureSignal = Get-SectionBody -Content $content -Heading "## Failure Signal / Repro"
    $hasRepairEvidence = $false
    if (-not [string]::IsNullOrWhiteSpace($failureSignal)) {
        foreach ($span in [regex]::Matches($failureSignal, '`([^`]+)`')) {
            $candidate = $span.Groups[1].Value.Trim()
            if ([string]::IsNullOrWhiteSpace($candidate) -or $candidate -match '\s') { continue }
            $candidate = ($candidate -split '#')[0].TrimEnd(':', ',', ';')
            if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
            $looksLikePath = ($candidate -match '[\\/]') -or ($candidate -match '^[A-Za-z0-9_.-]+\.[A-Za-z0-9]+$')
            if (-not $looksLikePath) { continue }
            $resolvedEvidence = $candidate
            if (-not [System.IO.Path]::IsPathRooted($resolvedEvidence)) {
                $resolvedEvidence = Join-Path $resolvedRoot $resolvedEvidence
            }
            if (Test-Path -LiteralPath $resolvedEvidence -PathType Leaf) {
                $hasRepairEvidence = $true
                break
            }
        }
        if (-not $hasRepairEvidence) {
            foreach ($signalLine in @($failureSignal -split "\r?\n")) {
                if ($signalLine -match '(?i)^\s*[-*]?\s*Evidence-Absent:\s*(\S.*)$') {
                    $absenceReason = $matches[1].Trim()
                    if (-not (Test-GuideOrPlaceholderLine -Line $absenceReason) -and
                        $absenceReason -notmatch '(?i)^(tbd|todo|pending|n/a|none)\.?$') {
                        $hasRepairEvidence = $true
                        break
                    }
                }
            }
        }
    }
    if (-not $hasRepairEvidence) {
        $errors.Add("REPAIR mode requires at least one verifiable evidence artifact in '## Failure Signal / Repro': a backtick-quoted path to an existing file (log excerpt, saved output, captured event), or an authored '- Evidence-Absent: <reason>' line.")
    }
}

if ($Phase -eq "Final") {
    $statusBody = Get-SectionBody -Content $content -Heading "## Status"
    $requiredStatusKeys = @("Contract preflight", "Implementation", "Validation", "Completion")
    $statusValues = @{}
    foreach ($statusLine in @($statusBody -split "\r?\n")) {
        if ($statusLine -match '^\s*[-*]\s*([^:]+):\s*(.*?)\s*$') {
            $statusValues[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    foreach ($statusKey in $requiredStatusKeys) {
        if (-not $statusValues.ContainsKey($statusKey)) {
            $errors.Add("Final contract status is missing '$statusKey'.")
            continue
        }
        $statusValue = [string]$statusValues[$statusKey]
        if ([string]::IsNullOrWhiteSpace($statusValue) -or
            $statusValue -match '(?i)^(pending|tbd|todo|not started|in progress|fill before implementation)\b') {
            $errors.Add("Final contract status '$statusKey' is not complete: '$statusValue'.")
        }
    }
    if ($statusValues.ContainsKey("Completion") -and
        ([string]$statusValues["Completion"]) -notmatch '(?i)\b(complete|completed|accepted|blocked-with-evidence)\b') {
        $errors.Add("Final contract Completion must explicitly state complete/completed/accepted or blocked-with-evidence.")
    }

    if ($contractVersion -eq 3 -and $null -ne $validationEvidenceBody) {
        $evidenceMatch = [regex]::Match($validationEvidenceBody, '(?ms)```json\s*(\{.*?\})\s*```')
        $evidenceDocument = $null
        if (-not $evidenceMatch.Success) {
            $errors.Add("Version 3 Final validation requires one JSON object fenced as ```json in '## Validation Evidence'.")
        } else {
            try {
                $evidenceDocument = $evidenceMatch.Groups[1].Value | ConvertFrom-Json -ErrorAction Stop
            } catch {
                $errors.Add("Version 3 Validation Evidence JSON is invalid: $($_.Exception.Message)")
            }
        }

        $evidenceStatuses = @{}
        if ($null -ne $evidenceDocument) {
            if ($null -eq $evidenceDocument.schemaVersion -or [int]$evidenceDocument.schemaVersion -ne 1) {
                $errors.Add("Validation Evidence schemaVersion must be 1.")
            }
            $checks = @($evidenceDocument.checks)
            if ($checks.Count -eq 0) {
                $errors.Add("Validation Evidence must contain one check for every acceptance criterion.")
            }
            foreach ($check in $checks) {
                $criterionId = ([string]$check.criterionId).Trim().ToUpperInvariant()
                if ([string]::IsNullOrWhiteSpace($criterionId)) {
                    $errors.Add("Validation Evidence check is missing criterionId.")
                    continue
                }
                if ($evidenceStatuses.ContainsKey($criterionId)) {
                    $errors.Add("Validation Evidence contains duplicate check for $criterionId.")
                    continue
                }
                if (-not $acceptanceCriteria.Contains($criterionId)) {
                    $errors.Add("Validation Evidence contains extra criterion not declared by the contract: $criterionId.")
                }

                $status = ([string]$check.status).Trim().ToLowerInvariant()
                $evidenceStatuses[$criterionId] = $status
                if ($status -notin @('passed', 'blocked')) {
                    $errors.Add("Validation Evidence status for $criterionId must be passed or blocked; found '$status'.")
                }
                if (([string]$check.performedBy).Trim().ToLowerInvariant() -ne 'agent') {
                    $errors.Add("Validation Evidence for $criterionId must be performed by the agent before delivery; user-first validation is not accepted.")
                }
                if (([string]$check.verificationMode).Trim().ToLowerInvariant() -ne 'direct') {
                    $errors.Add("Validation Evidence for $criterionId must use verificationMode 'direct'; proxy or inferred checks cannot close an acceptance criterion.")
                }
                foreach ($fieldName in @('method', 'target', 'procedure', 'expected', 'observed', 'performedAtUtc')) {
                    $fieldValue = [string]$check.$fieldName
                    if ([string]::IsNullOrWhiteSpace($fieldValue) -or (Test-GuideOrPlaceholderLine -Line $fieldValue)) {
                        $errors.Add("Validation Evidence for $criterionId requires a non-placeholder '$fieldName' value.")
                    }
                }
                $performedAtValid = $true
                try {
                    [datetimeoffset]::Parse([string]$check.performedAtUtc, [System.Globalization.CultureInfo]::InvariantCulture) | Out-Null
                } catch {
                    $performedAtValid = $false
                }
                if (-not $performedAtValid) {
                    $errors.Add("Validation Evidence performedAtUtc for $criterionId must be a valid timestamp.")
                }

                $artifacts = @($check.artifacts)
                if ($artifacts.Count -eq 0) {
                    $errors.Add("Validation Evidence for $criterionId requires at least one hash-bound artifact.")
                    continue
                }
                foreach ($artifact in $artifacts) {
                    $artifactRef = ([string]$artifact.path).Trim().Replace(([char]96).ToString(), '')
                    $declaredSha = ([string]$artifact.sha256).Trim().ToLowerInvariant()
                    if ([string]::IsNullOrWhiteSpace($artifactRef)) {
                        $errors.Add("Validation Evidence artifact for $criterionId is missing path.")
                        continue
                    }
                    if ($declaredSha -notmatch '^[a-f0-9]{64}$') {
                        $errors.Add("Validation Evidence artifact for $criterionId requires a lowercase or uppercase 64-character SHA-256.")
                        continue
                    }
                    $artifactPath = if ([System.IO.Path]::IsPathRooted($artifactRef)) {
                        [System.IO.Path]::GetFullPath($artifactRef)
                    } else {
                        [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot $artifactRef))
                    }
                    if (-not $artifactPath.StartsWith($rootPrefix, $pathComparison)) {
                        $errors.Add("Validation Evidence artifact for $criterionId must stay inside WorkspaceRoot: $artifactRef")
                        continue
                    }
                    if ([string]::Equals($artifactPath, $resolvedPath, $pathComparison)) {
                        $errors.Add("Validation Evidence for $criterionId cannot cite the execution contract itself as proof.")
                        continue
                    }
                    if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf)) {
                        $errors.Add("Validation Evidence artifact for $criterionId does not exist: $artifactRef")
                        continue
                    }
                    $actualSha = Get-AofArtifactSha256 -Path $artifactPath
                    if ($actualSha -cne $declaredSha) {
                        $errors.Add("Validation Evidence artifact hash mismatch for ${criterionId}: $artifactRef")
                    }
                }
            }

            foreach ($criterionId in $acceptanceCriteria.Keys) {
                if (-not $evidenceStatuses.ContainsKey($criterionId)) {
                    $errors.Add("Validation Evidence is missing acceptance criterion: $criterionId.")
                }
            }

            if ($statusValues.ContainsKey("Completion")) {
                $completion = [string]$statusValues["Completion"]
                $blockedIds = @($evidenceStatuses.Keys | Where-Object { $evidenceStatuses[$_] -eq 'blocked' })
                $nonPassingIds = @($evidenceStatuses.Keys | Where-Object { $evidenceStatuses[$_] -ne 'passed' })
                if ($completion -match '(?i)\b(complete|completed|accepted)\b' -and $nonPassingIds.Count -gt 0) {
                    $errors.Add("Final completion is forbidden while validation evidence is not passed for: $($nonPassingIds -join ', ').")
                }
                if ($completion -match '(?i)\bblocked-with-evidence\b' -and $blockedIds.Count -eq 0) {
                    $errors.Add("Completion says blocked-with-evidence but no acceptance criterion is recorded as blocked.")
                }
            }
        }
    }
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ }
    exit 1
}

$verified = "structure + critical sections (In Scope, Out of Scope, Root-Cause Hypothesis, Validation Plan) carry real content"
if ($contractVersion -eq 2) {
    $verified += " + version 2 acceptance/loop control + declared runtime authority"
} elseif ($contractVersion -eq 3) {
    $verified += " + version 3 acceptance/loop control + mandatory direct agent validation evidence"
}
if ($mode -match '^RESTORE') {
    $verified += " + restore-mode forbidden actions authored beyond boilerplate"
}
if ($mode -and $mode.ToUpperInvariant() -eq "REPAIR") {
    $verified += " + repair-mode evidence artifact"
}
$verified += " + phase $Phase"
if ($RequireCanonicalPath) {
    $verified += " + canonical path"
}
# Emit the structured scope manifest so the scope hook can skip markdown re-parsing.
$python = (Get-Command python -ErrorAction SilentlyContinue) ?? (Get-Command py -ErrorAction SilentlyContinue)
if ($python) {
    $contractDir = Split-Path -Parent $resolvedPath
    $workspaceRoot = if ((Split-Path -Leaf $contractDir) -ieq 'docs') { Split-Path -Parent $contractDir } else { $contractDir }
    $emitter = Join-Path $PSScriptRoot 'emit_scope_manifest.py'
    & $python.Source $emitter --contract $resolvedPath --workspace-root $workspaceRoot 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Scope manifest emission failed (validation still passed): $resolvedPath"
    }
} else {
    Write-Warning "python not found; scope manifest not emitted (validation still passed)."
}
Write-Output "Execution contract validated ($verified): $resolvedPath"
exit 0
