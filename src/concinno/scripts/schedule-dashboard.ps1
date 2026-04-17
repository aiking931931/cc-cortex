# schedule-dashboard.ps1 — Schedule Status Dashboard (WPF GUI)
# Part of tempero: The Cognitive Layer for Claude Code
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File schedule-dashboard.ps1
param(
    [string]$ConfigFile = "$env:USERPROFILE\.claude\hooks\schedule_config.json",
    [string]$LogDir = "$env:USERPROFILE\.claude\logs"
)

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

# ── Gather Data ──
$tasks = @()
$scheduledTasks = @(
    @{ Name = "SelfReflection";  Display = "Self Reflection";  Schedule = "Every day 09:00";  TaskName = "Claude-SelfReflection";  LogFile = "self-reflection.log" }
    @{ Name = "Scavenger";       Display = "Scavenger";         Schedule = "Every day 12:00";  TaskName = "Claude-Scavenger";       LogFile = "scavenger.log" }
    @{ Name = "WeeklyResearch";  Display = "Weekly Research";   Schedule = "Sunday 10:00";     TaskName = "Claude-WeeklyResearch";  LogFile = "weekly-research.log" }
    @{ Name = "ProcessGuard";    Display = "Process Guard";     Schedule = "Every 15 min";     TaskName = "Claude-ProcessGuard";    LogFile = "process-guard.log" }
    @{ Name = "BackupCleanup";   Display = "Backup Cleanup";    Schedule = "Every day 16:00";  TaskName = "Claude-BackupCleanup";   LogFile = "backup-cleanup.log" }
    @{ Name = "LocalCleanup";    Display = "Local Cleanup";     Schedule = "Every day 15:00";  TaskName = "Claude-LocalCleanup";    LogFile = "local-cleanup.log" }
    @{ Name = "AutoAgent";       Display = "Auto Agent";        Schedule = "Every 3 hours";    TaskName = "Claude-AutoAgent";       LogFile = "auto-agent.log" }
)

# Read config
$config = @{}
if (Test-Path $ConfigFile) {
    try { $config = Get-Content $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
}

foreach ($st in $scheduledTasks) {
    $info = @{
        Display  = $st.Display
        Schedule = $st.Schedule
        Status   = "Unknown"
        LastRun  = "N/A"
        NextRun  = "N/A"
        Enabled  = $true
        LogSize  = "N/A"
        LastLog  = ""
    }

    # Check Task Scheduler
    try {
        $t = Get-ScheduledTask -TaskName $st.TaskName -ErrorAction SilentlyContinue
        if ($t) {
            $info.Status = $t.State.ToString()
            $info.Enabled = ($t.State -ne "Disabled")
            $ti = $t | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
            if ($ti) {
                if ($ti.LastRunTime -and $ti.LastRunTime.Year -gt 2000) {
                    $info.LastRun = $ti.LastRunTime.ToString("MM/dd HH:mm")
                }
                if ($ti.NextRunTime -and $ti.NextRunTime.Year -gt 2000) {
                    $info.NextRun = $ti.NextRunTime.ToString("MM/dd HH:mm")
                }
            }
        } else {
            $info.Status = "Not Found"
            $info.Enabled = $false
        }
    } catch {
        $info.Status = "Error"
    }

    # Check config override
    if ($config.tasks) {
        $taskKey = switch ($st.Name) {
            "SelfReflection" { "self-reflection" }
            "Scavenger"      { "scavenger" }
            "WeeklyResearch"  { "weekly-research" }
            "AutoAgent"      { "auto-agent" }
            default          { $null }
        }
        if ($taskKey -and $config.tasks.$taskKey) {
            $tc = $config.tasks.$taskKey
            if ($null -ne $tc.enabled -and -not $tc.enabled) {
                $info.Enabled = $false
                $info.Status = "Disabled (config)"
            }
        }
    }

    # Check log file
    $logPath = Join-Path $LogDir $st.LogFile
    if (Test-Path $logPath) {
        $logItem = Get-Item $logPath
        $sizeKB = [math]::Round($logItem.Length / 1024, 1)
        $info.LogSize = "${sizeKB} KB"
        # Last 3 meaningful lines
        try {
            $lines = @(Get-Content $logPath -Tail 10 -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne "" })
            if ($lines.Count -gt 0) {
                $info.LastLog = ($lines | Select-Object -Last 3) -join "`n"
            }
        } catch {}
    }

    $tasks += $info
}

# ── Build XAML ──
$now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$rowDefs = ""
$rowContent = ""
$row = 1
foreach ($t in $tasks) {
    $rowDefs += "<RowDefinition Height='Auto'/>`n"

    # Status color + icon
    if (-not $t.Enabled) {
        $statusColor = "#888888"
        $statusIcon = [char]0x23F8  # pause
        $statusText = "Disabled"
        $rowBg = "#1A888888"
    } elseif ($t.Status -eq "Ready") {
        $statusColor = "#4CAF50"
        $statusIcon = [char]0x2705  # check
        $statusText = "Ready"
        $rowBg = "#1A4CAF50"
    } else {
        $statusColor = "#FF9800"
        $statusIcon = [char]0x26A0  # warning
        $statusText = $t.Status
        $rowBg = "#1AFF9800"
    }

    $escapedLog = [System.Security.SecurityElement]::Escape($t.LastLog)
    if ($escapedLog.Length -gt 200) { $escapedLog = $escapedLog.Substring(0, 200) + "..." }

    $rowContent += @"
    <Border Grid.Row='$row' Background='$rowBg' CornerRadius='6' Margin='4,3' Padding='12,8'>
        <Grid>
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width='30'/>
                <ColumnDefinition Width='130'/>
                <ColumnDefinition Width='90'/>
                <ColumnDefinition Width='90'/>
                <ColumnDefinition Width='90'/>
                <ColumnDefinition Width='*'/>
            </Grid.ColumnDefinitions>
            <TextBlock Grid.Column='0' Text='$statusIcon' FontSize='16' VerticalAlignment='Center'/>
            <StackPanel Grid.Column='1' VerticalAlignment='Center'>
                <TextBlock Text='$($t.Display)' FontWeight='Bold' Foreground='White' FontSize='13'/>
                <TextBlock Text='$($t.Schedule)' Foreground='#AAAAAA' FontSize='10'/>
            </StackPanel>
            <TextBlock Grid.Column='2' Text='$statusText' Foreground='$statusColor' VerticalAlignment='Center' FontSize='12'/>
            <TextBlock Grid.Column='3' Text='$($t.LastRun)' Foreground='#CCCCCC' VerticalAlignment='Center' FontSize='11'/>
            <TextBlock Grid.Column='4' Text='$($t.NextRun)' Foreground='#CCCCCC' VerticalAlignment='Center' FontSize='11'/>
            <TextBlock Grid.Column='5' Text='$($t.LogSize)' Foreground='#999999' VerticalAlignment='Center' FontSize='11' HorizontalAlignment='Right'/>
        </Grid>
    </Border>
"@
    $row++
}

