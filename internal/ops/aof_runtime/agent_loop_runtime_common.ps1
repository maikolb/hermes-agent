Set-StrictMode -Version Latest

function Get-AofPathStringComparison {
    [CmdletBinding()]
    [OutputType([System.StringComparison])]
    param()

    if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
        return [System.StringComparison]::OrdinalIgnoreCase
    }
    return [System.StringComparison]::Ordinal
}
function Test-AofPathEquals {
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$OtherPath
    )

    $normalizedPath = [System.IO.Path]::GetFullPath($Path)
    $normalizedOtherPath = [System.IO.Path]::GetFullPath($OtherPath)
    $pathRoot = [System.IO.Path]::GetPathRoot($normalizedPath)
    $otherPathRoot = [System.IO.Path]::GetPathRoot($normalizedOtherPath)
    if ($normalizedPath.Length -gt $pathRoot.Length) {
        $normalizedPath = $normalizedPath.TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    }
    if ($normalizedOtherPath.Length -gt $otherPathRoot.Length) {
        $normalizedOtherPath = $normalizedOtherPath.TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    }

    return $normalizedPath.Equals($normalizedOtherPath, (Get-AofPathStringComparison))
}

function Test-AofPathWithinRoot {
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $candidatePath = [System.IO.Path]::GetFullPath($Path)
    $rootPath = [System.IO.Path]::GetFullPath($Root)
    $candidatePathRoot = [System.IO.Path]::GetPathRoot($candidatePath)
    $rootPathRoot = [System.IO.Path]::GetPathRoot($rootPath)
    if ($candidatePath.Length -gt $candidatePathRoot.Length) {
        $candidatePath = $candidatePath.TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    }
    if ($rootPath.Length -gt $rootPathRoot.Length) {
        $rootPath = $rootPath.TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    }

    $comparison = Get-AofPathStringComparison
    if ($candidatePath.Equals($rootPath, $comparison)) {
        return $true
    }

    $rootPrefix = if ($rootPath.EndsWith([string][System.IO.Path]::DirectorySeparatorChar)) {
        $rootPath
    } else {
        $rootPath + [System.IO.Path]::DirectorySeparatorChar
    }
    return $candidatePath.StartsWith($rootPrefix, $comparison)
}

function Get-AofArtifactSha256 {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Artifact hash input file does not exist: $Path"
    }

    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $textExtensions = @(
        ".ps1", ".psm1", ".psd1", ".json", ".jsonl", ".md", ".txt",
        ".yaml", ".yml", ".toml", ".xml", ".csv"
    )
    $extension = [System.IO.Path]::GetExtension($resolvedPath).ToLowerInvariant()

    if ($textExtensions -notcontains $extension) {
        return (Get-FileHash -LiteralPath $resolvedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }

    $bytes = [System.IO.File]::ReadAllBytes($resolvedPath)
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        $text = $strictUtf8.GetString($bytes)
    } catch [System.Text.DecoderFallbackException] {
        throw "Artifact hash input must be valid UTF-8 for declared text extension '$extension': $resolvedPath"
    }

    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) {
        $text = $text.Substring(1)
    }
    $canonicalText = $text.Replace("`r`n", "`n").Replace("`r", "`n")
    $canonicalBytes = $strictUtf8.GetBytes($canonicalText)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($canonicalBytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}
