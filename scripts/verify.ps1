$ErrorActionPreference = 'Stop'

function Assert-LastExitCode {
    param([string]$Step)

    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot

Push-Location (Join-Path $projectRoot 'backend')
try {
    uv sync --locked
    Assert-LastExitCode 'uv sync --locked'
    uv run pytest
    Assert-LastExitCode 'uv run pytest'
}
finally {
    Pop-Location
}

Push-Location $projectRoot
try {
    $python = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
    & $python -m unittest discover -s evaluation\tests -v
    Assert-LastExitCode 'evaluation unit tests'
    & $python -m evaluation.validate_data
    Assert-LastExitCode 'evaluation data validation'
}
finally {
    Pop-Location
}

Push-Location (Join-Path $projectRoot 'frontend')
try {
    pnpm install --frozen-lockfile
    Assert-LastExitCode 'pnpm install --frozen-lockfile'
    pnpm test:contract
    Assert-LastExitCode 'pnpm test:contract'
    pnpm lint
    Assert-LastExitCode 'pnpm lint'
    pnpm typecheck
    Assert-LastExitCode 'pnpm typecheck'
    pnpm build
    Assert-LastExitCode 'pnpm build'
}
finally {
    Pop-Location
}

Write-Output 'All local verification checks passed.'
