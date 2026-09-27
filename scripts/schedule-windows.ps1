<#
.SYNOPSIS
  Register (or remove) the Windows scheduled tasks that run paper-auto.

.DESCRIPTION
  Evening: every 30 minutes from 6:30 pm to 10:30 pm US Eastern on weekdays.
  The first run after Tiingo publishes the close does the work; the rest exit
  quickly. Morning: 10:00 am Eastern, reconcile and report fills (never trades).

  The tasks run as you, with normal (not administrator) rights, and only while
  you are logged on, so desktop notifications can appear. Whether orders are
  actually sent is decided by auto_submit in risk.toml, not by this script.

  Times are converted from US Eastern to this PC's time zone when you run the
  script. If your time zone changes, or it does not follow US daylight-saving
  dates, run the script again after each change.

.EXAMPLE
  .\scripts\schedule-windows.ps1
  .\scripts\schedule-windows.ps1 -Python C:\TradeNow\.venv\Scripts\python.exe
  .\scripts\schedule-windows.ps1 -Remove
#>
param(
    [string]$Python = '',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$eveningName = 'TradeNow paper-auto evening'
$morningName = 'TradeNow paper-auto morning check'

if ($Remove) {
    foreach ($name in @($eveningName, $morningName)) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host 'Removed the TradeNow scheduled tasks.'
    return
}

if (-not $Python) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$Python = (Resolve-Path -LiteralPath $Python).Path
# pythonw.exe runs without flashing a console window every 30 minutes.
$windowless = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
$interpreter = if (Test-Path -LiteralPath $windowless) { $windowless } else { $Python }

function ConvertFrom-Eastern([int]$Hour, [int]$Minute) {
    $eastern = [TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
    $today = [DateTime]::Today
    $time = [DateTime]::new($today.Year, $today.Month, $today.Day, $Hour, $Minute, 0,
                            [DateTimeKind]::Unspecified)
    return [TimeZoneInfo]::ConvertTime($time, $eastern, [TimeZoneInfo]::Local)
}

function Get-TradingDays([DateTime]$LocalStart) {
    # Monday to Friday in New York, shifted if the local time falls on another date.
    $offset = ($LocalStart.Date - [DateTime]::Today).Days
    return @(1..5 | ForEach-Object { [DayOfWeek](($_ + $offset + 7) % 7) })
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$eveningStart = ConvertFrom-Eastern 18 30
$evening = New-ScheduledTaskTrigger -Weekly -DaysOfWeek (Get-TradingDays $eveningStart) -At $eveningStart
$evening.Repetition = (New-ScheduledTaskTrigger -Once -At $eveningStart `
    -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 4)).Repetition
$eveningAction = New-ScheduledTaskAction -Execute $interpreter -Argument '-m tradenow paper-auto' `
    -WorkingDirectory $repo
Register-ScheduledTask -TaskName $eveningName -Trigger $evening -Action $eveningAction `
    -Principal $principal -Settings $settings -Force `
    -Description 'TradeNow: import the GLD close, plan, and (if auto_submit) send a PAPER order.' | Out-Null

$morningStart = ConvertFrom-Eastern 10 0
$morning = New-ScheduledTaskTrigger -Weekly -DaysOfWeek (Get-TradingDays $morningStart) -At $morningStart
$morningAction = New-ScheduledTaskAction -Execute $interpreter -Argument '-m tradenow paper-auto --check' `
    -WorkingDirectory $repo
Register-ScheduledTask -TaskName $morningName -Trigger $morning -Action $morningAction `
    -Principal $principal -Settings $settings -Force `
    -Description 'TradeNow: reconcile the PAPER account and report fills. Never trades.' | Out-Null

Write-Host "Registered for $user using $interpreter"
Write-Host ("  Evening: {0:HH:mm} local, every 30 minutes for 4 hours, {1}" -f $eveningStart, ((Get-TradingDays $eveningStart) -join ', '))
Write-Host ("  Morning: {0:HH:mm} local, {1}" -f $morningStart, ((Get-TradingDays $morningStart) -join ', '))
Write-Host 'Results: data\private\logs\runs.jsonl and the dashboard. Remove with -Remove.'
