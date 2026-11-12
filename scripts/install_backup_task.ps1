param(
    [string]$ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [string]$BackupDirectory = "",
    [string]$At = "02:00",
    [string]$TaskName = "SEPS-Daily-Backup"
)
$ErrorActionPreference = "Stop"
$ProjectPath = (Resolve-Path $ProjectPath).Path
$Python = Join-Path $ProjectPath ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Create the .venv described in README first." }
if (-not $BackupDirectory) { $BackupDirectory = Join-Path $ProjectPath "backups" }
if ($BackupDirectory.Contains('"')) { throw "Invalid backup directory." }
$Arguments = '-m scripts.backup create --keep 7 --directory "{0}"' -f $BackupDirectory
$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $ProjectPath
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
# Without -Force: never silently replace another task with the same name.
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "SEPS pg_dump, retain 7 successful backups"
