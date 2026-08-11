<#
.SYNOPSIS
Build a complete Amazon Bedrock AgentCore ZIP for Linux/Python 3.13.

.DESCRIPTION
Installs all pinned requirements in a fresh Linux ARM64 Python 3.13 container,
copies the top-level runtime files from this directory into the bundle, verifies
imports and package versions inside the same container, and creates a ZIP whose
entry point is strands_agent/main.py.

.EXAMPLE
.\build_agentcore_bundle.ps1

.EXAMPLE
.\build_agentcore_bundle.ps1 -OutputPath .\dist\strands_agent_v0.0.3.zip -Force
#>

[CmdletBinding()]
param(
    [string]$OutputPath = (Join-Path $PSScriptRoot "dist\strands_agent_bundle.zip"),
    [ValidateSet("linux/arm64/v8", "linux/amd64")]
    [string]$Platform = "linux/arm64/v8",
    [string]$PythonImage = "python:3.13-slim-bookworm",
    [switch]$Force,
    [switch]$KeepStaging
)

$ErrorActionPreference = "Stop"

foreach ($requiredFile in @("main.py", "requirements.txt")) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $requiredFile) -PathType Leaf)) {
        throw "Required source file is missing: $requiredFile"
    }
}

if (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $PSScriptRoot $OutputPath
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

if (Test-Path -LiteralPath $OutputPath) {
    if (-not $Force) {
        throw "Output already exists: $OutputPath. Pass -Force to replace it."
    }
    Remove-Item -LiteralPath $OutputPath -Force
}

$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
}

$applicationFiles = @(
    Get-ChildItem -LiteralPath $PSScriptRoot -File |
        Where-Object { $_.FullName -ne $PSCommandPath }
)

$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    $dockerInfo = @(& docker info --format "{{.ServerVersion}}" 2>&1)
    $dockerInfoExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
if (
    $dockerInfoExitCode -ne 0 -or
    "$dockerInfo" -match "(?i)error during connect|cannot connect|access is denied"
) {
    throw "Docker is unavailable. Start Docker Desktop and run this script again. Docker said: $dockerInfo"
}

$buildRoot = Join-Path $PSScriptRoot ".bundle-build"
$stagingRoot = Join-Path $buildRoot ("agentcore-" + [guid]::NewGuid().ToString("N"))
$bundleRoot = Join-Path $stagingRoot "strands_agent"
New-Item -ItemType Directory -Path $bundleRoot -Force | Out-Null

try {
    Write-Host "Installing Linux/Python 3.13 dependencies for $Platform..."
    $dockerInstallArgs = @(
        "run",
        "--rm",
        "--platform", $Platform,
        "--mount", "type=bind,source=$PSScriptRoot,target=/src,readonly",
        "--mount", "type=bind,source=$bundleRoot,target=/bundle",
        $PythonImage,
        "sh", "-lc",
        "python -m pip install --disable-pip-version-check --no-cache-dir --target /bundle -r /src/requirements.txt"
    )
    & docker @dockerInstallArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed with exit code $LASTEXITCODE."
    }

    foreach ($applicationFile in $applicationFiles) {
        Copy-Item -LiteralPath $applicationFile.FullName -Destination $bundleRoot
    }

    $verificationPath = Join-Path $stagingRoot "verify_bundle.py"
    $verification = @'
import sys
from importlib.metadata import version

sys.path.insert(0, "/stage/strands_agent")

assert version("strands-agents") == "1.50.2", version("strands-agents")
assert version("mcp") == "1.29.0", version("mcp")

from strands import AgentSkills
from strands.models import CacheConfig
import pydantic_core
import rpds
import agent
import main

assert agent.CacheConfig is CacheConfig
assert agent.AgentSkills is AgentSkills
print("Verified strands-agents", version("strands-agents"))
print("Verified mcp", version("mcp"))
print("Verified entry point strands_agent/main.py")
'@
    [System.IO.File]::WriteAllText($verificationPath, $verification)

    Write-Host "Verifying the staged bundle inside $PythonImage..."
    $dockerVerifyArgs = @(
        "run",
        "--rm",
        "--platform", $Platform,
        "--mount", "type=bind,source=$stagingRoot,target=/stage,readonly",
        "--workdir", "/stage/strands_agent",
        $PythonImage,
        "python", "/stage/verify_bundle.py"
    )
    & docker @dockerVerifyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Bundle verification failed with exit code $LASTEXITCODE."
    }
    Remove-Item -LiteralPath $verificationPath -Force

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $stagingRoot,
        $OutputPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $archive = [System.IO.Compression.ZipFile]::OpenRead($OutputPath)
    try {
        $entryNames = @($archive.Entries | ForEach-Object FullName)
        if ($entryNames -notcontains "strands_agent/main.py") {
            throw "Bundle validation failed: strands_agent/main.py is missing."
        }
        if ($entryNames -notcontains "strands_agent/requirements.txt") {
            throw "Bundle validation failed: strands_agent/requirements.txt is missing."
        }
    }
    finally {
        $archive.Dispose()
    }

    $artifact = Get-Item -LiteralPath $OutputPath
    $checksum = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash
    Write-Host "Bundle created: $($artifact.FullName)"
    Write-Host "Size: $([math]::Round($artifact.Length / 1MB, 2)) MiB"
    Write-Host "SHA256: $checksum"
    Write-Host "AgentCore entry point: strands_agent/main.py"
}
finally {
    if ($KeepStaging) {
        Write-Host "Staging retained at: $stagingRoot"
    }
    elseif (Test-Path -LiteralPath $stagingRoot) {
        $resolvedBuildRoot = [System.IO.Path]::GetFullPath($buildRoot).TrimEnd('\') + '\'
        $resolvedStagingRoot = [System.IO.Path]::GetFullPath($stagingRoot)
        if (-not $resolvedStagingRoot.StartsWith($resolvedBuildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unexpected staging path: $resolvedStagingRoot"
        }
        Remove-Item -LiteralPath $resolvedStagingRoot -Recurse -Force
    }
}