[xml]$xaml = @"
<Window
    xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'
    xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml'
    Title='Claude Schedule Dashboard'
    Width='720' Height='520'
    WindowStartupLocation='CenterScreen'
    Background='#1E1E2E'
    ResizeMode='CanResize'>
    <Window.Resources>
        <Style TargetType='Button'>
            <Setter Property='Background' Value='#3B3B5C'/>
            <Setter Property='Foreground' Value='White'/>
            <Setter Property='BorderThickness' Value='0'/>
            <Setter Property='Padding' Value='20,8'/>
            <Setter Property='FontSize' Value='13'/>
            <Setter Property='Cursor' Value='Hand'/>
            <Setter Property='Template'>
                <Setter.Value>
                    <ControlTemplate TargetType='Button'>
                        <Border x:Name='bd' Background='{TemplateBinding Background}' CornerRadius='6' Padding='{TemplateBinding Padding}'>
                            <ContentPresenter HorizontalAlignment='Center' VerticalAlignment='Center'/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property='IsMouseOver' Value='True'>
                                <Setter TargetName='bd' Property='Background' Value='#5050A0'/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
    </Window.Resources>
    <Grid Margin='16'>
        <Grid.RowDefinitions>
            <RowDefinition Height='Auto'/>
            <RowDefinition Height='Auto'/>
            <RowDefinition Height='*'/>
            <RowDefinition Height='Auto'/>
        </Grid.RowDefinitions>

        <!-- Header -->
        <StackPanel Grid.Row='0' Margin='0,0,0,8'>
            <TextBlock Text='Claude Schedule Dashboard' FontSize='22' FontWeight='Bold' Foreground='#CDD6F4'/>
            <TextBlock Text='$now' Foreground='#888' FontSize='11' Margin='0,2,0,0'/>
        </StackPanel>

        <!-- Column Headers -->
        <Border Grid.Row='1' Background='#2A2A40' CornerRadius='6' Padding='12,6' Margin='4,0'>
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width='30'/>
                    <ColumnDefinition Width='130'/>
                    <ColumnDefinition Width='90'/>
                    <ColumnDefinition Width='90'/>
                    <ColumnDefinition Width='90'/>
                    <ColumnDefinition Width='*'/>
                </Grid.ColumnDefinitions>
                <TextBlock Grid.Column='1' Text='Task' Foreground='#888' FontSize='11' FontWeight='Bold'/>
                <TextBlock Grid.Column='2' Text='Status' Foreground='#888' FontSize='11' FontWeight='Bold'/>
                <TextBlock Grid.Column='3' Text='Last Run' Foreground='#888' FontSize='11' FontWeight='Bold'/>
                <TextBlock Grid.Column='4' Text='Next Run' Foreground='#888' FontSize='11' FontWeight='Bold'/>
                <TextBlock Grid.Column='5' Text='Log Size' Foreground='#888' FontSize='11' FontWeight='Bold' HorizontalAlignment='Right'/>
            </Grid>
        </Border>

        <!-- Task Rows -->
        <ScrollViewer Grid.Row='2' VerticalScrollBarVisibility='Auto' Margin='0,4'>
            <Grid>
                <Grid.RowDefinitions>
                    $rowDefs
                </Grid.RowDefinitions>
                $rowContent
            </Grid>
        </ScrollViewer>

        <!-- Footer -->
        <Grid Grid.Row='3' Margin='0,10,0,0'>
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width='*'/>
                <ColumnDefinition Width='Auto'/>
                <ColumnDefinition Width='Auto'/>
            </Grid.ColumnDefinitions>
            <TextBlock Grid.Column='0' Text='tempero | The Cognitive Layer for Claude Code' Foreground='#555' FontSize='10' VerticalAlignment='Center'/>
            <Button Grid.Column='1' Content='Refresh' Name='btnRefresh' Margin='0,0,8,0'/>
            <Button Grid.Column='2' Content='Close' Name='btnClose' Background='#5B3B3B'/>
        </Grid>
    </Grid>
</Window>
"@

# ── Create Window ──
$reader = (New-Object System.Xml.XmlNodeReader $xaml)
$window = [Windows.Markup.XamlReader]::Load($reader)

# ── Button Events ──
$window.FindName("btnClose").Add_Click({ $window.Close() })
$window.FindName("btnRefresh").Add_Click({
    $window.Close()
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -WindowStyle Normal
})

# ── Show ──
$window.ShowDialog() | Out-Null
