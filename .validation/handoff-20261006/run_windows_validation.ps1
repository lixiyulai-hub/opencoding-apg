param(
    [Parameter(Mandatory=$true)][string]$Repository,
    [Parameter(Mandatory=$true)][ValidateSet('3.11','3.12')][string]$PythonVersion,
    [string]$PythonExecutable = 'python',
    [Parameter(Mandatory=$true)][string]$BindingSha256,
    [Parameter(Mandatory=$true)][string]$EvidenceDirectory
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows required; validation remains pending.' }
if (Test-Path -LiteralPath $EvidenceDirectory) { throw 'Use a new evidence directory.' }
$repoPath = (Resolve-Path -LiteralPath $Repository).Path
$evidencePath = [IO.Path]::GetFullPath($EvidenceDirectory)
if ($evidencePath.Equals($repoPath, [StringComparison]::OrdinalIgnoreCase) -or
    $evidencePath.StartsWith($repoPath.TrimEnd('\','/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Evidence must be outside the source checkout.'
}
New-Item -ItemType Directory -Path $evidencePath | Out-Null
$binding = Join-Path $PSScriptRoot 'candidate-binding.json'
$verifier = Join-Path $PSScriptRoot 'verify_source.py'
if ((Get-FileHash -LiteralPath $binding -Algorithm SHA256).Hash.ToLower() -ne $BindingSha256) { throw 'Binding digest mismatch.' }
$bound = Get-Content -LiteralPath $binding -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $verifier -Algorithm SHA256).Hash.ToLower() -ne $bound.verifier_sha256) { throw 'Verifier digest mismatch.' }
if ((Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLower() -ne $bound.windows_runner_sha256) { throw 'Runner digest mismatch.' }
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
        # Windows PowerShell represents native stderr as ErrorRecord objects.
        # Capture it without throwing before the real native exit code is saved.
        $ErrorActionPreference = 'Continue'
        $PSNativeCommandUseErrorActionPreference = $false
        $lines = @(& $resolved @Arguments 2>&1)
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    $lines | Out-File -LiteralPath $log -Encoding utf8
    $text = $lines -join "`n"
    $skips = @($lines | Where-Object { "$_" -match '\.\.\. skipped ' } | ForEach-Object { "$_" })
    $count = $null
    $summary = [regex]::Match($text, '(?m)^Ran (\d+) tests? in ')
    if ($summary.Success) { $count = [int]$summary.Groups[1].Value }
    $records.Add([ordered]@{label=$Label; program=$Program; arguments=$Arguments; exit_code=$code; tests_run=$count; skips=$skips; log_sha256=(Get-FileHash -LiteralPath $log -Algorithm SHA256).Hash.ToLower()})
    if ($code -ne 0) { throw ($Label + ' failed; preserve evidence.') }
    if ($ExpectedTests -ge 0 -and $count -ne $ExpectedTests) { throw ($Label + ' test count mismatch.') }
    if ($skips.Count -gt 0) { throw ($Label + ' has skipped tests; acceptance incomplete.') }
    if ($ExpectedTests -ge 0 -and $text -match '(?m)^(FAILED|ERROR:|FAIL:)') { throw ($Label + ' reported a failure.') }
    if ($Label -eq 'rust-domain' -and $text -notmatch 'test result: ok\. 4 passed; 0 failed; 0 ignored;') { throw 'Rust result incomplete.' }
}

Push-Location -LiteralPath $repoPath
try {
    Invoke-Recorded 'source-before' $PythonExecutable @('-X','utf8',$verifier,'--root',$repoPath,'--binding',$binding,'--binding-sha256',$BindingSha256)
    Invoke-Recorded 'prerequisites' $PythonExecutable @('-c','import sys,platform,setuptools; print(sys.version); print(platform.platform()); print(setuptools.__version__); print("optimize=" + str(sys.flags.optimize)); sys.exit(0 if sys.platform == "win32" and sys.version_info[:2] == tuple(map(int,sys.argv[1].split("."))) and int(setuptools.__version__.split(".")[0]) >= 83 and sys.flags.optimize == 0 else "prerequisites_failed")',$PythonVersion)
    Invoke-Recorded 'cargo-version' 'cargo' @('--version')
    Invoke-Recorded 'rustc-version' 'rustc' @('--version')
    Invoke-Recorded 'adapter' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_taskplan_scheduler','-v') 19
    Invoke-Recorded 'full' $PythonExecutable @('-X','utf8','-m','unittest','-v') 247
    Invoke-Recorded 'packaging' $PythonExecutable @('-X','utf8','-m','unittest','tests.test_product_packaging','-v') 6
    Invoke-Recorded 'rust-domain' 'cargo' @('test','--offline','--manifest-path','services/domain/Cargo.toml')
    $state = 'passed_for_this_python_version'
}
catch {
    $state = 'blocked_or_failed'
    Write-Warning 'Validation stopped; inspect the recorded evidence locally.'
}
finally {
    try {
        Invoke-Recorded 'source-after' $PythonExecutable @('-X','utf8',$verifier,'--root',$repoPath,'--binding',$binding,'--binding-sha256',$BindingSha256)
    } catch { $state = 'blocked_or_failed' }
    [ordered]@{status=$state; python_version=$PythonVersion; binding_sha256=$BindingSha256; records=$records; runner=$env:RUNNER_OS; github_event=$env:GITHUB_EVENT_NAME; github_sha=$env:GITHUB_SHA; github_run_id=$env:GITHUB_RUN_ID; github_run_attempt=$env:GITHUB_RUN_ATTEMPT; github_job=$env:GITHUB_JOB; script_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLower()} |
        ConvertTo-Json -Depth 8 | Out-File -LiteralPath (Join-Path $evidencePath 'result.json') -Encoding utf8
    Pop-Location
}
if ($state -ne 'passed_for_this_python_version') { exit 1 }
exit 0
