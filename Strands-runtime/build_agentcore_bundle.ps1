<#
.SYNOPSIS
Build a Strands Runtime ZIP for Amazon Bedrock AgentCore.

.DESCRIPTION
Creates a clean Linux Python 3.13 bundle in Docker, installs requirements.txt,
copies the runtime source files, and produces a ZIP whose AgentCore entry point
is strands_agent/main.py.

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

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archiveStream = [System.IO.File]::Open(
        $temporaryArchivePath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    $writableArchive = New-Object System.IO.Compression.ZipArchive(
        $archiveStream,
        [System.IO.Compression.ZipArchiveMode]::Create
    )
    try {
        foreach ($bundleFile in Get-ChildItem -LiteralPath $stagingRoot -Recurse -File) {
            $relativeEntryName = $bundleFile.FullName.Substring($stagingRoot.Length)
            $relativeEntryName = $relativeEntryName.TrimStart([char[]]"\/").Replace("\", "/")
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $writableArchive,
                $bundleFile.FullName,
                $relativeEntryName,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    }
    finally {
        $writableArchive.Dispose()
        $archiveStream.Dispose()
    }

    # Keep an existing artifact intact until the replacement ZIP has been created.
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
