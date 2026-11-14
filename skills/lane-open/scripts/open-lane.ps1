<#
.SYNOPSIS
  Open a new lane of work: one new, visible, independent Claude Code or Codex window.

.DESCRIPTION
  Support script for the lane-open skill (skills/lane-open/core.md). It writes a small
  launcher outside every repository and starts it in a new PowerShell window. The
  launcher removes the caller's host-session variables (CLAUDE*, CODEX_*; it keeps
  CLAUDE_CONFIG_DIR and CODEX_HOME) so the new session is an independent peer, not a
  child of the session that opened it. It never uses claude --bg, never passes a
  permission-bypass or sandbox-bypass flag, and never touches another window.

  Run it as the real user. From a Codex sandbox it must run with escalation, or the
  window would belong to the sandbox user.

  Prints one JSON line. Exit 0 launched (or -DryRun planned); 2 bad argument; 4 the
  launch itself failed.

  ASCII only: Windows PowerShell 5.1 reads a no-BOM script as ANSI.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Lane,
    [Parameter(Mandatory = $true)][ValidateSet('claude', 'codex')][string]$HostName,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [Parameter(Mandatory = $true)][string]$PromptFile,
    [string]$Title = '',
    [string]$Model = '',
    [ValidateSet('', 'low', 'medium', 'high', 'xhigh', 'max')][string]$Effort = '',
    [switch]$WorkspaceWrite,
    [ValidateSet('', 'user', 'auto-review')][string]$ApprovalMode = '',
    [switch]$TrustWorkspace,
    [string]$StoreRoot = '',
    [string]$StateRoot = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$IdPattern = '^[a-z0-9]+(-[a-z0-9]+)*$'
$TokenPattern = '^[A-Za-z0-9._:-]+$'

function Fail([int]$Code, [string]$Reason) {
    $obj = [ordered]@{ launched = $false; error = $Reason }
    Write-Output ($obj | ConvertTo-Json -Compress)
    exit $Code
}

function Quote([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

if ($Project -notmatch $IdPattern -or $Project.Length -gt 32) { Fail 2 "project id '$Project' does not match the Passing Notes grammar" }
if ($Lane -notmatch $IdPattern -or $Lane.Length -gt 32 -or $Lane -in @('all', 'operator')) { Fail 2 "lane id '$Lane' does not match the Passing Notes grammar" }
if ($WorkspaceWrite -and $HostName -ne 'codex') { Fail 2 'WorkspaceWrite applies only to codex lanes' }
if (($ApprovalMode -or $TrustWorkspace) -and $HostName -ne 'codex') { Fail 2 'ApprovalMode and TrustWorkspace apply only to codex lanes' }
if ($Model -and $Model -notmatch $TokenPattern) { Fail 2 "model '$Model' is not a plain model name or alias" }
if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) { Fail 2 "working directory '$WorkingDirectory' does not exist" }
if (-not (Test-Path -LiteralPath $PromptFile -PathType Leaf)) { Fail 2 "prompt file '$PromptFile' does not exist" }
if ($Title -match '[\r\n"]') { Fail 2 'title must be one line without double quotes' }

$WorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
if (-not $Title) { $Title = "$Project - $Lane lane" }
if (-not $StoreRoot) {
    if ($env:PASSING_NOTES_ROOT) { $StoreRoot = $env:PASSING_NOTES_ROOT } else { $StoreRoot = Join-Path $env:LOCALAPPDATA 'passing-notes' }
}
if (-not $StateRoot) { $StateRoot = Join-Path $env:LOCALAPPDATA 'passing-notes-lanes' }

# Read supported host configuration before opening a window. Never silently trust
# a workspace or select automatic review. Dry runs describe but do not run probes.
$codexOverrides = @()
$preflight = $null
if ($HostName -eq 'codex') {
    # --approve-for-me itself selects workspace-write, so probe what will actually launch.
    if ($WorkspaceWrite -or $ApprovalMode -eq 'auto-review') { $codexOverrides += 'sandbox_mode=workspace-write' }
    if ($ApprovalMode) {
        $codexOverrides += 'approval_policy=on-request'
        $reviewer = if ($ApprovalMode -eq 'auto-review') { 'auto_review' } else { 'user' }
        $codexOverrides += "approvals_reviewer=$reviewer"
    }
    if ($TrustWorkspace) {
        if ($WorkingDirectory.Contains("'")) { Fail 2 'TrustWorkspace needs a path without a single quote; configure trust through Codex first' }
        $trustOverride = "projects={ '" + $WorkingDirectory + "' = { trust_level = 'trusted' } }"
        $codexOverrides += $trustOverride
    }
    $preflight = [ordered]@{ state = 'not run (dry run)'; requested_store_root = $StoreRoot; explicit_trust = [bool]$TrustWorkspace; approval_mode = $ApprovalMode }
    if (-not $DryRun) {
        if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail 2 'Codex preflight requires Python 3.11 or newer on PATH' }
        $probeArgs = @((Join-Path $PSScriptRoot 'codex-preflight.py'), '--cwd', $WorkingDirectory)
        foreach ($override in $codexOverrides) { $probeArgs += @('-c', $override) }
        $probeOutput = & python @probeArgs
        if ($LASTEXITCODE -ne 0) { Fail 2 ("Codex preflight failed: " + ($probeOutput -join ' ')) }
        $preflight = ($probeOutput -join "`n") | ConvertFrom-Json
        if ($preflight.workspace_trust -ne 'trusted') { Fail 2 'Workspace trust is not established. Review it in central and use explicit -TrustWorkspace only for an authorized workspace; no window opened.' }
        if ($preflight.permission_profile) { Fail 2 'A named Codex permission profile is active; verify its effective writable roots through the host before launching. No window opened.' }
        if ($preflight.sandbox_mode -ne 'workspace-write') { Fail 2 'Codex writable store is not verified. Use -WorkspaceWrite for an authorized coding lane; no window opened.' }
        if ($preflight.approval_policy -eq 'host-default (unverified)' -or $preflight.approvals_reviewer -eq 'host-default (unverified)') { Fail 2 'Select explicit -ApprovalMode user or auto-review after reviewing the native approval limitation. No window opened.' }
    }
}

