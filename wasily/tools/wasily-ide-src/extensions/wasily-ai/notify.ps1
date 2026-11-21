param(
  [string]$Title = 'Wasily AI',
  [string]$Body = '',
  [string]$Id = '',
  [string]$Bridge = 'http://127.0.0.1:18753/notify_click',
  [int]$Seconds = 8,
  [switch]$FlashOnly,
  [switch]$RestoreOnly,
  [string]$ClickUri = ''
)

# Wasily IDE task notification helper (ASCII-only source: Windows PowerShell 5.1
# reads non-BOM .ps1 files as ANSI, so non-ASCII literals here would break parsing).
# 1) Register a stable AppUserModelID + wasily-notify: protocol (HKCU only, first run).
# 2) Show a WinRT toast (real Windows system notification; visible when IDE minimized).
# 3) Clicking the toast runs this script with -ClickUri, which posts the id back to the
#    extension bridge so the IDE focuses the session and highlights the result.
# 4) If the shell does not accept the toast, fall back to a topmost clickable popup.
# 5) Always flashes the IDE taskbar button.

$ErrorActionPreference = 'SilentlyContinue'
$Aumid = 'Wasily.IDE.AI'
$Protocol = 'wasily-notify'
# "View result"
$viewLabel = [string]([char]0x67E5 + [char]0x770B + [char]0x7ED3 + [char]0x679C)

