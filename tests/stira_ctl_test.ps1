# Dependency-free regression tests. Only AST-extracted functions are executed;
# no real process discovery, startup or termination is permitted.
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$source = Join-Path $PSScriptRoot '../scripts/stira_ctl.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false) |
    ForEach-Object { Invoke-Expression $_.Extent.Text }
$Entry = 'C:\test checkout\dashboard\stira_server.py'
$Root = 'C:\test checkout'
$Port = 5555
$Py = 'python.exe'
$Base = 'http://127.0.0.1:5555'
$OutLog = Join-Path $PSScriptRoot 'fake.out.log'
$ErrLog = Join-Path $PSScriptRoot 'fake.err.log'
$script:passed = 0
function Assert($Condition, $Message) {
    if (-not $Condition) { throw $Message }
    $script:passed++
}
function Proc($Name, $CommandLine, $Id = 42) {
    [pscustomobject]@{ Name = $Name; CommandLine = $CommandLine; ProcessId = $Id }
}
Assert (Test-StiraProcess (Proc 'python.exe' ('python "' + $Entry + '"'))) 'quoted entry rejected'
Assert (Test-StiraProcess (Proc 'pythonw.exe' ('python "' + $Entry.ToUpper() + '"'))) 'case insensitive path rejected'
Assert (Test-StiraProcess (Proc 'stira.exe' ('stira "' + $Entry.Replace('\', '/') + '"'))) 'slash path rejected'
Assert (-not (Test-StiraProcess (Proc 'node.exe' ('node "' + $Entry + '"')))) 'non-python port owner accepted'
Assert (-not (Test-StiraProcess (Proc 'python.exe' 'python C:\other\dashboard\stira_server.py'))) 'other checkout accepted'
Assert (-not (Test-StiraProcess (Proc 'python.exe' 'python dashboard\stira_server.py'))) 'unknown CWD accepted'
Assert (-not (Test-StiraProcess (Proc 'python.exe' ('python "' + $Entry + '.bak"')))) 'path suffix accepted'
Assert (-not (Test-StiraProcess (Proc 'python.exe' $null))) 'missing command accepted'
Assert (-not (Test-StiraProcess $null)) 'missing process accepted'
$Entry = 'C:\repo\dashboard\stira_server.py'
Assert (Test-StiraProcess (Proc 'python.exe' "python $Entry --port 5555")) 'unquoted entry rejected'
$Entry = 'C:\test checkout\dashboard\stira_server.py'

# Port owners go through the same verification at the actual kill site.
function Get-CimInstance { param($ClassName, $Filter, $ErrorAction) $script:processInfo }
function Get-PortOwner { 42 }
function Start-Sleep { param($Seconds) }
function taskkill { $script:killed += 1 }
$script:killed = 0
$script:processInfo = Proc 'node.exe' 'node server.js'
$warnings = @()
Stop-Stira 3>&1 | ForEach-Object { $warnings += $_ }
Assert ($script:killed -eq 0) 'foreign port owner killed'
Assert ($warnings.Count -gt 0) 'skip warning missing'
$script:processInfo = Proc 'python.exe' 'python C:\other\dashboard\stira_server.py'
Stop-Stira 3>$null
Assert ($script:killed -eq 0) 'other checkout killed'
$script:processInfo = Proc 'python.exe' ('python "' + $Entry + '"')
Stop-Stira
Assert ($script:killed -eq 1) 'verified duplicate PID not killed exactly once'

function Get-PortOwner { $null }
function New-Item { param($ItemType, [switch]$Force, $Path) }
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $RedirectStandardOutput, $RedirectStandardError, $WindowStyle)
    $script:launchArgument = $ArgumentList
}
Start-Stira
Assert ($script:launchArgument -eq ('"' + $Entry + '"')) 'entry argument is not quoted'
function Get-PortOwner { 42 }
function Invoke-RestMethod {
    param($Uri, $TimeoutSec)
    @{ model = 'expected-model'; breaker = @{ level = 'normal' }; neural_state = @{} }
}
$output = Show-Status 6>&1 | Out-String
Assert ($output.Contains('model: expected-model')) 'status used wrong model field'
Write-Host "$script:passed passed / 0 failed (mocked PowerShell regression tests)"
