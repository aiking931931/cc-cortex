# show-result.ps1 — WPF GUI window for scheduled task results
# Part of tempero: The Cognitive Layer for Claude Code
# Usage: powershell -NoProfile -File show-result.ps1 -TaskName <name> -ResultFile <path> ...
param(
    [Parameter(Mandatory)][string]$TaskName,
    [string]$ResultFile,
    [string]$LogFile,
    [int]$DurationSec = 0,
    [int]$ExitCode = 0,
    [string]$BudgetStatus = "",
    [int]$AutoCloseSeconds = 30,
    [string]$Locale = "en",
    [int]$MaxLines = 80
)

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

# ── i18n ──
$Strings = @{
    "en" = @{
        Title     = "Claude Schedule Result"
        Duration  = "Duration"
        Status    = "Status"
        Ok        = "OK"
        Failed    = "FAILED"
        Budget    = "BUDGET EXCEEDED"
        Summary   = "Result Summary"
        FullLog   = "Full log"
        Close     = "Close"
        OpenLog   = "Open Log"
        AutoClose = "Auto-closing in {0}s"
        NoOutput  = "No output. Check log file."
        Truncated = "... ({0} more lines, see full log)"
    }
    "zh-TW" = @{
        Title     = "Claude 排程結果"
        Duration  = "執行時間"
        Status    = "狀態"
        Ok        = "成功"
        Failed    = "失敗"
        Budget    = "預算超額"
        Summary   = "結果摘要"
        FullLog   = "完整日誌"
        Close     = "關閉"
        OpenLog   = "開啟日誌"
        AutoClose = "{0} 秒後自動關閉"
        NoOutput  = "無輸出，請檢查日誌檔"
        Truncated = "...（還有 {0} 行，見完整日誌）"
    }
    "ja" = @{
        Title     = "Claude スケジュール結果"
        Duration  = "実行時間"
        Status    = "ステータス"
        Ok        = "成功"
        Failed    = "失敗"
        Budget    = "予算超過"
        Summary   = "結果概要"
        FullLog   = "完全ログ"
        Close     = "閉じる"
        OpenLog   = "ログを開く"
        AutoClose = "{0} 秒後に自動で閉じます"
        NoOutput  = "出力なし。ログファイルを確認してください"
        Truncated = "...（残り {0} 行、完全ログ参照）"
    }
}
$S = if ($Strings.ContainsKey($Locale)) { $Strings[$Locale] } else { $Strings["en"] }

# ── Status ──
$isOk = ($ExitCode -eq 0) -and ($BudgetStatus -ne "exceeded")
$isBudget = ($BudgetStatus -eq "exceeded")
$statusText = if ($isBudget) { $S.Budget } elseif ($isOk) { $S.Ok } else { $S.Failed }
$statusColor = if ($isBudget) { "#FFC107" } elseif ($isOk) { "#4CAF50" } else { "#F44336" }
$statusIcon = if ($isBudget) { [char]0x26A0 } elseif ($isOk) { [char]0x2705 } else { [char]0x274C }
$headerBg = if ($isBudget) { "#2D2A1A" } elseif ($isOk) { "#1A2D1A" } else { "#2D1A1A" }
$durationStr = if ($DurationSec -gt 0) { "${DurationSec}s" } else { "N/A" }

# ── Read content ──
$content = ""
$totalLines = 0
if ($ResultFile -and (Test-Path $ResultFile)) {
    $allLines = @(Get-Content $ResultFile -Encoding UTF8 -ErrorAction SilentlyContinue)
    $totalLines = $allLines.Count
    if ($totalLines -gt $MaxLines) {
        $content = ($allLines | Select-Object -First $MaxLines) -join "`n"
        $content += "`n" + ($S.Truncated -f ($totalLines - $MaxLines))
    } else {
        $content = $allLines -join "`n"
    }
} else {
    $content = $S.NoOutput
}
$logPath = if ($LogFile) { $LogFile } else { "N/A" }

# Escape for XAML (content set programmatically to preserve newlines)
$escapedLogPath = [System.Security.SecurityElement]::Escape($logPath)
$escapedTask = [System.Security.SecurityElement]::Escape($TaskName)