# ---- click handler (protocol activation) ----
if ($ClickUri -ne '') {
    $clickId = $ClickUri -replace ($Protocol + ':', '')
    if ($clickId -ne '' -and $Bridge -ne '') {
        try {
            Invoke-WebRequest -Uri $Bridge -Method POST -ContentType 'application/json' `
                -Body (@{ id = $clickId } | ConvertTo-Json) -TimeoutSec 3 -UseBasicParsing | Out-Null
        } catch { }
    }
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$flashCode = @'
using System;
using System.Runtime.InteropServices;
public class WasilyFlash {
    [StructLayout(LayoutKind.Sequential)]
    public struct FLASHWINFO { public IntPtr hwnd; public uint dwFlags; public uint uCount; public uint dwTimeout; }
    [DllImport("user32.dll")] public static extern bool FlashWindowEx(ref FLASHWINFO pfwi);
    public const uint FLASHW_ALL = 3;
    public const uint FLASHW_TIMERNOFG = 12;
    public static void Flash(IntPtr hwnd) {
        FLASHWINFO f = new FLASHWINFO();
        f.hwnd = hwnd;
        f.dwFlags = FLASHW_ALL | FLASHW_TIMERNOFG;
        f.uCount = 0;
        f.dwTimeout = 0;
        FlashWindowEx(ref f);
    }
}
'@
$flashReady = $false
try {
    if (-not ('WasilyFlash' -as [type])) { Add-Type -TypeDefinition $flashCode -ErrorAction Stop }
    $flashReady = $true
} catch { }

function Get-IdeWindow {
    foreach ($n in @('wasily-ide', 'VSCodium', 'Code')) {
        $p = Get-Process -Name $n -ErrorAction SilentlyContinue |
             Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
        if ($p) { return $p.MainWindowHandle }
    }
    $p = Get-Process -ErrorAction SilentlyContinue |
         Where-Object { $_.MainWindowTitle -ne '' -and $_.MainWindowTitle -like '*Wasily*' } |
         Select-Object -First 1
    if ($p) { return $p.MainWindowHandle }
    return [IntPtr]::Zero
}

if ($flashReady) {
    try {
        $hwnd = Get-IdeWindow
        if ($hwnd -ne [IntPtr]::Zero) { [WasilyFlash]::Flash($hwnd) }
    } catch { }
}

if ($FlashOnly) { exit 0 }

# ---- restore / foreground the IDE window (called by the extension on notify click) ----
if ($RestoreOnly) {
    $restoreCode = @'
using System;
using System.Runtime.InteropServices;
public class WasilyRestore {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int cmd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    public static void Do(IntPtr hwnd) {
        ShowWindow(hwnd, 9);
        BringWindowToTop(hwnd);
        SetForegroundWindow(hwnd);
    }
}
'@
    try {
        if (-not ('WasilyRestore' -as [type])) { Add-Type -TypeDefinition $restoreCode -ErrorAction Stop }
        $hwnd = Get-IdeWindow
        if ($hwnd -ne [IntPtr]::Zero) { [WasilyRestore]::Do($hwnd) }
    } catch { }
    exit 0
}

# ---- first run: register AUMID + protocol (HKCU only) ----
try {
    $aumidPath = "HKCU:\Software\Classes\AppUserModelId\$Aumid"
    if (-not (Test-Path $aumidPath)) {
        New-Item -Path $aumidPath -Force | Out-Null
        Set-ItemProperty -Path $aumidPath -Name DisplayName -Value 'Wasily AI'
        Set-ItemProperty -Path $aumidPath -Name IconUri -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    }
    $protoPath = "HKCU:\Software\Classes\$Protocol"
    if (-not (Test-Path $protoPath)) {
        New-Item -Path $protoPath -Force | Out-Null
        Set-ItemProperty -Path $protoPath -Name '(default)' -Value 'URL:Wasily AI notification'
        Set-ItemProperty -Path $protoPath -Name 'URL Protocol' -Value ''
        New-Item -Path "$protoPath\shell\open\command" -Force | Out-Null
        $psExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $cmd = '"' + $psExe + '" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -ClickUri "%1"'
        Set-ItemProperty -Path "$protoPath\shell\open\command" -Name '(default)' -Value $cmd
    }
} catch { }

# ---- primary channel: WinRT toast ----
function Show-Toast {
    param([string]$T, [string]$B)
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $et = [System.Security.SecurityElement]::Escape($T)
    $eb = [System.Security.SecurityElement]::Escape($B)
    $launch = $Protocol + ':' + $Id
    $xmlText = @"
<Toast activationType="protocol" launch="$launch">
  <Visual><Binding template="ToastGeneric"><Text>$et</Text><Text>$eb</Text></Binding></Visual>
  <Actions><Action content="$viewLabel" activationType="protocol" arguments="$launch" /></Actions>
  <Audio silent="true" />
</Toast>
"@
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($xmlText)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($Aumid).Show($toast)
}

function Show-FallbackPopup {
    param([string]$T, [string]$B)
    $form = New-Object System.Windows.Forms.Form
    $form.FormBorderStyle = 'None'
    $form.StartPosition = 'Manual'
    $form.TopMost = $true
    $form.ShowInTaskbar = $false
    $form.BackColor = [System.Drawing.Color]::FromArgb(30, 31, 36)
    $form.Width = 340
    $form.Height = 96
    $screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $form.Left = $screen.Right - $form.Width - 16
    $form.Top = $screen.Bottom - $form.Height - 16

    $lbl1 = New-Object System.Windows.Forms.Label
    $lbl1.Text = $T
    $lbl1.ForeColor = [System.Drawing.Color]::FromArgb(230, 233, 242)
    $lbl1.Font = New-Object System.Drawing.Font('Microsoft YaHei', 10, [System.Drawing.FontStyle]::Bold)
    $lbl1.AutoSize = $false
    $lbl1.Left = 12; $lbl1.Top = 10; $lbl1.Width = 316; $lbl1.Height = 22

    $lbl2 = New-Object System.Windows.Forms.Label
    $lbl2.Text = $B
    $lbl2.ForeColor = [System.Drawing.Color]::FromArgb(139, 141, 149)
    $lbl2.Font = New-Object System.Drawing.Font('Microsoft YaHei', 9)
    $lbl2.AutoSize = $false
    $lbl2.Left = 12; $lbl2.Top = 34; $lbl2.Width = 316; $lbl2.Height = 40

    $form.Controls.Add($lbl1)
    $form.Controls.Add($lbl2)

    $script:wfForm = $form
    $script:wfClicked = $false
    $form.Add_MouseClick({ $script:wfClicked = $true; $script:wfForm.Close() })
    $lbl1.Add_MouseClick({ $script:wfClicked = $true; $script:wfForm.Close() })
    $lbl2.Add_MouseClick({ $script:wfClicked = $true; $script:wfForm.Close() })

    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = $Seconds * 1000
    $script:wfTimer = $timer
    $timer.Add_Tick({ $script:wfTimer.Stop(); $script:wfForm.Close() })
    $timer.Start()

    [void]$form.ShowDialog()
    if ($script:wfClicked -and $Id -ne '' -and $Bridge -ne '') {
        try {
            Invoke-WebRequest -Uri $Bridge -Method POST -ContentType 'application/json' `
                -Body (@{ id = $Id } | ConvertTo-Json) -TimeoutSec 3 -UseBasicParsing | Out-Null
        } catch { }
    }
}

$toastOk = $false
try {
    Show-Toast -T $Title -B $Body
    Start-Sleep -Milliseconds 700
    # The shell only creates the per-app notification settings key when it accepts a toast.
    if (Test-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\$Aumid") {
        $toastOk = $true
    }
} catch { }

if (-not $toastOk) {
    Show-FallbackPopup -T $Title -B $Body
}
