$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot

Push-Location (Join-Path $projectRoot 'backend')
try {
    uv sync --locked
    uv run pytest
}
finally {
    Pop-Location
}

Push-Location $projectRoot
try {
    $python = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
    & $python -m unittest discover -s evaluation\tests -v
    & $python -m evaluation.validate_data
}
finally {
    Pop-Location
}

Push-Location (Join-Path $projectRoot 'frontend')
try {
    pnpm install --frozen-lockfile
    pnpm test:contract
    pnpm lint
    pnpm typecheck
    pnpm build
}
finally {
    Pop-Location
}

Write-Output 'All local verification checks passed.'
