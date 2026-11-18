# Load this checkout's Git-ignored configuration into the current process.
# Usage: . ./load-config.ps1
$tracefolioLocalBindingsPath = Join-Path $PSScriptRoot 'bindings.local.json'
try {
    $tracefolioLocalBindings = Get-Content -LiteralPath $tracefolioLocalBindingsPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ($tracefolioLocalBindings -isnot [PSCustomObject]) {
        throw 'Invalid configuration shape.'
    }
    foreach ($tracefolioLocalBinding in $tracefolioLocalBindings.PSObject.Properties) {
        if ($tracefolioLocalBinding.Name -notmatch '^TRACEFOLIO_[A-Z0-9_]+$' -or
            $tracefolioLocalBinding.Value -isnot [string]) {
            throw 'Invalid configuration entry.'
        }
    }
    $tracefolioHasSingleConfig = -not [string]::IsNullOrWhiteSpace($tracefolioLocalBindings.TRACEFOLIO_CONFIG)
    $tracefolioHasMultipleConfigs = -not [string]::IsNullOrWhiteSpace($tracefolioLocalBindings.TRACEFOLIO_CONFIG_FILES)
    if ($tracefolioHasSingleConfig -eq $tracefolioHasMultipleConfigs) {
        throw 'Select exactly one configuration selector.'
    }
} catch {
    throw 'Cannot load bindings.local.json. Check the local configuration format and selector.'
}

Remove-Item Env:TRACEFOLIO_CONFIG -ErrorAction SilentlyContinue
Remove-Item Env:TRACEFOLIO_CONFIG_FILES -ErrorAction SilentlyContinue
foreach ($tracefolioLocalBinding in $tracefolioLocalBindings.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable($tracefolioLocalBinding.Name, [string]$tracefolioLocalBinding.Value, 'Process')
}