[xml]$xaml = @"
<Window
    xmlns='http://schemas.microsoft.com/winfx/2006/xaml/presentation'
    xmlns:x='http://schemas.microsoft.com/winfx/2006/xaml'
    Title='CC Cortex | $($S.Title) - $escapedTask'
    Width='640' Height='480'
    MinWidth='480' MinHeight='320'
    WindowStartupLocation='CenterScreen'
    Background='#1E1E2E'
    Topmost='False'
    ResizeMode='CanResize'>
    <Window.Resources>
        <Style TargetType='Button' x:Key='BtnStyle'>
            <Setter Property='Background' Value='#3B3B5C'/>
            <Setter Property='Foreground' Value='White'/>
            <Setter Property='BorderThickness' Value='0'/>
            <Setter Property='Padding' Value='24,8'/>
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
    <Grid>
        <Grid.RowDefinitions>
            <RowDefinition Height='Auto'/>
            <RowDefinition Height='*'/>
            <RowDefinition Height='Auto'/>
            <RowDefinition Height='Auto'/>
        </Grid.RowDefinitions>

        <!-- Header -->
        <Border Grid.Row='0' Background='$headerBg' Padding='20,14'>
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width='Auto'/>
                    <ColumnDefinition Width='*'/>
                    <ColumnDefinition Width='Auto'/>
                </Grid.ColumnDefinitions>
                <TextBlock Grid.Column='0' Text='$statusIcon' FontSize='28' VerticalAlignment='Center' Margin='0,0,12,0'/>
                <StackPanel Grid.Column='1' VerticalAlignment='Center'>
                    <TextBlock Text='$escapedTask' FontSize='18' FontWeight='Bold' Foreground='#CDD6F4'/>
                    <TextBlock Foreground='#888' FontSize='11' Margin='0,2,0,0'>
                        <Run Text='$($S.Duration): $durationStr'/><Run Text='  |  '/><Run Text='$($S.Status): '/><Run Text='$statusText' Foreground='$statusColor' FontWeight='Bold'/>
                    </TextBlock>
                </StackPanel>
                <TextBlock Grid.Column='2' Text='$statusText' FontSize='20' FontWeight='Bold' Foreground='$statusColor' VerticalAlignment='Center'/>
            </Grid>
        </Border>

        <!-- Content -->
        <Border Grid.Row='1' Margin='12,8' Background='#181825' CornerRadius='8' Padding='4'>
            <ScrollViewer VerticalScrollBarVisibility='Auto' HorizontalScrollBarVisibility='Disabled'>
                <TextBox Name='txtContent' IsReadOnly='True' Background='Transparent' Foreground='#CDD6F4'
                         BorderThickness='0' FontFamily='Consolas' FontSize='12' TextWrapping='Wrap'
                         Padding='12,8' AcceptsReturn='True'/>
            </ScrollViewer>
        </Border>

        <!-- Log path -->
        <Border Grid.Row='2' Margin='12,0,12,4' Padding='8,4'>
            <TextBlock Foreground='#666' FontSize='10'>
                <Run Text='$($S.FullLog): '/><Run Text='$escapedLogPath' Foreground='#888'/>
            </TextBlock>
        </Border>

        <!-- Footer -->
        <Border Grid.Row='3' Background='#16161E' Padding='16,10'>
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width='Auto'/>
                    <ColumnDefinition Width='*'/>
                    <ColumnDefinition Width='Auto'/>
                    <ColumnDefinition Width='Auto'/>
                </Grid.ColumnDefinitions>
                <TextBlock Grid.Column='0' Name='txtCountdown' Text='' Foreground='#555' FontSize='11' VerticalAlignment='Center'/>
                <TextBlock Grid.Column='1' Text='CC Cortex' Foreground='#333' FontSize='10' VerticalAlignment='Center' HorizontalAlignment='Center'/>
                <Button Grid.Column='2' Content='$($S.OpenLog)' Name='btnOpenLog' Style='{StaticResource BtnStyle}' Margin='0,0,8,0'/>
                <Button Grid.Column='3' Content='$($S.Close)' Name='btnClose' Style='{StaticResource BtnStyle}' Background='#5B3B3B'/>
            </Grid>
        </Border>
    </Grid>
</Window>
"@

$reader = (New-Object System.Xml.XmlNodeReader $xaml)
$window = [Windows.Markup.XamlReader]::Load($reader)

# Set content programmatically to preserve newlines
$window.FindName("txtContent").Text = $content

$btnClose = $window.FindName("btnClose")
$btnOpenLog = $window.FindName("btnOpenLog")

$btnClose.Add_Click({ $window.Close() })
$btnOpenLog.Add_Click({
    if ($LogFile -and (Test-Path $LogFile)) {
        Start-Process notepad.exe $LogFile
    }
})

$window.ShowDialog() | Out-Null
