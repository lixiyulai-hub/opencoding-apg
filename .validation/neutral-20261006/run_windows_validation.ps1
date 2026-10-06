param(
    [Parameter(Mandatory=$true)][string]$Repository,
    [Parameter(Mandatory=$true)][ValidateSet('3.11','3.12')][string]$PythonVersion,
    [string]$PythonExecutable = 'python',
    [Parameter(Mandatory=$true)][string]$BindingSha256,
    [Parameter(Mandatory=$true)][string]$EvidenceDirectory
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$script:firstNativeFailure = $null
try {
    if ($env:OS -ne 'Windows_NT') { throw 'validation_unavailable' }
    if (Test-Path -LiteralPath $EvidenceDirectory) { throw 'validation_unavailable' }
    $repoPath = (Resolve-Path -LiteralPath $Repository).Path
    $evidencePath = [IO.Path]::GetFullPath($EvidenceDirectory)
    $tempPrefix = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar
    if (-not $evidencePath.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'validation_unavailable' }
    if ($evidencePath.Equals($repoPath, [StringComparison]::OrdinalIgnoreCase) -or
        $evidencePath.StartsWith($repoPath.TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'validation_unavailable' }
    New-Item -ItemType Directory -Path $evidencePath | Out-Null
    $binding = Join-Path $PSScriptRoot 'candidate-binding.json'
    $verifier = Join-Path $PSScriptRoot 'verify_source.py'
    if ((Get-FileHash -LiteralPath $binding -Algorithm SHA256).Hash.ToLower() -ne $BindingSha256) { throw 'validation_unavailable' }
    $bound = Get-Content -LiteralPath $binding -Raw | ConvertFrom-Json
    if ((Get-FileHash -LiteralPath $verifier -Algorithm SHA256).Hash.ToLower() -ne $bound.verifier_sha256) { throw 'validation_unavailable' }
    if ((Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLower() -ne $bound.windows_runner_sha256) { throw 'validation_unavailable' }
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONOPTIMIZE = '0'
    $env:CARGO_NET_OFFLINE = 'true'
    $env:CARGO_TARGET_DIR = Join-Path $evidencePath 'cargo-target'
    $records = [System.Collections.Generic.List[object]]::new()
    $state = 'pending'

    function Invoke-Recorded {
        param([string]$Label, [string]$Program, [string[]]$Arguments, [int]$ExpectedTests = -1)
        $log = Join-Path $evidencePath ($Label + '.log')
        $resolved = (Get-Command $Program -CommandType Application -ErrorAction Stop).Source
        $savedPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $lines = @(& $resolved @Arguments 2>&1)
            $code = $LASTEXITCODE
            if ($code -ne 0 -and $null -eq $script:firstNativeFailure) { $script:firstNativeFailure = $code }
        } finally { $ErrorActionPreference = $savedPreference }
        $lines | Out-File -LiteralPath $log -Encoding utf8
        $text = ($lines | ForEach-Object { "$_" }) -join "`n"
        $skips = @($lines | Where-Object { "$_" -match '\.\.\. skipped ' } | ForEach-Object { "$_" })
        $count = $null
        $parsed = $true
        $reportedFailure = $false
        if ($ExpectedTests -ge 0) {
            $summaries = [regex]::Matches($text, '(?m)^Ran ([0-9]+) tests? in [0-9]+(?:\.[0-9]+)?s\r?$')
            $footers = [regex]::Matches($text, '(?m)^(OK|FAILED)(?: \(([^\r\n]+)\))?\r?$')
            if ($summaries.Count -ne 1 -or $footers.Count -ne 1) { $parsed = $false }
            elseif ($summaries[0].Groups[1].Value.Length -gt 8) { $parsed = $false }
            else {
                $count = [int]$summaries[0].Groups[1].Value
                $tail = $text.Substring($summaries[0].Index + $summaries[0].Length)
                $footer = [regex]::Match($tail, '^\r?\n\r?\n(OK|FAILED)(?: \(([^\r\n]+)\))?(?:\r?\n|$)')
                if (-not $footer.Success) { $parsed = $false }
                else {
                    $reportedFailure = $footer.Groups[1].Value -eq 'FAILED'
                    $seen = @{}
                    $footerSkipCount = 0
                    if ($footer.Groups[2].Success) {
                        foreach ($item in ($footer.Groups[2].Value -split ', ')) {
                            $field = [regex]::Match($item, '^(failures|errors|skipped|expected failures|unexpected successes)=([0-9]+)$')
                            if (-not $field.Success -or $field.Groups[2].Value.Length -gt 8) { $parsed = $false; continue }
                            $key = $field.Groups[1].Value
                            if ($seen.ContainsKey($key)) { $parsed = $false; continue }
                            $seen[$key] = $true
                            $number = [int]$field.Groups[2].Value
                            if ($number -gt $ExpectedTests) { $parsed = $false; continue }
                            if ($key -eq 'skipped') { $footerSkipCount = $number }
                            if ($key -in @('failures','errors','unexpected successes') -and $number -gt 0) { $reportedFailure = $true }
                        }
                    }
                    if ($footerSkipCount -gt 0 -and $skips.Count -ne $footerSkipCount) {
                        # Count comes from the native footer; raw reasons stay in the log.
                        $skips = @(1..$footerSkipCount | ForEach-Object { 'native_footer_skip' })
                    }
                }
            }
        }
        $records.Add([ordered]@{label=$Label; program=$Program; arguments=$Arguments; exit_code=$code; tests_run=$count; skips=$skips; log_sha256=(Get-FileHash -LiteralPath $log -Algorithm SHA256).Hash.ToLower()})
        if ($code -ne 0) { throw 'recorded_native_failure' }
        if (-not $parsed -or $reportedFailure) { throw 'recorded_result_rejected' }
        if ($ExpectedTests -ge 0 -and $count -ne $ExpectedTests) { throw 'recorded_count_rejected' }
        if ($skips.Count -gt 0) { throw 'recorded_skip_rejected' }
        if ($ExpectedTests -ge 0 -and $text -match '(?m)^(FAILED|ERROR:|FAIL:)') { throw 'recorded_result_rejected' }
        if ($Label -eq 'rust-domain' -and $text -notmatch 'test result: ok\. 4 passed; 0 failed; 0 ignored;') { throw 'recorded_rust_result_rejected' }
    }

    Push-Location -LiteralPath $repoPath
    try {
        Invoke-Recorded 'source-before' $PythonExecutable @('-X','utf8',$verifier,'--root',$repoPath,'--binding',$binding,'--binding-sha256',$BindingSha256)
        Invoke-Recorded 'prerequisites' $PythonExecutable @('-c','import sys,platform,setuptools; print(sys.version); print(platform.platform()); print(setuptools.__version__); print("optimize=" + str(sys.flags.optimize)); sys.exit(0 if sys.platform == "win32" and sys.version_info[:2] == tuple(map(int,sys.argv[1].split("."))) and int(setuptools.__version__.split(".")[0]) >= 83 and sys.flags.optimize == 0 else "prerequisites_failed")',$PythonVersion)
        Invoke-Recorded 'cargo-version' 'cargo' @('--version')
        Invoke-Recorded 'rustc-version' 'rustc' @('--version')
        Invoke-Recorded 'adapter' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_taskplan_scheduler','-v') $bound.test_counts.adapter
        Invoke-Recorded 'cli-preview' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_taskplan_cli','-v') $bound.test_counts.cli_preview
        Invoke-Recorded 'evidence-projection' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_taskplan_evidence','-v') $bound.test_counts.evidence_projection
        Invoke-Recorded 'installed-taskplan' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_installed_taskplan','-v') $bound.test_counts.installed_taskplan
        Invoke-Recorded 'strict-validator' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_transaction_evidence_validation','-v') $bound.test_counts.strict_validator
        Invoke-Recorded 'transaction-regression' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_product_transactions','-v') $bound.test_counts.transaction_regression
        Invoke-Recorded 'full' $PythonExecutable @('-X','utf8','-m','unittest','-v') $bound.test_counts.full
        Invoke-Recorded 'packaging' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_product_packaging','-v') $bound.test_counts.packaging
        Invoke-Recorded 'rust-domain' 'cargo' @('test','--offline','--manifest-path','services/domain/Cargo.toml')
        $state = 'passed_for_this_python_version'
    } catch { $state = 'blocked_or_failed' }
    finally {
        try {
            Invoke-Recorded 'source-after' $PythonExecutable @('-X','utf8',$verifier,'--root',$repoPath,'--binding',$binding,'--binding-sha256',$BindingSha256)
        } catch { $state = 'blocked_or_failed' }
        [ordered]@{status=$state; python_version=$PythonVersion; binding_sha256=$BindingSha256; records=$records; runner=$env:RUNNER_OS; github_event=$env:GITHUB_EVENT_NAME; github_sha=$env:GITHUB_SHA; github_run_id=$env:GITHUB_RUN_ID; github_run_attempt=$env:GITHUB_RUN_ATTEMPT; github_job=$env:GITHUB_JOB; script_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLower()} |
            ConvertTo-Json -Depth 8 | Out-File -LiteralPath (Join-Path $evidencePath 'result.json') -Encoding utf8
        Pop-Location
    }
    if ($null -ne $script:firstNativeFailure) { exit $script:firstNativeFailure }
    if ($state -ne 'passed_for_this_python_version') { exit 1 }
    exit 0
} catch {
    Write-Output '{"kind":"public_summary_unavailable","status":"incomplete"}'
    if ($null -ne $script:firstNativeFailure) { exit $script:firstNativeFailure }
    exit 1
}
