<#
.SYNOPSIS
Build a Strands Runtime ZIP for Amazon Bedrock AgentCore.

.DESCRIPTION
Creates a clean Linux Python 3.13 bundle in Docker, installs the exact package
versions pinned in requirements.txt, copies the runtime source files, verifies
the bundle in the target container, and produces a ZIP whose AgentCore entry
point is strands_agent/main.py.

.EXAMPLE
.\build_agentcore_bundle.ps1

.EXAMPLE
.\build_agentcore_bundle.ps1 -OutputPath .\dist\strands_agent_v0.0.5.zip -Force
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

$runtimeFiles = @(
    "agent.py",
    "code_interpreter.py",
    "gateway_config.py",
    "gateway_proxy.py",
    "main.py",
    "memory.py",
    "requirements.txt",
    "skills_sync.py",
    "system_prompt.py"
)

foreach ($runtimeFile in $runtimeFiles) {
    $sourcePath = Join-Path $PSScriptRoot $runtimeFile
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Required runtime file is missing: $sourcePath"
    }
}

if (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $PSScriptRoot $OutputPath
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)

if ((Test-Path -LiteralPath $OutputPath) -and -not $Force) {
    throw "Output already exists: $OutputPath. Pass -Force to replace it."
}

$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install or start Docker Desktop and run this script again."
}

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

$requirementsPath = Join-Path $PSScriptRoot "requirements.txt"
$expectedVersions = [ordered]@{}
foreach ($line in Get-Content -LiteralPath $requirementsPath) {
    $requirement = $line.Trim()
    if (-not $requirement -or $requirement.StartsWith("#")) {
        continue
    }
    if ($requirement -notmatch "^([A-Za-z0-9][A-Za-z0-9._-]*)==([^;#\s]+)$") {
        throw "Every runtime dependency must use an exact == pin: $requirement"
    }
    $expectedVersions[$Matches[1]] = $Matches[2]
}
if ($expectedVersions.Count -eq 0) {
    throw "No pinned dependencies were found in $requirementsPath."
}
$expectedVersionsJson = $expectedVersions | ConvertTo-Json -Compress

$buildRoot = Join-Path $PSScriptRoot ".bundle-build"
$buildId = [guid]::NewGuid().ToString("N")
$stagingRoot = Join-Path $buildRoot "agentcore-$buildId"
$bundleRoot = Join-Path $stagingRoot "strands_agent"
$temporaryArchivePath = Join-Path $buildRoot "bundle-$buildId.zip"
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

    foreach ($runtimeFile in $runtimeFiles) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $runtimeFile) -Destination $bundleRoot
    }

    $verificationPath = Join-Path $stagingRoot "verify_bundle.py"
    $verification = @"
import json
import platform
import sys
from importlib.metadata import version

EXPECTED_VERSIONS = json.loads(r'''$expectedVersionsJson''')

assert sys.version_info[:2] == (3, 13), sys.version
for distribution, expected in EXPECTED_VERSIONS.items():
    actual = version(distribution)
    assert actual == expected, f"{distribution}: expected {expected}, found {actual}"

sys.path.insert(0, "/stage/strands_agent")

import boto3
import httpx
import mcp
import pydantic_core
import rpds
from bedrock_agentcore import BedrockAgentCoreApp
from strands import AgentSkills
from strands.models import CacheConfig, CacheToolsConfig
import agent
import main

assert agent.CacheConfig is CacheConfig
assert agent.CacheToolsConfig is CacheToolsConfig
assert agent.AgentSkills is AgentSkills
assert main.app is not None
print("Verified Python", platform.python_version(), "on", platform.machine())
for distribution, expected in EXPECTED_VERSIONS.items():
    print("Verified", distribution, expected)
print("Verified AgentCore entry point strands_agent/main.py")
"@
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
        $temporaryArchivePath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $archive = [System.IO.Compression.ZipFile]::OpenRead($temporaryArchivePath)
    try {
        $entryNames = @($archive.Entries | ForEach-Object FullName)
        foreach ($runtimeFile in $runtimeFiles) {
            $expectedEntry = "strands_agent/$runtimeFile"
            if ($entryNames -notcontains $expectedEntry) {
                throw "Bundle validation failed: $expectedEntry is missing."
            }
        }
        if ($entryNames -contains "verify_bundle.py") {
            throw "Bundle validation failed: the temporary verification script was included."
        }
    }
    finally {
        $archive.Dispose()
    }

    # Keep an existing artifact intact until the replacement has passed every check.
    Move-Item -LiteralPath $temporaryArchivePath -Destination $OutputPath -Force:$Force

    $artifact = Get-Item -LiteralPath $OutputPath
    $checksum = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash
    Write-Host "Bundle created: $($artifact.FullName)"
    Write-Host "Size: $([math]::Round($artifact.Length / 1MB, 2)) MiB"
    Write-Host "SHA256: $checksum"
    Write-Host "AgentCore entry point: strands_agent/main.py"
}
finally {
    $resolvedBuildRoot = [System.IO.Path]::GetFullPath($buildRoot).TrimEnd([char[]]"\/")
    $resolvedStagingRoot = [System.IO.Path]::GetFullPath($stagingRoot)
    $resolvedTemporaryArchive = [System.IO.Path]::GetFullPath($temporaryArchivePath)
    $pathPrefix = $resolvedBuildRoot + [System.IO.Path]::DirectorySeparatorChar

    if ($resolvedStagingRoot.StartsWith($pathPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        if ($KeepStaging) {
            Write-Host "Staging retained at: $resolvedStagingRoot"
        }
        elseif (Test-Path -LiteralPath $resolvedStagingRoot) {
            Remove-Item -LiteralPath $resolvedStagingRoot -Recurse -Force
        }
    }
    else {
        Write-Warning "Refusing to remove unexpected staging path: $resolvedStagingRoot"
    }

    if (
        $resolvedTemporaryArchive.StartsWith($pathPrefix, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $resolvedTemporaryArchive)
    ) {
        Remove-Item -LiteralPath $resolvedTemporaryArchive -Force
    }
}