# Windows PowerShell 5.1 mangles double quotes inside native-command arguments, which
# cut a prompt short in testing. The prompt travels as an argument, so they become '.
$utf8 = New-Object System.Text.UTF8Encoding($false)
$prompt = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $PromptFile).Path, $utf8)
$quotesReplaced = $prompt.Contains('"')
$prompt = $prompt.Replace('"', "'")
if (-not $prompt.Trim()) { Fail 2 'prompt file is empty' }

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$folder = Join-Path $StateRoot $Project
$launcher = Join-Path $folder "$Lane-$stamp.ps1"
$promptCopy = Join-Path $folder "$Lane-$stamp.prompt.txt"

if ($HostName -eq 'claude') {
    $cmd = '& claude -n ' + (Quote $Title)
    if ($Model) { $cmd += ' --model ' + (Quote $Model) }
    if ($Effort) { $cmd += ' --effort ' + (Quote $Effort) }
    $cmd += ' $prompt'
} else {
    $cmd = '& codex -C ' + (Quote $WorkingDirectory) + ' --add-dir ' + (Quote $StoreRoot)
    if ($WorkspaceWrite -and $ApprovalMode -ne 'auto-review') { $cmd += ' --sandbox workspace-write' }
    if ($ApprovalMode -eq 'auto-review') { $cmd += ' --approve-for-me' }
    if ($ApprovalMode -eq 'user') { $cmd += ' --ask-for-approval on-request -c approvals_reviewer=user' }
    if ($TrustWorkspace) { $cmd += ' -c ' + (Quote $trustOverride) }
    if ($Model) { $cmd += ' -m ' + (Quote $Model) }
    # An unquoted TOML value is passed through as a literal string by codex -c.
    if ($Effort) { $cmd += ' -c ' + (Quote "model_reasoning_effort=$Effort") }
    $cmd += ' $prompt'
}

$lines = @(
    ('# Generated by the lane-open skill. Opens one lane window; safe to delete afterwards.'),
    ("`$ErrorActionPreference = 'Stop'"),
    ("Get-ChildItem Env: | Where-Object { (`$_.Name -like 'CLAUDE*' -and `$_.Name -ne 'CLAUDE_CONFIG_DIR') -or (`$_.Name -like 'CODEX_*' -and `$_.Name -ne 'CODEX_HOME') } | ForEach-Object { Remove-Item -LiteralPath ('Env:' + `$_.Name) }"),
    ("`$Host.UI.RawUI.WindowTitle = " + (Quote $Title)),
    ('Set-Location -LiteralPath ' + (Quote $WorkingDirectory)),
    ("`$prompt = [System.IO.File]::ReadAllText(" + (Quote $promptCopy) + ', (New-Object System.Text.UTF8Encoding($false)))'),
    ($cmd)
)
$launcherText = ($lines -join "`r`n") + "`r`n"

$result = [ordered]@{
    launched = $false
    dry_run = [bool]$DryRun
    host = $HostName
    project = $Project
    lane = $Lane
    title = $Title
    working_directory = $WorkingDirectory
    launcher = $launcher
    prompt_file = $promptCopy
    double_quotes_replaced = $quotesReplaced
    pid = $null
    preflight = $preflight
}

if ($DryRun) {
    $result['launcher_text'] = $launcherText
    Write-Output ($result | ConvertTo-Json -Compress)
    exit 0
}

try {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    [System.IO.File]::WriteAllText($promptCopy, $prompt, $utf8)
    # UTF-8 with a BOM, so Windows PowerShell 5.1 reads non-ASCII titles and paths correctly.
    [System.IO.File]::WriteAllText($launcher, $launcherText, (New-Object System.Text.UTF8Encoding($true)))
    $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoExit', '-ExecutionPolicy', 'Bypass', '-File', $launcher) -WorkingDirectory $WorkingDirectory -PassThru
} catch {
    Fail 4 ("launch failed: " + $_.Exception.Message)
}
$result['launched'] = $true
$result['pid'] = $proc.Id
Write-Output ($result | ConvertTo-Json -Compress)
exit 0
