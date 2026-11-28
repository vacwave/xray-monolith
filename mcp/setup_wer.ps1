# One-time setup (run as admin): Windows Error Reporting local dumps for AnomalyDX11AVX.exe, so crashes that bypass
# the engine's own handler (__fastfail, heap corruption, stack overflow, replaced filters) still leave a dump.
# Grants the current user write access on the key, so the supervisor can switch DumpType (mid/full) per launch.
# Idempotent: re-running only fixes what differs.
$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Run this script from an elevated (admin) PowerShell."
    exit 1
}

$key = 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\AnomalyDX11AVX.exe'
$folder = Join-Path $env:LOCALAPPDATA 'anomaly-mcp\wer'
# MiniDumpWithThreadInfo | IndirectlyReferencedMemory | DataSegs | HandleData | UnloadedModules | ProcessThreadData | FullMemoryInfo
$flags = 0x1000 -bor 0x40 -bor 0x1 -bor 0x4 -bor 0x20 -bor 0x100 -bor 0x800

if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Force $folder | Out-Null; Write-Host "created $folder" }
if (-not (Test-Path $key)) { New-Item -Force $key | Out-Null; Write-Host "created $key" }

$values = [ordered]@{
    DumpFolder      = @('ExpandString', $folder)
    DumpCount       = @('DWord', 10)
    DumpType        = @('DWord', 0)
    CustomDumpFlags = @('DWord', $flags)
}
foreach ($name in $values.Keys) {
    $type, $value = $values[$name]
    $cur = (Get-ItemProperty -Path $key -Name $name -ErrorAction SilentlyContinue).$name
    if ($cur -ne $value) {
        New-ItemProperty -Path $key -Name $name -PropertyType $type -Value $value -Force | Out-Null
        Write-Host "set $name = $value"
    } else {
        Write-Host "ok  $name = $value"
    }
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$acl = Get-Acl $key
$rights = [Security.AccessControl.RegistryRights]'SetValue, QueryValues, ReadKey'
$has = $acl.Access | Where-Object { $_.IdentityReference -eq $user -and $_.AccessControlType -eq 'Allow' -and
    ($_.RegistryRights -band $rights) -eq $rights }
if ($has) {
    Write-Host "ok  $user can set values on the key"
} else {
    $rule = New-Object Security.AccessControl.RegistryAccessRule($user, $rights, 'Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -Path $key -AclObject $acl
    Write-Host "granted $user SetValue on the key"
}
Write-Host "WER local dumps for AnomalyDX11AVX.exe -> $folder"
